import pytest

from tests.test_support import install_dependency_stubs

install_dependency_stubs()


def test_options_package_exports_canonical_symbols():
    from options import execution, market, protection, strategy

    assert market is not None
    assert strategy is not None
    assert execution is not None
    assert protection is not None


def test_legacy_strategy_package_reexports_canonical_preview():
    from strategies.option_strategy import compile_option_strategy_preview

    assert callable(compile_option_strategy_preview)


def test_option_run_create_request_requires_product():
    from pydantic import ValidationError

    from options.execution.models import OptionRunCreateRequest

    with pytest.raises(ValidationError):
        OptionRunCreateRequest.model_validate({"strategy_name": "bull_call_spread", "legs": []})


def test_option_run_create_request_accepts_mis_product():
    from options.execution.models import OptionRunCreateRequest

    request = OptionRunCreateRequest(strategy_name="bull_call_spread", product="MIS", legs=[])

    assert request.product == "MIS"


def test_option_run_create_request_rejects_invalid_product():
    from pydantic import ValidationError

    from options.execution.models import OptionRunCreateRequest

    with pytest.raises(ValidationError):
        OptionRunCreateRequest.model_validate({"strategy_name": "bull_call_spread", "product": "CNC", "legs": []})


def test_option_execution_leg_generates_compat_leg_id_and_normalizes_fields():
    from options.execution.models import OptionExecutionLeg

    leg = OptionExecutionLeg.model_validate(
        {
            "tradingsymbol": "NIFTY26MAY25000CE",
            "transaction_type": "buy",
            "quantity": 75,
            "product": "mis",
        }
    )

    assert leg.leg_id is not None
    assert leg.leg_id.startswith("leg_")
    assert leg.transaction_type == "BUY"
    assert leg.product == "MIS"


def test_option_run_create_request_coerces_legs_to_option_execution_legs():
    from options.execution.models import OptionExecutionLeg, OptionRunCreateRequest

    request = OptionRunCreateRequest.model_validate(
        {
            "strategy_name": "bull_call_spread",
            "product": "MIS",
            "legs": [
                {
                    "tradingsymbol": "NIFTY26MAY25000CE",
                    "transaction_type": "BUY",
                    "quantity": 75,
                }
            ],
        }
    )

    assert len(request.legs) == 1
    assert isinstance(request.legs[0], OptionExecutionLeg)
    assert request.legs[0].leg_id is not None
    assert request.legs[0].leg_id.startswith("leg_")
    assert request.legs[0].product == "MIS"
