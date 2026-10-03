"""ICTRP 快照周任务契约测试（fetch → import → diff → discovery_queue）。

覆盖 the project design notes §2.1（Agent-C 部分）：

  1. reg_name → 源 short_name 模块常量契约（ChiCTR / 中国药物临床试验
     登记与信息公示平台 / CDE / ChinaDrugTrials → CTR；未知 → None）
  2. --apply：快照中 ChiCTR/CTR 记录的新号入队（ictrp_diff/pending），
     ICTRP 快照本身照常 import（AGGREGATOR 语义不变）
  3. 已有本源记录的号不入队（already_present 计数）
  4. 同快照跑两遍：第二遍 queued=0、队列行不膨胀、水位行 UPSERT 仍各一
  5. --dry-run（默认）：只统计不写库（队列/水位/registry_records 均无写入）
  6. 水位行写入 discovery_cursors(mode='ictrp_diff')，cursor_json 记
     快照文件名/记录数/入队数/时间
  7. 未知 reg_name 跳过并计数，不入队
  8. --skip-fetch 且本地快照缺失 → 记 error 不崩溃
  9. fetch 接线：monkeypatch fetch_snapshot 验证网络步被调用（零真实网络）
  10. 截断/损坏快照：--skip-fetch 记 error；允许联网时自动重抓一次，
      重抓仍失败则记 error 跳过——坏快照绝不拖垮整轮周任务

全部离线：自造小型 ICTRP XML fixture（tmp_path）；数据库走 conftest 的
test_db 临时库；网络函数以 monkeypatch 替换。
"""
from __future__ import annotations

import json

import pytest

from db.connection import get_connection
from scripts.ictrp_weekly import (
    CURSOR_MODE,
    DIFF_TARGET_SOURCES,
    map_reg_name,
    resolve_profiles,
    run_weekly,
)


# ── 工具与 fixture ──────────────────────────────────────────────────────────


def _trial(reg_name: str, trial_id: str, title: str = "t") -> str:
    """构造一条最小 <Trial> 记录（元素结构与真实 ICTRP 导出一致）。"""
    return (
        "<Trial>"
        f"<reg_name>{reg_name}</reg_name>"
        f"<trial_id>{trial_id}</trial_id>"
        f"<public_title>{title}</public_title>"
        "<recruitment_status>Recruiting</recruitment_status>"
        "</Trial>"
    )


def _xml(trials) -> str:
    """把若干 <Trial> 片段包成 ICTRP 导出根元素 <Trials>。"""
    return ("<?xml version='1.0' encoding='UTF-8' ?><Trials>"
            + "".join(trials) + "</Trials>")


# 5 条 diff 候选（2 ChiCTR + 3 CTR 变体 reg_name）+ 2 条非目标源
STANDARD_TRIALS = [
    ("ChiCTR", "ChiCTR2600128415"),
    ("ChiCTR", "ChiCTR2600131927"),
    ("中国药物临床试验登记与信息公示平台", "CTR20250001"),
    ("CDE", "CTR20250999"),
    ("ChinaDrugTrials", "CTR20250888"),
    ("ClinicalTrials.gov", "NCT04760888"),   # 已知注册源但非本任务目标
    ("TrialRegister.nl", "NL6217"),          # 完全未知的 reg_name
]


@pytest.fixture
def conn(test_db):
    """conftest 临时库连接（registry_sources 已由 create_schema 播种）。"""
    return get_connection()


@pytest.fixture
def snapshot_xml(tmp_path):
    """标准 fixture 快照：写盘并返回路径。"""
    path = tmp_path / "ictrp_test_export.xml"
    path.write_text(_xml(_trial(r, t) for r, t in STANDARD_TRIALS),
                    encoding="utf-8")
    return path


def _sid(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


def _queue_rows(conn, short_name: str):
    return conn.execute(
        "SELECT * FROM discovery_queue WHERE source_id = ? "
        "ORDER BY discovery_id",
        (_sid(conn, short_name),),
    ).fetchall()


def _insert_local_record(conn, short_name: str, trial_id: str) -> None:
    """预置一条本源 registry_records（模拟该号已被主源采集过）。"""
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title, "
        "raw_payload, is_latest) VALUES (?, ?, 'seed', '{}', 1)",
        (_sid(conn, short_name), trial_id),
    )
    conn.commit()


