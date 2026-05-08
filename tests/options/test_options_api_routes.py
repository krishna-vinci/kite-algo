from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers.worker_shared import require_worker_token
from api.routers.worker_auth import router as algo_workers_router
from options.api.execution_router import get_option_execution_runtime_instance
from options.api.execution_router import router as options_execution_router
from options.api.market_router import router as options_market_router
from options.api.protection_router import router as options_protection_router
from options.api.strategy_router import router as options_strategy_router
from options.api.worker_options_router import router as worker_options_router
from options.execution.models import OptionRunCreateRequest
from options.execution.store import OptionRunStore, get_option_run_store

app = FastAPI()
app.include_router(options_market_router)
app.include_router(options_strategy_router)
app.include_router(options_execution_router)
app.include_router(options_protection_router)
app.include_router(worker_options_router)


def _has_route(path: str, method: str) -> bool:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method.upper() in route.methods:
            return True
    return False


def test_options_market_expiries_route_is_registered() -> None:
    assert _has_route("/api/options/underlyings/{underlying}/expiries", "GET")
    assert _has_route("/api/options/sessions", "POST")
    assert _has_route("/api/options/session/{underlying}", "GET")
    assert _has_route("/api/options/underlyings/{underlying}/stream", "GET")


def test_options_strategy_preview_route_is_registered() -> None:
    assert _has_route("/api/options/strategies/preview", "POST")


def test_options_execution_route_family_scaffolding_is_registered() -> None:
    assert _has_route("/api/options/runs", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/preview-entry", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/enter", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/preview-exit", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/exit", "POST")
    assert _has_route("/api/options/runs/{strategy_run_id}/orders", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/trades", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/state", "GET")


def test_options_protection_route_family_scaffolding_is_registered() -> None:
    assert _has_route("/api/options/runs/{strategy_run_id}/protection", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/protection/state", "GET")
    assert _has_route("/api/options/runs/{strategy_run_id}/protection/replay", "POST")


def test_main_registers_canonical_options_routers() -> None:
    main_source = Path("/home/krishna/kite-algo/main.py").read_text(encoding="utf-8")
    assert "app.include_router(options_market_router)" in main_source
    assert "app.include_router(options_strategy_router)" in main_source
    assert "app.include_router(options_execution_router)" in main_source
    assert "app.include_router(options_protection_router)" in main_source
    assert "app.include_router(worker_options_router)" in main_source


def test_worker_options_market_routes_are_registered() -> None:
    assert _has_route("/api/algo-workers/worker/options/underlyings/{underlying}/session", "GET")
    assert _has_route("/api/algo-workers/worker/options/underlyings/{underlying}/expiries", "GET")
    assert _has_route("/api/algo-workers/worker/options/strategies/preview", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-entry", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/enter", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/preview-exit", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/exit", "POST")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/state", "GET")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection", "PUT")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/state", "GET")
    assert _has_route("/api/algo-workers/worker/options/runs/{strategy_run_id}/protection/replay", "POST")


class _FakeAlgoWorkerRepo:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.token = type(
            "WorkerTokenStub",
            (),
            {
                "token_id": "worker-1",
                "name": "worker-test",
                "account_scope": "kite:paper-a",
                "allowed_modes": ["paper"],
                "allowed_actions": ["runs:read", "intents:submit", "runs:exit"],
                "allowed_templates": [],
                "status": "active",
                "expires_at": None,
            },
        )()

    async def get_run(self, strategy_run_id: str):
        run = self.runs.get(strategy_run_id)
        return dict(run) if run is not None else None

    async def get_token_by_hash(self, _token_hash: str):
        return self.token

    async def touch_token(self, _token_id: str) -> None:
        return None

    async def claim_run_session(self, strategy_run_id: str, *, freshness_seconds: int, claimed_without_heartbeat_seconds: int):
        _ = (freshness_seconds, claimed_without_heartbeat_seconds)
        run = self.runs.get(strategy_run_id)
        if run is None:
            return None
        if run.get("worker_session_nonce"):
            return None
        run["worker_session_nonce"] = "nonce-1"
        return dict(run)

    async def release_run_session(self, strategy_run_id: str, *, expected_nonce: str):
        run = self.runs.get(strategy_run_id)
        if run is None or str(run.get("worker_session_nonce") or "") != str(expected_nonce):
            return None
        run["worker_session_nonce"] = None
        return dict(run)

    async def record_run_heartbeat(self, strategy_run_id: str, *, expected_nonce: str):
        run = self.runs.get(strategy_run_id)
        if run is None or str(run.get("worker_session_nonce") or "") != str(expected_nonce):
            return None
        run["last_heartbeat_at"] = "2026-05-06T09:16:00+00:00"
        return dict(run)

    async def list_stale_recovery_runs(self):
        return []

    async def list_exiting_recovery_runs(self):
        return []


