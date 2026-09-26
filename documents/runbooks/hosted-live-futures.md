# Runbook: hosted live futures

Public lane name `futures`. Shared procedures, env reference, halt ladder, and
refusal lookup: [README.md](README.md).

## 1. Scope and prerequisites

**What it does live.** The futures lane is one pinned contract with a pinned lot
size, admitted as plan kind `target_futures` (`backend/strategies/live_service.py:75,1217`),
and its only step builder is `build_futures_steps`
(`backend/strategies/live_sequence.py:612`). Two shapes:

- **`open_new`** (or a plan that is not part of a roll) is an ordinary
  `RULE_IMMEDIATE` step, sized by the adapter's frozen attribution-based delta
  (`backend/strategies/live_sequence.py:612-616,722-740`).
- **`close_old`** is the other plan of a roll, materialized `withheld` under
  `RULE_ROLL_CLOSE_RELEASED` (`backend/strategies/live_sequence.py:122,690-719`). It
  is released only when the roll's own state machine reaches `releasing_old`,
  i.e. the FULL required replacement quantity is proven filled by the roll's own
  recorded replacement executions
  (`backend/strategies/live_service.py:881-934`). A partial, rejected, stalled or
  unknown acquisition never reaches that state and refuses
  `BLOCKER_ROLL_CLOSE_NOT_RELEASED` / a named roll refusal
  (`backend/strategies/live_service.py:896,906,928-934`).
- The close is an **absolute flat** for this strategy's own attributed book, not a
  target-minus-current delta, and the released quantity is clamped again to the
  current attributed quantity
  (`backend/strategies/live_sequence.py:623-632`).
- A released `close_old` is a bounded platform-side LIMIT, never MARKET: the SELL
  limit is at least `max(bid, reference * 0.995)`, and the price is derived at
  release time (`backend/strategies/live_limit_orders.py:85-96,269-421`). A working
  gated LIMIT that times out is cancelled once and becomes terminal
  (`backend/strategies/live_adapter.py:2224-2590`).
- A future leg with no pinned lot refuses `LIVE_UNITS_UNPINNED`
  (`backend/strategies/live_sequence.py:645-652`). A `close_old` leg with no
  authoritative attributed reader refuses `LIVE_POSITION_EVIDENCE_UNAVAILABLE`
  (`backend/strategies/live_sequence.py:665-676`).
- The **peak** margin of the overlap (old + new) is prechecked before any leg is
  submitted; when it does not fit the refusal is `MARGIN_INSUFFICIENT` with the
  arithmetic (`backend/strategies/futures_margin.py:1-27`;
  `backend/strategies/admission.py:1290-1310`).

