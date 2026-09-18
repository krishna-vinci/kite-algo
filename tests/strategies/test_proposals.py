"""Proposal envelopes, pinned catalog reads and plan compilation (G5).

The pinned-generation read exists because the current-only view
(``public.instrument_catalog_published_v``) resolves whatever the catalog says
*now*. A frozen plan must resolve against the generation that was published when
the decision was validated, so this suite pins the difference directly: two
generations re-mapping one broker token, and a read at each generation returning
that generation's instrument while the current view returns only the newer one.

SQLite runs with the established ``public.`` ATTACH fixture.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.strategies.compiler.base import member_hash
from backend.workflows.repository import Base
import backend.strategies.models  # noqa: F401  registers the hosted tables on Base
import backend.strategies.attribution_models  # noqa: F401  registers the proposal tables

G1 = "11111111-1111-1111-1111-111111111111"
G2 = "22222222-2222-2222-2222-222222222222"
G_STAGING = "33333333-3333-3333-3333-333333333333"
G3 = "44444444-4444-4444-4444-444444444444"
T3 = "2026-09-15T00:00:00+00:00"
INST_OLD = "aaaaaaaa-0000-0000-0000-000000000001"
INST_NEW = "bbbbbbbb-0000-0000-0000-000000000002"
INST_OTHER = "cccccccc-0000-0000-0000-000000000003"
T1 = "2026-09-01T00:00:00+00:00"
T2 = "2026-09-10T00:00:00+00:00"


class ProposalTestCase(unittest.TestCase):
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
                CREATE TABLE public.instrument_catalog_generations (
                    id TEXT PRIMARY KEY, status TEXT, published_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_catalog_records (
                    instrument_id TEXT PRIMARY KEY, exchange TEXT, tradingsymbol TEXT,
                    lifecycle_status TEXT NOT NULL DEFAULT 'active',
                    current_generation_id TEXT,
                    instrument_type TEXT, expiry TEXT, lot_size INTEGER, tick_size REAL,
                    underlying TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.instrument_broker_mappings (
                    mapping_id TEXT PRIMARY KEY, instrument_id TEXT, broker TEXT,
                    broker_exchange TEXT, broker_symbol TEXT, broker_token INTEGER,
                    valid_from_generation TEXT, valid_to_generation TEXT, is_current INTEGER
                )
                """
            )
            # The current-only view: exactly the shape the production view has
            # (published/degraded generation + current mapping + non-retired).
            cursor.execute(
                """
                CREATE VIEW public.instrument_catalog_published_v AS
                SELECT r.instrument_id AS instrument_id,
                       r.exchange AS exchange,
                       r.tradingsymbol AS tradingsymbol,
                       r.lifecycle_status AS lifecycle_status,
                       m.broker AS broker,
                       m.broker_symbol AS broker_symbol,
                       m.broker_token AS broker_token,
                       r.current_generation_id AS catalog_generation
                FROM public.instrument_catalog_records r
                JOIN public.instrument_catalog_generations g
                  ON g.id = r.current_generation_id
                 AND g.status IN ('published', 'degraded')
                 AND r.lifecycle_status <> 'retired'
                JOIN public.instrument_broker_mappings m
                  ON m.instrument_id = r.instrument_id
                 AND m.is_current = 1
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.universe_revisions (
                    id TEXT PRIMARY KEY, universe_id TEXT, revision INTEGER,
                    members TEXT NOT NULL DEFAULT '[]', member_count INTEGER NOT NULL DEFAULT 0,
                    source_generation TEXT, coverage TEXT NOT NULL DEFAULT '{}',
                    resolved_at TEXT, created_at TEXT
                )
                """
            )
            dbapi_connection.commit()

        self.factory = sessionmaker(bind=self.engine)
        self._register_orm_tables()

    def _register_orm_tables(self):
        from backend.strategies.attribution_models import (
            Strategy,
            StrategyPlan,
            StrategyProposal,
            StrategyProposalJournal,
        )
        from backend.workflows.repository import Base as _Base

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyProposalJournal.__table__,
            ],
        )

    def tearDown(self):
        self.engine.dispose()

    # -- seeding ------------------------------------------------------------

    def seed_generation(self, gid, status, published_at):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_generations (id, status, published_at) "
                    "VALUES (:id, :status, :published_at)"
                ),
                {"id": gid, "status": status, "published_at": published_at},
            )
            session.commit()

    def seed_record(self, instrument_id, *, symbol, lifecycle="active", generation=G2):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_catalog_records "
                    "(instrument_id, exchange, tradingsymbol, lifecycle_status, current_generation_id) "
                    "VALUES (:id, 'NSE', :symbol, :lifecycle, :generation)"
                ),
                {
                    "id": instrument_id,
                    "symbol": symbol,
                    "lifecycle": lifecycle,
                    "generation": generation,
                },
            )
            session.commit()

    def seed_mapping(self, mapping_id, instrument_id, *, token, symbol, valid_from, valid_to=None,
                     is_current=1):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.instrument_broker_mappings "
                    "(mapping_id, instrument_id, broker, broker_exchange, broker_symbol, "
                    " broker_token, valid_from_generation, valid_to_generation, is_current) "
                    "VALUES (:mid, :iid, 'kite', 'NSE', :symbol, :token, :vf, :vt, :cur)"
                ),
                {
                    "mid": mapping_id,
                    "iid": instrument_id,
                    "symbol": symbol,
                    "token": token,
                    "vf": valid_from,
                    "vt": valid_to,
                    "cur": is_current,
                },
            )
            session.commit()

    def seed_universe_revision(self, revision_id, members, *, universe_id="uni-1", revision=1,
                               source_generation=G1):
        import json as _json

        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO public.universe_revisions "
                    "(id, universe_id, revision, members, member_count, source_generation, coverage, "
                    " resolved_at, created_at) "
                    "VALUES (:id, :universe_id, :revision, :members, :count, :gen, '{}', "
                    " '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
                ),
                {
                    "id": revision_id,
                    "universe_id": universe_id,
                    "revision": revision,
                    "members": _json.dumps(sorted(members)),
                    "count": len(members),
                    "gen": source_generation,
                },
            )
            session.commit()

    def seed_remapped_catalog(self):
        """One token, two generations: G1 mapped it to INST_OLD, G2 to INST_NEW."""
        self.seed_generation(G1, "published", T1)
        self.seed_generation(G2, "published", T2)
        self.seed_record(INST_OLD, symbol="RELIANCE", lifecycle="active", generation=G1)
        self.seed_record(INST_NEW, symbol="RELIANCE", lifecycle="active", generation=G2)
        self.seed_mapping("m-old", INST_OLD, token=100, symbol="RELIANCE", valid_from=G1,
                          valid_to=G2, is_current=0)
        self.seed_mapping("m-new", INST_NEW, token=100, symbol="RELIANCE", valid_from=G2,
                          is_current=1)


