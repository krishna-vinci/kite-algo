"""Audited public worker endpoint contract used by SDK coverage checks.

The manifest deliberately describes the public worker surface instead of the
whole application OpenAPI document.  It is a reviewable maintenance contract:
new backend worker operations must add an explicit entry and both clients must
expose the mapped helper.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointContract:
    method: str
    path: str
    public_method: str
    response_kind: str = "json"
    mutates: bool = False
    async_public_method: str | None = None

    @property
    def resolved_async_method(self) -> str:
        return self.async_public_method or self.public_method


def _e(
    method: str,
    path: str,
    public_method: str,
    *,
    response_kind: str = "json",
    mutates: bool = False,
    async_public_method: str | None = None,
) -> EndpointContract:
    return EndpointContract(
        method=method,
        path=path,
        public_method=public_method,
        response_kind=response_kind,
        mutates=mutates,
        async_public_method=async_public_method,
    )


WORKER_HTTP_ENDPOINTS = (
    _e("GET", "/worker/account/portfolio", "get_account_portfolio"),
    _e("GET", "/worker/fundamentals/export.csv", "export_fundamentals_csv", response_kind="text"),
    _e("GET", "/worker/fundamentals/features", "get_fundamentals_features"),
    _e("GET", "/worker/fundamentals/statements", "get_fundamentals_statements"),
    _e("GET", "/worker/fundamentals/status", "get_fundamentals_status"),
    _e("POST", "/worker/fundamentals/sync", "refresh_fundamentals", mutates=True),
    _e("GET", "/worker/funds", "get_funds"),
    _e("GET", "/worker/gtt/triggers", "list_gtts"),
    _e("POST", "/worker/gtt/triggers", "place_gtt", mutates=True),
    _e("DELETE", "/worker/gtt/triggers/{trigger_id}", "delete_gtt", mutates=True),
    _e("GET", "/worker/gtt/triggers/{trigger_id}", "get_gtt"),
    _e("PUT", "/worker/gtt/triggers/{trigger_id}", "modify_gtt", mutates=True),
    _e("GET", "/worker/health", "health"),
    _e("POST", "/worker/heartbeat", "heartbeat", mutates=True),
    _e("GET", "/worker/market/calendar", "get_market_calendar"),
    _e("GET", "/worker/market/calendar/status", "get_market_calendar_status"),
    _e("GET", "/worker/market/candles", "get_candles"),
    _e("GET", "/worker/market/candles/stream", "stream_candles", response_kind="sse"),
    _e("GET", "/worker/market/history", "get_historical_candles"),
    _e("GET", "/worker/market/indices/{source_list}", "get_index_constituents"),
    _e("GET", "/worker/market/indices/{source_list}/status", "get_index_constituent_status"),
    _e("GET", "/worker/market/instruments/resolve", "resolve_ticker"),
    _e("POST", "/worker/market/instruments/resolve", "resolve_tickers"),
    _e("GET", "/worker/market/instruments/search", "search_tickers"),
    _e("POST", "/worker/market/quotes", "get_quotes"),
    _e("POST", "/worker/market/snapshot", "get_market_snapshot"),
    _e("GET", "/worker/market/ticks/stream", "stream_ticks", response_kind="sse"),
    _e("POST", "/worker/options/runs", "options.create_run", mutates=True),
    _e("POST", "/worker/options/runs/{strategy_run_id}/enter", "options.enter", mutates=True),
    _e("POST", "/worker/options/runs/{strategy_run_id}/exit", "options.exit", mutates=True),
    _e("POST", "/worker/options/runs/{strategy_run_id}/preview-entry", "options.preview_run_entry"),
    _e("POST", "/worker/options/runs/{strategy_run_id}/preview-exit", "options.preview_exit"),
    _e("PUT", "/worker/options/runs/{strategy_run_id}/protection", "options.update_protection", mutates=True),
    _e("POST", "/worker/options/runs/{strategy_run_id}/protection/replay", "options.replay_protection"),
    _e("GET", "/worker/options/runs/{strategy_run_id}/protection/state", "options.get_protection_state"),
    _e("GET", "/worker/options/runs/{strategy_run_id}/state", "options.get_run_state"),
    _e("POST", "/worker/options/strategies/preview", "options.preview_strategy"),
    _e("GET", "/worker/options/underlyings/{underlying}/analytics/max-pain", "options.get_max_pain"),
    _e("GET", "/worker/options/underlyings/{underlying}/analytics/pcr", "options.get_pcr"),
    _e("GET", "/worker/options/underlyings/{underlying}/chain", "options.get_chain"),
    _e("GET", "/worker/options/underlyings/{underlying}/expiries", "options.list_expiries"),
    _e("GET", "/worker/options/underlyings/{underlying}/greeks", "options.get_greeks"),
    _e("GET", "/worker/options/underlyings/{underlying}/mini-chain", "options.get_mini_chain"),
    _e("POST", "/worker/options/underlyings/{underlying}/selection/resolve", "options.resolve_contracts"),
    _e("GET", "/worker/options/underlyings/{underlying}/session", "options.ensure_session"),
    _e("GET", "/worker/orders", "list_orders"),
    _e("GET", "/worker/orders/{order_id}", "get_order_snapshot"),
    _e("POST", "/worker/orders/{order_id}/cancel", "cancel_order", mutates=True),
    _e("GET", "/worker/orders/{order_id}/history", "get_order_history"),
    _e("POST", "/worker/orders/{order_id}/modify", "modify_order", mutates=True),
    _e("POST", "/worker/runs", "create_run", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}", "get_run"),
    _e("GET", "/worker/runs/{strategy_run_id}/baskets", "list_baskets"),
    _e("GET", "/worker/runs/{strategy_run_id}/baskets/{basket_execution_id}", "get_basket"),
    _e("GET", "/worker/runs/{strategy_run_id}/brackets", "list_brackets"),
    _e("POST", "/worker/runs/{strategy_run_id}/brackets", "create_bracket", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}", "get_bracket"),
    _e("POST", "/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}/cancel", "cancel_bracket", mutates=True),
    _e("DELETE", "/worker/runs/{strategy_run_id}/claim-session", "release_session", mutates=True),
    _e("POST", "/worker/runs/{strategy_run_id}/claim-session", "claim_session", mutates=True),
    _e("POST", "/worker/runs/{strategy_run_id}/decision-events", "log_decision_event", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}/execution-events", "list_execution_events"),
    _e("GET", "/worker/runs/{strategy_run_id}/execution-events/stream", "stream_execution_events", response_kind="sse"),
    _e("POST", "/worker/runs/{strategy_run_id}/exit", "exit_run", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}/funds", "get_run_funds"),
    _e("POST", "/worker/runs/{strategy_run_id}/heartbeat", "run_heartbeat", mutates=True),
    _e("POST", "/worker/runs/{strategy_run_id}/intents", "place_order", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}/pnl", "get_run_pnl"),
    _e("GET", "/worker/runs/{strategy_run_id}/pnl/stream", "stream_run_pnl", response_kind="sse"),
    _e("POST", "/worker/runs/{strategy_run_id}/preview/basket", "preview_basket"),
    _e("POST", "/worker/runs/{strategy_run_id}/preview/order", "preview_order"),
    _e("PATCH", "/worker/runs/{strategy_run_id}/protection", "update_backend_protection", mutates=True),
    _e("PATCH", "/worker/runs/{strategy_run_id}/risk", "patch_risk", mutates=True),
    _e("GET", "/worker/runs/{strategy_run_id}/safety-check", "safety_check"),
    _e("GET", "/worker/runs/{strategy_run_id}/timeline", "list_timeline"),
    _e("GET", "/worker/runs/{strategy_run_id}/timeline/stream", "stream_timeline", response_kind="sse"),
    _e("GET", "/worker/trades", "list_trades"),
)

WORKER_WEBSOCKET_PATHS = (
    "/worker/ws/market/ticks",
    "/worker/ws/market/candles",
    "/worker/ws/runs/{strategy_run_id}/pnl",
)

__all__ = ["EndpointContract", "WORKER_HTTP_ENDPOINTS", "WORKER_WEBSOCKET_PATHS"]
