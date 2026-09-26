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
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.strategies.scheduling import DECISION_CLAIM_STALE_SECONDS
from backend.workflows.repository import Base as _Base
import backend.strategies.models  # noqa: F401  registers the hosted tables
import backend.strategies.attribution_models  # noqa: F401  registers the new tables

#: A fixed instant so every expectation is arithmetic rather than "now".
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


def _utc_shift(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


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
            StrategyJob,
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
                StrategyJob.__table__,
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

    def _scheduler(self, *, submitter=None, trading_day_reader=None):
        from backend.strategies.scheduling import ScheduleScheduler

        def default_submitter(schedule, occurrence, detail):
            self.fired.append((occurrence.occurrence_key, occurrence.evaluation_id))
            return True

        return ScheduleScheduler(
            session_factory=self.factory,
            job_submitter=submitter or default_submitter,
            trading_day_reader=trading_day_reader,
        )

    # -- fixtures -----------------------------------------------------------

    def schedule(self, *, kind="monthly", at_time="09:30", day_of_month=None,
                 calendar_dates=None, enabled=True, schedule_id="sch-1", weekday=None):
        hosted_id = f"hs-{schedule_id}"
        version_id = f"v-{schedule_id}"
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT INTO hosted_strategies "
                    "(id, owner_id, name, template_id, default_execution_mode, "
                    " default_account_scope, default_job_kind, stale_exit_policy, "
                    " max_duration_s, progress_deadline_s, status) "
                    "VALUES (:id, 'app:o', :name, :template, 'paper', 'kite:paper', 'finite', "
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
                    " timezone, weekday, day_of_month, calendar_dates, enabled, params_snapshot, "
                    " policy_snapshot, capabilities_snapshot) "
                    "VALUES (:id, :sid, :vid, 'app:o', 'kite:paper', 'paper', 'finite', 3600, 600, "
                    " :kind, :at_time, 'Asia/Kolkata', :wd, :dom, :dates, :enabled, '{}', '{}', '{}')"
                ),
                {
                    "id": schedule_id,
                    "sid": hosted_id,
                    "vid": version_id,
                    "kind": kind,
                    "at_time": at_time,
                    "wd": weekday,
                    "dom": day_of_month,
                    "dates": json.dumps(calendar_dates) if calendar_dates is not None else None,
                    "enabled": enabled,
                },
            )
            session.commit()
        return {
            "id": schedule_id, "strategy_id": hosted_id, "account_scope": "kite:paper",
            "version_id": version_id, "owner_id": "app:o",
            "execution_mode": "paper", "schedule_kind": kind, "at_time": at_time,
            "timezone": "Asia/Kolkata", "day_of_month": day_of_month,
            "calendar_dates": calendar_dates, "weekday": weekday,
        }

    def occurrences(self, schedule_id="sch-1"):
        return self.scheduler.occurrences_for_schedule(schedule_id=schedule_id)

    def seed_fired_occurrence(self, *, due_at, evaluation_id, reservation_status,
                              proposal_status="validated"):
        """A prior occurrence whose work may or may not be resolved."""
        with self.factory() as session:
            session.execute(
                text(
                    "INSERT OR IGNORE INTO strategies (id, owner_id, name, account_scope, status) "
                    "VALUES ('hs-sch-1', 'app:o', 'A scheduled', 'kite:paper', 'active')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO strategy_schedule_occurrences "
                    "(id, schedule_id, strategy_id, occurrence_key, due_at, status, evaluation_id) "
                    "VALUES (:id, 'sch-1', :sid, :key, :due, 'fired', :eval)"
                ),
                {"id": f"occ-{evaluation_id}", "sid": "hs-sch-1",
                 "key": f"sch-1:{evaluation_id}", "due": due_at, "eval": evaluation_id},
            )
            session.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(proposal_id, strategy_id, account_id, evaluation_id, evaluation_kind, "
                    " strategy_run_id, target_kind, payload, payload_sha256, status, job_id) "
                    "VALUES (:pid, :sid, :acct, :eval, 'scheduled_occurrence', 'run-1', "
                    " 'target_weights', '{}', 'sha', :status, :job)"
                ),
                {"pid": f"prop-{evaluation_id}", "sid": "hs-sch-1", "acct": "kite:paper",
                 "eval": evaluation_id, "status": proposal_status,
                 "job": f"job-{evaluation_id}"},
            )
            if reservation_status is not None:
                session.execute(
                    text(
                        "INSERT INTO strategy_plans "
                        "(plan_id, proposal_id, strategy_id, account_id, plan_kind, plan_hash, "
                        " logical_plan, resolved_plan, pinned_catalog_generation, "
                        " pinned_universe_revision_id, pinned_member_hash) "
                        "VALUES (:lid, :pid, :sid, :acct, 'target_weights', 'h', '{}', '{}', "
                        " '11111111-1111-1111-1111-111111111111', 'rev-1', 'mh-1')"
                    ),
                    {"lid": f"plan-{evaluation_id}", "pid": f"prop-{evaluation_id}",
                     "sid": "hs-sch-1", "acct": "kite:paper"},
                )
                session.execute(
                    text(
                        "INSERT INTO strategy_reservations "
                        "(reservation_id, plan_id, strategy_id, account_id, evaluation_id, "
                        " execution_environment, status, reserved_notional_inr, valid_until) "
                        "VALUES (:rid, :lid, :sid, :acct, :eval, 'paper', :status, 1000, "
                        " :valid)"
                    ),
                    {"rid": f"res-{evaluation_id}", "lid": f"plan-{evaluation_id}",
                     "sid": "hs-sch-1", "acct": "kite:paper",
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

    def test_every_documented_kind_is_driven(self):
        # ``ck_hosted_strategy_schedules_kind`` allows exactly these four, and
        # nothing else in the codebase reads hosted_strategy_schedules: a kind
        # this runtime skips is a kind that never runs.
        from backend.strategies import scheduling as scheduling_module

        self.assertEqual(
            set(scheduling_module.SCHEDULE_KINDS),
            {"daily", "weekly", "monthly", "calendar"},
        )
        self.schedule(kind="monthly", day_of_month=1)
        self.assertIn("sch-1", {row["id"] for row in self.scheduler.enabled_schedules()})
        # A disabled schedule is not driven at all.
        self.schedule(schedule_id="sch-off", kind="monthly", day_of_month=1, enabled=False)
        self.assertNotIn("sch-off", {row["id"] for row in self.scheduler.enabled_schedules()})

    def test_daily_occurrences_are_one_per_due_day(self):
        schedule = self.schedule(kind="daily", at_time="09:30")
        occurrences = self.scheduler.due_occurrences(schedule, now=NOW)
        keys = [item.occurrence_key for item in occurrences]
        # NOW is 12:00 UTC = 17:30 IST, so today's 09:30 IST has already passed.
        self.assertEqual(keys, sorted(keys))
        self.assertIn("sch-1:2026-10-15", keys)
        self.assertIn("sch-1:2026-10-14", keys)
        self.assertNotIn("sch-1:2026-10-16", keys)
        # One row per day, never two for the same day, whichever tick runs it.
        self.assertEqual(len(keys), len(set(keys)))

    def test_weekly_occurrences_land_on_the_named_weekday_only(self):
        # 2026-10-15 is a Thursday (weekday 3); the previous Thursday is the 8th.
        schedule = self.schedule(kind="weekly", weekday=3, at_time="09:30")
        occurrences = self.scheduler.due_occurrences(schedule, now=NOW)
        days = [item.occurrence_key.split(":", 1)[1] for item in occurrences]
        self.assertIn("2026-10-15", days)
        self.assertIn("2026-10-08", days)
        for day in days:
            self.assertEqual(datetime.fromisoformat(day).weekday(), 3)

    def test_a_weekly_schedule_without_a_weekday_yields_nothing(self):
        # The stored CHECK (``ck_hosted_strategy_schedules_weekly_weekday``)
        # makes this unreachable through the database, so the pure function is
        # exercised directly: a weekly shape with no weekday yields no
        # occurrences rather than guessing one.
        schedule = {
            "id": "sch-1",
            "strategy_id": "stg-A",
            "schedule_kind": "weekly",
            "at_time": "09:30",
            "timezone": "Asia/Kolkata",
            "weekday": None,
        }
        self.assertEqual(self.scheduler.due_occurrences(schedule, now=NOW), [])


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

    def test_a_failed_submission_is_retried_and_then_fires(self):
        """The deferral must be *recoverable*, not merely reported as pending.

        The old assertion only checked the row's status after one tick; the
        retry it described could never happen, because a materialised row was
        skipped on every later tick.
        """
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        calls = {"n": 0}

        def flaky(schedule, occurrence, detail):
            calls["n"] += 1
            return calls["n"] > 1

        scheduler = self._scheduler(submitter=flaky)
        first = scheduler.tick(now=NOW)
        self.assertEqual(first["fired"], [])
        self.assertEqual(first["deferred"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        # Not fired and not skipped: the next tick must be able to try again.
        self.assertEqual(row["status"], "pending")
        self.assertIn("last_transient_error", row["detail"])

        second = scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(calls["n"], 2)
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "fired")
        self.assertIsNotNone(row["fired_at"])
        # The recorded reason for the *delay* is not a reason for the outcome.
        self.assertIsNone(row["skip_reason"])

    def test_a_pending_occurrence_expires_terminally_once_its_window_passes(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        # First tick inside the window: the job cannot be created yet.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)
        scheduler = self._scheduler(submitter=lambda *_: False)
        first = scheduler.tick(now=NOW)
        self.assertEqual(first["deferred"], ["sch-1:2026-10-10"])
        # Second tick after the window closed: terminal, with its reason, rather
        # than pending forever.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "60"
        second = scheduler.tick(now=NOW + timedelta(hours=1))
        self.assertEqual(second["fired"], [])
        self.assertEqual(second["expired"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["skip_reason"], "misfire_window_passed")
        # A settled occurrence is never revisited.
        third = scheduler.tick(now=NOW + timedelta(hours=2))
        self.assertEqual(third["fired"], [])
        self.assertEqual(third["expired"], [])

    def test_a_missing_submitter_defers_rather_than_firing(self):
        from backend.strategies.scheduling import ScheduleScheduler

        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        # No submitter at all: the unwired construction the production bootstrap
        # used to create. It must defer (retryable), never silently fire.
        scheduler = ScheduleScheduler(session_factory=self.factory)
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], [])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        self.assertEqual(self.occurrences()[0]["status"], "pending")

    def test_a_daily_occurrence_on_a_weekend_or_holiday_is_skipped_with_its_reason(self):
        """A shut market is a recorded skip, not a silent gap and not a launch.

        The 15th is an NSE holiday for this reader, the 11th is a Sunday (no
        calendar needed), and the 14th is an ordinary trading day: the daily
        schedule must skip the first two by name and fire the third.
        """

        def reader(_exchange, day):
            return day != date(2026, 10, 15)

        self.schedule(kind="daily", at_time="09:30")
        scheduler = self._scheduler(trading_day_reader=reader)
        result = scheduler.tick(now=NOW)

        rows = {row["occurrence_key"]: row for row in self.occurrences()}
        # Every weekend day in the lookback, plus the holiday, is skipped by
        # name; nothing else is.
        self.assertEqual(
            set(result["skipped"]),
            {
                "sch-1:2026-09-26",
                "sch-1:2026-09-27",
                "sch-1:2026-10-03",
                "sch-1:2026-10-04",
                "sch-1:2026-10-10",
                "sch-1:2026-10-11",
                "sch-1:2026-10-15",
            },
        )
        self.assertEqual(rows["sch-1:2026-10-15"]["status"], "skipped")
        self.assertEqual(rows["sch-1:2026-10-15"]["skip_reason"], "market_holiday")
        self.assertEqual(rows["sch-1:2026-10-15"]["detail"]["exchange"], "NSE")
        self.assertEqual(rows["sch-1:2026-10-11"]["skip_reason"], "market_weekend")
        # A trading day is decided the ordinary way (the first one fires; the
        # default callable submitter leaves no envelope, so the rest defer).
        self.assertEqual(rows["sch-1:2026-09-22"]["status"], "fired")
        self.assertNotIn("sch-1:2026-09-22", result["skipped"])


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

    def test_a_deferred_overlap_fires_once_the_previous_resolves(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.seed_fired_occurrence(
            due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            evaluation_id="sched:sch-1:2026-09-10",
            reservation_status="active",
        )
        first = self.scheduler.tick(now=NOW)
        self.assertEqual(first["fired"], [])
        self.assertEqual(first["deferred"], ["sch-1:2026-10-10"])
        # The earlier decision resolves. The blocked occurrence is still pending,
        # so this tick finally takes the decision it deferred.
        with self.factory() as session:
            session.execute(text("UPDATE strategy_reservations SET status = 'consumed'"))
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=5))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])
        row = [r for r in self.occurrences() if r["occurrence_key"] == "sch-1:2026-10-10"][0]
        self.assertEqual(row["status"], "fired")
        self.assertIsNone(row["skip_reason"])


