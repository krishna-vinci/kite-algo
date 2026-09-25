# Hosted options B2 completion - paper gate

Date: 2026-09-26. Branch `codex/b2-gate` at `b7c7159` (B1 `eb2cb00`..B2.6b). Authority:
`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md` (B2 exit gate), the B2 design notes, and
the source. Paper only: the gate drives the supervisor-child harness against a disposable PostgreSQL on
`127.0.0.1:15433` with a synthetic market boundary and a paper broker. No production configuration, account, real
order, notification or live gate is touched.

## What B2 delivers

B2 turns hosted options from a single continuous structure into a governed, adjustable, protected and
owner-operable one. Every commit id below was verified with `git log --oneline eb2cb00^..HEAD`.

| Slice | Commit(s) | Deliverable |
| --- | --- | --- |
| B1 (baseline) | `eb2cb00` | Keep hosted option structures continuous across evaluations; duplicate entry refused at execution (`OPTION_STRUCTURE_ALREADY_OPEN`); the option run is a continuation axis. |
| B2.1a | `83567dc` | Admission-time structure gate: before the owner is asked, and again at admission, refuse an equivalent open structure (`OPTION_STRUCTURE_ALREADY_OPEN`) or any non-finished own run (`OPTION_STRUCTURE_UNRESOLVED`); the named status is exposed in the proposal/plan view. The B1 execution check stays as defence in depth. |
| B2.1b | `22766f8` | Governed repair for `partial_entry` / `partial_exit` / `cleanup_required`: residual computed from the run's own confirmed fills, closed risk-reducing (shorts first) or dispositioned with evidence; ambiguous runs refuse `OPTION_RUN_REPAIR_AMBIGUOUS`. Operator route + repository, no new execution engine. |
| B2.2 | `4c04079` | S1 compile freeze: `target_option_structure` freezes an immutable full desired state (`phase: adjust`, `structure_units`, `based_on_generation`, `protection_policy`, `max_loss`). |
| B2.2 | `199bbfb` | S2 adjust engine: one run, a versioned leg set with `structure_generation` (migration `20260925_000045` widening the phase CHECK); each order derived as `signed(target) - the run's own confirmed open`. |
| B2.2 | `8b5cf33` | S3 adjust gates + admission: reductions first, hedges before dependent shorts under the fill gate, `OPTION_ADJUSTMENT_WOULD_UNHEDGE`, `OPTION_ADJUSTMENT_PROTECTION_ACTIVE`, `OPTION_RUN_ADJUST_IN_FLIGHT`; `assess_option_adjust_admissibility` asked before approval. |
| B2.2 | `59a10d5` | S4 expiry roll inside the owned run: acquire and prove the new generation hedge-first, then release the old generation short-first (`OPTION_ADJUSTMENT_ROLL_INCOMPLETE` until proven); a finished in-flight adjust may be superseded; live refuses adjust with `LIVE_OPTION_ADJUST_UNSUPPORTED`. |
| B2.2 | `847fdb5` | S5 example + harness: `options_dynamic_straddle.py` (+ schema), the `options_dynamic_resize_roll` scenario, the dynamic acceptance assertions, and two platform gaps the gate found (roll financing valuation; owned-work identity after a generation advance). |
| B2.2 hardening | `a279059`, `bd5b0b0` | First independent review's 4 findings: takeover/crash/generation races in adjustments, plus one request-fixture schema correction. |
| B2.5 | `0e02f39`, `6dc3fd3` | Per-version risk policy enforced at admission (family, expiry policy, naked permission, max loss, margin), `effective = min(declared, operator, ceiling)`; `STRATEGY_RISK_POLICY_MISSING` for a version without a policy; follow-up values released roll legs for notional limits. |
| B2.4 | `df5d29d` | S1 durable protection-owner row per option run (migration `20260925_000047`). |
| B2.4 | `d32d1d1` | S2a protection loop + safety gate: option exposure gated on a known protection owner. |
| B2.4 | `c270a70` | S2b+S4 owner gates + policy version: unknown/conflicting owner blocks new exposure but never blocks risk-reducing actions. |
| B2.4 | `965ca05` | S3 handover: protection ownership handed to the continuing run across evaluations. |
| B2.4 | `724e1c6` | Second independent review's 3 findings: close the protection-ownership gaps (takeover, crash, generation). |
| B2.6a | `4679956` | Options operations view + repair UI (`frontend-next`): structure/leg state, repair actions, protection owner, pending adjustments, approvals, refusal reasons. |
| B2.6b | `d917d8f`, `681c9a5` | S1 owner cancel-pending (entry work only) + dead-submission disposition. |
| B2.6b | `f092ffe` | S2 owner exit for one option structure (governed exit plan). |
| B2.6b | `b7c7159` | S3 resumable owner flatten (one operation per strategy scope, migration `20260925_000050`) + protection-owner display. |

Design notes accepted along the way (not code): B2.2 `e5664b3`, B2.4 `4976679`, B2.6b `2dc0143`.

## Named refusal vocabulary added in B2

Codes introduced or newly enforced by B2 (`rg` over `backend/`), grouped by enforcement stage.

