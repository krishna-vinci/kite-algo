"""Governed execution requests: the durable decision, claim and dispatch path.

These tests drive the REAL services (authorization, request, snapshot, pipeline)
against a real SQLite schema and a real ``SqlAlchemyStrategyRepository`` job
ledger. Only two things are faked: the broker/paper boundary (no order may be
placed here) and the HTTP transport.

What is pinned:

* a manual request waits, and waiting is not execution;
* an owner decision is durable and queues exactly one dispatch;
* an autonomous request queues only under a matching current grant, and the
  authorization it produces is recorded as AUTOMATIC, never as a manual click;
* a changed version, source, policy, mode, account or attempt refuses;
* a dependent release re-derives its authority instead of inheriting approval;
* a claim is a claim: it is not broker acceptance, and recovery never replays.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.routers import strategies as strategies_router  # noqa: E402
from backend.api.routers import worker_execution as worker_execution_router  # noqa: E402
from backend.api.routers import worker_executions as worker_executions_router  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.strategies.admission import AdmissionService  # noqa: E402
from backend.strategies.approvals import ApprovalService  # noqa: E402
from backend.strategies.attribution import SqlAttributionStore  # noqa: E402
from backend.strategies.attribution_models import (  # noqa: E402
    LivePlanSubmission,
    StrategyPlanExecutionEvent,
    StrategyPlan,
    StrategyPlanOptionRun,
    StrategyPositionProjection,
    StrategyProjectionState,
    StrategyProposal,
    StrategyRunBinding,
)
from backend.strategies.execution import ExecutionRefusal  # noqa: E402
from backend.strategies.execution_authorization import ExecutionAuthorizationService  # noqa: E402
from backend.strategies.execution_requests import (  # noqa: E402
    ExecutionRequestConflict,
    ExecutionRequestService,
    ExecutionRequestStateError,
)
from backend.strategies.execution_snapshot import OwnedWorkSnapshotService  # noqa: E402
from backend.strategies.models import (  # noqa: E402
    HostedExecutionAudit,
    StrategyJob,
)
from backend.strategies.plan_pipeline import (  # noqa: E402
    PlanExecutionPipeline,
    PipelineRefusal,
)
from backend.strategies.proposals import ProposalStore  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.strategies.reservations import ReservationLedger  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402
from tests.support.hosted_fakes import FakeWorkerRepository, StubJournalService  # noqa: E402

OWNER = "app:admin"
ACCOUNT = "kite:paper"
CHILD_ID = "worker_child"
CHILD_RAW = "kwa_child_execution_token"
EXTERNAL_ID = "worker_external"
EXTERNAL_RAW = "kwa_external_execution_token"
RUN_ID = "run_hosted_exec"
NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
#: The published catalog generation these plans are pinned to. The admission and
#: approval paths read ``public.instrument_catalog_generations``, so the SQLite
#: harness attaches a ``public`` schema with exactly the columns they select.
GENERATION_ID = "11111111-2222-3333-4444-555555555555"

_PUBLIC_DDL = (
    """
    CREATE TABLE public.instrument_catalog_generations (
        id TEXT PRIMARY KEY, status TEXT, published_at TEXT
    )
    """,
    """
    CREATE TABLE public.instrument_catalog_records (
        instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
        lifecycle_status TEXT NOT NULL DEFAULT 'active', current_generation_id TEXT,
        instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL,
        underlying TEXT, strike REAL, option_type TEXT
    )
    """,
    """
    CREATE TABLE public.instrument_broker_mappings (
        mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT, broker_exchange TEXT,
        broker_symbol TEXT, broker_token TEXT, valid_from_generation TEXT,
        valid_to_generation TEXT, is_current INTEGER
    )
    """,
    # The options lane's durable run table, exactly as ``schema.sql`` defines it.
    # The plan/run discovery reads it with a raw ``public.`` query, so it must
    # exist here for an option-entry plan to be assessed at all.
    """
    CREATE TABLE public.option_run_states (
        strategy_run_id TEXT PRIMARY KEY,
        strategy_name TEXT NOT NULL,
        product VARCHAR(8) NOT NULL CHECK (product IN ('MIS', 'NRML')),
        status VARCHAR(64) NOT NULL,
        legs TEXT NOT NULL DEFAULT '[]',
        protection TEXT,
        metadata TEXT NOT NULL DEFAULT '{}',
        orders TEXT NOT NULL DEFAULT '[]',
        trades TEXT NOT NULL DEFAULT '[]',
        completed_legs TEXT NOT NULL DEFAULT '[]',
        failed_legs TEXT NOT NULL DEFAULT '[]',
        pending_legs TEXT NOT NULL DEFAULT '[]',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Durable protection ownership (B2.4 S1): the entry hook writes the owner
    # row in the same transaction as the run and its entry edge.
    """
    CREATE TABLE public.option_protection_owners (
        option_run_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        execution_environment TEXT NOT NULL,
        owner_run_id TEXT,
        owner_epoch INTEGER NOT NULL DEFAULT 1,
        policy_version TEXT NOT NULL,
        policy TEXT NOT NULL,
        action_state TEXT NOT NULL DEFAULT 'none',
        stage_digest TEXT,
        state TEXT NOT NULL DEFAULT 'active',
        released_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE public.option_protection_owner_events (
        id TEXT PRIMARY KEY,
        option_run_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL,
        event TEXT NOT NULL,
        owner_run_id TEXT,
        actor_id TEXT,
        detail TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # The plan/run binding edge, exactly as ``schema.sql`` defines it. The raw
    # options store both writes and reads it with a ``public.`` qualifier (the
    # binding store, the adjust owner set and the run's bound-plan set), so the
    # edge lives in the attached ``public`` schema rather than in ``main``: one
    # row set, read the same way by every reader.
    """
    CREATE TABLE public.strategy_plan_option_runs (
        plan_id TEXT PRIMARY KEY,
        option_run_id TEXT NOT NULL,
        worker_run_id TEXT,
        strategy_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        execution_environment TEXT NOT NULL,
        phase TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakePaperExecutor:
    """The paper/broker boundary. Records calls and refuses a second execution."""

    def __init__(self, factory) -> None:
        self.session_factory = factory
        self.calls: list = []
        self.executed: set = set()
        self.trail_plan_ids: set = set()

    async def execute(self, plan, *, actor: str):
        plan_id = str(plan["plan_id"])
        self.calls.append({"plan_id": plan_id, "actor": actor})
        if plan_id in self.executed:
            raise ExecutionRefusal(
                "PLAN_ALREADY_EXECUTED",
                {"plan_id": plan_id, "message": "this plan already has an execution submission"},
            )
        self.executed.add(plan_id)
        if plan_id in self.trail_plan_ids:
            with self.session_factory() as session:
                session.add(
                    StrategyPlanExecutionEvent(
                        id=str(uuid.uuid4()),
                        plan_id=plan_id,
                        step_no=0,
                        event="submitted",
                        actor_id=str(actor),
                        detail={"quantity": 5},
                    )
                )
                session.commit()
        return {"status": "accepted", "plan_id": plan_id, "step_results": []}


class ExecutionWorkerRepository(FakeWorkerRepository):
    """The shared fake plus the intent-result surface the paper intent route uses.

    Phase 1's ``hosted_fakes`` is deliberately left untouched; that surface only
    matters for the external-compatibility assertion below, so it is added here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.intent_results: dict = {}

    async def get_intent_result(self, strategy_run_id, idempotency_key):
        return self.intent_results.get((str(strategy_run_id), str(idempotency_key)))

    async def save_intent_result(self, *, token_id, strategy_run_id, request, status, result):
        self.intent_results[(str(strategy_run_id), str(request.idempotency_key))] = dict(
            result or {}
        )
        return dict(result or {})


def _make_job(repo, *, strategy, version, mode="paper", run_id=RUN_ID, token_id=CHILD_ID):
    job = repo.create_job(
        strategy_id=strategy.id,
        version_id=version.id,
        owner_id=OWNER,
        job_kind="finite",
        execution_mode=mode,
        params={},
    )
    repo.claim_job(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=0,
        expected_attempt=1,
        lease_until=_now() + timedelta(hours=1),
    )
    repo.reserve_child_token(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=1,
        expected_attempt=1,
        token_id=token_id,
    )
    repo.record_child_run(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=1,
        expected_attempt=1,
        token_id=token_id,
        run_id=run_id,
    )
    repo.mark_running_and_handoff(
        job.id,
        lease_owner="sup-A",
        expected_lease_epoch=1,
        expected_attempt=1,
        run_id=run_id,
    )
    return job


def _binding(factory, *, strategy, run_id=RUN_ID, environment="paper", account=ACCOUNT):
    with factory() as session:
        session.add(
            StrategyRunBinding(
                strategy_run_id=str(run_id),
                strategy_id=str(strategy.id),
                owner_id=OWNER,
                account_id=str(account),
                execution_environment=str(environment),
                bound_by="test",
                binding_source="hosted_job",
            )
        )
        session.commit()


def _catalog_generation(engine) -> None:
    with engine.connect() as connection:
        connection.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:id, 'published', :at)"
            ),
            {"id": GENERATION_ID, "at": NOW.isoformat()},
        )
        connection.commit()


def _plan(
    factory,
    *,
    strategy,
    run_id=RUN_ID,
    account=ACCOUNT,
    plan_id=None,
    plan_hash=None,
    quantity=5,
    price=100.0,
):
    plan_id = plan_id or str(uuid.uuid4())
    plan_hash = plan_hash or ("h" * 64)
    proposal_id = str(uuid.uuid4())
    legs = [
        {
            "instrument_id": "i1",
            "signed_quantity": quantity,
            "reference_price": price,
            "product": "CNC",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "broker_symbol": "INFY",
            "broker_exchange": "NSE",
            # Frozen units: the production executor refuses a leg with no pinned
            # lot (PLAN_UNITS_UNPINNED) rather than guessing from the catalog.
            "lot_size": 1,
        }
    ]
    with factory() as session:
        session.add(
            StrategyProposal(
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                evaluation_id=f"eval-{proposal_id}",
                evaluation_kind="run_now",
                job_id=None,
                strategy_run_id=str(run_id),
                target_kind="single_instrument",
                payload={"legs": legs},
                payload_sha256="p" * 64,
                status="validated",
            )
        )
        session.flush()
        session.add(
            StrategyPlan(
                plan_id=plan_id,
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                plan_kind="single_instrument",
                plan_hash=plan_hash,
                logical_plan={"legs": legs},
                resolved_plan={"legs": legs},
                pinned_universe_revision_id=None,
                pinned_member_hash=None,
                pinned_catalog_generation=GENERATION_ID,
            )
        )
        session.commit()
    return plan_id, plan_hash


#: Frozen structure identities for the option-entry gate tests. Two DIFFERENT
#: digests are two different structures; the same digest is a duplicate.
HELD_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _option_legs():
    """Two frozen option legs, shaped exactly as the compiler emits them."""
    return [
        {
            "instrument_id": "opt-sold-25000",
            "tradingsymbol": "NIFTY26OCT25000CE",
            "broker_symbol": "NIFTY26OCT25000CE",
            "broker_exchange": "NFO",
            "exchange": "NFO",
            "product": "NRML",
            "instrument_type": "CE",
            "option_type": "CE",
            "side": "SELL",
            "ratio": 1,
            "quantity": 75,
            "signed_quantity": -75,
            "reference_price": 100.0,
            "lot_size": 75,
            "strike": 25000.0,
        },
        {
            "instrument_id": "opt-bought-26000",
            "tradingsymbol": "NIFTY26OCT26000CE",
            "broker_symbol": "NIFTY26OCT26000CE",
            "broker_exchange": "NFO",
            "exchange": "NFO",
            "product": "NRML",
            "instrument_type": "CE",
            "option_type": "CE",
            "side": "BUY",
            "ratio": 1,
            "quantity": 75,
            "signed_quantity": 75,
            "reference_price": 80.0,
            "lot_size": 75,
            "strike": 26000.0,
        },
    ]


def _option_plan(
    factory,
    *,
    strategy,
    run_id=RUN_ID,
    account=ACCOUNT,
    digest=OTHER_DIGEST,
    plan_id=None,
):
    """A validated frozen ``option_structure`` ENTRY plan of this strategy."""
    plan_id = plan_id or str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    legs = _option_legs()
    resolved = {
        "target_kind": "option_structure",
        "product": "NRML",
        "structure_digest": digest,
        "legs": legs,
        "option_run": {"phase": "entry", "option_run_id": None},
    }
    with factory() as session:
        session.add(
            StrategyProposal(
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                evaluation_id=f"eval-{proposal_id}",
                evaluation_kind="run_now",
                job_id=None,
                strategy_run_id=str(run_id),
                target_kind="option_structure",
                payload={"legs": legs, "phase": "entry"},
                payload_sha256="o" * 64,
                status="validated",
            )
        )
        session.flush()
        session.add(
            StrategyPlan(
                plan_id=plan_id,
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                plan_kind="option_structure",
                plan_hash="h" * 64,
                logical_plan={"legs": legs},
                resolved_plan=resolved,
                pinned_universe_revision_id=None,
                pinned_member_hash=None,
                pinned_catalog_generation=GENERATION_ID,
            )
        )
        session.commit()
    return plan_id


def _option_adjust_plan(
    factory,
    *,
    strategy,
    reference,
    generation=1,
    digest=OTHER_DIGEST,
    run_id=RUN_ID,
    account=ACCOUNT,
    plan_id=None,
    protection_policy=None,
):
    """A validated frozen ``option_structure`` ADJUST plan of this strategy.

    The desired target is the same covered structure ``_option_legs`` describes;
    what varies per test is the generation basis the plan froze.
    """
    plan_id = plan_id or str(uuid.uuid4())
    proposal_id = str(uuid.uuid4())
    legs = _option_legs()
    resolved = {
        "target_kind": "option_structure",
        "product": "NRML",
        "structure_digest": digest,
        "legs": legs,
        "option_run": {
            "phase": "adjust",
            "option_run_id": str(reference),
            "based_on_generation": int(generation),
        },
    }
    if protection_policy is not None:
        resolved["protection_policy"] = protection_policy
    with factory() as session:
        session.add(
            StrategyProposal(
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                evaluation_id=f"eval-{proposal_id}",
                evaluation_kind="run_now",
                job_id=None,
                strategy_run_id=str(run_id),
                target_kind="option_structure",
                payload={"legs": legs, "phase": "adjust"},
                payload_sha256="a" * 64,
                status="validated",
            )
        )
        session.flush()
        session.add(
            StrategyPlan(
                plan_id=plan_id,
                proposal_id=proposal_id,
                strategy_id=str(strategy.id),
                account_id=str(account),
                plan_kind="option_structure",
                plan_hash="h" * 64,
                logical_plan={"legs": legs},
                resolved_plan=resolved,
                pinned_universe_revision_id=None,
                pinned_member_hash=None,
                pinned_catalog_generation=GENERATION_ID,
            )
        )
        session.commit()
    return plan_id


def _bind_adjust_edge(world, *, plan_id, run_id):
    """The adjust edge a FIRST attempt of ``plan_id`` writes, before it submits."""
    with world["factory"]() as session:
        session.add(
            StrategyPlanOptionRun(
                plan_id=str(plan_id),
                option_run_id=str(run_id),
                strategy_id=str(world["strategy"].id),
                account_id=ACCOUNT,
                execution_environment="paper",
                phase="adjust",
            )
        )
        session.commit()


def _move_structure_generation(world, option_run_id, *, generation):
    """The run's held leg generation moved (an adjust landed)."""
    with world["factory"]() as session:
        session.execute(
            text(
                "UPDATE public.option_run_states SET metadata = :metadata "
                "WHERE strategy_run_id = :run"
            ),
            {
                "metadata": json.dumps({"structure_generation": int(generation)}),
                "run": option_run_id,
            },
        )
        session.commit()


def _own_option_run(world, *, plan_id, status=None, owner=True):
    """A durable option run THIS strategy owns, reached from ``plan_id``'s edge."""
    from backend.options.execution.durable_store import DurableOptionRunStore
    from backend.options.execution.models import OptionRunCreateRequest
    from backend.options.protection.ownership import (
        OptionProtectionOwnerStore,
        option_protection_policy_snapshot,
    )

    run = DurableOptionRunStore(session_factory=world["factory"]).create_run(
        OptionRunCreateRequest(
            strategy_name="held-structure",
            product="NRML",
            legs=[
                {
                    "tradingsymbol": "NIFTY26OCT25000CE",
                    "transaction_type": "SELL",
                    "quantity": 75,
                },
                {
                    "tradingsymbol": "NIFTY26OCT26000CE",
                    "transaction_type": "BUY",
                    "quantity": 75,
                },
            ],
            metadata={
                "source": "b21a-entry-gate",
                # The scope the protection owner row has to agree with: a
                # plan-created run carries it, and B2.4's gate reads the owner
                # row for every run reached through ``strategy_plan_option_runs``.
                "strategy_id": str(world["strategy"].id),
                "account_id": ACCOUNT,
                "execution_environment": "paper",
                "worker_run_id": "run-held-structure",
            },
        )
    )
    # The entry hook's own rule, done here because this fixture creates the run
    # directly: a run bound through ``strategy_plan_option_runs`` carries an
    # ACTIVE protection owner, and B2.4 S2b refuses to grow exposure for one
    # whose ownership cannot be read.
    if owner:
        OptionProtectionOwnerStore(session_factory=world["factory"]).claim(
            run,
            "run-held-structure",
            option_protection_policy_snapshot({"structure_digest": HELD_DIGEST}),
        )
    with world["factory"]() as session:
        session.add(
            StrategyPlanOptionRun(
                plan_id=str(plan_id),
                option_run_id=run.strategy_run_id,
                strategy_id=str(world["strategy"].id),
                account_id=ACCOUNT,
                execution_environment="paper",
                phase="entry",
            )
        )
        session.commit()
        if status is not None:
            session.execute(
                text(
                    "UPDATE public.option_run_states SET status = :status "
                    "WHERE strategy_run_id = :run"
                ),
                {"status": str(status), "run": run.strategy_run_id},
            )
            session.commit()
    return run.strategy_run_id


@pytest.fixture()
def world():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public(dbapi_connection, _record):  # pragma: no cover - harness
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        for statement in _PUBLIC_DDL:
            cursor.execute(statement)
        cursor.close()

    # The option binding edge is deliberately NOT created here: it lives in the
    # attached ``public`` schema with the run table it points at, because the raw
    # store qualifies it. SQLite resolves an unqualified name to ``main`` first,
    # so a second copy there would be the one the ORM wrote and read while the
    # raw readers saw the empty ``public`` table - two views of one relation.
    Base.metadata.create_all(
        engine,
        tables=[
            table
            for table in Base.metadata.sorted_tables
            if table.name != "strategy_plan_option_runs"
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _catalog_generation(engine)
    repo = SqlAlchemyStrategyRepository(factory)
    strategy = repo.create_strategy(
        owner_id=OWNER,
        name="governed-exec",
        description=None,
        execution_mode="paper",
        job_kind="finite",
        account_scope=ACCOUNT,
        max_duration_s=21600,
        progress_deadline_s=600,
        stale_exit_policy="exit_on_worker_stale",
    )
    version = repo.create_version(
        strategy_id=strategy.id,
        source="print('v1')",
        source_sha256="a" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        # A declared per-version risk policy (B2.5): the frozen structure this
        # world trades is a vertical spread, and the policy admits one.
        risk_policy={
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        },
        created_by=OWNER,
    )
    version2 = repo.create_version(
        strategy_id=strategy.id,
        source="print('v2')",
        source_sha256="b" * 64,
        parameters_schema={"type": "object"},
        capabilities_snapshot={"schema_version": 1},
        risk_policy={
            "allowed_structure_families": ["vertical_spread"],
            "naked_permitted": False,
        },
        created_by=OWNER,
    )
    job = _make_job(repo, strategy=strategy, version=version)
    # Re-read the row: the lifecycle transitions above wrote run_id/attempt/
    # lease_epoch to the database, and the request service derives its identity
    # from the persisted job.
    job = repo.get_job_by_id(job.id)
    _binding(factory, strategy=strategy)

    worker = ExecutionWorkerRepository()
    worker.tokens[CHILD_ID] = {
        "token_id": CHILD_ID,
        "name": "hosted-child",
        "account_scope": ACCOUNT,
        "allowed_modes": ["paper"],
        "allowed_actions": [
            "runs:read",
            "runs:log",
            "runs:progress",
            "proposals:submit",
            "intents:submit",
            "runs:exit",
        ],
        "allowed_templates": [f"hosted:{strategy.id}"],
        "status": "active",
        "expires_at": None,
        "metadata": {"source": "hosted_supervisor"},
    }
    worker.hashes[_hash_token(CHILD_RAW)] = CHILD_ID
    worker.tokens[EXTERNAL_ID] = {
        "token_id": EXTERNAL_ID,
        "name": "external",
        "account_scope": ACCOUNT,
        "allowed_modes": ["paper"],
        "allowed_actions": ["runs:read", "intents:submit"],
        "allowed_templates": [],
        "status": "active",
        "expires_at": None,
        "metadata": {},
    }
    worker.hashes[_hash_token(EXTERNAL_RAW)] = EXTERNAL_ID
    worker.runs[RUN_ID] = {
        "strategy_run_id": RUN_ID,
        "token_id": CHILD_ID,
        "template_id": f"hosted:{strategy.id}",
        "account_scope": ACCOUNT,
        "execution_mode": "paper",
        "status": "open",
        "summary_fields": [],
        "risk_schema": [],
        "allowed_actions": [],
        "runtime_state": {},
        "metadata": {},
        "worker_session_nonce": "wsn_live",
        "worker_session_claimed_at": _now().isoformat(),
        "last_heartbeat_at": None,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
        "closed_at": None,
    }
    worker.runs["run_external"] = {
        **worker.runs[RUN_ID],
        "strategy_run_id": "run_external",
        "token_id": EXTERNAL_ID,
        "template_id": "mean_reversion",
        "worker_session_nonce": None,
        "worker_session_claimed_at": None,
    }
    # A second hosted run for the SAME child token that has no persisted
    # binding: it exists for run-scoping and "no owned book" checks.
    worker.runs["run_hosted_2"] = {
        **worker.runs[RUN_ID],
        "strategy_run_id": "run_hosted_2",
    }

    executor = FakePaperExecutor(factory)
    pipeline = PlanExecutionPipeline(
        factory,
        proposal_store=ProposalStore(session_factory=factory),
        admission_service=AdmissionService(session_factory=factory),
        reservation_ledger=ReservationLedger(session_factory=factory),
        approval_service=ApprovalService(session_factory=factory),
        paper_executor_factory=lambda: executor,
    )
    authorization = ExecutionAuthorizationService(factory)
    service = ExecutionRequestService(factory, pipeline=pipeline, authorization=authorization)
    try:
        yield {
            "engine": engine,
            "factory": factory,
            "repo": repo,
            "strategy": strategy,
            "version": version,
            "version2": version2,
            "job": job,
            "worker": worker,
            "executor": executor,
            "pipeline": pipeline,
            "authorization": authorization,
            "service": service,
        }
    finally:
        engine.dispose()


def _app(world):
    app = FastAPI()
    app.include_router(worker_executions_router.router, prefix="/api")
    app.include_router(worker_execution_router.router, prefix="/api")
    app.include_router(strategies_router.router, prefix="/api")
    app.state.algo_worker_repository = world["worker"]
    app.state.strategies_session_factory = world["factory"]
    app.state.attribution_store = SqlAttributionStore(session_factory=world["factory"])
    app.state.journal_service = StubJournalService()
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


CHILD_HEADERS = {"Authorization": f"Bearer {CHILD_RAW}", "X-Worker-Session-Nonce": "wsn_live"}


def _record_policy(world, allocation=100000.0, **extra):
    return AdmissionService(session_factory=world["factory"]).upsert_policy(
        strategy_id=str(world["strategy"].id),
        account_id=ACCOUNT,
        updated_by=OWNER,
        allocation_inr=allocation,
        **extra,
    )


def _autonomous(world, *, version=None, key="grant-key-1", now=NOW):
    world["authorization"].set_mode(OWNER, world["strategy"].id, "autonomous", actor=OWNER)
    grant = world["authorization"].issue_grant(
        OWNER,
        world["strategy"].id,
        actor=OWNER,
        idempotency_key=key,
        version_id=(version or world["version"]).id,
        execution_environment="paper",
        now=now,
    )
    return grant["grant"]


def _audit_events(world):
    with world["factory"]() as session:
        return [
            str(row.event)
            for row in session.execute(
                select(HostedExecutionAudit)
                .where(HostedExecutionAudit.strategy_id == str(world["strategy"].id))
                .order_by(HostedExecutionAudit.audit_id)
            )
            .scalars()
            .all()
        ]


def _trail_events(world, plan_id):
    with world["factory"]() as session:
        return [
            str(row.event)
            for row in session.execute(
                select(StrategyPlanExecutionEvent)
                .where(StrategyPlanExecutionEvent.plan_id == str(plan_id))
                .order_by(StrategyPlanExecutionEvent.created_at, StrategyPlanExecutionEvent.id)
            )
            .scalars()
            .all()
        ]


def _dispatch(service, request_id, *, now=NOW):
    import asyncio

    return asyncio.run(
        service.dispatch(str(request_id), now=now)
    )


# ---------------------------------------------------------------------------
# manual (approval_based) requests
# ---------------------------------------------------------------------------


def test_manual_request_waits_and_executes_nothing(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    result = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0001", now=NOW
    )
    request = result["request"]
    assert result["idempotent"] is False
    assert request["status"] == "awaiting_approval"
    assert request["decision_kind"] is None
    assert request["authorization_mode"] == "approval_based"
    assert request["execution_environment"] == "paper"
    assert request["strategy_run_id"] == RUN_ID

    # Waiting is not execution: no approval, no reservation, no order.
    assert world["executor"].calls == []
    assert world["pipeline"].reservation_for_plan(plan_id) is None
    assert world["pipeline"].approvals.active_for_plan(plan_id) is None
    # And nothing is claimable while it waits.
    assert world["service"].claim_next(limit=10, now=NOW) == []
    assert _audit_events(world)[-1] == "requested"


# ---------------------------------------------------------------------------
# option entries: the structure gate runs BEFORE the owner is asked
# ---------------------------------------------------------------------------


def test_an_option_entry_request_refuses_a_structure_the_strategy_already_holds(world):
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    held_run = _own_option_run(world, plan_id=held_plan, status="created")
    # A DIFFERENT plan freezing the SAME structure (a restarted evaluation).
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-dup-0001", now=NOW
    )

    request = created["request"]
    assert request["status"] == "refused"
    assert request["refusal_code"] == "OPTION_STRUCTURE_ALREADY_OPEN"
    assert request["refusal_detail"]["option_run_id"] == held_run
    assert request["refusal_detail"]["option_run_status"] == "created"
    assert request["refusal_detail"]["stage"] == "request"
    # The owner is never asked to approve it, and nothing became dispatchable.
    assert world["service"].claim_next(limit=10, now=NOW) == []

    # Admission asks the SAME rule and refuses the SAME plan by the SAME name.
    with pytest.raises(PipelineRefusal) as ctx:
        world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert ctx.value.reason_code == "OPTION_STRUCTURE_ALREADY_OPEN"


def test_an_option_entry_request_refuses_while_an_unresolved_run_is_owned(world):
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    held_run = _own_option_run(world, plan_id=held_plan, status="partial_entry")
    # A DIFFERENT structure: nothing about it is a duplicate, and a new entry is
    # refused anyway while the strategy's own partial run is unresolved.
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=OTHER_DIGEST)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-unres-0001", now=NOW
    )

    request = created["request"]
    assert request["status"] == "refused"
    assert request["refusal_code"] == "OPTION_STRUCTURE_UNRESOLVED"
    assert request["refusal_detail"]["option_run_id"] == held_run
    assert request["refusal_detail"]["status"] == "partial_entry"
    assert request["refusal_detail"]["plan_id"] == candidate

    with pytest.raises(PipelineRefusal) as ctx:
        world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert ctx.value.reason_code == "OPTION_STRUCTURE_UNRESOLVED"
    assert ctx.value.detail["option_run_id"] == held_run


def test_approval_refuses_when_an_unresolved_run_appears_while_waiting(world):
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=OTHER_DIGEST)
    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-approve-0001", now=NOW
    )
    assert created["request"]["status"] == "awaiting_approval"

    # The world moved while the owner was deciding: another plan of this strategy
    # now owns a run that is entering, so this plan must not become dispatchable.
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    held_run = _own_option_run(world, plan_id=held_plan, status="entering")

    approved = world["service"].approve(
        created["request"]["request_id"],
        owner_id=OWNER,
        strategy_id=world["strategy"].id,
        actor=OWNER,
        now=NOW,
    )

    assert approved["approved"] is False
    assert approved["request"]["status"] == "refused"
    assert approved["request"]["refusal_code"] == "OPTION_STRUCTURE_UNRESOLVED"
    assert approved["request"]["refusal_detail"]["stage"] == "approval"
    assert approved["request"]["refusal_detail"]["option_run_id"] == held_run
    assert world["service"].claim_next(limit=10, now=NOW) == []


