# Hosted strategies - integration closure report

**CURRENT STATUS (2026-09-21, final correction pass):**

* **Head `20260921_000038`**, baseline `862cf0a`, branch `development`, uncommitted
  by design. Single Alembic head; from-zero, prior-head and downgrade probes all
  verified on disposable PostgreSQL.
* **Latest verified demo artifact:
  `examples/hosted_acceptance/evidence/acceptance-20260921T174124Z-final.json`**
  (`ok: true`, incl. the `linked_worker_run_closed` axis).
* **Paper readiness:** the single-instrument hosted paper chain (issuance →
  bootstrap → HTTP proposal → admission → paper execution → attribution →
  settlement/reconciliation) is demonstrated end to end.
* **NOT live-ready** and not deployed: the public live plan route still refuses
  `PAPER_ONLY_EXECUTION`, the hosted strategy registry has no live mode
  (`ck_hosted_strategies_execution_mode` allows only `paper`/`dry_run`), live fill
  ingestion is injected rather than wired, and no live market certification has
  been performed.

Sections below are HISTORICAL unless marked current: "Changed surfaces",
"Reproduce" and the earlier blocker lists record the state of their own pass.
Immutable evidence from earlier partial runs is preserved and labelled; nothing
here rewrites it.

## Changed surfaces (historical - Bundle 2/4 + guard hardening pass)

| Surface | Change |
| --- | --- |
| `backend/strategies/service.py`, `backend/api/routers/worker_shared.py`, `backend/api/schemas/worker.py` | `proposals:submit` is a dedicated, explicitly grantable, trade-capable-only action |
| `backend/api/routers/worker_proposals.py` | guard order token → action → run → run access → hosted attempt → session → authority → persistence; identity derived from persisted records |
| `backend/strategies/scheduling.py`, `backend/strategies/repository.py`, `backend/app/background.py` | scheduled occurrence creates a pinned hosted job; claim-fenced decision + job insert in one transaction (`submit_with_decision`) |
| `backend/strategies/execution.py` | pinned units, weight sizing from the FROZEN capital basis with drift refusal, shorts/reversals admitted, option role ordering (engine planner + exit builder), roll seam with **no optional-role bypass**, reconciliation-independent refusal vocabulary |
| `backend/strategies/compiler/{base,target_weights,single_instrument,futures}.py`, `backend/strategies/proposals.py` | pinned units frozen; weights capital basis resolved by the platform and overwritten if a caller supplies one; futures plan freezes `roll.{role,roll_id}` |
| `backend/strategies/rolls.py` | proof is the roll's own recorded replacement executions (idempotent by paper order); plan/coordinate validation; `replacement_filled` event |
| `backend/strategies/settlement.py`, `backend/strategies/repository.py`, `backend/api/routers/strategies.py` | operator reconciliation records the durable barrier proof under the book lock, and the unblock transaction re-validates the proof for the job's own persisted book at the exact version under the same lock |
| `backend/schema.sql`, `backend/alembic/versions/20260921_000035_executable_plan_kinds.py`, `.../20260921_000036_roll_replacement_fills.py` | plan-kind vocabulary widened; roll event vocabulary widened |
| `examples/hosted_acceptance/` | checked-in example strategy, isolated runner, README, sanitized evidence |

## Reproduce (command still current; the artifact quoted below is historical)

```bash
# PostgreSQL test server on 15433 only. Creates and drops its own database.
timeout 900 .venv/bin/python examples/hosted_acceptance/run_acceptance.py --timeout 240
```

Latest artifact of THAT pass (historical; superseded by
`acceptance-20260921T174124Z-final.json` under CURRENT STATUS):
`examples/hosted_acceptance/evidence/acceptance-20260921T163451Z-final.json`
(`ok: true`, entry BUY 10 -> +10 attributed, exit SELL 10 -> 0, child alive through
both executions, stop + cleanup confirmed, `proof_recorded`, reconciliation 200
`trading_settled_flat`, `quiescence_state: verified`).

## Phase and migration arithmetic (historical note; see CURRENT STATUS above)

* Campaign **phases 0..10 = ELEVEN phases**.
* The original phase migrations were **000025..000034 = TEN migrations**; the
  campaign ledger's earlier "ten phases / ten migrations" pairing was wrong.
* Migrations added by that earlier pass: `20260921_000035_executable_plan_kinds`
  and `20260921_000036_roll_replacement_fills` (head was `20260921_000036` then;
  the head is now `20260921_000038` - see CURRENT STATUS). Fresh (`alembic upgrade head`) and prior-head
  (`000034 -> head`, `000035 -> head`) upgrades plus downgrades were verified on
  disposable databases.

