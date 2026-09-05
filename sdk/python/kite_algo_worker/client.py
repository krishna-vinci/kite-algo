from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

import requests

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
from .run_config import RunConfig
from .models import (
    OrderPreview,
    RunProtectionState,
    SafetyCheckResult,
    WorkerFundsSnapshot,
    WorkerGttTrigger,
    WorkerGttWriteResult,
    WorkerHistoricalCandles,
    WorkerOrderSnapshot,
    WorkerOrdersResponse,
    WorkerRunHealthSnapshot,
    WorkerRunPnlSnapshot,
    WorkerTimelineResponse,
    WorkerTradesResponse,
)
from .options.client import OptionWorkerClient
from .protection import BackendProtection


JsonDict = Dict[str, Any]


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _build_historical_date_params(
    *,
    from_date: Optional[str | datetime] = None,
    to_date: Optional[str | datetime] = None,
    lookback_days: Optional[int] = None,
) -> JsonDict:
    if lookback_days is not None and lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if from_date is not None and lookback_days is not None:
        raise ValueError("from_date and lookback_days are mutually exclusive")

    params: JsonDict = {}
    if to_date is not None:
        params["to"] = to_date.isoformat() if isinstance(to_date, datetime) else to_date
    elif lookback_days is not None:
        params["to"] = datetime.now(timezone.utc).isoformat()

    if from_date is not None:
        params["from"] = from_date.isoformat() if isinstance(from_date, datetime) else from_date
    elif lookback_days is not None:
        to_dt = _coerce_datetime(params["to"])
        if to_dt.tzinfo is None:
            raise ValueError("to_date must include timezone information when lookback_days is used")
        params["from"] = (to_dt - timedelta(days=int(lookback_days))).isoformat()

    return params


