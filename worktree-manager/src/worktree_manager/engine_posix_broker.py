"""Finite POSIX construction broker, outside the groups whose leaders it retains."""

from __future__ import annotations

import array
import json
import math
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

ENDPOINT_ENV = "WORKTREE_MANAGER_ENGINE_BROKER"
TOKEN_ENV = "WORKTREE_MANAGER_ENGINE_BRANCH"
GROUP_ENV = "WORKTREE_MANAGER_ENGINE_OWNS_PROCESS_GROUP"
CLEANUP_TIMEOUT = 5
_linux_confirm_group = None
_MAX_FRAME = 16 * 1024 * 1024
_OWNER = """
import json,os,signal,subprocess,sys,threading
def disconnected():
    try:
        sys.stdin.buffer.read(1)
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)
try:
    if sys.stdin.buffer.read(1) == b'G':
        threading.Thread(target=disconnected, daemon=True).start()
        try:
            child = subprocess.Popen(
                json.loads(sys.argv[1]), stdin=subprocess.DEVNULL,
                stdout=int(sys.argv[2]), stderr=int(sys.argv[3]),
            )
        except OSError as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
        else:
            print(json.dumps({"returncode": child.wait()}), flush=True)
        threading.Event().wait()
finally:
    os.killpg(os.getpgrp(), signal.SIGKILL)
"""


class CleanupError(RuntimeError):
    """No cleanup acknowledgement: retain the broker and affected filesystem paths."""

    def __init__(self, message, *, process=None, connection=None, directory=None, job=None):
        super().__init__(message)
        self.process = process
        self.connection = connection
        self.directory = directory
        self.job = job


def send_frame(connection, value, descriptors=()):
    data = json.dumps(value).encode("utf-8")
    if len(data) > _MAX_FRAME:
        raise ValueError("construction broker frame is too large")
    data = struct.pack("!I", len(data)) + data
    if descriptors:
        sent = connection.sendmsg(
            [data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))],
        )
        connection.sendall(data[sent:])
    else:
        connection.sendall(data)


def receive_frame(connection, *, descriptors=False, descriptor_count=2):
    received = []
    try:
        if descriptors:
            header, ancillary, flags, _ = connection.recvmsg(
                4, socket.CMSG_SPACE(array.array("i").itemsize * descriptor_count),
            )
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[:len(data) - len(data) % values.itemsize])
                    received.extend(values)
            if flags & socket.MSG_CTRUNC or len(received) != descriptor_count:
                raise OSError("construction broker output descriptors are incomplete")
        else:
            header = connection.recv(4)
        while len(header) < 4:
            chunk = connection.recv(4 - len(header))
            if not chunk:
                raise OSError("construction broker closed before acknowledging cleanup")
            header += chunk
        size = struct.unpack("!I", header)[0]
        if size > _MAX_FRAME:
            raise ValueError("construction broker frame is too large")
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(min(65536, size - len(data)))
            if not chunk:
                raise OSError("construction broker completion is truncated")
            data.extend(chunk)
        return json.loads(data), received
    except BaseException:
        for descriptor in received:
            os.close(descriptor)
        raise


@dataclass
class Branch:
    process: subprocess.Popen
    connection: socket.socket
    parent: str | None
    deadline: float | None
    receipt: bytes = b""
    reaped: bool = False
    signalled: bool = False


