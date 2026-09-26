"""Hosted LIVE Phase 2B: the platform's OWN control-plane exit, executed.

Phase 2A established a boundary and stated it honestly: the hosted release pass
refuses to hand a dead child NEW authority, and the dead-child square-off is the
platform's own control-plane exit
(``background._worker_protection_loop`` -> ``WorkerProtectionRuntime`` ->
``submit_worker_protection_exit`` -> ``exit_control_strategy`` ->
``_exit_live_worker_run``). That claim was CITED, never EXECUTED.

This suite executes it. The runtime is built exactly as the background loop
builds it, the exit submitter is the production ``submit_worker_protection_exit``
seam, the control plane resolves the run through the real repository and the live
exit builds its orders from the strategy's own attributed book. Only the broker
write (and the broker reads the exit refresh needs) are faked - there is no real
broker in a disposable test database.

    RECONCILIATION_PG_ADMIN='postgresql://postgres:testonly@127.0.0.1:15433/postgres' \\
        .venv/bin/pytest tests/integration/test_hosted_live_phase2b_protection_postgres.py -q

Run this file in its own pytest process: ``tests/api/test_strategy_owner_and_binding.py``
installs a fake ``psycopg2`` at import, which breaks these PostgreSQL fixtures.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)

OWNER = "app:admin"
ACCOUNT = "kite:phase2b-protect"
TOKEN = 256265


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live_phase2b_prot_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, f"{PG_ADMIN.rpartition('/')[0]}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    try:
        os.environ["DATABASE_URL"] = dsn
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", dsn)
        cfg.set_main_option("script_location", "backend/alembic")
        command.upgrade(cfg, "head")
        engine = create_engine(dsn, poolclass=NullPool)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            yield {"dsn": dsn, "factory": factory, "engine": engine}
        finally:
            engine.dispose()
    finally:
        _drop_db(name)


class _BasketResult:
    """What the production orders service returns: a ``BasketOrderResponse``.

    ``OrdersService.place_basket`` answers with one ``results`` entry per requested
    leg, each carrying its own ``index`` and the broker's ``order_id`` ONLY when the
    broker accepted that leg. Modelling that exact shape (rather than a convenient
    stand-in) is what lets the support code under test parse the real thing.
    """

    def __init__(self, orders: list[dict]) -> None:
        #: One entry per requested leg, in order. A leg the broker refused is an
        #: entry with no order id - the caller must decide what that means, and the
        #: position in this list is how it is matched back to the leg.
        self.orders = orders

    def model_dump(self, mode: str = "python") -> dict:
        _ = mode
        results = []
        errors = []
        for index, row in enumerate(self.orders):
            row = dict(row or {})
            order_id = row.get("order_id")
            if not order_id:
                errors.append(
                    {
                        "index": index,
                        "tradingsymbol": row.get("tradingsymbol"),
                        "error": "broker returned no order id for this leg",
                    }
                )
            results.append(
                {
                    "index": index,
                    "tradingsymbol": row.get("tradingsymbol"),
                    "order_id": order_id,
                    "status": "success" if order_id else "failed",
                    "error": None if order_id else "broker returned no order id for this leg",
                }
            )
        return {
            "status": "success" if not errors else "partial",
            "results": results,
            "errors": errors,
        }


class _FakeBrokerBoundary:
    """The ONLY broker boundary: order acceptance plus the reads the exit needs."""

    def __init__(self, session_factory=None) -> None:
        self.session_factory = session_factory
        self.placed: list[dict] = []
        self.reconciliations = 0
        #: Leg indexes the boundary refuses to acknowledge (a broker rejection).
        self.reject_legs: set[int] = set()
        #: Set to a BaseException class to simulate a crash AFTER acceptance and
        #: BEFORE the caller records the outcome.
        self.crash_after_accept: type[BaseException] | None = None

    async def place_orders(self, *, orders, idempotency_key):
        """The staged structure exit's broker boundary: acceptance only."""
        ids = []
        for index, order in enumerate(orders, start=1):
            placed = {
                **dict(order),
                "order_id": f"OID-STAGE-{len(self.placed) + index}",
                "idempotency_key": idempotency_key,
            }
            ids.append(placed["order_id"])
            self.placed.append(placed)
        return ids

    async def place_basket(self, kite, request, corr_id, *, session_id, idempotency_key, response=None):
        """The production basket boundary: pre-send records, then per-leg answers.

        The platform's live order path writes a durable pre-send record for every
        leg BEFORE it calls the broker, and marks it with the broker's order id
        after the broker accepts. This stands in for that path's broker call only,
        so it has to write the same rows - they are what the platform's own fence
        reads after a crash.
        """
        _ = (kite, corr_id, response, session_id)
        from sqlalchemy import text

        if self.session_factory is not None:
            with self.session_factory() as session:
                for order in request.orders:
                    attribution = dict(getattr(order, "attribution", None) or {})
                    session.execute(
                        text(
                            "INSERT INTO public.live_order_intents "
                            "(intent_id, client_order_ref, account_id, strategy_run_id, "
                            " strategy_family, strategy_name, entry_surface, "
                            " idempotency_key, execution_mode, status) "
                            "VALUES (:iid, :ref, :account, :run, 'options_strategy', "
                            " 'phase2b', 'hosted_option_protection', :key, 'live', 'pending')"
                        ),
                        {
                            "iid": f"lint_{uuid.uuid4().hex[:8]}",
                            "ref": str(attribution.get("client_order_ref") or ""),
                            "account": str(attribution.get("account_ref") or ""),
                            "run": str(attribution.get("strategy_run_id") or ""),
                            "key": idempotency_key,
                        },
                    )
                session.commit()
        answers = []
        for index, order in enumerate(request.orders):
            if index in self.reject_legs:
                answers.append({})
                continue
            order_id = f"OID-STAGE-{len(self.placed) + 1}"
            self.placed.append(
                {
                    **dict(order),
                    "order_id": order_id,
                    "leg_index": index,
                    "idempotency_key": idempotency_key,
                }
            )
            answers.append({"order_id": order_id, "tradingsymbol": order.tradingsymbol})
            if self.session_factory is not None:
                from sqlalchemy import text as _text

                attribution = dict(getattr(order, "attribution", None) or {})
                with self.session_factory() as session:
                    session.execute(
                        _text(
                            "UPDATE public.live_order_intents "
                            "SET broker_order_id = :oid, status = 'placed' "
                            "WHERE client_order_ref = :ref"
                        ),
                        {
                            "oid": order_id,
                            "ref": str(attribution.get("client_order_ref") or ""),
                        },
                    )
                    session.commit()
        if self.crash_after_accept is not None:
            # The broker accepted; the caller dies before it can record that.
            raise self.crash_after_accept("process died after broker acceptance")
        return _BasketResult(answers)

    async def reconcile_account_positions(self, kite, account_id, corr_id):
        _ = (kite, corr_id)
        self.reconciliations += 1
        return {"account_id": account_id, "positions": 0}

    async def sync_dirty_orders(self, kite, service, batch_size=25):
        _ = (kite, service, batch_size)
        return 0


