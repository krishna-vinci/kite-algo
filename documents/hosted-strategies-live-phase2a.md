# Hosted live release — Phase 2A (durable multi-step engine, CNC and MIS)

Date: 2026-09-22. Baseline `806e71f` plus the shared Phase 1 live code.
**No commit, no push, no deploy, no production database, no real order and no
notification.** `HOSTED_LIVE_ENABLED` remains unset (false) everywhere.

Phase 2A is the prerequisite for every remaining lane plus the two lanes that need
no new domain engine: **CNC/portfolio baskets** and **MIS**. Futures/rolls,
options, the SDK/frontend supported-mode flow and the final rollout stay with the
next bundle.

## What shipped

| Surface | Change |
| --- | --- |
| `backend/alembic/versions/20260922_000040_live_plan_executions.py` | **New parent table** `live_plan_executions` (`UNIQUE (plan_id)`, `lane`, `state`, immutable `step_spec`), `withheld`/`releasing` added to the live-claim state vocabulary, and the `futures_roll`/`option_structure` lanes RESERVED so the next bundle adds no constraint migration. `schema.sql` mirrored |
| `backend/strategies/live_sequence.py` | The durable multi-step engine: `StepSpec` (immutable ordered step/dependency specification), the **lane extension contract** (`register_live_lane`), the CNC and MIS lane builders, `materialize` (parent + every step claim + barrier work in ONE transaction), the per-leg settlement rule, and the withheld-step scan/blocker records |
| `backend/strategies/live_adapter.py` | `submit` is now **materialize-then-dispatch**: validate → freeze the whole step set → one transactional parent → dispatch only the steps with no gating. Weight legs size from the FROZEN capital basis (the manufacturer's own `_target_quantity` arithmetic). New `release_step` (re-validate + CAS `withheld → releasing` under the book lock, then dispatch) and `_rewind_release` for a pre-send refusal. Per-step `state`/`withheld`/`depends_on` travel to the API |
| `backend/strategies/live_service.py` | Lanes (`single_instrument`, `target_weights`, `mis`) selected from PERSISTED plan content; per-step execution trail; the **shared sequence release pass** (`release_sequence`); the MIS square-off clock rule (exchange-local, see below); MIS square-off evidence; the retryable parent settlement |
| `backend/strategies/live_ingestion.py` | The capacity stage is now PER LEG: the claim's leg is recorded terminal and the parent reservation is settled only by the parent's own rule. The shared sequence pass runs inside `poll_once` after the outcome pass |
| `backend/strategies/live_repair.py` | Residual disposition redesigned (see "Corrections"): tri-state authority, atomic disposition, per-leg capacity |
| `backend/strategies/settlement.py` | The live in-flight enumeration counts `withheld`/`releasing` steps AND the parent itself while it is not settled, and it NAMES the bound order ids that have no authoritative terminal status |
| `backend/strategies/admission.py`, `backend/api/routers/strategies.py` | A weight leg's notional/margin is computed from the frozen capital basis instead of being mistaken for a share count |
| `backend/app/bootstrap.py` | The production consumer is started with the shared release pass bound to the same `LivePlanExecutor` the execute route uses |

## The durable multi-step protocol

`live_plan_submissions` is a PER-STEP claim; `live_plan_executions` is the durable
PARENT, one row per frozen plan, carrying the ordered step/dependency
specification frozen at **first admission**. It is not a second execution ledger:
per-leg claims stay where they were, and the parent never duplicates them.

Materialization is atomic and idempotent. Under the canonical book lock
(`ExecutionBarrier.lock_book`, the same lock the barrier work events take) one
transaction writes the parent, every step claim and the barrier `work_created` for
each actionable step; a failure rolls all of it back, and a concurrent second
executor (or a restart) reads the winner's rows instead of creating a second
protocol. A step whose prerequisites are unmet — or whose LANE gates it — is
written as `withheld`: in-flight work, never dispatched, and enumerated by the
settlement barrier so it cannot disappear behind a quiet proof.

**The frozen sizing is never re-derived.** The delta recorded at first admission
is what a released leg dispatches, even after a partial fill moved the attributed
book; a released leg with a smaller attributed quantity is clamped by the lane's
own rule (MIS uses `attributed_exit_size`), and both the frozen and the released
quantity travel in the record.

**Capacity is per leg.** The reservation must cover every increasing leg of the
parent (`capacity_covers`), not merely the one dispatched first. When a leg
terminates, the parent records the leg and then settles ONLY when every leg is
terminal: `released` when nothing filled, `consumed` when a fill exists (the
ledger has no partial release, so capacity that now backs exposure is not handed
back), `retained` while any leg is still pending or withheld. The settlement is
idempotent and retryable, so a failed effect is a recoverable state.

## The shared sequence release pass

`LivePlanExecutor.release_sequence` runs from the outcome consumer's pass (and is
callable on its own). A `withheld` step is dispatched ONLY when, at that moment:

1. every prerequisite leg is **`filled`** (a rejection, cancel, uncertain or
   partial fill releases nothing);
2. the deployment flag is on;
3. the **persisted** evaluation authority still re-derives: run open + live, token
   active/allowing `live`/holding `intents:submit`, hosting job in an authority
   status with a live lease at the current `attempt`;
4. the owner approval still validates against the frozen plan;
5. the reservation still validates and still covers every outstanding leg;
6. admission re-passes (fresh margin + quote evidence, current attribution).

Item 4 is pin-by-pin, and the ONE pin a moving multi-leg book may legitimately
change — the exposure snapshot — is tolerated only against a PROOF that the
movement is this parent's own: per instrument,
``current_attributed == frozen_current_at_admission + Σ own confirmed filled
delta``. Another plan of the same run, a manual trade or a corporate action on
that instrument breaks the identity and the release is refused
``LIVE_SEQUENCE_BOOK_MOVED_BEYOND_OWN_FILLS`` (the claim carries the expected and
actual quantities), so a frozen delta can never be traded against a book it no
longer describes. Every other pin — plan hash, reconciliation version, catalog
state, session products, reservation activity, approval window — is enforced
unconditionally.

Anything else records a NAMED blocker on the claim (`release_blocked` plus the
detail) and places NOTHING. A restarted process, a repeated callback or an expired
attempt can never turn into a new order. A pre-send refusal rewinds the claim to
`withheld` so a later pass may retry; a step that reached the broker can never
take that path (transport uncertainty is recorded, never raised).

## CNC / portfolio baskets

Reuses the `target_weights` compiler's frozen output: full-snapshot semantics
(every scope member appears; an omitted member is an explicit zero), the pinned lot
and the pinned reference price, and the compiler's own
`_target_quantity` arithmetic against the capital basis FROZEN with the plan.

Ordering is the compiler's rule, expressed as durable dependencies: reducing legs
are ready, each increasing leg depends on every reducing leg
(`release_rule = all_prerequisites_filled`). With no reducing leg there is nothing
to wait for, so an increase is ready immediately. Funds and margin are read at
release time as CURRENT evidence — sale proceeds are never counted before they
fill.

## MIS

MIS is not a plan kind: it is the ordinary `single_instrument` shape carried under
the frozen intraday product, so the product decides the lane. The intraday policy
(`mis_policy.validate_intraday_scope`) is already applied by the compiler.

A risk-INCREASING MIS leg is ordinary. A risk-REDUCING MIS leg is `withheld` under
`release_rule = mis_squareoff` and is released only by the platform's own
conditions:

* the **square-off clock** (`mis_squareoff.scheduled_time_for`, the same schedule
  the protection runtime uses) has been reached for the session, compared in the
  **exchange-local** zone — the schedule is wall clock, so comparing it to a UTC
  instant would read 15:20 as 20:50 IST and be a guessed close;
* or the MIS **stale-worker exit** policy is armed and the bound run's heartbeat is
  stale beyond the policy (`mis_stale_exit`);
* or the operator has asked the attempt to stop (risk reduction is always
  permitted).

