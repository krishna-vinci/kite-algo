"""B2.6b S1 owner actions on PostgreSQL: the production path, end to end.

Why PostgreSQL: this suite proves what SQLite cannot - that the trail mutation
and the settlement barrier are written in ONE transaction, that
``record_work_event_once`` appends exactly one ``work_resolved`` per action, that
two concurrent dispositions of the same step serialize so exactly ONE of them
wins, and that the widened ``ck_spee_event`` / journal / reconciliation
vocabularies accept the new rows on the real schema.

Everything here is the REAL path: the owner routes, the real durable option-run
store with its compare-and-set, the real paper runtime's cancel boundary, and
the real ``option_adjust_owner_state`` takeover rule.

    RECONCILIATION_PG_ADMIN='postgresql://postgres:testonly@127.0.0.1:15433/postgres' \
        .venv/bin/pytest tests/integration/test_strategy_owner_actions_postgres.py -q

Disposable database on the local test server (port 15433) only.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal

import pytest

PG_ADMIN = os.environ.get("RECONCILIATION_PG_ADMIN") or os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
if not PG_ADMIN:
    pytest.skip("no disposable PostgreSQL admin DSN configured", allow_module_level=True)

from sqlalchemy import text  # noqa: E402

OWNER = "app:admin"
ACCOUNT = "kite:paper-owner-actions"
G1 = "22222222-2222-2222-2222-222222222222"
PRICE = 1500.0
SHORT_SYMBOL = "NIFTY26OCT25000CE"
HEDGE_SYMBOL = "NIFTY26OCT30000CE"


def _create_db() -> tuple[str, str]:
    import psycopg2

    name = f"kite_owneractions_{uuid.uuid4().hex[:10]}"
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


_PARENT_REVISION = "20260925_000048"


def _complete_revision_chain(cfg) -> str | None:
    """Make the revision chain walkable, returning a temp dir to clean up.

    ``20260925_000049`` (this worktree's owner-action migration) names C1.1's
    ``20260925_000048`` as its parent, and that file lands with the rebase. Until
    then the chain has exactly one missing link, so this adds an EMPTY stub for it
    in a temporary version location: the real migration still runs, and nothing is
    written inside the repository. Once the parent is on disk this returns
    ``None`` and the upgrade is the ordinary one.
    """
    import tempfile
    import re
    from pathlib import Path

    versions = Path("backend/alembic/versions").resolve()
    # ``revision = "<parent>"`` means the parent exists on disk; a file that only
    # names it as ITS down_revision (this worktree's own migration) does not.
    declares_parent = re.compile(
        r'^\s*revision\s*=\s*"' + _PARENT_REVISION + r'"', re.MULTILINE
    )
    if any(
        declares_parent.search(path.read_text(encoding="utf-8"))
        for path in versions.glob("*.py")
    ):
        return None
    stub_dir = tempfile.mkdtemp(prefix="alembic-parent-stub-")
    (Path(stub_dir) / "20260925_000048_stub.py").write_text(
        "revision = \"20260925_000048\"\n"
        "down_revision = \"20260925_000047\"\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade():\n    pass\n\n\n"
        "def downgrade():\n    pass\n",
        encoding="utf-8",
    )
    # ``Config.get_version_locations_list`` reads the FILE config (not
    # ``set_main_option``), and needs an explicit separator so the ':' splits.
    cfg.file_config.set("alembic", "version_locations", f"{stub_dir}:{versions}")
    cfg.file_config.set("alembic", "path_separator", "os")
    return stub_dir


def _upgrade(cfg) -> None:
    """Upgrade the disposable database to head, tolerating the missing parent."""
    import shutil

    from alembic import command

    stub_dir = _complete_revision_chain(cfg)
    try:
        command.upgrade(cfg, "head")
    finally:
        if stub_dir is not None:
            shutil.rmtree(stub_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def pg():
    import psycopg2  # noqa: F401
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    name, dsn = _create_db()
    os.environ["DATABASE_URL"] = dsn
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", "backend/alembic")
    engine = create_engine(dsn, poolclass=NullPool)
    _upgrade(cfg)

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
        yield {"factory": factory, "dsn": dsn}
    finally:
        engine.dispose()
        _drop_db(name)


@pytest.fixture(autouse=True)
def _authorized_account_scope(monkeypatch):
    """The hosted surface only serves configured account scopes (production rule)."""
    monkeypatch.setenv("HOSTED_STRATEGY_ACCOUNT_SCOPES", ACCOUNT)
    yield


class _CatalogInstrument:
    def get_instrument_by_exchange_symbol(self, exchange, tradingsymbol):
        return {
            "instrument_token": 900001,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "lot_size": 75,
            "instrument_type": "CE",
            "last_price": PRICE,
        }


class _TickRuntime:
    async def get_tick(self, token):
        return {"instrument_token": token, "last_price": PRICE}

    async def get_last_price(self, token):
        return PRICE


def _leg(*, side, symbol, strike) -> dict:
    return {
        "instrument_id": str(uuid.uuid4()),
        "exchange": "NFO",
        "tradingsymbol": symbol,
        "broker_exchange": "NFO",
        "broker_symbol": symbol,
        "broker_token": 900001,
        "product": "NRML",
        "instrument_type": "CE",
        "option_type": "CE",
        "strike": strike,
        "expiry": "2026-10-29",
        "lot_size": 75,
        "ratio": 1,
        "side": side,
        "quantity": 150,
        "signed_quantity": 150 if side == "BUY" else -150,
        "reference_price": 100.0,
        "role": "short" if side == "SELL" else "hedge",
    }


class _Env:
    """One strategy + job + entry plan edge, seeded against the real tables."""

    def __init__(self, factory) -> None:
        from backend.strategies.attribution import SqlAttributionStore
        from backend.strategies.repository import SqlAlchemyStrategyRepository

        self.factory = factory
        self.run_id = f"run-owner-{uuid.uuid4().hex[:8]}"
        self.short_leg = _leg(side="SELL", symbol=SHORT_SYMBOL, strike=25000.0)
        self.hedge_leg = _leg(side="BUY", symbol=HEDGE_SYMBOL, strike=30000.0)
        repo = SqlAlchemyStrategyRepository(factory)
        strategy = repo.create_strategy(
            owner_id=OWNER,
            name=f"owneractions-{uuid.uuid4().hex[:6]}",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope=ACCOUNT,
            max_duration_s=21600,
            progress_deadline_s=600,
            stale_exit_policy="exit_on_worker_stale",
        )
        self.strategy_id = str(strategy.id)
        self._insert_run_row()
        SqlAttributionStore(session_factory=factory).bind_run(
            strategy_run_id=self.run_id,
            strategy_id=self.strategy_id,
            owner_id=OWNER,
            account_id=ACCOUNT,
            execution_environment="paper",
            bound_by="test",
            binding_source="hosted_job",
        )
        version = repo.create_version(
            strategy_id=self.strategy_id,
            source="# owner actions test\n",
            source_sha256=f"sha-{uuid.uuid4().hex[:12]}",
            parameters_schema={},
            capabilities_snapshot={},
            created_by=OWNER,
        )
        job = repo.create_job(
            strategy_id=self.strategy_id,
            version_id=str(version.id),
            owner_id=OWNER,
            job_kind="finite",
            execution_mode="paper",
            attempt=1,
            desired_state="started",
        )
        self.job_id = str(job.id)
        with factory() as session:
            session.execute(
                text("UPDATE public.strategy_jobs SET run_id = :run WHERE id = :job"),
                {"run": self.run_id, "job": self.job_id},
            )
            session.commit()
        self.entry_plan_id = self.seed_plan(phase="entry")

    def _insert_run_row(self) -> None:
        from sqlalchemy import text

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.algo_worker_runs (strategy_run_id, token_id, template_id, "
                    " account_scope, execution_mode, status) "
                    "VALUES (:run, 'tok-owner', 'tpl-owner', :account, 'paper', 'open')"
                ),
                {"run": self.run_id, "account": ACCOUNT},
            )
            session.commit()

    def seed_plan(self, *, phase: str, option_run_id: str | None = None) -> str:
        from sqlalchemy import text

        plan_id = str(uuid.uuid4())
        proposal_id = str(uuid.uuid4())
        resolved = {
            "target_kind": "option_structure",
            "product": "NRML",
            "expiry_policy": "exit_before_cutoff",
            "structure_digest": "digest-owner-actions",
            "protection_policy": {},
            "legs": [self.short_leg, self.hedge_leg],
            "option_run": {"phase": phase, "option_run_id": option_run_id},
        }
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_proposals (proposal_id, strategy_id, account_id, "
                    " evaluation_id, evaluation_kind, strategy_run_id, target_kind, payload, "
                    " payload_sha256, status) VALUES (:pid, :sid, :account, :eval, 'run_now', "
                    " :run, 'option_structure', '{}', :sha, 'validated')"
                ),
                {
                    "pid": proposal_id,
                    "sid": self.strategy_id,
                    "account": ACCOUNT,
                    "eval": f"eval-{plan_id}",
                    "run": self.run_id,
                    "sha": f"sha-{plan_id}",
                },
            )
            session.execute(
                text(
                    "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                    " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation) "
                    "VALUES (:pid, :prop, :sid, :account, 'option_structure', :hash, '{}', "
                    " :resolved, :gen)"
                ),
                {
                    "pid": plan_id,
                    "prop": proposal_id,
                    "sid": self.strategy_id,
                    "account": ACCOUNT,
                    "hash": f"hash-{plan_id}",
                    "resolved": json.dumps(resolved),
                    "gen": G1,
                },
            )
            session.commit()
        return plan_id

    def seed_run(
        self, *, plan_id: str, phase: str, status: str, trades: list | None = None
    ) -> str:
        """The real durable run one plan is bound to, with its OWN fills.

        The run row comes first and the binding edge follows: the edge's FK to
        ``option_run_states`` is enforced by the database.
        """
        from backend.options.execution.durable_store import DurableOptionRunStore
        from backend.options.execution.models import OptionRunCreateRequest
        from backend.options.execution.plan_binding import PlanOptionRunBindingStore

        store = DurableOptionRunStore(session_factory=self.factory)
        option_run_id = f"opt_run_{uuid.uuid4().hex[:10]}"
        store.create_run(
            OptionRunCreateRequest(
                strategy_run_id=option_run_id,
                strategy_name=self.strategy_id,
                product="NRML",
                legs=[
                    {
                        "leg_id": f"{plan_id}:1",
                        "tradingsymbol": SHORT_SYMBOL,
                        "transaction_type": "SELL",
                        "quantity": 150,
                        "exchange": "NFO",
                        "product": "NRML",
                    },
                    {
                        "leg_id": f"{plan_id}:2",
                        "tradingsymbol": HEDGE_SYMBOL,
                        "transaction_type": "BUY",
                        "quantity": 150,
                        "exchange": "NFO",
                        "product": "NRML",
                    },
                ],
                protection={"structure_digest": "digest-owner-actions"},
                metadata={
                    "strategy_id": self.strategy_id,
                    "account_id": ACCOUNT,
                    "execution_environment": "paper",
                    "worker_run_id": self.run_id,
                    "plan_id": plan_id,
                    "source": "hosted_plan_execution",
                },
            )
        )
        run = store.get_run(option_run_id)
        run.status = status
        run.trades = list(trades or [])
        store.save_run(run)
        PlanOptionRunBindingStore(session_factory=self.factory).bind(
            plan_id=plan_id,
            option_run_id=option_run_id,
            strategy_id=self.strategy_id,
            account_id=ACCOUNT,
            execution_environment="paper",
            phase=phase,
            worker_run_id=self.run_id,
        )
        return option_run_id


def _open_trade(leg_id: str, side: str, quantity: int) -> dict:
    return {
        "leg_id": leg_id,
        "transaction_type": side,
        "quantity": quantity,
        "tradingsymbol": SHORT_SYMBOL if leg_id.endswith(":1") else HEDGE_SYMBOL,
    }


def _app(factory, monkeypatch):
    from fastapi import FastAPI

    from backend.api.routers import strategies as strategies_module
    from backend.api.routers import strategy_owner_actions as owner_actions_module
    from backend.app import auth as auth_module
    from backend.app.auth import AppUser
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore

    monkeypatch.setattr(
        auth_module,
        "get_optional_app_user",
        lambda _request: AppUser(username="admin", role="admin"),
    )
    app = FastAPI()
    app.include_router(strategies_module.router, prefix="/api")
    app.include_router(owner_actions_module.router, prefix="/api")
    app.dependency_overrides[strategies_module._strategies_db] = lambda: factory
    app.dependency_overrides[owner_actions_module._owner_actions_db] = lambda: factory
    app.state.strategies_session_factory = factory
    app.state.attribution_store = SqlAttributionStore(session_factory=factory)
    # The REAL paper runtime, including its cancel boundary.
    app.state.paper_runtime_service = PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory=factory),
        instruments_repository=_CatalogInstrument(),
        market_data_runtime=_TickRuntime(),
        default_starting_balance=Decimal("1000000"),
    )
    return app


def _client(factory, monkeypatch):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(factory, monkeypatch)),
        base_url="http://test",
    )


def _rows(factory, sql, params=None):
    from sqlalchemy import text

    with factory() as session:
        return [
            dict(row)
            for row in session.execute(text(sql), params or {}).mappings().all()
        ]


def _seed_paper_order(
    factory,
    *,
    order_id: str,
    status: str,
    quantity: int,
    filled: int,
    pending: int,
    plan_id: str,
    step_no: int,
    symbol: str = SHORT_SYMBOL,
) -> None:
    """A paper order + its progress row, exactly as the runtime writes them."""
    from sqlalchemy import text

    metadata = {
        "plan_id": plan_id,
        "step_no": step_no,
        "execution_mode": "paper",
        "strategy_run_id": "run-owner",
    }
    with factory() as session:
        session.execute(
            text(
                "INSERT INTO public.paper_accounts (account_scope) VALUES (:scope) "
                "ON CONFLICT DO NOTHING"
            ),
            {"scope": ACCOUNT},
        )
        session.execute(
            text(
                "INSERT INTO public.paper_orders (account_scope, order_id, instrument_token, "
                " exchange, tradingsymbol, product, transaction_type, quantity, "
                " filled_quantity, pending_quantity, status, metadata_json) VALUES "
                "(:scope, :order_id, 900001, 'NFO', :symbol, 'NRML', 'sell', :quantity, "
                " :filled, :pending, :status, CAST(:metadata AS jsonb))"
            ),
            {
                "scope": ACCOUNT,
                "order_id": order_id,
                "symbol": symbol,
                "quantity": quantity,
                "filled": filled,
                "pending": pending,
                "status": status,
                "metadata": json.dumps(metadata),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.paper_order_fill_progress (account_scope, paper_order_id, "
                " filled_quantity, remaining_quantity, status) VALUES "
                "(:scope, :order_id, :filled, :pending, :status)"
            ),
            {
                "scope": ACCOUNT,
                "order_id": order_id,
                "filled": filled,
                "pending": pending,
                "status": status,
            },
        )
        session.commit()


def _seed_trail(factory, *, plan_id: str, step_no: int, rows) -> None:
    from sqlalchemy import text

    with factory() as session:
        for index, (event, order_id, filled) in enumerate(rows):
            session.execute(
                text(
                    "INSERT INTO public.strategy_plan_execution_events "
                    "(id, plan_id, step_no, event, paper_order_id, filled_quantity, actor_id, "
                    " detail, created_at) VALUES "
                    "(:id, :plan, :step, :event, :order_id, :filled, :actor, '{}'::jsonb, "
                    " NOW() + (:offset * INTERVAL '1 second'))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "plan": plan_id,
                    "step": step_no,
                    "event": event,
                    "order_id": order_id,
                    "filled": filled,
                    "actor": OWNER,
                    "offset": index,
                },
            )
        session.commit()


def _preview_url(strategy_id: str) -> str:
    return f"/api/strategies/{strategy_id}/owner-actions/pending-work"


def _cancel_url(strategy_id: str) -> str:
    return f"/api/strategies/{strategy_id}/owner-actions/cancel-pending"


def _dead_url(strategy_id: str, plan_id: str, step_no: int = 1) -> str:
    return f"/api/strategies/{strategy_id}/plans/{plan_id}/steps/{step_no}/dead-submission"


@pytest.mark.asyncio
async def test_cancel_pending_uses_the_real_paper_boundary_and_preserves_the_fill(
    pg, monkeypatch
):
    from backend.options.execution.models import OptionRunStatus

    factory = pg["factory"]
    env = _Env(factory)
    option_run_id = env.seed_run(
        plan_id=env.entry_plan_id,
        phase="entry",
        status=OptionRunStatus.ENTERING.value,
        trades=[
            _open_trade(f"{env.entry_plan_id}:1", "SELL", 75),
            _open_trade(f"{env.entry_plan_id}:2", "BUY", 150),
        ],
    )
    _seed_trail(
        factory,
        plan_id=env.entry_plan_id,
        step_no=1,
        rows=[("submitted", None, None), ("partially_filled", "PAPER-OWNER-1", 75)],
    )
    _seed_paper_order(
        factory,
        order_id="PAPER-OWNER-1",
        status="partially_filled",
        quantity=150,
        filled=75,
        pending=75,
        plan_id=env.entry_plan_id,
        step_no=1,
    )

    async with _client(factory, monkeypatch) as client:
        preview = await client.get(_preview_url(env.strategy_id))
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["coverage"] == "known"
        assert body["items"] == [
            {
                "plan_id": env.entry_plan_id,
                "step_no": 1,
                "order_id": "PAPER-OWNER-1",
                "remaining_quantity": 75,
                "eligibility": "eligible",
                "reason_code": None,
            }
        ]
        action = await client.post(
            _cancel_url(env.strategy_id),
            json={"evidence_digest": body["evidence_digest"], "reason": "owner_cancel"},
        )
        assert action.status_code == 200, action.text
        payload = action.json()
        assert payload["status"] == "complete"
        assert payload["items"][0]["outcome"] == "cancelled"
        assert payload["items"][0]["filled_quantity"] == 75
        assert payload["items"][0]["run_status"] == "partial_entry"
        assert payload["audit_id"]

    # The real paper runtime cancelled the order: terminal, zero remainder, and
    # the proven fill untouched on BOTH the order and the progress row.
    (order,) = _rows(
        factory,
        "SELECT status, filled_quantity, pending_quantity FROM public.paper_orders "
        "WHERE order_id = 'PAPER-OWNER-1'",
    )
    assert order == {"status": "cancelled", "filled_quantity": 75, "pending_quantity": 0}
    (progress,) = _rows(
        factory,
        "SELECT status, filled_quantity, remaining_quantity "
        "FROM public.paper_order_fill_progress WHERE paper_order_id = 'PAPER-OWNER-1'",
    )
    assert progress == {"status": "cancelled", "filled_quantity": 75, "remaining_quantity": 0}

    trail = _rows(
        factory,
        "SELECT event, filled_quantity, detail FROM public.strategy_plan_execution_events "
        "WHERE plan_id = :plan ORDER BY created_at, id",
        {"plan": env.entry_plan_id},
    )
    assert [row["event"] for row in trail] == [
        "submitted",
        "partially_filled",
        "partially_filled",
        "failed",
    ]
    assert trail[-1]["detail"]["disposition"] == "owner_cancelled"
    assert trail[-1]["detail"]["source_order_id"] == "PAPER-OWNER-1"

    # ONE work_resolved, written after the remainder was proven terminal.
    barriers = _rows(
        factory,
        "SELECT event, ref FROM public.strategy_execution_barrier_events "
        "WHERE strategy_id = :s ORDER BY version",
        {"s": env.strategy_id},
    )
    assert [row["event"] for row in barriers] == ["work_resolved"]

    # The run moved through the REAL compare-and-set.
    (run,) = _rows(
        factory,
        "SELECT status, trades FROM public.option_run_states WHERE strategy_run_id = :r",
        {"r": option_run_id},
    )
    assert run["status"] == "partial_entry"

    journal = _rows(
        factory,
        "SELECT event, reason_code FROM public.strategy_proposal_journal "
        "WHERE strategy_id = :s",
        {"s": env.strategy_id},
    )
    assert journal == [{"event": "owner_action", "reason_code": "cancel_pending"}]

    # The hosted job's own append-only audit carries the action.
    audit = _rows(
        factory,
        "SELECT outcome, reason_code, actor_id FROM public.strategy_job_reconciliations "
        "WHERE job_id = :job",
        {"job": env.job_id},
    )
    assert audit == [
        {
            "outcome": "owner_action",
            "reason_code": "OWNER_ACTION_CANCEL_PENDING",
            "actor_id": OWNER,
        }
    ]


@pytest.mark.asyncio
async def test_the_repair_assessment_names_the_unanswered_adjust_step(pg, monkeypatch):
    """The refusal carries its own coordinates, and the disposition clears them.

    This is the pair the options UI depends on: an ``adjusting`` run reports
    ``ambiguous`` with ``adjust_in_flight``, the assessment names the plan and step
    it is waiting on, and the dead-submission disposition of that step makes the
    takeover gate report ``finished``.
    """
    factory = pg["factory"]
    env = _Env(factory)
    adjust_plan_id = env.seed_plan(phase="adjust")
    option_run_id = env.seed_run(
        plan_id=adjust_plan_id, phase="adjust", status="adjusting"
    )
    _seed_trail(
        factory, plan_id=adjust_plan_id, step_no=1, rows=[("submitted", None, None)]
    )
    _seed_paper_order(
        factory,
        order_id="PAPER-OWNER-ADJ",
        status="cancelled",
        quantity=150,
        filled=0,
        pending=0,
        plan_id=adjust_plan_id,
        step_no=1,
    )
    repair_url = f"/api/strategies/{env.strategy_id}/option-runs/{option_run_id}/repair"

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(repair_url)
        assert inspection.status_code == 200, inspection.text
        body = inspection.json()
        assert body["state"] == "ambiguous"
        assert "adjust_in_flight" in body["reasons"]
        assert body["unresolved_steps"] == [
            {
                "plan_id": adjust_plan_id,
                "step_no": 1,
                "state": "submitted",
                "order_id": None,
            }
        ]

        # Act on exactly the step the assessment named.
        dead = await client.get(_dead_url(env.strategy_id, adjust_plan_id))
        assert dead.status_code == 200, dead.text
        disposed = await client.post(
            _dead_url(env.strategy_id, adjust_plan_id),
            json={
                "evidence_digest": dead.json()["evidence_digest"],
                "disposition": "cancelled",
                "reason": "owner_disposition",
            },
        )
        assert disposed.status_code == 200, disposed.text

        # Nothing is unanswered any more, so the refusal is gone.
        after = await client.get(repair_url)
        assert after.status_code == 200, after.text
        assert after.json()["unresolved_steps"] == []


@pytest.mark.asyncio
async def test_two_dead_submission_dispositions_race_and_exactly_one_wins(
    pg, monkeypatch
):
    """The trail is insert-only and the barrier is once-only: one action stands."""
    from backend.api.services.owner_actions import (
        OwnerActionsService,
        OwnerActionRefusal,
        owner_action_scope,
    )
    from backend.strategies.repository import SqlAlchemyStrategyRepository

    factory = pg["factory"]
    env = _Env(factory)
    adjust_plan_id = env.seed_plan(phase="adjust")
    # The durable run the adjust edge owns, so the takeover rule has a real
    # book to ask about.
    env.seed_run(plan_id=adjust_plan_id, phase="adjust", status="entering")
    _seed_trail(
        factory,
        plan_id=adjust_plan_id,
        step_no=1,
        rows=[("submitted", None, None)],
    )
    _seed_paper_order(
        factory,
        order_id="PAPER-OWNER-DEAD",
        status="cancelled",
        quantity=150,
        filled=0,
        pending=0,
        plan_id=adjust_plan_id,
        step_no=1,
    )

    async with _client(factory, monkeypatch) as client:
        inspection = await client.get(_dead_url(env.strategy_id, adjust_plan_id))
        assert inspection.status_code == 200, inspection.text
        evidence = inspection.json()
        assert evidence["trail_state"] == "submitted"
        assert evidence["source"] == "paper_order"
        assert evidence["status"] == "cancelled"
        assert evidence["allowed_dispositions"] == [
            "cancelled",
            "failed_residual_abandoned",
        ]
        assert evidence["evidence_digest"]

    repo = SqlAlchemyStrategyRepository(factory)
    # The SAME plan artifact the route resolves: the evidence (and therefore the
    # digest the owner pinned) is read from the frozen plan, so a hand-built
    # partial dict would be evidence the route never produced.
    from backend.strategies.proposals import ProposalStore

    plan = ProposalStore(session_factory=factory).get_plan(adjust_plan_id)
    assert plan is not None

    def _dispose():
        """Run the same disposition on its own thread: a real race, not a queue."""
        service = OwnerActionsService(
            session_factory=factory, repository=repo
        )
        scope = owner_action_scope(repo, OWNER, env.strategy_id)
        try:
            return service.dispose_dead_submission(
                scope,
                plan=plan,
                step_no=1,
                evidence_digest=evidence["evidence_digest"],
                disposition="cancelled",
                reason="owner_disposition",
                actor=OWNER,
            )
        except OwnerActionRefusal as exc:
            return {"status": "refused", "reason_code": exc.reason_code}

    outcomes = await asyncio.gather(
        asyncio.to_thread(_dispose), asyncio.to_thread(_dispose)
    )
    statuses = sorted(
        str(item.get("status") if item.get("status") != "refused" else item["reason_code"])
        for item in outcomes
    )
    # One of the two stands. The loser either observed the winner's committed
    # evidence (digest changed) or found the disposition already applied.
    assert "complete" in statuses, outcomes
    assert set(statuses) <= {"complete", "DEAD_SUBMISSION_EVIDENCE_CHANGED"}, outcomes

    trail = _rows(
        factory,
        "SELECT event, detail FROM public.strategy_plan_execution_events "
        "WHERE plan_id = :plan ORDER BY created_at, id",
        {"plan": adjust_plan_id},
    )
    terminal = [row for row in trail if row["event"] != "submitted"]
    assert len(terminal) == 1, trail
    assert terminal[0]["event"] == "cancelled"
    assert terminal[0]["detail"]["disposition"] == "cancelled"

    barriers = _rows(
        factory,
        "SELECT event, ref FROM public.strategy_execution_barrier_events "
        "WHERE strategy_id = :s ORDER BY version",
        {"s": env.strategy_id},
    )
    assert [row["event"] for row in barriers] == ["work_resolved"]

    journal = _rows(
        factory,
        "SELECT count(*) AS n FROM public.strategy_proposal_journal "
        "WHERE strategy_id = :s",
        {"s": env.strategy_id},
    )
    assert int(journal[0]["n"]) == 1

    # The adjust takeover rule reports FINISHED, which is what unblocks repair.
    from backend.options.execution.plan_binding import (
        option_adjust_owner_state,
        option_plan_execution_state,
    )
    from sqlalchemy import text as _text

    with factory() as session:
        assert option_plan_execution_state(adjust_plan_id, session=session)["state"] == "finished"
        (run_id,) = session.execute(
            _text(
                "SELECT option_run_id FROM public.strategy_plan_option_runs "
                "WHERE plan_id = :plan"
            ),
            {"plan": adjust_plan_id},
        ).first()
        assert (
            option_adjust_owner_state(str(run_id), session=session)["state"] == "finished"
        )