# ---------------------------------------------------------------------------
# Task 2: pinned read, compiler registry, single_instrument, plan hash
# ---------------------------------------------------------------------------


class PinnedCatalogReadTests(ProposalTestCase):
    def _read(self, generation):
        from backend.strategies.compiler.base import PinnedCatalogRead

        return PinnedCatalogRead(session_factory=self.factory, generation=generation)

    def test_pinned_read_resolves_generation_content_not_current(self):
        self.seed_remapped_catalog()

        at_g1 = self._read(G1).resolve_token(100, exchange="NSE", symbol="RELIANCE")
        at_g2 = self._read(G2).resolve_token(100, exchange="NSE", symbol="RELIANCE")
        self.assertEqual(at_g1["instrument_id"], INST_OLD)
        self.assertEqual(at_g2["instrument_id"], INST_NEW)

        # The current-only view knows nothing about the older pin.
        with self.factory() as session:
            current = [
                str(row[0])
                for row in session.execute(
                    text(
                        "SELECT instrument_id FROM public.instrument_catalog_published_v "
                        "WHERE broker = 'kite' AND broker_token = 100"
                    )
                ).fetchall()
            ]
        self.assertEqual(current, [INST_NEW])

    def test_pinned_read_rejects_unpublished_generation(self):
        from backend.strategies.compiler.base import PinnedCatalogRead, ValidationRefusal

        self.seed_generation(G_STAGING, "staging", None)
        with self.assertRaises(ValidationRefusal) as ctx:
            PinnedCatalogRead(
                session_factory=self.factory, generation=G_STAGING
            ).pin()
        self.assertEqual(ctx.exception.reason_code, "CATALOG_GENERATION_NOT_PUBLISHED")

        # An unknown generation is refused the same way, never resolved as "current".
        with self.assertRaises(ValidationRefusal) as ctx:
            PinnedCatalogRead(
                session_factory=self.factory, generation="99999999-9999-9999-9999-999999999999"
            ).pin()
        self.assertEqual(ctx.exception.reason_code, "CATALOG_GENERATION_NOT_PUBLISHED")

    def test_current_published_generation_is_available_for_defaulting(self):
        self.seed_generation(G1, "published", T1)
        self.seed_generation(G2, "published", T2)
        from backend.strategies.compiler.base import PinnedCatalogRead

        read = PinnedCatalogRead(session_factory=self.factory)
        self.assertEqual(read.current_published_generation(), G2)
        self.assertEqual(read.pin(), G2)


