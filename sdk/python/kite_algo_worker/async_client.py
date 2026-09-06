from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from importlib import import_module
from typing import Any, Dict, Iterable, Mapping, Optional

from ._shared import (
    build_create_run_payload,
    build_heartbeat_payload,
    build_historical_date_params,
    build_intent_payload,
    fundamentals_scope_params,
    normalize_calendar_date_params,
    session_headers,
    split_instruments,
    require_identity_param,
)
from .client import AlgoWorkerConfig, JsonDict
from .exceptions import KiteAlgoWorkerError, error_for_status
from .fundamentals import (
    FundamentalFeatures,
    FundamentalsStatements,
    FundamentalsStatus,
    FundamentalsSyncRun,
)
from .investment import (
    WorkerAccountPortfolioSnapshot,
    WorkerIndexConstituentStatus,
    WorkerIndexConstituentsSnapshot,
    WorkerMarketCalendarSnapshot,
    WorkerMarketCalendarStatus,
)
from .models import (
    OrderPreview,
    WorkerGttTrigger,
    WorkerGttWriteResult,
    WorkerHistoricalCandles,
    WorkerBasketExecution,
    WorkerBasketExecutionsResponse,
    WorkerBracketActionResult,
    WorkerBracketIntent,
    WorkerBracketListResponse,
    WorkerExecutionEventsResponse,
    WorkerOrderHistoryResponse,
    WorkerOrderSnapshot,
    WorkerOrdersResponse,
    WorkerRunHealthSnapshot,
    WorkerRunPnlSnapshot,
    SafetyCheckResult,
    WorkerTimelineResponse,
    WorkerTradesResponse,
)
from .protection import BackendProtection
from .options.async_client import AsyncOptionWorkerClient


