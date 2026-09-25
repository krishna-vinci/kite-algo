# Hosted live lanes - operator runbooks

One runbook per live lane. They share this page: the deployment order, the env
vars, the read-only verification commands, the halt ladder, and the refusal
lookup live here once and the lane files link back to them.

Every claim below is cited to the file and line that implements it in this
checkout. Where a lane is not yet wired to a control the code does not have, the
lane file says so instead of describing a command that would fail.

## The lanes

| Lane | Runbook | Public lane name | Live plan kind(s) | Lane builders |
| --- | --- | --- | --- | --- |
| CNC | [hosted-live-cnc.md](hosted-live-cnc.md) | `cnc` | `target_weights`, `single_instrument` | `LANE_PORTFOLIO`, `LANE_SINGLE` |
| MIS | [hosted-live-mis.md](hosted-live-mis.md) | `mis` | `single_instrument` | `LANE_MIS` |
| Futures | [hosted-live-futures.md](hosted-live-futures.md) | `futures` | `target_futures` | `LANE_FUTURES_ROLL` |
| Options | [hosted-live-options.md](hosted-live-options.md) | `options` | `option_structure` | `LANE_OPTION_STRUCTURE` |

Lane -> plan-kind mapping: `backend/strategies/live_service.py:1173` (`_LANE_PLAN_KINDS`)
and the executor's admitted kinds at `backend/strategies/live_service.py:66`
(`LIVE_PLAN_KINDS`). Public lane names and their builders:
`backend/strategies/live_sequence.py:335` (`PUBLIC_LANE_NAMES`) and
`backend/strategies/live_sequence.py:343` (`hosted_live_lanes`).

**Nothing in this folder authorizes a real order.** No live order may be placed
without the owner's explicit authorization. The readiness plan states the same
boundary: no real-money order is placed by any step of the plan
(`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:12-14`).

## Services

Production runs the same images for every lane. Compose service names:

| Service | Defined at | Role in a live lane |
| --- | --- | --- |
| `postgres` | `compose.yml:2` | durable plans, reservations, runs, trail |
| `redis` | `compose.yml:32` | runtime coordination |
| `market-runtime` | `compose.yml:47` | quotes/ticks |
| `finance-app` | `compose.yml:88` | API + Alembic entrypoint + background loops |
| `frontend-next` | `compose.yml:144` | owner UI (`/strategies/[strategyId]`) |
| `alerts-worker` | `compose.worker.yml:11` | worker runtime |
| `strategy-runner` | `compose.supervisor.yml:13` | job supervisor (`python -m backend.strategies.supervisor`) |

The API owns migrations: its entrypoint runs the Alembic chain
(`documents/hosted-strategies-live-deployment.md:115-125`).

## Environment variables

Read from the process environment at request/cycle time (not cached), except the
adapter's quote bound, which is a constructor parameter.

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `HOSTED_LIVE_ENABLED` | off | master gate for hosted live. Only `1/true/yes/on` enable it; anything else (unset, typo) is false. | `backend/strategies/live_settings.py:21,24,27-37` |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | empty (deny all) | comma-separated exact account-scope allowlist; unlisted scope is 403 | `backend/api/services/hosted_strategy_authz.py:31,38-48,50-66` |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | enabled | disabled only for an explicit falsy spelling (`0/false/no/off/disabled`) | `backend/strategies/execution_dispatcher.py:25,32,35-39` |
| `ACCOUNT_INGEST_ENABLED` | `true` | starts the account-wide fill ingest loop | `backend/app/bootstrap.py:705-708` |
| `ACCOUNT_INGEST_INTERVAL_SECONDS` | `60` (min 5) | ingest cycle interval | `backend/app/background.py:51-54` |
| `ACCOUNT_INGEST_ACCOUNT_SCOPES` | empty | comma-separated `kite:<broker_user_id>` scopes to ingest | `backend/app/background.py:56-59` |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | `60` | freshness bound for live margin/funds evidence | `backend/strategies/admission.py:77,134-142` |
| `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` | `0.005` | max buy-notional drift vs. the frozen reference before `LIVE_FINANCING_PRICE_DRIFT` | `backend/strategies/live_adapter.py:84,1001-1008` |
| `LIVE_OPTION_CHAIN_MAX_AGE_SECONDS` | `5.0` | pre-send option chain/Greeks freshness bound | `backend/options/market/freshness.py:18,205-209` |
| `OPTION_CHAIN_MAX_AGE_SECONDS` (freeze-time) | `10.0` | freeze-time chain/Greeks age bound | `backend/options/market/freshness.py:17,112-115,169-170` |
| `SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS` | `60` | max age of the broker snapshot used to prove settlement | `backend/strategies/settlement.py:1101-1112` |
| `ADMISSION_RISK_MAX_LOSS_INR`, `ADMISSION_RISK_NOTIONAL_LIMIT_INR`, `ADMISSION_RISK_MARGIN_LIMIT_INR`, `ADMISSION_RISK_ALLOWED_STRUCTURE_FAMILIES`, `ADMISSION_RISK_EXPIRY_POLICIES`, `ADMISSION_RISK_NAKED_PERMITTED`, `ADMISSION_RISK_STOP_REQUIRED` | absent = no ceiling | platform risk ceilings; only ever tighten a strategy version's declared policy | `backend/strategies/risk_policy.py:89-99,232-264` |
| `HOSTED_SUPERVISOR_CREDENTIAL` | required (`:?`) | supervisor auth to the lifecycle API | `compose.supervisor.yml:24` |
| `HOSTED_SUPERVISOR_*` (lease, heartbeat, grace, health) | see file | supervisor timing/health windows | `compose.supervisor.yml:23-49`; reader `backend/strategies/supervisor.py:190-205` |

