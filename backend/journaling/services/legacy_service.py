from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

from backend.execution_accounting.contracts import ChargesStatus, ExecutionCostContract, signed_cash_flow

from backend.journaling.benchmark import compare_return_series
from backend.journaling.metrics import (
    average_loss,
    average_win,
    cumulative_return,
    expectancy,
    gross_loss,
    gross_profit,
    max_drawdown_duration,
    max_drawdown_from_equity_points,
    net_pnl,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    streaks,
    total_fees,
    win_rate,
)
from backend.journaling.models import (
    AnalyticsMetrics,
    CapitalBasisType,
    BenchmarkDailyPrice,
    BenchmarkDefinition,
    CostBreakdown,
    EpisodeOutcome,
    ExecutionMode,
    JournalEnvironmentRef,
    JournalDecisionEvent,
    JournalEpisodeLegDirection,
    JournalEquityPoint,
    JournalExecutionFact,
    JournalMetricSnapshot,
    JournalRule,
    JournalRunStatus,
    JournalRun,
    JournalTimelineActorType,
    JournalTimelineEvent,
    JournalV2DailyResponse,
    JournalV2DailySummary,
    JournalV2EpisodeCard,
    JournalV2EpisodeDetailResponse,
    JournalV2EpisodeLegView,
    JournalV2ExecutionFillView,
    JournalV2OpenEpisodeCard,
    JournalV2PeriodBucket,
    JournalV2PeriodResponse,
    JournalV2StrategyGroup,
    JournalV2StrategyListResponse,
    JournalV2StrategyRef,
    JournalV2StrategySummaryItem,
    JournalV2TimelineEventView,
    MetricPeriod,
    ReviewState,
    JournalSourceLink,
    ProjectionState,
    SourceType,
    StrategyFamily,
)
from backend.journaling.periods import IST, day_bounds_utc, period_bounds_utc
from backend.journaling.repository import JournalRepository
from backend.journaling.v2.environment import resolve_environment_key
from backend.journaling.v2.episodes import classify_position_effect, next_episode_sequence
from backend.journaling.v2.identity import (
    is_low_confidence_resolution,
    resolve_strategy_identity,
    unresolved_reason_for_identity,
)
from backend.journaling.v2.metrics import (
    build_environment_episode_metrics,
    build_episode_outcome,
    build_paper_live_comparison,
    build_strategy_template_scorecards,
)
from backend.journaling.v2.notes import markdown_to_search_text


ZERO = Decimal("0")
DEFAULT_CALC_VERSION = "v1"
DEFAULT_V2_CALC_VERSION = "journal_v2_metrics_v1"
DEFAULT_BENCHMARK_IDS = ("NIFTY50",)
AGGREGATE_WINDOWS = ("day", "week", "month", "year", "since_inception")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _serialize_decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize_decimal(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_decimal(item) for key, item in value.items()}
    return value


def _normalize_cost_contract(value: ExecutionCostContract | Dict[str, Any] | None) -> ExecutionCostContract | None:
    if value is None:
        return None
    if isinstance(value, ExecutionCostContract):
        return value
    if isinstance(value, dict) and value:
        return ExecutionCostContract.model_validate(value)
    return ExecutionCostContract(charges_status=ChargesStatus.UNAVAILABLE)


def _normalize_interval_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    return None


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Optional[Decimal]:
    if denominator == ZERO:
        return None
    return numerator / denominator


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _window_start(now: datetime, window: str) -> Optional[datetime]:
    anchor = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if window == "day":
        return anchor - timedelta(days=1)
    if window == "week":
        return anchor - timedelta(days=7)
    if window == "month":
        return anchor - timedelta(days=30)
    if window == "year":
        return anchor - timedelta(days=365)
    if window == "since_inception":
        return None
    raise ValueError(f"Unsupported window: {window}")


def _normalize_period(period: str) -> str:
    normalized = str(period or "month").strip().lower()
    if normalized in {"all", "inception", "since_inception"}:
        return "since_inception"
    if normalized in AGGREGATE_WINDOWS:
        return normalized
    raise ValueError(f"Unsupported period: {period}")


def _option_strategy_journal_status(source_status: Optional[str]) -> str:
    normalized = str(source_status or "").strip().lower()
    if normalized in {"success", "completed", "closed", "done"}:
        return "closed"
    if normalized in {"failed", "cancelled", "canceled", "rejected", "aborted"}:
        return "cancelled"
    if normalized in {"partial", "planned", "queued", "running", "open", "active"}:
        return "open"
    return "open"


def _option_strategy_review_state(source_status: Optional[str], execution_result: Optional[Dict[str, Any]] = None) -> str:
    normalized = str(source_status or "").strip().lower()
    if normalized in {"success", "partial", "failed"}:
        return "pending"
    if normalized in {"cancelled", "canceled"}:
        return "waived"
    if execution_result and execution_result.get("review_state"):
        return str(execution_result["review_state"])
    return "pending"


