import asyncio
import sys
import types
from pathlib import Path

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)


def _install_httpx_stub(responses):
    module = types.ModuleType("httpx")
    setattr(module, "calls", [])
    setattr(module, "closed", 0)

    class AsyncClient:
        def __init__(self, headers=None, timeout=None):
            self.headers = headers or {}
            self.timeout = timeout

        async def request(self, method, url, **kwargs):
            module.calls.append({"method": method, "url": url, "kwargs": kwargs, "headers": dict(self.headers), "timeout": self.timeout})
            return responses.pop(0)

        async def aclose(self):
            setattr(module, "closed", module.closed + 1)

    setattr(module, "AsyncClient", AsyncClient)
    sys.modules["httpx"] = module
    return module


SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig  # noqa: E402
from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker.models import (  # noqa: E402
    WorkerGttTrigger,
    WorkerGttWriteResult,
    WorkerHistoricalCandles,
    WorkerOrderSnapshot,
    WorkerOrdersResponse,
    WorkerRunHealthSnapshot,
    WorkerRunPnlSnapshot,
    WorkerTradeSnapshot,
    WorkerTradesResponse,
)


def test_async_client_get_run_preview_and_close():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1"}', text='{"strategy_run_id": "run-1"}', json=lambda: {"strategy_run_id": "run-1"}),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}}',
                text='{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}}',
                json=lambda: {"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_order"}},
            ),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=2)) as client:
            run = await client.get_run("run-1")
            preview = await client.preview_order("run-1", {"exchange": "NSE", "tradingsymbol": "INFY"})
            return run, preview

    run, preview = asyncio.run(main())

    assert run == {"strategy_run_id": "run-1"}
    assert preview["mode"] == "live"
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1"
    assert httpx.calls[1]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/preview/order"
    assert httpx.closed == 1


def test_async_client_core_parity_methods_use_worker_endpoints():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(status_code=200, content=b'{"status": "ok"}', text='{"status": "ok"}', json=lambda: {"status": "ok"}),
            types.SimpleNamespace(status_code=200, content=b'{"account_scope": "kite:paper-a"}', text='{"account_scope": "kite:paper-a"}', json=lambda: {"account_scope": "kite:paper-a"}),
            types.SimpleNamespace(status_code=200, content=b'{"quotes": [{"symbol": "NSE:INFY"}], "missing": []}', text='{"quotes": [{"symbol": "NSE:INFY"}], "missing": []}', json=lambda: {"quotes": [{"symbol": "NSE:INFY"}], "missing": []}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "orders": []}', text='{"strategy_run_id": "run-1", "orders": []}', json=lambda: {"strategy_run_id": "run-1", "orders": []}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "trades": []}', text='{"strategy_run_id": "run-1", "trades": []}', json=lambda: {"strategy_run_id": "run-1", "trades": []}),
            types.SimpleNamespace(status_code=200, content=b'{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}', text='{"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}', json=lambda: {"strategy_run_id": "run-1", "mode": "live", "preview": {"intent_type": "place_basket"}}),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as client:
            health = await client.health()
            funds = await client.get_funds(mode="paper", account_scope="kite:paper-a")
            quotes = await client.get_quotes(["NSE:INFY"], mode="quote")
            orders = await client.list_orders("run-1")
            trades = await client.list_trades("run-1")
            preview = await client.preview_basket(
                "run-1",
                [
                    {
                        "exchange": "NSE",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "product": "CNC",
                        "order_type": "MARKET",
                        "quantity": 1,
                    }
                ],
            )
            return health, funds, quotes, orders, trades, preview

    health, funds, quotes, orders, trades, preview = asyncio.run(main())

    assert health == {"status": "ok"}
    assert funds["account_scope"] == "kite:paper-a"
    assert quotes["quotes"][0]["symbol"] == "NSE:INFY"
    assert orders["strategy_run_id"] == "run-1"
    assert trades["strategy_run_id"] == "run-1"
    assert preview["preview"]["intent_type"] == "place_basket"
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/health"
    assert httpx.calls[1]["kwargs"]["params"] == {"mode": "paper", "account_scope": "kite:paper-a"}
    assert httpx.calls[2]["url"] == "http://localhost:8000/api/algo-workers/worker/market/quotes"
    assert httpx.calls[2]["kwargs"]["json"] == {"symbols": ["NSE:INFY"], "instrument_tokens": [], "mode": "quote"}
    assert httpx.calls[3]["url"] == "http://localhost:8000/api/algo-workers/worker/orders"
    assert httpx.calls[3]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert httpx.calls[4]["url"] == "http://localhost:8000/api/algo-workers/worker/trades"
    assert httpx.calls[4]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert httpx.calls[5]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/preview/basket"
    assert httpx.calls[5]["kwargs"]["json"] == {
        "orders": [
            {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "product": "CNC",
                "order_type": "MARKET",
                "quantity": 1,
            }
        ],
        "metadata": {},
        "all_or_none": False,
    }
    assert httpx.closed == 1


