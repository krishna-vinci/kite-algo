from .bracket_stoploss import ModularBracketStoplossAlgo
from .ema_monitor import ModularEmaMonitorAlgo
from .index_stoploss import ModularIndexStoplossAlgo


def register_builtin_algos(registry) -> None:
    registry.register(ModularIndexStoplossAlgo)
    registry.register(ModularBracketStoplossAlgo)
    registry.register(ModularEmaMonitorAlgo)


__all__ = [
    "ModularBracketStoplossAlgo",
    "ModularEmaMonitorAlgo",
    "ModularIndexStoplossAlgo",
    "register_builtin_algos",
]
