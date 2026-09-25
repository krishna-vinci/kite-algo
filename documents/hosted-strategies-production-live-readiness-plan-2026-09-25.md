# Hosted strategies: production and live readiness plan

Date: 2026-09-25. Baseline: `development` at `eb2cb00` (B1 committed, not pushed). Deployed runtime: `8f9438c`,
migration head `20260924_000043`.

Authority: `hosted-strategies-architecture-r3.md` → `hosted-strategies-implementation-roadmap.md` → current
source/schema → phase reports. R1/R2/proposal docs are history only. OpenAlgo may be read for UX ideas only;
no code is copied from it.

## Goal

Make the hosted-strategy platform production-ready and live-ready for all four lanes (CNC, MIS, futures,
options). Strategies decide desired state; the platform owns validation, execution, attribution, protection
and settlement. **No real-money order is placed by any step of this plan.**

## Working model

- Claude plans, reviews, integrates, and commits unsigned locally after each accepted slice. No push or
  deploy without explicit user approval.
- Codex agents implement one slice per dispatch (`.claude/skills/codex-orchestrate/`).
  `fast` (`commandcode/deepseek-v4.1-flash`) for broad implementation, `precise` (`zai-coding/glm-5.3-flash`)
  for narrow high-risk fixes and review findings.
- Testing follows `AGENTS.md`: new tests for new behavior plus the existing test file of each changed module;
  the matching `*_postgres.py` file only when SQL/repository behavior changes; the supervisor-child harness only
  at the end of each phase, not per slice. Safety gates get one refusal test **and** its passing twin (a
  mutation pair), not exhaustive matrices.
- Dirty-tree rule: unrelated user files stay untouched and unstaged (R1 doc, untracked docs, `.commandcode/`,
  evidence files from earlier runs).

## Current state (verified 2026-09-25)

- Options engine: one `OptionRunState` lifecycle per whole structure (`created → entering → entered →
  exiting → exited/settled`, plus `partial_entry`, `cleanup_required`, `partial_exit`). The compiler
  (`backend/strategies/compiler/option_structure.py`) freezes `entry` or `exit` only. No adjust phase.
- B1: duplicate equivalent entry refused at **execution** (`OPTION_STRUCTURE_ALREADY_OPEN`); option-run work is a
  continuation axis; unresolved protective stage blocks exit and continuation.
- No repair path for `partial_entry` / `cleanup_required` option runs (only `lifecycle.mark_cleanup_required`).
- Per-strategy admission policy exists as an operator-set row (`StrategyAdmissionPolicy`: allocation, per-instrument
  and gross notional, max open instruments, rate, daily loss). It is not declared by the strategy version and has no
  options fields (max loss, margin, structure families, naked permission).
- Live: all lanes wired and deployed with `HOSTED_LIVE_ENABLED=true` (2026-09-22), no live account in
  `HOSTED_STRATEGY_ACCOUNT_SCOPES`. Live staged financing refuses `STAGED_LIVE_FINANCING_UNSUPPORTED`.

## Phase B2: governed dynamic options (paper)

Slices run in this order. Each one is committed after review before the next starts, unless marked parallel.

### B2.1 Early duplicate admission and partial-run repair

- **a. Admission-time structure gate.** Before owner approval is requested, and again at admission, run the B1
  equivalence check (reuse `_assert_no_equivalent_open_structure` discovery and equivalence; do not fork it).
  Also refuse **any** new option entry while this strategy owns a run in `created / entry_previewed / entering /
  partial_entry / cleanup_required / exit_previewed / exiting / partial_exit` or with unknown discovery:
  `OPTION_STRUCTURE_UNRESOLVED`. Expose the named status in the proposal/plan view so the owner is never asked to
  approve a plan that will be refused. The execution-time B1 check stays (defence in depth).
