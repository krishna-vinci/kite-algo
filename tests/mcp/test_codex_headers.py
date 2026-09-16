import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("mcp_codex_headers", Path(__file__).resolve().parents[2] / "scripts/mcp_codex_headers.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def test_only_client_secret_is_returned(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KITE_MCP_HTTP_TOKEN=" + "a" * 40 + "\nKITE_MCP_WORKER_TOKEN=not-for-codex\n")
    assert helper.headers(env) == {"Authorization": "Bearer " + "a" * 40}


def test_client_secret_file(tmp_path):
    (tmp_path / "client-token").write_text("b" * 40)
    env = tmp_path / ".env"
    env.write_text("KITE_MCP_HTTP_TOKEN_FILE=client-token\n")
    assert helper.headers(env) == {"Authorization": "Bearer " + "b" * 40}


def test_missing_or_ambiguous_credentials_fail(tmp_path):
    env = tmp_path / ".env"
    for text in ("", "KITE_MCP_HTTP_TOKEN=short\n", "KITE_MCP_HTTP_TOKEN=" + "a" * 40 + "\nKITE_MCP_HTTP_TOKEN_FILE=other\n"):
        env.write_text(text)
        with pytest.raises(ValueError):
            helper.headers(env)
