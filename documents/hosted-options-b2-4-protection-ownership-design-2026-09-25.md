# B2.4 design — durable protection ownership for option runs

Designer pass, read-only. Baseline: `/home/krishna/kite-algo` @ `847fdb5` (branch `development`,
alembic head `20260925_000045`). Authority: plan B2.4
(`documents/hosted-strategies-production-live-readiness-plan-2026-09-25.md:84-88`), R3 §15
(`documents/hosted-strategies-architecture-r3.md:326-335`), B2 phase report
(`documents/hosted-options-dynamic-b2-2026-09-25.md`).

## 0. Verified baseline

**Protection is enumerated per OPEN WORKER RUN, not per structure.**

- The loop enumerates runs, not positions: `WorkerProtectionRuntime.evaluate_once` iterates
  `repo.list_protection_enabled_runs()` (`backend/api/services/protection_runtime.py:53-67`), wired in the
  background daemon (`backend/app/background.py:120-153`). The SQL is
  `WHERE r.status IN ('open','exiting') AND runtime_state_json -> 'backend_protection' ->> 'enabled' = TRUE`
  (`backend/api/repositories/algo_worker_repo.py:740-758`).
- So a run leaves protection by *status change alone*: closure sets `status='closed'`
  (`backend/strategies/repository.py:986-1012`) and the row simply stops being evaluated. There is no
  ownership record that outlives it.

**The policy is pinned per worker run; the option run carries a different, frozen block.**

- Hosted run creation seeds the only policy a hosted strategy can declare today — `stale_exit_policy` maps
  to `backend_protection` via `_protection_runtime_state` (`backend/api/services/hosted_lifecycle.py:223-240`)
  and is written into the run at `:385-428`. Its `version` int is the only existing version counter
  (`backend/api/services/protection.py:215-231`; bumped by `_next_backend_protection_for_patch`,
  `backend/api/routers/worker_protection.py:895-900`). The structure identity rides in the same config
  (`StructureIdentity`, `backend/api/services/protection.py:195-212`).
- The durable option run carries its own frozen `protection` block (structure digest, underlying,
  expiry policy) written at creation (`backend/options/execution/plan_binding.py:1026-1033`, column
  `backend/schema.sql:1212-1226`). The option-run evaluator reads *that* block
  (`evaluate_option_protection_state`, `backend/options/protection/runtime.py:71-105`). An adjust rewrites it
  wholesale (`_adjusted_protection`, `backend/strategies/execution.py:1226-1243`) and bumps the leg generation
  (`:2160-2182`).

**Structure-aware exits are resolved THROUGH the worker run, by mutable metadata.**

- `StagedStructureExit.resolve_run_for_worker_run` finds the one run with
  `option_run_states.metadata ->> 'worker_run_id' = <worker run>` and refuses ambiguity
  (`backend/options/protection/staged_exit.py:182-250`). The stage claim CAS lives on the run's own `orders`
  (`backend/options/execution/durable_store.py:523-560`); `unresolved_stage_claim` is the module-level shared
  rule (`backend/options/protection/staged_exit.py:89-135`).
- `metadata.worker_run_id` is written **only at creation** (`plan_binding.py:1031-1038`); nothing rewrites it on
  an adjust or exit edge (`grep worker_run_id backend/options/execution/*.py` → creation only). The plan→run edge
  does carry the newer `worker_run_id` per phase (`backend/schema.sql:2855-2881`;
  `plan_binding.py:1256-1265`), but the staged exit does not read the edge.

**Two lookups key the "owner", and they disagree.**

- Generic protection → staged exit resolves by `metadata ->> 'worker_run_id'` (`staged_exit.py:201`, above).
- The safety gate resolves the option run by **primary key = the worker run id**
  (`store.get_run_in_session(session, strategy_run_id)` at `backend/api/routers/worker_protection.py:688-700`,
  called from `:607-620`, `:830-880`, `:946-1000`). Plan-created runs are `opt_run_<uuid>`
  (`durable_store.py:144-146`; evidence `examples/hosted_platform/evidence/phase5-20260925T122615Z.json`:
  `opt_run_4e594d8c…` vs hosted `run_58c1a524…`), so that arm is `applicable: False`
  (`worker_protection.py:700-720`) unless the run was made through the direct options API with a chosen
  `strategy_run_id` (`backend/options/api/execution_router.py:258-261`), which is exactly the shape the tests
  seed (`tests/options/test_options_api_routes.py:235-253`). The `OPTIONS_*` blocking reasons
  (`:629-649`; `backend/api/services/safety.py:11-17`, `:37-40`) therefore only fire on the direct path today.

