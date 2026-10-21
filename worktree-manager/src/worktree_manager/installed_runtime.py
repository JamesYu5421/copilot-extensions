"""Platform-local construction, health and launch for a standalone payload."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME_DIR = ".runtime"
RUNTIME_FORMAT = "platform-runtime-v2"
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
            environment.name == RUNTIME_DIR
            and (environment / "pyvenv.cfg").is_file()
            and (environment.parent / "pyproject.toml").is_file()
        ):
            return environment.parent
    raise RuntimeError("Cannot locate this Manager's standalone payload directory")


def runtime_python(slot: Path) -> Path:
    return slot.resolve() / RUNTIME_DIR / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )


def process_library() -> Path:
    root = payload_root()
    library = root / "libs" / "agent-procutil" / "src"
    if not library.is_dir() and root == root.parent / "worktree-manager":
        library = root.parent / "libs" / "agent-procutil" / "src"
    if not (library / "agent_procutil" / "__init__.py").is_file():
        raise RuntimeError("Manager's owned process helper is missing")
    return library


def _owned_runner(
    argv: list[str], *, cwd: Path, timeout: float,
) -> subprocess.CompletedProcess[str]:
    library = process_library()
    # The bootstrap has not installed dependencies yet. Use this executing
    # payload's stdlib-only helper, never code from the fetched candidate.
    before = sys.path.copy()
    try:
        sys.path.insert(0, str(library))
        if os.name == "nt":
            from agent_procutil.owned_process import OwnedCleanupError, run_owned
        else:
            from .engine_process import run
    finally:
        sys.path[:] = before
    if os.name != "nt":
        # Reuse the Manager's retained POSIX owner and contained-test suppression.
        # A file drains stderr without blocking its completion-receipt channel.
        with tempfile.TemporaryFile() as errors:
            env = dict(os.environ)
            env.pop("PYTHONHOME", None)
            env.pop("UV_INTERNAL__PYTHONHOME", None)
            result = run(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", timeout=timeout,
            )
            errors.seek(0)
            return subprocess.CompletedProcess(
                argv, result.returncode, result.stdout, errors.read().decode("utf-8"),
            )
    try:
        return run_owned(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", _CHILD, str(cwd), *argv],
            timeout=timeout,
        )
    except OwnedCleanupError as error:
        from .engine_posix_process import CleanupError

        raise CleanupError(
            "Windows construction cleanup is unconfirmed; retain its filesystem paths",
            process=error.process, job=error.job,
        ) from error


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
    slot = slot.resolve()
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Cannot construct the Manager runtime: uv is not available")
    policy = payload_root() / "scripts" / "installer_dependency_policy.py"
    if not policy.is_file() or not policy.with_name("installer_dependency_config.py").is_file():
        raise RuntimeError("Cannot construct the Manager runtime: dependency policy is missing")
    _run(
        [uv, "venv", "--python", sys.executable, str(slot / RUNTIME_DIR)],
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
    slot = slot.resolve()
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
            and Path(observed["prefix"]).resolve() == (slot / RUNTIME_DIR).resolve()
            and Path(observed["module"]).resolve().is_relative_to((slot / RUNTIME_DIR).resolve())
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
        'slot="$root/versions/$ver"\n'
        'if [ -f "$slot/.install-complete" ] && [ ! -s "$slot/.install-complete" ]; then\n'
        '  if [ -f "$slot/pyproject.toml" ] && [ -f "$slot/src/worktree_manager/__init__.py" ]; then\n'
        '    exec uv run --quiet --project "$slot" -m worktree_manager "$@"\n'
        '  fi\n'
        '  echo "Manager legacy payload is incomplete; re-run the supported bootstrap to repair it." >&2\n'
        '  exit 1\n'
        'fi\n'
        f'python="$slot/{RUNTIME_DIR}/{interpreter}"\n'
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
        'set "SLOT=%WORKTREE_MANAGER_ROOT%\\versions\\%VER%"\r\n'
        'if exist "%SLOT%\\.install-complete" (\r\n'
        '  for %%F in ("%SLOT%\\.install-complete") do if %%~zF EQU 0 goto legacy\r\n'
        ')\r\n'
        f'set "PYTHON=%SLOT%\\{RUNTIME_DIR}\\Scripts\\python.exe"\r\n'
        'if not exist "%PYTHON%" (\r\n'
        '  echo Manager runtime is missing; re-run the supported bootstrap to repair it. 1>&2\r\n'
        '  exit /b 1\r\n'
        ')\r\n'
        '"%PYTHON%" -I -B -X utf8 -m worktree_manager %*\r\n'
        "exit /b %errorlevel%\r\n"
        ":legacy\r\n"
        'if not exist "%SLOT%\\pyproject.toml" goto missing_legacy\r\n'
        'if not exist "%SLOT%\\src\\worktree_manager\\__init__.py" goto missing_legacy\r\n'
        'uv run --quiet --project "%SLOT%" -m worktree_manager %*\r\n'
        "exit /b %errorlevel%\r\n"
        ":missing_legacy\r\n"
        "echo Manager legacy payload is incomplete; re-run the supported bootstrap to repair it. 1>&2\r\n"
        "exit /b 1\r\n"
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
        '$completion = Join-Path $slot ".install-complete"\n'
        "if ((Test-Path -LiteralPath $completion -PathType Leaf) -and (Get-Item -LiteralPath $completion).Length -eq 0) {\n"
        '  if ((Test-Path -LiteralPath (Join-Path $slot "pyproject.toml") -PathType Leaf) -and '
        '(Test-Path -LiteralPath (Join-Path $slot "src\\worktree_manager\\__init__.py") -PathType Leaf)) {\n'
        "    & uv run --quiet --project $slot -m worktree_manager @args\n"
        "    exit $LASTEXITCODE\n"
        "  }\n"
        "  [Console]::Error.WriteLine('Manager legacy payload is incomplete; re-run the supported bootstrap to repair it.')\n"
        "  exit 1\n"
        "}\n"
        f'$python = Join-Path $slot "{RUNTIME_DIR}\\Scripts\\python.exe"\n'
        "if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {\n"
        "  [Console]::Error.WriteLine('Manager runtime is missing; re-run the supported bootstrap to repair it.')\n"
        "  exit 1\n"
        "}\n"
        "& $python -I -B -X utf8 -m worktree_manager @args\n"
        "exit $LASTEXITCODE\n"
    )
