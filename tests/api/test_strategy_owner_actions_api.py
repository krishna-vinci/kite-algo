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
    StrategyApproval,
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
        # Flatten (S3) reads the platform's own catalog to classify a book as an
        # option structure or not, and freezes its reduction plans against a
        # PUBLISHED generation, so these are the production shapes.
        cursor.execute(
            """
            CREATE TABLE public.instrument_catalog_generations (
                id TEXT PRIMARY KEY, status TEXT, published_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.instrument_catalog_records (
                instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                instrument_type TEXT, lot_size INTEGER, current_generation_id TEXT,
                expiry TEXT, tick_size REAL, underlying TEXT, strike REAL,
                option_type TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.instrument_broker_mappings (
                mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT,
                broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER,
                valid_from_generation TEXT, valid_to_generation TEXT,
                is_current INTEGER
            )
            """
        )
        # The durable option-run rows and the binding edges flatten's option-exit
        # pass reads/writes are ``public.``-qualified in production code.
        cursor.execute(
            """
            CREATE TABLE public.option_run_states (
                strategy_run_id TEXT PRIMARY KEY,
                strategy_name TEXT,
                product TEXT,
                status TEXT NOT NULL,
                legs TEXT,
                protection TEXT,
                metadata TEXT,
                orders TEXT,
                trades TEXT,
                completed_legs TEXT,
                failed_legs TEXT,
                pending_legs TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.strategy_plan_option_runs (
                plan_id TEXT PRIMARY KEY,
                option_run_id TEXT NOT NULL,
                worker_run_id TEXT,
                strategy_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                execution_environment TEXT NOT NULL,
                phase TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.option_protection_owners (
                option_run_id TEXT PRIMARY KEY,
                strategy_id TEXT,
                account_id TEXT,
                execution_environment TEXT,
                owner_run_id TEXT,
                owner_epoch INTEGER,
                policy_version TEXT,
                policy TEXT,
                action_state TEXT,
                stage_digest TEXT,
                state TEXT,
                released_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.order_trade_fills (
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                quantity INTEGER,
                price REAL,
                transaction_type TEXT,
                tradingsymbol TEXT,
                instrument_token INTEGER,
                product TEXT,
                fill_timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (account_id, order_id, trade_id)
            )
            """
        )
        # The broker order projection a LIVE pending-entry cancel reads to PROVE
        # the order went terminal (``_broker_projection``); a cancel that cannot
        # be seen as terminal is never an assumed cancellation.
        cursor.execute(
            """
            CREATE TABLE public.order_state_projection (
                account_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                latest_status TEXT NOT NULL DEFAULT 'OPEN',
                last_seen_filled_quantity INTEGER NOT NULL DEFAULT 0,
                terminal BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, order_id)
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


def _app(session_factory, monkeypatch, user, **state):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.include_router(owner_actions_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.dependency_overrides[owner_actions_router._owner_actions_db] = lambda: (
        session_factory
    )
    run_store = state.pop("run_store", None)
    if run_store is not None:
        app.state.option_run_store = run_store
    paper = state.pop("paper", None)
    if paper is not None:
        app.state.paper_runtime_service = paper
    for name, value in state.items():
        if value is not None:
            setattr(app.state, name, value)
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
    account=ACCOUNT,
):
    session = session_factory()
    try:
        session.add(
            StrategyProposal(
                proposal_id=f"prop-{plan_id}",
                strategy_id=strategy_id,
                account_id=str(account),
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
                account_id=str(account),
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
                    account_id=str(account),
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


def _flatten_url(strategy_id):
    return f"{BASE}/{strategy_id}/owner-actions/flatten"


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
        assert (await client.get(_flatten_url("stg_1"))).status_code == 401
        assert (
            await client.post(
                _flatten_url("stg_1"),
                json={"reason": "owner_flatten", "stop_evaluator": True},
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


# ---------------------------------------------------------------------------
# flatten (B2.6b S3, section 3)
# ---------------------------------------------------------------------------

EQ_ID = "NSE:INFY"
EQ = "INFY"
EQ_ORDER = "PAPER-EQ-1"
REDUCE_PLAN = "plan-reduce"
REDUCE_ORDER = "PAPER-REDUCE-1"
GEN = "gen-flatten-1"
GENERATION_AT = "2026-09-01T00:00:00+00:00"


def _seed_job(
    session_factory,
    *,
    strategy_id,
    status="queued",
    owner_id="app:admin",
    handoff_at=None,
    attempt=1,
):
    """A hosted job through the REAL repository, then its live status."""
    from backend.strategies import service as strategy_service
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(session_factory)
    version = repo.create_version(
        strategy_id=str(strategy_id),
        source="x",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot=strategy_service.build_capabilities_snapshot(trade=False),
        created_by=owner_id,
    )
    job = repo.create_job(
        strategy_id=str(strategy_id),
        version_id=version.id,
        owner_id=owner_id,
        job_kind="finite",
        execution_mode="paper",
        params={},
        attempt=int(attempt),
    )
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE strategy_jobs SET status = :status, handoff_at = :handoff, "
                "run_id = 'run-1' WHERE id = :id"
            ),
            {
                "status": str(status),
                "handoff": handoff_at,
                "id": str(job.id),
            },
        )
        session.commit()
    return str(job.id)


def _seed_approval(session_factory, *, strategy_id, approval_id="approval-1"):
    session = session_factory()
    try:
        session.add(
            StrategyApproval(
                approval_id=approval_id,
                plan_id="plan-approved",
                strategy_id=str(strategy_id),
                account_id=ACCOUNT,
                actor_id="app:admin",
                actor_kind="manual",
                reservation_id="reservation-1",
                execution_environment="live",
                status="active",
                plan_hash="hash-approved",
                snapshot={},
                evidence={},
                validity_seconds=900,
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_projection(
    session_factory,
    *,
    strategy_id,
    instrument_id,
    product,
    net_quantity,
    environment="paper",
    account=ACCOUNT,
    tradingsymbol="INFY",
    instrument_token=408065,
    identity_kind="canonical",
    identity_key=None,
):
    """ONE attributed book row for this strategy (canonical or raw)."""
    from backend.strategies.attribution_models import StrategyPositionProjection

    with session_factory() as session:
        session.add(
            StrategyPositionProjection(
                account_id=str(account),
                strategy_id=str(strategy_id),
                execution_environment=environment,
                identity_kind=identity_kind,
                identity_key=str(identity_key or instrument_id or "raw-key"),
                product=str(product),
                canonical_instrument_id=(
                    None if identity_kind != "canonical" else str(instrument_id)
                ),
                instrument_token=int(instrument_token),
                exchange="NSE",
                tradingsymbol=str(tradingsymbol),
                net_quantity=int(net_quantity),
                projection_version=1,
            )
        )
        session.commit()


def _seed_catalog(
    session_factory,
    *,
    instrument_id=EQ_ID,
    symbol=EQ,
    instrument_type="EQ",
    broker_token=408065,
    generation=GEN,
):
    """A published generation plus the record AND broker mapping resolution needs."""
    with session_factory() as session:
        session.execute(
            text(
                "INSERT OR IGNORE INTO public.instrument_catalog_generations "
                "(id, status, published_at) VALUES (:gen, 'published', :at)"
            ),
            {"gen": generation, "at": GENERATION_AT},
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.instrument_catalog_records "
                "(instrument_id, exchange, tradingsymbol, lifecycle_status, "
                " instrument_type, current_generation_id) "
                "VALUES (:iid, 'NSE', :symbol, 'active', :kind, :gen)"
            ),
            {
                "iid": instrument_id,
                "symbol": symbol,
                "kind": instrument_type,
                "gen": generation,
            },
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.instrument_broker_mappings "
                "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                " broker_token, valid_from_generation, is_current) "
                "VALUES (:mid, :iid, 'kite', 'NSE', :symbol, :token, :gen, 1)"
            ),
            {
                "mid": f"map-{instrument_id}",
                "iid": instrument_id,
                "symbol": symbol,
                "token": broker_token,
                "gen": generation,
            },
        )
        session.commit()


def _seed_bound_run(
    session_factory,
    *,
    strategy_id,
    run_id="run-bound-1",
    owner_id="app:admin",
    environment="paper",
    account=ACCOUNT,
):
    """The immutable run binding a flatten reduction plan is attributed to."""
    from backend.strategies.attribution_models import StrategyRunBinding

    with session_factory() as session:
        session.add(
            StrategyRunBinding(
                strategy_run_id=str(run_id),
                strategy_id=str(strategy_id),
                owner_id=owner_id,
                account_id=str(account),
                execution_environment=str(environment),
                bound_by=owner_id,
                binding_source="hosted_job",
            )
        )
        session.commit()


def _seed_live_claim(
    session_factory,
    *,
    strategy_id,
    plan_id,
    account,
    order_id,
    quantity=150,
    filled=0,
    step_no=1,
    state="pending",
    environment="live",
):
    """A durable LIVE claim still unresolved, plus its broker order projection."""
    from backend.strategies.attribution_models import LivePlanSubmission

    with session_factory() as session:
        session.add(
            LivePlanSubmission(
                submission_id=f"sub-{plan_id}-{step_no}",
                plan_id=str(plan_id),
                step_no=int(step_no),
                step_ref=f"{plan_id}:{step_no}",
                strategy_id=str(strategy_id),
                account_id=str(account),
                execution_environment=str(environment),
                state=str(state),
                broker_order_ids=[str(order_id)],
                delta_snapshot={
                    "quantity": int(quantity),
                    "filled_quantity": int(filled),
                    "remaining_quantity": int(quantity) - int(filled),
                },
                detail={},
            )
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.order_state_projection "
                "(account_id, order_id, latest_status, last_seen_filled_quantity, "
                " terminal) VALUES (:account, :order_id, 'OPEN', :filled, 0)"
            ),
            {
                "account": str(account),
                "order_id": str(order_id),
                "filled": int(filled),
            },
        )
        session.commit()


class _FakeLiveCancelBoundary:
    """The injectable broker cancel: proves the order terminal, like a real ack."""

    def __init__(self, session_factory, *, events=None):
        self.session_factory = session_factory
        self.calls = []
        self.events = events if events is not None else []

    async def __call__(self, *, account_id, order_id):
        self.calls.append((str(account_id), str(order_id)))
        self.events.append(f"cancel:{order_id}")
        with self.session_factory() as session:
            session.execute(
                text(
                    "UPDATE public.order_state_projection "
                    "SET terminal = 1, latest_status = 'CANCELLED' "
                    "WHERE account_id = :account AND order_id = :order_id"
                ),
                {"account": str(account_id), "order_id": str(order_id)},
            )
            session.commit()
        return {"order_id": str(order_id), "status": "cancelled"}


def _seed_public_edge(
    session_factory,
    *,
    strategy_id,
    plan_id,
    option_run_id=RUN_ID,
    worker_run_id="worker-1",
    phase="entry",
    environment="paper",
):
    """The binding edge the PRODUCTION stores read (``public.``-qualified)."""
    with session_factory() as session:
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.strategy_plan_option_runs "
                "(plan_id, option_run_id, worker_run_id, strategy_id, account_id, "
                " execution_environment, phase) VALUES (:plan, :run, :worker, "
                " :strategy, :account, :environment, :phase)"
            ),
            {
                "plan": str(plan_id),
                "run": str(option_run_id),
                "worker": str(worker_run_id),
                "strategy": str(strategy_id),
                "account": ACCOUNT,
                "environment": str(environment),
                "phase": str(phase),
            },
        )
        session.commit()


def _flat_run(*, status="exited"):
    """A run whose OWN fills prove it holds nothing."""
    return _entry_run(
        status=status,
        trades=[
            _trade("plan-entry:1", "SELL", 150),
            _trade("plan-entry:1", "BUY", 150),
            _trade("plan-entry:2", "BUY", 150),
            _trade("plan-entry:2", "SELL", 150),
        ],
    )


def _flatten_item(body, kind, key=None):
    for row in body["items"]:
        if row["kind"] != kind:
            continue
        if key is None or row["key"] == key:
            return row
    return None


class _FakeReductionPipeline:
    """The governed execute route's paper pipeline, as flatten calls it.

    ``execute`` writes the SAME attributed projection a real fill would, so the
    book genuinely closes; ``real_admission`` swaps the stand-in verdict for the
    production ``AdmissionService`` so "it admits" means what it means in
    production.
    """

    def __init__(
        self, session_factory, *, zero_book=True, real_admission=False, events=None
    ):
        self.session_factory = session_factory
        self.zero_book = zero_book
        self.real_admission = real_admission
        self.admit_calls = 0
        self.admitted = []
        self.environments = []
        self.executed = []
        self.events = events if events is not None else []

    def admit(self, plan, *, environment):
        self.admit_calls += 1
        self.environments.append(str(environment))
        if not self.real_admission:
            verdict = {"admitted": True, "detail": {"source": "test_pipeline"}}
        else:
            from backend.strategies.admission import AdmissionService

            verdict = AdmissionService(
                session_factory=self.session_factory
            ).evaluate(plan, execution_environment=environment).as_dict()
        self.admitted.append(verdict)
        return verdict

    async def execute(self, plan, *, actor):
        self.executed.append({"plan_id": plan.get("plan_id"), "actor": str(actor)})
        self.events.append(f"reduce:{plan.get('plan_id')}")
        if self.zero_book:
            with self.session_factory() as session:
                session.execute(
                    text("UPDATE strategy_position_projection SET net_quantity = 0")
                )
                session.commit()
        return {"status": "filled", "steps": [{"step_no": 1, "filled_quantity": 150}]}


def _canned_reduction_builder(plan_id="plan-flatten-eq", target=0):
    """A planner that returns ONE frozen plan without touching the catalog."""

    def build(scope, book, *, operation_id, actor):
        _ = (scope, operation_id, actor)
        return {
            "plan_id": plan_id,
            "plan": {
                "plan_id": plan_id,
                "strategy_id": str(scope["strategy_id"]),
                "account_id": str(scope["account_id"]),
                "plan_kind": "single_instrument",
                "resolved_plan": {
                    "target_kind": "single_instrument",
                    "legs": [
                        {
                            "instrument_id": book.get("instrument_id"),
                            "product": book.get("product"),
                            "quantity": abs(int(target)),
                            "signed_quantity": int(target),
                        }
                    ],
                },
            },
        }

    return build


class _ScriptedExitRunner:
    """The S2 owner exit for one run, with a caller-scripted sequence of results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def __call__(self, scope, option_run_id, *, reason):
        self.calls.append((str(option_run_id), str(reason)))
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return dict(result)


class _OneRunSnapshot:
    """A scope-derived run discovery returning exactly one run row."""

    def __init__(self, row):
        self.row = dict(row)

    def option_runs_for_scope(self, **kwargs):
        _ = kwargs
        return [dict(self.row)], {"coverage": "known", "reason": ""}


class _FakeExitBoundary:
    """The paper staged-exit boundary the S2 owner exit submits through."""

    def __init__(self):
        self.calls = []

    async def place_order(self, *, account_scope, order_payload, attribution):
        self.calls.append(
            {
                "account_id": str(account_scope),
                "order": dict(order_payload or {}),
                "attribution": dict(attribution or {}),
            }
        )
        return {"order": {"order_id": f"PAPER-EXIT-{len(self.calls)}"}}


def _run_legs():
    """The durable legs of the run the option-exit pass is asked to close."""
    return [
        {
            "leg_id": "plan-entry:1",
            "tradingsymbol": SHORT,
            "transaction_type": "SELL",
            "quantity": 150,
            "exchange": "NFO",
            "product": "NRML",
        },
        {
            "leg_id": "plan-entry:2",
            "tradingsymbol": HEDGE,
            "transaction_type": "BUY",
            "quantity": 150,
            "exchange": "NFO",
            "product": "NRML",
        },
    ]


def _one_run_row(*, status="entered"):
    return {
        "option_run_id": RUN_ID,
        "plan_ids": [ENTRY_PLAN],
        "originating_plan_id": ENTRY_PLAN,
        "originating_phase": "entry",
        "phase": "entry",
        "worker_run_id": "run-1",
        "underlying": "NIFTY",
        "expiry": "2026-11-26",
        "structure_digest": "sha-entry-digest",
        "structure_generation": 1,
        "product": "NRML",
        "status": str(status),
        "legs": _run_legs(),
        "completed_legs": [],
        "pending_legs": [],
        "failed_legs": [],
        "protective_exit_unresolved": False,
        "coverage": "known",
    }


async def _post_flatten(client, strategy_id, *, reason="owner_flatten", stop_evaluator=True):
    return await client.post(
        _flatten_url(strategy_id),
        json={"reason": reason, "stop_evaluator": stop_evaluator},
    )


@pytest.mark.asyncio
async def test_flatten_refuses_while_a_running_evaluation_cannot_be_proven_stopped(
    session_factory, monkeypatch
):
    """Section 3 step 1: stop, prove it, or refuse by name."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    _seed_job(
        session_factory,
        strategy_id=strategy_id,
        status="running",
        handoff_at=datetime.now(timezone.utc),
    )

    async with _client(session_factory, monkeypatch) as client:
        refused = await _post_flatten(client, strategy_id)
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["rejection_reason"] == "FLATTEN_EVALUATION_ACTIVE"
        (job,) = detail["stop"]["jobs"]
        # The stop was REQUESTED (durably) and is not proven: the child may still
        # place work, so flatten refuses rather than racing it.
        assert job["state"] == "stopping"
        assert job["proven_stopped"] is False
        assert job["requested"] is True

        # The caller declining the stop is the same refusal, reported as requested.
        declined = await _post_flatten(client, strategy_id, stop_evaluator=False)
        assert declined.status_code == 409, declined.text
        assert (
            declined.json()["detail"]["rejection_reason"]
            == "FLATTEN_EVALUATION_ACTIVE"
        )
        assert declined.json()["detail"]["stop"]["jobs"][0]["state"] == "stopping"

    # The durable stop request and its audit exist; no flatten operation was started.
    job_row = _rows(
        session_factory, "SELECT desired_state, stop_requested_at FROM strategy_jobs"
    )[0]
    assert job_row["desired_state"] == "stopped"
    assert job_row["stop_requested_at"]
    assert _rows(session_factory, "SELECT operation_id FROM strategy_flatten_operations") == []
    journal = _rows(
        session_factory,
        "SELECT reason_code FROM strategy_proposal_journal "
        "WHERE strategy_id = :strategy",
        {"strategy": strategy_id},
    )
    assert "flatten_stop_evaluator" in [row["reason_code"] for row in journal]


@pytest.mark.asyncio
async def test_flatten_stops_a_queued_evaluator_and_records_the_stop(
    session_factory, monkeypatch
):
    """A provable stop lets the flatten proceed, and the stop is on the record."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    _seed_job(session_factory, strategy_id=strategy_id, status="queued")

    async with _client(session_factory, monkeypatch) as client:
        accepted = await _post_flatten(client, strategy_id)
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["stop"]["state"] == "confirmed"
        (job,) = body["stop"]["jobs"]
        assert job["status"] == "stopped"
        assert job["proven_stopped"] is True
        # Nothing was left to do: an empty strategy IS flat.
        assert body["status"] == "complete", body
        assert body["missing"] == []
        assert body["operation_id"]
        assert body["audit_id"]

    assert _rows(session_factory, "SELECT status FROM strategy_jobs")[0]["status"] == (
        "stopped"
    )
    assert len(_rows(session_factory, "SELECT operation_id FROM strategy_flatten_operations")) == 1


@pytest.mark.asyncio
async def test_reducing_pending_work_survives_the_flatten_cancel_step(
    session_factory, monkeypatch
):
    """Section 3 step 3: cancel qualifying entries, leave reducing work alone."""
    from backend.strategies.attribution_models import StrategyPositionProjection

    run_store = _FakeRunStore({RUN_ID: _entry_run()})
    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    # An exposure-increasing entry (a naked short) that flatten MAY cancel.
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(naked=True),
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
        session_factory,
        order_id=ENTRY_ORDER,
        filled=75,
        remaining=75,
        status="partially_filled",
    )
    # And a REDUCING working order (150 -> 0, a close-to-flat instruction the
    # compiler treats as a real target) that flatten must never cancel.
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=REDUCE_PLAN,
        resolved={
            "target_kind": "single_instrument",
            "legs": [
                {
                    "instrument_id": EQ_ID,
                    "tradingsymbol": EQ,
                    "broker_symbol": EQ,
                    "side": "BUY",
                    "product": "CNC",
                    "quantity": 0,
                    "signed_quantity": 0,
                }
            ],
        },
        plan_kind="single_instrument",
        phase="entry",
        option_run_id="opt_run_reduce",
    )
    _seed_trail(
        session_factory,
        plan_id=REDUCE_PLAN,
        step_no=1,
        rows=[("submitted", REDUCE_ORDER, None)],
    )
    _seed_paper_order(
        session_factory,
        order_id=REDUCE_ORDER,
        status="pending",
        quantity=50,
        filled=0,
        pending=50,
    )
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    # The catalog decides that INFY is NOT an option, so flatten may reduce it
    # with a single-instrument plan rather than routing it to a run's exit.
    _seed_catalog(session_factory)

    paper = _FakePaperRuntime(session_factory)
    pipeline = _FakeReductionPipeline(session_factory)
    async with _client(
        session_factory,
        monkeypatch,
        run_store=run_store,
        paper=paper,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        first = await _post_flatten(client, strategy_id)
        assert first.status_code == 200, first.text
        body = first.json()
        # The qualifying entry is cancelled; the reducing order is left running.
        cancelled = _flatten_item(body, "cancel_pending", f"cancel:{ENTRY_PLAN}:1")
        assert cancelled["state"] == "done", cancelled
        assert cancelled["detail"]["outcome"] == "cancelled"
        assert cancelled["detail"]["filled_quantity"] == 75
        reducing = _flatten_item(body, "cancel_pending", f"cancel:{REDUCE_PLAN}:1")
        assert reducing["state"] == "in_progress", reducing
        assert reducing["reason_code"] == "CANCEL_REDUCTION_FORBIDDEN"
        assert body["status"] == "in_progress", body["items"]
        assert "no_in_flight_governed_work" in body["missing"]
        # The EQ book WAS closed by the reduction plan, so books_zero holds.
        assert "books_zero" not in body["missing"]
        assert _flatten_item(body, "nonoption_reduction")["state"] == "done"
        assert paper.calls == [(ACCOUNT, ENTRY_ORDER)]
        assert len(pipeline.executed) == 1

        # A resume preserves the finished cancel and re-reports the reducing work.
        second = await _post_flatten(client, strategy_id)
        assert second.status_code == 200, second.text
        again = second.json()
        assert _flatten_item(again, "cancel_pending", f"cancel:{ENTRY_PLAN}:1")["state"] == (
            "done"
        )
        assert _flatten_item(again, "cancel_pending", f"cancel:{REDUCE_PLAN}:1")["state"] == (
            "in_progress"
        )
        assert paper.calls == [(ACCOUNT, ENTRY_ORDER)]
        assert len(pipeline.executed) == 1

    # The reducing order is untouched: no party cancelled it and the fill stands.
    order = _rows(
        session_factory,
        "SELECT status, pending_quantity FROM public.paper_orders "
        "WHERE order_id = :order",
        {"order": REDUCE_ORDER},
    )[0]
    assert order["status"] == "pending"
    assert int(order["pending_quantity"]) == 50
    reducing_trail = _rows(
        session_factory,
        "SELECT event FROM strategy_plan_execution_events WHERE plan_id = :plan",
        {"plan": REDUCE_PLAN},
    )
    assert [row["event"] for row in reducing_trail] == ["submitted"]


@pytest.mark.asyncio
async def test_flatten_exits_an_option_run_short_first_and_keeps_the_hedge(
    session_factory, monkeypatch
):
    """Section 3 step 4: through the REAL S2 exit, shorts first, hedges proven."""
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest

    store = DurableOptionRunStore(session_factory=session_factory)
    async with _client(session_factory, monkeypatch, run_store=store) as client:
        strategy_id = await _create(client)
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id=ENTRY_PLAN,
        resolved=_resolved_plan(naked=True),
        phase="entry",
    )
    # The production stores read the binding edge and the run ``public.``-qualified.
    _seed_public_edge(session_factory, strategy_id=strategy_id, plan_id=ENTRY_PLAN)
    _seed_bound_run(session_factory, strategy_id=strategy_id, run_id="run-1")
    store.create_run(
        OptionRunCreateRequest(
            strategy_run_id=RUN_ID,
            strategy_name="iron_condor",
            product="NRML",
            legs=_run_legs(),
            protection={"structure_digest": "sha-entry-digest"},
            metadata={
                "strategy_id": str(strategy_id),
                "account_id": ACCOUNT,
                "execution_environment": "paper",
                "worker_run_id": "run-1",
                "plan_id": ENTRY_PLAN,
                "source": "hosted_plan_execution",
            },
        )
    )
    run = store.get_run(RUN_ID)
    run.status = "entered"
    run.trades = [
        _trade("plan-entry:1", "SELL", 150),
        _trade("plan-entry:2", "BUY", 150),
    ]
    store.save_run(run)

    boundary = _FakeExitBoundary()
    async with _client(
        session_factory, monkeypatch, run_store=store, paper=boundary
    ) as client:
        response = await _post_flatten(client, strategy_id)
        assert response.status_code == 200, response.text
        body = response.json()

    item = _flatten_item(body, "option_exit", f"option_exit:{RUN_ID}")
    assert item["state"] == "in_progress", item
    assert item["reason_code"] == "option_exit_stage_submitted"
    assert item["detail"]["shorts_proven_closed"] is False
    assert [row["reason"] for row in item["detail"]["withheld_hedges"]] == [
        "short_not_proven_closed"
    ]
    # ONE stage, and it is the SHORT cover: the hedge is never released early.
    stages = [
        (row["tradingsymbol"], row["transaction_type"], row["quantity"])
        for row in item["detail"]["stage_items"]
    ]
    assert stages == [(SHORT, "BUY", 150)]
    assert len(boundary.calls) == 1
    assert boundary.calls[0]["order"]["tradingsymbol"] == SHORT
    assert (
        boundary.calls[0]["attribution"]["entry_surface"]
        == "hosted_option_owner_exit"
    )
    assert boundary.calls[0]["attribution"]["source"] == "owner_discretionary_exit"
    # The run moved to ``exiting`` - never ``exited`` on acceptance - so flatten is
    # not complete while the structure is still held.
    assert store.get_run(RUN_ID).status == "exiting"
    assert body["status"] == "in_progress", body["missing"]
    assert "option_runs_flat" in body["missing"]
    assert "no_in_flight_governed_work" in body["missing"]


