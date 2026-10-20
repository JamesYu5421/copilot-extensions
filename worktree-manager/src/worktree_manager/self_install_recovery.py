"""Preserve prior installation artifacts when recovery or publication fails."""

from __future__ import annotations

import logging
import stat
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def publish_installed_selection(root: Path, version: str) -> list[Path]:
    """Restore launchers and selection if publication fails under the install lease."""
    from . import self_install as si

    paths = [si.local_bin() / name for name in si._expected_binstub_contents()]
    previous = {}
    for path in [*paths, root / si.MARKER]:
        previous[path] = (
            (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        )
    try:
        stubs = si._deploy_binstubs()
        si._write_marker(root, version)
        return stubs
    except BaseException as failure:
        rollback_failures = []
        for path, snapshot in previous.items():
            temporary = path.with_name(f".{path.name}.restore.{uuid.uuid4().hex}")
            try:
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    body, mode = snapshot
                    if path.is_file() and (
                        path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                    ) == snapshot:
                        continue
                    temporary.write_bytes(body)
                    temporary.chmod(mode)
                    si._replace_with_retry(temporary, path)
            except OSError as exc:
                log.exception("Could not restore Manager publication artifact %s", path)
                rollback_failures.append(f"{path}: {exc}")
            finally:
                temporary.unlink(missing_ok=True)
        if rollback_failures:
            raise RuntimeError(
                f"Manager selection publication failed ({failure}); publication rollback failed: "
                + "; ".join(rollback_failures)
            ) from failure
        raise


def reap_stranded_cutover_passive(root: Path, slot: Path, mux_daemon_cutover) -> str | None:
    """Run under the caller's install lease; leave the recovery record intact."""
    try:
        try:
            slot.lstat()
        except FileNotFoundError:
            return None
        from zdd import breadcrumb

        record = breadcrumb.read_breadcrumb(mux_daemon_cutover.routing_dir(root))
        result = mux_daemon_cutover._reap_abandoned_passive(root, record)
        if not isinstance(result, dict) or result.get("refused"):
            reason = "native passive custody refused; version slot retained"
        else:
            candidate = (record or {}).get("new_pid")
            if type(candidate) is not int or candidate <= 0:
                return None
            if candidate not in mux_daemon_cutover._iter_mux_daemon_pids():
                return None
            reason = "passive daemon exit unconfirmed; version slot retained"
    except Exception as exc:
        reason = f"native passive recovery unavailable ({type(exc).__name__}); version slot retained"
    log.error(reason)
    return reason