def test_a_cleanly_entered_different_structure_is_still_admitted(world):
    _record_policy(world)
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    _own_option_run(world, plan_id=held_plan, status="entered")
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=OTHER_DIGEST)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-clean-0001", now=NOW
    )

    assert created["request"]["status"] == "awaiting_approval"
    assert created["request"]["refusal_code"] is None

    verdict = world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert verdict["admitted"] is True


def test_an_entry_into_an_ownerless_held_structure_is_refused_by_name(world):
    """B2.4 S2b: a held structure whose protection ownership cannot be read is
    never "nothing to protect". The request refuses by name at creation and is
    never handed to the owner to approve."""
    _record_policy(world)
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    held_run = _own_option_run(world, plan_id=held_plan, status="entered", owner=False)
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=OTHER_DIGEST)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-ownerless-0001", now=NOW
    )

    request = created["request"]
    assert request["status"] == "refused"
    assert request["refusal_code"] == "OPTION_PROTECTION_OWNER_UNKNOWN"
    assert request["refusal_detail"]["option_run_id"] == held_run
    assert request["refusal_detail"]["stage"] == "request"
    assert world["service"].claim_next(limit=10, now=NOW) == []

    # Admission asks the SAME rule and refuses the SAME plan by the SAME name.
    with pytest.raises(PipelineRefusal) as ctx:
        world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert ctx.value.reason_code == "OPTION_PROTECTION_OWNER_UNKNOWN"


