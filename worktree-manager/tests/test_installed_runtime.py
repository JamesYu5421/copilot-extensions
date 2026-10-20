"""Construction, rollback and real native launcher contracts (no network)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from worktree_manager import installed_runtime as runtime
from worktree_manager import self_install as si
from worktree_manager import self_install_payload as payload


def _source(root: Path, version: str = "1.2.3") -> Path:
    package = root / "src" / "worktree_manager"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (package / "__main__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    return root


def _fake_build(slot: Path) -> None:
    python = runtime.runtime_python(slot)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("fixture interpreter", encoding="utf-8")


def test_source_policy_does_not_come_from_a_foreign_manager_venv(tmp_path, monkeypatch):
    expected = runtime.payload_root()
    foreign = _source(tmp_path / "old-manager")
    monkeypatch.setattr(runtime.sys, "prefix", str(foreign / ".venv"))
    assert runtime.payload_root() == expected


def test_wheel_resolves_its_own_payload_only(tmp_path, monkeypatch):
    slot = _source(tmp_path / "installed")
    environment = slot / ".venv"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("# fixture", encoding="utf-8")
    module = environment / "Lib" / "site-packages" / "worktree_manager" / "installed_runtime.py"
    monkeypatch.setattr(runtime, "__file__", str(module))
    monkeypatch.setattr(runtime.sys, "prefix", str(environment))
    assert runtime.payload_root() == slot.resolve()
    monkeypatch.setattr(runtime.sys, "prefix", str(tmp_path / "other" / ".venv"))
    assert runtime.payload_root() == slot.resolve(), "direct daemon Python retains its package owner"
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "unowned" / "installed_runtime.py"))
    with pytest.raises(RuntimeError, match="standalone payload"):
        runtime.payload_root()


def test_wheel_keeps_the_manager_owned_launch_asset(tmp_path, monkeypatch):
    from worktree_manager import relocated_launch

    slot = _source(tmp_path / "installed")
    package = slot / ".venv" / "Lib" / "site-packages" / "worktree_manager"
    (slot / ".venv").mkdir()
    (slot / ".venv" / "pyvenv.cfg").write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(runtime, "__file__", str(package / "installed_runtime.py"))
    monkeypatch.setattr(runtime.sys, "prefix", str(slot / ".venv"))
    monkeypatch.setattr(relocated_launch, "__file__", str(package / "relocated_launch.py"))
    expected = slot / "bin" / (
        "launch-session.ps1" if os.name == "nt" else "launch-session.sh"
    )
    expected.parent.mkdir()
    expected.write_text("# fixture", encoding="utf-8")
    assert relocated_launch._relocated_launch_script() == expected
    expected.unlink()
    assert relocated_launch._relocated_launch_script() is None


def test_direct_base_python_cannot_overwrite_its_executing_manager(tmp_path, monkeypatch):
    slot = tmp_path / "slot"
    monkeypatch.setattr(si, "__file__", str(slot / ".venv" / "site-packages" / "self_install.py"))
    monkeypatch.setattr(si.sys, "executable", str(tmp_path / "base-python"))
    assert si._currently_running_from(slot)


def test_build_uses_platform_install_and_canonical_policy(tmp_path, monkeypatch):
    source = _source(tmp_path / "payload")
    scripts = source / "scripts"
    scripts.mkdir()
    for name in ("installer_dependency_policy.py", "installer_dependency_config.py"):
        (scripts / name).write_text("# fixture", encoding="utf-8")
    library = source / "libs" / "first-party"
    library.mkdir(parents=True)
    (library / "pyproject.toml").write_text("[project]\nname='first-party'\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "payload_root", lambda: source)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "selected-uv")
    calls = []
    monkeypatch.setattr(runtime, "_run", lambda argv, **kw: calls.append((argv, kw)))

    runtime.build_runtime(source)

    assert calls[0][0] == [
        "selected-uv", "venv", "--python", sys.executable, str(source / ".venv"),
    ]
    command, options = calls[1]
    assert command[:6] == [
        sys.executable, "-I", "-B", "-X", "utf8", str(scripts / "installer_dependency_policy.py"),
    ]
    assert command[6:11] == [
        "--uv", "selected-uv", "--trusted-source-root", str(source.resolve()), "--",
    ]
    assert command[11:] == [
        "--python", str(runtime.runtime_python(source)), "--reinstall", "--no-sources",
        "--no-editable", "--link-mode", "copy", str(library.resolve()), str(source.resolve()),
    ]
    assert options["cwd"] == source
    assert all("run" not in argv for argv, _ in calls)


def test_policy_failure_is_not_retried_with_weaker_settings(tmp_path, monkeypatch):
    source = _source(tmp_path / "payload")
    scripts = source / "scripts"
    scripts.mkdir()
    for name in ("installer_dependency_policy.py", "installer_dependency_config.py"):
        (scripts / name).write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(runtime, "payload_root", lambda: source)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "selected-uv")
    calls = []

    def run(argv, **_):
        calls.append(argv)
        if len(calls) == 2:
            raise RuntimeError("dependency-seasoning: ineligible artifact")

    monkeypatch.setattr(runtime, "_run", run)
    with pytest.raises(RuntimeError, match="ineligible artifact"):
        runtime.build_runtime(source)
    assert len(calls) == 2


def test_posix_constructor_uses_existing_containment_and_retains_stderr(tmp_path, monkeypatch):
    from worktree_manager import engine_process

    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        options["stderr"].write(b"literal failure detail")
        return subprocess.CompletedProcess(argv, 7, "captured stdout", None)

    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="posix", environ={"CONTROLLED": "yes"}))
    monkeypatch.setattr(engine_process, "run", run)
    result = runtime._owned_runner(["fixture"], cwd=tmp_path, timeout=9)
    assert (result.returncode, result.stdout, result.stderr) == (
        7, "captured stdout", "literal failure detail",
    )
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["env"] == {"CONTROLLED": "yes"}
    assert calls[0][1]["timeout"] == 9


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job teardown contract")
def test_runtime_command_timeout_retires_its_native_descendants(tmp_path):
    pid_file = tmp_path / "descendant"
    code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));time.sleep(30)"
    )
    with pytest.raises(RuntimeError, match="runtime command failed"):
        runtime._run([sys.executable, "-c", code, str(pid_file)], cwd=tmp_path, timeout=2)
    pid = int(pid_file.read_text())
    from mux_windows_observer import processes
    assert pid not in processes()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_final_path_health_failure_restores_previous_slot(tmp_path, monkeypatch, failure):
    source = _source(tmp_path / "source")
    slot = tmp_path / "versions" / "1.2.3"
    monkeypatch.setattr(payload, "build_runtime", _fake_build)
    monkeypatch.setattr(payload, "probe_runtime", lambda *_: None)
    payload._copy_payload(source, slot)
    (slot / "retained.txt").write_text("previous runtime", encoding="utf-8")
    before = {path.relative_to(slot): path.read_bytes() for path in slot.rglob("*") if path.is_file()}

    def probe(candidate, version):
        assert version == "1.2.3"
        assert not (candidate / si._SLOT_COMPLETE_MARKER).exists()
        if candidate == slot:
            raise failure("final runtime import failed")

    monkeypatch.setattr(payload, "probe_runtime", probe)
    with pytest.raises(failure, match="final runtime import failed"):
        payload._copy_payload(source, slot)
    after = {path.relative_to(slot): path.read_bytes() for path in slot.rglob("*") if path.is_file()}
    assert after == before
    assert list(slot.parent.iterdir()) == [slot]


def test_construction_failure_never_publishes_a_complete_slot(tmp_path, monkeypatch):
    source = _source(tmp_path / "source")
    slot = tmp_path / "versions" / "1.2.3"

    def refuse(_):
        raise RuntimeError("construction refused")

    monkeypatch.setattr(payload, "build_runtime", refuse)
    with pytest.raises(RuntimeError, match="construction refused"):
        payload._copy_payload(source, slot)
    assert not slot.exists()
    assert not list(slot.parent.glob("*.staging.*"))


def test_two_native_installers_recheck_under_the_same_lease(tmp_path):
    source = _source(tmp_path / "source")
    root = tmp_path / "manager"
    root.mkdir()
    code = """
