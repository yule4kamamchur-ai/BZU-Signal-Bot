import os
import re
import time
import json
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")

TRADINGVIEW_SYMBOLS = [
    ("BINANCE:BZUSDT.P", "crypto"),
    ("BINANCE:BZUSDT", "crypto"),
    ("TVC:UKOIL", "cfd"),
    ("NYMEX:BZ1!", "futures"),
]

MACRO_SYMBOLS = [
    ("DXY", "TVC:DXY", "cfd"),
    ("US10Y", "TVC:US10Y", "cfd"),
    ("VIX", "CBOE:VIX", "cfd"),
    ("SPX", "SP:SPX", "cfd"),
    ("NDX", "NASDAQ:NDX", "cfd"),
    ("BTC", "BINANCE:BTCUSDT", "crypto"),
    ("UKOIL", "TVC:UKOIL", "cfd"),
]

NEWS_LOOKBACK_HOURS = 2
EVENT_LOOKBACK_HOURS = 18
MAX_NEWS_SCORE = 45
MAX_EVENT_SCORE = 50
MAX_ITEMS_PER_FEED = 15
MIN_CONFIDENCE_TO_SEND = 55

STRONG_UP_MOVE_PERCENT = 1.2
VERY_STRONG_UP_MOVE_PERCENT = 1.8
STRONG_DOWN_MOVE_PERCENT = -1.2
VERY_STRONG_DOWN_MOVE_PERCENT = -1.8

# GDELT disabled: on GitHub Actions it often gives 429/timeout.
GDELT_QUERIES = []

GOOGLE_NEWS_QUERIES = [
    'site:reuters.com oil Brent crude OPEC EIA Iran sanctions Hormuz',
    'site:reuters.com/business/energy oil prices Brent crude',
    'site:reuters.com Fed Powell dollar yields oil market',
    'Brent crude oil Trump tariff sanctions OPEC EIA',
    'oil prices Brent crude breaking news today',
    'crude oil inventory EIA API OPEC',
    'Iran Russia Ukraine Hormuz oil sanctions',
]

EVENT_QUERIES = [
    'site:reuters.com EIA crude inventories oil stockpiles',
    'site:reuters.com OPEC meeting oil production cut increase',
    'site:reuters.com Iran US talks sanctions oil Hormuz',
    'Fed speech Powell today FOMC oil market',
    'CPI release today Fed inflation oil market',
    'NFP jobs report Fed oil market',
    'EIA crude oil inventories today API crude draw build',
    'OPEC meeting production cut increase crude oil',
    'Iran US talks sanctions oil Hormuz Trump',
]

OFFICIAL_FED_RSS_FEEDS = [
    ("Fed Monetary Policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", 1.25),
    ("Fed Powell", "https://www.federalreserve.gov/feeds/s_t_powell.xml", 1.15),
    ("Fed Speeches", "https://www.federalreserve.gov/feeds/speeches.xml", 0.75),
]

EIA_WPSR_URL = "https://www.eia.gov/petroleum/supply/weekly/index.php"
EIA_WPSR_SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
OPEC_PRESS_URL = "https://www.opec.org/press-releases.html"

MACRO_NEWS_QUERIES = [
    'Fed Powell rate cut inflation dollar yields stock market',
    'DXY dollar yields VIX stocks oil market today',
    'CPI FOMC NFP Fed speech market reaction',
    'S&P 500 Nasdaq VIX dollar risk on risk off today',
]

NEWS_SOURCES = [
    # EIA RSS is intentionally not used as a direct source on GitHub Actions,
    # because it often times out. EIA inventory impact is still covered through
    # Google News RSS queries: "EIA crude oil inventories today API crude draw build".
    {
        "name": "OilPrice",
        "url": "https://oilprice.com/rss/main",
        "type": "rss",
        "weight": 1.0,
    },
    {
        "name": "Oil & Gas Journal",
        "url": "https://www.ogj.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22general-interest%22%7D",
        "type": "rss",
        "weight": 1.0,
    },
    {
        "name": "Energy Intelligence",
        "url": "https://www.energyintel.com/rss-feed",
        "type": "html",
        "weight": 0.5,
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "type": "rss",
        "weight": 0.18,
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "type": "rss",
        "weight": 0.18,
    },
]

BULLISH_WORDS = [
    "inventory draw", "crude draw", "stockpiles fell", "stockpiles decline",
    "supply disruption", "supply risk", "supply tight", "opec cut", "opec+ cut",
    "sanctions", "hormuz", "middle east tension", "russia supply", "ukraine attack",
    "demand rises", "demand growth", "bullish", "rally", "surge", "higher",
    "jumps", "rebounds", "rate cut", "fed pause",
]

BEARISH_WORDS = [
    "inventory build", "crude build", "stockpiles rose", "stockpiles rise",
    "demand weak", "weak demand", "demand falls", "oversupply", "supply glut",
    "opec increase", "output hike", "ceasefire", "peace talks", "recession",
    "rate hike", "inflation rises", "bearish", "falls", "drops", "tumbles",
    "slides", "lower",
]

BREAKING_WORDS = [
    "trump", "white house", "president", "tariff", "sanctions", "iran",
    "russia", "ukraine", "war", "ceasefire", "hormuz", "opec", "opec+",
    "fed", "powell", "fomc", "cpi", "eia", "api", "inventory",
    "stockpiles", "breaking", "urgent",
]

HIGH_IMPACT_WORDS = [
    "eia", "api", "inventory", "stockpiles", "opec", "opec+", "hormuz",
    "iran", "russia", "ukraine", "sanctions", "fed", "fomc", "cpi",
    "powell", "inflation", "interest rate", "tariff", "trump",
]

BULLISH_GEO_WORDS = [
    "attack", "missile", "strike", "war", "sanctions", "hormuz", "escalation",
    "embargo", "supply disruption", "shutdown", "blocked",
]

BEARISH_SUPPLY_WORDS = [
    "ceasefire", "peace", "deal", "output increase", "supply increase",
    "inventory build", "stockpiles rose", "demand weak", "oversupply",
]

EVENT_HIGH_RISK_WORDS = [
    "fed", "powell", "fomc", "cpi", "nfp", "jobs report", "payrolls",
    "eia", "api", "inventories", "inventory", "stockpiles",
    "opec", "opec+", "iran", "us-iran", "sanctions", "hormuz", "trump",
]

EVENT_LONG_WORDS = [
    "inventory draw", "crude draw", "larger-than-expected draw",
    "sanctions", "hormuz", "attack", "strike", "war", "opec cut",
    "supply risk", "supply disruption", "iran rejects", "talks fail",
]

EVENT_SHORT_WORDS = [
    "inventory build", "crude build", "larger-than-expected build",
    "ceasefire", "peace deal", "sanctions relief", "talks progress",
    "opec increase", "output increase", "demand weak",
]

def now_utc():
    return datetime.now(timezone.utc)

def safe_get(url, timeout=12, retries=1):
    for _ in range(retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                    "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html, */*",
                },
            )
            if response.status_code >= 400:
                print(f"[WARN] HTTP {response.status_code}: {url}")
                print(response.text[:180])
                return None
            return response
        except Exception as error:
            print(f"[WARN] {url}: {error}")
    return None

def safe_post(url, payload, timeout=15):
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        if response.status_code >= 400:
            print(f"[WARN] HTTP {response.status_code}: {url}")
            return None
        return response
    except Exception as error:
        print(f"[WARN] {url}: {error}")
        return None



# ==========================================================
# SIGNAL MEMORY
# ==========================================================

SIGNAL_MEMORY_FILE = os.getenv("SIGNAL_MEMORY_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "last_signal.json"))
SIGNAL_MEMORY_LIMIT = 4
SIGNAL_JOURNAL_FILE = os.getenv("SIGNAL_JOURNAL_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "signal_journal.json"))
SIGNAL_JOURNAL_LIMIT = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "300") or 300)
SIGNAL_JOURNAL_EVAL_MINUTES = int(os.getenv("SIGNAL_JOURNAL_EVAL_MINUTES", "60") or 60)


def load_signal_memory():
    """Read signal memory from local JSON file.

    Supports both old format:
      {"signal": "LONG", "price": 95.0}
    and new format:
      {"history": [{...}, {...}]}
    """
    try:
        if not os.path.exists(SIGNAL_MEMORY_FILE):
            return {"history": []}

        with open(SIGNAL_MEMORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict) and "history" in data and isinstance(data["history"], list):
            data["history"] = data["history"][-SIGNAL_MEMORY_LIMIT:]
            return data

        # Old single-signal format migration
        if isinstance(data, dict) and data.get("signal"):
            return {"history": [data]}

        return {"history": []}

    except Exception as error:
        print(f"[WARN] signal memory read error: {error}")
        return {"history": []}


def save_signal_memory(data):
    """Save signal memory to local JSON file.

    Uses atomic replace + .bak copy so the memory file is less likely to be
    corrupted if GitHub Actions stops the job during write.
    """
    try:
        memory_dir = os.path.dirname(os.path.abspath(SIGNAL_MEMORY_FILE)) or "."
        os.makedirs(memory_dir, exist_ok=True)

        tmp_file = SIGNAL_MEMORY_FILE + ".tmp"
        bak_file = SIGNAL_MEMORY_FILE + ".bak"

        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        os.replace(tmp_file, SIGNAL_MEMORY_FILE)

        with open(bak_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        print(f"MEMORY SAVED: {SIGNAL_MEMORY_FILE}")
    except Exception as error:
        print(f"[WARN] signal memory save error: {error}")

def load_signal_journal():
    try:
        if not os.path.exists(SIGNAL_JOURNAL_FILE):
            return {"signals": []}
        with open(SIGNAL_JOURNAL_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict) and isinstance(data.get("signals"), list):
            data["signals"] = data["signals"][-SIGNAL_JOURNAL_LIMIT:]
            return data
        return {"signals": []}
    except Exception as error:
        print(f"[WARN] signal journal read error: {error}")
        return {"signals": []}

def save_signal_journal(data):
    try:
        journal_dir = os.path.dirname(os.path.abspath(SIGNAL_JOURNAL_FILE)) or "."
        os.makedirs(journal_dir, exist_ok=True)
        data = data or {"signals": []}
        data["signals"] = (data.get("signals") or [])[-SIGNAL_JOURNAL_LIMIT:]
        data["updated_at"] = now_utc().isoformat()

        tmp_file = SIGNAL_JOURNAL_FILE + ".tmp"
        bak_file = SIGNAL_JOURNAL_FILE + ".bak"
        with open(tmp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp_file, SIGNAL_JOURNAL_FILE)
        with open(bak_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        print(f"JOURNAL SAVED: {SIGNAL_JOURNAL_FILE}")
    except Exception as error:
        print(f"[WARN] signal journal save error: {error}")

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

def candle_time(candle):
    ts = (candle or {}).get("ts")
    if ts is None:
        return None
    try:
        ts = int(ts)
        if ts > 10_000_000_000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None

def candles_after_time(candles, created, lookback_minutes=20):
    if not candles or not created:
        return []
    start_time = created - timedelta(minutes=lookback_minutes)
    selected = []
    for candle in candles:
        ctime = candle_time(candle)
        if ctime and ctime >= start_time:
            selected.append(candle)
    return selected

def evaluate_tp_sl_journal_entry(entry, candles, current_price, current_time=None):
    """Evaluate actionable journal entries using stored entry/SL/TP levels."""
    if not entry:
        return entry
    if entry.get("result_status") and entry.get("result_status") not in ["ACTIVE", "ENTRY_NOT_TRIGGERED"]:
        return entry

    signal = entry.get("signal")
    if signal not in ["LONG", "SHORT"]:
        return None

    no_entry = bool(entry.get("no_entry"))
    if no_entry:
        return None

    created = parse_iso_datetime(entry.get("time"))
    if not created:
        return None

    current_time = current_time or now_utc()
    age_minutes = (current_time - created).total_seconds() / 60
    if age_minutes < SIGNAL_JOURNAL_EVAL_MINUTES:
        return entry

    signal_price = safe_float(entry.get("price"))
    entry_price = safe_float(entry.get("entry")) or signal_price
    stop = safe_float(entry.get("stop"))
    tp1 = safe_float(entry.get("tp1"))
    tp2 = safe_float(entry.get("tp2"))
    tp3 = safe_float(entry.get("tp3"))
    if not entry_price or not stop:
        return None

    watch_candles = candles_after_time(candles, created)
    if not watch_candles:
        return None

    activated = bool(signal_price and abs(entry_price - signal_price) / signal_price <= 0.0025)
    activated_at = created.isoformat() if activated else None
    best_tp = 0
    best_tp_price = None
    stopped = False
    stopped_after_tp = False

    def hit_entry(candle):
        return candle.get("low", 0) <= entry_price <= candle.get("high", 0)

    def hit_stop(candle):
        return candle.get("low", 0) <= stop if signal == "LONG" else candle.get("high", 0) >= stop

    def hit_tp(candle, level):
        if not level:
            return False
        return candle.get("high", 0) >= level if signal == "LONG" else candle.get("low", 0) <= level

    for candle in watch_candles:
        if not activated and hit_entry(candle):
            activated = True
            ctime = candle_time(candle)
            activated_at = ctime.isoformat() if ctime else current_time.isoformat()

        if not activated:
            continue

        # Conservative for same-candle ambiguity: stop first if no TP was hit yet.
        stop_hit = hit_stop(candle)
        tp_hits = []
        for idx, level in [(1, tp1), (2, tp2), (3, tp3)]:
            if hit_tp(candle, level):
                tp_hits.append((idx, level))

        if stop_hit and not tp_hits and best_tp == 0:
            stopped = True
            break

        if tp_hits:
            idx, level = max(tp_hits, key=lambda x: x[0])
            if idx > best_tp:
                best_tp = idx
                best_tp_price = level

        if stop_hit:
            stopped = True
            stopped_after_tp = best_tp > 0
            break

    entry["entry_activated"] = activated
    entry["entry_activated_at"] = activated_at
    entry["evaluated_at"] = current_time.isoformat()
    entry["result_price"] = current_price
    if signal_price:
        entry["result_diff_pct"] = round(((current_price - signal_price) / signal_price) * 100, 3)

    if not activated:
        entry["result_status"] = "ENTRY_NOT_TRIGGERED"
        entry["result_note"] = "вхід не активувався"
        return entry

    if best_tp >= 3:
        entry["result_status"] = "TP3"
        entry["result_note"] = "TP3 взято"
    elif best_tp == 2:
        entry["result_status"] = "TP2"
        entry["result_note"] = "TP2 взято" + ("; потім був стоп" if stopped_after_tp else "")
    elif best_tp == 1:
        entry["result_status"] = "TP1"
        entry["result_note"] = "TP1 взято" + ("; потім був стоп" if stopped_after_tp else "")
    elif stopped:
        entry["result_status"] = "STOP"
        entry["result_note"] = "стоп вибило"
    else:
        entry["result_status"] = "ACTIVE"
        entry["result_note"] = "угода ще активна"

    entry["best_tp"] = best_tp
    entry["best_tp_price"] = best_tp_price
    return entry

def evaluate_journal_entry(entry, current_price, current_time=None, candles=None):
    if not entry:
        return entry
    if entry.get("result_status") and entry.get("result_status") not in ["ACTIVE", "ENTRY_NOT_TRIGGERED"]:
        return entry

    tp_sl_result = evaluate_tp_sl_journal_entry(entry, candles or [], current_price, current_time)
    if isinstance(tp_sl_result, dict) and tp_sl_result.get("result_status"):
        return tp_sl_result

    created = parse_iso_datetime(entry.get("time"))
    if not created:
        return entry

    current_time = current_time or now_utc()
    age_minutes = (current_time - created).total_seconds() / 60
    if age_minutes < SIGNAL_JOURNAL_EVAL_MINUTES:
        return entry

    signal = entry.get("signal")
    market_direction = entry.get("market_direction")
    eval_direction = signal if signal in ["LONG", "SHORT"] else market_direction
    price = float(entry.get("price") or 0)
    quality = entry.get("quality_percent")
    no_entry = bool(entry.get("no_entry"))
    if eval_direction not in ["LONG", "SHORT"] or price <= 0:
        entry["result_status"] = "SKIPPED"
        entry["result_note"] = "сигнал був без лонг/шорт напрямку"
        entry["evaluated_at"] = current_time.isoformat()
        return entry

    diff_pct = ((current_price - price) / price) * 100
    entry["result_price"] = current_price
    entry["result_diff_pct"] = round(diff_pct, 3)
    entry["evaluated_at"] = current_time.isoformat()

    moved_with_signal = (eval_direction == "LONG" and diff_pct >= 0.35) or (eval_direction == "SHORT" and diff_pct <= -0.35)
    moved_against_signal = (eval_direction == "LONG" and diff_pct <= -0.35) or (eval_direction == "SHORT" and diff_pct >= 0.35)

    if no_entry:
        if moved_against_signal:
            entry["result_status"] = "NO_ENTRY_SAVED"
            entry["result_note"] = "добре, що не входили — ціна пішла проти напрямку"
        elif moved_with_signal:
            entry["result_status"] = "MISSED_MOVE"
            entry["result_note"] = "рух пішов у правильний бік, але бот не давав входу"
        else:
            entry["result_status"] = "GOOD_NO_ENTRY"
            entry["result_note"] = "правильно чекали — сильного руху не було"
        return entry

    if moved_with_signal:
        entry["result_status"] = "GOOD"
        entry["result_note"] = "сигнал пішов у правильний бік"
    elif moved_against_signal:
        entry["result_status"] = "BAD"
        entry["result_note"] = "сигнал пішов проти напрямку"
    else:
        entry["result_status"] = "FLAT"
        entry["result_note"] = "через 1г сильного руху не було"

    return entry

def infer_market_direction(signal, signal_type=None, tech=None, technical_bias=None, fundamental_bias=None):
    if signal in ["LONG", "SHORT"]:
        return signal

    signal_type = str(signal_type or "").upper()
    if "SHOCK DOWN" in signal_type:
        return "SHORT"
    if "SHOCK UP" in signal_type:
        return "LONG"

    tech = tech or {}
    tech_score = tech.get("score", 0) or 0
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}
    tech_side = (technical_bias or {}).get("side", "")
    fund_side = (fundamental_bias or {}).get("side", "")

    if tech_score <= -45 or micro.get("bias") == "SHORT" or "SHORT" in str(tech_side):
        return "SHORT"
    if tech_score >= 45 or micro.get("bias") == "LONG" or "LONG" in str(tech_side):
        return "LONG"
    if "SHORT" in str(fund_side) and "LONG" not in str(fund_side):
        return "SHORT"
    if "LONG" in str(fund_side) and "SHORT" not in str(fund_side):
        return "LONG"
    return "NEUTRAL"

def update_signal_journal_results(journal, current_price, candles=None):
    journal = journal or {"signals": []}
    updated = []
    changed = False
    current_time = now_utc()

    for entry in journal.get("signals", []):
        before = entry.get("result_status")
        evaluated = evaluate_journal_entry(entry, current_price, current_time, candles)
        if evaluated.get("result_status") != before:
            changed = True
        updated.append(evaluated)

    journal["signals"] = updated[-SIGNAL_JOURNAL_LIMIT:]
    return journal, changed

def build_pattern_tags(signal, signal_type, tech=None, news=None, event_risk=None, orderflow=None, smc=None, late_entry=None, cooling=None, market_direction=None):
    tags = []
    tech = tech or {}
    news = news or {}
    event_risk = event_risk or {}
    orderflow = orderflow or {}
    smc = smc or {}

    news_score = news.get("score", 0) or 0
    event_side = event_risk.get("direction", "MIXED")
    event_high = event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}
    book_bias = (orderflow.get("order_book") or {}).get("bias", "NEUTRAL")
    trades_bias = (orderflow.get("real_flow") or {}).get("bias", "NEUTRAL")
    liquidity_bias = (orderflow.get("liquidity_proxy") or {}).get("bias", "NEUTRAL")
    smc_bias = smc.get("bias", "NEUTRAL")
    direction = signal if signal in ["LONG", "SHORT"] else market_direction

    if "SHOCK DOWN" in str(signal_type):
        tags.append("shock_down")
    if "SHOCK UP" in str(signal_type):
        tags.append("shock_up")
    if direction == "SHORT" and (event_side == "LONG" or news_score >= 35):
        tags.append("news_against")
    if direction == "LONG" and (event_side == "SHORT" or news_score <= -35):
        tags.append("news_against")
    if event_high:
        tags.append("event_high")
    if direction in ["LONG", "SHORT"] and book_bias in ["LONG", "SHORT"] and book_bias != direction:
        tags.append("book_against")
    if direction in ["LONG", "SHORT"] and trades_bias in ["LONG", "SHORT"] and trades_bias != direction:
        tags.append("trades_against")
    if direction in ["LONG", "SHORT"] and liquidity_bias in ["LONG", "SHORT"] and liquidity_bias != direction:
        tags.append("liquidity_against")
    if micro.get("state") == "RANGE":
        tags.append("3m_range")
    if direction in ["LONG", "SHORT"] and micro.get("bias") in ["LONG", "SHORT"] and micro.get("bias") != direction:
        tags.append("3m_against")
    if direction in ["LONG", "SHORT"] and smc_bias not in [direction]:
        tags.append("structure_unconfirmed")
    if late_entry and late_entry.get("late"):
        tags.append("late_entry")
    if cooling and cooling.get("active"):
        tags.append("cooling")

    return sorted(set(tags))

def build_signal_journal_entry(signal, signal_type, price, confidence, quality_percent, plan=None, tech=None, news=None, event_risk=None, orderflow=None, smc=None, late_entry=None, cooling=None, market_direction=None):
    plan = plan if isinstance(plan, dict) else {}
    no_entry = signal not in ["LONG", "SHORT"] or quality_percent is None or quality_percent < 65
    market_direction = market_direction or infer_market_direction(signal, signal_type, tech)
    tags = build_pattern_tags(signal, signal_type, tech, news, event_risk, orderflow, smc, late_entry, cooling, market_direction)
    return {
        "id": now_utc().strftime("%Y%m%d%H%M%S"),
        "time": now_utc().isoformat(),
        "signal": signal,
        "market_direction": market_direction,
        "signal_type": signal_type,
        "price": price,
        "confidence": confidence,
        "quality_percent": quality_percent,
        "no_entry": no_entry,
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "tp1": plan.get("tp1"),
        "tp2": plan.get("tp2"),
        "tp3": plan.get("tp3"),
        "tech_score": (tech or {}).get("score"),
        "news_score": (news or {}).get("score"),
        "event_direction": (event_risk or {}).get("direction"),
        "orderflow_score": (orderflow or {}).get("score"),
        "late_entry": bool((late_entry or {}).get("late")),
        "cooling": bool((cooling or {}).get("active")),
        "tags": tags,
        "result_status": None,
        "result_note": None,
    }

def append_signal_journal(journal, entry):
    signals = (journal or {}).get("signals", [])
    signals.append(entry)
    return {
        "updated_at": now_utc().isoformat(),
        "limit": SIGNAL_JOURNAL_LIMIT,
        "signals": signals[-SIGNAL_JOURNAL_LIMIT:],
    }

def signal_journal_stats_text(journal, hours=24):
    signals = (journal or {}).get("signals", [])
    if not signals:
        return ""

    cutoff = now_utc() - timedelta(hours=hours)
    recent = []
    for item in signals:
        dt = parse_iso_datetime(item.get("time"))
        if dt and dt >= cutoff:
            recent.append(item)

    evaluated = [x for x in recent if x.get("result_status")]
    if len(evaluated) < 3:
        return ""

    good = sum(1 for x in evaluated if x.get("result_status") == "GOOD")
    bad = sum(1 for x in evaluated if x.get("result_status") == "BAD")
    tp1 = sum(1 for x in evaluated if x.get("result_status") == "TP1")
    tp2 = sum(1 for x in evaluated if x.get("result_status") == "TP2")
    tp3 = sum(1 for x in evaluated if x.get("result_status") == "TP3")
    stop = sum(1 for x in evaluated if x.get("result_status") == "STOP")
    active = sum(1 for x in evaluated if x.get("result_status") == "ACTIVE")
    not_triggered = sum(1 for x in evaluated if x.get("result_status") == "ENTRY_NOT_TRIGGERED")
    saved = sum(1 for x in evaluated if x.get("result_status") == "NO_ENTRY_SAVED")
    missed = sum(1 for x in evaluated if x.get("result_status") == "MISSED_MOVE")
    waited_ok = sum(1 for x in evaluated if x.get("result_status") == "GOOD_NO_ENTRY")
    flat = sum(1 for x in evaluated if x.get("result_status") == "FLAT")
    skipped = sum(1 for x in evaluated if x.get("result_status") == "SKIPPED")

    return (
        f"<b>Статистика {hours}г:</b> оцінено {len(evaluated)} | "
        f"TP1/2/3 {tp1}/{tp2}/{tp3}, стоп {stop}, активні {active}, "
        f"не актив. {not_triggered}, напрям ок {good}, погано {bad}, "
        f"чекати ок {saved + waited_ok}, пропущено {missed}, флет {flat}, без напр. {skipped}"
    )

def pattern_stats_text(journal, current_tags, current_signal=None, min_matches=4):
    if not current_tags:
        return ""

    important_tags = [
        "shock_down", "shock_up", "news_against", "book_against",
        "trades_against", "liquidity_against", "3m_range",
        "3m_against", "structure_unconfirmed", "late_entry",
        "cooling", "event_high",
    ]
    current_set = set(current_tags)
    selected = [tag for tag in important_tags if tag in current_set][:5]
    if not selected:
        return ""

    matches = []
    for item in (journal or {}).get("signals", []):
        if not item.get("result_status"):
            continue
        item_tags = set(item.get("tags") or [])
        if all(tag in item_tags for tag in selected):
            item_direction = item.get("signal") if item.get("signal") in ["LONG", "SHORT"] else item.get("market_direction")
            if current_signal in ["LONG", "SHORT"] and item_direction != current_signal:
                continue
            matches.append(item)

    if len(matches) < min_matches:
        return ""

    good = sum(1 for x in matches if x.get("result_status") == "GOOD")
    bad = sum(1 for x in matches if x.get("result_status") == "BAD")
    tp1 = sum(1 for x in matches if x.get("result_status") == "TP1")
    tp2 = sum(1 for x in matches if x.get("result_status") == "TP2")
    tp3 = sum(1 for x in matches if x.get("result_status") == "TP3")
    stop = sum(1 for x in matches if x.get("result_status") == "STOP")
    saved = sum(1 for x in matches if x.get("result_status") == "NO_ENTRY_SAVED")
    missed = sum(1 for x in matches if x.get("result_status") == "MISSED_MOVE")
    no_entry_good = sum(1 for x in matches if x.get("result_status") == "GOOD_NO_ENTRY")
    flat = sum(1 for x in matches if x.get("result_status") == "FLAT")
    skipped = sum(1 for x in matches if x.get("result_status") == "SKIPPED")

    tp_total = tp1 + tp2 + tp3
    if tp_total > stop and tp_total >= max(1, missed):
        conclusion = "схожі входи частіше брали тейк"
    elif stop > tp_total:
        conclusion = "схожі входи часто били стоп"
    elif saved + no_entry_good >= max(good + missed, bad):
        conclusion = "частіше краще чекати"
    elif good > bad:
        conclusion = "частіше напрямок відпрацьовував"
    elif bad > good:
        conclusion = "частіше було проти сигналу"
    else:
        conclusion = "статистика змішана"

    return (
        f"<b>Схожі ситуації:</b> {len(matches)} | "
        f"TP {tp_total}, стоп {stop}, вхід ок {good}, погано {bad}, чекати було правильно {saved + no_entry_good}, "
        f"пропущено {missed}, без сильного руху {flat}, без напрямку {skipped}. "
        f"Висновок: {conclusion}."
    )


def get_signal_history(memory):
    if not memory:
        return []
    if isinstance(memory, dict) and isinstance(memory.get("history"), list):
        return memory.get("history", [])[-SIGNAL_MEMORY_LIMIT:]
    if isinstance(memory, dict) and memory.get("signal"):
        return [memory]
    return []


def get_last_signal(memory):
    history = get_signal_history(memory)
    return history[-1] if history else {}


def append_signal_memory(memory, current_signal):
    history = get_signal_history(memory)
    history.append(current_signal)
    history = history[-SIGNAL_MEMORY_LIMIT:]

    return {
        "updated_at": now_utc().isoformat(),
        "limit": SIGNAL_MEMORY_LIMIT,
        "history": history,
    }


def evaluate_previous_signal(memory, current_price):
    """Short human-readable evaluation of the last 4 signals."""
    history = get_signal_history(memory)
    if not history:
        return ""

    valid = [item for item in history if item.get("signal") in ["LONG", "SHORT"] and item.get("price")]
    if not valid:
        return ""

    results = []
    long_failed = 0
    short_failed = 0
    long_ok = 0
    short_ok = 0

    for item in valid:
        prev_signal = item.get("signal")
        prev_price = float(item.get("price"))
        diff_pct = ((current_price - prev_price) / prev_price) * 100 if prev_price else 0

        if prev_signal == "LONG":
            if diff_pct >= 0.35:
                long_ok += 1
            elif diff_pct <= -0.35:
                long_failed += 1

        if prev_signal == "SHORT":
            if diff_pct <= -0.35:
                short_ok += 1
            elif diff_pct >= 0.35:
                short_failed += 1

    last = valid[-1]
    last_signal = last.get("signal")
    last_price = float(last.get("price"))
    last_diff = ((current_price - last_price) / last_price) * 100 if last_price else 0

    if long_failed >= 2:
        results.append("останні LONG не підтверджуються")
    elif long_ok >= 2:
        results.append("LONG сценарій підтверджується")

    if short_failed >= 2:
        results.append("останні SHORT не підтверджуються")
    elif short_ok >= 2:
        results.append("SHORT сценарій підтверджується")

    if not results:
        if last_signal == "LONG":
            if last_diff >= 0.35:
                results.append("попередній LONG спрацював")
            elif last_diff <= -0.35:
                results.append("попередній LONG не підтвердився")
            else:
                results.append("попередній LONG ще без результату")
        elif last_signal == "SHORT":
            if last_diff <= -0.35:
                results.append("попередній SHORT спрацював")
            elif last_diff >= 0.35:
                results.append("попередній SHORT не підтвердився")
            else:
                results.append("попередній SHORT ще без результату")

    return f"<b>Памʼять 1г:</b> {'; '.join(results)}"


def memory_confidence_adjustment(signal, memory, current_price):
    """Adjust confidence using last 4 signals.

    Main goal:
    - avoid repeating LONG if last LONG signals failed;
    - avoid repeating SHORT if last SHORT signals failed;
    - support reversal if opposite direction clearly worked.
    """
    if signal not in ["LONG", "SHORT"]:
        return 0

    history = get_signal_history(memory)
    valid = [item for item in history if item.get("signal") in ["LONG", "SHORT"] and item.get("price")]
    if not valid:
        return 0

    same_failed = 0
    same_ok = 0
    opposite_failed = 0

    for item in valid:
        prev_signal = item.get("signal")
        prev_price = float(item.get("price"))
        diff_pct = ((current_price - prev_price) / prev_price) * 100 if prev_price else 0

        if signal == prev_signal:
            if signal == "LONG":
                if diff_pct <= -0.35:
                    same_failed += 1
                elif diff_pct >= 0.35:
                    same_ok += 1
            elif signal == "SHORT":
                if diff_pct >= 0.35:
                    same_failed += 1
                elif diff_pct <= -0.35:
                    same_ok += 1

        if signal != prev_signal:
            if prev_signal == "LONG" and diff_pct <= -0.35:
                opposite_failed += 1
            elif prev_signal == "SHORT" and diff_pct >= 0.35:
                opposite_failed += 1

    if same_failed >= 2:
        return -16
    if same_failed == 1:
        return -8
    if same_ok >= 2:
        return 8
    if opposite_failed >= 2:
        return 10

    return 0


def build_current_signal_memory(signal, signal_type, price, confidence, quality_percent=None, plan=None, tech=None):
    plan = plan or {}
    tech = tech or {}
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}

    return {
        "time": now_utc().isoformat(),
        "signal": signal,
        "signal_type": signal_type,
        "price": round(float(price), 4),
        "confidence": int(confidence) if confidence is not None else None,
        "quality_percent": int(quality_percent) if quality_percent is not None else None,
        "entry": plan.get("entry") if isinstance(plan, dict) else None,
        "stop": plan.get("stop") if isinstance(plan, dict) else None,
        "tp1": plan.get("tp1") if isinstance(plan, dict) else None,
        "tp2": plan.get("tp2") if isinstance(plan, dict) else None,
        "tp3": plan.get("tp3") if isinstance(plan, dict) else None,
        "change": tech.get("change"),
        "trend_15m": tech.get("trend_15m"),
        "trend_1h": tech.get("trend_1h"),
        "micro_3m": micro.get("bias"),
        "micro_3m_state": micro.get("state"),
    }




def memory_status_note(memory, current_price):
    """Return memory follow-up only when there is a useful trade history note."""
    history = get_signal_history(memory)
    if not history:
        return ""

    note = evaluate_previous_signal(memory, current_price)
    if note:
        return note

    return ""



