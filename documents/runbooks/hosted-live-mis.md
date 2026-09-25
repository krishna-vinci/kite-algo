# Runbook: hosted live MIS

Public lane name `mis`. Shared procedures, env reference, halt ladder, and
refusal lookup: [README.md](README.md).

## 1. Scope and prerequisites

**What it does live.** MIS is a single-leg intraday lane. The only plan kind it
admits is `single_instrument` (`backend/strategies/live_service.py:66,1174`), and
the plan's frozen product must be exactly `MIS` or the builder refuses
`LIVE_MIS_PRODUCT_REQUIRED` (`backend/strategies/live_sequence.py:416-440`).

- A **risk-increasing** MIS step is ordinary immediate work
  (`release_rule=RULE_IMMEDIATE`).
- A **risk-reducing** MIS step is materialized `withheld` under
  `RULE_MIS_SQUAREOFF` (`backend/strategies/live_sequence.py:118,445-458`). It is
  released only by the platform's own authority, never by a guessed exchange
  close (see the release rule at `backend/strategies/live_service.py:777-837`).
- Release authorities, in the order the rule checks them: the exchange-local
  square-off clock (`squareoff_clock`), an operator-requested stop
  (`operator_stop`), or the MIS stale-worker exit policy
  (`stale_worker_exit`). Otherwise the rule refuses `MIS_SQUAREOFF_NOT_DUE`
  (`backend/strategies/live_service.py:809-837`).
- The square-off schedule is exchange-local wall clock and delegates to the
  protection runtime's own resolver so the two cannot drift
  (`backend/strategies/mis_squareoff.py:47-76`). Defaults: `NSE:MIS`/`BSE:MIS`
  `15:20`, `NFO:MIS` `15:25`, `CDS:MIS` `16:45`, `MCX:MIS` `23:20`
  (`backend/strategies/mis_squareoff.py:47-53`).
- The exit quantity is clamped to the strategy's own attributed quantity at
  release (`backend/strategies/mis_squareoff.py:79`;
  `backend/strategies/live_sequence.py:417-424`).
- A multi-day MIS intent is refused at source validation,
  `MIS_OVERNIGHT_REFUSED`, which names CNC/NRML or futures/options as the right
  product (`backend/strategies/mis_policy.py:10-32,55-94`).