import json,sys,time
from pathlib import Path
from worktree_manager import self_install as si, self_install_payload as payload
from worktree_manager.installed_runtime import runtime_python
source,root,label=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
acquire=si._acquire_install_cutover_lease
def claimed(root):
    (root/(label+'-attempt')).write_text('attempt')
    return acquire(root)
def build(slot):
    python=runtime_python(slot); python.parent.mkdir(parents=True,exist_ok=True)
    python.write_text('fixture interpreter')
    with (root/'builds').open('a') as stream: stream.write(label+'\\n')
    (root/'building').write_text(label)
    deadline=time.monotonic()+10
    while not (root/'proceed').exists():
        if time.monotonic()>deadline: raise RuntimeError('fixture release timed out')
        time.sleep(.01)
si._acquire_install_cutover_lease=claimed
si._reap_stranded_cutover_passive=lambda *_: None
si.probe_runtime=lambda *_: None
payload.build_runtime=build
payload.probe_runtime=lambda *_: None
result=si.self_install(source,root=root,dry_run=False)
print(json.dumps({'action':result.action,'reason':result.reason}))
raise SystemExit(1 if result.action=='error' else 0)
"""
    env = dict(
        os.environ, WORKTREE_MANAGER_ROOT=str(root),
        AGENT_WORKTREES_CONTROL_PLANE_PROVIDERS_DIR=str(tmp_path / "providers"),
    )
    children = []

    def start(label):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", code, str(source), str(root), label],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        children.append(process)
        return process

    def wait_for(path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            for child in children:
                if child.poll() is not None:
                    stdout, stderr = child.communicate(timeout=1)
                    pytest.fail("installer exited before its gate:\n" + stdout + stderr)
            time.sleep(.01)
        assert path.exists(), "native installer did not reach its synchronization gate"

    try:
        start("first")
        wait_for(root / "building")
        start("second")
        wait_for(root / "second-attempt")
        (root / "proceed").write_text("release", encoding="utf-8")
        actions = []
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            actions.append(json.loads(stdout)["action"])
        assert sorted(actions) == ["already-current", "installed"]
        assert (root / "builds").read_text().splitlines() == ["first"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_legacy_source_only_completion_is_not_runtime_readiness(tmp_path):
    slot = _source(tmp_path / "legacy")
    (slot / si._SLOT_COMPLETE_MARKER).write_text("", encoding="utf-8")
    assert not si._slot_is_complete(slot)
    si._mark_slot_complete(slot)
    assert not si._slot_is_complete(slot)


@pytest.fixture
def standalone_runtime(tmp_path):
    slot = _source(tmp_path / "installed root" / "versions" / "1.2.3")
    environment = slot / ".venv"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    site = (
        environment / "Lib" / "site-packages" if os.name == "nt"
        else environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    package = site / "worktree_manager"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json,sys\n"
        "from . import __version__\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('worktree-manager ' + __version__)\n"
        "elif sys.argv[1:] == ['--fail']:\n"
        "    print('fixture failure', file=sys.stderr)\n"
        "    raise SystemExit(7)\n"
        "else:\n"
        "    print(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
        encoding="utf-8",
    )
    return slot


def _launcher_command(shell, executable, directory, arguments):
    if shell == "cmd":
        stub = directory / "worktree-manager.cmd"
        return (
            f'{subprocess.list2cmdline([executable])} /d /s /c '
            f'"{subprocess.list2cmdline([str(stub), *arguments])}"'
        )
    if shell == "bash":
        return [executable, str(directory / "worktree-manager"), *arguments]
    return [executable, "-NoProfile", "-File", str(directory / "worktree-manager.ps1"), *arguments]


def _shell_executable(shell):
    if shell == "bash" and os.name == "nt":
        git = shutil.which("git")
        if git:
            bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if bash.is_file():
                return str(bash)
        return None
    return shutil.which(shell)


@pytest.mark.parametrize("shell", ["cmd", "powershell", "pwsh", "bash"] if os.name == "nt" else ["bash"])
def test_native_generated_launcher_without_uv_or_source(standalone_runtime, tmp_path, monkeypatch, shell):
    executable = _shell_executable(shell)
    if not executable:
        pytest.skip(f"native {shell} is unavailable")
    slot = standalone_runtime
    root = slot.parent.parent
    si._write_marker(root, "1.2.3")
    monkeypatch.setattr(si, "local_bin", lambda: tmp_path / "bin with spaces")
    si._deploy_binstubs()
    shutil.rmtree(slot / "src")
    env = dict(os.environ, WORKTREE_MANAGER_ROOT=str(root), PATH="")
    env.pop("BASH_ENV", None)
    arguments = ["two words", "caf\u00e9", "--literal=alpha"]
    command = _launcher_command(shell, executable, si.local_bin(), arguments)
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments
    failed = subprocess.run(
        _launcher_command(shell, executable, si.local_bin(), ["--fail"]),
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert failed.returncode == 7
    assert not failed.stdout.strip()
    assert "fixture failure" in failed.stderr


def _record_internal_calls(slot, tmp_path):
    environment = slot / ".venv"
    main = next(environment.rglob("worktree_manager/__main__.py"))
    original = main.read_text(encoding="utf-8")
    main.write_text(
        "import json,os,pathlib,sys,worktree_manager\n"
        "if sys.argv[1:2] in (['mux-daemon'],['companion']):\n"
        "    pathlib.Path(os.environ['MANAGER_TEST_RECEIPT']).write_text(json.dumps({\n"
        "        'args':sys.argv[1:],'prefix':sys.prefix,'module':worktree_manager.__file__\n"
        "    }),encoding='utf-8')\n"
        "    raise SystemExit(0)\n" + original,
        encoding="utf-8",
    )
    shutil.rmtree(slot / "src")
    binary = slot / "bin"
    binary.mkdir()
    return binary, tmp_path / "receipt.json"


def test_internal_callers_cannot_resolve_or_sync_the_installed_project():
    binary = Path(__file__).resolve().parents[1] / "bin"
    for name in (
        "launch-session.ps1", "launch-session.sh", "pane-wrapper.ps1",
        "pane-wrapper.sh", "session-options.ps1",
    ):
        code = "\n".join(
            line for line in (binary / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not re.search(r"\buv\b", code), name


def _environment_snapshot(slot):
    return {
        str(path.relative_to(slot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (slot / ".venv").rglob("*") if path.is_file()
    }


def _check_internal_calls(command, env, slot, receipt, action, tmp_path):
    before = _environment_snapshot(slot)
    for _ in range(2):
        receipt.unlink(missing_ok=True)
        result = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not result.stderr.strip(), result.stderr
        deadline = time.monotonic() + 5
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert receipt.exists(), result.stdout + result.stderr
        record = json.loads(receipt.read_text(encoding="utf-8"))
        assert Path(record["prefix"]).resolve() == (slot / ".venv").resolve()
        assert Path(record["module"]).resolve().is_relative_to((slot / ".venv").resolve())
        if action == "companion":
            assert record["args"] == ["companion"]
        else:
            assert record["args"][:2] == ["mux-daemon", action]
            assert "--project=fixture" in record["args"]
            assert "--worktree-id=fixture-wt" in record["args"]
            assert "--session-incarnation=fixture-id:17" in record["args"]
            if action == "register":
                assert "--worktree-path=" + env["MANAGER_TEST_WORKDIR"] in record["args"]
        assert _environment_snapshot(slot) == before
    runtime.probe_runtime(slot, "1.2.3")
    assert _environment_snapshot(slot) == before


@pytest.mark.skipif(os.name != "nt", reason="native Windows mux and Companion adapters")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("action", ["register", "remove", "companion"])
def test_internal_windows_calls_preserve_the_installed_environment(
    standalone_runtime, tmp_path, shell, action, _no_visible_windows,
):
    executable = _shell_executable(shell)
    if not executable:
        pytest.skip(f"native {shell} is unavailable")
    slot = standalone_runtime
    binary, receipt = _record_internal_calls(slot, tmp_path)
    source_bin = Path(__file__).resolve().parents[1] / "bin"
    prefix = (
        "$ErrorActionPreference = 'Stop'\n"
        "$script:LaunchProject = 'fixture'\n"
        "$plan = [pscustomobject]@{worktree_id='fixture-wt'}\n"
        "$awProject = 'fixture'; $awWt = 'fixture-wt'; $awMuxSession = 'fixture-session'\n"
        "function Fixture-Mux { 'fixture-id:17' }\n"
        "$script:AwPsmuxBin = 'Fixture-Mux'; $awPsmuxBin = 'Fixture-Mux'\n"
        "function Write-SetupLog { param($Message,$Level); throw $Message }\n"
    )
    if action == "register":
        source = (source_bin / "launch-session.ps1").read_text(encoding="utf-8")
        match = re.search(r"^function Invoke-ManagedMuxRegister \{.*?^\}", source, re.M | re.S)
        assert match
        body = match.group() + "\nInvoke-ManagedMuxRegister 'fixture-session' $env:MANAGER_TEST_WORKDIR\n"
    elif action == "remove":
        source = (source_bin / "pane-wrapper.ps1").read_text(encoding="utf-8")
        body = source[
            source.index("if (\n    -not [string]::IsNullOrWhiteSpace($awProject)"):
            source.index("# Intentional interrupt -- exit silently")
        ]
    else:
        body = (source_bin / "session-options.ps1").read_text(encoding="utf-8") + (
            "\n$fragment = Get-AwMuxCompanionKeybindFragment -ManagerRoot (Split-Path -Parent $PSScriptRoot)\n"
            "$fragment\n"
        )
    script = binary / "fixture.ps1"
    script.write_text(prefix + body, encoding="utf-8-sig")
    env = dict(
        os.environ, PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
        MANAGER_TEST_RECEIPT=str(receipt),
        MANAGER_TEST_WORKDIR=str(tmp_path / "work tree caf\u00e9") + "\\",
    )
    assert shutil.which("uv", path=env["PATH"]) is None
    command = [executable, "-NoProfile", "-File", str(script)]
    if action == "companion":
        result = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        match = re.fullmatch(
            r'bind-key -T root C-k display-popup -E -w 80% -h 80% "(.*)"', result.stdout.strip(),
        )
        assert match, result.stdout
        popup_command = match.group(1).replace('\\"', '"')
        command = f'"{os.environ["COMSPEC"]}" /d /s /c "{popup_command}"'
    _check_internal_calls(
        command, env, slot, receipt, action, tmp_path,
    )


@pytest.mark.parametrize("action", ["register", "remove"])
def test_internal_posix_calls_preserve_the_installed_environment(
    standalone_runtime, tmp_path, action, _no_visible_windows,
):
    executable = _shell_executable("bash")
    if not executable:
        pytest.skip("native bash is unavailable")
    slot = standalone_runtime
    binary, receipt = _record_internal_calls(slot, tmp_path)
    source_bin = Path(__file__).resolve().parents[1] / "bin"
    prefix = (
        "#!/usr/bin/env bash\n"
        "setup_log() { echo \"$*\" >&2; }\n"
        "tmux() { printf 'fixture-id:17\\n'; }\n"
        "LAUNCH_PROJECT=fixture; WORKTREE_ID=fixture-wt\n"
        "AW_PROJECT=fixture; AW_WT=fixture-wt; AW_MUX_SESSION=fixture-session\n"
    )
    if action == "register":
        source = (source_bin / "launch-session.sh").read_text(encoding="utf-8")
        match = re.search(r"^        _aw_publish_managed_mux_live\(\) \{.*?^        \}", source, re.M | re.S)
        assert match
        body = match.group() + '\n_aw_publish_managed_mux_live fixture-session "$MANAGER_TEST_WORKDIR"\n'
    else:
        source = (source_bin / "pane-wrapper.sh").read_text(encoding="utf-8")
        body = source[
            source.index('if [[ -n "$AW_PROJECT" && -n "$AW_WT" ]]'):
            source.index("# Intentional interrupt -- exit silently")
        ]
    script = binary / "fixture.sh"
    script.write_text(prefix + body, encoding="utf-8")
    path = os.pathsep.join((
        str(Path(executable).parent), str(Path(executable).parent.parent / "usr" / "bin"),
    )) if os.name == "nt" else os.defpath
    env = dict(
        os.environ, PATH=path, MANAGER_TEST_RECEIPT=str(receipt),
        MANAGER_TEST_WORKDIR=str(tmp_path / "work tree caf\u00e9"), MSYS2_ARG_CONV_EXCL="*",
    )
    env.pop("BASH_ENV", None)
    assert shutil.which("uv", path=env["PATH"]) is None
    _check_internal_calls(
        [executable, str(script)], env, slot, receipt, action, tmp_path,
    )


@pytest.mark.skipif(
    not os.environ.get("MANAGER_ENGINE_TEST_PYTHON"),
    reason="opt-in engine-to-installed-Manager native qualification",
)
def test_native_engine_probe_preserves_the_installed_environment(
    standalone_runtime, tmp_path, _no_visible_windows,
):
    slot = standalone_runtime
    (slot / "bin").mkdir()
    shutil.rmtree(slot / "src")
    si._write_marker(slot.parent.parent, "1.2.3")
    env = dict(os.environ, WORKTREE_MANAGER_ROOT=str(slot.parent.parent), PATH="")
    assert shutil.which("uv", path=env["PATH"]) is None
    before = _environment_snapshot(slot)
    for _ in range(2):
        result = subprocess.run(
            [
                os.environ["MANAGER_ENGINE_TEST_PYTHON"], "-I", "-B", "-X", "utf8", "-c",
                "from agent_worktrees.manager_launch_cli import _usable_worktree_manager_launcher_dir;"
                "print(_usable_worktree_manager_launcher_dir())",
            ],
            cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == str(slot / "bin")
        assert not result.stderr.strip()
        assert _environment_snapshot(slot) == before


def test_probe_checks_actual_environment_and_import(standalone_runtime):
    runtime.probe_runtime(standalone_runtime, "1.2.3")
    with pytest.raises(RuntimeError, match="does not match"):
        runtime.probe_runtime(standalone_runtime, "other-version")
    python = runtime.runtime_python(standalone_runtime)
    python.unlink()
    with pytest.raises(RuntimeError, match="interpreter is missing"):
        runtime.probe_runtime(standalone_runtime, "1.2.3")


def test_probe_requires_the_actual_cli_version_not_only_a_success_exit(
    standalone_runtime, monkeypatch,
):
    slot = standalone_runtime
    outputs = iter((
        json.dumps({
            "version": "1.2.3", "prefix": str(slot / ".venv"),
            "module": str(slot / ".venv" / "worktree_manager" / "__init__.py"),
        }),
        "unexpected version output",
    ))
    monkeypatch.setattr(
        runtime, "_run", lambda argv, **_: subprocess.CompletedProcess(argv, 0, next(outputs), ""),
    )
    with pytest.raises(RuntimeError, match="CLI health does not report"):
        runtime.probe_runtime(slot, "1.2.3")


@pytest.mark.parametrize("shell", ["cmd", "powershell", "pwsh", "bash"] if os.name == "nt" else ["bash"])
def test_empty_marker_cannot_launch_an_inherited_version(
    standalone_runtime, tmp_path, monkeypatch, shell,
):
    executable = _shell_executable(shell)
    if not executable:
        pytest.skip(f"native {shell} is unavailable")
    root = standalone_runtime.parent.parent
    si._write_marker(root, "")
    monkeypatch.setattr(si, "local_bin", lambda: tmp_path / "bin")
    si._deploy_binstubs()
    env = dict(os.environ, WORKTREE_MANAGER_ROOT=str(root), VER="1.2.3", PATH="")
    env.pop("BASH_ENV", None)
    result = subprocess.run(
        _launcher_command(shell, executable, si.local_bin(), ["--version"]),
        env=env,
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode != 0
    assert not result.stdout.strip()
    assert result.stderr.strip()


def test_healthy_runtime_repairs_binstubs_without_reconstruction(
    standalone_runtime, tmp_path, monkeypatch,
):
    slot = standalone_runtime
    root = slot.parent.parent
    si._write_marker(root, "1.2.3")
    si._mark_slot_complete(slot)
    monkeypatch.setattr(si, "local_bin", lambda: tmp_path / "bin")
    monkeypatch.setattr(si, "control_plane_providers_dir", lambda: tmp_path / "providers")
    monkeypatch.setattr(si, "_copy_payload", lambda *_: pytest.fail("healthy runtime rebuilt"))
    result = si.self_install(slot, root=root, dry_run=False)
    assert result.action == "installed"
    assert (si.local_bin() / si._primary_binstub_name()).is_file()
    runtime.probe_runtime(slot, "1.2.3")


@pytest.fixture
def _no_visible_windows(monkeypatch):
    if os.name != "nt" or os.environ.get("MANAGER_RUNTIME_WINDOWS_E2E") != "1":
        yield
        return
    from mux_windows_observer import descendants, processes, windows

    baseline_windows, baseline_foreground = windows()
    roots, owned, visible, focused = set(), set(), set(), set()
    stop = threading.Event()
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        roots.add(process.pid)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)

    def observe():
        while not stop.is_set():
            table = processes()
            for pid in roots.copy():
                owned.update(descendants(pid, table))
            observed, foreground = windows()
            for window, pid in observed.items():
                name = table.get(pid, ("", 0))[0].lower()
                if pid in owned or (
                    window not in baseline_windows
                    and name in {"windowsterminal.exe", "openconsole.exe", "conhost.exe"}
                ):
                    visible.add(window)
            if foreground in owned or (
                foreground != baseline_foreground
                and table.get(foreground, ("", 0))[0].lower()
                in {"windowsterminal.exe", "openconsole.exe", "conhost.exe"}
            ):
                focused.add(foreground)
            stop.wait(.01)

    with ThreadPoolExecutor(max_workers=1) as executor:
        monitor = executor.submit(observe)
        try:
            yield
        finally:
            stop.set()
            monitor.result(timeout=3)
    assert roots and owned
    assert not visible, "a Manager invocation surfaced a visible window"
    assert not focused, "a Manager invocation stole focus"


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("MANAGER_RUNTIME_WINDOWS_E2E") != "1",
    reason="opt-in native desktop observation of generated launchers",
)
def test_generated_windows_launchers_two_headless_cycles(
    standalone_runtime, tmp_path, monkeypatch, _no_visible_windows,
):
    root = standalone_runtime.parent.parent
    si._write_marker(root, "1.2.3")
    monkeypatch.setattr(si, "local_bin", lambda: tmp_path / "bin")
    si._deploy_binstubs()
    env = dict(os.environ, WORKTREE_MANAGER_ROOT=str(root), PATH="")
    env.pop("BASH_ENV", None)
    for _ in range(2):
        for shell in ("cmd", "powershell", "pwsh", "bash"):
            executable = _shell_executable(shell)
            if shell == "bash" and not executable:
                continue
            assert executable, f"native {shell} is required for this explicit lane"
            result = subprocess.run(
                _launcher_command(shell, executable, si.local_bin(), ["two words"]),
                env=env, cwd=tmp_path, capture_output=True, text=True,
                encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == ["two words"]
