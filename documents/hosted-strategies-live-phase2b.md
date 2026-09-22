# Hosted live release - Phase 2B (futures/rolls, option structures, protection)

Date: 2026-09-22. Baseline `806e71f` plus the shared Phase 1 and Phase 2A live code.
**No commit, no push, no deploy, no production database, no real order and no
notification.** `HOSTED_LIVE_ENABLED` remains unset (false) everywhere.

Phase 2B completes the two remaining domain lanes on the durable multi-step
executor and closes the protection gap the Phase 2A bundle stated honestly:
the platform's own dead-child exit is now EXECUTED, and a hedged option structure
is no longer liquidated as a whole book.

## What shipped

| Surface | Change |
| --- | --- |
| `backend/strategies/live_sequence.py` | `build_futures_steps` (`futures_roll`) and `build_option_steps` (`option_structure`) registered through the existing `register_live_lane`; three new release rules (`RULE_ROLL_CLOSE_RELEASED`, `RULE_HEDGE_FILL_GATE`, `RULE_HEDGE_RELEASE_WITHHELD`); `LaneContext` gained `attributed_quantity`, `option_target` and `option_run_steps`; `lane_for_plan` maps `target_futures`/`option_structure` to their lanes |
| `backend/strategies/live_adapter.py` | Lane admission gates reuse the paper executor's own `_roll_binding`/`_roll_preconditions`/`_refuse_ungated_roll_close` and `_resolve_option_target`/`_option_run_steps`/`_begin_option_run`; `_check_option_gates` actually CALLS `OptionExpiryPolicy.check`; `_roll_close_size` reuses `attributed_exit_size`; the option run's transition is taken before the first submission |
| `backend/strategies/live_service.py` | `_lane_release_rule` implements the roll close (re-running `_roll_preconditions` against live roll state), the hedge fill gate (`hedge_fill_gate`, full release only) and the hedge-release rule (`build_structure_exit_orders`); the option settlement domain adapter is registered on the live path (it had no production caller before); `LIVE_PLAN_KINDS` covers all four lanes |
| `backend/strategies/live_lane_ledger.py` | **New.** Translates one CONFIRMED live fill into its lane's own durable record: the roll's `record_replacement_fill` + `prove_filled` (own linked executions only) and the option run's own trade. Idempotent per `(plan, step, cumulative filled quantity)`; called from `live_ingestion._consume_reservation` BEFORE the leg is declared terminal |
| `backend/strategies/live_dispatch_fence.py` | **New.** The durable PRE-SEND fence: `live_order_intents` proves whether a broker write was ever attempted for one step, and an optional authoritative broker read by the immutable client correlation resolves an attempted-but-unpersisted send |
| `backend/strategies/live_repair.py` | A `releasing` claim is now a real, bounded recovery target - decided by the fence proof, never by "the claim names no order" |
| `backend/options/protection/staged_exit.py` | **New.** `StagedStructureExit`: the staged structure exit derived server-side from the bound durable option run, its own confirmed fills and the existing `build_structure_exit_orders` rule |
| `backend/api/services/protection_runtime.py` | `WorkerProtectionRuntime` gained `structure_exit_submitter`; a structure run is no longer sent down the generic whole-book path; `submit_worker_protection_structure_exit` is the production stage submitter (account's own broker session, server-side attribution, deterministic client order refs) |
| `backend/app/background.py` | The protection loop wires the staged structure submitter |
| `backend/alembic/versions/20260922_000041_live_release_recovery.py` | Additive: the plan trail admits `release_recovered` (mirrored in `schema.sql`). No new table or column |
| `backend/api/schemas/proposals.py`, `backend/api/routers/strategies.py` | The residual disposition response carries the capacity outcome, the recovered order ids and the fence evidence; the route docstring states the `releasing` window |

## Futures / rolls

Two plans, one roll, and the order between them is the invariant.

* The **acquisition** half (`resolved_plan.roll.role = open_new`) is an ordinary
  immediate step. Its lane gate resolves the roll that NAMES the plan
  (`RollStateMachine.for_plan`) or the id the plan froze, re-validates the
  contract coordinates, the account and the required quantity, and refuses a plan
  that carries the roll's other contract.
* The **close** half (`close_old`) is a SEPARATE frozen plan. It is materialized
  `withheld` under `RULE_ROLL_CLOSE_RELEASED`: the release pass re-runs
  `_roll_preconditions` against the roll as it is now, so the close is released
  only from `releasing_old` - the state `RollStateMachine.release_close` reaches
  only when the FULL required replacement quantity is proven filled.
* A confirmed acquisition fill is recorded against the roll as a REPLACEMENT
  execution by `LiveLaneLedger` (keyed by the acquisition plan's own step, never
  by the strategy's aggregate book), and the roll is asked to re-decide. A partial
  fill records only what filled and stalls the roll: partial, rejected, cancelled
  and unknown acquisitions release nothing.
* The released close is an **absolute flat** of the strategy's own attributed
  old-contract quantity, sized through `attributed_exit_size` at release time.
  The frozen leg's signed target is NOT the order quantity - using it would double
  a long-old close. Both the frozen and the released quantity travel in the trail.
* An unbound future plan may not close a contract an open roll still holds:
  `_refuse_ungated_roll_close` runs at submission, so `ROLL_CLOSE_REQUIRES_BINDING`
  is a refusal, not a bypass.

## Option structures

One engine, reused: the frozen plan resolves through the existing plan/run binding
to the durable option run, and every quantity comes from the run's own confirmed
executions (`_option_run_steps`).

* **Entry.** The long (hedge) legs are ready immediately; a SHORT entry leg is
  materialized `withheld` behind the hedge step(s) under `RULE_HEDGE_FILL_GATE`.
  The release pass asks the existing `hedge_fill_gate` how much of the dependent
  short the CONFIRMED hedge fill releases, and only a FULL release proceeds: a
  submitted-but-unfilled hedge, a partial fill, a rejection, a cancellation or a
  timeout releases nothing.
* **Exit.** The short-closing legs are ready; the hedge half of the exit is
  `withheld` behind them under `RULE_HEDGE_RELEASE_WITHHELD`, and the pass asks the
  existing `build_structure_exit_orders` with the parent's own confirmed fills as
  proof.
* **Expiry.** `OptionExpiryPolicy.check` is consulted before an entry
  materializes anything: a structure whose frozen policy is `exit_before_cutoff`
  is refused `OPTION_EXPIRY_CUTOFF_PASSED` inside the window.
* **Run ownership.** The run's transition (`_begin_option_run`, a CAS on its
  observed status) is taken before the first submission, so two plans for one run
  can never both move it. The run's own trades are recorded from live fills, which
  is what makes a later exit size itself against the run's book rather than the
  strategy's aggregate projection.

## The platform's own protection exit (executed, not cited)

`background._worker_protection_loop` builds the runtime exactly as before and now
passes a STRUCTURE-aware submitter beside the generic one.

* A **non-structure** run takes the generic control-plane exit
  (`submit_worker_protection_exit` -> `exit_control_strategy` ->
  `_exit_live_worker_run`), which sizes the exit from the strategy's own attributed
  book and places it through the account's own broker session. That is the
  dead-child MIS square-off, and a test now executes it end to end.
* A **structure** run takes the staged exit
  (`submit_worker_protection_structure_exit` -> `StagedStructureExit`). The
  platform resolves the durable option run bound to the worker run, reads the
  run's OWN confirmed trades, and derives bounded actions with the existing
  `build_structure_exit_orders` rule. An evaluator's `recommended_exit_orders` is
  NEVER submitted: a caller's order list is not evidence. When the run cannot be
  resolved the protection refuses (`no_bound_option_run`) instead of falling back
  to a whole-book liquidation, because selling a structure's long with its short
  is the naked window the structure exists to avoid.
* **Release contract (conservative, selected).** A hedge is offered to the builder
  only once EVERY short the run holds is PROVEN fully closed by the run's own
  fills. Proportional hedge release is deliberately not used in this release; the
  builder can size one, this adapter does not ask for one. One closed short never
  unlocks a hedge that still answers to an open short.
* **Staging across a dead child.** Each stage is recorded on the run with the
  digest of the evidence it was derived from, and the quantity already SUBMITTED is
  netted out of the next stage, so a restarted runtime recomputes the same digest
  and places nothing, while a newly proven short closure changes the digest and
  releases the hedge exactly once. The exit uses the account's own broker session
  and a server-side attribution; the child's credential is never consulted, and
  the exit can only CLOSE legs the run's own evidence says it holds.
* **"Submitted" vs "complete".** The protection state's `exit_submitted` is true
  only when the stage submitted AND the run's own positions are flat. A stage that
  legitimately sends nothing while the remaining short is unfilled keeps the
  protection alive instead of stopping with the structure half-liquidated.

## The `releasing` window

## Corrections carried in this bundle (root acceptance review)

| Issue (root review) | Fix | Evidence |
| --- | --- | --- |
| `staged_exit.submit` called the broker BEFORE recording anything, and the digest was derived from current evidence, so a crash or a partial fill could change the retry key | The stage claim - digest, ATTEMPT number, deterministic client order references and the exact per-leg quantities - is written to the run's own durable record (under the option run store's row lock) BEFORE the broker call. An unresolved `sending`/`unknown` claim is NEVER re-sent: it is reconciled from the platform's pre-send records, it is adopted when those records name the broker's own order, and anything less than that keeps the stage blocked instead of advancing the attempt | `test_a_crash_after_acceptance_never_becomes_a_second_or_larger_order`, `test_concurrent_protection_triggers_place_one_stage` |
| The protection test seeded the run's trades by hand; production protection orders are not `live_plan_submissions`, so nothing translated the ORDINARY fills into the run | `StagedStructureExit.reconcile_own_fills` reads `order_trade_fills` for the orders the run itself submitted and records them against the run's own leg ids, idempotently per `(order_id, trade_id)`. The tests now place through the production submitter (real `OrdersService` boundary, fake broker) and write ordinary ingestion facts - no direct `record_trades` on the exit path | the staged tests assert the short's closure from `order_trade_fills` rows only |
| Only the successful ids were recorded while the WHOLE requested list was marked submitted, and `own_positions` subtracted TOTAL submitted quantities from a net that already excludes fills | Per-leg outcomes are associated by index; an unacknowledged leg is recorded with no order id and a NAMED blocker, and only the OUTSTANDING (unfilled) quantity of a leg with a real broker order is netted - so a partial fill is never double-subtracted and an unacknowledged leg is never "submitted" | `test_an_idless_leg_is_named_unknown_and_never_retried_on_a_guess` |
| `resolve_run_for_worker_run` took `LIMIT 1`, so a worker run with several bound option runs could be declared complete on the newest one | Every candidate is examined: more than one is `option_run_ambiguous` (refused), and a run whose recorded account does not match the worker run's account is `option_run_account_mismatch` (refused). Neither marks the worker complete | `test_a_structure_run_with_unknown_attribution_is_refused` (unbound) plus the resolver's refusals |
| `LiveDispatchFence` could prove the rows it could read and then answer "no such order" for the rest | A pre-send row without a client correlation is `CLIENT_ORDER_REF_MISSING` (UNKNOWN), an unreadable read is `BROKER_READ_FAILED` (UNKNOWN), and only EVERY attempted row proven absent yields non-submission | routes suite, `test_the_releasing_window_recovers_only_on_proof` scenarios 5 and 6 |

