"""
╔══════════════════════════════════════════════════════════════════════╗
║          BZU PROFESSIONAL SIGNAL BOT  — VERSION 3.0                 ║
║          Complete rewrite. Professional trading architecture.        ║
╠══════════════════════════════════════════════════════════════════════╣
║  ARCHITECTURE LAYERS (executed in order):                           ║
║                                                                      ║
║  L0  Environment / Config                                            ║
║  L1  Market Data  (OKX REST + fallback)                              ║
║  L2  Core Indicators  (EMA, RSI, ATR, VWAP, BBands)                 ║
║  L3  Market Regime  (Trend / Range / Volatile / Breakout)            ║
║  L4  Multi-TF Trend Alignment  (3m / 15m / 1h)                      ║
║  L5  Price Structure  (Swing HH/HL/LH/LL, BOS, CHoCH, FVG)         ║
║  L6  Liquidity Analysis  (sweeps, imbalances, stop clusters)         ║
║  L7  Order Flow  (delta, book imbalance, large prints)               ║
║  L8  Fundamental Filter  (news decay, calendar blackout)             ║
║  L9  Entry Engine  (confluence gate → precise entry zone)            ║
║  L10 Trade Plan Builder  (entry / stop / TP1-3 with R:R check)       ║
║  L11 Active Trade Manager  (trailing, partial exit, exit logic)      ║
║  L12 Message Composer  (Telegram HTML)                               ║
║  L13 Persistence  (state, journal, atomic writes)                    ║
╚══════════════════════════════════════════════════════════════════════╝

PROFESSIONAL RULES EMBEDDED IN CODE:
  • Entry only when REGIME is not CHOPPY and TF alignment >= 2/3
  • Stop always behind a structural level (swing / FVG edge), never arbitrary ATR multiple
  • Minimum R:R 2.0 for TP1; 3.0 for TP2; 4.5 for TP3
  • Late-chase detection: if price > 1.5× ATR from EMA20 → no entry, wait retest
  • News blackout: if high-impact event within 45 min → quality cap 50
  • Trailing stop: moves to break-even after TP1, trails 50% of swing after TP2
  • No new entry in same direction if previous same-direction trade closed at stop
"""

import html
import json
import math
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from statistics import mean, stdev
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests


# ══════════════════════════════════════════════════════════════════════
# L0 — ENVIRONMENT / CONFIG
# ══════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OKX_INST_ID  = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
STATE_FILE   = os.getenv("SIGNAL_MEMORY_FILE",  os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "last_signal.json"))
JOURNAL_FILE = os.getenv("SIGNAL_JOURNAL_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "signal_journal.json"))

LEVERAGE           = float(os.getenv("POSITION_LEVERAGE",   "10")  or 10)
ENTRY_QUALITY_MIN  = int(os.getenv("ENTRY_QUALITY_MIN",     "72")  or 72)   # strict: all gates must pass
RISKY_QUALITY_MIN  = int(os.getenv("RISKY_QUALITY_MIN",     "64")  or 64)   # early entry: most gates pass
MAX_HISTORY        = int(os.getenv("SIGNAL_HISTORY_LIMIT",  "80")  or 80)
MAX_JOURNAL        = int(os.getenv("SIGNAL_JOURNAL_LIMIT",  "500") or 500)
REQUEST_TIMEOUT    = int(os.getenv("REQUEST_TIMEOUT",       "12")  or 12)

# Risk / reward thresholds — do NOT loosen these
MAX_STOP_PCT   = float(os.getenv("MAX_STOP_DISTANCE_PCT",  "2.20") or 2.20)
MIN_TP1_RR     = float(os.getenv("MIN_TP1_RR",  "2.0") or 2.0)   # minimum reward-to-risk at TP1
MIN_TP2_RR     = float(os.getenv("MIN_TP2_RR",  "3.0") or 3.0)
MIN_TP3_RR     = float(os.getenv("MIN_TP3_RR",  "4.5") or 4.5)

# Calendar
IMPORTANT_EVENT_WINDOW_MINUTES = int(os.getenv("IMPORTANT_EVENT_WINDOW_MINUTES", "45") or 45)
MANUAL_IMPORTANT_EVENTS        = os.getenv("IMPORTANT_EVENTS", "")

EIA_WPSR_URL          = "https://www.eia.gov/petroleum/supply/weekly/index.php"
EIA_WPSR_SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# ── News keyword tables ────────────────────────────────────────────────
BULLISH_PHRASES = [
    "inventory draw", "crude draw", "stockpiles fell", "stockpiles decline",
    "supply disruption", "supply risk", "hormuz", "new sanctions", "fresh sanctions",
    "opec cut", "production cut", "output cut", "attack on", "strike on",
    "war escalat", "talks collapse", "talks fail", "запаси впали",
    "скорочення запасів", "нові санкції", "атака", "удар", "ормуз",
    "перебої постачання", "pipeline disruption", "force majeure",
]
BEARISH_PHRASES = [
    "inventory build", "crude build", "stockpiles rose", "stockpiles rise",
    "ceasefire", "cease-fire", "truce", "peace deal", "talks progress",
    "sanctions relief", "sanctions lifted", "output increase", "production increase",
    "opec increase", "demand weak", "oversupply", "recession fear", "demand destruction",
    "запаси зросли", "припинення вогню", "перемир", "мирна угода",
    "послаблення санкцій", "збільшення видобутку", "слабкий попит",
    "recessionary", "demand slowdown",
]
HIGH_IMPACT_SOURCES = ["reuters", "bloomberg", "ap ", "associated press", "wsj ", "ft ", "financial times"]
HIGH_IMPACT_TERMS   = [
    "eia", "api", "opec", "opec+", "fed", "powell", "fomc", "cpi",
    "iran", "hormuz", "sanctions", "inventory", "stockpiles",
    "brent", "crude", "tariff", "trump", "nato",
]

NEWS_QUERIES = [
    "Brent crude oil Reuters OPEC EIA inventories",
    "oil prices Brent crude today energy",
    "EIA crude oil inventories API crude draw build",
    "OPEC production cut increase crude oil",
    "Iran sanctions Hormuz oil supply",
    "Fed Powell dollar yields oil",
]


# ══════════════════════════════════════════════════════════════════════
# SHARED DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    ts:     int
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float = 0.0


@dataclass
class SwingPoint:
    idx:   int
    price: float
    ts:    int
    kind:  str   # "HH" | "LH" | "HL" | "LL"


@dataclass
class TradePlan:
    entry:       float
    stop:        float
    tp1:         float
    tp2:         float
    tp3:         float
    risk_pct:    float
    rr1:         float
    rr2:         float
    rr3:         float
    stop_reason: str   # "structural" | "atr-based"
    tp_method:   str   # "structural" | "rr-based"


@dataclass
class ActiveTrade:
    id:              str
    side:            str
    opened_at:       str
    entry:           float
    stop_initial:    float
    stop_current:    float
    tp1:             float
    tp2:             float
    tp3:             float
    quality:         int
    status:          str  = "OPEN"
    tp1_hit:         bool = False
    tp2_hit:         bool = False
    tp3_hit:         bool = False
    best_price:      float = 0.0
    last_action:     str  = "OPEN"
    last_msg_key:    str  = ""
    consecutive_stops: int = 0
    notes:           list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════

def now_utc():
    return datetime.now(timezone.utc)

def iso_now():
    return now_utc().isoformat()

def eastern_tz():
    return ZoneInfo("America/New_York")

def et_to_utc(date_obj, hour, minute=0):
    return datetime(date_obj.year, date_obj.month, date_obj.day,
                    hour, minute, tzinfo=eastern_tz()).astimezone(timezone.utc)

def sf(value, default=None):
    try:
        return float(value) if value is not None else default
    except Exception:
        return default

def rp(value, decimals=4):
    return round(float(value), decimals) if value is not None else None

def pct(a, b):
    return (a - b) / b * 100.0 if b else 0.0

def signed_pct(side, entry, price):
    raw = pct(price, entry)
    return raw if side == "LONG" else -raw

def opp(side):
    return "SHORT" if side == "LONG" else "LONG"

def side_ua(side):
    return {"LONG": "лонг", "SHORT": "шорт"}.get(side, "нейтрально")


# ══════════════════════════════════════════════════════════════════════
# L1 — MARKET DATA
# ══════════════════════════════════════════════════════════════════════

def http_get(url, timeout=REQUEST_TIMEOUT, retries=2):
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Signal-Bot/3.0",
        "Accept": "application/json, application/rss+xml, text/xml, */*",
    }
    for attempt in range(max(1, retries)):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code < 400:
                return r
        except Exception as e:
            pass
        time.sleep(0.35 * attempt)
    return None

def http_post(url, payload, timeout=REQUEST_TIMEOUT):
    headers = {"User-Agent": "Mozilla/5.0 BZU-Signal-Bot/3.0",
               "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code < 400:
            return r
    except Exception:
        pass
    return None

def _parse_okx_candles(rows):
    candles = []
    for row in (rows or []):
        try:
            candles.append(Candle(
                ts=int(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5] or 0),
            ))
        except Exception:
            continue
    candles.sort(key=lambda c: c.ts)
    return candles

def get_okx_candles(bar="15m", limit=200):
    url = f"https://www.okx.com/api/v5/market/candles?instId={OKX_INST_ID}&bar={bar}&limit={limit}"
    r = http_get(url)
    if not r:
        return []
    try:
        return _parse_okx_candles(r.json().get("data", []))
    except Exception:
        return []

