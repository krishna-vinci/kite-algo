# Hosted strategies — Project 1 parity matrix (durable strategy attribution, G1)

Status as of 2026-09-17, branch `development`. Scope per
[implementation roadmap, Project 1](hosted-strategies-implementation-roadmap.md):
canonical strategy identity, hosted/external compute adapters, owner-controlled
grants, atomic immutable run bindings, full-recompute strategy-position projection,
historical canonical instrument resolution. Detailed plan:
`docs/superpowers/plans/2026-09-17-durable-strategy-attribution.md` (executed after a
revalidation pass; the plan text was treated as frozen during execution).

> **Read this first.** Status labels: **Closed** (implemented + covered by an
> executed test in this repo), **Verified locally** (executed here, command shown),
> **NOT PROVEN** (requires separate authorization/infrastructure). "Closed" means
> the *mechanism* is proven by automated tests — most database-level claims are
> proven on real disposable PostgreSQL, not SQLite.

## 0. Baseline and what this phase delivered

| Item | State |
| --- | --- |
| Migration `20260917_000025` (one head, after `20260915_000024`) | Closed — canonical `strategies` (+hosted backfill preserving ids), `external_strategy_adapters`, `worker_token_strategy_grants`, `strategy_run_bindings` (insert-only trigger, RESTRICT both directions), `strategy_position_projection`, `strategy_projection_state`; mirrored in `backend/schema.sql` |
| One Strategy product, two compute adapters | Closed — `strategies` is the user-facing product; hosted adapter (existing `hosted_strategies`, composite-FK-pinned) and `external_strategy_adapters` |
| Owner-controlled external authority; token = credential | Closed — `worker_token_strategy_grants` (revocable, exact-account join); grant issuance verifies ownership/account/token |
| Atomic trusted run binding | Closed — hosted binds from the **persisted job** (never payload metadata; `HOSTED_RUN_BINDING_FAILED` 503 if absent); external binds only from an active grant (`STRATEGY_NOT_GRANTED` 403 before any run row; zero grants → explicit `legacy_unattributed`); `create_run_with_binding` writes run+binding in one transaction |
| Database-enforced integrity | Closed — composite FKs (`(id, owner_id, account_scope)`, `(id, account_scope)` targets; binding and projection/state account agreement; hosted adapter drift impossible), insert-only trigger, ON DELETE RESTRICT both directions — all proven on real PG |
| Full-recompute projection, lock-before-snapshot | Closed — `recompute_publish`: advisory lock → all reads on the locked session → delete+replace → version advance → commit; content_sha256 idempotence only; **no** source-version, **no** out-of-transaction publication (fails closed) |
| Per-fact canonical instrument resolution | Closed — versioned mapping windows (`valid_from_generation`/`valid_to_generation` × `published_at`) per fact's own effective time; exchange/symbol evidence check; catalog-evidence eras (`era=interval:`, `era=gen:`, `era=pre-catalog`, `evidence-mismatch:` marker); same-era nets across dates, distinct eras never merge |
| Books dimension | Closed — `execution_environment ∈ {live, paper, dry_run}` on facts, keys, PKs, lock key; dry-run book normally empty (previews only, no manufactured fills) |
| Backward-compatible hosted API | Closed — `POST /api/strategies` legacy payload unchanged (`kind` optional; `ConfigDict(extra="forbid")` everywhere); `PATCH /{id}` = hosted scheduling (`status`), `PATCH /{id}/status` = canonical product state (`product_status`); pre-existing hosted router suites green |

Commits (unsigned, on `development`, not pushed):

| Commit | Subject |
| --- | --- |
| `670a5bb` | feat(attribution): canonical strategies, adapters, grants, immutable bindings, projection schema |
| `5d4def9` | feat(attribution): deterministic fold domain |
| `84a632a` | feat(attribution): owned-order resolution, fact sources, versioned canonical resolution, serialized publish |
| `6eca439` | feat(attribution): full-recompute strategy attribution service |
| `d85bc19` | feat(attribution): atomic trusted run binding for hosted and external runs |
| `b4d074e` | feat(attribution): owner-controlled canonical strategies with backward-compatible hosted APIs |
| `942a2c7` | feat(attribution): wire attribution store/service into app state |
| `8da8002` | test(attribution): mirror create_run_with_binding/active_grants in the shared lifecycle fake |
| `4306ba1` | test(attribution): postgres integration — durability, concurrency, determinism, instrument identity |

## 1. Test evidence (executed 2026-09-17, this machine; counts independently re-run by the orchestrator)