def position_follow_note(memory, current_price, tech=None):
    """Position follow-up based on last actionable signal in 4-signal memory.

    This does not open a new trade. It helps manage a position if the user
    already entered on a previous 3/5, 4/5, or 5/5 signal.
    """
    tech = tech or {}
    history = get_signal_history(memory)

    if not history:
        return ""

    # Find the last real LONG/SHORT signal with an entry price.
    last_trade = None
    for item in reversed(history):
        if item.get("signal") in ["LONG", "SHORT"] and item.get("price"):
            last_trade = item
            break

    if not last_trade:
        return ""

    side = last_trade.get("signal")
    entry = float(last_trade.get("entry") or last_trade.get("price"))
    stop = last_trade.get("stop")
    tp1 = last_trade.get("tp1")
    tp2 = last_trade.get("tp2")
    tp3 = last_trade.get("tp3")

    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}
    micro_bias = micro.get("bias", "NEUTRAL")

    diff_pct = ((current_price - entry) / entry) * 100 if entry else 0

    def reached_long(level):
        return level is not None and current_price >= float(level)

    def reached_short(level):
        return level is not None and current_price <= float(level)

    def broken_long(level):
        return level is not None and current_price <= float(level)

    def broken_short(level):
        return level is not None and current_price >= float(level)

    if side == "LONG":
        if broken_long(stop):
            return "<b>Супровід:</b> LONG зламався — ціна біля/нижче стопу"
        if reached_long(tp3):
            return "<b>Супровід:</b> LONG дійшов до TP3 — краще фіксувати основну частину"
        if reached_long(tp2):
            return "<b>Супровід:</b> LONG дійшов до TP2 — підтягнути стоп і зафіксувати частину"
        if reached_long(tp1):
            return "<b>Супровід:</b> LONG дійшов до TP1 — частково фіксувати і підтягнути стоп"
        if micro_bias == "SHORT" and diff_pct > 0:
            return "<b>Супровід:</b> LONG у плюсі, але 3m слабшає — обережно, підтягнути стоп"
        if micro_bias == "SHORT" and diff_pct <= 0:
            return "<b>Супровід:</b> LONG слабшає — не додавати, чекати підтвердження"
        if diff_pct >= 0.25:
            return "<b>Супровід:</b> LONG тримається"
        if diff_pct <= -0.25:
            return "<b>Супровід:</b> LONG під тиском — контролювати стоп"
        return "<b>Супровід:</b> LONG без сильного руху — чекати підтвердження"

    if side == "SHORT":
        if broken_short(stop):
            return "<b>Супровід:</b> SHORT зламався — ціна біля/вище стопу"
        if reached_short(tp3):
            return "<b>Супровід:</b> SHORT дійшов до TP3 — краще фіксувати основну частину"
        if reached_short(tp2):
            return "<b>Супровід:</b> SHORT дійшов до TP2 — підтягнути стоп і зафіксувати частину"
        if reached_short(tp1):
            return "<b>Супровід:</b> SHORT дійшов до TP1 — частково фіксувати і підтягнути стоп"
        if micro_bias == "LONG" and diff_pct < 0:
            return "<b>Супровід:</b> SHORT у плюсі, але 3m слабшає — обережно, підтягнути стоп"
        if micro_bias == "LONG" and diff_pct >= 0:
            return "<b>Супровід:</b> SHORT слабшає — не додавати, чекати підтвердження"
        if diff_pct <= -0.25:
            return "<b>Супровід:</b> SHORT тримається"
        if diff_pct >= 0.25:
            return "<b>Супровід:</b> SHORT під тиском — контролювати стоп"
        return "<b>Супровід:</b> SHORT без сильного руху — чекати підтвердження"

    return ""


# ==========================================================
# TRADINGVIEW
# ==========================================================

def get_tradingview_scan(symbol, screener, columns):
    url = f"https://scanner.tradingview.com/{screener}/scan"
    payload = {
        "symbols": {"tickers": [symbol], "query": {"types": []}},
        "columns": columns,
    }

    response = safe_post(url, payload)
    if not response:
        return None

    try:
        rows = response.json().get("data", [])
        if not rows:
            return None
        values = rows[0].get("d", [])
        if not values or values[0] is None:
            return None
        return values
    except Exception as error:
        print(f"[WARN] TradingView scan parse error for {symbol}: {error}")
        return None

def get_tradingview_market_data():
    columns = [
        "close", "change", "volume",
        "Recommend.All|5", "Recommend.All|15", "Recommend.All|60",
        "RSI|5", "RSI|15", "RSI|60",
        "EMA20|5", "EMA50|5", "EMA20|15", "EMA50|15", "EMA20|60", "EMA50|60",
        "MACD.macd|5", "MACD.macd|15", "MACD.macd|60",
        "ATR|15", "ADX|15", "ADX+DI|15", "ADX-DI|15",
    ]

    for symbol, screener in TRADINGVIEW_SYMBOLS:
        values = get_tradingview_scan(symbol, screener, columns)
        if not values:
            print(f"[WARN] TradingView empty data for {symbol}")
            continue

        try:
            return {
                "source": "TradingView",
                "symbol": symbol,
                "price": float(values[0]),
                "change": values[1],
                "volume": values[2],
                "recommend_5m": values[3],
                "recommend_15m": values[4],
                "recommend_1h": values[5],
                "rsi_5m": values[6],
                "rsi_15m": values[7],
                "rsi_1h": values[8],
                "ema20_5m": values[9],
                "ema50_5m": values[10],
                "ema20_15m": values[11],
                "ema50_15m": values[12],
                "ema20_1h": values[13],
                "ema50_1h": values[14],
                "macd_5m": values[15],
                "macd_15m": values[16],
                "macd_1h": values[17],
                "atr_15m": values[18],
                "adx_15m": values[19],
                "plus_di_15m": values[20],
                "minus_di_15m": values[21],
            }
        except Exception as error:
            print(f"[WARN] TradingView parse error for {symbol}: {error}")

    return None

def analyze_technical(tv):
    price = tv["price"]
    change = tv.get("change") or 0
    atr = tv.get("atr_15m") or price * 0.006

    score = 0
    confirmations = []
    warnings = []
    momentum = "NEUTRAL"

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 32
        momentum = "VERY STRONG UP"
        confirmations.append("дуже сильний імпульс вгору")
    elif change >= STRONG_UP_MOVE_PERCENT:
        score += 24
        momentum = "STRONG UP"
        confirmations.append("сильний імпульс вгору")
    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 32
        momentum = "VERY STRONG DOWN"
        confirmations.append("дуже сильний імпульс вниз")
    elif change <= STRONG_DOWN_MOVE_PERCENT:
        score -= 24
        momentum = "STRONG DOWN"
        confirmations.append("сильний імпульс вниз")

    for tf, rec in [("5m", tv.get("recommend_5m")), ("15m", tv.get("recommend_15m")), ("1h", tv.get("recommend_1h"))]:
        if rec is None:
            continue
        add_score = int(rec * 20)
        score += add_score
        if rec > 0.25:
            confirmations.append(f"TradingView {tf}: buy bias")
        elif rec < -0.25:
            warnings.append(f"TradingView {tf}: sell bias")

    def ema_trend(ema20, ema50, weight):
        nonlocal score
        if ema20 is None or ema50 is None:
            return "UNKNOWN"
        if ema20 > ema50:
            score += weight
            return "UP"
        score -= weight
        return "DOWN"

    trend_5m = ema_trend(tv.get("ema20_5m"), tv.get("ema50_5m"), 8)
    trend_15m = ema_trend(tv.get("ema20_15m"), tv.get("ema50_15m"), 14)
    trend_1h = ema_trend(tv.get("ema20_1h"), tv.get("ema50_1h"), 18)

    if trend_5m == trend_15m == trend_1h == "UP":
        score += 20
        trend = "UP"
        confirmations.append("тренд 5m/15m/1h вверх")
    elif trend_5m == trend_15m == trend_1h == "DOWN":
        score -= 20
        trend = "DOWN"
        warnings.append("тренд 5m/15m/1h вниз")
    elif trend_15m == "UP" and trend_1h == "UP":
        trend = "UP"
        confirmations.append("тренд 15m/1h вверх")
    elif trend_15m == "DOWN" and trend_1h == "DOWN":
        trend = "DOWN"
        warnings.append("тренд 15m/1h вниз")
    else:
        trend = "MIXED"
        warnings.append("тренд змішаний")

    for tf, rsi in [("5m", tv.get("rsi_5m")), ("15m", tv.get("rsi_15m")), ("1h", tv.get("rsi_1h"))]:
        if rsi is None:
            continue
        if rsi > 82:
            score -= 20
            warnings.append(f"RSI {tf}: сильна перекупленість")
        elif rsi > 74:
            score -= 10
            warnings.append(f"RSI {tf}: перекупленість")
        elif rsi < 18:
            score += 16
            confirmations.append(f"RSI {tf}: сильна перепроданість")
        elif rsi < 26:
            score += 8
            confirmations.append(f"RSI {tf}: перепроданість")

    for tf, macd, weight in [("5m", tv.get("macd_5m"), 5), ("15m", tv.get("macd_15m"), 9), ("1h", tv.get("macd_1h"), 12)]:
        if macd is None:
            continue
        score += weight if macd > 0 else -weight

    adx = tv.get("adx_15m")
    plus_di = tv.get("plus_di_15m")
    minus_di = tv.get("minus_di_15m")

    if adx is not None:
        if adx >= 25:
            confirmations.append(f"ADX 15m: тренд сильний ({round(adx, 2)})")
            if plus_di is not None and minus_di is not None:
                score += 10 if plus_di > minus_di else -10
        elif adx < 18:
            warnings.append("ADX 15m: слабкий тренд / можливий боковик")

    return {
        "score": score,
        "trend": trend,
        "trend_5m": trend_5m,
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "momentum": momentum,
        "change": round(change, 4),
        "rsi_5m": round(tv.get("rsi_5m"), 2) if tv.get("rsi_5m") is not None else None,
        "rsi_15m": round(tv.get("rsi_15m"), 2) if tv.get("rsi_15m") is not None else None,
        "rsi_1h": round(tv.get("rsi_1h"), 2) if tv.get("rsi_1h") is not None else None,
        "ema20_15m": round(tv.get("ema20_15m"), 4) if tv.get("ema20_15m") is not None else None,
        "ema50_15m": round(tv.get("ema50_15m"), 4) if tv.get("ema50_15m") is not None else None,
        "macd_15m": round(tv.get("macd_15m"), 4) if tv.get("macd_15m") is not None else None,
        "recommend_5m": tv.get("recommend_5m"),
        "recommend_15m": tv.get("recommend_15m"),
        "recommend_1h": tv.get("recommend_1h"),
        "adx_15m": round(adx, 2) if adx is not None else None,
        "atr_15m": round(atr, 4),
        "confirmations": confirmations[:8],
        "warnings": warnings[:8],
    }

# ==========================================================
# MACRO QUANT
# ==========================================================

def get_macro_news():
    """Fresh macro layer through Google News RSS.
    First tries last 2 hours; if too few headlines, falls back to 6 hours.
    Never prints "unavailable" in Telegram; quiet macro = NEUTRAL.
    """
    macro_items = []

    for query in MACRO_NEWS_QUERIES:
        macro_items.extend(parse_google_rss(query, 2, "Google Macro RSS", 0.9))

    macro_items = deduplicate_news(macro_items)

    if len(macro_items) < 3:
        fallback = []
        for query in MACRO_NEWS_QUERIES:
            fallback.extend(parse_google_rss(query, 6, "Google Macro RSS", 0.75))
        macro_items = deduplicate_news(macro_items + fallback)

    return macro_items

def get_macro_quant_data():
    """Return macro data without unstable TradingView macro scraping."""
    return {"macro_news": get_macro_news()}

def analyze_macro_quant(macro):
    """Macro regime based on stable headline proxy.
    It is less granular than live DXY/VIX/US10Y, but much more reliable on GitHub Actions.
    """
    items = macro.get("macro_news", []) if isinstance(macro, dict) else []

    score = 0
    confirmations = []
    warnings = []

    risk_on_words = [
        "rate cut", "cuts", "dovish", "soft landing", "stocks rise", "stocks gain",
        "nasdaq rises", "s&p 500 rises", "vix falls", "dollar falls", "yields fall",
        "risk-on", "fed pause", "inflation cools", "cpi cools", "jobs slow",
    ]

    risk_off_words = [
        "rate hike", "hawkish", "inflation rises", "hot cpi", "yields rise",
        "dollar rises", "vix rises", "stocks fall", "stocks drop", "nasdaq falls",
        "risk-off", "recession", "higher for longer", "tariff", "trade war",
    ]

    impact_words = [
        "fed", "powell", "fomc", "cpi", "nfp", "inflation", "yields",
        "dollar", "vix", "nasdaq", "s&p", "stocks",
    ]

    for item in items[:30]:
        title = item.get("title", "")
        lower = title.lower()
        risk_on_hits = sum(1 for word in risk_on_words if word in lower)
        risk_off_hits = sum(1 for word in risk_off_words if word in lower)
        impact_hits = sum(1 for word in impact_words if word in lower)

        if risk_on_hits:
            add = 5 * risk_on_hits + min(4, impact_hits)
            score += add
            if len(confirmations) < 5:
                confirmations.append(f"macro risk-on: {title[:100]}")

        if risk_off_hits:
            sub = 5 * risk_off_hits + min(4, impact_hits)
            score -= sub
            if len(warnings) < 5:
                warnings.append(f"macro risk-off: {title[:100]}")

    score = max(-30, min(30, score))

    if score >= 20:
        regime = "RISK-ON / БИЧАЧИЙ MACRO"
    elif score <= -20:
        regime = "RISK-OFF / ВЕДМЕЖИЙ MACRO"
    elif score >= 8:
        regime = "ПОМІРНО БИЧАЧИЙ MACRO"
    elif score <= -8:
        regime = "ПОМІРНО ВЕДМЕЖИЙ MACRO"
    else:
        regime = "НЕЙТРАЛЬНИЙ"

    if not items:
        regime = "НЕЙТРАЛЬНИЙ"

    return {
        "score": score,
        "regime": regime,
        "confirmations": confirmations[:5],
        "warnings": warnings[:5],
        "data": {
            "macro_items": len(items),
            "source": "Google News RSS macro proxy",
        },
    }

# ==========================================================
# ORDERFLOW FROM TRADINGVIEW
# ==========================================================

def analyze_free_orderflow(tv):
    """TradingView-based orderflow proxy.

    This is not real bid/ask delta or order book imbalance. It estimates
    directional participation from change, volume and TradingView ratings.
    """
    score = 0
    details = []
    warnings = []

    change = tv.get("change") or 0
    volume = tv.get("volume") or 0
    rec_5m = tv.get("recommend_5m") or 0
    rec_15m = tv.get("recommend_15m") or 0
    rec_1h = tv.get("recommend_1h") or 0

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 22
        details.append("сильний потік покупців за імпульсом")
    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 22
        warnings.append("сильний потік продавців за імпульсом")

    if rec_5m > 0.2 and rec_15m > 0.2 and rec_1h > 0.2:
        score += 18
        details.append("підтвердження orderflow на 5m/15m/1h")
    elif rec_5m < -0.2 and rec_15m < -0.2 and rec_1h < -0.2:
        score -= 18
        warnings.append("ведмеже підтвердження orderflow на 5m/15m/1h")

    if volume and volume > 0:
        details.append(f"обсяг TradingView proxy: {round(volume, 2)}")
        if abs(change) > 1.5:
            score += 8 if change > 0 else -8
            details.append("обсяг підтверджує імпульсний рух")

    bias = "НЕЙТРАЛЬНИЙ"
    if score >= 25:
        bias = "БИЧАЧИЙ ORDERFLOW"
    elif score <= -25:
        bias = "ВЕДМЕЖИЙ ORDERFLOW"

    return {
        "score": score,
        "bias": bias,
        "used_symbol": "TradingView proxy only",
        "details": details[:7],
        "warnings": warnings[:7],
    }

# ==========================================================
# NEWS / EVENTS
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
        pass
    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def parse_gdelt_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def parse_google_rss(query, lookback_hours, source_name, weight=1.0):
    news = []
    cutoff = now_utc() - timedelta(hours=lookback_hours)
    url = f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{lookback_hours}h')}&hl=en-US&gl=US&ceid=US:en"

    response = safe_get(url, timeout=10, retries=1)
    if not response:
        return news

    try:
        root = ET.fromstring(response.content)
        for item in root.findall(".//item")[:MAX_ITEMS_PER_FEED]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            published_at = parse_date(pub_date)

            if published_at and published_at < cutoff:
                continue

            if title:
                clean_title = BeautifulSoup(title, "html.parser").get_text(" ", strip=True)
                item_weight = weight
                item_source = source_name
                if "Reuters" in clean_title or "reuters.com" in link:
                    item_weight = max(weight, 1.25)
                    item_source = "Reuters via Google News"
                if "CoinDesk" in clean_title or "Cointelegraph" in clean_title:
                    item_weight = min(item_weight, 0.35)
                news.append({
                    "title": clean_title,
                    "link": link,
                    "source": item_source,
                    "published_at": published_at,
                    "weight": item_weight,
                })
    except Exception as error:
        print(f"[WARN] Google RSS parse error: {error}")

    return news

def get_gdelt_news():
    # Disabled intentionally: GDELT often rate-limits GitHub Actions (429/timeout).
    return []

def get_google_news_rss():
    all_news = []
    for query in GOOGLE_NEWS_QUERIES:
        all_news.extend(parse_google_rss(query, NEWS_LOOKBACK_HOURS, "Google News RSS", 1.0))
    return all_news

def get_event_news():
    all_events = []
    for query in EVENT_QUERIES:
        all_events.extend(parse_google_rss(query, EVENT_LOOKBACK_HOURS, "Google Event RSS", 1.0))
    all_events.extend(get_fed_official_rss_events())
    all_events.extend(get_opec_official_events())
    return deduplicate_news(all_events)

def eastern_tz():
    return ZoneInfo("America/New_York")

def calendar_dt_to_utc(date_obj, hour=10, minute=30):
    return datetime(date_obj.year, date_obj.month, date_obj.day, hour, minute, tzinfo=eastern_tz()).astimezone(timezone.utc)

def calendar_time_status(event_time, now=None):
    now = now or now_utc()
    if not event_time:
        return "час невідомий", False

    minutes = int((event_time - now).total_seconds() / 60)
    if -120 <= minutes <= 180:
        return "зараз / близько до виходу", True
    if 180 < minutes <= 24 * 60:
        hours = max(1, round(minutes / 60))
        return f"через {hours} год", True
    if 24 * 60 < minutes <= 42 * 60:
        return "завтра", True
    if -6 * 60 <= minutes < -120:
        return "вийшло сьогодні", True
    return "", False

def calendar_minutes_to_event(event_time, now=None):
    if not event_time:
        return None
    now = now or now_utc()
    try:
        return int((event_time - now).total_seconds() / 60)
    except Exception:
        return None

def is_calendar_hard_block(event_time, now=None):
    """Hard risk only near the event, not many hours before it."""
    minutes = calendar_minutes_to_event(event_time, now)
    if minutes is None:
        return False
    return -60 <= minutes <= 90

def parse_month_date(date_text, default_year=None):
    default_year = default_year or now_utc().year
    clean = re.sub(r"[^A-Za-z0-9, ]+", "", str(date_text or "")).strip()
    for fmt in ["%B %d, %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(clean, fmt).date()
        except Exception:
            pass
    for fmt in ["%B %d", "%b %d"]:
        try:
            parsed = datetime.strptime(clean, fmt)
            return parsed.replace(year=default_year).date()
        except Exception:
            pass
    return None

def next_weekday_date(start_date, weekday):
    days = (weekday - start_date.weekday()) % 7
    return start_date + timedelta(days=days)

def extract_eia_release_time(text):
    text = text or ""
    match = re.search(r"at\s+(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", text, re.I)
    if not match:
        return 10, 30
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()
    if ampm == "P" and hour != 12:
        hour += 12
    if ampm == "A" and hour == 12:
        hour = 0
    return hour, minute

def get_eia_calendar_event():
    """Official EIA WPSR calendar. Fallback: Wednesday 10:30 ET."""
    now = now_utc()
    text = ""
    for url in [EIA_WPSR_URL, EIA_WPSR_SCHEDULE_URL]:
        response = safe_get(url, timeout=8, retries=1)
        if response:
            text += "\n" + BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)

    release_date = None
    release_time = (10, 30)
    next_match = re.search(r"Next Release Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if next_match:
        release_date = parse_month_date(next_match.group(1))

    # Holiday schedule text often says "released on Thursday, May 28, 2026, at 12:00 P.M."
    if release_date:
        around_date = release_date.strftime("%B %d, %Y").replace(" 0", " ")
        idx = text.find(around_date)
        if idx >= 0:
            release_time = extract_eia_release_time(text[idx:idx + 220])

    if not release_date:
        today_et = now.astimezone(eastern_tz()).date()
        release_date = next_weekday_date(today_et, 2)
        release_time = (10, 30)

    event_time = calendar_dt_to_utc(release_date, release_time[0], release_time[1])
    status, active = calendar_time_status(event_time, now)
    minutes_to = calendar_minutes_to_event(event_time, now)
    return {
        "name": "EIA",
        "title": "EIA запаси нафти",
        "time": event_time,
        "status": status,
        "active": active,
        "hard_block": is_calendar_hard_block(event_time, now),
        "minutes_to_event": minutes_to,
        "risk": "HIGH",
        "source": "EIA official",
    }

def parse_fomc_calendar_events():
    response = safe_get(FED_FOMC_CALENDAR_URL, timeout=8, retries=1)
    if not response:
        return []

    text = BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    month_map = {
        "January": 1, "February": 2, "March": 3, "April": 4, "Apr/May": 5,
        "May": 5, "June": 6, "July": 7, "August": 8, "September": 9,
        "October": 10, "November": 11, "December": 12,
    }
    current_year = str(now_utc().year)
    in_year = False
    current_month = None
    events = []

    for line in lines:
        if re.fullmatch(rf"{current_year}\s+FOMC Meetings", line):
            in_year = True
            continue
        if in_year and re.fullmatch(r"\d{4}\s+FOMC Meetings", line) and current_year not in line:
            break
        if not in_year:
            continue

        if line in month_map:
            current_month = month_map[line]
            continue

        match = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?\*?(?:\s+\(notation vote\))?", line)
        if current_month and match:
            day = int(match.group(2) or match.group(1))
            try:
                event_time = calendar_dt_to_utc(datetime(now_utc().year, current_month, day).date(), 14, 0)
                status, active = calendar_time_status(event_time)
                minutes_to = calendar_minutes_to_event(event_time)
                events.append({
                    "name": "Fed",
                    "title": "Fed / FOMC рішення",
                    "time": event_time,
                    "status": status,
                    "active": active,
                    "hard_block": is_calendar_hard_block(event_time),
                    "minutes_to_event": minutes_to,
                    "risk": "HIGH",
                    "source": "Fed official calendar",
                })
            except Exception:
                pass

        minutes_match = re.search(r"Released\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", line)
        if minutes_match:
            release_date = parse_month_date(minutes_match.group(1))
            if release_date:
                event_time = calendar_dt_to_utc(release_date, 14, 0)
                status, active = calendar_time_status(event_time)
                minutes_to = calendar_minutes_to_event(event_time)
                events.append({
                    "name": "Fed",
                    "title": "Fed minutes",
                    "time": event_time,
                    "status": status,
                    "active": active,
                    "hard_block": is_calendar_hard_block(event_time),
                    "minutes_to_event": minutes_to,
                    "risk": "MEDIUM",
                    "source": "Fed official calendar",
                })

    return events

def get_fed_official_rss_events():
    events = []
    cutoff = now_utc() - timedelta(hours=EVENT_LOOKBACK_HOURS)
    for source_name, url, weight in OFFICIAL_FED_RSS_FEEDS:
        response = safe_get(url, timeout=8, retries=1)
        if not response:
            continue
        try:
            root = ET.fromstring(response.content.strip())
            for item in root.findall(".//item")[:MAX_ITEMS_PER_FEED]:
                title = get_item_text(item, "title")
                date_text = get_item_text(item, "pubDate") or get_item_text(item, "published") or get_item_text(item, "updated")
                published_at = parse_date(date_text)
                if published_at and published_at < cutoff:
                    continue
                if title:
                    events.append({
                        "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                        "link": get_item_text(item, "link"),
                        "source": source_name,
                        "published_at": published_at,
                        "weight": weight,
                    })
        except Exception as error:
            print(f"[WARN] Fed RSS parse error {source_name}: {error}")
    return events

def get_opec_official_events():
    response = safe_get(OPEC_PRESS_URL, timeout=8, retries=1)
    if not response:
        return []
    events = []
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        current_year = str(now_utc().year)
        for link in soup.find_all("a"):
            title = link.get_text(" ", strip=True)
            lower = title.lower()
            if len(title) < 18:
                continue
            if not any(key in lower for key in ["opec", "opec+", "production", "output", "oil", "crude", "ministerial"]):
                continue
            if current_year not in title and not any(key in lower for key in ["today", "meeting", "ministerial"]):
                continue
            href = link.get("href", "")
            if href.startswith("/"):
                href = "https://www.opec.org" + href
            events.append({
                "title": title,
                "link": href,
                "source": "OPEC official",
                "published_at": None,
                "weight": 1.0,
            })
            if len(events) >= 4:
                break
    except Exception as error:
            print(f"[WARN] OPEC official parse error: {error}")
    return events

def get_opec_calendar_events():
    events = []
    items = parse_google_rss(
        "site:opec.org OPEC OPEC+ meeting production oil today upcoming",
        72,
        "OPEC official calendar",
        1.1,
    )
    for item in items[:3]:
        status = "свіжа офіційна новина"
        if item.get("published_at"):
            age_hours = (now_utc() - item["published_at"]).total_seconds() / 3600
            if age_hours <= 6:
                status = "останні 6 год"
            elif age_hours <= 24:
                status = "сьогодні"
            else:
                status = "найближчі дні"
        events.append({
            "name": "OPEC",
            "title": "OPEC/OPEC+",
            "time": item.get("published_at"),
            "status": status,
            "active": True,
            "hard_block": True,
            "minutes_to_event": 0,
            "risk": "HIGH",
            "source": "OPEC official / Google RSS",
        })
    return events

def analyze_economic_calendar():
    """Free official calendar layer for oil futures risk."""
    calendar_events = []
    try:
        calendar_events.append(get_eia_calendar_event())
    except Exception as error:
        print(f"[WARN] EIA calendar error: {error}")
    try:
        calendar_events.extend(parse_fomc_calendar_events())
    except Exception as error:
        print(f"[WARN] Fed calendar error: {error}")
    try:
        calendar_events.extend(get_opec_calendar_events())
    except Exception as error:
        print(f"[WARN] OPEC calendar error: {error}")

    active_events = [event for event in calendar_events if event.get("active")]
    blocking_events = [event for event in active_events if event.get("hard_block")]
    score = 0
    for event in blocking_events:
        if event.get("risk") == "HIGH":
            score -= 16
        elif event.get("risk") == "MEDIUM":
            score -= 10

    if abs(score) >= 24:
        risk = "ВИСОКИЙ"
    elif abs(score) >= 12:
        risk = "ПІДВИЩЕНИЙ"
    else:
        risk = "НОРМАЛЬНИЙ"

    return {
        "active": bool(active_events),
        "hard_block": bool(blocking_events),
        "score": score,
        "risk": risk,
        "events": active_events[:4],
        "blocking_events": blocking_events[:4],
        "all_events": calendar_events[:12],
    }

def merge_calendar_into_event_risk(event_risk, calendar):
    event_risk = dict(event_risk or {})
    calendar = calendar or {}
    event_risk["calendar"] = calendar
    if not calendar.get("active"):
        return event_risk

    event_risk["score"] = int(max(-MAX_EVENT_SCORE, min(MAX_EVENT_SCORE, event_risk.get("score", 0) + calendar.get("score", 0))))
    if abs(event_risk["score"]) >= 40:
        event_risk["risk"] = "ДУЖЕ ВИСОКИЙ"
    elif abs(event_risk["score"]) >= 20:
        event_risk["risk"] = "ВИСОКИЙ"
    elif abs(event_risk["score"]) >= 8:
        event_risk["risk"] = "ПІДВИЩЕНИЙ"

    important = list(event_risk.get("important", []) or [])
    for event in calendar.get("events", []):
        important.append({
            "title": f"{event.get('title')} — {event.get('status')}",
            "link": "",
            "source": event.get("source", "official calendar"),
            "published_at": event.get("time"),
            "weight": 1.0,
        })
    event_risk["important"] = important[:8]
    return event_risk

def get_item_text(item, tag):
    text = item.findtext(tag)
    if text:
        return text.strip()
    for child in list(item):
        if child.tag.lower().endswith(tag.lower()):
            return (child.text or "").strip()
    return ""

def parse_html_news(source, html):
    news = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for tag in soup.find_all(["a", "h2", "h3"]):
            text = tag.get_text(" ", strip=True)
            if not text or len(text) < 18:
                continue
            lower = text.lower()
            if not any(key in lower for key in ["oil", "crude", "brent", "opec", "eia", "inventory", "gas", "energy", "trump", "fed"]):
                continue
            href = tag.get("href", "")
            if href.startswith("/"):
                base = re.match(r"https?://[^/]+", source["url"])
                if base:
                    href = base.group(0) + href
            candidates.append((text, href))

        for title, link in candidates[:MAX_ITEMS_PER_FEED]:
            news.append({
                "title": title,
                "link": link,
                "source": source["name"],
                "published_at": None,
                "weight": source.get("weight", 1.0) * 0.35,
            })
    except Exception as error:
        print(f"[WARN] HTML parse error {source['name']}: {error}")
    return news

def parse_rss(source):
    response = safe_get(source["url"], timeout=10, retries=1)
    if not response:
        return []
    if source.get("type") == "html":
        return parse_html_news(source, response.text)

    news = []
    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)
    try:
        root = ET.fromstring(response.content.strip())
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for item in items[:MAX_ITEMS_PER_FEED]:
            title = get_item_text(item, "title")
            link = get_item_text(item, "link")
            date_text = get_item_text(item, "pubDate") or get_item_text(item, "published") or get_item_text(item, "updated") or get_item_text(item, "dc:date")
            published_at = parse_date(date_text)

            if published_at and published_at < cutoff:
                continue

            if title:
                news.append({
                    "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                    "link": link,
                    "source": source["name"],
                    "published_at": published_at,
                    "weight": source.get("weight", 1.0),
                })
    except Exception as error:
        print(f"[WARN] RSS parse error {source['name']}: {error}")
        return parse_html_news(source, response.text)

    return news

def get_cryptopanic_news():
    if not CRYPTOPANIC_KEY:
        return []

    url = "https://cryptopanic.com/api/v1/posts/" + f"?auth_token={CRYPTOPANIC_KEY}&filter=hot&public=true"
    response = safe_get(url, timeout=10, retries=1)
    if not response:
        return []

    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)
    news = []
    try:
        data = response.json()
        for item in data.get("results", [])[:MAX_ITEMS_PER_FEED]:
            title = item.get("title", "")
            published_at = parse_date(item.get("published_at"))
            if published_at and published_at < cutoff:
                continue
            if title:
                news.append({
                    "title": title,
                    "link": item.get("url", ""),
                    "source": "CryptoPanic",
                    "published_at": published_at,
                    "weight": 0.7,
                })
    except Exception as error:
        print(f"[WARN] CryptoPanic parse error: {error}")
    return news

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"[^a-z0-9а-яіїєґ]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:120]

