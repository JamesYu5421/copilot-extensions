# Native standalone coverage baselines

Fleet Manager's baseline/full promotion lane earns coverage with the same
installed-package preparation, environment lease and native host containment
as `tools/run-standalone-tests.py`. It does not change that runner or activate
per-PR selection. The existing coverage engine's context driver and schema-v3
test-to-line attribution/duration data are reused.

From a checkout at the exact revision being measured:

```powershell
python tools/coverage_guided_selection/standalone.py fleet-manager --prepare --admission-wait 60 --timeout 720 --measured-commit <full-HEAD-SHA> --out fleet-manager-windows.json
```

Linux uses the same command with `--out fleet-manager-linux.json`. These are
deliberate **full** validation runs: environment and project pytest selection
overrides are removed; incomplete, deselected, failed, empty or inconsistent
universes cannot produce a baseline. Instrumentation dependencies are provisioned
through the existing governed `run_uv_install` admission and `uv` configuration,
including the dependency-age policy; no alternate feed or raw install bypass is
selected. Admission failures propagate without a retry or fallback.
The dedicated full lane allows 720 seconds for instrumented execution inside its
15-minute job budget; ordinary required CI budgets are unchanged.

## Source equivalence and provenance

Before and after collection, installed Python modules must match the canonical
checkout byte-for-byte and have the exact expected inventory. The wheel's
generated runtime modules map to the canonical library resources declared by
`standalone_build.py`, not nonexistent source mirrors. The collector reads that
literal resource mapping without importing build-time setuptools, and still
rejects missing, extra, changed or ambiguously named modules. Only verified
installed paths receive repository line coordinates.
Source/build/test inputs must be clean at the named `HEAD`.

The `collection` record includes native platform, Python version, source/test-input
hashes, collector hashes, complete test-universe digest, test markers and pytest summary. It
contains no installation paths or machine identity. Test node IDs remain relative
to the component root; covered file keys are repository-relative POSIX paths.

The parent uses coverage.py's dynamic contexts. An exclusively leased venv `.pth`
hook starts `child_stream.py` in opted-in native children, including `python -I`,
without importing coverage.py there. Each test supplies a private, digest-named
manifest. Sparse unbuffered source/line records survive `os._exit()` and forced
termination; timing framing is emitted before line events. Future Python threads
use the same source-scoped tracer. Replacing the process/thread tracer invalidates
collection. Child backend/audit versions, helper hashes, producer counts, zero-hit
counts and initialization durations are recorded in provenance.
The duration endpoint follows fork-cleanup registration, both tracer
installations and the thread-tracer guard. The existing sparse-writer lock
keeps the duration record before any source-line record while charging all
required initialization to the measured prelude.

`child_audit.py` observes CPython `subprocess.Popen`/`os.posix_spawn` audit events,
without wrapping Popen or waiting for initialization. Each intent is matched to
exactly one dead producer by run, known test context, argv digest, audited owner
and creation/birth order. Children validate their auditor ancestry, including the
Windows venv redirector. Audit registration is self-tested. Empty-source-hit
producers remain distinguishable from missing producers.
The auditor retains the active parent test context independently of mutable
environment variables. Removing the parent's context marker cannot exempt later
child launches, fork/exec or opaque system commands. Stdin `-` ends interpreter
option parsing, so a following program `--help` never exempts site-disabled code.
An owned package loaded from an unverified checkout or shadow `PYTHONPATH` is
refused rather than recorded as a successful zero-hit producer. Its module
identity does not grant source coordinates outside the verified inventory.

The parent validates every manifest against its source inventory and context map,
rechecks installed bytes, rejects a filename-bound live writer **before** reading
its authoritative stream snapshot, and validates bounded framing,
version, IDs and line ranges before unioning child hits with parent attribution
in schema 3. Later producer identity/completeness checks remain mandatory; a
read/append/exit interleaving cannot turn a partial prefix into complete evidence.
Missing hooks/evidence, bootstrap errors swallowed by `.pth`,
trace/write errors, duplicate records, unknown contexts or unmatched producers
invalidate the entire collection. No success-shaped partial baseline is emitted.
Retained `CGS-ERROR/1` diagnostics bind the immutable manifest/context digest to the
parent's exact pytest node. Ownership failures include the expected auditor and a
bounded kernel PID lineage; image labels are allowlisted basenames, never full
paths or command bodies. Extra launcher ancestry is diagnostic evidence, not a
relaxation of producer admission.

