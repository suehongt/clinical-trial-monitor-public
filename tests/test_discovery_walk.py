"""三段式发现层契约测试（discover → queue → enrich + 水位线增量）。

覆盖 the project design notes §2.2/2.3/2.4
（Agent-A 部分）与 the project design notes 结论：

  1. 列表解析契约（真实 spike fixture：号严格倒序、详情键齐全）
  2. 倒序走列表页的停止条件（连续 2 页全部已见 → 换词/收尾）
  3. 游标 UPSERT 幂等（同轮跑两次第二遍 queued=0、游标行只有一条）
  4. 任一页失败 → 游标水位不前移、last_successful 不前移、已见号照常入队
  5. discovery_queue INSERT OR IGNORE 幂等（他源路径预插的号不被覆盖）
  6. 4097 字节桩页（号段空穴）→ enrich 按 skipped 处理
  7. enrich 失败 attempts 累计、达上限转 failed
  8. annual_backfill 号段入队分批 + 游标续跑（仅 ChiCTR）
  9. fetch_new_or_updated 兼容包装返回旧结构；legacy 关键词路径仍可调用
  10. _resolve_proj_id 按号搜索兜底：ictrp_diff 入队行（无 proj_id）经
      搜索反查详情键并写回辅助表，enrich 端到端可增强

全部离线：真实 spike fixture + 自造 overlap fixture；网络函数以实例属性
monkeypatch（ChiCTR `_fetch`、CTR `_fetch_page` / `fetch_detail_page`），
数据库走 conftest 的 test_db 临时库。
"""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlparse

import pytest

from collectors.chictr import ChiCTRCollector
from collectors.browser_base import WAFBrowserCollector
from collectors.chinadrugtrials import ChinaDrugTrialsCollector
from db.connection import get_connection

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CHICTR_SPIKE_P1 = os.path.join(_FIXTURE_DIR, "chictr", "spike_search_xin_p1.html")
CHICTR_OVERLAP = os.path.join(_FIXTURE_DIR, "chictr", "walk_overlap.html")
CHICTR_DETAIL = os.path.join(_FIXTURE_DIR, "chictr_detail_standalone.html")
CHICTR_STUB = os.path.join(_FIXTURE_DIR, "chictr", "spike_probe_plus1.html")
CTR_SPIKE_P1 = os.path.join(
    _FIXTURE_DIR, "chinadrugtrials", "spike_search_xin_p1.html")
CTR_OVERLAP = os.path.join(
    _FIXTURE_DIR, "chinadrugtrials", "walk_overlap.html")
CTR_DETAIL = os.path.join(_FIXTURE_DIR, "cde_detail_sample.html")


# ── 工具 ───────────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


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


def _fake_chictr_fetch(list_pages=None, detail_pages=None,
                       fail_list_pages=frozenset(), fail_details=False):
    """构造 ChiCTR `_fetch` 替身：按 URL 区分列表页/详情页。"""
    list_pages = list_pages or {}
    detail_pages = detail_pages or {}

    def fake_fetch(url, timeout_ms=25000):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "showproj" in parsed.path:
            if fail_details:
                raise RuntimeError("WAF blocked detail (injected)")
            proj = qs.get("proj", [""])[0]
            if proj in detail_pages:
                return detail_pages[proj]
            raise RuntimeError(f"no detail fixture for proj={proj}")
        page = int(qs.get("page", ["0"])[0])
        if page in fail_list_pages:
            raise RuntimeError("WAF blocked list page (injected)")
        if page in list_pages:
            return list_pages[page]
        raise RuntimeError(f"no list fixture for page={page}")

    return fake_fetch


def _fake_ctr_fetch_page(list_pages=None):
    """构造 CTR `_fetch_page` 替身（None = WAF/失败）。"""
    list_pages = list_pages or {}

    def fake_fetch_page(url, timeout_ms=None):
        qs = parse_qs(urlparse(url).query)
        page = int(qs.get("currentpage", ["0"])[0])
        return list_pages.get(page)

    return fake_fetch_page


@pytest.fixture
def conn(test_db):
    """conftest 临时库连接（registry_sources 已由 create_schema 播种）。"""
    return get_connection()


@pytest.fixture
def chictr(conn, monkeypatch):
    """ChiCTR 采集器：零延迟 + 单关键词词表（确定性走页）。"""
    from collectors import chictr as chictr_mod

    collector = ChiCTRCollector()
    monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
    monkeypatch.setattr(chictr_mod, "DISCOVERY_WALK_KEYWORDS", ["心"])
    return collector


@pytest.fixture
def ctr(conn, monkeypatch):
    """CTR 采集器：零延迟 + 单关键词词表（确定性走页）。"""
    from collectors import chinadrugtrials as ctr_mod

    collector = ChinaDrugTrialsCollector()
    monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
    monkeypatch.setattr(ctr_mod, "DISCOVERY_WALK_KEYWORDS", ["心"])
    return collector


# ── 词表与解析契约 ──────────────────────────────────────────────────────────