class CompilerRegistryTests(ProposalTestCase):
    def test_compiler_registry_unknown_kind_refuses(self):
        from backend.strategies.compiler import compiler_for
        from backend.strategies.compiler.base import ValidationRefusal

        self.assertIsNotNone(compiler_for("single_instrument"))
        with self.assertRaises(ValidationRefusal) as ctx:
            compiler_for("iron_condor")
        self.assertEqual(ctx.exception.reason_code, "TARGET_KIND_UNKNOWN")


class SingleInstrumentTests(ProposalTestCase):
    def test_single_instrument_resolution(self):
        from backend.strategies.compiler import compile_plan
        from backend.strategies.compiler.base import PinnedCatalogRead, ValidationRefusal

        self.seed_remapped_catalog()
        pinned = PinnedCatalogRead(session_factory=self.factory, generation=G1)

        resolved = compile_plan(
            "single_instrument",
            {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": 25,
            },
            pinned,
        )
        self.assertEqual(resolved["target_kind"], "single_instrument")
        leg = resolved["legs"][0]
        self.assertEqual(leg["instrument_id"], INST_OLD)
        self.assertEqual(leg["signed_quantity"], 25)
        self.assertEqual(leg["exchange"], "NSE")
        self.assertEqual(leg["tradingsymbol"], "RELIANCE")

        # A negative target is a short, not a refusal.
        short = compile_plan(
            "single_instrument",
            {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": -25,
            },
            pinned,
        )
        self.assertEqual(short["legs"][0]["signed_quantity"], -25)

        # Zero is a real instruction (flatten), not "absent".
        flat = compile_plan(
            "single_instrument",
            {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": 0,
            },
            pinned,
        )
        self.assertEqual(flat["legs"][0]["signed_quantity"], 0)

        with self.assertRaises(ValidationRefusal) as ctx:
            compile_plan(
                "single_instrument",
                {
                    "instrument_token": 999,
                    "exchange": "NSE",
                    "tradingsymbol": "NOPE",
                    "product": "CNC",
                    "target_quantity": 1,
                },
                pinned,
            )
        self.assertEqual(ctx.exception.reason_code, "INSTRUMENT_UNRESOLVED")


