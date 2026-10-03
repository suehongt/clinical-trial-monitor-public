"""
Tests for review-queue batch capabilities.

Covers:
  - queue_stats:        confidence 分档统计 + 总数 + 最早/最晚 created_at
  - get_queue_batch:    confidence 区间过滤（闭区间）与 limit
  - batch_resolve:      批量 approve/reject、审计字段、坏 id 容错
  - export_queue_csv:   行数与 utf-8-sig BOM
  - CLI 子命令:          review_queue stats / batch（默认 dry-run、--yes 才写库）/ export

所有测试均运行在 conftest.test_db 提供的临时库上，严禁读写真实库
db/ct_monitor.db。
"""
from __future__ import annotations

import csv
import sys
import uuid

import pytest

from db.connection import get_connection


# ── Fixtures / helpers ───────────────────────────────────────────────────


@pytest.fixture
def conn(test_db):
    """Get a connection to the temporary test database."""
    conn = get_connection()
    yield conn


def _seed_record(conn, trial_id: str = "NCT00000001",
                 title: str = "Batch Test Trial") -> int:
    """Insert one registry_record（使用 schema 预置的 NCT 来源与查表数据）."""
    sid = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = 'NCT'"
    ).fetchone()["source_id"]
    stid = conn.execute(
        "SELECT status_type_id FROM status_types WHERE label = 'Recruiting'"
    ).fetchone()["status_type_id"]
    cur = conn.execute(
        """INSERT INTO registry_records
           (source_id, source_trial_id, title, status_id, raw_payload, is_latest)
           VALUES (?, ?, ?, ?, '{}', 1)""",
        (sid, trial_id, title, stid),
    )
    conn.commit()
    return cur.lastrowid


def _seed_master(conn) -> str:
    """Insert a bare master_trials row（作为 FK 目标），返回其 UUID."""
    master_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO master_trials (master_trial_id, preferred_title) "
        "VALUES (?, 'Batch Master')",
        (master_id,),
    )
    conn.commit()
    return master_id


def _seed_queue(conn, record_id: int, confidence: float,
                status: str = "pending", master_id=None) -> int:
    """Insert one resolution_queue row，返回 queue_id."""
    if master_id is None:
        master_id = _seed_master(conn)
    cur = conn.execute(
        """INSERT INTO resolution_queue
           (record_id, suggested_master_id, match_method, confidence,
            reasoning, status)
           VALUES (?, ?, 'title_similarity', ?, 'looks similar', ?)""",
        (record_id, master_id, confidence, status),
    )
    conn.commit()
    return cur.lastrowid


def _run_cli(monkeypatch, capsys, *argv) -> str:
    """In-process 运行 `run_monitor.py review_queue <args>`，返回 stdout.

    注意：main() 结束时会 close_connection()，调用方之后需要重新
    get_connection() 才能继续查库（线程旧连接已关闭）。
    """
    from run_monitor import main
    monkeypatch.setattr(
        sys, "argv", ["run_monitor.py", "review_queue", *argv],
    )
    main()
    return capsys.readouterr().out


# ── queue_stats ──────────────────────────────────────────────────────────