class TestKeywordAndParseContract:
    """广谱词表常量与真实 spike fixture 的列表解析契约。"""

    def test_discovery_walk_keywords_contract(self):
        """两源词表均为非空宽词表，且包含任务指定的广谱字词。"""
        from collectors.chictr import DISCOVERY_WALK_KEYWORDS as k_chictr
        from collectors.chinadrugtrials import (DISCOVERY_WALK_KEYWORDS
                                                as k_ctr)
        for words in (k_chictr, k_ctr):
            assert words and all(isinstance(w, str) and w for w in words)
            for w in ("心", "癌", "细胞", "治疗", "液", "炎"):
                assert w in words

    def test_chictr_list_parse_contract(self, chictr, monkeypatch):
        """真实 spike 列表页：10 条、号严格倒序、proj_id 齐全（Spike S2）。"""
        monkeypatch.setattr(
            chictr, "_fetch",
            lambda url, timeout_ms=25000: _read(CHICTR_SPIKE_P1))
        entries = chictr._search_results_page("心", 1)
        assert len(entries) == 10
        nos = [e["chictr_no"] for e in entries]
        assert nos == sorted(nos, reverse=True)
        assert entries[0]["chictr_no"] == "ChiCTR2600131927"
        assert all(e["proj_id"] for e in entries)

    def test_ctr_list_parse_contract(self, ctr, monkeypatch):
        """真实 spike 列表页：20 条、号严格倒序、uuid 齐全（Spike S3）。"""
        monkeypatch.setattr(
            ctr, "_fetch_page",
            lambda url, timeout_ms=None: _read(CTR_SPIKE_P1))
        entries = ctr._search_results_page("心", 1)
        assert len(entries) == 20
        nos = [e["ctr"] for e in entries]
        assert nos == sorted(nos, reverse=True)
        assert entries[0]["ctr"] == "CTR20263443"
        assert all(e["uuid"] for e in entries)

    def test_stub_detectors(self):
        """ChiCTR 4097 字节桩页被判为桩页；真实详情页不是。"""
        from collectors.chictr import _is_stub_page
        assert _is_stub_page(_read(CHICTR_STUB))
        assert not _is_stub_page(_read(CHICTR_DETAIL))


# ── ChiCTR discover_new（发现 + 水位线）────────────────────────────────────


class TestChiCTRDiscover:
    """discover_new：停止条件、水位线、幂等。"""

    def test_first_round_walks_until_watermark(self, chictr, conn,
                                               monkeypatch):
        """首轮：p1 新号入队，p2/p3 全部已见 → 连续 2 页停止。"""
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)}))
        stats = chictr.discover_new()

        assert stats["queued"] == 10
        assert stats["seen"] == 10
        assert stats["pages_walked"] == 3
        assert stats["stopped_reason"] == "completed"
        assert len(_queue_rows(conn, "ChiCTR")) == 10

        row = conn.execute(
            "SELECT * FROM discovery_cursors WHERE mode = 'list_walk'"
        ).fetchone()
        assert row is not None
        assert row["last_successful"] is not None
        cursor = json.loads(row["cursor_json"])
        assert len(cursor["recent_seen_ids"]) == 10
        assert len(cursor["proj_ids"]) == 10
        assert cursor["proj_ids"]["ChiCTR2600131927"] == "336751"

    def test_second_round_queued_zero_and_cursor_upsert(self, chictr, conn,
                                                        monkeypatch):
        """同轮跑两次：第二遍 queued=0；游标 UPSERT 只有一行。"""
        fake = _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)})
        monkeypatch.setattr(chictr, "_fetch", fake)

        first = chictr.discover_new()
        assert first["queued"] == 10

        second = chictr.discover_new()
        assert second["queued"] == 0
        assert second["stopped_reason"] == "completed"
        # 第二遍 p1/p2 全部已见 → 连续 2 页停止
        assert second["pages_walked"] == 2
        assert len(_queue_rows(conn, "ChiCTR")) == 10
        # UPSERT：游标行只有一条，水位号数不膨胀
        rows = conn.execute(
            "SELECT cursor_json FROM discovery_cursors "
            "WHERE mode = 'list_walk'").fetchall()
        assert len(rows) == 1
        assert len(json.loads(rows[0]["cursor_json"])["recent_seen_ids"]) == 10

    def test_page_failure_keeps_watermark(self, chictr, conn, monkeypatch):
        """任一页失败：水位不前移、last_successful 为空、辅助表保留。"""
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1)},
            fail_list_pages={2}))
        stats = chictr.discover_new()

        assert stats["stopped_reason"] == "page_failed"
        assert stats["pages_walked"] == 1  # p1 成功、p2 失败不计
        assert stats["queued"] == 10       # 失败前已见的号照常入队

        row = conn.execute(
            "SELECT * FROM discovery_cursors WHERE mode = 'list_walk'"
        ).fetchone()
        assert row is not None
        assert row["last_successful"] is None  # 本轮游标不前移
        cursor = json.loads(row["cursor_json"])
        assert cursor["recent_seen_ids"] == []
        assert len(cursor["proj_ids"]) == 10  # 辅助表非水位，失败轮也保留

    def test_rerun_after_failure_enqueue_idempotent(self, chictr, conn,
                                                    monkeypatch):
        """失败后重跑：同一批号再次入队 → INSERT OR IGNORE，queued=0。"""
        fake = _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1)},
            fail_list_pages={2})
        monkeypatch.setattr(chictr, "_fetch", fake)

        chictr.discover_new()
        assert len(_queue_rows(conn, "ChiCTR")) == 10

        second = chictr.discover_new()
        assert second["queued"] == 0
        assert len(_queue_rows(conn, "ChiCTR")) == 10

    def test_last_successful_not_advanced_on_failure(self, chictr, conn,
                                                     monkeypatch):
        """成功轮之后失败轮：last_successful 保持原值。"""
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)}))
        chictr.discover_new()

        conn.execute(
            "UPDATE discovery_cursors SET last_successful = '2000-01-01 "
            "00:00:00' WHERE mode = 'list_walk'")
        conn.commit()

        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            fail_list_pages={1}))
        stats = chictr.discover_new()
        assert stats["stopped_reason"] == "page_failed"

        row = conn.execute(
            "SELECT last_successful, last_attempted FROM discovery_cursors "
            "WHERE mode = 'list_walk'").fetchone()
        assert row["last_successful"] == "2000-01-01 00:00:00"
        assert row["last_attempted"] is not None

    def test_enqueue_insert_or_ignore(self, chictr, conn, monkeypatch):
        """他源路径（ictrp_diff）预插的号：不被覆盖、不计 queued。"""
        conn.execute(
            "INSERT INTO discovery_queue "
            "(source_id, source_trial_id, discovered_via) VALUES (?, ?, ?)",
            (_sid(conn, "ChiCTR"), "ChiCTR2600131927", "ictrp_diff"),
        )
        conn.commit()

        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)}))
        stats = chictr.discover_new()

        assert stats["queued"] == 9  # 10 个号里 1 个已存在
        rows = _queue_rows(conn, "ChiCTR")
        assert len(rows) == 10
        kept = [r for r in rows if r["source_trial_id"] == "ChiCTR2600131927"]
        assert kept[0]["discovered_via"] == "ictrp_diff"  # 原路径保留


