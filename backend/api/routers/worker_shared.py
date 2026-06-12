from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, WebSocket
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository, WorkerToken, WORKER_RUN_STALE_ACTION_SECONDS, WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS
from backend.api.schemas.worker import WorkerIntentRequest, _parse_csv_int_values, _parse_csv_values
from backend.api.services.market_data import WorkerMarketDataService
from backend.api.services.safety import build_safety_fingerprint, build_signed_safety_token, option_run_status_blocks_trading, verify_signed_safety_token
from backend.algo_runtime.account_scope import parse_account_scope
from backend.app.auth import require_app_user
from backend.app.database import SessionLocal
from backend.journaling.service import JournalService
from backend.shared.serialization import _json_default, _json_dumps, _json_loads, _row_mapping, _to_float, _to_int, _utcnow, _hash_token, _query_int_param

logger = logging.getLogger(__name__)

__all__ = [
    # Public non-underscored symbols
    "ALLOWED_V1_MODES",
    "DEFAULT_WORKER_ACTIONS",
    "LIVE_REQUIRED_RUN_METADATA",
    "_OPTION_PROTECTION_STATE_UNAVAILABLE",
    "parse_account_scope",
    "VALID_WORKER_STRATEGY_FAMILIES",
    "WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS",
    "WORKER_SESSION_FRESHNESS_SECONDS",
    "require_active_worker_run_session",
    "require_worker_token",
    "require_worker_ws_token",
    # Underscored symbols re-used by other worker routers
    "_assert_run_access",
    "_broker_user_id_from_account_scope",
    "_enrich_run_health_fields",
    "_extract_bearer_token",
    "_extract_ws_token",
    "_journal_service",
    "_live_broker_positions_for_attribution",
    "_load_live_kite_for_account",
    "_load_live_kite_for_worker_account_scope",
    "_market_data_service",
    "_normalize_worker_gtt_error",
    "_parse_csv_int_values",
    "_parse_csv_values",
    "_payload_matches_live_attribution",
    "_payload_matches_strategy_run",
    "_payload_matches_worker_run",
    "_query_int_param",
    "_refresh_live_account_state",
    "_repo",
    "_require_action",
    "_require_live_run",
    "_require_v1_mode",
    "_require_worker_gtt_action",
    "_require_worker_live_account_scope",
    "_serialize_model",
    "_session_status_for_run",
    "_token_allows_account_scope",
    "_to_float",
    "_to_int",
    "_utcnow",
    "_json_default",
    "_validate_decision_related_ref",
    "_validate_live_run_contract",
    "_worker_request_correlation_id",
    "_worker_run_live_attribution_refs",
    "_worker_session_nonce_from_request",
]

DEFAULT_WORKER_ACTIONS = {
    "gtt:read",
    "gtt:write",
    "runs:create",
    "runs:read",
    "runs:log",
    "intents:submit",
    "risk:update",
    "runs:exit",
    "heartbeat",
    "market:read",
    "market:stream",
    "funds:read",
}
_OPTION_PROTECTION_STATE_UNAVAILABLE = "__options_protection_state_unavailable__"
ALLOWED_V1_MODES = {"paper", "dry_run", "live"}
LIVE_REQUIRED_RUN_METADATA = {"strategy_family", "strategy_name"}
VALID_WORKER_STRATEGY_FAMILIES = {
    "options_strategy",
    "indicator_strategy",
    "investment_strategy",
    "discretionary_strategy",
}
WORKER_SESSION_FRESHNESS_SECONDS = int(os.getenv("WORKER_SESSION_FRESHNESS_SECONDS", "60"))

def _repo(request: Any) -> Any:
    repository = getattr(request.app.state, "algo_worker_repository", None)
    if repository is None:
        repository = SqlAlchemyAlgoWorkerRepository()
        request.app.state.algo_worker_repository = repository
    return repository

def _market_data_service(request: Any) -> WorkerMarketDataService:
    service = getattr(request.app.state, "worker_market_data_service", None)
    if service is not None:
        return service
    candle_reader = getattr(request.app.state, "worker_candle_data_reader", None)
    if candle_reader is None:
        candle_reader = getattr(request.app.state, "algo_worker_candle_reader", None)
    if candle_reader is None:
        try:
            from backend.algo_runtime.snapshot_builder import RedisCandleDataReader
            from backend.broker_api.market.candle_aggregator import INTERVAL_SECONDS
            from backend.broker_api.market.candle_storage import CandleStorage
            from backend.broker_api.core.redis_events import get_redis

            candle_reader = RedisCandleDataReader(
                redis_client=get_redis(),
                candle_storage=CandleStorage,
                interval_seconds=INTERVAL_SECONDS,
            )
            request.app.state.algo_worker_candle_reader = candle_reader
        except Exception:
            candle_reader = None
    return WorkerMarketDataService(
        market_data_runtime=getattr(request.app.state, "market_data_runtime", None),
        redis=getattr(getattr(request.app.state, "market_data_runtime", None), "redis", None),
        candle_reader=candle_reader,
    )