# ── reg_name 映射契约 ────────────────────────────────────────────────────────


class TestRegNameMapping:
    """模块常量 REGNAME_TO_SOURCE / map_reg_name 契约。"""

    def test_required_keys(self):
        """任务书指定的三类 reg_name 均映射到正确 short_name。"""
        assert map_reg_name("ChiCTR") == "ChiCTR"
        assert map_reg_name("中国药物临床试验登记与信息公示平台") == "CTR"
        assert map_reg_name("CDE") == "CTR"

    def test_normalisation_and_variants(self):
        """大小写/首尾空白归一；真实导出常见变体也命中。"""
        assert map_reg_name(" chictr ") == "ChiCTR"
        assert map_reg_name("CHICTR") == "ChiCTR"
        assert map_reg_name("Chinese Clinical Trial Register") == "ChiCTR"
        assert map_reg_name("ChinaDrugTrials") == "CTR"
        assert map_reg_name("chinadrugtrials.org.cn") == "CTR"

    def test_unknown_and_empty_return_none(self):
        """非目标源（含 NCT）与空值 → None（调用方跳过并计数）。"""
        assert map_reg_name("ClinicalTrials.gov") is None
        assert map_reg_name("TrialRegister.nl") is None
        assert map_reg_name("") is None
        assert map_reg_name(None) is None

    def test_targets_match_schema_seed(self, conn):
        """目标 short_name 与 db/schema.py 种子完全一致（防源名漂移）。

        必须走 conn(test_db) 临时库：直接 get_connection() 会连到开发者
        本机的真实库，CI（无真实库）必然 no such table。
        """
        for short in DIFF_TARGET_SOURCES:
            row = conn.execute(
                "SELECT source_id, source_type FROM registry_sources "
                "WHERE short_name = ?", (short,)).fetchone()
            assert row is not None, f"registry_sources 缺少 {short}"
            assert row["source_type"] == "PRIMARY"


class TestResolveProfiles:
    """--profiles 参数解析契约。"""

    def test_all_expands_sorted(self):
        assert resolve_profiles("all") == sorted(resolve_profiles("ALL"))
        assert resolve_profiles("all") == list(resolve_profiles("all"))

    def test_comma_list_and_spaces(self):
        keys = resolve_profiles(" mi , hf ")
        assert keys == ["mi", "hf"]

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match="unknown profile"):
            resolve_profiles("mi,nope")


# ── --apply：新号入队 + 快照导入 ─────────────────────────────────────────────