# ── ChiCTR enrich_pending（增强）───────────────────────────────────────────


class TestChiCTREnrich:
    """enrich_pending：全链路 upsert、桩页 skip、attempts 累计转 failed。"""

    def _seed_one(self, collector, conn, chictr_no="ChiCTR2600128415",
                  proj_id="291119"):
        collector._enqueue(chictr_no, "list_walk")
        collector._save_cursor(
            "list_walk",
            {"recent_seen_ids": [chictr_no],
             "proj_ids": {chictr_no: proj_id}},
            success=True,
        )

    def test_enrich_full_chain_upserts_record(self, chictr, conn,
                                              monkeypatch):
        """详情抓取 → parse_detail_page → normalise → upsert → enriched。"""
        self._seed_one(chictr, conn)
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            detail_pages={"291119": _read(CHICTR_DETAIL)}))

        stats = chictr.enrich_pending()
        assert stats["enriched"] == 1
        assert stats["failed"] == 0
        assert stats["skipped"] == 0
        assert stats["records"][0]["chictr_no"] == "ChiCTR2600128415"

        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["state"] == "enriched"
        assert row["attempts"] == 0

        rec = conn.execute(
            "SELECT source_trial_id, title FROM registry_records "
            "WHERE source_id = ? AND is_latest = 1",
            (_sid(conn, "ChiCTR"),),
        ).fetchone()
        assert rec["source_trial_id"] == "ChiCTR2600128415"
        assert "克隆性造血" in rec["title"]

        # 已消费的 proj_id 辅助表条目被清理
        cursor = chictr._load_cursor("list_walk")
        assert "ChiCTR2600131927" not in cursor["proj_ids"]

    def test_enrich_stub_page_skipped(self, chictr, conn, monkeypatch):
        """4097 字节桩页（号段空穴）→ state=skipped，不入库。"""
        self._seed_one(chictr, conn)
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            detail_pages={"291119": _read(CHICTR_STUB)}))

        stats = chictr.enrich_pending()
        assert stats["skipped"] == 1
        assert stats["enriched"] == 0
        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["state"] == "skipped"
        count = conn.execute(
            "SELECT COUNT(*) FROM registry_records").fetchone()[0]
        assert count == 0

    def test_enrich_attempts_accumulate_to_failed(self, chictr, conn,
                                                  monkeypatch):
        """连续失败：attempts 累计，第 3 次（≥上限）转 failed，之后不再选。"""
        self._seed_one(chictr, conn)
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            fail_details=True))

        for round_no in range(1, 3):  # 前两轮：pending + attempts 累加
            stats = chictr.enrich_pending()
            assert stats["failed"] == 1
            row = _queue_rows(conn, "ChiCTR")[0]
            assert row["state"] == "pending"
            assert row["attempts"] == round_no

        stats = chictr.enrich_pending()  # 第 3 次：attempts=3 → failed
        assert stats["failed"] == 1
        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["state"] == "failed"
        assert row["attempts"] == 3
        assert "WAF" in row["last_error"]

        stats = chictr.enrich_pending()  # failed 行不再被选中
        assert stats == {"enriched": 0, "failed": 0, "skipped": 0,
                         "records": []}

    def test_enrich_respects_limit(self, chictr, conn, monkeypatch):
        """limit 只消化前 N 条 pending（ORDER BY discovery_id）。"""
        detail_html = _read(CHICTR_DETAIL)
        proj_map: dict = {}
        for no, proj in (("ChiCTR2600131927", "336751"),
                         ("ChiCTR2600131926", "323897"),
                         ("ChiCTR2600131920", "341911")):
            chictr._enqueue(no, "list_walk")
            proj_map[no] = proj
        chictr._save_cursor(
            "list_walk",
            {"recent_seen_ids": [], "proj_ids": proj_map},
            success=True,
        )
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            detail_pages={"336751": detail_html,
                          "323897": detail_html,
                          "341911": detail_html}))

        stats = chictr.enrich_pending(limit=2)
        assert stats["enriched"] == 2
        rows = _queue_rows(conn, "ChiCTR")
        assert rows[0]["state"] == "enriched"
        assert rows[1]["state"] == "enriched"
        assert rows[2]["state"] == "pending"
        assert rows[2]["attempts"] == 0

    def test_enrich_id_probe_row_uses_numeric_proj(self, chictr, conn,
                                                   monkeypatch):
        """annual_backfill 的纯数字占位行：proj 即详情键，真实注册号入库。"""
        result = chictr.annual_backfill(2026, start_proj=336751,
                                        end_proj=336751, batch=5)
        assert result["queued"] == 1
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            detail_pages={"336751": _read(CHICTR_DETAIL)}))

        stats = chictr.enrich_pending()
        assert stats["enriched"] == 1
        rec = conn.execute(
            "SELECT source_trial_id FROM registry_records "
            "WHERE source_id = ? AND is_latest = 1",
            (_sid(conn, "ChiCTR"),),
        ).fetchone()
        assert rec["source_trial_id"] == "ChiCTR2600128415"

    def test_enrich_zero_keyword_match_still_fills_budget(
            self, chictr, conn, monkeypatch):
        """关键词 0 命中时预算照常回填未命中行（M2 案例 enriched=0 根因）。"""
        self._seed_one(chictr, conn)  # 标题来自 fixture，不含关键词 "疟疾"
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            detail_pages={"291119": _read(CHICTR_DETAIL)}))

        stats = chictr.enrich_pending(keywords=["疟疾"])
        assert stats["keyword_matched"] == 0
        assert stats["enriched"] == 1  # 回填分支生效，预算未浪费
        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["state"] == "enriched"