class PlanHashTests(ProposalTestCase):
    def _resolved(self):
        from backend.strategies.compiler import compile_plan
        from backend.strategies.compiler.base import PinnedCatalogRead

        self.seed_remapped_catalog()
        pinned = PinnedCatalogRead(session_factory=self.factory, generation=G1)
        return compile_plan(
            "single_instrument",
            {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": 25,
            },
            pinned,
        )

    def test_plan_hash_stable_and_discriminating(self):
        from backend.strategies.compiler.base import (
            canonical_json,
            compute_plan_hash,
            plan_pin,
        )

        resolved = self._resolved()
        logical = {"target_kind": "single_instrument", "target_quantity": 25}
        pin = plan_pin(pinned_catalog_generation=G1)

        first = compute_plan_hash(logical=logical, resolved=resolved, pin=pin)
        second = compute_plan_hash(logical=logical, resolved=resolved, pin=plan_pin(
            pinned_catalog_generation=G1
        ))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

        # Identical content hashes identically regardless of dict insertion order.
        reordered = {"target_quantity": 25, "target_kind": "single_instrument"}
        self.assertEqual(
            compute_plan_hash(logical=reordered, resolved=resolved, pin=pin), first
        )
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

        # Any change to logical, resolved or the pin is discriminating.
        self.assertNotEqual(
            compute_plan_hash(logical={**logical, "target_quantity": 26}, resolved=resolved, pin=pin),
            first,
        )
        self.assertNotEqual(
            compute_plan_hash(
                logical=logical,
                resolved={**resolved, "legs": []},
                pin=pin,
            ),
            first,
        )
        self.assertNotEqual(
            compute_plan_hash(
                logical=logical, resolved=resolved, pin=plan_pin(pinned_catalog_generation=G2)
            ),
            first,
        )


class TargetWeightsTests(ProposalTestCase):
    """D-6: the scope is the pinned revision, so omission means zero."""

    REV = "dddddddd-0000-0000-0000-0000000000aa"

    def _seed_members(self, members=("RELIANCE", "INFY", "TCS")):
        for index, member in enumerate(members):
            self.seed_record(f"inst-{member}", symbol=member, generation=G2)
            self.seed_mapping(f"map-{member}", f"inst-{member}", token=100 + index, symbol=member,
                              valid_from=G2, is_current=1)

    def _pinned(self, generation=G2):
        from backend.strategies.compiler.base import PinnedCatalogRead

        return PinnedCatalogRead(session_factory=self.factory, generation=generation)

    def _compile(self, payload, generation=G2):
        from backend.strategies.compiler import compile_resolved_plan

        return compile_resolved_plan("target_weights", payload, self._pinned(generation))

    def test_full_snapshot_omission_means_zero(self):
        self.seed_generation(G2, "published", T2)
        self._seed_members()
        self.seed_universe_revision(self.REV, ["RELIANCE", "INFY", "TCS"])

        plan = self._compile(
            {
                "universe_revision_id": self.REV,
                "target_weights": {"RELIANCE": 0.5, "INFY": 0.25},
            }
        )
        # All three members are present; the omitted one is an explicit zero.
        weights = {leg["tradingsymbol"]: leg["target_weight"] for leg in plan.resolved["legs"]}
        self.assertEqual(weights, {"RELIANCE": 0.5, "INFY": 0.25, "TCS": 0.0})
        tcs = next(leg for leg in plan.resolved["legs"] if leg["tradingsymbol"] == "TCS")
        self.assertTrue(tcs["explicit_zero"])
        self.assertEqual(plan.universe_revision_id, self.REV)
        self.assertEqual(plan.member_hash, member_hash(["RELIANCE", "INFY", "TCS"]))

    def test_out_of_scope_instrument_untouched(self):
        self.seed_generation(G2, "published", T2)
        self._seed_members()
        # A mapped instrument that is NOT a member of the pinned revision.
        self.seed_record("inst-OTHER", symbol="WIPRO", generation=G2)
        self.seed_mapping("map-OTHER", "inst-OTHER", token=900, symbol="WIPRO", valid_from=G2,
                          is_current=1)
        self.seed_universe_revision(self.REV, ["RELIANCE", "INFY", "TCS"])

        plan = self._compile(
            {"universe_revision_id": self.REV, "target_weights": {"RELIANCE": 1.0}}
        )
        symbols = [leg["tradingsymbol"] for leg in plan.resolved["legs"]]
        self.assertEqual(sorted(symbols), ["INFY", "RELIANCE", "TCS"])
        # No zero row is invented for an instrument outside the scope.
        self.assertNotIn("WIPRO", symbols)

        # And a weight FOR an out-of-scope instrument is a malformed payload: the
        # scope is the snapshot, so there is no defined meaning for it.
        from backend.strategies.compiler.base import ValidationRefusal

        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(
                {"universe_revision_id": self.REV, "target_weights": {"WIPRO": 0.5}}
            )
        self.assertEqual(ctx.exception.reason_code, "UNIVERSE_MEMBER_UNRESOLVED")
        self.assertEqual(ctx.exception.detail["outside_scope"], ["WIPRO"])

    def test_universe_revision_and_member_hash_pinned(self):
        from backend.strategies.compiler.base import ValidationRefusal

        self.seed_generation(G2, "published", T2)
        self._seed_members()
        self.seed_universe_revision(self.REV, ["RELIANCE", "INFY", "TCS"])

        plan = self._compile(
            {"universe_revision_id": self.REV, "target_weights": {"INFY": 1.0}}
        )
        self.assertEqual(plan.resolved["universe_revision_id"], self.REV)
        self.assertEqual(plan.resolved["member_hash"], member_hash(["RELIANCE", "INFY", "TCS"]))
        self.assertEqual(plan.resolved["catalog_generation"], G2)

        # A payload built against different membership must not resolve against
        # this revision's scope.
        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(
                {
                    "universe_revision_id": self.REV,
                    "member_hash": member_hash(["RELIANCE", "INFY"]),
                    "target_weights": {"RELIANCE": 1.0},
                }
            )
        self.assertEqual(ctx.exception.reason_code, "UNIVERSE_MEMBER_UNRESOLVED")

        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(
                {
                    "universe_revision_id": self.REV,
                    "members": ["RELIANCE", "INFY"],
                    "target_weights": {"RELIANCE": 1.0},
                }
            )
        self.assertEqual(ctx.exception.reason_code, "UNIVERSE_MEMBER_UNRESOLVED")

        # An unknown revision is refused, never silently treated as empty.
        with self.assertRaises(ValidationRefusal) as ctx:
            self._compile(
                {"universe_revision_id": "no-such-revision", "target_weights": {}}
            )
        self.assertEqual(ctx.exception.reason_code, "UNIVERSE_REVISION_UNKNOWN")

    def test_weights_resolution_uses_pinned_generation(self):
        self.seed_remapped_catalog()
        self.seed_universe_revision(self.REV, ["RELIANCE"], source_generation=G1)

        at_g1 = self._compile(
            {"universe_revision_id": self.REV, "target_weights": {"RELIANCE": 1.0}},
            generation=G1,
        )
        at_g2 = self._compile(
            {"universe_revision_id": self.REV, "target_weights": {"RELIANCE": 1.0}},
            generation=G2,
        )
        # The SAME revision resolves differently per pin — which is exactly why the
        # generation is part of the scope and of the plan hash.
        self.assertEqual(at_g1.resolved["legs"][0]["instrument_id"], INST_OLD)
        self.assertEqual(at_g2.resolved["legs"][0]["instrument_id"], INST_NEW)
        self.assertEqual(at_g1.resolved["catalog_generation"], G1)