**What blocks a conflicting operation today.**

- `OPTION_ADJUSTMENT_PROTECTION_ACTIVE` — an increase while the run's own block is triggered/unreadable;
  reductions stay admissible (`backend/strategies/execution.py:1587-1600`).
- `OPTION_PROTECTIVE_EXIT_UNRESOLVED` — an unresolved stage blocks adjust and governed exit
  (`execution.py:1915-1950`; `backend/options/execution/plan_binding.py:922-928`).
- `OPTION_RUN_ADJUST_IN_FLIGHT` — a not-provably-finished adjust owner blocks takeover
  (`execution.py:1770-1800`; the owner rule `option_adjust_owner_state`, `plan_binding.py:718-763`).
  This is the closest existing precedent: a durable, read-back "owner set" where `unknown ≠ none`.

**Continuation refuses any protected book, by name.**

- `protection_enabled` is read from the predecessor run's `runtime_state.backend_protection.enabled`
  (`backend/strategies/continuation.py:215`, `:863-882`) and the gate refuses (`:437-452`) —
  `CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED` (`:100-102`).
- Rationale in source: protection covers a run while its own status is `open`, each run pins its own policy,
  so handover would leave two owners or a gap (`:441-452`). The unblock transaction closes the run
  (`:1261-1284` → `reconcile_with_audit(close_worker_run=True)`, `backend/strategies/repository.py:835-880`,
  `:986-1012`); with protection enabled that closure would silently strip protection.
- The B2 harness therefore only exercises the unprotected shape: `stale_exit_policy: "none"`
  (`examples/hosted_platform/run_phase5_acceptance.py:1948-1951`), and the unit tests assert the refusal
  (`tests/strategies/test_continuation.py:219-228`, `:502-513`).
- Schema precedent for what I recommend: `strategy_execution_barriers` — one mutable row keyed by
  `(account, strategy, environment)` with a version, plus an append-only event log
  (`backend/schema.sql:3157-3200`).

## 1. Owner model

**New table + append-only event log** (not a column on `option_run_states`; not a new meaning for
`metadata.worker_run_id`). One row per option run makes "two owners" unrepresentable, which is stronger than a
partial unique index that still allows a stale second row.

