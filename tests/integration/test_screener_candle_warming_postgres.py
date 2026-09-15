"""PostgreSQL proof for MCX screener candle warming (persistence + concurrency).

SQLite cannot prove this slice: the warmer's whole point is that daily bars are
persisted once, read back by the catalog-resolved token, and stay single-copy
under a concurrent backfill. Runs against the ISOLATED disposable PostgreSQL
only:

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_screener_candle_warming_postgres.py -q

No broker call is made: the ingestion adapter is faked, but it persists through
the REAL ``CandleStorage.upsert_candles`` path and the warmer reads through the
REAL ``PgCandleHistory`` path.
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from backend.screeners.candle_warming import ScreenerCandleWarmer
from backend.workflows.runtime import PgCandleHistory

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; PostgreSQL candle-warming suite skipped",
)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # 17:30 IST: MCX still open
TOKEN = 987_654_321
KEY = "MCX:SILVER10026SEPFUT"
REQUIRED_BARS = 5


@pytest.fixture(scope="module")
def pg_engine():
    engine = create_engine(PG_URL, poolclass=NullPool)
    yield engine
    engine.dispose()


def _clear_candles(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM public.historical_candles WHERE instrument_token = :token"),
            {"token": TOKEN},
        )


@pytest.fixture(autouse=True)
def isolated_candles(pg_engine):
    """Each test starts with no stored rows for the probe token."""
    _clear_candles(pg_engine)
    yield
    _clear_candles(pg_engine)


@pytest.fixture()
def storage_env(monkeypatch):
    """Point CandleStorage's own connection at the disposable database."""
    from urllib.parse import urlparse

    parsed = urlparse(PG_URL.replace("postgresql+psycopg2://", "postgresql://"))
    monkeypatch.setenv("DB_HOST", parsed.hostname or "127.0.0.1")
    monkeypatch.setenv("DB_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("DB_NAME", (parsed.path or "/kite_test").lstrip("/"))
    monkeypatch.setenv("DB_USER", parsed.username or "postgres")
    monkeypatch.setenv("DB_PASSWORD", parsed.password or "")
    import backend.app.database as database

    monkeypatch.setattr(database, "_SCHEMA_APPLIED", True)  # never re-create tables
    yield


def _sessions(engine) -> list[date]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ts FROM public.historical_candles "
                "WHERE instrument_token = :token AND interval = 'day' ORDER BY ts"
            ),
            {"token": TOKEN},
        ).fetchall()
    return [row[0].astimezone(IST).date() for row in rows]


def _trading_days(count: int, *, through: date) -> list[date]:
    days: list[date] = []
    cursor = through
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


class _PersistingIngestion:
    """Fake broker adapter that persists through the real storage path."""

    def __init__(self, *, fail: bool = False, days: list[date] | None = None) -> None:
        self.fail = fail
        self.days = days or _trading_days(8, through=date(2026, 9, 14))
        self.calls: list[int] = []

    async def ingest_historical_data(self, token, interval, from_dt, to_dt, force_refresh=False):
        self.calls.append(int(token))
        if self.fail:
            raise RuntimeError("broker rejected the request")
        from backend.broker_api.market.candle_storage import CandleStorage

        candles = [
            {
                "ts": datetime(day.year, day.month, day.day, tzinfo=IST),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10,
                "oi": 5,
            }
            for day in self.days
        ]
        inserted, updated = CandleStorage.upsert_candles(int(token), interval, candles)
        return {"status": "success", "inserted": inserted, "updated": updated}


class _Catalog:
    def __init__(self, lifecycle: str = "active") -> None:
        self.lifecycle = lifecycle

    def resolve_public_key(self, public_key: str):
        assert public_key == KEY
        return SimpleNamespace(
            broker_token=TOKEN,
            catalog_generation="generation-warming",
            lifecycle_status=self.lifecycle,
        )


def _warmer(engine, ingestion, *, catalog=None, required_bars=REQUIRED_BARS):
    return ScreenerCandleWarmer(
        PgCandleHistory(engine, {KEY: TOKEN}),
        catalog=catalog or _Catalog(),
        ingestion_factory=lambda: ingestion,
        required_bars=required_bars,
        clock=lambda: NOW,
    )


# ---------------------------------------------------------------------------


def test_backfill_persists_once_and_repeats_are_no_ops(pg_engine, storage_env):
    ingestion = _PersistingIngestion()
    warmer = _warmer(pg_engine, ingestion)

    first = warmer.ensure_members([KEY], session="mcx_commodity", as_of=NOW)
    assert first.status == "complete"
    assert first.warmed == 1
    assert ingestion.calls == [TOKEN]

    stored = _sessions(pg_engine)
    assert len(stored) == len(ingestion.days) == 8  # all final sessions persisted

    second = warmer.ensure_members([KEY], session="mcx_commodity", as_of=NOW)
    assert second.status == "complete"
    assert second.fresh == 1 and second.warmed == 0
    assert ingestion.calls == [TOKEN], "a warm member must not be fetched again"
    assert len(_sessions(pg_engine)) == 8, "repeat warming must not duplicate rows"


def test_the_forming_session_is_stored_but_not_counted(pg_engine, storage_env):
    today = NOW.astimezone(IST).date()
    ingestion = _PersistingIngestion(days=_trading_days(6, through=today))
    warmer = _warmer(pg_engine, ingestion, required_bars=7)

    outcome = warmer.ensure_members([KEY], session="mcx_commodity", as_of=NOW)

    # 6 sessions stored, today's is still forming -> 5 final bars < 7 required
    assert outcome.unavailable == 1
    assert outcome.members[0].bars == 5
    assert "5/7 final bars" in (outcome.members[0].detail or "")
    assert len(_sessions(pg_engine)) == 6, "the forming bar is still persisted as data"


def test_a_failed_fetch_is_unavailable_and_retryable(pg_engine, storage_env):
    failing = _PersistingIngestion(fail=True)
    warmer = _warmer(pg_engine, failing)

    outcome = warmer.ensure_members([KEY], session="mcx_commodity", as_of=NOW)

    assert outcome.status == "unavailable"
    assert "RuntimeError" in (outcome.members[0].detail or "")
    assert _sessions(pg_engine) == []

    # the retry succeeds because nothing was half-written
    recovery = _PersistingIngestion()
    retried = _warmer(pg_engine, recovery).ensure_members([KEY], session="mcx_commodity", as_of=NOW)
    assert retried.warmed == 1
    assert len(_sessions(pg_engine)) == 8


def test_concurrent_backfill_converges_to_one_row_set(pg_engine, storage_env):
    """Two warmers racing for the same member must not duplicate or deadlock."""
    ingestion = _PersistingIngestion()

    def run_warm():
        return _warmer(pg_engine, ingestion).ensure_members(
            [KEY], session="mcx_commodity", as_of=NOW
        ).status

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: run_warm(), range(2)))

    assert all(status == "complete" for status in statuses)
    sessions = _sessions(pg_engine)
    assert len(sessions) == len(set(sessions)) == 8


def test_expired_contract_never_binds_a_token(pg_engine, storage_env):
    ingestion = _PersistingIngestion()
    warmer = _warmer(pg_engine, ingestion, catalog=_Catalog(lifecycle="expired"))

    outcome = warmer.ensure_members([KEY], session="mcx_commodity", as_of=NOW)

    assert outcome.expired == 1
    assert ingestion.calls == []
    assert _sessions(pg_engine) == []
