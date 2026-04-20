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
TEMPLATE_FILE = TICKERS / "AAPL.html"  # any existing page works as template

# Line that needs substitution:
#   const SYMBOL = "AAPL";
SYMBOL_LINE_RE = re.compile(r'const\s+SYMBOL\s*=\s*"([A-Z0-9\-\.]+)"\s*;')


def _load_dataset():
    raw = DATASET.read_bytes().rstrip(b"\x00 \t\r\n")
    return json.loads(raw.decode("utf-8"))


def main():
    if not TEMPLATE_FILE.exists():
        print(f"ERROR: template file missing: {TEMPLATE_FILE}", file=sys.stderr)
        return 1
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    m = SYMBOL_LINE_RE.search(template)
    if not m:
        print(f"ERROR: template has no `const SYMBOL = \"...\";` line to substitute.",
              file=sys.stderr)
        return 1

    data = _load_dataset()
    symbols = [t["symbol"] for t in data.get("tickers", []) if t.get("symbol")]
    print(f"Generating {len(symbols)} ticker pages from template ({TEMPLATE_FILE.name})...")

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

    # Clean up stale .html files for symbols no longer in top-N
    removed = 0
    for f in TICKERS.glob("*.html"):
        if f.name not in keep:
            f.unlink()
            removed += 1
    print(f"Wrote {len(symbols)} pages; removed {removed} stale pages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
