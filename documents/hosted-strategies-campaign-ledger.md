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

<!-- Append one section per phase below. Never rewrite prior sections; add a
     follow-up note inside the phase section instead. -->
