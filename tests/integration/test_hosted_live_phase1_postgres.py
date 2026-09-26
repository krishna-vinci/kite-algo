"""Phase 1 hosted LIVE execution: persisted authority, fake broker, real ingestion.

Runs against a DISPOSABLE PostgreSQL database on port 15433. The broker boundary
is the ONLY fake: the intent handler. Every evidence reader (authority, position,
fills) is the production implementation, and the claim/barrier/reservation/ledger
are the real platform classes.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# The suite drives its own clock in a disposable database with no imported NSE
# calendar, so the market session is supplied as EVIDENCE through the production
# seam rather than guessed (see tests/support/market_session_stub.py).
from tests.support.market_session_stub import open_market_session  # noqa: F401

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

OWNER = "app:owner"
G1 = "11111111-1111-1111-1111-111111111111"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live1_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return name, f"{PG_ADMIN.rpartition('/')[0]}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="module")
def pg():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    os.environ["DATABASE_URL"] = dsn
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", "backend/alembic")
    command.upgrade(cfg, "head")
    engine = create_engine(dsn, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:id, 'published', NOW())"
            ),
            {"id": G1},
        )
        session.commit()
    try:
        yield {"dsn": dsn, "factory": factory, "engine": engine}
    finally:
        engine.dispose()
        _drop_db(name)


class _FakeBroker:
    """The ONLY fake: the broker boundary. Records calls; never fills."""

    def __init__(self, order_ids=("OID-1", "OID-2")):
        self.calls = []
        self._order_ids = list(order_ids)

    async def handle(self, intent, *, context=None):
        self.calls.append((intent, dict(context or {})))
        index = min(len(self.calls) - 1, len(self._order_ids) - 1)
        return {"result": {"order_id": self._order_ids[index]}}


class _LiveFixture:
    """One hosted strategy in LIVE mode + a frozen single-instrument plan."""

    def __init__(self, factory, *, target: int = 10, with_reservation: bool = True, with_catalog: bool = False):
        from sqlalchemy import text

        from backend.strategies.repository import SqlAlchemyStrategyRepository

        self.factory = factory
        # The fixture's clock is the REAL clock at construction: the persisted
        # authority (job lease, token expiry) is compared against the production
        # reader's own ``now``, so a frozen fixture timestamp would silently
        # expire as the wall clock advanced past it.
        self.now = datetime.now(timezone.utc)
        self.broker_user_id = f"live{uuid.uuid4().hex[:6]}"
        self.account_id = f"kite:{self.broker_user_id}"
        self.run_id = f"run-live1-{uuid.uuid4().hex[:8]}"
        self.plan_id = str(uuid.uuid4())
        self.proposal_id = str(uuid.uuid4())
        self.token_id = f"tok-live1-{uuid.uuid4().hex[:6]}"
        self.job_id = f"job-live1-{uuid.uuid4().hex[:6]}"
        self.instrument_id = str(uuid.uuid4())
        if with_catalog:
            self._seed_catalog()
        self.leg = {
            "instrument_id": self.instrument_id,
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "broker_exchange": "NSE",
            "broker_symbol": "RELIANCE",
            "broker_token": 738561,
            "product": "CNC",
            "instrument_type": "EQ",
            "lot_size": 1,
            "signed_quantity": int(target),
            "reference_price": 1500.0,
        }
        repo = SqlAlchemyStrategyRepository(factory)
        # The registry now REPRESENTS live mode (migration 000039). Execution is
        # still gated by HOSTED_LIVE_ENABLED.
        self.strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"live1-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="live",
            job_kind="finite",
            account_scope=self.account_id,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        self.strategy_id = str(self.strategy.id)

        self.version_id = f"ver-{uuid.uuid4().hex[:8]}"
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.hosted_strategy_versions (id, strategy_id, version, source, "
                    " source_sha256, created_by) VALUES (:vid, :sid, 1, 'print(1)', :sha, :owner)"
                ),
                {"vid": self.version_id, "sid": self.strategy_id, "sha": uuid.uuid4().hex, "owner": OWNER},
            )
            session.execute(
                text(
                    "INSERT INTO public.kite_sessions (session_id, access_token, broker_user_id, created_at) "
                    "VALUES (:sid, 'test-access-token', :uid, NOW())"
                ),
                {"sid": f"sess-{uuid.uuid4().hex[:10]}", "uid": self.broker_user_id},
            )
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope, "
                    " allowed_modes, allowed_actions, allowed_templates, status, expires_at) "
                    "VALUES (:tid, 'hosted-live', :hash, :account, '[\"live\"]'::jsonb, "
                    " '[\"proposals:submit\", \"intents:submit\"]'::jsonb, "
                    " '[]'::jsonb, 'active', :expires)"
                ),
                {
                    "tid": self.token_id,
                    "hash": uuid.uuid4().hex,
                    "account": self.account_id,
                    "expires": self.now + timedelta(hours=2),
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, template_id, "
                    " account_scope, execution_mode, status) "
                    "VALUES (:run, :tid, 'tpl-live', :account, 'live', 'open')"
                ),
                {"run": self.run_id, "tid": self.token_id, "account": self.account_id},
            )
            session.execute(
                text(
                    "INSERT INTO public.strategy_jobs (id, strategy_id, version_id, owner_id, account_scope, "
                    " job_kind, execution_mode, desired_state, run_id, token_id, lease_owner, lease_epoch, "
                    " lease_until, attempt, status, params_snapshot, capabilities_snapshot, policy_snapshot, "
                    " max_duration_s, progress_deadline_s, identity_json) "
                    "VALUES (:jid, :sid, :vid, :owner, :account, 'finite', 'live', 'started', :run, :tid, "
                    " 'sup-1', 1, :lease, 1, 'running', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 21600, 600, '{}'::jsonb)"
                ),
                {
                    "jid": self.job_id,
                    "sid": self.strategy_id,
                    "vid": self.version_id,
                    "owner": OWNER,
                    "account": self.account_id,
                    "run": self.run_id,
                    "tid": self.token_id,
                    "lease": self.now + timedelta(minutes=10),
                },
            )
            session.commit()

        from backend.strategies.attribution import SqlAttributionStore

        SqlAttributionStore(session_factory=factory).bind_run(
            strategy_run_id=self.run_id,
            strategy_id=self.strategy_id,
            owner_id=OWNER,
            account_id=self.account_id,
            execution_environment="live",
            bound_by="test",
            binding_source="hosted_job",
        )
        self._insert_plan(target=target)

        # The published book must exist BEFORE the owner approves: the approval
        # pins the exposure snapshot, so publishing afterwards invalidates it.
        self.publish_projection(net_quantity=0)

        from backend.strategies.admission import AdmissionService

        AdmissionService(session_factory=factory).upsert_policy(
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            updated_by=OWNER,
            allocation_inr=1_000_000.0,
        )
        self.reservation = None
        if with_reservation:
            from backend.strategies.reservations import ClaimRequest, ReservationLedger

            self.reservation = ReservationLedger(session_factory=factory).claim(
                ClaimRequest(
                    plan_id=self.plan_id,
                    strategy_id=self.strategy_id,
                    account_id=self.account_id,
                    evaluation_id=f"eval-{self.plan_id}",
                    execution_environment="live",
                    requirement_inr=15000.0,
                    valid_until=self.now + timedelta(hours=1),
                    allocation_inr=1_000_000.0,
                    actor_id=OWNER,
                ),
                now=self.now,
            )
            from backend.strategies.approvals import ApprovalRequest, ApprovalService

            self.approval = ApprovalService(session_factory=factory).approve(
                ApprovalRequest(
                    plan=self.plan(),
                    actor_id=OWNER,
                    reservation_id=str(self.reservation["reservation_id"]),
                    execution_environment="live",
                    session_product_snapshot={"products": ["CNC"]},
                    margin_evidence={"usable": 500000.0, "as_of": self.now.isoformat()},
                ),
                now=self.now,
            )

    def _seed_catalog(self) -> None:
        """Map the broker token to this strategy's canonical instrument.

        Without this, the fill ingestion cannot resolve the instrument identity
        and the attributed book carries an unresolved ``raw`` fact, which
        admission correctly refuses. The production catalog always carries the
        mapping; the neighboring live PG suites seed it for the same reason.
        """
        from sqlalchemy import text

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                    " lifecycle_status, instrument_type, lot_size, tick_size, current_generation_id) "
                    "VALUES (:iid, :key, :key, 'NSE', 'RELIANCE', 'active', 'EQ', 1, 0.05, :gen)"
                ),
                {"iid": self.instrument_id, "key": "NSE:RELIANCE", "gen": G1},
            )
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', 'NSE', 'RELIANCE', 738561, :gen, TRUE)"
                ),
                {"mid": str(uuid.uuid4()), "iid": self.instrument_id, "gen": G1},
            )
            session.commit()

    def _insert_plan(self, *, target: int) -> None:
        from sqlalchemy import text

        resolved = {"target_kind": "single_instrument", "catalog_generation": G1, "legs": [self.leg]}
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, evaluation_id, "
                    " evaluation_kind, strategy_run_id, target_kind, payload, payload_sha256, status) "
                    "VALUES (:pid, :sid, :account, :eval, 'run_now', :run, 'single_instrument', '{}', 'sha', 'validated')"
                ),
                {
                    "pid": self.proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "eval": f"eval-{self.plan_id}",
                    "run": self.run_id,
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, plan_kind, "
                    " plan_hash, logical_plan, resolved_plan, pinned_catalog_generation, pinned_universe_revision_id, "
                    " pinned_member_hash) VALUES (:pid, :prop, :sid, :account, 'single_instrument', 'hash', '{}', "
                    " :resolved, :gen, NULL, NULL)"
                ),
                {
                    "pid": self.plan_id,
                    "prop": self.proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "resolved": json.dumps(resolved),
                    "gen": G1,
                },
            )
            session.commit()

    def plan(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_id,
            "account_id": self.account_id,
            "plan_kind": "single_instrument",
            "plan_hash": "hash",
            "logical_plan": {},
            "resolved_plan": {"target_kind": "single_instrument", "legs": [self.leg]},
            "pinned_catalog_generation": G1,
        }

    def publish_projection(self, *, net_quantity: int) -> None:
        """Seed a PUBLISHED live book with one position (the readers' source)."""
        from sqlalchemy import text

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_projection_state (account_id, strategy_id, execution_environment, "
                    " projection_version, content_sha256, last_rebuild_at) "
                    "VALUES (:account, :sid, 'live', 1, 'sha', NOW()) "
                    "ON CONFLICT (account_id, strategy_id, execution_environment) DO NOTHING"
                ),
                {"account": self.account_id, "sid": self.strategy_id},
            )
            if net_quantity:
                session.execute(
                    text(
                        "INSERT INTO strategy_position_projection (account_id, strategy_id, execution_environment, "
                        " identity_kind, identity_key, canonical_instrument_id, instrument_token, exchange, "
                        " tradingsymbol, product, net_quantity, projection_version, updated_at) "
                        "VALUES (:account, :sid, 'live', 'canonical', :iid, :iid, 738561, 'NSE', 'RELIANCE', "
                        " 'CNC', :qty, 1, NOW()) "
                        "ON CONFLICT (account_id, strategy_id, execution_environment, identity_kind, "
                        " identity_key, product) DO NOTHING"
                    ),
                    {"account": self.account_id, "sid": self.strategy_id, "iid": self.instrument_id, "qty": int(net_quantity)},
                )
            session.commit()

    def plan_view_from_row(self) -> dict:
        from sqlalchemy import text

        with self.factory() as session:
            row = session.execute(
                text("SELECT resolved_plan FROM strategy_plans WHERE plan_id = :pid"),
                {"pid": self.plan_id},
            ).scalar_one()
        if isinstance(row, str):
            row = json.loads(row)
        view = self.plan()
        view["resolved_plan"] = row
        return view


