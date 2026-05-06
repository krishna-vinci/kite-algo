from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import text

from database import SessionLocal
from execution_accounting.contracts import OrderAttribution


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def make_client_order_ref(strategy_run_id: str) -> str:
    digest = hashlib.sha1(f"{strategy_run_id}:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:8].upper()
    return f"KA{digest}"


def validate_live_order_attribution(payload: Dict[str, Any]) -> OrderAttribution:
    try:
        data = dict(payload.get("attribution") or payload)
        if not data.get("client_order_ref"):
            data["client_order_ref"] = make_client_order_ref(str(data.get("strategy_run_id") or ""))
        if not data.get("journal_run_id"):
            data["journal_run_id"] = None
        return OrderAttribution(**data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Live orders require strategy attribution: {exc}") from exc


def _intent_params(
    *,
    intent_id: str,
    attribution: OrderAttribution,
    cost_contract: Dict[str, Any],
    idempotency_key: Optional[str],
    basket_execution_id: Optional[str] = None,
    basket_leg_index: Optional[int] = None,
) -> Dict[str, Any]:
    attribution_json = attribution.model_dump(mode="json")
    return {
        "intent_id": intent_id,
        "client_order_ref": attribution.client_order_ref,
        "account_id": attribution.account_ref,
        "strategy_run_id": attribution.strategy_run_id,
        "journal_run_id": attribution.journal_run_id,
        "strategy_family": attribution.strategy_family,
        "strategy_name": attribution.strategy_name,
        "execution_mode": attribution.execution_mode,
        "entry_surface": attribution.entry_surface,
        "idempotency_key": idempotency_key or attribution.idempotency_key,
        "basket_execution_id": basket_execution_id,
        "basket_leg_index": basket_leg_index,
        "attribution_json": _json_dumps(attribution_json),
        "cost_contract_json": _json_dumps(cost_contract),
    }


def create_live_order_intent(
    *,
    attribution: OrderAttribution,
    cost_contract: Dict[str, Any],
    idempotency_key: Optional[str],
    basket_execution_id: Optional[str] = None,
    basket_leg_index: Optional[int] = None,
    db: Any = None,
) -> str:
    intent_id = f"lint_{uuid.uuid4().hex}"
    owns_db = db is None
    session = db or SessionLocal()
    try:
        session.execute(
            text(
                """
                INSERT INTO public.live_order_intents (
                    intent_id,
                    client_order_ref,
                    account_id,
                    strategy_run_id,
                    journal_run_id,
                    strategy_family,
                    strategy_name,
                    execution_mode,
                    entry_surface,
                    idempotency_key,
                    basket_execution_id,
                    basket_leg_index,
                    attribution_json,
                    cost_contract_json
                ) VALUES (
                    :intent_id,
                    :client_order_ref,
                    :account_id,
                    :strategy_run_id,
                    CAST(:journal_run_id AS uuid),
                    :strategy_family,
                    :strategy_name,
                    :execution_mode,
                    :entry_surface,
                    :idempotency_key,
                    :basket_execution_id,
                    :basket_leg_index,
                    CAST(:attribution_json AS jsonb),
                    CAST(:cost_contract_json AS jsonb)
                )
                """
            ),
            _intent_params(
                intent_id=intent_id,
                attribution=attribution,
                cost_contract=cost_contract,
                idempotency_key=idempotency_key,
                basket_execution_id=basket_execution_id,
                basket_leg_index=basket_leg_index,
            ),
        )
        if owns_db:
            session.commit()
        return intent_id
    except Exception:
        if owns_db:
            session.rollback()
        raise
    finally:
        if owns_db:
            session.close()


def mark_live_order_intent_placed(*, client_order_ref: str, broker_order_id: str) -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE public.live_order_intents
                SET broker_order_id = :broker_order_id,
                    status = 'placed',
                    updated_at = NOW()
                WHERE client_order_ref = :client_order_ref
                """
            ),
            {"client_order_ref": client_order_ref, "broker_order_id": broker_order_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def seed_live_order_state_projection(
    *,
    account_id: str,
    broker_order_id: str,
    status: str,
    order_payload: Dict[str, Any],
) -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                INSERT INTO public.order_state_projection (
                    account_id,
                    order_id,
                    latest_status,
                    latest_event_timestamp,
                    last_seen_filled_quantity,
                    dirty_for_trade_sync,
                    needs_reconcile,
                    terminal,
                    exchange,
                    tradingsymbol,
                    instrument_token,
                    product,
                    transaction_type,
                    updated_at
                ) VALUES (
                    :account_id,
                    :order_id,
                    :latest_status,
                    NOW(),
                    0,
                    FALSE,
                    TRUE,
                    FALSE,
                    :exchange,
                    :tradingsymbol,
                    :instrument_token,
                    :product,
                    :transaction_type,
                    NOW()
                )
                ON CONFLICT (account_id, order_id) DO UPDATE SET
                    latest_status = EXCLUDED.latest_status,
                    latest_event_timestamp = EXCLUDED.latest_event_timestamp,
                    needs_reconcile = TRUE,
                    exchange = COALESCE(EXCLUDED.exchange, public.order_state_projection.exchange),
                    tradingsymbol = COALESCE(EXCLUDED.tradingsymbol, public.order_state_projection.tradingsymbol),
                    instrument_token = COALESCE(EXCLUDED.instrument_token, public.order_state_projection.instrument_token),
                    product = COALESCE(EXCLUDED.product, public.order_state_projection.product),
                    transaction_type = COALESCE(EXCLUDED.transaction_type, public.order_state_projection.transaction_type),
                    updated_at = NOW()
                """
            ),
            {
                "account_id": account_id,
                "order_id": broker_order_id,
                "latest_status": status,
                "exchange": order_payload.get("exchange"),
                "tradingsymbol": order_payload.get("tradingsymbol"),
                "instrument_token": order_payload.get("instrument_token"),
                "product": order_payload.get("product"),
                "transaction_type": order_payload.get("transaction_type"),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def mark_live_order_intent_failed(*, client_order_ref: str, error: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE public.live_order_intents
                SET status = 'failed',
                    error_json = CAST(:error_json AS jsonb),
                    updated_at = NOW()
                WHERE client_order_ref = :client_order_ref
                """
            ),
            {"client_order_ref": client_order_ref, "error_json": _json_dumps(error)},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
