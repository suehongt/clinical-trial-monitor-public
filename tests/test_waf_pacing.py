"""WAF pacing rule linkage: config follows the weekly probe's results.

The probe (scripts/waf_probe.py) records last_known_good pacing per source
in docs/waf_limits.json; config.waf_pacing() must read it so crawl rules
track tested limits automatically, and must fall back to static values
when the file is missing or corrupt.
"""
from __future__ import annotations

import json

import pytest

from config import waf_pacing


def _write_limits(root, data):
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "waf_limits.json").write_text(
        json.dumps(data), encoding="utf-8")
    return docs


def test_pacing_follows_probe_results(tmp_path):
    _write_limits(tmp_path, {"last_known_good": {"chictr": 1.5, "ctr": 1.5}})
    assert waf_pacing("chictr", 2.5, limits_root=tmp_path) == 1.5
    assert waf_pacing("ctr", 2.5, limits_root=tmp_path) == 1.5


def test_pacing_falls_back_when_file_missing(tmp_path):
    assert waf_pacing("chictr", 2.5, limits_root=tmp_path) == 2.5


def test_pacing_falls_back_when_file_corrupt(tmp_path):
    _write_limits(tmp_path, {})
    (tmp_path / "docs" / "waf_limits.json").write_text("{not json", encoding="utf-8")
    assert waf_pacing("chictr", 2.5, limits_root=tmp_path) == 2.5


def test_pacing_ignores_invalid_values(tmp_path):
    _write_limits(tmp_path, {"last_known_good": {"chictr": 0, "ctr": -3}})
    assert waf_pacing("chictr", 2.5, limits_root=tmp_path) == 2.5
    assert waf_pacing("ctr", 2.5, limits_root=tmp_path) == 2.5


def test_live_rules_file_drives_config_singleton():
    """The real docs/waf_limits.json (if present) feeds the CONFIG singleton."""
    import config
    limits_path = config._runtime_root / "docs" / "waf_limits.json"
    if not limits_path.exists():
        pytest.skip("no rules file in this checkout")
    recorded = json.loads(limits_path.read_text(encoding="utf-8"))["last_known_good"]
    assert config.CONFIG.sources["chictr"].request_delay_sec == \
        recorded["chictr"]
    assert config.CONFIG.sources["chinadrugtrials"].request_delay_sec == recorded["ctr"]
