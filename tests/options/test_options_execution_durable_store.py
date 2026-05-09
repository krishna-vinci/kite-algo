from __future__ import annotations

import json
from typing import Literal

import pytest

from backend.options.execution.durable_store import DurableOptionRunStore
from backend.options.execution.models import OptionRunCreateRequest, OptionRunState


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


class _FakeMappings:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[dict] | None = None, rowcount: int = 1):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return _FakeMappings(self._rows)


class _FakeSession:
    def __init__(self, *, fail_sql_contains: str | None = None, rows_by_id: dict[str, dict] | None = None):
        self.fail_sql_contains = fail_sql_contains
        self.calls: list[tuple[str, dict]] = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self._rows_by_id: dict[str, dict] = rows_by_id if rows_by_id is not None else {}

    def seed_run(self, run: OptionRunState) -> None:
        self._rows_by_id[run.strategy_run_id] = {
            "strategy_run_id": run.strategy_run_id,
            "strategy_name": run.strategy_name,
            "product": run.product,
            "status": run.status,
            "legs": list(run.legs),
            "protection": dict(run.protection) if isinstance(run.protection, dict) else run.protection,
            "metadata": dict(run.metadata),
            "orders": list(run.orders),
            "trades": list(run.trades),
            "completed_legs": list(run.completed_legs),
            "failed_legs": list(run.failed_legs),
            "pending_legs": list(run.pending_legs),
        }

    def execute(self, statement, params=None):
        text_sql = str(statement)
        bound = params or {}
        self.calls.append((text_sql, bound))
        if self.fail_sql_contains and self.fail_sql_contains in text_sql:
            raise RuntimeError("forced SQL failure")

        if "INSERT INTO public.option_run_states" in text_sql:
            strategy_run_id = bound["strategy_run_id"]
            self._rows_by_id[strategy_run_id] = {
                "strategy_run_id": strategy_run_id,
                "strategy_name": bound["strategy_name"],
                "product": bound["product"],
                "status": bound["status"],
                "legs": json.loads(bound["legs"]),
                "protection": json.loads(bound["protection"]),
                "metadata": json.loads(bound["metadata"]),
                "orders": json.loads(bound["orders"]),
                "trades": json.loads(bound["trades"]),
                "completed_legs": json.loads(bound["completed_legs"]),
                "failed_legs": json.loads(bound["failed_legs"]),
                "pending_legs": json.loads(bound["pending_legs"]),
            }
            return _FakeResult()

        if "WHERE strategy_run_id = :strategy_run_id" in text_sql and "SELECT" in text_sql:
            row = self._rows_by_id.get(bound["strategy_run_id"])
            return _FakeResult([row] if row else [], rowcount=1 if row else 0)

        if "FROM public.option_run_states" in text_sql and "ORDER BY" in text_sql:
            return _FakeResult(list(self._rows_by_id.values()))

        if "UPDATE public.option_run_states" in text_sql:
            strategy_run_id = bound["strategy_run_id"]
            row = self._rows_by_id.get(strategy_run_id)
            if row is None:
                return _FakeResult(rowcount=0)
            row.update(
                {
                    "strategy_name": bound["strategy_name"],
                    "product": bound["product"],
                    "status": bound["status"],
                    "legs": json.loads(bound["legs"]),
                    "protection": json.loads(bound["protection"]),
                    "metadata": json.loads(bound["metadata"]),
                    "orders": json.loads(bound["orders"]),
                    "trades": json.loads(bound["trades"]),
                    "completed_legs": json.loads(bound["completed_legs"]),
                    "failed_legs": json.loads(bound["failed_legs"]),
                    "pending_legs": json.loads(bound["pending_legs"]),
                }
            )
            return _FakeResult(rowcount=1)

        return _FakeResult()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


def test_create_run_inserts_json_fields_product_and_status_and_commits():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session, id_factory=lambda: "001")

    run = store.create_run(_create_request("MIS"))

    assert run.strategy_run_id == "opt_run_001"
    assert run.product == "MIS"
    assert run.status == "created"
    assert session.committed == 1
    assert session.rolled_back == 0
    assert session.closed == 1
    sql, params = session.calls[0]
    assert "INSERT INTO public.option_run_states" in sql
    assert params["product"] == "MIS"
    assert params["status"] == "created"
    assert json.loads(params["legs"])[0]["leg_id"] == "buy_1"
    assert json.loads(params["metadata"]) == {"source": "test"}


