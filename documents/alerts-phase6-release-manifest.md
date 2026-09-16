# Alerts Phase 6 — release manifest

Prepared 2026-09-13 for the closure build on branch `development`. This is the
operator-facing deployment plan: what to rebuild, what to configure, in what
order, and how to roll back. **No secret values appear here.**

> **Deployment status: DEPLOYED (2026-09-15).** Phase 6 plus the 2026-09-15
> frontend release-completion pass and the alerts fixes listed in §9 were rolled
> out to the existing application environment from branch `development`
> (`f6d1740` + forward fixes, migration `20260915_000024`). See §9 for what was
> deployed, what is live verified, what remains not tested, and the defects the
> deployment itself surfaced.

## 1. Revision

| Item | Value |
| --- | --- |
| Branch | `development` |
| Closure commits | `3a8683d`, `06148cf`, `6353eea`, `e4485b8`, `a211480`, `ac9c7a8`, `18e1fb3`, `09512f2` (plus this documentation commit) |
| Migration head | `20260912_000018` — **unchanged** by the closure (no new migration) |
| Backend Python | 3.11 (`Dockerfile`) |
| Frontend | Next 16 / React 19 (`frontend-next/`) |

## 2. Services/images to rebuild

| Service (compose) | Container | Rebuild? | Why |
| --- | --- | --- | --- |
| `finance-app` | `kite-app` | **Yes** | Operator API additions (workflow-health `suppressions`, producer credential list), compose health-file mount |
| `alerts-worker` (`compose.worker.yml`) | `kite-alerts-worker` | **Yes** | Shares the health-file volume; no Python change in the closure, but rebuild for a coherent image |
| `frontend-next` | `kite-frontend-next` | **Yes** | All Phase 6 alerts UI |
| `market-runtime` | `kite-market-runtime` | No | Unchanged |
| `postgres` / `redis` | — | No | Unchanged |

Migrations are additive and already at head; the app container runs
`alembic upgrade head` on start, so no manual migration step is required.

## 3. Required environment (names only, no values)

Backend (`finance-app` and `alerts-worker`):

| Variable | Purpose | Default |
| --- | --- | --- |
| `ALERTS_OPERATOR_SCOPES` | Comma-separated allowlist of scopes the operator may act as | falls back to `app:<username>` |
| `ALERTS_OPERATOR_OWNER` | Default selected scope (must be in the allowlist) | first authorized scope |
| `ALERTS_WORKER_HEALTH_FILE` | Path the API reads the worker health JSON from | `/app/alerts-health.json` (set to `/health/alerts-health.json` by compose — see §4) |
| `APP_ALLOWED_CORS_ORIGINS` | Origins allowed on unsafe methods (CSRF replacement on HTTPS) | `localhost`/`127.0.0.1` on `:3000` and `:13000` |
| `ALERTS_LTP_FRESHNESS_ENABLED` | Kill switch for LTP freshness | `true` |
| `ALERTS_LTP_MAX_TICK_AGE_S` / `ALERTS_LTP_MAX_FUTURE_SKEW_S` / `ALERTS_LTP_MAX_GAP_S` | Freshness bounds | `300` |
| `ALERTS_WORKFLOW_QUARANTINE_AFTER` / `ALERTS_WORKFLOW_QUARANTINE_COOLDOWN_S` | Failure isolation bounds | `3` / `300` |
| `ALERTS_HEALTH_FILE` (worker) | Path the worker writes its health JSON to | `/app/alerts-health.json` (compose sets `/health/alerts-health.json`) |
| `ALERTS_HEALTH_INTERVAL_S` | Health publish interval | `30` |

Channel secrets are **names of environment variables** resolved at send time
(`secret_env`); no secret value is ever stored by the app or returned by the API.

## 4. Worker-health visibility between containers (closes a real gap)

The evaluation worker and the API run in **different containers**. Previously the
worker wrote `/app/alerts-health.json` inside its own container while the API read
the same default path inside *its* container — so the runtime section
(quarantine, failure counts, task liveness, counters) could never be populated.

The closure adds a shared named volume `alerts_health`:

- `alerts-worker` (`compose.worker.yml`): `ALERTS_HEALTH_FILE=/health/alerts-health.json`, `volumes: [alerts_health:/health]`
- `finance-app` (`compose.yml`): `ALERTS_WORKER_HEALTH_FILE=/health/alerts-health.json`, `volumes: [alerts_health:/health]`

The mount is at a **directory** (`/health`), never `/app`, so the worker's code
tree is not shadowed. When the volume is present the API and the healthcheck read
the same file; the worker's own healthcheck keeps honouring `ALERTS_HEALTH_FILE`.

After deploy, verify with:

```bash
docker exec kite-alerts-worker ls -l /health/alerts-health.json
docker exec kite-app cat /health/alerts-health.json | head -c 200
curl -s "$API/api/alerts/health" -b "$SESSION_COOKIE" | jq '.runtime.available'
# expect: true (not "health_file_absent")
```

