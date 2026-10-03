"""Tests for the pipeline report-step language flags.

``run_monitor.py pipeline`` used to default to Chinese reports, which
silently triggers the LLM translation pass (needs DEEPSEEK_API_KEY, else
a degraded dictionary fallback) — wrong default for a daily automation.
``_report_step_flags`` encodes the default-to-English rule; these tests
pin it down so the default cannot silently regress.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from run_monitor import _report_step_flags

ROOT = Path(__file__).resolve().parent.parent


def test_explicit_english_yields_english():
    assert _report_step_flags(english=True, chinese=False) == ["--english"]


def test_default_is_english():
    # Neither flag given -> default-to-English rule kicks in.
    assert _report_step_flags(english=False, chinese=False) == ["--english"]


def test_chinese_flag_yields_chinese():
    assert _report_step_flags(english=False, chinese=True) == []


def test_conflict_resolves_to_english():
    # Both flags passed is a usage conflict: English wins (cmd_pipeline
    # prints the warning about it).
    assert _report_step_flags(english=True, chinese=True) == ["--english"]


def test_pipeline_help_mentions_chinese():
    r = subprocess.run(
        [sys.executable, "-m", "run_monitor", "pipeline", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "--chinese" in r.stdout
