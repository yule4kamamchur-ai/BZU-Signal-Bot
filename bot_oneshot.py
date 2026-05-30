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
from zoneinfo import ZoneInfo

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
BTC_INST_ID = os.getenv("BTC_INST_ID", "BTC-USDT-SWAP")
STATE_FILE = os.getenv("SIGNAL_MEMORY_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "last_signal.json"))
JOURNAL_FILE = os.getenv("SIGNAL_JOURNAL_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "signal_journal.json"))

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "10") or 10)
ENTRY_QUALITY_MIN = int(os.getenv("ENTRY_QUALITY_MIN", "68") or 68)
RISKY_QUALITY_MIN = int(os.getenv("RISKY_QUALITY_MIN", "62") or 62)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "80") or 80)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "500") or 500)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
UA_TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "UTC")

EIA_WPSR_URL = "https://www.eia.gov/petroleum/supply/weekly/index.php"
EIA_WPSR_SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
IMPORTANT_EVENT_WINDOW_MINUTES = int(os.getenv("IMPORTANT_EVENT_WINDOW_MINUTES", "60") or 60)
MANUAL_IMPORTANT_EVENTS = os.getenv("IMPORTANT_EVENTS", "")

# BZU is volatile. The trade plan should not use tiny scalp targets.
# For entry near 92 this gives roughly:
# LONG  stop not farther than ~90.05, TP1 not closer than ~94.00.
# SHORT stop not farther than ~93.95, TP1 not closer than ~90.05.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "2.12") or 2.12)
MIN_TP1_DISTANCE_PCT = float(os.getenv("MIN_TP1_DISTANCE_PCT", "2.18") or 2.18)
MIN_TP2_DISTANCE_PCT = float(os.getenv("MIN_TP2_DISTANCE_PCT", "3.30") or 3.30)
MIN_TP3_DISTANCE_PCT = float(os.getenv("MIN_TP3_DISTANCE_PCT", "5.00") or 5.00)

# Intraday filters
# Volume is a bonus only. Low volume must NOT block entries for BZ intraday,
# because compression often happens before the best impulse move.
VOLUME_ACTIVE_RATIO = float(os.getenv("VOLUME_ACTIVE_RATIO", "1.30") or 1.30)
VOLUME_STRONG_RATIO = float(os.getenv("VOLUME_STRONG_RATIO", "1.70") or 1.70)
CVD_STATE_MAX_AGE_MINUTES = int(os.getenv("CVD_STATE_MAX_AGE_MINUTES", "360") or 360)
# Reuters is priority, but not the only source. EIA/Fed/OPEC/event headlines remain useful.
REUTERS_PRIORITY_NEWS = os.getenv("REUTERS_PRIORITY_NEWS", "1").lower() not in ["0", "false", "no"]


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


def eastern_tz():
    return ZoneInfo("America/New_York")


def et_to_utc(date_obj, hour, minute=0):
    return datetime(date_obj.year, date_obj.month, date_obj.day, hour, minute, tzinfo=eastern_tz()).astimezone(timezone.utc)


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
    state["version"] = "pro-v2-intraday-architecture"
    return state


def save_state(state):
    state["updated_at"] = iso_now()
    state["history"] = (state.get("history") or [])[-MAX_HISTORY:]
    atomic_json_write(STATE_FILE, state)


def load_journal():
    journal = load_json(JOURNAL_FILE, {"version": "pro-v2", "trades": [], "signals": []})
    journal["version"] = "pro-v2-intraday-architecture"
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


def get_okx_candles(bar="15m", limit=160, inst_id=None):
    inst_id = inst_id or OKX_INST_ID
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        data = response.json().get("data", [])
        return parse_okx_candles(data)
    except Exception as error:
        print(f"[WARN] OKX candles parse failed {bar}: {error}")
        return []


def get_okx_ticker(inst_id=None):
    inst_id = inst_id or OKX_INST_ID
    url = f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}"
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
            "symbol": inst_id,
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


def get_okx_open_interest():
    """Current futures/swap open interest from OKX.

    This is used as a derivatives confirmation layer:
    - price up + OI up = stronger real LONG participation
    - price down + OI up = stronger real SHORT participation
    - price moves with OI down = mostly closing/liquidation, do not chase late
    """
    url = f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={OKX_INST_ID}"
    response = http_get(url, timeout=8, retries=1)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        return {
            "oi": safe_float(row.get("oi"), 0),
            "oi_ccy": safe_float(row.get("oiCcy"), 0),
            "ts": int(row.get("ts") or 0),
            "source": "OKX open interest",
        }
    except Exception as error:
        print(f"[WARN] OKX open interest parse failed: {error}")
        return {}


def get_okx_funding_rate():
    """Current funding rate from OKX.

    Funding is not a standalone entry. It is a risk/positioning filter:
    very positive funding warns against chasing LONG; very negative funding warns
    against chasing SHORT, especially after a squeeze/liquidation move.
    """
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={OKX_INST_ID}"
    response = http_get(url, timeout=8, retries=1)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        return {
            "funding_rate": safe_float(row.get("fundingRate"), 0),
            "next_funding_rate": safe_float(row.get("nextFundingRate"), None),
            "funding_time": int(row.get("fundingTime") or 0),
            "source": "OKX funding",
        }
    except Exception as error:
        print(f"[WARN] OKX funding parse failed: {error}")
        return {}


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
    candles_4h = get_okx_candles("4H", 140)
    btc_candles_3m = get_okx_candles("3m", 120, BTC_INST_ID)
    btc_candles_15m = get_okx_candles("15m", 120, BTC_INST_ID)
    btc_candles_1h = get_okx_candles("1H", 80, BTC_INST_ID)
    btc_ticker = get_okx_ticker(BTC_INST_ID)
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
        "candles_4h": candles_4h,
        "btc_ticker": btc_ticker,
        "btc_candles_3m": btc_candles_3m,
        "btc_candles_15m": btc_candles_15m,
        "btc_candles_1h": btc_candles_1h,
        "trades": get_okx_trades(),
        "book": get_okx_book(),
        "open_interest": get_okx_open_interest(),
        "funding": get_okx_funding_rate(),
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
    """TradingView-style RSI using Wilder/RMA smoothing.

    The previous version used a simple average of the last N gains/losses.
    TradingView's default RSI uses Wilder smoothing, so this keeps bot RSI
    closer to the chart the user watches.
    """
    values = [float(x) for x in values if x is not None]
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    if len(gains) < period:
        return 50.0

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder RMA: next = (prev * (period - 1) + current) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
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



# ==========================================================
# ICT INTRADAY MODEL
# ==========================================================

def _candle_dir(c):
    if c.close > c.open:
        return "UP"
    if c.close < c.open:
        return "DOWN"
    return "DOJI"


def _zone_contains(zone, price, pad=0.0):
    if not zone or price is None:
        return False
    low = min(zone.get("low", 0), zone.get("high", 0)) - pad
    high = max(zone.get("low", 0), zone.get("high", 0)) + pad
    return low <= price <= high


def _nearest_zone(zones, price):
    if not zones or price is None:
        return None
    return min(zones, key=lambda z: abs(((z["low"] + z["high"]) / 2) - price))


def detect_fvg_zones(candles, lookback=42):
    """ICT Fair Value Gap / imbalance detector.

    Bullish FVG: candle[i-2].high < candle[i].low.
    Bearish FVG: candle[i-2].low > candle[i].high.

    The bot uses FVG as a retracement destination, not as a reason to chase.
    """
    zones = []
    if not candles or len(candles) < 8:
        return zones
    sample = candles[-lookback:]
    for i in range(2, len(sample)):
        c0 = sample[i - 2]
        c1 = sample[i - 1]
        c2 = sample[i]
        mid_body = abs(c1.close - c1.open)
        avg_body = mean([abs(x.close - x.open) for x in sample[max(0, i - 10):i]]) if i > 2 else mid_body
        displacement = mid_body >= max(avg_body * 1.25, 1e-9)

        if c0.high < c2.low:
            zones.append({
                "side": "LONG",
                "low": round_price(c0.high),
                "high": round_price(c2.low),
                "mid": round_price((c0.high + c2.low) / 2),
                "created_ts": c2.ts,
                "displacement": displacement,
                "type": "BULLISH_FVG",
            })
        if c0.low > c2.high:
            zones.append({
                "side": "SHORT",
                "low": round_price(c2.high),
                "high": round_price(c0.low),
                "mid": round_price((c2.high + c0.low) / 2),
                "created_ts": c2.ts,
                "displacement": displacement,
                "type": "BEARISH_FVG",
            })
    return zones[-12:]


def detect_orderblock_zone(candles, side, lookback=34):
    """Simple ICT orderblock proxy.

    LONG: last down candle before bullish displacement.
    SHORT: last up candle before bearish displacement.
    Zone is candle range with the midpoint emphasized for lower risk.
    """
    if not candles or len(candles) < 12:
        return None
    sample = candles[-lookback:]
    bodies = [abs(c.close - c.open) for c in sample]
    avg_body = mean(bodies[-20:]) if len(bodies) >= 20 else mean(bodies)
    for idx in range(len(sample) - 3, 2, -1):
        c = sample[idx]
        next2 = sample[idx + 1:min(len(sample), idx + 4)]
        if not next2:
            continue
        if side == "LONG" and c.close < c.open:
            displacement = max(x.close for x in next2) > c.high and any(abs(x.close - x.open) >= avg_body * 1.15 and x.close > x.open for x in next2)
            if displacement:
                return {
                    "side": "LONG",
                    "low": round_price(c.low),
                    "high": round_price(c.high),
                    "mid": round_price((c.low + c.high) / 2),
                    "created_ts": c.ts,
                    "type": "BULLISH_OB",
                }
        if side == "SHORT" and c.close > c.open:
            displacement = min(x.close for x in next2) < c.low and any(abs(x.close - x.open) >= avg_body * 1.15 and x.close < x.open for x in next2)
            if displacement:
                return {
                    "side": "SHORT",
                    "low": round_price(c.low),
                    "high": round_price(c.high),
                    "mid": round_price((c.low + c.high) / 2),
                    "created_ts": c.ts,
                    "type": "BEARISH_OB",
                }
    return None




# ==========================================================
# ICT PERSISTENT ZONES / MITIGATION
# ==========================================================

def _zone_key(zone):
    if not zone:
        return ""
    return f"{zone.get('side')}:{zone.get('type')}:{round_price(zone.get('low'))}:{round_price(zone.get('high'))}:{zone.get('created_ts', '')}"


