from datetime import datetime, timezone

from backend.workflows.runtime import build_market_session_provider


def test_mcx_session_is_feed_driven_and_uses_ist_market_date():
    provider = build_market_session_provider(None)

    active, session_id = provider(
        "mcx_commodity",
        "MCX:GOLD26OCTFUT",
        datetime(2026, 9, 9, 10, 30, tzinfo=timezone.utc),
    )

    assert active is True
    assert session_id == "MCX:2026-09-09"


def test_currency_session_is_feed_driven_without_nse_calendar_lookup():
    provider = build_market_session_provider(None)

    active, session_id = provider(
        "currency",
        "CDS:USDINR26SEP",
        datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc),
    )

    assert active is True
    assert session_id == "CDS:2026-09-10"


def test_nse_session_rejects_non_nse_instrument_before_calendar_lookup():
    provider = build_market_session_provider(None)

    active, session_id = provider(
        "nse_equity",
        "MCX:GOLD26OCTFUT",
        datetime(2026, 9, 9, 10, 30, tzinfo=timezone.utc),
    )

    assert active is False
    assert session_id == "session_mismatch:nse_equity:MCX"
