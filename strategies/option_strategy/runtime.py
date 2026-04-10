from __future__ import annotations

from typing import Any, Dict, Optional

from algo_runtime.models import (
    AlgoInstance,
    AlgoLifecycleState,
    DependencySpec,
    ExecutionMode,
    OrderScope,
    PositionFilter,
    TriggerType,
)

from .models import CanonicalOptionStrategyPreview, RuntimeManagedOptionStrategyConfig, SelectedOptionLeg


def build_runtime_option_instance(*,
    strategy_id: str,
    execution_mode: str,
    account_scope: str,
    selected_legs: list[dict[str, Any]],
    strategy_preview: dict[str, Any],
    session_id: Optional[str] = None,
    spot_token: Optional[int] = None,
    underlying: Optional[str] = None,
) -> AlgoInstance:
    preview = CanonicalOptionStrategyPreview.model_validate(strategy_preview)
    legs = [leg if isinstance(leg, SelectedOptionLeg) else SelectedOptionLeg.model_validate(leg) for leg in selected_legs]
    if not legs:
        raise ValueError("runtime-managed option strategy requires at least one selected leg")

    triggers = {TriggerType.POSITION_UPDATE, TriggerType.ORDER_UPDATE, TriggerType.FILL_UPDATE}
    market_tokens: Dict[int, str] = {}
    if spot_token is not None:
        market_tokens[int(spot_token)] = 'ltp'
        triggers.add(TriggerType.TICK)

    dependency_spec = DependencySpec(
        market_tokens=market_tokens,
        account_scope=account_scope,
        position_filters=[PositionFilter(instrument_tokens={int(leg.instrument_token) for leg in legs})],
        order_scope=OrderScope.ACCOUNT_RELEVANT,
        triggers=triggers,
    )

    config = RuntimeManagedOptionStrategyConfig(
        account_scope=account_scope,
        selected_legs=legs,
        rules=preview.rules,
        precedence=preview.precedence,
        spot_token=spot_token,
        session_id=session_id,
        underlying=underlying,
        dry_run=(execution_mode == ExecutionMode.DRY_RUN.value),
    )

    return AlgoInstance(
        instance_id=f'option-strategy:{strategy_id}',
        algo_type='runtime_option_strategy',
        status=AlgoLifecycleState.ENABLED,
        execution_mode=ExecutionMode(execution_mode),
        config=config.model_dump(mode='json'),
        dependency_spec=dependency_spec,
        metadata={
            'strategy_id': strategy_id,
            'strategy_family': preview.inferred_family.value if hasattr(preview.inferred_family, 'value') else str(preview.inferred_family),
            'strategy_structure': preview.inferred_structure,
            'managed_by': 'option_strategy_runtime',
        },
    )
