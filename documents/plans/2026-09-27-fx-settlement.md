# Option Settlement Lifecycle + Generation History Implementation Plan

> **For agentic workers:** Execute this plan task-by-task, in order. Steps use checkbox (`- [ ]`) syntax. Do not load brainstorming or other workflow skills. Do not deviate: if a step's code does not fit the file you see, stop and report the mismatch instead of improvising.

**Goal:** Settling an option run validates the run and moves it to `settled` atomically; the settlement barrier's option axis is scoped to one strategy+environment and uses the barrier's real axis vocabulary; released leg generations are never dropped.

**Architecture:** `OptionSettlementService.settle()` does everything in one DB transaction: lock the `option_run_states` row, check the run belongs to the account (via `strategy_plan_option_runs` or `option_protection_owners`), check the structure digest, compare-and-set the status to `settled`, insert the evidence row, release the protection owner. `option_settlement_axes()` lists the strategy's own option runs and reports `satisfied` / `failed` / `unknown` — the only states `_make_axis` in `backend/strategies/settlement.py:1135-1145` accepts (today it emits `settled`/`unsettled`, which `_make_axis` silently turns into `unknown`, so every book with the adapter registered rolls up `unknown`). The generation-history cap `history[-10:]` is removed.

**Tech Stack:** Python 3.11, SQLAlchemy (raw `text()` SQL, SQLite for unit tests, PostgreSQL 15433 for integration), unittest/pytest.

**Spec:** sol options audit (`.claude/codex-runs/options-audit/last.md`, rows "Settlement lifecycle" and "Generation history").

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-settlement`, branch `codex/fx-settlement`. Read `AGENTS.md` first.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` run from the worktree root.
- PostgreSQL tests only against `postgresql://postgres:testonly@127.0.0.1:15433` (env below). Never another DB/port.
- Never touch `.env*`. Fake broker only. No migrations in this plan (no schema change).
- Do NOT commit — the orchestrator commits. (Where a step says "Checkpoint", just run `git status` and continue.)
- Do not edit files outside the "Files" lists below.

## Decisions (fixed, do not revisit)

1. A run may be settled from `entered`, `partial_exit`, `cleanup_required` or `exited` (held-to-expiry structures are `entered`; settlement is what ends them). `settled` has no outgoing transition.
2. Settling an already-`settled` run is idempotent: returns the latest evidence with `already_settled: True`, inserts nothing.
3. A run with no ownership record (neither `strategy_plan_option_runs` nor `option_protection_owners` row) cannot be settled: `SETTLEMENT_RUN_UNOWNED`.
4. Digest check: if `metadata.structure_digest` is set on the run and differs from the caller's → `SETTLEMENT_DIGEST_MISMATCH`. If the run has no recorded digest, the caller's digest is accepted.
5. Barrier axis: no option runs for the strategy/env → `satisfied` (`no_option_runs`); any run not in `{exited, settled}` → `failed` (`option_runs_open`); a bound run id with no `option_run_states` row → `unknown` (`option_run_state_missing`); query error → `unknown` (`option_runs_unreadable`).
6. Generation history keeps every generation (a weekly roller makes ~52/year; bounded by roll count).

## File Structure

- Modify `backend/options/execution/lifecycle.py` — add `SETTLED` transitions + `SETTLEABLE_STATUSES` + `mark_settled`.
- Modify `backend/options/protection/expiry_policy.py` — `SettlementRefusal` gets per-instance reason codes; `settle()` rewritten transactional; `option_settlement_axes()` rewritten.
- Modify `backend/strategies/execution.py:2406-2421` — use new `append_structure_generation()`; no cap.
- Tests: `tests/options/test_options_lifecycle.py` (create if absent — check `ls tests/options/ | grep lifecycle` first and extend the existing file if one exists), `tests/options/test_expiry_policy.py`, `tests/integration/test_options_structures_postgres.py`, `tests/api/test_strategy_owner_and_binding.py`, `tests/strategies/test_execution.py`.
- Docs: `documents/kite-algo-platform-reference.md` (settlement lines only).

---

### Task 1: Lifecycle — `settled` is reachable and terminal

**Files:**
- Modify: `backend/options/execution/lifecycle.py`
- Test: `tests/options/test_options_lifecycle.py` (or the existing lifecycle test file found by `ls tests/options | grep -i lifecycle`)

**Interfaces:**
- Produces: `SETTLEABLE_STATUSES: tuple[str, ...]` = `("cleanup_required", "entered", "exited", "partial_exit")` (sorted); `mark_settled(state: OptionRunState) -> OptionRunState`.

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from backend.options.execution.lifecycle import SETTLEABLE_STATUSES, mark_settled, transition_to
from backend.options.execution.models import OptionRunState, OptionRunStatus


