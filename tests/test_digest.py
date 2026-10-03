"""core.digest tests — 选取口径、分组、格式化与推送水位线门控。

全部离线：notify 渠道以 monkeypatch 替身（仿 test_notify），数据库走
conftest 的 test_db 临时库。
"""
from __future__ import annotations

import pytest

from core import digest
from core.digest import (
    collect_change_events,
    collect_new_trials,
    digest_watermark,
    format_digest,
    match_profile,
    run_digest,
)
from db.connection import get_connection


# ── 种子工具 ────────────────────────────────────────────────────────────


def _sid(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _insert_record(conn, short_name: str, trial_id: str, title: str,
                   *, first_crawled_at: str = "datetime('now', '-1 hour')",
                   is_bootstrap: int = 0, version_number: int = 1,
                   is_latest: int = 1, enrollment=None,
                   conditions=None) -> int:
    cur = conn.execute(
        f"INSERT INTO registry_records (source_id, source_trial_id, title, "
        f"conditions, enrollment, is_bootstrap, version_number, is_latest, "
        f"first_crawled_at, last_crawled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        f"{first_crawled_at}, {first_crawled_at})",
        (_sid(conn, short_name), trial_id, title, conditions, enrollment,
         is_bootstrap, version_number, is_latest),
    )
    conn.commit()
    return cur.lastrowid