class ProposalStoreTests(ProposalTestCase):
    """D-1: evaluation identity, idempotency, conflict, refusal, frozen plan."""

    def setUp(self):
        super().setUp()
        self.seed_generation(G2, "published", T2)
        self.seed_record("inst-REL", symbol="RELIANCE", generation=G2)
        self.seed_mapping("map-REL", "inst-REL", token=100, symbol="RELIANCE", valid_from=G2,
                          is_current=1)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'Strategy A', 'kite:A', 'active')"
                )
            )
            session.commit()

    def _store(self):
        from backend.strategies.proposals import ProposalStore

        return ProposalStore(session_factory=self.factory)

    def _submission(self, **overrides):
        from backend.strategies.proposals import ProposalSubmission

        values = {
            "strategy_id": "stg-A",
            "account_id": "kite:A",
            "evaluation_id": "eval-1",
            "evaluation_kind": "run_now",
            "job_id": None,
            "strategy_run_id": "run-1",
            "target_kind": "single_instrument",
            "payload": {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "CNC",
                "target_quantity": 10,
            },
        }
        values.update(overrides)
        return ProposalSubmission(**values)

    def _journal(self):
        with self.factory() as session:
            return [
                (str(row[0]), str(row[1] or ""))
                for row in session.execute(
                    text(
                        "SELECT event, reason_code FROM strategy_proposal_journal "
                        "WHERE strategy_id = 'stg-A' ORDER BY created_at, event"
                    )
                ).fetchall()
            ]

    def test_exact_retry_is_idempotent(self):
        store = self._store()
        first = store.submit(self._submission())
        self.assertFalse(first["idempotent"])
        self.assertEqual(first["status"], "validated")
        self.assertIsNotNone(first["plan"])

        second = store.submit(self._submission())
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["proposal_id"], first["proposal_id"])
        # The envelope is not re-inserted and the plan is not re-created.
        with self.factory() as session:
            envelopes = session.execute(
                text("SELECT COUNT(*) FROM strategy_proposals")
            ).scalar()
            plans = session.execute(text("SELECT COUNT(*) FROM strategy_plans")).scalar()
        self.assertEqual((envelopes, plans), (1, 1))
        self.assertEqual(second["plan"]["plan_id"], first["plan"]["plan_id"])
        self.assertEqual([event for event, _ in self._journal()], ["received", "plan_created", "idempotent_retry"])

    def test_different_payload_same_evaluation_conflicts(self):
        from backend.strategies.proposals import ProposalConflict

        store = self._store()
        first = store.submit(self._submission())
        with self.assertRaises(ProposalConflict) as ctx:
            store.submit(
                self._submission(
                    payload={
                        "instrument_token": 100,
                        "exchange": "NSE",
                        "tradingsymbol": "RELIANCE",
                        "product": "CNC",
                        "target_quantity": 999,
                    }
                )
            )
        self.assertEqual(ctx.exception.reason_code, "PROPOSAL_EVALUATION_CONFLICT")

        # The original envelope is untouched, and nothing new was created.
        with self.factory() as session:
            count = session.execute(text("SELECT COUNT(*) FROM strategy_proposals")).scalar()
        self.assertEqual(count, 1)
        events = [event for event, _ in self._journal()]
        self.assertIn("conflict", events)
        # The conflict is journalled with a reason.
        conflicts = [row for row in self._journal() if row[0] == "conflict"]
        self.assertEqual(conflicts[0][1], "PROPOSAL_EVALUATION_CONFLICT")
        self.assertIsNotNone(first["proposal_id"])

    def test_continuous_job_many_evaluations(self):
        store = self._store()
        created = [
            store.submit(
                self._submission(
                    evaluation_id=f"eval-{index}",
                    evaluation_kind="scheduled_occurrence",
                    job_id="job-1",
                )
            )
            for index in range(3)
        ]
        self.assertEqual(len({item["proposal_id"] for item in created}), 3)
        with self.factory() as session:
            count = session.execute(text("SELECT COUNT(*) FROM strategy_proposals")).scalar()
        self.assertEqual(count, 3)
        # Nothing in the contract limits evaluations per job — that is what makes
        # a continuous intraday strategy possible.
        self.assertTrue(all(item["status"] == "validated" for item in created))

    def test_scheduled_occurrence_requires_a_job(self):
        from backend.strategies.proposals import ProposalStoreError

        with self.assertRaises(ProposalStoreError):
            self._store().submit(
                self._submission(evaluation_id="eval-nojob", evaluation_kind="scheduled_occurrence")
            )

    def test_validation_refusal_ends_evaluation(self):
        from backend.strategies.proposals import ProposalConflict

        store = self._store()
        refused = store.submit(
            self._submission(
                payload={
                    "instrument_token": 424242,
                    "exchange": "NSE",
                    "tradingsymbol": "MISSING",
                    "product": "CNC",
                    "target_quantity": 5,
                }
            )
        )
        self.assertEqual(refused["status"], "refused")
        self.assertIsNone(refused["plan"])
        refusals = [row for row in self._journal() if row[0] == "validation_refused"]
        self.assertEqual(refusals, [("validation_refused", "INSTRUMENT_UNRESOLVED")])

        # The identity is spent: a corrected payload under the SAME evaluation_id
        # conflicts, because the evaluation already ended.
        with self.assertRaises(ProposalConflict):
            store.submit(self._submission())
        # A new evaluation_id is the only way forward — the platform never invents one.
        retried = store.submit(self._submission(evaluation_id="eval-2"))
        self.assertEqual(retried["status"], "validated")

    def test_plan_creation_sets_validated_and_journals(self):
        store = self._store()
        result = store.submit(self._submission())
        self.assertEqual(result["status"], "validated")
        plan = result["plan"]
        self.assertEqual(plan["plan_kind"], "single_instrument")
        self.assertEqual(len(plan["plan_hash"]), 64)
        self.assertEqual(plan["pinned_catalog_generation"], G2)
        self.assertEqual(plan["resolved_plan"]["legs"][0]["instrument_id"], "inst-REL")
        self.assertEqual(
            [event for event, _ in self._journal()], ["received", "plan_created"]
        )

        # Exactly one plan per proposal: the UNIQUE constraint refuses a second.
        with self.assertRaises(Exception):
            with self.factory() as session:
                session.execute(
                    text(
                        "INSERT INTO strategy_plans (plan_id, proposal_id, strategy_id, account_id, "
                        " plan_kind, plan_hash, logical_plan, resolved_plan, pinned_catalog_generation) "
                        "VALUES ('dup', :pid, 'stg-A', 'kite:A', 'single_instrument', 'h', '{}', '{}', :gen)"
                    ),
                    {"pid": plan["proposal_id"], "gen": G2},
                )
                session.commit()

    def test_unpublished_generation_refusal(self):
        self.seed_generation(G_STAGING, "staging", None)
        result = self._store().submit(
            self._submission(
                payload={
                    "instrument_token": 100,
                    "exchange": "NSE",
                    "tradingsymbol": "RELIANCE",
                    "product": "CNC",
                    "target_quantity": 10,
                    "catalog_generation": G_STAGING,
                }
            )
        )
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(result["plan"])
        refusals = [row for row in self._journal() if row[0] == "validation_refused"]
        self.assertEqual(refusals, [("validation_refused", "CATALOG_GENERATION_NOT_PUBLISHED")])

    def test_generation_defaults_to_current_published(self):
        store = self._store()
        result = store.submit(self._submission())
        # No catalog_generation in the payload: validation pins what is published now
        # and RECORDS it, so the plan is never resolved against a moving target.
        self.assertEqual(result["plan"]["pinned_catalog_generation"], G2)
        self.assertEqual(result["plan"]["resolved_plan"]["catalog_generation"], G2)

    def test_store_transaction_atomic(self):
        from backend.strategies.compiler.base import ValidationRefusal
        from backend.strategies.proposals import ProposalStore

        class _Exploding:
            def compile(self, payload, pinned):
                raise ValidationRefusal("PAYLOAD_INVALID", {"reason": "boom"})

        store = ProposalStore(session_factory=self.factory, compiler=_Exploding())
        result = store.submit(self._submission())
        self.assertEqual(result["status"], "refused")
        with self.factory() as session:
            plans = session.execute(text("SELECT COUNT(*) FROM strategy_plans")).scalar()
            status = session.execute(text("SELECT status FROM strategy_proposals")).scalar()
        # A failed compile leaves the refusal and NO plan.
        self.assertEqual((plans, status), (0, "refused"))
        self.assertEqual(
            [event for event, _ in self._journal()], ["received", "validation_refused"]
        )