def deduplicate_news(news):
    seen = set()
    unique = []

    for item in news:
        key = normalize_title(item["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)

    unique.sort(
        key=lambda x: x["published_at"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    return unique

def get_all_fresh_news():
    all_news = []
    # GDELT disabled for GitHub stability.
    all_news.extend(get_google_news_rss())
    for source in NEWS_SOURCES:
        all_news.extend(parse_rss(source))
    all_news.extend(get_cryptopanic_news())
    return deduplicate_news(all_news)

def keyword_score(title, words):
    lower = title.lower()
    return sum(1 for word in words if word in lower)

def directional_news_adjustment(title):
    lower = title.lower()
    if any(word in lower for word in BULLISH_GEO_WORDS):
        return 7
    if any(word in lower for word in BEARISH_SUPPLY_WORDS):
        return -7
    return 0

def headline_direction(title):
    lower = title.lower()

    long_hits = keyword_score(lower, BULLISH_WORDS) + keyword_score(lower, EVENT_LONG_WORDS)
    short_hits = keyword_score(lower, BEARISH_WORDS) + keyword_score(lower, EVENT_SHORT_WORDS)

    if long_hits > short_hits:
        return "LONG", "заголовок вказує на ризик дефіциту/санкцій/зростання нафти"
    if short_hits > long_hits:
        return "SHORT", "заголовок вказує на мирні переговори/санкційне послаблення/надлишок пропозиції"
    return "MIXED", "заголовок важливий, але напрямок неоднозначний"

def summarize_headline_directions(items, limit=5):
    summary = []
    for item in items[:limit]:
        direction, reason = headline_direction(item["title"])
        summary.append(f"{direction}: {reason}")
    if not summary:
        return "Немає важливих заголовків для висновку."
    long_count = sum(1 for text in summary if text.startswith("LONG"))
    short_count = sum(1 for text in summary if text.startswith("SHORT"))

    if long_count > short_count:
        return "Перевага новин: LONG. Більше заголовків підтримують ріст/ризик дефіциту."
    if short_count > long_count:
        return "Перевага новин: SHORT. Більше заголовків підтримують зниження/деескалацію."
    return "Перевага новин: MIXED. Напрямок неоднозначний."

def analyze_news(news):
    bullish = 0
    bearish = 0
    impact = 0
    breaking = 0
    raw_score = 0
    important = []

    for item in news:
        title = item["title"]
        weight = item.get("weight", 1.0)

        bull_hits = keyword_score(title, BULLISH_WORDS)
        bear_hits = keyword_score(title, BEARISH_WORDS)
        impact_hits = keyword_score(title, HIGH_IMPACT_WORDS)
        breaking_hits = keyword_score(title, BREAKING_WORDS)
        directional = directional_news_adjustment(title)

        item_score = 0

        if bull_hits:
            bullish += 1
            item_score += 6 * bull_hits * weight
        if bear_hits:
            bearish += 1
            item_score -= 6 * bear_hits * weight
        if directional > 0:
            bullish += 1
            item_score += directional * weight
        elif directional < 0:
            bearish += 1
            item_score += directional * weight
        if impact_hits:
            impact += 1
            important.append(item)
        if breaking_hits:
            breaking += 1
            if item not in important:
                important.append(item)

        # Impact/breaking words mean the headline is important, not bullish.
        # They amplify an already directional headline without changing its side.
        if item_score:
            importance_multiplier = 1.0 + min(0.55, 0.12 * impact_hits + 0.08 * breaking_hits)
            raw_score += item_score * importance_multiplier

    if bullish > bearish:
        sentiment = "БИЧАЧІ"
    elif bearish > bullish:
        sentiment = "ВЕДМЕЖІ"
    else:
        sentiment = "НЕЙТРАЛЬНІ"

    capped_score = int(max(-MAX_NEWS_SCORE, min(MAX_NEWS_SCORE, raw_score)))

    return {
        "score": capped_score,
        "raw_score": round(raw_score, 2),
        "noise_warning": news_noise_warning(len(news), raw_score, capped_score),
        "sentiment": sentiment,
        "bullish": bullish,
        "bearish": bearish,
        "impact": impact,
        "breaking": breaking,
        "important": important[:8],
        "total": len(news),
        "summary": summarize_headline_directions(important, 8),
    }

def analyze_event_risk(events):
    raw_score = 0
    direction_score = 0
    important = []

    for item in events:
        title = item["title"]
        lower = title.lower()

        if any(word in lower for word in EVENT_HIGH_RISK_WORDS):
            raw_score -= 8
            important.append(item)

        long_hits = keyword_score(title, EVENT_LONG_WORDS)
        short_hits = keyword_score(title, EVENT_SHORT_WORDS)

        if long_hits:
            direction_score += 7 * long_hits
        if short_hits:
            direction_score -= 7 * short_hits

    score = int(max(-MAX_EVENT_SCORE, min(MAX_EVENT_SCORE, raw_score)))

    if abs(score) >= 40:
        risk = "ДУЖЕ ВИСОКИЙ"
    elif abs(score) >= 20:
        risk = "ВИСОКИЙ"
    elif abs(score) >= 8:
        risk = "ПІДВИЩЕНИЙ"
    else:
        risk = "НОРМАЛЬНИЙ"

    if direction_score > 14:
        direction = "LONG"
    elif direction_score < -14:
        direction = "SHORT"
    else:
        direction = "MIXED"

    return {
        "score": score,
        "risk": risk,
        "direction_score": direction_score,
        "direction": direction,
        "important": important[:8],
        "total": len(events),
        "summary": summarize_headline_directions(important, 8),
    }

def news_noise_warning(total_news, raw_score, capped_score):
    if total_news >= 60 and abs(raw_score) > abs(capped_score) * 4:
        return "Високий новинний шум: багато заголовків, score обмежено"
    if total_news < 3:
        return "Мало свіжих новин: новинне підтвердження слабке"
    return "Нормально"

# ==========================================================
# REAL PRICE ACTION + SMC STRUCTURE
# ==========================================================

OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")

def get_real_candles(inst_id=OKX_INST_ID, bar="15m", limit=120):
    """Free public OHLC candles. OKX is used as a stable fallback source.
    Returns candles oldest -> newest.
    """
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    response = safe_get(url, timeout=10, retries=1)
    if not response:
        return []

    try:
        rows = response.json().get("data", [])
        candles = []
        for row in rows:
            candles.append({
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if row[5] is not None else 0.0,
            })
        candles.sort(key=lambda x: x["ts"])
        return candles
    except Exception as error:
        print(f"[WARN] real candles parse error: {error}")
        return []

def get_okx_recent_trades(inst_id=OKX_INST_ID, limit=100):
    """Recent public trades from OKX for a more realistic flow proxy."""
    url = f"https://www.okx.com/api/v5/market/trades?instId={inst_id}&limit={limit}"
    response = safe_get(url, timeout=10, retries=1)
    if not response:
        return []

    try:
        rows = response.json().get("data", [])
        trades = []
        for row in rows:
            trades.append({
                "px": float(row.get("px", 0) or 0),
                "sz": float(row.get("sz", 0) or 0),
                "side": str(row.get("side", "")).lower(),
                "ts": int(row.get("ts", 0) or 0),
            })
        return trades
    except Exception as error:
        print(f"[WARN] OKX trades parse error: {error}")
        return []

def analyze_okx_trade_flow(trades):
    """Buy/sell trade imbalance from public OKX prints.

    This is still a proxy, but it is closer to real flow than TradingView ratings.
    """
    if not trades:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "note": "OKX trades unavailable",
            "buy_volume": 0,
            "sell_volume": 0,
            "delta_pct": 0,
        }

    buy_volume = sum(t["sz"] for t in trades if t.get("side") == "buy")
    sell_volume = sum(t["sz"] for t in trades if t.get("side") == "sell")
    total = buy_volume + sell_volume
    delta_pct = ((buy_volume - sell_volume) / total * 100) if total else 0

    score = 0
    if delta_pct >= 18:
        score = 16
        bias = "LONG"
        note = "останні угоди більше за покупців"
    elif delta_pct <= -18:
        score = -16
        bias = "SHORT"
        note = "останні угоди більше за продавців"
    elif delta_pct >= 8:
        score = 8
        bias = "LONG"
        note = "легка перевага покупців"
    elif delta_pct <= -8:
        score = -8
        bias = "SHORT"
        note = "легка перевага продавців"
    else:
        bias = "NEUTRAL"
        note = "останні угоди без явної переваги"

    return {
        "available": True,
        "score": score,
        "bias": bias,
        "note": note,
        "buy_volume": round(buy_volume, 4),
        "sell_volume": round(sell_volume, 4),
        "delta_pct": round(delta_pct, 2),
    }

def get_okx_order_book(inst_id=OKX_INST_ID, depth=50):
    """Public OKX order book snapshot."""
    url = f"https://www.okx.com/api/v5/market/books?instId={inst_id}&sz={depth}"
    response = safe_get(url, timeout=10, retries=1)
    if not response:
        return {"bids": [], "asks": []}

    try:
        data = response.json().get("data", [])
        if not data:
            return {"bids": [], "asks": []}
        book = data[0]
        bids = [(float(x[0]), float(x[1])) for x in book.get("bids", []) if len(x) >= 2]
        asks = [(float(x[0]), float(x[1])) for x in book.get("asks", []) if len(x) >= 2]
        return {"bids": bids, "asks": asks}
    except Exception as error:
        print(f"[WARN] OKX order book parse error: {error}")
        return {"bids": [], "asks": []}

def analyze_order_book_pressure(book, price=None):
    """Order book pressure and nearby walls.

    This is a snapshot, not a guarantee. It helps explain short-term pressure:
    more bids below price = buyers support, more asks above price = sellers pressure.
    """
    bids = (book or {}).get("bids", [])
    asks = (book or {}).get("asks", [])
    if not bids or not asks:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "note": "Стакан недоступний",
            "wall": "",
            "imbalance_pct": 0,
        }

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = price or ((best_bid + best_ask) / 2)
    near_pct = 0.004
    lower = mid * (1 - near_pct)
    upper = mid * (1 + near_pct)

    near_bids = [(p, s) for p, s in bids if p >= lower]
    near_asks = [(p, s) for p, s in asks if p <= upper]
    bid_volume = sum(s for _, s in near_bids)
    ask_volume = sum(s for _, s in near_asks)
    total = bid_volume + ask_volume
    imbalance_pct = ((bid_volume - ask_volume) / total * 100) if total else 0

    biggest_bid = max(near_bids, key=lambda x: x[1]) if near_bids else None
    biggest_ask = max(near_asks, key=lambda x: x[1]) if near_asks else None

    wall = ""
    if biggest_ask and biggest_bid:
        if biggest_ask[1] > biggest_bid[1] * 1.7:
            wall = f"стіна продавців біля {round(biggest_ask[0], 4)}"
        elif biggest_bid[1] > biggest_ask[1] * 1.7:
            wall = f"стіна покупців біля {round(biggest_bid[0], 4)}"
    elif biggest_ask:
        wall = f"найбільша стіна продавців біля {round(biggest_ask[0], 4)}"
    elif biggest_bid:
        wall = f"найбільша стіна покупців біля {round(biggest_bid[0], 4)}"

    if imbalance_pct >= 18:
        score = 12
        bias = "LONG"
        note = "покупці тримають ціну"
    elif imbalance_pct <= -18:
        score = -12
        bias = "SHORT"
        note = "продавці тиснуть зверху"
    elif imbalance_pct >= 8:
        score = 6
        bias = "LONG"
        note = "легка перевага покупців"
    elif imbalance_pct <= -8:
        score = -6
        bias = "SHORT"
        note = "легка перевага продавців"
    else:
        score = 0
        bias = "NEUTRAL"
        note = "стакан без явної переваги"

    return {
        "available": True,
        "score": score,
        "bias": bias,
        "note": note,
        "wall": wall,
        "imbalance_pct": round(imbalance_pct, 2),
        "bid_volume": round(bid_volume, 4),
        "ask_volume": round(ask_volume, 4),
    }

def analyze_liquidity_proxy(candles, trade_flow=None, order_book=None):
    """Liquidation/stop-run proxy from candles, volume, trades and book pressure."""
    if not candles or len(candles) < 25:
        return {"available": False, "score": 0, "bias": "NEUTRAL", "note": "мало даних"}

    recent = candles[-20:]
    last = candles[-1]
    prev = candles[-2]
    avg_vol = sum(c.get("volume", 0) for c in recent[:-1]) / max(1, len(recent[:-1]))
    vol_ratio = (last.get("volume", 0) or 0) / avg_vol if avg_vol else 1
    body = last["close"] - last["open"]
    candle_range = max(last["high"] - last["low"], 1e-9)
    close_pos = (last["close"] - last["low"]) / candle_range
    move_pct = ((last["close"] - prev["close"]) / prev["close"] * 100) if prev["close"] else 0

    trade_bias = (trade_flow or {}).get("bias", "NEUTRAL")
    book_bias = (order_book or {}).get("bias", "NEUTRAL")
    note = "спокійно"
    score = 0
    bias = "NEUTRAL"

    if move_pct <= -0.45 and vol_ratio >= 1.35 and body < 0:
        bias = "SHORT"
        score = -14
        note = "вибивають лонги"
        if close_pos <= 0.25:
            score -= 4
        if trade_bias == "SHORT" or book_bias == "SHORT":
            score -= 4
    elif move_pct >= 0.45 and vol_ratio >= 1.35 and body > 0:
        bias = "LONG"
        score = 14
        note = "можливий шорт-сквіз"
        if close_pos >= 0.75:
            score += 4
        if trade_bias == "LONG" or book_bias == "LONG":
            score += 4

    # Sweep and reclaim: stops may have been taken, but continuation is not clean.
    prev_low = min(c["low"] for c in recent[:-1])
    prev_high = max(c["high"] for c in recent[:-1])
    if last["low"] < prev_low and last["close"] > prev_low:
        bias = "LONG"
        score = max(score, 8)
        note = "зняли знизу, можливий відскок"
    elif last["high"] > prev_high and last["close"] < prev_high:
        bias = "SHORT"
        score = min(score, -8)
        note = "зняли зверху, можливий відкат"

    return {
        "available": True,
        "score": score,
        "bias": bias,
        "note": note,
        "vol_ratio": round(vol_ratio, 2),
        "move_pct": round(move_pct, 3),
    }

def merge_orderflow_proxy(orderflow, trade_flow):
    orderflow = orderflow or {}
    trade_flow = trade_flow or {}
    if not trade_flow.get("available"):
        orderflow["real_flow"] = trade_flow
        return orderflow

    score = int(orderflow.get("score", 0) or 0) + int(trade_flow.get("score", 0) or 0)
    orderflow["score"] = score
    if score >= 25:
        orderflow["bias"] = "БИЧАЧИЙ ORDERFLOW"
    elif score <= -25:
        orderflow["bias"] = "ВЕДМЕЖИЙ ORDERFLOW"
    else:
        orderflow["bias"] = "НЕЙТРАЛЬНИЙ"
    orderflow["real_flow"] = trade_flow
    if trade_flow.get("note"):
        orderflow.setdefault("details", []).append("OKX trades: " + trade_flow["note"])
    return orderflow

def merge_market_microstructure(orderflow, order_book, liquidity_proxy):
    orderflow = orderflow or {}
    order_book = order_book or {}
    liquidity_proxy = liquidity_proxy or {}

    score = int(orderflow.get("score", 0) or 0)
    if order_book.get("available"):
        score += int(order_book.get("score", 0) or 0)
        if order_book.get("note"):
            orderflow.setdefault("details", []).append("Стакан: " + order_book["note"])
    if liquidity_proxy.get("available"):
        score += int(liquidity_proxy.get("score", 0) or 0)
        if liquidity_proxy.get("note"):
            orderflow.setdefault("details", []).append("Ліквідність: " + liquidity_proxy["note"])

    orderflow["score"] = score
    if score >= 25:
        orderflow["bias"] = "БИЧАЧИЙ ORDERFLOW"
    elif score <= -25:
        orderflow["bias"] = "ВЕДМЕЖИЙ ORDERFLOW"
    else:
        orderflow["bias"] = "НЕЙТРАЛЬНИЙ"
    orderflow["order_book"] = order_book
    orderflow["liquidity_proxy"] = liquidity_proxy
    return orderflow

def component_direction_text(component_side):
    if component_side not in ["LONG", "SHORT"]:
        return ""
    return "за лонг" if component_side == "LONG" else "за шорт"

def microstructure_text(orderflow, signal=None):
    orderflow = orderflow or {}
    trade_flow = orderflow.get("real_flow") or {}
    book = orderflow.get("order_book") or {}
    liquidity = orderflow.get("liquidity_proxy") or {}

    lines = []
    if book.get("available"):
        text = book.get("note", "без явної переваги")
        relation = component_direction_text(book.get("bias"))
        if relation:
            text = f"{relation} — {text}"
        if book.get("wall"):
            text += f"; {book.get('wall')}"
        lines.append("Стакан: " + text)
    if trade_flow.get("available"):
        note = trade_flow.get("note", "без явної переваги")
        if "продавців" in note:
            note = "продавці активні"
        elif "покупців" in note:
            note = "покупці активні"
        else:
            note = "без явної переваги"
        relation = component_direction_text(trade_flow.get("bias"))
        if relation:
            note = f"{relation} — {note}"
        lines.append("Угоди: " + note)
    if liquidity.get("available"):
        note = liquidity.get("note", "без явного вибивання")
        if note.lower().startswith("ліквідність:"):
            note = note.split(":", 1)[1].strip()
        relation = component_direction_text(liquidity.get("bias"))
        if relation:
            note = f"{relation} — {note}"
        lines.append("Ліквідність: " + note)

    return "\n".join(f"<b>{line}</b>" for line in lines[:3])

def microstructure_compact_text(orderflow):
    orderflow = orderflow or {}
    book = (orderflow.get("order_book") or {}).get("bias", "NEUTRAL")
    trades = (orderflow.get("real_flow") or {}).get("bias", "NEUTRAL")
    liquidity = (orderflow.get("liquidity_proxy") or {}).get("note", "спокійно")

    def side_text(side):
        if side == "LONG":
            return "лонг"
        if side == "SHORT":
            return "шорт"
        return "нейтр."

    if not (
        (orderflow.get("order_book") or {}).get("available")
        or (orderflow.get("real_flow") or {}).get("available")
        or (orderflow.get("liquidity_proxy") or {}).get("available")
    ):
        return ""
    return f"Потік: стакан {side_text(book)}, угоди {side_text(trades)}, ліквідність {liquidity}"

def smc_compact_text(smc):
    if not smc or not isinstance(smc, dict) or not smc.get("available"):
        return ""
    note = smc_conflict_note(smc)
    if "змішана" in note:
        return "Структура: змішана"
    if smc.get("bias") == "LONG":
        return "Структура: лонг"
    if smc.get("bias") == "SHORT":
        return "Структура: шорт"
    return "Структура: без підтвердження"

def quick_backtest_smoke(candles, lookback=80):
    """Small health-check backtest for GitHub logs.

    It is not a trading system. It only checks whether recent momentum signals
    had follow-through on the same OKX 15m data.
    """
    if not candles or len(candles) < 40:
        return {"available": False, "summary": "Backtest: not enough candles"}

    sample = candles[-lookback:]
    wins = 0
    losses = 0
    signals = 0

    for i in range(25, len(sample) - 5):
        window = sample[i - 20:i]
        close = sample[i]["close"]
        avg20 = sum(c["close"] for c in window) / len(window)
        prev = sample[i - 3:i]
        next_candles = sample[i + 1:i + 6]

        long_setup = close > avg20 and all(prev[j]["close"] <= prev[j + 1]["close"] for j in range(len(prev) - 1))
        short_setup = close < avg20 and all(prev[j]["close"] >= prev[j + 1]["close"] for j in range(len(prev) - 1))

        if not long_setup and not short_setup:
            continue

        signals += 1
        if long_setup:
            tp = close * 1.004
            sl = close * 0.997
            hit_tp = any(c["high"] >= tp for c in next_candles)
            hit_sl = any(c["low"] <= sl for c in next_candles)
        else:
            tp = close * 0.996
            sl = close * 1.003
            hit_tp = any(c["low"] <= tp for c in next_candles)
            hit_sl = any(c["high"] >= sl for c in next_candles)

        if hit_tp and not hit_sl:
            wins += 1
        elif hit_sl and not hit_tp:
            losses += 1

    total = wins + losses
    winrate = round(wins / total * 100, 1) if total else 0
    return {
        "available": True,
        "signals": signals,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "summary": f"Backtest smoke: {wins}/{total} wins, winrate {winrate}% ({signals} setups)",
    }

def detect_swing_points(candles, lookback=2):
    swings_high = []
    swings_low = []
    if not candles or len(candles) < lookback * 2 + 5:
        return swings_high, swings_low

    for i in range(lookback, len(candles) - lookback):
        current = candles[i]
        left = candles[i - lookback:i]
        right = candles[i + 1:i + 1 + lookback]

        if all(current["high"] > c["high"] for c in left + right):
            swings_high.append({"idx": i, "price": current["high"], "ts": current["ts"]})
        if all(current["low"] < c["low"] for c in left + right):
            swings_low.append({"idx": i, "price": current["low"], "ts": current["ts"]})

    return swings_high, swings_low

def detect_fvg(candles):
    """Simple 3-candle FVG / imbalance detection."""
    if not candles or len(candles) < 5:
        return {"side": "NONE", "zone": None, "note": "FVG немає"}

    recent = candles[-12:]
    last_fvg = None

    for i in range(2, len(recent)):
        c0 = recent[i - 2]
        c2 = recent[i]

        # Bullish FVG: high of candle 1 below low of candle 3
        if c0["high"] < c2["low"]:
            last_fvg = {
                "side": "LONG",
                "zone": (round(c0["high"], 4), round(c2["low"], 4)),
                "note": "bullish imbalance / FVG нижче ціни",
            }

        # Bearish FVG: low of candle 1 above high of candle 3
        if c0["low"] > c2["high"]:
            last_fvg = {
                "side": "SHORT",
                "zone": (round(c2["high"], 4), round(c0["low"], 4)),
                "note": "bearish imbalance / FVG вище ціни",
            }

    return last_fvg or {"side": "NONE", "zone": None, "note": "FVG немає"}

def analyze_real_volume_confirmation(candles):
    """Real volume confirmation from OKX candles.
    Detects:
    - volume spike
    - bullish/bearish impulse with volume
    - поглинання: large volume but weak close/progress
    """
    if not candles or len(candles) < 25:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "spike": False,
            "поглинання": "NONE",
            "note": "volume data unavailable",
        }

    recent = candles[-21:-1]
    last = candles[-1]
    avg_vol = sum(c.get("volume", 0) for c in recent) / max(1, len(recent))
    last_vol = last.get("volume", 0) or 0

    if avg_vol <= 0:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "spike": False,
            "поглинання": "NONE",
            "note": "volume average unavailable",
        }

    vol_ratio = last_vol / avg_vol
    candle_range = max(last["high"] - last["low"], 1e-9)
    body = last["close"] - last["open"]
    body_ratio = abs(body) / candle_range

    # Close location inside candle: 1 = close near high, 0 = close near low.
    close_location = (last["close"] - last["low"]) / candle_range

    score = 0
    bias = "NEUTRAL"
    spike = vol_ratio >= 1.6
    поглинання = "NONE"
    notes = []

    if spike:
        notes.append(f"volume spike x{round(vol_ratio, 2)}")

    # Strong candle + strong volume = continuation confirmation.
    if spike and body > 0 and body_ratio >= 0.55 and close_location >= 0.65:
        score += 16
        bias = "LONG"
        notes.append("обсяг підтверджує покупців")
    elif spike and body < 0 and body_ratio >= 0.55 and close_location <= 0.35:
        score -= 16
        bias = "SHORT"
        notes.append("обсяг підтверджує продавців")

    # Absorption: large volume, but price cannot close in direction of the wick/attempt.
    if spike and last["high"] > max(c["high"] for c in recent[-10:]) and close_location <= 0.45:
        поглинання = "BEARISH ABSORPTION"
        score -= 14
        bias = "SHORT"
        notes.append("поглинання зверху: покупців поглинули")
    elif spike and last["low"] < min(c["low"] for c in recent[-10:]) and close_location >= 0.55:
        поглинання = "BULLISH ABSORPTION"
        score += 14
        bias = "LONG"
        notes.append("поглинання знизу: продавців поглинули")

    # High volume doji / weak body = caution
    if spike and body_ratio <= 0.25:
        notes.append("великий обсяг без прогресу — можливий розворот/пауза")
        if close_location > 0.55:
            score += 4
        elif close_location < 0.45:
            score -= 4

    return {
        "available": True,
        "score": int(score),
        "bias": bias,
        "spike": spike,
        "vol_ratio": round(vol_ratio, 2),
        "поглинання": поглинання,
        "note": "; ".join(notes[:3]) if notes else "volume neutral",
    }

def analyze_smc_structure(candles):
    """Real price action + SMC structure:
    - пробій структури / ознака розвороту
    - liquidity sweep
    - FVG / imbalance
    - structure bias
    """
    if not candles or len(candles) < 30:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "phase": "NO DATA",
            "bos": "NONE",
            "choch": "NONE",
            "sweep": "NONE",
            "fvg": {"side": "NONE", "zone": None, "note": "FVG немає"},
            "summary": "real price action недоступний",
        }

    swings_high, swings_low = detect_swing_points(candles, lookback=2)
    last = candles[-1]
    prev = candles[-2]
    recent = candles[-20:]

    recent_high = max(c["high"] for c in recent[:-1])
    recent_low = min(c["low"] for c in recent[:-1])
    close = last["close"]

    last_swing_high = swings_high[-1]["price"] if swings_high else recent_high
    last_swing_low = swings_low[-1]["price"] if swings_low else recent_low

    # Simple ATR from real candles
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / min(14, len(trs)) if trs else max(close * 0.006, 0.01)

    score = 0
    notes = []
    bos = "NONE"
    choch = "NONE"
    sweep = "NONE"

    # пробій структури: close beyond recent structure.
    if close > last_swing_high:
        bos = "пробій структури LONG"
        score += 24
        notes.append("пробій структури LONG: закриття вище swing high")
    elif close < last_swing_low:
        bos = "пробій структури SHORT"
        score -= 24
        notes.append("пробій структури SHORT: закриття нижче swing low")

    # Liquidity sweep: wick takes high/low but close returns back.
    if last["high"] > recent_high and close < recent_high:
        sweep = "UPSIDE SWEEP / SHORT RISK"
        score -= 18
        notes.append("зняли ліквідність зверху — ризик SHORT-відкату")
    elif last["low"] < recent_low and close > recent_low:
        sweep = "DOWNSIDE SWEEP / LONG RISK"
        score += 18
        notes.append("зняли ліквідність знизу — ризик LONG-відскоку")

    # ознака розвороту approximation: previous candle broke one way, current close reverses through midpoint/structure.
    mid = (recent_high + recent_low) / 2
    if prev["low"] <= recent_low + atr * 0.15 and close > mid:
        choch = "ознака розвороту LONG"
        score += 16
        notes.append("ознака розвороту LONG: після sweep ціна повернулась у діапазон")
    elif prev["high"] >= recent_high - atr * 0.15 and close < mid:
        choch = "ознака розвороту SHORT"
        score -= 16
        notes.append("ознака розвороту SHORT: після sweep ціна повернулась у діапазон")

    fvg = detect_fvg(candles)
    if fvg.get("side") == "LONG":
        score += 6
    elif fvg.get("side") == "SHORT":
        score -= 6

    volume_confirmation = analyze_real_volume_confirmation(candles)
    if volume_confirmation.get("available"):
        score += int(volume_confirmation.get("score", 0))
        if volume_confirmation.get("note") and volume_confirmation.get("note") != "volume neutral":
            notes.append(volume_confirmation.get("note"))

    # Fake пробій структури protection: пробій структури without volume confirmation is weaker.
    if bos == "пробій структури LONG" and volume_confirmation.get("available"):
        if not volume_confirmation.get("spike") and volume_confirmation.get("bias") != "LONG":
            score -= 8
            notes.append("пробій структури LONG без сильного обсягу — ризик fake breakout")
        elif volume_confirmation.get("bias") == "LONG":
            score += 6
            notes.append("пробій структури LONG підтверджений обсягом")

    if bos == "пробій структури SHORT" and volume_confirmation.get("available"):
        if not volume_confirmation.get("spike") and volume_confirmation.get("bias") != "SHORT":
            score += 8
            notes.append("пробій структури SHORT без сильного обсягу — ризик fake breakdown")
        elif volume_confirmation.get("bias") == "SHORT":
            score -= 6
            notes.append("пробій структури SHORT підтверджений обсягом")

    # Impulse / cooling from real candles
    last_body = abs(last["close"] - last["open"])
    last_range = max(last["high"] - last["low"], 1e-9)
    body_ratio = last_body / last_range

    if last["close"] > last["open"] and body_ratio >= 0.60:
        score += 8
        notes.append("сильна bullish candle")
    elif last["close"] < last["open"] and body_ratio >= 0.60:
        score -= 8
        notes.append("сильна bearish candle")

    if score >= 22:
        bias = "LONG"
    elif score <= -22:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    if bos != "NONE":
        phase = "BREAKOUT / пробій структури"
    elif choch != "NONE":
        phase = "REVERSAL / ознака розвороту"
    elif sweep != "NONE":
        phase = "LIQUIDITY SWEEP"
    else:
        phase = "RANGE / WAIT"

    return {
        "available": True,
        "score": int(score),
        "bias": bias,
        "phase": phase,
        "bos": bos,
        "choch": choch,
        "sweep": sweep,
        "fvg": fvg,
        "volume": volume_confirmation,
        "swing_high": round(last_swing_high, 4),
        "swing_low": round(last_swing_low, 4),
        "atr": round(atr, 4),
        "summary": "; ".join(notes[:3]) if notes else "SMC структура нейтральна",
    }

def smc_probability_adjustment(signal, smc):
    if not smc or not smc.get("available") or signal not in ["LONG", "SHORT"]:
        return 0

    bias = smc.get("bias", "NEUTRAL")
    phase = smc.get("phase", "")
    sweep = smc.get("sweep", "NONE")
    volume = smc.get("volume", {}) if isinstance(smc, dict) else {}

    adjust = 0
    if bias == signal:
        adjust += 8
    elif bias in ["LONG", "SHORT"] and bias != signal:
        adjust -= 12

    if signal == "LONG" and sweep.startswith("UPSIDE SWEEP"):
        adjust -= 8
    if signal == "SHORT" and sweep.startswith("DOWNSIDE SWEEP"):
        adjust -= 8

    if volume.get("bias") == signal:
        adjust += 6
    elif volume.get("bias") in ["LONG", "SHORT"] and volume.get("bias") != signal:
        adjust -= 8

    if signal == "LONG" and volume.get("поглинання") == "BEARISH ABSORPTION":
        adjust -= 10
    if signal == "SHORT" and volume.get("поглинання") == "BULLISH ABSORPTION":
        adjust -= 10

    if phase == "RANGE / WAIT":
        adjust -= 4

    return adjust

def smc_short_text(smc):
    if not smc or not smc.get("available"):
        return ""
    phase = smc.get("phase", "RANGE / WAIT")
    bias = smc.get("bias", "NEUTRAL")
    volume = smc.get("volume", {}) if isinstance(smc, dict) else {}
    поглинання = volume.get("поглинання", "NONE")
    vol_bias = volume.get("bias", "NEUTRAL")

    if поглинання == "BEARISH ABSORPTION":
        return "Структура: поглинання зверху — ризик відкату"
    if поглинання == "BULLISH ABSORPTION":
        return "Структура: поглинання знизу — ризик відскоку"

    vol_note = ""
    if volume.get("spike") and vol_bias in ["LONG", "SHORT"]:
        vol_note = " + volume"

    if phase == "BREAKOUT / пробій структури":
        return f"Структура: {bias} пробій структури{vol_note}"
    if phase == "REVERSAL / ознака розвороту":
        return f"Структура: можливий розворот {bias}{vol_note}"
    if phase == "LIQUIDITY SWEEP":
        return "Структура: liquidity sweep — чекати підтвердження"
    return "Структура: діапазон — краще чекати"


def price_structure_priority_override(signal, signal_type, confidence, score, tech, smc, news, event_risk, micro=None):
    """Price action beats news when chart is clearly opposite."""
    tech = tech or {}
    smc = smc or {}
    micro = micro or {}

    tech_score = tech.get("score", 0) or 0
    change = tech.get("change", 0) or 0
    momentum = tech.get("momentum", "NEUTRAL")
    smc_bias = smc.get("bias", "NEUTRAL")
    smc_score = smc.get("score", 0) or 0
    micro_bias = micro.get("bias", "NEUTRAL")
    micro_state = micro.get("state", "RANGE")

    # Strong enough to block opposite news/event bias.
    # Important: if TECH is already SHORT and 3m does not confirm LONG,
    # the bot must not keep saying "wait LONG".
    strong_chart_short = (
        tech_score <= -55
        or smc_bias == "SHORT"
        or smc_score <= -18
        or micro_bias == "SHORT"
        or micro_state == "SHORT_STRENGTHENING"
        or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
        or (change <= -0.45 and micro_bias != "LONG")
    )

    strong_chart_long = (
        tech_score >= 55
        or smc_bias == "LONG"
        or smc_score >= 18
        or micro_bias == "LONG"
        or micro_state == "LONG_STRENGTHENING"
        or momentum in ["STRONG UP", "VERY STRONG UP"]
        or (change >= 0.45 and micro_bias != "SHORT")
    )

    if signal == "LONG" and strong_chart_short:
        # If chart is clearly SHORT, convert to SHORT-watch.
        # If it is only conflict, block LONG into neutral.
        if smc_bias == "SHORT" or micro_bias == "SHORT" or tech_score <= -55 or (change <= -0.45 and micro_bias != "LONG"):
            return {
                "signal": "SHORT",
                "signal_type": "PRICE ACTION SHORT / NEWS CONFLICT",
                "confidence": max(55, min(72, abs(tech_score) if abs(tech_score) < 72 else 72)),
                "score": -abs(max(abs(score), abs(tech_score), abs(smc_score))),
                "reason": "графік сильніший за LONG-новини",
            }
        return {
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "LONG BLOCKED / PRICE ACTION SHORT",
            "confidence": min(confidence, 50),
            "score": 0,
            "reason": "LONG скасовано: графік проти",
        }

    if signal == "SHORT" and strong_chart_long:
        if smc_bias == "LONG" or micro_bias == "LONG" or tech_score >= 55 or (change >= 0.45 and micro_bias != "SHORT"):
            return {
                "signal": "LONG",
                "signal_type": "PRICE ACTION LONG / NEWS CONFLICT",
                "confidence": max(55, min(72, abs(tech_score) if abs(tech_score) < 72 else 72)),
                "score": abs(max(abs(score), abs(tech_score), abs(smc_score))),
                "reason": "графік сильніший за SHORT-новини",
            }
        return {
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "SHORT BLOCKED / PRICE ACTION LONG",
            "confidence": min(confidence, 50),
            "score": 0,
            "reason": "SHORT скасовано: графік проти",
        }

    return {"signal": signal, "signal_type": signal_type, "confidence": confidence, "score": score, "reason": ""}


