from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from backend.journaling.metrics import (
    average_loss,
    average_win,
    cumulative_return,
    expectancy,
    max_drawdown_duration,
    max_drawdown_from_equity_points,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    streaks,
    win_rate,
)
from backend.journaling.models import (
    AnalyticsMetrics,
    AnalyticsStrategySummaryItem,
    AnalyticsSummaryResponse,
    ComparisonMetricDelta,
    CostAnalysisResponse,
    CostBreakdown,
    EquityCurvePointResponse,
    EquityCurveResponse,
    JournalEnvironmentRef,
    JournalEnvironmentMode,
    JournalExecutionEnvironment,
    JournalV2StrategyRef,
    MetricPeriod,
    PaperLiveComparisonResponse,
    StrategyCostAnalysisItem,
    StrategyDeepDiveResponse,
)
from backend.journaling.periods import IST, period_bounds_utc
from backend.journaling.repository import JournalRepository
from backend.journaling.v2.metrics import build_episode_outcome


ZERO = Decimal("0")
UNKNOWN_STRATEGY_DISPLAY_NAME = "Unknown Strategy"


def _to_decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return default
    return Decimal(str(value))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class _EpisodeBundle:
    episode: Any
    context: Any | None
    strategy: JournalV2StrategyRef
    outcome: Any
    closed_at: datetime
    closed_date_ist: date


