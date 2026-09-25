"""B2.6b S1: the owner-action API for one hosted strategy.

Bounded ``httpx`` ASGI transport over a minimal app that mounts the strategies
router and the owner-actions router (no lifespan, no background tasks), in the
style of ``tests/api/test_strategy_option_runs_api.py``. The platform tables the
action readers touch are created under the established ``public.`` ATTACH
fixture, so the SQL the production readers run is the SQL these tests exercise.

Pinned properties:

- every route requires an app session, and a foreign strategy is a 404;
- a protective hedge, and any step that is not exposure-increasing entry work, is
  never cancellable - it is reported with its named reason and left alone;
- an eligible entry admits, and the proven partial fill is PRESERVED while the
  cancelled remainder is recorded with ``disposition=owner_cancelled``;
- the preview's digest is binding: evidence that moved refuses the action
  (``CANCEL_EVIDENCE_CHANGED``) instead of being raced;
- a terminal paper order admits exactly the disposition it supports and refuses a
  mismatch;
- disposing of a dead adjust submission makes the plan's own execution state
  (and the adjust takeover rule) report ``finished``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.api.routers import strategy_owner_actions as owner_actions_router  # noqa: E402
from backend.app.auth import AppUser  # noqa: E402
from backend.options.execution.models import OptionRunState  # noqa: E402
from backend.strategies import models  # noqa: F401,E402  (table registration)
from backend.strategies.attribution_models import (  # noqa: E402
    PaperOrderFillProgress,
    StrategyPlan,
    StrategyPlanExecutionEvent,
    StrategyPlanOptionRun,
    StrategyProposal,
)
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/strategies"
ACCOUNT = "kite:paper"

SHORT = "NIFTY26NOV22500CE"
HEDGE = "NIFTY26NOV21500PE"
SHORT_ID = "NSE:NIFTY26NOV22500CE"
HEDGE_ID = "NSE:NIFTY26NOV21500PE"

RUN_ID = "opt_run_1"
ENTRY_PLAN = "plan-entry"
ADJUST_PLAN = "plan-adjust"
ENTRY_ORDER = "PAPER-ENTRY-1"
ADJUST_ORDER = "PAPER-ADJUST-1"


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        # The paper runtime's own table has no ORM model in this codebase, so the
        # reader reaches it through ``public.``-qualified SQL exactly as the
        # production reader does.
        cursor.execute(
            """
            CREATE TABLE public.paper_orders (
                account_scope TEXT NOT NULL,
                order_id TEXT NOT NULL,
                instrument_token BIGINT,
                exchange TEXT,
                tradingsymbol TEXT,
                product TEXT,
                transaction_type TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                filled_quantity INTEGER NOT NULL DEFAULT 0,
                pending_quantity INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                placed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (account_scope, order_id)
            )
            """
        )
        # The settlement barrier's own reader and its dedupe predicate are
        # ``public.``-qualified in production code while the ORM writes the
        # ambient schema, so the read needs the tables to resolve here. The
        # exactly-once guarantee itself is proved on PostgreSQL (one schema).
        cursor.execute(
            """
            CREATE TABLE public.strategy_execution_barriers (
                account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                execution_environment TEXT NOT NULL,
                barrier_version INTEGER NOT NULL DEFAULT 0,
                quiet_since_version INTEGER,
                last_proof_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (account_id, strategy_id, execution_environment)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.strategy_execution_barrier_events (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                execution_environment TEXT NOT NULL,
                version INTEGER NOT NULL,
                event TEXT NOT NULL,
                ref TEXT,
                detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT
            )
            """
        )

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    """Authorize exactly the account these fixtures use (default-deny)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeRunStore:
    """The durable option-run store surface, in memory."""

    def __init__(self, runs=None):
        self._runs = dict(runs or {})
        self.transitions = []

    def get_run(self, option_run_id):
        if option_run_id not in self._runs:
            raise KeyError(option_run_id)
        return self._runs[option_run_id]

    def save_run_if_status(self, run, *, allowed_from):
        # The CAS is on the STORED run's status, which is what the caller
        # observed; the passed run carries the NEW status.
        current = self._runs.get(run.strategy_run_id)
        if current is None or str(current.status) not in {str(value) for value in allowed_from}:
            return False
        self._runs[run.strategy_run_id] = run
        self.transitions.append((run.strategy_run_id, str(run.status)))
        return True


class _FakePaperRuntime:
    """The paper cancel boundary, writing the SAME durable rows the runtime does."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.calls = []

    async def cancel_order(self, *, account_scope, paper_order_id):
        self.calls.append((str(account_scope), str(paper_order_id)))
        with self.session_factory() as session:
            session.execute(
                text(
                    "UPDATE public.paper_orders SET status = 'cancelled', "
                    "pending_quantity = 0 WHERE account_scope = :account "
                    "AND order_id = :order_id"
                ),
                {"account": str(account_scope), "order_id": str(paper_order_id)},
            )
            # A cancel zeroes the unexecuted remainder and preserves the fill.
            session.execute(
                text(
                    "UPDATE paper_order_fill_progress SET status = 'cancelled', "
                    "remaining_quantity = 0 WHERE account_scope = :account "
                    "AND paper_order_id = :order_id"
                ),
                {"account": str(account_scope), "order_id": str(paper_order_id)},
            )
            session.commit()
        return {"mode": "paper", "status": "cancelled"}


def _app(session_factory, monkeypatch, user, *, run_store=None, paper=None):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.include_router(owner_actions_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.dependency_overrides[owner_actions_router._owner_actions_db] = lambda: (
        session_factory
    )
    if run_store is not None:
        app.state.option_run_store = run_store
    if paper is not None:
        app.state.paper_runtime_service = paper
    return app


def _client(session_factory, monkeypatch, username="admin", **kwargs):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user, **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client, name="owner-actions"):
    response = await client.post(
        BASE,
        json={
            "name": name,
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": ACCOUNT,
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "exit_on_worker_stale",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["strategy_id"]


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _frozen_leg(identity, symbol, side, role, quantity, option_type):
    return {
        "instrument_id": identity,
        "tradingsymbol": symbol,
        "broker_symbol": symbol,
        "side": side,
        "role": role,
        "ratio": 1,
        "quantity": quantity,
        "signed_quantity": quantity if side == "BUY" else -quantity,
        "product": "NRML",
        "expiry": "2026-11-26",
        "option_type": option_type,
        "lot_size": 75,
        "protection_policy": None,
    }


def _resolved_plan(*, legs=None, naked=False, plan_kind="option_structure"):
    resolved = {
        "target_kind": plan_kind,
        "underlying": "NIFTY",
        "expiry": "2026-11-26",
        "product": "NRML",
        "structure_digest": "sha-entry-digest",
        "expiry_policy": "exit_before_cutoff",
        "legs": legs
        if legs is not None
        else [
            _frozen_leg(SHORT_ID, SHORT, "SELL", "short", 150, "CE"),
            _frozen_leg(HEDGE_ID, HEDGE, "BUY", "hedge", 150, "PE"),
        ],
    }
    if plan_kind == "option_structure":
        resolved["protection_policy"] = {"naked": True} if naked else {}
    return resolved


def _seed_plan(
    session_factory,
    *,
    strategy_id,
    plan_id,
    resolved,
    plan_kind="option_structure",
    phase=None,
    option_run_id=None,
    environment="paper",
):
    session = session_factory()
    try:
        session.add(
            StrategyProposal(
                proposal_id=f"prop-{plan_id}",
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                evaluation_id=f"eval-{plan_id}",
                evaluation_kind="run_now",
                strategy_run_id="run-1",
                target_kind=plan_kind,
                payload={},
                payload_sha256="payload-sha",
                status="validated",
            )
        )
        # The envelope must be durable before the plan that references it: the FK
        # is enforced (PRAGMA foreign_keys=ON), and SQLAlchemy orders inserts by
        # mapper relationship, not by schema constraint.
        session.flush()
        session.add(
            StrategyPlan(
                plan_id=plan_id,
                proposal_id=f"prop-{plan_id}",
                strategy_id=strategy_id,
                account_id=ACCOUNT,
                plan_kind=plan_kind,
                plan_hash=f"hash-{plan_id}",
                logical_plan={},
                resolved_plan=dict(resolved),
                pinned_catalog_generation="gen-1",
                created_at=datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc),
            )
        )
        if phase is not None:
            session.add(
                StrategyPlanOptionRun(
                    plan_id=plan_id,
                    option_run_id=str(option_run_id or RUN_ID),
                    worker_run_id="run-1",
                    strategy_id=strategy_id,
                    account_id=ACCOUNT,
                    execution_environment=environment,
                    phase=phase,
                    created_at=datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc),
                )
            )
        session.commit()
    finally:
        session.close()


def _seed_trail(session_factory, *, plan_id, step_no, rows):
    """``rows`` is a list of ``(event, paper_order_id, filled_quantity)``."""
    base = datetime(2020, 1, 1, 10, 5, tzinfo=timezone.utc)
    session = session_factory()
    try:
        for index, (event, order_id, filled) in enumerate(rows):
            session.add(
                StrategyPlanExecutionEvent(
                    id=f"{plan_id}-{step_no}-{index}",
                    plan_id=plan_id,
                    step_no=step_no,
                    event=event,
                    paper_order_id=order_id,
                    filled_quantity=filled,
                    actor_id="app:admin",
                    detail={"side": "SELL"},
                    created_at=base + timedelta(seconds=index),
                )
            )
        session.commit()
    finally:
        session.close()


def _seed_paper_order(
    session_factory,
    *,
    order_id,
    status,
    quantity,
    filled,
    pending,
    plan_id=None,
    step_no=None,
):
    import json

    metadata = {}
    if plan_id is not None:
        metadata = {"plan_id": plan_id, "step_no": step_no, "execution_mode": "paper"}
    session = session_factory()
    try:
        session.execute(
            text(
                "INSERT INTO public.paper_orders (account_scope, order_id, instrument_token, "
                " exchange, tradingsymbol, product, transaction_type, quantity, "
                " filled_quantity, pending_quantity, status, metadata_json) VALUES "
                "(:account, :order_id, 900001, 'NFO', :symbol, 'NRML', 'sell', :quantity, "
                " :filled, :pending, :status, :metadata)"
            ),
            {
                "account": ACCOUNT,
                "order_id": order_id,
                "symbol": SHORT,
                "quantity": quantity,
                "filled": filled,
                "pending": pending,
                "status": status,
                "metadata": json.dumps(metadata),
            },
        )
        session.commit()
    finally:
        session.close()


def _seed_progress(session_factory, *, order_id, filled, remaining, status):
    session = session_factory()
    try:
        session.add(
            PaperOrderFillProgress(
                account_scope=ACCOUNT,
                paper_order_id=order_id,
                filled_quantity=filled,
                remaining_quantity=remaining,
                status=status,
            )
        )
        session.commit()
    finally:
        session.close()


def _entry_run(*, status="entering", trades=None):
    return OptionRunState(
        strategy_run_id=RUN_ID,
        strategy_name="iron_condor",
        product="NRML",
        status=status,
        legs=[
            {
                "leg_id": "plan-entry:1",
                "tradingsymbol": SHORT,
                "transaction_type": "SELL",
                "quantity": 150,
            },
            {
                "leg_id": "plan-entry:2",
                "tradingsymbol": HEDGE,
                "transaction_type": "BUY",
                "quantity": 150,
            },
        ],
        trades=list(trades or []),
        metadata={"worker_run_id": "run-1", "account_id": ACCOUNT},
    )


def _trade(leg_id, side, quantity):
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT if leg_id == "plan-entry:1" else HEDGE,
    }


def _rows(session_factory, sql, params=None):
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(sql), params or {}).mappings().all()]


def _json(value):
    """JSON columns come back decoded on PostgreSQL and as text on SQLite."""
    if isinstance(value, (dict, list)):
        return value
    import json

    return json.loads(value or "{}")


def _preview_url(strategy_id):
    return f"{BASE}/{strategy_id}/owner-actions/pending-work"


def _cancel_url(strategy_id):
    return f"{BASE}/{strategy_id}/owner-actions/cancel-pending"


def _dead_url(strategy_id, plan_id, step_no=1):
    return f"{BASE}/{strategy_id}/plans/{plan_id}/steps/{step_no}/dead-submission"


# ---------------------------------------------------------------------------
# authentication / authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_route_requires_a_session(session_factory, monkeypatch):
    async with _client(session_factory, monkeypatch, username=None) as client:
        assert (await client.get(_preview_url("stg_1"))).status_code == 401
        assert (
            await client.post(
                _cancel_url("stg_1"), json={"evidence_digest": "x", "reason": "r"}
            )
        ).status_code == 401
        assert (await client.get(_dead_url("stg_1", "p1"))).status_code == 401
        assert (
            await client.post(
                _dead_url("stg_1", "p1"),
                json={"evidence_digest": "x", "disposition": "cancelled", "reason": "r"},
            )
        ).status_code == 401


@pytest.mark.asyncio
async def test_a_foreign_strategy_is_never_reachable(session_factory, monkeypatch):
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        owner_id = await _create(client, "owner")
    # A strategy the session's owner does NOT own: its existence is never even
    # revealed by these routes.
    stranger = SqlAlchemyStrategyRepository(session_factory).create_strategy(
        owner_id="app:someone-else",
        name="stranger",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    stranger_id = str(stranger.id)
    _seed_plan(
        session_factory,
        strategy_id=owner_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(),
        phase="entry",
    )
    _seed_trail(
        session_factory,
        plan_id=ENTRY_PLAN,
        step_no=1,
        rows=[("submitted", None, None), ("partially_filled", ENTRY_ORDER, 75)],
    )
    _seed_paper_order(
        session_factory,
        order_id=ENTRY_ORDER,
        status="partially_filled",
        quantity=150,
        filled=75,
        pending=75,
    )

    async with _client(
        session_factory, monkeypatch, run_store=run_store
    ) as client:
        assert (await client.get(_preview_url(stranger_id))).status_code == 404
        assert (
            await client.post(
                _cancel_url(stranger_id),
                json={"evidence_digest": "0" * 32, "reason": "owner_cancel"},
            )
        ).status_code == 404
        assert (await client.get(_dead_url(stranger_id, ENTRY_PLAN))).status_code == 404
        assert (
            await client.post(
                _dead_url(stranger_id, ENTRY_PLAN),
                json={
                    "evidence_digest": "0" * 32,
                    "disposition": "cancelled",
                    "reason": "owner_disposition",
                },
            )
        ).status_code == 404
        # The owner's own preview still sees it.
        assert (await client.get(_preview_url(owner_id))).status_code == 200


# ---------------------------------------------------------------------------
# cancel pending work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_eligible_entry_admits_and_preserves_a_partial_fill(
    session_factory, monkeypatch
):
    run_store = _FakeRunStore(
        {RUN_ID: _entry_run(trades=[_trade("plan-entry:1", "SELL", 75), _trade("plan-entry:2", "BUY", 150)])}
    )
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(),
        phase="entry",
    )
    _seed_trail(
        session_factory,
        plan_id=ENTRY_PLAN,
        step_no=1,
        rows=[("submitted", None, None), ("partially_filled", ENTRY_ORDER, 75)],
    )
    _seed_paper_order(
        session_factory,
        order_id=ENTRY_ORDER,
        status="partially_filled",
        quantity=150,
        filled=75,
        pending=75,
    )
    _seed_progress(
        session_factory, order_id=ENTRY_ORDER, filled=75, remaining=75, status="partially_filled"
    )
    paper = _FakePaperRuntime(session_factory)

    async with _client(
        session_factory, monkeypatch, run_store=run_store, paper=paper
    ) as client:
        preview = await client.get(_preview_url(strategy_id))
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["coverage"] == "known"
        assert body["items"] == [
            {
                "plan_id": ENTRY_PLAN,
                "step_no": 1,
                "order_id": ENTRY_ORDER,
                "remaining_quantity": 75,
                "eligibility": "eligible",
                "reason_code": None,
            }
        ]

        action = await client.post(
            _cancel_url(strategy_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_cancel"},
        )
        assert action.status_code == 200, action.text
        payload = action.json()
        assert payload["status"] == "complete"
        assert payload["action_id"]
        assert payload["audit_id"]
        assert payload["items"] == [
            {
                "plan_id": ENTRY_PLAN,
                "step_no": 1,
                "order_id": ENTRY_ORDER,
                "eligibility": "eligible",
                "outcome": "cancelled",
                "filled_quantity": 75,
                "remaining_quantity": 0,
                "disposition": "owner_cancelled",
                "run_status": "partial_entry",
                "reason_code": None,
            }
        ]

    # The proven fill is preserved as its OWN trail row, and the cancelled
    # remainder carries the owner's disposition.
    trail = _rows(
        session_factory,
        "SELECT event, filled_quantity, detail FROM strategy_plan_execution_events "
        "WHERE plan_id = :plan ORDER BY created_at, id",
        {"plan": ENTRY_PLAN},
    )
    assert [row["event"] for row in trail] == ["submitted", "partially_filled", "partially_filled", "failed"]
    assert int(trail[-2]["filled_quantity"]) == 75
    assert int(trail[-1]["filled_quantity"]) == 0
    detail = _json(trail[-1]["detail"])
    assert detail["disposition"] == "owner_cancelled"
    assert detail["platform_status"] == "cancelled"

    # The run lands on the state its OWN fills prove.
    assert run_store.transitions == [(RUN_ID, "partial_entry")]

    # The owner action is audited on the strategy's append-only journal.
    journal = _rows(
        session_factory,
        "SELECT event, reason_code FROM strategy_proposal_journal WHERE strategy_id = :s",
        {"s": strategy_id},
    )
    assert journal == [{"event": "owner_action", "reason_code": "cancel_pending"}]

    assert paper.calls == [(ACCOUNT, ENTRY_ORDER)]


@pytest.mark.asyncio
async def test_a_protective_hedge_is_never_cancellable(session_factory, monkeypatch):
    """A covering long is protection: it is reported, never cancelled."""
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    # Step 2 is the hedge leg (role hedge); step 3 is a long leg whose readable
    # role does not protect it from the COVERAGE rule: it hedges the short of the
    # same option type, so it is protective too.
    legs = [
        _frozen_leg(SHORT_ID, SHORT, "SELL", "short", 150, "CE"),
        _frozen_leg(HEDGE_ID, HEDGE, "BUY", "hedge", 150, "PE"),
        _frozen_leg("NSE:COVER", "NIFTY26NOV22500CE2", "BUY", "naked", 150, "CE"),
    ]
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(legs=legs),
        phase="entry",
    )
    for step_no in (2, 3):
        order_id = f"{ENTRY_ORDER}-{step_no}"
        _seed_trail(
            session_factory,
            plan_id=ENTRY_PLAN,
            step_no=step_no,
            rows=[("submitted", None, None), ("partially_filled", order_id, 0)],
        )
        _seed_paper_order(
            session_factory,
            order_id=order_id,
            status="partially_filled",
            quantity=150,
            filled=0,
            pending=150,
        )
    paper = _FakePaperRuntime(session_factory)

    async with _client(
        session_factory, monkeypatch, run_store=run_store, paper=paper
    ) as client:
        preview = await client.get(_preview_url(strategy_id))
        assert preview.status_code == 200, preview.text
        body = preview.json()
        by_step = {row["step_no"]: row for row in body["items"]}
        assert by_step[2]["eligibility"] == "ineligible"
        assert by_step[2]["reason_code"] == "CANCEL_PROTECTIVE_ORDER_FORBIDDEN"
        assert by_step[3]["eligibility"] == "ineligible"
        assert by_step[3]["reason_code"] == "CANCEL_PROTECTIVE_ORDER_FORBIDDEN"

        action = await client.post(
            _cancel_url(strategy_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_cancel"},
        )
        assert action.status_code == 200, action.text
        payload = action.json()
        assert payload["status"] == "complete"
        assert {row["outcome"] for row in payload["items"]} == {"skipped"}
        assert {row["reason_code"] for row in payload["items"]} == {
            "CANCEL_PROTECTIVE_ORDER_FORBIDDEN"
        }

    # Nothing was cancelled and nothing was written to the trail.
    assert paper.calls == []
    assert all(
        row["status"] == "partially_filled"
        for row in _rows(session_factory, "SELECT status FROM public.paper_orders")
    )
    trail = _rows(
        session_factory,
        "SELECT event FROM strategy_plan_execution_events WHERE plan_id = :plan",
        {"plan": ENTRY_PLAN},
    )
    assert {row["event"] for row in trail} == {"submitted", "partially_filled"}


@pytest.mark.asyncio
async def test_changed_evidence_refuses_the_cancel(session_factory, monkeypatch):
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(),
        phase="entry",
    )
    _seed_trail(
        session_factory,
        plan_id=ENTRY_PLAN,
        step_no=1,
        rows=[("submitted", None, None), ("partially_filled", ENTRY_ORDER, 75)],
    )
    _seed_paper_order(
        session_factory,
        order_id=ENTRY_ORDER,
        status="partially_filled",
        quantity=150,
        filled=75,
        pending=75,
    )
    paper = _FakePaperRuntime(session_factory)

    async with _client(
        session_factory, monkeypatch, run_store=run_store, paper=paper
    ) as client:
        preview = await client.get(_preview_url(strategy_id))
        stale = preview.json()["evidence_digest"]

        # The order moves between the owner's look and the action.
        with session_factory() as session:
            session.execute(
                text(
                    "UPDATE public.paper_orders SET status = 'filled', "
                    "pending_quantity = 0, filled_quantity = 150 "
                    "WHERE order_id = :order_id"
                ),
                {"order_id": ENTRY_ORDER},
            )
            session.commit()

        action = await client.post(
            _cancel_url(strategy_id),
            json={"evidence_digest": stale, "reason": "owner_cancel"},
        )
        assert action.status_code == 409, action.text
        assert action.json()["detail"]["rejection_reason"] == "CANCEL_EVIDENCE_CHANGED"

    assert paper.calls == []
    assert run_store.transitions == []


# ---------------------------------------------------------------------------
# dead-submission disposition
# ---------------------------------------------------------------------------


def _seed_dead_adjust(session_factory, strategy_id):
    """An adjust step whose submission never got an outcome, and a dead order."""
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ADJUST_PLAN,
        resolved=_resolved_plan(),
        phase="adjust",
        option_run_id=RUN_ID,
    )
    _seed_trail(
        session_factory,
        plan_id=ADJUST_PLAN,
        step_no=1,
        rows=[("submitted", None, None)],
    )
    # The order exists (the platform's attribution binds it to this step) and is
    # terminal: the submission is dead, not merely unanswered.
    _seed_paper_order(
        session_factory,
        order_id=ADJUST_ORDER,
        status="cancelled",
        quantity=75,
        filled=0,
        pending=0,
        plan_id=ADJUST_PLAN,
        step_no=1,
    )


def _plan_state(session_factory, plan_id):
    from backend.options.execution.plan_binding import option_plan_execution_state

    with session_factory() as session:
        return dict(option_plan_execution_state(plan_id, session=session))


@pytest.mark.asyncio
async def test_terminal_paper_evidence_admits_its_disposition_and_refuses_a_mismatch(
    session_factory, monkeypatch
):
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_dead_adjust(session_factory, strategy_id)

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        inspection = await client.get(_dead_url(strategy_id, ADJUST_PLAN))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["plan_id"] == ADJUST_PLAN
        assert body["step_no"] == 1
        assert body["execution_environment"] == "paper"
        assert body["trail_state"] == "submitted"
        assert body["source"] == "paper_order"
        assert body["status"] == "cancelled"
        assert body["order_id"] == ADJUST_ORDER
        # The requested quantity is the FROZEN target of the step, not the size
        # of this order: 150 of the short leg was frozen, and this order carried
        # 75 of it.
        assert body["requested_quantity"] == 150
        assert body["filled_quantity"] == 0
        assert body["remaining_quantity"] == 0
        assert body["allowed_dispositions"] == ["cancelled", "failed_residual_abandoned"]
        assert body["evidence_digest"]

        # A disposition the platform's evidence does not support is refused.
        mismatch = await client.post(
            _dead_url(strategy_id, ADJUST_PLAN),
            json={
                "evidence_digest": body["evidence_digest"],
                "disposition": "filled",
                "reason": "owner_disposition",
            },
        )
        assert mismatch.status_code == 409, mismatch.text
        assert (
            mismatch.json()["detail"]["rejection_reason"]
            == "DEAD_SUBMISSION_DISPOSITION_MISMATCH"
        )

        accepted = await client.post(
            _dead_url(strategy_id, ADJUST_PLAN),
            json={
                "evidence_digest": body["evidence_digest"],
                "disposition": "cancelled",
                "reason": "owner_disposition",
            },
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert payload["status"] == "complete"
        assert payload["items"][0]["disposition"] == "cancelled"
        assert payload["items"][0]["outcome"] == "disposed"
        assert payload["audit_id"]

    trail = _rows(
        session_factory,
        "SELECT event, filled_quantity, detail FROM strategy_plan_execution_events "
        "WHERE plan_id = :plan ORDER BY created_at, id",
        {"plan": ADJUST_PLAN},
    )
    assert [row["event"] for row in trail] == ["submitted", "cancelled"]
    assert _json(trail[-1]["detail"])["disposition"] == "cancelled"
    assert _json(trail[-1]["detail"])["platform_status"] == "cancelled"

    journal = _rows(
        session_factory,
        "SELECT event, reason_code FROM strategy_proposal_journal WHERE strategy_id = :s",
        {"s": strategy_id},
    )
    assert journal == [
        {"event": "owner_action", "reason_code": "dead_submission_disposition"}
    ]


@pytest.mark.asyncio
async def test_a_dead_adjust_disposition_makes_the_plan_finished(
    session_factory, monkeypatch
):
    """The takeover gate this disposition exists for must open afterwards."""
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_dead_adjust(session_factory, strategy_id)

    # Before: a committed submission with no outcome is IN FLIGHT, so the adjust
    # takeover rule refuses to re-derive anything on top of it.
    assert _plan_state(session_factory, ADJUST_PLAN)["state"] == "in_flight"

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        inspection = await client.get(_dead_url(strategy_id, ADJUST_PLAN))
        digest = inspection.json()["evidence_digest"]
        accepted = await client.post(
            _dead_url(strategy_id, ADJUST_PLAN),
            json={
                "evidence_digest": digest,
                "disposition": "cancelled",
                "reason": "owner_disposition",
            },
        )
        assert accepted.status_code == 200, accepted.text

    assert _plan_state(session_factory, ADJUST_PLAN)["state"] == "finished"


@pytest.mark.asyncio
async def test_an_open_remainder_is_not_a_dead_submission(session_factory, monkeypatch):
    """Unanswered means unresolved, not dead."""
    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ADJUST_PLAN,
        resolved=_resolved_plan(),
        phase="adjust",
    )
    _seed_trail(
        session_factory,
        plan_id=ADJUST_PLAN,
        step_no=1,
        rows=[("submitted", None, None), ("partially_filled", ADJUST_ORDER, 25)],
    )
    _seed_paper_order(
        session_factory,
        order_id=ADJUST_ORDER,
        status="partially_filled",
        quantity=75,
        filled=25,
        pending=50,
    )

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        inspection = await client.get(_dead_url(strategy_id, ADJUST_PLAN))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["status"] == "partially_filled"
        assert body["remaining_quantity"] == 50
        assert body["allowed_dispositions"] == []

        refused = await client.post(
            _dead_url(strategy_id, ADJUST_PLAN),
            json={
                "evidence_digest": body["evidence_digest"],
                "disposition": "cancelled",
                "reason": "owner_disposition",
            },
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"]
            == "DEAD_SUBMISSION_OPEN_REMAINDER"
        )

    trail = _rows(
        session_factory,
        "SELECT event FROM strategy_plan_execution_events WHERE plan_id = :plan",
        {"plan": ADJUST_PLAN},
    )
    assert [row["event"] for row in trail] == ["submitted", "partially_filled"]