- **b. Repair path.** Operator/platform repair for `partial_entry`, `partial_exit`, `cleanup_required`:
  compute the residual from the run's **own** confirmed fills, then either (1) submit a governed risk-reducing
  close of the residual through the existing staged exit / exit builder (short liabilities first), or
  (2) record an owner-confirmed disposition with evidence. Ambiguous runs are never auto-adopted; they escalate
  by name (`OPTION_RUN_REPAIR_AMBIGUOUS`). Owner-authenticated route + repository; no new execution engine.

### B2.2 Desired-state option proposals (adjust phase)

- Extend `target_option_structure` with an immutable full desired state: underlying, expiry, product,
  structure identity, **structure units**, leg roles, ratios, sides, protection policy, expiry policy,
  max-loss policy.
- The compiler freezes the desired state; the platform computes the delta from the run's own confirmed
  quantities → phase `adjust` (or `entry` when nothing is owned, `exit` when desired is empty).
- Supported deltas: increase/decrease units, add/remove hedge legs, change ratios, roll expiry
  (close old run → prove close → open new run, linked like futures rolls), explicit exit, re-entry only after
  proven close/settlement. Anything else refuses `OPTION_ADJUSTMENT_UNSUPPORTED`.
- Engine: an adjust transition on `OptionRunState` that versions the leg set (new `structure_digest`
  generation), sequences risk-reducing legs first and hedge-before-short for increases, fill-gated with the
  existing hedge gate. Needs a migration if the leg-version history can't live in existing JSON columns.
- **Design gate:** Codex writes a short design note (state transitions, schema delta, refusal names) for Claude to
  review before implementing.

### B2.3 Dynamic delta hedging (example + harness)

No new platform surface beyond B2.2: a strategy reads chain/Greeks through existing data access and submits a
new desired state. Deliverable: example strategy (delta-neutral straddle resize + expiry roll) and one
supervisor-child harness scenario proving resize and roll through the governed path.

### B2.4 Protection ownership

Durable protection-owner row keyed by (strategy, account, environment, option run, protection policy version):
atomic owner transfer (compare-and-swap on version), unique active owner, active protective action blocks
conflicting adjust/exit, unknown owner blocks new exposure but never blocks risk-reducing actions. Migration +
repository + gates in execution/adjust paths.

### B2.5 Per-strategy risk policy (declared by version)

Strategy version manifest declares: max loss, notional limit, margin limit, protection thresholds, expiry
policy, allowed structure families, naked permission. Frozen with the version and enforced at admission as
`effective = min(declared, operator policy, platform ceiling)`; ceilings only tighten. A version without a
declared policy cannot enter options exposure (`STRATEGY_RISK_POLICY_MISSING`).

### B2.6 Options operations UI (`frontend-next`)

Structure/leg view (quantities, ratios, Greeks, premium, MTM, margin/exposure, partial fills), repair state and
actions (from B2.1b), protection policy and active owner, pending adjustments, approvals, expiry deadlines,
refusal reasons. Four clearly separate controls: stop evaluator / cancel pending work / exit structure /
flatten. Reuse existing hosted-strategy UI patterns and API; vitest on new components only.

**B2.6b owner actions.** Safe owner-facing routes for the three controls B2.6a leaves disabled. Cancel pending work
cancels this strategy's pending ENTRY orders only, never protective or exit orders. Exit structure is a
platform-generated governed exit plan for one option run. Flatten runs governed exits for all of the strategy's
exposure. B2.6b also adds the protection-owner display (after B2.4) and an operator disposition for an adjust
submission that has no outcome.

**B2 exit gate:** options harness scenarios (restart/hold/close, adjust/resize, roll) pass on paper; momentum
recurring regression passes once; report `documents/hosted-options-dynamic-b2-<date>.md`.

## Phase C1: live readiness (no real orders)

- **C1.1 Live staged CNC financing.** Replace `STAGED_LIVE_FINANCING_UNSUPPORTED` with: reductions submitted
  first; dependent buys wait for broker-confirmed fills; authoritative funds/margin read after confirmation;
  funds reserved under account lock; every dependent buy revalidated immediately before submission;
  partial/rejected/unknown reductions refuse dependent increases; a projected sale never funds a live buy.
  Proven against the fake broker boundary only.
