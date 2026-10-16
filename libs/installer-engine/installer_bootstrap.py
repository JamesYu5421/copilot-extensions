"""Import the Python installer engine from its own canonical or shipped bytes."""
from __future__ import annotations

from pathlib import Path
import os
import sys


def _import_io_path(value: str) -> str:
    if os.name != "nt" or not os.path.isabs(value) or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def bootstrap() -> None:
    here = Path(__file__).resolve().parent
    if (here / "versioned_runtime.py").is_file() and (here / "agent_procutil.py").is_file():
        roots = [here]
    elif here.name == "installer-engine" and here.parent.name == "libs":
        roots = [
            here, here.parent / "versioned-runtime",
            here.parent / "agent-procutil" / "src",
            here.parent / "zdd" / "src",
            here.parent / "single-instance-lease" / "src",
        ]
    else:
        raise RuntimeError("Installer-engine bootstrap resources are incomplete")
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Installer-engine bootstrap dependency is unavailable: {root}")
    sys.path[:0] = [str(root) for root in roots]
    # Prefix only this interpreter's existing import roots. Python 3.10's
    # file loader can otherwise lose long installed module names on Windows.
    sys.path[:] = [_import_io_path(value) for value in sys.path]


if __name__ == "__main__":
    bootstrap()
    if len(sys.argv) > 1 and sys.argv[1] == "--owned-command":
        from installer_processes import guardian_main
        raise SystemExit(guardian_main(*map(int, sys.argv[2:])))
    if len(sys.argv) > 1 and sys.argv[1] == "--owned-darwin":
        from installer_darwin import guardian_main
        raise SystemExit(guardian_main(*sys.argv[2:]))
    if len(sys.argv) > 2 and sys.argv[1] == "--native-driver":
        import runpy

        driver = Path(sys.argv[2]).resolve(strict=True)
        sys.argv = [str(driver), *sys.argv[3:]]
        runpy.run_path(str(driver), run_name="__main__")
    else:
        raise SystemExit("Expected an owned command or an adopter's native lifecycle driver")