class TestQueueStats:
    def test_bucket_counts_and_total(self, conn):
        """不同 confidence 的 pending 行按分档正确归桶；已处理行不计入."""
        from core.entity_resolution import queue_stats

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        for conf in (0.95, 0.92, 0.85, 0.75, 0.50):
            _seed_queue(conn, rid, conf, master_id=mid)
        # 非 pending 状态不计入统计
        _seed_queue(conn, rid, 0.99, master_id=mid, status="approved")
        _seed_queue(conn, rid, 0.60, master_id=mid, status="rejected")

        s = queue_stats()
        assert s["total"] == 5
        buckets = s["buckets"]
        assert buckets[">=0.90"] == 2
        assert buckets["0.80-0.90"] == 1
        assert buckets["0.70-0.80"] == 1
        assert buckets["<0.70"] == 1

    def test_boundary_confidence_goes_to_upper_bucket(self, conn):
        """confidence 恰等于分档边界时归入更高一档（左闭右开）."""
        from core.entity_resolution import queue_stats

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        for conf in (0.90, 0.80, 0.70):
            _seed_queue(conn, rid, conf, master_id=mid)

        buckets = queue_stats()["buckets"]
        assert buckets[">=0.90"] == 1
        assert buckets["0.80-0.90"] == 1
        assert buckets["0.70-0.80"] == 1

    def test_earliest_and_latest_created_at(self, conn):
        """返回 pending 行的最早/最晚 created_at."""
        from core.entity_resolution import queue_stats

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        q1 = _seed_queue(conn, rid, 0.90, master_id=mid)
        q2 = _seed_queue(conn, rid, 0.80, master_id=mid)
        conn.execute(
            "UPDATE resolution_queue SET created_at = '2026-01-01 00:00:00' "
            "WHERE queue_id = ?", (q1,))
        conn.execute(
            "UPDATE resolution_queue SET created_at = '2026-02-01 12:00:00' "
            "WHERE queue_id = ?", (q2,))
        conn.commit()

        s = queue_stats()
        assert s["earliest_created"] == "2026-01-01 00:00:00"
        assert s["latest_created"] == "2026-02-01 12:00:00"

    def test_empty_queue(self, conn):
        """空队列：total 为 0，时间字段为 None."""
        from core.entity_resolution import queue_stats

        s = queue_stats()
        assert s["total"] == 0
        assert s["earliest_created"] is None
        assert s["latest_created"] is None
        assert sum(s["buckets"].values()) == 0


# ── get_queue_batch ──────────────────────────────────────────────────────


class TestGetQueueBatch:
    def test_min_confidence_filter_and_desc_order(self, conn):
        from core.entity_resolution import get_queue_batch

        rid = _seed_record(conn)
        for conf in (0.95, 0.85, 0.75):
            _seed_queue(conn, rid, conf)

        items = get_queue_batch(min_confidence=0.80)
        assert [i["confidence"] for i in items] == [0.95, 0.85]

    def test_closed_interval_bounds(self, conn):
        """min/max 为闭区间：边界值本身包含在结果中."""
        from core.entity_resolution import get_queue_batch

        rid = _seed_record(conn)
        for conf in (0.95, 0.90, 0.85, 0.80, 0.70):
            _seed_queue(conn, rid, conf)

        items = get_queue_batch(min_confidence=0.80, max_confidence=0.90)
        assert [i["confidence"] for i in items] == [0.90, 0.85, 0.80]

    def test_limit_takes_top_n(self, conn):
        from core.entity_resolution import get_queue_batch

        rid = _seed_record(conn)
        for conf in (0.95, 0.90, 0.85, 0.75):
            _seed_queue(conn, rid, conf)

        items = get_queue_batch(limit=2)
        assert [i["confidence"] for i in items] == [0.95, 0.90]

    def test_status_filter_excludes_resolved(self, conn):
        from core.entity_resolution import get_queue_batch

        rid = _seed_record(conn)
        _seed_queue(conn, rid, 0.95)
        _seed_queue(conn, rid, 0.85, status="approved")

        items = get_queue_batch()
        assert len(items) == 1
        assert items[0]["confidence"] == 0.95

    def test_includes_record_details(self, conn):
        """JOIN 附带 source_trial_id / title / source_name 便于人工确认."""
        from core.entity_resolution import get_queue_batch

        rid = _seed_record(conn, trial_id="NCT77777777", title="Detail Check")
        _seed_queue(conn, rid, 0.88)

        item = get_queue_batch()[0]
        assert item["source_trial_id"] == "NCT77777777"
        assert item["title"] == "Detail Check"
        assert item["source_name"] == "NCT"


