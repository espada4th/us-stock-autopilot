#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build universe.json by scraping Wikipedia.

Sources:
  - S&P 500
  - S&P 400 (MidCap)
  - S&P 600 (SmallCap)
  - NASDAQ-100

Output: universe.json with ~1500-1600 unique tickers (deduplicated)

Run manually:  python scripts/build_universe.py
Run on Actions: workflow does this automatically if universe.json is missing
                or older than 30 days.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "universe.json"

SOURCES = [
    # (index_name, wikipedia_url, hint_to_find_table)
    ("SP500",  "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    ("SP400",  "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"),
    ("SP600",  "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"),
    ("NDX100", "https://en.wikipedia.org/wiki/Nasdaq-100"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; dip-scanner/1.0; +https://github.com/espada4th/us-stock-autopilot)"
}


def _norm_sym(s):
    """Normalize ticker for yfinance: BRK.B -> BRK-B"""
    s = str(s).strip().upper()
    if not s or s == "NAN":
        return None
    return s.replace(".", "-")


def _find_pick_columns(df):
    """Return (sym_col, name_col, sec_col) by best-effort matching column names."""
    cols = list(df.columns)
    cmap = {c: str(c).lower() for c in cols}
    sym_col = name_col = sec_col = None
    for c, lc in cmap.items():
        if sym_col is None and lc in ("ticker", "symbol", "ticker symbol"):
            sym_col = c
        if name_col is None and lc in ("company", "security", "name"):
            name_col = c
        if sec_col is None and "sector" in lc and "sub" not in lc:
            sec_col = c
    return sym_col, name_col, sec_col


def _scrape_wikipedia(url):
    """Return list of dicts: {symbol, name, sector}. Tries pandas.read_html first."""
    import pandas as pd
    import requests
    # pandas.read_html doesn't accept headers directly; use requests + io
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    from io import StringIO
    tables = pd.read_html(StringIO(r.text))
    rows = []
    for t in tables:
        sym_col, name_col, sec_col = _find_pick_columns(t)
        if sym_col is None:
            continue
        for _, r in t.iterrows():
            sym = _norm_sym(r[sym_col])
            if not sym:
                continue
            name = str(r[name_col]).strip() if name_col else ""
            sector = str(r[sec_col]).strip() if sec_col else ""
            rows.append({"symbol": sym, "name": name, "sector": sector})
        if rows:
            break  # take the first usable table only
    return rows


def main():
    all_rows = {}
    for idx_name, url in SOURCES:
        print(f"Fetching {idx_name} from {url} ...", flush=True)
        try:
            rows = _scrape_wikipedia(url)
        except Exception as e:
            print(f"  WARN: {idx_name} failed: {e}", file=sys.stderr)
            continue
        added = 0
        for row in rows:
            sym = row["symbol"]
            if sym not in all_rows:
                all_rows[sym] = {
                    "symbol": sym,
                    "name": row["name"],
                    "sector": row["sector"],
                    "indices": [],
                }
                added += 1
            if idx_name not in all_rows[sym]["indices"]:
                all_rows[sym]["indices"].append(idx_name)
            # Prefer non-empty name/sector
            if not all_rows[sym]["name"] and row["name"]:
                all_rows[sym]["name"] = row["name"]
            if not all_rows[sym]["sector"] and row["sector"]:
                all_rows[sym]["sector"] = row["sector"]
        print(f"  {idx_name}: parsed {len(rows)} rows, +{added} new (total now {len(all_rows)})")

    universe = sorted(all_rows.values(), key=lambda x: x["symbol"])
    out = {
        "version": 1,
        "source": "Wikipedia: S&P 500 + S&P 400 + S&P 600 + NASDAQ-100",
        "total": len(universe),
        "tickers": universe,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes, {len(universe)} unique tickers)")

    # Stats
    from collections import Counter
    combos = Counter(tuple(sorted(v["indices"])) for v in universe)
    print("Index membership combos:")
    for combo, n in combos.most_common():
        print(f"  {'+'.join(combo):20s}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
