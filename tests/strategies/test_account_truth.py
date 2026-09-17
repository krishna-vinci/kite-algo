"""Account truth: account-wide fill ingestion, manual residual, reconciliation.

The manual book is a **derived residual of persisted facts**, never
``broker − attributed`` (D-1): subtracting from the broker net would silently
absorb missing ingestion, which is exactly what ``pending_ingest`` must expose.

    manual_quantity(coord) = Σ signed unlinked ingested facts
                             − Σ adjustment-line deltas on that coord

Facts are insert-only and deduplicated by durable broker identity
``(account_id, trade_id)``, so re-ingesting a page is a no-op.

SQLite runs with the established ``public.`` ATTACH fixture: the new tables come
from the shared ``Base`` metadata, while the pre-existing platform tables the
store reads are ``public.``-qualified.
"""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.strategies.attribution import SqlAttributionStore
from backend.strategies.account_truth import AccountTruthService, AccountTruthStore


async def _inline(func, /, **kwargs):
    """Run the store call inline so the service logic is what is under test."""
    return func(**kwargs)
from backend.workflows.repository import Base
import backend.strategies.models  # noqa: F401  registers the hosted tables on Base
import backend.strategies.attribution_models  # noqa: F401  registers the attribution/truth tables


def _coord(token=738561, exchange="NSE", symbol="RELIANCE", product="CNC"):
    return (token, exchange, symbol, product)


class AccountTruthTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _attach_public(dbapi_connection, connection_record):
            _ = connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.execute(
                """
                CREATE TABLE public.worker_live_execution_links (
                    link_id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, broker_order_id TEXT NOT NULL, trade_id TEXT,
                    client_order_ref TEXT, basket_execution_id TEXT, basket_leg_index INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.store = AccountTruthStore(session_factory=self.factory)

    def tearDown(self):
        self.engine.dispose()

    def _link(self, *, order_id, run_id="run-1", account="kite:A"):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.worker_live_execution_links "
                    "(strategy_run_id, account_id, broker_order_id) VALUES (:run, :account, :order_id)"
                ),
                {"run": run_id, "account": account, "order_id": order_id},
            )
            session.commit()

    @staticmethod
    def _trade(trade_id, *, order_id="OID-1", side="BUY", qty=100, token=738561,
               exchange="NSE", symbol="RELIANCE", product="CNC"):
        return {
            "trade_id": trade_id, "order_id": order_id, "instrument_token": token,
            "exchange": exchange, "tradingsymbol": symbol, "product": product,
            "transaction_type": side, "quantity": qty, "average_price": 100.0,
            "fill_timestamp": "2026-09-17T10:00:00+00:00",
        }


class IngestTests(AccountTruthTestCase):
    def test_ingest_persists_untracked_fills(self):
        """Fills with NO platform link still land in the account's truth."""
        trades = [
            self._trade("T-1", order_id="OID-UNTRACKED-1"),
            self._trade("T-2", order_id="OID-UNTRACKED-2", side="SELL", qty=40),
        ]
        result = self.store.ingest_trades(account_id="kite:A", trades=trades)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["skipped"], 0)

        # Re-ingest is a no-op: dedupe is by durable broker identity.
        again = self.store.ingest_trades(account_id="kite:A", trades=trades)
        self.assertEqual((again["inserted"], again["skipped"]), (0, 2))
        self.assertEqual(self.store.fact_count(account_id="kite:A"), 2)

    def test_ingest_generation_and_cursor_advance(self):
        self.assertEqual(self.store.ingest_state(account_id="kite:A")["status"], "idle")

        generation = self.store.begin_ingest(account_id="kite:A")
        self.assertGreaterEqual(generation, 1)
        self.assertEqual(self.store.ingest_state(account_id="kite:A")["status"], "refreshing")

        self.store.ingest_trades(account_id="kite:A", trades=[self._trade("T-1")], generation=generation)
        self.store.complete_ingest(account_id="kite:A", generation=generation)

        state = self.store.ingest_state(account_id="kite:A")
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["ingest_generation"], generation)
        self.assertIsNotNone(state["last_orders_fetch_at"])
        self.assertIsNotNone(state["last_complete_ingest_at"])

    def test_failed_ingest_marks_state_stale(self):
        generation = self.store.begin_ingest(account_id="kite:A")
        self.store.fail_ingest(account_id="kite:A", generation=generation)
        self.assertEqual(self.store.ingest_state(account_id="kite:A")["status"], "stale")

    def test_reingest_never_overwrites_a_fact(self):
        """Insert-only by construction at the store level, and never an overwrite.

        The database trigger is the backstop and is proved on PostgreSQL (the
        disposable-PG suite); asserting it here would mean asserting a SQLite
        trigger this fixture invented, which would prove nothing about
        production.
        """
        self.store.ingest_trades(account_id="kite:A", trades=[self._trade("T-1", qty=100)])
        for method in ("update_fact", "delete_fact", "upsert_fact", "replace_facts"):
            self.assertFalse(hasattr(self.store, method), method)

        # A "corrected" re-ingest of the same durable identity is a no-op.
        again = self.store.ingest_trades(account_id="kite:A", trades=[self._trade("T-1", qty=999)])
        self.assertEqual((again["inserted"], again["skipped"]), (0, 1))
        with self.factory() as session:
            quantity = session.execute(
                text("SELECT quantity FROM broker_trade_facts WHERE account_id='kite:A'")
            ).scalar()
        self.assertEqual(quantity, 100)


