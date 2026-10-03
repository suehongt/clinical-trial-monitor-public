"""
Tests for the live source-check path (tier 1/3 实时检索).

Covers:
  1. ChiCTRCollector.live_search — stubbed list pages: entries normalised,
     rows queued (discovered_via='live_search'), proj_id aux map written,
     list_walk watermark untouched
  2. ChinaDrugTrialsCollector.live_search — same for the uuid detail_keys map
  3. POST /api/trials/live-check — ok payload shape, cooldown cache,
     422 on empty q, circuit_open pass-through, in_library marking against
     registry_records
"""
from __future__ import annotations

import pytest

from core.waf_guard import WafCircuitOpenError
from db.connection import get_connection


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def conn(test_db):
    """conftest 临时库连接（registry_sources 已由 create_schema 播种）。"""
    return get_connection()


@pytest.fixture
def chictr(conn, monkeypatch):
    from collectors.chictr import ChiCTRCollector
    collector = ChiCTRCollector()
    monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
    return collector


@pytest.fixture
def ctr(conn, monkeypatch):
    from collectors.chinadrugtrials import ChinaDrugTrialsCollector
    collector = ChinaDrugTrialsCollector()
    monkeypatch.setattr(collector.cfg, "request_delay_sec", 0)
    return collector


def _source_id(conn, short_name: str) -> int:
    return conn.execute(
        "SELECT source_id FROM registry_sources WHERE short_name = ?",
        (short_name,),
    ).fetchone()["source_id"]


# ── Collector 层：live_search ──────────────────────────────────────────────


def test_chictr_live_search_queues_rows_and_keeps_watermark(
        chictr, conn, monkeypatch):
    pages = {
        1: [
            {"chictr_no": "ChiCTR26000001", "title": "心肌炎 CAR-T 研究",
             "proj_id": "31001"},
            {"chictr_no": "ChiCTR26000002", "title": "心肌炎 MRI 队列",
             "proj_id": "31002"},
        ],
    }
    monkeypatch.setattr(
        chictr, "_search_results_page",
        lambda keyword, page=1: list(pages.get(page, [])))

    stats = chictr.live_search("心肌炎", max_pages=1)

    assert stats["found"] == 2
    assert stats["queued"] == 2
    assert stats["pages_walked"] == 1
    assert stats["stopped_reason"] == "completed"
    assert [e["source_trial_id"] for e in stats["entries"]] == [
        "ChiCTR26000001", "ChiCTR26000002"]
    assert stats["entries"][0]["url"].endswith("proj=31001")
    assert stats["entries"][1]["title"] == "心肌炎 MRI 队列"

    # 队列：命中行全部入队，via=live_search
    rows = conn.execute(
        "SELECT source_trial_id, discovered_via, state FROM discovery_queue "
        "WHERE source_id = ?", (chictr.source_id,)).fetchall()
    assert {r["source_trial_id"] for r in rows} == {
        "ChiCTR26000001", "ChiCTR26000002"}
    assert {r["discovered_via"] for r in rows} == {"live_search"}
    assert {r["state"] for r in rows} == {"pending"}

    # proj_id 辅助表已写入（enrich 侧消费），水位线未被 live 路径触碰
    cursor = chictr._load_cursor("list_walk")
    assert cursor["proj_ids"]["ChiCTR26000001"] == "31001"
    assert cursor.get("recent_seen_ids") == []


def test_chictr_live_search_circuit_open_propagates(chictr, conn, monkeypatch):
    def boom(keyword, page=1):
        raise WafCircuitOpenError("chictr", 3, 3)
    monkeypatch.setattr(chictr, "_search_results_page", boom)
    with pytest.raises(WafCircuitOpenError):
        chictr.live_search("心肌炎", max_pages=1)
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM discovery_queue WHERE source_id = ?",
        (chictr.source_id,)).fetchone()
    assert rows["n"] == 0


# ── 原站总命中数（site_total）：面板显示「原站共 N 条」────────────────────

# 带总数标记的最小搜索页（#data-total 同真实站点结构，含千分位逗号）
PAGE_WITH_TOTAL = (
    "<html><head><title>检索</title></head><body>"
    "<span id='data-total'>1,307</span>"
    "<table class='table1'>"
    "<tr><td>1</td><td>ChiCTR2600128415</td>"
    "<td><a href='showproj.html?proj=291119'>某试验</a></td>"
    "<td>2026-01-01</td></tr></table></body></html>"
)


def test_extract_site_total_handles_commas_and_missing():
    from collectors.chictr import _extract_site_total
    assert _extract_site_total("<span id='data-total'>18</span>") == 18
    assert _extract_site_total("<span id='data-total'>130,716</span>") == 130716
    assert _extract_site_total("<div>no marker</div>") is None
    assert _extract_site_total("<span id='data-total'>n/a</span>") is None


def test_chictr_search_page_stashes_site_total(chictr, monkeypatch):
    """_search_results_page 抓页时顺带记录原站总数（零额外请求）。"""
    monkeypatch.setattr(chictr, "_fetch",
                        lambda url, timeout_ms=25000: PAGE_WITH_TOTAL)
    rows = chictr._search_results_page("心肌炎", page=1)
    assert rows[0]["chictr_no"] == "ChiCTR2600128415"
    assert chictr._last_site_total == 1307


