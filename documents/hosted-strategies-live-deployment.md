# Hosted strategies — Phase 3 production deployment

Date: 2026-09-22. Deployed revision: `development` **f0c747c** (`feat(strategies): wire all
hosted live lanes`), already pushed to origin (`852ff9b..f0c747c`). Contracts and
acceptance evidence: `documents/hosted-strategies-live-release-plan.md`,
`hosted-strategies-live-phase1.md`, `live-phase2a.md`, `live-phase2b.md`,
`hosted-strategies-live-ui.md`.

## Authorization and boundaries

Authorized: required production migrations, build, rollout, read-only broker
readiness, and `HOSTED_LIVE_ENABLED=true`. Not authorized and not done: real orders,
real notifications, creating or activating a live/test strategy, changing existing
paper modes or schedules, market-runtime/Redis/PostgreSQL rebuilds or volume changes.
Fix-forward only; no rollback rehearsal was requested or performed. No secret values
appear here; account identifiers are masked to a three-character prefix.

## Deployed images

Built **before** any container was replaced, from the reviewed worktree, with the
`compose.yml` + `compose.worker.yml` + `compose.supervisor.yml` overlays:

| Service | Image | Image ID | Built (IST) | Container started (UTC) |
| --- | --- | --- | --- | --- |
| finance-app | `kite-algo-finance-app:latest` | `3da83f7ee446` | 17:14:59 | 11:55:15 (second start, after the ingest config) |
| alerts-worker | `kite-algo-alerts-worker:latest` | `4a217a03e4a6` | 17:14:59 | 11:49:14 |
| strategy-runner | `kite-algo-strategy-runner:latest` | `9a726526234d` | 17:11:55 | 11:49:14 |
| frontend-next | `kite-algo-frontend-next:latest` | `8cba849c8af9` | 17:12:52 | 11:49:14 |

The Dockerfiles carry no revision label, so the revision was proved by content:

| File | sha256 inside the running container = worktree |
| --- | --- |
| `/app/backend/strategies/live_adapter.py` | `c6a9b754de3b…` |
| `/app/backend/strategies/live_sequence.py` | `212b880bdcf2…` |
| `/app/backend/strategies/live_ingestion.py` | `b4019593a042…` |
| `/app/backend/alembic/versions/20260922_000041_live_release_recovery.py` | `49c64189f55e…` |
| `/app/backend/api/routers/strategies.py` | `5943847b1517…` |
| `/app/backend/app/bootstrap.py` | `55cf25916590…` |

`strategy-runner` carries the same `live_settings.py` / `supervisor.py`; the frontend
image's compiled output contains `live_lanes` and the `/strategies/[strategyId]`
route. `postgres`, `redis` and `market-runtime` were never recreated (uptime 6-11
days) and no volume was changed.

## Migration proof (disposable PostgreSQL 15433 only)

Production is `kite-postgres` on 15432; every proof database below lived on the
throwaway 15433 instance under a unique name and was dropped by the proof script.
`backend/alembic/env.py` resolves the URL from the environment, so the CLI was run as
`DATABASE_URL=<disposable dsn> alembic -c backend/alembic.ini upgrade …`.

From zero to head:

```
alembic upgrade head            # -> 20260922_000041 (exit 0)
live_plan_executions table      # present
live_plan_submissions           # consumer_token + consumer_until present
strategy_plan_execution_events  # broker_order_id present
```

Constraint vocabularies at head, and a savepoint-rolled-back insert proving each
widened CHECK really admits live rows:

```
live_plan_submissions state CHECK  pending, withheld, releasing, partial, finalizing,
                                   rejecting, repair_required, residual_abandoned,
                                   filled, uncertain, rejected, no_op
strategy_plan_execution_events     submitted, filled, partially_filled, rejected, failed,
                                   no_op, residual_abandoned, release_recovered
live row admitted by: strategies, hosted_strategies, hosted_strategy_versions,
                      hosted_strategy_schedules, strategy_jobs, algo_worker_runs,
                      option_strategy_runs
```

Production head `20260915_000024` to head with 13 representative rows seeded at the
old revision (unique digest per table, unchanged after the upgrade):

| Table | Rows | Digest (16) | After upgrade |
| --- | --- | --- | --- |
| `account_positions` | 1 | `d9458c96259aa576` | unchanged |
| `algo_worker_runs` | 1 | `547e8e5ae93e6709` | unchanged |
| `hosted_strategies` | 1 | `92a263fc0ced19b3` | unchanged |
| `hosted_strategy_schedules` | 1 | `c8a86696d37461b2` | unchanged |
| `hosted_strategy_versions` | 1 | `6bf48866e9a7e839` | unchanged |
| `kite_sessions` | 1 | `f23cf11bfdbe73ce` | unchanged |
| `live_order_intents` | 1 | `df7b5996e8fb9b50` | unchanged |
| `option_strategy_runs` | 1 | `4f75f07a259f0663` | unchanged |
| `order_state_projection` | 1 | `a7b7d68705bf5189` | unchanged |
| `order_trade_fills` | 2 | `9852de2ad211fd54` | unchanged |
| `strategy_jobs` | 1 | `613d8ff4b618a163` | unchanged |
| `worker_live_execution_links` | 1 | `6c5bf665f53e0236` | unchanged |

