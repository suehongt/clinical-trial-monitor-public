"""Dictionary-fallback translations must not pollute the persistent cache.

Before the fix, a run without DEEPSEEK_API_KEY wrote dictionary-quality
translations into ~/.cache/ct_monitor_translate.json, permanently pinning
those texts at fallback quality even after a key was configured.
"""
from __future__ import annotations

import pytest

import ct_report.translate as tr


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_TRANSLATION_CACHE_PATH", tmp_path / "translate.json")
    monkeypatch.setattr(tr, "_TRANSLATION_CACHE", {})
    monkeypatch.setattr(tr, "_DICT_ONLY", set())
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return tmp_path / "translate.json"


def test_dict_fallback_not_persisted(fresh_cache):
    out = tr._batch_translate_llm(["Recruiting status"])
    assert out  # in-memory result exists for this run
    assert not fresh_cache.exists()  # but nothing was written to disk


def test_save_skips_dict_only_entries(fresh_cache):
    tr._TRANSLATION_CACHE["aaa"] = "LLM 译文"
    tr._TRANSLATION_CACHE["bbb"] = "字典回退"
    tr._DICT_ONLY.add("bbb")
    tr._save_translation_cache()
    import json

    saved = json.loads(fresh_cache.read_text(encoding="utf-8"))
    assert saved == {"aaa": "LLM 译文"}
