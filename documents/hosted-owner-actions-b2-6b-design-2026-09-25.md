# B2.6b design note — safe owner actions and dead-submission disposition

## Decision summary

B2.6b adds four narrow, owner-scoped actions: cancel only qualifying pending
entry work, exit one governed option structure, flatten one hosted strategy's
exposure, and resolve one provably dead plan submission. The unsafe `/control`
routes stay unwired: `cancel-orders` would act on all of a run's working orders
(`backend/api/routers/control.py:62`), while `/control` exits bypass option
governance (`backend/api/routers/control.py:50`,
`backend/api/services/control_plane.py:507`). The existing four-card Options
surface remains (`frontend-next/features/strategies/components/hosted-options-panel.tsx:524`).

Authorization is uniform for mutations:

- hosted owner identity from `require_strategy_owner`
  (`backend/api/routers/strategies.py:203`), never a caller value;
- same-origin enforcement (`backend/api/services/csrf.py:60`);
- server-derived strategy/account scope like option-run repair
  (`backend/api/services/option_run_repair.py:31`), plus account authz;
- a required UI acknowledgement plus `reason` for exit/flatten.

Autonomous protective exits and platform square-off remain outside these routes.
Consistent with R3 §15, the new discretionary owner actions may reduce risk but
never cancel protective, in-flight exit, or already-authorized risk-reducing
work.

## 1. Cancel pending work

### Qualifying orders

An order qualifies only when the server can prove all of the following:

1. It belongs to this hosted `strategy_id`, account and environment through
   durable attribution. Paper candidates come from the plan trail's
   `paper_order_id` plus `paper_order_fill_progress`; live candidates come from
   `live_plan_submissions` plus the ordinary broker order projection. The trail
   and live claim models are `backend/strategies/attribution_models.py:1176`
   and `backend/strategies/attribution_models.py:725`.
2. Its plan step has an unresolved `submitted` event, or an accepted-but-open
   paper remainder. `option_plan_execution_state` is the existing proof that a
   committed submission is unfinished (`backend/options/execution/plan_binding.py:640`).
3. The step is exposure-increasing for this strategy's attributed book. Option
   entry execution already derives each step from the run's own fills and marks
   whether it opens or grows exposure (`backend/strategies/execution.py:1284`).
   For a non-option plan, compute the signed open quantity from the strategy's
   own attributed fills and classify the delta similarly; never infer it from a
   client order list.
4. For an option entry, the frozen leg is not protective:
   - `role == "hedge"` never qualifies;
   - a long leg whose frozen target covers a short in the same option type
     never qualifies unless the frozen `protection_policy.naked` is true;
   - a missing/unreadable role or coverage basis refuses rather than becoming
     cancellable.
   Roles are frozen by the compiler (`backend/strategies/compiler/option_structure.py:408`).

Never qualify: option hedges or other protective-stage orders; staged exits and
their `sending/unknown` claims; exit, reduction, adjust, roll, square-off or
continuation work; worker-owned live intents outside the governed plan trail;
another strategy/account/environment's orders; and an order whose ownership or
remaining quantity cannot be proved.

### Behaviour and transitions

`GET .../owner-actions/pending-work` returns a stable `evidence_digest` and each
candidate with `plan_id`, `step_no`, `order_id`, `remaining_quantity`, and
cancellation eligibility. POST must carry that digest. Between preview and POST,
any fill, terminal outcome, protective stage, or state change invalidates the
request.

Paper: call the paper cancel boundary, verify `paper_order_fill_progress`
reports terminal `cancelled` with zero remaining, then append the terminal trail
outcome. Live: call the broker cancel only for a known order id and verify the
broker projection is terminal; an ambiguous cancel becomes ordinary
dead-submission evidence work, never an assumed cancellation.

A partial fill is preserved. Record `partially_filled` for the proven quantity
and `failed` with `disposition=owner_cancelled` for the cancelled remainder.
The existing trail vocabulary supports both outcomes
(`backend/strategies/attribution_models.py:1190`). Record the corresponding
work-resolved barrier event only after the remainder is terminal.