def _executor(pg, fixture, *, live_enabled: bool, broker: _FakeBroker, adapter=None, admission=None):
    from backend.strategies.live_service import LivePlanExecutor

    # This deployment's own live configuration: the master switch AND the C2
    # per-lane allowlist with every lane a suite in this release may use.
    environ = (
        {
            "HOSTED_LIVE_ENABLED": "true",
            "HOSTED_LIVE_LANES": "cnc,mis,futures,options",
        }
        if live_enabled
        else {}
    )
    if adapter is None and live_enabled:
        from backend.strategies.live_readers import (
            attributed_position_reader,
            ingested_fill_reader,
        )
        from backend.strategies.live_adapter import LivePlanAdapter
        from backend.strategies.live_authority import live_authority_reader
        from backend.strategies.settlement import ExecutionBarrier

        adapter = LivePlanAdapter(
            session_factory=pg["factory"],
            intent_handler=broker,
            admission=admission,
            barrier=ExecutionBarrier(session_factory=pg["factory"]),
            fill_reader=ingested_fill_reader(pg["factory"]),
            position_reader=attributed_position_reader(pg["factory"]),
            authority_reader=live_authority_reader(pg["factory"]),
            clock=lambda: fixture.now,
        )
    return LivePlanExecutor(
        session_factory=pg["factory"],
        adapter=adapter,
        clock=lambda: fixture.now,
        intent_handler=broker,
        environ=environ,
        # The market-data/margin boundary is the second fake: a deterministic
        # tick and a stated margin amount. Authority, position, fills, claims and
        # settlement remain real.
        quote_reader=lambda leg: {
            "instrument_id": str(leg.get("instrument_id") or ""),
            "ltp": 1500.0,
            "as_of": fixture.now.isoformat(),
        },
        margin_reader=lambda account, plan: {"usable": 500000.0, "as_of": fixture.now.isoformat()},
    )


