from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from execution_accounting.contracts import ChargesStatus, ExecutionCostContract


class PaperChargesCalculator:
    """Lightweight local charges estimate for paper trading."""

    def estimate(
        self,
        *,
        price: Decimal,
        quantity: int,
        instrument_type: str | None = None,
        exchange: str | None = None,
        product: str | None = None,
    ) -> Decimal:
        turnover = Decimal(price or 0) * Decimal(max(int(quantity), 0))
        rate = Decimal("0.0003")
        if str(instrument_type or "").upper() in {"CE", "PE", "FUT"}:
            rate = Decimal("0.0005")
        elif str(exchange or "").upper() == "MCX":
            rate = Decimal("0.0006")
        charges = turnover * rate
        return charges.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def estimate_contract(
        self,
        *,
        price: Decimal,
        quantity: int,
        instrument_type: str | None = None,
        exchange: str | None = None,
        product: str | None = None,
    ) -> ExecutionCostContract:
        total = self.estimate(
            price=price,
            quantity=quantity,
            instrument_type=instrument_type,
            exchange=exchange,
            product=product,
        )
        return ExecutionCostContract(
            charges_estimate=total,
            brokerage=total,
            total_charges=total,
            charges_status=ChargesStatus.ESTIMATED,
            raw={
                "source": "paper_estimator",
                "instrument_type": str(instrument_type or ""),
                "exchange": str(exchange or ""),
                "product": str(product or ""),
            },
        )