def _state(status: str) -> OptionRunState:
    return OptionRunState(
        strategy_run_id="run-1", strategy_name="s", product="NRML", legs=[], protection=None,
        metadata={}, status=status, completed_legs=[], failed_legs=[], pending_legs=[],
        orders=[], trades=[],
    )


class SettledLifecycleTests(unittest.TestCase):
    def test_held_and_exited_runs_can_settle(self):
        self.assertEqual(SETTLEABLE_STATUSES, ("cleanup_required", "entered", "exited", "partial_exit"))
        for status in SETTLEABLE_STATUSES:
            self.assertEqual(mark_settled(_state(status)).status, "settled")

    def test_settled_is_terminal_and_unreachable_from_in_flight_states(self):
        with self.assertRaises(ValueError):
            transition_to(_state("settled"), OptionRunStatus.ENTERED)
        for status in ("created", "entering", "exiting", "adjusting"):
            with self.assertRaises(ValueError):
                mark_settled(_state(status))
```

If `OptionRunState(...)` needs different constructor arguments, copy the argument list from `transition_to()` in `lifecycle.py:57-72` (it builds one with exactly these fields).

- [ ] **Step 2: Run to verify failure**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_options_lifecycle.py -q -k Settled`
Expected: FAIL — `ImportError: cannot import name 'SETTLEABLE_STATUSES'`.

- [ ] **Step 3: Implement**

In `lifecycle.py`, add `OptionRunStatus.SETTLED.value` to the allowed-target sets of `CLEANUP_REQUIRED`, `ENTERED`, `PARTIAL_EXIT`, and change `EXITED` to allow settled; add a terminal `SETTLED` entry:

```python
    OptionRunStatus.PARTIAL_ENTRY.value: {OptionRunStatus.CLEANUP_REQUIRED.value},
    OptionRunStatus.CLEANUP_REQUIRED.value: {
        OptionRunStatus.EXIT_PREVIEWED.value,
        # A structure held into expiry ends by settlement, with evidence.
        OptionRunStatus.SETTLED.value,
    },
```
```python
    OptionRunStatus.ENTERED.value: {
        OptionRunStatus.EXIT_PREVIEWED.value,
        # Compatibility for direct partial-exit helpers.
        OptionRunStatus.PARTIAL_EXIT.value,
        # A desired-state mutation of the held structure.
        OptionRunStatus.ADJUSTING.value,
        OptionRunStatus.SETTLED.value,
    },
```
```python
    OptionRunStatus.PARTIAL_EXIT.value: {
        OptionRunStatus.EXITING.value,
        # Compatibility for direct close helper.
        OptionRunStatus.EXITED.value,
        OptionRunStatus.SETTLED.value,
    },
    OptionRunStatus.EXITED.value: {OptionRunStatus.SETTLED.value},
    OptionRunStatus.SETTLED.value: set(),
}

#: The statuses settlement evidence may end. Derived from the table so the two
#: can never disagree.
SETTLEABLE_STATUSES: tuple[str, ...] = tuple(
    sorted(
        status
        for status, targets in _ALLOWED_TRANSITIONS.items()
        if OptionRunStatus.SETTLED.value in targets
    )
)
```

Append at the end of the file:

```python
def mark_settled(state: OptionRunState) -> OptionRunState:
    next_state = transition_to(state, OptionRunStatus.SETTLED)
    next_state.pending_legs = []
    return next_state
```

- [ ] **Step 4: Run to verify pass**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_options_lifecycle.py -q`
Expected: all PASS. Then run `grep -rn "EXITED.value: set()" backend tests` — expected no output (nothing else relied on exited having no transitions). If a test asserts `exited` is a dead end, report it; do not change it.

- [ ] **Step 5: Checkpoint** — `git status`.

---

### Task 2: Transactional, validated `settle()`

**Files:**
- Modify: `backend/options/protection/expiry_policy.py:74-83` (`SettlementRefusal`), `:182-310` (`OptionSettlementService`)
- Test: `tests/options/test_expiry_policy.py` (class `EvidenceTests`, base `ExpiryTestCase`)
- Test: `tests/api/test_strategy_owner_and_binding.py:2226-2231`

**Interfaces:**
- Consumes: `SETTLEABLE_STATUSES` (Task 1); `OptionProtectionOwnerStore(session_factory=...).release(option_run_id, db=session)` (`backend/options/protection/ownership.py:737`).
- Produces: `SettlementRefusal(detail, reason_code="SETTLEMENT_EVIDENCE_REQUIRED")`; `settle(...)` returns `{"settled": True, "run_state": "settled", "evidence": {...}, "already_settled": bool}`; new reason codes `SETTLEMENT_RUN_UNKNOWN`, `SETTLEMENT_RUN_UNOWNED`, `SETTLEMENT_ACCOUNT_MISMATCH`, `SETTLEMENT_DIGEST_MISMATCH`, `SETTLEMENT_RUN_NOT_SETTLEABLE`.
- Helper for Task 3: `_owned_option_run_ids(db, *, account_id, strategy_id, execution_environment) -> set[str]` and `_run_owner_accounts(db, option_run_id) -> set[str]`.

- [ ] **Step 1: Add the fixture DDL and seeding helper to `ExpiryTestCase`**

In `tests/options/test_expiry_policy.py`, at the end of `ExpiryTestCase.setUp` (after `self.factory = sessionmaker(bind=self.engine)`), add:

```python
        # option_run_states has no ORM model; the database owns it. Mirror the
        # columns the settlement path reads.
        with self.engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE public.option_run_states (
                    strategy_run_id TEXT PRIMARY KEY, strategy_name TEXT, product TEXT,
                    status TEXT NOT NULL, legs TEXT, protection TEXT, metadata TEXT,
                    orders TEXT, trades TEXT, completed_legs TEXT, failed_legs TEXT,
                    pending_legs TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            ))
