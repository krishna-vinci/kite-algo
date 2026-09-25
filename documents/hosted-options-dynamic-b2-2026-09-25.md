# Hosted options: B2.2 S5 + B2.3 - dynamic option example and phase gate

Date: 2026-09-25. Baseline: branch `codex/b2-2-s5` at `a1ff0d1` (B2.1 + B2.2 S1-S4).
Authority: `documents/hosted-options-b2-2-design-2026-09-25.md` section 8 S5,
`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md` B2.2/B2.3.
Scope: paper only. Nothing here places a real-money order, and no live lane changed.

## What B2.1-B2.3 deliver

| Slice | Commit | What it adds |
| --- | --- | --- |
| B2.1a | `83567dc` | The structure gate runs before the owner is asked: an equivalent open structure, or any unresolved run of this strategy, refuses `OPTION_STRUCTURE_ALREADY_OPEN` / `OPTION_STRUCTURE_UNRESOLVED` at request creation, admission and execution. |
| B2.1b | `22766f8` | Governed repair for `partial_entry` / `partial_exit` / `cleanup_required`: the residual is computed from the run's own confirmed fills and closed risk-reducing (shorts first) or dispositioned with evidence; ambiguous runs refuse `OPTION_RUN_REPAIR_AMBIGUOUS`. |
| B2.2 S1 | `4c04079` | `target_option_structure` freezes a full desired state (`phase: adjust`, `structure_units`, `based_on_generation`, `protection_policy`, `max_loss`); `OPTION_ADJUSTMENT_REFERENCE_REQUIRED` / `OPTION_ADJUSTMENT_BASIS_REQUIRED`. |
| B2.2 S2 | `199bbfb` | The adjust engine: one run, a versioned leg set, `structure_generation`, migration `20260925_000045` widening the phase CHECK, and each order derived as `signed(target) - the run's own confirmed open`. |
| B2.2 S3 | `8b5cf33` | Sequencing gates and admission: reductions first, hedges before dependent shorts under the fill gate, `OPTION_ADJUSTMENT_WOULD_UNHEDGE`, `OPTION_ADJUSTMENT_PROTECTION_ACTIVE`, `OPTION_RUN_ADJUST_IN_FLIGHT`, and `assess_option_adjust_admissibility` asked before approval. |
| B2.2 S4 | `59a10d5` | Expiry roll inside the owned run: acquire and prove the new generation hedge-first, then release the old generation short-first (`OPTION_ADJUSTMENT_ROLL_INCOMPLETE` until proven); a finished in-flight adjust can be superseded; live refuses adjust with `LIVE_OPTION_ADJUST_UNSUPPORTED`. |
| B2.2 S5 + B2.3 | this slice | Example 3 (`options_dynamic_straddle.py` + schema), the `options_dynamic_resize_roll` harness scenario, the dynamic acceptance assertions, and two platform gaps the gate found. |

### Example 3: `examples/hosted_platform/options_dynamic_straddle.py`

A delta-neutral short straddle with protective wings (short ATM call + short ATM put, each covered by an
out-of-the-money long wing), managed across evaluations. Each evaluation reads the chain/Greeks through the
platform's data access, reads its own run from `owned_work()["option_runs"]`, and submits ONE desired state:
`entry` at `base_units`; `adjust` (resize) when |net delta| >= `resize_delta_threshold` or the harness forces it;
`adjust` (roll) when inside `roll_days_to_expiry` or `roll_to_expiry` names the next expiry, with the same legs
and roles; `exit` when `exit_position`. Every adjustment freezes the generation of the SAME read that produced
the decision, so a run that moved is refused rather than silently re-derived. All parameters are declared and
bounded in the schema; the two probe parameters (`duplicate_entry_probe`, `stale_basis_probe`) only ask a
question and place no order.

## Refusal vocabulary (paper)

| Stage | Code | Meaning |
| --- | --- | --- |
| request creation | `OPTION_STRUCTURE_ALREADY_OPEN` | This strategy already owns an equivalent open structure. |
| request creation | `OPTION_STRUCTURE_UNRESOLVED` | This strategy owns a run in a non-finished status. |
| request creation | `OPTION_STRUCTURE_DISCOVERY_UNKNOWN` / `OPTION_RUN_IDENTITY_UNKNOWN` | The owned-run read is incomplete, or a held run cannot be compared. |
| request creation | `OPTION_ADJUSTMENT_STALE_BASIS` | The run's held generation is not the generation the plan froze. |
| request creation | `OPTION_RUN_STATE_CHANGED` | The referenced run is not `entered` (or `adjusting` owned by this plan). |
| request creation | `OPTION_RUN_ADJUST_IN_FLIGHT` | Another plan's adjust is not provably finished. |
| request creation | `OPTION_ADJUSTMENT_RUN_NOT_OWNED` / `OPTION_ADJUSTMENT_SCOPE_MISMATCH` | The referenced run is not this strategy's, in this account and environment. |
| request creation | `OPTION_ADJUSTMENT_WOULD_UNHEDGE` | The frozen target leaves a short with less protective long coverage. |
| compile | `OPTION_ADJUSTMENT_REFERENCE_REQUIRED` / `OPTION_ADJUSTMENT_BASIS_REQUIRED` | An adjust must name its run and the generation it observed. |
| compile | `OPTION_ADJUSTMENT_LEG_MISMATCH` / `OPTION_ADJUSTMENT_UNSUPPORTED` | The target is not one structure: a reversal on one contract, mixed expiries, or an underlying change. |
| execution | `OPTION_ADJUSTMENT_PROTECTION_ACTIVE` | An increase while protection is triggered or unreadable (reductions stay admissible). |
| execution | `OPTION_ADJUSTMENT_ROLL_INCOMPLETE` | A roll's old generation is withheld until the new one is proven from the run's own fills. |
| execution | `OPTION_HEDGE_NOT_FILLED` / `OPTION_HEDGE_RELEASE_WITHHELD` | A dependent short is released only against a confirmed hedge fill. |
| execution | `OPTION_PROTECTIVE_EXIT_UNRESOLVED` | The run's own records still own an unresolved protective stage. |
| admission | `POSITION_VALUATION_UNAVAILABLE` | A configured notional limit with an unvalued coordinate in the post-plan book. |
| repair | `OPTION_RUN_REPAIR_AMBIGUOUS`, `OPTION_RUN_REPAIR_STATE_CHANGED`, `OPTION_RUN_REPAIR_EVIDENCE_CHANGED`, `OPTION_RUN_REPAIR_ACTION_MISMATCH`, `OPTION_RUN_NOT_REPAIRABLE` | The repair route's own refusals. |
| live | `LIVE_OPTION_ADJUST_UNSUPPORTED` | The live lane refuses an adjust plan (C1.2 owns it). |
| continuation | `HOSTED_JOB_NOT_BLOCKED` | Not a failure: a healthy completion cleared its own block, so the operator route has nothing to reconcile. |