# ── batch_resolve ────────────────────────────────────────────────────────


class TestBatchResolve:
    def test_approve_creates_link_and_audits(self, conn):
        from core.entity_resolution import batch_resolve

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        qid = _seed_queue(conn, rid, 0.92, master_id=mid)

        succeeded, failures = batch_resolve([qid], "approve", reviewer="alice")
        assert (succeeded, failures) == (1, [])

        row = conn.execute(
            "SELECT status, reviewed_by, reviewed_at FROM resolution_queue "
            "WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "approved"
        assert row["reviewed_by"] == "alice"
        assert row["reviewed_at"] is not None

        link = conn.execute(
            "SELECT master_trial_id FROM record_master_map WHERE record_id = ?",
            (rid,)).fetchone()
        assert link is not None
        assert link["master_trial_id"] == mid

    def test_reject_marks_without_link(self, conn):
        from core.entity_resolution import batch_resolve

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        qid = _seed_queue(conn, rid, 0.60, master_id=mid)

        succeeded, failures = batch_resolve([qid], "reject", reviewer="bob")
        assert (succeeded, failures) == (1, [])

        row = conn.execute(
            "SELECT status, reviewed_by FROM resolution_queue "
            "WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "rejected"
        assert row["reviewed_by"] == "bob"
        # reject 不建 link
        assert conn.execute(
            "SELECT count(*) FROM record_master_map WHERE record_id = ?",
            (rid,)).fetchone()[0] == 0

    def test_missing_id_does_not_break_batch(self, conn):
        """不存在的 id 进入失败列表，其余 id 正常处理."""
        from core.entity_resolution import batch_resolve

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        qid = _seed_queue(conn, rid, 0.90, master_id=mid)

        succeeded, failures = batch_resolve([999999, qid], "approve")
        assert succeeded == 1
        assert len(failures) == 1
        assert failures[0][0] == 999999
        assert failures[0][1]  # 带失败原因
        # 存在的条目未受影响
        assert conn.execute(
            "SELECT status FROM resolution_queue WHERE queue_id = ?",
            (qid,)).fetchone()["status"] == "approved"

    def test_invalid_action_raises(self, conn):
        from core.entity_resolution import batch_resolve

        with pytest.raises(ValueError):
            batch_resolve([1], "delete")


# ── export_queue_csv ─────────────────────────────────────────────────────


class TestExportQueueCsv:
    def test_row_count_and_bom(self, conn, tmp_path):
        from core.entity_resolution import export_queue_csv

        rid = _seed_record(conn)
        for conf in (0.95, 0.85, 0.75):
            _seed_queue(conn, rid, conf)

        out = tmp_path / "queue.csv"
        n = export_queue_csv(str(out))
        assert n == 3

        raw = out.read_bytes()
        # utf-8-sig：文件以 BOM 开头，Excel 可直接识别
        assert raw.startswith(b"\xef\xbb\xbf")

        rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
        assert rows[0][0] == "queue_id"
        assert len(rows) == 4  # 表头 + 3 行数据

    def test_min_confidence_filter(self, conn, tmp_path):
        from core.entity_resolution import export_queue_csv

        rid = _seed_record(conn)
        for conf in (0.95, 0.85, 0.75):
            _seed_queue(conn, rid, conf)

        out = tmp_path / "high.csv"
        n = export_queue_csv(str(out), min_confidence=0.80)
        assert n == 2


# ── CLI 子命令 ───────────────────────────────────────────────────────────


class TestReviewQueueCli:
    def test_stats_prints_buckets(self, conn, monkeypatch, capsys):
        """stats 打印总数与各分档计数."""
        from core.entity_resolution import queue_stats

        rid = _seed_record(conn)
        mid = _seed_master(conn)
        for conf in (0.95, 0.85, 0.75):
            _seed_queue(conn, rid, conf, master_id=mid)

        out = _run_cli(monkeypatch, capsys, "stats")
        s = queue_stats()
        assert f"pending: {s['total']} item(s)" in out
        for label, count in s["buckets"].items():
            assert label in out
            assert str(count) in out

    def test_batch_dry_run_does_not_write(self, conn, monkeypatch, capsys):
        """默认 dry-run：只打印影响条数/样例/confidence 范围，不写库."""
        rid = _seed_record(conn)
        mid = _seed_master(conn)
        qid = _seed_queue(conn, rid, 0.92, master_id=mid)

        out = _run_cli(monkeypatch, capsys, "batch", "--min-confidence", "0.8")
        assert "DRY-RUN" in out
        assert "1 pending item(s)" in out
        assert "confidence range 0.92 - 0.92" in out
        # 样例区打印了条目
        assert str(qid) in out

        # 库未被修改：状态仍是 pending，无新 link
        # （main() 末尾会 close_connection()，这里重新取连接）
        fresh = get_connection()
        row = fresh.execute(
            "SELECT status, reviewed_by FROM resolution_queue WHERE queue_id = ?",
            (qid,)).fetchone()
        assert row["status"] == "pending"
        assert row["reviewed_by"] is None
        assert fresh.execute(
            "SELECT count(*) FROM record_master_map WHERE record_id = ?",
            (rid,)).fetchone()[0] == 0

    def test_batch_yes_approve_writes(self, conn, monkeypatch, capsys):
        """--yes 才真正执行：status 翻转、reviewed_by 记录."""
        rid = _seed_record(conn)
        mid = _seed_master(conn)
        for conf in (0.92, 0.85):
            _seed_queue(conn, rid, conf, master_id=mid)

        out = _run_cli(monkeypatch, capsys, "batch",
                       "--min-confidence", "0.8", "--action", "approve",
                       "--reviewer", "carol", "--yes")
        assert "2 succeeded, 0 failed" in out

        fresh = get_connection()
        rows = fresh.execute(
            "SELECT status, reviewed_by FROM resolution_queue "
            "WHERE confidence >= 0.8").fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["status"] == "approved"
            assert row["reviewed_by"] == "carol"

    def test_batch_yes_reject_writes(self, conn, monkeypatch, capsys):
        """--yes --action reject：状态翻转为 rejected，且不建 link."""
        rid = _seed_record(conn)
        mid = _seed_master(conn)
        qid = _seed_queue(conn, rid, 0.55, master_id=mid)

        out = _run_cli(monkeypatch, capsys, "batch",
                       "--max-confidence", "0.7", "--action", "reject",
                       "--yes")
        assert "1 succeeded, 0 failed" in out

        fresh = get_connection()
        row = fresh.execute(
            "SELECT status FROM resolution_queue WHERE queue_id = ?",
            (qid,)).fetchone()
        assert row["status"] == "rejected"
        assert fresh.execute(
            "SELECT count(*) FROM record_master_map WHERE record_id = ?",
            (rid,)).fetchone()[0] == 0

    def test_batch_no_matches(self, conn, monkeypatch, capsys):
        _seed_record(conn)
        out = _run_cli(monkeypatch, capsys, "batch", "--min-confidence", "0.99")
        assert "No pending items" in out

    def test_export_cli_writes_csv(self, conn, monkeypatch, capsys, tmp_path):
        """export 子命令落盘 CSV（utf-8-sig）并打印导出行数."""
        rid = _seed_record(conn)
        for conf in (0.95, 0.85):
            _seed_queue(conn, rid, conf)

        out_path = tmp_path / "cli_export.csv"
        out = _run_cli(monkeypatch, capsys, "export", "--output", str(out_path))
        assert "Exported 2 queue item(s)" in out

        rows = list(csv.reader(
            out_path.read_bytes().decode("utf-8-sig").splitlines()))
        assert len(rows) == 3  # 表头 + 2 行
