"""Pure contracts for the explicitly opt-in measurement launcher prototype."""
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.coverage_guided_selection import measurement_launch as launch


@pytest.mark.parametrize("arguments,mode,prefix,program,tail", [
    (["python", "-I", "-S", "-B", "-c", "pass", "arg"], "c", ["python","-I","-S","-B"], "pass", ["arg"]),
    (["python", "-ISBcpass", "arg"], "c", ["python","-ISB"], "pass", ["arg"]),
    (["python", "-I", "-m", "package", "--help"], "m", ["python","-I"], "package", ["--help"]),
    (["python", "-I", "script.py", "--version"], "script", ["python","-I"], "script.py", ["--version"]),
])
def test_original_program_boundaries(arguments, mode, prefix, program, tail):
    assert launch.invocation(arguments) == (mode, prefix, program, tail)
    physical = launch.rewritten(arguments, "owned/request")
    assert physical[:len(prefix)] == prefix
    assert physical[len(prefix)] == "-c"
    assert physical[-len(tail):] == tail


def test_c_program_globals_and_argv_are_not_polluted(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["-c", "original"])
    monkeypatch.setattr(sys, "orig_argv", ["host"])
    namespace = {"__name__": "__main__", "__package__": None, "__spec__": None,
                 "__builtins__": builtins, "__doc__": None}
    original = set(namespace)
    code = "import sys; observed=(sys.argv[:],sys.orig_argv[:],set(globals()))"
    arguments = ["native-python", "-I", "-S", "-B", "-c", code, "value"]
    calls = []
    launch.execute({"argv": arguments}, namespace, lambda value: calls.append(value))
    argv, original_argv, keys = namespace["observed"]
    assert argv == ["-c", "value"] and original_argv == arguments
    assert keys == original | {"sys"}
    assert len(calls) == 1


def test_system_exit_is_not_converted(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setattr(sys, "orig_argv", ["host"])
    with pytest.raises(SystemExit) as exc:
        launch.execute({"argv":["python","-I","-S","-c","raise SystemExit(17)"]}, {}, lambda value: None)
    assert exc.value.code == 17


def test_bootstrap_future_flags_do_not_change_program_annotations(monkeypatch):
    monkeypatch.setattr(sys,"argv",["-c"])
    monkeypatch.setattr(sys,"orig_argv",["host"])
    with pytest.raises(NameError):
        launch.execute(
            {"argv":["python","-I","-S","-c",
                     "def f(value: UndefinedApplicationType): pass\nf.__annotations__"]},
            {},lambda value:None,
        )


def test_generated_offline_signature_is_required(tmp_path):
    import secrets
    key = secrets.token_bytes(32)
    path = tmp_path / "request.json"
    launch.signed_write(path, {"identity":"offline-measurement"}, key)
    assert launch.signed_read(path,key)["identity"] == "offline-measurement"
    record = json.loads(path.read_text())
    record["payload"]["identity"] = "tampered"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="signature"):
        launch.signed_read(path,key)


def test_issued_missing_request_cannot_disappear_as_empty_success(tmp_path):
    activation=tmp_path/"activation.py"
    activation.write_text("def activate(payload):pass\n")
    registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::missing")
    registry.requests.append(registry.root/"issued-but-missing")
    with pytest.raises(ValueError,match="issued request ledger"):
        registry.verify()


def test_extra_unissued_directory_is_refused_without_spawning(tmp_path):
    activation=tmp_path/"activation.py"
    activation.write_text("def activate(payload):pass\n")
    registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::extra")
    (registry.root/("f"*64)).mkdir()
    with pytest.raises(ValueError,match="extra issued request"):
        registry.verify()


@pytest.mark.parametrize("flag",["-x","-ISBx"])
def test_skip_first_line_is_explicitly_unsupported(flag):
    with pytest.raises(ValueError,match="skip-first-line"):
        launch.invocation(["python",flag,"program.py"])


def test_uv_requires_original_explicit_no_config(tmp_path):
    artifact=tmp_path/"package.whl"
    artifact.write_bytes(b"fixture")
    python=os.path.normcase(os.path.abspath(sys.executable))
    argv=["uv","pip","install","--python",sys.executable,"--target",str(tmp_path/"sdk"),
          "--offline","--no-index","--no-deps",str(artifact)]
    with pytest.raises(ValueError,match="original explicit --no-config"):
        launch.uv_wheel_contract(argv,{},python)
    explicit=[argv[0],"--no-config",*argv[1:]]
    launch.uv_wheel_contract(explicit,{},python)
    assert "--no-config" not in argv


