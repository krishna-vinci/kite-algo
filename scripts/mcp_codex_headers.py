"""Codex HTTP header helper. Stdout is secret-bearing: consumed only by Codex.

Reads only the MCP client credential, never the backend worker credential.
Keep credentials in the project's untracked .env instead of copying them into
Codex config or requiring the desktop process to inherit a shell variable.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from dotenv import dotenv_values


def headers(env_file: Path) -> dict[str, str]:
    settings = dotenv_values(env_file)
    token = (settings.get("KITE_MCP_HTTP_TOKEN") or "").strip()
    filename = (settings.get("KITE_MCP_HTTP_TOKEN_FILE") or "").strip()
    if token and filename:
        raise ValueError("Choose only one MCP client credential source")
    if filename:
        path = Path(filename)
        if not path.is_absolute():
            path = env_file.parent / path
        token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("MCP client credential is missing or invalid")
    return {"Authorization": "Bearer " + token}


def main() -> int:
    try:
        result = headers(Path(__file__).resolve().parents[1] / ".env")
    except (OSError, UnicodeError, ValueError):
        print("Cannot load MCP client credential from local configuration", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