class TargetWeightsPlanTests(ProposalTestCase):
    """D-6 end to end: a weights plan persists its scope and stays valid."""

    REV = "eeeeeeee-0000-0000-0000-0000000000bb"

    def setUp(self):
        super().setUp()
        self.seed_generation(G1, "published", T1)
        self.seed_generation(G2, "published", T2)
        for index, member in enumerate(("RELIANCE", "INFY", "TCS")):
            self.seed_record(f"inst-{member}", symbol=member, generation=G1)
            self.seed_mapping(f"map-{member}", f"inst-{member}", token=100 + index,
                              symbol=member, valid_from=G1, is_current=1)
        self.seed_universe_revision(self.REV, ["RELIANCE", "INFY", "TCS"])
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-W', 'app:o', 'Weights', 'kite:A', 'active')"
                )
            )
            session.commit()

    def _submit(self, generation=G1):
        from backend.strategies.proposals import ProposalStore, ProposalSubmission

        payload = {
            "universe_revision_id": self.REV,
            "target_weights": {"RELIANCE": 0.5, "INFY": 0.25},
        }
        if generation is not None:
            payload["catalog_generation"] = generation
        return ProposalStore(session_factory=self.factory).submit(
            ProposalSubmission(
                strategy_id="stg-W", account_id="kite:A", evaluation_id="eval-w",
                evaluation_kind="run_now", strategy_run_id="run-1",
                target_kind="target_weights", payload=payload,
            )
        )

    def test_weights_plan_persists_its_scope(self):
        result = self._submit()
        self.assertEqual(result["status"], "validated", result)
        plan = result["plan"]
        # The DB CHECK requires both scope columns for this kind, so a validated
        # plan proves they were written.
        self.assertEqual(plan["pinned_universe_revision_id"], self.REV)
        self.assertEqual(plan["pinned_member_hash"], member_hash(["RELIANCE", "INFY", "TCS"]))
        self.assertEqual(plan["pinned_catalog_generation"], G1)
        # Every member, with the omission an explicit zero.
        weights = {leg["tradingsymbol"]: leg["target_weight"] for leg in plan["resolved_plan"]["legs"]}
        self.assertEqual(weights, {"RELIANCE": 0.5, "INFY": 0.25, "TCS": 0.0})

    def test_weights_plan_records_the_coordinate_invalidation_needs(self):
        from backend.strategies.proposals import plan_invalidation_state

        plan = self._submit()["plan"]
        leg = plan["resolved_plan"]["legs"][0]
        # Invalidation resolves each leg's broker coordinate; a leg without its
        # exchange cannot be compared at all.
        self.assertTrue(leg.get("broker_exchange"), leg)

        # An unrelated newer generation must leave a weights plan VALID.
        self.seed_generation(G3, "published", T3)
        self.seed_record("inst-OTHER", symbol="WIPRO", generation=G3)
        self.seed_mapping("map-OTHER", "inst-OTHER", token=900, symbol="WIPRO", valid_from=G3,
                          is_current=1)
        state = plan_invalidation_state(plan, session_factory=self.factory)
        self.assertEqual(state["state"], "valid", state)
        self.assertEqual(state["reason"], "CATALOG_GENERATION_UNRELATED")