**Supported boundary:** canonical leased native Python with inherited instrumentation
and site initialization. Only informational help/version exits before a code/module/
script boundary are ignored. Site-disabled code execution (`-S`, including bundled
`-IS`) with inherited instrumentation invalidates collection rather than dropping
attribution. Explicitly stripped instrumentation environments are not opted in.
An opted-in unmanaged interpreter,
unaccounted producer, fork/forkpty or exec is refused. Fork cleanup resets inherited
descriptor, seen-set and mutex state; it does not make fork evidence admissible.
Spawn audit occurs before process creation, so an expected native launch refusal
also conservatively invalidates baseline collection. Opaque external launchers are
not an authority for unobservable native descendants; a descendant that loads this
backend without a matching audited owner is refused.

The collector does not serialize process startup, replace native processes, adjust
application clocks/deadlines or change ordinary/plugin/shadow consumers. Hook and
instrumentation files are removed before releasing the lease, including on failure.
Parent dynamic-context capability is checked in prepared environments; the coverage.py
`_exit` patch is not needed for sparse child persistence.
The positive parity regression computes its independent coverage.py reference in
a separate uninstrumented contained run before installing the leased hook.
Instrumented child tests retain their inherited context; stripping it still
invalidates collection rather than exempting the reference process.

Windows uses digest-named `tmp_path` directories beneath a short checkout-local
sandbox to preserve native artifact path headroom. Extended/UNC prefixes are not
used: those prefixes change SQLite's file-URI semantics. Other collectors and
platforms retain their ordinary pytest temporary-path behavior.

## Publication and the bootstrap gate

### Explicit immutable observer artifact experiment

`observer_artifact.py` separates the Linux x86-64 observer build from the
owned workload. `build_bundle` uses the existing hash-pinned compiler wheel
and canonical native source to produce an exclusively created bundle.
The manifest binds the library bytes, entry source, compiler, native platform
and actual Python image. It remains explicitly **unqualified**: a successful
build or ELF constructor check is not native execution, baseline admission,
release authentication or permission to activate the artifact.

`stage_bundle` requires a manifest digest supplied independently by the
owning caller, not a digest discovered in the bundle. It freshly checks that
pin, current source and interpreter identities, bounded ordinary input files
and ELF constructor identity, then stages an exclusive read-only copy. Input
replacement, content changes, links and existing destinations are refused.
Staging never extracts, compiles, installs, downloads or loads native code.
All staging and source verification still belong inside the original caller's
setup clocks. The builder is not a parallel release publisher or trust root;
any publication must use the existing governed artifact mechanism.
The prototype has no native Windows implementation or default activation.

### Opt-in measurement-launch prototype

`measurement_launch.py` is an explicit experiment, not a default collector or an
SDK tracing API. It substitutes only the measurement entry of a native Python
launch, keeps isolation flags, and restores original program argv/globals before
executing the original code. It performs no initialization handshake or wait.
Ordinary launches and SDK bytes are unchanged. Signed, generated offline tickets
bind the actual CPython spawn audit event, physical and logical argv hashes,
actual process creation, producer birth, and byte-verified source plans.
Verification starts from the complete parent-issued request set, not directory
discovery. Every direct request must retain its actual completed Popen object/PID;
missing/extra directories, changed lifecycle records and replayed producers fail.
UV native entries must claim an explicit parent-issued slot (one in the current
wheel-only prototype), and deleting that slot cannot erase the expected producer.
`-x` skip-first-line scripts are explicitly unsupported: their original native
launch still runs unchanged, but measurement admission fails rather than compiling
the skipped line or inventing equivalent line numbers.

The Linux prototype also supports explicit offline/no-index/no-dependency wheel
installation through UV. Configuration-enabled original calls are authorized,
but the current proxy backend cannot completely observe them and refuses them
as an unsupported capability. Its previously proved subset still requires the
original caller's `--no-config`; this is not a policy requiring consumers to add
that flag. The collector never adds it or suppresses configuration silently.
A stable per-registry measurement entry observes each
native UV Python entry **before exec**, verifies the UV binary hash, and joins it
to the signed UV launch. Ancestry alone is never producer authority, and interpreter
metadata is not forged. Source builds, bytecode-compilation subprocesses and UV
operations with no registered native entry are conservatively refused; cache-only
operation support is not claimed. This is not a general opaque-launcher contract.
Warm wheel installation is not evidence of a zero-child cache path: the tested
warm invocation still produces a genuine signed native entry. A pre-issued UV
entry with no actual request/birth is an explicit collection refusal, not an
empty successful ledger.