def _worker_test_app() -> tuple[FastAPI, OptionRunStore, _FakeAlgoWorkerRepo]:
    local_app = FastAPI()
    local_app.include_router(algo_workers_router, prefix="/api")
    local_app.include_router(options_execution_router)
    local_app.include_router(worker_options_router)
    store = OptionRunStore()
    worker_repo = _FakeAlgoWorkerRepo()
    local_app.state.algo_worker_repository = worker_repo
    local_app.dependency_overrides[get_option_run_store] = lambda: store
    local_app.dependency_overrides[require_worker_token] = lambda: type("T", (), {"token_id": "worker-1", "allowed_actions": ["runs:read", "intents:submit", "runs:exit"]})()
    local_app.dependency_overrides[get_option_execution_runtime_instance] = lambda: _FakeRuntime()
    return local_app, store, worker_repo


class _FakeRuntime:
    def build_entry_plan(self, run):
        return [
            {
                "leg_id": leg.get("leg_id"),
                "tradingsymbol": leg.get("tradingsymbol"),
                "transaction_type": leg.get("transaction_type"),
                "quantity": int(leg.get("quantity") or 0),
                "exchange": leg.get("exchange") or "NFO",
                "product": leg.get("product") or run.product,
            }
            for leg in list(run.legs or [])
        ]

    def default_entry_results(self, run):
        order_results = []
        trade_results = []
        for leg in list(run.legs or []):
            leg_id = str(leg.get("leg_id") or "")
            order_results.append({"order_id": f"ORD-{leg_id}", "leg_id": leg_id, "status": "filled", "phase": "entry"})
            trade_results.append(
                {
                    "trade_id": f"TRD-{leg_id}",
                    "order_id": f"ORD-{leg_id}",
                    "leg_id": leg_id,
                    "tradingsymbol": leg.get("tradingsymbol"),
                    "transaction_type": leg.get("transaction_type"),
                    "quantity": int(leg.get("quantity") or 0),
                    "phase": "entry",
                }
            )
        return order_results, trade_results

    def build_exit_plan(self, run):
        plans = []
        for leg in list(run.legs or []):
            side = str(leg.get("transaction_type") or "").upper()
            plans.append(
                {
                    "leg_id": leg.get("leg_id"),
                    "tradingsymbol": leg.get("tradingsymbol"),
                    "transaction_type": "SELL" if side == "BUY" else "BUY",
                    "quantity": int(leg.get("quantity") or 0),
                    "exchange": leg.get("exchange") or "NFO",
                    "product": leg.get("product") or run.product,
                }
            )
        return plans

    def default_exit_results(self, run):
        order_results = []
        trade_results = []
        for leg in list(run.legs or []):
            leg_id = str(leg.get("leg_id") or "")
            side = str(leg.get("transaction_type") or "").upper()
            order_results.append({"order_id": f"EXIT-ORD-{leg_id}", "leg_id": leg_id, "status": "filled", "phase": "exit"})
            trade_results.append(
                {
                    "trade_id": f"EXIT-TRD-{leg_id}",
                    "order_id": f"EXIT-ORD-{leg_id}",
                    "leg_id": leg_id,
                    "tradingsymbol": leg.get("tradingsymbol"),
                    "transaction_type": "SELL" if side == "BUY" else "BUY",
                    "quantity": int(leg.get("quantity") or 0),
                    "phase": "exit",
                }
            )
        return order_results, trade_results


def _seed_worker_option_run(store: OptionRunStore, strategy_run_id: str = "run-opt-1") -> None:
    store.create_run(
        OptionRunCreateRequest.model_validate(
            {
                "strategy_run_id": strategy_run_id,
                "strategy_name": "bull_call_spread",
                "product": "MIS",
                "legs": [
                    {
                        "leg_id": "buy_1",
                        "tradingsymbol": "NIFTY26MAY25000CE",
                        "transaction_type": "BUY",
                        "quantity": 75,
                        "exchange": "NFO",
                        "product": "MIS",
                    }
                ],
                "protection": None,
                "metadata": {},
            }
        )
    )


