import unittest
from decimal import Decimal

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from paper_runtime.models import (  # noqa: E402
    FundLedgerEntryType,
    PaperFundLedgerEntry,
    PaperOrder,
    PaperPositionLotAttribution,
)


class PaperRuntimeModelTests(unittest.TestCase):
    def test_order_defaults_pending_quantity_from_quantity(self):
        order = PaperOrder(
            account_scope="kite:test-user",
            order_id="PO-1",
            instrument_token=256265,
            transaction_type="buy",
            quantity=5,
        )

        self.assertEqual(order.pending_quantity, 5)

    def test_order_rejects_overfilled_quantity(self):
        with self.assertRaises(ValueError):
            PaperOrder(
                account_scope="kite:test-user",
                order_id="PO-2",
                instrument_token=256265,
                transaction_type="buy",
                quantity=5,
                filled_quantity=6,
            )

    def test_position_lot_defaults_remaining_quantity(self):
        lot = PaperPositionLotAttribution(
            account_scope="kite:test-user",
            lot_id="LOT-1",
            instrument_token=256265,
            source_trade_id="TR-1",
            open_quantity=15,
            entry_price=Decimal("223.50"),
        )

        self.assertEqual(lot.remaining_quantity, 15)

    def test_fund_ledger_entry_accepts_enum_values(self):
        entry = PaperFundLedgerEntry(
            account_scope="kite:test-user",
            entry_type=FundLedgerEntryType.CREDIT,
            amount=Decimal("2500.00"),
        )

        self.assertEqual(entry.entry_type, FundLedgerEntryType.CREDIT)
