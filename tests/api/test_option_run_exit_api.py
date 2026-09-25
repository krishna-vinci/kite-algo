"""B2.6b S2: the owner exit API for ONE option run.

Bounded ``httpx`` ASGI transport over a minimal app that mounts the strategies
router and the owner-actions router (no lifespan, no background tasks), in the
style of ``tests/api/test_strategy_owner_actions_api.py``. The run side is the
REAL durable option-run store (its compare-and-set, its durable stage claims and
``StagedStructureExit``), against ``public.`` tables created under the
established ATTACH fixture, so the SQL the production path runs is the SQL these
tests exercise. Only the broker boundary is fake: no test places a real order.

Pinned properties:

- a clean ``entered`` run admits ONE stage of the derived structure exit - the
  shorts only, the hedge withheld until its short is PROVEN closed - and the run
  is left ``exiting``, never ``exited`` on acceptance;
- each gate refuses by its own name before anything moves: an unresolved stage
  (``OPTION_PROTECTIVE_EXIT_UNRESOLVED``), an adjust that is not provably
  finished (``OPTION_RUN_ADJUST_IN_FLIGHT``), unexplained fills
  (``OPTION_RUN_EVIDENCE_AMBIGUOUS``), a stale digest
  (``OPTION_RUN_EXIT_EVIDENCE_CHANGED``) and a missing live boundary
  (``OPTION_OWNER_EXIT_LIVE_UNAVAILABLE``);
- an unknown / released protection owner does NOT block a risk-reducing exit,
  and an ACTIVE owner row names the run the stage is attributed to (B2.4);
- a flat run completes as ``exited``, and the exit stays owner-scoped.

The multi-stage continuation (a working stage is WAITING, and a proven short
closure admits the hedge release) needs the ordinary fill reads, so it lives in
``tests/integration/test_option_run_owner_exit_postgres.py`` with the race.
"""

from __future__ import annotations

import os

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
from backend.api.routers import (  # noqa: E402
    strategy_owner_actions as owner_actions_router,
)
from backend.app.auth import AppUser  # noqa: E402
from backend.options.execution.durable_store import DurableOptionRunStore  # noqa: E402
from backend.options.execution.models import OptionRunCreateRequest  # noqa: E402
from backend.strategies import models  # noqa: F401,E402  (table registration)
from backend.workflows.repository import Base  # noqa: E402

BASE = "/api/strategies"
ACCOUNT = "kite:paper"
SHORT = "NIFTY26NOV22500CE"
HEDGE = "NIFTY26NOV21500PE"
WORKER_RUN = "worker-exit-1"
SUCCESSOR_RUN = "worker-exit-2"


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
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        # The durable run/edge/owner rows and the ordinary ingestion read are
        # ``public.``-qualified in production code, so the ATTACH is where they
        # must resolve here.
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
                released_at TEXT DEFAULT CURRENT_TIMESTAMP
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

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def account_policy(monkeypatch):
    """Authorize exactly the account these fixtures use (default-deny)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


class _FakePaperBoundary:
    """The paper runtime surface the exit's staged boundary calls, in memory."""

    def __init__(self) -> None:
        self.calls: list = []

    async def place_order(self, *, account_scope, order_payload, attribution):
        self.calls.append(
            {
                "account_id": str(account_scope),
                "order": dict(order_payload or {}),
                "attribution": dict(attribution or {}),
            }
        )
        return {"order": {"order_id": f"PAPER-EXIT-{len(self.calls)}"}}


def _app(session_factory, monkeypatch, user, *, run_store, paper=None):
    from backend.app import auth as auth_module

    monkeypatch.setattr(auth_module, "get_optional_app_user", lambda _request: user)
    app = FastAPI()
    app.include_router(strategies_router.router, prefix="/api")
    app.include_router(owner_actions_router.router, prefix="/api")
    app.dependency_overrides[strategies_router._strategies_db] = lambda: session_factory
    app.dependency_overrides[owner_actions_router._owner_actions_db] = lambda: (
        session_factory
    )
    app.state.strategies_session_factory = session_factory
    app.state.option_run_store = run_store
    if paper is not None:
        app.state.paper_runtime_service = paper
    return app


