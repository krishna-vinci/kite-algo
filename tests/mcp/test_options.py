from __future__ import annotations

import json

import pytest
from fastmcp import Client

from kite_algo_mcp.config import MCPConfig
from kite_algo_mcp.server import create_server


class OptionsFakeNamespace:
    async def list_expiries(self, underlying):
        return {"underlying": underlying, "expiries": ["2026-09-10"]}

    async def get_chain(self, underlying, *, expiry=None):
        return {"underlying": underlying, "expiry": expiry, "items": []}

    async def get_mini_chain(self, underlying, *, expiry=None, window=5):
        return {"window": window, "items": []}

    async def get_greeks(self, underlying, *, expiry=None):
        return {"units": "per share", "items": []}

    async def get_pcr(self, underlying, *, expiry=None):
        return {"pcr": None}

    async def get_max_pain(self, underlying, *, expiry=None):
        return {"max_pain": None}

    async def resolve_contracts(self, underlying, payload):
        return {"resolved": [{"tradingsymbol": "ABC26SEP100CE", "lot_size": 25, "strike": 100, "option_type": "CE", "expiry_key": payload["expiry"], "ltp": 4.2}]}

    async def preview_strategy(self, payload):
        return {"preview": True, "payload": payload}

    async def preview_run_entry(self, run_id, payload):
        return {"preview": True, "run_id": run_id}

    async def preview_exit(self, run_id, payload):
        return {"preview": True, "run_id": run_id}

    async def create_run(self, **kwargs):
        return {"strategy_run_id": "opt-1", "legs": kwargs["legs"]}

    async def enter(self, run_id, payload, **kwargs):
        return {"status": "entered", "strategy_run_id": run_id}

    async def exit(self, run_id, payload, **kwargs):
        return {"status": "exited", "strategy_run_id": run_id}

    async def get_run_state(self, run_id):
        return {"strategy_run_id": run_id, "status": "created"}

    async def update_protection(self, run_id, protection, **kwargs):
        return {"updated": protection}

    async def get_protection_state(self, run_id):
        return {"strategy_run_id": run_id, "triggered": False}

    async def replay_protection(self, run_id, snapshots):
        return {"events": [{"index": index, "triggered": False} for index, _ in enumerate(snapshots)]}


class OptionsFake:
    def __init__(self):
        self.options = OptionsFakeNamespace()

    async def health(self):
        return {"allowed_actions": ["market:read", "runs:create", "runs:read", "intents:submit", "risk:update"]}

    async def claim_session(self, run_id):
        return {"session_nonce": "hidden"}

    async def release_session(self, run_id, *, session_nonce):
        pass

    async def run_heartbeat(self, run_id, *, session_nonce, status):
        pass


@pytest.mark.asyncio
async def test_option_market_and_contract_resolution_are_typed() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret"), client=OptionsFake())
    async with Client(server) as client:
        expiry = await client.call_tool("list_option_expiries", {"request": {"underlying": "NIFTY"}})
        assert "2026-09-10" in expiry.content[0].text
        resolved = await client.call_tool("resolve_option_contracts", {"request": {"underlying": "NIFTY", "expiry": "2026-09-10", "selector": {"kind": "exact", "option_type": "CE", "strike": 100}}})
        assert "ABC26SEP100CE" in resolved.content[0].text


@pytest.mark.asyncio
async def test_option_preview_replay_and_paper_entry_remain_explicit() -> None:
    server = create_server(MCPConfig(api_url="http://127.0.0.1:18777", worker_token="secret", profile="paper"), client=OptionsFake())
    async with Client(server) as client:
        preview = await client.call_tool("preview_option_strategy", {"request": {"strategy_name": "vertical", "underlying": "NIFTY", "expiry": "2026-09-10", "legs": [{"kind": "exact", "option_type": "CE", "strike": 100}]}})
        assert json.loads(preview.content[0].text)["data"]["preview"] is True
        replay = await client.call_tool("replay_option_protection", {"request": {"strategy_run_id": "opt-1", "metric_snapshots": [{"spot": 100}]}})
        assert "events" in replay.content[0].text
        entered = await client.call_tool("enter_option_run", {"request": {"strategy_run_id": "opt-1", "idempotency_key": "opt-entry-1234"}})
        assert "entered" in entered.content[0].text
