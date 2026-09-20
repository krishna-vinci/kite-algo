# Hosted-strategies implementation campaign ledger

Durable, per-phase record for the hosted-strategies implementation campaign in this
repository. Authority order: `documents/hosted-strategies-architecture-r3.md` →
`documents/hosted-strategies-implementation-roadmap.md` → production source/schema →
phase plans (`docs/superpowers/plans/…`) → R1/R2/proposal (history only).

Rules every phase obeys: one Alembic head, next sequential migration after the actual
current head; disposable PostgreSQL for constraint/lock/concurrency tests (never fake
passes on SQLite); unsigned reviewable commits, nothing pushed, no live deployment, no
live migrations, no orders, no real notifications; per-phase parity document under
`documents/hosted-strategies-project<N>-parity.md`.

Use this ledger to hand fresh agents only the context they need — not the whole
conversation.

---

## Phase 0 — Project 0 / G14: live order-mutation ownership hardening

- **Status:** COMPLETE — gate PASSED (2026-09-17)
- **Plan:** `docs/superpowers/plans/2026-09-17-worker-order-ownership-hardening.md`
- **Parity report:** `documents/hosted-strategies-project0-parity.md`
- **Commits (unsigned, on `development`, not pushed):**
  - `e7230588e5dd81788d910265ea1e85e871a0393f` conflict-aware live order ownership lookup
  - `eb99d42dfc44a7fdf892b3586e9b3f265411768a` proven/authoritatively-parented ownership before live cancel
  - `2cbda82a4d92b174bce3ae497bd7ae9e24870361` proven ownership before live modify
  - `3efb25f7b452cad58452ca794fcb6f4689fee746` unit tests: fencing precedence + non-disclosure
  - `ad25837be5c0c34c9c7d99f5f985aa4d1f257d4e` PostgreSQL integration: precedence, conflicts, concurrency
- **Migrations:** none. Alembic head unchanged: **`20260915_000024`** → **next migration number for Phase 1 is `20260917_000025`**.
- **Requirements closed:** all nine roadmap invariants (mutation-gating, strict four-way precedence, all-elements parent proof with caller input as comparison-only, link/intent corruption fail-closed, call-site ordering, non-disclosure, hosted-attempt fencing precedence, live-only, legacy fail-closed). Full matrix in the parity report §1–§2.
- **Test evidence:** 18 focused ownership unit tests; 147/147 in `tests/api/test_algo_worker_api.py`; 522 passed / 20 failed in `tests/api` with the failure set byte-identical to pre-G14 baseline `15335c1` (pre-existing env/config failures); 5/5 PostgreSQL integration tests on disposable DB (skip, never fake-pass, without a URL); `git diff --check` clean.
- **Decisions / deviations:** plan defects D1–D5 found and corrected during implementation (PG fixture DSN export; 500→404 on malformed snapshot; StaticPool fencing fixture; concurrency assertion pinned to the durable-owner invariant; selector typo). Unsigned commits (`--no-gpg-sign`) because the configured GPG key is unavailable — campaign-authorized.
- **Limitations:** legacy unlinked orders fail-closed (no backfill); child-link persistence at placement deferred; conflicts surfaced (409) not repaired; `upsert_order_link` check-then-act race remains (safe-failing, opaque). Live Kite behaviour NOT PROVEN — no live-market certification for this phase (N/A; no execution lane added).
- **Paper/live certification proven:** neither applicable nor attempted.
- **Next-phase input (Phase 1):**
  - Alembic head `20260915_000024`; Phase 1 migration = `20260917_000025` (single head; do not let agents duplicate numbers).
  - G1 plan at `docs/superpowers/plans/2026-09-17-durable-strategy-attribution.md` must be revalidated against current source before execution, with four mandated corrections: use actual schema names (`execution_mode`, `job_kind`); Pydantic request models use `ConfigDict(extra="forbid")`; historical instrument-resolution queries must select every mapping/generation evidence field used to build unresolved-era identities; verify the Alembic head after Project 0 before assigning the migration number (done: `20260915_000024` → `20260917_000025`).
  - Pre-existing unrelated worktree state to preserve untouched: ` M documents/hosted-strategies-architecture-r1.md`, untracked `.commandcode/`, `documents/architecture-flow.md`, `documents/hosted-strategies-architecture-r3.md`, `documents/hosted-strategies-implementation-roadmap.md`, `documents/hosted-strategies-proposal-draft.md`.

---

## Phase 1 — Project 1 / G1: durable strategy identity and attribution