def test_get_run_maps_row_to_option_run_state():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session)
    seeded = OptionRunState.from_create_request(_create_request("NRML"), strategy_run_id="opt_run_seed")
    seeded.status = "entry_previewed"
    seeded.orders = [{"order_id": "o1"}]
    seeded.trades = [{"trade_id": "t1"}]
    session.seed_run(seeded)

    fetched = store.get_run("opt_run_seed")

    assert fetched.strategy_run_id == "opt_run_seed"
    assert fetched.product == "NRML"
    assert fetched.status == "entry_previewed"
    assert fetched.orders == [{"order_id": "o1"}]
    assert fetched.trades == [{"trade_id": "t1"}]
    assert session.closed == 1


def test_save_run_updates_json_fields_and_status():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session)
    seeded = OptionRunState.from_create_request(_create_request(), strategy_run_id="opt_run_save")
    session.seed_run(seeded)

    run = store.get_run("opt_run_save")
    run.status = "entered"
    run.metadata["reviewed"] = True
    run.completed_legs = ["buy_1", "sell_1"]
    run.failed_legs = []
    run.pending_legs = []
    run.orders = [{"order_id": "o1"}]
    run.trades = [{"trade_id": "t1"}]
    saved = store.save_run(run)

    assert saved.status == "entered"
    assert session.committed == 1
    assert session.rolled_back == 0
    assert session.closed == 2
    update_sql, update_params = session.calls[-1]
    assert "UPDATE public.option_run_states" in update_sql
    assert update_params["status"] == "entered"
    assert json.loads(update_params["orders"]) == [{"order_id": "o1"}]


def test_record_orders_and_trades_are_append_only():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session)
    seeded = OptionRunState.from_create_request(_create_request(), strategy_run_id="opt_run_append")
    seeded.orders = [{"order_id": "o1"}]
    seeded.trades = [{"trade_id": "t1"}]
    session.seed_run(seeded)

    updated_orders = store.record_orders("opt_run_append", [{"order_id": "o2"}])
    updated_trades = store.record_trades("opt_run_append", [{"trade_id": "t2"}])

    assert [item["order_id"] for item in updated_orders.orders] == ["o1", "o2"]
    assert [item["trade_id"] for item in updated_trades.trades] == ["t1", "t2"]
    assert any("FOR UPDATE" in sql for sql, _params in session.calls)


def test_missing_run_and_empty_id_errors_are_clear():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session)

    with pytest.raises(ValueError, match="strategy_run_id is required"):
        store.get_run("")

    with pytest.raises(KeyError, match="Option run not found: opt_run_missing"):
        store.get_run("opt_run_missing")


def test_create_run_rolls_back_and_closes_on_error():
    session = _FakeSession(fail_sql_contains="INSERT INTO public.option_run_states")
    store = DurableOptionRunStore(session_factory=lambda: session, id_factory=lambda: "err")

    with pytest.raises(RuntimeError, match="forced SQL failure"):
        store.create_run(_create_request())

    assert session.committed == 0
    assert session.rolled_back == 1
    assert session.closed == 1


def test_save_run_missing_row_raises_keyerror_and_rolls_back():
    session = _FakeSession()
    store = DurableOptionRunStore(session_factory=lambda: session)
    run = OptionRunState.from_create_request(_create_request(), strategy_run_id="opt_run_missing")

    with pytest.raises(KeyError, match="Option run not found: opt_run_missing"):
        store.save_run(run)

    assert session.committed == 0
    assert session.rolled_back == 1
    assert session.closed == 1


def test_durable_store_recovers_run_state_across_store_instances():
    shared_rows: dict[str, dict] = {}

    def session_factory():
        return _FakeSession(rows_by_id=shared_rows)

    writer = DurableOptionRunStore(session_factory=session_factory, id_factory=lambda: "restart")
    run = writer.create_run(_create_request("MIS"))
    run.status = "entered"
    run.protection = {"stoploss_pct": 10, "target_pct": 25}
    run.orders = [{"order_id": "order_entry", "product": "MIS"}]
    run.trades = [{"trade_id": "trade_entry", "quantity": 75, "price": 101.5}]
    run.completed_legs = ["buy_1"]
    run.pending_legs = ["sell_1"]
    writer.save_run(run)

    # A new store instance with fresh sessions simulates process restart over the
    # same durable table. Executed facts and protection config must survive.
    reader = DurableOptionRunStore(session_factory=session_factory)
    recovered = reader.get_run("opt_run_restart")

    assert recovered.status == "entered"
    assert recovered.product == "MIS"
    assert recovered.protection == {"stoploss_pct": 10, "target_pct": 25}
    assert recovered.orders == [{"order_id": "order_entry", "product": "MIS"}]
    assert recovered.trades == [{"trade_id": "trade_entry", "quantity": 75, "price": 101.5}]
    assert recovered.completed_legs == ["buy_1"]
    assert recovered.pending_legs == ["sell_1"]
