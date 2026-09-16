"""Service-level tests for the universe membership subsystem.

Every test gets a fresh in-memory SQLite engine with the shared alerts
platform ``Base.metadata.create_all`` (``backend.workflows.repository.Base``
registers the ``universes``/``universe_revisions`` tables from
``backend.workflows.universes``). The index-ingestion tables
(``index_refresh_state`` / ``kite_ticker_tickers``) are created as minimal
raw-DDL SQLite fakes; membership itself comes from an injected
``index_constituents_loader`` fake and catalog resolution from a fake
catalog, mirroring the real Postgres semantics (active-lifecycle filtering,
generation capture, deduped exchange-qualified members).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

# A Postgres-style DATABASE_URL keeps backend.app.database's module-level
# engine constructible (psycopg2 is stubbed; nothing ever connects) — same
# pattern as tests/api/test_worker_workflows.py.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kite_algo_test")

from tests.support.test_support import install_dependency_stubs  # noqa: E402

install_dependency_stubs()

from backend.broker_api.instruments.catalog import (  # noqa: E402
    CatalogUnavailableError,
    InstrumentNotFoundError,
)
from backend.workflows.repository import Base  # noqa: E402
from backend.workflows.universes import (  # noqa: E402
    Universe,
    UniverseNotFoundError,
    UniverseRevision,
    UniverseService,
    UniverseSourceUnavailable,
    UniverseValidationError,
)
from sqlalchemy import create_engine, func, select, text, update  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

OWNER_A = "kite:paper-a"
OWNER_B = "kite:paper-b"

FAKE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS index_refresh_state (
        source_list VARCHAR(255) PRIMARY KEY,
        last_constituent_refresh_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kite_ticker_tickers (
        tradingsymbol VARCHAR(255),
        exchange VARCHAR(16),
        source_list VARCHAR(255),
        last_refreshed_at TIMESTAMP
    )
    """,
)


class FakeCatalog:
    """Stand-in for InstrumentCatalog.resolve_public_key (identity only)."""

    def __init__(self, descriptors=(), error=None, generation="generation-1"):
        self._descriptors = {d.public_key: d for d in descriptors}
        self._error = error
        self.generation = generation

    def resolve_public_key(self, key):
        if self._error is not None:
            raise self._error
        descriptor = self._descriptors.get(str(key).upper())
        if descriptor is None:
            raise InstrumentNotFoundError(f"instrument not found: {key}")
        return descriptor

    def health(self):
        return {"status": "published", "generation": self.generation}


def _descriptor(public_key, lifecycle_status="active", catalog_generation="generation-1"):
    return SimpleNamespace(
        public_key=public_key,
        lifecycle_status=lifecycle_status,
        catalog_generation=catalog_generation,
    )


def _loader(symbols_by_source):
    def loader(source_list):
        symbols = symbols_by_source.get(source_list)
        if not symbols:
            raise UniverseSourceUnavailable(f"index source list {source_list!r} unavailable")
        return list(symbols)

    return loader


def _count(factory, model):
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar())


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for statement in FAKE_DDL:
            conn.execute(text(statement))
    return engine


def _service(factory, **kwargs):
    return UniverseService(factory, **kwargs)


# ---------------------------------------------------------------------------
# explicit universes
# ---------------------------------------------------------------------------


def test_explicit_create_and_resolve_dedupes_and_qualifies_exchanges():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog(
        [
            _descriptor("NSE:RELIANCE"),
            _descriptor("BSE:RELIANCE"),
        ]
    )
    service = _service(factory, catalog=catalog)

    created = service.create_universe(
        OWNER_A,
        "reliance-both",
        "explicit",
        {"members": ["nse:reliance", "NSE:RELIANCE", "BSE:RELIANCE"]},
    )
    # lowercase input is uppercased, exact duplicates collapse, but NSE:X and
    # BSE:X stay DIFFERENT instruments.
    assert created["source_config"]["members"] == ["BSE:RELIANCE", "NSE:RELIANCE"]

    resolved = service.resolve_membership(OWNER_A, "reliance-both")
    assert resolved["revision"] == 1
    assert resolved["members"] == ["BSE:RELIANCE", "NSE:RELIANCE"]
    assert resolved["rejected"] == []
    assert resolved["source_generation"] == "generation-1"
    coverage = resolved["coverage"]
    assert coverage["resolved"] == 2
    assert coverage["rejected"] == 0
    assert coverage["source"] == "explicit"
    assert coverage["resolved_at"]
    assert coverage["source_freshness"] is None  # explicit: null freshness

    persisted = service.latest_revision(OWNER_A, "reliance-both")
    assert persisted is not None
    assert persisted["member_count"] == 2
    assert persisted["members"] == ["BSE:RELIANCE", "NSE:RELIANCE"]