## Evidence

`examples/hosted_platform/evidence/phase5-20260925T122615Z.json` - one run, four scenarios, `"ok": true`:

| Scenario | `ok` |
| --- | --- |
| `options_dynamic_resize_roll` | `true` |
| `options_adjustment` | `true` |
| `options_restart_hold_close` | `true` |
| `momentum_recurring_sequence` | `true` |

`options_dynamic_resize_roll` asserts, from the platform's own rows: exactly ONE option run; edge phases
`entry, adjust, adjust, exit`; generations `1 -> 2 -> 3`; the run back to `entered` after each adjustment; leg
sizes 1/2/2 units; the held legs on the rolled expiry with the released generation flat in the run's own trade
ledger; one `OPTION_STRUCTURE_ALREADY_OPEN` refusal (a duplicate entry probe at generation 2); and one
`OPTION_ADJUSTMENT_STALE_BASIS` refusal with `status=refused`, no owner decision and refusal stage `request` -
that is, refused BEFORE the owner was asked. Final run status `exited`.

`options_adjustment` now asserts the continuation proof instead of operator reconciliation: the scenario
requires `expects_self_cleared_block`, the operator route answers `HOSTED_JOB_NOT_BLOCKED`, and every other
assertion (two executed requests, one close, the four settlement axes) is unchanged.

## Commands and results

Harness (phase gate, one invocation, all four scenarios):

```
timeout 2400 /home/krishna/kite-algo/.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py \
  --only options_dynamic_resize_roll,options_adjustment,options_restart_hold_close,momentum_recurring_sequence
-> evidence written; ok=true; 4/4 scenarios ok=true (no FAIL lines)
```

Unit and integration:

```
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_hosted_option_example.py \
    tests/strategies/test_hosted_harness_assertions.py -q
-> 72 passed
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_admission.py \
    tests/strategies/test_reservations.py tests/strategies/test_execution_snapshot_option_runs.py -q
-> 61 passed
HOSTED_EXECUTION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
  /home/krishna/kite-algo/.venv/bin/python -m pytest \
  tests/integration/test_owned_work_option_runs_postgres.py -q
-> 11 passed
```

## Two platform gaps this gate found (and fixed here)

Both were invisible to the engine-level S2-S4 tests and appear only on the real supervisor-child path.

1. **A roll was refused at admission** (`POSITION_VALUATION_UNAVAILABLE`, `backend/strategies/financing.py`).
   The exposure builder treated every coordinate the plan does not name as held unchanged, so the roll's
   released old-generation legs survived into the post-plan book with no price. An option ADJUST's frozen
   desired state IS the post-plan book: an omitted held coordinate is now released (target flat), which is what
   the executor does. Conservative funding is unchanged: the new generation's increase is not netted against
   the release.
2. **The owned-work read went unknown after any generation-advancing adjustment**
   (`option_run_identity_mismatch`, `backend/strategies/execution_snapshot.py`). The identity check compared the
   run's current legs against the ORIGINATING plan's frozen legs only, which a resize or a roll makes obsolete by
   design - so a strategy could not read the run it had just adjusted. The check now asks whether ANY edge
   binding the run recognizes its current legs (still fail-closed when none does), and the reported `expiry` is
   the one the run holds now, not the one the entry froze.

## Known limitations

* **Live refuses adjust until C1.2.** `LIVE_OPTION_ADJUST_UNSUPPORTED` is the live lane's answer for the whole
  adjust phase; C1.2 owns broker margin/funds evidence per increase, stale chain/Greeks refusal, the B2.5
  max-loss/notional checks, protection-owner continuity (B2.4), and the peak-margin precheck for a roll's
  overlap window.
* **The dead-submission wedge still needs an operator disposition (B2.6).** An adjust plan whose process died
  with a `submitted` event that has no outcome keeps its run `adjusting`; takeover and repair both refuse by
  name (`OPTION_RUN_ADJUST_IN_FLIGHT`). This scenario exercises the healthy path only.
* **A roll's overlap needs real margin.** The harness funds this scenario's paper account explicitly
  (`paper_starting_balance`) because the paper broker charges margin on both generations during the overlap and
  the harness quotes every option leg at its synthetic flat price. That is a harness fixture, not a platform
  guarantee; C1.2/B2.5 own the live version of it.
* **Carried-over pre-existing issues** (plan doc, "Known pre-existing issues"): one hosted execution API test
  stalls in the dirty checkout but passes from a clean worktree.