def _seed_live_run(
    factory,
    *,
    legs: list[dict],
    protection: dict,
    structure: dict | None = None,
    heartbeat_age_seconds: int = 3600,
):
    """A live run with an attributed book, a revoked child token and protection on.

    ``legs`` are the strategy's OWN book: ``{"symbol", "token", "product",
    "net_quantity"}`` each. Everything else (the run, the binding, the broker
    position for the one-sided exit guard) is the platform's ordinary row.
    """
    from sqlalchemy import text

    from backend.strategies import attribution as attribution_module
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name=f"phase2b-protect-{uuid.uuid4().hex[:6]}",
        description=None,
        execution_mode="live",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21_600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    strategy_id = str(strategy.id)
    run_id = f"run-phase2b-{uuid.uuid4().hex[:10]}"
    token_id = f"tok-phase2b-{uuid.uuid4().hex[:8]}"
    config = {"enabled": True, "mode": "exposure", "version": 1, "operations": protection}
    if structure is not None:
        config["structure"] = structure
    runtime_state = {"backend_protection": config}
    now = datetime.now(timezone.utc)
    heartbeat = now - timedelta(seconds=int(heartbeat_age_seconds))
    metadata = {"strategy_family": "options_strategy", "strategy_name": "phase2b-protect"}
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.algo_worker_tokens "
                "(token_id, name, token_hash, account_scope, allowed_modes, "
                " allowed_actions, status) "
                "VALUES (:tid, 'phase2b', :hash, :account, '[\"live\"]'::jsonb, "
                " '[\"runs:exit\"]'::jsonb, 'revoked')"
            ),
            {"tid": token_id, "hash": f"phase2b-{uuid.uuid4().hex}", "account": ACCOUNT},
        )
        session.execute(
            text(
                "INSERT INTO public.algo_worker_runs "
                "(strategy_run_id, token_id, template_id, account_scope, execution_mode, "
                " status, runtime_state_json, metadata_json, last_heartbeat_at) "
                "VALUES (:run, :tid, 'phase2b', :account, 'live', 'open', "
                " CAST(:runtime AS jsonb), CAST(:metadata AS jsonb), :heartbeat)"
            ),
            {
                "run": run_id,
                "tid": token_id,
                "account": ACCOUNT,
                "runtime": __import__("json").dumps(runtime_state),
                "metadata": __import__("json").dumps(metadata),
                "heartbeat": heartbeat,
            },
        )
        for leg in legs:
            session.execute(
                text(
                    "INSERT INTO public.strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, "
                    " identity_key, product, canonical_instrument_id, instrument_token, "
                    " exchange, tradingsymbol, net_quantity, projection_version) "
                    "VALUES (:account, :sid, 'live', 'canonical', :key, :product, "
                    " :iid, :token, 'NFO', :symbol, :qty, 1)"
                ),
                {
                    "account": ACCOUNT,
                    "sid": strategy_id,
                    "key": f"NFO:{leg['symbol']}",
                    "product": leg["product"],
                    "iid": str(uuid.uuid4()),
                    "token": int(leg["token"]),
                    "symbol": leg["symbol"],
                    "qty": int(leg["net_quantity"]),
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, product, exchange, tradingsymbol, "
                    " net_quantity) "
                    "VALUES (:account, :token, :product, 'NFO', :symbol, :qty)"
                ),
                {
                    "account": ACCOUNT,
                    "token": int(leg["token"]),
                    "product": leg["product"],
                    "symbol": leg["symbol"],
                    "qty": int(leg["net_quantity"]),
                },
            )
        session.commit()
    attribution_module.SqlAttributionStore(session_factory=factory).bind_run(
        strategy_run_id=run_id,
        strategy_id=strategy_id,
        owner_id=OWNER,
        account_id=ACCOUNT,
        execution_environment="live",
        bound_by="phase2b-test",
        binding_source="hosted_job",
    )
    return {"run_id": run_id, "strategy_id": strategy_id}


class _MoveableClock:
    """A clock the staged protocol can be advanced past its own throttles with."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=int(seconds))


def _protection_state(pg, run_id: str) -> dict:
    from sqlalchemy import text

    with pg["factory"]() as session:
        row = (
            session.execute(
                text(
                    "SELECT runtime_state_json FROM public.algo_worker_runs "
                    "WHERE strategy_run_id = :run"
                ),
                {"run": run_id},
            )
            .mappings()
            .first()
        )
    return dict((row["runtime_state_json"] or {}).get("backend_protection_state") or {})


def _runtime(
    factory,
    broker: _FakeBrokerBoundary,
    *,
    pnl_legs: list[dict],
    now_fn=None,
    orders_service=None,
    live_kite=None,
):
    """The production runtime, exactly as ``_worker_protection_loop`` builds it."""
    from types import SimpleNamespace

    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.api.services.protection_runtime import (
        WorkerProtectionRuntime,
        submit_worker_protection_exit,
    )

    repo = SqlAlchemyAlgoWorkerRepository(factory)
    # The control plane resolves the run through the app's OWN repository, exactly
    # as the production app does; without it the exit falls through to the paper
    # branch and refuses.
    # The production structure submitter places through the app's orders service;
    # here that service's broker call is the fake boundary, which also writes the
    # platform's durable pre-send records.
    if getattr(broker, "session_factory", None) is None:
        broker.session_factory = factory
    app = SimpleNamespace(
        state=SimpleNamespace(
            # ``broker`` is the normal stand-in; a test may inject the REAL orders
            # service instead and hand the boundary a fake broker client.
            algo_worker_orders_service=orders_service or broker,
            algo_worker_repository=repo,
        )
    )
    request = SimpleNamespace(headers={}, app=app, is_disconnected=lambda: False)

    async def _pnl(run, _request=request):
        _ = run
        return {"legs": list(pnl_legs)}

    runtime = WorkerProtectionRuntime(
        repo=repo,
        pnl_loader=_pnl,
        exit_submitter=lambda run, state: submit_worker_protection_exit(request, run, state),
        # The PRODUCTION structure submitter, over the real OrdersService boundary
        # (only the broker call inside it is faked, by app.state's orders service).
        structure_exit_submitter=lambda run, state: _production_structure_submitter(
            request, run, state, live_kite=live_kite
        ),
        now_fn=now_fn or (lambda: datetime.now(timezone.utc)),
        squareoff_schedule={"NFO:MIS": "15:25"},
    )
    return runtime, request


async def _production_structure_submitter(request, run, state, *, live_kite=None):
    """Call the REAL production submitter, with the account's broker session faked."""
    from backend.api.routers import worker_shared
    from backend.api.services.protection_runtime import (
        submit_worker_protection_structure_exit,
    )

    original = getattr(worker_shared, "_load_live_kite_for_account", None)
    worker_shared._load_live_kite_for_account = lambda scope: (
        live_kite if live_kite is not None else {"scope": scope}
    )
    try:
        return await submit_worker_protection_structure_exit(request, run, state)
    finally:
        if original is not None:
            worker_shared._load_live_kite_for_account = original


def _ingest_platform_order_fill(
    factory,
    *,
    order_id: str,
    trade_id: str,
    quantity: int,
    side: str,
    symbol: str,
    token: int,
    terminal: bool = True,
):
    """The ORDINARY ingestion artifact for a platform protection order's fill.

    This is exactly what the live ingest writes: a trade fact plus the order's
    projected state. Nothing here touches the option run - the staged exit is
    expected to read these rows and translate them itself.
    """
    from sqlalchemy import text

    status = "COMPLETE" if terminal else "OPEN"
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, "
                " instrument_token, exchange, tradingsymbol, product, transaction_type, "
                " quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, :tid, :oid, :token, 'NFO', :symbol, 'NRML', :side, "
                " :qty, 100.0, NOW(), true)"
            ),
            {
                "account": ACCOUNT,
                "tid": trade_id,
                "oid": order_id,
                "token": int(token),
                "symbol": symbol,
                "side": side,
                "qty": int(quantity),
            },
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, "
                " needs_reconcile, terminal, exchange, tradingsymbol, instrument_token, "
                " product, transaction_type, updated_at) "
                "VALUES (:account, :oid, :status, NOW(), :qty, false, false, :terminal, "
                " 'NFO', :symbol, :token, 'NRML', :side, NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = :status, "
                " last_seen_filled_quantity = :qty, terminal = :terminal"
            ),
            {
                "account": ACCOUNT,
                "oid": order_id,
                "status": status,
                "terminal": bool(terminal),
                "qty": int(quantity),
                "symbol": symbol,
                "token": int(token),
                "side": side,
            },
        )
        session.commit()


def _stage_order_id(broker, *, symbol: str, index: int = 0) -> str:
    """The order id the fake boundary gave to one staged leg, by symbol."""
    matches = [
        str(order["order_id"])
        for order in broker.placed
        if str(order.get("tradingsymbol")) == symbol
    ]
    assert matches, (symbol, broker.placed)
    return matches[index]


