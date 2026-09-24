"""Run attribution on the NORMAL production path.

``_run_strategy_attribution`` used to read only
``app.state.strategies_session_factory``. Production never sets that attribute
(only tests and the isolated acceptance apps do), so every production run
reported ``unattributed`` and a strategy could not discover its own identity.

These tests pin the real path: the ordinary ``SessionLocal`` fallback is used
when no factory was injected, the persisted binding is authoritative only when
it AGREES with the run's own account/environment, and an unknown run stays
``unattributed`` rather than guessing.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.support.test_support import install_dependency_stubs

install_dependency_stubs()

import backend.strategies.attribution_models  # noqa: E402,F401  (registers the tables)
from backend.api.routers.worker_auth import _run_strategy_attribution  # noqa: E402
from backend.strategies.attribution_models import Strategy, StrategyRunBinding  # noqa: E402
from backend.workflows.repository import Base  # noqa: E402

ACCOUNT = "kite:paper-attribution"
ENVIRONMENT = "paper"
OWNER = "app:admin"


class _App:
    def __init__(self, state):
        self.state = state


class _Request:
    """A minimal Request: only ``app.state`` is read."""

    def __init__(self, state):
        self.app = _App(state)


class _State:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class WorkerRunAttributionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as session:
            session.add(
                Strategy(
                    id="stg-1",
                    owner_id=OWNER,
                    name="attribution test",
                    account_scope=ACCOUNT,
                    status="active",
                )
            )
            session.add(
                StrategyRunBinding(
                    strategy_run_id="run-1",
                    strategy_id="stg-1",
                    owner_id=OWNER,
                    account_id=ACCOUNT,
                    execution_environment=ENVIRONMENT,
                    bound_by="supervisor",
                    binding_source="hosted_job",
                )
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def _patch_default_factory(self):
        """Point the module-level ``SessionLocal`` the fallback imports at this DB."""
        import backend.app.database as database

        original = database.SessionLocal
        database.SessionLocal = self.factory
        self.addCleanup(setattr, database, "SessionLocal", original)

    def test_normal_route_resolves_the_binding_without_an_injected_factory(self):
        self._patch_default_factory()
        request = _Request(_State())  # NOTE: no strategies_session_factory

        attribution = _run_strategy_attribution(
            request,
            "run-1",
            account_scope=ACCOUNT,
            execution_environment=ENVIRONMENT,
        )

        self.assertIsInstance(attribution, dict)
        self.assertEqual(attribution["strategy_id"], "stg-1")
        self.assertEqual(attribution["owner_id"], OWNER)
        self.assertEqual(attribution["account_id"], ACCOUNT)
        self.assertEqual(attribution["execution_environment"], ENVIRONMENT)
        self.assertEqual(attribution["binding_source"], "hosted_job")

    def test_an_injected_factory_still_wins_over_the_default(self):
        request = _Request(_State(strategies_session_factory=self.factory))

        attribution = _run_strategy_attribution(
            request,
            "run-1",
            account_scope=ACCOUNT,
            execution_environment=ENVIRONMENT,
        )

        self.assertEqual(attribution["strategy_id"], "stg-1")

    def test_an_unknown_run_is_unattributed_not_a_guess(self):
        self._patch_default_factory()
        request = _Request(_State())

        attribution = _run_strategy_attribution(
            request,
            "run-unknown",
            account_scope=ACCOUNT,
            execution_environment=ENVIRONMENT,
        )

        self.assertEqual(attribution, "unattributed")

    def test_an_empty_run_id_is_unattributed(self):
        self._patch_default_factory()
        request = _Request(_State())

        self.assertEqual(
            _run_strategy_attribution(
                request, "", account_scope=ACCOUNT, execution_environment=ENVIRONMENT
            ),
            "unattributed",
        )

    def test_a_binding_for_another_account_is_unattributed(self):
        self._patch_default_factory()
        request = _Request(_State())

        attribution = _run_strategy_attribution(
            request,
            "run-1",
            account_scope="kite:paper-SOMEBODY-ELSE",
            execution_environment=ENVIRONMENT,
        )

        self.assertEqual(attribution, "unattributed")

    def test_a_binding_for_another_environment_is_unattributed(self):
        self._patch_default_factory()
        request = _Request(_State())

        attribution = _run_strategy_attribution(
            request,
            "run-1",
            account_scope=ACCOUNT,
            execution_environment="live",
        )

        self.assertEqual(attribution, "unattributed")

    def test_an_unreadable_store_is_unattributed(self):
        """A store that cannot be read is unknown coverage, never a guess."""

        def _boom():
            raise RuntimeError("the store is unavailable")

        request = _Request(_State(strategies_session_factory=_boom))
        self.assertEqual(
            _run_strategy_attribution(
                request,
                "run-1",
                account_scope=ACCOUNT,
                execution_environment=ENVIRONMENT,
            ),
            "unattributed",
        )


if __name__ == "__main__":
    unittest.main()
