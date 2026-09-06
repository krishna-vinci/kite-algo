#!/usr/bin/env python3
# pyright: reportMissingImports=false
from __future__ import annotations

import argparse
from dataclasses import asdict, fields
import json
import os
import sys
from pathlib import Path
from typing import Any


SDK_ROOT = Path(__file__).resolve().parents[1] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from kite_algo_worker import AlgoWorkerConfig, AsyncKiteAlgoWorkerClient, KiteAlgoWorkerClient  # noqa: E402
from kite_algo_worker import NUMBA_AVAILABLE, StreamHealth, ta, wait_for_history, warmup_history  # noqa: E402
from kite_algo_worker.endpoint_manifest import WORKER_HTTP_ENDPOINTS, WORKER_WEBSOCKET_PATHS  # noqa: E402
from kite_algo_worker.options import AsyncOptionWorkerClient, OptionWorkerClient  # noqa: E402


def _safe_number(value: Any) -> float | None:
    try:
        scalar = value.item() if hasattr(value, "item") else value
        return float(scalar)
    except (AttributeError, TypeError, ValueError):
        return None


def _last_scalar(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "dropna"):
        non_null = value.dropna()
        if getattr(non_null, "empty", False):
            return None
        return _safe_number(non_null.iloc[-1])
    if hasattr(value, "iloc"):
        try:
            return _safe_number(value.iloc[-1])
        except Exception:
            return None
    try:
        if isinstance(value, dict):
            return None
        if len(value) == 0:  # type: ignore[arg-type]
            return None
        return _safe_number(value[-1])  # type: ignore[index]
    except Exception:
        return _safe_number(value)


def collect_typed_marketdata_capability(client: KiteAlgoWorkerClient, symbol: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "available": hasattr(client, "get_candles_snapshot"),
        "symbol": symbol,
    }
    if not report["available"]:
        return report
    try:
        snapshot = client.get_candles_snapshot(symbol, interval="5minute", lookback=2)
        report.update(
            {
                "typed_snapshot": True,
                "snapshot_type": type(snapshot).__name__,
                "current_type": type(snapshot.current).__name__ if getattr(snapshot, "current", None) is not None else None,
                "candle_count": len(getattr(snapshot, "candles", []) or []),
                "is_stale": getattr(snapshot, "is_stale", None),
                "source": getattr(snapshot, "source", None),
            }
        )
    except Exception as exc:  # pragma: no cover - runtime safety
        report.update({"typed_snapshot": False, "error": str(exc)})
    return report


def collect_recovery_helper_capability(client: KiteAlgoWorkerClient, symbol: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "wait_for_history": True,
        "warmup_history": True,
        "symbol": symbol,
    }
    try:
        history = wait_for_history(client, symbol, timeframe="day", attempts=1, sleep_seconds=0)
        report["history_ready"] = bool((history or {}).get("candles"))
        report["history_candle_count"] = len((history or {}).get("candles") or [])
    except Exception as exc:  # pragma: no cover - runtime safety
        report["history_ready"] = False
        report["history_error"] = str(exc)

    try:
        warm = warmup_history(client, symbol, timeframe="day", min_candles=1, attempts=1, sleep_seconds=0)
        report["warmup_ready"] = len(getattr(warm, "candles", []) or []) >= 1
        report["warmup_candle_count"] = len(getattr(warm, "candles", []) or [])
        report["warmup_snapshot_type"] = type(warm).__name__
    except Exception as exc:  # pragma: no cover - runtime safety
        report["warmup_ready"] = False
        report["warmup_error"] = str(exc)

    return report


def collect_websocket_health_capability() -> dict[str, Any]:
    sample = StreamHealth(stream_name="ticks", subscription_key="/worker/ws/market/ticks?mode=quote")
    reconnect_fields = {
        "reconnect_count",
        "last_reconnect_at",
        "subscription_replayed",
        "subscription_replayed_at",
        "next_reconnect_delay_seconds",
        "last_error",
        "is_stale",
    }
    field_names = [field.name for field in fields(StreamHealth)]
    return {
        "available": True,
        "type": StreamHealth.__name__,
        "fields": field_names,
        "reconnect_metadata_fields": [name for name in field_names if name in reconnect_fields],
        "sample": asdict(sample),
    }


def collect_indicator_capability() -> dict[str, Any]:
    close = [100.0, 101.0, 102.5, 101.5, 103.0, 104.0, 105.5, 106.0, 107.0, 108.0, 109.5, 110.0, 111.0, 112.0, 113.0, 114.0]
    high = [value + 1.0 for value in close]
    low = [value - 1.0 for value in close]
    report: dict[str, Any] = {
        "available": False,
        "numba_available": bool(NUMBA_AVAILABLE),
        "representative": {},
    }
    try:
        sma = ta.sma(close, 5)
        ema = ta.ema(close, 5)
        rsi = ta.rsi(close, 14)
        atr = ta.atr(high, low, close, 14)
        report.update(
            {
                "available": True,
                "representative": {
                    "sma_last": _last_scalar(sma),
                    "ema_last": _last_scalar(ema),
                    "rsi_last": _last_scalar(rsi),
                    "atr_last": _last_scalar(atr),
                },
            }
        )
    except Exception as exc:  # pragma: no cover - optional-dependency/runtime safety
        report["error"] = str(exc)
    return report


