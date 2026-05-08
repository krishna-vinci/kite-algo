from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from broker_api.orders.worker_execution_links import WorkerExecutionLinksStore


def _sqlite_store() -> WorkerExecutionLinksStore:
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
                CREATE TABLE public.worker_live_execution_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL,
                    trade_id TEXT,
                    client_order_ref TEXT,
                    basket_execution_id TEXT,
                    basket_leg_index INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.order_trade_fills (
                    account_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    PRIMARY KEY (account_id, trade_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE public.order_state_projection (
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    latest_status TEXT,
                    PRIMARY KEY (account_id, order_id)
                )
                """
            )
        )

    factory = sessionmaker(bind=engine)
    return WorkerExecutionLinksStore(session_factory=factory)


def test_upsert_order_link_creates_exact_worker_bridge():
    store = _sqlite_store()
    store.upsert_order_link(
        strategy_run_id="run-1",
        account_id="kite:acct",
        broker_order_id="OID-1",
        client_order_ref="KA123",
        basket_execution_id="basket-1",
        basket_leg_index=0,
    )
    rows = store.list_links_for_run("run-1", "kite:acct")
    assert rows[0]["broker_order_id"] == "OID-1"


def test_upsert_trade_links_records_trade_ids():
    store = _sqlite_store()
    store.upsert_order_link(strategy_run_id="run-1", account_id="kite:acct", broker_order_id="OID-1")
    inserted = store.upsert_trade_links_for_order(
        account_id="kite:acct",
        broker_order_id="OID-1",
        trades=[{"trade_id": "T-1"}, {"trade_id": "T-2"}],
    )
    rows = store.list_trade_links_for_run("run-1", "kite:acct")
    assert inserted == 2
    assert {row["trade_id"] for row in rows} == {"T-1", "T-2"}
