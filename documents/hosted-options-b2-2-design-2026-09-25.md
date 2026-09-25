# B2.2 design — desired-state option proposals (adjust phase)

Designer pass, read-only. Baseline: `/home/krishna/kite-algo` @ `83567dc` (branch `development`,
migration head `20260924_000043`).
Authority: plan B2.2 (`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:62-77`),
R3 §14 E/G/H (`documents/hosted-strategies-architecture-r3.md:276-311`).

## 0. Verified baseline

- The compiler freezes `entry`/`exit` only: `_option_run_binding` rejects every other phase
  (`backend/strategies/compiler/option_structure.py:178`, `:196`) and requires a run reference for `exit`
  (`:199-208`).
- Leg quantity is `lot_size * ratio` (`:276`). `_structure_digest` covers underlying, expiry, leg contract,
  side, ratio and product, and **not** quantity (`:444-471`).
- The run vocabulary is `OptionRunStatus` (`backend/options/execution/models.py:12-25`); a run holds
  `legs`/`trades`/`metadata` (`:125-137`). `entered` today leads only to exit preview / partial exit
  (`backend/options/execution/lifecycle.py:24-28`), and status writes are CAS-guarded
  (`backend/options/execution/durable_store.py:364`).
- The plan→run edge freezes the phase and validates the exit reference (`plan_binding.py:144`, `:253`,
  `:573`, `:629`); the DB CHECK admits only `entry`/`exit` (`backend/schema.sql:2864`).
- B2.1a's rule is ONE function (`plan_binding.py:340`) called from request creation, approval, admission and
  execution (`backend/strategies/plan_pipeline.py:220`, `:229`).
- The paper executor sizes ENTRY as `target - own_confirmed` and EXIT as FLAT
  (`backend/strategies/execution.py:1046`, `:1085-1105`), takes the transition by CAS (`:1214`), and refuses an
  exit that would open exposure (`:1166`).
- Hedge gating: submission is not fill, with a proportional, floored ceiling
  (`backend/options/protection/hedge_gate.py:39`, `:82`, `:155-156`), used by paper (`execution.py:520-535`) and
  live (`live_service.py:837`).
- Exit sequencing is shorts-first with proven release (`exit_builder.py:50`; `staged_exit.py:700`, `:778`), and an
  unresolved protective stage blocks any other exit (`staged_exit.py:89`; `execution.py:1264`).
- The live option lane already expresses both dependency rules, but only for `entry`/`exit`
  (`live_sequence.py:711`, `:774`, `:780`).
- Roll precedent: acquire → prove → release, durable per roll, proof read from the strategy book
  (`backend/strategies/rolls.py:41`, `:141`, `:323`, `:569`, `:648`).
- Continuation counts only a clean `entered` run as `held`; anything else is `outstanding` and blocks
  (`continuation.py:710-728`, `:421`). The option-run snapshot exposes `status`, `structure_digest`,
  `protective_exit_unresolved` (`execution_snapshot.py:697`, `:710`) and fails closed on truncation (`:623`).
- A frozen plan is a pure function of payload + pin (`backend/strategies/proposals.py:235`): no DB-derived
  baseline can live in `resolved_plan`.

## 1. Payload contract

Extends `target_option_structure`. Every new key is optional, so today's entry and exit payloads compile
byte-identically.

```json
{
  "target_kind": "option_structure",
  "phase": "adjust",
  "option_run_id": "<durable option run>",
  "based_on_generation": 1,
  "underlying": "NIFTY",
  "expiry": "2026-10-29",
  "product": "NRML",
  "structure_id": "iron_condor_2026_10",
  "structure_units": 2,
  "expiry_policy": "exit_before_cutoff",
  "protection_policy": {"kind": "combined_premium_stop", "stop_points": 40, "naked": false},
  "max_loss": {"basis": "worst_case_at_expiry", "max_loss_inr": 25000},
  "legs": [
    {"instrument_token": 111, "side": "BUY",  "ratio": 1, "role": "hedge"},
    {"instrument_token": 222, "side": "SELL", "ratio": 1, "role": "short"}
  ]
}
```

- `phase` gains `adjust` (`:196`), and `adjust` requires `option_run_id` exactly as `exit` does today
  (`:199-208`) → new refusal `OPTION_ADJUSTMENT_REFERENCE_REQUIRED`.
- `structure_units`: positive whole number, default `1`; effective leg quantity is `lot_size * ratio *
  structure_units` (`:276`). Non-integral or non-positive → `PAYLOAD_INVALID` (same style as `_as_ratio`,
  `:39-51`).