## Remaining concrete blockers from the EARLIER pass (historical; superseded by CURRENT STATUS)

> The option-binding and worker-run-closure items below were closed in the
> "Final integration bundle status" and "Acceptance correction pass" sections.
> The live blockers remain true and are restated there.

1. **Durable OptionRunState binding (contract input needed).** The frozen
   `option_structure` plan currently reaches execution through the existing
   engine's planner/exit-builder functions with role-aware ordering and
   full-hedge gating. Binding the plan to a durable option run needs an approved
   edge: a plan↔run reference (and its uniqueness), the runner's state
   transitions per leg (`mark_entering`/`mark_partial_entry`/`mark_exiting`), and
   where the run's own fill proof is read. R3 does not specify that edge, so this
   is left as a named refusal rather than invented.
2. **Live adapter preparation (Bundle 3) not implemented.** Live gates remain
   closed: `PAPER_ONLY_EXECUTION` is untouched, and no live path was enabled or
   claimed. The plan-execution adapter for live needs the approved contract for
   broker async outcomes/reconciliation (a paper fill is synchronous; a broker is
   not), plus the R3 margins/quote-freshness inputs for the live branch.
3. **Worker run terminal status.** A settled, flat, authority-revoked attempt can
   leave `run_status: open`; four-axis settlement does not assert it terminal.
   Closing it needs the approved mapping of "hosted attempt stopped" onto the
   worker-run lifecycle; not forced here.
4. **Emergency risk reduction vs. roll close.** The executor now refuses an
   ungated plan that would close an open roll's old contract
   (`ROLL_CLOSE_REQUIRES_BINDING`). Independently authorized emergency reduction
   must use the existing risk/approval semantics; if that path needs a plan-level
   authorization artifact, its shape is a contract input to confirm.

## Evidence classes used below

**component** (unit), **route** (real HTTP/route path), **paper-demo** (the
isolated end-to-end example), **live-unverified** (nothing implemented/verified).

---

## Final integration bundle status (2026-09-21)

### Now IMPLEMENTED (with the evidence class named)

| Item | Evidence class | Where |
| --- | --- | --- |
| Durable plan -> option-run binding (unique `plan_id`, explicit `option_run_id`, `worker_run_id`, canonical scope, `phase`) | PG migration + PG constraints + SQLite/unit + production executor path | `backend/strategies/attribution_models.py` (`StrategyPlanOptionRun`), new migration `20260921_000037_option_plan_run_binding`, `backend/schema.sql`, `backend/options/execution/plan_binding.py`, `backend/strategies/execution.py` (`_resolve_option_target`/`_begin_option_run`/`_settle_option_run`) |
| Entry plan creates the durable `OptionRunState` from FROZEN inputs; exit plan validates the reference then uses the existing lifecycle (`mark_entering`/`mark_partial_entry`/`mark_cleanup_required`/`mark_exiting`/`mark_partial_exit`/`mark_closed`) and the existing durable store | production executor path tests | `tests/strategies/test_execution.py::ExecutorOptionRunBindingTests` (6) |
| `option_run.phase` / reference frozen by the compiler; exit without a reference refuses at plan time | unit | `backend/strategies/compiler/option_structure.py`, `tests/strategies/test_option_structure_compiler.py::OptionRunBindingFreezeTests` (4) |
| Linked hosted worker run closed with `status='closed'` + `closed_at` INSIDE the reconciliation unblock transaction; unrelated/data-only attempts unchanged | disposable PG (4 tests) | `backend/strategies/repository.py::reconcile_with_audit`, `backend/api/routers/strategies.py`, `tests/integration/test_worker_run_terminal_closure_postgres.py` |
| Internal live adapter (preparatory) with approval/authority/reservation/admission/quote gates, pre-dispatch work, async acceptance != fill, transport-uncertainty, idempotent duplicate | disposable PG with a FAKE broker boundary (12 tests) | `backend/strategies/live_adapter.py`, `tests/integration/test_live_adapter_preparation_postgres.py` |
| Roll seam: exact persisted coordinates, direction and required acquisition quantity; a plan may not carry both roll contracts | unit/production executor | `backend/strategies/execution.py::_roll_plan_contract`, `tests/strategies/test_execution.py::ExecutorRollSeamTests` |
| `target_weights` over HTTP, independent of options (pinned universe + frozen capital basis + frozen product) | production HTTP route | `backend/strategies/compiler/target_weights.py` (product frozen), `tests/api/test_strategy_owner_and_binding.py::test_a_target_weights_plan_executes_through_the_public_routes` |
| Simple hosted paper demo, now with a closed linked run as a REQUIRED axis | disposable PG + loopback API + real supervisor/child | `examples/hosted_acceptance/evidence/acceptance-20260921T174124Z-final.json` |

