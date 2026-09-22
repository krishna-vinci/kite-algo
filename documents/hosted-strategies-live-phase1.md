# Hosted live release — Phase 1 (shared live path)

Date: 2026-09-22. Baseline `806e71f`. **No commit, no deploy, no orders, no
notifications.** `HOSTED_LIVE_ENABLED` is unset everywhere, so the live path is
present but disabled.

## Implemented

| Surface | Change |
| --- | --- |
| `backend/strategies/live_settings.py` | `HOSTED_LIVE_ENABLED`, **default false**; unset/empty/`0/false/no/off` all mean disabled |
| `backend/strategies/live_authority.py` | `plan_binding()` + `derive_live_authority()`: environment comes from the **persisted run binding** frozen in the plan's proposal envelope; authority requires run `open` + `live`, token active/unexpired/allowing `live`, and a hosted job in `queued/starting/running` with `lease_owner`, `lease_epoch>=1`, unexpired `lease_until`, current `attempt`. Effective freshness = **min(token, lease)**. `live_authority_reader()` re-derives on every call |
| `backend/strategies/live_readers.py` | Production readers: broker session from owner account binding (`kite_sessions.broker_user_id`), live margin via `OrdersService.order_margins`, attributed current from `strategy_position_projection` (**never-published is unknown, not flat**), market quote from `WorkerMarketDataService`, fills from `order_trade_fills` deduped on `trade_id` |
| `backend/strategies/live_service.py` | `build_live_plan_adapter()` (real readers, real `KiteOrdersIntentHandler`) + `LivePlanExecutor`: gate → persisted authority → reservation/approval/admission → durable claim → dispatch. Phase 1 dispatches `single_instrument` only; other kinds are `LIVE_PLAN_KIND_UNSUPPORTED` |
| `backend/strategies/live_ingestion.py` | `LiveOutcomeConsumer`: `pending/partial` claims → bound broker order ids → `order_trade_fills` → `partial`/`filled`, barrier `work_resolved`, attribution publish, reservation consume/advance; terminal broker refusal releases terminal-unfilled. Unknown evidence leaves the claim, the work and the capacity untouched |
| `backend/strategies/live_adapter.py` | Order payload now carries `variety` and an `attribution` block (run/strategy/mode/account/entry_surface/plan+step) so broker order ids bind to the plan step/run **before** the order exists |
| `backend/api/routers/strategies.py` | `execute_plan` selects the executor from the **persisted binding environment**; no request parameter can choose live. `_live_plan_executor` reports `LIVE_DISABLED` when the setting is off |
| `backend/app/bootstrap.py` | Starts the outcome consumer only when `HOSTED_LIVE_ENABLED` is true; otherwise publishes `live_outcome_consumer: disabled`. Health is exposed through `set_component_status` |
| `backend/alembic/versions/20260922_000039_hosted_live_mode.py` | Additive: widens the three hosted mode CHECKs to admit `live`, widens the live-claim state vocabulary (`partial`/`filled`), adds `strategy_plan_execution_events.broker_order_id`. `schema.sql` mirrored |
| `backend/strategies/service.py` | `ALLOWED_EXECUTION_MODES` includes `live`; live requires a `kite:<broker_user_id>` scope |

## Executed checks (outside sandbox)

| Command | Result |
| --- | --- |
| `pytest tests/integration/test_hosted_live_phase1_postgres.py -q` | **5 passed** — hosted live mode representable; `LIVE_DISABLED` refuses with no broker call/claim; lease expired → `HOSTED_LEASE_EXPIRED`, token without `live` → `TOKEN_MODE_NOT_ALLOWED`; route environment from the persisted binding; **entry → broker acceptance → ordinary ingestion → consumed reservation → attributed position → exit (SELL, no new capacity) → guarded live settlement proof** |
| `pytest tests/strategies/test_execution.py tests/api/test_strategy_owner_and_binding.py -q` | **129 passed** (paper executor + owner API unchanged) |
| `pytest tests/integration/test_live_adapter_preparation_postgres.py -q` | **20 passed** |
| from-zero `alembic upgrade head` on a unique disposable DB | head `20260922_000039`; mode CHECK includes `live`; `broker_order_id` present; DB dropped |

