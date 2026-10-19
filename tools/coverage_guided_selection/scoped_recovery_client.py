"""Owned Recovery-test client; observer rights remain outside the worker."""

import hashlib
import inspect
import json
import os
import select
import subprocess
import time


class ScopedRecoveryClient:
    def __init__(self, reader, writer, token, contexts):
        self.reader, self.writer, self.token = reader, writer, token
        self.contexts = tuple(contexts)
        self.plan = self.completion = None
        self.pytest_result = None
        self.started = self.caller_end = self.pair_evidence = None
        self.processes, self.exits = {}, {}

    def _send(self, value):
        value = dict(value, token=self.token)
        self.writer.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.writer.flush()

    def _reply(self):
        if not select.select([self.reader], [], [], max(0, self.deadline-time.monotonic()))[0]:
            raise RuntimeError("owned observer fixture guard expired")
        raw = self.reader.readline(65537)
        if not raw or len(raw) > 65536:
            raise RuntimeError("invalid owned observer reply")
        value = json.loads(raw)
        if value.get("token") != self.token:
            raise RuntimeError("unowned observer reply")
        return value

    def install_launch_recorder(self, context):
        self.original = subprocess.Popen
        signature = inspect.signature(self.original)
        client = self

        class RecordedPopen(client.original):
            def _observed_exit(self, code):
                if code is not None and self.pid not in client.exits:
                    client.exits[self.pid] = code
                    client._send({"op": "EXIT_OBSERVED", "pid": self.pid, "returncode": code})
                return code

            def wait(self, *args, **kwargs):
                return self._observed_exit(super().wait(*args, **kwargs))

            def poll(self, *args, **kwargs):
                return self._observed_exit(super().poll(*args, **kwargs))

            def __init__(self, args, *positional, **kwargs):
                bound = signature.bind(args, *positional, **kwargs)
                label = context()
                if client.plan is not None and label in client.plan["controls"]:
                    environment = dict(os.environ if bound.arguments.get("env") is None else bound.arguments["env"])
                    # An unfamiliar preloader is not silently replaced.
                    if not environment.get("LD_PRELOAD"):
                        environment["LD_PRELOAD"] = client.plan["native_library"]
                    environment["CGS_ENTRY_SOCKET"] = client.plan["entry_socket"]
                    environment["CGS_NATIVE_CONTROL"] = client.plan["controls"][label]
                    bound.arguments["env"] = environment
                super().__init__(*bound.args, **bound.kwargs)
                client.processes[self.pid] = self
                digest = hashlib.sha256("\0".join(os.fsdecode(value) for value in args[1:]).encode()).hexdigest()
                client._send({"op": "LAUNCHED", "case": label, "pid": self.pid, "argv_key": digest})
        subprocess.Popen = RecordedPopen

    def restore_launch_recorder(self):
        subprocess.Popen = self.original

    def begin(self, started):
        # Local enclosing fixture guard, never authority to expand 5s/15s.
        self.deadline = started + 120
        self.started = started
        with open("/proc/self/status") as source:
            posture = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in source
                       if line.startswith(("CapEff:", "CapBnd:", "NoNewPrivs:"))}
        self._send({"op": "SETUP", "worker_pid": os.getpid(), "uid": os.getuid(), "posture": posture,
                    "started_ns": int(started*1e9), "mode": "recovery", "contexts": self.contexts})
        self.plan = self._reply()
        if self.plan["op"] != "READY":
            raise RuntimeError("scoped Recovery observer setup refused")

    def complete(self):
        self._send({"op": "COMPLETE", "pytest_returncode": self.pytest_result})
        self.completion = self._reply()
        if self.completion["op"] != "COMPLETED":
            raise RuntimeError("scoped Recovery observer completion refused")

    def evidence(self):
        if self.completion is None:
            raise RuntimeError("missing scoped Recovery completion")
        summaries = self.completion.get("summaries", [])
        return {
            "admitted": self.completion["all_admitted"],
            "producer_count": sum(row["source_streams"] for row in summaries),
            "preludes": [value for row in summaries for value in row["prelude_seconds"]],
            "completion": self.completion,
        }
