# Design notes

Deeper notes on how agentwatch decides what a watched process is doing, and
the cases where that decision can be wrong. Everything here describes the code
in `agentwatch.py` as it is; where a case is not handled, it says so.

## States of a watched process

agentwatch does not store a state name. The state is what `Supervisor.step`
sees from three facts: is the process alive, did the log mtime change since
the last tick, and has the current stall already been reported (`stalled`).

```
                 mtime changed                       mtime unchanged for
  +-----------+  (any tick)         +-----------+   --stall-after seconds   +-----------+
  |  WRITING  | <----------------- |   QUIET   | ----------------------->  |  STALLED  |
  | stalled=F |                     | stalled=F |                           | stalled=T |
  +-----------+                     +-----------+                           +-----------+
        |                                 |                                       |
        | process gone                    | process gone                          | mtime changed: back to WRITING (warn only)
        v                                 v                                       | policy kill:    kill, exited, stop
  +-----------+                                                                   | policy restart: kill, then restart or gave_up
  |   GONE    |  exited; restart if policy=restart, spawned child, rc != 0        v
  +-----------+  else stop with EXIT_OK (rc 0 or external) or EXIT_KILLED
```

- **WRITING** and **QUIET** are the same code path with different idle times.
  There is no event for entering QUIET. `heartbeat` reports `idle_seconds`
  so you can see it.
- **STALLED** is entered once per stall. `stalled=True` stops `step` from
  writing `stall_detected` again on every tick. It is cleared by an mtime
  change or by `spawn`.
- A restart goes through `spawn`, which resets `last_change` to now and
  `last_mtime` to the log's current mtime. The new process gets a full
  `--stall-after` before it can be called stalled.
- Without `--log` the process can never be STALLED. `idle_seconds` in
  heartbeats then grows from start to exit, because `last_change` is never
  updated.

## Restart budget

`restarts` counts restarts used. `restart` writes `gave_up` when
`restarts >= max_restarts` before spending one. So `--max-restarts 2` gives
three process lifetimes: the original and two restarts. The count never
resets, whatever the uptime between stalls.

Delay before restart n (0-based) is `min(base * 2**n, cap)`. With the
defaults that is 1, 2, 4, 8, 16, 32, 60, 60 seconds. The delay is slept
inside `restart`, so no ticks run and no heartbeats are written during it.

## Edge cases

### Zombie PIDs

An external `--pid` is checked with `kill(pid, 0)`. That succeeds on a
zombie, so a process that has exited but has not been reaped by its parent
counts as alive. Its log will not change, so with `--log` it is reported as
a stall after `--stall-after`; `kill` and `restart` then send SIGTERM and
SIGKILL, which do nothing to a zombie, and `kill_pid` returns `SIGKILL`
after the grace period. Only the parent reaping it makes it go away.

Spawned children do not have this problem: `is_alive` uses `Popen.poll`,
which reaps the child.

### PID reuse

If an external process dies and the OS gives its PID to a new process before
the next tick, agentwatch keeps watching the new process as if it were the
old one. The window is one `--interval`. PIDs are handed out in increasing
order and wrap only at the system's PID limit, so this needs a lot of
process churn inside one interval. It is not detected.
Spawned children are checked through `Popen.poll`, which tracks the child
itself, not the number.

### Clock jumps

The stall timer is `now - last_change`, both from `clock()`, which is
`time.time` by default. A backward step of s seconds makes the current quiet
period look s seconds shorter; a forward step makes it look s seconds longer
and can report a stall early. The mtime itself is only compared for
equality with the previous reading, so a jump in the file's timestamp
relative to the system clock has no effect. `kill_pid` uses
`time.monotonic` for its grace period and is unaffected.

### Symlinked logs

`log_mtime` calls `os.stat`, which follows symlinks. The mtime reported is
the target's. Repointing the symlink to a different file changes the result
and counts as progress. A dangling symlink reads as a missing file (None).

### Missing, deleted, rotated, or truncated logs

- Missing at start: `last_mtime` is None. Creating the file is a change.
- Deleted during the run: the reading goes from a float to None, which is a
  change and re-arms the stall. From then on None equals None, and a stall
  is reported after `--stall-after` if nothing recreates the file.
- Rotated (renamed away and recreated) or truncated: the new mtime differs,
  so it counts as progress. If the rotated file is written by something
  other than the agent, that is a false sign of life.
- Rewritten with the mtime set back to the previous value (for example a
  copy that preserves mtime): equal readings, not a change.

### Output buffering

Nothing in agentwatch reads the log. It only asks the filesystem for the
mtime, which updates when bytes reach the file. A runtime that buffers
stdout when it is not a terminal (Python, most C stdio) writes nothing until
the buffer fills or the program exits, so a busy process can look quiet for
minutes. Run the agent with `python -u`, set `PYTHONUNBUFFERED=1`, wrap it
in `stdbuf -oL`, or have it flush after each line. Set `--stall-after` with
the buffer in mind if you cannot change the agent.

### Processes that cannot be signalled

`kill(pid, 0)` returns EPERM for a process that exists but belongs to
another user. `main` treats that as alive for `--policy warn` and refuses to
start for `kill` and `restart`, exiting 3. If a process becomes
unsignallable during a run, `kill_pid` raises `PermissionError` for an
external PID. For a spawned child, `killpg` EPERM is treated as "already
gone", because macOS returns EPERM from `killpg` when every member of the
group has exited.

### Ctrl-C

`run` catches `KeyboardInterrupt`, writes `exited` with reason
`agentwatch_interrupted`, and returns 130. A spawned child is in its own
session and is not signalled, so it keeps running. An external PID is left
alone as well.

### Half-written events

`emit` opens the file for append and writes one line per event. `agentwatch
tail` reads the whole file and, on a line that is not yet complete JSON,
prints one line naming the line number and exits 3. Rerun it; the line will
be complete once the write returns.