def _journal_service(request: Any) -> JournalService:
    service = getattr(request.app.state, "journal_service", None)
    if service is None:
        service = JournalService()
        request.app.state.journal_service = service
    return service

def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Worker bearer token required")
    return token.strip()

def _extract_ws_token(websocket: WebSocket) -> str:
    token = str(websocket.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Worker websocket token required")
    return token

def _worker_session_nonce_from_request(request: Request) -> str:
    return str(request.headers.get("X-Worker-Session-Nonce") or "").strip()

async def require_active_worker_run_session(request: Request, run: Dict[str, Any]) -> str:
    active_nonce = str(run.get("worker_session_nonce") or "").strip()
    if not active_nonce:
        return ""
    nonce = _worker_session_nonce_from_request(request)
    if not nonce:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_REQUIRED",
                "strategy_run_id": str(run.get("strategy_run_id") or ""),
            },
        )
    if nonce != active_nonce:
        raise HTTPException(
            status_code=409,
            detail={
                "rejection_reason": "WORKER_SESSION_CONFLICT",
                "strategy_run_id": str(run.get("strategy_run_id") or ""),
            },
        )
    return nonce

def _session_status_for_run(run: Dict[str, Any], health: Dict[str, Any]) -> str:
    nonce = str(run.get("worker_session_nonce") or "").strip()
    if not nonce:
        return "missing"
    health_status = str(health.get("health_status") or "unknown")
    if health_status == "healthy":
        return "claimed"
    if health_status in {"stale", "disconnected"}:
        return "stale"
    return "takeover_required"

def _enrich_run_health_fields(run: Dict[str, Any]) -> Dict[str, Any]:
    from backend.api.services.control_plane import compute_worker_health

    payload = dict(run)
    now = _utcnow()
    health = compute_worker_health(payload.get("last_heartbeat_at"), now=now)
    runtime_state = dict(payload.get("runtime_state") or {})
    recovery_state = dict(runtime_state.get("runtime_recovery") or {})
    payload["heartbeat_age_sec"] = health.get("heartbeat_age_sec")
    payload["health_status"] = health.get("health_status")
    payload["session_status"] = _session_status_for_run(payload, health)
    payload["recovery_status"] = recovery_state.get("recovery_status")
    payload["recovery_action_required"] = bool(recovery_state.get("action_required"))
    return payload

async def require_worker_token(request: Request) -> WorkerToken:
    raw_token = _extract_bearer_token(request)
    repository = _repo(request)
    token = await repository.get_token_by_hash(_hash_token(raw_token))
    if token is None or token.status != "active":
        raise HTTPException(status_code=401, detail="Invalid worker token")
    if token.expires_at and token.expires_at <= _utcnow():
        raise HTTPException(status_code=401, detail="Worker token expired")
    await repository.touch_token(token.token_id)
    return token

async def require_worker_ws_token(websocket: WebSocket) -> WorkerToken:
    raw_token = _extract_ws_token(websocket)
    repository = _repo(websocket)
    token = await repository.get_token_by_hash(_hash_token(raw_token))
    if token is None or token.status != "active":
        raise HTTPException(status_code=401, detail="Invalid worker token")
    if token.expires_at and token.expires_at <= _utcnow():
        raise HTTPException(status_code=401, detail="Worker token expired")
    await repository.touch_token(token.token_id)
    return token

def _serialize_model(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)

def _payload_matches_strategy_run(payload: Dict[str, Any], strategy_run_id: str) -> bool:
    if str(payload.get("strategy_run_id") or "") == strategy_run_id:
        return True
    attribution = payload.get("attribution")
    if isinstance(attribution, dict) and str(attribution.get("strategy_run_id") or "") == strategy_run_id:
        return True
    meta = payload.get("meta")
    if isinstance(meta, dict) and str(meta.get("strategy_run_id") or "") == strategy_run_id:
        return True
    return False

