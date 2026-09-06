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
