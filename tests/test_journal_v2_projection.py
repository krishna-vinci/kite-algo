from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from journaling.models import DecisionActorType, DecisionType, JournalDecisionEvent, JournalSourceLink, SourceType
from journaling.service import JournalService
from scripts.backfill_journal_v2 import run_backfill


ENV_ID = "00000000-0000-4000-8000-000000001001"
CTX_ID = "00000000-0000-4000-8000-000000001101"
EPISODE_ID = "00000000-0000-4000-8000-000000001201"


@dataclass
class _StubRun:
    id: str
    metadata: dict[str, Any]


class _FakeProjectionRepository:
    def __init__(self) -> None:
        self.created_notes: list[dict[str, Any]] = []
        self.timeline_events: list[dict[str, Any]] = []

    def list_v1_review_note_candidates(
        self,
        *,
        limit: int = 100,
        environment_mode: str | None = None,
        account_scope: str | None = None,
    ):
        rows = [
            {
                "id": "run-1",
                "execution_mode": "paper",
                "account_ref": "kite:paper-e2e",
                "review_notes": "Great exit discipline",
            },
            {
                "id": "run-2",
                "execution_mode": "live",
                "account_ref": "kite:AB1234",
                "review_notes": "",  # skipped
            },
        ]
        if environment_mode is not None:
            rows = [row for row in rows if row["execution_mode"] == environment_mode]
        if account_scope is not None:
            rows = [row for row in rows if row["account_ref"] == account_scope]
        return rows[:limit]

    def list_source_links(self, run_id: str):
        if run_id == "run-1":
            return [JournalSourceLink(run_id=run_id, source_type=SourceType.PAPER_STRATEGY_RUN, source_key="paper-run-1")]
        return []

    def list_decision_events(self, run_id: str):
        if run_id != "run-1":
            return []
        return [
            JournalDecisionEvent(
                id=1,
                run_id=run_id,
                decision_type=DecisionType.REVIEW,
                actor_type=DecisionActorType.USER,
                occurred_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
                summary="legacy review",
                context={"from": "v1"},
            )
        ]

    def ensure_execution_environment(self, **kwargs):
        return ENV_ID

    def ensure_execution_context(self, **kwargs):
        return CTX_ID

    def ensure_episode(self, **kwargs):
        return EPISODE_ID

    def get_execution_environment(self, environment_id: str):
        return {"id": environment_id}

    def get_execution_context(self, context_id: str):
        if context_id != CTX_ID:
            return None

        class _Context:
            environment_id = ENV_ID

        return _Context()

    def get_episode_detail(self, episode_id: str):
        if episode_id != EPISODE_ID:
            return None

        class _Episode:
            environment_id = ENV_ID
            execution_context_id = CTX_ID

        return _Episode()

    def create_note(self, **kwargs):
        self.created_notes.append(dict(kwargs))
        return f"00000000-0000-4000-8000-{1300 + len(self.created_notes):012d}"

    def append_timeline_event(self, event):
        payload = event.model_dump(mode="python")
        payload["id"] = f"evt-{len(self.timeline_events) + 1}"
        self.timeline_events.append(payload)
        return payload["id"]