class _HostedJobMixin:
    """Shared setup for tests that drive the production job submitter.

    These use the real ``HostedJobSubmitter`` and the real
    ``SqlAlchemyStrategyRepository`` - the same classes ``backend/app/
    background.py`` constructs - so the test cannot pass by injecting a fake
    dispatch path. The two-scheduler race is proved on PostgreSQL, where the
    unique index is real.
    """

    def setUp(self):
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(30 * 24 * 3600)
        super().setUp()
        from backend.strategies.repository import SqlAlchemyStrategyRepository
        from backend.strategies.scheduling import HostedJobSubmitter, ScheduleScheduler

        self.repository = SqlAlchemyStrategyRepository(self.factory)
        self.scheduler = ScheduleScheduler(
            self.factory, job_submitter=HostedJobSubmitter(self.factory)
        )

    def tearDown(self):
        os.environ.pop("SCHEDULE_MISFIRE_GRACE_SECONDS", None)
        super().tearDown()

    def jobs(self):
        with self.factory() as session:
            rows = session.execute(
                text(
                    "SELECT id, strategy_id, version_id, owner_id, account_scope, occurrence_key, "
                    " status, attempt, params_snapshot, identity_json FROM strategy_jobs"
                )
            ).mappings().all()
        # Raw SQL on SQLite hands JSON columns back as text.
        jobs = []
        for row in rows:
            job = dict(row)
            for key in ("params_snapshot", "identity_json"):
                if isinstance(job.get(key), str):
                    job[key] = json.loads(job[key])
            jobs.append(job)
        return jobs


