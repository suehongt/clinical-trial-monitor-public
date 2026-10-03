"""
Global Clinical Trial Monitor — Configuration
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
_configured_data_dir = os.environ.get("CT_DATA_DIR")
_runtime_root = Path(_configured_data_dir).expanduser().resolve() if _configured_data_dir else PROJECT_ROOT
DB_DIR = _runtime_root / "db"
LOG_DIR = _runtime_root / "logs"
DATA_DIR = _runtime_root / "data"
RAW_JSON_DIR = DATA_DIR / "raw"
BACKUP_DIR = _runtime_root / "backups"

for _d in (DB_DIR, LOG_DIR, DATA_DIR, RAW_JSON_DIR):
    pass  # directories are created lazily by ensure_dirs()


def ensure_dirs() -> None:
    """Create runtime directories (idempotent).

    Called by entrypoints (run_monitor.main, BaseCollector.run) instead of
    at import time, so importing config never touches the filesystem.
    """
    for _d in (CONFIG.db.path.parent, CONFIG.log.file.parent, DATA_DIR, RAW_JSON_DIR, BACKUP_DIR):
        _d.mkdir(parents=True, exist_ok=True)


# ── Disease search profiles (shared by collectors + reporting) ────────────
# Single source of truth per disease: query_cond for ClinicalTrials.gov,
# report keywords for the cross-source report.  Select the active profile
# with the CT_DISEASE_PROFILE environment variable (default: "mi").

MI_QUERY = (
    "myocardial infarction OR acute coronary syndrome OR AMI "
    "OR STEMI OR NSTEMI"
)

MI_REPORT_KEYWORDS = [
    "心肌梗死",
    "myocardial infarction",
    "acute coronary syndrome",
    "心梗",
    "冠脉综合征",
    "AMI",
    "STEMI",
    "NSTEMI",
]

MM_QUERY = (
    "multiple myeloma OR plasma cell myeloma OR myeloma OR MGUS"
)

MM_REPORT_KEYWORDS = [
    "多发性骨髓瘤",
    "骨髓瘤",
    "浆细胞骨髓瘤",
    "浆细胞白血病",
    "multiple myeloma",
    "plasma cell myeloma",
    "plasma cell leukemia",
    "myeloma",
    "MGUS",
]

HF_QUERY = (
    "heart failure OR HFpEF OR HFrEF"
)

HF_REPORT_KEYWORDS = [
    "心力衰竭",
    "心衰",
    "heart failure",
    "HFpEF",
    "HFrEF",
    "HFmrEF",
]

CMP_QUERY = "cardiomyopathy"

CMP_REPORT_KEYWORDS = [
    "心肌病",
    "cardiomyopathy",
    "肥厚型心肌病",
    "扩张型心肌病",
    "限制型心肌病",
    "围产期心肌病",
    "应激性心肌病",
    "Takotsubo",
    "致心律失常性心肌病",
]

DISEASE_PROFILES = {
    "mi": {"label": "心肌梗死", "label_en": "Myocardial Infarction",
           "query_cond": MI_QUERY, "report_keywords": MI_REPORT_KEYWORDS},
    "hf": {"label": "心力衰竭", "label_en": "Heart Failure",
           "query_cond": HF_QUERY, "report_keywords": HF_REPORT_KEYWORDS},
    "cmp": {"label": "心肌病", "label_en": "Cardiomyopathy",
            "query_cond": CMP_QUERY, "report_keywords": CMP_REPORT_KEYWORDS},
    "mm": {"label": "多发性骨髓瘤", "label_en": "Multiple Myeloma",
           "query_cond": MM_QUERY, "report_keywords": MM_REPORT_KEYWORDS},
}

ACTIVE_PROFILE_KEY = os.environ.get("CT_DISEASE_PROFILE", "mi")
ACTIVE_PROFILE = DISEASE_PROFILES[ACTIVE_PROFILE_KEY]


def waf_pacing(source: str, fallback: float,
               limits_root: Optional[Path] = None) -> float:
    """WAF batch pacing for `source`, driven by the weekly probe results.

    docs/waf_limits.json (updated by scripts/waf_probe.py) records the
    last_known_good pacing measured on the live site; config follows it so
    crawl rules automatically track tested limits.  Falls back to the
    static value when the file is missing/corrupt.  `limits_root` overrides
    the directory that holds docs/waf_limits.json (tests).
    """
    root = Path(limits_root) if limits_root else _runtime_root
    try:
        limits = json.loads(
            (root / "docs" / "waf_limits.json").read_text(encoding="utf-8"))
        value = limits["last_known_good"][source]
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return fallback


def combined_query(profile_keys) -> str:
    """OR-join the query_conds of the given profiles into one search expression.

    Used by the multi-project pipeline: a single crawl with the combined
    query populates the DB for every profile at once (each profile's report
    then filters by its own keywords).
    """
    conds, seen = [], set()
    for key in profile_keys:
        for part in DISEASE_PROFILES[key]["query_cond"].split(" OR "):
            part = part.strip()
            if part and part.upper() not in seen:
                seen.add(part.upper())
                conds.append(part)
    return " OR ".join(conds)


@dataclass
class SourceConfig:
    """Single registry-source connection settings."""
    name: str
    short_name: str
    enabled: bool = False
    base_url: str = ""
    api_base: str = ""
    max_records_per_run: int = 500
    request_delay_sec: float = 1.0
    timeout_sec: int = 60
    extra: dict = field(default_factory=dict)  # Source-type-specific settings


@dataclass
class DatabaseConfig:
    path: Path = field(default_factory=lambda: Path(os.environ.get("CT_DB_PATH", str(DB_DIR / "ct_monitor.db"))).expanduser())
    pragmas: Dict[str, str] = field(default_factory=lambda: {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "cache_size": "-64000",
    })


@dataclass
class CrawlConfig:
    """Scheduling & behaviour defaults."""
    bootstrap_lookback_days: int = 365        # Bootstrap: how far back
    incremental_lookback_hours: int = 72      # Incremental: lookback window
    retry_attempts: int = 3
    retry_delay_sec: int = 30
    page_size: int = 100                       # API page size


@dataclass
class SyncConfig:
    """Last-successful-sync tracking."""
    enabled: bool = True
    # If last_successful_sync is older than this, force a full re-bootstrap
    max_staleness_days: int = 14


@dataclass
class LogConfig:
    level: str = "INFO"
    file: Path = LOG_DIR / "monitor.log"
    max_mb: int = 50
    backup_count: int = 3


@dataclass
class ChangeDetectionConfig:
    tracked_fields: List[str] = field(default_factory=lambda: [
        "status_id",
        "enrollment",
        "start_date",
        "primary_completion_date",
        "completion_date",
        "study_phase",
        "study_design",
        "sponsors",
        "conditions",
        "countries",
        "primary_endpoint",
        "secondary_endpoints",
    ])
    # Fields whose raw ORDER should be ignored for comparison
    order_insensitive_fields: List[str] = field(default_factory=lambda: [
        "conditions",
        "countries",
        "interventions",
        "secondary_endpoints",
        "locations",
    ])


# ── Singleton ──────────────────────────────────────────────────────────────

@dataclass
class EntityResolutionConfig:
    min_title_similarity: float = 0.85
    max_candidates: int = 200


@dataclass
class WafConfig:
    """WAF tuning for the ChiCTR / CTR collectors (browser_base, waf_http).

    circuit_breaker_threshold: consecutive WAF-challenge pages after which the
    run aborts (grinding on deepens bans); 0 disables the breaker.
    settle_jitter: randomise the page settle time (0.8–1.6×) to look less
    machine-like. proxy_url: optional egress proxy for Playwright contexts and
    requests sessions, sourced from the CT_WAF_PROXY_URL environment variable
    (empty = direct).
    challenge_mode: transport selection for WAF-protected paths
    (CT_WAF_CHALLENGE_MODE env). "auto" = pure-HTTP hot path with offline
    acw_sc__v2 solving, degrading to the browser only on the new-version
    challenge (aliyun_waf_aa/bb); "browser_only" = legacy Playwright path
    (one-kill rollback switch).
    min_request_interval_sec: WAF rhythm floor between consecutive HTTP
    requests (waf_http transport). The HTTP path is fast enough to outrun
    the old browser-latency-paced cadence, so the floor keeps the effective
    rate at the browser-era level (~5s/request) — a WAF hard constraint,
    never to be tuned down.
    failure_breaker_threshold: consecutive fetch attempts that exhaust all
    retries WITHOUT a WAF page (timeouts / network errors) after which the
    circuit opens — e.g. the 瑞数 WAF on CTR enters phases where every detail
    page times out (3×45s per row); without this breaker a batch grinds for
    hours and burns enrich attempts. 0 disables.
    circuit_reset_after_sec: seconds after which an OPEN circuit auto-resets
    (half-open) in ensure_closed — cooldown-based recovery for the long-lived
    API server process, where "open until restart" turned one tripped window
    into permanent live-check unavailability (2026-09-24 demo incident). A
    still-flagged IP simply re-trips after ≤threshold requests, so that is
    the only cost of a premature reset. 0 = never auto-reset (keeps the
    strict batch-run "abort this round" semantics).
    """
    circuit_breaker_threshold: int = 3
    settle_jitter: bool = True
    proxy_url: Optional[str] = None
    challenge_mode: str = "auto"
    min_request_interval_sec: float = 4.5
    failure_breaker_threshold: int = 5
    circuit_reset_after_sec: int = 1800


@dataclass
class AppConfig:
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    log: LogConfig = field(default_factory=LogConfig)
    change: ChangeDetectionConfig = field(default_factory=ChangeDetectionConfig)
    er: EntityResolutionConfig = field(default_factory=EntityResolutionConfig)
    waf: WafConfig = field(default_factory=lambda: WafConfig(
        proxy_url=os.environ.get("CT_WAF_PROXY_URL") or None,
        challenge_mode=os.environ.get("CT_WAF_CHALLENGE_MODE") or "auto",
    ))

    sources: Dict[str, SourceConfig] = field(default_factory=lambda: {
        "clinicaltrials_gov": SourceConfig(
            name="ClinicalTrials.gov",
            short_name="NCT",
            enabled=False,
            base_url="https://clinicaltrials.gov",
            api_base="https://clinicaltrials.gov/api/v2",
            # 0 = 不限条数（全量检索）。bootstrap 时按 condition 全历史抓取，
            # 增量时按 condition + 日期窗口抓取。
            max_records_per_run=0,
            request_delay_sec=0.3,
            extra=dict(
                # 当前疾病 profile 的 condition 检索式（API v2 query.cond），
                # 由 CT_DISEASE_PROFILE 选择（默认 mi）。仅英文（NCT 为英文库）。
                query_cond=ACTIVE_PROFILE["query_cond"],
            ),
        ),
        "chinadrugtrials": SourceConfig(
            name="中国药物临床试验登记与信息公示平台",
            short_name="CTR",
            enabled=False,
            base_url="http://www.chinadrugtrials.org.cn",
            max_records_per_run=200,
            request_delay_sec=waf_pacing("ctr", 2.5),  # driven by weekly probe (docs/waf_limits.json)
            extra=dict(
                # 单次 enrich 的详情页上限（G2：单晚 ≤50，控制 WAF 暴露）
                enrich_batch_size=200,
            ),
        ),
        "chictr": SourceConfig(
            name="中国临床试验注册中心 (ChiCTR)",
            short_name="ChiCTR",
            enabled=False,
            base_url="https://www.chictr.org.cn",
            max_records_per_run=200,
            request_delay_sec=waf_pacing("chictr", 2.5),  # driven by weekly probe (docs/waf_limits.json)
            extra=dict(
                enrich_batch_size=200,
                # 全量覆盖：每关键词最多翻 50 页（约 500 条结果）
                search_max_pages=50,
            ),
        ),
        "who_ictrp": SourceConfig(
            name="WHO ICTRP",
            short_name="ICTRP",
            enabled=False,
            base_url="https://trialsearch.who.int",
            max_records_per_run=1000,
            request_delay_sec=0.5,
            extra=dict(
                source_type="AGGREGATOR",
                # Pre-downloaded XML export; path follows the active disease
                # profile so switching CT_DISEASE_PROFILE never imports
                # another profile's snapshot. Fetch with
                # scripts/fetch_ictrp_xml.py (same default path).
                xml_export_path=f"data/ictrp_{ACTIVE_PROFILE_KEY}_export.xml",
            ),
        ),
        "ctis": SourceConfig(
            name="EU Clinical Trials Information System (CTIS)",
            short_name="CTIS",
            enabled=False,
            base_url="https://euclinicaltrials.eu/ctis-public",
            api_base="https://euclinicaltrials.eu/ctis-public-api",
            # Pilot cap; raise after observing daily volume and API latency.
            max_records_per_run=200,
            request_delay_sec=0.2,
            extra=dict(
                query_cond=ACTIVE_PROFILE["query_cond"],
                # Overlap detail-request latency while the adapter's global
                # pacer still spaces every outbound request start.
                detail_workers=4,
            ),
        ),
        "isrctn": SourceConfig(
            name="ISRCTN Registry",
            short_name="ISRCTN",
            enabled=False,
            base_url="https://www.isrctn.com",
            api_base="https://www.isrctn.com",
            # Pilot cap; raise after observing daily volume and payload size.
            max_records_per_run=200,
            request_delay_sec=0.2,
            extra=dict(query_cond=ACTIVE_PROFILE["query_cond"]),
        ),
        "euctr": SourceConfig(
            name="EU Clinical Trials Register (EUCTR historical)",
            short_name="EUCTR",
            enabled=False,
            base_url="https://www.clinicaltrialsregister.eu",
            api_base="https://www.clinicaltrialsregister.eu",
            # Historical backfill is manual and bounded, never a daily poll.
            max_records_per_run=200,
            request_delay_sec=0.25,
            extra=dict(
                query_cond=ACTIVE_PROFILE["query_cond"],
                historical_backfill_only=True,
                detail_workers=4,
            ),
        ),
    })

    @property
    def enabled_sources(self) -> Dict[str, SourceConfig]:
        return {k: v for k, v in self.sources.items() if v.enabled}


CONFIG = AppConfig()