def _client(session_factory, monkeypatch, run_store, paper=None, username="admin"):
    user = AppUser(username=username, role="admin") if username else None
    app = _app(session_factory, monkeypatch, user, run_store=run_store, paper=paper)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _legs() -> list:
    return [
        {
            "leg_id": "leg_short",
            "tradingsymbol": SHORT,
            "transaction_type": "SELL",
            "quantity": 75,
            "exchange": "NFO",
            "product": "NRML",
        },
        {
            "leg_id": "leg_hedge",
            "tradingsymbol": HEDGE,
            "transaction_type": "BUY",
            "quantity": 75,
            "exchange": "NFO",
            "product": "NRML",
        },
    ]


def _trade(leg_id: str, side: str, quantity: int = 75, order_id: str | None = None) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT if leg_id == "leg_short" else HEDGE,
        **({"order_id": order_id} if order_id else {}),
    }


async def _strategy(client, name="option-exit") -> str:
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


def _seed_run(
    session_factory,
    *,
    strategy_id: str,
    option_run_id: str = "opt_run_exit_1",
    plan_id: str | None = None,
    status: str = "entered",
    trades: list | None = None,
    orders: list | None = None,
    environment: str = "paper",
    worker_run_id: str = WORKER_RUN,
) -> str:
    store = DurableOptionRunStore(session_factory=session_factory)
    plan_id = str(plan_id or f"plan-{option_run_id}")
    store.create_run(
        OptionRunCreateRequest(
            strategy_run_id=option_run_id,
            strategy_name=strategy_id,
            product="NRML",
            legs=_legs(),
            protection={"structure_digest": "digest-exit"},
            metadata={
                "strategy_id": strategy_id,
                "account_id": ACCOUNT,
                "execution_environment": environment,
                "worker_run_id": worker_run_id,
                "plan_id": plan_id,
                "source": "hosted_plan_execution",
            },
        )
    )
    run = store.get_run(option_run_id)
    run.status = status
    run.trades = list(trades or [])
    run.orders = list(orders or [])
    store.save_run(run)
    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.strategy_plan_option_runs "
                "(plan_id, option_run_id, worker_run_id, strategy_id, account_id, "
                " execution_environment, phase) VALUES (:plan, :run, :worker, "
                " :strategy, :account, :environment, 'entry')"
            ),
            {
                "plan": plan_id,
                "run": option_run_id,
                "worker": worker_run_id,
                "strategy": strategy_id,
                "account": ACCOUNT,
                "environment": environment,
            },
        )
        session.commit()
    return option_run_id


def _seed_adjust_edge(
    session_factory, *, strategy_id: str, option_run_id: str, finished: bool
) -> None:
    """One adjust edge on the run, plus that plan's own trail and paper outcome."""
    from backend.strategies.attribution_models import StrategyPlanExecutionEvent

    with session_factory() as session:
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.strategy_plan_option_runs "
                "(plan_id, option_run_id, worker_run_id, strategy_id, account_id, "
                " execution_environment, phase) VALUES ('plan-adjust', :run, :worker, "
                " :strategy, :account, :environment, 'adjust')"
            ),
            {
                "run": option_run_id,
                "worker": WORKER_RUN,
                "strategy": strategy_id,
                "account": ACCOUNT,
                "environment": "paper",
            },
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.paper_orders (account_scope, order_id, "
                " status, quantity, filled_quantity, pending_quantity) VALUES "
                "(:account, 'PAPER-ADJUST-1', :status, 75, :filled, :pending)"
            ),
            {
                "account": ACCOUNT,
                "status": "filled" if finished else "pending",
                "filled": 75 if finished else 0,
                "pending": 0 if finished else 75,
            },
        )
        session.add(
            StrategyPlanExecutionEvent(
                id="plan-adjust-1-0",
                plan_id="plan-adjust",
                step_no=1,
                event="submitted",
                actor_id="admin",
                detail={"side": "SELL"},
            )
        )
        if finished:
            session.add(
                StrategyPlanExecutionEvent(
                    id="plan-adjust-1-1",
                    plan_id="plan-adjust",
                    step_no=1,
                    event="filled",
                    paper_order_id="PAPER-ADJUST-1",
                    filled_quantity=75,
                    actor_id="admin",
                    detail={"tradingsymbol": SHORT},
                )
            )
        session.commit()