## 5. Operator scope and origin configuration

- Set `ALERTS_OPERATOR_SCOPES` to the scopes the single operator may act as. A
  requested scope outside this list is **403**, and `GET /api/alerts/scopes`
  returns only authorized scopes.
- Set `APP_ALLOWED_CORS_ORIGINS` explicitly (never a wildcard). On HTTPS the
  session cookie is `SameSite=None`, so the server-side same-origin assertion on
  unsafe methods is the CSRF defense; the app's own origin must be in the list.
- No execution capability is offered anywhere on this surface: `intents:submit`,
  `risk:update`, `runs:*`, `gtt:*` are refused, and `live` is not an allowed mode.

## 6. Deployment order

1. **Backend migration + code** — `finance-app` (runs `alembic upgrade head` then
   uvicorn). Additive, nullable; independently deployable.
2. **Worker** — `compose.worker.yml` `alerts-worker` (start after the API is healthy).
3. **Frontend** — `frontend-next`. Tolerates an older backend and vice versa.

Recommended commands (adjust project/env as needed):

```bash
docker compose -f compose.yml -f compose.worker.yml build finance-app alerts-worker
docker compose -f compose.yml -f compose.worker.yml up -d finance-app
docker compose -f compose.yml -f compose.worker.yml up -d alerts-worker
docker compose -f compose.yml -f compose.worker.yml up -d frontend-next
```

## 7. Verification after deploy (non-market only)

- Container health for every service; the worker healthcheck requires a **recent**
  `last_health_at` **and** required-task liveness, not mere file existence.
- `GET /api/alerts/*` → **401** without a cookie, reachable with one.
- `GET /api/alerts/health` → `runtime.available: true`.
- Deployed file hashes vs the tree for the changed files.
- Frontend: `/alerts`, `/alerts/operations`, `/alerts/universes` render (auth
  middleware redirects to `/login` when unauthenticated, which is expected).

**Do not** exercise live-market evaluation, send notifications, or run the
capacity campaign as part of this deployment. Those are certification items (see
the parity document §8 and the validation inventory in the closure handoff).

## 8. Rollback

- Revert the image tag (or code revision) for the affected service.
- The migrations in this phase are **additive**, so **no down migration** is
  needed to roll back code.
- Behavioural changes are env-guarded: `ALERTS_LTP_FRESHNESS_ENABLED`,
  `ALERTS_WORKFLOW_QUARANTINE_AFTER`/`_COOLDOWN_S` can be disabled/tuned without a
  redeploy. The health-file env change is inert if the volume is absent (the API
  reports `runtime.available: false` rather than failing).


---

## 9. Deployment record and live verification (2026-09-15)

Status vocabulary: **deployed** (running in the environment), **live verified**
(observed against live market data/providers), **locally verified** (isolated
tests), **not tested**, **deferred**. No secret values appear here.

### 9.1 What was deployed

| Item | Value |
| --- | --- |
| Revision | branch `development`, deployed at `f6d1740`, extended by `a7ce3c3`, `3d348ab`, `3a48346`, `a60639c`, `3e79fb5` |
| Migration | `20260912_000018` → `20260915_000024` (applied by the `finance-app` container at start) |
| Images rebuilt | `kite-algo-finance-app`, `kite-algo-alerts-worker`, `kite-algo-frontend-next` (+ new `kite-algo-strategy-runner`); `market-runtime` unchanged |
| Services recreated | `finance-app`, `alerts-worker`, `frontend-next`, `strategy-runner` (no `compose down`; unrelated volumes preserved) |
| Alert delivery | `ALERTS_DELIVERY_ENABLED=1` (pre-existing); one operator-scoped Telegram channel created for the bounded test |

Deployed-code spot check: SHA-256 of the operator/router files inside the
running containers matches the deployed revision.

### 9.2 Live verified (Phase 6 objectives)

| Objective | Result |
| --- | --- |
| Worker-health visibility across containers | `GET /api/alerts/health` → `runtime.available: true`, with `evaluation-worker`, `screener-scheduler` and `delivery-worker` reported alive (the shared `alerts_health` volume works) |
| Alerts list, scope picker, empty state | Rendered in the deployed production build; scope selector offers only the authorized scope |
| Alert authoring wizard → validate → save → activate | Executed in the browser (instruments, session `mcx_commodity`, clock `ltp`, condition, trigger, channel, validate, save, activate) — this surfaced the capabilities defect in §9.4 |
| Live tick → accepted observation → evaluation | Observed continuously over three bounded windows (~45 minutes total) with per-subscription `last_evaluated_at` inside ~1 s of the poll and live exchange timestamps |
| Signal event → outbox → provider accepted | **One** complete run: exchange event `14:37:37Z` → event `8a529e4d-a37b-46c4-9cc6-e443ffafbd45` → delivery `f44410c7…` → Telegram **accepted**, provider message id `6`, exactly one event and one delivery |
| UI shows the event and the delivery accurately | Events tab renders the event with its instrument binding (broker, public key, broker token, catalog generation); Deliveries tab renders `PROVIDER ACCEPTED`, attempts 1, provider id 6 |
| No duplicate notification | One event, one delivery row, one attempt |
| Screener: explicit MCX universe, manual run, membership + coverage | Manual run executed; membership resolved 5/5 with 0 rejected; coverage `expected=5, evaluated=0, unavailable=5`; status **failed** with `candle_max_ts: null` — no stored daily candles for MCX futures in this deployment |
| Screener scheduler | Claimed the day's due `session_close` bucket once (coalesced per E-19) — not a long-running schedule claim |