Admission is closed to the registry's declared native interpreter and optional
pinned UV executable. A fresh byte-identical interpreter copy can be declared
explicitly; the isolated Linux proof verifies its actual kernel image and
`sys.executable`, copied SDK source bytes and generated signature validation.
It does not automatically admit other copies, platforms or interpreter builds.
Undeclared copies (including renamed executables), shells, captured/unissued
Popen calls, direct native spawns and opaque system commands invalidate
measurement while preserving their application's original execution.
Instrumented descendants also refuse unmanaged native executables instead of
silently ignoring names that do not resemble Python. Stripping the inherited
context from a child environment or its spawning instrumented process does not
erase that obligation; opaque execution/fork/exec/system operations still refuse.

Real offline UV `run`, source-directory and sdist installations, and native
cache-only operations are deliberately **not admitted**. Their original explicit
`--no-config` and other arguments are not changed, and their normal success is
not a collection success. These conservative refusals do not close the general
UV support gate or authorize immutable/full baselines. Changed collector/audit
source fingerprints also mean earlier performance acceptance is not transferable
to a newly modified candidate without separately authorized revalidation.

Prototype tests generate HMAC credentials and Ed25519 keys/signatures, verify real
SDK authenticity/integrity, and reject tampered signatures and missing producer
records. Real multiprocess proofs run only in an independently verified, pinned,
non-root, read-only-rootfs, capability-dropped, no-new-privileges container with no
network, host bind or forwarded credentials. Dependency wheels are restored through
the configured governed host feed and installed offline in that container.
Linux proof is not Windows proof; a Windows container lane is still required
before claiming native Windows coverage of the prototype.

Performance comparisons keep the pinned image and resource ceilings fixed, retain
every paired result, alternate whole-run order, and report every producer's actual
initialization duration. The comparison asserts both the 1.5x execution-cost gate
and the 0.2-second per-child target; a passing ratio does not turn a tail-duration
miss into a pass. Profiling diagnostics are explicitly excluded from paired timing.
`CGS_PRELUDE_PHASE_PROFILE=1` enables the isolated twelve-producer wall/CPU phase
diagnostic; adding `CGS_PRELUDE_IMPORT_PROFILE=1` records bounded import/source-load
intervals. Both retain the stream's original timer endpoints and include diagnostic
overhead, scheduler time and IO; neither produces benchmark or baseline authority.
The optional import profile is removed before executing the application.
Collector entry and owned activation helpers compile their exact hash-verified
bytes with `dont_inherit=True` in isolated helper namespaces, avoiding
`runpy.run_path`'s unrelated import graph. Application `-m` execution retains its
native `runpy` semantics; no required collector work moves past the prelude timer.
Only the measured child entry uses CPython's `_json` C scanner/encoder directly
for the existing JSON records. Capability probes execute within the original
prelude and refuse unsupported implementations, signatures or semantics; there
is no Python JSON parsing/encoding fallback. Canonical bytes, HMACs, key
normalization, duplicate-key behavior, native depth/integer limits, Unicode and
strict nonfinite signing refusals remain unchanged. The C decoder may import
stdlib JSON's error class on invalid input; that is a refusal, not a successful
fallback. Host aggregation, ordinary launches and the SDK retain standard JSON.
`child_stream.py` contains only the child tracer and its required threading/setup
logic. `child_stream_parent.py` owns inventory/manifests, evidence validation,
context registration and merging; child bootstrap never loads that module.
Both exact staged sources remain in helper integrity checks and collector
fingerprints. The split does not move required child work outside its timer.
Diagnostic retention is independent of admission. Recovery comparisons emit
sanitized `MEASUREMENT_DIAGNOSTICS` JSON before owned-container cleanup, including
both runs' setup/call/teardown reports, parent validation/staging/signing/spawn
spans, authenticated issuance/birth fields and first **existing** wait/poll exit
observations. Unknown kernel exit times remain null; no extra polling, waits or
process-authority claims are introduced. Keys, argv, environments and absolute
private paths are not exported. Failure paths retain reports too. These records
do not grant native zero/multiple-child completeness or baseline authority.
Failure fields are budgeted for their serialized size, including the second
escaping layer around `phases_json`. Non-BMP text, control characters, quotes and
backslashes cannot expand a bounded diagnostic past the existing completion
reply limit; truncation is explicit and the original failure remains primary.

The bounded native-entry proof represents runtime-generated sources separately:
actual native compile-event bytes are matched to the executing code object and
the verified emitting code object's source. Actual nested code objects receive
distinct source identities; sharing `<string>` is never an admission rule.
Generated lines retain their real audited source bytes under a SHA identity,
not guessed lines in a repository file. The sparse tracer supports code-object
identities, and the parent reader keeps the original static manifest intact
while checking caller-proven generated-source bytes, bounds and collisions.

