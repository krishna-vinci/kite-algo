"""The internal live adapter: validated against every control, fake broker only.

There is no public live route and no enable flag - this suite exercises the
internal seam with a FAKE broker at the handler boundary (never a network), and
proves the refusals are named. The public live refusal itself stays covered by
the HTTP suite (``tests/api/test_strategy_owner_and_binding.py``).

Runs against a DISPOSABLE database on the local test server (port 15433).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

OWNER = "app:owner"
G1 = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_live_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    base = PG_ADMIN.rpartition("/")[0]
    return name, f"{base}/{name}"


def _drop_db(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(PG_ADMIN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="module")
def pg():
    import psycopg2  # noqa: F401  real driver before any stub
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


def _leg(instrument_id: str) -> dict:
    return {
        "instrument_id": instrument_id,
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "broker_exchange": "NSE",
        "broker_symbol": "RELIANCE",
        "broker_token": 738561,
        "product": "CNC",
        "instrument_type": "EQ",
        "lot_size": 1,
        "signed_quantity": 10,
        "reference_price": 1500.0,
    }


class _Env:
    """One live fixture: strategy + frozen plan + reservation + approval + run."""

    def __init__(self, factory, *, run_id: str, plan_kind: str = "single_instrument",
                 approve: bool = True, reserve: bool = True, policy: bool = True,
                 target: int = 10):
        import json

        from sqlalchemy import text

        self.factory = factory
        self.run_id = run_id
        self.plan_kind = plan_kind
        self.strategy_id = f"stg-live-{uuid.uuid4().hex[:8]}"
        self.account_id = f"kite:live-{uuid.uuid4().hex[:6]}"
        self.plan_id = str(uuid.uuid4())
        self.instrument_id = str(uuid.uuid4())
        self.proposal_id = str(uuid.uuid4())
        self.leg = _leg(self.instrument_id)
        # Plans are INSERT-ONLY (enforced by a trigger on PostgreSQL), so the
        # frozen target is chosen at construction.
        self.leg["signed_quantity"] = int(target)

        from backend.strategies import service as strategy_service
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        repo = SqlAlchemyStrategyRepository(factory)
        # NOTE: the hosted strategy registry itself has no live mode
        # (``ck_hosted_strategies_execution_mode`` allows only paper/dry_run), so
        # the live scenario is constructed the only way the platform currently
        # can express it: a paper-registered strategy bound to a LIVE run. This
        # doubles as the evidence for that live-readiness limitation.
        self.strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"live-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=self.account_id,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        # The repository owns the canonical strategy id; the plan/proposal and the
        # binding must use THAT identity, not a test-invented one.
        self.strategy_id = str(self.strategy.id)
        self.resolved = {"target_kind": plan_kind, "catalog_generation": G1, "legs": [self.leg]}
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                    " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                    " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                    " :run, :kind, '{}', 'sha', 'validated')"
                ),
                {
                    "pid": self.proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "eval": f"eval-{self.plan_id}",
                    "run": run_id,
                    "kind": plan_kind,
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                    " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES (:pid, :prop, :sid, :account, :kind, 'plan-hash-1', '{}', :resolved, "
                    " :gen, NULL, NULL)"
                ),
                {
                    "pid": self.plan_id,
                    "prop": self.proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "kind": plan_kind,
                    "resolved": json.dumps(self.resolved),
                    "gen": G1,
                },
            )
            session.commit()

        from backend.strategies.admission import AdmissionService

        if policy:
            AdmissionService(session_factory=factory).upsert_policy(
                strategy_id=self.strategy_id,
                account_id=self.account_id,
                updated_by=OWNER,
                allocation_inr=1_000_000.0,
            )

        self.reservation = None
        if reserve:
            from backend.strategies.reservations import ClaimRequest, ReservationLedger

            self.reservation = ReservationLedger(session_factory=factory).claim(
                ClaimRequest(
                    plan_id=self.plan_id,
                    strategy_id=self.strategy_id,
                    account_id=self.account_id,
                    evaluation_id=f"eval-{self.plan_id}",
                    execution_environment="live",
                    requirement_inr=15000.0,
                    valid_until=NOW + timedelta(hours=1),
                    allocation_inr=1_000_000.0,
                    actor_id=OWNER,
                ),
                now=NOW,
            )

        if approve and self.reservation is not None:
            from backend.strategies.approvals import ApprovalRequest, ApprovalService

            self.approval = ApprovalService(session_factory=factory).approve(
                ApprovalRequest(
                    plan=self.plan(),
                    actor_id=OWNER,
                    reservation_id=str(self.reservation["reservation_id"]),
                    execution_environment="live",
                    session_product_snapshot={"products": ["CNC"]},
                    margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                ),
                now=NOW,
            )

        # The live-bound run the adapter requires: an ``algo_worker_runs`` row in
        # live mode (the FK target) plus the canonical binding.
        from sqlalchemy import text as _text

        with factory() as session:
            session.execute(
                _text(
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, template_id, "
                    " account_scope, execution_mode, status) "
                    "VALUES (:run, 'tok-live', 'tpl-live', :account, 'live', 'open') "
                    "ON CONFLICT (strategy_run_id) DO NOTHING"
                ),
                {"run": run_id, "account": self.account_id},
            )
            session.commit()

        from backend.strategies.attribution import SqlAttributionStore

        SqlAttributionStore(session_factory=factory).bind_run(
            strategy_run_id=run_id,
            strategy_id=self.strategy_id,
            owner_id=OWNER,
            account_id=self.account_id,
            execution_environment="live",
            bound_by="test",
            binding_source="hosted_job",
        )

    def seed_second_plan(self) -> str:
        """Another plan on the SAME strategy/account (a second step of one book)."""
        import json as _json

        from sqlalchemy import text as _text

        plan_id = str(uuid.uuid4())
        proposal_id = str(uuid.uuid4())
        with self.factory() as session:
            session.execute(
                _text(
                    "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                    " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                    " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                    " :run, :kind, '{}', 'sha', 'validated')"
                ),
                {
                    "pid": proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "eval": f"eval-{plan_id}",
                    "run": self.run_id,
                    "kind": "single_instrument",
                },
            )
            session.execute(
                _text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                    " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation, "
                    " pinned_universe_revision_id, pinned_member_hash) "
                    "VALUES (:pid, :prop, :sid, :account, 'single_instrument', 'plan-hash-2', "
                    " '{}', :resolved, :gen, NULL, NULL)"
                ),
                {
                    "pid": plan_id,
                    "prop": proposal_id,
                    "sid": self.strategy_id,
                    "account": self.account_id,
                    "resolved": _json.dumps(
                        {"target_kind": "single_instrument", "legs": [self.leg]}
                    ),
                    "gen": G1,
                },
            )
            session.commit()
        return plan_id

    def plan(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "plan_kind": self.plan_kind,
            "plan_hash": "plan-hash-1",
            "logical_plan": {},
            "resolved_plan": self.resolved,
            "pinned_catalog_generation": G1,
        }

    def binding(self) -> dict:
        return {
            "strategy_run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "owner_id": OWNER,
            "account_id": self.account_id,
            "execution_environment": "live",
        }

    def authority(self, *, expires_in=600, run_id=None, strategy_id=None):
        return {
            "evaluation_id": f"eval-{self.plan_id}",
            "strategy_id": strategy_id or self.strategy_id,
            "account_id": self.account_id,
            "worker_run_id": run_id or self.run_id,
            "expires_at": (NOW + timedelta(seconds=expires_in)).isoformat(),
        }

    def quote(self, *, age_seconds=0.0):
        return {
            "instrument_id": self.instrument_id,
            "ltp": 1500.0,
            "as_of": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        }


class _FakeBroker:
    """The ONLY fake: the broker boundary. It records calls; it never fills."""

    def __init__(self, *, result=None, error: Exception | None = None):
        self.calls = []
        self._result = result if result is not None else {"result": {"order_id": "OID-1"}}
        self._error = error

    async def handle(self, intent, *, context=None):
        self.calls.append((intent, dict(context or {})))
        if self._error is not None:
            raise self._error
        return dict(self._result)


def _adapter(factory, broker, *, current=0, env=None, authority_reader=None, **kwargs):
    """The adapter with authoritative position AND authority readers.

    ``current`` stands in for the platform's attribution/account-truth reading
    (the delta - and therefore the live order's side and quantity - is derived
    from it), and ``env`` supplies the platform's CURRENT evaluation authority
    that dispatch re-reads.
    """
    from backend.strategies.live_adapter import LivePlanAdapter

    kwargs.setdefault("position_reader", lambda *, plan, leg: current)
    if authority_reader is None and env is not None:
        authority_reader = lambda *, plan, binding: env.authority()
    kwargs.setdefault("authority_reader", authority_reader)
    return LivePlanAdapter(
        session_factory=factory,
        intent_handler=broker,
        clock=lambda: NOW,
        **kwargs,
    )


def test_a_supported_plan_is_accepted_as_pending_never_a_fill(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    submission = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )

    assert submission.state == "pending", submission.as_dict()
    assert submission.broker_order_ids == ["OID-1"]
    assert len(broker.calls) == 1
    # The dispatch carried the existing idempotency convention AND the derived
    # delta: the frozen target is +10 from flat, so it BUYs 10.
    intent, _context = broker.calls[0]
    assert intent.dedupe_key == f"live-plan:{env.plan_id}:step:1"
    assert intent.payload["order"]["transaction_type"] == "BUY"
    assert intent.payload["order"]["quantity"] == 10
    assert submission.detail["delta"]["delta"] == 10
    assert submission.detail["delta"]["current"] == 0

    # A duplicate submit of the SAME step does not reach the broker twice.
    again = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert again.state == "pending"
    assert len(broker.calls) == 1

    # A SECOND adapter instance (a restart / another process) reads the durable
    # claim: it must not dispatch again either.
    second_broker = _FakeBroker()
    second = _adapter(pg["factory"], second_broker, env=env)
    replayed = asyncio.run(
        second.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert replayed.state == "pending"
    assert replayed.broker_order_ids == ["OID-1"]
    assert second_broker.calls == []


def test_transport_uncertainty_is_recovery_required_and_never_repeated(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker(error=TimeoutError("socket closed"))
    adapter = _adapter(pg["factory"], broker, env=env)

    submission = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )

    assert submission.state == "uncertain"
    assert submission.reason_code == "LIVE_TRANSPORT_UNCERTAIN"
    assert len(broker.calls) == 1

    # A retry re-reads the SAME uncertain outcome; it is never auto-repeated.
    retry = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert retry.state == "uncertain"
    assert len(broker.calls) == 1

    # A brand-new instance (the process died) still sees the uncertain claim.
    fresh_broker = _FakeBroker()
    fresh = _adapter(pg["factory"], fresh_broker, env=env)
    after_restart = asyncio.run(
        fresh.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert after_restart.state == "uncertain"
    assert fresh_broker.calls == []


def test_a_compound_plan_kind_is_a_named_refusal(pg):
    """An UNSUPPORTED plan kind is a named refusal, never a guessed dispatch.

    ``target_futures`` and ``option_structure`` left this list in Phase 2B (they
    have their own lanes and their own route acceptance suites); ``intent_bundle``
    remains a named refusal, which is what this pins.
    """
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}", plan_kind="intent_bundle")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_PLAN_KIND_UNSUPPORTED"
    assert broker.calls == []


def test_a_non_live_binding_is_refused(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)
    binding = dict(env.binding(), execution_environment="paper")

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=binding,
                evaluation_authority=env.authority(),
                quote=env.quote(),
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_RUN_BINDING_REQUIRED"
    assert broker.calls == []


def test_a_stale_evaluation_authority_is_refused(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(expires_in=-5),
                quote=env.quote(),
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_EVALUATION_AUTHORITY_STALE"
    assert broker.calls == []


def test_an_authority_bound_to_another_run_is_refused(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(run_id="run-somewhere-else"),
                quote=env.quote(),
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_EVALUATION_AUTHORITY_MISMATCH"
    assert broker.calls == []


def test_a_plan_without_an_owner_approval_is_refused(pg):
    env = _Env(
        pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}", approve=False
    )
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_APPROVAL_REQUIRED"
    assert broker.calls == []


def test_a_stale_quote_is_refused(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(age_seconds=60),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_QUOTE_STALE"
    assert broker.calls == []


def test_missing_margin_evidence_is_refused_by_admission(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env)

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence=None,
                session_id="sess-1",
            )
        )
    assert ctx.value.reason_code == "LIVE_ADMISSION_REFUSED"
    assert ctx.value.detail["reason_code"] == "MARGIN_UNAVAILABLE"
    assert broker.calls == []


def test_a_definite_rejection_resolves_the_work(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker(result={"status": "rejected", "reason": "RMS blocked"})
    adapter = _adapter(pg["factory"], broker, env=env)

    submission = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )

    assert submission.state == "rejected"
    assert submission.reason_code == "LIVE_ORDER_REJECTED"
    assert len(broker.calls) == 1


def test_confirmed_fills_come_only_from_the_ingestion_reader(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    seen = {}

    def _reader(*, broker_order_ids, plan):
        seen["ids"] = list(broker_order_ids)
        return [{"broker_order_id": broker_order_ids[0], "quantity": 4, "partial": True}]

    adapter = _adapter(pg["factory"], broker, fill_reader=_reader)

    rows = adapter.confirmed_fills(plan=env.plan(), broker_order_ids=["OID-1"])

    assert rows[0]["quantity"] == 4 and rows[0]["partial"] is True
    assert seen["ids"] == ["OID-1"]


def test_without_a_fill_source_the_adapter_refuses_rather_than_guesses(pg):
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    adapter = _adapter(pg["factory"], _FakeBroker())

    from backend.strategies.live_adapter import LiveRefusal

    with pytest.raises(LiveRefusal) as ctx:
        adapter.confirmed_fills(plan=env.plan(), broker_order_ids=["OID-1"])
    assert ctx.value.reason_code == "LIVE_FILL_SOURCE_MISSING"


def test_a_malformed_response_is_uncertain_and_keeps_its_work(pg):
    """No order id and no authoritative refusal: UNKNOWN, never a rejection."""
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker(result={})
    adapter = _adapter(pg["factory"], broker, env=env)

    submission = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )

    assert submission.state == "uncertain"
    assert len(broker.calls) == 1
    from sqlalchemy import text

    with pg["factory"]() as session:
        events = [
            str(event)
            for event in session.execute(
                text(
                    "SELECT event FROM strategy_execution_barrier_events "
                    "WHERE strategy_id = :sid ORDER BY created_at"
                ),
                {"sid": env.strategy_id},
            )
            .scalars()
            .all()
        ]
    # The work was created before dispatch and was NOT resolved on a guess.
    assert "work_created" in events
    assert "work_resolved" not in events

    # And a second instance still refuses to repeat it.
    second = _adapter(pg["factory"], _FakeBroker())
    again = asyncio.run(
        second.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert again.state == "uncertain"


def test_the_live_delta_and_direction_come_from_the_attributed_position(pg):
    """target - current decides side AND quantity; a no-op sends nothing."""
    cases = [
        # (target, current, expected state, expected side, expected quantity)
        (-10, 0, "pending", "SELL", 10),   # a new SHORT is a sell, not a buy
        (5, 10, "pending", "SELL", 5),     # a reduction
        (0, 10, "pending", "SELL", 10),    # an absolute flat exit
        (10, 10, "no_op", None, 0),        # no-op
    ]
    for index, (target, current, expected_state, expected_side, expected_quantity) in enumerate(cases):
        env = _Env(
            pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}", target=target
        )
        broker = _FakeBroker()
        adapter = _adapter(pg["factory"], broker, current=current, env=env)

        submission = asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )

        assert submission.state == expected_state, (index, submission.as_dict())
        if expected_state == "no_op":
            assert broker.calls == []
            continue
        intent, _context = broker.calls[0]
        assert intent.payload["order"]["transaction_type"] == expected_side, index
        assert intent.payload["order"]["quantity"] == expected_quantity, index


def test_a_missing_position_reader_refuses_and_sends_nothing(pg):
    from backend.strategies.live_adapter import LivePlanAdapter, LiveRefusal

    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = LivePlanAdapter(
        session_factory=pg["factory"], intent_handler=broker, clock=lambda: NOW
    )

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )

    assert ctx.value.reason_code == "LIVE_POSITION_EVIDENCE_UNAVAILABLE"
    assert broker.calls == []


def test_the_authority_is_rechecked_at_the_moment_of_dispatch(pg):
    """A revocation between validation and dispatch must stop the submission."""
    from backend.strategies.approvals import ApprovalService
    from backend.strategies.live_adapter import LiveRefusal

    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()

    def _revoking_reader(*, plan, leg):
        # Stands in for a platform evidence read that happens after validation
        # and before the dispatch transaction commits its claim.
        ApprovalService(session_factory=pg["factory"]).revoke(
            env.approval["approval_id"], actor_id=OWNER
        )
        return 0

    adapter = _adapter(pg["factory"], broker, env=env, position_reader=_revoking_reader)

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )

    assert ctx.value.reason_code in {"LIVE_APPROVAL_REQUIRED", "LIVE_APPROVAL_INVALID"}
    assert broker.calls == []
    from sqlalchemy import text

    with pg["factory"]() as session:
        claims = session.execute(
            text(
                "SELECT COUNT(*) FROM public.live_plan_submissions WHERE plan_id = :p"
            ),
            {"p": env.plan_id},
        ).scalar()
    assert int(claims or 0) == 0


def _inflight(factory, env):
    from backend.strategies.settlement import enumerate_inflight_work

    with factory() as session:
        items = enumerate_inflight_work(
            account_id=env.account_id,
            strategy_id=env.strategy_id,
            execution_environment="live",
            db=session,
        )
    return [item.as_dict() for item in items]


def _proof(factory, env):
    from backend.strategies.settlement import ExecutionBarrier

    return ExecutionBarrier(session_factory=factory).record_proof(
        account_id=env.account_id,
        strategy_id=env.strategy_id,
        execution_environment="live",
        ref="test:proof",
    )


def test_a_committed_claim_blocks_a_quiet_proof(pg):
    """A crash AFTER the claim commit still leaves blocking work behind.

    This is the shape the two-phase claim exists for: the row is written before
    the network, so a process that dies mid-submission cannot leave a book that
    looks quiet.
    """
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    from backend.strategies.live_adapter import LiveSubmissionStore

    store = LiveSubmissionStore(session_factory=pg["factory"])
    store.claim(
        plan_id=env.plan_id,
        step_no=1,
        step_ref=f"live-plan:{env.plan_id}:step:1",
        strategy_id=env.strategy_id,
        account_id=env.account_id,
        execution_environment="live",
        delta_snapshot={"target": 10, "current": 0, "delta": 10, "quantity": 10},
        state="pending",
        detail={"crashed_after_commit": True},
    )

    kinds = {(item["kind"], item["ref"]) for item in _inflight(pg["factory"], env)}
    assert ("live_submission_pending", f"live-plan:{env.plan_id}:step:1") in kinds
    proof = _proof(pg["factory"], env)
    assert proof.recorded is False
    assert {item.kind for item in proof.inflight} >= {"live_submission_pending"}


def test_a_pending_or_uncertain_submission_blocks_the_whole_book(pg):
    """Another plan on the same book cannot bypass the unresolved submission."""
    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker(error=TimeoutError("socket closed"))
    adapter = _adapter(pg["factory"], broker, env=env)
    result = asyncio.run(
        adapter.submit(
            env.plan(),
            actor=OWNER,
            run_binding=env.binding(),
            evaluation_authority=env.authority(),
            quote=env.quote(),
            margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
            session_id="sess-1",
        )
    )
    assert result.state == "uncertain"

    # The blocker is the BOOK's, not the plan's: the proof refuses and the
    # enumeration names the uncertain step.
    proof = _proof(pg["factory"], env)
    assert proof.recorded is False
    assert "live_submission_uncertain" in {item.kind for item in proof.inflight}
    assert any(
        item["kind"] == "live_submission_uncertain"
        for item in _inflight(pg["factory"], env)
    )

    # A SECOND plan on the same book (a separate step of the same strategy) still
    # cannot make that book look quiet: the source is the book's, not the plan's.
    env.seed_second_plan()
    still_blocked = _proof(pg["factory"], env)
    assert still_blocked.recorded is False
    assert "live_submission_uncertain" in {item.kind for item in still_blocked.inflight}
    assert any(
        item["kind"] == "live_submission_uncertain"
        for item in _inflight(pg["factory"], env)
    )


def test_a_missing_authority_reader_refuses_at_dispatch(pg):
    """No platform authority reader ⇒ no claimed re-check, so no dispatch."""
    from backend.strategies.live_adapter import LivePlanAdapter, LiveRefusal

    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    broker = _FakeBroker()
    adapter = LivePlanAdapter(
        session_factory=pg["factory"],
        intent_handler=broker,
        clock=lambda: NOW,
        position_reader=lambda *, plan, leg: 0,
    )

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=env.authority(),
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )

    assert ctx.value.reason_code == "LIVE_AUTHORITY_EVIDENCE_UNAVAILABLE"
    assert broker.calls == []


def test_an_authority_that_expired_before_dispatch_refuses(pg):
    """The dispatch-time re-read sees the CURRENT authority, not the caller's."""
    from datetime import timedelta

    from backend.strategies.live_adapter import LiveRefusal

    env = _Env(pg["factory"], run_id=f"run-live-{uuid.uuid4().hex[:8]}")
    expired = env.authority()
    expired["expires_at"] = (NOW - timedelta(seconds=30)).isoformat()
    broker = _FakeBroker()
    adapter = _adapter(pg["factory"], broker, env=env, authority_reader=lambda *, plan, binding: expired)

    with pytest.raises(LiveRefusal) as ctx:
        asyncio.run(
            adapter.submit(
                env.plan(),
                actor=OWNER,
                run_binding=env.binding(),
                evaluation_authority=expired,
                quote=env.quote(),
                margin_evidence={"usable": 500000.0, "as_of": NOW.isoformat()},
                session_id="sess-1",
            )
        )

    # The initial check already refuses an expired authority; either refusal is
    # correct, and nothing was dispatched or claimed.
    assert ctx.value.reason_code in {
        "LIVE_EVALUATION_AUTHORITY_STALE",
        "LIVE_EVALUATION_AUTHORITY_MISSING",
    }
    assert broker.calls == []
