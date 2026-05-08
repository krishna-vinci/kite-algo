from options.market.analytics.max_pain import compute_bounded_max_pain
from options.market.analytics.pcr import compute_put_call_ratio
from options.market.snapshots import build_bounded_strike_window, build_mini_chain_view


def test_build_bounded_strike_window_returns_centered_window_around_atm():
    window = build_bounded_strike_window(
        strikes=[24900, 24950, 25000, 25050, 25100],
        atm_strike=25000,
        window=1,
    )
    assert window == [24950.0, 25000.0, 25050.0]


def test_build_mini_chain_view_returns_bounded_rows_in_strike_order():
    view = build_mini_chain_view(
        atm_strike=25000,
        window=1,
        strikes=[24900, 24950, 25000, 25050, 25100],
        strike_rows={
            24950: {"strike": 24950, "ce": {"oi": 100}, "pe": {"oi": 200}},
            25000: {"strike": 25000, "ce": {"oi": 120}, "pe": {"oi": 240}},
            25050: {"strike": 25050, "ce": {"oi": 90}, "pe": {"oi": 180}},
        },
    )

    assert [row["strike"] for row in view] == [24950, 25000, 25050]


def test_compute_put_call_ratio_uses_bounded_oi_totals_only():
    ratio = compute_put_call_ratio(
        [
            {"ce": {"oi": 100}, "pe": {"oi": 200}},
            {"ce": {"oi": 50}, "pe": {"oi": 150}},
        ]
    )
    assert ratio == 2.33


def test_compute_bounded_max_pain_returns_minimum_pain_strike():
    strike = compute_bounded_max_pain(
        [
            {"strike": 100, "ce": {"oi": 10}, "pe": {"oi": 30}},
            {"strike": 110, "ce": {"oi": 20}, "pe": {"oi": 20}},
            {"strike": 120, "ce": {"oi": 30}, "pe": {"oi": 10}},
        ]
    )
    assert strike == 110.0
