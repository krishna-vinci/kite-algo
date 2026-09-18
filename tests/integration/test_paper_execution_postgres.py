"""Paper plan execution on PostgreSQL (Project 6): the end-to-end proof.

Why PostgreSQL: this suite proves what SQLite structurally cannot — the
insert-only trigger on ``strategy_plan_execution_events``, the plan's advisory
lock serializing two concurrent executes into exactly ONE submission, the G1
paper fold attributing real ``paper_trades`` rows (written by the REAL paper
runtime repository) to the strategy's paper book, the consumed reservation's
capacity arithmetic, and a settled assessment going detectably stale when new
work appears.

The acceptance test is the D-1 chain exercised end to end, on paper only:
hosted proposal → validated frozen plan → paper admission + reservation →
owner-triggered execution through the existing paper runtime → attributed paper
fills → G1 projection shows the strategy's book → reservation consumed →
settlement assessment recorded.

Every test runs against a DISPOSABLE, uniquely named database created on the
test server, upgraded with ``alembic upgrade head`` and dropped afterwards. No
existing database is ever touched, and no real order is ever placed (the paper
runtime is a simulator).

    PAPER_EXEC_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_paper_execution_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import asyncio
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
import uuid

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.admission import AdmissionService  # noqa: E402
from backend.strategies.attribution import (  # noqa: E402
    SqlAttributionStore,
    StrategyAttributionService,
)
from backend.strategies.execution import ExecutionRefusal, PaperPlanExecutor  # noqa: E402
from backend.strategies.compiler.base import PinnedCatalogRead  # noqa: E402
from backend.strategies.proposals import ProposalStore, ProposalSubmission  # noqa: E402
from backend.strategies.reservations import (  # noqa: E402
    CapacityExceeded,
    ClaimRequest,
    ReservationLedger,
)
from backend.strategies.settlement import ExecutionBarrier, SettlementService  # noqa: E402

PG_URL = os.environ.get("PAPER_EXEC_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "PAPER_EXEC_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

HEAD = "20260917_000030"
PRIOR_HEAD = "20260917_000029"

ACCOUNT = "kite:paper-a"
STRATEGY = "stg-1"
OWNER = "app:o"
RUN_ID = "run-1"
TOKEN = 738561
PRICE = 1500.0


# ---------------------------------------------------------------------------
# disposable database
# ---------------------------------------------------------------------------


def _parts():
    from urllib.parse import urlsplit

    return urlsplit(PG_URL)


def _url_for(dbname: str) -> str:
    from urllib.parse import urlunsplit

    return urlunsplit(_parts()._replace(path=f"/{dbname}"))


def _admin_engine():
    return create_engine(_url_for("postgres"), pool_pre_ping=True)


def _create_database(dbname: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        admin.dispose()


def _drop_database(dbname: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                ),
                {"dbname": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _upgrade(db_url: str, revision: str = "head") -> None:
    """Run alembic against the disposable DSN.

    ``backend/alembic/env.py`` unconditionally overrides ``sqlalchemy.url`` with
    ``get_database_url()``, so the disposable DSN must be exported for the
    duration of the upgrade — otherwise the migration would target the ambient
    database.
    """
    original_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", "backend/alembic")
    try:
        command.upgrade(cfg, revision)
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url


@pytest.fixture()
def disposable_db():
    dbname = f"kite_pexec_{uuid.uuid4().hex[:12]}"
    _create_database(dbname)
    db_url = _url_for(dbname)
    engine = create_engine(db_url, pool_pre_ping=True)
    _upgrade(db_url, "head")
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()
        _drop_database(dbname)


@pytest.fixture()
def prior_head_db():
    """A disposable database upgraded only to the pre-execution head."""
    dbname = f"kite_pexec_pre_{uuid.uuid4().hex[:10]}"
    _create_database(dbname)
    db_url = _url_for(dbname)
    engine = create_engine(db_url, pool_pre_ping=True)
    _upgrade(db_url, PRIOR_HEAD)
    try:
        yield db_url
    finally:
        engine.dispose()
        _drop_database(dbname)


@pytest.fixture()
def paper_env():
    """The paper runtime isolated from the outside world: no Redis events.

    The ORDER repository is the REAL PostgreSQL one, so every fill lands in the
    durable ``paper_orders``/``paper_trades`` tables the G1 paper fold reads.
    """
    with patch("backend.paper_runtime.service.publish_event", autospec=True):
        yield


def _paper_service(sf):
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService

    return PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory=sf),
        instruments_repository=_CatalogInstrument(),
        market_data_runtime=_TickRuntime(PRICE),
        default_starting_balance=Decimal("100000"),
    )


class _CatalogInstrument:
    """Enough catalog truth for the paper runtime to price the pinned leg."""

    def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
        if tradingsymbol != "RELIANCE":
            return None
        return {
            "instrument_token": TOKEN,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "lot_size": 1,
            "instrument_type": "EQ",
            "last_price": PRICE,
        }


class _TickRuntime:
    def __init__(self, last_price: float) -> None:
        self.last_price = last_price

    async def get_tick(self, token):
        return {"instrument_token": token, "last_price": self.last_price}

    async def get_last_price(self, token):
        return self.last_price


# ---------------------------------------------------------------------------
# seed helpers
# ---------------------------------------------------------------------------


def _exec(sf, sql, params=None):
    with sf() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).scalar()


def _rows(sf, sql, params=None):
    with sf() as session:
        return session.execute(text(sql), params or {}).fetchall()


def seed_strategy(sf, *, sid=STRATEGY, owner=OWNER, account=ACCOUNT):
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, 'Paper exec', :account, 'active')",
        {"sid": sid, "owner": owner, "account": account},
    )
    return sid


def seed_run_binding(sf, *, run_id=RUN_ID, sid=STRATEGY, owner=OWNER, account=ACCOUNT, env="paper"):
    _exec(
        sf,
        "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope, status) "
        "VALUES ('tok-1', 't', 'hash', :account, 'active') ON CONFLICT DO NOTHING",
        {"account": account},
    )
    _exec(
        sf,
        "INSERT INTO public.algo_worker_runs "
        "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
        "VALUES (:run_id, 'tok-1', 'tmpl', :account, :env, 'open')",
        {"run_id": run_id, "account": account, "env": env},
    )
    SqlAttributionStore(session_factory=sf).bind_run(
        strategy_run_id=run_id,
        strategy_id=sid,
        owner_id=owner,
        account_id=account,
        execution_environment=env,
        bound_by="test",
        binding_source="hosted_job",
    )


def seed_catalog(sf, *, token=TOKEN, instrument_type="EQ"):
    """One published generation, one active record, one mapping."""
    gen = str(uuid.uuid4())
    instrument = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:gen, 'published', '2024-01-01T00:00:00+00:00')",
        {"gen": gen},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_records "
        "(instrument_id, identity_key, public_key, exchange, tradingsymbol, instrument_type, "
        " lot_size, lifecycle_status, current_generation_id) "
        "VALUES (:iid, :ikey, :pkey, 'NSE', 'RELIANCE', :kind, 1, 'active', :gen)",
        {"iid": instrument, "ikey": f"NSE:RELIANCE:{instrument[:8]}", "pkey": "NSE:RELIANCE", "kind": instrument_type, "gen": gen},
    )
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, is_current) "
        "VALUES (:iid, 'kite', 'NSE', 'RELIANCE', :token, :gen, true)",
        {"iid": instrument, "token": token, "gen": gen},
    )
    return gen, instrument


def submit_plan(sf, *, target_quantity=10, sid=STRATEGY, account=ACCOUNT, run_id=RUN_ID,
                evaluation_id="eval-1", target_kind="single_instrument", legs=None):
    """The real P3 flow: a hosted submission validated into a frozen plan."""
    store = ProposalStore(session_factory=sf)
    payload = {
        "catalog_generation": PinnedCatalogRead(sf).pin(),
        "instrument_token": TOKEN,
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "product": "CNC",
        "target_quantity": target_quantity,
        "reference_price": PRICE,
    }
    if legs is not None:
        payload = {"catalog_generation": payload["catalog_generation"], "legs": legs}
    return store, store.submit(
        ProposalSubmission(
            strategy_id=sid,
            account_id=account,
            evaluation_id=evaluation_id,
            evaluation_kind="run_now",
            strategy_run_id=run_id,
            target_kind=target_kind,
            payload=payload,
        )
    )


def admit_and_reserve(sf, plan, *, environment="paper", valid_seconds=3600, allocation=None):
    """Paper admission (P4) and the durable capacity claim it produces."""
    service = AdmissionService(session_factory=sf)
    if allocation is not None:
        service.upsert_policy(
            strategy_id=str(plan["strategy_id"]),
            account_id=str(plan["account_id"]),
            allocation_inr=float(allocation),
            updated_by=OWNER,
        )
    margin = None
    if environment == "live":
        # Authoritative live margin evidence, fresh (the owner route fetches
        # this from the broker; the verdict must not depend on the network).
        margin = {"usable": 10_000_000.0, "as_of": datetime.now(timezone.utc)}
    verdict = service.evaluate(plan, execution_environment=environment, margin_evidence=margin)
    assert verdict.admitted, verdict.as_dict()
    policy = service.policy_for(str(plan["strategy_id"])) or {}
    ledger = ReservationLedger(session_factory=sf)
    return ledger, ledger.claim(
        ClaimRequest(
            plan_id=str(plan["plan_id"]),
            strategy_id=str(plan["strategy_id"]),
            account_id=str(plan["account_id"]),
            evaluation_id="eval-1",
            execution_environment=environment,
            requirement_inr=float(verdict.detail.get("plan_requirement_inr") or 0.0),
            valid_until=datetime.now(timezone.utc) + timedelta(seconds=valid_seconds),
            allocation_inr=policy.get("allocation_inr"),
            margin_evidence=margin,
            margin_as_of=(margin or {}).get("as_of"),
            actor_id=OWNER,
        )
    )


def build_executor(sf, paper_service):
    return PaperPlanExecutor(session_factory=sf, paper_service=paper_service)


def trail(sf, plan_id, *, step_no=None):
    clause = "AND step_no = :step" if step_no is not None else ""
    params = {"plan_id": plan_id, "step": step_no}
    return _rows(
        sf,
        f"SELECT step_no, event, paper_order_id, filled_quantity, refusal_reason "
        f"FROM public.strategy_plan_execution_events WHERE plan_id = :plan_id {clause} "
        f"ORDER BY created_at, step_no, id",
        params,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. schema: head, prior-head upgrade, insert-only trigger
# ---------------------------------------------------------------------------


def test_head_and_plan_execution_schema_shape(disposable_db):
    version = _scalar(disposable_db, "SELECT version_num FROM public.alembic_version")
    assert version >= HEAD  # monotonic: never an exact pin

    columns = {row[0] for row in _rows(disposable_db, "SELECT column_name FROM information_schema.columns WHERE table_name = 'strategy_plan_execution_events'")}
    assert {
        "id", "plan_id", "step_no", "event", "paper_order_id", "filled_quantity",
        "refusal_reason", "actor_id", "detail", "created_at",
    } <= columns

    triggers = {
        row[0]
        for row in _rows(
            disposable_db,
            "SELECT tgname FROM pg_trigger WHERE tgname = 'trg_strategy_plan_execution_events_immutable'",
        )
    }
    assert triggers == {"trg_strategy_plan_execution_events_immutable"}


def test_upgrade_from_prior_head_creates_the_trail_and_widens_the_vocabulary(prior_head_db):
    _upgrade(prior_head_db, "head")
    engine = create_engine(prior_head_db, pool_pre_ping=True)
    try:
        sf = sessionmaker(bind=engine)

        def _constraint(name):
            return _scalar(
                sf,
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name",
                {"name": name},
            )

        assert "intent_bundle" in (_constraint("ck_plans_plan_kind") or "")
        assert "intent_bundle" in (_constraint("ck_proposals_target_kind") or "")
        assert (
            _scalar(sf, "SELECT COUNT(*) FROM public.strategy_plan_execution_events") == 0
        )
        assert _scalar(sf, "SELECT version_num FROM public.alembic_version") >= HEAD
    finally:
        engine.dispose()


def test_execution_event_trail_is_insert_only(disposable_db):
    seed_strategy(disposable_db)
    gen, _ = seed_catalog(disposable_db)
    store, result = submit_plan(disposable_db)
    plan_id = result["plan"]["plan_id"]
    _exec(
        disposable_db,
        "INSERT INTO public.strategy_plan_execution_events "
        "(plan_id, step_no, event, actor_id) VALUES (:plan_id, 1, 'no_op', :owner)",
        {"plan_id": plan_id, "owner": OWNER},
    )
    assert trail(disposable_db, plan_id)[0][1] == "no_op"

    with pytest.raises(Exception) as update_exc:
        _exec(
            disposable_db,
            "UPDATE public.strategy_plan_execution_events SET event = 'filled' WHERE plan_id = :plan_id",
            {"plan_id": plan_id},
        )
    assert "append-only" in str(update_exc.value)
    with pytest.raises(Exception) as delete_exc:
        _exec(
            disposable_db,
            "DELETE FROM public.strategy_plan_execution_events WHERE plan_id = :plan_id",
            {"plan_id": plan_id},
        )
    assert "append-only" in str(delete_exc.value)


# ---------------------------------------------------------------------------
# 2. refusals, by name
# ---------------------------------------------------------------------------


def _prepared(sf, *, target_quantity=10):
    seed_strategy(sf)
    seed_catalog(sf)
    seed_run_binding(sf)
    _, result = submit_plan(sf, target_quantity=target_quantity)
    return result["plan"]


def test_refusal_without_a_reservation_is_named(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    executor = build_executor(disposable_db, _paper_service(disposable_db))
    with pytest.raises(ExecutionRefusal) as ctx:
        run(executor.execute(plan, actor=OWNER))
    assert ctx.value.reason_code == "RESERVATION_REQUIRED"
    rows = trail(disposable_db, plan["plan_id"])
    assert [row[1] for row in rows] == ["rejected"]
    assert rows[0][4] == "RESERVATION_REQUIRED"


def test_refusal_of_an_expired_reservation_is_named(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, valid_seconds=-1)
    executor = build_executor(disposable_db, _paper_service(disposable_db))
    with pytest.raises(ExecutionRefusal) as ctx:
        run(executor.execute(plan, actor=OWNER))
    assert ctx.value.reason_code == "RESERVATION_EXPIRED"


def test_refusal_of_a_live_target_is_named_paper_only(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(
        disposable_db, plan, environment="live", allocation=100000.0
    )
    # The claim succeeded (live reservations exist in Phase 4); this executor
    # refuses to act on it — live enablement is a separate authorization.
    assert reservation["status"] == "active"
    executor = build_executor(disposable_db, _paper_service(disposable_db))
    with pytest.raises(ExecutionRefusal) as ctx:
        run(executor.execute(plan, actor=OWNER))
    assert ctx.value.reason_code == "PAPER_ONLY_EXECUTION"


def test_refusal_of_a_futures_leg_is_named_at_validation(disposable_db):
    """Unknown leg kinds never freeze into a plan: LEG_KIND_UNSUPPORTED is
    terminal at validation, so no executor can ever misread one."""
    seed_strategy(disposable_db)
    seed_catalog(disposable_db, instrument_type="FUT")
    seed_run_binding(disposable_db)
    store, result = submit_plan(
        disposable_db,
        target_kind="intent_bundle",
        legs=[
            {
                "instrument_token": TOKEN, "exchange": "NSE", "tradingsymbol": "RELIANCE",
                "product": "CNC", "target_quantity": 10, "reference_price": PRICE,
            }
        ],
    )
    assert result["status"] == "refused"
    assert result["plan"] is None
    assert result["refusal"]["rejection_reason"] == "LEG_KIND_UNSUPPORTED"


def test_refusal_of_a_second_execution_is_named(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, allocation=100000.0)
    executor = build_executor(disposable_db, _paper_service(disposable_db))
    first = run(executor.execute(plan, actor=OWNER))
    assert first["status"] == "filled"
    with pytest.raises(ExecutionRefusal) as ctx:
        run(executor.execute(plan, actor=OWNER))
    assert ctx.value.reason_code == "PLAN_ALREADY_EXECUTED"


# ---------------------------------------------------------------------------
# 3. the acceptance test and its proofs
# ---------------------------------------------------------------------------


def test_end_to_end_proposal_to_attributed_settlement_evidence(disposable_db, paper_env):
    """THE acceptance (D-1): proposal → plan → admission/reservation → execute →
    paper fills → G1 projection shows the strategy's paper book → reservation
    consumed → settlement assessment recorded."""
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, allocation=100000.0)

    executor = build_executor(disposable_db, _paper_service(disposable_db))
    result = run(executor.execute(plan, actor=OWNER))
    assert result["status"] == "filled"
    (step,) = result["steps"]
    assert step["event"] == "filled"
    assert step["filled_quantity"] == 10

    # The paper runtime produced durable facts carrying the BOUND run's
    # attribution and the plan/reservation/step refs.
    order = _rows(
        disposable_db,
        "SELECT order_id, status, quantity, metadata_json ->> 'strategy_run_id', "
        "metadata_json ->> 'plan_id', metadata_json ->> 'reservation_id' "
        "FROM public.paper_orders WHERE account_scope = :account",
        {"account": ACCOUNT},
    )
    assert len(order) == 1
    order_id, status, quantity, meta_run, meta_plan, meta_reservation = order[0]
    assert str(status) == "filled"
    assert int(quantity) == 10
    assert meta_run == RUN_ID
    assert meta_plan == plan["plan_id"]
    assert meta_reservation == reservation["reservation_id"]
    trade_count = _scalar(
        disposable_db,
        "SELECT COUNT(*) FROM public.paper_trades WHERE account_scope = :account AND order_id = :oid",
        {"account": ACCOUNT, "oid": order_id},
    )
    assert trade_count == 1

    # The trail: submitted → filled (append-only facts).
    rows = trail(disposable_db, plan["plan_id"])
    assert [(row[0], row[1]) for row in rows] == [(1, "submitted"), (1, "filled")]
    assert rows[1][2] == order_id
    assert rows[1][3] == 10

    # The fill CONSUMED the reservation with the plan/order refs (D-4).
    consumed = ledger.get(reservation["reservation_id"])
    assert consumed["status"] == "consumed"
    events = [row["event"] for row in ledger.events(reservation["reservation_id"])]
    assert events == ["created", "consumed"]

    # The barrier observed the work (D-5).
    barrier_rows = _rows(
        disposable_db,
        "SELECT event, ref FROM public.strategy_execution_barrier_events "
        "WHERE account_id = :account AND strategy_id = :sid AND execution_environment = 'paper' "
        "ORDER BY version",
        {"account": ACCOUNT, "sid": STRATEGY},
    )
    assert [row[0] for row in barrier_rows] == ["work_created", "work_resolved"]
    assert barrier_rows[0][1] == f"plan:{plan['plan_id']}:step:1"

    # G1's paper fold attributes the fill: the strategy's OWN paper book.
    service = StrategyAttributionService(SqlAttributionStore(session_factory=disposable_db))
    report = run(service.publish(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    ))
    assert report["folded_facts"] == 1
    positions = run(service.open_positions(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    ))
    assert [(p["execution_environment"], p["net_quantity"]) for p in positions] == [("paper", 10)]

    # A settlement assessment is recordable against the executed book.
    assessment = SettlementService(session_factory=disposable_db).assess(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    )
    assert assessment["account_id"] == ACCOUNT
    assert assessment["barrier_version"] == 2
    latest = SettlementService(session_factory=disposable_db).latest_assessment(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    )
    assert latest["assessment_id"] == assessment["assessment_id"]
    assert latest["stale"] is False


def test_settled_assessment_goes_stale_when_new_work_appears(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, allocation=100000.0)
    run(build_executor(disposable_db, _paper_service(disposable_db)).execute(plan, actor=OWNER))

    settlement = SettlementService(session_factory=disposable_db)
    assessment = settlement.assess(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    )
    assert assessment["stale"] is False

    # New work (a late-arriving order event on the same book) invalidates the
    # settled snapshot by version arithmetic — it is never rewritten fresh.
    ExecutionBarrier(session_factory=disposable_db).record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper",
        event="work_created", ref="fill:late-1",
    )
    latest = settlement.latest_assessment(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="paper"
    )
    assert latest["overall"] == assessment["overall"]  # snapshot, not state
    assert latest["stale"] is True


def test_capacity_after_consumption_cannot_double_spend(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, allocation=15000.0)
    run(build_executor(disposable_db, _paper_service(disposable_db)).execute(plan, actor=OWNER))
    assert ledger.for_plan(plan["plan_id"])["status"] == "consumed"

    # Consumed exposure still counts against the allocation: a second plan's
    # claim for the same capacity is refused, never double-spent.
    assert ledger.held_notional(account_id=ACCOUNT) == 15000.0
    _, second = submit_plan(disposable_db, evaluation_id="eval-2")
    with pytest.raises(CapacityExceeded):
        ReservationLedger(session_factory=disposable_db).claim(
            ClaimRequest(
                plan_id=str(second["plan"]["plan_id"]),
                strategy_id=STRATEGY,
                account_id=ACCOUNT,
                evaluation_id="eval-2",
                execution_environment="paper",
                requirement_inr=15000.0,
                valid_until=datetime.now(timezone.utc) + timedelta(seconds=600),
                allocation_inr=15000.0,
                actor_id=OWNER,
            )
        )
    assert (
        _scalar(
            disposable_db,
            "SELECT COUNT(*) FROM public.strategy_reservations WHERE account_id = :account",
            {"account": ACCOUNT},
        )
        == 1
    )


def test_concurrent_execute_yields_exactly_one_submission(disposable_db, paper_env):
    plan = _prepared(disposable_db)
    ledger, reservation = admit_and_reserve(disposable_db, plan, allocation=100000.0)
    executor = build_executor(disposable_db, _paper_service(disposable_db))

    start = threading.Barrier(2)
    results = {}
    errors = []

    def attempt(tag):
        try:
            start.wait(timeout=20)
            results[tag] = run(executor.execute(plan, actor=OWNER))
        except Exception as exc:  # noqa: BLE001 - the loser's refusal IS the proof
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [e for e in errors if not isinstance(e, ExecutionRefusal)], errors

    refusals = [e for e in errors if isinstance(e, ExecutionRefusal)]
    assert len(refusals) == 1
    assert refusals[0].reason_code == "PLAN_ALREADY_EXECUTED"
    filled = [r for r in results.values() if r.get("status") == "filled"]
    assert len(filled) == 1

    # Exactly ONE submission event, exactly ONE filled outcome, ONE order.
    submissions = _scalar(
        disposable_db,
        "SELECT COUNT(*) FROM public.strategy_plan_execution_events "
        "WHERE plan_id = :plan_id AND event = 'submitted'",
        {"plan_id": plan["plan_id"]},
    )
    assert submissions == 1
    assert _scalar(
        disposable_db,
        "SELECT COUNT(*) FROM public.paper_orders WHERE account_scope = :account",
        {"account": ACCOUNT},
    ) == 1
    assert _scalar(
        disposable_db,
        "SELECT COUNT(*) FROM public.strategy_reservation_events WHERE reservation_id = :rid AND event = 'consumed'",
        {"rid": reservation["reservation_id"]},
    ) == 1