## Boundaries / remaining (Phase 2)

* Only `single_instrument` dispatches; weights/baskets, MIS square-off, futures rolls
  and options are named refusals until the next phase wires their dispatch through
  the same executor/factory.
* The consumer resolves a step from bound broker ids; the production binding of
  `order_state_projection`/intent status is unchanged (it already exists for the
  algo-instance lane) and is what makes an intent terminal for settlement.
* Deployment setting stays false; the final rollout flips it only after all lanes
  are accepted.

---

## Acceptance correction pass (2026-09-22)

1. **Authority is the full hosted contract.** `derive_live_authority` now refuses
   unless: the job `desired_state` is not `stopped` (`HOSTED_STOP_REQUESTED`), the
   job mode equals the run mode and `live` (`HOSTED_JOB_MODE_MISMATCH`), the job
   owner/account match the binding, the job's `token_id` is the run's token, the
   token's pinned `hosted_attempt` equals the job attempt
   (`HOSTED_ATTEMPT_MISMATCH`), AND a live owned unexpired lease exists
   (`HOSTED_LEASE_MISSING`/`HOSTED_LEASE_EXPIRED`) for **every** authority status -
   `queued`/`starting` no longer authorise a trade.
2. **Ingestion is scoped and authoritative.** Fills must belong to the claim's
   account AND be owned by an order id whose `live_order_intents` /
   `worker_live_execution_links` row names the plan's bound run; terminal outcomes
   come from `order_state_projection` and require coverage for **every** order id
   with a terminal status (`COMPLETE/CANCELLED/REJECTED/LAPSED`); a partial fill
   with a terminal cancel resolves the RESIDUAL as `partial`, never as a fill or a
   zero-fill rejection; unknown stays pending.
3. **Finalize ordering is durable.** A fully filled step publishes attribution
   FIRST, consumes capacity second, and records the barrier `work_resolved` LAST;
   if publication fails the claim is written back as a scannable `partial` with
   `publication: unpublished`, so a later pass repairs it and the barrier is never
   released on unpublished evidence.
4. **Renewal needs NEW progress.** A partial fill advances the reservation only when
   the verified filled quantity increases (`progress_at` records the fact).

Executed after the corrections: `pytest tests/integration/test_hosted_live_phase1_postgres.py -q`
-> **7 passed** (adds the scoping/terminal-coverage test and the lease/stop authority test).

## Route-level acceptance and hardening pass (2026-09-22)

### (A) Real prepare-issued credential through the public production routes

`tests/integration/test_hosted_live_phase1_routes_postgres.py` is the acceptance
run the earlier evidence lacked. Nothing in it hand-inserts the credential, the
job, the run binding, the proposal or the plan:

| Step | Surface used |
| --- | --- |
| operator login | `POST /api/auth/login` (real credential) |
| strategy + immutable version + policy + job | owner routes (`/strategies`, `/versions`, `/admission-policy`, `/jobs`) |
| claim + **credential handoff** | `POST /api/hosted-supervisor/jobs/{id}/claim` then `/prepare` — the child token is minted by the lifecycle and used as issued |
| frozen plan | `POST /api/algo-workers/worker/proposals` with that child token (real `ProposalStore` resolver, real catalog) |
| reserve / approve / execute | public owner routes; environment is DERIVED from the persisted binding, never the request |
| attribution +10, exit to 0 | the real `LiveOutcomeConsumer` over ordinary ingestion artifacts |
| stop, release, process-cleanup, reconciliation | public routes; the live branch records the proof, unblocks, closes the bound worker run and appends the audit row in ONE transaction |

Only the broker intent handler, the market quote and the margin/funds reading are
faked. Asserted in the same test: attributed quantity `10` then `0`, the bound
`algo_worker_runs` row `closed` with `closed_at`, `reconciled_at` set, a valid
barrier proof (`quiet_since_version == barrier_version`), exactly TWO
`work_resolved` events for two steps, an audit row, and both claims `filled`.