@pytest.mark.parametrize("extra", [["--requirement","extra.txt"],["--compile-bytecode"],["source.tar.gz"]])
def test_uv_additional_unobservable_build_operations_are_refused(tmp_path,extra):
    artifact=tmp_path/"package.whl"
    artifact.write_bytes(b"fixture")
    python=os.path.normcase(os.path.abspath(sys.executable))
    argv=["uv","pip","install","--python",sys.executable,"--target",str(tmp_path/"sdk"),
          "--offline","--no-index","--no-deps",*extra,str(artifact)]
    with pytest.raises(ValueError):
        launch.uv_wheel_contract(argv,{},python)


@pytest.mark.skipif(
    os.environ.get("CGS_ISOLATED_MEASUREMENT") != "1",
    reason="real process proof requires the verified isolated container lane",
)
class TestMeasurementContainer:
    def _activation(self, tmp_path):
        path = tmp_path / "activation.py"
        path.write_text(
            "def activate(payload):\n"
            " import os,sys,importlib.util,hashlib\n"
            " plan=payload.get('measurement')\n"
            " if plan is None:return\n"
            " for name in ('_cgs_child_audit','_cgs_child_stream'):\n"
            "  assert hashlib.sha256(open(plan[name],'rb').read()).hexdigest()==plan['child_stream_helpers'][plan[name]]\n"
            "  spec=importlib.util.spec_from_file_location(name,plan[name])\n"
            "  module=importlib.util.module_from_spec(spec);sys.modules[name]=module\n"
            "  spec.loader.exec_module(module)\n"
            " os.environ['_CGS_CHILD_MANIFEST']=plan['manifest']\n"
            " os.environ['_CGS_CHILD_AUDITOR']=str(payload['auditor_pid'])\n"
            " sys.modules['_cgs_child_stream'].start(plan['child_stream_control'])\n"
        )
        return path

    def _plan(self, root, payload, copied):
        import hashlib
        from tools.coverage_guided_selection import standalone as s
        from tools.coverage_guided_selection import child_stream
        mapping, hashes = s.verified_source_mapping(s.REPO, copied / "fleet_manager", copied)
        _, options = s.coverage_files(root, mapping)
        token = hashlib.sha256(payload["context"].encode()).hexdigest()
        manifest = Path(options["child_stream_control"]) / "manifests" / (token + ".manifest")
        child_stream.write_manifest(
            manifest, nonce=options["child_stream_nonce"], context=token,
            directory=Path(options["child_stream_control"]) / "hits",
            inventory=options["child_stream_inventory"],
        )
        return dict(options, token=token, manifest=str(manifest),
                    source_binding="byte-equal-declared-source-root", canonical_sha256=hashes)

    def test_copied_sdk_real_lines_under_unchanged_isolation(self, tmp_path):
        import shutil
        import signal
        from tools.coverage_guided_selection.selection import select_tests
        assert Path("/.dockerenv").is_file()
        original = Path(__import__("fleet_manager").__file__).parent
        copied = tmp_path / "copied-sdk"
        shutil.copytree(original,copied / "fleet_manager")
        activation = self._activation(tmp_path)
        registry = launch.Registry(
            tmp_path / "tickets",sys.executable,activation,"offline::copied-sdk",
            measurement_plan=lambda root,payload:self._plan(root,payload,copied),
        )
        program = (
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from pathlib import Path;import fleet_manager;"
            "assert Path(fleet_manager.__file__).parent==Path(sys.argv[1])/'fleet_manager';"
            "from fleet_manager.planning import canonical;"
            "assert canonical({'source':'copied-sdk'})==b'{\"source\":\"copied-sdk\"}';"
            "print('SDK-OK')"
        )
        registry.install()
        try:
            result=subprocess.run([sys.executable,"-I","-S","-B","-c",program,str(copied)],
                                  capture_output=True,text=True,check=True)
            assert result.stdout=="SDK-OK\n" and not result.stderr
            common=program.rsplit("print('SDK-OK')",1)[0]
            crashed=subprocess.run([sys.executable,"-I","-S","-B","-c",
                common+"import os;os._exit(23)",str(copied)],capture_output=True,text=True)
            assert crashed.returncode==23
            forced=subprocess.Popen([sys.executable,"-I","-S","-B","-c",
                common+"import os;print('READY',flush=True);sys.stdin.read()",str(copied)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            assert forced.stdout.readline()=="READY\n"
            forced.send_signal(signal.SIGTERM)
            forced.communicate(timeout=5)
            assert forced.returncode!=0
        finally:
            registry.close()
        records=registry.verify()
        assert len(records)==3
        record=records[0]
        assert all(row["lines"]["audit_complete"] for row in records)
        assert record["lines"]["audit_complete"] and record["lines"]["audited_producers"]==1
        key="fleet-manager/src/fleet_manager/planning.py"
        line=next(int(value) for value in record["lines"]["coverage"][key])
        selected=select_tests({"coverage":record["lines"]["coverage"]},{key:[line]})
        assert selected.selected_tests==("offline::copied-sdk",)
        assert len(record["request"]["measurement"]["canonical_sha256"])==17
        print("COPIED_SDK",json.dumps({
            "source_equivalent_modules":17,"actual_line_records":sum(map(len,record["lines"]["coverage"].values())),
            "prelude_seconds":record["lines"]["prelude_seconds"],
            "spawn_authority":record["audit"]["origin"],"selected":selected.selected_tests,
            "normal_abrupt_forced_measured":True,
        }))

    def test_generated_offline_signature_is_verified_by_copied_sdk(self,tmp_path):
        import base64
        import secrets
        import shutil
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from tools.coverage_guided_selection import standalone as s
        sys.path.insert(0,str(s.REPO/"fleet-manager"/"tests"))
        try:
            from test_release_auth import artifact_fixture
            fixture=artifact_fixture(tmp_path)
        finally:
            sys.path.pop(0)
        from fleet_manager.planning import canonical
        from fleet_manager.release_auth import SIGNING_DOMAIN
        private=Ed25519PrivateKey.generate()
        authority="offline-"+secrets.token_hex(8)
        fixture["policy"]["authority"]=authority
        fixture["proof"]["payload"]["authority"]=authority
        fixture["policy"]["keys"][0]["public_key"]=base64.b64encode(
            private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
        fixture["proof"]["signature"]=base64.b64encode(
            private.sign(SIGNING_DOMAIN+canonical(fixture["proof"]["payload"]))).decode()
        inputs=tmp_path/"signed-inputs.json"
        inputs.write_text(json.dumps(fixture))
        source=Path(__import__("fleet_manager").__file__).parent
        copied=tmp_path/"sdk"
        shutil.copytree(source,copied/"fleet_manager")
        from importlib import metadata
        distribution=metadata.distribution("fleet-manager")
        record=next(item for item in distribution.files if str(item).endswith(".dist-info/RECORD"))
        dist_info=Path(distribution.locate_file(record)).parent
        shutil.copytree(dist_info,copied/dist_info.name)
        parent=tmp_path/"staging"
        parent.mkdir()
        crypto=Path(__import__("cryptography").__file__).parent.parent
        program=r'''
import base64,json,pathlib,socket,sys
target,crypto,inputs,parent,authority,mode=sys.argv[1:]
sys.path[:0]=[target,crypto]
import fleet_manager
assert pathlib.Path(fleet_manager.__file__).parent==pathlib.Path(target)/'fleet_manager'
from fleet_manager.artifact_staging import stage_authenticated_artifacts
from fleet_manager.planning import canonical,PlanError
value=json.loads(pathlib.Path(inputs).read_text())
if mode=='tampered':
    signature=bytearray(base64.b64decode(value['proof']['signature']));signature[0]^=1
    value['proof']['signature']=base64.b64encode(signature).decode()
def no_network(*args,**kwargs):raise AssertionError('network is forbidden')
socket.socket=socket.create_connection=socket.getaddrinfo=no_network
try:
    with stage_authenticated_artifacts(
        canonical(value['manifest']),value['proof'],value['policy'],value['sources'],
        authority=authority,installation='test-installation',role='client',
        staging_parent=pathlib.Path(parent),clock=lambda:150) as stage:
        result=stage.reverify(value['policy'],clock=lambda:151)
        assert result['authenticity_verified'] and result['artifact_integrity_verified']
        assert result['retention_held'] and not result['activation_authorized']
        assert mode=='valid'
except PlanError:
    assert mode=='tampered'
    raise SystemExit(19)
print('AUTH-VERIFIED')
'''
        registry=launch.Registry(
            tmp_path/"tickets",sys.executable,self._activation(tmp_path),"offline::signed-sdk",
            measurement_plan=lambda root,payload:self._plan(root,payload,copied),
        )
        registry.install()
        try:
            for mode,expected in (("valid",0),("tampered",19)):
                result=subprocess.run([sys.executable,"-I","-S","-B","-c",program,
                    str(copied),str(crypto),str(inputs),str(parent),authority,mode],
                    capture_output=True,text=True)
                assert result.returncode==expected,result.stderr
                if mode=="valid":assert result.stdout=="AUTH-VERIFIED\n"
        finally:
            registry.close()
        records=registry.verify()
        assert len(records)==2 and all(row["lines"]["audit_complete"] for row in records)
        assert all("fleet-manager/src/fleet_manager/release_auth.py" in row["lines"]["coverage"]
                   for row in records)
        print("OFFLINE_AUTH",json.dumps({"generated_ed25519":True,"valid_accepted":True,
              "tampered_rejected":True,"measured_producers":len(records),"live_auth":False}))

    def test_uv_native_probe_entry_is_truthful_without_identity_forgery(self,tmp_path):
        import shutil
        assert Path("/.dockerenv").is_file()
        uv=shutil.which("uv")
        assert uv
        from tools.coverage_guided_selection import standalone as s
        output=tmp_path / "wheel"
        output.mkdir()
        original=Path(__import__("fleet_manager").__file__).parent.parent
        registry=launch.Registry(
            tmp_path / "tickets",sys.executable,self._activation(tmp_path),"offline::uv-install",
            measurement_plan=lambda root,payload:self._plan(root,payload,original),uv=uv,
        )
        registry.install()
        try:
            built=subprocess.run([sys.executable,"-I","-c",
                "from setuptools.build_meta import build_wheel;build_wheel("+repr(str(output))+")"],
                cwd=s.REPO / "fleet-manager",capture_output=True,text=True)
            assert built.returncode==0,built.stderr
            wheel=next(output.glob("*.whl"))
            result=subprocess.run([uv,"--no-config","pip","install","--python",sys.executable,
                "--target",str(tmp_path / "sdk"),"--no-deps","--offline","--no-index",str(wheel)],
                capture_output=True,text=True)
            assert result.returncode==0,result.stderr
        finally:
            registry.close()
        records=registry.verify()
        uv_records=[row for row in records if row["request"]["kind"]=="uv-wheel-install"]
        uv_native=[row for row in records if row["request"].get("parent_uv")]
        assert len(uv_records)==1 and uv_native
        assert all(row["audit"]["origin"]=="UV/measurement-entry-before-exec" for row in uv_native)
        assert all(row["lines"]["audit_complete"] for row in records if "lines" in row)
        print("UV_ENTRY_PROOF",json.dumps({
            "returncode":result.returncode,"registered_uv_entries":len(uv_native),
            "uv_sha256":uv_records[0]["request"]["uv_sha256"],
            "origins":[row["audit"]["origin"] for row in records],
            "interpreter_identity_forged":False,
        }))
        missing=registry.root/uv_native[0]["request"]["nonce"]/"birth.json"
        missing.unlink()
        with pytest.raises((OSError,ValueError)):
            registry.verify()
        shutil.rmtree(missing.parent)
        with pytest.raises(ValueError,match="missing or extra issued"):
            registry.verify()

    def test_issued_request_lifecycle_missing_extra_live_and_replay(self,tmp_path):
        import shutil
        assert Path("/.dockerenv").is_file()
        activation=tmp_path/"activation.py"
        activation.write_text("def activate(payload):pass\n")
        registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::lifecycle")
        registry.install()
        workers=[]
        try:
            for _ in range(2):
                workers.append(subprocess.Popen(
                    [sys.executable,"-I","-S","-B","-c",
                     "import sys;print('READY',flush=True);sys.stdin.read()"],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            assert all(worker.poll() is None for worker in workers)
            with pytest.raises(ValueError,match="still running"):
                registry.verify()
            for worker in workers:
                assert worker.stdout.readline()=="READY\n"
                worker.communicate("release",timeout=5)
                assert worker.returncode==0
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
            registry.close()
        assert len(registry.verify())==2
        first,second=registry.requests
        backup=tmp_path/"backup"
        shutil.copytree(first,backup)
        shutil.rmtree(first)
        with pytest.raises(ValueError,match="missing or extra issued"):
            registry.verify()
        shutil.copytree(backup,first)
        removed=registry.requests.pop()
        with pytest.raises(ValueError,match="issued request ledger"):
            registry.verify()
        registry.requests.append(removed)
        extra=registry.root/("e"*64)
        shutil.copytree(first,extra)
        with pytest.raises(ValueError,match="missing or extra issued"):
            registry.verify()
        shutil.rmtree(extra)
        birth=(second/"birth.json").read_bytes()
        (second/"birth.json").write_bytes((first/"birth.json").read_bytes())
        with pytest.raises(ValueError,match="binding mismatch"):
            registry.verify()
        (second/"birth.json").write_bytes(birth)
        process=registry.processes.pop(first)
        with pytest.raises(ValueError,match="Popen lifecycle"):
            registry.verify()
        registry.processes[first]=process
        assert len(registry.verify())==2

    def test_skip_first_line_native_behavior_is_preserved_but_not_admitted(self,tmp_path):
        assert Path("/.dockerenv").is_file()
        script=tmp_path/"skip.py"
        script.write_text("this is not valid Python !!!\nimport sys;print(sys._getframe().f_lineno)\n")
        arguments=[sys.executable,"-I","-S","-B","-x",str(script)]
        expected=subprocess.run(arguments,capture_output=True,text=True,check=True)
        assert expected.stdout=="2\n"
        activation=tmp_path/"activation.py"
        activation.write_text("def activate(payload):pass\n")
        registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::skip-first")
        registry.install()
        try:
            actual=subprocess.run(arguments,capture_output=True,text=True,check=True)
        finally:
            registry.close()
        assert (actual.stdout,actual.stderr,actual.args)==(expected.stdout,expected.stderr,arguments)
        with pytest.raises(ValueError,match="skip-first-line"):
            registry.verify()

    def test_uv_implicit_config_is_not_silently_disabled(self,tmp_path):
        import shutil
        from tools.coverage_guided_selection import standalone as s
        assert Path("/.dockerenv").is_file()
        uv=shutil.which("uv")
        output=tmp_path/"wheel"
        output.mkdir()
        built=subprocess.run([sys.executable,"-I","-c",
            "from setuptools.build_meta import build_wheel;build_wheel("+repr(str(output))+")"],
            cwd=s.REPO/"fleet-manager",capture_output=True,text=True)
        assert built.returncode==0,built.stderr
        artifact=next(output.glob("*.whl"))
        (tmp_path/"uv.toml").write_text("[pip]\ncompile-bytecode = true\n")
        arguments=[uv,"pip","install","--python",sys.executable,"--target",str(tmp_path/"sdk"),
                   "--offline","--no-index","--no-deps",str(artifact)]
        activation=tmp_path/"activation.py"
        activation.write_text("def activate(payload):pass\n")
        registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::uv-config",uv=uv)
        registry.install()
        try:
            result=subprocess.run(arguments,cwd=tmp_path,capture_output=True,text=True)
        finally:
            registry.close()
        assert result.returncode==0,result.stderr
        assert result.args==arguments and "--no-config" not in result.args
        with pytest.raises(ValueError,match="original explicit --no-config"):
            registry.verify()

    def test_native_flags_globals_argv_exit_and_nonblocking_scheduling(self, tmp_path):
        assert Path("/.dockerenv").is_file()
        activation = tmp_path / "activation.py"
        activation.write_text("def activate(payload): pass\n")
        registry = launch.Registry(tmp_path / "tickets", sys.executable, activation, "offline::semantics")
        program = (
            "import sys,json;print(json.dumps({'argv':sys.argv,'orig':sys.orig_argv,"
            "'globals':sorted(globals()),'flags':[sys.flags.isolated,sys.flags.no_site,"
            "sys.flags.dont_write_bytecode],'name':__name__,'package':__package__,"
            "'spec':str(__spec__),'loader':str(__loader__),'file':globals().get('__file__')}))"
        )
        ordinary = {}
        for flags in (("-I","-S","-B"),("-ISB",)):
            argv = [sys.executable,*flags,"-c",program,"value","--help"]
            ordinary[flags] = subprocess.run(argv,capture_output=True,text=True,check=True)
        registry.install()
        try:
            for flags, expected in ordinary.items():
                argv = [sys.executable,*flags,"-c",program,"value","--help"]
                actual = subprocess.run(argv,capture_output=True,text=True,check=True)
                assert actual.stdout == expected.stdout
                assert actual.stderr == expected.stderr
                assert actual.args == argv
            exited = subprocess.run(
                [sys.executable,"-I","-S","-B","-c","print('before-exit');raise SystemExit(17)"],
                capture_output=True,text=True,
            )
            assert exited.returncode == 17 and exited.stdout == "before-exit\n" and not exited.stderr
            workers = [
                subprocess.Popen(
                    [sys.executable,"-I","-S","-B","-c",
                     "import sys;print('READY',flush=True);sys.stdin.read();print('DONE')"],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                ) for _ in range(2)
            ]
            assert all(worker.poll() is None for worker in workers)
            for worker in workers:
                assert worker.stdout.readline() == "READY\n"
            for worker in workers:
                output,error = worker.communicate("release",timeout=5)
                assert worker.returncode == 0 and output == "DONE\n" and not error
        finally:
            registry.close()
        records = registry.verify()
        assert len(records) == 5
        assert all(row["audit"]["origin"] == "CPython/subprocess.Popen" for row in records)

    def test_regular_script_and_module_entry_keep_native_behavior(self,tmp_path):
        assert Path("/.dockerenv").is_file()
        script=tmp_path/"program.py"
        script.write_text(
            "import sys,json\n"
            "print(json.dumps({'argv':sys.argv,'orig':sys.orig_argv,'globals':sorted(globals()),"
            "'file':__file__,'cached':__cached__,'name':__name__,'package':__package__,"
            "'spec':str(__spec__),'loader_type':type(__loader__).__name__,"
            "'flags':[sys.flags.isolated,sys.flags.no_site,sys.flags.dont_write_bytecode]}))\n"
        )
        data=tmp_path/"input.json"
        data.write_text('{"offline":true}')
        arguments=[
            [sys.executable,"-I","-S","-B",str(script),"arg"],
            [sys.executable,"-I","-S","-B","-m","json.tool",str(data)],
        ]
        expected=[subprocess.run(argv,capture_output=True,text=True) for argv in arguments]
        activation=tmp_path/"activation.py"
        activation.write_text("def activate(payload):pass\n")
        registry=launch.Registry(tmp_path/"tickets",sys.executable,activation,"offline::entrypoints")
        registry.install()
        try:
            actual=[subprocess.run(argv,capture_output=True,text=True) for argv in arguments]
        finally:
            registry.close()
        for first,second in zip(expected,actual):
            assert (first.returncode,first.stdout,first.stderr)==(second.returncode,second.stdout,second.stderr)
        assert len(registry.verify())==2

    def test_real_recovery_pair_keeps_original_deadlines_and_concurrency(self,tmp_path):
        import time
        from tools.coverage_guided_selection import standalone as s
        assert Path("/.dockerenv").is_file()
        node="tests/test_two_node_recovery.py"
        ordinary_started=time.monotonic()
        assert pytest.main(["-q",str(s.REPO/"fleet-manager"/node),
            "--basetemp",str(tmp_path/"ordinary"),"-p","no:cacheprovider"])==0
        ordinary=time.monotonic()-ordinary_started
        original=Path(__import__("fleet_manager").__file__).parent.parent
        context=["unset"]
        registry=launch.Registry(
            tmp_path/"tickets",sys.executable,self._activation(tmp_path),lambda:context[0],
            measurement_plan=lambda root,payload:self._plan(root,payload,original),
        )
        class Context:
            @pytest.hookimpl(wrapper=True,tryfirst=True)
            def pytest_runtest_protocol(self,item,nextitem):
                context[0]=item.nodeid
                return (yield)
        registry.install()
        started=time.monotonic()
        try:
            result=pytest.main(["-q",str(s.REPO/"fleet-manager"/node),
                "--basetemp",str(tmp_path/"measured"),"-p","no:cacheprovider"],plugins=[Context()])
        finally:
            registry.close()
        measured=time.monotonic()-started
        assert result==0
        records=registry.verify()
        assert len(records)==12
        assert all(row["lines"]["audit_complete"] for row in records)
        print("REAL_RECOVERY",json.dumps({"ordinary_s":ordinary,"measured_s":measured,
            "ratio":measured/ordinary,"native_producers":len(records),
            "original_5s_15s_assertions":True,"fixture_changes":False}))