def _normalize_zone(zone):
    if not zone:
        return None
    try:
        low = safe_float(zone.get("low"))
        high = safe_float(zone.get("high"))
        if low is None or high is None:
            return None
        low, high = min(low, high), max(low, high)
        return {
            "side": str(zone.get("side", "NEUTRAL")),
            "type": str(zone.get("type", "ZONE")),
            "low": round_price(low),
            "high": round_price(high),
            "mid": round_price(safe_float(zone.get("mid"), (low + high) / 2)),
            "created_ts": int(zone.get("created_ts") or 0),
            "displacement": bool(zone.get("displacement", False)),
            "mitigated": bool(zone.get("mitigated", False)),
            "last_seen": iso_now(),
        }
    except Exception:
        return None


def load_ict_memory(state):
    raw = (state or {}).get("ict_memory") or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("fvg_zones", [])
    raw.setdefault("ob_zones", [])
    raw.setdefault("mitigated_ob_keys", [])
    raw.setdefault("mitigated_fvg_keys", [])
    return raw


def _merge_zone_history(existing, fresh, max_items=32):
    merged = {}
    for zone in existing or []:
        nz = _normalize_zone(zone)
        if nz:
            merged[_zone_key(nz)] = nz
    for zone in fresh or []:
        nz = _normalize_zone(zone)
        if nz:
            key = _zone_key(nz)
            if key in merged:
                nz["mitigated"] = bool(merged[key].get("mitigated", False))
            merged[key] = nz
    zones = list(merged.values())
    zones.sort(key=lambda z: z.get("created_ts", 0), reverse=True)
    return zones[:max_items]


def _mark_mitigated_zones(zones, candles, side=None):
    """Mark FVG/OB zones as mitigated if price entered and then closed away.

    LONG zone mitigation:
      - candle trades into zone
      - later closes above zone high
    SHORT zone mitigation:
      - candle trades into zone
      - later closes below zone low

    Once mitigated, it is not used for a fresh signal again.
    """
    if not zones or not candles:
        return zones
    out = []
    recent = candles[-45:]
    for z in zones:
        nz = dict(z)
        if side and nz.get("side") != side:
            out.append(nz)
            continue
        low = safe_float(nz.get("low"))
        high = safe_float(nz.get("high"))
        if low is None or high is None:
            out.append(nz)
            continue
        low, high = min(low, high), max(low, high)
        touched = False
        mitigated = bool(nz.get("mitigated", False))
        for c in recent:
            if c.high >= low and c.low <= high:
                touched = True
            if touched:
                if nz.get("side") == "LONG" and c.close > high:
                    mitigated = True
                    break
                if nz.get("side") == "SHORT" and c.close < low:
                    mitigated = True
                    break
        nz["mitigated"] = mitigated
        out.append(nz)
    return out


def update_ict_memory(state, context, candles_15m):
    """Persist FVG/OB zones so pending ICT zones are stable between runs."""
    memory = load_ict_memory(state)
    ict = context.get("ict") or {}

    fresh_fvgs = []
    for key in ["bull_fvg", "bear_fvg"]:
        if ict.get(key):
            fresh_fvgs.append(ict[key])

    fresh_obs = []
    for key in ["bull_ob", "bear_ob"]:
        if ict.get(key):
            fresh_obs.append(ict[key])

    memory["fvg_zones"] = _merge_zone_history(memory.get("fvg_zones", []), fresh_fvgs, 36)
    memory["ob_zones"] = _merge_zone_history(memory.get("ob_zones", []), fresh_obs, 24)

    memory["fvg_zones"] = _mark_mitigated_zones(memory["fvg_zones"], candles_15m)
    memory["ob_zones"] = _mark_mitigated_zones(memory["ob_zones"], candles_15m)

    memory["mitigated_ob_keys"] = [_zone_key(z) for z in memory["ob_zones"] if z.get("mitigated")][-60:]
    memory["mitigated_fvg_keys"] = [_zone_key(z) for z in memory["fvg_zones"] if z.get("mitigated")][-80:]
    memory["updated_at"] = iso_now()
    state["ict_memory"] = memory
    return memory


def apply_ict_memory_to_context(context, memory):
    """Attach persistent zones to context and avoid reusing mitigated OBs/FVGs."""
    ict = context.get("ict") or {}
    memory = memory or {}

    fvg_zones = [z for z in memory.get("fvg_zones", []) if not z.get("mitigated")]
    ob_zones = [z for z in memory.get("ob_zones", []) if not z.get("mitigated")]

    price = context.get("price")
    if price:
        bull_fvgs = [z for z in fvg_zones if z.get("side") == "LONG"]
        bear_fvgs = [z for z in fvg_zones if z.get("side") == "SHORT"]
        bull_obs = [z for z in ob_zones if z.get("side") == "LONG"]
        bear_obs = [z for z in ob_zones if z.get("side") == "SHORT"]

        # Use persistent zone if current one is missing or already mitigated.
        if not ict.get("bull_fvg") and bull_fvgs:
            ict["bull_fvg"] = _nearest_zone(bull_fvgs, price)
        if not ict.get("bear_fvg") and bear_fvgs:
            ict["bear_fvg"] = _nearest_zone(bear_fvgs, price)
        if not ict.get("bull_ob") and bull_obs:
            ict["bull_ob"] = _nearest_zone(bull_obs, price)
        if not ict.get("bear_ob") and bear_obs:
            ict["bear_ob"] = _nearest_zone(bear_obs, price)

    ict["persistent_fvg_count"] = len(fvg_zones)
    ict["persistent_ob_count"] = len(ob_zones)
    context["ict"] = ict
    context["ict_memory"] = {
        "active_fvg": len(fvg_zones),
        "active_ob": len(ob_zones),
        "mitigated_ob": len(memory.get("mitigated_ob_keys", [])),
        "mitigated_fvg": len(memory.get("mitigated_fvg_keys", [])),
    }
    return context

def analyze_ict_model(candles_3m, candles_15m, structure, price):
    """ICT intraday decision layer.

    Important safety rule:
    ICT direction is NOT assigned from premium/discount alone.
    It needs an actual model:
    - liquidity sweep + reclaim
    - BOS/MSS + retracement into FVG/OB
    - clear FVG/OB reaction zone

    If price is in the middle of the dealing range and no model is active,
    return NEUTRAL instead of forcing SHORT/LONG.
    """
    if not candles_15m or len(candles_15m) < 45 or not price:
        return {
            "available": False,
            "bias": "NEUTRAL",
            "score": 0,
            "state": "NO_DATA",
            "setup": "NONE",
            "entry_ok": False,
            "no_chase": False,
            "note": "ICT: даних мало",
        }

    recent = candles_15m[-48:]
    last = recent[-1]
    atr15 = atr(candles_15m, 14) or max(price * 0.005, 0.01)

    # Use completed candles for the dealing range so current candle does not
    # falsely create a sweep signal.
    range_sample = recent[:-1]
    range_high = max(c.high for c in range_sample)
    range_low = min(c.low for c in range_sample)
    eq = (range_high + range_low) / 2
    range_size = max(range_high - range_low, atr15)
    pd_pos = (price - range_low) / range_size

    discount = pd_pos <= 0.45
    premium = pd_pos >= 0.55
    midrange = 0.45 < pd_pos < 0.55

    fvg15 = detect_fvg_zones(candles_15m, 42)
    fvg3 = detect_fvg_zones(candles_3m or [], 55)
    bullish_fvgs = [z for z in (fvg15 + fvg3) if z["side"] == "LONG"]
    bearish_fvgs = [z for z in (fvg15 + fvg3) if z["side"] == "SHORT"]
    nearest_bull_fvg = _nearest_zone(bullish_fvgs, price)
    nearest_bear_fvg = _nearest_zone(bearish_fvgs, price)

    bull_ob = detect_orderblock_zone(candles_15m, "LONG") or detect_orderblock_zone(candles_3m or [], "LONG")
    bear_ob = detect_orderblock_zone(candles_15m, "SHORT") or detect_orderblock_zone(candles_3m or [], "SHORT")

    pad = atr15 * 0.14
    in_bull_fvg = _zone_contains(nearest_bull_fvg, price, pad)
    in_bear_fvg = _zone_contains(nearest_bear_fvg, price, pad)
    in_bull_ob = _zone_contains(bull_ob, price, pad)
    in_bear_ob = _zone_contains(bear_ob, price, pad)

    phase = (structure or {}).get("phase", "")
    bos_long = phase == "BOS LONG"
    bos_short = phase == "BOS SHORT"
    choch_long = phase in ["CHOCH LONG", "DOWNSIDE SWEEP"]
    choch_short = phase in ["CHOCH SHORT", "UPSIDE SWEEP"]

    # Sweep must close back inside the previous range.
    swept_low = last.low < range_low and last.close > range_low
    swept_high = last.high > range_high and last.close < range_high

    move_3 = pct(last.close, recent[-4].close) if len(recent) >= 4 else 0
    body = abs(last.close - last.open)
    avg_body = mean([abs(c.close - c.open) for c in recent[-21:-1]]) if len(recent) >= 22 else body
    displacement_up = last.close > last.open and body >= max(avg_body * 1.45, atr15 * 0.36)
    displacement_down = last.close < last.open and body >= max(avg_body * 1.45, atr15 * 0.36)

    highs_near_above = sum(1 for c in recent[-28:-1] if price < c.high <= price + atr15 * 1.25)
    lows_near_below = sum(1 for c in recent[-28:-1] if price - atr15 * 1.25 <= c.low < price)

    score = 0
    notes = []
    setup = "NONE"
    bias = "NEUTRAL"
    entry_ok = False
    no_chase = False

    # 1) Liquidity sweep model.
    if swept_low or choch_long:
        score = 26
        bias = "LONG"
        setup = "LIQUIDITY_SWEEP_LONG"
        notes.append("sell-side liquidity sweep + reclaim")
        if discount:
            score += 7
            notes.append("discount")
        if highs_near_above <= 3:
            score += 5
            notes.append("шлях вгору з меншим опором")
        entry_ok = True

    elif swept_high or choch_short:
        score = -26
        bias = "SHORT"
        setup = "LIQUIDITY_SWEEP_SHORT"
        notes.append("buy-side liquidity sweep + reclaim вниз")
        if premium:
            score -= 7
            notes.append("premium")
        if lows_near_below <= 3:
            score -= 5
            notes.append("шлях вниз з меншим опором")
        entry_ok = True

    # 2) Continuation after BOS: only entry if price returns to FVG/OB.
    if bos_long:
        local = 14
        if in_bull_fvg or in_bull_ob:
            local += 18
            entry_ok = True
            setup = "BOS_LONG_RETRACE_FVG_OB"
            notes.append("BOS LONG + повернення у bullish FVG/OB")
        elif displacement_up and move_3 > 0.55:
            no_chase = True
            local -= 10
            setup = "BOS_LONG_WAIT_PULLBACK"
            notes.append("BOS LONG, але імпульс вже відбувся — чекати FVG/OB")
        if discount:
            local += 4
        score += local
        if score > 0:
            bias = "LONG"

    if bos_short:
        local = -14
        if in_bear_fvg or in_bear_ob:
            local -= 18
            entry_ok = True
            setup = "BOS_SHORT_RETRACE_FVG_OB"
            notes.append("BOS SHORT + повернення у bearish FVG/OB")
        elif displacement_down and move_3 < -0.55:
            no_chase = True
            local += 10
            setup = "BOS_SHORT_WAIT_PULLBACK"
            notes.append("BOS SHORT, але імпульс вже відбувся — чекати FVG/OB")
        if premium:
            local -= 4
        score += local
        if score < 0:
            bias = "SHORT"

    # 3) FVG/OB reaction model without fresh BOS is weaker.
    # It must be in correct premium/discount area. Midrange alone is no trade.
    if bias == "NEUTRAL":
        if (in_bull_fvg or in_bull_ob) and discount:
            bias = "LONG"
            score = 16
            setup = "DISCOUNT_FVG_OB_LONG"
            entry_ok = True
            notes.append("discount + bullish FVG/OB")
        elif (in_bear_fvg or in_bear_ob) and premium:
            bias = "SHORT"
            score = -16
            setup = "PREMIUM_FVG_OB_SHORT"
            entry_ok = True
            notes.append("premium + bearish FVG/OB")
        elif midrange:
            bias = "NEUTRAL"
            score = 0
            setup = "BALANCE_MIDRANGE"
            notes.append("ціна біля equilibrium — ICT входу немає")
        elif discount:
            bias = "NEUTRAL"
            score = 0
            setup = "DISCOUNT_CONTEXT"
            notes.append("discount є, але немає sweep/FVG/OB реакції")
        elif premium:
            bias = "NEUTRAL"
            score = 0
            setup = "PREMIUM_CONTEXT"
            notes.append("premium є, але немає sweep/FVG/OB реакції")

    # Context penalty only. Premium/discount cannot create direction by itself.
    if bias == "LONG" and premium and not (swept_low or in_bull_fvg or in_bull_ob):
        score -= 6
        notes.append("лонг у premium — якість нижча")
    if bias == "SHORT" and discount and not (swept_high or in_bear_fvg or in_bear_ob):
        score += 6
        notes.append("шорт у discount — якість нижча")

    if score >= 16:
        bias = "LONG"
    elif score <= -16:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
        if setup == "NONE":
            setup = "CONTEXT_ONLY"

    return {
        "available": True,
        "bias": bias,
        "score": int(max(-38, min(38, score))),
        "state": "NO_CHASE" if no_chase else ("ENTRY_MODEL" if entry_ok else "CONTEXT"),
        "setup": setup,
        "entry_ok": bool(entry_ok and not no_chase),
        "no_chase": bool(no_chase),
        "equilibrium": round_price(eq),
        "range_high": round_price(range_high),
        "range_low": round_price(range_low),
        "premium_discount": "MIDRANGE" if midrange else ("DISCOUNT" if discount else "PREMIUM"),
        "pd_pos": round(pd_pos, 3),
        "bull_fvg": nearest_bull_fvg,
        "bear_fvg": nearest_bear_fvg,
        "bull_ob": bull_ob,
        "bear_ob": bear_ob,
        "note": "; ".join(notes[:4]) if notes else "ICT: контекст без готового сетапу",
    }