def get_okx_ticker():
    url = f"https://www.okx.com/api/v5/market/ticker?instId={OKX_INST_ID}"
    r = http_get(url)
    if not r:
        return {}
    try:
        row = r.json().get("data", [{}])[0]
        last    = float(row.get("last") or 0)
        open24h = float(row.get("open24h") or 0)
        return {
            "price":     last,
            "change24h": pct(last, open24h) if open24h else 0,
            "volume24h": sf(row.get("volCcy24h"), 0),
            "source":    "OKX",
        }
    except Exception:
        return {}

def get_okx_trades(limit=150):
    url = f"https://www.okx.com/api/v5/market/trades?instId={OKX_INST_ID}&limit={limit}"
    r = http_get(url)
    if not r:
        return []
    try:
        return [
            {"price": float(t.get("px") or 0),
             "size":  float(t.get("sz") or 0),
             "side":  str(t.get("side") or "").lower(),
             "ts":    int(t.get("ts") or 0)}
            for t in r.json().get("data", [])
        ]
    except Exception:
        return []

def get_okx_book(depth=50):
    url = f"https://www.okx.com/api/v5/market/books?instId={OKX_INST_ID}&sz={depth}"
    r = http_get(url)
    if not r:
        return {"bids": [], "asks": []}
    try:
        raw = r.json().get("data", [{}])[0]
        bids = [(float(x[0]), float(x[1])) for x in raw.get("bids", []) if len(x) >= 2]
        asks = [(float(x[0]), float(x[1])) for x in raw.get("asks", []) if len(x) >= 2]
        return {"bids": bids, "asks": asks}
    except Exception:
        return {"bids": [], "asks": []}

def get_tv_fallback():
    url = "https://scanner.tradingview.com/crypto/scan"
    payload = {
        "symbols": {"tickers": ["BINANCE:BZUSDT.P", "BINANCE:BZUSDT"], "query": {"types": []}},
        "columns": ["close", "change", "volume"],
    }
    r = http_post(url, payload)
    if not r:
        return {}
    try:
        rows = r.json().get("data", [])
        if not rows:
            return {}
        v = rows[0].get("d") or []
        return {"price": float(v[0]), "change24h": sf(v[1], 0),
                "volume24h": sf(v[2], 0), "source": "TradingView"}
    except Exception:
        return {}

def collect_market_data():
    """Gather all raw market data; return structured dict."""
    c3   = get_okx_candles("3m",  220)
    c15  = get_okx_candles("15m", 200)
    c1h  = get_okx_candles("1H",  150)
    c4h  = get_okx_candles("4H",  100)   # NEW: 4h for macro bias
    tick = get_okx_ticker() or get_tv_fallback()

    if not tick and c15:
        tick = {"price": c15[-1].close, "change24h": pct(c15[-1].close, c15[0].open),
                "volume24h": 0, "source": "candles"}

    return {
        "ticker":      tick,
        "candles_3m":  c3,
        "candles_15m": c15,
        "candles_1h":  c1h,
        "candles_4h":  c4h,
        "trades":      get_okx_trades(),
        "book":        get_okx_book(),
    }


# ══════════════════════════════════════════════════════════════════════
# L2 — CORE INDICATORS
# ══════════════════════════════════════════════════════════════════════

def ema(values, period):
    vals = [float(x) for x in values if x is not None]
    if not vals:
        return None
    k = 2 / (period + 1)
    r = vals[0]
    for v in vals[1:]:
        r = v * k + r * (1 - k)
    return r

def ema_series(values, period):
    """Return full EMA series, same length as input."""
    vals = [float(x) for x in values if x is not None]
    if not vals:
        return []
    k = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    vals = [float(x) for x in values if x is not None]
    if len(vals) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0));  losses.append(abs(min(d, 0)))
    ag = mean(gains[-period:]) if gains[-period:] else 0
    al = mean(losses[-period:]) if losses[-period:] else 0
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))

def atr(candles, period=14):
    if not candles or len(candles) < 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    sample = trs[-period:]
    return mean(sample) if sample else None

def atr_series(candles, period=14):
    """ATR at each bar (same length as candles, first period-1 = None)."""
    if len(candles) < 2:
        return [None] * len(candles)
    trs = [None]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    out = [None] * period
    for i in range(period, len(trs)):
        sample = [x for x in trs[max(0, i - period + 1): i + 1] if x is not None]
        out.append(mean(sample) if sample else None)
    return out

def bollinger(values, period=20, k=2.0):
    vals = [float(x) for x in values if x is not None]
    if len(vals) < period:
        return None, None, None
    sample = vals[-period:]
    mid  = mean(sample)
    try:
        sd = stdev(sample)
    except Exception:
        sd = 0
    return mid - k * sd, mid, mid + k * sd

def vwap(candles):
    """Rolling session VWAP using available candles."""
    num = den = 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3
        num += tp * c.volume
        den += c.volume
    return num / den if den else None

def body_ratio(c):
    rng = max(c.high - c.low, 1e-9)
    return abs(c.close - c.open) / rng

def close_loc(c):
    rng = max(c.high - c.low, 1e-9)
    return (c.close - c.low) / rng


# ══════════════════════════════════════════════════════════════════════
# L3 — MARKET REGIME DETECTION
# ══════════════════════════════════════════════════════════════════════
# Regime drives every downstream decision.
# TREND_UP / TREND_DOWN → entry in trend direction
# BREAKOUT             → aggressive entries allowed
# RANGE                → entry only near range edges with confirmation
# VOLATILE / CHOPPY    → no new entries; manage existing only

def detect_regime(candles, label="15m"):
    """
    Returns:
      regime: TREND_UP | TREND_DOWN | RANGE | VOLATILE | CHOPPY | BREAKOUT
      score:  float  (positive = bullish lean, negative = bearish lean)
    """
    if not candles or len(candles) < 60:
        return {"regime": "INSUFFICIENT_DATA", "score": 0, "note": "мало даних"}

    closes = [c.close for c in candles]
    highs  = [c.high  for c in candles]
    lows   = [c.low   for c in candles]

    atr14      = atr(candles, 14) or closes[-1] * 0.006
    atr14_slow = atr(candles[-60:], 28) or atr14  # slower ATR for volatility ratio
    vol_ratio  = atr14 / atr14_slow if atr14_slow else 1.0

    ema8   = ema_series(closes, 8)
    ema21  = ema_series(closes, 21)
    ema55  = ema_series(closes, 55)

    e8_now, e21_now, e55_now = ema8[-1], ema21[-1], ema55[-1]

    # ADX proxy: directional strength via ema slope
    slope_short = (ema8[-1]  - ema8[-12])  / atr14 if len(ema8)  >= 12 else 0
    slope_long  = (ema21[-1] - ema21[-20]) / atr14 if len(ema21) >= 20 else 0

    # Choppiness Index proxy: range / sum of |bar moves|
    recent = candles[-20:]
    rng20  = max(highs[-20:]) - min(lows[-20:]) if len(highs) >= 20 else 0
    bar_moves = sum(abs(c.close - c.open) for c in recent) or 1e-9
    chop = rng20 / bar_moves  # <1.2 = trending; >1.8 = choppy

    score = 0
    notes = []

    # ── EMA stack alignment ───────────────────────────────────────────
    if closes[-1] > e8_now > e21_now > e55_now:
        score += 35; notes.append("EMA стек вгору")
    elif closes[-1] < e8_now < e21_now < e55_now:
        score -= 35; notes.append("EMA стек вниз")
    elif e8_now > e21_now:
        score += 12
    elif e8_now < e21_now:
        score -= 12

    # ── Slope strength ────────────────────────────────────────────────
    score += int(slope_short * 14)
    score += int(slope_long  * 8)

    # ── Volatility adjustment ─────────────────────────────────────────
    if vol_ratio >= 1.8:
        notes.append(f"волатильність висока x{round(vol_ratio, 1)}")
    if vol_ratio >= 2.5:
        return {"regime": "VOLATILE", "score": 0, "vol_ratio": round(vol_ratio, 2),
                "atr": rp(atr14), "chop": round(chop, 2),
                "note": f"екстремальна волатильність x{round(vol_ratio, 1)} — немає нових входів"}

    # ── Choppiness ───────────────────────────────────────────────────
    if chop >= 1.85 and abs(slope_short) < 0.35:
        return {"regime": "CHOPPY", "score": 0, "vol_ratio": round(vol_ratio, 2),
                "atr": rp(atr14), "chop": round(chop, 2),
                "note": "ціна рубає боковик — немає нових входів"}

    # ── Range detection ───────────────────────────────────────────────
    range_band = rng20 / (atr14 * 20) if atr14 else 0   # ideally ~1.5-2.5 in trend
    if range_band < 1.4 and abs(slope_long) < 0.25:
        notes.append("вузький діапазон/боковик")
        if score >= 15:
            regime = "TREND_UP"
        elif score <= -15:
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
        return {"regime": regime, "score": int(score), "vol_ratio": round(vol_ratio, 2),
                "atr": rp(atr14), "chop": round(chop, 2),
                "note": "; ".join(notes)}

    # ── Breakout ──────────────────────────────────────────────────────
    prev_rng = max(highs[-40:-20]) - min(lows[-40:-20]) if len(highs) >= 40 else rng20
    if rng20 >= prev_rng * 1.6 and abs(score) >= 20:
        notes.append("пробій з розширенням діапазону")
        return {"regime": "BREAKOUT", "score": int(score), "vol_ratio": round(vol_ratio, 2),
                "atr": rp(atr14), "chop": round(chop, 2),
                "note": "; ".join(notes)}

    # ── Standard trend ────────────────────────────────────────────────
    if score >= 22:
        regime = "TREND_UP"
    elif score <= -22:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"

    return {
        "regime":    regime,
        "score":     int(score),
        "vol_ratio": round(vol_ratio, 2),
        "atr":       rp(atr14),
        "chop":      round(chop, 2),
        "e8":        rp(e8_now),
        "e21":       rp(e21_now),
        "e55":       rp(e55_now),
        "note":      "; ".join(notes) if notes else "стандартний режим",
    }


