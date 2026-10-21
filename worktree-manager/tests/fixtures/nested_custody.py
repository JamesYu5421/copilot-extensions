"""Stdlib native construction exercise; no installation or package resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import types
from unittest import mock


def exercise(root, workspace, outcome):
    sys.path[:0] = [
        str(root / "worktree-manager" / "src"), str(root / "libs" / "agent-procutil" / "src"),
    ]
    from worktree_manager import engine_posix_process as owned, installed_runtime
    from worktree_manager import engine_process

    engine_process.contained_test_mode = lambda: False
    stage = workspace / "candidate"
    stage.mkdir()
    ready = workspace / "ready"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint = str(workspace / "life")
    listener.bind(endpoint)
    listener.listen()
    listener.settimeout(10)
    worker = (
        "import os,socket,time\nfrom pathlib import Path\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        f"s.connect({endpoint!r})\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "s.sendall(b'ready')\n"
        "while True: time.sleep(.01)\n"
    )
    policy = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{worker!r}])\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.01)\n"
        + (
            "raise SystemExit(7)\n" if outcome == "signal-ignored"
            else "" if outcome in ("success", "unbounded") else "time.sleep(60)\n"
        )
    )
    original = owned._complete
    identity = []

    def capture(connection, argv, timeout):
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("grandchild never started")
            time.sleep(.01)
        if hasattr(os, "pidfd_open"):
            try:
                identity.append(os.pidfd_open(int(ready.read_text())))
            except ProcessLookupError:
                assert outcome in ("success", "signal-ignored", "unbounded")
        if outcome in ("interrupt", "unbounded-interrupt"):
            raise KeyboardInterrupt()
        if outcome == "capture":
            raise OSError("injected capture failure")
        return original(connection, argv, timeout)

    owned._complete = capture
    try:
        try:
            if outcome in ("unbounded", "unbounded-interrupt"):
                from worktree_manager import engine_client

                plan = {
                    "action": "exec", "work_dir": str(stage), "status_path": str(stage),
                    "cmd": [sys.executable, "--version"], "env": {},
                    "worktree_id": "fixture", "post_exit": False, "no_mux": True,
                }
                command = policy + f"print({json.dumps(plan)!r})\n"
                os.environ[engine_client.ENGINE_ARGV_ENV] = json.dumps([
                    sys.executable, "-I", "-B", "-c", command,
                ])
                resolved = engine_client.resolve_launch_plan("fixture", worktree_id="fixture")
                assert resolved.is_exec and resolved.work_dir == str(stage)
            else:
                result = installed_runtime._owned_runner(
                    [sys.executable, "-c", policy], cwd=stage, timeout=2,
                )
                assert outcome in ("success", "signal-ignored")
                assert result.returncode == (7 if outcome == "signal-ignored" else 0)
        except subprocess.TimeoutExpired:
            assert outcome in ("timeout", "held-zombie")
        except KeyboardInterrupt:
            assert outcome in ("interrupt", "unbounded-interrupt")
        except OSError as error:
            assert outcome == "capture" and "injected" in str(error)
        connection, _ = listener.accept()
        try:
            connection.settimeout(.1)
            assert connection.recv(5) == b"ready"
            # The fixture holds this stream until process exit, not until a
            # signal was sent or a heartbeat happened to stop.
            assert connection.recv(1) == b"", "live descendant before destructive cleanup"
        finally:
            connection.close()
        for descriptor in identity:
            assert select.select([descriptor], [], [], 0)[0], "identity-pinned descendant is live"
        shutil.rmtree(stage)
        return {"outcome": outcome, "exited_before_cleanup": True}
    finally:
        for descriptor in identity:
            os.close(descriptor)
        listener.close()


def outer_exercise(root, workspace, outcome):
    sys.path[:0] = [
        str(root / "worktree-manager" / "src"), str(root / "libs" / "agent-procutil" / "src"),
    ]
    from worktree_manager import engine_process, engine_posix_process, installed_runtime

    script = root / "worktree-manager" / "scripts" / "bootstrap_runtime.py"
    module = types.ModuleType("native_manager_bootstrap")
    module.__file__ = str(script)
    exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    manager = workspace / "manager"
    ready = workspace / "outer-ready"
    endpoint = str(workspace / "outer-life")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(endpoint)
    listener.listen()
    listener.settimeout(10)
    connection = None
    identities = []
    observed_cleanup = []
    negative_detected = False
    worker = (
        "import os,socket\nfrom pathlib import Path\n"
        "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
        f"s.connect({endpoint!r})\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "s.sendall(b'ready')\n"
        "s.recv(1)\n"
    )
    nested = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path[:0]={[str(root / 'worktree-manager' / 'src'), str(root / 'libs' / 'agent-procutil' / 'src')]!r}\n"
        "from worktree_manager import engine_process,installed_runtime\n"
        "engine_process.contained_test_mode=lambda:False\n"
        + (
            "import subprocess,time\n"
            f"subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True)\n"
            "time.sleep(60)\n"
            if outcome == "escaped-control" else
            f"installed_runtime._owned_runner([sys.executable,'-c',{worker!r}],"
            "cwd=Path(sys.argv[1]),timeout=15)\n"
        )
    )
    complete = engine_posix_process._complete
    cleanup = shutil.rmtree

    def wait_for_worker(client, argv, timeout):
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("nested constructor never started")
            time.sleep(.01)
        if hasattr(os, "pidfd_open"):
            try:
                identities.append(os.pidfd_open(int(ready.read_text())))
            except ProcessLookupError:
                pass
        if outcome == "outer-interrupt":
            raise KeyboardInterrupt()
        return complete(client, argv, timeout)

    def verify_before_cleanup(path, *args, **kwargs):
        nonlocal connection
        if Path(path).parent == manager and Path(path).name.startswith(".bootstrap."):
            connection, _ = listener.accept()
            connection.settimeout(.3)
            received = b""
            while len(received) < 5:
                chunk = connection.recv(5 - len(received))
                assert chunk, "constructor closed before reporting readiness"
                received += chunk
            assert received == b"ready"
            try:
                assert connection.recv(1) == b"", "constructor live before bootstrap cleanup"
            except TimeoutError as error:
                raise AssertionError("constructor live before bootstrap cleanup") from error
            for descriptor in identities:
                assert select.select([descriptor], [], [], 1)[0], "constructor not exited"
            observed_cleanup.append(Path(path))
        return cleanup(path, *args, **kwargs)

    try:
        with mock.patch.multiple(
            module,
            default_root=lambda: manager,
            payload_version=lambda _: "1.2.3",
            needs_install=lambda *_: True,
            _copy_payload=lambda _source, slot: slot.mkdir(parents=True),
            _owned_runner=lambda _argv, *, cwd, timeout: installed_runtime._owned_runner(
                [sys.executable, "-I", "-B", "-c", nested, str(cwd)], cwd=cwd, timeout=2,
            ),
        ), mock.patch.object(
            engine_process, "contained_test_mode", lambda: False,
        ), mock.patch.object(
            engine_posix_process, "_complete", wait_for_worker,
        ), mock.patch.object(shutil, "rmtree", verify_before_cleanup):
            if outcome == "outer-foreign-home":
                os.environ["PYTHONHOME"] = str(workspace / "foreign-home")
                os.environ["UV_INTERNAL__PYTHONHOME"] = str(workspace / "foreign-uv-home")
            try:
                result = module.main(["--version"])
            except KeyboardInterrupt:
                assert outcome == "outer-interrupt"
            except AssertionError as error:
                assert outcome == "escaped-control" and "constructor live" in str(error)
                assert list(manager.glob(".bootstrap.*")), "failed witness must retain the runner"
                negative_detected = True
            else:
                assert outcome != "outer-interrupt" and result == 1
            if outcome == "escaped-control":
                assert negative_detected
            else:
                assert len(observed_cleanup) == 1 and not observed_cleanup[0].exists()
        return {
            "outcome": outcome, "exited_before_cleanup": not negative_detected,
            **({"negative_control_detected": True} if negative_detected else {}),
        }
    finally:
        if connection is not None:
            # The escaped negative control exits on this identity-bound stream.
            connection.close()
        for descriptor in identities:
            if negative_detected:
                assert select.select([descriptor], [], [], 2)[0], "negative-control cleanup failed"
            os.close(descriptor)
        listener.close()


def live_refusal(root):
    sys.path[:0] = [
        str(root / "worktree-manager" / "src"), str(root / "libs" / "agent-procutil" / "src"),
    ]
    from agent_procutil._caller_owned import _confirm_group
    from worktree_manager import engine_posix_broker as broker

    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    branch = broker.Branch(process, None, None, None)
    killpg = os.killpg
    try:
        with mock.patch.object(broker, "_linux_confirm_group", _confirm_group), mock.patch.object(
            broker.os, "killpg", lambda *_: None,
        ):
            try:
                broker.retire(branch)
            except broker.CleanupError as error:
                assert error.process is process
            else:
                raise AssertionError("a live identity-pinned group was accepted")
        assert branch.signalled and not branch.reaped
        assert process.poll() is None
    finally:
        killpg(process.pid, signal.SIGKILL)
        _confirm_group(process)
        process.communicate(timeout=5)
    return {"outcome": "live-refusal", "exited_before_cleanup": True}


def main():
    root, workspace = map(Path, sys.argv[1:3])
    outcome = sys.argv[3]
    if outcome == "live-refusal":
        print(json.dumps(live_refusal(root)), flush=True)
        return
    if outcome.startswith("outer-") or outcome == "escaped-control":
        print(json.dumps(outer_exercise(root, workspace, outcome)), flush=True)
        return
    if len(sys.argv) > 4:
        print(json.dumps(exercise(root, workspace, outcome)), flush=True)
        return
    sys.path[:0] = [
        str(root / "worktree-manager" / "src"), str(root / "libs" / "agent-procutil" / "src"),
    ]
    from worktree_manager import engine_process

    engine_process.contained_test_mode = lambda: False
    if outcome == "held-zombie":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "fixture subreaper setup failed")
    if outcome == "signal-ignored":
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    result = engine_process.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
         str(root), str(workspace), outcome, "--engine"],
        env=dict(os.environ), timeout=None if outcome.startswith("unbounded") else 20,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["exited_before_cleanup"]
    if outcome == "held-zombie":
        pid = int((workspace / "ready").read_text())
        observed = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        assert observed is not None and observed.si_pid == pid, "descendant was not held unreaped"
        assert os.waitpid(pid, os.WNOHANG)[0] == pid
    print(result.stdout, end="")


if __name__ == "__main__":
    main()
