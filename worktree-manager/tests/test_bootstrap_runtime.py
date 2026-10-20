"""Bootstrap admission, cleanup and inherited-stream contracts."""

from __future__ import annotations

import os
import json
import select
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_bootstrap_adapters_require_stdlib_toml_and_import_without_site_packages(tmp_path):
    payload = Path(__file__).resolve().parents[1]
    for name in ("bootstrap.ps1", "bootstrap.sh"):
        body = (payload / name).read_text(encoding="utf-8")
        assert "--no-project --python '>=3.11'" in body
    if sys.version_info < (3, 11):
        pytest.skip("the selected bootstrap requires Python 3.11+, not the runtime's 3.10 floor")
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", "-B", "-c",
            "import runpy,sys;"
            "assert sys.version_info >= (3,11);"
            "runpy.run_path(sys.argv[1],run_name='dependency_free_bootstrap');"
            "from worktree_manager import _trusted_pointer_materializer;"
            "assert 'tomli' not in sys.modules",
            str(payload / "scripts" / "bootstrap_runtime.py"),
        ],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_runtime.py"
    module = types.ModuleType("manager_bootstrap_fixture")
    module.__file__ = str(script)
    before = sys.path.copy()
    try:
        exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    finally:
        sys.path[:] = before
    root = tmp_path / "manager"
    monkeypatch.setattr(module, "default_root", lambda: root)
    monkeypatch.setattr(module, "payload_version", lambda _: "1.2.3")
    monkeypatch.setattr(module, "needs_install", lambda *_: True)
    return module, root


def test_bootstrap_construction_failure_cleans_runner_and_never_launches(bootstrap, monkeypatch, capsys):
    module, root = bootstrap

    def refuse(*_):
        raise RuntimeError("construction refused")

    monkeypatch.setattr(module, "_copy_payload", refuse)
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected launch"))
    assert module.main(["--version"]) == 1
    assert "construction refused" in capsys.readouterr().err
    assert not list(root.iterdir())


def test_bootstrap_install_failure_preserves_exit_and_streams(bootstrap, monkeypatch, capsys):
    module, root = bootstrap
    monkeypatch.setattr(module, "_copy_payload", lambda *_: None)
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=7, stdout="construction output\n", stderr="failure detail\n")

    monkeypatch.setattr(module, "_owned_runner", run)
    assert module.main(["--version"]) == 7
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["self-install", "--apply"]
    assert calls[0][1]["cwd"].is_relative_to(root)
    assert calls[0][1]["timeout"] == 600
    observed = capsys.readouterr()
    assert observed.out == "construction output\n"
    assert observed.err == "failure detail\n"
    assert not list(root.iterdir())


def test_bootstrap_current_install_skips_construction_and_propagates_launch_exit(
    bootstrap, monkeypatch,
):
    module, root = bootstrap
    monkeypatch.setattr(module, "needs_install", lambda *_: False)
    monkeypatch.setattr(module, "_copy_payload", lambda *_: pytest.fail("unnecessary construction"))
    monkeypatch.setattr(module, "current_version", lambda _: "1.2.3")
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=9)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main(["two words"]) == 9
    assert calls[0][0] == [
        str(module.runtime_python(module.version_slot("1.2.3", root))),
        "-I", "-B", "-X", "utf8", "-m", "worktree_manager", "two words",
    ]
    assert calls[0][1]["stdout"] is sys.stdout
    assert calls[0][1]["stderr"] is sys.stderr


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="native Linux identity-pinned nested constructor custody",
)
@pytest.mark.parametrize("outcome", ["timeout", "interrupt", "escaped-control"])
def test_nested_constructor_stops_before_bootstrap_runner_cleanup(
    bootstrap, tmp_path, monkeypatch, outcome,
):
    from worktree_manager import engine_process, installed_runtime

    module, root = bootstrap
    ready = tmp_path / "constructor.json"
    activity = tmp_path / "activity"
    worker = (
        "import json,os,time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).write_text(json.dumps({{"
        "'pid':os.getpid(),'start':Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]"
        "}))\n"
        "while True:\n"
        f"    Path({str(activity)!r}).write_text(str(time.monotonic()))\n"
        "    time.sleep(.01)\n"
    )
    nested = (
        "import os,sys\nfrom pathlib import Path\n"
        "from worktree_manager import engine_process,installed_runtime\n"
        "engine_process.contained_test_mode=lambda:False\n"
        + ("os.environ.pop(engine_process.OWNED_GROUP_ENV,None)\n" if outcome == "escaped-control" else "")
        + f"installed_runtime._owned_runner([sys.executable,'-c',{worker!r}],"
        "cwd=Path(sys.argv[1]),timeout=15)\n"
    )
    monkeypatch.setattr(module, "_copy_payload", lambda _source, slot: slot.mkdir(parents=True))
    monkeypatch.setattr(engine_process, "contained_test_mode", lambda: False)
    monkeypatch.delenv(engine_process.OWNED_GROUP_ENV, raising=False)
    monkeypatch.setattr(
        module, "_owned_runner",
        lambda _argv, *, cwd, timeout: installed_runtime._owned_runner(
            [sys.executable, "-I", "-B", "-c", nested, str(cwd)], cwd=cwd, timeout=2,
        ),
    )
    if outcome == "interrupt":
        def interrupt_when_constructing(*_):
            deadline = time.monotonic() + 5
            while not activity.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert activity.exists(), "constructor never entered its owned boundary"
            raise KeyboardInterrupt()

        monkeypatch.setattr(engine_process.engine_posix_process.select, "select", interrupt_when_constructing)
    cleanup = module.shutil.rmtree
    retained_fds = []
    observed_cleanup = []

    def verify_before_cleanup(path, *args, **kwargs):
        if Path(path).parent == root and Path(path).name.startswith(".bootstrap."):
            assert ready.exists() and activity.exists(), "nested constructor never started"
            record = json.loads(ready.read_text())
            try:
                fd = os.pidfd_open(record["pid"])
            except ProcessLookupError:
                fd = None
            if fd is not None:
                try:
                    start = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
                except FileNotFoundError:
                    start = None
                if start != record["start"]:
                    os.close(fd)
                else:
                    retained_fds.append(fd)
                    poll = select.poll()
                    poll.register(fd, select.POLLIN)
                    assert poll.poll(1000), "constructor remained live before bootstrap cleanup"
            observed_cleanup.append(Path(path))
        return cleanup(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", verify_before_cleanup)
    try:
        if outcome == "escaped-control":
            with pytest.raises(AssertionError, match="constructor remained live before bootstrap cleanup"):
                module.main(["--version"])
            assert list(root.glob(".bootstrap.*")), "failed custody must retain the runner"
        elif outcome == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                module.main(["--version"])
        else:
            assert module.main(["--version"]) == 1
        if outcome != "escaped-control":
            assert len(observed_cleanup) == 1
            assert not observed_cleanup[0].exists()
    finally:
        for fd in retained_fds:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            try:
                assert poll.poll(2000), "fixture could not retire its identity-pinned constructor"
            finally:
                os.close(fd)