def _require_identity_param(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _fundamentals_scope_params(symbols: Optional[Iterable[str]], index: Optional[str]) -> JsonDict:
    """Validate the exclusive symbols/index scope shared by fundamentals methods."""
    if bool(symbols) == bool(index):
        raise ValueError("provide exactly one of 'symbols' or 'index'")
    if symbols:
        cleaned = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not cleaned:
            raise ValueError("symbols must not be empty when provided")
        return {"symbols": cleaned}
    index_text = str(index or "").strip()
    if not index_text:
        raise ValueError("index must not be empty when provided")
    return {"index": index_text}


def _normalize_calendar_date_params(from_date: Any, to_date: Any, *, exchange: Any, segment: Any) -> JsonDict:
    from_text = _require_identity_param(from_date, field_name="from_date")
    to_text = _require_identity_param(to_date, field_name="to_date")
    try:
        start = date.fromisoformat(from_text)
        end = date.fromisoformat(to_text)
    except ValueError as exc:
        raise ValueError("from_date and to_date must be ISO dates (YYYY-MM-DD)") from exc
    if start > end:
        raise ValueError("from_date must not be after to_date")
    exchange_text = _require_identity_param(exchange, field_name="exchange").upper()
    segment_text = _require_identity_param(segment, field_name="segment").upper()
    return {
        "from": from_text,
        "to": to_text,
        "exchange": exchange_text,
        "segment": segment_text,
    }


@dataclass(frozen=True)
class AlgoWorkerConfig:
    """Connection settings for the Kite Algo worker API."""

    base_url: str
    token: str
    timeout: float = 10.0
    api_prefix: str = "/api/algo-workers"


class KiteAlgoWorkerClient:
    """Small, boring HTTP client for external algo workers.

    The client only calls public `/api/algo-workers/worker/*` endpoints. It never
    calls broker, database, paper-runtime, or market-runtime internals.
    """

    def __init__(self, config: AlgoWorkerConfig) -> None:
        if not config.base_url:
            raise ValueError("base_url is required")
        if not config.token:
            raise ValueError("token is required")
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.options = OptionWorkerClient(self)

    def health(self) -> JsonDict:
        return self._request("GET", "/worker/health")

    def heartbeat(
        self,
        worker_id: Optional[str] = None,
        status: str = "healthy",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        payload: JsonDict = {"status": status, "metrics": dict(metrics or {})}
        if worker_id is not None:
            payload["worker_id"] = worker_id
        return self._request("POST", "/worker/heartbeat", json=payload)

    def create_run(
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
        runtime_state_payload: JsonDict = dict(runtime_state or {})
        if backend_protection is not None:
            runtime_state_payload["backend_protection"] = backend_protection.to_dict()
        payload: JsonDict = {
            "template_id": template_id,
            "account_scope": account_scope,
            "execution_mode": execution_mode,
            "summary_fields": [dict(item) for item in (summary_fields or [])],
            "risk_schema": [dict(item) for item in (risk_schema or [])],
            "allowed_actions": list(allowed_actions or ["edit_risk", "exit_strategy"]),
            "runtime_state": runtime_state_payload,
            "metadata": dict(metadata or {}),
        }
        if strategy_run_id is not None:
            payload["strategy_run_id"] = strategy_run_id
        return self._request("POST", "/worker/runs", json=payload)

    def create_run_from_config(self, config: RunConfig) -> JsonDict:
        return self._request("POST", "/worker/runs", json=config.to_create_run_payload())

    @contextmanager
    def run(
        self,
        config: RunConfig,
        *,
        claim_session: bool = True,
        heartbeat_on_enter: bool = True,
        release_on_exit: bool = True,
    ):
        if heartbeat_on_enter and not claim_session:
            raise ValueError("heartbeat_on_enter requires claim_session=True")

        run_payload = _get_or_create_run_with_validation(self, config)
        run_id = str(run_payload["strategy_run_id"])
        session_nonce: str | None = None

        if claim_session:
            claim = self.claim_session(run_id)
            session_nonce = str(claim["worker_session_nonce"])
            if heartbeat_on_enter:
                self.run_heartbeat(run_id, session_nonce=session_nonce)

        from .managed_run import ManagedRun

        managed = ManagedRun(client=self, config=config, run=run_payload, session_nonce=session_nonce)
        body_error: Exception | None = None
        try:
            yield managed
        except Exception as exc:
            body_error = exc
            raise
        finally:
            if release_on_exit and session_nonce:
                try:
                    self.release_session(run_id, session_nonce=session_nonce)
                except Exception:
                    if body_error is None:
                        raise

    def get_run(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}")

    def get_run_health_snapshot(self, strategy_run_id: str) -> WorkerRunHealthSnapshot:
        return WorkerRunHealthSnapshot.model_validate(self.get_run(strategy_run_id))

    def claim_session(self, strategy_run_id: str) -> JsonDict:
        return self._request("POST", f"/worker/runs/{strategy_run_id}/claim-session")

    def release_session(self, strategy_run_id: str, *, session_nonce: str) -> JsonDict:
        return self._request(
            "DELETE",
            f"/worker/runs/{strategy_run_id}/claim-session",
            headers={"X-Worker-Session-Nonce": str(session_nonce)},
        )

    def run_heartbeat(
        self,
        strategy_run_id: str,
        *,
        session_nonce: str,
        worker_id: Optional[str] = None,
        status: str = "healthy",
        metrics: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        payload: JsonDict = {"status": status, "metrics": dict(metrics or {})}
        if worker_id is not None:
            payload["worker_id"] = worker_id
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/heartbeat",
            headers={"X-Worker-Session-Nonce": str(session_nonce)},
            json=payload,
        )

    def safety_check(self, strategy_run_id: str) -> SafetyCheckResult:
        return SafetyCheckResult.model_validate(self._request("GET", f"/worker/runs/{strategy_run_id}/safety-check"))

    def get_run_pnl(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}/pnl")

    def get_run_pnl_snapshot(self, strategy_run_id: str) -> WorkerRunPnlSnapshot:
        return WorkerRunPnlSnapshot.model_validate(self.get_run_pnl(strategy_run_id))

    def list_orders(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", "/worker/orders", params={"strategy_run_id": strategy_run_id})

    def list_trades(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", "/worker/trades", params={"strategy_run_id": strategy_run_id})

    def get_orders_snapshot(self, strategy_run_id: str) -> WorkerOrdersResponse:
        return WorkerOrdersResponse.model_validate(self.list_orders(strategy_run_id))

    def get_trades_snapshot(self, strategy_run_id: str) -> WorkerTradesResponse:
        return WorkerTradesResponse.model_validate(self.list_trades(strategy_run_id))

    def get_order_snapshot(self, strategy_run_id: str, order_id: str) -> WorkerOrderSnapshot:
        response = self._request("GET", f"/worker/orders/{order_id}", params={"strategy_run_id": strategy_run_id})
        return WorkerOrderSnapshot.model_validate(response.get("order") or response)

    def cancel_order(self, strategy_run_id: str, order_id: str, *, variety: str = "regular") -> JsonDict:
        return self._request(
            "POST",
            f"/worker/orders/{order_id}/cancel",
            json={"strategy_run_id": strategy_run_id, "variety": variety},
        )

    def modify_order(self, strategy_run_id: str, order_id: str, patch: Mapping[str, Any], *, variety: str = "regular") -> JsonDict:
        return self._request(
            "POST",
            f"/worker/orders/{order_id}/modify",
            json={"strategy_run_id": strategy_run_id, "variety": variety, **dict(patch)},
        )

    def preview_order(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/order",
            json={"order": dict(order), "metadata": dict(metadata or {})},
        )

    def preview_basket(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/preview/basket",
            json={
                "orders": [dict(order) for order in orders],
                "metadata": dict(metadata or {}),
                "all_or_none": all_or_none,
            },
        )

    def preview_order_snapshot(self, strategy_run_id: str, order: Mapping[str, Any], *, metadata: Optional[Mapping[str, Any]] = None) -> OrderPreview:
        """Typed order preview. Previews never submit orders."""
        return OrderPreview.model_validate(self.preview_order(strategy_run_id, order, metadata=metadata))

    def preview_basket_snapshot(
        self,
        strategy_run_id: str,
        orders: Iterable[Mapping[str, Any]],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        all_or_none: bool = False,
    ) -> OrderPreview:
        """Typed basket preview. Previews never submit orders."""
        return OrderPreview.model_validate(
            self.preview_basket(strategy_run_id, orders, metadata=metadata, all_or_none=all_or_none)
        )

    def get_run_protection_state(self, strategy_run_id: str) -> JsonDict:
        run = self.get_run(strategy_run_id)
        runtime_state = dict(run.get("runtime_state") or {})
        state = dict(runtime_state.get("backend_protection_state") or {})
        return RunProtectionState.model_validate(state).model_dump()

    def place_gtt(self, payload: Mapping[str, Any]) -> JsonDict:
        return self._request("POST", "/worker/gtt/triggers", json=dict(payload))

    def place_gtt_snapshot(self, payload: Mapping[str, Any]) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(self.place_gtt(payload))

    def list_gtts(self) -> List[JsonDict]:
        response = self._request("GET", "/worker/gtt/triggers")
        if not isinstance(response, list):
            return []
        return [dict(item) for item in response if isinstance(item, Mapping)]

    def list_gtts_snapshot(self) -> List[WorkerGttTrigger]:
        return [WorkerGttTrigger.model_validate(item) for item in self.list_gtts()]

    def get_gtt(self, trigger_id: int) -> JsonDict:
        return self._request("GET", f"/worker/gtt/triggers/{int(trigger_id)}")

    def get_gtt_snapshot(self, trigger_id: int) -> WorkerGttTrigger:
        return WorkerGttTrigger.model_validate(self.get_gtt(trigger_id))

    def modify_gtt(self, trigger_id: int, payload: Mapping[str, Any]) -> JsonDict:
        return self._request("PUT", f"/worker/gtt/triggers/{int(trigger_id)}", json=dict(payload))

    def modify_gtt_snapshot(self, trigger_id: int, payload: Mapping[str, Any]) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(self.modify_gtt(trigger_id, payload))

    def delete_gtt(self, trigger_id: int) -> JsonDict:
        return self._request("DELETE", f"/worker/gtt/triggers/{int(trigger_id)}")

    def delete_gtt_snapshot(self, trigger_id: int) -> WorkerGttWriteResult:
        return WorkerGttWriteResult.model_validate(self.delete_gtt(trigger_id))

    def get_funds(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> JsonDict:
        params: JsonDict = {"mode": mode}
        if account_scope is not None:
            params["account_scope"] = account_scope
        return self._request("GET", "/worker/funds", params=params)

    def get_funds_snapshot(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> WorkerFundsSnapshot:
        return WorkerFundsSnapshot.model_validate(self.get_funds(mode=mode, account_scope=account_scope))

    def get_run_funds(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}/funds")

    def get_index_constituents(self, source_list: str, *, schema_version: int = 1) -> JsonDict:
        source = _require_identity_param(source_list, field_name="source_list")
        return self._request(
            "GET",
            f"/worker/market/indices/{source}",
            params={"schema_version": schema_version},
        )

    def get_index_constituents_snapshot(self, source_list: str, *, schema_version: int = 1) -> WorkerIndexConstituentsSnapshot:
        return WorkerIndexConstituentsSnapshot.model_validate(
            self.get_index_constituents(source_list, schema_version=schema_version)
        )

    def get_index_constituent_status(self, source_list: str, *, schema_version: int = 1) -> JsonDict:
        source = _require_identity_param(source_list, field_name="source_list")
        return self._request(
            "GET",
            f"/worker/market/indices/{source}/status",
            params={"schema_version": schema_version},
        )

    def get_index_constituent_status_snapshot(self, source_list: str, *, schema_version: int = 1) -> WorkerIndexConstituentStatus:
        return WorkerIndexConstituentStatus.model_validate(
            self.get_index_constituent_status(source_list, schema_version=schema_version)
        )

    def get_market_calendar(self, from_date: Any, to_date: Any, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> JsonDict:
        params = _normalize_calendar_date_params(from_date, to_date, exchange=exchange, segment=segment)
        params["schema_version"] = schema_version
        return self._request("GET", "/worker/market/calendar", params=params)

    def get_market_calendar_snapshot(self, from_date: Any, to_date: Any, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> WorkerMarketCalendarSnapshot:
        return WorkerMarketCalendarSnapshot.model_validate(
            self.get_market_calendar(from_date, to_date, exchange=exchange, segment=segment, schema_version=schema_version)
        )

    def get_market_calendar_status(self, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> JsonDict:
        exchange_text = _require_identity_param(exchange, field_name="exchange").upper()
        segment_text = _require_identity_param(segment, field_name="segment").upper()
        return self._request(
            "GET",
            "/worker/market/calendar/status",
            params={"exchange": exchange_text, "segment": segment_text, "schema_version": schema_version},
        )

    def get_market_calendar_status_snapshot(self, *, exchange: str = "NSE", segment: str = "CM", schema_version: int = 1) -> WorkerMarketCalendarStatus:
        return WorkerMarketCalendarStatus.model_validate(
            self.get_market_calendar_status(exchange=exchange, segment=segment, schema_version=schema_version)
        )

    def get_account_portfolio(self, *, account_scope: Optional[str] = None, schema_version: int = 1) -> JsonDict:
        params: JsonDict = {"schema_version": schema_version}
        if account_scope is not None:
            scope_text = str(account_scope).strip()
            if not scope_text:
                raise ValueError("account_scope must not be empty when provided")
            params["account_scope"] = scope_text
        return self._request("GET", "/worker/account/portfolio", params=params)

    def get_account_portfolio_snapshot(self, *, account_scope: Optional[str] = None, schema_version: int = 1) -> WorkerAccountPortfolioSnapshot:
        return WorkerAccountPortfolioSnapshot.model_validate(
            self.get_account_portfolio(account_scope=account_scope, schema_version=schema_version)
        )

    # -- Fundamentals (0.7.7; read-only except refresh_fundamentals) --------

    def get_fundamentals_features(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalFeatures:
        """Typed fundamentals feature snapshot for symbols or an index universe."""
        params = _fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalFeatures.model_validate(
            self._request_api_root("GET", "fundamentals/features", params=params)
        )

    def get_fundamentals_status(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalsStatus:
        """Per-symbol fundamentals freshness plus recent sync-run history."""
        params = _fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalsStatus.model_validate(
            self._request_api_root("GET", "fundamentals/status", params=params)
        )

    def get_fundamentals_statements(self, symbol: str, *, dataset: str, statement_scope: str = "consolidated") -> FundamentalsStatements:
        """Raw statement rows for one symbol and dataset (e.g. ``quarterly``)."""
        symbol_text = _require_identity_param(symbol, field_name="symbol")
        if not str(dataset).strip():
            raise ValueError("dataset is required")
        return FundamentalsStatements.model_validate(
            self._request_api_root(
                "GET",
                "fundamentals/statements",
                params={
                    "symbol": symbol_text.upper(),
                    "dataset": dataset,
                    "statement_scope": statement_scope,
                    "schema_version": 1,
                },
            )
        )

    def refresh_fundamentals(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None, mode: str = "incremental") -> FundamentalsSyncRun:
        """Trigger an on-demand fundamentals sync. This is the only mutating
        fundamentals method: the server caps symbol scopes at 50 per request
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
        return FundamentalsSyncRun.model_validate(self._request_api_root("POST", "fundamentals/sync", json=body))

    def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> Iterator[JsonDict]:
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/pnl/stream",
            params={"interval_seconds": interval_seconds},
        )

    def log_decision_event(self, strategy_run_id: str, **payload: Any) -> JsonDict:
        return self._request("POST", f"/worker/runs/{strategy_run_id}/decision-events", json=dict(payload))

    def list_timeline(self, strategy_run_id: str, **params: Any) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}/timeline", params=dict(params or {}))

    def list_timeline_snapshot(self, strategy_run_id: str, **params: Any) -> WorkerTimelineResponse:
        return WorkerTimelineResponse.model_validate(self.list_timeline(strategy_run_id, **params))

    def stream_timeline(self, strategy_run_id: str, **params: Any) -> Iterator[JsonDict]:
        return self._stream_sse("GET", f"/worker/runs/{strategy_run_id}/timeline/stream", params=dict(params or {}))

    def resolve_ticker(self, symbol: str) -> JsonDict:
        return self._request("GET", "/worker/market/instruments/resolve", params={"symbol": symbol})

    def resolve_tickers(self, instruments: Iterable[str | int]) -> JsonDict:
        symbols, tokens = self._split_instruments(instruments)
        return self._request(
            "POST",
            "/worker/market/instruments/resolve",
            json={"symbols": symbols, "instrument_tokens": tokens},
        )

    def search_tickers(self, query: str, exchange: Optional[str] = None, limit: int = 20) -> JsonDict:
        params: JsonDict = {"query": query, "limit": limit}
        if exchange:
            params["exchange"] = exchange
        return self._request("GET", "/worker/market/instruments/search", params=params)

    def get_quotes(self, instruments: Iterable[str | int], mode: str = "quote") -> JsonDict:
        symbols, tokens = self._split_instruments(instruments)
        return self._request(
            "POST",
            "/worker/market/quotes",
            json={"symbols": symbols, "instrument_tokens": tokens, "mode": mode},
        )

    def stream_ticks(self, instruments: Iterable[str | int], mode: str = "quote") -> Iterator[JsonDict]:
        symbols, tokens = self._split_instruments(instruments)
        return self._stream_sse(
            "GET",
            "/worker/market/ticks/stream",
            params={
                "symbols": ",".join(symbols),
                "tokens": ",".join(str(token) for token in tokens),
                "mode": mode,
            },
        )

    def get_candles(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> JsonDict:
        """Return recent live/cache candles.

        This reads the worker live candle cache (`/worker/market/candles`). For
        full historical ranges, daily warmups, or broker passthrough history use
        :meth:`get_historical_candles` instead.
        """
        params: JsonDict = {"interval": interval, "lookback": lookback}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        return self._request("GET", "/worker/market/candles", params=params)

    def get_current_candle(self, instrument: str | int, interval: str = "5minute") -> Optional[JsonDict]:
        return self.get_candles(instrument, interval=interval, lookback=1).get("current")

    def get_candles_snapshot(self, instrument: str | int, interval: str = "5minute", lookback: int = 50) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(self.get_candles(instrument, interval=interval, lookback=lookback))

    def get_historical_candles_snapshot(
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
            self.get_historical_candles(
                instrument,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                lookback_days=lookback_days,
                ingest=ingest,
                passthrough=passthrough,
            )
        )

    def get_historical_candles(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str | datetime] = None,
        to_date: Optional[str | datetime] = None,
        lookback_days: Optional[int] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> JsonDict:
        """Return historical candles via `/worker/market/history`.

        Use this for daily history and warmup/backtest windows. `get_candles()`
        intentionally remains a recent live/cache surface and may be empty for
        `interval="day"` when no live daily candle cache is present.
        """
        params: JsonDict = {"timeframe": timeframe, "ingest": ingest, "passthrough": passthrough}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        params.update(_build_historical_date_params(from_date=from_date, to_date=to_date, lookback_days=lookback_days))
        return self._request("GET", "/worker/market/history", params=params)

    def stream_candles(self, instrument: str | int, interval: str = "5minute") -> Iterator[JsonDict]:
        params: JsonDict = {"interval": interval}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        return self._stream_sse("GET", "/worker/market/candles/stream", params=params)

    def get_market_snapshot(
        self,
        symbols: Optional[List[str]] = None,
        instrument_tokens: Optional[List[int]] = None,
        candles: Optional[List[Mapping[str, Any]]] = None,
        mode: str = "quote",
    ) -> JsonDict:
        return self._request(
            "POST",
            "/worker/market/snapshot",
            json={
                "symbols": symbols or [],
                "instrument_tokens": instrument_tokens or [],
                "candles": list(candles or []),
                "mode": mode,
            },
        )

    def _stream_sse(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None) -> Iterator[JsonDict]:
        response = self.session.request(
            method,
            self._url(path),
            timeout=(self.config.timeout, None),
            stream=True,
            params=dict(params or {}),
        )
        if not 200 <= response.status_code < 300:
            try:
                self._raise_response_error(response, method, path)
            finally:
                response.close()

        def _events() -> Iterator[JsonDict]:
            current_event = "message"
            try:
                for line in response.iter_lines(decode_unicode=True):
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
                            f"Worker API stream error at {path}: {decoded.get('detail') if isinstance(decoded, dict) else decoded}",
                            status_code=0,
                            response_body=decoded,
                        )
                    if current_event == "end":
                        break
                    yield decoded
                    current_event = "message"
            finally:
                response.close()

        return _events()

    @staticmethod
    def _split_instruments(instruments: Iterable[str | int]) -> tuple[List[str], List[int]]:
        symbols: List[str] = []
        tokens: List[int] = []
        for item in instruments:
            value = str(item).strip()
            if isinstance(item, int) or value.isdigit():
                tokens.append(int(value))
            else:
                symbols.append(value)
        return symbols, tokens

    def place_order(
        self,
        strategy_run_id: str,
        order: Mapping[str, Any],
        idempotency_key: str,
        metadata: Optional[Mapping[str, Any]] = None,
        safety_token: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        key = self._require_idempotency_key(idempotency_key)
        payload: JsonDict = {
            "intent_type": "place_order",
            "payload": {"order": dict(order)},
            "idempotency_key": key,
            "metadata": dict(metadata or {}),
        }
        if safety_token is not None:
            payload["safety_token"] = str(safety_token)
        headers = {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None
        return self._request("POST", f"/worker/runs/{strategy_run_id}/intents", json=payload, headers=headers)

    def place_basket(
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
        key = self._require_idempotency_key(idempotency_key)
        order_list: List[JsonDict] = [dict(order) for order in orders]
        payload: JsonDict = {
            "intent_type": "place_basket",
            "payload": {"basket": {"orders": order_list, "all_or_none": all_or_none, "dry_run": dry_run}},
            "idempotency_key": key,
            "metadata": dict(metadata or {}),
        }
        if safety_token is not None:
            payload["safety_token"] = str(safety_token)
        headers = {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None
        return self._request("POST", f"/worker/runs/{strategy_run_id}/intents", json=payload, headers=headers)

    def patch_risk(
        self,
        strategy_run_id: str,
        patch: Mapping[str, Any],
        reason: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        headers = {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None
        return self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/risk",
            json={"patch": dict(patch), "reason": reason},
            headers=headers,
        )

    def update_backend_protection(
        self,
        strategy_run_id: str,
        backend_protection: BackendProtection,
        *,
        reason: Optional[str] = None,
        reset_trailing: bool = True,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        headers = {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None
        return self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/protection",
            json={
                "backend_protection": backend_protection.to_dict(),
                "reason": reason,
                "reset_trailing": reset_trailing,
            },
            headers=headers,
        )

    def exit_run(
        self,
        strategy_run_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dry_run: bool = False,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        headers = {"X-Worker-Session-Nonce": str(session_nonce)} if session_nonce is not None else None
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/exit",
            json={"reason": reason, "idempotency_key": idempotency_key, "dry_run": dry_run},
            headers=headers,
        )

    @staticmethod
    def _require_idempotency_key(idempotency_key: str) -> str:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for order intents")
        if not 8 <= len(key) <= 160:
            raise ValueError("idempotency_key must be between 8 and 160 characters")
        return key

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        return self._request_url(method, self._url(path), **kwargs)

    def _request_api_root(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        """Request a path relative to the server API root (outside the
        ``/algo-workers`` worker prefix, e.g. ``fundamentals/features``)."""
        base = self.config.base_url.rstrip("/")
        return self._request_url(method, f"{base}/{path.lstrip('/')}", **kwargs)

    def _request_url(self, method: str, url: str, **kwargs: Any) -> JsonDict:
        response = self.session.request(method, url, timeout=self.config.timeout, **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        self._raise_response_error(response, method, url)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_response_error(response: requests.Response, method: str, path: str) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        raise error_for_status(response.status_code, body, fallback=f"Worker API returned {response.status_code} for {method} {path}")


def _get_or_create_run_with_validation(client: KiteAlgoWorkerClient, config: RunConfig) -> dict[str, Any]:
    if config.strategy_run_id:
        try:
            existing = client.get_run(config.strategy_run_id)
        except KiteAlgoWorkerError as exc:
            if exc.status_code != 404:
                raise
        else:
            mismatches = {
                "template_id": (existing.get("template_id"), config.template_id),
                "account_scope": (existing.get("account_scope"), config.account_scope),
                "execution_mode": (existing.get("execution_mode"), config.execution_mode),
            }
            wrong = {key: value for key, value in mismatches.items() if str(value[0]) != str(value[1])}
            if wrong:
                raise KiteAlgoWorkerError(
                    f"RunConfig mismatch for {config.strategy_run_id}: {wrong}",
                    status_code=409,
                )
            return existing
    return client.create_run_from_config(config)
