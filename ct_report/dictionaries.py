"""ct_report.dictionaries — Medical translation dictionaries, loaded from packaged JSON data."""
from __future__ import annotations

import json
import re
from pathlib import Path


_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load(name: str) -> dict:
    with open(_DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


_MEDICAL_ZH: dict = _load("medical_zh.json")
_EN_LEFTOVERS: dict = _load("en_leftovers.json")


_EN_LEFTOVERS_LIST = sorted(_EN_LEFTOVERS.items(), key=lambda x: -len(x[0]))
_EN_WORD_RE = re.compile(
    r'\\b(' + '|'.join(re.escape(eng) for eng, _ in _EN_LEFTOVERS_LIST) + r')\\b'
)