class TestChiCTRProjIdFallback:
    """_resolve_proj_id 按号检索兜底（ictrp_diff 入队行的可增强性）。

    ictrp_diff 从 ICTRP 快照入队，只有注册号没有 proj_id，且不经过
    list_walk（辅助表/历史 source_url 都查不到）——只能走 regno 精确
    检索反查（title 搜索不匹配注册号，实测 searchproj 仅 regno 有效）。
    """

    # 与 CHICTR_DETAIL 配对的最小搜索页：ChiCTR2600128415 → proj 291119
    SEARCH_PAGE_8415 = (
        "<html><body><table class='table1'>"
        "<tr><th>#</th><th>注册号</th><th>题目</th><th>注册时间</th></tr>"
        "<tr><td>1</td><td>ChiCTR2600128415</td>"
        "<td><a href='showproj.html?proj=291119'>克隆性造血试验</a></td>"
        "<td>2026-01-01</td></tr>"
        "</table></body></html>"
    )

    def test_resolve_via_regno_lookup_and_cache(self, chictr, conn,
                                                monkeypatch):
        """辅助表/DB 都查不到 → regno 检索 1 页反查，并写回辅助表。"""
        seen_urls: list[str] = []

        def fake_fetch(url, timeout_ms=25000):
            seen_urls.append(url)
            return self.SEARCH_PAGE_8415

        chictr._enqueue("ChiCTR2600128415", "ictrp_diff")
        assert chictr._load_cursor("list_walk").get("proj_ids", {}) == {}
        monkeypatch.setattr(chictr, "_fetch", fake_fetch)

        assert chictr._resolve_proj_id("ChiCTR2600128415") == "291119"
        # 反查必须走 regno 参数（title 搜索不匹配注册号，站点实测契约）
        assert any("regno=ChiCTR2600128415" in u for u in seen_urls)
        # 写回辅助表：后续重试不再重复检索
        cursor = chictr._load_cursor("list_walk")
        assert cursor["proj_ids"]["ChiCTR2600128415"] == "291119"

    def test_resolve_no_match_returns_none(self, chictr, conn, monkeypatch):
        """搜索页上没有该号 → 返回 None（调用方按解析失败计 attempts）。"""
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1)}))
        assert chictr._resolve_proj_id("ChiCTR9999999999") is None

    def test_resolve_search_failure_returns_none(self, chictr, conn,
                                                 monkeypatch):
        """搜索请求本身失败（WAF）→ 返回 None，不抛异常打断 enrich 循环。"""
        def broken_fetch(url, timeout_ms=25000):
            raise RuntimeError("WAF blocked (injected)")

        monkeypatch.setattr(chictr, "_fetch", broken_fetch)
        assert chictr._resolve_proj_id("ChiCTR2600131927") is None

    def test_enrich_ictrp_diff_row_end_to_end(self, chictr, conn,
                                              monkeypatch):
        """端到端：ictrp_diff 入队（无 proj_id）→ regno 反查 → 详情 upsert。"""
        chictr._enqueue("ChiCTR2600128415", "ictrp_diff")
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: self.SEARCH_PAGE_8415},
            detail_pages={"291119": _read(CHICTR_DETAIL)}))

        stats = chictr.enrich_pending()
        assert stats["enriched"] == 1
        assert stats["failed"] == 0
        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["state"] == "enriched"
        assert row["discovered_via"] == "ictrp_diff"
        rec = conn.execute(
            "SELECT source_trial_id FROM registry_records "
            "WHERE source_id = ? AND is_latest = 1",
            (_sid(conn, "ChiCTR"),),
        ).fetchone()
        assert rec["source_trial_id"] == "ChiCTR2600128415"


class TestChiCTRAnnualBackfill:
    """annual_backfill：号段分批入队 + id_probe 游标续跑（仅写队列）。"""

    def test_batches_and_cursor_resume(self, chictr, conn):
        r1 = chictr.annual_backfill(2026, batch=20)
        assert r1 == {"queued": 20, "year": 2026, "from_proj": 280000,
                      "to_proj": 280019, "done": False}

        r2 = chictr.annual_backfill(2026, batch=20)
        assert r2["from_proj"] == 280020  # 游标续跑，不重复
        assert r2["queued"] == 20

        rows = _queue_rows(conn, "ChiCTR")
        assert len(rows) == 40
        assert {r["discovered_via"] for r in rows} == {"id_probe"}
        assert {r["state"] for r in rows} == {"pending"}

        # 尾段 + 显式边界 → done
        r3 = chictr.annual_backfill(2026, start_proj=349995,
                                    end_proj=350000, batch=20)
        assert r3["queued"] == 6
        assert r3["to_proj"] == 350000
        assert r3["done"] is True

        cursor = chictr._load_cursor("id_probe")
        assert cursor["last_complete_proj"] == 350000
        assert cursor["year"] == 2026

    def test_unknown_year_requires_bounds(self, chictr):
        with pytest.raises(ValueError):
            chictr.annual_backfill(2030)


