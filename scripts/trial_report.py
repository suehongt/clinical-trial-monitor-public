# -*- coding: utf-8 -*-
"""跨源临床试验综合报告生成器 — CLI 入口.

实现拆分在 ct_report 包中:
  query / diffing / translate / render / report / constants / dictionaries
疾病口径由 CT_DISEASE_PROFILE 环境变量选择（默认 mi），用法:
  python3 scripts/trial_report.py --english [--force] [--since ...] [-k ...]
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ct_report.cli import main

if __name__ == "__main__":
    main()
