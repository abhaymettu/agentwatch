# agentwatch

A small supervisor for one long-running CLI agent process on one machine.

It watches a PID and a log file. If the process is alive but the log file's
modification time has not changed for N seconds, that is a stall. On a stall it
does one of three things: warn, kill, or kill and restart with exponential
backoff. Every event is appended to a JSONL file so you can read what happened
the next morning.

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
- No Windows. It uses POSIX signals.

## Install

Python 3.11 or newer. No dependencies outside the standard library. pytest is
only used for the tests.

```
git clone <this repo> agentwatch
cd agentwatch
./setup.sh
. .venv/bin/activate
```

`setup.sh` creates `.venv`, installs the package in editable mode, runs the
tests, and prints the usage line. Running it again is safe.

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

Options for `watch`:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pid` | none | PID to watch. Omit it to spawn `--cmd` |
| `--log` | none | File whose mtime shows progress. Without it, stalls are never detected |
| `--stall-after` | 300 | Seconds of unchanged mtime before a stall |
| `--policy` | warn | `warn`, `kill`, or `restart` |
| `--cmd` | none | Shell command to run. Required for `restart` |
| `--max-restarts` | 3 | Restarts before giving up |
| `--events` | events.jsonl | Where events are appended |
| `--interval` | 1 | Seconds between checks |
| `--heartbeat` | 60 | Seconds between heartbeat events |
| `--backoff-base` | 1 | First restart delay in seconds |
| `--backoff-cap` | 60 | Longest restart delay in seconds |
| `--grace` | 5 | Seconds between SIGTERM and SIGKILL |

Exit codes: 0 the process ended on its own with status 0 (or an external PID
went away), 1 agentwatch killed it or it exited nonzero under `warn` or
`kill`, 2 gave up after max restarts, 3 bad arguments, 130 Ctrl-C.

## Event log

One JSON object per line. Fields are always `ts` (UTC, ISO 8601), `kind`, and
`detail` (an object whose keys depend on the kind).

Kinds: `started`, `heartbeat`, `stall_detected`, `action_taken`, `exited`,
`gave_up`.

This is the output of a real run on this machine, with a command that writes
once and then goes quiet, `--stall-after 1`, `--max-restarts 2`:

```
{"detail": {"cmd": "echo tick >> /tmp/aw-demo.log; sleep 30", "log": "/tmp/aw-demo.log", "max_restarts": 2, "pid": 39420, "policy": "restart", "restart": 0, "stall_after": 1.0}, "kind": "started", "ts": "2026-09-10T15:44:04+00:00"}
{"detail": {"idle_seconds": 1.1, "pid": 39420, "policy": "restart"}, "kind": "stall_detected", "ts": "2026-09-10T15:44:05+00:00"}
{"detail": {"action": "kill", "pid": 39420, "signal": "SIGTERM"}, "kind": "action_taken", "ts": "2026-09-10T15:44:06+00:00"}
{"detail": {"action": "restart", "attempt": 1, "backoff_seconds": 1.0, "max_restarts": 2, "reason": "stall"}, "kind": "action_taken", "ts": "2026-09-10T15:44:06+00:00"}
{"detail": {"cmd": "echo tick >> /tmp/aw-demo.log; sleep 30", "pid": 39447, "restart": 1}, "kind": "started", "ts": "2026-09-10T15:44:07+00:00"}
{"detail": {"idle_seconds": 1.0, "pid": 39447, "policy": "restart"}, "kind": "stall_detected", "ts": "2026-09-10T15:44:08+00:00"}
{"detail": {"action": "kill", "pid": 39447, "signal": "SIGTERM"}, "kind": "action_taken", "ts": "2026-09-10T15:44:08+00:00"}
{"detail": {"action": "restart", "attempt": 2, "backoff_seconds": 2.0, "max_restarts": 2, "reason": "stall"}, "kind": "action_taken", "ts": "2026-09-10T15:44:08+00:00"}
{"detail": {"cmd": "echo tick >> /tmp/aw-demo.log; sleep 30", "pid": 39525, "restart": 2}, "kind": "started", "ts": "2026-09-10T15:44:10+00:00"}
{"detail": {"idle_seconds": 1.3, "pid": 39525, "policy": "restart"}, "kind": "stall_detected", "ts": "2026-09-10T15:44:12+00:00"}
{"detail": {"action": "kill", "pid": 39525, "signal": "SIGTERM"}, "kind": "action_taken", "ts": "2026-09-10T15:44:12+00:00"}
{"detail": {"max_restarts": 2, "pid": 39525, "reason": "stall", "restarts": 2}, "kind": "gave_up", "ts": "2026-09-10T15:44:12+00:00"}
```

`agentwatch tail` on that file ends with:

```
started=3  stall_detected=3  action_taken=5  gave_up=1
```

## Design notes

**Why mtime plus PID.** An agent that is working writes output. An agent that
is stuck on a hung network call, a deadlock, or a prompt waiting for input
usually stops writing but stays alive. The PID alone cannot tell those apart.
The log mtime can, without parsing anything, and it costs one `stat` per
check. Combining the two gives three states: alive and writing (fine), alive
and quiet (stall), gone (exited).

**Limits of this liveness signal.**

- A process can be alive and quiet for good reasons: a long model call, a
  big file download, a slow test suite. Set `--stall-after` above the longest
  legitimate quiet period, or you will kill healthy runs.
- A process can be stuck and still write. A retry loop that logs each attempt
  looks like progress. mtime does not catch that.
- Output buffering hides progress. If the agent writes to a pipe or redirects
  stdout to a file, Python and many other runtimes buffer it. Use `python -u`,
  `PYTHONUNBUFFERED=1`, `stdbuf -oL`, or have the agent flush.
- Log rotation or truncation changes mtime and counts as progress.
- mtime resolution and clock changes: the stall clock uses the supervisor's
  wall clock, and mtime is compared for change, not for its value. NTP jumps
  can shorten or lengthen a stall window by the size of the jump.
- PID reuse: if the watched process dies and the OS hands its PID to a new
  process before the next check, agentwatch will think it is still alive. The
  default 1 second interval makes this unlikely but not impossible. Processes
  agentwatch spawned itself are checked through `Popen.poll`, which does not
  have this problem.
- An external PID that turns into a zombie still counts as alive, because
  `kill(pid, 0)` succeeds on zombies.
- The stall clock starts when agentwatch starts, not from the log's existing
  mtime. A log that was already stale gets a full `--stall-after` before the
  first report.

**Backoff.** Delay before restart n (0-based) is `base * 2^n`, capped. With the
defaults that is 1, 2, 4, 8, 16, 32, 60, 60 seconds. The restart count never
resets, so a process that stalls every hour for a day still gives up after
`--max-restarts`. That is deliberate: a supervisor that quietly restarts
forever hides a real problem.

**Giving up.** When the restart budget is spent, agentwatch writes a `gave_up`
event and exits with code 2. It does not leave the last process running: the
kill happens before the budget check.

## Tests

```
. .venv/bin/activate
pytest
```

Covers backoff math, stall detection and re-arming with a fake clock and a
fake log, heartbeat timing, kill of a real child, restart give-up with real
child processes, restart on nonzero exit, no restart on clean exit, CLI
argument errors, and `tail` output.
