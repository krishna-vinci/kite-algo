from __future__ import annotations

from datetime import date, datetime, timezone

from tests.test_support import install_dependency_stubs

install_dependency_stubs()

from journaling.models import (  # noqa: E402
    JournalExecutionContext,
    JournalExecutionEnvironment,
    JournalExecutionFact,
    JournalStrategyTemplate,
    JournalTimelineEvent,
    JournalEpisode,
)
from journaling.service import JournalService  # noqa: E402


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


ENV_ID = "00000000-0000-4000-8000-000000000001"
TMPL_ID = "00000000-0000-4000-8000-000000000010"
CTX_1 = "00000000-0000-4000-8000-000000000101"
CTX_2 = "00000000-0000-4000-8000-000000000102"
EP_1 = "00000000-0000-4000-8000-000000000201"
EP_2 = "00000000-0000-4000-8000-000000000202"
EP_3 = "00000000-0000-4000-8000-000000000203"


class _FakeRepo:
    def __init__(self) -> None:
        self.environment = JournalExecutionEnvironment(
            id=ENV_ID,
            mode="paper",
            account_scope="kite:paper-test",
            display_name="Paper Test",
        )
        self.template = JournalStrategyTemplate(
            id=TMPL_ID,
            strategy_family="indicator_strategy",
            template_key="breakout-v1",
            display_name="Breakout V1",
        )
        self.contexts = {
            CTX_1: JournalExecutionContext(
                id=CTX_1,
                environment_id=ENV_ID,
                source_system="algo_worker",
                external_run_id="run-1",
                strategy_template_id=TMPL_ID,
            ),
            CTX_2: JournalExecutionContext(
                id=CTX_2,
                environment_id=ENV_ID,
                source_system="algo_worker",
                external_run_id="run-2",
            ),
        }
        self.episodes = {
            EP_1: JournalEpisode(
                id=EP_1,
                environment_id=ENV_ID,
                execution_context_id=CTX_1,
                episode_seq=1,
                status="closed",
                opened_at=_dt("2026-05-02T03:00:00Z"),
                closed_at=_dt("2026-05-02T04:00:00Z"),
                notes="reviewed",
            ),
            EP_2: JournalEpisode(
                id=EP_2,
                environment_id=ENV_ID,
                execution_context_id=CTX_2,
                episode_seq=2,
                status="open",
                opened_at=_dt("2026-05-02T12:00:00Z"),
                closed_at=None,
                notes="",
            ),
            EP_3: JournalEpisode(
                id=EP_3,
                environment_id=ENV_ID,
                execution_context_id=CTX_2,
                episode_seq=3,
                status="closed",
                opened_at=_dt("2026-05-03T04:00:00Z"),
                closed_at=_dt("2026-05-03T05:00:00Z"),
                notes="late close",
            ),
        }
        self.facts = {
            EP_1: [
                JournalExecutionFact(
                    id=1,
                    run_id="run-1",
                    environment_id=ENV_ID,
                    episode_id=EP_1,
                    source_type="paper_trade",
                    source_fact_key="ep1-buy",
                    fill_timestamp=_dt("2026-05-02T03:00:00Z"),
                    side="BUY",
                    quantity=1,
                    price="100",
                    gross_cash_flow="-100",
                    fees_amount="1",
                    payload={"instrument_token": 101, "exchange": "NSE", "tradingsymbol": "ABC", "product": "MIS"},
                ),
                JournalExecutionFact(
                    id=2,
                    run_id="run-1",
                    environment_id=ENV_ID,
                    episode_id=EP_1,
                    source_type="paper_trade",
                    source_fact_key="ep1-sell",
                    fill_timestamp=_dt("2026-05-02T04:00:00Z"),
                    side="SELL",
                    quantity=1,
                    price="120",
                    gross_cash_flow="120",
                    fees_amount="1",
                    payload={"instrument_token": 101, "exchange": "NSE", "tradingsymbol": "ABC", "product": "MIS"},
                ),
            ],
            EP_2: [
                JournalExecutionFact(
                    id=3,
                    run_id="run-2",
                    environment_id=ENV_ID,
                    episode_id=EP_2,
                    source_type="paper_trade",
                    source_fact_key="ep2-buy",
                    fill_timestamp=_dt("2026-05-02T12:00:00Z"),
                    side="BUY",
                    quantity=1,
                    price="50",
                    gross_cash_flow="-50",
                    payload={"instrument_token": 202, "exchange": "NSE", "tradingsymbol": "XYZ", "product": "MIS"},
                )
            ],
            EP_3: [
                JournalExecutionFact(
                    id=4,
                    run_id="run-2",
                    environment_id=ENV_ID,
                    episode_id=EP_3,
                    source_type="paper_trade",
                    source_fact_key="ep3-buy",
                    fill_timestamp=_dt("2026-05-03T04:00:00Z"),
                    side="BUY",
                    quantity=1,
                    price="100",
                    gross_cash_flow="-100",
                    payload={"instrument_token": 202, "exchange": "NSE", "tradingsymbol": "XYZ", "product": "MIS"},
                ),
                JournalExecutionFact(
                    id=5,
                    run_id="run-2",
                    environment_id=ENV_ID,
                    episode_id=EP_3,
                    source_type="paper_trade",
                    source_fact_key="ep3-sell",
                    fill_timestamp=_dt("2026-05-03T05:00:00Z"),
                    side="SELL",
                    quantity=1,
                    price="90",
                    gross_cash_flow="90",
                    payload={"instrument_token": 202, "exchange": "NSE", "tradingsymbol": "XYZ", "product": "MIS"},
                ),
            ],
        }
        self.timeline_events = {
            EP_1: [
                JournalTimelineEvent(
                    id="evt-1",
                    environment_id=ENV_ID,
                    episode_id=EP_1,
                    execution_context_id=CTX_1,
                    subject_type="episode",
                    subject_id=EP_1,
                    event_type="episode_opened",
                    occurred_at=_dt("2026-05-02T03:00:00Z"),
                )
            ]
        }

    def get_execution_environment(self, environment_id: str):
        return self.environment if environment_id == ENV_ID else None

    def get_strategy_template(self, template_id: str):
        return self.template if template_id == TMPL_ID else None

    def get_execution_context(self, context_id: str):
        return self.contexts.get(context_id)

    def list_episodes(self, *, environment_id: str | None = None, **_: object):
        episodes = list(self.episodes.values())
        if environment_id is not None:
            episodes = [episode for episode in episodes if episode.environment_id == environment_id]
        return sorted(episodes, key=lambda item: item.opened_at, reverse=True)

    def list_closed_episodes(self, *, environment_id: str, limit: int = 5000):
        return [
            episode
            for episode in self.list_episodes(environment_id=environment_id)[:limit]
            if str(episode.status) == "closed"
        ]

    def get_episode_detail(self, episode_id: str):
        return self.episodes.get(episode_id)

    def list_execution_facts_for_episodes(self, episode_ids: list[str]):
        return {episode_id: list(self.facts.get(episode_id, [])) for episode_id in episode_ids}

    def list_execution_facts_for_episode(self, episode_id: str):
        return list(self.facts.get(episode_id, []))

    def list_timeline_events(self, *, environment_id: str, episode_id: str, limit: int = 500, offset: int = 0):
        if environment_id != ENV_ID:
            return []
        return list(self.timeline_events.get(episode_id, []))[offset : offset + limit]

    def append_timeline_event(self, event):
        event.id = event.id or f"evt-{sum(len(items) for items in self.timeline_events.values()) + 1}"
        self.timeline_events.setdefault(str(event.episode_id), []).append(event)
        return str(event.id)

    def update_episode_notes(self, *, episode_id: str, environment_id: str, notes: str) -> bool:
        episode = self.episodes.get(episode_id)
        if episode is None or episode.environment_id != environment_id:
            return False
        episode.notes = notes
        return True


