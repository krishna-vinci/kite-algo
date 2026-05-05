from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import pytest

from journaling.analytics_service import AnalyticsService
from journaling.models import (
    CapitalBasisType,
    ExecutionMode,
    JournalEnvironmentMode,
    JournalEpisode,
    JournalEpisodeStatus,
    JournalExecutionContext,
    JournalExecutionEnvironment,
    JournalExecutionFact,
    JournalRun,
    JournalRunStatus,
    JournalStrategyTemplate,
    MetricPeriod,
    ReviewState,
    SourceType,
    StrategyFamily,
)
from journaling.repository import JournalRepository


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class _FakeRepository:
    def __init__(self) -> None:
        self.environments = {
            "env-paper": JournalExecutionEnvironment(
                id="env-paper",
                mode=JournalEnvironmentMode.PAPER,
                account_scope="acct-paper",
                display_name="Paper",
                metadata={"starting_equity": "1000"},
            ),
            "env-live": JournalExecutionEnvironment(
                id="env-live",
                mode=JournalEnvironmentMode.LIVE,
                account_scope="acct-live",
                display_name="Live",
                metadata={"starting_equity": "1200"},
            ),
            "env-live-bad": JournalExecutionEnvironment(
                id="env-live-bad",
                mode=JournalEnvironmentMode.LIVE,
                account_scope="acct-live-bad",
                display_name="Live Bad",
                metadata={},
            ),
            "env-unknown-start": JournalExecutionEnvironment(
                id="env-unknown-start",
                mode=JournalEnvironmentMode.PAPER,
                account_scope="acct-unknown",
                display_name="Unknown Start",
                metadata={},
            ),
        }
        self.templates = {
            "tmpl-a": JournalStrategyTemplate(id="tmpl-a", strategy_family="indicator_strategy", template_key="tmpl-a", display_name="Alpha"),
            "tmpl-b": JournalStrategyTemplate(id="tmpl-b", strategy_family="options_strategy", template_key="tmpl-b", display_name="Beta"),
        }
        self.contexts = {
            "ctx-a": JournalExecutionContext(id="ctx-a", environment_id="env-paper", source_system="algo", external_run_id="run-a", strategy_template_id="tmpl-a"),
            "ctx-b": JournalExecutionContext(id="ctx-b", environment_id="env-paper", source_system="algo", external_run_id="run-b", strategy_template_id="tmpl-b"),
            "ctx-live-a": JournalExecutionContext(id="ctx-live-a", environment_id="env-live", source_system="algo", external_run_id="run-live-a", strategy_template_id="tmpl-a"),
            "ctx-unknown": JournalExecutionContext(id="ctx-unknown", environment_id="env-unknown-start", source_system="algo", external_run_id="run-unknown", metadata={}),
        }
        self.episodes = [
            JournalEpisode(id="ep-prev", environment_id="env-paper", execution_context_id="ctx-a", episode_seq=1, status=JournalEpisodeStatus.CLOSED, opened_at=_dt("2026-05-03T10:00:00+00:00"), closed_at=_dt("2026-05-03T12:00:00+00:00")),
            JournalEpisode(id="ep-ist-day", environment_id="env-paper", execution_context_id="ctx-a", episode_seq=2, status=JournalEpisodeStatus.CLOSED, opened_at=_dt("2026-05-03T17:00:00+00:00"), closed_at=_dt("2026-05-03T18:45:00+00:00")),
            JournalEpisode(id="ep-week-loss", environment_id="env-paper", execution_context_id="ctx-b", episode_seq=3, status=JournalEpisodeStatus.CLOSED, opened_at=_dt("2026-05-05T09:30:00+00:00"), closed_at=_dt("2026-05-05T10:30:00+00:00")),
            JournalEpisode(id="ep-live-a", environment_id="env-live", execution_context_id="ctx-live-a", episode_seq=1, status=JournalEpisodeStatus.CLOSED, opened_at=_dt("2026-05-03T17:00:00+00:00"), closed_at=_dt("2026-05-03T18:45:00+00:00")),
            JournalEpisode(id="ep-unknown", environment_id="env-unknown-start", execution_context_id="ctx-unknown", episode_seq=1, status=JournalEpisodeStatus.CLOSED, opened_at=_dt("2026-05-06T09:30:00+00:00"), closed_at=_dt("2026-05-06T10:00:00+00:00")),
        ]
        self.facts = {
            "ep-prev": [
                JournalExecutionFact(run_id="run-1", environment_id="env-paper", episode_id="ep-prev", source_type=SourceType.LIVE_FILL, source_fact_key="fact-prev", side="SELL", quantity=1, price=Decimal("100"), gross_cash_flow=Decimal("50"), brokerage=Decimal("1"), exchange_txn_charge=Decimal("1"), stt=Decimal("1"), stamp_duty=Decimal("0.5"), sebi_charge=Decimal("0.25"), gst=Decimal("0.25"), margin_required=Decimal("500"), fill_timestamp=_dt("2026-05-03T12:00:00+00:00")),
            ],
            "ep-ist-day": [
                JournalExecutionFact(run_id="run-2", environment_id="env-paper", episode_id="ep-ist-day", source_type=SourceType.LIVE_FILL, source_fact_key="fact-ist-day", side="SELL", quantity=1, price=Decimal("100"), gross_cash_flow=Decimal("20"), brokerage=Decimal("2"), exchange_txn_charge=Decimal("1"), stt=Decimal("1"), stamp_duty=Decimal("1"), sebi_charge=Decimal("0.5"), gst=Decimal("0.5"), margin_required=Decimal("250"), fill_timestamp=_dt("2026-05-03T18:45:00+00:00")),
            ],
            "ep-week-loss": [
                JournalExecutionFact(run_id="run-3", environment_id="env-paper", episode_id="ep-week-loss", source_type=SourceType.LIVE_FILL, source_fact_key="fact-week-loss", side="BUY", quantity=1, price=Decimal("100"), gross_cash_flow=Decimal("-10"), brokerage=Decimal("1"), exchange_txn_charge=Decimal("1"), stt=Decimal("0.5"), stamp_duty=Decimal("0.5"), sebi_charge=Decimal("0.25"), gst=Decimal("0.25"), margin_required=Decimal("999"), fill_timestamp=_dt("2026-05-05T10:30:00+00:00")),
            ],
            "ep-live-a": [
                JournalExecutionFact(run_id="run-4", environment_id="env-live", episode_id="ep-live-a", source_type=SourceType.LIVE_FILL, source_fact_key="fact-live-a", side="SELL", quantity=1, price=Decimal("100"), gross_cash_flow=Decimal("8"), brokerage=Decimal("1"), exchange_txn_charge=Decimal("1"), stt=Decimal("0.5"), stamp_duty=Decimal("0.5"), sebi_charge=Decimal("0.25"), gst=Decimal("0.25"), fill_timestamp=_dt("2026-05-03T18:45:00+00:00")),
            ],
            "ep-unknown": [
                JournalExecutionFact(run_id="run-5", environment_id="env-unknown-start", episode_id="ep-unknown", source_type=SourceType.LIVE_FILL, source_fact_key="fact-unknown", side="SELL", quantity=1, price=Decimal("100"), gross_cash_flow=Decimal("5"), brokerage=Decimal("1"), exchange_txn_charge=Decimal("0"), stt=Decimal("0"), stamp_duty=Decimal("0"), sebi_charge=Decimal("0"), gst=Decimal("0"), fill_timestamp=_dt("2026-05-06T10:00:00+00:00")),
            ],
        }
        self.runs = [
            JournalRun(id="jr-live-bad", strategy_family=StrategyFamily.INDICATOR, execution_mode=ExecutionMode.LIVE, account_ref="acct-live-bad", status=JournalRunStatus.CLOSED, capital_basis_type=CapitalBasisType.MARGIN_USED, capital_committed=Decimal("500"), review_state=ReviewState.PENDING),
        ]

    def get_execution_environment(self, environment_id: str):
        return self.environments.get(environment_id)

    def list_episodes(self, *, environment_id: str | None = None, execution_context_id: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0):
        rows = self.episodes
        if environment_id is not None:
            rows = [episode for episode in rows if episode.environment_id == environment_id]
        if execution_context_id is not None:
            rows = [episode for episode in rows if episode.execution_context_id == execution_context_id]
        if status is not None:
            rows = [episode for episode in rows if str(episode.status) == status]
        return rows[offset : offset + limit]

    def list_execution_facts_for_episodes(self, episode_ids: list[str]):
        return {episode_id: list(self.facts.get(episode_id, [])) for episode_id in episode_ids}

    def get_execution_context(self, context_id: str):
        return self.contexts.get(context_id)

    def get_strategy_template(self, template_id: str):
        return self.templates.get(template_id)

    def list_runs(self, *, execution_mode: str | None = None, limit: int = 100, **_: object):
        rows = self.runs
        if execution_mode is not None:
            rows = [run for run in rows if str(run.execution_mode) == execution_mode]
        return rows[:limit]


