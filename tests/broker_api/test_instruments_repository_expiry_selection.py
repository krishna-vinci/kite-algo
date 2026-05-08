from __future__ import annotations

from datetime import date

from broker_api.instruments.instruments_repository import InstrumentsRepository


class _RepoStub(InstrumentsRepository):
    def __init__(self, expiries: list[date], grouped: dict[date, list[date]]):
        self._expiries = expiries
        self._grouped = grouped

    def get_expiries(self, underlying: str, today: date) -> list[date]:  # type: ignore[override]
        return self._expiries

    def get_expiries_grouped(self, underlying: str, today: date) -> dict[date, list[date]]:  # type: ignore[override]
        return self._grouped


def test_nifty_expiry_window_uses_three_weeklies_plus_two_monthlies():
    expiries = [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
        date(2026, 6, 2),
        date(2026, 6, 30),
        date(2026, 7, 28),
    ]
    repo = _RepoStub(
        expiries=expiries,
        grouped={
            date(2026, 5, 1): expiries[:4],
            date(2026, 6, 1): expiries[4:6],
            date(2026, 7, 1): expiries[6:],
        },
    )

    selected = repo.select_current_weeklies_plus_three_monthlies("NIFTY", today=date(2026, 5, 1))

    assert selected == [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
        date(2026, 6, 30),
    ]


def test_other_underlyings_keep_existing_weekly_monthly_window():
    expiries = [
        date(2026, 5, 7),
        date(2026, 5, 14),
        date(2026, 5, 21),
        date(2026, 5, 28),
        date(2026, 6, 4),
        date(2026, 6, 25),
        date(2026, 7, 30),
    ]
    repo = _RepoStub(
        expiries=expiries,
        grouped={
            date(2026, 5, 1): expiries[:4],
            date(2026, 6, 1): expiries[4:6],
            date(2026, 7, 1): expiries[6:],
        },
    )

    selected = repo.select_current_weeklies_plus_three_monthlies("FINNIFTY", today=date(2026, 5, 1))

    assert selected == expiries
