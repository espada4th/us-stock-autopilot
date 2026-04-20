#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh dataset.json hourly (v2 schema - buy-the-dip scanner).

Schedule (via refresh.yml):
  Hourly, Mon 08:00 Thai -> Sat 08:00 Thai (UTC+7).
  Window is Mon 01:00 UTC -> Sat 01:00 UTC.

Priority for price source:
  1. Bigdata.com (preferred) - needs BIGDATA_API_KEY secret
  2. FMP (Financial Modeling Prep) - needs FMP_API_KEY secret

If neither secret is set the script only bumps generated_at so the workflow
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


def _recompute_derived(t):
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


def _append_history(t, today):
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
    if len(dates) > HISTORY_CAP:
        hist["dates"] = dates[-HISTORY_CAP:]
        hist["closes"] = closes[-HISTORY_CAP:]
    else:
        hist["dates"] = dates
        hist["closes"] = closes


def refresh_via_yfinance(data):
    """Primary source: yfinance (free, no key, no quota)."""
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed - skipping.")
        return False

    tickers = data.get("tickers", [])
    syms = [t["symbol"] for t in tickers if t.get("symbol")]
    if not syms:
        return False

    today_utc = dt.datetime.utcnow().strftime("%Y-%m-%d")
    updated = 0
    try:
        # Batch download last 2 days of daily OHLC for all tickers at once
        hist_df = yf.download(
            syms, period="5d", interval="1d",
            group_by="ticker", auto_adjust=False, progress=False, threads=True,
        )
    except Exception as e:
        print("yfinance batch download failed: {}".format(e), file=sys.stderr)
        return False

    for t in tickers:
        sym = t["symbol"]
        try:
            # hist_df is a MultiIndex DataFrame: (ticker, field)
            df_sym = hist_df[sym].dropna() if sym in hist_df.columns.get_level_values(0) else None
            if df_sym is None or df_sym.empty:
                continue
            last = df_sym.iloc[-1]
            prev = df_sym.iloc[-2] if len(df_sym) >= 2 else last
            close = float(last["Close"])
            prev_close = float(prev["Close"])
            t["price"] = round(close, 2)
            if prev_close:
                t["change_d1_pct"] = round(100 * (close - prev_close) / prev_close, 2)
            _recompute_derived(t)
            _append_history(t, today_utc)
            updated += 1
        except Exception as e:
            print("yfinance: skip {}: {}".format(sym, e), file=sys.stderr)
            continue

    print("yfinance: updated {}/{} tickers".format(updated, len(tickers)))
    return updated > 0


def refresh_via_fmp(data):
    """Fallback: FMP stable API (requires FMP_API_KEY)."""
    import requests
    tickers = data.get("tickers", [])
    syms = ",".join(t["symbol"] for t in tickers if t.get("symbol"))
    if not syms:
        return False

    # FMP free tier: /api/v3/quote/{batch} now returns 403.
    # Use /stable/quote-short (free) with batch in symbol= param.
    url = "https://financialmodelingprep.com/stable/batch-quote?symbols={}&apikey={}".format(syms, FMP_KEY)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        quotes = {q["symbol"]: q for q in r.json()}
    except Exception as e:
        print("FMP fetch failed: {}".format(e), file=sys.stderr)
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
    print("FMP: updated {}/{} tickers".format(updated, len(tickers)))
    return updated > 0


def refresh_via_bigdata(data):
    print("Bigdata.com refresh not implemented - falling through.")
    return False


def _regen_summary(data):
    tickers = data.get("tickers", [])
    if not tickers:
        return
    s = data.setdefault("summary", {})
    s["total_tickers"] = len(tickers)
    scores = [t.get("dip_score", 0) for t in tickers]
    s["avg_dip_score"] = round(sum(scores) / len(scores)) if scores else 0
    s["high_conviction_count"] = sum(1 for sc in scores if sc >= 75)
    rsis = [t.get("tech", {}).get("rsi_14") for t in tickers]
    s["rsi_oversold_count"] = sum(1 for r in rsis if r is not None and r < 35)
    s["near_support_count"] = sum(
        1 for t in tickers
        if t.get("price") and t.get("tech", {}).get("support")
        and abs(t["price"] - t["tech"]["support"]) / t["price"] < 0.03
    )
    discounts = [t.get("val", {}).get("discount_to_fv_pct") for t in tickers]
    discounts = [d for d in discounts if d is not None]
    if discounts:
        s["fair_value_discount_avg_pct"] = round(sum(discounts) / len(discounts), 1)


def main():
    data = _load_current()

    # Priority: yfinance (free, no key) -> FMP (needs key) -> Bigdata (TODO)
    ok = refresh_via_yfinance(data)
    if not ok and FMP_KEY:
        print("yfinance failed, trying FMP fallback...")
        ok = refresh_via_fmp(data)
    if not ok and BIGDATA_KEY:
        ok = refresh_via_bigdata(data)

    if not ok:
        print("All price sources failed - bumping timestamp only.")
    else:
        _regen_summary(data)

    _save(data)
    print("Wrote {} ({} bytes) - as_of={}".format(DATASET, DATASET.stat().st_size, data.get("as_of")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