def _record_order_outcome(factory, *, account_id: str, order_id: str, filled: int) -> None:
    """The ordinary ingestion artifact: the broker order state projection."""
    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, "
                " latest_event_timestamp, last_seen_filled_quantity, dirty_for_trade_sync, needs_reconcile, "
                " terminal, exchange, tradingsymbol, instrument_token, product, transaction_type, updated_at) "
                "VALUES (:account, :oid, 'COMPLETE', NOW(), :filled, false, false, true, 'NSE', 'RELIANCE', "
                " 738561, 'CNC', 'BUY', NOW()) "
                "ON CONFLICT (account_id, order_id) DO UPDATE SET latest_status = 'COMPLETE', terminal = true"
            ),
            {"account": account_id, "oid": order_id, "filled": int(filled)},
        )
        session.commit()


def _claim(factory, plan_id: str):
    from sqlalchemy import text

    with factory() as session:
        return (
            session.execute(
                text(
                    "SELECT state, broker_order_ids, delta_snapshot, detail, consumer_token "
                    "FROM public.live_plan_submissions "
                    "WHERE plan_id = :pid AND step_no = 1"
                ),
                {"pid": plan_id},
            )
            .mappings()
            .first()
        )


def _inflight_live(factory, fixture):
    from backend.strategies.settlement import enumerate_inflight_work

    with factory() as session:
        return enumerate_inflight_work(
            account_id=fixture.account_id,
            strategy_id=fixture.strategy_id,
            execution_environment="live",
            db=session,
        )


def test_hosted_live_mode_is_representable(pg):
    """Migration 000039 admits live in the hosted registries (representable)."""
    fixture = _LiveFixture(pg["factory"], with_reservation=False)
    assert fixture.strategy is not None
    from sqlalchemy import text

    with pg["factory"]() as session:
        modes = session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_hosted_strategies_execution_mode'"
            )
        ).scalar_one()
    assert "live" in modes


def test_live_disabled_refuses_before_any_broker_call(pg):
    """HOSTED_LIVE_ENABLED defaults false: a live plan refuses, no claim is made."""
    from backend.strategies.execution import ExecutionRefusal

    fixture = _LiveFixture(pg["factory"])
    broker = _FakeBroker()
    executor = _executor(pg, fixture, live_enabled=False, broker=broker)
    with pytest.raises(ExecutionRefusal) as ctx:
        asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert ctx.value.reason_code == "LIVE_DISABLED"
    assert broker.calls == []
    assert _claim(pg["factory"], fixture.plan_id) is None


def test_live_authority_requires_lease_and_live_token_mode(pg):
    from backend.strategies.live_authority import LiveAuthorityRefusal, derive_live_authority
    from sqlalchemy import text

    fixture = _LiveFixture(pg["factory"])
    derived = derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert derived["binding"]["execution_environment"] == "live"
    assert derived["authority"]["worker_run_id"] == fixture.run_id
    assert derived["authority"]["lease_epoch"] == 1
    assert derived["authority"]["job_id"] == fixture.job_id

    with pg["factory"]() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :jid"),
            {"past": fixture.now - timedelta(seconds=30), "jid": fixture.job_id},
        )
        session.commit()
    with pytest.raises(LiveAuthorityRefusal) as ctx:
        derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert ctx.value.reason_code == "HOSTED_LEASE_EXPIRED"

    with pg["factory"]() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :future WHERE id = :jid"),
            {"future": fixture.now + timedelta(minutes=10), "jid": fixture.job_id},
        )
        session.execute(
            text("UPDATE algo_worker_tokens SET allowed_modes = '[\"paper\"]'::jsonb WHERE token_id = :tid"),
            {"tid": fixture.token_id},
        )
        session.commit()
    with pytest.raises(LiveAuthorityRefusal) as ctx2:
        derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert ctx2.value.reason_code == "TOKEN_MODE_NOT_ALLOWED"

    # A token whose CAPABILITIES do not include order submission is not trade
    # authority either: the pinned mode list alone is not the contract.
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE algo_worker_tokens SET allowed_modes = '[\"live\"]'::jsonb, "
                " allowed_actions = '[\"proposals:submit\"]'::jsonb WHERE token_id = :tid"
            ),
            {"tid": fixture.token_id},
        )
        session.commit()
    with pytest.raises(LiveAuthorityRefusal) as ctx3:
        derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert ctx3.value.reason_code == "TOKEN_ACTION_NOT_ALLOWED"
    assert ctx3.value.detail["missing_actions"] == ["intents:submit"]


def test_route_environment_comes_from_persisted_binding(pg):
    from backend.api.routers.strategies import _plan_environment
    from sqlalchemy import text

    from fastapi import HTTPException

    fixture = _LiveFixture(pg["factory"])
    # Binding says live, so the route must pick the LIVE executor even though the
    # registry's default mode is a separate column and the request carries no
    # environment at all.
    assert _plan_environment(pg["factory"], fixture.plan()) == "live"
    unbound = dict(fixture.plan())
    unbound["proposal_id"] = str(uuid.uuid4())
    with pytest.raises(HTTPException) as ctx:
        _plan_environment(pg["factory"], unbound)
    assert ctx.value.status_code == 409
    assert ctx.value.detail["reason_code"] == "PLAN_NOT_VALIDATED"


