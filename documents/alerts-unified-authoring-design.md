# Alerts: unified authoring with live market context — design

**Status:** DESIGN (approved requirements). Implementation follows this document;
where source and this document disagree, current source wins and the document is
corrected in the same slice.

**Base:** `development` at or after `a898c3f`.

**Scope guard:** alerts only — creation, editing, list, detail, and the
operator-authenticated live-price stream behind them. Screeners are not
redesigned; shared components change only where a regression would otherwise
follow. No order placement, no hosted trade capability, no real notifications,
no provider changes, no scheduling.

---

## 0. What is being replaced, and why

The current product has **two competing creation paths**:

| Path | Where | Problem |
| --- | --- | --- |
| `QuickAlertComposer` | `/alerts/new` | A second, weaker editor: it covers one condition and hides everything else behind "use the advanced editor". |
| `AlertWizard` (7-step stepper) | `/alerts/new?mode=advanced`, `/alerts/[id]/edit` | Asks for clock, session, trigger and limits as separate screens, in backend vocabulary, before the user has seen a price. |

Two products means two places to fix, two mental models, and a user who must
guess which one they are in. Both are removed: **one authoring page** serves
create and edit, keeps the common path at the top, discloses the uncommon
settings in place, and exposes a lossless Code view on the same page.

The canvas remains available as an optional visualization but is not part of the
creation path.

---

## 1. Live-price architecture

### 1.1 One broker connection, owned by the Go market-runtime

```
Kite WS ──> market-runtime (Go)  ──publish──> Redis "market:ticks"
                 ▲                                 │
                 │ PUT/DELETE subscriptions/{owner}│
                 │ (per browser connection)        ▼
        finance-app (Python)  ──cookie WS──>  browser
```

