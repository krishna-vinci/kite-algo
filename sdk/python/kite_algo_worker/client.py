from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, TYPE_CHECKING

import requests

from .exceptions import KiteAlgoWorkerError, error_for_status
from ._shared import (
    build_create_run_payload,
    build_heartbeat_payload,
    build_historical_date_params,
    build_intent_payload,
    fundamentals_scope_params,
    normalize_calendar_date_params,
    require_idempotency_key,
    require_identity_param,
    run_list_params,
    session_headers,
    split_instruments,
    document_payload,
    page_params,
)
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

if TYPE_CHECKING:  # pragma: no cover
    from .managed_run import ManagedRun

from .models import (
    OrderPreview,
    RunProtectionState,
    SafetyCheckResult,
    WorkerFundsSnapshot,
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
    WorkerTimelineResponse,
    WorkerTradesResponse,
)
from .options.client import OptionWorkerClient
from .protection import BackendProtection


JsonDict = Dict[str, Any]

# Keep the old private names importable for downstream code and existing tests.
_build_historical_date_params = build_historical_date_params
_fundamentals_scope_params = fundamentals_scope_params
_normalize_calendar_date_params = normalize_calendar_date_params
_require_identity_param = require_identity_param
# Alerts-platform payload helpers (shared byte-for-byte with the async client).
_document_payload = document_payload
_page_params = page_params


