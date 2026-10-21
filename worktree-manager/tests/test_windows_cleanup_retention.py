"""Windows retirement failures retain real adapter custody and candidate paths."""

from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import types

import pytest

from agent_procutil import owned_process
from worktree_manager import installed_runtime, self_install, self_install_payload
from worktree_manager.engine_posix_process import CleanupError


def _bootstrap_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_runtime.py"
    module = types.ModuleType("windows_bootstrap_retention")
    module.__file__ = str(script)
    before = sys.path.copy()
    try:
        exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    finally:
        sys.path[:] = before
    return module


@pytest.fixture
def failed_retirement(monkeypatch):
    streams = [io.StringIO(), io.StringIO()]
    process = SimpleNamespace(
        pid=123, returncode=0, stdin=None, stdout=streams[0], stderr=streams[1],
        communicate=lambda *_a, **_k: ("", ""),
        wait=lambda **_: pytest.fail("waited after unconfirmed Job retirement"),
    )

    def refuse(_):
        raise OSError("injected Job retirement failure")

    job = SimpleNamespace(terminate_and_wait=refuse)
    monkeypatch.setattr(owned_process, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        owned_process, "spawn_sync_in_kill_on_close_job", lambda *_a, **_k: (process, job),
    )
    monkeypatch.setattr(installed_runtime, "os", SimpleNamespace(name="nt"))
    return process, job, streams


def test_windows_owned_adapter_preserves_cleanup_identity(tmp_path, failed_retirement):
    process, job, streams = failed_retirement
    with pytest.raises(CleanupError, match="Windows construction cleanup") as caught:
        installed_runtime._owned_runner(["constructor"], cwd=tmp_path, timeout=1)
    assert caught.value.process is process and caught.value.job is job
    assert isinstance(caught.value.__cause__, owned_process.OwnedCleanupError)
    assert not any(stream.closed for stream in streams)


def test_failed_windows_retirement_retains_bootstrap_runner(
    tmp_path, monkeypatch, capsys, failed_retirement,
):
    module = _bootstrap_module()
    root = tmp_path / "manager"
    monkeypatch.setattr(module, "default_root", lambda: root)
    monkeypatch.setattr(module, "payload_version", lambda _: "1.2.3")
    monkeypatch.setattr(module, "_copy_payload", lambda *_: None)
    monkeypatch.setattr(module, "_launch", lambda *_: pytest.fail("unsafe launch"))
    assert module.main(["--version"]) == 1
    assert len(list(root.glob(".bootstrap.*"))) == 1
    assert "cleanup unconfirmed; retained" in capsys.readouterr().err
    assert not any(stream.closed for stream in failed_retirement[2])


def test_failed_windows_retirement_retains_payload_staging(
    tmp_path, monkeypatch, failed_retirement,
):
    source = tmp_path / "payload"
    source.mkdir()
    (source / "pyproject.toml").write_text("fixture")
    slot = tmp_path / "1.2.3"
    monkeypatch.setattr(self_install, "_SLOT_KEY_FILES", ["pyproject.toml"])
    monkeypatch.setattr(self_install, "payload_version", lambda _: "1.2.3")
    monkeypatch.setattr(self_install_payload, "_materialize_payload_pointers", lambda *_: None)
    monkeypatch.setattr(
        self_install_payload, "build_runtime",
        lambda candidate: installed_runtime._owned_runner(["constructor"], cwd=candidate, timeout=1),
    )
    with pytest.raises(CleanupError) as caught:
        self_install_payload._copy_payload(source, slot)
    assert caught.value.process is failed_retirement[0]
    assert caught.value.job is failed_retirement[1]
    retained = list(tmp_path.glob("1.2.3.staging.*"))
    assert len(retained) == 1 and (retained[0] / "pyproject.toml").is_file()
    assert not slot.exists()
    assert not any(stream.closed for stream in failed_retirement[2])


@pytest.mark.skipif(os.name != "nt", reason="native Windows retained-Job failure")
@pytest.mark.parametrize("operation", ["bootstrap", "payload"])
def test_native_failed_retirement_keeps_live_constructor_and_paths_until_cleanup(
    tmp_path, monkeypatch, operation,
):
    from agent_procutil import JobHandle

    ready = tmp_path / "constructor-ready"
    command = (
        "import pathlib,time;"
        f"pathlib.Path({str(ready)!r}).write_text('ready');time.sleep(30)"
    )
    terminate = JobHandle.terminate_and_wait
    jobs = []
    retained = []

    def refuse(job, _timeout):
        jobs.append(job)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        raise OSError("injected native Job retirement failure")

    def construct(_argv=None, *, cwd, timeout=None):
        try:
            return installed_runtime._owned_runner(
                [sys.executable, "-I", "-B", "-c", command], cwd=cwd, timeout=1,
            )
        except CleanupError as error:
            retained.append(error)
            raise

    monkeypatch.setattr(JobHandle, "terminate_and_wait", refuse)
    try:
        if operation == "bootstrap":
            module = _bootstrap_module()
            root = tmp_path / "manager"
            monkeypatch.setattr(module, "default_root", lambda: root)
            monkeypatch.setattr(module, "payload_version", lambda _: "1.2.3")
            monkeypatch.setattr(module, "_copy_payload", lambda _, slot: slot.mkdir(parents=True))
            monkeypatch.setattr(module, "_owned_runner", construct)
            assert module.main(["--version"]) == 1
            assert len(list(root.glob(".bootstrap.*"))) == 1
        else:
            source = tmp_path / "payload"
            source.mkdir()
            (source / "pyproject.toml").write_text("fixture")
            slot = tmp_path / "1.2.3"
            monkeypatch.setattr(self_install, "_SLOT_KEY_FILES", ["pyproject.toml"])
            monkeypatch.setattr(self_install, "payload_version", lambda _: "1.2.3")
            monkeypatch.setattr(self_install_payload, "_materialize_payload_pointers", lambda *_: None)
            monkeypatch.setattr(
                self_install_payload, "build_runtime",
                lambda candidate: construct(cwd=candidate),
            )
            with pytest.raises(CleanupError):
                self_install_payload._copy_payload(source, slot)
            assert len(list(tmp_path.glob("1.2.3.staging.*"))) == 1
            assert not slot.exists()
        assert ready.exists(), "constructor startup was not witnessed"
        assert len(retained) == 1 and retained[0].job is jobs[0]
        assert retained[0].process.poll() is None, "failure did not retain live process custody"
    finally:
        for job in jobs:
            terminate(job, 5)
        for error in retained:
            error.process.communicate(timeout=5)
            for stream in (error.process.stdin, error.process.stdout, error.process.stderr):
                if stream is not None:
                    stream.close()