The adapter's **quote** bound is not an env var: it is the module constant
`QUOTE_MAX_AGE_SECONDS = 5.0` (`backend/strategies/live_adapter.py:77`), passed
into the executor at `backend/strategies/live_adapter.py:510` and enforced at
`backend/strategies/live_adapter.py:947-953`.

## Deployment order (shared C2 procedure)

Source of truth: `documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:132-140`
(lane order CNC -> MIS -> futures -> options; every step needs user approval) and
the recorded procedure in `documents/hosted-strategies-live-deployment.md:99-129`.

1. Confirm the required migration head and that no live account is armed
   (read-only, below). Owner approval: yes.
2. Set the lane's env (account scope, `HOSTED_LIVE_ENABLED`) in the deployment's
   untracked `.env`. Owner approval: yes.
3. Build all images from the reviewed worktree, before replacing any container
   (`documents/hosted-strategies-live-deployment.md:18-30`). Owner approval: yes.
4. Recreate `finance-app` first (it owns Alembic), wait for migration + health.
   Owner approval: yes. Command shape:
   `documents/hosted-strategies-live-deployment.md:118-122`.
5. Recreate `alerts-worker`, `strategy-runner`, `frontend-next`. Owner approval: yes.
6. Verify migration head, service health, deployed source hashes, auth
   boundaries, startup logs, and that no unintended jobs/orders/notifications
   were created
   (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:138-140`).
7. Record the rollout in `documents/hosted-strategies-rollout-<lane>-<date>.md`.

## Verification commands (read-only)

Run all of these before enabling, and again after any recreate.

Migration head:

```bash
# Read, do not upgrade. backend/alembic/env.py resolves DATABASE_URL from the env.
DATABASE_URL=<deployment dsn> alembic -c backend/alembic.ini current
# or: SELECT version_num FROM alembic_version;
```

Current code head in this checkout is `20260925_000050`
(`backend/alembic/versions/20260925_000050_flatten_operations.py`); the last
recorded production head is `20260924_000043`
(`documents/hosted-recurring-portfolios-deployment-2026-09-24.md:20`; the earlier
`20260922_000041` in `documents/hosted-strategies-live-deployment.md:94` is
superseded). Any lane rollout must first land the `20260925_000044` through
`20260925_000050` revisions (and any later ones).

Service health + migration log:

```bash
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml ps
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml \
  logs --since 10m finance-app strategy-runner alerts-worker frontend-next
```

Control-plane components (`GET /api/system/runtime`,
`backend/api/routers/auth.py:279-301`) — `healthy` looks like:

| Component | Healthy value | Set at |
| --- | --- | --- |
| `live_outcome_consumer` | running/ok state, no `last_error` | `backend/app/bootstrap.py:107-134` |
| `hosted_execution_dispatcher` | running, `last_counts` advancing | `backend/app/bootstrap.py:210-241` |
| `account_ingest` | `healthy`, `accounts` >= 1, `failed: []` | `backend/app/background.py:69,77-88` |
| `app` | `running` | `backend/app/bootstrap.py:293` |

If `HOSTED_LIVE_ENABLED` is off the consumer reports `disabled` with an explicit
detail (`backend/app/bootstrap.py:79-84`) — that is the off state, not a fault.

Deployed source hash (prove the running container matches the worktree):

```bash
docker exec kite-finance-app sha256sum /app/backend/strategies/live_adapter.py
# compare with: sha256sum backend/strategies/live_adapter.py
```

File list and the recorded hashes are in
`documents/hosted-strategies-live-deployment.md:30-44`.

Capabilities / selectors (`GET /api/strategies/options`,
`backend/api/routers/strategies.py:554-583`):

- `live_lanes` is populated **only** while `HOSTED_LIVE_ENABLED` is on, and only
  with lanes whose builder is registered **and** whose plan kind the executor
  admits (`backend/strategies/live_service.py:1181-1193`).
- `execution_modes` includes `live` only while live is enabled
  (`backend/api/routers/strategies.py:570-576`).
- `account_scopes` comes from the server allowlist; the browser cannot invent one
  (`backend/api/routers/strategies.py:569-570`).
- `live_requires_owner_approval` is always `true`
  (`backend/api/routers/strategies.py:581`).

Broker/session readiness: `GET /api/system/broker-login-health`
(`backend/api/routers/auth.py:641-651`). Recorded snapshot of a good answer:
`documents/hosted-strategies-live-deployment.md:149-157`.

Owner UI: the Options panel renders on the strategy detail page
(`frontend-next/features/strategies/components/hosted-strategy-detail-page.tsx:45,511`;
component `frontend-next/features/strategies/components/hosted-options-panel.tsx:1620`).
Read the lane file for what "healthy" shows there.

Source readiness before storing a strategy version:
`POST /api/strategies/readiness` (`backend/api/routers/strategies.py:585`), a
static AST check that never imports or runs the source
(`backend/strategies/readiness.py:1-20`).

## Halt ladder (shared)

Least to most drastic. Every route is owner-authenticated: the owner is derived
server-side from the session (`require_strategy_owner`,
`backend/api/routers/strategies.py:204-206`), and each mutation enforces
same-origin.

| # | Lever | Route | What it does | What it never does |
| --- | --- | --- | --- | --- |
| 1 | Stop evaluator | `POST /api/strategies/{strategy_id}/jobs/{job_id}/stop` (`backend/api/routers/strategies.py:3189`) | queued job -> stopped; `starting`/`running` gets a durable stop request the supervisor observes for bounded cleanup | does **not** cancel orders or flatten positions; does not clear the replacement block (`backend/api/routers/strategies.py:3197-3200`) |
| 2 | Cancel pending work | `POST .../owner-actions/cancel-pending` (`backend/api/routers/strategy_owner_actions.py:275`); preview `GET .../owner-actions/pending-work` (`:237`) | cancels only ENTRY candidates the preview proved eligible; preserves a partial fill | never touches protective hedges, reductions, exit/adjust/roll/square-off work, or unowned/unreadable orders (`backend/api/services/owner_actions.py:57-61,76,84-87`) |
| 3 | Exit structure (options, per run, short-first) | `GET`/`POST /api/strategies/{strategy_id}/option-runs/{option_run_id}/exit` (`backend/api/routers/strategy_owner_actions.py:399,430`) | one governed stage of the staged structure exit derived from the run's own confirmed fills; shorts first, hedge withheld until its short is proven closed | never marks the run exited because a broker accepted a stage; completion is the run's own fills proving flat (`backend/options/execution/repair.py:73-77`) |
| 4 | Flatten (strategy-scoped, resumable) | `GET`/`POST /api/strategies/{strategy_id}/owner-actions/flatten` (`backend/api/routers/strategy_owner_actions.py:377,338`) | stops the evaluator and proves it, cancels qualifying pending entry, exits option runs one at a time, closes non-option books with target-zero reductions; a second POST resumes and preserves finished work | not a whole-account liquidation; refuses rather than guessing an unanswered order; a plan that would increase exposure is refused before admission (`backend/api/services/owner_actions.py:108-133`) |
| 5 | Disable the lane | set `HOSTED_LIVE_ENABLED` non-truthy, recreate `finance-app` (`documents/hosted-strategies-live-deployment.md:118-122`) | live admission/launch/submission refuse; the outcome consumer reports `disabled` | does not close broker positions; there is no route that liquidates the book on flag-off. Removing the account scope from `HOSTED_STRATEGY_ACCOUNT_SCOPES` similarly 403s the lane (`backend/api/services/hosted_strategy_authz.py:50-66`) |

Stop-evaluator refusals (409): `STALE_ATTEMPT`, `STALE_LEASE_EPOCH`,
`STOP_RACE_LOST` (`backend/api/routers/strategies.py:3205-3228`).
Cancel-pending refusals: `CANCEL_EVIDENCE_CHANGED`, `CANCEL_ORDER_NOT_OWNED`,
`CANCEL_PROTECTIVE_ORDER_FORBIDDEN`, `CANCEL_REDUCTION_FORBIDDEN`
(`backend/api/services/owner_actions.py:84-87`).
Flatten refusals: `FLATTEN_EVALUATION_ACTIVE`, `DEAD_SUBMISSION_UNRESOLVED`,
`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`, `FLATTEN_PLAN_INCREASES_EXPOSURE`,
`FLATTEN_REDUCTION_RUN_UNBOUND`, `FLATTEN_REDUCTION_PLAN_REFUSED`,
`FLATTEN_REDUCTION_PIPELINE_UNAVAILABLE`, `FLATTEN_REDUCTION_INSTRUMENT_UNKNOWN`,
`FLATTEN_UNATTRIBUTED_EXPOSURE`, `FLATTEN_OPTION_RUN_COVERAGE_UNKNOWN`
(`backend/api/services/owner_actions.py:108-133`).

Flatten is `complete` only when every done-condition holds:
`no_qualifying_pending_entry`, `no_live_unresolved_submission`, `option_runs_flat`,
`books_zero`, `no_in_flight_governed_work`, `no_live_evaluation_authority`
(`backend/api/services/owner_actions.py:151-156`).

## Repair doctrine (shared)

Named blocked states are repaired by evidence, never by guessing. The evidence
each requires and what is never auto-resolved is in each lane file; the shared
primitives are:

- **Dead submission** (`GET`/`POST .../plans/{plan_id}/steps/{step_no}/dead-submission`,
  `backend/api/routers/strategy_owner_actions.py:484,539`). The five dispositions
  are `filled`, `rejected`, `cancelled`, `failed_never_submitted`,
  `failed_residual_abandoned` (`backend/api/services/owner_actions.py:79-85`); the
  platform permits only the ones its own evidence supports
  (`backend/api/services/owner_actions.py:3410-3450`).
- **Live residual abandonment** (`POST .../plans/{plan_id}/residual` with
  `action="abandon"`, `backend/api/routers/strategies.py:2384`; the only action is
  `abandon`, `backend/api/schemas/proposals.py:163`). Writes `residual_abandoned`
  to the append-only trail, never a fabricated fill
  (`backend/strategies/live_repair.py:1-32,78`).
- **Staged dependent abandonment** — a `withheld` buy whose funding legs are all
  terminal-but-unfilled and whose authority is provably gone
  (`backend/strategies/live_repair.py:622-624`), disposition
  `staged_dependent_abandoned` (`backend/strategies/live_repair.py:84`).
- **Protection ownership** (options): `OPTION_PROTECTION_OWNER_UNKNOWN` /
  `OPTION_PROTECTION_OWNER_CONFLICT` / `OPTION_PROTECTION_OWNER_REQUIRED` /
  `OPTION_PROTECTION_RELEASE_NOT_TERMINAL` / `OPTION_PROTECTION_ACTION_STATE_INVALID`
  (`backend/options/protection/ownership.py:112-118`). Unknown ownership blocks
  new exposure but never blocks risk-reducing work; a conflict means the run has
  been handed to another owner and must not act
  (`backend/options/execution/plan_binding.py:527-540,1236-1245`).

Never auto-resolved: an order whose send is unknown, a terminal cancel with a
residual fill, an ambiguous option run, a protective stage that is unresolved,
and any disposition the platform's own evidence does not support.

## Rollback doctrine (shared)

Disable the lane without stranding positions, in this order:

1. Stop evaluator (ladder 1) and confirm the job is terminal — `TERMINAL_JOB_STATUSES`
   is `stopped`, `failed`, `recovery_required`
   (`backend/api/services/owner_actions.py:162`).
2. Cancel qualifying pending ENTRY work (ladder 2). Leave protective/exit work alone.
3. Exit option runs one at a time (ladder 3) or flatten the strategy (ladder 4).
   A live non-option book that cannot be reduced by the governed live pipeline is
   refused by name (`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`,
   `backend/api/services/owner_actions.py:117`), so it must be reduced by the lane
   that owns it.
4. Set `HOSTED_LIVE_ENABLED` non-truthy and recreate `finance-app`. Live
   admission/launch/submission then refuse; existing broker positions are untouched.

**Migrations are fix-forward.** Downgrades narrow CHECK vocabularies or drop
columns/tables, so they fail while data exists — that is intentional. The
recovery-event migration spells it out: downgrade "restores the previous
vocabulary, which fails while a recovery row exists - the correct fix-forward
posture" (`backend/alembic/versions/20260922_000041_live_release_recovery.py:24-27,70-78`).
Later revisions that destroy data on downgrade: the `risk_policy` column
(`backend/alembic/versions/20260925_000046_strategy_version_risk_policy.py`,
`downgrade`), protection-owner tables
(`backend/alembic/versions/20260925_000047_option_protection_owners.py`,
`downgrade`), and flatten operations
(`backend/alembic/versions/20260925_000050_flatten_operations.py`, `downgrade`).
The deployment report records the same posture: "Migration downgrade is
intentionally not a rollback path: it fails while live vocabulary rows exist"
(`documents/hosted-strategies-live-deployment.md:260-261`).

## Refusal lookup

| Code | Meaning | Source |
| --- | --- | --- |
| `LIVE_ADMISSION_REFUSED` | admission refused a live leg; inner `reason_code` names why | `backend/strategies/live_adapter.py:990` |
| `LIVE_QUOTE_MISSING` / `LIVE_QUOTE_STALE` / `LIVE_QUOTE_MISMATCH` | quote absent, older than 5 s, or wrong instrument | `backend/strategies/live_adapter.py:939-957` |
| `LIVE_EVALUATION_AUTHORITY_MISSING` / `_MISMATCH` / `_STALE` | the start request's authority is gone, changed, or expired | `backend/strategies/live_adapter.py:794-814` |
| `LIVE_APPROVAL_INVALID` | an approval pin moved | `backend/strategies/live_adapter.py:819-835` |
| `EXPOSURE_SNAPSHOT_CHANGED` | the one approval pin a dependent release may explain away | `backend/strategies/live_adapter.py:86-98` |
| `LIVE_AUTHORITY_EVIDENCE_UNAVAILABLE` | authority cannot be read | `backend/strategies/live_adapter.py:2277-2297` |
| `LIVE_CAPACITY_SHORTFALL` / `ACCOUNT_FUNDS_UNSECURED` | reservations cannot cover the leg | `backend/strategies/live_adapter.py:1485,1964,2002,2019` |
| `LIVE_PLAN_KIND_UNSUPPORTED` / `LIVE_PLAN_COMPOSITION_EMPTY` / `LIVE_PLAN_COMPOUND_UNSUPPORTED` | plan shape the lane/executor will not take | `backend/strategies/live_adapter.py:751`; `backend/strategies/live_sequence.py:493,793,894,939` |
| `LIVE_TARGET_MISSING` / `LIVE_UNITS_UNPINNED` | frozen target or lot missing | `backend/strategies/live_adapter.py:1260-1339`; `backend/strategies/live_sequence.py:648` |
| `LIVE_POSITION_EVIDENCE_UNAVAILABLE` | authoritative attributed reader unavailable | `backend/strategies/live_sequence.py:680`; `backend/strategies/live_readers.py:120,152` |
| `LIVE_BROKER_SESSION_UNAVAILABLE` / `LIVE_ACCOUNT_SCOPE_REQUIRED` | broker/session boundary cannot serve the read | `backend/strategies/live_readers.py:35,60` |
| `LIVE_QUOTE_INSTRUMENT_MISMATCH` | reader returned a quote for a different instrument | `backend/strategies/live_readers.py:219` |
| `HOSTED_STOP_REQUESTED` | a stop is queued against the job | `backend/strategies/live_repair.py:130-134` |
| `LIVE_REPAIR_*` | residual/staged-dependent disposition refusals | `backend/strategies/live_repair.py:1-32,87-98` |

Refusals raise HTTP 409 with `rejection_reason` for owner actions
(`backend/api/services/owner_actions.py:211-231`) and for option-run repair
(`backend/options/execution/repair.py:126-135`); admission verdicts carry
`refusal_reason` in the response body (`backend/strategies/admission.py:43-64`).

## Related documents

- Production rollout + lane order:
  `documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md`
- Deployment procedure + recorded evidence:
  `documents/hosted-strategies-live-deployment.md`
- CNC staged financing design (C1.1):
  `documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md`
- Live options readiness design (C1.2):
  `documents/hosted-live-options-c1-2-design-2026-09-25.md`
- Owner actions design (B2.6b):
  `documents/hosted-owner-actions-b2-6b-design-2026-09-25.md`
- Protection ownership design (B2.4):
  `documents/hosted-options-b2-4-protection-ownership-design-2026-09-25.md`