- **Status:** COMPLETE — gate PASSED (2026-09-17)
- **Plan:** `docs/superpowers/plans/2026-09-17-durable-strategy-attribution.md` (revalidated before execution: real schema names `execution_mode`/`job_kind`, `ConfigDict(extra="forbid")` request models, resolution queries select all era-evidence fields, migration number assigned after verifying the post-Project-0 head). Three delegations: Tasks 1–4, 5–7, 8–9; orchestrator reviewed each against the plan before proceeding.
- **Parity report:** `documents/hosted-strategies-project1-parity.md` (full invariant matrix, deviations, limitations)
- **Commits (unsigned, on `development`, not pushed):**
  - `670a5bb` canonical strategies, adapters, grants, immutable bindings, projection schema (+ migration 20260917_000025, schema.sql mirror, ORM models)
  - `5d4def9` deterministic fold domain
  - `84a632a` owned-order resolution, fact sources, versioned canonical resolution, serialized publish
  - `6eca439` full-recompute strategy attribution service
  - `d85bc19` atomic trusted run binding (hosted from persisted job; external from grants; legacy explicit)
  - `b4d074e` owner-controlled canonical strategies with backward-compatible hosted APIs
  - `942a2c7` attribution store/service wired into app state (no scheduler; on-demand publish + owner rebuild)
  - `8da8002` shared lifecycle fake mirrors (honest follow-up commit, outside the plan's file list)
  - `4306ba1` PostgreSQL integration: 29 tests, disposable databases
- **Migrations:** `20260917_000025` (down_revision `20260915_000024`) → **next migration for Phase 2 is `20260917_000026`**.
- **Requirements closed:** one Strategy product with hosted/external adapters; owner-controlled grants (token = credential, exact-account); atomic run+binding creation with hosted-mandatory binding from the persisted job (never payload metadata); insert-only RESTRICT bindings with composite-FK account/owner/environment integrity; full-recompute projection behind lock-before-snapshot (no source-version, no out-of-transaction publication, fails closed); per-fact canonical instrument resolution with catalog-evidence eras (same-era nets across dates, distinct eras never merge, evidence mismatch unresolved); `execution_environment` books incl. normally-empty dry-run; backward-compatible hosted create/status API (`status` vs `product_status`). All PG-proven: triggers, composite-FK refusals, RESTRICT both directions, lock-before-snapshot ordering, concurrent-rebuild serialization, upgrade-from-prior-head id-preserving backfill.
- **Test evidence:** 24 attribution unit; 188+1 `tests/strategies`; 170 binding+worker API; 50 hosted compat; 78 repo/service/lifecycle; **29/29 disposable-PG integration** (1 skipped without URL — never fake-passed); regressions identical to the documented 20-failure env/config baseline; `git diff --check` clean.
- **Decisions / deviations:** async false-pass fix; SQLite portability set (public-schema ATTACH fixture, Text UUIDs, dialect-guarded JSONB, ORM omits run/token FKs — enforcement in migration, PG-proven); `RunBindingFailed` named refusal; wiring in `background.py` + `combined_lifespan` (no factory init site exists); `evidence-mismatch:` era marker; ambiguity branch defensive (schema UNIQUE makes two-instruments-per-window unreachable); PG count 29 vs plan 25 (four items split, superset coverage); 7 stale `kite_own_*` throwaway DBs from G14 fixture failures dropped from the shared test server.
- **Limitations / design consequences for later phases:**
  - **One strategy = one account for life** (binding composite FK + paper-scope rule). Phase 6/7 paper→live certification across differently-shaped accounts needs an explicit product decision; Phase 1 proves promotion as same account / two environments.
  - External-only strategies have no positions route yet (account resolution rides the hosted-adapter mirror); decide by Phase 3/4 owner-UI work.
  - No adjustments table / closure / reconciliation / admission entities — Project 2+. `UNRESOLVED_INSTRUMENT_IDENTITY` surfaced for Project 2/4.
  - Publication on demand only; no background loop.
- **Paper/live certification proven:** paper-book mechanics PG-proven (folds, dedupe, environment separation); paper strategy product end-to-end is Phase 6; live NOT PROVEN (no live market contact, no deployed migration).
- **Next-phase input (Phase 2 = Project 2 / G2+G3+G4):**
  - Next migration `20260917_000026`; single Alembic head; mirror DDL in `backend/schema.sql`.
  - Plan the whole phase first (one delegation, per campaign-owner instruction): strategy closure, account-wide order/fill ingestion, manual/unattributed book, aggregate reconciliation, divergence classification, append-only `strategy_attribution_adjustments`.
  - Phase 2 consumes G1 surfaces: `SqlAttributionStore`/`StrategyAttributionService`, `UNRESOLVED_INSTRUMENT_IDENTITY`, books dimension, `legacy_unattributed` runs (manual-book candidates), the G14 ownership lookup, and `worker_live_execution_links`/`live_order_intents` fact sources.
  - PG fixture pattern to reuse: `tests/integration/test_durable_strategy_attribution_postgres.py` (DATABASE_URL export around `command.upgrade`; per-test create/drop with zero-leftover verification).
  - Pre-existing worktree state to preserve untouched: ` M documents/hosted-strategies-architecture-r1.md`; untracked `.commandcode/`, `documents/architecture-flow.md`, `documents/hosted-strategies-architecture-r3.md`, `documents/hosted-strategies-implementation-roadmap.md`, `documents/hosted-strategies-proposal-draft.md`.

---

## Phase 2 — Project 2 / G2+G3+G4: strategy closure, account truth, manual book, reconciliation

- **Status:** COMPLETE — gate PASSED (2026-09-17)
- **Plan:** `docs/superpowers/plans/2026-09-17-strategy-closure-manual-book-reconciliation.md` (written this phase per the just-in-time method, from R3 §10/§17/§22; self-reviewed; single whole-phase delegation as directed by the campaign owner).
- **Parity report:** `documents/hosted-strategies-project2-parity.md`
- **Commits (unsigned, on `development`, not pushed):**
  - `31dffc8` ingested fill facts, ingest state, reconciliation state, append-only adjustments schema (+ migration 20260917_000026, schema.sql mirror, ORM)
  - `15c9964` strategy-book flatness, exposure and exit sizing for bound runs (G2)
  - `813fefe` account-wide fill ingestion, manual residual, heuristic removal (G3)
  - `ae8bf17` divergence classification, coordinate freeze, owner escalation (G4)
  - `f19e0c1` append-only owner reclassification adjustments folded into strategy books
  - `dd26fd9` PostgreSQL integration (13 tests)
- **Migrations:** `20260917_000026` (down_revision `20260917_000025`, purely additive) → **next migration for Phase 3 is `20260917_000027`**.
- **Requirements closed:** all five roadmap invariants — strategy-book closure (account flatness never substitutes); `Σ attributed + manual = broker` quantity-only invariant as a checked, classified state machine (`aligned`/`pending_ingest`/`unexplained`); immediate coordinate-scoped freeze with risk-reducing exits permitted; owner-only audited reclassification as append-only adjustments (original fills never rewritten); explicit negative-manual-residual exit refusal. Account-wide fill truth (tracked + untracked), manual residual book, unique-candidate heuristic removed, once-only owner escalation via the durable outbox.
- **Test evidence:** 21 account-truth unit; 203 binding/worker/hosted API; 209+1 `tests/strategies`; **13/13 disposable-PG integration** (walkthrough 7 invariant, transitions, freeze symmetry, triggers, composite FK, rebuild-vs-adjustment concurrency, dedupe); skip proven without URL; `tests/journaling` 154 passed + 2 pre-existing `test_journal_filters` failures (proven unrelated by stash comparison); regression sets identical to the documented baseline; zero leftover disposable databases; `git diff --check` clean.
- **Decisions / deviations:** single-writer ingest via `OrdersService.trades()` (order_runtime untouched); freeze as a pure decision function; post-refresh re-read lifts freezes in-check (both branches pinned); escalation recipients = `strategies.owner_id` on the account (no new owner model), failure-independent classification; `create_reclassification` delegates to the attribution store; ORM/new-tables + SQL/old-tables dual-engine pattern; `fact_id` real UUID.
- **Limitations:** corporate-action detection deferred (Project 7 — a split currently classifies `unexplained` and freezes, fail-closed); reconciliation is live-book only; freeze covers worker live-placement paths; 2 pre-existing journaling test failures remain on the known list; escalation message targets strategy owners only (UI surfacing Phase 3+).
- **Paper/live certification proven:** N/A — no new execution lane; real broker/real notifications NOT PROVEN.
- **Next-phase input (Phase 3 = Project 3 / G5: typed proposal envelope, evaluation identity, immutable frozen plans, compiler interface, catalog/universe pinning):**
  - Next migration `20260917_000027`; single head; mirror DDL in `backend/schema.sql`.
  - Plan the whole phase first (one delegation). Read R3 §6 (proposal/evaluation lifecycle), §7 (frozen plan + catalog-generation model), §22 G5, and the roadmap Project 3 section against current source.
  - Reuse: G1 canonical strategies + adapters, binding provenance, `strategies` owner model; the instrument catalog generations (`instrument_catalog_generations`/`instrument_broker_mappings`, `status='published'`, `published_at`) for pinning; ConfigDict(extra="forbid") request schemas; the disposable-PG fixture pattern; `run_id=f"reconciliation:{account_id}"` outbox precedent for non-run records.
  - Known decision to carry: plans immutable and pinned to catalog/universe evidence (fixed architecture decision); unknown evidence fails closed for exposure-increasing work.
  - Pre-existing worktree state to preserve untouched: ` M documents/hosted-strategies-architecture-r1.md`; untracked `.commandcode/`, `documents/architecture-flow.md`, `documents/hosted-strategies-architecture-r3.md`, `documents/hosted-strategies-implementation-roadmap.md`, `documents/hosted-strategies-proposal-draft.md`.

---

## Phase 3 — Project 3 / G5: proposal envelope, evaluation identity, frozen plans, catalog pinning

- **Status:** COMPLETE — gate PASSED (2026-09-17)
- **Plan:** `docs/superpowers/plans/2026-09-17-proposals-frozen-plans.md` (R3 §6/§7 authored; D-1…D-9 binding; single whole-phase delegation).
- **Parity report:** `documents/hosted-strategies-project3-parity.md`
- **Commits (unsigned, on `development`, not pushed):** `f82e920` schema (+ migration 20260917_000027), `816e8b3` pinned-generation catalog read + compiler base + single_instrument, `59a207d` target_weights full-snapshot scope, `c852f16` proposal store (identity/idempotency/conflict/validation), `d4d05b7` worker submission + owner reads + SDK surface, `ccb88e6` PG integration (16 tests), `262fe54` verification fix.
- **Migrations:** `20260917_000027` (down_revision `20260917_000026`, purely additive: `strategy_proposals`, `strategy_plans`, `strategy_proposal_journal`, all insert-only-triggered; proposals/plans composite-FK'd to `strategies (id, account_scope)`) → **next migration for Phase 4 is `20260917_000028`**.
- **Requirements closed:** evaluation identity with exact-retry idempotency and 409 `PROPOSAL_EVALUATION_CONFLICT`; refusal-ends-evaluation; continuous-job multi-evaluation; resolution once against pinned published generation (generation-keyed window read, never the current-only view); immutable frozen plans (hash-stable, one per proposal, insert-only); DERIVED invalidation where an unrelated newer generation leaves plans valid; `target_weights` full-snapshot scope (omission = explicit zero, out-of-scope untouched); fails-closed submission authority via G1 bindings (`AUTHORITY_MISMATCH`); owner read endpoints incl. derived `invalidation_state`; SDK `proposals.submit()`.
- **Test evidence:** 701 passed / 2 skipped across strategies+API+SDK suites; **16/16 disposable-PG integration** (triggers, composite FK, concurrent same-evaluation race, pinned-vs-current read, invalidation states, journal trails); skip proven without URL; regression baseline identical; single head; `git diff --check` clean; zero leftover disposable databases.
- **Defect found at verification (fixed, red-first, PG-proven):** `target_weights` legs missing `broker_exchange` would have read `COORDINATE_UNMAPPED` and invalidated every weights plan on any newer generation — caught by driving weights plans end-to-end through invalidation in Task 7. Lesson recorded: compiler-level tests must also run the derived-invalidation path.
- **Limitations:** `scheduled_occurrence` reserved until the Project 7 calendar; weight→capital math, futures/option compilers, execution and approval all later phases by boundary; nothing consumes `strategy_plans` yet.
- **Paper/live certification proven:** N/A — inert artifacts only; no execution lane, no live contact.
- **Next-phase input (Phase 4 = Project 4 / G9+G10+G6: admission, capital/margin reservations, account-owner approval):**
  - Next migration `20260917_000028`; single head; mirror DDL in `backend/schema.sql`.
  - Plan the whole phase first (one delegation). Read R3 §6 (D2 live-approval paragraph + structural validity), §8 (admission, allocation, margin reservation), §22 G9/G10/G6, and the roadmap Project 4 section against current source.
  - Fixed decisions that bind this phase: owner approval required for live exposure-increasing plans in V1 (paper/dry-run exempt); approval bound to plan hash + account + exposure snapshot/version + reconciliation version + catalog generation + reservation identity + validity window; expiry means NO action; partial fills continue while pinned inputs hold; risk-reducing actions stay available; auto-approval is Project 11/post-V1.
  - Reuse: `strategy_plans` (approval binds plan_hash), G1 exposure snapshot = projection version/content hash, Phase 2 reconciliation state as the aggregate/manual-book version input, outbox notification precedent, disposable-PG fixture, ConfigDict(extra="forbid").
  - Pre-existing worktree state to preserve untouched: ` M documents/hosted-strategies-architecture-r1.md`; untracked `.commandcode/`, `documents/architecture-flow.md`, `documents/hosted-strategies-architecture-r3.md`, `documents/hosted-strategies-implementation-roadmap.md`, `documents/hosted-strategies-proposal-draft.md`.

---

## Phase 4 — Project 4 / G9+G10+G6: admission, reservations, owner approval

- **Status:** COMPLETE — gate PASSED (2026-09-17)
- **Plan:** `docs/superpowers/plans/2026-09-17-admission-reservations-approvals.md` (R3 §6-D2/§8/§22 authored; D-1…D-9 binding; single whole-phase delegation).
- **Parity report:** `documents/hosted-strategies-project4-parity.md`
- **Commits (unsigned, on `development`, not pushed):** `9e27cb3` schema (+ migration 20260917_000028), `f27eacc` admission verdicts, `8c37f10` reservation lifecycle + atomic claims, `3594563` owner approvals + structural validity, `f74d58c` owner surfaces, `8c87c61` PG integration (14 tests), `9296386` verification fix (account authorization on plan-scoped writes).
- **Migrations:** `20260917_000028` (down_revision `20260917_000027`, purely additive: policies, reservations, reservation events (insert-only trigger), approvals (partial unique one-active-per-plan), reconciliation versions) → **next migration for Phase 5 is `20260917_000029`**.
- **Requirements closed:** all eight R3 §8 admission controls as ordered named refusals (allocation/notional/gross/open-instruments/rate/daily-loss/catalog/session-product/margin-freshness); atomic capacity claim with the load-bearing-lock negative test; full reservation lifecycle verbatim (consumed never releases on expiry; `DISPOSITION_UNPROVEN` disposition; owner cannot steal active capacity); owner-only approvals binding all D2 pins with observable all-mismatches validity; unrelated catalog change keeps validity; margin max-age; preview ≠ reservation; paper/dry-run approval-exempt; reconciliation version counter monotonic under concurrency.
- **Test evidence:** 25+16+22 new unit tests; **777 passed / 2 skipped** strategies+API+SDK; **14/14 disposable-PG integration** (capacity race with lock-removal negative test, double-approval race, pin matrix, counter monotonicity, upgrade-from-prior-head); skip proven without URL; regression baseline identical; single head; `git diff --check` clean.
- **Defects found and fixed:** Task 7 scope audit caught missing account authorization on plan-scoped writes (real security gap, red-first fix); two PG-only defects (Phase 3 plan-insert ordering; `upsert_reconciliation` read-then-insert race → SAVEPOINT-safe); `REFERENCE_PRICE_UNAVAILABLE` added so unknown prices refuse instead of silently under-enforcing notional limits.
- **Limitations:** no execution path consumes verdicts yet (Project 6 wires paper); live daily-loss honestly refuses until a realized-loss source exists (Project 5+); "execution started" approval semantics pinned as pure functions pending wiring; `action_required` disposition refuses until execution state exists.
- **Paper/live certification proven:** N/A — no execution lane; real broker margin quotes and deployment NOT PROVEN.
- **Next-phase input (Phase 5 = Project 5 / G7: durable execution-quiescence barrier and four-axis settlement evidence):**
  - Next migration `20260917_000029`; single head; mirror DDL in `backend/schema.sql`.
  - Plan the whole phase first (one delegation). Read R3 §16 (expiry and settlement policy — the settlement levels), the settlement axis definitions across R3 (durable quiescence, strategy-scoped flatness, terminal domain state, expired/revoked evaluation authority), §22 G7, and the roadmap Project 5 section against current source.
  - Fixed decisions that bind this phase: settlement requires durable quiescence + strategy-scoped flatness (never account flatness) + terminal domain state + expired/revoked evaluation authority; the Phase 4 approval/validity machinery and the G7 barrier feed Phase 6's paper end-to-end.
  - Reuse: G1 projection/open_positions (flatness axis), Phase 2 reconciliation + `has_unresolved_worker_execution`, the execution-link machinery (`worker_live_execution_links`, `has_unresolved_execution_for_run`), Phase 4 reservation lifecycle (release-on-settlement), notification outbox, disposable-PG fixture.
  - Pre-existing worktree state to preserve untouched: ` M documents/hosted-strategies-architecture-r1.md`; untracked `.commandcode/`, `documents/architecture-flow.md`, `documents/hosted-strategies-architecture-r3.md`, `documents/hosted-strategies-implementation-roadmap.md`, `documents/hosted-strategies-proposal-draft.md`.
---

## Phase 5 — Project 5 / G7: durable execution-quiescence barrier and settlement evidence

- **Status:** COMPLETE — gate PASSED (2026-09-17). First phase executed by an orchestrator-launched subagent (campaign-owner instruction).
- **Plan:** `docs/superpowers/plans/2026-09-17-settlement-barrier.md` (R3 §16/§22; D-1…D-5 binding).
- **Parity report:** `documents/hosted-strategies-project5-parity.md`
- **Commits (unsigned):** `e88c3a8` schema (+ migration 20260917_000029), `ea408be` barrier, `3c5ee2f` four axes, `1da251a` reconciliation integration, `07748e2` owner surfaces, `95c85cd` PG integration (16 tests), `ff4209e` residual cleanup, `7e64c82` monotonic head assertions across all PG suites (orchestrator closure fix).
- **Migrations:** `20260917_000029` → **next migration for Phase 6 is `20260917_000030`**.
- **Requirements closed:** quiescence as a durable proof (version + empty in-flight under the book lock; never inferred); fail-closed in-flight enumeration; four axes with satisfied/failed/unknown and unknown-never-releases; strategy-scoped flatness; append-only assessments with barrier_version + digests and derived staleness; reconciliation `quiescence_state` barrier-backed; owner settlement read/assess endpoints.
- **Test evidence:** 66 settlement unit tests; 152 passed targeted sweep; **16/16 settlement PG**; all six prior PG suites re-green after the head-pin fix (ownership 5, attribution 29, truth 13, proposals 16, admission 14, settlement 16); API baseline unchanged.
- **Decisions:** work events also take the book lock; reconciliation flows through `reconciliation_service.py`; rollup failed-before-unknown; plans' terminality via reservation. **Process fix:** exact alembic head pins replaced with monotonic assertions permanently (universe-suite precedent).
- **Limitations:** domain-adapter registry empty (CNC/MIS/futures/options interpret in P6/P8/P9/P10); settlement evidence not yet consumed by any release action; live broker snapshots NOT PROVEN.
- **Next-phase input (Phase 6 = Project 6: paper single-instrument proposal execution end-to-end, Foundation B completion):** next migration `20260917_000030`. Wire the FULL chain on paper: proposal (Phase 3) → admission+reservation (Phase 4, paper evidence) → approval-exempt (paper) → plan execution against the PAPER runtime → attributed fills (G1 paper book) → reconciliation (Phase 2) → settlement assessment (Phase 5) with the CNC/single-instrument domain adapter. Execution consumes admission verdicts and approval validity for the FIRST time — the boundary that was deliberately left open in Phase 4. Preserve worktree state as always.

---

## Phase 6 — Project 6: paper single-instrument proposals end-to-end (Foundation B)

- **Status:** COMPLETE — gate PASSED (2026-09-17). Executed via two subagent delegations (first interrupted mid-Task-2; resumed from exact state `a03ad89` + inherited test file).
- **Plan:** `docs/superpowers/plans/2026-09-17-paper-execution.md` (D-1…D-7 binding).
- **Parity report:** `documents/hosted-strategies-project6-parity.md`
- **Commits (unsigned):** `a03ad89` schema (+ migration 20260917_000030), `51377bc` paper executor, `ad7394b` intent bundles, `a7c8442` owner surfaces, `66359d9` PG integration (12 tests).
- **Requirements closed:** the full chain exercised for the first time — proposal → frozen plan → paper admission/reservation → execution through the existing paper runtime with bound-run attribution → G1-attributed paper book owner-visible → reservation consumed/released → barrier + settlement assessment. Named-refusal precondition chain; append-only execution events; exactly-one-submission; intent bundles per-leg; paper-only (`PAPER_ONLY_EXECUTION`).
- **Test evidence:** 30 execution unit tests; 453+1 strategies+owner-API sweeps; **12/12 PG incl. the end-to-end acceptance**; API baseline unchanged; single head; clean.
- **Decisions:** inherited-test arithmetic corrections (documented); schema-impossible test replaced; `PLAN_ALREADY_EXECUTED` keyed on committed submission.
- **Limitations:** paper-only; no target_weights execution, no partial fills, no scheduler (all P7); owner-triggered execution; live enablement gated on separate authorization.
- **Next-phase input (Phase 7 = Project 7: CNC target-weight portfolios, monthly/calendar scheduler, paper partial fills, corporate-action detection/freeze/escalation):** next migration `20260917_000031`. Roadmap: CNC lane on paper then certified before live; scheduler kinds monthly/calendar with misfire/overlap policy (G11 — schedule table stored-only at backend/strategies/models.py:115-181); paper partial fills (G12); corporate-action detection first-delivery = detect + freeze + account-owner escalation (G13; R3 §18: splits allocate across pre-action attributed quantity, adjustments model exists from Phase 2/4). Preserve worktree state as always.
---

## Phase 7 — Project 7: CNC portfolios, scheduling, paper partial fills, corporate actions (G11+G12+G13)

- **Status:** COMPLETE — gate PASSED (2026-09-17). Subagent delegation; prompt issued via campaign owner after an orchestrator process miss (plan file written only after the agent flagged its absence — plan-file-before-prompt ordering now strict).
- **Plan:** `docs/superpowers/plans/2026-09-17-cnc-portfolios-scheduling.md`
- **Parity report:** `documents/hosted-strategies-project7-parity.md`
- **Commits (unsigned):** `ef5ff04` schema (+ migration 20260917_000031, incl. two CHECK widenings), `9739e70` weights compiler, `ae03bab` scheduler, `909f509` paper partial fills, `d2f35f6` corporate actions, `9285459` PG integration (21 tests).
- **Requirements closed:** target_weights portfolio execution with full-snapshot semantics (omission = sell to zero), sells-before-buys with gross cash reservation, named fit refusals; monthly/calendar scheduler with occurrence fencing, misfire grace, overlap skip, per-occurrence evaluations reusing the same book; paper partial fills (opt-in ratio, default instant-full) with verified progress + reservation renewal + barrier honesty; corporate-action detection → freeze → single owner escalation → owner-adjustment-only resolution.
- **Test evidence:** 65 new unit tests; 518+1 sweep; **21/21 PG** (walkthrough, partial-fill certification at mechanism level, corp-action divergence, scheduler concurrency); API baseline unchanged; single head; clean.
- **Decisions:** CHECK widenings (superset, downgrade restores); partial fills opt-in (default 1.0 — preserves Phase 6 certification); detection in reconcile_account; `SUSPECTED_CORPORATE_ACTION` as event reason freezing as unexplained; fail-closed `_open_remainders`; tranche ceil-min-1.
- **Carried limitation:** partial-fill rebalance not yet certified as a full multi-tick execute-and-watch run (needs the paper tick scheduler wiring) — **prerequisite for Phase 8 certification and any future live CNC enablement**.
- **Next-phase input (Phase 8 = Project 8: MIS intraday policy, per-product square-off, stale-worker exit integration):** next migration `20260917_000032`. Read roadmap Project 8 + R3 §12 (MIS correction walkthrough), §16 (platform-owned square-off schedules — NSE:MIS 15:20, BSE:MIS 15:20, NFO:MIS 15:25, CDS:MIS 16:45, MCX:MIS 23:20, overridable via WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON, already implemented in backend/app/background.py:20-38), §22 G-lane items. Square-off is MIS-specific and platform-owned; partial-fill tick certification rides along. Preserve worktree state as always.
---

## Phase 8 — Project 8: MIS intraday policy, square-off wiring, stale-worker exit

- **Status:** COMPLETE — gate PASSED (2026-09-17). Relay delegation (campaign owner relayed the prompt).
- **Plan:** `docs/superpowers/plans/2026-09-17-mis-intraday-policy.md`
- **Parity report:** `documents/hosted-strategies-project8-parity.md`
- **Commits (unsigned):** `7fb084a` schema (+ migration 20260917_000032), `89c52ad` intraday validation, `8033f80` square-off wiring + evidence, `f92ec0c` stale-exit attachment, `5196850` partial-fill tick certification, `7e1febd` PG integration (13 tests), `e307d49` residuals.
- **Requirements closed:** multi-day MIS refused `MIS_OVERNIGHT_REFUSED` at validation (evaluation-ending, alternatives named); platform-owned per-product square-off through the durable claim path with attributed-quantity clamps and full evidence; failed square-off = action_required + keeps reconciling (never settlement); broker square-off = recorded fallback; `exit_on_worker_stale` attached at policy level through the claim path; **Phase 7's carried partial-fill tick certification closed**; walkthrough-2 product-book isolation proven; protection runtime zero-diff.
- **Test evidence:** 39+ new unit tests; 569+1 sweep; **13/13 PG** (walkthrough 2, square-off failure, overnight refusal, schedules+override, tick certification); API baseline unchanged; single head; clean.
- **Limitations:** synthetic tick source in the certification test (stated); live MIS closed pending separate authorization; overnight MIS/SLB permanent non-goals.
- **Next-phase input (Phase 9 = Project 9: futures compiler, contract/lot resolution, margin admission, full-fill-gated rolls):** next migration `20260917_000033`. Read roadmap Project 9 + R3 §13 (futures and roll flow — roll is a parent operation; full-required-fill gating; D-decisions on roll release) + §22. Rolls: full-required-fill invariant (NOT basket all_or_none), contract/lot resolution via pinned catalog, margin admission via Phase 4. Preserve worktree state as always.
---

## Phase 9 — Project 9: futures contracts, full-fill-gated rolls, margin precheck

- **Status:** COMPLETE — gate PASSED (2026-09-17). Relay delegation.
- **Plan:** `docs/superpowers/plans/2026-09-17-futures-rolls.md`
- **Parity report:** `documents/hosted-strategies-project9-parity.md`
- **Commits (unsigned):** `67f3a47` schema (+ migration 20260917_000033), `609e72b` target_futures compiler, `f0becc8` roll state machine, `ed24bbf` margin precheck + expiry escalation + owner surfaces, `c42a90b` PG integration (13 tests).
- **Requirements closed:** the roll invariant verbatim (acquire → prove-filled → release-close; old attribution until flat; both identities; stalled ⇒ action_required, never auto-reverse; NOT the basket flag — negative test + AST audit); pinned-generation futures contract resolution with lot semantics; peak-margin admission precheck with evidence; expiry-cutoff escalation; owner roll reads.
- **Test evidence:** 46+ new unit tests; 617+1 sweep; **13/13 PG**; API baseline unchanged; single head; clean.
- **Deviation:** freeze-quantity metadata does not exist in the catalog schema — limit from declared intent with `freeze_source: unavailable` visibility (the one plan-vs-source contradiction; catalog work item for the future).
- **Limitations:** paper-only; live margin quotes future wiring; proportional release deliberately absent.
- **Next-phase input (Phase 10 = Project 10 / G8, FINAL PHASE: option structures compiled into the existing option-run engine):** next migration `20260917_000034`. Read roadmap Project 10 + R3 §14 (walkthroughs 4–6; Appendices A/B), R3 §16 (expiry policy: cash-settlement evidence, exit_before_cutoff, allow_physical_settlement, settled terminal state on the Phase 5 barrier). The existing options engine is the execution engine (backend/options/** — protection runtime, exit builder, strategy compiler, execution planner); structure-aware enforced exits, hedge fill gating, expiry policy, settled state. Certify paper before removing the hosted live-options block. Preserve worktree state as always.
---

## Phase 10 — Project 10 / G8: option structures, fill gating, expiry, settled — FINAL PHASE

- **Status:** COMPLETE — gate PASSED (2026-09-18). CAMPAIGN COMPLETE (Phases 0–10; Project 11 excluded per authorization).
- **Plan:** `docs/superpowers/plans/2026-09-17-options-structures.md`
- **Parity report:** `documents/hosted-strategies-project10-parity.md`
- **Commits (unsigned):** `a572de1` schema (+ migration 20260917_000034; the planned CHECK widening was a no-op — settled is the enum + adapter), `f4e574e` option_structure compiler, `89fd213` vocabulary bridge, `daee4f6` hedge fill gating, `3637a97` expiry policy + settled + evidence-gated settlement, `669d3d5` owner surfaces, `03ea1c8` PG integration (15 tests), `268538d` residuals, `170db27` fixture repair (caught by baseline count dropping), `ebcdfe1` gap-C wiring into the enforced runtime, `ef0d65a` domain-registry leak fix.
- **Requirements closed:** chain threading through pinned resolution into the one options engine; the vocabulary bridge; submitted ≠ filled everywhere (hedge gate with proportional ceiling and timeout); short-first exit builder with closure proof; the evaluator's recommended exits ACTUALLY submitted through the durable claim path (verified gap C closed, non-structure regression pinned, fail-closed seam evidence); expiry policy frozen per structure with evidence-gated settlement (structural refusal without evidence); settled on the Phase 5 barrier; MIS options via the platform schedule; walkthroughs 4–6 proven.
- **Test evidence:** 80+ new unit tests; 3× byte-identical stable sweeps; **15/15 PG**; tests/api and tests/options baselines unchanged; single head `20260917_000034`; clean.
- **Notable process catches:** baseline count DROPPING exposed a fixture SyntaxError masquerading as green; the flaky settlement test's real cause was a registry leak (orchestrator diagnosis corrected by the agent with reproduction).
- **Final state of the campaign:** heads `20260915_000024` → `20260917_000034` (11 migrations, single head). All ten phases gated PASSED. Every lane paper-certified; every live path refuses in code pending the owner's separate deployment + live-market certification. Nothing pushed at any point.