def _seed_option_run(
    factory,
    *,
    worker_run_id: str,
    strategy_id: str,
    short_symbol: str | None = None,
    hedge_symbol: str | None = None,
    legs: list[dict] | None = None,
    quantity: int = 75,
    status: str = "entered",
):
    """The durable option run bound to the worker run, with its OWN entry fills.

    This is the platform's own run edge (``create_run_from_frozen_plan`` writes
    ``metadata.worker_run_id``), not a test-only side channel: the staged exit is
    expected to size itself from exactly these rows.
    """
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest

    if legs is None:
        legs = [
            {
                "leg_id": "leg-short",
                "tradingsymbol": short_symbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "exchange": "NFO",
                "product": "NRML",
            },
            {
                "leg_id": "leg-hedge",
                "tradingsymbol": hedge_symbol,
                "transaction_type": "BUY",
                "quantity": quantity,
                "exchange": "NFO",
                "product": "NRML",
            },
        ]
    store = DurableOptionRunStore(session_factory=factory)
    run = store.create_run(
        OptionRunCreateRequest(
            strategy_name="phase2b-structure",
            product="NRML",
            legs=[dict(leg) for leg in legs],
            metadata={
                "worker_run_id": worker_run_id,
                "strategy_id": strategy_id,
                "account_id": ACCOUNT,
                "execution_environment": "live",
                "source": "hosted_plan_execution",
            },
        )
    )
    store.record_trades(
        run.strategy_run_id,
        [
            {
                "leg_id": str(leg["leg_id"]),
                "transaction_type": str(leg["transaction_type"]),
                "quantity": int(leg["quantity"]),
            }
            for leg in legs
        ],
    )
    if status != "created":
        from backend.options.execution.models import OptionRunState

        current = store.get_run(run.strategy_run_id)
        store.save_run(
            OptionRunState(
                strategy_run_id=current.strategy_run_id,
                strategy_name=current.strategy_name,
                product=current.product,
                legs=list(current.legs),
                protection=dict(current.protection or {}),
                metadata=dict(current.metadata),
                status=status,
                trades=list(current.trades),
                orders=list(current.orders),
            )
        )
    return store.get_run(run.strategy_run_id)


def _record_run_trade(factory, run_id: str, *, leg_id: str, side: str, quantity: int):
    from backend.options.execution.durable_store import DurableOptionRunStore

    DurableOptionRunStore(session_factory=factory).record_trades(
        run_id,
        [{"leg_id": leg_id, "transaction_type": side, "quantity": int(quantity)}],
    )


@pytest.fixture(autouse=True)
def _broker_reads(monkeypatch):
    """Fake only the broker READS the live exit refresh performs."""
    from backend.api.routers import worker_execution
    from backend.broker_api.orders import order_event_runtime, realtime_positions_service

    monkeypatch.setattr(
        worker_execution, "_load_live_kite_for_account", lambda scope: {"scope": scope}
    )

    async def _reconcile(kite, account_id, corr_id):
        _ = (kite, corr_id)
        return {"account_id": account_id, "positions": 0}

    async def _sync(kite, service, batch_size=25):
        _ = (kite, service, batch_size)
        return 0

    monkeypatch.setattr(realtime_positions_service, "reconcile_account_positions", _reconcile)
    monkeypatch.setattr(order_event_runtime, "sync_dirty_orders", _sync)


def test_mis_dead_child_squareoff_executes_through_the_control_plane(pg):
    """The platform's own exit, executed end to end, for a run whose child is dead.

    The hosted release pass refuses a risk-reducing leg once the child's
    credential is gone (pinned by the Phase 2A suite). This is the OTHER path, and
    the one that actually protects a MIS position whose worker died: the
    protection runtime evaluates the run's own protection config, triggers on the
    stale heartbeat, and submits an exit through the control plane's
    ``runs:exit`` authority to the broker boundary - sized from the strategy's own
    attributed book, in the MIS product, and nothing else.
    """
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26SEP25000CE",
                "token": TOKEN,
                "product": "MIS",
                "net_quantity": 75,
            }
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
    )
    runtime, request = _runtime(
        pg["factory"],
        broker,
        pnl_legs=[
            {
                "tradingsymbol": "NIFTY26SEP25000CE",
                "product": "MIS",
                "quantity": 75,
                "net_quantity": 75,
                "exchange": "NFO",
                "instrument_token": TOKEN,
            }
        ],
    )

    async def _run():
        return await runtime.evaluate_once()

    result = asyncio.run(_run())
    assert result["evaluated"] == 1, result
    assert result["triggered"] == 1, result
    assert result["errors"] == 0, result
    assert len(broker.placed) == 1, broker.placed
    order = broker.placed[0]
    assert order["transaction_type"] == "SELL", order
    assert int(order["quantity"]) == 75, order
    assert str(order["product"]).upper().endswith("MIS"), order
    assert order["tradingsymbol"] == "NIFTY26SEP25000CE", order
    assert order["attribution"]["strategy_run_id"] == seeded["run_id"], order["attribution"]
    assert order["attribution"]["execution_mode"] == "live", order["attribution"]
    assert order["attribution"]["account_ref"] == ACCOUNT, order["attribution"]

    from sqlalchemy import text

    with pg["factory"]() as session:
        row = (
            session.execute(
                text(
                    "SELECT status, runtime_state_json FROM public.algo_worker_runs "
                    "WHERE strategy_run_id = :run"
                ),
                {"run": seeded["run_id"]},
            )
            .mappings()
            .first()
        )
    assert row is not None
    state = row["runtime_state_json"]
    protection_state = dict(state.get("backend_protection_state") or {})
    assert protection_state.get("exit_submitted") is True, protection_state
    assert protection_state.get("triggered_rule") == "worker_stale", protection_state

    # A second evaluation with the same trigger must NOT exit twice.
    again = asyncio.run(_run())
    assert again["errors"] == 0, again
    assert len(broker.placed) == 1, broker.placed