| Code | Meaning | Where enforced |
| --- | --- | --- |
| `OPTION_STRUCTURE_ALREADY_OPEN` | This strategy already owns an equivalent open structure. | request creation `backend/options/execution/plan_binding.py:703`; execution `backend/strategies/execution.py` |
| `OPTION_STRUCTURE_UNRESOLVED` | This strategy owns a run in a non-finished status. | `plan_binding.py:731`, `:1444` |
| `OPTION_STRUCTURE_DISCOVERY_UNKNOWN` | The owned-run read is incomplete. | `plan_binding.py:408`, `:421` |
| `OPTION_RUN_IDENTITY_UNKNOWN` | A held run cannot be compared to the plan. | `plan_binding.py:718` |
| `OPTION_RUN_STATE_CHANGED` | The referenced run is not `entered` (or `adjusting` owned by this plan). | `plan_binding.py:1356` |
| `OPTION_RUN_ADJUST_IN_FLIGHT` | Another plan's adjust is not provably finished. | `plan_binding.py:1338`; `execution.py:1976` |
| `OPTION_ADJUSTMENT_STALE_BASIS` | The held generation is not the generation the plan froze. | `plan_binding.py:1375` |
| `OPTION_ADJUSTMENT_RUN_NOT_OWNED` / `OPTION_ADJUSTMENT_SCOPE_MISMATCH` | The referenced run is not this strategy's, in this account and environment. | `plan_binding.py:1274`, `:1636` |
| `OPTION_ADJUSTMENT_WOULD_UNHEDGE` | The frozen target leaves a short with less protective long coverage. | `plan_binding.py:1251` |
| `OPTION_PROTECTIVE_EXIT_UNRESOLVED` | The run's own records still own an unresolved protective stage. | `plan_binding.py:1419` |
| `OPTION_ADJUSTMENT_REFERENCE_REQUIRED` / `OPTION_ADJUSTMENT_BASIS_REQUIRED` | An adjust must name its run and the generation it observed. | compile `backend/strategies/compiler/option_structure.py:314`, `:326` |
| `OPTION_ADJUSTMENT_LEG_MISMATCH` / `OPTION_ADJUSTMENT_UNSUPPORTED` | The target is not one structure (reversal, mixed expiries, underlying change). | `plan_binding.py:1669`; `execution.py:1660` |
| `OPTION_ADJUSTMENT_PROTECTION_ACTIVE` | An increase while protection is triggered or unreadable (reductions stay admissible). | execution `execution.py:1787` |
| `OPTION_ADJUSTMENT_ROLL_INCOMPLETE` | A roll's old generation is withheld until the new one is proven from the run's own fills. | `execution.py:467`, `:2278` |
| `OPTION_HEDGE_NOT_FILLED` / `OPTION_HEDGE_RELEASE_WITHHELD` | A dependent short is released only against a confirmed hedge fill. | `execution.py:616`, `:699` |
| `OPTION_STRUCTURE_FAMILY_NOT_ALLOWED` | The frozen structure family is not in the version's declared risk policy. | admission `backend/strategies/admission.py:587` |
| `OPTION_EXPIRY_POLICY_NOT_ALLOWED` | The expiry policy is not in the declared risk policy. | `admission.py:598` |
| `OPTION_NAKED_NOT_PERMITTED` | The structure is naked and the version declines naked exposure. | `admission.py:620`, `:654` |
| `OPTION_MAX_LOSS_EXCEEDED` | The frozen max loss exceeds the version's declared limit. | `admission.py:668` |
| `STRATEGY_RISK_POLICY_MISSING` | A version without a declared policy may not enter options exposure. | `admission.py:509` |
| `OPTION_CHAIN_SNAPSHOT_STALE` / `OPTION_GREEKS_STALE` | The option chain snapshot or Greeks evidence is too old to freeze or size a structure. | `backend/options/market/freshness.py:114`, `:171`, `:225`, `:260` |
| `OPTION_RUN_REPAIR_AMBIGUOUS`, `_STATE_CHANGED`, `_EVIDENCE_CHANGED`, `_ACTION_MISMATCH`, `OPTION_RUN_NOT_REPAIRABLE` | The repair route's own refusals; an ambiguous run is never auto-adopted. | repair `backend/options/execution/repair.py:83`-`88` |
| `OPTION_PROTECTION_OWNER_UNKNOWN` / `OPTION_PROTECTION_OWNER_REQUIRED` / `OPTION_PROTECTION_OWNER_CONFLICT` | Protection owner is unreadable, required, or conflicting; blocks new exposure, never a reduction. | `backend/options/protection/ownership.py:112`-`114` |
| `OPTION_OWNER_EXIT_BOUNDARY_UNAVAILABLE` / `OPTION_OWNER_EXIT_STAGE_NOT_SUBMITTED` / `OPTION_OWNER_EXIT_LIVE_UNAVAILABLE` | The owner exit route cannot prove the boundary, the stage, or the live lane. | `backend/api/services/owner_actions.py:2851`, `:2835`; `backend/api/services/option_run_repair.py:429` |
| `FLATTEN_LIVE_NONOPTION_UNSUPPORTED` | Live non-option flatten fails closed until C1's governed live reduction lane exists. | `owner_actions.py:2939` |
| `LIVE_OPTION_ADJUST_UNSUPPORTED` | The live lane refuses an adjust plan (C1.2 owns it). | `backend/strategies/live_sequence.py:782` |
| `LIVE_OPTION_MARGIN_EVIDENCE_UNAVAILABLE` / `_STALE` / `_SCOPE_MISMATCH`, `LIVE_OPTION_ROLL_PEAK_UNAVAILABLE` | Live option margin evidence is missing/stale/out of scope, or a roll's peak margin cannot be proven. | `admission.py:85`-`86`; `backend/strategies/plan_pipeline.py:302`, `:324` |
| `HOSTED_JOB_NOT_BLOCKED` | Not a failure: a healthy continuation cleared its own block, so the operator route has nothing to reconcile. | `backend/strategies/reconciliation.py:70` |