def test_chictr_live_search_passes_site_total(chictr, conn, monkeypatch):
    """live_search 把原站总数透传进返回 stats（短页即止，found 不变）。"""
    monkeypatch.setattr(chictr, "_fetch",
                        lambda url, timeout_ms=25000: PAGE_WITH_TOTAL)
    stats = chictr.live_search("心肌炎", max_pages=2)
    assert stats["site_total"] == 1307
    assert stats["found"] == 1  # 单行短页 → 第 1 页即末页


def test_live_search_site_total_defaults_none(chictr, conn, monkeypatch):
    """子类未提供总数（如 CTR 浏览器路径/monkeypatch 替身）→ None，前端隐藏。"""
    monkeypatch.setattr(chictr, "_search_results_page",
                        lambda keyword, page=1: [])
    stats = chictr.live_search("x", max_pages=1)
    assert stats["site_total"] is None
    assert stats["found"] == 0


def test_ctr_live_search_writes_detail_keys(ctr, conn, monkeypatch):
    entries = [{"uuid": "uuid-1", "ctr": "CTR2026001", "index": "3",
                "title": "心衰 药物研究"}]
    monkeypatch.setattr(
        ctr, "_search_results_page", lambda keyword, page: list(entries))

    stats = ctr.live_search("心衰", max_pages=1)

    assert stats["found"] == 1 and stats["queued"] == 1
    assert stats["entries"][0]["source_trial_id"] == "CTR2026001"
    assert stats["entries"][0]["url"].endswith("id=uuid-1&ckm_index=3")
    cursor = ctr._load_cursor("list_walk")
    assert cursor["detail_keys"]["CTR2026001"] == {"uuid": "uuid-1",
                                                   "index": "3"}
    assert cursor.get("recent_seen_ids") == []


def test_ctr_live_search_short_page_stops_at_page_one(ctr, conn, monkeypatch):
    """不足一页的返回即末页：max_pages=3 也只走 1 页（不多打 WAF）。"""
    entries = [{"uuid": "u", "ctr": "CTR2026002", "index": "1", "title": "t"}]
    calls = []
    def fake(keyword, page):
        calls.append(page)
        return list(entries)
    monkeypatch.setattr(ctr, "_search_results_page", fake)

    stats = ctr.live_search("心", max_pages=3)

    assert calls == [1]
    assert stats["pages_walked"] == 1


# ── Server 层：POST /api/trials/live-check ────────────────────────────────


class _FakeCollector:
    """零网络替身：live_search 返回罐装数据，enrich 假装成功。"""

    source_id = None

    def __init__(self, entries=None, circuit=False, error=False):
        self._entries = entries or []
        self._circuit = circuit
        self._error = error

    def live_search(self, q, max_pages=1, start_page=1, field=None):
        self.last_query = q
        self.last_max_pages = max_pages
        if self._circuit:
            raise WafCircuitOpenError("chictr", 3, 3, "waf_page")
        if self._error:
            raise RuntimeError("fake transport failure")
        return {"keyword": q, "found": len(self._entries),
                "queued": len(self._entries), "pages_walked": 1,
                "stopped_reason": "completed",
                "entries": [dict(e) for e in self._entries]}

    def enrich_pending(self, limit=None, keywords=None, workers=1):
        return {"enriched": 1 if self._entries else 0, "failed": 0,
                "skipped": 0, "records": []}


class _FakeNctCollector:
    """NCT 替身：live_search 直接入库、无 enrich_pending（与真实一致）。"""

    source_id = None

    def __init__(self, entries=None):
        self._entries = entries or []

    def live_search(self, q, max_pages=1):
        self.last_query = q
        self.last_max_pages = max_pages
        return {"keyword": q, "found": len(self._entries), "queued": 0,
                "enriched": len(self._entries), "pages_walked": 1,
                "stopped_reason": "completed",
                "entries": [dict(e) for e in self._entries]}


class _FakeIctrpCollector(_FakeCollector):
    """WHO portal substitute with the same paged call signature."""

    def live_search(self, q, max_pages=1, start_page=1):
        self.last_query = q
        self.last_max_pages = max_pages
        self.last_start_page = start_page
        return {"keyword": q, "found": len(self._entries), "queued": 0,
                "enriched": 0, "pages_walked": 1,
                "stopped_reason": "completed", "site_total": len(self._entries),
                "has_more": False, "next_page": None,
                "entries": [dict(e) for e in self._entries]}


