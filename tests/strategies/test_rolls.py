"""The roll state machine: full-required-fill gating (D-2, R3 §13 locked decision 6).

The invariant is an ORDER, and the tests below pin it as one: acquire, prove the
FULL required replacement filled, and only then release the old contract's close
step. Everything else in this file exists to prove the two ways that could be
faked — reading a fill from the wrong place, or releasing on a partial — are
structurally impossible rather than merely discouraged.
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401
import backend.strategies.attribution_models  # noqa: F401

OLD = "a0000000-0000-0000-0000-00000000000a"
NEW = "b0000000-0000-0000-0000-00000000000b"


class RollTestCase(unittest.TestCase):
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
            dbapi_connection.commit()

        _Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.commit()
        self.notified: list = []
        self.machine = self._machine()

    def tearDown(self):
        self.engine.dispose()

    def _machine(self, *, notifier=None):
        from backend.strategies.rolls import RollStateMachine

        def default_notifier(account_id, roll):
            self.notified.append((account_id, roll["roll_id"]))
            return True

        return RollStateMachine(
            session_factory=self.factory, notifier=notifier or default_notifier
        )

    def book(self, instrument_id, quantity):
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_position_projection "
                    "(account_id, strategy_id, execution_environment, identity_kind, identity_key, "
                    " canonical_instrument_id, product, instrument_token, exchange, tradingsymbol, "
                    " net_quantity, projection_version) "
                    "VALUES ('kite:A', 'stg-A', 'paper', 'canonical', :key, :iid, 'NRML', 1, "
                    " 'NFO', 'CONTRACT', :qty, 1)"
                ),
                {"key": f"{instrument_id}-{quantity}", "iid": instrument_id,
                 "qty": int(quantity)},
            )
            session.commit()

    def roll(self, *, required=50, product="NRML", side="BUY"):
        return self.machine.create(
            strategy_id="stg-A",
            account_id="kite:A",
            old_instrument_id=OLD,
            new_instrument_id=NEW,
            required_replacement_quantity=required,
            old_coordinate={"product": product, "side": "SELL"},
            new_coordinate={"product": product, "side": side},
        )


class LifecycleTests(RollTestCase):
    def test_a_new_roll_retains_both_identities(self):
        roll = self.roll()
        self.assertEqual(roll["state"], "acquiring")
        self.assertEqual(roll["old_instrument_id"], OLD)
        self.assertEqual(roll["new_instrument_id"], NEW)
        self.assertEqual(roll["required_replacement_quantity"], 50)
        self.assertEqual(roll["proven_filled_quantity"], 0)
        self.assertEqual([row["event"] for row in self.machine.events(roll["roll_id"])], ["created"])

    def test_the_happy_path_in_order(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]

        self.assertEqual(self.machine.acquire(rid)["state"], "proving_filled")
        # The replacement is fully filled on the NEW contract: the fill is
        # RECORDED against the roll (the durable proof), not inferred from a book.
        self.machine.record_replacement_fill(
            rid, paper_order_id="PAPER-1", quantity=50, instrument_id=NEW
        )
        self.assertEqual(self.machine.prove_filled(rid)["state"], "releasing_old")
        self.assertEqual(self.machine.release_close(rid)["state"], "releasing_old")
        # The old book is flat.
        self.assertEqual(self.machine.mark_old_flat(rid)["state"], "completed")
        self.assertEqual(
            [row["event"] for row in self.machine.events(rid)],
            [
                "created",
                "acquired",
                "replacement_filled",
                "fill_proven",
                "close_released",
                "old_flat",
                "completed",
            ],
        )

    def test_the_state_order_is_the_invariant(self):
        from backend.strategies.rolls import ReleaseRefused, RollStateError

        roll = self.roll()
        rid = roll["roll_id"]
        # release_close before acquiring is refused by the INVARIANT's own error,
        # not a generic state error: the close step is gated on the fill, always.
        with self.assertRaises(ReleaseRefused):
            self.machine.release_close(rid)
        self.machine.acquire(rid)
        with self.assertRaises(RollStateError):
            self.machine.mark_old_flat(rid)


class FullFillGateTests(RollTestCase):
    """THE invariant: the close step is unreachable before the FULL fill."""

    def test_a_partial_replacement_stalls_with_old_attribution_intact(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)

        # 30 of 50 filled: not enough.
        self.machine.record_replacement_fill(
            rid, paper_order_id="PAPER-1", quantity=30, instrument_id=NEW
        )
        stalled = self.machine.prove_filled(rid)
        self.assertEqual(stalled["state"], "action_required")
        self.assertEqual(stalled["action_reason"], "replacement_incomplete")
        self.assertEqual(stalled["proven_filled_quantity"], 30)
        # The old contract's identity is still on the roll, unchanged.
        self.assertEqual(stalled["old_instrument_id"], OLD)
        self.assertEqual([row["event"] for row in self.machine.events(rid)][-1], "stalled")

    def test_the_close_step_is_unreachable_while_the_replacement_is_partial(self):
        from backend.strategies.rolls import ReleaseRefused

        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=49)

        with self.assertRaises(ReleaseRefused) as ctx:
            self.machine.release_close(rid)
        self.assertEqual(ctx.exception.reason_code, "ROLL_FILL_NOT_PROVEN")
        self.assertEqual(ctx.exception.detail["required_replacement_quantity"], 50)
        self.assertEqual(ctx.exception.detail["proven_filled_quantity"], 49)

    def test_a_stalled_roll_never_auto_reverses(self):
        """A stall records the shortfall; it does not undo anything."""
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=10)
        self.machine.prove_filled(rid, proven_quantity=20)

        final = self.machine.get(rid)
        self.assertEqual(final["state"], "action_required")
        # Progress is recorded, never rewound: 20 is more than 10, and both facts
        # are in the trail.
        self.assertEqual(final["proven_filled_quantity"], 20)
        events = [row["event"] for row in self.machine.events(rid)]
        self.assertEqual(events.count("stalled"), 2)
        # And nothing in the roll's own record says the old position was reduced.
        self.assertEqual(final["old_instrument_id"], OLD)

    def test_a_stalled_roll_can_still_complete_once_full(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=10)
        self.assertEqual(self.machine.get(rid)["state"], "action_required")
        # The replacement finally completes.
        self.machine.prove_filled(rid, proven_quantity=50)
        self.assertEqual(self.machine.get(rid)["state"], "releasing_old")

    def test_proof_is_the_roll_s_own_recorded_replacement_executions(self):
        """Recorded executions prove the roll; a book or a claim does not.

        The attributed book mixes holdings that predate the roll and holdings that
        belong to other decisions, so it cannot prove THIS replacement - and the
        HTTP surface cannot pass a quantity at all. The roll's own durable record
        of confirmed replacement fills is the proof.
        """
        from backend.strategies.rolls import RollStateError

        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        # Nothing recorded: the roll is unproven.
        self.assertEqual(self.machine.prove_filled(rid)["state"], "action_required")

        # A pre-existing holding on the NEW contract does not prove the roll...
        self.book(NEW, 50)
        self.assertEqual(self.machine.prove_filled(rid)["state"], "action_required")
        # ...and neither does a holding on another contract.
        self.book(OLD, 50)
        self.assertEqual(self.machine.prove_filled(rid)["state"], "action_required")

        # An execution on the WRONG contract is refused outright.
        with self.assertRaises(RollStateError):
            self.machine.record_replacement_fill(
                rid, paper_order_id="PAPER-WRONG", quantity=50, instrument_id=OLD
            )

        # Only recorded executions on the replacement contract prove it, and a
        # replayed paper order is recorded once (idempotent).
        self.machine.record_replacement_fill(
            rid, paper_order_id="PAPER-1", quantity=30, instrument_id=NEW
        )
        self.machine.record_replacement_fill(
            rid, paper_order_id="PAPER-1", quantity=30, instrument_id=NEW
        )
        self.assertEqual(self.machine.replacement_filled_quantity(rid), 30)
        self.assertEqual(self.machine.prove_filled(rid)["state"], "action_required")
        self.machine.record_replacement_fill(
            rid, paper_order_id="PAPER-2", quantity=20, instrument_id=NEW
        )
        self.assertEqual(self.machine.prove_filled(rid)["state"], "releasing_old")


class OldFlatTests(RollTestCase):
    def test_the_roll_cannot_complete_on_an_assumed_flat(self):
        from backend.strategies.rolls import RollNotFlat

        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=50)
        self.machine.release_close(rid)

        # The old contract is still held: attribution is RETAINED, not assumed away.
        self.book(OLD, 25)
        with self.assertRaises(RollNotFlat) as ctx:
            self.machine.mark_old_flat(rid)
        self.assertEqual(ctx.exception.reason_code, "ROLL_OLD_NOT_FLAT")
        self.assertEqual(ctx.exception.detail["old_attributed_quantity"], 25)
        self.assertEqual(self.machine.get(rid)["state"], "releasing_old")

    def test_both_identities_survive_to_completion(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=50)
        self.machine.release_close(rid)
        completed = self.machine.mark_old_flat(rid)
        self.assertEqual(completed["state"], "completed")
        # The whole point of retaining both: the completed roll can still say what
        # it transitioned from.
        self.assertEqual(completed["old_instrument_id"], OLD)
        self.assertEqual(completed["new_instrument_id"], NEW)


class UniquenessTests(RollTestCase):
    def test_one_open_roll_per_old_contract(self):
        from backend.strategies.rolls import RollDuplicate

        self.roll()
        with self.assertRaises(RollDuplicate) as ctx:
            self.roll()
        self.assertEqual(ctx.exception.reason_code, "ROLL_ALREADY_OPEN")

    def test_a_completed_roll_does_not_block_a_later_one(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=50)
        self.machine.release_close(rid)
        self.machine.mark_old_flat(rid)
        # The next roll may now open on the same contract.
        self.assertIsNotNone(self.roll())

    def test_a_different_old_contract_is_a_different_roll(self):
        self.roll()
        other = self.machine.create(
            strategy_id="stg-A", account_id="kite:A",
            old_instrument_id="c0000000-0000-0000-0000-00000000000c",
            new_instrument_id=NEW, required_replacement_quantity=10,
        )
        self.assertIsNotNone(other)

    def test_a_stalled_roll_still_blocks_a_duplicate(self):
        from backend.strategies.rolls import RollDuplicate

        roll = self.roll()
        self.machine.acquire(roll["roll_id"])
        self.machine.prove_filled(roll["roll_id"], proven_quantity=10)
        # action_required is still an OPEN roll: a second one would fight the first.
        with self.assertRaises(RollDuplicate):
            self.roll()


class BasketFlagIrrelevanceTests(RollTestCase):
    """The gate is NOT the basket all_or_none flag, however similar the names sound."""

    @staticmethod
    def _identifiers(module_path: str) -> set:
        """Every identifier the module's CODE uses, ignoring prose.

        A docstring that names the flag it refuses to use is not a use of it, so
        the check reads the AST rather than the text.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(module_path).read_text())
        found: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                found.add(node.arg)
        return found

    def test_the_roll_machine_never_references_the_basket_flag(self):
        identifiers = self._identifiers("backend/strategies/rolls.py")
        for forbidden in ("all_or_none", "all_or_none_atomic", "_AtomicBasketRejected"):
            self.assertNotIn(
                forbidden, identifiers,
                f"roll semantics must not be expressed through the basket {forbidden} flag",
            )

    def test_a_partial_fill_stalls_whether_or_not_a_basket_flag_is_set(self):
        """Toggling the basket flag changes nothing about roll behaviour."""
        outcomes = []
        for flagged in (True, False):
            with self.factory() as session:
                session.execute(text("DELETE FROM strategy_roll_events"))
                session.execute(text("DELETE FROM strategy_rolls"))
                session.commit()
            roll = self.machine.create(
                strategy_id="stg-A", account_id="kite:A",
                old_instrument_id=OLD, new_instrument_id=NEW,
                required_replacement_quantity=50,
                new_coordinate={"product": "NRML", "side": "BUY"},
                # The flag a basket would carry, which the roll ignores entirely.
                peak_margin_evidence={"all_or_none": flagged},
            )
            self.machine.acquire(roll["roll_id"])
            outcomes.append(self.machine.prove_filled(roll["roll_id"], proven_quantity=20)["state"])
        self.assertEqual(outcomes, ["action_required", "action_required"])

    def test_no_proportional_release_exists(self):
        """Proportional release is a future opt-in and appears nowhere."""
        identifiers = {name.lower() for name in self._identifiers("backend/strategies/rolls.py")}
        for forbidden in ("proportional", "pro_rata", "partial_release", "prorate"):
            self.assertNotIn(forbidden, identifiers)