def price_action_truth_filter(signal, tech, smc, news, event_risk, orderflow):
    """Price action must dominate news after strong dumps/pumps."""
    if signal not in ["LONG", "SHORT"]:
        return {"blocked": False, "penalty": 0, "bonus": 0, "reason": "", "mode": "NEUTRAL"}

    change = tech.get("change", 0) or 0
    momentum = tech.get("momentum", "NEUTRAL")
    trend_5m = tech.get("trend_5m", "UNKNOWN")
    trend_15m = tech.get("trend_15m", "UNKNOWN")
    smc_bias = (smc or {}).get("bias", "NEUTRAL")
    smc_phase = (smc or {}).get("phase", "NO DATA")
    bos = (smc or {}).get("bos", "NONE")
    choch = (smc or {}).get("choch", "NONE")
    volume = (smc or {}).get("volume", {}) if isinstance(smc, dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    order_score = orderflow.get("score", 0) if isinstance(orderflow, dict) else 0
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_dir = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    strong_dump = change <= -1.2 or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
    strong_pump = change >= 1.2 or momentum in ["STRONG UP", "VERY STRONG UP"]

    bearish_structure = (
        smc_bias == "SHORT" or bos == "пробій структури SHORT" or
        (trend_5m == "DOWN" and trend_15m == "DOWN") or
        order_score <= -20 or vol_bias == "SHORT"
    )
    bullish_structure = (
        smc_bias == "LONG" or bos == "пробій структури LONG" or
        (trend_5m == "UP" and trend_15m == "UP") or
        order_score >= 20 or vol_bias == "LONG"
    )

    bullish_reclaim = choch == "ознака розвороту LONG" or bos == "пробій структури LONG" or vol_bias == "LONG" or (smc_phase == "REVERSAL / ознака розвороту" and smc_bias == "LONG")
    bearish_reclaim = choch == "ознака розвороту SHORT" or bos == "пробій структури SHORT" or vol_bias == "SHORT" or (smc_phase == "REVERSAL / ознака розвороту" and smc_bias == "SHORT")

    if signal == "LONG" and strong_dump and bearish_structure and not bullish_reclaim:
        return {
            "blocked": True,
            "penalty": -28,
            "bonus": 0,
            "reason": "LONG не підтверджений: після дампу структура ще bearish, новини не підтверджені ціною",
            "mode": "BLOCK_LONG_AFTER_DUMP",
        }

    if signal == "SHORT" and strong_pump and bullish_structure and not bearish_reclaim:
        return {
            "blocked": True,
            "penalty": -28,
            "bonus": 0,
            "reason": "SHORT не підтверджений: після пампу структура ще bullish, новини не підтверджені ціною",
            "mode": "BLOCK_SHORT_AFTER_PUMP",
        }

    if signal == "SHORT" and strong_dump and bearish_structure:
        return {
            "blocked": False,
            "penalty": 0,
            "bonus": 12 if not (news_score >= 30 or event_dir == "LONG") else 8,
            "reason": "SHORT continuation: структура і momentum підтверджують продавців",
            "mode": "SHORT_CONTINUATION_CONFIRMED",
        }

    if signal == "LONG" and strong_pump and bullish_structure:
        return {
            "blocked": False,
            "penalty": 0,
            "bonus": 8,
            "reason": "LONG continuation: структура і momentum підтверджують покупців",
            "mode": "LONG_CONTINUATION_CONFIRMED",
        }

    return {"blocked": False, "penalty": 0, "bonus": 0, "reason": "", "mode": "NEUTRAL"}

def cap_countertrend_probability(probability, signal, tech, smc):
    if probability is None or signal not in ["LONG", "SHORT"]:
        return probability

    change = tech.get("change", 0) or 0
    momentum = tech.get("momentum", "NEUTRAL")
    smc_bias = (smc or {}).get("bias", "NEUTRAL")
    bos = (smc or {}).get("bos", "NONE")
    choch = (smc or {}).get("choch", "NONE")
    volume = (smc or {}).get("volume", {}) if isinstance(smc, dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")

    strong_dump = change <= -1.2 or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
    strong_pump = change >= 1.2 or momentum in ["STRONG UP", "VERY STRONG UP"]

    has_long_reclaim = bos == "пробій структури LONG" or choch == "ознака розвороту LONG" or vol_bias == "LONG"
    has_short_reclaim = bos == "пробій структури SHORT" or choch == "ознака розвороту SHORT" or vol_bias == "SHORT"

    if signal == "LONG" and strong_dump and smc_bias != "LONG" and not has_long_reclaim:
        return min(probability, 42)
    if signal == "SHORT" and strong_pump and smc_bias != "SHORT" and not has_short_reclaim:
        return min(probability, 42)

    return probability

def extension_exhaustion_filter(signal, tech, smc, news=None, event_risk=None):
    """Protects from late continuation entries after an already extended dump/pump.

    Main idea:
    - Do not allow 5/5 SHORT after a sharp dump unless price action confirms continuation.
    - Do not allow 5/5 LONG after a sharp pump unless price action confirms continuation.
    - If news/events are against the continuation, force WAIT/RETEST.
    """
    if signal not in ["LONG", "SHORT"]:
        return {"active": False, "cap": None, "reason": ""}

    tech = tech or {}
    smc = smc or {}
    news = news or {}
    event_risk = event_risk or {}

    change = tech.get("change", 0) or 0
    momentum = tech.get("momentum", "NEUTRAL")

    smc_bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    choch = smc.get("choch", "NONE")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    поглинання = volume.get("поглинання", "NONE")

    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    strong_dump = change <= -1.0 or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
    strong_pump = change >= 1.0 or momentum in ["STRONG UP", "VERY STRONG UP"]

    short_confirmed = (
        smc_bias == "SHORT"
        or bos == "пробій структури SHORT"
        or vol_bias == "SHORT"
        or поглинання == "BEARISH ABSORPTION"
    )
    long_confirmed = (
        smc_bias == "LONG"
        or bos == "пробій структури LONG"
        or vol_bias == "LONG"
        or поглинання == "BULLISH ABSORPTION"
    )

    # SHORT after a dump is dangerous if structure/volume did not confirm continuation.
    if signal == "SHORT" and strong_dump:
        if not short_confirmed:
            cap = 54
            reason = "SHORT після сильного дампу без SMC/пробій структури/volume підтвердження — ризик відскоку"
            if news_score >= 35 or event_side == "LONG" or long_confirmed or choch == "ознака розвороту LONG":
                cap = 49
                reason = "SHORT запізнений: дамп уже був, bullish-новини/відскок проти входу"
            return {"active": True, "cap": cap, "reason": reason}

    # LONG after a pump is dangerous if structure/volume did not confirm continuation.
    if signal == "LONG" and strong_pump:
        if not long_confirmed:
            cap = 54
            reason = "LONG після сильного пампу без SMC/пробій структури/volume підтвердження — ризик відкату"
            if news_score <= -35 or event_side == "SHORT" or short_confirmed or choch == "ознака розвороту SHORT":
                cap = 49
                reason = "LONG запізнений: памп уже був, bearish-фактори/відкат проти входу"
            return {"active": True, "cap": cap, "reason": reason}

    return {"active": False, "cap": None, "reason": ""}

def extension_exhaustion_reason(signal, tech, smc, news=None, event_risk=None):
    info = extension_exhaustion_filter(signal, tech, smc, news, event_risk)
    return info.get("reason", "") if info.get("active") else ""

def early_reversal_engine(tv, tech, smc, news=None, event_risk=None):
    """Detect early reversal after strong dump/pump. It is a WATCH layer, not an entry trigger."""
    tech = tech or {}
    smc = smc or {}
    news = news or {}
    event_risk = event_risk or {}

    price = tv.get("price") if isinstance(tv, dict) else None
    change = tech.get("change", 0) or 0
    momentum = tech.get("momentum", "NEUTRAL")
    rsi5 = tech.get("rsi_5m")
    rsi15 = tech.get("rsi_15m")
    ema20 = tech.get("ema20_15m")
    ema50 = tech.get("ema50_15m")
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    smc_bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    choch = smc.get("choch", "NONE")
    sweep = smc.get("sweep", "NONE")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    поглинання = volume.get("поглинання", "NONE")

    score = 0
    side = "NONE"
    reasons = []

    strong_dump = change <= -1.0 or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
    strong_pump = change >= 1.0 or momentum in ["STRONG UP", "VERY STRONG UP"]

    oversold = (rsi5 is not None and rsi5 <= 30) or (rsi15 is not None and rsi15 <= 35)
    overbought = (rsi5 is not None and rsi5 >= 70) or (rsi15 is not None and rsi15 >= 66)

    ema_reclaim_long = bool(price and ema20 and price > ema20)
    ema_reclaim_strong_long = bool(price and ema20 and ema50 and price > ema20 and ema20 >= ema50 * 0.998)

    ema_reject_short = bool(price and ema20 and price < ema20)
    ema_reject_strong_short = bool(price and ema20 and ema50 and price < ema20 and ema20 <= ema50 * 1.002)

    if strong_dump:
        side = "LONG"
        score += 12
        reasons.append("після сильного дампу шукаємо розворот")
        if news_score >= 35 or event_side == "LONG":
            score += 14
            reasons.append("bullish news/event підтримує відскок")
        if sweep.startswith("DOWNSIDE") or choch == "ознака розвороту LONG":
            score += 18
            reasons.append("sweep/ознака розвороту LONG")
        if поглинання == "BULLISH ABSORPTION" or vol_bias == "LONG":
            score += 16
            reasons.append("обсяг/поглинання за покупців")
        if bos == "пробій структури LONG" or smc_bias == "LONG":
            score += 16
            reasons.append("SMC підтверджує LONG")
        if oversold:
            score += 8
            reasons.append("перепроданість після дампу")
        if ema_reclaim_long:
            score += 10
            reasons.append("ціна вище EMA20")
        if ema_reclaim_strong_long:
            score += 6
            reasons.append("EMA reclaim посилюється")
        if bos == "пробій структури SHORT" or smc_bias == "SHORT" or vol_bias == "SHORT":
            score -= 14
            reasons.append("частина структури ще bearish")

    elif strong_pump:
        side = "SHORT"
        score += 12
        reasons.append("після сильного пампу шукаємо відкат")
        if news_score <= -35 or event_side == "SHORT":
            score += 14
            reasons.append("bearish news/event підтримує відкат")
        if sweep.startswith("UPSIDE") or choch == "ознака розвороту SHORT":
            score += 18
            reasons.append("sweep/ознака розвороту SHORT")
        if поглинання == "BEARISH ABSORPTION" or vol_bias == "SHORT":
            score += 16
            reasons.append("обсяг/поглинання за продавців")
        if bos == "пробій структури SHORT" or smc_bias == "SHORT":
            score += 16
            reasons.append("SMC підтверджує SHORT")
        if overbought:
            score += 8
            reasons.append("перекупленість після пампу")
        if ema_reject_short:
            score += 10
            reasons.append("ціна нижче EMA20")
        if ema_reject_strong_short:
            score += 6
            reasons.append("EMA reject посилюється")
        if bos == "пробій структури LONG" or smc_bias == "LONG" or vol_bias == "LONG":
            score -= 14
            reasons.append("частина структури ще bullish")

    if side == "NONE" or score < 28:
        return {"active": False, "side": "NONE", "score": int(score), "stage": "NONE", "quality_cap": None, "reason": "", "reasons": reasons[:4]}

    if score >= 64:
        stage = "EARLY CONFIRMATION"
        quality_cap = 64
    elif score >= 48:
        stage = "REVERSAL WATCH"
        quality_cap = 56
    else:
        stage = "WEAK REVERSAL WATCH"
        quality_cap = 49

    return {
        "active": True,
        "side": side,
        "score": int(score),
        "stage": stage,
        "quality_cap": quality_cap,
        "reason": "; ".join(reasons[:3]),
        "reasons": reasons[:5],
    }

def early_reversal_text(early):
    if not early or not early.get("active"):
        return ""
    side = early.get("side", "NONE")
    stage = early.get("stage", "REVERSAL WATCH")
    score = early.get("score", 0)
    if stage == "EARLY CONFIRMATION":
        return f"<b>Early reversal:</b> {side} раннє підтвердження ({score}%)"
    if stage == "REVERSAL WATCH":
        return f"<b>Early reversal:</b> можливий {side} розворот ({score}%)"
    return f"<b>Early reversal:</b> слабкий {side} watch ({score}%)"

def reversal_scalp_signal(tv, tech, smc, orderflow, news=None, event_risk=None, session=None):
    """Very early scalp trigger after a sharp dump/pump.

    This is not a normal trend entry. It only fires when the move is already
    stretched and there are first signs that price is being bought/sold back.
    """
    tech = tech or {}
    smc = smc or {}
    orderflow = orderflow or {}
    news = news or {}
    event_risk = event_risk or {}
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}

    change = tech.get("change", 0) or 0
    rsi5 = tech.get("rsi_5m")
    rsi15 = tech.get("rsi_15m")
    news_score = news.get("score", 0) or 0
    event_side = event_risk.get("direction", "MIXED")
    calendar = (event_risk.get("calendar") or {})
    calendar_active = bool(calendar.get("active"))
    session_name = (session or {}).get("session", "")

    book_bias = (orderflow.get("order_book") or {}).get("bias", "NEUTRAL")
    trade_bias = (orderflow.get("real_flow") or {}).get("bias", "NEUTRAL")
    liquidity_bias = (orderflow.get("liquidity_proxy") or {}).get("bias", "NEUTRAL")
    micro_state = micro.get("state", "RANGE")
    micro_bias = micro.get("bias", "NEUTRAL")
    micro_score = micro.get("score", 0) or 0

    sweep = smc.get("sweep", "NONE")
    choch = smc.get("choch", "NONE")
    bos = smc.get("bos", "NONE")
    smc_bias = smc.get("bias", "NEUTRAL")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    absorption = volume.get("поглинання", "NONE")

    def build(side):
        is_long = side == "LONG"
        score = 0
        reasons = []

        if is_long:
            if change > -0.9:
                return None
            if change <= -4.2:
                return None
            if (rsi5 is not None and rsi5 <= 31) or (rsi15 is not None and rsi15 <= 35):
                score += 12
                reasons.append("RSI перепроданий")
            if micro_state in ["SHORT_COOLING", "LONG_STRENGTHENING"] or micro_bias == "LONG" or micro_score >= -8:
                score += 18
                reasons.append("3m показує перший відкуп")
            if sweep.startswith("DOWNSIDE") or choch == "ознака розвороту LONG":
                score += 20
                reasons.append("зняли низ і ціна повертається")
            if book_bias == "LONG" or trade_bias == "LONG" or liquidity_bias == "LONG":
                score += 14
                reasons.append("покупці почали тримати ціну")
            if vol_bias == "LONG" or absorption == "BULLISH ABSORPTION" or smc_bias == "LONG" or bos == "пробій структури LONG":
                score += 16
                reasons.append("структура/обсяг за відскок")
            if news_score >= 25 or event_side == "LONG":
                score += 8
                reasons.append("новини підтримують відскок")
            if micro_state == "SHORT_STRENGTHENING" and trade_bias != "LONG" and book_bias != "LONG":
                score -= 18
                reasons.append("3m ще продавлює вниз")
            if smc_bias == "SHORT" and vol_bias == "SHORT" and trade_bias != "LONG":
                score -= 14
                reasons.append("структура ще за продавців")
        else:
            if change < 0.9:
                return None
            if change >= 4.2:
                return None
            if (rsi5 is not None and rsi5 >= 69) or (rsi15 is not None and rsi15 >= 65):
                score += 12
                reasons.append("RSI перекуплений")
            if micro_state in ["LONG_COOLING", "SHORT_STRENGTHENING"] or micro_bias == "SHORT" or micro_score <= 8:
                score += 18
                reasons.append("3m показує перший продаж")
            if sweep.startswith("UPSIDE") or choch == "ознака розвороту SHORT":
                score += 20
                reasons.append("зняли верх і ціна повертається")
            if book_bias == "SHORT" or trade_bias == "SHORT" or liquidity_bias == "SHORT":
                score += 14
                reasons.append("продавці почали тиснути")
            if vol_bias == "SHORT" or absorption == "BEARISH ABSORPTION" or smc_bias == "SHORT" or bos == "пробій структури SHORT":
                score += 16
                reasons.append("структура/обсяг за відкат")
            if news_score <= -25 or event_side == "SHORT":
                score += 8
                reasons.append("новини підтримують відкат")
            if micro_state == "LONG_STRENGTHENING" and trade_bias != "SHORT" and book_bias != "SHORT":
                score -= 18
                reasons.append("3m ще тисне вгору")
            if smc_bias == "LONG" and vol_bias == "LONG" and trade_bias != "SHORT":
                score -= 14
                reasons.append("структура ще за покупців")

        required = 52
        if session_name == "ASIA":
            required += 6
        if calendar_active:
            required += 8

        if score < required:
            return None

        confidence = min(78, max(58, score))
        return {
            "active": True,
            "side": side,
            "score": confidence if is_long else -confidence,
            "confidence": confidence,
            "signal_type": f"СКАЛЬП {side} / РІЗКИЙ ВІДСКОК" if is_long else f"СКАЛЬП {side} / РІЗКИЙ ВІДКАТ",
            "reason": ("скальп лонг: " if is_long else "скальп шорт: ") + ", ".join(reasons[:3]),
        }

    long_setup = build("LONG")
    short_setup = build("SHORT")
    if long_setup and short_setup:
        return long_setup if abs(long_setup["score"]) >= abs(short_setup["score"]) else short_setup
    return long_setup or short_setup or {"active": False, "side": "NONE", "reason": "скальп-відскок ще не підтверджений"}

def scalp_preparation_signal(tv, tech, smc, orderflow, news=None, event_risk=None):
    """Ukrainian pre-signal: scalp is forming, but entry is not confirmed yet."""
    tech = tech or {}
    smc = smc or {}
    orderflow = orderflow or {}
    news = news or {}
    event_risk = event_risk or {}
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}

    change = tech.get("change", 0) or 0
    rsi5 = tech.get("rsi_5m")
    rsi15 = tech.get("rsi_15m")
    micro_state = micro.get("state", "RANGE")
    micro_bias = micro.get("bias", "NEUTRAL")
    micro_score = micro.get("score", 0) or 0
    book_bias = (orderflow.get("order_book") or {}).get("bias", "NEUTRAL")
    trade_bias = (orderflow.get("real_flow") or {}).get("bias", "NEUTRAL")
    liquidity_bias = (orderflow.get("liquidity_proxy") or {}).get("bias", "NEUTRAL")
    sweep = smc.get("sweep", "NONE")
    choch = smc.get("choch", "NONE")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    absorption = volume.get("поглинання", "NONE")
    news_score = news.get("score", 0) or 0
    event_side = event_risk.get("direction", "MIXED")

    if change <= -0.9:
        score = 0
        reasons = []
        if (rsi5 is not None and rsi5 <= 34) or (rsi15 is not None and rsi15 <= 38):
            score += 10
            reasons.append("після сильного падіння є перепроданість")
        if micro_state in ["SHORT_COOLING", "LONG_STRENGTHENING"] or micro_bias == "LONG" or micro_score >= 18:
            score += 16
            reasons.append("3m почав відкуповувати")
        if book_bias == "LONG" or trade_bias == "LONG" or liquidity_bias == "LONG":
            score += 12
            reasons.append("покупці зʼявились у потоці")
        if sweep.startswith("DOWNSIDE") or choch == "ознака розвороту LONG":
            score += 14
            reasons.append("є ознака зняття низу")
        if vol_bias == "LONG" or absorption == "BULLISH ABSORPTION":
            score += 12
            reasons.append("обсяг за відкуп")
        if news_score >= 25 or event_side == "LONG":
            score += 6
            reasons.append("новини підтримують відскок")
        if score >= 22:
            return {
                "active": True,
                "side": "LONG",
                "status": "СКАЛЬП ГОТУЄТЬСЯ",
                "text": "можливий LONG-відскок",
                "reason": ", ".join(reasons[:2]),
            }

    if change >= 0.9:
        score = 0
        reasons = []
        if (rsi5 is not None and rsi5 >= 66) or (rsi15 is not None and rsi15 >= 62):
            score += 10
            reasons.append("після сильного росту є перекупленість")
        if micro_state in ["LONG_COOLING", "SHORT_STRENGTHENING"] or micro_bias == "SHORT" or micro_score <= -18:
            score += 16
            reasons.append("3m почав продавати")
        if book_bias == "SHORT" or trade_bias == "SHORT" or liquidity_bias == "SHORT":
            score += 12
            reasons.append("продавці зʼявились у потоці")
        if sweep.startswith("UPSIDE") or choch == "ознака розвороту SHORT":
            score += 14
            reasons.append("є ознака зняття верху")
        if vol_bias == "SHORT" or absorption == "BEARISH ABSORPTION":
            score += 12
            reasons.append("обсяг за продаж")
        if news_score <= -25 or event_side == "SHORT":
            score += 6
            reasons.append("новини підтримують відкат")
        if score >= 22:
            return {
                "active": True,
                "side": "SHORT",
                "status": "СКАЛЬП ГОТУЄТЬСЯ",
                "text": "можливий SHORT-відкат",
                "reason": ", ".join(reasons[:2]),
            }

    return {"active": False, "side": "NONE", "status": "", "text": "", "reason": ""}

def proactive_entry_watch(signal, tv, tech, smc, news=None, event_risk=None, early_reversal=None, trade_probability=None):
    """Creates a forward-looking conditional entry plan.

    This is the mode the user wants:
    - not "price already moved";
    - but "prepare entry IF trigger confirms".
    """
    if signal not in ["LONG", "SHORT"]:
        return {"active": False, "side": "NONE", "text": "", "trigger": None, "invalid": None}

    tv = tv or {}
    tech = tech or {}
    smc = smc or {}
    news = news or {}
    event_risk = event_risk or {}
    early_reversal = early_reversal or {}

    price = tv.get("price") or 0
    atr = tech.get("atr_15m") or smc.get("atr") or (price * 0.006 if price else 0)
    if not price or not atr:
        return {"active": False, "side": "NONE", "text": "", "trigger": None, "invalid": None}

    swing_high = smc.get("swing_high")
    swing_low = smc.get("swing_low")
    ema20 = tech.get("ema20_15m")
    tech_side = "LONG" if (tech.get("score", 0) or 0) >= 35 else "SHORT" if (tech.get("score", 0) or 0) <= -35 else "NEUTRAL"
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    probability = trade_probability or 0
    has_reason = (
        probability >= 50
        or (early_reversal.get("active") and early_reversal.get("side") == signal)
        or tech_side == signal
        or (signal == "LONG" and news_score >= 35)
        or (signal == "SHORT" and news_score <= -35)
        or event_side == signal
    )
    if not has_reason:
        return {"active": False, "side": "NONE", "text": "", "trigger": None, "invalid": None}

    # Avoid far-away triggers. The trigger must be close enough to be useful.
    if signal == "LONG":
        raw_trigger = max(price + atr * 0.12, swing_high if swing_high and swing_high > price else price + atr * 0.12)
        if raw_trigger > price * 1.012:
            raw_trigger = price + atr * 0.18

        pullback_zone = ema20 if ema20 and ema20 < price else price - atr * 0.35
        invalid = swing_low if swing_low and swing_low < price else price - atr * 0.65
        trigger = round(raw_trigger, 4)
        pullback_zone = round(pullback_zone, 4)
        invalid = round(invalid, 4)

        text = (
            f"Зараз не входити. "
            f"Лонг можна брати тільки якщо ціна закріпиться вище {trigger} або втримає відкат біля {pullback_zone}. "
            f"Скасування: нижче {invalid}."
        )

    else:
        raw_trigger = min(price - atr * 0.12, swing_low if swing_low and swing_low < price else price - atr * 0.12)
        if raw_trigger < price * 0.988:
            raw_trigger = price - atr * 0.18

        pullback_zone = ema20 if ema20 and ema20 > price else price + atr * 0.35
        invalid = swing_high if swing_high and swing_high > price else price + atr * 0.65
        trigger = round(raw_trigger, 4)
        pullback_zone = round(pullback_zone, 4)
        invalid = round(invalid, 4)

        text = (
            f"Зараз не входити. "
            f"Шорт можна брати тільки якщо ціна закріпиться нижче {trigger} або втримає відкат біля {pullback_zone}. "
            f"Скасування: вище {invalid}."
        )

    return {
        "active": True,
        "side": signal,
        "text": text,
        "trigger": trigger,
        "retest": pullback_zone,
        "invalid": invalid,
    }

def proactive_plan_text(signal, trade_probability, show_trade_plan, plan, entry_watch):
    """Telegram plan text. TRADE only if confirmed; otherwise conditional preparation."""
    if show_trade_plan:
        return format_trade_plan(plan)

    if entry_watch and entry_watch.get("active"):
        return entry_watch.get("text")

    return "WAIT — немає якісного входу; чекати нову умову"

def apply_entry_watch_quality_floor(signal, trade_probability, tech, news, event_risk, smc, entry_watch):
    """ГОТУЄМОСЬ should not be shown as 0/5 when the setup has a real directional reason.

    This does NOT allow a trade. It only makes the displayed quality fair:
    - strong news + neutral/not-opposite tech = at least 2/5 watch;
    - strong news + mild technical support = at least 3/5 watch;
    - if tech is strongly against direction, no floor is applied.
    """
    if signal not in ["LONG", "SHORT"] or trade_probability is None:
        return trade_probability
    if not entry_watch or not entry_watch.get("active"):
        return trade_probability

    tech = tech or {}
    news = news or {}
    event_risk = event_risk or {}
    smc = smc or {}

    tech_score = tech.get("score", 0) or 0
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"
    smc_bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")

    if signal == "LONG":
        tech_strong_against = tech_score <= -55 or smc_bias == "SHORT" or bos == "пробій структури SHORT" or vol_bias == "SHORT"
        news_support = news_score >= 45 or event_side == "LONG"
        mild_tech_support = tech_score >= 15 or smc_bias == "LONG" or bos == "пробій структури LONG" or vol_bias == "LONG"

        if news_support and not tech_strong_against:
            trade_probability = max(trade_probability, 52)  # 2/5 watch
        if news_support and mild_tech_support and not tech_strong_against:
            trade_probability = max(trade_probability, 58)  # 3/5 watch

    elif signal == "SHORT":
        tech_strong_against = tech_score >= 55 or smc_bias == "LONG" or bos == "пробій структури LONG" or vol_bias == "LONG"
        news_support = news_score <= -45 or event_side == "SHORT"
        mild_tech_support = tech_score <= -15 or smc_bias == "SHORT" or bos == "пробій структури SHORT" or vol_bias == "SHORT"

        if news_support and not tech_strong_against:
            trade_probability = max(trade_probability, 52)
        if news_support and mild_tech_support and not tech_strong_against:
            trade_probability = max(trade_probability, 58)

    # ГОТУЄМОСЬ is still not a confirmed trade.
    return min(trade_probability, 64)

def apply_confirmed_trade_quality_floor(signal, trade_probability, tech, news, event_risk, smc, orderflow=None):
    """Fair quality floor for real TRADE setups.

    Difference from ГОТУЄМОСЬ:
    - WATCH can be 2/5–3/5 without full structure confirmation.
    - TRADE needs confirmation: пробій структури/SMC/volume/orderflow/EMA trend.
    - If confirmation exists + news supports + tech is not against, quality can become 4/5–5/5.
    """
    if signal not in ["LONG", "SHORT"] or trade_probability is None:
        return trade_probability

    tech = tech or {}
    news = news or {}
    event_risk = event_risk or {}
    smc = smc or {}
    orderflow = orderflow or {}

    tech_score = tech.get("score", 0) or 0
    trend_5m = tech.get("trend_5m", "UNKNOWN")
    trend_15m = tech.get("trend_15m", "UNKNOWN")
    trend_1h = tech.get("trend_1h", "UNKNOWN")
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    smc_bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    phase = smc.get("phase", "")
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    поглинання = volume.get("поглинання", "NONE")
    order_score = orderflow.get("score", 0) if isinstance(orderflow, dict) else 0

    if signal == "LONG":
        strong_against = (
            tech_score <= -55
            or bos == "пробій структури SHORT"
            or smc_bias == "SHORT"
            or vol_bias == "SHORT"
            or поглинання == "BEARISH ABSORPTION"
        )
        news_support = news_score >= 45 or event_side == "LONG"
        tech_support = tech_score >= 35 or (trend_5m == "UP" and trend_15m == "UP") or order_score >= 15
        structure_confirmed = (
            bos == "пробій структури LONG"
            or smc_bias == "LONG"
            or vol_bias == "LONG"
            or поглинання == "BULLISH ABSORPTION"
            or phase == "BREAKOUT / пробій структури"
        )

        if strong_against:
            return min(trade_probability, 54)

        if news_support and structure_confirmed and tech_score >= 0:
            trade_probability = max(trade_probability, 66)  # 4/5 working trade
        if news_support and structure_confirmed and tech_support:
            trade_probability = max(trade_probability, 72)  # strong 4/5
        if news_support and structure_confirmed and tech_support and trend_1h == "UP":
            trade_probability = max(trade_probability, 76)  # 5/5 confirmed

    elif signal == "SHORT":
        strong_against = (
            tech_score >= 55
            or bos == "пробій структури LONG"
            or smc_bias == "LONG"
            or vol_bias == "LONG"
            or поглинання == "BULLISH ABSORPTION"
        )
        news_support = news_score <= -45 or event_side == "SHORT"
        tech_support = tech_score <= -35 or (trend_5m == "DOWN" and trend_15m == "DOWN") or order_score <= -15
        structure_confirmed = (
            bos == "пробій структури SHORT"
            or smc_bias == "SHORT"
            or vol_bias == "SHORT"
            or поглинання == "BEARISH ABSORPTION"
            or phase == "BREAKOUT / пробій структури"
        )

        if strong_against:
            return min(trade_probability, 54)

        if news_support and structure_confirmed and tech_score <= 0:
            trade_probability = max(trade_probability, 66)
        if news_support and structure_confirmed and tech_support:
            trade_probability = max(trade_probability, 72)
        if news_support and structure_confirmed and tech_support and trend_1h == "DOWN":
            trade_probability = max(trade_probability, 76)

    return min(trade_probability, 82)

# ==========================================================
# VOLATILITY REGIME / LIQUIDATION HEATMAP LOGIC / SYNTHETIC OI
# ==========================================================

def analyze_volatility_regime(tv, tech):
    price = tv.get("price") or 0
    atr = tech.get("atr_15m") or (price * 0.006 if price else 0)
    adx = tech.get("adx_15m") or 0
    change = abs(tech.get("change") or 0)
    rsi5 = tech.get("rsi_5m") or 50
    rsi15 = tech.get("rsi_15m") or 50

    atr_pct = (atr / price * 100) if price else 0
    score = 0
    regime = "NORMAL"
    direction_filter = "NEUTRAL"
    warning = "Нормальна волатильність"

    if atr_pct >= 1.2 or change >= 2.0:
        regime = "HIGH VOLATILITY / BREAKOUT MODE"
        score += 8 if tech.get("momentum") in ["STRONG UP", "VERY STRONG UP"] else 0
        score -= 8 if tech.get("momentum") in ["STRONG DOWN", "VERY STRONG DOWN"] else 0
        warning = "Висока волатильність: краще входити тільки після ретесту"
    elif atr_pct <= 0.35 and adx < 18:
        regime = "LOW VOLATILITY / CHOP MODE"
        score -= 10
        warning = "Низька волатильність і слабкий тренд: ризик флету"
    elif adx >= 25:
        regime = "TREND MODE"
        if tech.get("trend") == "UP":
            score += 10
            direction_filter = "LONG"
        elif tech.get("trend") == "DOWN":
            score -= 10
            direction_filter = "SHORT"
        warning = "Трендовий режим"

    if rsi5 > 78 or rsi15 > 78:
        score -= 8
        warning += "; є ризик перегріву LONG"
    if rsi5 < 22 or rsi15 < 22:
        score += 8
        warning += "; є ризик відскоку проти SHORT"

    return {
        "score": score,
        "regime": regime,
        "atr_pct": round(atr_pct, 3),
        "direction_filter": direction_filter,
        "warning": warning,
    }

def analyze_liquidation_heatmap(tv, tech, volatility):
    price = tv.get("price") or 0
    atr = tech.get("atr_15m") or price * 0.006
    momentum = tech.get("momentum", "NEUTRAL")

    # Approximate liquidation clusters. This is NOT real exchange heatmap,
    # but useful free logic for likely leverage liquidation zones.
    long_10x = price * 0.90
    long_20x = price * 0.95
    long_50x = price * 0.98
    short_10x = price * 1.10
    short_20x = price * 1.05
    short_50x = price * 1.02

    nearest_long_liq = max([long_10x, long_20x, long_50x])
    nearest_short_liq = min([short_10x, short_20x, short_50x])

    dist_long_atr = abs(price - nearest_long_liq) / atr if atr else 99
    dist_short_atr = abs(nearest_short_liq - price) / atr if atr else 99

    score = 0
    bias = "NEUTRAL"
    summary = "Ліквідаційні зони далеко"

    if momentum in ["STRONG UP", "VERY STRONG UP"] and dist_short_atr <= 3.5:
        score += 12
        bias = "SHORT SQUEEZE RISK / LONG"
        summary = "Ціна рухається до short-liquidation зони — можливий squeeze вгору"
    elif momentum in ["STRONG DOWN", "VERY STRONG DOWN"] and dist_long_atr <= 3.5:
        score -= 12
        bias = "LONG LIQUIDATION RISK / SHORT"
        summary = "Ціна рухається до long-liquidation зони — можливий cascade вниз"

    if volatility.get("regime") == "HIGH VOLATILITY / BREAKOUT MODE":
        if bias.startswith("SHORT SQUEEZE"):
            score += 6
        elif bias.startswith("LONG LIQUIDATION"):
            score -= 6

    return {
        "score": score,
        "bias": bias,
        "nearest_long_liq": round(nearest_long_liq, 4),
        "nearest_short_liq": round(nearest_short_liq, 4),
        "dist_long_atr": round(dist_long_atr, 2),
        "dist_short_atr": round(dist_short_atr, 2),
        "summary": summary,
    }

def analyze_market_structure(tv, tech):
    # Volume Profile is intentionally disabled: Binance candles are often blocked on GitHub runners.
    # We keep only stable free modules: volatility regime + liquidation zone logic.
    volatility = analyze_volatility_regime(tv, tech)
    liquidation = analyze_liquidation_heatmap(tv, tech, volatility)
    total_score = volatility["score"] + liquidation["score"]

    return {
        "score": total_score,
        "volatility": volatility,
        "liquidation": liquidation,
        "candles_count": 0,
    }

def market_structure_verdict(market):
    score = market.get("score", 0)
    if score >= 15:
        side = "LONG"
    elif score <= -15:
        side = "SHORT"
    else:
        side = "NEUTRAL"

    reason = (
        f"volatility {market['volatility']['regime']}, "
        f"liquidation {market['liquidation']['bias']}, score {score}"
    )
    return side, reason

# ==========================================================
# SYNTHETIC OPEN INTEREST PROXY (NO EXTERNAL API)
# ==========================================================

def analyze_synthetic_open_interest(tv, tech, orderflow, market):
    """
    Stable replacement for Binance/Bybit OI.
    It does NOT call blocked exchange APIs. It estimates positioning pressure from:
    - price momentum
    - TradingView volume
    - ATR/volatility regime
    - orderflow proxy
    - liquidation-zone logic
    """
    price = tv.get("price") or 0
    change = tech.get("change") or 0
    volume = tv.get("volume") or 0
    atr = tech.get("atr_15m") or (price * 0.006 if price else 0)
    momentum = tech.get("momentum", "NEUTRAL")
    vol_regime = market.get("volatility", {}).get("regime", "NORMAL")
    liquidation_bias = market.get("liquidation", {}).get("bias", "NEUTRAL")
    order_score = orderflow.get("score", 0)

    score = 0
    side = "NEUTRAL"
    notes = []

    # Strong directional move + volume = likely new participation.
    if momentum in ["STRONG UP", "VERY STRONG UP"]:
        if volume and abs(change) >= 1.2:
            score += 12
            side = "LONG"
            notes.append("synthetic OI: ймовірний LONG buildup")
        if liquidation_bias.startswith("SHORT SQUEEZE"):
            score += 8
            side = "LONG"
            notes.append("short squeeze pressure")

    elif momentum in ["STRONG DOWN", "VERY STRONG DOWN"]:
        if volume and abs(change) >= 1.2:
            score -= 12
            side = "SHORT"
            notes.append("synthetic OI: ймовірний SHORT buildup")
        if liquidation_bias.startswith("LONG LIQUIDATION"):
            score -= 8
            side = "SHORT"
            notes.append("long liquidation pressure")

    # Orderflow confirmation.
    if order_score >= 20:
        score += 6
        side = "LONG" if score > 0 else side
        notes.append("orderflow підтверджує покупців")
    elif order_score <= -20:
        score -= 6
        side = "SHORT" if score < 0 else side
        notes.append("orderflow підтверджує продавців")

    # High volatility means continuation is possible but more dangerous.
    if vol_regime == "HIGH VOLATILITY / BREAKOUT MODE":
        if score > 0:
            score += 4
        elif score < 0:
            score -= 4
        notes.append("high volatility participation")

    # If no strong pressure, keep it neutral.
    if abs(score) < 8:
        score = 0
        side = "NEUTRAL"
        notes.append("synthetic OI нейтральний")

    if side == "LONG":
        summary = "Synthetic OI: LONG BUILDUP"
    elif side == "SHORT":
        summary = "Synthetic OI: SHORT BUILDUP"
    else:
        summary = "Synthetic OI: NEUTRAL"

    return {
        "score": score,
        "side": side,
        "summary": summary,
        "details": "; ".join(notes[:3]),
    }

# ==========================================================
# SESSION + REVERSAL WATCH LAYER
# ==========================================================

def analyze_session_context():
    """Session analysis without any external API. Uses UTC time from GitHub runner."""
    now = now_utc()
    hour = now.hour

    if 0 <= hour < 7:
        session = "ASIA"
        score = -6
        note = "Asia session: нижча ліквідність, вищий ризик fake breakout"
        breakout_quality = "LOW"
    elif 7 <= hour < 13:
        session = "LONDON"
        score = 0
        note = "London session: можливі liquidity sweep / stop hunt"
        breakout_quality = "MEDIUM"
    elif 13 <= hour < 21:
        session = "NEW YORK"
        score = 8
        note = "New York session: найкраща реакція на oil/macro/news"
        breakout_quality = "HIGH"
    else:
        session = "LATE US / TRANSITION"
        score = -3
        note = "Пізня сесія: нижча якість continuation-сигналів"
        breakout_quality = "MEDIUM-LOW"

    return {
        "session": session,
        "score": score,
        "note": note,
        "breakout_quality": breakout_quality,
        "utc_hour": hour,
    }

def session_telegram_text(session):
    name = (session or {}).get("session", "UNKNOWN")
    if name == "ASIA":
        return "Сесія: ASIA — обережно, нижча ліквідність"
    if name == "LONDON":
        return "Сесія: LONDON — можливі різкі вибивання"
    if name == "NEW YORK":
        return "Сесія: NEW YORK — новини рухають сильніше"
    if name == "LATE US / TRANSITION":
        return "Сесія: пізня US — якість входів нижча"
    return ""