@pytest.fixture
def live_client(test_db, conn, monkeypatch):
    """TestClient + 各源采集器替换为零网络替身。"""
    # server._open_conn 按调用时读 ct_report.query.DB_PATH —— 指向临时库，
    # 否则 in_library 查询会落到真实库上
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod
    import collectors.who_ictrp as ictrp_mod

    chictr_entries = [
        {"source_trial_id": "ChiCTR26000100", "title": "实时命中·已入库",
         "url": "https://www.chictr.org.cn/showproj.html?proj=30001"},
        {"source_trial_id": "ChiCTR26000101", "title": "实时命中·待补爬",
         "url": "https://www.chictr.org.cn/showproj.html?proj=30002"},
    ]
    fake_chictr = _FakeCollector(entries=chictr_entries)
    fake_chictr.source_id = _source_id(conn, "ChiCTR")
    fake_ctr = _FakeCollector(entries=[])
    fake_ctr.source_id = _source_id(conn, "CTR")
    fake_nct = _FakeNctCollector(entries=[
        {"source_trial_id": "NCT99999999",
         "title": "Live troponin pathway study",
         "url": "https://clinicaltrials.gov/study/NCT99999999"},
    ])
    fake_nct.source_id = _source_id(conn, "NCT")
    fake_ictrp = _FakeIctrpCollector(entries=[
        {"source_trial_id": "NCT07008391",
         "title": "WHO portal troponin study",
         "url": "https://trialsearch.who.int/Trial2.aspx?TrialID=NCT07008391"},
    ])
    fake_ictrp.source_id = _source_id(conn, "ICTRP")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector",
                        lambda: fake_chictr)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector",
                        lambda: fake_ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector",
                        lambda: fake_nct)
    monkeypatch.setattr(ictrp_mod, "WHOICTRPCollector",
                        lambda: fake_ictrp)

    # ChiCTR26000100 已在本地库 → in_library=True
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title, "
        "raw_payload) VALUES (?, ?, ?, ?)",
        (fake_chictr.source_id, "ChiCTR26000100", "实时命中·已入库", "{}"),
    )
    # 双语检索扩展种子：英文 NCT 记录 + 中文 ChiCTR 记录互为译词命中对象
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title, "
        "raw_payload) VALUES (?, ?, ?, ?)",
        (_source_id(conn, "NCT"), "NCT00000123",
         "High sensitivity troponin assay in chest pain", "{}"),
    )
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title, "
        "raw_payload) VALUES (?, ?, ?, ?)",
        (fake_chictr.source_id, "ChiCTR26000200",
         "肌钙蛋白在急性冠脉综合征中的诊断价值", "{}"),
    )
    # 术语学习种子：同注册号跨库兄弟对（心肌标志物 ↔ cardiac biomarker）
    # + 一条仅英文记录（扩展生效后才会出现在结果里）
    ict = _source_id(conn, "ICTRP")
    for sid, zh_title, en_title in [
        ("ChiCTR26000300", "心肌标志物在胸痛中的应用研究",
         "Cardiac biomarker panel in chest pain"),
        ("ChiCTR26000301", "心肌标志物与预后评估",
         "Cardiac biomarker and prognosis"),
    ]:
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, title,"
            " raw_payload) VALUES (?, ?, ?, '{}')",
            (fake_chictr.source_id, sid, zh_title))
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, title,"
            " raw_payload) VALUES (?, ?, ?, '{}')", (ict, sid, en_title))
    conn.execute(
        "INSERT INTO registry_records (source_id, source_trial_id, title, "
        "raw_payload) VALUES (?, ?, ?, ?)",
        (_source_id(conn, "NCT"), "NCT00000456",
         "Cardiac biomarker dynamics after myocardial infarction", "{}"),
    )
    conn.commit()

    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app())
    client.live_fakes = {  # type: ignore[attr-defined]
        "chictr": fake_chictr, "ctr": fake_ctr, "nct": fake_nct,
        "ictrp": fake_ictrp,
    }
    yield client


def test_live_check_ok_and_in_library(live_client):
    resp = live_client.post("/api/trials/live-check", json={"q": "心肌炎"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["q"] == "心肌炎"
    by_source = {r["source"]: r for r in body["results"]}
    assert set(by_source) == {"chictr", "ctr", "nct", "ictrp"}

    chictr = by_source["chictr"]
    assert chictr["status"] == "ok"
    assert chictr["found"] == 2
    assert chictr["enriched"] == 1
    assert live_client.live_fakes["chictr"].last_max_pages == 5
    entries = {e["source_trial_id"]: e for e in chictr["entries"]}
    assert entries["ChiCTR26000100"]["in_library"] is True
    assert entries["ChiCTR26000101"]["in_library"] is False

    assert by_source["ctr"]["status"] == "ok"
    assert by_source["ctr"]["found"] == 0
    assert live_client.live_fakes["ctr"].last_max_pages == 3

    # NCT：官方 API，live_search 直接 upsert（enriched 来自 stats 本身）
    nct = by_source["nct"]
    assert nct["status"] == "ok"
    assert nct["found"] == 1
    assert nct["enriched"] == 1
    assert live_client.live_fakes["nct"].last_max_pages == 5
    assert nct["entries"][0]["source_trial_id"] == "NCT99999999"

    # ICTRP：公开门户分页适配器（轻量列表，不替代每周完整 XML）。
    ictrp = by_source["ictrp"]
    assert ictrp["status"] == "ok"
    assert ictrp["found"] == 1
    assert ictrp["entries"][0]["source_trial_id"] == "NCT07008391"


def test_live_check_cooldown_cache(live_client):
    first = live_client.post("/api/trials/live-check", json={"q": "心衰"}).json()
    second = live_client.post("/api/trials/live-check", json={"q": "心衰"}).json()
    assert {r["status"] for r in first["results"]} == {"ok"}
    assert {r["status"] for r in second["results"]} == {"cached"}
    # 缓存命中返回同样的内容
    assert second["results"][0]["entries"] == first["results"][0]["entries"]


def test_live_check_source_filter(live_client):
    body = live_client.post(
        "/api/trials/live-check",
        json={"q": "心衰", "sources": ["chictr"]},
    ).json()
    assert [r["source"] for r in body["results"]] == ["chictr"]


def test_ictrp_private_api_uses_portal_adapter(live_client):
    body = live_client.get("/api/ictrp/search", params={
        "q": "troponin", "page": 1,
    }).json()
    assert body["live"] is True
    assert body["query_used"] == "troponin"
    assert body["site_total"] == 1
    assert body["entries"][0]["source_trial_id"] == "NCT07008391"
    assert live_client.live_fakes["ictrp"].last_start_page == 1


def test_live_check_empty_q_rejected(live_client):
    resp = live_client.post("/api/trials/live-check", json={"q": "   "})
    assert resp.status_code == 422


def test_live_check_circuit_open_reported(test_db, conn, monkeypatch):
    # startup_check 经 _open_conn 读真实库 → 指向临时库
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod

    fake = _FakeCollector(circuit=True)
    fake.source_id = _source_id(conn, "ChiCTR")
    ok_ctr = _FakeCollector(entries=[])
    ok_ctr.source_id = _source_id(conn, "CTR")
    ok_nct = _FakeNctCollector(entries=[])
    ok_nct.source_id = _source_id(conn, "NCT")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: fake)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector", lambda: ok_ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector", lambda: ok_nct)

    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app())
    body = client.post("/api/trials/live-check", json={"q": "心"}).json()
    by_source = {r["source"]: r for r in body["results"]}
    assert by_source["chictr"]["status"] == "circuit_open"
    assert by_source["chictr"]["entries"] == []
    assert by_source["ctr"]["status"] == "ok"
    assert by_source["nct"]["status"] == "ok"


