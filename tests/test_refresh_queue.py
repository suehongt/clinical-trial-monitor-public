"""Refresh queue tests: plan_refresh 入队口径 + refresh_pending 消费驱动
+ ChiCTR refresh_one 全链路（含变更事件补写）。

全部离线：ChiCTR 详情页用 spike fixture + monkeypatch 实例属性；
消费驱动的失败/熔断语义用最小 FakeCollector 替身覆盖。
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from collectors.chictr import ChiCTRCollector
from core.refresh import (
    REFRESH_MAX_ATTEMPTS,
    plan_refresh,
    refresh_pending,
    refresh_queue_stats,
)
from db.connection import get_connection

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CHICTR_DETAIL = os.path.join(_FIXTURE_DIR, "chictr_detail_standalone.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _sid(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _insert_record(conn, short_name: str, trial_id: str,
                   title: str = "t", stale_days: int = 40) -> int:
    """插入一条 is_latest=1、last_crawled_at 陈旧/新鲜的记录，返回 record_id。"""
    stale = (f"datetime('now', '-{stale_days} days')"
             if stale_days is not None else "datetime('now')")
    cur = conn.execute(
        f"INSERT INTO registry_records (source_id, source_trial_id, title, "
        f"last_crawled_at, first_crawled_at) VALUES (?, ?, ?, {stale}, "
        f"datetime('now', '-90 days'))",
        (_sid(conn, short_name), trial_id, title),
    )
    conn.commit()
    return cur.lastrowid


class FakeCollector:
    """refresh_pending 驱动器所需的最小采集器替身。"""

    def __init__(self, source_id: int, outcomes=None, error: Exception = None):
        self._source_id = source_id
        self.outcomes = list(outcomes or [])
        self.error = error
        self.calls: list[str] = []

        class _Cfg:
            extra = {"enrich_batch_size": 2}
            max_records_per_run = 0
            request_delay_sec = 0
            short_name = "Fake"

        self.cfg = _Cfg()

    @property
    def source_id(self) -> int:
        return self._source_id

    def refresh_one(self, trial_id: str) -> dict:
        self.calls.append(trial_id)
        if self.error is not None:
            raise self.error
        return self.outcomes.pop(0)


@pytest.fixture
def fake_env(test_db):
    conn = get_connection()
    sid = _sid(conn, "ChiCTR")
    return conn, sid


# ── plan_refresh（入队口径）─────────────────────────────────────────────


class TestPlanRefresh:
    def test_queues_stale_latest_records_only(self, fake_env):
        conn, sid = fake_env
        _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        _insert_record(conn, "ChiCTR", "ChiCTR2600131927", stale_days=1)
        # bootstrap 导入的中文记录从未详情增强过——复查队列正是为它们服务的
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, "
            "is_bootstrap, last_crawled_at, first_crawled_at) VALUES (?, ?, 1, "
            "datetime('now', '-60 days'), datetime('now', '-90 days'))",
            (sid, "ChiCTR2600131926"),
        )
        conn.commit()

        queued = plan_refresh()
        assert queued == 2
        ids = {r["source_trial_id"] for r in
               conn.execute("SELECT source_trial_id FROM refresh_queue")}
        assert ids == {"ChiCTR2600128415", "ChiCTR2600131926"}

    def test_ignores_non_chinese_sources(self, fake_env):
        conn, _sid = fake_env
        _insert_record(conn, "NCT", "NCT99999999", stale_days=40)
        conn.commit()
        assert plan_refresh() == 0

    def test_idempotent_no_duplicate_pending(self, fake_env):
        conn, sid = fake_env
        _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        conn.commit()
        assert plan_refresh() == 1
        # NOT EXISTS(pending)：已 pending 的行不重复入队
        assert plan_refresh() == 0
        rows = conn.execute("SELECT COUNT(*) AS n FROM refresh_queue").fetchone()
        assert rows["n"] == 1

    def test_batch_limit_and_stale_order(self, fake_env):
        conn, sid = fake_env
        _insert_record(conn, "ChiCTR", "ChiCTR2600131920", stale_days=10)
        _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=50)
        _insert_record(conn, "ChiCTR", "ChiCTR2600131927", stale_days=30)
        conn.commit()

        plan_refresh(batch=2, stale_days=25)  # 10d 的不够陈旧，其余两条入队
        rows = conn.execute(
            "SELECT q.source_trial_id, r.last_crawled_at FROM refresh_queue q "
            "JOIN registry_records r ON r.record_id = q.record_id "
            "ORDER BY r.last_crawled_at ASC").fetchall()
        assert [r["source_trial_id"] for r in rows] == [
            "ChiCTR2600128415",  # 最久未验证者优先
            "ChiCTR2600131927",
        ]


# ── refresh_pending（消费驱动）──────────────────────────────────────────


class TestRefreshPending:
    def test_budget_from_enrich_batch_size(self, fake_env):
        conn, sid = fake_env
        for i in range(4):
            rid = _insert_record(conn, "ChiCTR", f"ChiCTR260012841{i}", stale_days=40)
            conn.execute(
                "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
                "VALUES (?, ?, ?)", (sid, f"ChiCTR260012841{i}", rid))
        conn.commit()
        collector = FakeCollector(sid, outcomes=[
            {"action": "skipped", "changes": 0}] * 4)
        stats = refresh_pending(collector)
        assert stats["refreshed"] == 2  # enrich_batch_size=2
        assert len(collector.calls) == 2

    def test_done_marks_and_changes_accumulate(self, fake_env):
        conn, sid = fake_env
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        conn.execute(
            "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
            "VALUES (?, ?, ?)", (sid, "ChiCTR2600128415", rid))
        conn.commit()
        collector = FakeCollector(sid, outcomes=[
            {"action": "updated", "changes": 2}])
        stats = refresh_pending(collector)
        assert stats == {"refreshed": 1, "failed": 0, "skipped": 0, "changes": 2}
        row = conn.execute("SELECT state, done_at FROM refresh_queue").fetchone()
        assert row["state"] == "done"
        assert row["done_at"] is not None

    def test_stub_page_skipped_not_failed(self, fake_env):
        conn, sid = fake_env
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        conn.execute(
            "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
            "VALUES (?, ?, ?)", (sid, "ChiCTR2600128415", rid))
        conn.commit()
        collector = FakeCollector(sid, outcomes=[
            {"action": "skipped", "changes": 0, "stub": True}])
        stats = refresh_pending(collector)
        assert stats["skipped"] == 1
        row = conn.execute("SELECT state FROM refresh_queue").fetchone()
        assert row["state"] == "skipped"

    def test_failure_attempts_accumulate_to_failed(self, fake_env):
        conn, sid = fake_env
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        conn.execute(
            "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
            "VALUES (?, ?, ?)", (sid, "ChiCTR2600128415", rid))
        conn.commit()
        collector = FakeCollector(sid, error=RuntimeError("WAF blocked"))

        for round_no in range(1, REFRESH_MAX_ATTEMPTS):
            stats = refresh_pending(collector)
            assert stats["failed"] == 1
            row = conn.execute("SELECT state, attempts FROM refresh_queue").fetchone()
            assert row["state"] == "pending"
            assert row["attempts"] == round_no

        stats = refresh_pending(collector)
        assert stats["failed"] == 1
        row = conn.execute("SELECT state, attempts, last_error FROM refresh_queue").fetchone()
        assert row["state"] == "failed"
        assert row["attempts"] == REFRESH_MAX_ATTEMPTS
        assert "WAF" in row["last_error"]

    def test_waf_circuit_break_aborts_round(self, fake_env):
        from collectors.browser_base import WafCircuitOpenError

        conn, sid = fake_env
        for i, no in enumerate(("ChiCTR2600128415", "ChiCTR2600131927")):
            rid = _insert_record(conn, "ChiCTR", no, stale_days=40)
            conn.execute(
                "INSERT INTO refresh_queue (source_id, source_trial_id, record_id) "
                "VALUES (?, ?, ?)", (sid, no, rid))
        conn.commit()
        # 第 1 条熔断（refresh_one 内部抛出），第 2 条不应再被触碰
        collector = FakeCollector(
            sid, error=WafCircuitOpenError("circuit open", 3, 3))
        stats = refresh_pending(collector)
        assert len(collector.calls) == 1
        assert stats["refreshed"] == 0
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM refresh_queue WHERE state='pending'").fetchone()
        assert pending["n"] == 2  # 熔断行保留 pending，留待下轮

    def test_stats_summary(self, fake_env):
        conn, sid = fake_env
        rid = _insert_record(conn, "ChiCTR", "ChiCTR2600128415", stale_days=40)
        conn.execute(
            "INSERT INTO refresh_queue (source_id, source_trial_id, record_id, state) "
            "VALUES (?, ?, ?, 'done')", (sid, "ChiCTR2600128415", rid))
        conn.commit()
        stats = refresh_queue_stats()
        assert stats["by_source_state"] == {"ChiCTR:done": 1}
        assert stats["totals"] == {"done": 1}


# ── ChiCTR refresh_one 全链路（真实采集器 + 变更事件） ──────────────────


@pytest.fixture
def chictr(test_db, monkeypatch):
    collector = ChiCTRCollector()
    # monkeypatch, not a bare assignment: cfg is the process-wide CONFIG
    # singleton, and a leaked 0 here made tests/test_waf_pacing.py (which
    # asserts the docs/waf_limits.json pacing) fail whenever this file ran
    # first in the same session.
    monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
    return collector


def _fake_chictr_detail(proj: str, html: str):
    def fake_fetch(url, timeout_ms=25000):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert "showproj" in parsed.path
        if qs.get("proj", [""])[0] == proj:
            return html
        raise RuntimeError(f"unexpected detail url: {url}")
    return fake_fetch


class TestChiCTRRefreshOne:
    def test_refresh_detects_enrollment_change_and_emits_events(
            self, chictr, monkeypatch):
        """复查发现入组人数变化 → 新版本 + trial_events（修复的端到端验证）。"""
        conn = get_connection()
        proj = "291119"
        chictr_no = "ChiCTR2600128415"

        # 首次入库（enrich 同款链路）
        parsed = chictr.parse_detail_page(_read(CHICTR_DETAIL), chictr_no)
        raw = {"chictr_no": chictr_no, "proj_id": proj,
               "html": _read(CHICTR_DETAIL), "parsed_fields": parsed}
        first = chictr._upsert_and_emit_events(chictr.normalise(raw))
        assert first["action"] == "new"
        assert first["changes"] == 0
        old_enrollment = parsed.get("enrollment")

        # 详情键走游标辅助表
        chictr._save_cursor(
            "list_walk",
            {"recent_seen_ids": [chictr_no], "proj_ids": {chictr_no: proj}},
            success=True,
        )
        # 记录变陈旧 → 入复查队列
        conn.execute(
            "UPDATE registry_records SET last_crawled_at = "
            "datetime('now', '-40 days') WHERE source_trial_id = ?", (chictr_no,))
        conn.commit()
        assert plan_refresh() == 1

        # 重访：同一详情页但入组人数被申办方调高 → updated + 事件
        orig_parse = chictr.parse_detail_page

        def parse_with_new_enrollment(html, no):
            fields = orig_parse(html, no)
            fields["enrollment"] = (old_enrollment or 100) + 120
            return fields

        monkeypatch.setattr(chictr, "parse_detail_page", parse_with_new_enrollment)
        monkeypatch.setattr(chictr, "_fetch",
                            _fake_chictr_detail(proj, _read(CHICTR_DETAIL)))

        stats = refresh_pending(chictr)
        assert stats["refreshed"] == 1
        assert stats["changes"] >= 1

        row = conn.execute("SELECT state FROM refresh_queue").fetchone()
        assert row["state"] == "done"
        event = conn.execute(
            """SELECT old_value, new_value FROM trial_events
               WHERE field_name = 'enrollment'""").fetchone()
        assert event is not None
        assert int(event["old_value"]) == old_enrollment
        assert int(event["new_value"]) == (old_enrollment or 100) + 120
        versions = conn.execute(
            "SELECT COUNT(*) AS n FROM registry_records "
            "WHERE source_trial_id = ?", (chictr_no,)).fetchone()
        assert versions["n"] == 2  # 版本链推进，无噪声版本

    def test_refresh_unchanged_page_creates_no_version(self, chictr, monkeypatch):
        """hash-skip：未变详情页 → skipped、零版本、零事件、refresh done。"""
        conn = get_connection()
        proj, chictr_no = "291119", "ChiCTR2600128415"
        parsed = chictr.parse_detail_page(_read(CHICTR_DETAIL), chictr_no)
        raw = {"chictr_no": chictr_no, "proj_id": proj,
               "html": _read(CHICTR_DETAIL), "parsed_fields": parsed}
        chictr._upsert_and_emit_events(chictr.normalise(raw))
        chictr._save_cursor(
            "list_walk",
            {"recent_seen_ids": [], "proj_ids": {chictr_no: proj}},
            success=True,
        )
        conn.execute(
            "UPDATE registry_records SET last_crawled_at = "
            "datetime('now', '-40 days') WHERE source_trial_id = ?", (chictr_no,))
        conn.commit()

        monkeypatch.setattr(chictr, "_fetch",
                            _fake_chictr_detail(proj, _read(CHICTR_DETAIL)))
        assert plan_refresh() == 1
        stats = refresh_pending(chictr)
        assert stats["refreshed"] == 1
        assert stats["changes"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM trial_events").fetchone()["n"] == 0
        versions = conn.execute(
            "SELECT COUNT(*) AS n FROM registry_records "
            "WHERE source_trial_id = ?", (chictr_no,)).fetchone()
        assert versions["n"] == 1

    def test_refresh_stub_page_marks_skipped(self, chictr, monkeypatch):
        """重访遇到号段空穴桩页（记录被源端撤销）→ skipped 不计失败。"""
        conn = get_connection()
        proj, chictr_no = "291119", "ChiCTR2600128415"
        parsed = chictr.parse_detail_page(_read(CHICTR_DETAIL), chictr_no)
        raw = {"chictr_no": chictr_no, "proj_id": proj,
               "html": _read(CHICTR_DETAIL), "parsed_fields": parsed}
        chictr._upsert_and_emit_events(chictr.normalise(raw))
        chictr._save_cursor(
            "list_walk",
            {"recent_seen_ids": [], "proj_ids": {chictr_no: proj}},
            success=True,
        )
        conn.execute(
            "UPDATE registry_records SET last_crawled_at = "
            "datetime('now', '-40 days') WHERE source_trial_id = ?", (chictr_no,))
        conn.commit()

        from tests.test_discovery_walk import CHICTR_STUB
        monkeypatch.setattr(chictr, "_fetch",
                            _fake_chictr_detail(proj, _read(CHICTR_STUB)))
        assert plan_refresh() == 1
        stats = refresh_pending(chictr)
        assert stats["skipped"] == 1
        row = conn.execute("SELECT state FROM refresh_queue").fetchone()
        assert row["state"] == "skipped"
