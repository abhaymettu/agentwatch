"""agentwatch: supervise one long-running CLI agent process on one machine.

Watches a PID and/or a log file. A stall is "the process is alive but the log
mtime has not changed for --stall-after seconds". On a stall it applies one
policy: warn, kill, or restart with exponential backoff. Every event goes to a
JSONL file, one object per line: {"ts": ..., "kind": ..., "detail": {...}}.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

KINDS = ("started", "heartbeat", "stall_detected", "action_taken", "exited", "gave_up")

EXIT_OK = 0
EXIT_KILLED = 1  # we killed it, or the child exited nonzero and we did not restart
EXIT_GAVE_UP = 2
EXIT_USAGE = 3


def backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Delay before restart number `attempt` (0-based): base * 2**attempt, capped."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return min(base * (2**attempt), cap)


def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. A zombie still counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_mtime(path: str | None) -> float | None:
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return None


def kill_pid(pid: int, grace: float, alive=pid_alive, sleep=time.sleep, group: bool = False) -> str:
    """SIGTERM, wait up to `grace` seconds, then SIGKILL. Returns which signal ended it.

    group=True signals the whole process group (only for children we spawned, which
    get their own session), so a `sh -c "a; b"` wrapper does not leave orphans.
    """
    send = os.killpg if group else os.kill
    try:
        send(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # macOS killpg gives EPERM on a dead group
        return "already_gone"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not alive(pid):
            return "SIGTERM"
        sleep(0.05)
    try:
        send(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "SIGTERM"
    return "SIGKILL"


class Supervisor:
    def __init__(
        self,
        *,
        pid: int | None,
        log: str | None,
        stall_after: float,
        policy: str,
        cmd: str | None,
        max_restarts: int,
        events: str,
        interval: float = 1.0,
        heartbeat: float = 60.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        grace: float = 5.0,
        clock=time.time,
        sleep=time.sleep,
        alive=pid_alive,
        out=sys.stderr,
    ):
        if policy not in ("warn", "kill", "restart"):
            raise ValueError(f"unknown policy {policy!r}")
        if policy == "restart" and not cmd:
            raise ValueError("--policy restart needs --cmd")
        if pid is None and not cmd:
            raise ValueError("need --pid or --cmd")
        self.pid = pid
        self.log = log
        self.stall_after = stall_after
        self.policy = policy
        self.cmd = cmd
        self.max_restarts = max_restarts
        self.events = events
        self.interval = interval
        self.heartbeat = heartbeat
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.grace = grace
        self.clock = clock
        self.sleep = sleep
        self.alive = alive
        self.out = out

        self.child: subprocess.Popen | None = None
        self.restarts = 0
        self.stalled = False
        self.last_mtime = log_mtime(log)
        now = clock()
        self.last_change = now
        self.last_heartbeat = now
        self.exit_code: int | None = None

    # -- events ---------------------------------------------------------

    def emit(self, kind: str, **detail) -> None:
        assert kind in KINDS, kind
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "detail": detail}
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"agentwatch {kind} {json.dumps(detail, sort_keys=True)}", file=self.out, flush=True)

    # -- process control ------------------------------------------------

    def spawn(self) -> None:
        self.child = subprocess.Popen(self.cmd, shell=True, start_new_session=True)
        self.pid = self.child.pid
        self.stalled = False
        now = self.clock()
        self.last_change = now
        self.last_mtime = log_mtime(self.log)

    def is_alive(self) -> bool:
        if self.child is not None:
            return self.child.poll() is None
        return self.alive(self.pid)

    def kill(self) -> str:
        how = kill_pid(self.pid, self.grace, alive=lambda _pid: self.is_alive(), sleep=self.sleep,
                       group=self.child is not None)
        if self.child is not None:
            self.child.wait()  # reap; SIGKILL has been sent if SIGTERM did not work
        return how

    # -- one tick -------------------------------------------------------

    def step(self) -> bool:
        """Run one check. Returns False when the supervisor should stop."""
        now = self.clock()

        if not self.is_alive():
            rc = self.child.returncode if self.child is not None else None
            self.emit("exited", pid=self.pid, returncode=rc)
            if self.policy == "restart" and self.child is not None and rc != 0:
                return self.restart(reason="exited_nonzero")
            self.exit_code = EXIT_OK if rc in (0, None) else EXIT_KILLED
            return False

        mtime = log_mtime(self.log)
        if mtime != self.last_mtime:
            self.last_mtime = mtime
            self.last_change = now
            self.stalled = False

        idle = now - self.last_change
        if self.log is not None and idle >= self.stall_after and not self.stalled:
            self.stalled = True
            self.emit("stall_detected", pid=self.pid, idle_seconds=round(idle, 1), policy=self.policy)
            if self.policy == "kill":
                how = self.kill()
                self.emit("action_taken", action="kill", pid=self.pid, signal=how)
                self.emit("exited", pid=self.pid, returncode=self.child.returncode if self.child else None)
                self.exit_code = EXIT_KILLED
                return False
            if self.policy == "restart":
                how = self.kill()
                self.emit("action_taken", action="kill", pid=self.pid, signal=how)
                return self.restart(reason="stall")
            # warn: nothing else; re-arms when the log changes again

        if now - self.last_heartbeat >= self.heartbeat:
            self.last_heartbeat = now
            self.emit("heartbeat", pid=self.pid, idle_seconds=round(idle, 1), restarts=self.restarts)
        return True

    def restart(self, *, reason: str) -> bool:
        if self.restarts >= self.max_restarts:
            self.emit("gave_up", pid=self.pid, restarts=self.restarts, max_restarts=self.max_restarts, reason=reason)
            self.exit_code = EXIT_GAVE_UP
            return False
        delay = backoff(self.restarts, self.backoff_base, self.backoff_cap)
        self.restarts += 1
        self.emit("action_taken", action="restart", attempt=self.restarts, max_restarts=self.max_restarts,
                  backoff_seconds=delay, reason=reason)
        self.sleep(delay)
        self.spawn()
        self.emit("started", pid=self.pid, cmd=self.cmd, restart=self.restarts)
        return True

    # -- main loop ------------------------------------------------------

    def run(self) -> int:
        if self.pid is None:
            self.spawn()
        self.emit("started", pid=self.pid, log=self.log, stall_after=self.stall_after, policy=self.policy,
                  cmd=self.cmd, max_restarts=self.max_restarts, restart=0)
        try:
            while self.step():
                self.sleep(self.interval)
        except KeyboardInterrupt:
            self.emit("exited", pid=self.pid, returncode=None, reason="agentwatch_interrupted")
            return 130
        return self.exit_code if self.exit_code is not None else EXIT_OK


# -- tail -------------------------------------------------------------------


def tail(path: str, out=None) -> int:
    out = out or sys.stdout
    counts: dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            d = rec.get("detail", {})
            summary = " ".join(f"{k}={v}" for k, v in sorted(d.items()) if v is not None)
            print(f"{rec['ts']}  {kind:<15} {summary}", file=out)
    print("--", file=out)
    print("  ".join(f"{k}={counts[k]}" for k in KINDS if k in counts), file=out)
    return 0


# -- cli --------------------------------------------------------------------

USAGE = ('agentwatch watch --pid 1234 --log run.log --stall-after 300 '
         '--policy warn|kill|restart [--cmd "..."] [--max-restarts 3] [--events events.jsonl]')


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentwatch", description=__doc__.strip().splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    w = sub.add_parser("watch", help="supervise a process")
    w.add_argument("--pid", type=int, help="PID to watch; omit to spawn --cmd yourself")
    w.add_argument("--log", help="output log file whose mtime shows progress")
    w.add_argument("--stall-after", type=float, default=300, help="seconds without log change before a stall (default 300)")
    w.add_argument("--policy", choices=["warn", "kill", "restart"], default="warn")
    w.add_argument("--cmd", help="shell command to (re)start; required for --policy restart")
    w.add_argument("--max-restarts", type=int, default=3)
    w.add_argument("--events", default="events.jsonl", help="JSONL event log path (default events.jsonl)")
    w.add_argument("--interval", type=float, default=1.0, help="seconds between checks (default 1)")
    w.add_argument("--heartbeat", type=float, default=60.0, help="seconds between heartbeat events (default 60)")
    w.add_argument("--backoff-base", type=float, default=1.0, help="first restart delay in seconds (default 1)")
    w.add_argument("--backoff-cap", type=float, default=60.0, help="max restart delay in seconds (default 60)")
    w.add_argument("--grace", type=float, default=5.0, help="seconds between SIGTERM and SIGKILL (default 5)")

    t = sub.add_parser("tail", help="print a human summary of an events file")
    t.add_argument("events")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "tail":
        return tail(args.events)
    try:
        sup = Supervisor(
            pid=args.pid, log=args.log, stall_after=args.stall_after, policy=args.policy, cmd=args.cmd,
            max_restarts=args.max_restarts, events=args.events, interval=args.interval, heartbeat=args.heartbeat,
            backoff_base=args.backoff_base, backoff_cap=args.backoff_cap, grace=args.grace,
        )
    except ValueError as e:
        print(f"agentwatch: {e}\nusage: {USAGE}", file=sys.stderr)
        return EXIT_USAGE
    if args.pid is not None and not pid_alive(args.pid):
        print(f"agentwatch: pid {args.pid} is not running", file=sys.stderr)
        return EXIT_USAGE
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