class MisOvernightProposalTests(ProposalStoreTests):
    """A MIS overnight refusal is a validation refusal, so it ends the evaluation."""

    def _mis_submission(self, **overrides):
        values = {
            "strategy_id": "stg-A",
            "account_id": "kite:A",
            "evaluation_id": "eval-mis",
            "evaluation_kind": "run_now",
            "job_id": None,
            "strategy_run_id": "run-1",
            "target_kind": "single_instrument",
            "payload": {
                "instrument_token": 100,
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE",
                "product": "MIS",
                "target_quantity": 10,
                "reference_price": 100.0,
                "hold_days": 3,
            },
        }
        values.update(overrides)
        from backend.strategies.proposals import ProposalSubmission

        return ProposalSubmission(**values)

    def test_a_multi_day_mis_proposal_is_refused_and_spends_the_evaluation(self):
        from backend.strategies.proposals import ProposalConflict

        store = self._store()
        result = store.submit(self._mis_submission())
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(result["plan"])
        refusals = [row for row in self._journal() if row[0] == "validation_refused"]
        self.assertEqual(refusals, [("validation_refused", "MIS_OVERNIGHT_REFUSED")])

        # The identity is spent: a corrected retry needs a NEW evaluation_id,
        # because the platform never invents one and never retries a refusal.
        with self.assertRaises(ProposalConflict):
            store.submit(self._mis_submission(payload={"instrument_token": 100,
                "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC",
                "target_quantity": 10, "reference_price": 100.0, "hold_days": 3}))
        corrected = store.submit(
            self._mis_submission(
                evaluation_id="eval-mis-2",
                payload={"instrument_token": 100, "exchange": "NSE",
                         "tradingsymbol": "RELIANCE", "product": "CNC",
                         "target_quantity": 10, "reference_price": 100.0, "hold_days": 3},
            )
        )
        self.assertEqual(corrected["status"], "validated")


if __name__ == "__main__":
    unittest.main()