def test_explicit_create_rejects_malformed_members_and_unknown_kinds():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory)

    for bad_config in (
        {},
        {"members": []},
        {"members": ["RELIANCE"]},  # missing exchange
        {"members": ["NSE:RE:LIANCE"]},  # two colons
        {"members": ["NSE:"]},  # empty symbol
        {"members": [":RELIANCE"]},  # empty exchange
        {"members": [123]},
    ):
        try:
            service.create_universe(OWNER_A, "bad", "explicit", bad_config)
        except UniverseValidationError:
            pass
        else:
            raise AssertionError(f"expected UniverseValidationError for {bad_config}")

    try:
        service.create_universe(OWNER_A, "bad", "telepathic", {})
    except UniverseValidationError:
        pass
    else:
        raise AssertionError("unknown kind must be rejected")

    assert _count(factory, Universe) == 0


def test_duplicate_create_raises_universe_exists():
    from backend.workflows.universes import UniverseExistsError

    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory)
    service.create_universe(OWNER_A, "dupe", "explicit", {"members": ["NSE:X"]})
    try:
        service.create_universe(OWNER_A, "dupe", "explicit", {"members": ["NSE:Y"]})
    except UniverseExistsError:
        pass
    else:
        raise AssertionError("duplicate (owner_id, name) must raise UniverseExistsError")
    assert _count(factory, Universe) == 1


def test_catalog_active_filtering_records_rejected_never_silently_drops():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog(
        [
            _descriptor("NSE:GOOD"),
            _descriptor("NSE:EXPIRED", lifecycle_status="expired"),
        ]
    )
    service = _service(factory, catalog=catalog)
    service.create_universe(
        OWNER_A,
        "mixed",
        "explicit",
        {"members": ["NSE:GOOD", "NSE:EXPIRED", "NSE:GONE"]},
    )

    resolved = service.resolve_membership(OWNER_A, "mixed")
    assert resolved["members"] == ["NSE:GOOD"]
    assert resolved["rejected"] == [
        {"key": "NSE:EXPIRED", "reason": "expired"},
        {"key": "NSE:GONE", "reason": "not_found"},
    ]
    assert resolved["coverage"]["resolved"] == 1
    assert resolved["coverage"]["rejected"] == 2

    persisted = service.latest_revision(OWNER_A, "mixed")
    assert persisted["members"] == ["NSE:GOOD"]
    assert persisted["coverage"]["rejected"] == 2


def test_ambiguous_member_is_rejected_not_fatal():
    from backend.broker_api.instruments.catalog import AmbiguousInstrumentError

    class AmbiguousCatalog(FakeCatalog):
        def resolve_public_key(self, key):
            raise AmbiguousInstrumentError("ambiguous")

    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory, catalog=AmbiguousCatalog())
    service.create_universe(OWNER_A, "amb", "explicit", {"members": ["NSE:X"]})
    resolved = service.resolve_membership(OWNER_A, "amb")
    assert resolved["members"] == []
    assert resolved["rejected"] == [{"key": "NSE:X", "reason": "ambiguous"}]


