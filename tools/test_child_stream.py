"""Strict evidence parsing for the lightweight standalone-child prototype."""
import os
import time

import pytest

from tools.coverage_guided_selection import child_stream as runtime
from tools.coverage_guided_selection import child_stream_parent as stream
from tools.coverage_guided_selection import child_audit


def test_child_runtime_has_only_runtime_functions_and_parent_reuses_constants():
    import ast
    from pathlib import Path
    names={node.name for node in ast.parse(Path(runtime.__file__).read_text()).body
           if isinstance(node,ast.FunctionDef)}
    assert names=={"_phase","_auditor","_write","_fail","_source","_frame_source",
                   "_line","_call","_after_fork","start"}
    assert stream._auditor is runtime._auditor
    assert (stream.ENV,stream.VERSION,stream.MAX_RECORDS,stream.MAX_MANIFEST)==(
        runtime.ENV,runtime.VERSION,runtime.MAX_RECORDS,runtime.MAX_MANIFEST)
    assert not any(name in runtime.__dict__ for name in
                   ("source_inventory","manifest_text","read_streams","configure_parent","_contexts","_helpers"))


def test_staged_parent_source_is_byte_exact_and_end_checked(tmp_path,monkeypatch):
    import hashlib
    from pathlib import Path
    from tools.coverage_guided_selection import standalone
    source=tmp_path/"source.py"
    source.write_text("pass\n")
    _,options=standalone.coverage_files(tmp_path,{str(source):"component/source.py"})
    assert len(options["child_stream_helpers"])==3
    for name in ("child_stream","child_stream_parent","child_audit"):
        staged=Path(options["_cgs_"+name])
        expected=Path(runtime.__file__).with_name(name+".py").read_bytes()
        assert staged.read_bytes()==expected
        assert options["child_stream_helpers"][str(staged)]==hashlib.sha256(expected).hexdigest()
    monkeypatch.setattr(stream,"_helpers",options["child_stream_helpers"])
    Path(options["_cgs_child_stream_parent"]).write_text("# changed parent evidence parser\n")
    with pytest.raises(ValueError,match="instrumentation source changed"):
        stream.finish_parent()


def test_sparse_seen_fast_path_only_publishes_committed_records(monkeypatch):
    stream = runtime
    import threading
    from types import SimpleNamespace
    frame=SimpleNamespace(f_lineno=2,f_code=SimpleNamespace(co_filename="source.py"))
    monkeypatch.setattr(stream,"_source",lambda name:(1,3))
    monkeypatch.setattr(stream,"_failed",False)
    monkeypatch.setattr(stream,"_seen",set())
    monkeypatch.setattr(stream,"_lock",threading.Lock())
    writes=[]
    def write(record):
        assert (1,2) not in stream._seen
        writes.append(record)
    monkeypatch.setattr(stream,"_write",write)
    stream._line(frame,"line",None)
    stream._line(frame,"line",None)
    assert len(writes)==1 and stream._seen=={(1,2)}
    monkeypatch.setattr(stream,"_seen",set())
    failures=[]
    monkeypatch.setattr(stream,"_fail",lambda:failures.append(True))
    def failed(record):
        raise OSError("write failure")
    monkeypatch.setattr(stream,"_write",failed)
    stream._line(frame,"line",None)
    assert failures==[True] and not stream._seen


def test_distinct_actual_code_objects_do_not_alias_shared_string_filename(monkeypatch):
    from types import SimpleNamespace
    first=compile("first=1\n","<string>","exec",dont_inherit=True)
    second=compile("second=2\n","<string>","exec",dont_inherit=True)
    monkeypatch.setattr(runtime,"_code_sources",{id(first):(500,1),id(second):(501,1)})
    monkeypatch.setattr(runtime,"_sources",{})
    monkeypatch.setattr(runtime,"_root","owned-source-root/")
    assert runtime._frame_source(SimpleNamespace(f_code=first))==(500,1)
    assert runtime._frame_source(SimpleNamespace(f_code=second))==(501,1)
    unknown=compile("third=3\n","<string>","exec",dont_inherit=True)
    assert runtime._frame_source(SimpleNamespace(f_code=unknown)) is None


