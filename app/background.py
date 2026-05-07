from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict

from fastapi import FastAPI

from api.repositories.algo_worker_repo import (
    WORKER_RUN_STALE_ACTION_SECONDS,
    WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
)
from api.services.runtime_recovery import build_worker_runtime_recovery_service
from broker_api.orders import bracket_runtime_store, get_bracket_executor_wakeup_event, run_bracket_executor_once
from app.monitor import heartbeat, set_component_status

def _worker_protection_squareoff_schedule() -> dict[str, str]:
    defaults = {
        "NSE:MIS": "15:20",
        "BSE:MIS": "15:20",
        "NFO:MIS": "15:25",
        "CDS:MIS": "16:45",
        "MCX:MIS": "23:20",
    }
    raw = os.getenv("WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON")
    if not raw:
        return defaults
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("schedule must be a JSON object")
        return {**defaults, **{str(key): str(value) for key, value in override.items()}}
    except Exception:
        logging.warning("Invalid WORKER_PROTECTION_SQUAREOFF_SCHEDULE_JSON; using defaults", exc_info=True)
        return defaults

async def _worker_protection_loop(app: FastAPI):
    from types import SimpleNamespace

    from api.repositories.algo_worker_repo import SqlAlchemyAlgoWorkerRepository
    from api.services.protection_runtime import (
        WorkerProtectionRuntime,
        load_worker_run_pnl_for_protection,
        submit_worker_protection_exit,
    )

    interval = max(1.0, float(os.getenv("WORKER_PROTECTION_INTERVAL_SECONDS", "5")))
    request = SimpleNamespace(headers={}, app=app, is_disconnected=lambda: False)
    repo = getattr(app.state, "algo_worker_repository", None)
    if repo is None:
        repo = SqlAlchemyAlgoWorkerRepository()
        app.state.algo_worker_repository = repo
    runtime = WorkerProtectionRuntime(
        repo=repo,
        pnl_loader=lambda run: load_worker_run_pnl_for_protection(request, run),
        exit_submitter=lambda run, state: submit_worker_protection_exit(request, run, state),
        squareoff_schedule=_worker_protection_squareoff_schedule(),
    )
    set_component_status("worker_protection", "healthy", detail="Worker protection runtime started")
    while True:
        try:
            result = await runtime.evaluate_once()
            heartbeat("worker_protection", detail="Evaluated worker backend protection", meta={**result, "interval_seconds": interval})
        except asyncio.CancelledError:
            set_component_status("worker_protection", "stopped", detail="Worker protection runtime cancelled")
            break
        except Exception as exc:
            logging.warning("Worker protection loop failed: %s", exc, exc_info=True)
            set_component_status("worker_protection", "degraded", detail=str(exc))
        await asyncio.sleep(interval)

async def _worker_runtime_recovery_runs_loop(app: FastAPI):
    interval = max(1.0, float(os.getenv("WORKER_RUNTIME_STALE_RECOVERY_INTERVAL_SECONDS", "30")))
    service = build_worker_runtime_recovery_service(
        app,
        stale_action_seconds=WORKER_RUN_STALE_ACTION_SECONDS,
        claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
    )
    set_component_status("worker_runtime_stale_recovery", "healthy", detail="Worker stale recovery runtime started")
    while True:
        try:
            result = await service.recover_stale_runs_once()
            heartbeat(
                "worker_runtime_stale_recovery",
                detail="Recovered stale worker runs",
                meta={**result, "interval_seconds": interval},
            )
        except asyncio.CancelledError:
            set_component_status("worker_runtime_stale_recovery", "stopped", detail="Worker stale recovery runtime cancelled")
            break
        except Exception as exc:
            logging.warning("Worker stale recovery loop failed: %s", exc, exc_info=True)
            set_component_status("worker_runtime_stale_recovery", "degraded", detail=str(exc))
        await asyncio.sleep(interval)