def _payload_matches_live_attribution(payload: Dict[str, Any], refs: Dict[str, List[str]]) -> bool:
    broker_order_ids = {str(value).strip() for value in refs.get("broker_order_ids") or [] if str(value).strip()}
    client_order_refs = {str(value).strip() for value in refs.get("client_order_refs") or [] if str(value).strip()}

    order_id_candidates = {
        str(payload.get("order_id") or "").strip(),
        str(payload.get("broker_order_id") or "").strip(),
    }
    if broker_order_ids.intersection({value for value in order_id_candidates if value}):
        return True

    tag_candidates: set[str] = set()
    tag_value = payload.get("tag")
    if tag_value is not None:
        cleaned = str(tag_value).strip()
        if cleaned:
            tag_candidates.add(cleaned)
    tags_value = payload.get("tags")
    if isinstance(tags_value, list):
        tag_candidates.update(str(item).strip() for item in tags_value if str(item).strip())
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("tag", "client_order_ref"):
            cleaned = str(meta.get(key) or "").strip()
            if cleaned:
                tag_candidates.add(cleaned)
    attribution = payload.get("attribution")
    if isinstance(attribution, dict):
        cleaned = str(attribution.get("client_order_ref") or "").strip()
        if cleaned:
            tag_candidates.add(cleaned)
    if client_order_refs.intersection(tag_candidates):
        return True

    return False

async def _worker_run_live_attribution_refs(request: Request, run: Dict[str, Any]) -> Dict[str, List[str]]:
    return await _repo(request).get_live_order_attribution_refs(
        strategy_run_id=str(run["strategy_run_id"]),
        account_id=str(run["account_scope"]),
    )

def _payload_matches_worker_run(payload: Dict[str, Any], strategy_run_id: str, refs: Optional[Dict[str, List[str]]] = None) -> bool:
    if _payload_matches_strategy_run(payload, strategy_run_id):
        return True
    if refs and _payload_matches_live_attribution(payload, refs):
        return True
    return False

def _require_action(token: WorkerToken, action: str) -> None:
    if action not in set(token.allowed_actions):
        raise HTTPException(status_code=403, detail=f"Worker token is not allowed to perform '{action}'")

def _require_worker_gtt_action(token: WorkerToken, action: str) -> None:
    allowed = set(token.allowed_actions)
    accepted = {
        "gtt:read": {"gtt:read", "runs:read"},
        "gtt:write": {"gtt:write", "intents:submit"},
    }.get(action, {action})
    if allowed.intersection(accepted):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "rejection_reason": "WORKER_ACTION_NOT_ALLOWED",
            "required_action": action,
        },
    )

def _require_v1_mode(mode: str) -> None:
    normalized = str(mode or "").strip().lower()
    if normalized not in ALLOWED_V1_MODES:
        raise HTTPException(status_code=403, detail="Algo worker API allows only paper, dry_run, and explicitly enabled live execution modes")

def _broker_user_id_from_account_scope(account_scope: str) -> str:
    try:
        parsed = parse_account_scope(account_scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Live worker execution requires a kite:<broker_user_id> account_scope") from exc
    if parsed.mode != "live" or not parsed.broker_user_id:
        raise HTTPException(status_code=400, detail="Live worker execution requires a real broker account_scope, not a paper account scope")
    return parsed.broker_user_id

def _require_worker_live_account_scope(token: WorkerToken) -> str:
    account_scope = str(token.account_scope or "").strip()
    if not account_scope:
        raise HTTPException(status_code=403, detail={"rejection_reason": "WORKER_ACCOUNT_SCOPE_REQUIRED"})
    try:
        parsed = parse_account_scope(account_scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "rejection_reason": "WORKER_ACCOUNT_SCOPE_UNSUPPORTED",
                "account_scope": account_scope,
            },
        ) from exc
    if parsed.mode != "live" or not parsed.broker_user_id:
        raise HTTPException(
            status_code=400,
            detail={
                "rejection_reason": "WORKER_ACCOUNT_SCOPE_UNSUPPORTED",
                "account_scope": account_scope,
            },
        )
    return account_scope

async def _load_live_kite_for_worker_account_scope(account_scope: str):
    try:
        return await asyncio.to_thread(_load_live_kite_for_account, account_scope)
    except HTTPException as exc:
        if exc.status_code == 503:
            raise HTTPException(
                status_code=503,
                detail={
                    "rejection_reason": "WORKER_KITE_SESSION_UNAVAILABLE",
                    "provider_detail": exc.detail,
                },
            ) from exc
        raise