**Required env/config.** Same gate set as every lane — see
[README.md](README.md#environment-variables). The settings MIS specifically
depends on:

| Setting | Why | Source |
| --- | --- | --- |
| `HOSTED_LIVE_ENABLED` | master live gate | `backend/strategies/live_settings.py:27-37` |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | exact account allowlist | `backend/api/services/hosted_strategy_authz.py:38-48` |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | must not be falsy or dispatch never runs | `backend/strategies/execution_dispatcher.py:35-39` |
| `WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON` | optional schedule override the MIS rule and the protection runtime share | `backend/app/background.py:28-38`; `backend/strategies/mis_squareoff.py:66-70` |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | margin evidence freshness | `backend/strategies/admission.py:77,134-142` |
| stale-exit policy (`none`/`exit_on_worker_stale`) | arms the stale-worker exit authority | `backend/api/routers/strategies.py:581` (`stale_exit_policies`); `backend/strategies/live_service.py:827-836` |

**Migration head.** Code head `20260925_000050`
(`backend/alembic/versions/20260925_000050_flatten_operations.py`); verify the
deployed head read-only ([README.md](README.md#verification-commands-read-only)).

**Services.** `finance-app`, `alerts-worker`, `strategy-runner`, `frontend-next`
(`compose.yml:88,144`; `compose.worker.yml:11`; `compose.supervisor.yml:13`).
The MIS stale-worker exit depends on the runner publishing heartbeats
(`backend/strategies/live_service.py:1015-1060`).

## 2. Enable

No real order may be placed without the owner's explicit authorization. Every
step marked **[owner approval]** needs the owner's explicit go-ahead.

1. **[owner approval]** Confirm the lane is intended for the exact MIS account and
   that the strategy's intraday horizon is genuinely one session.
2. **[owner approval]** Add the account scope to `HOSTED_STRATEGY_ACCOUNT_SCOPES`
   and confirm account ingest covers it
   (`documents/hosted-strategies-live-deployment.md:169-190`).
3. **[owner approval]** Follow the deploy order in
   [README.md](README.md#deployment-order-shared-c2-procedure). MIS opens after
   CNC in the C2 lane order
   (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:134`).
4. Verify (section 3) before configuring a version.
5. Configure the strategy's `live` mode and select the allowlisted scope.

## 3. Verify

All read-only.

| Check | Healthy |
| --- | --- |
| `GET /api/strategies/options` | `live_lanes` includes `mis`; `stale_exit_policies` includes the configured value (`backend/api/routers/strategies.py:554-583`) |
| `GET /api/system/runtime` | `live_outcome_consumer` and `hosted_execution_dispatcher` running (`backend/app/bootstrap.py:107-134,210-241`) |
| `GET /api/system/broker-login-health` | broker connected, session fresh (`backend/api/routers/auth.py:641-651`) |
| `GET /api/strategies/{strategy_id}/squareoffs` | square-off evidence rows appear with a named outcome; unresolved outcomes are `action_required`/`missed_by_broker`, never settlement (`backend/api/routers/strategies.py:1717`; `backend/strategies/mis_squareoff.py:34-46`) |
| Deployed source hash | matches the reviewed worktree (`documents/hosted-strategies-live-deployment.md:30-44`) |
| Owner UI `/strategies/[strategyId]` | Options panel and pending/coverage sections read `known` (`frontend-next/features/strategies/components/hosted-options-panel.tsx:960`; `backend/api/services/owner_actions.py:66-68`) |

Healthy MIS: no withheld reducing step that has no release authority; the
square-off evidence for the session shows a terminal outcome (not
`action_required`); the attributed book matches broker truth.

## 4. Halt

Ordered least to most drastic. Full semantics: [README.md](README.md#halt-ladder-shared).

1. **Stop evaluator** — `POST /api/strategies/{strategy_id}/jobs/{job_id}/stop`
   (`backend/api/routers/strategies.py:3189`). For MIS this is also the release
   authority: a durable stop request makes `_mis_context.stop_requested` true, so
   a withheld reducing step is released under `operator_stop`
   (`backend/strategies/live_service.py:823-825,1058`). It still does not
   cancel or flatten anything by itself. Refusals: `STALE_ATTEMPT`,
   `STALE_LEASE_EPOCH`, `STOP_RACE_LOST`
   (`backend/api/routers/strategies.py:3205-3228`).
2. **Cancel pending work** — `GET .../owner-actions/pending-work` then
   `POST .../owner-actions/cancel-pending`
   (`backend/api/routers/strategy_owner_actions.py:237,275`). Cancels only
   eligible ENTRY candidates; never a reduction. Refusals:
   `CANCEL_EVIDENCE_CHANGED`, `CANCEL_ORDER_NOT_OWNED`,
   `CANCEL_PROTECTIVE_ORDER_FORBIDDEN`, `CANCEL_REDUCTION_FORBIDDEN`
   (`backend/api/services/owner_actions.py:84-87`).
3. **Flatten** — `POST .../owner-actions/flatten`
   (`backend/api/routers/strategy_owner_actions.py:338`). Resumable. For a live
   MIS book the non-option reduction is not yet wired, so flatten refuses
   `FLATTEN_LIVE_NONOPTION_UNSUPPORTED`; use the stop/square-off authority in
   lever 1 to reduce MIS instead (`backend/api/services/owner_actions.py:117`).
4. **Disable the lane** — `HOSTED_LIVE_ENABLED` non-truthy + recreate
   `finance-app`. Does not close positions
   ([README.md](README.md#rollback-doctrine-shared)).

Useful refusals while halting: `MIS_SQUAREOFF_NOT_DUE` means the release rule
has no authority yet (`backend/strategies/live_service.py:837`);
`LIVE_MIS_PRODUCT_REQUIRED` means the frozen product is not MIS
(`backend/strategies/live_sequence.py:436`).

## 5. Repair

| Blocked state | Evidence required | Repair path | Never auto-resolved |
| --- | --- | --- | --- |
| Reducing step `withheld`, square-off not yet due | exchange-local clock, operator stop, or a proven stale worker | wait for the clock, request a stop (lever 1), or let the stale-worker policy arm the release (`backend/strategies/live_service.py:809-837`) | never released on a guessed exchange close (`backend/strategies/live_sequence.py:419-424`) |
| Square-off shows `action_required` / `missed_by_broker` | the recorded square-off outcome | read it via `GET .../squareoffs`; the platform keeps reconciling and never treats it as settlement (`backend/strategies/mis_squareoff.py:40-46`) | unresolved outcomes are never settlement |
| Unanswered plan step (unknown send / open remainder) | the platform's own evidence | dead-submission disposition (`backend/api/routers/strategy_owner_actions.py:484,539`); only evidence-backed dispositions are offered (`backend/api/services/owner_actions.py:3410-3450`) | not auto-resolved, and a staged protective order refuses (`DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN`, `backend/api/services/owner_actions.py:98`) |
| Multi-day MIS intent | the intent's declared `hold_days` | none — the correct fix is a different product (CNC/NRML or futures/options); the refusal names it (`backend/strategies/mis_policy.py:55-94`) | never downgraded silently to intraday |

## 6. Rollback

Follow [README.md](README.md#rollback-doctrine-shared). For MIS the practical
order is: stop evaluator (which arms the reduce authority), confirm the
square-off evidence is terminal, then cancel pending entry, then disable the lane.
Because live non-option flatten refuses, do not rely on flatten to zero an MIS
book (`backend/api/services/owner_actions.py:117`).

Migrations are fix-forward; do not downgrade
([README.md](README.md#rollback-doctrine-shared)).

## 7. Known limitations

- **Live non-option flatten refuses** (`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`,
  `backend/api/services/owner_actions.py:117`); MIS books are reduced through the
  lane's own square-off/stop authority, not flatten.
- **MIS staged financing is out of scope.** MIS has its own square-off rule and
  does not enter the staged CNC lane
  (`documents/hosted-live-staged-financing-c1-1-design-2026-09-25.md:135`;
  `backend/strategies/live_sequence.py:416-458`).
- **Exposure-increasing MIS is immediate**, i.e. it is not gated behind a
  square-off; only reductions are. An operator halting MIS should expect an armed
  entry to have already been sent.
- **The square-off clock is exchange-local wall clock.** A container clock/zone
  mistake would mis-time it; the rule compares in `EXCHANGE_TZ`, not UTC
  (`backend/strategies/live_service.py:811-818`).