# ══════════════════════════════════════════════════════════════════════
# L4 — MULTI-TIMEFRAME TREND ALIGNMENT
# ══════════════════════════════════════════════════════════════════════

def tf_bias(candles, label):
    """Single-TF directional bias with weighted confidence 0-100."""
    if not candles or len(candles) < 40:
        return {"label": label, "bias": "NEUTRAL", "confidence": 0,
                "rsi": 50, "note": "мало даних"}

    closes = [c.close for c in candles]
    last   = candles[-1]
    atr14  = atr(candles, 14) or last.close * 0.006
    rsi14  = rsi(closes, 14)
    e20    = ema(closes[-80:],  20)
    e50    = ema(closes[-100:], 50)
    e200   = ema(closes,        200) if len(closes) >= 200 else None
    bb_lo, bb_mid, bb_hi = bollinger(closes, 20)
    vwap_v = vwap(candles[-48:])  # ~12h session proxy

    score = 0
    notes = []

    # ── Price vs. EMAs ───────────────────────────────────────────────
    if e20 and e50:
        if last.close > e20 > e50:
            score += 28; notes.append("ціна > EMA20 > EMA50")
        elif last.close < e20 < e50:
            score -= 28; notes.append("ціна < EMA20 < EMA50")
        elif last.close > e20:
            score += 10
        elif last.close < e20:
            score -= 10

    if e200:
        if last.close > e200:
            score += 10; notes.append("над EMA200")
        else:
            score -= 10; notes.append("під EMA200")

    # ── VWAP ─────────────────────────────────────────────────────────
    if vwap_v:
        if last.close > vwap_v:
            score += 8
        else:
            score -= 8

    # ── RSI regime ───────────────────────────────────────────────────
    if 55 <= rsi14 <= 74:
        score += 10; notes.append(f"RSI {round(rsi14, 1)} бичачий")
    elif 26 <= rsi14 <= 45:
        score -= 10; notes.append(f"RSI {round(rsi14, 1)} ведмежий")
    elif rsi14 >= 80:
        score -= 8;  notes.append(f"RSI {round(rsi14, 1)} перекупленість")
    elif rsi14 <= 20:
        score += 8;  notes.append(f"RSI {round(rsi14, 1)} перепроданість")

    # ── Bollinger position ────────────────────────────────────────────
    if bb_hi and bb_lo:
        bb_pct = (last.close - bb_lo) / (bb_hi - bb_lo) if bb_hi != bb_lo else 0.5
        if bb_pct >= 0.72:
            score += 8
        elif bb_pct <= 0.28:
            score -= 8

    # ── Last candle character ─────────────────────────────────────────
    br = body_ratio(last)
    cl = close_loc(last)
    if br >= 0.60:
        if last.close > last.open and cl >= 0.65:
            score += 8; notes.append("сильна бичача свіча")
        elif last.close < last.open and cl <= 0.35:
            score -= 8; notes.append("сильна ведмежа свіча")

    # ── Confidence: normalised abs(score) ────────────────────────────
    confidence = min(100, int(abs(score) * 1.4))

    if score >= 24:
        bias = "LONG"
    elif score <= -24:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "label":      label,
        "bias":       bias,
        "confidence": confidence,
        "score":      int(score),
        "rsi":        round(rsi14, 1),
        "e20":        rp(e20),
        "e50":        rp(e50),
        "vwap":       rp(vwap_v),
        "atr":        rp(atr14),
        "note":       "; ".join(notes[:3]) if notes else "нема сильного перекосу",
    }

def tf_alignment(tf3, tf15, tf1h, tf4h):
    """
    Returns:
      direction: LONG | SHORT | CONFLICT
      strength:  0-3 (how many TFs agree)
      macro_ok:  bool (4h is not against)
    """
    votes = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    for tf in [tf3, tf15, tf1h]:
        votes[tf["bias"]] += 1

    if votes["LONG"] >= 2 and votes["SHORT"] == 0:
        direction = "LONG"
    elif votes["SHORT"] >= 2 and votes["LONG"] == 0:
        direction = "SHORT"
    elif votes["LONG"] >= 2 and votes["SHORT"] == 1:
        direction = "LONG"   # majority but one conflict
    elif votes["SHORT"] >= 2 and votes["LONG"] == 1:
        direction = "SHORT"
    else:
        direction = "CONFLICT"

    strength   = votes.get(direction, 0)
    macro_bias = tf4h.get("bias", "NEUTRAL")
    macro_ok   = macro_bias != opp(direction) if direction != "CONFLICT" else False

    return {
        "direction": direction,
        "strength":  strength,
        "macro_ok":  macro_ok,
        "macro_bias": macro_bias,
        "votes":     votes,
    }


# ══════════════════════════════════════════════════════════════════════
# L5 — PRICE STRUCTURE (SMC / ICT CONCEPTS)
# ══════════════════════════════════════════════════════════════════════
# Uses proper HH/HL/LH/LL chain — NOT simple n-bar lookback.
# Detects: BOS (Break of Structure), CHoCH (Change of Character),
#          FVG (Fair Value Gap), OB (Order Block proxy).

def find_pivot_highs(candles, left=3, right=3):
    """Return indices of confirmed pivot highs."""
    pivots = []
    for i in range(left, len(candles) - right):
        if all(candles[i].high >= candles[j].high for j in range(i - left, i + right + 1) if j != i):
            pivots.append(i)
    return pivots

def find_pivot_lows(candles, left=3, right=3):
    pivots = []
    for i in range(left, len(candles) - right):
        if all(candles[i].low <= candles[j].low for j in range(i - left, i + right + 1) if j != i):
            pivots.append(i)
    return pivots

def classify_swings(candles):
    """
    Walk pivot highs/lows and label as HH/LH/HL/LL.
    Returns list of SwingPoint sorted by index.
    """
    if len(candles) < 20:
        return []

    ph = find_pivot_highs(candles, 3, 3)
    pl = find_pivot_lows(candles,  3, 3)

    all_pivots = (
        [(i, candles[i].high, "HIGH") for i in ph] +
        [(i, candles[i].low,  "LOW")  for i in pl]
    )
    all_pivots.sort(key=lambda x: x[0])

    swings = []
    last_high = last_low = None

    for idx, price, kind in all_pivots:
        if kind == "HIGH":
            if last_high is None:
                label = "HH"
            else:
                label = "HH" if price > last_high else "LH"
            last_high = price
            swings.append(SwingPoint(idx=idx, price=price, ts=candles[idx].ts, kind=label))
        else:
            if last_low is None:
                label = "HL"
            else:
                label = "HL" if price > last_low else "LL"
            last_low = price
            swings.append(SwingPoint(idx=idx, price=price, ts=candles[idx].ts, kind=label))

    return swings

def detect_fvg(candles, min_size_atr=0.3):
    """
    Fair Value Gap = 3-candle pattern where candles[i-2].high < candles[i].low (bullish FVG)
    or candles[i-2].low > candles[i].high (bearish FVG).
    Only return recent ones (last 30 bars).
    """
    atr14 = atr(candles, 14) or candles[-1].close * 0.005
    fvgs  = []
    start = max(2, len(candles) - 30)
    for i in range(start, len(candles)):
        c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
        # Bullish FVG
        if c0.high < c2.low and (c2.low - c0.high) >= atr14 * min_size_atr:
            fvgs.append({"kind": "BULL", "top": c2.low, "bot": c0.high,
                         "mid": (c2.low + c0.high) / 2, "idx": i})
        # Bearish FVG
        if c0.low > c2.high and (c0.low - c2.high) >= atr14 * min_size_atr:
            fvgs.append({"kind": "BEAR", "top": c0.low, "bot": c2.high,
                         "mid": (c0.low + c2.high) / 2, "idx": i})
    return fvgs[-4:] if fvgs else []

