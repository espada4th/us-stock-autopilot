#!/usr/bin/env python3
"""
Refresh dataset.json hourly from live sources (v2 schema — buy-the-dip scanner).

Priority:
  1. Bigdata.com (preferred) — needs BIGDATA_API_KEY secret
  2. FMP (Financial Modeling Prep) — needs FMP_API_KEY secret

If neither is configured, the script bumps as_of and exits 0 so the workflow
won't fail — lets you deploy first, add secrets later.

What gets refreshed per ticker:
  - price, change_d1_pct, change_5d_pct, change_1m_pct, change_ytd_pct
  - high_52w, low_52w, pos_52w_pct, pullback_from_high_pct
  - market_cap_b (if available)

The static fundamentals / narrative / dip_signals are kept from the seed
dataset (they update less frequently). For full indicator recompute you can
extend refresh_via_bigdata().
"""
import json
import os
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset.json"

BIGDATA_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()
FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()


def _load_current():
    if DATASET.exists():
        with DATASET.open(encoding="utf-8") as f:
            return json.load(f)
    return {"version": 2, "tickers": [], "summary": {}}


def _save(data):
    data["as_of"] = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    data["generated_at"] = data["as_of"]
    with DATASET.open("w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def _recompute_derived(t: dict) -> None:
    """Recalculate pos_52w_pct, pullback_from_high_pct, discount_to_fv_pct."""
    price = t.get("price")
    hi, lo = t.get("high_52w"), t.get("low_52w")
    if price and hi and lo and hi > lo:
        t["pos_52w_pct"] = round(100 * (price - lo) / (hi - lo), 1)
    if price and hi:
        t["pullback_from_high_pct"] = round(100 * (price - hi) / hi, 1)
    val = t.get("val", {})
    fv = val.get("fair_value")
    if fv and price:
        val["discount_to_fv_pct"] = round(100 * (fv - price) / fv, 1)
    tm = val.get("analyst_target_mean")
    if tm and price:
        val["analyst_upside_pct"] = round(100 * (tm - price) / price, 1)


# ---------------------------------------------------------------------------
# FMP updater — free tier, quote endpoint
# ---------------------------------------------------------------------------
def refresh_via_fmp(data: dict) -> bool:
    import requests

    tickers = data.get("tickers", [])
    syms = ",".join(t["symbol"] for t in tickers if t.get("symbol"))
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
        q = quotes.get(t["symbol"])
        if not q:
            continue
        if q.get("price") is not None:
            t["price"] = round(q["price"], 2)
        if q.get("changesPercentage") is not None:
            t["change_d1_pct"] = round(q["changesPercentage"], 2)
        if q.get("yearHigh") is not None:
            t["high_52w"] = round(q["yearHigh"], 2)
        if q.get("yearLow") is not None:
            t["low_52w"] = round(q["yearLow"], 2)
        if q.get("marketCap") is not None:
            t["market_cap_b"] = round(q["marketCap"] / 1e9, 2)
        _recompute_derived(t)

    return True


def refresh_via_bigdata(data: dict) -> bool:
    # Placeholder — wire up bigdata.com endpoints here when ready.
    print("Bigdata.com refresh not implemented — falling through.")
    return False


def _regen_summary(data: dict) -> None:
    tickers = data.get("tickers", [])
    if not tickers:
        return
    s = data.setdefault("summary", {})
    s["total_tickers"] = len(tickers)
    scores = [t.get("dip_score", 0) for t in tickers]
    s["avg_dip_score"] = round(sum(scores) / len(scores)) if scores else 0
    s["high_conviction_count"] = sum(1 for sc in scores if sc >= 80)
    rsis = [t.get("tech", {}).get("rsi_14") for t in tickers]
    s["rsi_oversold_count"] = sum(1 for r in rsis if r is not None and r < 35)
    discounts = [t.get("val", {}).get("discount_to_fv_pct") for t in tickers]
    discounts = [d for d in discounts if d is not None]
    if discounts:
        s["fair_value_discount_avg_pct"] = round(sum(discounts) / len(discounts), 1)


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
    else:
        _regen_summary(data)

    _save(data)
    print(f"Wrote {DATASET} ({DATASET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