def economic_calendar_text(event_risk):
    calendar = (event_risk or {}).get("calendar") or {}
    events = calendar.get("events") or []
    if not events:
        return ""

    parts = []
    for event in events[:2]:
        name = event.get("name", "Подія")
        status = event.get("status") or "скоро"
        if name == "EIA":
            parts.append(f"EIA запаси — {status}")
        elif name == "Fed":
            parts.append(f"Fed — {status}")
        elif name == "OPEC":
            parts.append(f"OPEC — {status}")
        else:
            parts.append(f"{event.get('title', name)} — {status}")

    hard = bool(calendar.get("hard_block"))
    suffix = " — чекати реакцію" if hard else " — просто мати на увазі"
    return "Календар: " + "; ".join(parts) + suffix

def analyze_reversal_watch(tv, tech, news, event_risk, orderflow, market, oi_analysis, session):
    """Detects possible reversal setups such as breakdown failure / liquidity sweep.

    This is a watch-layer, not an automatic entry trigger. It improves decision text:
    - TECH SHORT + FUND LONG + oversold/weak downside = REVERSAL LONG WATCH
    - TECH LONG + FUND SHORT + overbought/weak upside = REVERSAL SHORT WATCH
    """
    score = 0
    side = "NONE"
    reasons = []

    price_change = tech.get("change", 0) or 0
    trend = tech.get("trend", "MIXED")
    momentum = tech.get("momentum", "NEUTRAL")
    rsi_5m = tech.get("rsi_5m")
    rsi_15m = tech.get("rsi_15m")
    news_score = news.get("score", 0)
    event_dir = event_risk.get("direction", "NEUTRAL")
    event_risk_level = event_risk.get("risk", "НОРМАЛЬНИЙ")
    liq_bias = market.get("liquidation", {}).get("bias", "NEUTRAL")
    vol_regime = market.get("volatility", {}).get("regime", "NORMAL")
    oi_side = oi_analysis.get("side", "NEUTRAL")
    order_score = orderflow.get("score", 0)

    oversold = (rsi_5m is not None and rsi_5m <= 28) or (rsi_15m is not None and rsi_15m <= 32)
    overbought = (rsi_5m is not None and rsi_5m >= 72) or (rsi_15m is not None and rsi_15m >= 68)

    # Possible breakdown failure / bullish reversal watch.
    if trend == "DOWN" and news_score >= 30 and event_dir == "LONG":
        score += 28
        side = "REVERSAL LONG WATCH"
        reasons.append("техніка SHORT, але фундамент/події сильно LONG")

        if oversold:
            score += 14
            reasons.append("RSI показує перепроданість — можливий squeeze/відскок")

        if momentum in ["NEUTRAL", "STRONG DOWN"] and price_change > -1.0:
            score += 10
            reasons.append("падіння слабшає — можливий breakdown failure")

        if liq_bias.startswith("LONG LIQUIDATION") or oi_side == "SHORT":
            score += 8
            reasons.append("після long liquidation можливий різкий відскок")

        if session.get("session") in ["LONDON", "NEW YORK"]:
            score += 6
            reasons.append(f"{session.get('session')} session може дати reversal після sweep")

    # Possible upside failure / bearish reversal watch.
    if trend == "UP" and news_score <= -25 and event_dir == "SHORT":
        score -= 28
        side = "REVERSAL SHORT WATCH"
        reasons.append("техніка LONG, але фундамент/події сильно SHORT")

        if overbought:
            score -= 14
            reasons.append("RSI показує перекупленість — можливий dump/відкат")

        if momentum in ["NEUTRAL", "STRONG UP"] and price_change < 1.0:
            score -= 10
            reasons.append("ріст слабшає — можливий breakout failure")

        if liq_bias.startswith("SHORT SQUEEZE") or oi_side == "LONG":
            score -= 8
            reasons.append("після squeeze можливий відкат")

        if session.get("session") in ["LONDON", "NEW YORK"]:
            score -= 6
            reasons.append(f"{session.get('session')} session може дати reversal після sweep")

    # Liquidity sweep proxy: extreme RSI + conflict + high event risk.
    sweep = "NONE"
    if oversold and news_score > 25 and event_dir == "LONG" and event_risk_level in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        sweep = "DOWNSIDE SWEEP / LONG WATCH"
        if side == "NONE":
            side = "REVERSAL LONG WATCH"
        score += 10
        reasons.append("liquidity sweep proxy: перепроданість + bullish event risk")
    elif overbought and news_score < -20 and event_dir == "SHORT" and event_risk_level in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        sweep = "UPSIDE SWEEP / SHORT WATCH"
        if side == "NONE":
            side = "REVERSAL SHORT WATCH"
        score -= 10
        reasons.append("liquidity sweep proxy: перекупленість + bearish event risk")

    confidence = min(95, abs(score))
    if side == "NONE" or confidence < 30:
        side = "NONE"
        confidence = 0
        if not reasons:
            reasons.append("reversal setup не підтверджений")

    return {
        "side": side,
        "score": score,
        "confidence": confidence,
        "sweep": sweep,
        "reason": "; ".join(reasons[:3]),
        "volatility": vol_regime,
    }

def analyze_priority_engine(tech, news, event_risk, macro, orderflow, market, session, reversal):
    """Dynamic priority engine for oil/BZ.
    In oil, news/event flow can dominate technicals during geopolitical/macro events.
    In quiet markets, technicals and market structure get higher priority.
    """
    event_level = event_risk.get("risk", "НОРМАЛЬНИЙ")
    event_dir = event_risk.get("direction", "MIXED")
    news_score = news.get("score", 0)
    tech_score = tech.get("score", 0)
    session_name = session.get("session", "UNKNOWN") if session else "UNKNOWN"
    reversal_side = reversal.get("side", "NONE") if reversal else "NONE"

    regime = "BALANCED"
    dominant = "BALANCED"
    tech_weight = 1.0
    news_weight = 1.0
    reason = "Техніка і фундамент мають приблизно однакову вагу"

    high_event = event_level in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]
    strong_news = abs(news_score) >= 35
    clear_event_direction = event_dir in ["LONG", "SHORT"]

    if high_event and strong_news and clear_event_direction:
        regime = "NEWS / EVENT DOMINANT"
        dominant = "FUNDAMENTAL"
        tech_weight = 0.65
        news_weight = 1.45
        reason = "Новини/події домінують над технікою: oil сильно реагує на OPEC, Iran, EIA, Fed, sanctions"
    elif high_event and clear_event_direction:
        regime = "EVENT RISK DOMINANT"
        dominant = "EVENT"
        tech_weight = 0.75
        news_weight = 1.25
        reason = "Подієвий ризик високий: технічні сигнали потрібно підтверджувати обережніше"
    elif abs(news_score) < 15 and event_level in ["НИЗЬКИЙ", "НОРМАЛЬНИЙ", "НЕЙТРАЛЬНИЙ"]:
        regime = "TECHNICAL DOMINANT"
        dominant = "TECHNICAL"
        tech_weight = 1.25
        news_weight = 0.75
        reason = "Новинний фон слабкий: техніка має більший пріоритет"

    # Session modifier: New York is where oil/news reactions are most valid.
    if session_name == "NEW YORK" and dominant in ["FUNDAMENTAL", "EVENT"]:
        news_weight += 0.15
        reason += "; New York session підсилює реакцію на oil/macro/news"
    elif session_name == "ASIA":
        tech_weight -= 0.10
        reason += "; Asia session: підвищений ризик фейкових рухів"

    # Reversal watch means technical trend can be late versus news/event flow.
    if reversal_side in ["REVERSAL LONG WATCH", "REVERSAL SHORT WATCH"]:
        regime += " + REVERSAL WATCH"
        reason += "; є ознаки можливого розвороту, тому continuation-сигнали треба фільтрувати"

    technical_component = tech_score * tech_weight
    fundamental_component = (news_score + macro.get("score", 0)) * news_weight

    if event_dir == "LONG":
        fundamental_component += 18 * news_weight
    elif event_dir == "SHORT":
        fundamental_component -= 18 * news_weight

    priority_score = int(technical_component + fundamental_component)

    return {
        "regime": regime,
        "dominant": dominant,
        "tech_weight": round(tech_weight, 2),
        "news_weight": round(news_weight, 2),
        "priority_score": priority_score,
        "reason": reason,
    }

# ==========================================================
# EARLY WARNING / CONTEXT TRUST ENGINE
# ==========================================================

def analyze_early_warning(tv, tech, news, event_risk, orderflow, market, oi_analysis, session):
    """
    Ранній фільтр перед різким рухом.
    Мета: не чекати великого дампу/пампу, а попередити, коли ціна вже НЕ підтверджує новини.
    Особливо важливо для Brent/oil: новини можуть бути LONG, але якщо ціна їх ігнорує і техніка продавлюється,
    пріоритет тимчасово переходить до price action.
    """
    price_change = tech.get("change", 0) or 0
    tech_score = tech.get("score", 0)
    news_score = news.get("score", 0)
    trend = tech.get("trend", "MIXED")
    trend_5m = tech.get("trend_5m", "UNKNOWN")
    trend_15m = tech.get("trend_15m", "UNKNOWN")
    trend_1h = tech.get("trend_1h", "UNKNOWN")
    momentum = tech.get("momentum", "NEUTRAL")
    order_score = orderflow.get("score", 0)
    oi_side = oi_analysis.get("side", "NEUTRAL")
    vol_regime = market.get("volatility", {}).get("regime", "NORMAL")
    event_direction = event_risk.get("direction", "MIXED")
    event_level = event_risk.get("risk", "НОРМАЛЬНИЙ")
    session_name = session.get("session", "UNKNOWN") if session else "UNKNOWN"

    warning = "NONE"
    side = "NEUTRAL"
    score = 0
    trust = "BALANCED"
    reason = "Немає раннього попередження"

    bullish_news = news_score >= 30 or event_direction == "LONG"
    bearish_news = news_score <= -25 or event_direction == "SHORT"
    high_event = event_level in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]

    # LONG news failure: новини bullish, але ціна не росте / техніка провалюється.
    long_news_ignored = (
        bullish_news
        and tech_score <= -45
        and trend_5m == "DOWN"
        and trend_15m == "DOWN"
        and price_change <= -0.25
        and order_score <= 5
    )

    # SHORT news failure: новини bearish, але ціна не падає / техніка вгору.
    short_news_ignored = (
        bearish_news
        and tech_score >= 45
        and trend_5m == "UP"
        and trend_15m == "UP"
        and price_change >= 0.25
        and order_score >= -5
    )

    # Early dump before full shock: ще не обвал, але продавлювання вже видно.
    early_dump = (
        tech_score <= -55
        and trend_5m == "DOWN"
        and trend_15m == "DOWN"
        and price_change <= -0.35
        and momentum in ["NEUTRAL", "STRONG DOWN", "VERY STRONG DOWN"]
    )

    # Early pump before full breakout.
    early_pump = (
        tech_score >= 55
        and trend_5m == "UP"
        and trend_15m == "UP"
        and price_change >= 0.35
        and momentum in ["NEUTRAL", "STRONG UP", "VERY STRONG UP"]
    )

    if long_news_ignored or early_dump:
        warning = "EARLY DUMP WARNING"
        side = "SHORT"
        score = -35
        trust = "PRICE ACTION / TECH"
        reason = "Новини можуть бути LONG, але ціна їх не підтверджує: 5m/15m вниз, покупців не видно"
        if high_event:
            reason += "; подієвий ризик високий — не ловити LONG проти падіння"
        if session_name in ["LONDON", "NEW YORK"]:
            score -= 6
            reason += f"; {session_name} може прискорити рух"
        if vol_regime == "HIGH VOLATILITY / BREAKOUT MODE":
            score -= 6
            reason += "; висока волатильність"

    elif short_news_ignored or early_pump:
        warning = "EARLY PUMP WARNING"
        side = "LONG"
        score = 35
        trust = "PRICE ACTION / TECH"
        reason = "Новини можуть бути SHORT, але ціна їх не підтверджує: 5m/15m вгору, продавців не видно"
        if high_event:
            reason += "; подієвий ризик високий — не шортити проти імпульсу"
        if session_name in ["LONDON", "NEW YORK"]:
            score += 6
            reason += f"; {session_name} може прискорити рух"
        if vol_regime == "HIGH VOLATILITY / BREAKOUT MODE":
            score += 6
            reason += "; висока волатильність"

    # If synthetic OI/orderflow strongly confirms, strengthen warning.
    if warning == "EARLY DUMP WARNING" and (oi_side == "SHORT" or order_score <= -20):
        score -= 10
        reason += "; OI/orderflow підтверджує продавців"
    elif warning == "EARLY PUMP WARNING" and (oi_side == "LONG" or order_score >= 20):
        score += 10
        reason += "; OI/orderflow підтверджує покупців"

    return {
        "warning": warning,
        "side": side,
        "score": score,
        "trust": trust,
        "reason": reason,
    }

def decide_current_priority(tech, news, event_risk, orderflow, early_warning):
    """
    Кому зараз довіряти більше:
    - якщо новини сильні, але ціна їх не підтверджує -> TECH/PRICE ACTION
    - якщо техніка нейтральна, а подія дуже сильна -> NEWS/EVENT
    - якщо все в один бік -> ALIGNMENT
    """
    tech_score = tech.get("score", 0)
    news_score = news.get("score", 0)
    event_dir = event_risk.get("direction", "MIXED")
    order_score = orderflow.get("score", 0)

    if early_warning.get("warning") != "NONE":
        return "PRICE ACTION", "Новини не підтверджуються ціною — важливіша реакція графіка"

    if abs(news_score) >= 35 and event_dir in ["LONG", "SHORT"] and abs(tech_score) < 45:
        return "NEWS/EVENT", "Техніка ще не сильна, але новини/події домінують"

    if tech_score >= 55 and news_score >= 20:
        return "ALIGNMENT LONG", "Техніка і новини підтримують LONG"
    if tech_score <= -55 and news_score <= -20:
        return "ALIGNMENT SHORT", "Техніка і новини підтримують SHORT"

    if abs(tech_score) >= 70 and abs(news_score) < 25:
        return "TECH", "Новини слабкі, тому пріоритет у техніки"

    return "BALANCED", "Ринок змішаний — потрібне підтвердження"

# ==========================================================
# WEEKEND / RR / CHASE / POSITION / CROSS-MARKET HELPERS
# ==========================================================

def analyze_weekend_mode():
    wd = now_utc().weekday()
    if wd == 5:
        return {"active": True, "label": "СУБОТА", "score": -18, "note": "Вихідний: нижча ліквідність, сигнали менш надійні."}
    if wd == 6:
        return {"active": True, "label": "НЕДІЛЯ", "score": -22, "note": "Вихідний: краще відкривати тільки дуже сильні сетапи."}
    return {"active": False, "label": "РОБОЧИЙ ДЕНЬ", "score": 0, "note": "Звичайний торговий день."}

def get_cross_market_data():
    result = {}
    for name, symbol, screener in [
        ("BTC", "BINANCE:BTCUSDT", "crypto"),
        ("DXY", "TVC:DXY", "cfd"),
        ("SPX", "SP:SPX", "cfd"),
        ("GOLD", "TVC:GOLD", "cfd"),
    ]:
        values = get_tradingview_scan(symbol, screener, ["close", "change", "Recommend.All|15"])
        if not values:
            continue
        try:
            result[name] = {
                "price": float(values[0]) if values[0] is not None else None,
                "change": float(values[1]) if values[1] is not None else 0.0,
                "rec15": float(values[2]) if values[2] is not None else 0.0,
            }
        except Exception:
            pass
    return result

def analyze_cross_market(cross, tech):
    if not cross:
        return {"score": 0, "bias": "NEUTRAL", "note": "нейтрально", "data": {}}

    btc = cross.get("BTC", {}).get("change", 0) or 0
    dxy = cross.get("DXY", {}).get("change", 0) or 0
    spx = cross.get("SPX", {}).get("change", 0) or 0
    gold = cross.get("GOLD", {}).get("change", 0) or 0
    oil = tech.get("change", 0) or 0

    score = 0
    notes = []

    if oil < -0.5 and btc < -0.4 and spx < -0.2:
        score -= 10
        notes.append("oil/BTC/SPX слабкі — risk-off")
    if oil > 0.5 and btc < -0.4:
        score += 6
        notes.append("oil росте проти BTC — oil/news драйвер")
    if dxy > 0.15:
        score -= 6
        notes.append("DXY росте — тиск на ризик")
    elif dxy < -0.15:
        score += 5
        notes.append("DXY слабшає — легше для commodities")
    if gold > 0.4 and btc < 0:
        score -= 3
        notes.append("gold strong/BTC weak — захисний режим")

    if score >= 8:
        bias = "LONG SUPPORT"
    elif score <= -8:
        bias = "SHORT PRESSURE"
    else:
        bias = "NEUTRAL"

    return {
        "score": score,
        "bias": bias,
        "note": "; ".join(notes[:2]) if notes else "нейтрально",
        "data": {"BTC": round(btc, 2), "DXY": round(dxy, 2), "SPX": round(spx, 2), "GOLD": round(gold, 2)},
    }

def adjust_plan_for_rr(plan, signal):
    # SMC HYBRID plan already includes liquidity + minimum RR targets.
    if isinstance(plan, dict) and str(plan.get("method", "")).startswith("SMC HYBRID"):
        return plan
    if not plan or not isinstance(plan, dict) or plan.get("entry") is None:
        return plan
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    risk = abs(entry - stop)
    if risk <= 0:
        return plan
    if signal == "LONG":
        plan["tp1"] = round(entry + risk * 1.0, 4)
        plan["tp2"] = round(entry + risk * 1.5, 4)
        plan["tp3"] = round(entry + risk * 2.0, 4)
    elif signal == "SHORT":
        plan["tp1"] = round(entry - risk * 1.0, 4)
        plan["tp2"] = round(entry - risk * 1.5, 4)
        plan["tp3"] = round(entry - risk * 2.0, 4)
    return plan

def rr_metrics(plan):
    if not plan or not isinstance(plan, dict) or plan.get("entry") is None:
        return {"rr1": None, "rr2": None, "ok": True, "note": ""}
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    tp1 = float(plan["tp1"])
    tp2 = float(plan["tp2"])
    risk = abs(entry - stop)
    if risk <= 0:
        return {"rr1": None, "rr2": None, "ok": False, "note": "RR помилка"}
    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    return {"rr1": round(rr1, 2), "rr2": round(rr2, 2), "ok": rr1 >= 1.2 or rr2 >= 1.8, "note": f"RR1 {round(rr1,2)} / RR2 {round(rr2,2)}"}

def early_entry_signal(tv, signal, signal_type, tech, smc, orderflow, news, event_risk, market=None, session=None):
    """Early LONG/SHORT trigger before the move becomes a late chase.

    Goal: catch the first quality continuation/breakdown moment, not the move
    after it already travelled too far. It still blocks weak entries near major
    calendar/news risk.
    """
    tech = tech or {}
    smc = smc or {}
    orderflow = orderflow or {}
    news = news or {}
    event_risk = event_risk or {}
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}

    price = (tv or {}).get("price")
    change = tech.get("change", 0) or 0
    abs_change = abs(change)
    tech_score = tech.get("score", 0) or 0
    trend_5m = tech.get("trend_5m", "UNKNOWN")
    trend_15m = tech.get("trend_15m", "UNKNOWN")
    trend_1h = tech.get("trend_1h", "UNKNOWN")
    rsi5 = tech.get("rsi_5m")
    rsi15 = tech.get("rsi_15m")
    ema20 = tech.get("ema20_15m")
    order_score = orderflow.get("score", 0) or 0
    news_score = news.get("score", 0) or 0
    event_side = event_risk.get("direction", "MIXED")
    calendar_active = bool((event_risk.get("calendar") or {}).get("active"))
    event_high = event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]
    session_name = (session or {}).get("session", "")
    volatility = (market or {}).get("volatility", {}).get("regime", "NORMAL")

    if abs_change >= 1.85:
        return {"active": False, "side": "NONE", "reason": "рух уже запізний"}
    if volatility == "LOW VOLATILITY / CHOP MODE":
        return {"active": False, "side": "NONE", "reason": "боковик, ранній вхід слабкий"}

    def side_setup(side):
        is_long = side == "LONG"
        score_ok = tech_score >= 36 if is_long else tech_score <= -36
        trend_ok = (
            trend_5m == "UP" and trend_15m == "UP"
            if is_long else
            trend_5m == "DOWN" and trend_15m == "DOWN"
        )
        micro_ok = (
            micro.get("bias") == "LONG" or micro.get("state") == "LONG_STRENGTHENING"
            if is_long else
            micro.get("bias") == "SHORT" or micro.get("state") == "SHORT_STRENGTHENING"
        )
        smc_ok = (
            smc.get("bias") == "LONG" or smc.get("bos") == "пробій структури LONG"
            if is_long else
            smc.get("bias") == "SHORT" or smc.get("bos") == "пробій структури SHORT"
        )
        order_ok = order_score >= 3 if is_long else order_score <= -3
        price_ema_ok = True
        if price and ema20:
            price_ema_ok = price > ema20 if is_long else price < ema20
        rsi_ok = True
        if rsi5 is not None and rsi15 is not None:
            rsi_ok = (rsi5 < 74 and rsi15 < 70) if is_long else (rsi5 > 26 and rsi15 > 30)
        early_move_ok = (0.10 <= change <= 1.45) if is_long else (-1.45 <= change <= -0.10)
        not_against_1h = trend_1h != ("DOWN" if is_long else "UP")

        confirmations = [
            score_ok,
            trend_ok,
            micro_ok,
            smc_ok,
            order_ok,
            price_ema_ok,
            early_move_ok,
            rsi_ok,
            not_against_1h,
        ]
        count = sum(1 for item in confirmations if item)
        required = 6
        if (micro_ok and smc_ok and trend_ok and score_ok and early_move_ok and rsi_ok):
            required = 5
        if session_name == "ASIA":
            required = 7

        hard_news_against = (
            (is_long and event_side == "SHORT" and news_score <= -35) or
            ((not is_long) and event_side == "LONG" and news_score >= 35)
        )

        if count >= required and score_ok and trend_ok and early_move_ok and rsi_ok and (micro_ok or smc_ok or order_ok):
            confidence = min(82, 58 + count * 3 + (5 if smc_ok else 0) + (4 if micro_ok else 0))
            if hard_news_against:
                confidence -= 7
            label = "ранній лонг" if is_long else "ранній шорт"
            details = []
            if trend_ok:
                details.append("5m/15m уже в один бік")
            if micro_ok:
                details.append("3m підтверджує")
            if smc_ok:
                details.append("структура підтверджує")
            if order_ok:
                details.append("угоди/стакан допомагають")
            return {
                "active": True,
                "side": side,
                "score": confidence if is_long else -confidence,
                "confidence": confidence,
                "signal_type": f"РАННІЙ {side} / ВХІД ЗАРАЗ",
                "reason": f"{label}: " + ", ".join(details[:3]),
                "confirmations": count,
            }
        return None

    candidates = []
    for side in ["LONG", "SHORT"]:
        result = side_setup(side)
        if result:
            candidates.append(result)

    if not candidates:
        return {"active": False, "side": "NONE", "reason": "ранній вхід ще не підтверджений"}

    candidates.sort(key=lambda item: abs(item.get("score", 0)), reverse=True)
    best = candidates[0]

    # Do not flip a strong confirmed signal unless early setup is clearly stronger.
    if signal in ["LONG", "SHORT"] and signal != best["side"] and abs(tech_score) < 70:
        return {"active": False, "side": "NONE", "reason": "ранній сигнал конфліктує з основним"}

    return best

def news_event_trade_block(signal, trade_probability, event_risk, news, session=None):
    """News/calendar is a caution layer, not a hard entry ban."""
    return {"blocked": False, "reason": ""}

def analyze_chase_protection(signal, tech, market):
    change = abs(tech.get("change", 0) or 0)
    rsi5 = tech.get("rsi_5m") or 50
    rsi15 = tech.get("rsi_15m") or 50
    vol = market.get("volatility", {}).get("regime", "NORMAL")
    extended = False
    reason = ""

    if signal == "LONG" and (change >= 1.4 or rsi5 >= 72 or rsi15 >= 68):
        extended = True
        reason = "LONG після сильного росту — краще чекати відкат/ретест."
    elif signal == "SHORT" and (change >= 1.4 or rsi5 <= 28 or rsi15 <= 32):
        extended = True
        reason = "SHORT після сильного падіння — краще чекати відкат/ретест."

    if extended and vol == "HIGH VOLATILITY / BREAKOUT MODE":
        reason += " Висока волатильність підсилює ризик відскоку."

    return {"extended": extended, "reason": reason}

def position_management_note(signal, plan, tech, news, event_risk, reversal):
    rev_side = (reversal or {}).get("side", "NONE")
    if signal == "LONG":
        if rev_side == "REVERSAL SHORT WATCH" or tech.get("trend") == "DOWN":
            return "Якщо вже в LONG: стоп обовʼязково; при слабкості 5m/15m краще скоротити або вийти."
        return "Якщо вже в LONG: після TP1 частково фіксувати і підтягнути стоп."
    if signal == "SHORT":
        if rev_side == "REVERSAL LONG WATCH" or event_risk.get("direction") == "LONG":
            return "Якщо вже в SHORT: стоп обовʼязково; bullish-новини можуть дати різкий відскок."
        return "Якщо вже в SHORT: після TP1 частково фіксувати і підтягнути стоп."
    if event_risk.get("direction") == "LONG" and news.get("score", 0) >= 30:
        return "Якщо вже в LONG: тримати тільки зі стопом; якщо техніка не підтвердить — скоротити/вийти."
    if event_risk.get("direction") == "SHORT" and news.get("score", 0) <= -20:
        return "Якщо вже в SHORT: тримати тільки зі стопом; якщо техніка не підтвердить — скоротити/вийти."
    return "Якщо вже в позиції: не усереднювати; чекати підтвердження або виходити при зламі сетапу."

# ==========================================================
# SIGNAL ENGINE
# ==========================================================

def build_signal(tech, news, orderflow, macro, event_risk, market, oi_analysis, session, reversal, priority=None, early_warning=None, trust_mode=None):

    if priority is None:
        priority = analyze_priority_engine(tech, news, event_risk, macro, orderflow, market, session, reversal)
    if early_warning is None:
        early_warning = {"warning": "NONE", "side": "NEUTRAL", "score": 0, "trust": "BALANCED", "reason": ""}
    if trust_mode is None:
        trust_mode = "BALANCED"

    
    early_warning = analyze_early_warning(None, tech, news, event_risk, orderflow, market, oi_analysis, session)
    trust_mode, trust_reason = decide_current_priority(tech, news, event_risk, orderflow, early_warning)
# Base score remains visible for logs, but the signal engine now uses dynamic priority.
    # When oil is in a high event/news regime, news/events get more weight.
    score = (
        tech["score"] * priority.get("tech_weight", 1.0)
        + news["score"] * priority.get("news_weight", 1.0)
        + orderflow["score"]
        + macro["score"] * priority.get("news_weight", 1.0)
        + event_risk["score"]
        + market["score"]
        + oi_analysis["score"]
        + session.get("score", 0)
    )
    score = int(score)

    # Early warning has priority over stale news direction.
    if early_warning.get("warning") == "EARLY DUMP WARNING":
        score += early_warning.get("score", 0)
    elif early_warning.get("warning") == "EARLY PUMP WARNING":
        score += early_warning.get("score", 0)

    signal_type = "НЕМАЄ УГОДИ"
    signal = "НЕЙТРАЛЬНО"

    if early_warning.get("warning") == "EARLY DUMP WARNING":
        signal = "НЕЙТРАЛЬНО"
        signal_type = "УВАГА: МОЖЛИВИЙ ДАМП / ЧЕКАТИ SHORT-ТРИГЕР"
    elif early_warning.get("warning") == "EARLY PUMP WARNING":
        signal = "НЕЙТРАЛЬНО"
        signal_type = "УВАГА: МОЖЛИВИЙ РІСТ / ЧЕКАТИ LONG-ТРИГЕР"

    if news["total"] >= 5 and news["score"] >= 35:
        score += 6
    if news["total"] >= 60:
        score -= 8
    if macro["score"] >= 25 and tech["momentum"] in ["STRONG UP", "VERY STRONG UP"]:
        score += 10
    if macro["score"] <= -25 and tech["momentum"] in ["STRONG DOWN", "VERY STRONG DOWN"]:
        score -= 10

    if market["volatility"]["regime"] == "LOW VOLATILITY / CHOP MODE":
        score -= 8
    if market["liquidation"]["bias"].startswith("SHORT SQUEEZE"):
        score += 8
    if market["liquidation"]["bias"].startswith("LONG LIQUIDATION"):
        score -= 8

    if event_risk["risk"] in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        score -= 8

    if event_risk["direction"] == "LONG":
        score += 8
    elif event_risk["direction"] == "SHORT":
        score -= 8

    # Dynamic priority: if news/event regime dominates oil, do not blindly follow opposite technical continuation.
    # It can upgrade conflict into Reversal Watch, or allow a conservative entry only when technicals start confirming.
    if signal_type == "НЕМАЄ УГОДИ" and priority.get("dominant") in ["FUNDAMENTAL", "EVENT"] and event_risk.get("direction") == "LONG" and news.get("score", 0) >= 35:
        if tech.get("momentum") in ["STRONG UP", "VERY STRONG UP"] and tech.get("trend") in ["UP", "MIXED"] and orderflow.get("score", 0) >= 0:
            signal = "LONG"
            signal_type = "NEWS-PRIORITY LONG / EVENT MOMENTUM"
        elif tech.get("trend") == "DOWN" or tech.get("score", 0) < 0:
            signal = "НЕЙТРАЛЬНО"
            signal_type = "REVERSAL LONG WATCH / NEWS PRIORITY"
    elif signal_type == "НЕМАЄ УГОДИ" and priority.get("dominant") in ["FUNDAMENTAL", "EVENT"] and event_risk.get("direction") == "SHORT" and news.get("score", 0) <= -25:
        if tech.get("momentum") in ["STRONG DOWN", "VERY STRONG DOWN"] and tech.get("trend") in ["DOWN", "MIXED"] and orderflow.get("score", 0) <= 0:
            signal = "SHORT"
            signal_type = "NEWS-PRIORITY SHORT / EVENT MOMENTUM"
        elif tech.get("trend") == "UP" or tech.get("score", 0) > 0:
            signal = "НЕЙТРАЛЬНО"
            signal_type = "REVERSAL SHORT WATCH / NEWS PRIORITY"

    # Early confirmation: avoid staying in "watch" forever when news/event is dominant
    # and the chart starts confirming the same direction.
    if signal == "НЕЙТРАЛЬНО" and priority.get("dominant") in ["FUNDAMENTAL", "EVENT"]:
        if event_risk.get("direction") == "LONG" and news.get("score", 0) >= 35:
            if tech.get("score", 0) >= -25 and tech.get("change", 0) > 0.15 and orderflow.get("score", 0) >= 0:
                signal = "LONG"
                signal_type = "EARLY NEWS LONG / CONFIRMATION STARTED"
        elif event_risk.get("direction") == "SHORT" and news.get("score", 0) <= -25:
            if tech.get("score", 0) <= 25 and tech.get("change", 0) < -0.15 and orderflow.get("score", 0) <= 0:
                signal = "SHORT"
                signal_type = "EARLY NEWS SHORT / CONFIRMATION STARTED"

    # Reversal Watch is not an automatic aggressive entry. It can upgrade a conflict into a watch-signal.
    if signal == "НЕЙТРАЛЬНО" and reversal.get("side") == "REVERSAL LONG WATCH" and reversal.get("confidence", 0) >= 45 and news.get("score", 0) >= 30:
        signal = "НЕЙТРАЛЬНО"
        signal_type = "REVERSAL LONG WATCH / ЧЕКАТИ ПІДТВЕРДЖЕННЯ"
    elif signal == "НЕЙТРАЛЬНО" and reversal.get("side") == "REVERSAL SHORT WATCH" and reversal.get("confidence", 0) >= 45 and news.get("score", 0) <= -25:
        signal = "НЕЙТРАЛЬНО"
        signal_type = "REVERSAL SHORT WATCH / ЧЕКАТИ ПІДТВЕРДЖЕННЯ"
    elif signal == "НЕЙТРАЛЬНО" and tech.get("momentum") == "VERY STRONG UP" and score >= 35:
        signal = "LONG"
        signal_type = "ІМПУЛЬСНИЙ LONG / BREAKOUT SCALP"
    elif signal == "НЕЙТРАЛЬНО" and tech.get("momentum") == "VERY STRONG DOWN" and score <= -35:
        signal = "SHORT"
        signal_type = "ІМПУЛЬСНИЙ SHORT / BREAKDOWN SCALP"
    elif signal == "НЕЙТРАЛЬНО" and score >= 75 and tech.get("trend") == "UP" and orderflow["score"] >= 20 and news["score"] >= 10 and macro["score"] >= 0:
        signal = "LONG"
        signal_type = "ПІДТВЕРДЖЕНИЙ TREND LONG"
    elif signal == "НЕЙТРАЛЬНО" and score <= -75 and tech.get("trend") == "DOWN" and orderflow["score"] <= -20 and news["score"] <= -10 and macro["score"] <= 0:
        signal = "SHORT"
        signal_type = "ПІДТВЕРДЖЕНИЙ TREND SHORT"
    elif signal == "НЕЙТРАЛЬНО" and score >= 90 and tech.get("trend") in ["UP", "MIXED"] and orderflow["score"] >= 8:
        signal = "LONG"
        signal_type = "РИЗИКОВИЙ LONG / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"
    elif signal == "НЕЙТРАЛЬНО" and score <= -90 and tech.get("trend") in ["DOWN", "MIXED"] and orderflow["score"] <= -8:
        signal = "SHORT"
        signal_type = "РИЗИКОВИЙ SHORT / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"

    # Shock Move Protection:
    # If price dumps/pumps sharply, do not let bullish/bearish headlines create
    # an opposite trade before the chart stabilizes. For oil this prevents
    # catching a falling knife during fast liquidation moves.
    shock_down = tech.get("change", 0) <= -1.2 and tech.get("score", 0) <= -80
    shock_up = tech.get("change", 0) >= 1.2 and tech.get("score", 0) >= 80

    if shock_down and news.get("score", 0) >= 25 and event_risk.get("direction") == "LONG":
        signal = "НЕЙТРАЛЬНО"
        signal_type = "SHOCK DOWN / LONG BLOCKED"
    elif shock_up and news.get("score", 0) <= -20 and event_risk.get("direction") == "SHORT":
        signal = "НЕЙТРАЛЬНО"
        signal_type = "SHOCK UP / SHORT BLOCKED"

        confidence = min(95, max(0, abs(score)))

    risk_note = "Нормальний ризик"
    if "РИЗИКОВИЙ" in signal_type:
        risk_note = "Підтвердження змішані — зменшити розмір позиції"
    if "ІМПУЛЬСНИЙ" in signal_type:
        risk_note = "Імпульсний сигнал — краще чекати відкат/ретест"
    if market["volatility"]["regime"] == "HIGH VOLATILITY / BREAKOUT MODE" and "ІМПУЛЬСНИЙ" in signal_type:
        risk_note = "Висока волатильність: тільки відкат/ретест, не доганяти свічку"
    if event_risk["risk"] in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        risk_note = "Подієвий ризик високий — краще чекати або зменшити позицію"
    if "REVERSAL" in signal_type:
        risk_note = "Можливий розворот: не входити одразу, чекати підтвердження/ретест"
    if "NEWS-PRIORITY" in signal_type:
        risk_note = "News/event priority: вхід тільки якщо техніка вже почала підтверджувати рух"
    if macro["score"] <= -25 and signal == "LONG":
        risk_note = "Macro risk-off проти LONG — тільки малий обʼєм або пропуск"
    if macro["score"] >= 25 and signal == "SHORT":
        risk_note = "Macro risk-on проти SHORT — тільки малий обʼєм або пропуск"
    if tech.get("trend") == "DOWN" and signal == "LONG":
        risk_note = "Тільки скальп: старший тренд не підтвердив LONG"
    if tech.get("trend") == "UP" and signal == "SHORT":
        risk_note = "Тільки скальп: старший тренд не підтвердив SHORT"

    confidence = min(95, max(0, abs(score)))

    return signal, signal_type, score, confidence, risk_note