def build_pending_ict_zones(context):
    """Create forward-looking ICT zones for the next runs.

    These are not market orders. They are zones where the bot should watch for
    reaction/confirmation, so the message becomes predictive instead of late.
    """
    ict = context.get("ict") or {}
    price = context.get("price")
    atr15 = context.get("atr15") or (price or 90) * 0.006
    side = context.get("bias", "NEUTRAL")
    if not ict or not price:
        return []

    zones = []

    def add_zone(side_, zone, label, priority, condition, reason):
        if not zone or zone.get("mitigated"):
            return
        low = safe_float(zone.get("low"))
        high = safe_float(zone.get("high"))
        mid = safe_float(zone.get("mid"), (low + high) / 2 if low and high else None)
        if low is None or high is None:
            return
        low, high = min(low, high), max(low, high)
        distance_pct = abs(((low + high) / 2) - price) / price * 100 if price else 0
        zones.append({
            "side": side_,
            "type": label,
            "low": round_price(low),
            "high": round_price(high),
            "mid": round_price(mid),
            "priority": int(priority),
            "distance_pct": round(distance_pct, 3),
            "condition": condition,
            "reason": reason,
        })

    premium_discount = ict.get("premium_discount", "")
    ict_bias = ict.get("bias", "NEUTRAL")

    # Main forward zones from ICT model.
    add_zone(
        "LONG",
        ict.get("bull_fvg"),
        "Bullish FVG",
        82 if premium_discount == "DISCOUNT" else 58,
        "реакція в зоні + 3M перестає робити lower low + CVD/потік не проти",
        "покупка не після імпульсу, а на поверненні в imbalance",
    )
    add_zone(
        "LONG",
        ict.get("bull_ob"),
        "Bullish OB",
        78 if premium_discount == "DISCOUNT" else 64,
        "утримання OB + reclaim середини зони + стоп нижче OB",
        "order block для low-risk LONG",
    )
    add_zone(
        "SHORT",
        ict.get("bear_fvg"),
        "Bearish FVG",
        82 if premium_discount == "PREMIUM" else 58,
        "реакція в зоні + 3M перестає робити higher high + CVD/потік не проти",
        "продаж не після падіння, а на поверненні в imbalance",
    )
    add_zone(
        "SHORT",
        ict.get("bear_ob"),
        "Bearish OB",
        78 if premium_discount == "PREMIUM" else 64,
        "утримання OB + rejection середини зони + стоп вище OB",
        "order block для low-risk SHORT",
    )

    # Liquidity objectives / warning zones.
    eq = safe_float(ict.get("equilibrium"))
    rh = safe_float(ict.get("range_high"))
    rl = safe_float(ict.get("range_low"))
    if rh and rl and eq:
        if price < eq:
            zones.append({
                "side": "LONG",
                "type": "Discount → EQ",
                "low": round_price(rl),
                "high": round_price(eq),
                "mid": round_price((rl + eq) / 2),
                "priority": 58,
                "distance_pct": round(abs(eq - price) / price * 100, 3),
                "condition": "після sweep low або реакції в bullish FVG/OB",
                "reason": "ціна у discount, перша магніт-зона — equilibrium",
            })
        if price > eq:
            zones.append({
                "side": "SHORT",
                "type": "Premium → EQ",
                "low": round_price(eq),
                "high": round_price(rh),
                "mid": round_price((rh + eq) / 2),
                "priority": 58,
                "distance_pct": round(abs(price - eq) / price * 100, 3),
                "condition": "після sweep high або реакції в bearish FVG/OB",
                "reason": "ціна у premium, перша магніт-зона — equilibrium",
            })

    # Prefer zones aligned with current context, but keep opposite zones as warnings.
    for z in zones:
        if side in ["LONG", "SHORT"] and z["side"] == side:
            z["priority"] += 8
        elif ict_bias in ["LONG", "SHORT"] and z["side"] == ict_bias:
            z["priority"] += 5

        # Do not give high priority to zones that are too far for intraday.
        if z["distance_pct"] > 1.35:
            z["priority"] -= 12
        elif z["distance_pct"] <= 0.45:
            z["priority"] += 4

    zones = sorted(zones, key=lambda z: (z["priority"], -z["distance_pct"]), reverse=True)
    # Deduplicate similar zones.
    unique = []
    for z in zones:
        duplicate = False
        for u in unique:
            if z["side"] == u["side"] and abs((z["mid"] or 0) - (u["mid"] or 0)) <= atr15 * 0.12:
                duplicate = True
                break
        if not duplicate:
            unique.append(z)
    return unique[:4]


def pending_zones_text(context, limit=3):
    zones = context.get("pending_ict_zones") or []
    if not zones:
        return ""
    lines = ["<b>Pending ICT зони:</b>"]
    for z in zones[:limit]:
        side = z["side"]
        icon = "🟢" if side == "LONG" else "🔴"
        lines.append(
            f"{icon} {side}: {z['low']}–{z['high']} | {z['type']} | P{z['priority']}"
        )
    return "\n".join(lines)