Option entry states are derived from the run's own fills and lifecycle:

- no filled leg: walk the existing edge to `cleanup_required`
  (`backend/options/execution/lifecycle.py:15`);
- at least one filled leg but incomplete target: `partial_entry`
  (`backend/options/execution/lifecycle.py:20`);
- complete target: `entered`.

The repair path can then classify `partial_entry`/`cleanup_required` from its
own fills (`backend/options/execution/repair.py:1`). Non-option plans have no
extra run state; the terminal trail and barrier are the disposition.

Idempotency: the action key is
`owner-cancel:{strategy_id}:{account_id}:{environment}:{plan_id}:{step_no}`.
Broker/paper cancellation repeats safely through that key, but trail mutation
runs once in the plan transaction and refuses `CANCEL_EVIDENCE_CHANGED`.
Audit writes the owner action to the proposal journal, the exact terminal
outcome to the plan trail, and the hosted job audit when a run is involved.

## 2. Exit structure

Use the staged structure exit, not a freshly compiled caller-order plan. The
run's confirmed fills—not a new order list—are the authority.
`StagedStructureExit.plan_exit` derives only current risk-reducing actions
(`backend/options/protection/staged_exit.py:778`), and
`build_structure_exit_orders` enforces short-first closure and
proof-before-hedge-release (`backend/options/protection/exit_builder.py:50`).
The existing engine refuses new work while an unresolved `sending/unknown`
stage owns the run (`backend/options/protection/staged_exit.py:89`).

Extend the repair assessment to admit an owner-exit request for `entered`,
while retaining all existing gates:

- ownership/scope through `option_run_repair_scope`
  (`backend/api/services/option_run_repair.py:31`);
- in-flight adjust ownership through `option_adjust_owner_state`; it must be
  `finished` (`backend/options/execution/plan_binding.py:723`);
- unresolved protective stage refuses before any new submission;
- unreadable/unattributed fills refuse through the existing ambiguous-repair
  evidence rule (`backend/options/execution/repair.py:80`);
- another run moving wins the CAS and causes `OPTION_RUN_STATE_CHANGED`
  (`backend/options/execution/repair.py:437`).

A POST is the owner-authorized discretionary exit. It does not alter protection
policy and does not wait for a new approval artifact. On paper, reuse the paper
boundary and staged submitter (`backend/api/services/option_run_repair.py:183`,
`backend/api/services/option_run_repair.py:262`). On live, reuse the live basket
boundary already built for backend protection
(`backend/api/services/protection_runtime.py:517`), but stamp
`entry_surface="hosted_option_owner_exit"` and
`source="owner_discretionary_exit"`. Live remains fail-closed if that boundary
or broker session is unavailable.

The first POST returns one accepted stage. Hedges stay until every short is
proven closed by confirmed fills. Later reconciliation may release the next
stage; the UI calls the same status endpoint and then POST again to continue a
multi-stage exit. Completion means `run_is_flat` is true and the run is
`exited` (or `settled` only through the separate settlement-evidence path).
Do not change `entered` when the first stage is merely accepted.

## 3. Flatten

Flatten is a strategy-scoped orchestration, not a whole-account liquidation.

1. Require or perform stop-evaluator first. If an active evaluation cannot be
   stopped, refuse `FLATTEN_EVALUATION_ACTIVE`; otherwise no new plan can race
   the snapshot.
2. Resolve dead submissions and unfinished protective stages before flatten.
   Flatten must not guess whether an unanswered order exists. Refuse with
   `DEAD_SUBMISSION_UNRESOLVED` and list the disposition URLs.
3. Cancel only qualifying pending entry work from section 1. Leave reducing
   orders alone.
4. Exit option runs one at a time. Within each run, the staged engine keeps
   short-first ordering and proof-based hedge release. Never merge legs across
   runs, because each run owns its hedge proof.
