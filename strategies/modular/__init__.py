from .bracket_stoploss import ModularBracketStoplossAlgo
from .combined_premium_stoploss import ModularCombinedPremiumStoplossAlgo
from .ema_monitor import ModularEmaMonitorAlgo
from .index_stoploss import ModularIndexStoplossAlgo


def register_builtin_algos(registry) -> None:
    registry.register(ModularIndexStoplossAlgo)
    registry.register(ModularBracketStoplossAlgo)
    registry.register(ModularEmaMonitorAlgo)
    registry.register(ModularCombinedPremiumStoplossAlgo)


__all__ = [
    "ModularBracketStoplossAlgo",
    "ModularCombinedPremiumStoplossAlgo",
    "ModularEmaMonitorAlgo",
    "ModularIndexStoplossAlgo",
    "register_builtin_algos",
]