@pytest.mark.parametrize("corruption",["collision","foreign-path","wrong-hash","fake-canonical"])
def test_generated_source_extension_identity_is_strict(evidence,corruption):
    import hashlib
    directory,_,_,_,options=evidence
    generated=options["audit_control"]/"generated"
    generated.mkdir()
    source=generated/"observed.py"
    content=b"actual_generated=1\n"
    source.write_bytes(content)
    digest=hashlib.sha256(content).hexdigest()
    row={"installed":str(source),"sha256":digest,"lines":1,
         "canonical":"generated-source:sha256:"+digest}
    identifier=500
    if corruption=="collision":identifier=next(iter(options["inventory"]))
    elif corruption=="foreign-path":row["installed"]=str(source.parent.parent/"foreign.py")
    elif corruption=="wrong-hash":row["sha256"]="0"*64
    else:row["canonical"]="component/pretend-fixed-source.py"
    with pytest.raises(ValueError,match="generated source"):
        stream.read_streams(directory,**options,generated_inventory={identifier:row})


@pytest.mark.parametrize("arguments,expected", [
    (["-I", "-S", "-c", "pass"], "site-disabled"),
    (["-IS", "-c", "pass"], "site-disabled"),
    (["-Scpass"], "site-disabled"),
    (["-S", "--version"], "information"),
    (["-IS", "--help"], "information"),
    (["-VV"], "information"),
    (["-X", "utf8", "-S", "-c", "pass"], "site-disabled"),
    (["-XS", "-c", "pass"], "instrumented"),
    (["-ISX", "--help", "-c", "pass"], "site-disabled"),
    (["-ISW", "--version", "-c", "pass"], "site-disabled"),
    (["-W", "--help", "-c", "pass"], "instrumented"),
    (["-c", "pass", "--help", "-S"], "instrumented"),
    (["-m", "module", "--version"], "instrumented"),
    (["script.py", "--help", "-S"], "instrumented"),
    (["--", "--version"], "instrumented"),
    (["-S", "-c", "pass", "--version"], "site-disabled"),
    (["-I", "-S", "-", "--help"], "site-disabled"),
    (["-I", "-", "--version"], "instrumented"),
])
def test_interpreter_options_do_not_exempt_site_disabled_code(arguments, expected):
    assert child_audit.python_mode(["python", *arguments]) == expected