Before any of those hold, the pass records `MIS_SQUAREOFF_NOT_DUE` and sends
nothing. On release the exit is sized by `attributed_exit_size` (an exit can never
reach another strategy's shares) and the platform records its OWN
`strategy_squareoff_evidence` row (`outcome = squared_off`, or `action_required`
when the release was refused) naming the clock that released it, the frozen and
released quantities and the attempt.

### Coverage boundary (do not over-read this lane)

The hosted release pass runs the authority check of item 3 FIRST, and a
risk-reducing leg is only released while the child's PERSISTED authority holds.
That is deliberate: a dead or stopped attempt must not be handed new authority by
the hosted path. The consequence is explicit and now pinned by a test — once the
child's credential is revoked and the job stopped, the withheld MIS exit STAYS
withheld, records `TOKEN_NOT_ACTIVE` (or `HOSTED_STOP_REQUESTED`), places nothing
and invents no square-off evidence.

The dead-child square-off is the platform's OWN control-plane exit, which does not
need a child credential at all:

`backend/app/background.py::_worker_protection_loop` builds
`WorkerProtectionRuntime(squareoff_schedule=...)` (the schedule resolver reused by
`mis_squareoff.squareoff_schedule`) and submits through
`protection_runtime.submit_worker_protection_exit` ->
`control_plane.exit_control_strategy` -> `_exit_live_worker_run`, which constructs
a CONTROL-PLANE token with `allowed_modes=["live"]` and
`allowed_actions=["runs:exit"]` and exits the run's own attributed legs
(`backend/api/services/control_plane.py:528-537`,
`backend/api/routers/worker_execution.py:398+`). It is exercised (with the live
helper patched) by `tests/api/test_control_plane_api.py::test_exit_live_worker_strategy_uses_existing_live_exit_helper`.

Two honest limitations of that sentence, stated rather than papered over:

* this suite does NOT execute the protection runtime's square-off end to end; it
  cites the existing path and the existing test, and it proves only the hosted
  pass's refusal;
* `mis_stale_exit.MisStaleExitPolicy.apply` is a library with no production caller
  in this repository today (`_claim_submitter` is never wired), so the MIS
  stale-worker exit is NOT claimed as wired by this bundle; the hosted pass models
  the same condition (armed policy + stale heartbeat) only for its own release
  gate.

## Corrections carried in this bundle

| Issue (root acceptance) | Fix | Evidence |
| --- | --- | --- |
| `live_repair._authority_still_live` caught EVERY exception (and a missing run) and answered "not live", so unreadable evidence authorised abandoning a residual | `_authority_state` returns `live` / `gone` / `unknown`: only a named, PROVEN withdrawal (`RUN_NOT_OPEN`, `TOKEN_NOT_ACTIVE`, `TOKEN_EXPIRED`, `HOSTED_STOP_REQUESTED`, `HOSTED_JOB_NOT_AUTHORITY`) authorises a disposition; a missing run, a missing job/token, an unreadable source or an unnamed refusal is `unknown` and the disposition is REFUSED `LIVE_REPAIR_AUTHORITY_UNKNOWN` | `test_residual_disposition_refuses_unknown_authority_evidence` (unreadable reader, inconclusive refusal, missing run) |
| `abandon_residual` released the WHOLE parent reservation despite a partial fill, and read the step/evidence outside any transaction | The disposition is ONE transaction on the book lock: authority re-read, row-locked step, CAS `repair_required → residual_abandoned`, barrier `work_resolved` once and the append-only trail row — all or nothing. Capacity now follows the parent's rule: `released` only when every leg is terminal with NO fill, `consumed` when a fill exists, `retained` while another leg is outstanding | `test_residual_disposition_retains_the_capacity_of_other_legs`, `..._consumes_capacity_a_real_fill_backs` (which replaces the Phase 1 assertion that a partial fill released the reservation), `..._rolls_back_when_the_audit_write_fails` |
| The approval's exposure-snapshot pin made a released dependent leg impossible (the plan's own fill legitimately moves the book) | A RELEASED leg re-validates EVERY pin; `EXPOSURE_SNAPSHOT_CHANGED` alone may be explained away, and ONLY by the per-instrument identity proof `current == frozen_current_at_admission + Σ own confirmed filled delta`. An unexplained movement is refused `LIVE_SEQUENCE_BOOK_MOVED_BEYOND_OWN_FILLS` with the expected/actual quantities, so the frozen delta can never over-target a book another plan, a manual trade or a corporate action moved | `test_cnc_full_snapshot_...` (own fills release), `test_cnc_release_blocks_when_the_book_moved_beyond_this_plans_own_fills` (another order of the same run moves the same instrument -> blocked with the mismatch recorded) |
| Root review: the MIS clock release derives the child's authority first, so a dead child cannot square off through this path | Confirmed and PINNED as a boundary: the hosted pass refuses (`TOKEN_NOT_ACTIVE`/`HOSTED_STOP_REQUESTED`) and places nothing, never renewing a dead child's authority; the platform's own control-plane exit is the risk-reduction path for that case (cited above, plus this suite's refusal test). The MIS lane is NOT claimed as a universal square-off engine | `test_mis_squareoff_is_refused_when_the_child_authority_is_gone` |
| A weight leg's notional/margin read the weight as a share count | `AdmissionService.plan_notional` and the route's margin reader size a weighed leg from the frozen capital basis | `test_cnc_...` reserves against the real gross; the admission suite is unchanged |
| The operator's residual route could not address a leg other than step 1 | `step_no` is optional: the server resolves the plan's own `repair_required` step and refuses on ambiguity (`LIVE_REPAIR_NOT_REQUIRED_AMBIGUOUS`) | residual suite |

## Extension contract for the next bundle (futures/options)

