# Alerts operator UI — frontend handoff (Phase 6)

**Audience:** the frontend agent implementing the alerts operator UI in `frontend-next/`.
**Backend status:** implemented, tested, and merged on branch `development`.
**Backend owner:** this document is the contract. If a route behaves differently from
what is written here, the route is wrong and the backend should be fixed — do not code
around it.

## How to read this document

Every claim carries a marker:

| Marker | Meaning |
| --- | --- |
| **VERIFIED** | I executed this against the backend in this session and it behaved as described. There is a test pinning it. |
| **PROPOSED** | Not built. A design decision for you (or a later phase) to make. |

Nothing in this document is a guess about the UI's appearance. **Visual styling is
entirely yours.** Where this document mentions a component or a layout it is naming an
existing convention you may follow or replace.

Do not create a second frontend application. Everything here extends `frontend-next/`.

---

## 1. What exists on the backend right now

**VERIFIED.** `/api/alerts/*` is a new app-cookie-authenticated router family mounted at
`/api`. 42 routes. It is additive: the existing worker surface at
`/api/worker/*` and `/api/algo-workers/*` is unchanged, so the SDK/MCP contracts did not
move.

Backend commits on this assignment:

| Commit | Contents |
| --- | --- |
| `c2f2ca0` | Canvas layout storage, migration `20260912_000018`, namespaced node identities |
| `df7bb29` | Operator health, scope-aligned tokens, revision-addressed YAML, `schema.sql` mirrors |
| `8112ecc` | Operator universes, screeners with attachment baselines, external producers |
| `e422078` | (earlier, same phase) Operator API: workflows CRUD, deliveries + attempts, channels, instrument search, YAML renderer, operator auth |
| `4d91792`, `d7d5193`, `2ba510f` | (earlier) LTP freshness, failure isolation, level-vs-crossing validation |

Migrations: `20260912_000017` (`delivery_attempts.provider_id`, nullable) and
`20260912_000018` (`workflow_canvas_layout`). Both additive, both applied from
zero-to-head on real PostgreSQL, both mirrored in `backend/schema.sql`.

Test evidence: 975 pass in `tests/{workflows,alerts,screeners,notifications,api}`
(20 pre-existing failures, unchanged, in four unrelated auth/control-plane modules —
they are not caused by this work and are not yours to fix).

---

## 2. Existing frontend architecture and conventions

**VERIFIED** by reading the tree. Follow these; they are load-bearing.

### Framing

| Concern | Convention | File |
| --- | --- | --- |
| Framework | Next 16 App Router, React 19 | — |
| Routing | authenticated chrome lives in route group `(app)` | `app/(app)/*/page.tsx` |
| Edge gate | `middleware.ts` requires a session cookie for everything except `/login`, `/_next`, `/api`, `/ws` | `middleware.ts` |
| Auth guard | `(app)` layout calls `fetchTradingRuntimeStatus()`, redirects to `/login?next=` when `appAuthenticated` is false | `app/(app)/layout.tsx` |
| Fetch | `apiFetch<T>` — `credentials: "include"`, one deduped `/api/auth/refresh` retry on 401, typed `ApiClientError` | `lib/api/client.ts` |
| Proxying | Browser uses **relative** paths; `rewrites()` sends `/api/:path*` → `BACKEND_INTERNAL_URL` (default `http://localhost:18777`) | `next.config.ts` |
| Server state | TanStack Query 5, `staleTime 30s`, `retry 1`; mutations invalidate by key | `lib/query/client.ts`, `features/trading/hooks/*` |
| Nav | `navigation[]` array + icon map feeds the left rail | `lib/navigation.ts`, `components/left-rail.tsx` |
| Notifications | `sonner` `toast.*`, mounted once in `app/providers.tsx` | — |
| Styling | Tailwind v4 CSS-first (**no `tailwind.config`**), CSS vars `--bg/--panel/--text/--muted/--accent/--green/--red/--border` | `app/globals.css` |
| Tests | Vitest + Testing Library, co-located `*.test.tsx` | `vitest.config.ts` |

### Existing primitives you can use

`components/ui/`: `alert`, `badge`, `breadcrumb`, `button`, `calendar`, `card`, `chart`,
`popover`, `select`, `separator`, `skeleton`, `table`, `tabs`, `textarea`, `toggle`,
`toggle-group`, `tooltip`.

`components/operator/`: `kpi-card`, `panel`, `section-label`, `status-badge`.

### Primitives that do NOT exist yet

**VERIFIED.** You will need to add: `dialog`, `sheet`, `input`, `label`, `checkbox`,
`switch`, and some stepper/field composition. `radix-ui` (the unified package, v1.4.3) is
already a dependency, so this adds **no new dependency**. `components.json` pins style
`new-york`.

There is **no form library** (no react-hook-form, no zod). The existing pattern is
controlled state plus explicit save — see `app/(app)/journal/…`.

### Closest existing models to copy

- `components/settings/algo-worker-access-panel.tsx` — create + list + **one-time secret
  reveal**, with explicit empty/loading/error states. This is the closest analogue to the
  token and producer-credential panels.
- `app/(app)/journal/…` — list → detail → `PATCH` with an explicit save action.
- `features/trading/components/risk-adjustment-sheet.tsx` — hand-rolled modal (useful if
  you would rather not add Radix `Dialog`).

### Feature module shape

Mirror `features/trading/`:

```
features/alerts/
  api.ts          # apiFetch wrappers, one function per endpoint
  types.ts        # response types (see §5 for shapes)
  hooks/          # useQuery / useMutation wrappers, invalidation keys
  components/     # page-level pieces
  lib/            # pure helpers (validation, formatting) — unit-testable
```

---

## 3. Pages, flows, and milestones

**PROPOSED.** The backend supports these; the page structure is yours to confirm.
Routes follow the plan and the existing `(app)` convention.

| Route | Purpose |
| --- | --- |
| `app/(app)/alerts/page.tsx` | list: name, kind, lifecycle, instruments/universe summary, freshness, warnings badge |
| `app/(app)/alerts/new/page.tsx` | structured creation flow |
| `app/(app)/alerts/[workflowId]/page.tsx` | detail: definition (readable + YAML), lifecycle, health, events, deliveries, revisions/rollback |
| `app/(app)/alerts/[workflowId]/edit/page.tsx` | edit → new draft revision with `expected_revision` |
| `app/(app)/alerts/[workflowId]/canvas/page.tsx` | visual canvas (milestone 6B) |
| `app/(app)/alerts/universes/page.tsx`, `.../universes/[name]/page.tsx` | universes and membership |
| `app/(app)/alerts/screeners/[workflowId]/page.tsx` | screener runs and attachment baselines |
| `app/(app)/alerts/operations/page.tsx` | channels, tokens, producers, platform health |

