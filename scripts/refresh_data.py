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
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET      = ROOT / "dataset.json"
UNIVERSE     = ROOT / "universe.json"
OVERLAY      = ROOT / "narratives_manual.json"
WATCHLIST    = ROOT / "watchlist.json"
BUILD_UNIV   = ROOT / "scripts" / "build_universe.py"

HISTORY_CAP  = 180      # days of sparkline history to keep per ticker
TOP_N        = 50       # how many tickers to publish
UNIVERSE_MAX_AGE_DAYS = 30

# Chunk size for yfinance batch download. Yahoo handles ~200 tickers per call well.
CHUNK_SIZE   = 200

# Finnhub API (optional). If FINNHUB_API_KEY env var is set, top-N tickers get
# enriched with real-time quote + news + fundamentals + insider transactions.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE    = "https://finnhub.io/api/v1"
FINNHUB_NEWS_LOOKBACK_DAYS = 7
FINNHUB_INSIDER_LOOKBACK_DAYS = 30
FINNHUB_REQ_TIMEOUT = 8
FINNHUB_CALL_SLEEP = 0.12   # ~500 calls/min theoretical; we stay well under 60/min/endpoint

# Translation (EN -> TH) for news headlines/summaries. Uses deep-translator
# (free, scrapes Google Translate). Falls back to English on any error.
# Set TRANSLATE_NEWS=0 to disable even if lib is installed.
TRANSLATE_NEWS = os.environ.get("TRANSLATE_NEWS", "1").strip() != "0"
_TRANSLATE_CACHE = {}            # {original_text: thai_text} in-memory per run
_TRANSLATE_FAILURES = [0]        # mutable counter; stop translation after too many errors
_TRANSLATE_MAX_FAILURES = 5      # after N consecutive failures, skip rest of run
_GoogleTranslator = None         # lazily imported


def _translate_th(text, max_chars=4500):
    """Translate English -> Thai via deep-translator (Google Translate).
    Returns original text on any failure. Caches results in-memory within one run.
    Same headline across multiple tickers is translated only once."""
    if not TRANSLATE_NEWS:
        return text
    if not text or not isinstance(text, str):
        return text
    t = text.strip()
    if not t:
        return text
    if t in _TRANSLATE_CACHE:
        return _TRANSLATE_CACHE[t]
    if _TRANSLATE_FAILURES[0] >= _TRANSLATE_MAX_FAILURES:
        return text
    global _GoogleTranslator
    if _GoogleTranslator is None:
        try:
            from deep_translator import GoogleTranslator as _GT
            _GoogleTranslator = _GT
        except Exception as e:
            print(f"[translate] deep-translator not available ({e}); news stays in English")
            _TRANSLATE_FAILURES[0] = _TRANSLATE_MAX_FAILURES
            return text
    src = t[:max_chars]
    try:
        result = _GoogleTranslator(source="auto", target="th").translate(src)
        if result and isinstance(result, str) and result.strip():
            _TRANSLATE_CACHE[t] = result
            _TRANSLATE_FAILURES[0] = 0
            return result
        _TRANSLATE_FAILURES[0] += 1
        return text
    except Exception as e:
        _TRANSLATE_FAILURES[0] += 1
        if _TRANSLATE_FAILURES[0] == 1 or _TRANSLATE_FAILURES[0] == _TRANSLATE_MAX_FAILURES:
            print(f"[translate] failure {_TRANSLATE_FAILURES[0]}/{_TRANSLATE_MAX_FAILURES}: {e}")
        return text



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


def load_watchlist():
    """Force-include symbols that may not be in index universe (e.g. small caps,
    emerging themes). Each entry: {symbol, name, sector, industry?, notes?}.
    Returns [] if watchlist.json missing."""
    if not WATCHLIST.exists():
        return []
    try:
        with WATCHLIST.open(encoding="utf-8") as f:
            d = json.load(f)
        return d.get("tickers", [])
    except Exception as e:
        print(f"WARN: failed to load watchlist: {e}")
        return []


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


def _pct_slope(series, days):
    """Percent change of a pandas Series over N trading days ago. None if insufficient."""
    try:
        if len(series) < days + 1:
            return None
        v0 = float(series.iloc[-days-1])
        v1 = float(series.iloc[-1])
        if v0 == 0:
            return None
        return 100 * (v1 - v0) / v0
    except Exception:
        return None


