import os
import sys
import time
import tempfile
import psutil
import pytest
from pathlib import Path
from core.process_runner import ProcessTreeRunner

def test_process_tree_kill_on_timeout():
    """
    Validates Process Tree Kill & Hang Protection:
    1. Parent process spawns a child process.
    2. Child sleeps for 60s. Parent sleeps for 60s.
    3. Timeout is set to 3.0s.
    4. Orchestrator ProcessTreeRunner kills entire process tree with taskkill /PID <pid> /T /F.
    5. Success criteria:
       - elapsed < 10 seconds
       - parent_alive == False
       - child_alive == False
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        parent_script = Path(tmpdir) / "parent_worker.py"
        parent_script.write_text(
            "import sys, time, subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], shell=False)\n"
            "print(f'CHILD_PID:{child.pid}', flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8"
        )

        start_time = time.time()

        # Run with timeout of 3 seconds
        result = ProcessTreeRunner.run(
            argv=[sys.executable, str(parent_script)],
            timeout=3.0,
            heartbeat_interval=1.0 # fast heartbeat for testing
        )

        elapsed = time.time() - start_time
        parent_pid = result.pid

        # Extract child PID from captured stdout
        child_pid = None
        for line in result.stdout.splitlines():
            if line.startswith("CHILD_PID:"):
                child_pid = int(line.split(":")[1].strip())
                break

        assert child_pid is not None, f"Child PID must be captured from stdout, got: {result.stdout}"

        # Check if parent and child are alive
        time.sleep(0.5) # Give OS a moment to finish cleanup
        parent_alive = psutil.pid_exists(parent_pid)
        child_alive = psutil.pid_exists(child_pid)

        # Verification of strict success criteria
        assert result.timed_out is True, "Process must be flagged as timed_out"
        assert elapsed < 10.0, f"Total test time must be under 10 seconds, got {elapsed:.2f}s"
        assert parent_alive is False, f"Parent PID {parent_pid} must be terminated (alive={parent_alive})"
        assert child_alive is False, f"Child PID {child_pid} must be terminated (alive={child_alive})"
