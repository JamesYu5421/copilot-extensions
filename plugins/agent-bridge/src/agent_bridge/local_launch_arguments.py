"""Consume the native argument representation returned by a local workspace owner."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

# The setup consumer already advertises this boundary, including relocated copies.
SETUP_BOUNDARY_MARKER = b"# agent-worktrees:launch-seed-boundary-v1"
NATIVE_VALUE_OPTIONS = {
    "-i", "--interactive", "-p", "--prompt", "--model", "--reasoning-effort",
    "--context", "--auto-tier", "--agent", "-n", "--name", "--session-id",
    "--log-dir", "--extension-sdk-path", "--log-level", "--stream", "--output-format",
    "--add-dir", "--attachment", "--disable-mcp-server", "--add-github-mcp-toolset",
    "--add-github-mcp-tool", "--plugin-dir", "--additional-mcp-config", "--mcp-github-auth",
    "--max-autopilot-continues", "--mode", "--environment-host", "--environment-compute-id",
    "--dynamic-retrieval", "--enable-mcp-server", "--max-ai-credits", "--usage-output-file",
}


def _owned_setup_index(command: list[str]) -> int | None:
    if not any(value.casefold() == "-nativeargumentsjson" for value in command):
        return None
    for host, value in enumerate(command):
        if PureWindowsPath(value).stem.casefold() not in {"pwsh", "powershell"}:
            continue
        index = host + 1
        while index < len(command) and command[index].casefold() in {"-noprofile", "-nologo"}:
            index += 1
        if index + 1 >= len(command) or command[index].casefold() != "-file":
            continue
        script = command[index + 1]
        if PureWindowsPath(script).name.casefold() != "default-setup.ps1":
            continue
        try:
            with Path(script).open("rb") as stream:
                header = stream.read(4096).removeprefix(b"\xef\xbb\xbf")
        except OSError as exc:
            raise RuntimeError(f"Cannot inspect worktree setup boundary: {script}") from exc
        if SETUP_BOUNDARY_MARKER in header.splitlines():
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
