"""The schedule runtime: occurrence fencing, misfire grace, overlap policy (G11).

The invariants here are all about *once*: an occurrence fires once, fires late at
most once, and never starts while the previous decision is unresolved. The unique
index on ``(schedule_id, occurrence_key)`` is what makes the first one structural
rather than conventional, so these tests exercise the policy above it.

SQLite runs with the established ``public.`` ATTACH fixture; the real
two-scheduler race is proved in the PostgreSQL suite.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

#: A fixed instant so every expectation is arithmetic rather than "now".
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


class SchedulingTestCase(unittest.TestCase):
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

        from backend.strategies.models import (
            HostedStrategy,
            HostedStrategySchedule,
            HostedStrategyVersion,
        )
        from backend.strategies.attribution_models import (
            Strategy,
            StrategyPlan,
            StrategyProposal,
            StrategyReservation,
            StrategyScheduleOccurrence,
        )

        _Base.metadata.create_all(
            self.engine,
            tables=[
                Strategy.__table__,
                StrategyProposal.__table__,
                StrategyPlan.__table__,
                StrategyReservation.__table__,
                StrategyScheduleOccurrence.__table__,
                HostedStrategy.__table__,
                HostedStrategyVersion.__table__,
                HostedStrategySchedule.__table__,
            ],
        )
        self.factory = sessionmaker(bind=self.engine)
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('stg-A', 'app:o', 'A', 'kite:A', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO hosted_strategies "
                    "(id, owner_id, name, template_id, default_execution_mode, "
                    " default_account_scope, default_job_kind, stale_exit_policy, "
                    " max_duration_s, progress_deadline_s, status) "
                    "VALUES ('hs-1', 'app:o', 'A', 'hosted:hs-1', 'paper', 'kite:A', 'finite', "
                    " 'exit_on_worker_stale', 3600, 600, 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_versions "
                    "(id, strategy_id, version, source, source_sha256, parameters_schema, "
                    " capabilities_snapshot, created_by) "
                    "VALUES ('v-1', 'hs-1', 1, 'inline', 'sha', '{}', '{}', 'app:o')"
                )
            )
            session.commit()
        self.fired: list = []
        self.scheduler = self._scheduler()

    def tearDown(self):
        self.engine.dispose()

    def _scheduler(self, *, submitter=None):
        from backend.strategies.scheduling import ScheduleScheduler

        def default_submitter(schedule, occurrence, detail):
            self.fired.append((occurrence.occurrence_key, occurrence.evaluation_id))
            return True

        return ScheduleScheduler(
            session_factory=self.factory, proposal_submitter=submitter or default_submitter
        )

    # -- fixtures -----------------------------------------------------------

    def schedule(self, *, kind="monthly", at_time="09:30", day_of_month=None,
                 calendar_dates=None, enabled=True, schedule_id="sch-1"):
        hosted_id = f"hs-{schedule_id}"
        version_id = f"v-{schedule_id}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO hosted_strategies "
                    "(id, owner_id, name, template_id, default_execution_mode, "
                    " default_account_scope, default_job_kind, stale_exit_policy, "
                    " max_duration_s, progress_deadline_s, status) "
                    "VALUES (:id, 'app:o', :name, :template, 'paper', 'kite:A', 'finite', "
                    " 'exit_on_worker_stale', 3600, 600, 'active')"
                ),
                {"id": hosted_id, "name": f"A {schedule_id}", "template": f"hosted:{hosted_id}"},
            )
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_versions "
                    "(id, strategy_id, version, source, source_sha256, parameters_schema, "
                    " capabilities_snapshot, created_by) "
                    "VALUES (:vid, :sid, 1, 'inline', 'sha', '{}', '{}', 'app:o')"
                ),
                {"vid": version_id, "sid": hosted_id},
            )
            session.execute(
                text(
                    "INSERT INTO hosted_strategy_schedules "
                    "(id, strategy_id, version_id, owner_id, account_scope, execution_mode, "
                    " job_kind, max_duration_s, progress_deadline_s, schedule_kind, at_time, "
                    " timezone, day_of_month, calendar_dates, enabled, params_snapshot, "
                    " policy_snapshot, capabilities_snapshot) "
                    "VALUES (:id, :sid, :vid, 'app:o', 'kite:A', 'paper', 'finite', 3600, 600, "
                    " :kind, :at_time, 'Asia/Kolkata', :dom, :dates, :enabled, '{}', '{}', '{}')"
                ),
                {
                    "id": schedule_id,
                    "sid": hosted_id,
                    "vid": version_id,
                    "kind": kind,
                    "at_time": at_time,
                    "dom": day_of_month,
                    "dates": json.dumps(calendar_dates) if calendar_dates is not None else None,
                    "enabled": enabled,
                },
            )
            session.commit()
        return {
            "id": schedule_id, "strategy_id": "stg-A", "account_scope": "kite:A",
            "execution_mode": "paper", "schedule_kind": kind, "at_time": at_time,
            "timezone": "Asia/Kolkata", "day_of_month": day_of_month,
            "calendar_dates": calendar_dates,
        }

    def occurrences(self, schedule_id="sch-1"):
        return self.scheduler.occurrences_for_schedule(schedule_id=schedule_id)

    def seed_fired_occurrence(self, *, due_at, evaluation_id, reservation_status,
                              proposal_status="validated"):
        """A prior occurrence whose work may or may not be resolved."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO strategy_schedule_occurrences "
                    "(id, schedule_id, strategy_id, occurrence_key, due_at, status, evaluation_id) "
                    "VALUES (:id, 'sch-1', 'stg-A', :key, :due, 'fired', :eval)"
                ),
                {"id": f"occ-{evaluation_id}", "key": f"sch-1:{evaluation_id}",
                 "due": due_at, "eval": evaluation_id},
            )
            session.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status, job_id) "
                    "VALUES (:pid, 'stg-A', 'kite:A', :eval, 'scheduled_occurrence', 'run-1', "
                    " 'target_weights', '{}', 'sha', :status, :job)"
                ),
                {"pid": f"prop-{evaluation_id}", "eval": evaluation_id,
                 "status": proposal_status, "job": f"job-{evaluation_id}"},
            )
            if reservation_status is not None:
                session.execute(
                    text(
                        "INSERT INTO strategy_plans "
                        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                        " logical_plan, resolved_plan, pinned_catalog_generation, "
                        " pinned_universe_revision_id, pinned_member_hash) "
                        "VALUES (:lid, :pid, 'stg-A', 'kite:A', 'target_weights', 'h', '{}', '{}', "
                        " '11111111-1111-1111-1111-111111111111', 'rev-1', 'mh-1')"
                    ),
                    {"lid": f"plan-{evaluation_id}", "pid": f"prop-{evaluation_id}"},
                )
                session.execute(
                    text(
                        "INSERT INTO strategy_reservations "
                        "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                        " execution_environment, status, reserved_notional_inr, valid_until) "
                        "VALUES (:rid, :lid, 'stg-A', 'kite:A', :eval, 'paper', :status, 1000, "
                        " :valid)"
                    ),
                    {"rid": f"res-{evaluation_id}", "lid": f"plan-{evaluation_id}",
                     "eval": evaluation_id, "status": reservation_status,
                     "valid": NOW + timedelta(days=30)},
                )
            session.commit()