```

Add this method to `ExpiryTestCase`:

```python
    def seed_run(self, run_id, *, status="entered", digest="d-1", account_id="kite:A",
                 strategy_id="stg-A", environment="paper", owned=True):
        import json as _json

        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT OR IGNORE INTO public.strategies (id, owner_id, name, account_scope) "
                "VALUES (:sid, 'app:owner', :sid, :acct)"
            ), {"sid": strategy_id, "acct": account_id})
            conn.execute(text(
                "INSERT INTO public.option_run_states (strategy_run_id, strategy_name, product, "
                "status, legs, metadata, orders, trades, completed_legs, failed_legs, pending_legs) "
                "VALUES (:rid, 'n', 'NRML', :status, '[]', :meta, '[]', '[]', '[]', '[]', '[]')"
            ), {"rid": run_id, "status": status,
                "meta": _json.dumps({"structure_digest": digest} if digest else {})})
            if owned:
                conn.execute(text(
                    "INSERT INTO public.option_protection_owners (option_run_id, strategy_id, "
                    "account_id, execution_environment, policy_version, policy, state) "
                    "VALUES (:rid, :sid, :acct, :env, 'v1', '{}', 'released')"
                ), {"rid": run_id, "sid": strategy_id, "acct": account_id, "env": environment})

    def run_status(self, run_id):
        with self.engine.connect() as conn:
            return conn.execute(text(
                "SELECT status FROM public.option_run_states WHERE strategy_run_id = :rid"
            ), {"rid": run_id}).scalar()
```

If `strategies` or `option_protection_owners` is not in the `public` schema in SQLite (error `no such table: public.strategies`), drop the `public.` prefix for those two INSERTs only.

- [ ] **Step 2: Rewrite the evidence tests (failing)**

Replace the bodies of `EvidenceTests.test_cash_settlement_with_evidence_settles_the_run` and `test_physical_settlement_is_recorded_as_its_own_kind` so they seed first, and add new tests. Final `EvidenceTests` settle-related tests:

```python
    def test_cash_settlement_with_evidence_settles_the_run(self):
        self.seed_run("run-1", digest="d-1")
        result = self.service().settle(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            settlement_kind="cash", evidence_source="contract_note",
            evidence_ref={"note_id": "CN-1"}, recorded_by="app:owner",
        )
        self.assertTrue(result["settled"])
        self.assertEqual(result["run_state"], "settled")
        self.assertFalse(result["already_settled"])
        self.assertEqual(self.run_status("run-1"), "settled")
        rows = self.service().evidence_for(option_run_id="run-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_source"], "contract_note")
        self.assertEqual(rows[0]["settlement_kind"], "cash")

    def test_physical_settlement_is_recorded_as_its_own_kind(self):
        self.seed_run("run-2", digest="d-2")
        result = self.service().settle(
            account_id="kite:A", option_run_id="run-2", structure_digest="d-2",
            settlement_kind="physical", evidence_source="exchange_file",
            evidence_ref={"file_id": "EF-9"}, recorded_by="app:owner",
        )
        self.assertTrue(result["settled"])
        self.assertEqual(result["evidence"]["settlement_kind"], "physical")

    def _refusal(self, **overrides):
        from backend.options.protection.expiry_policy import SettlementRefusal

        kwargs = dict(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            evidence_source="contract_note", evidence_ref={"n": 1}, recorded_by="app:owner",
        )
        kwargs.update(overrides)
        with self.assertRaises(SettlementRefusal) as ctx:
            self.service().settle(**kwargs)
        return ctx.exception

    def test_an_unknown_run_is_refused_and_records_nothing(self):
        self.assertEqual(self._refusal().reason_code, "SETTLEMENT_RUN_UNKNOWN")
        self.assertEqual(self.service().evidence_for(option_run_id="run-1"), [])

    def test_an_unowned_run_is_refused(self):
        self.seed_run("run-1", owned=False)
        self.assertEqual(self._refusal().reason_code, "SETTLEMENT_RUN_UNOWNED")

    def test_another_accounts_run_is_refused(self):
        self.seed_run("run-1", account_id="kite:B")
        self.assertEqual(self._refusal().reason_code, "SETTLEMENT_ACCOUNT_MISMATCH")
        self.assertEqual(self.run_status("run-1"), "entered")

    def test_a_different_structure_digest_is_refused(self):
        self.seed_run("run-1", digest="d-other")
        self.assertEqual(self._refusal().reason_code, "SETTLEMENT_DIGEST_MISMATCH")
        self.assertEqual(self.service().evidence_for(option_run_id="run-1"), [])

    def test_an_in_flight_run_is_not_settleable(self):
        self.seed_run("run-1", status="exiting")
        self.assertEqual(self._refusal().reason_code, "SETTLEMENT_RUN_NOT_SETTLEABLE")
        self.assertEqual(self.run_status("run-1"), "exiting")

    def test_settling_twice_is_idempotent(self):
        self.seed_run("run-1")
        first = self.service().settle(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            evidence_source="broker_ledger", evidence_ref={"l": 1}, recorded_by="app:owner",
        )
        second = self.service().settle(
            account_id="kite:A", option_run_id="run-1", structure_digest="d-1",
            evidence_source="broker_ledger", evidence_ref={"l": 2}, recorded_by="app:owner",
        )
        self.assertTrue(second["already_settled"])
        self.assertEqual(second["evidence"]["id"], first["evidence"]["id"])
        self.assertEqual(len(self.service().evidence_for(option_run_id="run-1")), 1)
