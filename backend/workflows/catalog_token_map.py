"""EXCHANGE:SYMBOL -> broker token, resolved through the published catalog.

Screeners rank a whole universe (e.g. NIFTY 50), not just the instruments that
have tick subscriptions, so their candle reads must resolve any member through
the catalog. It exposes ``get()`` only, so ``PgCandleHistory`` uses it as a
live provider. Successful resolutions are cached; failures are not, so a
transient catalog/DB error never hides an instrument for the life of the
process.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class CatalogTokenMap:
    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._sessions = session_factory
        self._cache: Dict[str, int] = {}

    def get(self, key: str) -> Optional[int]:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            from backend.workflows.worker_entry import resolve_catalog_instrument_tokens

            resolved, _rejected = resolve_catalog_instrument_tokens(
                {key}, self._sessions, fallback_tokens={}
            )
            token = resolved.get(key)
        except Exception:
            logger.warning("catalog token resolution failed for %s", key, exc_info=True)
            return None
        if token is not None:
            self._cache[key] = int(token)
        return token