def test_option_structure_protection_exit_executes_short_first(pg):
    """The same runtime, for a hedged structure: the protection actually acts.

    Two things are asserted, and they are different things on purpose.

    1. The structure-aware seam RUNS: ``StructureExitSubmission`` builds the
       structure's exits through the existing short-first builder, records its
       evidence (the short's close, and the hedge WITHHELD because no short
       closure is proven), and submits them over the production claim path. A
       protective rule that only recommends orders protects nothing, so the seam
       being entered and its claim being taken is the assertion.
    2. The control plane's live exit is a WHOLE-BOOK exit: it re-derives the
       strategy's own attributed legs and closes them as one basket, which is the
       existing worker-exit semantic for every lane. The broker therefore sees a
       close for the short AND a release for the hedge.

    The distinction matters and is stated rather than blurred: the structure's
    ordering rule governs the structure engine's own partial exits; the worker
    protection path liquidates the strategy's book, which leaves no leg behind.
    """
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26OCT25000CE",
                "token": 900001,
                "product": "NRML",
                "net_quantity": -75,
            },
            {
                "symbol": "NIFTY26OCT30000CE",
                "token": 900002,
                "product": "NRML",
                "net_quantity": 75,
            },
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
        structure={
            "structure_digest": "phase2b-structure-digest",
            "legs": [
                # SIGNED positions, exactly as the exit builder expects them: the
                # short is negative, its protective long positive.
                {"tradingsymbol": "NIFTY26OCT25000CE", "side": "SELL", "quantity": -75},
                {"tradingsymbol": "NIFTY26OCT30000CE", "side": "BUY", "quantity": 75},
            ],
            "closed_short_quantities": {},
        },
    )
    runtime, _request = _runtime(
        pg["factory"],
        broker,
        pnl_legs=[
            {
                "tradingsymbol": "NIFTY26OCT25000CE",
                "product": "NRML",
                "quantity": -75,
                "net_quantity": -75,
                "exchange": "NFO",
                "instrument_token": 900001,
            },
            {
                "tradingsymbol": "NIFTY26OCT30000CE",
                "product": "NRML",
                "quantity": 75,
                "net_quantity": 75,
                "exchange": "NFO",
                "instrument_token": TOKEN,
            }
        ],
    )

    order_run = _seed_option_run(
        pg["factory"],
        worker_run_id=seeded["run_id"],
        strategy_id=seeded["strategy_id"],
        short_symbol="NIFTY26OCT25000CE",
        hedge_symbol="NIFTY26OCT30000CE",
    )
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    def _placed():
        return [
            (
                str(order["tradingsymbol"]),
                str(order["transaction_type"]).rsplit(".", 1)[-1],
                int(order["quantity"]),
            )
            for order in broker.placed
        ]

    # -- stage 1: the SHORT closes, the hedge is NOT sold.
    first = asyncio.run(runtime.evaluate_once())
    assert first == {"evaluated": 1, "triggered": 1, "errors": 0}, first
    assert _placed() == [("NIFTY26OCT25000CE", "BUY", 75)], _placed()
    state = _protection_state(pg, seeded["run_id"])
    assert state.get("exit_submitted") is False, state
    structure_exit = dict(state.get("structure_exit") or {})
    assert structure_exit.get("submitted") is True, structure_exit
    assert structure_exit.get("complete") is False, structure_exit

    # -- duplicate / restart: the same evidence stage is never retransmitted.
    clock.advance(180)
    again = asyncio.run(runtime.evaluate_once())
    assert again["errors"] == 0, again
    assert _placed() == [("NIFTY26OCT25000CE", "BUY", 75)], _placed()

    # -- a PARTIAL short closure releases NOTHING of the hedge: under the selected
    #    release contract the hedge waits until the WHOLE short liability is
    #    proven closed. The short's own working order is not repeated either.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT25000CE"),
        trade_id="TR-STAGE-SHORT-1",
        quantity=30,
        side="BUY",
        symbol="NIFTY26OCT25000CE",
        token=900001,
        terminal=False,
    )
    clock.advance(180)
    partial = asyncio.run(runtime.evaluate_once())
    assert partial["errors"] == 0, partial
    assert _placed() == [("NIFTY26OCT25000CE", "BUY", 75)], _placed()

    # -- the short is now PROVEN flat by the run's own fills: the hedge releases.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT25000CE"),
        trade_id="TR-STAGE-SHORT-2",
        quantity=45,
        side="BUY",
        symbol="NIFTY26OCT25000CE",
        token=900001,
    )
    clock.advance(180)
    released = asyncio.run(runtime.evaluate_once())
    assert released["errors"] == 0, released
    assert _placed() == [
        ("NIFTY26OCT25000CE", "BUY", 75),
        ("NIFTY26OCT30000CE", "SELL", 75),
    ], _placed()
    # The hedge is released ONCE, for exactly what the run owned.
    hedge_sold = sum(
        int(order["quantity"])
        for order in broker.placed
        if str(order["tradingsymbol"]) == "NIFTY26OCT30000CE"
    )
    assert hedge_sold == 75, hedge_sold

    # -- the staged exit is complete only once the run's OWN fills say flat.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT30000CE", index=0),
        trade_id="TR-STAGE-HEDGE-1",
        quantity=75,
        side="SELL",
        symbol="NIFTY26OCT30000CE",
        token=900002,
    )
    clock.advance(180)
    finished = asyncio.run(runtime.evaluate_once())
    assert finished["errors"] == 0, finished
    assert _placed() == [
        ("NIFTY26OCT25000CE", "BUY", 75),
        ("NIFTY26OCT30000CE", "SELL", 75),
    ], _placed()
    state = _protection_state(pg, seeded["run_id"])
    assert state.get("exit_submitted") is True, state
    assert state.get("structure_exit_complete") is True, state

    # -- the durable run recorded each stage once, with its evidence digest.
    from backend.options.execution.durable_store import DurableOptionRunStore

    recorded = DurableOptionRunStore(session_factory=pg["factory"]).get_run(
        order_run.strategy_run_id
    )
    stages = [row for row in recorded.orders if row.get("stage_digest")]
    # TWO stages, each with its durable PRE-SEND claim followed by its outcome.
    assert len(stages) == 4, stages
    by_digest: dict = {}
    for row in stages:
        by_digest.setdefault(str(row["stage_digest"]), []).append(str(row["state"]))
    assert sorted(by_digest.values()) == [
        ["sending", "submitted"],
        ["sending", "submitted"],
    ], by_digest

    # -- the exit needed no child credential: the run's token is REVOKED.
    from sqlalchemy import text

    with pg["factory"]() as session:
        token_status = session.execute(
            text(
                "SELECT t.status FROM public.algo_worker_tokens t "
                "JOIN public.algo_worker_runs r ON r.token_id = t.token_id "
                "WHERE r.strategy_run_id = :run"
            ),
            {"run": seeded["run_id"]},
        ).scalar()
    assert str(token_status) == "revoked", token_status


def test_one_closed_short_does_not_unlock_the_hedge_of_another(pg):
    """The release gate is ALL short legs, not the one this hedge happens to sit on.

    Two shorts share one protective long. Closing the first one fully is NOT proof
    that the structure's liability is gone: the second short is still open, and
    releasing the hedge now would leave that short naked. The staged adapter
    therefore withholds the hedge until EVERY short it can answer for is proven
    closed, and only then sells the long once.
    """
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26OCT25000CE",
                "token": 900001,
                "product": "NRML",
                "net_quantity": -75,
            },
            {
                "symbol": "NIFTY26OCT26000CE",
                "token": 900003,
                "product": "NRML",
                "net_quantity": -75,
            },
            {
                "symbol": "NIFTY26OCT30000CE",
                "token": 900002,
                "product": "NRML",
                "net_quantity": 150,
            },
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
        structure={"structure_digest": "phase2b-multi-short", "legs": []},
    )
    option_run = _seed_option_run(
        pg["factory"],
        worker_run_id=seeded["run_id"],
        strategy_id=seeded["strategy_id"],
        legs=[
            {
                "leg_id": "leg-short-a",
                "tradingsymbol": "NIFTY26OCT25000CE",
                "transaction_type": "SELL",
                "quantity": 75,
                "exchange": "NFO",
                "product": "NRML",
            },
            {
                "leg_id": "leg-short-b",
                "tradingsymbol": "NIFTY26OCT26000CE",
                "transaction_type": "SELL",
                "quantity": 75,
                "exchange": "NFO",
                "product": "NRML",
            },
            {
                "leg_id": "leg-hedge",
                "tradingsymbol": "NIFTY26OCT30000CE",
                "transaction_type": "BUY",
                "quantity": 150,
                "exchange": "NFO",
                "product": "NRML",
            },
        ],
    )
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    first = asyncio.run(runtime.evaluate_once())
    assert first["errors"] == 0, first
    placed = [
        (str(order["tradingsymbol"]), str(order["transaction_type"]).rsplit(".", 1)[-1])
        for order in broker.placed
    ]
    # Both SHORTS are closed; the hedge is not touched.
    assert sorted(placed) == [
        ("NIFTY26OCT25000CE", "BUY"),
        ("NIFTY26OCT26000CE", "BUY"),
    ], placed

    # -- ONE short is fully closed. The hedge must still wait.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT25000CE"),
        trade_id="TR-MULTI-A",
        quantity=75,
        side="BUY",
        symbol="NIFTY26OCT25000CE",
        token=900001,
    )
    clock.advance(180)
    second = asyncio.run(runtime.evaluate_once())
    assert second["errors"] == 0, second
    assert len(broker.placed) == 2, broker.placed

    # -- the SECOND short closes too: now, and only now, the hedge releases ONCE.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT26000CE"),
        trade_id="TR-MULTI-B",
        quantity=75,
        side="BUY",
        symbol="NIFTY26OCT26000CE",
        token=900003,
    )
    clock.advance(180)
    third = asyncio.run(runtime.evaluate_once())
    assert third["errors"] == 0, third
    hedge_orders = [
        int(order["quantity"])
        for order in broker.placed
        if str(order["tradingsymbol"]) == "NIFTY26OCT30000CE"
    ]
    assert hedge_orders == [150], hedge_orders