@pytest.mark.asyncio
async def test_a_partial_option_failure_preserves_completed_reductions_and_resumes(
    session_factory, monkeypatch
):
    """Section 3 step 6: one item's failure blocks only that item."""
    from backend.options.execution.repair import OptionRunRepairRefusal

    runner = _ScriptedExitRunner(
        [
            OptionRunRepairRefusal(
                "OPTION_PROTECTIVE_EXIT_UNRESOLVED",
                {"option_run_id": RUN_ID, "message": "a stage still owns the run"},
            ),
            {
                "status": "complete",
                "state": "flat",
                "run_status": "exited",
                "evidence_digest": "digest-flat",
                "items": [],
                "shorts_proven_closed": True,
                "withheld_hedges": [],
            },
        ]
    )
    snapshot = _OneRunSnapshot(_one_run_row(status="entered"))
    run_store = _FakeRunStore({RUN_ID: _entry_run(status="entered")})
    pipeline = _FakeReductionPipeline(session_factory)

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    _seed_catalog(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        run_store=run_store,
        owned_work_snapshot_service=snapshot,
        owner_action_option_exit_runner=runner,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        first = await _post_flatten(client, strategy_id)
        assert first.status_code == 200, first.text
        body = first.json()
        # The option run failed; the reduction it does not depend on still ran.
        option = _flatten_item(body, "option_exit", f"option_exit:{RUN_ID}")
        assert option["state"] == "blocked", option
        assert option["reason_code"] == "OPTION_PROTECTIVE_EXIT_UNRESOLVED"
        reduction = _flatten_item(body, "nonoption_reduction")
        assert reduction["state"] == "done", reduction
        assert body["status"] == "blocked", body
        assert body["refusal"] == "OPTION_PROTECTIVE_EXIT_UNRESOLVED"
        assert len(pipeline.executed) == 1

        # The staged exit settles and the run's own fills prove it flat: a resume
        # finishes the operation, and the completed reduction is NOT redone.
        snapshot.row["status"] = "exited"
        run_store._runs[RUN_ID] = _flat_run()
        second = await _post_flatten(client, strategy_id)
        assert second.status_code == 200, second.text
        again = second.json()
        assert again["status"] == "complete", again
        assert (
            _flatten_item(again, "option_exit", f"option_exit:{RUN_ID}")["state"]
            == "done"
        )
        assert _flatten_item(again, "nonoption_reduction")["state"] == "done"
        assert len(pipeline.executed) == 1
        assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_completion_requires_flat_evidence_and_no_in_flight_work(
    session_factory, monkeypatch
):
    """An execution that reported ``filled`` is not proof the BOOK is flat."""
    snapshot = _OneRunSnapshot(_one_run_row(status="exited"))
    run_store = _FakeRunStore({RUN_ID: _flat_run()})
    pipeline = _FakeReductionPipeline(session_factory, zero_book=False)
    runner = _ScriptedExitRunner(
        [
            {
                "status": "complete",
                "state": "flat",
                "run_status": "exited",
                "evidence_digest": "digest-flat",
                "items": [],
                "shorts_proven_closed": True,
                "withheld_hedges": [],
            }
        ]
    )

    async with _client(session_factory, monkeypatch, run_store=run_store) as client:
        strategy_id = await _create(client)
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    _seed_catalog(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        run_store=run_store,
        owned_work_snapshot_service=snapshot,
        owner_action_option_exit_runner=runner,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        first = await _post_flatten(client, strategy_id)
        body = first.json()
        assert body["status"] != "complete", body
        assert "books_zero" in body["missing"]
        assert body["done_conditions"]["books_zero"] is False
        # The executor reported ``filled`` but the BOOK did not move: the item is
        # not "done" on the command's word alone.
        unfinished = _flatten_item(body, "nonoption_reduction")
        assert unfinished["state"] == "blocked", unfinished
        assert unfinished["reason_code"] == "FLATTEN_REDUCTION_INCOMPLETE"
        assert unfinished["detail"]["remaining_quantity"] == 150

        # The fills land: the attributed book is flat, and only THEN is the
        # operation complete.
        with session_factory() as session:
            session.execute(
                text("UPDATE strategy_position_projection SET net_quantity = 0")
            )
            session.commit()
        second = await _post_flatten(client, strategy_id)
        again = second.json()
        assert again["status"] == "complete", again
        assert again["missing"] == []
        assert all(again["done_conditions"].values())
        assert len(pipeline.executed) == 1


@pytest.mark.parametrize(
    "target, expected_state, expected_reason",
    [(0, "done", None), (300, "blocked", "FLATTEN_PLAN_INCREASES_EXPOSURE")],
)
@pytest.mark.asyncio
async def test_a_target_zero_plan_admits_while_an_increasing_plan_refuses_before_admission(
    session_factory, monkeypatch, target, expected_state, expected_reason
):
    """Section 3 step 5: a flatten plan may only reduce, and it is gated first."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    _seed_catalog(session_factory)
    _seed_bound_run(session_factory, strategy_id=strategy_id)
    pipeline = _FakeReductionPipeline(session_factory, real_admission=True)

    async with _client(
        session_factory,
        monkeypatch,
        # The default planner freezes the plan through the proposal/compile path.
        owner_action_reduction_plan_builder=(
            None if target == 0 else _canned_reduction_builder(target=target)
        ),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await _post_flatten(client, strategy_id)
        assert response.status_code == 200, response.text
        body = response.json()

    item = _flatten_item(body, "nonoption_reduction")
    assert item["state"] == expected_state, item
    assert item["reason_code"] == expected_reason
    assert item["detail"]["attributed_open_quantity"] == 150
    if target == 0:
        # The REAL admission rule admitted the frozen target-zero plan, and the
        # execution closed the book.
        assert pipeline.admit_calls == 1
        assert item["detail"]["admission"]["admitted"] is True
        assert len(pipeline.executed) == 1
    else:
        # The exposure-increasing plan never reached admission at all.
        assert pipeline.admit_calls == 0
        assert pipeline.executed == []


async def _create_live_strategy(client, live_account, name="live-flatten"):
    created = await client.post(
        BASE,
        json={
            "name": name,
            "execution_mode": "live",
            "job_kind": "finite",
            "account_scope": live_account,
            "max_duration_s": 21600,
            "progress_deadline_s": 600,
            "stale_exit_policy": "exit_on_worker_stale",
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["strategy_id"]


@pytest.mark.asyncio
async def test_live_nonoption_flatten_cancels_pending_first_then_reduces(
    session_factory, monkeypatch
):
    """Section 3.5, live: pending entry cancelled first, then a reduce-only plan.

    The live book takes the SAME governed path the paper book does: the pipeline
    dispatches the frozen target-zero plan to the live executor, and the item is
    ``done`` only when the strategy's own attributed quantity is zero.
    """
    from backend.strategies.attribution_models import StrategyRunBinding  # noqa: F401

    live_account = "kite:liveuser"
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", f"{ACCOUNT},{live_account}")
    snapshot = _OneRunSnapshot(_one_run_row(status="exited"))
    run_store = _FakeRunStore({RUN_ID: _flat_run()})
    runner = _ScriptedExitRunner(
        [
            {
                "status": "complete",
                "state": "flat",
                "run_status": "exited",
                "evidence_digest": "digest-flat",
                "items": [],
                "shorts_proven_closed": True,
                "withheld_hedges": [],
            }
        ]
    )
    tcs_id, tcs = "NSE:TCS", "TCS"
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create_live_strategy(client, live_account)
    # A live attributed CNC book flatten must reduce to zero...
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
        environment="live",
        account=live_account,
    )
    # ...and a still-unresolved live entry that flatten must cancel FIRST.
    _seed_plan(
        session_factory,
        strategy_id=strategy_id,
        plan_id="plan-live-entry",
        resolved={
            "target_kind": "single_instrument",
            "legs": [
                {
                    "instrument_id": tcs_id,
                    "tradingsymbol": tcs,
                    "broker_symbol": tcs,
                    "side": "BUY",
                    "product": "CNC",
                    "quantity": 150,
                    "signed_quantity": 150,
                }
            ],
        },
        plan_kind="single_instrument",
        account=live_account,
    )
    _seed_live_claim(
        session_factory,
        strategy_id=strategy_id,
        plan_id="plan-live-entry",
        account=live_account,
        order_id="LIVE-ENTRY-1",
        quantity=150,
    )
    _seed_catalog(session_factory)
    _seed_catalog(
        session_factory,
        instrument_id=tcs_id,
        symbol=tcs,
        broker_token=738561,
        generation="gen-flatten-2",
    )
    events: list = []
    boundary = _FakeLiveCancelBoundary(session_factory, events=events)
    pipeline = _FakeReductionPipeline(session_factory, events=events)

    async with _client(
        session_factory,
        monkeypatch,
        run_store=run_store,
        owned_work_snapshot_service=snapshot,
        owner_action_option_exit_runner=runner,
        owner_action_broker_cancel=boundary,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await _post_flatten(client, strategy_id)
        assert response.status_code == 200, response.text
        body = response.json()
        # A resume preserves the finished cancel and the finished reduction.
        resumed = await _post_flatten(client, strategy_id)
        assert resumed.status_code == 200, resumed.text
        again = resumed.json()

    # The live pending entry was cancelled through the broker boundary...
    cancelled = _flatten_item(body, "cancel_pending", "cancel:plan-live-entry:1")
    assert cancelled["state"] == "done", cancelled
    assert cancelled["detail"]["outcome"] == "cancelled"
    assert boundary.calls == [(live_account, "LIVE-ENTRY-1")]
    # ...and the live non-option book was reduced by a target-zero plan that went
    # through the LIVE executor path, never the ``..._UNSUPPORTED`` refusal.
    reduction = _flatten_item(body, "nonoption_reduction")
    assert reduction["state"] == "done", reduction
    assert reduction["reason_code"] is None
    assert reduction["detail"]["target_quantity"] == 0
    assert pipeline.environments == ["live"]
    assert len(pipeline.executed) == 1
    # Cancel happened BEFORE the reduction.
    assert events == ["cancel:LIVE-ENTRY-1", "reduce:plan-flatten-eq"]
    # The broker cancel is proven, but the live claim itself is resolved only when
    # the outcome consumer ingests it, so flatten is honestly still ``in_progress``.
    assert body["status"] == "in_progress", body["items"]
    assert "no_live_unresolved_submission" in body["missing"]
    assert "books_zero" not in body["missing"]
    # The resume did not re-run either side effect.
    assert _flatten_item(again, "cancel_pending", "cancel:plan-live-entry:1")["state"] == (
        "done"
    )
    assert _flatten_item(again, "nonoption_reduction")["state"] == "done"
    assert len(pipeline.executed) == 1
    assert len(boundary.calls) == 1


@pytest.mark.asyncio
async def test_live_flatten_refuses_an_increasing_plan_before_admission(
    session_factory, monkeypatch
):
    """Live flatten may only reduce: an increasing plan never reaches admission."""
    live_account = "kite:liveuser"
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", f"{ACCOUNT},{live_account}")
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create_live_strategy(client, live_account, name="live-incr")
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
        environment="live",
        account=live_account,
    )
    _seed_catalog(session_factory)
    pipeline = _FakeReductionPipeline(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(target=300),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await _post_flatten(client, strategy_id)
        assert response.status_code == 200, response.text
        body = response.json()

    reduction = _flatten_item(body, "nonoption_reduction")
    assert reduction["state"] == "blocked", reduction
    assert reduction["reason_code"] == "FLATTEN_PLAN_INCREASES_EXPOSURE"
    assert pipeline.admit_calls == 0
    assert pipeline.executed == []


@pytest.mark.asyncio
async def test_job_stop_with_flatten_stops_then_flattens_and_reports_both(
    session_factory, monkeypatch
):
    """Stop-and-flatten: the job stops AND the strategy is flattened, both reported."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    job_id = _seed_job(session_factory, strategy_id=strategy_id, status="queued")
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    _seed_catalog(session_factory)
    pipeline = _FakeReductionPipeline(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await client.post(
            f"{BASE}/{strategy_id}/jobs/{job_id}/stop",
            json={"attempt": 1, "flatten": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["stop"]["state"] == "confirmed"
    assert body["flatten"]["started"] is True, body["flatten"]
    assert body["flatten"]["status"] == "complete", body["flatten"]
    assert body["flatten"]["operation_id"]
    assert _rows(
        session_factory,
        "SELECT status FROM strategy_jobs WHERE id = :id",
        {"id": job_id},
    )[0]["status"] == "stopped"
    assert len(pipeline.executed) == 1


@pytest.mark.asyncio
async def test_job_stop_without_flatten_never_flattens(session_factory, monkeypatch):
    """The default Stop keeps its stop-only contract."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    job_id = _seed_job(session_factory, strategy_id=strategy_id, status="queued")
    _seed_projection(
        session_factory,
        strategy_id=strategy_id,
        instrument_id=EQ_ID,
        product="CNC",
        net_quantity=150,
    )
    _seed_catalog(session_factory)
    pipeline = _FakeReductionPipeline(session_factory)

    async with _client(
        session_factory,
        monkeypatch,
        owner_action_reduction_plan_builder=_canned_reduction_builder(),
        owner_action_reduction_pipeline=pipeline,
    ) as client:
        response = await client.post(
            f"{BASE}/{strategy_id}/jobs/{job_id}/stop", json={"attempt": 1}
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["stop"]["state"] == "confirmed"
    assert body.get("flatten") is None
    assert pipeline.executed == []
    assert _rows(
        session_factory, "SELECT operation_id FROM strategy_flatten_operations"
    ) == []


@pytest.mark.asyncio
async def test_job_stop_with_flatten_reports_a_refusal_without_hiding_the_stop(
    session_factory, monkeypatch
):
    """A flatten refusal leaves the stop in place and names the refusal."""
    async with _client(session_factory, monkeypatch) as client:
        strategy_id = await _create(client)
    # A LAUNCHED running job is evaluation authority whose stop is not yet
    # PROVEN, so flatten refuses rather than racing a child that may still trade.
    job_id = _seed_job(
        session_factory,
        strategy_id=strategy_id,
        status="running",
        handoff_at=datetime.now(timezone.utc),
    )

    async with _client(session_factory, monkeypatch) as client:
        response = await client.post(
            f"{BASE}/{strategy_id}/jobs/{job_id}/stop",
            json={"attempt": 1, "flatten": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()

    # The stop WAS requested (durably) even though the flatten could not proceed.
    assert body["stop"]["state"] == "stopping"
    assert body["flatten"]["started"] is False
    assert body["flatten"]["rejection_reason"] == "FLATTEN_EVALUATION_ACTIVE"
    assert _rows(
        session_factory,
        "SELECT desired_state FROM strategy_jobs WHERE id = :id",
        {"id": job_id},
    )[0]["desired_state"] == "stopped"