def analyze_structure(candles, label="15m"):
    if not candles or len(candles) < 40:
        return {"available": False, "bias": "NEUTRAL", "phase": "NO DATA",
                "swing_high": None, "swing_low": None,
                "bos": None, "choch": None, "fvg": [], "note": "мало даних"}

    swings = classify_swings(candles)
    last   = candles[-1]
    atr14  = atr(candles, 14) or last.close * 0.006
    fvgs   = detect_fvg(candles)

    # Get last confirmed swing high/low
    highs = [s for s in swings if s.kind in ("HH", "LH")]
    lows  = [s for s in swings if s.kind in ("HL", "LL")]
    last_sh = highs[-1] if highs else None
    last_sl = lows[-1]  if lows  else None

    # BOS detection: price closes beyond last swing extreme
    phase  = "RANGING"
    bos    = None
    choch  = None
    bias   = "NEUTRAL"

    if last_sh and last.close > last_sh.price:
        bos   = {"side": "LONG",  "level": last_sh.price, "label": last_sh.kind}
        phase = "BOS_LONG"
        bias  = "LONG"

    elif last_sl and last.close < last_sl.price:
        bos   = {"side": "SHORT", "level": last_sl.price, "label": last_sl.kind}
        phase = "BOS_SHORT"
        bias  = "SHORT"

    # CHoCH: sequence switch (e.g. was making HH/HL, now LH appears)
    if len(swings) >= 4:
        last4 = swings[-4:]
        last4_kinds = [s.kind for s in last4]
        if last4_kinds[-1] == "LH" and last4_kinds[-2] in ("HH",):
            choch = {"side": "SHORT", "note": "LH після HH — можлива зміна напряму"}
            if bias == "NEUTRAL":
                bias = "SHORT"; phase = "CHOCH_SHORT"
        elif last4_kinds[-1] == "HL" and last4_kinds[-2] in ("LL",):
            choch = {"side": "LONG",  "note": "HL після LL — можлива зміна напряму"}
            if bias == "NEUTRAL":
                bias = "LONG";  phase = "CHOCH_LONG"

    # Liquidity sweep proxy
    recent_high = max(c.high for c in candles[-20:])
    recent_low  = min(c.low  for c in candles[-20:])

    sweep_up   = last.high > recent_high and last.close < recent_high
    sweep_down = last.low  < recent_low  and last.close > recent_low

    if sweep_up:
        phase = "UPSIDE_SWEEP"
        bias  = "SHORT" if bias == "NEUTRAL" else bias
    if sweep_down:
        phase = "DOWNSIDE_SWEEP"
        bias  = "LONG"  if bias == "NEUTRAL" else bias

    # Key levels
    swing_high = rp(last_sh.price) if last_sh else rp(recent_high)
    swing_low  = rp(last_sl.price) if last_sl else rp(recent_low)

    # Swing chain note
    chain_note = " ".join(s.kind for s in swings[-5:]) if swings else "немає ланцюга"

    notes = []
    if bos:
        notes.append(f"BOS {bos['side']} @ {rp(bos['level'])}")
    if choch:
        notes.append(choch["note"])
    if sweep_up:
        notes.append("зняття high — повернення нижче")
    if sweep_down:
        notes.append("зняття low — повернення вище")
    if fvgs:
        notes.append(f"{len(fvgs)} FVG зон")

    return {
        "available":   True,
        "bias":        bias,
        "phase":       phase,
        "swing_high":  swing_high,
        "swing_low":   swing_low,
        "recent_high": rp(recent_high),
        "recent_low":  rp(recent_low),
        "bos":         bos,
        "choch":       choch,
        "fvg":         fvgs,
        "chain":       chain_note,
        "atr":         rp(atr14),
        "note":        "; ".join(notes) if notes else "чіткої структури немає",
    }


# ══════════════════════════════════════════════════════════════════════
# L6 — LIQUIDITY ANALYSIS
# ══════════════════════════════════════════════════════════════════════
# Identifies stop clusters, sweep events, post-sweep reclaims.
# These are HIGH-PROBABILITY entry triggers.

def analyze_liquidity(candles_3m, candles_15m, structure):
    candles = candles_3m if candles_3m and len(candles_3m) >= 25 else candles_15m
    if not candles or len(candles) < 25:
        return {"available": False, "event": "NO_DATA", "bias": "NEUTRAL",
                "score": 0, "blocks": [], "note": "мало даних"}

    last  = candles[-1]
    prev6 = candles[-7:-1]
    atr14 = atr(candles, 14) or last.close * 0.005
    avg_vol = mean([c.volume for c in candles[-20:-1]]) or 1
    vol_now = last.volume / avg_vol

    recent_high = max(c.high for c in prev6)
    recent_low  = min(c.low  for c in prev6)

    move1 = pct(last.close, candles[-2].close)
    move6 = pct(last.close, candles[-7].close) if len(candles) >= 7 else move1
    cl    = close_loc(last)
    br    = body_ratio(last)

    phase = structure.get("phase", "") if structure else ""

    score  = 0
    event  = "QUIET"
    blocks = []
    notes  = []

    # ── Downside sweep (sweep of lows, reclaim) ───────────────────────
    swept_low  = last.low  < recent_low  and last.close > recent_low
    swept_high = last.high > recent_high and last.close < recent_high

    if swept_low or phase == "DOWNSIDE_SWEEP":
        event = "DOWNSIDE_SWEEP"
        score += 22
        blocks.append("SHORT")  # block SHORT — don't short after sweep
        notes.append("зняли low і повернулись — шорт не актуальний, лонг на ретесті")

    elif swept_high or phase == "UPSIDE_SWEEP":
        event = "UPSIDE_SWEEP"
        score -= 22
        blocks.append("LONG")
        notes.append("зняли high і впали — лонг не актуальний, шорт на ретесті")

    # ── Strong move with high volume ─────────────────────────────────
    if vol_now >= 1.4 and br >= 0.55:
        if move1 <= -0.40 or move6 <= -0.65:
            if cl <= 0.28:
                event = "LONG_LIQ"
                score -= 16
                notes.append("дамп на обсязі, закриття в дні — ймовірно вибивають лонги")
            elif cl >= 0.55:
                event = "LONG_LIQ_ABSORBED"
                score += 12
                blocks.append("SHORT")
                notes.append("дамп викупили — новий шорт тільки після ретесту")

        if move1 >= 0.40 or move6 >= 0.65:
            if cl >= 0.72:
                event = "SHORT_SQUEEZE"
                score += 16
                notes.append("памп на обсязі — ймовірно шорт-сквіз")
            elif cl <= 0.45:
                event = "SHORT_SQUEEZE_ABSORBED"
                score -= 12
                blocks.append("LONG")
                notes.append("памп поглинули — новий лонг тільки після ретесту")

    if score >= 12:
        bias = "LONG"
    elif score <= -12:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "available": True,
        "event":     event,
        "bias":      bias,
        "score":     int(score),
        "blocks":    sorted(set(blocks)),
        "vol_ratio": round(vol_now, 2),
        "move1_pct": round(move1, 3),
        "note":      "; ".join(notes[:3]) if notes else "ліквідаційного тиску немає",
    }


# ══════════════════════════════════════════════════════════════════════
# L7 — ORDER FLOW
# ══════════════════════════════════════════════════════════════════════

def analyze_flow(trades, book, price):
    buy_vol  = sum(t["size"] for t in trades if t.get("side") == "buy")
    sell_vol = sum(t["size"] for t in trades if t.get("side") == "sell")
    total    = buy_vol + sell_vol or 1
    delta    = (buy_vol - sell_vol) / total * 100

    bids = (book or {}).get("bids", [])
    asks = (book or {}).get("asks", [])
    near_bid = near_ask = 0
    wall_note = ""

    if bids and asks and price:
        zone  = price * 0.005
        nb    = [(p, s) for p, s in bids if p >= price - zone]
        na    = [(p, s) for p, s in asks if p <= price + zone]
        near_bid = sum(s for _, s in nb)
        near_ask = sum(s for _, s in na)
        if nb and na:
            bb = max(nb, key=lambda x: x[1])
            ba = max(na, key=lambda x: x[1])
            if bb[1] > ba[1] * 2.0:
                wall_note = f"велика підтримка покупців @ {rp(bb[0])}"
            elif ba[1] > bb[1] * 2.0:
                wall_note = f"великий опір продавців @ {rp(ba[0])}"

    book_total = near_bid + near_ask or 1
    book_delta = (near_bid - near_ask) / book_total * 100

    score = 0
    notes = []
    if delta >= 20:
        score += 18; notes.append("угоди: перевага покупців")
    elif delta <= -20:
        score -= 18; notes.append("угоди: перевага продавців")
    elif delta >= 8:
        score += 7
    elif delta <= -8:
        score -= 7

    if book_delta >= 20:
        score += 12; notes.append("стакан: покупці домінують")
    elif book_delta <= -20:
        score -= 12; notes.append("стакан: продавці домінують")

    if wall_note:
        notes.append(wall_note)

    bias = "LONG" if score >= 16 else ("SHORT" if score <= -16 else "NEUTRAL")

    return {
        "bias":        bias,
        "score":       int(score),
        "trade_delta": round(delta, 2),
        "book_delta":  round(book_delta, 2),
        "note":        "; ".join(notes[:3]) if notes else "потік без явної переваги",
    }


# ══════════════════════════════════════════════════════════════════════
# L8 — FUNDAMENTAL FILTER (News + Calendar)
# ══════════════════════════════════════════════════════════════════════

def _parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _norm(text):
    return _clean(text).lower().replace("'", "'").replace("–", "-").replace("—", "-")

def _parse_google_rss(query, hours=4):
    url = f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{hours}h')}&hl=en-US&gl=US&ceid=US:en"
    r = http_get(url, timeout=10, retries=1)
    if not r:
        return []
    cutoff = now_utc() - timedelta(hours=hours)
    items  = []
    try:
        root = ET.fromstring(r.content)
        for node in root.findall(".//item")[:12]:
            title = _clean(node.findtext("title", ""))
            dt    = _parse_date(node.findtext("pubDate", ""))
            if dt and dt < cutoff:
                continue
            if title:
                items.append({"title": title, "published_at": dt})
    except Exception:
        pass
    return items