def _seed_owner_row(
    session_factory,
    *,
    option_run_id: str,
    owner_run_id: str,
    state: str = "active",
    action_state: str = "none",
) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "INSERT OR REPLACE INTO public.option_protection_owners "
                "(option_run_id, strategy_id, account_id, execution_environment, "
                " owner_run_id, owner_epoch, policy_version, policy, action_state, "
                " state) VALUES (:run, 'strategy', :account, 'paper', :owner, 1, "
                " 'v1', '{}', :action_state, :state)"
            ),
            {
                "run": option_run_id,
                "account": ACCOUNT,
                "owner": owner_run_id,
                "action_state": action_state,
                "state": state,
            },
        )
        session.commit()


def _seed_plan_trail(session_factory, *, plan_id: str, order_id: str) -> None:
    """One committed entry submission whose fill the run's trades do NOT carry."""
    from backend.strategies.attribution_models import StrategyPlanExecutionEvent

    with session_factory() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=f"{plan_id}-1-0",
                plan_id=plan_id,
                step_no=1,
                event="submitted",
                actor_id="admin",
                detail={"side": "SELL"},
            )
        )
        session.add(
            StrategyPlanExecutionEvent(
                id=f"{plan_id}-1-1",
                plan_id=plan_id,
                step_no=1,
                event="filled",
                paper_order_id=order_id,
                filled_quantity=75,
                actor_id="admin",
                detail={"tradingsymbol": SHORT},
            )
        )
        session.commit()


def _run_status(session_factory, option_run_id: str) -> str:
    store = DurableOptionRunStore(session_factory=session_factory)
    return str(store.get_run(option_run_id).status)


def _stage_claims(session_factory, option_run_id: str) -> list:
    """Distinct stage claims: one entry per ``(stage_digest, attempt)``.

    A stage is two records (the pre-send claim and its resolution), so counting
    rows would count one send twice.
    """
    store = DurableOptionRunStore(session_factory=session_factory)
    latest: dict = {}
    for row in store.get_run(option_run_id).orders:
        if not isinstance(row, dict) or not row.get("stage_digest"):
            continue
        latest[(str(row["stage_digest"]), int(row.get("attempt") or 1))] = dict(row)
    return list(latest.values())


def _exit_url(strategy_id: str, option_run_id: str) -> str:
    return f"{BASE}/{strategy_id}/option-runs/{option_run_id}/exit"


@pytest.mark.asyncio
async def test_a_clean_entered_run_admits_one_short_stage_with_the_hedge_withheld(
    session_factory, monkeypatch
):
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )

        inspection = await client.get(_exit_url(strategy_id, run_id))
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["status"] == "entered"
        assert body["state"] == "residual"
        assert body["adjust_owner_state"] == "finished"
        assert body["protective_stage_state"] == "resolved"
        assert body["shorts_proven_closed"] is False
        assert body["waiting_reason"] is None
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"])
            for row in body["close_plan"]
        ] == [(SHORT, "BUY", 75)]
        assert [row["reason"] for row in body["withheld_hedges"]] == [
            "short_not_proven_closed"
        ]

        accepted = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert payload["status"] == "accepted", payload
        assert payload["state"] == "residual"
        assert payload["run_status"] == "exiting"
        assert payload["refusal"] is None
        assert payload["audit_id"]
        assert [
            (row["tradingsymbol"], row["transaction_type"], row["quantity"], row["state"])
            for row in payload["items"]
        ] == [(SHORT, "BUY", 75, "submitted")]

    # Exactly one order, a short-covering BUY, stamped as the owner's own action.
    assert len(paper.calls) == 1
    order = paper.calls[0]["order"]
    assert order["tradingsymbol"] == SHORT
    assert order["transaction_type"] == "BUY"
    assert paper.calls[0]["attribution"]["entry_surface"] == "hosted_option_owner_exit"
    assert paper.calls[0]["attribution"]["source"] == "owner_discretionary_exit"

    # The run is EXITING, not exited, and carries the durable stage claim.
    assert _run_status(session_factory, run_id) == "exiting"
    claims = _stage_claims(session_factory, run_id)
    assert len(claims) == 1
    assert claims[0]["state"] == "submitted"
    assert [leg["tradingsymbol"] for leg in claims[0]["legs"]] == [SHORT]