@pytest.fixture
def service() -> AnalyticsService:
    return AnalyticsService(repository=cast(JournalRepository, _FakeRepository()))


def test_compute_analytics_summary_uses_ist_closed_day_semantics(service: AnalyticsService) -> None:
    response = service.compute_analytics_summary(environment_id="env-paper", period=MetricPeriod.DAY, anchor_date=datetime(2026, 5, 4).date())

    assert response.metrics.closed_episode_count == 1
    assert response.metrics.net_pnl == Decimal("14")
    assert response.metrics.total_charges == Decimal("6")
    assert response.strategies[0].strategy.display_name == "Alpha"
    assert response.strategies[0].metrics.closed_episode_count == 1


def test_compute_equity_curve_carries_forward_prior_equity(service: AnalyticsService) -> None:
    response = service.compute_equity_curve(environment_id="env-paper", period=MetricPeriod.WEEK, anchor_date=datetime(2026, 5, 6).date())

    assert [point.trading_date.isoformat() for point in response.points] == [
        "2026-05-04",
        "2026-05-05",
        "2026-05-06",
        "2026-05-07",
        "2026-05-08",
        "2026-05-09",
        "2026-05-10",
    ]
    assert response.points[0].starting_equity == Decimal("1046")
    assert response.points[0].ending_equity == Decimal("1060")
    assert response.points[1].starting_equity == Decimal("1060")
    assert response.points[1].ending_equity == Decimal("1046.5")
    assert response.points[2].ending_equity == Decimal("1046.5")


