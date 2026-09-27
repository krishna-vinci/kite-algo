# Auto-Journal ⇄ Hosted Strategies Alignment Implementation Plan

> **For agentic workers:** Execute task-by-task, in order. Steps use checkbox (`- [ ]`) syntax. Do not load brainstorming or other workflow skills. If a step's code does not fit the file you see, stop and report the mismatch instead of improvising.

**Goal:** Hosted (deployable) strategies feed the auto-journal the same way the old algo runtime did: paper fills land in a journal run, and every governed decision (request raised, auto-queued/refused, owner approve/reject, protection exit) is written as a `JournalDecisionEvent` on the same journal run the fills use.

**Architecture:** A new module `backend/strategies/journal_bridge.py` owns (a) resolving/creating the journal run for `(environment, strategy_run_id, account_id)` using the keys the fill projectors already use, and (b) a process-wide recorder hook. Services call `journal_bridge.record_decision(...)` AFTER their own commit; the default recorder is a no-op, so tests and non-app processes never touch a real DB. `combined_lifespan` installs the real recorder in the app process. Journaling is best-effort: it can never fail or block execution.

**Tech Stack:** Python 3.11, SQLAlchemy, pydantic journal models, pytest/unittest.

**Spec:** Orchestrator review 2026-09-27 (journal philosophy: the journal writes itself from execution truth; humans only reflect).

## Global Constraints

- Worktree `/home/krishna/kite-algo-worktrees/fx-journal`, branch `codex/fx-journal`. Read `AGENTS.md` first.
- Python: `/home/krishna/kite-algo/.venv/bin/python -m pytest ...` from the worktree root.
- Never touch `.env*`. Fake broker only. No migrations. Do NOT commit (orchestrator commits). "Checkpoint" = run `git status` and continue.
- Journal writes must never raise into the caller and never run inside the caller's DB transaction.

## Decisions (fixed)

1. **Journal rules are NOT enforced in admission.** `JournalRule` has no machine-checkable condition (only title/description/metadata; `backend/journaling/models.py:816-827`). Enforcing them would be guessing. Out of scope.
2. Journal-run keys (must match the fill projectors so decisions and fills share one run):
   - live: source link `('live_order', strategy_run_id)` — created via `repository.ensure_live_strategy_run_for_intent(intent={"account_id", "strategy_run_id"})` (`backend/journaling/repositories/legacy_repository.py:658`).
   - paper: `journal_service.ensure_paper_strategy_run(attribution={"strategy_run_id", "account_ref", "execution_mode": "paper"})` (`backend/journaling/services/legacy_service.py:2296`).
   - `dry_run` or empty ids: no journal write.
3. Decision mapping (`DecisionType` / `DecisionActorType` values from `backend/journaling/models.py:115-128`):

| event | decision_type | actor_type |
|---|---|---|
| `request_awaiting_approval` | `algo_trigger` | `algo` |
| `request_auto_queued` | `algo_trigger` | `system` |
| `request_refused` | `algo_trigger` | `system` |
| `request_approved` | `review` | `user` |
| `request_rejected` | `review` | `user` |
| `protection_exit` | `exit` | `system` |

## File Structure

- Create `backend/strategies/journal_bridge.py` — run resolution, recorder hook, `record_decision`, `request_event_for(view)`.
- Modify `backend/strategies/execution.py:3151-3159` — paper attribution gains `account_ref`.
- Modify `backend/strategies/execution_requests.py` — call the bridge after commit in `create_for_job` (new row path, ~628), `approve` (three commit points ~733/742/761), `reject` (~804).
- Modify `backend/api/services/protection_runtime.py:1246-1256` — record `protection_exit` after `exit_control_strategy` returns.
- Modify `backend/app/bootstrap.py` `combined_lifespan` (~337) — install the real recorder.
- Tests: create `tests/strategies/test_journal_bridge.py`; extend `tests/api/test_hosted_execution_requests.py`, `tests/strategies/test_execution.py`.
- Docs: `documents/kite-algo-platform-reference.md` journal section.

