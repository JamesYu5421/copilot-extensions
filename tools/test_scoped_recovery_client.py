"""Parent-side launch forwarding tests; these do not establish native admission."""

import io
import json
import subprocess

import pytest

from tools.coverage_guided_selection.scoped_recovery_client import ScopedRecoveryClient


@pytest.mark.parametrize("context", ["owned", "unrecognized"])
@pytest.mark.parametrize("preloader", [None, "/existing/preloader.so"])
def test_launch_options_and_actual_exits_preserved(monkeypatch, context, preloader):
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
    assert events[1]["returncode"] == 17


def test_empty_or_refused_completion_cannot_claim_producers():
    client = ScopedRecoveryClient(None, None, None, [])
    with pytest.raises(RuntimeError, match="missing scoped Recovery completion"):
        client.evidence()
    client.completion = {"all_admitted": False}
    assert client.evidence()["admitted"] is False
    assert client.evidence()["producer_count"] == 0
