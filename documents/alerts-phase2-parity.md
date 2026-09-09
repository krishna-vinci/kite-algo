# Alerts catalog / Phase 1.5 / Phase 2 parity matrix

Status against the handoff findings (C1–C7), the Phase 1.5 hardening list,
and the v2 spec Phase 2 features (F7/F8). Evidence paths are relative to the
repository root; commands assume `.venv/bin/python -m pytest`.

## Catalog corrections (C1–C7)

| Finding | Status | Implementation | Evidence | Remaining limitation |
| --- | --- | --- | --- | --- |
| C1 one coherent binding state | Closed | `backend/workflows/instrument_bindings.py` registry owns all bindings; runtime reads snapshots in factories/history/renewal/health; added/changed/removed bindings rebuild or release sources with fresh epochs; zero-token start works (renewal always built) | `tests/workflows/test_binding_lifecycle.py` (production wiring: factories, renewal snapshot, history provider, retirement stop) | Token replacement keeps candle replay boundary semantics (boundary ignored on replacement by design) |
| C2 fallback vs authoritative rejection | Closed | `worker_entry.resolve_catalog_instrument_tokens` returns (resolved, rejected); default fallback only while catalog uninitialized; retired/expired/ambiguous never bypassed; `ALERTS_INSTRUMENT_TOKEN_FALLBACK=always` explicit compat; catalog unavailability raises and the worker keeps current bindings | `tests/workflows/test_instrument_catalog_resolution.py` (9 cases incl. retired-hidden-from-view, always-policy, unavailable raises) | Legacy deployments relying on silent fallback must set the env flag (documented in ops guide) |
| C3 publication generation coherence | Closed | `catalog.py` complete-publication model: every publish moves all non-retired records to the new generation; `exchange_sources` keeps original observation generation/time; advisory xact lock serializes; `health()` reports last usable publication + `latest_attempt`; orchestrator records Go reload ack with `matches_publication` | `tests/broker_api/test_instrument_catalog_publish.py`; real-PG: `tests/integration/test_catalog_publication_postgres.py` (scoped refresh → 1 generation; failed refresh displaces nothing; concurrent publishes serialize); live evidence below | Go store stale between publish and reload is only detected at the next refresh call (best-effort HTTP) |
| C4 incomplete refresh guard | Closed | completeness floor `max(minimum_count, ceil(prev*coverage_floor_ratio))` with `force_exchanges` override; wrong-scope rows, non-finite numerics, unparseable expiries are typed `RefreshValidationError`s before publication | publish tests + real-PG truncated test (1-row payload retains 6 valid instruments) | Ratio default 0.5 is a policy default, not per-source calibrated |
| C5 identity vs enrichment | Closed | `identity_key` uses immutable attributes only (exchange, symbol, type, expiry, strike, option type) with strike canonicalization (0/NULL equal, rounding); segment/underlying/name are mutable metadata | `tests/broker_api/test_instrument_catalog_publish.py` C5 tests (enrichment correction, inferred-underlying change, 0-vs-NULL strike, real contract change, rounding) | Old identity keys (pre-fix rows) would need a re-import to re-key; fresh deployments unaffected |
| C6 provenance describes the binding used | Closed | subscriptions persist `instrument_binding` at creation AND backfill/refresh on later materializations; events copy the binding into `evidence.instrument_binding`; `universe_revision` recorded for membership events | `tests/workflows/test_binding_provenance.py` | Historical events from before the binding was first recorded have no binding snapshot (nothing to copy) |
| C7 bootstrap/migration/legacy | Closed | migration 20260909_000013 verified fresh-install + upgrade on isolated PostgreSQL; bootstrap procedure documented; legacy `kite_instruments` consumers enumerated and preserved | `documents/instrument-catalog-operations.md`; live deployment evidence (below); `market-runtime` store tests | Whole-app (options/orders/history) migration stays a later slice per decision 9 |

## Phase 1.5 hardening