---

### Task 1: Hosted paper fills reach the journal

**Files:**
- Modify: `backend/strategies/execution.py:3151-3159`
- Test: `tests/strategies/test_execution.py` (`ExecutorSubmissionTests.test_fill_submits_through_the_paper_runtime_and_consumes_the_reservation`, ~1199-1227)

- [ ] **Step 1: Failing assertion.** In that test, after `self.assertEqual(order.metadata["step_no"], 1)` add:

```python
        # The journal keys a paper strategy run on (strategy_run_id, account):
        # without the account the fill is never journaled.
        self.assertEqual(order.metadata["account_ref"], ACCOUNT)
```

- [ ] **Step 2: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_execution.py -q -k test_fill_submits_through_the_paper_runtime` → Expected FAIL `KeyError: 'account_ref'`.

  If it fails instead because `order.metadata` does not carry attribution keys at all, STOP and report (the other metadata asserts in that test show it does).

- [ ] **Step 3: Implement.** In the `attribution = {...}` dict at `execution.py:3151`, add one key after `"strategy_id"`:

```python
            "account_ref": str(binding.get("account_id") or plan.get("account_id") or ""),
```

(`binding` is the run-binding dict from `run_binding`; it carries `account_id`. If `binding` is not a Mapping at that point, use `str(plan.get("account_id") or "")` only.)

- [ ] **Step 4: Run** the same command → PASS. Then `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_execution.py -q` → PASS.

- [ ] **Step 5: Checkpoint.**

---

### Task 2: `journal_bridge` module

**Files:**
- Create: `backend/strategies/journal_bridge.py`
- Test: `tests/strategies/test_journal_bridge.py`

**Interfaces:**
- Produces:
  - `DECISION_KINDS: dict[str, tuple[str, str]]` (event → (decision_type, actor_type)).
  - `set_recorder(recorder: Optional[Callable[..., None]]) -> None` (None resets to no-op).
  - `record_decision(*, event: str, environment: str, strategy_run_id: str, account_id: str, summary: str, context: Mapping[str, Any]) -> None` — never raises.
  - `request_event_for(view: Mapping[str, Any]) -> Optional[str]` — maps an execution-request view's `status`/`decision_kind` to an event name.
  - `JournalDecisionRecorder(journal_service=None)` — callable with the same kwargs as `record_decision` (minus nothing); the production recorder.

- [ ] **Step 1: Failing tests** — create `tests/strategies/test_journal_bridge.py`:

```python
"""The hosted-strategy -> auto-journal bridge."""

from __future__ import annotations

import unittest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.strategies import journal_bridge  # noqa: E402


class _Repo:
    def __init__(self):
        self.live_intents = []

    def ensure_live_strategy_run_for_intent(self, *, intent):
        self.live_intents.append(dict(intent))
        return "jr-live"


class _Journal:
    def __init__(self):
        self.repository = _Repo()
        self.paper = []
        self.events = []

    def ensure_paper_strategy_run(self, *, attribution):
        self.paper.append(dict(attribution))
        return "jr-paper"

    def append_decision_event(self, run_id, event):
        self.events.append((run_id, event))
        return len(self.events)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.journal = _Journal()
        self.recorder = journal_bridge.JournalDecisionRecorder(journal_service=self.journal)

    def test_live_decision_binds_to_the_live_order_run(self):
        self.recorder(event="request_approved", environment="live", strategy_run_id="run-1",
                      account_id="kite:A", summary="approved", context={"request_id": "r1"})
        self.assertEqual(self.journal.repository.live_intents,
                         [{"account_id": "kite:A", "strategy_run_id": "run-1"}])
        run_id, event = self.journal.events[0]
        self.assertEqual(run_id, "jr-live")
        self.assertEqual(event.decision_type, "review")
        self.assertEqual(event.actor_type, "user")
        self.assertEqual(event.context["request_id"], "r1")
        self.assertEqual(event.context["event"], "request_approved")

    def test_paper_decision_binds_to_the_paper_strategy_run(self):
        self.recorder(event="request_awaiting_approval", environment="paper",
                      strategy_run_id="run-2", account_id="kite:P", summary="s", context={})
        self.assertEqual(self.journal.paper,
                         [{"strategy_run_id": "run-2", "account_ref": "kite:P", "execution_mode": "paper"}])
        run_id, event = self.journal.events[0]
        self.assertEqual(run_id, "jr-paper")
        self.assertEqual((event.decision_type, event.actor_type), ("algo_trigger", "algo"))

    def test_dry_run_and_missing_ids_write_nothing(self):
        self.recorder(event="request_approved", environment="dry_run", strategy_run_id="r",
                      account_id="a", summary="s", context={})
        self.recorder(event="request_approved", environment="live", strategy_run_id="",
                      account_id="a", summary="s", context={})
        self.assertEqual(self.journal.events, [])