Result: seeded revision `20260915_000024`, final revision `20260922_000041`,
`preserved=True`, no migration defect found; the live-admission probes passed again
on the upgraded database. Full log `/tmp/live_deploy/migration-proof.log`, JSON
`/tmp/live_deploy/migration-proof.json`.

## Production preflight and rollout

Read-only recheck immediately before rollout (via the container's local socket, no
credential printed): one hosted strategy (disabled, paper), 0 schedules, 2 stopped
paper jobs, **no** non-stopped strategy job, 0 rows in `account_positions`,
`alembic_version = 20260915_000024`, no canonical orders/signal events/deliveries in
24 h. Pre-existing and unrelated: 22 non-terminal `order_state_projection` rows and
12 open legacy `algo_worker_runs` (paper/dry-run plus three August `live` runs) —
none touched or reconfigured.

Configuration change (deployment-local file, untracked: `/home/krishna/kite-algo/.env`):

```
HOSTED_LIVE_ENABLED=true
```

Rollout, API first because its entrypoint owns Alembic:

```
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d --no-deps finance-app
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d --no-deps alerts-worker strategy-runner frontend-next
```

The API logged the full chain `20260915_000024 → … → 20260922_000041` and was healthy
~40 s later; production `alembic_version` is `20260922_000041` and
`live_plan_executions` / `live_plan_submissions` are empty. All four services are
healthy (`docker compose … ps`). No error or traceback in the API, worker, runner or
frontend startup logs beyond a pre-existing pydantic v2 `orm_mode` deprecation
warning.

## Post-deploy verification

