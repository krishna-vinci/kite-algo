import pytest

from backend.platform.order_alerts import (
    OrderAlertListener,
    format_order_alert,
    order_alerts_enabled,
)


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *, key, title, message, tags=()):
        self.calls.append({"key": key, "title": title, "message": message, "tags": list(tags)})


def _payload(**overrides):
    base = {
        "order_id": "240930000123456",
        "status": "COMPLETE",
        "exchange": "NSE",
        "tradingsymbol": "ITC",
        "transaction_type": "BUY",
        "product": "MIS",
        "order_type": "LIMIT",
        "quantity": 1,
        "filled_quantity": 1,
        "average_price": 412.3,
        "tag": None,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.delenv("ORDER_ALERTS_ENABLED", raising=False)


def test_complete_alerts_with_filled_quantity_and_average_price():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(_payload())

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["title"] == "Filled: BUY 1 ITC"
    assert call["message"] == "NSE ITC BUY 1 @ 412.30 (MIS, LIMIT) order 240930000123456"
    assert call["tags"] == ["white_check_mark"]
    assert call["key"] == "order:240930000123456:COMPLETE"


def test_complete_includes_strategy_tag_when_present():
    recorder = _Recorder()
    OrderAlertListener(alert=recorder)(_payload(tag="my-strategy"))

    assert "my-strategy" in recorder.calls[0]["message"]


def test_duplicate_terminal_update_alerts_once():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(_payload())
    listener(_payload())

    assert len(recorder.calls) == 1


def test_seen_cache_drops_oldest_past_bound():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder, max_seen=2)

    listener(_payload(order_id="1"))
    listener(_payload(order_id="2"))
    listener(_payload(order_id="3"))
    listener(_payload(order_id="1"))

    assert [call["key"] for call in recorder.calls] == [
        "order:1:COMPLETE",
        "order:2:COMPLETE",
        "order:3:COMPLETE",
        "order:1:COMPLETE",
    ]


def test_rejected_includes_broker_reason():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(
        _payload(
            status="REJECTED",
            transaction_type="SELL",
            quantity=50,
            filled_quantity=0,
            average_price=0.0,
            tradingsymbol="NIFTY26APR22500CE",
            status_message="RMS: blocked for insufficient funds",
        )
    )

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["title"] == "Order REJECTED: SELL 50 NIFTY26APR22500CE"
    assert "RMS: blocked for insufficient funds" in call["message"]
    assert call["tags"] == ["rotating_light"]


def test_rejected_falls_back_to_raw_status_message():
    recorder = _Recorder()

    OrderAlertListener(alert=recorder)(
        _payload(status="REJECTED", status_message=None, status_message_raw="raw rejection text")
    )

    assert "raw rejection text" in recorder.calls[0]["message"]


def test_open_and_unfilled_cancels_are_ignored():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(_payload(status="OPEN"))
    listener(_payload(status="CANCELLED", filled_quantity=0))
    listener(_payload(status="CANCELED", filled_quantity=0))
    listener(_payload(status="UPDATE"))

    assert recorder.calls == []


def test_partial_fill_cancel_alerts():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(
        _payload(
            status="CANCELLED",
            quantity=50,
            filled_quantity=20,
            average_price=100.5,
        )
    )

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["title"] == "Partial fill then cancelled: BUY 20 ITC"
    assert call["message"] == "filled 20 of 50 @ 100.50"
    assert call["tags"] == ["warning"]


def test_disabled_env_alerts_nothing(monkeypatch):
    monkeypatch.setenv("ORDER_ALERTS_ENABLED", "off")
    assert order_alerts_enabled() is False

    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)
    listener(_payload(status="REJECTED", status_message="nope"))

    assert recorder.calls == []


def test_enabled_env_defaults_true(monkeypatch):
    assert order_alerts_enabled() is True
    monkeypatch.setenv("ORDER_ALERTS_ENABLED", "TRUE")
    assert order_alerts_enabled() is True


def test_malformed_payloads_do_not_raise():
    recorder = _Recorder()
    listener = OrderAlertListener(alert=recorder)

    listener(None)
    listener({})
    listener({"order_id": "1"})
    listener({"status": "COMPLETE"})
    listener({"order_id": "1", "status": "COMPLETE", "filled_quantity": "not-a-number"})

    assert format_order_alert(None) is None
    assert format_order_alert({}) is None
    assert format_order_alert({"status": "OPEN"}) is None


def test_alert_failure_never_propagates():
    def boom(**_kwargs):
        raise RuntimeError("ntfy exploded")

    OrderAlertListener(alert=boom)(_payload())
