"""External signal context for evaluation (Phase 4 F10).

The consuming side of registered producers: resolves
``external.<producer>.<field>`` references for one observation into the
``context`` mapping that the predicate layer already understands, exactly the
way ``FundamentalsLoader`` supplies ``fundamentals.*``.

Sampled, never pushed: this loader runs when a stage evaluates on its own
``candle_close`` clock. It creates no clock, wakes no worker, and consumes no
queue — so an accepted value can expire between two evaluations, which is
disclosed rather than hidden. What durable acceptance guarantees is that the
value is stored and visible to any evaluation while it is valid.

Every failure mode resolves to UNKNOWN with a named reason; nothing here can
manufacture a signal.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from backend.workflows import external_signals as signals

__all__ = ["ExternalSignalLoader", "external_reference"]

# external.<producer>.<field>
_REFERENCE = re.compile(r"^external\.([A-Za-z0-9_\-]+)\.([A-Za-z0-9_]+)$")


def external_reference(name: str) -> Optional[Tuple[str, str]]:
    """Split ``external.<producer>.<field>`` into its parts, or None."""
    match = _REFERENCE.match(name or "")
    if match is None:
        return None
    return match.group(1), match.group(2)


class ExternalSignalLoader:
    """Bounded, per-dispatch resolver for external producer values.

    Results are cached per (owner, producer, field, cutoff, instrument). The
    cutoff is part of the cache key because it IS the evaluation semantics: the
    same field read at a different event time legitimately yields a different
    value, and caching across cutoffs would silently reuse a stale answer.
    """

    def __init__(self, session_factory: Any, *, max_entries: int = 512) -> None:
        self._sessions = session_factory
        self._cache: Dict[Tuple[str, str, str, str, Optional[str]], Any] = {}
        self._max_entries = int(max_entries)
        self.hits = 0
        self.misses = 0
        self.unknown_reasons: Dict[str, int] = {}

    def _record(self, reason: Optional[str]) -> None:
        if reason:
            self.unknown_reasons[reason] = self.unknown_reasons.get(reason, 0) + 1

    def context_for(
        self,
        owner_id: str,
        references: Iterable[str],
        *,
        cutoff: datetime,
        instrument_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve the requested references as of ``cutoff``.

        Returns a context fragment carrying both the resolved values
        (``external.<producer>.<field>``) and their provenance
        (``external.<producer>.<field>.event_time`` / ``.acquired_at``), so a
        fired event records exactly which observation was used. Unresolvable
        references are simply absent, which the predicate layer treats as
        unknown.
        """
        names = [name for name in dict.fromkeys(references) if name]
        wanted = [(name, external_reference(name)) for name in names]
        wanted = [(name, parts) for name, parts in wanted if parts is not None]
        if not wanted:
            return {}
        context: Dict[str, Any] = {}
        session: Optional[Session] = None
        try:
            for name, (producer_name, field) in wanted:
                key = (
                    owner_id, producer_name, field,
                    cutoff.isoformat(), instrument_key,
                )
                if key in self._cache:
                    self.hits += 1
                    result = self._cache[key]
                else:
                    self.misses += 1
                    if session is None:
                        session = self._sessions()
                    result = signals.lookup_value(
                        session,
                        owner_id=owner_id,
                        producer_name=producer_name,
                        field=field,
                        cutoff=cutoff,
                        instrument_key=instrument_key,
                    )
                    self._store(key, result)
                self._record(result.reason)
                if result.usable and result.value is not None:
                    context[name] = result.value
                    if result.event_time is not None:
                        context[f"{name}.event_time"] = result.event_time.isoformat()
                    if result.acquired_at is not None:
                        context[f"{name}.acquired_at"] = result.acquired_at.isoformat()
                    if result.expires_at is not None:
                        context[f"{name}.expires_at"] = result.expires_at.isoformat()
                elif result.reason:
                    # Unknown, with the reason preserved for evidence/health.
                    context[f"{name}.unknown_reason"] = result.reason
        finally:
            if session is not None:
                session.close()
        return context

    def _store(self, key, result) -> None:
        if len(self._cache) >= self._max_entries:
            # Bounded: drop the oldest insertion order entry rather than grow
            # without limit in a long-running worker.
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = result

    def release(self) -> None:
        self._cache.clear()

    def health(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "unknown_reasons": dict(self.unknown_reasons),
        }


def collect_external_references(stage_specs: Sequence[Tuple[str, Sequence[Any]]]) -> list:
    """Collect ``external.*`` field names referenced by a stage's conditions.

    Accepts ``(stage_id, conditions)`` pairs so the caller can gather from a
    stage and its ancestor layers in one pass.
    """
    names: list = []
    for _stage_id, conditions in stage_specs:
        for condition in conditions or ():
            for operand in (condition.left, condition.right):
                if operand is None or operand.kind != "field":
                    continue
                name = operand.name or ""
                if name.startswith("external."):
                    names.append(name)
    return names