def test_a_structure_run_with_unknown_attribution_is_refused(pg):
    """Unknown attribution must NOT fall back to a whole-book liquidation.

    A structure run whose durable option run cannot be resolved has no way to tell
    a hedge from a short. The generic path would sell the book as one basket and
    open the naked window the structure exists to avoid, so the platform places
    NOTHING and records the refusal - the sequencing is retained, and an operator
    resolves the attribution.
    """
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26OCT25000CE",
                "token": 900001,
                "product": "NRML",
                "net_quantity": -75,
            }
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
        structure={"structure_digest": "phase2b-unbound", "legs": []},
    )
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[])
    result = asyncio.run(runtime.evaluate_once())
    assert result["errors"] == 0, result
    assert broker.placed == [], broker.placed
    state = _protection_state(pg, seeded["run_id"])
    assert state.get("exit_submitted") is False, state
    structure_exit = dict(state.get("structure_exit") or {})
    assert structure_exit.get("reason") == "no_bound_option_run", structure_exit


def _single_short_env(pg):
    """One hedged structure: a short the protection must close first."""
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26OCT25000CE",
                "token": 900001,
                "product": "NRML",
                "net_quantity": -75,
            },
            {
                "symbol": "NIFTY26OCT30000CE",
                "token": 900002,
                "product": "NRML",
                "net_quantity": 75,
            },
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
        structure={"structure_digest": "phase2b-crash", "legs": []},
    )
    option_run = _seed_option_run(
        pg["factory"],
        worker_run_id=seeded["run_id"],
        strategy_id=seeded["strategy_id"],
        short_symbol="NIFTY26OCT25000CE",
        hedge_symbol="NIFTY26OCT30000CE",
    )
    return broker, seeded, option_run


def _stage_states(pg, run_id: str) -> list:
    from backend.options.execution.durable_store import DurableOptionRunStore

    run = DurableOptionRunStore(session_factory=pg["factory"]).get_run(run_id)
    return [
        (str(row.get("stage_digest")), str(row.get("state")))
        for row in run.orders
        if row.get("stage_digest")
    ]


def _latest_live_order_intent(pg) -> dict | None:
    """The platform's own PRE-SEND record for a structure-protection leg."""
    from sqlalchemy import text

    with pg["factory"]() as session:
        row = (
            session.execute(
                text(
                    "SELECT client_order_ref, broker_order_id, status "
                    "FROM public.live_order_intents "
                    "WHERE account_id = :account "
                    "AND entry_surface = 'hosted_option_protection' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"account": ACCOUNT},
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def test_a_crash_after_acceptance_never_becomes_a_second_or_larger_order(pg):
    """The durable pre-send claim is what makes a lost response safe.

    The process is killed AFTER the broker accepted and BEFORE the outcome was
    recorded: the run carries a ``sending`` claim with the exact per-leg intent.
    The next pass reconciles it from the platform's own pre-send records, records
    the acceptance, and places NOTHING - and because only OUTSTANDING quantity is
    netted, the working order cannot be re-sent later as a second (or doubled)
    close either.
    """
    broker, seeded, option_run = _single_short_env(pg)
    broker.crash_after_accept = KeyboardInterrupt
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime.evaluate_once())

    # The broker DID accept: the order exists and the claim says "sending".
    assert len(broker.placed) == 1, broker.placed
    states = _stage_states(pg, option_run.strategy_run_id)
    assert [state for _digest, state in states] == ["sending"], states

    # -- the broker's OWN pre-send record already names the accepted order, so the
    #    next pass ADOPTS that reference: the acceptance is recovered and NOTHING
    #    is re-sent. The resolution comes from durable FULL-COVERAGE evidence, not
    #    from elapsed time - the lease is still live here, and a pass with NO such
    #    evidence stays blocked instead of guessing (see
    #    test_a_paused_first_sender_cannot_be_declared_unsent).
    broker.crash_after_accept = None
    clock.advance(180)
    recovered = asyncio.run(runtime.evaluate_once())
    assert recovered["errors"] == 0, recovered
    assert len(broker.placed) == 1, broker.placed
    states = _stage_states(pg, option_run.strategy_run_id)
    assert [state for _digest, state in states] == ["sending", "submitted"], states

    # -- the short's fill arrives through ORDINARY ingestion, and the hedge is
    #    released ONCE, for exactly what the run owned.
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=_stage_order_id(broker, symbol="NIFTY26OCT25000CE"),
        trade_id="TR-CRASH-1",
        quantity=75,
        side="BUY",
        symbol="NIFTY26OCT25000CE",
        token=900001,
    )
    clock.advance(180)
    released = asyncio.run(runtime.evaluate_once())
    assert released["errors"] == 0, released
    assert (
        sum(
            1
            for order in broker.placed
            if str(order["tradingsymbol"]) == "NIFTY26OCT25000CE"
        )
        == 1
    ), broker.placed
    hedge = [
        int(order["quantity"])
        for order in broker.placed
        if str(order["tradingsymbol"]) == "NIFTY26OCT30000CE"
    ]
    assert hedge == [75], hedge

    # -- and a further pass changes nothing (no duplicate, no oversized close).
    clock.advance(180)
    again = asyncio.run(runtime.evaluate_once())
    assert again["errors"] == 0, again
    assert [
        int(order["quantity"])
        for order in broker.placed
        if str(order["tradingsymbol"]) == "NIFTY26OCT30000CE"
    ] == [75], broker.placed


def test_concurrent_protection_triggers_place_one_stage(pg):
    """Two runtime instances seeing the same trigger must not both submit.

    The exit claim is a compare-and-set on the run's protection state, so only one
    evaluation wins; the loser finds the claim taken and places nothing.
    """
    broker, seeded, option_run = _single_short_env(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)
    other, _request2 = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    async def _both():
        return await asyncio.gather(
            runtime.evaluate_once(), other.evaluate_once()
        )

    results = asyncio.run(_both())
    assert all(row["errors"] == 0 for row in results), results
    assert len(broker.placed) == 1, broker.placed
    stages = _stage_states(pg, option_run.strategy_run_id)
    assert len(stages) == 2, stages  # one claim + one outcome


def test_an_idless_leg_is_named_unknown_and_never_retried_on_a_guess(pg):
    """A leg with NO order reference is recorded, named, and NOT re-sent.

    A structure whose exit leg silently vanished is a structure left half-hedged,
    so the platform records the per-leg outcome and a blocker - but an idless leg is
    NOT proof that nothing was placed: the same answer comes back for an explicit
    refusal and for a failure AFTER the broker took the order. Retrying it could
    duplicate a live exit, so the stage stays UNKNOWN and keeps owning the send
    instead of opening a new attempt.
    """
    broker, seeded, option_run = _single_short_env(pg)
    broker.reject_legs = {0}
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    first = asyncio.run(runtime.evaluate_once())
    assert first["errors"] == 0, first
    assert broker.placed == [], broker.placed
    state = _protection_state(pg, seeded["run_id"])
    structure_exit = dict(state.get("structure_exit") or {})
    assert structure_exit.get("reason") == "stage_send_unknown", structure_exit
    assert structure_exit.get("blockers"), structure_exit
    assert state.get("exit_submitted") is False, state
    states = _stage_states(pg, option_run.strategy_run_id)
    assert [row[1] for row in states] == ["sending", "unknown"], states

    # -- the idless leg is NEVER re-sent on a guess, even once the broker would
    #    accept: no second order, no new attempt, no new claim.
    broker.reject_legs = set()
    clock.advance(180)
    second = asyncio.run(runtime.evaluate_once())
    assert second["errors"] == 0, second
    assert broker.placed == [], broker.placed
    assert [row[1] for row in _stage_states(pg, option_run.strategy_run_id)] == [
        "sending",
        "unknown",
    ], _stage_states(pg, option_run.strategy_run_id)


class _FakeAsyncRedis:
    """Just enough of the idempotency/limiter client for one live order path."""

    def __init__(self) -> None:
        self.values: dict = {}

    async def set(self, key, value, ex=None, nx=False, **kwargs):
        _ = (ex, kwargs)
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)

    async def eval(self, script, numkeys, *args):
        # The write limiter's reserve script: hand out the slot immediately.
        _ = (script, numkeys, args)
        return [0, 0, 0]


