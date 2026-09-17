"""Durable strategy attribution on PostgreSQL: the adversarial scenario list.

Why PostgreSQL: this suite proves what SQLite structurally cannot — composite-FK
identity integrity (owner/account/environment cannot drift), the insert-only
trigger on ``strategy_run_bindings``, ``ON DELETE RESTRICT`` durability in both
directions, advisory-lock serialization of recomputes, and per-fact canonical
instrument resolution against real generation windows.

Every test runs against a DISPOSABLE, uniquely named database created on the
test server, upgraded with ``alembic upgrade head`` and dropped afterwards. No
existing database is ever touched.

    ATTRIBUTION_PG_URL='postgresql://postgres:testonly@127.0.0.1:15433/kite_test' \
        .venv/bin/pytest tests/integration/test_durable_strategy_attribution_postgres.py -q

Run this file in its own pytest invocation (other suites stub ``psycopg2``).
Skipped (not failed) when no database URL is configured.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.api.repositories.algo_worker_repo import WorkerToken  # noqa: E402
from backend.api.schemas.worker import WorkerRunCreateRequest  # noqa: E402
from backend.strategies.attribution import (  # noqa: E402
    SqlAttributionStore,
    StrategyAttributionService,
)
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402

PG_URL = os.environ.get("ATTRIBUTION_PG_URL") or os.environ.get("ALERTS_TEST_DATABASE_URL", "")

if not getattr(psycopg2, "__file__", None):
    pytest.skip(
        "psycopg2 is stubbed in this process; run this suite in its own invocation",
        allow_module_level=True,
    )
if not PG_URL:
    pytest.skip(
        "ATTRIBUTION_PG_URL / ALERTS_TEST_DATABASE_URL not set; disposable PostgreSQL unavailable",
        allow_module_level=True,
    )


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
    dbname = f"kite_attr_{uuid.uuid4().hex[:12]}"
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
    """A disposable database upgraded only to the pre-attribution head."""
    dbname = f"kite_attr_pre_{uuid.uuid4().hex[:10]}"
    _create_database(dbname)
    db_url = _url_for(dbname)
    engine = create_engine(db_url, pool_pre_ping=True)
    _upgrade(db_url, "20260915_000024")
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


def _rows(session_factory, sql, params=None):
    with session_factory() as session:
        return session.execute(text(sql), params or {}).fetchall()


def seed_strategy(sf, *, sid="stg-1", owner="app:o", name=None, account="kite:A", status="active"):
    _exec(
        sf,
        "INSERT INTO public.strategies (id, owner_id, name, account_scope, status) "
        "VALUES (:sid, :owner, :name, :account, :status)",
        {"sid": sid, "owner": owner, "name": name or f"Strategy {sid}", "account": account, "status": status},
    )
    return sid


def seed_token(sf, *, token_id="tok-1", status="active", account_scope="kite:A"):
    _exec(
        sf,
        "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope, status) "
        "VALUES (:token_id, :name, :hash, :account, :status)",
        {"token_id": token_id, "name": token_id, "hash": f"hash-{token_id}", "account": account_scope, "status": status},
    )
    return token_id


def seed_run(sf, *, run_id="run-1", token_id="tok-1", template="tmpl", account="kite:A", mode="live", status="open"):
    _exec(
        sf,
        "INSERT INTO public.algo_worker_runs "
        "(strategy_run_id, token_id, template_id, account_scope, execution_mode, status) "
        "VALUES (:run_id, :token_id, :template, :account, :mode, :status)",
        {"run_id": run_id, "token_id": token_id, "template": template, "account": account, "mode": mode, "status": status},
    )
    return run_id


def seed_binding(sf, *, run_id, sid, owner="app:o", account="kite:A", env="live", source="hosted_job"):
    store = SqlAttributionStore(session_factory=sf)
    store.bind_run(
        strategy_run_id=run_id, strategy_id=sid, owner_id=owner, account_id=account,
        execution_environment=env, bound_by="test", binding_source=source,
    )


def seed_link(sf, *, account="kite:A", order_id="OID-1", run_id="run-1", trade_id=None):
    _exec(
        sf,
        "INSERT INTO public.worker_live_execution_links "
        "(strategy_run_id, account_id, broker_order_id, trade_id, client_order_ref) "
        "VALUES (:run_id, :account, :order_id, :trade_id, :ref)",
        {"run_id": run_id, "account": account, "order_id": order_id, "trade_id": trade_id, "ref": f"KA{order_id}"},
    )


def seed_intent(sf, *, account="kite:A", order_id="OID-I", run_id="run-1", status="placed"):
    _exec(
        sf,
        "INSERT INTO public.live_order_intents "
        "(intent_id, client_order_ref, account_id, strategy_run_id, strategy_family, strategy_name, "
        " execution_mode, entry_surface, broker_order_id, status) "
        "VALUES (:intent_id, :ref, :account, :run_id, 'f', 'n', 'live', 's', :order_id, :status)",
        {
            "intent_id": f"it-{uuid.uuid4().hex[:8]}",
            "ref": f"KAI{uuid.uuid4().hex[:6]}".upper(),
            "account": account,
            "run_id": run_id,
            "order_id": order_id,
            "status": status,
        },
    )


def seed_fill(
    sf, *, account="kite:A", order_id="OID-1", trade_id="T-1", token=738561,
    exchange="NSE", symbol="RELIANCE", product="CNC", side="BUY", qty=100, at="2026-09-01T10:00:00+00:00",
):
    _exec(
        sf,
        "INSERT INTO public.order_trade_fills "
        "(account_id, trade_id, order_id, instrument_token, exchange, tradingsymbol, product, "
        " transaction_type, quantity, price, fill_timestamp, payload_json) "
        "VALUES (:account, :trade_id, :order_id, :token, :exchange, :symbol, :product, "
        " :side, :qty, 100, :at, '{}'::jsonb)",
        {
            "account": account, "trade_id": trade_id, "order_id": order_id, "token": token,
            "exchange": exchange, "symbol": symbol, "product": product, "side": side,
            "qty": qty, "at": at,
        },
    )


def seed_generation(sf, *, published_at, status="published"):
    gid = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
        "VALUES (:gid, :status, :published_at)",
        {"gid": gid, "status": status, "published_at": published_at},
    )
    return gid


def seed_instrument(sf, *, symbol="RELIANCE", exchange="NSE"):
    iid = str(uuid.uuid4())
    _exec(
        sf,
        "INSERT INTO public.instrument_catalog_records "
        "(instrument_id, identity_key, public_key, exchange, tradingsymbol) "
        "VALUES (:iid, :ikey, :pkey, :exchange, :symbol)",
        {"iid": iid, "ikey": f"NSE:{symbol}:{iid[:8]}", "pkey": f"NSE:{symbol}", "exchange": exchange, "symbol": symbol},
    )
    return iid


def seed_mapping(sf, *, instrument_id, token=738561, exchange="NSE", symbol="RELIANCE", from_gen, to_gen=None, is_current=True):
    _exec(
        sf,
        "INSERT INTO public.instrument_broker_mappings "
        "(instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
        " valid_from_generation, valid_to_generation, is_current) "
        "VALUES (:iid, 'kite', :exchange, :symbol, :token, :from_gen, :to_gen, :is_current)",
        {
            "iid": instrument_id, "exchange": exchange, "symbol": symbol, "token": token,
            "from_gen": from_gen, "to_gen": to_gen, "is_current": is_current,
        },
    )


def seed_paper_account(sf, *, scope="kite:paper-a"):
    _exec(
        sf,
        "INSERT INTO public.paper_accounts (account_scope) VALUES (:scope) ON CONFLICT DO NOTHING",
        {"scope": scope},
    )


def seed_paper_order(sf, *, scope="kite:paper-a", order_id="PO-1", token=738561, symbol="RELIANCE", product="CNC", run_id="run-p"):
    _exec(
        sf,
        "INSERT INTO public.paper_orders "
        "(account_scope, order_id, instrument_token, exchange, tradingsymbol, product, transaction_type, "
        " quantity, status, metadata_json) "
        "VALUES (:scope, :order_id, :token, 'NSE', :symbol, :product, 'buy', 100, 'filled', "
        " CAST(:meta AS JSONB))",
        {
            "scope": scope, "order_id": order_id, "token": token, "symbol": symbol,
            "product": product, "meta": '{"attribution": {"strategy_run_id": "%s"}}' % run_id,
        },
    )


def seed_paper_trade(sf, *, scope="kite:paper-a", trade_id="PT-1", order_id="PO-1", side="buy", qty=100, at="2026-09-01T10:00:00+00:00"):
    _exec(
        sf,
        "INSERT INTO public.paper_trades "
        "(account_scope, trade_id, order_id, instrument_token, transaction_type, quantity, price, trade_timestamp) "
        "VALUES (:scope, :trade_id, :order_id, 738561, :side, :qty, 100, :at)",
        {"scope": scope, "trade_id": trade_id, "order_id": order_id, "side": side, "qty": qty, "at": at},
    )


def _service(sf) -> StrategyAttributionService:
    return StrategyAttributionService(SqlAttributionStore(session_factory=sf))


def _publish(sf, **kwargs):
    return asyncio.run(_service(sf).publish(**kwargs))


def _positions(sf, **kwargs):
    return asyncio.run(_service(sf).open_positions(**kwargs))


# ---------------------------------------------------------------------------
# 1. migration + backfill
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migration_head_and_schema_shape(self, disposable_db):
        assert _scalar(disposable_db, "SELECT version_num FROM alembic_version") >= "20260917_000025"

        constraints = {
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT conname FROM pg_constraint WHERE conname IN ("
                "'uq_strategies_id_owner_account', 'uq_strategies_id_account', "
                "'uq_strategies_owner_name', 'fk_hosted_strategies_canonical', "
                "'uq_algo_worker_runs_id_mode', 'ck_binding_environment', 'ck_binding_source', "
                "'ck_spp_identity_consistency', 'ck_spp_environment')",
            )
        }
        assert constraints == {
            "uq_strategies_id_owner_account", "uq_strategies_id_account", "uq_strategies_owner_name",
            "fk_hosted_strategies_canonical", "uq_algo_worker_runs_id_mode", "ck_binding_environment",
            "ck_binding_source", "ck_spp_identity_consistency", "ck_spp_environment",
        }
        triggers = {
            row[0]
            for row in _rows(
                disposable_db,
                "SELECT tgname FROM pg_trigger WHERE tgname = 'trg_strategy_run_bindings_immutable'",
            )
        }
        assert triggers == {"trg_strategy_run_bindings_immutable"}

    def test_upgrade_from_prior_head_backfills_hosted_ids(self, prior_head_db):
        engine = create_engine(prior_head_db, pool_pre_ping=True)
        try:
            factory = sessionmaker(bind=engine)
            # A hosted strategy exists BEFORE the attribution migration.
            _exec(
                factory,
                "INSERT INTO public.hosted_strategies "
                "(id, owner_id, name, template_id, default_account_scope, max_duration_s, "
                " progress_deadline_s, stale_exit_policy, status) "
                "VALUES ('stg-legacy', 'app:o', 'Legacy', 'hosted:stg-legacy', 'kite:paper', "
                " 3600, 600, 'none', 'disabled')",
            )
            _upgrade(prior_head_db, "head")

            rows = _rows(
                factory,
                "SELECT s.id, s.owner_id, s.account_scope, s.status, h.id, h.owner_id, h.default_account_scope "
                "FROM public.strategies s JOIN public.hosted_strategies h ON h.id = s.id",
            )
            assert len(rows) == 1
            sid, owner, account, status, host_id, host_owner, host_account = rows[0]
            assert sid == host_id == "stg-legacy"           # ids preserved
            assert owner == host_owner == "app:o"           # owner mirrored
            assert account == host_account == "kite:paper"  # account mirrored
            assert status == "disabled"                     # hosted status carried over
            assert _scalar(factory, "SELECT version_num FROM alembic_version") >= "20260917_000025"
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# 2/3. database-enforced integrity + immutability
# ---------------------------------------------------------------------------


class TestIntegrity:
    def _bound(self, sf):
        seed_strategy(sf)
        seed_token(sf)
        seed_run(sf)
        seed_binding(sf, run_id="run-1", sid="stg-1")

    def test_binding_immutability_trigger(self, disposable_db):
        self._bound(disposable_db)
        with pytest.raises(Exception) as update_exc:
            _exec(disposable_db, "UPDATE public.strategy_run_bindings SET bound_by='x' WHERE strategy_run_id='run-1'")
        assert "immutable" in str(update_exc.value).lower()

        with pytest.raises(Exception) as delete_exc:
            _exec(disposable_db, "DELETE FROM public.strategy_run_bindings WHERE strategy_run_id='run-1'")
        assert "immutable" in str(delete_exc.value).lower()

        with pytest.raises(Exception):
            seed_binding(disposable_db, run_id="run-1", sid="stg-1")

    def test_binding_account_and_environment_integrity_is_database_enforced(self, disposable_db):
        self._bound(disposable_db)

        # A binding whose owner/account disagrees with the canonical strategy.
        seed_token(disposable_db, token_id="tok-2")
        seed_run(disposable_db, run_id="run-2", token_id="tok-2")
        with pytest.raises(Exception):
            seed_binding(disposable_db, run_id="run-2", sid="stg-1", account="kite:OTHER")
        with pytest.raises(Exception):
            seed_binding(disposable_db, run_id="run-2", sid="stg-1", owner="app:other")

        # A binding whose environment differs from the run's persisted mode.
        seed_token(disposable_db, token_id="tok-3")
        seed_run(disposable_db, run_id="run-3", token_id="tok-3", mode="paper")
        with pytest.raises(Exception):
            seed_binding(disposable_db, run_id="run-3", sid="stg-1", env="live")

        # The matching case is accepted.
        seed_binding(disposable_db, run_id="run-3", sid="stg-1", env="paper")

    def test_hosted_adapter_drift_is_refused_by_the_composite_fk(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-1", owner="app:o", account="kite:A")
        _exec(
            disposable_db,
            "INSERT INTO public.hosted_strategies "
            "(id, owner_id, name, template_id, default_account_scope, max_duration_s, "
            " progress_deadline_s, stale_exit_policy) "
            "VALUES ('stg-1', 'app:o', 'Strategy stg-1', 'hosted:stg-1', 'kite:A', 3600, 600, 'none')",
        )
        # Direct SQL drift on either mirror column is refused by the database.
        with pytest.raises(Exception):
            _exec(disposable_db, "UPDATE public.hosted_strategies SET owner_id='app:other' WHERE id='stg-1'")
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "UPDATE public.hosted_strategies SET default_account_scope='kite:OTHER' WHERE id='stg-1'",
            )

    def test_projection_account_integrity_is_database_enforced(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-1", account="kite:A")
        # A projection row whose account differs from the canonical strategy's.
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "INSERT INTO public.strategy_position_projection "
                "(account_id, strategy_id, execution_environment, identity_kind, identity_key, product, "
                " canonical_instrument_id, instrument_token, exchange, tradingsymbol, net_quantity, "
                " projection_version) "
                "VALUES ('kite:OTHER', 'stg-1', 'live', 'canonical', :key, 'CNC', :key, 1, 'NSE', 'X', 1, 1)",
                {"key": str(uuid.uuid4())},
            )
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "INSERT INTO public.strategy_projection_state "
                "(account_id, strategy_id, execution_environment, projection_version) "
                "VALUES ('kite:OTHER', 'stg-1', 'live', 1)",
            )
        # Matching-account rows insert fine, and identity consistency is checked.
        _exec(
            disposable_db,
            "INSERT INTO public.strategy_projection_state "
            "(account_id, strategy_id, execution_environment, projection_version) "
            "VALUES ('kite:A', 'stg-1', 'live', 1)",
        )
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "INSERT INTO public.strategy_position_projection "
                "(account_id, strategy_id, execution_environment, identity_kind, identity_key, product, "
                " canonical_instrument_id, instrument_token, exchange, tradingsymbol, net_quantity, "
                " projection_version) "
                "VALUES ('kite:A', 'stg-1', 'live', 'raw', 'raw-x', 'CNC', :key, 1, 'NSE', 'X', 1, 1)",
                {"key": str(uuid.uuid4())},
            )

    def test_run_deletion_cannot_destroy_attribution_history(self, disposable_db):
        self._bound(disposable_db)
        with pytest.raises(Exception):
            _exec(disposable_db, "DELETE FROM public.algo_worker_runs WHERE strategy_run_id='run-1'")
        assert _scalar(
            disposable_db, "SELECT COUNT(*) FROM public.strategy_run_bindings WHERE strategy_run_id='run-1'"
        ) == 1

    def test_strategy_deletion_with_history_refused_archive_preserves(self, disposable_db):
        self._bound(disposable_db)
        with pytest.raises(Exception):
            _exec(disposable_db, "DELETE FROM public.strategies WHERE id='stg-1'")
        _exec(disposable_db, "UPDATE public.strategies SET status='archived' WHERE id='stg-1'")
        assert _scalar(disposable_db, "SELECT status FROM public.strategies WHERE id='stg-1'") == "archived"
        assert _scalar(
            disposable_db, "SELECT COUNT(*) FROM public.strategy_run_bindings WHERE strategy_id='stg-1'"
        ) == 1


# ---------------------------------------------------------------------------
# 4/6. atomicity and locking
# ---------------------------------------------------------------------------


class TestPublication:
    def _one_position(self, sf):
        self._bound = None  # noqa: F841  (documentation only)
        seed_strategy(sf, sid="stg-1", account="kite:A")
        seed_token(sf)
        seed_run(sf, run_id="run-1", account="kite:A", mode="live")
        seed_binding(sf, run_id="run-1", sid="stg-1", account="kite:A", env="live")
        seed_link(sf, order_id="OID-1", run_id="run-1")
        seed_fill(sf, order_id="OID-1", trade_id="T-1", qty=100)

    def test_recompute_publish_uses_the_locked_session_for_all_reads(self, disposable_db):
        seen = []

        def pipeline(db, bound):
            seen.append(db)
            return [], "sha"

        store = SqlAttributionStore(session_factory=disposable_db)
        seed_strategy(disposable_db, sid="stg-1")
        store.recompute_publish(
            account_id="kite:A", strategy_id="stg-1", execution_environment="live",
            resolve_and_fold=pipeline,
        )
        assert len(seen) == 1  # one locked session carried snapshot AND publication
        assert not hasattr(store, "publish_external_snapshot")
        assert not hasattr(store, "compute_source_version")

    def test_interrupted_publish_retains_previous_projection(self, disposable_db):
        self._one_position(disposable_db)
        first = _publish(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert first["projection_version"] == 1

        # A failing publish must leave rows AND version fully intact.
        store = SqlAttributionStore(session_factory=disposable_db)

        def exploding(db, bound):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            store.recompute_publish(
                account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                resolve_and_fold=exploding,
            )

        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in positions] == [100]
        assert _scalar(
            disposable_db,
            "SELECT projection_version FROM public.strategy_projection_state "
            "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'",
        ) == 1

    def test_atomic_run_plus_binding_rollback(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-1")
        seed_token(disposable_db)
        store = SqlAttributionStore(session_factory=disposable_db)
        token = WorkerToken(
            token_id="tok-1", name="t", account_scope="kite:A",
            allowed_modes=["live"], allowed_actions=[], allowed_templates=[],
        )
        payload = WorkerRunCreateRequest(template_id="tmpl", account_scope="kite:A", execution_mode="live")
        from backend.strategies.attribution import RunBindingInput, RunBindingFailed

        binding = RunBindingInput(
            strategy_id="stg-missing", owner_id="app:o", account_id="kite:A",
            execution_environment="live", bound_by="t", binding_source="hosted_job",
        )
        with pytest.raises(RunBindingFailed):
            store.create_run_with_binding(
                token=token, payload=payload, strategy_run_id="run-x", binding=binding
            )
        assert _scalar(disposable_db, "SELECT COUNT(*) FROM public.algo_worker_runs WHERE strategy_run_id='run-x'") == 0
        assert _scalar(
            disposable_db, "SELECT COUNT(*) FROM public.strategy_run_bindings WHERE strategy_run_id='run-x'"
        ) == 0

        # The happy path writes both.
        store.create_run_with_binding(
            token=token,
            payload=payload,
            strategy_run_id="run-y",
            binding=RunBindingInput(
                strategy_id="stg-1", owner_id="app:o", account_id="kite:A",
                execution_environment="live", bound_by="t", binding_source="hosted_job",
            ),
        )
        assert _scalar(disposable_db, "SELECT COUNT(*) FROM public.algo_worker_runs WHERE strategy_run_id='run-y'") == 1
        assert _scalar(
            disposable_db, "SELECT COUNT(*) FROM public.strategy_run_bindings WHERE strategy_run_id='run-y'"
        ) == 1

    def test_lock_before_snapshot_older_cannot_overwrite_newer(self, disposable_db):
        """The decisive race: an older snapshot can never overwrite a newer rebuild.

        Thread A takes the lock, snapshots, then pauses while still holding it.
        A new fill lands, and thread B starts a rebuild. B must block until A
        commits, and B's own locked snapshot must then include the new fill — so
        the final projection reflects the newer facts, never the stale ones.
        """
        self._one_position(disposable_db)
        store = SqlAttributionStore(session_factory=disposable_db)
        instrument_id = str(uuid.uuid4())

        a_snapshot_taken = threading.Event()
        release_a = threading.Event()
        b_pipeline_entered = threading.Event()
        results = {}
        errors = []

        def _read_total(db) -> int:
            return int(
                db.execute(
                    text(
                        "SELECT COALESCE(SUM(CASE WHEN UPPER(transaction_type)='BUY' THEN quantity "
                        "ELSE -quantity END), 0) FROM public.order_trade_fills WHERE account_id='kite:A'"
                    )
                ).scalar()
                or 0
            )

        def _row(qty):
            return [{
                "identity_kind": "canonical", "identity_key": instrument_id,
                "canonical_instrument_id": instrument_id, "instrument_token": 738561,
                "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
                "net_quantity": qty, "unresolved_reason": None,
            }]

        def pipeline_a(db, bound):
            total = _read_total(db)  # the snapshot is taken HERE, under the lock
            a_snapshot_taken.set()
            assert release_a.wait(timeout=20), "release_a was never set"
            assert total == 100, f"A must snapshot before the late fill, saw {total}"
            return _row(total), "sha-a"

        def pipeline_b(db, bound):
            b_pipeline_entered.set()
            return _row(_read_total(db)), "sha-b"

        def run(name, pipeline):
            try:
                results[name] = store.recompute_publish(
                    account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                    resolve_and_fold=pipeline,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread_a = threading.Thread(target=run, args=("a", pipeline_a))
        thread_a.start()
        assert a_snapshot_taken.wait(timeout=20)

        # A new fill arrives while A holds the lock.
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-2", qty=50)

        thread_b = threading.Thread(target=run, args=("b", pipeline_b))
        thread_b.start()
        # B cannot enter its pipeline while A holds the advisory lock.
        assert not b_pipeline_entered.wait(timeout=2.0), "B entered its snapshot while A held the lock"

        release_a.set()
        thread_a.join(timeout=30)
        thread_b.join(timeout=30)
        assert not errors, errors
        # B ran strictly after A, so its snapshot (and publication) is newer.
        assert results["a"]["projection_version"] == 1
        assert results["b"]["projection_version"] == 2
        assert results["b"]["content_sha256"] != results["a"]["content_sha256"]
        final = _scalar(
            disposable_db,
            "SELECT net_quantity FROM public.strategy_position_projection "
            "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'",
        )
        assert final == 150, "the newer facts must win, never the stale snapshot"

    def test_two_concurrent_rebuilds_same_strategy_same_environment_serialize(self, disposable_db):
        self._one_position(disposable_db)
        store = SqlAttributionStore(session_factory=disposable_db)
        instrument_ids = {"a": str(uuid.uuid4()), "b": str(uuid.uuid4())}
        bar = threading.Barrier(2)
        errors = []

        def publish(tag, qty):
            try:
                bar.wait(timeout=20)
                store.recompute_publish(
                    account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                    resolve_and_fold=lambda db, bound: (
                        [
                            {
                                "identity_kind": "canonical", "identity_key": instrument_ids[tag],
                                "canonical_instrument_id": instrument_ids[tag], "instrument_token": 738561,
                                "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
                                "net_quantity": qty, "unresolved_reason": None,
                            }
                        ],
                        f"sha-{tag}",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=publish, args=("a", 10)),
            threading.Thread(target=publish, args=("b", 20)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        assert not errors, errors

        rows = _rows(
            disposable_db,
            "SELECT identity_key, net_quantity FROM public.strategy_position_projection "
            "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'",
        )
        assert len(rows) == 1, f"interleaved rows: {rows}"
        assert str(rows[0][0]) in set(instrument_ids.values())
        version = _scalar(
            disposable_db,
            "SELECT projection_version FROM public.strategy_projection_state "
            "WHERE account_id='kite:A' AND strategy_id='stg-1' AND execution_environment='live'",
        )
        assert version == 2

    def test_concurrent_rebuild_and_late_fill(self, disposable_db):
        self._one_position(disposable_db)
        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")

        seed_fill(disposable_db, order_id="OID-1", trade_id="T-LATE", qty=25)
        store = SqlAttributionStore(session_factory=disposable_db)

        def late_publish():
            store.recompute_publish(
                account_id="kite:A", strategy_id="stg-1", execution_environment="live",
                resolve_and_fold=lambda db, bound: ([], "late"),
            )

        thread = threading.Thread(target=late_publish)
        thread.start()
        thread.join(timeout=30)

        final = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert final["folded_facts"] == 2
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in positions] == [125]


# ---------------------------------------------------------------------------
# 5. instrument identity
# ---------------------------------------------------------------------------


class TestInstrumentIdentity:
    def _live_book(self, sf, *, account="kite:A", sid="stg-1", run_id="run-1"):
        seed_strategy(sf, sid=sid, account=account)
        seed_token(sf, account_scope=account)
        seed_run(sf, run_id=run_id, account=account, mode="live")
        seed_binding(sf, run_id=run_id, sid=sid, account=account, env="live")
        seed_link(sf, account=account, order_id="OID-1", run_id=run_id)

    def test_per_fact_identity_across_mapping_eras(self, disposable_db):
        """An old fill must not be reinterpreted through today's mapping.

        The same broker token maps to instrument A in one generation interval
        and instrument B in a later one. An old BUY in interval 1 and a later
        SELL in interval 2 resolve per fact, producing two positions that do NOT
        net to zero — this test fails under grouped-resolution or is_current-only
        designs.
        """
        gen1 = seed_generation(disposable_db, published_at="2024-01-01T00:00:00+00:00")
        gen2 = seed_generation(disposable_db, published_at="2026-01-01T00:00:00+00:00")
        instrument_a = seed_instrument(disposable_db, symbol="RELIANCE")
        instrument_b = seed_instrument(disposable_db, symbol="RELIANCE")
        seed_mapping(disposable_db, instrument_id=instrument_a, from_gen=gen1, to_gen=gen2, is_current=False)
        seed_mapping(disposable_db, instrument_id=instrument_b, from_gen=gen2, to_gen=None, is_current=True)

        self._live_book(disposable_db)
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-OLD", qty=10, at="2024-06-01T10:00:00+00:00")
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-NEW", side="SELL", qty=10, at="2026-06-01T10:00:00+00:00")

        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 2
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert len(positions) == 2, "old and new eras must not merge into one position"
        by_instrument = {p["identity_key"]: p["net_quantity"] for p in positions}
        assert by_instrument == {instrument_a: 10, instrument_b: -10}
        assert all(p["identity_kind"] == "canonical" for p in positions)

    def test_unresolved_same_era_nets_across_dates_and_eras_stay_separate(self, disposable_db):
        """Same catalog era nets across dates; distinct eras never merge.

        ``instrument_broker_mappings`` is UNIQUE on
        ``(broker, broker_token, valid_from_generation)``, so two distinct
        instruments for one token inside a single window are not representable —
        the reachable unresolved eras here are the catalog generations that
        cover the fact (``era=gen:<id>``).
        """
        active_gen = seed_generation(disposable_db, published_at="2024-01-01T00:00:00+00:00")
        self._live_book(disposable_db)
        # Bounded catalog gap: a generation exists but no mapping for the token,
        # so both fills share era=gen:<active generation>.
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-MON", token=999001, qty=10,
                  at="2024-03-04T10:00:00+00:00")
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-TUE", token=999001, side="SELL", qty=10,
                  at="2024-03-05T10:00:00+00:00")

        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 2
        assert report["unchanged"] is False
        # Same era -> nets to flat, so no row survives: the era is catalog
        # evidence, not the fill date, which is why a Monday BUY and a Tuesday
        # SELL in one era cancel.
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert positions == []
        assert report["unresolved"] == []

        # A later catalog generation establishes a NEW era: facts on opposite
        # sides of it must never merge, even though the raw tuple is identical.
        new_gen = seed_generation(disposable_db, published_at="2026-01-01T00:00:00+00:00")
        seed_link(disposable_db, order_id="OID-2", run_id="run-1")
        seed_fill(disposable_db, order_id="OID-2", trade_id="T-E1", token=999001, qty=10,
                  at="2024-06-01T10:00:00+00:00")
        seed_fill(disposable_db, order_id="OID-2", trade_id="T-E2", token=999001, side="SELL", qty=10,
                  at="2026-06-01T10:00:00+00:00")

        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 4
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        by_era = {p["identity_key"]: p["net_quantity"] for p in positions}
        assert len(positions) == 2, f"distinct known eras must never merge: {by_era}"
        assert sorted(by_era.values()) == [-10, 10]
        assert any(f"era=gen:{active_gen}" in key for key in by_era)
        assert any(f"era=gen:{new_gen}" in key for key in by_era)
        assert all(p["identity_kind"] == "raw" for p in positions)
        assert all(p["unresolved_reason"] == "mapping_missing" for p in positions)

    def test_evidence_mismatch_is_unresolved_not_accepted(self, disposable_db):
        gen = seed_generation(disposable_db, published_at="2024-01-01T00:00:00+00:00")
        instrument = seed_instrument(disposable_db, symbol="RELIANCE")
        seed_mapping(
            disposable_db, instrument_id=instrument, token=999003,
            exchange="NSE", symbol="RELIANCE", from_gen=gen,
        )
        self._live_book(disposable_db)
        # The fill claims a different symbol than the mapping for its token.
        seed_fill(
            disposable_db, order_id="OID-1", trade_id="T-MM", token=999003,
            exchange="NSE", symbol="TCS", qty=10, at="2024-06-01T10:00:00+00:00",
        )
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert len(positions) == 1
        assert positions[0]["identity_kind"] == "raw"
        assert positions[0]["unresolved_reason"] == "evidence_mismatch"
        assert positions[0]["identity_key"] != str(instrument)
        # Surfaced as a named condition, never silently accepted.
        assert [entry["condition"] for entry in report["unresolved"]] == ["UNRESOLVED_INSTRUMENT_IDENTITY"]

    def test_unresolved_identity_stays_explicit_raw(self, disposable_db):
        # No catalog generation at all -> pre-catalog identity.
        self._live_book(disposable_db)
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-NOCAT", token=999004, qty=7,
                  at="2020-01-01T10:00:00+00:00")
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert len(positions) == 1
        assert positions[0]["identity_kind"] == "raw"
        assert positions[0]["unresolved_reason"] == "mapping_missing"
        assert "era=pre-catalog" in positions[0]["identity_key"]
        assert report["unresolved"][0]["condition"] == "UNRESOLVED_INSTRUMENT_IDENTITY"


# ---------------------------------------------------------------------------
# 7-19. folds, books, ownership
# ---------------------------------------------------------------------------


class TestFoldsAndBooks:
    def _book(self, sf, *, sid="stg-1", account="kite:A", mode="live", run_id="run-1", token_id="tok-1"):
        seed_strategy(sf, sid=sid, account=account)
        seed_token(sf, token_id=token_id, account_scope=account)
        seed_run(sf, run_id=run_id, token_id=token_id, account=account, mode=mode)
        seed_binding(sf, run_id=run_id, sid=sid, account=account, env=mode)

    def _canonical(self, sf, *, instrument_id):
        """One unambiguous mapping for the seeded fill token."""
        gen = seed_generation(sf, published_at="2024-01-01T00:00:00+00:00")
        seed_mapping(sf, instrument_id=instrument_id, token=738561, from_gen=gen)

    def test_books_are_separate_and_dry_run_is_empty(self, disposable_db):
        """Paper and live are separate books; a dry-run book is normally empty.

        One strategy is pinned to one account by the composite FK, so both runs
        share the account and differ only in ``execution_mode`` — which is
        exactly the dimension the books must separate on.
        """
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        seed_strategy(disposable_db, sid="stg-1", account="kite:A")
        seed_token(disposable_db, token_id="tok-l", account_scope="kite:A")
        seed_token(disposable_db, token_id="tok-p", account_scope="kite:A")
        seed_run(disposable_db, run_id="run-l", token_id="tok-l", account="kite:A", mode="live")
        seed_run(disposable_db, run_id="run-p", token_id="tok-p", account="kite:A", mode="paper")
        seed_binding(disposable_db, run_id="run-l", sid="stg-1", account="kite:A", env="live")
        seed_binding(disposable_db, run_id="run-p", sid="stg-1", account="kite:A", env="paper")

        seed_link(disposable_db, account="kite:A", order_id="OID-L", run_id="run-l")
        seed_fill(disposable_db, account="kite:A", order_id="OID-L", trade_id="T-L", qty=20)
        seed_paper_account(disposable_db, scope="kite:A")
        seed_paper_order(disposable_db, scope="kite:A", order_id="PO-1", run_id="run-p")
        seed_paper_trade(disposable_db, scope="kite:A", trade_id="PT-1", order_id="PO-1", qty=100)

        live = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        paper = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="paper")
        assert live["folded_facts"] == 1
        assert paper["folded_facts"] == 1

        live_positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        paper_positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="paper"
        )
        assert [(p["execution_environment"], p["net_quantity"]) for p in live_positions] == [("live", 20)]
        assert [(p["execution_environment"], p["net_quantity"]) for p in paper_positions] == [("paper", 100)]

        # Republishing one book must never change the other.
        before_live = _rows(
            disposable_db,
            "SELECT identity_key, net_quantity FROM public.strategy_position_projection "
            "WHERE execution_environment='live' ORDER BY identity_key",
        )
        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="paper")
        after_live = _rows(
            disposable_db,
            "SELECT identity_key, net_quantity FROM public.strategy_position_projection "
            "WHERE execution_environment='live' ORDER BY identity_key",
        )
        assert before_live == after_live

        # A dry-run run binds to the dry_run environment and rebuilds EMPTY: the
        # current dry-run path produces previews and no durable fill facts, and
        # no simulated fills are manufactured.
        seed_strategy(disposable_db, sid="stg-d", account="kite:D")
        seed_token(disposable_db, token_id="tok-d", account_scope="kite:D")
        seed_run(disposable_db, run_id="run-d", token_id="tok-d", account="kite:D", mode="dry_run")
        seed_binding(disposable_db, run_id="run-d", sid="stg-d", account="kite:D", env="dry_run")
        dry = _publish(disposable_db, account_id="kite:D", strategy_id="stg-d", execution_environment="dry_run")
        assert dry["folded_facts"] == 0
        assert _positions(
            disposable_db, account_id="kite:D", strategy_id="stg-d", execution_environment="dry_run"
        ) == []
        # The dry-run book never absorbed the paper or live fills.
        assert _scalar(
            disposable_db,
            "SELECT COUNT(*) FROM public.strategy_position_projection WHERE execution_environment='dry_run'",
        ) == 0

    def test_paper_to_live_promotion_imports_nothing(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        seed_strategy(disposable_db, sid="stg-1", account="kite:A")
        seed_token(disposable_db, token_id="tok-p", account_scope="kite:A")
        seed_token(disposable_db, token_id="tok-l", account_scope="kite:A")
        seed_run(disposable_db, run_id="run-p", token_id="tok-p", account="kite:A", mode="paper")
        seed_run(disposable_db, run_id="run-l", token_id="tok-l", account="kite:A", mode="live")
        seed_binding(disposable_db, run_id="run-p", sid="stg-1", account="kite:A", env="paper")
        seed_binding(disposable_db, run_id="run-l", sid="stg-1", account="kite:A", env="live")

        seed_paper_account(disposable_db, scope="kite:A")
        seed_paper_order(disposable_db, scope="kite:A", order_id="PO-1", run_id="run-p")
        seed_paper_trade(disposable_db, scope="kite:A", trade_id="PT-1", order_id="PO-1", qty=500)
        seed_link(disposable_db, account="kite:A", order_id="OID-L", run_id="run-l")
        seed_fill(disposable_db, account="kite:A", order_id="OID-L", trade_id="T-L", qty=20)

        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        paper = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="paper")
        assert paper["folded_facts"] == 1

        # The live book — what settlement and admission read — never sees the
        # paper exposure: promotion starts an empty live book.
        live_positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in live_positions] == [20]
        assert all(p["execution_environment"] == "live" for p in live_positions)

    def test_late_fill_with_order_link_only_no_trade_link(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        self._book(disposable_db)
        # Order-level link only: no trade rows at all (the failed trade-link
        # upsert scenario). The fill must still fold.
        seed_link(disposable_db, order_id="OID-1", run_id="run-1", trade_id=None)
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-1", qty=42)
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 1
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in positions] == [42]

    def test_dedupe_by_broker_trade_identity(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        self._book(disposable_db)
        seed_link(disposable_db, order_id="OID-1", run_id="run-1")
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-1", qty=100)
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 1

        # Re-ingesting the same trade is impossible (durable PK) and a rebuild
        # must not double-count.
        with pytest.raises(Exception):
            seed_fill(disposable_db, order_id="OID-1", trade_id="T-1", qty=100)
        second = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert second["folded_facts"] == 1
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in positions] == [100]

    def test_placed_intent_fallback_and_link_intent_conflict(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        self._book(disposable_db)

        # Intent-owned order with NO link: the intent is the fallback owner.
        seed_intent(disposable_db, order_id="OID-I", run_id="run-1")
        seed_fill(disposable_db, order_id="OID-I", trade_id="T-I", qty=30)
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 1

        # Link and intent disagree -> corruption surfaced, order excluded.
        seed_run(disposable_db, run_id="run-2", account="kite:A", mode="live")
        seed_binding(disposable_db, run_id="run-2", sid="stg-1", account="kite:A", env="live")
        seed_link(disposable_db, order_id="OID-D", run_id="run-1")
        seed_intent(disposable_db, order_id="OID-D", run_id="run-2")
        seed_fill(disposable_db, order_id="OID-D", trade_id="T-D", qty=99)
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert [a["kind"] for a in report["anomalies"]] == ["link_intent_disagreement"]
        assert report["folded_facts"] == 1  # the conflicting order contributes nothing

    def test_legacy_run_bound_after_facts_exist(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        seed_strategy(disposable_db, sid="stg-1", account="kite:A")
        seed_token(disposable_db)
        seed_run(disposable_db, run_id="run-legacy", account="kite:A", mode="live")
        seed_link(disposable_db, order_id="OID-1", run_id="run-legacy")
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-1", qty=500)

        # Unbound: contributes nothing (no watermark can hide it later).
        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        ) == []

        seed_binding(
            disposable_db, run_id="run-legacy", sid="stg-1", account="kite:A", env="live",
            source="audited_mapping",
        )
        report = _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        assert report["folded_facts"] == 1
        positions = _positions(
            disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live"
        )
        assert [p["net_quantity"] for p in positions] == [500]

    def test_account_positions_never_feed_projection(self, disposable_db):
        instrument = seed_instrument(disposable_db)
        self._canonical(disposable_db, instrument_id=instrument)
        self._book(disposable_db)
        seed_link(disposable_db, order_id="OID-1", run_id="run-1")
        seed_fill(disposable_db, order_id="OID-1", trade_id="T-1", qty=10)
        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")

        before = _rows(
            disposable_db,
            "SELECT identity_key, net_quantity FROM public.strategy_position_projection ORDER BY identity_key",
        )
        # A broker-reported aggregate must never become strategy ownership.
        try:
            _exec(
                disposable_db,
                "INSERT INTO public.account_positions (account_id, instrument_token, product, net_quantity) "
                "VALUES ('kite:A', 738561, 'CNC', 9999)",
            )
        except Exception:
            pass  # the table may not exist in this build; the fold must not read it either way
        _publish(disposable_db, account_id="kite:A", strategy_id="stg-1", execution_environment="live")
        after = _rows(
            disposable_db,
            "SELECT identity_key, net_quantity FROM public.strategy_position_projection ORDER BY identity_key",
        )
        assert before == after
        assert [row[1] for row in after] == [10]

    def test_paper_fold_attribution_and_dedupe(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-p", account="kite:paper-a")
        seed_token(disposable_db, token_id="tok-p", account_scope="kite:paper-a")
        seed_run(disposable_db, run_id="run-p", token_id="tok-p", account="kite:paper-a", mode="paper")
        seed_binding(disposable_db, run_id="run-p", sid="stg-p", account="kite:paper-a", env="paper")
        seed_paper_account(disposable_db, scope="kite:paper-a")
        seed_paper_order(disposable_db, scope="kite:paper-a", order_id="PO-1", run_id="run-p")
        seed_paper_trade(disposable_db, scope="kite:paper-a", trade_id="PT-1", order_id="PO-1", qty=100)

        report = _publish(
            disposable_db, account_id="kite:paper-a", strategy_id="stg-p", execution_environment="paper"
        )
        assert report["folded_facts"] == 1
        positions = _positions(
            disposable_db, account_id="kite:paper-a", strategy_id="stg-p", execution_environment="paper"
        )
        assert [(p["execution_environment"], p["net_quantity"]) for p in positions] == [("paper", 100)]

        # Duplicate trade id is impossible (PK) and cannot double-count.
        with pytest.raises(Exception):
            seed_paper_trade(disposable_db, scope="kite:paper-a", trade_id="PT-1", order_id="PO-1", qty=100)
        again = _publish(
            disposable_db, account_id="kite:paper-a", strategy_id="stg-p", execution_environment="paper"
        )
        assert again["folded_facts"] == 1
        assert again["unchanged"] is True


# ---------------------------------------------------------------------------
# 20-22. binding provenance, grants, legacy hosted compatibility
# ---------------------------------------------------------------------------


class TestBindingProvenanceAndOwnerApi:
    def test_hosted_identity_from_job_not_metadata(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-REAL", account="kite:A")
        seed_strategy(disposable_db, sid="stg-VICTIM", account="kite:A")
        seed_token(disposable_db)
        from backend.strategies.attribution import RunBindingInput

        store = SqlAttributionStore(session_factory=disposable_db)
        token = WorkerToken(
            token_id="tok-1", name="t", account_scope="kite:A",
            allowed_modes=["live"], allowed_actions=[], allowed_templates=["hosted:stg-REAL"],
        )
        payload = WorkerRunCreateRequest(
            template_id="hosted:stg-REAL", account_scope="kite:A", execution_mode="live",
            metadata={"hosted_strategy_id": "stg-VICTIM", "hosted_job_id": "job-1"},
        )
        store.create_run_with_binding(
            token=token, payload=payload, strategy_run_id="run-hosted",
            binding=RunBindingInput(
                strategy_id="stg-REAL", owner_id="app:o", account_id="kite:A",
                execution_environment="live", bound_by="supervisor", binding_source="hosted_job",
            ),
        )
        row = _rows(
            disposable_db,
            "SELECT strategy_id, owner_id, account_id, execution_environment, binding_source "
            "FROM public.strategy_run_bindings WHERE strategy_run_id='run-hosted'",
        )
        assert row == [("stg-REAL", "app:o", "kite:A", "live", "hosted_job")]

    def test_grants_enforcement(self, disposable_db):
        seed_strategy(disposable_db, sid="stg-1", owner="app:o", account="kite:A")
        seed_strategy(disposable_db, sid="stg-2", owner="app:o", account="kite:OTHER")
        seed_token(disposable_db, token_id="tok-1", account_scope="kite:A")
        store = SqlAttributionStore(session_factory=disposable_db)

        store.grant_strategy(token_id="tok-1", strategy_id="stg-1", granted_by="app:o")
        # Account match is a join condition, not a caller convention.
        store.grant_strategy(token_id="tok-1", strategy_id="stg-2", granted_by="app:o")
        active = store.active_grants(token_id="tok-1", account_id="kite:A")
        assert [g["strategy_id"] for g in active] == ["stg-1"]
        assert active[0]["owner_id"] == "app:o"

        # Cross-owner scoping resolves nothing.
        assert store.canonical_strategy(strategy_id="stg-1", owner_id="app:other") is None

        # Revocation removes authority but keeps history.
        assert store.revoke_grant(token_id="tok-1", strategy_id="stg-1") is True
        assert store.active_grants(token_id="tok-1", account_id="kite:A") == []
        assert _scalar(
            disposable_db,
            "SELECT COUNT(*) FROM public.worker_token_strategy_grants WHERE token_id='tok-1' AND strategy_id='stg-1'",
        ) == 1
        assert _scalar(
            disposable_db,
            "SELECT revoked_at IS NOT NULL FROM public.worker_token_strategy_grants "
            "WHERE token_id='tok-1' AND strategy_id='stg-1'",
        ) is True

        # Token rotation leaves bindings untouched and identity stable.
        seed_token(disposable_db, token_id="tok-2", account_scope="kite:A")
        seed_run(disposable_db, run_id="run-1", token_id="tok-2", account="kite:A", mode="live")
        seed_binding(disposable_db, run_id="run-1", sid="stg-1", account="kite:A", env="live")
        store.grant_strategy(token_id="tok-2", strategy_id="stg-1", granted_by="app:o")
        assert [g["strategy_id"] for g in store.active_grants(token_id="tok-2", account_id="kite:A")] == ["stg-1"]
        assert _scalar(
            disposable_db, "SELECT strategy_id FROM public.strategy_run_bindings WHERE strategy_run_id='run-1'"
        ) == "stg-1"

    def test_legacy_hosted_api_compatibility_and_drift(self, disposable_db):
        repo = SqlAlchemyStrategyRepository(disposable_db)
        row = repo.create_strategy(
            owner_id="app:admin", name="momentum", description="d", execution_mode="paper",
            job_kind="finite", account_scope="kite:paper", max_duration_s=3600,
            progress_deadline_s=600, stale_exit_policy="exit_on_worker_stale",
        )
        # Canonical identity and the hosted adapter exist together, same id.
        canonical = _rows(
            disposable_db,
            "SELECT id, owner_id, name, account_scope FROM public.strategies WHERE id=:sid",
            {"sid": row.id},
        )
        hosted = _rows(
            disposable_db,
            "SELECT id, owner_id, name, default_account_scope FROM public.hosted_strategies WHERE id=:sid",
            {"sid": row.id},
        )
        assert canonical == hosted
        assert canonical == [(row.id, "app:admin", "momentum", "kite:paper")]

        # A rename writes both sides in one transaction.
        repo.update_strategy("app:admin", row.id, name="renamed")
        assert _scalar(disposable_db, "SELECT name FROM public.strategies WHERE id=:sid", {"sid": row.id}) == "renamed"
        assert _scalar(
            disposable_db, "SELECT name FROM public.hosted_strategies WHERE id=:sid", {"sid": row.id}
        ) == "renamed"

        # Direct mirror drift is refused by the database.
        with pytest.raises(Exception):
            _exec(
                disposable_db,
                "UPDATE public.hosted_strategies SET owner_id='app:other' WHERE id=:sid",
                {"sid": row.id},
            )

        # Product status is independent of hosted scheduling status.
        repo.set_product_status("app:admin", row.id, "archived")
        assert _scalar(disposable_db, "SELECT status FROM public.strategies WHERE id=:sid", {"sid": row.id}) == "archived"
        assert _scalar(
            disposable_db, "SELECT status FROM public.hosted_strategies WHERE id=:sid", {"sid": row.id}
        ) == "active"