def test_worker_option_enter_rejects_stale_safety_token(monkeypatch):
    test_app, store, worker_repo = _worker_test_app()
    _seed_worker_option_run(store, "run-opt-1")
    worker_repo.runs["run-opt-1"] = {
        "strategy_run_id": "run-opt-1",
        "status": "open",
        "runtime_state": {"backend_protection_state": {"status": "active", "exit_submitted": False}},
        "execution_mode": "paper",
        "token_id": "worker-1",
        "account_scope": "kite:paper-a",
        "template_id": "bull_call_spread",
    }
    monkeypatch.setenv("WORKER_SAFETY_TOKEN_SECRET", "secret-key")

    from api.routers import worker_protection as algo_workers_module

    async def _fresh_snapshot(_request, _run_id):
        return {
            "applicable": True,
            "run_status": "created",
            "evaluation_mode": "run_state",
            "triggered": False,
            "blocking": False,
            "blocking_reason": None,
            "matched_rule": None,
            "metrics": {},
            "recommended_exit_orders_count": 0,
        }

    with TestClient(test_app) as client:
        monkeypatch.setattr(algo_workers_module, "_option_run_protection_snapshot_for_worker", _fresh_snapshot)
        response = client.get(
            "/api/algo-workers/worker/runs/run-opt-1/safety-check",
            headers={"Authorization": "Bearer secret-token"},
        )
        token = response.json()["safety_token"]

        async def _stale_snapshot(_request, _run_id):
            return {
                "applicable": True,
                "run_status": "exiting",
                "evaluation_mode": "run_state",
                "triggered": False,
                "blocking": True,
                "blocking_reason": "OPTIONS_RUN_NOT_ACTIVE",
                "matched_rule": None,
                "metrics": {},
                "recommended_exit_orders_count": 0,
            }

        monkeypatch.setattr(algo_workers_module, "_option_run_protection_snapshot_for_worker", _stale_snapshot)
        rejected = client.post(
            "/api/algo-workers/worker/options/runs/run-opt-1/enter",
            headers={"Authorization": "Bearer secret-token", "X-Worker-Session-Nonce": "nonce-1"},
            json={"safety_token": token},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["rejection_reason"] == "SAFETY_TOKEN_EXPIRED"


def test_worker_option_exit_rejects_stale_safety_token_before_state_mutation(monkeypatch):
    test_app, store, worker_repo = _worker_test_app()
    _seed_worker_option_run(store, "run-opt-2")
    worker_repo.runs["run-opt-2"] = {
        "strategy_run_id": "run-opt-2",
        "status": "open",
        "runtime_state": {"backend_protection_state": {"status": "active", "exit_submitted": False}},
        "execution_mode": "paper",
        "token_id": "worker-1",
        "account_scope": "kite:paper-a",
        "template_id": "bull_call_spread",
    }
    monkeypatch.setenv("WORKER_SAFETY_TOKEN_SECRET", "secret-key")

    from api.routers import worker_protection as algo_workers_module

    async def _fresh_snapshot(_request, _run_id):
        return {
            "applicable": True,
            "run_status": "created",
            "evaluation_mode": "run_state",
            "triggered": False,
            "blocking": False,
            "blocking_reason": None,
            "matched_rule": None,
            "metrics": {},
            "recommended_exit_orders_count": 0,
        }

    with TestClient(test_app) as client:
        monkeypatch.setattr(algo_workers_module, "_option_run_protection_snapshot_for_worker", _fresh_snapshot)
        response = client.get(
            "/api/algo-workers/worker/runs/run-opt-2/safety-check",
            headers={"Authorization": "Bearer secret-token"},
        )
        token = response.json()["safety_token"]

        async def _stale_snapshot(_request, _run_id):
            return {
                "applicable": True,
                "run_status": "cleanup_required",
                "evaluation_mode": "run_state",
                "triggered": False,
                "blocking": True,
                "blocking_reason": "OPTIONS_RUN_NOT_ACTIVE",
                "matched_rule": None,
                "metrics": {},
                "recommended_exit_orders_count": 0,
            }

        monkeypatch.setattr(algo_workers_module, "_option_run_protection_snapshot_for_worker", _stale_snapshot)
        before = store.get_run("run-opt-2")
        rejected = client.post(
            "/api/algo-workers/worker/options/runs/run-opt-2/exit",
            headers={"Authorization": "Bearer secret-token", "X-Worker-Session-Nonce": "nonce-1"},
            json={"safety_token": token},
        )
        after = store.get_run("run-opt-2")

    assert rejected.status_code == 409
    assert before.status == after.status


def test_worker_option_enter_requires_session_nonce_when_claimed():
    test_app, store, worker_repo = _worker_test_app()
    _seed_worker_option_run(store, "run-opt-session")
    worker_repo.runs["run-opt-session"] = {
        "strategy_run_id": "run-opt-session",
        "status": "open",
        "runtime_state": {},
        "execution_mode": "paper",
        "token_id": "worker-1",
        "account_scope": "kite:paper-a",
        "template_id": "bull_call_spread",
        "worker_session_nonce": "nonce-required",
    }

    with TestClient(test_app) as client:
        response = client.post(
            "/api/algo-workers/worker/options/runs/run-opt-session/enter",
            headers={"Authorization": "Bearer secret-token"},
            json={},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["rejection_reason"] == "WORKER_SESSION_REQUIRED"