- `based_on_generation`: the run generation the strategy observed (from the option-run snapshot, §6).
  Required for `adjust` → `OPTION_ADJUSTMENT_BASIS_REQUIRED` when absent.
- `protection_policy` / `max_loss`: frozen into the run's `protection` block, which
  `create_run_from_frozen_plan` already writes (`plan_binding.py:553-562`). B2.2 freezes and surfaces them;
  **enforcement of `max_loss` is B2.5**.
- An adjust cannot express "desired empty": the compiler already requires at least one leg (`:105-109`), so a
  full exit stays `phase: "exit"` and keeps the existing staged close. `desired_legs: []` → `PAYLOAD_INVALID`.
- Per-leg `role` (`hedge` / `short` / `naked`) is accepted and frozen for the naked gate in §4; `side` stays the
  authority.

## 2. Delta computation and freeze

Compile freezes the **target**; execution derives the **delta**.

- Compile resolves each desired leg against the pin exactly as today (`:98-160`), computes `structure_digest`
  (§6) and each leg's `desired_quantity`, and freezes `phase`, `option_run_id`, `based_on_generation`,
  `structure_units`, `expiry_policy`, `protection_policy`, `max_loss` into `resolved_plan`.
- Execution adds an `adjust` branch to `_option_run_steps` (`execution.py:1046`): `target =
  signed(desired_quantity)`, `current` = the run's own confirmed open for that leg identity
  (`_option_run_open_by_leg`, `:1034`; `staged_exit.own_open_by_leg`, `:609`), `delta = target - current`.
  Legs the run does not hold get a new run leg id (`{plan_id}:{index}`) appended to `run.legs`;
  `quantity = _floor_to_lot(delta, lot)` (`:2009`).
- Effective phase mapping (the derived phase is *not* frozen; the target is):
  - no `option_run_id` → `entry`, existing branch (`:1074-1093`); if anything equivalent is already owned,
    B2.1a refuses (`plan_binding.py:340`).
  - `option_run_id` declared → `adjust`, delta branch. All-flat own fills simply make every delta an increase,
    which the hedge gate still governs.
  - `desired_legs: []` → refused at compile; a real exit is `phase: "exit"`.
- Admission and preview run the same derivation for display and refusal (`plan_pipeline.py:220`); the
  **frozen target, not the previewed delta, is the approved artifact**.
- Drift between freeze and execution:
  - Run `structure_generation` ≠ `based_on_generation` → **refuse** `OPTION_ADJUSTMENT_STALE_BASIS`. Never
    recompute against a newer basis.
  - Same generation, own-fill quantities moved (a pending fill landed): the delta is re-derived, the target is
    unchanged, so the result only moves *toward* the approved target. Increases additionally re-run the
    freshness/funds recheck that already guards exposure-increasing steps (`execution.py:471-500`). No previewed
    delta is ever replayed from the plan.
  - A reversal on one contract (`target` and `current` non-zero, opposite signs) → `OPTION_ADJUSTMENT_UNSUPPORTED`.
    Reduce-to-zero then re-open is a re-entry and needs its own plan.

## 3. Engine model for adjust

**Decision: mutate the existing `OptionRunState` through a versioned leg set.** Not linked runs.

- The run is already the sole owner of the structure's own-fill ledger: paper sizing and staged-exit proof both
  read `run.trades` keyed by `leg_id` (`execution.py:1034`; `staged_exit.py:609`). Linked runs would split that
  ledger and make "the run's own confirmed fills" ambiguous.
- B1/B2.1a refuse any additional owned run and any unresolved status (`plan_binding.py:66-73`, `:340`), so every
  adjustment modelled as a new run would look like a second structure.
- Protection ownership and settlement evidence are keyed by `option_run_id` (`schema.sql:2855-2869`); splitting
  runs splits protection ownership (B2.4) and settlement proof.
- One CAS-guarded status is the existing ownership token (`durable_store.py:364`), and it is exactly what must
  serialize two concurrent adjustments.

Statuses and transitions:

- `OptionRunStatus.ADJUSTING = "adjusting"` joins `models.py:12-25`; `mark_adjusting` / `mark_adjusted` join the
  helper pattern at `lifecycle.py:41-97`; `_ALLOWED_TRANSITIONS` gains:

  `entered → adjusting`, `adjusting → entered`, `adjusting → cleanup_required`, `adjusting → adjusting`.