class AnalyticsService:
    def __init__(self, repository: JournalRepository | None = None) -> None:
        self.repository = repository or JournalRepository()

    def compute_analytics_summary(
        self,
        *,
        environment_id: str,
        period: MetricPeriod,
        anchor_date: date | None = None,
    ) -> AnalyticsSummaryResponse:
        environment = self._require_environment(environment_id)
        normalized_period, resolved_anchor, _from_date, _to_date, start_at, end_at = self._resolve_period(period, anchor_date)
        bundles = self._load_episode_bundles(environment_id=environment_id)
        period_bundles = self._filter_bundles_by_window(bundles, start_at=start_at, end_at=end_at)
        metrics = self._metrics_for_bundles(
            period_bundles,
            starting_equity=self._resolve_starting_equity(environment),
            from_date=_from_date,
            to_date=_to_date,
            prior_bundles=bundles,
        )

        grouped: dict[str, dict[str, Any]] = {}
        for bundle in period_bundles:
            key = str(bundle.strategy.template_id)
            entry = grouped.setdefault(key, {"strategy": bundle.strategy, "bundles": []})
            entry["bundles"].append(bundle)

        strategies = [
            AnalyticsStrategySummaryItem(
                strategy=entry["strategy"],
                metrics=self._metrics_for_bundles(entry["bundles"]),
            )
            for entry in grouped.values()
        ]
        strategies.sort(key=lambda item: (item.strategy.display_name or "").lower())

        return AnalyticsSummaryResponse(
            environment=self._to_environment_ref(environment),
            period=normalized_period,
            anchor_date=resolved_anchor,
            metrics=metrics,
            strategies=strategies,
        )

    def compute_strategy_deep_dive(
        self,
        *,
        environment_id: str,
        template_id: str,
        period: MetricPeriod,
        anchor_date: date | None = None,
    ) -> StrategyDeepDiveResponse:
        environment = self._require_environment(environment_id)
        normalized_template_id = self._require_nonblank("template_id", template_id)
        normalized_period, resolved_anchor, from_date, to_date, start_at, end_at = self._resolve_period(period, anchor_date)
        all_bundles = self._load_episode_bundles(environment_id=environment_id, template_id=normalized_template_id)
        period_bundles = self._filter_bundles_by_window(all_bundles, start_at=start_at, end_at=end_at)
        strategy = self._resolve_requested_strategy_ref(template_id=normalized_template_id, bundles=period_bundles or all_bundles)
        points = self._build_equity_curve_points(
            all_bundles,
            from_date=from_date,
            to_date=to_date,
            starting_equity=self._resolve_starting_equity(environment),
        )
        metrics = self._build_metrics(
            [bundle.outcome for bundle in period_bundles],
            equity_points=self._metric_points_from_curve(points),
        )
        return StrategyDeepDiveResponse(
            environment=self._to_environment_ref(environment),
            period=normalized_period,
            anchor_date=resolved_anchor,
            strategy=strategy,
            metrics=metrics,
            equity_curve=points,
        )

    def compute_equity_curve(
        self,
        *,
        environment_id: str,
        period: MetricPeriod,
        anchor_date: date | None = None,
        template_id: str | None = None,
    ) -> EquityCurveResponse:
        environment = self._require_environment(environment_id)
        normalized_period, resolved_anchor, from_date, to_date, start_at, end_at = self._resolve_period(period, anchor_date)
        all_bundles = self._load_episode_bundles(environment_id=environment_id, template_id=template_id)
        period_bundles = self._filter_bundles_by_window(all_bundles, start_at=start_at, end_at=end_at)
        points = self._build_equity_curve_points(
            all_bundles,
            from_date=from_date,
            to_date=to_date,
            starting_equity=self._resolve_starting_equity(environment),
        )
        metrics = self._build_metrics(
            [bundle.outcome for bundle in period_bundles],
            equity_points=self._metric_points_from_curve(points),
        )
        return EquityCurveResponse(
            environment=self._to_environment_ref(environment),
            period=normalized_period,
            anchor_date=resolved_anchor,
            template_id=str(template_id).strip() or None,
            metrics=metrics,
            points=points,
        )

    def compute_cost_analysis(
        self,
        *,
        environment_id: str,
        period: MetricPeriod,
        anchor_date: date | None = None,
    ) -> CostAnalysisResponse:
        environment = self._require_environment(environment_id)
        normalized_period, resolved_anchor, from_date, to_date, start_at, end_at = self._resolve_period(period, anchor_date)
        bundles = self._load_episode_bundles(environment_id=environment_id)
        period_bundles = self._filter_bundles_by_window(bundles, start_at=start_at, end_at=end_at)
        metrics = self._metrics_for_bundles(
            period_bundles,
            starting_equity=self._resolve_starting_equity(environment),
            from_date=from_date,
            to_date=to_date,
            prior_bundles=bundles,
        )

        grouped: dict[str, dict[str, Any]] = {}
        for bundle in period_bundles:
            key = str(bundle.strategy.template_id)
            entry = grouped.setdefault(key, {"strategy": bundle.strategy, "bundles": []})
            entry["bundles"].append(bundle)

        strategies = [
            StrategyCostAnalysisItem(
                strategy=entry["strategy"],
                cost_breakdown=self._sum_cost_breakdowns(bundle.outcome.cost_breakdown for bundle in entry["bundles"]),
                total_charges=sum((_to_decimal(bundle.outcome.total_charges) for bundle in entry["bundles"]), ZERO),
                cost_ratio=self._cost_ratio(entry["bundles"]),
                closed_episode_count=len(entry["bundles"]),
            )
            for entry in grouped.values()
        ]
        strategies.sort(key=lambda item: item.total_charges, reverse=True)

        return CostAnalysisResponse(
            environment=self._to_environment_ref(environment),
            period=normalized_period,
            anchor_date=resolved_anchor,
            metrics=metrics,
            cost_breakdown=metrics.cost_breakdown,
            strategies=strategies,
        )

    def compute_paper_live_comparison(
        self,
        *,
        template_id: str,
        paper_environment_id: str,
        live_environment_id: str,
        period: MetricPeriod = MetricPeriod.SINCE_INCEPTION,
        anchor_date: date | None = None,
    ) -> PaperLiveComparisonResponse:
        normalized_template_id = self._require_nonblank("template_id", template_id)
        paper_environment = self._require_environment(paper_environment_id)
        live_environment = self._require_environment(live_environment_id)
        if str(_enum_value(paper_environment.mode)) != "paper":
            raise ValueError("paper_environment_id must reference a paper environment")
        if str(_enum_value(live_environment.mode)) != "live":
            raise ValueError("live_environment_id must reference a live environment")

        normalized_period, resolved_anchor, from_date, to_date, start_at, end_at = self._resolve_period(period, anchor_date)
        paper_bundles = self._load_episode_bundles(environment_id=paper_environment_id, template_id=normalized_template_id)
        live_bundles = self._load_episode_bundles(environment_id=live_environment_id, template_id=normalized_template_id)
        paper_period_bundles = self._filter_bundles_by_window(paper_bundles, start_at=start_at, end_at=end_at)
        live_period_bundles = self._filter_bundles_by_window(live_bundles, start_at=start_at, end_at=end_at)

        paper_metrics = self._metrics_for_bundles(
            paper_period_bundles,
            starting_equity=self._resolve_starting_equity(paper_environment),
            from_date=from_date,
            to_date=to_date,
            prior_bundles=paper_bundles,
        )
        live_metrics = self._metrics_for_bundles(
            live_period_bundles,
            starting_equity=self._resolve_starting_equity(live_environment),
            from_date=from_date,
            to_date=to_date,
            prior_bundles=live_bundles,
        )

        return PaperLiveComparisonResponse(
            template_id=normalized_template_id,
            period=normalized_period,
            anchor_date=resolved_anchor,
            paper_environment=self._to_environment_ref(paper_environment),
            live_environment=self._to_environment_ref(live_environment),
            paper=paper_metrics,
            live=live_metrics,
            delta=self._build_comparison_delta(paper_metrics, live_metrics),
            combined=None,
        )

    def _require_environment(self, environment_id: str) -> JournalExecutionEnvironment:
        normalized_environment_id = self._require_nonblank("environment_id", environment_id)
        environment = self.repository.get_execution_environment(normalized_environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
        return environment

    def _resolve_period(
        self,
        period: MetricPeriod,
        anchor_date: date | None,
    ) -> tuple[MetricPeriod, date | None, date | None, date | None, datetime | None, datetime | None]:
        normalized_period = MetricPeriod(str(_enum_value(period) or MetricPeriod.SINCE_INCEPTION.value).strip().lower())
        resolved_anchor = anchor_date
        if normalized_period != MetricPeriod.SINCE_INCEPTION and resolved_anchor is None:
            resolved_anchor = datetime.now(IST).date()
        from_date, to_date, start_at, end_at = period_bounds_utc(normalized_period.value, resolved_anchor)
        return normalized_period, resolved_anchor, from_date, to_date, start_at, end_at

    def _require_nonblank(self, name: str, value: str | None) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError(f"{name} is required")
        return cleaned

    def _to_environment_ref(self, environment: Any) -> JournalEnvironmentRef:
        mode_value = getattr(environment, "mode", None)
        if not isinstance(mode_value, JournalEnvironmentMode):
            mode_value = JournalEnvironmentMode(str(_enum_value(mode_value) or JournalEnvironmentMode.PAPER.value))
        return JournalEnvironmentRef(
            environment_id=str(environment.id),
            mode=mode_value,
            account_scope=str(environment.account_scope),
            display_name=getattr(environment, "display_name", None),
            broker_user_id=getattr(environment, "broker_user_id", None),
            paper_account_key=getattr(environment, "paper_account_key", None),
        )

    def _context_template_id(self, context: Any | None) -> str | None:
        if context is None:
            return None
        metadata = dict(getattr(context, "metadata", {}) or {})
        resolved_identity = dict(metadata.get("resolved_identity") or {})
        return (
            str(getattr(context, "strategy_template_id", None) or "").strip()
            or str(metadata.get("strategy_template_id") or "").strip()
            or str(resolved_identity.get("template_id") or "").strip()
            or None
        )

    def _unknown_strategy_ref(self, *, template_id: str | None = None, context: Any | None = None, episode: Any | None = None) -> JournalV2StrategyRef:
        fallback_id = (
            str(template_id or "").strip()
            or str(getattr(context, "id", None) or "").strip()
            or str(getattr(episode, "execution_context_id", None) or "").strip()
            or "unknown_strategy"
        )
        return JournalV2StrategyRef(
            template_id=fallback_id,
            strategy_family="unknown_strategy",
            template_key=fallback_id,
            display_name=UNKNOWN_STRATEGY_DISPLAY_NAME,
        )

    def _resolve_strategy_ref(
        self,
        *,
        context: Any | None,
        episode: Any | None = None,
        template_id: str | None = None,
    ) -> JournalV2StrategyRef:
        resolved_template_id = self._context_template_id(context) or str(template_id or "").strip() or None
        metadata = dict(getattr(context, "metadata", {}) or {}) if context is not None else {}
        resolved_identity = dict(metadata.get("resolved_identity") or {})
        if resolved_template_id:
            template = self.repository.get_strategy_template(resolved_template_id)
            if template is not None:
                return JournalV2StrategyRef(
                    template_id=str(template.id),
                    strategy_family=str(getattr(template, "strategy_family", "unknown_strategy")),
                    template_key=getattr(template, "template_key", None),
                    display_name=getattr(template, "display_name", None) or getattr(template, "template_key", None) or str(template.id),
                )
            if resolved_identity:
                return JournalV2StrategyRef(
                    template_id=resolved_template_id,
                    strategy_family=str(resolved_identity.get("strategy_family") or metadata.get("strategy_family") or "unknown_strategy"),
                    template_key=str(resolved_identity.get("template_id") or resolved_template_id),
                    display_name=str(resolved_identity.get("display_name") or metadata.get("display_name") or UNKNOWN_STRATEGY_DISPLAY_NAME),
                )
        return self._unknown_strategy_ref(template_id=resolved_template_id, context=context, episode=episode)

    def _load_episode_bundles(self, *, environment_id: str, template_id: str | None = None) -> list[_EpisodeBundle]:
        episodes = self.repository.list_episodes(environment_id=environment_id, limit=5000)
        closed_episodes = [episode for episode in episodes if str(_enum_value(getattr(episode, "status", ""))) == "closed"]
        closed_episodes.sort(key=lambda episode: self._closed_timestamp(episode) or datetime.min.replace(tzinfo=timezone.utc))

        context_cache: dict[str, Any | None] = {}
        selected: list[tuple[Any, Any | None, JournalV2StrategyRef, datetime, date]] = []
        normalized_template_id = str(template_id or "").strip() or None
        for episode in closed_episodes:
            closed_at = self._closed_timestamp(episode)
            if closed_at is None:
                continue
            context_id = str(getattr(episode, "execution_context_id", "") or "")
            if context_id and context_id not in context_cache:
                context_cache[context_id] = self.repository.get_execution_context(context_id)
            context = context_cache.get(context_id)
            strategy = self._resolve_strategy_ref(context=context, episode=episode, template_id=normalized_template_id)
            if normalized_template_id and str(strategy.template_id) != normalized_template_id and str(strategy.template_key or "") != normalized_template_id:
                continue
            selected.append((episode, context, strategy, closed_at, closed_at.astimezone(IST).date()))

        facts_by_episode = self.repository.list_execution_facts_for_episodes([str(episode.id) for episode, *_ in selected]) if selected else {}
        bundles: list[_EpisodeBundle] = []
        for episode, context, strategy, closed_at, closed_date_ist in selected:
            facts = facts_by_episode.get(str(episode.id), [])
            bundles.append(
                _EpisodeBundle(
                    episode=episode,
                    context=context,
                    strategy=strategy,
                    outcome=build_episode_outcome(episode=episode, facts=facts),
                    closed_at=closed_at,
                    closed_date_ist=closed_date_ist,
                )
            )
        return bundles

    def _filter_bundles_by_window(
        self,
        bundles: Iterable[_EpisodeBundle],
        *,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> list[_EpisodeBundle]:
        filtered: list[_EpisodeBundle] = []
        for bundle in bundles:
            if start_at is not None and bundle.closed_at < start_at:
                continue
            if end_at is not None and bundle.closed_at >= end_at:
                continue
            filtered.append(bundle)
        return filtered

    def _closed_timestamp(self, episode: Any) -> datetime | None:
        return _coerce_utc(getattr(episode, "closed_at", None) or getattr(episode, "opened_at", None))

    def _sum_cost_breakdowns(self, breakdowns: Iterable[CostBreakdown]) -> CostBreakdown:
        items = list(breakdowns)
        return CostBreakdown(
            brokerage=sum((_to_decimal(item.brokerage) for item in items), ZERO),
            exchange_txn_charge=sum((_to_decimal(item.exchange_txn_charge) for item in items), ZERO),
            stt=sum((_to_decimal(item.stt) for item in items), ZERO),
            stamp_duty=sum((_to_decimal(item.stamp_duty) for item in items), ZERO),
            sebi_charge=sum((_to_decimal(item.sebi_charge) for item in items), ZERO),
            gst=sum((_to_decimal(item.gst) for item in items), ZERO),
        )

    def _build_metrics(
        self,
        outcomes: Iterable[Any],
        *,
        equity_points: list[Any] | None = None,
    ) -> AnalyticsMetrics:
        rows = list(outcomes)
        pnls = [item.net_pnl for item in rows]
        gross_pnl = sum((_to_decimal(item.gross_pnl) for item in rows), ZERO)
        total_charges = sum((_to_decimal(item.total_charges) for item in rows), ZERO)
        net_pnl = sum((_to_decimal(item.net_pnl) for item in rows), ZERO)
        realized_pnl = sum((_to_decimal(item.realized_pnl) for item in rows), ZERO)
        hold_seconds_total = sum((int(item.hold_seconds) for item in rows), 0)
        closed_episode_count = len(rows)
        win_count = sum(1 for item in rows if _to_decimal(item.net_pnl) > ZERO)
        loss_count = sum(1 for item in rows if _to_decimal(item.net_pnl) < ZERO)
        streak = streaks(pnls) if pnls else {"max_win_streak": 0, "max_loss_streak": 0}
        metric_points = equity_points or []
        has_equity = any(getattr(point, "ending_equity", None) is not None for point in metric_points)
        return AnalyticsMetrics(
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_charges=total_charges,
            realized_pnl=realized_pnl,
            cost_breakdown=self._sum_cost_breakdowns(item.cost_breakdown for item in rows),
            cost_ratio=(total_charges / gross_pnl) if gross_pnl != ZERO else None,
            closed_episode_count=closed_episode_count,
            hold_seconds_total=hold_seconds_total,
            hold_seconds_avg=int(hold_seconds_total / closed_episode_count) if closed_episode_count else None,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=win_rate(pnls),
            average_win=average_win(pnls),
            average_loss=average_loss(pnls),
            expectancy=expectancy(pnls),
            profit_factor=profit_factor(pnls),
            sharpe_ratio=sharpe_ratio(metric_points) if has_equity else None,
            sortino_ratio=sortino_ratio(metric_points) if has_equity else None,
            max_drawdown=max_drawdown_from_equity_points(metric_points) if has_equity else None,
            max_drawdown_duration_days=max_drawdown_duration(metric_points) if has_equity else None,
            cumulative_return=cumulative_return(metric_points) if has_equity else None,
            max_win_streak=int(streak.get("max_win_streak") or 0),
            max_loss_streak=int(streak.get("max_loss_streak") or 0),
            mae=None,
            mfe=None,
            r_multiple=None,
        )

    def _build_equity_curve_points(
        self,
        bundles: Iterable[_EpisodeBundle],
        *,
        from_date: date | None,
        to_date: date | None,
        starting_equity: Decimal | None,
    ) -> list[EquityCurvePointResponse]:
        daily: dict[date, dict[str, Decimal]] = {}
        for bundle in bundles:
            entry = daily.setdefault(bundle.closed_date_ist, {"realized_pnl": ZERO, "total_charges": ZERO})
            entry["realized_pnl"] += _to_decimal(bundle.outcome.realized_pnl)
            entry["total_charges"] += _to_decimal(bundle.outcome.total_charges)

        if from_date is None or to_date is None:
            if not daily:
                return []
            from_date = min(daily)
            to_date = max(daily)

        prior_net = sum(
            ((values["realized_pnl"] - values["total_charges"]) for day, values in daily.items() if day < from_date),
            ZERO,
        )
        current_equity = (starting_equity + prior_net) if starting_equity is not None else None
        points: list[EquityCurvePointResponse] = []
        cursor = from_date
        while cursor <= to_date:
            values = daily.get(cursor, {"realized_pnl": ZERO, "total_charges": ZERO})
            realized_pnl = values["realized_pnl"]
            total_charges = values["total_charges"]
            if current_equity is None:
                point = EquityCurvePointResponse(
                    trading_date=cursor,
                    realized_pnl=realized_pnl,
                    total_charges=total_charges,
                    starting_equity=None,
                    ending_equity=None,
                    return_pct=None,
                    benchmark_return_pct=None,
                    excess_return_pct=None,
                )
            else:
                start_value = current_equity
                end_value = start_value + realized_pnl - total_charges
                point = EquityCurvePointResponse(
                    trading_date=cursor,
                    realized_pnl=realized_pnl,
                    total_charges=total_charges,
                    starting_equity=start_value,
                    ending_equity=end_value,
                    return_pct=((end_value - start_value) / start_value) if start_value != ZERO else None,
                    benchmark_return_pct=None,
                    excess_return_pct=None,
                )
                current_equity = end_value
            points.append(point)
            cursor += timedelta(days=1)
        return points

    def _metric_points_from_curve(self, points: Iterable[EquityCurvePointResponse]) -> list[Any]:
        metric_points: list[Any] = []
        for point in points:
            metric_points.append(
                SimpleNamespace(
                    as_of=datetime.combine(point.trading_date, datetime.min.time(), tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc),
                    starting_equity=point.starting_equity,
                    ending_equity=point.ending_equity,
                    return_pct=point.return_pct,
                )
            )
        return metric_points

    def _resolve_starting_equity(self, environment: Any) -> Decimal | None:
        metadata = dict(getattr(environment, "metadata", {}) or {})
        if metadata.get("starting_equity") not in (None, ""):
            return _to_decimal(metadata.get("starting_equity"))

        execution_mode = self._environment_to_execution_mode(environment)
        runs = self.repository.list_runs(execution_mode=execution_mode, limit=5000)
        seen: set[str] = set()
        total = ZERO
        found = False
        for run in runs:
            run_id = str(getattr(run, "id", "") or "")
            if run_id and run_id in seen:
                continue
            if str(getattr(run, "account_ref", "") or "") != str(getattr(environment, "account_scope", "") or ""):
                continue
            capital_committed = getattr(run, "capital_committed", None)
            if capital_committed in (None, ""):
                continue
            if run_id:
                seen.add(run_id)
            total += _to_decimal(capital_committed)
            found = True
        return total if found else None

    def _environment_to_execution_mode(self, environment: Any) -> str | None:
        mode = str(_enum_value(getattr(environment, "mode", None)) or "").strip()
        if mode == "dry_run_preview":
            return "dry_run"
        return mode or None

    def _metrics_for_bundles(
        self,
        bundles: list[_EpisodeBundle],
        *,
        starting_equity: Decimal | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        prior_bundles: list[_EpisodeBundle] | None = None,
    ) -> AnalyticsMetrics:
        equity_points: list[Any] | None = None
        if starting_equity is not None or from_date is not None or to_date is not None:
            all_bundles = prior_bundles if prior_bundles is not None else bundles
            curve = self._build_equity_curve_points(
                all_bundles,
                from_date=from_date,
                to_date=to_date,
                starting_equity=starting_equity,
            )
            equity_points = self._metric_points_from_curve(curve)
        return self._build_metrics([bundle.outcome for bundle in bundles], equity_points=equity_points)

    def _cost_ratio(self, bundles: list[_EpisodeBundle]) -> Decimal | None:
        gross_pnl = sum((_to_decimal(bundle.outcome.gross_pnl) for bundle in bundles), ZERO)
        total_charges = sum((_to_decimal(bundle.outcome.total_charges) for bundle in bundles), ZERO)
        if gross_pnl == ZERO:
            return None
        return total_charges / gross_pnl

    def _resolve_requested_strategy_ref(
        self,
        *,
        template_id: str,
        bundles: list[_EpisodeBundle],
    ) -> JournalV2StrategyRef:
        if bundles:
            return bundles[0].strategy
        template = self.repository.get_strategy_template(template_id)
        if template is not None:
            return JournalV2StrategyRef(
                template_id=str(template.id),
                strategy_family=str(getattr(template, "strategy_family", "unknown_strategy")),
                template_key=getattr(template, "template_key", None),
                display_name=getattr(template, "display_name", None) or getattr(template, "template_key", None) or str(template.id),
            )
        return self._unknown_strategy_ref(template_id=template_id)

    def _build_comparison_delta(
        self,
        paper_metrics: AnalyticsMetrics,
        live_metrics: AnalyticsMetrics,
    ) -> dict[str, ComparisonMetricDelta]:
        field_names = [
            "gross_pnl",
            "net_pnl",
            "total_charges",
            "realized_pnl",
            "cost_ratio",
            "closed_episode_count",
            "hold_seconds_total",
            "hold_seconds_avg",
            "win_count",
            "loss_count",
            "win_rate",
            "average_win",
            "average_loss",
            "expectancy",
            "profit_factor",
            "sharpe_ratio",
            "sortino_ratio",
            "max_drawdown",
            "max_drawdown_duration_days",
            "cumulative_return",
            "max_win_streak",
            "max_loss_streak",
        ]
        result: dict[str, ComparisonMetricDelta] = {}
        for name in field_names:
            paper_raw = getattr(paper_metrics, name, None)
            live_raw = getattr(live_metrics, name, None)
            paper_value = None if paper_raw is None else _to_decimal(paper_raw)
            live_value = None if live_raw is None else _to_decimal(live_raw)
            delta = None if paper_value is None or live_value is None else paper_value - live_value
            deviation_pct = None
            if delta is not None and paper_value not in (None, ZERO):
                deviation_pct = (delta / abs(paper_value)) * Decimal("100")
            result[name] = ComparisonMetricDelta(
                paper=paper_value,
                live=live_value,
                delta=delta,
                deviation_pct=deviation_pct,
            )
        return result
