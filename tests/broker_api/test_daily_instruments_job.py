"""The daily instruments job must call the orchestrator with every required argument.

A missing ``reindex`` argument made the 07:00 job raise ``TypeError`` every day,
so the instrument catalog silently went stale.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest import mock

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import backend.api.routers  # noqa: E402,F401 - resolves the broker_api import cycle
from backend.broker_api import broker_api  # noqa: E402


def test_daily_job_calls_the_orchestrator_with_every_required_argument():
    captured = {}
    signature = inspect.signature(broker_api.sync_and_reindex_orchestrator)

    async def fake_orchestrator(**kwargs):
        signature.bind(**kwargs)  # raises TypeError if a required argument is missing
        captured.update(kwargs)
        return {"refreshed": 1}

    with mock.patch.object(broker_api, "sync_and_reindex_orchestrator", side_effect=fake_orchestrator), \
         mock.patch.object(broker_api, "SessionLocal", return_value=mock.MagicMock()), \
         mock.patch.object(broker_api, "send_ntfy_notification", new=mock.AsyncMock()):
        asyncio.run(broker_api.update_all_instruments_daily())

    assert captured["refresh_from_broker"] is True
    assert captured["reindex"] is False