def analyze_volume_guard(candles_15m):
    """Intraday volume context.

    Low volume is NOT a blocker. For BZ, quiet compression often comes right
    before a strong intraday move. Volume only adds quality when participation
    is clearly above normal.
    """
    if not candles_15m or len(candles_15m) < 23:
        return {
            "available": False,
            "ok": True,
            "score": 0,
            "ratio": 1.0,
            "state": "NO DATA",
            "note": "обсяг: даних мало",
        }

    vols = [max(0.0, float(c.volume or 0)) for c in candles_15m]
    avg20 = mean(vols[-23:-3]) if len(vols[-23:-3]) else 0
    avg3 = mean(vols[-3:]) if len(vols[-3:]) else 0
    ratio = (avg3 / avg20) if avg20 else 1.0

    if ratio >= VOLUME_STRONG_RATIO:
        score = 10
        state = "STRONG_VOLUME"
        note = f"обсяг сильний: {round(ratio * 100, 1)}% від середнього 20"
    elif ratio >= VOLUME_ACTIVE_RATIO:
        score = 6
        state = "ACTIVE_VOLUME"
        note = f"обсяг активний: {round(ratio * 100, 1)}% від середнього 20"
    else:
        score = 0
        state = "LOW_OR_NORMAL_VOLUME" if ratio < 1.0 else "NORMAL_VOLUME"
        note = f"обсяг без бонусу: {round(ratio * 100, 1)}% від середнього 20"

    return {
        "available": True,
        "ok": True,
        "score": score,
        "ratio": round(ratio, 3),
        "state": state,
        "note": note,
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



def analyze_cvd(trades, candles_3m, price, previous_snapshot=None):
    """Persistent CVD proxy from OKX public trades.

    Public trades are limited, so every run adds the current trade delta to the
    stored CVD in state. To reduce double-counting overlapping trades between
    15-minute runs, only trades newer than the previous saved trade timestamp
    are added when timestamps are available.
    """
    previous_snapshot = previous_snapshot or {}
    prev_cvd = safe_float(previous_snapshot.get("cvd"), 0) or 0
    prev_trade_ts = int(previous_snapshot.get("last_trade_ts") or 0)
    prev_cvd_time = previous_snapshot.get("cvd_time") or previous_snapshot.get("time")

    cvd_stale = False
    try:
        if prev_cvd_time:
            dt = datetime.fromisoformat(str(prev_cvd_time).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (now_utc() - dt.astimezone(timezone.utc)).total_seconds() / 60
            cvd_stale = age_min > CVD_STATE_MAX_AGE_MINUTES
    except Exception:
        cvd_stale = False

    if cvd_stale:
        prev_cvd = 0
        prev_trade_ts = 0

    fresh_trades = []
    for t in trades or []:
        ts = int(t.get("ts") or 0)
        if prev_trade_ts and ts and ts <= prev_trade_ts:
            continue
        fresh_trades.append(t)
    if not fresh_trades and trades:
        fresh_trades = trades

    buy_vol = sum(t.get("size", 0) for t in fresh_trades if t.get("side") == "buy")
    sell_vol = sum(t.get("size", 0) for t in fresh_trades if t.get("side") == "sell")
    delta = buy_vol - sell_vol
    total = buy_vol + sell_vol
    delta_pct = (delta / total * 100) if total else 0
    cvd_value = prev_cvd + delta
    last_trade_ts = max([int(t.get("ts") or 0) for t in trades or []] or [prev_trade_ts])

    price_move = 0
    if candles_3m and len(candles_3m) >= 10:
        price_move = pct(candles_3m[-1].close, candles_3m[-10].close)

    cvd_change = delta
    cvd_change_pct = 0
    if abs(prev_cvd) > 1e-9:
        cvd_change_pct = cvd_change / abs(prev_cvd) * 100

    score = 0
    state = "BALANCED"
    notes = []

    if delta_pct >= 18:
        score += 14
        state = "BUYERS_DOMINATE"
        notes.append("CVD росте")
    elif delta_pct <= -18:
        score -= 14
        state = "SELLERS_DOMINATE"
        notes.append("CVD падає")

    if price_move <= -0.35 and delta_pct >= 10:
        score += 18
        state = "BULLISH_ABSORPTION"
        notes.append("ціна вниз, CVD вгору")
    elif price_move >= 0.35 and delta_pct <= -10:
        score -= 18
        state = "BEARISH_ABSORPTION"
        notes.append("ціна вгору, CVD вниз")

    if score >= 16:
        bias = "LONG"
    elif score <= -16:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "state": state,
        "delta": round(delta, 4),
        "delta_pct": round(delta_pct, 2),
        "cvd": round(cvd_value, 4),
        "cvd_change_pct": round(cvd_change_pct, 3),
        "last_trade_ts": last_trade_ts,
        "price_move_pct": round(price_move, 3),
        "note": "; ".join(notes[:3]) if notes else "CVD без явної переваги",
    }


def analyze_clusters(trades, book, price):
    """Lightweight cluster / footprint proxy for intraday BZ trading.

    Important: this is NOT a real footprint chart. It uses public OKX trades and
    the order book as a local microstructure hint. Therefore clusters are a
    small helper, not a trade blocker. They can warn about a local buyer/seller,
    but they must not overrule 15m structure, 3m trigger, CVD, or OI.
    """
    if not trades or not price:
        return {"bias": "NEUTRAL", "score": 0, "state": "NO DATA", "zones": [], "note": "кластерних даних мало"}

    bin_size = max(price * 0.0008, 0.01)
    clusters = {}
    for t in trades:
        px = safe_float(t.get("price"), None)
        size = safe_float(t.get("size"), 0) or 0
        side = t.get("side")
        if px is None or abs(px - price) / price > 0.012:
            continue
        level = round(round(px / bin_size) * bin_size, 4)
        row = clusters.setdefault(level, {"buy": 0.0, "sell": 0.0, "total": 0.0})
        if side == "buy":
            row["buy"] += size
        elif side == "sell":
            row["sell"] += size
        row["total"] += size

    if not clusters:
        return {"bias": "NEUTRAL", "score": 0, "state": "NO CLUSTERS", "zones": [], "note": "немає активних кластерів біля ціни"}

    zones = []
    for level, row in clusters.items():
        total = row["total"]
        if total <= 0:
            continue
        imbalance = (row["buy"] - row["sell"]) / total * 100
        if abs(imbalance) >= 22:
            zones.append({"price": level, "imbalance_pct": round(imbalance, 1), "volume": round(total, 4)})
    zones.sort(key=lambda x: x["volume"], reverse=True)

    top = zones[0] if zones else None
    score = 0
    state = "BALANCED"
    notes = []
    if top:
        below = top["price"] <= price
        imb = top["imbalance_pct"]
        # Cluster influence is intentionally capped. It is a warning/helper.
        if below and imb > 0:
            score += 5
            state = "LOCAL_BUYER_SUPPORT"
            notes.append(f"локальний покупець біля {top['price']}")
        elif (not below) and imb < 0:
            score -= 5
            state = "LOCAL_SELLER_RESISTANCE"
            notes.append(f"локальний продавець біля {top['price']}")
        elif below and imb < 0:
            score -= 3
            state = "LOCAL_SELL_PRESSURE_BELOW"
            notes.append("локальні продажі нижче ціни")
        elif (not below) and imb > 0:
            score += 3
            state = "LOCAL_BUY_PRESSURE_ABOVE"
            notes.append("локальні покупки вище ціни")

    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    if bids and asks:
        bid_wall = max(bids[:15], key=lambda x: x[1], default=None)
        ask_wall = max(asks[:15], key=lambda x: x[1], default=None)
        if bid_wall and ask_wall:
            if bid_wall[1] > ask_wall[1] * 2:
                score += 2
                notes.append(f"bid-стіна {round_price(bid_wall[0])}")
            elif ask_wall[1] > bid_wall[1] * 2:
                score -= 2
                notes.append(f"ask-стіна {round_price(ask_wall[0])}")

    score = max(-8, min(8, int(score)))
    if score >= 5:
        bias = "LONG"
    elif score <= -5:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": score,
        "state": state,
        "zones": zones[:3],
        "note": "; ".join(notes[:3]) if notes else "кластери без явної переваги",
    }

def analyze_derivatives(open_interest, funding, price, previous_snapshot=None):
    previous_snapshot = previous_snapshot or {}
    oi_value = safe_float((open_interest or {}).get("oi_ccy"), None)
    if not oi_value:
        oi_value = safe_float((open_interest or {}).get("oi"), None)
    prev_oi = safe_float(previous_snapshot.get("oi"), None)
    prev_price = safe_float(previous_snapshot.get("price"), None)
    funding_rate = safe_float((funding or {}).get("funding_rate"), 0) or 0

    oi_change = pct(oi_value, prev_oi) if oi_value and prev_oi else 0
    price_change = pct(price, prev_price) if price and prev_price else 0
    funding_pct = funding_rate * 100

    score = 0
    state = "POSITIONING_UNKNOWN"
    notes = []

    if oi_value and prev_oi and abs(oi_change) >= 0.35 and abs(price_change) >= 0.18:
        if price_change > 0 and oi_change > 0:
            score += 18
            state = "LONG_BUILDUP"
            notes.append("ціна росте + OI росте — нові лонги/попит")
        elif price_change < 0 and oi_change > 0:
            score -= 18
            state = "SHORT_BUILDUP"
            notes.append("ціна падає + OI росте — нові шорти/тиск")
        elif price_change > 0 and oi_change < 0:
            score += 6
            state = "SHORTS_CLOSING"
            notes.append("ріст на падінні OI — шорти закриваються, не доганяти без ретесту")
        elif price_change < 0 and oi_change < 0:
            score -= 6
            state = "LONGS_CLOSING"
            notes.append("падіння на падінні OI — лонги закриваються, не доганяти без ретесту")
    else:
        notes.append("OI ще накопичує історію для порівняння")

    if funding_pct >= 0.035:
        score -= 6
        notes.append("funding перегрітий у лонг — лонг не доганяти")
    elif funding_pct <= -0.035:
        score += 6
        notes.append("funding перегрітий у шорт — шорт не доганяти")

    if score >= 12:
        bias = "LONG"
    elif score <= -12:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "state": state,
        "oi": round(oi_value, 4) if oi_value is not None else None,
        "oi_change_pct": round(oi_change, 3),
        "funding_pct": round(funding_pct, 5),
        "price_change_pct": round(price_change, 3),
        "note": "; ".join(notes[:3]),
    }

def analyze_liquidations(candles_3m, candles_15m, flow, structure, price):
    """Liquidation / stop-run proxy from public data.

    We do not have exchange liquidation feed here, so this uses:
    - fast candle move
    - volume spike
    - candle close location
    - SMC sweep
    - trades/book flow

    It is a practical proxy for: long liquidation, short squeeze, high/low sweep,
    and post-sweep reclaim.
    """
    candles = candles_3m if candles_3m and len(candles_3m) >= 25 else candles_15m
    if not candles or len(candles) < 25 or not price:
        return {
            "available": False,
            "bias": "NEUTRAL",
            "score": 0,
            "event": "NO DATA",
            "blocks": [],
            "note": "даних для ліквідацій мало",
        }

    recent = candles[-24:]
    last = recent[-1]
    prev = recent[-2]
    avg_vol = mean([c.volume for c in recent[:-1]]) if len(recent) > 1 else 0
    vol_ratio = last.volume / avg_vol if avg_vol else 1.0
    move_1 = pct(last.close, prev.close)
    move_6 = pct(last.close, recent[-7].close) if len(recent) >= 7 else move_1
    body = last.close - last.open
    close_pos = close_location(last)
    prev_high = max(c.high for c in recent[:-1])
    prev_low = min(c.low for c in recent[:-1])

    flow_bias = (flow or {}).get("bias", "NEUTRAL")
    phase = (structure or {}).get("phase", "")
    score = 0
    event = "QUIET"
    blocks = []
    notes = []

    downside_sweep = last.low < prev_low and last.close > prev_low
    upside_sweep = last.high > prev_high and last.close < prev_high

    if downside_sweep or phase == "DOWNSIDE SWEEP":
        event = "DOWNSIDE SWEEP / LONG RECLAIM"
        score += 18
        blocks.append("SHORT")
        notes.append("зняли low і повернулись вище — шорт не доганяти")
        if flow_bias == "LONG":
            score += 6
            notes.append("покупці відкуповують після зняття low")

    elif upside_sweep or phase == "UPSIDE SWEEP":
        event = "UPSIDE SWEEP / SHORT RECLAIM"
        score -= 18
        blocks.append("LONG")
        notes.append("зняли high і закрились нижче — лонг не доганяти")
        if flow_bias == "SHORT":
            score -= 6
            notes.append("продавці тиснуть після зняття high")

    strong_down = (move_1 <= -0.42 or move_6 <= -0.70) and vol_ratio >= 1.35 and body < 0
    strong_up = (move_1 >= 0.42 or move_6 >= 0.70) and vol_ratio >= 1.35 and body > 0

    if strong_down:
        if close_pos <= 0.28 and flow_bias != "LONG":
            event = "LONG LIQUIDATION"
            score -= 18
            notes.append("дамп на обсязі — ймовірно вибивають лонги")
        elif close_pos >= 0.52 or flow_bias == "LONG":
            event = "LONG LIQUIDATION ABSORBED"
            score += 10
            blocks.append("SHORT")
            notes.append("дамп викупили — новий шорт тільки після ретесту")

    if strong_up:
        if close_pos >= 0.72 and flow_bias != "SHORT":
            event = "SHORT SQUEEZE"
            score += 18
            notes.append("памп на обсязі — ймовірно шорт-сквіз")
        elif close_pos <= 0.48 or flow_bias == "SHORT":
            event = "SHORT SQUEEZE ABSORBED"
            score -= 10
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
        "bias": bias,
        "score": int(score),
        "event": event,
        "blocks": sorted(set(blocks)),
        "vol_ratio": round(vol_ratio, 2),
        "move_1_pct": round(move_1, 3),
        "move_6_pct": round(move_6, 3),
        "note": "; ".join(notes[:3]) if notes else "ліквідаційного тиску не видно",
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
    """News feed for the bot.

    Reuters is prioritized, but not exclusive. For oil/BZ intraday trading,
    official EIA/Fed/OPEC/event headlines can matter even when Reuters has not
    republished them yet. Reuters items are marked and sorted first.
    """
    items = []

    if REUTERS_PRIORITY_NEWS:
        reuters_queries = [
            "site:reuters.com oil Brent crude OPEC EIA inventories",
            "site:reuters.com oil prices Brent crude today",
            "site:reuters.com EIA crude inventories API crude",
            "site:reuters.com OPEC production cut increase oil",
            "site:reuters.com Iran sanctions Hormuz oil",
            "site:reuters.com Fed Powell dollar oil prices",
        ]
        for query in reuters_queries:
            items.extend(parse_google_rss(query, 6))

    for query in NEWS_QUERIES:
        items.extend(parse_google_rss(query, 3))

    out = []
    for item in dedupe_news(items):
        title = normalized(item.get("title", ""))
        link = normalized(item.get("link", ""))
        is_reuters = "reuters" in title or "reuters.com" in link
        item["source"] = "Reuters" if is_reuters else item.get("source", "Google News")
        item["priority"] = 1 if is_reuters else 0
        out.append(item)

    out.sort(key=lambda x: (x.get("priority", 0), x.get("published_at") or datetime(1970, 1, 1, tzinfo=timezone.utc)), reverse=True)
    return out


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


