import re
from pathlib import Path

import pytest
from datetime import datetime, timezone

from pydantic import ValidationError

from journaling.service import JournalService
from journaling.models import (
    JournalAttachment,
    JournalNote,
    JournalNoteRevision,
    JournalNoteType,
    JournalTimelineEvent,
)
from journaling.v2.notes import (
    NOTE_TEMPLATE_ADJUSTMENT,
    NOTE_TEMPLATE_EXIT_REVIEW,
    NOTE_TEMPLATE_EXPERIMENT,
    NOTE_TEMPLATE_LESSON,
    NOTE_TEMPLATE_PSYCHOLOGY,
    NOTE_TEMPLATE_RISK_PLAN,
    NOTE_TEMPLATE_THESIS,
    markdown_to_search_text,
)


ENV_ID = "00000000-0000-4000-8000-000000000001"
ENV_B_ID = "00000000-0000-4000-8000-000000000002"
EPISODE_ID = "00000000-0000-4000-8000-000000000101"
NOTE_ID = "00000000-0000-4000-8000-000000000201"


SCHEMA_SQL = Path("schema.sql").read_text()


def _assert_has(pattern: str) -> None:
    assert re.search(pattern, SCHEMA_SQL, flags=re.IGNORECASE | re.DOTALL), pattern


def test_v2_notes_required_table_names_exist() -> None:
    required_tables = [
        "journal_timeline_events",
        "journal_notes",
        "journal_note_revisions",
        "journal_attachments",
    ]
    for table_name in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS public.{table_name}" in SCHEMA_SQL


def test_notes_schema_includes_markdown_canonical_fields() -> None:
    _assert_has(r"journal_notes\s*\(.*?body_markdown\s+TEXT\s+NOT\s+NULL")
    _assert_has(r"journal_notes\s*\(.*?body_text\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+''")
    _assert_has(r"journal_notes\s*\(.*?body_json\s+JSONB")
    _assert_has(r"journal_note_revisions\s*\(.*?body_markdown\s+TEXT\s+NOT\s+NULL")
    _assert_has(r"journal_note_revisions\s*\(.*?body_text\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+''")


def test_notes_timeline_revisions_and_attachments_indexes_exist() -> None:
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_notes_environment_subject\s+"
        r"ON\s+public\.journal_notes\s*\(\s*environment_id\s*,\s*subject_type\s*,\s*subject_id\s*,\s*updated_at\s+DESC\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_notes_episode_updated\s+"
        r"ON\s+public\.journal_notes\s*\(\s*episode_id\s*,\s*updated_at\s+DESC\s*\)\s*"
        r"WHERE\s+episode_id\s+IS\s+NOT\s+NULL"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_timeline_episode_time\s+"
        r"ON\s+public\.journal_timeline_events\s*\(\s*episode_id\s*,\s*occurred_at\s+ASC\s*\)\s*"
        r"WHERE\s+episode_id\s+IS\s+NOT\s+NULL"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_timeline_environment_subject_time\s+"
        r"ON\s+public\.journal_timeline_events\s*\(\s*environment_id\s*,\s*subject_type\s*,\s*subject_id\s*,\s*occurred_at\s+ASC\s*\)"
    )
    _assert_has(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+ux_journal_note_revisions_note_revision\s+"
        r"ON\s+public\.journal_note_revisions\s*\(\s*note_id\s*,\s*revision_no\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_attachments_environment_subject_created\s+"
        r"ON\s+public\.journal_attachments\s*\(\s*environment_id\s*,\s*subject_type\s*,\s*subject_id\s*,\s*created_at\s+DESC\s*\)"
    )
    _assert_has(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_journal_attachments_note_created\s+"
        r"ON\s+public\.journal_attachments\s*\(\s*note_id\s*,\s*created_at\s+DESC\s*\)\s*"
        r"WHERE\s+note_id\s+IS\s+NOT\s+NULL"
    )


def test_journal_note_accepts_markdown_and_tags() -> None:
    note = JournalNote(
        environment_id=ENV_ID,
        subject_type="episode",
        subject_id=EPISODE_ID,
        note_type=JournalNoteType.THESIS,
        title="Breakout thesis",
        body_markdown="## Setup\n- Strong breakout",
        tags=["breakout", "nifty"],
    )

    assert note.body_markdown.startswith("## Setup")
    assert note.tags == ["breakout", "nifty"]


def test_journal_note_rejects_blank_title_or_blank_markdown() -> None:
    with pytest.raises(ValidationError):
        JournalNote(
            environment_id=ENV_ID,
            subject_type="episode",
            subject_id=EPISODE_ID,
            note_type=JournalNoteType.THESIS,
            title="   ",
            body_markdown="valid",
        )

    with pytest.raises(ValidationError):
        JournalNote(
            environment_id=ENV_ID,
            subject_type="episode",
            subject_id=EPISODE_ID,
            note_type=JournalNoteType.THESIS,
            title="Valid title",
            body_markdown="   ",
        )