def test_the_same_entry_is_admitted_once_the_held_structure_has_an_active_owner(world):
    """The admit twin: the identical request with a readable owner is approved."""
    _record_policy(world)
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    _own_option_run(world, plan_id=held_plan, status="entered")
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=OTHER_DIGEST)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-owned-0001", now=NOW
    )

    assert created["request"]["status"] == "awaiting_approval"
    assert created["request"]["refusal_code"] is None
    assert world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")[
        "admitted"
    ] is True


@pytest.mark.asyncio
async def test_the_admission_preview_refuses_a_duplicate_structure_by_name(world):
    """The owner's own preview is a decision too: 409 with the named refusal."""
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    _own_option_run(world, plan_id=held_plan, status="entered")
    candidate = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)

    app, patches = _operator_client(world)
    try:
        async with _client(app) as client:
            response = await client.post(
                f"/api/strategies/{world['strategy'].id}/plans/{candidate}/admission"
            )
    finally:
        for item in patches:
            item.stop()

    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]["rejection_reason"] == "OPTION_STRUCTURE_ALREADY_OPEN"
    )


def test_the_option_gate_covers_entry_plans_only():
    """Exit plans close work that exists; another lane has no option run at all."""
    from backend.options.execution.plan_binding import (
        is_option_adjust_plan,
        is_option_entry_plan,
    )

    def frozen(resolved):
        return {"resolved_plan": resolved}

    entry = frozen({"target_kind": "option_structure", "option_run": {"phase": "entry"}})
    closing = frozen(
        {
            "target_kind": "option_structure",
            "option_run": {"phase": "exit", "option_run_id": "run-1"},
        }
    )
    other = frozen({"target_kind": "single_instrument", "legs": []})
    adjusting = frozen(
        {
            "target_kind": "option_structure",
            "option_run": {
                "phase": "adjust",
                "option_run_id": "run-1",
                "based_on_generation": 1,
            },
        }
    )

    assert is_option_entry_plan(entry) is True
    assert is_option_entry_plan(closing) is False
    assert is_option_entry_plan(other) is False
    # The mutation gate is the same predicate over the other frozen phase: an
    # entry or exit plan never claims it, and an adjust plan never claims entry's.
    assert is_option_adjust_plan(adjusting) is True
    assert is_option_adjust_plan(entry) is False
    assert is_option_adjust_plan(closing) is False
    assert is_option_adjust_plan(other) is False


# ---------------------------------------------------------------------------
# option adjustments: the mutation gate runs BEFORE the owner is asked
# ---------------------------------------------------------------------------


def test_an_option_adjust_request_refuses_a_stale_basis_before_approval(world):
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    run_id = _own_option_run(world, plan_id=held_plan, status="entered")
    # The run HOLDS generation 1; the plan froze a basis of 5.
    candidate = _option_adjust_plan(
        world["factory"], strategy=world["strategy"], reference=run_id, generation=5
    )

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-adj-stale-0001", now=NOW
    )

    request = created["request"]
    # The owner is NEVER asked to approve it: it is refused at creation, not left
    # waiting for a decision the platform would refuse at execution anyway.
    assert request["status"] == "refused"
    assert request["status"] != "awaiting_approval"
    assert request["refusal_code"] == "OPTION_ADJUSTMENT_STALE_BASIS"
    assert request["refusal_detail"]["stage"] == "request"
    assert request["refusal_detail"]["option_run_id"] == run_id
    assert request["refusal_detail"]["based_on_generation"] == 5
    assert request["refusal_detail"]["structure_generation"] == 1
    assert world["service"].claim_next(limit=10, now=NOW) == []

    # Admission asks the SAME rule and refuses the SAME plan by the SAME name.
    with pytest.raises(PipelineRefusal) as ctx:
        world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert ctx.value.reason_code == "OPTION_ADJUSTMENT_STALE_BASIS"