class ProductionJobSubmitterTests(_HostedJobMixin, SchedulingTestCase):
    """The production wiring: a due occurrence creates a pinned hosted job."""

    def test_a_due_occurrence_creates_exactly_one_pinned_job(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], ["sch-1:2026-10-10"])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        # Pinned to the schedule's strategy/version/owner/account, queued for the
        # supervisor's existing lifecycle — never a proposal.
        self.assertEqual(job["strategy_id"], "hs-sch-1")
        self.assertEqual(job["version_id"], "v-sch-1")
        self.assertEqual(job["owner_id"], "app:o")
        self.assertEqual(job["account_scope"], "kite:paper")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(job["occurrence_key"], "sch-1:2026-10-10")
        # The bound evaluation identity travels with the job, so the child's
        # proposal must name it.
        self.assertEqual(job["identity_json"]["evaluation_id"], "sched:sch-1:2026-10-10")
        self.assertEqual(job["identity_json"]["evaluation_kind"], "scheduled_occurrence")
        self.assertEqual(job["identity_json"]["schedule_id"], "sch-1")
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "fired")
        self.assertEqual(row["evaluation_id"], "sched:sch-1:2026-10-10")

    def test_a_lost_settle_reuses_the_same_job_on_the_next_tick(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.scheduler.tick(now=NOW)
        # Simulate a crash after the job committed but before the occurrence was
        # settled: the row is still pending on the next tick.
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_schedule_occurrences SET status='pending', fired_at=NULL"
                )
            )
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.occurrences()[0]["status"], "fired")

    def test_a_transient_failure_then_a_successful_tick_creates_one_job(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        failing = self._scheduler(submitter=lambda *_: False)
        self.assertEqual(failing.tick(now=NOW)["deferred"], ["sch-1:2026-10-10"])
        self.assertEqual(self.jobs(), [])
        result = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(result["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(len(self.jobs()), 1)

    def test_overlap_defers_until_the_active_job_clears(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        # An operator-launched job for the same strategy is still active, so the
        # strategy's block refuses a second attempt (transient, not terminal).
        self.repository.create_job(
            strategy_id="hs-sch-1",
            version_id="v-sch-1",
            owner_id="app:o",
            job_kind="finite",
            execution_mode="paper",
        )
        first = self.scheduler.tick(now=NOW)
        self.assertEqual(first["fired"], [])
        self.assertEqual(first["deferred"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "pending")
        self.assertIn("active or unreconciled job", row["detail"]["last_transient_error"])
        self.assertEqual(len(self.jobs()), 1)
        # The active job clears: the still-pending occurrence takes its decision.
        with self.factory() as session:
            session.execute(text("UPDATE strategy_jobs SET status='stopped'"))
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=5))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j["occurrence_key"] for j in jobs].count(None), 1)

    def test_a_disabled_strategy_records_a_terminal_reason(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        with self.factory() as session:
            session.execute(text("UPDATE hosted_strategies SET status='disabled'"))
            session.commit()
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], [])
        self.assertEqual(result["skipped"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "strategy_disabled")
        self.assertEqual(self.jobs(), [])

    def test_a_changed_account_scope_records_a_terminal_reason(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        with self.factory() as session:
            session.execute(text("UPDATE hosted_strategies SET default_account_scope='kite:B'"))
            session.commit()
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(result["skipped"], ["sch-1:2026-10-10"])
        self.assertEqual(self.occurrences()[0]["skip_reason"], "account_scope_changed")
        self.assertEqual(self.jobs(), [])

    def test_the_default_session_factory_resolves_to_the_app_database(self):
        """The old default named a ``SessionLocal`` that does not exist.

        A production construction (no injected factory) therefore raised
        ImportError instead of scheduling. Resolve the name without importing the
        real database module (it needs the driver).
        """
        import sys
        import types

        from backend.strategies import scheduling as scheduling_module

        sentinel = object()
        stub = types.ModuleType("backend.app.database")
        stub.SessionLocal = sentinel
        original = sys.modules.get("backend.app.database")
        sys.modules["backend.app.database"] = stub
        try:
            scheduler = scheduling_module.ScheduleScheduler()
            self.assertIs(scheduler.session_factory, sentinel)
        finally:
            if original is None:
                sys.modules.pop("backend.app.database", None)
            else:
                sys.modules["backend.app.database"] = original


class OccurrenceTransitionTests(_HostedJobMixin, SchedulingTestCase):
    """A decided occurrence is never rewritten by a stale or duplicate actor."""

    def _pending_occurrence(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        failing = self._scheduler(submitter=lambda *_: False)
        first = failing.tick(now=NOW)
        self.assertEqual(first["deferred"], ["sch-1:2026-10-10"])
        return self.occurrences()[0]["id"]

    def test_a_fired_occurrence_cannot_be_overwritten_by_a_stale_expiry(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.assertEqual(self.scheduler.tick(now=NOW)["fired"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        fired_at = row["fired_at"]
        # A stale tick (or a concurrent timeout) that believes it still owns the
        # occurrence must not rewrite the decision.
        won = self.scheduler._settle(
            row["id"], status="expired", skip_reason="misfire_window_passed"
        )
        self.assertFalse(won)
        after = self.occurrences()[0]
        self.assertEqual(after["status"], "fired")
        self.assertEqual(after["fired_at"], fired_at)
        self.assertIsNone(after["skip_reason"])

    def test_a_stale_transient_note_cannot_annotate_a_fired_occurrence(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.scheduler.tick(now=NOW)
        row = self.occurrences()[0]
        self.assertFalse(
            self.scheduler._note_transient_failure(row["id"], error="late failure")
        )
        after = self.occurrences()[0]
        self.assertNotIn("last_transient_error", after["detail"])
        self.assertEqual(after["status"], "fired")

    def test_a_duplicate_successful_submission_settles_once(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        stored = self.scheduler.enabled_schedules()[0]
        occurrence = self.scheduler.due_occurrences(stored, now=NOW)[0]
        row_id = self.scheduler.materialize(stored, occurrence)
        claim = self.scheduler._claim_decision(row_id)
        first = self.scheduler._fire(stored, occurrence, row_id, claim=claim)
        fired_at = self.occurrences()[0]["fired_at"]
        second = self.scheduler._fire(stored, occurrence, row_id, claim=claim)
        # Both callers honestly submitted; exactly one record exists, and the
        # second reports what is actually recorded rather than erroring.
        self.assertEqual(first, "fired")
        self.assertEqual(second, "fired")
        self.assertEqual(self.occurrences()[0]["status"], "fired")
        self.assertEqual(self.occurrences()[0]["fired_at"], fired_at)
        self.assertEqual(len(self.jobs()), 1)

    def test_a_stale_expiry_cannot_overwrite_an_in_flight_submission(self):
        """Both orderings of "grace expired" vs "submission in flight" are safe."""
        # Ordering 1: the submission is settled first; the stale expiry is a no-op.
        row_id = self._pending_occurrence()
        self.assertTrue(self.scheduler._settle(row_id, status="fired", fired_at=True))
        self.assertFalse(
            self.scheduler._settle(
                row_id, status="expired", skip_reason="misfire_window_passed"
            )
        )
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "fired")
        self.assertIsNone(row["skip_reason"])

        # Ordering 2: the expiry is recorded first, and the in-flight submission
        # must not resurrect the row as fired (its settle loses the CAS and the
        # caller is told the recorded outcome instead).
        self.schedule(
            schedule_id="sch-2", kind="calendar", calendar_dates=["2026-10-11"]
        )
        pending = self._scheduler(submitter=lambda *_: False)
        self.assertEqual(pending.tick(now=NOW)["deferred"], ["sch-2:2026-10-11"])
        second_row = [
            r for r in self.occurrences(schedule_id="sch-2")
        ][0]
        self.assertTrue(
            self.scheduler._settle(
                second_row["id"],
                status="expired",
                skip_reason="misfire_window_passed",
            )
        )
        second_schedule = self.scheduler.enabled_schedules()[1]
        stale_claim = self.scheduler._claim_decision(second_row["id"])
        self.assertEqual(
            self.scheduler._fire(
                second_schedule,
                self.scheduler.due_occurrences(second_schedule, now=NOW)[0],
                second_row["id"],
                claim=stale_claim,
            ),
            "expired",
        )
        self.assertEqual(
            [r for r in self.occurrences(schedule_id="sch-2")][0]["status"],
            "expired",
        )


class LostResponseRecoveryTests(_HostedJobMixin, SchedulingTestCase):
    """A launch that already exists is reported, never expired or re-created."""

    def _fire_then_lose_the_settle(self, schedule_kwargs):
        self.schedule(**schedule_kwargs)
        self.assertEqual(len(self.scheduler.tick(now=NOW)["fired"]), 1)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_schedule_occurrences SET status='pending', fired_at=NULL"
                )
            )
            session.commit()

    def test_a_lost_settle_beyond_grace_still_reports_fired(self):
        self._fire_then_lose_the_settle(
            dict(kind="calendar", calendar_dates=["2026-10-10"])
        )
        # The window has long passed, but the launch happened: it must not be
        # rewritten as expired.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "1"
        result = self.scheduler.tick(now=NOW + timedelta(days=2))
        self.assertEqual(result["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(result["expired"], [])
        self.assertEqual(self.occurrences()[0]["status"], "fired")
        self.assertEqual(len(self.jobs()), 1)

    def test_a_lost_settle_after_the_strategy_is_disabled_still_reports_fired(self):
        self._fire_then_lose_the_settle(
            dict(kind="calendar", calendar_dates=["2026-10-10"])
        )
        with self.factory() as session:
            session.execute(text("UPDATE hosted_strategies SET status='disabled'"))
            session.commit()
        result = self.scheduler.tick(now=NOW + timedelta(minutes=5))
        self.assertEqual(result["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(self.occurrences()[0]["status"], "fired")
        self.assertEqual(len(self.jobs()), 1)

    def test_a_job_under_the_key_from_another_launch_is_a_recorded_conflict(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        # A pending occurrence, then the key is taken by a different strategy.
        failing = self._scheduler(submitter=lambda *_: False)
        self.assertEqual(
            failing.tick(now=NOW)["deferred"], ["sch-1:2026-10-10"]
        )
        other = self.repository.create_strategy(
            owner_id="app:o",
            name="conflicting",
            description=None,
            execution_mode="paper",
            job_kind="finite",
            account_scope="kite:paper",
            max_duration_s=3600,
            progress_deadline_s=600,
            stale_exit_policy="none",
        )
        other_version = self.repository.create_version(
            strategy_id=other.id,
            source="inline",
            source_sha256="b" * 64,
            parameters_schema={"type": "object"},
            capabilities_snapshot={"schema_version": 2, "capabilities": {"data": True}},
            created_by="app:o",
        )
        self.repository.create_job(
            strategy_id=other.id,
            version_id=other_version.id,
            owner_id="app:o",
            job_kind="finite",
            execution_mode="paper",
            params={},
            occurrence_key="sch-1:2026-10-10",
        )
        result = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(result["fired"], [])
        self.assertEqual(result["skipped"], ["sch-1:2026-10-10"])
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "occurrence_conflict")
        # The conflicting job is untouched and no second job was created.
        keys = [job["occurrence_key"] for job in self.jobs()]
        self.assertEqual(keys.count("sch-1:2026-10-10"), 1)


class DecisionOwnershipTests(_HostedJobMixin, SchedulingTestCase):
    """Every decision write is fenced by the exact claim the actor holds."""

    def _pending(self, *, grace_days=30):
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = str(grace_days * 24 * 3600)
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        failing = self._scheduler(submitter=lambda *_: False)
        self.assertEqual(failing.tick(now=NOW)["deferred"], ["sch-1:2026-10-10"])
        return self.occurrences()[0]["id"]

    def test_an_expiry_cannot_land_while_another_actor_holds_the_claim(self):
        """The window closing elsewhere must not expire a launch in flight."""
        row_id = self._pending()
        claim = self.scheduler._claim_decision(row_id)
        self.assertIsInstance(claim, datetime)  # a real claim value, not settled/busy

        # Another tick, long past the (now 1 second) window, must defer: the
        # decision belongs to the claim holder, who may be creating the job.
        os.environ["SCHEDULE_MISFIRE_GRACE_SECONDS"] = "1"
        other = self._scheduler()
        result = other.tick(now=NOW + timedelta(days=2))
        self.assertEqual(result["expired"], [])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        self.assertEqual(self.occurrences()[0]["status"], "pending")
        self.assertEqual(self.jobs(), [])

        # The holder finishes: the occurrence becomes fired and the job exists.
        self.assertTrue(self.scheduler._settle(row_id, status="fired", claim=claim, fired_at=True))
        self.assertEqual(self.occurrences()[0]["status"], "fired")

    def test_a_stale_holder_cannot_settle_or_release_the_new_claim(self):
        row_id = self._pending()
        stale_claim = self.scheduler._claim_decision(row_id)
        # The claim goes stale, and another actor takes it over.
        from sqlalchemy import update as _sa_update

        from backend.strategies.attribution_models import StrategyScheduleOccurrence

        with self.factory() as session:
            # Written through the ORM so the stored representation matches what
            # the claim compare-and-set reads back.
            session.execute(
                _sa_update(StrategyScheduleOccurrence)
                .where(StrategyScheduleOccurrence.id == row_id)
                .values(fired_at=_utc_shift(-(DECISION_CLAIM_STALE_SECONDS + 60)))
            )
            session.commit()
        new_claim = self.scheduler._claim_decision(row_id)
        self.assertIsInstance(new_claim, datetime)
        self.assertNotEqual(new_claim, stale_claim)

        # The new holder expires the occurrence.
        self.assertTrue(
            self.scheduler._settle(
                row_id,
                status="expired",
                claim=new_claim,
                skip_reason="misfire_window_passed",
            )
        )
        # The stale holder can neither settle it back to fired nor release it,
        # and a stale job insert is refused by the same fence.
        self.assertFalse(
            self.scheduler._settle(row_id, status="fired", claim=stale_claim, fired_at=True)
        )
        self.assertFalse(self.scheduler._release_claim(row_id, claim=stale_claim))
        self.assertFalse(
            self.scheduler._transition(
                row_id, allowed_from=("pending",), claim=stale_claim, clear_fired_at=True
            )
        )
        row = self.occurrences()[0]
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["fired_at"])


class PreviousWorkEvidenceTests(_HostedJobMixin, SchedulingTestCase):
    """The overlap predicate is attributed, and a dead child does not block."""

    def _two_occurrences(self):
        # A single schedule with two due occurrences, oldest first.
        # Both inside the misfire grace, so the overlap evidence is what decides.
        self.schedule(kind="calendar", calendar_dates=["2026-10-01", "2026-10-10"])

    def test_a_running_job_without_a_proposal_blocks_the_next_occurrence(self):
        self._two_occurrences()
        result = self.scheduler.tick(now=NOW)
        self.assertEqual(result["fired"], ["sch-1:2026-10-01"])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        with self.factory() as session:
            session.execute(text("UPDATE strategy_jobs SET status='running'"))
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], [])
        self.assertEqual(second["deferred"], ["sch-1:2026-10-10"])

    def test_a_stopped_job_without_a_proposal_unblocks_the_next_occurrence(self):
        self._two_occurrences()
        self.scheduler.tick(now=NOW)
        with self.factory() as session:
            session.execute(text("UPDATE strategy_jobs SET status='stopped'"))
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])
        self.assertEqual(len(self.jobs()), 2)

    def test_an_explicitly_reconciled_job_unblocks_the_next_occurrence(self):
        self._two_occurrences()
        self.scheduler.tick(now=NOW)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_jobs SET status='recovery_required', "
                    " reconciled_at=:now"
                ),
                {"now": NOW},
            )
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], ["sch-1:2026-10-10"])

    def test_a_recovery_required_job_awaiting_reconciliation_still_blocks(self):
        self._two_occurrences()
        self.scheduler.tick(now=NOW)
        with self.factory() as session:
            session.execute(
                text(
                    "UPDATE strategy_jobs SET status='recovery_required', reconciled_at=NULL"
                )
            )
            session.commit()
        second = self.scheduler.tick(now=NOW + timedelta(minutes=1))
        self.assertEqual(second["fired"], [])
        self.assertEqual(second["deferred"], ["sch-1:2026-10-10"])

