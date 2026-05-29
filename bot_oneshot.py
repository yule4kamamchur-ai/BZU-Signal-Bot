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
from statistics import mean
from urllib.parse import quote_plus

import requests


# ==========================================================
# BZU PROFESSIONAL SIGNAL BOT
# ==========================================================
# Core idea:
# 1. Price action is the base.
# 2. News is fuel/filter, not a standalone trade.
# 3. If the bot gives an entry, the next runs must manage that trade until
#    HOLD / PROTECT / EXIT / TP / STOP. A trade is never replaced by WAIT.
# ==========================================================


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
STATE_FILE = os.getenv("SIGNAL_MEMORY_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "last_signal.json"))
JOURNAL_FILE = os.getenv("SIGNAL_JOURNAL_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "signal_journal.json"))

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "10") or 10)
ENTRY_QUALITY_MIN = int(os.getenv("ENTRY_QUALITY_MIN", "68") or 68)
RISKY_QUALITY_MIN = int(os.getenv("RISKY_QUALITY_MIN", "62") or 62)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "80") or 80)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "500") or 500)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
UA_TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "UTC")


NEWS_QUERIES = [
    "Brent crude oil Reuters OPEC EIA inventories sanctions Hormuz",
    "oil prices Brent crude today Reuters energy",
    "EIA crude oil inventories API crude draw build",
    "OPEC production cut increase crude oil",
    "Iran sanctions Hormuz oil supply disruption",
    "Fed Powell dollar yields oil prices",
]

LONG_NEWS_PHRASES = [
    "inventory draw", "crude draw", "stockpiles fell", "stockpiles decline",
    "supply disruption", "supply risk", "hormuz", "new sanctions",
    "fresh sanctions", "opec cut", "production cut", "output cut",
    "attack", "strike", "war escalates", "talks fail",
    "запаси впали", "скорочення запасів", "нові санкції", "атака",
    "удар", "ормуз", "перебої постачання",
]

SHORT_NEWS_PHRASES = [
    "inventory build", "crude build", "stockpiles rose", "stockpiles rise",
    "ceasefire", "cease-fire", "truce", "peace deal", "talks progress",
    "sanctions relief", "sanctions lifted", "output increase",
    "production increase", "opec increase", "demand weak", "oversupply",
    "запаси зросли", "припинення вогню", "перемир", "мирна угода",
    "послаблення санкцій", "збільшення видобутку", "слабкий попит",
]

HIGH_IMPACT_WORDS = [
    "reuters", "eia", "api", "opec", "opec+", "fed", "powell", "cpi",
    "fomc", "iran", "hormuz", "sanctions", "inventory", "stockpiles",
    "brent", "crude", "oil", "tariff", "trump",
]


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class TradePlan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    risk_pct: float
    rr1: float
    rr2: float
    rr3: float
    invalidation: str


@dataclass
class ActiveTrade:
    id: str
    side: str
    opened_at: str
    entry: float
    stop_initial: float
    stop_current: float
    tp1: float
    tp2: float
    tp3: float
    quality: int
    status: str = "OPEN"
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    best_price: float = 0.0
    last_action: str = "OPEN"
    last_message_key: str = ""
    notes: list = field(default_factory=list)


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def round_price(value):
    if value is None:
        return None
    return round(float(value), 4)


def pct(a, b):
    if not b:
        return 0.0
    return (a - b) / b * 100.0


def signed_pct(side, entry, price):
    raw = pct(price, entry)
    return raw if side == "LONG" else -raw


def side_word(side):
    if side == "LONG":
        return "лонг"
    if side == "SHORT":
        return "шорт"
    return "нейтрально"


def opposite(side):
    return "SHORT" if side == "LONG" else "LONG"


def http_get(url, timeout=REQUEST_TIMEOUT, retries=2):
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Signal-Bot/2.0",
        "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html, */*",
    }
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code < 400:
                return response
            last_error = f"HTTP {response.status_code}"
        except Exception as error:
            last_error = str(error)
        time.sleep(0.4 * attempt)
    print(f"[WARN] GET failed: {url} | {last_error}")
    return None


def http_post(url, payload, timeout=REQUEST_TIMEOUT):
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Signal-Bot/2.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code < 400:
            return response
        print(f"[WARN] POST HTTP {response.status_code}: {url}")
    except Exception as error:
        print(f"[WARN] POST failed: {url} | {error}")
    return None


# ==========================================================
# STATE
# ==========================================================


def atomic_json_write(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    bak = path + ".bak"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with open(bak, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception as error:
        print(f"[WARN] JSON read failed {path}: {error}")
        return default


def load_state():
    state = load_json(STATE_FILE, {"version": "pro-v2", "active_trade": None, "history": []})
    if "active_trade" not in state:
        state["active_trade"] = None
    if "history" not in state or not isinstance(state["history"], list):
        state["history"] = []
    state["version"] = "pro-v2"
    return state


def save_state(state):
    state["updated_at"] = iso_now()
    state["history"] = (state.get("history") or [])[-MAX_HISTORY:]
    atomic_json_write(STATE_FILE, state)


def load_journal():
    journal = load_json(JOURNAL_FILE, {"version": "pro-v2", "trades": [], "signals": []})
    journal["version"] = "pro-v2"
    if "trades" not in journal or not isinstance(journal["trades"], list):
        journal["trades"] = []
    if "signals" not in journal or not isinstance(journal["signals"], list):
        journal["signals"] = []
    return journal


def save_journal(journal):
    journal["updated_at"] = iso_now()
    journal["trades"] = (journal.get("trades") or [])[-MAX_JOURNAL:]
    journal["signals"] = (journal.get("signals") or [])[-MAX_JOURNAL:]
    atomic_json_write(JOURNAL_FILE, journal)


def active_trade_from_state(state):
    raw = (state or {}).get("active_trade")
    if not isinstance(raw, dict):
        return None
    try:
        return ActiveTrade(
            id=str(raw.get("id") or uuid.uuid4().hex[:10]),
            side=str(raw.get("side")),
            opened_at=str(raw.get("opened_at") or iso_now()),
            entry=float(raw.get("entry")),
            stop_initial=float(raw.get("stop_initial", raw.get("stop_current"))),
            stop_current=float(raw.get("stop_current", raw.get("stop_initial"))),
            tp1=float(raw.get("tp1")),
            tp2=float(raw.get("tp2")),
            tp3=float(raw.get("tp3")),
            quality=int(raw.get("quality") or 0),
            status=str(raw.get("status") or "OPEN"),
            tp1_hit=bool(raw.get("tp1_hit")),
            tp2_hit=bool(raw.get("tp2_hit")),
            tp3_hit=bool(raw.get("tp3_hit")),
            best_price=float(raw.get("best_price") or raw.get("entry")),
            last_action=str(raw.get("last_action") or "OPEN"),
            last_message_key=str(raw.get("last_message_key") or ""),
            notes=list(raw.get("notes") or []),
        )
    except Exception as error:
        print(f"[WARN] active trade migration failed: {error}")
        return None


def store_active_trade(state, trade):
    state["active_trade"] = asdict(trade) if trade else None


def append_history(state, item):
    history = state.get("history") or []
    item["time"] = iso_now()
    history.append(item)
    state["history"] = history[-MAX_HISTORY:]


# ==========================================================
# MARKET DATA
# ==========================================================


def parse_okx_candles(rows):
    candles = []
    for row in rows or []:
        try:
            candles.append(Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5] or 0),
            ))
        except Exception:
            continue
    candles.sort(key=lambda c: c.ts)
    return candles