@dataclass(frozen=True)
class AsyncKiteAlgoWorkerClient:
    config: AlgoWorkerConfig
    client: Any = field(init=False, repr=False)
    options: AsyncOptionWorkerClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.config.base_url:
            raise ValueError("base_url is required")
        if not self.config.token:
            raise ValueError("token is required")
        httpx = import_module("httpx")
        object.__setattr__(self, "client", httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        ))
        object.__setattr__(self, "options", AsyncOptionWorkerClient(self))

    async def __aenter__(self) -> "AsyncKiteAlgoWorkerClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def health(self) -> JsonDict:
        return await self._request("GET", "/worker/health")

    async def heartbeat(
        self,
        worker_id: Optional[str] = None,
        status: str = "healthy",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        return await self._request(
            "POST",
            "/worker/heartbeat",
            json=build_heartbeat_payload(worker_id=worker_id, status=status, metrics=metrics),
        )

    async def create_run(
        self,
        *,
        template_id: str,
        account_scope: str,
        strategy_run_id: Optional[str] = None,
        execution_mode: str = "paper",
        summary_fields: Optional[Iterable[Mapping[str, Any]]] = None,
        risk_schema: Optional[Iterable[Mapping[str, Any]]] = None,
        allowed_actions: Optional[Iterable[str]] = None,
        runtime_state: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        backend_protection: Optional[BackendProtection] = None,
    ) -> JsonDict:
        payload = build_create_run_payload(
            template_id=template_id,
            account_scope=account_scope,
            strategy_run_id=strategy_run_id,
            execution_mode=execution_mode,
            summary_fields=summary_fields,
            risk_schema=risk_schema,
            allowed_actions=allowed_actions,
            runtime_state=runtime_state,
            metadata=metadata,
            backend_protection=backend_protection,
        )
        return await self._request("POST", "/worker/runs", json=payload)

    async def create_run_from_config(self, config: Any) -> JsonDict:
        return await self._request("POST", "/worker/runs", json=config.to_create_run_payload())

    async def claim_session(self, strategy_run_id: str) -> JsonDict:
        return await self._request("POST", f"/worker/runs/{strategy_run_id}/claim-session")

    async def release_session(self, strategy_run_id: str, *, session_nonce: str) -> JsonDict:
        return await self._request(
            "DELETE",
            f"/worker/runs/{strategy_run_id}/claim-session",
            headers=session_headers(session_nonce),
        )

    async def run_heartbeat(
        self,
        strategy_run_id: str,
        *,
        session_nonce: str,
        worker_id: Optional[str] = None,
        status: str = "healthy",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/heartbeat",
            headers=session_headers(session_nonce),
            json=build_heartbeat_payload(worker_id=worker_id, status=status, metrics=metrics),
        )

    async def safety_check(self, strategy_run_id: str):
        return SafetyCheckResult.model_validate(
            await self._request("GET", f"/worker/runs/{strategy_run_id}/safety-check")
        )

    async def cancel_order(self, strategy_run_id: str, order_id: str, *, variety: str = "regular") -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/orders/{order_id}/cancel",
            json={"strategy_run_id": strategy_run_id, "variety": variety},
        )

    async def modify_order(
        self,
        strategy_run_id: str,
        order_id: str,
        patch: Mapping[str, Any],
        *,
        variety: str = "regular",
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/orders/{order_id}/modify",
            json={"strategy_run_id": strategy_run_id, "variety": variety, **dict(patch)},
        )

    async def place_order(
        self,
        strategy_run_id: str,
        order: Mapping[str, Any],
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
        safety_token: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        payload = build_intent_payload(
            intent_type="place_order",
            body_key="order",
            body=dict(order),
            idempotency_key=idempotency_key,
            metadata=metadata,
            safety_token=safety_token,
        )
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json=payload,
            headers=session_headers(session_nonce),
        )

    async def place_basket(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
        *,
        all_or_none: bool = False,
        dry_run: bool = False,
        safety_token: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        payload = build_intent_payload(
            intent_type="place_basket",
            body_key="basket",
            body={
                "orders": [dict(order) for order in orders],
                "all_or_none": all_or_none,
                "dry_run": dry_run,
            },
            idempotency_key=idempotency_key,
            metadata=metadata,
            safety_token=safety_token,
        )
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json=payload,
            headers=session_headers(session_nonce),
        )

    async def patch_risk(
        self,
        strategy_run_id: str,
        patch: Mapping[str, Any],
        reason: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return await self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/risk",
            json={"patch": dict(patch), "reason": reason},
            headers=session_headers(session_nonce),
        )

    async def update_backend_protection(
        self,
        strategy_run_id: str,
        backend_protection: BackendProtection,
        *,
        reason: Optional[str] = None,
        reset_trailing: bool = True,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return await self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/protection",
            json={
                "backend_protection": backend_protection.to_dict(),
                "reason": reason,
                "reset_trailing": reset_trailing,
            },
            headers=session_headers(session_nonce),
        )

    async def exit_run(
        self,
        strategy_run_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dry_run: bool = False,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/exit",
            json={"reason": reason, "idempotency_key": idempotency_key, "dry_run": dry_run},
            headers=session_headers(session_nonce),
        )

    async def get_run(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}")

    async def get_run_health_snapshot(self, strategy_run_id: str) -> WorkerRunHealthSnapshot:
        return WorkerRunHealthSnapshot.model_validate(await self.get_run(strategy_run_id))

    async def get_run_pnl(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}/pnl")

    async def get_run_pnl_snapshot(self, strategy_run_id: str) -> WorkerRunPnlSnapshot:
        return WorkerRunPnlSnapshot.model_validate(await self.get_run_pnl(strategy_run_id))

    async def get_funds(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> JsonDict:
        params: JsonDict = {"mode": mode}
        if account_scope is not None:
            params["account_scope"] = account_scope
        return await self._request("GET", "/worker/funds", params=params)

    async def get_run_funds(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}/funds")

    async def get_index_constituents(self, source_list: str, *, schema_version: int = 1) -> JsonDict:
        source = require_identity_param(source_list, field_name="source_list")
        return await self._request(
            "GET",
            f"/worker/market/indices/{source}",
            params={"schema_version": schema_version},
        )

    async def get_index_constituents_snapshot(self, source_list: str, *, schema_version: int = 1) -> WorkerIndexConstituentsSnapshot:
        return WorkerIndexConstituentsSnapshot.model_validate(
            await self.get_index_constituents(source_list, schema_version=schema_version)
        )

    async def get_index_constituent_status(self, source_list: str, *, schema_version: int = 1) -> JsonDict:
        source = require_identity_param(source_list, field_name="source_list")
        return await self._request(
            "GET",
            f"/worker/market/indices/{source}/status",
            params={"schema_version": schema_version},
        )

    async def get_index_constituent_status_snapshot(self, source_list: str, *, schema_version: int = 1) -> WorkerIndexConstituentStatus:
        return WorkerIndexConstituentStatus.model_validate(
            await self.get_index_constituent_status(source_list, schema_version=schema_version)
        )

    async def get_market_calendar(self, from_date: Any, to_date: Any, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> JsonDict:
        params = normalize_calendar_date_params(from_date, to_date, exchange=exchange, segment=segment)
        params["schema_version"] = schema_version
        return await self._request("GET", "/worker/market/calendar", params=params)

    async def get_market_calendar_snapshot(self, from_date: Any, to_date: Any, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> WorkerMarketCalendarSnapshot:
        return WorkerMarketCalendarSnapshot.model_validate(
            await self.get_market_calendar(from_date, to_date, exchange=exchange, segment=segment, schema_version=schema_version)
        )

    async def get_market_calendar_status(self, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> JsonDict:
        exchange_text = require_identity_param(exchange, field_name="exchange").upper()
        segment_text = require_identity_param(segment, field_name="segment").upper()
        return await self._request(
            "GET",
            "/worker/market/calendar/status",
            params={"exchange": exchange_text, "segment": segment_text, "schema_version": schema_version},
        )

    async def get_market_calendar_status_snapshot(self, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> WorkerMarketCalendarStatus:
        return WorkerMarketCalendarStatus.model_validate(
            await self.get_market_calendar_status(exchange=exchange, segment=segment, schema_version=schema_version)
        )

    async def get_account_portfolio(self, *, account_scope: Optional[str] = None, schema_version: int = 1) -> JsonDict:
        params: JsonDict = {"schema_version": schema_version}
        if account_scope is not None:
            scope_text = str(account_scope).strip()
            if not scope_text:
                raise ValueError("account_scope must not be empty when provided")
            params["account_scope"] = scope_text
        return await self._request("GET", "/worker/account/portfolio", params=params)

    async def get_account_portfolio_snapshot(self, *, account_scope: Optional[str] = None, schema_version: int = 1) -> WorkerAccountPortfolioSnapshot:
        return WorkerAccountPortfolioSnapshot.model_validate(
            await self.get_account_portfolio(account_scope=account_scope, schema_version=schema_version)
        )

    # -- Fundamentals (0.8.0; read-only except refresh_fundamentals) --------

    async def get_fundamentals_features(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalFeatures:
        """Typed fundamentals feature snapshot for symbols or an index universe."""
        params = fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalFeatures.model_validate(
            await self._request("GET", "/worker/fundamentals/features", params=params)
        )

    async def get_fundamentals_status(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalsStatus:
        """Per-symbol fundamentals freshness plus recent sync-run history."""
        params = fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalsStatus.model_validate(
            await self._request("GET", "/worker/fundamentals/status", params=params)
        )

    async def get_fundamentals_statements(self, symbol: str, *, dataset: str, statement_scope: str = "consolidated") -> FundamentalsStatements:
        """Raw statement rows for one symbol and dataset (e.g. ``quarterly``)."""
        symbol_text = require_identity_param(symbol, field_name="symbol")
        if not str(dataset).strip():
            raise ValueError("dataset is required")
        return FundamentalsStatements.model_validate(
            await self._request(
                "GET",
                "/worker/fundamentals/statements",
                params={
                    "symbol": symbol_text.upper(),
                    "dataset": dataset,
                    "statement_scope": statement_scope,
                    "schema_version": 1,
                },
            )
        )

    async def refresh_fundamentals(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None, mode: str = "incremental") -> FundamentalsSyncRun:
        """Trigger an on-demand fundamentals sync. This is the only mutating
        fundamentals method: the server caps the resolved scope at 50 symbols
        and single-flights syncs (409 when one is already running)."""
        if bool(symbols) == bool(index):
            raise ValueError("provide exactly one of 'symbols' or 'index'")
        body: JsonDict = {"mode": mode}
        if symbols:
            cleaned = [str(s).strip().upper() for s in symbols if str(s).strip()]
            if not cleaned:
                raise ValueError("symbols must not be empty when provided")
            body["symbols"] = cleaned
        else:
            index_text = str(index or "").strip()
            if not index_text:
                raise ValueError("index must not be empty when provided")
            body["index"] = index_text
        return FundamentalsSyncRun.model_validate(await self._request("POST", "/worker/fundamentals/sync", json=body))

    async def place_gtt(self, payload: Mapping[str, Any]) -> JsonDict:
        return await self._request("POST", "/worker/gtt/triggers", json=dict(payload))

    async def place_gtt_snapshot(self, payload: Mapping[str, Any]) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(await self.place_gtt(payload))

    async def list_gtts(self) -> list[JsonDict]:
        response = await self._request("GET", "/worker/gtt/triggers")
        if not isinstance(response, list):
            return []
        return [dict(item) for item in response if isinstance(item, Mapping)]

    async def list_gtts_snapshot(self) -> list[WorkerGttTrigger]:
        return [WorkerGttTrigger.model_validate(item) for item in await self.list_gtts()]

    async def get_gtt(self, trigger_id: int) -> JsonDict:
        return await self._request("GET", f"/worker/gtt/triggers/{int(trigger_id)}")

    async def get_gtt_snapshot(self, trigger_id: int) -> WorkerGttTrigger:
        return WorkerGttTrigger.model_validate(await self.get_gtt(trigger_id))

    async def modify_gtt(self, trigger_id: int, payload: Mapping[str, Any]) -> JsonDict:
        return await self._request("PUT", f"/worker/gtt/triggers/{int(trigger_id)}", json=dict(payload))

    async def modify_gtt_snapshot(self, trigger_id: int, payload: Mapping[str, Any]) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(await self.modify_gtt(trigger_id, payload))

    async def delete_gtt(self, trigger_id: int) -> JsonDict:
        return await self._request("DELETE", f"/worker/gtt/triggers/{int(trigger_id)}")

    async def delete_gtt_snapshot(self, trigger_id: int) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(await self.delete_gtt(trigger_id))

    async def log_decision_event(self, strategy_run_id: str, **payload: Any) -> JsonDict:
        return await self._request("POST", f"/worker/runs/{strategy_run_id}/decision-events", json=dict(payload))

    async def list_timeline(self, strategy_run_id: str, **params: Any) -> JsonDict:
        return await self._request("GET", f"/worker/runs/{strategy_run_id}/timeline", params=dict(params or {}))

    async def list_timeline_snapshot(self, strategy_run_id: str, **params: Any) -> WorkerTimelineResponse:
        return WorkerTimelineResponse.model_validate(await self.list_timeline(strategy_run_id, **params))

    async def list_orders(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", "/worker/orders", params={"strategy_run_id": strategy_run_id})

    async def get_orders_snapshot(self, strategy_run_id: str) -> WorkerOrdersResponse:
        return WorkerOrdersResponse.model_validate(await self.list_orders(strategy_run_id))

    async def list_trades(self, strategy_run_id: str) -> JsonDict:
        return await self._request("GET", "/worker/trades", params={"strategy_run_id": strategy_run_id})

    async def get_trades_snapshot(self, strategy_run_id: str) -> WorkerTradesResponse:
        return WorkerTradesResponse.model_validate(await self.list_trades(strategy_run_id))

    async def get_order_snapshot(self, strategy_run_id: str, order_id: str) -> WorkerOrderSnapshot:
        response = await self._request("GET", f"/worker/orders/{order_id}", params={"strategy_run_id": strategy_run_id})
        return WorkerOrderSnapshot.model_validate(response.get("order") or response)

    async def get_order_history(self, strategy_run_id: str, order_id: str) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/orders/{order_id}/history",
            params={"strategy_run_id": strategy_run_id},
        )

    async def get_order_history_snapshot(self, strategy_run_id: str, order_id: str) -> WorkerOrderHistoryResponse:
        return WorkerOrderHistoryResponse.model_validate(await self.get_order_history(strategy_run_id, order_id))

    async def list_baskets(self, strategy_run_id: str, *, limit: int = 100) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/baskets",
            params={"limit": limit},
        )

    async def list_baskets_snapshot(self, strategy_run_id: str, *, limit: int = 100) -> WorkerBasketExecutionsResponse:
        return WorkerBasketExecutionsResponse.model_validate(await self.list_baskets(strategy_run_id, limit=limit))

    async def get_basket(self, strategy_run_id: str, basket_execution_id: str) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/baskets/{basket_execution_id}",
        )

    async def get_basket_snapshot(self, strategy_run_id: str, basket_execution_id: str) -> WorkerBasketExecution:
        return WorkerBasketExecution.model_validate(await self.get_basket(strategy_run_id, basket_execution_id))

    async def create_bracket(
        self,
        strategy_run_id: str,
        *,
        entry_order: Mapping[str, Any],
        stoploss: Mapping[str, Any],
        target: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        session_nonce: str,
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/brackets",
            json={
                "entry_order": dict(entry_order),
                "stoploss": dict(stoploss),
                "target": dict(target) if target is not None else None,
                "idempotency_key": idempotency_key,
                "metadata": dict(metadata or {}),
            },
            headers=session_headers(session_nonce),
        )

    async def create_bracket_snapshot(self, strategy_run_id: str, **kwargs: Any) -> WorkerBracketActionResult:
        return WorkerBracketActionResult.model_validate(await self.create_bracket(strategy_run_id, **kwargs))

    async def list_brackets(self, strategy_run_id: str, *, limit: int = 50) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/brackets",
            params={"limit": limit},
        )

    async def list_brackets_snapshot(self, strategy_run_id: str, *, limit: int = 50) -> WorkerBracketListResponse:
        return WorkerBracketListResponse.model_validate(await self.list_brackets(strategy_run_id, limit=limit))

    async def get_bracket(self, strategy_run_id: str, bracket_intent_id: str) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}",
        )

    async def get_bracket_snapshot(self, strategy_run_id: str, bracket_intent_id: str) -> WorkerBracketIntent:
        return WorkerBracketIntent.model_validate(await self.get_bracket(strategy_run_id, bracket_intent_id))

    async def cancel_bracket(self, strategy_run_id: str, bracket_intent_id: str, *, session_nonce: str) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}/cancel",
            headers=session_headers(session_nonce),
        )

    async def cancel_bracket_snapshot(
        self, strategy_run_id: str, bracket_intent_id: str, *, session_nonce: str
    ) -> WorkerBracketActionResult:
        return WorkerBracketActionResult.model_validate(
            await self.cancel_bracket(strategy_run_id, bracket_intent_id, session_nonce=session_nonce)
        )

    async def list_execution_events(
        self,
        strategy_run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 200,
        basket_execution_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> JsonDict:
        return await self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/execution-events",
            params={
                "after_cursor": after_cursor,
                "limit": limit,
                "basket_execution_id": basket_execution_id,
                "event_type": event_type,
            },
        )

    async def list_execution_events_snapshot(
        self, strategy_run_id: str, **params: Any
    ) -> WorkerExecutionEventsResponse:
        return WorkerExecutionEventsResponse.model_validate(
            await self.list_execution_events(strategy_run_id, **params)
        )

    async def export_fundamentals_csv(
        self,
        *,
        symbols: Optional[Iterable[str]] = None,
        index: Optional[str] = None,
        dataset: str = "fundamentals_features",
        schema_version: int = 1,
    ) -> str:
        params = fundamentals_scope_params(symbols, index)
        params.update({"dataset": dataset, "schema_version": schema_version})
        return await self._request_text("GET", "/worker/fundamentals/export.csv", params=params)

    async def get_candles(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> JsonDict:
        """Return recent live/cache candles.

        Use get_historical_candles() for daily history and broader historical ranges.
        """
        params: JsonDict = {"interval": interval, "lookback": lookback}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        return await self._request("GET", "/worker/market/candles", params=params)

    async def get_candles_snapshot(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(await self.get_candles(instrument, interval=interval, lookback=lookback))

    async def get_historical_candles(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str | datetime] = None,
        to_date: Optional[str | datetime] = None,
        lookback_days: Optional[int] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> JsonDict:
        params: JsonDict = {"timeframe": timeframe, "ingest": ingest, "passthrough": passthrough}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        params.update(build_historical_date_params(from_date=from_date, to_date=to_date, lookback_days=lookback_days))
        return await self._request("GET", "/worker/market/history", params=params)

    async def get_historical_candles_snapshot(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str | datetime] = None,
        to_date: Optional[str | datetime] = None,
        lookback_days: Optional[int] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(
            await self.get_historical_candles(
                instrument,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                lookback_days=lookback_days,
                ingest=ingest,
                passthrough=passthrough,
            )
        )

    async def resolve_ticker(self, symbol: str) -> JsonDict:
        return await self._request("GET", "/worker/market/instruments/resolve", params={"symbol": symbol})

    async def resolve_tickers(self, instruments: Iterable[str | int]) -> JsonDict:
        symbols, tokens = split_instruments(instruments)
        return await self._request(
            "POST",
            "/worker/market/instruments/resolve",
            json={"symbols": symbols, "instrument_tokens": tokens},
        )

    async def search_tickers(self, query: str, exchange: Optional[str] = None, limit: int = 20) -> JsonDict:
        params: JsonDict = {"query": query, "limit": limit}
        if exchange:
            params["exchange"] = exchange
        return await self._request("GET", "/worker/market/instruments/search", params=params)

    async def get_quotes(self, instruments: Iterable[str | int], mode: str = "quote") -> JsonDict:
        symbols, tokens = split_instruments(instruments)
        return await self._request(
            "POST",
            "/worker/market/quotes",
            json={"symbols": symbols, "instrument_tokens": tokens, "mode": mode},
        )

    async def get_market_snapshot(
        self,
        *,
        symbols: Optional[list[str]] = None,
        instrument_tokens: Optional[list[int]] = None,
        candles: Optional[list[Mapping[str, Any]]] = None,
        mode: str = "quote",
    ) -> JsonDict:
        return await self._request(
            "POST",
            "/worker/market/snapshot",
            json={
                "symbols": symbols or [],
                "instrument_tokens": instrument_tokens or [],
                "candles": list(candles or []),
                "mode": mode,
            },
        )

    async def preview_order(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/order",
            json={"order": dict(order), "metadata": dict(metadata or {})},
        )

    async def preview_basket(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
    ) -> JsonDict:
        return await self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/basket",
            json={
                "orders": [dict(order) for order in orders],
                "metadata": dict(metadata or {}),
                "all_or_none": all_or_none,
            },
        )

    async def preview_order_snapshot(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> OrderPreview:
        """Typed order preview. Previews never submit orders."""
        return OrderPreview.model_validate(await self.preview_order(strategy_run_id, order, metadata=metadata))

    async def preview_basket_snapshot(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
    ) -> OrderPreview:
        """Typed basket preview. Previews never submit orders."""
        return OrderPreview.model_validate(
            await self.preview_basket(strategy_run_id, orders, metadata=metadata, all_or_none=all_or_none)
        )

    def stream_ticks(self, instruments: Iterable[str | int], mode: str = "quote"):
        symbols, tokens = split_instruments(instruments)
        return self._stream_sse(
            "GET",
            "/worker/market/ticks/stream",
            params={
                "symbols": ",".join(symbols),
                "tokens": ",".join(str(token) for token in tokens),
                "mode": mode,
            },
        )

    def stream_candles(self, instrument: str | int, interval: str = "5minute"):
        value = str(instrument).strip()
        params: JsonDict = {"interval": interval}
        if isinstance(instrument, int) or value.isdigit():
            params["instrument_token"] = int(value)
        else:
            params["symbol"] = value
        return self._stream_sse("GET", "/worker/market/candles/stream", params=params)

    def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0):
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/pnl/stream",
            params={"interval_seconds": interval_seconds},
        )

    def stream_timeline(self, strategy_run_id: str, **params: Any):
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/timeline/stream",
            params=dict(params or {}),
        )

    def stream_execution_events(self, strategy_run_id: str, **params: Any):
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/execution-events/stream",
            params=dict(params or {}),
        )

    async def wait_for_terminal_order_state(
        self,
        strategy_run_id: str,
        order_id: str,
        *,
        attempts: int = 20,
        sleep_seconds: float = 1.0,
    ) -> WorkerOrderSnapshot:
        last_snapshot: Optional[WorkerOrderSnapshot] = None
        for _ in range(attempts):
            last_snapshot = await self.get_order_snapshot(strategy_run_id, order_id)
            if last_snapshot.status in {"COMPLETE", "CANCELLED", "REJECTED"}:
                return last_snapshot
            await asyncio.sleep(sleep_seconds)
        if last_snapshot is None:
            raise RuntimeError("wait_for_terminal_order_state exhausted without fetching an order snapshot")
        return last_snapshot

    async def _request_text(self, method: str, path: str, **kwargs: Any) -> str:
        response = await self.client.request(method, self._url(path), **kwargs)
        if 200 <= response.status_code < 300:
            return response.text
        # ``httpx.Response`` bodies are already read by ``request``.  Test
        # doubles may expose an explicit ``aread``; consume it before parsing
        # either JSON or text so error details are never lost.
        aread = getattr(response, "aread", None)
        if aread is not None:
            await aread()
        try:
            body: Any = response.json()
        except (TypeError, ValueError):
            body = {"raw": response.text}
        raise error_for_status(
            response.status_code,
            body,
            fallback=f"Worker API returned {response.status_code} for {method} {path}",
        )

    async def _stream_sse(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None):
        async with self.client.stream(method, self._url(path), params=dict(params or {})) as response:
            if not 200 <= response.status_code < 300:
                aread = getattr(response, "aread", None)
                if aread is not None:
                    await aread()
                try:
                    body: Any = response.json()
                except (TypeError, ValueError):
                    body = {"raw": response.text}
                raise error_for_status(
                    response.status_code,
                    body,
                    fallback=f"Worker API returned {response.status_code} for {method} {path}",
                )

            current_event = "message"
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip() or "message"
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line.split(":", 1)[1].strip()
                if not payload:
                    continue
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise KiteAlgoWorkerError(
                        f"Worker API stream at {path} returned invalid JSON: {exc}",
                        status_code=0,
                        response_body=payload,
                    ) from exc
                if current_event == "error":
                    raise KiteAlgoWorkerError(
                        f"Worker API stream error at {path}: "
                        f"{decoded.get('detail') if isinstance(decoded, dict) else decoded}",
                        status_code=0,
                        response_body=decoded,
                    )
                if current_event == "end":
                    return
                yield decoded
                current_event = "message"

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        return await self._request_url(method, self._url(path), **kwargs)

    async def _request_url(self, method: str, url: str, **kwargs: Any) -> JsonDict:
        response = await self.client.request(method, url, **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        # ``httpx.AsyncClient.request`` normally reads non-streaming response
        # bodies for us, but a custom transport/test double may leave the body
        # unread.  Consume it before attempting JSON/text error parsing so the
        # SDK never drops the backend's detail payload.
        aread = getattr(response, "aread", None)
        if aread is not None:
            await aread()
        try:
            body: Any = response.json()
        except ValueError:
            body = {"raw": response.text}
        raise error_for_status(response.status_code, body, fallback=f"Worker API returned {response.status_code} for {method} {url}")


__all__ = ["AsyncKiteAlgoWorkerClient"]
