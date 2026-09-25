# Runbook: hosted live options

Public lane name `options`. Shared procedures, env reference, halt ladder, and
refusal lookup: [README.md](README.md).

Authority for this lane: the C1.2 live options readiness design
(`documents/hosted-live-options-c1-2-design-2026-09-25.md`), the B2.4 protection
ownership design
(`documents/hosted-options-b2-4-protection-ownership-design-2026-09-25.md`), and
the B2.6b owner-actions design
(`documents/hosted-owner-actions-b2-6b-design-2026-09-25.md`).

## 1. Scope and prerequisites

**What it does live.** One durable option run per structure on the existing
options engine, admitted as plan kind `option_structure`
(`backend/strategies/live_service.py:66,1177`). Run lifecycle:
`created -> entering -> entered -> exiting -> exited/settled`, plus
`partial_entry`, `cleanup_required`, `partial_exit`
(`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:32-34`;
status vocabulary `backend/options/execution/repair.py:41-77`).

- **Entry hedges are immediate; a short entry is withheld** behind every required
  hedge with `RULE_HEDGE_FILL_GATE`
  (`backend/strategies/live_sequence.py:125,820`).
- **Exit closes shorts first;** the hedge half is withheld under
  `RULE_HEDGE_RELEASE_WITHHELD` until its short is proven closed
  (`backend/strategies/live_sequence.py:128,826`;
  `backend/options/protection/exit_builder.py:50-66`).
- **Adjust is refused in live.** The live lane refuses an adjust plan by name with
  `LIVE_OPTION_ADJUST_UNSUPPORTED` because B2.2 is paper-only
  (`backend/strategies/live_sequence.py:776-783`).
- If the durable run/binding is unavailable the lane refuses
  `LIVE_OPTION_RUN_UNAVAILABLE` (`backend/strategies/live_sequence.py:764-772`).
- **Broker basket-margin evidence is required** (`option_live_margin_evidence`,
  `backend/strategies/plan_pipeline.py:280-350`). Entry/resize uses the final
  basket requirement; a roll carries the max of final and overlap baskets and a
  missing peak refuses `LIVE_OPTION_ROLL_PEAK_UNAVAILABLE`
  (`backend/strategies/plan_pipeline.py:291,320-332`). A wrong-account read
  refuses `LIVE_OPTION_MARGIN_EVIDENCE_SCOPE_MISMATCH`
  (`backend/strategies/plan_pipeline.py:302`).
- **Chain/Greeks freshness is required** at freeze and again immediately before
  send (`validate_option_chain_evidence`, `backend/options/market/freshness.py:205`;
  called at `backend/strategies/live_adapter.py:966-980`).
- **Structure risk policy is required.** A version with no declared policy cannot
  take options exposure: `STRATEGY_RISK_POLICY_MISSING`, plus
  `OPTION_STRUCTURE_FAMILY_NOT_ALLOWED`, `OPTION_EXPIRY_POLICY_NOT_ALLOWED`,
  `OPTION_NAKED_NOT_PERMITTED`, `OPTION_MAX_LOSS_EXCEEDED`,
  `STRATEGY_NOTIONAL_LIMIT_EXCEEDED`
  (`backend/strategies/admission.py:53-58,509,587-697`).
- **Protection ownership is durable.** Unknown ownership blocks new exposure but
  not risk-reducing work; a conflict means the run has been handed to someone else
  (`backend/options/protection/ownership.py:112-118`;
  `backend/options/execution/plan_binding.py:527-540`).

**Required env/config.**

| Setting | Why | Default |
| --- | --- | --- |
| `HOSTED_LIVE_ENABLED` | master live gate | off (`backend/strategies/live_settings.py:27-37`) |
| `HOSTED_STRATEGY_ACCOUNT_SCOPES` | exact account allowlist | empty/deny (`backend/api/services/hosted_strategy_authz.py:38-48`) |
| `HOSTED_EXECUTION_DISPATCH_ENABLED` | must not be falsy | enabled (`backend/strategies/execution_dispatcher.py:35-39`) |
| `ACCOUNT_INGEST_ENABLED` + `ACCOUNT_INGEST_ACCOUNT_SCOPES` | fill attribution/settlement | `true`/empty (`backend/app/bootstrap.py:705-708`; `backend/app/background.py:51-59`) |
| `ADMISSION_MARGIN_MAX_AGE_SECONDS` | margin/funds freshness (shared with options) | `60` (`backend/strategies/admission.py:77,134-142`) |
| `LIVE_OPTION_CHAIN_MAX_AGE_SECONDS` | pre-send chain/Greeks freshness | `5.0` (`backend/options/market/freshness.py:18,205-209`) |
| `ADMISSION_RISK_*` ceilings | only if the deployment caps declared risk | absent = no ceiling (`backend/strategies/risk_policy.py:89-99,232-264`) |

