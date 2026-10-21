"""Coupled callback contracts; compiled concurrency is explicitly opt-in."""

import ast
import ctypes
import hashlib
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tools.coverage_guided_selection import observer_native_build as build
from tools.coverage_guided_selection import child_stream as stream


def test_python_generated_guard_preserves_actual_native_dispatch_mapping(tmp_path, monkeypatch):
    emitter_path = tmp_path / "emitter.py"
    emitter_bytes = b"def emit():\n    pass\n"
    emitter_path.write_bytes(emitter_bytes)
    emitter_module = compile(emitter_bytes, str(emitter_path), "exec", dont_inherit=True)
    emitter = next(code for code in emitter_module.co_consts if isinstance(code, type(emitter_module)))
    owner = compile("owned=1\n", str(tmp_path / "owner.py"), "exec", dont_inherit=True)
    frame = SimpleNamespace(f_code=emitter, f_back=SimpleNamespace(f_code=owner, f_back=None))
    for key, value in (
        ("_generated_active", True), ("_generated_state", threading.local()),
        ("_generated_lock", threading.RLock()), ("_generated_receipts", []),
        ("_generated_count", 0), ("_generator_codes", frozenset([emitter])),
        ("_expected_codes", {os.path.normcase(owner.co_filename): frozenset([owner])}),
        ("_code_sources", {}), ("_sources", {}), ("_root", ""), ("_package", "fixture_package"),
    ):
        monkeypatch.setattr(stream, key, value)
    monkeypatch.setattr(stream.sys, "_getframe", lambda depth: SimpleNamespace(f_back=frame))
    (tmp_path / "generated").mkdir()
    entry = Path(build.__file__).with_name("native_observer_entry.py")
    functions = [node for node in ast.parse(entry.read_text()).body
                 if isinstance(node, ast.FunctionDef) and
                 node.name in {"code_digest", "origin_identity", "native_source_event"}]
    namespace = {
        "hashlib": hashlib, "marshal": marshal, "json": json, "Path": Path,
        "os": os, "sys": sys, "control": tmp_path,
        "stream": stream, "auditor": SimpleNamespace(error=pytest.fail), "code_tree": stream._code_tree,
        "compile_receipts": [], "generated_code_refs": {}, "generated_ids": {},
        "catalog": {"sources": {}, "origin_source_sha256": [hashlib.sha256(emitter_bytes).hexdigest()]},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(entry), "exec"), namespace)
    content = b"def generated():\n    return 22\n"
    actual = compile(content, "<string>", "exec", dont_inherit=True)
    with (tmp_path / "events.jsonl").open("wb") as events:
        namespace["source_event_fd"] = events.fileno()
        namespace["native_source_event"]("compile", (content, "<string>"), emitter, 0)
        namespace["native_source_event"]("exec", (actual,), emitter, 0)
        native = dict(stream._code_sources)
        assert native and all(row[1] is not None for row in native.values())
        stream._generated_event("compile", (content, "<string>"))
        stream._generated_event("exec", (actual,))
        assert stream._code_sources == native
        assert stream._generated_count == 0
    assert json.loads((tmp_path / "events.jsonl").read_text())["generated_source_admitted"]
    method = next(code for code in actual.co_consts if isinstance(code, type(actual)))
    assert stream._frame_source(SimpleNamespace(
        f_code=method, f_globals={"__name__": "fixture_package.source"},
    )) == native[id(method)][1]


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("CGS_RUN_OBSERVER_CONCURRENCY") != "1",
    reason="deliberate Linux native callback build with the governed pinned toolchain",
)
def test_actual_native_dispatch_suppresses_only_same_thread_recursion(tmp_path):
    source = Path(build.__file__).with_name("native_observer_entry.c")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == build.C_SHA256
    wheel = Path(os.environ["CGS_OBSERVER_TOOLCHAIN_WHEEL"])
    toolchain = tmp_path / "toolchain"
    result = build.extract_toolchain(wheel, toolchain)
    try:
        # Keep the canonical callback body; disable only automatic registration.
        harness = tmp_path / "callback.c"
        harness.write_text(
            "#define constructor unused\n"
            f'#include "{source.as_posix()}"\n'
            "void cgs_test_dispatch(PyObject *callback) { source_dispatch=callback; handled=1; }\n"
            'int cgs_test_event(PyObject *args) { return audit("compile",args,NULL); }\n',
            encoding="utf-8",
        )
        library_path = tmp_path / "callback.so"
        compiled = subprocess.run(
            [result["compiler"], "cc", "-shared", "-fPIC", "-O1",
             '-DENTRY_SHA="' + "1" * 64 + '"', str(harness), "-o", str(library_path), "-ldl"],
            capture_output=True, text=True, timeout=60,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        library = ctypes.PyDLL(str(library_path))
        library.cgs_test_dispatch.argtypes = [ctypes.py_object]
        library.cgs_test_dispatch.restype = None
        library.cgs_test_event.argtypes = [ctypes.py_object]
        library.cgs_test_event.restype = ctypes.c_int
        entered, second_entered, release = (threading.Event() for _ in range(3))
        calls, failures = [], []

        def callback(event, arguments, origin, offset):
            label, = arguments
            calls.append(label)
            if event != "compile" or not isinstance(origin, type(callback.__code__)):
                failures.append("invalid actual callback origin")
            if label == "first":
                library.cgs_test_event(("recursive",))
                entered.set()
                if not release.wait(10):
                    failures.append("first callback release timeout")
            elif label == "second":
                second_entered.set()

        library.cgs_test_dispatch(callback)
        first = threading.Thread(target=lambda: library.cgs_test_event(("first",)))
        second = threading.Thread(target=lambda: library.cgs_test_event(("second",)))
        first.start()
        try:
            assert entered.wait(5), "first actual callback never entered"
            second.start()
            assert second_entered.wait(3), "another thread's actual callback was suppressed"
        finally:
            release.set()
            first.join(timeout=5)
            if second.ident is not None:
                second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        assert sorted(calls) == ["first", "second"] and not failures
    finally:
        shutil.rmtree(toolchain)
