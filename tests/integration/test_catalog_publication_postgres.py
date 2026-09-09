"""Real-PostgreSQL publication acceptance for the instrument catalog (C3/C4).

Runs against an ISOLATED disposable PostgreSQL only. Set
``CATALOG_TEST_DATABASE_URL`` (e.g. a throwaway container) to enable; the
module skips cleanly when it is absent so unit suites never touch live data.

Example:

    docker run -d --name kite-test-postgres -e POSTGRES_PASSWORD=testonly \
      -e POSTGRES_DB=kite_test -p 15433:5432 postgres:16-alpine
    alembic -c backend/alembic.ini upgrade head   # with DATABASE_URL pointed at it
    CATALOG_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
      pytest tests/integration/test_catalog_publication_postgres.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import date

import pytest
import psycopg2
from sqlalchemy import create_engine, text

from backend.broker_api.instruments.catalog import (
    CatalogRefreshPublisher,
    InstrumentCatalog,
    RefreshFailure,
)

CATALOG_DB_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not CATALOG_DB_URL,
    reason="CATALOG_TEST_DATABASE_URL not set; real-PostgreSQL publication tests skipped",
)


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(CATALOG_DB_URL)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE public.instrument_catalog_staging, "
            "public.instrument_broker_mappings, public.instrument_catalog_records, "
            "public.instrument_catalog_generations CASCADE"
        ))
    yield engine
    engine.dispose()


@pytest.fixture()
def publisher_factory(engine):
    def _make(**kwargs):
        kwargs.setdefault("minimum_count", 1)
        return CatalogRefreshPublisher(
            connection_factory=lambda: psycopg2.connect(CATALOG_DB_URL), **kwargs
        )

    return _make


def _nse_rows(n):
    return [
        {
            "instrument_token": 100000 + i,
            "exchange_token": i,
            "tradingsymbol": f"SYM{i}",
            "name": f"SYM{i}",
            "instrument_type": "EQ",
            "segment": "NSE",
            "exchange": "NSE",
            "tick_size": 0.05,
            "lot_size": 1,
            "strike": None,
            "expiry": None,
        }
        for i in range(n)
    ]


def _mcx_rows(n):
    return [
        {
            "instrument_token": 200000 + i,
            "exchange_token": 1000 + i,
            "tradingsymbol": f"GOLD{i}FUT",
            "name": "GOLD",
            "instrument_type": "FUT",
            "segment": "MCX-FUT",
            "exchange": "MCX",
            "tick_size": 1,
            "lot_size": 100,
            "strike": None,
            "expiry": date(2026, 12, 28),
        }
        for i in range(n)
    ]


def _cds_rows(n):
    return [
        {
            "instrument_token": 300000 + i,
            "exchange_token": 2000 + i,
            "tradingsymbol": f"USDINR{i}FUT",
            "name": "USDINR",
            "instrument_type": "FUT",
            "segment": "CDS-FUT",
            "exchange": "CDS",
            "tick_size": 0.0025,
            "lot_size": 1000,
            "strike": None,
            "expiry": date(2026, 11, 26),
        }
        for i in range(n)
    ]


def _view_stats(engine):
    with engine.connect() as conn:
        generations = conn.execute(text(
            "SELECT COUNT(DISTINCT catalog_generation) FROM public.instrument_catalog_published_v"
        )).scalar()
        total = conn.execute(text(
            "SELECT COUNT(*) FROM public.instrument_catalog_published_v"
        )).scalar()
        mcx = conn.execute(text(
            "SELECT COUNT(*) FROM public.instrument_catalog_published_v WHERE exchange = 'MCX'"
        )).scalar()
    return int(generations), int(total), int(mcx)


def test_full_then_scoped_refresh_keeps_single_generation(engine, publisher_factory):
    publisher = publisher_factory()

    full = publisher.publish({"NSE": _nse_rows(5), "MCX": _mcx_rows(2)})
    assert full.status == "published"
    assert full.record_count == 7

    scoped = publisher.publish({"NSE": _nse_rows(6)})
    assert scoped.status == "published"

    generations, total, mcx = _view_stats(engine)
    # C3: the published view is ONE generation even after a scoped refresh,
    # and retained MCX rows did not disappear (Go's single-generation contract).
    assert generations == 1
    assert total == 8
    assert mcx == 2
    # freshness is not faked: MCX is retained from the FIRST generation
    assert scoped.exchange_sources["MCX"]["state"] == "retained"
    assert scoped.exchange_sources["MCX"]["source_generation"] == full.generation_id
    assert scoped.exchange_sources["MCX"]["observed_at"] is not None
    assert scoped.exchange_sources["NSE"]["state"] == "accepted"


def test_truncated_refresh_retains_previous_state(engine, publisher_factory):
    publisher = publisher_factory(coverage_floor_ratio=0.5)
    good = publisher.publish({"NSE": _nse_rows(6)})
    assert good.status == "published"

    # One-row truncated NSE download must NOT retire 5 valid instruments.
    truncated = publisher.publish({"NSE": _nse_rows(1)})
    assert truncated.status == "failed"
    assert "suspiciously truncated" in truncated.validation_errors["NSE"]

    catalog = InstrumentCatalog(db=lambda: engine.connect())
    assert catalog.health()["generation"] == good.generation_id
    generations, total, _mcx = _view_stats(engine)
    assert generations == 1
    assert total == 8  # nothing retired by the truncated payload


def test_failed_scoped_refresh_displaces_nothing(engine, publisher_factory):
    publisher = publisher_factory()

    good = publisher.publish({"NSE": _nse_rows(5), "MCX": _mcx_rows(1)})
    failed = publisher.publish({}, failures=[RefreshFailure("BSE", "download timed out")])
    assert failed.status == "failed"

    catalog = InstrumentCatalog(db=lambda: engine.connect())
    health = catalog.health()
    assert health["generation"] == good.generation_id  # last usable publication
    assert health["status"] in ("published", "degraded")
    assert health["latest_attempt"]["status"] == "failed"


def test_concurrent_publications_serialize_to_one_generation(engine, publisher_factory):
    """Three overlapping refreshes (different exchanges) must serialize and
    end in one coherent single-generation view covering all of them."""
    def _publish(exchange, rows):
        publisher = publisher_factory()
        return publisher.publish({exchange: rows})

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_publish, "NSE", _nse_rows(8)),
            pool.submit(_publish, "MCX", _mcx_rows(2)),
            pool.submit(_publish, "CDS", _cds_rows(3)),
        ]
        results = [f.result() for f in futures]

    assert all(r.status == "published" for r in results)
    generations, total, _mcx = _view_stats(engine)
    # serialized publications: the final view is one coherent last-writer state
    assert generations == 1
    assert total == 13
    with engine.connect() as conn:
        per_exchange = conn.execute(text(
            "SELECT exchange, COUNT(*) FROM public.instrument_catalog_published_v GROUP BY exchange ORDER BY exchange"
        )).all()
    assert {row[0]: int(row[1]) for row in per_exchange} == {"CDS": 3, "MCX": 2, "NSE": 8}
