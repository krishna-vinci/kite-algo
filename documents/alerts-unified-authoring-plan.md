# Alerts: unified authoring with live market context — implementation plan

**Design:** `documents/alerts-unified-authoring-design.md` (this plan implements
only that design). **Base:** `development` at or after `a898c3f`.

Delivery rules for every slice: forward fixes only, unsigned commits
(`-c commit.gpgsign=false`), focused tests before the commit, no push until the
whole slice set is clean and verified, `docker compose down` is never run,
`.commandcode/` and unrelated drafts are not touched, no orders and no external
notifications.

---

## Slices and commits

### 1. Design specification and implementation plan
`documents/alerts-unified-authoring-design.md`,
`documents/alerts-unified-authoring-plan.md`. No code.

### 2. Operator live-price stream (backend)
- `backend/alerts/market_stream.py`
  - `MarketStreamHub`: one process-wide Redis `market:ticks` subscription,
    token→connection fan-out, per-connection coalescing buffers, bounded send
    queues, hub health counters.
  - `OperatorMarketConnection`: auth/origin/scope gate, catalog resolution,
    subscription cap, owner registration/renewal/deletion, snapshot priming,
    freshness + session classification.
  - Freshness constants and `classify_freshness(...)`; `exchange_session_state(...)`
    (NSE calendar, documented MCX/currency windows, `unknown` when unavailable).
- `backend/api/routers/alerts_market_ws.py`: `GET /api/alerts/market/ws`
  (WebSocket), mounted with the operator routers.
- `backend/main.py` (or the router include site): mount the new router; ensure
  the app's cookie middleware does not reject the WS handshake path.
- Tests `tests/api/test_alerts_market_ws.py`: cookie auth (missing/expired),
  Origin enforcement (allowed/foreign/absent), scope refusal, catalog
  resolution (canonical only, unknown/expired refused, generation recorded),
  subscription cap, initial snapshot from `market:tick:{token}` with honest age,
  coalescing (burst → one frame per instrument), backpressure (slow client drops
  ticks, never grows unbounded), owner create/update/delete lifecycle, renewal,
  disconnect cleanup on abnormal close, freshness classification
  (LIVE/DELAYED/STALE/NO DATA/CLOSED), and "no alert_subscriptions writes".

### 3. Shared frontend market-stream client
- `frontend-next/features/alerts/lib/market-stream.ts`: single-socket client
  (refcounted registrations, dedupe, debounced diffs, bounded quote store,
  per-key notifications, backoff reconnect with replay, state machine).
- `frontend-next/features/alerts/hooks/use-market-stream.tsx`: provider +
  `useMarketQuote(key)`, `useMarketQuotes(keys)`, `useInViewport()`.
- Tests `features/alerts/lib/market-stream.test.ts` (fake WebSocket):
  dedupe/refcount, diff coalescing, replay after reconnect, per-key listener
  isolation, state transitions, no credential storage.

### 4. Unified authoring page
- `frontend-next/features/alerts/components/unified-alert-editor.tsx` (+ section
  components as needed): create and edit in one page, live price card, rule row,
  target assistance, evaluation/timeframe, notification frequency, collapsed
  timing/noise, destinations, name, sticky save bar, same-page Code view.
- Routes: `/alerts/new` renders it; `/alerts/[id]/edit` renders it populated.
- Retire `alert-wizard.tsx` and `quick-alert-composer.tsx` (delete after parity);
  update `alerts-new-page.tsx`, `alerts-edit-page.tsx`, and any test that
  referenced them.
- Tests: create and edit render, session inference, evaluation/timeframe
  visibility, target distance language, partial create/activate failure, stable
  idempotency retry, keyboard order and labels.

### 5. Plain-language settings and background validation
- `features/alerts/lib/plain-language.ts` (trigger/limit mapping, timeframe
  labels, helper text) + tests.
- `features/alerts/hooks/use-definition-validation.ts`: debounce, abort,
  incomplete-definition skip, issue mapping back to fields, state machine
  (ready / incomplete / crossed / invalid / unavailable / no-data).
- Preview interpretation using the live price; the "already above the target"
  sentence.
- Tests: debounce, stale-response cancellation, issue mapping, no raw codes,
  outage preserves the draft.

### 6. Live alert list and detail
- `alerts-list-page.tsx`: decision-shaped rows, live price + distance + state +
  destination + last checked, filters, viewport-bounded subscriptions,
  diagnostics disclosure.
- `workflow-detail-page.tsx`: price/target/distance/effective-state block, plain
  states, deliveries below, diagnostics collapsed, edit opens the unified page.
- Tests: freshness states, distance language, multi-instrument summary,
  viewport registration/cleanup, no UUID as normal content.

### 7. Browser-found correctness fixes
Whatever the deployed-browser acceptance turns up, each with a regression test.

### 8. Verification documentation
`documents/alerts-unified-authoring-verification.md` + screenshots under
`documents/verification/alerts-unified-authoring-2026-09-15/`.

---

## Configuration (all optional, names only)

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALERTS_MARKET_MAX_INSTRUMENTS` | 25 | per-connection subscription cap |
| `ALERTS_MARKET_FLUSH_MS` | 250 | tick coalescing interval |
| `ALERTS_MARKET_QUEUE_MAX` | 256 | per-connection frame queue bound |
| `ALERTS_MARKET_LIVE_MAX_S` | 5 | `LIVE` freshness bound |
| `ALERTS_MARKET_DELAYED_MAX_S` | 30 | `DELAYED` freshness bound |
| `ALERTS_MARKET_OWNER_RENEW_S` | 20 | runtime owner re-`PUT` interval |
| `ALERTS_MARKET_IDLE_TIMEOUT_S` | 300 | idle connection close |

---

## Tests to run before the final push

- Backend focused: `tests/api` (alerts operator, alerts market ws, worker
  alerts), `tests/workflows`, `tests/alerts`, `tests/notifications`.
- Disposable PostgreSQL suites that touch alerts/subscriptions/catalog.
- Frontend: `NODE_ENV=test npx vitest run` (focused alerts suites plus the full
  run), `npx tsc --noEmit`, `npx eslint` on changed areas, `npm run build`.
- `git diff --check`.
- Go market-runtime: only if Go code changes (this slice does not plan to).

## Browser acceptance (deployed application, real LAN origin)

The 24-step list in the request is executed in order against the deployed stack,
with screenshots for: unified creation page with live LTP, a background
validation issue, plain-language notification controls, the alert list with live
LTP and distance, the detail page with state and freshness, same-page Code view,
narrow layout, and a stale/market-closed presentation. Subscription cleanup is
verified by inspecting the market-runtime owner list (before/after) and by
counting the runtime's effective tokens.

## Rollout

Rebuild and recreate only `finance-app` and `frontend-next`, record image IDs and
health, then push `development` after everything is clean and verified.