class HookTests(unittest.TestCase):
    def tearDown(self):
        journal_bridge.set_recorder(None)

    def test_default_recorder_is_a_no_op(self):
        journal_bridge.set_recorder(None)
        journal_bridge.record_decision(event="request_approved", environment="live",
                                       strategy_run_id="r", account_id="a", summary="s", context={})

    def test_a_failing_recorder_never_raises(self):
        def boom(**_kwargs):
            raise RuntimeError("journal down")

        journal_bridge.set_recorder(boom)
        journal_bridge.record_decision(event="request_approved", environment="live",
                                       strategy_run_id="r", account_id="a", summary="s", context={})

    def test_request_event_mapping(self):
        f = journal_bridge.request_event_for
        self.assertEqual(f({"status": "awaiting_approval"}), "request_awaiting_approval")
        self.assertEqual(f({"status": "queued", "decision_kind": "automatic"}), "request_auto_queued")
        self.assertEqual(f({"status": "queued", "decision_kind": "manual"}), "request_approved")
        self.assertEqual(f({"status": "rejected"}), "request_rejected")
        self.assertEqual(f({"status": "refused"}), "request_refused")
        self.assertIsNone(f({"status": "dispatching"}))
```

- [ ] **Step 2: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_journal_bridge.py -q` → Expected FAIL `ImportError: cannot import name 'journal_bridge'`.

- [ ] **Step 3: Implement** — create `backend/strategies/journal_bridge.py`:

