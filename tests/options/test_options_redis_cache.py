from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import date, datetime, timezone
from typing import Any, cast

import pytest

sys.modules.setdefault("mibian", types.ModuleType("mibian"))
if "numba" not in sys.modules:
    numba_stub: Any = types.ModuleType("numba")

    def _njit(*_args, **_kwargs):
        def _decorator(fn):
            return fn

        return _decorator

    numba_stub.njit = _njit
    sys.modules["numba"] = numba_stub

import backend.broker_api.options.options_sessions as options_sessions
from backend.broker_api.options.options_sessions import (
    MIN_T,
    OptionsSession,
    OptionsSessionManager,
)
from backend.options.market.redis_cache import (
    OPTION_SNAPSHOT_SCHEMA_VERSION,
    OPTION_SNAPSHOT_TTL_SECONDS,
    option_snapshot_v1_key,
    option_snapshot_v1_updates_channel,
    read_option_snapshot_from_redis,
    serialize_option_snapshot_v1,
)


class _FakeRedis:
    def __init__(self):
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.publish_calls: list[tuple[str, str]] = []
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None):
        self.set_calls.append((key, value, ex))
        self.values[key] = value

    async def publish(self, channel: str, payload: str):
        self.publish_calls.append((channel, payload))

    async def get(self, key: str):
        return self.values.get(key)


class _FakeInstrumentRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), None


class _FakeMarketData:
    async def set_owner_subscriptions(self, *_args, **_kwargs):
        return None

    async def delete_owner(self, *_args, **_kwargs):
        return None


class _SessionStub:
    def __init__(self, underlying: str, snapshot: dict):
        self.underlying = underlying
        self.snapshot = snapshot


def test_v1_key_and_channel_builders_uppercase_underlying():
    assert option_snapshot_v1_key("nifty") == "options:chain:v1:NIFTY"
    assert option_snapshot_v1_updates_channel("banknifty") == "options:chain:v1:updates:BANKNIFTY"


def test_serialize_payload_is_valid_json_with_schema_v1_and_json_safe_dates():
    snapshot = {
        "updated_at": datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
        "cadence_sec": 5,
        "expiries": [date(2026, 5, 7)],
        "per_expiry": {"2026-05-07": {"rows": []}},
        "desired_token_count": 3,
    }

    raw = serialize_option_snapshot_v1(snapshot, "nifty")
    payload = json.loads(raw)

    assert payload["schema_version"] == 1
    assert payload["schema_version"] == OPTION_SNAPSHOT_SCHEMA_VERSION
    assert payload["underlying"] == "NIFTY"
    assert payload["updated_at"] == "2026-04-29 10:00:00+00:00"
    assert payload["expiries"] == ["2026-05-07"]


def test_session_update_writes_v1_json_and_no_legacy_snapshot_or_publish(monkeypatch):
    fake_redis = _FakeRedis()

    monkeypatch.setattr("backend.broker_api.options.options_sessions.get_redis", lambda: fake_redis)

    manager = OptionsSessionManager(
        market_data=cast(Any, _FakeMarketData()),
        instrument_repo=cast(Any, _FakeInstrumentRepo()),
    )

    async def _noop_converge():
        return None

    manager._converge_subscriptions = _noop_converge

    snapshot = {
        "underlying": "nifty",
        "updated_at": "2026-04-29T10:00:00Z",
        "cadence_sec": 5,
        "expiries": ["2026-05-07"],
        "per_expiry": {"2026-05-07": {"rows": []}},
        "desired_token_count": 1,
    }

    asyncio.run(manager.on_session_update(cast(Any, _SessionStub("nifty", snapshot))))

    v1_key = "options:chain:v1:NIFTY"
    legacy_key = "options:snapshot:nifty"
    assert any(call[0] == v1_key and call[2] == OPTION_SNAPSHOT_TTL_SECONDS for call in fake_redis.set_calls)
    # Nothing reads the str(dict) legacy key; it is no longer written.
    assert not any(call[0] == legacy_key for call in fake_redis.set_calls)

    stored_v1 = fake_redis.values[v1_key]
    assert isinstance(stored_v1, str)
    assert json.loads(stored_v1)["schema_version"] == OPTION_SNAPSHOT_SCHEMA_VERSION

    assert fake_redis.publish_calls == [
        ("options:chain:v1:updates:NIFTY", stored_v1)
    ]