Add `Alerts` to `lib/navigation.ts` and whatever icon map `components/left-rail.tsx` uses.

### Suggested milestones

Each is independently reviewable and demoable.

- **M1 — list and detail (read-only).** List with warnings, detail with readable
  definition and YAML tab, events, health. No mutations. Proves the auth, scope, and
  error plumbing end to end.
- **M2 — lifecycle.** Activate, pause, resume, archive, revision rollback. This is where
  you meet the revision-conflict flow.
- **M3 — structured authoring.** The creation/edit flow driven entirely by
  `/capabilities`, with validate and preview. The largest milestone.
- **M4 — operations.** Channels, tokens, producers, one-time reveals, platform health.
- **M5 — screens and universes.** Screener runs, attachment baselines, universe
  membership.
- **M6 — canvas.** Layout persistence and the visual editor (§12).

M1 before M3 on purpose: the authoring flow cannot be debugged without a working detail
page to inspect the result.

---

## 4. Endpoint map — every user action

**VERIFIED** for all 42 routes: they mount, they are cookie-gated, and they are
owner-scoped. Base path is `/api/alerts`. All responses are JSON.

### Scopes and discovery

| User action | Call |
| --- | --- |
| Learn which scopes I may read | `GET /scopes` |
| Populate every picker and limit in the UI | `GET /capabilities` |
| Search instruments by symbol or name | `GET /instruments/search?q=REL&exchange=NSE&limit=20` |

### Workflows

| User action | Call |
| --- | --- |
| List alerts | `GET /workflows?include_archived=false` |
| Open one alert | `GET /workflows/{workflow_id}` |
| Validate without saving | `POST /workflows/validate` |
| Dry-run against supplied samples | `POST /workflows/preview` |
| Save as draft | `POST /workflows` |
| Save an edit as a new draft | `PATCH /workflows/{workflow_id}` |
| Switch on / roll back to a revision | `POST /workflows/{workflow_id}/activate?revision=N` |
| Pause | `POST /workflows/{workflow_id}/pause` |
| Resume | `POST /workflows/{workflow_id}/resume` |
| Archive | `POST /workflows/{workflow_id}/archive` |
| Read the definition as YAML | `GET /workflows/{workflow_id}/yaml?revision=N` |
| Export document + YAML | `GET /workflows/{workflow_id}/export?revision=N` |
| See signal history | `GET /workflows/{workflow_id}/events?limit=50&offset=0` |
| See whether it is actually receiving data | `GET /workflows/{workflow_id}/health` |

### Deliveries

| User action | Call |
| --- | --- |
| "Did my alert go out?" | `GET /workflows/{workflow_id}/deliveries?limit=50&offset=0&status=failed` |
| Per-attempt detail for one delivery | `GET /deliveries/{delivery_id}/attempts` |

### Channels

| User action | Call |
| --- | --- |
| List destinations | `GET /channels` |
| Add or update a destination | `POST /channels` |
| Send a real test message | `POST /channels/{channel_id}/test` |

### Tokens

| User action | Call |
| --- | --- |
| List tokens with their scope | `GET /tokens` |
| Read the preset definitions | `GET /tokens/presets` |
| Mint a token (one-time reveal) | `POST /tokens` |
| Revoke | `POST /tokens/{token_id}/revoke` |

### Platform health

| User action | Call |
| --- | --- |
| Worker quarantine / task liveness | `GET /health` |

### Universes

| User action | Call |
| --- | --- |
| List universes | `GET /universes` |
| Create | `POST /universes` |
| Preview membership without persisting | `POST /universes/preview` |
| Inspect one | `GET /universes/{name}` |
| Revision history | `GET /universes/{name}/revisions?limit=50` |
| Resolve and persist a revision | `POST /universes/{name}/resolve` |

### Screeners

A screener **is** a workflow with a `screener` block. `{workflow_id}` is the workflow id.

| User action | Call |
| --- | --- |
| Run history | `GET /screeners/{workflow_id}/runs?limit=20&offset=0` |
| One run with ranked members | `GET /screener-runs/{run_id}?limit=100&offset=0` |
| Trigger a run now | `POST /screeners/{workflow_id}/runs?idempotency_key=...` |
| Attachment events | `GET /screeners/{workflow_id}/events?limit=50&offset=0` |
| **Attachment baselines** | `GET /screeners/{workflow_id}/attachments?revision=N` |
| Dry-run a screener | `POST /screeners/preview` |

### External producers

| User action | Call |
| --- | --- |
| List producers | `GET /signals/producers` |
| Register | `POST /signals/producers` |
| Inspect | `GET /signals/producers/{name}` |
| Revoke | `POST /signals/producers/{name}/revoke` |
| Issue a credential (one-time reveal) | `POST /signals/producers/{name}/credentials` |
| Revoke a credential | `POST /signals/producers/{name}/credentials/{token_id}/revoke` |
| Value history | `GET /signals/values?producer={name}&limit=50&offset=0` |
| Producer counters and limits | `GET /signals/health` |

### Canvas

| User action | Call |
| --- | --- |
| Load saved positions | `GET /workflows/{workflow_id}/layout` |
| Save moved nodes | `PUT /workflows/{workflow_id}/layout` |
| Forget nodes that left the document | `POST /workflows/{workflow_id}/layout/delete` |

### Deliberately NOT available (do not ask for them)

**VERIFIED by test.** The operator surface has **no** `POST /signals/values` (submitting
a value needs a producer credential, not a browser session) and **no**
`signals/health?purge` (a GET that deletes rows). The health response says
`purge_available: false` rather than ignoring the parameter.

---

## 5. Request/response shapes, pagination, errors, conflicts

### Conventions

Reads return an `ok: true` envelope plus the payload. Mutations return `ok: true` plus
what changed. Most endpoints include a `note` string. **Read the `note`.** It is written
to be shown to an operator and it explains the semantic caveat specific to that payload
(e.g. that a delivery status means provider acceptance, not human receipt). Rendering it
as a tooltip or an info line is cheap and prevents most misunderstandings.

### The list `freshness` block

**VERIFIED.** Every row from `GET /workflows` carries a `freshness` object:

