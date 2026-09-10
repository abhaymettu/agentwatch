# agentwatch

A small supervisor for one long-running CLI agent process on one machine.

It watches a PID and a log file. If the process is alive but the log file's
modification time has not changed for N seconds, that is a stall. On a stall it
does one of three things: warn, kill, or kill and restart with exponential
backoff. Every event is appended to a JSONL file so you can read what happened
the next morning.

Single file, standard library only, Python 3.11 or newer, POSIX.

## What it does

- Polls a PID (yours or one it spawned from `--cmd`) and a log file's mtime.
- Reports `stall_detected` when the process is alive and the log has been
  quiet for `--stall-after` seconds. One report per stall; it re-arms when the
  log changes again.
- `--policy warn`: report the stall and keep watching.
- `--policy kill`: SIGTERM, wait `--grace` seconds, SIGKILL. Exit code 1.
- `--policy restart`: kill as above, wait `base * 2^n` seconds (capped by
  `--backoff-cap`), run `--cmd` again. Also restarts a spawned child that
  exits nonzero. After `--max-restarts` restarts it writes `gave_up` and exits
  with code 2.
- Writes a `heartbeat` event every `--heartbeat` seconds with idle time and
  restart count.
- `agentwatch tail events.jsonl` prints the events one per line plus counts.

## What it does not do

- No distributed supervision. One process, one machine, one agentwatch.
- No scheduling. It does not start jobs at a time of day. Use cron or launchd.
- No log parsing. It only looks at the file's mtime, never its content.
- No daemonizing. Run it under tmux, nohup, or a service manager.
- No memory or CPU limits, no health checks beyond "alive and writing".
- Killing an external `--pid` signals that PID only. Its children are not
  tracked. Processes agentwatch spawned itself get their own session and are
  killed as a group.
- Ctrl-C stops agentwatch, not the process it spawned. The child is in its
  own session, so the terminal's SIGINT does not reach it, and agentwatch
  does not forward it. Kill the child yourself if you want it gone.
- No Windows. It uses POSIX signals.

## Install

Python 3.11 or newer. No dependencies outside the standard library. pytest is
only used for the tests.

```
pip install git+https://github.com/abhaymettu/agentwatch
```

Or, to work on it:

```
git clone https://github.com/abhaymettu/agentwatch
cd agentwatch
./setup.sh
. .venv/bin/activate
```

`setup.sh` creates `.venv`, installs the package in editable mode, and runs
the tests. Running it again is safe.

## Usage

Watch a process you already started:

```
agentwatch watch --pid 1234 --log run.log --stall-after 300 --policy warn --events events.jsonl
```

Start the process yourself and restart it on stalls, up to 3 times:

```
agentwatch watch --log run.log --stall-after 300 --policy restart \
  --cmd "python agent.py > run.log 2>&1" --max-restarts 3 --events events.jsonl
```

Read what happened:

```
agentwatch tail events.jsonl
```

### Real runs

Everything below was captured from real runs on macOS on 2026-09-10 and is
pasted verbatim. The stall windows are seconds instead of minutes so the runs
finish quickly; nothing else is special.

**warn.** The command writes three lines one second apart, goes quiet for
three seconds, writes once more, and exits 0. `--stall-after 2` catches the
quiet period. The `heartbeat` fires because `--heartbeat 3` is short here.

```
$ agentwatch watch --log warn.log --stall-after 2 --interval 0.5 --heartbeat 3 --policy warn \
    --events warn.jsonl --cmd 'for i in 1 2 3; do echo tick $i >> warn.log; sleep 1; done; sleep 3; echo done >> warn.log'
agentwatch started {"cmd": "for i in 1 2 3; do echo tick $i >> warn.log; sleep 1; done; sleep 3; echo done >> warn.log", "log": "warn.log", "max_restarts": 3, "pid": 57672, "policy": "warn", "restart": 0, "stall_after": 2.0}
agentwatch heartbeat {"idle_seconds": 1.1, "pid": 57672, "restarts": 0}
agentwatch stall_detected {"idle_seconds": 2.2, "pid": 57672, "policy": "warn"}
agentwatch exited {"pid": 57672, "returncode": 0}
$ echo $?
0
$ agentwatch tail warn.jsonl
2026-09-10T16:07:57+00:00  started         cmd=for i in 1 2 3; do echo tick $i >> warn.log; sleep 1; done; sleep 3; echo done >> warn.log log=warn.log max_restarts=3 pid=57672 policy=warn restart=0 stall_after=2.0
2026-09-10T16:08:00+00:00  heartbeat       idle_seconds=1.1 pid=57672 restarts=0
2026-09-10T16:08:01+00:00  stall_detected  idle_seconds=2.2 pid=57672 policy=warn
2026-09-10T16:08:04+00:00  exited          pid=57672 returncode=0
--
started=1  heartbeat=1  stall_detected=1  exited=1
```