def get_okx_candles(bar="15m", limit=160):
    url = f"https://www.okx.com/api/v5/market/candles?instId={OKX_INST_ID}&bar={bar}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        data = response.json().get("data", [])
        return parse_okx_candles(data)
    except Exception as error:
        print(f"[WARN] OKX candles parse failed {bar}: {error}")
        return []


def get_okx_ticker():
    url = f"https://www.okx.com/api/v5/market/ticker?instId={OKX_INST_ID}"
    response = http_get(url)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        last = float(row.get("last") or row.get("lastSz") or 0)
        open24h = float(row.get("open24h") or 0)
        change = pct(last, open24h) if open24h else 0
        return {
            "price": last,
            "change24h": change,
            "volume24h": safe_float(row.get("volCcy24h"), 0),
            "source": "OKX",
            "symbol": OKX_INST_ID,
        }
    except Exception as error:
        print(f"[WARN] OKX ticker parse failed: {error}")
        return {}


def get_okx_trades(limit=120):
    url = f"https://www.okx.com/api/v5/market/trades?instId={OKX_INST_ID}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        trades = []
        for row in response.json().get("data", []):
            trades.append({
                "price": float(row.get("px") or 0),
                "size": float(row.get("sz") or 0),
                "side": str(row.get("side") or "").lower(),
                "ts": int(row.get("ts") or 0),
            })
        return trades
    except Exception as error:
        print(f"[WARN] OKX trades parse failed: {error}")
        return []


def get_okx_book(depth=50):
    url = f"https://www.okx.com/api/v5/market/books?instId={OKX_INST_ID}&sz={depth}"
    response = http_get(url)
    if not response:
        return {"bids": [], "asks": []}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {"bids": [], "asks": []}
        book = rows[0]
        bids = [(float(x[0]), float(x[1])) for x in book.get("bids", []) if len(x) >= 2]
        asks = [(float(x[0]), float(x[1])) for x in book.get("asks", []) if len(x) >= 2]
        return {"bids": bids, "asks": asks}
    except Exception as error:
        print(f"[WARN] OKX book parse failed: {error}")
        return {"bids": [], "asks": []}


def get_tradingview_price_fallback():
    url = "https://scanner.tradingview.com/crypto/scan"
    payload = {
        "symbols": {"tickers": ["BINANCE:BZUSDT.P", "BINANCE:BZUSDT"], "query": {"types": []}},
        "columns": ["close", "change", "volume"],
    }
    response = http_post(url, payload)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        values = rows[0].get("d") or []
        return {
            "price": float(values[0]),
            "change24h": safe_float(values[1], 0),
            "volume24h": safe_float(values[2], 0),
            "source": "TradingView",
            "symbol": rows[0].get("s", "BINANCE:BZUSDT.P"),
        }
    except Exception as error:
        print(f"[WARN] TradingView fallback parse failed: {error}")
        return {}


def collect_market_data():
    candles_3m = get_okx_candles("3m", 220)
    candles_15m = get_okx_candles("15m", 180)
    candles_1h = get_okx_candles("1H", 140)
    ticker = get_okx_ticker() or get_tradingview_price_fallback()
    if not ticker and candles_15m:
        ticker = {
            "price": candles_15m[-1].close,
            "change24h": pct(candles_15m[-1].close, candles_15m[0].open),
            "volume24h": 0,
            "source": "OKX candles",
            "symbol": OKX_INST_ID,
        }
    return {
        "ticker": ticker,
        "candles_3m": candles_3m,
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "trades": get_okx_trades(),
        "book": get_okx_book(),
    }


# ==========================================================
# INDICATORS / CONTEXT
# ==========================================================


def ema(values, period):
    values = [float(x) for x in values if x is not None]
    if not values:
        return None
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(values, period=14):
    values = [float(x) for x in values if x is not None]
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = mean(gains[-period:]) if gains[-period:] else 0
    avg_loss = mean(losses[-period:]) if losses[-period:] else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if not candles or len(candles) < 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    sample = trs[-period:]
    return mean(sample) if sample else None


def slope_pct(values, bars=12):
    values = [float(v) for v in values if v is not None]
    if len(values) < bars + 1:
        return 0.0
    start = mean(values[-bars - 1:-bars + 2])
    end = mean(values[-3:])
    return pct(end, start)


def candle_body_ratio(candle):
    rng = max(candle.high - candle.low, 1e-9)
    return abs(candle.close - candle.open) / rng


def close_location(candle):
    rng = max(candle.high - candle.low, 1e-9)
    return (candle.close - candle.low) / rng


