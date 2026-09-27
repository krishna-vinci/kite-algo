import asyncio
from unittest import mock

import pytest

from backend.platform import owner_alerts


@pytest.fixture(autouse=True)
def _reset():
    owner_alerts.reset_for_tests()
    yield
    owner_alerts.reset_for_tests()


def test_alert_sends_once_per_key_within_cooldown():
    sent = []

    async def fake_send(message, title="", tags=None):
        sent.append((title, message, tags))

    with mock.patch("backend.platform.owner_alerts._send", new=fake_send):
        assert asyncio.run(owner_alerts.alert_owner(key="k1", title="T", message="m", tags=["bell"])) is True
        assert asyncio.run(owner_alerts.alert_owner(key="k1", title="T", message="m")) is False
        assert asyncio.run(owner_alerts.alert_owner(key="k2", title="T", message="m")) is True
    assert [row[0] for row in sent] == ["T", "T"]


def test_a_failing_transport_never_raises():
    async def boom(*_args, **_kwargs):
        raise RuntimeError("ntfy down")

    with mock.patch("backend.platform.owner_alerts._send", new=boom):
        assert asyncio.run(owner_alerts.alert_owner(key="k3", title="T", message="m")) is False


def test_nowait_from_sync_code_sends_in_background():
    sent = []

    async def fake_send(message, title="", tags=None):
        sent.append(title)

    with mock.patch("backend.platform.owner_alerts._send", new=fake_send):
        owner_alerts.alert_owner_nowait(key="k4", title="Sync", message="m")
        owner_alerts.alert_owner_nowait(key="k4", title="Sync", message="m")
        owner_alerts.join_background_for_tests(timeout=2.0)
    assert sent == ["Sync"]


def test_long_text_is_truncated():
    captured = {}

    async def fake_send(message, title="", tags=None):
        captured["message"], captured["title"] = message, title

    with mock.patch("backend.platform.owner_alerts._send", new=fake_send):
        asyncio.run(owner_alerts.alert_owner(key="k5", title="x" * 500, message="y" * 9000))
    assert len(captured["title"]) <= 200 and len(captured["message"]) <= 4096