- `_begin_option_run` (`execution.py:1214`) gains an `adjust` branch: CAS from `entered`, or from `adjusting`
  when this plan's own binding already owns it; another plan holding `adjusting` →
  `OPTION_RUN_ADJUST_IN_FLIGHT`; an unresolved protective stage → existing
  `OPTION_PROTECTIVE_EXIT_UNRESOLVED` (`:1264`).
- `_settle_option_run` (`:1331`) writes the same orders/trades and, on success, rewrites `run.legs` to the
  desired generation, sets `structure_digest`, and increments `structure_generation`.
- Vocabulary sites that must learn `adjusting`: `plan_binding.py:60-73` (unresolved set), `continuation.py:710-728`
  (an in-flight adjust is `outstanding`, so continuation blocks until it lands, then `entered` is `held` again),
  `live_lane_ledger.py:41-45`, `is_option_entry_plan` (`plan_binding.py:323`), and the B2.6 UI vocabulary.
- Admissibility: `is_option_entry_plan` stays False for `adjust`, so the entry gate never applies to it. A new
  `assess_option_adjust_admissibility` beside `assess_option_entry_admissibility` (`plan_binding.py:340`) reuses
  the same discovery and `_same_structure` (`:302`) and requires: run owned by this (strategy, account,
  environment); an entry binding exists (`:700-706`); status `entered` (or `adjusting` owned by this plan);
  generation == `based_on_generation`; no unresolved protective stage. Because a run in `adjusting` is an
  unresolved status, a concurrent ENTRY is still refused `OPTION_STRUCTURE_UNRESOLVED` by B2.1a unchanged —
  which is the "adjust on its own entered run is admissible while other gates still hold" rule.

Schema delta — one migration (`20260924_000043` → new head):

- widen `ck_plan_option_run_phase` to `('entry','exit','adjust')` (`schema.sql:2864`);
- no new table and no new `option_run_states` column: `structure_generation` and the per-generation leg snapshots
  live in `option_run_states.metadata` JSONB (`schema.sql:1219`), which the store already round-trips
  (`durable_store.py:312-360`);
- optionally surface `structure_generation` on the snapshot row (`execution_snapshot.py:~697`).

## 4. Sequencing (within one adjust)

Order derived from the frozen legs and the run's own fills:

1. **Reductions first** — any leg with `|target| < |current|`, plus every leg removed by omission (`target = 0`).
   Within reductions, shorts first, then hedges released only by proven short closure: reuse
   `build_structure_exit_orders` / `StagedStructureExit.plan_exit` (`exit_builder.py:50`; `staged_exit.py:778`).
   A hedge release is withheld (`OPTION_HEDGE_RELEASE_WITHHELD`, `execution.py:87`) until its short is proven
   closed (`staged_exit.py:700`).
2. **Increases second**, hedges (BUY) before shorts (SELL). Each dependent short increase is gated on the
   *confirmed* hedge fill with the proportional, floored ceiling (`hedge_gate.py:82-171`), released in full only
   at `filled >= required`.
3. **Naked gate** — an adjust that leaves a held short with less protective long coverage than the frozen policy
   requires is refused `OPTION_ADJUSTMENT_WOULD_UNHEDGE`, unless the frozen desired state declares
   `protection_policy.naked: true` (R3 §14 D and walkthrough 4). Accidental transient naked exposure is never a
   strategy outcome.

Partial fill / reject / timeout:

- Partial hedge fill → the dependent short increase is released at most proportionally and withheld for the
  remainder (same generation, retryable).
- Rejected/cancelled/timed-out required increase → **nothing** of its dependents is released
  (`hedge_gate.py:39`, `:104-118`); the leg is recorded failed → `cleanup_required` (the failed-leg rule at
  `lifecycle.py:100-124`).
- A failed reduction → `cleanup_required`, never `entered`.
- Every adjust leg is sized from the run's own fills, so a retry cannot double-fill.

## 5. Roll expiry

A desired state whose legs name a different expiry (or a different contract for an existing role) is a **roll**,
executed inside the same run as a two-stage gated sequence:

1. open the new generation, hedges before shorts under the existing hedge gate;
2. **prove** the new structure's required quantities from the run's own confirmed fills;
3. release the old generation — shorts first, proven, then hedges (`staged_exit.plan_exit`);
4. only then rewrite `run.legs`, bump `structure_generation`, and return to `entered`.

Reuse vs mirror: **mirror `rolls.py`'s invariant, do not reuse its storage.** `RollStateMachine` keys one durable
roll on one old/new instrument pair with its own uniqueness rule (`rolls.py:323-344`) and proves from the G1
attribution projection (`:141`); an option adjustment is multi-leg and must be proven from the run's own trades
(`staged_exit.py:609`). Copy the acquire→prove→release ordering, the refusal vocabulary and "partial never
releases"; do not reuse `strategy_rolls`.