```python
"""Hosted strategies -> auto-journal: decisions land on the fills' journal run.

The journal writes itself from execution truth. Fills already reach it (live via
``live_order_intents`` -> ``LiveJournalProjector``, paper via the paper runtime's
attribution). This module adds the WHY: each governed decision on a hosted
execution request is appended as a ``JournalDecisionEvent`` on the SAME journal
run, found by the same source keys the fill projectors use.

Best effort by design: a journal outage must never fail or delay execution, so
``record_decision`` swallows every error, and services call it only after their
own commit. The default recorder is a no-op; the app process installs the real
one at startup (``backend/app/bootstrap.py``).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: event -> (DecisionType value, DecisionActorType value)
DECISION_KINDS: Dict[str, tuple] = {
    "request_awaiting_approval": ("algo_trigger", "algo"),
    "request_auto_queued": ("algo_trigger", "system"),
    "request_refused": ("algo_trigger", "system"),
    "request_approved": ("review", "user"),
    "request_rejected": ("review", "user"),
    "protection_exit": ("exit", "system"),
}

_JOURNALED_ENVIRONMENTS = ("live", "paper")


def _no_op(**_kwargs: Any) -> None:
    return None


_recorder: Callable[..., None] = _no_op


def set_recorder(recorder: Optional[Callable[..., None]]) -> None:
    """Install the process's recorder; ``None`` restores the no-op."""
    global _recorder
    _recorder = recorder or _no_op


def record_decision(
    *,
    event: str,
    environment: str,
    strategy_run_id: str,
    account_id: str,
    summary: str,
    context: Mapping[str, Any],
) -> None:
    """Append one decision. Never raises."""
    try:
        _recorder(
            event=event,
            environment=environment,
            strategy_run_id=strategy_run_id,
            account_id=account_id,
            summary=summary,
            context=dict(context or {}),
        )
    except Exception:  # noqa: BLE001 - the journal must never break execution
        logger.warning("journal decision not recorded", extra={"event": event}, exc_info=True)


def request_event_for(view: Mapping[str, Any]) -> Optional[str]:
    """The decision an execution-request state represents, if any."""
    status = str(view.get("status") or "")
    if status == "awaiting_approval":
        return "request_awaiting_approval"
    if status == "queued":
        kind = str(view.get("decision_kind") or "")
        return "request_auto_queued" if kind == "automatic" else "request_approved"
    if status == "rejected":
        return "request_rejected"
    if status == "refused":
        return "request_refused"
    return None


def record_request_decision(view: Mapping[str, Any], *, actor: str = "") -> None:
    """Journal an execution request's current decision (after its commit)."""
    event = request_event_for(view)
    if event is None:
        return
    record_decision(
        event=event,
        environment=str(view.get("execution_environment") or ""),
        strategy_run_id=str(view.get("strategy_run_id") or ""),
        account_id=str(view.get("account_id") or ""),
        summary=f"{event}: plan {view.get('plan_id')}",
        context={
            "request_id": view.get("request_id"),
            "strategy_id": view.get("strategy_id"),
            "plan_id": view.get("plan_id"),
            "status": view.get("status"),
            "refusal_code": view.get("refusal_code"),
            "decision_kind": view.get("decision_kind"),
            "decision_actor": view.get("decision_actor") or actor or None,
            "grant_id": view.get("grant_id"),
            "authorization_mode": view.get("authorization_mode"),
        },
    )


class JournalDecisionRecorder:
    """The production recorder: resolve the fills' journal run, append the event."""

    def __init__(self, journal_service: Any = None) -> None:
        self._journal_service = journal_service

    def _service(self) -> Any:
        if self._journal_service is None:
            from backend.journaling.service import JournalService

            self._journal_service = JournalService()
        return self._journal_service

    def _run_id(self, *, environment: str, strategy_run_id: str, account_id: str) -> Optional[str]:
        service = self._service()
        if environment == "live":
            return service.repository.ensure_live_strategy_run_for_intent(
                intent={"account_id": account_id, "strategy_run_id": strategy_run_id}
            )
        return service.ensure_paper_strategy_run(
            attribution={
                "strategy_run_id": strategy_run_id,
                "account_ref": account_id,
                "execution_mode": "paper",
            }
        )

    def __call__(
        self,
        *,
        event: str,
        environment: str,
        strategy_run_id: str,
        account_id: str,
        summary: str,
        context: Mapping[str, Any],
    ) -> None:
        if environment not in _JOURNALED_ENVIRONMENTS or not strategy_run_id or not account_id:
            return
        kinds = DECISION_KINDS.get(event)
        if kinds is None:
            return
        from backend.journaling.models import JournalDecisionEvent

        run_id = self._run_id(
            environment=environment, strategy_run_id=strategy_run_id, account_id=account_id
        )
        if not run_id:
            return
        decision_type, actor_type = kinds
        self._service().append_decision_event(
            str(run_id),
            JournalDecisionEvent(
                run_id=str(run_id),
                decision_type=decision_type,
                actor_type=actor_type,
                summary=summary,
                context={**dict(context or {}), "event": event, "source": "hosted_strategy"},
            ),
        )
```

Check `backend/journaling/service.py` exports `JournalService` (it does: `backend/journaling/__init__.py` imports `from .service import JournalService`). Check `JournalService` has a `.repository` attribute: `grep -n "self.repository" backend/journaling/services/legacy_service.py | head -3`. If the attribute is named differently, use that name in `_run_id` and in the test's `_Journal` fake, and report it.