def get_news():
    items = []
    for q in NEWS_QUERIES:
        items.extend(_parse_google_rss(q, 4))
    # Deduplicate
    seen, out = set(), []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", _norm(item.get("title", "")))[:110]
        if key and key not in seen:
            seen.add(key); out.append(item)
    out.sort(key=lambda x: x.get("published_at") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return out

def _score_item(title):
    text    = _norm(title)
    age_min = 0  # we already filtered to 4h; assume recent

    bull = sum(1 for p in BULLISH_PHRASES if p in text)
    bear = sum(1 for p in BEARISH_PHRASES if p in text)
    src  = sum(1 for s in HIGH_IMPACT_SOURCES if s in text)
    term = sum(1 for t in HIGH_IMPACT_TERMS   if t in text)
    impact = src * 2 + term

    raw = (bull - bear) * (10 + min(8, impact))
    if src >= 1:
        raw = int(raw * 1.25)
    return raw, impact

def analyze_news(items):
    total_raw = 0
    important = []
    for item in items[:40]:
        s, imp = _score_item(item.get("title", ""))
        total_raw += s
        if imp >= 2 or abs(s) >= 10:
            important.append({**item, "score": s, "impact": imp})

    score = max(-50, min(50, int(total_raw)))
    bias  = "LONG" if score >= 20 else ("SHORT" if score <= -20 else "NEUTRAL")
    top   = important[0]["title"] if important else "свіжого сильного драйвера немає"

    return {
        "bias":      bias,
        "score":     score,
        "total":     len(items),
        "important": important[:5],
        "top":       top,
        "note":      f"{side_ua(bias)} ({score}); {top[:120]}",
    }

def _parse_month_date(text):
    clean = re.sub(r"[^A-Za-z0-9, ]+", "", str(text or "")).strip()
    for fmt in ["%B %d, %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(clean, fmt).date()
        except Exception:
            pass
    return None

def _parse_us_time(text, dh=10, dm=30):
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", str(text or ""), re.I)
    if not m:
        return dh, dm
    h, mn = int(m.group(1)), int(m.group(2))
    if m.group(3).upper() == "P" and h != 12:
        h += 12
    if m.group(3).upper() == "A" and h == 12:
        h = 0
    return h, mn

def _event_status(event_time):
    try:
        mins = int((event_time - now_utc()).total_seconds() / 60)
        if 0 <= mins <= IMPORTANT_EVENT_WINDOW_MINUTES:
            return True, f"через {mins} хв"
        if -60 <= mins < 0:
            return True, f"вийшла {abs(mins)} хв тому"
        return False, ""
    except Exception:
        return False, ""

def get_eia_event():
    text = ""
    for url in [EIA_WPSR_URL, EIA_WPSR_SCHEDULE_URL]:
        r = http_get(url, timeout=8, retries=1)
        if r:
            text += "\n" + _clean(r.text)
    m = re.search(r"Next Release Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if not m:
        return None
    d = _parse_month_date(m.group(1))
    if not d:
        return None
    idx  = text.find(m.group(1))
    h, mn = _parse_us_time(text[idx: idx + 260] if idx >= 0 else text, 10, 30)
    return {"name": "EIA", "title": "EIA crude inventories",
            "time": et_to_utc(d, h, mn), "risk": "HIGH"}

def get_fomc_events():
    r = http_get(FED_FOMC_CALENDAR_URL, timeout=8, retries=1)
    if not r:
        return []
    text  = _clean(r.text)
    year  = now_utc().year
    months = ("January|February|March|April|May|June|July|August|"
               "September|October|November|December|Jan|Feb|Mar|Apr|"
               "Jun|Jul|Aug|Sep|Oct|Nov|Dec")
    events = []
    for m in re.finditer(rf"({months})\s+(\d{{1,2}})(?:-(\d{{1,2}}))?", text):
        raw = f"{m.group(1)} {m.group(3) or m.group(2)}, {year}"
        d   = _parse_month_date(raw)
        if not d:
            continue
        et = et_to_utc(d, 14, 0)
        active, _ = _event_status(et)
        if active:
            events.append({"name": "Fed", "title": "Fed / FOMC decision",
                           "time": et, "risk": "HIGH"})
    return events[:2]

def get_manual_events():
    events = []
    for chunk in [x.strip() for x in MANUAL_IMPORTANT_EVENTS.split(";") if x.strip()]:
        parts = [x.strip() for x in chunk.split("|")]
        if len(parts) < 2:
            continue
        try:
            dt = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            events.append({"name": "Manual", "title": parts[0],
                           "time": dt.astimezone(timezone.utc),
                           "risk": parts[2] if len(parts) > 2 else "HIGH"})
        except Exception:
            continue
    return events

def analyze_calendar():
    events  = []
    for fn in [get_eia_event, get_fomc_events, get_manual_events]:
        try:
            result = fn()
            if isinstance(result, list):
                events.extend(result)
            elif result:
                events.append(result)
        except Exception as e:
            print(f"[WARN] calendar: {e}")

    alerts = []
    for ev in events:
        active, status = _event_status(ev.get("time"))
        if not active:
            continue
        alerts.append({**ev, "status": status})

    return {
        "active":  bool(alerts),
        "alerts":  alerts[:3],
        "score":   -15 if alerts else 0,
        "note":    "; ".join(f"{x['title']} {x['status']}" for x in alerts[:2]) if alerts else "",
    }

def market_session():
    h = now_utc().hour
    if 12 <= h < 20:
        return {"name": "NY/LONDON OVERLAP", "score": 5,  "liquid": True}
    if 7  <= h < 12:
        return {"name": "LONDON",            "score": 3,  "liquid": True}
    if 20 <= h < 23:
        return {"name": "NY CLOSE",          "score": 1,  "liquid": True}
    return     {"name": "ASIA/QUIET",        "score": -3, "liquid": False}


# ══════════════════════════════════════════════════════════════════════
# L9 — ENTRY ENGINE (Confluence Gate)
# ══════════════════════════════════════════════════════════════════════
# All gates must pass for ENTRY. Failing a gate = WATCH or WAIT.
#
# GATE 1: Regime is NOT CHOPPY and NOT VOLATILE
# GATE 2: TF alignment >= 2/3 (3m + 15m + 1h), macro (4h) not opposed
# GATE 3: Structure bias matches direction (BOS or CHoCH or FVG retest)
# GATE 4: NOT a late-chase (price within 1.5 ATR of EMA20)
# GATE 5: No active liquidity block against direction
# GATE 6: Calendar blackout not active (or quality capped)
# GATE 7: Minimum quality score >= threshold

def _gate_report(name, passed, reason=""):
    return {"gate": name, "passed": passed, "reason": reason}

def late_chase_check(side, candles_15m, regime):
    """True if price has already moved too far; wait for retest."""
    if not candles_15m or len(candles_15m) < 20:
        return False, ""
    closes = [c.close for c in candles_15m]
    last   = candles_15m[-1]
    e20    = ema(closes[-60:], 20)
    atr14  = regime.get("atr") or atr(candles_15m, 14) or last.close * 0.006
    rsi14  = sf(None)  # not needed here, use regime
    if not e20:
        return False, ""
    dist = abs(last.close - e20) / atr14 if atr14 else 0
    if dist >= 1.5:
        dir_word = "вгору" if last.close > e20 else "вниз"
        return True, f"ціна вже пройшла {round(dist, 1)}×ATR {dir_word} від EMA20 — чекати ретест"
    return False, ""

def entry_quality(side, tf_align, regime, structure, liquidity, flow,
                  news, calendar_alert, candles_15m):
    """
    Compute quality 0-100.
    Quality is a GATE-based score — it does NOT artificially inflate.
    """
    gates  = []
    score  = 42   # base

    # GATE 1: Regime
    bad_regime = regime.get("regime") in ("CHOPPY", "VOLATILE", "INSUFFICIENT_DATA")
    gates.append(_gate_report("REGIME", not bad_regime,
                               regime.get("regime") if bad_regime else "OK"))
    if bad_regime:
        return 0, gates, "режим не підходить для входу"

    # GATE 2: TF Alignment
    direction = tf_align["direction"]
    align_ok  = direction == side and tf_align["strength"] >= 2
    macro_ok  = tf_align["macro_ok"]
    gates.append(_gate_report("TF_ALIGNMENT", align_ok,
                               f"strength={tf_align['strength']}, macro={'OK' if macro_ok else 'ПРОТИ'}"))
    if not align_ok:
        score -= 18
    if not macro_ok:
        score -= 10

    # GATE 3: Structure
    struct_ok = structure.get("bias") in (side, "NEUTRAL") and structure.get("available")
    gates.append(_gate_report("STRUCTURE", struct_ok, structure.get("phase", "")))
    if struct_ok and structure.get("bias") == side:
        score += 16
    elif not struct_ok:
        score -= 14

    # BOS/CHoCH bonus
    bos = structure.get("bos")
    choch = structure.get("choch")
    if bos and bos.get("side") == side:
        score += 10
    if choch and choch.get("side") == side:
        score += 8

    # FVG retest bonus
    fvgs = structure.get("fvg", [])
    last_price = candles_15m[-1].close if candles_15m else 0
    fvg_kind = "BULL" if side == "LONG" else "BEAR"
    fvg_in_zone = any(
        f["kind"] == fvg_kind and f["bot"] <= last_price <= f["top"]
        for f in fvgs
    ) if fvgs and last_price else False
    if fvg_in_zone:
        score += 8
        gates.append(_gate_report("FVG_RETEST", True, "ціна у зоні FVG — підвищений шанс відскоку"))
    else:
        gates.append(_gate_report("FVG_RETEST", False, "FVG зони немає або ціна не у ній"))

    # GATE 4: Late chase
    late, late_reason = late_chase_check(side, candles_15m, regime)
    gates.append(_gate_report("NO_LATE_CHASE", not late, late_reason))
    if late:
        score = min(score, 55)

    # GATE 5: Liquidity
    liq_blocked = side in liquidity.get("blocks", [])
    gates.append(_gate_report("LIQUIDITY", not liq_blocked,
                               liquidity.get("event", "")))
    if liq_blocked:
        score = min(score, 50)
    elif liquidity.get("bias") == side:
        score += 8

    # GATE 6: Flow
    flow_ok = flow.get("bias") != opp(side)
    gates.append(_gate_report("FLOW", flow_ok, flow.get("note", "")))
    if flow.get("bias") == side:
        score += 8
    elif not flow_ok:
        score -= 8

    # GATE 7: News
    news_ok = news.get("bias") != opp(side) or abs(news.get("score", 0)) < 25
    gates.append(_gate_report("NEWS", news_ok, news.get("top", "")[:80]))
    if news.get("bias") == side and abs(news.get("score", 0)) >= 20:
        score += 6
    elif not news_ok:
        score -= 10

    # GATE 8: Calendar
    cal_active = calendar_alert.get("active")
    gates.append(_gate_report("CALENDAR", not cal_active, calendar_alert.get("note", "")))
    if cal_active:
        score -= 12
        score = min(score, 58)

    # Session bonus
    session = market_session()
    if session["liquid"]:
        score += 4

    final = int(max(0, min(92, score)))
    fail_reasons = [g["reason"] for g in gates if not g["passed"] and g["reason"]]
    reason = "; ".join(fail_reasons[:3]) if fail_reasons else "всі умови виконані"
    return final, gates, reason


def evaluate_setup(context):
    """
    Master entry evaluator. Returns action, side, quality, plan, gates.
    Actions: ENTRY | RISKY_ENTRY | WATCH | WAIT_RETEST | NO_TRADE
    """
    regime    = context["regime"]
    tf_align  = context["tf_align"]
    structure = context["structure"]
    liquidity = context["liquidity"]
    flow      = context["flow"]
    news      = context["news"]
    calendar  = context["calendar"]
    c15       = context["candles_15m"]

    direction = tf_align["direction"]
    if direction == "CONFLICT" or not context.get("price"):
        return {
            "action":    "NO_TRADE",
            "side":      "NEUTRAL",
            "quality":   0,
            "title":     "ВХОДУ НЕМАЄ",
            "reason":    "ТФ-конфлікт або немає ціни",
            "plan":      None,
            "gates":     [],
        }

    side = direction

    # Block entry if regime is unfavorable
    if regime.get("regime") in ("CHOPPY", "VOLATILE"):
        return {
            "action":  "NO_TRADE",
            "side":    side,
            "quality": 0,
            "title":   f"НЕМАЄ ВХОДУ — {regime['regime']}",
            "reason":  regime.get("note", ""),
            "plan":    None,
            "gates":   [],
        }

    quality, gates, reason = entry_quality(
        side, tf_align, regime, structure, liquidity, flow, news, calendar, c15
    )

    # Late chase → WAIT_RETEST
    late_gate = next((g for g in gates if g["gate"] == "NO_LATE_CHASE"), None)
    if late_gate and not late_gate["passed"]:
        plan = build_plan(side, context)
        return {
            "action":  "WAIT_RETEST",
            "side":    side,
            "quality": quality,
            "title":   f"ЧЕКАТИ — {side} НЕ ДОГАНЯТИ",
            "reason":  late_gate["reason"],
            "plan":    plan,
            "gates":   gates,
        }

    plan = build_plan(side, context)

    if plan and quality >= ENTRY_QUALITY_MIN:
        all_critical_pass = all(
            g["passed"] for g in gates
            if g["gate"] in ("REGIME", "TF_ALIGNMENT", "NO_LATE_CHASE", "LIQUIDITY")
        )
        if all_critical_pass:
            return {
                "action":  "ENTRY",
                "side":    side,
                "quality": quality,
                "title":   f"ВХІД — {side}",
                "reason":  reason,
                "plan":    plan,
                "gates":   gates,
            }

    if plan and quality >= RISKY_QUALITY_MIN:
        return {
            "action":  "RISKY_ENTRY",
            "side":    side,
            "quality": quality,
            "title":   f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason":  reason,
            "plan":    plan,
            "gates":   gates,
        }

    return {
        "action":  "WATCH",
        "side":    side,
        "quality": quality,
        "title":   f"ЧЕКАТИ — ГОТУЄМОСЬ ДО {side}",
        "reason":  reason,
        "plan":    plan,
        "gates":   gates,
    }


# ══════════════════════════════════════════════════════════════════════
# L10 — TRADE PLAN BUILDER
# ══════════════════════════════════════════════════════════════════════
# Stop is ALWAYS behind a structural level.
# TP is set by R:R (minimum) or structural target (whichever is farther).

def build_plan(side, context):
    price     = context["price"]
    atr14     = sf(context["regime"].get("atr")) or price * 0.006
    structure = context["structure"]
    c15       = context["candles_15m"]

    if not price or not atr14:
        return None

    swing_high = sf(structure.get("swing_high")) or price + atr14 * 3
    swing_low  = sf(structure.get("swing_low"))  or price - atr14 * 3
    recent_hi  = sf(structure.get("recent_high")) or swing_high
    recent_lo  = sf(structure.get("recent_low"))  or swing_low

    buf = max(atr14 * 0.20, price * 0.0014)
    max_risk = price * (MAX_STOP_PCT / 100)
    min_risk = atr14 * 1.10

    if side == "LONG":
        # Stop: below swing low with buffer; cap at MAX_STOP_PCT
        raw_stop = swing_low - buf
        risk     = max(min_risk, min(price - raw_stop, max_risk))
        stop     = price - risk
        stop_reason = "structural (swing low)"

        # TP: structural targets first, then R:R floor
        tp1_struct = max(recent_hi, price + atr14 * 2.0)
        tp2_struct = swing_high
        tp1 = max(tp1_struct, price + risk * MIN_TP1_RR)
        tp2 = max(tp2_struct, price + risk * MIN_TP2_RR, tp1 + atr14 * 0.8)
        tp3 = max(price + risk * MIN_TP3_RR, tp2 + atr14 * 1.0)

    else:  # SHORT
        raw_stop = swing_high + buf
        risk     = max(min_risk, min(raw_stop - price, max_risk))
        stop     = price + risk
        stop_reason = "structural (swing high)"

        tp1_struct = min(recent_lo, price - atr14 * 2.0)
        tp2_struct = swing_low
        tp1 = min(tp1_struct, price - risk * MIN_TP1_RR)
        tp2 = min(tp2_struct, price - risk * MIN_TP2_RR, tp1 - atr14 * 0.8)
        tp3 = min(price - risk * MIN_TP3_RR, tp2 - atr14 * 1.0)

    risk_pct = abs(stop - price) / price * 100
    rr1 = abs(tp1 - price) / abs(stop - price) if abs(stop - price) else 0
    rr2 = abs(tp2 - price) / abs(stop - price) if abs(stop - price) else 0
    rr3 = abs(tp3 - price) / abs(stop - price) if abs(stop - price) else 0

    # Validate R:R — if structural TPs don't reach minimum, use R:R-based
    tp_method = "structural" if (rr1 >= MIN_TP1_RR and rr2 >= MIN_TP2_RR) else "rr-based"

    return TradePlan(
        entry=rp(price), stop=rp(stop),
        tp1=rp(tp1), tp2=rp(tp2), tp3=rp(tp3),
        risk_pct=round(risk_pct, 3),
        rr1=round(rr1, 2), rr2=round(rr2, 2), rr3=round(rr3, 2),
        stop_reason=stop_reason, tp_method=tp_method,
    )


# ══════════════════════════════════════════════════════════════════════
# L11 — ACTIVE TRADE MANAGER
# ══════════════════════════════════════════════════════════════════════
# Professional exit logic:
#  - Stop: structural (hard stop behind swing)
#  - After TP1: stop moves to break-even
#  - After TP2: trailing stop = 50% of last swing range
#  - Momentum exit: if 3m + structure flip against position and price
#    gives back > 60% of best profit → early exit

def _hit(side, price, level, direction="target"):
    """Check if price has reached a level (target or stop)."""
    if level is None:
        return False
    if direction == "stop":
        return price <= level if side == "LONG" else price >= level
    return price >= level if side == "LONG" else price <= level

def _best(side, trade, current):
    if side == "LONG":
        return max(trade.best_price or current, current)
    return min(trade.best_price or current, current)

def manage_trade(trade, context):
    """
    Returns dict with: closed bool, action, title, recommendation, pct fields, notes.
    Mutates trade in place.
    """
    price = context["price"]
    side  = trade.side
    tf3   = context["tf3"]
    tf15  = context["tf15"]
    structure = context["structure"]
    flow  = context["flow"]

    trade.best_price = _best(side, trade, price)
    current_pct = signed_pct(side, trade.entry, price)
    best_pct    = signed_pct(side, trade.entry, trade.best_price)
    giveback    = max(0, best_pct - current_pct)
    giveback_r  = giveback / best_pct if best_pct > 0.01 else 0

    notes  = []
    action = "HOLD"

    # ── Hard stop ────────────────────────────────────────────────────
    if _hit(side, price, trade.stop_current, "stop"):
        trade.status      = "CLOSED"
        trade.last_action = "STOP"
        return {
            "closed": True, "action": "STOP",
            "title":  f"УГОДУ {side} ЗУПИНЕНО — СТОП",
            "recommendation": "стоп пробитий — сценарій анульований",
            "current_pct": current_pct, "best_pct": best_pct,
            "notes": [f"ціна {rp(price)} | стоп {rp(trade.stop_current)}"],
        }

    # ── TP3 ───────────────────────────────────────────────────────────
    if _hit(side, price, trade.tp3):
        trade.tp3_hit = True; trade.status = "CLOSED"; trade.last_action = "TP3"
        return {
            "closed": True, "action": "TP3",
            "title":  f"УГОДУ {side} ЗАКРИТО — ПОВНА ЦІЛЬ",
            "recommendation": "TP3 досягнуто — повне закриття",
            "current_pct": current_pct, "best_pct": best_pct,
            "notes": [f"TP3 {rp(trade.tp3)} досягнуто"],
        }

    # ── TP2: trail to TP1 ─────────────────────────────────────────────
    if not trade.tp2_hit and _hit(side, price, trade.tp2):
        trade.tp2_hit = True
        action = "TP2_TRAIL"
        notes.append("TP2 досягнуто — зафіксувати 50%, стоп → TP1")
        if side == "LONG":
            trade.stop_current = max(trade.stop_current, trade.tp1)
        else:
            trade.stop_current = min(trade.stop_current, trade.tp1)
        notes.append(f"стоп перенесено → {rp(trade.stop_current)}")

    # ── TP1: move to break-even ────────────────────────────────────────
    elif not trade.tp1_hit and _hit(side, price, trade.tp1):
        trade.tp1_hit = True
        action = "TP1_BE"
        notes.append("TP1 досягнуто — зафіксувати 30-40%, стоп → б/у")
        be = trade.entry * (1.0010 if side == "LONG" else 0.9990)
        if side == "LONG":
            trade.stop_current = max(trade.stop_current, be)
        else:
            trade.stop_current = min(trade.stop_current, be)
        notes.append(f"стоп у б/у → {rp(trade.stop_current)}")

    # ── Momentum exit (early) ─────────────────────────────────────────
    opp_votes = sum(
        1 for b in [tf3, tf15, structure, flow]
        if b.get("bias") == opp(side)
    )
    if best_pct >= 0.40 and giveback_r >= 0.60 and opp_votes >= 2:
        trade.status = "CLOSED"; trade.last_action = "EXIT_MOM"
        return {
            "closed": True, "action": "EXIT",
            "title":  f"УГОДУ {side} ЗАКРИТО — ВТРАТА МОМЕНТУ",
            "recommendation": "прибуток майже відданий, підтвердження зникло — вихід біля входу",
            "current_pct": current_pct, "best_pct": best_pct,
            "notes": [f"віддали {round(giveback, 3)}% з {round(best_pct, 3)}% максимуму"],
        }

    # ── Under pressure ───────────────────────────────────────────────
    if current_pct < -0.30 and opp_votes >= 2 and action == "HOLD":
        action = "EXIT_WARN"
        notes.append("позиція під тиском + структура проти — розглянути ранній вихід")

    # ── Protect ──────────────────────────────────────────────────────
    elif current_pct > 0 and tf3.get("bias") == opp(side) and action == "HOLD":
        action = "PROTECT"
        notes.append("3m слабшає — підтягнути стоп, не віддавати прибуток")

    trade.last_action = action
    rec_map = {
        "HOLD":       "утримувати: тренд активний, стоп на місці",
        "TP1_BE":     "TP1 взято: часткова фіксація, стоп у б/у",
        "TP2_TRAIL":  "TP2 взято: фіксувати 50%, трейлінг-стоп за TP1",
        "EXIT_WARN":  "позиція під тиском — вихід до дальнього стопу",
        "PROTECT":    "підтягнути стоп — прибуток захистити",
    }

    return {
        "closed": False, "action": action,
        "title":  f"СУПРОВІД {side}",
        "recommendation": rec_map.get(action, "утримувати"),
        "current_pct": current_pct, "best_pct": best_pct,
        "notes": notes,
    }

def make_active_trade(setup):
    plan = setup["plan"]
    return ActiveTrade(
        id=uuid.uuid4().hex[:10],
        side=setup["side"],
        opened_at=iso_now(),
        entry=plan.entry, stop_initial=plan.stop, stop_current=plan.stop,
        tp1=plan.tp1, tp2=plan.tp2, tp3=plan.tp3,
        quality=setup["quality"],
        best_price=plan.entry,
        notes=[setup["reason"]],
    )


# ══════════════════════════════════════════════════════════════════════
# L12 — MESSAGE COMPOSER
# ══════════════════════════════════════════════════════════════════════

def _price_line(context):
    chg = round(context.get("change24h", 0), 3)
    sign = "+" if chg >= 0 else ""
    return f"<b>Ціна:</b> <code>{rp(context['price'])}</code> ({sign}{chg}%) | {context.get('source', 'OKX')}"

def _plan_text(plan):
    if not plan:
        return "плану немає"
    return (
        f"Вхід <code>{plan.entry}</code> | Стоп <code>{plan.stop}</code> ({plan.risk_pct}%) | "
        f"TP1 <code>{plan.tp1}</code> | TP2 <code>{plan.tp2}</code> | TP3 <code>{plan.tp3}</code> | "
        f"RR {plan.rr1}/{plan.rr2}/{plan.rr3} | стоп: {plan.stop_reason}"
    )

def _context_summary(context):
    r  = context["regime"]
    tf = context["tf_align"]
    s  = context["structure"]
    li = context["liquidity"]
    fl = context["flow"]
    n  = context["news"]
    tf3  = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context["tf4h"]
    lines = [
        f"<b>Режим:</b> {r.get('regime')} | ATR {r.get('atr')} | vol×{r.get('vol_ratio','')}",
        f"<b>ТФ:</b> 3m {side_ua(tf3['bias'])} | 15m {side_ua(tf15['bias'])} | 1h {side_ua(tf1h['bias'])} | 4h {side_ua(tf4h['bias'])}",
        f"<b>Структура:</b> {s.get('phase')} | {s.get('chain','')} | {s.get('note','')}",
        f"<b>Ліквідність:</b> {li.get('event')} — {li.get('note','')}",
        f"<b>Потік:</b> δ={fl.get('trade_delta')}% / книга δ={fl.get('book_delta')}% — {fl.get('note','')}",
        f"<b>Новини:</b> {side_ua(n['bias'])} ({n['score']}) — {n['top'][:100]}",
    ]
    cal = context.get("calendar", {})
    if cal.get("active"):
        lines.insert(0, f"⚠️ <b>Подія:</b> {cal.get('note')} — утримуватись від входів до виходу даних")
    return lines

def _gate_summary(gates):
    if not gates:
        return ""
    icons = {True: "✅", False: "❌"}
    parts = []
    for g in gates:
        label = g["gate"].replace("_", " ")
        parts.append(f"{icons[g['passed']]} {label}")
    return "<b>Гейти:</b> " + " | ".join(parts)

def build_entry_message(context, setup):
    plan  = setup.get("plan")
    lines = [
        "<b>🛢 BZU SIGNAL BOT v3</b>",
        f"<b>{setup['title']}</b>",
        "",
        _price_line(context),
        f"<b>Якість:</b> {setup['quality']}/100",
        f"<b>Причина:</b> {setup['reason']}",
    ]
    if plan and setup["action"] in ("ENTRY", "RISKY_ENTRY"):
        lines.append(f"<b>План:</b> {_plan_text(plan)}")
        if setup["action"] == "RISKY_ENTRY":
            lines.append("⚠️ <i>Ризикований вхід: менший розмір; якщо одразу йде проти — не тримати до дальнього стопу</i>")
    elif plan:
        lines.append(f"<b>Орієнтир:</b> {_plan_text(plan)}")

    lines.append(_gate_summary(setup.get("gates", [])))
    lines.append("")
    lines.extend(_context_summary(context)[:5])
    return "\n".join(lines).strip()

def build_follow_message(context, trade, result):
    lines = [
        "<b>🛢 BZU SIGNAL BOT v3</b>",
        f"<b>{result['title']}</b> — {result['recommendation']}",
        "",
        _price_line(context),
        (f"<b>Від входу:</b> {round(result['current_pct'], 3)}% | "
         f"Макс: {round(result['best_pct'], 3)}%"),
        (f"<b>Позиція:</b> Вхід {rp(trade.entry)} | Стоп {rp(trade.stop_current)} | "
         f"TP1 {rp(trade.tp1)} {'✅' if trade.tp1_hit else '○'} | "
         f"TP2 {rp(trade.tp2)} {'✅' if trade.tp2_hit else '○'} | "
         f"TP3 {rp(trade.tp3)} {'✅' if trade.tp3_hit else '○'}"),
    ]
    if result.get("notes"):
        lines.append("<b>Дія:</b> " + " | ".join(result["notes"][:3]))
    lines.extend(_context_summary(context)[:4])
    return "\n".join(lines).strip()


# ══════════════════════════════════════════════════════════════════════
# L13 — PERSISTENCE (State + Journal)
# ══════════════════════════════════════════════════════════════════════

def atomic_write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else default
    except Exception:
        pass
    return default

def load_state():
    s = load_json(STATE_FILE, {"version": "v3", "active_trade": None, "history": [], "last_stop_side": None})
    s.setdefault("active_trade", None)
    s.setdefault("history", [])
    s.setdefault("last_stop_side", None)
    return s

def save_state(s):
    s["updated_at"] = iso_now()
    s["history"] = (s.get("history") or [])[-MAX_HISTORY:]
    atomic_write(STATE_FILE, s)

def load_journal():
    j = load_json(JOURNAL_FILE, {"version": "v3", "trades": [], "signals": []})
    j.setdefault("trades",  [])
    j.setdefault("signals", [])
    return j

def save_journal(j):
    j["updated_at"] = iso_now()
    j["trades"]  = (j.get("trades")  or [])[-MAX_JOURNAL:]
    j["signals"] = (j.get("signals") or [])[-MAX_JOURNAL:]
    atomic_write(JOURNAL_FILE, j)

def trade_from_state(state):
    raw = (state or {}).get("active_trade")
    if not isinstance(raw, dict):
        return None
    try:
        return ActiveTrade(
            id=str(raw.get("id") or uuid.uuid4().hex[:10]),
            side=str(raw["side"]),
            opened_at=str(raw.get("opened_at") or iso_now()),
            entry=float(raw["entry"]),
            stop_initial=float(raw.get("stop_initial", raw["entry"])),
            stop_current=float(raw.get("stop_current", raw["entry"])),
            tp1=float(raw["tp1"]), tp2=float(raw["tp2"]), tp3=float(raw["tp3"]),
            quality=int(raw.get("quality") or 0),
            status=str(raw.get("status") or "OPEN"),
            tp1_hit=bool(raw.get("tp1_hit")),
            tp2_hit=bool(raw.get("tp2_hit")),
            tp3_hit=bool(raw.get("tp3_hit")),
            best_price=float(raw.get("best_price") or raw["entry"]),
            last_action=str(raw.get("last_action") or "OPEN"),
            last_msg_key=str(raw.get("last_msg_key") or ""),
            consecutive_stops=int(raw.get("consecutive_stops") or 0),
            notes=list(raw.get("notes") or []),
        )
    except Exception as e:
        print(f"[WARN] trade migration: {e}")
        return None

def store_trade(state, trade):
    state["active_trade"] = asdict(trade) if trade else None

def append_history(state, item):
    h = state.get("history") or []
    item["time"] = iso_now()
    h.append(item)
    state["history"] = h[-MAX_HISTORY:]

def journal_closed_trade(trade, result, context):
    return {
        "id":          trade.id,
        "opened_at":   trade.opened_at,
        "closed_at":   iso_now(),
        "side":        trade.side,
        "entry":       trade.entry,
        "close_price": rp(context["price"]),
        "stop_initial":trade.stop_initial,
        "tp1": trade.tp1, "tp2": trade.tp2, "tp3": trade.tp3,
        "quality":     trade.quality,
        "result":      result["action"],
        "result_pct":  round(result["current_pct"], 3),
        "leveraged_pct": round(result["current_pct"] * LEVERAGE, 2),
        "best_pct":    round(result["best_pct"], 3),
        "notes":       result.get("notes", []),
    }


# ══════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram не налаштований. Повідомлення:")
        print(message)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        print(f"Telegram: {r.status_code}")
    except Exception as e:
        print(f"[WARN] Telegram: {e}")


# ══════════════════════════════════════════════════════════════════════
# BUILD CONTEXT
# ══════════════════════════════════════════════════════════════════════

def build_context(data):
    """Assemble all layers into a single context dict."""
    tick  = data.get("ticker") or {}
    price = sf(tick.get("price"))

    c3   = data.get("candles_3m")  or []
    c15  = data.get("candles_15m") or []
    c1h  = data.get("candles_1h")  or []
    c4h  = data.get("candles_4h")  or []

    # L3
    regime = detect_regime(c15, "15m")

    # L4
    tf3  = tf_bias(c3,  "3m")
    tf15 = tf_bias(c15, "15m")
    tf1h = tf_bias(c1h, "1h")
    tf4h = tf_bias(c4h, "4h")
    align = tf_alignment(tf3, tf15, tf1h, tf4h)

    # L5
    structure = analyze_structure(c15, "15m")

    # L6
    flow = analyze_flow(data.get("trades") or [], data.get("book") or {}, price)

    # L7
    liquidity = analyze_liquidity(c3, c15, structure)

    # L8
    news_items = get_news()
    news       = analyze_news(news_items)
    calendar   = analyze_calendar()
    session    = market_session()

    price = price or sf(tf15.get("e20")) or 90.0

    return {
        "price":       price,
        "change24h":   sf(tick.get("change24h"), 0),
        "source":      tick.get("source", "OKX"),
        "regime":      regime,
        "tf3":         tf3,
        "tf15":        tf15,
        "tf1h":        tf1h,
        "tf4h":        tf4h,
        "tf_align":    align,
        "structure":   structure,
        "flow":        flow,
        "liquidity":   liquidity,
        "news":        news,
        "calendar":    calendar,
        "session":     session,
        "candles_15m": c15,
    }


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print("═" * 60)
    print("  BZU SIGNAL BOT v3 — START")
    print("═" * 60)

    state   = load_state()
    journal = load_journal()

    data    = collect_market_data()
    context = build_context(data)

    if not context.get("price"):
        print("[ERROR] Немає ціни — вихід")
        return

    regime_name = context["regime"].get("regime", "?")
    align_dir   = context["tf_align"]["direction"]
    print(f"Ціна: {context['price']} | Режим: {regime_name} | ТФ: {align_dir}")
    print(f"3m {context['tf3']['bias']} | 15m {context['tf15']['bias']} | "
          f"1h {context['tf1h']['bias']} | 4h {context['tf4h']['bias']}")
    print(f"Структура: {context['structure']['phase']} | "
          f"Ліквідність: {context['liquidity']['event']} | "
          f"Новини: {context['news']['bias']}")

    # ── Active trade management ───────────────────────────────────────
    active = trade_from_state(state)
    if active and active.status != "CLOSED":
        result  = manage_trade(active, context)
        message = build_follow_message(context, active, result)

        if result["closed"]:
            journal["trades"].append(journal_closed_trade(active, result, context))
            append_history(state, {
                "type":       "TRADE_CLOSED",
                "side":       active.side,
                "action":     result["action"],
                "price":      rp(context["price"]),
                "result_pct": round(result["current_pct"], 3),
            })
            if result["action"] == "STOP":
                state["last_stop_side"] = active.side
            store_trade(state, None)
        else:
            store_trade(state, active)
            append_history(state, {
                "type":         "FOLLOW",
                "side":         active.side,
                "action":       result["action"],
                "price":        rp(context["price"]),
                "result_pct":   round(result["current_pct"], 3),
                "stop_current": rp(active.stop_current),
            })

        journal["signals"].append({
            "time":   iso_now(),
            "type":   "FOLLOW" if not result["closed"] else "CLOSE",
            "side":   active.side,
            "action": result["action"],
            "price":  rp(context["price"]),
            "regime": regime_name,
        })

        save_state(state)
        save_journal(journal)
        send_telegram(message)
        print(f"ACTIVE TRADE MANAGED: {result['action']}")
        return

    # ── New setup evaluation ──────────────────────────────────────────
    setup   = evaluate_setup(context)
    message = build_entry_message(context, setup)

    if setup["action"] in ("ENTRY", "RISKY_ENTRY"):
        active = make_active_trade(setup)
        store_trade(state, active)
        state["last_stop_side"] = None   # reset on new entry
        append_history(state, {
            "type":    "ENTRY",
            "side":    setup["side"],
            "action":  setup["action"],
            "price":   rp(context["price"]),
            "quality": setup["quality"],
            "stop":    active.stop_current,
            "tp1":     active.tp1,
        })
    else:
        store_trade(state, None)
        append_history(state, {
            "type":    setup["action"],
            "side":    setup["side"],
            "price":   rp(context["price"]),
            "quality": setup["quality"],
            "reason":  setup["reason"],
        })

    journal["signals"].append({
        "time":    iso_now(),
        "type":    setup["action"],
        "side":    setup["side"],
        "price":   rp(context["price"]),
        "quality": setup["quality"],
        "reason":  setup["reason"],
        "plan":    asdict(setup["plan"]) if setup.get("plan") else None,
        "gates":   setup.get("gates", []),
        "regime":  regime_name,
        "tf_align": context["tf_align"],
        "structure_phase": context["structure"]["phase"],
        "news_bias": context["news"]["bias"],
        "news_score": context["news"]["score"],
    })

    save_state(state)
    save_journal(journal)
    send_telegram(message)
    print(f"SETUP: {setup['action']} {setup['side']} | якість {setup['quality']}/100")
    print("═" * 60)


if __name__ == "__main__":
    main()