class TestApplyDiff:
    """apply 模式：diff 判定、入队幂等、水位写入。"""

    def test_new_numbers_enqueued_and_snapshot_imported(
            self, conn, snapshot_xml):
        """新号按源入队（ictrp_diff/pending），ICTRP 快照照常 import。"""
        summary = run_weekly(profiles="mi", xml=str(snapshot_xml),
                             apply=True, skip_fetch=True)

        assert summary["mode"] == "apply"
        assert summary["queued"] == 5
        assert summary["already_present"] == 0
        assert summary["unknown_registry"] == 2
        entry = summary["per_profile"]["mi"]
        assert entry["parsed"] == 7
        assert entry["imported"] == 7  # ICTRP AGGREGATOR 记录全部新入库
        assert summary["errors"] == []

        chictr_rows = _queue_rows(conn, "ChiCTR")
        ctr_rows = _queue_rows(conn, "CTR")
        assert [r["source_trial_id"] for r in chictr_rows] == [
            "ChiCTR2600128415", "ChiCTR2600131927"]
        assert {r["source_trial_id"] for r in ctr_rows} == {
            "CTR20250001", "CTR20250999", "CTR20250888"}
        for row in chictr_rows + ctr_rows:
            assert row["discovered_via"] == "ictrp_diff"
            assert row["state"] == "pending"
            assert row["attempts"] == 0

        # 源生号只入队；ChiCTR 详情键（proj_id）不在本任务职责内
        assert all("proj" not in (r["last_error"] or "") for r in chictr_rows)

        n_ictrp = conn.execute(
            "SELECT count(*) FROM registry_records WHERE source_id = ?",
            (_sid(conn, "ICTRP"),)).fetchone()[0]
        assert n_ictrp == 7

    def test_existing_numbers_not_enqueued(self, conn, snapshot_xml):
        """已有本源记录的号不入队，计 already_present。"""
        _insert_local_record(conn, "ChiCTR", "ChiCTR2600128415")
        _insert_local_record(conn, "CTR", "CTR20250001")

        summary = run_weekly(profiles="mi", xml=str(snapshot_xml),
                             apply=True, skip_fetch=True)

        assert summary["queued"] == 3
        assert summary["already_present"] == 2
        ids = {r["source_trial_id"] for r in _queue_rows(conn, "ChiCTR")}
        ids |= {r["source_trial_id"] for r in _queue_rows(conn, "CTR")}
        assert ids == {"ChiCTR2600131927", "CTR20250999", "CTR20250888"}

    def test_same_snapshot_twice_second_queued_zero(self, conn,
                                                    snapshot_xml):
        """同快照跑两遍：第二遍 queued=0，队列与水位行都不膨胀。"""
        first = run_weekly(profiles="mi", xml=str(snapshot_xml),
                           apply=True, skip_fetch=True)
        assert first["queued"] == 5

        second = run_weekly(profiles="mi", xml=str(snapshot_xml),
                            apply=True, skip_fetch=True)
        assert second["queued"] == 0
        # 本源记录仍未补齐（enrich 未跑），但 INSERT OR IGNORE 保证不重复入队
        assert second["already_present"] == 0

        assert len(_queue_rows(conn, "ChiCTR")) == 2
        assert len(_queue_rows(conn, "CTR")) == 3
        rows = conn.execute(
            "SELECT cursor_id FROM discovery_cursors "
            "WHERE mode = ?", (CURSOR_MODE,)).fetchall()
        assert len(rows) == len(DIFF_TARGET_SOURCES)  # UPSERT 幂等

    def test_cursor_row_written(self, conn, snapshot_xml):
        """水位：discovery_cursors(mode='ictrp_diff') 记快照名/记录数/入队数。"""
        run_weekly(profiles="mi", xml=str(snapshot_xml),
                   apply=True, skip_fetch=True)

        rows = conn.execute(
            "SELECT * FROM discovery_cursors WHERE mode = ?",
            (CURSOR_MODE,)).fetchall()
        assert len(rows) == 2
        by_short = {
            short: next(r for r in rows
                        if r["source_id"] == _sid(conn, short))
            for short in DIFF_TARGET_SOURCES
        }
        for short, row in by_short.items():
            assert row["status"] == "active"
            assert row["last_attempted"] is not None
            assert row["last_successful"] is not None
            cursor = json.loads(row["cursor_json"])
            assert cursor["snapshot_files"] == {"mi": snapshot_xml.name}
            assert cursor["record_counts"] == {"mi": 7}
            assert cursor["queued"] == (2 if short == "ChiCTR" else 3)
            assert "updated_at" in cursor

    def test_unknown_regname_skipped_and_counted(self, conn, snapshot_xml):
        """未知 reg_name 跳过并计数，绝不入队。"""
        summary = run_weekly(profiles="mi", xml=str(snapshot_xml),
                             apply=True, skip_fetch=True)
        assert summary["unknown_registry"] == 2
        all_ids = {r["source_trial_id"]
                   for s in DIFF_TARGET_SOURCES
                   for r in _queue_rows(conn, s)}
        assert "NCT04760888" not in all_ids
        assert "NL6217" not in all_ids

    def test_fetch_wiring_via_monkeypatch(self, conn, snapshot_xml,
                                          tmp_path, monkeypatch):
        """网络步接线：fetch_snapshot 被调用且产物可导入（零真实网络）。"""
        from scripts import ictrp_weekly

        target = tmp_path / "nested" / "ictrp_mi_export.xml"
        calls = []

        def fake_fetch(profile_key, xml_path):
            calls.append((profile_key, str(xml_path)))
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(
                _xml(_trial("ChiCTR", "ChiCTR2600999999")),
                encoding="utf-8")
            return True

        monkeypatch.setattr(ictrp_weekly, "snapshot_path_for",
                            lambda key: target)
        monkeypatch.setattr(ictrp_weekly, "fetch_snapshot", fake_fetch)

        summary = run_weekly(profiles="mi", apply=True, skip_fetch=False)

        assert calls == [("mi", str(target))]
        entry = summary["per_profile"]["mi"]
        assert entry["fetched"] is True
        assert entry["fetch_ok"] is True
        assert summary["queued"] == 1
        assert summary["imported"] == 1
        assert _queue_rows(conn, "ChiCTR")[0]["source_trial_id"] == \
            "ChiCTR2600999999"

    def test_fetch_failure_with_local_fallback(self, conn, snapshot_xml,
                                               monkeypatch):
        """fetch 失败但本地快照存在 → 回退用本地快照继续 diff。"""
        from scripts import ictrp_weekly

        monkeypatch.setattr(ictrp_weekly, "snapshot_path_for",
                            lambda key: snapshot_xml)
        monkeypatch.setattr(ictrp_weekly, "fetch_snapshot",
                            lambda key, path: False)

        summary = run_weekly(profiles="mi", apply=True, skip_fetch=False)
        assert summary["errors"] == []
        assert summary["per_profile"]["mi"]["fetch_ok"] is False
        assert summary["queued"] == 5


