"""Enter self-install without a universal solve or preinstalled Manager."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PAYLOAD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAYLOAD / "src"))

from worktree_manager.installed_runtime import _owned_runner, runtime_python
from worktree_manager.self_install import (
    current_version, default_root, needs_install, payload_version, version_slot,
)
from worktree_manager.self_install_payload import _copy_payload


def main(args: list[str]) -> int:
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    version = payload_version(PAYLOAD)
    if version is None:
        print("Manager bootstrap failed: payload version is missing", file=sys.stderr)
        return 1
    if not needs_install(version, root):
        return _launch(root, args)
    runner = Path(tempfile.mkdtemp(prefix=".bootstrap.", dir=root))
    slot = runner / "payload"
    try:
        _copy_payload(PAYLOAD, slot)
        result = _owned_runner(
            [str(runtime_python(slot)), "-I", "-B", "-X", "utf8", "-m",
             "worktree_manager", "self-install", "--apply"],
            cwd=slot, timeout=600,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            return result.returncode
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Manager bootstrap failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(runner)
    return _launch(root, args)


def _launch(root: Path, args: list[str]) -> int:
    version = current_version(root)
    if not version:
        print("Manager bootstrap failed: no installed version was selected", file=sys.stderr)
        return 1
    python = runtime_python(version_slot(version, root))
    # headless-guard: allow interactive-bootstrap - retain an existing interactive console.
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and not sys.stdout.isatty() else 0
    return subprocess.run(
        [str(python), "-I", "-B", "-X", "utf8", "-m", "worktree_manager", *args],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, creationflags=flags,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
