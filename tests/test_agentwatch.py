import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agentwatch
from agentwatch import EXIT_GAVE_UP, EXIT_KILLED, EXIT_OK, EXIT_USAGE, Supervisor, backoff


def read_events(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def kinds(path):
    return [e["kind"] for e in read_events(path)]


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


# -- backoff ----------------------------------------------------------------


def test_backoff_doubles_and_caps():
    assert [backoff(i, base=1, cap=60) for i in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff(0, base=0.5, cap=10) == 0.5
    assert backoff(3, base=2, cap=5) == 5


def test_backoff_rejects_negative():
    with pytest.raises(ValueError):
        backoff(-1)


# -- stall detection with a fake log ----------------------------------------


def make_sup(tmp_path, clock, policy="warn", **kw):
    log = tmp_path / "run.log"
    log.write_text("start\n")
    os.utime(log, (clock.t, clock.t))
    events = tmp_path / "events.jsonl"
    sup = Supervisor(
        pid=424242,
        log=str(log),
        stall_after=10,
        policy=policy,
        cmd=kw.pop("cmd", None),
        max_restarts=kw.pop("max_restarts", 3),
        events=str(events),
        heartbeat=5,
        clock=clock,
        sleep=lambda s: None,
        alive=kw.pop("alive", lambda pid: True),
        out=io.StringIO(),
        **kw,
    )
    return sup, log, events


def test_stall_detected_when_log_mtime_frozen(tmp_path):
    clock = FakeClock()
    sup, _, events = make_sup(tmp_path, clock)
    assert sup.step() is True
    clock.advance(9)
    assert sup.step() is True
    assert "stall_detected" not in kinds(events)
    clock.advance(1.5)
    assert sup.step() is True  # warn policy keeps watching
    ev = read_events(events)
    stalls = [e for e in ev if e["kind"] == "stall_detected"]
    assert len(stalls) == 1
    assert stalls[0]["detail"]["idle_seconds"] >= 10
    assert stalls[0]["detail"]["policy"] == "warn"
    # warn fires once per stall, not every tick
    clock.advance(30)
    sup.step()
    assert kinds(events).count("stall_detected") == 1


def test_log_write_resets_stall_timer_and_rearms(tmp_path):
    clock = FakeClock()
    sup, log, events = make_sup(tmp_path, clock)
    clock.advance(8)
    log.write_text("progress\n")
    os.utime(log, (clock.t, clock.t))
    sup.step()
    clock.advance(8)
    sup.step()
    assert "stall_detected" not in kinds(events)  # only 8s since last write
    clock.advance(3)
    sup.step()
    assert kinds(events).count("stall_detected") == 1
    # new write re-arms: a second stall later is reported again
    clock.advance(1)
    os.utime(log, (clock.t, clock.t))
    sup.step()
    clock.advance(11)
    sup.step()
    assert kinds(events).count("stall_detected") == 2


def test_heartbeat_emitted_on_interval(tmp_path):
    clock = FakeClock()
    sup, _, events = make_sup(tmp_path, clock)
    sup.step()
    clock.advance(5)
    sup.step()
    clock.advance(5)
    sup.step()
    assert kinds(events).count("heartbeat") == 2


def test_process_exit_ends_watch(tmp_path):
    clock = FakeClock()
    sup, _, events = make_sup(tmp_path, clock, alive=lambda pid: False)
    assert sup.step() is False
    assert kinds(events) == ["exited"]
    assert sup.exit_code == EXIT_OK


def test_event_lines_have_ts_kind_detail(tmp_path):
    clock = FakeClock()
    sup, _, events = make_sup(tmp_path, clock)
    clock.advance(11)
    sup.step()
    for e in read_events(events):
        assert set(e) == {"ts", "kind", "detail"}
        assert e["kind"] in agentwatch.KINDS


# -- kill and restart with real child processes -----------------------------


def test_kill_policy_kills_real_process(tmp_path):
    proc = subprocess.Popen(["sleep", "60"])
    log = tmp_path / "run.log"
    log.write_text("x")
    events = tmp_path / "events.jsonl"
    clock = FakeClock(time.time())
    sup = Supervisor(
        pid=proc.pid,
        log=str(log),
        stall_after=1,
        policy="kill",
        cmd=None,
        max_restarts=0,
        events=str(events),
        clock=clock,
        sleep=lambda s: None,
        grace=2,
        out=io.StringIO(),
    )
    clock.advance(2)
    assert sup.step() is False
    proc.wait(timeout=5)
    assert proc.returncode != 0
    assert kinds(events) == ["stall_detected", "action_taken", "exited"]
    assert sup.exit_code == EXIT_KILLED


def test_restart_gives_up_after_max_restarts(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x")
    events = tmp_path / "events.jsonl"
    slept = []
    sup = Supervisor(
        pid=None,
        log=str(log),
        stall_after=0.05,
        policy="restart",
        cmd="sleep 60",
        max_restarts=2,
        events=str(events),
        interval=0.02,
        backoff_base=0.01,
        backoff_cap=0.02,
        grace=1,
        sleep=lambda s: slept.append(s) or time.sleep(min(s, 0.05)),
        out=io.StringIO(),
    )
    rc = sup.run()
    assert rc == EXIT_GAVE_UP
    ev = read_events(events)
    ks = [e["kind"] for e in ev]
    assert ks[0] == "started"
    assert ks[-1] == "gave_up"
    assert ks.count("stall_detected") == 3  # original + 2 restarts, each stalls
    restarts = [e["detail"] for e in ev if e["kind"] == "action_taken" and e["detail"]["action"] == "restart"]
    assert [r["attempt"] for r in restarts] == [1, 2]
    assert [r["backoff_seconds"] for r in restarts] == [0.01, 0.02]  # 1x then 2x, capped
    assert ev[-1]["detail"] == {"pid": sup.pid, "restarts": 2, "max_restarts": 2, "reason": "stall"}
    assert not agentwatch.pid_alive(sup.pid) or sup.child.poll() is not None
    assert 0.01 in slept and 0.02 in slept


def test_restart_on_nonzero_child_exit(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x")
    events = tmp_path / "events.jsonl"
    sup = Supervisor(
        pid=None,
        log=str(log),
        stall_after=100,
        policy="restart",
        cmd="exit 7",
        max_restarts=1,
        events=str(events),
        interval=0.02,
        backoff_base=0.01,
        backoff_cap=0.01,
        sleep=time.sleep,
        out=io.StringIO(),
    )
    rc = sup.run()
    assert rc == EXIT_GAVE_UP
    ks = kinds(events)
    assert ks == ["started", "exited", "action_taken", "started", "exited", "gave_up"]
    assert read_events(events)[-1]["detail"]["reason"] == "exited_nonzero"


def test_clean_child_exit_is_not_restarted(tmp_path):
    events = tmp_path / "events.jsonl"
    sup = Supervisor(
        pid=None,
        log=None,
        stall_after=100,
        policy="restart",
        cmd="true",
        max_restarts=3,
        events=str(events),
        interval=0.02,
        out=io.StringIO(),
    )
    assert sup.run() == EXIT_OK
    assert kinds(events) == ["started", "exited"]


# -- cli ----------------------------------------------------------------------


def test_cli_rejects_restart_without_cmd(capsys):
    assert agentwatch.main(["watch", "--pid", "1", "--policy", "restart"]) == agentwatch.EXIT_USAGE
    assert "needs --cmd" in capsys.readouterr().err


def test_cli_rejects_dead_pid(capsys):
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert agentwatch.main(["watch", "--pid", str(dead.pid), "--log", "x"]) == agentwatch.EXIT_USAGE


def test_tail_prints_summary(tmp_path, capsys):
    events = tmp_path / "events.jsonl"
    sup = Supervisor(
        pid=None,
        log=None,
        stall_after=100,
        policy="warn",
        cmd="true",
        max_restarts=0,
        events=str(events),
        interval=0.02,
        out=io.StringIO(),
    )
    sup.run()
    assert agentwatch.main(["tail", str(events)]) == 0
    out = capsys.readouterr().out
    assert "started" in out and "exited" in out
    assert out.strip().splitlines()[-1] == "started=1  exited=1"


def test_cli_rejects_nonpositive_numbers():
    cases = [("--interval", "-1"), ("--stall-after", "0"), ("--grace", "0"), ("--max-restarts", "-1")]
    cases += [("--interval", "nan"), ("--heartbeat", "inf"), ("--backoff-cap", "-inf")]
    for flag, value in cases:
        with pytest.raises(SystemExit) as e:
            agentwatch.main(["watch", "--cmd", "true", flag, value])
        assert e.value.code == 2


def test_cli_reports_unwritable_events_path_on_one_line(tmp_path, capsys):
    missing = tmp_path / "nodir" / "events.jsonl"
    assert agentwatch.main(["watch", "--cmd", "true", "--events", str(missing)]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "Traceback" not in err and "nodir" in err


def test_midrun_events_failure_names_the_running_pid(tmp_path, capsys, monkeypatch):
    events = tmp_path / "events.jsonl"

    def boom(self):
        raise PermissionError(f"{events}: read-only file system")

    monkeypatch.setattr(agentwatch.Supervisor, "step", boom)
    rc = agentwatch.main(["watch", "--cmd", "sleep 30", "--events", str(events)])
    child = read_events(events)[0]["detail"]["pid"]
    try:
        assert rc == EXIT_USAGE
        err = capsys.readouterr().err
        assert "Traceback" not in err and f"pid {child} is still running unsupervised" in err
    finally:
        os.killpg(child, signal.SIGKILL)


def test_tail_missing_file_is_one_line_error(tmp_path, capsys):
    assert agentwatch.main(["tail", str(tmp_path / "nope.jsonl")]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith("agentwatch: ") and "Traceback" not in err


def test_tail_names_the_half_written_line(tmp_path, capsys):
    events = tmp_path / "events.jsonl"
    events.write_text('{"ts": "t", "kind": "started", "detail": {}}\n{"ts": "t", "kind": "heartb')
    assert agentwatch.main(["tail", str(events)]) == EXIT_USAGE
    assert "line 2" in capsys.readouterr().err


def test_ctrl_c_exits_130_and_leaves_child_running(tmp_path):
    events = tmp_path / "events.jsonl"
    proc = subprocess.Popen(
        [
            sys.executable,
            agentwatch.__file__,
            "watch",
            "--cmd",
            "sleep 30",
            "--events",
            str(events),
            "--interval",
            "0.05",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):  # wait for the started event
        if events.exists() and read_events(events):
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=5)
    assert proc.returncode == 130
    ev = read_events(events)
    assert ev[-1]["kind"] == "exited" and ev[-1]["detail"]["reason"] == "agentwatch_interrupted"
    child = ev[0]["detail"]["pid"]
    assert agentwatch.pid_alive(child)  # documented: Ctrl-C does not reach the child
    os.killpg(child, signal.SIGKILL)


# -- regression: a PID we cannot signal must not be reported as gone ----------

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can signal PID 1")


@not_root
def test_kill_pid_raises_when_not_permitted():
    # PID 1 exists but belongs to root. Before the fix this returned "already_gone",
    # and the supervisor then wrote an "exited" event for a process still running.
    with pytest.raises(PermissionError):
        agentwatch.kill_pid(1, grace=0)


@not_root
def test_cli_rejects_unsignalable_pid_for_kill_and_restart(capsys):
    assert agentwatch.main(["watch", "--pid", "1", "--log", "x", "--policy", "kill"]) == agentwatch.EXIT_USAGE
    assert "cannot signal" in capsys.readouterr().err
    assert (
        agentwatch.main(["watch", "--pid", "1", "--log", "x", "--policy", "restart", "--cmd", "true"])
        == agentwatch.EXIT_USAGE
    )
