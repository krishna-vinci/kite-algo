"""The backup script dumps, verifies and records success (disposable PG only)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN = "postgresql://postgres:testonly@127.0.0.1:15433/postgres"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "pg_backup.sh"


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_one_backup_is_written_and_verified(tmp_path):
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT="15433", PGUSER="postgres",
               PGPASSWORD="testonly", PGDATABASE="postgres", BACKUP_DIR=str(tmp_path),
               BACKUP_ONCE="1", NTFY_URL="")
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    dumps = list(tmp_path.glob("kite-*.dump"))
    assert len(dumps) == 1 and dumps[0].stat().st_size > 1024
    assert (tmp_path / "LAST_SUCCESS").read_text().strip().endswith(dumps[0].name)


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_a_failed_dump_leaves_no_dump_and_fails(tmp_path):
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT="1", PGUSER="postgres", PGPASSWORD="x",
               PGDATABASE="postgres", BACKUP_DIR=str(tmp_path), BACKUP_ONCE="1", NTFY_URL="")
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert list(tmp_path.glob("kite-*.dump")) == []