def liquidity_buffer(atr, price, session=None, event_risk=None):
    """Dynamic buffer around likely liquidity zones.
    Wider during NY/event risk; smaller during quieter sessions.
    """
    buffer = atr * 0.22
    session_name = (session or {}).get("session", "")
    event_level = (event_risk or {}).get("risk", "")

    if session_name == "NEW YORK":
        buffer *= 1.25
    elif session_name == "ASIA":
        buffer *= 0.85

    if event_level in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        buffer *= 1.25

    # Minimum micro-buffer so stop is not exactly on the obvious level.
    return max(buffer, price * 0.0008)

def estimate_liquidity_levels(price, tech):
    """Approximate SMC-style liquidity levels using available free data.
    Since we do not have full candle history, this estimates likely swing/liquidity zones
    from ATR and EMA15 levels.
    """
    atr = tech.get("atr_15m") or price * 0.006
    ema20 = tech.get("ema20_15m")
    ema50 = tech.get("ema50_15m")

    # Local estimated liquidity pools.
    recent_low = price - atr * 0.85
    recent_high = price + atr * 0.85

    if ema20:
        if ema20 < price:
            recent_low = min(recent_low, ema20 - atr * 0.20)
        elif ema20 > price:
            recent_high = max(recent_high, ema20 + atr * 0.20)

    if ema50:
        if ema50 < price:
            recent_low = min(recent_low, ema50 - atr * 0.25)
        elif ema50 > price:
            recent_high = max(recent_high, ema50 + atr * 0.25)

    # Wider liquidity targets.
    lower_liquidity_1 = price - atr * 1.25
    lower_liquidity_2 = price - atr * 1.90
    lower_liquidity_3 = price - atr * 2.70

    upper_liquidity_1 = price + atr * 1.25
    upper_liquidity_2 = price + atr * 1.90
    upper_liquidity_3 = price + atr * 2.70

    return {
        "recent_low": recent_low,
        "recent_high": recent_high,
        "lower_liquidity_1": lower_liquidity_1,
        "lower_liquidity_2": lower_liquidity_2,
        "lower_liquidity_3": lower_liquidity_3,
        "upper_liquidity_1": upper_liquidity_1,
        "upper_liquidity_2": upper_liquidity_2,
        "upper_liquidity_3": upper_liquidity_3,
    }

def tp_rr_multipliers(signal_type, tech=None, session=None, event_risk=None):
    """Adaptive TP multipliers.

    Early confirmation = a bit more conservative.
    Confirmed пробій структури/trend/news alignment = wider targets.
    This avoids tiny scalping TPs while keeping TP1 realistic.
    """
    st = (signal_type or "").upper()
    tech = tech or {}
    session = session or {}
    event_risk = event_risk or {}

    rr1, rr2, rr3 = 1.35, 2.15, 3.20

    # Early reversal / watch entries are less mature, so TP1 is not too far.
    if "EARLY" in st or "REVERSAL" in st or "WATCH" in st:
        rr1, rr2, rr3 = 1.25, 2.00, 3.00

    # Strong confirmed trend gets wider expansion targets.
    if "пробій структури" in st or "BREAKOUT" in st or abs(tech.get("score", 0) or 0) >= 85:
        rr1, rr2, rr3 = 1.50, 2.40, 3.60

    # High-impact event/news can extend moves, but keep TP1 reachable.
    if event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        rr2 += 0.25
        rr3 += 0.45

    # New York usually has better follow-through for oil/macro.
    if session.get("session") == "NEW YORK":
        rr2 += 0.15
        rr3 += 0.25

    return rr1, rr2, rr3

def smc_hybrid_trade_plan(signal, signal_type, price, tech, session=None, event_risk=None):
    """SMC + ATR + RR hybrid plan.

    Stop:
      LONG  -> below estimated liquidity low / EMA zone + ATR buffer
      SHORT -> above estimated liquidity high / EMA zone + ATR buffer

    TP:
      Uses liquidity targets, but now with wider adaptive RR:
      early/reversal: about 1.25R / 2R / 3R
      confirmed trend: about 1.5R / 2.4R / 3.6R

    This is better than pure Smart Money or pure ATR alone:
      - pure SMC levels can be too subjective or too far;
      - pure ATR can be too mechanical and too small;
      - hybrid keeps stop behind structure and targets realistic expansion.
    """
    atr = tech.get("atr_15m") or price * 0.006
    levels = estimate_liquidity_levels(price, tech)
    buffer = liquidity_buffer(atr, price, session, event_risk)
    rr1, rr2, rr3 = tp_rr_multipliers(signal_type, tech, session, event_risk)

    if signal == "LONG":
        stop = levels["recent_low"] - buffer
        risk = max(price - stop, atr * 0.90)

        tp1 = max(levels["upper_liquidity_1"], price + risk * rr1)
        tp2 = max(levels["upper_liquidity_2"], price + risk * rr2)
        tp3 = max(levels["upper_liquidity_3"], price + risk * rr3)

        note = "SMC hybrid LONG: стоп нижче liquidity/EMA-зони; TP ширші по ліквідності + adaptive RR."

    elif signal == "SHORT":
        stop = levels["recent_high"] + buffer
        risk = max(stop - price, atr * 0.90)

        tp1 = min(levels["lower_liquidity_1"], price - risk * rr1)
        tp2 = min(levels["lower_liquidity_2"], price - risk * rr2)
        tp3 = min(levels["lower_liquidity_3"], price - risk * rr3)

        note = "SMC hybrid SHORT: стоп вище liquidity/EMA-зони; TP ширші по ліквідності + adaptive RR."

    else:
        return {
            "entry": None,
            "stop": None,
            "tp1": None,
            "tp2": None,
            "tp3": None,
            "note": "Не входити. Чекати підтвердження.",
            "method": "NO TRADE",
        }

    return {
        "entry": round(price, 4),
        "stop": round(stop, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "tp3": round(tp3, 4),
        "rr1": rr1,
        "rr2": rr2,
        "rr3": rr3,
        "note": note,
        "method": "SMC HYBRID / Liquidity + ATR + Adaptive RR",
    }

def make_trade_plan(signal, signal_type, price, tech, reversal=None, session=None, event_risk=None):
    """Main plan generator.
    Uses SMC-style hybrid logic:
    - Stop behind estimated liquidity/swing zone, not just random ATR.
    - TP targets use liquidity zones and minimum RR.
    """
    return smc_hybrid_trade_plan(signal, signal_type, price, tech, session, event_risk)

def side_from_score(score, long_thr=15, short_thr=-15):
    if score >= long_thr:
        return "LONG"
    if score <= short_thr:
        return "SHORT"
    return "NEUTRAL"

def tech_verdict(tech):
    score = tech.get("score", 0)
    momentum = tech.get("momentum", "NEUTRAL")
    trend = tech.get("trend", "UNKNOWN")

    if score >= 55 or (momentum in ["STRONG UP", "VERY STRONG UP"] and trend in ["UP", "MIXED"]):
        side = "LONG"
    elif score <= -55 or (momentum in ["STRONG DOWN", "VERY STRONG DOWN"] and trend in ["DOWN", "MIXED"]):
        side = "SHORT"
    else:
        side = "NEUTRAL"

    reason = f"trend {trend}, momentum {momentum}, score {score}"
    return side, reason

def news_verdict(news):
    side = side_from_score(news.get("score", 0), 15, -15)
    reason = (
        f"score {news.get('score')}, sentiment {news.get('sentiment')}, "
        f"bullish {news.get('bullish')}, bearish {news.get('bearish')}, breaking {news.get('breaking')}"
    )
    return side, reason

def event_verdict(event_risk):
    direction = event_risk.get("direction", "MIXED")
    risk = event_risk.get("risk", "НОРМАЛЬНИЙ")
    score = event_risk.get("score", 0)

    if direction in ["LONG", "SHORT"]:
        side = direction
    else:
        side = "NEUTRAL"

    reason = f"direction {direction}, risk {risk}, score {score}"
    return side, reason

def macro_verdict(macro):
    side = side_from_score(macro.get("score", 0), 15, -15)
    reason = f"regime {macro.get('regime')}, score {macro.get('score')}"
    return side, reason

def orderflow_verdict(orderflow):
    side = side_from_score(orderflow.get("score", 0), 15, -15)
    reason = f"{orderflow.get('bias')}, score {orderflow.get('score')}"
    return side, reason

def human_signal_label(signal, signal_type, early_warning=None):
    early_warning = early_warning or {"warning": "NONE", "side": "NEUTRAL"}
    st = signal_type or ""

    if early_warning.get("warning") == "EARLY DUMP WARNING":
        return "Увага: можливий дамп — чекаємо SHORT-тригер"
    if early_warning.get("warning") == "EARLY PUMP WARNING":
        return "Увага: можливий ріст — чекаємо LONG-тригер"

    if signal == "LONG":
        return "TRADE LONG"
    if signal == "SHORT":
        return "TRADE SHORT"

    if "STRUCTURE SHORT WATCH" in st:
        return "LONG слабшає — можливий відкат вниз"
    if "STRUCTURE LONG WATCH" in st:
        return "SHORT слабшає — можливий відскок вгору"
    if "LONG СКАСОВАНО" in st:
        return "LONG скасовано — чекати новий тригер"
    if "SHORT СКАСОВАНО" in st:
        return "SHORT скасовано — чекати новий тригер"
    if "REVERSAL LONG" in st or "LONG WATCH" in st:
        return "Чекаємо підтвердження LONG"
    if "REVERSAL SHORT" in st or "SHORT WATCH" in st:
        return "Чекаємо підтвердження SHORT"

    return "НЕ ВХОДИТИ — чекати"

def human_reversal_label(reversal):
    side = (reversal or {}).get("side", "NONE")
    conf = (reversal or {}).get("confidence", 0)

    if side == "REVERSAL LONG WATCH":
        return f"можливий розворот у LONG ({conf}%)"
    if side == "REVERSAL SHORT WATCH":
        return f"можливий розворот у SHORT ({conf}%)"
    return "немає"

def main_driver_override_for_early_warning(driver, early_warning):
    early_warning = early_warning or {"warning": "NONE"}
    if early_warning.get("warning") == "EARLY DUMP WARNING":
        return {
            "type": "TECH",
            "side": "SHORT",
            "title": "Раннє попередження: ціна не підтверджує bullish-новини",
            "ua_title": "Раннє попередження: можливий дамп",
            "time_context": "зараз",
            "expectation": "SHORT може прискоритись; LONG тільки після стабілізації/ретесту",
            "source": "Технічний аналіз TradingView",
            "link": "",
        }
    if early_warning.get("warning") == "EARLY PUMP WARNING":
        return {
            "type": "TECH",
            "side": "LONG",
            "title": "Раннє попередження: ціна не підтверджує bearish-новини",
            "ua_title": "Раннє попередження: можливий ріст",
            "time_context": "зараз",
            "expectation": "LONG може прискоритись; SHORT тільки після стабілізації/ретесту",
            "source": "Технічний аналіз TradingView",
            "link": "",
        }
    return driver

def final_short_summary(signal, signal_type, tech, news, orderflow, macro, event_risk, market=None, oi_analysis=None, reversal=None, session=None):
    tech_side, _ = tech_verdict(tech)
    news_side, _ = news_verdict(news)
    event_side, _ = event_verdict(event_risk)

    st = signal_type or ""
    reversal_side = (reversal or {}).get("side", "NONE")
    event_high = event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]

    if "SHOCK DOWN" in st:
        return (
            "Різкий дамп: технічний продаж зараз домінує. "
            "LONG по новинах можливий тільки після стабілізації, відскоку і ретесту. "
            "Не ловити падаючий ринок."
        )
    if "SHOCK UP" in st:
        return (
            "Різкий ріст: покупці зараз домінують. "
            "SHORT можливий тільки після стабілізації, відкату і ретесту. "
            "Не шортити сильний імпульс без підтвердження."
        )

    if signal == "SHORT":
        if reversal_side == "REVERSAL LONG WATCH" or news_side == "LONG" or event_side == "LONG":
            return (
                "SHORT зараз має перевагу через сильний технічний продаж. "
                "Але bullish-новини/події можуть дати різкий LONG-відскок — "
                "вхід тільки після ретесту або підтвердження продавців."
            )
        if event_high:
            return "SHORT є, але подієвий ризик високий. Вхід тільки після ретесту/підтвердження."
        return "SHORT підтверджений технікою. Вхід тільки зі стопом."

    if signal == "LONG":
        if reversal_side == "REVERSAL SHORT WATCH" or news_side == "SHORT" or event_side == "SHORT":
            return (
                "LONG зараз має перевагу, але bearish-новини/події можуть дати різкий відкат. "
                "Краще чекати ретест і не доганяти свічку."
            )
        if event_high:
            return "LONG є, але подієвий ризик високий. Не доганяти рух; краще чекати відкат/ретест."
        return "LONG підтверджений. Вхід тільки зі стопом."

    if reversal_side == "REVERSAL LONG WATCH":
        return "Можливий розворот у LONG: новини/події підтримують ріст, але техніка ще не дала повний тригер."
    if reversal_side == "REVERSAL SHORT WATCH":
        return "Можливий розворот у SHORT: новини/події підтримують падіння, але техніка ще не дала повний тригер."

    if tech_side == "SHORT" and news_side == "LONG":
        return "Техніка за SHORT, але новини/події за LONG. Краще не входити до підтвердження."
    if tech_side == "LONG" and news_side == "SHORT":
        return "Техніка за LONG, але новини/події за SHORT. Краще не входити до підтвердження."

    return "Сигналу на вхід немає. Ринок змішаний — краще чекати."

def combined_technical_bias(tech, orderflow, market, oi_analysis):
    """Separate technical-side decision. Does not include news/event/macro."""
    tech_side, tech_reason = tech_verdict(tech)
    order_side, order_reason = orderflow_verdict(orderflow)
    market_side, market_reason = market_structure_verdict(market)
    oi_side = oi_analysis.get("side", "NEUTRAL")

    score = (
        tech.get("score", 0)
        + orderflow.get("score", 0)
        + market.get("score", 0)
        + oi_analysis.get("score", 0)
    )

    long_votes = [tech_side, order_side, market_side, oi_side].count("LONG")
    short_votes = [tech_side, order_side, market_side, oi_side].count("SHORT")

    if score >= 80 and long_votes >= 2:
        side = "STRONG LONG"
    elif score >= 35:
        side = "LONG"
    elif score <= -80 and short_votes >= 2:
        side = "STRONG SHORT"
    elif score <= -35:
        side = "SHORT"
    else:
        side = "NEUTRAL"

    reason = (
        f"trend {tech.get('trend')}, 5m {tech.get('trend_5m')}, "
        f"15m {tech.get('trend_15m')}, 1h {tech.get('trend_1h')}, "
        f"momentum {tech.get('momentum')}, orderflow {orderflow.get('bias')}, "
        f"OI {oi_analysis.get('summary')}, score {score}"
    )

    return {"side": side, "score": score, "reason": reason}

def combined_fundamental_bias(news, event_risk, macro):
    """Separate news/fundamental-side decision. Does not include technicals."""
    news_side, news_reason = news_verdict(news)
    event_side, event_reason = event_verdict(event_risk)
    macro_side, macro_reason = macro_verdict(macro)

    # Event score is risk penalty, so use direction separately for bias.
    event_direction_score = 0
    if event_risk.get("direction") == "LONG":
        event_direction_score = 18
    elif event_risk.get("direction") == "SHORT":
        event_direction_score = -18

    score = news.get("score", 0) + macro.get("score", 0) + event_direction_score

    # High event risk means fundamentals are powerful but dangerous.
    risk = event_risk.get("risk", "НОРМАЛЬНИЙ")

    if score >= 55:
        side = "STRONG LONG"
    elif score >= 20:
        side = "LONG"
    elif score <= -55:
        side = "STRONG SHORT"
    elif score <= -20:
        side = "SHORT"
    else:
        side = "NEUTRAL"

    reason = (
        f"news {news_side} score {news.get('score')}, "
        f"event {event_side} risk {risk}, macro {macro.get('regime')}, "
        f"fundamental score {score}"
    )

    return {"side": side, "score": score, "risk": risk, "reason": reason}

def market_decision_from_bias(signal, signal_type, technical_bias, fundamental_bias, event_risk, reversal=None, session=None, priority=None):
    tech_side = technical_bias["side"]
    fund_side = fundamental_bias["side"]

    tech_short = "SHORT" in tech_side
    tech_long = "LONG" in tech_side
    fund_short = "SHORT" in fund_side
    fund_long = "LONG" in fund_side

    if priority and priority.get("dominant") in ["FUNDAMENTAL", "EVENT"] and fund_long and tech_short:
        return "NEWS/EVENT DOMINANT: техніка SHORT, але фундамент сильний LONG — SHORT не брати, чекати LONG-підтвердження"
    if priority and priority.get("dominant") in ["FUNDAMENTAL", "EVENT"] and fund_short and tech_long:
        return "NEWS/EVENT DOMINANT: техніка LONG, але фундамент сильний SHORT — LONG не брати, чекати SHORT-підтвердження"

    if reversal and reversal.get("side") == "REVERSAL LONG WATCH":
        return "REVERSAL LONG WATCH: можливий розворот вгору — чекати підтвердження"
    if reversal and reversal.get("side") == "REVERSAL SHORT WATCH":
        return "REVERSAL SHORT WATCH: можливий розворот вниз — чекати підтвердження"

    if signal == "НЕЙТРАЛЬНО":
        if tech_long and fund_short:
            return "КОНФЛІКТ: техніка LONG, фундамент SHORT — НЕ ВХОДИТИ"
        if tech_short and fund_long:
            return "КОНФЛІКТ: техніка SHORT, фундамент LONG — НЕ ВХОДИТИ"
        return "НЕ ВХОДИТИ — підтвердження недостатні"

    if tech_long and fund_long and signal == "LONG":
        return "LONG підтверджений технікою і фундаментом"
    if tech_short and fund_short and signal == "SHORT":
        return "SHORT підтверджений технікою і фундаментом"

    if event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        return f"{signal}, але подієвий ризик високий — чекати ретест"

    return f"{signal}, але підтвердження змішані"

def short_bias_label(side):
    if "STRONG LONG" in side:
        return "STRONG LONG"
    if "LONG" in side:
        return "LONG"
    if "STRONG SHORT" in side:
        return "STRONG SHORT"
    if "SHORT" in side:
        return "SHORT"
    return "NEUTRAL"

def compact_priority_label(priority, reversal):
    dominant = priority.get("dominant", "BALANCED")
    if dominant in ["FUNDAMENTAL", "EVENT"]:
        return "NEWS/EVENT"
    if dominant == "TECHNICAL":
        return "TECH"
    return "BALANCED"

def compact_reversal_label(reversal):
    side = reversal.get("side", "NONE") if reversal else "NONE"
    confidence = reversal.get("confidence", 0) if reversal else 0
    if side == "REVERSAL LONG WATCH":
        return f"можливий розворот у LONG ({confidence}%)"
    if side == "REVERSAL SHORT WATCH":
        return f"можливий розворот у SHORT ({confidence}%)"
    return "немає"

def human_decision_line(signal, signal_type, reversal, tech, news, event_risk):
    if "SHOCK DOWN" in signal_type:
        return "НЕ ВХОДИТИ — різкий дамп"
    if "SHOCK UP" in signal_type:
        return "НЕ ВХОДИТИ — різкий памп"
    if "РАННІЙ" in signal_type and signal == "LONG":
        return "РАННІЙ LONG — можна входити зараз"
    if "РАННІЙ" in signal_type and signal == "SHORT":
        return "РАННІЙ SHORT — можна входити зараз"
    if "СКАЛЬП" in signal_type and signal == "LONG":
        return "СКАЛЬП LONG — ранній відскок"
    if "СКАЛЬП" in signal_type and signal == "SHORT":
        return "СКАЛЬП SHORT — ранній відкат"

    if signal == "LONG":
        if "EARLY NEWS" in signal_type:
            return "TRADE LONG — можна входити"
        if "ІМПУЛЬСНИЙ" in signal_type:
            return "TRADE LONG — імпульсний"
        if "РИЗИКОВИЙ" in signal_type:
            return "TRADE LONG — обережно"
        return "TRADE LONG"

    if signal == "SHORT":
        if "EARLY NEWS" in signal_type:
            return "TRADE SHORT — можна входити"
        if "ІМПУЛЬСНИЙ" in signal_type:
            return "TRADE SHORT — імпульсний"
        if "РИЗИКОВИЙ" in signal_type:
            return "TRADE SHORT — обережно"
        return "TRADE SHORT"

    # No active trade: show a human trigger direction, not internal НЕЙТРАЛЬНО/WATCH terms.
    if reversal and reversal.get("side") == "REVERSAL LONG WATCH":
        return "Чекаємо підтвердження LONG"
    if reversal and reversal.get("side") == "REVERSAL SHORT WATCH":
        return "Чекаємо підтвердження SHORT"

    if event_risk.get("direction") == "LONG" and news.get("score", 0) >= 30:
        return "Чекаємо тригер по LONG"
    if event_risk.get("direction") == "SHORT" and news.get("score", 0) <= -20:
        return "Чекаємо тригер по SHORT"

    return "НЕ ВХОДИТИ — чекати"

def driver_time_context(item):
    title = (item or {}).get("title", "")
    lower = title.lower()
    published_at = (item or {}).get("published_at")

    if any(x in lower for x in ["now", "currently", "breaking", "urgent", "live"]):
        return "зараз"
    if any(x in lower for x in ["today", "later today", "this morning", "this afternoon", "tonight"]):
        return "сьогодні"
    if any(x in lower for x in ["tomorrow"]):
        return "завтра"
    if any(x in lower for x in ["this week", "upcoming", "ahead of", "expected", "awaits", "waiting for"]):
        return "очікується найближчим часом"

    if published_at:
        try:
            age_hours = (now_utc() - published_at).total_seconds() / 3600
            if age_hours <= 2:
                return "останні 2 год"
            if age_hours <= 6:
                return "останні 6 год"
        except Exception:
            pass

    return "час не уточнено"

def ua_driver_summary(title):
    lower = (title or "").lower()

    if "iran" in lower or "us-iran" in lower or "u.s.-iran" in lower or "hormuz" in lower:
        return "Переговори США–Іран / Ормузька протока"
    if "eia" in lower or "api" in lower or "inventory" in lower or "stockpiles" in lower:
        return "Запаси нафти EIA/API"
    if "opec" in lower or "opec+" in lower:
        return "OPEC/OPEC+: рішення щодо видобутку"
    if "fed" in lower or "powell" in lower or "fomc" in lower:
        return "ФРС / Powell: вплив на долар і ризик-апетит"
    if "cpi" in lower or "inflation" in lower:
        return "Інфляція США / CPI"
    if "nfp" in lower or "jobs" in lower or "payrolls" in lower:
        return "Ринок праці США / NFP"
    if "sanction" in lower or "tariff" in lower or "trump" in lower:
        return "Санкції / тарифи / політичні заяви"
    if "russia" in lower or "ukraine" in lower or "war" in lower:
        return "Геополітика: війна / ризик постачання"
    if "oil" in lower or "brent" in lower or "crude" in lower:
        return "Нафта: свіжий новинний імпульс"

    return BeautifulSoup(title or "Новинний фактор", "html.parser").get_text(" ", strip=True)[:95]

def driver_expectation(direction, title, driver_type="NEWS"):
    lower = (title or "").lower()
    side = direction if direction in ["LONG", "SHORT"] else "MIXED"

    if side == "LONG":
        if any(x in lower for x in ["iran", "hormuz", "sanction", "war", "attack", "strike", "supply risk", "disruption"]):
            return "LONG — ризик дефіциту/перебоїв постачання нафти"
        if any(x in lower for x in ["draw", "stockpiles fell", "inventory draw"]):
            return "LONG — запаси зменшуються, це підтримує нафту"
        if "opec" in lower and any(x in lower for x in ["cut", "cuts", "reduce"]):
            return "LONG — обмеження видобутку підтримує ціну"
        return "LONG — новини/події підтримують попит або ризик дефіциту"

    if side == "SHORT":
        if any(x in lower for x in ["ceasefire", "peace", "deal", "sanctions relief", "talks progress"]):
            return "SHORT — геополітична премія в ціні може зменшитись"
        if any(x in lower for x in ["build", "stockpiles rose", "inventory build"]):
            return "SHORT — запаси ростуть, це тисне на нафту"
        if "opec" in lower and any(x in lower for x in ["increase", "output hike", "production hike"]):
            return "SHORT — більша пропозиція може тиснути на ціну"
        if any(x in lower for x in ["hawkish", "rate hike", "dollar rises", "yields rise"]):
            return "SHORT — сильний долар/ставки тиснуть на нафту"
        return "SHORT — новини/події тиснуть на ціну"

    if driver_type == "TECH":
        return "Очікування залежить від підтвердження 5m/15m"
    return "MIXED — напрямок новини неоднозначний"

def technical_driver_summary(tech, orderflow, market):
    score = tech.get("score", 0)
    trend = tech.get("trend", "MIXED")
    momentum = tech.get("momentum", "NEUTRAL")
    vol = market.get("volatility", {}).get("regime", "NORMAL")

    if score >= 55:
        return "TECH / LONG", "Техніка підтримує LONG", "LONG — тренд/імпульс на боці покупців"
    if score <= -55:
        if (tech.get("rsi_5m") is not None and tech.get("rsi_5m") < 26) or (tech.get("rsi_15m") is not None and tech.get("rsi_15m") < 30):
            return "TECH / SHORT", "Техніка вниз, але є перепроданість", "SHORT обережно — можливий відскок"
        return "TECH / SHORT", "Техніка підтримує SHORT", "SHORT — тренд/імпульс на боці продавців"
    if momentum in ["STRONG UP", "VERY STRONG UP"]:
        return "TECH / LONG", "Сильний імпульс вгору", "LONG — краще після відкату/ретесту"
    if momentum in ["STRONG DOWN", "VERY STRONG DOWN"]:
        return "TECH / SHORT", "Сильний імпульс вниз", "SHORT — краще після відкату/ретесту"
    if vol == "TREND MODE":
        return f"TECH / {trend}", f"Трендовий режим: {trend}", "Очікування — рух за трендом після підтвердження"
    return "TECH / NEUTRAL", "Техніка без чіткого драйвера", "Чекати сильнішого 5m/15m сигналу"

def news_source_quality(source, title=""):
    s = (source or "").lower()
    t = (title or "").lower()
    if "reuters" in s or "reuters" in t:
        return 1.35
    if any(x in s for x in ["cnbc", "oilprice", "eia", "opec", "financial times", "wsj", "investing"]):
        return 1.15
    if any(x in s for x in ["coindesk", "cointelegraph", "cryptopanic"]):
        return 0.35
    return 1.0

def is_low_priority_oil_driver(item):
    source = (item or {}).get("source", "")
    title = (item or {}).get("title", "")
    return news_source_quality(source, title) < 0.6

def select_main_driver(tech, news, event_risk, macro, orderflow, market, session, priority):
    # Event/news dominates if priority says so and there is a clear event headline.
    important_events_raw = event_risk.get("important", []) or []
    important_news_raw = news.get("important", []) or []

    important_events = [x for x in important_events_raw if not is_low_priority_oil_driver(x)] or important_events_raw
    important_news = [x for x in important_news_raw if not is_low_priority_oil_driver(x)] or important_news_raw

    # Shock technical move should become the main driver even if headlines are bullish/bearish.
    # This avoids showing an EVENT/LONG driver during a fast technical dump.
    tech_score = tech.get("score", 0)
    if tech_score <= -120:
        return {
            "type": "TECH / SHORT",
            "summary": "Різкий технічний продаж",
            "time": "зараз",
            "expectation": "SHORT домінує зараз; LONG тільки після стабілізації/ретесту",
            "source": "Технічний аналіз TradingView",
            "link": "",
        }
    if tech_score >= 120:
        return {
            "type": "TECH / LONG",
            "summary": "Різкий технічний памп",
            "time": "зараз",
            "expectation": "LONG домінує зараз; SHORT тільки після слабкості/ретесту",
            "source": "Технічний аналіз TradingView",
            "link": "",
        }

    if priority.get("dominant") in ["FUNDAMENTAL", "EVENT"] and event_risk.get("direction") in ["LONG", "SHORT"] and important_events:
        item = important_events[0]
        direction = event_risk.get("direction")
        return {
            "type": f"EVENT / {direction}",
            "summary": ua_driver_summary(item.get("title", "")),
            "time": driver_time_context(item),
            "expectation": driver_expectation(direction, item.get("title", ""), "EVENT"),
            "source": item.get("source", ""),
            "link": item.get("link", ""),
        }

    if abs(news.get("score", 0)) >= 30 and important_news:
        item = important_news[0]
        direction = "LONG" if news.get("score", 0) > 0 else "SHORT"
        return {
            "type": f"NEWS / {direction}",
            "summary": ua_driver_summary(item.get("title", "")),
            "time": driver_time_context(item),
            "expectation": driver_expectation(direction, item.get("title", ""), "NEWS"),
            "source": item.get("source", ""),
            "link": item.get("link", ""),
        }

    tech_type, tech_summary, tech_expectation = technical_driver_summary(tech, orderflow, market)
    return {
        "type": tech_type,
        "summary": tech_summary,
        "time": "зараз",
        "expectation": tech_expectation,
        "source": "Технічний аналіз TradingView",
        "link": "",
    }

