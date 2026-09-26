"""Proposal authority at the worker boundary (G5 + hosted attempt fencing).

Pins the contract this bundle closed:

* ``proposals:submit`` is a real, grantable action — granted by the pinned
  capabilities of a trade-capable hosted child, grantable to an owner-issued
  external token, and absent from every default set;
* a hosted child's proposal passes the same attempt/session fence as every other
  worker mutation, *before* anything is persisted;
* run, job, strategy, account and evaluation identity come from persisted
  authority (the binding and the ``strategy_jobs`` row), never from the payload,
  and a mismatch is refused by name.

The route function is invoked directly with a real Starlette ``Request`` over a
real app state (real ``SqlAttributionStore``, real
``SqlAlchemyStrategyRepository``, real ``SqlAlchemyAlgoWorkerRepository``). Only
the transport is skipped: the ASGI transport stalls in this environment, and the
transport contributes no authority logic. The token row is inserted with raw SQL
because ``_create_token_sync`` uses ``CAST(:x AS JSONB)``, which SQLite coerces
to a number — a pre-existing harness limitation (see
``tests/api/test_worker_run_discovery.py``), not a shortcut around the grant: the
action list itself is produced by the production ``capability_actions``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from fastapi import FastAPI  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from starlette.requests import Request  # noqa: E402

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository  # noqa: E402
from backend.api.routers.worker_proposals import submit_proposal  # noqa: E402
from backend.api.routers.worker_shared import create_worker_run_for_token  # noqa: E402
from backend.api.schemas.proposals import ProposalSubmitRequest  # noqa: E402
from backend.api.schemas.worker import WorkerRunCreateRequest  # noqa: E402
from backend.shared.serialization import _hash_token  # noqa: E402
from backend.strategies import service as strategy_service  # noqa: E402
from backend.strategies.attribution import RunBindingInput, SqlAttributionStore  # noqa: E402
from backend.strategies.proposals import ProposalStore  # noqa: E402
from backend.strategies.repository import SqlAlchemyStrategyRepository  # noqa: E402
from backend.workflows.repository import Base as _Base  # noqa: E402
from tests.support.hosted_fakes import StubJournalService  # noqa: E402

OWNER = "app:admin"
ACCOUNT = "kite:paper"
LEASE_OWNER = "sup-A"

#: The schema the real worker repository reads. These tables have no ORM model
#: (they are Core ``text()`` SQL, always ``public.``-qualified), so the harness
#: declares exactly the columns the repository selects.
_PUBLIC_DDL = (
    """
    CREATE TABLE public.algo_worker_tokens (
        token_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
        account_scope TEXT, allowed_modes TEXT, allowed_actions TEXT,
        allowed_templates TEXT, status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT, created_at TEXT, updated_at TEXT, last_used_at TEXT
    )
    """,
    """
    CREATE TABLE public.algo_worker_runs (
        strategy_run_id TEXT PRIMARY KEY, token_id TEXT NOT NULL, template_id TEXT NOT NULL,
        account_scope TEXT NOT NULL, execution_mode TEXT NOT NULL, status TEXT NOT NULL,
        summary_fields_json TEXT, risk_schema_json TEXT, allowed_actions_json TEXT,
        runtime_state_json TEXT, metadata_json TEXT, worker_session_nonce TEXT,
        worker_session_claimed_at TEXT, last_heartbeat_at TEXT, created_at TEXT,
        updated_at TEXT, closed_at TEXT
    )
    """,
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
)


@dataclass(frozen=True)
class Child:
    """One minted hosted child credential and the attempt it is bound to."""

    label: str
    strategy_id: str
    version_id: str
    job_id: str
    run_id: str
    token_id: str
    raw_token: str
    nonce: str
    occurrence_key: str
    evaluation_id: str
    actions: tuple
    schedule_id: str
    session_date: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request(app, *, token=None, nonce=None):
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if nonce is not None:
        headers.append((b"x-worker-session-nonce", str(nonce).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/algo-workers/worker/proposals",
        "headers": headers,
        "app": app,
    }
    return Request(scope)


def _insert_token(factory, *, token_id, raw_token, actions, template):
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.algo_worker_tokens (token_id, name, token_hash, account_scope,"
                " allowed_modes, allowed_actions, allowed_templates, status)"
                " VALUES (:tid, :name, :hash, :scope, :modes, :actions, :templates, 'active')"
            ),
            {
                "tid": token_id,
                "name": token_id,
                "hash": _hash_token(raw_token),
                "scope": ACCOUNT,
                "modes": json.dumps(["paper"]),
                "actions": json.dumps(sorted(actions)),
                "templates": json.dumps([template]),
            },
        )
        session.commit()


def _seed_public_catalog(factory):
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES ('gen-1', 'published', '2026-09-01T00:00:00+00:00')"
            )
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_records "
                "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                "VALUES ('inst-REL', 'NSE', 'RELIANCE', 'active', 'gen-1')"
            )
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_broker_mappings "
                "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                " valid_from_generation, is_current) "
                "VALUES ('map-REL', 'inst-REL', 'kite', 'NSE', 'RELIANCE', 100, 'gen-1', 1)"
            )
        )
        session.commit()


class Harness:
    """One SQLite world, real repositories, real catalog, cheap minting.

    ``mint`` is deliberately synchronous. Production's async wrappers all
    delegate to these same functions through ``asyncio.to_thread``; this sandbox
    adds ~5s to every cross-thread wakeup, so the tests call the delegated
    functions directly and keep the real async entry point for the one test that
    certifies run creation itself.
    """

    def __init__(self, app, strategies, worker, attribution, factory):
        self.app = app
        self.strategies = strategies
        self.worker = worker
        self.attribution = attribution
        self.factory = factory
        self._counter = [0]

    def mint(
        self,
        label,
        capabilities,
        *,
        validated_run_create=False,
        evaluation_kind="scheduled_occurrence",
        session_date=None,
    ):
        self._counter[0] += 1
        label = f"{label}{self._counter[0]}"
        strategy = self.strategies.create_strategy(
            owner_id=OWNER,
            name=f"proposal-authority-{label}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=ACCOUNT,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        version = self.strategies.create_version(
            strategy_id=strategy.id,
            source="inline",
            source_sha256="a" * 64,
            parameters_schema={"type": "object"},
            capabilities_snapshot={"schema_version": 2, "capabilities": dict(capabilities)},
            created_by=OWNER,
        )
        session_kind = evaluation_kind == "session_occurrence"
        occurrence_key = f"sch-{label}:2026-10-10"
        evaluation_id = (
            f"session:sch-{label}:2026-10-10:0"
            if session_kind
            else f"sched:sch-{label}:2026-10-10"
        )
        identity = {
            "source": "schedule_occurrence",
            "schedule_id": f"sch-{label}",
            "occurrence_key": occurrence_key,
            "evaluation_id": evaluation_id,
            "evaluation_kind": evaluation_kind,
        }
        if session_kind:
            identity.update(
                {
                    "session_date": "2026-10-10",
                    "opens_at": "2026-10-10T03:45:00+00:00",
                    "closes_at": "2026-10-10T10:00:00+00:00",
                    "stop_at": "2026-10-10T09:55:00+00:00",
                }
            )
        job = self.strategies.create_job(
            strategy_id=strategy.id,
            version_id=version.id,
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            params={},
            occurrence_key=occurrence_key,
            identity=identity,
        )
        template = f"hosted:{strategy.id}"
        token_id = f"worker-{label}"
        raw_token = f"kwa_{label}_token"
        # The grant is composed by the production capability rule, so a
        # data-only child cannot even hold the action.
        actions = strategy_service.capability_actions(capabilities)
        _insert_token(
            self.factory, token_id=token_id, raw_token=raw_token, actions=actions, template=template
        )
        token = self.worker._get_token_by_hash_sync(_hash_token(raw_token))
        run_id = f"run-{label}"
        payload = WorkerRunCreateRequest(
            strategy_run_id=run_id,
            template_id=template,
            account_scope=ACCOUNT,
            execution_mode="paper",
            metadata={"hosted_job_id": job.id, "hosted_strategy_id": strategy.id},
            runtime_state={
                "hosted": {
                    "job_id": job.id,
                    "strategy_id": strategy.id,
                    "attempt": 1,
                    "version_id": version.id,
                    "capabilities": dict(capabilities),
                    "occurrence_key": occurrence_key,
                    "evaluation_id": evaluation_id,
                    "evaluation_kind": evaluation_kind,
                    **(
                        {
                            "schedule_id": f"sch-{label}",
                            "session_date": "2026-10-10",
                            "opens_at": "2026-10-10T03:45:00+00:00",
                            "closes_at": "2026-10-10T10:00:00+00:00",
                        }
                        if session_kind
                        else {}
                    ),
                },
            },
        )
        binding = RunBindingInput(
            strategy_id=strategy.id,
            owner_id=OWNER,
            account_id=ACCOUNT,
            execution_environment="paper",
            bound_by="supervisor",
            binding_source="hosted_job",
        )
        if validated_run_create:
            # The production run-creation entry point, with all of its token
            # scope / mode / template validation.
            asyncio.run(
                create_worker_run_for_token(
                    _request(self.app, token=raw_token),
                    token,
                    payload,
                    strategy_run_id=run_id,
                    binding=binding,
                )
            )
        else:
            self.attribution.create_run_with_binding(
                token=token, payload=payload, strategy_run_id=run_id, binding=binding
            )
        self.strategies.claim_job(
            job.id,
            lease_owner=LEASE_OWNER,
            expected_lease_epoch=0,
            expected_attempt=1,
            lease_until=_now() + timedelta(hours=1),
        )
        self.strategies.reserve_child_token(
            job.id,
            lease_owner=LEASE_OWNER,
            expected_lease_epoch=1,
            expected_attempt=1,
            token_id=token_id,
        )
        self.strategies.record_child_run(
            job.id,
            lease_owner=LEASE_OWNER,
            expected_lease_epoch=1,
            expected_attempt=1,
            token_id=token_id,
            run_id=run_id,
        )
        self.strategies.mark_running_and_handoff(
            job.id,
            lease_owner=LEASE_OWNER,
            expected_lease_epoch=1,
            expected_attempt=1,
            run_id=run_id,
        )
        claimed = self.worker._claim_run_session_sync(run_id, 600, 600)
        assert claimed, f"session claim failed for {label}"
        return Child(
            label=label,
            strategy_id=strategy.id,
            version_id=version.id,
            job_id=job.id,
            run_id=run_id,
            token_id=token_id,
            raw_token=raw_token,
            nonce=str(claimed["worker_session_nonce"]),
            occurrence_key=occurrence_key,
            evaluation_id=evaluation_id,
            schedule_id=f"sch-{label}",
            session_date=session_date,
            actions=tuple(actions),
        )


@pytest.fixture(scope="module")
def harness():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _prepare(dbapi_connection, connection_record):
        _ = connection_record
        dbapi_connection.create_function(
            "NOW", 0, lambda: datetime.now(timezone.utc).isoformat()
        )
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("ATTACH DATABASE ':memory:' AS public")
        for ddl in _PUBLIC_DDL:
            cursor.execute(ddl)
        cursor.close()

    _Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    strategies = SqlAlchemyStrategyRepository(factory)
    worker = SqlAlchemyAlgoWorkerRepository(factory)
    attribution = SqlAttributionStore(session_factory=factory)

    app = FastAPI()
    app.state.algo_worker_repository = worker
    app.state.strategies_session_factory = factory
    app.state.attribution_store = attribution
    app.state.proposal_store = ProposalStore(session_factory=factory)
    app.state.journal_service = StubJournalService()

    _seed_public_catalog(factory)
    try:
        yield Harness(app, strategies, worker, attribution, factory)
    finally:
        engine.dispose()


def _payload(child: Child, **overrides):
    body = {
        "evaluation_id": child.evaluation_id,
        "evaluation_kind": "scheduled_occurrence",
        "strategy_run_id": child.run_id,
        "strategy_id": child.strategy_id,
        "account_scope": ACCOUNT,
        "job_id": child.job_id,
        "target_kind": "single_instrument",
        "payload": {
            "instrument_token": 100,
            "exchange": "NSE",
            "tradingsymbol": "RELIANCE",
            "product": "CNC",
            "target_quantity": 5,
        },
    }
    body.update(overrides)
    return ProposalSubmitRequest(**body)


_MISSING = object()


async def _submit(harness, child, payload, *, token=_MISSING, nonce=_MISSING):
    """Await the real route while keeping the event loop awake.

    This sandbox does not deliver a worker thread's ``call_soon_threadsafe``
    wakeup to the loop's ``select``, so a bare ``await asyncio.to_thread(...)``
    stalls until some unrelated timer fires. The route is untouched: the tests
    only poll so the loop observes the real work promptly.
    """
    resolved_token = child.raw_token if token is _MISSING else token
    resolved_nonce = child.nonce if nonce is _MISSING else nonce
    task = asyncio.ensure_future(
        submit_proposal(
            _request(harness.app, token=resolved_token, nonce=resolved_nonce), payload
        )
    )
    while not task.done():
        await asyncio.wait({task}, timeout=0.05)
    return task.result()


def _proposals_for(harness, child: Child) -> int:
    """Proposals persisted **for this child's run**.

    The world is module-scoped, so a global count would see earlier tests' rows
    (and would hide a refusal that still wrote an envelope for this run).
    """
    with harness.factory() as session:
        return int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM strategy_proposals WHERE strategy_run_id = :run"
                ),
                {"run": child.run_id},
            ).scalar()
            or 0
        )


def _detail(exc) -> dict:
    return exc.value.detail


TRADE_CAPS = {"trade": True, "notify": False, "data": True}
DATA_CAPS = {"trade": False, "notify": False, "data": True}


# ---------------------------------------------------------------------------
# the grant itself
# ---------------------------------------------------------------------------


def test_the_grant_is_derived_from_capabilities_not_from_defaults():
    from backend.api.routers.worker_shared import DEFAULT_WORKER_ACTIONS
    from backend.api.schemas.worker import _DEFAULT_WORKER_ACTIONS

    # Grantable (an owner may issue it; a trade-capable child receives it)...
    assert "proposals:submit" in DEFAULT_WORKER_ACTIONS
    assert "proposals:submit" in strategy_service.capability_actions(TRADE_CAPS)
    # ...but never a default: no existing token and no data-only child gains it.
    assert "proposals:submit" not in _DEFAULT_WORKER_ACTIONS
    assert "proposals:submit" not in strategy_service.capability_actions(DATA_CAPS)
    assert "proposals:submit" not in strategy_service.child_run_token_actions()


# ---------------------------------------------------------------------------
# the happy path, end to end through the real records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trade_capable_child_opens_proposal_authority_for_its_own_evaluation(harness):
    child = harness.mint("trade", TRADE_CAPS)
    assert "proposals:submit" in child.actions
    response = await _submit(harness, child, _payload(child))
    assert response.status == "validated"
    assert response.plan is not None
    stored = ProposalStore(session_factory=harness.factory).get_proposal(response.proposal_id)
    # Job and evaluation identity are the persisted binding, not the payload.
    assert stored["job_id"] == child.job_id
    assert stored["evaluation_id"] == child.evaluation_id
    assert stored["strategy_id"] == child.strategy_id


@pytest.mark.asyncio
async def test_a_proposal_built_from_the_child_occurrence_is_accepted(harness, monkeypatch):
    """The id the child reads at attach is exactly the id the route accepts.

    This closes the scheduled-occurrence loop: the run detail (real DB) is fed
    through the real SDK ``attach_run``, the payload is built from the resulting
    ``config.hosted_occurrence`` rather than from a hardcoded id, and the real
    bound-evaluation check accepts it.
    """
    child = harness.mint("occurrence", TRADE_CAPS)
    run_detail = harness.worker._get_run_sync(child.run_id)

    from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient, RunConfig

    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(base_url="http://hosted.test", token=child.raw_token)
    )
    monkeypatch.setattr(client, "get_run", lambda run_id: run_detail)
    managed = client.attach_run(
        child.run_id,
        session_nonce=child.nonce,
        config=RunConfig(
            template_id=run_detail["template_id"],
            account_scope=run_detail["account_scope"],
            execution_mode=run_detail["execution_mode"],
        ),
    )

    occurrence = managed.config.hosted_occurrence
    assert occurrence is not None
    assert occurrence["job_id"] == child.job_id
    assert occurrence["evaluation_id"] == child.evaluation_id
    assert occurrence["evaluation_kind"] == "scheduled_occurrence"

    response = await _submit(
        harness,
        child,
        _payload(
            child,
            job_id=occurrence["job_id"],
            evaluation_id=occurrence["evaluation_id"],
            evaluation_kind=occurrence["evaluation_kind"],
        ),
    )
    assert response.status == "validated"
    stored = ProposalStore(session_factory=harness.factory).get_proposal(response.proposal_id)
    assert stored["evaluation_id"] == child.evaluation_id
    assert stored["job_id"] == child.job_id


@pytest.mark.asyncio
async def test_a_session_child_may_submit_many_valid_session_evaluations(harness):
    child = harness.mint(
        "session",
        TRADE_CAPS,
        evaluation_kind="session_occurrence",
        session_date="2026-10-10",
    )
    first_id = child.evaluation_id
    second_id = f"session:{child.schedule_id}:2026-10-10:1"

    first = await _submit(
        harness, child, _payload(child, evaluation_id=first_id, evaluation_kind="session_occurrence")
    )
    second = await _submit(
        harness,
        child,
        _payload(
            child,
            evaluation_id=second_id,
            evaluation_kind="session_occurrence",
            payload={**_payload(child).payload, "target_quantity": 6},
        ),
    )

    assert first.status == "validated"
    assert second.status == "validated"
    assert _proposals_for(harness, child) == 2


@pytest.mark.asyncio
async def test_a_session_child_cannot_submit_another_session_or_date(harness):
    from fastapi import HTTPException

    child = harness.mint(
        "session-boundary",
        TRADE_CAPS,
        evaluation_kind="session_occurrence",
        session_date="2026-10-10",
    )
    with pytest.raises(HTTPException) as other_date:
        await _submit(
            harness,
            child,
            _payload(
                child,
                evaluation_id=f"session:{child.schedule_id}:2026-10-11:0",
                evaluation_kind="session_occurrence",
            ),
        )
    with pytest.raises(HTTPException) as other_schedule:
        await _submit(
            harness,
            child,
            _payload(
                child,
                evaluation_id="session:sch-other:2026-10-10:0",
                evaluation_kind="session_occurrence",
            ),
        )

    assert other_date.value.status_code == 409
    assert _detail(other_date)["rejection_reason"] == "EVALUATION_IDENTITY_MISMATCH"
    assert other_schedule.value.status_code == 409
    assert _detail(other_schedule)["rejection_reason"] == "EVALUATION_IDENTITY_MISMATCH"
    assert _proposals_for(harness, child) == 0


def test_the_real_run_creation_entry_point_binds_a_proposal_capable_child(harness):
    """The production run-creation path accepts a proposal-capable child.

    Synchronous on purpose: ``Harness.mint(validated_run_create=True)`` drives the
    async entry point with ``asyncio.run``, which cannot run inside a test loop.
    """
    child = harness.mint("validated", TRADE_CAPS, validated_run_create=True)
    with harness.factory() as session:
        run = session.execute(
            text(
                "SELECT strategy_run_id, token_id, template_id, account_scope, execution_mode "
                "FROM public.algo_worker_runs WHERE strategy_run_id = :run"
            ),
            {"run": child.run_id},
        ).fetchone()
    assert run is not None
    assert run.token_id == child.token_id
    assert run.template_id == f"hosted:{child.strategy_id}"
    assert run.account_scope == ACCOUNT
    assert run.execution_mode == "paper"
    binding = harness.attribution.run_binding(strategy_run_id=child.run_id)
    assert binding["strategy_id"] == child.strategy_id
    assert binding["account_id"] == ACCOUNT


@pytest.mark.asyncio
async def test_the_same_evaluation_replays_idempotently(harness):
    child = harness.mint("replay", TRADE_CAPS)
    first = await _submit(harness, child, _payload(child))
    second = await _submit(harness, child, _payload(child))
    assert second.proposal_id == first.proposal_id
    assert second.idempotent is True
    assert _proposals_for(harness, child) == 1


# ---------------------------------------------------------------------------
# refusals, all before persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_data_only_child_cannot_hold_the_grant(harness):
    from fastapi import HTTPException

    child = harness.mint("data", DATA_CAPS)
    assert "proposals:submit" not in child.actions
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child))
    assert exc.value.status_code == 403
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_a_fenced_attempt_is_refused_before_persistence(harness):
    from fastapi import HTTPException

    child = harness.mint("fenced", TRADE_CAPS)
    assert harness.strategies.mark_recovery_required(
        child.job_id,
        lease_owner=LEASE_OWNER,
        expected_lease_epoch=1,
        expected_attempt=1,
    )
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child))
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "HOSTED_ATTEMPT_FENCED"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_an_expired_lease_is_refused_before_persistence(harness):
    from fastapi import HTTPException

    child = harness.mint("expired", TRADE_CAPS)
    with harness.factory() as session:
        session.execute(
            text("UPDATE strategy_jobs SET lease_until = :past WHERE id = :job"),
            {"past": _now() - timedelta(minutes=1), "job": child.job_id},
        )
        session.commit()
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child))
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "HOSTED_LEASE_EXPIRED"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_a_missing_session_nonce_is_refused_before_persistence(harness):
    from fastapi import HTTPException

    child = harness.mint("nononce", TRADE_CAPS)
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child), nonce=None)
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "WORKER_SESSION_REQUIRED"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_a_superseded_session_nonce_is_refused_before_persistence(harness):
    from fastapi import HTTPException

    child = harness.mint("stale", TRADE_CAPS)
    with pytest.raises(HTTPException) as exc:
        await _submit(
            harness, child, _payload(child), nonce="wsn_stale_from_another_attempt"
        )
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "WORKER_SESSION_CONFLICT"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_a_caller_supplied_job_id_is_checked_against_the_persisted_job(harness):
    from fastapi import HTTPException

    child = harness.mint("jobid", TRADE_CAPS)
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child, job_id="job-somebody-elses"))
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "JOB_IDENTITY_MISMATCH"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_an_evaluation_that_is_not_the_bound_occurrence_is_refused(harness):
    from fastapi import HTTPException

    child = harness.mint("eval", TRADE_CAPS)
    with pytest.raises(HTTPException) as exc:
        await _submit(
            harness,
            child,
            _payload(
                child,
                evaluation_id="sched:sch-elsewhere:2026-11-10",
                evaluation_kind="run_now",
            ),
        )
    assert exc.value.status_code == 409
    assert _detail(exc)["rejection_reason"] == "EVALUATION_IDENTITY_MISMATCH"
    assert _proposals_for(harness, child) == 0


@pytest.mark.asyncio
async def test_the_fallback_attribution_store_uses_the_injected_factory(harness):
    """With no store wired, the route must read the binding from the same DB.

    The fallback used to construct ``SqlAttributionStore()`` with its own default
    engine, so a submission would look unauthorized (or write to another
    database) even though the run was bound in the app's store.
    """
    child = harness.mint("fallback", TRADE_CAPS)
    original = harness.app.state.attribution_store
    del harness.app.state.attribution_store
    try:
        response = await _submit(harness, child, _payload(child))
    finally:
        harness.app.state.attribution_store = original
    assert response.status == "validated"
    assert _proposals_for(harness, child) == 1


@pytest.mark.asyncio
async def test_a_run_binding_that_disagrees_with_the_job_is_refused(harness):
    """The two halves of a hosted run's authority must agree."""
    from fastapi import HTTPException

    child = harness.mint("disagree", TRADE_CAPS)
    other = harness.mint("other", DATA_CAPS)
    # Rebind the run to another strategy: the binding and the hosted job now
    # disagree, and no plan may come out of that.
    with harness.factory() as session:
        session.execute(
            text(
                "UPDATE strategy_run_bindings SET strategy_id = :sid, account_id = :acct "
                "WHERE strategy_run_id = :run"
            ),
            {"sid": other.strategy_id, "acct": ACCOUNT, "run": child.run_id},
        )
        session.commit()
    with pytest.raises(HTTPException) as exc:
        await _submit(harness, child, _payload(child))
    assert exc.value.status_code in (403, 409)
    assert _proposals_for(harness, child) == 0
