"""Bootstrap the ordinary UV project run with shared mirror-age admission."""
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "scripts"))

from installer_dependency_policy import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--uv", "uv", "--run-project", str(PROJECT), "--", *sys.argv[1:]]))
