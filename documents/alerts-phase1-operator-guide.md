# Alerts Phase 1 — operator guide

This is the shortest safe path from a fresh checkout to one verified alert.
Commands assume the repository root is the current directory and that secrets
are supplied through the deployment secret store or an uncommitted `.env`.
Never paste bot tokens, ntfy URLs, passwords, or worker tokens into chat or
commit them.

## 1. Configure the services

Copy the project environment template and fill in the existing application,
Postgres, Redis, and market-runtime settings:

```sh
cp .env.example .env
```

For the alerts worker, use either `DATABASE_URL` or the complete `DB_*` set.
Compose supplies the internal hostnames. Set:

```dotenv
REDIS_URL=redis://redis:6379/0
MARKET_RUNTIME_URL=http://market-runtime:8780
ALERTS_INSTRUMENT_TOKENS={"NSE:RELIANCE": 738561}
ALERTS_REFRESH_INTERVAL_S=10
ALERTS_HEALTH_INTERVAL_S=30
ALERTS_DELIVERY_ENABLED=1
```

`ALERTS_INSTRUMENT_TOKENS` must contain every `EXCHANGE:SYMBOL` used by an
active workflow. Obtain the current numeric token from the broker instrument
master/import used by this deployment; do not guess a token from an old
instrument list. Missing mappings are reported as unresolved and the rule
remains silent.

Apply migrations before starting the worker:

```sh
docker compose -f compose.yml -f compose.worker.yml up -d postgres redis finance-app market-runtime
docker compose -f compose.yml -f compose.worker.yml exec finance-app \
  alembic -c /app/backend/alembic.ini upgrade head
docker compose -f compose.yml -f compose.worker.yml up -d alerts-worker
```

The worker health file is `/app/alerts-health.json` inside the container. The
Compose healthcheck requires it to be non-empty:

```sh
docker compose -f compose.yml -f compose.worker.yml logs -f alerts-worker
docker compose -f compose.yml -f compose.worker.yml exec alerts-worker \
  sh -c 'python -c "import json; print(json.dumps(json.load(open(\"/app/alerts-health.json\")), indent=2))"'
```

For a host-run worker, use `.venv/bin/python -m backend.workflows.worker_entry`
with the same environment. The standalone delivery entry point uses the same
database resolver and production subscription loader; it does not create a
local SQLite database.

## 2. Configure one notification channel

The API stores only a channel name, provider, destination metadata, and the
name of a secret environment variable. The secret value is read at send time.

### Telegram