```json
{
  "last_evaluated_at": "2026-09-11T12:00:00+00:00",
  "evaluation_age_s": 4.2,
  "subscription_count": 3,
  "stale_subscriptions": 1,
  "stale": false,
  "stale_after_seconds": 300
}
```

It is computed in one aggregate query for the whole page, from
`evaluation_checkpoints.updated_at` — which advances on **every** accepted observation,
including suppressed ones. That is what makes it a usable freshness signal rather than
a liveness guess.

`stale` is **tri-state, and the third state matters**:

| Value | Meaning |
| --- | --- |
| `true` | the newest checkpoint is older than `stale_after_seconds`; every subscription is at least that old |
| `false` | the workflow evaluated recently |
| `null` | **no evaluation has EVER happened** — either no subscriptions exist, or it was just activated |

`null` is not "fresh" and it is not "stale". It is the just-activated state, and the UI
must render it as "waiting for first evaluation". Reporting `false` here would tell an
operator the feed is fine when nothing has ever been processed.

The detail endpoint's `stale_reason` (§8) is the per-subscription refinement of this:
the list gives you a page-wide signal, the detail gives you the reason.

### Pagination

Two shapes exist; both are stable.

- **Offset/limit with a total:** `events` returns `{limit, offset, total, events: []}`;
  `signals/values` returns `{limit, offset, total, values: []}`.
- **Offset/limit without a total:** `deliveries` returns `{limit, offset, deliveries: []}`.
  `screener-runs/{run_id}` returns `{member_count, limit, offset, members: []}`, where
  `member_count` is the full count and `members` is the page. `screeners/{id}/runs`
  returns `{limit, offset, runs}` with no total.

Bounds are enforced server-side: `limit` is clamped by `Query(..., ge=1, le=N)`, so an
out-of-range limit is a **422**, not a silent clamp.

### Errors

| Status | Meaning | What to show |
| --- | --- | --- |
| 401 | No valid session cookie | `apiFetch` already refreshes once then redirects to `/login`; you get an `ApiClientError`. Usually you show nothing. |
| 403 | Authenticated but not authorized | Two causes, distinguishable by the message: a **cross-origin mutation** (`"cross-origin request refused"`) or a **scope outside the allowlist** (`"is not authorized for this operator"`). The second means your scope picker offered something the server does not allow — a bug on your side. |
| 404 | Not found **or** owned by another scope | Same response for both, deliberately: a foreign id must not reveal that it exists. Render as "not found". |
| 409 | State conflict | Three shapes, see below. |
| 422 | Validation failure | Body is either `{detail: [...]}` (FastAPI) or `{detail: {ok, issues|error, message}}`. Render `issues[].message`. |
| 503 | Dependency unavailable | Catalog or source down. Distinguish from 500: this is retryable and the operator is not at fault. |

### The three 409 shapes

**1. Revision conflict** — someone else saved first. `PATCH /workflows/{id}`:

```json
{
  "detail": {
    "ok": false,
    "rejection_reason": "REVISION_CONFLICT",
    "message": "..."
  }
}
```

Recoverable: re-fetch the workflow, show "this alert changed while you were editing",
offer to reload. Do **not** silently retry with the new revision — that would discard
the other operator's change.

**2. Not a screener** — a plain 409 with `detail` as a string: `"this workflow is not a
screener"`. This is distinct from 404 on purpose: you *can* read this workflow, it is
simply not a screener, so "not found" would be a lie.

**3. Invalid stored revision cannot activate** — `POST /activate` returns 409 with
`{ok: false, issues: [...]}`. The stored definition is re-validated on activate, so a
revision that became invalid cannot be switched on. Show the issues.

### Revision-conflict workflow

`GET /workflows/{id}` returns `latest_revision` and `active_revision` (each with
`revision`, `revision_id`, `status`, `canonical_hash`, `created_at`, `activated_at`).
Send `expected_revision` on `PATCH` — that is the `latest_revision.revision` you loaded.
The compare-and-insert happens inside the database transaction, so it is atomic.

---

## 6. Authentication, origin protection, scopes, tokens

### Cookie authentication

**VERIFIED.** Every `/api/alerts/*` route requires the app session cookie. There is no
route that accepts a worker bearer token. A missing session is a uniform 401.

`apiFetch` already does the right thing: `credentials: "include"`, one deduped refresh
attempt, then redirect. **Use `apiFetch` and do nothing extra.**

### Origin protection for mutations

**VERIFIED by a parameterised sweep over every mutation.** On an unsafe method
(POST/PUT/PATCH/DELETE) with an `Origin` or `Referer` header present, the origin must be
in the server's CORS allowlist or the request is **403**.

What this means for you:

- A normal browser request from the app's own origin passes automatically.
- A request with **no** `Origin` also passes — that is deliberate, so scripted clients
  still work; the cookie is still required.
- You do not need to send anything. But if you ever proxy mutations through a
  different hostname, that hostname must be in `APP_ALLOWED_CORS_ORIGINS`
  (default allowlist is `localhost:3000`, `127.0.0.1:3000`, `localhost:13000`,
  `127.0.0.1:13000`).

Reason it exists: on HTTPS the session cookies are issued `SameSite=None`, so the
SameSite defense is absent exactly there. This is the replacement.

### Authorized scopes — the rule you must not get wrong

**VERIFIED.** A scope sent by the client is a **selection, never an authority**. The
server holds an allowlist (`ALERTS_OPERATOR_SCOPES`) and:

- `GET /workflows?scope=X` where X is not allowlisted is **403**, *even when X holds
  data*, and the refusal does not reveal whether it does.
- `GET /scopes` returns **only** authorized scopes, each with `has_data`, so the picker
  cannot become a way to enumerate another owner's alerts.
- Writes take the owner from the authorization result. A body-supplied `scope` or
  `account_scope` is **not read at all** — there is no path on which a client value
  reaches the database.

**Practical guidance:** build the scope picker from `GET /scopes` and nothing else. If a
user deep-links a scope not in that list, you will get a 403 and should show a
permission state (§13), not a retry.

Why a picker exists at all: alerts created through the SDK/API live under a **token**
scope (`kite:paper-a`), while the browser session's own scope is `app:<username>`.
Without a picker an operator would see an empty page and think nothing is configured.

### Token rules

**VERIFIED.**

- `GET /tokens/presets` returns the presets **from the server**. Do not hard-code them —
  a hard-coded preset can drift from what the backend will grant.
- Presets: `alerts_authoring` (`workflows:read|write|activate`), `alerts_read_only`,
  `notifications` (adds `notifications:test`), `external_producers`
  (`signals:read|admin`).