```

Keep `test_expiry_time_alone_adjusts_nothing`, `test_a_non_authoritative_source_is_refused`, `test_an_invented_settlement_kind_refuses` unchanged (they refuse before any DB read, so an unseeded run is fine).

- [ ] **Step 3: Run to verify failure**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_expiry_policy.py -q -k Evidence`
Expected: FAIL — e.g. `KeyError: 'already_settled'` and `AssertionError: SettlementRefusal not raised`.

- [ ] **Step 4: Implement `SettlementRefusal` reason codes**

Replace `expiry_policy.py:74-82`:

```python
class SettlementRefusal(Exception):
    reason_code = "SETTLEMENT_EVIDENCE_REQUIRED"

    def __init__(
        self,
        detail: Optional[Mapping[str, Any]] = None,
        reason_code: Optional[str] = None,
    ) -> None:
        if reason_code:
            self.reason_code = str(reason_code)
        self.detail = dict(detail or {})
        super().__init__(self.reason_code)

    def as_detail(self) -> Dict[str, Any]:
        return {"rejection_reason": self.reason_code, **self.detail}
```

- [ ] **Step 5: Implement the ownership helpers (module level, above `class OptionSettlementService`)**

```python
def _run_owner_accounts(db: Any, option_run_id: str) -> set:
    """Every account that holds an ownership record for ``option_run_id``."""
    from sqlalchemy import text as _text

    rows = db.execute(
        _text(
            """
            SELECT account_id FROM public.strategy_plan_option_runs
            WHERE option_run_id = :rid
            UNION
            SELECT account_id FROM public.option_protection_owners
            WHERE option_run_id = :rid
            """
        ),
        {"rid": str(option_run_id)},
    ).fetchall()
    return {str(row[0]) for row in rows}


def _owned_option_run_ids(
    db: Any, *, account_id: str, strategy_id: str, execution_environment: str
) -> set:
    """The option runs one strategy owns in one environment of one account."""
    from sqlalchemy import text as _text

    params = {
        "acct": str(account_id),
        "sid": str(strategy_id),
        "env": str(execution_environment),
    }
    rows = db.execute(
        _text(
            """
            SELECT option_run_id FROM public.strategy_plan_option_runs
            WHERE account_id = :acct AND strategy_id = :sid
              AND execution_environment = :env
            UNION
            SELECT option_run_id FROM public.option_protection_owners
            WHERE account_id = :acct AND strategy_id = :sid
              AND execution_environment = :env
            """
        ),
        params,
    ).fetchall()
    return {str(row[0]) for row in rows}
```

- [ ] **Step 6: Rewrite `settle()`**

Replace the whole `settle` method (`expiry_policy.py:242-282`) with:

