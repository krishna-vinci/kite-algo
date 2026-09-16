# Alerts: unified authoring with live market context — verification

Date: 2026-09-16. Branch `development`. Deployment: the real LAN origin
`http://192.168.0.128:13000` (non-secure origin, exercising the cookie/Origin
path), headless Chrome driven over CDP, services rebuilt only where they changed
(`finance-app`, `frontend-next`).

## 1. What the acceptance proved end to end

| Step | Result |
| --- | --- |
| Open New alert | One page: instrument, rule, evaluation, frequency, destinations, name, sticky save bar, `Form`/`Code view` tabs. No stepper, no `?mode=advanced` product. |
| Search and select an instrument | Canonical key selected (`MCX:CRUDEOIL26DECFUT`), live price card appears immediately. |
| Live LTP with timestamps | `₹8,745.00 · LIVE · updated just now` plus exchange/ receipt stamps ("no exchange timestamp · received 10:39:06 AM" — the LTP feed omits exchange time, which is reported honestly rather than invented). |
| Target shortcuts use the visible price | `Use current price`, `+0.5%`, `+1%`, `-0.5%` present; +1% on 124,860 produced 126,108.6. |
| Session inferred | "Evaluated in the MCX commodities session, taken from the instrument's exchange." No session control anywhere on the page. |
| Evaluation switch | `Live price` ⇄ `Completed candle`; the timeframe control appears only for a candle evaluation or a percentage rule, labelled "15 minutes". |
| Multiple conditions | `All conditions and groups` disclosure keeps the full condition editor (AND/OR/NOT) on the page. |
| Frequency options | Three plain choices; a new alert defaults to "Once when it happens". |
| Timing/noise controls | Collapsed section with cooldown, daily cap, rearm, consecutive candles, "already true" — all on the same page. |
| Background validation | `VALID` appeared with no Validate button; before the value was entered the bar read `WAITING FOR THE REQUIRED FIELDS` (an incomplete definition is never sent). |
| Background preview | "With the current price as the sample, this alert would not fire yet." and, for a level already crossed, "Price is already above ₹8,700.00. This alert will wait for the price to move below the target and cross it again." |
| Save draft | Banner: "Draft saved — Nothing is evaluating yet." |
| Create and activate | Navigated to the workflow; `ACTIVE`, destination stored, subscription created. |
| Retry without duplicate | `Save draft` then `Create and activate` on the same page (same idempotency key): exactly **one** workflow with that name existed afterwards. |
| Edit on the same page | `/alerts/{id}/edit` populated the same editor (target `99999`, generated name, destination checked) with the live price on screen. |
| Code view | Same page, `YAML`/`JSON` with `Validate` and `Save as new draft revision`; a definition the form cannot represent opens here with the reason shown. |
| List | Row = rule as title ("MCX:SILVER26DEC185000CE crosses above ₹9,99,999.00"), live price `₹53,788.50 · LIVE`, distance (`₹9,46,210.50 below target`), state (`WAITING FOR THE FIRST CROSSING` / `WATCHING`), destination, last checked, Pause/Resume + Edit + Details. |
| Detail | Header: `₹370.30 · LIVE · updated just now · watching MCX:NATURALGAS26DECFUT crosses above ₹99,999.00 · Target is ₹99,628.70 above the current price (+26904.86%). · WATCHING`, with the revision/UUID behind "Technical details". |
| Narrow layouts | 390×844 on the list and the detail page: `scrollWidth == innerWidth` (no horizontal overflow). |
| Reconnect | The backend was restarted while the list was open: the socket closed, the client retried with backoff (4 failed attempts), then reopened and replayed both instruments — live price resumed. |
| One broker connection | `GET /internal/market-runtime/status`: `status: healthy`, **shards: 1 (connected)**, `owners: 1` with every alerts page closed (only the alerts-worker's owner). |
| Subscription cleanup | With the list open the runtime reported **owners: 2**; after navigating away and closing every alerts page it returned to **1**. The page also sent exactly one `subscribe` frame containing both visible instruments (dedup + diff coalescing). |

Not produced live: **MARKET CLOSED** and **STALE** presentations, because NSE,
MCX and currency were all trading during the acceptance window. Those states are
covered by the classification tests (including "a closed market outranks age")
and by the fact that the same vocabulary drives the badge the browser rendered for
LIVE and NO DATA; claiming a live closed-market screenshot would require either
waiting for the session to close or faking the clock.

## 2. Defects the browser found (all fixed with regression tests)

1. **Owner registration failed in production**: the market-runtime client accessor
   is a coroutine and the stream never awaited it, so every subscription owner was
   lost with `'coroutine' object has no attribute 'set_owner_subscriptions'`. The
   injected factory the unit tests use hid it; there is now a test that goes
   through the production accessor.
2. **Generated name never reached the document**: validation reported
   `document.name: must be a non-empty string, got ''` for a name visible on
   screen, and the save would have stored an empty name.
3. **Preview sample was dropped** as a consequence: with the document invalid the
   server never evaluated, so the editor said "Not enough data to preview" while a
   live price was on screen.
4. **Preselected destination was never saved**: the single enabled channel was
   rendered checked but not written into the draft, so the created alert stored
   `channels: []` — a "complete-looking" form produced an alert that could not
   notify.
5. **Detail reported `subscription_count: 0`** for every alert (the list
   placeholder was returned unchanged), so an armed alert read as "never
   evaluated" on its own page and never reached `WATCHING`.
6. **List carried no effective lifecycle**, so a paused workflow would have shown
   Active and offered Pause again.
7. **Session sentence** rendered as "mcx commoditiessession" (JSX whitespace) and
   in the previous build as a partly raw code.

## 3. Screenshots

`01-unified-create-live` (unified page + live LTP), `02-created-activated`,
`03-list-live`, `04-detail-live`, `05-code-view`, `06-list-narrow`,
`07-already-past-level`, `08-detail-narrow`, `09-plain-language-controls`.

## 4. Tests and builds

| Check | Result |
| --- | --- |
| `pytest tests/api tests/alerts tests/workflows tests/notifications` | **1093 passed**, 20 failed — identical set to the recorded pre-existing baseline (`ModuleNotFoundError: backend.app.runtime_public_config` and the `OptionRunCreateRequest` drift), verified by diff |
| `pytest tests/api/test_alerts_market_ws.py` | 24 passed (handshake policy, catalog resolution, cap, snapshots, coalescing, backpressure, freshness/session vocabulary, owner lifecycle + cleanup, the production-accessor regression) |
| `pytest tests/api/test_alerts_operator.py` | 47 passed (incl. the list rule summary, list lifecycle transitions, detail subscription/freshness) |
| `NODE_ENV=test npx vitest run` (frontend) | **350 passed**, 2 failed — the pre-existing strategies reference-page tests that look for older copy (they fail with these changes stashed too) |
| `npx tsc --noEmit` | clean |
| `npx eslint features/alerts components/operator` | clean |
| `npm run build` | compiled successfully |
| `git diff --check` | clean |

## 5. Deployment and cleanup

- Rebuilt and recreated only `finance-app` and `frontend-next`; both reported
  healthy after each rollout. `kite-app` image
  `sha256:28aa8a9de25b…` → rebuilt with the detail-freshness fix;
  `kite-frontend-next` `sha256:030a85705076…` + the channels/name fixes.
- No `docker compose down`, no force-push, no global Git change, `.commandcode/`
  and the unrelated proposal draft untouched.
- Temporary workflows created during acceptance (`SILVER26DEC185000CE crosses
  above 9,99,999`, `NATURALGAS26DECFUT crosses above 99,999`) were archived at
  the end; each was created with a level far above the market so no alert could
  fire, and no notification was sent at any point (the delivery counts in the
  alerts worker health stayed at zero for the whole session).
- Runtime owners verified back to the single worker owner after closing the pages.

## 6. Remaining usability limitations

- Advanced structured constructs (sequences, breadth, pair/external operands,
  multi-stage documents) are still authored in Code view; the structured form
  routes them there deliberately rather than describing them loosely.
- The preview evaluates a single live sample, not a historical series: it explains
  what would happen *now*, and says so.
- Universe alerts cannot show a single price by design; their detail page lists
  bounded member prices only when the members are resolved.
- The timeframe selector lists the capability payload's timeframes; a deployment
  that supports only the daily bar therefore offers a shorter list.
