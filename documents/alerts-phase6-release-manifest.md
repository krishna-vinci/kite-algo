# Alerts Phase 6 — release manifest

Prepared 2026-09-13 for the closure build on branch `development`. This is the
operator-facing deployment plan: what to rebuild, what to configure, in what
order, and how to roll back. **No secret values appear here.**

> **Deployment status: PENDING.** The revision is built and verified locally but
> has not been deployed from this machine. Do not read this manifest as evidence
> of a live deployment.

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