def test_single_instrument_live_entry_ingestion_exit_and_settlement(pg):
    from sqlalchemy import text

    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from backend.strategies.settlement import ExecutionBarrier

    fixture = _LiveFixture(pg["factory"], with_catalog=True)
    broker = _FakeBroker(order_ids=("OID-LIVE-1", "OID-LIVE-2"))
    executor = _executor(pg, fixture, live_enabled=True, broker=broker)

    entry = asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert entry["status"] == "submitted", entry
    assert entry["broker_order_ids"] == ["OID-LIVE-1"]
    claim = _claim(pg["factory"], fixture.plan_id)
    assert claim is not None and claim["state"] == "pending"
    kinds = {item.kind for item in _inflight_live(pg["factory"], fixture)}
    assert "live_submission_pending" in kinds

    # Ordinary ingestion: the broker order/fill lands in the platform's fact
    # table and intent ledger, NOT in the consumer.
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, broker_order_id, execution_mode, status) "
                "VALUES (:iid, 'KA1', :account, :run, 'single_instrument', :run, 'hosted_plan', "
                " 'OID-LIVE-1', 'live', 'placed')"
            ),
            {"iid": f"lint_{uuid.uuid4().hex[:8]}", "account": fixture.account_id, "run": fixture.run_id},
        )
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, 'TR-1', 'OID-LIVE-1', 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', 10, 1500.0, NOW(), true)"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    _record_order_outcome(pg["factory"], account_id=fixture.account_id, order_id="OID-LIVE-1", filled=10)

    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    counts = asyncio.run(consumer.poll_once())
    assert counts["filled"] == 1, counts
    claim = _claim(pg["factory"], fixture.plan_id)
    assert claim["state"] == "filled"
    kinds = {item.kind for item in _inflight_live(pg["factory"], fixture)}
    assert "live_submission_pending" not in kinds, kinds
    reservation = consumer.ledger.for_plan(fixture.plan_id)
    assert str(reservation["status"]) == "consumed"

    # EXIT: the published book now holds +10, so the same instrument targets 0.
    fixture.publish_projection(net_quantity=10)
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO strategy_position_projection (account_id, strategy_id, execution_environment, "
                " identity_kind, identity_key, canonical_instrument_id, instrument_token, exchange, tradingsymbol, "
                " product, net_quantity, projection_version, updated_at) "
                "VALUES (:account, :sid, 'live', 'canonical', :iid, :iid, 738561, 'NSE', 'RELIANCE', 'CNC', 10, 2, NOW()) "
                "ON CONFLICT DO NOTHING"
            ),
            {"account": fixture.account_id, "sid": fixture.strategy_id, "iid": fixture.instrument_id},
        )
        session.commit()
    exit_plan_id = str(uuid.uuid4())
    exit_proposal_id = str(uuid.uuid4())
    exit_leg = dict(fixture.leg)
    exit_leg["signed_quantity"] = 0
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, evaluation_id, "
                " evaluation_kind, strategy_run_id, target_kind, payload, payload_sha256, status) "
                "VALUES (:pid, :sid, :account, :eval, 'run_now', :run, 'single_instrument', '{}', 'sha2', 'validated')"
            ),
            {
                "pid": exit_proposal_id,
                "sid": fixture.strategy_id,
                "account": fixture.account_id,
                "eval": f"eval-{exit_plan_id}",
                "run": fixture.run_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                " logical_plan, resolved_plan, pinned_catalog_generation, pinned_universe_revision_id, pinned_member_hash) "
                "VALUES (:pid, :prop, :sid, :account, 'single_instrument', 'hash2', '{}', :resolved, :gen, NULL, NULL)"
            ),
            {
                "pid": exit_plan_id,
                "prop": exit_proposal_id,
                "sid": fixture.strategy_id,
                "account": fixture.account_id,
                "resolved": json.dumps(
                    {"target_kind": "single_instrument", "catalog_generation": G1, "legs": [exit_leg]}
                ),
                "gen": G1,
            },
        )
        session.commit()

    exit_view = fixture.plan_view_from_row()
    exit_view["plan_id"] = exit_plan_id
    exit_view["proposal_id"] = exit_proposal_id
    exit_view["resolved_plan"] = {"target_kind": "single_instrument", "legs": [exit_leg]}

    # The owner authorises the exit plan against a FRESH reservation (the
    # approval is what binds the immutable plan; the entry reservation is
    # already consumed by its fill).
    from backend.strategies.approvals import ApprovalRequest, ApprovalService
    from backend.strategies.reservations import ClaimRequest, ReservationLedger

    exit_reservation = ReservationLedger(session_factory=pg["factory"]).claim(
        ClaimRequest(
            plan_id=exit_plan_id,
            strategy_id=fixture.strategy_id,
            account_id=fixture.account_id,
            evaluation_id=f"eval-{exit_plan_id}",
            execution_environment="live",
            requirement_inr=15000.0,
            valid_until=fixture.now + timedelta(hours=1),
            allocation_inr=1_000_000.0,
            actor_id=OWNER,
        ),
        now=fixture.now,
    )
    ApprovalService(session_factory=pg["factory"]).approve(
        ApprovalRequest(
            plan=exit_view,
            actor_id=OWNER,
            reservation_id=str(exit_reservation["reservation_id"]),
            execution_environment="live",
            session_product_snapshot={"products": ["CNC"]},
            margin_evidence={"usable": 500000.0, "as_of": fixture.now.isoformat()},
        ),
        now=fixture.now,
    )

    exit_result = asyncio.run(executor.execute(exit_view, actor=OWNER))
    assert exit_result["status"] == "submitted"
    assert exit_result["broker_order_ids"] == ["OID-LIVE-2"]
    # The reducing exit needed no NEW capacity: the consumed reservation is not
    # re-opened, and the order's side is SELL (10 -> 0).
    intent, _ = broker.calls[-1]
    assert intent.payload["order"]["transaction_type"] == "SELL"
    assert intent.payload["order"]["quantity"] == 10

    # Guarded live settlement: with the exit ingsted, the book proves quiescent.
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, broker_order_id, execution_mode, status) "
                "VALUES (:iid, 'KA2', :account, :run, 'single_instrument', :run, 'hosted_plan', "
                " 'OID-LIVE-2', 'live', 'placed')"
            ),
            {"iid": f"lint_{uuid.uuid4().hex[:8]}", "account": fixture.account_id, "run": fixture.run_id},
        )
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, 'TR-2', 'OID-LIVE-2', 738561, 'NSE', 'RELIANCE', 'CNC', 'SELL', 10, 1501.0, NOW(), true)"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    _record_order_outcome(pg["factory"], account_id=fixture.account_id, order_id="OID-LIVE-2", filled=10)

    counts = asyncio.run(consumer.poll_once())
    assert counts["filled"] == 1, counts
    proof = ExecutionBarrier(session_factory=pg["factory"]).record_proof(
        account_id=fixture.account_id,
        strategy_id=fixture.strategy_id,
        execution_environment="live",
        ref="test:live",
    )
    assert proof.recorded is True, proof


def test_ingestion_scoping_and_terminal_coverage(pg):
    """Fills must be owned by the step's account/run; terminal proof is required."""
    from sqlalchemy import text

    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    fixture = _LiveFixture(pg["factory"])
    broker = _FakeBroker(order_ids=("OID-SCOPE-1",))
    executor = _executor(pg, fixture, live_enabled=True, broker=broker)
    result = asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert result["status"] == "submitted"

    # A fill for the SAME broker order id under ANOTHER account is not this
    # step's evidence (and the order has no owner row here at all).
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES ('kite:someone-else', 'TRX', 'OID-SCOPE-1', 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', 10, 1500.0, NOW(), true)"
            )
        )
        session.commit()
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    assert asyncio.run(consumer.poll_once())["filled"] == 0
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "pending"

    # A terminal CANCELLED with a partial fill resolves the RESIDUAL as partial,
    # never as a fabricated fill, and never as "rejected with fills".
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, broker_order_id, execution_mode, status) "
                "VALUES (:iid, 'KA9', :account, :run, 'single_instrument', :run, 'hosted_plan', "
                " 'OID-SCOPE-1', 'live', 'placed')"
            ),
            {"iid": f"lint_{uuid.uuid4().hex[:8]}", "account": fixture.account_id, "run": fixture.run_id},
        )
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, 'TRY', 'OID-SCOPE-1', 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', 4, 1500.0, NOW(), true)"
            ),
            {"account": fixture.account_id},
        )
        session.execute(
            text(
                "INSERT INTO order_state_projection (account_id, order_id, latest_status, latest_event_timestamp, "
                " last_seen_filled_quantity, dirty_for_trade_sync, needs_reconcile, terminal, updated_at) "
                "VALUES (:account, 'OID-SCOPE-1', 'CANCELLED', NOW(), 4, false, false, true, NOW())"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    counts = asyncio.run(consumer.poll_once())
    assert counts["rejected"] == 0, counts
    assert counts["repair_required"] == 1, counts
    claim = _claim(pg["factory"], fixture.plan_id)
    # The residual is NOT resolved and NOT reported as completed or rejected: it
    # stays an explicit repair for a human, with the residual named.
    assert claim["state"] == "repair_required"
    detail = claim["detail"]
    assert detail["repair_required"] is True
    assert detail["residual_quantity"] == 6
    assert detail["blocking"] == "terminal_cancel_with_residual"


def test_live_authority_requires_lease_and_stop_request_refuses(pg):
    from sqlalchemy import text

    from backend.strategies.live_authority import LiveAuthorityRefusal, derive_live_authority

    fixture = _LiveFixture(pg["factory"])
    # A queued job with NO lease is not trade authority.
    with pg["factory"]() as session:
        session.execute(
            text("UPDATE strategy_jobs SET status='queued', lease_owner=NULL, lease_epoch=0, lease_until=NULL WHERE id=:jid"),
            {"jid": fixture.job_id},
        )
        session.commit()
    with pytest.raises(LiveAuthorityRefusal) as ctx:
        derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert ctx.value.reason_code == "HOSTED_LEASE_MISSING"

    # An operator stop wins over a still-running status with a live lease.
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE strategy_jobs SET status='running', desired_state='stopped', lease_owner='sup-1', "
                " lease_epoch=1, lease_until=:until WHERE id=:jid"
            ),
            {"until": fixture.now + timedelta(minutes=10), "jid": fixture.job_id},
        )
        session.commit()
    with pytest.raises(LiveAuthorityRefusal) as ctx2:
        derive_live_authority(pg["factory"], plan=fixture.plan(), now=fixture.now)
    assert ctx2.value.reason_code == "HOSTED_STOP_REQUESTED"