def analyze_timeframe(candles, label):
    if not candles or len(candles) < 30:
        return {
            "label": label, "available": False, "bias": "NEUTRAL", "score": 0,
            "trend": "NO DATA", "note": "даних мало",
        }

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    last = candles[-1]
    ema20 = ema(closes[-60:], 20)
    ema50 = ema(closes[-80:], 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14) or max(last.close * 0.004, 0.01)
    slope = slope_pct(closes, 12)
    move_8 = pct(closes[-1], closes[-9]) if len(closes) >= 9 else 0
    range_20 = max(highs[-20:]) - min(lows[-20:])
    atr_range = range_20 / atr14 if atr14 else 0

    score = 0
    reasons = []

    if ema20 and ema50:
        if last.close > ema20 > ema50:
            score += 24
            reasons.append("ціна вище EMA20/50")
        elif last.close < ema20 < ema50:
            score -= 24
            reasons.append("ціна нижче EMA20/50")
        elif ema20 > ema50:
            score += 8
            reasons.append("EMA20 вище EMA50")
        elif ema20 < ema50:
            score -= 8
            reasons.append("EMA20 нижче EMA50")

    if slope >= 0.35:
        score += 18
        reasons.append("нахил вгору")
    elif slope <= -0.35:
        score -= 18
        reasons.append("нахил вниз")
    elif slope >= 0.12:
        score += 8
    elif slope <= -0.12:
        score -= 8

    if move_8 >= 0.45:
        score += 14
        reasons.append("імпульс вгору")
    elif move_8 <= -0.45:
        score -= 14
        reasons.append("імпульс вниз")

    if rsi14 >= 76:
        score -= 10
        reasons.append("перекупленість")
    elif rsi14 <= 24:
        score += 10
        reasons.append("перепроданість")

    if candle_body_ratio(last) >= 0.55:
        if last.close > last.open and close_location(last) >= 0.62:
            score += 7
        elif last.close < last.open and close_location(last) <= 0.38:
            score -= 7

    if atr_range < 2.3:
        score = int(score * 0.75)
        reasons.append("стиснення/боковик")

    if score >= 26:
        bias = "LONG"
    elif score <= -26:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    trend = bias
    if atr_range < 2.0:
        trend = "RANGE"

    return {
        "label": label,
        "available": True,
        "bias": bias,
        "score": int(score),
        "trend": trend,
        "close": last.close,
        "ema20": round_price(ema20),
        "ema50": round_price(ema50),
        "rsi": round(rsi14, 1),
        "atr": round_price(atr14),
        "slope_pct": round(slope, 3),
        "move_8_pct": round(move_8, 3),
        "range_atr": round(atr_range, 2),
        "note": "; ".join(reasons[:4]) if reasons else "без сильного перекосу",
    }


def swing_points(candles, lookback=2):
    highs = []
    lows = []
    if not candles or len(candles) < lookback * 2 + 8:
        return highs, lows
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        left = candles[i - lookback:i]
        right = candles[i + 1:i + 1 + lookback]
        if all(c.high > x.high for x in left + right):
            highs.append({"idx": i, "price": c.high, "ts": c.ts})
        if all(c.low < x.low for x in left + right):
            lows.append({"idx": i, "price": c.low, "ts": c.ts})
    return highs, lows


def analyze_structure(candles):
    if not candles or len(candles) < 35:
        return {"available": False, "bias": "NEUTRAL", "score": 0, "phase": "NO DATA", "note": "структура недоступна"}

    recent = candles[-32:]
    last = recent[-1]
    prev = recent[-2]
    highs, lows = swing_points(candles[-80:], 2)
    recent_high = max(c.high for c in recent[:-1])
    recent_low = min(c.low for c in recent[:-1])
    swing_high = highs[-1]["price"] if highs else recent_high
    swing_low = lows[-1]["price"] if lows else recent_low
    atr14 = atr(candles, 14) or last.close * 0.005

    score = 0
    notes = []
    phase = "RANGE / WAIT"

    if last.close > swing_high:
        score += 32
        phase = "BOS LONG"
        notes.append(f"закриття вище swing high {round_price(swing_high)}")
    elif last.close < swing_low:
        score -= 32
        phase = "BOS SHORT"
        notes.append(f"закриття нижче swing low {round_price(swing_low)}")

    if last.high > recent_high and last.close < recent_high:
        score -= 22
        phase = "UPSIDE SWEEP"
        notes.append("зняли ліквідність зверху і закрились нижче")
    elif last.low < recent_low and last.close > recent_low:
        score += 22
        phase = "DOWNSIDE SWEEP"
        notes.append("зняли ліквідність знизу і повернулись вище")

    mid = (recent_high + recent_low) / 2
    if prev.low <= recent_low + atr14 * 0.15 and last.close > mid:
        score += 16
        phase = "CHOCH LONG"
        notes.append("після зняття low повернення в діапазон")
    elif prev.high >= recent_high - atr14 * 0.15 and last.close < mid:
        score -= 16
        phase = "CHOCH SHORT"
        notes.append("після зняття high повернення в діапазон")

    last_range = max(last.high - last.low, 1e-9)
    body = abs(last.close - last.open)
    if body / last_range >= 0.6:
        if last.close > last.open:
            score += 8
            notes.append("сильне bullish-закриття")
        else:
            score -= 8
            notes.append("сильне bearish-закриття")

    if score >= 22:
        bias = "LONG"
    elif score <= -22:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "phase": phase,
        "swing_high": round_price(swing_high),
        "swing_low": round_price(swing_low),
        "recent_high": round_price(recent_high),
        "recent_low": round_price(recent_low),
        "atr": round_price(atr14),
        "note": "; ".join(notes[:3]) if notes else "структура без чистого пробою",
    }