def _safe_metrics(metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = {
        "execution_fact_count": 0,
        "trading_day_count": 0,
        "gross_profit": ZERO,
        "gross_loss": ZERO,
        "net_pnl": ZERO,
        "total_fees": ZERO,
        "win_rate": None,
        "average_win": None,
        "average_loss": None,
        "profit_factor": None,
        "expectancy": None,
        "cumulative_return": None,
        "max_drawdown": None,
        "max_drawdown_duration": 0,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "max_win_streak": 0,
        "max_loss_streak": 0,
        "ending_equity": None,
    }
    if metrics:
        base.update(metrics)
    return base


def _looks_like_uuid(value: str) -> bool:
    return bool(UUID_RE.match(str(value or "").strip()))


def _require_uuid(name: str, value: str | None) -> str:
    normalized = str(value or "").strip()
    if not _looks_like_uuid(normalized):
        raise ValueError(f"{name} must be a valid UUID")
    return normalized


class JournalService:
    def __init__(self, repository: Optional[JournalRepository] = None) -> None:
        self.repository = repository or JournalRepository()

    def resolve_v2_environment_id(
        self,
        *,
        environment_id: str | None = None,
        mode: str | None = None,
        account_scope: str | None = None,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int | None = None,
        create_if_missing: bool = True,
    ) -> str:
        normalized_environment_id = str(environment_id or "").strip()
        if normalized_environment_id:
            if not _looks_like_uuid(normalized_environment_id):
                raise ValueError("environment_id must be a valid UUID")
            environment = self.repository.get_execution_environment(normalized_environment_id)
            if environment is None:
                raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
            return str(environment.id)

        normalized_mode = str(mode or "").strip()
        normalized_scope = str(account_scope or "").strip()
        if not normalized_mode or not normalized_scope:
            raise ValueError("environment context requires either environment_id or mode + account_scope")

        resolved = resolve_environment_key(
            mode=normalized_mode,
            account_scope=normalized_scope,
            broker_user_id=broker_user_id,
            paper_account_key=paper_account_key,
            environment_epoch=int(environment_epoch or 1),
        )
        if not create_if_missing:
            resolved_mode = str(getattr(resolved.mode, "value", resolved.mode))
            for environment in self.repository.list_execution_environments(mode=resolved_mode):
                if (
                    str(environment.account_scope) == str(resolved.account_scope)
                    and str(environment.broker_user_id or "") == str(resolved.broker_user_id or "")
                    and str(environment.paper_account_key or "") == str(resolved.paper_account_key or "")
                    and int(environment.environment_epoch) == int(resolved.environment_epoch)
                ):
                    return str(environment.id)
            raise LookupError(
                "Unknown environment for mode/account_scope; provide environment_id or create the environment first"
            )
        return self.repository.ensure_execution_environment(
            mode=str(getattr(resolved.mode, "value", resolved.mode)),
            account_scope=resolved.account_scope,
            broker_user_id=resolved.broker_user_id,
            paper_account_key=resolved.paper_account_key,
            environment_epoch=resolved.environment_epoch,
            display_name=resolved.display_name,
            metadata=resolved.metadata,
        )

    def list_v2_environments(self, *, mode: str | None = None) -> List[Dict[str, Any]]:
        normalized_mode = str(mode).strip() if mode is not None else None
        environments = self.repository.list_execution_environments(mode=normalized_mode)
        return [_serialize_decimal(item.model_dump(mode="python")) for item in environments]

    def _require_v2_environment(self, environment_id: str):
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(normalized_environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
        return normalized_environment_id, environment

    def _current_ist_date(self) -> date:
        return datetime.now(IST).date()

    def _to_environment_ref(self, environment: Any) -> JournalEnvironmentRef:
        return JournalEnvironmentRef(
            environment_id=str(environment.id),
            mode=str(getattr(environment.mode, "value", environment.mode)),
            account_scope=environment.account_scope,
            display_name=environment.display_name,
            broker_user_id=environment.broker_user_id,
            paper_account_key=environment.paper_account_key,
        )

    def _context_template_id(self, context: Any | None) -> str | None:
        if context is None:
            return None
        metadata = dict(getattr(context, "metadata", {}) or {})
        return (
            str(getattr(context, "strategy_template_id", None) or "").strip()
            or str(metadata.get("strategy_template_id") or "").strip()
            or None
        )

    def _to_strategy_ref(self, context: Any | None) -> JournalV2StrategyRef | None:
        template_id = self._context_template_id(context)
        metadata = dict(getattr(context, "metadata", {}) or {}) if context is not None else {}
        if template_id:
            template = self.repository.get_strategy_template(template_id)
            if template is not None:
                return JournalV2StrategyRef(
                    template_id=str(template.id),
                    strategy_family=str(getattr(template.strategy_family, "value", template.strategy_family)),
                    template_key=template.template_key,
                    display_name=template.display_name or template.template_key or str(template.id),
                )
        resolved_identity = dict(metadata.get("resolved_identity") or {})
        template_key = str(resolved_identity.get("template_id") or template_id or "").strip()
        if not template_key:
            return None
        return JournalV2StrategyRef(
            template_id=template_id or template_key,
            strategy_family=str(resolved_identity.get("strategy_family") or metadata.get("strategy_family") or "unknown_strategy"),
            template_key=template_key,
            display_name=str(resolved_identity.get("display_name") or metadata.get("display_name") or template_key),
        )

    def _cost_breakdown_from_fact(self, fact: JournalExecutionFact) -> CostBreakdown:
        total_charges = (
            _to_decimal(fact.brokerage)
            + _to_decimal(fact.exchange_txn_charge)
            + _to_decimal(fact.stt)
            + _to_decimal(fact.stamp_duty)
            + _to_decimal(fact.sebi_charge)
            + _to_decimal(fact.gst)
        )
        if total_charges == ZERO:
            total_charges = _to_decimal(fact.fees_amount) + _to_decimal(fact.taxes_amount) + _to_decimal(fact.slippage_amount)
        return CostBreakdown(
            brokerage=_to_decimal(fact.brokerage),
            exchange_txn_charge=_to_decimal(fact.exchange_txn_charge),
            stt=_to_decimal(fact.stt),
            stamp_duty=_to_decimal(fact.stamp_duty),
            sebi_charge=_to_decimal(fact.sebi_charge),
            gst=_to_decimal(fact.gst),
            total_charges=total_charges,
        )

    def _to_fill_view(self, fact: JournalExecutionFact) -> JournalV2ExecutionFillView:
        breakdown = self._cost_breakdown_from_fact(fact)
        return JournalV2ExecutionFillView(
            fact_id=fact.id,
            leg_id=fact.leg_id,
            source_type=fact.source_type,
            source_fact_key=fact.source_fact_key,
            order_id=fact.order_id,
            trade_id=fact.trade_id,
            fill_timestamp=fact.fill_timestamp,
            side=fact.side,
            quantity=fact.quantity,
            price=fact.price,
            gross_cash_flow=fact.gross_cash_flow,
            fees_amount=fact.fees_amount,
            taxes_amount=fact.taxes_amount,
            slippage_amount=fact.slippage_amount,
            brokerage=breakdown.brokerage,
            exchange_txn_charge=breakdown.exchange_txn_charge,
            stt=breakdown.stt,
            stamp_duty=breakdown.stamp_duty,
            sebi_charge=breakdown.sebi_charge,
            gst=breakdown.gst,
            margin_required=fact.margin_required,
            charges_status=fact.charges_status,
            payload=dict(fact.payload or {}),
        )

    def _build_leg_views(self, facts: list[JournalExecutionFact]) -> list[JournalV2EpisodeLegView]:
        grouped: dict[str, dict[str, Any]] = {}
        for fact in facts:
            payload = dict(fact.payload or {})
            broker_fill = dict(payload.get("broker_fill") or {}) if isinstance(payload.get("broker_fill"), dict) else {}
            instrument_token = payload.get("instrument_token", broker_fill.get("instrument_token"))
            exchange = payload.get("exchange") or broker_fill.get("exchange")
            tradingsymbol = payload.get("tradingsymbol") or broker_fill.get("tradingsymbol")
            product = payload.get("product") or broker_fill.get("product")
            key = str(fact.leg_id) if fact.leg_id is not None else f"{instrument_token}:{exchange}:{tradingsymbol}:{product}"
            if key not in grouped:
                grouped[key] = {
                    "leg_id": fact.leg_id,
                    "leg_seq": len(grouped) + 1,
                    "instrument_token": instrument_token,
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "product": product,
                    "opened_quantity": 0,
                    "closed_quantity": 0,
                    "net_quantity": 0,
                    "direction": None,
                    "metadata": {},
                }
            leg = grouped[key]
            delta = int(fact.quantity) if str(fact.side or "").upper() == "BUY" else -int(fact.quantity)
            previous = int(leg["net_quantity"])
            if previous == 0 or (previous > 0 and delta > 0) or (previous < 0 and delta < 0):
                leg["opened_quantity"] += abs(delta)
            elif abs(delta) <= abs(previous):
                leg["closed_quantity"] += abs(delta)
            else:
                leg["closed_quantity"] += abs(previous)
                leg["opened_quantity"] += abs(delta) - abs(previous)
            leg["net_quantity"] = previous + delta
            if leg["direction"] is None and delta != 0:
                leg["direction"] = JournalEpisodeLegDirection.LONG if delta > 0 else JournalEpisodeLegDirection.SHORT
            leg["metadata"] = {**leg["metadata"], "position_effect": fact.position_effect}
        return [JournalV2EpisodeLegView(**item) for item in grouped.values()]

    def _derive_episode_direction(self, legs: list[JournalV2EpisodeLegView]) -> JournalEpisodeLegDirection | None:
        for leg in legs:
            if leg.direction is not None:
                return leg.direction
        return None

    def _to_timeline_view(self, event: JournalTimelineEvent) -> JournalV2TimelineEventView:
        return JournalV2TimelineEventView(
            event_id=event.id,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            channel=event.channel,
            event_type=event.event_type,
            actor_type=event.actor_type,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            occurred_at=event.occurred_at,
            payload=dict(event.payload or {}),
        )

    def _analytics_metrics(self, outcomes: list[EpisodeOutcome]) -> AnalyticsMetrics:
        rows = list(outcomes)
        payload = dict(build_environment_episode_metrics(rows))
        hold_seconds_total = sum((int(item.hold_seconds) for item in rows), 0)
        closed_episode_count = len(rows)
        win_count = sum(1 for item in rows if item.net_pnl > ZERO)
        loss_count = sum(1 for item in rows if item.net_pnl < ZERO)
        payload["hold_seconds_total"] = hold_seconds_total
        payload["hold_seconds_avg"] = int(hold_seconds_total / closed_episode_count) if closed_episode_count else None
        payload["win_count"] = win_count
        payload["loss_count"] = loss_count
        payload["cost_ratio"] = _safe_ratio(_to_decimal(payload.get("total_charges")), _to_decimal(payload.get("gross_pnl")))
        streak = streaks([item.net_pnl for item in rows]) if rows else {"wins": 0, "losses": 0}
        payload["max_win_streak"] = int(streak.get("wins") or 0)
        payload["max_loss_streak"] = int(streak.get("losses") or 0)
        return AnalyticsMetrics.model_validate(payload)

    def _unknown_strategy_ref(self, key: str = "unknown_strategy") -> JournalV2StrategyRef:
        normalized = str(key or "unknown_strategy")
        return JournalV2StrategyRef(
            template_id=normalized,
            strategy_family="unknown_strategy",
            template_key=normalized,
            display_name="Unknown strategy",
        )

    def _episode_matches_date_range(self, episode: Any, from_date: date | None, to_date: date | None) -> bool:
        if from_date is None and to_date is None:
            return True
        start_at = getattr(episode, "opened_at", None)
        end_at = getattr(episode, "closed_at", None) or start_at
        if start_at is None or end_at is None:
            return False
        start_day = start_at.astimezone(IST).date()
        end_day = end_at.astimezone(IST).date()
        if from_date is not None and end_day < from_date:
            return False
        if to_date is not None and start_day > to_date:
            return False
        return True

    def _strategy_matches_filter(self, strategy_ref: JournalV2StrategyRef | None, strategy_filter: str | None) -> bool:
        if not strategy_filter:
            return True
        normalized = str(strategy_filter or "").strip().lower()
        if not normalized:
            return True
        candidates = [
            str(strategy_ref.template_id).lower() if strategy_ref else "",
            str(strategy_ref.template_key or "").lower() if strategy_ref else "",
            str(strategy_ref.display_name or "").lower() if strategy_ref else "",
        ]
        return normalized in {item for item in candidates if item}

    def _closed_episode_date(self, episode: Any) -> date | None:
        closed_at = getattr(episode, "closed_at", None)
        if closed_at is None:
            return None
        return closed_at.astimezone(IST).date()

    def _bucket_start_for_date(self, value: date, granularity: str) -> date:
        if granularity == "day":
            return value
        if granularity == "week":
            return value - timedelta(days=value.weekday())
        if granularity == "month":
            return value.replace(day=1)
        raise ValueError("granularity must be one of: day, week, month")

    def _bucket_end_for_start(self, bucket_start: date, granularity: str) -> date:
        if granularity == "day":
            return bucket_start
        if granularity == "week":
            return bucket_start + timedelta(days=6)
        if granularity == "month":
            return bucket_start.replace(day=monthrange(bucket_start.year, bucket_start.month)[1])
        raise ValueError("granularity must be one of: day, week, month")

    def _next_bucket_start(self, bucket_start: date, granularity: str) -> date:
        if granularity == "day":
            return bucket_start + timedelta(days=1)
        if granularity == "week":
            return bucket_start + timedelta(days=7)
        if granularity == "month":
            if bucket_start.month == 12:
                return date(bucket_start.year + 1, 1, 1)
            return date(bucket_start.year, bucket_start.month + 1, 1)
        raise ValueError("granularity must be one of: day, week, month")

    def _bucket_label(self, bucket_start: date, granularity: str) -> str:
        if granularity == "month":
            return bucket_start.strftime("%Y-%m")
        return bucket_start.isoformat()

    def list_v2_episodes(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None = None,
        status: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        strategy: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_environment_id, _environment = self._require_v2_environment(environment_id)
        normalized_context_id = None
        if execution_context_id is not None:
            normalized_context_id = _require_uuid("execution_context_id", execution_context_id)
            self._ensure_v2_context_in_environment(normalized_context_id, normalized_environment_id)
        episodes = self.repository.list_episodes(
            environment_id=normalized_environment_id,
            execution_context_id=normalized_context_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        items: list[dict[str, Any]] = []
        for episode in episodes:
            if not self._episode_matches_date_range(episode, from_date, to_date):
                continue
            context = self.repository.get_execution_context(str(episode.execution_context_id))
            strategy_ref = self._to_strategy_ref(context)
            if not self._strategy_matches_filter(strategy_ref, strategy):
                continue
            facts = self.repository.list_execution_facts_for_episode(str(episode.id))
            legs = self._build_leg_views(facts)
            card = JournalV2EpisodeCard(
                episode_id=str(episode.id),
                status=str(getattr(episode.status, "value", episode.status)),
                opened_at=episode.opened_at,
                closed_at=episode.closed_at,
                strategy=strategy_ref,
                direction=self._derive_episode_direction(legs),
                outcome=build_episode_outcome(episode=episode, facts=facts),
                fill_count=len(facts),
                leg_count=len(legs),
                notes=str(getattr(episode, "notes", "") or ""),
            )
            items.append(_serialize_decimal(card.model_dump(mode="python")))
        return items

    def _build_v2_episode_detail_response(self, episode: Any, environment: Any) -> JournalV2EpisodeDetailResponse:
        context = self.repository.get_execution_context(str(episode.execution_context_id))
        strategy_ref = self._to_strategy_ref(context)
        facts = self.repository.list_execution_facts_for_episode(str(episode.id))
        legs = self._build_leg_views(facts)
        fills = [self._to_fill_view(fact) for fact in facts]
        timeline = [
            self._to_timeline_view(event)
            for event in self.repository.list_timeline_events(environment_id=str(environment.id), episode_id=str(episode.id), limit=500)
        ]
        return JournalV2EpisodeDetailResponse(
            environment=self._to_environment_ref(environment),
            episode=JournalV2EpisodeCard(
                episode_id=str(episode.id),
                status=str(getattr(episode.status, "value", episode.status)),
                opened_at=episode.opened_at,
                closed_at=episode.closed_at,
                strategy=strategy_ref,
                direction=self._derive_episode_direction(legs),
                outcome=build_episode_outcome(episode=episode, facts=facts),
                fill_count=len(fills),
                leg_count=len(legs),
                notes=str(getattr(episode, "notes", "") or ""),
            ),
            legs=legs,
            fills=fills,
            timeline=timeline,
            notes=str(getattr(episode, "notes", "") or ""),
        )

    def get_v2_episode_detail(self, episode_id: str, *, environment_id: str) -> JournalV2EpisodeDetailResponse | None:
        normalized_environment_id, environment = self._require_v2_environment(environment_id)
        normalized_episode_id = str(episode_id or "").strip()
        if not _looks_like_uuid(normalized_episode_id):
            raise ValueError("episode_id must be a valid UUID")
        episode = self.repository.get_episode_detail(normalized_episode_id)
        if episode is None:
            return None
        if str(episode.environment_id) != normalized_environment_id:
            raise ValueError("episode_id does not belong to environment_id")
        return self._build_v2_episode_detail_response(episode, environment)

    def get_v2_daily(self, *, environment_id: str, trading_date: date | None = None) -> JournalV2DailyResponse:
        normalized_environment_id, environment = self._require_v2_environment(environment_id)
        resolved_trading_date = trading_date or self._current_ist_date()
        day_start_utc, day_end_utc = day_bounds_utc(resolved_trading_date)
        episodes = self.repository.list_episodes(environment_id=normalized_environment_id, limit=5000)
        closed_episodes = [
            episode
            for episode in episodes
            if episode.closed_at is not None and day_start_utc <= episode.closed_at < day_end_utc
        ]
        open_episodes = [
            episode
            for episode in episodes
            if episode.opened_at < day_end_utc and (episode.closed_at is None or episode.closed_at >= day_end_utc)
        ]
        episode_ids = [str(episode.id) for episode in [*closed_episodes, *open_episodes] if getattr(episode, "id", None) is not None]
        facts_by_episode = self.repository.list_execution_facts_for_episodes(episode_ids)
        context_cache: dict[str, Any | None] = {}

        def _context_for(episode: Any) -> Any | None:
            context_id = str(getattr(episode, "execution_context_id", "") or "")
            if not context_id:
                return None
            if context_id not in context_cache:
                context_cache[context_id] = self.repository.get_execution_context(context_id)
            return context_cache[context_id]

        grouped_cards: dict[str, list[JournalV2EpisodeCard]] = defaultdict(list)
        grouped_outcomes: dict[str, list[EpisodeOutcome]] = defaultdict(list)
        strategy_refs: dict[str, JournalV2StrategyRef] = {}
        notes_count = 0

        for episode in closed_episodes:
            context = _context_for(episode)
            strategy_ref = self._to_strategy_ref(context)
            facts = facts_by_episode.get(str(episode.id), [])
            legs = self._build_leg_views(facts)
            outcome = build_episode_outcome(episode=episode, facts=facts)
            key = str(strategy_ref.template_id if strategy_ref else f"unmapped:{episode.execution_context_id}")
            if strategy_ref is not None:
                strategy_refs[key] = strategy_ref
            grouped_outcomes[key].append(outcome)
            grouped_cards[key].append(
                JournalV2EpisodeCard(
                    episode_id=str(episode.id),
                    status=str(getattr(episode.status, "value", episode.status)),
                    opened_at=episode.opened_at,
                    closed_at=episode.closed_at,
                    strategy=strategy_ref,
                    direction=self._derive_episode_direction(legs),
                    outcome=outcome,
                    fill_count=len(facts),
                    leg_count=len(legs),
                    notes=str(getattr(episode, "notes", "") or ""),
                )
            )
            if str(getattr(episode, "notes", "") or "").strip():
                notes_count += 1

        strategy_groups: list[JournalV2StrategyGroup] = []
        for key, cards in grouped_cards.items():
            strategy_groups.append(
                JournalV2StrategyGroup(
                    strategy=strategy_refs.get(key) or self._unknown_strategy_ref(key),
                    metrics=self._analytics_metrics(grouped_outcomes[key]),
                    episodes=cards,
                )
            )

        open_episode_cards: list[JournalV2OpenEpisodeCard] = []
        for episode in open_episodes:
            context = _context_for(episode)
            strategy_ref = self._to_strategy_ref(context)
            facts = facts_by_episode.get(str(episode.id), [])
            legs = self._build_leg_views(facts)
            open_episode_cards.append(
                JournalV2OpenEpisodeCard(
                    episode_id=str(episode.id),
                    status=str(getattr(episode.status, "value", episode.status)),
                    opened_at=episode.opened_at,
                    strategy=strategy_ref,
                    direction=self._derive_episode_direction(legs),
                    fill_count=len(facts),
                    leg_count=len(legs),
                    current_pnl_estimate=None,
                    notes=str(getattr(episode, "notes", "") or ""),
                )
            )
            if str(getattr(episode, "notes", "") or "").strip():
                notes_count += 1

        all_closed_outcomes = [outcome for values in grouped_outcomes.values() for outcome in values]
        return JournalV2DailyResponse(
            environment=self._to_environment_ref(environment),
            trading_date=resolved_trading_date,
            summary=JournalV2DailySummary(
                trading_date=resolved_trading_date,
                metrics=self._analytics_metrics(all_closed_outcomes),
                closed_episode_count=len(closed_episodes),
                open_episode_count=len(open_episodes),
                strategy_count=len(strategy_groups),
                notes_count=notes_count,
            ),
            strategy_groups=strategy_groups,
            open_episodes=open_episode_cards,
        )

    def get_v2_period(
        self,
        *,
        environment_id: str,
        from_date: date,
        to_date: date,
        granularity: str = "day",
    ) -> JournalV2PeriodResponse:
        normalized_environment_id, environment = self._require_v2_environment(environment_id)
        normalized_granularity = str(granularity or "day").strip().lower()
        if normalized_granularity not in {"day", "week", "month"}:
            raise ValueError("granularity must be one of: day, week, month")
        if to_date < from_date:
            raise ValueError("to_date must be on or after from_date")
        start_at, _ = day_bounds_utc(from_date)
        _, end_at = day_bounds_utc(to_date)
        filtered = [
            episode
            for episode in self.repository.list_closed_episodes(environment_id=normalized_environment_id, limit=5000)
            if episode.closed_at is not None and start_at <= episode.closed_at < end_at
        ]
        episode_ids = [str(episode.id) for episode in filtered if getattr(episode, "id", None) is not None]
        facts_by_episode = self.repository.list_execution_facts_for_episodes(episode_ids)
        context_cache: dict[str, Any | None] = {}

        def _context_for(episode: Any) -> Any | None:
            context_id = str(getattr(episode, "execution_context_id", "") or "")
            if not context_id:
                return None
            if context_id not in context_cache:
                context_cache[context_id] = self.repository.get_execution_context(context_id)
            return context_cache[context_id]

        grouped_buckets: dict[date, list[EpisodeOutcome]] = defaultdict(list)
        grouped_strategies: dict[str, dict[str, Any]] = {}
        overall_outcomes: list[EpisodeOutcome] = []
        for episode in filtered:
            facts = facts_by_episode.get(str(episode.id), [])
            outcome = build_episode_outcome(episode=episode, facts=facts)
            overall_outcomes.append(outcome)
            closed_day = self._closed_episode_date(episode)
            if closed_day is None:
                continue
            bucket_start = self._bucket_start_for_date(closed_day, normalized_granularity)
            grouped_buckets[bucket_start].append(outcome)
            context = _context_for(episode)
            strategy_ref = self._to_strategy_ref(context)
            key = str(strategy_ref.template_id if strategy_ref else f"unmapped:{episode.execution_context_id}")
            if key not in grouped_strategies:
                grouped_strategies[key] = {
                    "strategy": strategy_ref or self._unknown_strategy_ref(key),
                    "outcomes": [],
                }
            grouped_strategies[key]["outcomes"].append(outcome)

        buckets: list[JournalV2PeriodBucket] = []
        cursor = self._bucket_start_for_date(from_date, normalized_granularity)
        while cursor <= to_date:
            buckets.append(
                JournalV2PeriodBucket(
                    bucket_start=cursor,
                    bucket_end=min(self._bucket_end_for_start(cursor, normalized_granularity), to_date),
                    label=self._bucket_label(cursor, normalized_granularity),
                    metrics=self._analytics_metrics(grouped_buckets.get(cursor, [])),
                    closed_episode_count=len(grouped_buckets.get(cursor, [])),
                )
            )
            cursor = self._next_bucket_start(cursor, normalized_granularity)

        strategies = [
            JournalV2StrategySummaryItem(
                strategy=value["strategy"],
                metrics=self._analytics_metrics(value["outcomes"]),
                episode_count=len(value["outcomes"]),
            )
            for value in grouped_strategies.values()
        ]
        strategies.sort(key=lambda item: (item.metrics.net_pnl, item.episode_count), reverse=True)

        return JournalV2PeriodResponse(
            environment=self._to_environment_ref(environment),
            from_date=from_date,
            to_date=to_date,
            granularity=normalized_granularity,
            summary=self._analytics_metrics(overall_outcomes),
            buckets=buckets,
            strategies=strategies,
        )

    def patch_v2_episode_notes(
        self,
        episode_id: str,
        *,
        environment_id: str,
        notes: str,
    ) -> JournalV2EpisodeDetailResponse:
        normalized_environment_id, _environment = self._require_v2_environment(environment_id)
        normalized_episode_id = _require_uuid("episode_id", episode_id)
        updated = self.repository.update_episode_notes(
            episode_id=normalized_episode_id,
            environment_id=normalized_environment_id,
            notes=str(notes or ""),
        )
        if not updated:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        episode = self.repository.get_episode_detail(normalized_episode_id)
        if episode is not None:
            self._append_timeline_event_best_effort(
                environment_id=normalized_environment_id,
                execution_context_id=str(getattr(episode, "execution_context_id", "") or "") or None,
                episode_id=normalized_episode_id,
                subject_type="episode",
                subject_id=normalized_episode_id,
                channel="notes",
                event_type="notes_updated",
                payload={"notes_length": len(str(notes or ""))},
            )
        detail = self.get_v2_episode_detail(normalized_episode_id, environment_id=normalized_environment_id)
        if detail is None:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        return detail

    def list_v2_timeline(
        self,
        *,
        episode_id: str,
        environment_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        normalized_episode_id = _require_uuid("episode_id", episode_id)
        episode = self.repository.get_episode_detail(normalized_episode_id)
        if episode is None:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        if str(episode.environment_id) != normalized_environment_id:
            raise ValueError("episode_id does not belong to environment_id")
        events = self.repository.list_timeline_events(
            environment_id=normalized_environment_id,
            episode_id=normalized_episode_id,
            limit=limit,
            offset=offset,
        )
        return [_serialize_decimal(item.model_dump(mode="python")) for item in events]

    def _ensure_v2_episode_in_environment(self, episode_id: str, environment_id: str) -> None:
        normalized_episode_id = _require_uuid("episode_id", episode_id)
        episode = self.repository.get_episode_detail(normalized_episode_id)
        if episode is None:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        if str(episode.environment_id) != str(environment_id):
            raise ValueError("episode_id does not belong to environment_id")

    def _ensure_v2_context_in_environment(self, execution_context_id: str, environment_id: str) -> None:
        normalized_context_id = _require_uuid("execution_context_id", execution_context_id)
        context = self.repository.get_execution_context(normalized_context_id)
        if context is None:
            raise LookupError(f"Unknown execution_context_id: {normalized_context_id}")
        if str(context.environment_id) != str(environment_id):
            raise ValueError("execution_context_id does not belong to environment_id")

    def _ensure_v2_note_in_environment(self, note_id: str, environment_id: str) -> None:
        normalized_note_id = _require_uuid("note_id", note_id)
        note = self.repository.get_note(normalized_note_id)
        if note is None:
            raise LookupError(f"Unknown note_id: {normalized_note_id}")
        if str(note.environment_id) != str(environment_id):
            raise ValueError("note_id does not belong to environment_id")

    def append_timeline_event(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        event_type: str,
        episode_id: str | None = None,
        execution_context_id: str | None = None,
        channel: str | None = None,
        actor_type: str = "system",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        occurred_at: datetime | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> str:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        if episode_id is not None:
            episode_id = _require_uuid("episode_id", episode_id)
            self._ensure_v2_episode_in_environment(episode_id, normalized_environment_id)
        if execution_context_id is not None:
            execution_context_id = _require_uuid("execution_context_id", execution_context_id)
            self._ensure_v2_context_in_environment(execution_context_id, normalized_environment_id)
        try:
            resolved_actor_type = JournalTimelineActorType(str(actor_type or "system"))
        except ValueError as exc:
            raise ValueError("actor_type must be one of: system, user, algo") from exc
        event = JournalTimelineEvent(
            environment_id=normalized_environment_id,
            episode_id=episode_id,
            execution_context_id=execution_context_id,
            subject_type=subject_type,
            subject_id=subject_id,
            channel=channel,
            event_type=event_type,
            actor_type=resolved_actor_type,
            correlation_id=correlation_id,
            causation_id=causation_id,
            occurred_at=occurred_at or _utcnow(),
            payload=payload or {},
        )
        return self.repository.append_timeline_event(event)

    def _append_timeline_event_best_effort(self, **kwargs: Any) -> None:
        try:
            self.append_timeline_event(**kwargs)
        except Exception:
            logger.warning("journal_v2.timeline_emit_failed", extra={"timeline": kwargs}, exc_info=True)

    def ensure_v2_episode(
        self,
        *,
        environment_id: str,
        execution_context_id: str,
        episode_seq: int | None = None,
        status: str = "draft",
        opened_at: datetime | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> str:
        episode_id = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_seq=episode_seq,
            status=status,
            opened_at=opened_at,
            metadata=metadata,
        )
        self._append_timeline_event_best_effort(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_id=episode_id,
            subject_type="episode",
            subject_id=episode_id,
            channel="lifecycle",
            event_type="episode_opened",
            payload={"status": status, "episode_seq": episode_seq},
            occurred_at=opened_at,
        )
        return episode_id

    def close_v2_episode(
        self,
        episode_id: str,
        *,
        status: str = "closed",
        closed_at: datetime | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        episode = self.repository.get_episode_detail(episode_id)
        if episode is None:
            raise LookupError(f"Unknown episode_id: {episode_id}")
        self.repository.update_episode_status(
            episode_id,
            status=status,
            closed_at=closed_at,
            metadata=metadata,
        )
        self._append_timeline_event_best_effort(
            environment_id=str(episode.environment_id),
            execution_context_id=str(episode.execution_context_id),
            episode_id=episode_id,
            subject_type="episode",
            subject_id=episode_id,
            channel="lifecycle",
            event_type="episode_closed",
            payload={"status": status},
            occurred_at=closed_at,
        )

    def create_v2_execution_intent(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None = None,
        episode_id: str | None = None,
        channel: str | None = None,
        intent_type: str | None = None,
        idempotency_key: str | None = None,
        status: str = "pending",
        requested_at: datetime | None = None,
        payload: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> str:
        intent_id = self.repository.create_execution_intent(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_id=episode_id,
            channel=channel,
            intent_type=intent_type,
            idempotency_key=idempotency_key,
            status=status,
            requested_at=requested_at,
            payload=payload,
            metadata=metadata,
        )
        self._append_timeline_event_best_effort(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_id=episode_id,
            subject_type="intent",
            subject_id=intent_id,
            channel=channel,
            event_type="intent_created",
            payload={"intent_type": intent_type, "status": status},
            occurred_at=requested_at,
        )
        return intent_id

    def list_v2_strategies(
        self,
        *,
        environment_id: str,
        period: str = "since_inception",
        anchor_date: date | None = None,
    ) -> JournalV2StrategyListResponse:
        normalized_environment_id, environment = self._require_v2_environment(environment_id)
        normalized_period = MetricPeriod(str(getattr(period, "value", period) or "since_inception").strip().lower())
        resolved_anchor = anchor_date
        if normalized_period != MetricPeriod.SINCE_INCEPTION and resolved_anchor is None:
            resolved_anchor = self._current_ist_date()
        _from_date, _to_date, start_at, end_at = period_bounds_utc(normalized_period.value, resolved_anchor)

        grouped: dict[str, dict[str, Any]] = {}
        for episode in self.repository.list_closed_episodes(environment_id=normalized_environment_id, limit=5000):
            if start_at is not None and (episode.closed_at is None or episode.closed_at < start_at):
                continue
            if end_at is not None and (episode.closed_at is None or episode.closed_at >= end_at):
                continue
            context = self.repository.get_execution_context(str(episode.execution_context_id))
            strategy_ref = self._to_strategy_ref(context)
            key = str(strategy_ref.template_id if strategy_ref else f"unmapped:{episode.execution_context_id}")
            if key not in grouped:
                grouped[key] = {
                    "strategy": strategy_ref or JournalV2StrategyRef(template_id=key, strategy_family="unknown_strategy", template_key=key, display_name=key),
                    "outcomes": [],
                }
            grouped[key]["outcomes"].append(build_episode_outcome(episode=episode, facts=self.repository.list_execution_facts_for_episode(str(episode.id))))

        items = [
            JournalV2StrategySummaryItem(
                strategy=value["strategy"],
                metrics=self._analytics_metrics(value["outcomes"]),
                episode_count=len(value["outcomes"]),
            )
            for value in grouped.values()
        ]
        items.sort(key=lambda item: (item.metrics.net_pnl, item.episode_count), reverse=True)
        return JournalV2StrategyListResponse(
            environment=self._to_environment_ref(environment),
            period=normalized_period,
            anchor_date=resolved_anchor if normalized_period != MetricPeriod.SINCE_INCEPTION else None,
            items=items,
        )

    def patch_v2_episode_notes(
        self,
        episode_id: str,
        *,
        environment_id: str,
        notes: str,
    ) -> JournalV2EpisodeDetailResponse:
        normalized_environment_id, environment = self._require_v2_environment(environment_id)
        normalized_episode_id = _require_uuid("episode_id", episode_id)
        episode = self.repository.get_episode_detail(normalized_episode_id)
        if episode is None:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        if str(episode.environment_id) != normalized_environment_id:
            raise ValueError("episode_id does not belong to environment_id")
        updated = self.repository.update_episode_notes(
            episode_id=normalized_episode_id,
            environment_id=normalized_environment_id,
            notes=str(notes or ""),
        )
        if not updated:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        self._append_timeline_event_best_effort(
            environment_id=normalized_environment_id,
            execution_context_id=str(episode.execution_context_id),
            episode_id=normalized_episode_id,
            subject_type="episode",
            subject_id=normalized_episode_id,
            channel="notes",
            event_type="notes_updated",
            payload={"notes": str(notes or "")},
        )
        refreshed = self.repository.get_episode_detail(normalized_episode_id)
        if refreshed is None:
            raise LookupError(f"Unknown episode_id: {normalized_episode_id}")
        return self._build_v2_episode_detail_response(refreshed, environment)

    def list_v2_unresolved(self, *, environment_id: str) -> Dict[str, Any]:
        environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {environment_id}")
        items = [
            _serialize_decimal(item)
            for item in self.repository.list_unresolved_items(environment_id=environment_id)
        ]
        return {
            "environment_id": environment_id,
            "items": items,
            "count": len(items),
        }

    def compute_v2_environment_metrics(self, *, environment_id: str, calc_version: str = DEFAULT_V2_CALC_VERSION) -> Dict[str, Any]:
        environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {environment_id}")

        closed_episodes = self.repository.list_closed_episodes(environment_id=environment_id, limit=5000)
        outcomes = []
        for episode in closed_episodes:
            facts = self.repository.list_execution_facts_for_episode(str(episode.id))
            outcomes.append(build_episode_outcome(episode=episode, facts=facts))
        metrics = build_environment_episode_metrics(outcomes)
        self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=environment_id,
                subject_type="environment",
                subject_id=environment_id,
                window="since_inception",
                calc_version=calc_version,
                identity_rule_version="journal_v2_identity_v1",
                grouping_rule_version="journal_v2_grouping_v1",
                computed_at=_utcnow(),
                metrics=metrics,
            )
        )
        return {
            "environment_id": environment_id,
            "closed_episode_count": len(closed_episodes),
            "metrics": _serialize_decimal(metrics),
        }

    def compute_v2_environment_strategy_metrics(
        self,
        *,
        environment_id: str,
        calc_version: str = DEFAULT_V2_CALC_VERSION,
    ) -> Dict[str, Any]:
        environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {environment_id}")

        rows: list[dict[str, Any]] = []
        for template_row in self.repository.list_strategy_templates_for_environment(environment_id=environment_id):
            template_id = str(template_row.get("template_id") or "")
            if not template_id:
                continue
            closed_episodes = self.repository.list_closed_episodes_for_environment_template(
                environment_id=environment_id,
                template_id=template_id,
                limit=5000,
            )
            outcomes = [
                build_episode_outcome(
                    episode=episode,
                    facts=self.repository.list_execution_facts_for_episode(str(episode.id)),
                )
                for episode in closed_episodes
            ]
            metrics = build_environment_episode_metrics(outcomes)
            self.repository.replace_metric_snapshot(
                JournalMetricSnapshot(
                    environment_id=environment_id,
                    subject_type="strategy_template",
                    subject_id=template_id,
                    window="since_inception",
                    calc_version=calc_version,
                    identity_rule_version="journal_v2_identity_v1",
                    grouping_rule_version="journal_v2_grouping_v1",
                    computed_at=_utcnow(),
                    metrics=metrics,
                )
            )
            rows.append(
                {
                    "template_id": template_id,
                    "strategy_family": template_row.get("strategy_family") or "unknown_strategy",
                    "display_name": template_row.get("display_name") or template_row.get("template_key") or template_id,
                    "metrics": _serialize_decimal(metrics),
                }
            )

        scorecards = [
            _serialize_decimal(
                {
                    "template_id": scorecard.template_id,
                    "strategy_family": scorecard.strategy_family,
                    "display_name": scorecard.display_name,
                    "metrics": scorecard.metrics,
                }
            )
            for scorecard in build_strategy_template_scorecards(rows)
        ]
        return {"environment_id": environment_id, "items": scorecards, "count": len(scorecards)}

    def compare_v2_paper_live_for_template(
        self,
        *,
        template_id: str,
        paper_environment_id: str,
        live_environment_id: str,
        calc_version: str = DEFAULT_V2_CALC_VERSION,
    ) -> Dict[str, Any]:
        normalized_template_id = str(template_id or "").strip()
        if not normalized_template_id:
            raise ValueError("template_id is required")
        paper_environment_id = _require_uuid("paper_environment_id", paper_environment_id)
        live_environment_id = _require_uuid("live_environment_id", live_environment_id)

        paper_environment = self.repository.get_execution_environment(paper_environment_id)
        if paper_environment is None:
            raise LookupError(f"Unknown paper_environment_id: {paper_environment_id}")
        live_environment = self.repository.get_execution_environment(live_environment_id)
        if live_environment is None:
            raise LookupError(f"Unknown live_environment_id: {live_environment_id}")
        if str(getattr(paper_environment.mode, "value", paper_environment.mode)) != "paper":
            raise ValueError("paper_environment_id must reference a paper environment")
        if str(getattr(live_environment.mode, "value", live_environment.mode)) != "live":
            raise ValueError("live_environment_id must reference a live environment")

        def _metrics_for_environment(environment_id: str) -> dict[str, Any]:
            episodes = self.repository.list_closed_episodes_for_environment_template(
                environment_id=environment_id,
                template_id=normalized_template_id,
                limit=5000,
            )
            outcomes = [
                build_episode_outcome(
                    episode=episode,
                    facts=self.repository.list_execution_facts_for_episode(str(episode.id)),
                )
                for episode in episodes
            ]
            metrics = build_environment_episode_metrics(outcomes)
            return _serialize_decimal(metrics)

        paper_metrics = _metrics_for_environment(paper_environment_id)
        live_metrics = _metrics_for_environment(live_environment_id)
        payload = build_paper_live_comparison(
            template_id=normalized_template_id,
            paper_metrics=paper_metrics,
            live_metrics=live_metrics,
        )
        payload["paper_environment_id"] = paper_environment_id
        payload["live_environment_id"] = live_environment_id
        return _serialize_decimal(payload)

    def recompute_v2_metrics(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        window: str = "since_inception",
        calc_version: str = DEFAULT_V2_CALC_VERSION,
    ) -> Dict[str, Any]:
        environment_id = _require_uuid("environment_id", environment_id)
        normalized_subject_type = str(subject_type or "").strip()
        normalized_window = _normalize_period(window)
        if normalized_window != "since_inception":
            raise ValueError("V2 recompute currently supports window=since_inception only")

        if normalized_subject_type == "environment":
            return self.compute_v2_environment_metrics(environment_id=environment_id, calc_version=calc_version)

        if normalized_subject_type == "strategy_template":
            episodes = self.repository.list_closed_episodes_for_environment_template(
                environment_id=environment_id,
                template_id=subject_id,
                limit=5000,
            )
        elif normalized_subject_type == "strategy_deployment":
            episodes = self.repository.list_closed_episodes_for_environment_deployment(
                environment_id=environment_id,
                deployment_id=subject_id,
                limit=5000,
            )
        elif normalized_subject_type == "episode":
            episode = self.repository.get_episode_detail(subject_id)
            if episode is None:
                raise LookupError(f"Unknown episode_id: {subject_id}")
            if str(episode.environment_id) != str(environment_id):
                raise ValueError("episode_id does not belong to environment_id")
            episodes = [episode] if str(episode.status) == "closed" else []
        else:
            raise ValueError("subject_type must be one of: episode, strategy_template, strategy_deployment, environment")

        outcomes = [
            build_episode_outcome(
                episode=episode,
                facts=self.repository.list_execution_facts_for_episode(str(episode.id)),
            )
            for episode in episodes
        ]
        metrics = build_environment_episode_metrics(outcomes)
        self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=environment_id,
                subject_type=normalized_subject_type,
                subject_id=subject_id,
                window=normalized_window,
                calc_version=calc_version,
                identity_rule_version="journal_v2_identity_v1",
                grouping_rule_version="journal_v2_grouping_v1",
                computed_at=_utcnow(),
                metrics=metrics,
            )
        )
        return {
            "environment_id": environment_id,
            "subject_type": normalized_subject_type,
            "subject_id": subject_id,
            "window": normalized_window,
            "metrics": _serialize_decimal(metrics),
            "closed_episode_count": len(episodes),
        }

    def _queue_unresolved_identity(
        self,
        *,
        environment_id: str,
        execution_context_id: str | None,
        source_system: str,
        identity: Any,
    ) -> None:
        reason = unresolved_reason_for_identity(identity)
        self.repository.create_unresolved_item(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            source_system=source_system,
            reason=reason,
            raw_identity=dict(getattr(identity, "raw_identity", {}) or {}),
            candidate_mappings=[dict(getattr(identity, "resolved_identity", {}) or {})],
            metadata={
                "resolution_method": getattr(identity, "resolution_method", None),
                "resolution_confidence": str(getattr(identity, "resolution_confidence", "0")),
                "identity_rule_version": getattr(identity, "identity_rule_version", "journal_v2_identity_v1"),
                "grouping_rule_version": getattr(identity, "grouping_rule_version", "journal_v2_grouping_v1"),
            },
        )

    def create_v2_note(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        note_type: str,
        title: str,
        body_markdown: str,
        episode_id: str | None = None,
        body_json: Dict[str, Any] | None = None,
        effective_at: datetime | None = None,
        author_id: str | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> str:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(normalized_environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
        normalized_episode_id = None
        if episode_id is not None:
            normalized_episode_id = _require_uuid("episode_id", episode_id)
            self._ensure_v2_episode_in_environment(normalized_episode_id, normalized_environment_id)

        body_text = markdown_to_search_text(body_markdown)
        note_id = self.repository.create_note(
            environment_id=normalized_environment_id,
            subject_type=subject_type,
            subject_id=subject_id,
            episode_id=normalized_episode_id,
            note_type=note_type,
            title=title,
            body_markdown=body_markdown,
            body_text=body_text,
            body_json=body_json,
            effective_at=effective_at,
            author_id=author_id,
            tags=tags,
            metadata=metadata,
        )
        self._append_timeline_event_best_effort(
            environment_id=normalized_environment_id,
            episode_id=normalized_episode_id,
            subject_type=subject_type,
            subject_id=subject_id,
            channel="notes",
            event_type="note_created",
            payload={"note_id": note_id, "note_type": note_type, "title": title},
            occurred_at=effective_at,
        )
        return note_id

    def update_v2_note(
        self,
        note_id: str,
        *,
        environment_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        title: str | None = None,
        body_markdown: str | None = None,
        body_json: Dict[str, Any] | None = None,
        tags: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
        editor_id: str | None = None,
        change_reason: str | None = None,
    ) -> None:
        existing = self.repository.get_note(note_id)
        if existing is None:
            raise LookupError(f"Unknown note_id: {note_id}")

        if environment_id is not None and str(existing.environment_id) != str(environment_id):
            raise ValueError("environment_id mismatch for note update")
        if subject_type is not None and str(existing.subject_type) != str(subject_type):
            raise ValueError("subject_type mismatch for note update")
        if subject_id is not None and str(existing.subject_id) != str(subject_id):
            raise ValueError("subject_id mismatch for note update")

        body_text: str | None = None
        if body_markdown is not None:
            body_text = markdown_to_search_text(body_markdown)

        self.repository.update_note(
            note_id,
            title=title,
            body_markdown=body_markdown,
            body_text=body_text,
            body_json=body_json,
            tags=tags,
            metadata=metadata,
            editor_id=editor_id,
            change_reason=change_reason,
        )
        self._append_timeline_event_best_effort(
            environment_id=str(existing.environment_id),
            episode_id=existing.episode_id,
            subject_type=str(existing.subject_type),
            subject_id=str(existing.subject_id),
            channel="notes",
            event_type="note_updated",
            payload={"note_id": note_id, "title": title, "change_reason": change_reason},
        )

    def get_v2_note(self, note_id: str, *, environment_id: str) -> Dict[str, Any] | None:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        normalized_note_id = _require_uuid("note_id", note_id)
        note = self.repository.get_note(normalized_note_id)
        if note is None:
            return None
        if str(note.environment_id) != normalized_environment_id:
            raise ValueError("note_id does not belong to environment_id")
        return _serialize_decimal(note.model_dump(mode="python"))

    def list_v2_notes(
        self,
        environment_id: str,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        episode_id: str | None = None,
        note_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(normalized_environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
        normalized_episode_id = None
        if episode_id is not None:
            normalized_episode_id = _require_uuid("episode_id", episode_id)
            self._ensure_v2_episode_in_environment(normalized_episode_id, normalized_environment_id)
        notes = self.repository.list_notes(
            normalized_environment_id,
            subject_type=subject_type,
            subject_id=subject_id,
            episode_id=normalized_episode_id,
            note_type=note_type,
            limit=limit,
            offset=offset,
        )
        return [_serialize_decimal(item.model_dump(mode="python")) for item in notes]

    def list_v2_note_revisions(self, note_id: str, *, environment_id: str) -> List[Dict[str, Any]]:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        normalized_note_id = _require_uuid("note_id", note_id)
        note = self.repository.get_note(normalized_note_id)
        if note is None:
            raise LookupError(f"Unknown note_id: {normalized_note_id}")
        if str(note.environment_id) != normalized_environment_id:
            raise ValueError("note_id does not belong to environment_id")
        revisions = self.repository.list_note_revisions(normalized_note_id)
        return [_serialize_decimal(item.model_dump(mode="python")) for item in revisions]

    def attach_v2_file_metadata(
        self,
        *,
        environment_id: str,
        subject_type: str,
        subject_id: str,
        storage_key: str,
        mime_type: str,
        note_id: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        ocr_text: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> str:
        normalized_environment_id = _require_uuid("environment_id", environment_id)
        environment = self.repository.get_execution_environment(normalized_environment_id)
        if environment is None:
            raise LookupError(f"Unknown environment_id: {normalized_environment_id}")
        normalized_note_id = None
        if note_id is not None:
            normalized_note_id = _require_uuid("note_id", note_id)
            self._ensure_v2_note_in_environment(normalized_note_id, normalized_environment_id)

        return self.repository.attach_file_metadata(
            environment_id=normalized_environment_id,
            subject_type=subject_type,
            subject_id=subject_id,
            storage_key=storage_key,
            mime_type=mime_type,
            note_id=normalized_note_id,
            sha256=sha256,
            size_bytes=size_bytes,
            ocr_text=ocr_text,
            metadata=metadata,
        )

    def ensure_v2_worker_context(
        self,
        *,
        execution_mode: str,
        account_scope: str,
        strategy_run_id: str | None = None,
        external_run_id: str | None = None,
        template_id: str | None = None,
        worker_template_id: str | None = None,
        strategy_name: str | None = None,
        strategy_family: str | None = None,
        scenario_key: str | None = None,
        scenario_name: str | None = None,
        deployment_key: str | None = None,
        config_hash: str | None = None,
        source_system: str = "algo_worker",
        entry_surface: str | None = None,
        source_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_environment = resolve_environment_key(
            mode=execution_mode,
            account_scope=account_scope,
            metadata={"source_system": source_system, **dict(source_metadata or {})},
        )
        environment_id = self.repository.ensure_execution_environment(
            mode=str(getattr(resolved_environment.mode, "value", resolved_environment.mode)),
            account_scope=resolved_environment.account_scope,
            broker_user_id=resolved_environment.broker_user_id,
            paper_account_key=resolved_environment.paper_account_key,
            environment_epoch=resolved_environment.environment_epoch,
            display_name=resolved_environment.display_name,
            metadata=resolved_environment.metadata,
        )

        resolved_identity = resolve_strategy_identity(
            template_id=template_id,
            worker_template_id=worker_template_id,
            strategy_name=strategy_name,
            strategy_family=strategy_family,
            scenario_key=scenario_key,
            scenario_name=scenario_name,
            deployment_key=deployment_key,
            config_hash=config_hash,
            execution_mode=execution_mode,
            account_scope=account_scope,
            strategy_run_id=strategy_run_id,
            external_run_id=external_run_id,
        )

        low_confidence_identity = is_low_confidence_resolution(resolved_identity)
        template_ref_id: str | None = None
        if not low_confidence_identity:
            template_ref_id = self.repository.ensure_strategy_template(
                template_key=resolved_identity.template_id,
                strategy_family=resolved_identity.strategy_family,
                display_name=resolved_identity.display_name,
                metadata={
                    "source_system": source_system,
                    "entry_surface": entry_surface,
                },
            )

        variant_ref_id: str | None = None
        if template_ref_id is not None and resolved_identity.variant_key:
            variant_ref_id = self.repository.ensure_strategy_variant(
                template_id=template_ref_id,
                variant_key=resolved_identity.variant_key,
                display_name=scenario_name,
                metadata={"scenario_key": scenario_key, "config_hash": config_hash},
            )

        deployment_ref_id: str | None = None
        if template_ref_id is not None and resolved_identity.deployment_key:
            deployment_ref_id = self.repository.ensure_strategy_deployment(
                template_id=template_ref_id,
                variant_id=variant_ref_id,
                deployment_key=resolved_identity.deployment_key,
                display_name=deployment_key,
                metadata={"source_system": source_system, **dict(source_metadata or {})},
            )

        resolved_external_run_id = str(external_run_id or strategy_run_id or "").strip()
        if not resolved_external_run_id:
            raise ValueError("external_run_id or strategy_run_id is required")

        execution_context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system=source_system,
            external_run_id=resolved_external_run_id,
            template_id=template_ref_id,
            variant_id=variant_ref_id,
            deployment_id=deployment_ref_id,
            raw_identity=resolved_identity.raw_identity,
            resolved_identity=resolved_identity.resolved_identity,
            resolution_method=resolved_identity.resolution_method,
            resolution_confidence=resolved_identity.resolution_confidence,
            identity_rule_version=resolved_identity.identity_rule_version,
            metadata={
                "grouping_rule_version": resolved_identity.grouping_rule_version,
                "strategy_template_id": template_ref_id,
                "strategy_variant_id": variant_ref_id,
                "strategy_deployment_id": deployment_ref_id,
                **dict(source_metadata or {}),
            },
        )
        if low_confidence_identity:
            self._queue_unresolved_identity(
                environment_id=environment_id,
                execution_context_id=execution_context_id,
                source_system=source_system,
                identity=resolved_identity,
            )

        self._append_timeline_event_best_effort(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            subject_type="execution_context",
            subject_id=execution_context_id,
            channel="context",
            event_type="execution_context_created",
            payload={
                "source_system": source_system,
                "external_run_id": resolved_external_run_id,
            },
        )
        self._append_timeline_event_best_effort(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            subject_type="execution_context",
            subject_id=execution_context_id,
            channel="identity",
            event_type="identity_reclassified",
            payload={
                "resolution_method": resolved_identity.resolution_method,
                "resolution_confidence": str(resolved_identity.resolution_confidence),
                "identity_rule_version": resolved_identity.identity_rule_version,
                "grouping_rule_version": resolved_identity.grouping_rule_version,
            },
        )

        return {
            "environment_id": environment_id,
            "execution_context_id": execution_context_id,
            "template_id": template_ref_id,
            "variant_id": variant_ref_id,
            "deployment_id": deployment_ref_id,
            "identity_rule_version": resolved_identity.identity_rule_version,
            "grouping_rule_version": resolved_identity.grouping_rule_version,
            "ambiguous": resolved_identity.ambiguous,
            "resolution_method": resolved_identity.resolution_method,
            "resolution_confidence": str(resolved_identity.resolution_confidence),
        }

    def create_run(
        self,
        run: JournalRun,
        *,
        source_links: Optional[Iterable[JournalSourceLink]] = None,
        decision_events: Optional[Iterable[JournalDecisionEvent]] = None,
    ) -> Dict[str, Any]:
        run_id = self.repository.create_run(run)
        linked_sources: List[JournalSourceLink] = []
        appended_events: List[JournalDecisionEvent] = []

        for link in source_links or []:
            linked_sources.append(self.link_source(run_id, link))

        for event in decision_events or []:
            appended_events.append(self.append_decision_event(run_id, event))

        detail = self.get_run_detail(run_id)
        detail["linked_sources_count"] = len(linked_sources)
        detail["decision_events_appended"] = len(appended_events)
        return detail

    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        review_state: Optional[str] = None,
        ended_at: Optional[datetime] = None,
        source_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = self._require_run(run_id)
        merged_metadata = dict(existing.metadata or {})
        if metadata:
            merged_metadata.update(metadata)
        next_source_summary = source_summary if source_summary is not None else existing.source_summary
        self.repository.update_run(
            run_id,
            status=status,
            review_state=review_state,
            ended_at=ended_at,
            source_summary=next_source_summary,
            metadata=merged_metadata,
        )
        return self.get_run_detail(run_id)

    def list_trades(
        self,
        *,
        run_id: Optional[str] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        rows = self.repository.list_trade_rows(
            run_id=run_id,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            source_type=source_type,
            limit=limit,
            offset=offset,
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            fees_total = _to_decimal(row.get("fees_amount")) + _to_decimal(row.get("taxes_amount")) + _to_decimal(row.get("slippage_amount"))
            gross_cash_flow = _to_decimal(row.get("gross_cash_flow")) if row.get("gross_cash_flow") is not None else None
            items.append(
                _serialize_decimal(
                    {
                        **row,
                        "fees_total": fees_total,
                        "net_cash_flow": gross_cash_flow - fees_total if gross_cash_flow is not None else None,
                        "payload_json": row.get("payload_json") or {},
                    }
                )
            )
        return items

    def list_trades_page(
        self,
        *,
        run_id: Optional[str] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        source_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 200,
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page))
        safe_page_size = max(1, int(page_size))
        offset = (safe_page - 1) * safe_page_size
        items = self.list_trades(
            run_id=run_id,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            source_type=source_type,
            limit=safe_page_size,
            offset=offset,
        )
        total = self.repository.count_trade_rows(
            run_id=run_id,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            source_type=source_type,
        )
        return {"items": items, "total": total, "page": safe_page, "page_size": safe_page_size}

    def list_strategies(self, *, strategy_family: Optional[str] = None, execution_mode: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.repository.list_strategy_rollups(strategy_family=strategy_family, execution_mode=execution_mode, limit=limit)
        items: List[Dict[str, Any]] = []
        for row in rows:
            net_pnl = _to_decimal(row.get("net_pnl"))
            total_fees_value = _to_decimal(row.get("total_fees"))
            items.append(
                _serialize_decimal(
                    {
                        "strategy_family": row.get("strategy_family"),
                        "strategy_name": row.get("strategy_name"),
                        "run_count": int(row.get("run_count") or 0),
                        "open_run_count": int(row.get("open_run_count") or 0),
                        "closed_run_count": int(row.get("closed_run_count") or 0),
                        "review_backlog_count": int(row.get("review_backlog_count") or 0),
                        "latest_started_at": row.get("latest_started_at"),
                        "net_pnl": net_pnl,
                        "total_fees": total_fees_value,
                    }
                )
            )
        return items

    def get_review_queue(self, *, limit: int = 100, review_state: Optional[str] = None) -> Dict[str, Any]:
        items = [
            _serialize_decimal(
                {
                    **row,
                    "execution_fact_count": int(row.get("execution_fact_count") or 0),
                    "decision_event_count": int(row.get("decision_event_count") or 0),
                    "source_link_count": int(row.get("source_link_count") or 0),
                    "net_pnl": _to_decimal(row.get("net_pnl")) if row.get("net_pnl") is not None else None,
                }
            )
            for row in self.repository.list_review_queue_rows(limit=limit, review_state=review_state)
        ]
        return {
            "items": items,
            "count": len(items),
        }

    def list_rules(
        self,
        *,
        family_scope: Optional[str] = None,
        strategy_scope: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        rules = self.repository.list_rules(
            family_scope=family_scope,
            strategy_scope=strategy_scope,
            status=status,
            limit=limit,
        )
        return [_serialize_decimal(rule.model_dump(mode="python")) for rule in rules]

    def create_rule(self, rule: JournalRule) -> Dict[str, Any]:
        rule_id = self.repository.upsert_rule(rule)
        created = self.repository.get_rule(rule_id)
        if created is None:
            raise ValueError(f"Failed to load created rule: {rule_id}")
        return _serialize_decimal(created.model_dump(mode="python"))

    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.repository.get_rule(rule_id)
        if existing is None:
            raise ValueError(f"Unknown rule_id: {rule_id}")
        payload = existing.model_dump(mode="python")
        payload.update({key: value for key, value in updates.items() if value is not None})
        if updates.get("metadata"):
            merged = dict(existing.metadata or {})
            merged.update(updates["metadata"])
            payload["metadata"] = merged
        payload["id"] = rule_id
        next_rule = JournalRule(**payload)
        self.repository.upsert_rule(next_rule)
        stored = self.repository.get_rule(rule_id)
        return _serialize_decimal((stored or next_rule).model_dump(mode="python"))

    def get_calendar_summary(
        self,
        *,
        start_day: Optional[date] = None,
        end_day: Optional[date] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        limit: int = 366,
    ) -> Dict[str, Any]:
        rows = self.repository.list_calendar_summary_rows(
            start_day=start_day,
            end_day=end_day,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            limit=limit,
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            realized_pnl = _to_decimal(row.get("realized_pnl"))
            total_fees_value = _to_decimal(row.get("total_fees"))
            trade_count = int(row.get("trade_count") or 0)
            items.append(
                _serialize_decimal(
                    {
                        "trading_day": row.get("trading_day"),
                        "trade_count": trade_count,
                        "run_count": int(row.get("run_count") or 0),
                        "winning_trade_count": int(row.get("winning_trade_count") or 0),
                        "losing_trade_count": int(row.get("losing_trade_count") or 0),
                        "realized_pnl": realized_pnl,
                        "total_fees": total_fees_value,
                        "net_pnl": realized_pnl - total_fees_value,
                        "win_rate": _safe_ratio(Decimal(int(row.get("winning_trade_count") or 0)), Decimal(trade_count)) if trade_count else None,
                    }
                )
            )
        return {
            "items": items,
            "count": len(items),
        }

    def get_insights_feed(self, *, limit: int = 20) -> Dict[str, Any]:
        review_queue = self.get_review_queue(limit=max(1, min(limit, 20))).get("items", [])
        strategies = self.list_strategies(limit=max(1, min(limit, 20)))
        calendar = self.get_calendar_summary(limit=max(1, min(limit, 20))).get("items", [])
        aggregates = self.get_aggregate_summaries()

        items: List[Dict[str, Any]] = []
        for strategy in strategies[:limit]:
            items.append(
                {
                    "type": "strategy_rollup",
                    "title": f"{strategy['strategy_family']} / {strategy['strategy_name']}",
                    "summary": f"{strategy['run_count']} runs, net pnl {strategy['net_pnl']}",
                    "context": strategy,
                    "timestamp": strategy.get("latest_started_at"),
                }
            )
        for queue_item in review_queue[:limit - len(items)]:
            items.append(
                {
                    "type": "review_queue",
                    "title": f"Review {queue_item['strategy_family']} run",
                    "summary": f"Run {queue_item['id']} has {queue_item['execution_fact_count']} trades and review_state {queue_item['review_state']}",
                    "context": queue_item,
                    "timestamp": queue_item.get("ended_at") or queue_item.get("started_at"),
                }
            )
        if calendar:
            items.append(
                {
                    "type": "calendar_day",
                    "title": f"Latest trading day {calendar[0]['trading_day']}",
                    "summary": f"{calendar[0]['trade_count']} trades, net pnl {calendar[0]['net_pnl']}",
                    "context": calendar[0],
                    "timestamp": calendar[0].get("trading_day"),
                }
            )
        items.append(
            {
                "type": "aggregate",
                "title": "Since inception summary",
                "summary": f"Net pnl {aggregates['since_inception']['metrics']['net_pnl']}",
                "context": aggregates["since_inception"],
                "timestamp": _utcnow(),
            }
        )
        items.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return {"items": _serialize_decimal(items[:limit])}

    def get_summary(
        self,
        *,
        period: str = "month",
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        window = _normalize_period(period)
        aggregate = self.get_aggregate_summaries(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
        ).get(window, {})
        metrics = dict((aggregate.get("metrics") or {}))
        benchmark = self.get_benchmark_comparison(
            period=window,
            strategy_family=strategy_family,
            execution_mode=execution_mode,
        )
        metrics["benchmark_return"] = benchmark.get("benchmark_return")
        metrics["excess_return"] = benchmark.get("excess_return")
        return _serialize_decimal(
            {
                "period": window,
                "strategy_family": strategy_family,
                "execution_mode": execution_mode,
                **metrics,
            }
        )

    def get_benchmark_comparison(
        self,
        *,
        period: str = "month",
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        benchmark_id: str = "NIFTY50",
    ) -> Dict[str, Any]:
        window = _normalize_period(period)
        anchor = _utcnow()
        start_at = _window_start(anchor, window)
        trades = self.repository.list_trade_rows(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            limit=10000,
        )
        runs = self.repository.list_runs(strategy_family=strategy_family, limit=1000)
        if execution_mode is not None:
            runs = [run for run in runs if str(run.execution_mode) == str(execution_mode)]
        if start_at is not None:
            trades = [
                trade for trade in trades if ((_coerce_datetime(trade.get("fill_timestamp")) or anchor) >= start_at)
            ]
            runs = [run for run in runs if ((_coerce_datetime(run.started_at) or anchor) >= start_at)]

        capital_basis = sum(_to_decimal(getattr(run, "capital_committed", None), default=ZERO) for run in runs)
        if capital_basis <= ZERO:
            capital_basis = Decimal("1")

        daily_net: Dict[date, Decimal] = defaultdict(lambda: ZERO)
        for trade in trades:
            fill_at = _coerce_datetime(trade.get("fill_timestamp"))
            if fill_at is None:
                continue
            gross = _to_decimal(trade.get("gross_cash_flow")) if trade.get("gross_cash_flow") is not None else ZERO
            fees_total = _to_decimal(trade.get("fees_amount")) + _to_decimal(trade.get("taxes_amount")) + _to_decimal(trade.get("slippage_amount"))
            daily_net[fill_at.date()] += gross - fees_total

        benchmark_prices = self.repository.list_benchmark_prices(
            benchmark_id,
            start_day=start_at.date() if start_at else None,
            end_day=anchor.date(),
        )
        aggregate_points = [
            JournalEquityPoint(
                subject_type="portfolio",
                subject_id="aggregate",
                interval="1d",
                as_of=_normalize_interval_day(trading_day),
                starting_equity=capital_basis,
                ending_equity=capital_basis + pnl,
                realized_pnl=pnl,
                return_pct=(pnl / capital_basis) if capital_basis > ZERO else None,
            )
            for trading_day, pnl in sorted(daily_net.items())
        ]
        comparison = compare_return_series(aggregate_points, benchmark_prices)
        portfolio_series = [
            {"date": point.trading_day.isoformat(), "value": float(point.subject_cumulative_return)}
            for point in comparison
        ]
        benchmark_series = [
            {"date": point.trading_day.isoformat(), "value": float(point.benchmark_cumulative_return)}
            for point in comparison
        ]
        benchmark_return = comparison[-1].benchmark_cumulative_return if comparison else None
        portfolio_return = comparison[-1].subject_cumulative_return if comparison else None
        return _serialize_decimal(
            {
                "benchmark_id": benchmark_id,
                "benchmark_name": benchmark_id,
                "period": window,
                "portfolio_return": portfolio_return,
                "benchmark_return": benchmark_return,
                "excess_return": (portfolio_return - benchmark_return) if portfolio_return is not None and benchmark_return is not None else None,
                "portfolio_series": portfolio_series,
                "benchmark_series": benchmark_series,
            }
        )

    def get_aggregate_summaries(
        self,
        *,
        now: Optional[datetime] = None,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        anchor = now or _utcnow()
        trades = self.repository.list_trade_rows(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            limit=10000,
        )
        runs = self.repository.list_runs(strategy_family=strategy_family, limit=1000)
        result: Dict[str, Any] = {}
        for window in AGGREGATE_WINDOWS:
            start_at = _window_start(anchor, window)
            scoped_trades = [
                trade
                for trade in trades
                if start_at is None or ((_coerce_datetime(trade.get("fill_timestamp")) or anchor) >= start_at)
            ]
            scoped_runs = [
                run
                for run in runs
                if execution_mode is None or str(run.execution_mode) == str(execution_mode)
            ]
            if start_at is not None:
                scoped_runs = [run for run in scoped_runs if (_coerce_datetime(run.started_at) or anchor) >= start_at]
            pnls = [
                _to_decimal(row.get("gross_cash_flow")) - (_to_decimal(row.get("fees_amount")) + _to_decimal(row.get("taxes_amount")) + _to_decimal(row.get("slippage_amount")))
                for row in scoped_trades
                if row.get("gross_cash_flow") is not None
            ]
            fee_values = [
                _to_decimal(row.get("fees_amount")) + _to_decimal(row.get("taxes_amount")) + _to_decimal(row.get("slippage_amount"))
                for row in scoped_trades
            ]
            daily_net: Dict[date, Decimal] = defaultdict(lambda: ZERO)
            for row in scoped_trades:
                fill_at = _coerce_datetime(row.get("fill_timestamp"))
                if fill_at is None:
                    continue
                gross = _to_decimal(row.get("gross_cash_flow")) if row.get("gross_cash_flow") is not None else ZERO
                fees_total = _to_decimal(row.get("fees_amount")) + _to_decimal(row.get("taxes_amount")) + _to_decimal(row.get("slippage_amount"))
                daily_net[fill_at.date()] += gross - fees_total
            metrics = _safe_metrics(
                {
                    "window": window,
                    "run_count": len(scoped_runs),
                    "closed_run_count": len([run for run in scoped_runs if str(run.status) in {"closed", "reviewed"}]),
                    "execution_fact_count": len(scoped_trades),
                    "trading_day_count": len(daily_net),
                    "gross_profit": gross_profit(pnls),
                    "gross_loss": gross_loss(pnls),
                    "net_pnl": net_pnl(pnls),
                    "total_fees": total_fees(fee_values),
                    "win_rate": win_rate(pnls),
                    "average_win": average_win(pnls),
                    "average_loss": average_loss(pnls),
                    "profit_factor": profit_factor(pnls),
                    "expectancy": expectancy(pnls),
                    "max_win_streak": streaks(pnls).get("max_win_streak", 0),
                    "max_loss_streak": streaks(pnls).get("max_loss_streak", 0),
                    "review_completion_rate": _safe_ratio(
                        Decimal(len([run for run in scoped_runs if str(run.review_state) == "reviewed"])),
                        Decimal(len(scoped_runs)),
                    ) if scoped_runs else None,
                    "rule_adherence_rate": None,
                }
            )
            result[window] = _serialize_decimal(
                {
                    "window": window,
                    "start_at": start_at,
                    "end_at": anchor,
                    "metrics": metrics,
                }
            )
        return result

    def link_source(self, run_id: str, link: JournalSourceLink) -> JournalSourceLink:
        self._require_run(run_id)
        normalized = JournalSourceLink(
            run_id=run_id,
            source_type=link.source_type,
            source_key=link.source_key,
            source_key_2=link.source_key_2,
            linked_at=link.linked_at,
        )
        link_id = self.repository.link_source(normalized)
        payload = normalized.model_dump(mode="python")
        payload["id"] = link_id
        linked = JournalSourceLink(**payload)
        self._refresh_source_summary(run_id)
        return linked

    def resolve_run_id(
        self,
        *,
        journal_run_id: Optional[str] = None,
        journal_ref: Optional[Any] = None,
        source_type: Optional[str] = None,
        source_key: Optional[str] = None,
        source_key_2: Optional[str] = None,
    ) -> Optional[str]:
        direct = str(journal_run_id or "").strip()
        if direct and _looks_like_uuid(direct):
            run = self.repository.get_run(direct)
            return str(run.id) if run else None

        ref_payload: Dict[str, Any] = {}
        if isinstance(journal_ref, dict):
            ref_payload = dict(journal_ref)
        elif isinstance(journal_ref, str):
            ref_text = journal_ref.strip()
            if ref_text:
                if _looks_like_uuid(ref_text):
                    run = self.repository.get_run(ref_text)
                    if run is not None:
                        return str(run.id)
                parts = ref_text.split(":")
                if len(parts) == 2 and parts[0] == "run":
                    if _looks_like_uuid(parts[1]):
                        run = self.repository.get_run(parts[1])
                        return str(run.id) if run is not None else None
                    return None
                if len(parts) >= 2:
                    ref_payload = {
                        "source_type": parts[0],
                        "source_key": parts[1],
                        "source_key_2": ":".join(parts[2:]) or None,
                    }

        resolved_source_type = str(_enum_value(source_type or ref_payload.get("source_type") or "")).strip()
        resolved_source_key = str(source_key or ref_payload.get("source_key") or "").strip()
        resolved_source_key_2 = source_key_2 if source_key_2 is not None else ref_payload.get("source_key_2")
        if not resolved_source_type or not resolved_source_key:
            return None
        link = self.repository.find_source_link(
            source_type=resolved_source_type,
            source_key=resolved_source_key,
            source_key_2=resolved_source_key_2,
        )
        return str(link.run_id) if link else None

    def ensure_paper_strategy_run(self, *, attribution: Dict[str, Any]) -> Optional[str]:
        strategy_run_id = str(attribution.get("strategy_run_id") or "").strip()
        account_ref = str(attribution.get("account_ref") or attribution.get("account_scope") or "").strip()
        if not strategy_run_id or not account_ref:
            return None
        strategy_family_value = str(attribution.get("strategy_family") or StrategyFamily.INDICATOR.value).strip()
        try:
            strategy_family = StrategyFamily(strategy_family_value)
        except ValueError:
            strategy_family = StrategyFamily.INDICATOR

        existing = self.repository.find_source_link(
            source_type=SourceType.PAPER_STRATEGY_RUN,
            source_key=strategy_run_id,
            source_key_2=account_ref,
        )
        if existing is not None:
            return str(existing.run_id)

        run = self.create_run(
            JournalRun(
                strategy_family=strategy_family,
                strategy_name=str(attribution.get("strategy_name") or strategy_run_id),
                entry_surface=str(attribution.get("entry_surface") or "paper_runtime"),
                execution_mode=ExecutionMode(str(attribution.get("execution_mode") or ExecutionMode.PAPER.value)),
                account_ref=account_ref,
                status=JournalRunStatus.OPEN,
                benchmark_id="NIFTY50",
                capital_basis_type=CapitalBasisType.MARGIN_USED,
                review_state=ReviewState.PENDING,
                source_summary={"source": "paper_runtime", "strategy_run_id": strategy_run_id},
                metadata={"created_by": "paper_runtime", "paper_attribution": _serialize_decimal(attribution)},
            ),
        )
        run_id = str(run.get("id") or "") if isinstance(run, dict) else ""
        if not run_id:
            return None
        self.link_source(
            run_id,
            JournalSourceLink(
                run_id=run_id,
                source_type=SourceType.PAPER_STRATEGY_RUN,
                source_key=str(strategy_run_id),
                source_key_2=account_ref,
            ),
        )
        return run_id

    def mirror_option_strategy_run(
        self,
        *,
        option_strategy_run_id: str,
        underlying: Optional[str],
        expiry: Optional[str],
        user_intent: Optional[str],
        inferred_structure: Optional[str],
        inferred_family: Optional[str],
        execution_mode: str,
        algo_instance_id: Optional[str] = None,
        entry_surface: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        existing = self.repository.find_source_link(
            source_type=SourceType.OPTION_STRATEGY_RUN,
            source_key=str(option_strategy_run_id),
        )
        if existing is not None:
            run_id = str(existing.run_id)
            run = self._require_run(run_id)
            merged_metadata = dict(run.metadata or {})
            existing_payload = dict(merged_metadata.get("option_strategy_run") or {})
            existing_payload.update(
                {
                    "id": str(option_strategy_run_id),
                    "underlying": underlying,
                    "expiry": expiry,
                    "user_intent": user_intent,
                    "inferred_structure": inferred_structure,
                    "inferred_family": inferred_family,
                    "algo_instance_id": algo_instance_id,
                }
            )
            merged_metadata["option_strategy_run"] = existing_payload
            if metadata:
                merged_metadata.update(metadata)
            next_summary = dict(run.source_summary or {})
            next_summary["option_strategy_sync"] = {
                "last_synced_at": _utcnow().isoformat(),
                "option_strategy_run_id": str(option_strategy_run_id),
            }
            self.repository.update_run_fields(
                run_id,
                strategy_name=inferred_structure or user_intent or run.strategy_name,
                entry_surface=entry_surface or run.entry_surface or "quick_trade",
                source_summary=next_summary,
                metadata=merged_metadata,
            )
            return run_id

        run_metadata = {
            "option_strategy_run": {
                "id": str(option_strategy_run_id),
                "underlying": underlying,
                "expiry": expiry,
                "user_intent": user_intent,
                "inferred_structure": inferred_structure,
                "inferred_family": inferred_family,
                "algo_instance_id": algo_instance_id,
            },
        }
        if entry_surface:
            run_metadata["entry_surface"] = entry_surface
        if metadata:
            run_metadata.update(metadata)

        created = self.create_run(
            JournalRun(
                strategy_family="options_strategy",
                strategy_name=inferred_structure or user_intent,
                entry_surface=entry_surface or "quick_trade",
                execution_mode=execution_mode or "paper",
                status="open",
                capital_basis_type="margin_used",
                metadata=run_metadata,
            ),
            source_links=[
                JournalSourceLink(
                    run_id="placeholder",
                    source_type=SourceType.OPTION_STRATEGY_RUN,
                    source_key=str(option_strategy_run_id),
                )
            ],
        )
        return str((created.get("run") or {}).get("id"))

    def sync_option_strategy_lifecycle(
        self,
        *,
        option_strategy_run_id: str,
        status: str,
        execution_result: Optional[Dict[str, Any]] = None,
        algo_instance_id: Optional[str] = None,
    ) -> Optional[str]:
        resolved_run_id = self.resolve_run_id(
            source_type=SourceType.OPTION_STRATEGY_RUN,
            source_key=str(option_strategy_run_id),
        )
        if not resolved_run_id:
            return None
        run = self._require_run(resolved_run_id)
        merged_metadata = dict(run.metadata or {})
        option_strategy_metadata = dict(merged_metadata.get("option_strategy_run") or {})
        option_strategy_metadata.update(
            {
                "id": str(option_strategy_run_id),
                "status": status,
                "algo_instance_id": algo_instance_id or option_strategy_metadata.get("algo_instance_id"),
                "last_synced_at": _utcnow().isoformat(),
            }
        )
        if execution_result is not None:
            option_strategy_metadata["execution_result"] = execution_result
        merged_metadata["option_strategy_run"] = option_strategy_metadata
        self.repository.update_run_fields(
            resolved_run_id,
            status=_option_strategy_journal_status(status),
            review_state=_option_strategy_review_state(status, execution_result),
            ended_at=_utcnow() if status in {"success", "failed", "partial", "cancelled", "canceled"} else None,
            metadata=merged_metadata,
        )
        return resolved_run_id

    def ensure_investment_run(
        self,
        *,
        portfolio_tag: str,
        strategy_name: str,
        execution_mode: str = "live",
        source_key_2: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        existing = self.repository.find_source_link(
            source_type=SourceType.INVESTING_STRATEGY,
            source_key=str(portfolio_tag),
            source_key_2=source_key_2,
        )
        if existing is not None:
            return str(existing.run_id)

        created = self.create_run(
            JournalRun(
                strategy_family="investment_strategy",
                strategy_name=strategy_name,
                execution_mode=execution_mode or "live",
                status="open",
                capital_basis_type="portfolio_nav",
                metadata=metadata or {},
            ),
            source_links=[
                JournalSourceLink(
                    run_id="placeholder",
                    source_type=SourceType.INVESTING_STRATEGY,
                    source_key=str(portfolio_tag),
                    source_key_2=source_key_2,
                )
            ],
        )
        return str((created.get("run") or {}).get("id"))

    def record_paper_order(self, *, run_id: str, order_id: str) -> None:
        self.link_source(
            run_id,
            JournalSourceLink(
                run_id=run_id,
                source_type=SourceType.PAPER_ORDER,
                source_key=str(order_id),
            ),
        )

    def record_paper_trade(
        self,
        *,
        run_id: str,
        trade_id: str,
        order_id: Optional[str],
        trade_timestamp: datetime,
        side: str,
        quantity: int,
        price: Any,
        gross_cash_flow: Optional[Any] = None,
        fees_amount: Any = ZERO,
        taxes_amount: Any = ZERO,
        slippage_amount: Any = ZERO,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.link_source(
            run_id,
            JournalSourceLink(
                run_id=run_id,
                source_type=SourceType.PAPER_TRADE,
                source_key=str(trade_id),
                source_key_2=str(order_id) if order_id else None,
            ),
        )
        price_decimal = _to_decimal(price)
        computed_cash_flow = (
            _to_decimal(gross_cash_flow)
            if gross_cash_flow is not None
            else signed_cash_flow(
                side=side,
                price=price_decimal,
                quantity=int(quantity),
            )
        )
        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=run_id,
                source_type=SourceType.PAPER_TRADE,
                source_fact_key=str(trade_id),
                order_id=str(order_id) if order_id else None,
                trade_id=str(trade_id),
                fill_timestamp=trade_timestamp,
                side=side,
                quantity=int(quantity),
                price=price_decimal,
                gross_cash_flow=computed_cash_flow,
                fees_amount=_to_decimal(fees_amount),
                taxes_amount=_to_decimal(taxes_amount),
                slippage_amount=_to_decimal(slippage_amount),
                payload=payload or {},
            )
        )
        run = self.repository.get_run(run_id)
        if run is None:
            return
        timeline_meta = dict((run.metadata or {}).get("journal_v2") or {})
        environment_id = timeline_meta.get("environment_id")
        if environment_id:
            self._append_timeline_event_best_effort(
                environment_id=str(environment_id),
                execution_context_id=timeline_meta.get("execution_context_id"),
                episode_id=timeline_meta.get("episode_id"),
                subject_type="execution_fact",
                subject_id=str(trade_id),
                channel="fills",
                event_type="fill_recorded",
                occurred_at=trade_timestamp,
                payload={
                    "run_id": run_id,
                    "trade_id": str(trade_id),
                    "order_id": str(order_id) if order_id else None,
                    "side": side,
                    "quantity": int(quantity),
                    "price": str(price_decimal),
                },
            )
        attribution_payload = dict(payload or {})
        account_scope = str(attribution_payload.get("account_ref") or attribution_payload.get("account_scope") or run.account_ref or "").strip()
        external_run_id = str(attribution_payload.get("strategy_run_id") or run_id).strip()
        if account_scope and external_run_id:
            try:
                self.record_v2_execution_fill(
                    mode="paper",
                    account_scope=account_scope,
                    source_system=str(attribution_payload.get("source_system") or attribution_payload.get("source") or "paper_runtime"),
                    external_run_id=external_run_id,
                    source_type=SourceType.PAPER_TRADE,
                    source_fact_key=str(trade_id),
                    side=side,
                    quantity=int(quantity),
                    price=price_decimal,
                    fill_timestamp=trade_timestamp,
                    gross_cash_flow=computed_cash_flow,
                    fees_amount=_to_decimal(fees_amount),
                    taxes_amount=_to_decimal(taxes_amount),
                    slippage_amount=_to_decimal(slippage_amount),
                    run_id=run_id,
                    order_id=str(order_id) if order_id else None,
                    trade_id=str(trade_id),
                    attribution=attribution_payload,
                    payload={
                        "paper_trade": attribution_payload,
                        "run_id": run_id,
                    },
                )
            except Exception:
                logger.warning(
                    "journal_v2.paper_fill_projection_failed",
                    extra={"run_id": run_id, "trade_id": str(trade_id)},
                    exc_info=True,
                )

    def _normalize_attribution_source_system(self, attribution: Dict[str, Any] | None, *, default: str) -> str:
        payload = dict(attribution or {})
        return str(payload.get("source_system") or payload.get("source") or default).strip() or default

    def _ensure_v2_execution_context(
        self,
        *,
        mode: str,
        account_scope: str,
        source_system: str,
        external_run_id: str,
        attribution: Dict[str, Any] | None = None,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int | None = None,
    ) -> tuple[str, str, str | None, str | None, str | None]:
        payload = dict(attribution or {})
        environment_id = self.resolve_v2_environment_id(
            mode=mode,
            account_scope=account_scope,
            broker_user_id=broker_user_id,
            paper_account_key=paper_account_key,
            environment_epoch=environment_epoch,
        )

        resolved_identity = resolve_strategy_identity(
            template_id=payload.get("template_id"),
            worker_template_id=payload.get("worker_template_id"),
            strategy_family=payload.get("strategy_family"),
            strategy_name=payload.get("strategy_name"),
            scenario_key=payload.get("scenario_key"),
            scenario_name=payload.get("scenario_name"),
            deployment_key=payload.get("deployment_key"),
            config_hash=payload.get("config_hash"),
            source_system=source_system,
            entry_surface=payload.get("entry_surface"),
        )
        low_confidence_identity = is_low_confidence_resolution(resolved_identity)
        template_id: str | None = None
        if not low_confidence_identity:
            template_id = self.repository.ensure_strategy_template(
                template_key=resolved_identity.template_id,
                strategy_family=resolved_identity.strategy_family,
                display_name=resolved_identity.display_name,
                metadata={"source_system": source_system},
            )
        variant_id: str | None = None
        if template_id is not None and resolved_identity.variant_key:
            variant_id = self.repository.ensure_strategy_variant(
                template_id=template_id,
                variant_key=resolved_identity.variant_key,
                display_name=str(payload.get("scenario_name") or "").strip() or None,
                metadata={"scenario_key": payload.get("scenario_key"), "config_hash": payload.get("config_hash")},
            )
        deployment_id: str | None = None
        if template_id is not None and resolved_identity.deployment_key:
            deployment_id = self.repository.ensure_strategy_deployment(
                template_id=template_id,
                variant_id=variant_id,
                deployment_key=resolved_identity.deployment_key,
                display_name=str(payload.get("deployment_key") or "").strip() or None,
                metadata={"source_system": source_system},
            )

        context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system=source_system,
            external_run_id=external_run_id,
            template_id=template_id,
            variant_id=variant_id,
            deployment_id=deployment_id,
            raw_identity=resolved_identity.raw_identity,
            resolved_identity=resolved_identity.resolved_identity,
            resolution_method=resolved_identity.resolution_method,
            resolution_confidence=resolved_identity.resolution_confidence,
            identity_rule_version=resolved_identity.identity_rule_version,
            metadata={
                "grouping_rule_version": resolved_identity.grouping_rule_version,
                "strategy_template_id": template_id,
                "strategy_variant_id": variant_id,
                "strategy_deployment_id": deployment_id,
            },
        )
        if low_confidence_identity:
            self._queue_unresolved_identity(
                environment_id=environment_id,
                execution_context_id=context_id,
                source_system=source_system,
                identity=resolved_identity,
            )
        return environment_id, context_id, template_id, variant_id, deployment_id

    def _resolve_episode_for_fill(
        self,
        *,
        environment_id: str,
        execution_context_id: str,
        instrument_key: str,
        side: str,
        quantity: int,
        fill_timestamp: datetime,
    ) -> tuple[str, str, int, int]:
        episodes = self.repository.list_episodes(environment_id=environment_id, execution_context_id=execution_context_id, limit=200)
        sequence_values = [int(item.episode_seq) for item in episodes]
        active_episode = None
        for episode in episodes:
            if str(episode.status) not in {"closed", "cancelled", "unresolved"}:
                active_episode = episode
                break

        previous_qty = 0
        if active_episode is not None:
            position_map = dict((active_episode.metadata or {}).get("net_quantity_by_instrument") or {})
            previous_qty = int(position_map.get(instrument_key) or 0)

        if active_episode is None:
            episode_id = self.repository.ensure_episode(
                environment_id=environment_id,
                execution_context_id=execution_context_id,
                episode_seq=next_episode_sequence(sequence_values),
                status="open",
                opened_at=fill_timestamp,
                metadata={"net_quantity_by_instrument": {instrument_key: 0}},
            )
            active_episode = self.repository.get_episode_detail(episode_id)
            previous_qty = 0
        if active_episode is None:
            raise RuntimeError("Failed to resolve active V2 episode")

        position_effect = classify_position_effect(previous_qty=previous_qty, side=side, quantity=quantity)
        delta = int(quantity) if str(side or "").upper() == "BUY" else -int(quantity)
        position_map = dict((active_episode.metadata or {}).get("net_quantity_by_instrument") or {})

        def _all_positions_flat(values: Dict[str, Any]) -> bool:
            return all(int(value or 0) == 0 for value in values.values())

        if position_effect == "flip":
            current_episode_id = str(active_episode.id)
            other_position_map = dict(position_map)
            other_position_map[instrument_key] = 0
            if not _all_positions_flat(other_position_map):
                position_map[instrument_key] = previous_qty + delta
                self.repository.update_episode_status(
                    current_episode_id,
                    status="open",
                    metadata={"net_quantity_by_instrument": position_map},
                )
                return current_episode_id, position_effect, previous_qty, previous_qty + delta

            self.repository.update_episode_status(
                current_episode_id,
                status="closed",
                closed_at=fill_timestamp,
                metadata={"close_reason": "position_flip", "net_quantity_by_instrument": other_position_map},
            )
            next_seq = next_episode_sequence(sequence_values)
            episode_id = self.repository.ensure_episode(
                environment_id=environment_id,
                execution_context_id=execution_context_id,
                episode_seq=next_seq,
                status="open",
                opened_at=fill_timestamp,
                metadata={"net_quantity_by_instrument": {instrument_key: delta}},
            )
            return episode_id, position_effect, 0, delta

        next_qty = previous_qty + delta
        episode_id = str(active_episode.id)
        position_map[instrument_key] = next_qty
        episode_is_flat = _all_positions_flat(position_map)
        self.repository.update_episode_status(
            episode_id,
            status="closed" if episode_is_flat else "open",
            closed_at=fill_timestamp if episode_is_flat else None,
            metadata={"net_quantity_by_instrument": position_map},
        )
        return episode_id, position_effect, previous_qty, next_qty

    def record_v2_execution_fill(
        self,
        *,
        mode: str,
        account_scope: str,
        source_system: str,
        external_run_id: str,
        source_type: SourceType | str,
        source_fact_key: str,
        side: str,
        quantity: int,
        price: Any,
        fill_timestamp: datetime,
        gross_cash_flow: Any,
        fees_amount: Any = ZERO,
        taxes_amount: Any = ZERO,
        slippage_amount: Any = ZERO,
        cost_contract: ExecutionCostContract | dict[str, Any] | None = None,
        run_id: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        attribution: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
        broker_user_id: str | None = None,
        paper_account_key: str | None = None,
        environment_epoch: int | None = None,
    ) -> Dict[str, Any]:
        normalized_source_system = str(source_system or "").strip() or "journal"
        normalized_external_run_id = str(external_run_id or "").strip()
        if not normalized_external_run_id:
            raise ValueError("external_run_id is required for v2 execution fill")
        normalized_source_type = source_type if isinstance(source_type, SourceType) else SourceType(str(source_type))
        source_type_value = str(getattr(normalized_source_type, "value", normalized_source_type))
        claim_projection = getattr(self.repository, "claim_v2_projection_source", None)
        mark_projection = getattr(self.repository, "mark_v2_projection_source", None)
        if callable(claim_projection):
            claimed = bool(claim_projection(source_type=source_type_value, source_fact_key=str(source_fact_key)))
            if not claimed:
                find_existing_after_claim = getattr(self.repository, "find_v2_execution_fact_by_source", None)
                existing_after_claim = (
                    find_existing_after_claim(source_type=source_type_value, source_fact_key=str(source_fact_key))
                    if callable(find_existing_after_claim)
                    else None
                )
                return {
                    "environment_id": str(getattr(existing_after_claim, "environment_id", None) or ""),
                    "execution_context_id": None,
                    "episode_id": str(getattr(existing_after_claim, "episode_id", None) or "") or None,
                    "intent_id": str(getattr(existing_after_claim, "intent_id", None) or "") or None,
                    "position_effect": getattr(existing_after_claim, "position_effect", None),
                    "duplicate": True,
                    "pending": existing_after_claim is None,
                }
        find_existing = getattr(self.repository, "find_v2_execution_fact_by_source", None)
        if callable(find_existing):
            existing_fact = find_existing(source_type=source_type_value, source_fact_key=str(source_fact_key))
            if existing_fact is not None and existing_fact.environment_id:
                return {
                    "environment_id": str(existing_fact.environment_id),
                    "execution_context_id": None,
                    "episode_id": str(existing_fact.episode_id) if existing_fact.episode_id else None,
                    "intent_id": str(existing_fact.intent_id) if existing_fact.intent_id else None,
                    "position_effect": existing_fact.position_effect,
                    "duplicate": True,
                }
        attribution_payload = dict(attribution or {})
        fill_payload = dict(payload or {})
        normalized_cost_contract = _normalize_cost_contract(cost_contract)
        if normalized_cost_contract is not None:
            fees_value = normalized_cost_contract.brokerage + normalized_cost_contract.exchange_txn_charge
            taxes_value = (
                normalized_cost_contract.stt
                + normalized_cost_contract.stamp_duty
                + normalized_cost_contract.sebi_charge
                + normalized_cost_contract.gst
            )
            fill_payload["cost_contract"] = normalized_cost_contract.model_dump(mode="json")
        else:
            fees_value = _to_decimal(fees_amount)
            taxes_value = _to_decimal(taxes_amount)
        environment_id, execution_context_id, template_id, variant_id, deployment_id = self._ensure_v2_execution_context(
            mode=mode,
            account_scope=account_scope,
            source_system=normalized_source_system,
            external_run_id=normalized_external_run_id,
            attribution=attribution_payload,
            broker_user_id=broker_user_id,
            paper_account_key=paper_account_key,
            environment_epoch=environment_epoch,
        )

        instrument_token = fill_payload.get("instrument_token")
        if instrument_token is None:
            instrument_token = attribution_payload.get("instrument_token")
        if instrument_token is None and isinstance(fill_payload.get("broker_fill"), dict):
            instrument_token = fill_payload.get("broker_fill", {}).get("instrument_token")
        product_value = (
            fill_payload.get("product")
            or attribution_payload.get("product")
            or (fill_payload.get("broker_fill", {}) if isinstance(fill_payload.get("broker_fill"), dict) else {}).get("product")
            or ""
        )
        instrument_key = f"{instrument_token or 'unknown'}:{str(product_value or '').upper()}"

        episode_id, position_effect, previous_qty, next_qty = self._resolve_episode_for_fill(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            instrument_key=instrument_key,
            side=side,
            quantity=int(quantity),
            fill_timestamp=fill_timestamp,
        )

        intent_id = self.repository.create_execution_intent(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_id=episode_id,
            channel="fills",
            intent_type=str(source_type),
            idempotency_key=f"{source_fact_key}:intent",
            status="resolved",
            requested_at=fill_timestamp,
            resolved_at=fill_timestamp,
            payload={
                "source_system": normalized_source_system,
                "external_run_id": normalized_external_run_id,
                "source_fact_key": source_fact_key,
            },
            result={"position_effect": position_effect, "previous_qty": previous_qty, "next_qty": next_qty},
            metadata={
                "strategy_template_id": template_id,
                "strategy_variant_id": variant_id,
                "strategy_deployment_id": deployment_id,
            },
        )

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required for v2 execution fill")

        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=normalized_run_id,
                environment_id=environment_id,
                episode_id=episode_id,
                intent_id=intent_id,
                source_type=normalized_source_type,
                source_fact_key=source_fact_key,
                order_id=order_id,
                trade_id=trade_id,
                fill_timestamp=fill_timestamp,
                side=side,
                quantity=int(quantity),
                price=_to_decimal(price),
                gross_cash_flow=_to_decimal(gross_cash_flow),
                fees_amount=fees_value,
                taxes_amount=taxes_value,
                slippage_amount=_to_decimal(slippage_amount),
                brokerage=(normalized_cost_contract.brokerage if normalized_cost_contract is not None else None),
                exchange_txn_charge=(normalized_cost_contract.exchange_txn_charge if normalized_cost_contract is not None else None),
                stt=(normalized_cost_contract.stt if normalized_cost_contract is not None else None),
                stamp_duty=(normalized_cost_contract.stamp_duty if normalized_cost_contract is not None else None),
                sebi_charge=(normalized_cost_contract.sebi_charge if normalized_cost_contract is not None else None),
                gst=(normalized_cost_contract.gst if normalized_cost_contract is not None else None),
                margin_required=(normalized_cost_contract.margin_required if normalized_cost_contract is not None else None),
                charges_status=(str(normalized_cost_contract.charges_status.value) if normalized_cost_contract is not None else None),
                position_effect=position_effect,
                payload={
                    **fill_payload,
                    "attribution": attribution_payload,
                    "environment_id": environment_id,
                    "execution_context_id": execution_context_id,
                    "episode_id": episode_id,
                    "intent_id": intent_id,
                },
            )
        )

        if callable(mark_projection):
            mark_projection(source_type=source_type_value, source_fact_key=str(source_fact_key), status="projected")

        self._append_timeline_event_best_effort(
            environment_id=environment_id,
            execution_context_id=execution_context_id,
            episode_id=episode_id,
            subject_type="execution_fact",
            subject_id=str(trade_id or source_fact_key),
            channel="fills",
            event_type="fill_recorded",
            occurred_at=fill_timestamp,
            payload={
                    "source_fact_key": source_fact_key,
                    "source_type": str(normalized_source_type),
                    "side": side,
                    "quantity": int(quantity),
                    "position_effect": position_effect,
            },
        )
        return {
            "environment_id": environment_id,
            "execution_context_id": execution_context_id,
            "episode_id": episode_id,
            "intent_id": intent_id,
            "position_effect": position_effect,
        }

    def backfill_v1_review_notes_to_v2(
        self,
        *,
        apply: bool,
        limit: int,
        mode: str | None = None,
        account_scope: str | None = None,
    ) -> Dict[str, Any]:
        mode_filter = str(mode or "").strip().lower() or None
        if mode_filter not in {None, "live", "paper"}:
            raise ValueError("mode must be one of: live, paper")

        scanned = 0
        created = 0
        updated = 0
        unresolved = 0
        skipped = 0
        failed = 0
        candidates = self.repository.list_v1_review_note_candidates(
            limit=max(1, int(limit)),
            environment_mode=mode_filter,
            account_scope=(str(account_scope or "").strip() or None),
        )
        preview: List[Dict[str, Any]] = []

        for row in candidates:
            scanned += 1
            run_id = str(row.get("id") or "").strip()
            review_notes = str(row.get("review_notes") or "").strip()
            execution_mode = str(row.get("execution_mode") or "").strip().lower()
            if not run_id or not review_notes:
                skipped += 1
                continue

            resolved_mode = "paper" if execution_mode == "paper" else "live"
            account_scope = str(row.get("account_ref") or "").strip()
            if not account_scope:
                account_scope = f"legacy:{resolved_mode}:{run_id}"

            source_links = self.repository.list_source_links(run_id)
            resolution_confidence = "0.90" if source_links else "0.55"
            preview_item = {
                "run_id": run_id,
                "execution_mode": resolved_mode,
                "account_scope": account_scope,
                "resolution_confidence": resolution_confidence,
            }
            preview.append(preview_item)
            if not apply:
                continue
            try:
                environment_id = self.resolve_v2_environment_id(mode=resolved_mode, account_scope=account_scope)
                context_id = self.repository.ensure_execution_context(
                    environment_id=environment_id,
                    source_system="v1_journal_run",
                    external_run_id=run_id,
                    status="closed",
                    metadata={
                        "identity_rule_version": "v1_legacy_backfill",
                        "resolution_confidence": resolution_confidence,
                    },
                )
                episode_id = self.repository.ensure_episode(
                    environment_id=environment_id,
                    execution_context_id=context_id,
                    episode_seq=1,
                    status="closed",
                    metadata={
                        "identity_rule_version": "v1_legacy_backfill",
                        "resolution_confidence": resolution_confidence,
                    },
                )
                note_id = self.create_v2_note(
                    environment_id=environment_id,
                    subject_type="run",
                    subject_id=run_id,
                    episode_id=episode_id,
                    note_type="post_exit_review",
                    title="Backfilled V1 review notes",
                    body_markdown=review_notes,
                    metadata={
                        "source": "v1_review_notes",
                        "identity_rule_version": "v1_legacy_backfill",
                        "resolution_confidence": resolution_confidence,
                    },
                )
                created += 1

                decision_events = self.repository.list_decision_events(run_id)
                for decision in decision_events:
                    self._append_timeline_event_best_effort(
                        environment_id=environment_id,
                        execution_context_id=context_id,
                        episode_id=episode_id,
                        subject_type="run",
                        subject_id=run_id,
                        channel="decision",
                        event_type="legacy_decision_event_backfilled",
                        occurred_at=decision.occurred_at,
                        payload={
                            "decision_event_id": decision.id,
                            "decision_type": decision.decision_type,
                            "actor_type": decision.actor_type,
                            "summary": decision.summary,
                            "context": decision.context,
                            "source": "v1_decision_events",
                            "backfilled_note_id": note_id,
                            "identity_rule_version": "v1_legacy_backfill",
                            "resolution_confidence": resolution_confidence,
                        },
                    )
                    updated += 1
            except LookupError:
                unresolved += 1
            except Exception:
                failed += 1

        return {
            "apply": apply,
            "limit": max(1, int(limit)),
            "mode": mode_filter,
            "account_scope": (str(account_scope or "").strip() or None),
            "scanned": scanned,
            "created": created,
            "updated": updated,
            "unresolved": unresolved,
            "skipped": skipped,
            "failed": failed,
            "items": preview,
        }

    def backfill_journal_v2(
        self,
        *,
        apply: bool,
        limit: int,
        mode: str | None = None,
        account_scope: str | None = None,
    ) -> Dict[str, Any]:
        return self.backfill_v1_review_notes_to_v2(
            apply=apply,
            limit=limit,
            mode=mode,
            account_scope=account_scope,
        )

    def update_run_review(self, run_id: str, *, review_status: str, notes: Optional[str] = None) -> Dict[str, Any]:
        mapped_review_state = {
            "pending": "pending",
            "in_progress": "in_progress",
            "completed": "reviewed",
            "reviewed": "reviewed",
            "skipped": "waived",
            "waived": "waived",
        }.get(str(review_status), str(review_status))
        run = self._require_run(run_id)
        metadata = dict(run.metadata or {})
        if notes is not None:
            metadata["review_notes"] = notes
        return self.update_run(run_id, review_state=mapped_review_state, metadata=metadata)

    def append_decision_event(self, run_id: str, event: JournalDecisionEvent) -> JournalDecisionEvent:
        self._require_run(run_id)
        normalized = JournalDecisionEvent(
            run_id=run_id,
            decision_type=event.decision_type,
            actor_type=event.actor_type,
            occurred_at=event.occurred_at,
            summary=event.summary,
            context=event.context,
        )
        event_id = self.repository.append_decision_event(normalized)
        payload = normalized.model_dump(mode="python")
        payload["id"] = event_id
        return JournalDecisionEvent(**payload)

    def get_run_detail(self, run_id: str) -> Dict[str, Any]:
        run = self._require_run(run_id)
        detail = {
            "run": _serialize_decimal(run.model_dump(mode="python")),
            "legs": _serialize_decimal([leg.model_dump(mode="python") for leg in self.repository.list_run_legs(run_id)]),
            "sources": _serialize_decimal([link.model_dump(mode="python") for link in self.repository.list_source_links(run_id)]),
            "decision_events": _serialize_decimal([event.model_dump(mode="python") for event in self.repository.list_decision_events(run_id)]),
            "execution_facts": _serialize_decimal([fact.model_dump(mode="python") for fact in self.repository.list_execution_facts(run_id)]),
        }
        snapshot = self.repository.get_latest_metric_snapshot(subject_type="run", subject_id=run_id, window="since_inception")
        if snapshot is not None:
            detail["summary_metrics"] = _serialize_decimal(snapshot.metrics)
            detail["summary_metrics_computed_at"] = snapshot.computed_at.isoformat()
        return detail

    def list_runs(
        self,
        *,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        status: Optional[str] = None,
        review_state: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        runs = self.repository.list_runs(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            status=status,
            review_state=review_state,
            limit=limit,
            offset=offset,
        )
        items: List[Dict[str, Any]] = []
        for run in runs:
            payload = _serialize_decimal(run.model_dump(mode="python"))
            snapshot = self.repository.get_latest_metric_snapshot(
                subject_type="run",
                subject_id=str(run.id),
                window="since_inception",
            )
            metrics = snapshot.metrics if snapshot is not None else {}
            payload["net_pnl"] = _serialize_decimal(metrics.get("net_pnl")) if metrics.get("net_pnl") is not None else None
            payload["total_fees"] = _serialize_decimal(metrics.get("total_fees")) if metrics.get("total_fees") is not None else None
            items.append(payload)
        return items

    def list_runs_page(
        self,
        *,
        strategy_family: Optional[str] = None,
        execution_mode: Optional[str] = None,
        status: Optional[str] = None,
        review_state: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page))
        safe_page_size = max(1, int(page_size))
        offset = (safe_page - 1) * safe_page_size
        items = self.list_runs(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            status=status,
            review_state=review_state,
            limit=safe_page_size,
            offset=offset,
        )
        total = self.repository.count_runs(
            strategy_family=strategy_family,
            execution_mode=execution_mode,
            status=status,
            review_state=review_state,
        )
        return {"items": items, "total": total, "page": safe_page, "page_size": safe_page_size}

    def recompute_run_summary(self, run_id: str, *, calc_version: str = DEFAULT_CALC_VERSION) -> Dict[str, Any]:
        run = self._require_run(run_id)
        facts = self.repository.list_execution_facts(run_id)
        benchmark_prices = self.repository.list_benchmark_prices(run.benchmark_id)

        self.repository.delete_equity_points(subject_type="run", subject_id=run_id, interval="1d")
        equity_points = self._rebuild_run_equity_points(run, facts, benchmark_prices)
        summary_metrics = self._build_run_metrics(run, facts, equity_points)
        snapshot = JournalMetricSnapshot(
            subject_type="run",
            subject_id=run_id,
            window="since_inception",
            calc_version=calc_version,
            computed_at=_utcnow(),
            metrics=summary_metrics,
        )
        self.repository.replace_metric_snapshot(snapshot)
        return {
            "run_id": run_id,
            "metrics": _serialize_decimal(summary_metrics),
            "equity_points": _serialize_decimal([point.model_dump(mode="python") for point in equity_points]),
        }

    def get_run_summary(self, run_id: str, *, calc_version: str = DEFAULT_CALC_VERSION) -> Dict[str, Any]:
        snapshot = self.repository.get_latest_metric_snapshot(
            subject_type="run",
            subject_id=run_id,
            window="since_inception",
            calc_version=calc_version,
        )
        if snapshot is None:
            self.recompute_run_summary(run_id, calc_version=calc_version)
            snapshot = self.repository.get_latest_metric_snapshot(
                subject_type="run",
                subject_id=run_id,
                window="since_inception",
                calc_version=calc_version,
            )
        run = self._require_run(run_id)
        equity_points = self.repository.list_equity_points(subject_type="run", subject_id=run_id, interval="1d")
        benchmark_prices = self.repository.list_benchmark_prices(run.benchmark_id)
        comparison = compare_return_series(equity_points, benchmark_prices)
        return {
            "run_id": run_id,
            "benchmark_id": run.benchmark_id,
            "metrics": _serialize_decimal(snapshot.metrics if snapshot else {}),
            "comparison": _serialize_decimal([
                {
                    "trading_day": point.trading_day,
                    "subject_return_pct": point.subject_return_pct,
                    "benchmark_return_pct": point.benchmark_return_pct,
                    "excess_return_pct": point.excess_return_pct,
                    "subject_cumulative_return": point.subject_cumulative_return,
                    "benchmark_cumulative_return": point.benchmark_cumulative_return,
                    "excess_cumulative_return": point.excess_cumulative_return,
                }
                for point in comparison
            ]),
        }

    def refresh_benchmark_daily_prices(
        self,
        *,
        benchmark_id: str = "NIFTY50",
        start_day: Optional[date] = None,
        end_day: Optional[date] = None,
    ) -> Dict[str, Any]:
        definition = self.repository.get_benchmark_definition(benchmark_id)
        if definition is None:
            raise ValueError(f"Unknown benchmark_id: {benchmark_id}")

        rows = self._load_benchmark_price_rows(definition, start_day=start_day, end_day=end_day)
        upserted = 0
        previous_close: Optional[Decimal] = None
        for row in rows:
            close = _to_decimal(row["close"])
            daily_return = None if previous_close in (None, ZERO) else (close / previous_close) - Decimal("1")
            price = BenchmarkDailyPrice(
                benchmark_id=benchmark_id,
                trading_day=row["trading_day"],
                open=_to_decimal(row.get("open"), default=None) if row.get("open") is not None else None,
                high=_to_decimal(row.get("high"), default=None) if row.get("high") is not None else None,
                low=_to_decimal(row.get("low"), default=None) if row.get("low") is not None else None,
                close=close,
                daily_return=daily_return,
                source=row.get("source") or "historical_index",
            )
            self.repository.upsert_benchmark_daily_price(price)
            previous_close = close
            upserted += 1

        return {
            "benchmark_id": benchmark_id,
            "upserted": upserted,
            "start_day": start_day.isoformat() if start_day else None,
            "end_day": end_day.isoformat() if end_day else None,
        }

    def refresh_due_benchmarks(self, *, benchmark_ids: Iterable[str] = DEFAULT_BENCHMARK_IDS) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for benchmark_id in benchmark_ids:
            try:
                results.append(self.refresh_benchmark_daily_prices(benchmark_id=benchmark_id))
            except Exception as exc:
                results.append({"benchmark_id": benchmark_id, "error": str(exc)})
        return results

    def refresh_recent_run_metrics(self, *, limit: int = 50, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        allowed = {str(status) for status in (statuses or ["open", "closed", "reviewed"])}
        for run in self.repository.list_runs(limit=limit):
            if run.status not in allowed:
                continue
            try:
                results.append(self.recompute_run_summary(str(run.id)))
            except Exception as exc:
                results.append({"run_id": str(run.id), "error": str(exc)})
        return results

    def backfill_option_strategy_runs(self, *, limit: int = 100, apply: bool = False) -> Dict[str, Any]:
        candidates = self._load_unlinked_option_strategy_runs(limit=limit)
        created_run_ids: List[str] = []
        preview_items: List[Dict[str, Any]] = []
        for row in candidates:
            preview_items.append(
                {
                    "option_strategy_run_id": str(row.get("id")),
                    "execution_mode": row.get("execution_mode") or "paper",
                    "status": row.get("status"),
                    "underlying": row.get("underlying"),
                    "user_intent": row.get("user_intent"),
                }
            )
            if not apply:
                continue
            metadata = {
                "option_strategy_run": {
                    "underlying": row.get("underlying"),
                    "expiry": row.get("expiry").isoformat() if row.get("expiry") else None,
                    "user_intent": row.get("user_intent"),
                    "inferred_structure": row.get("inferred_structure"),
                    "inferred_family": row.get("inferred_family"),
                    "algo_instance_id": row.get("algo_instance_id"),
                }
            }
            run = JournalRun(
                strategy_family="options_strategy",
                strategy_name=row.get("inferred_structure") or row.get("user_intent"),
                entry_surface="quick_trade",
                execution_mode=row.get("execution_mode") or "paper",
                account_ref=None,
                status="closed" if row.get("status") == "success" else "open",
                capital_basis_type="margin_used",
                started_at=row.get("created_at") or _utcnow(),
                source_summary={"backfilled_from": "option_strategy_runs"},
                metadata=metadata,
            )
            run_id = self.repository.create_run(run)
            self.repository.link_source(
                JournalSourceLink(
                    run_id=run_id,
                    source_type=SourceType.OPTION_STRATEGY_RUN,
                    source_key=str(row["id"]),
                )
            )
            created_run_ids.append(run_id)
        return {
            "apply": apply,
            "candidate_count": len(candidates),
            "created_run_ids": created_run_ids,
            "candidates": preview_items,
        }

    def _build_run_metrics(
        self,
        run: JournalRun,
        facts: List[Any],
        equity_points: List[JournalEquityPoint],
    ) -> Dict[str, Any]:
        daily_pnls = [point.realized_pnl - point.fees for point in equity_points]
        fee_values = [point.fees for point in equity_points]
        metrics: Dict[str, Any] = {
            "run_id": str(run.id),
            "benchmark_id": run.benchmark_id,
            "execution_fact_count": len(facts),
            "trading_day_count": len(equity_points),
            "gross_profit": gross_profit(daily_pnls),
            "gross_loss": gross_loss(daily_pnls),
            "net_pnl": net_pnl(daily_pnls),
            "total_fees": total_fees(fee_values),
            "win_rate": win_rate(daily_pnls),
            "average_win": average_win(daily_pnls),
            "average_loss": average_loss(daily_pnls),
            "profit_factor": profit_factor(daily_pnls),
            "expectancy": expectancy(daily_pnls),
            "cumulative_return": cumulative_return(equity_points),
            "max_drawdown": max_drawdown_from_equity_points(equity_points),
            "max_drawdown_duration": max_drawdown_duration(equity_points),
            "sharpe_ratio": sharpe_ratio(equity_points),
            "sortino_ratio": sortino_ratio(equity_points),
        }
        metrics.update(streaks(daily_pnls))
        if equity_points:
            metrics["ending_equity"] = equity_points[-1].ending_equity
        elif run.capital_committed is not None:
            metrics["ending_equity"] = run.capital_committed
        return metrics

    def _rebuild_run_equity_points(
        self,
        run: JournalRun,
        facts: List[Any],
        benchmark_prices: List[BenchmarkDailyPrice],
    ) -> List[JournalEquityPoint]:
        daily = defaultdict(lambda: {"realized_pnl": ZERO, "fees": ZERO})
        for fact in facts:
            trading_day = fact.fill_timestamp.date()
            gross_cash_flow = fact.gross_cash_flow
            if gross_cash_flow is None:
                signed_notional = _to_decimal(fact.price) * _to_decimal(fact.quantity)
                gross_cash_flow = signed_notional if str(fact.side).lower() == "sell" else -signed_notional
            fees = _to_decimal(fact.fees_amount) + _to_decimal(fact.taxes_amount) + _to_decimal(fact.slippage_amount)
            daily[trading_day]["realized_pnl"] += _to_decimal(gross_cash_flow)
            daily[trading_day]["fees"] += fees

        benchmark_by_day = {price.trading_day: price for price in benchmark_prices}
        starting_equity = _to_decimal(run.capital_committed, default=ZERO)
        previous_ending = starting_equity
        points: List[JournalEquityPoint] = []
        for trading_day in sorted(daily):
            realized_pnl = daily[trading_day]["realized_pnl"]
            fees = daily[trading_day]["fees"]
            ending_equity = previous_ending + realized_pnl - fees
            return_pct = None if previous_ending == ZERO else (ending_equity - previous_ending) / previous_ending
            benchmark_return = benchmark_by_day.get(trading_day).daily_return if trading_day in benchmark_by_day else None
            point = JournalEquityPoint(
                subject_type="run",
                subject_id=str(run.id),
                interval="1d",
                as_of=_normalize_interval_day(trading_day),
                starting_equity=previous_ending,
                ending_equity=ending_equity,
                realized_pnl=realized_pnl,
                fees=fees,
                return_pct=return_pct,
                benchmark_return_pct=benchmark_return,
                excess_return_pct=(return_pct - benchmark_return) if return_pct is not None and benchmark_return is not None else None,
            )
            self.repository.upsert_equity_point(point)
            points.append(point)
            previous_ending = ending_equity
        return points

    def _refresh_source_summary(self, run_id: str) -> None:
        run = self._require_run(run_id)
        sources = self.repository.list_source_links(run_id)
        counts: Dict[str, int] = defaultdict(int)
        source_keys_by_type: Dict[str, List[str]] = defaultdict(list)
        for source in sources:
            counts[str(source.source_type)] += 1
            source_keys_by_type[str(source.source_type)].append(source.source_key)
        merged = dict(run.source_summary or {})
        merged.update(
            {
                "source_count": len(sources),
                "source_type_counts": dict(counts),
                "source_keys_by_type": dict(source_keys_by_type),
            }
        )
        self.repository.update_run(run_id, source_summary=merged)

    def _require_run(self, run_id: str) -> JournalRun:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        return run

    def _load_benchmark_price_rows(
        self,
        definition: BenchmarkDefinition,
        *,
        start_day: Optional[date],
        end_day: Optional[date],
    ) -> List[Dict[str, Any]]:
        aliases = [definition.name, definition.benchmark_id, str(definition.metadata.get("tradingsymbol") or "").strip()]
        aliases = [alias for alias in aliases if alias]
        instrument_token = definition.instrument_token
        with self.repository.unit_of_work() as db:
            rows = db.execute(
                text(
                    """
                    WITH daily_rows AS (
                        SELECT
                            DATE("timestamp") AS trading_day,
                            MIN(open) AS open,
                            MAX(high) AS high,
                            MIN(low) AS low,
                            MAX(close) AS close,
                            'kite_indices_historical_data' AS source
                        FROM public.kite_indices_historical_data
                        WHERE interval = 'day'
                          AND (:instrument_token IS NULL OR instrument_token = :instrument_token)
                          AND (:start_day IS NULL OR DATE("timestamp") >= :start_day)
                          AND (:end_day IS NULL OR DATE("timestamp") <= :end_day)
                          AND (
                                :instrument_token IS NOT NULL
                                OR tradingsymbol = ANY(:aliases)
                              )
                        GROUP BY DATE("timestamp")
                    )
                    SELECT trading_day, open, high, low, close, source
                    FROM daily_rows
                    ORDER BY trading_day ASC
                    """
                ),
                {
                    "instrument_token": instrument_token,
                    "start_day": start_day,
                    "end_day": end_day,
                    "aliases": aliases,
                },
            ).mappings().all()
        return [dict(row) for row in rows]

    def _load_unlinked_option_strategy_runs(self, *, limit: int) -> List[Dict[str, Any]]:
        with self.repository.unit_of_work() as db:
            rows = db.execute(
                text(
                    """
                    SELECT osr.*
                    FROM public.option_strategy_runs osr
                    LEFT JOIN public.journal_source_links jsl
                      ON jsl.source_type = 'option_strategy_run'
                     AND jsl.source_key = CAST(osr.id AS text)
                    WHERE jsl.id IS NULL
                    ORDER BY osr.created_at ASC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, int(limit))},
            ).mappings().all()
        return [dict(row) for row in rows]
