"""Option sessions must exist without an operator POST.

Three ways a session comes to exist are covered here: the app lifespan
autostarts the configured underlyings, and a live/paper option admission tries
one session start before it can refuse for a missing chain.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.api.routers.worker_proposals import _start_option_session_before_admission
from backend.app.bootstrap import autostart_option_sessions


class _FakeAutoManager:
    def __init__(self, *, fail: tuple[str, ...] = ()) -> None:
        self._fail = {name.upper() for name in fail}
        self.calls: list[str] = []

    async def ensure_session(self, underlying: str) -> bool:
        self.calls.append(underlying)
        return underlying.upper() not in self._fail


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_bootstrap_autostart_starts_every_configured_underlying(monkeypatch) -> None:
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "nifty, BANKNIFTY ,,SENSEX,NIFTY")
    app = _app()
    manager = _FakeAutoManager()

    result = asyncio.run(autostart_option_sessions(app, object(), manager=manager))

    assert manager.calls == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert result == {"started": ["NIFTY", "BANKNIFTY", "SENSEX"], "failed": []}
    assert app.state.options_session_manager is manager


def test_bootstrap_autostart_failure_never_blocks_the_others(monkeypatch) -> None:
    monkeypatch.setenv("OPTIONS_AUTOSTART_UNDERLYINGS", "NIFTY,BANKNIFTY,SENSEX")
    app = _app()
    manager = _FakeAutoManager(fail=("BANKNIFTY",))

    result = asyncio.run(autostart_option_sessions(app, object(), manager=manager))

    assert manager.calls == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert result == {"started": ["NIFTY", "SENSEX"], "failed": ["BANKNIFTY"]}
    assert app.state.options_session_manager is manager


class _AdmissionManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_session(self, underlying: str) -> bool:
        self.calls.append(underlying)
        return True


def test_option_admission_attempts_one_start_per_submission() -> None:
    manager = _AdmissionManager()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(options_session_manager=manager))
    )
    payload = SimpleNamespace(payload={"underlying": "NIFTY"})

    asyncio.run(
        _start_option_session_before_admission(
            request, payload=payload, target_kind="option_structure"
        )
    )
    assert manager.calls == ["NIFTY"]

    # A non-option plan and a plan without an underlying attempt nothing.
    asyncio.run(
        _start_option_session_before_admission(
            request, payload=payload, target_kind="equity"
        )
    )
    asyncio.run(
        _start_option_session_before_admission(
            request, payload=SimpleNamespace(payload={}), target_kind="option_structure"
        )
    )
    assert manager.calls == ["NIFTY"]


def test_option_admission_without_a_manager_is_a_noop() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    asyncio.run(
        _start_option_session_before_admission(
            request,
            payload=SimpleNamespace(payload={"underlying": "NIFTY"}),
            target_kind="option_structure",
        )
    )


class _RouteManager:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._snapshot = {"underlying": "NIFTY", "expiries": [], "updated_at": None}

    def normalize_underlying_symbol(self, value: str) -> str:
        return value.strip().upper()

    async def ensure_session(self, underlying: str) -> bool:
        self.calls.append(underlying)
        return True

    def get_snapshot(self, _underlying: str):
        return self._snapshot


def test_worker_session_read_ensures_the_session_once(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers.worker_shared import require_worker_token
    from backend.options.api import worker_options_router as worker_router_module
    from backend.options.api.market_router import get_options_session_manager, router as market_router
    from backend.options.api.worker_options_router import router as worker_options_router

    manager = _RouteManager()
    app = FastAPI()
    app.include_router(market_router)
    app.include_router(worker_options_router)
    app.dependency_overrides[require_worker_token] = lambda: SimpleNamespace(allowed_actions=[])
    app.dependency_overrides[get_options_session_manager] = lambda: manager

    async def _external_token_read_guard(_request, _token):
        return None

    monkeypatch.setattr(
        worker_router_module, "enforce_hosted_read_authority", _external_token_read_guard
    )

    response = TestClient(app).get(
        "/api/algo-workers/worker/options/underlyings/NIFTY/session"
    )

    assert response.status_code == 200
    assert manager.calls == ["NIFTY"]
