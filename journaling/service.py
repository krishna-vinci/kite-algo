from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

from .benchmark import compare_return_series
from .metrics import (
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
from .models import (
    BenchmarkDailyPrice,
    BenchmarkDefinition,
    JournalDecisionEvent,
    JournalEquityPoint,
    JournalExecutionFact,
    JournalMetricSnapshot,
    JournalRule,
    JournalRun,
    JournalSourceLink,
    ProjectionState,
    SourceType,
)
from .repository import JournalRepository


ZERO = Decimal("0")
DEFAULT_CALC_VERSION = "v1"
DEFAULT_BENCHMARK_IDS = ("NIFTY50",)
AGGREGATE_WINDOWS = ("day", "week", "month", "year", "since_inception")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


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


class JournalService:
    def __init__(self, repository: Optional[JournalRepository] = None) -> None:
        self.repository = repository or JournalRepository()

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

    def list_strategies(self, *, strategy_family: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.repository.list_strategy_rollups(strategy_family=strategy_family, limit=limit)
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
                price=_to_decimal(price),
                payload=payload or {},
            )
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
        status: Optional[str] = None,
        review_state: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        runs = self.repository.list_runs(
            strategy_family=strategy_family,
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
            status=status,
            review_state=review_state,
            limit=safe_page_size,
            offset=offset,
        )
        total = self.repository.count_runs(
            strategy_family=strategy_family,
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
