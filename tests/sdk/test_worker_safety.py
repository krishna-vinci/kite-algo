from datetime import datetime, timedelta, timezone

from backend.api.services.safety import (
    build_safety_fingerprint,
    build_signed_safety_token,
    option_run_status_blocks_trading,
    verify_signed_safety_token,
)


def test_build_safety_fingerprint_changes_when_run_or_protection_changes():
    base = build_safety_fingerprint(
        run_status="open",
        generic_status="active",
        generic_exit_submitted=False,
        option_run_status="entered",
    )
    changed = build_safety_fingerprint(
        run_status="open",
        generic_status="triggered",
        generic_exit_submitted=False,
        option_run_status="entered",
    )
    assert base != changed


def test_signed_safety_token_round_trip_and_expiry():
    now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    token = build_signed_safety_token(
        strategy_run_id="run-1",
        fingerprint="abc123",
        secret="secret-key",
        now=now,
        ttl_seconds=10,
    )
    payload = verify_signed_safety_token(
        token,
        strategy_run_id="run-1",
        secret="secret-key",
        now=now + timedelta(seconds=5),
    )
    assert payload is not None
    assert payload["strategy_run_id"] == "run-1"
    assert payload["fingerprint"] == "abc123"


def test_signed_safety_token_rejects_expired_or_wrong_run():
    now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    token = build_signed_safety_token(
        strategy_run_id="run-1",
        fingerprint="abc123",
        secret="secret-key",
        now=now,
        ttl_seconds=1,
    )
    assert verify_signed_safety_token(token, "run-2", "secret-key", now=now) is None
    assert verify_signed_safety_token(token, "run-1", "secret-key", now=now + timedelta(seconds=2)) is None


def test_option_run_status_projection_is_conservative():
    assert option_run_status_blocks_trading("entered") is False
    assert option_run_status_blocks_trading("partial_entry") is False
    assert option_run_status_blocks_trading("exiting") is True
    assert option_run_status_blocks_trading("cleanup_required") is True