**Required env/config.** Gate set as every lane
([README.md](README.md#environment-variables)). Futures-specific:

| Setting | Why | Source |
| --- | --- | --- |
| `HOSTED_LIVE_ENABLED` | master live gate | `backend/strategies/live_settings.py:27-37` |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | exact account allowlist | `backend/api/services/hosted_strategy_authz.py:38-48` |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | must not be falsy | `backend/strategies/execution_dispatcher.py:35-39` |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | margin evidence freshness | `backend/strategies/admission.py:77,134-142` |
| `ADMISSION_RISK_*` ceilings | only if the deployment caps declared risk | `backend/strategies/risk_policy.py:89-99,232-264` |

**Migration head.** Code head `20260926_000051`
(`backend/alembic/versions/20260926_000051_live_approval_binding.py`); verify the
deployed head read-only ([README.md](README.md#verification-commands-read-only)).

**Services.** `finance-app`, `alerts-worker`, `strategy-runner`, `frontend-next`
(`compose.yml:88,144`; `compose.worker.yml:11`; `compose.supervisor.yml:13`).
Roll release depends on the outcome consumer/dispatcher being healthy
(`backend/app/bootstrap.py:107-134,210-241`).

## 2. Enable

No real order may be placed without the owner's explicit authorization. Every
step marked **[owner approval]** needs the owner's explicit go-ahead.

1. **[owner approval]** Confirm the lane is intended for the exact futures account
   and that the strategy holds only pinned expiries/lots.
2. **[owner approval]** Add the account scope to `HOSTED_STRATEGY_ACCOUNT_SCOPES`
   and confirm account ingest covers it
   (`documents/hosted-strategies-live-deployment.md:169-190`).
3. **[owner approval]** Open this lane for new exposure: add `futures` to
   `HOSTED_LIVE_LANES` in the deployment's untracked `.env` (all open lanes are
   listed, e.g. `HOSTED_LIVE_LANES=cnc,mis,futures`), then restart `finance-app`.
   Default deny: unset or empty opens no lane, an unknown name is ignored with a
   startup warning, and closing a lane still releases reductions, exits and
   square-offs (`backend/strategies/live_service.py:1413,1443-1481`).
4. **[owner approval]** Follow the deploy order in
   [README.md](README.md#deployment-order-shared-c2-procedure). Futures opens
   after MIS in the C2 lane order
   (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:134`).
5. Verify (section 3) before configuring a version.
6. Configure the strategy's `live` mode and select the allowlisted scope.

## 3. Verify

All read-only.

| Check | Healthy |
| --- | --- |
| `GET /api/strategies/options` | `live_lanes` includes `futures` (`backend/api/routers/strategies.py:554-583`) |
| `GET /api/system/runtime` | `live_outcome_consumer` and `hosted_execution_dispatcher` running (`backend/app/bootstrap.py:107-134,210-241`) |
| Roll state | `GET /api/strategies/{strategy_id}/rolls` and `/rolls/{roll_id}` show the roll's state and proven quantities (`backend/api/routers/strategies.py:1464,1491`) |
| Roll acquire proof | `POST .../rolls/{roll_id}/prove-filled` is the route that records the replacement fill proof (`backend/api/routers/strategies.py:1613`) |
| Deployed source hash | matches the reviewed worktree (`documents/hosted-strategies-live-deployment.md:30-44`) |
| Owner UI `/strategies/[strategyId]` | Options/pending/coverage sections read `known`; no withheld `close_old` with a stalled acquisition (`frontend-next/features/strategies/components/hosted-options-panel.tsx:960`) |

Healthy futures: the roll's acquire half is `open_new`/immediate and progressing;
the `close_old` half is still `withheld` until the full replacement quantity is
proven filled; the attributed book matches broker truth.

## 4. Halt

Ordered least to most drastic. Full semantics: [README.md](README.md#halt-ladder-shared).

1. **Stop evaluator** — `POST /api/strategies/{strategy_id}/jobs/{job_id}/stop`
   (`backend/api/routers/strategies.py:3189`). Does not cancel orders or flatten.
   Refusals: `STALE_ATTEMPT`, `STALE_LEASE_EPOCH`, `STOP_RACE_LOST`
   (`backend/api/routers/strategies.py:3205-3228`).
2. **Cancel pending work** — `GET .../owner-actions/pending-work` then
   `POST .../owner-actions/cancel-pending`
   (`backend/api/routers/strategy_owner_actions.py:237,275`). Cancels only
   eligible ENTRY candidates; a roll `close_old` is not an ENTRY and is not
   cancelled by this action. Refusals as in [README.md](README.md#halt-ladder-shared).
3. **Roll stall** — `POST .../rolls/{roll_id}/stall`
   (`backend/api/routers/strategies.py:1693`) records a stalled roll rather than
   releasing its close; the close stays withheld
   (`backend/strategies/live_service.py:881-906`).
4. **Flatten** — `POST .../owner-actions/flatten`
   (`backend/api/routers/strategy_owner_actions.py:338`). For a live futures book
   the non-option reduction is not yet wired, so flatten refuses
   `FLATTEN_LIVE_NONOPTION_UNSUPPORTED`
   (`backend/api/services/owner_actions.py:117`).
5. **Disable the lane** — `HOSTED_LIVE_ENABLED` non-truthy + recreate
   `finance-app`. Does not close positions
   ([README.md](README.md#rollback-doctrine-shared)).

Expected refusals while halting: `ROLL_CLOSE_NOT_RELEASED` (the acquire is not
fully proven), `ROLL_FILL_NOT_PROVEN`, `ROLL_ALREADY_OPEN`, `ROLL_PLAN_MISMATCH`,
`ROLL_OLD_NOT_FLAT`, `ROLL_STATE_INVALID`, `ROLL_UNKNOWN`
(`backend/strategies/rolls.py:82,88,94,100,106,112,118`).

## 5. Repair

| Blocked state | Evidence required | Repair path | Never auto-resolved |
| --- | --- | --- | --- |
| `close_old` withheld; acquisition partial/stalled/rejected/unknown | the roll's own recorded replacement executions must reach the FULL required quantity | `POST .../rolls/{roll_id}/prove-filled` (`backend/api/routers/strategies.py:1613`); a stall is recorded via `/stall` (`:1693`) | the close is never released on a partial or unknown acquisition (`backend/strategies/live_service.py:885-906`) |
| Roll unknown / unbound | the roll binding on the frozen plan | re-derive via the roll read routes; an unbound plan may not close an open roll (`backend/strategies/live_service.py:902-913`) | a close is never aimed at another roll/account/quantity (`backend/strategies/live_service.py:887-892`) |
| Roll old contract not flat | attributed book must prove zero | `POST .../rolls/{roll_id}/old-flat` (`backend/api/routers/strategies.py:1666`); quantity is clamped to the attributed book (`backend/strategies/live_sequence.py:700-712`) | never assumes flat from a partial close |
| Unanswered plan step | the platform's own evidence | dead-submission disposition (`backend/api/routers/strategy_owner_actions.py:484,539`) | not auto-resolved; a staged protective order refuses (`DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN`, `backend/api/services/owner_actions.py:98`) |
| Live residual on a roll step | the claim's `repair_required` residual | owner residual disposition `POST .../plans/{plan_id}/residual` `action="abandon"` (`backend/api/routers/strategies.py:2384`) | never fabricates a fill; refuses while authority could still fill it (`backend/strategies/live_repair.py:20-26`) |

## 6. Rollback

Follow [README.md](README.md#rollback-doctrine-shared). A live futures roll cannot
be flattened by the strategy flatten route, so a roll must first be driven to a
terminal state (acquire proven, close released, old-flat proven) or stalled and
left to the owner before disabling the lane. Disabling `HOSTED_LIVE_ENABLED` does
not close broker positions.

Migrations are fix-forward; do not downgrade
([README.md](README.md#rollback-doctrine-shared)).

## 7. Known limitations

- **Live non-option flatten refuses** (`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`,
  `backend/api/services/owner_actions.py:117`), so a live futures book must be
  reduced through the roll/plan pipeline, not flatten.
- **The roll close release is a bounded LIMIT** (`RULE_ROLL_CLOSE_RELEASED` is a
  gated rule: `backend/strategies/live_limit_orders.py:88`), so an unfilled close
  cannot become a market order; an out-of-band book refuses
  `LIVE_LIMIT_PRICE_BOUND_EXCEEDED` rather than widening the bound.
- **The peak-margin engine is paper-based** while live broker quotes remain future
  wiring; the peak is computed from evidence before any leg is submitted
  (`backend/strategies/futures_margin.py:9-12`).
- **The roll close is an absolute flat and needs the authoritative attributed
  reader**; without it the step refuses `LIVE_POSITION_EVIDENCE_UNAVAILABLE` and
  nothing is sent (`backend/strategies/live_sequence.py:665-676`).
- **`RollStateMachine` storage is not imported into the options lane**, but the
  futures lane reuses it directly; a roll's release is decided by its own state,
  not by the plan (`backend/strategies/live_service.py:885-906`).
