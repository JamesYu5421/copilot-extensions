"""Native fetch/extraction failures must not reach runtime construction."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native PowerShell bootstrap contract")


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("step", ["clone", "fetch", "checkout", "tar"])
def test_failed_native_fetch_stops_before_construction(tmp_path, shell, step):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"native {shell} is unavailable")
    root = tmp_path / "manager root"
    root.mkdir()
    marker = root / "current-version"
    marker.write_text("retained", encoding="utf-8")
    (root / "config.toml").write_text(
        '[source]\nrepo = "https://github.com/example/fixture.git"\nref = "main"\n',
        encoding="utf-8",
    )
    (root / "staging" / "worktree-manager").mkdir(parents=True)
    if step in {"fetch", "checkout"}:
        (root / "staging" / ".git").mkdir()
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    driver = tmp_path / "native_fixture.py"
    driver.write_text(
        "import os,sys\n"
        "from pathlib import Path\n"
        "root=Path(os.environ['WORKTREE_MANAGER_ROOT'])\n"
        "verb=next(name for name in ('clone','fetch','checkout','tar') if name in sys.argv[1:])\n"
        "if verb!='tar':\n"
        "    assert 'core.longpaths=true' in sys.argv[1:]\n"
        "target=(root/'staging'/'extract'/'fixture'/'worktree-manager') if verb=='tar' "
        "else root/'staging'/'worktree-manager'\n"
        "target.mkdir(parents=True,exist_ok=True)\n"
        "if verb==os.environ['MANAGER_TEST_FAIL_STEP']:\n"
        "    print('fixture native '+verb+' failure',file=sys.stderr)\n"
        "    raise SystemExit(17)\n",
        encoding="utf-8",
    )
    if step != "tar":
        (binary / "git.cmd").write_bytes(
            b'@echo off\r\n"%MANAGER_TEST_PYTHON%" "%MANAGER_TEST_DRIVER%" %*\r\n'
            b"exit /b %errorlevel%\r\n"
        )
    else:
        (binary / "tar.cmd").write_bytes(
            b'@echo off\r\n"%MANAGER_TEST_PYTHON%" "%MANAGER_TEST_DRIVER%" tar %*\r\n'
            b"exit /b %errorlevel%\r\n"
        )
    (binary / "uv.cmd").write_bytes(
        b'@echo off\r\necho invoked>"%WORKTREE_MANAGER_ROOT%\\builder-ran"\r\nexit /b 0\r\n'
    )
    wrapper = tmp_path / "invoke.ps1"
    wrapper.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "if ((Get-Command uv).Source -ne (Join-Path $env:MANAGER_TEST_BIN 'uv.cmd')) {\n"
        "  throw 'fixture uv was not selected; refusing bootstrap'\n"
        "}\n"
        "if ($env:MANAGER_TEST_FAIL_STEP -eq 'tar') {\n"
        "  if ((Get-Command git -ErrorAction SilentlyContinue) -or "
        "(Get-Command winget -ErrorAction SilentlyContinue)) { throw 'fixture prerequisite isolation failed' }\n"
        "  if ((Get-Command tar).Source -ne (Join-Path $env:MANAGER_TEST_BIN 'tar.cmd')) { throw 'fixture tar was not selected' }\n"
        "  function Invoke-WebRequest {\n"
        "    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)\n"
        "    [IO.File]::WriteAllText($OutFile, 'fixture')\n"
        "  }\n"
        "} elseif ((Get-Command git).Source -ne (Join-Path $env:MANAGER_TEST_BIN 'git.cmd')) {\n"
        "  throw 'fixture git was not selected'\n"
        "}\n"
        "& $env:MANAGER_TEST_BOOTSTRAP --version\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    env = dict(
        os.environ, PATH=str(binary), PATHEXT=".CMD;.EXE;.BAT;.COM", WORKTREE_MANAGER_ROOT=str(root),
        MANAGER_TEST_FAIL_STEP=step, MANAGER_TEST_DRIVER=str(driver),
        MANAGER_TEST_BIN=str(binary),
        MANAGER_TEST_PYTHON=sys.executable,
        MANAGER_TEST_BOOTSTRAP=str(Path(__file__).resolve().parents[1] / "bootstrap.ps1"),
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-File", str(wrapper)], env=env, cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0, result.stdout
    assert "fixture native " + step + " failure" in result.stderr
    assert "exit 17" in result.stderr
    assert not (root / "builder-ran").exists()
    assert marker.read_text(encoding="utf-8") == "retained"