- **No preset grants an execution action.** `intents:submit`, `risk:update`, `runs:*`,
  `gtt:*` are refused with 422, and `live` is not an allowed mode. There is no UI state
  in which the user can obtain an order-placing credential from this screen. Do not
  offer one.
- `account_scope` is fixed server-side to the authorized scope. The create form should
  display it as read-only text with an explanation, and must not send it.
- The token value is returned **once**, in the create response, with
  `reveal_once: true`. It is stored hashed and cannot be retrieved. Show it in a dialog
  the user must dismiss; never put it in query cache, and never re-render it from a
  cached list response — the list does not contain it.
- `GET /tokens` marks each token `scope_matches_operator`. Surface a mismatch as a
  **warning**, not an error: a token pointed at another scope silently reads an empty
  alerts view, which looks like "nothing is configured" instead of "wrong scope".

---

## 7. Capability-driven fields

**VERIFIED** by executing `capabilities_payload()`. This is the real top-level shape:

```
arithmetic, breadth_modes, breadth_semantics, clock_aliases, clocks, features, fields,
fundamentals_fields, fundamentals_source, hysteresis, limits, operators, pair_lookback_bounds,
pair_max_skew_bars, pairs, screener, session_cap_note, session_cap_resets, sessions,
stage_types, timeframes, triggers, universe_ref_kinds, universe_source_kinds
```

Selected contents, to show the shape. **Read the live response rather than transcribing
these** — they are here so you can recognise the structure.

```json
{
  "sessions": ["currency", "mcx_commodity", "nse_equity"],
  "clocks": {
    "candle_close": {"latency": "one_bar", "source": "completed candles (redis completions + postgres continuity)"},
    "ltp": {"latency": "seconds", "source": "market:ticks pub/sub (live LTP)"}
  },
  "clock_aliases": {"fundamentals_refresh": "candle_close"},
  "timeframes": ["minute","3minute","5minute","10minute","15minute","30minute","60minute","day"],
  "triggers": ["once", "on_transition", "once_per_session", "reminder"],
  "stage_types": ["breadth", "feature", "filter", "signal"],
  "fields": ["change_pct","close","high","low","ltp","open","prev_day_high","prev_day_low","turnover","volume"],
  "operators": {
    "gt": "level", "gte": "level", "lt": "level", "lte": "level",
    "crosses_above": "crossing", "crosses_below": "crossing",
    "rises_pct": "pct", "falls_pct": "pct",
    "breaks_prev_high": "break_", "breaks_prev_low": "break_", "within": "range"
  },
  "limits": {
    "max_stages": 64, "max_alerts": 256, "max_instruments": 1000,
    "max_feature_stages": 8, "max_conditions_per_group": 32,
    "max_arithmetic_depth": 3, "max_input_chain_depth": 8,
    "max_consecutive_bars": 50, "max_sequence_within_bars": 500,
    "max_breadth_instruments": 1000, "max_breadth_window_s": 86400,
    "max_per_session": 1000
  }
}
```

`features` is keyed by indicator name, each with `params` (min/max per parameter),
`defaults`, `inputs`, `outputs`. Use the min/max as your input bounds — do not invent
them.

### The operator group is the level-vs-crossing fix

`operators` maps each operator to a **group**: `level`, `crossing`, `pct`, `break_`,
`range`. This is the vocabulary the UI needs.

**The trap this closes:** a level operator (`gt/gte/lt/lte`) never *fires* — it reports
whether a condition is currently true, not that it became true. So a rule built only
from level operators, with an `on_transition` trigger, can never notify. This was the
actual cause of a live Phase 4 failure.

Therefore: label `level` operators "is above / is below a level" and `crossing`
operators "crosses above / crosses below", and surface the warning described in §9.

### Unsupported combinations — render disabled, never hidden

**VERIFIED.** These are reported by the server as not implemented. Show them disabled
with the explanation, or omit them; do **not** present them as working.

| Feature | Server says |
| --- | --- |
| Simultaneous breadth | `breadth_modes.simultaneous.implemented == false`. Only `triggers_within` works. |
| Dynamic/indicator hysteresis | `hysteresis.threshold` = `"constant only (right: {value: X}); dynamic release operands are not implemented"` |
| Scheduled screener scans on MCX/currency | `screener.schedule_note`: only `nse_equity` is calendar-backed. Show the backend's own reason. |
| Exchange calendars for MCX/currency | Not built. Eligibility there is feed-driven (§8). |

### Screener-specific capabilities

`screener.attachment_triggers` = `["entry","exit","top_n","rank_delta"]`;
`screener.run_statuses` = `["running","complete","partial","failed"]`;
`screener.tie_break` = `"instrument identity (EXCHANGE:SYMBOL) ascending"` — display this,
because it explains why equal scores have a stable order.

---

## 8. Lifecycle vs freshness, warmup, quarantine, suppression

This is the section most likely to produce a misleading UI. Read it carefully.

### Lifecycle and freshness are different questions

**VERIFIED.** `GET /workflows/{id}/health` returns them as separate fields:

```json
{
  "lifecycle": {"active": true, "archived": false, "active_revision": {...}},
  "subscriptions": [
    {
      "subscription_id": "...",
      "alert_id": "a1",
      "instrument_key": "NSE:RELIANCE",
      "state": "active",
      "last_evaluated_at": "2026-09-11T12:00:00+00:00",
      "evaluation_age_s": 4.2,
      "last_tick_received_at": "2026-09-11T11:59:56+00:00",
      "tick_age_s": 4.0,
      "stale": false,
      "stale_reason": null,
      "continuity_invalidated_at": null,
      "continuity_invalidation_reason": null,
      "quarantined_until": null,
      "failures": 0,
      "last_error": null
    }
  ],
  "runtime": {"available": false, "reason": "health_file_absent", "note": "..."}
}
```

**An alert can be `active` and `stale` at the same time.** That is the normal state of a
working alert on a quiet instrument. Do not collapse these into one badge. The list page
should show both: a lifecycle chip and a data-freshness chip.

### `stale_reason` is a vocabulary, and each value means something different

| Value | Meaning | What the operator should do |
| --- | --- | --- |
| `null` | data is flowing | nothing |
| `no_accepted_tick` | nothing has ever been evaluated for this subscription | check wiring/activation — this is a setup problem, not a market one |
| `tick_age_exceeded` | it evaluated before; the feed has gone quiet | a data problem; expect no signals |
| `continuity_invalidated` | a silence was already detected and the crossing state was deliberately reset | the alert needs a **fresh crossing** before it can fire again; this is not a bug |
| `not_an_ltp_subscription` | a candle-clock subscription | candle freshness is a different mechanism; no tick age is reported and you must not display one as if it existed |

