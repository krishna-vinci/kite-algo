from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from options.strategy.compiler import compile_option_strategy_preview
from options.strategy.models import SelectedOptionLeg


def test_compile_short_straddle_uses_combined_premium_primary_metric():
    preview = compile_option_strategy_preview(
        underlying="NIFTY",
        template_id="short_straddle",
        strategy_type="short_straddle",
        current_spot=25000,
        legs=[
            SelectedOptionLeg(
                instrument_token=1,
                tradingsymbol="NIFTY26MAY25000CE",
                strike=25000,
                option_type="CE",
                transaction_type="SELL",
                ltp=120,
                lot_size=75,
                lots=1,
            ),
            SelectedOptionLeg(
                instrument_token=2,
                tradingsymbol="NIFTY26MAY25000PE",
                strike=25000,
                option_type="PE",
                transaction_type="SELL",
                ltp=130,
                lot_size=75,
                lots=1,
            ),
        ],
    )

    assert preview.primary_metric.value == "combined_premium_points"
    assert preview.inferred_family.value == "neutral-short-premium"
