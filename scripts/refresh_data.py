#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scanner-mode refresh for dataset.json (v3 schema).

Each run:
  1. Load universe.json (~1500 tickers: S&P 1500 + NASDAQ-100)
     If missing or >30 days old, rebuild via scripts/build_universe.py.
  2. Batch-download ~1y daily OHLC from yfinance for entire universe.
  3. Compute technical indicators + dip_score (0-100) for each.
  4. Rank; take top 50.
  5. Merge hand-curated overlay from narratives_manual.json for pinned names.
  6. For new entrants, generate template Thai narrative from signals.
  7. Write dataset.json (schema-compatible with existing index.html).

No API key required - yfinance is free and quota-less (within reason).
"""
import json
import os
import sys
import time
import datetime as dt
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET      = ROOT / "dataset.json"
UNIVERSE     = ROOT / "universe.json"
OVERLAY      = ROOT / "narratives_manual.json"
BUILD_UNIV   = ROOT / "scripts" / "build_universe.py"

HISTORY_CAP  = 180      # days of sparkline history to keep per ticker
TOP_N        = 50       # how many tickers to publish
UNIVERSE_MAX_AGE_DAYS = 30

# Chunk size for yfinance batch download. Yahoo handles ~200 tickers per call well.
CHUNK_SIZE   = 200


# -------------------- universe + overlay loading --------------------

def _ensure_universe():
    """Build universe.json if missing or >30 days old."""
    need_build = False
    if not UNIVERSE.exists():
        print("universe.json missing - building it now...")
        need_build = True
    else:
        age_days = (time.time() - UNIVERSE.stat().st_mtime) / 86400
        if age_days > UNIVERSE_MAX_AGE_DAYS:
            print(f"universe.json is {age_days:.1f} days old - rebuilding...")
            need_build = True
    if need_build and BUILD_UNIV.exists():
        subprocess.run([sys.executable, str(BUILD_UNIV)], check=True)


def load_universe():
    _ensure_universe()
    with UNIVERSE.open(encoding="utf-8") as f:
        u = json.load(f)
    return u.get("tickers", [])


def load_overlay():
    if not OVERLAY.exists():
        return {}
    with OVERLAY.open(encoding="utf-8") as f:
        d = json.load(f)
    return d.get("tickers", {})


# -------------------- indicator math --------------------

def _rsi(close, period=14):
    """Wilder's RSI on pandas Series."""
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    # Wilder's smoothing (EMA with alpha=1/period)
    roll_up = up.ewm(alpha=1/period, adjust=False).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _macd(close):
    """Return (macd, signal, hist)."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist


def _macd_bullish_cross_days_ago(hist, lookback=15):
    """How many days ago did MACD histogram cross from <=0 to >0? None if no cross within lookback."""
    h = hist.dropna().tail(lookback + 1)
    if len(h) < 2:
        return None
    arr = h.values
    for i in range(len(arr) - 1, 0, -1):
        if arr[i] > 0 and arr[i-1] <= 0:
            return len(arr) - 1 - i  # 0 = yesterday
    return None


def _round(x, n=2):
    try:
        if x is None:
            return None
        import math
        if math.isnan(x) or math.isinf(x):
            return None
        return round(float(x), n)
    except Exception:
        return None


def compute_tech(df):
    """df is a DataFrame with columns Open,High,Low,Close,Volume indexed by date.
    Return dict of tech fields."""
    import pandas as pd
    if df is None or df.empty or len(df) < 30:
        return None
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df else None

    price = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else price
    change_d1 = 100 * (price - prev) / prev if prev else 0

    # 52-week window (252 trading days approx)
    window = close.tail(252) if len(close) > 252 else close
    hi52 = float(window.max())
    lo52 = float(window.min())

    # 1-month / 5-day changes
    def pct_ago(days):
        if len(close) < days + 1:
            return None
        try:
            return 100 * (price - float(close.iloc[-days-1])) / float(close.iloc[-days-1])
        except Exception:
            return None

    change_5d  = pct_ago(5)
    change_1m  = pct_ago(21)
    change_3m  = pct_ago(63)
    change_1y  = pct_ago(252)

    # RSI, MACD, EMA
    rsi = _rsi(close, 14)
    rsi_last = float(rsi.iloc[-1]) if not rsi.empty and not pd.isna(rsi.iloc[-1]) else None
    _, _, hist = _macd(close)
    macd_cross_days = _macd_bullish_cross_days_ago(hist, lookback=15)

    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else None
    ema_status = "above" if ema50 and price > ema50 else "below"

    # ATR(14)
    tr = pd.concat([
        (high - low).abs(),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else None

    # Bollinger (20,2)
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    bb_upper = float((ma20 + 2*sd20).iloc[-1]) if len(close) >= 20 else None
    bb_lower = float((ma20 - 2*sd20).iloc[-1]) if len(close) >= 20 else None
    if bb_upper and bb_lower:
        if price >= bb_upper * 0.99:  bb_status = "upper-band"
        elif price <= bb_lower * 1.01: bb_status = "lower-band"
        else:                           bb_status = "middle"
    else:
        bb_status = None

    # Support / resistance: 20-day swing low / swing high
    swing_lo = float(low.tail(20).min())
    swing_hi = float(high.tail(20).max())

    # 5d avg price
    avg_5d = float(close.tail(5).mean())

    # Volume surge (5d avg / 60d avg)
    vol_ratio = None
    if vol is not None and len(vol) >= 60:
        v5 = float(vol.tail(5).mean())
        v60 = float(vol.tail(60).mean())
        vol_ratio = v5 / v60 if v60 > 0 else None

    # History for sparkline (last 180 closes)
    hist_closes = close.tail(HISTORY_CAP).round(2).tolist()
    hist_dates  = [d.strftime("%Y-%m-%d") for d in close.tail(HISTORY_CAP).index]

    pos_52w = 100 * (price - lo52) / (hi52 - lo52) if hi52 > lo52 else None
    pullback = 100 * (price - hi52) / hi52 if hi52 else None

    trend = "uptrend" if ema50 and price > ema50 and ema20 > ema50 else \
            "downtrend" if ema50 and price < ema50 and ema20 < ema50 else "sideways"

    return {
        "price": _round(price, 2),
        "change_d1_pct": _round(change_d1, 2),
        "change_5d_pct": _round(change_5d, 2) if change_5d is not None else None,
        "change_1m_pct": _round(change_1m, 2) if change_1m is not None else None,
        "change_3m_pct": _round(change_3m, 2) if change_3m is not None else None,
        "change_1y_pct": _round(change_1y, 2) if change_1y is not None else None,
        "high_52w": _round(hi52, 2),
        "low_52w":  _round(lo52, 2),
        "pos_52w_pct": _round(pos_52w, 1),
        "pullback_from_high_pct": _round(pullback, 1),
        "tech": {
            "trend": trend,
            "rsi_14": _round(rsi_last, 1),
            "macd_signal": "bullish cross" if macd_cross_days is not None and macd_cross_days <= 10 else
                           "bullish" if hist.iloc[-1] > 0 else "bearish",
            "macd_cross_days_ago": macd_cross_days,
            "ema_20": _round(ema20, 2),
            "ema_50": _round(ema50, 2) if ema50 else None,
            "ema_status": ema_status,
            "atr_14": _round(atr14, 2) if atr14 else None,
            "bb_lower": _round(bb_lower, 2) if bb_lower else None,
            "bb_upper": _round(bb_upper, 2) if bb_upper else None,
            "bb_status": bb_status,
            "avg_5d_price": _round(avg_5d, 2),
            "support":    _round(swing_lo, 2),
            "resistance": _round(swing_hi, 2),
            "vol_ratio_5_60": _round(vol_ratio, 2) if vol_ratio else None,
        },
        "history": {"dates": hist_dates, "closes": hist_closes},
    }


def compute_dip_score(t):
    """Composite 0-100 dip score from price-based signals."""
    pts = 0
    signals = []

    # 1) Pullback from 52w high (30 pts)
    pb = t.get("pullback_from_high_pct")
    if pb is not None:
        mag = min(abs(pb), 50)
        p = (mag / 50.0) * 30
        pts += p
        if pb <= -30:      signals.append(f"DEEP PULLBACK >{int(abs(pb))}PCT")
        elif pb <= -20:    signals.append(f"PULL {int(pb)}%")
        elif pb <= -10:    signals.append(f"PULL {int(pb)}%")
        elif pb <= -5:     signals.append(f"MODEST PULLBACK")

    # 2) RSI (25 pts): RSI 25 = max, 40 = 0
    tech = t.get("tech", {})
    rsi = tech.get("rsi_14")
    if rsi is not None:
        if rsi <= 25:      pts += 25; signals.append("RSI OVERSOLD")
        elif rsi <= 30:    pts += 22; signals.append("RSI OVERSOLD")
        elif rsi <= 35:    pts += 15; signals.append("RSI OS")
        elif rsi <= 40:    pts += 8
        # else 0

    # 3) Pos in 52w range (15 pts): <10% = 15, 50% = 0
    pos = t.get("pos_52w_pct")
    if pos is not None:
        p = max(0, min(15, 15 * (1 - pos / 50.0)))
        pts += p
        if pos <= 15:      signals.append("NEAR 52W LOW")

    # 4) MACD bullish cross within 10 days (15 pts)
    cross = tech.get("macd_cross_days_ago")
    if cross is not None and cross <= 10:
        pts += 15 if cross <= 5 else 10
        signals.append("MACD BULLISH CROSS")

    # 5) Volume surge (15 pts): 5d/60d ratio > 1.5 = 15
    vr = tech.get("vol_ratio_5_60")
    if vr is not None:
        if vr >= 1.5:      pts += 15; signals.append("VOLUME SURGE")
        elif vr >= 1.2:    pts += 8

    # Near support: within 3% of 20d swing low
    price = t.get("price")
    support = tech.get("support")
    if price and support and abs(price - support) / price < 0.03:
        signals.append("NEAR SUPPORT")

    return int(round(pts)), signals[:6]


def make_entry_zone(t):
    """Heuristic entry/stop/target from price + support/resistance + ATR."""
    price = t.get("price")
    tech = t.get("tech", {})
    support = tech.get("support")
    resistance = tech.get("resistance")
    atr = tech.get("atr_14") or (price * 0.02 if price else 0)
    if not price:
        return None
    entry_low  = round(support if support and support < price else price * 0.98, 2)
    entry_high = round(min(price * 1.02, resistance * 0.98) if resistance else price * 1.02, 2)
    stop_loss  = round(max(entry_low - atr * 1.5, entry_low * 0.92), 2)
    target_1   = round(resistance if resistance and resistance > price else price * 1.08, 2)
    target_2   = round(target_1 * 1.08, 2)
    rr = round((target_1 - entry_low) / max(entry_low - stop_loss, 0.01), 2)
    return {
        "entry_low": entry_low, "entry_high": entry_high,
        "stop_loss": stop_loss,
        "target_1": target_1, "target_2": target_2,
        "risk_reward": rr,
    }


# -------------------- template narrative --------------------

def template_narrative(t):
    """Auto-generate Thai narrative from signals when no manual overlay exists."""
    sym = t.get("symbol", "?")
    price = t.get("price")
    pb = t.get("pullback_from_high_pct") or 0
    pos = t.get("pos_52w_pct") or 50
    tech = t.get("tech", {})
    rsi = tech.get("rsi_14")
    support = tech.get("support")
    macd_cross = tech.get("macd_cross_days_ago")

    headline_bits = [f"{sym} ย่อ {abs(pb):.1f}% จาก high"]
    if rsi is not None and rsi < 35:
        headline_bits.append(f"RSI {rsi:.0f} oversold")
    if macd_cross is not None and macd_cross <= 10:
        headline_bits.append("MACD bullish cross")
    headline = " — ".join(headline_bits)

    why_now = []
    if pb <= -30:
        why_now.append(f"Pullback {abs(pb):.1f}% จาก 52w high — เข้าโซน deep-value")
    elif pb <= -15:
        why_now.append(f"Pullback {abs(pb):.1f}% จาก 52w high — setup mean-reversion")
    if rsi is not None and rsi < 35:
        why_now.append(f"RSI(14) = {rsi:.1f} oversold recovery")
    if macd_cross is not None and macd_cross <= 10:
        why_now.append(f"MACD เพิ่ง bullish crossover ({macd_cross} วันก่อน)")
    if pos <= 15:
        why_now.append(f"ราคาอยู่ที่ {pos:.0f}% ของ 52w range — ใกล้ low")
    if support and price and abs(price - support) / price < 0.03:
        why_now.append(f"ราคาใกล้ support ${support:.2f}")
    if not why_now:
        why_now.append("Technical setup กำลังก่อตัว — ดูจังหวะเข้า")

    risks = [
        "Market-wide drawdown อาจลากหุ้นลงต่อ",
        "ไม่มี hand-curated fundamental analysis — เช็ค earnings + balance sheet ก่อนลงทุน",
    ]

    thesis = ""
    if support and price:
        thesis = f"เข้าโซน ${support:.2f}-${price:.2f} stop ต่ำกว่า support 5-8%"

    return {
        "headline": headline,
        "summary": f"{sym} อยู่ใน opportunity zone จาก scanner — ย่อ {abs(pb):.1f}% + signals เด่น",
        "why_now": why_now,
        "risks": risks,
        "thesis_short": thesis,
        "auto_generated": True,
    }


# -------------------- download + rank pipeline --------------------

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def fetch_universe_history(symbols):
    """Batch download ~1y OHLC for all symbols. Returns dict sym -> DataFrame."""
    import yfinance as yf
    import pandas as pd
    out = {}
    for chunk in chunked(symbols, CHUNK_SIZE):
        print(f"Downloading {len(chunk)} tickers ({list(out.keys()).__len__()}/{len(symbols)} done so far)...", flush=True)
        try:
            df = yf.download(
                chunk, period="1y", interval="1d",
                group_by="ticker", auto_adjust=False, progress=False, threads=True,
            )
        except Exception as e:
            print(f"  chunk failed: {e}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        for sym in chunk:
            try:
                if sym in df.columns.get_level_values(0):
                    sub = df[sym].dropna(how="all")
                    if not sub.empty:
                        out[sym] = sub
            except Exception:
                continue
    print(f"yfinance: got history for {len(out)}/{len(symbols)} tickers")
    return out


def main():
    now = dt.datetime.utcnow()
    print(f"--- Scanner refresh @ {now.isoformat()}Z ---")

    universe = load_universe()
    overlay  = load_overlay()
    syms = [u["symbol"] for u in universe]
    meta_by_sym = {u["symbol"]: u for u in universe}
    print(f"Universe: {len(syms)} tickers. Overlay (manual): {len(overlay)} tickers.")

    histories = fetch_universe_history(syms)
    print(f"Computing indicators for {len(histories)} tickers...")

    candidates = []
    for sym, df in histories.items():
        tech_pack = compute_tech(df)
        if tech_pack is None:
            continue
        meta = meta_by_sym.get(sym, {})
        ov = overlay.get(sym, {})

        t = {
            "symbol": sym,
            "name": ov.get("name") or meta.get("name") or sym,
            "sector": ov.get("sector") or meta.get("sector") or "Unknown",
            "industry": ov.get("industry"),
            "market_cap_b": ov.get("market_cap_b"),
            "logo_color": ov.get("logo_color") or "#888",
            # Overlay hand-curated deep fields (may be None for new entrants)
            "val":      ov.get("val"),
            "fund":     ov.get("fund"),
            "catalyst": ov.get("catalyst"),
        }
        t.update(tech_pack)   # price, tech, change_*, 52w, history
        score, signals = compute_dip_score(t)
        t["dip_score"] = score
        t["dip_signals"] = signals
        t["entry"] = make_entry_zone(t)

        # Narrative: overlay wins if present
        if ov.get("narrative_th"):
            t["narrative_th"] = ov["narrative_th"]
            t["narrative_th"]["auto_generated"] = False
        else:
            t["narrative_th"] = template_narrative(t)

        # Recompute discount/upside if we have val overlay + new price
        if t.get("val") and t.get("price"):
            fv = t["val"].get("fair_value")
            if fv:
                t["val"]["discount_to_fv_pct"] = round(100 * (fv - t["price"]) / fv, 1)
            tm = t["val"].get("analyst_target_mean")
            if tm:
                t["val"]["analyst_upside_pct"] = round(100 * (tm - t["price"]) / t["price"], 1)

        candidates.append(t)

    candidates.sort(key=lambda x: x.get("dip_score", 0), reverse=True)
    top = candidates[:TOP_N]
    print(f"Top {TOP_N} dip scores: " + ", ".join(f"{t['symbol']}={t['dip_score']}" for t in top[:10]) + " ...")

    # Summary
    scores = [t["dip_score"] for t in top]
    rsis = [t.get("tech", {}).get("rsi_14") for t in top]
    summary = {
        "total_tickers": len(top),
        "avg_dip_score": round(sum(scores) / len(scores)) if scores else 0,
        "high_conviction_count": sum(1 for s in scores if s >= 75),
        "rsi_oversold_count": sum(1 for r in rsis if r is not None and r < 35),
        "near_support_count": sum(
            1 for t in top
            if t.get("price") and t.get("tech", {}).get("support")
            and abs(t["price"] - t["tech"]["support"]) / t["price"] < 0.03
        ),
        "scanner_universe_size": len(syms),
    }
    discs = [t.get("val", {}).get("discount_to_fv_pct") for t in top if t.get("val")]
    discs = [d for d in discs if d is not None]
    if discs:
        summary["fair_value_discount_avg_pct"] = round(sum(discs) / len(discs), 1)

    data = {
        "version": 3,
        "schema": "scanner+top50",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of":       now.strftime("%Y-%m-%d %H:%M UTC"),
        "universe_label": f"S&P 1500 + NASDAQ-100 Scanner (top {TOP_N}/{len(syms)})",
        "tickers": top,
        "summary": summary,
    }

    with DATASET.open("w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"Wrote {DATASET} ({DATASET.stat().st_size} bytes) - as_of={data['as_of']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
