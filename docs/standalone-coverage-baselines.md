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
through the existing `uv` configuration; no alternate feed is selected.
The dedicated full lane allows 720 seconds for instrumented execution inside its
15-minute job budget; ordinary required CI budgets are unchanged.

## Source equivalence and provenance

Before and after collection, installed Python modules must match the canonical
checkout byte-for-byte and have the exact expected inventory. The wheel's
generated `_versioned_runtime.py` maps to its canonical library, not a nonexistent
source mirror. Only verified installed paths receive repository line coordinates.
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

`child_audit.py` observes CPython `subprocess.Popen`/`os.posix_spawn` audit events,
without wrapping Popen or waiting for initialization. Each intent is matched to
exactly one dead producer by run, known test context, argv digest, audited owner
and creation/birth order. Children validate their auditor ancestry, including the
Windows venv redirector. Audit registration is self-tested. Empty-source-hit
producers remain distinguishable from missing producers.

The parent validates every manifest against its source inventory and context map,
rechecks installed bytes, rejects live writers, and validates bounded framing,
version, IDs and line ranges before unioning child hits with parent attribution
in schema 3. Missing hooks/evidence, bootstrap errors swallowed by `.pth`,
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

Windows uses digest-named `tmp_path` directories beneath a short checkout-local
sandbox to preserve native artifact path headroom. Extended/UNC prefixes are not
used: those prefixes change SQLite's file-URI semantics. Other collectors and
platforms retain their ordinary pytest temporary-path behavior.

## Publication and the bootstrap gate

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

Workflow-structure assertions require PyYAML and skip only those assertions in
the pytest-only manual tooling environment; collection, source-equivalence,
selection and provenance tests continue to run without it.
