"""Actual adopter installers must refuse publication before staging writes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_command_binding import (
    POWERSHELL, ROOT, _catalog_environment, _snapshot, _write,
    binding_fixture as binding_fixture,
    powershell_host as powershell_host,
)
from test_installer_publication import _policy
from test_native_publication import _run

SHELLS = [
    pytest.param("bash", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX installer")),
    pytest.param("powershell", marks=pytest.mark.skipif(
        os.name != "nt" or POWERSHELL is None, reason="Native Windows installer",
    )),
]


def _command(fixture: dict, shell: str, name: str) -> list[str]:
    suffix = "sh" if shell == "bash" else "ps1"
    script = str(fixture["payload"] / f"scripts/{name}.{suffix}")
    if shell == "bash":
        interpreter = shutil.which("bash")
        assert interpreter is not None
        return [interpreter, script]
    assert POWERSHELL is not None
    return [POWERSHELL, "-NoLogo", "-NoProfile", "-File", script]


def _adopter(fixture: dict, plugin: str) -> dict[str, str]:
    payload = fixture["payload"].with_name(plugin)
    payload.mkdir()
    root = fixture["home"] / f".{plugin}"
    root.mkdir()
    fixture.update(payload=payload, legacy=root)
    fixture["arguments"].update(payload_root=payload, plugin_id=plugin, legacy_root=root)
    source = ROOT / "plugins" / plugin
    shutil.copytree(source / "scripts", payload / "scripts")
    shutil.copyfile(source / "plugin.json", payload / "plugin.json")
    environment = _catalog_environment(fixture)
    environment.update(
        COPILOT_PLUGIN_INSTALL_SMOKE="1",
        COPILOT_PLUGIN_INSTALL_SMOKE_SLEEP="0",
    )
    for name in (
        "COPILOT_EXTENSIONS_CONTEXT", "COPILOT_EXTENSIONS_COMMAND_LIFECYCLE",
        "COPILOT_PLUGIN_INSTALL_STAGED", "COPILOT_PLUGIN_STAGED_FROM",
        "COPILOT_PROJECT_DIR", "COPILOT_EXTENSIONS_INSTALL_PROJECT_ROOT",
    ):
        environment.pop(name, None)
    if os.name != "nt":
        helper = payload / "scripts/installation-context/installation-context.ps1"
        content = helper.read_text(encoding="utf-8")
        original = "$passwdHome = [CePosixAccount]::Home()"
        assert content.count(original) == 1
        helper.write_text(content.replace(original, "$passwdHome = $env:HOME"), encoding="utf-8")
        getent = fixture["home"] / "test-bin/getent"
        getent.write_text(
            "#!/bin/sh\n"
            'printf "fixture:x:%s:%s:fixture:%s:/bin/sh\\n" "$(id -u)" "$(id -g)" "$HOME"\n',
            encoding="utf-8",
        )
        getent.chmod(0o755)
    return environment


def _without_python(fixture: dict, environment: dict[str, str]) -> None:
    if os.name == "nt":
        environment["PATH"] = ""
        return
    commands = fixture["home"] / "native-tools"
    commands.mkdir()
    for name in (
        "awk", "bash", "basename", "cat", "chmod", "cp", "cut", "date", "dirname",
        "find", "flock", "getent", "grep", "head", "id", "ln", "mkdir", "mktemp", "mv", "od", "pwd",
        "readlink", "realpath", "rm", "sed", "sha256sum", "shasum", "sleep", "sort",
        "stat", "tail", "tr", "uname", "wc",
    ):
        executable = shutil.which(name, path=environment["PATH"])
        if executable is not None:
            (commands / name).symlink_to(executable)
    environment["PATH"] = str(commands)


def _assert_refusal(
    binding_fixture: dict, shell: str, state: str, action: str,
    scoped: bool, powershell_host,
) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    root = binding_fixture["legacy"]
    if scoped:
        root = binding_fixture["home"] / "scoped-runtime"
        binding_fixture["arguments"]["legacy_root"] = root
    _policy(binding_fixture, root, binding_fixture["arguments"], state)
    retained = root / ".install-stage/retained-0/retained"
    retained.parent.mkdir(parents=True)
    retained.write_text("preserve", encoding="utf-8")
    options = (
        ["--install-dir" if shell == "bash" else "-InstallDir", str(root)]
        if scoped else []
    )
    entry = "install"
    if scoped and shell == "powershell":
        entry = "scoped-install"
        (binding_fixture["payload"] / "scripts/scoped-install.ps1").write_text(
            "param([string]$Action)\n"
            "& (Join-Path $PSScriptRoot 'install.ps1') -Action $Action -InstallDir '"
            + str(root).replace("'", "''") + "'\nexit $LASTEXITCODE\n",
            encoding="utf-8",
        )
        options = []
    host = powershell_host(binding_fixture, environment) if shell == "powershell" else None
    before = _snapshot(binding_fixture["home"])
    result = _run(
        binding_fixture, environment,
        [*_command(binding_fixture, shell, entry), action, *options], host,
    )
    assert _snapshot(binding_fixture["home"]) == before, result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "publication refused" in result.stdout + result.stderr
    if host:
        host.close()
        assert _snapshot(binding_fixture["home"]) == before


@pytest.mark.parametrize("shell", SHELLS)
def test_logger_managed_first_stamp_refuses_before_staging(
    binding_fixture: dict, shell: str, powershell_host,
) -> None:
    _assert_refusal(binding_fixture, shell, "managed", "stamp", False, powershell_host)


@pytest.mark.portfolio_tier("T3")
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("state", ["managed", "invalid", "foreign", "future"])
@pytest.mark.parametrize("action", ["stamp", "install", "update", "provision"])
@pytest.mark.parametrize("scoped", [False, True])
def test_logger_publication_refuses_before_staging(
    binding_fixture: dict, shell: str, state: str, action: str,
    scoped: bool, powershell_host,
) -> None:
    _assert_refusal(binding_fixture, shell, state, action, scoped, powershell_host)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("state", ["missing", "standalone"])
def test_logger_standalone_retains_staging(binding_fixture: dict, shell: str, state: str) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    _policy(binding_fixture, binding_fixture["legacy"], binding_fixture["arguments"], state)
    result = _run(
        binding_fixture, environment, [*_command(binding_fixture, shell, "install"), "stamp"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    smoke = json.loads((binding_fixture["legacy"] / "smoke.json").read_text(encoding="utf-8-sig"))
    assert smoke["staged"] is True
    assert smoke["staged_from"] == str(binding_fixture["payload"])
    assert ".install-stage" in smoke["ran_from"]


@pytest.mark.parametrize("shell", SHELLS)
def test_logger_explicit_management_reuses_canonical_custody(binding_fixture: dict, shell: str) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    _policy(binding_fixture, binding_fixture["legacy"], binding_fixture["arguments"], "managed")
    interpreter = (
        sys.executable if os.name == "nt" else
        str(binding_fixture["home"] / "test-bin/python3")
    )
    payload = binding_fixture["payload"]
    result = _run(binding_fixture, environment, [
        interpreter, "-I", "-B", str(payload / "scripts/command_binding.py"),
        "--payload-root", str(payload), "--plugin-id", "agent-logger",
        "--legacy-root", str(binding_fixture["legacy"]),
        "--run-lifecycle", "--deferred-runtime", "--managed-deployment", "--",
        *_command(binding_fixture, shell, "install"), "stamp",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    smoke = json.loads((binding_fixture["legacy"] / "smoke.json").read_text(encoding="utf-8-sig"))
    assert smoke["staged"] is True
    assert smoke["staged_from"] == str(payload)
    assert not binding_fixture["legacy"].with_name(
        ".agent-logger.command-binding-publication.lock"
    ).exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_logger_forged_management_does_not_authorize_stamping(
    binding_fixture: dict, shell: str, powershell_host,
) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    _policy(binding_fixture, binding_fixture["legacy"], binding_fixture["arguments"], "managed")
    environment["COPILOT_EXTENSIONS_COMMAND_LIFECYCLE"] = json.dumps({
        "token": "f" * 32, "pid": os.getpid(), "host": "fixture",
        "root": str(binding_fixture["legacy"]),
    })
    host = powershell_host(binding_fixture, environment) if shell == "powershell" else None
    before = _snapshot(binding_fixture["home"])
    result = _run(
        binding_fixture, environment, [*_command(binding_fixture, shell, "install"), "stamp"], host,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ticket" in result.stdout + result.stderr
    assert _snapshot(binding_fixture["home"]) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows native Python-free bootstrap")
@pytest.mark.parametrize("state", ["missing", "managed"])
def test_logger_python_free_policy_admission(binding_fixture: dict, state: str, powershell_host) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    environment["PATH"] = ""
    _policy(binding_fixture, binding_fixture["legacy"], binding_fixture["arguments"], state)
    host = powershell_host(binding_fixture, environment) if state == "managed" else None
    before = _snapshot(binding_fixture["home"])
    result = _run(
        binding_fixture, environment,
        [*_command(binding_fixture, "powershell", "install"), "stamp"], host,
    )
    if state == "managed":
        assert result.returncode != 0, result.stdout + result.stderr
        assert "deployment-managed" in result.stdout + result.stderr
        assert _snapshot(binding_fixture["home"]) == before
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        smoke = json.loads((binding_fixture["legacy"] / "smoke.json").read_text(encoding="utf-8-sig"))
        assert smoke["staged"] is True


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("spelling", [
    "default", "relative", "trailing", "missing-relative", "missing-trailing",
])
def test_python_free_explicit_standalone_preserves_source_and_root(
    binding_fixture: dict, shell: str, spelling: str,
) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    _without_python(binding_fixture, environment)
    root = binding_fixture["legacy"]
    options: list[str] = []
    if spelling != "default":
        root = binding_fixture["home"] / "runtime"
        value = "./runtime" if spelling.endswith("relative") else str(root) + os.sep
        options = ["--install-dir" if shell == "bash" else "-InstallDir", value]
    arguments = {**binding_fixture["arguments"], "legacy_root": root}
    _policy(binding_fixture, root, arguments, "missing" if spelling.startswith("missing-") else "standalone")
    if spelling.startswith("missing-"):
        assert not root.exists()
    result = _run(
        binding_fixture, environment,
        [*_command(binding_fixture, shell, "install"), "stamp", *options],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    smoke = json.loads((binding_fixture["legacy"] / "smoke.json").read_text(encoding="utf-8-sig"))
    assert smoke["staged_from"] == str(binding_fixture["payload"])


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("python_available", [False, True])
def test_relocated_admission_retains_launch_project(
    binding_fixture: dict, shell: str, python_available: bool,
) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    if not python_available:
        _without_python(binding_fixture, environment)
    environment.pop("COPILOT_PROJECT_DIR", None)
    environment.pop("COPILOT_EXTENSIONS_INSTALL_PROJECT_ROOT", None)
    _write(binding_fixture["home"] / ".copilot/settings.json", {})
    project = binding_fixture["home"] / "launch-project"
    _write(project / ".github/copilot/settings.json", {
        "extraKnownMarketplaces": {
            "example": {"source": {"source": "github", "repo": "Project/Plugins"}},
        },
    })
    arguments = {**binding_fixture["arguments"], "project_root": project}
    _policy(binding_fixture, binding_fixture["legacy"], arguments, "standalone")
    result = subprocess.run(
        [*_command(binding_fixture, shell, "install"), "stamp"],
        env=environment, cwd=project, capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    smoke = json.loads((binding_fixture["legacy"] / "smoke.json").read_text(encoding="utf-8-sig"))
    assert smoke["staged"] is True


@pytest.mark.portfolio_tier("T3")
@pytest.mark.parametrize("shell", SHELLS)
def test_waiting_admission_releases_replaceable_payload(
    binding_fixture: dict, shell: str,
) -> None:
    environment = _adopter(binding_fixture, "agent-logger")
    command = [*_command(binding_fixture, shell, "install"), "stamp"]
    _assert_waiting_payload_released(binding_fixture, environment, command)


def _assert_waiting_payload_released(
    binding_fixture: dict, environment: dict[str, str], command: list[str],
    *, cwd: Path | None = None,
) -> None:
    environment["COPILOT_PLUGIN_INSTALL_SMOKE_SLEEP"] = "6"
    payload = binding_fixture["payload"]
    smoke = binding_fixture["legacy"] / "smoke.json"
    process = subprocess.Popen(
        command, env=environment, cwd=cwd or payload, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 15
        while not smoke.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not smoke.exists():
            stdout, stderr = process.communicate(timeout=10)
            pytest.fail("Installer did not reach the held staging seam: " + stdout + stderr)
        assert process.poll() is None
        if os.name != "nt":
            proc = Path("/proc") / str(process.pid)
            assert not (proc / "cwd").resolve().is_relative_to(payload)
            for descriptor in (proc / "fd").iterdir():
                try:
                    target = Path(os.readlink(descriptor))
                except FileNotFoundError:
                    continue
                assert not target.is_relative_to(payload), target
        replacement = payload.with_name("replaceable-payload")
        payload.rename(replacement)
        replacement.rename(payload)
    finally:
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stdout + stderr


@pytest.mark.portfolio_tier("T3")
@pytest.mark.skipif(os.name != "nt", reason="Native Windows Git Bash")
def test_windows_bash_waiting_admission_releases_payload(binding_fixture: dict) -> None:
    git = shutil.which("git")
    bash = Path(git).resolve().parent.parent / "bin/bash.exe" if git else None
    if bash is None or not bash.is_file():
        pytest.skip("Native Windows Git Bash is unavailable")
    environment = _adopter(binding_fixture, "agent-logger")
    environment["HOME"] = binding_fixture["home"].as_posix()
    command = [
        str(bash), (binding_fixture["payload"] / "scripts/install.sh").as_posix(), "stamp",
    ]
    _assert_waiting_payload_released(
        binding_fixture, environment, command, cwd=binding_fixture["home"],
    )


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Git Bash")
def test_windows_bash_retains_payload_cwd_readiness_refusal(binding_fixture: dict) -> None:
    git = shutil.which("git")
    bash = Path(git).resolve().parent.parent / "bin/bash.exe" if git else None
    if bash is None or not bash.is_file():
        pytest.skip("Native Windows Git Bash is unavailable")
    environment = _adopter(binding_fixture, "agent-logger")
    environment["HOME"] = binding_fixture["home"].as_posix()
    before = _snapshot(binding_fixture["home"])
    result = subprocess.run(
        [str(bash), (binding_fixture["payload"] / "scripts/install.sh").as_posix(), "stamp"],
        env=environment, cwd=binding_fixture["payload"], capture_output=True,
        text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode != 0
    assert "Windows shell launch CWD pins the payload" in result.stderr
    assert _snapshot(binding_fixture["home"]) == before