**kill.** The command writes once and sleeps. SIGTERM ends it within the
grace period, so the child's return code is -15 and agentwatch exits 1.

```
$ agentwatch watch --log kill.log --stall-after 2 --interval 0.5 --policy kill --grace 1 \
    --events kill.jsonl --cmd 'echo start >> kill.log; sleep 60'
agentwatch started {"cmd": "echo start >> kill.log; sleep 60", "log": "kill.log", "max_restarts": 3, "pid": 57835, "policy": "kill", "restart": 0, "stall_after": 2.0}
agentwatch stall_detected {"idle_seconds": 2.3, "pid": 57835, "policy": "kill"}
agentwatch action_taken {"action": "kill", "pid": 57835, "signal": "SIGTERM"}
agentwatch exited {"pid": 57835, "returncode": -15}
$ echo $?
1
```

**restart.** Same quiet command, `--max-restarts 2`. Each restart backs off
for twice as long as the last (1 s, then 2 s). The third stall spends the
budget: the process is killed, `gave_up` is written, and agentwatch exits 2.

```
$ agentwatch watch --log restart.log --stall-after 1 --interval 0.5 --policy restart --grace 1 \
    --max-restarts 2 --events restart.jsonl --cmd 'echo tick >> restart.log; sleep 30'
agentwatch started {"cmd": "echo tick >> restart.log; sleep 30", "log": "restart.log", "max_restarts": 2, "pid": 57873, "policy": "restart", "restart": 0, "stall_after": 1.0}
agentwatch stall_detected {"idle_seconds": 1.1, "pid": 57873, "policy": "restart"}
agentwatch action_taken {"action": "kill", "pid": 57873, "signal": "SIGTERM"}
agentwatch action_taken {"action": "restart", "attempt": 1, "backoff_seconds": 1.0, "max_restarts": 2, "reason": "stall"}
agentwatch started {"cmd": "echo tick >> restart.log; sleep 30", "pid": 57945, "restart": 1}
agentwatch stall_detected {"idle_seconds": 1.1, "pid": 57945, "policy": "restart"}
agentwatch action_taken {"action": "kill", "pid": 57945, "signal": "SIGTERM"}
agentwatch action_taken {"action": "restart", "attempt": 2, "backoff_seconds": 2.0, "max_restarts": 2, "reason": "stall"}
agentwatch started {"cmd": "echo tick >> restart.log; sleep 30", "pid": 57994, "restart": 2}
agentwatch stall_detected {"idle_seconds": 1.1, "pid": 57994, "policy": "restart"}
agentwatch action_taken {"action": "kill", "pid": 57994, "signal": "SIGTERM"}
agentwatch gave_up {"max_restarts": 2, "pid": 57994, "reason": "stall", "restarts": 2}
$ echo $?
2
$ agentwatch tail restart.jsonl | tail -2
--
started=3  stall_detected=3  action_taken=5  gave_up=1
```

The first two lines of the same run's `restart.jsonl`, which is what `tail` read:

```
{"detail": {"cmd": "echo tick >> restart.log; sleep 30", "log": "restart.log", "max_restarts": 2, "pid": 57873, "policy": "restart", "restart": 0, "stall_after": 1.0}, "kind": "started", "ts": "2026-09-10T16:08:07+00:00"}
{"detail": {"idle_seconds": 1.1, "pid": 57873, "policy": "restart"}, "kind": "stall_detected", "ts": "2026-09-10T16:08:08+00:00"}
```