class _FakeV2FillRepository:
    def __init__(self) -> None:
        self.environments: dict[tuple[str, str], str] = {}
        self.contexts: dict[tuple[str, str, str], str] = {}
        self.episodes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.intents: list[dict[str, Any]] = []
        self.facts: list[Any] = []
        self.snapshots: list[Any] = []
        self.claims: dict[tuple[str, str], str] = {}
        self.v2_fact_count = 0
        self.unresolved_items: list[dict[str, Any]] = []

    def ensure_execution_environment(self, **kwargs):
        key = (str(kwargs.get("mode")), str(kwargs.get("account_scope")))
        if key not in self.environments:
            self.environments[key] = f"00000000-0000-4000-8000-{1000 + len(self.environments):012d}"
        return self.environments[key]

    def get_execution_environment(self, environment_id: str):
        if environment_id not in self.environments.values():
            return None
        return {"id": environment_id}

    def ensure_strategy_template(self, **kwargs):
        return "00000000-0000-4000-8000-000000000111"

    def ensure_strategy_variant(self, **kwargs):
        return "00000000-0000-4000-8000-000000000112"

    def ensure_strategy_deployment(self, **kwargs):
        return "00000000-0000-4000-8000-000000000113"

    def ensure_execution_context(self, **kwargs):
        key = (str(kwargs.get("environment_id")), str(kwargs.get("source_system")), str(kwargs.get("external_run_id")))
        if key not in self.contexts:
            self.contexts[key] = f"00000000-0000-4000-8000-{1100 + len(self.contexts):012d}"
        return self.contexts[key]

    def get_execution_context(self, context_id: str):
        for (environment_id, _source, _run), cid in self.contexts.items():
            if cid == context_id:
                return SimpleNamespace(environment_id=environment_id)
        return None

    def list_episodes(self, *, environment_id: str | None = None, execution_context_id: str | None = None, **kwargs):
        key = (str(environment_id), str(execution_context_id))
        rows = self.episodes.get(key, [])
        return [SimpleNamespace(**row) for row in rows]

    def ensure_episode(self, **kwargs):
        key = (str(kwargs.get("environment_id")), str(kwargs.get("execution_context_id")))
        rows = self.episodes.setdefault(key, [])
        requested_seq = int(kwargs.get("episode_seq") or (len(rows) + 1))
        for row in rows:
            if int(row["episode_seq"]) == requested_seq:
                return row["id"]
        episode_id = f"00000000-0000-4000-8000-{1200 + sum(len(items) for items in self.episodes.values()):012d}"
        rows.append(
            {
                "id": episode_id,
                "environment_id": key[0],
                "execution_context_id": key[1],
                "episode_seq": requested_seq,
                "status": str(kwargs.get("status") or "open"),
                "opened_at": kwargs.get("opened_at"),
                "closed_at": kwargs.get("closed_at"),
                "metadata": dict(kwargs.get("metadata") or {}),
            }
        )
        return episode_id

    def get_episode_detail(self, episode_id: str):
        for rows in self.episodes.values():
            for row in rows:
                if row["id"] == episode_id:
                    return SimpleNamespace(**row)
        return None

    def update_episode_status(self, episode_id: str, *, status: str, closed_at=None, metadata=None):
        for rows in self.episodes.values():
            for row in rows:
                if row["id"] == episode_id:
                    row["status"] = status
                    if closed_at is not None:
                        row["closed_at"] = closed_at
                    if metadata is not None:
                        merged = dict(row.get("metadata") or {})
                        merged.update(metadata)
                        row["metadata"] = merged
                    return

    def create_execution_intent(self, **kwargs):
        intent_id = f"00000000-0000-4000-8000-{1300 + len(self.intents):012d}"
        payload = dict(kwargs)
        payload["id"] = intent_id
        self.intents.append(payload)
        return intent_id

    def insert_execution_fact(self, fact):
        self.facts.append(fact)
        if getattr(fact, "environment_id", None):
            self.v2_fact_count += 1
        return len(self.facts)

    def find_v2_execution_fact_by_source(self, *, source_type: str, source_fact_key: str):
        for fact in self.facts:
            fact_source_type = str(getattr(getattr(fact, "source_type", None), "value", getattr(fact, "source_type", "")))
            if fact_source_type == source_type and getattr(fact, "source_fact_key", None) == source_fact_key and getattr(fact, "environment_id", None):
                return fact
        return None

    def claim_v2_projection_source(self, *, source_type: str, source_fact_key: str):
        key = (source_type, source_fact_key)
        if key in self.claims:
            return False
        self.claims[key] = "processing"
        return True

    def mark_v2_projection_source(self, *, source_type: str, source_fact_key: str, status: str):
        self.claims[(source_type, source_fact_key)] = status

    def append_timeline_event(self, event):
        return f"evt-{len(self.intents)}"

    def list_closed_episodes(self, *, environment_id: str, limit: int = 1000):
        episodes = []
        for (env_id, _ctx_id), rows in self.episodes.items():
            if env_id != environment_id:
                continue
            for row in rows:
                if row.get("status") == "closed":
                    episodes.append(SimpleNamespace(**row))
        return episodes[:limit]

    def list_execution_facts_for_episode(self, episode_id: str):
        return [fact for fact in self.facts if str(getattr(fact, "episode_id", "")) == str(episode_id)]

    def replace_metric_snapshot(self, snapshot):
        self.snapshots.append(snapshot)
        return len(self.snapshots)

    def create_unresolved_item(self, **kwargs):
        item = dict(kwargs)
        item["id"] = f"unres-{len(self.unresolved_items) + 1}"
        self.unresolved_items.append(item)
        return item["id"]

    def list_unresolved_items(self, *, environment_id: str, limit: int = 200, offset: int = 0):
        items = [item for item in self.unresolved_items if str(item.get("environment_id")) == str(environment_id)]
        return items[offset : offset + limit]