# ── --dry-run：只统计不写库 ──────────────────────────────────────────────────


class TestDryRun:
    """dry-run（默认）语义：统计齐全，零写入。"""

    def test_dry_run_writes_nothing(self, conn, snapshot_xml):
        """dry-run：queued 统计真实，但库零写入。"""
        summary = run_weekly(profiles="mi", xml=str(snapshot_xml),
                             apply=False, skip_fetch=True)

        assert summary["mode"] == "dry-run"
        assert summary["queued"] == 5
        assert summary["already_present"] == 0
        assert summary["imported"] == 0

        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM discovery_cursors"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM registry_records"
                            ).fetchone()[0] == 0
        entry = summary["per_profile"]["mi"]
        assert "cursors" not in summary
        assert entry["parsed"] == 7

    def test_dry_run_counts_existing(self, conn, snapshot_xml):
        """dry-run 对已有本源记录同样只计 already_present。"""
        _insert_local_record(conn, "ChiCTR", "ChiCTR2600131927")
        summary = run_weekly(profiles="mi", xml=str(snapshot_xml),
                             apply=False, skip_fetch=True)
        assert summary["queued"] == 4
        assert summary["already_present"] == 1
        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0


# ── CLI 与异常路径 ───────────────────────────────────────────────────────────


class TestCliAndErrors:
    """main() 参数解析与快照缺失容错。"""

    def test_main_defaults_to_dry_run(self, conn, snapshot_xml, capsys):
        """不带 --apply 时默认 dry-run，不写库且打印汇总 JSON。"""
        from scripts.ictrp_weekly import main

        summary = main(["--profiles", "mi", "--xml", str(snapshot_xml),
                        "--skip-fetch"])
        assert summary["mode"] == "dry-run"
        payload = json.loads(capsys.readouterr().out)
        assert payload["queued"] == 5
        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0

    def test_main_apply_flag(self, conn, snapshot_xml):
        """--apply 显式开启写库。"""
        from scripts.ictrp_weekly import main

        summary = main(["--profiles", "mi", "--xml", str(snapshot_xml),
                        "--apply", "--skip-fetch"])
        assert summary["mode"] == "apply"
        assert summary["queued"] == 5

    def test_main_rejects_unknown_profile(self):
        """未知 profile → parser.error（SystemExit 2）。"""
        from scripts.ictrp_weekly import main

        with pytest.raises(SystemExit) as exc:
            main(["--profiles", "nope", "--skip-fetch"])
        assert exc.value.code == 2

    def test_main_rejects_xml_with_multiple_profiles(self):
        """--xml 只允许配合单一 profile。"""
        from scripts.ictrp_weekly import main

        with pytest.raises(SystemExit) as exc:
            main(["--profiles", "mi,hf", "--xml", "x.xml", "--skip-fetch"])
        assert exc.value.code == 2

    def test_missing_snapshot_records_error(self, conn, tmp_path):
        """--skip-fetch 且本地快照缺失 → 记 error、不崩溃、不写库。"""
        missing = tmp_path / "nope.xml"
        summary = run_weekly(profiles="mi", xml=str(missing),
                             apply=True, skip_fetch=True)
        assert len(summary["errors"]) == 1
        assert "mi" in summary["errors"][0]
        assert "error" in summary["per_profile"]["mi"]
        assert summary["queued"] == 0
        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0