def test_an_option_adjust_request_with_a_matching_basis_is_admitted(world):
    _record_policy(world)
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    run_id = _own_option_run(world, plan_id=held_plan, status="entered")
    candidate = _option_adjust_plan(
        world["factory"], strategy=world["strategy"], reference=run_id, generation=1
    )

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-adj-ok-0001", now=NOW
    )

    assert created["request"]["status"] == "awaiting_approval"
    assert created["request"]["refusal_code"] is None

    verdict = world["pipeline"].admit(world["pipeline"].plan(candidate), environment="paper")
    assert verdict["admitted"] is True


def test_an_option_adjust_approval_refuses_when_the_generation_moved(world):
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    run_id = _own_option_run(world, plan_id=held_plan, status="entered")
    candidate = _option_adjust_plan(
        world["factory"], strategy=world["strategy"], reference=run_id, generation=1
    )
    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-adj-moved-0001", now=NOW
    )
    assert created["request"]["status"] == "awaiting_approval"

    # The world moved while the owner was deciding: the run's leg generation
    # advanced, so the basis the plan froze is dead and it must not become
    # dispatchable work.
    _move_structure_generation(world, run_id, generation=2)
    approved = world["service"].approve(
        created["request"]["request_id"],
        owner_id=OWNER,
        strategy_id=world["strategy"].id,
        actor=OWNER,
        now=NOW,
    )

    assert approved["approved"] is False
    assert approved["request"]["status"] == "refused"
    assert approved["request"]["refusal_code"] == "OPTION_ADJUSTMENT_STALE_BASIS"
    assert approved["request"]["refusal_detail"]["stage"] == "approval"
    assert approved["request"]["refusal_detail"]["based_on_generation"] == 1
    assert approved["request"]["refusal_detail"]["structure_generation"] == 2
    assert world["service"].claim_next(limit=10, now=NOW) == []


def test_an_option_adjust_request_refuses_while_another_plan_owns_the_transition(world):
    """One transition, one owner: a second plan never shares an in-flight adjust."""
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    run_id = _own_option_run(world, plan_id=held_plan, status="adjusting")
    candidate = _option_adjust_plan(
        world["factory"], strategy=world["strategy"], reference=run_id, generation=1
    )

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-adj-inflight-0001", now=NOW
    )

    request = created["request"]
    assert request["status"] == "refused"
    assert request["refusal_code"] == "OPTION_RUN_ADJUST_IN_FLIGHT"
    assert request["refusal_detail"]["option_run_id"] == run_id
    assert request["refusal_detail"]["option_run_status"] == "adjusting"


def test_an_option_adjust_retry_through_its_own_edge_is_admitted(world):
    """``adjusting`` reached through THIS plan's own edge is a retry, not a race."""
    _record_policy(world)
    held_plan = _option_plan(world["factory"], strategy=world["strategy"], digest=HELD_DIGEST)
    run_id = _own_option_run(world, plan_id=held_plan, status="adjusting")
    candidate = _option_adjust_plan(
        world["factory"], strategy=world["strategy"], reference=run_id, generation=1
    )
    _bind_adjust_edge(world, plan_id=candidate, run_id=run_id)

    created = world["service"].create_for_job(
        job=world["job"], plan_id=candidate, idempotency_key="opt-adj-retry-0001", now=NOW
    )

    assert created["request"]["status"] == "awaiting_approval"
    assert created["request"]["refusal_code"] is None


def test_manual_request_is_idempotent_and_conflicts_on_changed_content(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    first = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0002", now=NOW
    )
    replay = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0002", now=NOW
    )
    assert replay["idempotent"] is True
    assert replay["request"]["request_id"] == first["request"]["request_id"]

    # The identity is per (owner, plan, key) - the brief's keying - so the same
    # key on a DIFFERENT plan is a different request...
    other_plan_id, _hash2 = _plan(world["factory"], strategy=world["strategy"], quantity=7)
    other = world["service"].create_for_job(
        job=world["job"], plan_id=other_plan_id, idempotency_key="exec-key-0002", now=NOW
    )
    assert other["idempotent"] is False
    assert other["request"]["request_id"] != first["request"]["request_id"]

    # ...while the SAME key on the SAME plan with changed content (the strategy
    # moved to autonomous in between) is a conflict, never a silent reuse.
    _record_policy(world)
    _autonomous(world, key="grant-key-0002")
    with pytest.raises(ExecutionRequestConflict):
        world["service"].create_for_job(
            job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0002", now=NOW
        )


def test_owner_approval_queues_one_dispatch_and_is_not_repeatable(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0003", now=NOW
    )
    request_id = created["request"]["request_id"]
    approved = world["service"].approve(
        request_id, owner_id=OWNER, strategy_id=world["strategy"].id, actor=OWNER, now=NOW
    )
    assert approved["approved"] is True
    assert approved["request"]["status"] == "queued"
    assert approved["request"]["decision_kind"] == "manual"
    assert approved["request"]["decision_actor"] == OWNER

    # The decision is durable, so a retry of the HTTP call cannot lose it.
    with pytest.raises(ExecutionRequestStateError):
        world["service"].approve(
            request_id, owner_id=OWNER, strategy_id=world["strategy"].id, actor=OWNER, now=NOW
        )

    claims = world["service"].claim_next(limit=10, now=NOW)
    assert len(claims) == 1
    assert claims[0]["request_id"] == request_id
    assert claims[0]["status"] == "dispatching"


def test_owner_rejection_is_terminal(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0004", now=NOW
    )
    request_id = created["request"]["request_id"]
    rejected = world["service"].reject(
        request_id,
        owner_id=OWNER,
        strategy_id=world["strategy"].id,
        actor=OWNER,
        reason="size too large",
        now=NOW,
    )
    assert rejected["rejected"] is True
    assert rejected["request"]["status"] == "rejected"
    assert rejected["request"]["refusal_code"] == "OWNER_REJECTED"
    assert world["service"].claim_next(limit=10, now=NOW) == []
    assert world["executor"].calls == []
    with pytest.raises(ExecutionRequestStateError):
        world["service"].approve(
            request_id, owner_id=OWNER, strategy_id=world["strategy"].id, actor=OWNER, now=NOW
        )


def test_owner_decision_requires_the_owning_scope(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0005", now=NOW
    )
    from backend.strategies.execution_requests import ExecutionRequestNotFound

    with pytest.raises(ExecutionRequestNotFound):
        world["service"].approve(
            created["request"]["request_id"],
            owner_id="app:someone-else",
            strategy_id=world["strategy"].id,
            actor="app:someone-else",
            now=NOW,
        )


# ---------------------------------------------------------------------------
# autonomous (grant-bound) requests
# ---------------------------------------------------------------------------


def test_autonomous_request_without_a_grant_is_refused_by_name(world):
    world["authorization"].set_mode(OWNER, world["strategy"].id, "autonomous", actor=OWNER)
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0010", now=NOW
    )
    request = created["request"]
    assert request["status"] == "refused"
    assert request["refusal_code"] == "GRANT_REQUIRED"
    assert world["executor"].calls == []
    assert world["service"].claim_next(limit=10, now=NOW) == []


def test_autonomous_request_under_a_matching_grant_executes_once(world):
    _record_policy(world)
    grant = _autonomous(world, key="grant-key-10")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0011", now=NOW
    )
    request = created["request"]
    assert request["status"] == "queued"
    assert request["grant_id"] == grant["grant_id"]
    assert request["decision_kind"] == "automatic"
    assert request["decision_actor"] == OWNER
    assert request["decision_evidence"]["grant_id"] == grant["grant_id"]

    claims = world["service"].claim_next(limit=10, now=NOW)
    assert [row["request_id"] for row in claims] == [request["request_id"]]
    import asyncio

    outcome = asyncio.run(
        world["service"].dispatch(request["request_id"], now=NOW)
    )
    assert outcome["status"] == "executed"
    assert world["executor"].calls == [{"plan_id": plan_id, "actor": OWNER}]

    # The authorization was recorded as AUTOMATIC with grant evidence - never as
    # a manual click - and the reservation it consumed is linked.
    approval = world["pipeline"].approvals.active_for_plan(plan_id)
    if approval is not None:  # paper plans are approval-exempt
        assert approval["actor_kind"] == "automatic"
        assert approval["authorization_evidence"]["grant_id"] == grant["grant_id"]
    reservation = world["pipeline"].reservation_for_plan(plan_id)
    assert reservation is not None
    assert reservation["execution_environment"] == "paper"
    assert outcome["reservation_id"] == reservation["reservation_id"]
    assert "executed" in _audit_events(world)


# ---------------------------------------------------------------------------
# the REAL paper executor over a fake provider boundary
# ---------------------------------------------------------------------------


class FakePaperProvider:
    """The paper RUNTIME boundary only: it records orders, it decides nothing.

    Everything above it - admission, reservation, approval, the plan envelope,
    the per-step claim, the trail - is the production ``PaperPlanExecutor``.
    """

    def __init__(self, results=None) -> None:
        self.orders: list = []
        self._results = list(results or [])

    async def place_order(self, *, account_scope, order_payload, attribution):
        self.orders.append(
            {
                "account_scope": str(account_scope),
                "order": dict(order_payload),
                "attribution": dict(attribution),
            }
        )
        configured = dict(self._results.pop(0)) if self._results else {}
        quantity = int(order_payload.get("quantity") or 0)
        return {
            "status": str(configured.get("status") or "filled"),
            "order": {
                "order_id": str(configured.get("order_id") or f"paper-{len(self.orders)}"),
                "filled_quantity": int(configured.get("filled_quantity", quantity)),
                "average_price": float(configured.get("average_price", 100.0)),
                "tradingsymbol": order_payload.get("tradingsymbol"),
            },
        }


def _use_real_paper_executor(world, *, results=None):
    """Swap the stub executor for the production one over a fake provider."""
    from backend.strategies.execution import PaperPlanExecutor

    provider = FakePaperProvider(results)
    executor = PaperPlanExecutor(session_factory=world["factory"], paper_service=provider)
    pipeline = PlanExecutionPipeline(
        world["factory"],
        proposal_store=ProposalStore(session_factory=world["factory"]),
        admission_service=AdmissionService(session_factory=world["factory"]),
        reservation_ledger=ReservationLedger(session_factory=world["factory"]),
        approval_service=ApprovalService(session_factory=world["factory"]),
        paper_executor_factory=lambda: executor,
    )
    world["paper_provider"] = provider
    world["pipeline"] = pipeline
    world["service"] = ExecutionRequestService(
        world["factory"], pipeline=pipeline, authorization=world["authorization"]
    )
    return provider


def test_real_paper_executor_manual_request_waits_then_submits_exactly_once(world):
    provider = _use_real_paper_executor(world)
    _record_policy(world)
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="real-paper-0001", now=NOW
    )
    assert created["request"]["status"] == "awaiting_approval"
    # Waiting is not a submission: the production executor never ran.
    assert provider.orders == []
    assert _trail_events(world, plan_id) == []

    world["service"].approve(
        created["request"]["request_id"],
        owner_id=OWNER,
        strategy_id=world["strategy"].id,
        actor=OWNER,
        now=NOW,
    )
    claims = world["service"].claim_next(limit=10, now=NOW)
    assert [row["request_id"] for row in claims] == [created["request"]["request_id"]]
    outcome = _dispatch(world["service"], created["request"]["request_id"])
    assert outcome["status"] == "executed"
    assert [order["order"]["tradingsymbol"] for order in provider.orders] == ["INFY"]
    assert provider.orders[0]["order"]["transaction_type"] == "BUY"
    assert provider.orders[0]["order"]["quantity"] == 5
    assert provider.orders[0]["attribution"]["plan_id"] == plan_id
    events = _trail_events(world, plan_id)
    assert "submitted" in events and "filled" in events
    # A dispatched request is not replayable: no second physical submission.
    assert world["service"].claim_next(limit=10, now=NOW) == []
    assert len(provider.orders) == 1


