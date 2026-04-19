#\!/usr/bin/env python3
"""
Refresh dataset.json hourly from live sources.

Priority:
  1. Bigdata.com (preferred) — needs BIGDATA_API_KEY secret
  2. FMP (Financial Modeling Prep) — needs FMP_API_KEY secret

If neither is configured, the script bumps as_of and exits 0 so the workflow
won't fail — lets you deploy first, add secrets later.
"""
import json
import os
import sys
import datetime as dt
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset.json"

BIGDATA_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()
FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()


def _load_current():
    if DATASET.exists():
        with DATASET.open() as f:
            return json.load(f)
    return {"tickers": [], "sectors": [], "market_snapshot": {}}


def _save(data):
    data["as_of"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with DATASET.open("w") as f:
        json.dump(data, f, separators=(",", ":"))


# ---------------------------------------------------------------------------
# FMP updater (simple REST calls, free-tier friendly for major US tickers)
# ---------------------------------------------------------------------------
def refresh_via_fmp(data: dict) -> bool:
    import requests

    tickers = data.get("tickers", [])
    syms = ",".join(t["ticker"] for t in tickers if t.get("ticker"))
    if not syms:
        return False

    url = f"https://financialmodelingprep.com/api/v3/quote/{syms}?apikey={FMP_KEY}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        quotes = {q["symbol"]: q for q in r.json()}
    except Exception as e:
        print(f"FMP fetch failed: {e}", file=sys.stderr)
        return False

    for t in tickers:
        q = quotes.get(t["ticker"])
        if not q:
            continue
        t["last_price"] = q.get("price", t.get("last_price"))
        t["d1"] = q.get("changesPercentage", t.get("d1"))
        t["high_52w"] = q.get("yearHigh", t.get("high_52w"))
        t["low_52w"] = q.get("yearLow", t.get("low_52w"))
        if t["high_52w"] and t["low_52w"] and t["last_price"]:
            span = t["high_52w"] - t["low_52w"]
            t["pos_52w_pct"] = 100 * (t["last_price"] - t["low_52w"]) / span if span else 0

    # Index snapshot (S&P, Nasdaq, Dow, VIX, 10Y, WTI, Gold, BTC)
    idx_syms = {
        "spx": "%5EGSPC", "ndx": "%5ENDX", "dji": "%5EDJI", "rut": "%5ERUT",
        "vix": "%5EVIX", "us10y": "%5ETNX", "wti": "CL=F", "gold": "GC=F",
        "btc": "BTCUSD",
    }
    snap = data.setdefault("market_snapshot", {})
    for k, sym in idx_syms.items():
        try:
            r = requests.get(
                f"https://financialmodelingprep.com/api/v3/quote/{sym}?apikey={FMP_KEY}",
                timeout=15,
            )
            rows = r.json()
            if rows:
                snap[f"{k}_last"] = rows[0].get("price")
                snap[f"{k}_d1"] = rows[0].get("changesPercentage")
        except Exception:
            pass

    return True


# ---------------------------------------------------------------------------
# Bigdata.com updater (placeholder — fill in when you know the endpoint)
# ---------------------------------------------------------------------------
def refresh_via_bigdata(data: dict) -> bool:
    # TODO: replace with real bigdata.com REST endpoints once you have API docs.
    # This is a stub so the workflow doesn't fail.
    print("Bigdata.com refresh not implemented — falling through.")
    return False


def main():
    data = _load_current()

    ok = False
    if BIGDATA_KEY:
        ok = refresh_via_bigdata(data)
    if not ok and FMP_KEY:
        ok = refresh_via_fmp(data)

    if not ok:
        print("No API key configured — bumping timestamp only.")
        print("Add BIGDATA_API_KEY or FMP_API_KEY as repo secrets to enable refresh.")

    _save(data)
    print(f"Wrote {DATASET} ({DATASET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