| Suite | Result |
| --- | --- |
| `tests/strategies/test_attribution_service.py` | 24 passed (fold 8, store 9, service 7) |
| `pytest tests/strategies -q` | 188 passed, 1 skipped |
| `tests/api/test_strategy_owner_and_binding.py` + `tests/api/test_algo_worker_api.py` | 170 passed |
| Hosted compat proof (`test_strategies_api`, `test_reconciliation_api`, `test_operator_controls`) | 50 passed |
| `tests/strategies/test_repository.py test_service.py test_lifecycle_prepare.py` | 78 passed |
| `pytest tests/api tests/strategies -q` vs baseline | identical set to the documented 20 pre-existing env/config failures — no regressions |
| **PostgreSQL:** `ATTRIBUTION_PG_URL=… pytest tests/integration/test_durable_strategy_attribution_postgres.py -q` | **29 passed** (superset of the plan's 25; four items split into independent tests). Without a URL: **1 skipped, never fake-passed** |

The PG suite proves on real PostgreSQL: migration head + shape; upgrade-from-prior-head with id-preserving backfill; binding immutability trigger; binding account/environment composite-FK refusals; hosted-adapter drift refusal; projection account integrity (plan 3b); RESTRICT both directions; locked-session reads; interrupted publish retaining previous version; atomic run+binding rollback; lock-before-snapshot (older recompute cannot overwrite newer); two concurrent rebuilds serialize; concurrent rebuild vs late fill; per-fact identity across eras; 16b same-era netting across dates with distinct eras separate; 16c evidence mismatch unresolved; dry-run book empty; paper↔live promotion imports nothing; late-fill/dedupe/intent-fallback/conflict; hosted identity from job not metadata; grants enforcement; legacy hosted API compatibility.

## 2. Decisions and deviations (all reviewed and accepted by the orchestrator)

- **Async false-pass fix (Tasks 1–4):** plan's service tests were `async def` on `unittest.TestCase` (coroutines never awaited — a verified false pass). Synced signatures.
- **SQLite portability:** `public.`-schema ATTACH fixture pattern; ORM omits FKs to `algo_worker_runs`/`algo_worker_tokens` (no ORM models exist — enforcement lives in the migration/schema.sql and is PG-proven); UUID ids as `Text` in ORM; JSONB binding serialization dialect-guarded (PG keeps `CAST(:x AS JSONB)`).
- **`RunBindingFailed`:** the run insert precedes the binding insert; without the named failure a PG composite-FK refusal would surface as a misleading 409 "run already exists".
- **`ensure_attribution_state` wiring:** no app factory initializes the worker repo (single site inside the protection loop); wiring added in `backend/app/background.py` + `combined_lifespan` so it does not depend on `WORKER_PROTECTION_ENABLED`. No scheduler added — publication is on demand + owner rebuild.
- **`evidence-mismatch:` era marker:** `PositionKey` is fixed at four fields, so the unresolved reason for mismatched evidence is encoded in the era identity; distinct anomalous evidence classes never silently merge.
- **Mapping-ambiguity branch is defensive:** `instrument_broker_mappings` is `UNIQUE (broker, broker_token, valid_from_generation)`, so two instruments for one token inside one window are unreachable; the 16b test exercises the reachable era mechanism (`era=gen:<id>` across distinct generations). The branch remains as documented defense.
- **Task 8 count 29 vs plan 25:** four plan items split into independent tests (migration shape vs upgrade backfill; hosted-adapter drift; mapping_missing vs pre-catalog). Coverage is a superset.
- **Test-server cleanup:** 7 stale `kite_own_*` throwaway databases left by earlier G14 fixture failures were dropped; verified zero leftovers after this suite (its fixture drops on all paths).

## 3. Known limitations and design consequences

- **One strategy is pinned to one account for life.** The binding composite FK `(strategy_id, owner_id, account_id)` plus the paper-scope rule mean a canonical strategy cannot span a paper-shaped and a live-shaped account. Books separate on `execution_environment` within the account. **Consequence for Phase 6/7 ("certify paper before enabling live"):** promoting one canonical strategy across differently-shaped accounts is blocked by design and needs an explicit product decision (clone/alias or policy change) when that phase is planned. The Phase 1 test expresses promotion as the same account, two environments — the dimension books actually separate on.
- **External-only strategies have no positions route yet:** owner account resolution rides the hosted adapter's compatibility mirror; a strategy with no hosted adapter cannot resolve it. Composite FK guarantees the mirror cannot drift; the owner-UI gap is deferred to Phase 3/4 planning.
- No `strategy_attribution_adjustments` table, no closure/reconciliation/admission entities — explicitly Project 2+ (scope-exclusion audit passed). `UNRESOLVED_INSTRUMENT_IDENTITY` is surfaced for Project 2/4 to consume as freeze/refusal input; G1 builds no freeze service.
- Publication is on demand; no background recompute loop exists.
- Grant issuance reads tokens via list-and-find (fine at current scale).

## 4. NOT PROVEN (requires separate authorization)

- Behaviour against the real Kite API / live market: no live orders, no live data. Live-market certification is a separate campaign step.
- Deployment of the migration to any real database: all migration execution was against per-test disposable databases, created and dropped by the fixture.

## 5. Gate summary

All roadmap Project 1 requirements are Closed with executed tests, including
real-PostgreSQL proof of every constraint, trigger, lock-ordering and concurrency
invariant. **Paper certification:** the paper book mechanics (folds, dedupe,
environment separation) are PG-proven; the paper strategy *product* end-to-end is
Phase 6. **Live certification:** not applicable/attempted this phase. Phase gate:
**PASSED**.
