import unittest

from backend.journaling.models import ProjectionState
from backend.journaling.repository import JournalRepository


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.closed = 0

    def execute(self, _statement, _params):
        return _FakeResult(self._row)

    def close(self):
        self.closed += 1


class _MappingRow(dict):
    pass


class JournalRepositoryProjectionStateTests(unittest.TestCase):
    def test_get_projection_state_supports_mapping_rows(self):
        row = _MappingRow(
            projector_name="journal-runtime-worker",
            cursor_json={"last_tick_at": "2026-04-15T08:00:00+00:00"},
            updated_at=None,
        )
        session = _FakeSession(row)
        repository = JournalRepository(session_factory=lambda: session)

        result = repository.get_projection_state("journal-runtime-worker")

        self.assertIsInstance(result, ProjectionState)
        self.assertEqual(result.projector_name, "journal-runtime-worker")
        self.assertEqual(result.cursor["last_tick_at"], "2026-04-15T08:00:00+00:00")
        self.assertEqual(session.closed, 1)


if __name__ == "__main__":
    unittest.main()
