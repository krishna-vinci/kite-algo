# Hosted strategies — Project 3 parity matrix (proposal envelope, frozen plans, catalog pinning — G5)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 3](hosted-strategies-implementation-roadmap.md):
typed proposal envelope, evaluation identity/idempotency, validation, compiler
interface, immutable frozen plans, catalog/universe pinning. Plan:
`docs/superpowers/plans/2026-09-17-proposals-frozen-plans.md` (R3 §6/§7 authored;
design decisions D-1…D-9 binding).

> **Read this first.** **Closed** = implemented + covered by an executed test in
> this repo (database/classification claims proven on real disposable PostgreSQL).
> **NOT PROVEN** = requires separate authorization/infrastructure.

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Migration `20260917_000027` (single head, after `000026`; purely additive) | Closed — `strategy_proposals` (`UNIQUE (strategy_id, evaluation_id)`, immutable payload + sha256, insert-only trigger, composite FK `(strategy_id, account_id) → strategies (id, account_scope)`), `strategy_plans` (`UNIQUE (proposal_id)`, plan hash, logical + resolved JSONB, pin scope, `CHECK` requiring revision+member-hash for `target_weights`), `strategy_proposal_journal` (append-only trail); mirrored in `backend/schema.sql` |
| Evaluation identity and cardinality (R3 §6) | Closed — exact retry idempotent (`idempotent_retry` journalled); different payload under the same identity → 409 `PROPOSAL_EVALUATION_CONFLICT`; validation refusal ends the evaluation (identity spent); continuous jobs hold many evaluations (test-pinned) |
| Compiler interface + reference compilers | Closed — `backend/strategies/compiler/`: registry (`TARGET_KIND_UNKNOWN`), `single_instrument` (shares; `INSTRUMENT_UNRESOLVED`), `target_weights` (full-snapshot); futures/options compilers deferred to Projects 9/10 (non-goals) |
| Pinned-generation catalog read (R3 §7) | Closed — generation-parameterized window read alongside the current-only view; unpublished generation refused (`CATALOG_GENERATION_NOT_PUBLISHED`); absent pin defaults to current published |
| Immutable frozen plans | Closed — plan hash = sha256 over canonical logical+resolved+pin (stable/discriminating, test-pinned); insert-only; exactly one plan per proposal |
| Derived invalidation (unrelated change never invalidates) | Closed — `plan_invalidation_state` derived, never stored; same generation → valid; newer generation re-mapping a pinned instrument or retiring its record → invalidated; unrelated newer generation → valid (PG-proven) |
| Full-snapshot scope semantics | Closed — scope = (universe revision id, member hash, catalog generation); omission inside scope = explicit zero row; out-of-scope instruments untouched (no row); weights resolve via the pinned generation |
| Submission authority (fails closed) | Closed — worker token → run → G1 binding must match payload `strategy_id` + `account_scope` (`AUTHORITY_MISMATCH` 403); unbound run refused; owner endpoints read-only and worker-token-proof |
| Surfaces | Closed — `backend/api/routers/worker_proposals.py` (worker submission), owner reads in `strategies.py` (plan read includes derived `invalidation_state`), SDK `proposals.submit()` thin transport on client + async client |

Commits (unsigned, on `development`, not pushed): `f82e920` (schema), `816e8b3` (pinned read + compiler base), `59a207d` (target_weights), `c852f16` (proposal store), `d4d05b7` (API + SDK), `ccb88e6` (PG integration), `262fe54` (verification fix).

## 1. Test evidence (executed 2026-09-17, this machine; counts independently re-run by the orchestrator)

| Suite | Result |
| --- | --- |
| `tests/strategies` + binding/worker/hosted-router + `tests/sdk` | **701 passed, 2 skipped** |
| **PostgreSQL:** `PROPOSALS_PG_URL=… pytest tests/integration/test_proposals_plans_postgres.py -q` | **16 passed** — migration head + upgrade-from-prior-head; insert-only triggers (all three tables); composite-FK account refusal; **concurrent same-evaluation race** (exactly one envelope; loser idempotent-or-conflict, never duplicate); pinned read vs current view across two generations; invalidation states (valid / re-mapped / retired); journal trails. Without a URL: **1 skipped — never fake-passed** |
| `tests/api` vs documented baseline | identical 20-failure set — no regressions |
| `git diff --check` | clean; single head `20260917_000027`; zero leftover disposable databases |

## 2. Defect found and fixed at verification (the phase's most important finding)

**`target_weights` legs lacked `broker_exchange`** — derived invalidation resolves each leg's full broker coordinate against the newest generation, so an exchange-less leg could never be compared and read as `COORDINATE_UNMAPPED` — which would have invalidated **every** weights-based plan the moment any newer catalog generation published: precisely the failure R3 §7 forbids ("an unrelated catalog update must not invalidate a plan"). It survived six tasks because all invalidation tests used `single_instrument` plans (both fields present) and the compiler-level weights tests never drove a plan through invalidation. Fixed red-first and now proven end-to-end on PostgreSQL: scope columns persisted against the real CHECK, omission-as-explicit-zero, unrelated-generation-stays-valid, re-mapped-member-invalidates.

## 3. Decisions and deviations

- No structural deviations from the plan. `scheduled_occurrence` is schema-reserved (CHECK requires `job_id`) and unexercisable until Project 7's calendar exists — by design.
- `strategy_run_id` is required provenance on submissions (binding authority), `job_id` optional — matches D-8 as written.

## 4. Known limitations and deferrals

- No execution, admission, reservation or approval entities (Projects 4/6+); nothing consumes `strategy_plans` yet (scope-audit pinned).
- Weight→capital quantity math is Project 4; futures compilers Project 9; option structures Project 10.
- `scheduled_occurrence` evaluations await the Project 7 calendar.
- Only two reference compilers exist by design; the interface is the deliverable.

## 5. NOT PROVEN (requires separate authorization)

- Real Kite catalog behaviour at scale (real generation publication cadence); deployment/migration against any real database. No live-market contact this phase.

## 6. Gate summary

All roadmap Project 3 acceptance evidence is Closed with executed tests, including
real-PostgreSQL proof of immutability, the idempotency race, pinning and derived
invalidation. **Paper/live certification:** N/A this phase (no execution lane).
Phase gate: **PASSED**.
