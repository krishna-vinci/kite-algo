import pytest
from typing import Literal

from backend.options.execution.models import OptionRunCreateRequest
from backend.options.execution.store import OptionRunStore


def _create_request(product: Literal["MIS", "NRML"] = "MIS") -> OptionRunCreateRequest:
    return OptionRunCreateRequest(
        strategy_name="bull_call_spread",
        product=product,
        legs=[
            {"leg_id": "buy_1", "transaction_type": "BUY", "quantity": 75},
            {"leg_id": "sell_1", "transaction_type": "SELL", "quantity": 75},
        ],
        protection={"stoploss_pct": 20},
        metadata={"source": "test"},
    )


def test_create_run_persists_canonical_state_with_product_and_defaults():
    store = OptionRunStore()

    run = store.create_run(_create_request("MIS"))

    assert run.strategy_run_id.startswith("opt_run_")
    assert run.strategy_name == "bull_call_spread"
    assert run.product == "MIS"
    assert run.status == "created"
    assert run.legs == [
        {"leg_id": "buy_1", "transaction_type": "BUY", "quantity": 75},
        {"leg_id": "sell_1", "transaction_type": "SELL", "quantity": 75},
    ]
    assert run.protection == {"stoploss_pct": 20}
    assert run.metadata == {"source": "test"}
    assert run.orders == []
    assert run.trades == []


def test_create_run_ids_are_unique_with_expected_prefix():
    store = OptionRunStore()

    run_one = store.create_run(_create_request("MIS"))
    run_two = store.create_run(_create_request("NRML"))

    assert run_one.strategy_run_id.startswith("opt_run_")
    assert run_two.strategy_run_id.startswith("opt_run_")
    assert run_one.strategy_run_id != run_two.strategy_run_id


def test_record_orders_and_trades_are_append_only():
    store = OptionRunStore()
    run = store.create_run(_create_request())

    store.record_orders(run.strategy_run_id, [{"order_id": "o1"}])
    updated = store.record_orders(run.strategy_run_id, [{"order_id": "o2"}])
    assert [item["order_id"] for item in updated.orders] == ["o1", "o2"]

    store.record_trades(run.strategy_run_id, [{"trade_id": "t1"}])
    updated = store.record_trades(run.strategy_run_id, [{"trade_id": "t2"}])
    assert [item["trade_id"] for item in updated.trades] == ["t1", "t2"]


def test_list_get_save_roundtrip():
    store = OptionRunStore()
    run = store.create_run(_create_request())

    fetched = store.get_run(run.strategy_run_id)
    fetched.status = "entry_previewed"
    fetched.metadata["reviewed"] = True
    saved = store.save_run(fetched)

    assert saved.status == "entry_previewed"
    assert saved.metadata["reviewed"] is True

    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].strategy_run_id == run.strategy_run_id
    assert runs[0].status == "entry_previewed"


def test_missing_run_lookup_has_clear_failure_message():
    store = OptionRunStore()

    with pytest.raises(KeyError, match="Option run not found"):
        store.get_run("opt_run_missing")


def test_get_run_rejects_empty_id_with_value_error():
    store = OptionRunStore()

    with pytest.raises(ValueError, match="strategy_run_id is required"):
        store.get_run("")


def test_reset_clears_store_and_resets_counter():
    store = OptionRunStore()
    store.create_run(_create_request())
    store.reset()

    assert store.list_runs() == []

    run = store.create_run(_create_request())
    assert run.strategy_run_id == "opt_run_000001"
