"""Opt-in measurement launch bootstrap; never installed for ordinary launches.

Real interpreters, flags and process creation remain native. Only the measurement
code entry is substituted; the original program argv/globals are restored before
execution. Signed offline tickets bind the actual spawn event to its producer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time

SCHEMA = "CGS-MEASUREMENT/1"
REQUEST = "_CGS_MEASUREMENT_REQUEST"
_registry = None
_hook_installed = False


def canonical(value):
    # JSON object keys are strings on read-back (source inventory IDs start as ints).
    value = json.loads(json.dumps(value, allow_nan=False))
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def signed_write(path, payload, key):
    data = canonical(payload)
    record = {"payload": payload, "signature": hmac.new(key, data, hashlib.sha256).hexdigest()}
    with path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
    path.chmod(0o600)


def signed_read(path, key):
    if path.is_symlink() or path.stat().st_size > 1_048_576:
        raise ValueError("unsafe measurement record")
    with path.open("rb") as handle:
        data = handle.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError("measurement record exceeds bound")
    record = json.loads(data)
    expected = hmac.new(key, canonical(record["payload"]), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, record["signature"]):
        raise ValueError("measurement signature mismatch")
    return record["payload"]


def invocation(arguments):
    if not isinstance(arguments, (list, tuple)) or not all(isinstance(a, str) for a in arguments):
        raise ValueError("measurement requires structured native argv")
    prefix = [arguments[0]]
    index = 1
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            index += 1
            break
        if not value.startswith("-") or value == "-":
            break
        if value in {"-X", "-W", "--check-hash-based-pycs"}:
            prefix.extend(arguments[index:index + 2])
            index += 2
            continue
        if value.startswith(("-X", "-W")):
            prefix.append(value)
            index += 1
            continue
        if value in {"--help", "--help-env", "--help-all", "--help-xoptions", "--version"}:
            return "information", prefix, "", []
        if value in {"-c", "-m"}:
            if index + 1 == len(arguments):
                raise ValueError("missing interpreter program")
            return value[1:], prefix, arguments[index + 1], list(arguments[index + 2:])
        consumed = False
        for position, flag in enumerate(value[1:], 1):
            if flag in "Vh?":
                return "information", prefix, "", []
            if flag == "x":
                raise ValueError("skip-first-line (-x) measurement is unsupported")
            if flag in "cm":
                if position > 1:
                    prefix.append(value[:position])
                program = value[position + 1:]
                if program:
                    return flag, prefix, program, list(arguments[index + 1:])
                if index + 1 == len(arguments):
                    raise ValueError("missing bundled interpreter program")
                return flag, prefix, arguments[index + 1], list(arguments[index + 2:])
            if flag in "XW":
                if position == len(value) - 1:
                    if index + 1 == len(arguments):
                        raise ValueError("missing interpreter option value")
                    prefix.extend(arguments[index:index + 2])
                    index += 2
                    consumed = True
                break
        if consumed:
            continue
        prefix.append(value)
        index += 1
    if index == len(arguments) or arguments[index] == "-":
        raise ValueError("interactive/stdin measurement is not supported")
    return "script", prefix, arguments[index], list(arguments[index + 1:])


def rewritten(arguments, request):
    mode, prefix, _, tail = invocation(arguments)
    if mode == "information":
        return list(arguments)
    # No assignment/import aliases leak into the application's __main__ globals.
    body = (
        "__import__('os').environ.__setitem__('_CGS_PRELUDE_NS',"
        "str(__import__('time').perf_counter_ns()));__import__('runpy').run_path("
        + repr(str(Path(__file__).resolve()))
        + ",run_name='_cgs_measurement_entry')['entry']("
        + repr(str(request)) + ",globals())"
    )
    return [*prefix, "-c", body, *tail]


def physical(payload, root):
    if payload.get("kind") != "uv-wheel-install":
        return rewritten(payload["argv"], root)
    result = list(payload["argv"])
    result[result.index("--python") + 1] = str(root.parent / "python-measurement-entry")
    return result


def uv_wheel_contract(arguments, environment, python):
    start = 2 if arguments[1:2] == ["--no-config"] else 1
    no_config = start == 2
    if arguments[start:start + 2] != ["pip", "install"]:
        raise ValueError("UV prototype supports only explicit offline wheel installation")
    values, switches, artifacts = {}, set(), []
    iterator = iter(arguments[start + 2:])
    for item in iterator:
        if item in {"--python", "--target"}:
            if item in values:
                raise ValueError("duplicate UV measurement option")
            values[item] = next(iterator, None)
            if not values[item]:
                raise ValueError("missing UV measurement option value")
        elif item in {"--offline", "--no-index", "--no-deps"}:
            switches.add(item)
        elif item == "--no-config":
            no_config = True
        elif item.startswith("-"):
            raise ValueError("UV operation is outside the closed wheel-install contract")
        else:
            artifacts.append(item)
    if set(values) != {"--python", "--target"} or switches != {"--offline", "--no-index", "--no-deps"}:
        raise ValueError("UV wheel measurement requires offline/no-index/no-deps/target")
    if not no_config:
        raise ValueError("UV measurement requires original explicit --no-config")
    if environment.get("UV_COMPILE_BYTECODE") or environment.get("UV_CONFIG_FILE"):
        raise ValueError("UV implicit subprocess/config operation is unsupported")
    if os.path.normcase(os.path.abspath(values["--python"])) != python:
        raise ValueError("UV requested a different interpreter")
    if len(artifacts) != 1 or Path(artifacts[0]).suffix != ".whl" or not Path(artifacts[0]).is_file():
        raise ValueError("UV source builds/additional requirements are not admitted")


def uv_entry(path, arguments):
    """A real, signed entry before exec; never infer a Python spawn from ancestry."""
    import secrets
    outer = Path(path)
    registry_root = outer.parent
    key = (registry_root / "key").read_bytes()
    parent = signed_read(outer / "request.json", key)
    if parent["kind"] != "uv-wheel-install":
        raise ValueError("invalid UV measurement contract")
    actual = Path(os.readlink(f"/proc/{os.getppid()}/exe")).resolve()
    if str(actual) != parent["uv_executable"] or hashlib.sha256(actual.read_bytes()).hexdigest() != parent["uv_sha256"]:
        raise ValueError("UV entry executable identity mismatch")
    slots = parent["uv_slots"]
    if len(slots) != 1:
        raise ValueError("unknown UV parent issuance")
    root = registry_root / slots[0]
    issuance = signed_read(root / "issuance.json", key)
    if (
        issuance["schema"] != SCHEMA or issuance["nonce"] != root.name
        or issuance["parent_uv"] != outer.name or issuance["context"] != parent["context"]
    ):
        raise ValueError("UV entry was not issued by its parent")
    try:
        signed_write(root / "claim.json", {
            "schema": SCHEMA, "nonce": root.name, "parent_uv": outer.name,
            "pid": os.getpid(), "parent": os.getppid(), "context": parent["context"],
        }, key)
    except FileExistsError:
        signed_write(outer / ("denied-" + secrets.token_hex(16) + ".json"), {
            "schema": SCHEMA, "parent_uv": outer.name, "pid": os.getpid(),
            "reason": "UV entry exceeds explicit parent issuance",
        }, key)
        raise ValueError("UV entry exceeds explicit parent issuance") from None
    argv = [parent["python"], *arguments]
    payload = dict(parent, kind="python", nonce=root.name, argv=argv,
                   parent_uv=outer.name, auditor_pid=os.getppid())
    signed_write(root / "request.json", payload, key)
    timestamp = time.monotonic_ns()
    signed_write(root / "audit.json", {
        "schema": SCHEMA, "nonce": root.name, "origin": "UV/measurement-entry-before-exec",
        "parent": os.getppid(), "entry_pid": os.getpid(), "created_ns": timestamp,
        "logical_argv_sha256": hashlib.sha256(canonical(argv)).hexdigest(),
        "physical_argv_sha256": hashlib.sha256(canonical(rewritten(argv,root))).hexdigest(),
    }, key)
    signed_write(root / "spawn.json", {"schema": SCHEMA,"nonce":root.name,"pid":os.getpid()}, key)
    _line_intent(payload, timestamp)
    environment = dict(os.environ)
    environment[REQUEST] = str(root)
    os.execve(parent["python"], rewritten(argv, root), environment)


def _line_intent(payload, timestamp):
    plan = payload.get("measurement")
    if not plan:
        return
    import runpy
    import secrets
    reader = runpy.run_path(str(Path(__file__).with_name("child_audit.py")))
    record = (
        f"{reader['VERSION']} {plan['child_stream_nonce']} {plan['token']} {payload['auditor_pid']} "
        f"{reader['argv_key'](payload['argv'])} {timestamp}\n"
    )
    path = Path(plan["child_stream_control"]) / "audit" / f"{payload['auditor_pid']}-{secrets.token_hex(16)}.intent"
    with path.open("x", encoding="ascii") as handle:
        handle.write(record)


def execute(payload, namespace, activate):
    mode, _, program, tail = invocation(payload["argv"])
    sys.orig_argv = list(payload["argv"])
    sys.argv[:] = ["-c" if mode == "c" else "-m" if mode == "m" else program, *tail]
    activate(payload)
    if mode == "c":
        exec(compile(program, "<string>", "exec", dont_inherit=True), namespace, namespace)
    elif mode == "m":
        import runpy
        if not sys.flags.safe_path:
            sys.path[0] = os.getcwd()
        runpy._run_module_as_main(program, alter_argv=True)
    else:
        import importlib.machinery
        file = Path(program).absolute()
        if not file.is_file():
            raise ValueError("directory/zip measurement entry is not supported")
        if not sys.flags.safe_path:
            sys.path[0] = str(file.parent)
        namespace.update(__file__=str(file), __cached__=None,
                         __loader__=importlib.machinery.SourceFileLoader("__main__", str(file)))
        exec(compile(file.read_bytes(), str(file), "exec", dont_inherit=True), namespace, namespace)


def entry(path, namespace):
    root = Path(path)
    key = (root.parent / "key").read_bytes()
    payload = signed_read(root / "request.json", key)
    if payload["schema"] != SCHEMA or payload["nonce"] != root.name:
        raise ValueError("unknown measurement request")
    if payload["bootstrap_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("measurement bootstrap source changed")
    activation = payload["activation"]
    module = Path(activation["path"])
    if hashlib.sha256(module.read_bytes()).hexdigest() != activation["sha256"]:
        raise ValueError("measurement activation source changed")
    import runpy
    callback = runpy.run_path(str(module), run_name="_cgs_measurement_activation")["activate"]
    def activate(value):
        signed_write(root / "birth.json", {
            "schema": SCHEMA, "nonce": value["nonce"], "context": value["context"],
            "pid": os.getpid(), "parent": os.getppid(), "born_ns": time.monotonic_ns(),
        }, key)
        callback(value)
        os.environ.pop(REQUEST, None)
    execute(payload, namespace, activate)


def _audit(event, arguments):
    registry = _registry
    if registry is None or event != "subprocess.Popen":
        return
    executable, args, _, environment = arguments
    environment = os.environ if environment is None else environment
    request = environment.get(REQUEST)
    if not request:
        return
    if isinstance(args, str):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_cgs_launch_argv", Path(__file__).with_name("child_audit.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = module.native_argv(args)
    root = Path(request)
    payload = signed_read(root / "request.json", registry.key)
    expected = physical(payload, root)
    if args != expected:
        registry.failures.append("physical measurement argv differs from signed request")
        return
    signed_write(root / "audit.json", {
        "schema": SCHEMA, "nonce": payload["nonce"],
        "origin": "CPython/subprocess.Popen", "parent": os.getpid(),
        "physical_argv_sha256": hashlib.sha256(canonical(args)).hexdigest(),
        "logical_argv_sha256": hashlib.sha256(canonical(payload["argv"])).hexdigest(),
        "created_ns": time.monotonic_ns(),
    }, registry.key)
    if payload["kind"] == "python":
        _line_intent(payload, time.monotonic_ns())


class Registry:
    def __init__(self, root, python, activation, context, measurement_plan=None, uv=None):
        import secrets
        self.root = Path(root)
        self.root.mkdir(mode=0o700)
        self.key = secrets.token_bytes(32)
        (self.root / "key").write_bytes(self.key)
        (self.root / "key").chmod(0o600)
        self.python = os.path.normcase(os.path.abspath(python))
        self.activation = {
            "path": str(Path(activation).resolve()),
            "sha256": hashlib.sha256(Path(activation).read_bytes()).hexdigest(),
        }
        self.context, self.requests, self.failures = context, [], []
        self._issued, self._issued_processes, self._spawn_pids = {}, {}, {}
        self.measurement_plan = measurement_plan
        self.uv = str(Path(uv).resolve()) if uv else None
        self.processes = {}
        if self.uv:
            proxy = self.root / "python-measurement-entry"
            proxy.write_text(
                "#!"+self.python+"\nimport sys,runpy,os\n"
                f"runpy.run_path({str(Path(__file__).resolve())!r},"
                f"run_name='_cgs_uv_entry')['uv_entry'](os.environ[{REQUEST!r}],sys.argv[1:])\n"
            )
            proxy.chmod(0o700)
            self.uv_proxy_sha256 = hashlib.sha256(proxy.read_bytes()).hexdigest()

    def uv_contract(self, arguments, environment):
        if os.name != "posix":
            raise ValueError("UV measurement entry currently requires the Linux container lane")
        uv_wheel_contract(arguments, environment, self.python)

    def install(self):
        import inspect
        import secrets
        import subprocess
        global _registry, _hook_installed
        if _registry is not None:
            raise ValueError("nested measurement registry is not supported")
        self.original = subprocess.Popen
        signature = inspect.signature(self.original)
        registry = self
        class MeasurementPopen(registry.original):
            def __init__(self, args, *positional, **kwargs):
                bound = signature.bind(args, *positional, **kwargs)
                executable = bound.arguments.get("executable")
                candidate = executable or (args[0] if isinstance(args, (list, tuple)) and args else "")
                is_uv = registry.uv and str(Path(candidate).resolve()) == registry.uv
                if bound.arguments.get("shell") or (
                    os.path.normcase(os.path.abspath(candidate)) != registry.python and not is_uv
                ):
                    if os.path.basename(str(candidate)).lower() in {"uv", "uv.exe"}:
                        registry.failures.append("UV native launches are not yet registered by the prototype")
                    super().__init__(args, *positional, **kwargs)
                    return
                if not is_uv:
                    try:
                        mode = invocation(args)[0]
                    except ValueError as exc:
                        registry.failures.append(str(exc))
                        super().__init__(args, *positional, **kwargs)
                        return
                    if mode == "information":
                        super().__init__(args, *positional, **kwargs)
                        return
                else:
                    environment = os.environ if bound.arguments.get("env") is None else bound.arguments["env"]
                    try:
                        registry.uv_contract(list(args), environment)
                    except ValueError as exc:
                        registry.failures.append(str(exc))
                        super().__init__(args, *positional, **kwargs)
                        return
                nonce = secrets.token_hex(32)
                root = registry.root / nonce
                root.mkdir(mode=0o700)
                payload = {
                    "schema": SCHEMA, "nonce": nonce, "argv": list(args),
                    "kind": "uv-wheel-install" if is_uv else "python",
                    "activation": registry.activation,
                    "context": registry.context() if callable(registry.context) else registry.context,
                    "bootstrap_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "auditor_pid": os.getpid(),
                }
                if is_uv:
                    payload.update(python=registry.python,uv_executable=registry.uv,
                                   uv_sha256=hashlib.sha256(Path(registry.uv).read_bytes()).hexdigest())
                    payload["uv_proxy_sha256"] = registry.uv_proxy_sha256
                    child = registry.root / secrets.token_hex(32)
                    child.mkdir(mode=0o700)
                    signed_write(child / "issuance.json", {
                        "schema": SCHEMA, "nonce": child.name, "parent_uv": root.name,
                        "context": payload["context"],
                    }, registry.key)
                    registry.requests.append(child)
                    registry._issued[child] = {
                        "kind": "uv-slot", "parent_uv": root,
                        "issuance_sha256": hashlib.sha256((child / "issuance.json").read_bytes()).hexdigest(),
                    }
                    payload["uv_slots"] = [child.name]
                if registry.measurement_plan:
                    payload["measurement"] = registry.measurement_plan(root, payload)
                signed_write(root / "request.json", payload, registry.key)
                registry.requests.append(root)
                registry._issued[root] = {
                    "kind": payload["kind"],
                    "request_sha256": hashlib.sha256((root / "request.json").read_bytes()).hexdigest(),
                }
                environment = dict(os.environ if bound.arguments.get("env") is None else bound.arguments["env"])
                environment[REQUEST] = str(root)
                environment.pop("_CGS_CHILD_MANIFEST", None)
                environment.pop("COVERAGE_PROCESS_START", None)
                environment.pop("COVERAGE_PROCESS_CONFIG", None)
                bound.arguments["args"] = physical(payload, root)
                bound.arguments["env"] = environment
                super().__init__(*bound.args, **bound.kwargs)
                self.args = args
                signed_write(root / "spawn.json", {
                    "schema": SCHEMA, "nonce": nonce, "pid": self.pid,
                }, registry.key)
                registry.processes[root] = self
                registry._issued_processes[root] = self
                registry._spawn_pids[root] = self.pid
        _registry = self
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True
        subprocess.Popen = MeasurementPopen

    def close(self):
        import subprocess
        global _registry
        subprocess.Popen = self.original
        _registry = None

    def verified_python_producers(self):
        """Require all issued requests and their real, completed process lifecycles."""
        if self.failures:
            raise ValueError("; ".join(self.failures))
        expected = set(self._issued)
        if len(self.requests) != len(expected) or set(self.requests) != expected:
            raise ValueError("issued request ledger mismatch")
        actual = {path for path in self.root.iterdir() if path.is_dir() or path.is_symlink()}
        if actual != expected or any(path.is_symlink() for path in actual):
            raise ValueError("missing or extra issued request directory")
        direct = {path for path, record in self._issued.items() if record["kind"] != "uv-slot"}
        if set(self.processes) != direct or set(self._issued_processes) != direct:
            raise ValueError("issued request has no matching Popen lifecycle")
        for root in direct:
            process = self.processes[root]
            if process is not self._issued_processes[root] or process.pid != self._spawn_pids[root]:
                raise ValueError("issued request Popen identity changed")
            if process.poll() is None:
                raise ValueError("issued request Popen is still running")
        result = []
        for root in sorted(expected):
            issued = self._issued[root]
            payload = signed_read(root / "request.json", self.key)
            audit = signed_read(root / "audit.json", self.key)
            spawn = signed_read(root / "spawn.json", self.key)
            if payload.get("schema") != SCHEMA or payload.get("nonce") != root.name:
                raise ValueError("issued request schema/nonce mismatch")
            if issued["kind"] == "uv-slot":
                issuance = signed_read(root / "issuance.json", self.key)
                claim = signed_read(root / "claim.json", self.key)
                if (
                    hashlib.sha256((root / "issuance.json").read_bytes()).hexdigest() != issued["issuance_sha256"]
                    or issuance["parent_uv"] != issued["parent_uv"].name
                    or payload.get("parent_uv") != issuance["parent_uv"]
                    or payload["context"] != issuance["context"] or claim["context"] != issuance["context"]
                    or claim["nonce"] != root.name or claim["parent_uv"] != issuance["parent_uv"]
                    or claim["pid"] != spawn["pid"] or claim["parent"] != audit["parent"]
                ):
                    raise ValueError("UV child has no matching parent issuance/claim")
            elif (
                hashlib.sha256((root / "request.json").read_bytes()).hexdigest() != issued["request_sha256"]
                or spawn["pid"] != self._spawn_pids[root]
            ):
                raise ValueError("issued request or Popen spawn record changed")
            if payload["kind"] == "uv-wheel-install":
                process = self.processes[root]
                if process.poll() != 0:
                    raise ValueError("UV operation is unfinished or unsuccessful")
                if (
                    audit["nonce"] != payload["nonce"] or spawn["nonce"] != payload["nonce"]
                    or audit["logical_argv_sha256"] != hashlib.sha256(canonical(payload["argv"])).hexdigest()
                    or audit["physical_argv_sha256"] != hashlib.sha256(canonical(physical(payload,root))).hexdigest()
                    or hashlib.sha256(Path(payload["uv_executable"]).read_bytes()).hexdigest() != payload["uv_sha256"]
                ):
                    raise ValueError("UV measurement authority/source binding mismatch")
                if hashlib.sha256((root.parent/"python-measurement-entry").read_bytes()).hexdigest() != payload["uv_proxy_sha256"]:
                    raise ValueError("UV measurement entry changed")
                children = [p for p, item in self._issued.items()
                            if item["kind"] == "uv-slot" and item["parent_uv"] == root]
                if set(payload["uv_slots"]) != {p.name for p in children} or not children:
                    raise ValueError("UV operation has no explicitly issued native entry")
                if list(root.glob("denied-*.json")):
                    raise ValueError("UV operation exceeded parent native issuance")
                result.append({"request":payload,"audit":audit,"spawn":spawn,"uv_entries":len(children)})
                continue
            birth = signed_read(root / "birth.json", self.key)
            if (
                audit["nonce"] != payload["nonce"] or birth["nonce"] != payload["nonce"]
                or birth["context"] != payload["context"]
                or audit["created_ns"] > birth["born_ns"]
                or not (birth["pid"] == spawn["pid"] or birth["parent"] == spawn["pid"])
                or audit["logical_argv_sha256"] != hashlib.sha256(canonical(payload["argv"])).hexdigest()
                or audit["physical_argv_sha256"] != hashlib.sha256(canonical(rewritten(payload["argv"],root))).hexdigest()
            ):
                raise ValueError("measurement spawn/producer binding mismatch")
            import runpy
            if runpy.run_path(str(Path(__file__).with_name("child_audit.py")))["live"](birth["pid"]):
                raise ValueError("measurement producer is still running")
            if payload.get("parent_uv"):
                if issued["kind"] != "uv-slot":
                    raise ValueError("unissued UV dynamic producer")
                outer = self.root / payload["parent_uv"]
                parent_spawn = signed_read(outer / "spawn.json",self.key)
                if audit["origin"] != "UV/measurement-entry-before-exec" or audit["parent"] != parent_spawn["pid"]:
                    raise ValueError("UV native entry has no matching authenticated launch")
            row = {"request": payload, "audit": audit, "spawn": spawn, "birth": birth}
            plan = payload.get("measurement")
            if plan:
                from tools.coverage_guided_selection.child_stream import read_streams
                if any(
                    hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected
                    for path, expected in plan["child_stream_helpers"].items()
                ):
                    raise ValueError("measurement child backend source changed")
                row["lines"] = read_streams(
                    Path(plan["child_stream_control"]) / "hits",
                    nonce=plan["child_stream_nonce"], contexts={plan["token"]: payload["context"]},
                    inventory={int(k):v for k,v in plan["child_stream_inventory"].items()},
                    audit_control=plan["child_stream_control"],
                )
            result.append(row)
        return result

    def verify(self):
        return self.verified_python_producers()
