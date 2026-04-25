from __future__ import annotations

from .models import DependencySpec, OrderScope, TriggerEvent, TriggerType


def trigger_matches(dependency_spec: DependencySpec, trigger: TriggerEvent) -> bool:
    if trigger.trigger_type not in dependency_spec.triggers:
        return False

    if trigger.trigger_type == TriggerType.TICK:
        return trigger.token in dependency_spec.market_tokens if trigger.token is not None else bool(dependency_spec.market_tokens)

    if trigger.trigger_type == TriggerType.CANDLE_CLOSE:
        if trigger.token is None or not trigger.timeframe:
            return bool(dependency_spec.candle_series or dependency_spec.indicators)
        for candle_spec in dependency_spec.candle_series:
            if candle_spec.token == trigger.token and candle_spec.timeframe == trigger.timeframe:
                return True
        for indicator_spec in dependency_spec.indicators:
            if indicator_spec.token == trigger.token and indicator_spec.timeframe == trigger.timeframe:
                return True
        return False

    if trigger.trigger_type in {TriggerType.POSITION_UPDATE, TriggerType.ORDER_UPDATE, TriggerType.FILL_UPDATE}:
        if dependency_spec.account_scope and trigger.account_id:
            if dependency_spec.account_scope != trigger.account_id:
                return False
        if trigger.trigger_type in {TriggerType.ORDER_UPDATE, TriggerType.FILL_UPDATE}:
            return dependency_spec.order_scope != OrderScope.NONE
        return dependency_spec.account_scope is not None or bool(dependency_spec.position_filters)

    return True


def manual_trigger(*, reason: str = "manual") -> TriggerEvent:
    return TriggerEvent(type=TriggerType.MANUAL, reason=reason)