# ── ChiCTR 兼容包装与 legacy 回退 ──────────────────────────────────────────


class TestChiCTRCompat:
    """fetch_new_or_updated 三段式包装 + legacy 关键词路径保留。"""

    def test_fetch_wrapper_returns_legacy_shape(self, chictr, conn,
                                                monkeypatch):
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)},
            detail_pages={"336751": _read(CHICTR_DETAIL)}))

        raws = chictr.fetch_new_or_updated()
        assert len(raws) == 1
        assert set(raws[0]) == {"chictr_no", "proj_id", "html",
                                "parsed_fields"}
        # 解析器保留等长种子号（队列源生号），详情字段来自真实 fixture
        assert raws[0]["chictr_no"] == "ChiCTR2600131927"
        assert raws[0]["parsed_fields"]["source_trial_id"] == "ChiCTR2600131927"

    def test_legacy_path_preserved(self, chictr, conn, monkeypatch):
        """legacy 关键词路径可调用、返回旧结构、且不写 discovery_queue。"""
        from collectors import chictr as chictr_mod

        monkeypatch.setattr(chictr_mod, "BOOTSTRAP_KEYWORDS", ["心"])
        monkeypatch.setattr(chictr, "_search_total_count", lambda kw: 15)
        monkeypatch.setattr(
            chictr, "_search_results_page",
            lambda kw, page: [{"chictr_no": "ChiCTR2600128415",
                               "title": "t", "proj_id": "291119"}]
            if page == 1 else [])
        monkeypatch.setattr(
            chictr, "_fetch",
            lambda url, timeout_ms=25000: _read(CHICTR_DETAIL))

        assert callable(chictr.fetch_new_or_updated_legacy)
        raws = chictr.fetch_new_or_updated_legacy()
        assert len(raws) == 1
        assert raws[0]["chictr_no"] == "ChiCTR2600128415"
        assert raws[0]["parsed_fields"]["title"]
        assert _queue_rows(conn, "ChiCTR") == []


# ── CTR（chinadrugtrials）──────────────────────────────────────────────────


class TestCTRDiscover:
    """CTR discover_new：与 ChiCTR 同构的停止条件与水位线。"""

    def test_first_round_walks_until_watermark(self, ctr, conn, monkeypatch):
        monkeypatch.setattr(ctr, "_fetch_page", _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1),
             2: _read(CTR_OVERLAP),
             3: _read(CTR_OVERLAP)}))
        stats = ctr.discover_new()

        assert stats["queued"] == 20
        assert stats["seen"] == 20
        assert stats["pages_walked"] == 3
        assert stats["stopped_reason"] == "completed"
        assert len(_queue_rows(conn, "CTR")) == 20

        row = conn.execute(
            "SELECT * FROM discovery_cursors WHERE mode = 'list_walk'"
        ).fetchone()
        assert row is not None
        assert row["last_successful"] is not None
        cursor = json.loads(row["cursor_json"])
        assert len(cursor["recent_seen_ids"]) == 20
        key = cursor["detail_keys"]["CTR20263443"]
        assert key["uuid"] == "ea080a27b37f47a5b9f1794e5f078a50"

    def test_second_round_queued_zero(self, ctr, conn, monkeypatch):
        fake = _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1),
             2: _read(CTR_OVERLAP),
             3: _read(CTR_OVERLAP)})
        monkeypatch.setattr(ctr, "_fetch_page", fake)

        assert ctr.discover_new()["queued"] == 20
        second = ctr.discover_new()
        assert second["queued"] == 0
        assert second["pages_walked"] == 2
        assert len(_queue_rows(conn, "CTR")) == 20
        rows = conn.execute(
            "SELECT cursor_json FROM discovery_cursors "
            "WHERE mode = 'list_walk'").fetchall()
        assert len(rows) == 1  # UPSERT 幂等

    def test_page_failure_keeps_watermark(self, ctr, conn, monkeypatch):
        monkeypatch.setattr(ctr, "_fetch_page", _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1)}))  # p2 返回 None → 失败
        stats = ctr.discover_new()

        assert stats["stopped_reason"] == "page_failed"
        assert stats["queued"] == 20  # p1 已见号照常入队
        row = conn.execute(
            "SELECT cursor_json, last_successful FROM discovery_cursors "
            "WHERE mode = 'list_walk'").fetchone()
        assert row["last_successful"] is None
        cursor = json.loads(row["cursor_json"])
        assert cursor["recent_seen_ids"] == []
        assert len(cursor["detail_keys"]) == 20


