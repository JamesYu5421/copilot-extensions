"""Consume the native argument representation returned by a local workspace owner."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

NATIVE_VALUE_OPTIONS = {
    "-i", "--interactive", "-p", "--prompt", "--model", "--reasoning-effort",
    "--context", "--auto-tier", "--agent", "-n", "--name", "--session-id",
    "--log-dir", "--extension-sdk-path", "--log-level", "--stream", "--output-format",
    "--add-dir", "--attachment", "--disable-mcp-server", "--add-github-mcp-toolset",
    "--add-github-mcp-tool", "--plugin-dir", "--additional-mcp-config", "--mcp-github-auth",
    "--max-autopilot-continues", "--mode", "--environment-host", "--environment-compute-id",
    "--dynamic-retrieval", "--enable-mcp-server", "--max-ai-credits", "--usage-output-file",
}


def _worktrees_setup_script() -> Path:
    from . import _peer_launch, session_lifecycle_cli
    from .install_paths import uses_legacy_install_dir

    own_root = session_lifecycle_cli._agent_bridge_owner_root()
    context = session_lifecycle_cli._agent_bridge_installation_context()
    if context:
        try:
            owner = _peer_launch.validate_owner("agent-bridge", own_root, context)
        except (OSError, ValueError, ImportError) as exc:
            raise _peer_launch.ContextRefused(f"agent-bridge installation context refused: {exc}") from exc
        root = Path(owner["cellRoot"]) / "plugins" / "agent-worktrees"
    else:
        if not uses_legacy_install_dir(own_root):
            raise _peer_launch.ContextRefused("non-legacy bridge requires an installation context")
        root = Path.home() / ".agent-worktrees"  # marketplace-isolation: allow legacy-compatibility
    return root / "scripts" / "default-setup.ps1"


def _owned_setup_index(command: list[str]) -> int | None:
    if not any(value.casefold() == "-nativeargumentsjson" for value in command):
        return None
    if not command or PureWindowsPath(command[0]).stem.casefold() not in {"pwsh", "powershell"}:
        return None
    index = 1
    while index < len(command) and command[index].casefold() in {"-noprofile", "-nologo"}:
        index += 1
    if index + 1 < len(command) and command[index].casefold() == "-file":
        if Path(command[index + 1]) == _worktrees_setup_script():
            return index + 1
    return None


def _insert_native_options(native: list[str], arguments: list[str]) -> list[str]:
    index = 0
    while index < len(native) and native[index] != "--":
        takes_value = native[index] in NATIVE_VALUE_OPTIONS or native[index] == "-C"
        if takes_value and index + 1 >= len(native):
            raise RuntimeError(f"Invalid worktree native argument envelope: {native[index]} requires a value")
        index += 2 if takes_value else 1
    return [*native[:index], *arguments, *native[index:]]


def append_local_launch_arguments(command: list[str], arguments: list[str]) -> list[str]:
    """Keep native arguments inside the Worktrees PowerShell setup envelope."""
    result = list(command)
    setup = _owned_setup_index(result)
    if setup is not None:
        envelopes = [
            index for index in range(setup + 1, len(result))
            if result[index].casefold() == "-nativeargumentsjson"
        ]
        if envelopes:
            index = envelopes[0] + 1
            if len(envelopes) != 1 or index >= len(result):
                raise RuntimeError("Invalid worktree native argument envelope")
            try:
                envelope = json.loads(result[index])
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Invalid worktree native argument envelope") from exc
            native = envelope.get("args") if isinstance(envelope, dict) else None
            if not isinstance(native, list) or any(not isinstance(value, str) for value in native):
                raise RuntimeError("Invalid worktree native argument envelope")
            envelope["args"] = _insert_native_options(native, arguments)
            result[index] = json.dumps(envelope)
            return result
    return [*result, *arguments]
