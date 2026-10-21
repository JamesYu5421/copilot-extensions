# Phase 7 - Platform-local installed runtime

- **Parent:** [Worktree Manager control plane](README.md#phase-7a---platform-local-installed-runtime-7305)
- **Status:** Active; original proposal reviewed and merged in #7339;
  operator-approved runtime-isolation revision pending review in #7420.
- **Coordination:** [#7305](https://github.com/ThomasMichon/copilot-extensions/issues/7305)
- **Participant / PR owner:** Platform runtime implementer (`spry-workbench`).

## Request and guiding intent

**Sanitized summary of operator authorization:** repair the Manager's versioned
installer/runtime through the normal source, review, release and authorized
local-update flow. Preserve the configured package feed and seven-day dependency
policy. Other update-flow and sibling-runtime repairs retain their current
owners. No deployed-file patch, package-source substitution, seasoning waiver,
arbitrary service restart or fleet rollout is authorized.

**Subsequent operator authorization:** revise the qualified environment layout
and continue through the same implementation PR. Keep the qualified non-editable
runtime separate from the legacy UV-managed `.venv`: already-loaded old launch
code can read the newly published selector and synchronize that legacy path.
Atomic dispatcher publication alone cannot protect the qualified environment
from that in-flight reader. This authorization changes neither deployment
authority nor the retained feed, dependency policy, sibling ownership or
published-consumer gates.

The implementation sequence and validation matrix below are
**agent-recommended** realizations of that approved boundary, not additional
operator requests. This plan closes the existing installer vision's
`one-line-bootstrap`, `self-updating` and out-of-plugin delivery intent; it does
not introduce a new deployment/source policy.

## Established problem and scope

The generated Manager launchers execute `uv run --project <slot>`. That performs
a universal project solve at invocation time, including Linux-only requirements
on Windows. A configured index can legitimately reject one of those artifacts
under its enforced cutoff; changing a dependency floor or passing a native
platform flag did not qualify the actual consumer in #7305.

`self_install_payload` currently materializes first-party libraries and copies
source, but excludes the source `.venv` and publishes `.install-complete`
without constructing an importable installed environment. A copied payload or
version marker is therefore not installed-runtime health.

Own Manager bootstrap, payload/runtime construction, completeness checks and
generated launchers, plus directly related tests and documentation. Reuse the
existing stdlib installer dependency policy and attributable first-party
materialization mechanisms where applicable; ship any required policy bytes
with the standalone Manager, never import an installed sibling plugin.

Do not modify Linux dependency pins, source-selection policy, the initializer's
exit/error propagation, Terminal profile error presentation, daemon ownership,
restart recovery, Bridge transport or Index construction. Those remain separate
scopes. A newly discovered shared-file conflict requires coordination before
implementation; public-file absence is not proof of unpublished custody.

## Plan

### 1. Review and coordinate

- [x] Land this proposal through the normal automated review/CI/merge gate.
- [x] Recheck #7305 coordination and any overlapping PRs before implementation;
      preserve its initializer/profile-diagnostic owner. Bind only this declared
      Phase 7a slice, never the entire umbrella.

### 2. Construct a complete platform-local runtime

- [x] Reuse the supported `uv venv` plus non-editable `uv pip install`
      construction path, resolving only for the selected interpreter/platform.
      Install the Manager and materialized first-party libraries into the
      candidate environment, without editable references to fetched or temporary
      source. Preserve native configured indexes, authentication, TLS, stronger
      cutoffs and the canonical retained-input admission/execution boundary.
      Do not invent an independent dependency-policy implementation.
- [x] Keep source/pointer/symlink checks and existing cutover lease/recheck
      semantics. Do not overwrite a healthy active or currently executing
      environment to repair metadata. Construct a new candidate before selection;
      failed construction leaves the prior marker, slot and live sessions usable.
- [x] Isolate qualified environments in the slot's `.runtime`, reserving `.venv`
      for legacy UV readers. Align qualification markers, payload-root discovery,
      completeness/copy exclusions, generated launchers, internal adapters and
      the Worktrees consumer. A broken qualified runtime must never fall back to
      UV. Preserve only the explicitly identified source-only legacy contract.
- [x] Validate with the candidate's own interpreter: actual module import,
      expected version and bounded CLI health. Account for environment relocation:
      probe at its final runtime path before publishing the active marker, and
      retain rollback through a failed final-path probe. No temporary build path
      may remain necessary for ordinary invocation.
- [x] Publish completion only after successful runtime qualification, then use
      the existing activation/cutover path. Same-version readiness must detect a
      source-only legacy slot or a missing/broken environment and report a
      truthful repair/refusal, not claim success from an old completion marker.

### 3. Align launch and first touch

- [x] Generate Windows `.cmd`/PowerShell and POSIX launchers that resolve the
      selected version and execute that slot's own interpreter. Ordinary version,
      profile and CLI calls perform no dependency solve and require no `uv` on
      PATH. Preserve argument boundaries, exit codes and UTF-8 protocol stdout;
      missing runtime errors remain actionable on stderr.
- [x] Make both bootstrap scripts enter the same platform-local construction
      contract before importing runtime-dependent Manager modules. The first
      touch must work without an existing Manager, its venv, plugin runtime or
      monorepo checkout, and then invoke the installed command rather than
      re-solving the fetched source. Propagate construction failure without
      launching a half-installed Manager.
- [x] Update the Manager installation/repair documentation and directly affected
      install-contract descriptions. Add the required Manager changefile; leave
      release version stamping to promotion.

### 4. Qualify and deliver

- [x] Complete local pre-publication validation, record exact results and review
      documentation impact. Published-consumer qualification remains a separate
      post-release gate below, not a prerequisite that could precede its own PR.
- [ ] Address review feedback, merge with required checks, and observe actual
      promotion of the changed bytes. A Dev merge is not a released runtime.
- [ ] Run the already-authorized local unified update after release, not a
      standalone production installer. Prove the selected Manager version and
      real profile JSON consumer. The complete-update exit-0 gate remains
      jointly dependent on the retained #7305 diagnostic owner and sibling
      blockers; do not relabel partial rollout as success.
- [ ] Journal delivery and resolve this slice, transferring any concrete
      remaining gate to its named tracked owner. Leave the parent effort Active
      while its other phases remain open; release/finalize this source workspace
      only through the managed lifecycle.

## Validation plan

- [x] Prove an actual old UV caller started before publication can read the new
      selector afterward without changing any qualified `.runtime` byte or
      installed identity. Permit its legacy `.venv` synchronization in the
      disposable fixture; then run the new dispatchers without UV.
- [x] Repeat layout-affected native bootstrap and policy-negative contracts.
      Native dispatcher, internal-adapter and engine checks are complete;
      earlier same-`.venv` bootstrap evidence does not qualify the revised layout.
- [x] Focused unit/contracts: actual platform dependency markers and non-editable
      first-party installs; supported dependency-policy invocation and rejected
      admission; missing interpreter, failed install/import/version/CLI health;
      legacy source-only slots, idempotence, metadata-only repair, lease
      contention, concurrent installers and failed-publication rollback.
- [x] Simple end-to-end: construct a disposable standalone materialized slot,
      execute its generated native launcher with paths/arguments containing
      spaces, and read actual version/import-identity JSON and exit status.
      The real profile-JSON consumer remains the post-release gate below. Remove the
      fetched source and remove `uv` from the invocation PATH; assert launch
      still works without a new solve. Verify final-path execution after
      candidate relocation, not just pre-swap imports.
- [x] Dependency-policy negatives: an ineligible dependency fails construction
      without weaker retries, source/index changes, activation or damage to the
      previous slot. Prove Windows construction does not request Linux-only
      D-Bus dependencies; Linux retains the declared requirement.
- [x] Fresh-box: extend/run the existing Manager bootstrap clean-room scenario
      where practical. Exercise both shell adapters, including native Windows
      PowerShell 5.1 and 7 failure/argument behavior. Record unavailable lanes
      and reasons explicitly; a warm reconcile is not first-touch evidence.
- [x] Run the smallest repository-standard contained Manager suites covering
      these changes, touched-Python lint and relevant install-contract/vendor
      guards. Escalate only on discovered failures; retain all existing budgets
      and process containment. Native launch changes owe zero-window/focus
      regression evidence rather than a mock of process creation.
- [ ] Published consumer: after promotion and the authorized local unified
      update, verify selected-version/importability, actual launcher output and
      Terminal profile JSON, the update's final exit status, and preservation of
      the original live CLI. No GUI/mux activation or remote-machine mutation
      is needed merely to qualify this bounded headless runtime contract.
- [ ] Native Linux nested-construction custody: execute the preserved timeout,
      interruption and escaped-session negative-control cases in an approved
      Linux lane. The Windows state-machine contracts do not qualify this
      native POSIX behavior. Keep this gate owned by the platform runtime slice
      under #7305 until it is executed or explicitly transferred.

## Journal

- **2026-10-20** - Qualified the revised layout against committed candidate
  `b56efeb8623055c263c3f8e2e14cf90886a1767c`. Native PowerShell 5.1 and 7
  each fetched a complete, clean candidate checkout, constructed a fresh
  standalone `.runtime`, published `platform-runtime-v2`, and reused it on a
  warm invocation without rewriting completion. After removing the fetched
  source from its original path and removing UV from PATH, installed identity,
  owning payload/assets, CLI health and the generated launcher still passed;
  Linux-only D-Bus was absent from both Windows environments.
  An actual candidate with a direct remote dependency lacking release-age
  evidence was refused by the canonical policy on each run. The prior selector,
  every qualified environment byte and CLI remained intact; no rejected slot
  or staging directory remained. Both disposable roots were cleaned only after
  contained teardown. The production selector was byte-unchanged throughout.
  Existing Git/UV prerequisites were used; this is not pristine prerequisite
  provisioning, Linux/macOS qualification or published rollout. Lint,
  module-size, headless-launch, canonical-policy-copy and install-contract
  checks pass. Documentation impact: aligned the Manager recovery contract,
  standalone install-contract analog and clean-room scenario with the isolated
  layout and explicit legacy compatibility. Native Linux, review, release and
  authorized published-consumer gates remain open.

- **2026-10-20** - Implemented the approved `.runtime` / `platform-runtime-v2`
  isolation boundary and aligned qualification, payload ownership/copy exclusion,
  generated launchers, internal adapters and the Worktrees engine. The engine
  preserves direct-interpreter compatibility for zero-byte legacy completion
  markers; neither it nor the dispatchers substitutes UV for a broken qualified
  environment. New format-rejection and source-environment-copy tests cover the
  distinction.
  An actual offline UV caller starts before selector publication, waits before
  reading it, then installs the newly selected project editably in `.venv`.
  The qualified `.runtime` remains byte-identical, passes installed identity and
  CLI health, and supports two no-UV cycles through all four native Windows
  dispatchers. The contained Manager selection passes **231 tests**, with eight
  explicit platform skips; the Worktrees consumer selection passes **81**.
  The final format/old-reader selection passes **6**. Initial desktop-observer
  runs were inconclusive because focus changed to a pre-existing terminal;
  enriched diagnostics retained the same strict failure assertion, and the
  operator-confirmed untouched-desktop run passed with no visible windows or
  focus changes. Fresh bootstrap/policy-negative proof of this layout, native
  Linux, review, release and published-consumer gates remain open.

- **2026-10-20** - The operator approved revising the environment layout on
  implementation PR #7420 after the old in-flight UV-reader gap was identified.
  Updated this plan before implementation: the qualified non-editable runtime
  will use `.runtime`, distinct from UV's legacy `.venv`. Current pre-revision
  tests pass 245 cases with eight explicit platform skips, but do not prove the
  new isolation boundary. The new old-reader and layout-affected consumer gates
  above remain unchecked. Review, native Linux, release and authorized local
  update qualification remain open; no deployed files or running services were
  changed by this decision.

- **2026-10-20** - Addressed the additional nested-POSIX-constructor finding
  locally. Foreground commands already inside the declared Manager-owned
  process group now retain it, including the ownership marker for deeper
  nesting; they no longer create independent sessions that escape the outer
  timeout/cancellation sweep. Windows Job and contained-test suppression remain
  unchanged. The focused bootstrap/process selection passes **18 tests**;
  five native Linux/POSIX cases are explicitly skipped on Windows.
  Added native Linux bootstrap-boundary regressions for timeout and interruption,
  plus an intentionally escaped-session negative control. They check constructor
  exit before deleting the temporary runner and retain identity-pinned pidfds
  for fixture cleanup if an escape is detected.
  Native Linux execution remains **unqualified**, not a pass: the local host
  has no general-purpose Linux distro, the Docker backend is unavailable, and
  the existing periodic integration route covers plugin suites but not the
  standalone Manager. No system distro, alternate venue or new workflow was
  activated to bypass that boundary. The explicit native gate above remains
  open under #7305. Review, release and authorized local consumer proof remain
  separate open gates.

- **2026-10-20** - Closed the subsequent review's remaining engine consumer
  and metadata-repair gaps locally. The Worktrees launcher probe now executes
  the selected Manager interpreter directly with the shared headless helper,
  preserving its existing retry and actionable-error behavior. The Worktrees
  release entry covers that tightly coupled consumer change; #7410 retains
  initializer/profile diagnostics and #7431 retains transport ancestry.
  Healthy-runtime launcher repair now takes the install lease, rechecks
  readiness and shares preimage rollback without republishing the selector or
  rebuilding the environment. Regressions truncate an existing launcher before
  raising either a write error or cancellation, then prove restoration and
  lease release.
  The contained Worktrees consumer/launch selection passes **79 tests**;
  the Manager install/runtime selection passes **102**, with two Windows
  symlink-permission skips and the separate opt-in desktop observer disabled
  for that run. A further opt-in native engine qualification passes both
  no-UV probe cycles through the real prepared Worktrees interpreter with
  unchanged Manager environment bytes and zero visible windows/focus changes.
  No remote or production runtime is changed by these checks. Existing
  pristine-box/Linux/macOS limitations remain explicit; review, promotion and
  published local consumers are still open gates.

- **2026-10-20** - The next current-head review identified five internal mux/
  Companion adapters still entering `uv run --project`, which could overwrite
  the same qualified environment with an editable install. Registration,
  pane removal and Companion now use their owning slot's installed interpreter,
  without a global-version lookup or project synchronization. Windows
  background dispatch is headless and quotes native argument boundaries.
  The final contained nine-file selection passes **235 tests**, with only the
  two Windows symlink-permission skips. Actual PowerShell 5.1/7 registration,
  removal and generated Companion popup commands, plus Git Bash registration
  and removal, each run twice without UV or fetched source, retain the exact
  installed module/prefix, leave every environment file byte unchanged and
  pass installed health afterward. Native observation records no visible
  windows or focus theft across those calls and the existing launcher cycles.
  The popup test executes the generated command through native CMD; it does
  not claim a live interactive mux/keybinding qualification. Linux/macOS and
  pristine-box lane limitations remain as recorded above. Lint, headless-launch
  and patch checks pass. Documentation impact: the Manager recovery contract
  now explicitly includes these internal callers. Review, release and the
  authorized published-consumer gate remain open.

- **2026-10-20** - Addressed four findings from implementation PR #7420's
  first current-head review. Native PowerShell IEX failures now throw without
  exiting the caller; separate script invocation retains the child's exit
  code. Both bootstrap adapters require Python 3.11+ before dependency-free
  TOML imports. Publication retains launcher bytes/permissions and selector
  preimages through partial launcher writes, selector failure and cancellation,
  including source-only legacy selection. Final-path health cancellation
  restores the previous slot before propagating the interruption.
  The contained six-file Manager selection passes **183 tests**, with two
  Windows symlink-permission skips and the opt-in desktop-observer case skipped
  (its prior two-cycle qualification remains recorded above). It includes native
  PowerShell 5.1/7 IEX and script-process failures and a real isolated `-S`
  bootstrap import without third-party site packages. Touched-Python lint,
  module-size, headless-launch and install-contract guards pass.
  Documentation impact: updated the Manager bootstrap/recovery contract;
  these repairs close the already-reviewed intent rather than change it.
  Review acceptance, release and published-consumer qualification remain open.

- **2026-10-20** - Local pre-publication qualification is complete. The final
  contained selection passes **196 tests**, with two explicit Windows
  symlink-permission skips; current root containment/vendor tests pass
  **131**, with one platform skip. Lint, module size, headless launch,
  canonical policy-copy and install-contract guards pass.
  Corrected native PowerShell 5.1/7 bootstrap runs each complete a clean fetch
  at the candidate HEAD, qualify a fresh environment and reuse it on the next
  invocation; Git status, not HEAD alone, confirms source completeness.
  Installed launchers run after removing their fetched source and `uv` from
  PATH. CMD, PowerShell 5.1/7 and Git Bash pass the native two-cycle
  window/focus contract. Direct-base execution is verified by the actual
  executable image and owned wheel context, with the venv-launcher environment
  applied only at the final process boundary.
  An actual unaged-source candidate is refused by the canonical policy before
  selection, its candidate is removed, and the prior installed CLI still works.
  No live installation is qualified by these disposable checks.

  Native Windows first touch used existing Git/UV prerequisites, not a pristine
  machine that provisioned those tools. The Docker backend was unavailable;
  Linux/macOS native and pristine-Docker execution are explicitly unqualified,
  while their source adapters and the extended scenario remain in the change.
  The installed headless Picker initialized successfully; a populated preview
  additionally requires a genuine registered engine project, intentionally
  absent from this standalone test. No interactive/mux session was activated.
  Review, release, authorized rollout and the real profile-JSON/unified-update
  consumer gate remain open.

- **2026-10-20** - Rejected an exit-zero native bootstrap observation after
  Git had failed its checkout. Windows bootstrap now enables long paths per Git
  invocation and refuses clone/fetch/checkout/extraction failures before
  construction; self-update uses the same process-local Git setting. Native
  PowerShell 5.1/7 failure contracts retain the original exit-17 diagnostic,
  preserve the previous selector and prove the constructor stays uncalled.
  The focused fetch/update/delivery selection passes 76 cases. Full positive
  bootstrap must be rerun against the corrected candidate and a clean fetched
  tree, not qualified by a matching Git HEAD or launcher exit alone.

- **2026-10-20** - Implemented the bounded construction/launch slice locally.
  The focused contained selection passes 184 tests with two explicit native
  symlink-permission skips, including two-cycle CMD/PowerShell 5.1/7
  window/focus observation, exact failure exits, real-process lease rechecks,
  owned descendant timeout cleanup and wheel-layout launch-asset lookup.
  Root containment/vendor tests pass 62 cases. Lint, module-size and headless
  guards pass; canonical dependency-policy copies and install-contract checks
  also pass. Full native bootstrap against the candidate, negative policy
  construction, provider review, release and authorized rollout remain open.
  The Docker clean-room backend is unavailable; the scenario was extended,
  not reported as executed or qualified.

- **2026-10-20** - Plan #7339 merged after current-head Copilot approval and
  required green checks. Reconciled the merged plan and preserved the other
  #7305 participant's initializer/profile-diagnostic scope. The next stretch is
  runtime construction, final-path validation and launcher/bootstrap alignment;
  all implementation and deployment gates remain open.

- **2026-10-20** - Proposed this bounded source repair after public coordination
  on #7305. No code or installed-runtime change has been made. Review of this
  proposal is the next gate; other participants retain their existing work.
