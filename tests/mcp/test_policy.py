from __future__ import annotations

import pytest

from kite_algo_mcp.catalog import TOOL_CATALOG
from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.policy import BackendCapabilities, PolicyService, PolicyViolation


def _policy(profile: str = "read", refresh: bool = False) -> PolicyService:
    return PolicyService(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="x", profile=profile, allow_data_refresh=refresh))


def test_visibility_matrix_is_default_deny_for_writes() -> None:
    read = _policy()
    assert read.visible("get_quotes")
    assert not read.visible("refresh_fundamentals")
    assert not read.visible("place_order")

    paper = _policy("paper", True)
    assert paper.visible("refresh_fundamentals")
    assert paper.visible("place_order")
    assert not paper.visible("create_gtt")

    live = _policy("live", True)
    assert live.visible("create_gtt")
    assert len(TOOL_CATALOG) == 73


@pytest.mark.asyncio
async def test_backend_action_intersection_and_live_profile_are_enforced() -> None:
    service = _policy("paper")
    service.capabilities = BackendCapabilities(actions={"market:read"}, modes={"paper"})
    with pytest.raises(PolicyViolation, match="worker token"):
        await service.authorize("get_funds", {}, object(), refresh=False)
    with pytest.raises(PolicyViolation, match="live execution"):
        await service.authorize("place_order", {"execution_mode": "live"}, object(), refresh=False)
    service.capabilities.actions = {"market:read", "intents:submit"}
    with pytest.raises(PolicyViolation, match="execution mode"):
        await service.authorize("place_order", {"execution_mode": "dry_run"}, object(), refresh=False)


@pytest.mark.asyncio
async def test_authorize_refreshes_backend_capabilities() -> None:
    class Fake:
        async def health(self):
            return {"allowed_actions": ["market:read"]}

    service = _policy()
    await service.authorize("get_quotes", {}, Fake())
    assert service.capabilities.actions == {"market:read"}


@pytest.mark.asyncio
async def test_health_scope_is_refreshed_before_argument_checks() -> None:
    class Fake:
        async def health(self):
            return {
                "allowed_actions": ["runs:create"],
                "allowed_modes": ["paper"],
                "account_scope": "kite:paper-a",
            }

    service = _policy("live")
    with pytest.raises(PolicyViolation, match="execution mode"):
        await service.authorize(
            "create_run",
            {
                "template_id": "template-a",
                "account_scope": "kite:paper-a",
                "execution_mode": "live",
            },
            Fake(),
        )


@pytest.mark.asyncio
async def test_scalar_account_scope_and_explicit_empty_modes_are_restrictive() -> None:
    class ScalarAccount:
        async def health(self):
            return {
                "allowed_actions": ["funds:read"],
                "allowed_modes": ["paper"],
                "account_scope": "kite:paper-a",
            }

    service = _policy("paper")
    with pytest.raises(PolicyViolation, match="account"):
        await service.authorize(
            "get_funds",
            {"mode": "paper", "account_scope": "kite:paper-b"},
            ScalarAccount(),
        )

    class EmptyModes:
        async def health(self):
            return {"allowed_actions": ["runs:create"], "allowed_modes": []}

    service = _policy("paper")
    with pytest.raises(PolicyViolation, match="execution mode"):
        await service.authorize(
            "create_run",
            {"template_id": "template-a", "account_scope": "kite:paper-a", "execution_mode": "paper"},
            EmptyModes(),
        )


def test_backend_health_action_names_match_worker_routes() -> None:
    assert TOOL_CATALOG["get_capabilities"].required_action == ""
    assert TOOL_CATALOG["get_fundamentals_features"].required_action == "market:read"
    assert TOOL_CATALOG["refresh_fundamentals"].required_action == "market:read"
    assert TOOL_CATALOG["request_history"].required_action == "market:read"


def test_catalog_only_advertises_backend_deduped_submission_hints() -> None:
    assert TOOL_CATALOG["place_order"].idempotent is True
    assert TOOL_CATALOG["place_basket"].idempotent is True
    for name in (
        "create_run", "modify_order", "cancel_order", "create_bracket", "cancel_bracket",
        "create_gtt", "modify_gtt", "delete_gtt", "exit_run", "update_run_risk",
        "update_run_protection", "create_option_run", "enter_option_run", "exit_option_run",
        "update_option_protection", "log_run_decision",
    ):
        assert TOOL_CATALOG[name].idempotent is False, name

    assert TOOL_CATALOG["place_order"].reconcile_with == "get_order"
    assert TOOL_CATALOG["place_basket"].reconcile_with == "get_basket"
    assert TOOL_CATALOG["create_option_run"].reconcile_with == "get_option_run_state"
