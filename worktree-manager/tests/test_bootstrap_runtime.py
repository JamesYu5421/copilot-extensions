"""Bootstrap admission, cleanup and inherited-stream contracts."""

from __future__ import annotations

import os
import subprocess
import sys
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
