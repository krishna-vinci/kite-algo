import unittest
from datetime import datetime, timezone

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs(stub_kite_orders=False)

from backend.broker_api.market.candle_aggregator import CandleAggregator


class _FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def publish(self, channel, payload):
        return 1


class CandleAggregatorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_tick_processes_iso_exchange_timestamp(self):
        aggregator = CandleAggregator("test-key")
        aggregator.redis = _FakeRedis()
        aggregator.intervals = ["minute"]
        aggregator.source = "market_runtime"

        await aggregator._process_ticks(
            [
                {
                    "instrument_token": 256265,
                    "last_price": 22450.5,
                    "exchange_timestamp": "2026-04-05T09:45:10+05:30",
                    "volume_traded": 100,
                    "oi": 50,
                }
            ]
        )

        state = aggregator.candle_states[(256265, "minute")]
        self.assertEqual(state.open, 22450.5)
        self.assertEqual(state.close, 22450.5)
        self.assertEqual(state.oi, 50)
        self.assertEqual(state.bucket_start_ts.tzinfo, timezone.utc)

    def test_normalize_tick_timestamp_handles_iso_string(self):
        aggregator = CandleAggregator("test-key")
        ts = aggregator._normalize_tick_timestamp("2026-04-05T09:45:10+05:30")
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts.tzinfo, timezone.utc)

    def test_daily_bucket_uses_nse_ist_trading_date(self):
        aggregator = CandleAggregator("test-key")
        tick_ts = datetime.fromisoformat("2026-09-04T09:45:10+05:30").astimezone(timezone.utc)

        bucket = aggregator._get_bucket_start(tick_ts, "day")

        self.assertEqual(bucket, datetime.fromisoformat("2026-09-03T18:30:00+00:00"))


if __name__ == "__main__":
    unittest.main()