- [ ] **Step 4: Run** → PASS (7 tests).

- [ ] **Step 5: Checkpoint.**

---

### Task 3: Execution requests journal their decisions

**Files:**
- Modify: `backend/strategies/execution_requests.py`
- Test: `tests/api/test_hosted_execution_requests.py`

**Interfaces:**
- Consumes: `journal_bridge.record_request_decision(view, actor=...)` (Task 2).

- [ ] **Step 1: Failing tests.** Append to `tests/api/test_hosted_execution_requests.py`:

```python
@pytest.fixture
def journal_calls():
    from backend.strategies import journal_bridge

    calls = []
    journal_bridge.set_recorder(lambda **kwargs: calls.append(kwargs))
    try:
        yield calls
    finally:
        journal_bridge.set_recorder(None)


def test_request_and_owner_approval_are_journaled(world, journal_calls):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-journal-1", now=NOW
    )
    request_id = created["request"]["request_id"]
    world["service"].approve(
        request_id, owner_id=OWNER, strategy_id=world["strategy"].id, actor=OWNER, now=NOW
    )
    assert [call["event"] for call in journal_calls] == [
        "request_awaiting_approval",
        "request_approved",
    ]
    assert journal_calls[1]["context"]["request_id"] == request_id
    assert journal_calls[1]["strategy_run_id"] == created["request"]["strategy_run_id"]
    assert journal_calls[1]["environment"] == created["request"]["execution_environment"]


def test_owner_rejection_is_journaled(world, journal_calls):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-journal-2", now=NOW
    )
    world["service"].reject(
        created["request"]["request_id"], owner_id=OWNER, strategy_id=world["strategy"].id,
        actor=OWNER, reason="no", now=NOW,
    )
    assert [call["event"] for call in journal_calls][-1] == "request_rejected"


def test_an_idempotent_replay_is_not_journaled_twice(world, journal_calls):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    for _ in range(2):
        world["service"].create_for_job(
            job=world["job"], plan_id=plan_id, idempotency_key="exec-key-journal-3", now=NOW
        )
    assert [call["event"] for call in journal_calls] == ["request_awaiting_approval"]
```

- [ ] **Step 2: Run** `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api/test_hosted_execution_requests.py -q -k journal` → Expected FAIL (`journal_calls` empty).

- [ ] **Step 3: Implement.** Add at the top of `execution_requests.py` imports: `from backend.strategies import journal_bridge`.

  In `create_for_job`, replace the new-row return (`execution_requests.py:628-629`):

```python
            session.commit()
            view = self._view(row)
            journal_bridge.record_request_decision(view)
            return {"idempotent": False, "request": view}
```
  (The IntegrityError replay path and any earlier "existing request" replay return are NOT journaled.)

  In `approve`, at each of the three `session.commit()` + `return {"request": self._view(row), "approved": ...}` pairs (~733, ~742, ~761), change to:

```python
                session.commit()
                view = self._view(row)
                journal_bridge.record_request_decision(view, actor=str(actor))
                return {"request": view, "approved": False}
```
  (keep each site's own `approved` value and indentation.)

  In `reject` (~804):

```python
            session.commit()
            view = self._view(row)
            journal_bridge.record_request_decision(view, actor=str(actor))
            return {"request": view, "rejected": True}
```

- [ ] **Step 4: Run** `-k journal` → PASS; then the whole file `/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api/test_hosted_execution_requests.py -q` → PASS (the default recorder is a no-op, so other tests are unaffected).

- [ ] **Step 5: Checkpoint.**

---

### Task 4: Protection exits are journaled; install the recorder at startup

**Files:**
- Modify: `backend/api/services/protection_runtime.py:1246-1256` (`submit_worker_protection_exit`)
- Modify: `backend/app/bootstrap.py` (`combined_lifespan`, ~337)
- Test: `tests/strategies/test_journal_bridge.py` (append)

- [ ] **Step 1: Failing test** — append to `tests/strategies/test_journal_bridge.py`:

```python
class ProtectionExitJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        journal_bridge.set_recorder(None)

    async def test_protection_exit_is_journaled(self):
        from unittest import mock

        from backend.api.services import protection_runtime

        calls = []
        journal_bridge.set_recorder(lambda **kwargs: calls.append(kwargs))
        with mock.patch(
            "backend.api.services.control_plane.exit_control_strategy",
            new=mock.AsyncMock(return_value={"status": "exit_requested"}),
        ):
            await protection_runtime.submit_worker_protection_exit(
                object(),
                {"strategy_run_id": "run-9", "account_scope": "kite:A", "execution_mode": "live"},
                {"triggered_rule": "index_stop", "exit_idempotency_key": "k1"},
            )
        self.assertEqual(calls[0]["event"], "protection_exit")
        self.assertEqual(calls[0]["environment"], "live")
        self.assertEqual(calls[0]["context"]["triggered_rule"], "index_stop")
```

  If importing `backend.api.services.protection_runtime` needs `import backend.api.routers` first (import cycle, as in `tests/broker_api/test_daily_instruments_job.py:17`), add `import backend.api.routers  # noqa: F401` inside the test before the import.

- [ ] **Step 2: Run** `-k protection_exit` → FAIL (`IndexError`).

- [ ] **Step 3: Implement** — replace `submit_worker_protection_exit` body:

```python
async def submit_worker_protection_exit(request: Any, run: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.services.control_plane import exit_control_strategy
    from backend.strategies import journal_bridge

    triggered_rule = str(state.get("triggered_rule") or "unknown")
    result = await exit_control_strategy(
        request,
        str(run["strategy_run_id"]),
        account_scope=str(run.get("account_scope") or "default"),
        reason=f"backend_protection:{triggered_rule}",
        dry_run=False,
        idempotency_key=str(state.get("exit_idempotency_key") or "") or None,
    )
    journal_bridge.record_decision(
        event="protection_exit",
        environment=str(run.get("execution_mode") or "").lower(),
        strategy_run_id=str(run.get("strategy_run_id") or ""),
        account_id=str(run.get("account_scope") or ""),
        summary=f"protection exit: {triggered_rule}",
        context={
            "triggered_rule": triggered_rule,
            "exit_idempotency_key": state.get("exit_idempotency_key"),
            "result_status": (result or {}).get("status") if isinstance(result, dict) else None,
        },
    )
    return result
```

- [ ] **Step 4: Install the recorder.** In `backend/app/bootstrap.py` `combined_lifespan`, directly after `run_schema_migrations()` (inside the same `try`), add:

```python
        # Hosted-strategy decisions join the auto-journal in the app process.
        from backend.strategies import journal_bridge

        journal_bridge.set_recorder(journal_bridge.JournalDecisionRecorder())
```

- [ ] **Step 5: Run**

```bash
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_journal_bridge.py -q
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/api -q -k "protection" -x
```
Expected: PASS. Report any failure in the second command that also fails on `development` as pre-existing.

- [ ] **Step 6: Checkpoint.**

---

### Task 5: Docs + final targeted run

- [ ] **Step 1:** In `documents/kite-algo-platform-reference.md`, find the journal bullet (`grep -n "Journal and analytics" documents/kite-algo-platform-reference.md`) and append one sentence: "Hosted strategies: paper and live fills land on a journal run keyed by the strategy run; execution-request decisions (raised, auto-queued/refused, owner approve/reject) and protection exits are appended as decision events on that run (`backend/strategies/journal_bridge.py`). Journal rules are not enforced by admission (they carry no machine-checkable condition)."

- [ ] **Step 2: Final run**

```bash
/home/krishna/kite-algo/.venv/bin/python -m pytest tests/strategies/test_journal_bridge.py tests/strategies/test_execution.py tests/api/test_hosted_execution_requests.py tests/journaling -q
```
Expected: PASS.

- [ ] **Step 3: Final report** per AGENTS.md.
