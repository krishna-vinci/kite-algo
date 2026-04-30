from __future__ import annotations

from typing import Any

from .models import OptionRunState
from .previews import build_entry_preview_packet, build_exit_preview_packet


class OptionExecutionRuntimeInstance:
    """Deterministic route-injection seam for execution packets/results."""

    def build_entry_plan(self, run: OptionRunState) -> list[dict[str, Any]]:
        return list(build_entry_preview_packet(run).get("order_plan") or [])

    def build_exit_plan(self, run: OptionRunState) -> list[dict[str, Any]]:
        return list(build_exit_preview_packet(run).get("order_plan") or [])

    def default_entry_results(self, run: OptionRunState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        for index, order in enumerate(self.build_entry_plan(run), start=1):
            order_id = f"{run.strategy_run_id}_entry_o{index}"
            normalized = {
                "order_id": order_id,
                "leg_id": order.get("leg_id"),
                "tradingsymbol": order.get("tradingsymbol"),
                "transaction_type": order.get("transaction_type"),
                "quantity": int(order.get("quantity") or 0),
                "product": order.get("product") or run.product,
                "status": "filled",
                "phase": "entry",
            }
            orders.append(normalized)
            trades.append(
                {
                    "trade_id": f"{run.strategy_run_id}_entry_t{index}",
                    "order_id": order_id,
                    "leg_id": normalized["leg_id"],
                    "tradingsymbol": normalized["tradingsymbol"],
                    "transaction_type": normalized["transaction_type"],
                    "quantity": normalized["quantity"],
                    "product": normalized["product"],
                    "phase": "entry",
                }
            )
        return orders, trades

    def default_exit_results(self, run: OptionRunState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        orders: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        for index, order in enumerate(self.build_exit_plan(run), start=1):
            order_id = f"{run.strategy_run_id}_exit_o{index}"
            normalized = {
                "order_id": order_id,
                "leg_id": order.get("leg_id"),
                "tradingsymbol": order.get("tradingsymbol"),
                "transaction_type": order.get("transaction_type"),
                "quantity": int(order.get("quantity") or 0),
                "product": order.get("product") or run.product,
                "status": "filled",
                "phase": "exit",
            }
            orders.append(normalized)
            trades.append(
                {
                    "trade_id": f"{run.strategy_run_id}_exit_t{index}",
                    "order_id": order_id,
                    "leg_id": normalized["leg_id"],
                    "tradingsymbol": normalized["tradingsymbol"],
                    "transaction_type": normalized["transaction_type"],
                    "quantity": normalized["quantity"],
                    "product": normalized["product"],
                    "phase": "exit",
                }
            )
        return orders, trades


_DEFAULT_RUNTIME_INSTANCE = OptionExecutionRuntimeInstance()


def get_option_execution_runtime_instance() -> OptionExecutionRuntimeInstance:
    return _DEFAULT_RUNTIME_INSTANCE