def retire(branch):
    # The broker alone owns this Popen. Never poll/wait before the one group
    # signal: the unreaped leader pins the group identity through that signal.
    if not branch.reaped:
        if not branch.signalled:
            try:
                os.killpg(branch.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            branch.signalled = True
        if _linux_confirm_group is not None:
            try:
                _linux_confirm_group(branch.process)
            except (OSError, RuntimeError) as error:
                raise CleanupError(
                    "construction process group still has unconfirmed live members",
                    process=branch.process,
                ) from error
        branch.process.communicate(timeout=CLEANUP_TIMEOUT)
        branch.reaped = True
    if _linux_confirm_group is not None:
        return
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    while True:
        try:
            # Observation only after reaping: never signal a potentially reused ID.
            os.killpg(branch.process.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise CleanupError("construction process group has not disappeared")
        time.sleep(.01)


class Broker:
    def __init__(self, listener, root, *, endpoint=None):
        self.listener, self.root = listener, root
        self.endpoint = endpoint
        self.branches = {}

    def launch(self, connection, request, descriptors):
        if not isinstance(request, dict):
            raise ValueError("construction request must be an object")
        argv, env, timeout = request["argv"], request["env"], request["timeout"]
        if (
            not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not isinstance(env, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                or not key or "=" in key or "\0" in key or "\0" in value
                for key, value in env.items()
            )
            or timeout is not None and (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0
            )
            or request["cwd"] is not None and (
                not isinstance(request["cwd"], str) or not os.path.isabs(request["cwd"])
                or "\0" in request["cwd"]
            )
        ):
            raise ValueError("construction request has invalid process arguments")
        parent = request["parent"]
        if parent is not None and (
            not isinstance(parent, str) or parent not in self.branches
        ):
            raise OSError("construction parent branch is no longer owned")
        token = uuid.uuid4().hex
        env = dict(request["env"])
        env.update({
            ENDPOINT_ENV: self.endpoint or self.listener.getsockname(),
            TOKEN_ENV: token, GROUP_ENV: "1",
        })
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", _OWNER,
             json.dumps(request["argv"]), *map(str, descriptors)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=tuple(descriptors),
            env=env, cwd=request["cwd"],
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        branch = Branch(process, connection, parent, deadline)
        self.branches[token] = branch
        # Registration precedes permission to execute, including the cancellation race.
        process.stdin.write(b"G")
        process.stdin.flush()

    def finish(self, token, reply):
        branch = self.branches[token]
        for child, value in list(self.branches.items()):
            if value.parent == token:
                self.finish(child, {"error": "cancelled"})
        retire(branch)
        del self.branches[token]
        try:
            send_frame(branch.connection, {**reply, "cleaned": True})
        except OSError:
            pass
        branch.connection.close()

    def accept(self, connection):
        descriptors = []
        try:
            connection.settimeout(CLEANUP_TIMEOUT)
            request, descriptors = receive_frame(connection, descriptors=True)
            self.launch(connection, request, descriptors)
            connection.settimeout(None)
        except (OSError, ValueError, KeyError, TypeError) as error:
            registered = next((
                token for token, branch in self.branches.items()
                if branch.connection is connection
            ), None)
            if registered is not None:
                self.finish(registered, {"error": str(error)})
            else:
                try:
                    send_frame(connection, {"error": str(error), "cleaned": True})
                finally:
                    connection.close()
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def run(self):
        self.accept(self.root)
        while self.branches:
            streams = [self.listener]
            for branch in self.branches.values():
                streams.extend([branch.connection, branch.process.stdout])
            ready, _, _ = select.select(streams, [], [], .05)
            # Cancellation wins over queued registration and command completion.
            for token, branch in list(self.branches.items()):
                if token not in self.branches:
                    continue
                if branch.connection in ready:
                    branch.connection.recv(1)
                    self.finish(token, {"error": "cancelled"})
                elif branch.deadline is not None and time.monotonic() >= branch.deadline:
                    self.finish(token, {"error": "timeout"})
                elif branch.process.stdout in ready:
                    chunk = os.read(branch.process.stdout.fileno(), 4096)
                    branch.receipt += chunk
                    if b"\n" in branch.receipt or not chunk:
                        try:
                            reply = json.loads(branch.receipt)
                        except ValueError:
                            reply = {"error": "construction owner returned no completion receipt"}
                        self.finish(token, reply)
            if self.listener in ready and self.branches:
                connection, _ = self.listener.accept()
                self.accept(connection)

    def close(self):
        for token in list(self.branches):
            if token in self.branches:
                self.finish(token, {"error": "cancelled"})


def main():
    global _linux_confirm_group
    if sys.argv[1] == "--connect":
        with socket.socket(fileno=int(sys.argv[2])) as channel:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(CLEANUP_TIMEOUT)
                connection.connect(sys.argv[3])
                send_frame(channel, {"connected": True}, (connection.fileno(),))
        return
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    with socket.socket(fileno=int(sys.argv[1])) as root:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            try:
                if sys.platform.startswith("linux"):
                    sys.path.insert(0, sys.argv[3])
                    from agent_procutil._caller_owned import _confirm_group

                    _linux_confirm_group = _confirm_group
                listener.bind(os.path.basename(sys.argv[2]))
                listener.listen()
            except (OSError, ImportError) as error:
                send_frame(root, {"ready": False, "error": str(error)})
                raise SystemExit(1) from error
            broker = Broker(listener, root, endpoint=sys.argv[2])
            send_frame(root, {"ready": True})
            try:
                broker.run()
            finally:
                broker.close()


if __name__ == "__main__":
    main()
