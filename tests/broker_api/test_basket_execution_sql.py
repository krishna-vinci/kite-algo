from __future__ import annotations

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

from backend.broker_api.orders.basket_execution import BasketExecutionStore


class _FakeDB:
    def __init__(self) -> None:
        self.statement = ""
        self.params = {}

    def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params or {}


def test_mark_leg_submit_failed_uses_sqlalchemy_safe_text_cast() -> None:
    store = BasketExecutionStore()
    db = _FakeDB()

    store.mark_leg_submit_failed(
        db,  # type: ignore[arg-type]
        basket_execution_id="basket-1",
        leg_index=0,
        error_message="broker rejected order",
    )

    assert "CAST(:error_message AS TEXT)" in db.statement
    assert ":error_message::TEXT" not in db.statement
    assert db.params == {
        "basket_execution_id": "basket-1",
        "leg_index": 0,
        "error_message": "broker rejected order",
    }
