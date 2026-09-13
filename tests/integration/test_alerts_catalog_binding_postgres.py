"""Materialization without the instrument catalog present (PostgreSQL only).

Why this file exists
--------------------
``EvaluationService._resolve_catalog_binding`` documents that "alert tests and
pre-migration deployments continue to materialize subscriptions without a
binding". On SQLite that was true. On PostgreSQL it was not: a failed statement
aborts the surrounding transaction *even when the Python exception is handled*,
so catching ``CatalogUnavailableError`` and returning ``None`` left the
caller's transaction poisoned and the following ``INSERT`` failed with
"current transaction is aborted".

That is exactly the deployment the branch exists to support, so the guarantee
was false where it mattered. The fix is a SAVEPOINT around the catalog lookup.
This suite is the only place that can catch a regression, because the failure
mode does not exist in SQLite.

    ALERTS_TEST_DATABASE_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \\
        pytest tests/integration/test_alerts_catalog_binding_postgres.py -q
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.workflows import advanced_repository  # noqa: F401 (table registration)
from backend.workflows.compiler import compile_document
from backend.workflows.models import WorkflowDocument  # noqa: F401 (type reference)
from backend.workflows.parser import parse_workflow_dict
from backend.workflows.repository import (
    AlertSubscription,
    Base,
    SqlAlchemyWorkflowRepository,
)
from backend.workflows.service import EvaluationService

PG_URL = os.environ.get("ALERTS_TEST_DATABASE_URL", "")
PROBE_DATABASE = "kite_catalog_probe"

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ALERTS_TEST_DATABASE_URL not set; catalog-binding PostgreSQL suite skipped",
)

DOCUMENT = {
    "version": 1,
    "name": "no-catalog-materialization",
    "session": "nse_equity",
    "instruments": ["NSE:AAA", "NSE:BBB"],
    "stages": [
        {
            "id": "px",
            "type": "signal",
            "clock": "candle_close",
            "timeframe": "5minute",
            "conditions": {
                "all": [
                    {"left": {"field": "close"}, "op": "crosses_above", "right": {"value": 100}}
                ]
            },
        }
    ],
    "alerts": [{"id": "a1", "source": "px", "trigger": "on_transition", "channels": ["ops"]}],
}


@pytest.fixture(scope="module")
def probe_database():
    """A dedicated database holding the workflow tables and NO catalog tables.

    A whole database is used rather than a schema so the absence of the catalog
    is real: the ORM tables land in the default ``public`` schema, and nothing
    creates ``kite_ticker_tickers`` because that DDL lives in raw SQL, not in
    the metadata.
    """
    url = make_url(PG_URL)
    admin = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {PROBE_DATABASE}"))
        connection.execute(text(f"CREATE DATABASE {PROBE_DATABASE}"))
    admin.dispose()

    engine = create_engine(url.set(database=PROBE_DATABASE), poolclass=NullPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()

    admin = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {PROBE_DATABASE}"))
    admin.dispose()


def test_the_probe_database_really_has_no_catalog(probe_database) -> None:
    """Guard the premise: if the catalog were present, the test below would
    pass vacuously without exercising the failure path at all."""
    with probe_database.connect() as connection:
        present = connection.execute(
            text("SELECT to_regclass('public.kite_ticker_tickers')")
        ).scalar()
    assert present is None, "probe database unexpectedly has a catalog table"


def test_materialization_survives_an_absent_catalog(probe_database) -> None:
    """Subscriptions materialize with no binding instead of aborting.

    Regression: before the savepoint, the catalog lookup raised
    ``CatalogUnavailableError`` (a missing relation), the handler returned
    ``None``, and the very next INSERT failed with ``InFailedSqlTransaction`` —
    the transaction was already dead.
    """
    session_factory = sessionmaker(bind=probe_database, expire_on_commit=False)
    repo = SqlAlchemyWorkflowRepository(session_factory)
    compiled = compile_document(parse_workflow_dict(DOCUMENT))
    workflow, revision = repo.create_workflow(
        "owner-no-catalog",
        "no-catalog-materialization",
        compiled.document.to_document_dict(),
        compiled.canonical_hash,
    )
    repo.activate_revision(workflow.id, revision.id)
    active = repo.get_active_revision(workflow.id)

    # The assertion is that this does not raise: the old behaviour surfaced as
    # an aborted transaction on the following INSERT, not as an exception here.
    created = EvaluationService(repo, session_factory).ensure_subscriptions(active)
    assert created == 2

    with session_factory() as session:
        rows = session.execute(select(AlertSubscription)).scalars().all()
        assert len(rows) == 2

    # No binding — the documented degraded state, not a failure.
    assert all("instrument_binding" not in (row.config or {}) for row in rows)
