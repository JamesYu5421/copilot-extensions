"""Bounded construction commands with canonical descendant ownership."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import agent_procutil


log = logging.getLogger("installer-engine")


def _cleanup(terminate) -> None:
    # Never release construction while a writer may still run. A teardown
    # failure is visible and retains custody, not an excuse to drop the guard.
    cancelled = None
    retry = False
    while True:
        try:
            if retry:
                time.sleep(1)
            terminate()
        except (KeyboardInterrupt, SystemExit) as exc:
            cancelled = exc
            retry = False
            log.warning("Cancellation deferred until owned construction writers quiesce")
        except (OSError, TimeoutError, RuntimeError, ValueError) as exc:
            log.error("Owned construction tree has not quiesced; retaining custody: %s", exc)
            retry = True
        else:
            if cancelled is not None:
                raise cancelled
            return


class OwnedCommands:
    """Use only in a dedicated lifecycle process with no unrelated children."""

    def __init__(self, construction_descriptor: int) -> None:
        self.descriptor = construction_descriptor

    def run(
        self, argv: Sequence[str], *, cwd: Path, timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("Owned construction command requires a nonempty argv")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Owned construction command requires a finite positive timeout")
        os.fstat(self.descriptor)
        if os.name == "nt":
            return self._windows(list(argv), cwd, timeout, env)
        if sys.platform == "linux":
            return self._linux(list(argv), cwd, timeout, env)
        if sys.platform == "darwin":
            from installer_darwin import run

            return run(self.descriptor, list(argv), cwd, timeout, env)
        raise NotImplementedError("Owned construction requires a proven native descendant backend on this OS")

    def _windows(self, argv, cwd, timeout, env):
        process, job = agent_procutil.spawn_sync_in_kill_on_close_job(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            require_job=True, allow_breakaway=False,
            **agent_procutil.no_window_kwargs(),
        )
        if job is None:
            raise RuntimeError("Construction command has no owning Windows Job")
        with ThreadPoolExecutor(max_workers=2) as readers:
            assert process.stdout is not None and process.stderr is not None
            out = readers.submit(process.stdout.read)
            err = readers.submit(process.stderr.read)
            timed_out = None
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                timed_out = error
            finally:
                _cleanup(lambda: job.terminate_and_wait(5))
                process.wait(timeout=5)
                # Descendant-held pipe handles close only after Job teardown.
                stdout, stderr = out.result(timeout=5), err.result(timeout=5)
                process.stdout.close()
                process.stderr.close()
            if timed_out is not None:
                timed_out.output, timed_out.stderr = stdout, stderr
                raise timed_out
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        result.check_returncode()
        return result

    def _linux(self, argv, cwd, timeout, env):
        from zdd.singleton_linux import LinuxBackend

        children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
        if children.read_text().strip():
            raise RuntimeError("Construction owner has unrelated children; refusing tree-wide custody")
        backend = LinuxBackend()
        backend.claim_tree()
        read_fd, write_fd = os.pipe()
        guardian = None
        try:
            guardian = subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).with_name("installer_bootstrap.py")),
                 "--owned-command", str(read_fd), str(self.descriptor)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, pass_fds=(read_fd, self.descriptor), start_new_session=True,
            )
            os.close(read_fd)
            read_fd = -1
            request = json.dumps({"argv": argv, "cwd": str(cwd), "env": dict(env) if env is not None else None})
            try:
                stdout, stderr = guardian.communicate(request, timeout=timeout)
            except BaseException as error:
                os.close(write_fd)
                write_fd = -1
                try:
                    stdout, stderr = guardian.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    _cleanup(backend.cleanup)
                    stdout, stderr = guardian.communicate(timeout=5)
                if isinstance(error, subprocess.TimeoutExpired):
                    error.output, error.stderr = stdout, stderr
                raise
            if guardian.returncode != 0:
                # A killed guardian reparents its writers to this subreaper.
                _cleanup(backend.cleanup)
            result = subprocess.CompletedProcess(argv, guardian.returncode, stdout, stderr)
            result.check_returncode()
            return result
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            if guardian is not None and guardian.poll() is None:
                _cleanup(backend.cleanup)
                guardian.communicate(timeout=5)


def guardian_main(control_fd: int, construction_fd: int) -> int:
    """Keep the inherited flock description until every owned writer is gone."""
    if sys.platform != "linux":
        raise NotImplementedError("Construction guardian requires Linux pidfd/subreaper primitives")
    from zdd.singleton_linux import LinuxBackend

    os.fstat(construction_fd)
    backend = LinuxBackend()
    backend.claim_tree()
    request = json.load(sys.stdin)
    poller = select.poll()
    poller.register(control_fd, select.POLLIN | select.POLLHUP)
    process = None
    try:
        if poller.poll(0):
            raise RuntimeError("Construction owner exited before writer launch")
        process = subprocess.Popen(
            request["argv"], cwd=request["cwd"], env=request["env"],
            stdin=subprocess.DEVNULL,
        )
        while process.poll() is None:
            if poller.poll(50):
                raise RuntimeError("Construction owner exited; terminating its owned writer tree")
        return process.returncode
    finally:
        _cleanup(backend.cleanup)
        if process is not None:
            process.wait(timeout=5)
        os.close(control_fd)
        # Closing only: an explicit LOCK_UN here would release the parent's
        # shared open-file-description lock before it finishes publication.
        os.close(construction_fd)