def test_live_check_error_reported(test_db, conn, monkeypatch):
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod

    fake = _FakeCollector(error=True)
    fake.source_id = _source_id(conn, "ChiCTR")
    ok_ctr = _FakeCollector(entries=[])
    ok_ctr.source_id = _source_id(conn, "CTR")
    ok_nct = _FakeNctCollector(entries=[])
    ok_nct.source_id = _source_id(conn, "NCT")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: fake)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector", lambda: ok_ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector", lambda: ok_nct)

    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app())
    body = client.post("/api/trials/live-check", json={"q": "心"}).json()
    chictr = next(r for r in body["results"] if r["source"] == "chictr")
    assert chictr["status"] == "error"
    assert "fake transport failure" in chictr["error"]


def test_chictr_live_search_absorbs_into_existing_cursor(
        chictr, conn, monkeypatch):
    """回归：游标已有 proj_ids（生产常态）时，absorb 仍必须落盘新映射。

    历史缺陷：cursor.get("proj_ids") 与 proj_map 指向同一对象，原地变更后
    「有无新增」比较恒为 False，辅助表不落盘 → enrich 报无法解析详情键。
    """
    sid = chictr.source_id
    conn.execute(
        "INSERT INTO discovery_cursors (source_id, mode, cursor_json, status)"
        " VALUES (?, 'list_walk', ?, 'active')",
        (sid, '{"recent_seen_ids": ["ChiCTR99999999"],'
              ' "proj_ids": {"ChiCTR99999999": "9999"}}'),
    )
    conn.commit()

    entries = [{"chictr_no": "ChiCTR26000009", "title": "t", "proj_id": "91009"}]
    monkeypatch.setattr(
        chictr, "_search_results_page", lambda keyword, page=1: list(entries))

    chictr.live_search("心", max_pages=1)

    cursor = chictr._load_cursor("list_walk")
    assert cursor["proj_ids"].get("ChiCTR26000009") == "91009"
    assert cursor["proj_ids"].get("ChiCTR99999999") == "9999"  # 旧映射保留
    assert cursor["recent_seen_ids"] == ["ChiCTR99999999"]     # 水位不动


def test_ctr_live_search_absorbs_into_existing_cursor(ctr, conn, monkeypatch):
    """同上，CTR detail_keys 版。"""
    sid = ctr.source_id
    conn.execute(
        "INSERT INTO discovery_cursors (source_id, mode, cursor_json, status)"
        " VALUES (?, 'list_walk', ?, 'active')",
        (sid, '{"recent_seen_ids": ["CTR9999999"],'
              ' "detail_keys": {"CTR9999999": {"uuid": "u-old", "index": "1"}}}'),
    )
    conn.commit()

    entries = [{"uuid": "u-new", "ctr": "CTR2026009", "index": "2",
                "title": "t"}]
    monkeypatch.setattr(
        ctr, "_search_results_page", lambda keyword, page: list(entries))

    ctr.live_search("心", max_pages=1)

    cursor = ctr._load_cursor("list_walk")
    assert cursor["detail_keys"].get("CTR2026009") == {"uuid": "u-new",
                                                       "index": "2"}
    assert cursor["detail_keys"].get("CTR9999999") == {"uuid": "u-old",
                                                       "index": "1"}
    assert cursor["recent_seen_ids"] == ["CTR9999999"]


# ── Collector 层：NCT live_search ──────────────────────────────────────────