def compute_momentum_score(df, tech_pack):
    """Momentum composite 0-35 from trend + RSI-slope + short/mid return + structure + vol.

    Measures whether price is *starting to turn up*, not whether it has
    already rallied. Designed to pair with dip_score so we filter out
    falling-knife setups (dip is deep but price still sliding).

    Signals (sum to ~35 pts):
      1. Price > EMA20          (5 pts)   — short-term trend flipped up
      2. EMA20 > EMA50          (5 pts)   — bullish stack (short over mid)
      3. RSI rising from OS     (7 pts)   — was oversold recently, now recovering
      4. 5-day return > 0       (5 pts)   — short-term momentum positive
      5. Higher low structure   (5 pts)   — last 5d low > prior 10d low
      6. Accumulation volume    (4 pts)   — up-day vol > down-day vol (last 10d)
      7. Relative strength      (4 pts)   — 20d return > SPY 20d (placeholder: >0 bonus)
    """
    if df is None or tech_pack is None:
        return 0, []

    try:
        import pandas as pd
    except Exception:
        return 0, []

    close = df["Close"].astype(float) if "Close" in df else None
    low   = df["Low"].astype(float)   if "Low"   in df else None
    vol   = df["Volume"].astype(float) if "Volume" in df else None
    if close is None or len(close) < 30:
        return 0, []

    pts = 0
    signals = []
    tech = tech_pack.get("tech", {}) or {}
    price = tech_pack.get("price")
    ema20 = tech.get("ema_20")
    ema50 = tech.get("ema_50")

    # 1) Price > EMA20
    if price is not None and ema20 is not None and price > ema20:
        pts += 5
        signals.append("PX>EMA20")

    # 2) EMA20 > EMA50 (bullish stack)
    if ema20 is not None and ema50 is not None and ema20 > ema50:
        pts += 5
        signals.append("EMA STACK")

    # 3) RSI rising from oversold: RSI(now) > RSI(3d ago) AND RSI touched <=40 in last 10d
    try:
        rsi_series = _rsi(close, 14).dropna()
        if len(rsi_series) >= 5:
            rsi_now = float(rsi_series.iloc[-1])
            rsi_3ago = float(rsi_series.iloc[-4])
            rsi_10min = float(rsi_series.tail(10).min())
            if rsi_now > rsi_3ago and rsi_10min <= 40:
                pts += 7
                signals.append("RSI TURNING UP")
            elif rsi_now > rsi_3ago and rsi_10min <= 50:
                pts += 4  # partial credit — not quite OS but recovering
    except Exception:
        pass

    # 4) 5-day return > 0
    r5 = _pct_slope(close, 5)
    if r5 is not None and r5 > 0:
        pts += 5
        if r5 >= 3:
            signals.append(f"5D +{r5:.1f}%")

    # 5) Higher low structure: min(last 5) > min(prior 10)
    try:
        if low is not None and len(low) >= 20:
            recent_lo = float(low.tail(5).min())
            prior_lo  = float(low.iloc[-15:-5].min())
            if recent_lo > prior_lo:
                pts += 5
                signals.append("HIGHER LOW")
    except Exception:
        pass

    # 6) Accumulation: volume on up-days > volume on down-days (last 10d)
    try:
        if vol is not None and len(vol) >= 11:
            cs = close.tail(11)
            vs = vol.tail(10)
            up_vol = 0.0; dn_vol = 0.0
            for i in range(10):
                # cs[i+1] vs cs[i]: close today vs yesterday
                if float(cs.iloc[i+1]) > float(cs.iloc[i]):
                    up_vol += float(vs.iloc[i])
                elif float(cs.iloc[i+1]) < float(cs.iloc[i]):
                    dn_vol += float(vs.iloc[i])
            if up_vol > dn_vol * 1.2:
                pts += 4
                signals.append("ACCUMULATION")
    except Exception:
        pass

    # 7) Relative strength: 20d return positive (simple proxy — full RS vs SPY needs SPY history)
    r20 = _pct_slope(close, 20)
    if r20 is not None and r20 > 0:
        pts += 4
        if r20 >= 5:
            signals.append(f"20D +{r20:.1f}%")

    return int(round(pts)), signals[:6]


def rotation_flag(dip, mom):
    """Tag the ticker so the UI can colour-code setup quality.

    - setup_ready   : dip>=50 AND momentum>=15  → both signals align, actionable
    - wait_confirm  : dip>=50 AND momentum<15   → oversold but no reversal yet
    - falling_knife : dip>=65 AND momentum<5    → deep dip + still sliding = avoid
    - meh           : everything else
    """
    d = dip or 0
    m = mom or 0
    if d >= 65 and m < 5:
        return "falling_knife"
    if d >= 50 and m >= 15:
        return "setup_ready"
    if d >= 50 and m < 15:
        return "wait_confirm"
    return "meh"


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


