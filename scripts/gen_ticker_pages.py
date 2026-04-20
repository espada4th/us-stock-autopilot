#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate ticker/SYM.html for every symbol in dataset.json.

Strategy: use the shared template from one existing page (AAPL.html) and
substitute the SYMBOL constant. The page's JS then fetches ../dataset.json
and renders whichever row matches its SYMBOL.

Also removes stale ticker/*.html for symbols no longer in the top-N list.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset.json"
TICKERS = ROOT / "ticker"
# Preferred template; fallback to any existing ticker/*.html if missing.
PREFERRED_TEMPLATE = TICKERS / "AAPL.html"
MIN_TEMPLATE_SIZE = 20000  # guard against truncated templates

# Line that needs substitution:
#   const SYMBOL = "AAPL";
SYMBOL_LINE_RE = re.compile(r'const\s+SYMBOL\s*=\s*"([A-Z0-9\-\.]+)"\s*;')


def _load_dataset():
    raw = DATASET.read_bytes().rstrip(b"\x00 \t\r\n")
    return json.loads(raw.decode("utf-8"))


def _find_template():
    """Return (path, text) for a usable template page.

    Strategy:
      1. Prefer ticker/AAPL.html (canonical template).
      2. Fallback: pick the LARGEST ticker/*.html that parses as a valid template
         (contains the SYMBOL-assignment line AND is above MIN_TEMPLATE_SIZE).
    """
    candidates = []
    if PREFERRED_TEMPLATE.exists():
        candidates.append(PREFERRED_TEMPLATE)
    # Add all other ticker pages as fallback, sorted by size descending so
    # we try the most-complete ones first.
    for f in sorted(TICKERS.glob("*.html"), key=lambda p: p.stat().st_size, reverse=True):
        if f not in candidates:
            candidates.append(f)
    for f in candidates:
        try:
            txt = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if len(txt) < MIN_TEMPLATE_SIZE:
            continue
        if not SYMBOL_LINE_RE.search(txt):
            continue
        if "</html>" not in txt:
            # truncated — skip
            continue
        return f, txt
    return None, None


def main():
    template_path, template = _find_template()
    if template is None:
        print(f"ERROR: no usable template found in {TICKERS}", file=sys.stderr)
        return 1
    if template_path != PREFERRED_TEMPLATE:
        print(f"WARN: preferred template {PREFERRED_TEMPLATE.name} missing/invalid — using {template_path.name} as template instead")
        # Auto-restore AAPL.html from the fallback template so future runs
        # find the canonical file. Substitute SYMBOL to AAPL.
        restored = SYMBOL_LINE_RE.sub('const SYMBOL = "AAPL";', template, count=1)
        PREFERRED_TEMPLATE.write_text(restored, encoding="utf-8")
        print(f"  → auto-restored {PREFERRED_TEMPLATE.name} from {template_path.name}")
    m = SYMBOL_LINE_RE.search(template)
    if not m:
        print(f"ERROR: template has no `const SYMBOL = \"...\";` line to substitute.",
              file=sys.stderr)
        return 1

    data = _load_dataset()
    symbols = [t["symbol"] for t in data.get("tickers", []) if t.get("symbol")]
    print(f"Generating {len(symbols)} ticker pages from template ({template_path.name})...")

    TICKERS.mkdir(exist_ok=True)
    keep = set()
    for sym in symbols:
        page = SYMBOL_LINE_RE.sub(f'const SYMBOL = "{sym}";', template, count=1)
        out = TICKERS / f"{sym}.html"
        # Avoid a no-op rewrite (preserves git diff noise)
        if out.exists() and out.read_text(encoding="utf-8") == page:
            keep.add(out.name)
            continue
        out.write_text(page, encoding="utf-8")
        keep.add(out.name)

    # Clean up stale .html files for symbols no longer in top-N.
    # BUT always preserve BOTH:
    #   - the preferred template (ticker/AAPL.html) so future runs have a canonical
    #     source even if AAPL rotates out of top-50
    #   - the actual template we used this run (in case it differs from preferred)
    preserved = {PREFERRED_TEMPLATE.name, template_path.name}
    removed = 0
    for f in TICKERS.glob("*.html"):
        if f.name in preserved:
            continue  # never delete the template
        if f.name not in keep:
            f.unlink()
            removed += 1
    print(f"Wrote {len(symbols)} pages; removed {removed} stale pages (preserved template(s): {sorted(preserved)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