def collect_sdk_surface_capability(client: KiteAlgoWorkerClient) -> dict[str, Any]:
    options_client = getattr(client, "options", None)
    return {
        "managed_lifecycle": all(hasattr(client, name) for name in ("run", "claim_session", "release_session", "run_heartbeat")),
        "safety_check": hasattr(client, "safety_check"),
        "run_health_snapshot": hasattr(client, "get_run_health_snapshot"),
        "timeline": all(hasattr(client, name) for name in ("log_decision_event", "list_timeline", "stream_timeline")),
        "gtt_helpers": all(hasattr(client, name) for name in ("place_gtt", "list_gtts", "get_gtt", "modify_gtt", "delete_gtt")),
        "option_resolvers": bool(
            options_client
            and all(hasattr(options_client, name) for name in ("resolve_option_leg", "resolve_offset_leg", "resolve_delta_leg", "resolve_spread"))
        ),
        "amo_market_order_helper": hasattr(__import__("kite_algo_worker"), "amo_market_order"),
        "execution_observability": all(
            hasattr(client, name)
            for name in (
                "get_order_history",
                "list_baskets",
                "get_basket",
                "create_bracket",
                "list_brackets",
                "get_bracket",
                "cancel_bracket",
                "list_execution_events",
                "stream_execution_events",
                "export_fundamentals_csv",
            )
        ),
    }


def _has_public_method(client_class: type, options_class: type, dotted_name: str) -> bool:
    if dotted_name.startswith("options."):
        return callable(getattr(options_class, dotted_name.split(".", 1)[1], None))
    return callable(getattr(client_class, dotted_name, None))


def collect_endpoint_coverage() -> dict[str, int]:
    """Report measured manifest-to-client method coverage.

    Keeping these counts derived from the manifest and class attributes makes
    certification useful when a helper is accidentally removed: it reports the
    deficit instead of claiming a static 77/77 result.
    """

    sync_count = sum(
        _has_public_method(KiteAlgoWorkerClient, OptionWorkerClient, item.public_method)
        for item in WORKER_HTTP_ENDPOINTS
    )
    async_count = sum(
        _has_public_method(AsyncKiteAlgoWorkerClient, AsyncOptionWorkerClient, item.resolved_async_method)
        for item in WORKER_HTTP_ENDPOINTS
    )
    return {
        "worker_http_operations": len(WORKER_HTTP_ENDPOINTS),
        "sync_http_operations": sync_count,
        "async_http_operations": async_count,
        "worker_websocket_routes": len(WORKER_WEBSOCKET_PATHS),
    }


def collect_certification_report(
    client: KiteAlgoWorkerClient,
    *,
    mode: str,
    symbol: str,
    account_scope: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    preview_report: dict[str, Any] | Any = {"skipped": True}
    if mode == "live" and run_id:
        preview_report = client.preview_order(
            run_id,
            {
                "exchange": os.environ.get("KITE_ALGO_PREVIEW_EXCHANGE", "NSE"),
                "tradingsymbol": os.environ.get("KITE_ALGO_PREVIEW_SYMBOL", "INFY"),
                "transaction_type": "BUY",
                "variety": "regular",
                "product": os.environ.get("KITE_ALGO_PREVIEW_PRODUCT", "CNC"),
                "order_type": "MARKET",
                "quantity": 1,
                "market_protection": -1,
            },
        )

    return {
        "health": client.health(),
        "funds": client.get_funds(mode=mode, account_scope=account_scope),
        "quotes": client.get_quotes([symbol], mode="quote"),
        "preview": preview_report,
        "capabilities": {
            "async_client": True,
            "endpoint_coverage": collect_endpoint_coverage(),
            "websocket_client": True,
            "preview_order": True,
            "list_orders": True,
            "wait_for_history": True,
            "sdk_surface": collect_sdk_surface_capability(client),
            "typed_marketdata": collect_typed_marketdata_capability(client, symbol),
            "recovery_helpers": collect_recovery_helper_capability(client, symbol),
            "websocket_health": collect_websocket_health_capability(),
            "indicators": collect_indicator_capability(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a lightweight Kite Algo worker SDK certification check.")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()

    client = KiteAlgoWorkerClient(
        AlgoWorkerConfig(
            base_url=os.environ.get("KITE_ALGO_API_BASE", "http://localhost:18777"),
            token=os.environ["KITE_ALGO_WORKER_TOKEN"],
            timeout=float(os.environ.get("KITE_ALGO_TIMEOUT", "10")),
        )
    )

    report = collect_certification_report(
        client,
        mode=args.mode,
        symbol=os.environ.get("KITE_ALGO_SYMBOL", "NSE:NIFTY 50"),
        account_scope=os.environ.get("KITE_ALGO_ACCOUNT_SCOPE"),
        run_id=os.environ.get("KITE_ALGO_RUN_ID"),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