def star_from_dip(score):
    """Auto-derive star rating (1-5) from dip_score when no manual overlay exists.
    Calibration: mirror how the hand-curated overlays distribute ratings."""
    if score is None:
        return 1
    if score >= 80: return 5
    if score >= 65: return 4
    if score >= 50: return 3
    if score >= 35: return 2
    return 1


# -------------------- Finnhub API (optional) --------------------

def _finnhub_get(path, params):
    """GET https://finnhub.io/api/v1{path}?...&token=KEY; return parsed JSON or None."""
    if not FINNHUB_API_KEY:
        return None
    p = dict(params or {})
    p["token"] = FINNHUB_API_KEY
    qs = urllib.parse.urlencode(p)
    url = f"{FINNHUB_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "stock-autopilot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=FINNHUB_REQ_TIMEOUT) as r:
            raw = r.read()
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # rate limited - back off 1s and retry once
            time.sleep(1.0)
            try:
                with urllib.request.urlopen(req, timeout=FINNHUB_REQ_TIMEOUT) as r:
                    raw = r.read()
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return None
        return None
    except Exception:
        return None
    finally:
        time.sleep(FINNHUB_CALL_SLEEP)


def fetch_finnhub_bundle(sym):
    """Gather quote + news + fundamentals + insider + analyst recs for one symbol.
    Returns dict of available fields (missing endpoints silently dropped)."""
    out = {}
    today = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).date()
    to_d = today.strftime("%Y-%m-%d")

    # 1) Real-time quote
    q = _finnhub_get("/quote", {"symbol": sym})
    if q and q.get("c"):
        pc = q.get("pc") or 0
        c = q.get("c") or 0
        out["quote_rt"] = {
            "price": _round(c, 2),
            "high":  _round(q.get("h"), 2),
            "low":   _round(q.get("l"), 2),
            "open":  _round(q.get("o"), 2),
            "prev_close": _round(pc, 2),
            "change_pct": _round(100 * (c - pc) / pc, 2) if pc else None,
            "ts": q.get("t"),
        }

    # 2) News (last 7 days)
    since_n = (today - dt.timedelta(days=FINNHUB_NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    n = _finnhub_get("/company-news", {"symbol": sym, "from": since_n, "to": to_d})
    if isinstance(n, list):
        news = []
        for it in n[:5]:
            hl = (it.get("headline") or "").strip()
            if not hl:
                continue
            summary_en = (it.get("summary") or "")[:240]
            # Translate to Thai (cached in-memory, falls back to English on error)
            hl_th = _translate_th(hl)
            sum_th = _translate_th(summary_en) if summary_en else ""
            news.append({
                "headline": hl,
                "headline_th": hl_th if hl_th and hl_th != hl else "",
                "url": it.get("url"),
                "source": it.get("source"),
                "datetime": it.get("datetime"),
                "summary": summary_en,
                "summary_th": sum_th if sum_th and sum_th != summary_en else "",
                "image": it.get("image") or "",
            })
        if news:
            out["news"] = news

    # 3) Fundamentals (snapshot metrics)
    m = _finnhub_get("/stock/metric", {"symbol": sym, "metric": "all"})
    if m and isinstance(m, dict) and m.get("metric"):
        mm = m["metric"]
        def _g(*keys):
            for k in keys:
                v = mm.get(k)
                if v is not None:
                    try:
                        return _round(float(v), 2)
                    except Exception:
                        return None
            return None
        out["fund_rt"] = {
            "pe_ttm":        _g("peTTM", "peBasicExclExtraTTM", "peNormalizedAnnual"),
            "pe_forward":    _g("forwardPE"),
            "eps_ttm":       _g("epsBasicExclExtraItemsTTM", "epsTTM"),
            "rev_growth_yoy_pct": _g("revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"),
            "profit_margin_pct":  _g("netProfitMarginTTM"),
            "roe_ttm_pct":        _g("roeTTM"),
            "debt_to_equity":     _g("totalDebt/totalEquityQuarterly"),
            "dividend_yield_pct": _g("dividendYieldIndicatedAnnual"),
            "52w_high":      _g("52WeekHigh"),
            "52w_low":       _g("52WeekLow"),
            "beta":          _g("beta"),
        }

    # 4) Insider transactions (last 30 days)
    since_i = (today - dt.timedelta(days=FINNHUB_INSIDER_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    ins = _finnhub_get("/stock/insider-transactions",
                       {"symbol": sym, "from": since_i, "to": to_d})
    if ins and isinstance(ins, dict) and ins.get("data"):
        rows = ins["data"][:15]
        buys = sum(1 for r in rows if (r.get("change") or 0) > 0)
        sells = sum(1 for r in rows if (r.get("change") or 0) < 0)
        net = sum((r.get("change") or 0) for r in rows)
        out["insider"] = {
            "window_days": FINNHUB_INSIDER_LOOKBACK_DAYS,
            "count": len(rows),
            "buys": buys,
            "sells": sells,
            "net_shares": int(net),
            "top": [{
                "name": r.get("name"),
                "share_change": r.get("change"),
                "date": r.get("transactionDate"),
                "code": r.get("transactionCode"),
                "price": r.get("transactionPrice"),
            } for r in rows[:5]],
        }

    # 5) Analyst recommendations (latest month)
    rec = _finnhub_get("/stock/recommendation", {"symbol": sym})
    if isinstance(rec, list) and rec:
        latest = rec[0]
        out["analyst_rec"] = {
            "period": latest.get("period"),
            "strong_buy": latest.get("strongBuy"),
            "buy":         latest.get("buy"),
            "hold":        latest.get("hold"),
            "sell":        latest.get("sell"),
            "strong_sell": latest.get("strongSell"),
        }

    return out if out else None


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
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    print(f"--- Scanner refresh @ {now.isoformat()}Z ---")

    universe  = load_universe()
    watchlist = load_watchlist()
    overlay   = load_overlay()

    # Merge watchlist: force-include symbols not already in index universe.
    existing_syms = {u.get("symbol") for u in universe}
    added_watch = 0
    for w in watchlist:
        s = (w.get("symbol") or "").strip().upper()
        if not s:
            continue
        if s in existing_syms:
            # Already in index; just tag so meta knows it's pinned
            for u in universe:
                if u.get("symbol") == s:
                    u["on_watchlist"] = True
                    u["watchlist_notes"] = w.get("notes")
                    break
        else:
            universe.append({
                "symbol":   s,
                "name":     w.get("name") or s,
                "sector":   w.get("sector") or "Unknown",
                "industry": w.get("industry"),
                "source":   "watchlist",
                "on_watchlist":   True,
                "watchlist_notes": w.get("notes"),
            })
            existing_syms.add(s)
            added_watch += 1

    syms = [u["symbol"] for u in universe]
    meta_by_sym = {u["symbol"]: u for u in universe}
    print(f"Universe: {len(syms)} tickers "
          f"(index {len(syms) - added_watch} + watchlist +{added_watch}). "
          f"Overlay (manual): {len(overlay)} tickers.")

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
            "industry": ov.get("industry") or meta.get("industry"),
            "market_cap_b": ov.get("market_cap_b"),
            "logo_color": ov.get("logo_color") or "#888",
            # Overlay hand-curated deep fields (may be None for new entrants)
            "val":      ov.get("val"),
            "fund":     ov.get("fund"),
            "catalyst": ov.get("catalyst"),
            "on_watchlist": bool(meta.get("on_watchlist")),
        }
        t.update(tech_pack)   # price, tech, change_*, 52w, history
        score, signals = compute_dip_score(t)
        t["dip_score"] = score
        t["dip_signals"] = signals

        # Momentum score 0-35 — is price starting to turn up? Filters knives.
        mom_score, mom_signals = compute_momentum_score(df, tech_pack)
        t["momentum_score"] = mom_score
        t["momentum_signals"] = mom_signals
        t["rotation_flag"]    = rotation_flag(score, mom_score)

        t["entry"] = make_entry_zone(t)

        # Star rating: manual overlay wins; else auto-derive from dip_score so
        # every top-N ticker has stars (not just hand-curated ones).
        val_existing = dict(t.get("val") or {})
        if val_existing.get("star_rating") is None:
            val_existing["star_rating"]   = star_from_dip(score)
            val_existing["rating_source"] = "auto_dip"
        else:
            val_existing.setdefault("rating_source", "manual")
        t["val"] = val_existing

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

    # Sort by dip_score first, then drop "falling knife" candidates:
    # dip is deep but momentum is near zero → still sliding. Except watchlist,
    # which is always force-included.
    candidates.sort(key=lambda x: x.get("dip_score", 0), reverse=True)
    MOMENTUM_MIN = 10
    filtered = [
        c for c in candidates
        if c.get("on_watchlist") or (c.get("momentum_score", 0) >= MOMENTUM_MIN)
    ]
    dropped = len(candidates) - len(filtered)
    top = filtered[:TOP_N]
    print(f"Momentum filter: dropped {dropped} tickers with momentum<{MOMENTUM_MIN} "
          f"(likely falling knives)")

    # Force-include watchlist symbols even if outside top-N (append, keep sort).
    top_syms = {t["symbol"] for t in top}
    forced = [c for c in candidates if c.get("on_watchlist") and c["symbol"] not in top_syms]
    if forced:
        print(f"Force-including {len(forced)} watchlist symbols "
              f"({', '.join(c['symbol'] for c in forced)}) below top-{TOP_N}")
        top.extend(forced)

    # ---- Change tracking: compare vs previous dataset for badges (new / rank_up / rank_down) ----
    prev_rank = {}
    if DATASET.exists():
        try:
            prev_data = json.loads(DATASET.read_text(encoding="utf-8"))
            for i, pt in enumerate(prev_data.get("tickers", [])):
                if pt.get("symbol"):
                    prev_rank[pt["symbol"]] = i + 1   # 1-indexed rank
        except Exception as e:
            print(f"WARN: failed to load prev dataset for change tracking: {e}")

    for i, t in enumerate(top):
        new_rank = i + 1
        sym = t["symbol"]
        old = prev_rank.get(sym)
        if old is None:
            t["delta"] = {"status": "new", "prev_rank": None, "rank_change": None}
        else:
            change = old - new_rank  # positive = moved up
            if change >= 3:
                status = "rank_up"
            elif change <= -3:
                status = "rank_down"
            else:
                status = "stable"
            t["delta"] = {"status": status, "prev_rank": old, "rank_change": change}

    print(f"Top dip scores: " + ", ".join(f"{t['symbol']}={t['dip_score']}" for t in top[:10]) + " ...")

    # ---- Optional Finnhub enrichment (news + insider + quote + fundamentals) ----
    if FINNHUB_API_KEY:
        print(f"Finnhub: enriching {len(top)} tickers (this takes ~{len(top)*5//10}s at ~2-3 calls/sec)...")
        enriched = 0
        for t in top:
            bundle = fetch_finnhub_bundle(t["symbol"])
            if not bundle:
                continue
            # Real-time quote: only override if yfinance missed or is stale (weekend)
            q_rt = bundle.get("quote_rt")
            if q_rt and q_rt.get("price"):
                t["quote_rt"] = q_rt
            if bundle.get("news"):
                t["news"] = bundle["news"]
            if bundle.get("insider"):
                t["insider"] = bundle["insider"]
            if bundle.get("analyst_rec"):
                t["analyst_rec"] = bundle["analyst_rec"]
            # Fundamentals: merge into fund if manual overlay missing fields
            f_rt = bundle.get("fund_rt")
            if f_rt:
                fund_merged = dict(t.get("fund") or {})
                for k, v in f_rt.items():
                    if v is not None and fund_merged.get(k) is None:
                        fund_merged[k] = v
                t["fund"] = fund_merged
                t.setdefault("fund_rt", f_rt)
                # Also push analyst target to val if finnhub has it
                if f_rt.get("pe_forward") and t["val"].get("pe_forward") is None:
                    t["val"]["pe_forward"] = f_rt["pe_forward"]
            enriched += 1
        print(f"Finnhub: enriched {enriched}/{len(top)} tickers with news/insider/fundamentals")
    else:
        print("Finnhub: skipped (no API key). Set FINNHUB_API_KEY env var to enable.")

    # Summary
    scores = [t["dip_score"] for t in top]
    moms   = [t.get("momentum_score", 0) for t in top]
    rsis = [t.get("tech", {}).get("rsi_14") for t in top]
    summary = {
        "total_tickers": len(top),
        "avg_dip_score": round(sum(scores) / len(scores)) if scores else 0,
        "avg_momentum_score": round(sum(moms) / len(moms)) if moms else 0,
        "high_conviction_count": sum(1 for s in scores if s >= 75),
        "setup_ready_count":   sum(1 for t in top if t.get("rotation_flag") == "setup_ready"),
        "wait_confirm_count":  sum(1 for t in top if t.get("rotation_flag") == "wait_confirm"),
        "rsi_oversold_count": sum(1 for r in rsis if r is not None and r < 35),
        "near_support_count": sum(
            1 for t in top
            if t.get("price") and t.get("tech", {}).get("support")
            and abs(t["price"] - t["tech"]["support"]) / t["price"] < 0.03
        ),
        "new_entries_count":   sum(1 for t in top if t.get("delta", {}).get("status") == "new"),
        "scanner_universe_size": len(syms),
        "watchlist_count":       added_watch,
        "finnhub_enabled":       bool(FINNHUB_API_KEY),
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