`tick_age_s` is derived from a **stored receipt timestamp**, not from live state. It
grows on its own with no tick arriving, and it survives a worker restart. That is
deliberate: the failure mode being covered is silence, and a counter that only advances
when data arrives can never report it.

### Runtime-only facts are UNKNOWN, not zero

**VERIFIED.** Quarantine, per-subscription failure counts, suppression counters and task
liveness live in the evaluation worker's memory, which is a **different container** from
the API. They are merged only when the worker's health file is readable; otherwise:

```json
{"available": false, "reason": "health_file_absent", "note": "... UNKNOWN — this is not a report that they are zero or healthy"}
```

**You must render this as unknown.** Showing "0 quarantined" when the state is unknown
tells the operator the opposite of the truth. Reasons you may see: `no_health_file_configured`,
`health_file_absent`, `health_file_unreadable`, `health_file_too_large`,
`health_file_invalid`, `health_file_not_an_object`.

When available, the runtime section carries `quarantined` (subscription id → ISO
timestamp), `subscription_failures` (id → `{failures, last_error, last_failure_at,
quarantined_until}`), `tasks` (`{evaluation|delivery|screener: {alive, restarts,
backoff_s, last_started_at, last_exit_reason}}`), and counters
(`rejected_ticks`, `stale_tick_instruments`, `never_ticked_instruments`,
`future_ticks`, `stale_ticks`).

### Quarantine semantics

**VERIFIED** on the runtime side. A subscription that fails repeatedly is parked
("quarantined") after a small number of consecutive failures and re-probed after a
cooldown; a success clears it. It is **in-memory by design** — a worker restart re-probes
once. Consequence: quarantine can disappear from health after a deploy, and that is
intended, not a bug. Copy should say "parked after repeated failures; will be retried".

### Warmup

**VERIFIED gap.** There is **no live warmup-progress field** on the health endpoint.
`warmup_bars` exists only in the **preview** response (how many completed bars the
supplied samples contained). So:

- preview can tell the user "these samples contain 3 warmup bars, which is fewer than
  this indicator needs";
- the detail page cannot show "warming up: 40%". Do not invent one.

The plan listed "warmup progress" on the detail page. It is not exposed. See §16.

### Suppression

**VERIFIED gap.** Suppression reasons (`stale_tick`, `future_tick`, `ltp_gap`,
`session_cap`, and others) are **logged at INFO, not persisted**. Only the aggregate
counters in the runtime health section are available, and only when the health file is
readable. There is no per-occurrence suppression record to list.

Two reasons *are* durable and do appear per subscription in health:
`continuity_invalidation_reason` and the `stale_reason` vocabulary above.

The per-session cap **is** explainable without a new endpoint: see
`capabilities.session_cap_note` — the cap is scoped to `(workflow, alert)`, counts
**logical notifications** (one per signal event, never per channel delivery), and resets
when the resolved session id changes. MCX/currency sessions are feed-driven, so their
boundary is the IST date.

---

## 9. Preview guarantees and activation behavior

### Preview

**VERIFIED.** `POST /workflows/preview` takes **either** `yaml_text` **or** `document`
(exactly one; providing both or neither is a 422), plus `observations`: an array of
timestamped samples you supply.

```
{ "document": {...}, "observations": [ {"instrument_key": "NSE:RELIANCE", "ts": "...", "close": 2900.0}, ... ] }
```

It returns `{ok, issues, instruments, stages, alerts, evaluation, warmup_bars,
evaluated_observations, would_fire, unknown_reasons, note}`.

Guarantees, asserted by test:

- **Nothing is persisted.** No workflows, no signal events, no deliveries. Verified by
  asserting all three tables are empty after a preview.
- **Nothing is sent.** No provider call.
- **No evaluation state changes.** It runs in memory over your samples.
- The `note` says so, and says preview cannot guarantee a future market event. **Show
  that text.** An operator who believes a preview predicts the market has been misled by
  the UI, not by the backend.

**Important:** preview does **not** fetch market data. You supply the observations. If
you want "preview with real data", that is a separate feature and it is not built.

`unknown_reasons` is where "why didn't it fire" lives — e.g.
`missing_or_unsupported_data`, `insufficient_history`, `non_final_candle`,
`instrument_not_in_workflow`. Render them; they are the difference between a useful
preview and a silent nothing.

### Validation, and the warning you must surface

**VERIFIED.** `POST /workflows/validate` returns `{ok, issues}` where each issue has
`{where, code, message, severity}`. `severity` is `"error"` or `"warning"`.

- `ok: true` **with** warnings is a valid document. Errors block; warnings do not.
- The important warning is `level_only_never_fires`, explaining that a rule built only
  from level operators with a trigger that cannot fire will never notify, and how to fix
  it (choose a crossing operator, or use the `reminder` trigger).
- `GET /workflows` also carries `warnings` per workflow, computed from the **stored**
  document, so the list can flag it without the user opening the alert.

Warnings never affect the canonical hash and never change whether a document is valid.

### Activation

**VERIFIED.** `POST /workflows/{id}/activate` with optional `?revision=N` (omit for the
latest; pass one for rollback). It re-validates the **stored** revision first, so an
invalid stored revision returns 409 rather than activating. Response includes
`subscriptions_created` and a note.

**A fresh activation is silent by design.** An alert whose condition is already true at
activation initializes and does **not** notify, unless the alert sets
`notify_if_already_true`. The response note says this, and the UI should too — otherwise
the first experience of the feature is "I activated it and nothing happened".

Pause/resume act on the subscriptions of the active revision and return `updated` (how
many subscription rows changed). A workflow with no active revision returns 409.

---

## 10. Channels, credentials, one-time secrets

### Channels

**VERIFIED.** `GET /channels` returns channels for the authorized scope with
`{channel_id, name, provider, destination, secret_env, enabled, created_at}`.
Supported providers are exactly `telegram` and `ntfy`; anything else is a 422 naming the
supported set.

**A secret value is never returned.** What you get is `secret_env` — the **name** of a
server-side environment variable that is resolved at send time. Render it as a name, and
say where it comes from. The response `note` already says this.

`POST /channels/{id}/test` sends a **real message**. It is the only route in this family
that contacts a provider:

- require an explicit confirmation before calling it;
- it is not called from any read path, and you must not call it on mount;
- a missing environment variable returns **400** with
  `{error: "missing_env_secret", secret_env: "NAME", message: "..."}` — show the variable
  name, because that is the actionable part.

**PROPOSED:** the plan suggested masking the chat/topic identifier in `destination` by
default with a reveal. Not implemented server-side; if you want it, do it in the UI.

### One-time secrets, both kinds

Two endpoints return a secret exactly once. Treat them identically:

| Endpoint | Field | Envelope |
| --- | --- | --- |
| `POST /tokens` | `token` (starts `kwa_`) | `reveal_once: true` |
| `POST /signals/producers/{name}/credentials` | `secret` | `reveal_once: true` |

Rules that make the "one-time" claim true:

1. Show it in a modal the user explicitly dismisses.
2. **Never** put it in TanStack Query cache. Use `useState` local to the modal, or
   mutation `onSuccess` state — not `useQuery`, because query data is retained and can
   be re-rendered by any refetch or window focus.
3. Never write it to `localStorage` or a URL.
4. The list endpoints do not return it. If you ever see a secret in a list response,
   that is a backend bug — report it.

A **producer credential is not a worker token**, and the copy must say so: a producer
credential can only submit values for its own producer and cannot read alerts, runs or
orders. The two are minted on different screens for different purposes. The server's
`note` already states this.

### External producers

**VERIFIED.** `GET /signals/health` explains the semantics an operator needs:

- accepted values are **SAMPLED** by the consuming stage's candle clock and never
  trigger evaluation on their own, so a value can expire between evaluations;
- there is **no fallback**: a missing or expired value makes the condition **UNKNOWN**,
  not false. This matters — "unknown" and "not triggered" are different, and the UI must
  not imply the rule evaluated to false.

`GET /signals/values` returns each value with `status` (`accepted` | `late`) and
`expires_at`, so a retained-but-unusable value is distinguishable from a usable one.
Show both.

---

## 11. Canonical document examples and round-trip requirements

### The one rule

**VERIFIED by test.** The structured editor, the YAML view and the canvas are three
views of **one** definition. For any document:

```
compile(parse(yaml))  →  the SAME canonical hash
```

`GET /workflows/{id}/yaml` returns `yaml` plus the `canonical_hash` it round-trips to.
Pinned by tests over a simple document and a rich one (sequences, indicators,
hysteresis, multiple alerts, `max_per_session`).

Do not build a second document format, and do not treat the YAML as a lossy display.

### The YAML is readable shorthand, not an internal dump

**VERIFIED.** The renderer emits authoring shorthand and then *verifies* it parses back
to an identical hash, falling back to the verbose canonical form if it would not. So you
will see:

```yaml
version: 1
name: reliance-breakout
session: nse_equity
instruments:
- NSE:RELIANCE
stages:
- id: px
  type: signal
  clock: candle_close
  timeframe: 15minute
  conditions:
    all:
    - left:
        field: close
      op: crosses_above
      right: 3000.0
    - left:
        indicator: rsi
        period: 14
      op: lt
      right: 70.0
alerts:
- id: a1
  source: px
  trigger: on_transition
  channels:
  - ops
  cooldown_s: 300
  rearm_level: 2900.0
  rearm_direction: below
```

Note `right: 3000.0` — a bare number is the shorthand for a constant. `{field: close}`
and `{indicator: rsi, period: 14}` are the shorthand forms. The verbose
`{kind: field, name: close, value: null, params: {}}` form is also accepted on input.

### Document skeleton

```json
{
  "version": 1,
  "name": "reliance-breakout",
  "session": "nse_equity",
  "instruments": ["NSE:RELIANCE"],
  "universe": null,
  "stages": [ ... ],
  "alerts": [ ... ]
}
```

A screener adds a `screener` block; **attachments live inside that block**:

```json
{
  "screener": {
    "schedule": {"every": "1d"},
    "rank": {"by": {"field": "close"}, "direction": "desc"},
    "top_n": 10,
    "attachments": [
      {"id": "entry", "trigger": "entry", "channels": ["ops"],
       "entry_rank": 10, "exit_rank": 25, "exit_after": 3}
    ]
  }
}
```

Two shape details that cost me time, so they will cost you time too:

- `rank.by` is an **operand**, not a string: `{"field": "close"}`, not `"close"`.
- Attachment hysteresis fields (`entry_rank`, `exit_rank`, `exit_after`) are **flat on
  the attachment**, not nested under a `hysteresis` key.

### Validation you can do client-side

Session ↔ exchange pairing can be checked before submit using `capabilities.sessions`
plus the compiler's own rule, so the user gets an immediate explanation instead of a
round trip. **Also let the server validate** — do not treat the client check as
sufficient, because the compiler's rule is the authority.

---

## 12. Canvas layout contract

**VERIFIED.** Endpoints, storage and validation are implemented and tested; the canvas
rendering itself is not built.

### The hash guarantee

A **layout-only** change (move or collapse a node) writes only to the layout table:

- no new revision is created,
- `canonical_hash` is unchanged,
- the stored document is unchanged.

Asserted directly, all three. This is why layout is a table rather than a `ui:` block in
the document: nothing about the canvas can reach the hash, so spec §3 holds by
construction instead of by teaching the hasher to skip a field.

A **semantic** change (conditions, instruments, session, clock, trigger, alerts) goes
through the normal `PATCH` path with `expected_revision`, and therefore produces a new
hash and a new draft revision.

**Therefore the UI must distinguish them:** a cosmetic move saves without a
revision-conflict check; a semantic edit participates in conflict handling.

### Node identities are namespaced

`node_id` is `"<namespace>:<id>"` with namespaces `stage`, `alert`, `channel`.
`GET .../layout` returns a `contract` object with `namespaces`, `node_id_format`,
`max_abs_coordinate` and `max_node_id_length`, so you can build ids from the server's
own declaration rather than hard-coding.

Why it matters: stage ids, alert ids and channel names are **separate id spaces that can
legally collide**. A stage named `telegram_primary` and a channel named
`telegram_primary` can coexist in one document. Keyed by a bare id, those two nodes would
share one row, and a reorder could hand one node the other's saved position. A test uses
exactly that collision and asserts three distinct rows.

### Calls

```
GET  /api/alerts/workflows/{id}/layout
  → {ok, workflow_id, nodes: [{node_id, x, y, collapsed, updated_at}], contract}

PUT  /api/alerts/workflows/{id}/layout
  body: {"nodes": [{"node_id": "stage:px", "x": 100.5, "y": 200.25, "collapsed": false}]}
  → {ok, workflow_id, saved, nodes, note}

POST /api/alerts/workflows/{id}/layout/delete
  body: {"node_ids": ["stage:gone"]}
  → {ok, workflow_id, removed}
```