```python
    def settle(
        self,
        *,
        account_id: str,
        option_run_id: str,
        structure_digest: str,
        settlement_kind: str = "cash",
        evidence_source: Optional[str] = None,
        evidence_ref: Optional[Mapping[str, Any]] = None,
        recorded_by: str = "",
        adjustment_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Settle ONE owned run — only with evidence, in one transaction.

        Locks the run, proves the account owns it and the digest is the held
        shape, moves it to ``settled`` by compare-and-set, records the evidence
        and releases its protection owner. Any refusal leaves nothing behind.
        """
        import json
        import uuid

        from sqlalchemy import text as _text

        from backend.options.execution.lifecycle import SETTLEABLE_STATUSES
        from backend.options.protection.ownership import OptionProtectionOwnerStore

        _ = now
        if not evidence_source:
            raise SettlementRefusal(
                {
                    "option_run_id": option_run_id,
                    "message": (
                        "Settlement requires recorded evidence from an authoritative "
                        "source; expiry time alone adjusts nothing"
                    ),
                }
            )
        self._validate_source(settlement_kind=settlement_kind, evidence_source=evidence_source)

        with self.session_factory() as session:
            dialect = str(getattr(getattr(session.get_bind(), "dialect", None), "name", "") or "")
            lock = "" if dialect == "sqlite" else " FOR UPDATE"
            row = session.execute(
                _text(
                    "SELECT status, metadata FROM public.option_run_states "
                    "WHERE strategy_run_id = :rid" + lock
                ),
                {"rid": str(option_run_id)},
            ).first()
            if row is None:
                raise SettlementRefusal({"option_run_id": option_run_id}, "SETTLEMENT_RUN_UNKNOWN")
            status = str(row[0] or "")
            metadata = row[1]
            if isinstance(metadata, (str, bytes)):
                metadata = json.loads(metadata or "{}")
            metadata = dict(metadata or {})

            owners = _run_owner_accounts(session, option_run_id)
            if not owners:
                raise SettlementRefusal({"option_run_id": option_run_id}, "SETTLEMENT_RUN_UNOWNED")
            if str(account_id) not in owners:
                raise SettlementRefusal(
                    {"option_run_id": option_run_id, "account_id": str(account_id)},
                    "SETTLEMENT_ACCOUNT_MISMATCH",
                )
            held_digest = str(metadata.get("structure_digest") or "")
            if held_digest and held_digest != str(structure_digest):
                raise SettlementRefusal(
                    {"option_run_id": option_run_id, "held_digest": held_digest,
                     "structure_digest": str(structure_digest)},
                    "SETTLEMENT_DIGEST_MISMATCH",
                )

            if status == "settled":
                latest = session.execute(
                    select(OptionSettlementEvidence)
                    .where(OptionSettlementEvidence.option_run_id == str(option_run_id))
                    .order_by(OptionSettlementEvidence.created_at.desc())
                ).scalars().first()
                session.rollback()
                return {
                    "settled": True,
                    "run_state": "settled",
                    "evidence": self._view(latest) if latest is not None else None,
                    "already_settled": True,
                }
            if status not in SETTLEABLE_STATUSES:
                raise SettlementRefusal(
                    {"option_run_id": option_run_id, "status": status,
                     "settleable": list(SETTLEABLE_STATUSES)},
                    "SETTLEMENT_RUN_NOT_SETTLEABLE",
                )

            placeholders = ", ".join(f":s{i}" for i in range(len(SETTLEABLE_STATUSES)))
            moved = session.execute(
                _text(
                    "UPDATE public.option_run_states SET status = 'settled', "
                    "pending_legs = '[]', updated_at = CURRENT_TIMESTAMP "
                    f"WHERE strategy_run_id = :rid AND status IN ({placeholders})"
                ),
                {"rid": str(option_run_id),
                 **{f"s{i}": value for i, value in enumerate(SETTLEABLE_STATUSES)}},
            ).rowcount
            if moved != 1:
                session.rollback()
                raise SettlementRefusal(
                    {"option_run_id": option_run_id, "status": status},
                    "SETTLEMENT_RUN_NOT_SETTLEABLE",
                )

            evidence = OptionSettlementEvidence(
                id=str(uuid.uuid4()),
                account_id=str(account_id),
                option_run_id=str(option_run_id),
                structure_digest=str(structure_digest),
                settlement_kind=str(settlement_kind),
                evidence_source=str(evidence_source),
                evidence_ref=dict(evidence_ref or {}),
                recorded_by=str(recorded_by),
                adjustment_id=adjustment_id,
            )
            session.add(evidence)
            session.flush()
            view = self._view(evidence)
            OptionProtectionOwnerStore(session_factory=self.session_factory).release(
                str(option_run_id), db=session
            )
            session.commit()
        return {"settled": True, "run_state": "settled", "evidence": view, "already_settled": False}
```

Add the `select` import at the top of `expiry_policy.py` if it is not already there: `from sqlalchemy import select`. (Note `evidence_for` imports it locally; a module-level import is fine too.)

On PostgreSQL `pending_legs` is `jsonb`: `SET pending_legs = '[]'` is a valid jsonb literal. Keep it as written.

- [ ] **Step 7: Split validation out of `record_evidence()`**

