"""Consume the native argument representation returned by a local workspace owner."""

from __future__ import annotations

import json
from pathlib import PureWindowsPath


def append_local_launch_arguments(command: list[str], arguments: list[str]) -> list[str]:
    """Keep native arguments inside the Worktrees PowerShell setup envelope."""
    result = list(command)
    setup = next((
        index for index, value in enumerate(result)
        if index and result[index - 1].casefold() == "-file"
        and PureWindowsPath(value).name.casefold() == "default-setup.ps1"
    ), None)
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
            envelope["args"] = [*native, *arguments]
            result[index] = json.dumps(envelope)
            return result
    return [*result, *arguments]