`test_live_launch_and_prepare_routes_follow_the_deployment_setting` covers the
launch side: with `HOSTED_LIVE_ENABLED` unset the live job launch is refused
(no job row is created) and a claimed job cannot be handed a live credential.

### (B) LIVE reconciliation evidence

`ReconciliationEvidenceCollector._live_settlement` selects the **live** sources
and never the paper collector: the run binding must name the job's
strategy/account and the `live` environment; exposure comes from the strategy's
OWN published live attribution projection (an account-net-flat is not a strategy
flattness proof); work is `outstanding` while any durable live step is
unresolved; and account truth must be a COMPLETE ingest cycle, so missing or
refreshing truth is `unknown`, never `flat`. The reconcile route now requires the
durable proof for `live` as well as `paper`/`dry_run`, validated inside the
unblock transaction at the assessed version, and closes the linked worker run in
that same transaction.

### (C) Ingestion: proven single writer, crash repair, no duplicate effects

`backend/strategies/live_ingestion.py` was rebuilt around a durable consumer
lease plus a stage cursor:

* **Single writer.** `live_plan_submissions.consumer_token` / `consumer_until`
  are taken by a conditional UPDATE. Two consumers racing one step produce ONE
  terminal claim, ONE reservation consumption and ONE barrier event
  (`test_two_consumers_serialize_the_step_and_do_not_duplicate_effects`). An
  abandoned lease expires by time and is taken over
  (`test_abandoned_lease_is_taken_over_after_expiry`).
* **Crash repair at every stage.** The cursor records `publish` -> `capacity` ->
  `barrier`; each effect is idempotent (full recompute, status-guarded
  reservation transition, barrier de-duplicated on `(book, event, ref,
  detail.plan_id)`). Repairing a claim rewound after the publish does not
  duplicate the barrier or the consumption
  (`test_crash_after_publish_resumes_without_duplicate_effects`).
* **No terminal claim before its effects.** The terminal write happens only after
  re-reading the barrier event and the reservation from their own sources; an
  unconfirmed stage keeps the claim `finalizing`/`rejecting` with the blocker
  named. `enumerate_inflight_work` now enumerates EVERY unresolved live state
  (not just `pending`/`uncertain`), so a staged claim cannot disappear behind a
  quiet proof (`test_staged_claim_stays_in_flight_for_the_settlement_barrier`).
* **Cross-publication generation consistency.** The cursor records the
  publication generation before and after the publish and refuses to treat the
  stage as done when the generation moved backwards or vanished
  (`test_publication_generation_regression_blocks_the_terminal_write`).
* **Per-order, per-account evidence.** Fills count only for order ids bound to
  THIS account AND this plan's bound run, deduplicated on the broker trade id and
  totalled per order; terminal proof requires EVERY order to be terminal AND its
  own ingested fills to reach the broker's last-seen quantity. A CANCELLED order
  whose trades are still arriving resolves nothing
  (`test_per_order_fill_sync_completeness_is_required`).
* **A terminal cancel with a residual is `repair_required`.** It stays visible
  with `repair_required: true`, the residual quantity and
  `blocking=terminal_cancel_with_residual`; it is never reported as completed,
  never as a zero-fill rejection, and the residual capacity is not released.
  A partial fill that did not increase renews nothing.

### (D) Deployment setting and mode vocabulary

`HOSTED_LIVE_ENABLED` (default false) now gates LAUNCH (`/jobs`), the child
credential handoff (`/hosted-supervisor/jobs/{id}/prepare`), ADMISSION
(`/plans/{id}/reserve`, on the DERIVED binding environment) and SUBMISSION (the
executor), all through the one reader in `live_settings`. Migration `000039`
admits `live` in the hosted mode CHECKs and adds the consumer-lease columns plus
the resolved/repair state vocabulary; `schema.sql` mirrors it.

The persisted authority also verifies the token's **capabilities**, not just its
mode list: a run whose token does not hold `intents:submit` is refused
`TOKEN_ACTION_NOT_ALLOWED`, because the hosted lifecycle mints that action from
the pinned `trade` capability and a data-only child must never inherit trade
authority.

### (E) Readers and background lifecycle