Add this method to `OptionSettlementService` and make `record_evidence()` call it in place of its two inline `if` checks (the error payloads stay exactly as they are today):

```python
    @staticmethod
    def _validate_source(*, settlement_kind: str, evidence_source: str) -> None:
        if str(settlement_kind) not in SETTLEMENT_KINDS:
            raise SettlementRefusal({"settlement_kind": str(settlement_kind)})
        if str(evidence_source) not in AUTHORITATIVE_SOURCES:
            raise SettlementRefusal(
                {
                    "evidence_source": str(evidence_source),
                    "authoritative": list(AUTHORITATIVE_SOURCES),
                    "message": (
                        "Settlement requires an authoritative source; a position that "
                        "is merely not visible is not evidence that it was settled"
                    ),
                }
            )
```

`record_evidence()` otherwise stays (it is the low-level append for evidence that does not end a run).

- [ ] **Step 8: Run to verify pass**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_expiry_policy.py -q -k Evidence`
Expected: PASS for every settle test. The adapter tests (`test_the_adapter_*`) may now fail — Task 3 fixes them.

If `OptionProtectionOwnerStore.release` raises in SQLite because of its own SQL, run it once in a Python shell against the test DB to see the error, and report it — do not wrap it in `try/except`.

- [ ] **Step 9: Fix the API test that settled an unseeded run**

In `tests/api/test_strategy_owner_and_binding.py:2226-2231` replace the `OptionSettlementService(...).settle(...)` call with a direct evidence append (the test is about the READ route):

```python
        OptionSettlementService(session_factory=self.factory).record_evidence(
            account_id="kite:paper", option_run_id="run-opt-1", structure_digest="digest-1",
            settlement_kind="cash", evidence_source="contract_note",
            evidence_ref={"note_id": "CN-7"}, recorded_by="app:admin",
        )
```

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api/test_strategy_owner_and_binding.py -q -k settlement`
Expected: PASS (this file has known pre-existing failures elsewhere; only the `-k settlement` selection matters).

- [ ] **Step 10: Checkpoint** — `git status`.

---

### Task 3: Scoped barrier axis in the barrier's own vocabulary

**Files:**
- Modify: `backend/options/protection/expiry_policy.py` — replace `option_settlement_axes` (`:313-365`)
- Test: `tests/options/test_expiry_policy.py` (the three `test_the_adapter_*` tests)

**Interfaces:**
- Consumes: `_owned_option_run_ids` (Task 2), `TERMINAL_RUN_STATUSES` from `backend/options/protection/ownership.py:50` (`{"exited", "settled"}`).
- Produces: `option_settlement_axes(...) -> list[dict]` with exactly one dict `{"name": "domain:option_settlement", "state": "satisfied"|"failed"|"unknown", "detail": {...}}` when `db` is given; `[]` when `db is None`.

- [ ] **Step 1: Replace the adapter tests (failing)**

Replace `test_the_adapter_reports_settled_once_evidence_exists` and `test_the_adapter_reports_unsettled_when_there_is_no_evidence` with (keep `test_the_adapter_reports_settled_only_with_evidence` — the `db=None` → `[]` case — unchanged):

```python
    def _axes(self, strategy_id="stg-A", environment="paper"):
        from backend.options.protection.expiry_policy import option_settlement_axes

        with self.factory() as session:
            return option_settlement_axes(
                account_id="kite:A", strategy_id=strategy_id,
                execution_environment=environment, db=session,
            )

    def test_a_strategy_without_option_runs_is_satisfied(self):
        (axis,) = self._axes()
        self.assertEqual(axis["state"], "satisfied")
        self.assertEqual(axis["detail"]["reason"], "no_option_runs")

    def test_an_open_run_fails_the_axis(self):
        self.seed_run("run-open", status="entered")
        (axis,) = self._axes()
        self.assertEqual(axis["state"], "failed")
        self.assertEqual(axis["detail"]["reason"], "option_runs_open")
        self.assertEqual(axis["detail"]["open_runs"], ["run-open"])

    def test_settled_and_exited_runs_satisfy_the_axis(self):
        self.seed_run("run-9")
        self.seed_run("run-10", status="exited")
        self.service().settle(
            account_id="kite:A", option_run_id="run-9", structure_digest="d-1",
            evidence_source="broker_ledger", evidence_ref={"ledger": "L-1"},
            recorded_by="app:owner",
        )
        (axis,) = self._axes()
        self.assertEqual(axis["state"], "satisfied")
        self.assertEqual(axis["detail"]["settled_runs"], ["run-9"])

    def test_the_axis_is_scoped_to_the_strategy_and_environment(self):
        self.seed_run("run-other", strategy_id="stg-B")
        self.seed_run("run-live", environment="live")
        (axis,) = self._axes()
        self.assertEqual(axis["state"], "satisfied")
        self.assertEqual(axis["detail"]["reason"], "no_option_runs")

    def test_the_axis_uses_the_barrier_vocabulary(self):
        """_make_axis turns anything else into unknown; the rollup must see the real state."""
        from backend.strategies.settlement import _make_axis, _rollup

        self.seed_run("run-10", status="exited")
        (axis,) = self._axes()
        made = _make_axis(axis["name"], axis["state"], axis["detail"])
        self.assertEqual(made["state"], "satisfied")
        self.assertEqual(_rollup([made]), "settled")
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_expiry_policy.py -q -k adapter_or_axis`
(use `-k "axis or adapter or option_runs"`)
Expected: FAIL — states are `settled`/`unsettled`.