Refusal codes: `OPTION_ADJUSTMENT_ROLL_INCOMPLETE` (old legs held, new not proven), `OPTION_ADJUSTMENT_UNSUPPORTED`
(a contract swap not declared as a roll — a strike change on a role is close + re-entry, not adjust), plus the
shared `OPTION_HEDGE_NOT_FILLED` / `OPTION_HEDGE_RELEASE_WITHHELD`. The overlap window's peak margin is prechecked
at admission (R3 §13), never discovered at fill time.

## 6. Structure identity across adjustments

- `structure_digest` stays the **shape** identity: underlying, expiry, leg contracts, sides, ratios, product
  (`option_structure.py:444-471`). It excludes quantity, so a resize does not change identity.
- `structure_units` and per-leg quantities are *size*, carried beside the digest; the frozen desired state is
  their authority.
- The run gains `structure_generation` (int, default 1, in `metadata`). It increments only when an adjust/roll
  **completes** and the run's leg set or shape is rewritten, so a failed or retried adjust keeps a stable basis
  for `based_on_generation`.
- The run's `structure_digest` is updated to the new generation's shape digest on completion, so a read of
  `execution_snapshot.py:697` always reports the shape the run holds now.
- B1/B2.1a: `_same_structure` (`plan_binding.py:302`) compares the new entry plan's digest with the shape held
  **now**. A resized structure is still "already open" (`OPTION_STRUCTURE_ALREADY_OPEN`); a shape-changed structure
  compares unequal, and only the B2.1a unresolved-status rule (any in-flight adjust) plus §4's gates constrain a
  new entry. Consequence: re-entry of a shape the run held *before* an adjust becomes admissible again — §9 Q2.

## 7. Paper vs live

- **B2.2 is paper-only.** The whole adjust path (compile → binding → `_option_run_steps` → sequenced submit →
  settle) lives in `backend/strategies/execution.py`.
- Live must refuse an adjust plan by name in B2.2: `LIVE_OPTION_ADJUST_UNSUPPORTED` in `build_option_steps`
  (`live_sequence.py:711`). The two dependency rules already exist for entry/exit (`:774`, `:780`); B2.2 does not
  enable them for `adjust`.
- **C1.2 adds**: broker margin/funds evidence per increase; stale chain/Greeks refusal; B2.5 max-loss/notional
  checks; protection-owner continuity across an adjust (B2.4); approval bound to plan + exposure snapshot +
  catalog + reservation; `_hedge_release_withheld_rule` (`live_service.py:887`) taught the adjust ordering; and the
  release rules' blocker vocabulary (`live_service.py:837-946`) exercised against the fake broker only.
- The known harness expectation issue (plan doc, "Known pre-existing issues" 1) is fixed in S5.

## 8. Slices

Ordered; each independently testable and committable.

**S1 — payload + compile freeze.** `structure_units`, `based_on_generation`, `protection_policy`, `max_loss`,
`phase: "adjust"`, `OPTION_ADJUSTMENT_REFERENCE_REQUIRED`, `OPTION_ADJUSTMENT_BASIS_REQUIRED`; digest unchanged.
Tests: extend `tests/strategies/test_option_structure_compiler.py` (admit: adjust payload compiles with a frozen
target; refuse: adjust without run reference, without basis, units ≤ 0; legacy entry and exit payloads unchanged).

**S2 — engine model, binding, migration.** `OptionRunStatus.ADJUSTING` + transitions + helpers,
`structure_generation`, migration widening `ck_plan_option_run_phase`, `resolve_plan_option_run` adjust branch,
`_option_run_steps` adjust delta, `_settle_option_run` generation bump.
Tests: `tests/strategies/test_execution.py` (admit: resize converges by delta from own fills; refuse:
`OPTION_ADJUSTMENT_STALE_BASIS`), `tests/options/test_options_execution_lifecycle.py` (transition admit/refuse
pair), `tests/options/test_options_execution_durable_store.py` (generation round-trip),
`tests/integration/test_option_plan_binding_postgres.py` (adjust binding under the widened CHECK).