Quotes are matched back to the EXACT frozen instrument (token, then symbol) and a
payload for a different instrument is a named refusal; margin evidence must cover
EVERY leg with a stated amount or it is `None` (unknown, never headroom); the
attributed-position reader still refuses a never-published book. Bootstrap
startup/shutdown moved into `start_live_outcome_consumer` /
`stop_live_outcome_consumer`, and the consumer's cancellation path now always
reports `stopped`. `test_consumer_loop_and_bootstrap_start_stop_are_functional`
covers both the disabled and enabled deployment.

## Executed after this pass (outside sandbox, disposable PG 15433)

| Command | Result |
| --- | --- |
| `pytest tests/integration/test_hosted_live_phase1_routes_postgres.py -q` | **2 passed** — prepare-issued credential through the public routes to attributed +10 / 0 / closed run / audit; launch + prepare + admission gated when disabled |
| `pytest tests/integration/test_hosted_live_phase1_postgres.py -q` | **15 passed** — authority, scoping, terminal coverage, two-consumer serialization, crash repair, generation regression, lease takeover, gating, consumer/bootstrap lifecycle |
| `pytest tests/strategies/test_execution.py tests/strategies/test_settlement.py tests/strategies/test_reconciliation.py tests/strategies/test_live_submission_store.py tests/strategies/test_lifecycle_prepare.py tests/api/test_strategy_owner_and_binding.py tests/integration/test_live_adapter_preparation_postgres.py tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q` | **271 passed, 14 skipped** (paper executor, owner API, settlement barrier, lifecycle prepare, adapter preparation unchanged) |

## Residual hardening pass (root review follow-up, 2026-09-22)

| Issue | Fix | Evidence |
| --- | --- | --- |
| `HOSTED_LIVE_ENABLED=flase` armed live | explicit truthy allowlist (`1/true/yes/on`); everything else, including typos, is false | `tests/strategies/test_live_settings.py` |
| The consumer acted on the pre-lease copy of a row | `_process_row` re-reads state, orders, delta and detail from the row the lease UPDATE returned | phase1 suite |
| `record_outcome` checked the token only | the write also requires an UNEXPIRED lease, so a slow owner after a takeover cannot write | `test_paused_owner_after_takeover_cannot_apply_effects` |
| Barrier de-dup was check-then-insert | `record_work_event_once` holds the book lock across check+insert, and a partial unique index (`uq_barrier_work_resolved_live_step`) makes a duplicate impossible | `test_barrier_work_resolved_is_unique_per_live_step` |
| Lease expiry used the caller's clock | the lease is stamped with `NOW() + interval`, so app/DB clock skew cannot invalidate a healthy lease or extend an abandoned one | phase1 suite |
| Live settlement only rejected `refreshing`| only `idle` + a completion timestamp is complete CURRENT truth; `stale`/`refreshing`/missing read `unknown`, never flat | `test_live_settlement_requires_complete_current_account_truth` |
| In-flight enumeration used "not terminal" | explicit non-terminal allowlist, so an unclassified future state blocks a quiet proof | phase1 suite |

Also added: `POST /{strategy_id}/plans/{plan_id}/residual` — the bounded,
owner-only disposition of a `repair_required` residual. It refuses while the
plan's authority could still fill the residual, releases only unused capacity,
records the step's `work_resolved` once, appends a `residual_abandoned` trail
row, and is idempotent. Nothing is silently unblocked and no fill is invented
(`test_residual_repair_disposition_is_bounded_and_audited`).

After this pass: `pytest` over the Phase 1 route/authority/ingestion suites plus
the paper-executor, settlement, reconciliation, lifecycle and owner-API suites →
**296 passed, 14 skipped**.

## Not yet done (explicit Phase 2 scope)

* Per-lane dispatch (weights/baskets, MIS, futures rolls, options) through the same
  executor/factory. `LIVE_PLAN_KINDS` is still `single_instrument` and every other
  kind remains a named refusal.
* Live SDK/frontend supported-mode flow.
* Deployment: `HOSTED_LIVE_ENABLED` is unset everywhere; nothing here enables it.

The detailed remaining work, with the exact hook points, is in
`docs/agent-work/hosted-live-release/PHASE2-CHECKPOINT.md`.