The browser never touches the broker, the runtime's HTTP control API, or Redis.
The Python process **does not open a second broker WebSocket**: it registers a
*subscription owner* with the running market-runtime, which already owns the one
Kite connection, and then reads the ticks the runtime publishes to Redis
(`market:ticks`, plus the TTL'd `market:tick:{token}` last-value keys).

Reused as-is:
- `InstrumentCatalog.resolve_public_key` for canonical identity → current
  broker token + catalog generation (never a bare symbol, never a stale token);
- `MarketRuntimeClient.set_owner_subscriptions` / `delete_owner`;
- Redis `market:ticks` and `market:tick:{token}`;
- the app's cookie/session authority (`app_access_token`) and the CORS
  allowlist that `enforce_same_origin` already uses.

### 1.2 Route and authentication

```
WS  /api/alerts/market/ws?scope=<app-scope>
```

- **Cookie session.** The access cookie is decoded with the existing app auth
  code path. No cookie → close `4401`.
- **Origin.** The `Origin` header must be in the configured application origins
  (`get_allowed_cors_origins()`), because cookies ride along on a WS handshake
  exactly as they do on an unsafe HTTP method. Missing/foreign Origin → close
  `4403`.
- **Scope.** The requested scope is resolved through the same server-side
  allowlist the operator HTTP routes use (client selection, server authority).
  An unauthorized scope → close `4403` with a generic reason; foreign resources
  are never named.
- The browser can never choose its own runtime owner id: the server derives it.

### 1.3 Protocol

Client → server:

```json
{"type": "subscribe",   "instruments": ["MCX:CRUDEOIL26DECFUT", "NSE:RELIANCE"]}
{"type": "unsubscribe", "instruments": ["NSE:RELIANCE"]}
{"type": "ping"}
{"type": "status"}                      // ask for a fresh state/heartbeat
```

Server → client (every frame carries `type`; ticks carry the quote fields):

```json
{"type": "welcome",  "connection_id": "...", "scope": "app:admin",
 "limits": {"max_instruments": 25, "flush_ms": 250},
 "runtime": {"status": "healthy", "websocket_state": "CONNECTED"},
 "server_time": "..."}

{"type": "quote", "instrument_key": "MCX:CRUDEOIL26DECFUT",
 "broker_token": 147523591, "catalog_generation": "...",
 "last_price": 8704.0, "change_absolute": -86.0, "change_percent": -0.98,
 "exchange_timestamp": "...", "received_at": "...", "server_time": "...",
 "age_ms": 412, "session_state": "open", "freshness": "LIVE",
 "origin": "snapshot" | "tick"}

{"type": "state", "instruments": { "MCX:...": {"freshness": "STALE", ...} },
 "runtime": {...}, "session_states": {"NSE": "closed"}}

{"type": "error", "code": "INSTRUMENT_UNKNOWN", "instrument": "NSE:NOPE",
 "message": "..."}

{"type": "heartbeat", "server_time": "...", "runtime": {...}}
```

### 1.4 Freshness vocabulary (single source of truth)

| Value | Meaning | Rule (server-computed) |
| --- | --- | --- |
| `LIVE` | current session, current price | tick age ≤ `ALERTS_MARKET_LIVE_MAX_S` (5s) **and** session open |
| `DELAYED` | recent but not current | age ≤ `ALERTS_MARKET_DELAYED_MAX_S` (30s) |
| `STALE` | old | age > delayed bound |
| `MARKET CLOSED` | session is closed | session state `closed` (calendar for NSE, documented window for MCX/currency) |
| `NO DATA` | nothing cached and nothing streamed | no tick for the key |
| `RECONNECTING` | client-side only | the browser lost the socket and is backing off |

A cached row is never labelled `LIVE`: the first frame for a key is a
`snapshot` and is classified with its real `age_ms`. `RECONNECTING` is a client
state; the server never claims it.

### 1.5 Bounding and backpressure

- **Per connection:** at most `ALERTS_MARKET_MAX_INSTRUMENTS` (25) canonical
  instruments; extra keys are refused with a typed error, never silently
  dropped.
- **Coalescing:** incoming ticks are accumulated per connection in a
  `{token: quote}` map and flushed on a fixed interval
  (`ALERTS_MARKET_FLUSH_MS`, 250ms) with the newest value per instrument, so an
  exchange burst becomes one frame per instrument per interval.
- **Bounded queues:** each connection has a bounded send queue
  (`ALERTS_MARKET_QUEUE_MAX`, 256 frames). When it is full the connection is
  marked `backpressure` and *new tick frames are dropped* (the next flush sends
  the newest value anyway) — the hub never awaits a slow client and never grows
  without limit. Control frames (`error`, `state`, `heartbeat`) are allowed to
  evict tick frames, never the other way round.
- **Idle:** a connection with no subscribe activity for
  `ALERTS_MARKET_IDLE_TIMEOUT_S` (300s) and no heartbeat reply is closed.

### 1.6 Owner lifecycle

- Owner id: `alerts-ui:{scope}:{uuid4}` — unique per browser connection.
- Created on the first `subscribe` (`PUT` with mode `ltp`), updated on later
  changes (single `PUT` of the complete token set), deleted on disconnect.
- A renewal task re-`PUT`s the same set every
  `ALERTS_MARKET_OWNER_RENEW_S` (20s) so the runtime's owner lease
  (`MARKET_RUNTIME_OWNER_LEASE_TTL_SEC`, 90s) never expires a live connection.
- Deletion runs in a `finally` for both clean closes and abnormal disconnects
  (client vanish, server cancel, protocol error), so the runtime stops
  subscribing promptly. The runtime's own TTL sweep remains the second safety
  mechanism for the crash case the process cannot handle.
- UI-only subscriptions are **never** written to `alert_subscriptions`; they are
  not evaluation state and must not look like it.

### 1.7 Session state

`session_state` per exchange:

- **NSE** — from the imported verified calendar (`get_calendar_sessions` for
  today): open between `opens_at` and `closes_at`, closed otherwise, `unknown`
  when the calendar is unavailable or has no row for today (never inferred).
- **MCX / currency** — documented session windows (the same ones the candle
  finality rule uses), state `open` inside the window and `closed` outside;
  no holiday calendar exists for them, and the UI says "market closed" only on
  the window, never on a guess.

---

## 2. Frontend market-stream client

One socket for the whole alerts area, owned by a module-level client + a React
provider:

- `register(key) → unregister()` — components declare what they currently need;
  registrations are reference-counted and deduplicated, so five rows wanting the
  same key produce one subscribe and one unsubscribe.
- Diffs are debounced (~50ms) and coalesced into one `subscribe`/`unsubscribe`.
- **One socket**, reconnecting with bounded exponential backoff (500ms → 8s,
  jittered) and replaying the outstanding registration set on reopen.
- Latest normalized quote per key, in a store that notifies **per key**
  (`subscribeKey(key, listener)`), so a tick for one row cannot rerender the
  list.
- Connection state is exposed (`connecting | live | reconnecting | closed`), and
  the UI labels it `RECONNECTING` rather than showing a stale price as live.
- No credentials, tokens or internal identifiers are stored client-side.

**Viewport bounding:** list rows register through an `IntersectionObserver`
hook, so only visible rows hold subscriptions; a row scrolled out of view
unsubscribes. The detail page registers only its own instruments, bounded by the
server cap.

**Multi-instrument and universe alerts** never show a single price as "the"
price: the list shows the member count and a feed summary; the detail page shows
a bounded list of visible member prices with per-member freshness.

---

## 3. Unified authoring page

One component, used by `/alerts/new` and `/alerts/[id]/edit`
(`features/alerts/components/unified-alert-editor.tsx`). Layout, top to bottom:

1. **Instrument** — canonical search/select. Live price card appears immediately
   after selection: `₹124,860.00  LIVE  updated 1s ago`, exchange and receipt
   timestamps on hover/expanded detail.
2. **Alert me when** — the rule in one line: `[Live price] [crosses above] [₹125,000]`,
   with the existing `ConditionEditor` below for conditions beyond the first.
3. **Target assistance** — `Target is ₹140 above the current price (+0.11%)`,
   `[Use current price] [+0.5%] [+1%] [-0.5%]`. Never moves a target the user
   typed; only explains it.
4. **Evaluation** — `Live price` / `Completed candle`; the timeframe control
   appears only when the evaluation or a chosen condition needs one, labelled
   `1 minute`, `15 minutes`, `1 day`.
5. **Notifications frequency** — plain language (see §5).
6. **More timing and noise controls** — collapsed by default.
7. **Notify via** — destinations, immediately above the save area.
8. **Name** — generated, editable.
9. **Save area** — sticky: `Save draft`, `Create and activate` (new) /
   `Save changes`, `Save and activate latest` (edit).
10. **Code view** — a tab on the same page; lossless YAML/JSON with
    `Format`, `Validate`, `Save as new draft revision`, and an explicit
    explanation when the structured form cannot represent a stored construct.

Sessions are inferred from the selected instrument's exchange, and are not a
user-facing control; ambiguous inference produces an actionable inline error.
Raw session codes appear only in Code view. Timeframes, clocks, triggers and
limits are never presented as backend identifiers.

---

## 4. Background validation and preview

- Validation runs automatically after meaningful changes: `debounce` 600ms,
  `AbortController` cancellation of superseded requests (stale responses never
  apply), and skipped entirely while the definition is obviously incomplete
  (no instrument, no value, no channel).
- The server stays authoritative: the same `POST /api/alerts/workflows/validate`
  contract is used, and no client-side acceptance replaces it.
- Issues are mapped back to their field/section; raw issue codes and raw server
  objects are never rendered. A concise unresolved-issue summary sits next to the
  save controls.
- **Preview** (side-effect free, existing contract) runs on the same debounce
  and is interpreted into language, using the live price as the observation
  context when available:
  > Price is already above ₹125,000. This crossing alert will wait for the price
  > to move below the target and cross it again.
- Six UI states are distinguished: *valid and ready*, *waiting for required
  input*, *valid but currently beyond a crossing level*, *invalid*, *validation
  service unavailable*, *stale/no market data*. A validation outage never
  clears the draft.

---

## 5. Plain-language settings

| Backend concept | User-facing wording |
| --- | --- |
| `trigger` | When should we notify you? |
| `once` | Once when it happens |
| `repeated crossing` (`on_transition`) | Every time it happens again |
| reminder (`reminder_interval_s`) | Remind me while it remains true |
| `rearm_level` / `rearm_direction` | Ready the alert again after… |
| `cooldown_s` | Wait before notifying again |
| `max_per_session` | Maximum notifications today |
| `notify_if_already_true` | Tell me if it is already true when I switch it on |
| hysteresis | Avoid repeated alerts near the target |
| `ltp` | Live price |
| `candle_close` | Completed candle |
| `consecutive_bars` | Consecutive completed candles |

Visible by default: *Once when it happens*, *Every time it happens again*,
*Remind me while true*. Everything else lives under **More timing and noise
controls**, which stays on the page and explains outcomes rather than backend
vocabulary. Defaults are safe for a first-time user (once, no cooldown, no cap,
no rearm).

---

## 6. Notifications, save controls, honesty

- Destinations show provider + friendly name; credentials and internal channel
  ids are never rendered. A single enabled destination is preselected; none
  produces a link to add one **without** losing the draft.
- The UI states that provider acceptance is not proof of human receipt, and
  creation never sends a test notification.
- Partial success is explicit: create-and-activate that fails at activation says
  the **draft was saved**, links to it, and keeps every entered value.
- Idempotency: one key per creation attempt, reused on retry, regenerated only
  for a genuinely new alert; the submit button is disabled while in flight.
- 409 concurrency conflicts keep the operator's input and explain the choice
  (reload or save as a new revision).

---

## 7. List and detail as monitoring surfaces

**List** — one row per alert, decision-shaped:

```
GOLD OCT crosses above ₹125,000
₹124,860 LIVE · ₹140 below target
Active · Telegram · checked 1s ago
                                   Pause  Edit
```

plus filters (Active / Paused / Draft / Needs attention / Archived / instrument
search) and an expandable diagnostics area for revisions, hashes, subscription
ids and raw lifecycle codes. Multi-instrument and universe alerts show member
count and a feed summary instead of a single price. No raw UUID is normal
content.

**Detail** — current price, target and distance, and the effective state near
the top; plain states (`Watching`, `Waiting for the first crossing`, `Waiting to
reset`, `Paused`, `Market data stale`, `Market closed`, `Needs attention`); event
and delivery history below; diagnostics collapsed. Editing opens the same
unified page populated from the stored document.

---

## 8. Preserved contracts (must not regress)

Server-authoritative capabilities; catalog-backed identity; session/exchange
compatibility; `expected_revision` concurrency; no-op edit hash preservation;
409 recovery without input loss; activation guard and silence contract;
side-effect-free preview; owner/scope isolation; advanced-document losslessness
(structured edits merge onto the stored document, and unmodeled constructs route
to Code view); no order capability anywhere in alerts. Tests are not weakened to
make the UI simpler.

---

## 9. Non-goals

Screeners (except shared-component regressions), orders, hosted trade
capability, real notification delivery or provider changes, scheduling changes,
a second broker connection, and any client-side re-implementation of validation
or ranking.
