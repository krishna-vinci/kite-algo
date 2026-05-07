from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.control_plane_protection import ControlPlaneProtectionService, InvestingProtectionRepository


HEALTHY_HEARTBEAT_SECONDS = 45
STALE_HEARTBEAT_SECONDS = 180
_CANCEL_DISABLED_REASON = "Strategy-scoped cancel is disabled until a broker-safe open-order lookup is registered"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def optional_isoformat_utc(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return isoformat_utc(value)
    text = str(value).strip()
    return text or None


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def compute_worker_health(last_heartbeat_at: Optional[datetime], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or utcnow()
    if last_heartbeat_at is None:
        return {
            "last_heartbeat_at": None,
            "heartbeat_age_sec": None,
            "health_status": "unknown",
        }
    if last_heartbeat_at.tzinfo is None:
        last_heartbeat_at = last_heartbeat_at.replace(tzinfo=timezone.utc)
    age = max(0, int((current.astimezone(timezone.utc) - last_heartbeat_at.astimezone(timezone.utc)).total_seconds()))
    if age <= HEALTHY_HEARTBEAT_SECONDS:
        status = "healthy"
    elif age <= STALE_HEARTBEAT_SECONDS:
        status = "stale"
    else:
        status = "disconnected"
    return {
        "last_heartbeat_at": isoformat_utc(last_heartbeat_at),
        "heartbeat_age_sec": age,
        "health_status": status,
    }


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


def build_empty_snapshot(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    generated = now or utcnow()
    return {
        "generated_at": isoformat_utc(generated),
        "totals": {
            "strategy_count": 0,
            "open_strategy_count": 0,
            "position_count": 0,
            "stale_worker_count": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        },
        "strategies": [],
        "unattributed": {
            "display_name": "Manual / unattributed broker exposure",
            "positions": [],
            "orders": [],
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        },
    }


async def build_strategy_positions_snapshot(
    request: Any,
    *,
    account_scope: str = "default",
    broker_account_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    generated = now or utcnow()
    strategies = []
    strategies.extend(await _paper_strategy_rows(request, account_scope=account_scope))
    strategies.extend(await _worker_strategy_rows(request, broker_account_id=broker_account_id, now=generated))
    unattributed = await _unattributed_bucket(request, broker_account_id=broker_account_id, live_strategy_rows=strategies)
    return _finalize_snapshot(strategies, unattributed, now=generated)


def _protection_service(request: Any) -> ControlPlaneProtectionService:
    return ControlPlaneProtectionService(
        option_strategy_store=getattr(request.app.state, "option_strategy_store", None),
        algo_runtime_service=getattr(request.app.state, "algo_runtime_service", None),
        investing_repository=getattr(request.app.state, "investing_protection_repository", None) or InvestingProtectionRepository(),
    )


async def _paper_strategy_rows(request: Any, *, account_scope: str) -> list[Dict[str, Any]]:
    service = getattr(request.app.state, "paper_runtime_service", None)
    if service is None:
        return []
    summary = await service.get_strategy_summary(account_scope=account_scope)
    protection_service = _protection_service(request)
    rows = []
    for item in list(summary.get("strategies") or []):
        row = dict(item)
        strategy_run_id = str(row.get("strategy_run_id") or row.get("strategy_id") or "unknown")
        capabilities = dict(row.get("capabilities") or {})
        can_exit = bool(capabilities.get("can_exit_strategy")) and bool(row.get("is_open", False))
        action_reasons = {}
        if not can_exit:
            action_reasons["exit_strategy"] = str(capabilities.get("exit_reason") or "Paper strategy is not exit-enabled")
        adapter_row = {
            "strategy_run_id": strategy_run_id,
            "display_name": str(row.get("display_name") or strategy_run_id),
            "source": "paper_runtime",
            "mode": "paper",
            "status": str(row.get("status") or ("open" if row.get("is_open") else "closed")),
            "metadata": dict(row.get("metadata") or {}),
            "strategy_family": row.get("strategy_family"),
            "algo_instance_id": row.get("algo_instance_id"),
            "protection": row.get("protection"),
        }
        protection = await _safe_protection_for_strategy(protection_service, adapter_row)
        rows.append(
            {
                "strategy_run_id": strategy_run_id,
                "display_name": str(row.get("display_name") or strategy_run_id),
                "source": "paper_runtime",
                "mode": "paper",
                "status": str(row.get("status") or ("open" if row.get("is_open") else "closed")),
                "worker_id": None,
                "worker_name": None,
                "worker_metrics": {},
                "last_heartbeat_at": None,
                "heartbeat_age_sec": None,
                "health_status": "unknown",
                "is_open": bool(row.get("is_open")),
                "realized_pnl": to_float(row.get("realized_pnl")),
                "unrealized_pnl": to_float(row.get("unrealized_pnl")),
                "net_pnl": to_float(row.get("realized_pnl")) + to_float(row.get("unrealized_pnl")),
                "position_count": len(list(row.get("positions") or [])),
                "open_order_count": len(list(row.get("orders") or [])),
                "trade_count": len(list(row.get("trades") or [])),
                "positions": list(row.get("positions") or []),
                "orders": list(row.get("orders") or []),
                "trades": list(row.get("trades") or []),
                "summary_fields": list(row.get("summary_fields") or []),
                "protection": protection,
                "allowed_actions": ["exit_strategy"] if can_exit else [],
                "action_reasons": action_reasons,
                "last_updated_at": optional_isoformat_utc(row.get("last_updated_at") or row.get("last_event_at")),
            }
        )
    return rows


async def _worker_strategy_rows(request: Any, *, broker_account_id: Optional[str], now: datetime) -> list[Dict[str, Any]]:
    repo = getattr(request.app.state, "algo_worker_repository", None)
    if repo is None or not hasattr(repo, "list_runs_for_control_plane"):
        return []
    protection_service = _protection_service(request)
    rows = []
    for run in await repo.list_runs_for_control_plane():
        mode = str(run.get("execution_mode") or "paper").lower()
        if mode == "paper":
            continue
        if broker_account_id and str(run.get("account_scope") or "") != str(broker_account_id):
            continue
        run_heartbeat = run.get("last_heartbeat_at")
        token_heartbeat = run.get("token_last_heartbeat_at")
        health = compute_worker_health(run_heartbeat or token_heartbeat, now=now)
        metadata = dict(run.get("metadata") or {})
        runtime_state = dict(run.get("runtime_state") or {})
        recovery_state = dict(runtime_state.get("runtime_recovery") or {})
        heartbeat_json = dict(run.get("heartbeat_json") or {})
        pnl = await _worker_pnl_or_empty(request, run)
        totals = dict(pnl.get("totals") or {})
        allowed_actions = []
        action_reasons = {}
        is_open = str(run.get("status") or "open") not in {"closed", "failed"}
        if is_open and "exit_strategy" in list(run.get("allowed_actions") or []):
            allowed_actions.append("exit_strategy")
        else:
            action_reasons["exit_strategy"] = "Worker run is closed or does not allow strategy exit"
        action_reasons["cancel_orders"] = _CANCEL_DISABLED_REASON
        adapter_row = {
            "strategy_run_id": str(run.get("strategy_run_id")),
            "display_name": str(metadata.get("strategy_name") or run.get("template_id") or run.get("strategy_run_id")),
            "source": "algo_worker",
            "mode": mode,
            "status": str(run.get("status") or "open"),
            "metadata": metadata,
            "strategy_family": metadata.get("strategy_family"),
            "algo_instance_id": metadata.get("algo_instance_id") or run.get("algo_instance_id"),
            "protection": runtime_state.get("protection") if isinstance(runtime_state.get("protection"), dict) else metadata.get("protection"),
            "backend_protection": runtime_state.get("backend_protection") if isinstance(runtime_state.get("backend_protection"), dict) else None,
            "backend_protection_state": runtime_state.get("backend_protection_state") if isinstance(runtime_state.get("backend_protection_state"), dict) else None,
        }
        protection = await _safe_protection_for_strategy(protection_service, adapter_row)
        rows.append(
            {
                "strategy_run_id": str(run.get("strategy_run_id")),
                "display_name": str(metadata.get("strategy_name") or run.get("template_id") or run.get("strategy_run_id")),
                "source": "algo_worker",
                "mode": mode,
                "status": str(run.get("status") or "open"),
                "worker_id": heartbeat_json.get("worker_id"),
                "worker_name": run.get("worker_name"),
                "worker_metrics": dict(heartbeat_json.get("metrics") or {}),
                **health,
                "session_status": _session_status_for_run(run, health),
                "recovery_status": recovery_state.get("recovery_status"),
                "recovery_action_required": bool(recovery_state.get("action_required")),
                "is_open": is_open,
                "realized_pnl": to_float(totals.get("realized_pnl")),
                "unrealized_pnl": to_float(totals.get("unrealized_pnl")),
                "net_pnl": to_float(totals.get("net_pnl")),
                "position_count": len(list(pnl.get("legs") or [])),
                "open_order_count": len(list(runtime_state.get("open_orders") or [])),
                "trade_count": len(list(runtime_state.get("recent_trades") or [])),
                "positions": list(pnl.get("legs") or []),
                "orders": list(runtime_state.get("open_orders") or []),
                "trades": list(runtime_state.get("recent_trades") or []),
                "summary_fields": list(run.get("summary_fields") or []),
                "protection": protection,
                "allowed_actions": allowed_actions,
                "action_reasons": action_reasons,
                "last_updated_at": optional_isoformat_utc(pnl.get("updated_at") or run.get("updated_at") or run.get("created_at")),
            }
        )
    return rows


async def _worker_pnl_or_empty(request: Any, run: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from api.routers.worker_protection import _build_worker_run_pnl_snapshot

        return await _build_worker_run_pnl_snapshot(request, run)
    except Exception:
        return {
            "totals": {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "net_pnl": 0.0},
            "legs": [],
            "updated_at": optional_isoformat_utc(run.get("updated_at") or run.get("created_at")),
        }


def _serialize_position(position: Any) -> Dict[str, Any]:
    if hasattr(position, "model_dump"):
        return dict(position.model_dump())
    if isinstance(position, dict):
        return dict(position)
    if hasattr(position, "__dict__"):
        return dict(position.__dict__)
    return {}


async def _safe_protection_for_strategy(service: ControlPlaneProtectionService, strategy: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return await service.for_strategy(strategy)
    except Exception as exc:
        return {
            "source": "none",
            "status": "error",
            "summary": "Protection adapter failed; strategy exposure snapshot is still available",
            "last_checked_at": None,
            "details": {"error": str(exc), "strategy_run_id": strategy.get("strategy_run_id")},
        }


def _position_identity(position: Dict[str, Any]) -> Optional[tuple[str, Any, str]]:
    product = str(position.get("product") or "").strip().upper()
    instrument_token = to_int(position.get("instrument_token"), default=0)
    if instrument_token:
        return ("token", instrument_token, product)
    exchange = str(position.get("exchange") or "").strip().upper()
    tradingsymbol = str(position.get("tradingsymbol") or "").strip().upper()
    if exchange and tradingsymbol:
        return ("symbol", f"{exchange}:{tradingsymbol}", product)
    return None


def _position_quantity(position: Dict[str, Any]) -> int:
    for key in ("net_quantity", "quantity", "broker_net_quantity"):
        if key in position:
            return to_int(position.get(key), default=0)
    return 0


def _live_strategy_position_quantities(rows: list[Dict[str, Any]]) -> Dict[tuple[str, Any, str], int]:
    quantities: Dict[tuple[str, Any, str], int] = {}
    for row in rows:
        if str(row.get("mode") or "").lower() != "live":
            continue
        for position in list(row.get("positions") or []):
            serialized = _serialize_position(position)
            identity = _position_identity(serialized)
            if identity is not None:
                quantities[identity] = quantities.get(identity, 0) + _position_quantity(serialized)
    return quantities


def _subtract_attributed_position(position: Dict[str, Any], attributed_quantity: int) -> Optional[Dict[str, Any]]:
    broker_quantity = _position_quantity(position)
    if broker_quantity == 0:
        return None
    residual_quantity = broker_quantity - attributed_quantity
    if residual_quantity == 0:
        return None
    residual = dict(position)
    if "quantity" in residual:
        residual["quantity"] = residual_quantity
    else:
        residual["net_quantity"] = residual_quantity
    ratio = min(1.0, abs(float(residual_quantity)) / max(1.0, abs(float(broker_quantity))))
    for key in ("pnl", "realized_pnl", "unrealized_pnl"):
        if key in residual:
            residual[key] = to_float(residual.get(key)) * ratio
    residual["attributed_quantity_removed"] = attributed_quantity
    return residual


async def _unattributed_bucket(
    request: Any,
    *,
    broker_account_id: Optional[str],
    live_strategy_rows: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    service = getattr(request.app.state, "realtime_positions_service", None)
    if service is None or not broker_account_id:
        positions = []
    else:
        raw_positions = await service.get_positions(broker_account_id, "control_plane_snapshot")
        attributed_quantities = _live_strategy_position_quantities(list(live_strategy_rows or []))
        positions = []
        for position in raw_positions.values():
            serialized = _serialize_position(position)
            identity = _position_identity(serialized)
            if identity is not None and identity in attributed_quantities:
                residual = _subtract_attributed_position(serialized, attributed_quantities[identity])
                if residual is not None:
                    positions.append(residual)
                continue
            positions.append(serialized)
    realized = sum(to_float(item.get("realized_pnl")) for item in positions)
    unrealized = sum(to_float(item.get("unrealized_pnl")) for item in positions)
    return {
        "display_name": "Manual / unattributed broker exposure",
        "positions": positions,
        "orders": [],
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "net_pnl": realized + unrealized,
    }


def _finalize_snapshot(strategies: list[Dict[str, Any]], unattributed: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    realized_total = sum(to_float(item.get("realized_pnl")) for item in strategies) + to_float(unattributed.get("realized_pnl"))
    unrealized_total = sum(to_float(item.get("unrealized_pnl")) for item in strategies) + to_float(unattributed.get("unrealized_pnl"))
    return {
        "generated_at": isoformat_utc(now),
        "totals": {
            "strategy_count": len(strategies),
            "open_strategy_count": sum(1 for item in strategies if item.get("is_open")),
            "position_count": sum(to_int(item.get("position_count")) for item in strategies) + len(list(unattributed.get("positions") or [])),
            "stale_worker_count": sum(1 for item in strategies if item.get("health_status") in {"stale", "disconnected"}),
            "realized_pnl": realized_total,
            "unrealized_pnl": unrealized_total,
            "net_pnl": realized_total + unrealized_total,
        },
        "strategies": strategies,
        "unattributed": unattributed,
    }


async def exit_control_strategy(
    request: Any,
    strategy_run_id: str,
    *,
    account_scope: str = "default",
    reason: Optional[str] = None,
    dry_run: bool = False,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    repo = getattr(request.app.state, "algo_worker_repository", None)
    run = await repo.get_run(strategy_run_id) if repo is not None and hasattr(repo, "get_run") else None
    if run is not None:
        return await _exit_worker_control_strategy(request, run, reason=reason, dry_run=dry_run, idempotency_key=idempotency_key)

    paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
    if paper_runtime_service is None:
        raise HTTPException(status_code=503, detail="Paper runtime is not available")
    if not await _paper_strategy_exists(paper_runtime_service, account_scope=account_scope, strategy_run_id=strategy_run_id):
        raise HTTPException(status_code=404, detail="Strategy run not found in worker or paper runtime")
    if dry_run:
        return {
            "mode": "paper",
            "status": "dry_run",
            "strategy_id": strategy_run_id,
            "message": "Paper exit dry run accepted by control plane",
        }
    result = await paper_runtime_service.exit_strategy(account_scope=account_scope, strategy_id=strategy_run_id)
    return {
        "mode": "paper",
        "status": str(result.get("status") or "closed"),
        "result": result,
    }


async def _paper_strategy_exists(service: Any, *, account_scope: str, strategy_run_id: str) -> bool:
    if not hasattr(service, "get_strategy_summary"):
        return True
    summary = await service.get_strategy_summary(account_scope=account_scope)
    for strategy in list(summary.get("strategies") or []):
        if str(strategy.get("strategy_run_id") or strategy.get("strategy_id") or "") == str(strategy_run_id):
            return True
    return False


async def _exit_worker_control_strategy(request: Any, run: Dict[str, Any], *, reason: Optional[str], dry_run: bool, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    from api.schemas.worker import WorkerExitRequest
    from api.repositories.algo_worker_repo import WorkerToken
    from api.routers.worker_execution import _exit_live_worker_run

    mode = str(run.get("execution_mode") or "paper").lower()
    strategy_run_id = str(run.get("strategy_run_id"))
    payload = WorkerExitRequest(reason=reason, dry_run=dry_run, idempotency_key=idempotency_key)
    repo = getattr(request.app.state, "algo_worker_repository", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Algo worker repository is not available")
    if mode == "dry_run":
        updated = await repo.update_run_status(strategy_run_id, "closed", state_patch={"exit_reason": reason or "control_plane_dry_run_exit"})
        return {"mode": "dry_run", "status": "closed", "run": updated}
    if mode == "paper":
        paper_runtime_service = getattr(request.app.state, "paper_runtime_service", None)
        if paper_runtime_service is None:
            raise HTTPException(status_code=503, detail="Paper runtime is not available")
        if dry_run:
            return {"mode": "paper", "status": "dry_run", "strategy_id": strategy_run_id}
        result = await paper_runtime_service.exit_strategy(account_scope=str(run["account_scope"]), strategy_id=strategy_run_id)
        updated = await repo.update_run_status(strategy_run_id, "closed", state_patch={"exit_result": result, "exit_reason": reason})
        return {"mode": "paper", "status": "closed", "result": result, "run": updated}
    if mode == "live":
        token = WorkerToken(
            token_id=str(run.get("token_id") or "control-plane"),
            name=str(run.get("worker_name") or "control-plane"),
            account_scope=run.get("account_scope"),
            allowed_modes=["live"],
            allowed_actions=["runs:exit"],
            allowed_templates=[],
        )
        return await _exit_live_worker_run(request=request, token=token, run=run, payload=payload)
    raise HTTPException(status_code=400, detail=f"Unsupported strategy execution mode '{mode}'")


async def cancel_control_strategy_orders(request: Any, strategy_run_id: str, *, reason: Optional[str] = None) -> Dict[str, Any]:
    _ = (request, strategy_run_id, reason)
    raise HTTPException(status_code=409, detail=_CANCEL_DISABLED_REASON)
