"""Tests for the pipeline ops wiring (notify + backup step).

``cmd_pipeline`` records per-step results and pushes a summary through
``core.notify`` when steps fail (opt-in success brief) — a notification
problem must never change the pipeline's own outcome, so the helper is
best-effort by contract.  These tests pin that contract and the
``run_monitor.py backup`` path wiring without touching the real database.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import config
from run_monitor import _notify_pipeline_results, cmd_backup, cmd_pipeline

ROOT = Path(__file__).resolve().parent.parent


# ── notify wiring ──────────────────────────────────────────────────────────

def _patch_channels(monkeypatch, channels, send_calls=None):
    import core.notify as notify

    monkeypatch.setattr(notify, "configured_channels", lambda: channels)

    def fake_send(title, text, level="error", timeout=10.0):
        if send_calls is not None:
            send_calls.append({"title": title, "text": text, "level": level})
        return {c: "ok" for c in channels}

    monkeypatch.setattr(notify, "send_notification", fake_send)


def test_failure_results_alert_with_error_level(monkeypatch):
    calls = []
    _patch_channels(monkeypatch, ["wework"], calls)
    results = [{"step": "Crawl", "ok": False, "seconds": 1.0},
               {"step": "Report", "ok": True, "seconds": 2.0}]
    _notify_pipeline_results(results, level="error")
    assert len(calls) == 1
    assert calls[0]["level"] == "error"
    assert "失败" in calls[0]["title"]
    assert "Crawl" in calls[0]["text"]


def test_success_brief_uses_info_level(monkeypatch):
    calls = []
    _patch_channels(monkeypatch, ["webhook"], calls)
    results = [{"step": "Report", "ok": True, "seconds": 2.0}]
    _notify_pipeline_results(results, level="info")
    assert calls[0]["level"] == "info"
    assert "成功" in calls[0]["title"]


def test_no_channels_configured_is_a_printed_noop(monkeypatch, capsys):
    calls = []
    _patch_channels(monkeypatch, [], calls)
    _notify_pipeline_results([{"step": "Crawl", "ok": False, "seconds": 1.0}])
    out = capsys.readouterr().out
    assert "no notify channels" in out
    assert calls == []


def test_notify_failure_never_raises(monkeypatch, capsys):
    import core.notify as notify

    monkeypatch.setattr(notify, "configured_channels",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # must not raise even if the notify module itself misbehaves
    _notify_pipeline_results([])
    assert "notification skipped" in capsys.readouterr().out


# ── backup wiring ──────────────────────────────────────────────────────────

def test_cmd_backup_creates_backup_in_out_dir(test_db, tmp_path, monkeypatch, capsys):
    src = test_db
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(config.CONFIG.db, "path", src)
    out_dir = tmp_path / "backs"
    args = argparse.Namespace(out=str(out_dir), keep=3, label="db")
    cmd_backup(args)

    backups = list(out_dir.glob("db_*.db"))
    assert len(backups) == 1
    check = sqlite3.connect(backups[0])
    assert check.execute("SELECT x FROM t").fetchone()[0] == 42
    assert "Backup written" in capsys.readouterr().out


def test_cmd_backup_default_out_uses_config_dir(test_db, tmp_path, monkeypatch):
    src = test_db
    monkeypatch.setattr(config.CONFIG.db, "path", src)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "default_backs")

    args = argparse.Namespace(out=None, keep=1, label="db")
    cmd_backup(args)
    assert len(list((tmp_path / "default_backs").glob("db_*.db"))) == 1


# ── parser surface ─────────────────────────────────────────────────────────

def test_backup_help_mentions_flags():
    r = subprocess.run(
        [sys.executable, "-m", "run_monitor", "backup", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    for flag in ("--out", "--keep", "--label"):
        assert flag in r.stdout


def test_pipeline_help_mentions_new_steps():
    r = subprocess.run(
        [sys.executable, "-m", "run_monitor", "pipeline", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    for flag in ("--skip-quality", "--skip-backup", "--notify-success"):
        assert flag in r.stdout


def test_single_profile_pipeline_includes_watch_keywords(monkeypatch):
    """Default single-profile runs must still crawl user subscriptions."""
    calls = []

    def fake_run(cmd, env=None):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("run_monitor._watch_keywords",
                        lambda: ["rare biomarker"])
    args = argparse.Namespace(
        profiles=None, profile="mi", source="clinicaltrials_gov",
        skip_crawl=False, skip_quality=True, skip_backup=True,
        skip_digest=True, notify_success=False, english=True,
        chinese=False, full=False,
    )

    cmd_pipeline(args)

    crawl = calls[0]
    assert "--query-cond" in crawl
    query = crawl[crawl.index("--query-cond") + 1]
    assert config.DISEASE_PROFILES["mi"]["query_cond"] in query
    assert "rare biomarker" in query
