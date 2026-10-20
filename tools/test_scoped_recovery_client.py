"""Parent-side launch forwarding tests; these do not establish native admission."""

import io
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tools.coverage_guided_selection.scoped_recovery_client import ScopedRecoveryClient


@pytest.mark.parametrize("context", ["owned", "unrecognized"])
@pytest.mark.parametrize("preloader", [None, "/existing/preloader.so"])
@pytest.mark.parametrize("argument_kind", ["list", "tuple", "path", "text", "bytes"])
def test_launch_options_and_actual_exits_preserved(monkeypatch, context, preloader, argument_kind):
    calls = []

    class NativePopen:
        def __init__(self, args, bufsize=-1, *, env=None, cwd=None):
            calls.append((args, bufsize, env, cwd))
            self.pid = 71

        def wait(self, timeout=None):
            assert timeout == 5
            return 17

        def poll(self):
            return 17

    monkeypatch.setattr(subprocess, "Popen", NativePopen)
    writer = io.StringIO()
    client = ScopedRecoveryClient(None, writer, "private-token", ["owned"])
    client.plan = {"controls": {"owned": "/control"}, "native_library": "/observer.so",
                   "entry_socket": "/entry.sock"}
    environment = {"ORIGINAL_CONFIG": "unchanged"}
    if preloader:
        environment["LD_PRELOAD"] = preloader
    original = dict(environment)
    argv = ["/actual/python", "-I", "-c", "raise SystemExit(17)"]
    if argument_kind == "tuple":
        argv = tuple(argv)
    elif argument_kind == "path":
        argv = Path(argv[0])
    elif argument_kind == "text":
        argv = argv[0]
    elif argument_kind == "bytes":
        argv = argv[0].encode()
    client.install_launch_recorder(lambda: context)
    try:
        child = subprocess.Popen(argv, 0, env=environment, cwd="/original")
        assert child.wait(timeout=5) == child.poll() == 17
    finally:
        client.restore_launch_recorder()
    assert subprocess.Popen is NativePopen
    assert environment == original
    actual_args, bufsize, forwarded, cwd = calls[0]
    assert actual_args is argv and bufsize == 0 and cwd == "/original"
    if context == "owned":
        assert forwarded["LD_PRELOAD"] == (preloader or "/observer.so")
        assert forwarded["CGS_NATIVE_CONTROL"] == "/control"
    else:
        assert forwarded is environment
    events = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [row["op"] for row in events] == ["LAUNCHED", "EXIT_OBSERVED"]
    assert events[0]["case"] == context and events[0]["pid"] == 71
    tail = b"-I\0-c\0raise SystemExit(17)" if argument_kind in {"list", "tuple"} else b""
    assert events[0]["argv_key"] == hashlib.sha256(tail).hexdigest()
    assert events[1]["returncode"] == 17


def test_empty_or_refused_completion_cannot_claim_producers():
    client = ScopedRecoveryClient(None, None, None, [])
    with pytest.raises(RuntimeError, match="missing scoped Recovery completion"):
        client.evidence()
    client.completion = {"all_admitted": False}
    assert client.evidence()["admitted"] is False
    assert client.evidence()["producer_count"] == 0


@pytest.mark.skipif(os.name == "nt", reason="owned observer uses POSIX control pipes")
def test_reply_prefix_without_newline_cannot_block_beyond_deadline():
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb", buffering=0) as reader, os.fdopen(writer_fd, "wb", buffering=0) as writer:
        client = ScopedRecoveryClient(reader, None, "token", [])
        writer.write(b'{"token":"token"')
        started = time.monotonic()
        client.deadline = started + 0.05
        with pytest.raises(RuntimeError, match="fixture guard expired"):
            client._reply()
        assert time.monotonic() - started < 0.20


@pytest.mark.skipif(os.name == "nt", reason="owned observer uses POSIX control pipes")
def test_multiple_bounded_reply_frames_preserve_buffered_next_reply():
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb", buffering=0) as reader, os.fdopen(writer_fd, "wb", buffering=0) as writer:
        client = ScopedRecoveryClient(reader, None, "token", [])
        client.deadline = time.monotonic() + 1
        writer.write(b'{"token":"token","op":"READY"}\n{"token":"token","op":"COMPLETED"}\n')
        assert client._reply()["op"] == "READY"
        assert client._reply()["op"] == "COMPLETED"


@pytest.mark.skipif(os.name == "nt", reason="owned observer uses POSIX control pipes")
def test_reply_bound_applies_before_json_parsing():
    import threading

    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb", buffering=0) as reader, os.fdopen(writer_fd, "wb", buffering=0) as writer:
        client = ScopedRecoveryClient(reader, None, "token", [])
        client.deadline = time.monotonic() + 1

        def send():
            writer.write(b"x" * 65536)

        thread = threading.Thread(target=send)
        thread.start()
        try:
            with pytest.raises(RuntimeError, match="invalid owned observer reply"):
                client._reply()
        finally:
            thread.join(timeout=1)
        assert not thread.is_alive()
