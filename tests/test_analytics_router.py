from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers import analytics as analytics_router  # noqa: E402


class _FakeAnalyticsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def compute_analytics_summary(self, **kwargs):
        self.calls.append(("summary", kwargs))
        if kwargs["environment_id"] == "missing":
            raise LookupError("Unknown environment_id: missing")
        return {
            "environment": {"environment_id": kwargs["environment_id"], "mode": "paper", "account_scope": "acct-paper"},
            "period": kwargs["period"],
            "anchor_date": kwargs["anchor_date"],
            "metrics": {"closed_episode_count": 1, "net_pnl": "10", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "strategies": [],
        }

    def compute_strategy_deep_dive(self, **kwargs):
        self.calls.append(("strategy", kwargs))
        return {
            "environment": {"environment_id": kwargs["environment_id"], "mode": "paper", "account_scope": "acct-paper"},
            "period": kwargs["period"],
            "anchor_date": kwargs["anchor_date"],
            "strategy": {"template_id": kwargs["template_id"], "strategy_family": "indicator_strategy", "template_key": kwargs["template_id"], "display_name": "Alpha"},
            "metrics": {"closed_episode_count": 1, "net_pnl": "10", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "equity_curve": [],
        }

    def compute_equity_curve(self, **kwargs):
        self.calls.append(("equity", kwargs))
        return {
            "environment": {"environment_id": kwargs["environment_id"], "mode": "paper", "account_scope": "acct-paper"},
            "period": kwargs["period"],
            "anchor_date": kwargs["anchor_date"],
            "template_id": kwargs.get("template_id"),
            "metrics": {"closed_episode_count": 1, "net_pnl": "10", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "points": [],
        }

    def compute_cost_analysis(self, **kwargs):
        self.calls.append(("cost", kwargs))
        return {
            "environment": {"environment_id": kwargs["environment_id"], "mode": "paper", "account_scope": "acct-paper"},
            "period": kwargs["period"],
            "anchor_date": kwargs["anchor_date"],
            "metrics": {"closed_episode_count": 1, "net_pnl": "10", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"},
            "strategies": [],
        }

    def compute_paper_live_comparison(self, **kwargs):
        self.calls.append(("compare", kwargs))
        if kwargs["paper_environment_id"] == "bad-paper":
            raise ValueError("paper_environment_id must reference a paper environment")
        return {
            "template_id": kwargs["template_id"],
            "period": kwargs["period"],
            "anchor_date": kwargs["anchor_date"],
            "paper_environment": {"environment_id": kwargs["paper_environment_id"], "mode": "paper", "account_scope": "acct-paper"},
            "live_environment": {"environment_id": kwargs["live_environment_id"], "mode": "live", "account_scope": "acct-live"},
            "paper": {"closed_episode_count": 1, "net_pnl": "10", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "live": {"closed_episode_count": 1, "net_pnl": "5", "cost_breakdown": {"brokerage": "1", "exchange_txn_charge": "0", "stt": "0", "stamp_duty": "0", "sebi_charge": "0", "gst": "0", "total_taxes": "0", "total_charges": "1"}},
            "delta": {},
            "combined": None,
        }


def _build_client(service: _FakeAnalyticsService) -> TestClient:
    app = FastAPI()
    app.state.analytics_service = service
    app.include_router(analytics_router.router, prefix="/api")
    return TestClient(app)


def test_analytics_routes_require_auth() -> None:
    client = _build_client(_FakeAnalyticsService())

    response = client.get("/api/analytics/v1/summary", params={"environment_id": "env-paper"})

    assert response.status_code == 401


def test_summary_route_passes_period_and_date(monkeypatch) -> None:
    service = _FakeAnalyticsService()
    client = _build_client(service)
    monkeypatch.setattr(analytics_router, "require_app_user", lambda _request: None)

    response = client.get("/api/analytics/v1/summary", params={"environment_id": "env-paper", "period": "month", "date": "2026-05-04"})

    assert response.status_code == 200
    assert service.calls[0][0] == "summary"
    assert service.calls[0][1]["period"] == "month"
    assert str(service.calls[0][1]["anchor_date"]) == "2026-05-04"


def test_strategy_route_uses_path_template_id(monkeypatch) -> None:
    service = _FakeAnalyticsService()
    client = _build_client(service)
    monkeypatch.setattr(analytics_router, "require_app_user", lambda _request: None)

    response = client.get("/api/analytics/v1/strategy/tmpl-a", params={"environment_id": "env-paper", "period": "week", "date": "2026-05-04"})

    assert response.status_code == 200
    assert service.calls[0][0] == "strategy"
    assert service.calls[0][1]["template_id"] == "tmpl-a"


def test_equity_curve_route_passes_optional_template_id(monkeypatch) -> None:
    service = _FakeAnalyticsService()
    client = _build_client(service)
    monkeypatch.setattr(analytics_router, "require_app_user", lambda _request: None)

    response = client.get("/api/analytics/v1/equity-curve", params={"environment_id": "env-paper", "template_id": "tmpl-a"})

    assert response.status_code == 200
    assert service.calls[0][0] == "equity"
    assert service.calls[0][1]["template_id"] == "tmpl-a"


def test_compare_route_maps_value_error_to_400(monkeypatch) -> None:
    service = _FakeAnalyticsService()
    client = _build_client(service)
    monkeypatch.setattr(analytics_router, "require_app_user", lambda _request: None)

    response = client.get(
        "/api/analytics/v1/compare",
        params={"template_id": "tmpl-a", "paper_environment_id": "bad-paper", "live_environment_id": "env-live"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "paper_environment_id must reference a paper environment"


def test_compare_route_requires_named_query_params(monkeypatch) -> None:
    service = _FakeAnalyticsService()
    client = _build_client(service)
    monkeypatch.setattr(analytics_router, "require_app_user", lambda _request: None)

    response = client.get("/api/analytics/v1/compare", params={"template_id": "tmpl-a", "paper_environment_id": "env-paper"})

    assert response.status_code == 422
