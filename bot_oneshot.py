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

# Smart Money / ICT risk-reward guard.
# A setup is not professional if the first target is smaller than the stop.
# Default: TP1 must be at least 1:1 to risk. Better setups receive quality bonus.
MIN_RR1_ENTRY = float(os.getenv("MIN_RR1_ENTRY", "1.00") or 1.00)
GOOD_RR1_BONUS = float(os.getenv("GOOD_RR1_BONUS", "1.30") or 1.30)
STRONG_RR1_BONUS = float(os.getenv("STRONG_RR1_BONUS", "1.80") or 1.80)

# Intraday filters
# Volume is a bonus only. Low volume must NOT block entries for BZ intraday,
# because compression often happens before the best impulse move.
VOLUME_ACTIVE_RATIO = float(os.getenv("VOLUME_ACTIVE_RATIO", "1.30") or 1.30)
VOLUME_STRONG_RATIO = float(os.getenv("VOLUME_STRONG_RATIO", "1.70") or 1.70)
CVD_STATE_MAX_AGE_MINUTES = int(os.getenv("CVD_STATE_MAX_AGE_MINUTES", "360") or 360)
CVD_MIN_FRESH_TRADES = int(os.getenv("CVD_MIN_FRESH_TRADES", "80") or 80)
CVD_LOW_CONFIDENCE_FACTOR = float(os.getenv("CVD_LOW_CONFIDENCE_FACTOR", "0.45") or 0.45)
CVD_MEDIUM_CONFIDENCE_FACTOR = float(os.getenv("CVD_MEDIUM_CONFIDENCE_FACTOR", "0.70") or 0.70)
ICT_FVG_MAX_AGE_HOURS = float(os.getenv("ICT_FVG_MAX_AGE_HOURS", "18") or 18)
ICT_OB_MAX_AGE_HOURS = float(os.getenv("ICT_OB_MAX_AGE_HOURS", "36") or 36)
PRICE_SOURCE_MAX_DIFF_PCT = float(os.getenv("PRICE_SOURCE_MAX_DIFF_PCT", "0.18") or 0.18)
LOW_LIQUIDITY_VOLUME_RATIO = float(os.getenv("LOW_LIQUIDITY_VOLUME_RATIO", "0.35") or 0.35)
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
    stop_updated_at: str = ""
    status: str = "OPEN"
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    # Stop-management locks. After TP1 the first protective stop is fixed and
    # must not be recalculated on every 15m run. It can be changed only after
    # TP2 or after a hard exit/close decision.
    tp1_stop_locked: bool = False
    tp2_stop_locked: bool = False
    tp1_locked_stop: float = 0.0
    tp2_locked_stop: float = 0.0
    best_price: float = 0.0
    last_action: str = "OPEN"
    last_message_key: str = ""
    # Entry point lifecycle: used by supervision to decide whether the original
    # entry idea is still alive, weak, recovered, or broken. This prevents noisy
    # contradictions like "entry not confirmed" while risk still looks moderate.
    entry_integrity_score: int = 100
    entry_fail_streak: int = 0
    entry_recovery_checks: int = 0
    entry_last_state: str = "СИЛЬНА"
    # Adaptive MFE Giveback Guard 2.0 memory. It requires repeated confirmation
    # before closing on profit giveback, so one noisy 3M candle does not kick us
    # out of a still-valid trend.
    mfe_giveback_streak: int = 0
    mfe_giveback_last_state: str = "OK"
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


def clamp(value, lo=0, hi=100):
    """Clamp numeric value into [lo, hi].

    Used by risk/MFE engines. Kept global so helper functions defined before
    assess_trade_risk can call it safely in GitHub Actions.
    """
    try:
        return max(lo, min(hi, value))
    except Exception:
        return lo


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


