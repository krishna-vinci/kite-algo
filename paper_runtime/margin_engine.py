from __future__ import annotations

from decimal import Decimal


class PaperMarginEngine:
    """Conservative paper margin model for equities, futures, and options."""

    def required_margin(
        self,
        *,
        side: str,
        product: str,
        quantity: int,
        reference_price: Decimal,
        instrument_type: str | None = None,
    ) -> Decimal:
        qty = Decimal(max(int(quantity), 0))
        price = Decimal(reference_price or 0)
        if qty <= 0 or price <= 0:
            return Decimal("0")

        side_value = str(side or "").upper()
        product_value = str(product or "").upper()
        instrument_value = str(instrument_type or "").upper()
        notional = price * qty

        if instrument_value in {"CE", "PE"}:
            if side_value == "BUY":
                return notional
            return notional * Decimal("1.25")
        if instrument_value == "FUT":
            return notional * (Decimal("0.18") if product_value == "MIS" else Decimal("0.35"))
        if product_value == "MIS":
            return notional * Decimal("0.20")
        return notional