def test_async_client_gtt_and_health_helpers_return_models():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "status": "open", "execution_mode": "paper", "health_status": "healthy", "session_status": "claimed", "recovery_status": "idle", "recovery_action_required": false}',
                text='{"strategy_run_id": "run-1", "status": "open", "execution_mode": "paper", "health_status": "healthy", "session_status": "claimed", "recovery_status": "idle", "recovery_action_required": false}',
                json=lambda: {"strategy_run_id": "run-1", "status": "open", "execution_mode": "paper", "health_status": "healthy", "session_status": "claimed", "recovery_status": "idle", "recovery_action_required": False},
            ),
            types.SimpleNamespace(status_code=200, content=b'{"trigger_id": 55}', text='{"trigger_id": 55}', json=lambda: {"trigger_id": 55}),
            types.SimpleNamespace(
                status_code=200,
                content=b'[{"id": 55, "type": "single", "status": "active", "condition": {"tradingsymbol": "INFY"}, "orders": []}]',
                text='[{"id": 55, "type": "single", "status": "active", "condition": {"tradingsymbol": "INFY"}, "orders": []}]',
                json=lambda: [{"id": 55, "type": "single", "status": "active", "condition": {"tradingsymbol": "INFY"}, "orders": []}],
            ),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as sdk:
            health = await sdk.get_run_health_snapshot("run-1")
            placed = await sdk.place_gtt_snapshot({"type": "single"})
            listed = await sdk.list_gtts_snapshot()
            return health, placed, listed

    health, placed, listed = asyncio.run(main())

    assert isinstance(health, WorkerRunHealthSnapshot)
    assert isinstance(placed, WorkerGttWriteResult)
    assert isinstance(listed[0], WorkerGttTrigger)
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1"
    assert httpx.calls[1]["url"] == "http://localhost:8000/api/algo-workers/worker/gtt/triggers"
    assert httpx.calls[2]["url"] == "http://localhost:8000/api/algo-workers/worker/gtt/triggers"