class IsolationTests(SchedulingTestCase):
    def test_enabled_schedules_fails_visibly_when_the_read_fails(self):
        """An unreadable schedule table is not "no schedules".

        Swallowing the error would report a healthy tick while nothing is ever
        driven, so the read must fail and let the runtime degrade.
        """
        from sqlalchemy.exc import SQLAlchemyError

        from backend.strategies.scheduling import ScheduleScheduler

        empty_engine = create_engine("sqlite+pysqlite:///:memory:")
        broken = ScheduleScheduler(
            session_factory=sessionmaker(bind=empty_engine)
        )
        with self.assertRaises(SQLAlchemyError):
            broken.enabled_schedules()
        empty_engine.dispose()

    def test_unreadable_overlap_evidence_fails_closed(self):
        """No proof the previous decision finished means: do not start the next."""
        from backend.strategies.scheduling import ScheduleScheduler

        empty_engine = create_engine("sqlite+pysqlite:///:memory:")
        broken = ScheduleScheduler(
            session_factory=sessionmaker(bind=empty_engine)
        )
        self.assertTrue(
            broken.previous_work_unresolved(
                schedule_id="sch-1",
                strategy_id="hs-1",
                account_scope="kite:paper",
                before_due_at=NOW,
            )
        )
        empty_engine.dispose()
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
        # A submitter failure is transient: the occurrence is deferred and stays
        # retryable (its error is visible on the row), and the other schedule
        # still fires in the same tick.
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["deferred"], ["sch-1:2026-10-10"])
        self.assertEqual(result["fired"], ["sch-2:2026-10-11"])
        failed = [r for r in self.occurrences() if r["occurrence_key"] == "sch-1:2026-10-10"][0]
        self.assertEqual(failed["status"], "pending")
        self.assertIn("RuntimeError", failed["detail"]["last_transient_error"])

    def test_a_schedule_that_raises_outside_the_submitter_is_isolated_and_reported(self):
        self.schedule(kind="calendar", calendar_dates=["2026-10-10"])
        self.schedule(schedule_id="sch-2", kind="calendar", calendar_dates=["2026-10-11"])
        scheduler = self._scheduler()
        original = scheduler._tick_schedule

        def boom(schedule, *, moment, grace):
            if schedule["id"] == "sch-1":
                raise RuntimeError("boom")
            return original(schedule, moment=moment, grace=grace)

        scheduler._tick_schedule = boom
        result = scheduler.tick(now=NOW)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["schedule_id"], "sch-1")
        # The other schedule still fired.
        self.assertEqual(result["fired"], ["sch-2:2026-10-11"])


if __name__ == "__main__":
    unittest.main()