@pytest.fixture
def evidence(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("def f():\n    return 3\n", encoding="utf-8")
    inventory = stream.source_inventory({str(source): "component/source.py"})
    directory = tmp_path / "hits"
    directory.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    (tmp_path / "status").write_bytes(b"R")
    nonce, context = "a" * 64, "b" * 64
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    stream.write_manifest(
        manifests / (context + ".manifest"), nonce=nonce, context=context,
        directory=directory, inventory=inventory,
    )
    pid = 2147483647
    path = directory / f"{pid}-{'c' * 32}.hits"
    header = f"R {stream.VERSION} {nonce} {context} {pid} 1 {'d' * 64} 2\n".encode()
    (audit / ("1-" + "e" * 32 + ".intent")).write_text(
        f"{child_audit.VERSION} {nonce} {context} 1 {'d' * 64} 1\n",
    )
    payload = b"\0\0" + (100).to_bytes(4, "big") + b"\0\1" + (2).to_bytes(4, "big")
    path.write_bytes(header + payload)
    return directory, path, header, payload, dict(
        nonce=nonce, contexts={context: "tests/test_source.py::test_f"}, inventory=inventory,
        audit_control=tmp_path,
    )


def test_verified_records_merge_without_replacing_parent_evidence(evidence):
    directory, _, _, _, options = evidence
    result = stream.read_streams(directory, **options)
    assert result["prelude_seconds"] == [0.0001]
    assert result["child_streams"] == 1
    merged = stream.merge_coverage(
        {"component/source.py": {"2": ["tests/test_other.py::test_other"]}}, result["coverage"],
    )
    assert merged["component/source.py"]["2"] == [
        "tests/test_other.py::test_other", "tests/test_source.py::test_f",
    ]


@pytest.mark.parametrize("corruption", [
    "truncated", "unknown-source", "line-bound", "unknown-context", "wrong-run",
    "wrong-version", "producer-error", "oversized", "wrong-pid-name", "source-changed",
    "duplicate", "missing-producer", "missing-intent", "unknown-audit-context",
    "manifest-changed", "manifest-missing",
    "future-birth",
])
def test_malformed_or_unearned_child_evidence_is_refused(evidence, corruption):
    directory, path, header, payload, options = evidence
    if corruption == "truncated":
        payload = payload[:-1]
    elif corruption == "unknown-source":
        payload = payload[:6] + b"\0\x02" + (2).to_bytes(4, "big")
    elif corruption == "line-bound":
        payload = payload[:6] + b"\0\x01" + (3).to_bytes(4, "big")
    elif corruption == "unknown-context":
        options["contexts"] = {}
    elif corruption == "wrong-run":
        options["nonce"] = "d" * 64
    elif corruption == "wrong-version":
        header = header.replace(stream.VERSION.encode(), b"INVALID/0")
    elif corruption == "future-birth":
        header = header.rstrip().rsplit(b" ", 1)[0] + b" 99999999999999999999999\n"
    elif corruption == "producer-error":
        header = b"E" + header[1:]
    elif corruption == "oversized":
        payload = b"\0" * (6 * (stream.MAX_RECORDS + 2))
    elif corruption == "wrong-pid-name":
        path = path.rename(directory / ("1-" + "c" * 32 + ".hits"))
    elif corruption == "duplicate":
        payload += payload[6:]
    elif corruption == "missing-producer":
        path.unlink()
        with pytest.raises(ValueError, match="missing"):
            stream.read_streams(directory, **options)
        return
    elif corruption == "missing-intent":
        next((options["audit_control"] / "audit").iterdir()).unlink()
    elif corruption == "unknown-audit-context":
        intent = next((options["audit_control"] / "audit").iterdir())
        intent.write_text(intent.read_text().replace("b" * 64, "f" * 64))
    elif corruption.startswith("manifest-"):
        manifest = next((options["audit_control"] / "manifests").iterdir())
        if corruption == "manifest-missing":
            manifest.unlink()
        else:
            manifest.write_text(manifest.read_text() + "2\t3\tffff\n")
    else:
        from pathlib import Path
        Path(options["inventory"][1]["installed"]).write_text("changed = True\n")
    path.write_bytes(header + payload)
    with pytest.raises(ValueError):
        stream.read_streams(directory, **options)


def test_zero_source_hit_producer_is_not_missing(evidence):
    directory, path, header, payload, options = evidence
    path.write_bytes(header + payload[:6])
    result = stream.read_streams(directory, **options)
    assert result["coverage"] == {}
    assert result["zero_hit_producers"] == result["audited_producers"] == 1


def test_no_opted_in_spawns_requires_no_producer(evidence):
    directory, path, _, _, options = evidence
    path.unlink()
    next((options["audit_control"] / "audit").iterdir()).unlink()
    result = stream.read_streams(directory, **options)
    assert result["audit_complete"] and result["audited_producers"] == 0


def test_manifest_and_inventory_bounds_are_explicit(evidence):
    directory, _, _, _, options = evidence
    row = dict(options["inventory"][1], installed="x" * 40000)
    with pytest.raises(ValueError, match="manifest exceeds"):
        stream.write_manifest(
            directory / "oversized", nonce=options["nonce"], context="b" * 64,
            directory=directory, inventory={1: row},
        )
    installed = options["inventory"][1]["installed"]
    with pytest.raises(ValueError, match="ambiguous"):
        stream.source_inventory({installed: "same.py", installed + ".other": "same.py"})


def test_running_producer_is_never_a_complete_prefix(evidence):
    directory, path, header, payload, options = evidence
    path.unlink()
    path = directory / f"{os.getpid()}-{'c' * 32}.hits"
    path.write_bytes(header.replace(b"2147483647", str(os.getpid()).encode()) + payload)
    with pytest.raises(ValueError, match="still-running"):
        stream.read_streams(directory, **options)


def test_live_producer_cannot_append_and_exit_between_snapshot_and_verification(evidence, monkeypatch):
    from pathlib import Path

    directory, path, _, payload, options = evidence
    state = {"alive": True, "snapshot_taken": False}
    original_open = Path.open

    class Snapshot:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def read(self, size):
            prefix = self.handle.read(size)
            state["snapshot_taken"] = True
            with original_open(path, "ab") as producer:
                producer.write(payload[6:8] + (1).to_bytes(4, "big"))
            state["alive"] = False
            return prefix

    def open_path(candidate, mode="r", *args, **kwargs):
        handle = original_open(candidate, mode, *args, **kwargs)
        return Snapshot(handle) if candidate == path and mode == "rb" else handle

    monkeypatch.setattr(child_audit, "live", lambda _pid: state["alive"])
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(ValueError, match="still-running"):
        stream.read_streams(directory, **options)
    assert not state["snapshot_taken"]
    assert state["alive"]


def test_timing_must_precede_line_records(evidence):
    directory, path, header, payload, options = evidence
    path.write_bytes(header + payload[6:] + payload[:6])
    with pytest.raises(ValueError, match="framing"):
        stream.read_streams(directory, **options)


def test_audited_spawn_time_must_precede_producer_birth(evidence):
    directory, _, _, _, options = evidence
    intent = next((options["audit_control"] / "audit").iterdir())
    text = intent.read_text().rstrip()
    intent.write_text(text.rsplit(" ", 1)[0] + f" {time.monotonic_ns()}\n")
    with pytest.raises(ValueError, match="freshness"):
        stream.read_streams(directory, **options)


@pytest.mark.parametrize("event", ["os.fork", "os.forkpty", "os.exec"])
def test_untraceable_native_lifecycle_fails_closed(tmp_path, monkeypatch, event):
    (tmp_path / "audit").mkdir()
    (tmp_path / "status").write_bytes(b"R")
    monkeypatch.setattr(child_audit, "_control", str(tmp_path))
    monkeypatch.setattr(child_audit, "_failed", False)
    monkeypatch.setenv(stream.ENV, "opted-in")
    child_audit._event(event, ())
    assert (tmp_path / "status").read_bytes() == b"E"


@pytest.mark.parametrize("event", ["subprocess.Popen", "os.fork", "os.forkpty", "os.exec", "os.system"])
def test_active_parent_context_survives_environment_marker_removal(tmp_path, monkeypatch, event):
    from tools.coverage_guided_selection import child_stream_parent

    (tmp_path / "audit").mkdir()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "hits").mkdir()
    (tmp_path / "status").write_bytes(b"R")
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    monkeypatch.setattr(child_stream_parent, "_control", str(tmp_path))
    monkeypatch.setattr(child_stream_parent, "_nonce", "a" * 64)
    monkeypatch.setattr(child_stream_parent, "_contexts", {})
    monkeypatch.setattr(child_stream_parent, "_inventory", stream.source_inventory({str(source): "source.py"}))
    monkeypatch.setattr(child_audit, "_control", str(tmp_path))
    monkeypatch.setattr(child_audit, "_nonce", "a" * 64)
    monkeypatch.setattr(child_audit, "_python", child_audit.key(os.sys.executable))
    monkeypatch.setattr(child_audit, "_failed", False)
    monkeypatch.setattr(child_audit, "_child", False)
    monkeypatch.setattr(child_audit, "_own_context", None)
    monkeypatch.setenv(stream.ENV, "temporary")
    child_stream_parent.register_context("tests/test_source.py::test_parent")
    monkeypatch.delenv(stream.ENV)
    arguments = (os.sys.executable, [os.sys.executable, "-I", "-S", "-", "--help"], None, {})
    child_audit._event(event, arguments if event == "subprocess.Popen" else ())
    assert (tmp_path / "status").read_bytes() == b"E"
    assert child_audit._own_context
    with pytest.raises(ValueError, match="audit failure"):
        child_audit.verify(tmp_path, nonce="a" * 64, contexts={}, producers=[])