# ---------------------------------------------------------------------------
# Ingestion hardening: single writer, crash repair, no duplicate effects
# ---------------------------------------------------------------------------


def _barrier_events(factory, fixture, *, event="work_resolved"):
    from sqlalchemy import text

    with factory() as session:
        rows = session.execute(
            text(
                "SELECT ref, detail ->> 'plan_id' AS plan_id, version "
                "FROM public.strategy_execution_barrier_events "
                "WHERE account_id = :account AND strategy_id = :sid "
                "AND execution_environment = 'live' AND event = :event"
            ),
            {
                "account": fixture.account_id,
                "sid": fixture.strategy_id,
                "event": event,
            },
        ).fetchall()
    return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]


def _consume_events(factory, reservation_id):
    from sqlalchemy import text

    with factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM strategy_reservation_events "
                    "WHERE reservation_id = :rid AND event = 'consumed'"
                ),
                {"rid": reservation_id},
            ).scalar()
            or 0
        )


def _claim_with_order(pg, fixture, *, order_id: str, filled: int, trade_id: str = "TR-H1"):
    """Drive a live step to ``pending`` with an accepted broker order id."""
    from sqlalchemy import text

    broker = _FakeBroker(order_ids=(order_id,))
    executor = _executor(pg, fixture, live_enabled=True, broker=broker)
    result = asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert result["status"] == "submitted", result
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO live_order_intents (intent_id, client_order_ref, account_id, strategy_run_id, "
                " strategy_family, strategy_name, entry_surface, broker_order_id, execution_mode, status) "
                "VALUES (:iid, :ref, :account, :run, 'single_instrument', :run, 'hosted_plan', "
                " :oid, 'live', 'placed')"
            ),
            {
                "iid": f"lint_{uuid.uuid4().hex[:8]}",
                "ref": f"KA-{uuid.uuid4().hex[:6]}",
                "account": fixture.account_id,
                "run": fixture.run_id,
                "oid": order_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, :tid, :oid, 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', :qty, 1500.0, NOW(), true)"
            ),
            {"account": fixture.account_id, "tid": trade_id, "oid": order_id, "qty": int(filled)},
        )
        session.commit()
    _record_order_outcome(pg["factory"], account_id=fixture.account_id, order_id=order_id, filled=filled)


def test_two_consumers_serialize_the_step_and_do_not_duplicate_effects(pg):
    """Two consumer instances racing one step: ONE writer, ONE set of effects."""
    import threading

    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-RACE-1", filled=10)

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _consume():
        consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)

        async def _go():
            barrier.wait(timeout=30)
            return await consumer.poll_once()

        try:
            results.append(asyncio.run(_go()))
        except Exception as exc:  # noqa: BLE001 - reported by the assertions below
            errors.append(repr(exc))

    threads = [threading.Thread(target=_consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert not errors, errors
    assert len(results) == 2, results
    # The step resolves exactly once across BOTH consumers, and the loser either
    # skipped the locked row or found it already terminal.
    assert sum(int(result["filled"]) for result in results) == 1, results
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "filled"
    events = _barrier_events(pg["factory"], fixture)
    assert len(events) == 1, events
    reservation_id = str(fixture.reservation["reservation_id"])
    assert _consume_events(pg["factory"], reservation_id) == 1


def test_crash_after_publish_resumes_without_duplicate_effects(pg):
    """A staged claim whose cursor stopped after PUBLISH is repaired, not redone."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from sqlalchemy import text

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-CRASH-1", filled=10)
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)

    # Stage the claim exactly as the consumer would after publishing attribution
    # and consuming capacity, then simulate the crash BEFORE the barrier write.
    first = asyncio.run(consumer.poll_once())
    assert first["filled"] == 1, first
    assert len(_barrier_events(pg["factory"], fixture)) == 1

    # Rewind the durable row to the mid-finalize state (publish+capacity done,
    # barrier not recorded) — the recovery cursor a crash would leave behind.
    with pg["factory"]() as session:
        detail = session.execute(
            text("SELECT detail FROM public.live_plan_submissions WHERE plan_id = :pid"),
            {"pid": fixture.plan_id},
        ).scalar_one()
        if isinstance(detail, str):
            detail = json.loads(detail)
        cursor = dict(detail["cursor"])
        cursor["barrier"] = False
        cursor.pop("barrier_version", None)
        session.execute(
            text(
                "UPDATE public.live_plan_submissions SET state = 'finalizing', detail = :detail, "
                " consumer_token = NULL, consumer_until = NULL WHERE plan_id = :pid"
            ),
            {"pid": fixture.plan_id, "detail": json.dumps({"cursor": cursor})},
        )
        session.commit()

    second = asyncio.run(consumer.poll_once())
    assert second["filled"] == 1, second
    claim = _claim(pg["factory"], fixture.plan_id)
    assert claim["state"] == "filled"
    # The barrier event was NOT duplicated: one step, one work_resolved.
    assert len(_barrier_events(pg["factory"], fixture)) == 1
    assert _consume_events(pg["factory"], str(fixture.reservation["reservation_id"])) == 1


def test_staged_claim_stays_in_flight_for_the_settlement_barrier(pg):
    """A staged (unconfirmed) claim is enumerated as in-flight work."""
    from backend.strategies.settlement import enumerate_inflight_work
    from sqlalchemy import text

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-STAGE-1", filled=10)
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.live_plan_submissions SET state = 'finalizing' WHERE plan_id = :pid"
            ),
            {"pid": fixture.plan_id},
        )
        session.commit()
    with pg["factory"]() as session:
        kinds = {
            item.kind
            for item in enumerate_inflight_work(
                account_id=fixture.account_id,
                strategy_id=fixture.strategy_id,
                execution_environment="live",
                db=session,
            )
        }
    assert "live_submission_finalizing" in kinds, kinds


def test_publication_generation_regression_blocks_the_terminal_write(pg, monkeypatch):
    """Cross-publication consistency: a book that moved backwards is re-published."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-GEN-1", filled=10)
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)

    async def _fail_publish(*, account_id, strategy_id):
        return None

    # A publication that cannot be confirmed keeps the step in ``finalizing``
    # with the blocker named; the terminal claim is never written.
    monkeypatch.setattr(consumer, "_publish_attribution", _fail_publish)
    counts = asyncio.run(consumer.poll_once())
    assert counts["filled"] == 0, counts
    claim = _claim(pg["factory"], fixture.plan_id)
    assert claim["state"] == "finalizing", claim
    assert claim["detail"]["cursor"]["blocking"] == "attribution_unpublished"
    assert _barrier_events(pg["factory"], fixture) == []

    # With publication available again the same staged claim completes.
    monkeypatch.undo()
    counts = asyncio.run(consumer.poll_once())
    assert counts["filled"] == 1, counts
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "filled"
    assert len(_barrier_events(pg["factory"], fixture)) == 1


