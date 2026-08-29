from datetime import datetime, timedelta, timezone

import pytest

from backend.broker_api.account.portfolio_snapshot import PortfolioSnapshotUnavailable, build_portfolio_snapshot


class FakeKite:
    def margins(self):
        return {"equity": {"available": {"cash": 50000}}}

    def holdings(self):
        return [{"exchange": "NSE", "tradingsymbol": "SYNTH", "instrument_token": 101, "product": "CNC", "quantity": 2, "t1_quantity": 0, "used_quantity": 0, "authorised_quantity": 1, "authorisation": {}, "average_price": 100, "last_price": 120, "discrepancy": False, "mtf": {"quantity": 0}}]

    def positions(self):
        return {"net": [{"exchange": "NSE", "tradingsymbol": "SYNTH", "instrument_token": 101, "product": "CNC", "quantity": 2, "pnl": 40}], "day": []}

    def profile(self):
        return {"meta": {"demat_consent": "consent"}}


def test_snapshot_has_v1_envelope_and_broker_fields():
    moments = iter(datetime(2026, 8, 29, tzinfo=timezone.utc) + timedelta(milliseconds=step) for step in (0, 1, 2, 3, 4))
    snapshot = build_portfolio_snapshot(FakeKite(), "kite:TEST", clock=lambda: next(moments))
    assert snapshot["schema_version"] == 1
    assert snapshot["account_scope"] == "kite:TEST"
    assert snapshot["coherent"] is True
    assert snapshot["holdings"][0]["authorised_quantity"] == 1
    assert snapshot["profile_capabilities"]["demat_consent"] == "consent"
    assert "total_fund" not in snapshot["funds"]


def test_snapshot_fails_closed_when_a_required_component_is_unavailable():
    class BrokenKite(FakeKite):
        def positions(self):
            raise RuntimeError("unavailable")

    with pytest.raises(PortfolioSnapshotUnavailable, match="positions"):
        build_portfolio_snapshot(BrokenKite(), "kite:TEST")


def test_snapshot_rejects_duplicate_holdings():
    class DuplicateKite(FakeKite):
        def holdings(self):
            row = super().holdings()[0]
            return [row, dict(row)]

    with pytest.raises(PortfolioSnapshotUnavailable, match="duplicate"):
        build_portfolio_snapshot(DuplicateKite(), "kite:TEST")
