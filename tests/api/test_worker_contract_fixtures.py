import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "worker_api" / "v1"


def test_sanitized_v1_fixtures_have_a_stable_envelope():
    for path in sorted(FIXTURES.glob("*.json")):
        payload = json.loads(path.read_text())
        assert payload["schema_version"] == 1, path.name
        assert "source" in payload and "retrieved_at" in payload, path.name
