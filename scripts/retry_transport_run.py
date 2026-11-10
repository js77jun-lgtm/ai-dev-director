"""Retry a known CLI failure through the normal validation/review pipeline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from director import load_config
from core.orchestrator import Orchestrator

if __name__ == '__main__':
    orch = Orchestrator(load_config())
    run_id = sys.argv[1]
    if not orch.sm.reopen_transport_failure(run_id):
        raise SystemExit('Run is not a retryable CLI failure; unchanged.')
    print(orch.resume(run_id), flush=True)
