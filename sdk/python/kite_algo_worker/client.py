from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

import requests

from .exceptions import KiteAlgoWorkerError, error_for_status
from .models import (
    RunProtectionState,
    SafetyCheckResult,
    WorkerFundsSnapshot,
    WorkerHistoricalCandles,
    WorkerOrderSnapshot,
    WorkerOrdersResponse,
    WorkerRunPnlSnapshot,
    WorkerTradesResponse,
)
from .options.client import OptionWorkerClient
from .protection import BackendProtection


JsonDict = Dict[str, Any]


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

    def get_run(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}")

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

    def get_run_protection_state(self, strategy_run_id: str) -> JsonDict:
        run = self.get_run(strategy_run_id)
        runtime_state = dict(run.get("runtime_state") or {})
        state = dict(runtime_state.get("backend_protection_state") or {})
        return RunProtectionState.model_validate(state).model_dump()

    def get_funds(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> JsonDict:
        params: JsonDict = {"mode": mode}
        if account_scope is not None:
            params["account_scope"] = account_scope
        return self._request("GET", "/worker/funds", params=params)

    def get_funds_snapshot(self, *, mode: str = "paper", account_scope: Optional[str] = None) -> WorkerFundsSnapshot:
        return WorkerFundsSnapshot.model_validate(self.get_funds(mode=mode, account_scope=account_scope))

    def get_run_funds(self, strategy_run_id: str) -> JsonDict:
        return self._request("GET", f"/worker/runs/{strategy_run_id}/funds")

    def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> Iterator[JsonDict]:
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/pnl/stream",
            params={"interval_seconds": interval_seconds},
        )

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
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> WorkerHistoricalCandles:
        return WorkerHistoricalCandles.model_validate(
            self.get_historical_candles(
                instrument,
                timeframe=timeframe,
                from_date=from_date,
                to_date=to_date,
                ingest=ingest,
                passthrough=passthrough,
            )
        )

    def get_historical_candles(
        self,
        instrument: str | int,
        timeframe: str = "day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ingest: bool = True,
        passthrough: bool = False,
    ) -> JsonDict:
        params: JsonDict = {"timeframe": timeframe, "ingest": ingest, "passthrough": passthrough}
        instrument_value = str(instrument).strip()
        if isinstance(instrument, int) or instrument_value.isdigit():
            params["instrument_token"] = int(instrument_value)
        else:
            params["symbol"] = instrument_value
        if from_date is not None:
            params["from"] = from_date
        if to_date is not None:
            params["to"] = to_date
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
        response = self.session.request(method, self._url(path), timeout=self.config.timeout, **kwargs)
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw": response.text}

        self._raise_response_error(response, method, path)
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_response_error(response: requests.Response, method: str, path: str) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        raise error_for_status(response.status_code, body, fallback=f"Worker API returned {response.status_code} for {method} {path}")