def test_async_client_typed_snapshot_helpers_return_models():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(
                status_code=200,
                content=b'{"symbol": "NSE:SBIN", "instrument_token": 123, "interval": "5minute", "current": {"ts": "2026-04-28T09:20:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1200, "oi": null, "is_complete": false}, "candles": [{"ts": "2026-04-28T09:15:00+05:30", "open": 99, "high": 100, "low": 98.5, "close": 99.8, "volume": 1000, "oi": null, "is_complete": true}], "is_stale": false, "source": "runtime"}',
                text='{"symbol": "NSE:SBIN", "instrument_token": 123, "interval": "5minute", "current": {"ts": "2026-04-28T09:20:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1200, "oi": null, "is_complete": false}, "candles": [{"ts": "2026-04-28T09:15:00+05:30", "open": 99, "high": 100, "low": 98.5, "close": 99.8, "volume": 1000, "oi": null, "is_complete": true}], "is_stale": false, "source": "runtime"}',
                json=lambda: {
                    "symbol": "NSE:SBIN",
                    "instrument_token": 123,
                    "interval": "5minute",
                    "current": {
                        "ts": "2026-04-28T09:20:00+05:30",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 1200,
                        "oi": None,
                        "is_complete": False,
                    },
                    "candles": [
                        {
                            "ts": "2026-04-28T09:15:00+05:30",
                            "open": 99,
                            "high": 100,
                            "low": 98.5,
                            "close": 99.8,
                            "volume": 1000,
                            "oi": None,
                            "is_complete": True,
                        }
                    ],
                    "is_stale": False,
                    "source": "runtime",
                },
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"symbol": "NSE:SBIN", "instrument_token": 123, "timeframe": "day", "interval": "day", "from": "2026-01-01T09:15:00+05:30", "to": "2026-01-02T15:30:00+05:30", "count": 1, "source": "historical_db", "ingestion": {"status": "disabled"}, "candles": [{"ts": "2026-01-01T15:30:00+05:30", "open": 99, "high": 100, "low": 98, "close": 99.5, "volume": 500, "oi": 12, "is_complete": true}]}',
                text='{"symbol": "NSE:SBIN", "instrument_token": 123, "timeframe": "day", "interval": "day", "from": "2026-01-01T09:15:00+05:30", "to": "2026-01-02T15:30:00+05:30", "count": 1, "source": "historical_db", "ingestion": {"status": "disabled"}, "candles": [{"ts": "2026-01-01T15:30:00+05:30", "open": 99, "high": 100, "low": 98, "close": 99.5, "volume": 500, "oi": 12, "is_complete": true}]}',
                json=lambda: {
                    "symbol": "NSE:SBIN",
                    "instrument_token": 123,
                    "timeframe": "day",
                    "interval": "day",
                    "from": "2026-01-01T09:15:00+05:30",
                    "to": "2026-01-02T15:30:00+05:30",
                    "count": 1,
                    "source": "historical_db",
                    "ingestion": {"status": "disabled"},
                    "candles": [
                        {
                            "ts": "2026-01-01T15:30:00+05:30",
                            "open": 99,
                            "high": 100,
                            "low": 98,
                            "close": 99.5,
                            "volume": 500,
                            "oi": 12,
                            "is_complete": True,
                        }
                    ],
                },
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "orders": [{"order_id": "o1", "status": "COMPLETE", "tradingsymbol": "INFY", "quantity": 2, "price": 10.5, "meta": {"source": "worker"}}]}',
                text='{"strategy_run_id": "run-1", "orders": [{"order_id": "o1", "status": "COMPLETE", "tradingsymbol": "INFY", "quantity": 2, "price": 10.5, "meta": {"source": "worker"}}]}',
                json=lambda: {
                    "strategy_run_id": "run-1",
                    "orders": [
                        {
                            "order_id": "o1",
                            "status": "COMPLETE",
                            "tradingsymbol": "INFY",
                            "quantity": 2,
                            "price": 10.5,
                            "meta": {"source": "worker"},
                        }
                    ],
                },
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "order": {"order_id": "o1", "status": "COMPLETE", "tradingsymbol": "INFY", "quantity": 2, "price": 10.5, "meta": {"source": "worker"}}}',
                text='{"strategy_run_id": "run-1", "order": {"order_id": "o1", "status": "COMPLETE", "tradingsymbol": "INFY", "quantity": 2, "price": 10.5, "meta": {"source": "worker"}}}',
                json=lambda: {
                    "strategy_run_id": "run-1",
                    "order": {
                        "order_id": "o1",
                        "status": "COMPLETE",
                        "tradingsymbol": "INFY",
                        "quantity": 2,
                        "price": 10.5,
                        "meta": {"source": "worker"},
                    },
                },
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "trades": [{"trade_id": "abc", "order_id": "o1", "quantity": 2, "average_price": 10.5, "meta": {"fill": "worker"}}]}',
                text='{"strategy_run_id": "run-1", "trades": [{"trade_id": "abc", "order_id": "o1", "quantity": 2, "average_price": 10.5, "meta": {"fill": "worker"}}]}',
                json=lambda: {
                    "strategy_run_id": "run-1",
                    "trades": [
                        {
                            "trade_id": "abc",
                            "order_id": "o1",
                            "quantity": 2,
                            "average_price": 10.5,
                            "meta": {"fill": "worker"},
                        }
                    ],
                },
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"strategy_run_id": "run-1", "execution_mode": "live", "status": "OK", "totals": {"net_pnl": 12.5}, "legs": []}',
                text='{"strategy_run_id": "run-1", "execution_mode": "live", "status": "OK", "totals": {"net_pnl": 12.5}, "legs": []}',
                json=lambda: {
                    "strategy_run_id": "run-1",
                    "execution_mode": "live",
                    "status": "OK",
                    "totals": {"net_pnl": 12.5},
                    "legs": [],
                },
            ),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as client:
            candles = await client.get_candles_snapshot("NSE:SBIN")
            history = await client.get_historical_candles_snapshot("NSE:SBIN", timeframe="day")
            orders = await client.get_orders_snapshot("run-1")
            order = await client.get_order_snapshot("run-1", "o1")
            trades = await client.get_trades_snapshot("run-1")
            pnl = await client.get_run_pnl_snapshot("run-1")
            return candles, history, orders, order, trades, pnl

    candles, history, orders, order, trades, pnl = asyncio.run(main())

    assert isinstance(candles, WorkerHistoricalCandles)
    assert candles.current.close == 100.5
    assert isinstance(history, WorkerHistoricalCandles)
    assert history.from_ts == "2026-01-01T09:15:00+05:30"
    assert isinstance(orders, WorkerOrdersResponse)
    assert isinstance(orders.orders[0], WorkerOrderSnapshot)
    assert orders.orders[0].order_id == "o1"
    assert isinstance(order, WorkerOrderSnapshot)
    assert order.price == 10.5
    assert isinstance(trades, WorkerTradesResponse)
    assert isinstance(trades.trades[0], WorkerTradeSnapshot)
    assert trades.trades[0].trade_id == "abc"
    assert isinstance(pnl, WorkerRunPnlSnapshot)
    assert pnl.totals.net_pnl == 12.5
    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/candles"
    assert httpx.calls[1]["url"] == "http://localhost:8000/api/algo-workers/worker/market/history"
    assert httpx.calls[2]["url"] == "http://localhost:8000/api/algo-workers/worker/orders"
    assert httpx.calls[3]["url"] == "http://localhost:8000/api/algo-workers/worker/orders/o1"
    assert httpx.calls[4]["url"] == "http://localhost:8000/api/algo-workers/worker/trades"
    assert httpx.calls[5]["url"] == "http://localhost:8000/api/algo-workers/worker/runs/run-1/pnl"