1. Create a bot with [Telegram BotFather](https://t.me/BotFather), send the bot
   `/start`, and put the token in the API and worker secret environment as
   `TELEGRAM_BOT_TOKEN`.
2. With the token already loaded in the shell (do not print it), obtain the
   chat id by sending a message to the bot and reading only ids:

   ```sh
   curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" |
     .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(sorted({str(x.get("message",{}).get("chat",{}).get("id")) for x in d.get("result",[]) if x.get("message",{}).get("chat",{}).get("id") is not None}))'
   ```

3. Create the channel through the API. `WORKER_TOKEN` is an existing worker
   token with `workflows:read`, `workflows:write`, and `notifications:test`:

   ```sh
   API_BASE=http://127.0.0.1:18777
   curl -fsS -X POST "$API_BASE/api/worker/notification-channels" \
     -H "Authorization: Bearer $WORKER_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"name":"telegram_primary","provider":"telegram","destination":{"chat_id":"CHAT_ID"},"secret_env":"TELEGRAM_BOT_TOKEN"}'
   ```

   Replace `CHAT_ID` locally; do not put the bot token in the JSON. The live
   smoke script accepts either `CHANNEL_CHAT_ID` or `TELE_CHAT_ID`.

Telegram's current Bot API uses HTTPS requests of the form
`https://api.telegram.org/bot<token>/METHOD_NAME`; see the [official Bot API
manual](https://core.telegram.org/bots/api) and [Bot FAQ](https://core.telegram.org/bots/faq).

### ntfy

Choose a topic on the approved ntfy server, or use the deployment's private
ntfy base URL. Put the full topic URL in a secret environment variable such as
`NTFY_PRIMARY_URL` on both API and worker. Create the channel with an empty
destination and a matching `secret_env`:

```sh
curl -fsS -X POST "$API_BASE/api/worker/notification-channels" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"ntfy_primary","provider":"ntfy","destination":{},"secret_env":"NTFY_PRIMARY_URL"}'
```

The adapter POSTs the message body to that URL. Review ntfy's [publishing
guide](https://docs.ntfy.sh/publish/) and, for self-hosted deployments, its
[server configuration guide](https://docs.ntfy.sh/config/). Treat a private
topic URL as a secret even if the server does not require authentication.

Test the channel before attaching it to a workflow:

```sh
curl -fsS -X POST "$API_BASE/api/worker/notification-channels/CHANNEL_ID/test" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"alerts channel test"}'
```

`status: accepted` means the provider accepted the request, not that a human
has read the message. A missing `secret_env` variable is a configuration
failure and is named in the response without exposing its value.

## 3. Validate, preview, import, and activate

Validate first. The alert contract is documented in
[workflow-format.md](workflow-format.md). Preview can evaluate supplied recent
samples without writing anything:

```sh
curl -fsS -X POST "$API_BASE/api/worker/workflows/preview" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @preview.json
```

`preview.json` contains `yaml_text` and, optionally, an `observations` array
with `instrument_key`, `epoch_id`, a real exchange `ts`, and market fields such
as `ltp` or `close`. `evaluation: dry_run` and `would_fire` are advisory only;
the preview creates no workflow, checkpoint, event, or delivery.

Import a valid YAML file as a draft, then activate it explicitly:

```sh
curl -fsS -X POST "$API_BASE/api/worker/workflows/import" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary '{"yaml_text":"...","idempotency_key":"reliance-breakout-2026-09-09"}'

curl -fsS -X POST "$API_BASE/api/worker/workflows/WORKFLOW_ID/activate" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

Activation creates one subscription per alert/instrument. The worker refreshes
active subscriptions every 10 seconds, so activation, pause, resume, archive,
and revision changes do not require a process restart.

## 4. Verify the complete path

Run the isolated smoke test when provider credentials or market-runtime data are
not available:

```sh
MODE=integration sh scripts/smoke_alerts.sh
```

For a bounded live check, use a fresh channel and a worker token with the four
actions named in the script. The script accepts `WORKER_TOKEN` or the existing
`KITE_MCP_WORKER_TOKEN`. Telegram additionally needs `CHANNEL_CHAT_ID` (or
`TELE_CHAT_ID`) and
`TELEGRAM_BOT_TOKEN`; ntfy needs `CHANNEL_TOPIC_URL` and the configured secret
variable:

```sh
WORKER_TOKEN='loaded-by-your-secret-manager' \
CHANNEL_PROVIDER=telegram CHANNEL_CHAT_ID='your-chat-id' \
POLL_TIMEOUT_S=90 sh scripts/smoke_alerts.sh
```

The live smoke performs a direct channel test, then validates/imports/activates
a unique workflow and waits for evaluated state, an event id, and a delivered
outbox row. It archives its workflow afterward unless `KEEP=1`; it leaves the
channel untouched for explicit cleanup.

## 5. Operate and troubleshoot

- **No evaluation:** inspect `ALERTS_INSTRUMENT_TOKENS`, market-runtime
  ownership, Redis connectivity, and the worker health file. An unresolved
  instrument is intentionally silent.
- **No event after restart:** LTP rules require a new epoch and first-tick
  activation guard. Candle rules replay closed history before live data. A
  missed crossing during an outage is not reconstructed from wall-clock time.
- **Duplicate or corrected candle:** identical timestamp/payload is ignored;
  changed final payload is recorded as a silent correction audit event and
  recomputed from the state before that bar.
- **Stale workflow:** `GET /api/worker/workflows/WORKFLOW_ID/health` reports
  `last_evaluated_at`, delivery counts, unresolved channels, gaps, and the
  `stale_after_seconds` contract. A stale subscription is visible; it is not
  treated as a successful quiet state.
- **Provider failure:** inspect the event id, delivery row, and delivery
  attempts. Accepted is provider acceptance; unknown outcomes retry only within
  a finite budget. Message bodies include the event id, including custom
  templates, so provider duplicates can be correlated.
- **Calendar/session issue:** `nse_equity` uses the operator-imported NSE CM
  session calendar. MCX and currency workflows use the Phase 1 feed-driven
  `mcx_commodity` and `currency` session policies instead of the NSE calendar;
  their workflow session must match the instrument exchange. Unsupported
  session names and session/exchange mismatches fail validation. Missing NSE
  calendar coverage fails closed. Previous-day predicates use the previous
  trading session's levels.

For a complete event trace, use the workflow events endpoint:

```sh
curl -fsS "$API_BASE/api/worker/workflows/WORKFLOW_ID/events?limit=50" \
  -H "Authorization: Bearer $WORKER_TOKEN"
```
