#!/usr/bin/env sh
# End-to-end smoke test for the alerts platform (Phase 1).
#
# Exercises: channel test-send and workflow-driven delivery as separate paths.
# The workflow name and channel name are unique per invocation, so the script
# never overwrites or disables a user's existing channel.
#
# Required environment:
#   API_BASE      e.g. http://127.0.0.1:18777           (default below)
#   WORKER_TOKEN or KITE_MCP_WORKER_TOKEN  a worker token with actions:
#                 workflows:read workflows:write workflows:activate notifications:test
#   CHANNEL_PROVIDER   telegram (default) or ntfy
#   CHANNEL_NAME       optional explicit new destination name
#   For telegram: CHANNEL_CHAT_ID (numeric chat id) and TELEGRAM_BOT_TOKEN in the
#                 API process environment.
#   For ntfy:     CHANNEL_TOPIC_URL and the topic URL exported in the API process env
#                 (secret_env defaults to NTFY_PRIMARY_URL).
#
#   KEEP=1             leave the smoke workflow/channel in place
#   POLL_TIMEOUT_S     bounded event/delivery wait (default: 90)
#   MODE=live|integration; integration runs the isolated mocked-provider test
#
# Usage: sh scripts/smoke_alerts.sh

set -eu

if [ "${MODE:-live}" = "integration" ]; then
  exec "${PYTHON_BIN:-.venv/bin/python}" -m pytest tests/integration/test_alerts_smoke.py -q
fi

API_BASE="${API_BASE:-http://127.0.0.1:18777}"
WORKER_TOKEN="${WORKER_TOKEN:-${KITE_MCP_WORKER_TOKEN:-}}"
: "${WORKER_TOKEN:?WORKER_TOKEN or KITE_MCP_WORKER_TOKEN is required}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
PROVIDER="${CHANNEL_PROVIDER:-telegram}"
SMOKE_ID="alerts-smoke-$(date +%s)-$$"
CHANNEL_NAME="${CHANNEL_NAME:-${SMOKE_ID}-channel}"
KEEP="${KEEP:-0}"
POLL_TIMEOUT_S="${POLL_TIMEOUT_S:-90}"

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/alerts-smoke.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM
YAML_FILE="$TMP_DIR/workflow.yaml"
IMPORT_FILE="$TMP_DIR/import.json"
IMPORT_RESPONSE="$TMP_DIR/import-response.json"
CHANNEL_RESPONSE="$TMP_DIR/channel-response.json"

auth() { curl -fsS -H "Authorization: Bearer $WORKER_TOKEN" -H "Content-Type: application/json" "$@"; }
post() { curl -fsS -X POST -H "Authorization: Bearer $WORKER_TOKEN" -H "Content-Type: application/json" "$@"; }

json_field() {
  FIELD="$1" "${PYTHON_BIN}" -c 'import json, os, sys; value=json.load(sys.stdin); print(value[os.environ["FIELD"]])'
}

assert_provider_ok() {
  "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin); status=value.get("status"); assert status in {"accepted","delivered","ok"}, value'
}

echo "== 1. channel upsert ($CHANNEL_NAME/$PROVIDER)"
if [ "$PROVIDER" = "telegram" ]; then
  CHANNEL_CHAT_ID="${CHANNEL_CHAT_ID:-${TELE_CHAT_ID:-}}"
  : "${CHANNEL_CHAT_ID:?CHANNEL_CHAT_ID or TELE_CHAT_ID is required for telegram}"
  DEST="{\"chat_id\": \"$CHANNEL_CHAT_ID\"}"
  SECRET_ENV="TELEGRAM_BOT_TOKEN"
else
  : "${CHANNEL_TOPIC_URL:?CHANNEL_TOPIC_URL is required for ntfy (used only to verify the selected destination)}"
  # The credential-bearing/full topic URL stays in the ignored API env file;
  # it is not placed in workflow JSON or exported API responses.
  DEST="{}"
  SECRET_ENV="${NTFY_SECRET_ENV:-NTFY_PRIMARY_URL}"
fi
post "$API_BASE/api/worker/notification-channels" \
  -d "{\"name\": \"$CHANNEL_NAME\", \"provider\": \"$PROVIDER\", \"destination\": $DEST, \"secret_env\": \"$SECRET_ENV\"}" > "$CHANNEL_RESPONSE"