def test_real_paper_executor_autonomous_request_executes_under_matching_grant(world):
    provider = _use_real_paper_executor(world)
    _record_policy(world)
    grant = _autonomous(world, key="grant-key-40")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="real-paper-0002", now=NOW
    )
    assert created["request"]["status"] == "queued"
    assert created["request"]["decision_kind"] == "automatic"
    assert created["request"]["grant_id"] == grant["grant_id"]
    assert provider.orders == []

    claims = world["service"].claim_next(limit=10, now=NOW)
    assert len(claims) == 1
    outcome = _dispatch(world["service"], created["request"]["request_id"])
    assert outcome["status"] == "executed"
    assert len(provider.orders) == 1
    assert "filled" in _trail_events(world, plan_id)


class _CannedExecutor:
    """The executor boundary returning exactly one canned report."""

    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list = []

    async def execute(self, plan, *, actor):
        self.calls.append(str(plan.get("plan_id")))
        if self.error is not None:
            raise self.error
        return dict(self.result or {})


def _dispatch_with_executor(world, executor, *, key):
    pipeline = PlanExecutionPipeline(
        world["factory"],
        proposal_store=ProposalStore(session_factory=world["factory"]),
        admission_service=AdmissionService(session_factory=world["factory"]),
        reservation_ledger=ReservationLedger(session_factory=world["factory"]),
        approval_service=ApprovalService(session_factory=world["factory"]),
        paper_executor_factory=lambda: executor,
    )
    service = ExecutionRequestService(
        world["factory"], pipeline=pipeline, authorization=world["authorization"]
    )
    _record_policy(world)
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = service.create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key=key, now=NOW
    )
    service.approve(
        created["request"]["request_id"],
        owner_id=OWNER,
        strategy_id=world["strategy"].id,
        actor=OWNER,
        now=NOW,
    )
    service.claim_next(limit=5, now=NOW)
    outcome = _dispatch(service, created["request"]["request_id"])
    return service, outcome


@pytest.mark.parametrize(
    "result, expected_status, expected_outcome, expected_code",
    [
        ({"status": "submitted"}, "executed", "submitted", None),
        ({"status": "accepted"}, "executed", "accepted", None),
        ({"status": "filled"}, "executed", "filled", None),
        ({"status": "partial"}, "executed", "partial", None),
        (
            {"status": "partial", "steps": [{"state": "partially_filled"}]},
            "executed",
            "partial",
            None,
        ),
        ({"status": "rejected"}, "refused", "rejected", "ORDER_REJECTED"),
        ({"status": "no_op"}, "refused", "no_op", "NO_OP"),
        ({"status": "uncertain"}, "dispatch_unresolved", "uncertain", "TRANSPORT_UNCERTAIN"),
        ({"status": "failed"}, "dispatch_unresolved", "failed", "EXECUTION_OUTCOME_UNKNOWN"),
    ],
)
def test_dispatch_records_the_executors_own_outcome_not_a_blanket_executed(
    world, result, expected_status, expected_outcome, expected_code
):
    """A dispatch is not a fill: the executor's own word travels with the request."""
    _service, outcome = _dispatch_with_executor(
        world, _CannedExecutor(result=result), key=f"outcome-{expected_outcome}-{expected_status}"
    )
    assert outcome["status"] == expected_status, (outcome, result)
    assert outcome["refusal_code"] == expected_code, (outcome, result)
    assert outcome["execution_detail"]["outcome_state"] == expected_outcome, outcome
    assert outcome["terminal"] is True


def test_dispatch_of_a_transport_failure_is_unresolved_and_never_replayed(world):
    _service, outcome = _dispatch_with_executor(
        world, _CannedExecutor(error=RuntimeError("socket closed")), key="outcome-error"
    )
    assert outcome["status"] == "dispatch_unresolved", outcome
    assert outcome["refusal_code"] == "EXECUTION_OUTCOME_UNKNOWN", outcome
    assert outcome["executable"] is False


def test_next_action_never_claims_a_fill_for_a_bare_dispatch(world):
    """``executed`` means dispatched; the API must not say the trade finished."""
    from backend.api.routers.worker_executions import _request_response

    _service, submitted = _dispatch_with_executor(
        world, _CannedExecutor(result={"status": "submitted"}), key="next-action-submitted"
    )
    response = _request_response(submitted)
    assert response["outcome_state"] == "submitted"
    assert "dispatched and submitted" in response["next_action"]
    assert "every step is filled" not in response["next_action"]

    _service2, filled = _dispatch_with_executor(
        world, _CannedExecutor(result={"status": "filled"}), key="next-action-filled"
    )
    assert "every step is filled" in _request_response(filled)["next_action"]


def test_changed_version_mode_policy_or_grant_state_refuses(world):
    _record_policy(world)
    # A grant issued for a DIFFERENT immutable version never authorises this job.
    wrong_version_grant = _autonomous(world, version=world["version2"], key="grant-key-11")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0012", now=NOW
    )
    assert created["request"]["status"] == "refused"
    assert created["request"]["refusal_code"] == "GRANT_VERSION_MISMATCH"
    assert wrong_version_grant["grant_id"] != created["request"]["grant_id"]


def test_changed_policy_after_queueing_refuses_the_claim(world):
    _record_policy(world)
    _autonomous(world, key="grant-key-12")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0013", now=NOW
    )
    assert created["request"]["status"] == "queued"

    # ANY policy change invalidates the grant - a tightening is not a subset.
    _record_policy(world, allocation=100000.0, daily_loss_budget_inr=1000.0)
    assert world["service"].claim_next(limit=10, now=NOW) == []
    row = world["service"].get(created["request"]["request_id"])
    assert row["status"] == "refused"
    assert row["refusal_code"] == "GRANT_POLICY_CHANGED"
    assert world["executor"].calls == []


def test_mode_change_after_queueing_refuses_the_claim(world):
    _record_policy(world)
    _autonomous(world, key="grant-key-13")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0014", now=NOW
    )
    world["authorization"].set_mode(
        OWNER, world["strategy"].id, "approval_based", actor=OWNER, reason="review again"
    )
    assert world["service"].claim_next(limit=10, now=NOW) == []
    row = world["service"].get(created["request"]["request_id"])
    assert row["status"] == "refused"
    assert row["refusal_code"] == "AUTHORIZATION_MODE_NOT_AUTONOMOUS"


def test_revoked_and_expired_grants_refuse(world):
    _record_policy(world)
    grant = _autonomous(world, key="grant-key-14", now=NOW)
    world["authorization"].revoke_grant(
        OWNER, world["strategy"].id, actor=OWNER, reason="stop", grant_id=grant["grant_id"]
    )
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    revoked = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0015", now=NOW
    )
    assert revoked["request"]["refusal_code"] == "GRANT_REVOKED"

    # An expired grant is refused the same way, at the moment of use.
    # It is issued a second later so the grant that governs the later request is
    # unambiguously the newer one (``_latest_grant`` orders by creation time).
    expiring = world["authorization"].issue_grant(
        OWNER,
        world["strategy"].id,
        actor=OWNER,
        idempotency_key="grant-key-15",
        version_id=world["version"].id,
        execution_environment="paper",
        expires_at=NOW + timedelta(minutes=10),
        now=NOW + timedelta(seconds=1),
    )
    plan_id2, _hash2 = _plan(world["factory"], strategy=world["strategy"], quantity=3)
    expired = world["service"].create_for_job(
        job=world["job"],
        plan_id=plan_id2,
        idempotency_key="exec-key-0016",
        now=NOW + timedelta(hours=1),
    )
    assert expired["request"]["refusal_code"] == "GRANT_EXPIRED"
    assert expiring["grant"]["status"] == "active"


def test_fenced_or_expired_attempt_refuses_the_claim(world):
    _record_policy(world)
    _autonomous(world, key="grant-key-16")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0017", now=NOW
    )
    assert created["request"]["status"] == "queued"

    with world["factory"]() as session:
        job = session.execute(
            select(StrategyJob).where(StrategyJob.id == world["job"].id)
        ).scalar_one()
        job.status = "recovery_required"
        session.commit()
    assert world["service"].claim_next(limit=10, now=NOW) == []
    row = world["service"].get(created["request"]["request_id"])
    assert row["status"] == "refused"
    assert row["refusal_code"] == "HOSTED_ATTEMPT_FENCED"
    assert world["executor"].calls == []


# ---------------------------------------------------------------------------
# claims, duplicate protection and recovery
# ---------------------------------------------------------------------------


def test_a_claim_is_single_and_a_second_dispatch_never_duplicates(world):
    _record_policy(world)
    _autonomous(world, key="grant-key-20")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0020", now=NOW
    )
    request_id = created["request"]["request_id"]

    first = world["service"].claim_next(limit=10, now=NOW)
    second = world["service"].claim_next(limit=10, now=NOW)
    assert len(first) == 1 and second == []
    # The claim is not broker acceptance; it is a durable claim identity.
    assert first[0]["dispatch_claim_id"]

    import asyncio

    executed = asyncio.run(
        world["service"].dispatch(request_id, now=NOW)
    )
    assert executed["status"] == "executed"
    with pytest.raises(ExecutionRequestStateError):
        asyncio.run(
            world["service"].dispatch(request_id, now=NOW)
        )
    assert len(world["executor"].calls) == 1

    # A DIFFERENT request for the SAME plan cannot produce a second submission:
    # the executor's own once-per-plan rule stays authoritative.
    other = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0021", now=NOW
    )
    assert other["request"]["status"] == "queued"
    claimed = world["service"].claim_next(limit=10, now=NOW)
    assert [row["request_id"] for row in claimed] == [other["request"]["request_id"]]
    refused = asyncio.run(
        world["service"].dispatch(other["request"]["request_id"], now=NOW)
    )
    assert refused["status"] == "refused"
    assert refused["refusal_code"] == "PLAN_ALREADY_EXECUTED"
    assert len(world["executor"].calls) == 2
    assert len(world["executor"].executed) == 1


def test_recovery_resolves_proven_claims_and_never_replays_unknown_ones(world):
    _record_policy(world)
    _autonomous(world, key="grant-key-21")
    proved_plan, _h1 = _plan(world["factory"], strategy=world["strategy"], quantity=4)
    unknown_plan, _h2 = _plan(world["factory"], strategy=world["strategy"], quantity=6)
    proved = world["service"].create_for_job(
        job=world["job"], plan_id=proved_plan, idempotency_key="exec-key-0022", now=NOW
    )
    unknown = world["service"].create_for_job(
        job=world["job"], plan_id=unknown_plan, idempotency_key="exec-key-0023", now=NOW
    )
    claimed = world["service"].claim_next(limit=10, now=NOW)
    assert len(claimed) == 2

    # One claim left a durable pre-send record; the other left nothing at all.
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=proved_plan,
                step_no=0,
                event="submitted",
                actor_id=OWNER,
                detail={},
            )
        )
        session.commit()

    counts = world["service"].recover_abandoned_claims(
        timeout_seconds=60, now=NOW + timedelta(hours=1)
    )
    assert counts == {
        "scanned": 2,
        "proved_submitted": 1,
        "proved_rejected": 0,
        "unresolved": 1,
        "stale": 0,
    }
    assert world["service"].get(proved["request"]["request_id"])["status"] == "executed"
    unresolved = world["service"].get(unknown["request"]["request_id"])
    assert unresolved["status"] == "dispatch_unresolved"
    assert unresolved["refusal_code"] == "DISPATCH_OUTCOME_UNKNOWN"
    assert world["service"].claim_next(limit=10, now=NOW + timedelta(hours=2)) == []
    assert world["executor"].calls == []


