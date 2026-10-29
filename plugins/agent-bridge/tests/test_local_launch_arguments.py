"""Bridge preserves the Worktrees-owned setup boundary when adding ACP options."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agent_procutil import no_window_kwargs

from agent_bridge import transport, venue_plugins


@pytest.fixture(autouse=True)
def admitted_target(monkeypatch):
    # Owner admission is covered by the repository-hosting suites.
    monkeypatch.setattr("agent_bridge.hosting_policy.require_target_hosting", AsyncMock())


def setup_command(native, *, script=r"C:\launch\default-setup.ps1"):
    return [
        "pwsh", "-NoProfile", "-File", script,
        "-ExecutionDirectory", r"C:\workspace with spaces",
        "-NativeArgumentsJson", json.dumps({"args": native, "extra": "retained"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("prepare", [False, True])
async def test_resolved_setup_keeps_final_native_arguments_in_envelope(
    tmp_path, monkeypatch, resume, prepare,
):
    native = ["--resume=original-head"] if resume else ["--model", "configured-model"]
    command = setup_command(native)
    target = transport.SpawnTarget(
        project="example", worktree_id="retained-workspace",
        native_resume_id="original-head" if resume else None,
        local_plugin_preparation=prepare,
        static_plugin_args={"base": ["--allow-all"], "automatic": ["--plugin-dir", "old"]},
        copilot_args=["--allow-all", "--plugin-dir", "old", "--name", "two words"],
    )
    plan = {"launch": {
        "cmd": command, "work_dir": str(tmp_path), "worktree_id": "retained-workspace",
        "env": {"PLAN_VALUE": "retained"},
    }}
    monkeypatch.setattr(transport, "_resolve_worktree", AsyncMock(return_value=plan))
    barrier = AsyncMock(return_value=["--plugin-dir", "prepared path"])
    monkeypatch.setattr(venue_plugins, "prepare_local_plugins", barrier)
    args, cwd, env = await transport.resolve_local_launch(target)
    index = args.index("-NativeArgumentsJson") + 1
    assert index == len(args) - 1
    assert Path(args[0]).stem.casefold() == "pwsh"
    assert args[1:index] == command[1:index]
    assert json.loads(args[index]) == {
        "args": [
            *native, "--acp", "--stdio", "--no-auto-update",
            "--allow-all", "--plugin-dir", "prepared path" if prepare else "old",
            "--name", "two words",
        ],
        "extra": "retained",
    }
    assert json.loads(command[-1])["args"] == native
    assert target.copilot_args == ["--allow-all", "--plugin-dir", "old", "--name", "two words"]
    assert target.worktree_id == "retained-workspace"
    assert cwd == str(tmp_path) and env["PLAN_VALUE"] == "retained"
    assert barrier.await_count == int(prepare)


@pytest.mark.parametrize("command", [
    ["copilot", "--resume=original-head"],
    ["bash", "/launch/default-setup.sh", "--recovery"],
    ["pwsh", "-File", r"C:\launch\default-setup.ps1", "-Recovery"],
    ["custom-launcher", "-NativeArgumentsJson", "opaque"],
    ["pwsh", "-File", r"C:\launch\custom.ps1", "-NativeArgumentsJson", "opaque"],
])
def test_legacy_and_custom_launch_commands_keep_append_semantics(command):
    additions = ["--acp", "--stdio", "--no-auto-update", "--name", "literal ' $value"]
    assert transport._append_local_launch_arguments(command, additions) == command + additions


@pytest.mark.parametrize("payload", ["not-json", "[]", "{}", '{"args":"text"}', '{"args":[1]}'])
def test_invalid_native_envelope_is_not_reinterpreted_as_legacy(payload):
    command = setup_command([])
    command[-1] = payload
    with pytest.raises(RuntimeError, match="Invalid worktree native argument envelope"):
        transport._append_local_launch_arguments(command, ["--acp"])


@pytest.mark.parametrize("suffix", [[], ["{}", "-NativeArgumentsJson", "{}"]])
def test_missing_or_duplicate_envelope_refuses(suffix):
    command = setup_command([])[:-1] + suffix
    with pytest.raises(RuntimeError, match="Invalid worktree native argument envelope"):
        transport._append_local_launch_arguments(command, ["--acp"])


def test_setup_parameter_names_are_case_insensitive():
    command = ["pwsh", "-FILE", r"C:\launch\DEFAULT-SETUP.PS1", "-nativeargumentsjson", '{"args":[]}']
    args = transport._append_local_launch_arguments(command, ["--stdio"])
    assert json.loads(args[-1]) == {"args": ["--stdio"]}


@pytest.mark.skipif(os.name != "nt", reason="native Windows PowerShell setup consumer")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_real_setup_consumer_rejects_old_shape_and_preserves_fixed_arguments(
    tmp_path, monkeypatch, shell,
):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    source = Path(__file__).resolve().parents[2] / "agent-worktrees" / "scripts" / "default-setup.ps1"
    script = tmp_path / "default-setup.ps1"
    shutil.copy2(source, script)
    backend = tmp_path / "capture.ps1"
    backend.write_text(
        "[Console]::Out.Write((ConvertTo-Json -InputObject @($args) -Compress))\n"
        "exit 37\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ, "PATH": "", "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "AGENT_WORKTREES_LAUNCH_RUNTIME_ROOT": str(tmp_path / "absent-runtime"),
    }
    command = [
        executable, "-NoProfile", "-File", str(script), "-Recovery",
        "-CopilotPath", str(backend), "-ExecutionDirectory", str(tmp_path),
        "-NativeArgumentsJson", json.dumps({"args": ["--resume=original-head"]}),
    ]
    additions = ["--acp", "--stdio", "--no-auto-update", "--name", "literal ' $value", "--plugin-dir", ""]
    old = subprocess.run(
        command + additions, cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=20, **no_window_kwargs(),
    )
    assert old.returncode != 0
    assert "Native argument envelope cannot be combined" in old.stderr
    assert not old.stdout

    target = transport.SpawnTarget(
        project="example", worktree_id="retained-workspace", native_resume_id="original-head",
        copilot_args=additions[3:],
    )
    monkeypatch.setattr(transport, "_resolve_worktree", AsyncMock(return_value={"launch": {
        "cmd": command, "work_dir": str(tmp_path), "worktree_id": "retained-workspace", "env": environment,
    }}))
    args, cwd, env = asyncio.run(transport.resolve_local_launch(target))
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=20, **no_window_kwargs(),
    )
    assert result.returncode == 37, result.stderr
    assert json.loads(result.stdout) == ["-C", str(tmp_path), "--resume=original-head", *additions]
