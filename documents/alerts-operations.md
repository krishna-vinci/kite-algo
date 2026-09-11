# Alerts platform — operations guide (Phase 1)

How to run, activate, restart, and reason about the alert service. Format details: [workflow-format.md](workflow-format.md). Design: [spec v2](../docs/superpowers/specs/2026-09-08-alerts-platform-spec-v2.md).

## Components

| Process | What it does | Run |
| --- | --- | --- |
| FastAPI app | authoring: validate/preview/CRUD/activate/history/channels | existing `backend/main.py` |
| Evaluation worker | subscribes instruments, consumes ticks + completed candles, evaluates rules, commits events + outbox | `.venv/bin/python -m backend.workflows.worker_entry` |
| Delivery worker | claims outbox rows, sends to Telegram/ntfy, records attempts | embedded in the evaluation worker loop (Phase 1); standalone `backend.notifications.worker.DeliveryWorker.run_forever()` available |

The evaluation worker is deliberately a separate process: restarting or deploying the API never interrupts monitoring.

## Configuration (environment)

| Variable | Used by | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | worker + API | Postgres connection. If absent in Compose, both workers assemble it from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`; credentials are URL-encoded. |
| `REDIS_URL` | evaluation worker | Redis for `market:ticks` and `realtime_candles:{token}:{interval}` pub/sub — worker exits if unset |
| `MARKET_RUNTIME_URL` | evaluation worker | market-runtime HTTP endpoint used to register instrument subscriptions |
| `ALERTS_INSTRUMENT_TOKENS` | evaluation worker | JSON map of `"EXCHANGE:SYMBOL"` to numeric instrument tokens, e.g. `{"NSE:RELIANCE": 738561}`. **Required for instrument resolution**: without a token for an instrument key the worker cannot subscribe to its ticks, resolve completed candles, or read candle history, so its rules stay silent. |
| `ALERTS_REFRESH_INTERVAL_S` | evaluation worker | Seconds between subscription-refresh/renewal passes (default 10): the worker re-materializes subscriptions from the database (picking up activations, pauses, archives, completed rules, and orphan cleanup without a restart) and renews its market-runtime registration on this interval |
| `ALERTS_HEALTH_INTERVAL_S` / `ALERTS_HEALTH_FILE` | evaluation worker | Health snapshot cadence (default 30s) and optional JSON path. Compose writes `/app/alerts-health.json`, which is also the worker container healthcheck. |
| `ALERTS_DELIVERY_ENABLED` | delivery (in-process) | Set `false`/`0` to disable the embedded delivery loop (outbox rows stay pending for a standalone delivery worker); delivery is on by default |
| `ALERTS_HEALTH_FILE` | evaluation worker | Optional path where the worker periodically writes its health JSON (counters above + `last_evaluated_at`) for scraping without Redis/DB access |
| `TELEGRAM_BOT_TOKEN` | delivery (channel `secret_env`) | bot token; chat id lives in the channel row |
| `NTFY_PRIMARY_URL` (per channel `secret_env`) | delivery | full ntfy topic URL |

Secrets exist only in env config. Channel rows reference them by name (`secret_env`); workflow documents contain channel *names* only. A channel's `secret_env` names the variable the adapter resolves at send time (telegram `token_env`, ntfy `url_env`); test-send pre-checks exactly that variable and fails with a 400 naming it when unset.

## Database

Migration `20260908_000011_alerts_platform_phase1` plus `20260909_000012_alerts_evaluation_ownership` creates the Phase 1 alert tables and the durable evaluation-owner lease table. Apply with the project's normal `alembic upgrade head` flow. The historical dropped-alerts migration is never reversed.

## Operating procedures

**Activate a workflow:** import/create draft → `POST /{id}/activate` (requires `workflows:activate`). Activation validates, selects the revision, and materializes one subscription per (alert, instrument). Only one revision is active at a time; older revisions keep their event history.

**Pause / resume:** `POST /{id}/pause|resume` flips subscription state; checkpoints and history are retained. Pausing stops evaluation effects but the worker keeps ownership bookkeeping.

**Roll back an edit:** `POST /{id}/activate` with an explicit revision body — `{"revision": N}` — activates that revision (re-activating an archived one and archiving the currently-active revision; exactly one revision stays active). Regretting a PATCH that created a draft you never activated? Just activate the previously good revision. `GET /{id}/export?revision=N` inspects any revision first. Activating an archived workflow also un-archives it.

**Subscription renewal:** every `ALERTS_REFRESH_INTERVAL_S` the worker re-reads active subscriptions from the database and renews its market-runtime instrument registration. Activations, pauses and rollbacks are picked up on the next refresh pass without a worker restart; checkpoints and event history are untouched by refreshes.

**Restart the worker (planned or crash):**
- Evaluation ownership is durable and fenced per `(subscription, instrument)`: one live worker evaluates a key at a time; a takeover increments the owner epoch.
- `ltp`-clock rules intentionally start a **new observation epoch**: first tick initializes state, nothing fires from stale pre-restart data. A crossing during downtime is *missed and disclosed* (health gap counter), never fabricated. Trigger bookkeeping is retained so a restart cannot reset a `once` rule.
- `candle_close` rules recover continuity from completed candles (warmup replay happens before live evaluation), so confirmed signals resume correctly.
- A reconnect replays completed history before exposing live candles. A changed final bar at an existing timestamp is treated as a correction and produces a silent correction audit event; an identical duplicate is ignored.

**Degraded feed:** a Pub/Sub disconnect or trimmed data breaks crossing continuity. The rule re-initializes on the next observation (new epoch) and the gap shows up in worker health (`gaps` counter). Missing data never manufactures a signal — unknown propagates.

**Trading calendar:** production evaluation uses the operator-imported NSE CM session calendar, including holidays and special hours. An unsupported calendar name is rejected at validation; missing calendar coverage fails closed for live evaluation. Previous-day level predicates use the previous trading session, not the previous civil day.

**Unknown sends:** a provider timeout is recorded as outcome `unknown` and the delivery is retried — but only a **finite** number of times (`max_unknown_retries`); after the last unknown attempt the delivery stops with `failed` and the attempts log records every outcome. **Exactly-once delivery to the provider is explicitly not promised**: at-least-once is the guarantee, provider acceptance is not a read receipt, and duplicates are identifiable from the attempt log (the event id is in every message).

**Test a channel:** `POST /api/worker/notification-channels/{id}/test` sends a real message (requires `notifications:test`). Missing env secret returns a 400 naming the variable — delivery would record `failed` similarly instead of dropping silently.

## Observability

- `GET /api/worker/workflows/{id}/health` — active revision, per-subscription state **and `last_evaluated_at`** (from `evaluation_checkpoints.updated_at`, per subscription), last event time, delivery counts by status, and the constant `stale_after_seconds: 300`. A subscription whose `last_evaluated_at` is older than `stale_after_seconds` (relative to now) should be treated as stale — a stale workflow is visible as stale, never silently quiet (F11).
- `GET /api/worker/workflows/{id}/events` — event history with evidence (values, epoch, timeframe); archived workflows keep their history.
- Worker process logs report evaluations, emissions, suppression reasons (cooldown, not_armed, already_fired, quiet_session, expired), unresolved channels/instruments, and gaps. Every non-delivery has a recorded reason.

## Phase 2 additions (universes, features, layered rules)

- **Universes:** documents may reference saved universes and index sources
  (`universe:` block). The worker re-resolves membership every
  `ALERTS_UNIVERSE_RESOLVE_INTERVAL_S` (default 300 s); new members are
  admitted with a fresh epoch and warmed before they may signal; departed
  members are paused with `universe_departed` in their config. A failed
  resolution keeps the last valid membership and increments
  `universe_membership.resolution_failures` in worker health.
- **Shared features:** indicator/layered stages compute through one feature
  engine per worker; identical dependencies compute once per event. Windows
  are bounded (`400` completed bars per instrument/timeframe) and warmed from
  durable history before live evaluation. Insufficient history is `unknown`,
  never a signal.
- **Storm guard:** emissions per (workflow, alert) are bounded by
  `ALERTS_DELIVERY_BUDGET_PER_WINDOW` (default 60/60s); excess is suppressed
  with reason `storm_budget`.
- **Capabilities:** `GET /api/worker/workflows/capabilities` lists exactly the
  executable functions/operators/limits; authoring contract in
  [workflow-format.md](workflow-format.md); full parity in
  [alerts-phase2-parity.md](alerts-phase2-parity.md).

## Phase 3 additions (scheduled screeners, attachments, dynamic universes)

- **Authoring:** a document with a `screener:` block is a screener workflow
  (same validation/lifecycle/authorization; YAML + `/api/worker/workflows`
  CRUD). The reference example is
  `tests/fixtures/workflows/nifty-quality-momentum-screener.yaml`. Screeners
  run over STORED completed candles; live-tick stages and per-rule alerts
  are rejected in screener documents — attachments are the notification
  surface.
- **Schedules:** IST-anchored buckets on the NSE calendar only
  (`calendar: nse_equity`). MCX/currency have feed-driven eligibility and no
  session calendar, so such schedules are REJECTED at validation rather than
  silently applying NSE hours. Non-session days (holidays) produce no runs.
  Missed schedules coalesce to the latest due occurrence — a day of downtime
  yields one catch-up run, never a backlog (E-19).
- **Manual run:** `POST /api/worker/screeners/{id}/runs?idempotency_key=...`
  executes immediately through the same pipeline; the same key returns the
  original run instead of duplicating it.
- **Inspection:** `GET .../runs` (history), `GET .../runs/{run_id}` (members,
  ranks, exclusion reasons, coverage, freshness), `GET .../events`
  (attachment events). A `partial` run lists which members were excluded and
  why; `failed` carries a `failure_reason`.
- **Attachments:** entry/exit/top-N/rank-delta triggers with persisted
  state — restart never resets baselines or hysteresis bands. Events flow
  through the same signal event + outbox + delivery-worker machinery as
  alerts (same Telegram/ntfy channels; no new bot). Events are idempotent
  per (run, attachment, instrument); a per-run emission cap bounds storms.
  Partial runs never emit events and never advance baselines (E-18).
- **Downstream universes:** create a universe of kind `screener` with
  `source_config: {"workflow": "<screener name>", "top_n": 10}`. The worker
  re-materializes it after each complete run; members added warm up before
  they may signal; if the screener has no COMPLETE run within
  `freshness_limit_s`, resolution fails visibly and dependent alerts stay
  silent (unknown) instead of scanning stale membership.
- **Health:** the worker exposes no separate screener health file; run
  history is the record. `data_freshness` per run carries the max completed
  candle timestamp and fundamentals acquisition metadata per §5.8.
- **Env knobs:** `ALERTS_SCREENER_POLL_INTERVAL_S` (30),
  `ALERTS_SCREENER_LEASE_TTL_S` (300), `ALERTS_SCREENER_WINDOW_BARS` (120),
  `ALERTS_SCREENER_MAX_ATTACHMENT_EVENTS` (100).

## Known Phase 1 limitations

- Delivery attempts and lease claims are tested on SQLite; true multi-process Postgres concurrency (`FOR UPDATE SKIP LOCKED`) is exercised in production Postgres only — Phase 1.5 follow-up adds a Postgres-based fault-injection suite (spec E-1…E-3).
- The API preview dry-run accepts caller-supplied, exchange-timestamped samples; it does not fetch a hidden live-data snapshot. It never writes checkpoints, events, or deliveries.
- Indicators, universes, screeners, quiet hours/digest: later phases (schema reserves the names).

## Phase 4 additions (advanced conditions, external producers, SDK)

### Authoring

Advanced conditions are ordinary document fields (see
[workflow-format.md](workflow-format.md)): `consecutive_bars`, a bounded
`sequence` (`within_bars` and/or `within`), condition `hysteresis`, a `breadth`
stage, `pair_ratio`/`relative_strength` operands and `max_per_session`. They
are authored through the same YAML/REST/SDK paths as everything else and
execute with the same lifecycle and authorization.

SDK: `KiteAlgoWorkerClient` / `AsyncKiteAlgoWorkerClient` (package
`kite-algo-worker` 0.10.0) expose the platform surface — capabilities,
validate, preview, CRUD, activate/pause/resume/archive, YAML import/export,
events, health, universes, screeners, channels, and the producer/value surface.
The platform mount is `/api/worker/...`, configured by
`AlgoWorkerConfig.platform_prefix` (default `/api`).

### External signal producers

Producers are **not** authorized by worker tokens; they have their own
credential, so neither side can do the other's job.

```bash
# 1. Register (needs a worker token holding signals:admin — NOT a default grant)
curl -X POST "$API/api/worker/signals/producers" \
  -H "Authorization: Bearer $WORKER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"my-model","value_schema":{"fields":{"score":"number"}},"default_ttl_s":900}'

