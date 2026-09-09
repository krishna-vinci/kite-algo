"""Task 4 tests: Telegram/ntfy adapters + message building (spec F4/F5, E-14, E-20..E-24).

All network interaction is simulated with httpx.MockTransport — no real network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Optional

import httpx
import pytest

from backend.notifications.adapters import (
    DeliveryOutcome,
    NtfyAdapter,
    TelegramAdapter,
    get_adapter,
    register_adapter,
)
from backend.notifications.message import build_message

TOKEN = "SECRET-TELEGRAM-TOKEN-9f3a"  # distinctive sentinel asserted absent from outcomes

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def make_telegram(handler) -> TelegramAdapter:
    return TelegramAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def make_ntfy(handler) -> NtfyAdapter:
    return NtfyAdapter(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def telegram_destination() -> dict:
    return {"chat_id": "4242", "token_env": "TELEGRAM_BOT_TOKEN"}


def ntfy_destination() -> dict:
    return {"url_env": "NTFY_PRIMARY_URL"}


class Capture:
    """MockTransport handler that records the request and replies with a scripted response."""

    def __init__(
        self,
        status_code: int = 200,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        text: str = "",
        raise_exc: Optional[Exception] = None,
    ):
        self.status_code = status_code
        self.json_body = json_body if json_body is not None else {"ok": True}
        self.headers = headers or {}
        self.text = text
        self.raise_exc = raise_exc
        self.request: Optional[httpx.Request] = None
        self.content: Optional[bytes] = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        self.content = request.read()
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.text:
            return httpx.Response(self.status_code, headers=self.headers, text=self.text)
        return httpx.Response(self.status_code, headers=self.headers, json=self.json_body)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def test_telegram_2xx_accepted_with_message_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(200, {"ok": True, "result": {"message_id": 987}})
    adapter = make_telegram(capture)

    outcome = asyncio.run(
        adapter.send(telegram_destination(), "subject", "body text")
    )

    assert outcome.status == "accepted"
    assert outcome.provider_id == "987"
    assert outcome.retry_after_s is None
    assert capture.request is not None
    assert capture.request.method == "POST"
    assert capture.request.url.host == "api.telegram.org"
    assert capture.request.url.path == f"/bot{TOKEN}/sendMessage"
    payload = __import__("json").loads(capture.request.read())
    assert payload["chat_id"] == "4242"
    assert "body text" in payload["text"]


def test_telegram_429_honors_body_retry_after(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 7",
            "parameters": {"retry_after": 7},
        },
    )
    adapter = make_telegram(capture)

    outcome = asyncio.run(
        adapter.send(telegram_destination(), "s", "b")
    )

    assert outcome.status == "retryable"
    assert outcome.retry_after_s == 7


def test_telegram_429_falls_back_to_retry_after_header(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(429, {"ok": False}, headers={"Retry-After": "11"})
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "retryable"
    assert outcome.retry_after_s == 11


def test_telegram_429_default_retry_after_5(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(429, {"ok": False})
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "retryable"
    assert outcome.retry_after_s == 5


@pytest.mark.parametrize("code", [400, 401, 403, 404, 418])
def test_telegram_4xx_permanent(monkeypatch, code):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(code, {"ok": False, "description": "Bad Request: something"})
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "permanent"
    assert outcome.retry_after_s is None


def test_telegram_5xx_retryable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(503, {"ok": False, "description": "Bad Gateway"})
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "retryable"


def test_telegram_timeout_unknown(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(raise_exc=httpx.TimeoutException("timed out"))
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "unknown"
    assert outcome.provider_id is None


def test_telegram_connect_error_unknown(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(raise_exc=httpx.ConnectError("connection refused"))
    adapter = make_telegram(capture)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))

    assert outcome.status == "unknown"


def test_telegram_5000_char_body_truncated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    capture = Capture(200, {"ok": True, "result": {"message_id": 1}})
    adapter = make_telegram(capture)

    outcome = asyncio.run(
        adapter.send(telegram_destination(), "subject line", "x" * 5000)
    )

    assert outcome.status == "accepted"
    text_field = __import__("json").loads(capture.request.read())["text"]
    assert len(text_field) <= 4096
    assert text_field.endswith("…[truncated]")
    assert "subject line" in text_field


def test_telegram_missing_env_permanent_names_env_var(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    called = Capture()
    adapter = make_telegram(called)

    outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))  # must not raise

    assert outcome.status == "permanent"
    assert "TELEGRAM_BOT_TOKEN" in outcome.detail
    assert called.request is None  # no HTTP call attempted


def test_telegram_outcomes_never_leak_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    scenarios = [
        Capture(200, {"ok": True, "result": {"message_id": 5}}),
        Capture(429, {"ok": False, "parameters": {"retry_after": 3}}),
        Capture(400, {"ok": False, "description": "Bad Request: chat not found"}),
        Capture(401, {"ok": False, "description": "Unauthorized"}),
        Capture(500, {"ok": False, "description": "internal error"}),
        Capture(raise_exc=httpx.TimeoutException("t")),
        Capture(),
    ]
    for capture in scenarios:
        adapter = make_telegram(capture)
        outcome = asyncio.run(adapter.send(telegram_destination(), "s", "b"))
        assert isinstance(outcome, DeliveryOutcome)
        assert TOKEN not in (outcome.detail or "")
        assert TOKEN not in (outcome.provider_id or "")


def test_telegram_default_client_when_none_given(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    adapter = TelegramAdapter()  # default construction: owns its own AsyncClient
    assert adapter.provider == "telegram"


# --------------------------------------------------------------------------
# ntfy
# --------------------------------------------------------------------------


def test_ntfy_2xx_accepted_with_id_header(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(200, headers={"X-Ntfy-Id": "abc123"})
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "price alert", "the body"))

    assert outcome.status == "accepted"
    assert outcome.provider_id == "abc123"
    assert capture.request.url == httpx.URL("https://ntfy.example.com/alerts")
    assert capture.request.headers["Title"] == "price alert"
    assert capture.content == b"the body"


def test_ntfy_2xx_without_id_header(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(200)
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "accepted"
    assert outcome.provider_id is None


def test_ntfy_429_parses_retry_after_header(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(429, headers={"Retry-After": "9"})
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "retryable"
    assert outcome.retry_after_s == 9


def test_ntfy_429_default_retry_after_5(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(429)
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "retryable"
    assert outcome.retry_after_s == 5


def test_ntfy_5xx_retryable(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(502, text="bad gateway")
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "retryable"


def test_ntfy_4xx_permanent(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(403, text="forbidden")
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "permanent"


def test_ntfy_timeout_unknown(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(raise_exc=httpx.TimeoutException("timed out"))
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "unknown"


def test_ntfy_connect_error_unknown(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(raise_exc=httpx.ConnectError("no route"))
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))

    assert outcome.status == "unknown"


def test_ntfy_body_truncated(monkeypatch):
    monkeypatch.setenv("NTFY_PRIMARY_URL", "https://ntfy.example.com/alerts")
    capture = Capture(200)
    adapter = make_ntfy(capture)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "y" * 5000))

    assert outcome.status == "accepted"
    body = capture.content.decode()
    assert len(body) <= 4096
    assert body.endswith("…[truncated]")


def test_ntfy_missing_env_permanent_names_env_var(monkeypatch):
    monkeypatch.delenv("NTFY_PRIMARY_URL", raising=False)
    called = Capture()
    adapter = make_ntfy(called)

    outcome = asyncio.run(adapter.send(ntfy_destination(), "t", "b"))  # must not raise

    assert outcome.status == "permanent"
    assert "NTFY_PRIMARY_URL" in outcome.detail
    assert called.request is None


# --------------------------------------------------------------------------
# Adapter registry
# --------------------------------------------------------------------------


def test_get_adapter_known_providers():
    assert get_adapter("telegram").provider == "telegram"
    assert get_adapter("ntfy").provider == "ntfy"


def test_get_adapter_unknown_provider_raises_value_error():
    with pytest.raises(ValueError):
        get_adapter("sms")


def test_register_adapter_override_and_restore():
    class FakeAdapter:
        provider = "fake"

        async def send(self, destination, subject, body):
            return DeliveryOutcome(status="accepted")

    previous = register_adapter("fake", FakeAdapter)
    try:
        adapter = get_adapter("fake")
        assert isinstance(adapter, FakeAdapter)
    finally:
        register_adapter("fake", previous)  # restore (None removes)

    assert previous is None
    with pytest.raises(ValueError):
        get_adapter("fake")


# --------------------------------------------------------------------------
# message building
# --------------------------------------------------------------------------


FIRED_AT = dt.datetime(2026, 9, 8, 9, 0, 0, tzinfo=dt.timezone.utc)  # 14:30 IST


def test_build_message_default_subject_and_body():
    subject, body = build_message(
        rule_name="breakout",
        instrument_key="NSE:RELIANCE",
        evidence={"ltp": 3002.5, "level": 3000, "prev_ltp": 2999.1},
        fired_at=FIRED_AT,
        event_id="evt-42",
    )

    assert subject == "[Alert] breakout: NSE:RELIANCE"
    assert "rule: breakout" in body
    assert "symbol: NSE:RELIANCE" in body
    assert "values: " in body and "ltp=3002.5" in body and "level=3000" in body
    assert "time: 2026-09-08 14:30:00 IST" in body
    assert "2026-09-08 09:00:00 UTC" in body
    assert "evt-42" in body


def test_build_message_includes_condition_and_timeframe_from_evidence():
    _, body = build_message(
        rule_name="candle-rule",
        instrument_key="NSE:TCS",
        evidence={"ltp": 1.0, "condition": "ltp crosses_above 3000", "timeframe": "5m"},
        fired_at=FIRED_AT,
    )

    assert "condition: ltp crosses_above 3000" in body
    assert "timeframe: 5m" in body


def test_build_message_template_override_and_missing_placeholder_dash():
    subject, body = build_message(
        rule_name="breakout",
        instrument_key="NSE:RELIANCE",
        evidence={"ltp": 3002.5},  # no level
        fired_at=FIRED_AT,
        template="${symbol} broke ${level} at ltp ${ltp} (${rule}) id=${event_id} at ${time}",
        event_id="evt-7",
    )

    assert subject == "[Alert] breakout: NSE:RELIANCE"  # subject stays default
    assert "NSE:RELIANCE broke - at ltp 3002.5 (breakout)" in body
    assert "id=evt-7" in body
    assert "2026-09-08 14:30:00 IST" in body
    assert "${" not in body


def test_build_message_caps_subject_and_body():
    evidence = {"blob": "z" * 6000}
    subject, body = build_message(
        rule_name="r" * 300,
        instrument_key="NSE:LONG" + "X" * 300,
        evidence=evidence,
        fired_at=FIRED_AT,
    )

    assert len(subject) <= 120
    assert len(body) <= 3800
    assert body.endswith("…[truncated]")