### Options for `watch`

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pid` | none | PID to watch. Omit it to spawn `--cmd` |
| `--log` | none | File whose mtime shows progress. Without it, stalls are never detected |
| `--stall-after` | 300 | Seconds of unchanged mtime before a stall |
| `--policy` | warn | `warn`, `kill`, or `restart` |
| `--cmd` | none | Shell command to run. Required for `restart` |
| `--max-restarts` | 3 | Restarts before giving up. 0 or more |
| `--events` | events.jsonl | Where events are appended |
| `--interval` | 1 | Seconds between checks |
| `--heartbeat` | 60 | Seconds between heartbeat events |
| `--backoff-base` | 1 | First restart delay in seconds |
| `--backoff-cap` | 60 | Longest restart delay in seconds |
| `--grace` | 5 | Seconds between SIGTERM and SIGKILL |

Every number must be finite and greater than 0 (`--max-restarts` may be 0).
argparse rejects anything else, including `nan` and `inf`, with exit code 2.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The process ended on its own with status 0, or an external PID went away |
| 1 | agentwatch killed it, or a spawned child exited nonzero under `warn` or `kill` |
| 2 | Gave up after `--max-restarts`. Also argparse's code for an unknown flag or a bad number |
| 3 | A PID that is not running or that `kill` or `restart` cannot signal, `restart` without `--cmd`, or an events or log path that cannot be used. One line on stderr, no traceback |
| 130 | Ctrl-C |

## Event log

One JSON object per line. Fields are always `ts` (UTC, ISO 8601, second
resolution), `kind`, and `detail` (an object whose keys depend on the kind).
The same event is also printed to stderr as one line: `agentwatch <kind> <detail>`.

| Kind | When | Detail keys |
| --- | --- | --- |
| `started` | agentwatch starts watching, and after each restart | `pid`, `cmd`, `restart`; the first one also has `log`, `stall_after`, `policy`, `max_restarts` |
| `heartbeat` | every `--heartbeat` seconds while alive | `pid`, `idle_seconds`, `restarts` |
| `stall_detected` | log quiet for `--stall-after` seconds, once per stall | `pid`, `idle_seconds`, `policy` |
| `action_taken` | a kill or a restart | `action=kill`: `pid`, `signal`. `action=restart`: `attempt`, `max_restarts`, `backoff_seconds`, `reason` |
| `exited` | the process is gone, or was killed, or agentwatch got Ctrl-C | `pid`, `returncode` (null for external PIDs and on Ctrl-C), `reason` on Ctrl-C only |
| `gave_up` | restart budget spent | `pid`, `restarts`, `max_restarts`, `reason` |

`signal` in a kill action is `SIGTERM` if the process left during the grace
period, `SIGKILL` if it had to be forced, or `already_gone` if it was gone
before SIGTERM was sent.

## Architecture

Everything is in `agentwatch.py`. There is no config file, no pidfile, and no
state on disk other than the events file.

**Components.**

- Four helpers: `backoff` (delay for restart n), `pid_alive` (`kill(pid, 0)`),
  `log_mtime` (`stat` or None), and `kill_pid` (SIGTERM, grace, SIGKILL).
  They take no state from the supervisor and are tested on their own.
- `Supervisor`: holds the state and runs the loop. `run` spawns `--cmd` if no
  `--pid` was given and then calls `step` until it returns False. `step` is
  one tick and never sleeps on its own except during a restart's backoff.
- `tail`: reads an events file and prints it.
- `build_parser` and `main`: argparse and validation. `main` refuses a PID
  that does not exist, and, for `kill` and `restart`, a PID it cannot signal.

**Control flow of one tick** (`Supervisor.step`), in order:

1. Is the process alive? A spawned child is checked with `Popen.poll`; an
   external PID with `kill(pid, 0)`. If it is gone: write `exited`. Under
   `restart`, a spawned child that exited nonzero is restarted. Anything else
   stops the loop.
2. Read the log's mtime. If it differs from the last value seen (including
   None for a missing file), record now as the last change and clear the
   stall flag.
3. If a log is being watched, the log has been quiet for `--stall-after`, and
   this stall has not been reported yet: write `stall_detected` and apply the
   policy. `warn` does nothing more. `kill` kills, writes `exited`, stops.
   `restart` kills and calls `restart`, which either backs off and spawns or
   writes `gave_up` and stops.
4. If `--heartbeat` seconds have passed since the last heartbeat, write one.

**Where state lives.** All on the `Supervisor` instance; the class docstring
lists each field. `clock`, `sleep`, `alive`, and `out` are constructor
arguments so the tests can drive `step` with a fake clock and no waiting.

## Design decisions

Each entry is a choice the code makes, why, and what that costs.
[docs/design.md](docs/design.md) has the state machine and the edge cases.

**Liveness is PID plus log mtime.** A working agent writes output. An agent
stuck on a hung network call, a deadlock, or a prompt waiting for input stays
alive and stops writing. The PID alone cannot tell those apart; the mtime can,
for the price of one `stat` per tick and no parsing. Cost: a healthy process
that is quiet for longer than `--stall-after` gets treated as stalled, and a
stuck process that keeps writing (a retry loop that logs each attempt) is
never caught.

**Change detection, not age.** `step` compares the mtime with the last value
it saw. It never compares the mtime with the clock, so the file's timestamp
and the supervisor's clock never need to agree. Cost: any change counts as
progress, including
truncation, rotation, and deletion, and a log that was already stale when
agentwatch started still gets a full `--stall-after` before the first report.

**The stall timer reads the wall clock.** `Supervisor` takes a `clock`
argument that defaults to `time.time`, and the tests inject a fake through the
same argument. Cost: a step in system time (NTP correction, manual change)
shortens or lengthens the current stall window by the size of the step.
`kill_pid` measures its grace period with `time.monotonic`, which does not
have this problem.

**Polling, not file or process notifications.** One `stat` and one signal 0
(or `Popen.poll`) per tick, from the standard library. `pyproject.toml`
lists no dependencies, the same code runs on macOS and Linux, and the tests
drive the loop with a fake clock. Cost: detection latency up to `--interval`,
and for an external PID a
window of one interval in which the OS could reuse the PID.

**Spawned children get their own session and die as a group.** `spawn` uses
`start_new_session=True`, and `kill` uses `killpg` for spawned children. Why:
`--cmd` runs through `sh -c`, and killing only the shell would orphan the
agent it started. Cost: the child no longer belongs to the terminal's
foreground group, so Ctrl-C on agentwatch does not reach it. An external
`--pid` is signalled alone, because agentwatch did not create its group and
must not kill what else is in it.

**Spawned children are checked with `Popen.poll`, external PIDs with
`kill(pid, 0)`.** `poll` reaps the child and cannot be fooled by PID reuse.
`kill(pid, 0)` is all the kernel offers for someone else's process. Cost: for
an external PID, a zombie counts as alive, and a reused PID counts as the
original.

**The restart count never resets.** A process that stalls once an hour for a
day still gives up after `--max-restarts`. Why: a supervisor that restarts
forever hides a real problem. Cost: a long-lived job with rare, harmless
stalls needs a larger budget, or `warn`.

**Kill before the budget check.** When a stall hits under `restart`, the
process is killed first and only then does `restart` check whether a restart
is allowed. Why: giving up must not leave a stalled process running with
nobody watching. Cost: the last stalled process is never left for inspection.

**Only a nonzero exit restarts, and only for spawned children.** A spawned
child that exits 0 ends the run with exit code 0. An external PID that
disappears ends the run with exit code 0, whatever its status, because
agentwatch cannot read the status of a process it did not spawn.

**Append-only JSONL, plus one line on stderr.** Each event is written with
`sort_keys=True` on its own line and the file is opened for append every
time. Why: `tail`, `grep`, and `jq` all work on it, a crash loses at most the
line being written, and rerunning appends to the same history. Cost: no
rotation, and `agentwatch tail` stops with the line number when it meets a
line that is still being written.

**A PID that cannot be signalled is refused up front.** `main` sends signal 0
to `--pid` before starting. A process that exists but returns EPERM is
accepted for `warn` and rejected for `kill` and `restart`. Why: without this,
`kill_pid` used to report the process as `already_gone` and the run wrote an
`exited` event for a process that was still running. Cost: none for normal
use; if permissions change during a run, `kill` raises `PermissionError`
rather than lying.

## Setting `--stall-after`

- Set it above the longest quiet period the agent has when healthy: a long
  model call, a big download, a slow test suite.
- Output buffering hides progress. If the agent writes to a pipe or redirects
  stdout to a file, Python and many other runtimes buffer it. Use `python -u`,
  `PYTHONUNBUFFERED=1`, `stdbuf -oL`, or have the agent flush.
- With the default 1 s interval, a stall is reported between `--stall-after`
  and `--stall-after` plus one interval after the last write.

## Tests

```
. .venv/bin/activate
pytest
```

Covers backoff math, stall detection and re-arming with a fake clock and a
fake log, heartbeat timing, kill of a real child, restart give-up with real
child processes, restart on nonzero exit, no restart on clean exit, CLI
argument errors, refusal of a PID that cannot be signalled, Ctrl-C exiting
130 with the child left running, one-line errors for unusable paths and
half-written events files, and `tail` output. CI runs the same suite plus
`ruff check` and `ruff format --check` on Linux and macOS.
