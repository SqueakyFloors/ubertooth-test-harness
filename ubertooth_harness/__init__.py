"""Local Ubertooth test-harness runner."""

import sys

if sys.version_info[:2] != (3, 12):
    sys.stderr.write(
        "error: " + sys.executable + " is Python " + sys.version.split()[0]
        + "; the harness needs its own Python 3.12 environment. Run "
        "'python3 bootstrap.py' and use .venv/bin/python -m ubertooth_harness\n"
    )
    raise SystemExit(2)

from ubertooth_harness.plan import FlashStep, Plan, PlanError, load_plan  # noqa: E402

__all__ = ["FlashStep", "Plan", "PlanError", "load_plan"]