def test_unavailable_posix_process_query_is_not_confirmed_exit(monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX process-query error boundary")

    def denied(*_args):
        raise PermissionError("process visibility denied")

    monkeypatch.setattr(os, "kill", denied)
    assert child_audit.live(1) is True


def test_fork_reset_never_reuses_inherited_fd_seen_set_or_locked_mutex(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import os,sys;sys.path.insert(0,{str(root)!r});"
        "from tools.coverage_guided_selection import child_stream as s;"
        f"fd=os.open({str(tmp_path / 'owned')!r},os.O_CREAT|os.O_WRONLY,0o600);"
        "s._fd=fd;s._lock.acquire();s._seen.add((1,1));s._after_fork();"
        "assert s._fd is None and s._failed and not s._seen and not s._lock.locked();"
        "\n"
        "try:os.fstat(fd)\n"
        "except OSError:pass\n"
        "else:raise AssertionError('inherited descriptor remains open')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=20,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    assert result.returncode == 0, result.stderr


def test_bootstrap_failure_maps_immutable_context_and_sanitized_lineage(tmp_path, monkeypatch):
    import json
    (tmp_path / "audit").mkdir()
    (tmp_path / "status").write_bytes(b"R")
    token = "b" * 64
    monkeypatch.setenv(stream.ENV, str(tmp_path / "manifests" / (token + ".manifest")))
    monkeypatch.setattr(child_audit, "_control", None)
    monkeypatch.setattr(child_audit, "_failed", False)
    error = child_audit.LineageError({
        "expected_auditor_pid": 101, "producer_pid": 202,
        "actual_parent_chain": [202, 303, 404, 101], "path": "must-not-be-retained",
        "actual_parent_images": ["python.exe", "python.exe", "uv.exe", "python.exe", "private-name.exe"],
    })
    child_audit.report_bootstrap_error(tmp_path, error)
    path = next((tmp_path / "audit").iterdir())
    record = json.loads(path.read_text())
    assert record["context_digest"] == token
    assert record["lineage"]["actual_parent_chain"] == [202, 303, 404, 101]
    assert "path" not in record["lineage"]
    assert record["lineage"]["actual_parent_images"][-1] == "other"
    assert str(tmp_path) not in path.read_text()
    with pytest.raises(ValueError, match="tests/test_sdk.py::test_consumer"):
        child_audit.verify(
            tmp_path, nonce="a" * 64, contexts={token: "tests/test_sdk.py::test_consumer"},
            producers=[],
        )


@pytest.mark.skipif(
    os.environ.get("CGS_RUN_STANDALONE_INTEGRATION") != "1",
    reason="requires deliberately prepared native installed fleet environment",
)
class TestChildStreamNative:
    """Persisted real-process regressions; never invokes full baseline collection."""

    def _run(self, source=None, *, pair=False, ordinary=False, selection=None, reference_code=None):
        import hashlib
        import json
        import subprocess
        import tempfile
        from pathlib import Path
        from tools.coverage_guided_selection import standalone as s

        repo = s.REPO
        python = s.runner.default_python("fleet-manager")
        environment = python.parent.parent
        lease = s.runner.acquire(60, environment)
        temporary = tempfile.TemporaryDirectory(prefix="c", dir=repo / ".pytmp")
        hook = None
        try:
            sandbox = Path(temporary.name)
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            probe = subprocess.run(
                [str(python), "-I", "-c", "import importlib.util;from pathlib import Path;"
                 "print(Path(importlib.util.find_spec('fleet_manager').origin).parent)"],
                check=True, capture_output=True, text=True, **flags,
            )
            installed = Path(probe.stdout.strip())
            mapping, hashes = s.verified_source_mapping(repo, installed, environment)
            self._last_source_mapping = mapping
            if reference_code is not None:
                reference_sandbox = sandbox / "reference-run"
                reference_env = s.isolated_environment(os.environ, reference_sandbox)
                for name in (
                    "PYTEST_ADDOPTS", "PYTHONPATH", "COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG",
                    "_CGS_CHILD_MANIFEST", "_CGS_CHILD_AUDITOR",
                ):
                    reference_env.pop(name, None)
                reference_driver = sandbox / "reference.py"
                reference_driver.write_text(
                    "import coverage,json\nfrom pathlib import Path\n"
                    f"c=coverage.Coverage(config_file=False,data_file={str(sandbox / 'reference.coverage')!r},"
                    f"source=[{str(installed)!r}]);c.start()\n"
                    + reference_code + "\nc.stop();d=c.get_data()\n"
                    f"Path({str(sandbox / 'reference.json')!r}).write_text(json.dumps("
                    "{f:sorted(d.lines(f)) for f in d.measured_files() if d.lines(f)}))\n",
                    encoding="utf-8",
                )
                assert s.run_contained(
                    [str(python), "-I", str(reference_driver)], cwd=repo / "fleet-manager",
                    env=reference_env, sandbox=reference_sandbox, limits=s.Limits(wall_seconds=90),
                ) == 0
            text, options = s.coverage_files(sandbox, mapping)
            path = installed.parent / "_cgs_standalone.pth"
            with path.open("x", encoding="utf-8") as file:
                hook = path
                file.write(text)
            env = s.isolated_environment(os.environ, sandbox)
            for name in (
                "PYTEST_ADDOPTS", "PYTHONPATH", "COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG",
                "_CGS_CHILD_MANIFEST", "_CGS_CHILD_AUDITOR",
            ):
                env.pop(name, None)
            env["CGS_TEST_HOOK"] = str(hook)
            env["CGS_TEST_STREAM"] = options["_cgs_child_stream"]
            env["CGS_TEST_AUDIT"] = options["_cgs_child_audit"]
            env["CGS_TEST_CONTROL"] = options["child_stream_control"]
            env["CGS_TEST_INSTALLED"] = str(installed)
            test = sandbox / "test_native_stream.py"
            if source is not None:
                test.write_text(source, encoding="utf-8")
            selected = selection or (["tests/test_two_node_recovery.py"] if pair else [str(test)])
            driver = sandbox / "driver.py"
            driver.write_text(s._DRIVER_SCRIPT, encoding="utf-8")
            out = sandbox / "result.json"
            command = (
                [str(python), "-I", "-m", "pytest", "-q", *selected, "--basetemp", str(sandbox / "p")]
                if ordinary else [
                    str(python), "-I", str(driver), json.dumps(selected), s.SOURCE, str(repo),
                    str(sandbox / ".coverage"), str(sandbox / "report.json"), str(out),
                    str(sandbox / "p"), json.dumps({
                        "installed_source": str(installed), "source_mapping": mapping,
                        "compact_tmp_paths": os.name == "nt", **options,
                    }),
                ]
            )
            started = time.perf_counter()
            status = s.run_contained(
                command, cwd=repo / "fleet-manager", env=env, sandbox=sandbox,
                limits=s.Limits(wall_seconds=90),
            )
            elapsed = time.perf_counter() - started
            data = json.loads(out.read_text()) if out.exists() else None
            report = sandbox / "report.json"
            self._last_report = json.loads(report.read_text()) if report.exists() else None
            nodes = {
                hashlib.sha256(row["nodeid"].encode()).hexdigest(): row["nodeid"]
                for row in (self._last_report or {}).get("tests", [])
            }
            self._last_errors = []
            for error in sorted(
                (sandbox / "child-lines" / "audit").glob("*.error"),
                key=lambda path: path.stat().st_mtime_ns,
            ):
                record = json.loads(error.read_text())
                record["pytest_node"] = nodes.get(record.get("context_digest"), "unknown-context")
                self._last_errors.append(record)
            self._last_sdk = []
            if selection:
                for target in (sandbox / "p").rglob("installed-sdk"):
                    _, sdk_hashes = s.verified_source_mapping(repo, target / "fleet_manager", sandbox)
                    self._last_sdk.append({
                        "copied_wheel_modules": len(sdk_hashes), "canonical_sha256": sdk_hashes,
                        "source_equivalent": True,
                    })
            reference = sandbox / "reference.json"
            reference = json.loads(reference.read_text()) if reference.exists() else None
            assert s.verified_source_mapping(repo, installed, environment) == (mapping, hashes)
            return status, data, reference, elapsed
        finally:
            if hook is not None:
                hook.unlink(missing_ok=True)
            s.runner.cleanup_sandbox(temporary)
            lease.release()

    def test_exact_line_parity_threads_crashes_union_and_selection(self):
        import json
        from tools.coverage_guided_selection.selection import select_tests
        code = (
            "import os,sys,threading;from fleet_manager.planning import canonical;"
            "canonical({'child':True});"
            "t=threading.Thread(target=lambda:canonical('thread-only'));t.start();t.join();"
        )
        source = f"CODE={code!r}\n" + r'''
import json,os,signal,subprocess,sys,threading
from pathlib import Path
import pytest
RUN = "assert 'coverage' not in __import__('sys').modules;" + CODE
FLAGS={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
def test_normal():
    assert subprocess.run([sys.executable,'-I','-c',RUN],**FLAGS).returncode==0
def test_abrupt():
    assert subprocess.run([sys.executable,'-I','-c',RUN+"os._exit(23)"],**FLAGS).returncode==23
def test_forced():
    p=subprocess.Popen([sys.executable,'-I','-c',RUN+
        "print('READY',os.getpid(),flush=True);sys.stdin.read()"],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**FLAGS)
    line=p.stdout.readline().decode()
    assert line.startswith('READY ')
    os.kill(int(line.split()[1]),signal.SIGTERM)
    p.communicate(timeout=10)
    assert p.returncode!=0
def test_zero():
    assert subprocess.run([sys.executable,'-I','-c','pass'],**FLAGS).returncode==0
'''
        status, data, reference, _ = self._run(source, reference_code=code)
        assert status == 0
        children = data["children"]
        assert children["audited_producers"] == children["child_streams"] == 4
        assert children["zero_hit_producers"] == 1
        names = {node.rsplit("::", 1)[-1]: node for node in data["durations"]}
        for name in ("test_normal", "test_abrupt", "test_forced"):
            actual = {
                file: sorted(int(line) for line, nodes in lines.items() if names[name] in nodes)
                for file, lines in data["coverage"].items()
            }
            actual = {file: lines for file, lines in actual.items() if lines}
            expected = {}
            for file, lines in reference.items():
                canonical = self._last_source_mapping[str(__import__("pathlib").Path(file).resolve())]
                expected[canonical] = lines
            assert actual == expected
        key = "fleet-manager/src/fleet_manager/planning.py"
        nodes = {names[name] for name in ("test_normal", "test_abrupt", "test_forced")}
        line = next(int(line) for line, covered in data["coverage"][key].items() if set(covered) == nodes)
        selected = select_tests({"coverage": data["coverage"]}, {key: [line]})
        assert set(selected.selected_tests) == nodes and not selected.fallback_triggered
        assert stream.merge_coverage({"parent.py": {"1": ["parent-test"]}}, data["coverage"])["parent.py"]
        print("INTEGRATED_NATIVE", json.dumps(children))

    @pytest.mark.parametrize("failure", [
        "bootstrap", "audit-disabled", "missing", "unknown", "write", "trace", "thread-trace", "live",
        "site-disabled", "site-disabled-bundled",
        "parent-context-stripped", "site-disabled-stdin",
    ])
    def test_opted_in_failure_never_emits_successful_result(self, failure):
        source = r'''
import os,subprocess,sys
from pathlib import Path
FLAGS={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
def test_expected_application_behavior():
    CASE=CASE_VALUE
    environment=dict(os.environ)
    code="from fleet_manager.planning import canonical;canonical({'x':1})"
    arguments=[sys.executable,'-I','-c']
    if CASE=='bootstrap':
        Path(os.environ['CGS_TEST_STREAM']).write_text("raise RuntimeError('bootstrap injection')\n")
    elif CASE=='audit-disabled':
        path=Path(os.environ['CGS_TEST_AUDIT'])
        path.write_text(path.read_text().replace('sys.addaudithook(_event)', 'pass # refused hook'))
    elif CASE=='missing':
        Path(os.environ['CGS_TEST_HOOK']).unlink()
    elif CASE=='unknown':
        original=Path(environment['_CGS_CHILD_MANIFEST'])
        token='f'*64
        path=original.with_name(token+'.manifest')
        content=original.read_text().splitlines()
        fields=content[0].split('\t');fields[2]=token;content[0]='\t'.join(fields)
        path.write_text('\n'.join(content)+'\n')
        environment['_CGS_CHILD_MANIFEST']=str(path)
    elif CASE=='write':
        code=("import _cgs_child_stream as s;"
              "s._write=lambda v:(_ for _ in ()).throw(OSError('write injection'));"+code)
    elif CASE=='trace':
        code="import sys;sys.settrace(None);"+code
    elif CASE=='thread-trace':
        code="import threading;threading.settrace(None);"+code
    elif CASE=='live':
        code+=";import time;print('READY',flush=True);time.sleep(30)"
        p=subprocess.Popen([sys.executable,'-I','-c',code],env=environment,
                           stdout=subprocess.PIPE,**FLAGS)
        assert p.stdout.readline().strip()==b'READY'
        return
    elif CASE=='parent-context-stripped':
        os.environ.pop('_CGS_CHILD_MANIFEST')
        environment.pop('_CGS_CHILD_MANIFEST')
    elif CASE.startswith('site-disabled'):
        package=str(Path(os.environ['CGS_TEST_INSTALLED']).parent)
        code="import sys;sys.path.insert(0,"+repr(package)+");"+code
        arguments=[sys.executable,*(['-IS'] if CASE.endswith('bundled') else ['-I','-S']),'-c']
        if CASE=='site-disabled-stdin':
            assert subprocess.run([sys.executable,'-I','-S','-','--help'],
                                  input=code,text=True,env=environment,**FLAGS).returncode==0
            return
    assert subprocess.run([*arguments,code],env=environment,**FLAGS).returncode==0
'''
        status, data, _, _ = self._run(source.replace("CASE_VALUE", repr(failure)))
        assert status != 0 and data is None
        assert self._last_report["summary"]["passed"] == 1

    def test_real_recovery_pair_original_deadlines(self):
        import json
        ordinary, _, _, ordinary_wall = self._run(pair=True, ordinary=True)
        assert ordinary == 0
        status, data, _, wall = self._run(pair=True)
        assert status == 0 and data["summary"]["passed"] == 2
        assert data["children"]["audited_producers"] == 12
        print("REAL_PAIR", json.dumps({
            "ordinary_wall_s": ordinary_wall, "integrated_wall_s": wall,
            "wall_ratio": wall / ordinary_wall, "children": data["children"],
        }))

    def test_sdk_isolation_reports_exact_consumer_and_lineage(self):
        import json
        node = (
            "tests/test_artifact_staging.py::"
            "test_actual_installed_authenticated_artifact_staging_has_no_execution_or_network"
        )
        status, data, _, elapsed = self._run(selection=[node])
        assert status != 0 and data is None and self._last_report["summary"]["passed"] == 1
        assert self._last_sdk and self._last_sdk[0]["source_equivalent"]
        assert all(row["pytest_node"] == node for row in self._last_errors)
        print("SDK_BOUNDARY", json.dumps({
            "application_passed": 1, "collector_status": status, "wall_s": elapsed,
            "failures": self._last_errors, "copied_sdk": self._last_sdk,
            "owner_rejection_reproduced": any(row.get("lineage") for row in self._last_errors),
        }, sort_keys=True))