@pytest.mark.asyncio
async def test_a_flat_entered_run_completes_the_exit_as_exited(session_factory, monkeypatch):
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            trades=[
                _trade("leg_short", "SELL"),
                _trade("leg_short", "BUY"),
                _trade("leg_hedge", "BUY"),
                _trade("leg_hedge", "SELL"),
            ],
        )
        inspection = await client.get(_exit_url(strategy_id, run_id))
        assert inspection.json()["state"] == "flat"

        completed = await client.post(
            _exit_url(strategy_id, run_id),
            json={
                "evidence_digest": inspection.json()["evidence_digest"],
                "reason": "owner_exit",
            },
        )
        assert completed.status_code == 200, completed.text
        payload = completed.json()
        assert payload["status"] == "complete"
        assert payload["run_status"] == "exited"
        assert payload["items"] == []

        # Idempotent: the terminal run reads complete, and a POST pinned to THAT
        # read is a no-op (the run's own status is part of the digest, so the
        # pre-completion digest is correctly stale).
        exhausted = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": payload["evidence_digest"], "reason": "owner_exit"},
        )
        assert exhausted.status_code == 409, exhausted.text
        assert (
            exhausted.json()["detail"]["rejection_reason"]
            == "OPTION_RUN_EXIT_EVIDENCE_CHANGED"
        )
        terminal = await client.get(_exit_url(strategy_id, run_id))
        assert terminal.json()["state"] == "flat"
        assert terminal.json()["status"] == "exited"
        again = await client.post(
            _exit_url(strategy_id, run_id),
            json={
                "evidence_digest": terminal.json()["evidence_digest"],
                "reason": "owner_exit",
            },
        )
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "complete"

    assert paper.calls == []
    assert _run_status(session_factory, run_id) == "exited"


@pytest.mark.asyncio
async def test_each_gate_refuses_by_its_own_name(session_factory, monkeypatch):
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)

        # 1. An unresolved protective stage owns the run.
        unresolved_run = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_unresolved",
            trades=[_trade("leg_short", "SELL")],
            orders=[
                {
                    "stage_digest": "abcdef1234567890",
                    "attempt": 1,
                    "state": "sending",
                    "account_id": ACCOUNT,
                    "legs": [],
                }
            ],
        )
        body = (await client.get(_exit_url(strategy_id, unresolved_run))).json()
        assert body["state"] == "ambiguous"
        assert body["protective_stage_state"] == "sending"
        refused = await client.post(
            _exit_url(strategy_id, unresolved_run),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"]
            == "OPTION_PROTECTIVE_EXIT_UNRESOLVED"
        )
        assert _run_status(session_factory, unresolved_run) == "entered"

        # 2. An adjust that cannot be proven finished.
        adjusting_run = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_adjusting",
            status="adjusting",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        _seed_adjust_edge(
            session_factory,
            strategy_id=strategy_id,
            option_run_id=adjusting_run,
            finished=False,
        )
        body = (await client.get(_exit_url(strategy_id, adjusting_run))).json()
        assert body["adjust_owner_state"] == "in_flight"
        refused = await client.post(
            _exit_url(strategy_id, adjusting_run),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"] == "OPTION_RUN_ADJUST_IN_FLIGHT"
        )
        assert _run_status(session_factory, adjusting_run) == "adjusting"

        # 3. Fills the run cannot attribute to its own legs.
        ambiguous_run = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_ambiguous",
            trades=[{"leg_id": "leg_ghost", "transaction_type": "BUY", "quantity": 75}],
        )
        body = (await client.get(_exit_url(strategy_id, ambiguous_run))).json()
        assert body["state"] == "ambiguous"
        refused = await client.post(
            _exit_url(strategy_id, ambiguous_run),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"] == "OPTION_RUN_EVIDENCE_AMBIGUOUS"
        )
        assert _run_status(session_factory, ambiguous_run) == "entered"

        # 3b. A trail fill the run's own trades do not account for.
        ledger_run = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_ledger",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        _seed_plan_trail(
            session_factory,
            plan_id=f"plan-{ledger_run}",
            order_id="PAPER-UNTRACKED-1",
        )
        body = (await client.get(_exit_url(strategy_id, ledger_run))).json()
        assert body["state"] == "ambiguous", body
        assert "ledger_incomplete" in body["reasons"]
        refused = await client.post(
            _exit_url(strategy_id, ledger_run),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"] == "OPTION_RUN_EVIDENCE_AMBIGUOUS"
        )
        assert _run_status(session_factory, ledger_run) == "entered"

        # 4. A digest that no longer describes the run.
        stale_run = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_stale",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        stale = (await client.get(_exit_url(strategy_id, stale_run))).json()[
            "evidence_digest"
        ]
        store.record_trades(stale_run, [_trade("leg_short", "BUY")])
        refused = await client.post(
            _exit_url(strategy_id, stale_run),
            json={"evidence_digest": stale, "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"]
            == "OPTION_RUN_EXIT_EVIDENCE_CHANGED"
        )
        assert _run_status(session_factory, stale_run) == "entered"

    assert paper.calls == []


