# Phase 8 - Dev-owned module-size baseline updates

## Guiding Intent

Baseline maintenance must not create an independent release-branch writer that
invalidates a generated promotion while it is waiting to merge. `dev` owns the
module-size policy baseline; ordinary validated promotion carries that policy
to `main`. Keep post-merge maintenance reviewable and keep privileged execution
under trusted release-control code.

This is vision-closing for
[`main-owned-workflow-control`](../../../visions/main-owned-workflow-control/README.md):
`one-compatible-execution`, `validated-composition`, and the boundary against
duplicate competing release writers. It follows the existing
[promotion pattern](../../../docs/patterns/dev-main-promotion-pipeline.md).

## Request

> Another set of agents is dealing with 6408. We should fix pipeline behavior
> so module size baseline updates can't much up promotion

The prevention driver owns source changes only. Live recovery of PR #6408,
candidate edits, workflow cancellation, and manual release intervention remain
outside this slice.

## Context

Generated candidate #6408 was based on a release tip that baseline-refresh
PR #6071 subsequently advanced. Both changed `tools/module-size-baseline.json`;
the candidate became conflicting after validation had passed. Its exact-PR
waiter then retained the promotion workflow's exclusive queue slot.

The candidate's baseline blob is identical to the baseline in its validated
`dev` input. An independent baseline PR to `main` is therefore not needed to
establish baseline authority. Serializing only workflow execution would not
serialize an already-open housekeeping PR's later merge.

