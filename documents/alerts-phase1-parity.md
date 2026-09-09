# Alerts Phase 1 parity matrix

Status of the handoff findings against the implemented Phase 1 contract.

| Finding | Coverage | Evidence / operator check |
| --- | --- | --- |
| Compose worker database configuration | Closed | `worker_entry.resolve_database_url`; Compose DB_* settings; standalone delivery uses the same resolver. |
| Delivery timing and lease safety | Closed | Per-row live clock, pre-send expiry/lease recheck, lease-fenced completion; `tests/notifications/test_delivery_worker.py`. |
| Temporary subscription/context failures | Closed | Loader raises a retryable error for DB failures; missing rows remain terminally unresolvable. |
| Nested evidence and event identity | Closed | Custom templates retain `event_id`; nested predicate evidence is flattened only when unambiguous; truncation reserves the id. |
| LTP duplicate ownership / epoch behavior | Closed | Durable `(subscription, instrument)` owner lease and owner epoch; trigger bookkeeping survives source epoch changes; overlap test covers fencing. |
| Candle reconnect and correction behavior | Closed | History replay before live exposure, calendar-aware gap reset, exact duplicate suppression, changed final-bar correction audit. |
| Session/calendar injection | Closed | Supported session capability validation; production worker injects NSE CM calendar provider and fails closed on missing coverage. |
| Previous-session context | Closed | `PgCandleHistory.previous_session_levels` supplies previous trading-session high/low to predicates. |
| Malformed or missing event timestamps | Closed | Runtime sources drop invalid/non-finite/missing timestamps; no wall-clock substitution. |
| Standalone delivery parity | Closed | `backend.notifications.worker.main` uses production repository, resolver, DB config, and migrations. |
| Lifecycle refresh / orphan cleanup | Closed | Refresh diffs active subscriptions and removes paused, archived, completed, and no-longer-active rows without restart. |
| Preview, smoke, and provider parity | Closed with bounded live prerequisite | Preview is deterministic/read-only over supplied samples; isolated smoke covers fan-out/provider failure; live smoke requires user-owned token, destination, worker, and fresh market data. |

## Deliberate limits

- Provider acceptance is not read receipt and exactly-once provider delivery is
  not promised. Event ids and attempt rows make duplicates auditable.
- Postgres row-lock behavior must still be exercised in the deployment
  environment; SQLite tests cover semantics but not multi-process locking.
- Phase 1 does not implement indicators, universes, screeners, quiet hours, or
  digest batching.