@pytest.mark.asyncio
async def test_a_finished_adjust_owner_admits_the_exit(session_factory, monkeypatch):
    """The admit twin of the adjust gate: only an UNFINISHED owner blocks."""
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            status="adjusting",
            trades=[
                _trade("leg_short", "SELL", order_id="PAPER-ADJUST-1"),
                _trade("leg_hedge", "BUY"),
            ],
        )
        _seed_adjust_edge(
            session_factory, strategy_id=strategy_id, option_run_id=run_id, finished=True
        )
        body = (await client.get(_exit_url(strategy_id, run_id))).json()
        assert body["adjust_owner_state"] == "finished"
        accepted = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted"
        assert accepted.json()["run_status"] == "exiting"

    assert len(paper.calls) == 1
    assert paper.calls[0]["order"]["tradingsymbol"] == SHORT


@pytest.mark.asyncio
async def test_a_live_run_without_a_boundary_refuses_before_mutation(
    session_factory, monkeypatch
):
    store = DurableOptionRunStore(session_factory=session_factory)
    async with _client(
        session_factory, monkeypatch, store, _FakePaperBoundary()
    ) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_live",
            environment="live",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        body = (await client.get(_exit_url(strategy_id, run_id))).json()
        assert body["state"] == "residual"
        refused = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert refused.status_code == 409, refused.text
        assert (
            refused.json()["detail"]["rejection_reason"]
            == "OPTION_OWNER_EXIT_LIVE_UNAVAILABLE"
        )

    # Nothing moved and nothing was claimed: the refusal precedes the mutation.
    assert _run_status(session_factory, run_id) == "entered"
    assert _stage_claims(session_factory, run_id) == []


@pytest.mark.asyncio
async def test_an_unknown_or_released_owner_does_not_block_the_exit(
    session_factory, monkeypatch
):
    """A risk-reducing exit stays admissible when the owner row says nothing."""
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_released",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        _seed_owner_row(
            session_factory,
            option_run_id=run_id,
            owner_run_id=WORKER_RUN,
            state="released",
        )
        body = (await client.get(_exit_url(strategy_id, run_id))).json()
        accepted = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted"

    # With no ACTIVE owner row, the stage follows the run's own creation binding
    # (B2.4's reduce-only split).
    claims = _stage_claims(session_factory, run_id)
    assert claims and claims[0]["worker_run_id"] == WORKER_RUN


@pytest.mark.asyncio
async def test_the_exit_is_attributed_to_the_current_protection_owner(
    session_factory, monkeypatch
):
    """B2.4: after a handover the stage follows the owner ROW, not the snapshot."""
    store = DurableOptionRunStore(session_factory=session_factory)
    paper = _FakePaperBoundary()
    async with _client(session_factory, monkeypatch, store, paper) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_handover",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        _seed_owner_row(
            session_factory, option_run_id=run_id, owner_run_id=SUCCESSOR_RUN
        )
        body = (await client.get(_exit_url(strategy_id, run_id))).json()
        accepted = await client.post(
            _exit_url(strategy_id, run_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_exit"},
        )
        assert accepted.status_code == 200, accepted.text

    claims = _stage_claims(session_factory, run_id)
    assert claims and claims[0]["worker_run_id"] == SUCCESSOR_RUN


@pytest.mark.asyncio
async def test_a_foreign_strategy_is_never_reachable(session_factory, monkeypatch):
    store = DurableOptionRunStore(session_factory=session_factory)
    async with _client(
        session_factory, monkeypatch, store, _FakePaperBoundary()
    ) as client:
        strategy_id = await _strategy(client)
        run_id = _seed_run(
            session_factory,
            strategy_id=strategy_id,
            option_run_id="opt_run_foreign",
            trades=[_trade("leg_short", "SELL"), _trade("leg_hedge", "BUY")],
        )
        for url in (
            f"{BASE}/strategy-somebody-else/option-runs/{run_id}/exit",
            f"{BASE}/{strategy_id}/option-runs/opt_run_somebody_else/exit",
        ):
            response = await client.get(url)
            assert response.status_code == 404, response.text