def parse_month_date(text):
    clean = re.sub(r"[^A-Za-z0-9, ]+", "", str(text or "")).strip()
    for fmt in ["%B %d, %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(clean, fmt).date()
        except Exception:
            pass
    return None


def parse_us_time(text, default_hour=10, default_minute=30):
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", str(text or ""), re.I)
    if not match:
        return default_hour, default_minute
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()
    if ampm == "P" and hour != 12:
        hour += 12
    if ampm == "A" and hour == 12:
        hour = 0
    return hour, minute


def event_minutes(event_time):
    try:
        return int((event_time - now_utc()).total_seconds() / 60)
    except Exception:
        return None


def event_alert_status(event_time):
    minutes = event_minutes(event_time)
    if minutes is None:
        return False, ""
    if 0 <= minutes <= IMPORTANT_EVENT_WINDOW_MINUTES:
        return True, f"через {minutes} хв"
    if -45 <= minutes < 0:
        return True, f"вийшла {abs(minutes)} хв тому"
    return False, ""


def get_eia_event():
    text = ""
    for url in [EIA_WPSR_URL, EIA_WPSR_SCHEDULE_URL]:
        response = http_get(url, timeout=8, retries=1)
        if response:
            text += "\n" + clean_text(response.text)
    match = re.search(r"Next Release Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if not match:
        return None
    release_date = parse_month_date(match.group(1))
    if not release_date:
        return None
    idx = text.find(match.group(1))
    hour, minute = parse_us_time(text[idx:idx + 260] if idx >= 0 else text, 10, 30)
    event_time = et_to_utc(release_date, hour, minute)
    return {
        "name": "EIA",
        "title": "EIA crude inventories",
        "time": event_time,
        "risk": "HIGH",
        "source": "EIA official",
    }


def get_fomc_events():
    response = http_get(FED_FOMC_CALENDAR_URL, timeout=8, retries=1)
    if not response:
        return []
    text = clean_text(response.text)
    year = now_utc().year
    events = []

    month_names = (
        "January|February|March|April|May|June|July|August|September|October|November|December|"
        "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    for match in re.finditer(rf"({month_names})\s+(\d{{1,2}})(?:-(\d{{1,2}}))?", text):
        raw = f"{match.group(1)} {match.group(3) or match.group(2)}, {year}"
        date_obj = parse_month_date(raw)
        if not date_obj:
            continue
        event_time = et_to_utc(date_obj, 14, 0)
        active, _ = event_alert_status(event_time)
        if active:
            events.append({
                "name": "Fed",
                "title": "Fed / FOMC decision or minutes",
                "time": event_time,
                "risk": "HIGH",
                "source": "Federal Reserve calendar",
            })
    return events[:3]


def get_manual_events():
    events = []
    for chunk in [x.strip() for x in MANUAL_IMPORTANT_EVENTS.split(";") if x.strip()]:
        parts = [x.strip() for x in chunk.split("|")]
        if len(parts) < 2:
            continue
        title = parts[0]
        try:
            dt = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            events.append({
                "name": "Manual",
                "title": title,
                "time": dt.astimezone(timezone.utc),
                "risk": parts[2] if len(parts) > 2 else "HIGH",
                "source": "IMPORTANT_EVENTS",
            })
        except Exception:
            continue
    return events


def analyze_calendar_alerts():
    events = []
    try:
        eia = get_eia_event()
        if eia:
            events.append(eia)
    except Exception as error:
        print(f"[WARN] EIA calendar failed: {error}")
    try:
        events.extend(get_fomc_events())
    except Exception as error:
        print(f"[WARN] Fed calendar failed: {error}")
    events.extend(get_manual_events())

    alerts = []
    for event in events:
        active, status = event_alert_status(event.get("time"))
        if not active:
            continue
        item = dict(event)
        item["status"] = status
        item["minutes_to_event"] = event_minutes(event.get("time"))
        alerts.append(item)

    return {
        "active": bool(alerts),
        "alerts": alerts[:4],
        "score": -12 if alerts else 0,
        "note": "; ".join(f"{x['title']} — {x['status']}" for x in alerts[:3]) if alerts else "",
    }



def _move_from_candles(candles, bars):
    if not candles or len(candles) < bars + 1:
        return 0.0
    return pct(candles[-1].close, candles[-bars - 1].close)


def analyze_btc_inverse(btc_ticker, bzu_ticker):
    """BTC ↔ BZ inverse filter with risk-off fallback.

    Normal observation for this bot:
      BTC strong up -> BZ often weaker / SHORT support
      BTC strong down -> BZ often stronger / LONG support

    But during broad risk-off/risk-on regimes both can move in the same
    direction. In that case inverse mapping is disabled to avoid false signals.
    """
    btc_change = safe_float((btc_ticker or {}).get("change24h"), 0)
    bzu_change = safe_float((bzu_ticker or {}).get("change24h"), 0)

    # Risk-off / broad market same-direction guard.
    # If BZ and BTC have the same sign and BZ move is meaningful, do NOT invert.
    # This specifically prevents: BTC down + BZ down => false LONG.
    if abs(bzu_change) > 1.5 and (btc_change * bzu_change) > 0:
        return {
            "bias": "NEUTRAL",
            "score": 0,
            "state": "RISK_OFF_SAME_DIRECTION",
            "btc_change_pct": round(btc_change, 3),
            "bzu_change_pct": round(bzu_change, 3),
            "note": "BTC і BZ рухаються синхронно — inverse-фільтр вимкнено",
        }

    score = 0
    notes = []

    # Apply inverse only when BTC move is meaningful and not in same-direction regime.
    if btc_change >= 1.2:
        score -= 18
        notes.append("BTC росте — для BZ це inverse SHORT-фільтр")
    elif btc_change <= -1.2:
        score += 18
        notes.append("BTC падає — для BZ це inverse LONG-фільтр")
    elif btc_change >= 0.65:
        score -= 9
        notes.append("BTC помірно росте")
    elif btc_change <= -0.65:
        score += 9
        notes.append("BTC помірно падає")

    if score >= 10:
        bias = "LONG"
    elif score <= -10:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "state": "INVERSE_ACTIVE" if bias != "NEUTRAL" else "NEUTRAL",
        "btc_change_pct": round(btc_change, 3),
        "bzu_change_pct": round(bzu_change, 3),
        "note": "; ".join(notes) if notes else "BTC inverse без сильного сигналу",
    }

def build_context(data, state=None):
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price"))
    tf3 = analyze_micro(data.get("candles_3m") or [])
    tf15 = analyze_timeframe(data.get("candles_15m") or [], "15m")
    tf1h = analyze_timeframe(data.get("candles_1h") or [], "1h")
    tf4h = analyze_timeframe(data.get("candles_4h") or [], "4h")
    structure = analyze_structure(data.get("candles_15m") or [])
    ict = analyze_ict_model(data.get("candles_3m") or [], data.get("candles_15m") or [], structure, price)
    volume_guard = analyze_volume_guard(data.get("candles_15m") or [])
    previous_snapshot = (state or {}).get("last_market_snapshot") or {}
    flow = analyze_flow(data.get("trades") or [], data.get("book") or {}, price)
    cvd = analyze_cvd(data.get("trades") or [], data.get("candles_3m") or [], price, previous_snapshot)
    clusters = analyze_clusters(data.get("trades") or [], data.get("book") or {}, price)
    derivatives = analyze_derivatives(data.get("open_interest") or {}, data.get("funding") or {}, price, previous_snapshot)
    btc_inverse = analyze_btc_inverse(data.get("btc_ticker") or {}, data.get("btc_candles_3m") or [], data.get("btc_candles_15m") or [], data.get("btc_candles_1h") or [])
    liquidity = analyze_liquidations(data.get("candles_3m") or [], data.get("candles_15m") or [], flow, structure, price)
    news_items = get_news()
    news = analyze_news(news_items)
    calendar = analyze_calendar_alerts()
    session = market_session()

    price = price or safe_float(tf15.get("close"))
    atr15 = safe_float(structure.get("atr")) or safe_float(tf15.get("atr")) or (price or 90) * 0.006

    # Intraday architecture for trades lasting a few hours:
    # 15m/structure/3m are the engine. CVD and OI confirm quality.
    # 4H is only a background hint; clusters are only a local warning.
    tech_score = (
        tf15.get("score", 0) * 1.45
        + structure.get("score", 0) * 1.20
        + ict.get("score", 0) * 1.15
        + tf3.get("score", 0) * 0.62
        + tf1h.get("score", 0) * 0.45
        + cvd.get("score", 0) * 0.90
        + derivatives.get("score", 0) * 0.60
        + btc_inverse.get("score", 0) * 0.75
        + liquidity.get("score", 0) * 0.70
        + flow.get("score", 0) * 0.45
        + clusters.get("score", 0) * 0.18
        + tf4h.get("score", 0) * 0.10
        + volume_guard.get("score", 0)
    )
    total_score = tech_score + news.get("score", 0) * 0.30 + calendar.get("score", 0) + session.get("score", 0)

    if total_score >= 42:
        bias = "LONG"
    elif total_score <= -42:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    pending_ict_zones = build_pending_ict_zones({
        "price": price,
        "bias": bias,
        "atr15": atr15,
        "ict": ict,
    })

    return {
        "price": price,
        "change24h": safe_float(ticker.get("change24h"), 0),
        "source": ticker.get("source", "unknown"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "atr15": atr15,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "structure": structure,
        "ict": ict,
        "pending_ict_zones": pending_ict_zones,
        "volume_guard": volume_guard,
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
        "btc_inverse": btc_inverse,
        "liquidity": liquidity,
        "news": news,
        "calendar": calendar,
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
    """Anti-chase guard for intraday entries.

    This is intentionally NOT a full Retest Engine.
    It only blocks fresh market entries when the move has already gone vertical
    and the risk/reward is likely worse. Direction may still be correct, but
    the bot should wait for a pullback or a short consolidation instead of
    chasing the candle.
    """
    price = context["price"]
    atr15 = context["atr15"]
    tf15 = context["tf15"]
    tf3 = context["tf3"]
    structure = context["structure"]
    liquidity = context.get("liquidity") or {}
    ict = context.get("ict") or {}
    ema20 = safe_float(tf15.get("ema20"))
    rsi15 = safe_float(tf15.get("rsi"), 50)
    move_8 = safe_float(tf15.get("move_8_pct"), 0)
    fast_move_3m = safe_float(tf3.get("fast_move_pct"), 0)
    drift_3m = safe_float(tf3.get("drift_pct"), 0)
    score_3m = int(tf3.get("score", 0) or 0)

    if not price or not atr15:
        return False, ""

    if ict.get("no_chase") and ict.get("bias") == side:
        return True, "ICT: імпульс вже відбувся — не доганяти; чекати FVG/OB або нову проторговку"

    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    # Strong 3m impulse already happened. For a 15m intraday strategy this is
    # the classic place where market entries become late/chasing.
    long_vertical_3m = fast_move_3m >= 0.62 and score_3m >= 34
    short_vertical_3m = fast_move_3m <= -0.62 and score_3m <= -34

    # Price is stretched away from 15m EMA20 while 3m is already impulsive.
    long_stretched = distance_atr >= 1.25 and fast_move_3m >= 0.42
    short_stretched = distance_atr >= 1.25 and fast_move_3m <= -0.42

    # 15m move is already mature. This is not a hard reversal signal, just
    # a warning not to enter after the candle has done the easy part.
    mature_15m_long = rsi15 >= 72 and move_8 >= 0.85
    mature_15m_short = rsi15 <= 28 and move_8 <= -0.85

    if side == "LONG":
        if "LONG" in liquidity.get("blocks", []):
            return True, "ліквідність проти: high зняли/памп поглинули, лонг не доганяти"
        if long_vertical_3m:
            return True, "3M вже дав різкий імпульс вгору — лонг не доганяти; чекати проторговку або відкат"
        if long_stretched:
            return True, "ціна розтягнута від EMA20 після 3M-імпульсу — лонг тільки після охолодження"
        if mature_15m_long:
            return True, "лонг після сильного 15M імпульсу: чекати відкат/проторговку"
        if distance_atr >= 1.75 and tf3.get("state") != "LONG_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3M не підсилює"
        if structure.get("phase") == "UPSIDE SWEEP":
            return True, "зверху зняли ліквідність, лонг не доганяти"
    else:
        if "SHORT" in liquidity.get("blocks", []):
            return True, "ліквідність проти: low зняли/дамп викупили, шорт не доганяти"
        if short_vertical_3m:
            return True, "3M вже дав різкий імпульс вниз — шорт не доганяти; чекати проторговку або відкат"
        if short_stretched:
            return True, "ціна розтягнута від EMA20 після 3M-імпульсу — шорт тільки після охолодження"
        if mature_15m_short:
            return True, "шорт після сильного 15M імпульсу: чекати відкат/проторговку"
        if distance_atr >= 1.75 and tf3.get("state") != "SHORT_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3M не підсилює"
        if structure.get("phase") == "DOWNSIDE SWEEP":
            return True, "знизу зняли ліквідність, шорт не доганяти"
    return False, ""


def entry_confirmations(side, context):
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context.get("tf4h") or {}
    structure = context["structure"]
    ict = context.get("ict") or {}
    flow = context["flow"]
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    btc_inverse = context.get("btc_inverse") or {}
    liquidity = context.get("liquidity") or {}
    calendar = context.get("calendar") or {}
    news = context["news"]

    confirmations = []
    conflicts = []

    volume_guard = context.get("volume_guard") or {}

    if calendar.get("active"):
        conflicts.append("важлива новина у найближчу годину")
    if volume_guard.get("score", 0) >= 10:
        confirmations.append("сильний обсяг")
    elif volume_guard.get("score", 0) >= 6:
        confirmations.append("активний обсяг")

    # 4H is deliberately background only for this intraday bot.
    # It can add context when it agrees, but it must not block an entry.
    if tf4h.get("bias") == side and abs(tf4h.get("score", 0)) >= 35:
        confirmations.append("4h фон підтримує")

    if tf15.get("bias") == side:
        confirmations.append("15m за напрямом")
    elif tf15.get("bias") == opposite(side):
        conflicts.append("15m проти")

    if tf1h.get("bias") == side:
        confirmations.append("1h підтримує")
    elif tf1h.get("bias") == opposite(side):
        conflicts.append("1h проти")

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    if tf3.get("bias") == side:
        confirmations.append("3m дав тригер")
    elif tf3.get("bias") == opposite(side) and tf3_score_abs >= 32:
        conflicts.append("3m сильно проти")
    elif tf3.get("bias") == opposite(side):
        # Weak opposite 3M is often just a pullback inside the 15M move.
        conflicts.append("3m локальний відкат")

    if structure.get("bias") == side:
        confirmations.append("структура підтверджує")
    elif structure.get("bias") == opposite(side):
        conflicts.append("структура проти")

    if ict.get("bias") == side and ict.get("entry_ok"):
        confirmations.append("ICT сетап готовий")
    elif ict.get("bias") == side:
        confirmations.append("ICT контекст підтримує")
    elif ict.get("bias") == opposite(side) and abs(int(ict.get("score", 0) or 0)) >= 20:
        conflicts.append("ICT проти")
    if ict.get("no_chase") and ict.get("bias") == side:
        conflicts.append("ICT не доганяти")

    if cvd.get("bias") == side:
        confirmations.append("CVD підтверджує")
    elif cvd.get("bias") == opposite(side):
        conflicts.append("CVD проти")

    if derivatives.get("bias") == side:
        confirmations.append("OI/Funding підтримує")
    elif derivatives.get("bias") == opposite(side):
        conflicts.append("OI/Funding проти")

    if btc_inverse.get("bias") == side:
        confirmations.append("BTC inverse підтверджує")
    elif btc_inverse.get("bias") == opposite(side):
        conflicts.append("BTC inverse проти")

    if liquidity.get("bias") == side:
        confirmations.append("ліквідність підтримує")
    elif liquidity.get("bias") == opposite(side):
        conflicts.append("ліквідність проти")
    if side in liquidity.get("blocks", []):
        conflicts.append("після зняття ліквідності цей напрям не доганяти")

    # Flow and clusters are micro-hints, not blockers.
    if flow.get("bias") == side:
        confirmations.append("потік підтримує")
    elif flow.get("bias") == opposite(side):
        conflicts.append("потік локально проти")

    if clusters.get("bias") == side:
        confirmations.append("кластери локально підтримують")
    elif clusters.get("bias") == opposite(side):
        if side == "SHORT":
            conflicts.append("локальний покупець у стакані")
        else:
            conflicts.append("локальний продавець у стакані")

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

    min_risk = max(atr15 * 1.15, price * 0.0075)
    max_risk = price * (MAX_STOP_DISTANCE_PCT / 100)
    buffer = max(atr15 * 0.18, price * 0.0012)
    min_tp1_distance = price * (MIN_TP1_DISTANCE_PCT / 100)
    min_tp2_distance = price * (MIN_TP2_DISTANCE_PCT / 100)
    min_tp3_distance = price * (MIN_TP3_DISTANCE_PCT / 100)

    if side == "LONG":
        raw_stop = min(swing_low - buffer, price - min_risk)
        risk = min(max(price - raw_stop, min_risk), max_risk)
        stop = price - risk
        technical_tp1 = max(swing_high, price + atr15 * 1.8)
        technical_tp2 = max(structure.get("recent_high") or technical_tp1, price + atr15 * 2.6)
        tp1 = max(technical_tp1, price + min_tp1_distance)
        tp2 = max(technical_tp2, price + min_tp2_distance, tp1 + atr15 * 0.9)
        tp3 = max(price + min_tp3_distance, tp2 + atr15 * 1.2)
        invalidation = (
            f"15m закриття нижче {round_price(stop)} або злам 3m/структури проти LONG. "
            f"Стоп не далі {MAX_STOP_DISTANCE_PCT}% від входу; TP1 не ближче {MIN_TP1_DISTANCE_PCT}%."
        )
    else:
        raw_stop = max(swing_high + buffer, price + min_risk)
        risk = min(max(raw_stop - price, min_risk), max_risk)
        stop = price + risk
        technical_tp1 = min(swing_low, price - atr15 * 1.8)
        technical_tp2 = min(structure.get("recent_low") or technical_tp1, price - atr15 * 2.6)
        tp1 = min(technical_tp1, price - min_tp1_distance)
        tp2 = min(technical_tp2, price - min_tp2_distance, tp1 - atr15 * 0.9)
        tp3 = min(price - min_tp3_distance, tp2 - atr15 * 1.2)
        invalidation = (
            f"15m закриття вище {round_price(stop)} або злам 3m/структури проти SHORT. "
            f"Стоп не далі {MAX_STOP_DISTANCE_PCT}% від входу; TP1 не ближче {MIN_TP1_DISTANCE_PCT}%."
        )

    risk_pct = abs(stop - price) / price * 100 if price else 0
    reward1_pct = abs(tp1 - price) / price * 100 if price else 0
    reward2_pct = abs(tp2 - price) / price * 100 if price else 0
    reward3_pct = abs(tp3 - price) / price * 100 if price else 0
    return TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1),
        tp2=round_price(tp2),
        tp3=round_price(tp3),
        risk_pct=round(risk_pct, 3),
        rr1=round(reward1_pct / risk_pct, 2) if risk_pct else 0,
        rr2=round(reward2_pct / risk_pct, 2) if risk_pct else 0,
        rr3=round(reward3_pct / risk_pct, 2) if risk_pct else 0,
        invalidation=invalidation,
    )


def evaluate_new_setup(context):
    side = context["bias"]
    if side not in ["LONG", "SHORT"] or not context.get("price"):
        # Diagnostic quality: not an entry probability, but a market readiness score.
        # Do not show 0/100 when the market is simply balanced.
        tf15 = context.get("tf15") or {}
        structure = context.get("structure") or {}
        ict = context.get("ict") or {}
        cvd = context.get("cvd") or {}
        flow = context.get("flow") or {}
        tf1h = context.get("tf1h") or {}
        tf4h = context.get("tf4h") or {}
        readiness = 28
        for block, weight in [
            (tf15, 0.22),
            (structure, 0.26),
            (ict, 0.28),
            (cvd, 0.14),
            (flow, 0.10),
            (tf1h, 0.08),
            (tf4h, 0.04),
        ]:
            readiness += min(8, abs(int(block.get("score", 0) or 0)) * weight)
        if ict.get("setup") in ["BALANCE_MIDRANGE", "CONTEXT_ONLY", "DISCOUNT_CONTEXT", "PREMIUM_CONTEXT"]:
            reason = "ринок у балансі / ICT-сетап не готовий"
        else:
            reason = "перевага нечітка, немає професійного входу"
        return {
            "action": "NO_TRADE",
            "side": "NEUTRAL",
            "quality": int(max(25, min(55, readiness))),
            "title": "ВХОДУ НЕМАЄ",
            "reason": reason,
            "plan": None,
            "confirmations": [],
            "conflicts": [],
        }

    confirmations, conflicts = entry_confirmations(side, context)
    late, late_reason = is_late_chase(side, context)
    plan = make_plan(side, context)

    tf3 = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context.get("tf4h") or {}
    structure = context["structure"]
    flow = context["flow"]
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    btc_inverse = context.get("btc_inverse") or {}
    liquidity = context.get("liquidity") or {}
    calendar = context.get("calendar") or {}
    news = context.get("news") or {}

    def block_points(block, same, opposite_penalty, neutral=0, weak_opposite_penalty=None):
        bias = block.get("bias")
        score = abs(int(block.get("score", 0) or 0))
        if bias == side:
            return same
        if bias == opposite(side):
            if weak_opposite_penalty is not None and score < 32:
                return -weak_opposite_penalty
            return -opposite_penalty
        return neutral

    # Intraday quality model:
    # 15M + structure define the setup.
    # 3M is a trigger/better timing tool, NOT a mandatory permission.
    # CVD/flow/OI/BTC confirm whether the move has real pressure.
    score_for_side = side_score(context["total_score"], side)
    quality = 47 + min(10, max(0, score_for_side // 9))
    quality += block_points(tf15, 14, 15)
    quality += block_points(structure, 11, 13)
    if ict.get("state") == "ENTRY_MODEL":
        quality += block_points(ict, 14, 14)
    elif ict.get("bias") == side and ict.get("score", 0):
        quality += block_points(ict, 5, 5)
    if ict.get("entry_ok") and ict.get("bias") == side:
        quality += 8
    if ict.get("no_chase") and ict.get("bias") == side:
        quality -= 18
    quality += block_points(tf3, 6, 9, neutral=0, weak_opposite_penalty=4)
    quality += block_points(tf1h, 4, 4)
    quality += block_points(tf4h, 1, 1)
    quality += block_points(cvd, 10, 11)
    quality += block_points(derivatives, 6, 7)
    quality += block_points(btc_inverse, 7, 8)
    quality += block_points(liquidity, 7, 10)
    quality += block_points(flow, 5, 5)
    quality += block_points(clusters, 1, 2)

    if news.get("bias") == side:
        quality += 2
    elif news.get("bias") == opposite(side) and abs(news.get("score", 0)) >= 35:
        quality -= 7

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    tf3_same = tf3.get("bias") == side
    tf3_neutral = tf3.get("bias") == "NEUTRAL"
    tf3_weak_pullback = tf3.get("bias") == opposite(side) and tf3_score_abs < 32
    tf3_strong_against = tf3.get("bias") == opposite(side) and tf3_score_abs >= 32

    tf15_same = tf15.get("bias") == side
    tf15_neutral = tf15.get("bias") == "NEUTRAL"
    tf15_against = tf15.get("bias") == opposite(side)

    structure_same = structure.get("bias") == side
    structure_neutral = structure.get("bias") == "NEUTRAL"
    structure_against = structure.get("bias") == opposite(side)

    ict_same = ict.get("bias") == side
    ict_entry_ok = ict_same and bool(ict.get("entry_ok"))
    ict_no_chase = ict_same and bool(ict.get("no_chase"))
    ict_against = ict.get("bias") == opposite(side) and ict.get("state") == "ENTRY_MODEL" and abs(int(ict.get("score", 0) or 0)) >= 22

    cvd_same = cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 10
    flow_same = flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10
    oi_same = derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10
    btc_same = btc_inverse.get("bias") == side and abs(int(btc_inverse.get("score", 0) or 0)) >= 10

    strong_cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 18
    strong_oi_against = derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14
    strong_btc_against = btc_inverse.get("bias") == opposite(side) and abs(int(btc_inverse.get("score", 0) or 0)) >= 18

    core_direction_ok = (
        (tf15_same and not structure_against)
        or (structure_same and not tf15_against)
        or (ict_same and not tf15_against and not structure_against)
        or (tf15_neutral and (structure_same or ict_same))
    )

    pressure_ok = cvd_same or flow_same or oi_same or btc_same or ict_entry_ok

    # Classic entry: 3M confirms the 15M/structure direction.
    trigger_entry_ok = (
        (tf3_same or ict_entry_ok)
        and core_direction_ok
        and not strong_cvd_against
        and not strong_oi_against
        and not strong_btc_against
        and not ict_against
        and not ict_no_chase
        and side not in liquidity.get("blocks", [])
    )

    # Pullback / early intraday entry:
    # If the higher intraday setup is aligned and pressure confirms, 3M RANGE or
    # weak 3M counter-move is treated as a pullback, not as a blocker.
    pullback_entry_ok = (
        core_direction_ok
        and pressure_ok
        and (tf3_neutral or tf3_weak_pullback)
        and not tf3_strong_against
        and not strong_cvd_against
        and not strong_oi_against
        and not strong_btc_against
        and not ict_against
        and not ict_no_chase
        and side not in liquidity.get("blocks", [])
    )

    trigger_ok = trigger_entry_ok or pullback_entry_ok

    # 4H is not included here. It is only context.
    trend_ok = core_direction_ok

    hard_conflict = (
        side in liquidity.get("blocks", [])
        or ict_against
        or ict_no_chase
        or (tf15_against and structure_against)
        or (strong_cvd_against and strong_oi_against)
        or (tf3_strong_against and (strong_cvd_against or strong_btc_against))
        or (strong_btc_against and (strong_cvd_against or strong_oi_against or tf15_against))
    )

    if late:
        quality = min(quality, 55)
    if calendar.get("active"):
        quality -= 8
        if quality < 75:
            hard_conflict = True
    if hard_conflict:
        quality = min(quality, 56)
    if not trend_ok:
        quality = min(quality, 59)
    elif not trigger_ok:
        # Do not crush quality just because 3M is RANGE.
        # The bot should show "готуватись" instead of acting as if setup is invalid.
        quality = min(quality, 66)

    quality = int(max(0, min(92, quality)))

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
        if pullback_entry_ok and not trigger_entry_ok:
            reason = "ICT/відкат: 15M/структура тримають напрям, вхід не після погоні, CVD/потік/OI/BTC підтверджують"
        else:
            reason = "сигнал підтверджений: " + " | ".join(confirmations[:4])
        return {
            "action": "ENTRY",
            "side": side,
            "quality": quality,
            "title": f"ВХІД Є — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

    if quality >= RISKY_QUALITY_MIN and trigger_ok and not hard_conflict:
        if pullback_entry_ok and not trigger_entry_ok:
            reason = "ранній ICT/відкат; 3M ще не дав повний імпульсний тригер"
        else:
            reason = "є ранній тригер, але підтверджень ще не максимум: " + " | ".join(confirmations[:4])
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
        }

    if core_direction_ok and pressure_ok and (tf3_neutral or tf3_weak_pullback):
        wait_reason = "напрям є, 3M зараз у відкаті/проторговці; чекати реакцію в Pending ICT зоні або коротке підтвердження"
    elif conflicts:
        wait_reason = "напрям є, але входу ще немає: " + "; ".join(conflicts[:3])
    else:
        wait_reason = "напрям є, але бракує якості входу; орієнтир — Pending ICT зона"

    return {
        "action": "WATCH",
        "side": side,
        "quality": quality,
        "title": f"ЧЕКАТИ — ГОТУЄМОСЬ ДО {side}",
        "reason": wait_reason,
        "plan": plan,
        "confirmations": confirmations,
        "conflicts": conflicts,
    }
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
    ict = context.get("ict") or {}
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

    # Weighted management pressure for intraday trades.
    # 3m/15m/structure matter most. 4H and clusters are only small background hints.
    opposite_votes = 0.0
    support_votes = 0.0
    weighted_blocks = [
        (tf3, 1.20),
        (tf15, 1.00),
        (structure, 1.00),
        (context.get("cvd") or {}, 0.80),
        (context.get("derivatives") or {}, 0.55),
        (context.get("btc_inverse") or {}, 0.65),
        (flow, 0.35),
        (context.get("clusters") or {}, 0.20),
        (context.get("tf4h") or {}, 0.20),
    ]
    for block, weight in weighted_blocks:
        if block.get("bias") == side:
            support_votes += weight
        elif block.get("bias") == opposite(side):
            opposite_votes += weight

    near_entry = abs(current_pct) <= 0.18
    lost_after_profit = best_pct >= 0.38 and giveback_ratio >= 0.62 and current_pct <= 0.18

    # Intraday early-exit logic.
    # For trades that should last only a few hours, do not wait for the formal stop
    # if 3m has already broken against the position and order-flow/CVD confirms it.
    tf3_against = tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 32
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10
    cvd_block = context.get("cvd") or {}
    cvd_against = cvd_block.get("bias") == opposite(side) and abs(int(cvd_block.get("score", 0) or 0)) >= 10
    btc_block = context.get("btc_inverse") or {}
    btc_against = btc_block.get("bias") == opposite(side) and abs(int(btc_block.get("score", 0) or 0)) >= 16
    structure_against = structure.get("bias") == opposite(side)

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - trade.stop_current) / trade.entry * 100) if trade.entry else 99.0
    else:
        stop_distance_pct = max(0.0, (trade.stop_current - price) / trade.entry * 100) if trade.entry else 99.0

    near_stop = stop_distance_pct <= 0.30
    clearly_losing = current_pct <= -0.30

    if tf3_against and (flow_against or cvd_against or btc_against) and (near_stop or clearly_losing):
        trade.status = "CLOSED"
        trade.last_action = "EXIT_LOCAL_BREAK"
        reasons = ["3M зламався проти позиції"]
        if flow_against:
            reasons.append("потік проти")
        if cvd_against:
            reasons.append("CVD проти")
        if btc_against:
            reasons.append("BTC inverse проти")
        if near_stop:
            reasons.append("ціна близько до стопу")
        return {
            "closed": True,
            "action": "EXIT_LOCAL_BREAK",
            "title": f"{side} ЗАКРИТИ — ЛОКАЛЬНИЙ ЗЛАМ",
            "recommendation": "закрити біля поточної / не чекати дальній стоп",
            "current_pct": current_pct,
            "best_pct": best_pct,
            "notes": reasons[:4],
        }

    if tf3_against and (near_stop or clearly_losing):
        action = "EXIT_WARNING"
        title = f"{side} ПІД ЗАГРОЗОЮ"
        recommendation = "3M вже проти позиції: захистити або закрити, не усереднювати"
        notes.append("3M зламався проти позиції")
        if near_stop:
            notes.append("ціна близько до стопу")

    if lost_after_profit and opposite_votes >= 2.2:
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

    if action == "HOLD" and near_entry and opposite_votes >= 2.2 and support_votes <= 1.2:
        action = "PROTECT_OR_EXIT"
        title = f"{side} ПІД ЗАГРОЗОЮ"
        recommendation = "ціна біля входу, підтвердження слабке: захистити позицію або закрити біля входу"
        notes.append("не усереднювати; чекати нового тригера після виходу")
    elif action == "HOLD" and current_pct < -0.28 and opposite_votes >= 2.2:
        action = "EXIT_WARNING"
        title = f"{side} ПІД ЗАГРОЗОЮ"
        recommendation = "позиція під тиском: краще виходити раніше, не чекати дальній стоп"
        notes.append("злам 3m/структури проти позиції")
    elif action == "HOLD" and current_pct > 0 and tf3.get("bias") == opposite(side):
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


def _fmt_price(value):
    value = round_price(value)
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _bias_label(value):
    value = str(value or "NEUTRAL").upper()
    if value == "LONG":
        return "LONG"
    if value == "SHORT":
        return "SHORT"
    return "NEUTRAL"


def _score_line(label, block):
    block = block or {}
    return f"<b>{label}:</b> {_bias_label(block.get('bias'))} ({block.get('score', 0)})"


def _short_list(items, limit=3):
    out = []
    for item in items or []:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def price_line(context):
    # Clean intraday format: no exchange/source, no extra 24h noise.
    return f"<b>Ціна:</b> {_fmt_price(context.get('price'))}"


def context_lines(context):
    # Short dashboard only. Detailed notes stay inside the bot logic, not in Telegram.
    lines = []
    calendar = context.get("calendar") or {}
    if calendar.get("active"):
        lines.append(f"<b>⚠️ Новина:</b> {calendar.get('note')}. Новий вхід тільки після реакції ціни.")

    lines.extend([
        _score_line("4H", context.get("tf4h")),
        _score_line("1H", context.get("tf1h")),
        _score_line("15M", context.get("tf15")),
        _score_line("3M", context.get("tf3")),
    ])

    # Extra professional blocks, also short.
    if context.get("ict"):
        lines.append(_score_line("ICT", context.get("ict")))
    if context.get("cvd"):
        lines.append(_score_line("CVD", context.get("cvd")))
    if context.get("clusters"):
        lines.append(_score_line("Кластери", context.get("clusters")))
    if context.get("derivatives"):
        lines.append(_score_line("OI/Funding", context.get("derivatives")))
    if context.get("btc_inverse"):
        lines.append(_score_line("BTC↔BZ", context.get("btc_inverse")))
    if context.get("flow"):
        lines.append(_score_line("Потік", context.get("flow")))
    return lines


def plan_text(plan, multiline=False):
    if not plan:
        return "плану немає"
    if multiline:
        return "\n".join([
            f"<b>Вхід:</b> {_fmt_price(plan.entry)}",
            f"<b>Стоп:</b> {_fmt_price(plan.stop)}",
            f"<b>TP1:</b> {_fmt_price(plan.tp1)}",
            f"<b>TP2:</b> {_fmt_price(plan.tp2)}",
            f"<b>TP3:</b> {_fmt_price(plan.tp3)}",
            f"<b>Ризик:</b> {plan.risk_pct}% | <b>RR:</b> {plan.rr1}/{plan.rr2}/{plan.rr3}",
        ])
    return (
        f"Вхід {_fmt_price(plan.entry)} | Стоп {_fmt_price(plan.stop)} | "
        f"TP1 {_fmt_price(plan.tp1)} | TP2 {_fmt_price(plan.tp2)} | TP3 {_fmt_price(plan.tp3)} | "
        f"ризик {plan.risk_pct}% | RR {plan.rr1}/{plan.rr2}/{plan.rr3}"
    )


def _compact_title(setup):
    action = setup.get("action")
    side = setup.get("side")
    if action == "ENTRY":
        return f"ВХІД {side}"
    if action == "RISKY_ENTRY":
        return f"РИЗИКОВАНИЙ ВХІД {side}"
    if action == "WAIT_RETEST":
        return f"НЕ ДОГАНЯТИ {side}"
    if action == "WATCH" and side in ["LONG", "SHORT"]:
        return f"ЧЕКАТИ {side}"
    if action == "NO_TRADE":
        return "ЧЕКАТИ — ICT НЕ ГОТОВИЙ"
    return str(setup.get("title") or "СИГНАЛ")


def build_new_setup_message(context, setup):
    plan = setup.get("plan")
    conflicts = _short_list(setup.get("conflicts"), 3)
    confirmations = _short_list(setup.get("confirmations"), 3)

    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{_compact_title(setup)}</b>",
        "",
        price_line(context),
        f"<b>Якість:</b> {setup['quality']}/100",
    ]

    if setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        if confirmations:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            lines.extend([f"✅ {x}" for x in confirmations])
        if conflicts:
            lines.append("")
            lines.append("<b>Ризики:</b>")
            lines.extend([f"⚠️ {x}" for x in conflicts])
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(plan_text(plan, multiline=True))
        lines.append("")
        lines.append(f"<b>Скасування:</b> {plan.invalidation}")
        if setup.get("action") == "RISKY_ENTRY":
            lines.append("<b>Режим:</b> малий ризик; якщо 3M одразу піде проти — не тримати до дальнього стопу.")
    else:
        reason_items = conflicts or _short_list([setup.get("reason")], 1)
        if reason_items:
            lines.append("")
            lines.append("<b>Причина:</b>")
            for x in reason_items:
                icon = "⚠️" if ("локаль" in x or "4h фон" in x or "потік локально" in x) else "❌"
                lines.append(f"{icon} {x}")
        if setup.get("side") in ["LONG", "SHORT"] and plan:
            lines.append("")
            lines.append("<b>Орієнтир:</b>")
            lines.append(plan_text(plan, multiline=True))

    lines.append("")
    lines.extend(context_lines(context))
    return "\n".join(lines).strip()


def build_follow_message(context, trade, result):
    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{result['title']}</b>",
        f"{result['recommendation']}",
        "",
        price_line(context),
        f"<b>Від входу:</b> {round(result['current_pct'], 3)}% | <b>Макс:</b> {round(result['best_pct'], 3)}%",
        "",
        "<b>Позиція:</b>",
        f"Вхід {_fmt_price(trade.entry)} | Стоп {_fmt_price(trade.stop_current)}",
        f"TP1 {_fmt_price(trade.tp1)} | TP2 {_fmt_price(trade.tp2)} | TP3 {_fmt_price(trade.tp3)}",
        f"TP1 {'✅' if trade.tp1_hit else '—'} | TP2 {'✅' if trade.tp2_hit else '—'} | TP3 {'✅' if trade.tp3_hit else '—'}",
    ]
    if result.get("notes"):
        lines.append("")
        lines.append("<b>Дія:</b>")
        lines.extend([f"⚠️ {x}" for x in _short_list(result.get("notes"), 3)])
    lines.append("")
    lines.extend(context_lines(context))
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


