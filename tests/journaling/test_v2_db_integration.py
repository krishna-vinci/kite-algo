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


def _table_columns(db, table_name: str) -> set[str]:
    rows = db.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row[0] for row in rows}


def _table_column_details(db, table_name: str) -> dict[str, dict[str, str | None]]:
    rows = db.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {
        row[0]: {
            "data_type": row[1],
            "is_nullable": row[2],
            "column_default": row[3],
        }
        for row in rows
    }


def _table_constraint_defs(db, table_name: str) -> dict[str, str]:
    rows = db.execute(
        text(
            """
            SELECT c.conname, pg_get_constraintdef(c.oid)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relname = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row[0]: row[1] for row in rows}


def _table_index_defs(db, table_name: str) -> dict[str, str]:
    rows = db.execute(
        text(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {row[0]: row[1] for row in rows}


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

    def test_journal_v2_schema_has_itemized_cost_notes_constraint_and_indexes(self):
        db = self.SessionLocal()
        try:
            fact_columns = _table_column_details(db, "journal_execution_facts")
            for name in [
                "brokerage",
                "exchange_txn_charge",
                "stt",
                "stamp_duty",
                "sebi_charge",
                "gst",
                "margin_required",
                "charges_status",
            ]:
                assert name in fact_columns
                assert fact_columns[name]["is_nullable"] == "NO"

            for name in [
                "brokerage",
                "exchange_txn_charge",
                "stt",
                "stamp_duty",
                "sebi_charge",
                "gst",
                "margin_required",
            ]:
                assert fact_columns[name]["data_type"] == "numeric"
                assert fact_columns[name]["column_default"] == "0"

            assert fact_columns["charges_status"]["data_type"] == "text"
            assert fact_columns["charges_status"]["column_default"] == "'unavailable'::text"

            episode_columns = _table_column_details(db, "journal_episodes")
            assert "notes" in episode_columns
            assert episode_columns["notes"]["data_type"] == "text"
            assert episode_columns["notes"]["is_nullable"] == "NO"
            assert episode_columns["notes"]["column_default"] == "''::text"

            fact_constraints = _table_constraint_defs(db, "journal_execution_facts")
            assert "journal_execution_facts_charges_status_chk" in fact_constraints
            constraint_def = fact_constraints["journal_execution_facts_charges_status_chk"]
            for expected_status in ["estimated", "broker_quoted", "reconciled", "unavailable"]:
                assert expected_status in constraint_def

            episode_indexes = _table_index_defs(db, "journal_episodes")
            assert "idx_journal_episodes_environment_opened_at" in episode_indexes
            assert "idx_journal_episodes_environment_closed_at" in episode_indexes
            assert "opened_at DESC" in episode_indexes["idx_journal_episodes_environment_opened_at"]
            assert "closed_at DESC" in episode_indexes["idx_journal_episodes_environment_closed_at"]
            assert "WHERE (closed_at IS NOT NULL)" in episode_indexes["idx_journal_episodes_environment_closed_at"]

            fact_indexes = _table_index_defs(db, "journal_execution_facts")
            assert "idx_journal_execution_facts_environment_fill_timestamp" in fact_indexes
            fact_index = fact_indexes["idx_journal_execution_facts_environment_fill_timestamp"]
            assert "fill_timestamp DESC" in fact_index
            assert "WHERE (environment_id IS NOT NULL)" in fact_index
        finally:
            db.close()

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
                environment_id=first["environment_id"],
                episode_id=first["episode_id"],
                intent_id=first["intent_id"],
                source_type=SourceType.PAPER_TRADE,
                source_fact_key="paper:v1-replay-db-fill",
                order_id=f"OID-paper:v1-replay-db-fill",
                trade_id=f"TRD-paper:v1-replay-db-fill",
                fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
                side="BUY",
                quantity=1,
                price=Decimal("100"),
                gross_cash_flow=Decimal("-100"),
                brokerage=Decimal("1.25"),
                exchange_txn_charge=Decimal("0.45"),
                stt=Decimal("0.80"),
                stamp_duty=Decimal("0.10"),
                sebi_charge=Decimal("0.02"),
                gst=Decimal("0.31"),
                margin_required=Decimal("2500"),
                charges_status="reconciled",
                payload={"source": "v2-enriched"},
            )
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
        assert fact.brokerage == Decimal("1.25")
        assert fact.exchange_txn_charge == Decimal("0.45")
        assert fact.stt == Decimal("0.80")
        assert fact.stamp_duty == Decimal("0.10")
        assert fact.sebi_charge == Decimal("0.02")
        assert fact.gst == Decimal("0.31")
        assert fact.margin_required == Decimal("2500")
        assert fact.charges_status == "reconciled"

    def test_repository_reads_itemized_execution_costs_from_execution_fact_rows(self):
        run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")
        recorded = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="itemized-db-run",
            source_type="paper_trade",
            source_fact_key="paper:itemized-db-fill",
            run_id=run_id,
        )

        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=run_id,
                environment_id=recorded["environment_id"],
                episode_id=recorded["episode_id"],
                intent_id=recorded["intent_id"],
                source_type=SourceType.PAPER_TRADE,
                source_fact_key="paper:itemized-db-fill",
                order_id="OID-paper:itemized-db-fill",
                trade_id="TRD-paper:itemized-db-fill",
                fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
                side="BUY",
                quantity=1,
                price=Decimal("100"),
                gross_cash_flow=Decimal("-100"),
                brokerage=Decimal("1.50"),
                exchange_txn_charge=Decimal("0.55"),
                stt=Decimal("0.90"),
                stamp_duty=Decimal("0.11"),
                sebi_charge=Decimal("0.03"),
                gst=Decimal("0.37"),
                margin_required=Decimal("3000"),
                charges_status="broker_quoted",
                payload={"source": "itemized-read"},
            )
        )

        fact = self.repository.find_v2_execution_fact_by_source(
            source_type="paper_trade",
            source_fact_key="paper:itemized-db-fill",
        )

        assert fact is not None
        assert fact.brokerage == Decimal("1.50")
        assert fact.exchange_txn_charge == Decimal("0.55")
        assert fact.stt == Decimal("0.90")
        assert fact.stamp_duty == Decimal("0.11")
        assert fact.sebi_charge == Decimal("0.03")
        assert fact.gst == Decimal("0.37")
        assert fact.margin_required == Decimal("3000")
        assert fact.charges_status == "broker_quoted"

    def test_itemized_upsert_preserves_unspecified_existing_cost_fields(self):
        run_id = self._create_run(execution_mode="paper", account_ref="kite:paper-journal-v2-db")
        recorded = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="partial-itemized-db-run",
            source_type="paper_trade",
            source_fact_key="paper:partial-itemized-db-fill",
            run_id=run_id,
        )

        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=run_id,
                environment_id=recorded["environment_id"],
                episode_id=recorded["episode_id"],
                intent_id=recorded["intent_id"],
                source_type=SourceType.PAPER_TRADE,
                source_fact_key="paper:partial-itemized-db-fill",
                order_id="OID-paper:partial-itemized-db-fill",
                trade_id="TRD-paper:partial-itemized-db-fill",
                fill_timestamp=datetime(2026, 5, 1, 9, 15, tzinfo=timezone.utc),
                side="BUY",
                quantity=1,
                price=Decimal("100"),
                gross_cash_flow=Decimal("-100"),
                brokerage=Decimal("1.50"),
                exchange_txn_charge=Decimal("0.55"),
                stt=Decimal("0.90"),
                stamp_duty=Decimal("0.11"),
                sebi_charge=Decimal("0.03"),
                gst=Decimal("0.37"),
                margin_required=Decimal("3000"),
                charges_status="broker_quoted",
                payload={"source": "itemized-seed"},
            )
        )

        self.repository.insert_execution_fact(
            JournalExecutionFact(
                run_id=run_id,
                environment_id=recorded["environment_id"],
                episode_id=recorded["episode_id"],
                intent_id=recorded["intent_id"],
                source_type=SourceType.PAPER_TRADE,
                source_fact_key="paper:partial-itemized-db-fill",
                order_id="OID-paper:partial-itemized-db-fill",
                trade_id="TRD-paper:partial-itemized-db-fill",
                fill_timestamp=datetime(2026, 5, 1, 9, 16, tzinfo=timezone.utc),
                side="BUY",
                quantity=1,
                price=Decimal("100"),
                gross_cash_flow=Decimal("-100"),
                stt=Decimal("1.10"),
                charges_status="reconciled",
                payload={"source": "itemized-partial-update"},
            )
        )

        fact = self.repository.find_v2_execution_fact_by_source(
            source_type="paper_trade",
            source_fact_key="paper:partial-itemized-db-fill",
        )

        assert fact is not None
        assert fact.brokerage == Decimal("1.50")
        assert fact.exchange_txn_charge == Decimal("0.55")
        assert fact.stt == Decimal("1.10")
        assert fact.stamp_duty == Decimal("0.11")
        assert fact.sebi_charge == Decimal("0.03")
        assert fact.gst == Decimal("0.37")
        assert fact.margin_required == Decimal("3000")
        assert fact.charges_status == "reconciled"

    def test_update_episode_notes_is_environment_scoped_and_can_clear_empty_string(self):
        first = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="notes-db-run-a",
            source_type="paper_trade",
            source_fact_key="paper:notes-db-fill-a",
        )
        second = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db-2",
            external_run_id="notes-db-run-b",
            source_type="paper_trade",
            source_fact_key="paper:notes-db-fill-b",
        )

        assert self.repository.update_episode_notes(
            episode_id=first["episode_id"],
            environment_id=second["environment_id"],
            notes="wrong environment",
        ) is False

        episode = self.repository.get_episode_detail(first["episode_id"])
        assert episode is not None
        assert episode.notes == ""

        assert self.repository.update_episode_notes(
            episode_id=first["episode_id"],
            environment_id=first["environment_id"],
            notes="Initial note",
        ) is True

        episode = self.repository.get_episode_detail(first["episode_id"])
        assert episode is not None
        assert episode.notes == "Initial note"

        assert self.repository.update_episode_notes(
            episode_id=first["episode_id"],
            environment_id=first["environment_id"],
            notes="",
        ) is True

        episode = self.repository.get_episode_detail(first["episode_id"])
        other_episode = self.repository.get_episode_detail(second["episode_id"])
        assert episode is not None
        assert other_episode is not None
        assert episode.notes == ""
        assert other_episode.notes == ""

    def test_list_execution_facts_for_episodes_groups_rows_and_empty_input_returns_empty_dict(self):
        first = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="batch-db-run-a",
            source_type="paper_trade",
            source_fact_key="paper:batch-db-fill-a",
        )
        second = self._record_fill(
            mode="paper",
            account_scope="kite:paper-journal-v2-db",
            external_run_id="batch-db-run-b",
            source_type="paper_trade",
            source_fact_key="paper:batch-db-fill-b",
            side="SELL",
        )

        assert self.repository.list_execution_facts_for_episodes([]) == {}

        grouped = self.repository.list_execution_facts_for_episodes([
            first["episode_id"],
            second["episode_id"],
        ])

        assert set(grouped) == {first["episode_id"], second["episode_id"]}
        assert [fact.source_fact_key for fact in grouped[first["episode_id"]]] == ["paper:batch-db-fill-a"]
        assert [fact.source_fact_key for fact in grouped[second["episode_id"]]] == ["paper:batch-db-fill-b"]

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