```sql
CREATE TABLE public.option_protection_owners (
    option_run_id        TEXT PRIMARY KEY
        REFERENCES public.option_run_states(strategy_run_id) ON DELETE RESTRICT,
    strategy_id          TEXT NOT NULL,
    account_id           TEXT NOT NULL,
    execution_environment TEXT NOT NULL,
    owner_run_id         TEXT,                        -- hosted worker run currently authoritative
    owner_epoch          BIGINT NOT NULL DEFAULT 1,   -- CAS ticket, monotonic per run
    policy_version       TEXT NOT NULL,               -- digest, section 4
    policy               JSONB NOT NULL,              -- frozen policy snapshot, section 2
    action_state         TEXT NOT NULL DEFAULT 'none'
        CHECK (action_state IN ('none','claimed','staging','unresolved')),
    stage_digest         TEXT,
    state                TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','released')),
    released_at          TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_opo_owner_present CHECK ((state = 'active') = (owner_run_id IS NOT NULL)),
    CONSTRAINT ck_opo_environment CHECK (execution_environment IN ('live','paper','dry_run')),
    CONSTRAINT fk_opo_strategy FOREIGN KEY (strategy_id, account_id)
        REFERENCES public.strategies(id, account_scope) ON DELETE RESTRICT
);
CREATE TABLE public.option_protection_owner_events (   -- append-only, barrier precedent
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    option_run_id TEXT NOT NULL, owner_epoch BIGINT NOT NULL,
    event TEXT NOT NULL CHECK (event IN
        ('claimed','transferred','policy_changed','action_claimed','action_resolved','released')),
    owner_run_id TEXT, actor_id TEXT, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

- **Key**: `option_run_id` is globally unique (it is `option_run_states`' PK), so it is the key;
  `(strategy_id, account_id, execution_environment)` are stored and FK-checked against `strategies` exactly as
  `strategy_plan_option_runs` does (`backend/schema.sql:2855-2881`) — a row that disagrees with the run's own
  scope is a data error, never a second owner.
- **States: two only — `active` and `released`.** I deliberately **reject `transferring`**: a two-phase state
  is a gap by construction (there is a committed instant with no owner), and the requirement is "old owner stays
  authoritative until the new one is committed". The transfer is a single CAS `UPDATE` (section 2), so it is
  atomic without an intermediate state.
- `owner_epoch` is the compare-and-swap ticket: every claim/transfer/policy change increments it.

## 2. Transfer protocol (one transaction, no gap)

1. **Creation.** In `_create_entry_run_atomically` (`plan_binding.py:1407-1520`), in the same transaction that
   writes the run + the entry edge, insert the owner row: `owner_run_id = worker_run_id`, `owner_epoch = 1`,
   `policy` = the frozen snapshot (section 4), `state='active'`. If the plan carries a structure policy, the
   worker run's `backend_protection.structure` is populated from the same snapshot so the generic loop and the
   option evaluator read one policy.
2. **Transfer** (`transfer_owner`), invoked at **successor hosted-run creation** (extend the seeding in
   `hosted_lifecycle.py:385-428`), not at first evaluation:

   ```sql
   UPDATE option_protection_owners
      SET owner_run_id = :successor, owner_epoch = owner_epoch + 1,
          policy = :policy, policy_version = :version, updated_at = NOW()
    WHERE option_run_id = :run AND state = 'active' AND owner_epoch = :observed
   RETURNING owner_epoch
   ```

   run inside `pg_advisory_xact_lock('option-run:'||:run)` (the lock `_resolve_adjust_binding` already takes,
   `plan_binding.py:1238-1248`), together with the run's protection-config write. Zero rows → conflict →
   refuse `OPTION_PROTECTION_OWNER_CONFLICT`; the losing caller re-reads, never retries blindly. The
   predecessor is **never** released by the transfer; its authority ends only because a newer `owner_epoch`
   exists. There is no interval in which the row is absent or ownerless.
3. **Loop reads owner rows.** Add `list_protection_owners()` and iterate owner rows (join `option_run_states`
   for live evidence, not `algo_worker_runs`). The row carries the policy, so protection survives the
   predecessor's closure; the owner row is authoritative **even when its `owner_run_id` run is not `open`** —
   that is the whole point, and it replaces the "run is open" predicate at `algo_worker_repo.py:752-754`.
   `owner_run_id` is used for attribution, timeline, and "is the caller the current owner".
4. **Release** in the same transaction as a terminal run status (`exited`/`settled`, written by the exit path):
   `state='released'`, `released_at=NOW()`, `owner_run_id=NULL`, increment `owner_epoch`, append `released`.
   Never released while the run is non-terminal — an operator "stop evaluator" leaves it `active`
   (R3 "Never cancelled", `hosted-strategies-architecture-r3.md:333`).
5. **Unreadable row = fail closed for exposure, open for reductions.** If the row cannot be read (DB error) or
   is `released` while the run is not terminal, `assess_option_entry_admissibility` /
   `assess_option_adjust_admissibility` refuse increases with **`OPTION_PROTECTION_OWNER_UNKNOWN`**, and
   `_safety_blocking_reasons` (`worker_protection.py:629-649`) appends the same reason; reduce-only adjust and
   exit stay admissible (the same split as `OPTION_ADJUSTMENT_PROTECTION_ACTIVE`, `execution.py:1587-1600`).

## 3. Blocking rules (reconciled)

| Situation | Read from | Effect |
| --- | --- | --- |
| Triggered / unreadable policy | owner `policy` (fallback: run's own block) | increases refused `OPTION_ADJUSTMENT_PROTECTION_ACTIVE`; reductions and exit admitted (unchanged) |
| Exit claim in flight (`action_state='claimed'/'staging'`) | owner row | adjust and governed exit refused `OPTION_PROTECTIVE_EXIT_UNRESOLVED` (unchanged names) |
| Unresolved stage in the run's own `orders` | `unresolved_stage_claim` | unchanged, and the loop mirrors it into `action_state='unresolved'` so gates and loop cannot disagree |
| Owner absent / unreadable / released-non-terminal | owner row | **new** `OPTION_PROTECTION_OWNER_UNKNOWN`: new exposure refused at request creation, admission, execution and the safety gate; risk-reducing actions allowed |
| Owner `owner_epoch` ≠ the epoch recorded on the caller's worker run | owner row + `runtime_state` | refuse `OPTION_PROTECTION_OWNER_CONFLICT` (a superseded run must not act) |

The owner row becomes the single place `action_state` lives; the run's `orders` stage claims stay the evidence,
and neither the loop nor a gate derives "clear" from absence.

## 4. Policy version

`policy_version = sha256(canonical_json({structure_digest, structure_id, underlying, expiry, expiry_policy,
rules, precedence, stale_exit_policy, operations}))` — everything the loop needs to decide, not just the
digest. Triggers for a new version, all applied by the **current owner** in the same CAS that bumps
`owner_epoch`:

- an adjust freezes a new `protection_policy` / generation (`_adjusted_protection`, `execution.py:1226-1243`;
  generation bump `:2160-2182`);
- the platform patches the run's protection (`version++`, `worker_protection.py:895-900`);
- a new strategy version declares a different policy (B2.5).

A transfer that carries a **changed** policy is still one CAS: new `owner_run_id` + new
`policy`/`policy_version` + `owner_epoch+1`, plus a `transferred`/`policy_changed` event pair. There is no
window in which two policies are both active, because there is only ever one row. (This is why a digest, not an
int, is the identity: two runs can both be at "version 3" with different content.)

## 5. Continuation change

Replace the blanket refusal (`continuation.py:452`) with a transfer **only** when all hold:

1. `option_work_state == "held"` and `protection_state == "settled"` (no interim exit claim; an active claim
   still returns `CONTINUATION_PROTECTION_IN_FLIGHT`, `:445-447`);
2. an **active** owner row exists for that option run in the same `(strategy, account, environment)`;
3. the row's `owner_run_id` == the predecessor's `run_id` (the predecessor really is the owner);
4. the row is readable and its `policy_version` matches the policy the predecessor was running.

Then the owner row stays `active` with the predecessor as owner, the predecessor's worker run is closed
(`:1261-1284`, unchanged), and the successor's hosted-run creation performs the CAS transfer (section 2).
Protection is continuous across the unblock because the loop reads the owner row, not the predecessor's
status.

Still refuse, by name:

- no owner row / unreadable / `released`-non-terminal → `CONTINUATION_PROTECTION_OWNER_UNKNOWN` (new);
- `owner_run_id` ≠ predecessor, or epoch mismatch → `CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED` (kept);
- unresolved protective stage or `action_state != 'none'` → `CONTINUATION_PROTECTION_IN_FLIGHT`;
- any non-option run with `protection_enabled` (the existing generic `stale_exit_policy` case) →
  `CONTINUATION_PROTECTION_OWNERSHIP_UNSUPPORTED` (scope unchanged; the owner model is for option runs).

## 6. Live

Paper only. The owner row, `owner_epoch` CAS and transfer are lane-neutral and should be written for
`execution_environment='live'` too, but nothing live can reach them in this phase: the live lane refuses an
adjust by name (`LIVE_OPTION_ADJUST_UNSUPPORTED`, `backend/strategies/live_sequence.py:749`,
`backend/strategies/live_adapter.py:984`) and creates no option structures. So B2.4 changes no live behaviour;
C1.2 owns activating it, and its live approval (plan + exposure snapshot + catalog + reservation) must be
pinned to `policy_version` so a policy change invalidates the approval.

## 7. Slices

**S1 — owner row + repository** (migration `20260925_000046`, `backend/schema.sql`).
`OptionProtectionOwnerStore` (`claim`, `read`, `transfer` CAS, `record_action`, `release`) with the creation
hook in `_create_entry_run_atomically` and the release hook in the terminal-status write.
Tests: `tests/integration/test_option_protection_ownership_postgres.py` — two concurrent `transfer` calls on
one run, exactly one wins and `owner_epoch` advances once; a second active row is impossible; release twice is
idempotent; release is refused while non-terminal.

**S2 — gates read the owner row** (loop + safety gate + entry/exit paths).
`list_protection_owners()` drives the loop; `OPTION_PROTECTION_OWNER_UNKNOWN` added to
`assess_option_entry_admissibility` / `assess_option_adjust_admissibility` and `_safety_blocking_reasons`; the
safety gate resolves the run through the owner row, removing the PK-vs-metadata asymmetry (section 0).
Tests: `tests/options/test_options_protection_ownership.py` — refusal twin (increase with unknown owner refused
by name, no owner decision recorded) + admit twin (same request with an active owner admitted); risk-reducing
exit admitted under an unknown owner. Extend `tests/api/test_algo_worker_api.py` for the safety-gate reason.

**S3 — transfer on successor creation + continuation exemption.**
Transfer hook in `hosted_lifecycle.py:385-428`; replace `continuation.py:452` with the section 5 condition.
Tests: `tests/strategies/test_continuation.py` — admit twin (protected held option run with an active owner row
owned by the predecessor → eligible) + refusal twins (no owner row; owner ≠ predecessor; unresolved stage);
PG twin for the transfer-under-lock path.

**S4 — policy version + adjust interaction.**
`policy_version` digest wired through `_adjusted_protection` and the patch path;
`OPTION_ADJUSTMENT_PROTECTION_ACTIVE` and `OPTION_PROTECTIVE_EXIT_UNRESOLVED` read the owner policy when present.
Tests: `tests/strategies/test_execution.py` (increase refused, reduction admitted; policy change bumps version
and `owner_epoch` exactly once).

**Final harness** — new scenario `options_protection_handover` in
`examples/hosted_platform/run_phase5_acceptance.py`: a **protected** option structure (declared policy, not
`stale_exit_policy: "none"`) held across two evaluations, asserting exactly one `active` owner row with one
`owner_run_id` at every checkpoint (predecessor before unblock, predecessor after unblock but before the
successor exists, successor after creation), a clear continuation with the policy unchanged, and one increase
refusal + admit twin while the owner row is unknown.

## 8. Open questions

1. **Table vs columns** — Recommend the new table + event log (section 1): the barrier precedent keeps
   `option_run_states` unchanged and makes dual ownership unrepresentable. Risk: one more join in a hot loop.
2. **Transfer point** (successor run creation vs first evaluation) — Recommend **run creation**, so the
   unprotected window is bounded by the successor's boot, not by its first evaluation. If the team prefers not
   to touch `hosted_lifecycle`, the fallback is first-evaluation transfer with the predecessor authoritative
   throughout; the invariant is identical, only the window is longer.
3. **Operator stop** — Recommend the owner row stays `active` on "stop evaluator" (risk-reducing authority
   survives, R3 section 15) and is released only by terminal run status. Should an explicit operator "flatten"
   also release? Recommendation: no — flatten is an action, release belongs to the terminal state.
4. **Non-option runs** — Should the generic `stale_exit_policy` protection move onto the same owner row?
   Recommend **no** in B2.4 (keeps the diff scoped and the continuation contract unchanged for equities);
   revisit in B2.5 when the strategy version declares the policy and there is a resource to key on.
5. **Policy identity** — digest vs integer. Recommend digest for identity + the existing integer `version` for
   human display, so "version 3" is never trusted as unique.

## 9. Decisions (orchestrator, 2026-09-25)

Design accepted: one owner row per option run plus an append-only event log. The owner changes with a single
`owner_epoch` CAS under the `option-run:` advisory lock. There is no `transferring` state.

1. **Table vs columns:** a new table and event log, as recommended.
2. **Transfer point:** successor run creation, as recommended.
3. **Operator stop:** the owner stays `active`. Only a terminal run status releases it, and flatten does not.
4. **Non-option runs:** no change in B2.4. Generic `stale_exit_policy` protection keeps its current continuation
   refusal.
5. **Policy identity:** the digest is the identity, and the integer `version` is for display only.
6. **Migration numbering:** B2.5 takes `20260925_000046`, and B2.4 S1 takes the next revision after it.
7. **Safety-gate gap (§0):** today the worker safety gate never resolves plan-created option runs, so `OPTIONS_*`
   blocking reasons never fire for hosted strategies. B2.4 S2 must close this. It is a correctness fix, not an
   optional extra.