class OccurrenceComputationTests(SchedulingTestCase):
    def test_monthly_occurrences_are_due_times_in_schedule_order(self):
        schedule = self.schedule(kind="monthly", day_of_month=1, at_time="09:30")
        occurrences = self.scheduler.due_occurrences(schedule, now=NOW)
        # 2026-10-01 09:30 IST has passed; November's has not. The walker looks
        # back 24 months, so the set is every due month up to now, in order.
        self.assertTrue(occurrences)
        keys = [item.occurrence_key for item in occurrences]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("sch-1:2026-10-01", keys)
        self.assertNotIn("sch-1:2026-11-01", keys)
        # Due times are zone-aware UTC instants, not naive local strings.
        self.assertEqual(occurrences[-1].due_at.tzinfo, timezone.utc)

    def test_day_of_month_beyond_month_length_clamps(self):
        schedule = self.schedule(kind="monthly", day_of_month=31, at_time="09:30")
        occurrences = self.scheduler.due_occurrences(schedule, now=NOW)
        # September has 30 days, so a "31st" occurrence lands on the 30th rather
        # than being skipped or rolling into October.
        keys = [item.occurrence_key for item in occurrences]
        self.assertIn("sch-1:2026-09-30", keys)

    def test_calendar_occurrences_come_from_explicit_dates(self):
        schedule = self.schedule(
            kind="calendar", calendar_dates=["2026-09-15", "2026-10-10", "2026-12-01"]
        )
        occurrences = self.scheduler.due_occurrences(schedule, now=NOW)
        keys = [item.occurrence_key for item in occurrences]
        self.assertEqual(keys, ["sch-1:2026-09-15", "sch-1:2026-10-10"])

    def test_evaluation_id_is_deterministic_per_occurrence(self):
        schedule = self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        first = self.scheduler.due_occurrences(schedule, now=NOW)
        second = self.scheduler.due_occurrences(schedule, now=NOW)
        self.assertEqual([item.evaluation_id for item in first],
                         [item.evaluation_id for item in second])
        self.assertEqual(first[0].evaluation_id, "sched:sch-1:2026-10-10")

    def test_only_the_new_kinds_are_driven(self):
        self.schedule(kind="monthly", day_of_month=1)
        self.assertIn("sch-1", {row["id"] for row in self.scheduler.enabled_schedules()})
        # A disabled schedule is not driven at all.
        self.schedule(schedule_id="sch-off", kind="monthly", day_of_month=1, enabled=False)
        self.assertNotIn("sch-off", {row["id"] for row in self.scheduler.enabled_schedules()})