def decision_confidence(signal, signal_type, score, technical_bias, fundamental_bias, event_risk, priority, reversal):
    """Confidence means confidence in the decision, not win-rate."""
    tech_side = technical_bias.get("side", "NEUTRAL")
    fund_side = fundamental_bias.get("side", "NEUTRAL")
    tech_score = abs(technical_bias.get("score", 0))
    fund_score = abs(fundamental_bias.get("score", 0))
    event_high = event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]
    reversal_active = reversal and reversal.get("side") in ["REVERSAL LONG WATCH", "REVERSAL SHORT WATCH"]

    if "SHOCK" in signal_type:
        return 90

    if signal in ["LONG", "SHORT"]:
        base = 55 + min(30, abs(int(score)) // 3)
        if "EARLY NEWS" in signal_type:
            base = min(base, 72)
        if event_high and "EARLY NEWS" not in signal_type:
            base -= 5
        return max(55, min(95, int(base)))

    conflict = (("LONG" in tech_side and "SHORT" in fund_side) or ("SHORT" in tech_side and "LONG" in fund_side))
    if reversal_active:
        return min(92, max(75, int(reversal.get("confidence", 0)) + 35))
    if conflict and event_high:
        return 88
    if conflict:
        return 80
    return min(78, max(50, int((tech_score + fund_score) / 3)))

def format_driver_source(driver):
    link = (driver or {}).get("link") or ""
    source = (driver or {}).get("source") or ""
    if link and link.startswith("http"):
        label = source if source else "відкрити новину"
        return f'<a href="{link}">{label}</a>'
    if source:
        return source
    return "не вказано"

def driver_side(driver):
    text = f"{(driver or {}).get('type', '')} {(driver or {}).get('expectation', '')}".upper()
    has_long = "LONG" in text
    has_short = "SHORT" in text
    if has_long and not has_short:
        return "LONG"
    if has_short and not has_long:
        return "SHORT"
    return "NEUTRAL"

def simple_direction_word(signal):
    if signal == "LONG":
        return "лонг"
    if signal == "SHORT":
        return "шорт"
    return "рух"

def simple_opposite_action(signal):
    if signal == "LONG":
        return "не йде в шорт"
    if signal == "SHORT":
        return "не йде в лонг"
    return "не підтверджує"

def simple_bias_label(side):
    side = side or "NEUTRAL"
    if "STRONG LONG" in side:
        return "сильний лонг"
    if "LONG" in side:
        return "лонг"
    if "STRONG SHORT" in side:
        return "сильний шорт"
    if "SHORT" in side:
        return "шорт"
    return "нейтрально"

def technical_reason_text(signal, technical_bias, smc=None, tech=None):
    tech_side = short_bias_label((technical_bias or {}).get("side", "NEUTRAL"))
    tech_score = (technical_bias or {}).get("score", 0)
    smc_bias = (smc or {}).get("bias", "NEUTRAL")
    micro = (tech or {}).get("micro_3m") if isinstance((tech or {}).get("micro_3m"), dict) else {}
    micro_bias = micro.get("bias", "NEUTRAL")
    direction = simple_direction_word(signal)

    parts = []
    if signal in ["LONG", "SHORT"]:
        if signal in tech_side:
            parts.append("графік")
        if smc_bias == signal:
            parts.append("структура")
        if micro_bias == signal:
            parts.append("3m")

    if parts:
        verb = "показують" if len(parts) > 1 else "показує"
        return f"{', '.join(parts)} {verb} {direction} ({tech_score})"
    if "LONG" in tech_side or "SHORT" in tech_side:
        return f"графік задає напрямок ({tech_score})"
    if signal in ["LONG", "SHORT"]:
        return f"можливий рух на {direction}, але треба підтвердження"
    return "перевага нечітка — краще чекати"

def signal_conflict_text(signal, driver, technical_bias, fundamental_bias, event_risk):
    if signal not in ["LONG", "SHORT"]:
        return ""

    opposite = "SHORT" if signal == "LONG" else "LONG"
    driver_direction = driver_side(driver)
    fund_side = (fundamental_bias or {}).get("side", "NEUTRAL")
    event_side = (event_risk or {}).get("direction", "MIXED")

    if driver_direction == opposite or opposite in fund_side or event_side == opposite:
        news_direction = simple_direction_word(opposite)
        return f"новини за {news_direction}, але ціна {simple_opposite_action(signal)} — зараз важливіший графік"
    return ""

def align_driver_with_final_signal(driver, signal, technical_bias, fundamental_bias, event_risk, smc=None, tech=None):
    conflict = signal_conflict_text(signal, driver, technical_bias, fundamental_bias, event_risk)
    if not conflict:
        return driver

    if signal == "SHORT":
        summary = "Графік показує шорт, хоча новини за лонг"
        expectation = "Не входити по ринку. Чекати відкат або пробій нижче рівня"
    elif signal == "LONG":
        summary = "Графік показує лонг, хоча новини за шорт"
        expectation = "Не входити по ринку. Чекати відкат або пробій вище рівня"
    else:
        return driver

    return {
        "type": f"Графік / {simple_direction_word(signal)}",
        "summary": summary,
        "time": "зараз",
        "expectation": expectation,
        "source": "Графік / структура / 3m",
        "link": "",
    }

def reversal_display_label(signal, reversal):
    side = (reversal or {}).get("side", "NONE")
    conf = (reversal or {}).get("confidence", 0)

    if signal == "SHORT" and side == "REVERSAL LONG WATCH":
        return f"ризик LONG-відскоку ({conf}%)"
    if signal == "LONG" and side == "REVERSAL SHORT WATCH":
        return f"ризик SHORT-відкату ({conf}%)"
    if side == "REVERSAL LONG WATCH":
        return f"можливий розворот у LONG ({conf}%)"
    if side == "REVERSAL SHORT WATCH":
        return f"можливий розворот у SHORT ({conf}%)"
    return "немає"

def reversal_risk_note(signal, reversal):
    side = (reversal or {}).get("side", "NONE")
    conf = (reversal or {}).get("confidence", 0)

    if signal == "SHORT" and side == "REVERSAL LONG WATCH":
        return f"Ризик: можливий LONG-відскок пізніше ({conf}%)"
    if signal == "LONG" and side == "REVERSAL SHORT WATCH":
        return f"Ризик: можливий SHORT-відкат пізніше ({conf}%)"
    return ""

def analyze_exhaustion_cooling(signal, tech, tv=None):
    """Detects post-pump/dump cooling phase using available TradingView data.
    No candle history required: uses change, RSI, EMA stretch, and momentum.
    """
    price = (tv or {}).get("price") if isinstance(tv, dict) else None
    price = price or 0
    change = tech.get("change", 0) or 0
    rsi5 = tech.get("rsi_5m") or 50
    rsi15 = tech.get("rsi_15m") or 50
    ema20 = tech.get("ema20_15m")
    momentum = tech.get("momentum", "NEUTRAL")

    active = False
    side = "NEUTRAL"
    note = ""
    stretch_pct = 0

    if price and ema20:
        try:
            stretch_pct = abs(price - ema20) / price * 100
        except Exception:
            stretch_pct = 0

    if signal == "LONG" and change >= 2.5:
        active = True
        side = "LONG_OVEREXTENDED"
        note = "Ринок після сильного імпульсу охолоджується. Новий LONG краще шукати після ретесту або консолідації."
    elif signal == "SHORT" and change <= -2.5:
        active = True
        side = "SHORT_OVEREXTENDED"
        note = "Ринок після сильного падіння охолоджується. Новий SHORT краще шукати після ретесту або консолідації."

    if signal == "LONG" and (rsi5 >= 76 or rsi15 >= 72 or stretch_pct >= 1.2):
        active = True
        side = "LONG_OVEREXTENDED"
        note = "LONG перегрітий: ціна сильно відірвалась від середньої. Краще чекати відкат/ретест."
    elif signal == "SHORT" and (rsi5 <= 24 or rsi15 <= 28 or stretch_pct >= 1.2):
        active = True
        side = "SHORT_OVEREXTENDED"
        note = "SHORT перегрітий: ціна сильно відірвалась від середньої. Краще чекати відкат/ретест."

    # If momentum is already neutral after a large move, cooling is more likely.
    if active and momentum == "NEUTRAL":
        note = note.replace("Краще чекати", "Momentum слабшає. Краще чекати")

    return {
        "active": active,
        "side": side,
        "note": note,
        "stretch_pct": round(stretch_pct, 2),
    }

def analyze_late_entry_risk(signal, tech, market):
    """Detects when the move is already too extended for a clean entry."""
    change = abs(tech.get("change", 0) or 0)
    rsi5 = tech.get("rsi_5m") or 50
    rsi15 = tech.get("rsi_15m") or 50
    vol = market.get("volatility", {}).get("regime", "NORMAL") if market else "NORMAL"

    late = False
    label = ""
    note = ""
    penalty = 0

    if signal == "LONG" and (change >= 2.0 or rsi5 >= 76 or rsi15 >= 72):
        late = True
        label = "LONG активний — пізній вхід"
        note = "Рух уже частково реалізований. Не доганяти свічку; краще чекати відкат/ретест."
        penalty = -12
    elif signal == "SHORT" and (change >= 2.0 or rsi5 <= 24 or rsi15 <= 28):
        late = True
        label = "SHORT активний — пізній вхід"
        note = "Рух уже частково реалізований. Не доганяти падіння; краще чекати відкат/ретест."
        penalty = -12

    if late and vol == "HIGH VOLATILITY / BREAKOUT MODE":
        penalty -= 5
        note += ""

    return {"late": late, "label": label, "note": note, "penalty": penalty}

def apply_expansion_targets(plan, signal, tech, market):
    """Widen targets/stops when volatility expands or move is already large."""
    if not plan or not isinstance(plan, dict) or plan.get("entry") is None:
        return plan

    change = abs(tech.get("change", 0) or 0)
    vol = market.get("volatility", {}).get("regime", "NORMAL") if market else "NORMAL"

    if change < 2.0 and vol != "HIGH VOLATILITY / BREAKOUT MODE":
        return plan

    entry = float(plan["entry"])
    stop = float(plan["stop"])
    risk = abs(entry - stop)
    if risk <= 0:
        return plan

    # Wider targets in expansion/news breakout mode: 1.5R / 2.5R / 4R
    if signal == "LONG":
        plan["tp1"] = round(entry + risk * 1.5, 4)
        plan["tp2"] = round(entry + risk * 2.5, 4)
        plan["tp3"] = round(entry + risk * 4.0, 4)
    elif signal == "SHORT":
        plan["tp1"] = round(entry - risk * 1.5, 4)
        plan["tp2"] = round(entry - risk * 2.5, 4)
        plan["tp3"] = round(entry - risk * 4.0, 4)

    plan["expansion"] = True
    return plan

def probability_note(probability, late_entry):
    if probability is None:
        return "немає входу"
    if late_entry and late_entry.get("late"):
        if probability < 50:
            return f"{probability}% — краще чекати відкат"
        return f"{probability}% — ризик пізнього входу"
    return f"{probability}%"

def entry_quality_scale(probability, late_entry=None, signal_type=""):
    """User-friendly entry quality scale for Telegram.
    This separates market direction from actual entry quality.
    """
    signal_type = str(signal_type or "")
    if probability is None:
        if "SHOCK DOWN" in signal_type:
            return "0/5 — шорт уже пізно, не доганяти падіння"
        if "SHOCK UP" in signal_type:
            return "0/5 — лонг уже пізно, не доганяти імпульс"
        return "0/5 — немає входу"
    try:
        probability = int(probability)
    except Exception:
        return "0/5 — немає входу"

    suffix = ""
    if late_entry and late_entry.get("late"):
        suffix = " — пізній вхід, краще чекати відкат"

    if probability < 50:
        return f"0/5 — не входити ({probability}%){suffix}"
    if probability < 55:
        return f"2/5 — тільки спостерігати ({probability}%){suffix}"
    if probability < 65:
        return f"3/5 — чекати підтвердження ({probability}%){suffix}"
    if probability < 75:
        return f"4/5 — можна входити, тільки зі стопом ({probability}%){suffix}"
    return f"5/5 — найкращий вхід ({probability}%){suffix}"

def simple_decision_text(signal, trade_probability, late_entry=None, cooling=None):
    if signal not in ["LONG", "SHORT"]:
        return None

    direction = simple_direction_word(signal)
    if trade_probability is None:
        return f"Можливий {direction}, але сигналу на вхід немає"

    if trade_probability < 50:
        if late_entry and late_entry.get("late"):
            return f"{direction.capitalize()} вже йде — зараз не входити"
        return f"Можливий {direction}, але зараз не входити"

    if trade_probability < 65:
        if cooling and cooling.get("active"):
            return f"{direction.capitalize()} можливий, але чекати відкат"
        return f"Чекати {direction} — потрібне підтвердження"

    if trade_probability < 75:
        return f"Можна пробувати {direction}, тільки зі стопом"

    return f"Сильний сигнал на {direction}, тільки зі стопом"

def smc_conflict_note(smc):
    """Explain mixed SMC signals instead of showing contradictory пробій структури/volume silently."""
    if not smc or not isinstance(smc, dict) or not smc.get("available"):
        return ""

    bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    choch = smc.get("choch", "NONE")
    summary = str(smc.get("summary", "")).lower()
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    поглинання = volume.get("поглинання", "NONE")

    long_structure = bias == "LONG" or bos == "пробій структури LONG" or choch == "ознака розвороту LONG"
    short_structure = bias == "SHORT" or bos == "пробій структури SHORT" or choch == "ознака розвороту SHORT"

    bearish_candle = "bearish candle" in summary or "продавців" in summary or поглинання == "BEARISH ABSORPTION"
    bullish_candle = "bullish candle" in summary or "покупців" in summary or поглинання == "BULLISH ABSORPTION"

    if long_structure and (vol_bias == "SHORT" or bearish_candle):
        return "Структура змішана: лонг є, але свічка/обсяг проти. Лонг ще не підтверджений."
    if short_structure and (vol_bias == "LONG" or bullish_candle):
        return "Структура змішана: шорт є, але свічка/обсяг проти. Шорт ще не підтверджений."
    if bias == "LONG":
        return "Структура підтверджує лонг."
    if bias == "SHORT":
        return "Структура підтверджує шорт."
    return "Структура: ще без чіткого підтвердження."

def no_entry_reason(signal, market_bias, trade_probability, technical_bias, news, event_risk, smc, late_entry=None, cooling=None, tech=None):
    """Short explanation why Telegram says there is no entry now."""
    reasons = []

    if signal not in ["LONG", "SHORT"] or trade_probability is None:
        reasons.append("немає повного торгового сигналу")
    elif trade_probability < 55:
        reasons.append("вхід тільки по тригеру, не по ринку")
    elif trade_probability < 65:
        reasons.append("є watch-сигнал, але ще немає повного підтвердження")

    tech_side = technical_bias.get("side", "NEUTRAL") if isinstance(technical_bias, dict) else "NEUTRAL"
    tech_score = technical_bias.get("score", 0) if isinstance(technical_bias, dict) else 0
    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    if market_bias in ["LONG", "SHORT"]:
        if tech_side not in [market_bias, "NEUTRAL"]:
            reasons.append("техніка проти напрямку")
        elif tech_side == "NEUTRAL" or abs(tech_score) < 20:
            reasons.append("техніка ще не підтвердила")

        if abs(news_score) >= 35 and event_side == market_bias and (tech_side == "NEUTRAL" or abs(tech_score) < 20):
            reasons.append("новини сильніші за price action")

    smc_note = smc_conflict_note(smc)
    if "змішано" in smc_note:
        reasons.append("SMC змішаний")

    exhaustion_reason = extension_exhaustion_reason(signal, tech or {}, smc, news, event_risk)
    if exhaustion_reason:
        reasons.append(exhaustion_reason)

    early = early_reversal_engine({}, tech or {}, smc, news, event_risk)
    if early.get("active") and early.get("side") == signal and (trade_probability or 0) < 65:
        reasons.append("ранній розворот ще без повного підтвердження")

    if late_entry and late_entry.get("late"):
        reasons.append("пізній вхід після імпульсу")
    if cooling and cooling.get("active"):
        reasons.append("потрібне охолодження/ретест")

    if not reasons:
        reasons.append("чекати ретест або підтвердження ціною")

    # Deduplicate, keep short.
    unique = []
    for r in reasons:
        if r not in unique:
            unique.append(r)
    return "; ".join(unique[:3])

def compact_final_summary_text(final_summary, market_bias, trade_probability):
    """Keep Telegram conclusion short and actionable."""
    if trade_probability is None:
        if market_bias in ["LONG", "SHORT"]:
            return f"{market_bias} bias є, але входу ще немає — чекати підтвердження."
        return "Сигнал не підтверджений — краще чекати."

    if trade_probability < 55:
        return "Сигнал слабкий — краще чекати."
    if market_bias in ["LONG", "SHORT"]:
        return f"{market_bias} сценарій активний, але працювати тільки зі стопом."
    return "Перевага нечітка — не поспішати з входом."

def estimate_trade_probability(signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk, orderflow, market, reversal, chase=None, weekend=None, late_entry=None, smc=None, tech=None):
    """Human-friendly probability estimate for Telegram.
    This is NOT a guarantee. It is a normalized bot estimate based on alignment/risk.
    """
    if signal not in ["LONG", "SHORT"]:
        return None

    prob = 50

    # Confidence contribution, but capped so it does not become unrealistic.
    try:
        prob += min(18, max(0, (int(confidence) - 50) * 0.45))
    except Exception:
        pass

    target = signal
    tech_side = technical_bias.get("side", "NEUTRAL")
    fund_side = fundamental_bias.get("side", "NEUTRAL")
    event_side = event_risk.get("direction", "MIXED")
    order_score = orderflow.get("score", 0)

    if tech_side == target:
        prob += 8
    elif tech_side in ["LONG", "SHORT"] and tech_side != target:
        prob -= 10

    if fund_side == target:
        prob += 7
    elif fund_side in ["LONG", "SHORT"] and fund_side != target:
        prob -= 9

    if event_side == target:
        prob += 5
    elif event_side in ["LONG", "SHORT"] and event_side != target:
        prob -= 8

    if target == "LONG" and order_score >= 15:
        prob += 5
    elif target == "SHORT" and order_score <= -15:
        prob += 5
    elif abs(order_score) < 10:
        prob -= 3

    early_entry = early_entry_signal({}, signal, "", tech or {}, smc or {}, orderflow, news, event_risk, market)
    if early_entry.get("active") and early_entry.get("side") == signal:
        prob += 8

    if event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
        prob -= 7

    if (reversal or {}).get("side") == "REVERSAL LONG WATCH" and target == "SHORT":
        prob -= 6
    if (reversal or {}).get("side") == "REVERSAL SHORT WATCH" and target == "LONG":
        prob -= 6

    if chase and chase.get("extended"):
        prob -= 6

    if weekend and weekend.get("active"):
        prob -= 5
    if late_entry and late_entry.get("late"):
        prob += late_entry.get("penalty", -10)

    prob += smc_probability_adjustment(signal, smc)
    truth_filter = price_action_truth_filter(signal, tech or {}, smc, news, event_risk, orderflow)
    prob += truth_filter.get('penalty', 0)
    prob += truth_filter.get('bonus', 0)

    # Quality adjustment.
    q = str(quality or "")
    if "A+" in q:
        prob += 5
    elif q == "A":
        prob += 3
    elif q.startswith("C"):
        prob -= 5

    # Keep realistic range.
    prob = cap_countertrend_probability(prob, signal, tech or {}, smc)

    # Anti-late-continuation filter:
    # prevents strong 5/5 entries after an extended move without SMC/пробій структури/volume confirmation.
    exhaustion = extension_exhaustion_filter(signal, tech or {}, smc, news, event_risk)
    if exhaustion.get("active") and exhaustion.get("cap") is not None:
        prob = min(prob, int(exhaustion["cap"]))

    # Early reversal is only a WATCH until SMC/пробій структури/volume fully confirms it.
    early = early_reversal_engine({}, tech or {}, smc, news, event_risk)
    if early.get("active") and early.get("side") == signal and early.get("quality_cap") is not None:
        prob = min(prob, int(early["quality_cap"]))

    return int(max(30, min(82, round(prob))))


def hard_no_long_when_chart_short(signal, signal_type, confidence, score, tech, trade_probability=None):
    """Final safety guard before Telegram.

    If chart is SHORT and price is falling, bot must not say "wait LONG".
    Especially when entry quality is 0/5.
    """
    tech = tech or {}
    tech_score = tech.get("score", 0) or 0
    change = tech.get("change", 0) or 0

    if signal == "LONG" and tech_score <= -35 and change < -0.25:
        if trade_probability is None or trade_probability < 55:
            return {
                "signal": "НЕЙТРАЛЬНО",
                "signal_type": "LONG BLOCKED / TECH SHORT",
                "confidence": min(confidence, 50),
                "score": 0,
                "reason": "LONG не давати: графік SHORT і якість входу низька",
            }

    if signal == "SHORT" and tech_score >= 35 and change > 0.25:
        if trade_probability is None or trade_probability < 55:
            return {
                "signal": "НЕЙТРАЛЬНО",
                "signal_type": "SHORT BLOCKED / TECH LONG",
                "confidence": min(confidence, 50),
                "score": 0,
                "reason": "SHORT не давати: графік LONG і якість входу низька",
            }

    return {
        "signal": signal,
        "signal_type": signal_type,
        "confidence": confidence,
        "score": score,
        "reason": "",
    }


def final_signal_sanity_guard(signal, signal_type, confidence, tech, smc=None, micro=None, trade_probability=None):
    """Final guard before Telegram.

    Prevents:
    - waiting for LONG while chart is already SHORT
    - waiting for SHORT while chart is already LONG
    """
    tech = tech or {}
    smc = smc or {}
    micro = micro or {}

    tech_score = tech.get("score", 0) or 0
    change = tech.get("change", 0) or 0
    smc_bias = smc.get("bias", "NEUTRAL")
    micro_bias = micro.get("bias", "NEUTRAL")
    micro_state = micro.get("state", "RANGE")

    low_quality = trade_probability is None or trade_probability < 55

    chart_short = (
        tech_score <= -35
        or smc_bias == "SHORT"
        or micro_bias == "SHORT"
        or micro_state == "SHORT_STRENGTHENING"
        or change <= -0.45
    )

    chart_long = (
        tech_score >= 35
        or smc_bias == "LONG"
        or micro_bias == "LONG"
        or micro_state == "LONG_STRENGTHENING"
        or change >= 0.45
    )

    if "СКАЛЬП" in str(signal_type) and signal in ["LONG", "SHORT"]:
        return {
            "signal": signal,
            "signal_type": signal_type,
            "confidence": confidence,
            "reason": "",
        }

    if signal == "LONG" and chart_short and low_quality:
        return {
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "LONG BLOCKED / CHART SHORT",
            "confidence": min(confidence, 50),
            "reason": "графік SHORT, LONG не підтверджений",
        }

    if signal == "SHORT" and chart_long and low_quality:
        return {
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "SHORT BLOCKED / CHART LONG",
            "confidence": min(confidence, 50),
            "reason": "графік LONG, SHORT не підтверджений",
        }

    # If chart is strongly opposite, allow direction switch only as prepare/watch, not blind trade.
    if signal == "LONG" and (tech_score <= -60 or smc_bias == "SHORT" or micro_bias == "SHORT"):
        return {
            "signal": "SHORT",
            "signal_type": "PRICE ACTION SHORT / NEWS CONFLICT",
            "confidence": max(55, min(72, abs(tech_score))),
            "reason": "графік сильніший за LONG-новини",
        }

    if signal == "SHORT" and (tech_score >= 60 or smc_bias == "LONG" or micro_bias == "LONG"):
        return {
            "signal": "LONG",
            "signal_type": "PRICE ACTION LONG / NEWS CONFLICT",
            "confidence": max(55, min(72, abs(tech_score))),
            "reason": "графік сильніший за SHORT-новини",
        }

    return {
        "signal": signal,
        "signal_type": signal_type,
        "confidence": confidence,
        "reason": "",
    }

def market_mode_engine(signal, signal_type, trade_probability, tech, smc, orderflow, news, event_risk, market, session, late_entry=None, cooling=None):
    """Human market mode + strategy layer for Telegram.

    This does not replace the signal. It explains how aggressively the signal
    may be traded in the current regime.
    """
    signal_type = str(signal_type or "")
    tech = tech or {}
    smc = smc or {}
    orderflow = orderflow or {}
    event_risk = event_risk or {}
    market = market or {}
    micro = tech.get("micro_3m") if isinstance(tech.get("micro_3m"), dict) else {}

    prob = trade_probability or 0
    tech_score = tech.get("score", 0) or 0
    change = tech.get("change", 0) or 0
    trend = tech.get("trend", "MIXED")
    micro_state = micro.get("state", "RANGE")
    micro_bias = micro.get("bias", "NEUTRAL")
    smc_bias = smc.get("bias", "NEUTRAL")
    event_high = event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]
    calendar_active = bool((event_risk.get("calendar") or {}).get("active"))
    volatility = market.get("volatility", {}).get("regime", "NORMAL")

    has_entry = signal in ["LONG", "SHORT"] and prob >= 65
    has_watch = signal in ["LONG", "SHORT"] and 50 <= prob < 65

    scalp_prep = scalp_preparation_signal({}, tech, smc, orderflow, news, event_risk)
    if scalp_prep.get("active") and "СКАЛЬП" not in signal_type and not has_entry:
        return {
            "status": "СКАЛЬП ГОТУЄТЬСЯ",
            "mode": scalp_prep.get("text", "можливий відскок"),
            "strategy": "це ще не вхід; чекати підтвердження 3m і потоку",
            "priority": "3m + стакан/угоди + структура",
            "aggression": "поки не входити",
        }

    if "СКАЛЬП" in signal_type:
        side_text = "лонг-відскок" if signal == "LONG" else "шорт-відкат"
        status = "СКАЛЬП" if prob >= 55 else "ЧЕКАТИ"
        return {
            "status": status,
            "mode": f"скальп {side_text}",
            "strategy": "короткий швидкий вхід, малий стоп, фіксація швидше; не тримати як трендову угоду",
            "priority": "3m + SMC + стакан/угоди важливіші за новини",
            "aggression": "агресивно, але тільки малим ризиком",
        }

    if calendar_active or event_high:
        if "СКАЛЬП" in signal_type:
            status = "СКАЛЬП" if prob >= 55 else "ЧЕКАТИ"
        elif has_entry:
            status = "ВХІД Є"
        elif has_watch:
            status = "ЧЕКАТИ"
        else:
            status = "ВХОДУ НЕМАЄ"
        return {
            "status": status,
            "mode": "новинна торгівля",
            "strategy": "можна входити по новинах, якщо ціна вже підтвердила напрям",
            "priority": "новини + реакція ціни; для скальпу 3m/стакан важливіші",
            "aggression": "дозволено, але тільки зі стопом",
        }

    if volatility == "LOW VOLATILITY / CHOP MODE" or (micro_state == "RANGE" and abs(tech_score) < 45):
        return {
            "status": "ВХОДУ НЕМАЄ" if prob < 55 else "ЧЕКАТИ",
            "mode": "боковик",
            "strategy": "не брати середину діапазону; чекати пробій або відскок від краю",
            "priority": "рівні + 3m; новини лише як фільтр",
            "aggression": "не агресивно",
        }

    if signal in ["LONG", "SHORT"]:
        direction = "лонг" if signal == "LONG" else "шорт"
        aligned_trend = (
            (signal == "LONG" and (trend == "UP" or tech_score >= 55 or micro_bias == "LONG" or smc_bias == "LONG")) or
            (signal == "SHORT" and (trend == "DOWN" or tech_score <= -55 or micro_bias == "SHORT" or smc_bias == "SHORT"))
        )
        if aligned_trend:
            status = "ВХІД Є" if has_entry else ("ЧЕКАТИ" if has_watch else "ВХОДУ НЕМАЄ")
            aggression = "агресивний " + direction if has_entry and not (late_entry and late_entry.get("late")) else "обережний " + direction
            if late_entry and late_entry.get("late"):
                status = "ВХОДУ НЕМАЄ"
                aggression = "не доганяти рух"
            return {
                "status": status,
                "mode": f"трендовий {direction}",
                "strategy": "працювати за трендом; найкраще після ретесту або раннього підтвердження 3m",
                "priority": "графік + SMC + 3m > новини, якщо новини не проти руху",
                "aggression": aggression,
            }

    if "SHOCK DOWN" in signal_type:
        return {
            "status": "ВХОДУ НЕМАЄ",
            "mode": "різкий дамп",
            "strategy": "шорт не доганяти; чекати ретест для шорту або скальп-відскок тільки після підтвердження",
            "priority": "ціна і 3m > новини; для скальпу потрібен відкуп",
            "aggression": "не агресивно",
        }
    if "SHOCK UP" in signal_type:
        return {
            "status": "ВХОДУ НЕМАЄ",
            "mode": "різкий памп",
            "strategy": "лонг не доганяти; чекати ретест для лонгу або скальп-відкат тільки після підтвердження",
            "priority": "ціна і 3m > новини; для скальпу потрібен продаж",
            "aggression": "не агресивно",
        }

    return {
        "status": "ЧЕКАТИ" if prob >= 50 else "ВХОДУ НЕМАЄ",
        "mode": "змішаний ринок",
        "strategy": "чекати, поки графік, 3m і структура зійдуться в один бік",
        "priority": "баланс: графік + новини + стакан",
        "aggression": "не агресивно",
    }


def compact_telegram_message(tv, signal, signal_type, confidence, quality, plan, technical_bias, fundamental_bias, news, event_risk, macro, orderflow, oi_analysis, market, session, reversal, priority, final_summary, weekend=None, cross_market=None, rr=None, chase=None, pos_note='', late_entry=None, cooling=None, smc=None, tech=None):
    raw_tech = tech or {}
    local_warning = ""
    decision = human_decision_line(signal, signal_type, reversal, technical_bias, news, event_risk)
    if late_entry and late_entry.get("late"):
        decision = late_entry.get("label") or decision

    tech_label = short_bias_label(technical_bias.get("side", "NEUTRAL"))
    fund_label = short_bias_label(fundamental_bias.get("side", "NEUTRAL"))
    priority_label = compact_priority_label(priority, reversal)
    driver = select_main_driver(raw_tech or technical_bias, news, event_risk, macro, orderflow, market, session, priority)
    trade_probability = estimate_trade_probability(signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk, orderflow, market, reversal, chase, weekend, late_entry, smc, raw_tech)
    confidence, trade_probability, local_warning = apply_local_3m_confidence_filter(signal, confidence, trade_probability, raw_tech)
    final_guard = final_signal_sanity_guard(
        signal, signal_type, confidence, raw_tech, smc or {}, raw_tech.get("micro_3m") or {}, trade_probability
    )
    if final_guard.get("reason"):
        signal = final_guard["signal"]
        signal_type = final_guard["signal_type"]
        confidence = final_guard["confidence"]
        plan = make_trade_plan(signal, signal_type, tv["price"], raw_tech, reversal, session, event_risk)
        plan = adjust_plan_for_rr(plan, signal)
        plan = apply_expansion_targets(plan, signal, raw_tech, market)
        quality = setup_quality_rank(signal, signal_type, technical_bias.get("score", 0), raw_tech, news, orderflow, macro, event_risk, market, oi_analysis)
        trade_probability = estimate_trade_probability(signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk, orderflow, market, reversal, chase, weekend, late_entry, smc, raw_tech)
        local_warning = ""
        confidence, trade_probability, local_warning = apply_local_3m_confidence_filter(signal, confidence, trade_probability, raw_tech)
        decision = human_decision_line(signal, signal_type, reversal, technical_bias, news, event_risk)
        if late_entry and late_entry.get("late"):
            decision = late_entry.get("label") or decision

    if local_warning:
        if "підтверджує LONG" in local_warning and trade_probability is not None and trade_probability >= 65:
            decision = "TRADE LONG — 3m підтверджує вхід"
        elif "підтверджує SHORT" in local_warning and trade_probability is not None and trade_probability >= 65:
            decision = "TRADE SHORT — 3m підтверджує вхід"
        elif signal == "LONG":
            decision = "LONG слабшає — чекати відкат"
        elif signal == "SHORT":
            decision = "SHORT слабшає — чекати відскок"
    exhaustion = extension_exhaustion_filter(signal, tech or {}, smc, news, event_risk)
    early_reversal = early_reversal_engine(tv, tech or {}, smc, news, event_risk)
    show_trade_plan = should_show_trade_plan(signal, trade_probability, late_entry)

    if show_trade_plan and signal in ["LONG", "SHORT"] and not str(decision).startswith("TRADE"):
        decision = f"TRADE {signal} — підтверджений вхід"
    entry_watch = proactive_entry_watch(signal, tv, tech or {}, smc, news, event_risk, early_reversal, trade_probability)
    trade_probability = apply_entry_watch_quality_floor(signal, trade_probability, tech or {}, news, event_risk, smc, entry_watch)
    trade_probability = apply_confirmed_trade_quality_floor(signal, trade_probability, tech or {}, news, event_risk, smc, orderflow)
    event_block = news_event_trade_block(signal, trade_probability, event_risk, news, session)
    if event_block.get("blocked"):
        trade_probability = min(trade_probability or 0, 54)
    show_trade_plan = should_show_trade_plan(signal, trade_probability, late_entry)

    if exhaustion.get("active") and trade_probability is not None and trade_probability < 65:
        if signal == "SHORT":
            decision = "ГОТУЄМОСЬ ДО SHORT — чекати тригер"
        elif signal == "LONG":
            decision = "ГОТУЄМОСЬ ДО LONG — чекати тригер"

    if early_reversal.get("active") and early_reversal.get("side") == signal and trade_probability is not None and trade_probability < 65:
        if signal == "LONG":
            decision = "ГОТУЄМОСЬ ДО LONG — чекати тригер"
        elif signal == "SHORT":
            decision = "ГОТУЄМОСЬ ДО SHORT — чекати тригер"

    # Never show TRADE if the setup is not confirmed.
    if trade_probability is not None and trade_probability < 65 and str(decision).startswith("TRADE"):
        if signal == "LONG":
            decision = "ГОТУЄМОСЬ ДО LONG — чекати тригер"
        elif signal == "SHORT":
            decision = "ГОТУЄМОСЬ ДО SHORT — чекати тригер"
        else:
            decision = "НЕ ВХОДИТИ — чекати тригер"

    if trade_probability is not None and trade_probability < 50 and late_entry and late_entry.get("late"):
        if signal == "LONG":
            decision = "ГОТУЄМОСЬ ДО LONG — тільки після відкату/утримання"
        elif signal == "SHORT":
            decision = "ГОТУЄМОСЬ ДО SHORT — тільки після відкату/утримання"
    if cooling and cooling.get("active"):
        if signal == "LONG":
            decision = "ГОТУЄМОСЬ ДО LONG — тільки після відкату/утримання"
        elif signal == "SHORT":
            decision = "ГОТУЄМОСЬ ДО SHORT — тільки після відкату/утримання"

    simple_decision = simple_decision_text(signal, trade_probability, late_entry, cooling)
    if simple_decision:
        decision = simple_decision
    if "РАННІЙ" in str(signal_type) and signal in ["LONG", "SHORT"] and trade_probability is not None and trade_probability >= 65:
        decision = f"РАННІЙ {signal} — можна входити зараз"
    if "СКАЛЬП" in str(signal_type) and signal in ["LONG", "SHORT"] and trade_probability is not None and trade_probability >= 55:
        decision = f"СКАЛЬП {signal} — рання точка, тільки зі стопом"
    if event_block.get("blocked"):
        decision = "Зараз не входити — високий новинний ризик"

    market_mode = market_mode_engine(
        signal, signal_type, trade_probability, raw_tech, smc, orderflow, news,
        event_risk, market, session, late_entry, cooling
    )

    # Telegram-level hard safety:
    # If the displayed decision is "різкий дамп/памп", the conclusion must NOT be a reversal headline.
    if "різкий дамп" in decision.lower():
        final_summary = (
            "Різкий дамп: технічний продаж зараз домінує. "
            "LONG по новинах можливий тільки після стабілізації, відскоку і ретесту. "
            "Не ловити падаючий ринок."
        )
    elif "різкий памп" in decision.lower() or "різкий ріст" in decision.lower():
        final_summary = (
            "Різкий ріст: покупці зараз домінують. "
            "SHORT можливий тільки після стабілізації, відкату і ретесту. "
            "Не шортити сильний імпульс без підтвердження."
        )

    # Direction label for the user.
    # It must show the real market direction (LONG/SHORT), not the internal signal value.
    # Example: signal can be "НЕЙТРАЛЬНО", while decision is "Чекаємо підтвердження LONG".
    decision_upper = str(decision).upper()
    conflict_note = signal_conflict_text(signal, driver, technical_bias, fundamental_bias, event_risk)
    driver = align_driver_with_final_signal(driver, signal, technical_bias, fundamental_bias, event_risk, smc, tech)
    main_reason = technical_reason_text(signal, technical_bias, smc, tech) if signal in ["LONG", "SHORT"] else driver.get("summary", "перевага нечітка — краще чекати")
    driver_text = f"{driver.get('type', '')} {driver.get('expectation', '')} {driver.get('summary', '')}".upper()

    technical_side_raw = technical_bias.get("side", "NEUTRAL")
    fundamental_side_raw = fundamental_bias.get("side", "NEUTRAL")
    tech_short = "SHORT" in technical_side_raw and "LONG" not in technical_side_raw
    tech_long = "LONG" in technical_side_raw and "SHORT" not in technical_side_raw
    fund_long = "LONG" in fundamental_side_raw and "SHORT" not in fundamental_side_raw
    fund_short = "SHORT" in fundamental_side_raw and "LONG" not in fundamental_side_raw
    shock_conflict = (
        signal == "НЕЙТРАЛЬНО"
        and ("SHOCK DOWN" in str(signal_type) or "SHOCK UP" in str(signal_type))
        and ((tech_short and fund_long) or (tech_long and fund_short))
    )

    if shock_conflict:
        market_bias = "CONFLICT"
    elif signal in ["LONG", "SHORT"]:
        market_bias = signal
    elif "LONG" in decision_upper:
        market_bias = "LONG"
    elif "SHORT" in decision_upper:
        market_bias = "SHORT"
    elif "LONG" in driver_text and "SHORT" not in driver_text:
        market_bias = "LONG"
    elif "SHORT" in driver_text and "LONG" not in driver_text:
        market_bias = "SHORT"
    elif fund_long and abs(fundamental_bias.get("score", 0)) >= abs(technical_bias.get("score", 0)):
        market_bias = "LONG"
    elif fund_short and abs(fundamental_bias.get("score", 0)) >= abs(technical_bias.get("score", 0)):
        market_bias = "SHORT"
    elif tech_long:
        market_bias = "LONG"
    elif tech_short:
        market_bias = "SHORT"
    else:
        market_bias = "НЕЙТРАЛЬНО"

    if shock_conflict:
        market_bias_text = "конфлікт"
        if "SHOCK DOWN" in str(signal_type):
            main_reason = "графік різко впав, але покупці почали відкуповувати"
        elif "SHOCK UP" in str(signal_type):
            main_reason = "графік різко виріс, але продавці можуть повернути тиск"
    elif market_bias in ["LONG", "SHORT"]:
        market_bias_text = f"{simple_direction_word(market_bias)} ({confidence}%)"
    else:
        market_bias_text = "змішано"

    calendar_text = economic_calendar_text(event_risk)
    scalp_prep = scalp_preparation_signal(tv, raw_tech, smc, orderflow, news, event_risk)
    compact_reasons = [
        f"графік {simple_bias_label(tech_label)} ({technical_bias.get('score')})",
        f"новини {simple_bias_label(fund_label)} ({fundamental_bias.get('score')})",
    ]
    if calendar_text:
        compact_reasons.append(calendar_text.replace("Календар: ", "календар "))

    top_decision = decision
    if market_mode.get("status") == "ВХОДУ НЕМАЄ" and str(top_decision).startswith("НЕ ВХОДИТИ"):
        top_decision = top_decision.replace("НЕ ВХОДИТИ — ", "")
    elif market_mode.get("status") == "ВХОДУ НЕМАЄ" and str(top_decision).startswith("Зараз не входити — "):
        top_decision = top_decision.replace("Зараз не входити — ", "")
    elif market_mode.get("status") == "ЧЕКАТИ" and str(top_decision).startswith("Чекати "):
        top_decision = top_decision.replace("Чекати ", "")

    lines = [
        "<b>📊 BZU SIGNAL BOT</b>",
        f"<b>{market_mode['status']}</b> — {top_decision}",
        f"<b>Режим:</b> {market_mode['mode']}",
        "",
        f"<b>Ринок:</b> {market_bias_text}",
        f"<b>Якість входу:</b> {entry_quality_scale(trade_probability, late_entry, signal_type)}",
        f"<b>Ціна:</b> {tv['price']} | {round(tv['change'], 4)}% | <b>{local_3m_status_text((tech or {}).get('micro_3m'), signal)}</b>",
        f"<b>План:</b> {proactive_plan_text(signal, trade_probability, show_trade_plan, plan, entry_watch)}",
        f"<b>Причини:</b> " + " | ".join(compact_reasons[:3]),
    ]

    if scalp_prep.get("active") and "СКАЛЬП" not in str(signal_type):
        detail = scalp_prep.get("reason")
        text = scalp_prep.get("text")
        lines.append(f"<b>{scalp_prep.get('status')}:</b> {text}" + (f" — {detail}" if detail else ""))

    if conflict_note:
        lines.append(f"<b>Конфлікт:</b> {conflict_note}")

    if event_block.get("blocked"):
        lines.append(f"<b>Новинний ризик:</b> {event_block.get('reason')}")

    micro_text = microstructure_compact_text(orderflow)
    smc_note = smc_compact_text(smc)
    flow_parts = []
    if micro_text:
        flow_parts.append(micro_text)
    if smc_note:
        flow_parts.append(smc_note)
    if flow_parts:
        lines.append("<b>" + " | ".join(flow_parts[:2]) + "</b>")

    no_entry_active = (trade_probability is None) or (trade_probability < 55) or (not show_trade_plan)

    risk_text = reversal_risk_note(signal, reversal)
    rev_text = compact_reversal_label(reversal)

    if risk_text:
        lines.append(f"<b>{risk_text}</b>")
    elif "різкий дамп" in decision.lower() and rev_text != "немає":
        lines.append("<b>Ризик:</b> можливий LONG-відскок пізніше")
    elif "різкий памп" in decision.lower() and rev_text != "немає":
        lines.append("<b>Ризик:</b> можливий SHORT-відкат пізніше")
    elif rev_text != "немає":
        lines.append(f"<b>Reversal:</b> {rev_text}")



    return "\n".join(lines).strip()