## Gate evidence

One invocation, all five scenarios, one evidence file, `"ok": true`:

```
timeout 3600 /home/krishna/kite-algo/.venv/bin/python examples/hosted_platform/run_phase5_acceptance.py \
  --only options_restart_hold_close,options_adjustment,options_dynamic_resize_roll,options_protection_handover,momentum_recurring_sequence
-> examples/hosted_platform/evidence/phase5-20260925T183422Z.json  (ok=true, no FAIL lines)
```

Evidence file: `examples/hosted_platform/evidence/phase5-20260925T183422Z.json`. The harness records the SHA-256 of
the driver and the momentum example/schema it drove, so the evidence is bound to this revision. Every scenario:

| Scenario | `acceptance.ok` | What it proves (platform facts) |
| --- | --- | --- |
| `options_restart_hold_close` | `true` | One durable run survives a restart between entry and close. Job 1 enters and holds; job 2 is a fresh child that finds the same run and submits no duplicate (`OPTION_STRUCTURE_ALREADY_OPEN`, 1 refusal), then closes it. 3 evaluations, 1 option run, final run `exited`. |
| `options_adjustment` | `true` | The same strategy discovers its run from `owned_work()["option_runs"]` and submits one governed CLOSE; a repeat observation adds no third request. 2 requests created / 2 executed (`executed, executed`), 4 submitted steps, 1 run closed, all four settlement axes `satisfied` (`quiescence`, `attribution_scoped_flatness`, `terminal_domain_state`, `no_live_evaluation_authority`), child exit code 0, 0 orders before approval, reconciliation `HOSTED_JOB_NOT_BLOCKED`. |
| `options_dynamic_resize_roll` | `true` | The delta-neutral straddle manages ONE run across evaluations: edge phases `entry, adjust, adjust, exit`; generations `1 -> 2 -> 3, 3`; final run `exited` with all 4 legs on the rolled expiry `2026-11-26` and every leg flat; leg units 2/2/2/2. ONE `OPTION_STRUCTURE_ALREADY_OPEN` refusal (duplicate entry probe) and ONE `OPTION_ADJUSTMENT_STALE_BASIS` refusal, both at `stage=request` with no owner decision - refused before the owner was asked. |
| `options_protection_handover` | `true` | Protection ownership is durable and moves with the work. Owner epoch 1 = entry run (`claimed`); on the continuing close job, `transferred` to epoch 2, then `released` at epoch 3 when the run reaches terminal. The worker's own resolution probe returns `reason: ok` from `source: protection_owner` in both runs. 2 requests, 1 run, final `exited`. |
| `momentum_recurring_sequence` | `true` | Non-option regression: 4 evaluations ran (`entry, noop, rebalance, exit`), 10 orders, exactly one request per job, and the other strategy's `BYSTANDER` holding is untouched before and after. |

## Independent reviews

Two independent reviews of B2 code found real defects; both sets are fixed and committed, and this gate re-runs the
whole options path afterwards.

- B2.2 hardening `a279059` (+ `bd5b0b0`): 4 findings - takeover, crash and generation races in adjustments, plus a
  request-fixture schema correction.
- B2.4 hardening `724e1c6`: 3 findings - protection-ownership gaps on takeover, crash and generation advance.

## Known limitations

- Live adjust/roll, live option LIMIT orders and live approval binding (plan + exposure snapshot + catalog +
  reservation + version/generation/policy) are **C1.2**, in progress; the live lane still refuses an adjust with
  `LIVE_OPTION_ADJUST_UNSUPPORTED`.
- B2.6b live **non-option** flatten refuses `FLATTEN_LIVE_NONOPTION_UNSUPPORTED` until C1's governed live reduction
  lane exists. Paper flatten and live option exit are wired.
- Two pre-existing failures in `tests/sdk/test_worker_protection_runtime.py` (structure-seam tests that fail at
  `ebc98ac`, before B1) are unrelated to B2 and not fixed here.
- Pre-existing phase2b futures-roll/capacity failures are unrelated to B2 and not fixed here.
- Paper only: the market boundary (quotes, candles, one option chain) and the broker are simulated. Nothing here
  opens a live gate or places a real order.