def _worker_request_correlation_id(request: Request, prefix: str) -> str:
    return request.headers.get("X-Correlation-ID") or request.headers.get("x-correlation-id") or f"{prefix}-{uuid.uuid4()}"

def _normalize_worker_gtt_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict) and exc.detail.get("rejection_reason"):
            return exc
        provider_status = int(exc.status_code)
        if provider_status == 404:
            reason = "GTT_TRIGGER_NOT_FOUND"
        elif provider_status == 400:
            reason = "GTT_REQUEST_INVALID"
        elif provider_status == 409:
            reason = "GTT_PROVIDER_REJECTED"
        elif provider_status in {502, 503, 504}:
            reason = "GTT_PROVIDER_UNAVAILABLE"
        else:
            reason = "GTT_PROVIDER_ERROR"
        return HTTPException(
            status_code=provider_status,
            detail={
                "rejection_reason": reason,
                "provider_status_code": provider_status,
                "provider_detail": exc.detail,
            },
        )
    return HTTPException(
        status_code=502,
        detail={
            "rejection_reason": "GTT_PROVIDER_ERROR",
            "provider_detail": str(exc),
        },
    )

def _validate_live_run_contract(*, account_scope: str, metadata: Dict[str, Any]) -> None:
    _broker_user_id_from_account_scope(account_scope)
    missing = sorted(key for key in LIVE_REQUIRED_RUN_METADATA if not str(metadata.get(key) or "").strip())
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Live worker runs require metadata fields: {', '.join(missing)}",
        )
    strategy_family = str(metadata.get("strategy_family") or "").strip()
    if strategy_family not in VALID_WORKER_STRATEGY_FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"Live worker metadata.strategy_family must be one of: {', '.join(sorted(VALID_WORKER_STRATEGY_FAMILIES))}",
        )

def _load_live_kite_for_account(account_scope: str):
    broker_user_id = _broker_user_id_from_account_scope(account_scope)
    from backend.broker_api.session.kite_session import build_kite_client

    db = SessionLocal()
    try:
        row = db.execute(
            text(
                """
                SELECT session_id, access_token
                FROM public.kite_sessions
                WHERE broker_user_id = :broker_user_id
                  AND access_token IS NOT NULL
                ORDER BY CASE WHEN session_id = 'system' THEN 0 ELSE 1 END, created_at DESC
                LIMIT 1
                """
            ),
            {"broker_user_id": broker_user_id},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=503, detail="No live Kite broker session is available for this worker account_scope")
        return build_kite_client(str(row[1]), session_id=str(row[0]))
    finally:
        db.close()

def _assert_run_access(token: WorkerToken, run: Dict[str, Any]) -> None:
    if str(run.get("token_id") or "") != token.token_id:
        raise HTTPException(status_code=403, detail="Worker token cannot access this run")
    if not _token_allows_account_scope(token, str(run.get("account_scope") or "")):
        raise HTTPException(status_code=403, detail="Worker token cannot access this account scope")
    if token.allowed_templates and run.get("template_id") not in token.allowed_templates:
        raise HTTPException(status_code=403, detail="Worker token cannot access this strategy template")

def _token_allows_account_scope(token: WorkerToken, account_scope: str) -> bool:
    token_scope = str(token.account_scope or "").strip()
    if not token_scope:
        return True
    requested_scope = str(account_scope or "").strip()
    if not requested_scope:
        return False
    try:
        token_parsed = parse_account_scope(token_scope)
        requested_parsed = parse_account_scope(requested_scope)
    except ValueError:
        return token_scope == requested_scope

    if token_parsed.mode == "live":
        if requested_parsed.mode == "paper":
            return True
        return requested_parsed.normalized == token_parsed.normalized
    return requested_parsed.normalized == token_parsed.normalized

def _require_live_run(run: Dict[str, Any], *, feature: str) -> None:
    if str(run.get("execution_mode") or "").lower() != "live":
        raise HTTPException(status_code=409, detail=f"{feature} is only supported for live runs")

