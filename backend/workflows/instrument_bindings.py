"""Explicit instrument-binding ownership for the evaluation worker.

C1 contract: exactly ONE mutable binding state exists per worker. Feed-source
factories, candle-history readers, the market-runtime renewal callback, and
health reporting all read immutable snapshots from this registry; only the
refresh path applies catalog resolutions through :meth:`apply`. Mutable
shared dictionaries are never handed to consumers.

Every accepted change bumps :attr:`revision` so consumers can compare the
binding revision they were built against with the one currently accepted.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

__all__ = ["BindingChange", "InstrumentBindingRegistry"]


@dataclass(frozen=True)
class BindingChange:
    """The diff one :meth:`apply` call accepted."""

    added: Dict[str, int] = field(default_factory=dict)
    changed: Dict[str, int] = field(default_factory=dict)  # token replacement
    removed: Set[str] = field(default_factory=set)         # authoritative rejections
    previous_revision: int = 0
    revision: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)


class InstrumentBindingRegistry:
    """Thread-safe ``instrument_key -> broker_token`` binding owner."""

    def __init__(self, initial: Optional[Dict[str, int]] = None) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, int] = {
            str(key).strip().upper(): int(token)
            for key, token in (initial or {}).items()
        }
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._tokens)

    def get(self, instrument_key: str) -> Optional[int]:
        with self._lock:
            return self._tokens.get(str(instrument_key).strip().upper())

    def apply(
        self,
        resolved: Dict[str, int],
        rejected: Optional[Set[str]] = None,
    ) -> BindingChange:
        """Accept one resolution diff and return what changed.

        ``resolved`` maps instrument keys to their catalog-approved tokens;
        ``rejected`` lists previously-bound keys the catalog authoritatively
        refuses (retired / expired / ambiguous / not found). Keys absent from
        both keep their current binding untouched.
        """
        cleaned: Dict[str, int] = {}
        for key, token in (resolved or {}).items():
            try:
                value = int(token)
            except (TypeError, ValueError):
                logger.warning("ignoring invalid catalog token for %s: %r", key, token)
                continue
            if value > 0:
                cleaned[str(key).strip().upper()] = value

        rejected_keys = {
            str(key).strip().upper() for key in (rejected or set())
        }

        with self._lock:
            added: Dict[str, int] = {}
            changed: Dict[str, int] = {}
            removed: Set[str] = set()
            for key, token in cleaned.items():
                current = self._tokens.get(key)
                if current is None:
                    added[key] = token
                elif current != token:
                    changed[key] = token
            for key in rejected_keys:
                if key in self._tokens and key not in cleaned:
                    removed.add(key)
            if added or changed or removed:
                for key, token in added.items():
                    self._tokens[key] = token
                for key, token in changed.items():
                    self._tokens[key] = token
                for key in removed:
                    self._tokens.pop(key, None)
                self._revision += 1
            return BindingChange(
                added=added,
                changed=changed,
                removed=removed,
                previous_revision=self._revision - (1 if added or changed or removed else 0),
                revision=self._revision,
            )
