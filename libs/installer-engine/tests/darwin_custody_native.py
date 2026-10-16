"""Focused, stdlib-only native contract lane; no installer provisioning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))


WRITER = r"""
import json, os, sys, time
from pathlib import Path
base = Path(sys.argv[1])
assert Path.cwd() == base
assert sys.stdin.read() == ""
assert os.environ["CONSTRUCTION_TEST_ENV"] == "owned inheritance"
guardian = os.getppid()
child = os.fork()
if child == 0:
    for _ in range(16):
        if os.fork():
            os._exit(0)
    os.setsid()
    (base / "leaf").write_text(str(os.getpid()))
    while True:
        with (base / "writes").open("a") as stream:
            stream.write("owned\n")
        time.sleep(.005)
while not (base / "leaf").exists() or not (base / "writes").exists():
    time.sleep(.005)
pending = base / "participants.pending"
pending.write_text(json.dumps([guardian, os.getpid(), int((base / "leaf").read_text())]))
pending.replace(base / "participants")
print("owned output", flush=True)
print("owned error", file=sys.stderr, flush=True)
if sys.argv[2] == "success":
    raise SystemExit(0)
while True:
    time.sleep(.02)
"""


def wait(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        time.sleep(.01)


def owner(base: Path, mode: str) -> None:
    import fcntl

    from installer_bootstrap import bootstrap
    bootstrap()
    from installer_processes import OwnedCommands
    import installer_darwin

    real_launchctl = installer_darwin._launchctl

    def record(*arguments, **kwargs):
        if arguments[0] == "bootstrap":
            import plistlib

            job = plistlib.loads(Path(arguments[2]).read_bytes())
            (base / "target").write_text(arguments[1] + "/" + job["Label"])
        return real_launchctl(*arguments, **kwargs)

    installer_darwin._launchctl = record
    with (base / "lease").open("a+b") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        commands = OwnedCommands(lease.fileno())
        try:
            result = commands.run(
                [sys.executable, "-I", "-B", "-c", WRITER, str(base),
                 "success" if mode == "retained" else mode],
                cwd=base, timeout=4 if mode == "timeout" else 30,
            )
        except subprocess.TimeoutExpired as error:
            if mode != "timeout":
                raise
            (base / "result").write_text(json.dumps({
                "timeout": True, "stdout": error.stdout, "stderr": error.stderr,
            }))
        else:
            if mode not in ("success", "retained"):
                raise AssertionError("A cancelled/failed guardian returned success")
            (base / "result").write_text(json.dumps({
                "stdout": result.stdout, "stderr": result.stderr,
            }))
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (base / "settled").touch()
        if mode == "retained":
            wait(lambda: (base / "release").exists(), "Owner was not released after custody observation")
            second = commands.run(
                [sys.executable, "-I", "-B", "-c",
                 "import os,sys;assert dict(os.environ).get('ONLY_THIS') == 'explicit';"
                 "assert 'CONSTRUCTION_TEST_ENV' not in os.environ;"
                 "print('second');print('second error',file=sys.stderr)"],
                cwd=base, timeout=10, env={"ONLY_THIS": "explicit"},
            )
            assert second.stdout == "second\n" and second.stderr == "second error\n"


@unittest.skipUnless(sys.platform == "darwin", "Requires an actual Darwin kernel")
class NativeCustody(unittest.TestCase):
    def setUp(self):
        from installer_darwin import Kernel

        self.assertNotEqual(os.geteuid(), 0, "This lane must prove ordinary-user operation")
        self.kernel = Kernel()
        self.temporary = tempfile.TemporaryDirectory(prefix="ie-test-")
        self.base = Path(self.temporary.name)
        self.process = None
        self.identities = []

    def tearDown(self):
        from installer_darwin import _launchctl

        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()
                try:
                    self.process.communicate(timeout=20)
                finally:
                    if self.identities:
                        self.kernel.quiesce(self.identities[0].coalition)
                    target = self.base / "target"
                    if target.exists():
                        _launchctl("bootout", target.read_text(), absent_ok=True)
        finally:
            self.temporary.cleanup()

    def start(self, mode):
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--owner", str(self.base), mode],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "CONSTRUCTION_TEST_ENV": "owned inheritance"},
        )
        wait(
            lambda: (self.base / "participants").exists() or self.process.poll() is not None,
            "Native writer did not start",
        )
        if not (self.base / "participants").exists():
            out, err = self.process.communicate(timeout=5)
            self.fail(f"Native construction never reached writer readiness: {out}{err}")
        pids = json.loads((self.base / "participants").read_text())
        for pid in pids:
            try:
                self.identities.append(self.kernel.identity(pid))
            except ProcessLookupError:
                self.assertIn(mode, ("success", "retained"))
        return pids

    def live(self, identity):
        return self.kernel.status(identity) not in (None, 5)

    def assert_retired_under_lease(self):
        import fcntl

        def retired():
            if all(not self.live(identity) for identity in self.identities):
                return True
            with (self.base / "lease").open("a+b") as contender:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                self.assertTrue(
                    all(not self.live(identity) for identity in self.identities[1:]),
                    "Lease was released while a construction writer was alive",
                )
            return False

        wait(retired, "Construction coalition survived owner/guardian termination")
        out, err = self.process.communicate(timeout=20)
        with (self.base / "lease").open("a+b") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = (self.base / "target").read_text()
        check = subprocess.run(
            ["/bin/launchctl", "print", target], capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(check.returncode, 0, "One-shot construction job leaked")
        before = (self.base / "writes").read_bytes()
        time.sleep(.05)
        self.assertEqual(before, (self.base / "writes").read_bytes())
        return out, err

    def test_success_retires_detached_reparented_writer(self):
        self.start("success")
        out, err = self.assert_retired_under_lease()
        self.assertEqual(self.process.returncode, 0, out + err)
        self.assertTrue((self.base / "settled").exists())
        result = json.loads((self.base / "result").read_text())
        self.assertEqual(result, {"stdout": "owned output\n", "stderr": "owned error\n"})

    def test_timeout_preserves_both_streams_and_releases_quiescent_lease(self):
        self.start("timeout")
        out, err = self.assert_retired_under_lease()
        self.assertEqual(self.process.returncode, 0, out + err)
        result = json.loads((self.base / "result").read_text())
        self.assertEqual(result, {"timeout": True, "stdout": "owned output\n", "stderr": "owned error\n"})

    def test_guardian_close_does_not_unlock_live_owner_and_next_command_works(self):
        import fcntl

        self.start("retained")
        wait(lambda: (self.base / "settled").exists(), "Owner did not finish its first command")
        self.assertIsNone(self.process.poll())
        with (self.base / "lease").open("a+b") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (self.base / "release").touch()
        out, err = self.assert_retired_under_lease()
        self.assertEqual(self.process.returncode, 0, out + err)

    def test_owner_sigkill_retains_inherited_open_description(self):
        self.start("owner")
        self.process.kill()
        self.assert_retired_under_lease()
        self.assertFalse((self.base / "settled").exists())

    def test_guardian_sigkill_is_cleaned_by_original_owner(self):
        self.start("guardian")
        self.kernel.send(self.identities[0], signal.SIGKILL)
        out, err = self.assert_retired_under_lease()
        self.assertNotEqual(self.process.returncode, 0, out + err)
        self.assertFalse((self.base / "settled").exists())

    def test_sigint_defers_release_until_kernel_retirement(self):
        self.start("interrupt")
        self.process.send_signal(signal.SIGINT)
        out, err = self.assert_retired_under_lease()
        self.assertNotEqual(self.process.returncode, 0, out + err)
        self.assertIn("KeyboardInterrupt", err)
        self.assertFalse((self.base / "settled").exists())

    def test_foreign_caller_coalition_is_never_retired(self):
        identity = self.kernel.identity(os.getpid())
        with self.assertRaisesRegex(RuntimeError, "caller's coalition"):
            self.kernel.quiesce(identity.coalition)

    def test_native_counter_prefix_is_available_without_privileges(self):
        identity = self.kernel.identity(os.getpid())
        counts = self.kernel.counts(identity.coalition)
        self.assertIsNotNone(counts)
        self.assertGreater(counts[0] - counts[1], 0)
        with self.assertRaises(ValueError):
            self.kernel.send(identity, 0)
        foreign = type(identity)(identity.pid, identity.unique_id + 1, identity.idversion, identity.coalition)
        self.kernel.send(foreign, signal.SIGKILL)
        self.assertTrue(self.live(identity))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--owner":
        owner(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main(verbosity=2)
