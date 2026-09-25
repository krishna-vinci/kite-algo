"""The option-run branch of the owned-work snapshot: honest failure reporting.

The PostgreSQL suite proves scoping and truncation against the real database.
These are the cheap, deterministic guards for the two facts a strategy depends
on when a read fails or a run row is unreachable:

* a read failure reports a NAMED reason, never the driver's raw message (which
  would carry SQL text and bound parameters into a strategy's own log);
* an unreachable run row is unknown coverage, not an empty set.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.sql.elements import TextClause

from backend.strategies.execution_snapshot import OwnedWorkSnapshotService

ACCOUNT = "kite:paper-snapshot"
STRATEGY = "stg-snapshot"
ENVIRONMENT = "paper"


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Returns the edges for the ORM select and fails on the raw state read."""

    def __init__(self, edges):
        self._edges = edges

    def execute(self, statement, params=None):
        if isinstance(statement, TextClause):
            raise RuntimeError(
                "psycopg2 error: SELECT FROM public.option_run_states "
                "WHERE strategy_run_id IN (:id0) parameters: {'id0': 'opt-secret'}"
            )
        return _FakeResult(self._edges)


def _edge(plan_id: str, *, phase: str = "entry"):
    return SimpleNamespace(
        plan_id=plan_id,
        option_run_id="opt-1",
        worker_run_id=None,
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        execution_environment=ENVIRONMENT,
        phase=phase,
    )


def _service():
    service = OwnedWorkSnapshotService(session_factory=lambda: None)
    service._bound_runs = lambda *args, **kwargs: ["run-1"]  # type: ignore[assignment]
    service._plans = lambda *args, **kwargs: [  # type: ignore[assignment]
        SimpleNamespace(plan_id="plan-1", resolved_plan={})
    ]
    return service


def test_state_read_failure_reports_a_named_reason_without_sql_text():
    service = _service()
    rows, coverage = service._option_runs(
        _FakeSession([_edge("plan-1")]),
        account_id=ACCOUNT,
        strategy_id=STRATEGY,
        environment=ENVIRONMENT,
    )

    assert rows == []
    assert coverage["coverage"] == "unknown"
    assert coverage["reason"] == "option_run_state_read_failed"
    # No fragment of the driver's message is exposed to the strategy.
    assert "psycopg2" not in coverage["reason"]
    assert "public.option_run_states" not in coverage["reason"]
    assert "opt-secret" not in coverage["reason"]


def test_edge_read_failure_reports_a_named_reason_without_sql_text():
    class _FailingEdgeSession:
        def execute(self, statement, params=None):  # noqa: ANN001
            raise RuntimeError("syntax error at or near 'strategy_plan_option_runs'")

    service = _service()
    rows, coverage = service._option_runs(
        _FailingEdgeSession(),
        account_id=ACCOUNT,
        strategy_id=STRATEGY,
        environment=ENVIRONMENT,
    )

    assert rows == []
    assert coverage["coverage"] == "unknown"
    assert coverage["reason"] == "option_run_read_failed"
    assert "strategy_plan_option_runs" not in coverage["reason"]


def test_unreadable_leg_list_is_unknown_coverage_not_no_outstanding_legs():
    class _GarbledStateSession:
        def execute(self, statement, params=None):  # noqa: ANN001
            if isinstance(statement, TextClause):
                return _FakeResult(
                    [
                        {
                            "strategy_run_id": "opt-1",
                            "status": "entered",
                            "legs": "[]",
                            "completed_legs": "[]",
                            "pending_legs": "not-json",
                            "failed_legs": "[]",
                            "orders": "[]",
                        }
                    ]
                )
            return _FakeResult([_edge("plan-1")])

    rows, coverage = _service()._option_runs(
        _GarbledStateSession(),
        account_id=ACCOUNT,
        strategy_id=STRATEGY,
        environment=ENVIRONMENT,
    )

    assert rows == []
    assert coverage["coverage"] == "unknown"
    assert coverage["reason"] == "option_run_state_unreadable"


def _state_row(*, metadata=None):
    return {
        "strategy_run_id": "opt-1",
        "status": "entered",
        "legs": "[]",
        "completed_legs": "[]",
        "pending_legs": "[]",
        "failed_legs": "[]",
        "orders": "[]",
        "metadata": metadata,
    }


def _session_returning(state_row):
    class _Session:
        def execute(self, statement, params=None):  # noqa: ANN001
            if isinstance(statement, TextClause):
                return _FakeResult([state_row])
            return _FakeResult([_edge("plan-1")])

    return _Session()


def _service_with_digest(digest: str):
    service = OwnedWorkSnapshotService(session_factory=lambda: None)
    service._bound_runs = lambda *args, **kwargs: ["run-1"]  # type: ignore[assignment]
    service._plans = lambda *args, **kwargs: [  # type: ignore[assignment]
        SimpleNamespace(plan_id="plan-1", resolved_plan={"structure_digest": digest})
    ]
    return service


def test_the_runs_own_digest_wins_over_the_originating_plans():
    """After a shape-changing adjust the OPENING plan's digest is stale.

    The duplicate gate compares this row against a new plan's frozen digest, so
    reporting the originating plan's digest here would let an entry re-open the
    very structure the run holds.
    """
    rows, coverage = _service_with_digest("digest-opened")._option_runs(
        _session_returning(
            _state_row(metadata='{"structure_generation": 2, "structure_digest": "digest-held"}')
        ),
        account_id=ACCOUNT,
        strategy_id=STRATEGY,
        environment=ENVIRONMENT,
    )

    assert coverage["coverage"] == "known"
    (row,) = rows
    assert row["structure_digest"] == "digest-held"
    assert row["structure_generation"] == 2

    # A run that has never been adjusted still reports the plan that opened it.
    rows, _coverage = _service_with_digest("digest-opened")._option_runs(
        _session_returning(_state_row(metadata="{}")),
        account_id=ACCOUNT,
        strategy_id=STRATEGY,
        environment=ENVIRONMENT,
    )
    assert rows[0]["structure_digest"] == "digest-opened"
    assert rows[0]["structure_generation"] == 1
