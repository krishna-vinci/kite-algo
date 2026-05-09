from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


ZERO = Decimal("0")


class ChargesStatus(str, Enum):
    ESTIMATED = "estimated"
    BROKER_QUOTED = "broker_quoted"
    RECONCILED = "reconciled"
    UNAVAILABLE = "unavailable"


class OrderAttribution(BaseModel):
    strategy_run_id: str
    strategy_family: str
    strategy_name: str
    execution_mode: str
    account_ref: str
    entry_surface: str
    journal_run_id: Optional[str] = None
    source: str = "kite_algo"
    client_order_ref: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "strategy_run_id",
        "strategy_family",
        "strategy_name",
        "execution_mode",
        "account_ref",
        "entry_surface",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("app order attribution requires strategy, mode, account, and surface")
        return cleaned


class ExecutionCostContract(BaseModel):
    margin_required: Decimal = ZERO
    charges_estimate: Decimal = ZERO
    brokerage: Decimal = ZERO
    exchange_txn_charge: Decimal = ZERO
    stt: Decimal = ZERO
    stamp_duty: Decimal = ZERO
    sebi_charge: Decimal = ZERO
    gst: Decimal = ZERO
    total_taxes: Decimal = ZERO
    total_charges: Decimal = ZERO
    net_cash_impact_estimate: Decimal = ZERO
    charges_status: ChargesStatus = ChargesStatus.ESTIMATED
    raw: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_totals(self) -> "ExecutionCostContract":
        taxes = self.stt + self.stamp_duty + self.sebi_charge + self.gst
        non_tax_charges = self.brokerage + self.exchange_txn_charge
        total = non_tax_charges + taxes
        if self.total_taxes == ZERO:
            self.total_taxes = taxes
        if self.total_charges == ZERO:
            self.total_charges = total if total != ZERO else self.charges_estimate
        return self

    def journal_payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


def signed_cash_flow(*, side: str, price: Decimal, quantity: int) -> Decimal:
    notional = Decimal(price) * Decimal(int(quantity))
    return notional if str(side or "").lower() == "sell" else -notional
