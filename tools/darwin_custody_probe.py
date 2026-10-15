"""Validation-only probe; not a construction backend or installation prerequisite."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import uuid


class UniqueInfo(ctypes.Structure):
    _fields_ = [
        ("uuid", ctypes.c_ubyte * 16),
        ("unique_id", ctypes.c_uint64),
        ("parent_unique_id", ctypes.c_uint64),
        ("idversion", ctypes.c_int32),
        ("parent_idversion", ctypes.c_int32),
        ("reserved", ctypes.c_uint64 * 2),
    ]


class CoalitionInfo(ctypes.Structure):
    _fields_ = [("ids", ctypes.c_uint64 * 2), ("reserved", ctypes.c_uint64 * 3)]


def libproc():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_signal_with_audittoken.restype = ctypes.c_int
    return library


def info(pid: int) -> dict:
    library = libproc()
    identity = UniqueInfo()
    coalition = CoalitionInfo()
    for flavor, target in ((17, identity), (20, coalition)):
        ctypes.set_errno(0)
        size = library.proc_pidinfo(pid, flavor, 0, ctypes.byref(target), ctypes.sizeof(target))
        if size != ctypes.sizeof(target):
            raise OSError(ctypes.get_errno(), f"proc_pidinfo flavor {flavor} returned {size}")
    return {
        "pid": pid, "unique_id": identity.unique_id, "idversion": identity.idversion,
        "parent_unique_id": identity.parent_unique_id, "coalitions": list(coalition.ids),
    }


def send(identity: dict, number: int, *, stale: bool = False) -> int:
    token = (ctypes.c_uint32 * 8)()
    token[5] = identity["pid"]
    token[7] = identity["idversion"] + (1 if stale else 0)
    return libproc().proc_signal_with_audittoken(ctypes.byref(token), number)


def wait(predicate, description: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.02)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def launchctl(*arguments: str) -> None:
    result = subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise RuntimeError(
            f"launchctl {arguments[0]} failed ({result.returncode}): "
            f"{result.stdout}{result.stderr}"
        )


def writer(base: Path) -> None:
    os.setsid()
    write_json(base / "writer.json", info(os.getpid()))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with (base / "writes").open("a", encoding="utf-8") as stream:
            stream.write("owned\n")
        time.sleep(0.01)


def guardian(base: Path) -> None:
    write_json(base / "guardian.json", info(os.getpid()))
    child = os.fork()
    if child == 0:
        grandchild = os.fork()
        if grandchild == 0:
            writer(base)
        os._exit(0)
    os.waitpid(child, 0)
    write_json(base / "intermediate-retired.json", {"retired": True})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (base / "release").exists():
        time.sleep(0.02)


def main() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("This capability probe requires an actual Darwin kernel")
    if len(sys.argv) == 3 and sys.argv[1] == "--guardian":
        guardian(Path(sys.argv[2]))
        return
    if os.geteuid() == 0:
        raise RuntimeError("Probe must establish ordinary-user capability, not root authority")
    with tempfile.TemporaryDirectory(prefix="darwin-custody-probe-") as directory:
        base = Path(directory)
        label = f"dev.copilot-extensions.custody-probe.{uuid.uuid4().hex}"
        plist = base / "job.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": [
                sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                "--guardian", str(base),
            ],
            "RunAtLoad": True, "KeepAlive": False, "ProcessType": "Background",
            "StandardOutPath": str(base / "out"), "StandardErrorPath": str(base / "err"),
        }))
        gui = f"gui/{os.getuid()}"
        domain_probe = subprocess.run(
            ["launchctl", "print", gui], capture_output=True, text=True, timeout=10,
        )
        target = gui if domain_probe.returncode == 0 else f"user/{os.getuid()}"
        print(json.dumps({"selected_domain_kind": target.split("/")[0]}), flush=True)
        loaded = False
        owned = []
        try:
            launchctl("bootstrap", target, str(plist))
            loaded = True
            wait(
                lambda: (base / "writer.json").exists()
                and (base / "intermediate-retired.json").exists()
                and (base / "writes").exists(),
                "launchd helper did not establish the detached, reparented writer",
            )
            root = json.loads((base / "guardian.json").read_text(encoding="utf-8"))
            descendant = json.loads((base / "writer.json").read_text(encoding="utf-8"))
            owned.extend((descendant, root))
            caller = info(os.getpid())
            assert root["coalitions"][0] > 0
            assert root["coalitions"][0] != caller["coalitions"][0], (
                "launchd job shares caller resource coalition; not an ownership boundary"
            )
            assert descendant["coalitions"] == root["coalitions"], (
                "double-fork/setsid escaped launchd coalition"
            )
            assert info(descendant["pid"])["coalitions"] == root["coalitions"]
            assert send(descendant, 0, stale=True) == errno.ESRCH, (
                "stale audit identity did not refuse"
            )
            assert send(descendant, 0) == 0
            assert send(descendant, signal.SIGSTOP) == 0
            time.sleep(0.1)
            before = (base / "writes").read_bytes()
            time.sleep(0.1)
            assert (base / "writes").read_bytes() == before
            assert send(descendant, signal.SIGKILL) == 0
            wait(
                lambda: send(descendant, 0) == errno.ESRCH,
                "kernel did not confirm exact descendant retirement",
            )
            (base / "release").touch()
            print(json.dumps({
                "ordinary_user": True, "isolated_resource_coalition": True,
                "double_fork_setsid_inherits": True,
                "stale_audit_identity_refused": True, "exact_retirement": True,
            }))
        finally:
            for filename in ("writer.json", "guardian.json"):
                path = base / filename
                if path.exists():
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                    if candidate not in owned:
                        owned.append(candidate)
            for identity in owned:
                outcome = send(identity, signal.SIGKILL)
                if outcome not in (0, errno.ESRCH):
                    raise OSError(outcome, "Exact probe participant cleanup failed")
            if loaded:
                launchctl("bootout", target, str(plist))
            for identity in owned:
                wait(
                    lambda identity=identity: send(identity, 0) == errno.ESRCH,
                    "Exact probe participant survived cleanup",
                )


if __name__ == "__main__":
    main()