def test_async_get_historical_candles_supports_lookback_days():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(
                status_code=200,
                content=b'{"candles": []}',
                text='{"candles": []}',
                json=lambda: {"candles": []},
            )
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as client:
            return await client.get_historical_candles(
                "NSE:INFY",
                timeframe="day",
                to_date="2024-12-31T00:00:00+00:00",
                lookback_days=366,
                passthrough=True,
            )

    asyncio.run(main())

    assert httpx.calls[0]["url"] == "http://localhost:8000/api/algo-workers/worker/market/history"
    assert httpx.calls[0]["kwargs"]["params"] == {
        "timeframe": "day",
        "ingest": "True",
        "passthrough": "True",
        "symbol": "NSE:INFY",
        "to": "2024-12-31T00:00:00+00:00",
        "from": "2023-12-31T00:00:00+00:00",
    }


def test_async_wait_for_terminal_order_state_polls_until_complete():
    httpx = _install_httpx_stub(
        [
            types.SimpleNamespace(
                status_code=200,
                content=b'{"order": {"order_id": "o1", "status": "OPEN"}}',
                text='{"order": {"order_id": "o1", "status": "OPEN"}}',
                json=lambda: {"order": {"order_id": "o1", "status": "OPEN"}},
            ),
            types.SimpleNamespace(
                status_code=200,
                content=b'{"order": {"order_id": "o1", "status": "COMPLETE"}}',
                text='{"order": {"order_id": "o1", "status": "COMPLETE"}}',
                json=lambda: {"order": {"order_id": "o1", "status": "COMPLETE"}},
            ),
        ]
    )

    async def main():
        async with AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test")) as client:
            return await client.wait_for_terminal_order_state("run-1", "o1", attempts=2, sleep_seconds=0)

    result = asyncio.run(main())

    assert isinstance(result, WorkerOrderSnapshot)
    assert result.status == "COMPLETE"
    assert httpx.calls[0]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
    assert httpx.calls[1]["kwargs"]["params"] == {"strategy_run_id": "run-1"}