echo "   ok"

echo "== 2. channel test-send (a real message should arrive)"
# Resolve the response envelope's channels list; never print the secret.
CHANNEL_ID=$(auth "$API_BASE/api/worker/notification-channels" | "${PYTHON_BIN}" -c "import sys,json; name='$CHANNEL_NAME'; print([c['channel_id'] for c in json.load(sys.stdin)['channels'] if c['name']==name][0])")
post "$API_BASE/api/worker/notification-channels/$CHANNEL_ID/test" -d '{"message": "alerts smoke: direct channel test"}' | assert_provider_ok
echo

echo "== 3. import + validate smoke workflow"
cat > "$YAML_FILE" <<YAML
version: 1
name: $SMOKE_ID
instruments: ["NSE:RELIANCE"]
stages:
  - id: px
    type: signal
    clock: ltp
    conditions:
      all:
        - left: {field: ltp}
          op: gt
          right: {value: 1}
alerts:
  - id: smoke
    source: px
    trigger: once
    notify_if_already_true: true
    channels: [$CHANNEL_NAME]
YAML
SMOKE_ID="$SMOKE_ID" "${PYTHON_BIN}" - "$YAML_FILE" "$IMPORT_FILE" <<'PY'
import json, pathlib, sys
import os
yaml_text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
json.dump({"yaml_text": yaml_text, "idempotency_key": os.environ["SMOKE_ID"]}, open(sys.argv[2], "w", encoding="utf-8"))
PY
post "$API_BASE/api/worker/workflows/validate" --data-binary @"$IMPORT_FILE" | "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin); assert value.get("ok") is True, value'
post "$API_BASE/api/worker/workflows/import" --data-binary @"$IMPORT_FILE" > "$IMPORT_RESPONSE"
WF=$(json_field workflow_id < "$IMPORT_RESPONSE")
echo "   workflow: $WF"

echo "== 4. activate"
post "$API_BASE/api/worker/workflows/$WF/activate" -d '{}' > /dev/null
echo "   ok"

echo "== 5. health"
DEADLINE=$(( $(date +%s) + POLL_TIMEOUT_S ))
EVENT_ID=""
DELIVERY_STATUS=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  HEALTH=$(auth "$API_BASE/api/worker/workflows/$WF/health")
  EVENT_PAGE=$(auth "$API_BASE/api/worker/workflows/$WF/events?limit=5")
  EVENT_ID=$(printf '%s' "$EVENT_PAGE" | "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin); print(value.get("events", [{}])[0].get("id", ""))')
  DELIVERY_STATUS=$(printf '%s' "$HEALTH" | "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin); counts=value.get("delivery_counts", {}); print("delivered" if counts.get("delivered",0)>0 else ("failed" if counts.get("failed",0)>0 else "pending"))')
  EVALUATED=$(printf '%s' "$HEALTH" | "${PYTHON_BIN}" -c 'import json,sys; value=json.load(sys.stdin); print(any(item.get("last_evaluated_at") for item in value.get("subscriptions", [])))')
  if [ "$DELIVERY_STATUS" = "failed" ]; then
    echo "worker-generated delivery failed; event=$EVENT_ID" >&2
    printf '%s\n' "$HEALTH" >&2
    exit 1
  fi
  if [ "$EVALUATED" = "True" ] && [ -n "$EVENT_ID" ] && [ "$DELIVERY_STATUS" = "delivered" ]; then
    break
  fi
  sleep 2
done
if [ -z "$EVENT_ID" ] || [ "$DELIVERY_STATUS" != "delivered" ]; then
  echo "bounded live smoke did not observe tick -> event -> delivered before timeout" >&2
  echo "Prerequisites: alerts worker running, instrument token configured, and a fresh RELIANCE tick." >&2
  exit 1
fi
echo "   worker-generated delivery accepted; event=$EVENT_ID status=$DELIVERY_STATUS"
echo "   direct channel test and tick->outbox->provider paths passed separately"

if [ "$KEEP" != "1" ]; then
  echo "== 6. archive smoke workflow"
  post "$API_BASE/api/worker/workflows/$WF/archive" -d '{}' > /dev/null
  echo "   ok (KEEP=1 to retain workflow; channel is left untouched for explicit user cleanup)"
fi

echo "DONE"
