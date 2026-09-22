#!/usr/bin/env python
"""Isolated end-to-end acceptance run for a simple hosted paper strategy.

What this runs (all against a DISPOSABLE database and a loopback API):

1. a uniquely named PostgreSQL database on the local test server, migrated to the
   real migration head;
2. an API process serving the PRODUCTION routers (operator, worker, hosted
   supervisor) over loopback, with only two boundaries simulated: the market-data
   runtime (deterministic quotes) and the instrument-catalog rows the fixture
   seeds;
3. the operator path: strategy -> immutable version (the checked-in example) ->
   admission policy -> manual job;
4. the real supervisor (``backend.strategies.supervisor``) claiming that job,
   preparing the launch (a real child credential), spawning the real child
   (``python -m kite_algo_worker.hosted <example>``) with a minimal environment;
5. the child submitting entry and reducing-exit proposals over HTTP; the operator
   performing reserve + execute through the production routes between them;
6. verification of attributed fills, the execution trail, the reservation, the
   job/attempt and the supervisor's process-cleanup evidence.

Nothing is committed, nothing touches production (DB port 15433 only), no broker
is contacted, no notification is sent and no live gate is opened.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
SDK_ROOT = REPO / "sdk" / "python"
EXAMPLE = Path(__file__).resolve().parent / "simple_entry_exit.py"
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"

ADMIN_DSN = os.environ.get(
    "ACCEPTANCE_ADMIN_DSN", "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
)
SUPERVISOR_CREDENTIAL = "acceptance-supervisor-credential"
APP_JWT_SECRET = "acceptance-jwt-secret"
APP_ADMIN_PASSWORD = "acceptance-operator-password"
STRATEGY = "acc-simple"
ACCOUNT_SCOPE = "kite:paper-acc"
SYMBOL = "RELIANCE"
TOKEN = 738561
GENERATION = str(uuid.uuid4())

RESULT: Dict[str, Any] = {"steps": [], "evidence": {}, "errors": []}


def step(name: str, **detail: Any) -> None:
    entry = {"step": name, "at": time.time(), **detail}
    RESULT["steps"].append(entry)
    print(f"[acceptance] {name}", json.dumps(detail, default=str), flush=True)


def fail(where: str, exc: BaseException) -> None:
    RESULT["errors"].append({"where": where, "error": repr(exc), "trace": traceback.format_exc()})


# ---------------------------------------------------------------- database

def create_database() -> tuple[str, str]:
    import psycopg2

    name = f"kite_accept_{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    root, _sep, _db = ADMIN_DSN.rpartition("/")
    return name, f"{root}/{name}"


def drop_database(name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


def migrate(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = dsn
    cfg = Config(str(REPO / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", str(REPO / "backend" / "alembic"))
    command.upgrade(cfg, "head")


def seed_catalog(session_factory) -> None:
    from sqlalchemy import text

    with session_factory() as session:
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                "VALUES (:gen, 'published', NOW())"
            ),
            {"gen": GENERATION},
        )
        instrument_id = str(uuid.uuid4())
        session.execute(
            text(
                "INSERT INTO public.instrument_catalog_records "
                "(instrument_id, identity_key, public_key, exchange, tradingsymbol, "
                " lifecycle_status, instrument_type, lot_size, tick_size, current_generation_id) "
                "VALUES (:iid, 'NSE:RELIANCE', 'NSE:RELIANCE', 'NSE', 'RELIANCE', "
                " 'active', 'EQ', 1, 0.05, :gen)"
            ),
            {"gen": GENERATION, "iid": instrument_id},
        )
        session.execute(
            text(
                "INSERT INTO public.instrument_broker_mappings "
                "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, broker_token, "
                " valid_from_generation, is_current) "
                "VALUES (:mid, :iid, 'kite', 'NSE', 'RELIANCE', :token, :gen, TRUE)"
            ),
            {"token": TOKEN, "gen": GENERATION, "iid": instrument_id, "mid": str(uuid.uuid4())},
        )
        session.commit()


# ---------------------------------------------------------------- boundaries

class SyntheticQuotes:
    """The market-data boundary: one deterministic price, no broker, no Redis."""

    def __init__(self, price: float = 1500.0) -> None:
        self.price = float(price)
        self.calls = 0

    async def get_tick(self, token: int) -> Dict[str, Any]:
        self.calls += 1
        return {"instrument_token": int(token), "last_price": self.price}

    async def get_last_price(self, token: int) -> float:
        self.calls += 1
        return self.price


# ---------------------------------------------------------------- api process

def build_app(session_factory):
    from fastapi import FastAPI

    from backend.api.routers import auth as auth_module
    from backend.api.routers import hosted_lifecycle, strategies, worker_auth, worker_execution
    from backend.api.routers import worker_proposals
    from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from backend.paper_runtime.repository import SqlAlchemyPaperRepository
    from backend.paper_runtime.service import PaperTradingService
    from backend.strategies.attribution import SqlAttributionStore

    app = FastAPI(title="acceptance loopback API")
    for router in (
        auth_module.router,
        worker_auth.router,
        worker_execution.router,
        worker_proposals.router,
        strategies.router,
        hosted_lifecycle.router,
    ):
        app.include_router(router, prefix="/api")

    app.state.strategies_session_factory = session_factory
    app.state.attribution_store = SqlAttributionStore(session_factory=session_factory)
    app.state.algo_worker_repository = SqlAlchemyAlgoWorkerRepository(session_factory)
    app.state.paper_runtime_service = PaperTradingService(
        repository=SqlAlchemyPaperRepository(session_factory),
        market_data_runtime=SyntheticQuotes(),
    )
    return app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ApiServer(threading.Thread):
    def __init__(self, app, port: int) -> None:
        super().__init__(daemon=True)
        self._config = None
        self.port = port
        self._app = app
        self._server = None

    def run(self) -> None:  # pragma: no cover - thread body
        import uvicorn

        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


# ---------------------------------------------------------------- operator

class Operator:
    """The operator surface: production routes, an ordinary login cookie."""

    def __init__(self, base: str) -> None:
        import httpx

        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=60.0)

    def login(self) -> None:
        _ensure_operator_credential()
        step(
            "operator_credential_env",
            **{
                name: bool(os.environ.get(name))
                for name in (
                    "APP_ADMIN_PASSWORD",
                    "APP_ADMIN_PASSWORD_HASH",
                    "APP_ADMIN_PASSWORD_HASH_B64",
                    "APP_ADMIN_PASSWORD_HASH_FILE",
                    "APP_JWT_SECRET",
                )
            },
        )
        response = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": APP_ADMIN_PASSWORD}
        )
        if response.status_code >= 400:
            raise RuntimeError(f"login failed: {response.status_code} {response.text}")
        step("operator_login", status=response.status_code)

    def post(self, path: str, **kwargs) -> Any:
        response = self.client.post(path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {response.status_code}: {response.text}")
        return response.json()

    def put(self, path: str, **kwargs) -> Any:
        response = self.client.put(path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"PUT {path} -> {response.status_code}: {response.text}")
        return response.json()

    def get(self, path: str, **kwargs) -> Any:
        response = self.client.get(path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {response.status_code}: {response.text}")
        return response.json()

    def close(self) -> None:
        self.client.close()


def operator_setup(operator: Operator, source: str, params: Dict[str, Any]) -> Dict[str, Any]:
    created = operator.post(
        "/api/strategies",
        json={
            "name": "Acceptance simple entry/exit",
            "description": "Bundle 4 isolated acceptance strategy",
            "execution_mode": "paper",
            "job_kind": "finite",
            "account_scope": ACCOUNT_SCOPE,
            "max_duration_s": 3600,
            "progress_deadline_s": 900,
            "stale_exit_policy": "none",
        },
    )
    strategy_id = str(created.get("strategy_id") or STRATEGY)
    version = operator.post(
        f"/api/strategies/{strategy_id}/versions",
        json={
            "source": source,
            "parameters_schema": {
                "type": "object",
                "properties": {"prices": {"type": "array"}},
                "required": ["prices"],
            },
            "capabilities": {"trade": True, "data": True},
        },
    )
    operator.put(
        f"/api/strategies/{strategy_id}/admission-policy",
        json={"allocation_inr": 100000.0},
    )
    # The child asserts these against its durable binding, so they must be the
    # REAL identifiers the platform just minted (never a fixture constant).
    params = {**params, "strategy_id": strategy_id}
    version_id = str(version.get("version_id") or version.get("version") or "1")
    step("version_created", version_id=version_id, keys=sorted(version.keys()))
    job = operator.post(
        f"/api/strategies/{strategy_id}/jobs",
        json={
            "version_id": version_id,
            "job_kind": "finite",
            "execution_mode": "paper",
            "params": params,
            "idempotency_key": f"acceptance-{uuid.uuid4().hex[:8]}",
        },
    )
    job_body = job.get("job") or {}
    job_id = str(job_body.get("id") or job_body.get("job_id") or "")
    step(
        "operator_setup",
        strategy_id=strategy_id,
        version_id=version_id,
        job_id=job_id,
        job_keys=sorted(job_body.keys()),
    )
    setup = {"strategy_id": strategy_id, "job": job, "job_id": job_id, "version_id": version_id}
    return setup


def proposals_for(session_factory, strategy_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT p.proposal_id, p.status, p.target_kind, l.plan_id "
                "FROM strategy_proposals p LEFT JOIN strategy_plans l ON l.proposal_id = p.proposal_id "
                "WHERE p.strategy_id = :sid ORDER BY p.created_at"
            ),
            {"sid": strategy_id},
        ).all()
    return [
        {"proposal_id": r[0], "status": r[1], "target_kind": r[2], "plan_id": r[3]} for r in rows
    ]


def wait_for_plan(session_factory, strategy_id: str, *, count: int, deadline_s: float) -> Dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        rows = [row for row in proposals_for(session_factory, strategy_id) if row["plan_id"]]
        if len(rows) >= count:
            return rows[count - 1]
        time.sleep(0.3)
    raise RuntimeError(
        f"timed out waiting for proposal #{count} (saw {proposals_for(session_factory, strategy_id)})"
    )


def execute_plan(operator: Operator, strategy_id: str, plan_id: str) -> Dict[str, Any]:
    operator.post(
        f"/api/strategies/{strategy_id}/plans/{plan_id}/reserve?execution_environment=paper"
    )
    trail = operator.post(f"/api/strategies/{strategy_id}/plans/{plan_id}/execute")
    step("operator_execute", plan_id=plan_id, status=trail.get("status"), steps=trail.get("steps"))
    return trail


# ---------------------------------------------------------------- child + supervisor

def run_supervisor(base: str, port: int, workspace: Path, job_id: str) -> Dict[str, Any]:
    from backend.strategies.supervisor import HostedSupervisor, SupervisorConfig

    config = SupervisorConfig(
        base_url=f"{base}/api",
        credential=SUPERVISOR_CREDENTIAL,
        workspace_root=str(workspace),
        lease_seconds=300.0,
        heartbeat_interval_s=1.0,
        startup_grace_s=60.0,
        progress_poll_s=0.5,
        term_grace_s=5.0,
        child_python=sys.executable,
        child_pythonpath=str(SDK_ROOT),
        child_base_url=base,
        api_timeout_s=30.0,
        observe_timeout_s=300.0,
    )
    loop = HostedSupervisor(config)
    return loop.run_once(job_id)


# ---------------------------------------------------------------- verification

def wait_for_terminal_job(
    session_factory, job_id: str, *, deadline_s: float = 90.0
) -> Dict[str, Any]:
    """Wait until the attempt is terminal AND its child process is confirmed gone."""
    from sqlalchemy import text

    started = time.monotonic()
    row = None
    while time.monotonic() - started < deadline_s:
        with session_factory() as session:
            row = (
                session.execute(
                    text(
                        "SELECT id, status, process_cleanup_state, reconciled_at "
                        "FROM public.strategy_jobs WHERE id = :jid"
                    ),
                    {"jid": job_id},
                )
                .mappings()
                .first()
            )
        if row is not None and str(row["status"]) in {"recovery_required", "stopped", "failed"} and str(
            row["process_cleanup_state"] or ""
        ) == "confirmed":
            return dict(row)
        time.sleep(0.5)
    raise RuntimeError(f"attempt never reached a terminal, cleanup-confirmed state: {row}")


def publish_positions(operator: "Operator", strategy_id: str, *, env: str = "paper") -> Dict[str, Any]:
    """Publish the attributed book (the production on-demand recompute).

    Attribution is published on demand, not by a scheduler: without this the
    executor would read a stale (empty) projection and compute a no-op exit.
    """
    result = operator.post(f"/api/strategies/{strategy_id}/positions/rebuild?environment={env}")
    step("attribution_published", projection_version=result.get("projection_version"), strategy_id=strategy_id)
    return result


def wait_for_quantity(
    session_factory, strategy_id: str, account: str, *, expected: int, deadline_s: float = 60.0
) -> int:
    from sqlalchemy import text

    started = time.monotonic()
    last = None
    while time.monotonic() - started < deadline_s:
        with session_factory() as session:
            last = int(
                session.execute(
                    text(
                        "SELECT COALESCE(SUM(net_quantity), 0) FROM strategy_position_projection "
                        "WHERE strategy_id = :sid AND account_id = :account "
                        "AND execution_environment = 'paper'"
                    ),
                    {"sid": strategy_id, "account": account},
                ).scalar()
                or 0
            )
        if last == expected:
            return last
        time.sleep(0.3)
    raise RuntimeError(f"attributed quantity never reached {expected} (last {last})")


def collect_evidence(session_factory, strategy_id: str, account: str) -> Dict[str, Any]:
    from sqlalchemy import text

    evidence: Dict[str, Any] = {}
    with session_factory() as session:
        evidence["proposals"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT proposal_id, status, target_kind, evaluation_id, created_at "
                    "FROM strategy_proposals WHERE strategy_id = :sid ORDER BY created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        evidence["plans"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT l.plan_id, l.plan_kind, l.resolved_plan -> 'legs' AS legs "
                    "FROM strategy_plans l WHERE l.strategy_id = :sid ORDER BY l.created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        evidence["execution_events"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT e.plan_id, e.step_no, e.event, e.filled_quantity, e.refusal_reason, "
                    " e.paper_order_id FROM strategy_plan_execution_events e "
                    "JOIN strategy_plans l ON l.plan_id = e.plan_id "
                    "WHERE l.strategy_id = :sid ORDER BY e.created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        evidence["reservations"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT reservation_id, plan_id, status, execution_environment "
                    "FROM strategy_reservations WHERE strategy_id = :sid ORDER BY created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        evidence["paper_orders"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT order_id, tradingsymbol, transaction_type, quantity, filled_quantity, "
                    " status, metadata_json ->> 'strategy_run_id' AS strategy_run_id "
                    "FROM public.paper_orders WHERE account_scope = :account ORDER BY updated_at"
                ),
                {"account": account},
            ).mappings()
        ]
        evidence["attributed_position"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT canonical_instrument_id, product, execution_environment, net_quantity, "
                    " projection_version FROM strategy_position_projection "
                    "WHERE strategy_id = :sid AND account_id = :account ORDER BY projection_version"
                ),
                {"sid": strategy_id, "account": account},
            ).mappings()
        ]
        evidence["jobs"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT id, status, execution_mode, lease_owner, lease_epoch, attempt, "
                    " run_id, process_cleanup_state FROM public.strategy_jobs "
                    "WHERE strategy_id = :sid"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        # The LINKED hosted worker runs: stopping a child is not the same as
        # closing the trading run it carried, so the closure is a required axis.
        evidence["linked_runs"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT r.strategy_run_id, r.execution_mode, r.status, r.closed_at "
                    "FROM public.algo_worker_runs r "
                    "JOIN public.strategy_jobs j ON j.run_id = r.strategy_run_id "
                    "WHERE j.strategy_id = :sid ORDER BY r.created_at"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
        evidence["barrier_events"] = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT event, ref FROM strategy_execution_barrier_events "
                    "WHERE strategy_id = :sid ORDER BY version"
                ),
                {"sid": strategy_id},
            ).mappings()
        ]
    return evidence


def summarise(evidence: Dict[str, Any]) -> Dict[str, Any]:
    events = evidence.get("execution_events", [])
    fills = [row for row in events if row["event"] == "filled"]
    # Entry/exit quantity comes from the paper book itself (the authoritative
    # record of what the runtime did), not from the child's narration.
    buy = sum(
        int(row.get("filled_quantity") or 0)
        for row in evidence.get("paper_orders", [])
        if str(row.get("transaction_type") or "").upper() == "BUY"
    )
    sell = sum(
        int(row.get("filled_quantity") or 0)
        for row in evidence.get("paper_orders", [])
        if str(row.get("transaction_type") or "").upper() == "SELL"
    )
    position = evidence.get("attributed_position") or []
    return {
        "plans": len(evidence.get("plans", [])),
        "filled_steps": len(fills),
        "paper_orders": len(evidence.get("paper_orders", [])),
        "final_attributed_quantity": sum(int(row.get("net_quantity") or 0) for row in position),
        "reservation_statuses": [row.get("status") for row in evidence.get("reservations", [])],
        "job_statuses": [row.get("status") for row in evidence.get("jobs", [])],
        "process_cleanup": [row.get("process_cleanup_state") for row in evidence.get("jobs", [])],
        "barrier_events": [row.get("event") for row in evidence.get("barrier_events", [])],
        "_buy_sell_shapes": {"buy": buy, "sell": sell},
    }


# ---------------------------------------------------------------- main

def _diagnose(session_factory) -> None:
    """Print why the run stopped: job state, proposals and the child's log tail."""
    workspace = REPO / ".acceptance-workspace"
    try:
        if session_factory is not None:
            from sqlalchemy import text

            with session_factory() as session:
                jobs = session.execute(
                    text(
                        "SELECT id, status, desired_state, attempt, lease_owner, "
                        " process_cleanup_state, last_progress_at FROM public.strategy_jobs"
                    )
                ).mappings().all()
            for row in jobs:
                print(f"[acceptance][diagnose] job={dict(row)}", flush=True)
        for log in sorted(workspace.rglob("*.log")):
            tail = log.read_text(errors="replace").splitlines()[-25:]
            print(f"[acceptance][diagnose] log {log}:", flush=True)
            for line in tail:
                print(f"    {line}", flush=True)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the failure
        print(f"[acceptance][diagnose] failed: {exc!r}", flush=True)


