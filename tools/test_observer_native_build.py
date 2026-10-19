"""Source-only build guards; no compiler, native loading, workload or Docker."""

import ctypes
import hashlib
import os
from pathlib import Path
import struct
from types import SimpleNamespace
import zipfile

import pytest

from tools.coverage_guided_selection import observer_native_build as build

ENTRY = "1" * 64


def wheel(tmp_path, monkeypatch, entries=None):
    path = tmp_path / build.WHEEL
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in (entries or {
            "ziglang/zig": b"compiler-not-executed",
            "ziglang/lib/include/stdio.h": b"header",
            "ziglang/lib/libc/include/any-windows-any/example.h": b"not-pruned",
            "ziglang/lib/libcxx/header": b"not-pruned-either",
            "ziglang-0.14.1.dist-info/METADATA": b"metadata-not-installed",
        }).items():
            archive.writestr(name, value)
    monkeypatch.setattr(build, "WHEEL_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def test_fresh_extraction_keeps_complete_library_tree_and_never_installs_metadata(tmp_path, monkeypatch):
    source = wheel(tmp_path, monkeypatch)
    output = tmp_path / "toolchain"
    observed = []
    original = build.zipfile.ZipFile
    def same_file(file, *args, **kwargs):
        observed.append(hasattr(file, "read") and not file.closed)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(build.zipfile, "ZipFile", same_file)
    result = build.extract_toolchain(source, output)
    assert observed == [True]
    assert Path(result["compiler"]).read_bytes() == b"compiler-not-executed"
    assert (output / "ziglang/lib/libc/include/any-windows-any/example.h").read_bytes() == b"not-pruned"
    assert (output / "ziglang/lib/libcxx/header").read_bytes() == b"not-pruned-either"
    assert not (output / "ziglang-0.14.1.dist-info").exists()
    assert result["extracted_files"] == 4
    assert result["extracted_bytes"] == sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    with pytest.raises(FileExistsError):
        build.extract_toolchain(source, output)
    assert Path(result["compiler"]).is_file()


def test_wheel_hash_refuses_before_allocating_toolchain(tmp_path, monkeypatch):
    source = wheel(tmp_path, monkeypatch)
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="wheel identity"):
        build.extract_toolchain(source, tmp_path / "toolchain")
    assert not (tmp_path / "toolchain").exists()


def test_changed_open_wheel_after_extraction_refuses_and_cleans(tmp_path, monkeypatch):
    source = wheel(tmp_path, monkeypatch)
    original = build.hashlib.file_digest
    calls = []
    def changed_on_second_read(file, algorithm):
        calls.append(file)
        actual = original(file, algorithm)
        return actual if len(calls) == 1 else SimpleNamespace(hexdigest=lambda: "0" * 64)
    monkeypatch.setattr(build.hashlib, "file_digest", changed_on_second_read)
    with pytest.raises(ValueError, match="changed during extraction"):
        build.extract_toolchain(source, tmp_path / "toolchain")
    assert len(calls) == 2 and calls[0] is calls[1] and calls[0].closed
    assert not (tmp_path / "toolchain").exists()


@pytest.mark.parametrize("entries", [
    {"ziglang/lib/../escape": b"no"},
    {"ziglang/zig": b"no-library-tree"},
    {"ziglang/lib/include/header": b"no-compiler"},
])
def test_invalid_or_partial_archive_refuses_and_cleans(tmp_path, monkeypatch, entries):
    source = wheel(tmp_path, monkeypatch, entries)
    with pytest.raises(ValueError):
        build.extract_toolchain(source, tmp_path / "toolchain")
    assert not (tmp_path / "toolchain").exists()


def test_partial_write_failure_cleans_and_rethrows_original_error(tmp_path, monkeypatch):
    source = wheel(tmp_path, monkeypatch)
    error = OSError("injected extraction failure")
    def fail(*args):
        raise error
    monkeypatch.setattr(build.shutil, "copyfileobj", fail)
    with pytest.raises(OSError) as caught:
        build.extract_toolchain(source, tmp_path / "toolchain")
    assert caught.value is error
    assert not (tmp_path / "toolchain").exists()


def elf_content():
    data = bytearray(1024)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", data, 16, 3, 62)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 2)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, len(data), len(data), 4096)
    struct.pack_into("<IIQQQQQQ", data, 120, 2, 6, 512, 512, 512, 96, 96, 8)
    for offset, pair in enumerate([(25, 768), (27, 8), (7, 800), (8, 24), (9, 24), (0, 0)]):
        struct.pack_into("<qQ", data, 512 + offset * 16, *pair)
    struct.pack_into("<QQq", data, 800, 768, 8, 896)
    data[912:976] = ENTRY.encode()
    return data


def test_artifact_requires_actual_constructor_relocation_and_entry_identity(tmp_path):
    artifact = tmp_path / "helper.so"
    artifact.write_bytes(elf_content())
    result = build.verify_artifact(artifact, ENTRY)
    assert result["bytes"] == 1024 and result["constructor_entries"] == 1
    assert result["static_shape_only"] and "all_admitted" not in result