@pytest.fixture
def nct(conn, monkeypatch):
    from collectors.clinicaltrials import ClinicalTrialsGovCollector
    collector = ClinicalTrialsGovCollector()
    # normalise/upsert 打桩：live_search 的职责是检索+解析+入库调用，
    # normalise 的字段映射已由既有 NCT 测试覆盖
    monkeypatch.setattr(
        collector, "normalise",
        lambda study: study["protocolSection"]["identificationModule"]["nctId"])
    monkeypatch.setattr(collector, "_upsert_and_emit_events",
                        lambda norm: {"changes": 0})
    return collector


def test_nct_live_search_parses_and_upserts(nct, monkeypatch):
    payload = {
        "totalCount": 2840,
        "studies": [
            {"protocolSection": {"identificationModule": {
                "nctId": "NCT00000001", "briefTitle": "Troponin pathway"}}},
            {"protocolSection": {"identificationModule": {
                "briefTitle": "No nctId — must be skipped"}}},
        ],
        "nextPageToken": None,
    }

    seen_params: list = []

    class FakeResp:
        def raise_for_status(self):
            pass
        def json(self):
            return payload

    def fake_get(url, params=None, timeout=None):
        seen_params.append(dict(params or {}))
        return FakeResp()

    monkeypatch.setattr("collectors.clinicaltrials.requests.get", fake_get)

    stats = nct.live_search("troponin", max_pages=2)

    assert stats["found"] == 1  # 缺 nctId 的行被跳过
    assert stats["queued"] == 0
    assert stats["enriched"] == 1
    assert stats["stopped_reason"] == "completed"
    assert stats["site_total"] == 2840  # countTotal=true → totalCount 透传
    assert stats["has_more"] is False
    assert seen_params[0].get("countTotal") == "true"
    entry = stats["entries"][0]
    assert entry["source_trial_id"] == "NCT00000001"
    assert entry["title"] == "Troponin pathway"
    assert entry["url"] == "https://clinicaltrials.gov/study/NCT00000001"


def test_nct_live_search_resume_with_token(nct, monkeypatch):
    """续批：start_token 作为 pageToken 续抓；nextPageToken 决定 has_more。"""
    calls: list = []

    class FakeResp:
        def __init__(self, token):
            self._token = token
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "totalCount": 30, "nextPageToken": self._token,
                "studies": [{"protocolSection": {"identificationModule": {
                    "nctId": f"NCT{len(calls)}000000{i}",
                    "briefTitle": f"study {len(calls)}-{i}"}}}
                    for i in range(10)],
            }

    def fake_get(url, params=None, timeout=None):
        calls.append(params.get("pageToken"))
        return FakeResp(f"token{len(calls)}" if len(calls) < 3 else None)

    monkeypatch.setattr("collectors.clinicaltrials.requests.get", fake_get)

    first = nct.live_search("troponin", max_pages=1)
    assert first["has_more"] is True
    assert first["next_token"] == "token1"
    assert first["site_total"] == 30

    cont = nct.live_search("troponin", max_pages=2,
                           start_token=first["next_token"])
    assert calls[1] == "token1"
    assert cont["found"] == 20
    assert cont["has_more"] is False  # token2 之后 API 返回 None
    assert cont["next_token"] is None


def test_live_check_routes_language_per_source(live_client):
    """双语分发：NCT 拿英文变体，CTR 拿中文变体，ChiCTR 原词直发。"""
    body = live_client.post(
        "/api/trials/live-check", json={"q": "肌钙蛋白"}).json()
    by_source = {r["source"]: r for r in body["results"]}
    assert by_source["nct"]["query_used"] == "troponin"
    assert by_source["chictr"]["query_used"] == "肌钙蛋白"
    assert by_source["ctr"]["query_used"] == "肌钙蛋白"

    # 反方向：英文词 → CTR 拿中文译文，NCT/ChiCTR 原词直发
    body = live_client.post(
        "/api/trials/live-check", json={"q": "troponin"}).json()
    by_source = {r["source"]: r for r in body["results"]}
    assert by_source["ctr"]["query_used"] == "肌钙蛋白"
    assert by_source["nct"]["query_used"] == "troponin"
    assert by_source["chictr"]["query_used"] == "troponin"


def test_search_bilingual_expansion(live_client):
    """本地检索：术语表词条自动合并译文结果（双语一次命中）。"""
    body = live_client.get(
        "/api/trials/search", params={"q": "troponin"}).json()
    ids = {t["id"] for t in body["trials"]}
    assert "NCT00000123" in ids        # 英文原词 → NCT 英文记录
    assert "ChiCTR26000200" in ids     # 译文 → ChiCTR 中文记录
    assert body["expanded_query"] == ["troponin", "肌钙蛋白"]

    body = live_client.get(
        "/api/trials/search", params={"q": "肌钙蛋白"}).json()
    ids = {t["id"] for t in body["trials"]}
    assert "NCT00000123" in ids and "ChiCTR26000200" in ids
    assert body["expanded_query"] == ["肌钙蛋白", "troponin"]


def test_search_no_expansion_for_unknown_term(live_client):
    body = live_client.get(
        "/api/trials/search", params={"q": "zzz-not-a-term"}).json()
    assert body["expanded_query"] is None


