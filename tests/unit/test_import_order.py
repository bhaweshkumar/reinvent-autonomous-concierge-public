"""M6: each write-path module imports cleanly on its own in a fresh interpreter.

Guards against import cycles that only show up when a module is imported first.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULES = [
    "reinvent_concierge.booking",
    "reinvent_concierge.booking.changes",
    "reinvent_concierge.booking.service",
    "reinvent_concierge.booking.approvals",
    "reinvent_concierge.booking.reader",
    "reinvent_concierge.schedule",
    "reinvent_concierge.snapper.worker",
    "reinvent_concierge.planner",
    "reinvent_concierge.planner.application",
    "reinvent_concierge.runtime",
    "reinvent_concierge.app_context",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import {module}"],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
