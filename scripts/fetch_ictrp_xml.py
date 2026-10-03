#!/usr/bin/env python3
"""Download a WHO ICTRP XML export via the trialsearch.who.int ASP.NET flow.

Drives the search → terms → export form sequence with `requests` (no
browser needed) and saves the XML for use by the WHO ICTRP collector
(``sources.who_ictrp.extra.xml_export_path`` in config.py).

Note: the search POST must include every hidden form field — most
importantly ``ToolkitScriptManager_HiddenField`` — otherwise the portal
returns a bare error page.

Usage:
    python scripts/fetch_ictrp_xml.py                       # default query/output
    python scripts/fetch_ictrp_xml.py --query "breast cancer" \
        --output data/ictrp_breast_cancer.xml
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "https://trialsearch.who.int/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def hidden_fields(html: str) -> dict[str, str]:
    """All hidden form fields, incl. ToolkitScriptManager_HiddenField.

    The portal rejects search POSTs that omit it (bare 'Error Page').
    """
    fields = dict(re.findall(r'name="([A-Za-z_][A-Za-z_0-9]*)"[^>]*value="([^"]*)"', html))
    return {k: v for k, v in fields.items()
            if k.startswith("__") or k == "ToolkitScriptManager_HiddenField"}


def fetch_export(query: str, output: Path, max_seconds: int = 1800) -> bool:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": "https://trialsearch.who.int",
        "Referer": BASE,
    })

    print("1. Loading main page...")
    r = s.get(BASE, timeout=30)
    print(f"   status {r.status_code}, {len(r.text)} chars")

    print(f"2. Searching for {query!r}...")
    f = hidden_fields(r.text)
    f.update({"TextBox1": query, "Button1": "Search"})
    r = s.post(BASE, data=f, timeout=60)
    found = re.findall(r"(\d[\d,]*)\s*records", r.text)
    print(f"   search results: {found[:1] or 'unknown'}")
    if "Error Page" in r.text[:2000]:
        print("   portal returned an error page — aborting.")
        return False

    if "I agree" in r.text or "Button13" in r.text:
        print("   Terms page detected, agreeing...")
        f = hidden_fields(r.text)
        f["Button13"] = "I agree"
        r = s.post(BASE, data=f, timeout=60)

    if "Button14" not in r.text and "Export all trials to XML" not in r.text:
        print("   No export button on the page — aborting. "
              "Debug HTML saved to /tmp/ictrp_debug.html")
        Path("/tmp/ictrp_debug.html").write_text(r.text)
        return False

    print("3. Exporting all trials to XML (streaming)...")
    f = hidden_fields(r.text)
    f["Button14"] = "Export all trials to XML"
    start, chunks, size = time.time(), [], 0
    try:
        with s.post(BASE, data=f, timeout=(30, 120), stream=True) as r:
            print(f"   status {r.status_code}, content-type {r.headers.get('content-type')}")
            for chunk in r.iter_content(chunk_size=1 << 16):
                chunks.append(chunk)
                size += len(chunk)
                if size % (1 << 20) < (1 << 16):
                    print(f"   {size / 1e6:.1f} MB ({time.time() - start:.0f}s)", flush=True)
                if time.time() - start > max_seconds:
                    print("   wall-clock cap hit — keeping partial file")
                    break
    except requests.exceptions.RequestException as e:
        print(f"   connection issue after {size / 1e6:.1f} MB: {e}")

    blob = b"".join(chunks)
    if b"<Trial" not in blob or len(blob) < 10_000:
        print("   response does not contain trial XML — not saved.")
        return False
    try:
        ET.fromstring(blob)
    except ET.ParseError as exc:
        print(f"   incomplete or malformed XML — not saved: {exc}")
        return False

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(blob)
    temporary.replace(output)
    n_trials = blob.count(b"<Trial>")
    print(f"4. XML saved to {output}")
    print(f"   size {len(blob) / 1e6:.1f} MB, trial records ~{n_trials}")
    return True


def _profile_defaults() -> tuple[str, str]:
    """(default search query, output path) for the active disease profile.

    Mirrors config's per-profile ICTRP snapshot path so a fetch always
    lands where `run_monitor.py crawl --source who_ictrp` will read it.
    """
    try:
        from config import ACTIVE_PROFILE, ACTIVE_PROFILE_KEY
        kw = next((k for k in ACTIVE_PROFILE["report_keywords"] if k.isascii()), None)
        return (kw or ACTIVE_PROFILE["label_en"],
                f"data/ictrp_{ACTIVE_PROFILE_KEY}_export.xml")
    except Exception:
        return ("myocardial infarction", "data/ictrp_mi_export.xml")


def main() -> None:
    default_query, default_output = _profile_defaults()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default=default_query,
                        help=f"Search query (default: {default_query!r}, from CT_DISEASE_PROFILE)")
    parser.add_argument("--output", default=default_output,
                        help=f"Output XML path (default: {default_output})")
    parser.add_argument("--max-seconds", type=int, default=1800,
                        help="Maximum export transfer time (default: 1800)")
    args = parser.parse_args()

    ok = fetch_export(args.query, Path(args.output), max_seconds=args.max_seconds)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