def update_market_snapshot(state, context):
    derivatives = context.get("derivatives") or {}
    cvd = context.get("cvd") or {}
    state["last_market_snapshot"] = {
        "time": iso_now(),
        "price": round_price(context.get("price")),
        "oi": derivatives.get("oi"),
        "cvd": cvd.get("cvd"),
        "cvd_time": iso_now(),
        "last_trade_ts": cvd.get("last_trade_ts"),
        "bias": context.get("bias"),
        "tech_score": context.get("tech_score"),
        "total_score": context.get("total_score"),
    }


# ==========================================================
# MAIN
# ==========================================================


def main():
    print("START BZU SIGNAL BOT PRO")
    state = load_state()
    journal = load_journal()

    data = collect_market_data()
    context = build_context(data, state)
    if not context.get("price"):
        print("NO PRICE DATA")
        return

    print(f"PRICE {context['price']} | BIAS {context['bias']} | TECH {context['tech_score']} | TOTAL {context['total_score']}")
    print(f"4h {context['tf4h'].get('bias')} {context['tf4h'].get('score')} | 15m {context['tf15'].get('bias')} {context['tf15'].get('score')} | 3m {context['tf3'].get('bias')} {context['tf3'].get('score')}")
    print(f"STRUCTURE {context['structure'].get('phase')} {context['structure'].get('bias')} | CVD {context['cvd'].get('bias')} | CLUSTERS {context['clusters'].get('bias')} | OI {context['derivatives'].get('bias')} | NEWS {context['news'].get('bias')}")

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
        update_market_snapshot(state, context)
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
            "tf4h": context["tf4h"],
            "structure": context["structure"],
            "flow": context["flow"],
            "cvd": context["cvd"],
            "clusters": context["clusters"],
            "derivatives": context["derivatives"],
            "liquidity": context["liquidity"],
            "news": {
                "bias": context["news"]["bias"],
                "score": context["news"]["score"],
                "top": context["news"]["top"],
                "total": context["news"]["total"],
            },
            "calendar": context["calendar"],
        },
    })

    update_market_snapshot(state, context)
    save_state(state)
    save_journal(journal)
    send_telegram(message)
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
