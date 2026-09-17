"""Settlement barrier on PostgreSQL: the adversarial scenario list (G7).

Why PostgreSQL: this suite proves what SQLite structurally cannot — the
insert-only triggers, the atomic version bump under concurrent work, the book
advisory lock serializing proofs against work transitions, late-fill
invalidation of a settled assessment, partial cancellation keeping work
in flight, and the stale-broker-snapshot unknown. These are the D-1/D-2/D-5
invariants that make quiescence a durable proof rather than an inference.

Every test runs against a DISPOSABLE, uniquely named database created on the
test server, upgraded with ``alembic upgrade head`` and dropped afterwards. No
existing database is ever touched.

    SETTLEMENT_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_settlement_barrier_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2  # real psycopg2 must be imported BEFORE the stubs
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.strategies.attribution import SqlAttributionStore  # noqa: E402
from backend.strategies.settlement import ExecutionBarrier, SettlementService  # noqa: E402

PG_URL = os.environ.get("SETTLEMENT_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "SETTLEMENT_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )

HEAD = "20260917_000029"
PRIOR_HEAD = "20260917_000028"

ACCOUNT = "kite:A"
STRATEGY = "stg-1"
OWNER = "app:o"


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
    dbname = f"kite_stl_{uuid.uuid4().hex[:12]}"
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
    """A disposable database upgraded only to the pre-settlement head."""
    dbname = f"kite_stl_pre_{uuid.uuid4().hex[:10]}"
    _create_database(dbname)
    db_url = _url_for(dbname)
    engine = create_engine(db_url, pool_pre_ping=True)
    _upgrade(db_url, PRIOR_HEAD)
    try:
        yield db_url
    finally:
        engine.dispose()
        _drop_database(dbname)


# ---------------------------------------------------------------------------
# seed helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def _exec(session_factory, sql, params=None):
    with session_factory() as session:
        session.execute(text(sql), params or {})
        session.commit()


def _scalar(session_factory, sql, params=None):
    with session_factory() as session:
        return session.execute(text(sql), params or {}).scalar()


def seed_strategy(sf, *, sid=STRATEGY, owner=OWNER, account=ACCOUNT, status="active"):
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, :name, :account, :status)",
        {"sid": sid, "owner": owner, "name": f"Strategy {sid}", "account": account, "status": status},
    )
    return sid


def seed_binding(sf, *, run_id="run-1", sid=STRATEGY, account=ACCOUNT, env="live"):
    store = SqlAttributionStore(session_factory=sf)
    store.bind_run(
        strategy_run_id=run_id, strategy_id=sid, owner_id=OWNER, account_id=account,
        execution_environment=env, bound_by="test", binding_source="hosted_job",
    )


def seed_run(sf, *, run_id="run-1", account=ACCOUNT, mode="live", status="open"):
    _exec(
        sf,
        "INSERT INTO public.algo_worker_runs "
        "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
        "VALUES (:run_id, 'tok', 'tmpl', :account, :mode, :status)",
        {"run_id": run_id, "account": account, "mode": mode, "status": status},
    )
    return run_id


def seed_link(sf, *, account=ACCOUNT, order_id="OID-1", run_id="run-1", trade_id=None):
    _exec(
        sf,
        "INSERT INTO public.worker_live_execution_links "
        "(strategy_run_id, account_id, broker_order_id, trade_id, client_order_ref) "
        "VALUES (:run_id, :account, :order_id, :trade_id, :ref)",
        {"run_id": run_id, "account": account, "order_id": order_id, "trade_id": trade_id, "ref": f"KA{order_id}"},
    )


def seed_order_state(sf, *, order_id="OID-1", status="OPEN", account=ACCOUNT):
    _exec(
        sf,
        "INSERT INTO public.order_state_projection "
        "(account_id, order_id, latest_status, latest_event_timestamp, terminal) "
        "VALUES (:account, :order_id, :status, NOW(), :terminal)",
        {"account": account, "order_id": order_id, "status": status, "terminal": status in ("COMPLETE", "CANCELLED", "REJECTED", "LAPSED")},
    )


def seed_fill(sf, *, account=ACCOUNT, order_id="OID-1", trade_id="T-1", side="BUY", qty=100):
    _exec(
        sf,
        "INSERT INTO public.order_trade_fills "
        "(account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol, product, "
        " transaction_type, quantity, price, fill_timestamp, payload_json) "
        "VALUES (:account, :trade_id, :order_id, 738561, 'NSE', 'RELIANCE', 'CNC', "
        " :side, :qty, 100.0, NOW(), '{}')",
        {"account": account, "order_id": order_id, "trade_id": trade_id, "side": side, "qty": qty},
    )


def seed_book_row(sf, *, sid=STRATEGY, account=ACCOUNT, env="live", token=738561, product="CNC", qty=100):
    _exec(
        sf,
        "INSERT INTO public.strategy_position_projection "
        "(account_id, strategy_id, execution_environment, identity_kind, identity_key, product, "
        " canonical_instrument_id, instrument_token, exchange, tradingsymbol, net_quantity, projection_version) "
        "VALUES (:account, :sid, :env, 'canonical', :inst, :product, :inst, "
        " :token, 'NSE', 'RELIANCE', :qty, 1)",
        {
            "account": account,
            "sid": sid,
            "env": env,
            "product": product,
            "token": token,
            "qty": qty,
            # canonical identities are native UUIDs on PostgreSQL
            "inst": "11111111-2222-3333-4444-555555555555",
        },
    )


def seed_account_position(sf, *, account=ACCOUNT, token=738561, product="CNC", qty=100, age_seconds=0):
    _exec(
        sf,
        "INSERT INTO public.account_positions "
        "(account_id, instrument_token, product, exchange, tradingsymbol, net_quantity, updated_at) "
        "VALUES (:account, :token, :product, 'NSE', 'RELIANCE', :qty, NOW() - make_interval(secs => :age))",
        {"account": account, "token": token, "product": product, "qty": qty, "age": age_seconds},
    )


def barrier_state(sf, *, account=ACCOUNT, sid=STRATEGY, env="live"):
    barrier = ExecutionBarrier(session_factory=sf)
    return barrier.state(account_id=account, strategy_id=sid, execution_environment=env)


# ---------------------------------------------------------------------------
# migration shape
# ---------------------------------------------------------------------------


def test_head_is_the_settlement_barrier_revision(disposable_db):
    version = _scalar(disposable_db, "SELECT version_num FROM public.alembic_version")
    assert version == HEAD


def test_upgrade_from_prior_head_creates_the_settlement_tables(prior_head_db):
    with create_engine(prior_head_db).connect() as conn:
        present = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('strategy_execution_barriers', 'strategy_execution_barrier_events', "
                "'strategy_settlement_assessments')"
            )
        ).scalar()
    assert int(present) == 0
    _upgrade(prior_head_db, "head")
    engine = create_engine(prior_head_db)
    try:
        with engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name LIKE 'strategy_%'"
                    )
                ).fetchall()
            }
            version = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
    finally:
        engine.dispose()
    assert version == HEAD
    assert {
        "strategy_execution_barriers",
        "strategy_execution_barrier_events",
        "strategy_settlement_assessments",
    } <= names


def test_barrier_event_rows_are_insert_only(disposable_db):
    sf = disposable_db
    _exec(
        sf,
        "INSERT INTO public.strategy_execution_barrier_events "
        "(account_id, strategy_id, execution_environment, version, event) "
        "VALUES (:a, :s, 'live', 1, 'work_created')",
        {"a": ACCOUNT, "s": STRATEGY},
    )
    for mutation in (
        "UPDATE public.strategy_execution_barrier_events SET version = 99",
        "DELETE FROM public.strategy_execution_barrier_events",
    ):
        with pytest.raises(Exception, match="append-only"):
            _exec(sf, mutation)


def test_settlement_assessment_rows_are_insert_only(disposable_db):
    sf = disposable_db
    _exec(
        sf,
        "INSERT INTO public.strategy_settlement_assessments "
        "(account_id, strategy_id, execution_environment, overall, barrier_version, axes, evidence_digest) "
        "VALUES (:a, :s, 'live', 'settled', 0, '{}', 'digest')",
        {"a": ACCOUNT, "s": STRATEGY},
    )
    for mutation in (
        "UPDATE public.strategy_settlement_assessments SET overall = 'unknown'",
        "DELETE FROM public.strategy_settlement_assessments",
    ):
        with pytest.raises(Exception, match="append-only"):
            _exec(sf, mutation)


# ---------------------------------------------------------------------------
# barrier: version, proofs, invalidation
# ---------------------------------------------------------------------------


def test_work_events_bump_the_version_with_their_event_row(disposable_db):
    sf = disposable_db
    barrier = ExecutionBarrier(session_factory=sf)
    assert barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
        event="work_created", ref="order:OID-1",
    ) == 1
    assert barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
        event="work_resolved", ref="order:OID-1",
    ) == 2
    rows = None
    with sf() as session:
        rows = session.execute(
            text(
                "SELECT event, version FROM public.strategy_execution_barrier_events "
                "WHERE account_id = :a AND strategy_id = :s ORDER BY version"
            ),
            {"a": ACCOUNT, "s": STRATEGY},
        ).fetchall()
    assert [(event, version) for event, version in rows] == [("work_created", 1), ("work_resolved", 2)]


def test_concurrent_work_bumps_never_share_a_version(disposable_db):
    """The PG upsert path: N concurrent work events yield N distinct versions."""
    sf = disposable_db
    barrier = ExecutionBarrier(session_factory=sf)
    errors: list = []
    versions: list = []

    def _worker(index: int) -> None:
        try:
            versions.append(
                barrier.record_work_event(
                    account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
                    event="work_created", ref=f"work:{index}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert sorted(versions) == [1, 2, 3, 4, 5, 6]
    assert int(barrier_state(sf)["barrier_version"]) == 6


def test_proof_records_at_the_current_version_and_bumps_nothing(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    barrier = ExecutionBarrier(session_factory=sf)
    barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live", event="work_resolved"
    )
    result = barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert result.recorded
    assert result.barrier_version == 1
    state = barrier_state(sf)
    assert state["barrier_version"] == 1  # proofs do not bump
    assert state["quiet_since_version"] == 1
    assert state["proof_valid"]


def test_proof_fails_closed_with_in_flight_work_and_records_nothing(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf)
    seed_binding(sf)
    seed_link(sf, order_id="OID-1")  # no order_state_projection row: non-terminal
    barrier = ExecutionBarrier(session_factory=sf)
    result = barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert not result.recorded
    assert result.reason == "inflight_work_present"
    assert any(item.kind == "order_non_terminal" for item in result.inflight)
    state = barrier_state(sf)
    assert state["exists"] is False  # nothing was written


def test_partial_cancellation_keeps_in_flight_nonempty(disposable_db):
    """A cancelled order with a partial fill still has unresolved execution."""
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf)
    seed_binding(sf)
    seed_link(sf, order_id="OID-1", trade_id="T-1")
    seed_fill(sf, trade_id="T-1", side="BUY", qty=40)
    seed_order_state(sf, order_id="OID-1", status="CANCELLED")
    from backend.strategies.settlement import enumerate_inflight_work

    with sf() as session:
        items = enumerate_inflight_work(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live", db=session
        )
    kinds = {item.kind for item in items}
    assert "execution_link_unresolved" in kinds
    barrier = ExecutionBarrier(session_factory=sf)
    result = barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert not result.recorded


def test_delayed_fill_after_a_terminal_order_is_caught_after_the_proof(disposable_db):
    """D-5 late fill: proof records, the fill is admitted (work_created), the
    barrier bumps, and every prior proof is invalid by version arithmetic."""
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf)
    seed_binding(sf)
    seed_link(sf, order_id="OID-1")
    seed_order_state(sf, order_id="OID-1", status="COMPLETE")  # terminal: not in flight
    barrier = ExecutionBarrier(session_factory=sf)

    first = barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert first.recorded

    # The delayed fill lands: a trade link + fill for the completed order, and
    # the work transition its admission must record (execution wiring's duty).
    seed_link(sf, order_id="OID-1", trade_id="T-9")
    seed_fill(sf, order_id="OID-1", trade_id="T-9", side="BUY", qty=25)
    barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
        event="work_created", ref="fill:T-9",
    )

    state = barrier_state(sf)
    assert state["barrier_version"] == 1
    assert state["quiet_since_version"] == 0
    assert not state["proof_valid"]

    from backend.strategies.settlement import enumerate_inflight_work

    with sf() as session:
        items = enumerate_inflight_work(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live", db=session
        )
    assert any(item.kind == "execution_link_unresolved" for item in items)
    second = barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert not second.recorded


def test_concurrent_proof_and_work_serialize_on_the_book_lock(disposable_db):
    """Proof and work bump serialize; the proof can never survive the work."""
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf)
    seed_binding(sf)
    barrier = ExecutionBarrier(session_factory=sf)

    proof_done = threading.Event()

    def _proof():
        # Holds the book lock for its whole transaction: the work transaction
        # below must wait for it (or run entirely before it).
        outcome["proof"] = barrier.record_proof(
            account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live"
        )
        proof_done.set()

    def _work():
        # Non-terminal order + its work transition in ONE transaction.
        with sf() as session:
            session.execute(
                text(
                    "INSERT INTO public.worker_live_execution_links "
                    "(strategy_run_id, account_id, broker_order_id, trade_id, client_order_ref) "
                    "VALUES ('run-1', :account, 'OID-RACE', NULL, 'KARACE')"
                ),
                {"account": ACCOUNT},
            )
            barrier.record_work_event(
                account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
                event="work_created", ref="order:OID-RACE", db=session,
            )
            session.commit()

    outcome: dict = {}
    start = threading.Barrier(2, timeout=15)

    def _proof_synced():
        start.wait()
        _proof()

    def _work_synced():
        start.wait()
        _work()

    thread_proof = threading.Thread(target=_proof_synced)
    thread_work = threading.Thread(target=_work_synced)
    thread_proof.start()
    thread_work.start()
    thread_work.join()
    thread_proof.join()

    state = barrier_state(sf)
    proof = outcome["proof"]
    work_rows = None
    with sf() as session:
        work_rows = session.execute(
            text(
                "SELECT COUNT(*) FROM public.strategy_execution_barrier_events "
                "WHERE account_id = :a AND strategy_id = :s AND event = 'work_created'"
            ),
            {"a": ACCOUNT, "s": STRATEGY},
        ).scalar()
    assert int(work_rows) == 1
    # The lock serialized the two; EITHER order ends invalidated-or-refused:
    #  - proof first: recorded at version 0, then the bump to 1 invalidates it;
    #  - work first: the proof then enumerates the non-terminal order and fails.
    if proof.recorded:
        assert proof.barrier_version == 0
        assert state["barrier_version"] == 1
        assert state["quiet_since_version"] == 0
        assert not state["proof_valid"]
    else:
        assert proof.reason == "inflight_work_present"
        assert not state["proof_valid"]
    _ = proof_done


# ---------------------------------------------------------------------------
# four-axis assessment on PostgreSQL
# ---------------------------------------------------------------------------


def test_assessment_of_a_quiescent_flat_strategy_settles(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    barrier = ExecutionBarrier(session_factory=sf)
    barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    service = SettlementService(session_factory=sf)
    assessment = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert assessment["overall"] == "settled"
    assert all(axis["state"] == "satisfied" for axis in assessment["axes"].values())
    assert assessment["stale"] is False


def test_stale_broker_snapshot_makes_flatness_unknown(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf, status="closed")
    seed_binding(sf)
    seed_book_row(sf, qty=100)
    seed_account_position(sf, qty=100, age_seconds=600)  # 10 minutes old: stale
    barrier = ExecutionBarrier(session_factory=sf)
    barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    service = SettlementService(session_factory=sf)
    assessment = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert assessment["axes"]["attribution_scoped_flatness"]["state"] == "unknown"
    assert assessment["axes"]["attribution_scoped_flatness"]["detail"]["reason"] == "stale_broker_snapshot"
    assert assessment["overall"] == "unknown"  # unknown never releases


def test_fresh_broker_snapshot_with_open_book_is_unsettled(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    seed_run(sf, status="closed")
    seed_binding(sf)
    seed_book_row(sf, qty=100)
    seed_account_position(sf, qty=100, age_seconds=1)
    barrier = ExecutionBarrier(session_factory=sf)
    barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    service = SettlementService(session_factory=sf)
    assessment = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert assessment["axes"]["attribution_scoped_flatness"]["state"] == "failed"
    assert assessment["overall"] == "unsettled"


def test_late_work_makes_a_settled_assessment_detectably_stale(disposable_db):
    """D-5: the settled snapshot is invalidated, never rewritten."""
    sf = disposable_db
    seed_strategy(sf)
    barrier = ExecutionBarrier(session_factory=sf)
    barrier.record_proof(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    service = SettlementService(session_factory=sf)
    settled = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert settled["overall"] == "settled"
    assert settled["barrier_version"] == 0

    barrier.record_work_event(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live",
        event="work_created", ref="fill:late",
    )
    latest = service.latest_assessment(
        account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live"
    )
    assert latest["assessment_id"] == settled["assessment_id"]
    assert latest["overall"] == "settled"  # the snapshot is immutable
    assert latest["stale"] is True  # ...and detectably stale

    re_assessed = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert re_assessed["overall"] == "unknown"  # the platform re-assesses before acting


def test_assessment_snapshot_records_the_per_axis_digests(disposable_db):
    sf = disposable_db
    seed_strategy(sf)
    service = SettlementService(session_factory=sf)
    first = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    second = service.assess(account_id=ACCOUNT, strategy_id=STRATEGY, execution_environment="live")
    assert first["assessment_id"] != second["assessment_id"]
    assert first["axes"].keys() == second["axes"].keys()
    for name, axis in second["axes"].items():
        assert axis["evidence_digest"]
        assert axis["evidence_digest"] == first["axes"][name]["evidence_digest"]  # identical evidence
    assert second["evidence_digest"] == first["evidence_digest"]  # deterministic snapshots
