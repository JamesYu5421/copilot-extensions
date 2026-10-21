"""Group lifetime remains owned after engine success and inner capture failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from worktree_manager import engine_posix_process as pp
from worktree_manager import engine_posix_broker as broker


@pytest.mark.parametrize("gone", [False, True])
def test_group_owner_is_not_reaped_before_descendant_sweep(monkeypatch, gone):
    events = []
    owner = SimpleNamespace(
        pid=123,
        poll=lambda: pytest.fail("reaped the group owner before cleanup"),
    )
    owner.communicate = lambda **kwargs: events.append(("reap", kwargs["timeout"]))

    def signal_group(pid, sig):
        events.append(("signal", pid, sig))
        if sig == 0 or gone:
            raise ProcessLookupError

    monkeypatch.setattr(broker.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(broker.os, "killpg", signal_group, raising=False)
    branch = broker.Branch(owner, None, None, 0)
    broker.retire(branch)
    assert events == [("signal", 123, 9), ("reap", 5), ("signal", 123, 0)]
    assert branch.reaped


def test_reaped_group_is_only_observed_never_signalled_again(monkeypatch):
    events = []
    branch = broker.Branch(SimpleNamespace(pid=123), None, None, 0, reaped=True)
    monkeypatch.setattr(broker.time, "monotonic", iter([0, 6]).__next__)
    monkeypatch.setattr(
        broker.os, "killpg", lambda pid, sig: events.append((pid, sig)), raising=False,
    )
    with pytest.raises(broker.CleanupError, match="has not disappeared"):
        broker.retire(branch)
    assert events == [(123, 0)]


def test_branch_is_registered_before_command_permission(monkeypatch):
    events = []
    state = broker.Broker(SimpleNamespace(getsockname=lambda: "private-socket"), None)
    process = SimpleNamespace(
        stdin=SimpleNamespace(
            write=lambda data: events.append((data, len(state.branches))),
            flush=lambda: None,
        ),
    )
    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["env"][broker.GROUP_ENV] == "1"
        assert kwargs["env"][broker.ENDPOINT_ENV] == "private-socket"
        return process

    monkeypatch.setattr(broker.subprocess, "Popen", spawn)
    state.launch(None, {"parent": None, "env": {}, "argv": ["engine"], "cwd": None, "timeout": 1}, [1, 2])
    assert events == [(b"G", 1)]


def test_unbounded_completion_wait_still_requires_cleanup_acknowledgement(monkeypatch):
    cleanup_waits = []
    connection = SimpleNamespace(settimeout=cleanup_waits.append)
    waits = []
    monkeypatch.setattr(
        pp.select, "select",
        lambda _read, _write, _errors, timeout: waits.append(timeout) or ([connection], [], []),
    )
    reply = {"returncode": 0, "cleaned": True}
    monkeypatch.setattr(broker, "receive_frame", lambda _: (reply, []))
    assert pp._complete(connection, ["engine"], None) is reply
    assert waits == [None]
    assert cleanup_waits == [broker.CLEANUP_TIMEOUT]


def test_unbounded_branch_registers_without_a_numeric_deadline(monkeypatch):
    state = broker.Broker(SimpleNamespace(getsockname=lambda: "private-socket"), None)
    process = SimpleNamespace(
        stdin=SimpleNamespace(write=lambda _: None, flush=lambda: None),
    )
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *_a, **_k: process)
    state.launch(None, _request(timeout=None), [1, 2])
    assert len(state.branches) == 1
    assert next(iter(state.branches.values())).deadline is None


def test_outer_branch_retires_nested_groups_before_acknowledging(monkeypatch):
    events = []
    root = SimpleNamespace(close=lambda: events.append("close-root"))
    nested = SimpleNamespace(close=lambda: events.append("close-nested"))
    state = broker.Broker(None, root)
    state.branches = {
        "root": broker.Branch(SimpleNamespace(pid=1), root, None, 0),
        "nested": broker.Branch(SimpleNamespace(pid=2), nested, "root", 0),
    }
    monkeypatch.setattr(broker, "retire", lambda branch: events.append(("retire", branch.process.pid)))
    monkeypatch.setattr(broker, "send_frame", lambda connection, _: events.append(
        "ack-root" if connection is root else "ack-nested",
    ))
    state.finish("root", {"returncode": 0})
    assert events == [("retire", 2), "ack-nested", "close-nested",
                      ("retire", 1), "ack-root", "close-root"]


def _request(**overrides):
    return {"parent": None, "env": {}, "argv": ["engine"], "cwd": None,
            "timeout": 1, **overrides}


def test_retired_parent_cannot_register_more_work(monkeypatch):
    state = broker.Broker(None, None)
    monkeypatch.setattr(
        broker.subprocess, "Popen", lambda *_a, **_k: pytest.fail("orphan branch spawned"),
    )
    with pytest.raises(OSError, match="parent branch is no longer owned"):
        state.launch(None, _request(parent="retired"), [1, 2])
    assert not state.branches


@pytest.mark.parametrize("cleanup_confirmed", [False, True])
def test_failed_execution_grant_cannot_acknowledge_before_retirement(
    monkeypatch, cleanup_confirmed,
):
    events = []
    connection = SimpleNamespace(
        settimeout=lambda _: None, close=lambda: events.append("closed"),
    )
    state = broker.Broker(SimpleNamespace(getsockname=lambda: "private-socket"), connection)

    def fail_grant(_):
        raise BrokenPipeError("execution grant failed")

    def retire(branch):
        events.append(("retired", branch.process.pid))
        if not cleanup_confirmed:
            raise broker.CleanupError("still live")

    process = SimpleNamespace(
        pid=123, stdin=SimpleNamespace(write=fail_grant, flush=lambda: None),
    )
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(broker, "receive_frame", lambda *_a, **_k: (_request(), [101, 102]))
    monkeypatch.setattr(broker, "retire", retire)
    monkeypatch.setattr(broker, "send_frame", lambda _c, value: events.append(("ack", value)))
    monkeypatch.setattr(broker, "os", SimpleNamespace(
        close=lambda fd: events.append(("closed-fd", fd)),
    ))
    if cleanup_confirmed:
        state.accept(connection)
        assert events[0] == ("retired", 123)
        assert events[1][0] == "ack" and events[1][1]["cleaned"] is True
        assert events[2] == "closed"
        assert not state.branches
    else:
        with pytest.raises(broker.CleanupError, match="still live"):
            state.accept(connection)
        assert len(state.branches) == 1
        assert all(item != "closed" and item[0] != "ack" for item in events)
    assert events[-2:] == [("closed-fd", 101), ("closed-fd", 102)]


def test_cancellation_wins_over_a_queued_nested_registration(monkeypatch):
    events = []
    connection = SimpleNamespace(
        recv=lambda _: b"", close=lambda: events.append("closed"),
    )
    listener = SimpleNamespace(accept=lambda: pytest.fail("registered work after cancellation"))
    state = broker.Broker(listener, connection)
    state.branches["root"] = broker.Branch(
        SimpleNamespace(pid=123, stdout=object()), connection, None, float("inf"),
    )
    monkeypatch.setattr(state, "accept", lambda _: None)
    monkeypatch.setattr(broker.select, "select", lambda *_: ([connection, listener], [], []))
    monkeypatch.setattr(broker, "retire", lambda _: events.append("retired"))
    monkeypatch.setattr(broker, "send_frame", lambda *_: events.append("ack"))
    state.run()
    assert events == ["retired", "ack", "closed"]
    assert not state.branches


def test_truncated_descriptor_delivery_closes_every_received_descriptor(monkeypatch):
    import array
    import struct

    closed = []
    constants = SimpleNamespace(
        SOL_SOCKET=1, SCM_RIGHTS=2, MSG_CTRUNC=8, CMSG_SPACE=lambda _: 32,
    )
    monkeypatch.setattr(broker, "socket", constants)
    monkeypatch.setattr(broker, "os", SimpleNamespace(close=closed.append))
    connection = SimpleNamespace(recvmsg=lambda *_: (
        struct.pack("!I", 2), [(1, 2, array.array("i", [101, 102]).tobytes())], 8, None,
    ))
    with pytest.raises(OSError, match="descriptors are incomplete"):
        broker.receive_frame(connection, descriptors=True)
    assert closed == [101, 102]


def test_oversized_completion_is_rejected_without_reading_its_body():
    import struct

    calls = []
    connection = SimpleNamespace(
        recv=lambda size: calls.append(size) or struct.pack("!I", broker._MAX_FRAME + 1),
    )
    with pytest.raises(ValueError, match="frame is too large"):
        broker.receive_frame(connection)
    assert calls == [4]


@pytest.mark.parametrize("value", [None, {}, {"cleaned": False}, {"cleaned": 1}])
def test_missing_cleanup_acknowledgement_never_confirms_custody(monkeypatch, value):
    connection = SimpleNamespace(settimeout=lambda _: None)
    monkeypatch.setattr(pp.select, "select", lambda *_: ([connection], [], []))
    monkeypatch.setattr(broker, "receive_frame", lambda _: (value, []))
    with pytest.raises(broker.CleanupError, match="receipt"):
        pp._complete(connection, ["engine"], 1)


def test_failed_retirement_never_sends_cleanup_acknowledgement(monkeypatch):
    connection = SimpleNamespace(close=lambda: pytest.fail("custody connection released"))
    state = broker.Broker(None, connection)
    state.branches["root"] = broker.Branch(None, connection, None, 0)

    def refused(_):
        raise broker.CleanupError("group remains live")

    monkeypatch.setattr(broker, "retire", refused)
    monkeypatch.setattr(broker, "send_frame", lambda *_: pytest.fail("false cleanup receipt"))
    with pytest.raises(broker.CleanupError, match="remains live"):
        state.finish("root", {"returncode": 0})
    assert "root" in state.branches


@pytest.mark.parametrize("field,value", [
    ("timeout", 0), ("timeout", -1), ("timeout", float("nan")),
    ("timeout", float("inf")), ("timeout", True),
    ("argv", []), ("argv", ["bad\0argument"]), ("env", {"NAME": 1}),
    ("cwd", "relative"), ("parent", []),
])
def test_invalid_request_cannot_launch_work(monkeypatch, field, value):
    state = broker.Broker(None, None)
    request = _request(**{field: value})
    monkeypatch.setattr(
        broker.subprocess, "Popen", lambda *_a, **_k: pytest.fail("invalid work launched"),
    )
    with pytest.raises((ValueError, OSError)):
        state.launch(None, request, [])
    assert not state.branches


@pytest.mark.parametrize("timeout", [1, None])
def test_unconfirmed_cleanup_preserves_broker_and_its_directory(monkeypatch, tmp_path, timeout):
    directory = tmp_path / "broker"
    directory.mkdir()
    connection = SimpleNamespace(close=lambda: pytest.fail("unconfirmed connection closed"))
    child = SimpleNamespace(fileno=lambda: 42, close=lambda: None)
    process = SimpleNamespace(pid=123)
    monkeypatch.setattr(pp.tempfile, "mkdtemp", lambda **_: str(directory))
    monkeypatch.setattr(pp.socket, "socketpair", lambda: (connection, child))
    monkeypatch.setattr(pp.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(pp, "_ready", lambda _: None)
    monkeypatch.setattr(broker, "send_frame", lambda *_: None)

    def interrupted(*_):
        raise KeyboardInterrupt()

    def unconfirmed(_):
        raise broker.CleanupError("missing acknowledgement")

    monkeypatch.setattr(pp, "_complete", interrupted)
    monkeypatch.setattr(pp, "_acknowledge", unconfirmed)
    with pytest.raises(broker.CleanupError) as raised:
        pp.run(["engine"], timeout=timeout, env={})
    assert raised.value.process is process
    assert raised.value.connection is connection
    assert raised.value.directory == directory
    assert directory.is_dir()


def test_partial_request_failure_still_requires_cleanup_ack(monkeypatch, tmp_path):
    events = []
    directory = tmp_path / "broker"
    directory.mkdir()
    connection = SimpleNamespace(close=lambda: events.append("closed"))
    child = SimpleNamespace(fileno=lambda: 42, close=lambda: None)
    process = SimpleNamespace(wait=lambda **_: events.append("reaped"))
    monkeypatch.setattr(pp.tempfile, "mkdtemp", lambda **_: str(directory))
    monkeypatch.setattr(pp.socket, "socketpair", lambda: (connection, child))
    monkeypatch.setattr(pp.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(pp, "_ready", lambda _: None)

    def partial(*_):
        raise OSError("partial request")

    monkeypatch.setattr(broker, "send_frame", partial)
    monkeypatch.setattr(pp, "_acknowledge", lambda _: events.append("acknowledged"))
    with pytest.raises(OSError, match="partial request"):
        pp.run(["engine"], timeout=1, env={})
    assert events == ["acknowledged", "closed", "reaped"]
    assert not directory.exists()


@pytest.mark.parametrize("missing", [broker.ENDPOINT_ENV, broker.TOKEN_ENV])
def test_incomplete_inherited_context_refuses_before_launch(monkeypatch, missing):
    monkeypatch.setenv(broker.ENDPOINT_ENV, "unused-endpoint")
    monkeypatch.setenv(broker.TOKEN_ENV, "unused-token")
    monkeypatch.delenv(missing)
    monkeypatch.setattr(
        pp.subprocess, "Popen", lambda *_a, **_k: pytest.fail("unowned nested launch"),
    )
    with pytest.raises(RuntimeError, match="outside the older Manager process"):
        pp.run(["engine"], inherited=True, timeout=1, env={})


@pytest.mark.parametrize("reply", [
    {"ready": False, "error": "injected bind failure"}, None, {},
])
def test_broker_startup_failure_is_reported_before_construction(monkeypatch, reply):
    connection = SimpleNamespace(settimeout=lambda _: None)
    monkeypatch.setattr(broker, "receive_frame", lambda _: (reply, []))
    with pytest.raises(OSError, match="construction broker startup failed"):
        pp._ready(connection)


def test_broker_startup_clears_its_finite_readiness_timeout(monkeypatch):
    waits = []
    connection = SimpleNamespace(settimeout=waits.append)
    monkeypatch.setattr(broker, "receive_frame", lambda _: ({"ready": True}, []))
    pp._ready(connection)
    assert waits == [broker.CLEANUP_TIMEOUT, None]


@pytest.mark.parametrize("retirement", ["confirmed", "timeout", "interrupted"])
def test_relative_connector_preserves_directory_and_exact_custody(
    monkeypatch, tmp_path, retirement,
):
    endpoint = tmp_path / ("long" * 40) / "broker"
    channel, child, connection = MagicMock(), MagicMock(), MagicMock()
    child.fileno.return_value = 42
    process = SimpleNamespace(returncode=0)
    events = []

    def wait(**kwargs):
        events.append(("wait", kwargs["timeout"]))
        if retirement == "timeout":
            raise subprocess.TimeoutExpired(["connector"], kwargs["timeout"])
        if retirement == "interrupted":
            raise KeyboardInterrupt()
        assert not connection.close.called

    def spawn(argv, **kwargs):
        assert argv[-3:] == ["--connect", "42", "broker"]
        assert kwargs["cwd"] == endpoint.parent
        assert kwargs["pass_fds"] == (42,)
        return process

    process.wait = wait
    monkeypatch.setattr(pp.socket, "socketpair", lambda: (channel, child))
    monkeypatch.setattr(pp.socket, "socket", lambda **_: connection)
    monkeypatch.setattr(pp.subprocess, "Popen", spawn)
    monkeypatch.setattr(broker, "receive_frame", lambda *_a, **_k: (
        {"connected": True}, [101],
    ))
    if retirement == "confirmed":
        assert pp._connect(str(endpoint)) is connection
        connection.settimeout.assert_called_once_with(None)
    else:
        with pytest.raises(broker.CleanupError, match="connector retirement") as caught:
            pp._connect(str(endpoint))
        assert caught.value.process is process
        assert caught.value.connection is connection
        assert caught.value.directory == endpoint.parent
        assert not connection.close.called
    assert events == [("wait", broker.CLEANUP_TIMEOUT)]


def test_failed_connector_surfaces_startup_stderr_after_confirmed_exit(monkeypatch, tmp_path):
    channel, child = MagicMock(), MagicMock()
    child.fileno.return_value = 42

    def spawn(_argv, **kwargs):
        kwargs["stderr"].write(b"injected connect failure")
        return SimpleNamespace(returncode=1, wait=lambda **_: None)

    def refuse(*_a, **_k):
        raise OSError("connection ended before descriptor delivery")

    monkeypatch.setattr(pp.socket, "socketpair", lambda: (channel, child))
    monkeypatch.setattr(pp.subprocess, "Popen", spawn)
    monkeypatch.setattr(broker, "receive_frame", refuse)
    with pytest.raises(OSError, match="injected connect failure"):
        pp._connect(str(tmp_path / "broker"))


@pytest.mark.skipif(os.name == "nt", reason="native Linux/macOS construction ownership")
@pytest.mark.parametrize(
    "outcome", [
        "timeout", "interrupt", "capture", "success", "signal-ignored",
        "unbounded", "unbounded-interrupt",
    ],
)
def test_native_inner_cleanup_precedes_candidate_deletion(tmp_path, outcome):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).parent / "fixtures" / "nested_custody.py"),
         str(root), str(tmp_path), outcome],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"outcome": outcome, "exited_before_cleanup": True}


@pytest.mark.skipif(os.name == "nt", reason="native POSIX relative Unix endpoints")
def test_native_nested_construction_keeps_long_temporary_root(tmp_path):
    temporary = tmp_path / ("long-root-" * 16)
    temporary.mkdir()
    assert len(os.fsencode(str(temporary))) > 118
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).parent / "fixtures" / "nested_custody.py"),
         str(root), str(tmp_path), "success"],
        env=dict(os.environ, TMPDIR=str(temporary)),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"outcome": "success", "exited_before_cleanup": True}
    assert not list(temporary.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="native POSIX broker working directory")
def test_broker_private_working_directory_does_not_change_target_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = pp.run(
        [sys.executable, "-I", "-B", "-c", "import os;print(os.getcwd())"],
        timeout=10, env=dict(os.environ), stdout=subprocess.PIPE, text=True,
    )
    assert result.returncode == 0
    assert Path(result.stdout.strip()) == tmp_path


@pytest.mark.skipif(os.name == "nt", reason="native POSIX broker startup failure")
def test_native_bind_failure_reports_cause_without_launching_work(tmp_path, monkeypatch):
    directory = tmp_path / "broker-root"
    directory.mkdir()
    (directory / "broker").write_text("occupied")
    effect = tmp_path / "must-not-exist"
    monkeypatch.setattr(pp.tempfile, "mkdtemp", lambda **_: str(directory))
    with pytest.raises(OSError, match="construction broker startup failed") as caught:
        pp.run(
            [sys.executable, "-I", "-B", "-c",
             f"from pathlib import Path;Path({str(effect)!r}).touch()"],
            timeout=10, env=dict(os.environ),
        )
    assert not isinstance(caught.value, pp.CleanupError)
    assert not effect.exists()
    assert not directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="native POSIX group lifetime")
def test_real_spawn_failure_preserves_the_engine_error(tmp_path):
    with pytest.raises(OSError, match="No such file"):
        pp.run(
            [str(tmp_path / "missing-engine")], timeout=10, text=True,
            env=dict(os.environ), stderr=subprocess.DEVNULL,
        )


@pytest.mark.skipif(os.name == "nt", reason="native POSIX group lifetime")
def test_real_degraded_capture_leaves_no_live_grandchild(tmp_path):
    ready = tmp_path / "ready"
    evidence = tmp_path / "alive"
    grandchild = (
        "import time\nfrom pathlib import Path\n"
        f"p=Path({str(evidence)!r})\n"
        "while True:\n    p.write_text(str(time.monotonic()))\n    time.sleep(.05)\n"
    )
    renderer = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}])\n"
        "time.sleep(60)\n"
    )
    root = Path(__file__).resolve().parents[2]
    engine = (
        "import importlib.util,json,os,sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(root / 'libs' / 'agent-procutil' / 'src')!r})\n"
        f"s=importlib.util.spec_from_file_location('push_timeout',"
        f"{str(root / 'plugins' / 'agent-worktrees' / 'src' / 'agent_worktrees' / 'push_timeout.py')!r})\n"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        "spawn=m.spawn_sync_in_kill_on_close_job\n"
        "def failing_spawn(*args,**kwargs):\n"
        "    child,job=spawn(*args,**kwargs)\n"
        "    def failed_capture(**kwargs):\n"
        "        deadline=time.monotonic()+10\n"
        f"        while not Path({str(evidence)!r}).exists():\n"
        "            if time.monotonic()>deadline: raise RuntimeError('grandchild never started')\n"
        "            time.sleep(.05)\n"
        "        raise OSError('injected renderer capture failure')\n"
        "    child.communicate=failed_capture\n"
        "    return child,job\n"
        "m.spawn_sync_in_kill_on_close_job=failing_spawn\n"
        "try:\n"
        f"    m.run_bounded([sys.executable,'-c',{renderer!r}],cwd=None,env=dict(os.environ),"
        "timeout=None,inherit_process_group=True)\n"
        "except OSError:\n"
        f"    Path({str(ready)!r}).write_text('degraded')\n"
        "    print(json.dumps({'action':'exec'}))\n"
    )
    result = pp.run(
        [sys.executable, "-c", engine], timeout=20, text=True,
        env=dict(os.environ), stderr=subprocess.DEVNULL,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"action": "exec"}
    assert ready.read_text() == "degraded"
    assert evidence.exists()
    stopped = evidence.read_text()
    time.sleep(0.3)
    assert evidence.read_text() == stopped