class TestCTREnrich:
    """CTR enrich_pending：limit、桩页 skip、attempts 累计。"""

    def test_enrich_mixed_states_with_limit(self, ctr, conn, monkeypatch):
        """limit=2：第 1 条真实详情 → enriched，第 2 条空穴短页 → skipped。"""
        rows = conn.execute(
            "SELECT source_trial_id FROM discovery_queue "
            "ORDER BY discovery_id").fetchall()
        assert rows == []  # 前置：先 discover 建队列

        monkeypatch.setattr(ctr, "_fetch_page", _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1)}))
        ctr.discover_new()

        uuid_first = "ea080a27b37f47a5b9f1794e5f078a50"
        monkeypatch.setattr(
            ctr, "fetch_detail_page",
            lambda uuid, index="1": _read(CTR_DETAIL)
            if uuid == uuid_first else "<html><body>系统异常</body></html>")

        stats = ctr.enrich_pending(limit=2)
        assert stats["enriched"] == 1
        assert stats["skipped"] == 1

        rows = _queue_rows(conn, "CTR")
        assert rows[0]["state"] == "enriched"
        assert rows[1]["state"] == "skipped"
        rec = conn.execute(
            "SELECT source_trial_id FROM registry_records "
            "WHERE source_id = ? AND is_latest = 1",
            (_sid(conn, "CTR"),),
        ).fetchone()
        assert rec["source_trial_id"] == "CTR20263443"

    def test_enrich_attempts_accumulate_to_failed(self, ctr, conn,
                                                  monkeypatch):
        monkeypatch.setattr(ctr, "_fetch_page", _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1)}))
        ctr.discover_new()
        monkeypatch.setattr(ctr, "fetch_detail_page",
                            lambda uuid, index="1": None)  # WAF 拦截

        for round_no in range(1, 3):
            ctr.enrich_pending(limit=1)
            row = _queue_rows(conn, "CTR")[0]
            assert row["state"] == "pending"
            assert row["attempts"] == round_no

        ctr.enrich_pending(limit=1)
        row = _queue_rows(conn, "CTR")[0]
        assert row["state"] == "failed"
        assert row["attempts"] == 3


class TestCTRCompat:
    """CTR 兼容包装 / legacy 回退 / annual_backfill 仅 ChiCTR。"""

    def test_fetch_wrapper_returns_legacy_shape(self, ctr, monkeypatch):
        uuid_first = "ea080a27b37f47a5b9f1794e5f078a50"
        monkeypatch.setattr(ctr, "_fetch_page", _fake_ctr_fetch_page(
            {1: _read(CTR_SPIKE_P1),
             2: _read(CTR_OVERLAP),
             3: _read(CTR_OVERLAP)}))
        monkeypatch.setattr(
            ctr, "fetch_detail_page",
            lambda uuid, index="1": _read(CTR_DETAIL)
            if uuid == uuid_first else None)

        raws = ctr.fetch_new_or_updated()
        assert len(raws) == 1
        assert set(raws[0]) == {"ctr_number", "uuid", "html",
                                "parsed_fields"}

    def test_legacy_path_preserved(self, ctr, conn, monkeypatch):
        entry = {"uuid": "ea080a27b37f47a5b9f1794e5f078a50",
                 "ctr": "CTR20263443", "index": "1", "status": "",
                 "drug_name": "", "conditions": "", "title": "t"}
        monkeypatch.setattr(ctr, "_search_keyword", lambda kw, max_pages=5: [entry])
        monkeypatch.setattr(
            ctr, "fetch_detail_page",
            lambda uuid, index="1": _read(CTR_DETAIL))

        assert callable(ctr.fetch_new_or_updated_legacy)
        raws = ctr.fetch_new_or_updated_legacy()
        assert len(raws) == 1
        assert raws[0]["ctr_number"] == "CTR20263443"
        assert _queue_rows(conn, "CTR") == []  # legacy 不写队列

    def test_no_annual_backfill_on_ctr(self):
        """Spike：号段探测仅适用于 ChiCTR，CTR 不提供 annual_backfill。"""
        assert not hasattr(ChinaDrugTrialsCollector, "annual_backfill")


# ── 标题入队 + 关键词精准增强（myocarditis 案例后的流程优化）───────────────


class TestTitleCaptureAndKeywordEnrich:
    """发现时存列表页标题；enrich 支持关键词精准优先 + 预算回填。"""

    def test_discover_stores_list_page_title(self, chictr, conn,
                                             monkeypatch):
        """discover_new 把列表页解析出的 title 原样写入队列。"""
        monkeypatch.setattr(chictr, "_fetch", _fake_chictr_fetch(
            list_pages={1: _read(CHICTR_SPIKE_P1),
                        2: _read(CHICTR_OVERLAP),
                        3: _read(CHICTR_OVERLAP)}))
        chictr.discover_new()

        rows = _queue_rows(conn, "ChiCTR")
        assert len(rows) == 10
        entries = chictr._search_results_page("心", 1)
        by_no = {e["chictr_no"]: e["title"] for e in entries}
        for r in rows:
            assert r["title"] == by_no[r["source_trial_id"]]

    def test_enqueue_backfills_title_on_existing_row(self, chictr, conn):
        """先由 ictrp_diff 无标题入队的行，列表页再见时回填标题且来源不变。"""
        no = "ChiCTR2600131927"
        assert chictr._enqueue(no, "ictrp_diff") is True
        assert chictr._enqueue(no, "list_walk", title="心肌炎免疫治疗研究") is False
        row = _queue_rows(conn, "ChiCTR")[0]
        assert row["discovered_via"] == "ictrp_diff"
        assert row["title"] == "心肌炎免疫治疗研究"

    def test_enrich_keyword_priority_with_filler_backfill(self, chictr, conn,
                                                          monkeypatch):
        """keywords 命中行先增强；预算剩余用未命中行回填（不浪费预算）。"""
        for no, title in [("ChiCTR2600000001", "心肌炎的免疫治疗研究"),
                          ("ChiCTR2600000002", "急性心肌炎队列登记"),
                          ("ChiCTR2600000003", "糖尿病足溃疡"),
                          ("ChiCTR2600000004", "腰椎间盘突出"),
                          ("ChiCTR2600000005", None)]:
            chictr._enqueue(no, "list_walk", title=title)

        def fail_fetch(url, timeout_ms=25000):
            raise RuntimeError("injected detail failure")

        monkeypatch.setattr(chictr, "_fetch", fail_fetch)
        stats = chictr.enrich_pending(limit=3, keywords=["心肌炎"])

        # 2 行标题命中优先选中；剩余 1 条预算由 discovery_id 最小的未命中行回填
        assert stats["keyword_matched"] == 2
        assert stats["enriched"] == 0
        attempted = {r["source_trial_id"] for r in _queue_rows(conn, "ChiCTR")
                     if r["attempts"] > 0}
        assert attempted == {"ChiCTR2600000001", "ChiCTR2600000002",
                             "ChiCTR2600000003"}

    def test_enrich_without_keywords_keeps_fifo(self, chictr, conn):
        """不给 keywords 时保持旧行为：严格按 discovery_id 先进先增强。"""
        for no, title in [("ChiCTR2600000011", "心肌炎研究"),
                          ("ChiCTR2600000012", "无关试验")]:
            chictr._enqueue(no, "list_walk", title=title)

        def fail_fetch(url, timeout_ms=25000):
            raise RuntimeError("injected detail failure")

        chictr._fetch = fail_fetch
        stats = chictr.enrich_pending(limit=1)
        assert "keyword_matched" not in stats
        attempted = {r["source_trial_id"] for r in _queue_rows(conn, "ChiCTR")
                     if r["attempts"] > 0}
        assert attempted == {"ChiCTR2600000011"}


