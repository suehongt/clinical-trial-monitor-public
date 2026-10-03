"""ct_report.paths — Shared filesystem locations for the report package."""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


DB_PATH = PROJECT_ROOT / "db" / "ct_monitor.db"


REPORT_DIR = PROJECT_ROOT / "reports"
