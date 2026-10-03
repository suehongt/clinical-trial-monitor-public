"""ct_report.textutil — Generic text/HTML escaping and truncation helpers."""
from __future__ import annotations

import html
import re
from typing import Any


def _normalise_external_text(val: Any) -> str:
    """Collapse encoding layers that registries stack onto user text.

    ClinicalTrials.gov sponsors write ``\\&``, ``\\<`` … and the registry
    may additionally HTML-encode on top (``\\&amp;amp;`` appears verbatim
    in stored criteria).  Unescape entities (up to 3 levels) and strip the
    backslash-escape convention so display sees plain "P&Q", "P<=75".
    """
    text = str(val)
    while True:  # collapse entity encoding to a fixpoint
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    return re.sub(r"\\([<>&])", r"\1", text)


def _safe_text(val: Any, max_len: int = 500) -> str:
    """Truncate and HTML-escape a value for embedding in the report.

    Registry text comes from external sites (titles, sponsors, criteria…)
    and is interpolated into f-string HTML, so it must never reach the page
    raw.  Truncation happens before escaping so an entity can never be cut
    in half.
    """
    if val is None:
        return '<span class="na">-</span>'
    text = _normalise_external_text(val).strip()
    if not text:
        return '<span class="na">-</span>'
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return html.escape(text, quote=True)


def esc(val: Any) -> str:
    """HTML-escape a value (same normalisation as _safe_text).

    For list items, badge labels and other fragments that need escaping
    but no truncation or N/A handling.
    """
    return html.escape(_normalise_external_text(val), quote=True)