def test_recovery_distinguishes_withheld_acceptance_rejection_and_unknown(world):
    """A claim is not broker acceptance, and neither is a withheld step.

    Four abandoned claims, four honest answers:

    * nothing but a ``withheld`` live claim (the row exists BEFORE the send) ->
      unresolved, and the detail names "not submitted" instead of claiming a
      submission happened;
    * a live claim holding an accepted broker order id -> ``executed``;
    * an AUTHORITATIVE post-send rejection carrying broker evidence -> ``refused``
      with no retry;
    * a ``releasing`` claim with no outcome recorded -> unresolved (the outcome
      is genuinely unknown and is never replayed).
    """
    _record_policy(world)
    _autonomous(world, key="grant-key-22")
    plans = {
        name: _plan(world["factory"], strategy=world["strategy"], quantity=quantity)[0]
        for name, quantity in (
            ("withheld", 2),
            ("accepted", 3),
            ("rejected", 4),
            ("uncertain", 5),
        )
    }
    requests = {
        name: world["service"].create_for_job(
            job=world["job"], plan_id=plan_id, idempotency_key=f"exec-key-22-{name}", now=NOW
        )
        for name, plan_id in plans.items()
    }
    claimed = world["service"].claim_next(limit=10, now=NOW)
    assert len(claimed) == 4

    def _claim(name: str, **values: object) -> None:
        with world["factory"]() as session:
            session.add(
                LivePlanSubmission(
                    submission_id=str(uuid.uuid4()),
                    plan_id=plans[name],
                    step_no=1,
                    step_ref=f"plan:{plans[name]}:step:1",
                    strategy_id=str(world["strategy"].id),
                    account_id=ACCOUNT,
                    execution_environment="paper",
                    delta_snapshot={"quantity": 1},
                    detail={},
                    **values,
                )
            )
            session.commit()

    # 1. Materialized before the send, never updated: no submission is proven.
    _claim("withheld", state="withheld")
    # 2. Accepted: the broker order id is unconditional proof of a submission.
    _claim("accepted", state="pending", broker_order_ids=["240001"])
    # 3. Authoritative post-send rejection, with broker evidence in the trail.
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plans["rejected"],
                step_no=1,
                event="rejected",
                refusal_reason="BROKER_REJECTED",
                actor_id=OWNER,
                detail={"rejection_code": "RMS:Margin", "message": "insufficient margin"},
            )
        )
        session.commit()
    # 4. Sent, no outcome recorded: the send may have reached the broker.
    _claim("uncertain", state="releasing")

    counts = world["service"].recover_abandoned_claims(
        timeout_seconds=60, now=NOW + timedelta(hours=1)
    )
    assert counts == {
        "scanned": 4,
        "proved_submitted": 1,
        "proved_rejected": 1,
        "unresolved": 2,
        "stale": 0,
    }

    executed = world["service"].get(requests["accepted"]["request"]["request_id"])
    assert executed["status"] == "executed"
    assert executed["execution_detail"]["outcome_state"] == "submitted"

    refused = world["service"].get(requests["rejected"]["request"]["request_id"])
    assert refused["status"] == "refused"
    assert refused["refusal_code"] == "ORDER_REJECTED"
    assert refused["execution_detail"]["outcome_state"] == "rejected"

    withheld = world["service"].get(requests["withheld"]["request"]["request_id"])
    assert withheld["status"] == "dispatch_unresolved"
    assert withheld["refusal_code"] == "DISPATCH_OUTCOME_UNKNOWN"
    assert withheld["execution_detail"]["outcome_state"] == "not_submitted"
    assert withheld["execution_detail"]["evidence"]["unsubmitted_claims"]
    assert "execution_events" not in withheld["execution_detail"]["evidence"]

    uncertain = world["service"].get(requests["uncertain"]["request"]["request_id"])
    assert uncertain["status"] == "dispatch_unresolved"
    assert uncertain["execution_detail"]["outcome_state"] == "unknown"

    # Nothing was replayed, and no abandoned claim is claimable again.
    assert world["executor"].calls == []
    assert world["service"].claim_next(limit=10, now=NOW + timedelta(hours=2)) == []


def test_no_operation_events_never_prove_a_submission(world):
    """``no_op`` means the book already sat at the target - nothing was sent."""
    _record_policy(world)
    _autonomous(world, key="grant-key-23")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"], quantity=7)
    request = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0024", now=NOW
    )
    world["service"].claim_next(limit=10, now=NOW)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="no_op",
                actor_id=OWNER,
                detail={"message": "the attributed book already sits at the target"},
            )
        )
        session.commit()

    counts = world["service"].recover_abandoned_claims(
        timeout_seconds=60, now=NOW + timedelta(hours=1)
    )
    assert counts["proved_submitted"] == 0
    assert counts["unresolved"] == 1
    row = world["service"].get(request["request"]["request_id"])
    assert row["status"] == "dispatch_unresolved"
    assert row["execution_detail"]["outcome_state"] == "unknown"
    assert row["execution_detail"]["evidence"]["non_proof_events"]


def test_pre_send_rejected_event_is_not_broker_acceptance(world):
    """A bare ``rejected`` event with no broker evidence is a pre-send refusal."""
    _record_policy(world)
    _autonomous(world, key="grant-key-24")
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"], quantity=8)
    request = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0025", now=NOW
    )
    world["service"].claim_next(limit=10, now=NOW)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="rejected",
                refusal_reason="RESERVATION_EXPIRED",
                actor_id=OWNER,
                detail={"plan_id": plan_id},
            )
        )
        session.commit()

    counts = world["service"].recover_abandoned_claims(
        timeout_seconds=60, now=NOW + timedelta(hours=1)
    )
    # It is neither a submission nor an authoritative broker rejection.
    assert counts["proved_submitted"] == 0
    assert counts["proved_rejected"] == 0
    assert counts["unresolved"] == 1
    row = world["service"].get(request["request"]["request_id"])
    assert row["status"] == "dispatch_unresolved"
    assert row["execution_detail"]["evidence"]["non_proof_events"]


# ---------------------------------------------------------------------------
# dependent releases
# ---------------------------------------------------------------------------


def test_dependent_release_rechecks_autonomous_authority(world):
    _record_policy(world)
    grant = _autonomous(world, key="grant-key-30")
    plan_id, plan_hash = _plan(world["factory"], strategy=world["strategy"])
    plan = world["pipeline"].plan(plan_id)
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0030", now=NOW
    )
    import asyncio

    world["service"].claim_next(limit=10, now=NOW)
    asyncio.run(
        world["service"].dispatch(created["request"]["request_id"], now=NOW)
    )
    assert world["service"].authorize_dependent_release(plan, now=NOW) is None

    # A revocation after the initial request blocks the LATER dependent release.
    world["authorization"].revoke_grant(
        OWNER, world["strategy"].id, actor=OWNER, grant_id=grant["grant_id"]
    )
    refusal = world["service"].authorize_dependent_release(plan, now=NOW)
    assert refusal is not None
    assert refusal["reason_code"] == "GRANT_REVOKED"


def test_dependent_release_of_an_approval_based_plan_is_unaffected(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    plan = world["pipeline"].plan(plan_id)
    assert world["service"].authorize_dependent_release(plan, now=NOW) is None


# ---------------------------------------------------------------------------
# the owned-work snapshot
# ---------------------------------------------------------------------------


def _snapshot(world):
    return OwnedWorkSnapshotService(world["factory"]).snapshot(
        strategy_id=str(world["strategy"].id),
        account_id=ACCOUNT,
        execution_environment="paper",
        strategy_run_id=RUN_ID,
        now=NOW,
    )


def test_snapshot_reports_unknown_coverage_rather_than_a_flat_book(world):
    snapshot = _snapshot(world)
    assert snapshot["coverage"] == "unknown"
    assert snapshot["projection"]["published"] is False
    assert snapshot["projection"]["coverage"] == "unpublished_unknown"
    assert snapshot["positions"] == []
    assert any("unknown" in note for note in snapshot["notes"])


def test_snapshot_shows_owned_positions_and_pending_work_without_double_counting(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    with world["factory"]() as session:
        session.add(
            StrategyPositionProjection(
                account_id=ACCOUNT,
                strategy_id=str(world["strategy"].id),
                execution_environment="paper",
                identity_kind="canonical",
                identity_key="i1",
                product="CNC",
                canonical_instrument_id="i1",
                instrument_token=408065,
                exchange="NSE",
                tradingsymbol="INFY",
                net_quantity=5,
                projection_version=1,
            )
        )
        session.add(
            StrategyProjectionState(
                account_id=ACCOUNT,
                strategy_id=str(world["strategy"].id),
                execution_environment="paper",
                projection_version=1,
                content_sha256="c" * 64,
                last_rebuild_at=NOW - timedelta(seconds=30),
            )
        )
        session.commit()

    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="exec-key-0040", now=NOW
    )
    snapshot = _snapshot(world)
    # The projection itself is published AND fresh, so the positions lens is
    # known. The overall coverage is still unknown because the pending request
    # row carries no quantity evidence, and the contract says unknown pending
    # work cannot be reported as a complete picture.
    assert snapshot["projection"]["published"] is True
    assert snapshot["projection"]["fresh"] is True
    assert snapshot["projection"]["age_seconds"] == 30.0
    assert snapshot["coverage"] == "unknown"
    assert any("pending unit" in note for note in snapshot["notes"])
    assert snapshot["projection"]["projection_version"] == 1
    assert [row["tradingsymbol"] for row in snapshot["positions"]] == ["INFY"]
    pending = [row for row in snapshot["pending"] if row["source"] == "execution_request"]
    assert [row["plan_id"] for row in pending] == [plan_id]
    assert pending[0]["state"] == "awaiting_approval"
    assert pending[0]["detail"]["request_id"] == created["request"]["request_id"]
    # Pending request rows carry no quantity claim, and never invent one.
    assert pending[0]["remaining_quantity"] is None
    assert pending[0]["coverage"] == "unknown"

    # A submitted paper step reports its requested quantity and remaining work
    # separately from any fill, so a fill is never counted twice.
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 5, "side": "BUY"},
            )
        )
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="partially_filled",
                filled_quantity=2,
                actor_id=OWNER,
                detail={"pending_quantity": 3, "ref": "plan:1"},
            )
        )
        session.commit()
    snapshot = _snapshot(world)
    step = next(row for row in snapshot["pending"] if row["step_no"] == 1)
    assert step["state"] == "partially_filled"
    assert step["submitted_quantity"] == 5.0
    assert step["filled_quantity"] == 2.0
    assert step["remaining_quantity"] == 3.0
    assert step["coverage"] == "known"