- **C1.2 Live options readiness.** Broker margin/funds evidence required; structure max-loss and notional
  checks (from B2.5); stale chain/Greeks refusal; fill-gated multi-leg sequencing (reuse B2.2 path);
  protection-owner continuity (B2.4); live approval bound to plan + exposure snapshot + catalog + reservation;
  new strategy version ⇒ new plan and approval.
- **C1.3 Live account scope (after B2 paper gate).** Verify own broker account identity read-only, add to
  `HOSTED_STRATEGY_ACCOUNT_SCOPES`, verify selectors and readiness read-only. **Requires explicit user go-ahead;
  places no orders.**
- **C1 exit requirement:** gated dependent live CNC buys use bounded LIMIT orders before any real account is
  enabled (C1.1 design, decision 1).
- Runbooks per lane: `documents/runbooks/hosted-live-<lane>.md` (enable, verify, halt, repair, rollback).

## Phase C2: sequential production rollout

Lane order: CNC → MIS → futures → options. A lane opens only when its focused paper tests, deployed UI evidence,
runbook, live-readiness checks, and migration/build/deploy checks all pass.

Deploy procedure (each step needs user approval): build all images → recreate `finance-app` → wait for
migration + health → recreate `alerts-worker`, `strategy-runner`, `frontend-next` → verify migration head,
service health, deployed source hashes, auth boundaries, startup logs, and no unintended jobs/orders/
notifications. Record in `documents/hosted-strategies-rollout-<lane>-<date>.md`.

## Known pre-existing issues (not regressions)

1. `options_adjustment` harness expects operator reconciliation; healthy continuation now clears the block
   (`HOSTED_JOB_NOT_BLOCKED`). Fix the expectation during B2.2/B2.3 harness work.
2. `tests/options/test_options_execution_durable_store.py` fixture misses required `tradingsymbol`. Small fix
   inside B2.1.
4. An adjust plan whose process died with a `submitted` event that has no outcome keeps its run `adjusting`: takeover and repair both refuse by name (`OPTION_RUN_ADJUST_IN_FLIGHT` / `adjust_in_flight`). It needs an operator disposition for the unanswered step. That lands with the repair UI in B2.6.
5. `tests/sdk/test_worker_protection_runtime.py`: two structure-seam tests already fail at `ebc98ac`, before this work.
   They expect a default structure-exit seam, but the runtime only gets one from `background.py`. Their
   expectations need updating.
3. One hosted execution API test stalls in the dirty checkout but passes from a clean worktree.

## Progress

| Slice | Status | Commit |
| --- | --- | --- |
| B1 | done, reviewed | `eb2cb00` |
| B2.1a | done, reviewed | `83567dc` |
| B2.1b | done, reviewed | `22766f8` |
| B2.2 design | accepted | `e5664b3` |
| B2.2 S1 compile freeze | done, reviewed | `4c04079` |
| B2.2 S2 adjust engine | done, reviewed | `199bbfb` |
| B2.2 S3 adjust gates | done, reviewed | `8b5cf33` |
| B2.2 S4 roll + takeover | done, reviewed | `59a10d5` |
| B2.2 S5 example + harness | done, reviewed | `847fdb5` |
| B2.4 design | accepted | `4976679` |
| B2.5 risk policy | done, reviewed | `0e02f39` |
| B2.6a options view + repair UI | done, reviewed | `4679956` |
| B2.2 hardening (review findings) | in progress | |
| B2.5 follow-up (roll pricing) | done, reviewed | `6dc3fd3` |
| C1.1 design | accepted | `dbfe46e` |
| B2.4 S1 owner row | done, reviewed | `df5d29d` |
| B2.4 S2a loop + safety gate | done, reviewed | `d32d1d1` |
| B2.6b owner actions | design | |