# ── 截断/损坏快照容错 ────────────────────────────────────────────────────────


class TestTruncatedSnapshot:
    """中断的下载会留下半份 XML（ET.ParseError）——周任务必须不崩。"""

    def test_truncated_with_skip_fetch_records_error(self, conn, tmp_path):
        """--skip-fetch 且快照截断 → 记 error、不崩溃、不写库。"""
        broken = tmp_path / "ictrp_broken_export.xml"
        broken.write_text(
            "<?xml version='1.0' encoding='UTF-8' ?><Trials><Trial>"
            "<reg_name>ChiCTR</reg_name><trial_id>ChiCTR2600128",
            encoding="utf-8")

        summary = run_weekly(profiles="mi", xml=str(broken),
                             apply=True, skip_fetch=True)
        assert len(summary["errors"]) == 1
        assert "unparseable" in summary["errors"][0]
        assert "error" in summary["per_profile"]["mi"]
        assert summary["queued"] == 0
        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM discovery_cursors"
                            ).fetchone()[0] == 0

    def test_truncated_snapshot_recovered_by_refetch(self, conn, tmp_path,
                                                     monkeypatch):
        """首次抓取中断留下坏文件 → 自动重抓一次，用新快照继续 diff。"""
        from scripts import ictrp_weekly

        broken = tmp_path / "ictrp_mi_export.xml"
        broken.write_text(
            "<?xml version='1.0' encoding='UTF-8' ?><Trials><Trial>"
            "<reg_name>ChiCTR</reg_name><trial_id>ChiCTR2600128",
            encoding="utf-8")
        calls = {"n": 0}

        def fake_fetch(key, path):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # 首次抓取中断，坏文件留在盘上
            path.write_text(_xml(_trial("ChiCTR", "ChiCTR2600999999")),
                            encoding="utf-8")
            return True

        monkeypatch.setattr(ictrp_weekly, "snapshot_path_for",
                            lambda key: broken)
        monkeypatch.setattr(ictrp_weekly, "fetch_snapshot", fake_fetch)

        summary = run_weekly(profiles="mi", apply=True, skip_fetch=False)
        assert calls["n"] == 2  # 解析失败触发了一次重抓
        assert summary["errors"] == []
        assert summary["queued"] == 1
        entry = summary["per_profile"]["mi"]
        assert entry["fetch_ok"] is True
        assert entry["parsed"] == 1
        assert _queue_rows(conn, "ChiCTR")[0]["source_trial_id"] == \
            "ChiCTR2600999999"

    def test_truncated_refetch_also_failing_records_error(self, conn,
                                                          tmp_path,
                                                          monkeypatch):
        """重抓后仍截断（重抓产出坏文件/失败回退到坏文件）→ 记 error 跳过。"""
        from scripts import ictrp_weekly

        broken = tmp_path / "ictrp_mi_export.xml"
        broken.write_text(
            "<?xml version='1.0' encoding='UTF-8' ?><Trials><Trial>",
            encoding="utf-8")
        monkeypatch.setattr(ictrp_weekly, "snapshot_path_for",
                            lambda key: broken)
        monkeypatch.setattr(ictrp_weekly, "fetch_snapshot",
                            lambda key, path: False)  # 重抓失败

        summary = run_weekly(profiles="mi", apply=True, skip_fetch=False)
        assert len(summary["errors"]) == 1
        assert "unparseable" in summary["errors"][0]
        assert summary["queued"] == 0
        assert conn.execute("SELECT count(*) FROM discovery_queue"
                            ).fetchone()[0] == 0