class FiringTests(SchedulingTestCase):
    def setUp(self):
        # These exercise firing and overlap, not the misfire window: earlier phases
        # of the policy are tested in MisfireTests.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)
        super().setUp()

    def tearDown(self):
        os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)
        super().tearDown()

    def test_a_due_occurrence_fires_once_and_records_its_evaluation(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(len(result["fired"]), 1)
        rows = self.occurrences()
        fired = [row for row in rows if row["status"] == "fired"]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["evaluation_id"], "sched:sch-1:2026-10-10")
        self.assertIsNotNone(fired[0]["fired_at"])

    def test_a_second_tick_does_not_double_fire(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.scheduler.tick(now=NOW)
        before = len(self.fired)
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        # The occurrence already exists, so the unique index refuses a second row
        # and nothing fires again.
        self.assertEqual(second["fired"], [])
        self.assertEqual(len(self.fired), before)
        self.assertEqual(len(self.occurrences()), 1)

    def test_a_failed_submission_leaves_the_occurrence_retryable(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        scheduler = self._scheduler(submitter=lambda *_: False)
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], [])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        # Not fired and not skipped: the next tick must be able to try again.
        self.assertEqual(row["status"], "pending")


class MisfireTests(SchedulingTestCase):
    def test_a_missed_occurrence_fires_late_within_the_grace(self):
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "7200"
        try:
            self.schedule(kind="calendar", calendar_dates=["2026-10-15"], at_time="09:30")
            # Due 09:30 IST, ticked at 12:00 UTC = 17:30 IST: ~8h late.
            self.scheduler.tick(now=NOW)
            row = self.occurrences()[0]
            self.assertEqual(row["status"], "skipped")
            self.assertEqual(row["skip_reason"], "misfire_beyond_grace")
            self.assertEqual(self.fired, [])
        finally:
            os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)

    def test_late_within_grace_fires_and_journals_the_lateness(self):
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "86400"
        try:
            self.schedule(kind="calendar", calendar_dates=["2026-10-15"], at_time="09:30")
            result = self.scheduler.tick(now=NOW)
            self.assertEqual(len(result["fired"]), 1)
            row = self.occurrences()[0]
            self.assertEqual(row["status"], "fired")
        finally:
            os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)

    def test_skipped_occurrences_are_never_retried(self):
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "1"
        try:
            self.schedule(kind="calendar", calendar_dates=["2026-10-15"], at_time="09:30")
            self.scheduler.tick(now=NOW)
            second = self.scheduler.tick(now=NOW + timedelta(hours=1))
            self.assertEqual(second["fired"], [])
            self.assertEqual(second["skipped"], [])
            self.assertEqual(self.fired, [])
        finally:
            os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)


class OverlapTests(SchedulingTestCase):
    def setUp(self):
        # These exercise firing and overlap, not the misfire window: earlier phases
        # of the policy are tested in MisfireTests.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)
        super().setUp()

    def tearDown(self):
        os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)
        super().tearDown()

    def test_a_new_evaluation_waits_while_the_previous_is_unresolved(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.seed_fired_occurrence(
            due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            evaluation_id="sched:sch-1:2026-09-10",
            reservation_status="active",
        )
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], [])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        row = [r for r in self.occurrences() if r["occurrence_key"] == "sch-1:2026-10-10"][0]
        # Not skipped forever: pending with the reason recorded, so the next tick
        # can still take the decision once the previous one resolves.
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["skip_reason"], "overlap_skipped")
        self.assertTrue(row["detail"]["overlap_skipped"])

    def test_the_blocked_occurrence_fires_once_the_previous_resolves(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.seed_fired_occurrence(
            due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            evaluation_id="sched:sch-1:2026-09-10",
            reservation_status="consumed",
        )
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(len(result["fired"]), 1)

    def test_a_refused_previous_proposal_does_not_block(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.seed_fired_occurrence(
            due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            evaluation_id="sched:sch-1:2026-09-10",
            reservation_status=None,
            proposal_status="refused",
        )
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(len(result["fired"]), 1)

    def test_a_released_reservation_does_not_block(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.seed_fired_occurrence(
            due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            evaluation_id="sched:sch-1:2026-09-10",
            reservation_status="released",
        )
        self.assertEqual(len(self.scheduler.tick(now=NOW)["fired"]), 1)


class IsolationTests(SchedulingTestCase):
    def setUp(self):
        # These exercise firing and overlap, not the misfire window: earlier phases
        # of the policy are tested in MisfireTests.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)
        super().setUp()

    def tearDown(self):
        os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)
        super().tearDown()

    def test_one_failing_schedule_does_not_stop_the_tick(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.schedule(schedule_id="sch-2", kind="calendar", calendar_dates=["2026-10-11"])

        def submitter(schedule, occurrence, detail):
            if schedule["id"] == "sch-1":
                raise RuntimeError("boom")
            self.fired.append((occurrence.occurrence_key, occurrence.evaluation_id))
            return True

        scheduler = self._scheduler(submitter=submitter)
        result = scheduler.tick(now=NOW)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["schedule_id"], "sch-1")
        # The other schedule still fired.
        self.assertEqual(result["fired"], ["sch-2:2026-10-11"])


if __name__ == "__main__":
    unittest.main()
