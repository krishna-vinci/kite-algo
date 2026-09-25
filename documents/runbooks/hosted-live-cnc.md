# Runbook: hosted live CNC

Public lane name `cnc`. Shared procedures, env reference, halt ladder, and
refusal lookup: [README.md](README.md).

Authority for this lane: C1.1 staged financing design
(`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md`) and the
readiness plan's C1/C2 sections
(`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:114-140`).

## 1. Scope and prerequisites

**What it does live.** The CNC lane executes two plan kinds against the real
account: `target_weights` (portfolio rebalance) and `single_instrument` (one leg)
(`backend/strategies/live_service.py:66,1173`; lane constants
`backend/strategies/live_sequence.py:57-58`). A portfolio plan materializes
**reductions first**, then increases; each dependent buy is
materialized `withheld` and released only by its lane's release rule
(`backend/strategies/live_sequence.py:524-576`).

- A one-leg plan is `RULE_IMMEDIATE` and refuses a compound shape with
  `LIVE_PLAN_COMPOUND_UNSUPPORTED` (`backend/strategies/live_sequence.py:107,381-390`).
- A portfolio plan with reductions gates its dependent buys. A staged CNC
  rebalance uses `staged_funding_gate`; a non-staged one uses
  `all_prerequisites_filled` (`backend/strategies/live_sequence.py:109,115,570-576`).
- A gated buy is released only when every funding reduction is proven `filled`,
  funds are authoritative and fresh, the buy is authorized under the account
  reservation lock, and the quote is fresh and inside the drift bound
  (`backend/strategies/live_sequence.py:115`; gates at
  `backend/strategies/live_adapter.py:1040-1170`).
- CNC shorts and reductions that cross flat are refused
  `STAGED_CNC_SHORT_UNSUPPORTED` (`backend/strategies/admission.py:61,1069,1322`).
- Live `intent_bundle` is not a staged-CNC plan kind; only persisted
  `target_weights` enters the staged lane
  (`backend/strategies/admission.py:94`).