class AppendOnlyTests(RollTestCase):
    def test_the_event_trail_is_strategy_scoped_and_ordered(self):
        roll = self.roll(required=50)
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=50)
        self.machine.release_close(rid)
        events = [row["event"] for row in self.machine.events(rid)]
        # The trail IS the order: a reader can reconstruct the transition from it.
        self.assertEqual(events, ["created", "acquired", "fill_proven", "close_released"])


class EscalationTests(RollTestCase):
    def test_escalation_notifies_once_and_leaves_the_state_alone(self):
        roll = self.roll()
        rid = roll["roll_id"]
        self.machine.acquire(rid)
        self.machine.prove_filled(rid, proven_quantity=10)

        escalated = self.machine.escalate(rid, reason="replacement_incomplete")
        self.assertTrue(escalated["escalated"])
        self.assertEqual(escalated["state"], "action_required")
        self.assertEqual(self.notified, [("kite:A", rid)])

    def test_a_failed_notification_does_not_break_the_roll(self):
        machine = self._machine(notifier=lambda *_: False)
        roll = self.roll()
        escalated = machine.escalate(roll["roll_id"], reason="x")
        self.assertFalse(escalated["escalated"])
        # The roll is untouched: escalation reports, it does not decide.
        self.assertEqual(escalated["state"], "acquiring")


if __name__ == "__main__":
    unittest.main()
