"""Bootstrap admission, cleanup and inherited-stream contracts."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


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
