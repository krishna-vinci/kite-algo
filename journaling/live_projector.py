from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from execution_accounting.contracts import signed_cash_flow

from .models import JournalExecutionFact, SourceType
from .repository import JournalRepository


def _parse_fill_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _is_reducing_fill(*, net_quantity: int, side: str, quantity: int) -> bool:
    normalized = str(side or "").upper()
    if net_quantity > 0 and normalized == "SELL":
        return quantity <= abs(net_quantity)
    if net_quantity < 0 and normalized == "BUY":
        return quantity <= abs(net_quantity)
    return False


def resolve_external_fill_run(*, repository: JournalRepository, fill: Dict[str, Any]) -> Dict[str, str]:
    candidates = repository.find_open_live_runs_for_instrument(
        account_id=str(fill["account_id"]),
        instrument_token=int(fill["instrument_token"]),
        product=str(fill["product"]),
    )
    reducing_candidates = [
        candidate
        for candidate in candidates
        if _is_reducing_fill(
            net_quantity=int(candidate.get("net_quantity") or 0),
            side=str(fill.get("transaction_type") or ""),
            quantity=int(fill.get("quantity") or 0),
        )
    ]
    if len(reducing_candidates) == 1:
        return {"resolution": "external_exit", "run_id": str(reducing_candidates[0]["run_id"])}
    return {"resolution": "broker_import", "run_id": ""}


class LiveJournalProjector:
    def __init__(self, repository: Optional[JournalRepository] = None) -> None:
        self.repository = repository or JournalRepository()

    def project(self, *, batch_size: int = 100) -> Dict[str, int]:
        projected = 0
        imported = 0
        external_exit = 0
        for fill in self.repository.list_unprojected_live_fills(batch_size=batch_size):
            intent = self.repository.find_live_order_intent(account_id=str(fill["account_id"]), order_id=str(fill["order_id"]))
            resolution = "live_intent"
            if intent:
                run_id = self.repository.ensure_live_strategy_run_for_intent(intent=intent)
                source_type = SourceType.LIVE_FILL
                cost_contract = intent.get("cost_contract_json") or {}
                projected += 1
            else:
                resolved = resolve_external_fill_run(repository=self.repository, fill=fill)
                if resolved["resolution"] == "external_exit" and resolved.get("run_id"):
                    run_id = resolved["run_id"]
                    source_type = SourceType.LIVE_FILL
                    cost_contract = {}
                    resolution = "external_exit"
                    external_exit += 1
                    projected += 1
                else:
                    run_id = self.repository.ensure_imported_broker_run(account_id=str(fill["account_id"]))
                    source_type = SourceType.BROKER_IMPORT
                    cost_contract = {}
                    resolution = "broker_import"
                    imported += 1

            total_charges = _decimal(cost_contract.get("total_charges"))
            total_taxes = _decimal(cost_contract.get("total_taxes"))
            fees = total_charges - total_taxes
            price = _decimal(fill["price"])
            quantity = int(fill["quantity"])
            side = str(fill["transaction_type"])
            source_key = f"{fill['account_id']}:{fill['trade_id']}"

            self.repository.insert_execution_fact(
                JournalExecutionFact(
                    run_id=run_id,
                    source_type=source_type,
                    source_fact_key=source_key,
                    order_id=str(fill["order_id"]),
                    trade_id=str(fill["trade_id"]),
                    fill_timestamp=_parse_fill_timestamp(fill["fill_timestamp"]),
                    side=side,
                    quantity=quantity,
                    price=price,
                    gross_cash_flow=signed_cash_flow(side=side, price=price, quantity=quantity),
                    fees_amount=fees,
                    taxes_amount=total_taxes,
                    payload={"broker_fill": fill, "cost_contract": cost_contract, "resolution": resolution},
                )
            )
            if resolution == "external_exit":
                self.repository.mark_run_externally_closed_if_flat(run_id=run_id)
        return {"projected": projected, "imported": imported, "external_exit": external_exit}
