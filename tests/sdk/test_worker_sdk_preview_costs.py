"""0.7.6 SDK coverage: typed preview costs, snapshot previews, and data-failure errors."""
import asyncio
import sys
from pathlib import Path

import pytest

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)
sys.modules.pop("broker_api.orders", None)

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))


COST_CONTRACT_PAYLOAD = {
    "margin_required": 25000.0,
    "charges_estimate": 31.25,
    "total_charges": 31.25,
    "total_taxes": 26.25,
    "itemized": {
        "brokerage": 0.0,
        "exchange_transaction_charge": 5.0,
        "stt": 10.0,
        "stamp_duty": 2.0,
        "sebi_charge": 0.25,
        "gst": 14.0,
        "future_additive_charge": 1.5,
    },
    "dp_charge": None,
    "dp_charge_status": "unavailable",
}

ORDER_PREVIEW_PAYLOAD = {
    "strategy_run_id": "run-1",
    "mode": "paper",
    "preview": {
        "intent_type": "place_order",
        "order": {"tradingsymbol": "INFY", "quantity": 1},
        "cost_contract": COST_CONTRACT_PAYLOAD,
    },
}

BASKET_PREVIEW_PAYLOAD = {
    "strategy_run_id": "run-1",
    "mode": "paper",
    "preview": {
        "intent_type": "place_basket",
        "basket": {"orders": [{"tradingsymbol": "INFY", "quantity": 1}], "all_or_none": False},
        "cost_contract": COST_CONTRACT_PAYLOAD,
    },
}


def _sync_client():
    from kite_algo_worker import AlgoWorkerConfig, KiteAlgoWorkerClient

    return KiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_request(client, payload):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return dict(payload)

    client._request = fake_request
    return calls


def _async_client():
    from kite_algo_worker import AlgoWorkerConfig
    from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient

    return AsyncKiteAlgoWorkerClient(AlgoWorkerConfig(base_url="http://localhost:8000", token="kwa_test", timeout=3))