## Capability surface (`GET /api/strategies/options`)

## Second review round: tenancy and concurrency

| Issue (root review) | Fix | Evidence |
| --- | --- | --- |
| `order_fills` / `order_terminal_status` read by `order_id` alone, and the bound account was ignored | Both reads take the stage's bound `account_id` (`order_trade_fills.account_id` / `order_state_projection.account_id` are part of the predicate), and a fill is only recorded when its symbol, side, product and instrument token agree with the leg this run actually sent | `test_a_fill_for_another_accounts_order_is_not_our_evidence`, `StagedStructureExit._fill_matches_leg` |
| `reconcile_own_fills` deduped from a pre-lock read and then merely appended under the lock, so two reconcilers could both book the same fill | The dedup moved into the store: `DurableOptionRunStore.record_trades_once` re-reads the recorded keys and appends inside ONE row-locked transaction on the run (`dedupe_key="stage_fill_id"`) | `test_concurrent_reconcilers_record_one_fill` (two threads, barrier-synchronised, exactly one trade recorded) |
| The stage "claim" was a plain append, so two submitters could both derive the same stage and both send; and a sender paused between claiming and writing its pre-send records could be read as "never sent" | `DurableOptionRunStore.claim_stage` takes the claim under the run's row lock and refuses while the latest record for a `(digest, attempt)` is `sending` OR `unknown`, so only one sender owns a stage. The claim carries an owner and a lease, but the LEASE IS AN OBSERVATION ONLY: there is no fence at the broker write, so an owner paused past its lease can still send, and elapsed time never proves it is gone. `_resolve_sending_stage` therefore NEVER reads missing pre-send records as non-submission - an expired lease with no pre-send rows returns a named `STAGE_SEND_UNKNOWN` (`claim_stage` refuses it too). Claims and resolutions are keyed `(digest, attempt)` so an old outcome can never resolve a newer attempt | `test_a_paused_first_sender_cannot_be_declared_unsent` (clock advanced 600s past the 300s lease: the second pass is refused by name, starts no new attempt, places nothing; the paused sender then resumes to exactly ONE physical broker call, Redis idempotency independent), `test_a_crash_after_acceptance_never_becomes_a_second_or_larger_order` (durable pre-send references ⇒ adopted, nothing re-sent) |
| An IDLESS `live_order_intents` row marked `failed` was read as an acknowledged zero-send, so a refusal was retryable - but the production order path writes that SAME idless `failed` row for a generic failure AFTER the broker may have accepted (`OrdersService.place_order`'s `except Exception` → `mark_live_order_intent_failed`), so a retry could duplicate a live exit | The one durable non-submission proof is the broker's OWN order reference. `_resolve_sending_stage` now leaves EVERY idless row (any status, `failed` included) `UNKNOWN`, so it never earns a retry, and the immediate per-leg answer is treated the same way: an answer with NO order id resolves the stage to `UNKNOWN` (blocking, blockers named), never `rejected`. `_leg_outcomes` and the production `BasketOrderResponse` parse were both corrected (the boundary's `results` array is read by `index`, so an ACCEPTED leg is recognised instead of being misread as idless). Only an order reference releases the stage; absence of one never does | `test_an_idless_leg_is_named_unknown_and_never_retried_on_a_guess` (idless leg → UNKNOWN, no retry, no new claim), `test_a_post_acceptance_timeout_via_orders_service_is_never_retried` (REAL `OrdersService` + a broker that ACCEPTS then loses the response: the run carries the idless `failed` row, the stage is UNKNOWN, the repeat pass places NOTHING) |

The endpoint is a SAFETY surface, so it reports only what the server will accept:
`account_scopes`, `job_kinds` and `stale_exit_policies` are unchanged; `live` joins
`execution_modes` ONLY when `HOSTED_LIVE_ENABLED` is true; `live_lanes` is empty
while live is off and otherwise names only the lanes whose step builder is
registered AND whose plan kind the executor admits (derived, never hardcoded);
`live_requires_owner_approval` is true. Evidence:
`tests/api/test_hosted_live_capabilities.py` (3 passed) - flag off, flag on, and a
lane whose builder is removed disappearing from the advertisement.

A `releasing` claim means the release pass committed and the process may or may not
have reached the broker. "The claim names no order" is what a LOST RESPONSE looks
like, so it proves nothing. `LiveDispatchFence` decides, from the platform's own
durable pre-send record (`live_order_intents`, written by the order path BEFORE the
broker write, keyed by the account and the immutable step-reference idempotency
key):

| Fence | Meaning | Action |
| --- | --- | --- |
| no pre-send record | no broker write was ever attempted for this step | the bounded disposition may proceed; the fence evidence is recorded in the claim, the audit row and the disposition record |
| pre-send record naming a broker order | the order EXISTS | the discovered reference is ADOPTED onto the claim (`pending`), a `release_recovered` trail row is appended, capacity is retained, and ingestion owns the outcome |
| pre-send record with no order id, no authoritative read | UNKNOWN | refused `LIVE_REPAIR_RELEASING_OUTCOME_UNKNOWN`: nothing is abandoned, nothing is unblocked, capacity is retained, the step stays in flight and is NEVER retransmitted |
| pre-send record + authoritative broker read finds the order | accepted | adopted, as above |
| pre-send record + COMPLETE authoritative read finds no order | nothing was accepted | the disposition may proceed |

An unavailable or inconclusive read is always UNKNOWN, never "absent".

## Executed checks (outside the sandbox, disposable PostgreSQL 15433)

| Command | Result |
| --- | --- |
| `pytest tests/integration/test_hosted_live_phase2b_routes_postgres.py -q` | **4 passed** (the releasing test now covers six fence scenarios, including a known subset that cannot prove the rest) - the futures roll (acquisition FULL fill releases the close, partial holds it, the released close is an absolute flat of the attributed 75 rather than the doubled frozen target, duplicate pass sends nothing, unbound plan refused `ROLL_CLOSE_REQUIRES_BINDING`), the option entry (hedge first, partial hedge keeps the short withheld, full hedge releases the short exactly once), the frozen expiry cutoff refusal, and the four `releasing`-window scenarios |
| `pytest tests/integration/test_hosted_live_phase2b_protection_postgres.py -q` | **11 passed** (the cross-account, concurrent-reconciler, paused-sender, idless-leg and production-`OrdersService`-timeout acceptance; the paused-sender test moves the clock 600s past the 300s lease before the second pass, and the timeout test drives the REAL `OrdersService.place_basket` with a broker that accepts then loses the response) - the MIS dead-child square-off executed end to end through the real protection runtime to the fake broker (and never twice), the staged option structure exit (SHORT closes first, NO hedge order until the whole short is proven closed, the hedge then released ONCE for exactly what the run owned, duplicate passes place nothing, complete only when the run's own fills say flat, child token revoked), one closed short not unlocking another's hedge, and unknown attribution refused |
| `pytest tests/integration/test_hosted_live_phase2a_routes_postgres.py -q` | **10 passed** (Phase 2A regression, unchanged); `pytest tests/api/test_hosted_live_capabilities.py -q` **3 passed** |
| `pytest tests/strategies/test_execution.py -q` | **60 passed** |
| `pytest tests/strategies/test_live_settings.py tests/strategies/test_live_submission_store.py tests/strategies/test_settlement.py -q` | **74 passed** |
| `pytest tests/integration/test_hosted_live_phase1_postgres.py tests/integration/test_hosted_live_phase1_routes_postgres.py tests/integration/test_live_adapter_preparation_postgres.py -q` | **40 passed** after updating one stale pin in `test_live_adapter_preparation_postgres.py`: `test_a_compound_plan_kind_is_a_named_refusal` used `target_futures` as its unsupported kind, which Phase 2B legitimately made supported; it now uses `intent_bundle`, which is still a named refusal |
| `pytest tests/strategies/test_admission.py tests/strategies/test_proposals.py tests/strategies/test_weights_compiler.py tests/strategies/test_lifecycle_prepare.py tests/strategies/test_reconciliation.py tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q` | **127 passed, 14 skipped** |
| `pytest tests/options/test_exit_builder_structure.py tests/options/test_hedge_fill_gating.py tests/options/test_expiry_policy.py tests/options/test_options_protection_evaluator.py tests/options/test_options_execution_lifecycle.py tests/options/test_bridge.py -q` | **77 passed, 6 subtests passed** - the options rules this bundle reuses are unchanged |

Known PRE-EXISTING failures in this working tree, unrelated to this bundle and not
fixed here:

* `tests/api/test_control_plane_api.py` - 6 tests patch `api.*` module paths that do
  not exist in this layout (`ModuleNotFoundError: No module named 'api'`); 8 pass.
* `tests/options/test_options_api_routes.py::test_main_registers_canonical_options_routers`
  - reads `/home/krishna/kite-algo/main.py`, which does not exist in this checkout.
* Several `tests/options/test_options_market_*` / `test_options_redis_cache.py` files
  do not complete inside a bounded run in this environment (they appear to wait on
  Redis/market services); they are not part of this bundle's surface.
* `tests/api/test_strategy_owner_and_binding.py` must still be run in its own
  process (it installs a fake `psycopg2` at import).

## Boundaries / remaining

* `HOSTED_LIVE_ENABLED` is false; nothing in this bundle enables it.
* The staged structure exit is throttled by the protection runtime's existing
  60-second re-claim guard (`_has_recent_exit_claim`), so a hedge release can
  follow the proven short closure by up to that interval. It is a bounded delay at
  a staged boundary, not a correctness property.
* **The stage lease is an observation, not proof.** An unresolved `sending` /
  `unknown` stage keeps blocking a new claim EVEN AFTER its lease expires, because
  there is no fence at the broker write: a sender paused past its lease can still
  resume and call the broker, so elapsed time can never prove non-submission. The
  stage is released only by durable evidence that resolves EVERY leg. The
  deliberate trade: an automatic no-fence crash recovery is given up for a correct
  BLOCKED state - with no durable broker evidence, the stage stays blocked for
  operator disposition rather than being retried. (The 300s lease is still
  stamped and reported, and the observation timeout is retained; it is simply
  never used as a cessation proof.)
* **An idless `live_order_intents` row is never non-submission.** The live order
  path (`OrdersService.place_order`) writes the SAME idless `failed` status for an
  explicit refusal and for a generic failure that can happen AFTER the broker
  accepted (a timeout, a socket close, a process death with the broker holding the
  order) - the two are told apart only by free text in `error_json`, which is not
  evidence. So an idless row (any status, `failed` included), and an immediate
  per-leg answer with no order reference, are both `UNKNOWN`: nothing is retried,
  no capacity is released and no settlement is proven from them. The accepted cost
  is that a genuinely refused exit leg cannot currently be proven from this table
  and therefore blocks for operator disposition rather than retrying; preserving
  retry for a real refusal would need the order path to record a structured
  non-submission marker, which is a separate follow-up outside this bundle.
* The SDK/frontend supported-mode flow and the rollout remain with the next
  bundle.
* `MisStaleExitPolicy` (the `exit_on_worker_stale` library) still has no direct
  production caller of its own: the protection runtime's `worker_stale` rule is
  the production path for the same condition, and the Phase 2B protection suite
  executes it end to end. The library's separate `apply` entry point remains
  unwired, and this bundle does not claim otherwise.
* A `withheld` leg whose authority has expired stays in flight with a named
  blocker; the bounded operator disposition covers residuals, and a "continue the
  plan" path (a replacement step for an abandoned residual) is still a separate
  plan.