- [ ] **Step 3: Implement**

Replace the whole `option_settlement_axes` function with:

```python
def option_settlement_axes(
    *, account_id: str, strategy_id: str, execution_environment: str, db: Any = None
) -> List[Dict[str, Any]]:
    """What the option domain contributes to ONE strategy book's settlement.

    Scoped to the strategy's own option runs in this environment. Answers in
    the barrier's vocabulary (``satisfied``/``failed``/``unknown``): every run
    terminal (``exited`` or ``settled``) is satisfied; any held run fails the
    axis, because expiry time is not evidence; an unreadable answer is unknown
    and never releases.
    """
    from sqlalchemy import text as _text

    from backend.options.protection.ownership import TERMINAL_RUN_STATUSES

    name = "domain:option_settlement"
    if db is None:
        return []
    try:
        run_ids = _owned_option_run_ids(
            db,
            account_id=account_id,
            strategy_id=strategy_id,
            execution_environment=execution_environment,
        )
        statuses: Dict[str, str] = {}
        for run_id in sorted(run_ids):
            status = db.execute(
                _text(
                    "SELECT status FROM public.option_run_states "
                    "WHERE strategy_run_id = :rid"
                ),
                {"rid": run_id},
            ).scalar()
            if status is not None:
                statuses[run_id] = str(status)
    except Exception:  # noqa: BLE001 - unreadable evidence is unknown, never settled
        return [{"name": name, "state": "unknown", "detail": {"reason": "option_runs_unreadable"}}]

    if not run_ids:
        return [{"name": name, "state": "satisfied", "detail": {"reason": "no_option_runs"}}]
    open_runs = sorted(r for r, s in statuses.items() if s not in TERMINAL_RUN_STATUSES)
    if open_runs:
        return [{"name": name, "state": "failed",
                 "detail": {"reason": "option_runs_open", "open_runs": open_runs}}]
    missing = sorted(r for r in run_ids if r not in statuses)
    if missing:
        return [{"name": name, "state": "unknown",
                 "detail": {"reason": "option_run_state_missing", "option_runs": missing}}]
    return [{
        "name": name,
        "state": "satisfied",
        "detail": {
            "option_runs": sorted(run_ids),
            "settled_runs": sorted(r for r, s in statuses.items() if s == "settled"),
        },
    }]
```

