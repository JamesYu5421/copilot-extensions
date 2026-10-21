"""Request a registered construction branch and await synchronous group retirement."""

from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile

from . import engine_posix_broker as broker
from .engine_posix_broker import CleanupError


def _complete(connection, argv, timeout):
    wait = None if timeout is None else timeout + broker.CLEANUP_TIMEOUT + 1
    ready, _, _ = select.select([connection], [], [], wait)
    if not ready:
        raise subprocess.TimeoutExpired(argv, timeout)
    connection.settimeout(broker.CLEANUP_TIMEOUT)
    reply, _ = broker.receive_frame(connection)
    _require_cleanup_receipt(reply, connection)
    return reply


def _require_cleanup_receipt(reply, connection):
    if not isinstance(reply, dict) or reply.get("cleaned") is not True:
        raise CleanupError(
            "construction cleanup receipt is missing or invalid",
            connection=connection,
        )


def _acknowledge(connection):
    try:
        connection.settimeout(3 * broker.CLEANUP_TIMEOUT)
        try:
            connection.sendall(b"C")
        except BrokenPipeError:
            # Completion can close the peer after queuing its cleanup receipt.
            pass
        reply, _ = broker.receive_frame(connection)
    except (OSError, ValueError) as error:
        raise CleanupError(
            "construction cleanup is unconfirmed; retain its filesystem paths",
            connection=connection,
        ) from error
    _require_cleanup_receipt(reply, connection)


def run(argv: list[str], *, inherited=False, **kwargs) -> subprocess.CompletedProcess:
    timeout = kwargs.pop("timeout")
    env = kwargs["env"]
    process = None
    directory = None
    connection = None
    confirmed = False
    sent = False
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            if inherited:
                if not os.environ.get(broker.ENDPOINT_ENV) or not os.environ.get(broker.TOKEN_ENV):
                    raise RuntimeError(
                        "inherited construction broker context is incomplete; run the supported "
                        "bootstrap from a shell outside the older Manager process"
                    )
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(broker.CLEANUP_TIMEOUT)
                connection.connect(os.environ[broker.ENDPOINT_ENV])
                parent = os.environ[broker.TOKEN_ENV]
            else:
                directory = Path(tempfile.mkdtemp(prefix="wm-"))
                connection, child = socket.socketpair()
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-B", "-X", "utf8",
                         str(Path(broker.__file__).resolve()), str(child.fileno()),
                         str(directory / "broker")],
                        pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                finally:
                    child.close()
                parent = None
            stderr = kwargs.get("stderr")
            descriptor = errors.fileno() if stderr in (None, subprocess.PIPE, subprocess.DEVNULL) else (
                stderr if isinstance(stderr, int) else stderr.fileno()
            )
            sent = True
            try:
                broker.send_frame(connection, {
                    "argv": argv, "env": env,
                    "cwd": str(kwargs["cwd"]) if kwargs.get("cwd") else None,
                    "timeout": timeout, "parent": parent,
                }, (output.fileno(), descriptor))
                reply = _complete(connection, argv, timeout)
            except BaseException:
                _acknowledge(connection)
                confirmed = True
                raise
            confirmed = True
            if reply.get("error") == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if "error" in reply:
                raise OSError(reply["error"])
            if type(reply.get("returncode")) is not int:
                raise OSError("construction owner returned an invalid completion receipt")
            output.seek(0)
            stdout = output.read()
            errors.seek(0)
            stderr = errors.read() if kwargs.get("stderr") == subprocess.PIPE else None
            if kwargs.get("text"):
                encoding = kwargs.get("encoding") or "utf-8"
                stdout = stdout.decode(encoding, errors="replace")
                if stderr is not None:
                    stderr = stderr.decode(encoding, errors="replace")
            return subprocess.CompletedProcess(argv, reply["returncode"], stdout, stderr)
        finally:
            if connection is not None and (confirmed or not sent):
                connection.close()
            if process is not None:
                if sent and not confirmed:
                    raise CleanupError(
                        f"construction broker {process.pid} custody retained at {directory}; "
                        "do not remove candidate paths",
                        process=process, connection=connection, directory=directory,
                    )
                try:
                    process.wait(timeout=broker.CLEANUP_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise CleanupError(
                        f"construction broker {process.pid} did not retire; retained {directory}",
                        process=process, directory=directory,
                    ) from error
            if directory is not None:
                shutil.rmtree(directory)
