"""HTML-escaping guarantees for the report render layer.

Registry text (titles, sponsors, criteria, endpoints…) comes from
external sites and is interpolated into f-string HTML by ct_report.
These tests pin the escaping contract: external text must never reach
the page raw, already-escaped source data must not be double-escaped,
and truncation must never cut an entity in half (regression for the
P0 escaping fix found in the 2026-09 code review).
"""
from __future__ import annotations

from ct_report.render import (_criteria_to_html, _format_endpoints,
                              _format_list_json, _registry_url,
                              _status_badge, format_cde_endpoint_table,
                              format_cross_source_badge,
                              parse_cde_endpoint_table)
from ct_report.textutil import _safe_text, esc


def test_safe_text_escapes_markup_and_ampersands():
    out = _safe_text('<B>ACS & "quoted"</B>')
    assert "<B>" not in out and "</B>" not in out
    assert "&lt;B&gt;" in out
    assert "&amp;" in out
    assert "&quot;quoted&quot;" in out


def test_safe_text_does_not_double_escape_entities():
    assert _safe_text("P&amp;P") == "P&amp;P"
    assert esc("P&amp;P") == "P&amp;P"


def test_safe_text_collapses_multi_level_source_encoding():
    # Regression: NCT criteria store verbatim "\\&amp;amp;" (sponsor "\\&"
    # plus an extra registry-side encode). Must render as a single "&".
    assert _safe_text("\\&amp;amp; laptop") == "&amp; laptop"
    assert esc("\\&amp;lt;40%") == "&lt;40%"
    assert _safe_text("\\<= 75 U/L") == "&lt;= 75 U/L"


def test_safe_text_truncates_before_escaping():
    # "<b>&" must be dropped whole by truncation, never partially escaped
    assert _safe_text("A" * 8 + "<b>&", max_len=8) == "AAAAAAAA..."


def test_safe_text_empty_and_none():
    assert _safe_text(None) == '<span class="na">-</span>'
    assert _safe_text("   ") == '<span class="na">-</span>'


def test_format_endpoints_escapes_list_items():
    out = _format_endpoints('["PFS & OS", "<i>x</i>"]')
    assert "<li>PFS &amp; OS</li>" in out
    assert "&lt;i&gt;x&lt;/i&gt;" in out
    assert "<i>" not in out


def test_format_endpoints_empty_array_is_na():
    # Regression: literal "[]" stored by collectors must render as N/A
    assert _format_endpoints("[]") == '<span class="na">-</span>'
    assert _format_endpoints(" [ ] ") == '<span class="na">-</span>'


def test_format_list_json_escapes_and_handles_empty():
    assert _format_list_json("[]") == '<span class="na">-</span>'
    assert "<li>A&amp;B</li>" in _format_list_json('["A&B"]')


def test_status_badge_escapes_status_text():
    out = _status_badge('Recruiting" onload="x')
    assert "onload" in out  # text still visible
    assert '" onload="' not in out  # but not as a raw attribute breaker
    assert "&quot;" in out


def test_registry_url_escapes_trial_id():
    url = _registry_url("NCT", 'NCT1"onmouseover="x')
    assert url.startswith("https://clinicaltrials.gov/study/NCT1")
    assert '"' not in url.replace("&quot;", "")


def test_criteria_escapes_angle_brackets_and_ampersands():
    out = _criteria_to_html("1. ALT \\<= 75 U/L\n2. AST & Bili elevated")
    assert "&lt;= 75" in out          # un-escaped source "\\" + re-escaped once
    assert "AST &amp; Bili" in out
    assert "<=" not in out            # no raw '<' reaches the page


def test_criteria_escapes_ampersands_without_numbering():
    out = _criteria_to_html("single line without numbering & <tags>")
    assert "<li>" in out
    assert "&amp;" in out and "&lt;tags&gt;" in out


def test_cde_endpoint_table_roundtrip_escapes():
    text = "序号\n指标\n评价时间\n终点指标选择\n1\nPFS & OS\n每3个月\n主要指标"
    rows = parse_cde_endpoint_table(text)
    assert rows and rows[0]["indicator"] == "PFS & OS"
    table = format_cde_endpoint_table(rows)
    assert table and "PFS &amp; OS</td>" in table
    assert format_cde_endpoint_table([]) is None


def test_cross_source_badge_escapes_and_handles_empty():
    assert format_cross_source_badge(None) == ""
    assert format_cross_source_badge([]) == ""
    out = format_cross_source_badge([
        {"short_name": "ChiCTR", "source_trial_id": 'ChiCTR1"&x',
         "source_url": "https://www.chictr.org.cn/show.html?regno=ChiCTR1"},
    ])
    assert 'class="xsrc-chip"' in out
    assert 'title="ChiCTR1&quot;&amp;x"' in out
    assert "跨源" in out
    assert "registered on" in format_cross_source_badge(
        [{"short_name": "CTR", "source_trial_id": "CTR1", "source_url": "u"}],
        english=True,
    )


def test_status_badge_unknown_label_localised():
    assert "未知" in _status_badge(None)
    assert "Unknown" in _status_badge(None, unknown_label="Unknown")