def test_catalog_unavailable_propagates_and_persists_nothing():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog(error=CatalogUnavailableError("catalog database down"))
    service = _service(factory, catalog=catalog)
    service.create_universe(OWNER_A, "broken", "explicit", {"members": ["NSE:X"]})

    try:
        service.resolve_membership(OWNER_A, "broken")
    except CatalogUnavailableError:
        pass
    else:
        raise AssertionError("CatalogUnavailableError must propagate")

    assert _count(factory, UniverseRevision) == 0


# ---------------------------------------------------------------------------
# index universes
# ---------------------------------------------------------------------------


def test_index_source_via_loader_and_freshness_in_coverage():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog([_descriptor("NSE:RELIANCE"), _descriptor("NSE:TCS")])
    service = _service(
        factory,
        catalog=catalog,
        index_constituents_loader=_loader({"Nifty50": ["RELIANCE", "TCS"]}),
    )
    service.create_universe(OWNER_A, "n50", "index", {"source_list": "Nifty50"})

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO index_refresh_state (source_list, last_constituent_refresh_at) "
                "VALUES ('Nifty50', '2026-09-01 06:00:00.000000')"
            )
        )
        session.execute(
            text(
                "INSERT INTO kite_ticker_tickers (tradingsymbol, exchange, source_list, last_refreshed_at) "
                "VALUES ('RELIANCE', 'NSE', 'Nifty50', '2026-09-02 06:00:00.000000')"
            )
        )
        session.commit()

    resolved = service.resolve_membership(OWNER_A, "n50")
    assert resolved["members"] == ["NSE:RELIANCE", "NSE:TCS"]  # bare symbols -> NSE-qualified
    freshness = resolved["coverage"]["source_freshness"]
    assert freshness["source_list"] == "Nifty50"
    assert freshness["last_constituent_refresh_at"] == "2026-09-01T06:00:00+00:00"
    # max(last_refreshed_at) across the state row and constituent rows
    assert freshness["last_refreshed_at"] == "2026-09-02T06:00:00+00:00"
    assert resolved["coverage"]["source"] == "index"


def test_unavailable_index_source_raises_and_persists_nothing():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(
        factory,
        catalog=FakeCatalog(),
        index_constituents_loader=_loader({}),  # every list unknown/empty
    )
    service.create_universe(OWNER_A, "n50", "index", {"source_list": "Nifty50"})

    try:
        service.resolve_membership(OWNER_A, "n50")
    except UniverseSourceUnavailable:
        pass
    else:
        raise AssertionError("unavailable index source must raise")

    assert _count(factory, UniverseRevision) == 0


def test_index_loader_wraps_unexpected_failure_as_unavailable():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def boom(source_list):
        raise RuntimeError("ingestion cache corrupted")

    service = _service(factory, catalog=FakeCatalog(), index_constituents_loader=boom)
    service.create_universe(OWNER_A, "n50", "index", {"source_list": "Nifty50"})
    try:
        service.resolve_membership(OWNER_A, "n50")
    except UniverseSourceUnavailable as exc:
        assert "ingestion cache corrupted" in str(exc)
    else:
        raise AssertionError("loader crash must surface as UniverseSourceUnavailable")
    assert _count(factory, UniverseRevision) == 0


def test_unknown_index_source_list_is_rejected_at_creation():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory)
    try:
        service.create_universe(OWNER_A, "weird", "index", {"source_list": "Sensex30"})
    except UniverseValidationError as exc:
        assert "Sensex30" in str(exc)
    else:
        raise AssertionError("unsupported source_list must be rejected at creation")
    assert _count(factory, Universe) == 0