def test_snapshot_coverage_is_known_when_every_source_has_evidence(world):
    """A fully-evidenced picture is the only one reported as ``known``.

    The pending request row is what makes overall coverage unknown even when the
    projection is fresh: it is work with no quantity claim. Rejecting it (a
    terminal decision) removes the unknown pending unit, and a submitted step
    that reports its own quantities keeps coverage known.
    """
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    with world["factory"]() as session:
        session.add(
            StrategyProjectionState(
                account_id=ACCOUNT,
                strategy_id=str(world["strategy"].id),
                execution_environment="paper",
                projection_version=1,
                content_sha256="c" * 64,
                last_rebuild_at=NOW - timedelta(seconds=5),
            )
        )
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=0,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 4, "side": "BUY"},
            )
        )
        session.commit()

    snapshot = _snapshot(world)
    assert snapshot["coverage"] == "known"
    step = next(row for row in snapshot["pending"] if row["step_no"] == 0)
    assert step["state"] == "submitted"
    assert step["remaining_quantity"] == 4.0
    assert step["coverage"] == "known"


def test_snapshot_flags_a_stale_projection_as_unknown(world):
    """A published projection past its freshness bound is not trusted as current."""
    with world["factory"]() as session:
        session.add(
            StrategyProjectionState(
                account_id=ACCOUNT,
                strategy_id=str(world["strategy"].id),
                execution_environment="paper",
                projection_version=1,
                content_sha256="c" * 64,
                last_rebuild_at=NOW - timedelta(hours=4),
            )
        )
        session.commit()

    snapshot = _snapshot(world)
    assert snapshot["projection"]["published"] is True
    assert snapshot["projection"]["fresh"] is False
    assert snapshot["coverage"] == "unknown"
    assert any("freshness" in note for note in snapshot["notes"])


def _live_binding(factory, *, strategy, run_id, environment="live", account=ACCOUNT):
    with factory() as session:
        session.add(
            StrategyRunBinding(
                strategy_run_id=str(run_id),
                strategy_id=str(strategy.id),
                owner_id=OWNER,
                account_id=str(account),
                execution_environment=str(environment),
                bound_by="test",
                binding_source="hosted_job",
            )
        )
        session.commit()


def _live_claim(
    factory,
    *,
    strategy,
    plan_id,
    step_no,
    state,
    account=ACCOUNT,
    environment="paper",
    delta=None,
    detail=None,
):
    with factory() as session:
        session.add(
            LivePlanSubmission(
                submission_id=str(uuid.uuid4()),
                plan_id=str(plan_id),
                step_no=int(step_no),
                step_ref=f"plan:{plan_id}:{int(step_no)}",
                strategy_id=str(strategy.id),
                account_id=str(account),
                execution_environment=str(environment),
                state=str(state),
                broker_order_ids=[],
                delta_snapshot=dict(delta or {}),
                detail=dict(detail or {}),
            )
        )
        session.commit()


def _snapshot_env(world, *, environment, run_id):
    return OwnedWorkSnapshotService(world["factory"]).snapshot(
        strategy_id=str(world["strategy"].id),
        account_id=ACCOUNT,
        execution_environment=environment,
        strategy_run_id=run_id,
        now=NOW,
    )


def test_snapshot_separates_paper_and_live_and_never_relabels(world):
    """Same strategy, two environments: each snapshot shows its OWN book only."""
    paper_plan, _h1 = _plan(world["factory"], strategy=world["strategy"], quantity=5)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=paper_plan,
                step_no=1,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 5, "side": "BUY"},
            )
        )
        session.commit()

    live_run = "run_hosted_live"
    _live_binding(world["factory"], strategy=world["strategy"], run_id=live_run)
    live_plan, _h2 = _plan(
        world["factory"], strategy=world["strategy"], run_id=live_run, quantity=9
    )
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=live_plan,
        step_no=1,
        state="withheld",
        environment="live",
        delta={"quantity": 9, "side": "BUY"},
    )

    paper = _snapshot_env(world, environment="paper", run_id=RUN_ID)
    assert [row["plan_id"] for row in paper["pending"]] == [paper_plan]
    assert {row["execution_environment"] for row in paper["pending"]} == {"paper"}

    live = _snapshot_env(world, environment="live", run_id=live_run)
    assert [row["plan_id"] for row in live["pending"]] == [live_plan]
    # The row reports its OWN environment, never the one that was asked for.
    assert {row["execution_environment"] for row in live["pending"]} == {"live"}
    withheld = live["pending"][0]
    assert withheld["state"] == "withheld"
    assert withheld["remaining_quantity"] == 9.0
    assert withheld["product"] == "CNC"
    assert withheld["side"] == "BUY"
    assert withheld["tradingsymbol"] == "INFY"


def test_snapshot_merges_a_trail_event_and_a_live_claim_into_one_order(world):
    """One step is ONE unit of work, even when both sources describe it."""
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"], quantity=5)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 5, "side": "BUY"},
            )
        )
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="partially_filled",
                filled_quantity=2,
                actor_id=OWNER,
                detail={"pending_quantity": 3, "side": "BUY"},
            )
        )
        session.commit()
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=plan_id,
        step_no=1,
        state="partial",
        delta={"quantity": 5, "side": "BUY"},
        detail={"filled_quantity": 2, "remaining_quantity": 3},
    )

    snapshot = _snapshot_env(world, environment="paper", run_id=RUN_ID)
    steps = [row for row in snapshot["pending"] if row["step_no"] == 1]
    assert len(steps) == 1, snapshot["pending"]
    row = steps[0]
    assert row["sources"] == ["live_submission", "plan_execution"]
    assert row["state"] == "partial"
    assert row["submitted_quantity"] == 5.0
    assert row["filled_quantity"] == 2.0
    assert row["remaining_quantity"] == 3.0
    assert row["coverage"] == "known"


def test_snapshot_treats_a_source_disagreement_as_unknown(world):
    """When the trail and the live claim disagree, neither is picked as truth."""
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"], quantity=5)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 5, "side": "BUY"},
            )
        )
        session.commit()
    # The claim says 3 remain; the trail still says all 5 are outstanding.
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=plan_id,
        step_no=1,
        state="partial",
        delta={"quantity": 5, "side": "BUY"},
        detail={"filled_quantity": 2, "remaining_quantity": 3},
    )

    snapshot = _snapshot_env(world, environment="paper", run_id=RUN_ID)
    row = next(row for row in snapshot["pending"] if row["step_no"] == 1)
    assert row["remaining_quantity"] is None
    assert row["filled_quantity"] is None
    assert row["coverage"] == "unknown"
    assert row["confidence"]["disagreement"]["live_claim_remaining"] == 3.0
    assert snapshot["coverage"] == "unknown"


def test_snapshot_reports_partial_withheld_and_unknown_units(world):
    """The three states a real plan produces: pending quantities, a held leg, and
    a unit whose quantity the platform cannot prove. Overall coverage is unknown
    as soon as ANY unit is unknown."""
    live_run = "run_hosted_live"
    _live_binding(world["factory"], strategy=world["strategy"], run_id=live_run)
    plan_id, _h = _plan(
        world["factory"], strategy=world["strategy"], run_id=live_run, quantity=4
    )
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=plan_id,
        step_no=1,
        state="partial",
        environment="live",
        delta={"quantity": 4},
        detail={"filled_quantity": 1, "remaining_quantity": 3},
    )
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=plan_id,
        step_no=2,
        state="withheld",
        environment="live",
        delta={"quantity": 6},
    )
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=3,
                event="submitted",
                actor_id=OWNER,
                detail={"side": "SELL"},
            )
        )
        session.commit()

    live = _snapshot_env(world, environment="live", run_id=live_run)
    by_step = {int(row["step_no"]): row for row in live["pending"]}
    assert by_step[1]["remaining_quantity"] == 3.0
    assert by_step[2]["state"] == "withheld"
    assert by_step[2]["remaining_quantity"] == 6.0
    assert by_step[3]["coverage"] == "unknown"
    assert live["coverage"] == "unknown"


def test_snapshot_lets_a_pending_aware_decision_avoid_a_duplicate(world):
    """The contract's adjustment example, expressed over the snapshot.

    A strategy that owns 5 of a target 8, with 3 already submitted and still
    outstanding, must NOT submit the same 3 again on its next observation.
    """
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"], quantity=3)
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="submitted",
                actor_id=OWNER,
                detail={"quantity": 3, "side": "BUY"},
            )
        )
        session.commit()

    snapshot = _snapshot_env(world, environment="paper", run_id=RUN_ID)

    def _outstanding_buy(tradingsymbol: str) -> float:
        return sum(
            float(row["remaining_quantity"] or 0.0)
            for row in snapshot["pending"]
            if row["tradingsymbol"] == tradingsymbol
            and str(row["side"] or "").upper() == "BUY"
            and row["coverage"] == "known"
        )

    target, owned = 8.0, 5.0
    missing = max(0.0, target - owned)
    assert missing == 3.0
    # The pending work already covers the gap, so the next decision is a no-op.
    assert missing - _outstanding_buy("INFY") == 0.0

    # Once the step settles (no pending row), the same gap WOULD be actionable -
    # the snapshot is what distinguishes "already asked for" from "not yet".
    with world["factory"]() as session:
        session.add(
            StrategyPlanExecutionEvent(
                id=str(uuid.uuid4()),
                plan_id=plan_id,
                step_no=1,
                event="filled",
                filled_quantity=3,
                actor_id=OWNER,
                detail={"side": "BUY"},
            )
        )
        session.commit()
    _live_claim(
        world["factory"],
        strategy=world["strategy"],
        plan_id=plan_id,
        step_no=1,
        state="filled",
        delta={"quantity": 3, "side": "BUY"},
        detail={"filled_quantity": 3, "remaining_quantity": 0},
    )
    settled = _snapshot_env(world, environment="paper", run_id=RUN_ID)
    assert all(row["step_no"] != 1 for row in settled["pending"])


# ---------------------------------------------------------------------------
# the worker HTTP boundary
# ---------------------------------------------------------------------------


def _operator_client(world, *, username="admin"):
    """A cookie-authenticated operator client over the same app state."""
    from unittest.mock import patch as _patch

    from backend.app import auth as auth_module
    from backend.app.auth import AppUser

    app = _app(world)
    user = AppUser(username=username, role="admin") if username else None
    patches = [
        _patch.object(auth_module, "get_optional_app_user", lambda _request: user),
        _patch.dict("os.environ", {"HOSTED_STRATEGY_ACCOUNT_SCOPES": ACCOUNT}),
    ]
    for item in patches:
        item.start()
    return app, patches


