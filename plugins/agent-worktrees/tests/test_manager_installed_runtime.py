"""The engine probes a selected installed Manager without changing its environment."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import venv

import pytest

from agent_worktrees import manager_launch_cli as launch


@pytest.fixture
def manager_runtime(tmp_path, monkeypatch):
    root = tmp_path / "manager root"
    slot = root / "versions" / "1.2.3"
    environment = slot / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    site = (
        environment / "Lib" / "site-packages" if os.name == "nt" else
        environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    package = site / "worktree_manager"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    main = package / "__main__.py"
    main.write_text(
        "import sys\nfrom . import __version__\n"
        "assert sys.argv[1:] == ['--version']\n"
        "print('worktree-manager ' + __version__)\n",
        encoding="utf-8",
    )
    (slot / "bin").mkdir()
    (root / "current-version").write_text("1.2.3", encoding="utf-8")
    monkeypatch.setenv("WORKTREE_MANAGER_ROOT", str(root))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "foreign-python-home"))
    monkeypatch.setenv("UV_INTERNAL__PYTHONHOME", str(tmp_path / "foreign-uv-home"))
    assert shutil.which("uv") is None
    return slot, main


def _snapshot(slot):
    return {
        str(path.relative_to(slot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in slot.rglob("*") if path.is_file()
    }


def test_engine_probes_selected_manager_without_uv_or_environment_mutation(manager_runtime):
    slot, _ = manager_runtime
    before = _snapshot(slot)
    for _ in range(2):
        assert launch._usable_worktree_manager_launcher_dir() == slot / "bin"
        assert _snapshot(slot) == before
    assert not (slot / "src").exists()
    assert not (slot / "pyproject.toml").exists(), "probe cannot depend on a project manifest"


@pytest.mark.parametrize("failure", ["missing-interpreter", "broken-cli", "invalid-version"])
def test_installed_manager_probe_keeps_actionable_failure_diagnostics(
    manager_runtime, failure, capsys,
):
    slot, main = manager_runtime
    if failure == "missing-interpreter":
        python = slot / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "python.exe" if os.name == "nt" else "python"
        )
        python.unlink()
        diagnostic = "versioned runtime could not be started"
    elif failure == "broken-cli":
        main.write_text("raise SystemExit(7)\n", encoding="utf-8")
        diagnostic = "failed a --version health check (exit 7)"
    else:
        main.write_text("print('unsupported version')\n", encoding="utf-8")
        diagnostic = "did not include a supported version"
    before = _snapshot(slot)
    assert launch._usable_worktree_manager_launcher_dir() is None
    captured = capsys.readouterr()
    assert diagnostic in captured.err + captured.out
    assert _snapshot(slot) == before
