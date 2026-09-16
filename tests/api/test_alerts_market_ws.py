"""Operator live-price websocket: policy, bounding, and cleanup.

Two layers are tested here, deliberately:

* **handshake policy** through the real route with the real scope logic —
  cookie, Origin (present, foreign, absent) and scope refusal;
* **stream behaviour** through the real connection object, with fakes for Redis,
  the catalog and the market-runtime client, so every assertion is about a
  contract the UI depends on: canonical resolution, the per-connection cap,
  coalescing, snapshots that tell the truth about age, backpressure, freshness
  ageing and owner cleanup.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from backend.alerts import market_stream as stream_module
from backend.alerts.market_stream import (
    FRESHNESS_CLOSED,
    FRESHNESS_DELAYED,
    FRESHNESS_LIVE,
    FRESHNESS_NO_DATA,
    FRESHNESS_STALE,
    MarketStreamHub,
    OperatorMarketStream,
    StreamLimits,
    build_quote,
    classify_freshness,
    exchange_session_state,
)
from backend.api.routers import alerts_market_ws as ws_router
from backend.app.auth import AppUser

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # 17:30 IST: MCX open, NSE closed
ALLOWED_ORIGIN = "http://localhost:3000"
SCOPE = "app:admin"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakePubSub:
    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages = messages or []
        self.subscribed: List[str] = []

    async def subscribe(self, *channels: str) -> None:
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self.subscribed = [item for item in self.subscribed if item not in channels]

    async def get_message(self, *, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
        if self.messages:
            return {"type": "message", "data": json.dumps(self.messages.pop(0))}
        await asyncio.sleep(0.01)
        return None

    async def aclose(self) -> None:
        return None


class FakeRedis:
    def __init__(self, ticks: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.ticks = ticks or {}
        self.mget_calls: List[List[str]] = []

    def pubsub(self) -> FakePubSub:
        return FakePubSub()

    async def mget(self, keys: List[str]):
        self.mget_calls.append(list(keys))
        out = []
        for key in keys:
            token = str(key).rsplit(":", 1)[-1]
            payload = self.ticks.get(token)
            out.append(json.dumps(payload) if payload is not None else None)
        return out


class FakeCatalog:
    def __init__(self, descriptors: Dict[str, Any]) -> None:
        self.descriptors = descriptors
        self.resolved: List[str] = []

    def resolve_public_key(self, public_key: str):
        self.resolved.append(public_key)
        if public_key not in self.descriptors:
            raise KeyError(public_key)
        return self.descriptors[public_key]


class FakeRuntimeClient:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.owner_tokens: Dict[str, Dict[int, str]] = {}

    async def set_owner_subscriptions(self, owner_id: str, subscriptions: Dict[int, str]):
        self.calls.append(("put", owner_id, dict(subscriptions)))
        self.owner_tokens[owner_id] = dict(subscriptions)
        return {"owner_id": owner_id}

    async def delete_owner(self, owner_id: str):
        self.calls.append(("delete", owner_id, None))
        self.owner_tokens.pop(owner_id, None)
        return {"status": "ok"}


class FakeWebSocket:
    """Just enough socket for the connection object."""

    def __init__(self, incoming: Optional[List[Any]] = None) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.incoming = list(incoming or [])
        self.closed: List[tuple] = []
        self._received = asyncio.Event()

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent.append(payload)

    async def receive_json(self):
        if not self.incoming:
            await asyncio.sleep(3600)
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))
        self.incoming.append(WebSocketDisconnect(code))


def descriptor(token: int, *, exchange: str = "MCX", lifecycle: str = "active", generation: str = "gen-1"):
    return SimpleNamespace(
        broker_token=token,
        exchange=exchange,
        lifecycle_status=lifecycle,
        catalog_generation=generation,
    )


def tick_payload(token: int, price: float, *, age_s: float = 0.5, close: Optional[float] = None):
    received = NOW - timedelta(seconds=age_s)
    payload: Dict[str, Any] = {
        "instrument_token": token,
        "last_price": price,
        "received_at": received.isoformat(),
        "exchange_timestamp": received.isoformat(),
        "volume_traded": 100,
    }
    if close is not None:
        payload["ohlc"] = {"open": close, "high": price, "low": close, "close": close}
    return payload


def make_stream(
    *,
    redis_client: FakeRedis,
    catalog: FakeCatalog,
    runtime: FakeRuntimeClient,
    limits: StreamLimits,
    incoming: Optional[List[Any]] = None,
    clock=lambda: NOW,
    websocket: Optional[FakeWebSocket] = None,
) -> tuple:
    hub = MarketStreamHub(redis_client=redis_client, limits=limits, clock=clock)
    socket = websocket or FakeWebSocket(incoming)
    stream = OperatorMarketStream(
        socket,
        hub=hub,
        scope=SCOPE,
        catalog=catalog,
        runtime_client_factory=lambda: runtime,
        redis_client=redis_client,
        limits=limits,
        clock=clock,
        calendar_loader=lambda exchange, day: {"2026-09-15"},
    )
    return stream, hub, socket


def lim(**kwargs) -> StreamLimits:
    base = dict(
        max_instruments=5,
        flush_ms=20,
        queue_max=64,
        live_max_s=5.0,
        delayed_max_s=30.0,
        owner_renew_s=20.0,
        idle_timeout_s=300.0,
        heartbeat_s=30.0,
        state_s=30.0,
    )
    base.update(kwargs)
    return StreamLimits(**base)


async def pump(stream: OperatorMarketStream, socket: FakeWebSocket) -> List[Dict[str, Any]]:
    """Move everything currently queued to the socket, deterministically.

    The sender loop only runs under ``run()``; tests that drive the connection
    object directly use this instead of waiting on timers, so a failure is never
    a scheduling accident.
    """
    frames: List[Dict[str, Any]] = []
    while not stream._queue.empty():
        frame = stream._queue.get_nowait()
        frames.append(frame)
        await socket.send_json(frame)
    return frames


# ---------------------------------------------------------------------------
# handshake policy (real route, real scope logic)
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("ALERTS_OPERATOR_SCOPES", "app:admin,app:other")
    monkeypatch.delenv("ALERTS_OPERATOR_OWNER", raising=False)
    application = FastAPI()
    application.include_router(ws_router.router, prefix="/api")
    return application


def _patch_user(monkeypatch, user: Optional[AppUser]):
    monkeypatch.setattr(ws_router, "get_optional_app_user", lambda _ws: user)


def _patch_hub(monkeypatch, hub: MarketStreamHub, recorded: Optional[list] = None):
    async def _hub():
        return hub

    monkeypatch.setattr(ws_router, "get_market_stream_hub", _hub)
    real = ws_router.OperatorMarketStream

    def factory(websocket, **kwargs):
        stream = real(websocket, **kwargs)
        if recorded is not None:
            recorded.append(stream)
        return stream

    monkeypatch.setattr(ws_router, "OperatorMarketStream", factory)


def test_missing_cookie_is_refused(app, monkeypatch):
    _patch_user(monkeypatch, None)
    hub = MarketStreamHub(redis_client=FakeRedis(), limits=lim())
    _patch_hub(monkeypatch, hub)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            f"/api/alerts/market/ws?scope={SCOPE}", headers={"origin": ALLOWED_ORIGIN}
        ):
            pass


def test_foreign_origin_is_refused(app, monkeypatch):
    _patch_user(monkeypatch, AppUser(username="admin", role="admin"))
    hub = MarketStreamHub(redis_client=FakeRedis(), limits=lim())
    _patch_hub(monkeypatch, hub)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            f"/api/alerts/market/ws?scope={SCOPE}", headers={"origin": "http://evil.example"}
        ):
            pass


def test_absent_origin_is_refused(app, monkeypatch):
    """A browser always sends Origin; its absence is refused rather than trusted."""
    _patch_user(monkeypatch, AppUser(username="admin", role="admin"))
    hub = MarketStreamHub(redis_client=FakeRedis(), limits=lim())
    _patch_hub(monkeypatch, hub)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/api/alerts/market/ws?scope={SCOPE}"):
            pass


def test_unauthorized_scope_is_refused(app, monkeypatch):
    _patch_user(monkeypatch, AppUser(username="admin", role="admin"))
    hub = MarketStreamHub(redis_client=FakeRedis(), limits=lim())
    _patch_hub(monkeypatch, hub)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            "/api/alerts/market/ws?scope=worker:some-token",
            headers={"origin": ALLOWED_ORIGIN},
        ):
            pass


def test_authorized_connection_receives_welcome_and_cleans_up(app, monkeypatch):
    """The happy path, plus proof that the runtime owner is released on close."""
    _patch_user(monkeypatch, AppUser(username="admin", role="admin"))
    recorded: list = []
    redis_client = FakeRedis()
    runtime = FakeRuntimeClient()
    monkeypatch.setattr(
        "backend.broker_api.core.redis_events.get_redis", lambda: redis_client
    )
    monkeypatch.setattr(
        "backend.broker_api.instruments.catalog.InstrumentCatalog",
        lambda *a, **k: FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)}),
    )
    async def _runtime_accessor():
        # mirrors the production accessor, which is a coroutine
        return runtime

    monkeypatch.setattr(
        "backend.broker_api.orders.market_runtime_client.get_market_runtime_client",
        _runtime_accessor,
    )
    hub = MarketStreamHub(redis_client=redis_client, limits=lim())
    _patch_hub(monkeypatch, hub, recorded)

    with TestClient(app).websocket_connect(
        f"/api/alerts/market/ws?scope={SCOPE}", headers={"origin": ALLOWED_ORIGIN}
    ) as socket:
        socket.send_json({"type": "subscribe", "instruments": ["MCX:GOLD26DECFUT"]})
        welcome = socket.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["scope"] == SCOPE
        assert welcome["limits"]["max_instruments"] == 5
        ack = None
        for _ in range(6):
            frame = socket.receive_json()
            if frame["type"] == "subscriptions":
                ack = frame
                break
        assert ack is not None and ack["instruments"] == ["MCX:GOLD26DECFUT"]
        owner = recorded[0].owner_id
        assert f"alerts-ui:{SCOPE}:" in owner
        assert runtime.calls and runtime.calls[0][0] == "put"

    # after the context exits the connection is torn down and the owner released
    assert any(call[0] == "delete" and call[1] == owner for call in runtime.calls)
    assert runtime.owner_tokens == {}


# ---------------------------------------------------------------------------
# subscription behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_resolution_cap_and_snapshots():
    redis_client = FakeRedis({"111": tick_payload(111, 124_860.0, age_s=1.0, close=124_000.0)})
    catalog = FakeCatalog(
        {
            "MCX:GOLD26DECFUT": descriptor(111),
            "MCX:SILVER26DECFUT": descriptor(222),
            "MCX:ZINC26DECFUT": descriptor(333),
        }
    )
    runtime = FakeRuntimeClient()
    stream, _hub, socket = make_stream(
        redis_client=redis_client, catalog=catalog, runtime=runtime, limits=lim(max_instruments=2)
    )
    await stream.subscribe(["MCX:GOLD26DECFUT", "MCX:SILVER26DECFUT", "MCX:ZINC26DECFUT"])
    await pump(stream, socket)

    owner = runtime.owner_tokens[stream.owner_id]
    assert owner == {111: "ltp", 222: "ltp"}  # third refused, not silently dropped
    limit_errors = [f for f in socket.sent if f.get("code") == "INSTRUMENT_LIMIT"]
    assert [e["instrument"] for e in limit_errors] == ["MCX:ZINC26DECFUT"]
    snapshot = [f for f in socket.sent if f.get("origin") == "snapshot"]
    assert len(snapshot) == 1
    assert snapshot[0]["instrument_key"] == "MCX:GOLD26DECFUT"
    assert snapshot[0]["last_price"] == 124_860.0
    # 1s old at 17:30 IST on an open MCX session is LIVE, and says so honestly
    assert snapshot[0]["freshness"] == FRESHNESS_LIVE
    assert snapshot[0]["age_ms"] == 1000


@pytest.mark.asyncio
async def test_bare_symbol_and_unknown_and_inactive_are_refused_without_owner_write():
    catalog = FakeCatalog(
        {
            "MCX:GOLD26DECFUT": descriptor(111),
            "MCX:EXPIRED26JANFUT": descriptor(444, lifecycle="expired"),
        }
    )
    runtime = FakeRuntimeClient()
    stream, _hub, socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim()
    )
    await stream.subscribe(["GOLD", "NSE:NOPE", "MCX:EXPIRED26JANFUT"])
    await pump(stream, socket)

    codes = {frame.get("code") for frame in socket.sent if frame.get("type") == "error"}
    assert codes == {"INSTRUMENT_KEY_NOT_CANONICAL", "INSTRUMENT_UNKNOWN", "INSTRUMENT_INACTIVE"}
    assert runtime.calls == []  # nothing registered for refused instruments
    assert stream.tokens() == set()


@pytest.mark.asyncio
async def test_unsubscribe_resyncs_the_owner():
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111), "MCX:SILVER26DECFUT": descriptor(222)})
    runtime = FakeRuntimeClient()
    stream, _hub, _socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim()
    )
    await stream.subscribe(["MCX:GOLD26DECFUT", "MCX:SILVER26DECFUT"])
    await stream.unsubscribe(["MCX:GOLD26DECFUT"])

    assert runtime.owner_tokens[stream.owner_id] == {222: "ltp"}
    assert [call[0] for call in runtime.calls] == ["put", "put"]


@pytest.mark.asyncio
async def test_empty_subscriptions_are_reported_to_the_runtime():
    """Subscribing to nothing must still clear the previous set."""
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, _socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim()
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    await stream.unsubscribe(["MCX:GOLD26DECFUT"])
    assert runtime.owner_tokens[stream.owner_id] == {}


# ---------------------------------------------------------------------------
# ticks, coalescing, backpressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticks_are_coalesced_and_the_newest_price_wins():
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim()
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    await pump(stream, socket)
    socket.sent.clear()

    for price in (100.0, 101.0, 102.0):
        stream.offer_tick(111, tick_payload(111, price))
    assert stream.health["ticks_coalesced"] == 2  # three ticks, one frame
    stream.flush()
    await pump(stream, socket)

    quotes = [f for f in socket.sent if f.get("type") == "quote"]
    assert len(quotes) == 1
    assert quotes[0]["last_price"] == 102.0


@pytest.mark.asyncio
async def test_backpressure_drops_tick_frames_and_keeps_control_frames():
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, _socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim(queue_max=3)
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    stream._queue = asyncio.Queue(maxsize=3)

    # fill with control frames
    for index in range(3):
        assert stream.enqueue({"type": "state", "seq": index}) is True
    assert stream.enqueue({"type": "quote", "last_price": 1.0}, kind="tick") is False
    assert stream.health["ticks_dropped"] == 1
    assert stream._queue.qsize() == 3  # nothing evicted a control frame

    # a tick-heavy queue evicts the oldest tick, not a control frame
    stream._queue = asyncio.Queue(maxsize=2)
    stream.enqueue({"type": "quote", "last_price": 1.0}, kind="tick")
    stream.enqueue({"type": "state", "seq": 9})
    assert stream.enqueue({"type": "quote", "last_price": 2.0}, kind="tick") is True
    frames = [stream._queue.get_nowait() for _ in range(stream._queue.qsize())]
    assert {"type": "state", "seq": 9} in frames
    assert {"type": "quote", "last_price": 2.0} in frames
    assert {"type": "quote", "last_price": 1.0} not in frames


# ---------------------------------------------------------------------------
# freshness and session state
# ---------------------------------------------------------------------------


def test_freshness_vocabulary():
    assert classify_freshness(age_s=0.4, session_state="open") == FRESHNESS_LIVE
    assert classify_freshness(age_s=12.0, session_state="open") == FRESHNESS_DELAYED
    assert classify_freshness(age_s=120.0, session_state="open") == FRESHNESS_STALE
    assert classify_freshness(age_s=1.0, session_state="closed") == FRESHNESS_CLOSED
    assert classify_freshness(age_s=None, session_state="open") == FRESHNESS_NO_DATA
    # a closed market with no data is still "no data": there is nothing to age
    assert classify_freshness(age_s=None, session_state="closed") == FRESHNESS_NO_DATA


def test_session_state_for_nse_uses_the_calendar_and_never_guesses():
    open_now = exchange_session_state(
        "NSE", now=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc), calendar_loader=lambda e, d: {"2026-09-15"}
    )
    assert open_now["state"] == "open"
    holiday = exchange_session_state(
        "NSE", now=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc), calendar_loader=lambda e, d: set()
    )
    assert holiday["state"] == "closed" and holiday["basis"] == "calendar_holiday"
    unknown = exchange_session_state(
        "NSE", now=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc), calendar_loader=lambda e, d: None
    )
    assert unknown["state"] == "unknown" and unknown["basis"] == "calendar_unavailable"
    # outside the session window even on a trading day
    after_close = exchange_session_state(
        "NSE", now=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc), calendar_loader=lambda e, d: {"2026-09-15"}
    )
    assert after_close["state"] == "closed"


def test_session_state_for_mcx_uses_the_documented_window():
    assert exchange_session_state("MCX", now=NOW)["state"] == "open"
    late = exchange_session_state("MCX", now=datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc))
    assert late["state"] == "closed"
    assert late["session"] == "mcx_commodity"


def test_build_quote_derives_change_from_the_ohlc_close_when_needed():
    quote = build_quote(
        instrument_key="MCX:GOLD26DECFUT",
        descriptor=descriptor(111),
        tick=tick_payload(111, 110.0, close=100.0),
        now=NOW,
        session_state="open",
        origin="tick",
        limits=lim(),
    )
    assert quote["change_absolute"] == pytest.approx(10.0)
    assert quote["change_percent"] == pytest.approx(10.0)
    assert quote["catalog_generation"] == "gen-1"


@pytest.mark.asyncio
async def test_states_age_out_when_ticks_stop():
    """A price that stops arriving must lose its LIVE label without a new tick."""
    clock = {"now": NOW}
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim(), clock=lambda: clock["now"]
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    stream.offer_tick(111, tick_payload(111, 100.0, age_s=0.2))
    stream.flush()
    await pump(stream, socket)
    socket.sent.clear()

    clock["now"] = NOW + timedelta(seconds=45)
    stream.refresh_states()
    await pump(stream, socket)

    states = [f for f in socket.sent if f.get("type") == "state"]
    assert states, "an aged instrument must produce a state frame"
    assert states[0]["instruments"]["MCX:GOLD26DECFUT"]["freshness"] == FRESHNESS_STALE


@pytest.mark.asyncio
async def test_market_closed_state_is_reported_for_mcx_after_the_window():
    clock = {"now": datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)}  # 01:30 IST
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim(), clock=lambda: clock["now"]
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    stream.offer_tick(111, tick_payload(111, 100.0, age_s=0.2))
    stream.flush()
    await pump(stream, socket)
    quotes = [f for f in socket.sent if f.get("type") == "quote"]
    assert quotes and quotes[0]["freshness"] == FRESHNESS_CLOSED
    assert quotes[0]["session_state"] == "closed"


# ---------------------------------------------------------------------------
# protocol handling and cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_frames_are_refused_without_closing_the_connection():
    stream, _hub, socket = make_stream(
        redis_client=FakeRedis(),
        catalog=FakeCatalog({}),
        runtime=FakeRuntimeClient(),
        limits=lim(),
    )
    await stream.handle_message("not-an-object")
    await stream.handle_message({"type": "nonsense"})
    await pump(stream, socket)
    codes = [f.get("code") for f in socket.sent if f.get("type") == "error"]
    assert codes == ["BAD_FRAME", "UNSUPPORTED_FRAME"]


@pytest.mark.asyncio
async def test_release_owner_is_called_even_when_the_client_vanishes():
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    socket = FakeWebSocket([{"type": "subscribe", "instruments": ["MCX:GOLD26DECFUT"]}, WebSocketDisconnect(1006)])
    stream, hub, socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim(), websocket=socket
    )
    await asyncio.wait_for(stream.run(), timeout=5)

    assert hub.connections() == []
    assert any(call[0] == "delete" for call in runtime.calls)
    # the welcome was queued before the client vanished; cleanup is what matters
    assert runtime.owner_tokens == {}


@pytest.mark.asyncio
async def test_owner_renewal_repeats_the_current_set():
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    runtime = FakeRuntimeClient()
    stream, _hub, _socket = make_stream(
        redis_client=FakeRedis(), catalog=catalog, runtime=runtime, limits=lim()
    )
    await stream.subscribe(["MCX:GOLD26DECFUT"])
    await stream.sync_owner()
    puts = [call for call in runtime.calls if call[0] == "put"]
    assert len(puts) == 2
    assert puts[0][2] == puts[1][2] == {111: "ltp"}


def test_ui_subscriptions_never_touch_alert_subscriptions():
    """The stream is presentation state: it must not write evaluation state."""
    import inspect

    source = inspect.getsource(stream_module)
    assert "alert_subscriptions" not in source
    assert "insert(" not in source.lower()


def test_calendar_lookup_never_blocks_the_stream(monkeypatch):
    """A cold or dead calendar degrades to `unknown`, it never stalls ticks.

    The default loader must not touch the database: the hub keeps the cache warm
    on a worker thread, and a cache miss is reported as "we do not know" rather
    than blocking every connection's fan-out on a query.
    """
    monkeypatch.setattr(stream_module, "_calendar_cache", {})

    def _explode(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the stream consulted the database on the event loop")

    monkeypatch.setattr(
        "backend.app.database.get_db_connection", _explode
    )
    cold = exchange_session_state("NSE", now=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc))
    assert cold["state"] == "unknown" and cold["basis"] == "calendar_unavailable"


def test_calendar_refresh_populates_the_cache(monkeypatch):
    monkeypatch.setattr(stream_module, "_calendar_cache", {})
    calls = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("backend.app.database.get_db_connection", lambda: _Conn())
    monkeypatch.setattr(
        "backend.broker_api.market.exchange_calendar.get_calendar_sessions",
        lambda conn, **kwargs: calls.update(kwargs)
        or {"sessions": [{"session_date": "2026-09-15", "session_type": "REGULAR"}]},
    )

    assert stream_module.refresh_calendar_cache("NSE", day=datetime(2026, 9, 15).date()) is True
    state = exchange_session_state(
        "NSE", now=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    )
    assert state["state"] == "open" and state["basis"] == "calendar"
    assert calls["exchange"] == "NSE" and calls["closed"] is True


@pytest.mark.asyncio
async def test_owner_sync_awaits_the_production_client_accessor(monkeypatch):
    """The default path must await the shared runtime client.

    Regression: `get_market_runtime_client()` is a coroutine, and returning the
    coroutine object made every owner registration fail with "'coroutine' object
    has no attribute 'set_owner_subscriptions'" — invisible to the injected
    factory the other tests use, and caught only by the deployed browser run.
    """
    runtime = FakeRuntimeClient()
    calls: List[str] = []

    async def _accessor():
        calls.append("awaited")
        return runtime

    monkeypatch.setattr(
        "backend.broker_api.orders.market_runtime_client.get_market_runtime_client",
        _accessor,
    )
    catalog = FakeCatalog({"MCX:GOLD26DECFUT": descriptor(111)})
    hub = MarketStreamHub(redis_client=FakeRedis(), limits=lim())
    stream = OperatorMarketStream(
        FakeWebSocket(),
        hub=hub,
        scope=SCOPE,
        catalog=catalog,
        # no factory: exercise the production path
        redis_client=FakeRedis(),
        limits=lim(),
        clock=lambda: NOW,
        calendar_loader=lambda exchange, day: {"2026-09-15"},
    )

    await stream.subscribe(["MCX:GOLD26DECFUT"])
    assert calls == ["awaited"]
    assert runtime.owner_tokens[stream.owner_id] == {111: "ltp"}

    await stream.release_owner()
    assert runtime.owner_tokens == {}