@pytest.mark.asyncio
async def test_owner_authorization_surface_is_cookie_scoped_and_idempotent(world):
    app, patches = _operator_client(world)
    try:
        async with _client(app) as client:
            strategy_id = str(world["strategy"].id)
            status = await client.get(f"/api/strategies/{strategy_id}/authorization")
            assert status.status_code == 200, status.text
            assert status.json()["authorization_mode"] == "approval_based"
            assert status.json()["active_grant"] is None
            assert "AUTHORIZATION_POLICY_INCOMPLETE" in status.json()["blocking_reasons"]

            autonomous = await client.put(
                f"/api/strategies/{strategy_id}/authorization",
                json={"mode": "autonomous", "reason": "standing mandate"},
            )
            assert autonomous.status_code == 200, autonomous.text
            assert autonomous.json()["changed"] is True

            # A grant cannot be issued without a concrete recorded policy.
            premature = await client.post(
                f"/api/strategies/{strategy_id}/authorization/grants",
                json={
                    "idempotency_key": "owner-grant-key-1",
                    "version_id": str(world["version"].id),
                    "execution_environment": "paper",
                },
            )
            assert premature.status_code == 409, premature.text
            assert (
                premature.json()["detail"]["rejection_reason"]
                == "AUTHORIZATION_POLICY_INCOMPLETE"
            )

            _record_policy(world)
            issued = await client.post(
                f"/api/strategies/{strategy_id}/authorization/grants",
                json={
                    "idempotency_key": "owner-grant-key-1",
                    "version_id": str(world["version"].id),
                    "execution_environment": "paper",
                },
            )
            assert issued.status_code == 200, issued.text
            grant = issued.json()
            assert grant["status"] == "active"
            assert grant["idempotent"] is False
            assert grant["account_id"] == ACCOUNT
            assert grant["source_sha256"] == "a" * 64

            replay = await client.post(
                f"/api/strategies/{strategy_id}/authorization/grants",
                json={
                    "idempotency_key": "owner-grant-key-1",
                    "version_id": str(world["version"].id),
                    "execution_environment": "paper",
                },
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["grant_id"] == grant["grant_id"]
            assert replay.json()["idempotent"] is True

            usable = await client.get(f"/api/strategies/{strategy_id}/authorization")
            assert usable.json()["grant_usable"] is True
            assert usable.json()["active_grant"]["grant_id"] == grant["grant_id"]

            revoked = await client.post(
                f"/api/strategies/{strategy_id}/authorization/grants/revoke",
                json={"grant_id": grant["grant_id"], "reason": "stop"},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["grant"]["status"] == "revoked"

            after = await client.get(f"/api/strategies/{strategy_id}/authorization")
            assert after.json()["active_grant"] is None
            assert after.json()["grant_usable"] is False
            assert "GRANT_REQUIRED" in after.json()["blocking_reasons"]
    finally:
        for item in patches:
            item.stop()


@pytest.mark.asyncio
async def test_owner_decision_routes_and_cross_owner_invisibility(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    created = world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="owner-key-0001", now=NOW
    )
    request_id = created["request"]["request_id"]
    strategy_id = str(world["strategy"].id)

    app, patches = _operator_client(world)
    try:
        async with _client(app) as client:
            listed = await client.get(f"/api/strategies/{strategy_id}/execution-requests")
            assert listed.status_code == 200, listed.text
            assert [row["request_id"] for row in listed.json()["requests"]] == [request_id]

            approved = await client.post(
                f"/api/strategies/{strategy_id}/execution-requests/{request_id}/approve",
                json={},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["approved"] is True
            assert approved.json()["request"]["status"] == "queued"

            # The same request cannot be approved twice.
            again = await client.post(
                f"/api/strategies/{strategy_id}/execution-requests/{request_id}/approve",
                json={},
            )
            assert again.status_code == 409, again.text
    finally:
        for item in patches:
            item.stop()

    # A different owner sees a 404, never a hint that the strategy exists.
    other_app, other_patches = _operator_client(world, username="someone-else")
    try:
        async with _client(other_app) as client:
            hidden = await client.get(f"/api/strategies/{strategy_id}/authorization")
            assert hidden.status_code == 404
    finally:
        for item in other_patches:
            item.stop()


@pytest.mark.asyncio
async def test_child_requests_execution_over_http_and_gets_the_durable_state(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    app = _app(world)
    async with _client(app) as client:
        created = await client.post(
            "/api/algo-workers/worker/executions",
            headers=CHILD_HEADERS,
            json={
                "strategy_run_id": RUN_ID,
                "plan_id": plan_id,
                "idempotency_key": "http-key-0001",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "awaiting_approval"
        assert "owner" in body["next_action"]
        assert body["authorization_mode"] == "approval_based"

        replay = await client.post(
            "/api/algo-workers/worker/executions",
            headers=CHILD_HEADERS,
            json={
                "strategy_run_id": RUN_ID,
                "plan_id": plan_id,
                "idempotency_key": "http-key-0001",
            },
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["request_id"] == body["request_id"]
        assert replay.json()["idempotent"] is True

        listed = await client.get(
            "/api/algo-workers/worker/executions",
            headers=CHILD_HEADERS,
            params={"strategy_run_id": RUN_ID},
        )
        assert listed.status_code == 200, listed.text
        assert [row["request_id"] for row in listed.json()["requests"]] == [
            body["request_id"]
        ]

        detail = await client.get(
            f"/api/algo-workers/worker/executions/{body['request_id']}",
            headers=CHILD_HEADERS,
            params={"strategy_run_id": RUN_ID},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["plan_id"] == plan_id

        # A request id is scoped to the run that owns it: another hosted run of
        # the same token cannot read it.
        elsewhere = await client.get(
            f"/api/algo-workers/worker/executions/{body['request_id']}",
            headers=CHILD_HEADERS,
            params={"strategy_run_id": "run_hosted_2"},
        )
        assert elsewhere.status_code == 404


@pytest.mark.asyncio
async def test_child_cannot_use_the_operator_authorization_surface(world):
    """Grants are an owner (cookie-authenticated) act; a child bearer is not one."""
    app = _app(world)
    async with _client(app) as client:
        for method, path in (
            ("GET", f"/api/strategies/{world['strategy'].id}/authorization"),
            ("PUT", f"/api/strategies/{world['strategy'].id}/authorization"),
            ("POST", f"/api/strategies/{world['strategy'].id}/authorization/grants"),
        ):
            response = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {CHILD_RAW}"},
                json={"mode": "autonomous"},
            )
            assert response.status_code in (401, 403), (method, path, response.text)


@pytest.mark.asyncio
async def test_hosted_raw_mutations_are_refused_and_external_runs_are_unchanged(world):
    class _PaperRuntime:
        def __init__(self) -> None:
            self.calls = []

        async def place_order(self, *, account_scope, order_payload, attribution):
            self.calls.append({"account": account_scope, "order": dict(order_payload)})
            return {"status": "accepted", "order": {"order_id": "paper-1"}}

        async def exit_strategy(self, *, account_scope, strategy_id):
            return {"status": "success"}

    paper = _PaperRuntime()
    app = _app(world)
    app.state.paper_runtime_service = paper

    hosted_body = {
        "intent_type": "place_order",
        "payload": {"order": {"tradingsymbol": "INFY", "quantity": 1}},
        "idempotency_key": "raw-hosted-0001",
    }
    external_body = {**hosted_body, "idempotency_key": "raw-external-0001"}
    async with _client(app) as client:
        hosted = await client.post(
            f"/api/algo-workers/worker/runs/{RUN_ID}/intents",
            headers=CHILD_HEADERS,
            json=hosted_body,
        )
        assert hosted.status_code == 409, hosted.text
        detail = hosted.json()["detail"]
        assert detail["rejection_reason"] == "HOSTED_RAW_MUTATION_FORBIDDEN"
        assert detail["governed_surface"] == "/api/algo-workers/worker/executions"
        hosted_exit = await client.post(
            f"/api/algo-workers/worker/runs/{RUN_ID}/exit",
            headers=CHILD_HEADERS,
            json={"reason": "flatten"},
        )
        assert hosted_exit.status_code == 409, hosted_exit.text
        assert (
            hosted_exit.json()["detail"]["rejection_reason"]
            == "HOSTED_RAW_MUTATION_FORBIDDEN"
        )

        # EXTERNAL runs keep the contract they always had.
        external = await client.post(
            "/api/algo-workers/worker/runs/run_external/intents",
            headers={"Authorization": f"Bearer {EXTERNAL_RAW}"},
            json=external_body,
        )
        assert external.status_code == 200, external.text
        assert external.json()["status"] == "accepted"
        assert [call["account"] for call in paper.calls] == [ACCOUNT]


@pytest.mark.asyncio
async def test_owned_work_snapshot_is_available_over_http(world):
    plan_id, _hash = _plan(world["factory"], strategy=world["strategy"])
    world["service"].create_for_job(
        job=world["job"], plan_id=plan_id, idempotency_key="http-key-0002", now=NOW
    )
    app = _app(world)
    async with _client(app) as client:
        response = await client.get(
            f"/api/algo-workers/worker/runs/{RUN_ID}/positions",
            headers=CHILD_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["strategy_id"] == str(world["strategy"].id)
        assert body["execution_environment"] == "paper"
        assert body["projection"]["coverage"] == "unpublished_unknown"
        assert body["coverage"] == "unknown"
        assert [row["plan_id"] for row in body["pending"]] == [plan_id]

        # An unbound run has no owned book at all: it is a refusal, not empty.
        unbound = await client.get(
            "/api/algo-workers/worker/runs/run_hosted_2/positions",
            headers=CHILD_HEADERS,
        )
        assert unbound.status_code == 403
        assert unbound.json()["detail"]["rejection_reason"] == "AUTHORITY_MISMATCH"


# ---------------------------------------------------------------------------
# weights plans: a stated capital basis is verified, never overwritten
# ---------------------------------------------------------------------------


def _weights_submission(world, *, stated):
    from backend.strategies.proposals import ProposalSubmission

    return ProposalStore(session_factory=world["factory"]).submit(
        ProposalSubmission(
            strategy_id=str(world["strategy"].id),
            account_id=ACCOUNT,
            evaluation_id=f"eval-basis-{uuid.uuid4().hex[:8]}",
            evaluation_kind="run_now",
            strategy_run_id=RUN_ID,
            target_kind="target_weights",
            payload={
                "universe_revision_id": str(uuid.uuid4()),
                "target_weights": {"INFY": 1.0},
                # Stated on purpose: the number the caller believes it is sizing
                # against.
                "capital_basis_inr": stated,
            },
        )
    )


def _weights_ledger_counts(world):
    from sqlalchemy import func

    from backend.strategies.attribution_models import StrategyReservation

    with world["factory"]() as session:
        plans = session.execute(
            select(func.count())
            .select_from(StrategyPlan)
            .where(StrategyPlan.strategy_id == str(world["strategy"].id))
        ).scalar()
        reservations = session.execute(
            select(func.count())
            .select_from(StrategyReservation)
            .where(StrategyReservation.strategy_id == str(world["strategy"].id))
        ).scalar()
    return int(plans or 0), int(reservations or 0)


def test_a_stated_basis_below_or_above_the_allocation_refuses_before_any_work(world):
    """The financial guard: a stated budget that is not the owner's allocation is
    refused by name, in BOTH directions, before a plan, reservation or order
    exists. Silently sizing to the allocation would spend a budget the caller
    never approved."""
    _record_policy(world, allocation=100000.0)
    before = _weights_ledger_counts(world)

    for stated in (40000.0, 500000.0):
        result = _weights_submission(world, stated=stated)
        assert result["status"] == "refused", result
        refusal = result["refusal"]
        assert refusal["rejection_reason"] == "CAPITAL_BASIS_MISMATCH", refusal
        assert refusal["stated_capital_basis_inr"] == stated
        assert refusal["authoritative_allocation_inr"] == 100000.0

    assert before == (0, 0)
    assert _weights_ledger_counts(world) == before
    # Nothing reached the order boundary: the executor was never called.
    assert world["executor"].calls == []


def test_a_non_finite_stated_basis_is_invalid_not_a_sizing_input(world):
    _record_policy(world, allocation=100000.0)

    for stated in (0.0, -1.0, float("nan"), float("inf")):
        result = _weights_submission(world, stated=stated)
        assert result["status"] == "refused", result
        assert result["refusal"]["rejection_reason"] == "CAPITAL_BASIS_INVALID", result

    assert _weights_ledger_counts(world) == (0, 0)
    assert world["executor"].calls == []