class CandleAggregatorAlertTokenTests(unittest.IsolatedAsyncioTestCase):
    """The aggregator must build candles for what ACTIVE alerts need.

    Regression: its token source was ``user_watchlists`` (plus in-process
    external tokens, which nothing sets for the alerts path) while the alerts
    worker subscribes under its OWN market-runtime owner. The two sets are
    disjoint, so with an empty watchlist a candle-close workflow never received
    a single completed candle — no history row, no live completion — and
    everything validation restricts to that clock (all Phase 4 breadth and
    advanced conditions) could not run in production at all.
    """

    def _factory(self):
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from backend.workflows import advanced_repository  # noqa: F401 (tables)
        from backend.workflows.repository import Base

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        # The aggregator queries schema-qualified names (`public.<table>`) as
        # it does against PostgreSQL; SQLite needs the schema attached.
        @event.listens_for(engine, "connect")
        def _attach_public_schema(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("ATTACH DATABASE ':memory:' AS public")
            cursor.close()

        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine, expire_on_commit=False)

    def _seed(self, factory, *, candle_clock=True, archived=False):
        import uuid as _uuid

        from backend.workflows.repository import (
            AlertSubscription,
            Workflow as WorkflowModel,
            WorkflowRevision,
        )
        from backend.workflows.compiler import compile_document
        from backend.workflows.parser import parse_workflow_dict

        stage = {
            "id": "s", "type": "signal",
            "clock": "candle_close" if candle_clock else "ltp",
            "conditions": {"all": [{"field": "close", "op": "gt", "value": 1}]},
        }
        if candle_clock:
            stage["timeframe"] = "5minute"
        doc = {
            "version": 1, "name": "agg-token-test", "session": "mcx_commodity",
            "instruments": ["MCX:TESTFUT"],
            "stages": [stage],
            "alerts": [{"id": "a", "source": "s"}],
        }
        compiled = compile_document(parse_workflow_dict(doc))
        workflow_id = str(_uuid.uuid4())
        revision_id = str(_uuid.uuid4())
        with factory() as session:
            session.add(WorkflowModel(
                id=workflow_id, owner_id="owner-1", name=f"agg-token-{workflow_id[:8]}",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))
            session.add(WorkflowRevision(
                id=revision_id, workflow_id=workflow_id, revision=1,
                document=compiled.document.to_document_dict(),
                canonical_hash=compiled.canonical_hash, status="active",
            ))
            session.add(AlertSubscription(
                id=str(_uuid.uuid4()), revision_id=revision_id, alert_id="a",
                stage_id="s", instrument_symbol="TESTFUT", instrument_exchange="MCX",
                instrument_key="MCX:TESTFUT", trigger="on_transition",
                config={}, state="active",
            ))
            session.commit()
        return workflow_id, revision_id

    async def test_candle_clock_subscription_yields_a_token(self):
        from backend.broker_api.instruments import catalog as catalog_module
        from backend.broker_api.market import candle_aggregator as agg_module

        engine, factory = self._factory()
        try:
            self._seed(factory, candle_clock=True)

            original_db = agg_module.get_db
            agg_module.get_db = lambda: iter([factory()])
            original_catalog = catalog_module.InstrumentCatalog

            class _Descriptor:
                broker_token = 999001

            class _Catalog:
                def __init__(self, *args, **kwargs):
                    pass

                def resolve_public_key(self, key):
                    assert key == "MCX:TESTFUT", key
                    return _Descriptor()

            catalog_module.InstrumentCatalog = _Catalog
            try:
                aggregator = CandleAggregator("test-key")
                tokens = await aggregator._get_alert_tokens()
            finally:
                agg_module.get_db = original_db
                catalog_module.InstrumentCatalog = original_catalog
            self.assertEqual(tokens, {999001})
        finally:
            engine.dispose()

    async def test_ltp_only_subscription_yields_no_token(self):
        """An LTP-only rule needs no candle; subscribing it would waste quota."""
        from backend.broker_api.instruments import catalog as catalog_module
        from backend.broker_api.market import candle_aggregator as agg_module

        engine, factory = self._factory()
        try:
            self._seed(factory, candle_clock=False)

            original_db = agg_module.get_db
            agg_module.get_db = lambda: iter([factory()])
            original_catalog = catalog_module.InstrumentCatalog
            called = []

            class _Catalog:
                def __init__(self, *args, **kwargs):
                    pass

                def resolve_public_key(self, key):  # pragma: no cover
                    called.append(key)
                    raise AssertionError("must not resolve LTP-only instruments")

            catalog_module.InstrumentCatalog = _Catalog
            try:
                aggregator = CandleAggregator("test-key")
                tokens = await aggregator._get_alert_tokens()
            finally:
                agg_module.get_db = original_db
                catalog_module.InstrumentCatalog = original_catalog
            self.assertEqual(tokens, set())
            self.assertEqual(called, [])
        finally:
            engine.dispose()

    async def test_non_active_dependencies_are_excluded(self):
        """Only ACTIVE, non-archived candle dependencies consume quota.

        A paused subscription or an archived workflow must not keep a token
        subscribed: otherwise a retired rule holds market-data quota forever.
        The mock catalog maps its key to a token so an included dependency is
        OBSERVABLE — asserting only "no tokens" would also pass if the lookup
        were silently failing, which is the bug class this guards.
        """
        from backend.broker_api.instruments import catalog as catalog_module
        from backend.broker_api.market import candle_aggregator as agg_module
        from backend.workflows.repository import (
            AlertSubscription,
            Workflow as WorkflowModel,
            WorkflowRevision,
        )

        engine, factory = self._factory()

        class _Descriptor:
            def __init__(self, token):
                self.broker_token = token

        class _Catalog:
            def __init__(self, *args, **kwargs):
                pass

            def resolve_public_key(self, key):
                return _Descriptor(999001)

        async def _resolve_tokens():
            original_db = agg_module.get_db
            agg_module.get_db = lambda: iter([factory()])
            original_catalog = catalog_module.InstrumentCatalog
            catalog_module.InstrumentCatalog = _Catalog
            try:
                aggregator = CandleAggregator("test-key")
                return await aggregator._get_alert_tokens()
            finally:
                agg_module.get_db = original_db
                catalog_module.InstrumentCatalog = original_catalog

        try:
            workflow_id, revision_id = self._seed(factory, candle_clock=True)

            # Active dependency: the token IS tracked (the positive control —
            # without this the negatives below could pass for the wrong reason).
            self.assertEqual(await _resolve_tokens(), {999001})

            # Archived workflow.
            with factory() as session:
                session.get(WorkflowModel, workflow_id).archived_at = (
                    datetime.now(timezone.utc)
                )
                session.commit()
            self.assertEqual(await _resolve_tokens(), set())

            # Un-archive, then pause the subscription instead.
            with factory() as session:
                session.get(WorkflowModel, workflow_id).archived_at = None
                session.query(AlertSubscription).filter_by(
                    revision_id=revision_id
                ).one().state = "paused"
                session.commit()
            self.assertEqual(await _resolve_tokens(), set())

            # A superseded (non-active) revision is not a dependency either.
            with factory() as session:
                session.query(AlertSubscription).filter_by(
                    revision_id=revision_id
                ).one().state = "active"
                session.query(WorkflowRevision).filter_by(
                    id=revision_id
                ).one().status = "superseded"
                session.commit()
            self.assertEqual(await _resolve_tokens(), set())
        finally:
            engine.dispose()

    async def test_watchlist_consumers_are_preserved(self):
        """Alert tokens are ADDED to the watchlist set, never replace it."""
        aggregator = CandleAggregator("test-key")
        aggregator.running = True
        aggregator.owner_id = "candles:all:test"
        synced = {}

        async def _no_watchlist():
            return set()

        async def _alert_tokens():
            return {999001}

        async def _capture(desired):
            synced["tokens"] = set(desired)

        aggregator._get_alert_tokens = _alert_tokens
        aggregator._sync_market_runtime_subscriptions = _capture

        # Watchlist consumers are the base set...
        async def _watchlist():
            return {111, 222}

        aggregator._get_watchlist_tokens = _watchlist
        await aggregator._refresh_subscriptions()
        self.assertEqual(synced.get("tokens"), {111, 222, 999001})

        # ...and dropping the alert dependency leaves them intact, which is
        # also how a removed dependency stops consuming resources.
        async def _no_alerts():
            return set()

        aggregator._get_alert_tokens = _no_alerts
        await aggregator._refresh_subscriptions()
        self.assertEqual(synced.get("tokens"), {111, 222})
        self.assertEqual(aggregator.subscribed_tokens, {111, 222})

    async def test_refresh_subscriptions_includes_alert_tokens(self):
        """Guard the WIRING, not just the method.

        ``_get_alert_tokens`` existing is not enough: the refresh loop must
        actually merge it into the token set it syncs, or the aggregator keeps
        tracking only watchlists and candle-close workflows still never
        evaluate. This asserts the merged payload.
        """
        aggregator = CandleAggregator("test-key")
        aggregator.running = True
        aggregator.owner_id = "candles:all:test"
        synced = {}

        async def _no_watchlist():
            return set()

        async def _alert_tokens():
            return {999001}

        async def _capture(desired):
            synced["tokens"] = set(desired)

        aggregator._get_watchlist_tokens = _no_watchlist
        aggregator._get_alert_tokens = _alert_tokens
        aggregator._sync_market_runtime_subscriptions = _capture

        await aggregator._refresh_subscriptions()

        self.assertEqual(synced.get("tokens"), {999001})
        self.assertEqual(aggregator.subscribed_tokens, {999001})
