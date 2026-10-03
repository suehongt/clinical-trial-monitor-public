"""
中国临床试验注册中心 (Chinese Clinical Trial Register) ChiCTR Collector.

Platform: ChiCTR (chictr.org.cn) — WHO ICTRP Primary Registry
WAF:      Alibaba Cloud WAF — no public JSON API available
Method:   HTTP 热路径（requests + 离线 acw_sc__v2 求解，waf_http.py），
          新版挑战（aliyun_waf_aa/bb）或 browser_only 配置时降级 Playwright
          （WAFBrowserCollector，fresh context per request）+ BeautifulSoup

URL structure:
  Search (GET): /searchproj.html?page=N&title=<keyword>
  Detail (GET): /showproj.html?proj=<numeric_id>

  The search page returns ChiCTR numbers, titles, and numeric proj IDs.
  Detail pages contain full structured data with Chinese/English labels.

Key fields extracted:
  ChiCTR#, title, scientific_title, registration_status, study_type,
  study_design, conditions, interventions, sponsor, study_leader,
  enrollment, outcomes, eligibility_criteria, locations, registration_date,
  last_updated, ethics_approval, secondary_registration, funding, etc.

Bootstrap: keyword search to discover ChiCTR numbers.
Incremental: 三段式「发现→队列→增强」——discover_new 倒序走广谱词表列表页，
把新源生号写入 discovery_queue 并维护 discovery_cursors 水位线（schema v7）；
enrich_pending 按预算逐条抓详情并复用 parse/upsert 全链路。旧的纯关键词
路径保留为 fetch_new_or_updated_legacy 供回退。
Raw HTML is stored as raw_payload for traceability.

Design refs: the project design notes（传输层三档降级）、
the project design notes §2.2/2.3/2.4,
the project design notes.
"""
from __future__ import annotations

import json
import logging
import threading
import re
import time
from dataclasses import asdict
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from collectors.base import NormalisedRecord, to_json_array
from collectors.browser_base import (WAFBrowserCollector, WafCircuitOpenError,
                                     _STEALTH_UA)
from collectors.waf_http import WafChallengeNewVersion, WafHttpClient
from config import CONFIG
from db.connection import get_connection

logger = logging.getLogger(__name__)

# ── URL templates ───────────────────────────────────────────────────────────

CHICTR_BASE_URL = "https://www.chictr.org.cn"
CHICTR_SEARCH_PATH = "/searchproj.html"
CHICTR_DETAIL_PATH = "/showproj.html"

# ── Field mapping: Chinese label → English field name ─────────────────────

FIELD_LABEL_MAP: dict[str, str] = {
    "注册号：": "source_trial_id",
    "最近更新日期：": "last_updated_at_source",
    "注册时间：": "registration_date",
    "注册号状态：": "registration_status",
    "注册题目：": "title",
    "注册题目简写：": "title_acronym",
    "研究课题的正式科学名称：": "scientific_title",
    "研究课题代号(代码)：": "study_subject_id",
    "在二级注册机构或其它机构的注册号：": "secondary_registration_no",
    "申请注册联系人：": "applicant_contact",
    "研究负责人：": "study_leader",
    "申请注册联系人电话：": "applicant_telephone",
    "研究负责人电话：": "study_leader_telephone",
    "申请注册联系人电子邮件：": "applicant_email",
    "研究负责人电子邮件：": "study_leader_email",
    "申请人所在单位：": "applicant_institution",
    "研究负责人所在单位：": "study_leader_institution",
    "申请注册联系人通讯地址：": "applicant_address",
    "研究负责人通讯地址：": "study_leader_address",
    "是否获伦理委员会批准：": "ethics_approved",
    "伦理委员会批件文号：": "ethics_approval_no",
    "批准本研究的伦理委员会名称：": "ethics_committee_name",
    "伦理委员会批准日期：": "ethics_approval_date",
    "研究实施负责（组长）单位：": "responsible_unit",
    "试验主办单位(项目批准或申办者)：": "sponsor",
    "经费或物资来源：": "funding_source",
    "研究疾病：": "conditions",
    "研究疾病代码：": "disease_code",
    "研究类型：": "study_type",
    "研究所处阶段：": "study_phase",
    "研究设计：": "study_design",
    "研究目的：": "study_purpose",
    "纳入标准：": "inclusion_criteria",
    "排除标准：": "exclusion_criteria",
    "研究实施时间：": "study_execute_time",
    "征募观察对象时间：": "recruitment_time",
    "干预措施：": "interventions",
    "研究实施地点：": "locations",
    "测量指标：": "outcomes",
    "征募研究对象情况：": "recruitment_status",
    "年龄范围：": "age_range",
    "性别：": "gender",
    "随机方法（请说明由何人用什么方法产生随机序列）：": "randomization_method",
    "盲法：": "blinding",
    "是否公开试验完成后的统计结果:": "results_public_access",
    "是否共享原始数据：": "ipd_sharing",
    "共享原始数据的方式（说明：请填入公开原始数据日期和方式，如采用网络平台，需填该网络平台名称和网址）：": "ipd_sharing_method",
    "数据采集和管理（说明：数据采集和管理由两部分组成，一为病例记录表(Case": "data_management",
    "数据与安全监察委员会：": "data_safety_monitoring",
}

# ── Status mapping ──────────────────────────────────────────────────────────

REGISTRATION_STATUS_MAP: dict[str, str] = {
    "预注册": "Prospective registration",
    "补注册": "Retrospective registration",
}

STUDY_TYPE_MAP: dict[str, str] = {
    "干预性研究": "Interventional",
    "观察性研究": "Observational",
    "诊断试验": "Diagnostic",
    "相关因素研究": "Related factor study",
    "流行病学研究": "Epidemiological research",
    "预防性研究": "Preventive study",
    "病因学研究": "Etiological study",
    "预测研究": "Predictive study",
    "卫生经济学研究": "Health economics research",
    "健康服务研究": "Health services research",
    "基础研究": "Basic research",
    "其他": "Other",
    "其它": "Other",
}

STUDY_PHASE_MAP: dict[str, str] = {
    "探索性研究/预试验": "Pilot/Exploratory",
    "I期": "Phase 1",
    "I期/II期": "Phase 1/Phase 2",
    "II期": "Phase 2",
    "II期/III期": "Phase 2/Phase 3",
    "III期": "Phase 3",
    "IV期": "Phase 4",
    "其他": "Other",
    "其它": "Other",
    "不适用": "Not Applicable",
}

RECRUITMENT_STATUS_MAP: dict[str, str] = {
    "尚未开始": "Not yet recruiting",
    "正在进行": "Recruiting",
    "暂停": "Suspended",
    "停止": "Stopped early",
    "完成": "Completed",
    "未知": "Unknown",
}

GENDER_MAP: dict[str, str] = {
    "男性": "Male",
    "女性": "Female",
    "男女均可": "Both",
}