def analyze_micro(candles):
    if not candles or len(candles) < 35:
        return {"available": False, "bias": "NEUTRAL", "score": 0, "state": "NO DATA", "note": "3m недоступний"}
    recent = candles[-28:]
    fast = candles[-12:]
    last = candles[-1]
    closes = [c.close for c in recent]
    fast_closes = [c.close for c in fast]
    fast_highs = [c.high for c in fast]
    fast_lows = [c.low for c in fast]
    fast_move = pct(fast_closes[-1], fast_closes[0])
    lower_closes = sum(1 for i in range(1, len(fast_closes)) if fast_closes[i] < fast_closes[i - 1])
    higher_closes = sum(1 for i in range(1, len(fast_closes)) if fast_closes[i] > fast_closes[i - 1])
    lower_highs = sum(1 for i in range(1, len(fast_highs)) if fast_highs[i] < fast_highs[i - 1])
    higher_lows = sum(1 for i in range(1, len(fast_lows)) if fast_lows[i] > fast_lows[i - 1])
    red = sum(1 for c in fast if c.close < c.open)
    green = sum(1 for c in fast if c.close > c.open)
    drift = slope_pct(closes, 10)
    avg_vol = mean([c.volume for c in recent[:-1]]) if len(recent) > 1 else 0
    vol_ratio = last.volume / avg_vol if avg_vol else 1

    score = 0
    notes = []
    if fast_move >= 0.28:
        score += 26
        notes.append("3m швидко йде вгору")
    elif fast_move <= -0.28:
        score -= 26
        notes.append("3m швидко йде вниз")

    if higher_closes >= 7 or higher_lows >= 5:
        score += 22
        notes.append("3m higher lows/closes")
    if lower_closes >= 7 or lower_highs >= 5:
        score -= 22
        notes.append("3m lower highs/closes")

    if green >= 8:
        score += 10
    elif red >= 8:
        score -= 10

    if drift >= 0.18:
        score += 10
    elif drift <= -0.18:
        score -= 10

    if vol_ratio >= 1.5 and candle_body_ratio(last) >= 0.45:
        if last.close > last.open:
            score += 8
            notes.append("обсяг за покупців")
        else:
            score -= 8
            notes.append("обсяг за продавців")

    if score >= 22:
        bias = "LONG"
        state = "LONG_STRENGTHENING"
    elif score <= -22:
        bias = "SHORT"
        state = "SHORT_STRENGTHENING"
    else:
        bias = "NEUTRAL"
        state = "RANGE"
        total_move = pct(closes[-1], closes[0])
        if total_move >= 0.45 and drift < 0:
            state = "LONG_COOLING"
            notes.append("покупці охолоджуються")
        elif total_move <= -0.45 and drift > 0:
            state = "SHORT_COOLING"
            notes.append("продавці охолоджуються")

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "state": state,
        "fast_move_pct": round(fast_move, 3),
        "drift_pct": round(drift, 3),
        "vol_ratio": round(vol_ratio, 2),
        "note": "; ".join(notes[:3]) if notes else "3m без чіткого тригера",
    }


def analyze_flow(trades, book, price):
    buy_vol = sum(t["size"] for t in trades if t.get("side") == "buy")
    sell_vol = sum(t["size"] for t in trades if t.get("side") == "sell")
    total = buy_vol + sell_vol
    delta = (buy_vol - sell_vol) / total * 100 if total else 0

    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    near_bid = near_ask = 0
    wall = ""
    if bids and asks and price:
        lower = price * 0.996
        upper = price * 1.004
        near_bids = [(p, s) for p, s in bids if p >= lower]
        near_asks = [(p, s) for p, s in asks if p <= upper]
        near_bid = sum(s for _, s in near_bids)
        near_ask = sum(s for _, s in near_asks)
        biggest_bid = max(near_bids, key=lambda x: x[1]) if near_bids else None
        biggest_ask = max(near_asks, key=lambda x: x[1]) if near_asks else None
        if biggest_bid and biggest_ask:
            if biggest_bid[1] > biggest_ask[1] * 1.8:
                wall = f"підтримка покупців біля {round_price(biggest_bid[0])}"
            elif biggest_ask[1] > biggest_bid[1] * 1.8:
                wall = f"опір продавців біля {round_price(biggest_ask[0])}"

    book_total = near_bid + near_ask
    book_delta = (near_bid - near_ask) / book_total * 100 if book_total else 0
    score = 0
    notes = []
    if delta >= 18:
        score += 16
        notes.append("угоди за покупців")
    elif delta <= -18:
        score -= 16
        notes.append("угоди за продавців")
    elif delta >= 8:
        score += 7
    elif delta <= -8:
        score -= 7

    if book_delta >= 18:
        score += 10
        notes.append("стакан підтримує покупців")
    elif book_delta <= -18:
        score -= 10
        notes.append("стакан тисне продавцями")
    if wall:
        notes.append(wall)

    if score >= 15:
        bias = "LONG"
    elif score <= -15:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "trade_delta_pct": round(delta, 2),
        "book_delta_pct": round(book_delta, 2),
        "note": "; ".join(notes[:3]) if notes else "потік без явної переваги",
    }


def market_session():
    hour = now_utc().hour
    if 12 <= hour < 20:
        return {"name": "NEW YORK / LONDON", "score": 4, "note": "ліквідна сесія"}
    if 7 <= hour < 12:
        return {"name": "LONDON", "score": 2, "note": "європейська сесія"}
    return {"name": "ASIA / QUIET", "score": -3, "note": "тихіша ліквідність"}


# ==========================================================
# NEWS
# ==========================================================


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalized(text):
    return clean_text(text).lower().replace("’", "'").replace("–", "-").replace("—", "-")


def parse_google_rss(query, lookback_hours=3):
    url = f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{lookback_hours}h')}&hl=en-US&gl=US&ceid=US:en"
    response = http_get(url, timeout=10, retries=1)
    if not response:
        return []
    cutoff = now_utc() - timedelta(hours=lookback_hours)
    items = []
    try:
        root = ET.fromstring(response.content)
        for node in root.findall(".//item")[:12]:
            title = clean_text(node.findtext("title", ""))
            link = node.findtext("link", "") or ""
            dt = parse_date(node.findtext("pubDate", ""))
            if dt and dt < cutoff:
                continue
            if title:
                items.append({"title": title, "link": link, "published_at": dt, "source": "Google News"})
    except Exception as error:
        print(f"[WARN] Google RSS parse failed: {error}")
    return items


