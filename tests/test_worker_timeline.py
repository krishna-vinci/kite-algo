from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.worker_timeline import WorkerTimelineStore


def _sqlite_timeline_store() -> tuple[WorkerTimelineStore, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, connection_record):
        _ = connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.close()

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE public.worker_execution_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    basket_execution_id TEXT,
                    event_kind TEXT NOT NULL DEFAULT 'execution',
                    event_source TEXT NOT NULL DEFAULT 'legacy_execution',
                    event_type TEXT NOT NULL,
                    related_resource_type TEXT,
                    related_resource_id TEXT,
                    summary TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

    factory = sessionmaker(bind=engine)
    return WorkerTimelineStore(session_factory=factory), factory


def test_worker_timeline_store_returns_normalized_committed_row():
    store, factory = _sqlite_timeline_store()
    db = factory()
    try:
        row = store.append_event(
            db=db,
            strategy_run_id="run-1",
            account_id="kite:acct",
            basket_execution_id=None,
            event_kind="execution",
            event_source="basket_runtime",
            event_type="order.updated",
            related_resource_type="broker_order",
            related_resource_id="OID-1",
            summary=None,
            payload={"order_id": "OID-1", "status": "COMPLETE"},
        )
    finally:
        db.close()
    assert row["cursor"] > 0
    assert row["event_kind"] == "execution"
    assert row["event_source"] == "basket_runtime"
    assert row["related_resource_type"] == "broker_order"


def test_worker_timeline_store_uses_caller_session_for_latest_lookup():
    store, factory = _sqlite_timeline_store()
    db = factory()
    try:
        store.append_event(
            db=db,
            strategy_run_id="run-1",
            account_id="kite:acct",
            basket_execution_id=None,
            event_kind="protection",
            event_source="options_protection",
            event_type="protection.triggered",
            related_resource_type=None,
            related_resource_id=None,
            summary="Options protection triggered: emergency_guard",
            payload={"emission_mode": "observation_driven", "triggered": True},
        )
        latest = store.get_latest_event_for_source(
            db=db,
            strategy_run_id="run-1",
            event_kind="protection",
            event_source="options_protection",
        )
    finally:
        db.close()
    assert latest is not None
    assert latest["event_type"] == "protection.triggered"


def test_worker_timeline_store_list_events_is_session_aware():
    store, factory = _sqlite_timeline_store()
    db = factory()
    try:
        store.append_event(
            db=db,
            strategy_run_id="run-2",
            account_id="kite:acct",
            basket_execution_id="basket-1",
            event_kind="execution",
            event_source="basket_runtime",
            event_type="basket.status_changed",
            related_resource_type="basket_execution",
            related_resource_id="basket-1",
            summary=None,
            payload={"status": "active"},
        )
        rows = store.list_events(
            db=db,
            strategy_run_id="run-2",
            event_kind="execution",
            related_resource_type="basket_execution",
            related_resource_id="basket-1",
        )
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0]["basket_execution_id"] == "basket-1"