def test_search_learns_unknown_term_then_expands(live_client):
    """首查无译文但播种学习（evidence=2 即生效）；再查起双语扩展。"""
    first = live_client.get(
        "/api/trials/search", params={"q": "心肌标志物"}).json()
    assert first["expanded_query"] is None          # 首查：术语表还没有
    first_ids = {t["id"] for t in first["trials"]}
    assert "NCT00000456" not in first_ids           # 仅英文记录未命中

    second = live_client.get(
        "/api/trials/search", params={"q": "心肌标志物"}).json()
    assert second["expanded_query"] == ["心肌标志物", "cardiac biomarker"]
    second_ids = {t["id"] for t in second["trials"]}
    assert "NCT00000456" in second_ids              # 学到的译文把英文记录并进来


# ── 分批抓全部页面：续抓游标 / 强制间隔 / 有限预览、全量入库 ────────────────

import re as _re


def _chi_page(rows: int, total: int, first_seq: int) -> str:
    """构造 rows 行、原站总数 total 的搜索页 fixture（页号从 first_seq 编号）。"""
    trs = "".join(
        f"<tr><td>{i}</td><td>ChiCTR2699{first_seq + i:04d}</td>"
        f"<td><a href='showproj.html?proj={30000 + first_seq + i}'>试验{i}</a></td>"
        f"<td>2026-01-01</td></tr>"
        for i in range(rows))
    return ("<html><body>"
            f"<span id='data-total'>{total}</span>"
            f"<table class='table1'>{trs}</table></body></html>")


def _paged_fetch(pages: dict) -> object:
    def fake(url, timeout_ms=25000):
        m = _re.search(r"page=(\d+)", url)
        return pages[int(m.group(1))]
    return fake


def test_chictr_live_search_stops_at_site_total(chictr, conn, monkeypatch):
    """首次抓取：到原站总数即停（25 条 = 2 满页 + 1 短页，无需更多批）。"""
    pages = {1: _chi_page(10, 25, 1), 2: _chi_page(10, 25, 11),
             3: _chi_page(5, 25, 21)}
    monkeypatch.setattr(chictr, "_fetch", _paged_fetch(pages))
    stats = chictr.live_search("心肌炎", max_pages=3)
    assert stats["found"] == 25
    assert stats["pages_walked"] == 3
    assert stats["site_total"] == 25
    assert stats["has_more"] is False
    assert stats["next_page"] is None


def test_chictr_live_search_batch_resume(chictr, conn, monkeypatch):
    """续批：start_page=2 从游标处续抓；has_more/next_page 契约正确。"""
    pages = {1: _chi_page(10, 25, 1), 2: _chi_page(10, 25, 11),
             3: _chi_page(5, 25, 21)}
    monkeypatch.setattr(chictr, "_fetch", _paged_fetch(pages))
    first = chictr.live_search("心肌炎", max_pages=1)
    assert first["has_more"] is True
    assert first["next_page"] == 2
    assert first["found"] == 10
    assert first["site_total"] == 25

    rest = chictr.live_search("心肌炎", max_pages=3,
                              start_page=first["next_page"])
    assert rest["found"] == 15
    assert rest["has_more"] is False
    assert rest["next_page"] is None


class _PagedFakeCollector:
    """分批续抓替身：端点级会话游标 / 强制间隔 / 上限行为的验证。"""

    def __init__(self, page_sizes, prefix="ChiCTR2699", source_id=None):
        self._page_sizes = page_sizes
        self._prefix = prefix
        self.source_id = source_id
        self.calls = []
        self.enrich_calls = []

    def live_search(self, q, max_pages=1, start_page=1, start_token=None,
                    field=None):
        self.calls.append((q, max_pages, start_page))
        got, served = [], 0
        page = start_page
        while page < start_page + max_pages and page <= len(self._page_sizes):
            got.extend({"source_trial_id": f"{self._prefix}{page:02d}{i:03d}",
                        "title": f"命中 p{page}-{i}", "url": None}
                       for i in range(self._page_sizes[page - 1]))
            served += 1
            page += 1
        has_more = bool(got) and served == max_pages \
            and start_page + max_pages - 1 < len(self._page_sizes)
        return {"keyword": q, "found": len(got), "queued": len(got),
                "pages_walked": served, "stopped_reason": "completed",
                "site_total": sum(self._page_sizes),
                "has_more": has_more,
                "next_page": start_page + served if has_more else None,
                "enriched": 0, "entries": got}

    def enrich_pending(self, limit=None, keywords=None, source_trial_ids=None,
                       workers=1):
        self.enrich_calls.append((limit, tuple(keywords or []),
                                  tuple(source_trial_ids or []), workers))
        return {"enriched": 0, "failed": 0, "skipped": 0, "records": []}


def _setup_paged(test_db, conn, monkeypatch, chictr_fake):
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod
    chictr_fake.source_id = _source_id(conn, "ChiCTR")
    ctr = _FakeCollector(entries=[])
    ctr.source_id = _source_id(conn, "CTR")
    nct = _FakeNctCollector(entries=[])
    nct.source_id = _source_id(conn, "NCT")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: chictr_fake)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector", lambda: ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector", lambda: nct)

    from fastapi.testclient import TestClient
    from server.app import create_app
    return TestClient(create_app())


