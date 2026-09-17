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