class _AcceptThenLoseResponseKite:
    """A broker that TAKES the order and then loses the response.

    ``place_order`` records the placement - so the order really does exist at the
    broker - and then raises, exactly like a socket timeout AFTER acceptance. That
    is the case ``OrdersService.place_order``'s generic ``except Exception``
    collapses into the same idless ``failed`` pre-send row an explicit refusal
    writes.
    """

    def __init__(self) -> None:
        self.placed: list = []
        self.access_token = "fake-access-token"
        self.api_key = "fake-api-key"

    def place_order(self, **params):
        self.placed.append(dict(params))
        raise TimeoutError("broker accepted the order, then the response was lost")


def test_a_post_acceptance_timeout_via_orders_service_is_never_retried(pg, monkeypatch):
    """The REAL OrdersService is IDLESS for a timeout AFTER the broker accepted.

    ``OrdersService.place_order`` catches a generic exception and calls
    ``mark_live_order_intent_failed`` - the SAME idless ``failed`` pre-send row an
    explicit refusal writes - and ``place_basket`` answers that leg with no order
    id. The staged exit must read that as UNKNOWN, not as "nothing was placed": a
    repeat pass sends NOTHING and starts no new claim, even though the platform's
    own order path is what lost the reference.
    """
    from backend.broker_api.orders.service import OrdersService

    broker, seeded, option_run = _single_short_env(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    live_kite = _AcceptThenLoseResponseKite()
    redis = _FakeAsyncRedis()
    # The production order path's own durable writes and its idempotency/limiter
    # client are redirected at the test database and a fake redis. Nothing else
    # about the service is stubbed.
    monkeypatch.setattr("backend.app.database.SessionLocal", pg["factory"])
    monkeypatch.setattr(
        "backend.broker_api.orders.live_order_intents.SessionLocal", pg["factory"]
    )
    monkeypatch.setattr("backend.broker_api.orders.service.get_redis", lambda: redis)
    service = OrdersService()
    # The cost preview would read the broker over the network; it degrades to
    # UNAVAILABLE on any error anyway, so keep it deterministic here.
    monkeypatch.setattr(service, "order_margins", lambda *args, **kwargs: [])
    monkeypatch.setattr(service, "charges_orders", lambda *args, **kwargs: [])

    runtime, _request = _runtime(
        pg["factory"],
        broker,
        pnl_legs=[],
        now_fn=clock,
        orders_service=service,
        live_kite=live_kite,
    )

    first = asyncio.run(runtime.evaluate_once())
    assert first["errors"] == 0, first
    # The broker DID take the order; the platform never got the reference back.
    assert len(live_kite.placed) == 1, live_kite.placed
    # The pre-send record the order path wrote is idless and marked failed.
    intent = _latest_live_order_intent(pg)
    assert intent is not None, "the production order path wrote no pre-send record"
    assert not intent.get("broker_order_id"), intent
    assert str(intent.get("status")) == "failed", intent
    # ...and it was NOT read as a refusal.
    states = _stage_states(pg, option_run.strategy_run_id)
    assert [state for _digest, state in states] == ["sending", "unknown"], states
    state = _protection_state(pg, seeded["run_id"])
    structure_exit = dict(state.get("structure_exit") or {})
    assert structure_exit.get("reason") == "stage_send_unknown", structure_exit
    assert state.get("exit_submitted") is False, state

    # -- the repeat pass must NOT send again: an idless ``failed`` is ambiguous,
    #    not a refusal, so no second physical order and no new stage claim.
    clock.advance(180)
    repeat = asyncio.run(runtime.evaluate_once())
    assert repeat["errors"] == 0, repeat
    assert len(live_kite.placed) == 1, live_kite.placed
    assert [state for _digest, state in _stage_states(pg, option_run.strategy_run_id)] == [
        "sending",
        "unknown",
    ], _stage_states(pg, option_run.strategy_run_id)


def test_a_fill_for_another_accounts_order_is_not_our_evidence(pg):
    """Broker order ids are account-scoped: a stranger's fill is not a closure.

    The same order id on another account must never close this run's short, or the
    hedge could be released against a position that is still open.
    """
    broker, seeded, option_run = _single_short_env(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)
    first = asyncio.run(runtime.evaluate_once())
    assert first["errors"] == 0, first
    assert len(broker.placed) == 1, broker.placed
    order_id = _stage_order_id(broker, symbol="NIFTY26OCT25000CE")

    # The SAME order id, on somebody else's account.
    from sqlalchemy import text

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, "
                " instrument_token, exchange, tradingsymbol, product, transaction_type, "
                " quantity, price, fill_timestamp, applied_to_position) "
                "VALUES ('kite:somebody-else', 'TR-FOREIGN', :oid, 900001, 'NFO', "
                " 'NIFTY26OCT25000CE', 'NRML', 'BUY', 75, 100.0, NOW(), true)"
            ),
            {"oid": order_id},
        )
        session.commit()

    clock.advance(180)
    after = asyncio.run(runtime.evaluate_once())
    assert after["errors"] == 0, after
    # The hedge is NOT released: this run's short is still open as far as its own
    # account's evidence goes.
    assert [
        order for order in broker.placed if str(order["tradingsymbol"]) == "NIFTY26OCT30000CE"
    ] == [], broker.placed