def test_per_order_fill_sync_completeness_is_required(pg):
    """Every order's trades must be delivered before its status proves anything."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from sqlalchemy import text

    # A CANCELLED order whose broker last-seen filled quantity (4) is NOT fully
    # ingested (3) is not terminal proof: the missing trade could still arrive.
    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-SYNC-1", filled=3)
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.order_state_projection SET latest_status = 'CANCELLED', "
                " last_seen_filled_quantity = 4, terminal = true "
                "WHERE account_id = :account AND order_id = 'OID-SYNC-1'"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    counts = asyncio.run(consumer.poll_once())
    # 3 of 10 filled, and the order is not provably settled: nothing resolves.
    assert counts["filled"] == 0 and counts["rejected"] == 0, counts
    assert counts["repair_required"] == 0, counts
    state = _claim(pg["factory"], fixture.plan_id)["state"]
    assert state in ("pending", "partial", "unknown"), state
    assert _barrier_events(pg["factory"], fixture) == []

    # Once the missing trade is delivered, the SAME evidence resolves the
    # residual as an explicit repair (terminal cancel + residual).
    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO order_trade_fills (account_id, trade_id, order_id, instrument_token, exchange, "
                " tradingsymbol, product, transaction_type, quantity, price, fill_timestamp, applied_to_position) "
                "VALUES (:account, 'TR-SYNC-2', 'OID-SYNC-1', 738561, 'NSE', 'RELIANCE', 'CNC', 'BUY', 1, 1500.0, "
                " NOW(), true)"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    counts = asyncio.run(consumer.poll_once())
    assert counts["repair_required"] == 1, counts
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "repair_required"


def test_abandoned_lease_is_taken_over_after_expiry(pg):
    """A crashed consumer's lease expires by time; the step is never stranded."""
    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from sqlalchemy import text

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-LEASE-1", filled=10)
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.live_plan_submissions SET consumer_token = 'dead-consumer', "
                " consumer_until = NOW() - INTERVAL '1 minute' WHERE plan_id = :pid"
            ),
            {"pid": fixture.plan_id},
        )
        session.commit()
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    counts = asyncio.run(consumer.poll_once())
    assert counts["filled"] == 1, counts
    assert counts["locked"] == 0, counts


def test_live_subsystem_gates_launch_admission_and_submission(pg, monkeypatch):
    """``HOSTED_LIVE_ENABLED`` is enforced at every live surface, default off."""
    from fastapi import HTTPException

    from backend.api.routers.strategies import _refuse_live_when_disabled
    from backend.strategies.execution import ExecutionRefusal
    from backend.strategies.live_settings import hosted_live_enabled

    monkeypatch.setenv("HOSTED_LIVE_ENABLED", "false")
    assert hosted_live_enabled() is False
    for surface in ("job_launch", "plan_reserve"):
        with pytest.raises(HTTPException) as ctx:
            _refuse_live_when_disabled("live", surface=surface)
        assert ctx.value.status_code == 409
        assert ctx.value.detail["setting"] == "HOSTED_LIVE_ENABLED"

    # A paper/dry-run launch is untouched by the setting.
    _refuse_live_when_disabled("paper", surface="job_launch")
    _refuse_live_when_disabled("dry_run", surface="job_launch")

    # SUBMISSION: the executor refuses before any broker call or claim.
    fixture = _LiveFixture(pg["factory"])
    broker = _FakeBroker()
    executor = _executor(pg, fixture, live_enabled=False, broker=broker)
    with pytest.raises(ExecutionRefusal) as ctx2:
        asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert ctx2.value.reason_code == "LIVE_DISABLED"
    assert broker.calls == []
    assert _claim(pg["factory"], fixture.plan_id) is None


def test_a_weekend_clock_refuses_an_increasing_live_plan(pg):
    """The production gate, end to end: a shut market refuses an increase.

    The fixture's clock is the wall clock and the disposable database carries no
    imported calendar, so the session is supplied as EVIDENCE through the
    production ``market_session_provider`` seam - pinned to a Saturday by the
    REAL helper - and everything else (plan, book, reservation, approval,
    authority, the adapter's admission call) is the production path.
    """
    from backend.strategies.admission import AdmissionService
    from backend.strategies.execution import ExecutionRefusal
    from tests.support.market_session_stub import weekend_session_provider

    # No catalog seed: the market gate runs before catalog validation, and this
    # module's shared database already carries the instrument from its sibling
    # tests (that catalog row is keyed by ``NSE:RELIANCE``).
    fixture = _LiveFixture(pg["factory"])
    broker = _FakeBroker()
    executor = _executor(
        pg,
        fixture,
        live_enabled=True,
        broker=broker,
        admission=AdmissionService(
            session_factory=pg["factory"],
            market_session_provider=weekend_session_provider,
        ),
    )
    with pytest.raises(ExecutionRefusal) as ctx:
        asyncio.run(executor.execute(fixture.plan_view_from_row(), actor=OWNER))
    assert ctx.value.reason_code == "LIVE_ADMISSION_REFUSED"
    assert ctx.value.detail["reason_code"] == "MARKET_CLOSED"
    assert ctx.value.detail["detail"]["reason"] == "weekend"
    assert ctx.value.detail["detail"]["exchange"] == "NSE"
    assert broker.calls == []
    assert _claim(pg["factory"], fixture.plan_id) is None