### 9.3 Not tested here (do not read as verified)

- A **crossing** alert firing on natural price movement: two bounded windows
  observed live evaluation but no crossing (reported as “live evaluation
  observed, crossing not observed”; the delivery leg was then proven with a
  documented `notify_if_already_true` level trigger).
- Scheduled execution beyond that single coalesced bucket; currency (CDS/BCD)
  live validation; the capacity campaign; restart fault injection; ntfy
  delivery; delivery retry/backoff under provider failure.
- Rollback: not prepared or rehearsed by instruction (forward-fix only). A
  `pg_dump` was taken before the migration as operational protection.

### 9.4 Defects the deployment surfaced (all fixed forward)

Four of the five were invisible to the SQLite suites and appeared immediately
against PostgreSQL / the real UI:

| Commit | Defect |
| --- | --- |
| `a7ce3c3` | `GET /api/alerts/capabilities` delegated to the worker handler (worker bearer token required) → 401 for an authenticated operator, so the whole authoring form showed “Could not load capabilities”. |
| `3d348ab` | `max(jsonb)` in the workflow health route → 500 for every workflow with subscriptions on PostgreSQL. |
| `3d348ab` | `PgCandleHistory` required both `get()` and `snapshot()`, so the API's lazy catalog token map crashed with `AttributeError: '_CatalogTokenMap' object has no attribute 'items'` — every manual screener run and preview 500'd. |
| `3a48346` | `ScreenerRunRepository.claim_run` returned a committed, expired ORM instance after `session.close()` → `DetachedInstanceError` on every manual screener run. |
| `a60639c` | Pause/resume read a detached `WorkflowRevision.revision` → 500 on Pause. |
| `3e79fb5` | Fired signal events were written with a NULL `workflow_id`, so a delivered alert still showed “No signal events recorded” and “No deliveries recorded for this workflow yet”. The one pre-fix event was backfilled from its occurrence key. |

Each fix carries a focused regression test that fails without it.

---

## 10. Second deployment pass (2026-09-15, later the same day)

Forward-deployed from `origin/development` after the release-cleanup task. No
migration was added (head stays `20260915_000024`).

| Item | Value |
| --- | --- |
| Revision | `development` at `880bb9e` plus the commits listed in §10.1 |
| Services rebuilt | `finance-app`, `frontend-next`, `strategy-runner` (the runner gained a healthcheck) |
| New configuration (names only) | `APP_ALLOWED_ORIGINS` (explicit CSRF allowlist for the operator's origin), `HOSTED_SUPERVISOR_HEALTH_*` windows, `ALERTS_SCREENER_WARM_*` bounds |

### 10.1 What changed and what was verified live

| Area | Status |
| --- | --- |
| MCX candle acquisition for screener universes | **DEPLOYED + LIVE VERIFIED**: 5/5 contracts fetched (58–77 daily bars each), a repeat fetch made no broker calls, and the screener then ranked all five members with `unavailable: 0` |
| Partial availability handling | **LIVE VERIFIED**: a mixed universe produced "Scanned 1 of 2 symbols; 1 could not be scored (missing or insufficient data)" |
| One history window for manual and scheduled runs | **FIXED** (30 vs 120 default) and pinned by a test; visible in the UI as "needs 120 final daily candles" |
| Strategy-runner Docker health | **LIVE VERIFIED**: `healthy`, failing streak 0, `healthy: ok` probe output; the child identity cannot read the snapshot |
| Alert/screener creation over a non-secure origin | **FIXED + LIVE VERIFIED**: creation, activation, retry-idempotency, edit, pause/resume and archive all exercised in a browser on the LAN origin |
| One-screen creation paths | **DEPLOYED + LIVE VERIFIED** (desktop and 390px wide) |
| Candle-data warming/unavailability in the UI | **LAST ASSESSMENT**: measured live; a universe with unresolved members reports `UNAVAILABLE`, a universe needing history reports `WARMING` with a bounded fetch action, and both states are distinct from a zero-match result |

### 10.2 Still not tested (unchanged)

- Scheduled execution beyond the single coalesced `session_close` bucket.
- Currency (CDS/BCD) live validation; the capacity campaign; restart fault
  injection; ntfy delivery; delivery retry/backoff under provider failure.
- MCX daily-candle **backfill for arbitrary new contracts** beyond the bounded
  warming path (a recently listed contract legitimately reports insufficient
  history until it has traded enough sessions).