def dedupe_news(items):
    seen = set()
    out = []
    for item in items:
        key = re.sub(r"[^a-z0-9а-яіїєґ]+", " ", normalized(item.get("title", "")))[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: x.get("published_at") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return out


def get_news():
    items = []
    for query in NEWS_QUERIES:
        items.extend(parse_google_rss(query, 3))
    return dedupe_news(items)


def score_news_item(title):
    text = normalized(title)
    long_hits = sum(1 for p in LONG_NEWS_PHRASES if p in text)
    short_hits = sum(1 for p in SHORT_NEWS_PHRASES if p in text)
    impact = sum(1 for p in HIGH_IMPACT_WORDS if p in text)

    score = 0
    if long_hits:
        score += long_hits * (12 + min(6, impact))
    if short_hits:
        score -= short_hits * (12 + min(6, impact))
    if "reuters" in text:
        score = int(score * 1.2)
    return score, impact


def analyze_news(items):
    raw = 0
    important = []
    long_count = 0
    short_count = 0
    for item in items[:35]:
        item_score, impact = score_news_item(item.get("title", ""))
        raw += item_score
        if item_score > 0:
            long_count += 1
        elif item_score < 0:
            short_count += 1
        if impact or abs(item_score) >= 12:
            important.append({**item, "score": item_score})
    score = max(-45, min(45, int(raw)))
    if score >= 18:
        bias = "LONG"
    elif score <= -18:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
    top = important[0]["title"] if important else "свіжого сильного драйвера немає"
    return {
        "bias": bias,
        "score": score,
        "raw_score": raw,
        "total": len(items),
        "long_count": long_count,
        "short_count": short_count,
        "important": important[:5],
        "top": top,
        "note": f"{side_word(bias)} ({score}); {top}",
    }


# ==========================================================
# SETUP ENGINE
# ==========================================================


def build_context(data):
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price"))
    tf3 = analyze_micro(data.get("candles_3m") or [])
    tf15 = analyze_timeframe(data.get("candles_15m") or [], "15m")
    tf1h = analyze_timeframe(data.get("candles_1h") or [], "1h")
    structure = analyze_structure(data.get("candles_15m") or [])
    flow = analyze_flow(data.get("trades") or [], data.get("book") or {}, price)
    news_items = get_news()
    news = analyze_news(news_items)
    session = market_session()

    price = price or safe_float(tf15.get("close"))
    atr15 = safe_float(structure.get("atr")) or safe_float(tf15.get("atr")) or (price or 90) * 0.006

    tech_score = (
        tf15.get("score", 0) * 1.15
        + tf1h.get("score", 0) * 0.85
        + tf3.get("score", 0) * 0.55
        + structure.get("score", 0) * 1.00
        + flow.get("score", 0) * 0.55
    )
    total_score = tech_score + news.get("score", 0) * 0.35 + session.get("score", 0)

    if total_score >= 42:
        bias = "LONG"
    elif total_score <= -42:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "price": price,
        "change24h": safe_float(ticker.get("change24h"), 0),
        "source": ticker.get("source", "unknown"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "atr15": atr15,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "structure": structure,
        "flow": flow,
        "news": news,
        "session": session,
        "tech_score": int(tech_score),
        "total_score": int(total_score),
        "bias": bias,
    }


def side_score(value, side):
    if side == "LONG":
        return value
    if side == "SHORT":
        return -value
    return 0


def is_late_chase(side, context):
    price = context["price"]
    atr15 = context["atr15"]
    tf15 = context["tf15"]
    tf3 = context["tf3"]
    structure = context["structure"]
    ema20 = safe_float(tf15.get("ema20"))
    rsi15 = safe_float(tf15.get("rsi"), 50)
    move_8 = safe_float(tf15.get("move_8_pct"), 0)

    if not price or not atr15:
        return False, ""

    distance_atr = abs(price - ema20) / atr15 if ema20 else 0
    if side == "LONG":
        if rsi15 >= 74 and move_8 >= 0.9:
            return True, "лонг після сильного імпульсу: чекати відкат/ретест"
        if distance_atr >= 1.75 and tf3.get("state") != "LONG_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3m не підсилює"
        if structure.get("phase") == "UPSIDE SWEEP":
            return True, "зверху зняли ліквідність, лонг не доганяти"
    else:
        if rsi15 <= 26 and move_8 <= -0.9:
            return True, "шорт після сильного падіння: чекати відкат/ретест"
        if distance_atr >= 1.75 and tf3.get("state") != "SHORT_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3m не підсилює"
        if structure.get("phase") == "DOWNSIDE SWEEP":
            return True, "знизу зняли ліквідність, шорт не доганяти"
    return False, ""


def entry_confirmations(side, context):
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    structure = context["structure"]
    flow = context["flow"]
    news = context["news"]

    confirmations = []
    conflicts = []

    if tf15.get("bias") == side:
        confirmations.append("15m за напрямом")
    elif tf15.get("bias") == opposite(side):
        conflicts.append("15m проти")

    if tf1h.get("bias") == side:
        confirmations.append("1h підтримує")
    elif tf1h.get("bias") == opposite(side):
        conflicts.append("1h проти")

    if tf3.get("bias") == side:
        confirmations.append("3m дав тригер")
    elif tf3.get("bias") == opposite(side):
        conflicts.append("3m проти")

    if structure.get("bias") == side:
        confirmations.append("структура підтверджує")
    elif structure.get("bias") == opposite(side):
        conflicts.append("структура проти")

    if flow.get("bias") == side:
        confirmations.append("угоди/стакан підтримують")
    elif flow.get("bias") == opposite(side):
        conflicts.append("потік проти")

    if news.get("bias") == side:
        confirmations.append("новини дають паливо")
    elif news.get("bias") == opposite(side) and abs(news.get("score", 0)) >= 28:
        conflicts.append("сильні новини проти")

    return confirmations, conflicts


def make_plan(side, context):
    price = context["price"]
    atr15 = context["atr15"] or price * 0.006
    structure = context["structure"]
    swing_low = safe_float(structure.get("swing_low")) or price - atr15
    swing_high = safe_float(structure.get("swing_high")) or price + atr15

    min_risk = max(atr15 * 0.65, price * 0.0035)
    max_risk = max(atr15 * 1.45, price * 0.0125)
    buffer = max(atr15 * 0.18, price * 0.0012)

    if side == "LONG":
        raw_stop = min(swing_low - buffer, price - min_risk)
        risk = min(max(price - raw_stop, min_risk), max_risk)
        stop = price - risk
        tp1 = price + risk * 1.25
        tp2 = price + risk * 2.05
        tp3 = price + risk * 3.10
        invalidation = f"15m закриття нижче {round_price(stop)} або злам 3m/структури проти LONG"
    else:
        raw_stop = max(swing_high + buffer, price + min_risk)
        risk = min(max(raw_stop - price, min_risk), max_risk)
        stop = price + risk
        tp1 = price - risk * 1.25
        tp2 = price - risk * 2.05
        tp3 = price - risk * 3.10
        invalidation = f"15m закриття вище {round_price(stop)} або злам 3m/структури проти SHORT"

    risk_pct = abs(stop - price) / price * 100 if price else 0
    return TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1),
        tp2=round_price(tp2),
        tp3=round_price(tp3),
        risk_pct=round(risk_pct, 3),
        rr1=1.25,
        rr2=2.05,
        rr3=3.10,
        invalidation=invalidation,
    )