The parent protocol, the transactional materialization, the withheld-dependency
release pass and the settlement rule are LANE-AGNOSTIC. Wiring a lane means:

1. **A lane builder.** `live_sequence.register_live_lane(lane, builder)` where the
   builder takes a `LaneContext` (`plan`, `binding`, `authority`, `execution_id`
   and `size_leg(leg)` — the adapter's frozen attribution-based sizing) and returns
   an ordered `List[StepSpec]`.
2. **Dependencies, not assumed order.** `StepSpec.depends_on` is the release
   graph: a futures roll's old-contract CLOSE depends on the replacement
   acquisition; an option structure's short entry depends on the hedge fill.
   `sells-before-buys` is the PORTFOLIO lane's rule and is not applied anywhere
   else. `release_rule` selects the rule: `immediate`,
   `all_prerequisites_filled`, or a lane rule.
3. **A lane rule** (optional). `LivePlanExecutor._lane_release_rule` is the hook
   for a condition that is not another step (MIS's clock is the worked example).
   Lane-specific sizing belongs in `LivePlanAdapter.dispatch_step`, next to
   `_mis_squareoff_size`.
4. **Registration + vocabulary.** Add the lane to `live_sequence.LANE_*`
   mappings in `lane_for_plan`, to `live_service.LIVE_PLAN_KINDS` and to
   `live_adapter.LIVE_SUPPORTED_PLAN_KINDS`. The migration ALREADY admits
   `futures_roll`/`option_structure` in `ck_live_plan_execution_lane`, and the
   step vocabulary already carries `withheld`/`releasing`, so no constraint
   migration is needed.
5. **Reuse, do not rebuild.** The paper executor's `execution._roll_binding` /
   `_roll_preconditions` / `_refuse_ungated_roll_close` and
   `_resolve_option_target` / `_option_run_steps` / `_begin_option_run` plus
   `options.protection.hedge_gate` / `exit_builder` are the domain rules to reuse;
   `compiler/pinned_units` is the pinned lot source. Nothing in the parent needs to
   know what a roll or a structure is.
6. **Nothing else changes.** The barrier enumeration, the outcome consumer, the
   per-leg capacity rule, the residual disposition and the API shape already cover
   a new lane.

## Executed checks (outside the sandbox, disposable PostgreSQL 15433)

| Command | Result |
| --- | --- |
| `pytest tests/integration/test_hosted_live_phase2a_routes_postgres.py -q` | **10 passed** — CNC full-snapshot sequencing (partial sell withholds the buy, full fill releases it exactly once, duplicate/restart release sends nothing), authority-gone refusal with a named blocker, book-moved-beyond-own-fills refusal with the identity mismatch recorded, uncertain release never retransmitted, MIS released by the platform clock only (with `squared_off` evidence and attributed-size clamping), MIS refused when the child's authority is gone, and the four bounded residual-disposition scenarios |
| `pytest tests/integration/test_hosted_live_phase1_postgres.py tests/integration/test_hosted_live_phase1_routes_postgres.py tests/integration/test_live_adapter_preparation_postgres.py tests/strategies/test_settlement.py tests/strategies/test_live_settings.py tests/strategies/test_live_submission_store.py tests/strategies/test_execution.py -q` | **175 passed** |
| `pytest tests/strategies/test_admission.py tests/strategies/test_proposals.py tests/strategies/test_weights_compiler.py tests/strategies/test_lifecycle_prepare.py tests/strategies/test_reconciliation.py tests/api/test_strategy_owner_and_binding.py tests/integration/test_hosted_supervisor_lifecycle_postgres.py -q` | **195 passed, 14 skipped** |
| from-zero `alembic upgrade head` on a unique disposable DB | head `20260922_000040`; `ck_live_plan_submission_state` includes `withheld`/`releasing`; `ck_live_plan_execution_lane` admits the five lanes; `uq_barrier_work_resolved_live_step` present; DB dropped |

## Boundaries / remaining

* `HOSTED_LIVE_ENABLED` is false; nothing here enables it.
* Futures/rolls and options are still NAMED refusals
  (`LIVE_PLAN_KIND_UNSUPPORTED`) — the extension contract above is the wiring
  point, not a partial implementation.
* The SDK/frontend supported-mode flow and the rollout remain with the next
  bundle.
* A `withheld` leg whose authority has expired stays in flight with a named
  blocker; the bounded operator disposition covers residuals, and a "continue the
  plan" path (a replacement step for an abandoned residual) is still a separate
  plan.
* The consumer's release pass runs inside the outcome poll (bounded, error
  isolated, health reported). A dedicated cadence is a tuning decision, not a
  correctness one.