- Reductions are ordinary immediate work and are never gated by fundraising
  (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:7`).

**Required env/config.**

| Setting | Required for | Default |
| --- | --- | --- |
| `HOSTED_LIVE_ENABLED` | master live gate; without it the lane is not offered and launch/submission refuse | off (`backend/strategies/live_settings.py:27-37`) |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | the exact real account scope must be allowlisted or every route 403s | empty/deny (`backend/api/services/hosted_strategy_authz.py:38-48`) |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | must not be falsy, or queued dispatch never runs | enabled (`backend/strategies/execution_dispatcher.py:35-39`) |
| `ACCOUNT_INGEST_ENABLED` + `ACCOUNT_INGEST_ACCOUNT_SCOPES` | post-fill attribution/settlement needs account-wide fills; a live settlement proof refuses a book whose ingest is missing rather than reading it as flat | `true` / empty (`backend/app/bootstrap.py:705-708`; `backend/app/background.py:51-59`; `documents/hosted-strategies-live-deployment.md:169-190`) |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | freshness bound on margin/funds evidence | `60` (`backend/strategies/admission.py:77,134-142`) |
| `LIVE_STAGED_BUY_MAX_PRICE_DRIFT_PCT` | buy-notional drift bound before `LIVE_FINANCING_PRICE_DRIFT` | `0.005` (`backend/strategies/live_adapter.py:84,1001-1008`) |
| `SETTLEMENT_BROKER_SNAPSHOT_MAX_AGE_SECONDS` | settlement proof freshness | `60` (`backend/strategies/settlement.py:1101-1112`) |

**Migration head.** Code head is `20260925_000050`
(`backend/alembic/versions/20260925_000050_flatten_operations.py`). Verify the
deployed head read-only before enabling; see
[README.md](README.md#verification-commands-read-only).

**Services.** `finance-app` (API, migrations, live outcome consumer, dispatcher,
account ingest), `alerts-worker`, `strategy-runner`, `frontend-next`
(`compose.yml:88,144`; `compose.worker.yml:11`; `compose.supervisor.yml:13`).
Component names to watch: `live_outcome_consumer`
(`backend/app/bootstrap.py:107-134`), `hosted_execution_dispatcher`
(`backend/app/bootstrap.py:210-241`), `account_ingest`
(`backend/app/background.py:69`).

## 2. Enable

No real order may be placed without the owner's explicit authorization. Every
step marked **[owner approval]** needs the owner's explicit go-ahead before it is
run.

1. **[owner approval]** Confirm the lane is intended for the exact broker account
   and that no other live lane is being opened in the same change.
2. Read-only preflight (no mutation): migration head
   ([README.md](README.md#verification-commands-read-only)), service health,
   `GET /api/strategies/options` showing the current `account_scopes`,
   `live_lanes`, and `HOSTED_LIVE_ENABLED` state.
3. **[owner approval]** Add the real account scope to
   `HOSTED_STRATEGY_ACCOUNT_SCOPES` in the deployment's untracked `.env`. This is
   the expansion that was previously rejected by automatic review in
   `documents/hosted-strategies-live-deployment.md:203-230`.
4. **[owner approval]** Confirm the account-fill ingest scope
   (`ACCOUNT_INGEST_ACCOUNT_SCOPES`) covers the same account, otherwise live
   settlement cannot prove flatness
   (`documents/hosted-strategies-live-deployment.md:169-190`).
5. **[owner approval]** Follow the deployment order in [README.md](README.md#deployment-order-shared-c2-procedure):
   build -> recreate `finance-app` -> wait for migration + health -> recreate the
   other three services.
6. Verify (section 3) before configuring any strategy version.
7. Configure the strategy's `live` mode and select the allowlisted account scope;
   `live_requires_owner_approval` is always true
   (`backend/api/routers/strategies.py:581`).

## 3. Verify

All read-only.

```bash
# migration head, service health, deployed source hash: see README
DATABASE_URL=<dsn> alembic -c backend/alembic.ini current      # -> 20260925_000050
docker compose -f compose.yml -f compose.worker.yml -f compose.supervisor.yml ps
```

| Check | Healthy |
| --- | --- |
| `GET /api/strategies/options` | `live_lanes` includes `cnc`; `account_scopes` includes the real scope; `execution_modes` includes `live` (`backend/api/routers/strategies.py:554-583`) |
| `GET /api/system/runtime` | `live_outcome_consumer` running, `hosted_execution_dispatcher` running, `account_ingest` healthy with >= 1 account (`backend/app/bootstrap.py:107-134,210-241`; `backend/app/background.py:69,77-88`) |
| `GET /api/system/broker-login-health` | broker connected and session fresh (`backend/api/routers/auth.py:641-651`) |
| Deployed source hash | container file hashes equal the reviewed worktree (`documents/hosted-strategies-live-deployment.md:30-44`) |
| Owner UI `/strategies/[strategyId]` | Options panel renders; protection/pending sections report coverage `known` (not `unknown`) (`frontend-next/features/strategies/components/hosted-options-panel.tsx:96-130,960`; coverage contract `backend/api/services/owner_actions.py:66-68`) |
| Reservations | `GET /api/strategies/{strategy_id}/reservations` shows the expected plan's reservation as `active`/`renewed` (`backend/api/routers/strategies.py:1759`) |

In the owner UI, a healthy CNC strategy shows no `unknown` coverage banner, a
non-zero attributed book matching broker truth, and no pending-withheld buys whose
funding reduction is terminal-but-unfilled.

## 4. Halt

Ordered least to most drastic. Full semantics and the exact "never does" list:
[README.md](README.md#halt-ladder-shared).

1. **Stop evaluator** — `POST /api/strategies/{strategy_id}/jobs/{job_id}/stop`
   (`backend/api/routers/strategies.py:3189`). Does not cancel orders or flatten.
   Refusals: `STALE_ATTEMPT`, `STALE_LEASE_EPOCH`, `STOP_RACE_LOST`
   (`backend/api/routers/strategies.py:3205-3228`).
2. **Cancel pending work** — preview `GET .../owner-actions/pending-work`, then
   `POST .../owner-actions/cancel-pending`
   (`backend/api/routers/strategy_owner_actions.py:237,275`). Cancels only
   eligible ENTRY candidates; never reductions or protective work. Refusals:
   `CANCEL_EVIDENCE_CHANGED`, `CANCEL_ORDER_NOT_OWNED`,
   `CANCEL_PROTECTIVE_ORDER_FORBIDDEN`, `CANCEL_REDUCTION_FORBIDDEN`
   (`backend/api/services/owner_actions.py:84-87`).
3. **Flatten** — `POST .../owner-actions/flatten`
   (`backend/api/routers/strategy_owner_actions.py:338`; resume with `GET :377`).
   Resumable; stops the evaluator, cancels qualifying pending entry, and closes
   non-option books with target-zero reductions. **Live non-option reduction is
   not yet wired**: a live book is refused `FLATTEN_LIVE_NONOPTION_UNSUPPORTED`
   while completed option work is still reported
   (`backend/api/services/owner_actions.py:117`).
4. **Disable the lane** — set `HOSTED_LIVE_ENABLED` non-truthy, recreate
   `finance-app` ([README.md](README.md#rollback-doctrine-shared)). Does not close
   positions.

## 5. Repair

| Blocked state | Evidence required | Repair path | Never auto-resolved |
| --- | --- | --- | --- |
| Dependent buy stays `withheld` because a reduction is not confirmed | the reduction's own confirmed fills | wait for the reduction to fill; if it is rejected/cancelled with no fill the buy records `STAGED_FUNDING_REDUCTION_NOT_CONFIRMED` and stays in flight (`backend/strategies/live_service.py:610`; `backend/strategies/live_adapter.py:1065`) | the buy is never auto-released, and a *projected* sale never funds it (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:83`) |
| Reduction terminal cancel with a residual fill | the claim's `repair_required` state and its proven residual | owner disposition `POST .../plans/{plan_id}/residual` with `action="abandon"` (`backend/api/routers/strategies.py:2384`; `backend/api/schemas/proposals.py:163`); writes `residual_abandoned` (`backend/strategies/live_repair.py:78`) | never fabricates a fill or a rejection; refuses while the plan's authority could still fill the residual (`backend/strategies/live_repair.py:20-26`) |
| Dependent buy can never be funded (every funding leg terminal but unfilled, authority provably gone) | funding-leg evidence + proven-gone authority | staged dependent abandonment, disposition `staged_dependent_abandoned` (`backend/strategies/live_repair.py:84,622-624`) | unknown authority stays `LIVE_REPAIR_AUTHORITY_UNKNOWN` (`backend/strategies/live_repair.py:125`) |
| Unanswered plan step (unknown send / open remainder) | the platform's own paper/broker evidence | dead-submission disposition, one of `filled`/`rejected`/`cancelled`/`failed_never_submitted`/`failed_residual_abandoned` (`backend/api/routers/strategy_owner_actions.py:484,539`) | only dispositions the evidence supports are offered (`backend/api/services/owner_actions.py:3410-3450`) |
| Funds/margin evidence missing or stale | fresh authoritative funds read | leave `withheld`; blocker `STAGED_FUNDING_EVIDENCE_UNAVAILABLE` / `STAGED_FUNDING_EVIDENCE_STALE`; retries next bounded pass (`backend/strategies/live_adapter.py:1142-1167`) | missing evidence is never treated as zero headroom (`backend/strategies/plan_pipeline.py:174-180`) |

