import sys
import types
import unittest

from tests.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

if "broker_api.options_sessions" not in sys.modules:
    options_sessions = types.ModuleType("broker_api.options_sessions")

    class OptionsSessionManager:  # pragma: no cover - stub carrier
        pass

    options_sessions.OptionsSessionManager = OptionsSessionManager
    sys.modules["broker_api.options_sessions"] = options_sessions

if "broker_api.instruments_repository" not in sys.modules:
    instruments_repository = types.ModuleType("broker_api.instruments_repository")

    class InstrumentsRepository:  # pragma: no cover - stub carrier
        pass

    instruments_repository.InstrumentsRepository = InstrumentsRepository
    sys.modules["broker_api.instruments_repository"] = instruments_repository

from strategies.strike_selector import PositionBuilder, StrikeSelector  # noqa: E402


class _FakeRepo:
    def __init__(self, lot_sizes=None):
        self.lot_sizes = lot_sizes or {}

    def get_lot_size(self, _token):
        return self.lot_sizes.get(_token, 50)

    def get_distinct_strikes(self, _underlying, _expiry):
        return [22300, 22400, 22500, 22600, 22700]

    def get_strikes_around_atm(self, _center, strikes, _count):
        return strikes


class _FakeSelectorBackend:
    async def suggest_strikes(self, *_args, **_kwargs):
        return {
            "legs": [
                {
                    "tsym": "NIFTY24APR22600CE",
                    "token": 601,
                    "strike": 22600,
                    "option_type": "CE",
                    "transaction_type": "SELL",
                    "ltp": 42.0,
                }
            ],
            "recommended_lots": 1,
        }


class StrikeSelectorTests(unittest.IsolatedAsyncioTestCase):
    def test_find_strike_by_delta_uses_strikes_payload_shape(self):
        selector = StrikeSelector(types.SimpleNamespace(sessions={}), _FakeRepo())
        chain_data = {
            "strikes": [
                {
                    "strike": 22600,
                    "ce": {
                        "instrument_token": 601,
                        "tradingsymbol": "NIFTY24APR22600CE",
                        "ltp": 42.0,
                        "greeks": {"delta": 0.31, "gamma": 0.02, "theta": -5.0, "vega": 8.0, "iv": 14.2},
                    },
                    "pe": None,
                }
            ]
        }

        match = selector.find_strike_by_delta(chain_data, "CE", 0.30)

        self.assertIsNotNone(match)
        self.assertEqual(match["token"], 601)
        self.assertEqual(match["tsym"], "NIFTY24APR22600CE")

    async def test_auto_position_plan_populates_strategy_leg_quantity_without_risk_amount(self):
        builder = PositionBuilder(_FakeSelectorBackend(), _FakeRepo())

        plan = await builder.build_position_plan(
            underlying="NIFTY",
            expiry=__import__("datetime").date(2026, 4, 30),
            strategy_type="single_leg",
        )

        self.assertEqual(plan["strategy_legs"][0]["quantity"], 50)
        self.assertEqual(plan["strategy_legs"][0]["lots"], 1)

    async def test_manual_position_plan_prefers_repository_lot_size_over_frontend_payload(self):
        builder = PositionBuilder(_FakeSelectorBackend(), _FakeRepo({16237570: 65, 16237826: 65}))

        plan = await builder.build_position_plan_from_strikes(
            underlying="NIFTY",
            expiry=__import__("datetime").date(2026, 4, 21),
            strategy_type="straddle",
            selected_strikes=[
                {
                    "instrument_token": 16237570,
                    "tradingsymbol": "NIFTY2642124250CE",
                    "strike": 24250,
                    "option_type": "CE",
                    "transaction_type": "SELL",
                    "ltp": 216.0,
                    "lot_size": 25,
                    "lots": 1,
                },
                {
                    "instrument_token": 16237826,
                    "tradingsymbol": "NIFTY2642124250PE",
                    "strike": 24250,
                    "option_type": "PE",
                    "transaction_type": "SELL",
                    "ltp": 220.4,
                    "lot_size": 25,
                    "lots": 1,
                },
            ],
        )

        self.assertEqual(plan["orders"][0]["lot_size"], 65)
        self.assertEqual(plan["orders"][0]["quantity"], 65)
        self.assertEqual(plan["orders"][1]["lot_size"], 65)
        self.assertEqual(plan["orders"][1]["quantity"], 65)
        self.assertEqual(plan["strategy_legs"][0]["lot_size"], 65)
        self.assertEqual(plan["strategy_legs"][0]["quantity"], 65)


if __name__ == "__main__":
    unittest.main()