def test_default_index_loader_reads_ticker_rows_with_exchange_qualification():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog([_descriptor("NSE:BAR"), _descriptor("NSE:REL"), _descriptor("BSE:FOO")])
    # No injected loader: exercise the default kite_ticker_tickers reader.
    service = _service(factory, catalog=catalog)
    service.create_universe(OWNER_A, "n50", "index", {"source_list": "Nifty50"})

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO kite_ticker_tickers (tradingsymbol, exchange, source_list) "
                "VALUES ('BAR', 'NSE', 'Nifty50')"
            )
        )
        session.execute(
            text(
                "INSERT INTO kite_ticker_tickers (tradingsymbol, exchange, source_list) "
                "VALUES ('REL', NULL, 'Nifty50')"
            )
        )
        session.execute(
            text(
                "INSERT INTO kite_ticker_tickers (tradingsymbol, exchange, source_list) "
                "VALUES ('FOO', 'BSE', 'Nifty50')"
            )
        )
        session.commit()

    resolved = service.resolve_membership(OWNER_A, "n50")
    # row exchange wins; missing exchange defaults to NSE; NSE:BAR and BSE:FOO
    # are DIFFERENT instruments and duplicates collapse.
    assert resolved["members"] == ["BSE:FOO", "NSE:BAR", "NSE:REL"]


def test_default_index_loader_raises_unavailable_when_empty():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory, catalog=FakeCatalog())
    service.create_universe(OWNER_A, "n50", "index", {"source_list": "Nifty50"})
    try:
        service.resolve_membership(OWNER_A, "n50")
    except UniverseSourceUnavailable:
        pass
    else:
        raise AssertionError("empty source list must raise UniverseSourceUnavailable")
    assert _count(factory, UniverseRevision) == 0


# ---------------------------------------------------------------------------
# portfolio universes
# ---------------------------------------------------------------------------


def test_portfolio_provider_wiring():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog([_descriptor("NSE:AAA"), _descriptor("BSE:BBB")])

    def provider(owner_id, source_config):
        assert owner_id == OWNER_A
        assert source_config == {"focus": "largecap"}
        return ["nse:aaa", "BSE:BBB", "nse:aaa"]

    service = _service(factory, catalog=catalog, portfolio_provider=provider)
    created = service.create_universe(
        OWNER_A, "my-holdings", "portfolio", {"focus": "largecap"}
    )
    assert created["kind"] == "portfolio"

    resolved = service.resolve_membership(OWNER_A, "my-holdings")
    assert resolved["members"] == ["BSE:BBB", "NSE:AAA"]
    assert resolved["coverage"]["source"] == "portfolio"
    assert resolved["coverage"]["source_freshness"]["owner_id"] == OWNER_A
    assert resolved["coverage"]["source_freshness"]["last_refreshed_at"]


def test_portfolio_provider_failure_is_unavailable_and_persists_nothing():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def broken(owner_id, source_config):
        raise RuntimeError("broker session expired")

    service = _service(factory, catalog=FakeCatalog(), portfolio_provider=broken)
    service.create_universe(OWNER_A, "my-holdings", "portfolio", {})
    try:
        service.resolve_membership(OWNER_A, "my-holdings")
    except UniverseSourceUnavailable as exc:
        assert "broker session expired" in str(exc)
    else:
        raise AssertionError("provider crash must surface as UniverseSourceUnavailable")
    assert _count(factory, UniverseRevision) == 0


def test_portfolio_universe_without_provider_is_rejected_at_creation():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory)  # no portfolio_provider wired
    try:
        service.create_universe(OWNER_A, "my-holdings", "portfolio", {})
    except UniverseValidationError as exc:
        assert "provider" in str(exc).lower()
    else:
        raise AssertionError("portfolio without provider must be rejected")
    assert _count(factory, Universe) == 0


# ---------------------------------------------------------------------------
# revisions, history, concurrency semantics
# ---------------------------------------------------------------------------


def test_revision_monotonicity_across_sequential_resolves():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    descriptors = [_descriptor("NSE:X"), _descriptor("BSE:X", catalog_generation="generation-2")]
    catalog = FakeCatalog(descriptors)
    service = _service(factory, catalog=catalog)
    service.create_universe(OWNER_A, "x", "explicit", {"members": ["NSE:X", "BSE:X"]})

    first = service.resolve_membership(OWNER_A, "x")
    second = service.resolve_membership(OWNER_A, "x")
    third = service.resolve_membership(OWNER_A, "x")
    assert [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]

    revisions = service.list_revisions(OWNER_A, "x")
    assert [revision["revision"] for revision in revisions] == [3, 2, 1]
    latest = service.latest_revision(OWNER_A, "x")
    assert latest["revision"] == 3
    assert latest["members"] == ["BSE:X", "NSE:X"]
    assert _count(factory, UniverseRevision) == 3