def _capture_async_request(client, payload):
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append({"method": method, "path": path, "json": kwargs.get("json")})
        return dict(payload)

    object.__setattr__(client, "_request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# Task 5: itemized preview costs
# ---------------------------------------------------------------------------


def test_itemized_charges_types_known_keys_and_preserves_unknown_keys():
    from kite_algo_worker import ItemizedCharges

    itemized = ItemizedCharges.model_validate(COST_CONTRACT_PAYLOAD["itemized"])
    assert itemized.brokerage == 0.0
    assert itemized.exchange_transaction_charge == 5.0
    assert itemized.stt == 10.0
    assert itemized.stamp_duty == 2.0
    assert itemized.sebi_charge == 0.25
    assert itemized.gst == 14.0
    assert itemized.raw == {"future_additive_charge": 1.5}

    dumped = itemized.model_dump()
    assert dumped["future_additive_charge"] == 1.5
    assert dumped["brokerage"] == 0.0
    assert dumped.pop("future_additive_charge") == 1.5
    assert dumped == {
        "brokerage": 0.0,
        "exchange_transaction_charge": 5.0,
        "stt": 10.0,
        "stamp_duty": 2.0,
        "sebi_charge": 0.25,
        "gst": 14.0,
    }


def test_itemized_charges_absent_components_stay_none():
    from kite_algo_worker import ItemizedCharges

    itemized = ItemizedCharges.model_validate({"gst": 14})
    assert itemized.gst == 14.0
    assert itemized.brokerage is None
    assert itemized.stt is None
    assert "brokerage" not in itemized.model_dump()
    assert itemized.model_dump(exclude_none=False)["brokerage"] is None


def test_cost_contract_types_costs_and_never_invents_dp_charge():
    from kite_algo_worker import CostContract, ItemizedCharges

    contract = CostContract.model_validate(COST_CONTRACT_PAYLOAD)
    assert contract.margin_required == 25000.0
    assert contract.charges_estimate == 31.25
    assert contract.total_charges == 31.25
    assert contract.total_taxes == 26.25
    assert isinstance(contract.itemized, ItemizedCharges)
    assert contract.itemized.raw == {"future_additive_charge": 1.5}
    # An absent/unavailable DP charge must not be converted to zero.
    assert contract.dp_charge is None
    assert type(contract.dp_charge) is not float
    assert contract.dp_charge_status == "unavailable"

    provided = CostContract.model_validate({**COST_CONTRACT_PAYLOAD, "dp_charge": 15.5, "dp_charge_status": "estimated"})
    assert provided.dp_charge == 15.5
    assert provided.dp_charge_status == "estimated"


def test_cost_contract_coerces_numeric_strings_and_ints():
    from kite_algo_worker import CostContract

    contract = CostContract.model_validate({"margin_required": "25000", "charges_estimate": 31, "total_taxes": "26.25"})
    assert contract.margin_required == 25000.0
    assert contract.charges_estimate == 31.0
    assert contract.total_taxes == 26.25
    assert contract.itemized is None
    assert contract.dp_charge is None
    assert contract.dp_charge_status is None


def test_order_preview_model_carries_itemized_contract():
    from kite_algo_worker import OrderPreview

    preview = OrderPreview.model_validate(ORDER_PREVIEW_PAYLOAD)
    assert preview.strategy_run_id == "run-1"
    assert preview.preview.cost_contract.itemized.stt == 10.0
    assert preview.preview.cost_contract.dp_charge is None
    assert preview.preview.cost_contract.itemized.model_dump()["future_additive_charge"] == 1.5


# ---------------------------------------------------------------------------
# Task 5: typed snapshot preview helpers (never submit orders)
# ---------------------------------------------------------------------------


def test_sync_preview_order_snapshot_validates_without_submitting():
    from kite_algo_worker import OrderPreview

    client = _sync_client()
    calls = _capture_request(client, ORDER_PREVIEW_PAYLOAD)

    snapshot = client.preview_order_snapshot("run-1", {"tradingsymbol": "INFY", "quantity": 1})
    assert isinstance(snapshot, OrderPreview)
    assert snapshot.preview.cost_contract.itemized.gst == 14.0
    assert calls == [
        {
            "method": "POST",
            "path": "/worker/runs/run-1/preview/order",
            "json": {"order": {"tradingsymbol": "INFY", "quantity": 1}, "metadata": {}},
        }
    ]


def test_sync_preview_basket_snapshot_validates_without_submitting():
    from kite_algo_worker import OrderPreview

    client = _sync_client()
    calls = _capture_request(client, BASKET_PREVIEW_PAYLOAD)

    orders = [{"tradingsymbol": "INFY", "quantity": 1}]
    snapshot = client.preview_basket_snapshot("run-1", orders, metadata={"signal": "x"}, all_or_none=True)
    assert isinstance(snapshot, OrderPreview)
    assert snapshot.preview.intent_type == "place_basket"
    assert snapshot.preview.cost_contract.total_charges == 31.25
    assert calls == [
        {
            "method": "POST",
            "path": "/worker/runs/run-1/preview/basket",
            "json": {"orders": orders, "metadata": {"signal": "x"}, "all_or_none": True},
        }
    ]


def test_sync_raw_preview_methods_stay_backward_compatible():
    client = _sync_client()
    calls = _capture_request(client, ORDER_PREVIEW_PAYLOAD)
    raw = client.preview_order("run-1", {"tradingsymbol": "INFY"})
    assert type(raw) is dict
    assert calls[0]["path"] == "/worker/runs/run-1/preview/order"


def test_async_preview_snapshots_validate_without_submitting():
    from kite_algo_worker import OrderPreview
    from kite_algo_worker.async_client import AsyncKiteAlgoWorkerClient

    async def main():
        client = _async_client()
        calls = _capture_async_request(client, ORDER_PREVIEW_PAYLOAD)
        order_snapshot = await client.preview_order_snapshot("run-1", {"tradingsymbol": "INFY", "quantity": 1})
        assert isinstance(order_snapshot, OrderPreview)
        assert order_snapshot.preview.cost_contract.itemized.sebi_charge == 0.25
        assert calls[-1]["path"] == "/worker/runs/run-1/preview/order"

        calls2 = _capture_async_request(client, BASKET_PREVIEW_PAYLOAD)
        basket_snapshot = await client.preview_basket_snapshot("run-1", [{"tradingsymbol": "INFY"}], all_or_none=True)
        assert isinstance(basket_snapshot, OrderPreview)
        assert calls2[-1]["path"] == "/worker/runs/run-1/preview/basket"
        assert calls2[-1]["json"]["all_or_none"] is True

        raw = await client.preview_order("run-1", {"tradingsymbol": "INFY"})
        assert type(raw) is dict

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Task 6: data-failure exception hierarchy
# ---------------------------------------------------------------------------


def test_rejection_reason_is_normalized_from_both_envelope_shapes():
    from kite_algo_worker import KiteAlgoWorkerError

    direct = KiteAlgoWorkerError("down", status_code=503, response_body={"rejection_reason": "CALENDAR_UNAVAILABLE", "detail": "x"})
    assert direct.rejection_reason == "CALENDAR_UNAVAILABLE"

    nested = KiteAlgoWorkerError("down", status_code=503, response_body={"detail": {"rejection_reason": "CALENDAR_UNAVAILABLE"}})
    assert nested.rejection_reason == "CALENDAR_UNAVAILABLE"

    missing = KiteAlgoWorkerError("boom", status_code=500, response_body={"detail": "boom"})
    assert missing.rejection_reason is None

    no_body = KiteAlgoWorkerError("boom", status_code=409)
    assert no_body.response_body is None
    assert no_body.rejection_reason is None
    assert no_body.status_code == 409


def test_error_for_status_maps_rejection_reasons_to_typed_errors():
    from kite_algo_worker import (
        AuthError,
        BrokerValidationError,
        CalendarRangeUncoveredError,
        KiteAlgoWorkerError,
        PermissionDeniedError,
        UnsupportedSchemaVersionError,
        WorkerDataUnavailableError,
    )
    from kite_algo_worker.exceptions import error_for_status

    exc = error_for_status(422, {"detail": {"rejection_reason": "UNSUPPORTED_SCHEMA_VERSION", "supported": [1]}}, fallback="fb")
    assert type(exc) is UnsupportedSchemaVersionError
    assert isinstance(exc, BrokerValidationError)
    assert exc.rejection_reason == "UNSUPPORTED_SCHEMA_VERSION"
    assert exc.status_code == 422

    exc = error_for_status(503, {"detail": {"rejection_reason": "CALENDAR_RANGE_UNCOVERED"}}, fallback="fb")
    assert type(exc) is CalendarRangeUncoveredError
    assert isinstance(exc, WorkerDataUnavailableError)
    assert isinstance(exc, KiteAlgoWorkerError)
    assert exc.rejection_reason == "CALENDAR_RANGE_UNCOVERED"

    for reason in ("CALENDAR_UNAVAILABLE", "PORTFOLIO_SNAPSHOT_UNAVAILABLE"):
        exc = error_for_status(503, {"rejection_reason": reason}, fallback="fb")
        assert type(exc) is WorkerDataUnavailableError, reason
        assert exc.rejection_reason == reason

    # Preserved legacy mappings.
    assert type(error_for_status(401, {}, fallback="fb")) is AuthError
    assert type(error_for_status(403, {}, fallback="fb")) is PermissionDeniedError
    assert type(error_for_status(400, {}, fallback="fb")) is BrokerValidationError
    assert type(error_for_status(422, {"detail": "bad order"}, fallback="fb")) is BrokerValidationError
    assert type(error_for_status(500, {}, fallback="fb")) is KiteAlgoWorkerError
    # 503 without an unavailable reason stays a generic worker error.
    assert type(error_for_status(503, {"detail": "maintenance"}, fallback="fb")) is KiteAlgoWorkerError
    # 401 keeps priority over any rejection reason.
    assert type(error_for_status(401, {"rejection_reason": "CALENDAR_UNAVAILABLE"}, fallback="fb")) is AuthError


def test_new_exceptions_are_exported_from_package_root():
    import kite_algo_worker as sdk

    for name in (
        "ItemizedCharges",
        "WorkerDataUnavailableError",
        "CalendarRangeUncoveredError",
        "UnsupportedSchemaVersionError",
    ):
        assert hasattr(sdk, name), name
        assert name in sdk.__all__, name

    from kite_algo_worker import exceptions as sdk_exceptions

    for name in ("WorkerDataUnavailableError", "CalendarRangeUncoveredError", "UnsupportedSchemaVersionError"):
        assert name in sdk_exceptions.__all__, name


def test_data_unavailable_errors_carry_status_and_body():
    from kite_algo_worker import CalendarRangeUncoveredError, WorkerDataUnavailableError

    body = {"detail": {"rejection_reason": "CALENDAR_RANGE_UNCOVERED", "coverage_end": "2026-08-31"}}
    exc = CalendarRangeUncoveredError("range uncovered", status_code=503, response_body=body)
    assert exc.status_code == 503
    assert exc.response_body == body
    assert exc.rejection_reason == "CALENDAR_RANGE_UNCOVERED"

    exc = WorkerDataUnavailableError("calendar unavailable", status_code=503, response_body={"rejection_reason": "CALENDAR_UNAVAILABLE"})
    assert exc.status_code == 503
    assert exc.rejection_reason == "CALENDAR_UNAVAILABLE"
