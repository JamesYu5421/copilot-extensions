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
_root = ""
_control = None
_contexts = {}
_inventory = {}
_nonce = None
_arming = False
_helpers = {}


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


def _line(frame, event, arg):
    if event == "line" and not _failed:
        source = _source(frame.f_code.co_filename)
        if source is not None:
            identifier, maximum = source
            line = frame.f_lineno
            if line <= 0:
                return _line
            with _lock:
                key = (identifier, line)
                if key not in _seen:
                    if not 0 < line <= maximum or len(_seen) >= MAX_RECORDS:
                        _fail()
                    else:
                        _seen.add(key)
                        try:
                            _write(identifier.to_bytes(2, "big") + line.to_bytes(4, "big"))
                        except OSError:
                            _fail()
    return _line


def _call(frame, event, arg):
    return _line if _source(frame.f_code.co_filename) is not None else None


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
    global _started, _fd, _root, _control, _arming
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
    started = int(os.environ.get("_CGS_PRELUDE_NS", time.perf_counter_ns()))
    with open(value, "rb") as stream:
        content = stream.read(MAX_MANIFEST + 1)
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
    auditor = _auditor()
    owner = auditor.owner_pid()
    auditor.install(control, sys.executable, nonce, child=True, token=context)
    if auditor.context(dict(os.environ)) != context:
        raise ValueError("child context differs from owned manifest")
    arguments = auditor.argv_key(sys.orig_argv)
    born = time.monotonic_ns()
    path = os.path.join(directory, f"{os.getpid()}-{os.urandom(16).hex()}.hits")
    _fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    _write(f"R {VERSION} {nonce} {context} {os.getpid()} {owner} {arguments} {born}\n".encode("ascii"))
    _started = True
    # ID zero is the one initialization-duration record (microseconds).
    duration = (time.perf_counter_ns() - started) // 1000
    _write(b"\0\0" + duration.to_bytes(4, "big"))
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


def source_inventory(mapping):
    """Parent-only helpers import heavier libraries lazily, never at child startup."""
    import hashlib
    from pathlib import Path

    if len(mapping) >= 65535 or len(set(mapping.values())) != len(mapping):
        raise ValueError("ambiguous or oversized child source inventory")
    result = {}
    for identifier, (installed, canonical) in enumerate(sorted(mapping.items()), 1):
        path = Path(installed)
        content = path.read_bytes()
        result[identifier] = {
            "installed": str(path), "canonical": canonical,
            "sha256": hashlib.sha256(content).hexdigest(),
            "lines": len(content.splitlines()),
        }
    return result


def manifest_text(*, nonce, context, directory, inventory):
    from pathlib import Path

    root = os.path.commonpath([str(Path(row["installed"]).parent) for row in inventory.values()])
    root = root + os.sep
    header = "\t".join([
        VERSION, nonce, context, str(directory).encode().hex(), root.encode().hex(),
    ])
    rows = [header] + [
        f"{identifier}\t{row['lines']}\t{row['installed'].encode().hex()}"
        for identifier, row in sorted(inventory.items(), key=lambda item: int(item[0]))
    ]
    return "\n".join(rows) + "\n"


def write_manifest(path, **options):
    content = manifest_text(**options).encode("utf-8")
    if len(content) > MAX_MANIFEST:
        raise ValueError("child manifest exceeds bound")
    path.write_bytes(content)