def test_consumer_loop_and_bootstrap_start_stop_are_functional(pg, monkeypatch):
    """The background consumer starts, polls, publishes health and stops cleanly.

    Also covers the bootstrap wiring: the deployment setting decides whether the
    task exists at all, and shutdown cancels and awaits it.
    """
    from fastapi import FastAPI

    from backend.app import bootstrap as bootstrap_module
    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], interval_seconds=0.05)

    async def _loop_lifecycle():
        task = asyncio.create_task(consumer.run_forever())
        await asyncio.sleep(0.2)
        assert consumer.health()["state"] in ("starting", "ok", "degraded")
        assert consumer.health()["last_poll_at"] is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert consumer.health()["state"] == "stopped"

    asyncio.run(_loop_lifecycle())

    app = FastAPI()

    async def _bootstrap_lifecycle():
        monkeypatch.delenv("HOSTED_LIVE_ENABLED", raising=False)
        task = await bootstrap_module.start_live_outcome_consumer(app)
        assert task is None
        assert app.state.live_outcome_consumer is None

        monkeypatch.setenv("HOSTED_LIVE_ENABLED", "true")
        task = await bootstrap_module.start_live_outcome_consumer(app)
        assert task is not None and not task.done()
        assert app.state.live_outcome_consumer is not None
        await asyncio.sleep(0.05)
        await bootstrap_module.stop_live_outcome_consumer(app)
        assert app.state.live_outcome_task is None
        assert task.cancelled() or task.done()

    asyncio.run(_bootstrap_lifecycle())


def test_paused_owner_after_takeover_cannot_apply_effects(pg):
    """Pause -> lease expiry -> takeover -> the OLD owner resumes.

    The old owner must not be able to write a stage, record a barrier event or
    consume capacity after another consumer has taken the step over. Every
    effect is fenced by the lease inside the same transaction that applies it,
    and the barrier is de-duplicated by the database as well.
    """
    from sqlalchemy import text

    from backend.strategies.live_ingestion import LiveOutcomeConsumer

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-PAUSE-1", filled=10)

    # The stale owner: it takes the lease, then "pauses" mid-finalize.
    stale = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    stale.consumer_id = "stale-owner"

    def _fail_consume(_plan_id, *, filled):
        _ = filled
        return False

    stale._consume_reservation = _fail_consume  # type: ignore[assignment]
    counts = asyncio.run(stale.poll_once())
    assert counts["filled"] == 0, counts
    mid = _claim(pg["factory"], fixture.plan_id)
    assert mid["state"] == "finalizing", mid
    assert _barrier_events(pg["factory"], fixture) == []

    # Model the pause the consumer itself cannot express: the owner is still
    # NAMED on the row but its lease has expired (a pause longer than the lease).
    # The row and its cursor stay; the authority does not.
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.live_plan_submissions "
                "SET consumer_token = 'stale-owner', "
                " consumer_until = NOW() - INTERVAL '1 minute' WHERE plan_id = :pid"
            ),
            {"pid": fixture.plan_id},
        )
        session.commit()

    # A fresh consumer takes the step over and completes it.
    fresh = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    counts = asyncio.run(fresh.poll_once())
    assert counts["filled"] == 1, counts
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "filled"
    assert len(_barrier_events(pg["factory"], fixture)) == 1
    reservation_id = str(fixture.reservation["reservation_id"])
    assert _consume_events(pg["factory"], reservation_id) == 1

    # The old owner now resumes. Its stage write must be refused by the lease
    # guard, and its barrier attempt must be refused by the in-transaction lease
    # fence + de-duplication. Neither changes the durable row or the barrier.
    before = _claim(pg["factory"], fixture.plan_id)
    written = stale.submissions.record_outcome(
        plan_id=fixture.plan_id,
        step_no=1,
        state="filled",
        detail={"cursor": {"outcome": "filled", "barrier": True}},
        consumer_token="stale-owner",
    )
    after = _claim(pg["factory"], fixture.plan_id)
    assert written["state"] == "filled"
    assert dict(after["detail"]) == dict(before["detail"]), (before, after)

    version = stale._record_barrier_once(
        account_id=fixture.account_id,
        strategy_id=fixture.strategy_id,
        ref=f"live-plan:{fixture.plan_id}:step:1",
        plan_id=fixture.plan_id,
        outcome="filled",
        filled=10,
        ordered=10,
        step_no=1,
    )
    assert version is None, "a stale owner recorded a barrier effect after takeover"
    assert len(_barrier_events(pg["factory"], fixture)) == 1
    assert _consume_events(pg["factory"], reservation_id) == 1