5. Close non-option books with governed reduction plans derived from attributed
   books: one frozen plan per `(instrument, product, signed_open_quantity)`,
   target zero, pinned catalog generation. Reuse the paper pipeline through the
   governed execute route (`backend/api/routers/strategies.py:2402`) and
   executable single-instrument/intent bundles
   (`backend/strategies/attribution_models.py:1072`). A plan that would increase
   exposure is refused before admission.
6. Recompute the manifest after each terminal outcome. Partial failure marks
   only that item `blocked`, preserves completed reductions, and returns a
   resumable operation. A failed option short leaves protection in place and
   requires repair/dead-submission disposition before retry.

Done means all of: no qualifying pending entry; no live unresolved submission;
every option run's own fills are flat and its status terminal; every attributed
equity/future `(instrument, product)` is zero against broker truth; no in-flight
governed work; and no live evaluation authority. Until then the response is
`in_progress` or `blocked`, never `complete`.

Paper uses the paper runtime. Live option exits use the live staged boundary.
Live non-option flatten is allowed only through the governed live reduction
pipeline when that lane can submit reductions; otherwise refuse
`FLATTEN_LIVE_NONOPTION_UNSUPPORTED` while continuing to report
already-completed option work as done.

## 4. Dead-submission disposition

This is an owner/operator disposition for one unanswered generic plan step. It
does not apply to staged protective exits: those must resolve through
`StagedStructureExit`'s pre-send records and ordinary fills.

`GET .../plans/{plan_id}/steps/{step_no}/dead-submission` returns the trail
state, execution environment, linked order evidence, filled/remaining quantity,
allowed terminal dispositions, and an `evidence_digest`. Evidence is read from
the paper order/progress row or broker order projection; the owner cannot type
an outcome into existence.

POST accepts `{evidence_digest, disposition, reason}` and permits only:

- `filled`: proven fills cover the full requested quantity;
- `rejected`: broker/paper terminal rejection with zero filled;
- `cancelled`: terminal cancellation with the proven fill preserved;
- `failed_never_submitted`: durable pre-send/dispatch records prove the step
  did not reach the order path;
- `failed_residual_abandoned`: terminal order plus zero remaining, after every
  proven fill is recorded.

Refuse, by name: missing/unreadable evidence; changed digest; open remainder;
staged protective order; unknown live send; disposition inconsistent with
platform evidence; and a step still owned by an active evaluator. Unanswered
means unresolved, not dead.

On success, append the allowed terminal event to the insert-only plan trail in
one transaction with barrier settlement. The trail is explicitly the schema's
execution state (`backend/strategies/attribution_models.py:1176`); details
carry the source order id, platform status, owner, reason, and disposition. Do
not directly edit `adjusting`; once the owning adjust is provably terminal,
`option_adjust_owner_state` becomes `finished` and the existing takeover/repair
gates proceed.

## 5. API and UI contract

All routes are under `/api/strategies/{strategy_id}`. Responses share `status`
(`complete|accepted|blocked`), `action_id`, `evidence_digest`, `items[]`,
`refusal` (optional), and `audit_id` (optional). Errors use HTTP 409 with
`rejection_reason`.

```jsonc
// GET /owner-actions/pending-work
{ "coverage": "known", "evidence_digest": "sha256...", "items": [
  { "plan_id": "p1", "step_no": 2, "order_id": "o1", "remaining_quantity": 10,
    "eligibility": "eligible", "reason_code": null } ] }

// POST /owner-actions/cancel-pending
{ "evidence_digest": "sha256...", "reason": "owner_cancel" }

// GET /option-runs/{option_run_id}/exit
{ "adjust_owner_state": "finished", "protective_stage_state": "resolved",
  "state": "residual", "close_plan": [], "evidence_digest": "sha256..." }

// POST /option-runs/{option_run_id}/exit
{ "evidence_digest": "sha256...", "reason": "owner_exit" }

// POST /owner-actions/flatten
{ "reason": "owner_flatten", "stop_evaluator": true }

// GET /plans/{plan_id}/steps/{step_no}/dead-submission
{ "trail_state": "submitted", "source": "paper_order", "status": "cancelled",
  "filled_quantity": 3, "remaining_quantity": 0,
  "allowed_dispositions": ["cancelled"], "evidence_digest": "sha256..." }
```