def evaluate_new_setup(context):
    side = context["bias"]
    if side not in ["LONG", "SHORT"] or not context.get("price"):
        return {
            "action": "NO_TRADE",
            "side": "NEUTRAL",
            "quality": 0,
            "title": "ВХОДУ НЕМАЄ",
            "reason": "перевага нечітка, немає професійного входу",
            "plan": None,
            "confirmations": [],
            "conflicts": [],
        }

    confirmations, conflicts = entry_confirmations(side, context)
    late, late_reason = is_late_chase(side, context)
    plan = make_plan(side, context)

    score_for_side = side_score(context["total_score"], side)
    quality = 48 + min(18, score_for_side // 5) + len(confirmations) * 5 - len(conflicts) * 8

    tf3 = context["tf3"]
    tf15 = context["tf15"]
    structure = context["structure"]
    flow = context["flow"]

    trigger_ok = (
        tf3.get("bias") == side
        and tf15.get("bias") in [side, "NEUTRAL"]
        and structure.get("bias") in [side, "NEUTRAL"]
        and flow.get("bias") != opposite(side)
    )
    trend_ok = tf15.get("bias") == side or (tf15.get("bias") == "NEUTRAL" and structure.get("bias") == side)
    hard_conflict = len(conflicts) >= 3 or "сильні новини проти" in conflicts

    if late:
        quality = min(quality, 54)
    if hard_conflict:
        quality = min(quality, 54)
    if not trigger_ok:
        quality = min(quality, 61)
    if not trend_ok:
        quality = min(quality, 58)

    quality = int(max(0, min(88, quality)))

    if late:
        return {
            "action": "WAIT_RETEST",
            "side": side,
            "quality": quality,
            "title": f"ЧЕКАТИ — {side} НЕ ДОГАНЯТИ",
            "reason": late_reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

    if quality >= ENTRY_QUALITY_MIN and trigger_ok and not hard_conflict:
        return {
            "action": "ENTRY",
            "side": side,
            "quality": quality,
            "title": f"ВХІД Є — {side}",
            "reason": "сигнал підтверджений: " + " | ".join(confirmations[:4]),
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

    if quality >= RISKY_QUALITY_MIN and trigger_ok and not hard_conflict:
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": "є ранній тригер, але підтверджень ще не максимум: " + " | ".join(confirmations[:4]),
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

    return {
        "action": "WATCH",
        "side": side,
        "quality": quality,
        "title": f"ЧЕКАТИ — ГОТУЄМОСЬ ДО {side}",
        "reason": "напрям є, але входу ще немає: " + ("; ".join(conflicts[:3]) if conflicts else "бракує 3m/структурного тригера"),
        "plan": plan,
        "confirmations": confirmations,
        "conflicts": conflicts,
    }


# ==========================================================
# ACTIVE TRADE MANAGEMENT
# ==========================================================


def trade_hit_level(side, price, level):
    if level is None:
        return False
    return price >= level if side == "LONG" else price <= level


def trade_hit_stop(side, price, stop):
    if stop is None:
        return False
    return price <= stop if side == "LONG" else price >= stop


def best_trade_price(side, trade, current_price):
    if not trade.best_price:
        return current_price
    if side == "LONG":
        return max(trade.best_price, current_price)
    return min(trade.best_price, current_price)


def active_trade_message_key(trade, action):
    return f"{trade.id}:{action}:{trade.tp1_hit}:{trade.tp2_hit}:{trade.tp3_hit}:{round_price(trade.stop_current)}"


def manage_active_trade(trade, context):
    price = context["price"]
    side = trade.side
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    structure = context["structure"]
    flow = context["flow"]

    trade.best_price = best_trade_price(side, trade, price)
    current_pct = signed_pct(side, trade.entry, price)
    best_pct = signed_pct(side, trade.entry, trade.best_price)
    giveback = max(0, best_pct - current_pct)
    giveback_ratio = giveback / best_pct if best_pct > 0 else 0

    notes = []
    action = "HOLD"
    title = f"СУПРОВІД {side}"
    recommendation = "утримувати, поки 3m/15m не ламаються"

    if trade_hit_stop(side, price, trade.stop_current):
        trade.status = "CLOSED"
        trade.last_action = "STOP"
        return {
            "closed": True,
            "action": "STOP",
            "title": f"УГОДУ {side} ЗАКРИТО — STOP",
            "recommendation": "стоп/зона зламу пробита, сценарій закрито",
            "current_pct": current_pct,
            "best_pct": best_pct,
            "notes": [f"ціна {round_price(price)} проти стопу {round_price(trade.stop_current)}"],
        }

    if trade_hit_level(side, price, trade.tp3):
        trade.tp3_hit = True
        trade.status = "CLOSED"
        trade.last_action = "TP3"
        return {
            "closed": True,
            "action": "TP3",
            "title": f"УГОДУ {side} ЗАКРИТО — TP3",
            "recommendation": "основна ціль виконана, сценарій закрито",
            "current_pct": current_pct,
            "best_pct": best_pct,
            "notes": [f"TP3 {round_price(trade.tp3)} взято"],
        }

    if trade_hit_level(side, price, trade.tp2):
        trade.tp2_hit = True
        action = "TP2_PROTECT"
        recommendation = "TP2 взято: зафіксувати ще частину, залишок вести трейлінгом"
        if side == "LONG":
            trade.stop_current = max(trade.stop_current, trade.tp1)
        else:
            trade.stop_current = min(trade.stop_current, trade.tp1)
        notes.append(f"стоп підтягнути до TP1 {round_price(trade.tp1)}")
    elif trade_hit_level(side, price, trade.tp1):
        trade.tp1_hit = True
        action = "TP1_PROTECT"
        recommendation = "TP1 взято: частково фіксувати, стоп у б/у або малий плюс"
        be = trade.entry * (1.0008 if side == "LONG" else 0.9992)
        if side == "LONG":
            trade.stop_current = max(trade.stop_current, be)
        else:
            trade.stop_current = min(trade.stop_current, be)
        notes.append(f"стоп у б/у біля {round_price(trade.stop_current)}")

    opposite_votes = 0
    support_votes = 0
    for block in [tf3, tf15, structure, flow]:
        if block.get("bias") == side:
            support_votes += 1
        elif block.get("bias") == opposite(side):
            opposite_votes += 1

    near_entry = abs(current_pct) <= 0.18
    lost_after_profit = best_pct >= 0.38 and giveback_ratio >= 0.62 and current_pct <= 0.18

    if lost_after_profit and opposite_votes >= 2:
        trade.status = "CLOSED"
        trade.last_action = "EXIT_GIVEBACK"
        return {
            "closed": True,
            "action": "EXIT",
            "title": f"УГОДУ {side} ЗАКРИТО — АКТУАЛЬНІСТЬ ВТРАЧЕНА",
            "recommendation": "рух у плюс майже віддали назад, підтвердження зникло",
            "current_pct": current_pct,
            "best_pct": best_pct,
            "notes": ["краще закрити біля входу / не чекати дальній стоп"],
        }

    if near_entry and opposite_votes >= 2 and support_votes <= 1:
        action = "PROTECT_OR_EXIT"
        recommendation = "ціна біля входу, підтвердження слабке: захистити позицію або закрити біля входу"
        notes.append("не усереднювати; чекати нового тригера після виходу")
    elif current_pct < -0.28 and opposite_votes >= 2:
        action = "EXIT_WARNING"
        recommendation = "позиція під тиском, структура проти: краще виходити раніше, не чекати дальній стоп"
        notes.append("злам 3m/структури проти позиції")
    elif current_pct > 0 and tf3.get("bias") == opposite(side):
        action = "PROTECT"
        recommendation = "позиція у плюсі, але 3m слабшає: підтягнути стоп"
        notes.append("прибуток не віддавати назад")
    elif action == "HOLD":
        if current_pct > 0:
            recommendation = "утримувати, напрям ще працює"
        else:
            recommendation = "тримати тільки поки 15m/3m не зламаються; стоп обов'язковий"

    trade.last_action = action
    key = active_trade_message_key(trade, action)
    trade.last_message_key = key
    return {
        "closed": False,
        "action": action,
        "title": title,
        "recommendation": recommendation,
        "current_pct": current_pct,
        "best_pct": best_pct,
        "notes": notes,
    }


def new_active_trade(setup):
    plan = setup["plan"]
    return ActiveTrade(
        id=uuid.uuid4().hex[:10],
        side=setup["side"],
        opened_at=iso_now(),
        entry=plan.entry,
        stop_initial=plan.stop,
        stop_current=plan.stop,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        quality=setup["quality"],
        best_price=plan.entry,
        notes=[setup["reason"]],
    )


# ==========================================================
# MESSAGES
# ==========================================================


def price_line(context):
    return f"<b>Ціна:</b> {round_price(context['price'])} | 24h {round(context.get('change24h', 0), 3)}% | {context.get('source')}"


def context_lines(context):
    return [
        f"<b>15m:</b> {side_word(context['tf15'].get('bias'))} ({context['tf15'].get('score')}) — {context['tf15'].get('note')}",
        f"<b>1h:</b> {side_word(context['tf1h'].get('bias'))} ({context['tf1h'].get('score')}) — {context['tf1h'].get('note')}",
        f"<b>3m:</b> {side_word(context['tf3'].get('bias'))} ({context['tf3'].get('score')}) — {context['tf3'].get('note')}",
        f"<b>Структура:</b> {side_word(context['structure'].get('bias'))} — {context['structure'].get('phase')} | {context['structure'].get('note')}",
        f"<b>Потік:</b> {side_word(context['flow'].get('bias'))} ({context['flow'].get('score')}) — {context['flow'].get('note')}",
        f"<b>Новини:</b> {side_word(context['news'].get('bias'))} ({context['news'].get('score')}) — {context['news'].get('top')[:150]}",
    ]


def plan_text(plan):
    if not plan:
        return "плану немає"
    return (
        f"Вхід {plan.entry} | Стоп {plan.stop} | TP1 {plan.tp1} | "
        f"TP2 {plan.tp2} | TP3 {plan.tp3} | ризик {plan.risk_pct}%"
    )


def build_new_setup_message(context, setup):
    plan = setup.get("plan")
    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        f"<b>{setup['title']}</b>",
        "",
        price_line(context),
        f"<b>Якість:</b> {setup['quality']}/100",
        f"<b>Причина:</b> {setup['reason']}",
    ]
    if setup["action"] in ["ENTRY", "RISKY_ENTRY"]:
        lines.append(f"<b>План:</b> {plan_text(plan)}")
        lines.append(f"<b>Скасування:</b> {plan.invalidation}")
        if setup["action"] == "RISKY_ENTRY":
            lines.append("<b>Режим:</b> малий ризик; якщо 3m одразу піде проти — не тримати до дальнього стопу.")
    elif setup["action"] == "WAIT_RETEST":
        lines.append(f"<b>Зона:</b> напрям {setup['side']} є, але вхід тільки після відкату/ретесту. Орієнтир плану: {plan_text(plan)}")
    elif setup["side"] in ["LONG", "SHORT"] and plan:
        lines.append(f"<b>Що чекати:</b> {setup['side']} тільки після 3m-тригера і підтвердження структури. Орієнтир: {plan_text(plan)}")

    if setup.get("conflicts"):
        lines.append("<b>Ризики:</b> " + " | ".join(setup["conflicts"][:3]))

    lines.extend(context_lines(context)[:4])
    return "\n".join(lines).strip()


def build_follow_message(context, trade, result):
    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        f"<b>{result['title']}</b> — {result['recommendation']}",
        "",
        price_line(context),
        f"<b>Стан:</b> від входу {round(result['current_pct'], 3)}% | максимум у плюс {round(result['best_pct'], 3)}%",
        (
            f"<b>Позиція:</b> Вхід {round_price(trade.entry)} | Стоп зараз {round_price(trade.stop_current)} "
            f"| TP1 {round_price(trade.tp1)} | TP2 {round_price(trade.tp2)} | TP3 {round_price(trade.tp3)}"
        ),
        f"<b>TP:</b> TP1 {'так' if trade.tp1_hit else 'ні'} | TP2 {'так' if trade.tp2_hit else 'ні'} | TP3 {'так' if trade.tp3_hit else 'ні'}",
    ]
    if result.get("notes"):
        lines.append("<b>Дія:</b> " + " | ".join(result["notes"][:3]))
    lines.extend(context_lines(context)[:5])
    return "\n".join(lines).strip()


def build_closed_trade_journal_item(trade, result, context):
    return {
        "id": trade.id,
        "opened_at": trade.opened_at,
        "closed_at": iso_now(),
        "side": trade.side,
        "entry": trade.entry,
        "close_price": round_price(context["price"]),
        "stop_initial": trade.stop_initial,
        "stop_final": round_price(trade.stop_current),
        "tp1": trade.tp1,
        "tp2": trade.tp2,
        "tp3": trade.tp3,
        "quality": trade.quality,
        "result_action": result["action"],
        "result_pct": round(result["current_pct"], 3),
        "leveraged_pct": round(result["current_pct"] * LEVERAGE, 2),
        "best_pct": round(result["best_pct"], 3),
        "notes": result.get("notes", []),
    }


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram token/chat missing. Message:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        print(f"Telegram status: {response.status_code}")
        print(response.text[:300])
    except Exception as error:
        print(f"[WARN] Telegram send failed: {error}")


# ==========================================================
# MAIN
# ==========================================================


def main():
    print("START BZU SIGNAL BOT PRO")
    state = load_state()
    journal = load_journal()

    data = collect_market_data()
    context = build_context(data)
    if not context.get("price"):
        print("NO PRICE DATA")
        return

    print(f"PRICE {context['price']} | BIAS {context['bias']} | TECH {context['tech_score']} | TOTAL {context['total_score']}")
    print(f"15m {context['tf15'].get('bias')} {context['tf15'].get('score')} | 3m {context['tf3'].get('bias')} {context['tf3'].get('score')}")
    print(f"STRUCTURE {context['structure'].get('phase')} {context['structure'].get('bias')} | FLOW {context['flow'].get('bias')} | NEWS {context['news'].get('bias')}")

    active = active_trade_from_state(state)
    if active and active.status != "CLOSED":
        result = manage_active_trade(active, context)
        message = build_follow_message(context, active, result)
        if result.get("closed"):
            journal["trades"].append(build_closed_trade_journal_item(active, result, context))
            append_history(state, {
                "type": "TRADE_CLOSED",
                "side": active.side,
                "action": result["action"],
                "price": round_price(context["price"]),
                "result_pct": round(result["current_pct"], 3),
            })
            store_active_trade(state, None)
        else:
            store_active_trade(state, active)
            append_history(state, {
                "type": "FOLLOW",
                "side": active.side,
                "action": result["action"],
                "price": round_price(context["price"]),
                "result_pct": round(result["current_pct"], 3),
                "stop_current": round_price(active.stop_current),
            })
        journal["signals"].append({
            "time": iso_now(),
            "type": "FOLLOW" if not result.get("closed") else "CLOSE",
            "side": active.side,
            "action": result["action"],
            "price": round_price(context["price"]),
            "quality": active.quality,
            "context_bias": context["bias"],
            "tech_score": context["tech_score"],
            "total_score": context["total_score"],
        })
        save_state(state)
        save_journal(journal)
        send_telegram(message)
        print("BOT COMPLETE: ACTIVE TRADE MANAGED")
        return

    setup = evaluate_new_setup(context)
    message = build_new_setup_message(context, setup)

    if setup["action"] in ["ENTRY", "RISKY_ENTRY"]:
        active = new_active_trade(setup)
        store_active_trade(state, active)
        append_history(state, {
            "type": "ENTRY",
            "side": setup["side"],
            "action": setup["action"],
            "price": round_price(context["price"]),
            "quality": setup["quality"],
            "entry": active.entry,
            "stop": active.stop_current,
            "tp1": active.tp1,
            "tp2": active.tp2,
            "tp3": active.tp3,
        })
    else:
        store_active_trade(state, None)
        append_history(state, {
            "type": setup["action"],
            "side": setup["side"],
            "price": round_price(context["price"]),
            "quality": setup["quality"],
            "reason": setup["reason"],
        })

    journal["signals"].append({
        "time": iso_now(),
        "type": setup["action"],
        "side": setup["side"],
        "price": round_price(context["price"]),
        "quality": setup["quality"],
        "reason": setup["reason"],
        "plan": asdict(setup["plan"]) if setup.get("plan") else None,
        "confirmations": setup.get("confirmations", []),
        "conflicts": setup.get("conflicts", []),
        "context": {
            "bias": context["bias"],
            "tech_score": context["tech_score"],
            "total_score": context["total_score"],
            "tf3": context["tf3"],
            "tf15": context["tf15"],
            "tf1h": context["tf1h"],
            "structure": context["structure"],
            "flow": context["flow"],
            "news": {
                "bias": context["news"]["bias"],
                "score": context["news"]["score"],
                "top": context["news"]["top"],
                "total": context["news"]["total"],
            },
        },
    })

    save_state(state)
    save_journal(journal)
    send_telegram(message)
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