def read_streams(directory, *, nonce, contexts, inventory, audit_control):
    """Refuse unknown sources/contexts, malformed/truncated/oversized evidence."""
    import hashlib
    import re
    from pathlib import Path

    for row in inventory.values():
        if hashlib.sha256(Path(row["installed"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("installed source changed after tracing")
    manifests = Path(audit_control) / "manifests"
    if not manifests.is_dir() or {p.name for p in manifests.iterdir()} != {
        token + ".manifest" for token in contexts
    }:
        raise ValueError("unknown or missing test context manifest")
    for token in contexts:
        path = manifests / (token + ".manifest")
        if path.is_symlink():
            raise ValueError("linked test context manifest")
        with path.open("rb") as handle:
            content = handle.read(MAX_MANIFEST + 1)
        expected = manifest_text(
            nonce=nonce, context=token, directory=directory, inventory=inventory,
        ).encode("utf-8")
        if content != expected:
            raise ValueError("test context/source manifest changed")
    coverage, preludes, producers, zero_hits = {}, [], [], 0
    files = list(directory.iterdir())
    if len(files) > 4096:
        raise ValueError("unexpected child stream count")
    for path in files:
        maximum = 512 + 6 * (MAX_RECORDS + 1)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            raise ValueError("unsafe or oversized child stream")
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("child stream grew beyond bound")
        header, separator, payload = data.partition(b"\n")
        try:
            status, version, run, context, pid, owner, arguments, born = header.decode("ascii").split(" ")
        except (ValueError, UnicodeError) as exc:
            raise ValueError("malformed child stream header") from exc
        if (
            not separator or len(header) > 512 or status != "R" or version != VERSION or run != nonce
            or context not in contexts or not pid.isdigit() or int(pid) <= 0
            or not owner.isdigit() or not born.isdigit()
            or not 0 < int(pid) < 2**32 or not 0 < int(owner) < 2**32
            or not 0 < int(born) <= time.monotonic_ns()
            or not re.fullmatch("[0-9a-f]{64}", arguments)
            or not re.fullmatch(re.escape(pid) + r"-[0-9a-f]{32}\.hits", path.name)
            or len(payload) < 6 or len(payload) % 6 or len(payload) // 6 > MAX_RECORDS + 1
            or payload[:2] != b"\0\0"
        ):
            raise ValueError("invalid child stream identity/version/record framing")
        preludes.append(int.from_bytes(payload[2:6], "big") / 1_000_000)
        producers.append((context, int(pid), int(owner), arguments, int(born)))
        zero_hits += len(payload) == 6
        seen = set()
        for offset in range(6, len(payload), 6):
            identifier = int.from_bytes(payload[offset:offset + 2], "big")
            line = int.from_bytes(payload[offset + 2:offset + 6], "big")
            if identifier not in inventory or not 0 < line <= inventory[identifier]["lines"]:
                raise ValueError("child record outside verified source bounds")
            if (identifier, line) in seen:
                raise ValueError("duplicate sparse child record")
            seen.add((identifier, line))
            file = inventory[identifier]["canonical"]
            coverage.setdefault(file, {}).setdefault(str(line), set()).add(contexts[context])
    count = _auditor().verify(audit_control, nonce=nonce, contexts=contexts, producers=producers)
    return {
        "coverage": {
            file: {line: sorted(nodes) for line, nodes in lines.items()}
            for file, lines in coverage.items()
        },
        "prelude_seconds": preludes, "child_streams": len(files),
        "audited_producers": count, "zero_hit_producers": zero_hits, "audit_complete": True,
    }


def merge_coverage(parent, child):
    """Union into the existing file -> line -> test-node schema, never replace."""
    for file, lines in child.items():
        for line, nodes in lines.items():
            target = parent.setdefault(file, {})
            target[line] = sorted(set(target.get(line, ())) | set(nodes))
    return parent


def configure_parent(control, python, nonce, inventory, helpers):
    global _control, _nonce, _inventory, _helpers
    _control, _nonce = control, nonce
    _helpers = helpers
    _inventory = {int(identifier): row for identifier, row in inventory.items()}
    for name in ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG"):
        os.environ.pop(name, None)
    _auditor().install(control, python, nonce)


def register_context(node):
    import hashlib
    from pathlib import Path
    token = hashlib.sha256(node.encode()).hexdigest()
    if token in _contexts and _contexts[token] != node:
        raise ValueError("test-context digest collision")
    _contexts[token] = node
    path = Path(_control) / "manifests" / (token + ".manifest")
    write_manifest(
        path, nonce=_nonce, context=token, directory=Path(_control) / "hits", inventory=_inventory,
    )
    os.environ[ENV] = str(path)


def finish_parent():
    import hashlib
    from pathlib import Path
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected
           for path, expected in _helpers.items()):
        raise ValueError("child instrumentation source changed")
    return read_streams(
        Path(_control) / "hits", nonce=_nonce, contexts=_contexts, inventory=_inventory,
        audit_control=_control,
    )