def test_concurrent_reconcilers_record_one_fill(pg):
    """Two reconcilers racing the same fill must write it exactly once.

    The dedup happens inside the run's row-locked transaction, not from a set read
    before the lock, so a double-booked fill (which could falsely prove a short
    closed) is impossible.
    """
    import threading

    from backend.options.protection.staged_exit import StagedStructureExit

    broker, seeded, option_run = _single_short_env(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)
    first = asyncio.run(runtime.evaluate_once())
    assert first["errors"] == 0, first
    order_id = _stage_order_id(broker, symbol="NIFTY26OCT25000CE")
    _ingest_platform_order_fill(
        pg["factory"],
        order_id=order_id,
        trade_id="TR-RACE-1",
        quantity=75,
        side="BUY",
        symbol="NIFTY26OCT25000CE",
        token=900001,
    )

    barrier = threading.Barrier(2)
    results: list = []

    def _reconcile():
        staged = StagedStructureExit(session_factory=pg["factory"])
        run = staged.resolve_run_for_worker_run(
            worker_run_id=seeded["run_id"], account_id=ACCOUNT
        )[0]
        barrier.wait(timeout=30)
        results.append(staged.reconcile_own_fills(run))

    threads = [threading.Thread(target=_reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    from backend.options.execution.durable_store import DurableOptionRunStore

    run = DurableOptionRunStore(session_factory=pg["factory"]).get_run(
        option_run.strategy_run_id
    )
    markers = [
        str(trade.get("stage_fill_id"))
        for trade in run.trades
        if trade.get("stage_fill_id")
    ]
    assert markers == [f"{order_id}:TR-RACE-1"], markers
    assert sum(int(row.get("recorded") or 0) for row in results) == 1, results


def test_a_paused_first_sender_cannot_be_declared_unsent(pg):
    """A sender paused between claiming and creating pre-send records is UNKNOWN.

    The first sender takes the stage claim and blocks BEFORE the broker boundary
    writes anything. A concurrent second sender must not win the claim, must not
    call the broker, and must not conclude "nothing was sent" from the missing
    pre-send records - independence from Redis idempotency is the point, so the
    assertion is on PHYSICAL broker calls.

    The clock is moved WELL PAST the claim's lease before the second pass: an
    expired lease is an OBSERVATION, never proof that the paused sender is gone,
    because there is no fence at the broker write it is about to make. The second
    pass must therefore still refuse, start NO new attempt, and place nothing; only
    when the paused sender actually resumes does exactly ONE physical order reach
    the broker.
    """
    import threading

    broker, seeded, option_run = _single_short_env(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    entered = threading.Event()
    release = threading.Event()
    original = broker.place_basket

    async def _paused_basket(*args, **kwargs):
        entered.set()
        release.wait(timeout=30)
        return await original(*args, **kwargs)

    broker.place_basket = _paused_basket
    outcomes: dict = {}

    def _first():
        outcomes["first"] = asyncio.run(runtime.evaluate_once())

    first_thread = threading.Thread(target=_first)
    first_thread.start()
    assert entered.wait(timeout=30), "the first sender never reached the broker"
    # The run now carries the first sender's live claim, and no pre-send record.
    states = _stage_states(pg, option_run.strategy_run_id)
    assert [state for _digest, state in states] == ["sending"], states

    # An expired lease proves NOTHING about a paused sender. Move the clock past
    # the 300s stage lease (and past the runtime's 60s re-claim guard): the second
    # pass must STILL refuse - no new attempt, no order - because the absence of a
    # pre-send record cannot be read as non-submission while the first sender is
    # only "expired", not proven gone.
    clock.advance(600)

    # -- a concurrent second sender: it must lose the claim AND call nothing.
    second, _request2 = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)
    lost = asyncio.run(second.evaluate_once())
    assert lost["errors"] == 0, lost
    state = _protection_state(pg, seeded["run_id"])
    structure_exit = dict(state.get("structure_exit") or {})
    # The second pass reaches the staged exit (the clock cleared the 60s guard) and
    # refuses by NAME: the missing pre-send record is UNKNOWN, not "never sent".
    assert structure_exit.get("reason") == "stage_send_unknown", structure_exit
    assert len(broker.placed) == 0, broker.placed
    # The first sender's claim is untouched: no second claim, no new attempt, no
    # resolution - an expired lease never hands the stage over.
    assert [state for _digest, state in _stage_states(pg, option_run.strategy_run_id)] == [
        "sending"
    ], _stage_states(pg, option_run.strategy_run_id)

    release.set()
    first_thread.join(timeout=60)
    assert outcomes["first"]["errors"] == 0, outcomes
    assert len(broker.placed) == 1, broker.placed


def _owner_row(factory, option_run_id: str) -> dict:
    from sqlalchemy import text

    with factory() as session:
        return dict(
            session.execute(
                text(
                    "SELECT option_run_id, owner_run_id, owner_epoch, action_state, "
                    " stage_digest, state FROM public.option_protection_owners "
                    "WHERE option_run_id = :run"
                ),
                {"run": option_run_id},
            )
            .mappings()
            .one()
        )


def _claim_owner(factory, option_run, worker_run_id: str) -> None:
    """The S1 entry hook's write, made directly: one ACTIVE owner row at epoch 1."""
    from backend.options.protection.ownership import (
        OptionProtectionOwnerStore,
        option_protection_policy_snapshot,
        option_protection_policy_version,
    )

    policy = option_protection_policy_snapshot({})
    OptionProtectionOwnerStore(session_factory=factory).claim(
        option_run, worker_run_id, policy, option_protection_policy_version(policy)
    )


def _set_worker_run_status(factory, run_id: str, status: str) -> None:
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "UPDATE public.algo_worker_runs SET status = :status "
                "WHERE strategy_run_id = :run"
            ),
            {"status": status, "run": run_id},
        )
        session.commit()


def _owned_structure_seed(pg):
    """A live structure whose worker run is CLOSED but whose owner row is not."""
    broker = _FakeBrokerBoundary()
    seeded = _seed_live_run(
        pg["factory"],
        legs=[
            {
                "symbol": "NIFTY26OCT25000CE",
                "token": 900001,
                "product": "NRML",
                "net_quantity": -75,
            },
            {
                "symbol": "NIFTY26OCT30000CE",
                "token": 900002,
                "product": "NRML",
                "net_quantity": 75,
            },
        ],
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
        structure={
            "structure_digest": "phase2b-owned-structure",
            "legs": [
                {"tradingsymbol": "NIFTY26OCT25000CE", "side": "SELL", "quantity": -75},
                {"tradingsymbol": "NIFTY26OCT30000CE", "side": "BUY", "quantity": 75},
            ],
            "closed_short_quantities": {},
        },
    )
    option_run = _seed_option_run(
        pg["factory"],
        worker_run_id=seeded["run_id"],
        strategy_id=seeded["strategy_id"],
        short_symbol="NIFTY26OCT25000CE",
        hedge_symbol="NIFTY26OCT30000CE",
    )
    _claim_owner(pg["factory"], option_run, seeded["run_id"])
    _set_worker_run_status(pg["factory"], seeded["run_id"], "closed")
    return broker, seeded, option_run


def _placed_orders(broker) -> list[tuple[str, str, int]]:
    return [
        (
            str(order["tradingsymbol"]),
            str(order["transaction_type"]).rsplit(".", 1)[-1],
            int(order["quantity"]),
        )
        for order in broker.placed
    ]


def test_a_closed_worker_run_still_protects_the_structure_it_owns(pg):
    """B2.4 S2a: the OWNER ROW - not the worker run's status - keeps protection on.

    The worker run here is CLOSED, so ``list_protection_enabled_runs`` cannot see
    it at all: before this slice a structure left protection by status change
    alone. The loop reads the owner row instead, and the staged exit still goes
    out over the platform's own risk-reducing authority.
    """
    broker, seeded, option_run = _owned_structure_seed(pg)
    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    result = asyncio.run(runtime.evaluate_once())

    assert result == {"evaluated": 1, "triggered": 1, "errors": 0}, result
    assert _placed_orders(broker) == [("NIFTY26OCT25000CE", "BUY", 75)], _placed_orders(broker)
    owner = _owner_row(pg["factory"], option_run.strategy_run_id)
    assert owner["state"] == "active"
    assert owner["owner_run_id"] == seeded["run_id"]
    # The stage is still OWED (the hedge waits for the short's proven closure), so
    # the row a gate reads says exactly that rather than "none".
    assert owner["action_state"] == "staging", owner
    assert owner["stage_digest"], owner


def test_a_released_owner_row_leaves_the_closed_run_unprotected(pg):
    """Twin: the same closed run, once the OPTION run reaches a terminal status."""
    from backend.options.execution.durable_store import DurableOptionRunStore

    broker, _seeded, option_run = _owned_structure_seed(pg)
    runs = DurableOptionRunStore(session_factory=pg["factory"])
    current = runs.get_run(option_run.strategy_run_id)
    current.status = "exited"
    runs.save_run(current)
    assert _owner_row(pg["factory"], option_run.strategy_run_id)["state"] == "released"

    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    result = asyncio.run(runtime.evaluate_once())

    assert result == {"evaluated": 0, "triggered": 0, "errors": 0}, result
    assert broker.placed == [], broker.placed
    # No stage was ever taken for this structure: the released row really did
    # stop the loop rather than only the counter.
    assert _stage_states(pg, option_run.strategy_run_id) == []


def _seed_handover_successor_run(factory, *, structure: dict, protection: dict) -> str:
    """The successor's own OPEN, structured worker run (B2.4 S3).

    This is the run ``hosted_lifecycle`` creates once the predecessor's
    continuation cleared its block: it carries the structure protection config
    seeded from the owner row's frozen policy, and it is the run the owner row
    names after the transfer.
    """
    import json as _json

    from sqlalchemy import text

    run_id = f"run-successor-{uuid.uuid4().hex[:10]}"
    token_id = f"tok-successor-{uuid.uuid4().hex[:8]}"
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.algo_worker_tokens "
                "(token_id, name, token_hash, account_scope, allowed_modes, "
                " allowed_actions, status) "
                "VALUES (:tid, 'successor', :hash, :account, '[\"live\"]'::jsonb, "
                " '[\"runs:exit\"]'::jsonb, 'revoked')"
            ),
            {"tid": token_id, "hash": f"successor-{uuid.uuid4().hex}", "account": ACCOUNT},
        )
        session.execute(
            text(
                "INSERT INTO public.algo_worker_runs "
                "(strategy_run_id, token_id, template_id, account_scope, execution_mode, "
                " status, runtime_state_json, metadata_json, last_heartbeat_at) "
                "VALUES (:run, :tid, 'phase2b', :account, 'live', 'open', "
                " CAST(:runtime AS jsonb), CAST(:metadata AS jsonb), :heartbeat)"
            ),
            {
                "run": run_id,
                "tid": token_id,
                "account": ACCOUNT,
                "runtime": _json.dumps(
                    {
                        "backend_protection": {
                            "enabled": True,
                            "mode": "exposure",
                            "version": 1,
                            "operations": protection,
                            "structure": structure,
                        }
                    }
                ),
                "metadata": _json.dumps(
                    {
                        "strategy_family": "options_strategy",
                        "strategy_name": "phase2b-successor",
                    }
                ),
                "heartbeat": datetime.now(timezone.utc) - timedelta(seconds=3600),
            },
        )
        session.commit()
    return run_id