class ManualResidualTests(AccountTruthTestCase):
    def test_manual_residual_is_unlinked_signed_sum(self):
        """Walkthrough 7 case 1: platform holds 100, the owner sells 10."""
        self.store.ingest_trades(
            account_id="kite:A",
            trades=[
                self._trade("T-1", order_id="OID-TRACKED"),          # A's own fill
                self._trade("T-2", order_id="OID-MANUAL", side="SELL", qty=10),  # the human
            ],
        )
        self._link(order_id="OID-TRACKED")

        residual = self.store.manual_residual_by_coordinate(account_id="kite:A")
        self.assertEqual(residual[_coord()], -10)  # NOT broker-minus-attributed

    def test_manual_residual_empty_when_all_fills_linked(self):
        self.store.ingest_trades(account_id="kite:A", trades=[self._trade("T-1", order_id="OID-TRACKED")])
        self._link(order_id="OID-TRACKED")
        residual = self.store.manual_residual_by_coordinate(account_id="kite:A")
        self.assertEqual(residual.get(_coord(), 0), 0)

    def test_manual_residual_is_per_coordinate(self):
        self.store.ingest_trades(
            account_id="kite:A",
            trades=[
                self._trade("T-1", order_id="OID-M1", qty=10),
                self._trade("T-2", order_id="OID-M2", side="SELL", qty=7, token=408065, symbol="INFY"),
            ],
        )
        residual = self.store.manual_residual_by_coordinate(account_id="kite:A")
        self.assertEqual(residual[_coord()], 10)
        self.assertEqual(residual[_coord(token=408065, symbol="INFY")], -7)


class IngestServiceTests(AccountTruthTestCase):
    def test_service_ingests_via_the_injected_provider_and_isolates_failures(self):
        seen = []

        async def provider(account_id):
            seen.append(account_id)
            if account_id == "kite:BROKEN":
                raise RuntimeError("broker unavailable")
            return [self._trade("T-1", order_id=f"OID-{account_id}")]

        service = AccountTruthService(self.store, trades_provider=provider)
        import asyncio

        results = asyncio.run(service.ingest_accounts(["kite:A", "kite:BROKEN", "kite:C"]))
        self.assertEqual(seen, ["kite:A", "kite:BROKEN", "kite:C"])
        self.assertEqual(results["kite:A"]["inserted"], 1)
        self.assertEqual(results["kite:C"]["inserted"], 1)
        # One account's broker error must not stall the others.
        self.assertEqual(results["kite:BROKEN"]["error"], "RuntimeError")
        self.assertEqual(self.store.ingest_state(account_id="kite:BROKEN")["status"], "stale")
        self.assertEqual(self.store.fact_count(account_id="kite:A"), 1)
        self.assertEqual(self.store.fact_count(account_id="kite:C"), 1)


