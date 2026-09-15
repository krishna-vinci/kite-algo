"""Process containment tests with harmless local child programs.

No strategy source, no orders, no network. These exercise the supervisor's
process manager directly: completion, crash, hang + termination, bounded logs,
and identity handling (PID-reuse rejection).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from backend.strategies import supervisor_process as proc

PY = sys.executable


def _spawn(script: str, tmp_path: Path, *, max_log_bytes: int = 64 * 1024):
    log_path = tmp_path / "child.log"
    return proc.spawn_child(
        [PY, "-c", script],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"},
        cwd=str(tmp_path),
        log_path=str(log_path),
        max_log_bytes=max_log_bytes,
        rlimits=[],
    )


def test_child_runs_in_its_own_session_and_completes(tmp_path):
    handle = _spawn("import sys; sys.exit(0)", tmp_path)
    assert os.getpgid(handle.pid) == handle.identity.pgid == handle.pid
    assert proc.identity_is_current(handle.identity) is True
    assert handle.wait(timeout=10) == 0
    assert handle.stop(1.0) == "exited"
    assert proc.identity_is_current(handle.identity) is False


def test_child_crash_reports_exit_code(tmp_path):
    handle = _spawn("import sys; sys.exit(3)", tmp_path)
    assert handle.wait(timeout=10) == 3


def test_hung_child_is_terminated_by_process_group(tmp_path):
    # The child spawns a grandchild in the same process group; both must die.
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    handle = _spawn(script, tmp_path)
    # Wait for the grandchild pid to be logged.
    grandchild_pid = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and grandchild_pid is None:
        time.sleep(0.05)
        try:
            text = Path(handle.log_path).read_text()
        except FileNotFoundError:
            continue
        if text.strip():
            grandchild_pid = int(text.split()[0])
    assert grandchild_pid is not None
    result = handle.stop(2.0)
    assert result in {"terminated", "killed"}
    # Both the child and its grandchild are gone.
    assert handle.poll() is not None
    assert not Path(f"/proc/{grandchild_pid}").exists()


def test_bounded_log_caps_file_and_flags_overflow(tmp_path):
    script = "import sys\nfor _ in range(200):\n    sys.stdout.write('x' * 1024)\nsys.stdout.flush()\n"
    handle = _spawn(script, tmp_path, max_log_bytes=4096)
    assert handle.wait(timeout=15) == 0
    handle._join_reader()
    size = Path(handle.log_path).stat().st_size
    assert size <= 4096
    assert handle.log_overflow is True


def test_identity_rejects_pid_reuse(tmp_path):
    handle = _spawn("import time; time.sleep(30)", tmp_path)
    try:
        identity = handle.identity
        assert proc.identity_is_current(identity) is True
        # A fabricated identity with a wrong start time (PID reused) is not current.
        from dataclasses import replace

        reused = replace(identity, start_time="0")
        assert proc.identity_is_current(reused) is False
        # terminate_identity refuses to signal a process it cannot prove it owns.
        assert proc.terminate_identity(reused, 0.5) == "identity_lost"
    finally:
        handle.stop(2.0)
