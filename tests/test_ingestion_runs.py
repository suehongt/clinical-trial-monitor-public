"""Offline contracts for Phase 3F's durable ingestion orchestration."""
from __future__ import annotations

from datetime import datetime, timezone

from config import CONFIG
from core.ingestion import registry_health, run_due_registry_ingestions, run_registry_ingestion
from db.connection import get_connection


class FakeCollector:
    """Deterministic collector seam: no network, no adapter duplication."""
    def __init__(self, source_id: int, result=None, error: Exception | None = None):
        self.source_id = source_id
        self.result = result or {"found": 1, "new": 1, "updated": 0, "skipped": 0, "changes": 0, "failed": 0, "status": "succeeded"}
        self.error = error
        self.calls = []

    def run(self, *, since=None, is_bootstrap=False):
        self.calls.append((since, is_bootstrap))
        if self.error:
            raise self.error
        return self.result


def _source_id() -> int:
    conn = get_connection()
    return conn.execute("SELECT source_id FROM registry_sources WHERE short_name='NCT'").fetchone()[0]


def test_run_lifecycle_cursor_failure_and_retry_safety(test_db):
    conn = get_connection(); sid = _source_id()
    conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES (?,?)", (sid, "2026-09-01 00:00:00")); conn.commit()
    failed = run_registry_ingestion("clinicaltrials_gov", collector=FakeCollector(sid, error=TimeoutError("offline")))
    assert failed["status"] == "failed"
    state = conn.execute("SELECT last_successful_sync,consecutive_failures FROM sync_status WHERE source_id=?", (sid,)).fetchone()
    assert state["last_successful_sync"] == "2026-09-01 00:00:00" and state["consecutive_failures"] == 1

    fake = FakeCollector(sid)
    recovered = run_registry_ingestion("clinicaltrials_gov", trigger="retry", retry_of_run_id=failed["run_id"], collector=fake,
                                       now=datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert recovered["status"] == "succeeded" and fake.calls == [("2026-09-01 00:00:00", False)]
    row = conn.execute("SELECT status,retry_of_run_id,cursor_before,cursor_after FROM registry_ingestion_runs WHERE run_id=?", (recovered["run_id"],)).fetchone()
    assert row["status"] == "succeeded" and row["retry_of_run_id"] == failed["run_id"]
    assert row["cursor_before"] == "2026-09-01 00:00:00" and row["cursor_after"]


def test_explicit_full_run_ignores_previous_success_cursor(test_db):
    conn = get_connection(); sid = _source_id()
    conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES (?,?)",
                 (sid, "2026-09-01 00:00:00")); conn.commit()
    fake = FakeCollector(sid)
    result = run_registry_ingestion("clinicaltrials_gov", collector=fake, force_full=True)
    assert result["status"] == "succeeded"
    assert fake.calls == [(None, False)]
    row = conn.execute("SELECT cursor_before FROM registry_ingestion_runs WHERE run_id=?",
                       (result["run_id"],)).fetchone()
    assert row["cursor_before"] == "2026-09-01 00:00:00"


def test_unexpected_empty_response_is_failed_without_erasing_data(test_db):
    from tests.test_server_api import _insert_record
    conn = get_connection(); sid = _source_id()
    _insert_record(conn, "NCT", "NCTEMPTY001", "Existing trial")
    conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES (?,?)", (sid, "2026-09-01 00:00:00")); conn.commit()
    result = run_registry_ingestion("clinicaltrials_gov", collector=FakeCollector(sid, {"found": 0, "status": "succeeded"}))
    assert result["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM registry_records WHERE source_trial_id='NCTEMPTY001' AND is_latest=1").fetchone()[0] == 1
    assert conn.execute("SELECT last_successful_sync FROM sync_status WHERE source_id=?", (sid,)).fetchone()[0] == "2026-09-01 00:00:00"


def test_registry_health_unknown_disabled_and_recovery(test_db):
    conn = get_connection(); sid = _source_id()
    conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES (?,NULL)", (sid,)); conn.commit()
    health = {r["short_name"]: r for r in registry_health(datetime(2026, 9, 10, tzinfo=timezone.utc))}
    assert health["NCT"]["freshness"] == "disabled"
    conn.execute("UPDATE registry_sources SET enabled=1 WHERE source_id=?", (sid,))
    conn.execute("UPDATE sync_status SET last_successful_sync='2026-09-10 00:00:00' WHERE source_id=?", (sid,)); conn.commit()
    assert {r["short_name"]: r for r in registry_health(datetime(2026, 9, 11, tzinfo=timezone.utc))}["NCT"]["freshness"] == "fresh"


def test_due_scheduler_uses_one_occurrence_and_does_not_consume_manual_schedule(test_db, monkeypatch):
    conn = get_connection(); sid = _source_id()
    monkeypatch.setattr(CONFIG.sources["clinicaltrials_gov"], "enabled", True)
    conn.execute("UPDATE registry_sources SET enabled=1 WHERE source_id=?", (sid,))
    conn.execute("INSERT INTO sync_status (source_id,next_sync_at) VALUES (?,NULL)", (sid,)); conn.commit()
    calls = []
    def fake_runner(registry, **kwargs):
        calls.append((registry, kwargs["trigger"], kwargs["scheduled_for"]))
        # The production runner advances next_sync_at after success; emulate
        # that durable effect here so a second tick is not due.
        conn.execute("UPDATE sync_status SET next_sync_at='2026-09-11 00:00:00' WHERE source_id=?", (sid,)); conn.commit()
        return {"status": "succeeded"}
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert len(run_due_registry_ingestions(now, runner=fake_runner)) == 1
    assert run_due_registry_ingestions(now, runner=fake_runner) == []
    assert calls == [("clinicaltrials_gov", "scheduled", "2026-09-10 00:00:00")]