### Still NOT live-ready (named, not guessed)

1. **The hosted strategy registry has no live mode.**
   `backend/schema.sql:2305` `ck_hosted_strategies_execution_mode` allows only
   `('paper','dry_run')`. A live-bound hosted run cannot be created through the
   platform's own lifecycle today, so "deploy + market certification + remove
   `PAPER_ONLY_EXECUTION`" is insufficient: the strategy/job model itself must
   gain an approved live mode.
2. **Broker async outcomes are not reconciled into the adapter.** The adapter
   records acceptance as PENDING and reads confirmed fills from an injected
   ingestion source; that ingestion→adapter binding for live is not wired, and a
   broker outcome is not a synchronous fill.
3. **Live certification has not been performed** (no broker contact, no live
   market, no deployed migration). The public live plan route still refuses
   `PAPER_ONLY_EXECUTION` (covered by `tests/api/test_strategy_owner_and_binding.py`).

### Phase / migration arithmetic (final)

* Campaign phases **0..10 = ELEVEN**.
* Original phase migrations **000025..000034 = TEN**.
* Closure migrations: `000035` (plan-kind vocabulary), `000036` (roll
  replacement-fill event), `000037` (plan↔option-run binding) → head was
  `20260921_000037` at that point (superseded by `000038` below; see CURRENT
  STATUS), single head, prior-head upgrade + downgrade verified on a disposable
  database.

---

## Acceptance correction pass (2026-09-21, head `20260921_000038`)

Root review found six concrete defects; all six are fixed and re-verified.

| # | Defect | Fix | Evidence class |
| --- | --- | --- | --- |
| 1 | Live adapter kept its submission state in process memory (`_submissions`), so two instances/restarts could dispatch one plan step twice | New durable `public.live_plan_submissions` (migration `20260921_000038`): `UNIQUE (plan_id, step_no)` claim written under the book advisory lock BEFORE the network, holding `pending`/`uncertain`/`rejected`/`no_op`, the broker order ids and the delta snapshot | disposable PG (20 live-adapter tests incl. two instances, a recreated instance, a committed-claim proof block and dispatch-time authority re-reads) |
| 2 | A response with no order id was treated as a rejection (which resolved the barrier and released work) | Only an explicit authoritative refusal is `rejected`; absent/ambiguous responses are `uncertain` (recovery required), barrier work retained and never auto-repeated. Plus dispatch-time re-read of approval/reservation inside the claim transaction | disposable PG (`test_a_malformed_response_is_uncertain_and_keeps_its_work`, `test_the_authority_is_rechecked_at_the_moment_of_dispatch`) |
| 3 | Live sizing used the leg's absolute target as an unsigned quantity with a default BUY (target -10 bought 10; target 0 with current +10 sent nothing) | `target - attributed current` decides side AND quantity, floored to the pinned lot, recorded as the delta snapshot; missing position evidence refuses (`LIVE_POSITION_EVIDENCE_UNAVAILABLE`) | disposable PG (`test_the_live_delta_and_direction_come_from_the_attributed_position`: -10/flat → SELL 10, 10→5 → SELL 5, 0 with +10 → SELL 10, equal → `no_op`) |
| 4 | The plan's option run was created and bound in two commits (orphan runs under concurrency); the plan's exit sizing came from the AGGREGATE strategy book, so closing +50 with a signed -50 leg produced -100 and could touch another structure | `resolve_plan_option_run` now creates the run AND the binding in one transaction under a per-plan advisory lock (loser rolls back its run and adopts the winner). Option steps are derived from the run's OWN recorded fills: `target - run open`, exit targets flat for the run's leg, direction/contract validated, never a new exposure | disposable PG concurrency (4 threads, real `DurableOptionRunStore` → exactly one run/binding) + `ExecutorOptionRunBindingTests` (entry ignores the aggregate book; two structures sharing a contract; repeated exit is `no_op`, never a reversal) |
| 5 | Options had no actual PG/HTTP entry→exit evidence | `tests/integration/test_options_plan_route_postgres.py`: real route `POST /strategies/{id}/plans/{id}/execute`, real durable option store and real paper repository — entry → exit, partial-then-full exit, replay 409, run status/order ids/trades cross-checked against the step trail | disposable PG (2 end-to-end route tests) |
| 6 | `RollStateMachine.create` required a plan carrying BOTH contracts while the executor refuses exactly that; the required quantity was caller-supplied | `create(plan_id=...)` now validates the approved ACQUISITION plan (replacement contract only), refuses a plan carrying the old contract, and takes the required quantity FROM the plan (a lower caller value is refused). An absolute flat (`signed_quantity == 0`) close leg is treated as a close, not "the wrong way" | production route (`tests/api/test_strategy_owner_and_binding.py::test_a_roll_is_opened_by_its_approved_acquisition_plan`) |

