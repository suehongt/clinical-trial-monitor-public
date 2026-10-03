"""CLI entry-point smoke tests.

`python -m ct_report.cli` used to exit 0 with no output because cli.py
lacked an `if __name__ == "__main__"` guard — argparse never ran and
"regenerate the report" silently did nothing.  These subprocess tests
fail if the module ever becomes un-runnable again.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ct_report_cli_module_runs():
    r = subprocess.run(
        [sys.executable, "-m", "ct_report.cli", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    # argparse must actually have executed (a missing __main__ guard
    # exits 0 but prints nothing at all)
    assert "--english" in r.stdout
    assert "Traceback" not in r.stderr


def test_run_monitor_module_runs():
    r = subprocess.run(
        [sys.executable, "-m", "run_monitor", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "crawl" in r.stdout
