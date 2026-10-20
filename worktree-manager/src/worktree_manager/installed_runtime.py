"""Platform-local construction, health and launch for a standalone payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME_FORMAT = "platform-runtime-v1"
_CHILD = (
    "import os,subprocess,sys;os.chdir(sys.argv[1]);"
    "raise SystemExit(subprocess.run(sys.argv[2:],"
    "stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,"
    "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).returncode)"
)


def payload_root() -> Path:
    module = Path(__file__).resolve()
    source = module.parents[2]
    if (source / "src" / "worktree_manager" / "installed_runtime.py").resolve() == module:
        return source
    for environment in module.parents:
        if (
            environment.name == ".venv"
            and (environment / "pyvenv.cfg").is_file()
            and (environment.parent / "pyproject.toml").is_file()
        ):
            return environment.parent
    raise RuntimeError("Cannot locate this Manager's standalone payload directory")


def runtime_python(slot: Path) -> Path:
    return slot / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )


def _owned_runner(
    argv: list[str], *, cwd: Path, timeout: float,
) -> subprocess.CompletedProcess[str]:
    root = payload_root()
    library = root / "libs" / "agent-procutil" / "src"
    if not library.is_dir() and root == root.parent / "worktree-manager":
        library = root.parent / "libs" / "agent-procutil" / "src"
    if not (library / "agent_procutil" / "__init__.py").is_file():
        raise RuntimeError("Manager's owned process helper is missing")
    # The bootstrap has not installed dependencies yet. Use this executing
    # payload's stdlib-only helper, never code from the fetched candidate.
    before = sys.path.copy()
    try:
        sys.path.insert(0, str(library))
        if os.name == "nt":
            from agent_procutil.owned_process import run_owned
        else:
            from .engine_process import run
    finally:
        sys.path[:] = before
    if os.name != "nt":
        # Reuse the Manager's retained POSIX owner and contained-test suppression.
        # A file drains stderr without blocking its completion-receipt channel.
        with tempfile.TemporaryFile() as errors:
            result = run(
                argv, cwd=cwd, env=dict(os.environ), stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", timeout=timeout,
            )
            errors.seek(0)
            return subprocess.CompletedProcess(
                argv, result.returncode, result.stdout, errors.read().decode("utf-8"),
            )
    return run_owned(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", _CHILD, str(cwd), *argv],
        timeout=timeout,
    )


def _run(argv: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        result = _owned_runner(argv, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise RuntimeError(f"Manager runtime command failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Manager runtime command exited {result.returncode}: {detail}")
    return result


def build_runtime(slot: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Cannot construct the Manager runtime: uv is not available")
    policy = payload_root() / "scripts" / "installer_dependency_policy.py"
    if not policy.is_file() or not policy.with_name("installer_dependency_config.py").is_file():
        raise RuntimeError("Cannot construct the Manager runtime: dependency policy is missing")
    _run(
        [uv, "venv", "--python", sys.executable, str(slot / ".venv")],
        cwd=slot, timeout=120,
    )
    libraries = sorted(
        path for path in (slot / "libs").iterdir() if (path / "pyproject.toml").is_file()
    ) if (slot / "libs").is_dir() else []
    _run(
        [
            sys.executable, "-I", "-B", "-X", "utf8", str(policy), "--uv", uv,
            "--trusted-source-root", str(slot.resolve()), "--",
            "--python", str(runtime_python(slot)), "--reinstall", "--no-sources",
            "--no-editable", "--link-mode", "copy",
            *(str(path.resolve()) for path in libraries), str(slot.resolve()),
        ],
        cwd=slot, timeout=300,
    )


def probe_runtime(slot: Path, version: str) -> None:
    python = runtime_python(slot)
    if not python.is_file():
        raise RuntimeError(f"Manager runtime interpreter is missing: {python}")
    result = _run(
        [
            str(python), "-I", "-B", "-X", "utf8", "-c",
            "import json,sys,worktree_manager;"
            "print(json.dumps({'version':worktree_manager.__version__,"
            "'prefix':sys.prefix,'module':worktree_manager.__file__}))",
        ],
        cwd=slot, timeout=20,
    )
    try:
        observed = json.loads(result.stdout)
        valid = (
            observed["version"] == version
            and Path(observed["prefix"]).resolve() == (slot / ".venv").resolve()
            and Path(observed["module"]).resolve().is_relative_to((slot / ".venv").resolve())
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Manager runtime health returned invalid identity JSON") from exc
    if not valid:
        raise RuntimeError("Manager runtime health does not match the candidate environment/version")
    cli = _run(
        [str(python), "-I", "-B", "-X", "utf8", "-m", "worktree_manager", "--version"],
        cwd=slot, timeout=20,
    )
    if cli.stdout.strip() != f"worktree-manager {version}":
        raise RuntimeError("Manager CLI health does not report the expected version")


def sh_binstub() -> str:
    interpreter = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return (
        "#!/usr/bin/env bash\n"
        "# worktree-manager binstub (versioned) - resolves the current-version marker.\n"
        'set -euo pipefail\n'
        'root="${WORKTREE_MANAGER_ROOT:-$HOME/.worktree-manager}"\n'
        'ver="$(< "$root/current-version")"\n'
        'if [ -z "$ver" ]; then\n'
        '  echo "Manager version marker is empty; re-run the supported bootstrap to repair it." >&2\n'
        '  exit 1\n'
        'fi\n'
        f'python="$root/versions/$ver/.venv/{interpreter}"\n'
        'if [ ! -x "$python" ]; then\n'
        '  echo "Manager runtime is missing; re-run the supported bootstrap to repair it." >&2\n'
        '  exit 1\n'
        'fi\n'
        'exec "$python" -I -B -X utf8 -m worktree_manager "$@"\n'
    )


def cmd_binstub() -> str:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "PYTHONUTF8=1"\r\n'
        'if "%WORKTREE_MANAGER_ROOT%"=="" set "WORKTREE_MANAGER_ROOT=%USERPROFILE%\\.worktree-manager"\r\n'
        'set "VER="\r\n'
        'set /p VER=<"%WORKTREE_MANAGER_ROOT%\\current-version"\r\n'
        'if not defined VER (\r\n'
        '  echo Manager version marker is missing or empty; re-run the supported bootstrap to repair it. 1>&2\r\n'
        '  exit /b 1\r\n'
        ')\r\n'
        'set "PYTHON=%WORKTREE_MANAGER_ROOT%\\versions\\%VER%\\.venv\\Scripts\\python.exe"\r\n'
        'if not exist "%PYTHON%" (\r\n'
        '  echo Manager runtime is missing; re-run the supported bootstrap to repair it. 1>&2\r\n'
        '  exit /b 1\r\n'
        ')\r\n'
        '"%PYTHON%" -I -B -X utf8 -m worktree_manager %*\r\n'
        "exit /b %errorlevel%\r\n"
    )


def ps1_binstub() -> str:
    return (
        "# worktree-manager binstub (versioned) - resolves the current-version marker.\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$env:PYTHONUTF8 = '1'\n"
        '$root = if ($env:WORKTREE_MANAGER_ROOT) { $env:WORKTREE_MANAGER_ROOT } '
        'else { Join-Path $env:USERPROFILE ".worktree-manager" }\n'
        '$ver = ([string](Get-Content -LiteralPath (Join-Path $root "current-version") -Raw)).Trim()\n'
        "if (-not $ver) {\n"
        "  [Console]::Error.WriteLine('Manager version marker is empty; re-run the supported bootstrap to repair it.')\n"
        "  exit 1\n"
        "}\n"
        '$slot = Join-Path (Join-Path $root "versions") $ver\n'
        '$python = Join-Path $slot ".venv\\Scripts\\python.exe"\n'
        "if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {\n"
        "  [Console]::Error.WriteLine('Manager runtime is missing; re-run the supported bootstrap to repair it.')\n"
        "  exit 1\n"
        "}\n"
        "& $python -I -B -X utf8 -m worktree_manager @args\n"
        "exit $LASTEXITCODE\n"
    )
