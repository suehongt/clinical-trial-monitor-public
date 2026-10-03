"""
中国药物临床试验登记与信息公示平台 (chinadrugtrials.org.cn) Collector.

Platform: CDE / NMPA (国家药品监督管理局药品审评中心) Drug Trial Registry
WAF:      Wangsu WAF (JS fingerprint challenge) — no public JSON API available
Method:   Playwright browser automation for WAF bypass + BeautifulSoup HTML parsing

URL structure (discovered via live exploration):
  Search (GET):  /clinicaltrials.searchlist.dhtml?keywords=<keyword>&currentpage=<N>
  Detail (GET):  /clinicaltrials.searchlistdetail.dhtml?id=<uuid>&ckm_index=<row>

  Search returns a table with UUID identifiers for each row. The detail URL
  requires the UUID (not the CTR number). The collector extracts UUIDs from
  search results, then fetches each detail page by UUID.

Bootstrap: multi-keyword search to discover CTR numbers (no "list all" endpoint).
Incremental: 三段式「发现→队列→增强」——discover_new 倒序走广谱词表列表页，
把新 CTR 号写入 discovery_queue 并维护 discovery_cursors 水位线（schema v7）；
enrich_pending 按预算逐条抓详情并复用 parse/upsert 全链路。旧的纯关键词
路径保留为 fetch_new_or_updated_legacy 供回退。
Raw HTML is stored as raw_payload for traceability.

Design refs: the project design notes §2.2/2.3/2.4,
the project design notes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict
from typing import Any, Optional

from collectors.base import NormalisedRecord, to_json_array
from collectors.browser_base import (WAFBrowserCollector, WafCircuitOpenError,
                                     _STEALTH_UA)
from db.connection import get_connection

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

CDE_BASE_URL = "https://www.chinadrugtrials.org.cn"
CDE_SEARCH_PATH = "/clinicaltrials.searchlist.dhtml"
CDE_DETAIL_PATH = "/clinicaltrials.searchlistdetail.dhtml"

# CJK 判定：live_search 据此决定走 indication（适应症）还是 keywords 字段
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Bootstrap keywords — broad Chinese therapeutic area keywords.
# CDE has no "list all" endpoint, so we search by keyword.
BOOTSTRAP_KEYWORDS: list[str] = [
    "糖尿病", "高血压", "高血脂",
    "肺癌", "乳腺癌", "胃癌", "肝癌", "结直肠癌", "淋巴瘤", "白血病",
    "慢性乙型肝炎", "艾滋病", "类风湿关节炎", "新型冠状病毒",
    "阿尔茨海默病", "脑卒中", "冠心病", "慢性阻塞性肺疾病", "哮喘", "多发性骨髓瘤", "骨髓瘤",
    "抑郁症", "精神分裂症", "肾衰竭", "系统性红斑狼疮", "银屑病",
    "克罗恩病", "溃疡性结肠炎", "多发性硬化", "帕金森病",
    "贫血", "血友病", "骨质疏松", "疼痛", "疫苗",
    "抗生素", "抗肿瘤", "免疫抑制剂", "降压药", "降糖药",
]

MAX_SEARCH_PAGES = 5  # result pages per keyword

# ── 三段式发现层（discover → queue → enrich）调参常量 ──────────────────────
#
# 与 collectors/chictr.py 同构（两源同属 Agent-A，保持风格一致）。
# Spike S3 证实：CTR「最新公示」列表同样严格按登记号倒序（号序即时间序）、
# 列表页直接可见 CTR 号与详情 UUID → 水位线用 recent_seen_ids 号集合。

# 广谱词表：从 BOOTSTRAP_KEYWORDS 精选覆盖面最大的宽词，用于倒序走
# 「最新公示」列表页。任何词表都只能覆盖全站登记量的一个切面——覆盖率
# 缺口由 L0 WHO ICTRP 快照 diff 兜底（§2.1，设计既有分工）。
DISCOVERY_WALK_KEYWORDS: list[str] = [
    "心", "癌", "细胞", "治疗", "液", "炎", "瘤", "血管", "糖尿", "肝", "肺", "肾",
]

# recent_seen_ids 水位上限：保留最近 N 个源生号（p1 §4.3）
WATERMARK_MAX_IDS = 5000
# 停止条件（p1 §4.3 简化版）：某词连续 N 页所有号都已在已见集合中 → 换词
STOP_CONSECUTIVE_SEEN_PAGES = 2
# 单词单轮最大翻页数（WAF 预算护栏）
MAX_PAGES_PER_KEYWORD = 10
# CTR 列表页满页行数；不足视为该词末页
LIST_PAGE_SIZE = 20
# 详情键辅助表（ctr_no → uuid/index）上限，供 enrich 免二次检索
DETAIL_KEY_MAP_MAX_ENTRIES = 5000
# enrich 失败重试上限：attempts 达到即转 failed
ENRICH_MAX_ATTEMPTS = 3
# 桩页判定阈值：真实详情/列表页均 >5000 字节（与 _has_cde_content 对齐）
STUB_PAGE_MAX_BYTES = 5000

# ── Chinese → English field label mapping ──────────────────────────────────
# Maps Chinese labels found on CDE detail pages to normalised field names.
# Keys must match the exact <th> text on detail pages.

FIELD_LABEL_MAP: dict[str, str] = {
    "登记号": "source_trial_id",
    "相关登记号": "related_ctr",
    "试验方案编号": "protocol_number",
    "试验通俗题目": "title",
    "试验专业题目": "scientific_title",
    "药物名称": "drug_name",
    "药物类型": "drug_type",
    "适应症": "conditions",
    "试验分类": "study_type",
    "试验分期": "study_phase",
    "试验状态": "status",
    "首次公示信息日期": "registration_date",
    "申请人名称": "sponsors",
    "申请人联系人": "sponsor_contact",
    "目标入组人数": "enrollment",
    "已入组人数": "enrolled_count",
    "实际入组总人数": "actual_enrollment",
    "主要终点指标及评价时间": "primary_endpoint",
    "次要终点指标及评价时间": "secondary_endpoints",
    "入选标准": "inclusion_criteria",
    "排除标准": "exclusion_criteria",
    "试验药": "arm_group_interventions",
    "对照药": "control_drug",
    "机构名称": "locations",                 # 参加机构
    "试验设计": "study_design",
    "设计类型": "design_type",
    "随机化": "randomization",
    "盲法": "blinding",
    "试验范围": "trial_scope",
    "年龄": "age_range",
    "性别": "gender",
    "健康受试者": "healthy_subjects",
    "临床申请受理号": "clinical_application_no",
    "方案最新版本号": "protocol_version",
    "版本日期": "version_date",
    "试验完成日期": "completion_date",
}

# Status mapping (Chinese → English status_type label)
STATUS_MAP: dict[str, str] = {
    "进行中": "Recruiting",
    "已完成": "Completed",
    "尚未招募": "Not yet recruiting",
    "主动暂停": "Suspended",
    "暂停": "Suspended",
    "提前终止": "Terminated",
    "终止": "Terminated",
    "招募完成": "Active, not recruiting",
}

# Study phase mapping
PHASE_MAP: dict[str, str] = {
    "Ⅰ期": "Phase 1",
    "I期": "Phase 1",
    "II期": "Phase 2",
    "Ⅱ期": "Phase 2",
    "Ⅲ期": "Phase 3",
    "III期": "Phase 3",
    "Ⅳ期": "Phase 4",
    "IV期": "Phase 4",
    "I/II期": "Phase 1/Phase 2",
    "Ⅰ/Ⅱ期": "Phase 1/Phase 2",
    "II/III期": "Phase 2/Phase 3",
    "Ⅱ/Ⅲ期": "Phase 2/Phase 3",
    "其它": "Other",
    "其他": "Other",
    "不适用": "Not Applicable",
}

# CDE content markers for distinguishing real pages from WAF challenge
CDE_CONTENT_MARKERS = ["chinadrugtrials", "药物临床试验", "登记号", "CTR", "试验状态"]


# ── Detection helpers ──────────────────────────────────────────────────────


def _has_cde_content(html: str) -> bool:
    """Check if HTML contains real CDE content (vs WAF challenge shell)."""
    if len(html) < 5000:
        return False
    lower = html.lower()
    count = sum(1 for m in CDE_CONTENT_MARKERS if m.lower() in lower)
    return count >= 2


def _is_waf_page(html: str) -> bool:
    """Check if HTML is a WAF challenge page (no real content)."""
    return not _has_cde_content(html)


# ── Parsing helpers ────────────────────────────────────────────────────────


def _parse_int(val: str | None) -> int | None:
    """Extract integer from text that may contain Chinese characters."""
    if not val:
        return None
    cleaned = re.sub(r"[^\d\-]", "", val.strip())
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_date(val: str | None) -> str | None:
    """Parse Chinese/ISO date formats into YYYY-MM-DD."""
    if not val:
        return None
    val = val.strip()
    # YYYY年MM月DD日
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", val)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY-MM-DD or YYYY/MM/DD or YYYY.MM.DD
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", val)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY年MM月
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月", val)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return None


def _is_stub_page(html: str) -> bool:
    """判定 CTR 桩页/空穴页：页面很短且不含任何 CTR 登记号。

    与 chictr 同构（schema v7 三段式）：短页无源生号视为空穴而非错误，
    enrich 时按 skipped 处理；WAF 挑战页在 _fetch_page 层已返回 None，
    走 failed-attempt 路径。
    """
    return len(html) < STUB_PAGE_MAX_BYTES and not re.search(r"CTR\d{6,}", html)


def _cap_map(mapping: dict, cap: int) -> dict:
    """超过上限时丢弃最早的键（dict 保序，先入先丢）。"""
    if len(mapping) <= cap:
        return mapping
    return dict(list(mapping.items())[-cap:])


# ── Collector ──────────────────────────────────────────────────────────────


class ChinaDrugTrialsCollector(WAFBrowserCollector):
    """CDE 中国药物临床试验登记与信息公示平台 collector.

    Uses Playwright (full browser, channel="chromium") to get past the
    Wangsu WAF, then parses the detail-page HTML with BeautifulSoup.

    WAF notes: the Wangsu WAF only allows one page load per browser
    context (hence fresh contexts per attempt).

    Pipeline:
      1. Search by keywords → extract (UUID, CTR, row index) from result table
      2. Fetch detail page: GET /clinicaltrials.searchlistdetail.dhtml?id=<uuid>
      3. Parse HTML table with Chinese field labels
      4. Normalise to NormalisedRecord
    """

    wait_until = "networkidle"
    page_timeout_ms = 45000
    settle_ms = 2000
    retry_backoff_ms = 0
    total_attempts = 3
    list_page_size = LIST_PAGE_SIZE

    def __init__(self) -> None:
        super().__init__("chinadrugtrials")
        # ── HTTP 热路径（王苏两段式 cookie，2026-09-23 实测）───────────
        # 首次 GET 返回 202 + Set-Cookie(FSSBBIl1UgzbN7N80T)；带 cookie
        # 立即重放即 200 真实内容，无需执行 JS。会话 cookie 跨请求复用
        # （实测连抓 5 页 0.25s/页），详情页同源放行。连续 3 次 HTTP
        # 失败 → 本 run 降级 Playwright 路径（与 ChiCTR 同款策略）。
        # 2026-09-24：WAF 升级为 JS 挑战（202 页带 meta 计算载荷），
        # 纯 cookie 重放失效——HTTP 热路径实际不可用，靠 Playwright 降级。
        self._http_tls = threading.local()   # 每 worker 独立 cookie 会话
        self._http_failures = 0
        self._http_degraded = False
        # live_search 的检索字段（线程级：API 并发请求共用单例采集器）
        self._live_tls = threading.local()

    def _http_session(self) -> Any:
        """当前线程的 requests 会话（UA/语言头 + 自动 cookie 保持）。

        线程级实例：多 worker 并行时每线程独立 cookie（王苏 WAF 会话级
        限速/封禁 → 多会话 = 独立限速桶，社区 cookie 池标准做法）。
        """
        sess = getattr(self._http_tls, "sess", None)
        if sess is None:
            import requests as _requests
            sess = _requests.Session()
            sess.headers.update({
                "User-Agent": _STEALTH_UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": ("text/html,application/xhtml+xml,"
                           "application/xml;q=0.9,*/*;q=0.8"),
            })
            self._http_tls.sess = sess
        return sess

    def _http_get(self, url: str) -> Optional[str]:
        """HTTP 直连单页：202 → 带 cookie 重放一次；内容判定复用
        ``_has_cde_content``。成功清零失败计数，失败返回 None（不抛）。
        """
        sess = self._http_session()
        timeout = min(20, self.page_timeout_ms / 1000)
        try:
            r = sess.get(url, timeout=timeout)
            if r.status_code == 202:
                r = sess.get(url, timeout=timeout)
            if r.status_code == 200 and _has_cde_content(r.text):
                self._http_failures = 0
                return r.text
            logger.warning("CTR HTTP 状态异常 %s: code=%d len=%d",
                           url, r.status_code, len(r.text))
        except Exception as exc:
            logger.warning("CTR HTTP 请求失败 %s: %s", url, exc)
        return None

    def _fetch_page(self, url: str,
                    timeout_ms: Optional[int] = None) -> Optional[str]:
        """HTTP 热路径优先，按需降级浏览器（同 ChiCTR 策略）。

        - HTTP 成功 → 直接返回（零浏览器，~0.3s/页）；
        - 连续 3 次 HTTP 失败 → 本 run 弃用 HTTP，改走 Playwright；
        - WafCircuitOpenError（共享熔断器）由浏览器基类照常处理。
        """
        if not self._http_degraded:
            html = self._http_get(url)
            if html is not None:
                return html
            self._http_failures += 1
            if self._http_failures >= 3:
                self._http_degraded = True
                logger.warning(
                    "CTR HTTP 直连连续失败 %d 次，本 run 降级 Playwright",
                    self._http_failures)
        return super()._fetch_page(url, timeout_ms)

    def _is_waf_page(self, html: str) -> bool:
        """Detect WAF challenge pages (module-level content check)."""
        return _is_waf_page(html)

    # ── Keyword search → list of trials ────────────────────────────────

    def _search_keyword(
        self, keyword: str, max_pages: int = MAX_SEARCH_PAGES
    ) -> list[dict[str, str]]:
        """Search CDE by keyword and extract trial entries from results.

        Returns a list of dicts::
            {"uuid": "...", "ctr": "CTR...", "index": "1",
             "status": "...", "drug_name": "...", "conditions": "...",
             "title": "..."}
        """
        trials: list[dict[str, str]] = []
        seen_uuids: set[str] = set()

        for page_num in range(1, max_pages + 1):
                url = (
                    f"{CDE_BASE_URL}{CDE_SEARCH_PATH}"
                    f"?keywords={keyword}&currentpage={page_num}"
                )
                logger.info(
                    "Search keyword '%s' page %d/%d", keyword, page_num, max_pages
                )

                html = self._fetch_page(url)
                if html is None:
                    break

                # Extract rows from search result table
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html, "html.parser")
                table = soup.find("table", class_="searchTable")
                if not table:
                    logger.info("No results table found for keyword '%s'", keyword)
                    break

                rows = table.select("tr")[1:]  # skip header row
                if not rows:
                    logger.info(
                        "No result rows for keyword '%s' page %d", keyword, page_num
                    )
                    break

                page_found = 0
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < 6:
                        continue

                    # Column 2: CTR number (has onclick with UUID)
                    ctr_link = cells[1].find("a") if len(cells) > 1 else None
                    if not ctr_link or not ctr_link.get("id"):
                        continue

                    uuid = ctr_link["id"]
                    if uuid in seen_uuids:
                        continue
                    seen_uuids.add(uuid)

                    entry = {
                        "uuid": uuid,
                        "ctr": ctr_link.get_text(strip=True),
                        "index": ctr_link.get("name", str(len(rows))),
                        "status": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                        "drug_name": cells[3].get_text(strip=True) if len(cells) > 3 else "",
                        "conditions": cells[4].get_text(strip=True) if len(cells) > 4 else "",
                        "title": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                    }
                    trials.append(entry)
                    page_found += 1

                logger.info(
                    "  Page %d: %d trials found", page_num, page_found
                )
                if page_found == 0:
                    break

                time.sleep(self.cfg.request_delay_sec)

        return trials

    # ── 三段式发现层：discover → queue → enrich（schema v7）────────────
    # 表结构见 db/schema.py v7：discovery_cursors（水位线）、discovery_queue
    # （发现/增强解耦的工作队列）。与 collectors/chictr.py 同构。

    def _search_results_page(self, keyword: str, page: int,
                             field: Optional[str] = None) -> list[dict[str, str]]:
        """抓取并解析单个关键词搜索单页，返回 {uuid, ctr, index, title} 列表。

        供三段式发现（discover_new 倒序走列表页）按页调用；WAF 拦截时
        抛 RuntimeError，由 discover_new 按「页失败」处理（游标不前移）。

        field=None（默认）取当前线程的 live_search 字段选择（恒为
        "keywords"，除非正处于 live_search 调用栈内）；显式传参可强制。
        "keywords"：站内通用检索框，匹配药物名称类字段。
        "indication"：二级查询的适应症字段（secondLevel=1）——疾病词必须
        走这里：keywords 对疾病词不可靠（2026-09-24 实测：单字词被站点
        忽略、返回未过滤的默认最新列表，"心肌炎"恒 0 命中），而
        indication="心力衰竭" 精准命中心血管药物。GET 与 POST 等效、
        currentpage 翻页正常（均已实测）。
        """
        if field is None:
            field = getattr(self._live_tls, "field", "keywords")
        if field == "indication":
            url = (
                f"{CDE_BASE_URL}{CDE_SEARCH_PATH}?secondLevel=1"
                f"&indication={keyword}&sort=desc&rule=CTR"
                f"&currentpage={page}"
            )
        else:
            url = (
                f"{CDE_BASE_URL}{CDE_SEARCH_PATH}"
                f"?keywords={keyword}&currentpage={page}"
            )
        html = self._fetch_page(url)
        if html is None:
            raise RuntimeError(f"WAF blocked search page: {url}")

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="searchTable")
        if not table:
            return []

        entries: list[dict[str, str]] = []
        for row in table.select("tr")[1:]:  # skip header row
            cells = row.find_all("td")
            if len(cells) < 6:
                continue
            ctr_link = cells[1].find("a") if len(cells) > 1 else None
            if not ctr_link or not ctr_link.get("id"):
                continue
            entries.append({
                "uuid": ctr_link["id"],
                "ctr": ctr_link.get_text(strip=True),
                "index": ctr_link.get("name", "1"),
                "title": cells[5].get_text(strip=True) if len(cells) > 5 else "",
            })
        return entries

    def _load_cursor(self, mode: str) -> dict:
        """读取本源 discovery_cursors.cursor_json（缺失或损坏时返回 {}）。"""
        conn = get_connection()
        row = conn.execute(
            "SELECT cursor_json FROM discovery_cursors "
            "WHERE source_id = ? AND mode = ?",
            (self.source_id, mode),
        ).fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["cursor_json"])
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cursor(self, mode: str, cursor: dict, success: bool) -> None:
        """UPSERT discovery_cursors（UNIQUE(source_id, mode)，幂等）。

        last_attempted 每次都刷新；last_successful 仅在 success=True 时
        前移——失败轮游标不前移（p1 §4.3 语义）。
        """
        payload = json.dumps(cursor, ensure_ascii=False)
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO discovery_cursors
                (source_id, mode, cursor_json, last_attempted,
                 last_successful, status)
            VALUES (?, ?, ?, datetime('now'),
                    CASE WHEN ? = 1 THEN datetime('now') ELSE NULL END,
                    'active')
            ON CONFLICT(source_id, mode) DO UPDATE SET
                cursor_json = excluded.cursor_json,
                last_attempted = excluded.last_attempted,
                last_successful = COALESCE(excluded.last_successful,
                                           discovery_cursors.last_successful),
                status = 'active'
            """,
            (self.source_id, mode, payload, 1 if success else 0),
        )
        conn.commit()

    def _enqueue(self, source_trial_id: str, via: str,
                 title: Optional[str] = None) -> bool:
        """新号写入 discovery_queue（INSERT OR IGNORE 幂等）。

        title 是列表页上免费拿到的疾病线索：存进队列后 enrich 可按关键词
        精准优先，不必盲增强。已存在的行（如 ictrp_diff 先入队、无标题）
        用列表页标题回填一次。
        返回是否真的新插入（已存在 → False，便于统计 queued 数）。
        """
        conn = get_connection()
        cur = conn.execute(
            "INSERT OR IGNORE INTO discovery_queue "
            "(source_id, source_trial_id, discovered_via, title) "
            "VALUES (?, ?, ?, ?)",
            (self.source_id, source_trial_id, via, title),
        )
        if cur.rowcount == 0 and title:
            conn.execute(
                "UPDATE discovery_queue SET title = ? "
                "WHERE source_id = ? AND source_trial_id = ? AND title IS NULL",
                (title, self.source_id, source_trial_id),
            )
        conn.commit()
        return cur.rowcount > 0

    def _mark_queue(self, discovery_id: int, state: str) -> None:
        """队列行终态：enriched / skipped（清除 last_error）。"""
        conn = get_connection()
        conn.execute(
            "UPDATE discovery_queue SET state = ?, last_error = NULL "
            "WHERE discovery_id = ?",
            (state, discovery_id),
        )
        conn.commit()

    def _mark_queue_failed_attempt(self, discovery_id: int, error: str) -> None:
        """失败尝试：attempts+1 并记 last_error；达上限转 failed。"""
        conn = get_connection()
        conn.execute(
            "UPDATE discovery_queue SET attempts = attempts + 1, "
            "last_error = ?, "
            "state = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE state END "
            "WHERE discovery_id = ?",
            ((error or "unknown error")[:1000], ENRICH_MAX_ATTEMPTS,
             discovery_id),
        )
        conn.commit()

    def _resolve_detail_key(self, ctr_number: str) -> Optional[dict]:
        """解析详情键 {uuid, index}：辅助表 → 历史 source_url → 按号检索兜底。

        discovery_queue 只存 CTR 号；详情 URL 需要 UUID（列表页发现时随手
        记入 cursor_json.detail_keys 辅助表、非水位，失败轮也保留），离线
        缺失时回查本源已有记录的 source_url（…dhtml?id=<uuid>）。
        前两级都查不到（如 ictrp_diff 入队的、从未被关键词走查见过的号）
        时，用完整登记号走 keywords 检索（1 个列表页预算）反查详情键，
        并写回辅助表使后续重试免于重复检索；检索不到才返回 None。
        """
        cursor = self._load_cursor("list_walk")
        key = cursor.get("detail_keys", {}).get(ctr_number)
        if key:
            return key
        conn = get_connection()
        row = conn.execute(
            "SELECT source_url FROM registry_records "
            "WHERE source_id = ? AND source_trial_id = ? AND is_latest = 1",
            (self.source_id, ctr_number),
        ).fetchone()
        if row and row["source_url"] and "id=" in row["source_url"]:
            tail = row["source_url"].rsplit("id=", 1)[-1]
            uuid = tail.split("&", 1)[0]
            if uuid:
                return {"uuid": uuid, "index": "1"}
        try:
            entries = self._lookup_by_regno(ctr_number)
        except WafCircuitOpenError:
            raise  # 熔断穿透：由 enrich 循环统一中止，不折算成行失败
        except Exception as exc:
            logger.warning("CTR 按号反查 uuid 失败 %s: %s", ctr_number, exc)
            return None
        hit = next((e for e in entries
                    if e.get("ctr") == ctr_number and e.get("uuid")), None)
        if hit:
            key = {"uuid": hit["uuid"], "index": hit.get("index", "1")}
            self._remember_detail_key(ctr_number, key)
            return key
        logger.warning("CTR 按号反查无命中 %s", ctr_number)
        return None

    def _lookup_by_regno(self, ctr_number: str) -> list[dict[str, str]]:
        """按完整登记号检索（keywords 参数可匹配登记号），返回结果行。

        供 enrich 侧反查详情键：ictrp_diff 入队的行只有登记号没有 uuid。
        """
        return self._search_results_page(ctr_number, 1)

    def _remember_detail_key(self, ctr_number: str, key: dict) -> None:
        """把反查得到的 uuid 写回游标辅助表（success=False，不前移水位）。"""
        cursor = self._load_cursor("list_walk")
        detail_keys = dict(cursor.get("detail_keys", {}))
        detail_keys[ctr_number] = key
        self._save_cursor(
            "list_walk",
            {"recent_seen_ids": cursor.get("recent_seen_ids", []),
             "detail_keys": _cap_map(detail_keys,
                                     DETAIL_KEY_MAP_MAX_ENTRIES)},
            success=False,
        )

    def _forget_detail_key(self, ctr_number: str) -> None:
        """从游标辅助表移除已消费的 uuid 映射（控制 cursor_json 体积）。

        success=False → last_successful 不因清理动作而前移。
        """
        cursor = self._load_cursor("list_walk")
        detail_keys = cursor.get("detail_keys", {})
        if ctr_number in detail_keys:
            detail_keys.pop(ctr_number)
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": cursor.get("recent_seen_ids", []),
                 "detail_keys": detail_keys},
                success=False,
            )

    # ── 实时检索钩子（live_search，见 browser_base） ────────────────────

    def _choose_search_field(self, keyword: str) -> tuple[str, str]:
        """按词义选原站检索字段，返回 (field, kind)。

        站内通用 keywords 检索匹配药物名称/申办者类字段，对疾病词不可靠
        （2026-09-24 实测：单字被忽略、返回未过滤默认列表，"心肌炎"恒
        0 命中），适应症字段才按病名精确过滤；而申办者词走 indication
        必 0 命中（2026-09-29「罗氏」案例）。规则：

        - 非 CJK（英文药名/申办者/登记号）→ keywords（CTR 无英文适应症）；
        - CJK 单字 → indication（keywords 对单字返回未过滤默认列表，
          假阳性比诚实 0 更糟）；
        - 申办者词（机构名特征词，或术语表认不出但本地库 sponsors 已含
          该词——简称如「罗氏」靠这条识别）→ keywords（2026-09-29 实测
          keywords=罗氏 → 240 条罗氏试验；二级查询另有 appliers 申请人
          字段可做精确申办者检索，暂不启用）；
        - 疾病/标志物词（静态或已学术语）→ indication；
        - 其余未知多字词 → keywords，0 命中后由 live_search 再做一次
          indication 重试（术语表没收录的疾病词兜底）。

        kind 供 live_search 区分是否需要重试（sponsor 词去适应症重试
        必 0，纯属浪费请求）。
        """
        from core import terminology as _term

        kw = (keyword or "").strip()
        if not _CJK_RE.search(kw):
            return "keywords", "ascii"
        if len(kw) == 1:
            return "indication", "single_char"
        if _term.has_sponsor_marker(kw):
            return "keywords", "sponsor"
        # get_connection 是线程级缓存连接，不能 close（与 _load_cursor 同源）
        conn = get_connection()
        if _term.is_condition_term(kw, conn):
            return "indication", "condition"
        if _term.sponsors_in_library(kw, conn):
            return "keywords", "sponsor"
        return "keywords", "unknown"

    def live_search(self, keyword: str, max_pages: int = 1,
                    start_page: int = 1, field: Optional[str] = None) -> dict:
        """实时检索按词义选字段（2026-09-29 升级，替代「CJK 一刀切」）。

        路由规则见 _choose_search_field；``field`` 供服务端续批沿用首查
        生效字段——未知词可能靠 indication 回退才命中，续批若重新分类
        会回 keywords 丢上下文。发现走查（discover_new）与登记号反查
        （_lookup_by_regno）维持 keywords 路径不变——走查依赖宽词的默认
        列表兜底行为，登记号只有 keywords 能匹配。
        """
        chosen, kind = (field, "resumed") if field in ("keywords",
                                                       "indication") \
            else self._choose_search_field(keyword)
        self._live_tls.field = chosen
        try:
            stats = super().live_search(keyword, max_pages=max_pages,
                                        start_page=start_page)
        finally:
            self._live_tls.field = "keywords"
        stats["field_used"] = chosen
        # 未知词兜底：keywords 完整跑完仍 0 命中（非熔断/页失败）→
        # indication 重试一次。仅限首查（续批沿用字段不重试）；0 命中的
        # 重试在第 1 页即结束，额外请求成本可忽略。
        if (kind == "unknown" and start_page == 1
                and stats["stopped_reason"] == "completed"
                and stats["found"] == 0):
            self._live_tls.field = "indication"
            try:
                retry = super().live_search(keyword, max_pages=max_pages,
                                            start_page=1)
            finally:
                self._live_tls.field = "keywords"
            if retry["found"]:
                retry["field_used"] = "indication"
                retry["field_fallback"] = True
                return retry
        return stats

    # ── Local API adapter (read-only; no queue/database writes) ─────────

    def api_search(self, query: str, page: int = 1,
                   field: Optional[str] = None) -> dict[str, Any]:
        """Return one CDE search page in a stable JSON-ready contract.

        This is an unofficial adapter over the public HTML pages, not an
        upstream CDE API.  It deliberately bypasses ``live_search`` because
        that workflow enqueues hits; callers of the local API get a read-only
        view with the same semantic field routing.
        """
        query = str(query or "").strip()
        if not query:
            raise ValueError("query must not be blank")
        page = int(page)
        if page < 1:
            raise ValueError("page must be at least 1")

        if field not in ("keywords", "indication"):
            field, _ = self._choose_search_field(query)
        entries = self._search_results_page(query, page, field=field)
        items = []
        for entry in entries:
            trial_id = entry.get("ctr", "")
            uuid = entry.get("uuid", "")
            index = entry.get("index", "1")
            if not trial_id:
                continue
            items.append({
                "source_trial_id": trial_id,
                "title": entry.get("title", ""),
                "uuid": uuid or None,
                "index": index,
                "url": (
                    f"{CDE_BASE_URL}{CDE_DETAIL_PATH}?id={uuid}"
                    f"&ckm_index={index}" if uuid else None
                ),
            })
        return {
            "source": "CTR",
            "query": query,
            "query_field": field,
            "page": page,
            "page_size": LIST_PAGE_SIZE,
            "site_total": None,
            "has_more": len(items) >= LIST_PAGE_SIZE,
            "items": items,
        }

    def api_study(self, ctr_number: str) -> Optional[dict[str, Any]]:
        """Fetch one CDE record by CTR number without mutating local state."""
        trial_id = str(ctr_number or "").strip().upper()
        if not re.fullmatch(r"CTR\d{8,}", trial_id):
            raise ValueError(f"not a CTR registration number: {trial_id}")

        entries = self._lookup_by_regno(trial_id)
        hit = next(
            (entry for entry in entries
             if entry.get("ctr", "").upper() == trial_id
             and entry.get("uuid")),
            None,
        )
        if hit is None:
            return None

        uuid = hit["uuid"]
        index = hit.get("index", "1")
        if self.cfg.request_delay_sec:
            time.sleep(self.cfg.request_delay_sec)
        html = self.fetch_detail_page(uuid, index)
        if html is None or _is_stub_page(html):
            return None

        fields = self.parse_detail_html(html, trial_id)
        canonical = asdict(self.normalise({
            "ctr_number": trial_id,
            "uuid": uuid,
            "html": html,
            "parsed_fields": fields,
        }))
        canonical.pop("raw_payload", None)
        for name in (
            "conditions", "interventions", "countries", "locations",
            "sponsors", "secondary_endpoints", "arm_group_interventions",
        ):
            value = canonical.get(name)
            if isinstance(value, str):
                try:
                    canonical[name] = json.loads(value)
                except (TypeError, ValueError):
                    canonical[name] = [value]

        url = (f"{CDE_BASE_URL}{CDE_DETAIL_PATH}?id={uuid}"
               f"&ckm_index={index}")
        return {
            "source": "CTR",
            "source_trial_id": trial_id,
            "uuid": uuid,
            "index": index,
            "url": url,
            "fields": fields,
            "canonical": canonical,
        }

    def _entry_identity(self, entry: dict) -> tuple[Optional[str], str]:
        return entry.get("ctr") or None, entry.get("title", "")

    def _entry_url(self, entry: dict) -> Optional[str]:
        uuid = entry.get("uuid")
        if not uuid:
            return None
        return (f"{CDE_BASE_URL}{CDE_DETAIL_PATH}?id={uuid}"
                f"&ckm_index={entry.get('index', '1')}")

    def _absorb_live_entries(self, entries: list) -> int:
        """live_search 吸收：入队 + detail_keys 辅助表回填（水位线不动）。

        uuid 是 enrich 侧解析详情页的唯一即时来源（离线回查要等
        source_url 落库），必须在入队同一轮写入辅助表；success=False
        保证不前移 list_walk 水位。
        """
        cursor = self._load_cursor("list_walk")
        # 浅拷贝隔离：cursor["detail_keys"] 原地变更会让「有无新增」的比较
        # 恒为 False（同一对象），辅助表永远不会落盘——必须拷贝后再改。
        detail_keys = dict(cursor.get("detail_keys", {}))
        queued = 0
        for entry in entries:
            no = entry.get("ctr", "")
            if not no:
                continue
            if entry.get("uuid"):
                detail_keys[no] = {
                    "uuid": entry["uuid"],
                    "index": entry.get("index", "1"),
                }
            if self._enqueue(no, "live_search", title=entry.get("title")):
                queued += 1
        if detail_keys != cursor.get("detail_keys", {}):
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": cursor.get("recent_seen_ids", []),
                 "detail_keys": _cap_map(detail_keys,
                                         DETAIL_KEY_MAP_MAX_ENTRIES)},
                success=False,
            )
        return queued

    def discover_new(self, since: Optional[str] = None) -> dict:
        """阶段 1（发现）：广谱词表倒序走「最新公示」列表页，新号入队。

        水位线 = discovery_cursors(mode='list_walk') 的 recent_seen_ids
        （保留最近 WATERMARK_MAX_IDS 个源生号；Spike S3 证实号序即时间序）。

        停止条件（p1 §4.3 简化版）：
          - 某词连续 STOP_CONSECUTIVE_SEEN_PAGES 页所有号都已在已见集合
            （水位 ∪ 本轮已见）中 → 换下一个词；
          - 短页（< LIST_PAGE_SIZE 行）视为该词末页；
          - 任一页失败 → 本轮终止，游标水位不前移，已见号照常入队
            （INSERT OR IGNORE 保证重复入队幂等）。

        返回 {queued, seen, pages_walked, stopped_reason}；
        stopped_reason ∈ {'completed', 'page_failed'}。since 仅记录日志。
        """
        if since:
            logger.info("CTR discover_new (since %s，仅记录)", since)

        cursor = self._load_cursor("list_walk")
        recent_seen = [str(x) for x in cursor.get("recent_seen_ids", [])]
        watermark_set = set(recent_seen)
        detail_keys = dict(cursor.get("detail_keys", {}))

        queued = 0
        pages_walked = 0
        stopped_reason = "completed"
        round_ids: list[str] = []       # 本轮去重后的源生号（含已见）
        round_ids_set: set[str] = set()

        for keyword in DISCOVERY_WALK_KEYWORDS:
            consecutive_seen = 0
            for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
                try:
                    entries = self._search_results_page(keyword, page)
                except WafCircuitOpenError:
                    # 熔断即停：水位不前移，本轮立即中止（设计 §2.5）
                    stopped_reason = "circuit_open"
                    raise
                except Exception as exc:
                    logger.warning("CTR 列表页失败 '%s' p%d: %s",
                                   keyword, page, exc)
                    stopped_reason = "page_failed"
                    break

                pages_walked += 1
                if not entries:
                    break  # 空页 → 该词已到末尾

                # 停止条件判据 = 水位 ∪ 本轮已见（跨词重复的号也算已见）
                known_set = watermark_set | round_ids_set
                all_seen = all(e.get("ctr") in known_set for e in entries)

                for entry in entries:
                    no = entry.get("ctr", "")
                    if not no or no in round_ids_set:
                        continue
                    round_ids_set.add(no)
                    round_ids.append(no)
                    if entry.get("uuid"):
                        detail_keys[no] = {
                            "uuid": entry["uuid"],
                            "index": entry.get("index", "1"),
                        }
                    if no not in watermark_set:
                        if self._enqueue(no, "list_walk",
                                         title=entry.get("title")):
                            queued += 1

                if all_seen:
                    consecutive_seen += 1
                    if consecutive_seen >= STOP_CONSECUTIVE_SEEN_PAGES:
                        break  # 已越过水位线 → 下一个词
                else:
                    consecutive_seen = 0

                if len(entries) < LIST_PAGE_SIZE:
                    break  # 短页 → 该词末页

                time.sleep(self.cfg.request_delay_sec)

            if stopped_reason == "page_failed":
                break  # 熔断式终止：本轮不再尝试其它词

        # 游标维护：
        # - recent_seen_ids 水位仅在整轮成功时前移（失败轮不前移，下轮重扫，
        #   队列 INSERT OR IGNORE 保证幂等）；
        # - detail_keys 辅助表随入队推进（非水位，失败轮也保留，否则失败前
        #   入队的号在 enrich 时无法解析详情键）。
        if stopped_reason != "page_failed":
            merged = round_ids + [r for r in recent_seen
                                  if r not in round_ids_set]
            new_cursor = {
                "recent_seen_ids": merged[:WATERMARK_MAX_IDS],
                "detail_keys": _cap_map(detail_keys,
                                        DETAIL_KEY_MAP_MAX_ENTRIES),
            }
            self._save_cursor("list_walk", new_cursor, success=True)
        else:
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": recent_seen,
                 "detail_keys": _cap_map(detail_keys,
                                         DETAIL_KEY_MAP_MAX_ENTRIES)},
                success=False,
            )

        stats = {
            "queued": queued,
            "seen": len(round_ids),
            "pages_walked": pages_walked,
            "stopped_reason": stopped_reason,
        }
        logger.info("CTR discover_new: %s", stats)
        return stats

    def enrich_pending(self, limit: Optional[int] = None,
                       keywords: Optional[list[str]] = None,
                       source_trial_ids: Optional[list[str]] = None,
                       workers: int = 1) -> dict:
        """阶段 2（增强）：按预算消化 discovery_queue 本源 pending 行。

        keywords 给定时走「精准优先」：先增强标题命中任一关键词的行
        （大小写不敏感），预算未用满再用不命中的行回填——把 WAF 预算
        花在相关试验上。否则 ORDER BY discovery_id（先进先增强）；limit
        默认取 cfg.max_records_per_run（0 → 50）。逐条抓详情页并复用
        parse_detail_html → normalise → BaseCollector._upsert_record 全链路：
          - 成功 → state=enriched（同时清理 detail_keys 辅助表条目）；
          - 失败 → attempts+1、last_error 记录；attempts ≥ ENRICH_MAX_ATTEMPTS
            转 failed；
          - 短页且无 CTR 号（桩页/空穴，非错误）→ state=skipped。

        返回 {enriched, failed, skipped, records[, keyword_matched]}。
        """
        if limit is None:
            # enrich 预算优先取专用键（G2：单晚 ≤50 详情请求）
            limit = ((self.cfg.extra or {}).get("enrich_batch_size")
                     or self.cfg.max_records_per_run or 50)
        conn = get_connection()
        keyword_matched: Optional[int] = None
        if source_trial_ids:
            wanted = list(dict.fromkeys(str(x) for x in source_trial_ids if x))
            placeholders = ",".join("?" for _ in wanted)
            rows = conn.execute(
                "SELECT discovery_id, source_trial_id FROM discovery_queue "
                "WHERE source_id = ? AND state = 'pending' "
                f"AND source_trial_id IN ({placeholders}) "
                "ORDER BY discovery_id LIMIT ?",
                [self.source_id, *wanted, limit],
            ).fetchall()
        elif keywords:
            kws = [k for k in (k.strip() for k in keywords) if k]
            kw_conds = " OR ".join(
                ["lower(COALESCE(title,'')) LIKE ?"] * len(kws))
            kw_params = [f"%{k.lower()}%" for k in kws]
            rows = conn.execute(
                "SELECT discovery_id, source_trial_id FROM discovery_queue "
                f"WHERE source_id = ? AND state = 'pending' "
                f"AND ({kw_conds}) "
                "ORDER BY discovery_id LIMIT ?",
                [self.source_id, *kw_params, limit],
            ).fetchall()
            keyword_matched = len(rows)
            if len(rows) < limit:
                # 0 命中也照常回填：预算不得因关键词零命中而整体跳过
                chosen = {r["discovery_id"] for r in rows}
                filler = conn.execute(
                    "SELECT discovery_id, source_trial_id FROM discovery_queue "
                    f"WHERE source_id = ? AND state = 'pending' "
                    f"AND NOT ({kw_conds}) "
                    "ORDER BY discovery_id LIMIT ?",
                    [self.source_id, *kw_params, limit - len(rows)],
                ).fetchall()
                rows = list(rows) + [r for r in filler
                                     if r["discovery_id"] not in chosen]
            logger.info("CTR enrich 关键词优先：%d/%d 条标题命中 %s",
                        keyword_matched, len(rows), kws)
        else:
            rows = conn.execute(
                "SELECT discovery_id, source_trial_id FROM discovery_queue "
                "WHERE source_id = ? AND state = 'pending' "
                "ORDER BY discovery_id LIMIT ?",
                (self.source_id, limit),
            ).fetchall()

        stats: dict = {"enriched": 0, "failed": 0, "skipped": 0,
                       "records": []}
        if keyword_matched is not None:
            stats["keyword_matched"] = keyword_matched
        def consume(result: Optional[dict]) -> None:
            """聚合 _enrich_row 的产出（None = 熔断中止哨兵）。"""
            if result is None:
                return
            stats[result["outcome"]] += 1
            if result.get("raw") is not None:
                stats["records"].append(result["raw"])
            stats["changes"] = stats.get("changes", 0) + result.get("changes", 0)

        if workers > 1 and len(rows) > 1:
            # 多会话并行（cookie 池）：见 _http_session 注释；熔断共享，
            # 任一 worker 触发后其余 worker 快速失败，主循环取消余量。
            from concurrent.futures import ThreadPoolExecutor
            stats["workers"] = min(workers, len(rows))
            with ThreadPoolExecutor(
                    max_workers=stats["workers"]) as pool:
                try:
                    for result in pool.map(self._enrich_row, rows):
                        if result is None:
                            pool.shutdown(cancel_futures=True)
                            logger.error("CTR enrich 熔断中止，"
                                         "剩余 pending 留待下轮")
                            break
                        consume(result)
                except WafCircuitOpenError:
                    pool.shutdown(cancel_futures=True)
                    logger.error("CTR enrich 熔断中止，剩余 pending 留待下轮")
        else:
            for row in rows:
                result = self._enrich_row(row)
                if result is None:
                    logger.error("CTR enrich 熔断中止，剩余 pending 留待下轮")
                    break
                consume(result)

        logger.info("CTR enrich_pending: enriched=%d failed=%d skipped=%d"
                    "%s", stats["enriched"], stats["failed"], stats["skipped"],
                    f" (workers={stats['workers']})" if "workers" in stats else "")
        return stats

    def _enrich_row(self, row) -> Optional[dict]:
        """enrich_pending 的单行体（多 worker 时在线程内执行）。

        返回 {"outcome": "enriched"|"failed"|"skipped", "raw": …, "changes": n}
        或 None（熔断中止哨兵——共享熔断器已打开，本轮应整体停止）。
        """
        discovery_id = row["discovery_id"]
        ctr_number = row["source_trial_id"]
        time.sleep(self.cfg.request_delay_sec)
        try:
            key = self._resolve_detail_key(ctr_number)
            if not key or not key.get("uuid"):
                raise RuntimeError(f"无法解析详情键 uuid: {ctr_number}")

            html = self.fetch_detail_page(
                key["uuid"], key.get("index", "1")
            )
            if html is None:
                raise RuntimeError(
                    f"详情页抓取失败（WAF）: {ctr_number}"
                )
            if _is_stub_page(html):
                # 空穴页：短且无源生号，非错误 → skipped
                logger.info("CTR enrich 桩页跳过 %s", ctr_number)
                self._mark_queue(discovery_id, "skipped")
                return {"outcome": "skipped", "raw": None, "changes": 0}

            parsed = self.parse_detail_html(html, ctr_number)
            raw = {
                "ctr_number": parsed.get("source_trial_id") or ctr_number,
                "uuid": key["uuid"],
                "html": html,
                "parsed_fields": parsed,
            }
            outcome = self._upsert_and_emit_events(self.normalise(raw))
            self._mark_queue(discovery_id, "enriched")
            self._forget_detail_key(ctr_number)
            logger.info("CTR enrich 完成 %s", ctr_number)
            return {"outcome": "enriched", "raw": raw,
                    "changes": outcome["changes"]}
        except WafCircuitOpenError:
            # 熔断哨兵：不计失败、不动 attempts（行仍为 pending）
            return None
        except Exception as exc:
            logger.warning("CTR enrich 失败 %s: %s", ctr_number, exc)
            self._mark_queue_failed_attempt(discovery_id, str(exc))
            return {"outcome": "failed", "raw": None, "changes": 0}

    def refresh_one(self, ctr_number: str) -> dict:
        """复查钩子：重访一条已入库记录的详情页（core.refresh 驱动）。

        详情键解析与 enrich 同源（游标辅助表 → 历史 source_url 回退），
        upsert hash-skip 保证未变记录零版本噪声；变化时补写 trial_events。
        返回 {"action": "new"|"updated"|"skipped", "changes": int[, "stub"]}。
        """
        key = self._resolve_detail_key(ctr_number)
        if not key or not key.get("uuid"):
            raise RuntimeError(f"无法解析详情键 uuid: {ctr_number}")

        html = self.fetch_detail_page(
            key["uuid"], key.get("index", "1")
        )
        if html is None:
            raise RuntimeError(
                f"详情页抓取失败（WAF）: {ctr_number}"
            )
        if _is_stub_page(html):
            return {"action": "skipped", "changes": 0, "stub": True}

        parsed = self.parse_detail_html(html, ctr_number)
        raw = {
            "ctr_number": parsed.get("source_trial_id") or ctr_number,
            "uuid": key["uuid"],
            "html": html,
            "parsed_fields": parsed,
        }
        return self._upsert_and_emit_events(self.normalise(raw))

    # ── Detail page ────────────────────────────────────────────────────

    def fetch_detail_page(self, uuid: str, index: str = "1") -> str | None:
        """Fetch a trial detail page by UUID.

        URL: /clinicaltrials.searchlistdetail.dhtml?id=<uuid>&ckm_index=<idx>
        """
        url = f"{CDE_BASE_URL}{CDE_DETAIL_PATH}?id={uuid}&ckm_index={index}"
        logger.info("Fetching detail for UUID %s", uuid)
        return self._fetch_page(url)

    # ── HTML Parsing ───────────────────────────────────────────────────

    @staticmethod
    def parse_detail_html(html: str, ctr_number: str) -> dict[str, Any]:
        """Parse CDE detail page HTML into structured fields.

        1. Scans <th> elements and reads adjacent <td> values
        2. Maps Chinese labels → English field names via FIELD_LABEL_MAP
        3. Post-processes dates, integers, status, phase
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        fields: dict[str, Any] = {
            "source_trial_id": ctr_number,
            "_raw_html_length": len(html),
        }

        def _set_field(key: str, value: str) -> None:
            value = value.strip()
            if not value:
                return
            existing = fields.get(key)
            if existing is None or len(value) > len(str(existing)):
                fields[key] = value

        # Scan <th> → <td> pairs — handle multi-column rows with
        # multiple <th>-<td> pairs per <tr>
        for row in soup.select("table tr"):
            ths = row.find_all("th")
            tds = row.find_all("td")
            for th, td in zip(ths, tds):
                label = th.get_text(strip=True)
                value = td.get_text(strip=True, separator="\n")
                eng_key = FIELD_LABEL_MAP.get(label)
                if eng_key and value:
                    _set_field(eng_key, value)

        # Handle tables where <th> labels are in a separate header row
        # from <td> data (e.g. 机构名称, 主要研究者 tables).
        for table in soup.select("table"):
            rows = table.select("tr")
            if len(rows) < 2:
                continue
            header_row = rows[0]
            # Only match if header row has <th> but no <td>
            headers = header_row.find_all("th")
            if not headers or header_row.find_all("td"):
                continue
            # Process each data row
            for data_row in rows[1:]:
                cells = data_row.find_all("td")
                if len(cells) != len(headers):
                    continue
                for header, cell in zip(headers, cells):
                    label = header.get_text(strip=True)
                    value = cell.get_text(strip=True)
                    eng_key = FIELD_LABEL_MAP.get(label)
                    if eng_key and value:
                        existing = fields.get(eng_key)
                        separator = "\n" if existing else ""
                        fields[eng_key] = (existing + separator + value) if existing else value

        # ── Post-process ───────────────────────────────────────────────

        # Parse dates
        for date_field in ("registration_date", "version_date", "completion_date"):
            if date_field in fields:
                fields[date_field] = _parse_date(fields[date_field])

        # Parse enrollment from "国内: 60 ；" format
        enrollment_raw = fields.get("enrollment", "")
        if enrollment_raw:
            nums = re.findall(r"\d+", enrollment_raw)
            if nums:
                fields["enrollment"] = int(nums[0])
            else:
                fields["enrollment"] = None

        # Map status
        status_raw = fields.get("status", "")
        if status_raw:
            mapped = STATUS_MAP.get(status_raw)
            if mapped is None:
                for cn, en in STATUS_MAP.items():
                    if cn in status_raw:
                        mapped = en
                        break
            fields["status"] = mapped or status_raw

        # Map study_phase
        phase_raw = fields.get("study_phase", "")
        if phase_raw:
            mapped = PHASE_MAP.get(phase_raw)
            if mapped is None:
                for cn, en in PHASE_MAP.items():
                    if cn in phase_raw:
                        mapped = en
                        break
            fields["study_phase"] = mapped or phase_raw

        # Combine inclusion + exclusion into eligibility_criteria
        inclusion = fields.pop("inclusion_criteria", "")
        exclusion = fields.pop("exclusion_criteria", "")
        parts = []
        if inclusion:
            parts.append(f"Inclusion: {inclusion[:2000]}")
        if exclusion:
            parts.append(f"Exclusion: {exclusion[:2000]}")
        if parts:
            fields["eligibility_criteria"] = "\n".join(parts)

        return fields

    # ── BaseCollector interface ────────────────────────────────────────

    def fetch_new_or_updated(
        self, since: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Fetch trial records from CDE（三段式兼容包装）。

        内部 = discover_new（列表倒序发现 + 水位线入队）→ enrich_pending
        （按预算抓详情 + upsert）。对外行为与旧实现兼容：返回本轮增强
        产出的 raw dict 列表（ctr_number / uuid / html / parsed_fields）。
        纯关键词旧路径保留在 fetch_new_or_updated_legacy，供回退。
        """
        if getattr(self, "use_legacy_discovery", False):
            return self.fetch_new_or_updated_legacy(since)
        discovery = self.discover_new(since)
        enrich = self.enrich_pending()
        records: list[dict[str, Any]] = enrich.get("records", [])
        logger.info(
            "CTR 三段式完成：discover=%s enrich(enriched=%d failed=%d "
            "skipped=%d)", discovery, enrich["enriched"], enrich["failed"],
            enrich["skipped"],
        )
        return records

    def fetch_new_or_updated_legacy(
        self, since: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Legacy 关键词发现路径（回退用，行为与历史版本一致）。

        Bootstrap mode (since=None): search by all BOOTSTRAP_KEYWORDS,
        collect unique trial entries, fetch each detail page.

        Incremental mode (since is set): search by keywords to find new
        trials. The BaseCollector upsert handles dedup.

        Returns list of raw dicts with keys: ctr_number, html, parsed_fields.
        """
        if since:
            logger.info("CDE incremental mode (since %s)", since)

        # Phase 1: Collect trial entries across all keywords
        all_trials: list[dict[str, str]] = []
        seen_uuids: set[str] = set()

        for idx, keyword in enumerate(BOOTSTRAP_KEYWORDS):
            logger.info("Keyword search %d/%d: '%s'", idx + 1, len(BOOTSTRAP_KEYWORDS), keyword)
            try:
                trials = self._search_keyword(keyword)
                new_count = 0
                for t in trials:
                    if t["uuid"] not in seen_uuids:
                        seen_uuids.add(t["uuid"])
                        all_trials.append(t)
                        new_count += 1
                logger.info(
                    "  %d trials from '%s' (%d new)",
                    len(trials), keyword, new_count,
                )
            except Exception as exc:
                logger.error("Keyword search failed for '%s': %s", keyword, exc)

            if idx < len(BOOTSTRAP_KEYWORDS) - 1:
                time.sleep(self.cfg.request_delay_sec * 2)

        logger.info("Total unique trials: %d", len(all_trials))

        # Limit to configured max
        max_records = self.cfg.max_records_per_run
        if len(all_trials) > max_records:
            logger.warning(
                "Found %d trials, limiting to %d", len(all_trials), max_records
            )
            all_trials = all_trials[:max_records]

        # Phase 2: Fetch detail pages.
        # Trials already stored (is_latest) are skipped without fetching so
        # repeated batch runs advance coverage instead of refetching.  A run
        # aborts after 3 consecutive failures (WAF circuit breaker).
        from db.connection import get_connection
        existing = {r[0] for r in get_connection().execute(
            "SELECT source_trial_id FROM registry_records "
            "WHERE source_id=? AND is_latest=1",
            (self.source_id,))}

        raw_records: list[dict[str, Any]] = []
        consecutive_failures = 0
        for trial in all_trials:
            if consecutive_failures >= 3:
                logger.warning("WAF circuit breaker: 3 consecutive fetch "
                               "failures — stopping this batch")
                break
            if trial["ctr"] in existing:
                logger.info("  %s already stored, skipping", trial["ctr"])
                continue

            try:
                html = self.fetch_detail_page(trial["uuid"], trial.get("index", "1"))
                if html is None:
                    logger.warning("Detail unavailable for %s (%s)", trial["ctr"], trial["uuid"])
                    continue

                parsed = self.parse_detail_html(html, trial["ctr"])
                # Merge search result metadata into parsed fields
                if not parsed.get("title") and trial.get("title"):
                    parsed["title"] = trial["title"]
                if not parsed.get("status") and trial.get("status"):
                    parsed["status"] = trial["status"]
                if not parsed.get("conditions") and trial.get("conditions"):
                    parsed["conditions"] = trial["conditions"]
                if not parsed.get("drug_name") and trial.get("drug_name"):
                    parsed["drug_name"] = trial["drug_name"]

                raw_records.append({
                    "ctr_number": trial["ctr"],
                    "uuid": trial["uuid"],
                    "html": html,
                    "parsed_fields": parsed,
                })
                consecutive_failures = 0

            except Exception as exc:
                logger.error("Failed for %s: %s", trial["ctr"], exc)
                consecutive_failures += 1

            time.sleep(self.cfg.request_delay_sec)

        logger.info("Fetched %d / %d detail pages", len(raw_records), len(all_trials))
        return raw_records

    def normalise(self, raw: dict[str, Any]) -> NormalisedRecord:
        """Convert a raw CDE record to NormalisedRecord."""
        fields: dict[str, Any] = raw.get("parsed_fields", {})
        ctr_number: str = raw.get("ctr_number", "")

        return NormalisedRecord(
            source_trial_id=ctr_number,
            title=fields.get("title") or "",
            scientific_title=fields.get("scientific_title"),
            study_type=fields.get("study_type"),
            status=fields.get("status"),
            enrollment=fields.get("enrollment"),
            registration_date=fields.get("registration_date"),
            completion_date=fields.get("completion_date"),
            last_updated_at_source=None,
            conditions=to_json_array(fields.get("conditions")),
            interventions=to_json_array(
                fields.get("drug_name") or fields.get("interventions")
            ),
            countries='["China"]',
            locations=to_json_array(fields.get("locations")),
            sponsors=to_json_array(fields.get("sponsors")),
            study_phase=fields.get("study_phase"),
            study_design=fields.get("study_design"),
            eligibility_criteria=fields.get("eligibility_criteria"),
            primary_endpoint=fields.get("primary_endpoint"),
            secondary_endpoints=to_json_array(fields.get("secondary_endpoints")),
            arm_group_interventions=fields.get("arm_group_interventions"),
            source_url=(
                f"{CDE_BASE_URL}{CDE_DETAIL_PATH}"
                f"?id={raw.get('uuid', '')}"
            ),
            raw_payload=raw.get("html"),
        )
