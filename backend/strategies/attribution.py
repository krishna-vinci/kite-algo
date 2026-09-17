"""Durable strategy attribution: facts, resolution, fold, store and service.

Why this module exists
----------------------

One canonical, owner-controlled strategy identity must own every fill, and the
broker account's net position must never become strategy ownership: the broker
nets per ``(instrument, product)`` and knows nothing about which strategy opened
a position. This module materialises the platform's truth — a rebuildable
strategy position book per execution environment — from immutable fill facts
plus their durable attribution.

Three properties are structural, not conventional:

* **Books are separate.** ``execution_environment ∈ {live, paper, dry_run}`` is
  carried on every fact and every position key, so a paper fill can never alter
  a live projection and a simulated offset can never hide live exposure.
* **Canonical identity is resolved per fact, before folding.** A fill is mapped
  to an instrument using the mapping interval valid at *that fill's* effective
  time, never today's ``is_current`` mapping. A token re-mapped to a different
  instrument across generations therefore produces two positions rather than a
  false flat.
* **The projection is a full recompute**, inside one transaction whose advisory
  lock is taken *before* the fact snapshot, so an older snapshot can never
  overwrite a newer rebuild. ``content_sha256`` exists only for idempotence and
  is never chronology evidence.

Unresolved facts are never guessed at: they remain explicit ``identity_kind="raw"``
rows carrying a catalog-evidence era, so facts from the same era net across dates
while facts from distinct known eras never merge. Unresolved exposure is surfaced
as ``UNRESOLVED_INSTRUMENT_IDENTITY`` for later admission/reconciliation to
consume; this module does not build a freeze service.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

#: The immutable book dimension. Equal to the run's persisted execution mode.
EXECUTION_ENVIRONMENTS = ("live", "paper", "dry_run")

#: Named condition for exposure that could not be mapped to a canonical instrument.
UNRESOLVED_INSTRUMENT_IDENTITY = "UNRESOLVED_INSTRUMENT_IDENTITY"


@dataclass(frozen=True)
class TradeFact:
    """One immutable fill fact, before canonical identity resolution.

    ``source_key`` is the durable fill identity (``trade:<trade_id>`` for live,
    the paper trade key for paper) and is the deduplication key: re-ingesting
    the same fill is idempotent. ``execution_environment`` comes from the run's
    binding, never from the fact's own payload, so a fill can never drift into
    another book.
    """

    source_key: str
    strategy_run_id: str
    execution_environment: str
    instrument_token: int
    exchange: str
    tradingsymbol: str
    product: str
    signed_quantity: int
    effective_at: datetime
    pinned_generation: Optional[str] = None


class PositionKey(NamedTuple):
    """The resolved position identity: one book line.

    ``identity_kind`` is ``canonical`` (``identity_key`` is the canonical
    instrument UUID) or ``raw`` (``identity_key`` is a catalog-evidence
    era-qualified raw identity). Raw and canonical identities never merge, and
    neither do distinct eras or distinct environments.
    """

    execution_environment: str
    identity_kind: str
    identity_key: str
    product: str


class AttributionFold:
    """Pure, deterministic aggregation of resolved facts into a position book."""

    @staticmethod
    def fold(facts: Iterable[Tuple[TradeFact, PositionKey]]) -> Dict[PositionKey, int]:
        """Sum signed quantities per resolved position key.

        Deduplicates by ``source_key`` (the fact sources already deduplicate at
        SQL level; this is belt-and-braces determinism), drops flat positions,
        and never consults broker aggregates. The result is ordered by position
        key so identical fact *sets* produce byte-identical content regardless
        of ingestion order — that ordering is what makes the published
        ``content_sha256`` meaningful for idempotence.
        """
        ordered = sorted(facts, key=lambda pair: (pair[0].source_key, pair[1]))
        totals: Dict[PositionKey, int] = {}
        seen: Set[str] = set()
        for fact, key in ordered:
            if fact.source_key in seen:
                continue
            seen.add(fact.source_key)
            if key.execution_environment != fact.execution_environment:
                raise ValueError(
                    f"position key environment {key.execution_environment!r} disagrees with "
                    f"fact environment {fact.execution_environment!r} for {fact.source_key}"
                )
            totals[key] = totals.get(key, 0) + int(fact.signed_quantity)
        return {key: quantity for key, quantity in sorted(totals.items()) if quantity != 0}