def test_v2_backfill_dry_run_does_not_write():
    repo = _FakeProjectionRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    result = service.backfill_v1_review_notes_to_v2(apply=False, limit=10, mode="paper")

    assert result["apply"] is False
    assert result["scanned"] == 1
    assert result["created"] == 0
    assert result["updated"] == 0
    assert repo.created_notes == []
    assert repo.timeline_events == []


def test_v2_backfill_apply_writes_notes_and_timeline():
    repo = _FakeProjectionRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    result = service.backfill_v1_review_notes_to_v2(apply=True, limit=10)

    assert result["apply"] is True
    assert result["created"] == 1
    assert result["updated"] == 1
    assert repo.created_notes[0]["note_type"] == "post_exit_review"
    assert repo.created_notes[0]["metadata"]["source"] == "v1_review_notes"
    assert repo.created_notes[0]["metadata"]["identity_rule_version"] == "v1_legacy_backfill"
    assert any(item["event_type"] == "legacy_decision_event_backfilled" for item in repo.timeline_events)


def test_backfill_script_entrypoint_uses_service(monkeypatch):
    fake_result = {"apply": False, "scanned": 2}

    class _FakeService:
        def backfill_v1_review_notes_to_v2(self, **kwargs):
            assert kwargs["apply"] is False
            assert kwargs["limit"] == 5
            assert kwargs["mode"] == "paper"
            assert kwargs["account_scope"] == "kite:paper-e2e"
            return fake_result

    monkeypatch.setattr("scripts.backfill_journal_v2.JournalService", lambda: _FakeService())
    result = run_backfill(apply=False, limit=5, mode="paper", account_scope="kite:paper-e2e")
    assert result == fake_result


def test_live_and_paper_with_same_strategy_run_id_separate_environments_and_episodes():
    repo = _FakeV2FillRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    live = service.record_v2_execution_fill(
        mode="live",
        account_scope="kite:AB1234",
        source_system="live_projector",
        external_run_id="same-run",
        source_type="live_fill",
        source_fact_key="live:trade-1",
        side="BUY",
        quantity=1,
        price=Decimal("100"),
        fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
        gross_cash_flow=Decimal("-100"),
        run_id="11111111-1111-4111-8111-111111111111",
        order_id="O1",
        trade_id="T1",
        attribution={"strategy_run_id": "same-run", "strategy_family": "indicator_strategy", "strategy_name": "MR"},
    )
    paper = service.record_v2_execution_fill(
        mode="paper",
        account_scope="kite:paper-e2e",
        source_system="paper_runtime",
        external_run_id="same-run",
        source_type="paper_trade",
        source_fact_key="paper:trade-1",
        side="BUY",
        quantity=1,
        price=Decimal("100"),
        fill_timestamp=datetime(2026, 5, 1, 9, 16, tzinfo=timezone.utc),
        gross_cash_flow=Decimal("-100"),
        run_id="22222222-2222-4222-8222-222222222222",
        order_id="O2",
        trade_id="T2",
        attribution={"strategy_run_id": "same-run", "strategy_family": "indicator_strategy", "strategy_name": "MR"},
    )

    assert live["environment_id"] != paper["environment_id"]
    assert live["episode_id"] != paper["episode_id"]


def test_paper_charges_flow_into_v2_fact_and_episode_metrics():
    repo = _FakeV2FillRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    first = service.record_v2_execution_fill(
        mode="paper",
        account_scope="kite:paper-e2e",
        source_system="paper_runtime",
        external_run_id="paper-run-2",
        source_type="paper_trade",
        source_fact_key="paper:entry",
        side="BUY",
        quantity=1,
        price=Decimal("100"),
        fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
        gross_cash_flow=Decimal("-100"),
        fees_amount=Decimal("1.0"),
        taxes_amount=Decimal("0.5"),
        run_id="33333333-3333-4333-8333-333333333333",
        order_id="O3",
        trade_id="T3",
        attribution={"strategy_run_id": "paper-run-2", "strategy_family": "indicator_strategy", "strategy_name": "MR"},
    )
    second = service.record_v2_execution_fill(
        mode="paper",
        account_scope="kite:paper-e2e",
        source_system="paper_runtime",
        external_run_id="paper-run-2",
        source_type="paper_trade",
        source_fact_key="paper:exit",
        side="SELL",
        quantity=1,
        price=Decimal("120"),
        fill_timestamp=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
        gross_cash_flow=Decimal("120"),
        fees_amount=Decimal("1.0"),
        taxes_amount=Decimal("0.5"),
        run_id="33333333-3333-4333-8333-333333333333",
        order_id="O4",
        trade_id="T4",
        attribution={"strategy_run_id": "paper-run-2", "strategy_family": "indicator_strategy", "strategy_name": "MR"},
    )

    assert first["episode_id"] == second["episode_id"]
    assert repo.v2_fact_count == 2
    metrics_result = service.compute_v2_environment_metrics(environment_id=second["environment_id"])
    assert metrics_result["metrics"]["closed_episode_count"] == 1
    assert Decimal(metrics_result["metrics"]["total_charges"]) == Decimal("3.0")