def _ensure_operator_credential() -> None:
    """Re-assert this instance's operator credential at the moment of login.

    Importing the app can load a `.env` (load_dotenv without override only fills
    unset names), so the isolated instance's own admin password is asserted right
    before the login route verifies it. Nothing is bypassed: the route still
    checks the credential.
    """
    for stale in (
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        os.environ.pop(stale, None)
    os.environ["APP_ADMIN_USERNAME"] = "admin"
    os.environ["APP_ADMIN_PASSWORD"] = APP_ADMIN_PASSWORD
    os.environ["APP_JWT_SECRET"] = APP_JWT_SECRET
    os.environ.setdefault("JWT_SECRET", APP_JWT_SECRET)



def _export_isolated_env(dsn: str) -> None:
    """The isolated process environment: disposable DB, loopback-only auth.

    Nothing here widens a gate: the account scope is the isolated paper account,
    the supervisor credential is a local secret, and the app runs in its
    documented development auth mode so the operator can log in like any user.
    """
    # This IS the operator for this isolated instance: clear any inherited admin
    # hash so the local password below is the credential (never a bypass - the
    # login route still verifies it).
    for stale in (
        "APP_ADMIN_PASSWORD_HASH",
        "APP_ADMIN_PASSWORD_HASH_B64",
        "APP_ADMIN_PASSWORD_HASH_FILE",
    ):
        os.environ.pop(stale, None)
    os.environ.update(
        {
            "DATABASE_URL": dsn,
            "APP_ADMIN_USERNAME": "admin",
            "APP_ENV": "development",
            "APP_ALLOW_INSECURE_DEV_AUTH": "true",
            "APP_JWT_SECRET": APP_JWT_SECRET,
            "APP_ADMIN_PASSWORD": APP_ADMIN_PASSWORD,
            "HOSTED_SUPERVISOR_CREDENTIAL": SUPERVISOR_CREDENTIAL,
            "HOSTED_STRATEGY_ACCOUNT_SCOPES": ACCOUNT_SCOPE,
            "HOSTED_SUPERVISOR_WORKSPACE": str(REPO / ".acceptance-workspace"),
        }
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-database", action="store_true", help="do not drop the disposable DB")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    db_name, dsn = create_database()
    _export_isolated_env(dsn)
    step("database_created", database=db_name, dsn_host=dsn.split("@")[-1])
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server: Optional[ApiServer] = None
    supervisor_thread: Optional[threading.Thread] = None
    supervisor_result: Dict[str, Any] = {}
    source = EXAMPLE.read_text()
    params = {
        "strategy_id": STRATEGY,
        "prices": [1490.0, 1510.0, 1520.0, 1505.0, 1480.0],
        "entry_price": 1505.0,
        "exit_price": 1495.0,
        "quantity": 10,
        "tradingsymbol": SYMBOL,
        "instrument_token": TOKEN,
        "exchange": "NSE",
        "product": "CNC",
        "reference_price": 1500.0,
        "poll_seconds": 0.25,
        "deadline_seconds": 180.0,
    }
    try:
        migrate(dsn)
        step("migrated_to_head")

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        engine = create_engine(dsn, poolclass=NullPool)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        seed_catalog(session_factory)
        step("catalog_seeded", symbol=SYMBOL)

        app = build_app(session_factory)
        server = ApiServer(app, port)
        server.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                import httpx

                httpx.get(f"{base}/api/hosted-supervisor/jobs", timeout=2.0)
                break
            except Exception:  # noqa: BLE001 - the socket is not up yet
                time.sleep(0.2)
        step("api_serving", base=base)

        operator = Operator(base)
        operator.login()
        setup = operator_setup(operator, source, params)
        strategy_id = setup["strategy_id"]
        job_id = str(setup["job_id"])

        from backend.strategies import supervisor_auth as _sup_auth

        import httpx as _httpx

        listed = _httpx.get(
            f"{base}/api/hosted-supervisor/jobs",
            params={"status": "queued", "limit": 50},
            headers={_sup_auth.HEADER_NAME: SUPERVISOR_CREDENTIAL},
            timeout=30.0,
        )
        step(
            "supervisor_jobs_listed",
            status=listed.status_code,
            body=listed.text[:400],
            header=_sup_auth.HEADER_NAME,
        )

        def _supervise() -> None:
            try:
                result = run_supervisor(base, port, REPO / ".acceptance-workspace", job_id)
                supervisor_result.update(result)
                step("supervisor_returned", result=result)
            except BaseException as exc:  # noqa: BLE001 - reported below
                fail("supervisor", exc)
                supervisor_result["error"] = repr(exc)
                print(f"[acceptance] supervisor FAILED: {exc!r}", file=sys.stderr, flush=True)

        supervisor_thread = threading.Thread(target=_supervise, daemon=True)
        supervisor_thread.start()

        first = wait_for_plan(session_factory, strategy_id, count=1, deadline_s=args.timeout)
        step("entry_plan_frozen", plan_id=first["plan_id"])
        entry_trail = execute_plan(operator, strategy_id, first["plan_id"])
        # Attribution is published on demand: the operator publishes the entry fill
        # before the reducing plan is executed, so the exit's target is computed
        # against the position that actually exists.
        publish_positions(operator, strategy_id)
        step(
            "entry_position_attributed",
            quantity=wait_for_quantity(
                session_factory, strategy_id, ACCOUNT_SCOPE, expected=int(params["quantity"])
            ),
        )

        second = wait_for_plan(session_factory, strategy_id, count=2, deadline_s=args.timeout)
        step("exit_plan_frozen", plan_id=second["plan_id"])
        exit_trail = execute_plan(operator, strategy_id, second["plan_id"])
        publish_positions(operator, strategy_id)
        step(
            "final_position_attributed",
            quantity=wait_for_quantity(session_factory, strategy_id, ACCOUNT_SCOPE, expected=0),
        )

        RESULT["trails"] = {"entry": entry_trail, "exit": exit_trail}

        # Ordinary operator cleanup: stop the attempt (release + revoke). This is
        # also what ends the child, so it comes AFTER every execution - an
        # exposure increase never happens once the attempt's authority is gone.
        stop = operator.post(
            f"/api/strategies/{strategy_id}/jobs/{job_id}/stop", json={"attempt": 1}
        )
        step("job_stop", stop=stop.get("stop"), idempotent=stop.get("idempotent"))
        terminal = wait_for_terminal_job(session_factory, job_id)
        step("attempt_terminal", **terminal)

        supervisor_thread.join(timeout=args.timeout)
        step("supervisor_finished", alive=supervisor_thread.is_alive(), result=supervisor_result)

        # The ordinary reconciliation path: this is where the platform records
        # the durable settlement-barrier proof (under the book's advisory lock)
        # and decides from persisted evidence. A non-2xx is an UNMET assertion.
        response = operator.client.post(
            f"/api/strategies/{strategy_id}/jobs/{job_id}/reconciliation",
            json={"attempt": 1},
        )
        settled = response.status_code < 400
        body = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - the raw text is the evidence
            body = response.text[:600]
        step("job_reconciliation", status=response.status_code, settled=settled, body=body)
        RESULT["settlement"] = {"settled": settled, "status": response.status_code, "body": body}

        evidence = collect_evidence(session_factory, strategy_id, ACCOUNT_SCOPE)
        summary = summarise(evidence)
        RESULT["evidence"] = evidence
        RESULT["summary"] = summary
        step("evidence_collected", summary={
            key: value for key, value in summary.items() if key != "_buy_sell_shapes"
        })
    except BaseException as exc:  # noqa: BLE001 - the report carries the failure
        fail("run", exc)
        print(traceback.format_exc(), file=sys.stderr)
        _diagnose(session_factory if "session_factory" in dir() else None)
    finally:
        if supervisor_thread is not None and supervisor_thread.is_alive():
            supervisor_thread.join(timeout=10)
        if server is not None:
            server.stop()
        try:
            evidence = RESULT.get("evidence") or {}
            if evidence:
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                dash = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                (EVIDENCE_DIR / f"acceptance-{dash}.json").write_text(
                    json.dumps(RESULT, indent=2, default=str)
                )
                step("evidence_written", path=str(EVIDENCE_DIR / f"acceptance-{dash}.json"))
        except Exception as exc:  # noqa: BLE001
            fail("evidence_write", exc)
        if not args.keep_database:
            # The supervisor's scratch workspace (per-job source/logs/state) is
            # host state, not evidence: the sanitized evidence JSONs are written
            # under examples/hosted_acceptance/evidence. Removing it keeps a
            # successful run from leaving task artifacts behind.
            try:
                shutil.rmtree(REPO / ".acceptance-workspace", ignore_errors=True)
                step("workspace_cleaned", path=str(REPO / ".acceptance-workspace"))
            except Exception as exc:  # noqa: BLE001
                fail("workspace_cleanup", exc)
            try:
                drop_database(db_name)
                step("database_dropped", database=db_name)
            except Exception as exc:  # noqa: BLE001
                fail("drop_database", exc)

    ok = not RESULT["errors"] and bool(RESULT.get("summary"))
    if ok:
        summary = RESULT["summary"]
        shapes = summary.get("_buy_sell_shapes") or {}
        settlement = RESULT.get("settlement") or {}
        runs = list((RESULT.get("evidence") or {}).get("linked_runs") or [])
        runs_closed = bool(runs) and all(
            str(row.get("status") or "") == "closed" and row.get("closed_at") is not None
            for row in runs
        )
        ok = (
            summary.get("final_attributed_quantity") == 0
            and int(summary.get("paper_orders", 0)) >= 2
            and int(shapes.get("buy", 0)) > 0
            and int(shapes.get("sell", 0)) > 0
            and bool(settlement.get("settled"))
            and runs_closed
        )
        RESULT["required_assertions"] = {
            "final_attributed_quantity_zero": summary.get("final_attributed_quantity") == 0,
            "entry_and_exit_paper_orders": int(shapes.get("buy", 0)) > 0 and int(shapes.get("sell", 0)) > 0,
            "settlement_reconciled": bool(settlement.get("settled")),
            # A stopped child with an OPEN run is not a closed exposure.
            "linked_worker_run_closed": runs_closed,
        }
    RESULT["ok"] = bool(ok)
    try:
        evidence = RESULT.get("evidence") or {}
        if evidence:
            EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            final_path = EVIDENCE_DIR / f"acceptance-{stamp}-final.json"
            final_path.write_text(json.dumps(RESULT, indent=2, default=str))
            step("final_verdict_written", path=str(final_path), ok=bool(ok))
    except Exception as exc:  # noqa: BLE001 - never mask the verdict
        print(f"[acceptance] final verdict write failed: {exc!r}", file=sys.stderr)

    print(json.dumps({"ok": ok, "required_assertions": RESULT.get("required_assertions"),
                      "summary": RESULT.get("summary"), "errors": RESULT["errors"]}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
