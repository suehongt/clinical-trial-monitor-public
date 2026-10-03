"""解析金丝雀断言 + 发现层漏斗指标（§2.6 L5 / Agent-D）。

覆盖：
- core.quality_checks.parse_canary_ok：真实 fixture 正/负样本、未知源、
  空 HTML、CANARY_MIN_HITS 边界；
- core.quality_checks.discovery_funnel_stats：空队列、混合 state 下的
  (source, state) 计数与 enriched/(enriched+failed+skipped) 转化率；
- get_quality_summary 的 "discovery" 新键（向后兼容）；
- scripts/quality_report.py --json 输出携带 discovery（直接调 main，
  不走 subprocess——子进程拿不到 test_db 的临时库路径）。

全部离线：只用 tests/fixtures/ 下的真实 HTML，不发网络请求。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 真实契约 fixture
CHICTR_DETAIL_STANDALONE = FIXTURES / "chictr_detail_standalone.html"
CHICTR_DETAIL_WITH_NCT = FIXTURES / "chictr_detail_with_nct.html"
CHICTR_LIST_PAGE = FIXTURES / "chictr" / "spike_search_xin_p1.html"
CDE_DETAIL_SAMPLE = FIXTURES / "cde_detail_sample.html"
CTR_LIST_PAGE = FIXTURES / "chinadrugtrials" / "spike_search_xin_p1.html"


def _read_html(path: Path) -> str:
    """Read a fixture HTML file as text (fixtures are static, no network)."""
    return path.read_text(encoding="utf-8")


def _enqueue(db_path: Path, short_name: str, trial_id: str, state: str,
             via: str = "search") -> None:
    """向临时库 discovery_queue 插入一行（直接 sqlite3，与 conftest 同风格）。"""
    conn = sqlite3.connect(str(db_path))
    source_id = conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO discovery_queue
               (source_id, source_trial_id, discovered_via, state)
           VALUES (?, ?, ?, ?)""",
        (source_id, trial_id, via, state),
    )
    conn.commit()
    conn.close()


# ── parse_canary_ok：正样本（真实详情页 fixture） ───────────────────────────

def test_chictr_detail_fixtures_pass_canary():
    """真实 ChiCTR 详情页 fixture 必须命中 ≥K 个关键标签。"""
    from core.quality_checks import parse_canary_ok

    assert parse_canary_ok("chictr", _read_html(CHICTR_DETAIL_STANDALONE))
    assert parse_canary_ok("chictr", _read_html(CHICTR_DETAIL_WITH_NCT))


def test_cde_detail_fixture_passes_canary():
    """真实 CDE（chinadrugtrials）详情页 fixture 必须通过金丝雀。"""
    from core.quality_checks import parse_canary_ok

    assert parse_canary_ok("chinadrugtrials", _read_html(CDE_DETAIL_SAMPLE))


# ── parse_canary_ok：负样本（真实列表页 fixture） ───────────────────────────

def test_chictr_list_page_fails_canary():
    """ChiCTR 列表页不含详情页专属标签（入选标准/测量指标等），金丝雀应拒绝。"""
    from core.quality_checks import parse_canary_ok

    assert not parse_canary_ok("chictr", _read_html(CHICTR_LIST_PAGE))


def test_ctr_list_page_fails_canary():
    """CTR 平台列表页（最新公示）不满足详情页金丝雀。"""
    from core.quality_checks import parse_canary_ok

    assert not parse_canary_ok("chinadrugtrials", _read_html(CTR_LIST_PAGE))


# ── parse_canary_ok：未知源 / 空 HTML ───────────────────────────────────────

def test_unknown_source_key_returns_false_and_warns(caplog):
    """未知 source_key 返回 False 并 log warning（防配置漂移静默放行）。"""
    from core.quality_checks import parse_canary_ok

    with caplog.at_level(logging.WARNING, logger="core.quality_checks"):
        assert not parse_canary_ok("no_such_source", "<html>注册号</html>")
    assert any("no_such_source" in rec.message for rec in caplog.records)


def test_empty_html_returns_false():
    """空 / 纯空白 HTML 一律返回 False，即使 source_key 合法。"""
    from core.quality_checks import CANARY_LABELS, parse_canary_ok

    known_key = next(iter(CANARY_LABELS))
    assert not parse_canary_ok(known_key, "")
    assert not parse_canary_ok(known_key, "   \n  ")


# ── parse_canary_ok：CANARY_MIN_HITS 边界 ──────────────────────────────────