def test_journal_timeline_event_requires_nonblank_subject_and_event_fields() -> None:
    with pytest.raises(ValidationError):
        JournalTimelineEvent(
            environment_id="env-1",
            subject_type="   ",
            subject_id="subject-1",
            event_type="episode_opened",
        )

    with pytest.raises(ValidationError):
        JournalTimelineEvent(
            environment_id="env-1",
            subject_type="episode",
            subject_id="   ",
            event_type="episode_opened",
        )

    with pytest.raises(ValidationError):
        JournalTimelineEvent(
            environment_id="env-1",
            subject_type="episode",
            subject_id="subject-1",
            event_type="   ",
        )


def test_journal_note_revision_requires_revision_no_at_least_one() -> None:
    with pytest.raises(ValidationError):
        JournalNoteRevision(
            note_id=NOTE_ID,
            revision_no=0,
            body_markdown="Updated body",
        )


def test_journal_attachment_rejects_blank_storage_key_or_mime_type() -> None:
    with pytest.raises(ValidationError):
        JournalAttachment(
            environment_id=ENV_ID,
            subject_type="note",
            subject_id=NOTE_ID,
            storage_key="   ",
            mime_type="image/png",
        )

    with pytest.raises(ValidationError):
        JournalAttachment(
            environment_id=ENV_ID,
            subject_type="note",
            subject_id=NOTE_ID,
            storage_key="attachments/abc.png",
            mime_type="   ",
        )


def test_markdown_to_search_text_preserves_readable_content() -> None:
    markdown = """
# Heading
**Bold** and _italic_ with [docs link](https://example.com).
![Chart Alt](https://cdn/x.png)
- item one
1. item two
> quoted line
`inline code`
"""
    text = markdown_to_search_text(markdown)

    assert "Heading" in text
    assert "Bold" in text
    assert "docs link" in text
    assert "Chart Alt" in text
    assert "item one" in text
    assert "item two" in text
    assert "quoted line" in text
    assert "inline code" in text
    assert "https://example.com" not in text


def test_note_templates_are_non_empty_markdown() -> None:
    templates = [
        NOTE_TEMPLATE_THESIS,
        NOTE_TEMPLATE_RISK_PLAN,
        NOTE_TEMPLATE_ADJUSTMENT,
        NOTE_TEMPLATE_EXIT_REVIEW,
        NOTE_TEMPLATE_LESSON,
        NOTE_TEMPLATE_PSYCHOLOGY,
        NOTE_TEMPLATE_EXPERIMENT,
    ]
    for item in templates:
        assert isinstance(item, str)
        assert item.strip()
        assert "#" in item


class _FakeServiceNoteRepository:
    def __init__(self) -> None:
        self._envs = {ENV_ID: {"id": ENV_ID}, ENV_B_ID: {"id": ENV_B_ID}}
        self._episodes = {EPISODE_ID: {"id": EPISODE_ID, "environment_id": ENV_ID, "execution_context_id": None}}
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self._notes: dict[str, dict] = {}
        self._timeline: list[JournalTimelineEvent] = []
        self._seq = 0

    def get_execution_environment(self, environment_id: str):
        return self._envs.get(environment_id)

    def get_episode_detail(self, episode_id: str):
        payload = self._episodes.get(episode_id)
        if payload is None:
            return None

        class _Episode:
            def __init__(self, data: dict) -> None:
                self.id = data["id"]
                self.environment_id = data["environment_id"]
                self.execution_context_id = data.get("execution_context_id")

        return _Episode(payload)

    def get_execution_context(self, _context_id: str):
        return None

    def create_note(self, **kwargs):
        self._seq += 1
        note_id = f"00000000-0000-4000-8000-{200 + self._seq:012d}"
        payload = dict(kwargs)
        payload["id"] = note_id
        self.create_calls.append(payload)
        self._notes[note_id] = {
            "id": note_id,
            "environment_id": kwargs["environment_id"],
            "subject_type": kwargs["subject_type"],
            "subject_id": kwargs["subject_id"],
            "episode_id": kwargs.get("episode_id"),
            "note_type": kwargs["note_type"],
            "title": kwargs["title"],
            "body_markdown": kwargs["body_markdown"],
            "body_text": kwargs.get("body_text") or "",
            "body_json": kwargs.get("body_json"),
            "effective_at": kwargs.get("effective_at"),
            "author_id": kwargs.get("author_id"),
            "tags": kwargs.get("tags") or [],
            "metadata": kwargs.get("metadata") or {},
        }
        return note_id

    def append_timeline_event(self, event: JournalTimelineEvent):
        event_id = f"evt-{len(self._timeline) + 1}"
        payload = event.model_dump(mode="python")
        payload["id"] = event_id
        self._timeline.append(JournalTimelineEvent(**payload))
        return event_id

    def list_timeline_events(self, **kwargs):
        items = list(self._timeline)
        if kwargs.get("episode_id") is not None:
            items = [item for item in items if item.episode_id == kwargs["episode_id"]]
        items.sort(key=lambda item: (item.occurred_at, item.id or ""))
        offset = int(kwargs.get("offset") or 0)
        limit = int(kwargs.get("limit") or 200)
        return items[offset : offset + limit]

    def update_note(self, note_id: str, **kwargs):
        self.update_calls.append({"note_id": note_id, **dict(kwargs)})

    def get_note(self, note_id: str):
        payload = self._notes.get(note_id)
        return JournalNote(**payload) if payload else None

    def list_notes(self, environment_id: str, **kwargs):
        items = [item for item in self._notes.values() if item["environment_id"] == environment_id]
        if kwargs.get("subject_type") is not None:
            items = [item for item in items if item["subject_type"] == kwargs["subject_type"]]
        if kwargs.get("subject_id") is not None:
            items = [item for item in items if item["subject_id"] == kwargs["subject_id"]]
        return [JournalNote(**item) for item in items]

    def list_note_revisions(self, note_id: str):
        return [
            JournalNoteRevision(
                note_id=note_id,
                revision_no=1,
                body_markdown="old",
                body_text="old",
            )
        ]

    def attach_file_metadata(self, **kwargs):
        return "attachment-1"


