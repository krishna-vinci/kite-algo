import json
import unittest
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from journaling.models import JournalMetricSnapshot
from journaling.repository import JournalRepository


class _FakeResult:
    def __init__(self, row=None, rows=None, scalar=None):
        self._row = row
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return self._rows

    def scalar_one(self):
        if self._scalar is None:
            raise AssertionError("No scalar value present")
        return self._scalar


class _FakeSession:
    def __init__(self):
        self.execution_environments = []
        self.execution_contexts = []
        self.episodes = []
        self.execution_intents = []
        self.strategy_templates = []
        self.strategy_variants = []
        self.strategy_deployments = []
        self.metric_snapshots = []
        self.notes = []
        self.note_revisions = []
        self.attachments = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}

        if "INSERT INTO public.journal_notes" in sql:
            note_id = str(uuid4())
            now = datetime.now(timezone.utc)
            row = {
                "id": note_id,
                "environment_id": params["environment_id"],
                "subject_type": params["subject_type"],
                "subject_id": params["subject_id"],
                "episode_id": params.get("episode_id"),
                "note_type": params["note_type"],
                "title": params["title"],
                "body_markdown": params["body_markdown"],
                "body_text": params.get("body_text") or "",
                "body_json": params.get("body_json"),
                "effective_at": params.get("effective_at"),
                "author_id": params.get("author_id"),
                "tags_json": params["tags_json"],
                "metadata_json": params["metadata_json"],
                "created_at": now,
                "updated_at": now,
                "archived_at": None,
            }
            self.notes.append(row)
            return _FakeResult(scalar=note_id)

        if "SELECT * FROM public.journal_notes WHERE id = CAST(:note_id AS uuid)" in sql:
            for row in self.notes:
                if row["id"] == params["note_id"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_revision_no" in sql and "FROM public.journal_note_revisions" in sql:
            note_id = params["note_id"]
            nums = [int(row["revision_no"]) for row in self.note_revisions if row["note_id"] == note_id]
            return _FakeResult(row={"next_revision_no": (max(nums) + 1) if nums else 1})

        if "INSERT INTO public.journal_note_revisions" in sql:
            revision_id = len(self.note_revisions) + 1
            row = {
                "id": revision_id,
                "note_id": params["note_id"],
                "revision_no": int(params["revision_no"]),
                "body_markdown": params["body_markdown"],
                "body_text": params["body_text"],
                "editor_id": params.get("editor_id"),
                "edited_at": datetime.now(timezone.utc),
                "change_reason": params.get("change_reason"),
                "metadata_json": params["metadata_json"],
            }
            self.note_revisions.append(row)
            return _FakeResult()

        if "UPDATE public.journal_notes" in sql:
            note_id = params["note_id"]
            for row in self.notes:
                if row["id"] != note_id:
                    continue
                row["title"] = params["title"]
                row["body_markdown"] = params["body_markdown"]
                row["body_text"] = params["body_text"]
                row["body_json"] = params["body_json"]
                row["tags_json"] = params["tags_json"]
                row["metadata_json"] = params["metadata_json"]
                row["updated_at"] = datetime.now(timezone.utc)
                break
            return _FakeResult()

        if "FROM public.journal_notes" in sql and "WHERE environment_id = CAST(:environment_id AS uuid)" in sql:
            rows = [row for row in self.notes if row["environment_id"] == params["environment_id"]]
            if params.get("subject_type") is not None:
                rows = [row for row in rows if row["subject_type"] == params["subject_type"]]
            if params.get("subject_id") is not None:
                rows = [row for row in rows if row["subject_id"] == params["subject_id"]]
            if params.get("episode_id") is not None:
                rows = [row for row in rows if row.get("episode_id") == params["episode_id"]]
            if params.get("note_type") is not None:
                rows = [row for row in rows if row["note_type"] == params["note_type"]]
            rows = sorted(rows, key=lambda item: (item.get("updated_at"), item.get("created_at"), item["id"]), reverse=True)
            start = int(params.get("offset") or 0)
            end = start + int(params.get("limit") or len(rows))
            return _FakeResult(rows=rows[start:end])

        if "FROM public.journal_note_revisions" in sql and "ORDER BY revision_no ASC" in sql:
            rows = [row for row in self.note_revisions if row["note_id"] == params["note_id"]]
            rows = sorted(rows, key=lambda item: (item["revision_no"], item["id"]))
            return _FakeResult(rows=rows)

        if "INSERT INTO public.journal_attachments" in sql:
            attachment_id = str(uuid4())
            row = {
                "id": attachment_id,
                "environment_id": params["environment_id"],
                "subject_type": params["subject_type"],
                "subject_id": params["subject_id"],
                "note_id": params.get("note_id"),
                "storage_key": params["storage_key"],
                "mime_type": params["mime_type"],
                "sha256": params.get("sha256"),
                "size_bytes": params.get("size_bytes"),
                "ocr_text": params.get("ocr_text"),
                "metadata_json": params["metadata_json"],
                "created_at": datetime.now(timezone.utc),
            }
            self.attachments.append(row)
            return _FakeResult(scalar=attachment_id)

        if "FROM public.journal_execution_environments" in sql and "COALESCE(broker_user_id" in sql:
            for row in self.execution_environments:
                if (
                    row["mode"] == params["mode"]
                    and row["account_scope"] == params["account_scope"]
                    and (row.get("broker_user_id") or "") == (params.get("broker_user_id") or "")
                    and (row.get("paper_account_key") or "") == (params.get("paper_account_key") or "")
                    and row["environment_epoch"] == params["environment_epoch"]
                ):
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_execution_environments" in sql:
            environment_id = str(uuid4())
            row = {
                "id": environment_id,
                "mode": params["mode"],
                "account_scope": params["account_scope"],
                "broker_user_id": params.get("broker_user_id"),
                "paper_account_key": params.get("paper_account_key"),
                "environment_epoch": params["environment_epoch"],
                "display_name": params.get("display_name"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "retired_at": None,
            }
            self.execution_environments.append(row)
            return _FakeResult(scalar=environment_id)

        if "UPDATE public.journal_execution_environments" in sql:
            env_id = params["environment_id"]
            for row in self.execution_environments:
                if row["id"] == env_id:
                    if params.get("display_name") is not None:
                        row["display_name"] = params["display_name"]
                    if params.get("metadata_json") is not None:
                        row["metadata_json"] = params["metadata_json"]
                    break
            return _FakeResult()

        if "FROM public.journal_execution_environments WHERE id = CAST(:environment_id AS uuid)" in sql:
            for row in self.execution_environments:
                if row["id"] == params["environment_id"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "FROM public.journal_execution_environments" in sql and "ORDER BY mode ASC" in sql:
            rows = self.execution_environments
            if params.get("mode") is not None:
                rows = [row for row in rows if row["mode"] == params["mode"]]
            rows = sorted(rows, key=lambda item: (item["mode"], item["account_scope"], item["environment_epoch"]))
            return _FakeResult(rows=rows)

        if "FROM public.journal_execution_contexts" in sql and "external_run_id = :external_run_id" in sql:
            for row in self.execution_contexts:
                if (
                    row["environment_id"] == params["environment_id"]
                    and row["source_system"] == params["source_system"]
                    and row["external_run_id"] == params["external_run_id"]
                ):
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_execution_contexts" in sql:
            context_id = str(uuid4())
            row = {
                "id": context_id,
                "environment_id": params["environment_id"],
                "source_system": params["source_system"],
                "external_run_id": params["external_run_id"],
                "strategy_template_id": None,
                "strategy_variant_id": None,
                "strategy_deployment_id": params.get("deployment_id"),
                "status": params["status"],
                "opened_at": params.get("opened_at"),
                "closed_at": params.get("closed_at"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
            }
            self.execution_contexts.append(row)
            return _FakeResult(scalar=context_id)

        if "UPDATE public.journal_execution_contexts" in sql:
            context_id = params["context_id"]
            for row in self.execution_contexts:
                if row["id"] != context_id:
                    continue
                if params.get("deployment_id") is not None:
                    row["strategy_deployment_id"] = params["deployment_id"]
                if params.get("status") is not None:
                    row["status"] = params["status"]
                if params.get("ended_at") is not None:
                    row["closed_at"] = params["ended_at"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "FROM public.journal_execution_contexts WHERE id = CAST(:context_id AS uuid)" in sql:
            for row in self.execution_contexts:
                if row["id"] == params["context_id"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "SELECT COALESCE(MAX(episode_seq), 0) AS max_episode_seq" in sql and "FROM public.journal_episodes" in sql:
            context_id = params["execution_context_id"]
            seqs = [int(row["episode_seq"]) for row in self.episodes if row["execution_context_id"] == context_id]
            return _FakeResult(row={"max_episode_seq": max(seqs) if seqs else 0})

        if "FROM public.journal_episodes" in sql and "episode_seq = :episode_seq" in sql and "execution_context_id = CAST(:execution_context_id AS uuid)" in sql:
            for row in self.episodes:
                if row["execution_context_id"] == params["execution_context_id"] and row["episode_seq"] == params["episode_seq"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_episodes" in sql:
            episode_id = str(uuid4())
            row = {
                "id": episode_id,
                "environment_id": params["environment_id"],
                "execution_context_id": params["execution_context_id"],
                "episode_seq": int(params["episode_seq"]),
                "status": params["status"],
                "opened_at": params.get("opened_at"),
                "closed_at": params.get("closed_at"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "updated_at": None,
            }
            self.episodes.append(row)
            return _FakeResult(scalar=episode_id)

        if "UPDATE public.journal_episodes" in sql:
            episode_id = params["episode_id"]
            for row in self.episodes:
                if row["id"] != episode_id:
                    continue
                row["status"] = params["status"]
                if params.get("closed_at") is not None:
                    row["closed_at"] = params["closed_at"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "SELECT metadata_json FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid)" in sql:
            for row in self.episodes:
                if row["id"] == params["episode_id"]:
                    return _FakeResult(row={"metadata_json": row.get("metadata_json")})
            return _FakeResult(row=None)

        if "FROM public.journal_episodes WHERE id = CAST(:episode_id AS uuid)" in sql:
            for row in self.episodes:
                if row["id"] == params["episode_id"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "FROM public.journal_episodes" in sql and "ORDER BY opened_at DESC, episode_seq DESC" in sql:
            rows = self.episodes
            if params.get("environment_id") is not None:
                rows = [row for row in rows if row["environment_id"] == params["environment_id"]]
            if params.get("execution_context_id") is not None:
                rows = [row for row in rows if row["execution_context_id"] == params["execution_context_id"]]
            if params.get("status") is not None:
                rows = [row for row in rows if row["status"] == params["status"]]
            rows = sorted(rows, key=lambda item: (item.get("opened_at") or "", item.get("episode_seq") or 0), reverse=True)
            start = int(params.get("offset") or 0)
            end = start + int(params.get("limit") or len(rows))
            return _FakeResult(rows=rows[start:end])

        if "FROM public.journal_execution_intents" in sql and "idempotency_key = :idempotency_key" in sql and "environment_id = CAST(:environment_id AS uuid)" in sql:
            for row in self.execution_intents:
                if row["environment_id"] == params["environment_id"] and row.get("idempotency_key") == params["idempotency_key"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_execution_intents" in sql:
            intent_id = str(uuid4())
            row = {
                "id": intent_id,
                "environment_id": params["environment_id"],
                "execution_context_id": params.get("execution_context_id"),
                "episode_id": params.get("episode_id"),
                "channel": params.get("channel"),
                "intent_type": params.get("intent_type"),
                "idempotency_key": params.get("idempotency_key"),
                "status": params["status"],
                "requested_at": params.get("requested_at"),
                "resolved_at": params.get("resolved_at"),
                "payload_json": params["payload_json"],
                "result_json": params["result_json"],
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "updated_at": None,
            }
            self.execution_intents.append(row)
            return _FakeResult(scalar=intent_id)

        if "SELECT result_json, metadata_json FROM public.journal_execution_intents WHERE id = CAST(:intent_id AS uuid)" in sql:
            for row in self.execution_intents:
                if row["id"] == params["intent_id"]:
                    return _FakeResult(row={"result_json": row.get("result_json"), "metadata_json": row.get("metadata_json")})
            return _FakeResult(row=None)

        if "UPDATE public.journal_execution_intents" in sql:
            intent_id = params["intent_id"]
            for row in self.execution_intents:
                if row["id"] != intent_id:
                    continue
                row["status"] = params["status"]
                if params.get("resolved_at") is not None:
                    row["resolved_at"] = params["resolved_at"]
                row["result_json"] = params["result_json"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "FROM public.journal_strategy_templates" in sql and "WHERE template_key = :template_key" in sql:
            for row in self.strategy_templates:
                if row["template_key"] == params["template_key"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_strategy_templates" in sql:
            template_id = str(uuid4())
            row = {
                "id": template_id,
                "strategy_family": params["strategy_family"],
                "template_key": params["template_key"],
                "display_name": params.get("display_name"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "updated_at": None,
            }
            self.strategy_templates.append(row)
            return _FakeResult(scalar=template_id)

        if "UPDATE public.journal_strategy_templates" in sql:
            template_id = params["template_id"]
            for row in self.strategy_templates:
                if row["id"] != template_id:
                    continue
                row["strategy_family"] = params["strategy_family"]
                if params.get("display_name") is not None:
                    row["display_name"] = params["display_name"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "FROM public.journal_strategy_templates WHERE id = CAST(:template_id AS uuid)" in sql:
            for row in self.strategy_templates:
                if row["id"] == params["template_id"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "FROM public.journal_strategy_templates" in sql and "ORDER BY strategy_family ASC" in sql:
            rows = self.strategy_templates
            if params.get("strategy_family") is not None:
                rows = [row for row in rows if row["strategy_family"] == params["strategy_family"]]
            rows = sorted(rows, key=lambda item: (item["strategy_family"], item["template_key"]))
            return _FakeResult(rows=rows)

        if "FROM public.journal_strategy_variants" in sql and "WHERE template_id = CAST(:template_id AS uuid)" in sql and "variant_key = :variant_key" in sql:
            for row in self.strategy_variants:
                if row["template_id"] == params["template_id"] and row["variant_key"] == params["variant_key"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_strategy_variants" in sql:
            variant_id = str(uuid4())
            row = {
                "id": variant_id,
                "template_id": params["template_id"],
                "variant_key": params["variant_key"],
                "display_name": params.get("display_name"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "updated_at": None,
            }
            self.strategy_variants.append(row)
            return _FakeResult(scalar=variant_id)

        if "UPDATE public.journal_strategy_variants" in sql:
            variant_id = params["variant_id"]
            for row in self.strategy_variants:
                if row["id"] != variant_id:
                    continue
                if params.get("display_name") is not None:
                    row["display_name"] = params["display_name"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "FROM public.journal_strategy_variants" in sql and "ORDER BY variant_key ASC" in sql:
            rows = [row for row in self.strategy_variants if row["template_id"] == params["template_id"]]
            rows = sorted(rows, key=lambda item: item["variant_key"])
            return _FakeResult(rows=rows)

        if "FROM public.journal_strategy_deployments" in sql and "WHERE template_id = CAST(:template_id AS uuid)" in sql and "deployment_key = :deployment_key" in sql:
            for row in self.strategy_deployments:
                if row["template_id"] == params["template_id"] and row["deployment_key"] == params["deployment_key"]:
                    return _FakeResult(row=row)
            return _FakeResult(row=None)

        if "INSERT INTO public.journal_strategy_deployments" in sql:
            deployment_id = str(uuid4())
            row = {
                "id": deployment_id,
                "template_id": params["template_id"],
                "variant_id": params.get("variant_id"),
                "deployment_key": params["deployment_key"],
                "display_name": params.get("display_name"),
                "metadata_json": params["metadata_json"],
                "created_at": None,
                "updated_at": None,
            }
            self.strategy_deployments.append(row)
            return _FakeResult(scalar=deployment_id)

        if "UPDATE public.journal_strategy_deployments" in sql:
            deployment_id = params["deployment_id"]
            for row in self.strategy_deployments:
                if row["id"] != deployment_id:
                    continue
                if params.get("variant_id") is not None:
                    row["variant_id"] = params["variant_id"]
                if params.get("display_name") is not None:
                    row["display_name"] = params["display_name"]
                if params.get("metadata_json") is not None:
                    row["metadata_json"] = params["metadata_json"]
                break
            return _FakeResult()

        if "FROM public.journal_strategy_deployments" in sql and "ORDER BY deployment_key ASC" in sql:
            rows = [row for row in self.strategy_deployments if row["template_id"] == params["template_id"]]
            rows = sorted(rows, key=lambda item: item["deployment_key"])
            return _FakeResult(rows=rows)

        if "INSERT INTO public.journal_metric_snapshots" in sql:
            environment_id = params.get("environment_id")
            if environment_id is None:
                for row in self.metric_snapshots:
                    if (
                        row.get("environment_id") is None
                        and row["subject_type"] == params["subject_type"]
                        and row["subject_id"] == params["subject_id"]
                        and row["time_window"] == params["window"]
                        and row["calc_version"] == params["calc_version"]
                    ):
                        row["computed_at"] = params["computed_at"]
                        row["metrics_json"] = params["metrics_json"]
                        row["identity_rule_version"] = params["identity_rule_version"]
                        row["grouping_rule_version"] = params["grouping_rule_version"]
                        return _FakeResult(scalar=row["id"])
            else:
                for row in self.metric_snapshots:
                    if (
                        row.get("environment_id") == environment_id
                        and row["subject_type"] == params["subject_type"]
                        and row["subject_id"] == params["subject_id"]
                        and row["time_window"] == params["window"]
                        and row["calc_version"] == params["calc_version"]
                        and row["identity_rule_version"] == params["identity_rule_version"]
                        and row["grouping_rule_version"] == params["grouping_rule_version"]
                    ):
                        row["computed_at"] = params["computed_at"]
                        row["metrics_json"] = params["metrics_json"]
                        return _FakeResult(scalar=row["id"])

            snapshot_id = len(self.metric_snapshots) + 1
            self.metric_snapshots.append(
                {
                    "id": snapshot_id,
                    "environment_id": environment_id,
                    "subject_type": params["subject_type"],
                    "subject_id": params["subject_id"],
                    "time_window": params["window"],
                    "calc_version": params["calc_version"],
                    "identity_rule_version": params["identity_rule_version"],
                    "grouping_rule_version": params["grouping_rule_version"],
                    "computed_at": params["computed_at"],
                    "metrics_json": params["metrics_json"],
                }
            )
            return _FakeResult(scalar=snapshot_id)

        if "FROM public.journal_metric_snapshots" in sql:
            rows = [
                row
                for row in self.metric_snapshots
                if row["subject_type"] == params["subject_type"]
                and row["subject_id"] == params["subject_id"]
                and (params.get("window") is None or row["time_window"] == params["window"])
                and (params.get("calc_version") is None or row["calc_version"] == params["calc_version"])
            ]
            if params.get("environment_id") is None:
                rows = [row for row in rows if row.get("environment_id") is None]
            else:
                rows = [row for row in rows if row.get("environment_id") == params["environment_id"]]
            if params.get("identity_rule_version") is not None:
                rows = [row for row in rows if row["identity_rule_version"] == params["identity_rule_version"]]
            if params.get("grouping_rule_version") is not None:
                rows = [row for row in rows if row["grouping_rule_version"] == params["grouping_rule_version"]]
            rows = sorted(rows, key=lambda item: (str(item.get("computed_at") or ""), item["id"]), reverse=True)
            return _FakeResult(row=rows[0] if rows else None)

        raise AssertionError(f"Unhandled SQL in fake session: {sql}")

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


class JournalRepositoryV2Tests(unittest.TestCase):
    def setUp(self):
        self.session = _FakeSession()
        self.repository = JournalRepository(session_factory=cast(Any, lambda: self.session))

    def test_ensure_execution_environment_inserts_and_returns_uuid(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
            metadata={"seed": 1},
        )

        assert isinstance(environment_id, str)
        assert len(environment_id) == 36
        assert self.session.commit_count == 1
        assert len(self.session.execution_environments) == 1
        assert self.session.execution_environments[0]["mode"] == "live"

    def test_ensuring_same_environment_twice_returns_same_id(self):
        first_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
            metadata={"first": True},
        )
        second_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
            metadata={"second": True},
        )

        assert first_id == second_id
        assert len(self.session.execution_environments) == 1
        stored = self.session.execution_environments[0]
        merged = json.loads(stored["metadata_json"])
        assert merged["first"] is True
        assert merged["second"] is True

    def test_same_account_scope_but_live_and_preview_modes_return_different_ids(self):
        live_id = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
        )
        preview_id = self.repository.ensure_execution_environment(
            mode="dry_run",
            account_scope="kite:XJJ446",
        )

        assert live_id != preview_id

    def test_repository_rejects_paper_mode_with_live_scope(self):
        with self.assertRaisesRegex(ValueError, "paper mode requires a paper account_scope"):
            self.repository.ensure_execution_environment(
                mode="paper",
                account_scope="kite:XJJ446",
            )

    def test_paper_epoch_one_and_two_return_different_ids(self):
        epoch_one = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-reset",
            paper_account_key="kite:paper-reset",
            environment_epoch=1,
        )
        epoch_two = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-reset",
            paper_account_key="kite:paper-reset",
            environment_epoch=2,
        )

        assert epoch_one != epoch_two

    def test_environment_epoch_zero_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "environment_epoch must be >= 1"):
            self.repository.ensure_execution_environment(
                mode="paper",
                account_scope="kite:paper-reset",
                paper_account_key="kite:paper-reset",
                environment_epoch=0,
            )

    def test_get_execution_environment_returns_decoded_metadata(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
            metadata={"display": "primary"},
        )
        # Simulate driver returning JSON string payload
        self.session.execution_environments[0]["metadata_json"] = '{"display":"primary"}'

        environment = self.repository.get_execution_environment(environment_id)

        assert environment is not None
        assert environment.id == environment_id
        assert environment.metadata == {"display": "primary"}

    def test_list_execution_environments_filters_by_mode(self):
        self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
        )
        self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-b",
            paper_account_key="kite:paper-b",
        )

        paper_only = self.repository.list_execution_environments(mode="paper")

        assert len(paper_only) == 2
        assert all(item.mode == "paper" for item in paper_only)

    def test_same_external_run_id_separates_contexts_by_environment(self):
        live_env = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
        )
        paper_env = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )

        live_ctx = self.repository.ensure_execution_context(
            environment_id=live_env,
            source_system="algo_worker",
            external_run_id="run-1",
        )
        paper_ctx = self.repository.ensure_execution_context(
            environment_id=paper_env,
            source_system="algo_worker",
            external_run_id="run-1",
        )

        assert live_ctx != paper_ctx

    def test_same_external_run_id_source_environment_returns_same_context_id(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )

        first_ctx = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-1",
            metadata={"a": 1},
        )
        second_ctx = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-1",
            metadata={"b": 2},
            resolved_identity={"template_id": "tmpl-1"},
            resolution_method="exact",
        )

        assert first_ctx == second_ctx
        assert len(self.session.execution_contexts) == 1
        metadata = json.loads(self.session.execution_contexts[0]["metadata_json"])
        assert metadata["a"] == 1
        assert metadata["b"] == 2
        assert metadata["resolved_identity"]["template_id"] == "tmpl-1"
        assert metadata["resolution_method"] == "exact"

    def test_blank_external_run_id_raises_value_error(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
        )

        with self.assertRaisesRegex(ValueError, "external_run_id is required"):
            self.repository.ensure_execution_context(
                environment_id=environment_id,
                source_system="algo_worker",
                external_run_id="   ",
            )

    def test_ensure_episode_none_sequence_creates_first_then_next_for_same_context(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-ep-1",
        )

        first = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=context_id,
            episode_seq=None,
            metadata={"open": 1},
        )
        second = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=context_id,
            episode_seq=None,
            metadata={"open": 2},
        )

        assert first != second
        assert [row["episode_seq"] for row in self.session.episodes] == [1, 2]

    def test_ensure_episode_explicit_sequence_reuses_row_and_merges_metadata(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-ep-2",
        )

        first = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=context_id,
            episode_seq=1,
            metadata={"a": 1},
        )
        second = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=context_id,
            episode_seq=1,
            status="open",
            metadata={"b": 2},
        )

        assert first == second
        assert len(self.session.episodes) == 1
        metadata = json.loads(self.session.episodes[0]["metadata_json"])
        assert metadata == {"a": 1, "b": 2}
        assert self.session.episodes[0]["status"] == "open"

    def test_ensure_episode_rejects_sequence_zero(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-ep-3",
        )

        with self.assertRaisesRegex(ValueError, "episode_seq must be >= 1"):
            self.repository.ensure_episode(
                environment_id=environment_id,
                execution_context_id=context_id,
                episode_seq=0,
            )

    def test_list_episodes_filters_by_environment_and_context(self):
        live_env = self.repository.ensure_execution_environment(
            mode="live",
            account_scope="kite:XJJ446",
            broker_user_id="XJJ446",
        )
        paper_env = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        live_ctx = self.repository.ensure_execution_context(
            environment_id=live_env,
            source_system="algo_worker",
            external_run_id="run-live",
        )
        paper_ctx = self.repository.ensure_execution_context(
            environment_id=paper_env,
            source_system="algo_worker",
            external_run_id="run-paper",
        )

        self.repository.ensure_episode(environment_id=live_env, execution_context_id=live_ctx, episode_seq=None)
        self.repository.ensure_episode(environment_id=paper_env, execution_context_id=paper_ctx, episode_seq=None)

        paper_only = self.repository.list_episodes(environment_id=paper_env)
        assert len(paper_only) == 1
        assert paper_only[0].environment_id == paper_env

        live_ctx_only = self.repository.list_episodes(execution_context_id=live_ctx)
        assert len(live_ctx_only) == 1
        assert live_ctx_only[0].execution_context_id == live_ctx

    def test_update_episode_status_sets_closed_and_merges_metadata(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        context_id = self.repository.ensure_execution_context(
            environment_id=environment_id,
            source_system="algo_worker",
            external_run_id="run-ep-4",
        )
        episode_id = self.repository.ensure_episode(
            environment_id=environment_id,
            execution_context_id=context_id,
            episode_seq=1,
            metadata={"seed": 1},
        )

        closed_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.repository.update_episode_status(
            episode_id,
            status="closed",
            closed_at=closed_at,
            metadata={"reason": "done"},
        )

        episode = self.repository.get_episode_detail(episode_id)
        assert episode is not None
        assert episode.status == "closed"
        assert episode.closed_at == closed_at
        assert episode.metadata == {"seed": 1, "reason": "done"}

    def test_create_execution_intent_idempotent_per_environment(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )

        first = self.repository.create_execution_intent(
            environment_id=environment_id,
            idempotency_key="idem-1",
            status="pending",
            result={"a": 1},
            metadata={"x": 1},
        )
        second = self.repository.create_execution_intent(
            environment_id=environment_id,
            idempotency_key="idem-1",
            status="resolved",
            result={"b": 2},
            metadata={"y": 2},
        )

        assert first == second
        assert len(self.session.execution_intents) == 1
        row = self.session.execution_intents[0]
        assert row["status"] == "resolved"
        assert json.loads(row["result_json"]) == {"a": 1, "b": 2}
        assert json.loads(row["metadata_json"]) == {"x": 1, "y": 2}

    def test_create_execution_intent_same_idempotency_key_different_environment_creates_new(self):
        env_one = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        env_two = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-b",
            paper_account_key="kite:paper-b",
        )

        first = self.repository.create_execution_intent(environment_id=env_one, idempotency_key="idem-2")
        second = self.repository.create_execution_intent(environment_id=env_two, idempotency_key="idem-2")

        assert first != second
        assert len(self.session.execution_intents) == 2

    def test_create_execution_intent_without_idempotency_key_always_creates_new(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )

        first = self.repository.create_execution_intent(environment_id=environment_id, idempotency_key=None)
        second = self.repository.create_execution_intent(environment_id=environment_id, idempotency_key=None)

        assert first != second
        assert len(self.session.execution_intents) == 2

    def test_create_execution_intent_blank_idempotency_key_raises(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )

        with self.assertRaisesRegex(ValueError, "idempotency_key cannot be blank"):
            self.repository.create_execution_intent(environment_id=environment_id, idempotency_key="   ")

    def test_ensure_strategy_template_inserts_and_returns_id(self):
        template_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
            display_name="Option Strategy",
            metadata={"seed": 1},
        )

        assert isinstance(template_id, str)
        assert len(template_id) == 36
        assert len(self.session.strategy_templates) == 1

    def test_ensuring_same_template_twice_returns_same_id_and_merges_metadata(self):
        first_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
            metadata={"a": 1},
        )
        second_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
            metadata={"b": 2},
        )

        assert first_id == second_id
        assert len(self.session.strategy_templates) == 1
        merged = json.loads(self.session.strategy_templates[0]["metadata_json"])
        assert merged["a"] == 1
        assert merged["b"] == 2

    def test_same_display_name_different_template_key_returns_different_template_ids(self):
        first = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
            display_name="Same Name",
        )
        second = self.repository.ensure_strategy_template(
            template_key="internal:indicator_strategy",
            strategy_family="indicator_strategy",
            display_name="Same Name",
        )

        assert first != second

    def test_ensure_strategy_variant_returns_same_id_for_same_template_and_variant(self):
        template_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
        )

        first = self.repository.ensure_strategy_variant(
            template_id=template_id,
            variant_key="scenario-a",
            metadata={"x": 1},
        )
        second = self.repository.ensure_strategy_variant(
            template_id=template_id,
            variant_key="scenario-a",
            metadata={"y": 2},
        )

        assert first == second
        assert len(self.session.strategy_variants) == 1
        merged = json.loads(self.session.strategy_variants[0]["metadata_json"])
        assert merged["x"] == 1
        assert merged["y"] == 2

    def test_ensure_strategy_deployment_returns_same_id_for_same_template_and_deployment(self):
        template_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
        )

        first = self.repository.ensure_strategy_deployment(
            template_id=template_id,
            deployment_key="deploy-1",
            metadata={"a": True},
        )
        second = self.repository.ensure_strategy_deployment(
            template_id=template_id,
            deployment_key="deploy-1",
            metadata={"b": True},
        )

        assert first == second
        assert len(self.session.strategy_deployments) == 1
        merged = json.loads(self.session.strategy_deployments[0]["metadata_json"])
        assert merged["a"] is True
        assert merged["b"] is True

    def test_blank_template_key_variant_key_and_deployment_key_raise_value_error(self):
        template_id = self.repository.ensure_strategy_template(
            template_key="internal:option_strategy",
            strategy_family="options_strategy",
        )

        with self.assertRaisesRegex(ValueError, "template_key is required"):
            self.repository.ensure_strategy_template(
                template_key="   ",
                strategy_family="options_strategy",
            )

        with self.assertRaisesRegex(ValueError, "variant_key is required"):
            self.repository.ensure_strategy_variant(
                template_id=template_id,
                variant_key="   ",
            )

        with self.assertRaisesRegex(ValueError, "deployment_key is required"):
            self.repository.ensure_strategy_deployment(
                template_id=template_id,
                deployment_key="   ",
            )

    def test_replace_legacy_metric_snapshot_uses_legacy_key(self):
        first = self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                subject_type="run",
                subject_id="run-1",
                window="since_inception",
                calc_version="v1",
                metrics={"net_pnl": 1},
            )
        )
        second = self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                subject_type="run",
                subject_id="run-1",
                window="since_inception",
                calc_version="v1",
                metrics={"net_pnl": 2},
            )
        )

        assert first == second
        assert len(self.session.metric_snapshots) == 1
        assert json.loads(self.session.metric_snapshots[0]["metrics_json"]) == {"net_pnl": 2}

    def test_replace_v2_metric_snapshot_partitions_by_environment_and_rule_versions(self):
        env_one = str(uuid4())
        env_two = str(uuid4())
        first = self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=env_one,
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                identity_rule_version="identity-v1",
                grouping_rule_version="grouping-v1",
                metrics={"net_pnl": 1},
            )
        )
        second = self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=env_two,
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                identity_rule_version="identity-v1",
                grouping_rule_version="grouping-v1",
                metrics={"net_pnl": 2},
            )
        )
        third = self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=env_one,
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                identity_rule_version="identity-v2",
                grouping_rule_version="grouping-v1",
                metrics={"net_pnl": 3},
            )
        )

        assert len({first, second, third}) == 3
        assert len(self.session.metric_snapshots) == 3

    def test_get_latest_metric_snapshot_defaults_to_legacy_environment_null(self):
        env_id = str(uuid4())
        self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                metrics={"net_pnl": 1},
            )
        )
        self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=env_id,
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                identity_rule_version="identity-v1",
                grouping_rule_version="grouping-v1",
                metrics={"net_pnl": 2},
            )
        )

        snapshot = self.repository.get_latest_metric_snapshot(
            subject_type="strategy_template",
            subject_id="tmpl-1",
            window="since_inception",
            calc_version="journal_v2_metrics_v1",
        )

        assert snapshot is not None
        assert snapshot.environment_id is None
        assert snapshot.metrics == {"net_pnl": 1}

    def test_get_latest_metric_snapshot_can_select_v2_environment_and_versions(self):
        env_id = str(uuid4())
        self.repository.replace_metric_snapshot(
            JournalMetricSnapshot(
                environment_id=env_id,
                subject_type="strategy_template",
                subject_id="tmpl-1",
                window="since_inception",
                calc_version="journal_v2_metrics_v1",
                identity_rule_version="identity-v1",
                grouping_rule_version="grouping-v1",
                metrics={"net_pnl": 2},
            )
        )

        snapshot = self.repository.get_latest_metric_snapshot(
            subject_type="strategy_template",
            subject_id="tmpl-1",
            window="since_inception",
            calc_version="journal_v2_metrics_v1",
            environment_id=env_id,
            identity_rule_version="identity-v1",
            grouping_rule_version="grouping-v1",
        )

        assert snapshot is not None
        assert snapshot.environment_id == env_id
        assert snapshot.identity_rule_version == "identity-v1"
        assert snapshot.grouping_rule_version == "grouping-v1"
        assert snapshot.metrics == {"net_pnl": 2}

    def test_create_note_stores_markdown_text_tags_and_metadata(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-notes",
            paper_account_key="kite:paper-notes",
        )

        note_id = self.repository.create_note(
            environment_id=environment_id,
            subject_type="episode",
            subject_id="ep-1",
            note_type="thesis",
            title="Breakout idea",
            body_markdown="## Setup\n- Breakout",
            body_text="Setup Breakout",
            tags=["breakout", "nifty"],
            metadata={"source": "test"},
        )

        assert isinstance(note_id, str)
        assert len(note_id) == 36
        assert len(self.session.notes) == 1
        row = self.session.notes[0]
        assert row["body_markdown"].startswith("## Setup")
        assert row["body_text"] == "Setup Breakout"
        assert json.loads(row["tags_json"]) == ["breakout", "nifty"]
        assert json.loads(row["metadata_json"]) == {"source": "test"}

    def test_update_note_creates_revision_before_updating_head(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-notes",
            paper_account_key="kite:paper-notes",
        )
        note_id = self.repository.create_note(
            environment_id=environment_id,
            subject_type="episode",
            subject_id="ep-1",
            note_type="thesis",
            title="Original",
            body_markdown="old body",
            body_text="old body",
            metadata={"seed": 1},
        )

        self.repository.update_note(
            note_id,
            title="Updated",
            body_markdown="new body",
            body_text="new body",
            metadata={"edited": True},
            editor_id="user-1",
            change_reason="tighten",
        )

        assert len(self.session.note_revisions) == 1
        rev = self.session.note_revisions[0]
        assert rev["note_id"] == note_id
        assert rev["revision_no"] == 1
        assert rev["body_markdown"] == "old body"
        assert rev["body_text"] == "old body"

        stored = self.repository.get_note(note_id)
        assert stored is not None
        assert stored.title == "Updated"
        assert stored.body_markdown == "new body"
        assert stored.body_text == "new body"
        assert stored.metadata == {"seed": 1, "edited": True}

    def test_list_notes_filters_by_environment_and_subject(self):
        env_a = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-a",
            paper_account_key="kite:paper-a",
        )
        env_b = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-b",
            paper_account_key="kite:paper-b",
        )
        self.repository.create_note(
            environment_id=env_a,
            subject_type="episode",
            subject_id="ep-1",
            note_type="thesis",
            title="A1",
            body_markdown="a1",
        )
        self.repository.create_note(
            environment_id=env_a,
            subject_type="strategy_template",
            subject_id="tmpl-1",
            note_type="lesson",
            title="A2",
            body_markdown="a2",
        )
        self.repository.create_note(
            environment_id=env_b,
            subject_type="episode",
            subject_id="ep-1",
            note_type="thesis",
            title="B1",
            body_markdown="b1",
        )

        result = self.repository.list_notes(env_a, subject_type="episode", subject_id="ep-1")

        assert len(result) == 1
        assert result[0].environment_id == env_a
        assert result[0].subject_type == "episode"
        assert result[0].subject_id == "ep-1"

    def test_list_note_revisions_orders_by_revision_number(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-rev",
            paper_account_key="kite:paper-rev",
        )
        note_id = self.repository.create_note(
            environment_id=environment_id,
            subject_type="episode",
            subject_id="ep-1",
            note_type="thesis",
            title="Original",
            body_markdown="v1",
            body_text="v1",
        )
        self.repository.update_note(note_id, body_markdown="v2", body_text="v2")
        self.repository.update_note(note_id, body_markdown="v3", body_text="v3")

        revisions = self.repository.list_note_revisions(note_id)

        assert [item.revision_no for item in revisions] == [1, 2]
        assert [item.body_markdown for item in revisions] == ["v1", "v2"]

    def test_attach_file_metadata_stores_payload_and_returns_id(self):
        environment_id = self.repository.ensure_execution_environment(
            mode="paper",
            account_scope="kite:paper-attach",
            paper_account_key="kite:paper-attach",
        )

        attachment_id = self.repository.attach_file_metadata(
            environment_id=environment_id,
            subject_type="note",
            subject_id="note-1",
            storage_key="attachments/n1.png",
            mime_type="image/png",
            sha256="abc",
            size_bytes=123,
            ocr_text="chart",
            metadata={"label": "entry"},
        )

        assert isinstance(attachment_id, str)
        assert len(attachment_id) == 36
        assert len(self.session.attachments) == 1
        row = self.session.attachments[0]
        assert row["storage_key"] == "attachments/n1.png"
        assert row["mime_type"] == "image/png"
        assert json.loads(row["metadata_json"]) == {"label": "entry"}


if __name__ == "__main__":
    unittest.main()