**Migration head.** Code head `20260925_000050`
(`backend/alembic/versions/20260925_000050_flatten_operations.py`). Options
specifically needs the adjust-phase, protection-owner, and risk-policy revisions
present (`backend/alembic/versions/20260925_000045_option_adjust_phase.py`,
`.../20260925_000046_strategy_version_risk_policy.py`,
`.../20260925_000047_option_protection_owners.py`). Verify the deployed head
read-only ([README.md](README.md#verification-commands-read-only)).

**Services.** `finance-app` (margin/chain readers, live outcome consumer,
dispatcher), `strategy-runner`, `alerts-worker`, `frontend-next`
(`compose.yml:88,144`; `compose.worker.yml:11`; `compose.supervisor.yml:13`).

## 2. Enable

No real order may be placed without the owner's explicit authorization. Every
step marked **[owner approval]** needs the owner's explicit go-ahead.

1. **[owner approval]** Confirm the lane is intended for the exact options account
   and that the strategy version declares a risk policy
   (`STRATEGY_RISK_POLICY_MISSING`, `backend/strategies/admission.py:53,509`).
2. **[owner approval]** Add the account scope to `HOSTED_STRATEGY_ACCOUNT_SCOPES`
   and confirm account ingest covers it
   (`documents/hosted-strategies-live-deployment.md:169-190`).
3. **[owner approval]** Follow the deploy order in
   [README.md](README.md#deployment-order-shared-c2-procedure). Options is the
   LAST lane in the C2 order and opens only after the others pass
   (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:134`).
4. Verify (section 3), including that the option-chain snapshot service is fresh
   and the broker basket-margin read is available.
5. Configure the strategy's `live` mode, select the allowlisted scope, and obtain
   the owner approval bound to the plan
   (`POST /api/strategies/{strategy_id}/plans/{plan_id}/approval`,
   `backend/api/routers/strategies.py:1771`).

## 3. Verify

All read-only.

| Check | Healthy |
| --- | --- |
| `GET /api/strategies/options` | `live_lanes` includes `options`; `execution_modes` includes `live` (`backend/api/routers/strategies.py:554-583`) |
| `GET /api/strategies/{strategy_id}/option-runs` | `coverage: "known"` (an incomplete read reports `unknown` with a reason, never an empty list) (`backend/api/routers/strategy_option_runs.py:757-790`; `backend/api/services/owner_actions.py:66-68`) |
| `GET /api/strategies/{strategy_id}/option-runs/{option_run_id}` | one run's legs/frozen state/refusals/Greeks read back (`backend/api/routers/strategy_option_runs.py:792-857`) |
| `GET /api/system/runtime` | `live_outcome_consumer` and `hosted_execution_dispatcher` running (`backend/app/bootstrap.py:107-134,210-241`) |
| Deployed source hash | matches the reviewed worktree (`documents/hosted-strategies-live-deployment.md:30-44`) |
| Owner UI `/strategies/[strategyId]` | Options panel renders; per-run status and protection-owner line are shown; coverage reads `known` (`frontend-next/features/strategies/components/hosted-options-panel.tsx:96-130,897-960`) |

In the owner UI (the Options panel), "healthy" is: every run shows a status
(`option-run-status-*`), "No protection owner" / a named owner rather than the
"Protection ownership unreadable" alert
(`frontend-next/features/strategies/components/hosted-options-panel.tsx:105-130`),
no `option-runs-coverage-warning` (`:960`), no pending-work coverage warning
(`:1115`), and the four control cards (Stop evaluator / Cancel pending work /
Exit structure / Flatten) with the expected enablement
(`frontend-next/features/strategies/components/hosted-options-panel.tsx:1558-1566`).

## 4. Halt

Ordered least to most drastic. Full semantics: [README.md](README.md#halt-ladder-shared).

1. **Stop evaluator** — `POST /api/strategies/{strategy_id}/jobs/{job_id}/stop`
   (`backend/api/routers/strategies.py:3189`). No new evaluations; holdings and
   protection stay (`frontend-next/features/strategies/components/hosted-options-panel.tsx:1558`).
   Refusals: `STALE_ATTEMPT`, `STALE_LEASE_EPOCH`, `STOP_RACE_LOST`
   (`backend/api/routers/strategies.py:3205-3228`).
2. **Cancel pending work** — `GET .../owner-actions/pending-work` then
   `POST .../owner-actions/cancel-pending`
   (`backend/api/routers/strategy_owner_actions.py:237,275`). Cancels only
   eligible ENTRY candidates; **never** a protective hedge, an option exit, or a
   reduction (`backend/api/services/owner_actions.py:57-61,76,84-87`).
3. **Exit structure (per run, short-first)** — `GET .../option-runs/{option_run_id}/exit`
   then `POST` the same path
   (`backend/api/routers/strategy_owner_actions.py:399,430`). One stage per POST;
   later reconciliation releases the next stage and the owner POSTs again.
   Expected refusals: `OPTION_RUN_ADJUST_IN_FLIGHT`,
   `OPTION_PROTECTIVE_EXIT_UNRESOLVED`, `OPTION_RUN_EVIDENCE_AMBIGUOUS`,
   `OPTION_RUN_EXIT_EVIDENCE_CHANGED`, `OPTION_RUN_STATE_CHANGED`,
   `OPTION_RUN_EXIT_NOT_APPLICABLE`, `OPTION_EXIT_BEFORE_ENTRY`
   (`backend/options/execution/repair.py:93-99`).
4. **Flatten** — `POST .../owner-actions/flatten`
   (`backend/api/routers/strategy_owner_actions.py:338`; resume with `GET :377`).
   Resumable; exits option runs one at a time, so parts of the strategy finish
   while others wait (`backend/api/services/owner_actions.py:108-133`).
   Refusals include `FLATTEN_EVALUATION_ACTIVE`, `DEAD_SUBMISSION_UNRESOLVED`,
   `FLATTEN_OPTION_RUN_COVERAGE_UNKNOWN`.
5. **Disable the lane** — `HOSTED_LIVE_ENABLED` non-truthy + recreate
   `finance-app`. Does not close positions
   ([README.md](README.md#rollback-doctrine-shared)).

Never: no lever marks a run exited because a broker accepted a stage — completion
is the run's own fills proving flat (`backend/options/execution/repair.py:73-77`).

## 5. Repair

| Blocked state | Evidence required | Repair path | Never auto-resolved |
| --- | --- | --- | --- |
| Option run `partial_entry` / `partial_exit` / `cleanup_required` | the run's own confirmed fills | `GET`/`POST .../option-runs/{option_run_id}/repair` (`backend/api/routers/strategies.py:1337,1364`). Verdicts: `flat`, `residual`, `ambiguous`, `not_repairable` (`backend/options/execution/repair.py:78-81`). Actions: `close_flat`, `close_residual` (`:109-113`) | an `ambiguous` run escalates as `OPTION_RUN_REPAIR_AMBIGUOUS`; it is never auto-adopted (`backend/options/execution/repair.py:83-85`) |
| Live residual repair | the claim's residual | **Refused in live**: `close_residual` against a live environment returns `OPTION_RUN_REPAIR_LIVE_UNSUPPORTED` — there is no governed live residual-close submission path yet (`backend/api/routers/strategies.py:1401-1408`; `backend/options/execution/repair.py:88`) | no live residual close is invented by the operator route |
| `adjusting` run whose adjust may still submit | the owning adjust must be provably finished | assessment reason `adjust_in_flight`; the takeover/repair gate refuses until it is terminal (`backend/options/execution/repair.py:103,293-296`) | a run whose adjust process died with an unanswered submission keeps `adjusting` until disposition (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:148`) |
| Ledger cannot be read completely | the run's own ledger | assessment reason `ledger_incomplete` (`backend/options/execution/repair.py:104`); a frozen option step with no durable run leg refuses `LIVE_OPTION_RUN_LEG_UNRESOLVED` and keeps the parent in flight (`backend/strategies/live_lane_ledger.py:211-222`) | never derives a new generation from an incomplete ledger (`documents/hosted-live-options-c1-2-design-2026-09-25.md:94-98`) |
| Protective stage unresolved (`sending`/`unknown`) | the stage's own pre-send records | owner exit refuses `OPTION_PROTECTIVE_EXIT_UNRESOLVED`; the stage resolves through `StagedStructureExit` and ordinary fills (`backend/options/execution/repair.py:95`; `documents/hosted-owner-actions-b2-6b-design-2026-09-25.md:102-112`) | never bypassed by cancel-pending or a fresh order |
| Protection owner unknown | an active owner row, or a release proof | unknown blocks exposure growth, not risk-reducing work (`OPTION_PROTECTION_OWNER_UNKNOWN`, `backend/options/protection/ownership.py:113`; `backend/options/execution/plan_binding.py:530-533`) | never assumed owned; never auto-transferred |
| Protection owner conflict | the run that is the actual owner | `OPTION_PROTECTION_OWNER_CONFLICT`; the caller's run is superseded and must not act (`backend/options/protection/ownership.py:112`; `backend/options/execution/plan_binding.py:533-540`) | never resolved by a blind retry |
| Unanswered plan step | the platform's own evidence | dead-submission disposition (`backend/api/routers/strategy_owner_actions.py:484,539`) | not auto-resolved; a staged protective order refuses (`DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN`, `backend/api/services/owner_actions.py:98`) |

**`LIVE_OPTION_RUN_LEDGER_INCONSISTENT` is not present in the code.** The C1.2
design names it as a planned refusal for a terminal order whose matching run trade
diverges (`documents/hosted-live-options-c1-2-design-2026-09-25.md:94-98`), but
the current code has no such string. What exists today is
`LIVE_OPTION_RUN_LEG_UNRESOLVED` (`backend/strategies/live_lane_ledger.py:220`)
and the `ledger_incomplete` assessment reason
(`backend/options/execution/repair.py:104`). Treat the C1.2 name as not-yet-built.

## 6. Rollback

Follow [README.md](README.md#rollback-doctrine-shared). For options the practical
order is: stop evaluator, cancel qualifying pending entry, exit each run
short-first, confirm each run is flat via its own fills, then disable the lane.
Flatten will do the per-run exits for you but is resumable and may report
`in_progress`/`blocked` while stages wait on broker fills.

Migrations are fix-forward — do not downgrade; the adjust-phase, risk-policy, and
protection-owner revisions all destroy data on downgrade
([README.md](README.md#rollback-doctrine-shared)).

## 7. Known limitations

- **Live adjust is not supported.** The live lane refuses
  `LIVE_OPTION_ADJUST_UNSUPPORTED`; B2.2 (the desired-state adjust/roll engine)
  is paper-only today (`backend/strategies/live_sequence.py:776-783`).
- **C1.2 is design-accepted but not fully implemented.** The broker basket-margin
  evidence and the fresh chain/Greeks checks have landed (margin reader
  `backend/strategies/plan_pipeline.py:280`; pre-send chain check
  `backend/strategies/live_adapter.py:966-980`), but the remaining C1.2 scope —
  expressing the B2.2 adjust/roll shapes on the live protocol, and binding the
  live approval to the strategy version, option-run generation, and protection
  policy — is not landed
  (`documents/hosted-live-options-c1-2-design-2026-09-25.md:8-13,68-98`).
- **Approval pinning is the current contract**, not the C1.2 one: the live
  approval is validated pin by pin against the plan/exposure snapshot/catalog/
  reservation, with `LIVE_APPROVAL_INVALID` on a moved pin and
  `EXPOSURE_SNAPSHOT_CHANGED` the only pin a dependent release may explain away
  (`backend/strategies/live_adapter.py:86-98,819-835,1889-1917`).
- **Gated dependent option legs must become bounded LIMIT before any real
  account.** C1.2 requires every gated dependent leg to be a bounded
  platform-side LIMIT with explicit cancel/terminal handling; an unfilled or
  unknown leg must never become a market order
  (`documents/hosted-live-options-c1-2-design-2026-09-25.md:11-13`). Until that is
  built, do not enable a real options account on the assumption it exists.
- **Live residual option closes have no governed submission path** — an operator
  repair of a live partial run is refused `OPTION_RUN_REPAIR_LIVE_UNSUPPORTED`
  (`backend/api/routers/strategies.py:1401-1408`).
- **`LIVE_OPTION_RUN_LEDGER_INCONSISTENT` is named in the design only** (see
  section 5).
