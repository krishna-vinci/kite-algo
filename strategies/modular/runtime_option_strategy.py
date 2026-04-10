from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from algo_runtime.models import NoopAction, NotifyAction, OrderIntent, Snapshot, StatePatchAction
from strategies.option_strategy.models import MetricKind, RuleRole, RuntimeManagedOptionStrategyConfig


TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED", "EXPIRED", "FILLED"}


class RuntimeManagedOptionStrategyAlgo:
    ALGO_TYPE = "runtime_option_strategy"

    def __init__(self, instance, **kwargs):
        self.instance = instance
        self.config = RuntimeManagedOptionStrategyConfig.model_validate(instance.config)

    async def initialize(self, context: Any) -> None:
        return None

    async def evaluate(self, snapshot: Snapshot, state: Dict[str, Any]) -> List[Any]:
        if state.get("triggered"):
            return [NoopAction(reason="already_triggered")]

        open_positions = self._open_positions(snapshot)
        if not open_positions:
            return [
                StatePatchAction(
                    patch={
                        "completed": True,
                        "completed_at": self._utcnow(),
                        "last_evaluated_at": self._utcnow(),
                        "_instance_status": "stopped",
                    }
                )
            ]

        metrics = self._metric_values(snapshot, open_positions)
        trigger_rule = self._trigger_rule(metrics)

        if trigger_rule is None:
            return [
                StatePatchAction(
                    patch={
                        "current_metrics": metrics,
                        "last_evaluated_at": self._utcnow(),
                    }
                )
            ]

        actions: List[Any] = [
            NotifyAction(
                message=f"Runtime option strategy trigger for {self.instance.instance_id}: {trigger_rule.label}",
                level="warning",
                metadata={
                    "instance_id": self.instance.instance_id,
                    "rule_key": trigger_rule.key,
                    "rule_label": trigger_rule.label,
                    "metric": trigger_rule.metric.value,
                    "threshold": trigger_rule.threshold,
                    "current_metrics": metrics,
                },
            )
        ]

        exit_orders, skipped_positions, blocked_symbols = self._build_exit_orders(snapshot, open_positions)
        if blocked_symbols and self.config.skip_if_exit_order_open and not exit_orders:
            actions.append(
                NotifyAction(
                    message=(
                        f"Runtime option strategy trigger for {self.instance.instance_id} is currently blocked because exit orders are already open "
                        f"for {', '.join(sorted(blocked_symbols))}"
                    ),
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "blocked_symbols": sorted(blocked_symbols)},
                )
            )
            actions.append(
                StatePatchAction(
                    patch={
                        "current_metrics": metrics,
                        "blocked_symbols": sorted(blocked_symbols),
                        "last_evaluated_at": self._utcnow(),
                        "pending_trigger_rule": trigger_rule.key,
                    }
                )
            )
            return actions
        elif exit_orders:
            basket_payload = {
                "basket": {
                    "orders": exit_orders,
                    "all_or_none": self.config.all_or_none,
                    "dry_run": self.config.dry_run,
                },
                "trigger_reason": trigger_rule.key,
                "trigger_metric": trigger_rule.metric.value,
            }
            if self.config.session_id:
                basket_payload["session_id"] = self.config.session_id
            actions.append(
                OrderIntent(
                    intent_type="place_basket",
                    dedupe_key=f"{self.instance.instance_id}:{trigger_rule.key}",
                    payload=basket_payload,
                )
            )
            if blocked_symbols:
                actions.append(
                    StatePatchAction(
                        patch={
                            "current_metrics": metrics,
                            "blocked_symbols": sorted(blocked_symbols),
                            "last_evaluated_at": self._utcnow(),
                            "pending_trigger_rule": trigger_rule.key,
                        }
                    )
                )
                return actions
        else:
            actions.append(
                NotifyAction(
                    message=f"Runtime option strategy triggered for {self.instance.instance_id}, but no positions were available to exit",
                    level="info",
                    metadata={"instance_id": self.instance.instance_id, "rule_key": trigger_rule.key},
                )
            )

        if skipped_positions:
            actions.append(
                NotifyAction(
                    message=f"Skipped {skipped_positions} invalid position(s) while building runtime option exit basket for {self.instance.instance_id}",
                    level="warning",
                    metadata={"instance_id": self.instance.instance_id, "skipped_positions": skipped_positions},
                )
            )

        patch = {
            "triggered": True,
            "triggered_at": self._utcnow(),
            "trigger_rule": trigger_rule.key,
            "trigger_label": trigger_rule.label,
            "current_metrics": metrics,
            "last_evaluated_at": self._utcnow(),
            "blocked_symbols": sorted(blocked_symbols),
        }
        if self.config.auto_disable_after_trigger:
            patch["_instance_status"] = "stopped"
        actions.append(StatePatchAction(patch=patch))
        return actions

    def _open_positions(self, snapshot: Snapshot) -> List[Dict[str, Any]]:
        positions = ((snapshot.positions or {}).get("filtered", {})) or {}
        open_positions: List[Dict[str, Any]] = []
        for position in positions.values():
            payload = position if isinstance(position, dict) else position.model_dump(mode="json")
            quantity = self._position_quantity(payload)
            if quantity == 0:
                continue
            open_positions.append(payload)
        return open_positions

    def _metric_values(self, snapshot: Snapshot, positions: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
        return {
            MetricKind.INDEX_PRICE.value: self._current_index_price(snapshot),
            MetricKind.COMBINED_PREMIUM_POINTS.value: self._current_combined_premium_points(positions),
            MetricKind.BASKET_MTM_RUPEES.value: self._current_basket_mtm(snapshot, positions),
        }

    def _current_index_price(self, snapshot: Snapshot) -> Optional[float]:
        if self.config.spot_token is None:
            return None
        market_ltp = (snapshot.market or {}).get("ltp", {}) or {}
        if str(self.config.spot_token) in market_ltp:
            return float(market_ltp[str(self.config.spot_token)])
        tick = ((snapshot.market or {}).get("ticks", {}) or {}).get(str(self.config.spot_token))
        if isinstance(tick, dict) and tick.get("last_price") is not None:
            return float(tick["last_price"])
        return None

    def _current_combined_premium_points(self, positions: List[Dict[str, Any]]) -> Optional[float]:
        if not positions:
            return None
        leg_map = {int(leg.instrument_token): leg for leg in self.config.selected_legs}
        total = 0.0
        found = False
        for payload in positions:
            instrument_token = int(payload.get("instrument_token") or 0)
            leg = leg_map.get(instrument_token)
            last_price = self._position_last_price(payload)
            quantity = abs(self._position_quantity(payload))
            if last_price is None or quantity <= 0:
                continue
            lot_size = int(payload.get("lot_size") or (leg.lot_size if leg else 1) or 1)
            total += float(last_price) * (float(quantity) / float(max(lot_size, 1)))
            found = True
        return round(total, 2) if found else None

    def _current_basket_mtm(self, snapshot: Snapshot, positions: List[Dict[str, Any]]) -> Optional[float]:
        totals = ((snapshot.positions or {}).get("totals", {})) or {}
        total_pnl = totals.get("total_pnl")
        if total_pnl is not None:
            return round(float(total_pnl), 2)
        found = False
        total = 0.0
        for payload in positions:
            pnl = payload.get("pnl")
            if pnl is None:
                realized = payload.get("realized_pnl") or 0
                unrealized = payload.get("unrealized_pnl") or 0
                try:
                    pnl = float(realized) + float(unrealized)
                except (TypeError, ValueError):
                    pnl = None
            if pnl is None:
                continue
            total += float(pnl)
            found = True
        return round(total, 2) if found else None

    def _trigger_rule(self, metrics: Dict[str, Optional[float]]):
        role_order = list(self.config.precedence or [RuleRole.EMERGENCY_GUARD, RuleRole.HARD_STOP, RuleRole.PROFIT_TARGET, RuleRole.TRAILING_STOP])
        rules = list(self.config.rules)
        for role in role_order:
            for rule in rules:
                if rule.role != role:
                    continue
                value = metrics.get(rule.metric.value)
                if value is None:
                    continue
                if rule.operator == "lte" and float(value) <= float(rule.threshold):
                    return rule
                if rule.operator == "gte" and float(value) >= float(rule.threshold):
                    return rule
        return None

    def _build_exit_orders(self, snapshot: Snapshot, positions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, set[str]]:
        relevant_orders = ((snapshot.orders or {}).get("relevant", [])) or []
        exit_orders: List[Dict[str, Any]] = []
        skipped_positions = 0
        blocked_symbols: set[str] = set()
        for payload in positions:
            quantity = abs(self._position_quantity(payload))
            tradingsymbol = payload.get("tradingsymbol")
            if quantity == 0 or not tradingsymbol:
                skipped_positions += 1
                continue
            transaction_type = "SELL" if self._position_quantity(payload) > 0 else "BUY"
            if self.config.skip_if_exit_order_open and self._has_open_exit_order(relevant_orders, tradingsymbol, transaction_type):
                blocked_symbols.add(str(tradingsymbol))
                continue
            exit_orders.append({
                "exchange": payload.get("exchange", "NFO"),
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "variety": self.config.order_variety,
                "product": self.config.product_override or payload.get("product", "MIS"),
                "order_type": self.config.exit_order_type,
                "quantity": quantity,
            })
        return exit_orders, skipped_positions, blocked_symbols

    def _has_open_exit_order(self, orders: List[Dict[str, Any]], tradingsymbol: str, transaction_type: str) -> bool:
        for order in orders:
            status = str(order.get("latest_status") or order.get("status") or "").upper()
            if status in TERMINAL_ORDER_STATUSES:
                continue
            if order.get("tradingsymbol") != tradingsymbol:
                continue
            if str(order.get("transaction_type") or "").upper() != transaction_type:
                continue
            return True
        return False

    def _position_quantity(self, payload: Dict[str, Any]) -> int:
        return int(payload.get("quantity") or payload.get("net_quantity") or 0)

    def _position_last_price(self, payload: Dict[str, Any]) -> Optional[float]:
        value = payload.get("last_price")
        if value is None and isinstance(payload.get("metadata"), dict):
            value = payload["metadata"].get("last_price")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _utcnow(self) -> str:
        return datetime.now(timezone.utc).isoformat()