def test_get_v2_daily_groups_closed_and_open_episodes():
    service = JournalService(repository=_FakeRepo())

    response = service.get_v2_daily(environment_id=ENV_ID, trading_date=date(2026, 5, 2))

    assert response.environment.environment_id == ENV_ID
    assert response.summary.closed_episode_count == 1
    assert response.summary.open_episode_count == 1
    assert response.summary.strategy_count == 1
    assert response.summary.notes_count == 1
    assert response.strategy_groups[0].strategy.display_name == "Breakout V1"
    assert response.strategy_groups[0].episodes[0].outcome.net_pnl == 18
    assert response.open_episodes[0].episode_id == EP_2


def test_get_v2_period_returns_dense_daily_buckets_and_unknown_strategy_fallback():
    service = JournalService(repository=_FakeRepo())

    response = service.get_v2_period(
        environment_id=ENV_ID,
        from_date=date(2026, 5, 2),
        to_date=date(2026, 5, 4),
        granularity="day",
    )

    assert [bucket.bucket_start.isoformat() for bucket in response.buckets] == ["2026-05-02", "2026-05-03", "2026-05-04"]
    assert response.buckets[0].closed_episode_count == 1
    assert response.buckets[1].closed_episode_count == 1
    assert response.buckets[2].closed_episode_count == 0
    assert response.summary.closed_episode_count == 2
    assert {item.strategy.display_name for item in response.strategies} == {"Breakout V1", "Unknown strategy"}


def test_patch_v2_episode_notes_returns_enriched_detail_and_appends_timeline():
    repo = _FakeRepo()
    service = JournalService(repository=repo)

    response = service.patch_v2_episode_notes(EP_1, environment_id=ENV_ID, notes="updated notes")

    assert response.notes == "updated notes"
    assert response.episode.notes == "updated notes"
    assert response.timeline[-1].event_type == "notes_updated"
    assert response.timeline[-1].channel == "notes"