**S3 — sequencing gates + admission.** reductions-first, hedge-before-short increases, proportional ceiling,
`OPTION_ADJUSTMENT_WOULD_UNHEDGE`, `OPTION_RUN_ADJUST_IN_FLIGHT`, `assess_option_adjust_admissibility` wired into
request creation, approval and admission.
Tests: `tests/strategies/test_execution.py` (admit: increase released on full hedge fill; refuse:
`OPTION_HEDGE_NOT_FILLED` on a partial fill with the short withheld), `tests/api/test_hosted_execution_requests.py`
(admit/refuse pair at request creation). `tests/options/test_hedge_fill_gating.py` only if the gate itself changes.

**S4 — roll + vocabulary.** roll two-stage with the proof gate, `OPTION_ADJUSTMENT_ROLL_INCOMPLETE`, `adjusting`
added to `_UNRESOLVED_RUN_STATUSES`, continuation `_option_run_state`, live `LIVE_OPTION_ADJUST_UNSUPPORTED`.
Tests: `tests/strategies/test_execution.py` (admit: expiry roll completes; refuse: old legs not released before the
new are proven), `tests/strategies/test_continuation.py` (`adjusting` ⇒ outstanding/blocked, `entered` ⇒ held),
one live-lane test for the named refusal, `tests/strategies/test_execution_snapshot_option_runs.py` if the snapshot
row gains `structure_generation`.

**S5 — harness + example.** resize and roll through the governed path, the `options_adjustment` harness expectation
fix, report `documents/hosted-options-dynamic-b2-<date>.md`.
Tests: `tests/strategies/test_hosted_option_example.py`, `tests/strategies/test_hosted_harness_assertions.py`, plus
one supervisor-child harness scenario (phase gate, not per slice).

Budget per slice follows AGENTS.md: one refusal plus one admit mutation pair per safety gate; no full-suite runs; no
new test file where an existing one covers the module.

## 9. Open questions for the owner

1. **`max_loss` enforcement point.** B2.2 freezes it, B2.5 enforces it. Should an adjust be refused at admission
   when the desired state's worst case exceeds the frozen `max_loss`, or defer entirely to B2.5? (Recommendation:
   refuse at admission for `adjust`, since an adjust can raise risk without a new version.)
2. **Re-entry of a previously held shape.** After a shape change, an entry of the old shape compares unequal and is
   admitted by B1. Should the duplicate gate remember every shape generation a run has held, or is "different from
   what is held now" the intended rule?
3. **`structure_units` vs per-leg ratio.** The compiler sizes with `ratio` (`option_structure.py:276`) and the run
   leg carries `lots = ratio` (`plan_binding.py:518`). Is a separate `structure_units` multiplier right, or should
   the full desired state express size purely through per-leg ratios?
4. **Naked declaration.** Is `protection_policy.naked: true` the sanctioned declaration of an intentional naked
   structure, or should the adjust require explicit owner approval bound to the new exposure (R3 §14 D vs
   walkthrough 4)?
5. **Adjust while protection is triggered.** R3 §15 blocks new entries while protection is triggered or unreadable
   and never blocks risk-reducing actions. Split the adjust into reductions-allowed / increases-refused
   (recommended), or refuse the whole plan?
6. **Roll peak margin.** Does the roll's overlap window need a precheck against the admission policy's
   notional/gross limits in B2.2, or is that deferred to B2.5 with the rest of the risk policy?

## 10. Decisions (orchestrator, 2026-09-25)

Design accepted: adjust mutates the existing run through a versioned leg set (§3), and quantity stays out of the
shape digest (§6).

1. **`max_loss`**: B2.2 freezes it and exposes it. Enforcement for every phase lands in B2.5, in one place. Until
   then, adjust increases stay bounded by the existing admission limits (notional, gross, reservations).
2. **Re-entry of an earlier shape**: the rule is "different from what is held now". The duplicate gate does not
   remember history. At most one structure is unresolved at a time (B2.1a), and B2.5 can add a max-structures
   limit.
3. **`structure_units`**: keep it as a separate size multiplier. Ratios express shape and units express size,
   which matches a digest that excludes quantity.
4. **Naked**: `protection_policy.naked: true` in the frozen desired state is the declaration. B2.5 adds the
   version-level "naked permitted" policy that must also allow it. Owner approval applies unchanged under
   `approval_based` mode.
5. **Adjust while protection is triggered or unreadable**: split the plan. Reductions are allowed and increases
   are refused (`OPTION_ADJUSTMENT_PROTECTION_ACTIVE`). Risk reduction is never blocked.
6. **Roll peak margin**: B2.2 admission counts the new generation's increase notional gross, without netting the
   old legs. That is conservative. The margin-based peak check comes with B2.5 and C1.2.