def test_episode_remains_open_until_all_instruments_are_flat():
    repo = _FakeV2FillRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]
    base_attribution = {"strategy_run_id": "multi-leg-run", "strategy_family": "indicator_strategy", "strategy_name": "Pair"}
    base = {
        "mode": "paper",
        "account_scope": "kite:paper-e2e",
        "source_system": "paper_runtime",
        "external_run_id": "multi-leg-run",
        "source_type": "paper_trade",
        "price": Decimal("100"),
        "fill_timestamp": datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
        "gross_cash_flow": Decimal("-100"),
        "run_id": "44444444-4444-4444-8444-444444444444",
    }

    first = service.record_v2_execution_fill(
        **base,
        source_fact_key="paper:leg-a-entry",
        side="BUY",
        quantity=1,
        trade_id="TA1",
        attribution={**base_attribution, "instrument_token": 111, "product": "MIS"},
    )
    second = service.record_v2_execution_fill(
        **base,
        source_fact_key="paper:leg-b-entry",
        side="BUY",
        quantity=1,
        trade_id="TB1",
        attribution={**base_attribution, "instrument_token": 222, "product": "MIS"},
    )
    partial_exit = service.record_v2_execution_fill(
        **{**base, "gross_cash_flow": Decimal("100")},
        source_fact_key="paper:leg-a-exit",
        side="SELL",
        quantity=1,
        trade_id="TA2",
        attribution={**base_attribution, "instrument_token": 111, "product": "MIS"},
    )

    assert first["episode_id"] == second["episode_id"] == partial_exit["episode_id"]
    episode = repo.get_episode_detail(partial_exit["episode_id"])
    assert episode is not None
    assert episode.status == "open"
    assert episode.metadata["net_quantity_by_instrument"] == {"111:MIS": 0, "222:MIS": 1}


def test_replayed_v2_fill_does_not_mutate_episode_state_twice():
    repo = _FakeV2FillRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]
    kwargs = {
        "mode": "paper",
        "account_scope": "kite:paper-e2e",
        "source_system": "paper_runtime",
        "external_run_id": "replay-run",
        "source_type": "paper_trade",
        "source_fact_key": "paper:replayed-fill",
        "side": "BUY",
        "quantity": 1,
        "price": Decimal("100"),
        "fill_timestamp": datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
        "gross_cash_flow": Decimal("-100"),
        "run_id": "55555555-5555-4555-8555-555555555555",
        "trade_id": "TRP1",
        "attribution": {"strategy_run_id": "replay-run", "strategy_family": "indicator_strategy", "strategy_name": "Replay", "instrument_token": 111, "product": "MIS"},
    }

    first = service.record_v2_execution_fill(**kwargs)
    second = service.record_v2_execution_fill(**kwargs)

    assert second["duplicate"] is True
    assert first["episode_id"] == second["episode_id"]
    episode = repo.get_episode_detail(first["episode_id"])
    assert episode is not None
    assert episode.metadata["net_quantity_by_instrument"] == {"111:MIS": 1}
    assert repo.v2_fact_count == 1


def test_missing_template_id_routes_identity_to_unresolved_queue():
    repo = _FakeV2FillRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    fill = service.record_v2_execution_fill(
        mode="paper",
        account_scope="kite:paper-e2e",
        source_system="paper_runtime",
        external_run_id="legacy-run-1",
        source_type="paper_trade",
        source_fact_key="paper:legacy-1",
        side="BUY",
        quantity=1,
        price=Decimal("100"),
        fill_timestamp=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        gross_cash_flow=Decimal("-100"),
        run_id="55555555-5555-4555-8555-555555555555",
        trade_id="T5",
        attribution={"strategy_name": "Legacy Name Only", "strategy_family": "indicator_strategy"},
    )

    unresolved = repo.list_unresolved_items(environment_id=fill["environment_id"])
    assert len(unresolved) == 1
    assert str(unresolved[0]["reason"])
    assert unresolved[0]["raw_identity"]["strategy_name"] == "Legacy Name Only"