def test_barrier_work_resolved_is_unique_per_live_step(pg):
    """The database refuses a duplicate live ``work_resolved`` for one step."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from backend.strategies.settlement import ExecutionBarrier

    fixture = _LiveFixture(pg["factory"])
    barrier = ExecutionBarrier(session_factory=pg["factory"])
    first, created = barrier.record_work_event_once(
        account_id=fixture.account_id,
        strategy_id=fixture.strategy_id,
        execution_environment="live",
        event="work_resolved",
        ref="live-plan:step:1",
        detail={"plan_id": fixture.plan_id, "outcome": "filled"},
        dedupe_key=fixture.plan_id,
    )
    assert created is True and first is not None
    second, created_again = barrier.record_work_event_once(
        account_id=fixture.account_id,
        strategy_id=fixture.strategy_id,
        execution_environment="live",
        event="work_resolved",
        ref="live-plan:step:1",
        detail={"plan_id": fixture.plan_id, "outcome": "filled"},
        dedupe_key=fixture.plan_id,
    )
    assert created_again is False
    assert second == first, (second, first)

    # Bypassing the helper (raw INSERT) hits the unique index: the guarantee is
    # the database's, not only the helper's.
    with pg["factory"]() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO public.strategy_execution_barrier_events "
                    "(id, account_id, strategy_id, execution_environment, version, event, ref, detail) "
                    "VALUES (:id, :account, :sid, 'live', :version, 'work_resolved', 'live-plan:step:1', "
                    " CAST(:detail AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "account": fixture.account_id,
                    "sid": fixture.strategy_id,
                    "version": int(first),
                    "detail": json.dumps({"plan_id": fixture.plan_id, "outcome": "filled"}),
                },
            )
        session.rollback()


def test_live_settlement_requires_complete_current_account_truth(pg):
    """Stale/unknown ingest truth blocks the live book; it is never 'flat'."""
    from sqlalchemy import text

    from backend.strategies.reconciliation_service import ReconciliationEvidenceCollector
    from backend.strategies.attribution import SqlAttributionStore
    from backend.strategies.settlement import ExecutionBarrier

    fixture = _LiveFixture(pg["factory"])

    class _Job:
        id = "job-collector"
        strategy_id = ""
        attempt = 1
        run_id = ""
        account_scope = ""
        execution_mode = "live"
        status = "stopped"
        desired_state = "started"
        token_id = None
        capabilities_snapshot = {"trade": True}
        process_cleanup_state = "confirmed"
        process_cleanup_at = None
        process_cleanup_actor = None
        reconciled_at = None
        handoff_at = None

    job = _Job()
    job.strategy_id = fixture.strategy_id
    job.account_scope = fixture.account_id
    job.run_id = fixture.run_id

    store = SqlAttributionStore(session_factory=pg["factory"])
    from backend.strategies.attribution import StrategyAttributionService

    asyncio.run(
        StrategyAttributionService(store).publish(
            account_id=fixture.account_id,
            strategy_id=fixture.strategy_id,
            execution_environment="live",
        )
    )
    collector = ReconciliationEvidenceCollector(
        worker_repo=None, settlement_barrier=ExecutionBarrier(session_factory=pg["factory"]),
        session_factory=pg["factory"],
    )

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.account_ingest_state (account_id, last_complete_ingest_at, "
                " ingest_generation, status) VALUES (:a, NOW(), 1, 'stale') "
                "ON CONFLICT (account_id) DO UPDATE SET status = 'stale'"
            ),
            {"a": fixture.account_id},
        )
        session.commit()
    stale = collector._live_settlement(job)
    assert stale["exposure_state"] == "unknown", stale
    assert "live_account_ingest_stale" in stale["unavailable"], stale

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.account_ingest_state (account_id, last_complete_ingest_at, "
                " ingest_generation, status) VALUES (:a, NOW(), 2, 'refreshing') "
                "ON CONFLICT (account_id) DO UPDATE SET status = 'refreshing'"
            ),
            {"a": fixture.account_id},
        )
        session.commit()
    refreshing = collector._live_settlement(job)
    assert refreshing["exposure_state"] == "unknown", refreshing
    assert "live_account_ingest_refreshing" in refreshing["unavailable"], refreshing
    assert refreshing["work_state"] == "unknown", refreshing

    # A completion timestamp is REQUIRED even when the status reads idle: an
    # ingest that never finished is not complete truth.
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.account_ingest_state SET status = 'idle', "
                " last_complete_ingest_at = NULL WHERE account_id = :a"
            ),
            {"a": fixture.account_id},
        )
        session.commit()
    no_timestamp = collector._live_settlement(job)
    assert no_timestamp["exposure_state"] == "unknown", no_timestamp
    assert "live_account_ingest_incomplete" in no_timestamp["unavailable"], no_timestamp

    with pg["factory"]() as session:
        session.execute(
            text(
                "INSERT INTO public.account_ingest_state (account_id, last_complete_ingest_at, "
                " ingest_generation, status) VALUES (:a, NOW(), 3, 'idle') "
                "ON CONFLICT (account_id) DO UPDATE SET status = 'idle', last_complete_ingest_at = NOW()"
            ),
            {"a": fixture.account_id},
        )
        session.commit()
    complete = collector._live_settlement(job)
    assert complete["unavailable"] == [], complete
    assert complete["exposure_state"] == "flat", complete
    assert complete["work_state"] == "settled", complete


def test_residual_repair_disposition_is_bounded_and_audited(pg):
    """A residual is dispositioned only by an authorised, allowed action.

    While the plan's evaluation authority is live the disposition is REFUSED, so
    an operator cannot abandon a residual that might still fill. Once the attempt
    is stopped, the bounded action records the decision in the append-only trail,
    releases the unused capacity and resolves the step's work exactly once.
    """
    from sqlalchemy import text

    from backend.strategies.live_ingestion import LiveOutcomeConsumer
    from backend.strategies.live_repair import LiveRepairRefusal, LiveRepairService

    fixture = _LiveFixture(pg["factory"])
    _claim_with_order(pg, fixture, order_id="OID-REPAIR-1", filled=4)
    with pg["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.order_state_projection SET latest_status = 'CANCELLED', "
                " last_seen_filled_quantity = 4, terminal = true "
                "WHERE account_id = :account AND order_id = 'OID-REPAIR-1'"
            ),
            {"account": fixture.account_id},
        )
        session.commit()
    consumer = LiveOutcomeConsumer(session_factory=pg["factory"], clock=lambda: fixture.now)
    counts = asyncio.run(consumer.poll_once())
    assert counts["repair_required"] == 1, counts
    assert _claim(pg["factory"], fixture.plan_id)["state"] == "repair_required"

    service = LiveRepairService(
        session_factory=pg["factory"], clock=lambda: fixture.now
    )
    # The attempt is still live, so a residual might still fill: refused.
    with pytest.raises(LiveRepairRefusal) as ctx:
        service.abandon_residual(plan_id=fixture.plan_id, actor=OWNER, reason="operator decision")
    assert ctx.value.reason_code == "LIVE_AUTHORITY_STILL_ACTIVE"
    assert _barrier_events(pg["factory"], fixture) == []

    # The owner stops the attempt (revoked credential, closed run); now the
    # bounded disposition is allowed.
    with pg["factory"]() as session:
        session.execute(
            text("UPDATE public.algo_worker_tokens SET status = 'revoked' WHERE token_id = :tid"),
            {"tid": fixture.token_id},
        )
        session.execute(
            text("UPDATE public.algo_worker_runs SET status = 'closed' WHERE strategy_run_id = :run"),
            {"run": fixture.run_id},
        )
        session.commit()

    result = service.abandon_residual(
        plan_id=fixture.plan_id, actor=OWNER, reason="residual will not be worked"
    )
    assert result["state"] == "residual_abandoned", result
    assert result["idempotent"] is False
    disposition = result["disposition"]
    assert disposition["residual_quantity"] == 6, disposition
    assert disposition["filled_quantity"] == 4, disposition
    assert disposition["actor_id"] == OWNER
    # CAPACITY CORRECTION (root acceptance): the step FILLED 4 of 10, so the
    # reservation already backs real exposure. The ledger has no partial release,
    # so releasing the plan's reservation here would un-fund the position that
    # exists - the disposition marks the leg terminal and CONSUMES the capacity
    # instead. Only a plan whose every leg is terminal with NO fill releases.
    assert disposition["capacity_released"] is False, disposition
    assert disposition["capacity_consumed"] is True, disposition

    events = _barrier_events(pg["factory"], fixture)
    assert len(events) == 1, events
    reservation = service.ledger.for_plan(fixture.plan_id)
    assert str(reservation["status"]) == "consumed", reservation

    with pg["factory"]() as session:
        trail = session.execute(
            text(
                "SELECT event, actor_id, detail ->> 'residual_quantity' AS residual "
                "FROM public.strategy_plan_execution_events "
                "WHERE plan_id = :pid AND event = 'residual_abandoned'"
            ),
            {"pid": fixture.plan_id},
        ).mappings().all()
    assert len(trail) == 1, [dict(row) for row in trail]
    assert trail[0]["actor_id"] == OWNER
    assert str(trail[0]["residual"]) == "6"

    # Idempotent: the second call reports the existing disposition and writes
    # nothing new.
    again = service.abandon_residual(plan_id=fixture.plan_id, actor=OWNER)
    assert again["idempotent"] is True
    assert len(_barrier_events(pg["factory"], fixture)) == 1

    # A step that is not awaiting repair is refused by name.
    other = _LiveFixture(pg["factory"])
    _claim_with_order(pg, other, order_id="OID-REPAIR-2", filled=4)
    with pytest.raises(LiveRepairRefusal) as ctx2:
        service.abandon_residual(plan_id=other.plan_id, actor=OWNER)
    assert ctx2.value.reason_code == "LIVE_REPAIR_NOT_REQUIRED"