An owned-test observer may use the explicitly authorized setup/completion
control-plane transaction inside the existing caller clocks. The recovery
test's opt-in lifecycle hook begins after the original start and completes,
parses and closes observer state before the original end. This is not a
per-child startup ACK: native line entry publishes one-way, without waiting.
The zero-capability worker receives no privileged event descriptor or added
capability. Compiler extraction/build, runtime import/staging, event setup,
draining, source validation and cleanup remain charged; a source/clock
correctness proof is not a performance or baseline pass. Default collectors
do not activate this experimental native observer.

The owned-test native build candidate uses `observer_native_build.py` to extract
the same hash-pinned compiler wheel's executable and **complete** library tree
into a fresh invocation-local directory. It does not install a compiler package,
prune target libraries or reuse a cross-run cache. Extraction, input verification,
compilation, report serialization and removal of toolchain/cache state stay
inside the original caller clock. Bounded monotonic setup subphases are
diagnostics, never exemptions from that clock.

Its non-default `runtime-link-only` variant changes only the measurement helper's
link arguments: no CRT/default libraries, actual protected loaded runtime symbol
providers linked explicitly, and undefined-symbol rejection. The original C
source hash and entry-source hash remain mandatory. Missing/changed providers,
unknown ELF layout, absent constructor initialization or mismatched entry bytes
refuse setup without fallback. ELF checks establish static shape only, **not**
ABI equivalence, authentic child entry or performance acceptance. The real
configured workload, kernel lifecycle, source/line evidence and genuine signature
checks remain mandatory; this candidate is not enabled by the standalone runner.

The prior native source/clock proof is bounded correctness evidence, **not**
Recovery 5-second/15-second deadline or performance acceptance. Its 120-second
owned-fixture guard is not authority to widen any original application clock.
Setup refusal is fatal before the consumer body. Partial initialization/capture
resources are cleaned immediately and again idempotently during the authorized
completion exchange; missing/unproven cleanup cannot produce an admitted result.
A refusal retains the original bounded error/stdout/stderr evidence and actual
reached/failed setup phase before sending the completion reply, even when no
entry listener was allocated. Cleanup distinguishes absent from closed resources.
Unavailable build argv, ELF identity and runtime providers stay null, never
filled from a planned command. A reporting/cleanup failure cannot replace the
first setup error or turn a refused transaction into success.
A failure-only caller interval is retained after completion/cleanup before the
original refusal propagates. Native setup-refusal validation remains a separate
explicit check; source-only protocol tests do not claim privileged execution.

An abrupt exit after authentic entry can retain valid source records only when
the real kernel birth/exec/exit ledger and source evidence match. Its exit code
alone never establishes “no SDK.” Missing exit, missing entry, unknown source or
observer loss remains refusal even when the application reported success.
Owned-test completion replies use bounded frame reads with deadline checks
throughout the frame, not just before its first byte. A prefix without a newline
cannot hold setup or completion past the existing fixture deadline.
Inventory derivation may reuse the exact bytes just checked for source equivalence,
but launch-time equivalence and end-of-run source rechecks remain mandatory.

`validate-and-promote.yml` earns separate Linux and Windows evidence under
`fleet-manager-linux` and `fleet-manager-windows`. Its two independent
`fleet-manager-baselines-linux`/`fleet-manager-baselines-windows` jobs use literal
GitHub-hosted runner labels, not dynamic runner expressions, preserving the
trusted-routing contract. Both jobs must succeed and both
artifacts must exist before promotion. Artifact names retain the existing
`coverage-baseline-` prefix; JSON filenames match the embedded baseline identity.
The existing publisher writes the small main-history pointers and publishes their
full JSON maps as Release assets at `coverage-baselines-<measured-dev-SHA>`.

This is **stage 1**. A locally produced JSON is validation evidence, not an admitted
production baseline. Stage 2 may activate strict required selection only after a
real promotion has published both assets and qualifying main-history pointers.
It must resolve the appropriate platform identity, verify freshness and attribution,
and fail visibly on unavailable/untrusted evidence rather than run a full suite
or treat a null decision as a successful empty run. Existing shadow consumers
remain unchanged.

Workflow-structure assertions require PyYAML in the prepared tooling environment.
The manual integration lane declares it through governed dependency admission;
missing YAML support fails visibly rather than skipping required assertions.