Coordination token: [issue #3336](https://github.com/ThomasMichon/copilot-extensions/issues/3336).
The publication-attestation work under #6332 retains its existing owner.

## Scope and Design Contract

_(Agent-recommended)_ The authority migration below and its adversarial
validation scenarios are the reviewed mechanism for satisfying the operator's
prevention request (plan PR #6436).

- Baseline maintenance reads one pinned, committed `dev` snapshot as data and
  proposes a baseline-only PR to `dev`. Normal review and contribution CI
  remain required; the updater does not approve or merge its own change.
- Workflow declarations and helper implementations execute from trusted,
  compatible release control. Do not switch privileged execution to mutable
  `dev` YAML, import `dev` tooling, or run candidate-supplied scripts with write
  credentials. A source checkout used for measurement is data, not executable
  authority.
- Preserve default shrink-only behavior and the existing deliberate,
  post-merge widening exception. Never automatically grandfather a new
  over-cap module or widen an ordinary contributor PR's own diff.
- Baseline generation is deterministic for the pinned snapshot. Refuse unsafe
  source paths, malformed baseline data, and stale publication inputs with
  explicit diagnostics; do not overwrite a newer maintenance PR silently.
- Generated/vendored path differences need actual promotion-fixture coverage.
  If release materialization requires derived ceilings, derive and validate
  them inside the generated promotion, not through another `main` writer.
- Retire the baseline-only `main` admission exception only after its
  replacement is proven and its trusted-control rollout is coordinated.
  Ordinary promotion, rollback, and declared control repairs keep their own
  acceptance boundaries.
- Do not alter the live #6408 candidate or its pipeline run, bypass checks,
  force a release, or modify #6332's signing/attestation surfaces.

## Plan

- [x] Land this reviewed design before changing the mechanism (PR #6436).
- [x] Inspect and reuse the trusted-main admission/implementation pattern and
  existing module-size refresh semantics.
- [ ] Implement pinned-data measurement and reviewed `dev`-targeted
  maintenance, preserving the workflow's release-control trust boundary.
- [ ] Wire all relevant workflow, admission, verification, and documentation
  surfaces so none still advertises or opens baseline-maintenance PRs to `main`.
- [ ] Add focused real-Git and workflow-contract regressions, then run the
  smallest bounded test selections that cover these surfaces.
- [ ] Drive the implementation PR through review and merge; record the
  coordinated release-control rollout and its actual evidence.

## Validation Plan

- [ ] Reproduce the old candidate/baseline collision in isolated Git fixtures.
  Prove the replacement updater changes only its `dev` PR branch and leaves
  the existing `main` tip and generated promotion head unchanged.
- [ ] Prove ordinary and baseline-maintenance PRs still pass through review
  and required contribution checks; a baseline-only PR to `main` cannot use
  the retired exception after the replacement rollout.
- [ ] Cover unchanged, grown, shrunk, graduated, and newly over-cap modules;
  new offenders fail rather than silently receiving grandfathered ceilings.
- [ ] Cover generated/vendored source layouts through the actual promotion
  helper; no materialized module loses its intended guard protection.
- [ ] Prove pinned inputs, deterministic JSON, unsafe-path refusal, and
  concurrent `dev` advancement fail-safe behavior.
- [ ] Put deliberately hostile tooling in the data snapshot and prove it is
  not imported or executed. Verify write credentials are unavailable to
  measurement and the trusted helper controls all privileged publication.
- [ ] Validate the actual workflow declarations and helper invocation
  boundaries, not merely a stand-in function.
- [ ] Run targeted tests through the bounded supervisor, with no production
  workflow dispatch, cancellation, closure, or release used as a test.
- [ ] Reconcile the trusted-main control rollout with its owner and inspect
  the deployed baseline workflow's target and authority before claiming live
  prevention. A merged `dev` implementation alone is not rollout evidence.

## Coordination and Completion

The Baseline prevention driver owns this slice's source PRs and evidence under
#3336. Other participants retain Phase 7, live release recovery, and publication
attestation. Completing this slice does not declare the broader release-pipeline
effort or any downstream effort Done.

## Journal

### 2026-10-12 - Prevention scope proposed

The operator assigned durable prevention separately from live recovery.
Read-only Git and provider evidence established the single-file baseline
collision and the competing publication paths. No live release intervention
was performed for this proposal.

The plan's CI exposed a separate shared-library fixture defect: the
cross-process dial-log test observed 239 of 240 lines. Its intended 30-second
lock wait used `functools.partial`, but the production call's explicit
two-second argument overrode that partial keyword. The fixture now sets the
constant read by that call; production best-effort logging is unchanged. The
named regression passed through the bounded supervisor on Windows. Required CI
and plan review subsequently passed; PR #6436 merged as `df15d7ce2`.

### 2026-10-13 - Prevention implementation and isolated proofs

The same worktree advanced from the reviewed plan to Git-object measurement
under trusted policy and a leased dev-only proposal publisher. The producer
does not check out or execute the measured source's tooling. Its write
credential is confined to publication; source freshness and proposal ownership
are independently checked.

The legacy main-baseline admission path is removed from both gate and reminder
recognition. Targeted Windows contracts passed with explicit Linux-only Bash
skips, and native Linux executed the actual Bash publisher and admission
contracts against disposable Git remotes. A real promotion fixture proved
that dev baseline policy survives modern editable-reference materialization
and protects both canonical and generated modules.

The first materialization fixture used a retired directory-pointer schema and
was correctly refused. Replacing that fixture with the supported editable
reference exercised the actual current generator successfully; production
materialization and signing code were not changed.

Implementation review, merge, and actual trusted-control rollout remain open.
No live candidate recovery or production workflow dispatch was performed here.

Independent review identified the steady-state existing-PR race: a branch
update could change an already-reviewable proposal before a stale-source
postcheck failed. The publisher now parks existing ready proposals before
mutation, creates new ones as drafts, and checks source/head freshness before
review admission. Earlier draft holds are preserved without cross-run
inference, and armed auto-merge requests are refused. Native Bash regressions
cover the ordering and stale-source hold; the reviewer verified the named fix.

The freshness checks now fetch dev into an explicit remote-tracking destination.
A real-Git fixture narrows the checkout's configured fetch mapping to main,
leaves a stale `origin/dev` locally, and proves the producer and both admission
steps refresh it before measuring or rejecting an outdated proposal. This avoids
depending on a checkout's implicit remote-ref mapping.

Required CI runs only a path-gated contract smoke. A separate weekly/manual
workflow owns the full baseline portfolio, including publication races and the
actual materialization fixture, without scheduling the entire CI matrix.
The full selected Linux portfolio passed 64 tests through the bounded runner;
the revised Windows smoke passed 7 tests with 3 Linux-only skips. The contributor
guide now links the dev-owned maintenance contract rather than the retired
main-writer convention.

## See Also

- [Parent effort](README.md)
- [Release mechanics](../../../docs/pipelines.md)