def _insert_event(conn, record_id: int, field_name: str, old_value,
                  new_value, event_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO trial_events (record_id, event_type, field_name, "
        "old_value, new_value, change_category, event_hash) "
        "VALUES (?, 'field_change', ?, ?, ?, ?, ?)",
        (record_id, field_name, old_value, new_value, "other", event_hash),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def clean_notify(monkeypatch):
    """digest 的渠道/发送全部替换为可控替身，不触网、不动真实环境变量。"""
    calls: dict = {"send": [], "channels": []}

    monkeypatch.setattr(digest, "configured_channels",
                        lambda: list(calls["channels"]))
    monkeypatch.setattr(digest, "send_notification",
                        lambda title, text, level="info", timeout=10.0:
                        calls["send"].append(
                            {"title": title, "text": text, "level": level})
                        or dict(calls["results"]))
    calls["results"] = {}
    return calls


# ── 水位线 ──────────────────────────────────────────────────────────────


def test_watermark_defaults_and_advance(test_db):
    assert digest_watermark() == {"last_event_id": 0, "last_run_at": None}
    digest._advance_watermark(77)
    wm = digest_watermark()
    assert wm["last_event_id"] == 77
    assert wm["last_run_at"] is not None


# ── 选取口径 ────────────────────────────────────────────────────────────


class TestCollectNewTrials:
    def test_excludes_bootstrap_and_later_versions(self, test_db):
        conn = get_connection()
        _insert_record(conn, "NCT", "NCT11111111", "Fresh MI trial")
        _insert_record(conn, "ICTRP", "NCT22222222", "WHO import",
                       is_bootstrap=1)
        rid = _insert_record(conn, "NCT", "NCT33333333", "MI trial v1")
        # 同号出现新版本：v2 行有自己的 first_crawled_at，但不是新试验
        _insert_record(conn, "NCT", "NCT33333333", "MI trial v2",
                       version_number=2)
        conn.execute("UPDATE registry_records SET is_latest = 0 "
                     "WHERE record_id = ?", (rid,))
        conn.commit()

        trials = collect_new_trials(since=None)
        ids = {t["source_trial_id"] for t in trials}
        assert ids == {"NCT11111111"}

    def test_since_window_filters(self, test_db):
        from datetime import datetime, timedelta, timezone

        conn = get_connection()
        _insert_record(conn, "NCT", "NCT11111111", "Old",
                       first_crawled_at="datetime('now', '-3 days')")
        _insert_record(conn, "NCT", "NCT22222222", "Fresh")
        conn.commit()

        # since 为字面时间戳（水位线的存储格式），两天前 → 只剩新记录
        since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S")
        trials = collect_new_trials(since=since)
        assert {t["source_trial_id"] for t in trials} == {"NCT22222222"}

        # 首跑（since=None）默认 24h 窗口：旧记录同样不在窗口内
        trials = collect_new_trials(since=None)
        assert {t["source_trial_id"] for t in trials} == {"NCT22222222"}


class TestCollectChangeEvents:
    def test_event_id_watermark_and_humanised_status(self, test_db):
        conn = get_connection()
        rid = _insert_record(conn, "NCT", "NCT11111111", "MI trial",
                             conditions='["myocardial infarction"]')
        status_id = conn.execute(
            "SELECT status_type_id FROM status_types WHERE label='Recruiting'"
        ).fetchone()["status_type_id"]
        _insert_event(conn, rid, "status_id", "1", str(status_id), "h1")
        _insert_event(conn, rid, "enrollment", "120", "240", "h2")
        digest._advance_watermark(1)  # 第一条已推送

        events = collect_change_events(1)
        assert [e["field_name"] for e in events] == ["enrollment"]
        assert events[0]["old_value"] == "120"
        assert events[0]["new_value"] == "240"

        # status_id 的整型 FK 值被翻译为状态名
        events_all = collect_change_events(0)
        status_event = next(e for e in events_all
                            if e["field_name"] == "status_id")
        assert status_event["new_value"] == "Recruiting"
        assert status_event["short_name"] == "NCT"
        assert status_event["source_trial_id"] == "NCT11111111"


# ── profile 分组 ────────────────────────────────────────────────────────


class TestMatchProfile:
    def test_cjk_substring(self):
        assert match_profile("急性心肌梗死患者的研究", None) == "mi"

    def test_ascii_word_boundary(self):
        assert match_profile("STEMI management trial", None) == "mi"
        # 词边界：familial 不含独立词 AMI
        assert match_profile("familial hypercholesterolemia study",
                             None) is None

    def test_conditions_fallback_and_no_match(self):
        assert match_profile("A registry study",
                             '["multiple myeloma"]') == "mm"
        assert match_profile("A hypertension trial", None) is None


# ── 格式化（纯函数） ────────────────────────────────────────────────────


class TestFormatDigest:
    def test_sections_and_counts(self):
        news = [{"short_name": "NCT", "source_trial_id": "NCT11111111",
                 "title": "A myocardial infarction trial",
                 "study_phase": "Phase 3"}]
        changes = [{"short_name": "NCT", "source_trial_id": "NCT22222222",
                    "field_name": "enrollment", "old_value": "120",
                    "new_value": "240"}]
        title, text = format_digest({"mi": news}, {"mi": changes})
        assert "临床试验监测日报" in title
        assert "【心肌梗死】" in text
        assert "新增 1 · 变更 1" in text
        assert "入组人数: 120 → 240" in text
        assert "NCT11111111" in text and "Phase 3" in text

    def test_overflow_line(self):
        news = [{"short_name": "NCT", "source_trial_id": f"NCT{i:08d}",
                 "title": f"心梗试验 {i}", "study_phase": None}
                for i in range(8)]
        _, text = format_digest({"mi": news}, {})
        assert "…另有 3 项" in text
        assert text.count("· [NCT]") == 5  # NEW_PER_PROFILE

    def test_empty_renders_no_text(self):
        title, text = format_digest({}, {})
        assert text == ""
        assert "临床试验监测日报" in title

    def test_json_list_value_compacted(self):
        changes = [{"short_name": "NCT", "source_trial_id": "NCT22222222",
                    "field_name": "sponsors",
                    "old_value": '["Pfizer"]',
                    "new_value": '["Pfizer", "Roche"]'}]
        _, text = format_digest({}, {None: changes})
        assert "Pfizer、Roche" in text

    def test_english_labels(self):
        changes = [{"short_name": "NCT", "source_trial_id": "NCT22222222",
                    "field_name": "enrollment", "old_value": "120",
                    "new_value": "240"}]
        title, text = format_digest({}, {"mm": changes}, english=True)
        assert "Clinical Trial Digest" in title
        assert "Enrollment: 120 → 240" in text


# ── run_digest 推送门控 ─────────────────────────────────────────────────


class TestRunDigestGating:
    def _seed_updates(self, conn):
        rid = _insert_record(conn, "NCT", "NCT11111111",
                             "A myocardial infarction trial")
        _insert_event(conn, rid, "enrollment", "120", "240", "h-seed-1")

    def test_empty_status(self, test_db, clean_notify):
        result = run_digest()
        assert result["status"] == "empty"
        assert result["text"] == ""

    def test_no_channels_skips_without_touching_watermark(
            self, test_db, clean_notify):
        conn = get_connection()
        self._seed_updates(conn)
        result = run_digest()
        assert result["status"] == "skipped_no_channels"
        assert digest_watermark()["last_event_id"] == 0
        assert clean_notify["send"] == []

    def test_successful_push_advances_watermark(self, test_db, clean_notify):
        conn = get_connection()
        self._seed_updates(conn)
        clean_notify["channels"] = ["wework"]
        clean_notify["results"] = {"wework": "ok", "dingtalk": "skipped"}

        result = run_digest()
        assert result["status"] == "pushed"
        assert len(clean_notify["send"]) == 1
        sent = clean_notify["send"][0]
        assert sent["level"] == "info"
        assert "NCT11111111" in sent["text"]
        assert "入组人数: 120 → 240" in sent["text"]
        assert digest_watermark()["last_event_id"] >= 1

    def test_push_failure_keeps_watermark(self, test_db, clean_notify):
        conn = get_connection()
        self._seed_updates(conn)
        clean_notify["channels"] = ["wework"]
        clean_notify["results"] = {"wework": "failed"}

        result = run_digest()
        assert result["status"] == "push_failed"
        assert digest_watermark()["last_event_id"] == 0

    def test_dry_run_neither_sends_nor_advances(self, test_db, clean_notify):
        conn = get_connection()
        self._seed_updates(conn)
        clean_notify["channels"] = ["wework"]
        clean_notify["results"] = {"wework": "ok"}

        result = run_digest(dry_run=True)
        assert result["status"] == "dry_run"
        assert clean_notify["send"] == []
        assert digest_watermark()["last_event_id"] == 0

    def test_second_run_after_push_is_empty(self, test_db, clean_notify):
        conn = get_connection()
        self._seed_updates(conn)
        clean_notify["channels"] = ["wework"]
        clean_notify["results"] = {"wework": "ok"}

        assert run_digest()["status"] == "pushed"
        assert run_digest()["status"] == "empty"