BLINDING_MAP: dict[str, str] = {
    "无": "None",
    "开放标签": "Open label",
    "单盲": "Single blind",
    "双盲": "Double blind",
    "三盲": "Triple blind",
}

# ── Bootstrap keywords ──────────────────────────────────────────────────────

BOOTSTRAP_KEYWORDS = [
    "糖尿病", "高血压", "肺癌", "乳腺癌", "胃癌", "肝癌", "结直肠癌",
    "疫苗", "抗肿瘤", "免疫治疗", "COVID", "新型冠状病毒",
    "干细胞", "基因治疗", "CAR-T", "单抗", "靶向药",
    "心脏病", "心肌梗死", "心梗", "冠心病", "心力衰竭", "心律失常",
    "多发性骨髓瘤", "骨髓瘤",
    "脑卒中", "阿尔茨海默", "慢阻肺",
    "艾滋病", "肝炎", "结核",
    "中药", "针灸", "中西医",
    "诊断", "IVD", "试剂盒",
    "真实世界", "观察性",
    "疼痛", "抑郁", "焦虑", "失眠",
    "儿科", "罕见病",
    "医疗器械", "体外诊断",
    "IIT", "研究者发起",
    "营养", "健康",
]

# ── 三段式发现层（discover → queue → enrich）调参常量 ──────────────────────
#
# 设计依据 the project design notes §2.2/2.3/2.4 与
# the project design notes：
#   · 搜索列表严格按源生号倒序（号序即时间序），列表页无可靠日期列 →
#     水位线用 recent_seen_ids 号集合而非时间戳；
#   · proj_id 近似单调但稀疏（~9:1）、未注册号返回 4097 字节桩页 →
#     号段探测降级为 annual_backfill（id_probe），不进日增量。

# 广谱词表：从 BOOTSTRAP_KEYWORDS 精选覆盖面最大的宽词（单字/短词），
# 用于倒序走「最近注册」列表页（每词命中样本大、词间重叠互补）。
# 任何词表都只能覆盖全站注册量的一个切面——覆盖率缺口由 L0 WHO ICTRP
# 快照 diff 兜底（§2.1，设计既有分工）。
DISCOVERY_WALK_KEYWORDS: list[str] = [
    "心", "癌", "细胞", "治疗", "液", "炎", "瘤", "血管", "糖尿", "肝", "肺", "肾",
]

# recent_seen_ids 水位上限：保留最近 N 个源生号（p1 §4.3）
WATERMARK_MAX_IDS = 5000
# 停止条件（p1 §4.3 简化版）：某词连续 N 页所有号都已在水位集合中 → 换词
STOP_CONSECUTIVE_SEEN_PAGES = 2
# 单词单轮最大翻页数（WAF 预算护栏）
MAX_PAGES_PER_KEYWORD = 10
# ChiCTR 列表页满页行数；不足视为该词末页（与 legacy 行为一致）
LIST_PAGE_SIZE = 10
# proj_id 辅助表（chictr_no → proj_id）上限，供 enrich 免二次检索
PROJ_MAP_MAX_ENTRIES = 5000
# enrich 失败重试上限：attempts 达到即转 failed
ENRICH_MAX_ATTEMPTS = 3
# 桩页判定阈值：未注册号占位页实测 4097 字节，真实详情/列表页均 >5000
STUB_PAGE_MAX_BYTES = 5000

# 年度回补（id_probe）默认号段。Spike S1 锚点（2026-09-08 实测）：
# proj 332062 ↔ ChiCTR2600130816，当时前沿 proj ≈ 342000；
# 按年段外推并留余量。其它年份需调用方显式传 start_proj/end_proj。
ANNUAL_PROJ_SEGMENTS: dict[int, tuple[int, int]] = {
    2026: (280_000, 350_000),
}


def _is_stub_page(html: str) -> bool:
    """判定 ChiCTR 桩页（号段空穴）：页面很短且不含任何源生注册号。

    Spike 证实未注册 proj 号返回 ~4097 字节的站点外壳占位页——它带站点
    标题等 marker、能通过 WAF 内容检查，但没有任何 ChiCTR 号；据此与
    WAF 挑战页（enrich 中表现为抓取失败）区分开，按 skipped 而非 failed 处理。
    """
    return len(html) < STUB_PAGE_MAX_BYTES and not _extract_chictr_numbers(html)


def _extract_site_total(html: str) -> int | None:
    """从搜索结果页提取原站总命中数（#data-total），缺失/异常返回 None。

    供 live_search 零额外请求透传「原站共 N 条」；int() 失败（空串、
    含千分位逗号等）按缺失处理，不让展示性数字影响主流程。
    """
    soup = BeautifulSoup(html, "html.parser")
    total_el = soup.select_one("#data-total")
    if total_el:
        try:
            return int(total_el.get_text(strip=True).replace(",", ""))
        except (ValueError, TypeError):
            pass
    return None


def _cap_map(mapping: dict, cap: int) -> dict:
    """超过上限时丢弃最早的键（dict 保序，先入先丢）。"""
    if len(mapping) <= cap:
        return mapping
    return dict(list(mapping.items())[-cap:])

# ── Content markers for ChiCTR (to distinguish real content from WAF) ─────

CHICTR_CONTENT_MARKERS: list[str] = [
    "ChiCTR",
    "中国临床试验注册中心",
    "注册号",
    "project-tit",
    "left_title",
    "table1",
]


def _is_chictr_content(html: str) -> bool:
    """Check if HTML contains real ChiCTR content (vs WAF challenge shell)."""
    if len(html) < 1000:
        return False
    lower = html.lower()
    count = sum(1 for m in CHICTR_CONTENT_MARKERS if m.lower() in lower)
    return count >= 2


def _is_waf_page(html: str) -> bool:
    """Check if HTML is a WAF challenge page (no real content)."""
    return not _is_chictr_content(html)


# ── Helper functions ────────────────────────────────────────────────────────


def _parse_date(value: str | None) -> str | None:
    """Parse ChiCTR date format to ISO date."""
    if not value:
        return None
    value = value.strip()
    # Already ISO: 2026-07-20
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    # Chinese format: 2026年07月20日
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # Year-month only: 2026年07月
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # Slash format: 2026/07/20
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return value


def _extract_chictr_numbers(text: str) -> list[str]:
    """Extract ChiCTRXXXX... numbers from text."""
    return re.findall(r"ChiCTR[\dA-Za-z]+", text)


def _map_value(value: str, mapping: dict[str, str]) -> str | None:
    """Map a Chinese value to English using the given mapping dict."""
    value = value.strip()
    if not value:
        return None
    mapped = mapping.get(value)
    if mapped:
        return mapped
    # Partial match
    for cn, en in mapping.items():
        if cn in value:
            return en
    return None


