"""Bootstrap admission, cleanup and inherited-stream contracts."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_bootstrap_adapters_require_stdlib_toml_and_import_without_site_packages(tmp_path):
    payload = Path(__file__).resolve().parents[1]
    for name in ("bootstrap.ps1", "bootstrap.sh"):
        body = (payload / name).read_text(encoding="utf-8")
        assert "--no-project --python '>=3.11'" in body
    if sys.version_info < (3, 11):
        pytest.skip("the selected bootstrap requires Python 3.11+, not the runtime's 3.10 floor")
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", "-B", "-c",
            "import runpy,sys;"
            "assert sys.version_info >= (3,11);"
            "runpy.run_path(sys.argv[1],run_name='dependency_free_bootstrap');"
            "from worktree_manager import _trusted_pointer_materializer;"
            "assert 'tomli' not in sys.modules",
            str(payload / "scripts" / "bootstrap_runtime.py"),
        ],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_runtime.py"
    module = types.ModuleType("manager_bootstrap_fixture")
    module.__file__ = str(script)
    before = sys.path.copy()
    try:
        exec(compile(script.read_bytes(), str(script), "exec"), module.__dict__)
    finally:
        sys.path[:] = before
    root = tmp_path / "manager"
    monkeypatch.setattr(module, "default_root", lambda: root)
    monkeypatch.setattr(module, "payload_version", lambda _: "1.2.3")
    monkeypatch.setattr(module, "needs_install", lambda *_: True)
    return module, root


def test_bootstrap_construction_failure_cleans_runner_and_never_launches(bootstrap, monkeypatch, capsys):
    module, root = bootstrap

    def refuse(*_):
        raise RuntimeError("construction refused")

    monkeypatch.setattr(module, "_copy_payload", refuse)
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected launch"))
    assert module.main(["--version"]) == 1
    assert "construction refused" in capsys.readouterr().err
    assert not list(root.iterdir())


def test_bootstrap_install_failure_preserves_exit_and_streams(bootstrap, monkeypatch, capsys):
    module, root = bootstrap
    monkeypatch.setattr(module, "_copy_payload", lambda *_: None)
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=7, stdout="construction output\n", stderr="failure detail\n")

    monkeypatch.setattr(module, "_owned_runner", run)
    assert module.main(["--version"]) == 7
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["self-install", "--apply"]
    assert calls[0][1]["cwd"].is_relative_to(root)
    assert calls[0][1]["timeout"] == 600
    observed = capsys.readouterr()
    assert observed.out == "construction output\n"
    assert observed.err == "failure detail\n"
    assert not list(root.iterdir())


def test_bootstrap_retains_runner_when_cleanup_is_unconfirmed(bootstrap, monkeypatch, capsys):
    module, root = bootstrap

    def unconfirmed(*_):
        raise module.CleanupError("construction broker cleanup is unconfirmed")

    monkeypatch.setattr(module, "_copy_payload", unconfirmed)
    assert module.main(["--version"]) == 1
    retained = list(root.glob(".bootstrap.*"))
    assert len(retained) == 1
    assert f"retained {retained[0]}" in capsys.readouterr().err


def test_copy_retains_staging_when_cleanup_is_unconfirmed(tmp_path, monkeypatch):
    from worktree_manager import self_install as si, self_install_payload as payload

    source = tmp_path / "payload"
    source.mkdir()
    (source / "pyproject.toml").write_text("fixture")
    slot = tmp_path / "1.2.3"
    monkeypatch.setattr(si, "_SLOT_KEY_FILES", ["pyproject.toml"])
    monkeypatch.setattr(si, "payload_version", lambda _: "1.2.3")
    monkeypatch.setattr(payload, "_materialize_payload_pointers", lambda *_: None)

    def unconfirmed(_):
        raise payload.CleanupError("construction group still owned")

    monkeypatch.setattr(payload, "build_runtime", unconfirmed)
    with pytest.raises(payload.CleanupError, match="still owned"):
        payload._copy_payload(source, slot)
    retained = list(tmp_path.glob("1.2.3.staging.*"))
    assert len(retained) == 1
    assert (retained[0] / "pyproject.toml").is_file()
    assert not slot.exists()


def test_swap_retains_both_paths_when_final_probe_custody_is_unconfirmed(tmp_path):
    from worktree_manager import self_install_payload as payload

    staging, slot = tmp_path / "staging", tmp_path / "1.2.3"
    staging.mkdir()
    slot.mkdir()
    (staging / "candidate").touch()
    (slot / "previous").touch()
    marker = tmp_path / "current-version"
    marker.write_text("previous-version")

    def unconfirmed(_):
        raise payload.CleanupError("final probe cleanup unconfirmed")

    with pytest.raises(payload.CleanupError, match="unconfirmed"):
        payload._swap_in_staged_slot(staging, slot, validate=unconfirmed)
    retired = list(tmp_path.glob("1.2.3.retired.*"))
    assert len(retired) == 1
    assert (retired[0] / "previous").is_file()
    assert (slot / "candidate").is_file()
    assert marker.read_text() == "previous-version"


def test_bootstrap_current_install_skips_construction_and_propagates_launch_exit(
    bootstrap, monkeypatch,
):
    module, root = bootstrap
    monkeypatch.setattr(module, "needs_install", lambda *_: False)
    monkeypatch.setattr(module, "_copy_payload", lambda *_: pytest.fail("unnecessary construction"))
    monkeypatch.setattr(module, "current_version", lambda _: "1.2.3")
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=9)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main(["two words"]) == 9
    assert calls[0][0] == [
        str(module.runtime_python(module.version_slot("1.2.3", root))),
        "-I", "-B", "-X", "utf8", "-m", "worktree_manager", "two words",
    ]
    assert calls[0][1]["stdout"] is sys.stdout
    assert calls[0][1]["stderr"] is sys.stderr


@pytest.mark.skipif(os.name == "nt", reason="native POSIX nested constructor custody")
@pytest.mark.parametrize(
    "outcome", ["outer-timeout", "outer-interrupt", "outer-foreign-home", "escaped-control"],
)
def test_nested_constructor_stops_before_bootstrap_runner_cleanup(
    tmp_path, outcome,
):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).parent / "fixtures" / "nested_custody.py"),
         str(root), str(tmp_path), outcome],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["outcome"] == outcome
    assert observed["exited_before_cleanup"] == (outcome != "escaped-control")
    assert observed.get("negative_control_detected", False) == (outcome == "escaped-control")


def test_bootstrap_retains_runner_when_descendant_cleanup_is_unconfirmed(
    bootstrap, monkeypatch, capsys,
):
    module, root = bootstrap
    monkeypatch.setattr(module, "_copy_payload", lambda *_: None)

    def unconfirmed(*_a, **_k):
        raise module.CleanupError("cleanup not confirmed")

    monkeypatch.setattr(module, "_owned_runner", unconfirmed)
    monkeypatch.setattr(module, "_launch", lambda *_: pytest.fail("unqualified launch"))
    assert module.main(["--version"]) == 1
    assert len(list(root.glob(".bootstrap.*"))) == 1
    assert "cleanup unconfirmed; retained" in capsys.readouterr().err