Behavior you must rely on:

- **`PUT` merges.** Sending only the moved node preserves every other position. There is
  no delete-by-omission. Use the delete route when a node leaves the document.
- **Validation is all-or-nothing.** One bad entry rejects the whole payload, so you can
  never end up with a half-applied layout.
- **Rejections are 422** with `{error: "invalid_node", message}`. The bare-id message
  explains the collision hazard.
- Coordinates must be finite and within ±1,000,000. NaN/Infinity are refused.
- `collapsed` defaults to false.

**PROPOSED and unresolved:** there is no optimistic concurrency on layout. Two operators
moving nodes simultaneously will last-write-wins per node. That is acceptable for
cosmetic state, but the UI should not present layout as a shared authoritative view.
There is also no per-node partial update: send the nodes you moved.

### Unsupported-field preservation

**PROPOSED for the canvas, but the backend rule is VERIFIED.** The canvas is intended to
render a subset (signal/filter/feature stages, alerts, channel leaves) and to show
anything outside it — screener blocks, `sequence`, `breadth`, external operands,
arithmetic, deep chains — in a **read-only "advanced"** presentation.

The backend side of this already holds and is tested: the YAML round-trip is lossless,
so a document you load and save unchanged produces an identical canonical hash. The
requirement on you is:

> the canvas mutates only the fields it models, and rebuilds the document **from the
> loaded document** rather than from its own model of it.

If you rebuild from a partial model, you will silently delete the fields you did not
model, and the hash will change on a no-op save. Pin that with a test: load → save
unchanged → identical hash.

---

## 13. UI states

**PROPOSED** in presentation, **VERIFIED** in the backend conditions that produce them.

| State | Trigger | What the user needs to understand |
| --- | --- | --- |
| **Loading** | query in flight | skeletons per section, matching the existing convention |
| **Empty (new)** | `workflows: []` | "No alerts yet" + a create action. Distinguish from the next row. |
| **Empty (wrong scope)** | `workflows: []` but another scope has data | Use `GET /scopes` `has_data`. "No alerts under `app:admin`; `kite:paper-a` has 3" with a switch action. This is the single most likely confusion in the product. |
| **Empty (no data yet)** | workflow exists, `stale_reason: "no_accepted_tick"` | "Activated, waiting for the first evaluation" — a setup state, not a failure |
| **Stale** | `stale: true`, `tick_age_exceeded` | "No fresh data for Ns" + the age. Do **not** show an error style; a quiet market is not an error. |
| **Error (validation)** | 422 | `issues[].message`, with the offending field where you can locate it. Errors block, warnings do not. |
| **Error (revision conflict)** | 409 `REVISION_CONFLICT` | "Changed while you were editing" + reload action. Never auto-retry. |
| **Error (dependency)** | 503 | "Catalog unavailable" — retryable, not the user's fault |
| **Permission (scope)** | 403 with `"is not authorized"` | "This scope is not available to you." Not a retry. |
| **Permission (origin)** | 403 with `"cross-origin"` | A deployment/config problem. It should be unreachable in normal use; if a user sees it, the allowlist is wrong. |
| **Unknown (runtime health)** | `runtime.available: false` | "Quarantine state unknown — the worker health file is not readable." **Never** render as 0. |
| **Partial (screener run)** | run `status: "partial"` | "Partial run — this is not a complete membership replacement." Downstream universes keep the last complete revision. |
| **Unavailable (feature)** | `capabilities.breadth_modes.simultaneous.implemented === false` etc. | Disabled control with the reason. Never a control that appears to work. |

Accessibility: keyboard reachable, labelled controls, focus trapped in dialogs and
returned on close, and status changes announced (a stale badge appearing is a state
change a screen-reader user must learn about).

---

## 14. Local startup and safe development fixtures

### Startup

**VERIFIED** from the repo's own configuration.

```
docker compose -f compose.yml -f compose.dev.yml up -d
```

| Service | URL |
| --- | --- |
| Frontend (dev) | `http://localhost:13000` |
| API | `http://localhost:18777` |
| Market runtime | `http://localhost:8780` |

Frontend-only loop (if the stack is already up):

```
cd frontend-next
npm run dev
npm run typecheck
npm run lint
npm run test          # vitest run
```

`next.config.ts` proxies `/api/:path*` to `BACKEND_INTERNAL_URL` (default
`http://localhost:18777`), so use **relative** paths in the browser. Never hard-code an
absolute backend URL.

### Environment you need for the operator API

| Variable | Purpose | Default if unset |
| --- | --- | --- |
| `ALERTS_OPERATOR_SCOPES` | comma-separated allowlist of scopes the operator may act as | falls back to `app:<username>` |
| `ALERTS_OPERATOR_OWNER` | which authorized scope is the default selection | first authorized scope |
| `ALERTS_WORKER_HEALTH_FILE` | path to the alerts worker's health JSON, for the runtime section | `/app/alerts-health.json` |
| `APP_ALLOWED_CORS_ORIGINS` | origins permitted on unsafe methods | `localhost/127.0.0.1` on `:3000` and `:13000` |

To see the runtime health section during development, either point
`ALERTS_WORKER_HEALTH_FILE` at a local file or mount the worker's file. Without it the
section is honestly `available: false`, which is also a state worth developing against.

### Safe fixtures

**PROPOSED**, but designed to be non-destructive and to need no live market data:

1. **An empty scope.** Point `ALERTS_OPERATOR_SCOPES` at `app:admin` only and sign in as
   a different user — or simply leave the tables empty. Exercises the empty state.
2. **A scope with data in another scope.** Create alerts via the API under
   `kite:paper-a`, then view as `app:admin` with both in the allowlist. Exercises the
   scope picker and the `has_data` hint — the most valuable fixture, because it is the
   most likely production confusion.
3. **A stale alert, with no worker running.** Create a workflow, activate it, and do not
   start the alerts worker. `stale_reason` becomes `no_accepted_tick`. You get the
   stale/unknown states with no market data and no waiting.
4. **A level-only rule.** A stage with only `gt`/`gte`/`lt`/`lte` conditions and an
   `on_transition` alert. Exercises the `level_only_never_fires` warning in both the list
   and validate.
