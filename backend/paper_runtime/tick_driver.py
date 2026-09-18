"""Deterministic paper tick driver: certify partial fills across ticks (D-5).

Phase 7 shipped partial fills but could not certify a rebalance completing across
ticks, because the paper runtime's tick path needs a market snapshot and no test
had one. A certification that depends on a live feed is not a certification — it
is a hope with a passing test attached.

So this supplies the ticks instead: a deterministic price per instrument per tick,
fed to the runtime's own ``process_tick``. Nothing about the runtime changes; the
driver only decides what the market "said" and then asks the runtime to act on it.

What the certification actually proves is convergence and honesty. Tranches advance
monotonically, an order with a remainder is never reported as resolved, and the
book only settles once nothing is outstanding — because the failure this guards
against is a partially filled rebalance that *looks* finished.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class TickRun:
    """What one instrument's price series did, and what the runtime did with it."""

    instrument_token: int
    prices: List[Decimal]
    filled_per_tick: List[int] = field(default_factory=list)

    @property
    def final_filled(self) -> int:
        return self.filled_per_tick[-1] if self.filled_per_tick else 0

    @property
    def monotonic(self) -> bool:
        """Fills only ever grow: a rebalance that un-fills is a bug, not progress."""
        return all(
            later >= earlier
            for earlier, later in zip(self.filled_per_tick, self.filled_per_tick[1:])
        )


class SyntheticTickDriver:
    """Feeds deterministic prices through the real paper tick path."""

    def __init__(
        self,
        paper_service: Any,
        *,
        fill_progress_store: Any = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.paper_service = paper_service
        if fill_progress_store is None:
            from backend.paper_runtime.partial_fills import PaperFillProgressStore

            fill_progress_store = PaperFillProgressStore(session_factory=session_factory)
        self.fill_progress = fill_progress_store

    @staticmethod
    def price_series(*, start: float, step: float = 0.0, count: int = 6) -> List[Decimal]:
        """A deterministic series: ``start``, ``start + step``, and so on."""
        base = Decimal(str(start))
        increment = Decimal(str(step))
        return [base + (increment * index) for index in range(int(count))]

    async def run(
        self,
        *,
        instrument_token: int,
        prices: Sequence[Any],
        account_scope: str = "kite:paper",
        paper_order_ids: Optional[Iterable[str]] = None,
    ) -> TickRun:
        """Drive one tick per price and record how the fill advanced each time."""
        series = [Decimal(str(price)) for price in prices]
        wanted = {str(item) for item in paper_order_ids} if paper_order_ids else None
        filled_per_tick: List[int] = []

        for price in series:
            await self.paper_service.process_tick(
                {
                    "instrument_token": int(instrument_token),
                    "last_price": price,
                    "account_scope": account_scope,
                }
            )
            filled_per_tick.append(
                self._filled_total(account_scope=account_scope, paper_order_ids=wanted)
            )

        return TickRun(
            instrument_token=int(instrument_token),
            prices=series,
            filled_per_tick=filled_per_tick,
        )

    def _filled_total(
        self, *, account_scope: str, paper_order_ids: Optional[set]
    ) -> int:
        """Total filled quantity across the tracked orders.

        Reads progress directly rather than the OPEN remainders: a completed order
        leaves that set, and a series that fell back to zero on completion would
        look like a fill that un-happened instead of one that finished.
        """
        if paper_order_ids:
            total = 0
            for order_id in sorted(paper_order_ids):
                progress = self.fill_progress.progress_for(
                    account_scope=account_scope, paper_order_id=order_id
                )
                if progress is not None:
                    total += int(progress.filled_quantity)
            return total
        return sum(
            int(row.filled_quantity)
            for row in self.fill_progress.open_remainder_for(account_scope=account_scope)
        )

    def outstanding(self, *, account_scope: str, paper_order_ids: Optional[Iterable[str]] = None) -> bool:
        """Whether anything is still in flight: the quiescence check."""
        return self.fill_progress.has_open_remainder(
            account_scope=account_scope,
            paper_order_ids=list(paper_order_ids) if paper_order_ids else None,
        )


def tick_series_for(instrument_token: int, prices: Sequence[Any]) -> List[Dict[str, Any]]:
    """The tick payloads a driver would send, for tests that assert on shape."""
    return [
        {"instrument_token": int(instrument_token), "last_price": Decimal(str(price))}
        for price in prices
    ]