class TestCTRDetailKeyFallback:
    """_resolve_detail_key 按号检索兜底（ictrp_diff 入队行的可增强性）。

    与 ChiCTR 的 regno 反查同构：ictrp_diff 入队只有登记号没有 uuid，
    辅助表/历史 source_url 都查不到时走 keywords 检索反查详情键。
    """

    # 与 CDE 详情页配对的最小搜索页：CTR202630015 → uuid-ctr-30015
    SEARCH_PAGE_30015 = (
        "<html><body><table class='searchTable'>"
        "<tr><th>#</th><th>登记号</th><th>状态</th><th>药物名称</th>"
        "<th>适应症</th><th>题目</th></tr>"
        "<tr><td>1</td>"
        "<td><a id='uuid-ctr-30015' name='1'>CTR202630015</a></td>"
        "<td>进行中</td><td>药</td><td>症</td><td>心肌病试验</td></tr>"
        "</table></body></html>"
    )

    def test_resolve_via_regno_lookup_and_cache(self, ctr, conn,
                                                monkeypatch):
        """辅助表/DB 都查不到 → keywords 检索 1 页反查，并写回辅助表。"""
        seen_urls: list[str] = []

        def fake_fetch_page(url, timeout_ms=None):
            seen_urls.append(url)
            return self.SEARCH_PAGE_30015

        ctr._enqueue("CTR202630015", "ictrp_diff")
        assert ctr._load_cursor("list_walk").get("detail_keys", {}) == {}
        monkeypatch.setattr(ctr, "_fetch_page", fake_fetch_page)

        key = ctr._resolve_detail_key("CTR202630015")
        assert key == {"uuid": "uuid-ctr-30015", "index": "1"}
        # 反查必须带完整登记号（keywords 参数命中登记号）
        assert any("keywords=CTR202630015" in u for u in seen_urls)
        # 写回辅助表：后续重试不再重复检索
        cursor = ctr._load_cursor("list_walk")
        assert cursor["detail_keys"]["CTR202630015"] == {
            "uuid": "uuid-ctr-30015", "index": "1"}

    def test_resolve_no_match_returns_none(self, ctr, conn, monkeypatch):
        """搜索页上没有该号 → 返回 None（调用方按解析失败计 attempts）。"""
        monkeypatch.setattr(
            ctr, "_fetch_page",
            lambda url, timeout_ms=None: "<html><body><p>empty</p></body></html>")
        assert ctr._resolve_detail_key("CTR9999999999") is None

    def test_resolve_search_failure_returns_none(self, ctr, conn,
                                                 monkeypatch):
        """搜索请求本身失败（普通异常）→ 返回 None，不打断 enrich 循环。"""
        def broken_fetch_page(url, timeout_ms=None):
            raise RuntimeError("WAF blocked (injected)")

        monkeypatch.setattr(ctr, "_fetch_page", broken_fetch_page)
        assert ctr._resolve_detail_key("CTR202630016") is None

    def test_enrich_ictrp_diff_row_end_to_end(self, ctr, conn, monkeypatch):
        """端到端：ictrp_diff 入队（无 uuid）→ 按号反查 → 详情 upsert。"""
        ctr._enqueue("CTR202630015", "ictrp_diff")
        monkeypatch.setattr(
            ctr, "_fetch_page",
            lambda url, timeout_ms=None: self.SEARCH_PAGE_30015)
        monkeypatch.setattr(
            ctr, "fetch_detail_page",
            lambda uuid, index="1": _read(CTR_DETAIL))

        stats = ctr.enrich_pending()
        assert stats["enriched"] == 1
        assert stats["failed"] == 0
        row = _queue_rows(conn, "CTR")[0]
        assert row["state"] == "enriched"
        assert row["discovered_via"] == "ictrp_diff"


