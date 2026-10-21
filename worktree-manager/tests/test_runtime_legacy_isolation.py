"""An already-loaded UV dispatcher cannot modify the qualified runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_installed_runtime import (
    _environment_snapshot,
    _launcher_command,
    _no_visible_windows as _no_visible_windows,
    _shell_executable,
    standalone_runtime as standalone_runtime,
)
from worktree_manager import installed_runtime as runtime
from worktree_manager import self_install as si
from worktree_manager import self_install_payload as payload
from worktree_manager.self_install_recovery import publish_install_artifacts

_BACKEND = '''
import pathlib, zipfile
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    name = "manager_test_fixture-1.2.3"
    wheel = name + "-py3-none-any.whl"
    metadata = name + ".dist-info/"
    files = {
        "manager_test_fixture.pth": str(pathlib.Path(__file__).parent / "src") + "\\n",
        metadata + "METADATA": "Metadata-Version: 2.1\\nName: manager-test-fixture\\nVersion: 1.2.3\\n",
        metadata + "WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    files[metadata + "RECORD"] = "".join(path + ",,\\n" for path in [*files, metadata + "RECORD"])
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel
build_wheel = build_editable
'''


def test_old_inflight_uv_reader_cannot_modify_new_qualified_runtime(
    standalone_runtime, tmp_path, monkeypatch, _no_visible_windows,
):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("actual UV is required for the old in-flight reader contract")
    slot = standalone_runtime
    root = slot.parent.parent
    (slot / "pyproject.toml").write_text(
        "[build-system]\nrequires=[]\nbuild-backend='fixture_backend'\nbackend-path=['.']\n"
        "[project]\nname='manager-test-fixture'\nversion='1.2.3'\nrequires-python='>=3.11'\n",
        encoding="utf-8",
    )
    (slot / "fixture_backend.py").write_text(_BACKEND, encoding="utf-8")
    (slot / "src" / "worktree_manager" / "__main__.py").write_text(
        "import json,sys,worktree_manager\n"
        "print(json.dumps({'version':worktree_manager.__version__,"
        "'prefix':sys.prefix,'module':worktree_manager.__file__}))\n",
        encoding="utf-8",
    )
    si._mark_slot_complete(slot)
    si._write_marker(root, "0.9.0")
    monkeypatch.setattr(si, "local_bin", lambda: tmp_path / "bin")
    monkeypatch.setattr(si, "control_plane_providers_dir", lambda: tmp_path / "providers")
    for name, value in {
        "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON": sys.executable, "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    before = _environment_snapshot(slot)
    ready, proceed = tmp_path / "ready", tmp_path / "proceed"
    code = """
import json,os,pathlib,subprocess,sys,time
root,ready,proceed=map(pathlib.Path,sys.argv[1:4])
ready.write_text('old dispatcher loaded')
deadline=time.monotonic()+20
while not proceed.exists():
    if time.monotonic()>deadline: raise RuntimeError('publication gate timed out')
    time.sleep(.01)
slot=root/'versions'/(root/'current-version').read_text().strip()
result=subprocess.run([sys.argv[4],'run','--quiet','--project',str(slot),
    '-m','worktree_manager','--version'],capture_output=True,text=True,
    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
print(result.stdout,end='')
print(result.stderr,end='',file=sys.stderr)
raise SystemExit(result.returncode)
"""
    with ThreadPoolExecutor(max_workers=1) as executor:
        old = executor.submit(
            runtime._owned_runner,
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", code,
             str(root), str(ready), str(proceed), uv],
            cwd=tmp_path, timeout=30,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if old.done():
                    pytest.fail(str(old.result()))
                time.sleep(.01)
            assert ready.exists(), "old dispatcher did not reach its pre-selection gate"
            lease, _ = si._acquire_install_cutover_lease(root)
            try:
                publish_install_artifacts(root, "1.2.3")
            finally:
                lease.release()
            assert not (slot / ".venv").exists()
        finally:
            proceed.write_text("new selection published", encoding="utf-8")
        result = old.result(timeout=35)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["version"] == "1.2.3"
    assert os.path.samefile(observed["prefix"], slot / ".venv")
    assert os.path.samefile(observed["module"], slot / "src" / "worktree_manager" / "__init__.py")
    assert _environment_snapshot(slot) == before
    runtime.probe_runtime(slot, "1.2.3")
    shells = [
        (shell, _shell_executable(shell))
        for shell in (("cmd", "powershell", "pwsh", "bash") if os.name == "nt" else ("bash",))
    ]
    monkeypatch.setenv("WORKTREE_MANAGER_ROOT", str(root))
    monkeypatch.setenv("PATH", "")
    for shell, executable in shells:
        assert executable, f"native {shell} is required for this explicit lane"
        for _ in range(2):
            current = subprocess.run(
                _launcher_command(shell, executable, si.local_bin(), ["--version"]),
                cwd=tmp_path, env=dict(os.environ), capture_output=True, text=True,
                encoding="utf-8", timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert current.returncode == 0, current.stdout + current.stderr
            assert current.stdout.strip() == "worktree-manager 1.2.3"
            assert _environment_snapshot(slot) == before


@pytest.mark.parametrize("directory", [".venv", runtime.RUNTIME_DIR])
def test_payload_copy_never_inherits_source_environments(tmp_path, monkeypatch, directory):
    from test_installed_runtime import _fake_build, _source

    source = _source(tmp_path / "source")
    (source / directory).mkdir()
    (source / directory / "source-only").write_text("must not be published", encoding="utf-8")
    monkeypatch.setattr(payload, "build_runtime", _fake_build)
    monkeypatch.setattr(payload, "probe_runtime", lambda *_: None)
    slot = tmp_path / "slot"
    payload._copy_payload(source, slot)
    assert not (slot / directory / "source-only").exists()
    assert runtime.runtime_python(slot).is_file()


@pytest.mark.parametrize("marker", [b"", b"platform-runtime-v1\n", b"unknown-format\n", b"\xff"])
def test_prior_completion_formats_do_not_qualify_the_isolated_layout(
    standalone_runtime, marker,
):
    slot = standalone_runtime
    (slot / ".install-complete").write_bytes(marker)
    root = slot.parent.parent
    si._write_marker(root, "1.2.3")
    assert not si._slot_is_complete(slot)
    assert si.needs_install("1.2.3", root)
    si._mark_slot_complete(slot)
    assert si._slot_is_complete(slot)
