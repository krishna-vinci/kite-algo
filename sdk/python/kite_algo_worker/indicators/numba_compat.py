from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

try:  # pragma: no cover - optional dependency shim
    from numba import njit as _njit

    NUMBA_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised when numba is absent
    NUMBA_AVAILABLE = False

    def _njit(*args: Any, **kwargs: Any):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def decorator(func: F) -> F:
            @wraps(func)
            def wrapped(*f_args: Any, **f_kwargs: Any):
                return func(*f_args, **f_kwargs)

            return wrapped  # type: ignore[return-value]

        return decorator


njit = _njit

__all__ = ["NUMBA_AVAILABLE", "njit"]
