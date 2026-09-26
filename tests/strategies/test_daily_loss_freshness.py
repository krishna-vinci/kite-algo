from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.strategies.daily_loss import account_day_pnl_inr


def _factory_with_reconciled_at(reconciled_at: str):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, _connection_record):
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE public.account_positions (
                    account_id TEXT NOT NULL,
                    instrument_token INTEGER NOT NULL,
                    product TEXT NOT NULL,
                    realized_pnl NUMERIC NOT NULL,
                    last_price NUMERIC,
                    average_price NUMERIC,
                    net_quantity INTEGER NOT NULL,
                    last_reconciled_at TIMESTAMPTZ,
                    PRIMARY KEY (account_id, instrument_token, product)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO public.account_positions (
                    account_id, instrument_token, product, realized_pnl,
                    last_price, average_price, net_quantity, last_reconciled_at
                ) VALUES ('kite:A', 1, 'NRML', -100, NULL, NULL, 0, :reconciled_at)
                """
            ),
            {"reconciled_at": reconciled_at},
        )
    return sessionmaker(bind=engine), engine


def test_account_day_pnl_requires_current_session_reconciliation():
    now = datetime(2026, 9, 17, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    fresh_factory, fresh_engine = _factory_with_reconciled_at(
        (now - timedelta(seconds=30)).isoformat()
    )
    stale_factory, stale_engine = _factory_with_reconciled_at(
        (now - timedelta(days=1)).isoformat()
    )

    try:
        assert (
            account_day_pnl_inr(
                account_id="kite:A",
                session_factory=fresh_factory,
                now=now,
            )
            == -100.0
        )
        assert (
            account_day_pnl_inr(
                account_id="kite:A",
                session_factory=stale_factory,
                now=now,
            )
            is None
        )
    finally:
        fresh_engine.dispose()
        stale_engine.dispose()
