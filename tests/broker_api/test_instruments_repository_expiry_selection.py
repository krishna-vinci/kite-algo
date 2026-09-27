from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.broker_api.instruments.instruments_repository import InstrumentsRepository


class _RepoStub(InstrumentsRepository):
    def __init__(self, expiries: list[date], grouped: dict[date, list[date]]):
        self._expiries = expiries
        self._grouped = grouped

    def get_expiries(self, underlying: str, today: date) -> list[date]:  # type: ignore[override]
        return self._expiries

    def get_expiries_grouped(self, underlying: str, today: date) -> dict[date, list[date]]:  # type: ignore[override]
        return self._grouped


def test_nifty_expiry_window_uses_three_weeklies_plus_two_monthlies():
    expiries = [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
        date(2026, 6, 2),
        date(2026, 6, 30),
        date(2026, 7, 28),
    ]
    repo = _RepoStub(
        expiries=expiries,
        grouped={
            date(2026, 5, 1): expiries[:4],
            date(2026, 6, 1): expiries[4:6],
            date(2026, 7, 1): expiries[6:],
        },
    )

    selected = repo.select_current_weeklies_plus_three_monthlies("NIFTY", today=date(2026, 5, 1))

    assert selected == [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
        date(2026, 6, 30),
    ]


def test_finnifty_expiry_window_is_monthly_only():
    expiries = [
        date(2026, 5, 7),
        date(2026, 5, 14),
        date(2026, 5, 21),
        date(2026, 5, 28),
        date(2026, 6, 4),
        date(2026, 6, 25),
        date(2026, 7, 30),
    ]
    repo = _RepoStub(
        expiries=expiries,
        grouped={
            date(2026, 5, 1): expiries[:4],
            date(2026, 6, 1): expiries[4:6],
            date(2026, 7, 1): expiries[6:],
        },
    )

    selected = repo.select_current_weeklies_plus_three_monthlies("FINNIFTY", today=date(2026, 5, 1))

    assert selected == [
        date(2026, 5, 28),
        date(2026, 6, 25),
        date(2026, 7, 30),
    ]


def test_sensex_expiry_window_uses_weeklies_plus_monthlies():
    expiries = [
        date(2026, 5, 7),
        date(2026, 5, 14),
        date(2026, 5, 21),
        date(2026, 5, 28),
        date(2026, 6, 4),
        date(2026, 6, 25),
        date(2026, 7, 30),
    ]
    repo = _RepoStub(
        expiries=expiries,
        grouped={
            date(2026, 5, 1): expiries[:4],
            date(2026, 6, 1): expiries[4:6],
            date(2026, 7, 1): expiries[6:],
        },
    )

    selected = repo.select_current_weeklies_plus_three_monthlies(
        "SENSEX", today=date(2026, 5, 1)
    )

    assert selected == expiries