def test_session_update_skips_unchanged_market_payload_but_refreshes_set(monkeypatch):
    fake_redis = _FakeRedis()
    now = 100.0
    monkeypatch.setattr("backend.broker_api.options.options_sessions.get_redis", lambda: fake_redis)
    monkeypatch.setattr(options_sessions.time, "monotonic", lambda: now)
    manager = OptionsSessionManager(
        market_data=cast(Any, _FakeMarketData()),
        instrument_repo=cast(Any, _FakeInstrumentRepo()),
    )

    async def _noop_converge():
        return None

    manager._converge_subscriptions = _noop_converge
    snapshot = {
        "updated_at": "2026-04-29T10:00:00Z",
        "health": {"spot_age_sec": 1.0},
        "per_expiry": {
            "2026-05-07": {
                "forward": 25010.0,
                "rows": [
                    {
                        "strike": 25000.0,
                        "CE": {"ltp": 100.0, "iv": 0.15, "oi": 1000},
                        "PE": {"ltp": 90.0, "iv": 0.15, "oi": 900},
                    }
                ],
            }
        },
    }
    session = cast(Any, _SessionStub("NIFTY", snapshot))

    asyncio.run(manager.on_session_update(session))
    snapshot["updated_at"] = "2026-04-29T10:00:01Z"
    snapshot["health"]["spot_age_sec"] = 2.0
    now = 101.0
    asyncio.run(manager.on_session_update(session))
    assert len(fake_redis.set_calls) == 1
    assert len(fake_redis.publish_calls) == 1

    now = 105.0
    asyncio.run(manager.on_session_update(session))
    assert len(fake_redis.set_calls) == 2
    assert len(fake_redis.publish_calls) == 1

    snapshot["per_expiry"]["2026-05-07"]["rows"][0]["CE"]["ltp"] = 101.0
    now = 105.1
    asyncio.run(manager.on_session_update(session))
    assert len(fake_redis.set_calls) == 3
    assert len(fake_redis.publish_calls) == 2


def test_reader_decodes_v1_json_and_rejects_invalid_json_or_schema():
    redis_client = _FakeRedis()
    key = option_snapshot_v1_key("nifty")

    redis_client.values[key] = json.dumps(
        {
            "schema_version": 1,
            "underlying": "NIFTY",
            "updated_at": "2026-04-29T10:00:00Z",
            "cadence_sec": 5,
            "expiries": [],
            "per_expiry": {},
            "desired_token_count": 0,
        }
    )
    decoded = asyncio.run(read_option_snapshot_from_redis(redis_client, "nifty"))
    assert decoded is not None
    assert decoded["underlying"] == "NIFTY"

    redis_client.values[key] = "{bad-json"
    assert asyncio.run(read_option_snapshot_from_redis(redis_client, "nifty")) is None

    redis_client.values[key] = json.dumps({"schema_version": 2})
    assert asyncio.run(read_option_snapshot_from_redis(redis_client, "nifty")) is None


def _freeze_options_session_clock(monkeypatch, moment_utc: datetime) -> None:
    """Freeze the module-level ``datetime.now`` used by time-to-expiry."""
    real_datetime = options_sessions.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return moment_utc.astimezone(timezone.utc).replace(tzinfo=None)
            return moment_utc.astimezone(tz)

    monkeypatch.setattr(options_sessions, "datetime", _FrozenDatetime)


def test_time_to_expiry_anchors_to_1530_ist_on_expiry_day(monkeypatch):
    session = OptionsSession("NIFTY", cast(Any, object()))
    expiry = date(2026, 5, 7)

    # 15:00 IST on the expiry day == 09:30 UTC, so 30 minutes of life remain.
    _freeze_options_session_clock(monkeypatch, datetime(2026, 5, 7, 9, 30, tzinfo=timezone.utc))
    time_to_expiry = session._time_to_expiry(expiry)

    expected = (30 * 60) / (365.0 * 24 * 60 * 60)
    assert time_to_expiry == pytest.approx(expected, rel=1e-9)
    # A UTC anchor would report ~6h of life here (T ~ 6.8e-4).
    assert time_to_expiry < 1e-4


def test_time_to_expiry_floors_after_1530_ist_on_expiry_day(monkeypatch):
    session = OptionsSession("NIFTY", cast(Any, object()))
    expiry = date(2026, 5, 7)

    # 15:45 IST on the expiry day == 10:15 UTC: the contract has expired.
    _freeze_options_session_clock(monkeypatch, datetime(2026, 5, 7, 10, 15, tzinfo=timezone.utc))

    assert session._time_to_expiry(expiry) == MIN_T


def test_v1_payload_carries_session_health():
    snapshot = {
        "updated_at": "2026-09-28T04:00:00Z",
        "health": {"spot_live": False, "spot_age_sec": None, "dropped_tokens": 3},
    }
    payload = json.loads(serialize_option_snapshot_v1(snapshot, "nifty"))
    assert payload["health"] == {"spot_live": False, "spot_age_sec": None, "dropped_tokens": 3}
