"""Read-only admission shares governance without publishing advisory state."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from test_installation_mode_governance import (
    PLUGIN_ID,
    _activation,
    _api_arguments,
    _cell_layout,
    _load_module,
    _policy,
    _profile_environment,
    _snapshot,
    _write_json,
)

pytestmark = pytest.mark.installation_context_smoke


def _evidence(root: Path) -> tuple[
    dict[str, tuple[str, int, str]], dict[str, tuple[int, int]]
]:
    return _snapshot(root), {
        path.relative_to(root).as_posix(): (
            path.lstat().st_ino,
            path.lstat().st_mtime_ns,
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("existing_pointer", [False, True])
@pytest.mark.parametrize(
    ("state", "expected_status", "expected_mode"),
    [
        ("ordinary", "ready", "legacy"),
        ("legacy", "ready", "legacy"),
        ("active", "ready", "namespaced"),
        ("stale", "revalidation-required", "namespaced"),
        ("invalid", "invalid", None),
        ("foreign", "foreign-environment", None),
        ("migration", "migration-required", "legacy"),
        ("invalid-policy", "invalid", "legacy"),
    ],
)
def test_inspection_preserves_all_files_and_resolution_semantics(
    tmp_path: Path, existing_pointer: bool, state: str,
    expected_status: str, expected_mode: str | None,
) -> None:
    module = _load_module()
    layout = _cell_layout(tmp_path)
    profile = tmp_path / "profile"
    legacy = tmp_path / "legacy"
    profile.mkdir()
    legacy.mkdir()
    if state == "ordinary":
        layout["durable"] = tmp_path / "unprovisioned-durable-home"
    if state in {"active", "stale", "invalid", "foreign", "migration"}:
        _write_json(
            profile / ".copilot-extensions" / "installation-mode.json",
            _policy(True, str(layout["marketplace_id"])),
        )
    if state in {"active", "stale", "invalid", "foreign"}:
        environment = _profile_environment(profile)
        if state == "foreign":
            environment = {**environment, "homeRealPath": str(tmp_path / "other-profile")}
        activation = _activation(
            layout, environment=environment,
            install_generation=1 if state == "stale" else None,
        )
        if state == "invalid":
            activation.write_text("{", encoding="utf-8")
    if state == "invalid-policy":
        _write_json(
            profile / ".copilot-extensions" / "installation-mode.json",
            {"schema": "invalid"},
        )
    pointer = Path(layout["durable"]) / PLUGIN_ID / "runtime-root"
    if existing_pointer:
        pointer.parent.mkdir(parents=True)
        pointer.write_text(str(tmp_path / "retained-runtime") + "\n", encoding="utf-8")
    arguments = _api_arguments(
        layout, profile, legacy, platform=_profile_environment(profile)["platform"]
    )
    before = _evidence(tmp_path)
    inspected = module.inspect_installation_mode(**arguments)
    assert _evidence(tmp_path) == before
    assert inspected["status"] == expected_status
    assert inspected["actualMode"] == expected_mode

    resolved = module.resolve_installation_mode(**arguments)
    assert inspected == resolved
    if expected_status == "ready":
        assert pointer.read_text(encoding="utf-8") == resolved["runtimeRoot"] + "\n"
    after_resolution = _evidence(tmp_path)
    assert module.inspect_installation_mode(**arguments) == resolved
    assert _evidence(tmp_path) == after_resolution


def test_inspection_refuses_malformed_arguments_without_publication(tmp_path: Path) -> None:
    module = _load_module()
    layout = _cell_layout(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    before = _evidence(tmp_path)
    with pytest.raises(module.InstallationContextError, match="absolute"):
        module.inspect_installation_mode(
            **_api_arguments(layout, profile, Path("relative-legacy"))
        )
    assert _evidence(tmp_path) == before


@pytest.mark.parametrize(
    "consumer",
    [
        "agent-bridge", "agent-codespaces", "agent-containers", "agent-dispatch",
        "agent-machines", "agent-index", "agent-logger", "agent-worktrees",
        "worktree-manager",
    ],
)
def test_vendored_consumer_first_touch_inspection_is_nonmutating(
    tmp_path: Path, consumer: str,
) -> None:
    repo = Path(__file__).resolve().parents[3]
    package = consumer.replace("-", "_")
    root = repo / consumer if consumer == "worktree-manager" else repo / "plugins" / consumer
    entry = root / "src" / package / "_installation_context.py"
    spec = importlib.util.spec_from_file_location(f"inspection_{package}", entry)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        layout = _cell_layout(tmp_path)
        layout["durable"] = tmp_path / "absent-durable-home"
        profile = tmp_path / "profile"
        profile.mkdir()
        legacy = tmp_path / "absent-legacy-root"
        before = _evidence(tmp_path)
        result = module.inspect_installation_mode(
            **_api_arguments(
                layout, profile, legacy,
                platform=_profile_environment(profile)["platform"],
            )
        )
        assert result["status"] == "ready"
        assert result["actualMode"] == "legacy"
        assert result["runtimeRoot"] == str(legacy)
        assert _evidence(tmp_path) == before
        assert not Path(layout["durable"]).exists()
        assert not legacy.exists()
    finally:
        sys.modules.pop(spec.name, None)