def test_live_check_continue_burst_and_cooldown(test_db, conn, monkeypatch):
    """ChiCTR 首次 ≤5 页 → continue 续批；有冷却且整查走缓存。"""
    fake = _PagedFakeCollector([10, 10, 10, 10, 10, 10])
    client = _setup_paged(test_db, conn, monkeypatch, fake)

    import time as _real_time

    class _ShiftedTime:
        off = 0.0

        def time(self):
            return _real_time.time() + self.off

        def __getattr__(self, name):
            return getattr(_real_time, name)

    shift = _ShiftedTime()
    monkeypatch.setattr("server.app.time", shift)

    def post(**payload):
        return client.post(
            "/api/trials/live-check",
            json={"q": "分批", "sources": ["chictr"], **payload}).json()

    first = post()["results"][0]
    assert first["status"] == "ok"
    assert first["found"] == 50
    assert first["has_more"] is True
    assert fake.calls[0] == ("分批", 5, 1)

    burst = post(**{"continue": True})["results"][0]
    assert burst["status"] == "cooldown"
    assert burst["retry_after"] >= 1

    shift.off += 11.0
    done = post(**{"continue": True})["results"][0]
    assert done["status"] == "ok"
    assert done["found"] == 60
    assert done["has_more"] is False
    assert fake.calls[-1] == ("分批", 2, 6)  # 续批:每次 2 页,从游标第 6 页起

    refetch = post()["results"][0]
    assert refetch["status"] == "cached"
    assert refetch["found"] == 60


def test_live_check_preview_cap_does_not_stop_full_pagination(
        test_db, conn, monkeypatch):
    """响应预览最多 200 行，但累计数与续页不能再被 200 条截断。"""
    fake = _PagedFakeCollector([60] * 6, prefix="ChiCTR2698")
    client = _setup_paged(test_db, conn, monkeypatch, fake)

    def post(**payload):
        return client.post(
            "/api/trials/live-check",
            json={"q": "大批", "sources": ["chictr"], **payload}).json()

    first = post()["results"][0]
    assert first["found"] == 300
    assert first["shown"] == 200
    assert len(first["entries"]) == 200
    assert first["has_more"] is True


def test_live_check_full_ingest_enriches_every_waf_batch(
        test_db, conn, monkeypatch):
    fake = _PagedFakeCollector([10, 10, 10, 10, 10, 10])
    client = _setup_paged(test_db, conn, monkeypatch, fake)

    import time as _real_time

    class _ShiftedTime:
        off = 0.0
        def time(self):
            return _real_time.time() + self.off
        def __getattr__(self, name):
            return getattr(_real_time, name)

    shift = _ShiftedTime()
    monkeypatch.setattr("server.app.time", shift)
    payload = {"q": "全量", "sources": ["chictr"], "full_ingest": True}
    first = client.post("/api/trials/live-check", json=payload).json()["results"][0]
    assert first["found"] == 50 and first["has_more"] is True
    assert fake.enrich_calls[0][0:2] == (50, ("全量",))
    assert len(fake.enrich_calls[0][2]) == 50
    assert fake.enrich_calls[0][3] == 2

    shift.off += 11.0
    done = client.post(
        "/api/trials/live-check", json={**payload, "continue": True}
    ).json()["results"][0]
    assert done["found"] == 60 and done["has_more"] is False
    assert fake.enrich_calls[-1][0:2] == (10, ("全量",))
    assert len(fake.enrich_calls[-1][2]) == 10
    assert fake.enrich_calls[-1][3] == 2


# ── 中文复合词拆分：segment / translate_segments ──────────────────────────


def test_terminology_segment():
    from core.terminology import segment, translate_segments
    assert segment("结直肠癌甲基化") == ["结直肠癌", "甲基化"]
    # 贪心最长匹配：更长的既有词条优先成段
    assert segment("多发性骨髓瘤甲基化") == ["多发性骨髓瘤", "甲基化"]
    assert segment("心肌病甲基化") == ["心肌病", "甲基化"]
    # 整词命中 / 带空格 / 碎词 / 非 CJK 一律不拆
    assert segment("心肌炎") is None
    assert segment("结直肠癌 甲基化") is None
    assert segment("结直肠癌xyz") is None
    assert segment("troponin") is None
    assert translate_segments("结直肠癌甲基化") == \
        "colorectal cancer methylation"


# ── live-check：整串 0 命中拆词回退 + NCT 分段译文 ─────────────────────────


class _SegmentFakeCollector(_FakeCollector):
    """整串 0 命中、拆出的段各自命中的替身（记录全部调用与 enrich 关键词）。"""

    def __init__(self, per_query, source_id=None):
        super().__init__(entries=[])
        self._per_query = per_query
        self.source_id = source_id
        self.calls: list = []
        self.enrich_keywords = None

    def live_search(self, q, max_pages=1):
        self.calls.append(q)
        self.last_query = q
        self.last_max_pages = max_pages
        entries = self._per_query.get(q, [])
        return {"keyword": q, "found": len(entries),
                "queued": len(entries),
                "pages_walked": 1 if entries else 0,
                "stopped_reason": "completed",
                "entries": [dict(e) for e in entries]}

    def enrich_pending(self, limit=None, keywords=None, workers=1):
        self.enrich_keywords = list(keywords or [])
        return {"enriched": 1, "failed": 0, "skipped": 0, "records": []}