def _transfer_owner(pg, option_run_id: str, successor_run_id: str) -> None:
    """The hosted-run creation's CAS, made directly: owner moves, policy does not."""
    from backend.options.protection.ownership import OptionProtectionOwnerStore

    store = OptionProtectionOwnerStore(session_factory=pg["factory"])
    current = store.read(option_run_id)
    store.transfer(
        option_run_id,
        successor_run_id,
        int(current["owner_epoch"]),
        dict(current["policy"] or {}),
        current["policy_version"],
    )


def _attributed_runs(pg) -> set:
    """The worker runs the platform's own pre-send records attribute orders to."""
    from sqlalchemy import text

    with pg["factory"]() as session:
        return {
            str(value or "")
            for value in session.execute(
                text(
                    "SELECT strategy_run_id FROM public.live_order_intents "
                    "WHERE account_id = :account "
                    "AND entry_surface = 'hosted_option_protection'"
                ),
                {"account": ACCOUNT},
            ).scalars()
        }


def test_a_protection_trigger_after_a_handover_resolves_the_successor(pg):
    """B2.4 S3: the trigger AFTER a handover must still find the structure.

    The predecessor is closed and the ACTIVE owner row names the successor, but the
    option run's CREATION binding (``metadata.worker_run_id``) still names the
    predecessor. Resolving from that binding finds nothing - the structure would be
    unprotected - so the staged exit resolves through the OWNER ROW and submits the
    short-first close, attributed to the successor.
    """
    broker, seeded, option_run = _owned_structure_seed(pg)
    structure = {
        "structure_digest": "phase2b-owned-structure",
        "legs": [
            {"tradingsymbol": "NIFTY26OCT25000CE", "side": "SELL", "quantity": -75},
            {"tradingsymbol": "NIFTY26OCT30000CE", "side": "BUY", "quantity": 75},
        ],
        "closed_short_quantities": {},
    }
    successor_run_id = _seed_handover_successor_run(
        pg["factory"],
        structure=structure,
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
    )
    _transfer_owner(pg, option_run.strategy_run_id, successor_run_id)

    # The creation binding still names the PREDECESSOR: metadata alone is stale.
    from backend.options.execution.durable_store import DurableOptionRunStore

    bound = DurableOptionRunStore(session_factory=pg["factory"]).get_run(
        option_run.strategy_run_id
    )
    assert str(dict(bound.metadata or {}).get("worker_run_id") or "") == seeded["run_id"]
    assert str(_owner_row(pg["factory"], option_run.strategy_run_id)["owner_run_id"]) == (
        successor_run_id
    )

    clock = _MoveableClock(datetime.now(timezone.utc))
    runtime, _request = _runtime(pg["factory"], broker, pnl_legs=[], now_fn=clock)

    result = asyncio.run(runtime.evaluate_once())

    assert result == {"evaluated": 1, "triggered": 1, "errors": 0}, result
    assert _placed_orders(broker) == [("NIFTY26OCT25000CE", "BUY", 75)], _placed_orders(
        broker
    )
    # ... and the platform's OWN pre-send record attributes the close to the
    # successor, so the exit was resolved through the owner row (B2.4).
    assert _attributed_runs(pg) == {successor_run_id}, _attributed_runs(pg)
    owner = _owner_row(pg["factory"], option_run.strategy_run_id)
    assert owner["state"] == "active"
    assert owner["owner_run_id"] == successor_run_id
    assert owner["action_state"] == "staging", owner


def test_a_superseded_worker_run_cannot_resolve_after_a_handover(pg):
    """Twin: the run the structure moved AWAY from must not act for it.

    The predecessor's creation binding still matches the option run, so a
    metadata-only resolution would let a superseded run submit a close. The active
    owner row is the authority, and a binding that contradicts it is refused by
    name rather than acted on.
    """
    from backend.options.protection.staged_exit import (
        BINDING_CONFLICT,
        StagedStructureExit,
    )

    _broker, seeded, option_run = _owned_structure_seed(pg)
    successor_run_id = _seed_handover_successor_run(
        pg["factory"],
        structure={"structure_digest": "phase2b-owned-structure", "legs": []},
        protection={"exit_on_worker_stale": True, "worker_stale_sec": 60},
    )
    staged = StagedStructureExit(session_factory=pg["factory"])

    # BEFORE the handover the predecessor - and only the predecessor - resolves.
    run, resolution = staged.resolve_run_for_worker_run(
        worker_run_id=seeded["run_id"], account_id=ACCOUNT
    )
    assert str(run.strategy_run_id) == option_run.strategy_run_id
    assert resolution["reason"] == "ok"
    assert resolution["source"] == "protection_owner"
    absent, absent_resolution = staged.resolve_run_for_worker_run(
        worker_run_id=successor_run_id, account_id=ACCOUNT
    )
    assert absent is None
    assert absent_resolution["reason"] == "no_bound_option_run"

    _transfer_owner(pg, option_run.strategy_run_id, successor_run_id)

    # AFTER the handover the successor resolves ...
    run, resolution = staged.resolve_run_for_worker_run(
        worker_run_id=successor_run_id, account_id=ACCOUNT
    )
    assert str(run.strategy_run_id) == option_run.strategy_run_id
    assert resolution["source"] == "protection_owner"
    # ... and the superseded predecessor is refused BY NAME, never handed the run.
    stale, stale_resolution = staged.resolve_run_for_worker_run(
        worker_run_id=seeded["run_id"], account_id=ACCOUNT
    )
    assert stale is None
    assert stale_resolution["reason"] == BINDING_CONFLICT


def test_two_active_owner_rows_for_one_worker_run_are_ambiguous(pg):
    """One worker run naming two structures is AMBIGUOUS, never "pick one"."""
    from backend.options.protection.staged_exit import StagedStructureExit

    _broker, seeded, option_run = _owned_structure_seed(pg)
    second = _seed_option_run(
        pg["factory"],
        worker_run_id=seeded["run_id"],
        strategy_id=seeded["strategy_id"],
        short_symbol="NIFTY26OCT26000CE",
        hedge_symbol="NIFTY26OCT31000CE",
    )
    _claim_owner(pg["factory"], second, seeded["run_id"])

    staged = StagedStructureExit(session_factory=pg["factory"])
    run, resolution = staged.resolve_run_for_worker_run(
        worker_run_id=seeded["run_id"], account_id=ACCOUNT
    )

    assert run is None
    assert resolution["reason"] == "option_run_ambiguous"
    assert resolution["source"] == "protection_owner"
    assert sorted(resolution["candidates"]) == sorted(
        [option_run.strategy_run_id, second.strategy_run_id]
    )


def test_an_unreadable_owner_table_refuses_rather_than_falling_back():
    """Fail closed: the record that MOVED at a handover is never guessed at.

    The creation binding here would happily resolve (it is queried second), so a
    silent fallback would look like success while resolving a superseded owner.
    """
    from backend.options.protection.staged_exit import (
        OWNER_UNREADABLE,
        StagedStructureExit,
    )

    class _UnreadableOwnerTable:
        def __call__(self):
            raise RuntimeError("owner table unavailable")

    run, resolution = StagedStructureExit(
        session_factory=_UnreadableOwnerTable()
    ).resolve_run_for_worker_run(worker_run_id="run-some-worker-run", account_id=ACCOUNT)

    assert run is None
    assert resolution["reason"] == OWNER_UNREADABLE
    assert resolution["worker_run_id"] == "run-some-worker-run"
