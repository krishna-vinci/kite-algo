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
| `DATABASE_URL` | worker + API | Postgres connection (Phase 1 tests use in-memory SQLite) |
| `REDIS_URL` | evaluation worker | Redis for `market:ticks` and `realtime_candles:{token}:{interval}` pub/sub — worker exits if unset |
| `MARKET_RUNTIME_URL` | evaluation worker | market-runtime HTTP endpoint used to register instrument subscriptions |
| `TELEGRAM_BOT_TOKEN` | delivery (channel `secret_env`) | bot token; chat id lives in the channel row |
| `NTFY_PRIMARY_URL` (per channel `secret_env`) | delivery | full ntfy topic URL |

Secrets exist only in env config. Channel rows reference them by name (`secret_env`); workflow documents contain channel *names* only.

## Database

Migration `20260908_000011_alerts_platform_phase1` (head after `20260905_000010`) creates: `workflows`, `workflow_revisions`, `alert_subscriptions`, `evaluation_checkpoints`, `signal_events`, `channel_references`, `deliveries`, `delivery_attempts`. Apply with the project's normal `alembic upgrade head` flow. The historical dropped-alerts migration is never reversed.

## Operating procedures

**Activate a workflow:** import/create draft → `POST /{id}/activate` (requires `workflows:activate`). Activation validates, selects the revision, and materializes one subscription per (alert, instrument). Only one revision is active at a time; older revisions keep their event history.

**Pause / resume:** `POST /{id}/pause|resume` flips subscription state; checkpoints and history are retained. Pausing stops evaluation effects but the worker keeps ownership bookkeeping.

**Roll back an edit:** PATCH created a new draft you regret? Just activate the previously good revision (drafts are kept; `GET /{id}/export?revision=N` to inspect any revision).

**Restart the worker (planned or crash):**
- `ltp`-clock rules intentionally start a **new observation epoch**: first tick initializes state, nothing fires from stale pre-restart data. A crossing during downtime is *missed and disclosed* (health gap counter), never fabricated.
- `candle_close` rules recover continuity from completed candles (warmup replay happens before live evaluation), so confirmed signals resume correctly.

**Degraded feed:** a Pub/Sub disconnect or trimmed data breaks crossing continuity. The rule re-initializes on the next observation (new epoch) and the gap shows up in worker health (`gaps` counter). Missing data never manufactures a signal — unknown propagates.

**Unknown sends:** a provider timeout is recorded as outcome `unknown`, the delivery retries after the default backoff, and delivery history keeps every attempt with its outcome. Provider acceptance is not a read receipt. At-least-once to the provider is the guarantee; the delivery attempt log makes duplicates identifiable (event id is in every message).

**Test a channel:** `POST /api/worker/notification-channels/{id}/test` sends a real message (requires `notifications:test`). Missing env secret returns a 400 naming the variable — delivery would record `failed` similarly instead of dropping silently.

## Observability

- `GET /api/worker/workflows/{id}/health` — active revision, per-subscription state, last event time, delivery counts by status.
- `GET /api/worker/workflows/{id}/events` — event history with evidence (values, epoch, timeframe); archived workflows keep their history.
- Worker process logs report evaluations, emissions, suppression reasons (cooldown, not_armed, already_fired, quiet_session, expired), unresolved channels, and gaps. Every non-delivery has a recorded reason.

## Known Phase 1 limitations

- Delivery attempts and lease claims are tested on SQLite; true multi-process Postgres concurrency (`FOR UPDATE SKIP LOCKED`) is exercised in production Postgres only — Phase 1.5 follow-up adds a Postgres-based fault-injection suite (spec E-1…E-3).
- Compose/service wiring for the worker process is intentionally deferred (in-flight compose changes); run via `python -m` behind a supervisor for now.
- Indicators, universes, screeners, quiet hours/digest: later phases (schema reserves the names).
