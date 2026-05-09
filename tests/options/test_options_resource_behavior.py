from __future__ import annotations

from backend.options.execution.models import OptionRunState
from backend.options.market.service import OptionsMarketService
from backend.options.protection.runtime import evaluate_option_protection_state


class _FakeInstrumentRepo:
    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper(), None


class _CountingManager:
    def __init__(self, snapshot: dict):
        self.instrument_repo = _FakeInstrumentRepo()
        self._snapshot = snapshot
        self.get_snapshot_calls = 0
        self.start_session_calls = 0
        self.ensure_session_calls = 0

    def normalize_underlying_symbol(self, value: str):
        return value.strip().upper()

    def get_snapshot(self, _underlying: str):
        self.get_snapshot_calls += 1
        return self._snapshot

    def start_session(self, *_args, **_kwargs):  # pragma: no cover - should never be called
        self.start_session_calls += 1
        raise AssertionError("start_session must not be called by market service")

    def ensure_session(self, *_args, **_kwargs):  # pragma: no cover - should never be called
        self.ensure_session_calls += 1
        raise AssertionError("ensure_session must not be called by market service")


def _build_snapshot_with_many_strikes() -> dict:
    rows = []
    strike = 22000
    for i in range(21):
        current = strike + (i * 50)
        rows.append(
            {
                "strike": current,
                "CE": {
                    "token": 10000 + i,
                    "tsym": f"NIFTY30MAY{current}CE",
                    "ltp": 100.0 - i,
                },
                "PE": {
                    "token": 20000 + i,
                    "tsym": f"NIFTY30MAY{current}PE",
                    "ltp": 80.0 + i,
                },
            }
        )

    return {
        "underlying": "NIFTY",
        "spot_ltp": 22520.0,
        "updated_at": "2030-04-29T10:00:00Z",
        "expiries": ["2030-05-09"],
        "per_expiry": {
            "2030-05-09": {
                "atm_strike": 22500,
                "rows": rows,
            }
        },
    }


def test_market_service_uses_get_snapshot_only_without_session_start_hooks():
    manager = _CountingManager(_build_snapshot_with_many_strikes())
    service = OptionsMarketService(manager)

    session = service.get_session("NIFTY")
    chain = service.get_chain("NIFTY", "nearest")

    assert session["underlying"] == "NIFTY"
    assert chain["underlying"] == "NIFTY"
    assert manager.get_snapshot_calls == 2
    assert manager.start_session_calls == 0
    assert manager.ensure_session_calls == 0


def test_mini_chain_is_bounded_to_two_window_plus_one_rows():
    manager = _CountingManager(_build_snapshot_with_many_strikes())
    service = OptionsMarketService(manager)

    response = service.get_mini_chain("NIFTY", "nearest", window=2)

    assert response["window"] == 2
    assert len(response["contracts"]) <= (2 * 2) + 1


def test_mini_chain_direct_service_rejects_out_of_bounds_window():
    manager = _CountingManager(_build_snapshot_with_many_strikes())
    service = OptionsMarketService(manager)

    for invalid in (0, 21):
        try:
            service.get_mini_chain("NIFTY", "nearest", window=invalid)
        except ValueError as exc:
            assert "window" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"window={invalid} should fail")


def test_protection_runtime_uses_only_run_and_metric_snapshot():
    run = OptionRunState(
        strategy_run_id="opt_run_protection_resource",
        strategy_name="resource_guard",
        product="MIS",
        legs=[
            {
                "leg_id": "sell_ce",
                "transaction_type": "SELL",
                "tradingsymbol": "NIFTY30MAY22500CE",
                "quantity": 75,
            }
        ],
        protection={
            "rules": [
                {
                    "metric": "combined_premium",
                    "operator": "gte",
                    "threshold": 120.0,
                    "action": "exit",
                }
            ]
        },
    )
    run.completed_legs = ["sell_ce"]

    state = evaluate_option_protection_state(
        run=run,
        metric_snapshot={"combined_premium": 125.0, "open_quantity": 1},
    )

    assert state["triggered"] is True
    assert state["matched_rule"]["metric"] == "combined_premium"
    assert len(state["recommended_exit_orders"]) == 1


def test_market_service_passes_through_resource_error_without_crashing():
    snapshot = _build_snapshot_with_many_strikes()
    snapshot["resource_error"] = {
        "code": "OPTIONS_MARKET_RESOURCE_LIMIT",
        "message": "Option market data request exceeded the configured resource limit",
        "retryable": True,
    }
    manager = _CountingManager(snapshot)
    service = OptionsMarketService(manager)

    session = service.get_session("NIFTY")
    chain = service.get_chain("NIFTY", "nearest")

    assert session["resource_error"]["code"] == "OPTIONS_MARKET_RESOURCE_LIMIT"
    assert chain["resource_error"]["retryable"] is True