async def _live_broker_positions_for_attribution(
    request: Request,
    *,
    kite: Any,
    corr_id: str,
    refs: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    from backend.broker_api.orders import OrdersService

    if not (refs.get("broker_order_ids") or refs.get("client_order_refs")):
        return []

    orders_service = getattr(request.app.state, "algo_worker_orders_service", None) or OrdersService()
    broker_orders = await asyncio.to_thread(orders_service.orders, kite, corr_id)
    matched_orders = [
        _serialize_model(order)
        for order in broker_orders
        if _payload_matches_live_attribution(_serialize_model(order), refs)
    ]
    if not matched_orders:
        return []

    positions_payload = await asyncio.to_thread(kite.positions)
    net_positions = positions_payload.get("net", []) if isinstance(positions_payload, dict) else []
    positions_by_key: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
    for position in net_positions:
        instrument_token = _to_int(position.get("instrument_token"))
        product = str(position.get("product") or "")
        exchange = str(position.get("exchange") or "")
        tradingsymbol = str(position.get("tradingsymbol") or "")
        if not instrument_token or not product or not exchange or not tradingsymbol:
            continue
        positions_by_key[(instrument_token, product, exchange, tradingsymbol)] = dict(position)

    exposure: List[Dict[str, Any]] = []
    seen: set[Tuple[int, str, str, str]] = set()
    for order in matched_orders:
        key = (
            _to_int(order.get("instrument_token")),
            str(order.get("product") or ""),
            str(order.get("exchange") or ""),
            str(order.get("tradingsymbol") or ""),
        )
        if key in seen or not all(key):
            continue
        seen.add(key)
        position = positions_by_key.get(key)
        if not position:
            continue
        if _to_int(position.get("quantity") or position.get("net_quantity")) == 0:
            continue
        exposure.append(
            {
                "account_id": position.get("account_id") or position.get("account_ref"),
                "instrument_token": key[0],
                "product": key[1],
                "exchange": key[2],
                "tradingsymbol": key[3],
                "net_quantity": _to_int(position.get("quantity") or position.get("net_quantity")),
                "average_price": _to_float(position.get("average_price")),
                "last_price": _to_float(position.get("last_price")),
            }
        )
    return exposure

async def _refresh_live_account_state(*, kite: Any, account_id: str, corr_id: str) -> Dict[str, Any]:
    from backend.broker_api.orders import order_event_runtime, realtime_positions_service

    refresh_result: Dict[str, Any] = {"account_id": account_id}
    try:
        refresh_result["reconciled_positions"] = await realtime_positions_service.reconcile_account_positions(kite, account_id, corr_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to reconcile live broker positions before exit: {exc}") from exc

    try:
        refresh_result["synced_dirty_orders"] = await order_event_runtime.sync_dirty_orders(kite, realtime_positions_service, batch_size=25)
    except Exception as exc:
        refresh_result["sync_warning"] = str(exc)
    return refresh_result

def _validate_decision_related_ref(
    *,
    db: Session,
    strategy_run_id: str,
    account_id: str,
    related_resource_type: str,
    related_resource_id: str,
) -> None:
    if related_resource_type == "basket_execution":
        row = db.execute(
            text(
                """
                SELECT 1
                FROM public.basket_executions
                WHERE strategy_run_id = :strategy_run_id
                  AND basket_execution_id = :resource_id
                LIMIT 1
                """
            ),
            {"strategy_run_id": strategy_run_id, "resource_id": related_resource_id},
        ).fetchone()
        if row:
            return
    elif related_resource_type == "bracket_intent":
        row = db.execute(
            text(
                """
                SELECT 1
                FROM public.bracket_intents
                WHERE strategy_run_id = :strategy_run_id
                  AND bracket_intent_id = :resource_id
                LIMIT 1
                """
            ),
            {"strategy_run_id": strategy_run_id, "resource_id": related_resource_id},
        ).fetchone()
        if row:
            return
    elif related_resource_type == "worker_live_execution_link":
        row = db.execute(
            text(
                """
                SELECT 1
                FROM public.worker_live_execution_links
                WHERE strategy_run_id = :strategy_run_id
                  AND account_id = :account_id
                  AND (
                    broker_order_id = :resource_id
                    OR COALESCE(trade_id, '') = :resource_id
                    OR COALESCE(client_order_ref, '') = :resource_id
                  )
                LIMIT 1
                """
            ),
            {
                "strategy_run_id": strategy_run_id,
                "account_id": account_id,
                "resource_id": related_resource_id,
            },
        ).fetchone()
        if row:
            return

    raise HTTPException(
        status_code=422,
        detail={
            "rejection_reason": "UNKNOWN_RELATED_REF",
            "related_resource_type": related_resource_type,
            "related_resource_id": related_resource_id,
        },
    )
