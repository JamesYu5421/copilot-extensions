"""Darwin construction custody in a private, one-shot launchd coalition."""
from __future__ import annotations

import array
import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid


class _UniqueInfo(ctypes.Structure):
    _fields_ = [
        ("uuid", ctypes.c_ubyte * 16), ("unique_id", ctypes.c_uint64),
        ("parent_unique_id", ctypes.c_uint64), ("idversion", ctypes.c_uint32),
        ("parent_idversion", ctypes.c_uint32), ("reserved", ctypes.c_uint64 * 2),
    ]


class _CoalitionInfo(ctypes.Structure):
    _fields_ = [("ids", ctypes.c_uint64 * 2), ("reserved", ctypes.c_uint64 * 3)]


@dataclass(frozen=True)
class Identity:
    pid: int
    unique_id: int
    idversion: int
    coalition: int


class Kernel:
    """Never substitute PID/group signals for an atomic audit-token signal."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise NotImplementedError("Construction coalitions require Darwin")
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_signal_with_audittoken.restype = ctypes.c_int
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listallpids.restype = ctypes.c_int
        self.system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.system.coalition_info_resource_usage.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t,
        ]
        self.system.coalition_info_resource_usage.restype = ctypes.c_int

    def counts(self, coalition: int) -> tuple[int, int] | None:
        # XNU copies MIN(requested, sizeof(resource_usage)); the stable prefix
        # is tasks_started/tasks_exited, captured under the coalition lock.
        counters = (ctypes.c_uint64 * 2)(2**64 - 1, 2**64 - 1)
        ctypes.set_errno(0)
        result = self.system.coalition_info_resource_usage(
            coalition, ctypes.byref(counters), ctypes.sizeof(counters),
        )
        if result:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return None
            raise OSError(error, "Native construction coalition accounting is unavailable")
        started, exited = counters
        if started == 2**64 - 1 or exited > started:
            raise RuntimeError("Invalid native construction coalition accounting")
        return started, exited

    def _read(self, pid: int, flavor: int, target) -> None:
        ctypes.set_errno(0)
        size = ctypes.sizeof(target)
        count = self.lib.proc_pidinfo(pid, flavor, 0, ctypes.byref(target), size)
        if count != size:
            raise OSError(ctypes.get_errno(), f"Native process flavor {flavor} returned {count}/{size}")

    def identity(self, pid: int) -> Identity:
        unique, coalition = _UniqueInfo(), _CoalitionInfo()
        self._read(pid, 17, unique)
        self._read(pid, 20, coalition)
        confirm = _UniqueInfo()
        self._read(pid, 17, confirm)
        if unique.unique_id != confirm.unique_id:
            raise ProcessLookupError(errno.ESRCH, "Process identity changed during coalition lookup")
        if not unique.unique_id or not coalition.ids[0]:
            raise RuntimeError("Native process identity/coalition is unavailable")
        return Identity(pid, confirm.unique_id, confirm.idversion, coalition.ids[0])

    def status(self, identity: Identity) -> int | None:
        try:
            current = self.identity(identity.pid)
            if current.unique_id != identity.unique_id:
                return None
            bsd = ctypes.create_string_buffer(136)
            self._read(identity.pid, 3, bsd)
            confirm = self.identity(identity.pid)
            if confirm.unique_id != identity.unique_id:
                return None
            return ctypes.c_uint32.from_buffer(bsd, 4).value
        except ProcessLookupError:
            return None

    def send(self, identity: Identity, number: int) -> None:
        if number <= 0:
            raise ValueError("Audit-token signaling requires a positive signal")
        try:
            current = self.identity(identity.pid)
        except ProcessLookupError:
            return
        if current.unique_id != identity.unique_id:
            return
        if current.coalition != identity.coalition:
            raise RuntimeError("Construction participant changed coalition")
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = current.pid, current.idversion
        result = self.lib.proc_signal_with_audittoken(ctypes.byref(token), number)
        if result == errno.ESRCH:
            # Exec can change idversion without retiring the original process.
            if self.status(identity) not in (None, 5):
                raise RuntimeError("Construction audit identity changed; retrying exact cleanup")
        elif result:
            raise OSError(result, "Exact construction participant signal failed")

    def members(self, coalition: int) -> list[Identity]:
        capacity = self.lib.proc_listallpids(None, 0) + 64
        for _ in range(8):
            if capacity <= 64:
                raise RuntimeError("Native process enumeration is unavailable")
            pids = (ctypes.c_int * capacity)()
            count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
            if count <= 0:
                raise OSError(ctypes.get_errno(), "Native process enumeration failed")
            if count >= capacity:
                capacity *= 2
                continue
            result = []
            for pid in pids[:count]:
                if pid <= 0:
                    continue
                try:
                    candidate = self.identity(pid)
                except ProcessLookupError:
                    continue
                if candidate.coalition == coalition:
                    result.append(candidate)
            return result
        raise RuntimeError("Native process enumeration remained saturated")

    def quiesce(self, coalition: int, *, exclude: int | None = None) -> None:
        if coalition <= 0 or (self.identity(os.getpid()).coalition == coalition and exclude != os.getpid()):
            raise RuntimeError("Refusing to retire the caller's coalition")
        deadline = time.monotonic() + 10
        while True:
            counts = self.counts(coalition)
            excluded = int(exclude is not None)
            if counts is None:
                if exclude is not None:
                    raise RuntimeError("Guardian lost its own coalition accounting")
                return
            if counts[0] - counts[1] == excluded:
                return
            participants = [
                member for member in self.members(coalition)
                if member.pid != exclude and self.status(member) not in (None, 5)
            ]
            for member in participants:
                self.send(member, signal.SIGSTOP)
            before = self.counts(coalition)
            frozen = [
                member for member in self.members(coalition)
                if member.pid != exclude and self.status(member) not in (None, 5)
            ]
            stopped = all(self.status(member) in (None, 4, 5) for member in frozen)
            after = self.counts(coalition)
            # A PID census alone can miss a fast exiting parent's new child.
            # Stable kernel counters plus complete stopped membership close
            # that hole; only a kernel-empty coalition licenses return.
            if (
                before is not None and before == after
                and before[0] - before[1] == len(frozen) + excluded and stopped
            ):
                for member in frozen:
                    self.send(member, signal.SIGKILL)
            if time.monotonic() >= deadline:
                raise TimeoutError("Kernel has not confirmed construction coalition retirement")
            time.sleep(.01)


def _peer_identity(connection: socket.socket, kernel: Kernel) -> Identity:
    # Darwin SOL_LOCAL / LOCAL_PEERTOKEN: credentials and PID generation from
    # the kernel. CPython does not expose the BSD getpeereid() function.
    token = struct.unpack("8I", connection.getsockopt(0, 6, 32))
    identity = kernel.identity(token[5])
    if token[1] != os.getuid() or identity.idversion != token[7]:
        raise RuntimeError("Construction control peer credentials changed")
    return identity


def _deadline(connection: socket.socket, deadline: float | None) -> None:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Construction command exceeded its bounded budget")
        connection.settimeout(remaining)


def _send(connection: socket.socket, value: dict, deadline: float | None = None) -> None:
    data = json.dumps(value).encode("utf-8")
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Construction control message is too large")
    _deadline(connection, deadline)
    connection.sendall(struct.pack("!I", len(data)) + data)


def _receive(connection: socket.socket, deadline: float | None = None) -> dict:
    def read(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            _deadline(connection, deadline)
            chunk = connection.recv(size - len(result))
            if not chunk:
                raise EOFError("Construction control peer exited")
            result.extend(chunk)
        return bytes(result)

    size = struct.unpack("!I", read(4))[0]
    if size > 16 * 1024 * 1024:
        raise ValueError("Construction control message is too large")
    value = json.loads(read(size))
    if not isinstance(value, dict):
        raise ValueError("Invalid construction control message")
    return value


def _launchctl(*arguments: str, absent_ok: bool = False) -> None:
    result = subprocess.run(
        ["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode and not (absent_ok and result.returncode == errno.ESRCH):
        raise RuntimeError(
            f"Construction launchctl {arguments[0]} failed ({result.returncode}): {result.stderr.strip()}"
        )


def _streams(base: Path) -> tuple[str, str]:
    return (
        (base / "out").read_text(encoding="utf-8", errors="replace"),
        (base / "err").read_text(encoding="utf-8", errors="replace"),
    )


def run(descriptor: int, argv: list[str], cwd: Path, timeout: float, env) -> subprocess.CompletedProcess[str]:
    from installer_processes import _cleanup

    kernel = Kernel()
    owner = kernel.identity(os.getpid())
    if kernel.counts(owner.coalition) is None:
        raise RuntimeError("Construction owner has no coalition accounting")
    target = f"user/{os.getuid()}/dev.copilot-extensions.construction.{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="ie-") as directory:
        base = Path(directory)
        endpoint = base / "s"
        for name in ("out", "err"):
            (base / name).touch(mode=0o600)
        plist = base / "job.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": target.rsplit("/", 1)[1],
            "ProgramArguments": [
                sys.executable, "-I", "-B", str(Path(__file__).with_name("installer_bootstrap.py")),
                "--owned-darwin", str(endpoint), str(owner.pid), str(owner.unique_id), target,
            ],
            "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Background",
            "LimitLoadToSessionType": ["Background", "Aqua", "StandardIO"],
            "StandardOutPath": str(base / "guardian-out"), "StandardErrorPath": str(base / "guardian-err"),
        }))
        connection = None
        coalition = None
        started = time.monotonic()
        deadline = started + timeout
        result = None
        failure = None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            server.listen(1)
            try:
                _launchctl("bootstrap", target.rsplit("/", 1)[0], str(plist))
                _deadline(server, min(deadline, started + 15))
                connection, _ = server.accept()
                _deadline(connection, deadline)
                root = _peer_identity(connection, kernel)
                if root.coalition == owner.coalition:
                    raise RuntimeError("Construction guardian has no private user coalition")
                coalition = root.coalition
                counts = kernel.counts(coalition)
                if kernel.members(coalition) != [root] or counts is None or counts[0] - counts[1] != 1:
                    raise RuntimeError("Construction coalition is not exclusively the new guardian")
                _deadline(connection, deadline)
                sent = connection.sendmsg(
                    [b"F"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))],
                )
                if sent != 1 or _receive(connection, deadline) != {"custody": True}:
                    raise RuntimeError("Construction guardian did not acknowledge inherited lease custody")
                _send(connection, {
                    "argv": argv, "cwd": str(cwd), "env": dict(os.environ if env is None else env),
                }, deadline)
                response = _receive(connection, deadline)
                code = response.get("returncode")
                if type(code) is not int:
                    raise RuntimeError("Construction guardian returned no command result")
                result = code
            except BaseException as error:
                failure = error
            finally:
                if connection is not None:
                    connection.close()

                def retire():
                    if coalition is not None:
                        kernel.quiesce(coalition)
                    _launchctl("bootout", target, absent_ok=True)

                _cleanup(retire)
        stdout, stderr = _streams(base)
        if isinstance(failure, TimeoutError):
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from failure
        if isinstance(failure, EOFError) or (isinstance(failure, OSError) and failure.errno == errno.ENOTCONN):
            diagnostic = base / "guardian-err"
            detail = diagnostic.read_text(encoding="utf-8", errors="replace").strip() if diagnostic.exists() else ""
            raise RuntimeError(f"Construction guardian exited before completing its command: {detail}") from failure
        if failure is not None:
            raise failure
        completed = subprocess.CompletedProcess(argv, result, stdout, stderr)
        completed.check_returncode()
        return completed


def guardian_main(endpoint: str, owner_pid: str, owner_unique: str, target: str) -> int:
    from installer_processes import _cleanup

    kernel = Kernel()
    identity = kernel.identity(os.getpid())
    descriptor = None
    process = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.settimeout(15)
            connection.connect(endpoint)
            owner = _peer_identity(connection, kernel)
            if (
                owner.pid != int(owner_pid) or owner.unique_id != int(owner_unique)
                or owner.coalition == identity.coalition
            ):
                raise RuntimeError("Construction control peer does not match the original owner")
            message, ancillary, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize))
            descriptors = array.array("i")
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors.frombytes(data)
            if message != b"F" or flags & socket.MSG_CTRUNC or len(descriptors) != 1:
                for received in descriptors:
                    os.close(received)
                raise RuntimeError("Construction guardian received no exact lease descriptor")
            descriptor = descriptors[0]
            os.set_inheritable(descriptor, False)
            os.fstat(descriptor)
            counts = kernel.counts(identity.coalition)
            if (
                kernel.members(identity.coalition) != [identity]
                or counts is None or counts[0] - counts[1] != 1
            ):
                raise RuntimeError("Construction guardian coalition contains unexpected processes")
            _send(connection, {"custody": True})
            request = _receive(connection)
            connection.settimeout(None)
            base = Path(endpoint).parent
            with (base / "out").open("wb") as out, (base / "err").open("wb") as err:
                process = subprocess.Popen(
                    request["argv"], cwd=request["cwd"], env=request["env"],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True,
                )
                while process.poll() is None:
                    if select.select([connection], [], [], .05)[0]:
                        raise EOFError("Construction owner exited; retiring its writer coalition")
            _cleanup(lambda: kernel.quiesce(identity.coalition, exclude=os.getpid()))
            _send(connection, {"returncode": process.returncode})
            # The owner keeps its original descriptor through publication.
            # If it dies now, EOF still retires the one-shot registration.
            connection.recv(1)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            print(f"Construction guardian failed: {error}", file=sys.stderr, flush=True)
        finally:
            _cleanup(lambda: kernel.quiesce(identity.coalition, exclude=os.getpid()))
            if process is not None:
                process.wait(timeout=5)
            if descriptor is not None:
                # LOCK_UN would also unlock the live owner's original copy.
                os.close(descriptor)
    # All writers are gone before launchctl can kill this job's remaining self.
    os.execv("/bin/launchctl", ["/bin/launchctl", "bootout", target])
    return 1