def test_service_create_note_computes_body_text() -> None:
    repo = _FakeServiceNoteRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    note_id = service.create_v2_note(
        environment_id=ENV_ID,
        subject_type="episode",
        subject_id="ep-1",
        note_type="thesis",
        title="Plan",
        body_markdown="# Idea\n- alpha",
        tags=["alpha"],
        metadata={"source": "svc"},
    )

    assert note_id == NOTE_ID
    assert repo.create_calls
    create_payload = repo.create_calls[-1]
    assert create_payload["body_text"] == "Idea alpha"
    assert create_payload["body_markdown"].startswith("# Idea")
    assert create_payload["tags"] == ["alpha"]
    assert create_payload["metadata"] == {"source": "svc"}


def test_service_update_note_computes_body_text() -> None:
    repo = _FakeServiceNoteRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]
    note_id = service.create_v2_note(
        environment_id=ENV_ID,
        subject_type="episode",
        subject_id="ep-1",
        note_type="thesis",
        title="Plan",
        body_markdown="old",
    )

    service.update_v2_note(note_id, body_markdown="## New\n- beta", metadata={"x": 1})

    assert repo.update_calls
    update_payload = repo.update_calls[-1]
    assert update_payload["note_id"] == note_id
    assert update_payload["body_text"] == "New beta"
    assert update_payload["metadata"] == {"x": 1}


def test_service_create_note_rejects_cross_environment_episode() -> None:
    repo = _FakeServiceNoteRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="episode_id does not belong"):
        service.create_v2_note(
            environment_id=ENV_B_ID,
            subject_type="episode",
            subject_id=EPISODE_ID,
            episode_id=EPISODE_ID,
            note_type="thesis",
            title="Plan",
            body_markdown="body",
        )


def test_service_attachment_rejects_cross_environment_note() -> None:
    repo = _FakeServiceNoteRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]
    note_id = service.create_v2_note(
        environment_id=ENV_ID,
        subject_type="strategy_template",
        subject_id="template-a",
        note_type="lesson",
        title="Lesson",
        body_markdown="body",
    )

    with pytest.raises(ValueError, match="note_id does not belong"):
        service.attach_v2_file_metadata(
            environment_id=ENV_B_ID,
            subject_type="note",
            subject_id=note_id,
            note_id=note_id,
            storage_key="attachments/note.png",
            mime_type="image/png",
        )


def test_timeline_events_are_ordered_by_occurred_at_then_event_id() -> None:
    repo = _FakeServiceNoteRepository()
    service = JournalService(repository=repo)  # type: ignore[arg-type]

    at = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    service.append_timeline_event(
        environment_id=ENV_ID,
        episode_id=EPISODE_ID,
        subject_type="episode",
        subject_id=EPISODE_ID,
        event_type="episode_opened",
        occurred_at=at,
    )
    service.append_timeline_event(
        environment_id=ENV_ID,
        episode_id=EPISODE_ID,
        subject_type="episode",
        subject_id=EPISODE_ID,
        event_type="intent_created",
        occurred_at=at,
    )
    service.append_timeline_event(
        environment_id=ENV_ID,
        episode_id=EPISODE_ID,
        subject_type="episode",
        subject_id=EPISODE_ID,
        event_type="note_created",
        occurred_at=datetime(2026, 5, 1, 10, 1, tzinfo=timezone.utc),
    )

    events = service.list_v2_timeline(episode_id=EPISODE_ID, environment_id=ENV_ID)
    assert [item["event_type"] for item in events] == ["episode_opened", "intent_created", "note_created"]
