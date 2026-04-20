#!/usr/bin/env python3
"""
Refresh dataset.json hourly (v2 schema — buy-the-dip scanner).

Schedule (via refresh.yml):
  Hourly, Mon 08:00 Thai → Sat 08:00 Thai (UTC+7).
  Window is Mon 01:00 UTC → Sat 01:00 UTC.

Priority for price source:
  1. Bigdata.com (preferred) — needs BIGDATA_API_KEY secret
  2. FMP (Financial Modeling Prep) — needs FMP_API_KEY secret

If neither secret is set the script only bumps `generated_at` so the workflow
stays green (lets you deploy first, add API key later).

Per-run updates:
  - price, change_d1_pct, high_52w, low_52w, market_cap_b (from quote endpoint)
  - Recomputes pos_52w_pct, pullback_from_high_pct, discount_to_fv_pct, analyst_upside_pct
  - Appends today's close to history.closes / history.dates (capped at 180 days)
  - Regenerates summary (dip score avg, RSI oversold count, etc.)
"""
import json
import os
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "dataset.json"
HISTORY_CAP = 180

BIGDATA_KEY = os.environ.get("BIGDATA_API_KEY", "").strip()
FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()


def _load_current():
    if DATASET.exists():
        with DATASET.open(encoding="utf-8") as f:
            return json.load(f)
    return {"version": 2, "tickers": [], "summary": {}}


def _save(data):
    now = dt.datetime.utcnow()
    data["generated_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    data["as_of"] = now.strftime("%Y-%m-%d %H:%M UTC")
    with DATASET.open("w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def _recompute_derived(t: dict) -> None:
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


def _append_history(t: dict, today: str) -> None:
    """Append today's close once per UTC date. If today already exists, update."""
    hist = t.setdefault("history", {"dates": [], "closes": []})
    dates = hist.get("dates", [])
    closes = hist.get("closes", [])
    price = t.get("price")
    if price is None:
        return
    if dates and dates[-1] == today:
        closes[-1] = round(price, 2)
    else:
        dates.append(today)
        closes.append(round(price, 2))
    # Cap to last N days
    if len(dates) > HISTORY_CAP:
        hist["dates"] = dates[-HISTORY_CAP:]
        hist["closes"] = closes[-HISTORY_CAP:]
    else:
        hist["dates"] = dates
        hist["closes"] = closes


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

    today_utc = dt.datetime.utcnow().strftime("%Y-%m-%d")
    updated = 0
    for t in tickers:
        q = quotes.get(t["symbol"])
        if not q:
            continue
        if q.get("price") is not None:
            t["price"] = round(q["price"], 2)
            updated += 1
        if q.get("changesPercentage") is not None:
            t["change_d1_pct"] = round(q["changesPercentage"], 2)
        if q.get("yearHigh") is not None:
            t["high_52w"] = round(q["yearHigh"], 2)
        if q.get("yearLow") is not None:
            t["low_52w"] = round(q["yearLow"], 2)
        if q.get("marketCap") is not None:
            t["market_cap_b"] = round(q["marketCap"] / 1e9, 2)
        _recompute_derived(t)
        _append_history(t, today_utc)
    print(f"FMP: updated {updated}/{len(tickers)} tickers")
    return updated > 0


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
    s["high_conviction_count"] = sum(1 for sc in scores if sc >= 75)
    rsis = [t.get("tech", {}).get("rsi_14") for t in tickers]
    s["rsi_oversold_count"] = sum(1 for r in rsis if r is not None and r < 35