class ReconciliationTests(AccountTruthTestCase):
    """G4: classification, the bounded refresh, the freeze and escalation."""

    def _seed_broker(self, *, qty, token=738561, symbol="RELIANCE", product="CNC", exchange="NSE"):
        with self.factory() as session:
            session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS public.account_positions (
                        account_id TEXT NOT NULL, instrument_token BIGINT NOT NULL,
                        exchange TEXT NOT NULL DEFAULT '', tradingsymbol TEXT NOT NULL DEFAULT '',
                        product TEXT NOT NULL, net_quantity BIGINT NOT NULL,
                        PRIMARY KEY (account_id, instrument_token, product)
                    )
                    """
                )
            )
            session.execute(
                text(
                    "INSERT INTO public.account_positions "
                    "(account_id, instrument_token, exchange, tradingsymbol, product, net_quantity) "
                    "VALUES ('kite:A', :token, :exchange, :symbol, :product, :qty) "
                    "ON CONFLICT (account_id, instrument_token, product) DO UPDATE SET net_quantity = :qty"
                ),
                {"token": token, "exchange": exchange, "symbol": symbol, "product": product, "qty": qty},
            )
            session.commit()

    def _seed_book(self, *, qty, sid="stg-1", token=738561, symbol="RELIANCE"):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope) "
                    "VALUES (:sid, 'app:o', :name, 'kite:A')"
                ),
                {"sid": sid, "name": f"Strategy {sid}"},
            )
            session.commit()
        recomputed = SqlAttributionStore(session_factory=self.factory)
        import uuid as _uuid

        identity = str(_uuid.uuid4())
        recomputed.recompute_publish(
            account_id="kite:A", strategy_id=sid, execution_environment="live",
            resolve_and_fold=lambda db, bound: (
                [{
                    "identity_kind": "canonical", "identity_key": identity,
                    "canonical_instrument_id": identity, "instrument_token": token,
                    "exchange": "NSE", "tradingsymbol": symbol, "product": "CNC",
                    "net_quantity": qty, "unresolved_reason": None,
                }],
                f"sha-{sid}-{qty}",
            ),
        )

    def _service(self, **kwargs):
        from backend.strategies.account_truth import ReconciliationService

        return ReconciliationService(self.store, run_async=_inline, **kwargs)

    def test_aligned_when_identity_holds(self):
        self._seed_broker(qty=100)
        self._seed_book(qty=100)
        report = asyncio.run(self._service().reconcile_account("kite:A"))
        self.assertEqual([c["divergence_class"] for c in report["coordinates"]], ["aligned"])
        self.assertEqual(report["coordinates"][0]["residual_quantity"], 0)
        self.assertEqual(report["frozen"], [])

    def test_pending_ingest_then_aligned_after_refresh(self):
        """A missing fill is `pending_ingest`, and ingesting it re-aligns."""
        self._seed_broker(qty=90)
        self._seed_book(qty=100)
        missing = [self._trade("T-MISSING", order_id="OID-M", side="SELL", qty=10)]

        class _Ingest:
            def __init__(self):
                self.calls = 0

            async def ingest_account(self, account_id):
                self.calls += 1
                # The bounded refresh is what finds the missing truth.
                self.store.ingest_trades(account_id=account_id, trades=missing)
                return {"account_id": account_id, "inserted": 1}

        ingest = _Ingest()
        ingest.store = self.store
        service = self._service(ingest_service=ingest, max_attempts=3)
        first = asyncio.run(service.reconcile_account("kite:A"))
        self.assertEqual(first["coordinates"][0]["divergence_class"], "pending_ingest")

        # Now that the fill is ingested, the identity holds (100 = 100 + 0 - ...).
        # The 10 the human sold is manual, so broker 90 == attributed 100 + manual -10.
        second = asyncio.run(service.reconcile_account("kite:A"))
        self.assertEqual(second["coordinates"][0]["divergence_class"], "aligned")
        self.assertEqual(second["coordinates"][0]["manual_quantity"], -10)
        self.assertEqual(second["frozen"], [])

    def test_unexplained_after_bounded_attempts_escalates_once(self):
        self._seed_broker(qty=90)
        self._seed_book(qty=100)  # no fill will ever arrive for the missing 10
        notifications = []

        def notifier(account_id, divergence, coordinate, detail):
            notifications.append((account_id, divergence, coordinate))
            return True

        service = self._service(max_attempts=2, notifier=notifier)
        for _ in range(5):
            asyncio.run(service.reconcile_account("kite:A"))

        state = self.store.reconciliation_state(account_id="kite:A")[_coord()]
        self.assertEqual(state["divergence_class"], "unexplained")
        # Idempotent escalation: duplicate checks never re-notify.
        self.assertEqual(len(notifications), 1)
        self.assertIsNotNone(state["owner_notified_at"])

    def test_failed_dispatch_leaves_the_stamp_unset_for_retry(self):
        self._seed_broker(qty=90)
        self._seed_book(qty=100)
        sent = []

        def failing(account_id, divergence, coordinate, detail):
            sent.append(1)
            return False  # e.g. no channel resolved

        service = self._service(max_attempts=1, notifier=failing)
        asyncio.run(service.reconcile_account("kite:A"))
        asyncio.run(service.reconcile_account("kite:A"))
        state = self.store.reconciliation_state(account_id="kite:A")[_coord()]
        # Classification and the freeze never depend on notification success.
        self.assertEqual(state["divergence_class"], "unexplained")
        self.assertIsNone(state["owner_notified_at"])
        self.assertEqual(len(sent), 2)  # retried on the later check

    def test_freeze_is_coordinate_scoped(self):
        self._seed_broker(qty=90)
        self._seed_broker(qty=50, token=408065, symbol="INFY")
        self._seed_book(qty=100)
        self._seed_book(qty=50, sid="stg-2", token=408065, symbol="INFY")
        service = self._service(max_attempts=1)
        asyncio.run(service.reconcile_account("kite:A"))
        self.assertEqual(
            self.store.is_frozen_coordinate(account_id="kite:A", coordinate=_coord()),
            "unexplained",
        )
        # A different instrument on the same account trades freely.
        self.assertIsNone(
            self.store.is_frozen_coordinate(account_id="kite:A", coordinate=_coord(token=408065, symbol="INFY"))
        )


class FreezeRefusalTests(unittest.TestCase):
    """The freeze decision itself: asymmetric, named and coordinate-scoped."""

    def test_freeze_refuses_exposure_increasing_live_order(self):
        from backend.strategies.account_truth import reconciliation_refusal

        refusal = reconciliation_refusal(
            account_id="kite:A", coordinate=_coord(), divergence_class="unexplained",
            side="BUY", net_quantity=40,
        )
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["rejection_reason"], "RECONCILIATION_FREEZE")
        self.assertEqual(refusal["coordinate"]["tradingsymbol"], "RELIANCE")
        self.assertEqual(refusal["divergence_class"], "unexplained")
        # pending_ingest freezes too — an uncertain residual is never harmless.
        self.assertIsNotNone(
            reconciliation_refusal(
                account_id="kite:A", coordinate=_coord(), divergence_class="pending_ingest",
                side="BUY", net_quantity=40,
            )
        )

    def test_reducing_exit_permitted_under_freeze(self):
        from backend.strategies.account_truth import reconciliation_refusal

        self.assertIsNone(
            reconciliation_refusal(
                account_id="kite:A", coordinate=_coord(), divergence_class="unexplained",
                side="SELL", net_quantity=100,
            )
        )
        self.assertIsNone(
            reconciliation_refusal(
                account_id="kite:A", coordinate=_coord(), divergence_class="unexplained",
                side="BUY", net_quantity=-100,
            )
        )
        # Aligned admits everything.
        self.assertIsNone(
            reconciliation_refusal(
                account_id="kite:A", coordinate=_coord(), divergence_class="aligned",
                side="BUY", net_quantity=0,
            )
        )

    def test_negative_manual_residual_blocks_full_exit_with_named_reason(self):
        """Walkthrough 7 case 2: broker 70, the strategy owns 100."""
        from backend.strategies.account_truth import unresolved_exit_detail

        detail = unresolved_exit_detail(
            divergence_class="unexplained", manual_quantity=-30, broker_net=70
        )
        self.assertEqual(detail["rejection_reason"], "MANUAL_RESIDUAL_BLOCKS_FULL_EXIT")
        self.assertEqual(detail["manual_quantity"], -30)
        self.assertIn("unexplained", detail["message"])
        self.assertIn("30", detail["message"])


if __name__ == "__main__":
    unittest.main()