@dataclass(frozen=True)
class AlgoWorkerConfig:
    """Connection settings for the Kite Algo worker API."""

    base_url: str
    token: str
    timeout: float = 10.0
    api_prefix: str = "/api/algo-workers"
    # The alerts-platform authoring surface lives under a DIFFERENT mount
    # (`/api/worker/...`), not beneath ``api_prefix``. Keeping it explicit
    # means the existing worker methods cannot accidentally target the wrong
    # family, and the platform methods cannot silently 404.
    platform_prefix: str = "/api"


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
        payload = build_heartbeat_payload(worker_id=worker_id, status=status, metrics=metrics)
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
        return self._request("POST", "/worker/runs", json=payload)

    def create_run_from_config(self, config: RunConfig) -> JsonDict:
        return self._request("POST", "/worker/runs", json=config.to_create_run_payload())

    def attach_run(
        self,
        run_id: str,
        *,
        session_nonce: str,
        config: RunConfig,
    ) -> "ManagedRun":
        """Attach to an EXISTING run as a hosted child — no lifecycle calls.

        This is the attach-only entry point: it fetches and validates the run and
        returns a :class:`~kite_algo_worker.managed_run.ManagedRun` that carries
        the caller-supplied ``session_nonce`` for authorized operations. It
        deliberately does **not** create a run, claim a session, heartbeat or
        release — those are owned by the supervisor through the lifecycle API,
        and a hosted child token is not permitted to perform them.

        ``client.run(...)`` is unchanged and remains the create/claim path for
        external workers.
        """
        existing = self.get_run(run_id)
        mismatches = {
            "template_id": (existing.get("template_id"), config.template_id),
            "account_scope": (existing.get("account_scope"), config.account_scope),
            "execution_mode": (existing.get("execution_mode"), config.execution_mode),
        }
        wrong = {key: value for key, value in mismatches.items() if str(value[0]) != str(value[1])}
        if wrong:
            raise KiteAlgoWorkerError(
                f"RunConfig mismatch for {run_id}: {wrong}",
                status_code=409,
            )

        from .managed_run import ManagedRun

        return ManagedRun(
            client=self,
            config=config,
            run=existing,
            session_nonce=str(session_nonce),
        )

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

    def list_runs(self, *, limit: int = 25, cursor: Optional[str] = None) -> JsonDict:
        """List runs visible to this worker token using stable pagination."""

        return self._request("GET", "/worker/runs", params=run_list_params(limit=limit, cursor=cursor))

    def get_run_health_snapshot(self, strategy_run_id: str) -> WorkerRunHealthSnapshot:
        return WorkerRunHealthSnapshot.model_validate(self.get_run(strategy_run_id))

    def claim_session(self, strategy_run_id: str) -> JsonDict:
        return self._request("POST", f"/worker/runs/{strategy_run_id}/claim-session")

    def release_session(self, strategy_run_id: str, *, session_nonce: str) -> JsonDict:
        return self._request(
            "DELETE",
            f"/worker/runs/{strategy_run_id}/claim-session",
            headers=session_headers(session_nonce),
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
        payload = build_heartbeat_payload(worker_id=worker_id, status=status, metrics=metrics)
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/heartbeat",
            headers=session_headers(session_nonce),
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

    def get_order_history(self, strategy_run_id: str, order_id: str) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/orders/{order_id}/history",
            params={"strategy_run_id": strategy_run_id},
        )

    def get_order_history_snapshot(self, strategy_run_id: str, order_id: str) -> WorkerOrderHistoryResponse:
        return WorkerOrderHistoryResponse.model_validate(self.get_order_history(strategy_run_id, order_id))

    def cancel_order(
        self,
        strategy_run_id: str,
        order_id: str,
        *,
        variety: str = "regular",
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/orders/{order_id}/cancel",
            json={"strategy_run_id": strategy_run_id, "variety": variety},
            headers=session_headers(session_nonce),
        )

    def modify_order(
        self,
        strategy_run_id: str,
        order_id: str,
        patch: Mapping[str, Any],
        *,
        variety: str = "regular",
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/orders/{order_id}/modify",
            json={"strategy_run_id": strategy_run_id, "variety": variety, **dict(patch)},
            headers=session_headers(session_nonce),
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

    def list_baskets(self, strategy_run_id: str, *, limit: int = 100) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/baskets",
            params={"limit": limit},
        )

    def list_baskets_snapshot(self, strategy_run_id: str, *, limit: int = 100) -> WorkerBasketExecutionsResponse:
        return WorkerBasketExecutionsResponse.model_validate(self.list_baskets(strategy_run_id, limit=limit))

    def get_basket(self, strategy_run_id: str, basket_execution_id: str) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/baskets/{basket_execution_id}",
        )

    def get_basket_snapshot(self, strategy_run_id: str, basket_execution_id: str) -> WorkerBasketExecution:
        return WorkerBasketExecution.model_validate(self.get_basket(strategy_run_id, basket_execution_id))

    def create_bracket(
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
        return self._request(
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

    def create_bracket_snapshot(self, strategy_run_id: str, **kwargs: Any) -> WorkerBracketActionResult:
        return WorkerBracketActionResult.model_validate(self.create_bracket(strategy_run_id, **kwargs))

    def list_brackets(self, strategy_run_id: str, *, limit: int = 50) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/brackets",
            params={"limit": limit},
        )

    def list_brackets_snapshot(self, strategy_run_id: str, *, limit: int = 50) -> WorkerBracketListResponse:
        return WorkerBracketListResponse.model_validate(self.list_brackets(strategy_run_id, limit=limit))

    def get_bracket(self, strategy_run_id: str, bracket_intent_id: str) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}",
        )

    def get_bracket_snapshot(self, strategy_run_id: str, bracket_intent_id: str) -> WorkerBracketIntent:
        return WorkerBracketIntent.model_validate(self.get_bracket(strategy_run_id, bracket_intent_id))

    def cancel_bracket(self, strategy_run_id: str, bracket_intent_id: str, *, session_nonce: str) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/brackets/{bracket_intent_id}/cancel",
            headers=session_headers(session_nonce),
        )

    def cancel_bracket_snapshot(
        self,
        strategy_run_id: str,
        bracket_intent_id: str,
        *,
        session_nonce: str,
    ) -> WorkerBracketActionResult:
        return WorkerBracketActionResult.model_validate(
            self.cancel_bracket(strategy_run_id, bracket_intent_id, session_nonce=session_nonce)
        )

    def list_execution_events(
        self,
        strategy_run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 200,
        basket_execution_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "GET",
            f"/worker/runs/{strategy_run_id}/execution-events",
            params={
                "after_cursor": after_cursor,
                "limit": limit,
                "basket_execution_id": basket_execution_id,
                "event_type": event_type,
            },
        )

    def list_execution_events_snapshot(
        self, strategy_run_id: str, **params: Any
    ) -> WorkerExecutionEventsResponse:
        return WorkerExecutionEventsResponse.model_validate(
            self.list_execution_events(strategy_run_id, **params)
        )

    def stream_execution_events(self, strategy_run_id: str, **params: Any) -> Iterator[JsonDict]:
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/execution-events/stream",
            params=dict(params or {}),
        )

    def export_fundamentals_csv(
        self,
        *,
        symbols: Optional[Iterable[str]] = None,
        index: Optional[str] = None,
        dataset: str = "fundamentals_features",
        schema_version: int = 1,
    ) -> str:
        params = fundamentals_scope_params(symbols, index)
        params.update({"dataset": dataset, "schema_version": schema_version})
        return self._request_text("GET", "/worker/fundamentals/export.csv", params=params)

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

    # -- Fundamentals (0.8.0; read-only except refresh_fundamentals) --------

    def get_fundamentals_features(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalFeatures:
        """Typed fundamentals feature snapshot for symbols or an index universe."""
        params = _fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalFeatures.model_validate(
            self._request("GET", "/worker/fundamentals/features", params=params)
        )

    def get_fundamentals_status(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None) -> FundamentalsStatus:
        """Per-symbol fundamentals freshness plus recent sync-run history."""
        params = _fundamentals_scope_params(symbols, index)
        params["schema_version"] = 1
        return FundamentalsStatus.model_validate(
            self._request("GET", "/worker/fundamentals/status", params=params)
        )

    def get_fundamentals_statements(self, symbol: str, *, dataset: str, statement_scope: str = "consolidated") -> FundamentalsStatements:
        """Raw statement rows for one symbol and dataset (e.g. ``quarterly``)."""
        symbol_text = _require_identity_param(symbol, field_name="symbol")
        if not str(dataset).strip():
            raise ValueError("dataset is required")
        return FundamentalsStatements.model_validate(
            self._request(
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

    def refresh_fundamentals(self, *, symbols: Optional[Iterable[str]] = None, index: Optional[str] = None, mode: str = "incremental") -> FundamentalsSyncRun:
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
        return FundamentalsSyncRun.model_validate(self._request("POST", "/worker/fundamentals/sync", json=body))

    def stream_run_pnl(self, strategy_run_id: str, *, interval_seconds: float = 1.0) -> Iterator[JsonDict]:
        return self._stream_sse(
            "GET",
            f"/worker/runs/{strategy_run_id}/pnl/stream",
            params={"interval_seconds": interval_seconds},
        )

    def log_decision_event(
        self,
        strategy_run_id: str,
        *,
        session_nonce: Optional[str] = None,
        **payload: Any,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/decision-events",
            json=dict(payload),
            headers=session_headers(session_nonce),
        )

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

    def calculate_indicator(self, request: Mapping[str, Any]) -> JsonDict:
        """Compute one allowlisted indicator over supplied candles server-side.

        ``request`` mirrors the worker contract: ``name``, ``bars`` and the
        optional ``period``/``fast_period``/``slow_period``/``signal_period``/
        ``multiplier``/``include_forming`` knobs.  The heavy numerical stack
        stays on the worker, so callers never need pandas for this.
        """
        return self._request("POST", "/worker/indicators", json=dict(request))

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
        return split_instruments(instruments)

    def place_order(
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
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json=payload,
            headers=session_headers(session_nonce),
        )

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
        order_list: List[JsonDict] = [dict(order) for order in orders]
        payload = build_intent_payload(
            intent_type="place_basket",
            body_key="basket",
            body={"orders": order_list, "all_or_none": all_or_none, "dry_run": dry_run},
            idempotency_key=idempotency_key,
            metadata=metadata,
            safety_token=safety_token,
        )
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/intents",
            json=payload,
            headers=session_headers(session_nonce),
        )

    def patch_risk(
        self,
        strategy_run_id: str,
        patch: Mapping[str, Any],
        reason: Optional[str] = None,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/risk",
            json={"patch": dict(patch), "reason": reason},
            headers=session_headers(session_nonce),
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
        return self._request(
            "PATCH",
            f"/worker/runs/{strategy_run_id}/protection",
            json={
                "backend_protection": backend_protection.to_dict(),
                "reason": reason,
                "reset_trailing": reset_trailing,
            },
            headers=session_headers(session_nonce),
        )

    def exit_run(
        self,
        strategy_run_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dry_run: bool = False,
        session_nonce: Optional[str] = None,
    ) -> JsonDict:
        return self._request(
            "POST",
            f"/worker/runs/{strategy_run_id}/exit",
            json={"reason": reason, "idempotency_key": idempotency_key, "dry_run": dry_run},
            headers=session_headers(session_nonce),
        )

    @staticmethod
    def _require_idempotency_key(idempotency_key: str) -> str:
        return require_idempotency_key(idempotency_key)

    def _url(self, path: str) -> str:
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.api_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    def _platform_url(self, path: str) -> str:
        """URL for the alerts-platform family mounted at ``platform_prefix``."""
        base = self.config.base_url.rstrip("/")
        prefix = "/" + self.config.platform_prefix.strip("/")
        suffix = "/" + path.strip("/")
        return f"{base}{prefix}{suffix}"

    def _platform_request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        return self._request_url(method, self._platform_url(path), **kwargs)

    # -- alerts platform: capabilities / validate / preview -----------------

    def workflow_capabilities(self) -> JsonDict:
        """Exactly the capabilities the server can evaluate (registry-derived)."""
        return self._platform_request("GET", "/worker/workflows/capabilities")

    def validate_workflow(self, *, yaml_text: Optional[str] = None,
                          document: Optional[JsonDict] = None) -> JsonDict:
        """Validate without persisting. Returns issues rather than raising."""
        return self._platform_request(
            "POST", "/worker/workflows/validate",
            json=_document_payload(yaml_text=yaml_text, document=document),
        )

    def preview_workflow(self, *, yaml_text: Optional[str] = None,
                         document: Optional[JsonDict] = None,
                         observations: Optional[list] = None) -> JsonDict:
        """Dry-run. Writes nothing, sends nothing, schedules nothing."""
        payload = _document_payload(yaml_text=yaml_text, document=document)
        if observations is not None:
            payload["observations"] = observations
        return self._platform_request("POST", "/worker/workflows/preview", json=payload)

    # -- alerts platform: workflow CRUD and lifecycle -----------------------

    def create_workflow(self, *, document: Optional[JsonDict] = None,
                        yaml_text: Optional[str] = None,
                        idempotency_key: Optional[str] = None) -> JsonDict:
        """Create a workflow revision.

        Supply ``idempotency_key`` to make a retry safe: the same key returns
        the original resource instead of creating a duplicate.
        """
        payload = _document_payload(yaml_text=yaml_text, document=document)
        if idempotency_key is not None:
            payload["idempotency_key"] = require_idempotency_key(idempotency_key)
        return self._platform_request("POST", "/worker/workflows", json=payload)

    def import_workflow(self, *, yaml_text: str,
                        idempotency_key: Optional[str] = None) -> JsonDict:
        payload: JsonDict = {"yaml_text": yaml_text}
        if idempotency_key is not None:
            payload["idempotency_key"] = require_idempotency_key(idempotency_key)
        return self._platform_request("POST", "/worker/workflows/import", json=payload)

    def list_workflows(self, *, limit: int = 50, offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", "/worker/workflows", params=_page_params(limit, offset)
        )

    def get_workflow(self, workflow_id: str) -> JsonDict:
        return self._platform_request("GET", f"/worker/workflows/{workflow_id}")

    def update_workflow(self, workflow_id: str, *, document: Optional[JsonDict] = None,
                        yaml_text: Optional[str] = None,
                        expected_revision: Optional[int] = None) -> JsonDict:
        """Update a workflow.

        ``expected_revision`` guards against lost updates: a mismatch fails with
        ``REVISION_CONFLICT`` rather than overwriting a concurrent edit.
        """
        payload = _document_payload(yaml_text=yaml_text, document=document)
        if expected_revision is not None:
            payload["expected_revision"] = int(expected_revision)
        return self._platform_request(
            "PATCH", f"/worker/workflows/{workflow_id}", json=payload
        )

    def activate_workflow(self, workflow_id: str, *, revision: Optional[int] = None) -> JsonDict:
        payload: JsonDict = {}
        if revision is not None:
            payload["revision"] = int(revision)
        return self._platform_request(
            "POST", f"/worker/workflows/{workflow_id}/activate", json=payload
        )

    def pause_workflow(self, workflow_id: str) -> JsonDict:
        return self._platform_request("POST", f"/worker/workflows/{workflow_id}/pause")

    def resume_workflow(self, workflow_id: str) -> JsonDict:
        return self._platform_request("POST", f"/worker/workflows/{workflow_id}/resume")

    def archive_workflow(self, workflow_id: str) -> JsonDict:
        return self._platform_request("POST", f"/worker/workflows/{workflow_id}/archive")

    def workflow_events(self, workflow_id: str, *, limit: int = 50,
                        offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", f"/worker/workflows/{workflow_id}/events",
            params=_page_params(limit, offset),
        )

    def workflow_health(self, workflow_id: str) -> JsonDict:
        return self._platform_request("GET", f"/worker/workflows/{workflow_id}/health")

    def export_workflow(self, workflow_id: str, *, revision: Optional[int] = None) -> JsonDict:
        """Export a revision's canonical document (the round-trippable form)."""
        params = {} if revision is None else {"revision": int(revision)}
        return self._platform_request(
            "GET", f"/worker/workflows/{workflow_id}/export", params=params
        )

    # -- alerts platform: universes ----------------------------------------

    def create_universe(self, *, name: str, kind: str,
                        source_config: Optional[JsonDict] = None) -> JsonDict:
        payload: JsonDict = {"name": name, "kind": kind}
        if source_config is not None:
            payload["source_config"] = source_config
        return self._platform_request("POST", "/worker/universes", json=payload)

    def list_universes(self) -> JsonDict:
        return self._platform_request("GET", "/worker/universes")

    def get_universe(self, name: str) -> JsonDict:
        return self._platform_request("GET", f"/worker/universes/{name}")

    def resolve_universe(self, name: str) -> JsonDict:
        """Resolve and PERSIST a new membership revision."""
        return self._platform_request("POST", f"/worker/universes/{name}/resolve")

    def universe_revisions(self, name: str, *, limit: int = 50) -> JsonDict:
        return self._platform_request(
            "GET", f"/worker/universes/{name}/revisions", params={"limit": int(limit)}
        )

    def preview_universe(self, *, kind: str, source_config: JsonDict) -> JsonDict:
        """Resolve membership in memory; nothing is persisted."""
        return self._platform_request(
            "POST", "/worker/universes/preview",
            json={"kind": kind, "source_config": source_config},
        )

    # -- alerts platform: screeners ----------------------------------------

    def screener_runs(self, workflow_id: str, *, limit: int = 50,
                      offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", f"/worker/screeners/{workflow_id}/runs",
            params=_page_params(limit, offset),
        )

    def screener_run(self, run_id: str, *, limit: int = 50, offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", f"/worker/screeners/runs/{run_id}",
            params=_page_params(limit, offset),
        )

    def run_screener(self, workflow_id: str, *,
                     idempotency_key: Optional[str] = None) -> JsonDict:
        """Trigger a manual run; the same key returns the original run."""
        params = {}
        if idempotency_key is not None:
            params["idempotency_key"] = require_idempotency_key(idempotency_key)
        return self._platform_request(
            "POST", f"/worker/screeners/{workflow_id}/runs", params=params
        )

    def screener_events(self, workflow_id: str, *, limit: int = 50,
                        offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", f"/worker/screeners/{workflow_id}/events",
            params=_page_params(limit, offset),
        )

    def preview_screener(self, *, document: Optional[JsonDict] = None,
                         yaml_text: Optional[str] = None) -> JsonDict:
        """Dry-run over stored data: no run rows, no state, no deliveries."""
        return self._platform_request(
            "POST", "/worker/screeners/preview",
            json=_document_payload(yaml_text=yaml_text, document=document),
        )

    # -- alerts platform: notification channels -----------------------------

    def list_notification_channels(self) -> JsonDict:
        return self._platform_request("GET", "/worker/notification-channels")

    def upsert_notification_channel(self, *, name: str, provider: str,
                                    destination: JsonDict,
                                    secret_env: Optional[str] = None) -> JsonDict:
        payload: JsonDict = {"name": name, "provider": provider,
                             "destination": destination}
        if secret_env is not None:
            payload["secret_env"] = secret_env
        return self._platform_request(
            "POST", "/worker/notification-channels", json=payload
        )

    def test_notification_channel(self, channel_id: str) -> JsonDict:
        """Send a real test message. Requires the notifications:test action."""
        return self._platform_request(
            "POST", f"/worker/notification-channels/{channel_id}/test"
        )

    # -- alerts platform: external signal producers -------------------------

    def list_signal_producers(self) -> JsonDict:
        return self._platform_request("GET", "/worker/signals/producers")

    def create_signal_producer(self, *, name: str,
                               value_schema: Optional[JsonDict] = None,
                               default_ttl_s: int = 3600) -> JsonDict:
        payload: JsonDict = {"name": name, "default_ttl_s": int(default_ttl_s)}
        if value_schema is not None:
            payload["value_schema"] = value_schema
        return self._platform_request("POST", "/worker/signals/producers", json=payload)

    def get_signal_producer(self, name: str) -> JsonDict:
        return self._platform_request("GET", f"/worker/signals/producers/{name}")

    def revoke_signal_producer(self, name: str) -> JsonDict:
        return self._platform_request("POST", f"/worker/signals/producers/{name}/revoke")

    def issue_signal_credential(self, name: str) -> JsonDict:
        """Issue a producer credential.

        The returned ``secret`` is the ONLY time it is ever shown: it is stored
        as a hash, so it cannot be retrieved again, and the SDK never logs it.
        """
        return self._platform_request(
            "POST", f"/worker/signals/producers/{name}/credentials"
        )

    def revoke_signal_credential(self, name: str, token_id: str) -> JsonDict:
        return self._platform_request(
            "POST", f"/worker/signals/producers/{name}/credentials/{token_id}/revoke"
        )

    def submit_signal_value(self, secret: str, *, value: JsonDict, event_time: str,
                            instrument_key: Optional[str] = None,
                            expires_at: Optional[str] = None,
                            idempotency_key: Optional[str] = None) -> JsonDict:
        """Submit a value as a PRODUCER (its own credential, not a worker token).

        Committed server-side before the response, so a successful return means
        the value is stored. Supplying ``idempotency_key`` makes a retry safe.
        """
        payload: JsonDict = {"value": value, "event_time": event_time}
        if instrument_key is not None:
            payload["instrument_key"] = instrument_key
        if expires_at is not None:
            payload["expires_at"] = expires_at
        if idempotency_key is not None:
            payload["idempotency_key"] = require_idempotency_key(idempotency_key)
        return self._request_url(
            "POST", self._platform_url("/worker/signals/values"),
            json=payload, headers={"Authorization": f"Bearer {secret}"},
        )

    def list_signal_values(self, producer: str, *, limit: int = 50,
                           offset: int = 0) -> JsonDict:
        return self._platform_request(
            "GET", "/worker/signals/values",
            params={"producer": producer, **_page_params(limit, offset)},
        )

    def signals_health(self) -> JsonDict:
        return self._platform_request("GET", "/worker/signals/health")

    def _request(self, method: str, path: str, **kwargs: Any) -> JsonDict:
        return self._request_url(method, self._url(path), **kwargs)

    def _request_text(self, method: str, path: str, **kwargs: Any) -> str:
        response = self.session.request(method, self._url(path), timeout=self.config.timeout, **kwargs)
        if 200 <= response.status_code < 300:
            return response.text
        self._raise_response_error(response, method, path)
        raise AssertionError("unreachable")

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
