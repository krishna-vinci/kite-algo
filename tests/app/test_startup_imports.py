"""Heavy libraries only rare paths need must not load at app import."""
import os
import subprocess
import sys

# Other tests may point DB URLs at SQLite drivers that are not installed; the
# import check must not depend on test order, so the child gets a clean DB env.
_DB_ENV = ("DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URL", "ASYNC_DATABASE_URL")


def test_app_import_does_not_load_pandas_mibian_or_scipy_stats():
    code = (
        "import sys; import backend.app.bootstrap, backend.api.routers; "
        "print(','.join(m for m in ('pandas','mibian','scipy.stats') if m in sys.modules))"
    )
    env = {k: v for k, v in os.environ.items() if k not in _DB_ENV}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=180, env=env
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", out.stdout