def make_json_safe(value):
    """Recursively convert bot state/journal values to JSON-safe types.

    Some market/calendar/news helpers can leave datetime objects inside the
    context saved to signal_journal.json. GitHub Actions then crashes on
    json.dump(). This sanitizer keeps the journal useful and prevents one
    non-serializable value from killing the bot run.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return str(value)


def atomic_json_write(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    bak = path + ".bak"
    safe_data = make_json_safe(data)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with open(bak, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, ensure_ascii=False, indent=2)


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
            stop_updated_at=str(raw.get("stop_updated_at") or raw.get("last_stop_update") or raw.get("opened_at") or iso_now()),
            status=str(raw.get("status") or "OPEN"),
            tp1_hit=bool(raw.get("tp1_hit")),
            tp2_hit=bool(raw.get("tp2_hit")),
            tp3_hit=bool(raw.get("tp3_hit")),
            tp1_stop_locked=bool(raw.get("tp1_stop_locked", False)),
            tp2_stop_locked=bool(raw.get("tp2_stop_locked", False)),
            tp1_locked_stop=float(raw.get("tp1_locked_stop") or 0),
            tp2_locked_stop=float(raw.get("tp2_locked_stop") or 0),
            best_price=float(raw.get("best_price") or raw.get("entry")),
            last_action=str(raw.get("last_action") or "OPEN"),
            last_message_key=str(raw.get("last_message_key") or ""),
            entry_integrity_score=int(raw.get("entry_integrity_score", 100) or 100),
            entry_fail_streak=int(raw.get("entry_fail_streak", 0) or 0),
            entry_recovery_checks=int(raw.get("entry_recovery_checks", 0) or 0),
            entry_last_state=str(raw.get("entry_last_state") or "СИЛЬНА"),
            mfe_giveback_streak=int(raw.get("mfe_giveback_streak", 0) or 0),
            mfe_giveback_last_state=str(raw.get("mfe_giveback_last_state") or "OK"),
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

    # IMPORTANT: the chart the user watches is BINANCE:BZUSDT.P on TradingView.
    # Use TradingView/Binance as the primary displayed/current price and OKX only
    # as a fallback. This prevents Telegram from showing an OKX price that is
    # already different from the TradingView chart.
    tv_ticker = get_tradingview_price_fallback()
    okx_ticker = get_okx_ticker()
    ticker = tv_ticker or okx_ticker
    if ticker:
        ticker["checked_at"] = iso_now()
        ticker["price_status"] = "primary"
    if tv_ticker and okx_ticker:
        tv_price = safe_float(tv_ticker.get("price"))
        okx_price = safe_float(okx_ticker.get("price"))
        if tv_price and okx_price:
            diff_pct = abs(tv_price - okx_price) / tv_price * 100
            ticker["okx_reference_price"] = okx_price
            ticker["cross_price_diff_pct"] = round(diff_pct, 4)
            ticker["price_status"] = "cross_checked" if diff_pct <= PRICE_SOURCE_MAX_DIFF_PCT else "price_source_warning"
            ticker["price_warning"] = diff_pct > PRICE_SOURCE_MAX_DIFF_PCT
    elif okx_ticker:
        ticker["okx_reference_price"] = okx_ticker.get("price")
        if not tv_ticker:
            ticker["price_status"] = "tradingview_unavailable_okx_fallback"
    if not ticker and candles_15m:
        ticker = {
            "price": candles_15m[-1].close,
            "change24h": pct(candles_15m[-1].close, candles_15m[0].open),
            "volume24h": 0,
            "source": "OKX candles",
            "symbol": OKX_INST_ID,
            "checked_at": iso_now(),
        }
    return {
        "ticker": ticker,
        "candles_3m": candles_3m,
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "candles_4h": candles_4h,
        "trades": get_okx_trades(),
        "book": get_okx_book(),
        "open_interest": get_okx_open_interest(),
        "funding": get_okx_funding_rate(),
    }


def refresh_context_price(context):
    """Refresh the current TradingView price immediately before decision/message.

    The bot can spend several seconds collecting news/candles/orderbook. During
    that time BZU can move fast, so the entry plan must be based on the newest
    ticker, not on the price captured at the beginning of the run.
    """
    if not isinstance(context, dict):
        return context
    old_price = safe_float(context.get("price"))
    tv_latest = get_tradingview_price_fallback()
    okx_latest = get_okx_ticker()
    latest = tv_latest or okx_latest
    new_price = safe_float((latest or {}).get("price"))
    if not new_price:
        context["price_checked_at"] = iso_now()
        context["price_status"] = "refresh_failed_using_previous"
        return context

    context["price_checked_at"] = iso_now()
    context["price_source"] = latest.get("source") or context.get("price_source") or "ticker"
    context["price_symbol"] = latest.get("symbol") or context.get("price_symbol") or ""
    if tv_latest and okx_latest:
        tv_price = safe_float(tv_latest.get("price"))
        okx_price = safe_float(okx_latest.get("price"))
        if tv_price and okx_price:
            cross_diff = abs(tv_price - okx_price) / tv_price * 100
            context["cross_price_diff_pct"] = round(cross_diff, 4)
            context["okx_reference_price"] = round_price(okx_price)
            context["price_warning"] = cross_diff > PRICE_SOURCE_MAX_DIFF_PCT

    if old_price:
        diff_pct = abs(new_price - old_price) / old_price * 100
        context["price_refresh_diff_pct"] = round(diff_pct, 4)
        context["price_before_refresh"] = round_price(old_price)
    else:
        diff_pct = 0
        context["price_refresh_diff_pct"] = 0
        context["price_before_refresh"] = None

    # Always use the freshest ticker for the Telegram price and any new plan.
    context["price"] = round_price(new_price)
    context["price_status"] = "fresh" if diff_pct <= 0.08 else "updated_before_signal"
    return context


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
    """TradingView-style ATR using Wilder/RMA smoothing."""
    if not candles or len(candles) < 2:
        return None

    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))

    if not trs:
        return None

    if len(trs) < period:
        return mean(trs)

    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = ((atr_val * (period - 1)) + tr) / period
    return atr_val


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


def _filter_fresh_ict_zones(zones, candles, max_age_hours):
    """Keep ICT zones fresh only. Old FVG/OB zones often become noise after many runs."""
    if not zones:
        return []
    if not candles:
        return [z for z in zones if not z.get("mitigated")][:24]
    try:
        last_ts = max(int(c.ts) for c in candles if getattr(c, "ts", None))
    except Exception:
        return [z for z in zones if not z.get("mitigated")][:24]
    max_age_ms = int(max_age_hours * 60 * 60 * 1000)
    fresh = []
    for z in zones:
        if z.get("mitigated"):
            continue
        created = int(z.get("created_ts") or 0)
        if not created or last_ts - created <= max_age_ms:
            fresh.append(z)
    return fresh


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
    memory["fvg_zones"] = _filter_fresh_ict_zones(memory["fvg_zones"], candles_15m, ICT_FVG_MAX_AGE_HOURS)
    memory["ob_zones"] = _filter_fresh_ict_zones(memory["ob_zones"], candles_15m, ICT_OB_MAX_AGE_HOURS)

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

    # BOS continuation without perfect retest:
    # If BOS has happened and the next candles hold the broken level instead of
    # fully returning into FVG/OB, the bot can treat this as continuation.
    recent3 = recent[-3:]
    recent5 = recent[-5:]
    swing_high = safe_float((structure or {}).get("swing_high"))
    swing_low = safe_float((structure or {}).get("swing_low"))
    bos_long_hold = bool(
        bos_long and swing_high and
        min(c.close for c in recent3) >= swing_high - atr15 * 0.12 and
        last.close >= swing_high and
        not displacement_up
    )
    bos_short_hold = bool(
        bos_short and swing_low and
        max(c.close for c in recent3) <= swing_low + atr15 * 0.12 and
        last.close <= swing_low and
        not displacement_down
    )
    bos_long_continuation = bool(
        bos_long and swing_high and
        last.close > swing_high and
        min(c.low for c in recent5) >= swing_high - atr15 * 0.28 and
        abs(move_3) <= 0.55
    )
    bos_short_continuation = bool(
        bos_short and swing_low and
        last.close < swing_low and
        max(c.high for c in recent5) <= swing_low + atr15 * 0.28 and
        abs(move_3) <= 0.55
    )

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
        elif bos_long_hold or bos_long_continuation:
            local += 12
            entry_ok = True
            setup = "BOS_LONG_CONTINUATION_HOLD"
            notes.append("BOS LONG утримує пробитий рівень — continuation без повного ретесту")
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
        elif bos_short_hold or bos_short_continuation:
            local -= 12
            entry_ok = True
            setup = "BOS_SHORT_CONTINUATION_HOLD"
            notes.append("BOS SHORT утримує пробитий рівень — continuation без повного ретесту")
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

    sample_size = len(fresh_trades)
    confidence = "HIGH"
    confidence_factor = 1.0
    if sample_size < max(20, int(CVD_MIN_FRESH_TRADES * 0.5)):
        confidence = "LOW"
        confidence_factor = CVD_LOW_CONFIDENCE_FACTOR
        notes.append("CVD low confidence: мало свіжих трейдів")
    elif sample_size < CVD_MIN_FRESH_TRADES:
        confidence = "MEDIUM"
        confidence_factor = CVD_MEDIUM_CONFIDENCE_FACTOR
        notes.append("CVD medium confidence: вибірка обмежена")

    score = int(score * confidence_factor)

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
        "confidence": confidence,
        "confidence_factor": round(confidence_factor, 2),
        "sample_size": sample_size,
        "delta": round(delta, 4),
        "delta_pct": round(delta_pct, 2),
        "cvd": round(cvd_value, 4),
        "cvd_change_pct": round(cvd_change_pct, 3),
        "last_trade_ts": last_trade_ts,
        "price_move_pct": round(price_move, 3),
        "note": "; ".join(notes[:4]) if notes else "CVD без явної переваги",
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
    # BZU is much cleaner during London/NY. Asia/quiet hours are not forbidden,
    # but entries must be treated as lower-liquidity / higher slippage risk.
    if 12 <= hour < 20:
        return {"name": "NEW YORK / LONDON", "score": 4, "liquidity_risk": False, "note": "ліквідна сесія"}
    if 7 <= hour < 12:
        return {"name": "LONDON", "score": 2, "liquidity_risk": False, "note": "європейська сесія"}
    return {"name": "ASIA / QUIET", "score": -6, "liquidity_risk": True, "note": "тиха сесія: ризик спреду/slippage"}


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
    newest_age_min = None
    for item in items[:35]:
        item_score, impact = score_news_item(item.get("title", ""))
        age_min = None
        published = item.get("published_at")
        try:
            if published:
                age_min = max(0, (now_utc() - published.astimezone(timezone.utc)).total_seconds() / 60)
                newest_age_min = age_min if newest_age_min is None else min(newest_age_min, age_min)
        except Exception:
            age_min = None
        # Google RSS is delayed for fast oil events. Use it as fuel/context, not as a late entry trigger.
        age_factor = 1.0
        if age_min is None:
            age_factor = 0.60
        elif age_min <= 30:
            age_factor = 1.0
        elif age_min <= 60:
            age_factor = 0.60
        else:
            age_factor = 0.25
        item_score = int(item_score * age_factor)
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
        "newest_age_min": round(newest_age_min, 1) if newest_age_min is not None else None,
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




# ==========================================================
# SETUP ENGINE
# ==========================================================


def _parse_iso_time(value):
    try:
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def analyze_reentry_cooldown(state, max_age_hours=6):
    """Soft same-direction cooldown after a weak exit.

    Old behavior was too strict: after a weak LONG/SHORT exit the bot could
    block the same direction for hours and keep printing WATCH. New behavior:
    - no hard ban by default;
    - apply a quality penalty for the same direction;
    - remove/override the penalty when a fresh BOS/reclaim/ICT continuation appears;
    - prevent revenge-looping with a max same-side entries guard.
    """
    history = (state or {}).get("history") or []
    now = now_utc()
    weak_actions = {
        "EXIT", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK",
        "EXIT_AFTER_TP1_GIVEBACK", "PROTECT_OR_EXIT", "STOP",
    }

    def item_age_hours(item):
        ts = _parse_iso_time(item.get("time"))
        return ((now - ts).total_seconds() / 3600.0) if ts else 999.0

    last_entry_quality_by_side = {}
    same_side_entries_3h = {"LONG": 0, "SHORT": 0}
    for item in reversed(history[-MAX_HISTORY:]):
        if not isinstance(item, dict):
            continue
        age_h = item_age_hours(item)
        side_i = item.get("side")
        if side_i in ["LONG", "SHORT"] and item.get("type") == "ENTRY":
            if side_i not in last_entry_quality_by_side:
                last_entry_quality_by_side[side_i] = int(item.get("quality") or 0)
            if age_h <= 3.0:
                same_side_entries_3h[side_i] += 1

    for item in reversed(history[-MAX_HISTORY:]):
        if not isinstance(item, dict) or item.get("type") != "TRADE_CLOSED":
            continue
        side = item.get("side")
        if side not in ["LONG", "SHORT"]:
            continue
        action = str(item.get("action") or "")
        result_pct = safe_float(item.get("result_pct"), 0.0) or 0.0
        age_h = item_age_hours(item)
        if age_h > max_age_hours:
            return {"active": False}

        is_weak = (
            action in weak_actions
            and (
                result_pct <= 0.25
                or action in ["STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK", "EXIT_AFTER_TP1_GIVEBACK"]
            )
        )
        if is_weak:
            # Penalty fades with time so the bot does not sit silent all day.
            if age_h <= 1.0:
                penalty = 15
            elif age_h <= 2.5:
                penalty = 10
            else:
                penalty = 6
            return {
                "active": True,
                "side": side,
                "action": action,
                "result_pct": round(result_pct, 3),
                "age_hours": round(age_h, 2),
                "quality_penalty": penalty,
                "last_entry_quality": last_entry_quality_by_side.get(side, 0),
                "same_side_entries_3h": same_side_entries_3h.get(side, 0),
                "max_same_side_entries_3h": 2,
                "reason": f"після слабкого виходу {side} ({action}, {round(result_pct, 3)}%) якість нового входу тимчасово знижена, але не заблокована",
            }
        return {"active": False}
    return {"active": False}

def detect_market_regime(context, side=None):
    """Classify market state for dynamic TP/Stop and exits.

    The regime must not confuse a normal pullback with range:
    - PULLBACK: HTF still supports the side, and price is in correct ICT side.
    - RANGE/BALANCE: EQ/midrange + neutral 15M/structure + choppy 3M.
    - TREND: 15M/1H/structure align and the market has momentum.
    """
    if not isinstance(context, dict):
        return {"name": "UNKNOWN", "score": 0, "reason": "немає контексту"}

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    news = context.get("news") or {}
    calendar = context.get("calendar") or {}
    liquidity = context.get("liquidity") or {}

    phase = str(structure.get("phase") or "").upper()
    pd = str(ict.get("premium_discount") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    range_atr = safe_float(tf15.get("range_atr"), 0) or 0
    move8 = abs(safe_float(tf15.get("move_8_pct"), 0) or 0)
    score_total = abs(int(context.get("total_score", 0) or 0))
    bias = side if side in ["LONG", "SHORT"] else context.get("bias")

    ict_balance, balance_reason = detect_ict_balance_market(context, bias if bias in ["LONG", "SHORT"] else None)
    near_eq = pd == "MIDRANGE" or ict_setup == "BALANCE_MIDRANGE"
    tf15_neutral = tf15.get("bias") == "NEUTRAL" or tf15.get("trend") == "RANGE" or abs(int(tf15.get("score", 0) or 0)) < 26
    structure_neutral = structure.get("bias") == "NEUTRAL" or phase in ["RANGE / WAIT", "NO DATA", ""]
    tf3_choppy = tf3.get("bias") == "NEUTRAL" or abs(int(tf3.get("score", 0) or 0)) < 32

    if calendar.get("active") or (abs(int(news.get("score", 0) or 0)) >= 35 and move8 >= 1.2):
        return {"name": "NEWS_IMPULSE", "score": 70, "reason": "новинний/подієвий імпульс — цілі ближче, ризик різкого відкату"}

    if bias in ["LONG", "SHORT"]:
        same15 = tf15.get("bias") == bias
        same1h = tf1h.get("bias") == bias or side_score(int(tf1h.get("score", 0) or 0), bias) >= 22
        same4h = tf4h.get("bias") == bias or side_score(int(tf4h.get("score", 0) or 0), bias) >= 18
        same_struct = structure.get("bias") == bias
        same3 = tf3.get("bias") == bias
        correct_pd = (bias == "LONG" and pd == "DISCOUNT") or (bias == "SHORT" and pd == "PREMIUM")

        if (same1h or same4h) and correct_pd and not same15 and not near_eq:
            return {"name": "PULLBACK", "score": 68, "reason": "відкат у правильну ICT-зону за старшим напрямом"}

        if same15 and same1h and (same_struct or same3) and range_atr >= 3.0 and score_total >= 70:
            return {"name": "TREND", "score": 85, "reason": "15M/1H/структура підтримують тренд — можна тримати довше"}

        if any(x in phase for x in ["CHOCH", "SWEEP"]) or (same_struct and not same1h):
            return {"name": "REVERSAL", "score": 62, "reason": "розворотний режим — прибуток захищати швидше, ніж у тренді"}

    if ict_balance or (near_eq and tf15_neutral and (structure_neutral or tf3_choppy)):
        return {"name": "RANGE", "score": 55, "reason": balance_reason or "баланс/EQ — брати швидший TP і не тримати до дальніх цілей"}

    if move8 >= 1.5 and liquidity.get("event") not in ["QUIET", ""]:
        return {"name": "IMPULSE", "score": 60, "reason": "імпульсний рух після ліквідності — потрібен швидкий захист прибутку"}

    return {"name": "NORMAL", "score": 50, "reason": "звичайний intraday режим"}


def trade_mode_profile(context, side=None):
    """Dynamic TP/Stop profile by market regime.

    Values are conservative for BZU intraday. They affect new plans and profit
    management, but do not change the core signal direction.
    """
    regime = detect_market_regime(context, side)
    name = regime.get("name", "NORMAL")
    profiles = {
        # Range: do not ask for +2.18% if the market is balanced. Take the first
        # realistic edge at +0.75–1.0% and protect aggressively.
        "RANGE": {"tp1_pct": 0.72, "tp2_pct": 1.15, "tp3_pct": 1.80, "max_stop_pct": 1.05, "be_trigger": 0.40, "protect_trigger": 0.62, "giveback": 0.20},
        "PULLBACK": {"tp1_pct": 0.95, "tp2_pct": 1.75, "tp3_pct": 3.00, "max_stop_pct": 1.35, "be_trigger": 0.52, "protect_trigger": 0.82, "giveback": 0.30},
        # TREND: TP2/TP3 stay far, but TP1 is realistic for BZU intraday.
        # The previous 2.18% TP1 often missed +0.7–1.0% moves and gave profit back.
        # TREND: do not put TP1 too close, but also do not force TP1 to the far swing.
        # First partial/protection target is ATR-based (~1.2–1.5 ATR). Stop remains ICT/SMC-based.
        "TREND": {"tp1_pct": 0.00, "tp2_pct": 2.45, "tp3_pct": 4.80, "max_stop_pct": MAX_STOP_DISTANCE_PCT, "be_trigger": 0.70, "protect_trigger": 1.00, "giveback": 0.40},
        "REVERSAL": {"tp1_pct": 0.95, "tp2_pct": 1.80, "tp3_pct": 3.10, "max_stop_pct": 1.45, "be_trigger": 0.48, "protect_trigger": 0.78, "giveback": 0.25},
        "NEWS_IMPULSE": {"tp1_pct": 0.85, "tp2_pct": 1.55, "tp3_pct": 2.70, "max_stop_pct": 1.25, "be_trigger": 0.45, "protect_trigger": 0.75, "giveback": 0.50},
        "IMPULSE": {"tp1_pct": 0.92, "tp2_pct": 1.65, "tp3_pct": 2.80, "max_stop_pct": 1.35, "be_trigger": 0.48, "protect_trigger": 0.78, "giveback": 0.35},
        "NORMAL": {"tp1_pct": 1.05, "tp2_pct": 1.95, "tp3_pct": 3.40, "max_stop_pct": 1.55, "be_trigger": 0.55, "protect_trigger": 0.88, "giveback": 0.35},
        "UNKNOWN": {"tp1_pct": 1.05, "tp2_pct": 1.95, "tp3_pct": 3.40, "max_stop_pct": 1.55, "be_trigger": 0.55, "protect_trigger": 0.88, "giveback": 0.35},
    }
    profile = dict(profiles.get(name, profiles["NORMAL"]))
    profile["regime"] = name
    profile["reason"] = regime.get("reason", "")
    return profile



def regime_label(regime):
    """User-facing Ukrainian label for internal market regime codes."""
    name = regime
    if isinstance(regime, dict):
        name = regime.get("name")
    name = str(name or "NORMAL").upper()
    labels = {
        "TREND": "сильний тренд",
        "RANGE": "боковик",
        "PULLBACK": "відкат у тренді",
        "REVERSAL": "розворот",
        "NEWS_IMPULSE": "новинний імпульс",
        "IMPULSE": "новинний імпульс",
        "NORMAL": "звичайний intraday режим",
        "UNKNOWN": "звичайний intraday режим",
    }
    return labels.get(name, "звичайний intraday режим")


def enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15=None):
    """Force professional Smart Money minimum risk/reward in the plan.

    ICT/SMC chooses the entry/stop idea first. After that this guard makes
    sure TP1 is not mathematically worse than the stop. If risk is 1%, TP1
    must be at least 1% away. TP2/TP3 are kept beyond TP1 so the ladder stays
    logical.
    """
    price = safe_float(price)
    stop = safe_float(stop)
    tp1 = safe_float(tp1)
    tp2 = safe_float(tp2)
    tp3 = safe_float(tp3)
    atr15 = safe_float(atr15, (price or 90) * 0.006) or ((price or 90) * 0.006)
    if side not in ["LONG", "SHORT"] or not price or stop is None or tp1 is None:
        return stop, tp1, tp2, tp3

    risk = abs(price - stop)
    if risk <= 0:
        return stop, tp1, tp2, tp3

    min_tp1_distance = risk * max(1.0, MIN_RR1_ENTRY)
    min_tp2_distance = risk * max(1.45, MIN_RR1_ENTRY + 0.45)
    min_tp3_distance = risk * max(2.10, MIN_RR1_ENTRY + 1.10)
    step = max(atr15 * 0.45, price * 0.003)

    if side == "LONG":
        tp1 = max(tp1, price + min_tp1_distance)
        tp2 = max(tp2 if tp2 is not None else tp1 + step, price + min_tp2_distance, tp1 + step)
        tp3 = max(tp3 if tp3 is not None else tp2 + step, price + min_tp3_distance, tp2 + step)
    else:
        tp1 = min(tp1, price - min_tp1_distance)
        tp2 = min(tp2 if tp2 is not None else tp1 - step, price - min_tp2_distance, tp1 - step)
        tp3 = min(tp3 if tp3 is not None else tp2 - step, price - min_tp3_distance, tp2 - step)

    return stop, tp1, tp2, tp3


def smart_money_rr_status(plan):
    """Return whether the plan has acceptable Smart Money RR."""
    if not plan:
        return {"ok": False, "rr1": 0, "label": "RR недоступний"}
    rr1 = safe_float(getattr(plan, "rr1", 0), 0) or 0
    if rr1 < MIN_RR1_ENTRY:
        return {
            "ok": False,
            "rr1": rr1,
            "label": f"RR1 {round(rr1, 2)} < {round(MIN_RR1_ENTRY, 2)} — TP1 менший за стоп",
        }
    if rr1 >= STRONG_RR1_BONUS:
        label = f"Smart Money RR сильний: {round(rr1, 2)}R"
    elif rr1 >= GOOD_RR1_BONUS:
        label = f"Smart Money RR добрий: {round(rr1, 2)}R"
    else:
        label = f"Smart Money RR мінімальний: {round(rr1, 2)}R"
    return {"ok": True, "rr1": rr1, "label": label}


def cooldown_override_ok(side, context):
    """Allow professional same-side re-entry after a weak exit.

    Old behavior was too strict: any EXIT_LOCAL_BREAK/weak close blocked the
    same direction until a perfect new BOS/reclaim. In a strong trend this can
    make the bot watch the whole continuation without re-entering.

    New behavior:
    - still blocks random re-entry in RANGE/BALANCE;
    - allows same-side continuation only when the market did NOT reverse and
      there is fresh trend/structure/ICT evidence;
    - does not require CVD/flow to be fully aligned, but blocks if both are
      strongly against.
    """
    if side not in ["LONG", "SHORT"] or not isinstance(context, dict):
        return False

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    market_regime = context.get("market_regime") or {}
    if isinstance(market_regime, dict):
        regime_name = str(market_regime.get("name") or "NORMAL").upper()
    else:
        regime_name = str(market_regime or "NORMAL").upper()

    fresh_bos = (side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase)
    fresh_sweep = (side == "LONG" and "DOWNSIDE SWEEP" in phase) or (side == "SHORT" and "UPSIDE SWEEP" in phase)
    fresh_choch = (side == "LONG" and "CHOCH LONG" in phase) or (side == "SHORT" and "CHOCH SHORT" in phase)
    ict_continuation = (
        side == "LONG" and ict_setup in ["BOS_LONG_RETRACE_FVG_OB", "BOS_LONG_CONTINUATION_HOLD", "LIQUIDITY_SWEEP_LONG"]
    ) or (
        side == "SHORT" and ict_setup in ["BOS_SHORT_RETRACE_FVG_OB", "BOS_SHORT_CONTINUATION_HOLD", "LIQUIDITY_SWEEP_SHORT"]
    )

    tf3_same = tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22
    tf15_same = tf15.get("bias") == side and abs(int(tf15.get("score", 0) or 0)) >= 26
    tf1h_support = tf1h.get("bias") == side or side_score(int(tf1h.get("score", 0) or 0), side) >= 18
    tf4h_support = tf4h.get("bias") == side or side_score(int(tf4h.get("score", 0) or 0), side) >= 18
    structure_support = structure.get("bias") == side or fresh_bos or fresh_sweep or fresh_choch
    ict_support = ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16 or ict_continuation)

    # Do not override if price/liquidity model explicitly blocks this side.
    if side in (liquidity.get("blocks") or []):
        return False

    cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 22
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 18
    oi_against = derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14
    if sum([bool(cvd_against), bool(flow_against), bool(oi_against)]) >= 2:
        return False

    # Fresh BOS/sweep/ICT continuation is the cleanest unlock.
    fresh_structure_unlock = bool(
        (fresh_bos or fresh_sweep or fresh_choch or ict_continuation)
        and (tf3_same or tf15_same)
        and (structure_support or ict_support)
    )

    # Trend-continuation unlock: after a weak exit, allow re-entry if the trend
    # clearly did not reverse and intraday direction is aligned again.
    trend_continuation_unlock = bool(
        regime_name in ["TREND", "PULLBACK", "IMPULSE", "NEWS_IMPULSE"]
        and tf15_same
        and (tf3_same or structure_support or ict_support)
        and (tf1h_support or tf4h_support or structure_support)
    )

    # Normal continuation unlock is stricter: needs 15M + structure/ICT + 3M.
    normal_continuation_unlock = bool(
        tf15_same
        and tf3_same
        and (structure_support or ict_support)
        and side_score(int(context.get("total_score", 0) or 0), side) >= 45
    )

    return bool(fresh_structure_unlock or trend_continuation_unlock or normal_continuation_unlock)


def cooldown_override_reason(side, context):
    """Short Ukrainian explanation when same-side cooldown is professionally unlocked."""
    market_regime = context.get("market_regime") or {}
    regime_name = market_regime.get("name") if isinstance(market_regime, dict) else market_regime
    if str(regime_name or "").upper() in ["TREND", "PULLBACK", "IMPULSE", "NEWS_IMPULSE"]:
        return f"після слабкого виходу {side} дозволено повторний вхід: тренд/структура продовжуються"
    return f"після слабкого виходу {side} дозволено повторний вхід: зʼявився новий BOS/reclaim або ICT continuation"

def analyze_failed_trade_reversal(state, context, max_age_minutes=150):
    """Detect a professional reversal setup after a failed trade.

    This module is intentionally NOT an automatic flip. It activates only after
    a weak/failed close (STOP, EXIT_LOCAL_BREAK, EXIT_MFE_GIVEBACK, etc.) and
    then watches the opposite side for 3M structure + ICT/SMC confirmation.

    Example:
      failed SHORT -> watch LONG only if 3M turns up, there is no new lower low,
      and CVD/flow are not strongly against the reversal.
    """
    if not isinstance(state, dict) or not isinstance(context, dict):
        return {"active": False}

    history = state.get("history") or []
    failed_actions = {
        "STOP", "EXIT", "EXIT_LOCAL_BREAK", "EXIT_STRUCTURE_BREAK",
        "EXIT_WARNING_CONFIRM", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK",
        "EXIT_AFTER_TP1_GIVEBACK", "PROTECT_OR_EXIT",
    }

    now = now_utc()
    last_close = None
    for item in reversed(history[-MAX_HISTORY:]):
        if isinstance(item, dict) and item.get("type") == "TRADE_CLOSED":
            last_close = item
            break
    if not last_close:
        return {"active": False}

    failed_side = last_close.get("side")
    action = str(last_close.get("action") or "")
    result_pct = safe_float(last_close.get("result_pct"), 0.0) or 0.0
    ts = _parse_iso_time(last_close.get("time"))
    age_min = ((now - ts).total_seconds() / 60.0) if ts else 99999.0

    # Reversal mode is for recent failed/weak exits only.
    weak_failed_close = action in failed_actions and (result_pct <= 0.35 or action in {
        "STOP", "EXIT_LOCAL_BREAK", "EXIT_STRUCTURE_BREAK", "EXIT_WARNING_CONFIRM",
        "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_AFTER_TP1_GIVEBACK",
    })
    if failed_side not in ["LONG", "SHORT"] or not weak_failed_close or age_min > max_age_minutes:
        return {"active": False}

    side = opposite(failed_side)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    clusters = context.get("clusters") or {}
    candles_3m = context.get("candles_3m") or []

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()

    bos_reclaim = (
        (side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase))
        or (side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase))
    )
    ict_reclaim = (
        (side == "LONG" and ict_setup in ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "BOS_LONG_CONTINUATION_HOLD"])
        or (side == "SHORT" and ict_setup in ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "BOS_SHORT_CONTINUATION_HOLD"])
    )

    # 3M micro reversal shape: higher low/no lower low after failed SHORT,
    # lower high/no higher high after failed LONG.
    micro_reversal = False
    micro_reason = ""
    try:
        recent = candles_3m[-10:]
        if len(recent) >= 6:
            first = recent[:len(recent)//2]
            last = recent[len(recent)//2:]
            if side == "LONG":
                micro_reversal = min(c.low for c in last) >= min(c.low for c in first) and recent[-1].close > mean([c.close for c in recent[-4:]])
                micro_reason = "3M робить higher low після зламаного SHORT"
            else:
                micro_reversal = max(c.high for c in last) <= max(c.high for c in first) and recent[-1].close < mean([c.close for c in recent[-4:]])
                micro_reason = "3M робить lower high після зламаного LONG"
    except Exception:
        micro_reversal = False

    score = 0
    confirmations = []
    conflicts = []

    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22:
        score += 24
        confirmations.append("3M розвернувся після невдалої угоди")
    elif tf3.get("bias") == side:
        score += 14
        confirmations.append("3M починає розвертатись")
    elif tf3.get("bias") == failed_side and abs(int(tf3.get("score", 0) or 0)) >= 32:
        score -= 20
        conflicts.append("3M ще не зламав попередній напрям")

    if tf15.get("bias") == side:
        score += 18
        confirmations.append("15M вже підтримує розворот")
    elif tf15.get("bias") == "NEUTRAL":
        score += 6
        confirmations.append("15M нейтральний — розворот можливий, але потрібен 3M/BOS")
    elif tf15.get("bias") == failed_side:
        score -= 14
        conflicts.append("15M ще тримає старий напрям")

    if bos_reclaim:
        score += 24
        confirmations.append("SMC/BOS підтверджує розворот")
    elif structure.get("bias") == side:
        score += 14
        confirmations.append("структура вже за новий напрям")
    elif structure.get("bias") == failed_side:
        score -= 12
        conflicts.append("структура ще проти розвороту")

    if ict_reclaim or (ict.get("bias") == side and bool(ict.get("entry_ok"))):
        score += 18
        confirmations.append("ICT дає sweep/reclaim або continuation для розвороту")
    elif ict.get("bias") == side:
        score += 8
        confirmations.append("ICT підтримує ідею розвороту")
    elif ict.get("bias") == failed_side and ict.get("state") == "ENTRY_MODEL":
        score -= 16
        conflicts.append("ICT ще підтримує старий напрям")

    if micro_reversal:
        score += 10
        confirmations.append(micro_reason)

    # Flow/CVD must not be strongly against the reversal. They are not required
    # to be perfect, because reversal often starts before all flow confirms.
    cvd_against = cvd.get("bias") == failed_side and abs(int(cvd.get("score", 0) or 0)) >= 22
    flow_against = flow.get("bias") == failed_side and abs(int(flow.get("score", 0) or 0)) >= 14
    if cvd.get("bias") == side:
        score += 10
        confirmations.append("CVD підтримує розворот")
    elif cvd_against:
        score -= 12
        conflicts.append("CVD ще проти розвороту")
    else:
        score += 3

    if flow.get("bias") == side:
        score += 8
        confirmations.append("потік підтримує розворот")
    elif flow_against:
        score -= 10
        conflicts.append("потік ще проти розвороту")
    else:
        score += 2

    if clusters.get("bias") == side:
        score += 4

    score = int(max(0, min(100, score)))
    allow_watch = score >= 52 and (tf3.get("bias") == side or bos_reclaim or ict_reclaim or micro_reversal)
    allow_entry = score >= 68 and (bos_reclaim or ict_reclaim or tf15.get("bias") == side) and not (cvd_against and flow_against)

    return {
        "active": bool(allow_watch),
        "side": side,
        "failed_side": failed_side,
        "failed_action": action,
        "failed_result_pct": round(result_pct, 3),
        "age_minutes": round(age_min, 1),
        "score": score,
        "allow_entry": bool(allow_entry),
        "reason": f"попередній {failed_side} зламано ({action}, {round(result_pct, 3)}%); шукати {side} тільки після 3M + BOS/reclaim",
        "confirmations": confirmations[:5],
        "conflicts": conflicts[:5],
    }


def build_context(data, state=None):
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price"))
    context_change24h = safe_float(ticker.get("change24h"), 0)
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
    liquidity = analyze_liquidations(data.get("candles_3m") or [], data.get("candles_15m") or [], flow, structure, price)
    news_items = get_news()
    news = analyze_news(news_items)
    calendar = analyze_calendar_alerts()
    session = market_session()

    price = price or safe_float(tf15.get("close"))
    price_warning = bool(ticker.get("price_warning"))
    low_liquidity_risk = bool(session.get("liquidity_risk")) and safe_float(volume_guard.get("ratio"), 1.0) <= LOW_LIQUIDITY_VOLUME_RATIO

    atr15 = safe_float(structure.get("atr")) or safe_float(tf15.get("atr")) or (price or 90) * 0.006

    # Intraday architecture for trades lasting a few hours:
    # 15m/structure/3m are the engine. CVD and OI confirm quality.
    # 4H is background, but a strong opposite 4H/1H trend must reduce ICT-only
    # entries. This prevents the bot from printing market ENTRY when only ICT is
    # bullish/bearish while 4H is strongly against and 15M/3M are still neutral.
    ict_score_raw = safe_float(ict.get("score"), 0) or 0
    ict_score_used = ict_score_raw
    ict_bias = ict.get("bias", "NEUTRAL")
    if ict_bias == "LONG":
        if (tf4h.get("score", 0) or 0) <= -35 and tf15.get("bias") != "LONG":
            ict_score_used *= 0.55
        if (tf1h.get("score", 0) or 0) <= -15 and tf15.get("bias") != "LONG":
            ict_score_used *= 0.75
        if tf15.get("bias") != "LONG" and tf3.get("bias") != "LONG" and structure.get("bias") != "LONG":
            ict_score_used *= 0.70
    elif ict_bias == "SHORT":
        if (tf4h.get("score", 0) or 0) >= 35 and tf15.get("bias") != "SHORT":
            ict_score_used *= 0.55
        if (tf1h.get("score", 0) or 0) >= 15 and tf15.get("bias") != "SHORT":
            ict_score_used *= 0.75
        if tf15.get("bias") != "SHORT" and tf3.get("bias") != "SHORT" and structure.get("bias") != "SHORT":
            ict_score_used *= 0.70

    # BTC↔BZ filter is intentionally removed.
    # Quality/market bias is recalculated from BZU-native data only:
    # ICT/SMC + 15M/3M/1H + CVD/Flow/OI/Liquidity/Clusters.
    tech_score = (
        tf15.get("score", 0) * 1.50
        + structure.get("score", 0) * 1.28
        + ict_score_used * 1.38
        + tf3.get("score", 0) * 0.52
        + tf1h.get("score", 0) * 0.48
        + cvd.get("score", 0) * 0.78
        + derivatives.get("score", 0) * 0.65
        + liquidity.get("score", 0) * 0.72
        + flow.get("score", 0) * 0.52
        + clusters.get("score", 0) * 0.18
        + tf4h.get("score", 0) * 0.10
        + volume_guard.get("score", 0)
    )
    total_score = tech_score + news.get("score", 0) * 0.22 + calendar.get("score", 0) + session.get("score", 0)
    if low_liquidity_risk:
        total_score -= 10 if tech_score > 0 else -10
    if price_warning:
        total_score *= 0.92

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
    temp_context_for_regime = {
        "price": price,
        "atr15": atr15,
        "change24h": context_change24h,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "structure": structure,
        "ict": ict,
        "news": news,
        "calendar": calendar,
        "liquidity": liquidity,
        "total_score": int(total_score),
        "bias": bias,
    }
    market_regime = detect_market_regime(temp_context_for_regime, bias if bias in ["LONG", "SHORT"] else None)
    reentry_cooldown = analyze_reentry_cooldown(state)

    # Reversal engine after a failed/weak trade. It is attached to context so
    # setup evaluation can show REVERSAL WATCH or allow a confirmed opposite entry.
    temp_context_for_reversal = dict(temp_context_for_regime)
    temp_context_for_reversal.update({
        "candles_3m": data.get("candles_3m") or [],
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
    })
    reversal_after_failed_trade = analyze_failed_trade_reversal(state, temp_context_for_reversal)

    return {
        "price": price,
        "change24h": safe_float(ticker.get("change24h"), 0),
        "source": ticker.get("source", "unknown"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "atr15": atr15,
        "candles_3m": data.get("candles_3m") or [],
        "candles_15m": data.get("candles_15m") or [],
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "structure": structure,
        "ict": ict,
        "ict_score_raw": int(ict_score_raw),
        "ict_score_used": int(ict_score_used),
        "pending_ict_zones": pending_ict_zones,
        "market_regime": market_regime,
        "reentry_cooldown": reentry_cooldown,
        "reversal_after_failed_trade": reversal_after_failed_trade,
        "volume_guard": volume_guard,
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
        "liquidity": liquidity,
        "news": news,
        "calendar": calendar,
        "session": session,
        "low_liquidity_risk": low_liquidity_risk,
        "price_warning": price_warning,
        "cross_price_diff_pct": ticker.get("cross_price_diff_pct"),
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


def detect_ict_balance_market(context, side=None):
    """ICT balance / midrange filter.

    ICT idea used here:
    - the middle of the dealing range around equilibrium is low edge;
    - in balance we do NOT open market entries from the middle;
    - valid trades come from sweep/reclaim at range edges or BOS + FVG/OB retest.

    Important: this must not confuse a normal pullback with a range.
    A pullback is allowed when price is in discount for LONG / premium for SHORT
    and structure/HTF direction still supports continuation.
    """
    if not isinstance(context, dict):
        return False, ""

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}

    if not price or not atr15:
        return False, ""

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    pd = str(ict.get("premium_discount") or "").upper()
    pd_pos = safe_float(ict.get("pd_pos"), 0.5)
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    has_bos_or_sweep = any(x in phase for x in ["BOS LONG", "BOS SHORT", "UPSIDE SWEEP", "DOWNSIDE SWEEP", "CHOCH LONG", "CHOCH SHORT"])
    has_ict_entry_model = ict.get("state") == "ENTRY_MODEL" and bool(ict.get("entry_ok"))
    near_equilibrium = pd == "MIDRANGE" or 0.43 <= pd_pos <= 0.57 or ict_setup == "BALANCE_MIDRANGE"

    tf15_neutral_or_range = tf15.get("bias") == "NEUTRAL" or tf15.get("trend") == "RANGE" or abs(int(tf15.get("score", 0) or 0)) < 26
    structure_neutral = structure.get("bias") == "NEUTRAL" or phase in ["RANGE / WAIT", "NO DATA", ""]
    tf3_choppy = tf3.get("bias") == "NEUTRAL" or abs(int(tf3.get("score", 0) or 0)) < 32

    # Pullback exception: do not call it balance if the market has a clear HTF/structure side
    # and price is pulling back into the correct ICT side of the range.
    if side in ["LONG", "SHORT"]:
        htf_support = (tf1h.get("bias") == side or side_score(int(tf1h.get("score", 0) or 0), side) >= 18)
        structure_support = structure.get("bias") == side or (side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase)
        correct_side_of_range = (side == "LONG" and pd == "DISCOUNT") or (side == "SHORT" and pd == "PREMIUM")
        has_retest_zone = bool(ict.get("bull_fvg") or ict.get("bull_ob")) if side == "LONG" else bool(ict.get("bear_fvg") or ict.get("bear_ob"))
        if correct_side_of_range and (structure_support or htf_support) and (has_retest_zone or has_bos_or_sweep):
            return False, ""

    balance_score = 0
    if near_equilibrium:
        balance_score += 2
    if tf15_neutral_or_range:
        balance_score += 1
    if structure_neutral:
        balance_score += 1
    if tf3_choppy:
        balance_score += 1
    if distance_atr <= 0.95:
        balance_score += 1
    if has_bos_or_sweep or has_ict_entry_model:
        balance_score -= 2

    if balance_score >= 4:
        return True, "ICT: ціна біля equilibrium / середина діапазону — входу немає; чекати sweep high/low або BOS + FVG/OB retest"
    return False, ""



def detect_exhausted_move(side, context):
    """Detect when a directional move is already mostly played out.

    This does NOT create an opposite entry. It only prevents the bot from
    opening a fresh LONG after a vertical daily pump, or a fresh SHORT after a
    vertical daily dump. The correct state is WAIT / new structure, not chase.
    """
    if side not in ["LONG", "SHORT"] or not isinstance(context, dict):
        return False, ""

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}
    candles_15m = context.get("candles_15m") or []

    if not price or not atr15:
        return False, ""

    ema20 = safe_float(tf15.get("ema20"))
    rsi15 = safe_float(tf15.get("rsi"), 50)
    rsi1h = safe_float(tf1h.get("rsi"), 50)
    change24h = safe_float(context.get("change24h"), 0)
    move_8 = safe_float(tf15.get("move_8_pct"), 0)
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    recent = candles_15m[-48:] if candles_15m else []
    recent_low = min((c.low for c in recent), default=None)
    recent_high = max((c.high for c in recent), default=None)
    move_from_low = pct(price, recent_low) if recent_low else 0
    move_from_high = pct(price, recent_high) if recent_high else 0

    cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 10
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10
    cluster_against = clusters.get("bias") == opposite(side) or side_score(int(clusters.get("score", 0) or 0), side) <= -4
    liquidity_against = side in (liquidity.get("blocks") or []) or liquidity.get("bias") == opposite(side)
    shorts_closing_or_longs_closing = str(derivatives.get("state", "")).upper() in ["SHORTS_CLOSING", "LONGS_CLOSING"]
    pressure_against_count = sum([bool(cvd_against), bool(flow_against), bool(cluster_against), bool(liquidity_against), bool(shorts_closing_or_longs_closing)])

    if side == "LONG":
        big_daily = change24h >= 3.0 or move_from_low >= 3.2 or move_8 >= 3.0
        stretched = distance_atr >= 1.45 or rsi15 >= 72 or rsi1h >= 74
        near_top = bool(recent_high and (recent_high - price) / price * 100 <= 0.75)
        tired_price_action = structure.get("phase") == "UPSIDE SWEEP" or tf3.get("bias") in ["SHORT", "NEUTRAL"]
        if big_daily and stretched and (near_top or pressure_against_count >= 1 or tired_price_action):
            reasons = []
            if change24h >= 3.0:
                reasons.append(f"за добу вже +{round(change24h, 2)}%")
            elif move_from_low >= 3.2:
                reasons.append(f"від локального low вже +{round(move_from_low, 2)}%")
            if rsi15 >= 72:
                reasons.append(f"15M RSI {round(rsi15, 1)}")
            if distance_atr >= 1.45:
                reasons.append(f"ціна далеко від EMA20 ({round(distance_atr, 2)} ATR)")
            if pressure_against_count >= 1:
                reasons.append("CVD/потік/кластери вже попереджають")
            return True, "LONG вже відпрацював основний імпульс — не доганяти; чекати нову базу/відкат або окремий SHORT-сетап. " + "; ".join(reasons[:4])

    else:
        big_daily = change24h <= -3.0 or move_from_high <= -3.2 or move_8 <= -3.0
        stretched = distance_atr >= 2.0 or rsi15 <= 28 or rsi1h <= 26
        near_bottom = bool(recent_low and (price - recent_low) / price * 100 <= 0.75)
        tired_price_action = structure.get("phase") == "DOWNSIDE SWEEP" or tf3.get("bias") in ["LONG", "NEUTRAL"]
        if big_daily and stretched and (near_bottom or pressure_against_count >= 1 or tired_price_action):
            reasons = []
            if change24h <= -3.0:
                reasons.append(f"за добу вже {round(change24h, 2)}%")
            elif move_from_high <= -3.2:
                reasons.append(f"від локального high вже {round(move_from_high, 2)}%")
            if rsi15 <= 28:
                reasons.append(f"15M RSI {round(rsi15, 1)}")
            if distance_atr >= 1.45:
                reasons.append(f"ціна далеко від EMA20 ({round(distance_atr, 2)} ATR)")
            if pressure_against_count >= 1:
                reasons.append("CVD/потік/кластери вже попереджають")
            return True, "SHORT вже відпрацював основний імпульс — не доганяти; чекати нову базу/відкат або окремий LONG-сетап. " + "; ".join(reasons[:4])

    return False, ""

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


def soft_late_entry_penalty(side, context, late_reason=""):
    """Soft anti-chase penalty.

    It lowers quality after an extended 3M/15M impulse, but it does not hard
    block the entry. Strong ICT + structure can still open an early/reversal
    trade, just with a more honest quality score.
    """
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or (price or 90) * 0.006
    tf15 = context.get("tf15") or {}
    tf3 = context.get("tf3") or {}
    ict = context.get("ict") or {}
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if price and ema20 and atr15 else 0
    fast = abs(safe_float(tf3.get("fast_move_pct"), 0) or 0)
    move8 = abs(safe_float(tf15.get("move_8_pct"), 0) or 0)

    penalty = 0
    if distance_atr >= 1.5:
        penalty += 7
    elif distance_atr >= 1.0:
        penalty += 5
    elif distance_atr >= 0.55:
        penalty += 3

    if fast >= 0.75:
        penalty += 6
    elif fast >= 0.45:
        penalty += 4

    if move8 >= 1.2:
        penalty += 5
    elif move8 >= 0.8:
        penalty += 3

    if ict.get("no_chase") and ict.get("bias") == side:
        penalty += 5

    # A real ICT entry model can justify entering an impulse continuation/reversal,
    # so reduce but never remove the anti-chase discount.
    strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    setup = str(ict.get("setup", "") or "").upper()
    if ict.get("bias") == side and ict.get("entry_ok") and setup in strong_setups.get(side, []):
        penalty = max(3, int(penalty * 0.55))

    return int(max(0, min(18, penalty)))


def analyze_entry_location_score(side, context):
    """Internal Entry Location Score (ELS), hidden from Telegram.

    Purpose: separate a correct direction from a good entry location.
    The score is deliberately SOFT: it does not block trades and it must not
    kill early 3M/ICT entries. It only adjusts quality and supervision risk.

    High score = entry is close to a professional location: ICT zone/reclaim,
    fresh BOS, not too stretched from 15M EMA20/ATR, no obvious liquidity block.
    Low score = direction may still be correct, but the market entry is late or
    far from the smart-money area.
    """
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or (price or 90) * 0.006
    tf15 = context.get("tf15") or {}
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    liquidity = context.get("liquidity") or {}

    opp = opposite(side)
    score = 62.0
    factors = []

    def add(points, key):
        nonlocal score
        score += points
        factors.append((key, points))

    if not price or not atr15:
        return {"score": 62, "adjustment": 0, "state": "NORMAL", "penalty": 0, "bonus": 0, "factors": factors}

    # 1) Stretch from 15M mean. This catches late entries without blocking them.
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0.0
    if distance_atr <= 0.35:
        add(8, "near_ema20")
    elif distance_atr <= 0.75:
        add(4, "fair_distance")
    elif distance_atr >= 1.75:
        add(-10, "very_stretched")
    elif distance_atr >= 1.20:
        add(-7, "stretched")
    elif distance_atr >= 0.95:
        add(-3, "mild_stretch")

    # 2) ICT entry model. This is the best location anchor.
    setup = str(ict.get("setup", "") or "").upper()
    strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    ict_same = ict.get("bias") == side
    ict_against = ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16
    ict_strong = bool(ict_same and ict.get("entry_ok") and setup in strong_setups.get(side, []))
    ict_weak = bool(ict_same and setup in weak_setups.get(side, []))
    if ict_strong:
        add(16, "strong_ict_location")
    elif ict_weak:
        add(7, "weak_ict_location")
    elif ict_same:
        add(2, "ict_context")
    elif ict_against:
        add(-12, "ict_against_location")

    if ict.get("no_chase") and ict_same:
        add(-8, "ict_no_chase")

    # 3) Freshness of BOS / reclaim. Entering close to a fresh broken level is good;
    # chasing far from it is not. This is still soft and cannot block a trade.
    phase = str(structure.get("phase", "") or "").upper()
    swing_high = safe_float(structure.get("swing_high"))
    swing_low = safe_float(structure.get("swing_low"))
    if side == "LONG" and phase in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"]:
        ref = swing_high or swing_low
        if ref:
            d = abs(price - ref) / atr15
            if d <= 0.35:
                add(10, "fresh_reclaim")
            elif d <= 0.75:
                add(5, "acceptable_reclaim")
            elif d >= 1.45:
                add(-7, "late_from_reclaim")
            elif d >= 1.0:
                add(-4, "extended_from_reclaim")
        else:
            add(4, "structure_same")
    elif side == "SHORT" and phase in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]:
        ref = swing_low or swing_high
        if ref:
            d = abs(price - ref) / atr15
            if d <= 0.35:
                add(10, "fresh_reclaim")
            elif d <= 0.75:
                add(5, "acceptable_reclaim")
            elif d >= 1.45:
                add(-7, "late_from_reclaim")
            elif d >= 1.0:
                add(-4, "extended_from_reclaim")
        else:
            add(4, "structure_same")
    elif structure.get("bias") == opp:
        add(-8, "structure_against")

    # 4) 3M impulse quality. Good early 3M trigger is ok; vertical candle gets a small discount.
    fast = safe_float(tf3.get("fast_move_pct"), 0) or 0
    drift = safe_float(tf3.get("drift_pct"), 0) or 0
    if tf3.get("bias") == side:
        if (side == "LONG" and fast >= 0.70) or (side == "SHORT" and fast <= -0.70):
            add(-5, "vertical_3m")
        elif (side == "LONG" and 0.12 <= fast <= 0.45) or (side == "SHORT" and -0.45 <= fast <= -0.12):
            add(4, "clean_3m_trigger")
        elif abs(drift) <= 0.18:
            add(2, "controlled_3m")
    elif tf3.get("bias") == opp:
        add(-6, "3m_against")

    # 5) Liquidity / micro pressure. These are small adjustments, not blockers.
    if side in (liquidity.get("blocks") or []):
        add(-10, "liquidity_blocks_side")
    elif liquidity.get("bias") == side:
        add(4, "liquidity_support")
    elif liquidity.get("bias") == opp:
        add(-5, "liquidity_against")

    pressure_against = 0
    pressure_for = 0
    for block in [cvd, flow, clusters]:
        if block.get("bias") == side:
            pressure_for += 1
        elif block.get("bias") == opp:
            pressure_against += 1
    if pressure_for >= 2:
        add(4, "micro_support")
    if pressure_against >= 2:
        add(-6, "micro_against")
    elif pressure_against == 1:
        add(-3, "one_micro_against")

    # Keep ELS soft. A bad location should not delete the trade; it should only
    # make the score more honest. Strong ICT/early 3M further softens penalties.
    score = int(max(0, min(100, round(score))))

    if score >= 84:
        adjustment = 4
        state = "STRONG"
    elif score >= 72:
        adjustment = 2
        state = "GOOD"
    elif score >= 58:
        adjustment = 0
        state = "NORMAL"
    elif score >= 46:
        adjustment = -3
        state = "WEAK"
    elif score >= 34:
        adjustment = -6
        state = "LATE"
    else:
        adjustment = -9
        state = "VERY_LATE"

    # Do not kill early quality entries: if full ICT + 3M agree, cap the ELS
    # penalty to -4 even if the location model is cautious.
    if adjustment < -4 and ict_strong and tf3.get("bias") == side:
        adjustment = -4
    # Trend continuation without ICT can still be traded, but keep the penalty moderate.
    if adjustment < -7 and tf15.get("bias") == side and tf3.get("bias") == side:
        adjustment = -7

    return {
        "score": score,
        "adjustment": int(adjustment),
        "state": state,
        "penalty": int(abs(adjustment)) if adjustment < 0 else 0,
        "bonus": int(adjustment) if adjustment > 0 else 0,
        "distance_atr": round(distance_atr, 3),
        "factors": factors[-8:],
    }


def entry_quality_adjustment(side, context, late_penalty=0):
    """Unified entry-location quality adjustment.

    This merges Anti-chase + ELS + ICT cap into ONE controlled correction.
    The goal is to avoid double punishment for the same problem:
      - late impulse / stretched price,
      - weak location away from ICT/SMC,
      - no full ICT model.

    It never blocks an entry. It only adjusts quality softly:
      - strong ICT + structure/3M keeps early entries alive;
      - very late non-ICT entries cannot look like high-quality setups;
      - total location penalty is capped so the bot does not overreact.
    """
    context = context if isinstance(context, dict) else {}
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    entry_location = context.get("entry_location") or analyze_entry_location_score(side, context)
    context["entry_location"] = entry_location

    els_adj = int(entry_location.get("adjustment", 0) or 0)
    late_penalty = int(max(0, late_penalty or 0))
    raw_adjustment = els_adj - late_penalty

    ict_setup = str(ict.get("setup", "") or "").upper()
    strong_ict_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_strong = bool(ict.get("bias") == side and ict.get("entry_ok") and ict_setup in strong_ict_setups.get(side, []))
    structure_same = structure.get("bias") == side
    tf3_same = tf3.get("bias") == side
    tf15_same = tf15.get("bias") == side
    trend_stack = bool(tf3_same and tf15_same and (structure_same or ict.get("bias") == side))

    # Compensation rules: early ICT entries should not be killed, while late
    # non-ICT continuation entries still receive a real quality discount.
    if ict_strong and (structure_same or tf3_same):
        max_penalty = 7
    elif trend_stack:
        max_penalty = 10
    elif ict.get("bias") == side:
        max_penalty = 12
    else:
        max_penalty = 15

    # Bonus is deliberately tiny. ELS should not inflate weak signals.
    max_bonus = 5 if ict_strong else 3
    adjustment = max(-max_penalty, min(max_bonus, raw_adjustment))

    state = "NEUTRAL"
    if adjustment >= 3:
        state = "LOCATION_BONUS"
    elif adjustment <= -10:
        state = "LATE_OR_WEAK_LOCATION"
    elif adjustment < 0:
        state = "SOFT_LOCATION_PENALTY"

    result = {
        "adjustment": int(adjustment),
        "raw_adjustment": int(raw_adjustment),
        "late_penalty_used": late_penalty,
        "els_adjustment_used": els_adj,
        "max_penalty": int(max_penalty),
        "state": state,
        "els_score": int(entry_location.get("score", 62) or 62),
    }
    context["entry_quality_adjustment"] = result
    return result


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

    ict_setup = str(ict.get("setup", "") or "").upper()
    ict_strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    if ict.get("bias") == side and ict.get("entry_ok") and ict_setup in ict_strong_setups.get(side, []):
        confirmations.append("ICT сетап готовий")
    elif ict.get("bias") == side and ict_setup in ict_weak_setups.get(side, []):
        confirmations.append("ICT частково підтримує")
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
    profile = trade_mode_profile(context, side)
    regime = profile.get("regime", "NORMAL")

    min_risk = max(atr15 * 1.05, price * 0.0065)
    max_risk = price * (safe_float(profile.get("max_stop_pct"), MAX_STOP_DISTANCE_PCT) / 100)
    buffer = max(atr15 * 0.18, price * 0.0012)
    profile_tp1_pct = safe_float(profile.get("tp1_pct"), MIN_TP1_DISTANCE_PCT)
    # In TREND profile TP1 is deliberately ATR-based, not percent/far-swing based.
    # RR guard below will still guarantee TP1 >= stop risk (at least 1:1).
    min_tp1_distance = 0.0 if regime == "TREND" and profile_tp1_pct <= 0 else price * (profile_tp1_pct / 100)
    min_tp2_distance = price * (safe_float(profile.get("tp2_pct"), MIN_TP2_DISTANCE_PCT) / 100)
    min_tp3_distance = price * (safe_float(profile.get("tp3_pct"), MIN_TP3_DISTANCE_PCT) / 100)

    if side == "LONG":
        raw_stop = min(swing_low - buffer, price - min_risk)
        risk = min(max(price - raw_stop, min_risk), max_risk)
        stop = price - risk
        if regime == "RANGE":
            # In balance, nearest realistic objective is EQ/range edge, not a far 2% TP.
            eq = safe_float((context.get("ict") or {}).get("equilibrium"))
            rh = safe_float((context.get("ict") or {}).get("range_high"))
            range_target = min([x for x in [eq, rh, swing_high] if x and x > price], default=price + min_tp1_distance)
            technical_tp1 = max(price + min_tp1_distance, min(range_target, price + atr15 * 1.45))
            technical_tp2 = max(technical_tp1 + atr15 * 0.75, price + min_tp2_distance)
        else:
            # TP1 is the first partial/protection objective, not the final liquidity target.
            # In a strong trend use ~1.35 ATR: not too close, but reachable before normal pullback.
            tp1_atr_mult = 1.35 if regime == "TREND" else (1.35 if regime in ["PULLBACK", "REVERSAL"] else 1.25)
            tp2_atr_mult = 3.00 if regime == "TREND" else (2.25 if regime in ["PULLBACK", "REVERSAL"] else 2.00)
            technical_tp1 = price + atr15 * tp1_atr_mult
            technical_tp2 = max(structure.get("recent_high") or swing_high or technical_tp1, price + atr15 * tp2_atr_mult)
        tp1 = max(technical_tp1, price + min_tp1_distance)
        tp2 = max(technical_tp2, price + min_tp2_distance, tp1 + atr15 * 0.55)
        tp3 = max(price + min_tp3_distance, tp2 + atr15 * (1.35 if regime == "TREND" else 0.95))
        invalidation = (
            f"15m закриття нижче {round_price(stop)} або злам 3m/структури проти LONG. "
            f"Режим: {regime_label(regime)}. TP/стоп динамічні; TP1 у тренді ≈1.35 ATR, у боковику ближче."
        )
    else:
        raw_stop = max(swing_high + buffer, price + min_risk)
        risk = min(max(raw_stop - price, min_risk), max_risk)
        stop = price + risk
        if regime == "RANGE":
            eq = safe_float((context.get("ict") or {}).get("equilibrium"))
            rl = safe_float((context.get("ict") or {}).get("range_low"))
            range_target = max([x for x in [eq, rl, swing_low] if x and x < price], default=price - min_tp1_distance)
            technical_tp1 = min(price - min_tp1_distance, max(range_target, price - atr15 * 1.45))
            technical_tp2 = min(technical_tp1 - atr15 * 0.75, price - min_tp2_distance)
        else:
            # TP1 is the first partial/protection objective, not the final liquidity target.
            # In a strong trend use ~1.35 ATR: not too close, but reachable before normal pullback.
            tp1_atr_mult = 1.35 if regime == "TREND" else (1.35 if regime in ["PULLBACK", "REVERSAL"] else 1.25)
            tp2_atr_mult = 3.00 if regime == "TREND" else (2.25 if regime in ["PULLBACK", "REVERSAL"] else 2.00)
            technical_tp1 = price - atr15 * tp1_atr_mult
            technical_tp2 = min(structure.get("recent_low") or swing_low or technical_tp1, price - atr15 * tp2_atr_mult)
        tp1 = min(technical_tp1, price - min_tp1_distance)
        tp2 = min(technical_tp2, price - min_tp2_distance, tp1 - atr15 * 0.55)
        tp3 = min(price - min_tp3_distance, tp2 - atr15 * (1.35 if regime == "TREND" else 0.95))
        invalidation = (
            f"15m закриття вище {round_price(stop)} або злам 3m/структури проти SHORT. "
            f"Режим: {regime_label(regime)}. TP/стоп динамічні; TP1 у тренді ≈1.35 ATR, у боковику ближче."
        )

    # Smart Money RR guard: TP1 must be at least equal to the stop risk.
    # This preserves the ICT stop idea but refuses a mathematically bad ladder.
    stop, tp1, tp2, tp3 = enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15)

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
        tf3 = context.get("tf3") or {}
        tf15 = context.get("tf15") or {}
        structure = context.get("structure") or {}
        ict = context.get("ict") or {}
        cvd = context.get("cvd") or {}
        flow = context.get("flow") or {}
        tf1h = context.get("tf1h") or {}
        tf4h = context.get("tf4h") or {}
        ict_balance, ict_balance_reason = detect_ict_balance_market(context)
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
        readiness_quality = int(max(25, min(55, readiness)))

        if ict_balance:
            return {
                "action": "NO_TRADE",
                "side": "NEUTRAL",
                "quality": min(readiness_quality, 45),
                "title": "ВХОДУ НЕМАЄ",
                "reason": ict_balance_reason,
                "plan": None,
                "confirmations": [],
                "conflicts": [ict_balance_reason, "Не плутати з відкатом: для входу потрібен вихід з EQ, sweep або BOS + retest"],
                "show_wait_plan": False,
            }

        reversal_after_failed = context.get("reversal_after_failed_trade") or {}
        if reversal_after_failed.get("active"):
            rev_side = reversal_after_failed.get("side")
            rev_plan = make_plan(rev_side, context) if rev_side in ["LONG", "SHORT"] else None
            return {
                "action": "WATCH",
                "side": rev_side,
                "quality": int(max(readiness_quality, min(66, reversal_after_failed.get("score", 55)))),
                "title": f"ЧЕКАТИ — {rev_side} РОЗВОРОТ ПІСЛЯ НЕВДАЛОГО ВИХОДУ",
                "reason": reversal_after_failed.get("reason") or "після слабкого EXIT шукаємо протилежний розворот",
                "plan": rev_plan,
                "confirmations": reversal_after_failed.get("confirmations") or [],
                "conflicts": reversal_after_failed.get("conflicts") or [],
                "reversal_after_failed_trade": True,
                "show_wait_plan": True,
            }

        # Preparation mode even when the global bias is still NEUTRAL.
        # Example: 4H/1H are against or neutral, total_score is not enough for a market bias,
        # but 3M and ICT already point the same way. In this case the user should not see only
        # "ВХОДУ НЕМАЄ". The bot must show what direction is preparing and from which price
        # the setup becomes active.
        prep_side = None
        prep_confirmations = []
        prep_conflicts = []
        for candidate in ["LONG", "SHORT"]:
            tf3_same = tf3.get("bias") == candidate and abs(int(tf3.get("score", 0) or 0)) >= 18
            ict_same = ict.get("bias") == candidate and abs(int(ict.get("score", 0) or 0)) >= 12
            structure_same = structure.get("bias") == candidate and abs(int(structure.get("score", 0) or 0)) >= 18
            tf15_same = tf15.get("bias") == candidate and abs(int(tf15.get("score", 0) or 0)) >= 18
            flow_same = flow.get("bias") == candidate and abs(int(flow.get("score", 0) or 0)) >= 8
            cvd_same = cvd.get("bias") == candidate and abs(int(cvd.get("score", 0) or 0)) >= 8

            tf3_against_strong = tf3.get("bias") == opposite(candidate) and abs(int(tf3.get("score", 0) or 0)) >= 32
            ict_against_entry = ict.get("bias") == opposite(candidate) and ict.get("state") == "ENTRY_MODEL"

            prep_score = 0
            if tf3_same:
                prep_score += 3
            if ict_same:
                prep_score += 3
            if structure_same:
                prep_score += 2
            if tf15_same:
                prep_score += 2
            if flow_same or cvd_same:
                prep_score += 1

            if prep_score >= 5 and not tf3_against_strong and not ict_against_entry:
                prep_side = candidate
                if tf3_same:
                    prep_confirmations.append("3M вже показує напрям, але ще потрібен тригер ціни")
                if ict_same:
                    prep_confirmations.append("ICT підтримує ідею")
                if structure_same:
                    prep_confirmations.append("структура підтримує напрям")
                if tf15.get("bias") == "NEUTRAL":
                    prep_conflicts.append("15M ще нейтральний — вхід тільки після 3M reclaim/ретесту")
                if tf1h.get("bias") == opposite(candidate) or int(tf1h.get("score", 0) or 0) * (1 if candidate == "LONG" else -1) < -15:
                    prep_conflicts.append("1H слабкий/проти — не заходити без чіткого тригера")
                if tf4h.get("bias") == opposite(candidate) or int(tf4h.get("score", 0) or 0) * (1 if candidate == "LONG" else -1) < -35:
                    prep_conflicts.append("4H проти — це підготовка, не агресивний вхід")
                break

        if prep_side:
            plan = make_plan(prep_side, context)
            return {
                "action": "WATCH",
                "side": prep_side,
                "quality": int(max(readiness_quality, min(66, readiness_quality + 8))),
                "title": f"ЧЕКАТИ — {prep_side} ГОТУЄТЬСЯ",
                "reason": "напрям формується, але потрібна ціна-тригер: reclaim/ретест на 3M",
                "plan": plan,
                "confirmations": prep_confirmations,
                "conflicts": prep_conflicts,
            }

        long_exhausted, long_exhausted_reason = detect_exhausted_move("LONG", context)
        short_exhausted, short_exhausted_reason = detect_exhausted_move("SHORT", context)

        no_trade_conflicts = []
        if long_exhausted:
            no_trade_conflicts.append("LONG вже відіграний після сильного імпульсу")
            no_trade_conflicts.append("SHORT ще не підтверджений структурою / ICT")
            reason = long_exhausted_reason or "LONG вже відіграний, а SHORT ще не готовий"
        elif short_exhausted:
            no_trade_conflicts.append("SHORT вже відіграний після сильного імпульсу")
            no_trade_conflicts.append("LONG ще не підтверджений структурою / ICT")
            reason = short_exhausted_reason or "SHORT вже відіграний, а LONG ще не готовий"
        elif ict.get("setup") in ["BALANCE_MIDRANGE", "CONTEXT_ONLY", "DISCOUNT_CONTEXT", "PREMIUM_CONTEXT"]:
            reason = "ринок у балансі / ICT-сетап не готовий"
        else:
            reason = "перевага нечітка, немає професійного входу"
        return {
            "action": "NO_TRADE",
            "side": "NEUTRAL",
            "quality": readiness_quality,
            "title": "ВХОДУ НЕМАЄ",
            "reason": reason,
            "plan": None,
            "confirmations": [],
            "conflicts": no_trade_conflicts,
        }

    confirmations, conflicts = entry_confirmations(side, context)
    exhausted, exhausted_reason = detect_exhausted_move(side, context)
    late, late_reason = is_late_chase(side, context)
    late_penalty = soft_late_entry_penalty(side, context, late_reason) if late else 0
    if late and late_reason:
        conflicts.append(f"мʼякий anti-chase штраф -{late_penalty}: {late_reason}")

    # Internal Entry Location Score: improves the bot brain without adding
    # a new noisy Telegram line. It does not block entries; it only makes
    # quality/risk more honest when the direction is right but the location is late.
    entry_location = analyze_entry_location_score(side, context)
    context["entry_location"] = entry_location

    plan = make_plan(side, context)
    rr_status = smart_money_rr_status(plan)
    mode_profile = trade_mode_profile(context, side)
    mode_profile["atr15"] = context.get("atr15") or safe_float((context.get("tf15") or {}).get("atr"), None)
    market_regime = mode_profile.get("regime", "NORMAL")
    reentry_cooldown = context.get("reentry_cooldown") or {}
    reversal_after_failed = context.get("reversal_after_failed_trade") or {}
    reversal_active_for_side = bool(reversal_after_failed.get("active") and reversal_after_failed.get("side") == side)
    reversal_entry_allowed = bool(reversal_active_for_side and reversal_after_failed.get("allow_entry"))

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
    liquidity = context.get("liquidity") or {}
    calendar = context.get("calendar") or {}
    news = context.get("news") or {}
    ict_balance, ict_balance_reason = detect_ict_balance_market(context, side)

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

    # Intraday quality model WITHOUT BTC↔BZ:
    # ICT + SMC/structure define the idea.
    # 3M is the main timing trigger for early entries.
    # 15M/1H confirm strength. CVD/Flow/Clusters/OI adjust risk, but do not fully block ICT+3M.
    score_for_side = side_score(context["total_score"], side)
    quality = 46 + min(9, max(0, score_for_side // 10))
    quality += block_points(tf15, 10, 12)
    quality += block_points(structure, 12, 14)
    if ict.get("state") == "ENTRY_MODEL":
        quality += block_points(ict, 16, 16)
    elif ict.get("bias") == side and ict.get("score", 0):
        quality += block_points(ict, 6, 6)
    if ict.get("entry_ok") and ict.get("bias") == side:
        quality += 8
    if ict.get("no_chase") and ict.get("bias") == side:
        quality -= 18
    # 3M has strong weight: it is the timing trigger.
    quality += block_points(tf3, 16, 16, neutral=0, weak_opposite_penalty=6)
    quality += block_points(tf1h, 4, 6)
    quality += block_points(tf4h, 1, 2)
    quality += block_points(cvd, 8, 9)
    quality += block_points(derivatives, 6, 7)
    quality += block_points(liquidity, 7, 10)
    quality += block_points(flow, 6, 6)
    quality += block_points(clusters, 2, 2)

    if reversal_active_for_side:
        quality += 8 if reversal_entry_allowed else 4
        confirmations.append("REVERSAL: попередня угода зламалась, шукаємо протилежний рух")
        for item in reversal_after_failed.get("confirmations") or []:
            if item not in confirmations:
                confirmations.append(item)
        for item in reversal_after_failed.get("conflicts") or []:
            if item not in conflicts:
                conflicts.append(item)

    if news.get("bias") == side:
        quality += 2
    elif news.get("bias") == opposite(side) and abs(news.get("score", 0)) >= 35:
        quality -= 7

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    tf3_same = tf3.get("bias") == side
    tf3_neutral = tf3.get("bias") == "NEUTRAL"
    tf3_weak_pullback = tf3.get("bias") == opposite(side) and tf3_score_abs < 32
    tf3_strong_against = tf3.get("bias") == opposite(side) and tf3_score_abs >= 42

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

    # ICT priority layer. ICT is not a hard blocker for every intraday trend
    # continuation, because BZU can move without a perfect FVG/OB retest.
    # But a normal 90+ quality ENTRY must have a real ICT model or a very strong
    # trend stack. Without ICT, the bot may still enter, but quality is capped
    # and the signal is treated as trend/risky rather than ideal.
    ict_score_abs = abs(int(ict.get("score", 0) or 0))
    ict_entry_model = ict_same and ict.get("state") == "ENTRY_MODEL" and bool(ict.get("entry_ok"))
    ict_context_support = ict_same and ict_score_abs >= 16
    ict_context_against = ict.get("bias") == opposite(side) and ict_score_abs >= 16

    cvd_reliable = cvd.get("confidence", "HIGH") != "LOW"
    cvd_same = cvd_reliable and cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 10
    flow_same = flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10
    oi_same = derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10

    strong_cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 22
    strong_oi_against = derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14

    # Micro-pressure risk layer.
    # ICT/SMC + 3M is allowed to open early, but if CVD/flow/clusters are against,
    # the bot must label the entry as risky instead of pretending it is a 90+ setup.
    cvd_score_side = side_score(int(cvd.get("score", 0) or 0), side)
    flow_score_side = side_score(int(flow.get("score", 0) or 0), side)
    clusters_score_side = side_score(int(clusters.get("score", 0) or 0), side)
    cvd_pressure_against = cvd_reliable and (cvd_score_side <= -10 or str(cvd.get("state", "")).upper() in [
        "SELLERS_DOMINATE" if side == "LONG" else "BUYERS_DOMINATE"
    ])
    flow_pressure_against = flow_score_side <= -10 or flow.get("bias") == opposite(side)
    clusters_pressure_against = clusters_score_side <= -4 or clusters.get("bias") == opposite(side)
    pressure_risk_count = sum([
        bool(cvd_pressure_against),
        bool(flow_pressure_against),
        bool(clusters_pressure_against),
    ])
    pressure_risk = pressure_risk_count >= 1
    heavy_pressure_risk = pressure_risk_count >= 2

    if cvd_pressure_against and "CVD проти" not in conflicts:
        conflicts.append("CVD/дельта поки проти — ранній вхід тільки як ризиковий")
    if flow_pressure_against and "потік локально проти" not in conflicts:
        conflicts.append("потік локально проти — потрібен швидкий розворот після входу")
    if clusters_pressure_against:
        if side == "LONG" and "локальний продавець у стакані" not in conflicts:
            conflicts.append("локальний продавець у стакані")
        elif side == "SHORT" and "локальний покупець у стакані" not in conflicts:
            conflicts.append("локальний покупець у стакані")

    if market_regime in ["RANGE", "NEWS_IMPULSE", "REVERSAL"]:
        confirmations.append(f"режим: {regime_label(market_regime)} — TP/стоп адаптовані")

    if rr_status.get("ok"):
        confirmations.append(rr_status.get("label"))
        if rr_status.get("rr1", 0) >= STRONG_RR1_BONUS:
            quality += 4
        elif rr_status.get("rr1", 0) >= GOOD_RR1_BONUS:
            quality += 2
    else:
        conflicts.append(rr_status.get("label") or "RR не відповідає Smart Money мінімуму")

    cooldown_active = bool(reentry_cooldown.get("active") and reentry_cooldown.get("side") == side)
    cooldown_can_override = cooldown_override_ok(side, context)
    cooldown_penalty = int(reentry_cooldown.get("quality_penalty") or 0) if cooldown_active else 0
    cooldown_loop_guard = bool(
        cooldown_active
        and int(reentry_cooldown.get("same_side_entries_3h") or 0) >= int(reentry_cooldown.get("max_same_side_entries_3h") or 2)
        and not cooldown_can_override
    )
    if cooldown_active and cooldown_can_override:
        msg = cooldown_override_reason(side, context)
        if msg not in confirmations:
            confirmations.append(msg)
    elif cooldown_active:
        conflicts.append((reentry_cooldown.get("reason") or "після слабкого EXIT якість нового входу тимчасово знижена") + f"; штраф якості -{cooldown_penalty}")
        if cooldown_loop_guard:
            conflicts.append("ліміт повторних входів у цей напрям за 3 години — потрібен новий BOS/ICT для розблокування")

    if context.get("low_liquidity_risk"):
        conflicts.append("низька ліквідність/тиха сесія — ризик спреду і slippage")
    if context.get("price_warning"):
        conflicts.append("ціна TV/OKX розходиться — вхід тільки обережно")
    if cvd.get("confidence") == "LOW":
        confirmations.append("CVD має низьку довіру — не є головним фільтром")

    # Higher timeframe counter-trend gate.
    # 4H is context/filter only. It must not forbid every intraday reversal,
    # but ICT alone is not enough for a market ENTRY.
    # A real entry needs 3M confirmation; 15M only upgrades strength.
    tf4h_score_side = side_score(int(tf4h.get("score", 0) or 0), side)
    tf1h_score_side = side_score(int(tf1h.get("score", 0) or 0), side)
    tf4h_strong_against = tf4h_score_side <= -35
    tf1h_against_or_heavy = tf1h_score_side <= -15 or tf1h.get("bias") == opposite(side)
    htf_countertrend = tf4h_strong_against and tf1h_against_or_heavy
    intraday_confirmation_present = tf3_same or tf15_same or structure_same
    real_pressure_present = cvd_same or flow_same or oi_same
    countertrend_wait_required = bool(
        htf_countertrend
        and not tf3_same
        and not tf15_same
        and not structure_same
        and not real_pressure_present
    )
    if countertrend_wait_required:
        conflicts.append("4H/1H проти — ICT сам не дає вхід, потрібне 3M підтвердження")

    core_direction_ok = (
        (tf15_same and not structure_against)
        or (structure_same and not tf15_against)
        or (ict_same and not tf15_against and not structure_against)
        or (tf15_neutral and (structure_same or ict_same))
    )

    pressure_ok = cvd_same or flow_same or oi_same or (ict_entry_ok and tf3_same)

    # Professional structure gate.
    # ICT remains the entry model, but ICT + 3M alone must not open a trade
    # from the middle of balance. A real ENTRY needs at least one structural
    # confirmation so the bot does not buy/sell every small 3M twitch:
    #   - 15M already agrees, OR
    #   - SMC structure agrees (BOS/CHOCH/sweep reclaim), OR
    #   - ICT itself is a liquidity sweep/reclaim model, OR
    #   - price holds a BOS continuation level.
    # If this is missing, the bot keeps WATCH/activation, so the user is not
    # late: it shows the idea, but waits for reclaim/rejection instead of ENTRY.
    structure_phase = str(structure.get("phase", "") or "").upper()
    ict_setup = str(ict.get("setup", "") or "").upper()
    side_structure_phases = {
        "LONG": ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"],
        "SHORT": ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"],
    }
    structure_reclaim_same = structure_phase in side_structure_phases.get(side, [])
    # ICT quality hierarchy.
    # STRONG ICT = real Smart Money entry model: sweep/reclaim, FVG/OB retrace,
    # or clear discount/premium FVG/OB reaction. Only this can justify 90+ quality.
    # WEAK ICT = BOS continuation/hold or directional ICT context without a clean
    # FVG/OB/sweep entry. It supports a trend trade, but must not be called
    # "ICT сетап готовий" and must not inflate quality to 90+.
    ict_strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    ict_strong_model = bool(ict_same and ict.get("entry_ok") and ict_setup in ict_strong_setups.get(side, []))
    ict_weak_model = bool(ict_same and ict_setup in ict_weak_setups.get(side, []))
    ict_full_model = bool(ict_strong_model)
    ict_reclaim_same = bool(ict_strong_model or ict_weak_model)
    trend_stack_same = bool(tf3_same and tf15_same and (tf1h.get("bias") == side or tf4h.get("bias") == side or structure_same))
    strong_trend_stack = bool(tf3_same and tf15_same and structure_same and (tf1h.get("bias") == side or tf4h.get("bias") == side))

    if ict_strong_model:
        quality += 8
        if "ICT сетап готовий" not in confirmations:
            confirmations.append("ICT сетап готовий")
    elif ict_weak_model:
        quality += 3
        confirmations.append("ICT частково підтримує напрям, але це не повний FVG/OB/sweep сетап")
    elif ict_context_support:
        quality += 1
        confirmations.append("ICT лише контекстно підтримує напрям")
    elif ict_context_against:
        quality -= 12
        conflicts.append("ICT проти напрямку — якість входу нижча")
    else:
        conflicts.append("немає повного ICT-сетапу — якість входу обмежена")

    structural_entry_ok = bool(tf15_same or structure_same or structure_reclaim_same or ict_reclaim_same or reversal_entry_allowed)
    structure_gate_missing = bool(tf3_same and ict_entry_ok and not structural_entry_ok)
    if structure_gate_missing:
        conflicts.append("ICT+3M є, але немає 15M/BOS/reclaim — чекати структурне підтвердження")

    # Classic/early entry: 3M must confirm the direction, but not alone.
    # 15M may still be NEUTRAL only if BOS/reclaim/ICT sweep has confirmed
    # structure. This avoids late entries while filtering mid-range false starts.
    trigger_entry_ok = (
        tf3_same
        and core_direction_ok
        and structural_entry_ok
        # CVD/flow against must NOT forbid an ICT+3M early entry.
        # It changes the signal to RISKY_ENTRY and caps quality, so we do not enter late.
        and not strong_oi_against
        and not ict_against
        and not countertrend_wait_required
        and side not in liquidity.get("blocks", [])
    )

    # Pullback / pre-trigger state:
    # 3M RANGE or weak 3M counter-move is useful for WATCH, but it must not open
    # an immediate trade. The bot waits for 3M to turn with the setup.
    pullback_watch_ok = (
        core_direction_ok
        and pressure_ok
        and (tf3_neutral or tf3_weak_pullback)
        and not tf3_strong_against
        and not strong_cvd_against
        and not strong_oi_against
        and not ict_against
        and side not in liquidity.get("blocks", [])
    )

    trigger_ok = trigger_entry_ok

    # 4H is not included here. It is only context.
    trend_ok = core_direction_ok

    hard_conflict = (
        not rr_status.get("ok")
        or side in liquidity.get("blocks", [])
        or ict_against
        or (tf15_against and structure_against)
        or (strong_cvd_against and strong_oi_against)
        or (tf3_strong_against and strong_cvd_against)
        or cooldown_loop_guard
    )

    # EARLY ICT ENTRY GATE.
    # Purpose: do not wait for the full confirmation stack after ICT + SMC
    # already agree. This keeps the user's preferred early entries.
    # It does NOT create a normal high-confidence ENTRY; it only allows
    # RISKY_ENTRY when:
    #   - ICT is aligned with the trade side,
    #   - market structure is aligned with the trade side,
    #   - the setup quality remains above the risky threshold,
    #   - anti-chase did not mark the move as heavily overextended,
    #   - there is no hard conflict/liquidity block.
    # 3M may be neutral/weak during the retest; it must simply not be strongly
    # against together with strong CVD pressure.
    early_ict_entry_ok = bool(
        ict_same
        and structure_same
        and not ict_against
        and not hard_conflict
        and late_penalty < 15
        and not exhausted
        and not ict_balance
        and side not in liquidity.get("blocks", [])
        and not (tf3_strong_against and strong_cvd_against)
        and not countertrend_wait_required
    )

    if not tf3_same:
        # Without 3M confirmation this is preparation, not an entry.
        quality = min(quality, 66)
    elif structure_gate_missing:
        # ICT + 3M without 15M/BOS/reclaim can be a good idea, but not a market entry.
        quality = min(quality, 64)
    elif htf_countertrend and not (tf15_same or structure_same or real_pressure_present):
        # 3M may catch the turn early, but against 4H/1H keep quality realistic.
        quality = min(quality, 78)
    elif htf_countertrend and not tf15_same:
        quality = min(quality, 84)

    # Counter-trend quality honesty. A trade against both 1H and 4H can still be
    # taken only as a risky bounce/reversal attempt, but it must not look like
    # a premium 85-90+ setup in Telegram. This prevents cases like a
    # counter-trend LONG showing 88/100 after a broad downtrend.
    both_htf_against = bool(tf1h.get("bias") == opposite(side) and tf4h.get("bias") == opposite(side))
    one_htf_against = bool(tf1h.get("bias") == opposite(side) or tf4h.get("bias") == opposite(side))
    countertrend_entry = bool(one_htf_against or htf_countertrend)
    if both_htf_against and not (ict_strong_model and structure_same and tf15_same):
        quality = min(quality, 76)
    elif one_htf_against and not (ict_strong_model and (structure_same or tf15_same)):
        quality = min(quality, 82)

    if heavy_pressure_risk:
        # Keep the early entry available, but never call it an ideal/strong trade
        # while CVD/flow/clusters show absorption or seller/buyer pressure against it.
        quality = min(quality, 76)
    elif pressure_risk:
        quality = min(quality, 80)

    # ICT priority caps: ICT is the quality anchor, but not a hard blocker.
    # Goal: keep good trend entries, but stop printing 90+ when there is no
    # real ICT setup. Only a STRONG ICT model may score above 88-90.
    if ict_context_against:
        quality = min(quality, 72)
    elif ict_weak_model:
        if strong_trend_stack:
            quality = min(quality, 86)
        elif trend_stack_same:
            quality = min(quality, 83)
        else:
            quality = min(quality, 80)
    elif not ict_strong_model:
        if strong_trend_stack:
            quality = min(quality, 82)
        elif trend_stack_same:
            quality = min(quality, 80)
        else:
            quality = min(quality, 76)

    if context.get("low_liquidity_risk"):
        quality = min(quality, 74)
    if context.get("price_warning"):
        quality = min(quality, 72)

    # Unified entry-location correction. This replaces separate Anti-chase + ELS
    # deductions so the bot does not punish the same late/chase problem twice.
    eq_adjust = entry_quality_adjustment(side, context, late_penalty)
    quality += int(eq_adjust.get("adjustment", 0) or 0)

    # Soft same-direction cooldown after a weak exit.
    # It no longer blocks the trade; it only lowers quality unless a fresh
    # BOS/reclaim/ICT continuation overrides it. This prevents half-day silence
    # while still reducing revenge entries after a bad close.
    if cooldown_active and not cooldown_can_override and cooldown_penalty:
        quality -= cooldown_penalty
        previous_quality = int(reentry_cooldown.get("last_entry_quality") or 0)
        # If the new setup is clearly better than the failed one, do not let the
        # old loss suppress a materially stronger signal.
        if previous_quality and quality >= previous_quality + 10:
            quality += cooldown_penalty
            cooldown_can_override = True
            confirmations.append("повторний вхід дозволено: новий сетап значно якісніший за попередній")

    if calendar.get("active"):
        quality -= 8
        if quality < 75:
            hard_conflict = True
    if hard_conflict:
        quality = min(quality, 56)
    if not trend_ok:
        quality = min(quality, 59)
    elif not trigger_ok:
        # If 3M is RANGE/pullback, the setup can be good, but entry is not active yet.
        quality = min(quality, 66)

    # Late or weak-location entries may still be traded, but they must not look
    # like strong clean setups. This preserves early entries without chasing.
    eq_state = (context.get("entry_quality_adjustment") or {}).get("state")
    if eq_state == "LATE_OR_WEAK_LOCATION" and not ict_strong_model:
        quality = min(quality, 74)
    elif eq_state == "SOFT_LOCATION_PENALTY" and not ict_strong_model:
        quality = min(quality, 82)

    quality = int(max(0, min(92, quality)))

    if not rr_status.get("ok"):
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} RR ЗАСЛАБКИЙ",
            "reason": rr_status.get("label") or "тейк менший за стоп — Smart Money угода неякісна",
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "show_wait_plan": True,
        }

    if cooldown_loop_guard:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 58),
            "title": f"ЧЕКАТИ — {side} НЕ ПОВТОРЮВАТИ СЕРІЮ",
            "reason": "вже були повторні входи в цей напрям після слабкого виходу; потрібен новий BOS/reclaim або повний ICT continuation",
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "reentry_cooldown": True,
            "show_wait_plan": True,
        }

    if structure_gate_missing:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 64),
            "title": f"ЧЕКАТИ — {side} ПОТРІБЕН RECLAIM/BOS",
            "reason": "ICT і 3M вже показують ідею, але структури ще нема: чекати 15M підтвердження, BOS або reclaim/rejection",
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "structure_gate": True,
            "show_wait_plan": True,
        }

    if ict_balance:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} ТІЛЬКИ ПІСЛЯ ВИХОДУ З БАЛАНСУ",
            "reason": ict_balance_reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": [ict_balance_reason],
            "ict_balance": True,
            "show_wait_plan": True,
        }

    if exhausted:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} ВЖЕ ВІДІГРАНИЙ",
            "reason": exhausted_reason,
            "plan": plan,
            "confirmations": [],
            "conflicts": [exhausted_reason],
            "exhausted_move": True,
            "show_wait_plan": False,
        }

    # HARD NO-CHASE GUARD.
    # If the move is already extended enough to receive a heavy anti-chase
    # penalty, the bot must not still print ENTRY/RISKY_ENTRY in the same
    # direction. Direction can be correct, but location is bad. The proper
    # state is WATCH until a pullback/protorgovka/new FVG-OB retest appears.
    # This prevents signals like: "anti-chase -18, не доганяти" +
    # "РИЗИКОВАНИЙ ВХІД SHORT" in the same message.
    heavy_chase = bool(late_penalty >= 15)
    chase_reason = late_reason or "рух уже розтягнутий — не доганяти; чекати відкат/проторговку або новий ICT/FVG/OB retest"
    if heavy_chase:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 61),
            "title": f"ЧЕКАТИ — {side} НЕ ДОГАНЯТИ",
            "reason": chase_reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": list(dict.fromkeys(conflicts + [chase_reason])),
            "hard_no_chase": True,
            "show_wait_plan": True,
        }

    if quality >= RISKY_QUALITY_MIN and early_ict_entry_ok:
        # Early ICT/SMC is allowed to preserve early entries, but it must not
        # look like a clean 90+ confirmed trade. If it is counter-trend, without
        # 3M confirmation, or without 15M agreement, keep the score in the risky
        # band so Telegram does not overstate the setup quality.
        if countertrend_entry:
            quality = min(quality, 76)
        elif not tf3_same:
            quality = min(quality, 72)
        elif not tf15_same:
            quality = min(quality, 79)
        else:
            quality = min(quality, 82)
        reason = "ранній ICT/SMC вхід: ICT і структура вже за напрям, не чекаємо повного набору підтверджень"
        if late_penalty:
            reason += f"; anti-chase штраф уже врахований (-{late_penalty})"
        if not tf3_same:
            reason += "; 3M ще не ідеальний, тому тільки ризиковий режим"
        if "ранній ICT/SMC: ICT + структура за напрям" not in confirmations:
            confirmations.append("ранній ICT/SMC: ICT + структура за напрям")
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "countertrend_entry": countertrend_entry,
            "early_ict_entry": True,
        }


    if quality >= ENTRY_QUALITY_MIN and trigger_ok and not hard_conflict and not pressure_risk:
        if reversal_entry_allowed:
            if ict_strong_model:
                reason = "REVERSAL ENTRY: попередній напрям зламано, 3M + структура + повний ICT підтвердили розворот"
            else:
                reason = "REVERSAL ENTRY: попередній напрям зламано, але ICT не повний — якість обмежена"
        elif not ict_strong_model:
            reason = "трендовий вхід без повного ICT-сетапу: напрям/структура/3M підтверджують, якість обмежена"
            if late_penalty:
                reason += f"; anti-chase штраф уже врахований (-{late_penalty})"
        elif not tf15_same:
            reason = "ранній вхід: 3M підтвердив напрям, 15M ще не запізнився/нейтральний"
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
            "countertrend_entry": countertrend_entry,
        }

    if quality >= RISKY_QUALITY_MIN and trigger_ok and not hard_conflict:
        # Any RISKY_ENTRY must remain visually honest: it should not print
        # 85-92/100 like a fully confirmed signal. Normal high scores are
        # reserved for ENTRY, not risky/early attempts.
        quality = min(quality, 79 if not countertrend_entry else 76)
        if reversal_active_for_side and not reversal_entry_allowed:
            return {
                "action": "WATCH",
                "side": side,
                "quality": min(quality, 66),
                "title": f"ЧЕКАТИ — {side} РОЗВОРОТ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ",
                "reason": reversal_after_failed.get("reason") or "після невдалої угоди потрібен BOS/reclaim перед переворотом",
                "plan": plan,
                "confirmations": confirmations,
                "conflicts": conflicts,
                "reversal_after_failed_trade": True,
                "show_wait_plan": True,
            }
        if pressure_risk:
            reason = "ICT/SMC + 3M дають ранній вхід, але CVD/потік/кластери ще не підтвердили — ризиковий режим"
        elif not tf15_same:
            reason = "ранній 3M-тригер; 15M ще не підтвердив повністю, тому ризик вищий"
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
            "countertrend_entry": countertrend_entry,
        }

    if pullback_watch_ok:
        wait_reason = "напрям є, але 3M ще не підтвердив; чекати 3M higher low/reclaim для LONG або lower high/rejection для SHORT"
    elif conflicts:
        wait_reason = "напрям є, але входу ще немає: " + "; ".join(conflicts[:3])
    else:
        wait_reason = "напрям є, але бракує якості входу; орієнтир — Pending ICT зона"

    return {
        "action": "WATCH",
        "side": side,
        "quality": quality,
        "title": f"ЧЕКАТИ — {side} ГОТУЄТЬСЯ",
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


def _parse_iso_ts_ms(value):
    try:
        if not value:
            return 0
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _trade_extremes_since_open(trade, context):
    """High/low actually traded after entry.

    The bot runs every ~15 minutes, while TP can be touched inside a candle and
    disappear before the next run. For TP/MFE accounting we must use candle
    high/low since the trade was opened, not only the latest ticker price.
    We intentionally do NOT use this to trigger newly moved stops, because the
    candle may have traded that level before the stop was moved.
    """
    opened_ms = _parse_iso_ts_ms(getattr(trade, "opened_at", ""))
    highs = []
    lows = []
    for candle in (context.get("candles_3m") or []):
        try:
            if opened_ms and int(candle.ts) + 3 * 60 * 1000 < opened_ms:
                continue
            highs.append(float(candle.high))
            lows.append(float(candle.low))
        except Exception:
            continue
    price = safe_float(context.get("price"))
    if price:
        highs.append(price)
        lows.append(price)
    if not highs or not lows:
        return price, price
    return max(highs), min(lows)




def _trade_extremes_since_stop_update(trade, context):
    """High/low after the current stop became active.

    STOP must be checked by candle high/low, not only by the latest close/price.
    But for a newly moved stop we must not use old candles from before the stop
    was moved. Therefore every stop update stores stop_updated_at, and this
    helper only scans 3M candles after that moment.
    """
    anchor_ms = _parse_iso_ts_ms(getattr(trade, "stop_updated_at", ""))
    if not anchor_ms:
        anchor_ms = _parse_iso_ts_ms(getattr(trade, "opened_at", ""))
    highs = []
    lows = []
    for candle in (context.get("candles_3m") or []):
        try:
            # include candles that overlap the active-stop window
            if anchor_ms and int(candle.ts) + 3 * 60 * 1000 < anchor_ms:
                continue
            highs.append(float(candle.high))
            lows.append(float(candle.low))
        except Exception:
            continue
    price = safe_float(context.get("price"))
    if price:
        highs.append(price)
        lows.append(price)
    if not highs or not lows:
        return price, price
    return max(highs), min(lows)


def trade_hit_stop_by_extreme(side, price, stop, high_since_stop=None, low_since_stop=None):
    if stop is None:
        return False
    if trade_hit_stop(side, price, stop):
        return True
    if side == "LONG":
        return low_since_stop is not None and low_since_stop <= stop
    return high_since_stop is not None and high_since_stop >= stop

def trade_hit_level_by_extreme(side, price, level, high_since_open=None, low_since_open=None):
    if level is None:
        return False
    if trade_hit_level(side, price, level):
        return True
    if side == "LONG":
        return high_since_open is not None and high_since_open >= level
    return low_since_open is not None and low_since_open <= level


def best_trade_price(side, trade, current_price, high_since_open=None, low_since_open=None):
    candidate = current_price
    if side == "LONG" and high_since_open is not None:
        candidate = max(current_price, high_since_open)
    elif side == "SHORT" and low_since_open is not None:
        candidate = min(current_price, low_since_open)
    if not trade.best_price:
        return candidate
    if side == "LONG":
        return max(trade.best_price, candidate)
    return min(trade.best_price, candidate)


def _valid_stop_for_side(side, price, stop):
    if stop is None or price is None:
        return False
    return stop < price if side == "LONG" else stop > price


def _last_smc_trailing_level(side, candles, price, entry, atr_value):
    """Find a practical SMC trailing stop from recent 3M swing structure.

    LONG: trail under the latest meaningful 3M higher low.
    SHORT: trail above the latest meaningful 3M lower high.
    The level must be on the safe side of current price; after TP1 we prefer
    levels that also protect at least breakeven/partial profit.
    """
    if not candles or len(candles) < 20 or not price:
        return None, ""
    highs, lows = swing_points(candles[-110:], 2)
    buffer = max((atr_value or price * 0.004) * 0.12, price * 0.00035)

    if side == "LONG":
        candidates = [safe_float(x.get("price")) for x in lows[-12:]]
        candidates = [x for x in candidates if x and x < price]
        protected = [x for x in candidates if x >= entry * 0.999]
        base = max(protected or candidates) if candidates else None
        if base:
            return round_price(base - buffer), f"SMC: стоп під останній 3M higher low {round_price(base)}"
    else:
        candidates = [safe_float(x.get("price")) for x in highs[-12:]]
        candidates = [x for x in candidates if x and x > price]
        protected = [x for x in candidates if x <= entry * 1.001]
        base = min(protected or candidates) if candidates else None
        if base:
            return round_price(base + buffer), f"SMC: стоп над останній 3M lower high {round_price(base)}"
    return None, ""




def _best_smc_trailing_level(side, context, price, entry, atr_value):
    """Best SMC trailing stop from 3M + 15M swings.

    3M gives the active intraday structure; 15M gives wider structure so the
    stop is not placed inside ordinary 3M noise. After TP1/TP2 this lets the
    bot protect profit while still keeping the stop behind real structure.
    """
    candidates = []
    tf3_level, tf3_reason = _last_smc_trailing_level(side, (context or {}).get("candles_3m") or [], price, entry, atr_value)
    if tf3_level is not None:
        candidates.append((tf3_level, tf3_reason))

    # Wider 15M swing anchor. Use a slightly larger ATR buffer so it does not
    # hug a 15M wick. This is intentionally not used alone if it would worsen
    # protection too much; the final clamp/air rules still apply.
    tf15_level, tf15_reason = _last_smc_trailing_level(side, (context or {}).get("candles_15m") or [], price, entry, (atr_value or price * 0.006) * 1.35)
    if tf15_level is not None:
        candidates.append((tf15_level, tf15_reason.replace("3M", "15M")))

    valid = [(lvl, why) for lvl, why in candidates if _valid_stop_for_side(side, price, lvl)]
    if not valid:
        return None, ""
    if side == "LONG":
        lvl, why = max(valid, key=lambda x: x[0])
    else:
        lvl, why = min(valid, key=lambda x: x[0])
    return round_price(lvl), why


def _tp_hit_note(side, label, level, high_since_open, low_since_open, price):
    """Explain whether TP was reached by live price or candle high/low."""
    level = round_price(level)
    if side == "LONG":
        if high_since_open is not None and high_since_open >= level and (price is None or price < level):
            return f"{label} {level} взято по high свічки 3M/між запусками"
        return f"{label} {level} взято"
    if low_since_open is not None and low_since_open <= level and (price is None or price > level):
        return f"{label} {level} взято по low свічки 3M/між запусками"
    return f"{label} {level} взято"

def _ict_zone_trailing_level(side, ict, price, atr_value):
    """Find an ICT/FVG/OB based protective stop if the zone is usable."""
    if not ict or not price:
        return None, ""
    buffer = max((atr_value or price * 0.004) * 0.10, price * 0.0003)
    zones = []
    if side == "LONG":
        for key, label in [("bull_ob", "bullish OB"), ("bull_fvg", "bullish FVG")]:
            z = ict.get(key)
            if z and not z.get("mitigated"):
                low = safe_float(z.get("low"))
                high = safe_float(z.get("high"))
                if low and high and min(low, high) < price:
                    zones.append((min(low, high) - buffer, label, min(low, high), max(low, high)))
        if zones:
            level, label, low, high = max(zones, key=lambda x: x[0])
            return round_price(level), f"ICT: стоп під {label} {round_price(low)}–{round_price(high)}"
    else:
        for key, label in [("bear_ob", "bearish OB"), ("bear_fvg", "bearish FVG")]:
            z = ict.get(key)
            if z and not z.get("mitigated"):
                low = safe_float(z.get("low"))
                high = safe_float(z.get("high"))
                if low and high and max(low, high) > price:
                    zones.append((max(low, high) + buffer, label, min(low, high), max(low, high)))
        if zones:
            level, label, low, high = min(zones, key=lambda x: x[0])
            return round_price(level), f"ICT: стоп над {label} {round_price(low)}–{round_price(high)}"
    return None, ""



def _stop_air_profile(context, mode_profile=None, stage="PRE_TP1"):
    """Minimum safe air between current price and a moved stop.

    BZU 3M candles can wick 0.15-0.35% without a real reversal.
    A protective stop therefore must stay outside normal 3M noise and should be
    wider before TP1 than after TP1. This function uses both regime and ATR.
    """
    mode_profile = mode_profile or {}
    regime = mode_profile.get("regime", "NORMAL")
    price = safe_float((context or {}).get("price"), 0) or 0
    atr_value = safe_float((context or {}).get("atr15"), 0) or (price * 0.006 if price else 0)
    atr_pct = (atr_value / price * 100.0) if price and atr_value else 0.55

    if stage == "PRE_TP1":
        base = {
            "RANGE": 0.24,
            "REVERSAL": 0.26,
            "PULLBACK": 0.30,
            "TREND": 0.34,
            "NEWS_IMPULSE": 0.42,
            "IMPULSE": 0.34,
            "NORMAL": 0.30,
            "UNKNOWN": 0.30,
        }.get(regime, 0.30)
        atr_mult = 0.46
        cap = 0.58
    elif stage == "POST_TP2":
        base = {
            "RANGE": 0.20,
            "REVERSAL": 0.22,
            "PULLBACK": 0.25,
            "TREND": 0.30,
            "NEWS_IMPULSE": 0.36,
            "IMPULSE": 0.30,
            "NORMAL": 0.26,
            "UNKNOWN": 0.26,
        }.get(regime, 0.26)
        atr_mult = 0.34
        cap = 0.48
    else:  # POST_TP1
        base = {
            "RANGE": 0.22,
            "REVERSAL": 0.24,
            "PULLBACK": 0.28,
            "TREND": 0.32,
            "NEWS_IMPULSE": 0.38,
            "IMPULSE": 0.32,
            "NORMAL": 0.28,
            "UNKNOWN": 0.28,
        }.get(regime, 0.28)
        atr_mult = 0.38
        cap = 0.54

    gap_pct = max(base, atr_pct * atr_mult)
    return round(min(gap_pct, cap), 4)


def _apply_stop_air(side, stop, price, entry, context, mode_profile=None, stage="PRE_TP1"):
    """Push a proposed stop away from current price if it sits inside noise."""
    stop = safe_float(stop)
    price = safe_float(price)
    entry = safe_float(entry)
    if stop is None or price is None or entry is None or not price or not entry:
        return stop, ""
    gap_pct = _stop_air_profile(context, mode_profile, stage)
    gap_abs = price * gap_pct / 100.0
    adjusted = stop
    if side == "LONG":
        max_allowed = price - gap_abs
        if stop > max_allowed:
            adjusted = max_allowed
    else:
        min_allowed = price + gap_abs
        if stop < min_allowed:
            adjusted = min_allowed
    adjusted = round_price(adjusted)
    if adjusted != round_price(stop):
        return adjusted, f"стоп відсунуто від ціни: мінімум ~{gap_pct}% / ATR-buffer, щоб не вибило 1-2 свічками"
    return round_price(stop), f"стоп має запас від ціни ~{gap_pct}%"

def _clamp_stop_between_entry_tp(side, raw_level, entry, tp1, tp2=None, after_tp="TP1"):
    """Clamp protective stop so it is not too tight after TP1.

    After TP1 the stop should usually sit around the middle of entry→TP1,
    while still respecting ICT/SMC context. It must not jump directly under TP1
    on every new 3M higher-low. After TP2 we allow a tighter lock, but still
    avoid placing the stop exactly on the current noise.
    """
    if raw_level is None or not entry or not tp1:
        return raw_level

    if side == "LONG":
        move1 = max(tp1 - entry, entry * 0.002)
        tp1_floor = entry + move1 * 0.50
        tp1_ceiling = entry + move1 * 0.68
        if after_tp == "TP2" and tp2:
            move2 = max(tp2 - entry, move1)
            floor = max(tp1, entry + move2 * 0.50)
            ceiling = entry + move2 * 0.78
            return round_price(min(max(raw_level, floor), ceiling))
        return round_price(min(max(raw_level, tp1_floor), tp1_ceiling))

    move1 = max(entry - tp1, entry * 0.002)
    tp1_floor = entry - move1 * 0.68   # lower number = tighter for SHORT
    tp1_ceiling = entry - move1 * 0.50 # higher number = looser for SHORT
    if after_tp == "TP2" and tp2:
        move2 = max(entry - tp2, move1)
        floor = entry - move2 * 0.78
        ceiling = min(tp1, entry - move2 * 0.50)
        return round_price(max(min(raw_level, ceiling), floor))
    return round_price(max(min(raw_level, tp1_ceiling), tp1_floor))


def protective_stop_ict_smc(trade, context, after_tp="TP1"):
    """Return a staged protective stop recommendation using ICT + SMC.

    Important management rule for BZU:
    - After TP1, calculate ONE protective stop and lock it until TP2.
      It should be around the middle of entry→TP1, adjusted by ICT/SMC/tech.
      Do not trail directly under every new 3M higher-low/lower-high.
    - After TP2, calculate ONE new tighter stop and lock it until TP3/exit.
    """
    price = context.get("price")
    side = trade.side
    atr_value = context.get("atr15") or (price or trade.entry) * 0.006

    if side == "LONG":
        move1 = max(trade.tp1 - trade.entry, trade.entry * 0.002)
        midpoint = trade.entry + move1 * 0.50
        base_level = midpoint
        base_reason = "TP1 взято: стоп у середині руху entry→TP1, щоб не віддати прибуток і не вибило шумом"
    else:
        move1 = max(trade.entry - trade.tp1, trade.entry * 0.002)
        midpoint = trade.entry - move1 * 0.50
        base_level = midpoint
        base_reason = "TP1 взято: стоп у середині руху entry→TP1, щоб не віддати прибуток і не вибило шумом"

    candidates = [(base_level, base_reason)]
    smc_level, smc_reason = _best_smc_trailing_level(side, context, price, trade.entry, atr_value)
    ict_level, ict_reason = _ict_zone_trailing_level(side, context.get("ict") or {}, price, atr_value)
    if smc_level is not None:
        candidates.append((smc_level, smc_reason))
    if ict_level is not None:
        candidates.append((ict_level, ict_reason))

    if after_tp == "TP2":
        if side == "LONG":
            candidates.append((trade.tp1, "TP2 взято: мінімум захистити TP1"))
        else:
            candidates.append((trade.tp1, "TP2 взято: мінімум захистити TP1"))

    valid = [(lvl, why) for lvl, why in candidates if _valid_stop_for_side(side, price, lvl)]
    if not valid:
        raw_level, why = base_level, base_reason
    elif side == "LONG":
        # Use the most protective valid ICT/SMC level, but then cap it so TP1
        # stop remains around the middle of entry→TP1 instead of right under TP1.
        raw_level, why = max(valid, key=lambda x: x[0])
    else:
        raw_level, why = min(valid, key=lambda x: x[0])

    clamped = _clamp_stop_between_entry_tp(side, raw_level, trade.entry, trade.tp1, trade.tp2, after_tp)
    if clamped != round_price(raw_level):
        why = f"{why}; обмежено, щоб стоп не був занадто близько до TP1"

    # Final safety: even after TP1/TP2 do not place the stop inside ordinary
    # 3M wick-noise. This keeps the stop behind SMC/ICT structure with ATR air.
    stage = "POST_TP2" if after_tp == "TP2" else "POST_TP1"
    clamped, air_reason = _apply_stop_air(side, clamped, price, trade.entry, context, trade_mode_profile(context, side), stage)
    if air_reason and "відсунуто" in air_reason:
        why = f"{why}; {air_reason}"
    return round_price(clamped), why


def _profit_lock_stop_level(side, entry, price, best_pct, current_pct, profile=None):
    """Regime-aware MFE protective stop for BZU intraday trades.

    The purpose is not to scalp every small pullback. It is to stop the bot from
    giving back most of a real MFE. Protection is stricter in RANGE/REVERSAL,
    moderate in PULLBACK/NORMAL, and softer in TREND/NEWS_IMPULSE so big moves
    still have room to continue.
    """
    profile = profile or {}
    regime = profile.get("regime", "NORMAL")
    be_trigger = safe_float(profile.get("be_trigger"), 0.65) or 0.65
    protect_trigger = safe_float(profile.get("protect_trigger"), 1.05) or 1.05
    if not entry or not price or best_pct < be_trigger or current_pct < 0.12:
        return None, ""

    lock_pct = 0.08
    label = f"{regime}: MFE +{round(best_pct, 2)}% — перший захист прибутку"

    if regime == "RANGE":
        # In balance the market often gives a short move and returns. Capture more.
        if best_pct >= 1.05 and current_pct >= 0.55:
            lock_pct = 0.72
            label = f"{regime}: MFE у боковику — агресивно захистити прибуток"
        elif best_pct >= 0.75 and current_pct >= 0.35:
            lock_pct = 0.52
            label = f"{regime}: MFE у боковику — не віддавати короткий рух"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.30
            label = f"{regime}: перший захист у боковику"
    elif regime == "REVERSAL":
        # Reversal setups often fail after the first push, so protect sooner.
        if best_pct >= 1.05 and current_pct >= 0.55:
            lock_pct = 0.70
            label = f"{regime}: розворотний MFE — захистити більшу частину"
        elif best_pct >= 0.72 and current_pct >= 0.34:
            lock_pct = 0.50
            label = f"{regime}: розворотний MFE — стоп у плюс"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.32
            label = f"{regime}: перший захист розвороту"
    elif regime == "PULLBACK":
        # Pullback in trend: protect profit, but leave room for continuation.
        if best_pct >= 1.30 and current_pct >= 0.78:
            lock_pct = 0.82
            label = f"{regime}: відкат дав MFE — захистити основний рух"
        elif best_pct >= 0.85 and current_pct >= 0.42:
            lock_pct = 0.55
            label = f"{regime}: MFE на відкаті — стоп у плюс"
        elif best_pct >= be_trigger and current_pct >= 0.20:
            lock_pct = 0.34
            label = f"{regime}: перший захист на відкаті"
    elif regime == "TREND":
        # Trend keeps wider room than RANGE, but now captures at least ~60% of MFE
        # when the move is meaningful. This protects cases like +0.8% MFE -> +0.2% exit.
        if best_pct >= 1.70 and current_pct >= 0.95:
            lock_pct = 1.08
            label = f"{regime}: сильний трендовий MFE — захистити 1%+"
        elif best_pct >= 1.15 and current_pct >= 0.62:
            lock_pct = 0.78
            label = f"{regime}: трендовий MFE — стоп у хороший плюс"
        elif best_pct >= 0.72 and current_pct >= 0.30:
            lock_pct = 0.52
            label = f"{regime}: трендовий MFE — не віддавати рух назад"
        elif best_pct >= be_trigger and current_pct >= 0.16:
            lock_pct = 0.34
            label = f"{regime}: перший трендовий захист"
    elif regime == "NEWS_IMPULSE":
        # News needs the most air. Do not over-tighten on normal news volatility.
        if best_pct >= 1.80 and current_pct >= 1.00:
            lock_pct = 0.95
            label = f"{regime}: новинний MFE — захистити прибуток, але дати простір"
        elif best_pct >= 1.10 and current_pct >= 0.55:
            lock_pct = 0.55
            label = f"{regime}: новинний MFE — мʼякий захист"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.28
            label = f"{regime}: перший новинний захист"
    else:
        # NORMAL / IMPULSE / UNKNOWN: medium protection. Stronger than before,
        # especially for LONGs, but not a hard exit on every pullback.
        if best_pct >= protect_trigger + 0.60 and current_pct >= protect_trigger * 0.75:
            lock_pct = 0.78
            label = f"{regime}: сильний MFE — захистити більшу частину руху"
        elif best_pct >= 0.75 and current_pct >= 0.35:
            lock_pct = 0.50
            label = f"{regime}: MFE — зафіксувати частину прибутку"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.32
            label = f"{regime}: перший захист прибутку"

    # LONG pullbacks on BZU often return faster than shorts. Add a small extra
    # lock only after meaningful MFE; the validity check below prevents putting
    # the stop above current market price.
    if side == "LONG" and best_pct >= 0.70:
        lock_pct += 0.06

    # Never place the protective stop inside ordinary BZU candle noise.
    # Use ATR/regime air. Before TP1 the stop must be wider than after TP1,
    # because there has not yet been a partial fix and BZU often retests.
    temp_context = {"price": price, "atr15": safe_float(profile.get("atr15"), None)}
    if not temp_context.get("atr15"):
        temp_context["atr15"] = price * 0.006
    air_profile = dict(profile or {})
    min_gap_pct = _stop_air_profile(temp_context, air_profile, "PRE_TP1")

    if side == "LONG":
        max_lock_now = current_pct - min_gap_pct
        if max_lock_now <= 0.08:
            return None, ""
        lock_pct = min(lock_pct, max_lock_now)
        level = entry * (1 + lock_pct / 100.0)
        level, air_reason = _apply_stop_air(side, level, price, entry, temp_context, air_profile, "PRE_TP1")
        if level is None or level >= price:
            return None, ""
    else:
        max_lock_now = current_pct - min_gap_pct
        if max_lock_now <= 0.08:
            return None, ""
        lock_pct = min(lock_pct, max_lock_now)
        level = entry * (1 - lock_pct / 100.0)
        level, air_reason = _apply_stop_air(side, level, price, entry, temp_context, air_profile, "PRE_TP1")
        if level is None or level <= price:
            return None, ""
    return round_price(level), label + f"; {air_reason}"

def _apply_more_protective_stop(trade, side, new_stop):
    """Apply only if the new stop improves protection and stays on valid side."""
    if new_stop is None:
        return False
    if side == "LONG" and new_stop > trade.stop_current:
        trade.stop_current = float(new_stop)
        trade.stop_updated_at = iso_now()
        return True
    if side == "SHORT" and new_stop < trade.stop_current:
        trade.stop_current = float(new_stop)
        trade.stop_updated_at = iso_now()
        return True
    return False


def _mfe_exit_floor(best_pct, market_regime):
    """Minimum acceptable captured MFE before closing/protecting a profitable trade.

    This is the regime-aware MFE Giveback Guard:
    - TREND: can give back about 40% of MFE, capture ~60%.
    - RANGE: can give back about 20%, capture ~80%.
    - PULLBACK/NORMAL: can give back about 30–35%.
    - REVERSAL: can give back about 25%.
    - NEWS_IMPULSE: can give back about 50%, because news moves are noisy.
    """
    best_pct = safe_float(best_pct, 0.0) or 0.0
    if best_pct <= 0:
        return 0.0

    allowed_giveback = {
        "TREND": 0.40,
        "RANGE": 0.20,
        "PULLBACK": 0.30,
        "REVERSAL": 0.25,
        "NEWS_IMPULSE": 0.50,
        "IMPULSE": 0.35,
        "NORMAL": 0.35,
        "UNKNOWN": 0.35,
    }.get(market_regime, 0.35)

    capture_ratio = 1.0 - allowed_giveback
    # For tiny MFE do not force unrealistic exits. Once MFE is meaningful,
    # the ratio becomes the main rule.
    min_absolute = 0.18
    if market_regime == "RANGE":
        min_absolute = 0.22
    elif market_regime == "TREND":
        min_absolute = 0.28
    elif market_regime == "NEWS_IMPULSE":
        min_absolute = 0.20
    elif market_regime == "REVERSAL":
        min_absolute = 0.22

    return max(min_absolute, best_pct * capture_ratio)


def _adaptive_mfe_guard_snapshot(trade, context, market_regime, current_pct, best_pct, giveback,
                                 giveback_ratio, post_tp1, tf3_against=False,
                                 structure_against=False, ict_against=False,
                                 flow_against=False, cvd_against=False,
                                 liquidity_against=False, news_against=False,
                                 confirmed_ict_reversal=False, support_votes=0.0,
                                 opposite_votes=0.0):
    """Adaptive MFE Giveback Guard 2.0.

    Protect real profit without killing good trends:
    - before TP1: mostly protect with stop, not panic close;
    - after TP1 / MFE > 2%: giveback threshold is stricter;
    - close after two confirmed warnings, except hard ICT/SMC reversal;
    - valid trend gets PROTECT first, not immediate close.
    """
    best_pct = safe_float(best_pct, 0.0) or 0.0
    current_pct = safe_float(current_pct, 0.0) or 0.0
    giveback = safe_float(giveback, 0.0) or 0.0
    giveback_ratio = safe_float(giveback_ratio, 0.0) or 0.0
    regime = str(market_regime or "NORMAL").upper()

    if best_pct < 0.45 or giveback <= 0:
        trade.mfe_giveback_streak = 0
        trade.mfe_giveback_last_state = "OK"
        return {"active": False, "streak": 0, "severity": "OK"}

    tp1_distance_pct = abs(pct(trade.tp1, trade.entry)) if getattr(trade, "entry", 0) else 0.0
    reached_tp1_zone = bool(post_tp1 or (tp1_distance_pct and best_pct >= tp1_distance_pct * 0.92))
    big_mfe = best_pct >= 2.0
    meaningful_mfe = best_pct >= max(0.65, tp1_distance_pct * 0.70 if tp1_distance_pct else 0.65)

    if not reached_tp1_zone and not big_mfe:
        allowed_giveback = 0.68
        stage = "PRE_TP1"
    elif getattr(trade, "tp2_hit", False):
        allowed_giveback = 0.45
        stage = "POST_TP2"
    elif reached_tp1_zone:
        allowed_giveback = 0.60
        stage = "POST_TP1"
    else:
        allowed_giveback = 0.55
        stage = "MFE"

    if big_mfe:
        allowed_giveback = min(allowed_giveback, 0.35)
        stage = "BIG_MFE"

    if regime == "TREND":
        allowed_giveback += 0.08
    elif regime in ["RANGE", "REVERSAL"]:
        allowed_giveback -= 0.08
    elif regime == "NEWS_IMPULSE":
        allowed_giveback += 0.08
    allowed_giveback = clamp(allowed_giveback, 0.28, 0.72)

    hard_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
    soft_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
    warning_score = hard_votes * 2 + soft_votes
    trend_still_valid = bool(
        regime == "TREND"
        and (support_votes >= 2.0 or (context.get("tf15") or {}).get("bias") == trade.side or context.get("bias") == trade.side)
        and not (structure_against or ict_against or confirmed_ict_reversal)
    )

    triggered = bool(meaningful_mfe and giveback_ratio >= allowed_giveback)
    if not triggered:
        trade.mfe_giveback_streak = max(0, int(getattr(trade, "mfe_giveback_streak", 0) or 0) - 1)
        trade.mfe_giveback_last_state = "COOLING" if trade.mfe_giveback_streak else "OK"
        return {
            "active": False,
            "streak": trade.mfe_giveback_streak,
            "severity": trade.mfe_giveback_last_state,
            "allowed_giveback": round(allowed_giveback, 3),
            "stage": stage,
        }

    trade.mfe_giveback_streak = int(getattr(trade, "mfe_giveback_streak", 0) or 0) + 1
    hard_reversal = bool(confirmed_ict_reversal or hard_votes >= 1)
    min_capture = _mfe_exit_floor(best_pct, regime)
    close_now = bool(
        hard_reversal
        or (
            trade.mfe_giveback_streak >= 2
            and warning_score >= 3
            and not trend_still_valid
            and (post_tp1 or big_mfe or current_pct <= min_capture)
        )
        or (big_mfe and trade.mfe_giveback_streak >= 2 and warning_score >= 2 and current_pct <= min_capture)
    )
    trade.mfe_giveback_last_state = "CLOSE" if close_now else "PROTECT"

    reason_bits = [
        f"MFE було +{round(best_pct, 3)}%, зараз {round(current_pct, 3)}%",
        f"віддано {round(giveback_ratio * 100, 1)}% руху; ліміт {round(allowed_giveback * 100, 1)}%",
        f"перевірка {trade.mfe_giveback_streak}/2",
    ]
    if hard_reversal:
        reason_bits.append("ICT/SMC злам підтверджений")
    elif trend_still_valid:
        reason_bits.append("тренд ще живий — спершу захист, не панічний вихід")

    return {
        "active": True,
        "close": close_now,
        "protect": not close_now,
        "stage": stage,
        "streak": trade.mfe_giveback_streak,
        "allowed_giveback": round(allowed_giveback, 3),
        "warning_score": warning_score,
        "hard_reversal": hard_reversal,
        "trend_still_valid": trend_still_valid,
        "reason": "; ".join(reason_bits),
        "reasons": reason_bits,
    }

def active_trade_message_key(trade, action):
    return f"{trade.id}:{action}:{trade.tp1_hit}:{trade.tp2_hit}:{trade.tp3_hit}:{trade.tp1_stop_locked}:{trade.tp2_stop_locked}:{round_price(trade.stop_current)}"


def _has_confirmed_ict_reversal(side, context):
    """True only when the opposite side has real ICT/SMC reversal evidence."""
    opp = opposite(side)
    structure = (context or {}).get("structure") or {}
    ict = (context or {}).get("ict") or {}
    liquidity = (context or {}).get("liquidity") or {}
    phase = str(structure.get("phase", "")).upper()
    ict_setup = str(ict.get("setup", "")).upper()
    liq_event = str(liquidity.get("event", "")).upper()
    if opp == "LONG":
        choch = "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase
        sweep = liquidity.get("bias") == "LONG" and any(x in liq_event for x in ["SWEEP", "RECLAIM"])
        model = ict.get("bias") == "LONG" and (ict.get("entry_ok") or any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]))
    else:
        choch = "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase
        sweep = liquidity.get("bias") == "SHORT" and any(x in liq_event for x in ["SWEEP", "RECLAIM"])
        model = ict.get("bias") == "SHORT" and (ict.get("entry_ok") or any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]))
    components = int(bool(choch)) + int(bool(sweep)) + int(bool(model))
    return components >= 2, components

def _should_apply_pre_tp_profit_stop(trade, context, proposed_stop, best_pct, current_pct, market_regime):
    """Avoid moving pre-TP1 stops on every small MFE."""
    if proposed_stop is None or trade is None:
        return False, ""
    if getattr(trade, "tp1_hit", False):
        return True, "після TP1 стоп керується TP-lock логікою"
    price = safe_float((context or {}).get("price"), 0.0) or 0.0
    if not price or not trade.entry:
        return False, "немає ціни для перевірки стопу"
    atr_value = safe_float((context or {}).get("atr15"), price * 0.006) or price * 0.006
    min_gap_pct = _stop_air_profile(context, trade_mode_profile(context, trade.side), "PRE_TP1")
    gap_pct = ((price - proposed_stop) / trade.entry * 100.0) if trade.side == "LONG" else ((proposed_stop - price) / trade.entry * 100.0)
    if gap_pct < min_gap_pct:
        return False, f"стоп занадто близько до ціни до TP1 ({round(gap_pct, 3)}% < {round(min_gap_pct, 3)}%)"
    smc_level, _ = _last_smc_trailing_level(trade.side, (context or {}).get("candles_3m") or [], price, trade.entry, atr_value)
    ict_level, _ = _ict_zone_trailing_level(trade.side, (context or {}).get("ict") or {}, price, atr_value)
    has_structure_anchor = smc_level is not None or ict_level is not None
    if best_pct >= 1.05 and current_pct >= 0.45:
        return True, "MFE достатній для першого захисту до TP1"
    if has_structure_anchor and best_pct >= 0.65 and current_pct >= 0.25:
        return True, "є SMC/ICT опора для стопу до TP1"
    return False, "до TP1 стоп не рухати без нового swing/ICT або достатнього MFE"

def _exit_reason_code(result, context=None, trade=None):
    action = str((result or {}).get("action") or "")
    notes = " ".join(str(x) for x in ((result or {}).get("notes") or [])).lower()
    if action.startswith("TP"):
        return "TP_HIT"
    if action == "STOP":
        return "STOP_BY_HIGH_LOW"
    if "MFE" in action or "giveback" in notes or "відда" in notes:
        return "MFE_GIVEBACK"
    if "LOCAL_BREAK" in action or "3m" in notes or "структ" in notes:
        return "STRUCTURE_BREAK"
    if "EXIT" in action:
        return "MANUAL_EXIT_SIGNAL"
    return action or "UNKNOWN"

def _exit_quality_score(trade, result, context):
    """0-100 quality of the exit, separate from entry quality."""
    action = str((result or {}).get("action") or "")
    current_pct = safe_float((result or {}).get("current_pct"), 0.0) or 0.0
    best_pct = safe_float((result or {}).get("best_pct"), 0.0) or 0.0
    captured = (current_pct / best_pct * 100.0) if best_pct > 0.1 else None
    confirmed_rev, _ = _has_confirmed_ict_reversal(getattr(trade, "side", "NEUTRAL"), context or {})
    score = 55.0
    if action.startswith("TP"):
        score += 25
    if action == "STOP" and current_pct >= 0:
        score += 16
    elif action == "STOP" and current_pct < 0:
        score -= 18
    if captured is not None:
        if captured >= 70:
            score += 14
        elif captured >= 50:
            score += 6
        elif captured < 30:
            score -= 16
    if confirmed_rev:
        score += 10
    elif "EXIT" in action and current_pct > 0:
        score -= 6
    return int(max(0, min(100, round(score))))

def active_trade_risk_snapshot(trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, action):
    """Professional, dynamic supervision risk model.

    This is informational only: it does not open, close or move stops by itself.
    It is recalculated on EVERY follow message from the fresh context and answers:
      1) Current trade risk: how safe it is to keep holding the open position.
      2) Reversal risk: whether a real opposite setup is forming.

    The model is intentionally organic, not a hard one-factor switch:
    - 15M / structure / ICT have the largest weight.
    - 3M is important, but can be noisy.
    - CVD / flow / clusters confirm or warn, but do not dominate alone.
    - MFE giveback raises current trade risk, especially after meaningful profit.
    - In a trend, normal pullbacks are tolerated more than in range/reversal modes.
    """
    side = trade.side
    opp = opposite(side)

    def block(name):
        return context.get(name) or {}

    tf3 = block("tf3")
    tf15 = block("tf15")
    tf1h = block("tf1h")
    tf4h = block("tf4h")
    structure = block("structure")
    ict = block("ict")
    cvd = block("cvd")
    flow = block("flow")
    clusters = block("clusters")
    derivatives = block("derivatives")
    liquidity = block("liquidity")
    news = block("news")

    mode_profile = trade_mode_profile(context, side)
    market_regime = mode_profile.get("regime", "NORMAL")

    def clamp(value, lo=0, hi=100):
        return max(lo, min(hi, value))

    def strength(b, norm=40):
        try:
            return clamp(abs(float((b or {}).get("score", 0) or 0)) / float(norm), 0.35, 1.25)
        except Exception:
            return 0.65

    def risk_label(score):
        if score >= 82:
            return "КРИТИЧНИЙ"
        if score >= 64:
            return "ВИСОКИЙ"
        if score >= 36:
            return "СЕРЕДНІЙ"
        return "НИЗЬКИЙ"

    def reversal_label(score):
        if score >= 82:
            return "ДУЖЕ ВИСОКИЙ"
        if score >= 64:
            return "ВИСОКИЙ"
        if score >= 42:
            return "СЕРЕДНІЙ"
        if score >= 24:
            return "ПОМІРНИЙ"
        return "НИЗЬКИЙ"

    def ict_reversal_evidence():
        """Return how strong the opposite ICT reversal is.

        CVD/flow/news may warn, but they must not create HIGH/CRITICAL
        reversal labels by themselves. For a scary label the bot needs actual
        ICT/structure evidence: CHOCH/sweep/reclaim/FVG/OB against the trade.
        """
        phase = str((structure or {}).get("phase", "")).upper()
        ict_setup = str((ict or {}).get("setup", "")).upper()
        ict_state = str((ict or {}).get("state", "")).upper()
        ict_note = str((ict or {}).get("note", "")).lower()

        components = []
        if opp == "LONG":
            if "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase:
                components.append("CHOCH/SWEEP LONG")
            if (liquidity or {}).get("bias") == "LONG" and any(x in str((liquidity or {}).get("event", "")).upper() for x in ["SWEEP", "RECLAIM"]):
                components.append("LIQUIDITY RECLAIM LONG")
            if (ict or {}).get("bias") == "LONG" and any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]):
                components.append("ICT MODEL LONG")
            if (ict or {}).get("entry_ok") and (ict or {}).get("bias") == "LONG":
                components.append("ICT ENTRY LONG")
        else:
            if "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase:
                components.append("CHOCH/SWEEP SHORT")
            if (liquidity or {}).get("bias") == "SHORT" and any(x in str((liquidity or {}).get("event", "")).upper() for x in ["SWEEP", "RECLAIM"]):
                components.append("LIQUIDITY RECLAIM SHORT")
            if (ict or {}).get("bias") == "SHORT" and any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]):
                components.append("ICT MODEL SHORT")
            if (ict or {}).get("entry_ok") and (ict or {}).get("bias") == "SHORT":
                components.append("ICT ENTRY SHORT")

        # Avoid duplicate inflation when the same idea is seen by two modules.
        return len(set(components)), list(dict.fromkeys(components))

    ict_rev_count, ict_rev_components = ict_reversal_evidence()

    # -------------------------
    # 1) Current trade risk
    # -------------------------
    # Start from a neutral-but-not-scary baseline. The score then moves up/down
    # according to fresh market evidence.
    trade_risk_score = 30.0

    # If the bot itself is no longer in a clean HOLD state, risk should rise,
    # but not automatically become HIGH; strong 15M/ICT can still keep it moderate.
    if action in ["EXIT_WARNING", "PROTECT_OR_EXIT"]:
        trade_risk_score += 18
    elif action in ["PROTECT", "TP1_PROTECT", "TP2_PROTECT", "TRAIL_ICT_SMC"]:
        trade_risk_score += 6
    elif action in ["EXIT_AFTER_TP1_GIVEBACK", "EXIT_MFE_GIVEBACK"]:
        trade_risk_score += 30

    # Position result / stop proximity.
    if current_pct <= -0.45:
        trade_risk_score += 22
    elif current_pct <= -0.18:
        trade_risk_score += 13
    elif current_pct < 0:
        trade_risk_score += 7
    elif current_pct >= 0.55:
        trade_risk_score -= 6
    elif current_pct >= 0.25:
        trade_risk_score -= 3

    # MFE giveback: if the market gave profit and is taking it back, risk rises.
    if best_pct > 0.20:
        giveback_ratio = clamp(giveback / best_pct if best_pct else 0.0, 0.0, 1.0)
        if best_pct >= 0.90:
            trade_risk_score += giveback_ratio * 22
        elif best_pct >= 0.55:
            trade_risk_score += giveback_ratio * 17
        else:
            trade_risk_score += giveback_ratio * 10
        if current_pct > 0 and giveback_ratio < 0.25:
            trade_risk_score -= 4

    # Follow-through failure: if after several runs the trade did not move toward TP1,
    # the position risk must rise even if 3M is not fully reversed yet. This catches
    # situations like SHORT made after a drop: price gave only small MFE, then bounced
    # back toward/above entry. It is informational only, not an auto-exit.
    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    weak_followthrough = bool(elapsed_min >= 45 and best_pct < 0.30 and current_pct <= 0.08)
    no_followthrough = bool(elapsed_min >= 75 and best_pct < 0.22 and current_pct <= 0.05)
    gave_back_small_mfe = bool(best_pct >= 0.12 and giveback >= max(0.12, best_pct * 0.65))

    if weak_followthrough:
        trade_risk_score += 8
    if no_followthrough:
        trade_risk_score += 10
    if gave_back_small_mfe:
        trade_risk_score += 7

    # Universal trade validation: does the opened idea behave as expected?
    # This is integrated into "Поточний ризик угоди"; it is not displayed as a
    # separate noisy Telegram block. It works for all regimes, not only countertrend.
    trade_validation = trade_entry_validation_snapshot(trade, context, current_pct, best_pct, giveback)
    if trade_validation:
        trade_risk_score += float(trade_validation.get("risk_adjustment", 0) or 0)
        if trade_validation.get("severity") == "BAD":
            reversal_score_floor_from_validation = 26
        elif trade_validation.get("severity") == "WARN":
            reversal_score_floor_from_validation = 20
        else:
            reversal_score_floor_from_validation = 0
    else:
        reversal_score_floor_from_validation = 0

    # Hidden ELS support: if the current position was opened/managed from a poor
    # location, the informational risk should not stay unrealistically low.
    # This is only a small organic adjustment; it never moves stop/TP by itself.
    entry_location = context.get("entry_location") or analyze_entry_location_score(side, context)
    els_score = int(entry_location.get("score", 62) or 62)
    if els_score < 38:
        trade_risk_score += 9
        reversal_score_floor_from_els = 26
    elif els_score < 50:
        trade_risk_score += 5
        reversal_score_floor_from_els = 22
    elif els_score >= 78 and current_pct >= 0:
        trade_risk_score -= 3
        reversal_score_floor_from_els = 0
    else:
        reversal_score_floor_from_els = 0

    # Distance to active stop: when price is already near the protective stop, risk is not low.
    if trade.entry:
        if side == "LONG":
            stop_dist_pct = max(0.0, (safe_float(context.get("price"), trade.entry) - trade.stop_current) / trade.entry * 100)
        else:
            stop_dist_pct = max(0.0, (trade.stop_current - safe_float(context.get("price"), trade.entry)) / trade.entry * 100)
        if stop_dist_pct <= 0.12:
            trade_risk_score += 12
        elif stop_dist_pct <= 0.28:
            trade_risk_score += 7

    # Fresh market alignment. 15M, structure and ICT dominate; flow confirms.
    risk_blocks = [
        (tf15, 22, 45),
        (structure, 22, 35),
        (ict, 22, 38),
        (tf3, 13, 65),
        # Flow/CVD/news are warning layers, not reasons for HIGH/CRITICAL
        # by themselves. Their weights stay soft to avoid psychological noise.
        (cvd, 4, 25),
        (flow, 4, 25),
        (clusters, 3, 14),
        (derivatives, 3, 18),
        (liquidity, 7, 18),
        (tf1h, 8, 55),
        (tf4h, 5, 60),
        (news, 1, 35),
    ]
    for b, weight, norm in risk_blocks:
        bias = (b or {}).get("bias")
        s = strength(b, norm)
        if bias == opp:
            trade_risk_score += weight * s
        elif bias == side:
            trade_risk_score -= weight * min(s, 1.0)

    # Regime smoothing: do not panic in a real trend, but be stricter in range/reversal.
    if market_regime == "TREND" and (tf15.get("bias") == side or ict.get("bias") == side or structure.get("bias") == side):
        trade_risk_score -= 7
    elif market_regime == "RANGE":
        trade_risk_score += 7
    elif market_regime == "REVERSAL":
        trade_risk_score += 5
    elif market_regime == "NEWS_IMPULSE":
        trade_risk_score += 3

    if trade.tp1_hit:
        trade_risk_score -= 6
    if "RISKY_ENTRY" in (trade.notes or []):
        trade_risk_score += 5

    # Counter-trend context floor: if both 1H and 4H are against the open trade,
    # the trade can still be valid, but risk should not be shown as ultra-low.
    higher_tf_against = (tf1h.get("bias") == opp and tf4h.get("bias") == opp)
    intraday_supports = sum([
        tf15.get("bias") == side,
        structure.get("bias") == side,
        ict.get("bias") == side,
        tf3.get("bias") == side,
        flow.get("bias") == side or cvd.get("bias") == side,
    ])
    if higher_tf_against:
        floor_value = 24 if (current_pct > 0.45 and intraday_supports >= 3) else 32
        trade_risk_score = max(trade_risk_score, floor_value)

    # Support/opposition aggregate from manage_active_trade, used as soft confirmation.
    trade_risk_score += max(0.0, opposite_votes - support_votes) * 4.5
    trade_risk_score -= max(0.0, support_votes - opposite_votes) * 2.5
    if higher_tf_against:
        floor_value = 24 if (current_pct > 0.45 and intraday_supports >= 3) else 32
        trade_risk_score = max(trade_risk_score, floor_value)

    # Do not show scary current-risk labels when there is no real ICT/structure
    # reversal. CVD/flow/news can lift risk to MEDIUM, but HIGH/CRITICAL should
    # require ICT/structure evidence or the trade actually being close to stop/deep red.
    if ict_rev_count == 0:
        # Without ICT/structure reversal evidence, CVD/flow/news can warn,
        # but must not print scary HIGH/CRITICAL risk. If the trade is not
        # materially losing, cap it at MEDIUM.
        if current_pct > -0.25 and action not in ["EXIT_AFTER_TP1_GIVEBACK", "EXIT_MFE_GIVEBACK"]:
            trade_risk_score = min(trade_risk_score, 52)
    elif ict_rev_count == 1:
        # One ICT clue means caution, not panic. HIGH requires a cluster of
        # real reversal evidence or actual stop/deep loss conditions.
        trade_risk_score = min(trade_risk_score, 62 if current_pct > -0.35 else 68)

    trade_risk_score = int(round(clamp(trade_risk_score)))
    trade_risk = risk_label(trade_risk_score)

    # -------------------------
    # 2) Opposite/reversal risk
    # -------------------------
    # This is not just "blocks against position". It asks whether the opposite
    # side is becoming a tradable setup: 3M + structure/ICT + flow/confirmation.
    reversal_score = 8.0

    reversal_blocks = [
        (tf3, 16, 65),
        (tf15, 20, 45),
        (structure, 24, 35),
        (ict, 24, 38),
        # These are confirmations/warnings only. They should not create a
        # reversal call without ICT/structure.
        (cvd, 6, 25),
        (flow, 6, 25),
        (clusters, 4, 14),
        (liquidity, 8, 18),
        (derivatives, 3, 18),
        (tf1h, 5, 55),
        (tf4h, 2, 60),
    ]
    opposite_core = 0
    support_core = 0
    soft_opposite_pressure = 0
    for b, weight, norm in reversal_blocks:
        bias = (b or {}).get("bias")
        s = strength(b, norm)
        signed_to_opp = side_score(int((b or {}).get("score", 0) or 0), opp)
        if bias == opp:
            reversal_score += weight * s
            soft_opposite_pressure += 1
            if b in [tf3, tf15, structure, ict]:
                opposite_core += 1
        elif signed_to_opp >= max(6, int(norm * 0.22)):
            # Some modules keep bias NEUTRAL while their signed score already
            # shows pressure for the opposite side (common in CVD/flow/clusters).
            reversal_score += weight * 0.45 * min(s, 1.0)
            soft_opposite_pressure += 1
        elif bias == side:
            reversal_score -= (weight * 0.65) * min(s, 1.0)
            if b in [tf3, tf15, structure, ict]:
                support_core += 1

    # A real reversal normally needs at least two core components.
    if opposite_core >= 3:
        reversal_score += 12
    elif opposite_core == 2:
        reversal_score += 6
    elif opposite_core == 0:
        reversal_score -= 10

    if support_core >= 3:
        reversal_score -= 12
    elif support_core == 2:
        reversal_score -= 6

    # If the open trade fails to follow through, the opposite side becomes more
    # credible even before a textbook opposite ICT setup appears. This prevents
    # unrealistic readings like "LONG reversal 5%" after a weak SHORT that failed
    # to expand.
    if weak_followthrough:
        reversal_score += 7
    if no_followthrough:
        reversal_score += 10
    if gave_back_small_mfe:
        reversal_score += 6
    if soft_opposite_pressure >= 3:
        reversal_score += 5

    # MFE and PnL context: when a trade gives back profit or goes red, opposite scenario gains credibility.
    if current_pct < -0.15:
        reversal_score += 8
    if best_pct > 0.45:
        giveback_ratio = clamp(giveback / best_pct if best_pct else 0.0, 0.0, 1.0)
        reversal_score += giveback_ratio * 12
    if trade.tp1_hit and current_pct > 0:
        reversal_score -= 6

    # In a clean trend with higher timeframe still supporting the position, avoid overcalling reversals.
    if market_regime == "TREND" and (tf15.get("bias") == side or tf1h.get("bias") == side) and opposite_core < 3:
        reversal_score -= 8
    if market_regime == "RANGE":
        reversal_score += 4
    if higher_tf_against and opposite_core >= 1:
        reversal_score = max(reversal_score, 22)

    # Floors for weak/failed positions. These floors are intentionally moderate:
    # they do not force exits, but they stop the bot from displaying an
    # unrealistically low reversal probability while the trade is already stalling.
    if no_followthrough:
        reversal_score = max(reversal_score, 28)
    elif weak_followthrough or gave_back_small_mfe:
        reversal_score = max(reversal_score, 22)
    if current_pct < 0 and best_pct < 0.25 and elapsed_min >= 60:
        reversal_score = max(reversal_score, 25)
    if 'reversal_score_floor_from_els' in locals() and reversal_score_floor_from_els:
        reversal_score = max(reversal_score, reversal_score_floor_from_els)
    if 'reversal_score_floor_from_validation' in locals() and reversal_score_floor_from_validation:
        reversal_score = max(reversal_score, reversal_score_floor_from_validation)

    # ICT-gated labels: without a real opposite ICT/structure setup, do not
    # frighten the trader with HIGH/CRITICAL reversal labels.
    # 0 ICT components -> max low/moderate warning.
    # 1 component -> max medium.
    # 2 components -> high is possible.
    # 3+ components -> very high is possible.
    if ict_rev_count == 0:
        reversal_score = min(reversal_score, 36)
    elif ict_rev_count == 1:
        reversal_score = min(reversal_score, 48)
    elif ict_rev_count == 2:
        reversal_score = min(reversal_score, 72)

    reversal_score = int(round(clamp(reversal_score)))
    rev_label = reversal_label(reversal_score)

    return {
        "trade_risk": trade_risk,
        "trade_risk_score": trade_risk_score,
        "trade_risk_reasons": [],
        "reversal_side": opp,
        "reversal_score": reversal_score,
        "reversal_label": rev_label,
        "reversal_reasons": [],
        "ict_reversal_components": ict_rev_components,
        "ict_reversal_count": ict_rev_count,
        "trade_validation": trade_validation,
    }



def trade_entry_validation_snapshot(trade, context, current_pct, best_pct, giveback,
                                    tf3_against=False, structure_against=False,
                                    ict_against=False, flow_against=False, cvd_against=False):
    """Universal trade validation used inside current trade risk.

    It answers one question: is the opened idea behaving as expected after entry?
    This is NOT a separate Telegram block and it does NOT delay entries. It only
    adjusts "Поточний ризик угоди" and gives manage_active_trade a soft warning
    when a position fails to develop.
    """
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}

    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    notes = [str(x).upper() for x in (getattr(trade, "notes", None) or [])]
    flagged_countertrend = any("COUNTERTREND_ENTRY" in n or "КОНТРТРЕНД" in n for n in notes)
    htf_against_now = bool(tf1h.get("bias") == opp or tf4h.get("bias") == opp)
    is_countertrend = bool(flagged_countertrend or ("RISKY_ENTRY" in notes and htf_against_now))

    confirmations = 0
    warnings = 0

    # Price/action validation. A normal trend trade gets more time; a countertrend
    # trade must start working sooner, but this still remains informational.
    if current_pct >= 0.18:
        confirmations += 1
    if best_pct >= (0.24 if is_countertrend else 0.32):
        confirmations += 1
    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 24:
        confirmations += 1
    if tf15.get("bias") == side:
        confirmations += 1
    if structure.get("bias") == side:
        confirmations += 1
    if ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16):
        confirmations += 1
    if flow.get("bias") == side or cvd.get("bias") == side:
        confirmations += 1

    if current_pct <= (-0.16 if is_countertrend else -0.22):
        warnings += 1
    if best_pct < (0.16 if is_countertrend else 0.20) and elapsed_min >= 14:
        warnings += 1
    if elapsed_min >= 45 and best_pct < 0.25 and current_pct <= 0.05:
        warnings += 1
    if tf3_against or tf3.get("bias") == opp:
        warnings += 1
    if structure_against or structure.get("bias") == opp:
        warnings += 1
    if ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16):
        warnings += 1
    if flow_against and cvd_against:
        warnings += 1
    if giveback >= max(0.18, best_pct * 0.65) and best_pct >= 0.20:
        warnings += 1

    raw = 50 + confirmations * 8 - warnings * 10
    if is_countertrend:
        raw -= 6
        # Countertrend entries that do not develop by the first follow message
        # should be treated as higher current-risk, not as a normal trade.
        if elapsed_min >= 14 and confirmations <= 1 and warnings >= 2:
            raw -= 10

    score = int(max(0, min(100, round(raw))))
    if score >= 70:
        status = "CONFIRMED"
        risk_adjustment = -8
        severity = "OK"
    elif score >= 52:
        status = "VALIDATING"
        risk_adjustment = 0
        severity = "WATCH"
    elif score >= 36:
        status = "WEAK"
        risk_adjustment = 8 if not is_countertrend else 12
        severity = "WARN"
    else:
        status = "FAILED"
        risk_adjustment = 16 if not is_countertrend else 22
        severity = "BAD"

    return {
        "active": True,
        "status": status,
        "severity": severity,
        "score": score,
        "risk_adjustment": risk_adjustment,
        "is_countertrend": is_countertrend,
        "elapsed_min": round(elapsed_min, 1),
        "confirmations": confirmations,
        "warnings": warnings,
    }


def entry_point_state_snapshot(trade, context, current_pct, best_pct, giveback,
                               high_since_open=None, low_since_open=None,
                               trade_validation=None,
                               tf3_against=False, structure_against=False,
                               ict_against=False, flow_against=False, cvd_against=False):
    """Professional lifecycle model for the original entry point.

    This is not a duplicate of generic risk. It answers: is the exact entry
    price/idea still valid, damaged but recoverable, or broken?

    It uses:
    - MFE/MAE relative to the initial stop;
    - whether price recovered back to entry after a bad move;
    - 3M/15M/ICT/structure/flow confirmation;
    - deep adverse move before the formal stop.
    """
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)
    price = safe_float(context.get("price"), getattr(trade, "entry", 0)) or getattr(trade, "entry", 0)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}

    entry = safe_float(getattr(trade, "entry", 0), 0) or 0
    stop_initial = safe_float(getattr(trade, "stop_initial", getattr(trade, "stop_current", 0)), 0) or 0
    stop_current = safe_float(getattr(trade, "stop_current", stop_initial), stop_initial) or stop_initial
    risk_distance_pct = abs(entry - stop_initial) / entry * 100 if entry else 0.0
    risk_distance_pct = max(risk_distance_pct, 0.01)

    if side == "LONG":
        worst_price = safe_float(low_since_open, min(price, entry))
        adverse_pct = max(0.0, (entry - worst_price) / entry * 100 if entry else 0.0)
        stop_distance_now_pct = max(0.0, (price - stop_current) / entry * 100 if entry else 99.0)
    else:
        worst_price = safe_float(high_since_open, max(price, entry))
        adverse_pct = max(0.0, (worst_price - entry) / entry * 100 if entry else 0.0)
        stop_distance_now_pct = max(0.0, (stop_current - price) / entry * 100 if entry else 99.0)

    adverse_fraction = adverse_pct / risk_distance_pct if risk_distance_pct else 0.0
    near_entry = abs(current_pct) <= 0.16
    recovered_to_entry = bool(adverse_fraction >= 0.34 and near_entry)
    deep_adverse = bool(adverse_fraction >= 0.60)
    near_stop = bool(stop_distance_now_pct <= max(0.12, risk_distance_pct * 0.18))

    core_support = 0
    core_against = 0
    soft_support = 0
    soft_against = 0
    for block in [tf3, tf15, structure, ict]:
        if block.get("bias") == side:
            core_support += 1
        elif block.get("bias") == opp:
            core_against += 1
    for block in [flow, cvd]:
        if block.get("bias") == side:
            soft_support += 1
        elif block.get("bias") == opp:
            soft_against += 1

    ict_valid = bool(ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16))
    structure_valid = bool(structure.get("bias") == side)
    structure_broken = bool(structure_against or structure.get("bias") == opp)
    ict_broken = bool(ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16))

    base = int((trade_validation or {}).get("score", getattr(trade, "entry_integrity_score", 100) or 100) or 50)
    score = float(base)

    # Price lifecycle.
    if current_pct >= 0.45:
        score += 10
    elif current_pct >= 0.18:
        score += 5
    elif current_pct <= -0.45:
        score -= 16
    elif current_pct <= -0.22:
        score -= 9

    if best_pct >= 0.55:
        score += 7
    elif best_pct >= 0.25:
        score += 3
    elif best_pct < 0.16:
        score -= 6

    if deep_adverse:
        score -= 18
    elif adverse_fraction >= 0.42:
        score -= 10

    if core_support >= 3:
        score += 12
    elif core_support == 2:
        score += 6
    if core_against >= 3:
        score -= 18
    elif core_against == 2:
        score -= 10
    if soft_support >= 1:
        score += 3
    if soft_against >= 2:
        score -= 5

    if giveback >= max(0.20, best_pct * 0.65) and best_pct >= 0.25:
        score -= 7

    recovery_mode = "NONE"
    reason = "точка входу працює штатно"
    advice = "можна залишатися в угоді, поки ICT/структура не зламаються"
    auto_exit = False
    hard_broken = False

    if recovered_to_entry:
        recovery_mode = "RECHECK_AT_ENTRY"
        if (core_support >= 2 or ict_valid or structure_valid) and core_against <= 1:
            # Price came back to entry after a bad move, but the setup rebuilt.
            # Re-rate from the current context instead of punishing it forever.
            score = max(score, 62)
            reason = "ціна повернулась до входу після просадки; сетап повторно перевірений і ще має підтвердження"
            advice = "залишатися можна, але без усереднення; якщо наступний супровід знову слабшає — вихід біля входу"
        else:
            score = min(score, 48)
            reason = "ціна повернулась до входу після просадки, але ICT/структура не відновили сетап"
            advice = "краще закрити біля входу, якщо наступний імпульс не підтвердить напрям"
    elif deep_adverse:
        recovery_mode = "DEEP_ADVERSE_MOVE"
        if structure_broken or ict_broken or core_against >= 2:
            score = min(score, 34)
            reason = "ціна пройшла понад 60% шляху до стопа і структура/ICT проти точки входу"
            advice = "вихід раніше повного стопа; точка входу втратила актуальність"
            auto_exit = True
            hard_broken = True
        else:
            score = min(score, 55)
            reason = "глибока просадка до стопа, але повного ICT/SMC зламу ще немає"
            advice = "тримати тільки до наступної перевірки; без reclaim/підтвердження не чекати повний стоп"
    elif (trade_validation or {}).get("severity") == "BAD":
        reason = "угода не розкривається після входу: MFE слабкий або структура/3M проти"
        advice = "не чекати дальній стоп; шукати вихід біля входу або при наступному слабкому супроводі"
    elif (trade_validation or {}).get("severity") == "WARN":
        reason = "точка входу слабшає, але ще не зламана"
        advice = "залишатися тільки якщо наступний супровід покаже відновлення"

    score = int(max(0, min(100, round(score))))
    if score >= 80:
        label = "🟢 Сильна"
        state = "STRONG"
    elif score >= 60:
        label = "🟡 Робоча"
        state = "WORKING"
    elif score >= 40:
        label = "🟠 Слабка"
        state = "WEAK"
    else:
        label = "🔴 Зламана"
        state = "BROKEN"
        if core_against >= 2 or hard_broken:
            auto_exit = True

    fail_now = state in ["WEAK", "BROKEN"] and not (recovered_to_entry and state == "WORKING")

    return {
        "active": True,
        "score": score,
        "label": label,
        "state": state,
        "reason": reason,
        "advice": advice,
        "fail_now": fail_now,
        "auto_exit": bool(auto_exit),
        "recovery_mode": recovery_mode,
        "adverse_pct": round(adverse_pct, 3),
        "adverse_fraction": round(adverse_fraction, 3),
        "near_entry": near_entry,
        "deep_adverse": deep_adverse,
        "near_stop": near_stop,
        "core_support": core_support,
        "core_against": core_against,
        "soft_support": soft_support,
        "soft_against": soft_against,
    }


def countertrend_entry_validation(trade, context, current_pct, best_pct, giveback, tf3_against=False,
                                  structure_against=False, ict_against=False, flow_against=False, cvd_against=False):
    """Status block for early counter-trend entries.

    It is informational for Telegram supervision. It does NOT close the trade by itself
    and it does NOT delay the entry. Its purpose is to tell the user on the next
    15-minute follow message whether a risky counter-trend reversal is confirming
    or failing, so the user is not forced to watch 3M candles manually.
    """
    notes = [str(x).upper() for x in (getattr(trade, "notes", None) or [])]
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}

    flagged_countertrend = any("COUNTERTREND_ENTRY" in n or "КОНТРТРЕНД" in n for n in notes)
    htf_against_now = bool(tf1h.get("bias") == opp or tf4h.get("bias") == opp)
    is_countertrend = bool(flagged_countertrend or ("RISKY_ENTRY" in notes and htf_against_now))
    if not is_countertrend:
        return None

    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    # Confirmation means the reversal started to work, not merely that it is not stopped.
    confirms = []
    warnings = []
    if current_pct >= 0.18:
        confirms.append("ціна вже утримується в плюс")
    if best_pct >= 0.28:
        confirms.append("угода дала достатній MFE для раннього розвороту")
    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 24:
        confirms.append("3M підтримує напрям")
    if tf15.get("bias") == side:
        confirms.append("15M підтверджує")
    if structure.get("bias") == side:
        confirms.append("структура підтримує")
    if ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16):
        confirms.append("ICT підтримує")

    if current_pct <= -0.18:
        warnings.append("ціна проти входу")
    if best_pct < 0.18 and elapsed_min >= 14:
        warnings.append("після входу немає нормального розкриття")
    if tf3_against or tf3.get("bias") == opp:
        warnings.append("3M вже проти")
    if structure_against or structure.get("bias") == opp:
        warnings.append("структура проти")
    if ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16):
        warnings.append("ICT проти")
    if flow_against and cvd_against:
        warnings.append("CVD і потік проти")

    # First follow after 15 minutes is the important user-facing checkpoint.
    if len(confirms) >= 3 and current_pct > -0.05:
        status = "ПІДТВЕРДЖУЄТЬСЯ ✅"
        advice = "можна залишатися в угоді; далі супровід як звичайна позиція, але стоп обовʼязковий"
        severity = "OK"
        score = 28
    elif (len(warnings) >= 3 and len(confirms) <= 1) or (current_pct <= -0.28 and best_pct < 0.25):
        status = "НЕ ПІДТВЕРДЖУЄТЬСЯ ⚠️"
        advice = "контртрендовий розворот не розкрився; краще захистити/закрити біля входу або не чекати повний стоп"
        severity = "BAD"
        score = 72
    elif len(warnings) >= 2 and len(confirms) <= 2:
        status = "СЛАБКЕ ПІДТВЕРДЖЕННЯ ⚠️"
        advice = "залишатися тільки якщо наступний супровід покаже покращення; не розширювати стоп"
        severity = "WARN"
        score = 56
    else:
        status = "ЩЕ ПЕРЕВІРЯЄТЬСЯ"
        advice = "чекати наступне підтвердження; якщо ціна не дає MFE і 3M стане проти — виходити раніше"
        severity = "WATCH"
        score = 42

    return {
        "active": True,
        "status": status,
        "advice": advice,
        "severity": severity,
        "score": score,
        "elapsed_min": round(elapsed_min, 1),
        "confirmations": confirms[:3],
        "warnings": warnings[:3],
    }


def _trade_open_elapsed_minutes(trade):
    try:
        opened = datetime.fromisoformat(str(getattr(trade, "opened_at", "")).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc() - opened).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _candle_ts_to_dt(ts):
    try:
        ts = float(ts)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _candles_after_trade_open(trade, candles):
    try:
        opened = datetime.fromisoformat(str(getattr(trade, "opened_at", "")).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
    except Exception:
        opened = None
    out = []
    for c in (candles or []):
        dt = _candle_ts_to_dt(getattr(c, "ts", None))
        if opened is None or dt is None or dt >= opened:
            out.append(c)
    return out


def _close_mfe_mae_after_open(trade, context, current_price=None):
    """Close-based MFE/MAE for phase management.

    Wick MFE is useful for TP/stop detection, but professional supervision should
    not tighten or close too early from a single wick. This helper separates:
    - close_mfe_pct: best favorable candle close after entry;
    - confirmed_mfe_pct: close MFE confirmed by at least two favorable closes;
    - close_mae_pct: worst adverse candle close after entry.
    """
    side = getattr(trade, "side", "")
    entry = safe_float(getattr(trade, "entry", None), 0.0) or 0.0
    if side not in ["LONG", "SHORT"] or not entry:
        return {"close_mfe_pct": 0.0, "confirmed_mfe_pct": 0.0, "close_mae_pct": 0.0, "follow_closes": 0}
    candles = _candles_after_trade_open(trade, (context or {}).get("candles_3m") or [])
    vals = []
    for c in candles:
        cl = safe_float(getattr(c, "close", None), None)
        if cl:
            vals.append(signed_pct(side, entry, cl))
    cp = safe_float(current_price if current_price is not None else (context or {}).get("price"), None)
    if cp:
        vals.append(signed_pct(side, entry, cp))
    if not vals:
        return {"close_mfe_pct": 0.0, "confirmed_mfe_pct": 0.0, "close_mae_pct": 0.0, "follow_closes": 0}
    close_mfe = max(0.0, max(vals))
    close_mae = abs(min(0.0, min(vals)))
    # Confirmed MFE: do not treat a single close or wick as a stable expansion.
    confirmation_floor = max(0.10, close_mfe * 0.55)
    favorable_closes = sum(1 for v in vals if v >= confirmation_floor)
    if favorable_closes >= 2:
        confirmed = close_mfe
    elif close_mfe >= 0.35 and vals[-1] >= close_mfe * 0.55:
        confirmed = close_mfe * 0.75
    else:
        confirmed = min(close_mfe, close_mfe * 0.45)
    return {
        "close_mfe_pct": round(close_mfe, 3),
        "confirmed_mfe_pct": round(max(0.0, confirmed), 3),
        "close_mae_pct": round(close_mae, 3),
        "follow_closes": len(vals),
    }


def _phase_label_name(phase):
    return {
        "ENTRY_VALIDATION": "валідація входу",
        "EXPANSION": "розвиток імпульсу",
        "PROFIT_PROTECTION": "захист прибутку",
        "CONTINUATION_OR_EXIT": "продовження / вихід",
    }.get(str(phase or ""), "супровід")


def analyze_trade_phase(trade, context, current_pct, best_pct, giveback,
                        high_since_open=None, low_since_open=None,
                        entry_state=None, trade_validation=None,
                        support_votes=0.0, opposite_votes=0.0,
                        action="HOLD", market_regime=None,
                        tf3_against=False, structure_against=False, ict_against=False,
                        flow_against=False, cvd_against=False, liquidity_against=False,
                        news_against=False, confirmed_ict_reversal=False,
                        closed=False, exit_reason_code=""):
    """MFE/MAE Intelligence: professional phase model for open trade supervision.

    It does not replace the stop. It explains and soft-gates decisions by phase:
    1) ENTRY_VALIDATION      — first 1–3 3M candles after entry;
    2) EXPANSION             — price should start moving in our direction;
    3) PROFIT_PROTECTION     — meaningful MFE exists; avoid giving it back;
    4) CONTINUATION_OR_EXIT  — decide whether the setup still deserves TP2/TP3.
    """
    side = getattr(trade, "side", "NEUTRAL")
    opp = opposite(side) if side in ["LONG", "SHORT"] else "NEUTRAL"
    price = safe_float((context or {}).get("price"), safe_float(getattr(trade, "entry", None), 0.0)) or 0.0
    entry = safe_float(getattr(trade, "entry", 0), 0.0) or 0.0
    stop_initial = safe_float(getattr(trade, "stop_initial", getattr(trade, "stop_current", 0)), 0.0) or 0.0
    tp1 = safe_float(getattr(trade, "tp1", 0), 0.0) or 0.0
    risk_distance_pct = abs(pct(stop_initial, entry)) if entry and stop_initial else 0.65
    risk_distance_pct = max(0.10, risk_distance_pct)
    tp1_distance_pct = abs(pct(tp1, entry)) if entry and tp1 else 0.0
    elapsed_min = _trade_open_elapsed_minutes(trade)
    close_stats = _close_mfe_mae_after_open(trade, context, price)
    close_mfe = safe_float(close_stats.get("close_mfe_pct"), 0.0) or 0.0
    confirmed_mfe = safe_float(close_stats.get("confirmed_mfe_pct"), 0.0) or 0.0
    close_mae = safe_float(close_stats.get("close_mae_pct"), 0.0) or 0.0

    if side == "LONG":
        adverse_extreme = safe_float(low_since_open, min(price, entry))
        wick_mae = max(0.0, (entry - adverse_extreme) / entry * 100.0) if entry else close_mae
    elif side == "SHORT":
        adverse_extreme = safe_float(high_since_open, max(price, entry))
        wick_mae = max(0.0, (adverse_extreme - entry) / entry * 100.0) if entry else close_mae
    else:
        wick_mae = close_mae
    mae_pct = round(max(close_mae, wick_mae), 3)
    mae_risk_used = round(mae_pct / risk_distance_pct, 3) if risk_distance_pct else 0.0

    best_pct = safe_float(best_pct, 0.0) or 0.0
    current_pct = safe_float(current_pct, 0.0) or 0.0
    giveback = max(0.0, safe_float(giveback, best_pct - current_pct) or 0.0)
    giveback_ratio = giveback / best_pct if best_pct > 0.10 else 0.0
    regime = str(market_regime or (trade_mode_profile(context or {}, side).get("regime") if side in ["LONG", "SHORT"] else "NORMAL") or "NORMAL").upper()

    phase = "ENTRY_VALIDATION"
    if getattr(trade, "tp1_hit", False) or (tp1_distance_pct and best_pct >= tp1_distance_pct * 0.85):
        phase = "CONTINUATION_OR_EXIT"
    elif best_pct >= max(0.60, risk_distance_pct * 0.70) or confirmed_mfe >= max(0.45, risk_distance_pct * 0.50):
        phase = "PROFIT_PROTECTION"
    elif elapsed_min > 12 or close_stats.get("follow_closes", 0) >= 4:
        phase = "EXPANSION"

    hard_against = sum([bool(structure_against), bool(ict_against), bool(confirmed_ict_reversal)])
    soft_against = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(liquidity_against), bool(news_against)])
    support_ok = bool(support_votes >= max(1.8, opposite_votes) or ((context or {}).get("tf15") or {}).get("bias") == side)
    entry_state_name = str((entry_state or {}).get("state") or "")
    validation_severity = str((trade_validation or {}).get("severity") or "")

    # Exit permission: never allow one weak metric to close by itself.
    exit_permission = False
    if confirmed_ict_reversal or hard_against >= 2:
        exit_permission = True
    elif phase == "ENTRY_VALIDATION":
        exit_permission = bool(mae_risk_used >= 0.70 and (hard_against >= 1 or soft_against >= 2))
    elif phase == "EXPANSION":
        exit_permission = bool(
            (entry_state_name == "BROKEN" and (hard_against >= 1 or soft_against >= 2))
            or (validation_severity == "BAD" and current_pct <= -0.18 and soft_against >= 2)
            or (best_pct < 0.18 and elapsed_min >= 30 and opposite_votes >= 2.2 and support_votes <= 1.2)
        )
    elif phase == "PROFIT_PROTECTION":
        # Giveback is actionable only when plus was real enough and weakness is confirmed.
        allowed = 0.72 if regime in ["TREND", "NEWS_IMPULSE"] else 0.62
        exit_permission = bool(giveback_ratio >= allowed and (hard_against >= 1 or soft_against >= 3) and not support_ok)
    elif phase == "CONTINUATION_OR_EXIT":
        allowed = 0.62 if regime in ["TREND", "NEWS_IMPULSE"] else 0.52
        exit_permission = bool(
            confirmed_ict_reversal
            or (giveback_ratio >= allowed and (hard_against >= 1 or soft_against >= 2) and not support_ok)
            or (entry_state_name == "BROKEN" and current_pct <= 0.18 and hard_against >= 1)
        )
    if str(action or "").startswith("TP") or str(action or "") == "STOP":
        exit_permission = True

    # Human title / status.
    action_s = str(action or "")
    title = f"🟡 СУПРОВІД {side} — ПОТРІБНЕ ПРОДОВЖЕННЯ"
    status = "NEEDS_CONTINUATION"
    if closed or action_s in ["STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK", "EXIT_ENTRY_POINT_BROKEN", "EXIT_AFTER_TP1_GIVEBACK", "EXIT"]:
        status = "EDGE_LOST"
        if action_s.startswith("TP"):
            title = f"🔵 {side} ЗАКРИТО — ЦІЛЬ ВИКОНАНА"
            status = "TARGET_DONE"
        elif action_s == "STOP":
            title = f"🔴 {side} ЗАКРИТО — СТОП СПРАЦЮВАВ"
        elif best_pct >= 0.35 and current_pct >= -0.05:
            title = f"🔴 {side} ЗАКРИТО — ПРОДОВЖЕННЯ НЕ ПІДТВЕРДИЛОСЬ"
        else:
            title = f"🔴 {side} ЗАКРИТО — ПЕРЕВАГА ВТРАЧЕНА"
    elif action_s.startswith("TP"):
        title = f"🔵 СУПРОВІД {side} — ЦІЛЬ ВЗЯТО, ВЕСТИ ЗАЛИШОК"
        status = "TARGET_PROTECTION"
    elif phase == "ENTRY_VALIDATION":
        if current_pct >= 0.12 and not (hard_against or soft_against >= 2):
            title = f"🟢 СУПРОВІД {side} — ВХІД ПІДТВЕРДЖУЄТЬСЯ"
            status = "ENTRY_VALIDATING_POSITIVE"
        else:
            title = f"🟡 СУПРОВІД {side} — ЙДЕ ПЕРЕВІРКА ВХОДУ"
            status = "ENTRY_VALIDATION"
    elif phase == "EXPANSION":
        if current_pct > 0 and (support_votes >= opposite_votes or confirmed_mfe >= 0.25) and not hard_against:
            title = f"🟢 СУПРОВІД {side} — ІМПУЛЬС ПІДТВЕРДЖУЄТЬСЯ"
            status = "EXPANSION_CONFIRMED"
        elif entry_state_name in ["WEAK", "BROKEN"] or soft_against >= 2:
            title = f"🟠 СУПРОВІД {side} — СЕТАП СЛАБШАЄ"
            status = "SETUP_WEAKENING"
        else:
            title = f"🟡 СУПРОВІД {side} — ПОТРІБНЕ ПРОДОВЖЕННЯ"
            status = "NEEDS_CONTINUATION"
    elif phase == "PROFIT_PROTECTION":
        if giveback_ratio >= 0.55 or action_s in ["PROTECT", "PROTECT_OR_EXIT", "EXIT_WARNING"]:
            title = f"🟠 СУПРОВІД {side} — ПРИБУТОК СЛАБШАЄ"
            status = "PROFIT_WEAKENING"
        else:
            title = f"🟢 СУПРОВІД {side} — ПРИБУТОК ПІД КОНТРОЛЕМ"
            status = "PROFIT_CONTROLLED"
    elif phase == "CONTINUATION_OR_EXIT":
        if exit_permission or action_s in ["PROTECT_OR_EXIT", "EXIT_WARNING"]:
            title = f"🟠 СУПРОВІД {side} — ПЕРЕВАГА СЛАБШАЄ"
            status = "EDGE_WEAKENING"
        else:
            title = f"🟢 СУПРОВІД {side} — ПРОДОВЖЕННЯ ЩЕ АКТУАЛЬНЕ"
            status = "CONTINUATION_VALID"

    # One concise reason for Telegram. Full diagnostics stay in the journal.
    if closed or action_s in ["STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK", "EXIT_ENTRY_POINT_BROKEN", "EXIT_AFTER_TP1_GIVEBACK", "EXIT"]:
        if action_s == "STOP":
            reason = "стопова зона була зачеплена; сценарій закрито за планом."
        elif best_pct >= 0.35 and current_pct >= -0.05:
            reason = f"угода давала +{round(best_pct, 3)}% MFE, але продовження {side} не підтвердилось; вихід біля входу замість очікування дальнього стопа."
        else:
            reason = f"перевага {side} втрачена: структура/імпульс не дали продовження, ризик утримання став вищий за потенціал."
    elif phase == "ENTRY_VALIDATION":
        reason = f"перші 1–3 свічки після входу ще перевіряють точку; MFE {round(best_pct, 3)}%, MAE {round(mae_pct, 3)}%."
        if hard_against or soft_against >= 2:
            reason += " Є ранні попередження, але без повного зламу це ще не самостійна причина для виходу."
    elif phase == "EXPANSION":
        if status == "EXPANSION_CONFIRMED":
            reason = "ціна утримує напрям після входу; 3M закриття підтверджують рух, структура ще не зламана."
        elif status == "SETUP_WEAKENING":
            reason = f"після входу немає достатнього розвитку: MFE {round(best_pct, 3)}%, ціна повертається до входу, частина підтверджень вже проти."
        else:
            reason = f"угода ще не дала достатнього розвитку до TP1; потрібне продовження імпульсу без зламу 3M/ICT."
    elif phase == "PROFIT_PROTECTION":
        if status == "PROFIT_WEAKENING":
            reason = f"угода вже давала +{round(best_pct, 3)}% MFE, зараз {round(current_pct, 3)}%; віддано {round(giveback_ratio*100, 1)}% руху."
        else:
            reason = f"MFE вже змістовний (+{round(best_pct, 3)}%), але ціна ще утримує робочу зону; без confirmed-розвороту даємо простір."
    else:
        if status == "CONTINUATION_VALID":
            reason = "після першого руху структура/фон ще не зламали сценарій; є підстава вести залишок до наступної цілі."
        else:
            reason = f"після MFE +{round(best_pct, 3)}% перевага слабшає: потрібне відновлення структури, інакше краще не віддавати рух назад."

    return {
        "phase": phase,
        "phase_label": _phase_label_name(phase),
        "status": status,
        "telegram_title": title,
        "telegram_reason": reason,
        "exit_permission": bool(exit_permission),
        "elapsed_min": round(elapsed_min, 1),
        "current_pct": round(current_pct, 3),
        "best_pct": round(best_pct, 3),
        "close_mfe_pct": round(close_mfe, 3),
        "confirmed_mfe_pct": round(confirmed_mfe, 3),
        "mae_pct": round(mae_pct, 3),
        "mae_risk_used": mae_risk_used,
        "giveback_pct": round(giveback, 3),
        "giveback_ratio": round(giveback_ratio, 3),
        "support_votes": round(float(support_votes or 0.0), 2),
        "opposite_votes": round(float(opposite_votes or 0.0), 2),
        "hard_against": int(hard_against),
        "soft_against": int(soft_against),
    }

def manage_active_trade(trade, context):
    price = context["price"]
    side = trade.side
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    structure = context["structure"]
    ict = context.get("ict") or {}
    flow = context["flow"]
    mode_profile = trade_mode_profile(context, side)
    market_regime = mode_profile.get("regime", "NORMAL")

    high_since_open, low_since_open = _trade_extremes_since_open(trade, context)
    high_since_stop, low_since_stop = _trade_extremes_since_stop_update(trade, context)
    trade.best_price = best_trade_price(side, trade, price, high_since_open, low_since_open)
    current_pct = signed_pct(side, trade.entry, price)
    best_pct = signed_pct(side, trade.entry, trade.best_price)
    giveback = max(0, best_pct - current_pct)
    giveback_ratio = giveback / best_pct if best_pct > 0 else 0

    notes = []
    if market_regime in ["RANGE", "NEWS_IMPULSE", "REVERSAL"]:
        notes.append(f"режим: {regime_label(market_regime)} — прибуток захищати швидше")
    elif market_regime == "TREND":
        notes.append("режим: сильний тренд — можна тримати довше, але MFE не віддавати")
    action = "HOLD"
    title = f"СУПРОВІД {side}"
    recommendation = "утримувати, поки 3m/15m не ламаються"

    if trade_hit_stop_by_extreme(side, price, trade.stop_current, high_since_stop, low_since_stop):
        trade.status = "CLOSED"
        trade.last_action = "STOP"
        stop_exit_pct = signed_pct(side, trade.entry, trade.stop_current)
        return {
            "closed": True,
            "action": "STOP",
            "title": f"УГОДУ {side} ЗАКРИТО — STOP",
            "recommendation": "стоп/зона зламу пробита, сценарій закрито",
            "current_pct": stop_exit_pct,
            "market_price_pct": current_pct,
            "best_pct": best_pct,
            "exit_price": round_price(trade.stop_current),
            "notes": [f"стоп {round_price(trade.stop_current)} зачеплено по high/low 3M після його встановлення; ринкова ціна {round_price(price)}"],
        }

    if trade_hit_level_by_extreme(side, price, trade.tp3, high_since_open, low_since_open):
        trade.tp3_hit = True
        trade.status = "CLOSED"
        trade.last_action = "TP3"
        tp3_pct = signed_pct(side, trade.entry, trade.tp3)
        return {
            "closed": True,
            "action": "TP3",
            "exit_reason_code": "TP3_BY_HIGH_LOW",
            "exit_price": round_price(trade.tp3),
            "title": f"УГОДУ {side} ЗАКРИТО — TP3",
            "recommendation": "основна ціль виконана, сценарій закрито",
            "current_pct": tp3_pct,
            "best_pct": max(best_pct, tp3_pct),
            "notes": [_tp_hit_note(side, "TP3", trade.tp3, high_since_open, low_since_open, price)],
        }

    recommended_stop = None
    recommended_stop_reason = ""

    if trade_hit_level_by_extreme(side, price, trade.tp2, high_since_open, low_since_open):
        trade.tp2_hit = True
        trade.tp1_hit = True
        action = "TP2_PROTECT"
        title = f"{side} — TP2 ВЗЯТО, НОВИЙ СТОП ЗАФІКСОВАНО"
        recommendation = "TP2 взято: зафіксувати ще частину; новий ICT/SMC стоп рахується один раз і далі не рухається до TP3/виходу"
        if not trade.tp2_stop_locked:
            recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP2")
            if side == "LONG":
                trade.stop_current = max(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
            else:
                trade.stop_current = min(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
            trade.tp2_locked_stop = float(trade.stop_current)
            trade.tp2_stop_locked = True
            notes.append(_tp_hit_note(side, "TP2", trade.tp2, high_since_open, low_since_open, price))
            notes.append(f"зафіксувати стоп до TP3: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)
        else:
            recommended_stop = trade.tp2_locked_stop or trade.stop_current
            recommended_stop_reason = "TP2-стоп уже зафіксований; до TP3 не перераховувати на кожній свічці"
            trade.stop_current = float(recommended_stop)
            notes.append(f"стоп вже зафіксовано до TP3: {round_price(trade.stop_current)}")
            notes.append(recommended_stop_reason)
    elif trade_hit_level_by_extreme(side, price, trade.tp1, high_since_open, low_since_open):
        trade.tp1_hit = True
        action = "TP1_PROTECT"
        title = f"{side} — TP1 ВЗЯТО, СТОП ЗАФІКСОВАНО ДО TP2"
        recommendation = "TP1 взято: частково фіксувати; один раз переставити стоп у зону між входом і TP1 з урахуванням ICT/SMC, далі не рухати до TP2"
        if not trade.tp1_stop_locked:
            recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
            if side == "LONG":
                trade.stop_current = max(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
            else:
                trade.stop_current = min(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
            trade.tp1_locked_stop = float(trade.stop_current)
            trade.tp1_stop_locked = True
            notes.append(_tp_hit_note(side, "TP1", trade.tp1, high_since_open, low_since_open, price))
            notes.append(f"зафіксувати стоп до TP2: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)
        else:
            recommended_stop = trade.tp1_locked_stop or trade.stop_current
            recommended_stop_reason = "TP1-стоп уже зафіксований; до TP2 не перераховувати на кожній свічці"
            trade.stop_current = float(recommended_stop)
            notes.append(f"стоп вже зафіксовано до TP2: {round_price(trade.stop_current)}")
            notes.append(recommended_stop_reason)

    # Weighted management pressure for intraday trades.
    # 3m/15m/structure matter most. 4H and clusters are only small background hints.
    opposite_votes = 0.0
    support_votes = 0.0
    weighted_blocks = [
        (tf3, 1.20),
        (tf15, 1.00),
        (structure, 1.00),
        (ict, 0.90),
        (context.get("liquidity") or {}, 0.75),
        (context.get("news") or {}, 0.35),
        (context.get("cvd") or {}, 0.80),
        (context.get("derivatives") or {}, 0.55),
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
    post_tp1 = bool(trade.tp1_hit)
    profile_be = safe_float(mode_profile.get("be_trigger"), 0.65) or 0.65
    profile_giveback = safe_float(mode_profile.get("giveback"), 0.55) or 0.55
    lost_after_profit = best_pct >= max(0.35, profile_be * 0.75) and giveback_ratio >= profile_giveback and current_pct <= (0.22 if market_regime in ["RANGE", "NEWS_IMPULSE"] else 0.18)
    tp1_giveback_to_entry = post_tp1 and best_pct >= max(0.55, profile_be) and giveback_ratio >= min(0.60, profile_giveback + 0.08) and current_pct <= 0.22

    # Intraday early-exit logic.
    # For trades that should last only a few hours, do not wait for the formal stop
    # if 3m has already broken against the position and order-flow/CVD confirms it.
    tf3_against = tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 42
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10
    cvd_block = context.get("cvd") or {}
    cvd_against = cvd_block.get("bias") == opposite(side) and abs(int(cvd_block.get("score", 0) or 0)) >= 10
    structure_against = structure.get("bias") == opposite(side)
    ict_against = ict.get("bias") == opposite(side) and abs(int(ict.get("score", 0) or 0)) >= 16
    liquidity_block = context.get("liquidity") or {}
    liquidity_against = liquidity_block.get("bias") == opposite(side) and abs(int(liquidity_block.get("score", 0) or 0)) >= 12
    news_block = context.get("news") or {}
    news_against = news_block.get("bias") == opposite(side) and abs(int(news_block.get("score", 0) or 0)) >= 18
    confirmed_ict_reversal, ict_reversal_components = _has_confirmed_ict_reversal(side, context)
    real_structure_break = bool(structure_against or ict_against or confirmed_ict_reversal)
    soft_warning_only = bool((cvd_against or flow_against or news_against or liquidity_against) and not real_structure_break)
    countertrend_validation = countertrend_entry_validation(
        trade, context, current_pct, best_pct, giveback,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    trade_validation = trade_entry_validation_snapshot(
        trade, context, current_pct, best_pct, giveback,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    entry_state = entry_point_state_snapshot(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        trade_validation=trade_validation,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    if entry_state:
        if entry_state.get("fail_now") and not trade.tp1_hit:
            trade.entry_fail_streak = int(getattr(trade, "entry_fail_streak", 0) or 0) + 1
        elif entry_state.get("state") in ["STRONG", "WORKING"]:
            trade.entry_fail_streak = 0
        if entry_state.get("recovery_mode") == "RECHECK_AT_ENTRY":
            trade.entry_recovery_checks = int(getattr(trade, "entry_recovery_checks", 0) or 0) + 1
        trade.entry_integrity_score = int(entry_state.get("score") or 50)
        trade.entry_last_state = str(entry_state.get("label") or "")
    exhausted_trade, exhausted_trade_reason = detect_exhausted_move(side, context)

    phase_snapshot = analyze_trade_phase(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        entry_state=entry_state,
        trade_validation=trade_validation,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
        action=action,
        market_regime=market_regime,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
    )

    # Universal validation of the opened idea. This does not delay entries.
    # It changes the supervision wording through "Стан точки входу", without
    # adding a duplicated generic trade-risk block to Telegram.
    if entry_state and entry_state.get("state") in ["BROKEN", "WEAK"] and action == "HOLD" and not trade.tp1_hit:
        action = "EXIT_WARNING" if entry_state.get("state") == "BROKEN" else "PROTECT_OR_EXIT"
        title = f"{side} — ТОЧКА ВХОДУ ЗЛАМАНА" if entry_state.get("state") == "BROKEN" else f"{side} — ТОЧКА ВХОДУ СЛАБШАЄ"
        recommendation = entry_state.get("advice") or "угода слабшає; не чекати дальній стоп без відновлення ICT/структури"
        notes.append("стан точки входу: " + str(entry_state.get("label")))

    # If the opened idea fails several times in a row, the entry point is no
    # longer valid. Exit near entry / before full stop, but do not close a trade
    # that has already reached TP1 or is still clearly protected by ICT/structure.
    if entry_state and not trade.tp1_hit:
        should_close_by_integrity = bool(
            entry_state.get("auto_exit")
            or (
                int(getattr(trade, "entry_fail_streak", 0) or 0) >= 3
                and current_pct <= 0.18
                and phase_snapshot.get("exit_permission")
            )
        )
        if should_close_by_integrity:
            trade.status = "CLOSED"
            trade.last_action = "EXIT_ENTRY_POINT_BROKEN"
            return {
                "closed": True,
                "action": "EXIT_ENTRY_POINT_BROKEN",
                "exit_reason_code": "ENTRY_POINT_INTEGRITY_BROKEN",
                "exit_quality": "ENTRY_POINT",
                "title": f"{side} ЗАКРИТИ — ТОЧКА ВХОДУ ЗЛАМАНА",
                "recommendation": entry_state.get("advice") or "точка входу не відпрацювала; краще вийти раніше повного стопа",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "notes": [entry_state.get("reason", "точка входу втратила актуальність"), f"слабких перевірок підряд: {getattr(trade, 'entry_fail_streak', 0)}"],
                "entry_state": entry_state,
                "trade_phase": analyze_trade_phase(
                    trade, context, current_pct, best_pct, giveback,
                    high_since_open=high_since_open, low_since_open=low_since_open,
                    entry_state=entry_state, trade_validation=trade_validation,
                    support_votes=support_votes, opposite_votes=opposite_votes,
                    action="EXIT_ENTRY_POINT_BROKEN", market_regime=market_regime,
                    tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
                    flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
                    news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
                    closed=True, exit_reason_code="ENTRY_POINT_INTEGRITY_BROKEN",
                ),
            }

    # Smart post-TP1 supervision. This does not close by itself; it labels the
    # trade correctly and prevents panic exits when TP1 is done but ICT/15M is
    # still valid. Closing after TP1 still requires ICT/SMC reversal, deep MFE
    # giveback, stop, or a broad confirmed failure.
    post_tp1_trend_valid = bool(
        post_tp1
        and (tf15.get("bias") == side or context.get("bias") == side or support_votes >= 2.6)
        and not (structure_against or ict_against)
        and not confirmed_ict_reversal
    )
    post_tp1_warning = bool(
        post_tp1
        and (tf3_against or flow_against or cvd_against or news_against)
        and not (structure_against or ict_against or confirmed_ict_reversal)
    )

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - trade.stop_current) / trade.entry * 100) if trade.entry else 99.0
    else:
        stop_distance_pct = max(0.0, (trade.stop_current - price) / trade.entry * 100) if trade.entry else 99.0

    # MFE protection before formal TP1: do not allow a good intraday move
    # to come back to zero just because TP1 is still far away.
    # Use confirmed/close MFE for pre-TP1 stop tightening so one wick does not
    # over-tighten the trade. Big wick MFE still matters, but only after the
    # candle closes/follow-through validate it.
    effective_mfe_for_lock = best_pct
    if not trade.tp1_hit:
        close_mfe_for_lock = safe_float(phase_snapshot.get("close_mfe_pct"), 0.0) or 0.0
        confirmed_mfe_for_lock = safe_float(phase_snapshot.get("confirmed_mfe_pct"), 0.0) or 0.0
        if best_pct >= 0.45 and confirmed_mfe_for_lock < best_pct * 0.45:
            effective_mfe_for_lock = max(confirmed_mfe_for_lock, close_mfe_for_lock, min(best_pct, 0.65))
    mfe_stop, mfe_stop_reason = _profit_lock_stop_level(side, trade.entry, price, effective_mfe_for_lock, current_pct, mode_profile)
    if action == "HOLD" and mfe_stop is not None:
        if _apply_more_protective_stop(trade, side, mfe_stop):
            action = "PROTECT"
            title = f"{side} — ПРИБУТОК ЗАХИЩЕНО"
            recommendation = "ціна вже давала хороший плюс: стоп підтягнуто, щоб не віддати угоду назад"
            recommended_stop = trade.stop_current
            recommended_stop_reason = mfe_stop_reason
            notes.append(f"MFE-захист: {mfe_stop_reason}")
            notes.append(f"новий стоп: {round_price(trade.stop_current)}")

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - trade.stop_current) / trade.entry * 100) if trade.entry else 99.0
    else:
        stop_distance_pct = max(0.0, (trade.stop_current - price) / trade.entry * 100) if trade.entry else 99.0

    near_stop = stop_distance_pct <= 0.30
    clearly_losing = current_pct <= -0.30

    if action == "HOLD" and post_tp1_trend_valid:
        action = "HOLD_TO_TP2"
        title = f"{side} — TP1 ВЗЯТО, ТРЕНД ЩЕ ЖИВИЙ"
        recommendation = "TP1 виконано: якщо ICT/15M структура не зламана, не закривати залишок через шум; тримати до TP2 зі зафіксованим стопом"
        notes.append("після TP1 структура/15M ще підтримують угоду")
    elif action == "HOLD" and post_tp1_warning:
        action = "PROTECT"
        title = f"{side} — TP1 ВЗЯТО, Є ЛОКАЛЬНИЙ ВІДКАТ"
        recommendation = "TP1 виконано: є локальні попередження, але без ICT/SMC зламу це не команда закривати; стоп має бути зафіксований"
        notes.append("після TP1 є CVD/flow/3M шум, але ICT-розворот не підтверджений")

    if tf3_against and (flow_against or cvd_against) and (near_stop or clearly_losing):
        min_capture_pct = _mfe_exit_floor(best_pct, market_regime)
        trend_soft_break = (
            market_regime == "TREND"
            and current_pct > 0
            and best_pct >= profile_be
            and current_pct >= min_capture_pct
            and not (structure_against or ict_against)
        )
        if trend_soft_break:
            action = "PROTECT"
            title = f"{side} — ЛОКАЛЬНИЙ ЗЛАМ, АЛЕ ТРЕНД ЩЕ ЖИВИЙ"
            recommendation = "3M/потік дають відкат проти, але 15M/ICT структура ще не зламана: стоп у прибуток і дати тренду шанс"
            protect_stop, protect_reason = _profit_lock_stop_level(side, trade.entry, price, best_pct, current_pct, mode_profile)
            if protect_stop is not None and _apply_more_protective_stop(trade, side, protect_stop):
                recommended_stop = trade.stop_current
                recommended_stop_reason = protect_reason
                notes.append(f"новий захисний стоп: {round_price(trade.stop_current)}")
            notes.append(f"MFE було +{round(best_pct, 3)}%, захоплено {round((current_pct / best_pct * 100), 1) if best_pct > 0 else 0}%")
        else:
            # Local 3M + flow/CVD noise must not close a trade by itself.
            # Hard exit requires ICT/structure break, deep loss, or stop danger
            # after the trade failed to develop. Otherwise protect/hold.
            hard_local_exit = bool(
                real_structure_break
                or (clearly_losing and near_stop)
                or (near_stop and best_pct < 0.18 and (tf3_against or cvd_against or flow_against))
            )
            if hard_local_exit:
                trade.status = "CLOSED"
                trade.last_action = "EXIT_LOCAL_BREAK"
                reasons = ["3M зламався проти позиції"]
                if real_structure_break:
                    reasons.append("ICT/SMC структура підтвердила злам")
                if flow_against:
                    reasons.append("потік проти")
                if cvd_against:
                    reasons.append("CVD проти")
                if near_stop:
                    reasons.append("ціна близько до стопу")
                if best_pct > 0.1:
                    reasons.append(f"MFE було +{round(best_pct, 3)}%, захоплено мало")
                risk_snapshot = active_trade_risk_snapshot(
                    trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, "EXIT_WARNING"
                )
                return {
                    "closed": True,
                    "action": "EXIT_LOCAL_BREAK",
                    "exit_reason_code": "STRUCTURE_OR_STOP_LOCAL_BREAK",
                    "exit_quality": "HARD",
                    "title": f"{side} ЗАКРИТИ — ЛОКАЛЬНИЙ ЗЛАМ",
                    "recommendation": "закрити біля поточної / не чекати дальній стоп",
                    "current_pct": current_pct,
                    "best_pct": best_pct,
                    "notes": reasons[:4],
                    "trade_risk": risk_snapshot.get("trade_risk"),
                    "trade_risk_reasons": risk_snapshot.get("trade_risk_reasons"),
                    "reversal_side": risk_snapshot.get("reversal_side"),
                    "reversal_score": risk_snapshot.get("reversal_score"),
                    "reversal_label": risk_snapshot.get("reversal_label"),
                    "reversal_reasons": risk_snapshot.get("reversal_reasons"),
                    "exit_score": risk_snapshot.get("exit_score"),
                    "exit_signal": risk_snapshot.get("exit_signal"),
                }
            else:
                action = "PROTECT" if current_pct > 0 else "EXIT_WARNING"
                title = f"{side} — ЛОКАЛЬНИЙ ШУМ ПРОТИ, ICT ЩЕ НЕ ЗЛАМАНИЙ"
                recommendation = "не закривати лише через CVD/потік; чекати ICT/SMC злам або спрацювання стопу"
                notes.append("3M/потік проти, але ICT/структура ще не дали повний розворот")

    if tf3_against and (near_stop or clearly_losing):
        if real_structure_break or clearly_losing:
            action = "EXIT_WARNING"
            title = f"{side} ПІД ЗАГРОЗОЮ"
            recommendation = "3M вже проти позиції: захистити або закрити, не усереднювати"
            notes.append("3M зламався проти позиції")
            if near_stop:
                notes.append("ціна близько до стопу")
        else:
            action = "PROTECT"
            title = f"{side} — 3M ВІДКАТ, ICT ЩЕ НЕ ЗЛАМАНИЙ"
            recommendation = "3M шумить проти позиції, але ICT/15M структура ще не дали підтверджений розворот"
            notes.append("3M проти, але без ICT-зламу")

    if exhausted_trade and action == "HOLD" and (current_pct <= 0.15 or tf3_against or cvd_against or flow_against):
        action = "EXIT_WARNING"
        title = f"{side} ПІД ЗАГРОЗОЮ — РУХ ВІДІГРАНИЙ"
        recommendation = "рух уже відпрацював імпульс: захистити або закрити, не чекати дальній стоп без нового 3M/15M підтвердження"
        notes.append(exhausted_trade_reason)
        if tf3_against:
            notes.append("3M вже проти позиції")
        if cvd_against or flow_against:
            notes.append("CVD/потік проти супроводу")

    if tp1_giveback_to_entry:
        # After TP1, do not close just because soft layers disagree.
        # Close requires ICT/structure reversal or a broad confirmed failure.
        hard_reversal_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
        soft_warning_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
        warning_votes = hard_reversal_votes * 2 + soft_warning_votes
        if confirmed_ict_reversal or (hard_reversal_votes >= 1 and warning_votes >= 3) or opposite_votes >= 2.6:
            trade.status = "CLOSED"
            trade.last_action = "EXIT_AFTER_TP1_GIVEBACK"
            reasons = ["після TP1 ціна повертається майже до входу"]
            if tf3_against:
                reasons.append("3M вже проти")
            if structure_against or ict_against:
                reasons.append("ICT/SMC структура слабшає")
            if flow_against or cvd_against:
                reasons.append("потік/CVD проти")
            if liquidity_against or news_against:
                reasons.append("ліквідність/новини проти")
            return {
                "closed": True,
                "action": "EXIT_AFTER_TP1_GIVEBACK",
                "title": f"{side} ЗАКРИТИ — ПРИБУТОК НЕ ВІДДАВАТИ",
                "recommendation": "TP1 вже був, ціна майже повернулась до входу: краще закрити залишок або мінімум не тримати без стопу в плюсі",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": trade.stop_current,
                "recommended_stop_reason": "після TP1 не віддавати угоду назад до входу",
                "notes": reasons[:4],
            }
        elif action == "HOLD":
            action = "PROTECT"
            title = f"{side} — ПІСЛЯ TP1 ВІДКАТ, ICT НЕ ЗЛАМАНИЙ"
            recommendation = "після TP1 ціна відкотилася, але без ICT-розвороту це не команда закривати; тримати тільки зі зафіксованим стопом"
            if trade.tp1_stop_locked:
                recommended_stop = trade.tp1_locked_stop or trade.stop_current
                recommended_stop_reason = "TP1-стоп уже зафіксований; якщо ціна повертається до входу — аналізуємо закриття, а не підтягування стопу"
                trade.stop_current = float(recommended_stop)
            else:
                recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
                if side == "LONG":
                    trade.stop_current = max(trade.stop_current, recommended_stop)
                else:
                    trade.stop_current = min(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
                trade.tp1_locked_stop = float(trade.stop_current)
                trade.tp1_stop_locked = True
            notes.append(f"стоп для захисту: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)

    # Adaptive MFE Giveback Guard 2.0.
    # Protects real profit without killing normal trend retests:
    # - before TP1: mostly protect with stop, no panic close;
    # - after TP1 / after big MFE: close only after 2 confirmed warnings;
    # - hard ICT/SMC reversal can close immediately.
    effective_giveback = max(0.0, effective_mfe_for_lock - current_pct)
    effective_giveback_ratio = effective_giveback / effective_mfe_for_lock if effective_mfe_for_lock > 0 else 0.0
    mfe_guard = _adaptive_mfe_guard_snapshot(
        trade, context, market_regime, current_pct, effective_mfe_for_lock, effective_giveback, effective_giveback_ratio,
        post_tp1=post_tp1,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
    )
    if mfe_guard.get("active") and action in ["HOLD", "PROTECT", "HOLD_TO_TP2", "TP1_PROTECT"]:
        if mfe_guard.get("close"):
            trade.status = "CLOSED"
            trade.last_action = "EXIT_MFE_GIVEBACK"
            reasons = list(mfe_guard.get("reasons") or [])
            if tf3_against:
                reasons.append("3M вже проти")
            if flow_against or cvd_against:
                reasons.append("потік/CVD проти")
            return {
                "closed": True,
                "action": "EXIT_MFE_GIVEBACK",
                "exit_reason_code": "ADAPTIVE_MFE_GIVEBACK_2_CONFIRMED",
                "exit_quality": "CONFIRMED" if mfe_guard.get("streak", 0) >= 2 else "HARD_ICT_SMC",
                "title": f"{side} ЗАКРИТИ — ПРИБУТОК ВІДДАЄТЬСЯ",
                "recommendation": "адаптивний MFE Guard підтвердив, що рух у плюс віддається і є слабкість: краще закрити/зафіксувати, ніж чекати дальній TP або повний стоп",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": trade.stop_current,
                "recommended_stop_reason": "Adaptive MFE Giveback Guard 2.0",
                "notes": reasons[:5],
            }
        else:
            action = "PROTECT"
            title = f"{side} — MFE ВІДДАЄТЬСЯ, ПОТРІБЕН ЗАХИСТ"
            recommendation = "угода вже давала хороший плюс і частину руху віддала; це ще не обовʼязковий вихід, але стоп треба захистити"
            protect_stop, protect_reason = _profit_lock_stop_level(side, trade.entry, price, best_pct, max(current_pct, 0.20), mode_profile)
            if protect_stop is not None and _apply_more_protective_stop(trade, side, protect_stop):
                recommended_stop = trade.stop_current
                recommended_stop_reason = protect_reason or "Adaptive MFE Giveback Guard 2.0"
                notes.append(f"новий захисний стоп: {round_price(trade.stop_current)}")
            notes.extend(list(mfe_guard.get("reasons") or [])[:3])

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

    if post_tp1 and action in ["HOLD", "HOLD_TO_TP2"]:
        # Do NOT trail every 15 minutes after TP1. Keep the first TP1 stop
        # locked until TP2. This avoids getting stopped by normal BZU noise
        # right after the first take-profit.
        if trade.tp2_hit:
            if trade.tp2_stop_locked:
                recommended_stop = trade.tp2_locked_stop or trade.stop_current
                recommended_stop_reason = "TP2-стоп зафіксований; до TP3 не рухати без окремого exit-сигналу"
                trade.stop_current = float(recommended_stop)
        else:
            if trade.tp1_stop_locked:
                recommended_stop = trade.tp1_locked_stop or trade.stop_current
                recommended_stop_reason = "TP1-стоп зафіксований; до TP2 не рухати, супровід оцінює тільки утримувати чи закривати"
                trade.stop_current = float(recommended_stop)
            else:
                recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
                if side == "LONG":
                    trade.stop_current = max(trade.stop_current, recommended_stop)
                else:
                    trade.stop_current = min(trade.stop_current, recommended_stop)
                trade.stop_updated_at = iso_now()
                trade.tp1_locked_stop = float(trade.stop_current)
                trade.tp1_stop_locked = True
                action = "TP1_PROTECT"
                title = f"{side} — TP1 ВЗЯТО, СТОП ЗАФІКСОВАНО ДО TP2"
                recommendation = "TP1 взято: стоп зафіксовано один раз до TP2"
                notes.append(_tp_hit_note(side, "TP1", trade.tp1, high_since_open, low_since_open, price))
                notes.append(f"зафіксувати стоп до TP2: {round_price(trade.stop_current)}")
                if recommended_stop_reason:
                    notes.append(recommended_stop_reason)

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
    risk_snapshot = active_trade_risk_snapshot(
        trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, action
    )
    return {
        "closed": False,
        "action": action,
        "title": title,
        "recommendation": recommendation,
        "current_pct": current_pct,
        "best_pct": best_pct,
        "recommended_stop": round_price(trade.stop_current),
        "recommended_stop_reason": recommended_stop_reason,
        "notes": notes,
        "trade_risk": risk_snapshot.get("trade_risk"),
        "trade_risk_score": risk_snapshot.get("trade_risk_score"),
        "trade_risk_reasons": risk_snapshot.get("trade_risk_reasons"),
        "reversal_side": risk_snapshot.get("reversal_side"),
        "reversal_score": risk_snapshot.get("reversal_score"),
        "reversal_label": risk_snapshot.get("reversal_label"),
        "reversal_reasons": risk_snapshot.get("reversal_reasons"),
        "exit_score": risk_snapshot.get("exit_score"),
        "exit_signal": risk_snapshot.get("exit_signal"),
        "countertrend_validation": None,
        "trade_validation": risk_snapshot.get("trade_validation"),
        "entry_state": entry_state,
        "trade_phase": analyze_trade_phase(
            trade, context, current_pct, best_pct, giveback,
            high_since_open=high_since_open, low_since_open=low_since_open,
            entry_state=entry_state, trade_validation=trade_validation,
            support_votes=support_votes, opposite_votes=opposite_votes,
            action=action, market_regime=market_regime,
            tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
            flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
            news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
        ),
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
        tp1_stop_locked=False,
        tp2_stop_locked=False,
        tp1_locked_stop=0.0,
        tp2_locked_stop=0.0,
        best_price=plan.entry,
        entry_integrity_score=100,
        entry_fail_streak=0,
        entry_recovery_checks=0,
        entry_last_state="СИЛЬНА",
        notes=(
            (["RISKY_ENTRY"] if setup.get("action") == "RISKY_ENTRY" else [])
            + (["COUNTERTREND_ENTRY"] if setup.get("countertrend_entry") else [])
            + ([setup.get("reason", "")])
        ),
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
    # Current price is refreshed immediately before the signal is built.
    line = f"<b>Ціна:</b> {_fmt_price(context.get('price'))}"
    if context.get("price_status") == "updated_before_signal" and context.get("price_before_refresh") is not None:
        line += f" <i>(оновлено з {_fmt_price(context.get('price_before_refresh'))})</i>"
    return line


def context_lines(context):
    # User requested to remove the detailed dashboard block from Telegram messages.
    # Keep all calculations inside the bot, but do not print 4H/1H/15M/3M/ICT/CVD/etc.
    return []

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



def _side_icon(side):
    return "🟢" if side == "LONG" else "🔴"


def _zone_line(zone):
    if not zone:
        return ""
    low = zone.get("low")
    high = zone.get("high")
    ztype = zone.get("type") or "зона"
    if low is None or high is None:
        return ""
    return f"{_fmt_price(low)}–{_fmt_price(high)} ({ztype})"


def planned_wait_text(context, setup):
    """Human-readable waiting plan for WATCH / WAIT_RETEST states.

    The goal is to avoid vague messages like "чекати LONG". The bot should
    tell the user what exactly confirms the idea and from what price the trade
    becomes active.
    """
    side = setup.get("side")
    plan = setup.get("plan")
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15"), (price or 90) * 0.006) or ((price or 90) * 0.006)
    if side not in ["LONG", "SHORT"] or not price or not plan:
        return ""

    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    pending = context.get("pending_ict_zones") or []
    same_zones = [z for z in pending if z.get("side") == side]
    best_zone = same_zones[0] if same_zones else None

    recent_high = safe_float(structure.get("recent_high"))
    recent_low = safe_float(structure.get("recent_low"))
    swing_high = safe_float(structure.get("swing_high"))
    swing_low = safe_float(structure.get("swing_low"))

    if side == "LONG":
        # Prefer a nearby reclaim/trigger, not a far 15m swing if it is too stretched.
        candidates = [price + atr15 * 0.16]
        for lvl in [recent_high, swing_high, safe_float(ict.get("equilibrium"))]:
            if lvl and lvl > price and (lvl - price) / price * 100 <= 1.15:
                candidates.append(lvl + atr15 * 0.04)
        trigger = min(candidates) if candidates else price + atr15 * 0.16
        entry_low = trigger
        entry_high = trigger + atr15 * 0.22
        retest_zone = _zone_line(best_zone)
        if not retest_zone:
            support = max(safe_float(getattr(plan, "stop", None), price - atr15), price - atr15 * 0.55)
            retest_zone = f"{_fmt_price(support)}–{_fmt_price(price)}"
        wait_items = [
            f"3M reclaim вище {_fmt_price(trigger)} і закриття/утримання над рівнем",
            "після reclaim — 3M higher low, без нового lower low",
            f"або ретест зони {retest_zone} з викупом",
        ]
        activation = f"якщо 3M закриється вище {_fmt_price(trigger)} або дасть retest→reclaim цієї зони"
        cancel = f"якщо 3M/15M закриється нижче {_fmt_price(plan.stop)} або CVD/потік різко стане проти LONG"
    else:
        candidates = [price - atr15 * 0.16]
        for lvl in [recent_low, swing_low, safe_float(ict.get("equilibrium"))]:
            if lvl and lvl < price and (price - lvl) / price * 100 <= 1.15:
                candidates.append(lvl - atr15 * 0.04)
        trigger = max(candidates) if candidates else price - atr15 * 0.16
        entry_high = trigger
        entry_low = trigger - atr15 * 0.22
        retest_zone = _zone_line(best_zone)
        if not retest_zone:
            resistance = min(safe_float(getattr(plan, "stop", None), price + atr15), price + atr15 * 0.55)
            retest_zone = f"{_fmt_price(price)}–{_fmt_price(resistance)}"
        wait_items = [
            f"3M rejection/пробій нижче {_fmt_price(trigger)} і утримання під рівнем",
            "після пробою — 3M lower high, без нового higher high",
            f"або ретест зони {retest_zone} з відбоєм вниз",
        ]
        activation = f"якщо 3M закриється нижче {_fmt_price(trigger)} або дасть retest→rejection цієї зони"
        cancel = f"якщо 3M/15M закриється вище {_fmt_price(plan.stop)} або CVD/потік різко стане проти SHORT"

    why = []
    if tf3.get("bias") == side:
        why.append("3M вже показує напрям")
    elif tf3.get("bias") == "NEUTRAL":
        why.append("3M ще без повного тригера")
    else:
        why.append("3M поки дає відкат — потрібен розворот назад")

    if ict.get("bias") == side:
        why.append("ICT контекст підтримує ідею")
    if tf15.get("bias") == side:
        why.append("15M підтримує напрям")
    elif tf15.get("bias") == "NEUTRAL":
        why.append("15M ще нейтральний, тому вхід тільки по 3M")
    if flow.get("bias") == side or cvd.get("bias") == side:
        why.append("потік/CVD не проти входу")

    lines = []
    if why:
        lines.append("<b>Чому готується:</b>")
        lines.extend([f"✅ {x}" for x in why[:4]])
        lines.append("")
    lines.append("<b>Що чекаємо:</b>")
    lines.extend([f"• {x}" for x in wait_items])
    lines.append("")
    # For WAIT/WATCH messages keep the alert short: activation is enough.
    # Full entry/stop/TP plan is shown only when the bot gives a real ENTRY/RISKY_ENTRY.
    lines.append("<b>Активація:</b>")
    lines.append(activation)
    return "\n".join(lines).strip()

def _compact_title(setup):
    action = setup.get("action")
    side = setup.get("side")
    if action == "ENTRY":
        return f"🟢 ВХІД {side}"
    if action == "RISKY_ENTRY":
        return f"🟠 РИЗИКОВАНИЙ ВХІД {side}"
    if action == "WAIT_RETEST":
        return f"🟡 НЕ ДОГАНЯТИ {side}"
    if action == "WATCH" and setup.get("exhausted_move") and side in ["LONG", "SHORT"]:
        return f"🟡 ЧЕКАТИ — {side} ВЖЕ ВІДІГРАНИЙ"
    if action == "WATCH" and side in ["LONG", "SHORT"]:
        return f"🟡 ЧЕКАТИ — {side} ГОТУЄТЬСЯ"
    if action == "NO_TRADE":
        return "⚪ " + str(setup.get("title") or "ЧЕКАТИ — ВХОДУ НЕМАЄ")
    return str(setup.get("title") or "СИГНАЛ")


def entry_type_text(context, setup):
    """Human-readable entry type for Telegram.

    This does not block or downgrade early entries. It only marks whether the
    signal goes with the higher-timeframe/flow context or against it, so the
    user can instantly see if the trade is trend-following or countertrend.
    """
    side = (setup or {}).get("side")
    action = (setup or {}).get("action")
    if side not in ["LONG", "SHORT"] or action not in ["ENTRY", "RISKY_ENTRY"]:
        return ""

    opposite_side = opposite(side)
    tf1h = (context.get("tf1h") or {}).get("bias", "NEUTRAL")
    tf15 = (context.get("tf15") or {}).get("bias", "NEUTRAL")
    tf4h = (context.get("tf4h") or {}).get("bias", "NEUTRAL")
    cvd = (context.get("cvd") or {}).get("bias", "NEUTRAL")
    flow = (context.get("flow") or {}).get("bias", "NEUTRAL")
    news = (context.get("news") or {}).get("bias", "NEUTRAL")

    against = []
    support = []

    if tf1h == opposite_side:
        against.append("1H проти")
    elif tf1h == side:
        support.append("1H за")

    if tf15 == opposite_side:
        against.append("15M проти")
    elif tf15 == side:
        support.append("15M за")

    if tf4h == opposite_side:
        against.append("4H проти")
    elif tf4h == side:
        support.append("4H за")

    if cvd == opposite_side:
        against.append("CVD проти")
    elif cvd == side:
        support.append("CVD за")

    if flow == opposite_side:
        against.append("потік проти")
    elif flow == side:
        support.append("потік за")

    if news == opposite_side:
        against.append("новини проти")
    elif news == side:
        support.append("новини за")

    if tf1h == opposite_side:
        label = f"⚠️ Контртрендовий {side}"
    elif tf1h == side or (tf15 == side and len(support) >= 2):
        label = f"✅ Трендовий {side}"
    elif len(against) >= 3:
        label = f"⚠️ Агресивний {side} проти фону"
    elif len(support) >= 2:
        label = f"✅ Локально трендовий {side}"
    else:
        label = f"⚪ Нейтральний {side}"

    if against:
        return f"<b>Тип:</b> {label} | ризики: {', '.join(against[:3])}"
    if support:
        return f"<b>Тип:</b> {label} | підтримка: {', '.join(support[:3])}"
    return f"<b>Тип:</b> {label}"


def build_new_setup_message(context, setup):
    plan = setup.get("plan")
    conflicts = _short_list(setup.get("conflicts"), 3)
    confirmations = _short_list(setup.get("confirmations"), 3)

    quality_label = "Якість" if setup.get("action") in ["ENTRY", "RISKY_ENTRY"] else "Готовність"
    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{_compact_title(setup)}</b>",
        "",
        price_line(context),
        f"<b>{quality_label}:</b> {setup['quality']}/100",
    ]

    entry_type = entry_type_text(context, setup)
    if entry_type:
        lines.append(entry_type)

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
        if setup.get("action") == "RISKY_ENTRY":
            lines.append("")
            lines.append("<b>Контроль:</b> якщо після входу 1–2 свічки 3M не підтвердять напрям або CVD/потік різко проти — очікувати EXIT WARNING.")
    else:
        reason_items = conflicts or _short_list([setup.get("reason")], 1)
        if reason_items:
            lines.append("")
            lines.append("<b>Причина:</b>")
            for x in reason_items:
                icon = "⚠️" if ("локаль" in x or "4h фон" in x or "потік локально" in x) else "❌"
                lines.append(f"{icon} {x}")
        # WAIT/ЧЕКАТИ alerts stay calm: no separate "Активація" block.
        # The dynamic reason already explains what is missing for entry.
        wait_text = ""
        if wait_text:
            lines.append("")
            lines.append(wait_text)
        elif setup.get("side") in ["LONG", "SHORT"] and plan and setup.get("show_wait_plan", True):
            lines.append("")
            lines.append("<b>Орієнтир:</b>")
            lines.append(plan_text(plan, multiline=True))

    lines.append("")
    lines.extend(context_lines(context))
    return "\n".join(lines).strip()


def build_follow_message(context, trade, result):
    current_pct = safe_float((result or {}).get("current_pct"), 0.0) or 0.0
    best_pct = safe_float((result or {}).get("best_pct"), 0.0) or 0.0
    giveback = max(0.0, best_pct - current_pct)
    high_since_open, low_since_open = _trade_extremes_since_open(trade, context)
    phase = (result or {}).get("trade_phase") or analyze_trade_phase(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        entry_state=(result or {}).get("entry_state"),
        trade_validation=(result or {}).get("trade_validation"),
        action=(result or {}).get("action"),
        closed=bool((result or {}).get("closed")),
        exit_reason_code=(result or {}).get("exit_reason_code"),
    )
    title = phase.get("telegram_title") or (result or {}).get("title") or f"СУПРОВІД {trade.side}"
    reason = phase.get("telegram_reason") or (result or {}).get("recommendation") or "супровід оновлено за поточним контекстом."
    mae_pct = safe_float(phase.get("mae_pct"), 0.0) or 0.0
    close_mfe = safe_float(phase.get("close_mfe_pct"), 0.0) or 0.0

    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{html.escape(str(title))}</b>",
        "",
        price_line(context),
        f"<b>Від входу:</b> {round(current_pct, 3)}% | <b>MFE:</b> {round(best_pct, 3)}% | <b>MAE:</b> {round(mae_pct, 3)}%",
    ]
    # Close/confirmed MFE is used by the engine internally, but not printed by
    # default to keep Telegram clean and fast to read.

    lines.append("")
    lines.append("<b>Причина:</b>")
    lines.append(html.escape(str(reason)))

    if result.get("reversal_label"):
        lines.append("")
        rev_side = result.get("reversal_side") or opposite(trade.side)
        lines.append(f"<b>Ризик розвороту в {rev_side}:</b> {result.get('reversal_label')} ({int(result.get('reversal_score') or 0)}%)")

    lines.extend([
        "",
        "<b>Позиція:</b>",
        f"Вхід {_fmt_price(trade.entry)} | Стоп {_fmt_price(trade.stop_current)}",
        f"TP1 {_fmt_price(trade.tp1)} | TP2 {_fmt_price(trade.tp2)} | TP3 {_fmt_price(trade.tp3)}",
        f"TP1 {'✅' if trade.tp1_hit else '—'} | TP2 {'✅' if trade.tp2_hit else '—'} | TP3 {'✅' if trade.tp3_hit else '—'}",
    ])

    # Show stop update only if it really changed from the initial stop; do not
    # repeat a "control stop" when it is identical to the entry stop.
    rec_stop = result.get("recommended_stop")
    if rec_stop is not None:
        rec_stop = safe_float(rec_stop, None)
        stop_initial = safe_float(getattr(trade, "stop_initial", None), None)
        if rec_stop is not None and stop_initial is not None and abs(rec_stop - stop_initial) > max(0.0001, abs(stop_initial) * 0.00005):
            lines.append(f"<b>Активний стоп:</b> {_fmt_price(rec_stop)}")

    return "\n".join(lines).strip()

def build_closed_trade_journal_item(trade, result, context):
    """Build a closed-trade journal row with MFE analytics.

    best_pct is kept for backward compatibility, but the same value is also
    stored as mfe_pct so later analysis can directly answer:
    - how much profit the trade had at its best moment;
    - how much of that MFE was captured at exit;
    - how much profit was given back before closing.
    """
    actual_pct = round(safe_float(result.get("current_pct"), 0.0) or 0.0, 3)
    mfe_pct = round(safe_float(result.get("best_pct"), 0.0) or 0.0, 3)
    mfe_captured_pct = round(actual_pct / mfe_pct * 100, 1) if mfe_pct > 0.1 else None
    mfe_giveback_pct = round(mfe_pct - actual_pct, 3) if mfe_pct > 0.1 else None

    return {
        "id": trade.id,
        "opened_at": trade.opened_at,
        "closed_at": iso_now(),
        "side": trade.side,
        "entry": trade.entry,
        "close_price": round_price(result.get("exit_price") or context["price"]),
        "market_close_price": round_price(context["price"]),
        "stop_initial": trade.stop_initial,
        "stop_final": round_price(trade.stop_current),
        "tp1": trade.tp1,
        "tp2": trade.tp2,
        "tp3": trade.tp3,
        "quality": trade.quality,
        "result_action": result["action"],
        "result_pct": actual_pct,
        "leveraged_pct": round(actual_pct * LEVERAGE, 2),
        "best_pct": mfe_pct,
        "mfe_pct": mfe_pct,
        "mfe_captured_pct": mfe_captured_pct,
        "mfe_giveback_pct": mfe_giveback_pct,
        "trade_phase": (result.get("trade_phase") or {}).get("phase"),
        "phase_status": (result.get("trade_phase") or {}).get("status"),
        "mae_pct": (result.get("trade_phase") or {}).get("mae_pct"),
        "confirmed_mfe_pct": (result.get("trade_phase") or {}).get("confirmed_mfe_pct"),
        "exit_reason_code": result.get("exit_reason_code") or _exit_reason_code(result, context, trade),
        "exit_quality": result.get("exit_quality") or _exit_quality_score(trade, result, context),
        "exit_score": result.get("exit_score"),
        "missed_continuation_check": "pending_next_runs",
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
    context = refresh_context_price(context)
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
            closed_mfe_pct = round(safe_float(result.get("best_pct"), 0.0) or 0.0, 3)
            closed_result_pct = round(safe_float(result.get("current_pct"), 0.0) or 0.0, 3)
            append_history(state, {
                "type": "TRADE_CLOSED",
                "side": active.side,
                "action": result["action"],
                "price": round_price(context["price"]),
                "result_pct": closed_result_pct,
                "mfe_pct": closed_mfe_pct,
                "mfe_captured_pct": round(closed_result_pct / closed_mfe_pct * 100, 1) if closed_mfe_pct > 0.1 else None,
                "mfe_giveback_pct": round(closed_mfe_pct - closed_result_pct, 3) if closed_mfe_pct > 0.1 else None,
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
