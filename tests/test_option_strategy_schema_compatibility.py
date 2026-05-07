import unittest

from app.database import _ensure_option_strategy_runs_compatibility


class _FakeCursor:
    def __init__(self, statements):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement):
        self._statements.append(statement)


class _FakeConnection:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self.statements)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class OptionStrategySchemaCompatibilityTests(unittest.TestCase):
    def test_compatibility_helper_adds_algo_instance_id_column(self):
        conn = _FakeConnection()

        _ensure_option_strategy_runs_compatibility(conn)

        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(len(conn.statements), 1)
        self.assertIn("ALTER TABLE public.option_strategy_runs", conn.statements[0])
        self.assertIn("ADD COLUMN IF NOT EXISTS algo_instance_id TEXT", conn.statements[0])


if __name__ == "__main__":
    unittest.main()
