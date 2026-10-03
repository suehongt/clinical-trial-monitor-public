"""ct_report.cli — Command-line entry point for the cross-source report generator."""
from __future__ import annotations

import logging
import argparse
from ct_report.constants import PROFILE_LABEL, SEARCH_KEYWORDS
from ct_report.report import generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",)


def main():
    parser = argparse.ArgumentParser(
        description="跨源临床试验综合报告生成器（当前疾病口径：" + PROFILE_LABEL + "，"
                    "可用 CT_DISEASE_PROFILE 环境变量切换，见 config.DISEASE_PROFILES）"
    )
    parser.add_argument(
        "--output", "-o",
        help="输出HTML文件路径",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制重新生成（忽略增量检查）",
    )
    parser.add_argument(
        "--since",
        help="只查询指定ISO时间之后的记录 (e.g. '2026-07-20 00:00:00')",
    )
    parser.add_argument(
        "--keywords", "-k",
        nargs="+",
        default=SEARCH_KEYWORDS,
        help="搜索关键词（默认：当前疾病 profile 的关键词）",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="生成后自动打开浏览器",
    )
    parser.add_argument(
        "--english", "-e",
        action="store_true",
        help="生成英文报告（无翻译、无数据质量说明）",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="Generate the report WITHOUT moving the incremental baseline (one-off / analysis runs).",
    )
    args = parser.parse_args()

    path = generate_report(
        output=args.output,
        force=args.force,
        keywords=args.keywords,
        since=args.since,
        english=args.english,
        update_checkpoint=not args.no_checkpoint,
    )

    output_label = path if args.english else f"报告已生成: {path}"
    print(f"\n{output_label}")
    if args.serve:
        import webbrowser
        webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
