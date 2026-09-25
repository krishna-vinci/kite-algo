# Phase B1 - safe recurring hosted option continuity

Status: implementation + focused tests + supervisor-child harness extension landed
in the working tree (uncommitted, for Astra's review). No commit, push, deploy,
production database, live order or notification was used.

Scope of this slice: an option structure that outlives one finite supervised
evaluation must never be opened twice, must never be reported flat or settled
while it is held, and must never be closed twice.

## What changed

**1. A duplicate equivalent entry is refused before a run exists**
`backend/options/execution/plan_binding.py`

- `resolve_plan_option_run`'s entry path now asks the platform's OWN
  scope-derived discovery (`OwnedWorkSnapshotService.option_runs_for_scope`) for
  every option run this `(strategy, account, environment)` owns before it creates
  a run, and refuses an equivalent structure that is not provably finished.
- Equivalence is decided by the frozen `structure_digest` when both sides carry
  one, and otherwise by the frozen leg identity + direction set. A comparison that
  cannot be made is a refusal, never "different".
- Refusals, all named: `OPTION_STRUCTURE_ALREADY_OPEN`,
  `OPTION_STRUCTURE_DISCOVERY_UNKNOWN` (unknown/truncated discovery),
  `OPTION_RUN_STATUS_UNKNOWN` (a status outside the durable vocabulary),
  `OPTION_RUN_IDENTITY_UNKNOWN` (a held run that cannot be compared).
- Only `exited` / `settled` stop blocking; every other status - `created`,
  `entry_previewed`, `entering`, `entered`, `partial_entry`, `cleanup_required`,
  `exit_previewed`, `exiting`, `partial_exit` - blocks.
- On PostgreSQL the admission takes a per-scope advisory lock before the existing
  per-plan lock (fixed order, so the two can never deadlock), so two concurrent
  plans for the same structure cannot both read "nothing open".
- A retry of the SAME plan is unchanged: it resolves through its existing binding
  and never reaches this guard.

**2. Option-run work is its own continuation axis**
`backend/strategies/continuation.py`, `backend/strategies/execution_snapshot.py`

- `ContinuationEvidence` gained `option_work_state` (`none | held | outstanding |
  unknown`, default **unknown**) plus an `option_runs` evidence list; both are in
  `continuation_digest` and in the persisted proof, so a change under the caller
  still fails the recheck closed.
- New named refusals: `CONTINUATION_OPTION_WORK_OUTSTANDING`,
  `CONTINUATION_OPTION_WORK_UNKNOWN`.
- `entering`, `entry_previewed`, `created`, `partial_entry`, `cleanup_required`,
  `exit_previewed`, `exiting`, `partial_exit`, an unrecognised status, a partially
  filled/failed leg, or an **unresolved protective exit stage** all block automatic
  continuation.
- A cleanly held structure (`entered`, nothing in flight) MAY continue, and the
  verdict reports `held = true` even when the equity projection is flat - an option
  structure writes no equity leg, so the equity book is blind to it and is never
  allowed to call it flat or settled.
- The read reuses the same scope-derived option-run discovery as `owned_work()`,
  so it cannot be widened by a caller and unknown coverage can never be read as
  "no structure".

**3. A conflicting governed exit is refused while a protective stage is unresolved**
`backend/strategies/execution.py`, `backend/options/protection/staged_exit.py`

- `PaperPlanExecutor._begin_option_run`'s exit branch refuses a governed exit with
  `OPTION_PROTECTIVE_EXIT_UNRESOLVED` (carrying stage digest/state/attempt) when the
  run still owns a `sending`/`unknown` stage claim. Both the live adapter and the
  paper executor reach this single choke point, before any submit.
- The "what is an unresolved stage" rule now lives in one module-level function,
  `staged_exit.unresolved_stage_claim(orders)`, used by the staged-exit engine, the
  governed exit path and the snapshot, so the three can never disagree. An
  unreadable stage payload keeps the claim (fail closed).

**4. Strict exit ownership validation is preserved and pinned**
`backend/options/execution/plan_binding.py`, tests

- No relaxation: `resolve_plan_option_run`'s exit path still validates the entry
  binding's strategy/account/environment, the run's leg identities, the opposite
  direction of every closing leg and the run reference ("an option-run id is not
  the hosted worker-run id"); `_begin_option_run` still refuses
  `OPTION_EXIT_BEFORE_ENTRY` and `OPTION_RUN_EXIT_IN_FLIGHT`, and the leg contract
  still refuses a product mismatch. A caller-supplied id remains a lookup key.
- New tests pin the previously untested refusals: `OPTION_EXIT_SCOPE_MISMATCH`
  (account and environment), `OPTION_EXIT_LEG_MISMATCH`, `OPTION_EXIT_BEFORE_ENTRY`,
  `OPTION_EXIT_CONTRACT_MISMATCH`, plus the positive exit path.
- Note for review: the exit plan's own digest is computed over CLOSING legs, so it
  can never equal the entry run's digest; a digest equality check at exit would be
  meaningless, which is why the exit contract compares frozen leg identity and
  direction against the bound run's own legs instead.

**5. Supervisor-child options example/harness extended**
`examples/hosted_platform/options_index_setup_adjustment.py`,
`examples/hosted_platform/options_index_setup.schema.json`,
`examples/hosted_platform/run_phase5_acceptance.py`

- Two bounded, default-false evaluation-boundary parameters (declared in the
  strategy's parameter schema, so they are visible contract, not hidden switches):
  `hold_after_entry` finishes the evaluation once its own entry has executed and
  leaves the structure held; `duplicate_entry_probe` asks the platform ONCE whether
  a second entry for the held structure would be admitted and reports the answer.
  The probe is skipped - never submitted - when the freshly frozen legs are not the
  structure the held run carries, because a probe that could open something new is
  not a probe.
- New harness scenario `options_restart_hold_close`: job 1 enters and finishes
  holding; a NEW supervised child (job 2) discovers the same durable run from
  `owned_work()["option_runs"]`, is refused a duplicate entry by name, submits no
  duplicate, and closes the one structure it owns. The harness asserts the
  platform's facts, not the child's self-report: exactly one option run, one entry
  edge, one close, all statuses closed, and exactly one
  `OPTION_STRUCTURE_ALREADY_OPEN` refusal.
- `backend/api/schemas/execution_requests.py`: `OwnedOptionRunRow` widened with the
  two new snapshot fields (`structure_digest`, `protective_exit_unresolved`), and
  `execution_snapshot` rows now normalise JSON list columns so a text-shaped driver
  cannot turn "no outstanding leg" into a list of characters.

## Tests run (exact commands, results)

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/strategies/test_execution.py -q` | 74 passed (9 new B1 cases) |
| `.venv/bin/python -m pytest tests/strategies/test_continuation.py -q` | 43 passed (6 new) |
| `.venv/bin/python -m pytest tests/strategies/test_hosted_option_example.py -q` | 25 passed (3 new) |
| `.venv/bin/python -m pytest tests/strategies/test_execution_snapshot_option_runs.py -q` | 2 passed |
| `.venv/bin/python -m pytest tests/options/test_exit_builder_structure.py tests/options/test_expiry_policy.py -q` | 33 passed |
| `.venv/bin/python -m pytest tests/integration/test_option_plan_binding_postgres.py -q` | 11 passed (7 new) |
| `CONTINUATION_PG_URL=... .venv/bin/python -m pytest tests/integration/test_evaluation_continuation_postgres.py -q` | 23 passed (6 new) |
| `HOSTED_EXECUTION_PG_URL=... .venv/bin/python -m pytest tests/integration/test_owned_work_option_runs_postgres.py -q` | 9 passed |
| `HOSTED_EXECUTION_PG_URL=... .venv/bin/python -m pytest tests/integration/test_options_plan_route_postgres.py -q` | 3 passed |
| `timeout 900 .venv/bin/python examples/hosted_platform/run_phase5_acceptance.py --only options_restart_hold_close` | `ok: true` - 1 option run, 1 duplicate-entry refusal |
| `timeout 900 .venv/bin/python examples/hosted_platform/run_phase5_acceptance.py --only momentum_recurring_sequence` | `ok: true` - 4 evaluations, bystander untouched (continuation regression) |

New B1 coverage runs on the production path (`PaperPlanExecutor.execute` and the
real `resolve_plan_option_run` edge): a second equivalent entry is refused and
creates nothing; a genuinely different structure is still admitted; a finished
structure does not block a new entry; a `partial_entry` run blocks; an unrecognised
run status does not read as "no structure"; unknown discovery refuses; an
unresolved protective stage blocks a governed exit and the SAME exit succeeds once
that stage is resolved (mutation pair); exit-before-entry and product mismatch
still refuse; and the continuation PostgreSQL suite proves a held option structure
continues as HELD with a flat equity book, while `partial_entry`,
`cleanup_required`, `exiting` and an unresolved protective stage all block.

## Harness evidence

- `examples/hosted_platform/evidence/phase5-20260925T061012Z.json` - the new
  restart scenario, `ok: true`, one option run, one refusal.
- `examples/hosted_platform/evidence/phase5-20260925T061654Z.json` - the existing
  recurring momentum scenario, `ok: true` after the continuation change.
- `examples/hosted_platform/evidence/phase5-20260925T061314Z.json` - both options
  scenarios in one run: the new scenario passes; `options_adjustment` fails its
  operator-reconciliation expectation (see below).

## Pre-existing failures and limits (not introduced here)

1. `options_adjustment` harness expectation. Its attempt now clears its own block
   through the (already shipped) continuation path, so the operator reconciliation
   route correctly answers `HOSTED_JOB_NOT_BLOCKED` and the harness records a
   failure. Verified PRE-EXISTING: the same failure occurs on a pristine `HEAD`
   worktree (evidence in that worktree,
   `examples/hosted_platform/evidence/phase5-20260925T061545Z.json`). Left
   untouched: it is an expectation of an existing scenario, outside the B1 contract.
2. `tests/options/test_options_execution_durable_store.py` fails its first test on
   both this branch and pristine `HEAD` (`OptionRunCreateRequest` requires
   `tradingsymbol`; the fixture omits it). Not touched.
3. `tests/api/test_hosted_execution_requests.py::test_owner_authorization_surface_is_cookie_scoped_and_idempotent`
   stalls in THIS checkout, while the whole file passes (49 passed) on a pristine
   `HEAD` worktree and the same single test passes on that worktree with all six
   modified backend files applied. Same code, different checkout environment, so
   the stall follows the checkout's untracked runtime state, not the tracked
   change. Reproduction: run the file from this checkout (`-q`), it reaches 41
   passed and stalls at that test; run it from a clean `HEAD` worktree and it
   finishes in ~13s.
4. Broad `tests/strategies` / `tests/sdk` invocations were started and abandoned
   (they did not return inside this sandbox); per the repo testing policy those
   runs were not required and no conclusion was drawn from them.

## What remains for B2

- Re-entry after a proven close is still the next evaluation's job: no automatic
  roll, adjustment or delta hedging was added, and a structure left partially
  entered/exited keeps the strategy blocked for an operator.
- The duplicate-equivalent refusal is a governed-execution refusal; there is no
  pre-execution admission check at proposal/freeze time, and no operator "adopt
  this structure" flow for a run left in `created`/`entering`.
- Reconciling an unresolved protective stage through the staged-exit engine and
  then re-entering automatic continuation is the natural B2 follow-up.
- Options-lane `settled` remains evidence-gated and unchanged; nothing in this
  slice infers settlement.

## Decisions for Astra

- Refusal shape: a duplicate-equivalent entry is refused at execution
  (`OPTION_STRUCTURE_ALREADY_OPEN`, request status `refused`). Refusing at plan
  freeze would be a different seam and needs a decision.
- `option_work_state` defaults to `unknown` on `ContinuationEvidence`, so any future
  caller must declare the axis explicitly (the unit tests now do). That is
  deliberate fail-closed behaviour, and a behavioural change for any out-of-tree
  caller constructing that dataclass.

## Orchestrator review (2026-09-25)

Accepted after one correction.

- Verified against the diff from `ebc98ac`: nothing staged; duplicate-entry gate fails closed
  (discovery error, unknown/truncated coverage, uncomparable identity and unknown status all
  refuse, only `exited`/`settled` skip); scope lock + run + binding share one transaction;
  `option_work_state` is in both `continuation_digest` and the proof; `held` covers option holds
  so a flat equity book never reports a held structure as flat/settled; an unresolved protective
  stage blocks both governed exit and continuation.
- Evidence `phase5-20260925T061012Z.json`: one option run
  (`opt_run_87c746c7969440a3862d56d84c79047e`), entry executed, one
  `OPTION_STRUCTURE_ALREADY_OPEN` refusal, exit executed, `acceptance.ok = true`.
- Correction: `execution_snapshot._json_list` read an unparseable leg column as `[]`, which
  would turn "cannot read pending legs" into "no outstanding legs" and let a run classify as
  `held`. It now raises, and the option-run read reports unknown coverage
  (`option_run_state_unreadable`). Test:
  `tests/strategies/test_execution_snapshot_option_runs.py::test_unreadable_leg_list_is_unknown_coverage_not_no_outstanding_legs`.
- Re-run after the correction: `test_execution_snapshot_option_runs.py` + `test_continuation.py`
  46 passed; `test_execution.py` 74 passed.