def setup_quality_rank(signal, signal_type, score, tech, news, orderflow, macro, event_risk, market, oi_analysis):
    if signal == "НЕЙТРАЛЬНО":
        return "NO TRADE"

    aligned = 0
    conflicts = 0
    target = signal

    components = [
        tech_verdict(tech)[0],
        news_verdict(news)[0],
        event_verdict(event_risk)[0],
        macro_verdict(macro)[0],
        orderflow_verdict(orderflow)[0],
        market_structure_verdict(market)[0],
        oi_analysis.get("side", "NEUTRAL"),
    ]

    for side in components:
        if side == target:
            aligned += 1
        elif side in ["LONG", "SHORT"] and side != target:
            conflicts += 1

    if abs(score) >= 150 and aligned >= 4 and conflicts <= 1 and "РИЗИКОВИЙ" not in signal_type:
        return "A+"
    if abs(score) >= 110 and aligned >= 3 and conflicts <= 2:
        return "A"
    if abs(score) >= 80 and aligned >= 2:
        return "B"
    return "C / ризиковий"

def should_show_trade_plan(signal, trade_probability, late_entry=None):
    """Show entry/SL/TP only for high-quality setups."""
    if signal not in ["LONG", "SHORT"]:
        return False
    if late_entry and late_entry.get("late") and (trade_probability or 0) < 65:
        return False
    return (trade_probability or 0) >= 65

def format_trade_plan(plan):
    if isinstance(plan, str):
        return plan
    if not plan or not isinstance(plan, dict) or plan.get("entry") is None:
        return "Входу немає — чекати підтвердження."
    return (
        f"Стоп: {plan.get('stop')} | "
        f"TP1: {plan.get('tp1')} | "
        f"TP2: {plan.get('tp2')} | "
        f"TP3: {plan.get('tp3')}"
    )

def format_time(dt):
    if not dt:
        return "час невідомий"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def dedupe_telegram_blocks(message):
    """Remove duplicated identical paragraphs from Telegram message."""
    parts = [p.strip() for p in message.split("\\n\\n") if p.strip()]
    seen = set()
    clean = []
    for part in parts:
        key = re.sub(r"\\s+", " ", part).strip()
        if key in seen:
            continue
        seen.add(key)
        clean.append(part)
    return "\\n\\n".join(clean)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram secrets missing")
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
        print(f"[WARN] Telegram error: {error}")

# ==========================================================
# MICRO 3M STRUCTURE
# ==========================================================

def analyze_micro_structure(candles):
    """3m local movement state.

    This version is stricter:
    if the last 3m candles are stair-stepping down, it must show SHORT,
    not 'боковик'.
    """
    if not candles or len(candles) < 30:
        return {
            "available": False,
            "bias": "NEUTRAL",
            "state": "NO DATA",
            "score": 0,
            "note": "3m data unavailable",
        }

    recent = candles[-24:]
    fast = candles[-12:]
    last8 = candles[-8:]
    last = candles[-1]

    closes = [c["close"] for c in recent]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]

    fast_closes = [c["close"] for c in fast]
    fast_highs = [c["high"] for c in fast]
    fast_lows = [c["low"] for c in fast]

    recent_high = max(highs[:-1])
    recent_low = min(lows[:-1])

    last4 = sum(closes[-4:]) / 4
    prev4 = sum(closes[-8:-4]) / 4
    first6 = sum(closes[:6]) / 6
    last6 = sum(closes[-6:]) / 6

    fast_start = fast_closes[0]
    fast_end = fast_closes[-1]
    fast_move_pct = ((fast_end - fast_start) / fast_start) * 100 if fast_start else 0

    avg_vol = sum(c.get("volume", 0) for c in recent[:-1]) / max(1, len(recent[:-1]))
    last_vol = last.get("volume", 0) or 0
    vol_ratio = last_vol / avg_vol if avg_vol else 1.0

    candle_range = max(last["high"] - last["low"], 1e-9)
    body = last["close"] - last["open"]
    body_ratio = abs(body) / candle_range
    close_location = (last["close"] - last["low"]) / candle_range

    # Structure counters
    lower_high_count = 0
    lower_low_count = 0
    higher_high_count = 0
    higher_low_count = 0
    lower_close_count = 0
    higher_close_count = 0

    for i in range(1, len(fast)):
        if fast_highs[i] < fast_highs[i - 1]:
            lower_high_count += 1
        if fast_lows[i] < fast_lows[i - 1]:
            lower_low_count += 1
        if fast_highs[i] > fast_highs[i - 1]:
            higher_high_count += 1
        if fast_lows[i] > fast_lows[i - 1]:
            higher_low_count += 1
        if fast_closes[i] < fast_closes[i - 1]:
            lower_close_count += 1
        if fast_closes[i] > fast_closes[i - 1]:
            higher_close_count += 1

    red_count = sum(1 for c in last8 if c["close"] < c["open"])
    green_count = sum(1 for c in last8 if c["close"] > c["open"])

    total_move_pct = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0
    drift_pct = ((last6 - first6) / first6) * 100 if first6 else 0

    short_momentum = last4 < prev4
    long_momentum = last4 > prev4

    score = 0
    notes = []

    # Local breakout/breakdown
    if last["close"] > recent_high:
        score += 24
        notes.append("пробій локального high")
    elif last["close"] < recent_low:
        score -= 24
        notes.append("пробій локального low")

    # Immediate 12-candle direction — this is the main fix
    if fast_move_pct <= -0.35:
        score -= 28
        notes.append("3m швидко йде вниз")
    elif fast_move_pct >= 0.35:
        score += 28
        notes.append("3m швидко йде вгору")

    # Stair-step structure
    if lower_high_count >= 5 or lower_close_count >= 7:
        score -= 24
        notes.append("3m lower highs / lower closes")
    if lower_low_count >= 4:
        score -= 14
        notes.append("3m оновлює lows")

    if higher_high_count >= 5 or higher_close_count >= 7:
        score += 24
        notes.append("3m higher highs / higher closes")
    if higher_low_count >= 4:
        score += 14
        notes.append("3m утримує higher lows")

    # Candle balance
    if red_count >= 5:
        score -= 12
        notes.append("більшість останніх 3m свічок червоні")
    elif green_count >= 5:
        score += 12
        notes.append("більшість останніх 3m свічок зелені")

    # Short-term momentum
    if long_momentum:
        score += 8
    elif short_momentum:
        score -= 8

    # Drift
    if drift_pct <= -0.22:
        score -= 12
        notes.append("3m ціна сповзає вниз")
    elif drift_pct >= 0.22:
        score += 12
        notes.append("3m ціна підтискається вгору")

    # Last candle
    if body > 0 and body_ratio >= 0.50 and close_location >= 0.58:
        score += 6
    elif body < 0 and body_ratio >= 0.50 and close_location <= 0.42:
        score -= 6

    # Volume
    if vol_ratio >= 1.5:
        if body > 0:
            score += 8
            notes.append("обсяг за покупців")
        elif body < 0:
            score -= 8
            notes.append("обсяг за продавців")

    # Very strict directional classification
    if score >= 18:
        bias = "LONG"
        state = "LONG_STRENGTHENING"
    elif score <= -18:
        bias = "SHORT"
        state = "SHORT_STRENGTHENING"
    else:
        bias = "NEUTRAL"
        state = "RANGE"

        if total_move_pct >= 0.30 and short_momentum:
            state = "LONG_COOLING"
            notes.append("LONG охолоджується після імпульсу")
        elif total_move_pct <= -0.30 and long_momentum:
            state = "SHORT_COOLING"
            notes.append("SHORT охолоджується після падіння")

    return {
        "available": True,
        "bias": bias,
        "state": state,
        "score": int(score),
        "note": "; ".join(notes[:3]) if notes else "3m боковик",
        "vol_ratio": round(vol_ratio, 2),
        "move_pct": round(total_move_pct, 3),
        "fast_move_pct": round(fast_move_pct, 3),
        "drift_pct": round(drift_pct, 3),
        "lower_high_count": lower_high_count,
        "lower_low_count": lower_low_count,
        "lower_close_count": lower_close_count,
        "red_count": red_count,
    }


def micro_structure_text(micro):
    """Human-readable 3m warning for Telegram."""
    if not micro or not micro.get("available"):
        return ""
    bias = micro.get("bias", "NEUTRAL")
    score = micro.get("score", 0)

    if bias == "SHORT":
        return f"<b>3m:</b> LONG слабшає — можливий відкат вниз ({score})"
    if bias == "LONG":
        return f"<b>3m:</b> SHORT слабшає — можливий відскок вгору ({score})"
    return ""

def structure_override_engine(signal, signal_type, confidence, score, tech, smc, micro, news, event_risk):
    """SMC/price action override.

    News can create the bias, but confirmed structure should cancel the opposite setup.
    This prevents LONG watch while SMC/3m already shows SHORT, and vice versa.
    """
    if signal not in ["LONG", "SHORT"]:
        return {
            "active": False,
            "signal": signal,
            "signal_type": signal_type,
            "confidence": confidence,
            "score": score,
            "reason": "",
        }

    tech = tech or {}
    smc = smc or {}
    micro = micro or {}
    news = news or {}
    event_risk = event_risk or {}

    smc_bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    choch = smc.get("choch", "NONE")
    smc_score = smc.get("score", 0) or 0

    micro_bias = micro.get("bias", "NEUTRAL")
    micro_score = micro.get("score", 0) or 0

    tech_score = tech.get("score", 0) or 0

    structure_short = (
        smc_bias == "SHORT"
        or bos == "пробій структури SHORT"
        or choch == "ознака розвороту SHORT"
        or smc_score <= -22
    )
    structure_long = (
        smc_bias == "LONG"
        or bos == "пробій структури LONG"
        or choch == "ознака розвороту LONG"
        or smc_score >= 22
    )

    micro_short = micro_bias == "SHORT" and micro_score <= -18
    micro_long = micro_bias == "LONG" and micro_score >= 18

    if signal == "LONG" and structure_short:
        if micro_short or tech_score <= -35:
            return {
                "active": True,
                "signal": "SHORT",
                "signal_type": "LONG СЛАБШАЄ / МОЖЛИВИЙ ВІДКАТ ВНИЗ",
                "confidence": min(68, max(55, abs(smc_score) + abs(micro_score))),
                "score": -abs(max(abs(score), abs(smc_score) + abs(micro_score) + 20)),
                "reason": "LONG скасовано: SMC/3m структура вже показує SHORT",
            }
        return {
            "active": True,
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "LONG СКАСОВАНО / ЧЕКАТИ НОВИЙ ТРИГЕР",
            "confidence": min(confidence, 50),
            "score": 0,
            "reason": "LONG скасовано: SMC проти сценарію",
        }

    if signal == "SHORT" and structure_long:
        if micro_long or tech_score >= 35:
            return {
                "active": True,
                "signal": "LONG",
                "signal_type": "SHORT СЛАБШАЄ / МОЖЛИВИЙ ВІДСКОК ВГОРУ",
                "confidence": min(68, max(55, abs(smc_score) + abs(micro_score))),
                "score": abs(max(abs(score), abs(smc_score) + abs(micro_score) + 20)),
                "reason": "SHORT скасовано: SMC/3m структура вже показує LONG",
            }
        return {
            "active": True,
            "signal": "НЕЙТРАЛЬНО",
            "signal_type": "SHORT СКАСОВАНО / ЧЕКАТИ НОВИЙ ТРИГЕР",
            "confidence": min(confidence, 50),
            "score": 0,
            "reason": "SHORT скасовано: SMC проти сценарію",
        }

    return {
        "active": False,
        "signal": signal,
        "signal_type": signal_type,
        "confidence": confidence,
        "score": score,
        "reason": "",
    }

def local_3m_status_text(micro, signal=None):
    """Human-readable 3m state."""
    if not micro or not micro.get("available"):
        return "Локально 3m: дані недоступні"

    state = micro.get("state", "RANGE")
    score = micro.get("score", 0)

    if state == "LONG_STRENGTHENING":
        if signal == "SHORT":
            return f"Локально 3m: шорт слабшає — покупці активні ({score})"
        return f"Локально 3m: лонг посилюється — покупці активні ({score})"

    if state == "SHORT_STRENGTHENING":
        if signal == "LONG":
            return f"Локально 3m: лонг слабшає — продавці активні ({score})"
        return f"Локально 3m: шорт посилюється — продавці активні ({score})"

    if state == "LONG_COOLING":
        return f"Локально 3m: лонг охолоджується — краще чекати ретест ({score})"

    if state == "SHORT_COOLING":
        return f"Локально 3m: шорт охолоджується — можливий відскок ({score})"

    return f"Локально 3m: боковик / немає чіткого входу ({score})"


def global_trend_text(tech, market_bias):
    """Human-readable global 15m/1h trend for Telegram."""
    tech = tech or {}
    trend_15m = tech.get("trend_15m", "UNKNOWN")
    trend_1h = tech.get("trend_1h", "UNKNOWN")

    if trend_15m == "UP" and trend_1h == "UP":
        return "Загалом: лонг"
    if trend_15m == "DOWN" and trend_1h == "DOWN":
        return "Загалом: шорт"
    if market_bias == "LONG":
        return "Загалом: лонг"
    if market_bias == "SHORT":
        return "Загалом: шорт"
    return "Загалом: змішано"

def apply_local_3m_confidence_filter(signal, confidence, trade_probability, tech):
    """Adjust confidence/quality by local 3m timing and state."""
    micro = (tech or {}).get("micro_3m") or {}
    if signal not in ["LONG", "SHORT"] or not micro.get("available"):
        return confidence, trade_probability, ""

    micro_bias = micro.get("bias", "NEUTRAL")
    micro_state = micro.get("state", "RANGE")
    micro_score = micro.get("score", 0) or 0

    # Against main signal = reduce
    if signal == "LONG" and (micro_bias == "SHORT" or micro_state in ["LONG_COOLING", "LONG_PAUSE"]):
        confidence = min(confidence, 62)
        if trade_probability is not None:
            trade_probability = min(trade_probability, 58)
        return confidence, trade_probability, "LONG охолоджується на 3m — краще чекати ретест"

    if signal == "SHORT" and (micro_bias == "LONG" or micro_state in ["SHORT_COOLING", "SHORT_PAUSE"]):
        confidence = min(confidence, 62)
        if trade_probability is not None:
            trade_probability = min(trade_probability, 58)
        return confidence, trade_probability, "SHORT охолоджується на 3m — краще чекати ретест"

    # Same direction = boost
    if signal == "LONG" and micro_state == "LONG_STRENGTHENING":
        confidence = min(95, confidence + 6)
        if trade_probability is not None:
            trade_probability = max(trade_probability, 66 if micro_score < 30 else 70)
        return confidence, trade_probability, "3m підтверджує LONG — покупці активні"

    if signal == "SHORT" and micro_state == "SHORT_STRENGTHENING":
        confidence = min(95, confidence + 6)
        if trade_probability is not None:
            trade_probability = max(trade_probability, 66 if micro_score > -30 else 70)
        return confidence, trade_probability, "3m підтверджує SHORT — продавці активні"

    return confidence, trade_probability, ""

# ==========================================================
# MAIN
# ==========================================================

def main():
    print("START BZU PROFESSIONAL FREE BOT UA REVERSAL-SESSION")

    tv = get_tradingview_market_data()
    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    signal_memory = load_signal_memory()
    previous_signal_note = memory_status_note(signal_memory, tv["price"])
    signal_journal = load_signal_journal()

    fresh_news = get_all_fresh_news()
    event_items = get_event_news()

    tech = analyze_technical(tv)
    
    real_candles = get_real_candles()
    signal_journal, _ = update_signal_journal_results(signal_journal, tv["price"], real_candles)
    journal_stats_note = signal_journal_stats_text(signal_journal)
    backtest_smoke = quick_backtest_smoke(real_candles)
    smc = analyze_smc_structure(real_candles)

    micro_candles = get_real_candles(bar="3m", limit=160)
    micro = analyze_micro_structure(micro_candles)
    tech["micro_3m"] = micro

    if smc.get('available'):
        tech['score'] += int(smc.get('score', 0))
        tech.setdefault('confirmations', []).append('SMC: ' + smc.get('summary', ''))

    # 3m is a micro-trigger only. It has small weight, but can override false news bias with SMC.
    if micro.get("available"):
        tech['score'] += int(max(-18, min(18, micro.get("score", 0))))
        tech.setdefault('confirmations', []).append('MICRO 3m: ' + micro.get('note', ''))

    news = analyze_news(fresh_news)
    orderflow = analyze_free_orderflow(tv)
    okx_trades = get_okx_recent_trades(limit=100)
    real_trade_flow = analyze_okx_trade_flow(okx_trades)
    orderflow = merge_orderflow_proxy(orderflow, real_trade_flow)
    okx_book = get_okx_order_book(depth=50)
    order_book_pressure = analyze_order_book_pressure(okx_book, tv["price"])
    liquidity_proxy = analyze_liquidity_proxy(real_candles, real_trade_flow, order_book_pressure)
    orderflow = merge_market_microstructure(orderflow, order_book_pressure, liquidity_proxy)
    macro_data = get_macro_quant_data()
    macro = analyze_macro_quant(macro_data)
    event_risk = analyze_event_risk(event_items)
    economic_calendar = analyze_economic_calendar()
    event_risk = merge_calendar_into_event_risk(event_risk, economic_calendar)
    market = analyze_market_structure(tv, tech)
    oi_analysis = analyze_synthetic_open_interest(tv, tech, orderflow, market)
    session = analyze_session_context()
    
    weekend = analyze_weekend_mode()
    cross_data = get_cross_market_data()
    cross_market = analyze_cross_market(cross_data, tech)
    reversal = analyze_reversal_watch(tv, tech, news, event_risk, orderflow, market, oi_analysis, session)
    priority = analyze_priority_engine(tech, news, event_risk, macro, orderflow, market, session, reversal)

    signal, signal_type, score, confidence, risk_note = build_signal(
        tech, news, orderflow, macro, event_risk, market, oi_analysis, session, reversal, priority, weekend, cross_market
    )

    structure_override = structure_override_engine(signal, signal_type, confidence, score, tech, smc, micro, news, event_risk)
    if structure_override.get("active"):
        signal = structure_override["signal"]
        signal_type = structure_override["signal_type"]
        confidence = structure_override["confidence"]
        score = structure_override["score"]
        risk_note = structure_override.get("reason", risk_note)

    memory_adj = memory_confidence_adjustment(signal, signal_memory, tv["price"])
    if memory_adj:
        score += memory_adj
        confidence = max(35, min(95, confidence + memory_adj))
        print(f"MEMORY ADJUSTMENT: {memory_adj}")

    price_override = price_structure_priority_override(
        signal, signal_type, confidence, score, tech, smc, news, event_risk, micro
    )
    if price_override.get("reason"):
        signal = price_override["signal"]
        signal_type = price_override["signal_type"]
        confidence = price_override["confidence"]
        score = price_override["score"]
        risk_note = price_override.get("reason", risk_note)
        print("PRICE STRUCTURE OVERRIDE:", risk_note)

    current_truth_filter = price_action_truth_filter(signal, tech, smc, news, event_risk, orderflow)
    if current_truth_filter.get('blocked'):
        signal = 'НЕЙТРАЛЬНО'
        signal_type = 'НЕ ВХОДИТИ — ціна не підтвердила новини'
        risk_note = current_truth_filter.get('reason')
        confidence = min(confidence, 50)
    elif current_truth_filter.get('bonus'):
        score += current_truth_filter.get('bonus', 0)
        confidence = min(95, max(0, abs(score)))

    early_entry = early_entry_signal(tv, signal, signal_type, tech, smc, orderflow, news, event_risk, market, session)
    if early_entry.get("active"):
        signal = early_entry["side"]
        signal_type = early_entry["signal_type"]
        confidence = max(confidence, early_entry.get("confidence", confidence))
        score = early_entry.get("score", score)
        risk_note = early_entry.get("reason", risk_note)
        print(f"EARLY ENTRY TRIGGER: {signal} | {risk_note}")

    scalp_reversal = reversal_scalp_signal(tv, tech, smc, orderflow, news, event_risk, session)
    if scalp_reversal.get("active"):
        signal = scalp_reversal["side"]
        signal_type = scalp_reversal["signal_type"]
        confidence = max(confidence, scalp_reversal.get("confidence", confidence))
        score = scalp_reversal.get("score", score)
        risk_note = scalp_reversal.get("reason", risk_note)
        print(f"SCALP REVERSAL TRIGGER: {signal} | {risk_note}")

    plan = make_trade_plan(signal, signal_type, tv["price"], tech, reversal, session, event_risk)
    plan = adjust_plan_for_rr(plan, signal)
    rr = rr_metrics(plan)
    chase = analyze_chase_protection(signal, tech, market)
    
    late_entry = analyze_late_entry_risk(signal, tech, market)
    
    cooling = analyze_exhaustion_cooling(signal, tech, tv)
    plan = apply_expansion_targets(plan, signal, tech, market)
    rr = rr_metrics(plan)
    pos_note = position_management_note(signal, plan, tech, news, event_risk, reversal)

    tech_side, tech_reason = tech_verdict(tech)
    news_side, news_reason = news_verdict(news)
    event_side, event_reason = event_verdict(event_risk)
    macro_side, macro_reason = macro_verdict(macro)
    order_side, order_reason = orderflow_verdict(orderflow)
    market_side, market_reason = market_structure_verdict(market)
    quality = setup_quality_rank(signal, signal_type, score, tech, news, orderflow, macro, event_risk, market, oi_analysis)

    print(f"SOURCE: {tv['source']}")
    print(f"SYMBOL: {tv['symbol']}")
    print(f"PRICE: {tv['price']}")
    print(f"CHANGE: {tv['change']}")
    print(f"FRESH NEWS COUNT: {news['total']}")
    print(f"NEWS RAW SCORE: {news['raw_score']}")
    print(f"NEWS CAPPED SCORE: {news['score']}")
    print(f"TECH SCORE: {tech['score']}")
    print(f"SMC STRUCTURE: {smc.get('phase')} | {smc.get('bias')} | SCORE: {smc.get('score')} | {smc.get('summary')}")
    print(f"SMC VOLUME: {smc.get('volume', {}).get('bias', 'NEUTRAL')} | {smc.get('volume', {}).get('поглинання', 'NONE')} | {smc.get('volume', {}).get('note', '')}")
    print(backtest_smoke.get("summary", "Backtest smoke unavailable"))
    print(f"ORDERFLOW PROXY SCORE: {orderflow['score']} | {orderflow['bias']}")
    print(f"OKX TRADE FLOW: {real_trade_flow['bias']} | DELTA: {real_trade_flow['delta_pct']}% | {real_trade_flow['note']}")
    print(f"OKX BOOK: {order_book_pressure['bias']} | IMBALANCE: {order_book_pressure['imbalance_pct']}% | {order_book_pressure['note']} | {order_book_pressure.get('wall', '')}")
    print(f"LIQUIDITY PROXY: {liquidity_proxy['bias']} | SCORE: {liquidity_proxy['score']} | {liquidity_proxy['note']}")
    print(f"MACRO SCORE: {macro['score']} | {macro['regime']}")
    print(f"EVENT RISK: {event_risk['risk']} | SCORE: {event_risk['score']} | DIRECTION: {event_risk['direction']}")
    print(f"ECONOMIC CALENDAR: {economic_calendar['risk']} | ACTIVE: {economic_calendar['active']} | EVENTS: {len(economic_calendar.get('events', []))}")
    print(f"MARKET STRUCTURE SCORE: {market['score']} | VOL: {market['volatility']['regime']} | LIQ: {market['liquidation']['bias']}")
    print(f"OPEN INTEREST: {oi_analysis['summary']} | SCORE: {oi_analysis['score']}")
    print(f"SESSION: {session['session']} | SCORE: {session['score']} | {session['note']}")
    print(f"WEEKEND: {weekend['label']} | {weekend['note']}")
    print(f"CROSS-MARKET: {cross_market['bias']} | SCORE: {cross_market['score']} | {cross_market['note']}")
    print(f"PRIORITY: {priority['regime']} | DOMINANT: {priority['dominant']} | P-SCORE: {priority['priority_score']}")
    if "trust_mode" not in locals():
        early_warning = analyze_early_warning(None, tech, news, event_risk, orderflow, market, oi_analysis, session)
        trust_mode, trust_reason = decide_current_priority(tech, news, event_risk, orderflow, early_warning)
    print(f"TRUST MODE: {trust_mode} | {trust_reason}")
    print(f"EARLY WARNING: {early_warning['warning']} | SIDE: {early_warning['side']} | {early_warning['reason']}")
    print(f"REVERSAL: {reversal['side']} | CONF: {reversal['confidence']} | SWEEP: {reversal['sweep']}")
    print(f"MOMENTUM: {tech['momentum']} | CHANGE: {tech['change']}%")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL TYPE: {signal_type}")
    print(f"SIGNAL: {signal}")
    print(f"PRICE ACTION FILTER: {current_truth_filter.get('mode')} | {current_truth_filter.get('reason')}")

    if signal == "НЕЙТРАЛЬНО":
        decision = "НЕ ВХОДИТИ"
    elif "РИЗИКОВИЙ" in signal_type or "ІМПУЛЬСНИЙ" in signal_type:
        decision = f"{signal}, але обережно"
    else:
        decision = signal

    technical_bias = combined_technical_bias(tech, orderflow, market, oi_analysis)
    fundamental_bias = combined_fundamental_bias(news, event_risk, macro)
    market_decision = market_decision_from_bias(signal, signal_type, technical_bias, fundamental_bias, event_risk, reversal, session, priority)

    # Впевненість тепер означає якість рішення, а не win-rate.
    # Для NO TRADE у чіткому конфлікті вона може бути високою: бот впевнено каже "не входити".
    confidence = decision_confidence(
        signal, signal_type, score, technical_bias, fundamental_bias, event_risk, priority, reversal
    )

    pre_message_probability = estimate_trade_probability(
        signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk,
        orderflow, market, reversal, chase, weekend, late_entry, smc, tech
    )
    confidence, pre_message_probability, _ = apply_local_3m_confidence_filter(
        signal, confidence, pre_message_probability, tech
    )
    message_guard = final_signal_sanity_guard(
        signal, signal_type, confidence, tech, smc, tech.get("micro_3m") or {}, pre_message_probability
    )
    if message_guard.get("reason"):
        signal = message_guard["signal"]
        signal_type = message_guard["signal_type"]
        confidence = message_guard["confidence"]
        if signal == "LONG":
            score = abs(score) if score else abs(technical_bias.get("score", 0))
        elif signal == "SHORT":
            score = -abs(score) if score else -abs(technical_bias.get("score", 0))
        else:
            score = 0
        plan = make_trade_plan(signal, signal_type, tv["price"], tech, reversal, session, event_risk)
        plan = adjust_plan_for_rr(plan, signal)
        plan = apply_expansion_targets(plan, signal, tech, market)
        rr = rr_metrics(plan)
        chase = analyze_chase_protection(signal, tech, market)
        late_entry = analyze_late_entry_risk(signal, tech, market)
        cooling = analyze_exhaustion_cooling(signal, tech, tv)
        pos_note = position_management_note(signal, plan, tech, news, event_risk, reversal)
        quality = setup_quality_rank(signal, signal_type, score, tech, news, orderflow, macro, event_risk, market, oi_analysis)
        market_decision = market_decision_from_bias(signal, signal_type, technical_bias, fundamental_bias, event_risk, reversal, session, priority)
        print("FINAL SANITY GUARD:", message_guard.get("reason"))

    summary = final_short_summary(
        signal, signal_type, tech, news, orderflow, macro, event_risk, market, oi_analysis, reversal, session
    )

    reversal_label = reversal_display_label(signal, reversal)
    message = compact_telegram_message(
        tv=tv,
        signal=signal,
        signal_type=signal_type,
        confidence=confidence,
        quality=quality,
        plan=plan,
        technical_bias=technical_bias,
        fundamental_bias=fundamental_bias,
        news=news,
        event_risk=event_risk,
        macro=macro,
        orderflow=orderflow,
        oi_analysis=oi_analysis,
        market=market,
        session=session,
        reversal=reversal,
        priority=priority,
        final_summary=summary,
        weekend=weekend,
        cross_market=cross_market,
        rr=rr,
        chase=chase,
        pos_note=pos_note,
        late_entry=late_entry,
        smc=smc,
        cooling=cooling,
        tech=tech
    )

    show_memory_in_telegram = os.getenv("SHOW_MEMORY_IN_TELEGRAM", "0") == "1"
    position_note = position_follow_note(signal_memory, tv["price"], tech) if "position_follow_note" in globals() else ""
    if show_memory_in_telegram and position_note and position_note not in message:
        message = message.strip() + "\n\n" + position_note

    if show_memory_in_telegram and previous_signal_note and previous_signal_note not in message:
        message = message.strip() + "\n\n" + previous_signal_note

    current_quality_percent = estimate_trade_probability(
        signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk,
        orderflow, market, reversal, chase, weekend, late_entry, smc, tech
    )
    journal_market_direction = infer_market_direction(
        signal,
        signal_type,
        tech=tech,
        technical_bias=technical_bias,
        fundamental_bias=fundamental_bias,
    )

    current_journal_entry = build_signal_journal_entry(
        signal=signal,
        signal_type=signal_type,
        price=tv["price"],
        confidence=confidence,
        quality_percent=current_quality_percent,
        plan=plan,
        tech=tech,
        news=news,
        event_risk=event_risk,
        orderflow=orderflow,
        smc=smc,
        late_entry=late_entry,
        cooling=cooling,
        market_direction=journal_market_direction,
    )
    pattern_note = pattern_stats_text(signal_journal, current_journal_entry.get("tags"), journal_market_direction)
    signal_journal = append_signal_journal(signal_journal, current_journal_entry)
    save_signal_journal(signal_journal)

    show_stats_in_telegram = os.getenv("SHOW_STATS_IN_TELEGRAM", "0") == "1"

    if show_stats_in_telegram and pattern_note and pattern_note not in message:
        message = message.strip() + "\n\n" + pattern_note

    if show_stats_in_telegram and journal_stats_note and journal_stats_note not in message:
        message = message.strip() + "\n\n" + journal_stats_note

    updated_memory = append_signal_memory(
        signal_memory,
        build_current_signal_memory(
            signal=signal,
            signal_type=signal_type,
            price=tv["price"],
            confidence=confidence,
            quality_percent=current_quality_percent,
            plan=plan,
            tech=tech,
        )
    )
    save_signal_memory(updated_memory)

    message = dedupe_telegram_blocks(message.strip())
    send_telegram(message.strip())
    print("TELEGRAM SENT")
    print("BOT COMPLETE")

if __name__ == "__main__":
    main()