- [ ] **Step 4: Run to verify pass**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options/test_expiry_policy.py -q`
Expected: all PASS.

- [ ] **Step 5: Checkpoint** — `git status`.

---

### Task 4: PostgreSQL walkthrough updated to the validated path

**Files:**
- Modify: `tests/integration/test_options_structures_postgres.py` (tests calling `.settle(` at ~193, ~345, ~366 and the axes asserts at ~356-378)

**Interfaces:**
- Consumes: Tasks 2-3 behaviour. Helper `_exec(sf, sql)` already exists in this file.

- [ ] **Step 1: Add a seeding helper to the file's `_PgTestCase`**

```python
    def seed_run(self, sf, run_id, *, digest, status="entered", account_id="kite:A",
                 strategy_id="stg-A", environment="paper"):
        _exec(sf, f"INSERT INTO public.strategies (id, owner_id, name, account_scope) "
                  f"VALUES ('{strategy_id}', 'app:o', '{strategy_id}', '{account_id}') "
                  f"ON CONFLICT DO NOTHING")
        _exec(sf, f"INSERT INTO public.option_run_states (strategy_run_id, strategy_name, product, "
                  f"status, metadata) VALUES ('{run_id}', 'n', 'NRML', '{status}', "
                  f"'{{\"structure_digest\": \"{digest}\"}}'::jsonb)")
        _exec(sf, f"INSERT INTO public.option_protection_owners (option_run_id, strategy_id, "
                  f"account_id, execution_environment, policy_version, policy, state) "
                  f"VALUES ('{run_id}', '{strategy_id}', '{account_id}', '{environment}', "
                  f"'v1', '{{}}'::jsonb, 'released')")
```

If an INSERT fails on a NOT NULL column you did not list, add that column with the column's obvious neutral value (read `backend/schema.sql` for `option_protection_owners`) and report it.

- [ ] **Step 2: Update the tests**

- Before every `.settle(` call in this file, add `self.seed_run(sf, "<same run id>", digest="<same structure_digest>")`.
- In `TestWalkthroughSix.test_cash_settlement_with_evidence_settles_the_run`: change `assert axes[0]["state"] == "settled"` to `assert axes[0]["state"] == "satisfied"` and add `assert _scalar(sf, "SELECT status FROM public.option_run_states WHERE strategy_run_id='run-w6'") == "settled"`.
- In `test_expiry_time_alone_adjusts_nothing`: seed `run-w6` BEFORE the refused settle, then change `assert axes[0]["state"] == "unsettled"` to `assert axes[0]["state"] == "failed"` (the held run is open).
- `test_evidence_is_append_only`: seed then settle; unchanged otherwise.

- [ ] **Step 3: Run**

```bash
ADMISSION_PG_URL=postgresql://postgres:testonly@127.0.0.1:15433/kite_test \
RECONCILIATION_PG_ADMIN=postgresql://postgres:testonly@127.0.0.1:15433/postgres \
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/integration/test_options_structures_postgres.py -q
```
Expected: all PASS (read the file header for the exact env var it needs; use the same host/port).

- [ ] **Step 4: Checkpoint** — `git status`.

---

### Task 5: Never drop a released leg generation

**Files:**
- Modify: `backend/strategies/execution.py:2406-2421`
- Test: `tests/strategies/test_execution.py` (append a new small test class at the end of the file)

**Interfaces:**
- Produces: `append_structure_generation(history: Iterable[Mapping], entry: Mapping) -> list[dict]` (module level in `backend/strategies/execution.py`).

- [ ] **Step 1: Failing test**

```python
class StructureGenerationHistoryTests(unittest.TestCase):
    def test_every_released_generation_is_kept(self):
        from backend.strategies.execution import append_structure_generation

        history: list = []
        for generation in range(1, 13):
            history = append_structure_generation(
                history, {"generation": generation, "structure_digest": f"d{generation}", "legs": []}
            )
        self.assertEqual([row["generation"] for row in history], list(range(1, 13)))
```

(If `unittest` is not imported at the top of `tests/strategies/test_execution.py`, add `import unittest`.)

- [ ] **Step 2: Run to verify failure**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_execution.py -q -k StructureGenerationHistory`
Expected: FAIL — `ImportError: cannot import name 'append_structure_generation'`.

- [ ] **Step 3: Implement**

Add at module level in `backend/strategies/execution.py` (next to the other module-level helpers, above the executor class):

```python
def append_structure_generation(
    history: Iterable[Mapping[str, Any]], entry: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Append a released leg generation, keeping every earlier one.

    A released generation's fills keep their old leg ids for the life of the
    run, so dropping its record would make those trades unattributable
    (staged exit, repair and owner exit all read this history).
    """
    return [dict(row) for row in (history or [])] + [dict(entry)]
```

Ensure `Iterable`, `Mapping`, `List`, `Dict`, `Any` are imported from `typing` (add any missing ones to the existing `from typing import ...` line).

Then replace lines 2406-2412 and 2421:

```python
                    history = append_structure_generation(
                        metadata.get("structure_generation_history") or [],
                        {
                            "generation": generation,
                            "structure_digest": previous_digest,
                            "legs": previous_legs,
                        },
                    )
```
and
```python
                    metadata["structure_generation_history"] = history
```

Also update the comment at ~2392 "with the previous generation's legs kept (bounded)" → "with every previous generation's legs kept".

- [ ] **Step 4: Run**

Run: `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_execution.py -q -k "StructureGenerationHistory or adjust"`
Expected: PASS.

- [ ] **Step 5: Checkpoint** — `git status`.

---

### Task 6: Docs + final targeted run

**Files:**
- Modify: `documents/kite-algo-platform-reference.md`

- [ ] **Step 1:** `grep -n -i "settle" documents/kite-algo-platform-reference.md`. For each line describing option settlement or the option settlement axis, update it to: settlement validates run ownership/digest and moves the run to `settled` in one transaction; the option axis is scoped to strategy+environment and reports satisfied/failed/unknown. For generation history, `grep -n -i "generation" ...` and replace any "last 10"/"bounded" wording with "all generations kept". Change nothing else.

- [ ] **Step 2: Final targeted run (high-risk area → whole options dir + touched files)**

```bash
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/options -q
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_execution.py -q
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api/test_strategy_owner_and_binding.py -q -k settlement
```
Expected: PASS. Report any failure that also fails on `development` (check with `git stash; <cmd>; git stash pop`) as pre-existing, with the test id and error line.

- [ ] **Step 3: Final report** per AGENTS.md (changed files, tests run with counts, not done/open questions).