Authenticated `GET /api/strategies/options` (operator token minted in-process by the
app's own issuer, stored nowhere, never printed):

```
execution_modes                 [paper, dry_run, live]
live_lanes                      [cnc, mis, futures, options]
live_requires_owner_approval    true
job_kinds                       [continuous, finite]
stale_exit_policies             [none, exit_on_worker_stale]
account_scopes                  1 entry, a paper scope (kite:pa…) [deployed snapshot;
                                the requested two-scope snapshot was not applied]
```

Boundary: unauthenticated `/api/strategies/options` and `/api/strategies` → `401`;
the published frontend `/strategies` → `307` to `/login?next=%2Fstrategies`.

Read-only broker/session/quote readiness:

```
broker connected, mode system, /api/profile_kite 200
system session refreshed 2026-09-22 11:47:41 UTC, daily_token_gate ready, websocket CONNECTED
/api/margins -> 401 "Not authenticated; login first"   (per-user endpoint, unchanged)
WorkerMarketDataService().get_quotes(738561) -> 1 quote, RELIANCE, is_stale false, last_price 1240.4
live_outcome_consumer -> status ok, 0 pending / 0 partial / 0 repair_required / 0 resolved
```

Deployment created no work:

```
strategy_jobs 2 (0 non-stopped)   schedules enabled 0
live_plan_executions 0            live_plan_submissions 0
strategy_plan_execution_events 0
canonical_order_events 24h 0      order_trade_fills 24h 0
signal_events 24h 0               deliveries 24h 0
```

## Account-ingest readiness configuration (added after review)

The API reported `account_ingest: "No accounts configured for ingest"`, which is a
real blocker for an authorized live release: the live settlement proof refuses a
book whose account ingest is missing rather than reading it as flat.

Configuration contract (`backend/app/background.py`):

| Setting | Meaning | Default |
| --- | --- | --- |
| `ACCOUNT_INGEST_ENABLED` | starts the loop | `true` |
| `ACCOUNT_INGEST_INTERVAL_SECONDS` | cycle interval | `60` |
| `ACCOUNT_INGEST_ACCOUNT_SCOPES` | comma-separated `kite:<broker_user_id>` scopes, resolved server-side through `kite_sessions.broker_user_id` | empty |

The scope was derived, not guessed: the deployment's own `KITE_USER_ID` matches the
broker identity of the **system** session (`kite_sessions.session_id='system'`,
refreshed today) and of the four same-day user sessions, and the same identity
already keys production `order_trade_fills`, `worker_live_execution_links` and
`live_order_intents`. The other stored identity is a 5-month-old April login and was
left alone. `ACCOUNT_INGEST_ENABLED=true` and
`ACCOUNT_INGEST_ACCOUNT_SCOPES=kite:XJ…` (one entry, the deployment's own account)
were appended to `.env`; only `finance-app` was recreated.

`account_ingest_state` result: `idle`, generation 1 then 2 then 3 then 4 (still
advancing, ~one cycle per minute), each with a fresh
`last_complete_ingest_at`; the component reports
`{"accounts": 1, "failed": [], "interval_seconds": 60.0}`. The cycle is
`OrdersService.trades()`, the account-wide trade book, read-only; it sends no order
and no notification. Today's trade book holds no new fills, so `order_trade_fills`
stayed at 72 rows (42 + 30 across the two historical identities).

Final API container start (the restart that applied these settings):
`2026-09-22T11:55:15.969980837Z` on image `3da83f7ee446`.

## Account-scope allowlist: deployed snapshot vs requested final snapshot

`HOSTED_STRATEGY_ACCOUNT_SCOPES` is a comma-separated allowlist of exact account
identities, default-deny, read from the process environment on every request
(`backend/api/services/hosted_strategy_authz.py`). Two snapshots exist for this
release:

| Snapshot | Allowlist (masked) | State |
| --- | --- | --- |
| Deployed and verified today | `kite:pa…` (one paper scope) | In effect; `/api/strategies/options` reports 1 `account_scopes` entry, re-verified after the revert |
| Requested final | `kite:pa…`, `kite:XJ…` (paper scope retained + the deployment's own verified broker account) | **Not applied.** The host's automatic approval review rejected the API restart that would apply it, reason quoted below; `.env` was restored to the deployed one-scope state so the widened allowlist is not silently armed |

The requested entry was the exact previously-verified own account (the identity that
`KITE_USER_ID`, the `system` `kite_sessions` row, and the existing
`order_trade_fills` / `worker_live_execution_links` / `live_order_intents` rows all
agree on). No wildcard and no other account was added. The blocked action was:

```
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  up -d --no-deps finance-app
```

Auto-review reason, quoted: "Adding the real broker account to the persistent
hosted-strategy allowlist enables live strategy configuration against a real-money
account; live-mode deployment authorization does not clearly authorize this separate
account-scope expansion." Until that is explicitly approved, a live plan cannot be
configured because no broker scope is offered; the capability contract and lanes
otherwise remain as verified below.

## Remaining operational needs (not blockers of this deployment)

* **Live launch is blocked on an explicit allowlist approval.** `/api/strategies/options`
  offers exactly one account scope and it is a paper scope, so no broker scope can be
  selected; the requested two-scope snapshot is prepared but unapplied (see the
  account-scope section) because the host's automatic approval review rejected the
  API restart that applies it.
* **Configured live is not proven live.** No order was submitted; the production
  broker-acceptance → fill → settlement path is unexercised by design.
* The attributed-position projection has never been published
  (`strategy_position_projection` = 0 rows). The live reader correctly treats that as
  unknown rather than flat, and no publish/bootstrap hook was exercised here, so the
  path by which a first strategy would publish its book is untested in this
  deployment. No automatic bootstrap is claimed.
* `account_positions` remains empty; broker flatness is **not** asserted from it.
* Three legacy `live` `algo_worker_runs` stay open (August) and
  `worker_runtime_stale_recovery` reports `scanned 2 / stale_detected 2 /
  action_required 2`; they predate this release and were neither activated nor
  reconfigured.
* `algo_runtime` reports `degraded` because legacy instance
  `option-strategy:839a07ec…` is skipped as `unregistered_type`; pre-existing and
  unrelated to the hosted live path.
* Pending cleanup (auto-review rejected the restore twice, not retried): the
  disposable 15433 instance's `postgres` database holds an empty 107-table schema at
  `20260915_000024` that my first probe created. It contains no data (only
  `alembic_version` and the baseline `benchmark_definitions` seed row). Restore
  command: `DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA
  public TO postgres;`
* Migration downgrade is intentionally not a rollback path: it fails while live
  vocabulary rows exist (fix-forward posture).
* `.env` is gitignored, so `HOSTED_LIVE_ENABLED` and the ingest settings are
  deployment-local and will not appear in the commit.

## Files changed by this bundle

| Path | Change |
| --- | --- |
| `documents/hosted-strategies-live-deployment.md` | **New**: this report |
| `.env` (untracked/ignored) | `HOSTED_LIVE_ENABLED=true`, `ACCOUNT_INGEST_ENABLED=true`, `ACCOUNT_INGEST_ACCOUNT_SCOPES=kite:XJ…` (own account, masked here and in this report); `HOSTED_STRATEGY_ACCOUNT_SCOPES` left at the deployed one-scope `kite:pa…` value after the blocked change was reverted |

Preserved untouched: the modified `documents/hosted-strategies-architecture-r1.md` and
every untracked architecture/roadmap/proposal/visual/`.commandcode` file.
