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
    """Return True if a process with this PID exists.

    Uses ``kill(pid, 0)``, which sends no signal. A zombie still counts as
    alive, and so does a process owned by another user (EPERM means it exists).

    Args:
        pid: Process ID to check.

    Returns:
        True if the PID exists, False if the kernel reports no such process.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_mtime(path: str | None) -> float | None:
    """Return the modification time of ``path``, or None.

    ``os.stat`` follows symlinks, so a symlinked log reports its target's mtime.

    Args:
        path: File path, or None when no log is being watched.

    Returns:
        ``st_mtime`` as a float, or None if ``path`` is None or the file does
        not exist. Other OS errors (for example EACCES) propagate.
    """
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return None


def kill_pid(pid: int, grace: float, alive=pid_alive, sleep=time.sleep, group: bool = False) -> str:
    """Send SIGTERM, wait up to ``grace`` seconds, then send SIGKILL.

    Args:
        pid: Process ID, or process group ID when ``group`` is True.
        grace: Seconds to wait after SIGTERM before sending SIGKILL. The wait
            is measured with ``time.monotonic``.
        alive: Callable ``(pid) -> bool`` polled every 50 ms during the grace
            period. Injectable for tests.
        sleep: Sleep function used between polls. Injectable for tests.
        group: If True, signal the whole process group with ``killpg``. Only
            safe for children agentwatch spawned, which get their own session,
            so a ``sh -c "a; b"`` wrapper does not leave orphans.

    Returns:
        ``"already_gone"`` if the process did not exist when SIGTERM was sent,
        ``"SIGTERM"`` if it went away during the grace period (or was gone by
        the time SIGKILL was sent), ``"SIGKILL"`` otherwise.

    Raises:
        PermissionError: ``group`` is False and the caller may not send
            SIGTERM to the process. With ``group`` True, EPERM is swallowed and
            reported as ``"already_gone"``, because macOS ``killpg`` returns
            EPERM for a group whose members have all exited. EPERM on the later
            SIGKILL is never raised: SIGTERM already succeeded on that PID, so
            the original process is gone.
    """
    send = os.killpg if group else os.kill
    try:
        send(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except PermissionError:
        if group:
            return "already_gone"
        raise
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not alive(pid):
            return "SIGTERM"
        sleep(0.05)
    try:
        send(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # gone after SIGTERM; EPERM means the PID was reused
        return "SIGTERM"
    return "SIGKILL"


class Supervisor:
    """Watch one process and one log file, and act on stalls.

    All state lives on the instance: the watched PID (``pid``), the spawned
    child if any (``child``), the last observed log mtime (``last_mtime``),
    when it last changed (``last_change``), whether the current stall has
    already been reported (``stalled``), the restart count (``restarts``),
    and the exit code to return from ``run`` (``exit_code``). Nothing is
    persisted except the events file.

    ``clock``, ``sleep``, ``alive`` and ``out`` are injectable so tests can
    drive ``step`` with a fake clock and no real waiting.
    """

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
        """Validate arguments and record the starting state.

        Args:
            pid: External PID to watch, or None to spawn ``cmd`` in ``run``.
            log: Path whose mtime shows progress, or None to never detect stalls.
            stall_after: Seconds of unchanged mtime before a stall is reported.
            policy: ``"warn"``, ``"kill"`` or ``"restart"``.
            cmd: Shell command to (re)start. Required for ``"restart"`` and
                when ``pid`` is None.
            max_restarts: Restarts allowed before ``gave_up``.
            events: Path of the JSONL events file. Appended to, never truncated.
            interval: Seconds ``run`` sleeps between ``step`` calls.
            heartbeat: Seconds between ``heartbeat`` events.
            backoff_base: First restart delay in seconds.
            backoff_cap: Longest restart delay in seconds.
            grace: Seconds between SIGTERM and SIGKILL.
            clock: Returns the current time in seconds. Defaults to ``time.time``.
            sleep: Sleep function. Defaults to ``time.sleep``.
            alive: ``(pid) -> bool`` used for external PIDs. Defaults to ``pid_alive``.
            out: Stream for the one-line human copy of each event.

        Raises:
            ValueError: unknown policy, ``restart`` without ``cmd``, or neither
                ``pid`` nor ``cmd`` given.
        """
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
        """Append one event to the events file and echo one line to ``out``.

        Args:
            kind: One of ``KINDS``.
            **detail: JSON-serialisable fields for the event's ``detail`` object.

        Raises:
            AssertionError: ``kind`` is not in ``KINDS``.
            OSError: the events file cannot be opened for append.
        """
        assert kind in KINDS, kind
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "detail": detail}
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"agentwatch {kind} {json.dumps(detail, sort_keys=True)}", file=self.out, flush=True)

    # -- process control ------------------------------------------------

    def spawn(self) -> None:
        """Start ``cmd`` through the shell in its own session and reset stall state.

        Sets ``child`` and ``pid``, clears ``stalled``, and restarts the stall
        timer from now with the log's current mtime as the baseline.
        """
        self.child = subprocess.Popen(self.cmd, shell=True, start_new_session=True)
        self.pid = self.child.pid
        self.stalled = False
        now = self.clock()
        self.last_change = now
        self.last_mtime = log_mtime(self.log)

    def is_alive(self) -> bool:
        """Return True if the watched process is still running.

        A spawned child is checked with ``Popen.poll``, which also reaps it and
        is immune to PID reuse. An external PID goes through ``alive``.
        """
        if self.child is not None:
            return self.child.poll() is None
        return self.alive(self.pid)

    def kill(self) -> str:
        """Terminate the watched process and, for a spawned child, reap it.

        Returns:
            The string ``kill_pid`` returned: ``"already_gone"``, ``"SIGTERM"``
            or ``"SIGKILL"``.

        Raises:
            PermissionError: the external PID cannot be signalled. ``main``
                rejects such PIDs before ``run`` starts.
        """
        how = kill_pid(self.pid, self.grace, alive=lambda _pid: self.is_alive(), sleep=self.sleep,
                       group=self.child is not None)
        if self.child is not None:
            self.child.wait()  # reap; SIGKILL has been sent if SIGTERM did not work
        return how

    # -- one tick -------------------------------------------------------

    def step(self) -> bool:
        """Run one check.

        In order: if the process is gone, emit ``exited`` and either restart
        (policy ``restart``, spawned child, nonzero exit) or stop. Otherwise
        compare the log mtime with the last one seen; any difference counts
        as progress and re-arms stall reporting. If the log has been quiet
        for ``stall_after`` seconds and this stall has not been reported yet,
        emit ``stall_detected`` and apply the policy. Finally emit a
        ``heartbeat`` if one is due.

        Returns:
            False when the supervisor should stop (``exit_code`` is then set),
            True to keep watching.
        """
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
        """Spend one restart from the budget, back off, and spawn ``cmd`` again.

        The caller has already killed the old process when ``reason`` is
        ``"stall"``; for ``"exited_nonzero"`` it exited on its own.

        Args:
            reason: ``"stall"`` or ``"exited_nonzero"``, recorded in the event.

        Returns:
            True if a new process was started. False if the budget is spent;
            a ``gave_up`` event is written and ``exit_code`` is ``EXIT_GAVE_UP``.
        """
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
        """Spawn ``cmd`` if no PID was given, then call ``step`` until it returns False.

        Returns:
            ``EXIT_OK``, ``EXIT_KILLED`` or ``EXIT_GAVE_UP`` as set by ``step``,
            or 130 on KeyboardInterrupt. On interrupt a spawned child is left
            running; only an ``exited`` event with reason
            ``agentwatch_interrupted`` is written.
        """
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
    """Print one line per event from a JSONL events file, then per-kind counts.

    Args:
        path: Events file written by ``Supervisor.emit``.
        out: Output stream. Defaults to ``sys.stdout``.

    Returns:
        0.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        json.JSONDecodeError: a non-blank line is not valid JSON, for example
            a line agentwatch is still writing.
        KeyError: a record has no ``kind``.
    """
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
    """Build the ``agentwatch`` argument parser with the ``watch`` and ``tail`` subcommands."""
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
    """CLI entry point.

    Args:
        argv: Arguments without the program name. None means ``sys.argv[1:]``.

    Returns:
        Process exit code: ``tail`` returns 0; ``watch`` returns what
        ``Supervisor.run`` returns, or ``EXIT_USAGE`` for bad arguments, a
        PID that is not running, or a PID that ``kill`` or ``restart`` could
        not signal.

    Raises:
        SystemExit: argparse rejected the arguments.
    """
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
    if args.pid is not None:
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            print(f"agentwatch: pid {args.pid} is not running", file=sys.stderr)
            return EXIT_USAGE
        except PermissionError:
            if args.policy != "warn":
                print(f"agentwatch: pid {args.pid} is running but you cannot signal it; use --policy warn",
                      file=sys.stderr)
                return EXIT_USAGE
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