@pytest.mark.parametrize("fault", ["missing-init", "unresolved-init", "changed-entry", "unknown-relocation", "wrong-abi"])
def test_missing_or_changed_native_initialization_refuses(tmp_path, fault):
    data = elf_content()
    if fault == "missing-init":
        struct.pack_into("<Q", data, 512 + 8, 0)
    elif fault == "unresolved-init":
        struct.pack_into("<q", data, 800 + 16, 5000)
    elif fault == "changed-entry":
        data[912:976] = b"2" * 64
    elif fault == "unknown-relocation":
        struct.pack_into("<Q", data, 800 + 8, 7)
    else:
        struct.pack_into("<H", data, 18, 183)
    artifact = tmp_path / "helper.so"
    artifact.write_bytes(data)
    with pytest.raises(ValueError):
        build.verify_artifact(artifact, ENTRY)


@pytest.fixture
def provider(tmp_path, monkeypatch):
    path = tmp_path / "runtime.so"
    path.write_bytes(elf_content())
    concrete = type(Path())
    class ProtectedPath(concrete):
        weak = False
        def stat(self, *args, **kwargs):
            actual = super().stat(*args, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=0o100777 if self.weak else 0o100755,
                                   st_dev=actual.st_dev, st_ino=actual.st_ino, st_size=actual.st_size)
    monkeypatch.setattr(build, "Path", ProtectedPath)
    return path, {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "device": path.stat().st_dev, "inode": path.stat().st_ino,
                  "symbols": list(build.RUNTIME_SYMBOLS)}, ProtectedPath


def test_runtime_providers_come_from_real_pointer_provider_queries(provider, monkeypatch):
    path, expected, protected = provider
    calls = []
    class Dladdr:
        def __call__(self, pointer, info):
            calls.append(pointer.value)
            info._obj.path = os.fsencode(path)
            return 1
    class Process:
        dladdr = Dladdr()
        def __getattr__(self, name):
            if name not in build.RUNTIME_SYMBOLS:
                raise AttributeError(name)
            return ctypes.c_void_p(4096 + build.RUNTIME_SYMBOLS.index(name))
    monkeypatch.setattr(build.sys, "platform", "linux")
    monkeypatch.setattr(build.ctypes, "CDLL", lambda name: Process())
    actual = build.runtime_link_inputs()
    assert actual == [expected] and len(calls) == len(build.RUNTIME_SYMBOLS)
    build.verify_runtime_inputs(actual)
    protected.weak = True
    with pytest.raises(ValueError, match="unprotected"):
        build.runtime_link_inputs()


@pytest.mark.parametrize("fault", ["missing-symbol", "duplicate-symbol", "changed-bytes", "changed-inode", "weak-permissions"])
def test_missing_or_changed_runtime_link_inputs_refuse(provider, fault):
    path, row, protected = provider
    if fault == "missing-symbol":
        row["symbols"].pop()
    elif fault == "duplicate-symbol":
        row["symbols"].append(row["symbols"][0])
    elif fault == "changed-bytes":
        path.write_bytes(bytes(elf_content()) + b"changed")
    elif fault == "changed-inode":
        row["inode"] += 1
    else:
        protected.weak = True
    with pytest.raises(ValueError):
        build.verify_runtime_inputs([row])


def test_missing_native_constructor_fails_setup_before_consumer_and_cleans(tmp_path):
    from tools.coverage_guided_selection.observer_lifecycle import OwnedObserverLifecycle
    artifact = tmp_path / "helper.so"
    content = elf_content()
    struct.pack_into("<Q", content, 512 + 8, 0)
    artifact.write_bytes(content)
    calls = []
    def cleanup(session):
        artifact.unlink(missing_ok=True)
        calls.append("cleanup")
    lifecycle = OwnedObserverLifecycle(lambda:build.verify_artifact(artifact, ENTRY),
        lambda:pytest.fail("capture after native initialization refusal"),
        lambda session:pytest.fail("admission after native initialization refusal"),cleanup)
    assert not lifecycle.begin()
    result = lifecycle.complete()
    assert not result["all_admitted"] and result["observer_setup_refused"]
    assert result["capture_started_ns"] is None and not artifact.exists()
    assert calls == ["cleanup", "cleanup"]


def test_build_variant_changes_only_helper_link_argv_without_fallback(tmp_path, provider, monkeypatch):
    path, row, protected = provider
    source = tmp_path / "entry.c"
    source.write_bytes(b"same-source")
    monkeypatch.setattr(build, "C_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    old = build.build_argv("/compiler/zig", source, "/owned/helper.so", ENTRY, variant="original")
    new = build.build_argv("/compiler/zig", source, "/owned/helper.so", ENTRY,
                          variant="runtime-link-only", providers=[row])
    assert old[:-1] == new[:len(old)-1]
    assert old[-1] == "-ldl"
    assert new[len(old)-1:] == ["-nostdlib", "-nostartfiles", "-nodefaultlibs", "-Wl,-z,defs", str(path)]
    with pytest.raises(ValueError, match="missing observer runtime"):
        build.build_argv("/compiler/zig", source, "/owned/helper.so", ENTRY,
                         variant="runtime-link-only")
    with pytest.raises(ValueError, match="unknown"):
        build.build_argv("/compiler/zig", source, "/owned/helper.so", ENTRY, variant="fallback")
    source.write_bytes(b"changed-source")
    with pytest.raises(ValueError, match="source or entry"):
        build.build_argv("/compiler/zig", source, "/owned/helper.so", ENTRY, variant="original")