def test_live_check_segment_fallback(test_db, conn, monkeypatch):
    """整串 0 命中 → 拆词逐段查原站：去重合并、全段命中优先、enrich 按段。"""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod

    both = {"source_trial_id": "ChiCTR26000302",
            "title": "甲基化在结直肠癌术后监测中的应用", "url": None}
    fake = _SegmentFakeCollector({
        "结直肠癌": [
            {"source_trial_id": "ChiCTR26000301",
             "title": "结直肠癌辅助化疗研究", "url": None},
            both,
        ],
        "甲基化": [
            {"source_trial_id": "ChiCTR26000303",
             "title": "DNA甲基化液体活检研究", "url": None},
            both,
        ],
    })
    fake.source_id = _source_id(conn, "ChiCTR")
    ctr = _FakeCollector(entries=[])
    ctr.source_id = _source_id(conn, "CTR")
    nct = _FakeNctCollector(entries=[])
    nct.source_id = _source_id(conn, "NCT")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: fake)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector", lambda: ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector", lambda: nct)

    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app())
    body = client.post("/api/trials/live-check", json={
        "q": "结直肠癌甲基化", "sources": ["chictr"]}).json()
    r = body["results"][0]
    assert r["status"] == "ok"
    assert r["stopped_reason"] == "segmented_fallback"
    assert r["has_more"] is False
    assert r["found"] == 3  # 两段结果去重后
    assert r["query_used"] == "结直肠癌 甲基化"
    assert [e["source_trial_id"] for e in r["entries"]][0] == "ChiCTR26000302"
    # 调用顺序：整串（0 命中）→ 逐段；enrich 关键词换成拆出的段
    assert fake.calls == ["结直肠癌甲基化", "结直肠癌", "甲基化"]
    assert fake.enrich_keywords == ["结直肠癌", "甲基化"]

    # 有命中（或非复合词）时绝不触发拆词回退
    ok_fake = _SegmentFakeCollector({"心肌炎": [
        {"source_trial_id": "ChiCTR26000311", "title": "心肌炎", "url": None}]})
    ok_fake.source_id = _source_id(conn, "ChiCTR")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: ok_fake)
    # 采集器单例缓存在 create_app 闭包里 → 新 app 才会换上 ok_fake
    client2 = TestClient(create_app())
    body = client2.post("/api/trials/live-check", json={
        "q": "心肌炎", "sources": ["chictr"]}).json()
    assert ok_fake.calls == ["心肌炎"]


def test_live_check_nct_segment_translation(test_db, conn, monkeypatch):
    """NCT：整词翻不出时改发分段译文（空格即 AND），零额外请求。"""
    from config import CONFIG
    import ct_report.query as qmod
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    import collectors.chictr as chictr_mod
    import collectors.chinadrugtrials as ctr_mod
    import collectors.clinicaltrials as nct_mod

    chictr = _FakeCollector(entries=[
        {"source_trial_id": "ChiCTR26000321", "title": "结直肠癌甲基化研究",
         "url": None}])
    chictr.source_id = _source_id(conn, "ChiCTR")
    ctr = _FakeCollector(entries=[])
    ctr.source_id = _source_id(conn, "CTR")
    nct = _FakeNctCollector(entries=[])
    nct.source_id = _source_id(conn, "NCT")
    monkeypatch.setattr(chictr_mod, "ChiCTRCollector", lambda: chictr)
    monkeypatch.setattr(ctr_mod, "ChinaDrugTrialsCollector", lambda: ctr)
    monkeypatch.setattr(nct_mod, "ClinicalTrialsGovCollector", lambda: nct)

    from fastapi.testclient import TestClient
    from server.app import create_app
    client = TestClient(create_app())
    body = client.post("/api/trials/live-check", json={
        "q": "结直肠癌甲基化", "sources": ["nct"]}).json()
    r = body["results"][0]
    assert r["status"] == "ok"
    assert r["query_used"] == "colorectal cancer methylation"
    assert nct.last_query == "colorectal cancer methylation"


def test_search_segment_fallback(live_client, conn):
    """本地检索复合词 0 命中 → 拆词 OR 扩展（各段含译文并入）。"""
    for sid, tid, title in [
        ("ChiCTR", "ChiCTR26000401", "结直肠癌患者预后队列研究"),
        ("ChiCTR", "ChiCTR26000402", "循环DNA甲基化筛查研究"),
        ("NCT", "NCT00000789",
         "Colorectal cancer methylation biomarker study"),
    ]:
        conn.execute(
            "INSERT INTO registry_records (source_id, source_trial_id, "
            "title, raw_payload) VALUES (?, ?, ?, '{}')",
            (_source_id(conn, sid), tid, title))
    conn.commit()

    body = live_client.get(
        "/api/trials/search", params={"q": "结直肠癌甲基化"}).json()
    assert body["segmented_query"] == ["结直肠癌", "甲基化"]
    ids = {t["id"] for t in body["trials"]}
    assert {"ChiCTR26000401", "ChiCTR26000402", "NCT00000789"} <= ids
    assert body["total"] >= 3

    # 原本能命中的查询不做拆词扩展
    plain = live_client.get(
        "/api/trials/search", params={"q": "troponin"}).json()
    assert plain["segmented_query"] is None