def test_latest_revision_and_revision_at_for_historical_attribution():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog([_descriptor("NSE:X")])
    service = _service(factory, catalog=catalog)
    service.create_universe(OWNER_A, "x", "explicit", {"members": ["NSE:X"]})
    service.resolve_membership(OWNER_A, "x")
    service.resolve_membership(OWNER_A, "x")

    # Pin deterministic resolved_at values (SQLite round-trips naive UTC).
    with factory() as session:
        session.execute(
            update(UniverseRevision)
            .where(UniverseRevision.revision == 1)
            .values(resolved_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
        )
        session.execute(
            update(UniverseRevision)
            .where(UniverseRevision.revision == 2)
            .values(resolved_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc))
        )
        session.commit()

    latest = service.latest_revision(OWNER_A, "x")
    assert latest["revision"] == 2

    at = lambda *args: service.revision_at(OWNER_A, "x", *args)
    assert at(datetime(2026, 8, 31, tzinfo=timezone.utc)) is None
    assert at(datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))["revision"] == 1
    assert at(datetime(2026, 9, 3, tzinfo=timezone.utc))["revision"] == 1
    assert at(datetime(2026, 9, 9, tzinfo=timezone.utc))["revision"] == 2
    # naive datetimes are treated as UTC
    assert at(datetime(2026, 9, 2))["revision"] == 1


def test_owner_isolation():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog([_descriptor("NSE:X")])
    service = _service(factory, catalog=catalog)
    service.create_universe(OWNER_A, "private", "explicit", {"members": ["NSE:X"]})

    try:
        service.get_universe(OWNER_B, "private")
    except UniverseNotFoundError:
        pass
    else:
        raise AssertionError("owner B must not see owner A's universe")

    try:
        service.resolve_membership(OWNER_B, "private")
    except UniverseNotFoundError:
        pass
    else:
        raise AssertionError("owner B must not resolve owner A's universe")

    assert service.list_universes(OWNER_B) == []
    try:
        service.latest_revision(OWNER_B, "private")
    except UniverseNotFoundError:
        pass
    else:
        raise AssertionError("owner B must not read owner A's latest revision")
    try:
        service.revision_at(OWNER_B, "private", datetime.now(timezone.utc))
    except UniverseNotFoundError:
        pass
    else:
        raise AssertionError("owner B must not read owner A's revisions")


# ---------------------------------------------------------------------------
# preview (zero persistent side effects)
# ---------------------------------------------------------------------------


def test_preview_membership_resolves_without_persisting():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = FakeCatalog(
        [_descriptor("NSE:GOOD"), _descriptor("NSE:EXPIRED", lifecycle_status="expired")]
    )
    service = _service(
        factory,
        catalog=catalog,
        index_constituents_loader=_loader({"Nifty50": ["GOOD", "EXPIRED"]}),
    )

    explicit = service.preview_membership(
        OWNER_A, "explicit", {"members": ["NSE:GOOD", "NSE:EXPIRED", "NSE:GONE"]}
    )
    assert explicit["members"] == ["NSE:GOOD"]
    assert [item["key"] for item in explicit["rejected"]] == ["NSE:EXPIRED", "NSE:GONE"]
    assert "revision" not in explicit

    index = service.preview_membership(OWNER_A, "index", {"source_list": "Nifty50"})
    assert index["members"] == ["NSE:GOOD"]

    assert _count(factory, Universe) == 0
    assert _count(factory, UniverseRevision) == 0


def test_preview_unavailable_source_raises():
    engine = _engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = _service(factory, catalog=FakeCatalog(), index_constituents_loader=_loader({}))
    try:
        service.preview_membership(OWNER_A, "index", {"source_list": "Nifty50"})
    except UniverseSourceUnavailable:
        pass
    else:
        raise AssertionError("preview of unavailable source must raise")
    assert _count(factory, UniverseRevision) == 0
