"""Compatibility shim for canonical options strategy compiler.

Canonical ownership now lives under ``options.strategy.compiler``.
"""

from options.strategy.compiler import compile_option_strategy_preview

__all__ = ["compile_option_strategy_preview"]