async def _worker_runtime_recovery_exit_loop(app: FastAPI):
    interval = max(1.0, float(os.getenv("WORKER_RUNTIME_EXITING_RECOVERY_INTERVAL_SECONDS", "10")))
    service = build_worker_runtime_recovery_service(
        app,
        stale_action_seconds=WORKER_RUN_STALE_ACTION_SECONDS,
        claimed_without_heartbeat_seconds=WORKER_SESSION_CLAIM_WITHOUT_HEARTBEAT_SECONDS,
    )
    set_component_status("worker_runtime_exiting_recovery", "healthy", detail="Worker exiting recovery runtime started")
    while True:
        try:
            result = await service.recover_exiting_runs_once()
            heartbeat(
                "worker_runtime_exiting_recovery",
                detail="Recovered exiting worker runs",
                meta={**result, "interval_seconds": interval},
            )
        except asyncio.CancelledError:
            set_component_status("worker_runtime_exiting_recovery", "stopped", detail="Worker exiting recovery runtime cancelled")
            break
        except Exception as exc:
            logging.warning("Worker exiting recovery loop failed: %s", exc, exc_info=True)
            set_component_status("worker_runtime_exiting_recovery", "degraded", detail=str(exc))
        await asyncio.sleep(interval)

async def _bracket_executor_loop(app: FastAPI):
    poll_seconds = max(0.5, float(os.getenv("BRACKET_EXECUTOR_POLL_SECONDS", "1.0")))
    claim_limit = max(1, int(os.getenv("BRACKET_EXECUTOR_CLAIM_LIMIT", "10")))
    wake_event = get_bracket_executor_wakeup_event()
    set_component_status("bracket_executor", "healthy", detail="Bracket executor runtime started")

    async def _place_order_fn(*, strategy_run_id: str, account_id: str, bracket_intent_id: str, action_type: str, payload: Dict[str, Any], idempotency_key: str):
        from broker_api.orders import OrdersService, PlaceOrderRequest
        from api.routers.worker_shared import _load_live_kite_for_account

        kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
        orders_service = getattr(app.state, "algo_worker_orders_service", None) or OrdersService()
        order_payload = dict(payload or {})
        order_payload.setdefault("variety", "regular")
        order_payload.setdefault("validity", "DAY")
        order_payload.setdefault("market_protection", -1)
        if action_type == "place_stoploss":
            order_payload.setdefault("order_type", "SL")
        else:
            order_payload.setdefault("order_type", "LIMIT")
        order_payload["attribution"] = {
            "strategy_run_id": strategy_run_id,
            "strategy_family": "indicator_strategy",
            "strategy_name": "worker_bracket",
            "execution_mode": "live",
            "account_ref": account_id,
            "entry_surface": "backend_bracket_executor",
            "source": "backend_bracket_executor",
            "idempotency_key": idempotency_key,
            "bracket_intent_id": bracket_intent_id,
            "metadata": {"bracket_intent_id": bracket_intent_id},
        }
        req = PlaceOrderRequest.model_validate(order_payload)
        result = await orders_service.place_order(
            kite,
            req,
            corr_id=f"bracket-executor-{bracket_intent_id}",
            idempotency_key=idempotency_key,
            session_id=f"backend:bracket:{bracket_intent_id}",
        )
        return {"order_id": str(result.order_id)}

    async def _cancel_order_fn(*, account_id: str, order_id: str):
        from broker_api.orders import OrdersService
        from api.routers.worker_shared import _load_live_kite_for_account

        kite = await asyncio.to_thread(_load_live_kite_for_account, account_id)
        orders_service = getattr(app.state, "algo_worker_orders_service", None) or OrdersService()
        await orders_service.cancel_order(
            kite,
            "regular",
            order_id,
            corr_id=f"bracket-executor-cancel-{order_id}",
        )

    while True:
        try:
            claimed = await run_bracket_executor_once(
                store=bracket_runtime_store,
                place_order_fn=_place_order_fn,
                cancel_order_fn=_cancel_order_fn,
                claim_limit=claim_limit,
            )
            heartbeat(
                "bracket_executor",
                detail="Processed bracket actions",
                meta={"claimed_actions": claimed, "poll_seconds": poll_seconds, "claim_limit": claim_limit},
            )
        except asyncio.CancelledError:
            set_component_status("bracket_executor", "stopped", detail="Bracket executor runtime cancelled")
            break
        except Exception as exc:
            logging.warning("Bracket executor loop failed: %s", exc, exc_info=True)
            set_component_status("bracket_executor", "degraded", detail=str(exc))
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=poll_seconds)
            wake_event.clear()
        except asyncio.TimeoutError:
            pass
