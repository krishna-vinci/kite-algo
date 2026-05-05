from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from journaling.models import CostBreakdown, EpisodeOutcome


ZERO = Decimal("0")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def build_episode_outcome(*, episode: Any, facts: Iterable[Any]) -> EpisodeOutcome:
    gross_pnl = ZERO
    legacy_total_charges = ZERO
    itemized = CostBreakdown()
    for fact in facts:
        gross_pnl += _to_decimal(getattr(fact, "gross_cash_flow", None))
        itemized = CostBreakdown(
            brokerage=itemized.brokerage + _to_decimal(getattr(fact, "brokerage", None)),
            exchange_txn_charge=itemized.exchange_txn_charge + _to_decimal(getattr(fact, "exchange_txn_charge", None)),
            stt=itemized.stt + _to_decimal(getattr(fact, "stt", None)),
            stamp_duty=itemized.stamp_duty + _to_decimal(getattr(fact, "stamp_duty", None)),
            sebi_charge=itemized.sebi_charge + _to_decimal(getattr(fact, "sebi_charge", None)),
            gst=itemized.gst + _to_decimal(getattr(fact, "gst", None)),
        )
        legacy_total_charges += (
            _to_decimal(getattr(fact, "fees_amount", None))
            + _to_decimal(getattr(fact, "taxes_amount", None))
            + _to_decimal(getattr(fact, "slippage_amount", None))
        )

    total_charges = itemized.total_charges if itemized.total_charges else legacy_total_charges
    if not itemized.total_charges and legacy_total_charges:
        itemized = CostBreakdown(total_charges=legacy_total_charges)

    opened_at = getattr(episode, "opened_at", None)
    closed_at = getattr(episode, "closed_at", None)
    hold_seconds = 0
    if isinstance(opened_at, datetime) and isinstance(closed_at, datetime):
        hold_seconds = max(0, int((closed_at - opened_at).total_seconds()))

    net_pnl = gross_pnl - total_charges
    return EpisodeOutcome(
        episode_id=str(getattr(episode, "id", "")),
        gross_pnl=gross_pnl,
        total_charges=total_charges,
        net_pnl=net_pnl,
        realized_pnl=gross_pnl,
        hold_seconds=hold_seconds,
        cost_breakdown=itemized,
    )


def build_environment_episode_metrics(outcomes: Iterable[EpisodeOutcome]) -> dict[str, Any]:
    rows = list(outcomes)
    closed_episode_count = len(rows)
    gross_pnl = sum((item.gross_pnl for item in rows), ZERO)
    total_charges = sum((item.total_charges for item in rows), ZERO)
    net_pnl = sum((item.net_pnl for item in rows), ZERO)
    realized_pnl = sum((item.realized_pnl for item in rows), ZERO)
    hold_seconds = sum((item.hold_seconds for item in rows), 0)
    cost_breakdown = CostBreakdown(
        brokerage=sum((item.cost_breakdown.brokerage for item in rows), ZERO),
        exchange_txn_charge=sum((item.cost_breakdown.exchange_txn_charge for item in rows), ZERO),
        stt=sum((item.cost_breakdown.stt for item in rows), ZERO),
        stamp_duty=sum((item.cost_breakdown.stamp_duty for item in rows), ZERO),
        sebi_charge=sum((item.cost_breakdown.sebi_charge for item in rows), ZERO),
        gst=sum((item.cost_breakdown.gst for item in rows), ZERO),
        total_taxes=sum((item.cost_breakdown.total_taxes for item in rows), ZERO),
        total_charges=total_charges,
    )

    winning = [item.net_pnl for item in rows if item.net_pnl > ZERO]
    losing = [item.net_pnl for item in rows if item.net_pnl < ZERO]
    win_count = len(winning)
    loss_count = len(losing)
    win_rate = (Decimal(win_count) / Decimal(closed_episode_count)) if closed_episode_count else None
    average_win = (sum(winning, ZERO) / Decimal(win_count)) if win_count else None
    average_loss = (abs(sum(losing, ZERO)) / Decimal(loss_count)) if loss_count else None

    expectancy = None
    if win_rate is not None and average_win is not None and average_loss is not None:
        expectancy = (win_rate * average_win) - ((Decimal("1") - win_rate) * average_loss)

    profit_factor = None
    gross_wins = sum(winning, ZERO)
    gross_losses = abs(sum(losing, ZERO))
    if gross_losses > ZERO:
        profit_factor = gross_wins / gross_losses

    return {
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "total_charges": total_charges,
        "realized_pnl": realized_pnl,
        "hold_seconds": hold_seconds,
        "cost_breakdown": cost_breakdown.model_dump(),
        "closed_episode_count": closed_episode_count,
        "win_rate": win_rate,
        "average_win": average_win,
        "average_loss": average_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "mae": None,
        "mfe": None,
        "r_multiple": None,
    }


@dataclass(slots=True)
class StrategyTemplateScorecard:
    template_id: str
    strategy_family: str
    display_name: str
    metrics: dict[str, Any]


def build_strategy_template_scorecards(rows: Iterable[dict[str, Any]]) -> list[StrategyTemplateScorecard]:
    scorecards: list[StrategyTemplateScorecard] = []
    for row in rows:
        scorecards.append(
            StrategyTemplateScorecard(
                template_id=str(row.get("template_id") or ""),
                strategy_family=str(row.get("strategy_family") or "unknown_strategy"),
                display_name=str(row.get("display_name") or row.get("template_key") or row.get("template_id") or "Unknown"),
                metrics=dict(row.get("metrics") or {}),
            )
        )
    return scorecards


def build_paper_live_comparison(*, template_id: str, paper_metrics: dict[str, Any], live_metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "template_id": template_id,
        "paper": paper_metrics,
        "live": live_metrics,
        "combined": None,
    }