### Head and arithmetic (updated)

* Campaign phases **0..10 = ELEVEN**; original phase migrations **000025..000034 = TEN**.
* Closure migrations: `000035`, `000036`, `000037`, `000038` → **current head
  `20260921_000038`**, single head; prior-head upgrade + downgrade verified on a
  disposable database.

### Still not live-ready (unchanged)

1. `ck_hosted_strategies_execution_mode` still allows only `paper`/`dry_run`, so a
   live-bound hosted strategy/job cannot be created by the platform itself.
2. The live fill-ingestion binding is still injected, not wired to production
   ingestion; a broker outcome is not a synchronous fill.
3. No live market certification, no deployment; the public live plan route still
   refuses `PAPER_ONLY_EXECUTION`.
4. The internal live adapter supports single-instrument plans only; compound live
   plans are named refusals. It is **preparatory plumbing, not production-ready**.

---

## Final narrow pass (2026-09-21, head `20260921_000038`)

Root review of the previous correction pass found four remaining items; all are
closed.

| # | Finding | Fix | Evidence |
| --- | --- | --- | --- |
| A1 | The live claim used its own `live-plan-claim:<plan_id>` advisory lock, and the `work_created` barrier event was recorded in a SEPARATE transaction after the claim commit; `enumerate_inflight_work` did not know about live submissions at all | The claim, the dispatch-time re-check and `barrier.record_work_event` now run in ONE transaction on the CANONICAL book lock (`ExecutionBarrier.lock_book`). A new enumeration source `_live_submission_inflight` lists `live_plan_submissions` rows in `pending`/`uncertain` for the book | disposable PG: `test_a_committed_claim_blocks_a_quiet_proof`, `test_a_pending_or_uncertain_submission_blocks_the_whole_book` (proof refuses; a second plan on the same book cannot make it look quiet) |
| A2 | `_recheck_at_dispatch` only re-checked approval/reservation while the comment claimed the authority | It now re-reads the evaluation authority from a platform reader and refuses `LIVE_AUTHORITY_EVIDENCE_UNAVAILABLE` when no reader is wired (no implied check) | disposable PG: `test_a_missing_authority_reader_refuses_at_dispatch`, `test_an_authority_that_expired_before_dispatch_refuses` |
| A3 | The attributed-current read that sizes the step ran BEFORE the book lock was taken | `_resolve_delta` now runs INSIDE the same canonical book-locked transaction as the claim, so a concurrent writer on the book cannot change the size between the read and the committed claim | disposable PG: the delta/direction cases still pass under the new ordering (`test_the_live_delta_and_direction_come_from_the_attributed_position`) |
| B | Distinct exit plans could both snapshot the same run's trades and each close it (overclose), because serialization was per PLAN | The run transition is now a compare-and-set (`DurableOptionRunStore.save_run_if_status`) taken BEFORE any submission: exactly one plan owns `entered -> exiting`; a run already `exiting` refuses (`OPTION_RUN_EXIT_IN_FLIGHT`), so a restart cannot repeat an unknown exit | disposable PG: `test_two_exit_plans_for_one_run_cannot_overclose` (deterministic: the first exit blocks inside the runtime, the second refuses; the run's legs end exactly flat with 4 fills total) |
| C | `.acceptance-workspace/` (supervisor scratch from the demo runs) was left untracked; the closure report's top summary still named the old artifact and head | The workspace was inspected (job scratch: source/logs/state/attempts), removed, and the runner now deletes it on a successful run (`workspace_cleaned`). Task databases were already dropped (no `kite_optroute_*`/`kite_live_*`/`kite_bind_*`/`kite_mig_*` remained) | `git status` no longer lists it; `examples/hosted_acceptance/run_acceptance.py` cleanup step |
| D | Only a prior-head migration probe had been reported for `000038` | From-zero migration to the final head on a unique disposable database, then dropped | `alembic upgrade head` from an empty DB reached `20260921_000038` |

**Paper readiness:** demonstrated (single-instrument hosted paper chain above).
**Live readiness: not claimed.** Remaining live limitations are exactly the four
listed under CURRENT STATUS.
