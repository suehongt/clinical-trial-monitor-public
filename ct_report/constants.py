"""ct_report.constants — Static display constants: source/status labels, colors, quality matrices."""
from __future__ import annotations

from config import ACTIVE_PROFILE, ACTIVE_PROFILE_KEY


# 搜索关键词：由 CT_DISEASE_PROFILE 选择疾病 profile（默认 mi）
SEARCH_KEYWORDS = list(ACTIVE_PROFILE["report_keywords"])
PROFILE_KEY = ACTIVE_PROFILE_KEY
PROFILE_LABEL = ACTIVE_PROFILE["label"]
PROFILE_LABEL_EN = ACTIVE_PROFILE["label_en"]


SOURCE_LABELS: dict[str, str] = {
    "NCT": "ClinicalTrials.gov",
    "ChiCTR": "中国临床试验注册中心",
    "CTR": "中国药物临床试验登记与信息公示平台",
    "ICTRP": "WHO ICTRP",
    "ICTRP_native": "WHO ICTRP（除中国）",
    "ICTRP_ChiCTR": "WHO ICTRP → 中国临床试验注册中心",
    "CTIS": "欧盟临床试验信息系统（CTIS）",
    "ISRCTN": "ISRCTN 注册库",
    "EUCTR": "欧盟临床试验注册库（历史）",
}


SOURCE_LABELS_EN: dict[str, str] = {
    "NCT": "ClinicalTrials.gov",
    "ChiCTR": "ChiCTR (Chinese Clinical Trial Register)",
    "CTR": "CTR (China Drug Trials)",
    "ICTRP": "WHO ICTRP",
    "ICTRP_native": "WHO ICTRP (non-China)",
    "ICTRP_ChiCTR": "WHO ICTRP → ChiCTR",
    "CTIS": "EU Clinical Trials Information System (CTIS)",
    "ISRCTN": "ISRCTN Registry",
    "EUCTR": "EU Clinical Trials Register (historical)",
}


SOURCE_COLORS: dict[str, str] = {
    "NCT": "#2563eb",
    "ChiCTR": "#dc2626",
    "CTR": "#ca8a04",
    "ICTRP": "#16a34a",
    "ICTRP_native": "#059669",
    "ICTRP_ChiCTR": "#dc2626",
    "CTIS": "#7c3aed",
    "ISRCTN": "#0891b2",
    "EUCTR": "#9333ea",
}


STATUS_LABELS: dict[str, str] = {
    "Not yet recruiting": "尚未招募",
    "Recruiting": "招募中",
    "Enrolling by invitation": "邀请入组",
    "Active, not recruiting": "进行中（未招募）",
    "Suspended": "暂停",
    "Terminated": "终止",
    "Completed": "已完成",
    "Withdrawn": "撤回",
    "Unknown": "未知",
    "No status found": "无状态",
    "Available": "可获取",
    "Temporarily not available": "暂不可用",
    "Withheld": "未公开",
    "Approved for marketing": "已批准上市",
}


FIELD_QUALITY: dict[str, dict[str, str]] = {
    "ICTRP_ChiCTR": {
        "Status": "✅ 可用 (同 ICTRP)",
        "Conditions": "✅ 可用 (同 ICTRP)",
        "Sponsors": "✅ 可用 (同 ICTRP)",
        "Enrollment": "✅ 可用 (同 ICTRP)",
        "Primary Endpoint": "✅ 可用 (同 ICTRP)",
        "Eligibility Criteria": "✅ 可用 (同 ICTRP)",
        "Study Phase": "⚠️ 可用但值不规范 (同 ICTRP)",
        "Secondary Endpoints": "⚠️ 可用 (同 ICTRP)",
        "Study Design": "❌ 缺失 (同 ICTRP)",
        "Locations": "❌ 缺失 (同 ICTRP)",
        "Arm Group": "❌ 缺失 (同 ICTRP)",
    },
    "ICTRP": {
        "Status": "✅ 可用 (99.0%)",
        "Study Type": "✅ 可用 (98.1%)",
        "Countries": "✅ 可用 (92.5%)",
        "Sponsors": "✅ 可用 (99.9%)",
        "Enrollment": "✅ 可用 (98.8%)",
        "Primary Endpoint": "✅ 可用 (99.0%)",
        "Conditions": "✅ 可用 (99.7%)",
        "Eligibility Criteria": "✅ 可用 (100%)",
        "Study Phase": "⚠️ 可用 (79%) 但值不规范",
        "Secondary Endpoints": "⚠️ 可用 (76.3%)",
        "Study Design": "❌ 缺失",
        "Locations": "❌ 缺失",
        "Arm Group": "❌ 缺失",
    },
    "NCT": {
        "Status": "✅ 完整",
        "Study Type": "✅ 完整",
        "Study Design": "✅ 完整 (77%)",
        "Locations": "✅ 完整 (94%)",
        "Arm Group": "✅ 完整 (92%)",
        "Countries": "✅ 完整 (94%)",
        "Sponsors": "✅ 完整",
        "Enrollment": "✅ 完整",
        "Primary Endpoint": "✅ 完整",
        "Eligibility Criteria": "✅ 完整",
        "Conditions": "✅ 完整",
    },
    "ChiCTR": {
        "Status": "❌ 缺失",
        "Study Design": "✅ 完整 (100%)",
        "Locations": "✅ 完整 (100%)",
        "Study Phase": "✅ 完整 (100%)",
        "Sponsors": "✅ 完整",
        "Eligibility Criteria": "✅ 完整",
        "Conditions": "✅ 完整",
    },
    "CTR": {
        "Study Type": "❌ 缺失",
        "Study Design": "❌ 缺失",
        "Arm Group": "✅ 完整 (100%)",
        "Status": "✅ 完整 (100%)",
        "Secondary Endpoints": "✅ 完整 (100%)",
        "Locations": "✅ 完整 (100%)",
        "Primary Endpoint": "✅ 完整 (100%)",
        "Sponsors": "✅ 完整",
    },
}


FIELD_QUALITY_MATRIX = [
    ("Title", "✅", "✅", "✅", "✅"),
    ("Status", "✅", "✅", "❌", "✅"),
    ("Study Type", "✅", "❌", "✅", "✅"),
    ("Study Design", "⚠️ 77%", "❌", "✅ 100%", "❌"),
    ("Locations", "✅ 94%", "✅", "✅ 100%", "❌"),
    ("Arm Group", "✅ 92%", "✅", "❌", "❌"),
    ("Conditions", "✅", "✅", "✅", "✅"),
    ("Enrollment", "✅", "⚠️ 91%", "✅", "✅"),
    ("Sponsors", "✅", "✅", "✅", "✅"),
    ("Countries", "✅ 94%", "✅", "✅", "✅"),
    ("Primary Endpoint", "✅", "✅", "✅", "✅"),
    ("Secondary Endpoints", "⚠️ 79%", "✅", "⚠️ 83%", "⚠️ 76%"),
    ("Study Phase", "⚠️ 77%", "✅", "✅", "⚠️ 79%"),
    ("Eligibility", "✅", "✅", "✅", "✅"),
]