def test_canary_min_hits_boundary():
    """命中 K-1 个标签 → False；命中 K 个 → True（跟随常量而非写死 3）。"""
    from core.quality_checks import CANARY_LABELS, CANARY_MIN_HITS, parse_canary_ok

    labels = CANARY_LABELS["chictr"]
    assert len(labels) >= CANARY_MIN_HITS  # 标签表须足够支撑阈值

    below = "".join(f"<td>{label}</td>"
                    for label in labels[:CANARY_MIN_HITS - 1])
    assert not parse_canary_ok("chictr", below)

    at = "".join(f"<td>{label}</td>" for label in labels[:CANARY_MIN_HITS])
    assert parse_canary_ok("chictr", at)


# ── discovery_funnel_stats：空队列 / 混合 state ────────────────────────────

def test_discovery_funnel_empty_queue(test_db):
    """队列为空返回空结构，不报错。"""
    from core.quality_checks import discovery_funnel_stats

    stats = discovery_funnel_stats()
    assert stats == {"by_source_state": {}, "totals": {}, "conversion_rate": None}


def test_discovery_funnel_mixed_states(test_db):
    """混合 state 下 (source, state) 计数与转化率精确：
    enriched=2, failed=1, skipped=1 → 2/4 = 0.5；pending 不进分母。"""
    from core.quality_checks import discovery_funnel_stats

    _enqueue(test_db, "ChiCTR", "ChiCTR2600123444", "enriched")
    _enqueue(test_db, "ChiCTR", "ChiCTR2600123455", "enriched")
    _enqueue(test_db, "ChiCTR", "ChiCTR2600123466", "pending")
    _enqueue(test_db, "CTR", "CTR20250001", "failed")
    _enqueue(test_db, "CTR", "CTR20250002", "skipped")
    _enqueue(test_db, "CTR", "CTR20250003", "pending")

    stats = discovery_funnel_stats()
    assert stats["totals"] == {"enriched": 2, "pending": 2,
                               "failed": 1, "skipped": 1}
    assert stats["by_source_state"]["ChiCTR"] == {"enriched": 2, "pending": 1}
    assert stats["by_source_state"]["CTR"] == {"failed": 1, "skipped": 1,
                                               "pending": 1}
    assert stats["conversion_rate"] == 0.5


def test_discovery_funnel_only_pending_has_no_conversion_rate(test_db):
    """只有 pending（分母为 0）时转化率为 None 而非除零报错。"""
    from core.quality_checks import discovery_funnel_stats

    _enqueue(test_db, "ChiCTR", "ChiCTR2600123400", "pending")
    stats = discovery_funnel_stats()
    assert stats["totals"] == {"pending": 1}
    assert stats["conversion_rate"] is None


# ── get_quality_summary 的 "discovery" 新键 ────────────────────────────────

def test_quality_summary_adds_discovery_key_without_touching_existing(test_db):
    """summary 新增 discovery 键（挂漏斗精简版），已有键全部保持。"""
    from core.quality_checks import get_quality_summary

    _enqueue(test_db, "ChiCTR", "ChiCTR2600123477", "enriched")
    _enqueue(test_db, "ChiCTR", "ChiCTR2600123488", "failed")

    summary = get_quality_summary()
    # 向后兼容：既有键一个不少
    for legacy_key in ("records_per_source", "total_master_trials",
                       "match_status_counts", "source_failure_rates",
                       "false_merge_candidates", "missed_merge_candidates",
                       "duplicate_records", "multi_master_records",
                       "registry_id_multi_master", "unlinked_records"):
        assert legacy_key in summary
    # 新键内容 = 漏斗精简版
    assert summary["discovery"]["totals"] == {"enriched": 1, "failed": 1}
    assert summary["discovery"]["conversion_rate"] == 0.5
    assert summary["discovery"]["enriched_per_source"] == {"ChiCTR": 1}


# ── quality_report --json 携带 discovery ───────────────────────────────────

def test_quality_report_json_includes_discovery(test_db, monkeypatch, capsys):
    """quality_report --json 的 quality_summary 含 discovery 键且数值正确。

    直接 monkeypatch sys.argv 调 main()（不经 subprocess：子进程不会继承
    test_db 覆盖的临时库路径）。
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "quality_report.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--json"])

    import scripts.quality_report as quality_report

    _enqueue(test_db, "CTR", "CTR20250100", "enriched")
    _enqueue(test_db, "CTR", "CTR20250101", "skipped")

    quality_report.main()
    payload = json.loads(capsys.readouterr().out)

    discovery = payload["quality_summary"]["discovery"]
    assert discovery["totals"] == {"enriched": 1, "skipped": 1}
    assert discovery["conversion_rate"] == 0.5
    assert discovery["enriched_per_source"] == {"CTR": 1}
