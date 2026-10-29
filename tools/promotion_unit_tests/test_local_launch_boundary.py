"""Join the Worktrees argument producer to Bridge's consumer without process I/O."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    package_name = "promotion_launch_worktrees"
    source = ROOT / "plugins/agent-worktrees/src/agent_worktrees"
    package = ModuleType(package_name)
    package.__path__ = [str(source)]
    monkeypatch.setitem(sys.modules, package_name, package)
    installer = ModuleType(f"{package_name}.installer")
    installer.install_dir = lambda: tmp_path
    monkeypatch.setitem(sys.modules, installer.__name__, installer)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load(f"{package_name}.native_arguments", source / "native_arguments.py")
    producer = load(f"{package_name}.process_launcher", source / "process_launcher.py")
    consumer = load(
        "promotion_launch_bridge",
        ROOT / "plugins/agent-bridge/src/agent_bridge/local_launch_arguments.py",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(consumer, "_worktrees_setup_script", lambda: scripts / "default-setup.ps1")
    return producer, consumer


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
@pytest.mark.parametrize("mode", ["fresh", "resume", "literal-tail", "terminator-as-value"])
def test_owner_produced_setup_preserves_native_and_setup_arguments(boundary, tmp_path, shell, mode):
    producer, consumer = boundary
    native = ["--model", "configured-model"] if mode == "fresh" else ["--resume=original-head"]
    if mode == "terminator-as-value":
        native += ["--name", "--"]
    tail = ["--", "literal-tail"] if mode in {"literal-tail", "terminator-as-value"} else []
    setup = [
        shell, "-NoProfile", "-File", str(tmp_path / "scripts/default-setup.ps1"),
        "-Recovery", "-ExecutionDirectory", str(tmp_path / "workspace with spaces"),
        "-CopilotPath", str(tmp_path / "backend.ps1"),
    ]
    template = [*setup, *native, *tail]
    original_template = list(template)
    command = producer.quote_powershell_command(template, RuntimeError)
    original_command = list(command)
    additions = [
        "--acp", "--stdio", "--no-auto-update", "--plugin-dir", "prepared path",
        "--name", "literal ' \" $value; &", "--prompt", "", "--prompt", "--resume=not-an-option",
    ]
    original_additions = list(additions)

    result = consumer.append_local_launch_arguments(command, additions)

    assert result[:-2] == setup
    assert result[-2] == "-NativeArgumentsJson"
    assert json.loads(result[-1]) == {"args": [*native, *additions, *tail]}
    assert command == original_command
    assert template == original_template
    assert additions == original_additions
    assert json.loads(command[-1]) == {"args": [*native, *tail]}


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_owner_produced_envelope_corruption_is_not_legacy_fallback(boundary, tmp_path, shell):
    producer, consumer = boundary
    command = producer.quote_powershell_command(
        [shell, "-File", str(tmp_path / "scripts/default-setup.ps1"), "--resume=original-head"],
        RuntimeError,
    )
    command[-1] = '{"args":"not-an-array"}'
    with pytest.raises(RuntimeError, match="Invalid worktree native argument envelope"):
        consumer.append_local_launch_arguments(command, ["--acp", "--stdio"])


@pytest.mark.parametrize("command", [
    ["copilot", "--resume=original-head"],
    ["bash", "/launch/default-setup.sh", "--recovery"],
], ids=["direct", "bash-setup"])
def test_other_owner_launch_forms_preserve_append_semantics(boundary, command):
    producer, consumer = boundary
    additions = ["--acp", "--stdio", "--no-auto-update"]
    assert consumer.append_local_launch_arguments(
        producer.quote_powershell_command(command, RuntimeError), additions,
    ) == [*command, *additions]


@pytest.mark.parametrize("payload", ["opaque", '{"args":["custom-data"]}'])
def test_same_named_custom_script_remains_opaque(boundary, tmp_path, payload):
    producer, consumer = boundary
    script = tmp_path / "custom/default-setup.ps1"
    script.parent.mkdir()
    script.write_text(
        "# agent-worktrees:launch-seed-boundary-v1\nparam([string]$NativeArgumentsJson)\n",
        encoding="utf-8",
    )
    command = ["pwsh", "-File", str(script), "-NativeArgumentsJson", payload]
    assert producer.quote_powershell_command(command, RuntimeError) == command
    assert consumer.append_local_launch_arguments(command, ["--acp"]) == [*command, "--acp"]


def test_adapter_vocabulary_matches_owner(boundary):
    producer, consumer = boundary
    assert consumer.NATIVE_VALUE_OPTIONS == producer._NATIVE_VALUE_OPTIONS
    for option in sorted(consumer.NATIVE_VALUE_OPTIONS):
        assert consumer._insert_native_options(
            [option, "--", "--", "literal-tail"], ["--stdio"],
        ) == [option, "--", "--stdio", "--", "literal-tail"]
