import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from journaling.models import CapitalBasisType, ExecutionMode, JournalExecutionFact, JournalRun, SourceType, StrategyFamily
from journaling.repository import JournalRepository
from journaling.service import JournalService


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _db_ready() -> bool:
    if not TEST_DATABASE_URL:
        return False
    if os.getenv("JOURNAL_V2_ALLOW_DESTRUCTIVE_TEST_DB") == "1":
        return True
    try:
        database_name = str(make_url(TEST_DATABASE_URL).database or "").lower()
    except Exception:
        return False
    return any(marker in database_name for marker in ("test", "ci", "validation"))


def _apply_schema_twice() -> None:
    schema_sql = (REPO_ROOT / "schema.sql").read_text()
    for _ in range(2):
        with psycopg2.connect(TEST_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
            conn.commit()


@unittest.skipUnless(_db_ready(), "TEST_DATABASE_URL not configured")
class JournalV2DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _apply_schema_twice()
        cls.engine = create_engine(str(TEST_DATABASE_URL), pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self._truncate_journal_tables()
        self.repository = JournalRepository(self.SessionLocal)
        self.service = JournalService(repository=self.repository)

    def _truncate_journal_tables(self) -> None:
        db = self.SessionLocal()
        try:
            db.execute(
                text(
                    """
                    TRUNCATE TABLE
                        public.journal_v2_projection_claims,
                        public.journal_attachments,
                        public.journal_note_revisions,
                        public.journal_notes,
                        public.journal_timeline_events,
                        public.journal_unresolved_queue,
                        public.journal_metric_snapshots,
                        public.journal_execution_facts,
                        public.journal_execution_intents,
                        public.journal_episode_legs,
                        public.journal_episodes,
                        public.journal_execution_contexts,
                        public.journal_strategy_deployments,
                        public.journal_strategy_variants,
                        public.journal_strategy_templates,
                        public.journal_source_links,
                        public.journal_decision_events,
                        public.journal_run_legs,
                        public.journal_runs,
                        public.journal_execution_environments
                    RESTART IDENTITY CASCADE
                    """
                )
            )
            db.commit()
        finally:
            db.close()

    def _create_run(self, *, execution_mode: str, account_ref: str, strategy_name: str = "Validation") -> str:
        return self.repository.create_run(
            JournalRun(
                strategy_family=StrategyFamily.INDICATOR,
                strategy_name=strategy_name,
                entry_surface="journal_v2_db_integration",
                execution_mode=ExecutionMode(execution_mode),
                account_ref=account_ref,
                capital_basis_type=CapitalBasisType.CASH_DEPLOYED,
                capital_committed=Decimal("1000"),
            )
        )

    def _record_fill(
        self,
        *,
        mode: str = "paper",
        account_scope: str = "kite:paper-journal-v2-db",
        external_run_id: str = "db-run-1",
        source_type: str = "paper_trade",
        source_fact_key: str = "paper:db-fill-1",
        side: str = "BUY",
        quantity: int = 1,
        run_id: str | None = None,
        instrument_token: int = 111,
        product: str = "MIS",
    ) -> dict:
        if run_id is None:
            run_id = self._create_run(execution_mode="paper" if mode == "paper" else "live", account_ref=account_scope)
        return self.service.record_v2_execution_fill(
            mode=mode,
            account_scope=account_scope,
            source_system="journal_v2_db_integration",
            external_run_id=external_run_id,
            source_type=source_type,
            source_fact_key=source_fact_key,
            side=side,
            quantity=quantity,
            price=Decimal("100"),
            fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
            gross_cash_flow=Decimal("-100") if side.upper() == "BUY" else Decimal("100"),
            run_id=run_id,
            order_id=f"OID-{source_fact_key}",
            trade_id=f"TRD-{source_fact_key}",
            attribution={
                "strategy_run_id": external_run_id,
                "template_id": "journal-v2-db-template",
                "strategy_family": "indicator_strategy",
                "strategy_name": "Journal V2 DB Validation",
                "instrument_token": instrument_token,
                "product": product,
            },
        )

    def test_schema_sql_applies_twice_to_real_postgres(self):
        _apply_schema_twice()

    def test_live_and_paper_same_external_run_id_create_separate_contexts_and_episodes(self):
        live_run_id = self._create_run(execution_mode="live", account_ref="kite:AB1234")
        paper_run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")

        live = self._record_fill(
            mode="live",
            account_scope="kite:AB1234",
            external_run_id="same-external-run",
            source_type="live_fill",
            source_fact_key="live:same-external-run:1",
            run_id=live_run_id,
        )
        paper = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="same-external-run",
            source_type="paper_trade",
            source_fact_key="paper:same-external-run:1",
            run_id=paper_run_id,
        )

        assert live["environment_id"] != paper["environment_id"]
        assert live["execution_context_id"] != paper["execution_context_id"]
        assert live["episode_id"] != paper["episode_id"]

    def test_replayed_v2_fill_does_not_mutate_episode_state_twice(self):
        run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")
        kwargs = {
            "mode": "paper",
            "account_scope": "kite:paper-journal-v2-db",
            "external_run_id": "replay-db-run",
            "source_type": "paper_trade",
            "source_fact_key": "paper:replay-db-fill",
            "side": "BUY",
            "quantity": 1,
            "run_id": run_id,
            "instrument_token": 111,
            "product": "MIS",
        }

        first = self._record_fill(**kwargs)
        replay = self._record_fill(**kwargs)
        episode = self.repository.get_episode_detail(first["episode_id"])

        assert first["episode_id"] == replay["episode_id"]
        assert replay["duplicate"] is True
        assert episode is not None
        assert episode.metadata["net_quantity_by_instrument"] == {"111:MIS": 1}

    def test_v1_fact_replay_does_not_erase_existing_v2_fact_fields(self):
        run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")
        first = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="v1-replay-db-run",
            source_type="paper_trade",
            source_fact_key="paper:v1-replay-db-fill",
            run_id=run_id,
        )

        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=run_id,
                source_type=SourceType.PAPER_TRADE,
                source_fact_key="paper:v1-replay-db-fill",
                fill_timestamp=datetime(2026, 5, 1, 9, 16, tzinfo=timezone.utc),
                side="BUY",
                quantity=1,
                price=Decimal("100"),
                gross_cash_flow=Decimal("-100"),
                payload={"source": "legacy-v1-replay"},
            )
        )

        fact = self.repository.find_v2_execution_fact_by_source(
            source_type="paper_trade",
            source_fact_key="paper:v1-replay-db-fill",
        )
        assert fact is not None
        assert str(fact.environment_id) == first["environment_id"]
        assert str(fact.episode_id) == first["episode_id"]
        assert str(fact.intent_id) == first["intent_id"]

    def test_concurrent_note_updates_serialize_revisions(self):
        run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")
        fill = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="note-db-run",
            source_type="paper_trade",
            source_fact_key="paper:note-db-fill",
            run_id=run_id,
        )
        note_id = self.service.create_v2_note(
            environment_id=fill["environment_id"],
            subject_type="episode",
            subject_id=fill["episode_id"],
            episode_id=fill["episode_id"],
            note_type="thesis",
            title="Initial thesis",
            body_markdown="# Initial\n\n- setup",
        )

        def _update(idx: int) -> None:
            repository = JournalRepository(self.SessionLocal)
            service = JournalService(repository=repository)
            service.update_v2_note(
                note_id,
                environment_id=fill["environment_id"],
                subject_type="episode",
                subject_id=fill["episode_id"],
                title=f"Revision {idx}",
                body_markdown=f"# Revision {idx}",
                change_reason=f"db-concurrency-{idx}",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(_update, [1, 2]))

        revisions = self.repository.list_note_revisions(note_id)
        assert [rev.revision_no for rev in revisions] == [1, 2]