Named refusals: `CANCEL_EVIDENCE_CHANGED`, `CANCEL_ORDER_NOT_OWNED`,
`CANCEL_PROTECTIVE_ORDER_FORBIDDEN`, `CANCEL_REDUCTION_FORBIDDEN`,
`OPTION_PROTECTIVE_EXIT_UNRESOLVED`, `OPTION_RUN_ADJUST_IN_FLIGHT`,
`OPTION_RUN_STATE_CHANGED`, `OPTION_RUN_EVIDENCE_AMBIGUOUS`,
`DEAD_SUBMISSION_EVIDENCE_UNAVAILABLE`, `DEAD_SUBMISSION_EVIDENCE_CHANGED`,
`DEAD_SUBMISSION_OPEN_REMAINDER`, `DEAD_SUBMISSION_PROTECTIVE_FORBIDDEN`,
`FLATTEN_EVALUATION_ACTIVE`, `FLATTEN_LIVE_NONOPTION_UNSUPPORTED`.

The UI keeps the four separate controls and existing test ids. Cancel exposes
the preview and shows exactly what will and will not be cancelled. Exit and
flatten require dialog confirmation. Render `blocked` as actionable rather than
done. The backend and frontend can build against the schemas above without
sharing implementation code.

## 6. Slices and tests

**S1 — dispositions and cancellation.** Add evidence readers, cancel
classifier, and dead-submission service. Tests: protective hedge refuses; an
eligible naked/short entry admits and preserves a partial fill; changed evidence
refuses; terminal paper/live evidence admits the matching disposition and
refuses a mismatch; a dead adjust makes `option_adjust_owner_state` finished.
Use PostgreSQL for concurrent trail disposition and CAS cancel/transition.

**S2 — single-run governed exit.** Extend repair assessment to owner `entered`
exit while reusing staged derivation and CAS. Tests: unresolved stage, active
adjust, and unreadable/unattributed fills refuse; clean/short-only run admits a
first stage; proven short closure admits hedge release; missing live boundary
refuses before mutation. Include one PG run-state race.

**S3 — flatten orchestration.** Add stop/manifest/ordering/retry. Tests: active
evaluation refuses; reducing pending work survives; option hedges remain until
shorts are proven; partial option failure preserves completed reductions;
completion requires flat evidence and no work; a paper target-zero plan admits
while an exposure-increasing plan refuses. Use PG for operation resume.

## 7. Open questions and recommendations

- Live non-option flatten: defer to C1's governed live reduction lane rather
  than adding direct broker liquidation; fail closed until then.
- Flatten and stop: stop the evaluator first and record it in the flatten audit;
  if stop cannot be proven, flatten refuses.
- Dead submission age: use no time-based auto-resolution. Age is alerting
  context, never evidence.
- Exit reservation/ledger: no new capital reservation for risk-reducing exit,
  but retain ledger/barrier evidence checks and refuse ambiguous fills.
- Operator visibility: expose per-stage status and why hedges are withheld;
  proof-based waiting is expected, not an error.

## Decisions (orchestrator, 2026-09-25)

Design accepted with its §7 recommendations:

- **Live non-option flatten:** fails closed (`FLATTEN_LIVE_NONOPTION_UNSUPPORTED`) until C1's governed live reduction
  lane can submit reductions.
- **Flatten order:** flatten stops the evaluator first and records it. If the stop cannot be proven, flatten
  refuses.
- **Dead submissions:** they are never resolved automatically by age.
- **Exits:** no new capital reservation for a risk-reducing exit.
- **Build order:** S1 (dispositions and cancel) → S2 (single-run exit) → S3 (flatten). The owner Options UI
  (commit 4679956) wires each control as its slice lands.
