"""Sparse stdlib-only child line tracing; no subprocess interception.

The parent owns source equivalence, context identities, bounds and schema-v3
merging. This module grants no baseline publication or selection authority.
"""

import os
import sys
import threading
import time

ENV = "_CGS_CHILD_MANIFEST"
VERSION = "CGS-LINES/2"
MAX_RECORDS = 100_000
MAX_MANIFEST = 65_536
_started = False
_failed = False
_fd = None
_seen = set()
_lock = threading.Lock()
_sources = {}
_code_sources = {}
_root = ""
_package = ""
_control = None
_arming = False
_phase_marker = None
_phase_reporter = None


def _phase(name):
    if _phase_marker is not None:
        _phase_marker(name)


def _auditor():
    value = sys.modules.get("_cgs_child_audit")
    if value is None:
        from tools.coverage_guided_selection import child_audit
        value = child_audit
    return value


def _write(value):
    while value:
        count = os.write(_fd, value)
        if count <= 0:
            raise OSError("short child coverage write")
        value = value[count:]


def _fail():
    global _failed
    _failed = True
    _auditor().error("child trace bounds or write failure")
    try:
        os.lseek(_fd, 0, os.SEEK_SET)
        os.write(_fd, b"E")
    except OSError:
        pass


def _source(filename):
    value = _sources.get(filename)
    if value is None:
        scoped = (filename.lower() if os.name == "nt" else filename).startswith(_root)
        if scoped:
            value = _sources.get(os.path.normcase(filename))
            if value is None and filename.endswith(".py"):
                _auditor().error("unmapped code filename inside verified source scope")
    return value


def _frame_source(frame):
    source = _code_sources.get(id(frame.f_code)) or _source(frame.f_code.co_filename)
    if source is None and _package:
        module = getattr(frame, "f_globals", {}).get("__name__", "")
        if isinstance(module, str) and (module == _package or module.startswith(_package + ".")):
            _auditor().error("owned package executed outside verified source inventory")
    return source


def _line(frame, event, arg):
    if event == "line" and not _failed:
        source = _frame_source(frame)
        if source is not None:
            identifier, maximum = source
            line = frame.f_lineno
            if line <= 0:
                return _line
            key = (identifier, line)
            if key in _seen:
                return _line
            with _lock:
                if key not in _seen:
                    if not 0 < line <= maximum or len(_seen) >= MAX_RECORDS:
                        _fail()
                    else:
                        try:
                            _write(identifier.to_bytes(2, "big") + line.to_bytes(4, "big"))
                            # Publish only after the unbuffered record is committed.
                            _seen.add(key)
                        except OSError:
                            _fail()
    return _line


def _call(frame, event, arg):
    return _line if _frame_source(frame) is not None else None


def _after_fork():
    global _fd, _failed, _lock, _seen
    # Fork is explicitly refused by the audit contract. Never reuse its lock,
    # descriptor or sparse seen-set while the application finishes normally.
    if _fd is not None:
        os.close(_fd)
    _fd, _failed, _lock, _seen = None, True, threading.Lock(), set()
    sys.settrace(None)
    threading.settrace(None)


def start(control):
    global _started, _fd, _root, _package, _control, _arming
    value = os.environ.get(ENV)
    if _started or not value:
        return
    _control = control
    if (
        os.path.islink(value)
        or os.path.dirname(os.path.realpath(value))
        != os.path.realpath(os.path.join(control, "manifests"))
    ):
        raise ValueError("unowned child context manifest")
    start_marker = os.environ.get("_CGS_PRELUDE_NS")
    started = int(start_marker) if start_marker is not None else time.perf_counter_ns()
    with open(value, "rb") as stream:
        content = stream.read(MAX_MANIFEST + 1)
    _phase("manifest-read")
    if len(content) > MAX_MANIFEST:
        raise ValueError("child coverage manifest exceeds bound")
    rows = content.decode("utf-8").splitlines()
    version, nonce, context, directory, root = rows[0].split("\t")
    with open(os.path.join(control, "run"), encoding="ascii") as handle:
        run = handle.read(65)
    if version != VERSION or nonce != run or any(
        len(token) != 64 or any(c not in "0123456789abcdef" for c in token)
        for token in (nonce, context)
    ):
        raise ValueError("invalid child coverage manifest identity")
    directory, root = (bytes.fromhex(field).decode("utf-8") for field in (directory, root))
    if os.path.islink(directory) or os.path.realpath(directory) != os.path.realpath(
        os.path.join(control, "hits")
    ):
        raise ValueError("unowned child stream directory")
    _root = os.path.normcase(root)
    _package = os.path.basename(root.rstrip("/\\"))
    identifiers = set()
    for row in rows[1:]:
        identifier, maximum, filename = row.split("\t")
        identifier, maximum = int(identifier), int(maximum)
        filename = bytes.fromhex(filename).decode("utf-8")
        if (
            not 0 < identifier < 65535 or maximum < 0 or not filename.startswith(root)
            or identifier in identifiers or filename in _sources
        ):
            raise ValueError("invalid child source bounds")
        identifiers.add(identifier)
        _sources[filename] = (identifier, maximum)
        _sources[os.path.normcase(filename)] = (identifier, maximum)
    if not _sources:
        raise ValueError("empty child coverage source map")
    _phase("source-map-ready")
    auditor = _auditor()
    owner = auditor.owner_pid()
    _phase("owner-checked")
    auditor.install(control, sys.executable, nonce, child=True, token=context)
    _phase("audit-installed")
    if auditor.context(dict(os.environ)) != context:
        raise ValueError("child context differs from owned manifest")
    _phase("context-rechecked")
    arguments = auditor.argv_key(sys.orig_argv)
    _phase("argv-digest-ready")
    born = time.monotonic_ns()
    path = os.path.join(directory, f"{os.getpid()}-{os.urandom(16).hex()}.hits")
    _fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    _write(f"R {VERSION} {nonce} {context} {os.getpid()} {owner} {arguments} {born}\n".encode("ascii"))
    _started = True
    _phase("stream-header-written")
    with _lock:
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_after_fork)
        _arming = True
        try:
            threading.settrace(_call)
            sys.settrace(_call)
        finally:
            _arming = False
        original = threading.settrace
        def set_thread_trace(function):
            if function is not _call:
                auditor.error("child thread tracer was replaced")
            return original(function)
        threading.settrace = set_thread_trace
        # Publish timing after all required setup, before any line writer can run.
        finished = time.perf_counter_ns()
        finished_cpu = time.process_time_ns() if _phase_reporter is not None else None
        duration = (finished - started) // 1000
        _write(b"\0\0" + duration.to_bytes(4, "big"))
    if _phase_reporter is not None:
        _phase_reporter(control,start_ns=started,end_ns=finished,end_cpu_ns=finished_cpu,duration_us=duration)