`LIVE_OPTION_RUN_LEDGER_INCONSISTENT` is not part of this lane (it is not present
in the code at all; see the options runbook).

## 6. Rollback

Follow [README.md](README.md#rollback-doctrine-shared): stop evaluator, cancel
pending entry, reduce the book (flatten where the governed live pipeline supports
it — see the C1 exit requirement below), then set `HOSTED_LIVE_ENABLED` non-truthy
and recreate `finance-app`.

Migrations are fix-forward. Do not attempt a downgrade as a rollback; the
`2026-09-22`/`2026-09-25` revisions narrow vocabularies or drop columns/tables
and will fail or destroy data
(`backend/alembic/versions/20260922_000041_live_release_recovery.py:24-27,70-78`;
`documents/hosted-strategies-live-deployment.md:260-261`).

## 7. Known limitations

- **Gated buys are market orders today.** C1 keeps market dispatch against the
  fake broker only; before any real account is enabled, gated live CNC buys must
  become bounded LIMIT orders. This is the C1 exit requirement, not yet
  implemented (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:128-129`;
  `documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:206-208`).
  The drift check bounds *estimated* cost, not market-order slippage
  (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:197`).
- **Live non-option flatten refuses** (`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`,
  `backend/api/services/owner_actions.py:117`), so a live CNC book must be reduced
  by hand or by a new governed plan; flatten alone will not zero it.
- **Live `intent_bundle` refuses** `LIVE_PLAN_KIND_UNSUPPORTED`
  (`backend/strategies/live_adapter.py:751`; `backend/strategies/live_sequence.py:894`).
- **Mixed-product portfolio legs** do not enter the staged lane; only
  all-CNC staged plans qualify (`backend/strategies/admission.py:94`;
  `documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:29-34`).
- **Stale reservations are never auto-renewed** during a dependent release;
  continuing requires the governed reservation/approval flow
  (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:211-212`).
