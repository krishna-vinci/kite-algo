from types import SimpleNamespace
from unittest.mock import Mock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from api.routers import journal as journal_router  # noqa: E402


def _request_with_service(service):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(journal_service=service)))


def test_summary_forwards_indicator_live_filters(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = Mock()
    service.get_summary.return_value = {"ok": True}

    journal_router.get_summary(
        _request_with_service(service),
        period="month",
        strategy_family="indicator_strategy",
        execution_mode="live",
    )

    service.get_summary.assert_called_once_with(
        period="month",
        strategy_family="indicator_strategy",
        execution_mode="live",
    )


def test_summary_forwards_investment_live_filters(monkeypatch):
    monkeypatch.setattr(journal_router, "require_app_user", lambda _request: None)
    service = Mock()
    service.get_summary.return_value = {"ok": True}

    journal_router.get_summary(
        _request_with_service(service),
        period="month",
        strategy_family="investment_strategy",
        execution_mode="live",
    )

    service.get_summary.assert_called_once_with(
        period="month",
        strategy_family="investment_strategy",
        execution_mode="live",
    )