@pytest.fixture
def catalog_repo():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        cursor.execute(
            """
            CREATE TABLE public.instrument_catalog_published_v (
                instrument_id TEXT PRIMARY KEY,
                identity_key TEXT,
                public_key TEXT,
                exchange TEXT,
                segment TEXT,
                tradingsymbol TEXT,
                name TEXT,
                instrument_type TEXT,
                underlying TEXT,
                option_type TEXT,
                expiry TEXT,
                strike REAL,
                tick_size REAL,
                lot_size INTEGER,
                lifecycle_status TEXT,
                catalog_generation TEXT,
                broker TEXT,
                broker_token INTEGER,
                broker_exchange_token INTEGER
            )
            """
        )
        dbapi_connection.commit()

    factory = sessionmaker(bind=engine)
    rows = [
        ("spot-nifty", "NSE:NIFTY 50", "NSE", "INDICES", "NIFTY 50", "NIFTY 50", "EQ", None, None, None, None, 0.05, 1, "active", 256265),
        ("spot-banknifty", "NSE:NIFTY BANK", "NSE", "INDICES", "NIFTY BANK", "NIFTY BANK", "EQ", None, None, None, None, 0.05, 1, "active", 260105),
        ("spot-finnifty", "NSE:NIFTY FIN SERVICE", "NSE", "INDICES", "NIFTY FIN SERVICE", "NIFTY FIN SERVICE", "EQ", None, None, None, None, 0.05, 1, "active", 257801),
        ("spot-midcpnifty", "NSE:NIFTY MID SELECT", "NSE", "INDICES", "NIFTY MID SELECT", "NIFTY MIDCAP SELECT (MIDCPNIFTY)", "EQ", None, None, None, None, 0.05, 1, "active", 288009),
        ("spot-sensex", "BSE:SENSEX", "BSE", "INDICES", "SENSEX", "SENSEX", "EQ", None, None, None, None, 0.05, 1, "active", 265),
        ("spot-bankex", "BSE:BANKEX", "BSE", "INDICES", "BANKEX", "BSE INDEX BANKEX", "EQ", None, None, None, None, 0.05, 1, "active", 274441),
        ("nifty-ce", "NFO:NIFTY26OCT23000CE", "NFO", "NFO-OPT", "NIFTY26OCT23000CE", "NIFTY", "CE", "NIFTY", "CE", "2026-10-06", 23000.0, 0.05, 65, "active", 1001),
        ("nifty-pe", "NFO:NIFTY26OCT23000PE", "NFO", "NFO-OPT", "NIFTY26OCT23000PE", "NIFTY", "PE", "NIFTY", "PE", "2026-10-06", 23000.0, 0.05, 65, "active", 1002),
        ("nifty-next", "NFO:NIFTY26OCT23100CE", "NFO", "NFO-OPT", "NIFTY26OCT23100CE", "NIFTY", "CE", "NIFTY", "CE", "2026-10-13", 23100.0, 0.05, 65, "active", 1003),
        ("nifty-expired", "NFO:NIFTY26OCT23200CE", "NFO", "NFO-OPT", "NIFTY26OCT23200CE", "NIFTY", "CE", "NIFTY", "CE", "2026-10-20", 23200.0, 0.05, 65, "expired", 1004),
        ("sensex-ce", "BFO:SENSEX26OCT82000CE", "BFO", "BFO-OPT", "SENSEX26OCT82000CE", "SENSEX", "CE", "SENSEX", "CE", "2026-10-01", 82000.0, 0.05, 20, "active", 2001),
        ("sensex-pe", "BFO:SENSEX26OCT82000PE", "BFO", "BFO-OPT", "SENSEX26OCT82000PE", "SENSEX", "PE", "SENSEX", "PE", "2026-10-01", 82000.0, 0.05, 20, "active", 2002),
        ("sensex-wrong-exchange", "NFO:SENSEX26OCT82100CE", "NFO", "NFO-OPT", "SENSEX26OCT82100CE", "SENSEX", "CE", "SENSEX", "CE", "2026-10-01", 82100.0, 0.05, 20, "active", 2003),
    ]
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO public.instrument_catalog_published_v (
                    instrument_id, identity_key, public_key, exchange, segment,
                    tradingsymbol, name, instrument_type, underlying, option_type,
                    expiry, strike, tick_size, lot_size, lifecycle_status,
                    catalog_generation, broker, broker_token, broker_exchange_token
                ) VALUES (
                    :instrument_id, :instrument_id, :public_key, :exchange, :segment,
                    :tradingsymbol, :name, :instrument_type, :underlying, :option_type,
                    :expiry, :strike, :tick_size, :lot_size, :lifecycle_status,
                    'generation-1', 'kite', :broker_token, :broker_token
                )
                """
            ),
            [
                {
                    "instrument_id": row[0],
                    "public_key": row[1],
                    "exchange": row[2],
                    "segment": row[3],
                    "tradingsymbol": row[4],
                    "name": row[5],
                    "instrument_type": row[6],
                    "underlying": row[7],
                    "option_type": row[8],
                    "expiry": row[9],
                    "strike": row[10],
                    "tick_size": row[11],
                    "lot_size": row[12],
                    "lifecycle_status": row[13],
                    "broker_token": row[14],
                }
                for row in rows
            ],
        )
        session.commit()

    yield InstrumentsRepository(db=factory)
    engine.dispose()


def test_spot_tokens_resolve_all_supported_indices_from_catalog(catalog_repo):
    assert {
        underlying: catalog_repo.get_spot_token(underlying)
        for underlying in (
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX",
            "BANKEX",
        )
    } == {
        "NIFTY": 256265,
        "BANKNIFTY": 260105,
        "FINNIFTY": 257801,
        "MIDCPNIFTY": 288009,
        "SENSEX": 265,
        "BANKEX": 274441,
    }


def test_nfo_and_bfo_expiries_and_strikes_use_active_catalog_rows(catalog_repo):
    assert catalog_repo.get_expiries("NIFTY", date(2026, 9, 27)) == [
        date(2026, 10, 6),
        date(2026, 10, 13),
    ]
    assert catalog_repo.get_expiries("SENSEX", date(2026, 9, 27)) == [
        date(2026, 10, 1)
    ]
    assert catalog_repo.get_distinct_strikes("NIFTY", date(2026, 10, 6)) == [
        23000.0
    ]
    assert catalog_repo.get_distinct_strikes("SENSEX", date(2026, 10, 1)) == [
        82000.0
    ]


def test_bfo_contracts_alias_broker_token_and_keep_catalog_metadata(catalog_repo):
    contracts = catalog_repo.get_option_instruments_for_strikes(
        "SENSEX", date(2026, 10, 1), [82000.0, 82100.0]
    )

    assert {row["instrument_token"] for row in contracts} == {2001, 2002}
    assert {row["option_type"] for row in contracts} == {"CE", "PE"}
    assert {row["lot_size"] for row in contracts} == {20}
    assert catalog_repo.get_lot_size(2001) == 20
    instrument = catalog_repo.get_instrument_by_token(2001)
    assert instrument is not None
    assert instrument["tick_size"] == pytest.approx(0.05)
    assert catalog_repo.get_instrument_by_token(1004) is None
