import pytest


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