class TestCTRHttpHotPath:
    """CTR HTTP 热路径（王苏两段式 cookie）：202→带 cookie 重放→200。

    实测依据 2026-09-23：首 GET 返回 202+Set-Cookie，会话带 cookie 立即
    重放即 200 真实内容；连续 3 次失败降级浏览器路径。
    """

    # 足够长以通过 _has_cde_content（>5000B 且命中 ≥2 个内容标记）
    FAKE_CONTENT = ("<html><body>" + "<p>药物临床试验登记与信息公示平台 "
                    "登记号 CTR202630015 适应症 心肌病</p>" * 120 +
                    "</body></html>")

    class _FakeResp:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    @pytest.fixture(autouse=True)
    def _stub_browser_base(self, monkeypatch):
        """隔离浏览器基类：降级路径不真的启动 Playwright。"""
        monkeypatch.setattr(
            WAFBrowserCollector, "_fetch_page",
            lambda self, url, timeout_ms=None: TestCTRHttpHotPath.FAKE_CONTENT)

    def test_two_stage_202_then_200(self, ctr, monkeypatch):
        """202 挑战 → 同会话重放 → 200 内容（两段式核心）。"""
        calls = {"n": 0}

        class TwoStageSession:
            headers = {}
            def get(self, url, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return TestCTRHttpHotPath._FakeResp(
                        202, "<html>challenge</html>")
                return TestCTRHttpHotPath._FakeResp(
                    200, TestCTRHttpHotPath.FAKE_CONTENT)

        monkeypatch.setattr(ctr, "_http_session", lambda: TwoStageSession())
        html = ctr._http_get("https://example.org/x")
        assert html == TestCTRHttpHotPath.FAKE_CONTENT
        assert calls["n"] == 2  # 恰好两次请求，cookie 由会话自动携带

    def test_failure_then_fallback_and_degrade(self, ctr, monkeypatch):
        """HTTP 连续 3 次失败 → 降级标志置位 → 后续直接走浏览器。"""
        class DeadSession:
            headers = {}
            def get(self, url, timeout=None):
                raise RuntimeError("connection reset (injected)")

        monkeypatch.setattr(ctr, "_http_session", lambda: DeadSession())
        for _ in range(3):
            assert ctr._fetch_page("https://example.org/x") is not None
        assert ctr._http_degraded is True

    def test_success_resets_failure_count(self, ctr, monkeypatch):
        """成功清零失败计数——偶发失败不会累积到降级。"""
        state = {"n": 0}

        class FlakySession:
            headers = {}
            def get(self, url, timeout=None):
                state["n"] += 1
                if state["n"] % 2 == 1:
                    raise RuntimeError("transient (injected)")
                return TestCTRHttpHotPath._FakeResp(
                    200, TestCTRHttpHotPath.FAKE_CONTENT)

        monkeypatch.setattr(ctr, "_http_session", lambda: FlakySession())
        for _ in range(6):
            assert ctr._fetch_page("https://example.org/x") is not None
            assert ctr._http_failures <= 1, "成功必须清零失败计数"
        assert not ctr._http_degraded


class TestParallelEnrichWorkers:
    """多会话并行 enrich（cookie 池）：行级结果与串行一致，会话线程隔离。"""

    def test_chictr_parallel_enrich_end_to_end(self, chictr, conn,
                                               monkeypatch):
        """3 行 × workers=2：全部 enriched，stats 聚合正确。"""
        import re as _re
        # 每行一个独立注册号（夹具 HTML 的号按行改写，否则并发 upsert
        # 同一记录会撞版本唯一约束——生产中各行本就是不同试验）
        base_detail = _read(CHICTR_DETAIL)
        m = _re.search(r"ChiCTR\d+", base_detail)
        base_no = m.group(0) if m else None

        def detail_for(no: str) -> str:
            return base_detail.replace(base_no, no) if base_no else base_detail

        nos = [f"ChiCTR26009{i:04d}" for i in range(3)]
        for no in nos:
            chictr._enqueue(no, "ictrp_diff", title="心肌病试验")
        monkeypatch.setattr(
            chictr, "_resolve_proj_id",
            lambda no: nos.index(no) + 30001)
        monkeypatch.setattr(
            chictr, "_fetch",
            lambda url, timeout_ms=25000: detail_for(
                nos[int(url.rsplit("proj=", 1)[1]) - 30001]))

        stats = chictr.enrich_pending(workers=2)
        assert stats["workers"] == 2
        assert stats["enriched"] == 3
        assert stats["failed"] == 0
        rows = _queue_rows(conn, "ChiCTR")
        assert all(r["state"] == "enriched" for r in rows)

    def test_chictr_http_client_thread_local(self, chictr, monkeypatch):
        """每线程独立 WafHttpClient（cookie 池基础）。"""
        import threading
        chictr._http_enabled = True
        seen = []  # 持有对象引用：只存 id() 会在实例回收后被地址复用伪造相等
        orig = chictr._get_http
        def spy():
            c = orig()
            seen.append((threading.get_ident(), c))
            return c
        monkeypatch.setattr(chictr, "_get_http", spy)
        threads = [threading.Thread(target=chictr._get_http)
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {id(c) for _, c in seen}
        assert len(ids) == 3  # 三个线程三个客户端实例

    def test_ctr_parallel_enrich_end_to_end(self, ctr, conn, monkeypatch):
        """3 行 × workers=2：全部 enriched。"""
        for i in range(3):
            ctr._enqueue(f"CTR20263{i:04d}", "ictrp_diff", title="心肌病试验")
        monkeypatch.setattr(
            ctr, "_resolve_detail_key",
            lambda no: {"uuid": f"uuid-{no}", "index": "1"})
        monkeypatch.setattr(
            ctr, "fetch_detail_page",
            lambda uuid, index="1": _read(CTR_DETAIL))

        stats = ctr.enrich_pending(workers=2)
        assert stats["workers"] == 2
        assert stats["enriched"] == 3
        assert stats["failed"] == 0

    def test_ctr_http_session_thread_local(self, ctr):
        """每线程独立 requests 会话（两段式 cookie 互不串扰）。"""
        import threading
        results = {}  # 持有对象引用：只存 id() 会在实例回收后被地址复用伪造相等
        def grab(name):
            results[name] = ctr._http_session()
        ts = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len({id(s) for s in results.values()}) == 3