5. **A screener with attachments.** Needed for the baseline view. Use the document in
   §11 — remember `rank.by` is an operand and hysteresis is flat.
6. **A workflow with a `telegram_primary` stage and a `telegram_primary` channel.** The
   collision case; useful for confirming your node-id construction in the canvas.

Use a **scratch scope** for anything mutating. Do not point fixtures at a scope holding
real alerts, and do not call `POST /channels/{id}/test` against a real destination while
others are watching it — it sends a real message.

---

## 15. Acceptance scenarios to exercise in a browser

**PROPOSED** as a checklist. Each maps to a verified backend behavior, so a failure means
either a UI bug or a backend regression — both worth catching.

1. Sign out, then load `/alerts`. You land on the login page and return to `/alerts`
   afterwards.
2. Load the alert list with an empty scope. You get an empty state, not an error.
3. Load the list where a *different* authorized scope has data. The picker shows
   `has_data`, and switching scopes changes the list. **This is the one to get right.**
4. Open an alert whose rule is level-only. The list badge and the detail page both show
   the never-fires warning, and the document still saves.
5. Create an alert through the structured form. Every number bound comes from
   `/capabilities`. Confirm you can construct a working `crosses_above` rule without
   reading YAML.
6. Validate a document with a level-only rule. `ok` is true and the issue's severity
   reads as a warning, not an error.
7. Preview that rule with two samples. Confirm the page states that nothing was
   persisted, nothing was sent, and that preview cannot guarantee a future event.
8. Save, activate. Confirm the response's `subscriptions_created` appears and that the
   UI explains the silent-activation behavior.
9. Edit the same alert in two tabs; save the second. You get a recoverable conflict
   state, not a silent overwrite and not a crash.
10. Open health on an activated alert with no worker. Freshness reads stale with a
    growing age; the runtime section reads **unknown**, not zero.
11. Open the YAML tab. Copy it out, change a value, paste it back through validate — the
    canonical hash changes only for the value you changed.
12. Create a channel with a `secret_env` naming an unset variable, then test-send. You
    get a 400 naming the variable.
13. Mint a token. The secret appears once; navigating away and back does not restore it;
    a list refresh does not contain it.
14. Attempt to find any control that grants `intents:submit`, `runs:*` or a `live`
    mode. There should be none.
15. Open a screener's runs. A partial run is labelled partial and says it is not a
    complete replacement.
16. Open attachment baselines. `consecutive_absent` and `exit_after` are both visible.
17. Move a node on the canvas. Confirm **no** new revision appears and the hash is
    unchanged. Then edit a condition and confirm a new draft revision **does** appear.
18. Create a document with a stage named `telegram_primary` and a channel named
    `telegram_primary`; move both. They keep separate positions.
19. Load a document containing a field the canvas does not model, save without changing
    anything, and confirm the canonical hash is identical.
20. With a screen reader, traverse the creation flow end to end and confirm every control
    is labelled and dialog focus is trapped and restored.

---

## 16. Backend gaps — stated, not assumed

These are things the plan or a reader might expect to exist, which **do not**. Do not
build UI against them.

| Gap | Detail | Impact |
| --- | --- | --- |
| **Live warmup progress** | No endpoint. `warmup_bars` exists only in preview responses (count of completed bars in the supplied samples). | The detail page cannot show "warming up 40%". Show "waiting for first evaluation" (`no_accepted_tick`) instead. |
| **Per-occurrence suppression records** | Suppression reasons are logged at INFO, not persisted. Only aggregate counters exist, and only when the health file is readable. | No suppression history list. Show counters and the per-subscription `stale_reason` instead. |
| **Runtime health without a mounted health file** | Quarantine, failure counts and task liveness live in the worker process in a different container. | `runtime.available: false`. Requires a mount to populate. Design for both states. |
| **Deliveries total count** | `GET /{id}/deliveries` has no `total`, unlike `events` and `values`. | "Showing 50" rather than "50 of 312". |
| **Layout concurrency** | No optimistic concurrency on layout. | Last-write-wins per node. Acceptable for cosmetic state; do not present layout as shared truth. |
| **Per-node layout update** | `PUT` takes a list; there is no single-node endpoint. | Send the nodes you moved; the write merges. |
| **Screener preview needs candle history** | It runs the real pipeline over stored candles. | In a dev stack with no candle data it may return little. Not a UI bug. |
| **Screener authoring validation depth** | The compiler validates structure; whether a *schedule* is calendar-backed is reported via `capabilities.screener.schedule_calendars`. | Check capabilities before offering a schedule. |
| **Producer credential list** | There is no endpoint listing a producer's issued credentials. | You cannot render "3 active credentials" for a producer. Revoke by token id from the issue response only. |
| **No platform-wide freshness aggregate** | Each workflow row carries a `freshness` block (see below); there is no single "N alerts stale" endpoint across all workflows. | An operations overview summing staleness does one `GET /workflows` (which returns every row's `freshness`), not one call per workflow. |
| **`channel_name` is a LEFT JOIN** | `deliveries.channel_id` is `NOT NULL` with a foreign key, so the name resolves in practice; it is read defensively. | Render a null `channel_name` as "unknown channel" rather than crashing, but do not build a "removed channel" state around it — it is not a routine case. |

## 17. Out of scope (do not build)

- MCP authoring — deferred.
- Multi-tenancy, accounts, roles, sharing, admin surfaces.
- Any order placement or execution action from alerts or screeners.
- Options/OI/depth conditions.
- `mode: simultaneous` breadth and dynamic/indicator hysteresis — shown unavailable.
- Exchange calendars for MCX/currency.
- A second frontend application.
- Certification work (E-1…E-30 matrix, capacity measurement) and the scheduler-ntfy
  cutover — both are later phases, not frontend work.

---

## 18. Provenance

Backend work in this assignment is complete and committed. Frontend work has not
started: **there are currently zero alerts references in `frontend-next/`.**

Test commands used for the claims above:

```
.venv/bin/python -m pytest tests/workflows tests/alerts tests/screeners tests/notifications tests/api -q
.venv/bin/python -m pytest tests/api/test_alerts_operator.py tests/api/test_alerts_operator_ops.py tests/api/test_alerts_operator_platform.py tests/workflows/test_canvas_layout.py -q
```

Behavior changes in this assignment were checked by mutation: for every guarantee
asserted above, the corresponding mutation was introduced and the suite was confirmed to
fail. The mutations are recorded in the commit messages.

If you find a discrepancy between this document and the running backend, the backend is
the source of truth and this document is the bug. Report it rather than coding around it.