# 2. Issue a credential. The SECRET IS SHOWN ONCE — store it now.
curl -X POST "$API/api/worker/signals/producers/my-model/credentials" \
  -H "Authorization: Bearer $WORKER_TOKEN"
# {"ok":true,"token_id":"producer_...","secret":"kas_...","note":"store this now; it cannot be retrieved"}

# 3. Submit a value as the PRODUCER
curl -X POST "$API/api/worker/signals/values" \
  -H "Authorization: Bearer kas_..." -H 'Content-Type: application/json' \
  -d '{"value":{"score":82.5},"event_time":"2026-09-11T09:55:00Z","idempotency_key":"run-42"}'

# 4. Revoke when done (disables the producer and all its credentials)
curl -X POST "$API/api/worker/signals/producers/my-model/revoke" \
  -H "Authorization: Bearer $WORKER_TOKEN"
```

**Operations**

- **Credential storage**: the secret is stored only as a hash. It cannot be
  retrieved, is echoed in no other response and is never logged. Losing it means
  issuing a new credential and revoking the old one. Multiple credentials can
  exist at once (rotation): issuing a second does not invalidate the first;
  revoke individually or revoke the whole producer.
- **Ingestion guards**: a value stamped more than 300s in the future is
  rejected; one later than 3600s is stored with `status='late'` — recorded for
  the audit trail but permanently unusable; an already-past `expires_at` is
  rejected; the payload is bounded (8 KiB, 32 fields, 512-char strings, finite
  numbers only).
- **Idempotency**: `idempotency_key` is unique per producer. Submitting the same
  key with identical content returns the original value with
  `deduplicated: true`; with different content it is refused (409
  `IDEMPOTENCY_CONFLICT`) rather than overwritten. A retry is therefore safe.
  Keys age out with their row, so a replay older than the retention window is
  treated as new work.
- **Capacity**: a producer at its row limit (100,000) is refused with 429 rather
  than having a still-valid value evicted. Expired values are purged
  automatically once past `ALERTS_EXTERNAL_RETENTION_S`.

**Restart recovery.** A value is committed before the endpoint responds, so a
2xx means stored — a restart between acceptance and evaluation loses nothing.
There is no per-value consumption queue to lose: a value that is still
unexpired is visible to the first evaluation after the restart.

**Stale inputs.** `GET /api/worker/signals/health` reports, per producer,
accepted/late counts, `expired_now`, the last receipt time, and whether it is
disabled or revoked. A consuming condition is `unknown` (never `false`) for an
absent, expired, late, future or revoked value, with the reason recorded in
event evidence.

**Rollback.** `POST /api/worker/signals/producers/{name}/revoke` disables
ingestion and makes stored values unusable immediately, without deleting them.

### Database migration

Migration `20260911_000016_alerts_phase4` adds the Phase 4 tables and repairs
the `universes.kind` CHECK so `screener`-backed universes work (a Phase 3
defect: the constraint admitted only `explicit`/`index`/`portfolio`, so creating
one failed on PostgreSQL even though the code supported it).

**Downgrade is conditional.** It refuses while user-authored configuration
exists — external producers, their credentials, or `screener`-kind universes —
and every refusal check runs BEFORE any DDL, so a refused downgrade leaves the
schema exactly as it was. To drop that configuration deliberately:

```bash
alembic -c backend/alembic.ini -x drop_phase4_config=true downgrade 20260910_000015
```

Export first (`GET /api/worker/signals/producers` plus a database backup): the
configuration is not recoverable by re-upgrading.

### Environment additions

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALERTS_PAIR_MAX_BAR_AGE_S` | `0` (derive `2 × timeframe`) | Max age of a pair leg's head bar before the pair is unknown |
| `ALERTS_BREADTH_MEMBERSHIP_MAX_AGE_S` | `900` | Age at which a breadth stage's membership becomes `membership_stale` |
| `ALERTS_BREADTH_MAX_INSTRUMENTS` | `1000` | Member count above which breadth reports `breadth_capacity_exceeded` |
| `ALERTS_EXTERNAL_RETENTION_S` | `604800` (7d) | How long past expiry an unusable value is retained |
| `ALERTS_EXTERNAL_MAX_ROWS_PER_PRODUCER` | `100000` | Per-producer value cap (rejects; never evicts a valid value) |