def test_compute_cost_analysis_aggregates_itemized_costs_without_margin(service: AnalyticsService) -> None:
    response = service.compute_cost_analysis(environment_id="env-paper", period=MetricPeriod.WEEK, anchor_date=datetime(2026, 5, 6).date())

    assert response.cost_breakdown.total_charges == Decimal("9.5")
    assert [item.strategy.display_name for item in response.strategies] == ["Alpha", "Beta"]
    assert response.strategies[0].total_charges == Decimal("6")
    assert response.strategies[1].total_charges == Decimal("3.5")


def test_compute_paper_live_comparison_returns_delta_and_deviation(service: AnalyticsService) -> None:
    response = service.compute_paper_live_comparison(
        template_id="tmpl-a",
        paper_environment_id="env-paper",
        live_environment_id="env-live",
        period=MetricPeriod.DAY,
        anchor_date=datetime(2026, 5, 4).date(),
    )

    assert response.paper.net_pnl == Decimal("14")
    assert response.live.net_pnl == Decimal("4.5")
    assert response.delta["net_pnl"].delta == Decimal("9.5")
    assert response.delta["net_pnl"].deviation_pct == Decimal("67.85714285714285714285714286")


def test_compute_paper_live_comparison_rejects_invalid_environment_modes(service: AnalyticsService) -> None:
    with pytest.raises(ValueError, match="paper_environment_id must reference a paper environment"):
        service.compute_paper_live_comparison(
            template_id="tmpl-a",
            paper_environment_id="env-live",
            live_environment_id="env-live",
        )


def test_compute_equity_curve_keeps_points_when_starting_equity_unknown(service: AnalyticsService) -> None:
    response = service.compute_equity_curve(environment_id="env-unknown-start", period=MetricPeriod.MONTH, anchor_date=datetime(2026, 5, 6).date())

    matching = next(point for point in response.points if point.trading_date.isoformat() == "2026-05-06")
    assert matching.realized_pnl == Decimal("5")
    assert matching.starting_equity is None
    assert response.metrics.cumulative_return is None
    assert response.metrics.sharpe_ratio is None
