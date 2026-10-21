"""bootstrap.ps1 must fail when a native step fails.

``$ErrorActionPreference = 'Stop'`` does not apply to native exit codes, and
``pwsh -File`` reports success unless the script itself fails. Without explicit
checks a failed ``uv`` resolution left no ``worktree-manager`` binstub while the
bootstrap still exited 0, so callers only failed later with a misleading
"command is missing" error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    PWSH is None or shutil.which("git") is None,
    reason="pwsh and git are required to exercise bootstrap.ps1",
)

_GIT_ID = [
    "-c", "user.email=e2e@example.invalid",
    "-c", "user.name=e2e",
    "-c", "commit.gpgsign=false",
]


def _source_repo(root: Path) -> Path:
    repo = root / "source"
    (repo / "worktree-manager").mkdir(parents=True)
    (repo / "worktree-manager" / "README.md").write_text("payload\n", "utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), *_GIT_ID, "commit", "-q", "-m", "payload"], check=True)
    return repo


def _fake_uv(bin_dir: Path, exit_code: int) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / "uv.cmd").write_text(f"@echo off\r\nexit /b {exit_code}\r\n", "utf-8")
    else:
        script = bin_dir / "uv"
        script.write_text(f"#!/bin/sh\nexit {exit_code}\n", "utf-8")
        script.chmod(0o755)


def _run(tmp_path: Path, uv_exit: int, ref: str = "main") -> subprocess.CompletedProcess[str]:
    repo = _source_repo(tmp_path)
    root = tmp_path / "manager-root"
    root.mkdir()
    (root / "config.toml").write_text(
        f'[source]\nrepo = "{repo.as_posix()}"\nref = "{ref}"\n', "utf-8"
    )
    fake_bin = tmp_path / "fake-bin"
    _fake_uv(fake_bin, uv_exit)
    env = dict(os.environ)
    env["WORKTREE_MANAGER_ROOT"] = str(root)
    env["USERPROFILE"] = str(tmp_path / "profile")
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(BOOTSTRAP), "--version"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )


def test_failed_uv_fails_the_bootstrap(tmp_path):
    result = _run(tmp_path, uv_exit=3)
    assert result.returncode != 0
    assert "self-install failed (uv exit 3)" in result.stdout + result.stderr


def test_successful_uv_succeeds(tmp_path):
    result = _run(tmp_path, uv_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_git_fetch_fails_the_bootstrap(tmp_path):
    result = _run(tmp_path, uv_exit=0, ref="no-such-branch")
    assert result.returncode != 0
    assert "git clone of" in result.stdout + result.stderr
