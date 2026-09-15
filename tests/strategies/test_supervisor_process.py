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
from dataclasses import replace
from pathlib import Path

import pytest

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
        reused = replace(identity, start_time="0")
        assert proc.identity_is_current(reused) is False
        # terminate_identity refuses to signal a process it cannot prove it owns.
        assert proc.terminate_identity(reused, 0.5) == "identity_lost"
    finally:
        handle.stop(2.0)


def _wait_for_log(path: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = Path(path).read_text()
        except FileNotFoundError:
            text = ""
        if text.strip():
            return text
        time.sleep(0.05)
    return ""


def test_leader_exit_with_surviving_descendant_is_cleaned(tmp_path):
    """Leader exit is not proof the group emptied: descendants must be reaped."""
    script = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "sys.exit(0)\n"  # leader exits immediately, grandchild keeps running
    )
    handle = _spawn(script, tmp_path)
    grandchild_pid = int(_wait_for_log(handle.log_path).split()[0])
    assert handle.wait(timeout=10) == 0  # leader is gone
    assert Path(f"/proc/{grandchild_pid}").exists()
    result = handle.stop(2.0)
    assert result in {"group_terminated", "group_killed"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and Path(f"/proc/{grandchild_pid}").exists():
        time.sleep(0.05)
    assert not Path(f"/proc/{grandchild_pid}").exists()


def test_group_finalize_without_session_is_unresolved_and_signals_nothing(tmp_path):
    """No safe attribution ⇒ do not signal; report unresolved instead."""
    handle = _spawn("import time; time.sleep(30)", tmp_path)
    try:
        # Drop the session id, simulating a record from an older/uncertain source.
        unattributable = replace(handle.identity, session_id=None, pid=-1, pgid=handle.identity.pgid)
        assert proc.group_members(unattributable) == []
        assert proc.finalize_group(unattributable, 0.2) == "unresolved"
        assert handle.poll() is None  # nothing was signalled
    finally:
        handle.stop(2.0)


@pytest.mark.skipif(os.getuid() != 0, reason="requires root to drop to a different uid/gid")
def test_child_identity_reads_source_writes_scratch_cannot_alter_source(tmp_path):
    """Real cross-UID check (root only): source read-only, state inaccessible."""
    supervisor_dir = tmp_path / "ws"
    source_dir = supervisor_dir / "source" / "j"
    attempts_dir = supervisor_dir / "attempts"
    scratch = tmp_path / "scratch"
    source_dir.mkdir(parents=True)
    attempts_dir.mkdir(parents=True)
    scratch.mkdir()
    os.chmod(supervisor_dir, 0o711)
    os.chmod(source_dir, 0o755)
    os.chmod(attempts_dir, 0o700)
    source_file = source_dir / "v.py"
    source_file.write_text("print('hi')\n")
    os.chmod(source_file, 0o444)
    (attempts_dir / "j.json").write_text('{"secret": true}')
    os.chmod(attempts_dir / "j.json", 0o600)
    os.chown(scratch, 65534, 65534)
    os.chmod(scratch, 0o700)

    script = (
        "import sys\n"
        f"src = {str(source_file)!r}\n"
        f"scratch = {str(scratch)!r}\n"
        f"attempt = {str(attempts_dir / 'j.json')!r}\n"
        "read_ok = open(src).read().strip() == 'hi'\n"
        "write_ok = True\n"
        "try:\n"
        "    open(src, 'a').write('x')\n"
        "except OSError:\n"
        "    write_ok = False\n"
        "state_ok = True\n"
        "try:\n"
        "    open(attempt).read()\n"
        "except OSError:\n"
        "    state_ok = False\n"
        "scratch_ok = True\n"
        "try:\n"
        "    open(scratch + '/work.txt', 'w').write('y')\n"
        "except OSError:\n"
        "    scratch_ok = False\n"
        "print(int(read_ok), int(write_ok), int(state_ok), int(scratch_ok))\n"
    )
    handle = proc.spawn_child(
        [PY, "-c", script],
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(scratch)},
        cwd=str(scratch),
        log_path=str(tmp_path / "child.log"),
        max_log_bytes=8192,
        uid=65534,
        gid=65534,
        rlimits=[],
    )
    assert handle.wait(timeout=15) == 0
    handle._join_reader()
    values = _wait_for_log(handle.log_path).split()
    read_ok, write_ok, state_ok, scratch_ok = (int(v) for v in values)
    assert read_ok == 1       # source is readable
    assert write_ok == 0      # source is not writable
    assert state_ok == 0      # supervisor attempt records are inaccessible
    assert scratch_ok == 1    # scratch is usable