| Requirement | Status | Evidence |
| --- | --- | --- |
| Ownership acquire/renew/expiry/takeover/fencing | Closed (real PostgreSQL) | `tests/integration/test_alerts_postgres_hardening.py::test_ownership_takeover_and_stale_owner_fencing` |
| Two workers racing one occurrence (different epochs) | Closed | `::test_concurrent_occurrence_dedup_yields_one_event`; epoch restart semantics in `tests/workflows/test_recovery.py` |
| Event/checkpoint/outbox atomicity under injected crash | Closed | `::test_signal_deliveries_checkpoint_rollback_together` |
| Delivery claim/reclaim, pre-send expiry, stale completion | Closed | `::test_delivery_claims_are_disjoint_and_expired_leases_reclaim`, `::test_pre_send_lease_recheck_prevents_stale_completion` |
| Concurrent create/activate idempotency (E-29) | Closed + 1 bug fixed | `::test_concurrent_workflow_creation_is_idempotent_by_idempotency_key`; savepoint replay added to `repository._create_workflow` |
| Catalog publication concurrency (Section 2) | Closed | `tests/integration/test_catalog_publication_postgres.py::test_concurrent_publications_serialize_to_one_generation` |
| API test harness hang | Closed (environmental) | Root causes fixed in this branch: SQLite-invalid pool kwargs in `backend/app/database.py`, broken cryptography/pyOpenSSL pairing in the venv, stub `__spec__`, stale websocket test. Whole `tests/api` directory now runs (no hang); residual failures are pre-existing stale pre-move tests unrelated to alerts |
| API/worker restart, Redis reconnect, corrections, replay | Carried from Phase 1 + C1 | `tests/workflows/test_recovery.py`, `tests/workflows/test_service_warmup.py` |
| Compose/deploy verification | Closed | Migration applied live; API/worker/market-runtime rebuilt and healthy; renewal observed across multiple lease periods; initial catalog published (generation `48d56789…`, 88,769 records, 7 exchanges) and Go reload acknowledged the same generation/count |
| Smoke script (both paths) | Carried | `scripts/smoke_alerts.sh` (live: channel test-send AND tick→event→outbox→provider, bounded polling, unique identity, cleanup after verification); `MODE=integration` runs `tests/integration/test_alerts_smoke.py` (fan-out with one provider failing) |

## Phase 2 (F7/F8)

| Requirement | Status | Implementation | Evidence |
| --- | --- | --- | --- |
| F7 saved lists / index / portfolio universes | Closed | `backend/workflows/universes.py` (UniverseService, kinds explicit/index/portfolio, owner-scoped), migration 20260909_000014 | `tests/workflows/test_universes.py` (20) |
| F7 union/exclusion/dedupe, effective revisions, freshness | Closed | deterministic union/exclude, sorted dedupe, revision rows with coverage + source freshness; unavailable source is never an empty success | universe tests; `documents/workflow-format.md` |
| F7 workflow references, no-restart changes | Closed | document `universe:` expression; worker materializes/departs members on refresh; new members warm before signaling | `tests/workflows/test_universe_materialization.py` |
| F7 events keep membership revision; owner isolation | Closed | `universe_revision` in evidence + subscription config; owner scoping on every universe query | materialization tests; `tests/api/test_worker_universes.py` (17) |
| F8 indicators + volume features, SDK parity | Closed | `backend/alerts/features.py` (CALC_VERSION=1) — sma/ema/wma/rsi/macd/atr/bollinger/supertrend/vwap_session/volume_sma/volume_ratio | `tests/alerts/test_feature_numerics.py` (30; parity ≤5.7e-14 measured) |
| F8 compute-once fan-out; EMA20/EMA50 coexist | Closed | `backend/workflows/feature_engine.py` + per-sub plans | `tests/workflows/test_quality_momentum_layered.py::test_identical_dependency_computed_once_across_100_rules` |
| F8 bounded warmup; insufficient → unknown (E-15/E-26) | Closed | bounded windows (400 bars); None values propagate | engine unit path + layered unknown test |
| F8 layered AND/OR/NOT 3VL; daily confirmations (E-16) | Closed | predicates groups with 3VL; ancestor layers use latest completed-bar snapshots | layered tests; `test_predicates_public_surface`-adjacent suites |
| F8 quality-momentum reference workflow | Closed (with explicit scope decision) | fixture compiles and executes end-to-end on deterministic fixtures; fundamentals conditions consume the latest snapshot via context with acquisition metadata — there is NO fundamentals event stream (`evaluate_on: fundamentals_refresh` maps to candle_close) | `test_unknown_upstream_layer_blocks_firing` + plan tests |
| P2-D capabilities discovery / admission limits | Closed | `GET /api/worker/workflows/capabilities` from the same registry the compiler validates; stage/feature/arith depth+width limits enforced at compile | tests/api/test_worker_workflows.py; compiler tests |
| P2-D storm controls (E-25) | Closed | per-rule rolling emission budget with `storm_budget` suppression reason | `test_storm_budget_bounds_emissions_and_is_reported` |
| P2-D measured capacity (500 symbols / 5,000 rules) | Deferred | certification target is Phase 6 per spec §9; no measurement claims made | — |

## Live deployment evidence (2026-09-09)

- Live `kite-postgres` migrated 20260909_000012 → 20260909_000013 (additive).
- Bootstrap import via the production code path inside `kite-app`: status
  `published`, generation `48d56789-9ee2-4ca6-bf83-b536368d6fb1`, accepted
  exchanges BCD/BFO/BSE/CDS/MCX/NFO/NFO/NSE (7/7), record_count 88,769,
  zero validation errors.
- Go reload ack: `{"count":88769,"generation":"48d56789…","status":"ok"}` —
  matches the published generation exactly.
- Worker health file shows `binding_revisions`, `unresolved_instruments`,
  renewal timestamps advancing at TTL/3 across multiple lease periods with
  zero renewal failures.

A deployment race was found and fixed during this verification: the new
market-runtime attempted its initial store load before the API container had
finished migrations, and never retried (log: `instrument store load failed`).
The load now retries with backoff (5 attempts), and recovery via the refresh
endpoint was demonstrated live.