def _parse_enrollment_from_interventions(text: str | None) -> int | None:
    """Extract total enrollment from interventions/arm group text."""
    if not text:
        return None
    # Sum all sample sizes found
    samples = re.findall(r"样本量[：:]?\s*(\d+)", text)
    if samples:
        return sum(int(s) for s in samples)
    return None


# ── Outcome parsing helper ──────────────────────────────────────────────────


def _parse_outcomes(outcomes_text: str) -> tuple[str | None, str | None]:
    """Parse primary and secondary endpoints from outcomes text.

    ChiCTR outcomes use the format:
      指标中文名： ... 指标类型： 主要指标 ...
      指标中文名： ... 指标类型： 次要指标 ...
    """
    if not outcomes_text:
        return None, None

    primary_parts: list[str] = []
    secondary_parts: list[str] = []

    # Split by "指标中文名：" to get individual outcomes
    blocks = re.split(r"指标中文名：", outcomes_text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        is_primary = "主要指标" in block
        is_secondary = "次要指标" in block

        # Extract the name (text before 指标类型 or end)
        name_match = re.match(r"([^。]*?)(?:\s*指标类型|$)", block)
        name = name_match.group(1).strip() if name_match else ""

        if name:
            if is_primary:
                primary_parts.append(name)
            elif is_secondary:
                secondary_parts.append(name)

    primary_str = "; ".join(primary_parts) if primary_parts else None
    secondary_str = "; ".join(secondary_parts) if secondary_parts else None

    return primary_str, secondary_str


# ── Main collector class ────────────────────────────────────────────────────


class ChiCTRCollector(WAFBrowserCollector):
    """ChiCTR (Chinese Clinical Trial Register) collector.

    Transport: HTTP 热路径优先（WafHttpClient：requests + 离线 acw_sc__v2
    求解）；遇新版挑战（aliyun_waf_aa/bb）本 run 内自动降级 Playwright。
    Browser notes (fallback path): Alibaba Cloud WAF allows one page load
    per browser context (hence fresh contexts) and its JS polling breaks
    ``networkidle`` (hence ``domcontentloaded`` + settle wait + retry
    backoff).
    """

    wait_until = "domcontentloaded"
    page_timeout_ms = 25000
    settle_ms = 2000
    retry_backoff_ms = 3000
    total_attempts = 2
    block_images = True
    list_page_size = LIST_PAGE_SIZE
    # 最近一次列表页的原站总命中数（_search_results_page 写，live_search 读）
    _last_site_total: int | None = None

    def __init__(self):
        super().__init__("chictr")
        self._page_cache: dict[str, str] = {}
        # HTTP 热路径（waf-bypass-upgrade-plan §2.2）：auto 模式下默认走
        # requests + 离线求解；browser_only 或运行中降级后停用。
        # 多会话并行（enrich workers）：客户端线程级实例化——每 worker
        # 独立 cookie 会话（阿里 WAF 封禁是会话级的，多会话=独立限速桶，
        # 社区标准做法），熔断器共享（已加锁）。
        self._http_enabled = CONFIG.waf.challenge_mode != "browser_only"
        self._http_tls = threading.local()
        # live_search 检索字段选择（线程级：enrich workers 共享采集器实例）
        self._live_tls = threading.local()

    def _is_waf_page(self, html: str) -> bool:
        """Detect WAF challenge pages (module-level content check)."""
        return _is_waf_page(html)

    def _get_http(self) -> WafHttpClient:
        """当前线程的 WafHttpClient（每线程独立 cookie 会话，共享守卫）。"""
        client = getattr(self._http_tls, "client", None)
        if client is None:
            client = WafHttpClient(
                "chictr",
                content_ok=_is_chictr_content,
                user_agent=_STEALTH_UA,
                timeout_ms=self.page_timeout_ms,
                guard=self._guard,
            )
            self._http_tls.client = client
        return client

    def _fetch_page(self, url: str,
                    timeout_ms: Optional[int] = None) -> Optional[str]:
        """HTTP 热路径优先，按需降级浏览器（waf-bypass-upgrade-plan §2.3）。

        - auto + HTTP 成功 → 直接返回（零浏览器）；
        - 新版挑战 → 抛 WafChallengeNewVersion → 记日志、弃用 HTTP，
          本次 run 内改走 Playwright 父实现；
        - WafCircuitOpenError（共享熔断器）原样穿透；
        - HTTP 尝试耗尽（None）按契约返回 None，不再叠加浏览器重试
          （档3 = 熔断即停，避免同 IP 双通道加压）。
        """
        if self._http_enabled:
            try:
                return self._get_http().get(url)
            except WafChallengeNewVersion as exc:
                logger.warning("ChiCTR %s；本次 run 降级 Playwright 路径", exc)
                self._http_enabled = False
        return super()._fetch_page(url, timeout_ms)

    # ── Page fetching via Playwright (fresh context per request) ──────────

    def _fetch(self, url: str, timeout_ms: int = 25000) -> str:
        """Fetch a page, caching successful results for the current run.

        Raises RuntimeError when every attempt ends in a WAF challenge.
        """
        if url in self._page_cache:
            return self._page_cache[url]

        html = self._fetch_page(url, timeout_ms=timeout_ms)
        if html is None:
            raise RuntimeError("WAF blocked the request after "
                               f"{self.total_attempts} attempts")
        if html and len(html) >= 500:
            self._page_cache[url] = html
        return html

    def _clear_cache(self):
        self._page_cache.clear()

    # ── Search ──────────────────────────────────────────────────────────

    def _search_results_page(self, keyword: str,
                             page: int = 1,
                             field: Optional[str] = None) -> list[dict[str, str]]:
        """Search ChiCTR by keyword and return trial entries from one page.

        field=None（默认）取当前线程的 live_search 字段选择（恒为
        "title"，除非正处于 live_search 调用栈内）；显式传参可强制。
        "title"：注册题目字段（搜索框原生行为）；
        "secsponsor"：试验主办单位（=申办者）字段——「更多筛选」里的
        隐藏检索项（searchproj.js 证实 URL 参数；注意 sponsor= 是研究
        实施负责(组长)单位，另一个概念，勿混用）。2026-09-29 实测
        secsponsor=罗氏 → 0 命中：行业试验登记在 CTR 平台，ChiCTR 以
        研究者发起为主，这是诚实 0 而非检索失效。
        """
        if field is None:
            field = getattr(self._live_tls, "field", "title")
        params = f"page={page}&{field}={quote(keyword)}&btngo=btn"
        html = self._fetch(f"{CHICTR_BASE_URL}{CHICTR_SEARCH_PATH}?{params}")
        # 顺带记录原站总命中数（#data-total）：live_search 零额外请求透传，
        # 面板可如实显示「原站共 N 条」（本页只取前 10 条/页 × 翻页数）。
        self._last_site_total = _extract_site_total(html)
        return self._parse_search_rows(html)

    def live_search(self, keyword: str, max_pages: int = 1,
                    start_page: int = 1, field: Optional[str] = None) -> dict:
        """实时检索按词义选字段：申办者词走 secsponsor，其余走注册题目。

        申办者信号 = 公司/机构名特征词（「上海罗氏制药有限公司」
        「Roche Diagnostics GmbH」）或本地库 sponsors 已含该词（简称如
        「罗氏」无特征词，靠本地 CTR 记录的申办者名识别）。此前恒走
        title，把标题含「罗氏」的无关词（罗氏菌/皮罗氏序列征）当申办者
        结果返回，是 2026-09-29 用户案例的根源。``field`` 供服务端续批
        沿用首查生效字段。api_search/发现走查等非 live_search 调用维持
        title 不变。
        """
        if field not in ("title", "secsponsor"):
            from core import terminology as _term

            kw = (keyword or "").strip()
            # get_connection 是线程级缓存连接，不能 close
            sponsor = (_term.has_sponsor_marker(kw)
                       or _term.sponsors_in_library(kw, get_connection()))
            field = "secsponsor" if sponsor else "title"
        self._live_tls.field = field
        try:
            stats = super().live_search(keyword, max_pages=max_pages,
                                        start_page=start_page)
        finally:
            self._live_tls.field = "title"
        stats["field_used"] = field
        return stats

    def _lookup_by_regno(self, regno: str) -> list[dict[str, str]]:
        """按完整注册号精确检索（searchproj 的 regno 参数，见 searchproj.js）。

        title 搜索不匹配注册号；注册号必须走 regno 参数。供 enrich 侧
        反查详情键（ictrp_diff 入队行只有注册号没有 proj_id）。
        """
        params = f"page=1&regno={quote(regno)}&btngo=btn"
        html = self._fetch(f"{CHICTR_BASE_URL}{CHICTR_SEARCH_PATH}?{params}")
        return self._parse_search_rows(html)

    @staticmethod
    def _parse_search_rows(html: str) -> list[dict[str, str]]:
        """解析搜索结果页的行：注册号 / 标题 / 详情键 proj_id。

        Structure-robust: matches rows under `.table1` whether or not the
        served HTML contains an explicit <tbody> (html.parser does not insert
        one; the 2026-09 site redesign removed it), and locates the ChiCTR
        number / title link by content rather than fixed column positions.
        """
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select(".table1 tr")

        results: list[dict[str, str]] = []
        for row in rows:
            cells = row.select("td")
            if len(cells) < 3:
                continue  # header / decoration row
            chictr_td = next(
                (td for td in cells
                 if td.get_text(strip=True).startswith("ChiCTR")), None)
            if not chictr_td:
                continue
            chictr_no = chictr_td.get_text(strip=True)

            link = row.select_one("a[href*='showproj']")
            title = (link.get_text(strip=True) if link else "")
            proj_href = link.get("href", "") if link else ""
            proj_id = proj_href.split("=")[-1] if "=" in proj_href else ""

            results.append({
                "chictr_no": chictr_no,
                "title": title,
                "proj_id": proj_id,
            })

        return results

    def _search_total_count(self, keyword: str) -> int:
        """Get total number of search results for a keyword."""
        url = (
            f"{CHICTR_BASE_URL}{CHICTR_SEARCH_PATH}"
            f"?page=1&title={quote(keyword)}&btngo=btn"
        )
        html = self._fetch(url)
        return _extract_site_total(html) or 0

    # ── Local API adapter (read-only; no queue/database writes) ─────────

    def api_search(self, query: str, page: int = 1) -> dict[str, Any]:
        """Return one ChiCTR search page in a stable JSON-ready contract.

        Unlike :meth:`live_search`, this adapter deliberately does not enqueue
        hits or move discovery cursors.  It is the read-only building block for
        the local ``/api/chictr/search`` endpoint.
        """
        query = str(query or "").strip()
        if not query:
            raise ValueError("query must not be blank")
        page = int(page)
        if page < 1:
            raise ValueError("page must be at least 1")

        entries = self._search_results_page(query, page)
        items = []
        for entry in entries:
            trial_id = entry.get("chictr_no", "")
            proj_id = entry.get("proj_id", "")
            if not trial_id:
                continue
            items.append({
                "source_trial_id": trial_id,
                "title": entry.get("title", ""),
                "proj_id": proj_id or None,
                "url": (
                    f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"
                    if proj_id else None
                ),
            })
        site_total = self._last_site_total
        return {
            "source": "ChiCTR",
            "query": query,
            "page": page,
            "page_size": LIST_PAGE_SIZE,
            "site_total": site_total,
            "has_more": (
                page * LIST_PAGE_SIZE < site_total
                if site_total is not None
                else len(items) >= LIST_PAGE_SIZE
            ),
            "items": items,
        }

    def api_study(self, chictr_no: str) -> Optional[dict[str, Any]]:
        """Fetch one ChiCTR record by registration number for the local API.

        The exact-registration-number search resolves ChiCTR's internal
        ``proj`` key, then the existing detail parser and normaliser produce
        both lossless extracted fields and the cross-registry canonical view.
        No discovery queue, cursor, or registry record is changed.
        """
        trial_id = str(chictr_no or "").strip()
        if not re.fullmatch(r"ChiCTR\d{10,}", trial_id, flags=re.IGNORECASE):
            raise ValueError(f"not a ChiCTR registration number: {trial_id}")
        trial_id = "ChiCTR" + trial_id[6:]

        entries = self._lookup_by_regno(trial_id)
        hit = next(
            (entry for entry in entries
             if entry.get("chictr_no", "").lower() == trial_id.lower()
             and entry.get("proj_id")),
            None,
        )
        if hit is None:
            return None

        proj_id = hit["proj_id"]
        url = f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"
        # Exact-ID resolution and detail retrieval are two upstream pages.
        # Keep the collector's configured politeness gap between them.
        if self.cfg.request_delay_sec:
            time.sleep(self.cfg.request_delay_sec)
        html = self._fetch(url)
        if _is_stub_page(html):
            return None

        fields = self.parse_detail_page(html, trial_id)
        canonical = asdict(self.normalise({
            "chictr_no": trial_id,
            "proj_id": proj_id,
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

        return {
            "source": "ChiCTR",
            "source_trial_id": trial_id,
            "proj_id": proj_id,
            "url": url,
            "fields": fields,
            "canonical": canonical,
        }

    # ── 三段式发现层：discover → queue → enrich（schema v7）────────────
    # 表结构见 db/schema.py v7：discovery_cursors（水位线）、discovery_queue
    # （发现/增强解耦的工作队列）。设计依据 §2.2/2.3/2.4 与 Spike 结论。

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
        精准优先，不必盲增强（myocarditis 案例后的流程优化）。已存在的行
        （如 ictrp_diff 先入队、无标题）用列表页标题回填一次。
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

    def _remember_proj_id(self, chictr_no: str, proj_id: str) -> None:
        """把外源发现（ictrp_diff/live 回查）解析出的 proj_id 记入辅助表。

        success=False → last_successful 不因辅助写入而前移（与
        _forget_proj_id 同语义）；体积由 PROJ_MAP_MAX_ENTRIES 封顶。
        """
        cursor = self._load_cursor("list_walk")
        proj_map = cursor.get("proj_ids", {})
        proj_map[chictr_no] = str(proj_id)
        self._save_cursor(
            "list_walk",
            {"recent_seen_ids": cursor.get("recent_seen_ids", []),
             "proj_ids": _cap_map(proj_map, PROJ_MAP_MAX_ENTRIES)},
            success=False,
        )

    def _resolve_proj_id(self, chictr_no: str) -> Optional[str]:
        """解析详情键 proj_id：辅助表 → 历史 source_url → 按号检索兜底。

        discovery_queue 只存源生号；proj_id 由 list_walk 发现时随手记入
        cursor_json.proj_ids（辅助表、非水位，失败轮也保留），离线缺失时
        回查本源已有记录的 source_url（showproj.html?proj=N）。
        前两级都查不到（如 ictrp_diff 入队的、从未被关键词走查见过的号）
        时，用注册号走 regno 精确检索（1 个列表页预算）反查详情键，并写
        回辅助表使后续重试免于重复检索；检索不到才返回 None。
        """
        cursor = self._load_cursor("list_walk")
        pid = cursor.get("proj_ids", {}).get(chictr_no)
        if pid:
            return str(pid)
        conn = get_connection()
        row = conn.execute(
            "SELECT source_url FROM registry_records "
            "WHERE source_id = ? AND source_trial_id = ? AND is_latest = 1",
            (self.source_id, chictr_no),
        ).fetchone()
        if row and row["source_url"] and "proj=" in row["source_url"]:
            return row["source_url"].rsplit("proj=", 1)[-1] or None
        try:
            entries = self._lookup_by_regno(chictr_no)
        except Exception as exc:
            logger.warning("ChiCTR 按号反查 proj_id 失败 %s: %s",
                           chictr_no, exc)
            return None
        hit = next((e for e in entries
                    if e.get("chictr_no") == chictr_no and e.get("proj_id")),
                   None)
        if hit:
            self._remember_proj_id(chictr_no, hit["proj_id"])
            return hit["proj_id"]
        return None

    def _forget_proj_id(self, chictr_no: str) -> None:
        """从游标辅助表移除已消费的 proj_id 映射（控制 cursor_json 体积）。

        success=False → last_successful 不因清理动作而前移。
        """
        cursor = self._load_cursor("list_walk")
        proj_map = cursor.get("proj_ids", {})
        if chictr_no in proj_map:
            proj_map.pop(chictr_no)
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": cursor.get("recent_seen_ids", []),
                 "proj_ids": _cap_map(proj_map, PROJ_MAP_MAX_ENTRIES)},
                success=False,
            )

    # ── 实时检索钩子（live_search，见 browser_base） ────────────────────

    def _entry_identity(self, entry: dict) -> tuple[Optional[str], str]:
        return entry.get("chictr_no") or None, entry.get("title", "")

    def _entry_url(self, entry: dict) -> Optional[str]:
        proj_id = entry.get("proj_id")
        if not proj_id:
            return None
        return f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"

    def _absorb_live_entries(self, entries: list) -> int:
        """live_search 吸收：入队 + proj_id 辅助表回填（水位线不动）。

        live_search 拿到的 proj_id 是 enrich 侧解析详情键的唯一即时来源
        （离线回查要等 source_url 落库），所以辅助表必须在入队同一轮写入；
        success=False 保证不前移 list_walk 水位。
        """
        cursor = self._load_cursor("list_walk")
        # 浅拷贝隔离：cursor["proj_ids"] 原地变更会让「有无新增」的比较恒为
        # False（同一对象），辅助表永远不会落盘——必须拷贝后再改。
        proj_map = dict(cursor.get("proj_ids", {}))
        queued = 0
        for entry in entries:
            no = entry.get("chictr_no", "")
            if not no:
                continue
            if entry.get("proj_id"):
                proj_map[no] = entry["proj_id"]
            if self._enqueue(no, "live_search", title=entry.get("title")):
                queued += 1
        if proj_map != cursor.get("proj_ids", {}):
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": cursor.get("recent_seen_ids", []),
                 "proj_ids": _cap_map(proj_map, PROJ_MAP_MAX_ENTRIES)},
                success=False,
            )
        return queued

    def discover_new(self, since: Optional[str] = None) -> dict:
        """阶段 1（发现）：广谱词表倒序走「最近注册」列表页，新号入队。

        水位线 = discovery_cursors(mode='list_walk') 的 recent_seen_ids
        （保留最近 WATERMARK_MAX_IDS 个源生号；Spike 证实号序即时间序）。

        停止条件（p1 §4.3 简化版）：
          - 某词连续 STOP_CONSECUTIVE_SEEN_PAGES 页所有号都已在已见集合
            （水位 ∪ 本轮已见）中 → 换下一个词；
          - 短页（< LIST_PAGE_SIZE 行）视为该词末页；
          - 任一页失败 → 本轮终止，游标水位不前移，已见号照常入队
            （INSERT OR IGNORE 保证重复入队幂等）。

        返回 {queued, seen, pages_walked, stopped_reason}；
        stopped_reason ∈ {'completed', 'page_failed'}。since 仅记录日志
        （远端列表无 since 过滤，增量由水位线承担）。
        """
        if since:
            logger.info("ChiCTR discover_new (since %s，仅记录)", since)

        cursor = self._load_cursor("list_walk")
        recent_seen = [str(x) for x in cursor.get("recent_seen_ids", [])]
        watermark_set = set(recent_seen)
        proj_map = dict(cursor.get("proj_ids", {}))

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
                    logger.warning("ChiCTR 列表页失败 '%s' p%d: %s",
                                   keyword, page, exc)
                    stopped_reason = "page_failed"
                    break

                pages_walked += 1
                if not entries:
                    break  # 空页 → 该词已到末尾

                # 停止条件判据 = 水位 ∪ 本轮已见（跨词重复的号也算已见）
                known_set = watermark_set | round_ids_set
                all_seen = all(
                    e.get("chictr_no") in known_set for e in entries
                )

                for entry in entries:
                    no = entry.get("chictr_no", "")
                    if not no or no in round_ids_set:
                        continue
                    round_ids_set.add(no)
                    round_ids.append(no)
                    proj_id = entry.get("proj_id") or None
                    if proj_id:
                        proj_map[no] = proj_id
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
        # - proj_ids 辅助表随入队推进（非水位，失败轮也保留，否则失败前
        #   入队的号在 enrich 时无法解析详情键）。
        if stopped_reason != "page_failed":
            merged = round_ids + [r for r in recent_seen
                                  if r not in round_ids_set]
            new_cursor = {
                "recent_seen_ids": merged[:WATERMARK_MAX_IDS],
                "proj_ids": _cap_map(proj_map, PROJ_MAP_MAX_ENTRIES),
            }
            self._save_cursor("list_walk", new_cursor, success=True)
        else:
            self._save_cursor(
                "list_walk",
                {"recent_seen_ids": recent_seen,
                 "proj_ids": _cap_map(proj_map, PROJ_MAP_MAX_ENTRIES)},
                success=False,
            )

        stats = {
            "queued": queued,
            "seen": len(round_ids),
            "pages_walked": pages_walked,
            "stopped_reason": stopped_reason,
        }
        logger.info("ChiCTR discover_new: %s", stats)
        return stats

    def enrich_pending(self, limit: Optional[int] = None,
                       keywords: Optional[list[str]] = None,
                       source_trial_ids: Optional[list[str]] = None,
                       workers: int = 1) -> dict:
        """阶段 2（增强）：按预算消化 discovery_queue 本源 pending 行。

        keywords 给定时走「精准优先」：先增强标题命中任一关键词的行
        （大小写不敏感），预算未用满再用不命中的行回填——疾病验证/定向
        采集时把 WAF 预算花在相关试验上，而不是最新号段的盲增强。
        否则 ORDER BY discovery_id（先进先增强）；limit 默认取
        cfg.max_records_per_run（0 → 50）。逐条抓详情页并复用
        parse_detail_page → normalise → BaseCollector._upsert_record 全链路：
          - 成功 → state=enriched（同时清理 proj_id 辅助表条目）；
          - 失败 → attempts+1、last_error 记录；attempts ≥ ENRICH_MAX_ATTEMPTS
            转 failed；
          - 4097 字节桩页（号段空穴，非错误）→ state=skipped。

        返回 {enriched, failed, skipped, records[, keyword_matched]}；
        records 为本轮增强产出的原始记录列表（与 fetch_new_or_updated 旧
        返回结构同构），供兼容包装与上层统计使用。
        """
        if limit is None:
            # enrich 预算优先取专用键（G2：单晚 ≤50 详情请求），
            # 缺省回落 max_records_per_run / 50
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
            logger.info("ChiCTR enrich 关键词优先：%d/%d 条标题命中 %s",
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
            # 多会话并行（cookie 池）：每 worker 独立 HTTP 会话、独立
            # 2.5s 控速——单会话频率画像不变，吞吐 ×workers。熔断共享：
            # 任一 worker 触发后，其余 worker 下一次抓取前快速失败，
            # 主循环收到哨兵即取消未起跑的任务并中止本轮。
            from concurrent.futures import ThreadPoolExecutor
            stats["workers"] = min(workers, len(rows))
            with ThreadPoolExecutor(
                    max_workers=stats["workers"]) as pool:
                try:
                    for result in pool.map(self._enrich_row, rows):
                        if result is None:
                            pool.shutdown(cancel_futures=True)
                            logger.error("ChiCTR enrich 熔断中止，"
                                         "剩余 pending 留待下轮")
                            break
                        consume(result)
                except WafCircuitOpenError:
                    pool.shutdown(cancel_futures=True)
                    logger.error("ChiCTR enrich 熔断中止，剩余 pending 留待下轮")
        else:
            for row in rows:
                result = self._enrich_row(row)
                if result is None:
                    logger.error("ChiCTR enrich 熔断中止，剩余 pending 留待下轮")
                    break
                consume(result)

        logger.info("ChiCTR enrich_pending: enriched=%d failed=%d skipped=%d"
                    "%s", stats["enriched"], stats["failed"], stats["skipped"],
                    f" (workers={stats['workers']})" if "workers" in stats else "")
        return stats

    def _enrich_row(self, row) -> Optional[dict]:
        """enrich_pending 的单行体（多 worker 时在线程内执行）。

        返回 {"outcome": "enriched"|"failed"|"skipped", "raw": …, "changes": n}
        或 None（熔断中止哨兵——共享熔断器已打开，本轮应整体停止）。
        """
        discovery_id = row["discovery_id"]
        chictr_no = row["source_trial_id"]
        time.sleep(self.cfg.request_delay_sec)
        try:
            if chictr_no.isdigit():
                # annual_backfill（id_probe）占位行：纯数字 proj 串
                # 本身就是详情键；解析出的真实注册号用于入库主键。
                proj_id = chictr_no
            else:
                proj_id = self._resolve_proj_id(chictr_no)
            if not proj_id:
                raise RuntimeError(f"无法解析详情键 proj_id: {chictr_no}")

            detail_url = (
                f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"
            )
            html = self._fetch(detail_url)
            if _is_stub_page(html):
                # 号段空穴：未注册号占位页，非错误 → skipped
                logger.info("ChiCTR enrich 桩页跳过 %s (proj=%s)",
                            chictr_no, proj_id)
                self._mark_queue(discovery_id, "skipped")
                return {"outcome": "skipped", "raw": None, "changes": 0}

            parsed = self.parse_detail_page(html, chictr_no)
            raw = {
                "chictr_no": parsed.get("source_trial_id") or chictr_no,
                "proj_id": proj_id,
                "html": html,
                "parsed_fields": parsed,
            }
            outcome = self._upsert_and_emit_events(self.normalise(raw))
            self._mark_queue(discovery_id, "enriched")
            self._forget_proj_id(chictr_no)
            logger.info("ChiCTR enrich 完成 %s", chictr_no)
            return {"outcome": "enriched", "raw": raw,
                    "changes": outcome["changes"]}
        except WafCircuitOpenError:
            # 熔断哨兵：不计失败、不动 attempts（行仍为 pending）
            return None
        except Exception as exc:
            logger.warning("ChiCTR enrich 失败 %s: %s", chictr_no, exc)
            self._mark_queue_failed_attempt(discovery_id, str(exc))
            return {"outcome": "failed", "raw": None, "changes": 0}

    def refresh_one(self, chictr_no: str) -> dict:
        """复查钩子：重访一条已入库记录的详情页（core.refresh 驱动）。

        详情键解析与 enrich 同源（游标辅助表 → 历史 source_url 回退），
        upsert hash-skip 保证未变记录零版本噪声；变化时补写 trial_events。
        返回 {"action": "new"|"updated"|"skipped", "changes": int[, "stub"]}。
        """
        proj_id = self._resolve_proj_id(chictr_no)
        if not proj_id:
            raise RuntimeError(f"无法解析详情键 proj_id: {chictr_no}")

        detail_url = (
            f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"
        )
        html = self._fetch(detail_url)
        if _is_stub_page(html):
            return {"action": "skipped", "changes": 0, "stub": True}

        parsed = self.parse_detail_page(html, chictr_no)
        raw = {
            "chictr_no": parsed.get("source_trial_id") or chictr_no,
            "proj_id": proj_id,
            "html": html,
            "parsed_fields": parsed,
        }
        return self._upsert_and_emit_events(self.normalise(raw))

    def annual_backfill(self, year: int, start_proj: Optional[int] = None,
                        end_proj: Optional[int] = None,
                        batch: int = 500) -> dict:
        """ChiCTR 年度完整性回补（id_probe）：按 proj 号段生成候选入队。

        Spike S1 证实 proj_id 与注册顺序近似单调但稀疏（~9:1，未注册号
        返回 4097 字节桩页），号段探测不适合日发现、只适合年度回补：
        本方法只把号段清单写入 discovery_queue（discovered_via='id_probe'、
        INSERT OR IGNORE 幂等），不抓任何页面；由 enrich_pending 分批消化
        （桩页自动 skipped）。号段默认值见 ANNUAL_PROJ_SEGMENTS；游标
        mode='id_probe' 记录 last_complete_proj，跨轮续跑不重复入队。

        返回 {queued, year, from_proj, to_proj, done}。
        """
        if start_proj is None or end_proj is None:
            segment = ANNUAL_PROJ_SEGMENTS.get(int(year))
            if segment is None:
                raise ValueError(
                    f"未知年份 {year} 的 proj 号段，"
                    "请显式传入 start_proj / end_proj"
                )
            start_proj = segment[0] if start_proj is None else start_proj
            end_proj = segment[1] if end_proj is None else end_proj
        start_proj, end_proj = int(start_proj), int(end_proj)
        if end_proj < start_proj:
            raise ValueError("end_proj 必须不小于 start_proj")

        cursor = self._load_cursor("id_probe")
        last_done = int(cursor.get("last_complete_proj") or 0)
        from_proj = max(start_proj, last_done + 1)
        to_proj = min(end_proj, from_proj + max(1, int(batch)) - 1)

        queued = 0
        for proj in range(from_proj, to_proj + 1):
            # 占位源生号用纯数字 proj 串：与真实 ChiCTR 号（ChiCTR 前缀）
            # 天然不冲突；enrich 时解析出的真实注册号作为入库主键。
            if self._enqueue(str(proj), "id_probe"):
                queued += 1

        self._save_cursor(
            "id_probe",
            {"year": int(year), "last_complete_proj": to_proj},
            success=True,
        )
        stats = {
            "queued": queued,
            "year": int(year),
            "from_proj": from_proj,
            "to_proj": to_proj,
            "done": to_proj >= end_proj,
        }
        logger.info("ChiCTR annual_backfill(%d): %s", year, stats)
        return stats

    # ── Detail page parsing ─────────────────────────────────────────────

    @staticmethod
    def parse_detail_page(html: str, chictr_no: str) -> dict[str, Any]:
        """Parse ChiCTR detail page HTML into structured fields.

        ChiCTR pages have:
          - Title in <div class="project-tit">
          - Fields in <table> rows: <td class="left_title"> (label) + <td> (value)
          - Chinese rows (<tr class="cn">) alternate with English rows
          - Multi-column rows: two label-value pairs per <tr>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields: dict[str, Any] = {
            "source_trial_id": chictr_no,
            "_raw_html_length": len(html),
        }

        # ── Title ───────────────────────────────────────────────────────
        tit_el = soup.select_one(".project-tit")
        if tit_el:
            tit_text = tit_el.get_text(strip=True)
            fields["title"] = tit_text

        # ── Scan tables for label-value pairs ───────────────────────────
        for table in soup.select("div.project-ms table"):
            for row in table.select("tr"):
                left_titles = row.select("td.left_title")
                # Get all <td> that are NOT left_title
                value_cells = [
                    td for td in row.find_all("td")
                    if "left_title" not in (td.get("class", []))
                ]

                for lt, vt in zip(left_titles, value_cells):
                    cn_label = lt.find("p", class_="cn")
                    if not cn_label:
                        continue
                    label = cn_label.get_text(strip=True)
                    if not label:
                        continue

                    eng_key = FIELD_LABEL_MAP.get(label)
                    if not eng_key:
                        continue

                    value = vt.get_text(strip=True, separator=" ")
                    if value:
                        existing = fields.get(eng_key)
                        if existing is None or len(value) > len(str(existing)):
                            fields[eng_key] = value

        # ── Post-process ────────────────────────────────────────────────

        # Parse dates
        for date_field in ("registration_date", "ethics_approval_date",
                           "last_updated_at_source"):
            if date_field in fields:
                fields[date_field] = _parse_date(fields[date_field])

        # Map registration status
        status_raw = fields.get("registration_status", "")
        if status_raw:
            mapped = REGISTRATION_STATUS_MAP.get(status_raw)
            fields["registration_status"] = mapped or status_raw

        # Map study_type
        st_raw = fields.get("study_type", "")
        if st_raw:
            mapped = _map_value(st_raw, STUDY_TYPE_MAP)
            fields["study_type"] = mapped or st_raw

        # Map study_phase
        phase_raw = fields.get("study_phase", "")
        if phase_raw:
            mapped = _map_value(phase_raw, STUDY_PHASE_MAP)
            fields["study_phase"] = mapped or phase_raw

        # Map recruitment_status
        rs_raw = fields.get("recruitment_status", "")
        if rs_raw:
            mapped = _map_value(rs_raw, RECRUITMENT_STATUS_MAP)
            fields["recruitment_status"] = mapped or rs_raw

        # Map gender
        gender_raw = fields.get("gender", "")
        if gender_raw:
            mapped = GENDER_MAP.get(gender_raw)
            fields["gender"] = mapped or gender_raw

        # Map blinding
        blinding_raw = fields.get("blinding", "")
        if blinding_raw:
            mapped = _map_value(blinding_raw, BLINDING_MAP)
            fields["blinding"] = mapped or blinding_raw

        # Combine inclusion + exclusion → eligibility_criteria
        inclusion = fields.pop("inclusion_criteria", "")
        exclusion = fields.pop("exclusion_criteria", "")
        parts = []
        if inclusion:
            parts.append(f"Inclusion: {inclusion[:2000]}")
        if exclusion:
            parts.append(f"Exclusion: {exclusion[:2000]}")
        if parts:
            fields["eligibility_criteria"] = "\n".join(parts)

        # Extract enrollment from interventions text
        interventions = fields.get("interventions", "")
        if interventions:
            enrollment = _parse_enrollment_from_interventions(interventions)
            if enrollment is not None:
                fields["enrollment"] = enrollment

        # Extract primary/secondary endpoints from outcomes
        outcomes = fields.get("outcomes", "")
        if outcomes:
            primary, secondary = _parse_outcomes(outcomes)
            if primary:
                fields["primary_endpoint"] = primary
            if secondary:
                fields["secondary_endpoints"] = secondary

        return fields

    # ── BaseCollector interface ─────────────────────────────────────────

    def fetch_new_or_updated(
        self, since: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Fetch trial records from ChiCTR（三段式兼容包装）。

        内部 = discover_new（列表倒序发现 + 水位线入队）→ enrich_pending
        （按预算抓详情 + upsert）。对外行为与旧实现兼容：返回本轮增强
        产出的 raw dict 列表（chictr_no / proj_id / html / parsed_fields）。
        纯关键词旧路径保留在 fetch_new_or_updated_legacy，供回退。
        """
        if getattr(self, "use_legacy_discovery", False):
            return self.fetch_new_or_updated_legacy(since)
        discovery = self.discover_new(since)
        enrich = self.enrich_pending()
        records: list[dict[str, Any]] = enrich.get("records", [])
        logger.info(
            "ChiCTR 三段式完成：discover=%s enrich(enriched=%d failed=%d "
            "skipped=%d)", discovery, enrich["enriched"], enrich["failed"],
            enrich["skipped"],
        )
        self._clear_cache()
        return records

    def fetch_new_or_updated_legacy(
        self, since: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Legacy 关键词发现路径（回退用，行为与历史版本一致）。

        Bootstrap mode (since=None): search by keywords,
        collect unique trial entries, fetch each detail page.

        Incremental mode (since is set): keyword search to find new trials;
        BaseCollector upsert handles dedup.

        Returns list of raw dicts with keys: chictr_no, html, parsed_fields.
        """
        if since:
            logger.info("ChiCTR incremental mode (since %s)", since)

        # Phase 1: Collect trial entries across all keywords
        all_trials: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        keywords = BOOTSTRAP_KEYWORDS

        for idx, keyword in enumerate(keywords):
            logger.info("Keyword search %d/%d: '%s'",
                        idx + 1, len(keywords), keyword)
            try:
                total = self._search_total_count(keyword)
                if total == 0:
                    continue
                logger.info("  Total results: %d", total)

                # Pages per keyword: configurable for full coverage
                # (extra["search_max_pages"], default 5 ≈ first 50 results)
                max_pages = min(
                    (total + 9) // 10,
                    (self.cfg.extra or {}).get("search_max_pages", 5),
                )
                for page in range(1, max_pages + 1):
                    try:
                        trials = self._search_results_page(keyword, page)
                        new_count = 0
                        for t in trials:
                            if t["chictr_no"] not in seen_ids:
                                seen_ids.add(t["chictr_no"])
                                all_trials.append(t)
                                new_count += 1
                        logger.info(
                            "  Page %d/%d: %d trials (%d new)",
                            page, max_pages, len(trials), new_count,
                        )
                        if len(trials) < 10:
                            break
                    except Exception as exc:
                        logger.warning("  Page %d failed: %s", page, exc)
                        continue

            except Exception as exc:
                logger.warning("  Keyword '%s' search failed: %s",
                               keyword, exc)
                continue

        logger.info("Total unique trials found: %d", len(all_trials))

        # Phase 2: Fetch detail pages.
        # max_records_per_run == 0 means "no cap this run".
        # Trials already stored (is_latest) are skipped without fetching so
        # repeated batch runs advance coverage instead of refetching — this
        # keeps WAF exposure proportional to the remaining backlog.  A run
        # aborts after 3 consecutive fetch failures (WAF circuit breaker).
        from db.connection import get_connection
        existing = {r[0] for r in get_connection().execute(
            "SELECT source_trial_id FROM registry_records "
            "WHERE source_id=? AND is_latest=1",
            (self.source_id,))}

        max_records = self.cfg.max_records_per_run or len(all_trials)
        raw_records: list[dict[str, Any]] = []
        fetched = consecutive_failures = 0

        for trial in all_trials:
            if fetched >= max_records:
                logger.info("Reached max_records_per_run (%d), stopping",
                            max_records)
                break
            if consecutive_failures >= 3:
                logger.warning("WAF circuit breaker: 3 consecutive fetch "
                               "failures — stopping this batch")
                break

            chictr_no = trial["chictr_no"]
            proj_id = trial["proj_id"]
            if not proj_id:
                logger.warning("  No proj_id for %s, skipping", chictr_no)
                continue
            if chictr_no in existing:
                logger.info("  %s already stored, skipping", chictr_no)
                continue

            try:
                detail_url = (
                    f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}?proj={proj_id}"
                )
                html = self._fetch(detail_url)
                parsed = self.parse_detail_page(html, chictr_no)

                raw_records.append({
                    "chictr_no": chictr_no,
                    "proj_id": proj_id,
                    "html": html,
                    "parsed_fields": parsed,
                })
                fetched += 1
                consecutive_failures = 0

                logger.info("  [%d/%d] %s: %s",
                            fetched, max_records,
                            chictr_no, parsed.get("title", "")[:50])

                time.sleep(self.cfg.request_delay_sec)

            except Exception as exc:
                logger.error("  Error fetching %s: %s", chictr_no, exc)
                consecutive_failures += 1
                continue

        self._clear_cache()
        return raw_records

    # ── Normalise ───────────────────────────────────────────────────────

    def normalise(self, raw: dict[str, Any]) -> NormalisedRecord:
        """Convert a raw ChiCTR dict into a NormalisedRecord."""
        pf = raw.get("parsed_fields", {})

        status = pf.get("registration_status")
        recruitment_status = pf.get("recruitment_status")
        study_type = pf.get("study_type")

        return NormalisedRecord(
            source_trial_id=pf.get(
                "source_trial_id", raw.get("chictr_no", ""),
            ),
            title=pf.get("title", ""),
            scientific_title=pf.get("scientific_title"),
            study_type=study_type,
            status=status or recruitment_status,
            enrollment=pf.get("enrollment"),
            registration_date=pf.get("registration_date"),
            last_updated_at_source=pf.get("last_updated_at_source"),
            conditions=to_json_array(pf.get("conditions")),
            interventions=to_json_array(pf.get("interventions")),
            countries=json.dumps(["中国"], ensure_ascii=False),
            locations=to_json_array(pf.get("locations")),
            sponsors=to_json_array(pf.get("sponsor")),
            study_phase=pf.get("study_phase"),
            study_design=pf.get("study_design"),
            eligibility_criteria=pf.get("eligibility_criteria"),
            primary_endpoint=pf.get("primary_endpoint"),
            secondary_endpoints=to_json_array(pf.get("secondary_endpoints")),
            source_url=(
                f"{CHICTR_BASE_URL}{CHICTR_DETAIL_PATH}"
                f"?proj={raw.get('proj_id', '')}"
            ),
            raw_payload=raw.get("html"),
        )

    def source_short_name(self) -> str:
        return "ChiCTR"
