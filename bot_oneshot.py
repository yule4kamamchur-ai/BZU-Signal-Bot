  import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus


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
    'Brent crude oil Trump tariff sanctions OPEC EIA',
    'oil prices Brent crude breaking news today',
    'crude oil inventory EIA API OPEC',
    'Iran Russia Ukraine Hormuz oil sanctions',
]

EVENT_QUERIES = [
    'Fed speech Powell today FOMC oil market',
    'CPI release today Fed inflation oil market',
    'NFP jobs report Fed oil market',
    'EIA crude oil inventories today API crude draw build',
    'OPEC meeting production cut increase crude oil',
    'Iran US talks sanctions oil Hormuz Trump',
]

MACRO_NEWS_QUERIES = [
    'Fed Powell rate cut inflation dollar yields stock market',
    'DXY dollar yields VIX stocks oil market today',
    'CPI FOMC NFP Fed speech market reaction',
    'S&P 500 Nasdaq VIX dollar risk on risk off today',
]

NEWS_SOURCES = [
    {
        "name": "EIA Today in Energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "type": "rss",
        "weight": 1.2,
    },
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
        "weight": 0.35,
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "type": "rss",
        "weight": 0.35,
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
    """Stable macro layer through Google News RSS instead of TradingView macro symbols.
    This avoids constant TVC:US10Y / NASDAQ:NDX / UKOIL unavailable warnings on GitHub.
    """
    macro_items = []
    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for query in MACRO_NEWS_QUERIES:
        macro_items.extend(parse_google_rss(query, 6, "Google Macro RSS", 0.9))

    # keep only reasonably fresh macro items
    filtered = []
    for item in macro_items:
        published_at = item.get("published_at")
        if published_at and published_at < cutoff:
            continue
        filtered.append(item)

    return deduplicate_news(filtered)


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
        regime = "НЕЙТРАЛЬНИЙ / macro RSS unavailable"

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
        details.append(f"обсяг TradingView: {round(volume, 2)}")
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
        "used_symbol": "TradingView only",
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
                news.append({
                    "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                    "link": link,
                    "source": source_name,
                    "published_at": published_at,
                    "weight": weight,
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
    return deduplicate_news(all_events)


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

        if bull_hits:
            bullish += 1
            raw_score += 6 * bull_hits * weight
        if bear_hits:
            bearish += 1
            raw_score -= 6 * bear_hits * weight
        if directional > 0:
            bullish += 1
            raw_score += directional * weight
        elif directional < 0:
            bearish += 1
            raw_score += directional * weight
        if impact_hits:
            impact += 1
            raw_score += 4 * impact_hits * weight
            important.append(item)
        if breaking_hits:
            breaking += 1
            raw_score += 7 * breaking_hits * weight
            if item not in important:
                important.append(item)

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

# ==========================================================
# SIGNAL ENGINE
# ==========================================================

def build_signal(tech, news, orderflow, macro, event_risk, market, oi_analysis, session, reversal):
    score = (
        tech["score"]
        + news["score"]
        + orderflow["score"]
        + macro["score"]
        + event_risk["score"]
        + market["score"]
        + oi_analysis["score"]
        + session.get("score", 0)
    )

    signal_type = "НЕМАЄ УГОДИ"
    signal = "NO SIGNAL"

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

    # Reversal Watch is not an automatic aggressive entry. It can upgrade a conflict into a watch-signal.
    if reversal.get("side") == "REVERSAL LONG WATCH" and reversal.get("confidence", 0) >= 45 and news.get("score", 0) >= 30:
        signal = "NO SIGNAL"
        signal_type = "REVERSAL LONG WATCH / ЧЕКАТИ ПІДТВЕРДЖЕННЯ"
    elif reversal.get("side") == "REVERSAL SHORT WATCH" and reversal.get("confidence", 0) >= 45 and news.get("score", 0) <= -25:
        signal = "NO SIGNAL"
        signal_type = "REVERSAL SHORT WATCH / ЧЕКАТИ ПІДТВЕРДЖЕННЯ"
    elif tech.get("momentum") == "VERY STRONG UP" and score >= 35:
        signal = "LONG"
        signal_type = "ІМПУЛЬСНИЙ LONG / BREAKOUT SCALP"
    elif tech.get("momentum") == "VERY STRONG DOWN" and score <= -35:
        signal = "SHORT"
        signal_type = "ІМПУЛЬСНИЙ SHORT / BREAKDOWN SCALP"
    elif score >= 75 and tech.get("trend") == "UP" and orderflow["score"] >= 20 and news["score"] >= 10 and macro["score"] >= 0:
        signal = "LONG"
        signal_type = "ПІДТВЕРДЖЕНИЙ TREND LONG"
    elif score <= -75 and tech.get("trend") == "DOWN" and orderflow["score"] <= -20 and news["score"] <= -10 and macro["score"] <= 0:
        signal = "SHORT"
        signal_type = "ПІДТВЕРДЖЕНИЙ TREND SHORT"
    elif score >= 90 and tech.get("trend") in ["UP", "MIXED"] and orderflow["score"] >= 8:
        signal = "LONG"
        signal_type = "РИЗИКОВИЙ LONG / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"
    elif score <= -90 and tech.get("trend") in ["DOWN", "MIXED"] and orderflow["score"] <= -8:
        signal = "SHORT"
        signal_type = "РИЗИКОВИЙ SHORT / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"

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
        risk_note = "Reversal watch: не входити одразу, чекати підтвердження/ретест"
    if macro["score"] <= -25 and signal == "LONG":
        risk_note = "Macro risk-off проти LONG — тільки малий обʼєм або пропуск"
    if macro["score"] >= 25 and signal == "SHORT":
        risk_note = "Macro risk-on проти SHORT — тільки малий обʼєм або пропуск"
    if tech.get("trend") == "DOWN" and signal == "LONG":
        risk_note = "Тільки скальп: старший тренд не підтвердив LONG"
    if tech.get("trend") == "UP" and signal == "SHORT":
        risk_note = "Тільки скальп: старший тренд не підтвердив SHORT"

    return signal, signal_type, score, confidence, risk_note


def make_trade_plan(signal, signal_type, price, tech):
    atr = tech.get("atr_15m") or price * 0.006

    if signal == "NO SIGNAL":
        return {
            "entry": None,
            "stop": None,
            "tp1": None,
            "tp2": None,
            "tp3": None,
            "note": "Не входити. Чекати підтвердження.",
        }

    if reversal and reversal.get("side") == "REVERSAL LONG WATCH":
        return "Можливий reversal LONG: техніка ще не підтвердила вхід, але фундамент/події проти SHORT. Чекати повернення вище ключового рівня або ретест."
    if reversal and reversal.get("side") == "REVERSAL SHORT WATCH":
        return "Можливий reversal SHORT: техніка ще не підтвердила вхід, але фундамент/події проти LONG. Чекати пробій/ретест."

    if signal == "LONG":
        if "ІМПУЛЬСНИЙ" in signal_type:
            stop = price - atr * 1.1
            tp1 = price + atr * 0.9
            tp2 = price + atr * 1.6
            tp3 = price + atr * 2.4
            note = "Імпульсний скальп: краще входити на відкаті/ретесті, не після великої свічки."
        else:
            stop = price - atr * 1.5
            tp1 = price + atr * 1.2
            tp2 = price + atr * 2.0
            tp3 = price + atr * 3.0
            note = "Trend long: вхід тільки якщо ціна утримує EMA20/EMA50."
    else:
        if "ІМПУЛЬСНИЙ" in signal_type:
            stop = price + atr * 1.1
            tp1 = price - atr * 0.9
            tp2 = price - atr * 1.6
            tp3 = price - atr * 2.4
            note = "Імпульсний скальп: краще входити на відкаті/ретесті, не після великої свічки."
        else:
            stop = price + atr * 1.5
            tp1 = price - atr * 1.2
            tp2 = price - atr * 2.0
            tp3 = price - atr * 3.0
            note = "Trend short: вхід тільки якщо ціна відбивається від EMA20/EMA50 вниз."

    return {
        "entry": round(price, 4),
        "stop": round(stop, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "tp3": round(tp3, 4),
        "note": note,
    }



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


def final_short_summary(signal, signal_type, tech, news, orderflow, macro, event_risk, market=None, oi_analysis=None, reversal=None, session=None):
    tech_side, _ = tech_verdict(tech)
    news_side, _ = news_verdict(news)
    event_side, _ = event_verdict(event_risk)
    macro_side, _ = macro_verdict(macro)
    order_side, _ = orderflow_verdict(orderflow)
    market_side = "NEUTRAL"
    if market:
        market_side, _ = market_structure_verdict(market)

    long_votes = [tech_side, news_side, event_side, macro_side, order_side, market_side].count("LONG")
    short_votes = [tech_side, news_side, event_side, macro_side, order_side, market_side].count("SHORT")

    if reversal and reversal.get("side") == "REVERSAL LONG WATCH":
        return "Можливий reversal LONG: техніка ще не підтвердила вхід, але фундамент/події проти SHORT. Чекати повернення вище ключового рівня або ретест."
    if reversal and reversal.get("side") == "REVERSAL SHORT WATCH":
        return "Можливий reversal SHORT: техніка ще не підтвердила вхід, але фундамент/події проти LONG. Чекати пробій/ретест."

    if signal == "LONG":
        if oi_analysis and oi_analysis.get("side") == "SHORT":
            return "LONG є, але OI/позиціювання проти входу. Краще чекати ретест або пропустити."
        if market and market["volatility"]["regime"] == "HIGH VOLATILITY / BREAKOUT MODE":
            return "LONG є, але режим високої волатильності. Не доганяти рух; чекати відкат/ретест і чіткий стоп."
        if event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
            return "Є LONG, але подієвий ризик високий. Не доганяти свічку; краще чекати відкат/ретест або входити мінімальним обсягом."
        if tech.get("trend") == "UP" and orderflow.get("score", 0) >= 20 and macro.get("score", 0) >= 0:
            return "LONG достатньо підтверджений. Вхід можливий тільки за планом і зі стопом."
        return "LONG ризиковий. Перевага вгору є, але підтвердження не ідеальні."

    if signal == "SHORT":
        if oi_analysis and oi_analysis.get("side") == "LONG":
            return "SHORT є, але OI/позиціювання проти входу. Краще чекати підтвердження або пропустити."
        if market and market["volatility"]["regime"] == "HIGH VOLATILITY / BREAKOUT MODE":
            return "SHORT є, але режим високої волатильності. Краще чекати пробій/ретест, бо можливий різкий відскок."
        if event_risk.get("risk") in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"]:
            return "Є SHORT, але подієвий ризик високий. Краще чекати підтвердження пробою/ретесту."
        if tech.get("trend") == "DOWN" and orderflow.get("score", 0) <= -20 and macro.get("score", 0) <= 0:
            return "SHORT достатньо підтверджений. Вхід можливий тільки за планом і зі стопом."
        return "SHORT ризиковий. Перевага вниз є, але підтвердження не ідеальні."

    if long_votes > short_votes:
        return "Сигналу на вхід немає. Перевага більше в бік LONG, але підтвердження недостатні — чекати кращий сетап."
    if short_votes > long_votes:
        return "Сигналу на вхід немає. Перевага більше в бік SHORT, але підтвердження недостатні — чекати кращий сетап."
    return "Сигналу на вхід немає. Картина змішана — краще не відкривати позицію."



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


def market_decision_from_bias(signal, signal_type, technical_bias, fundamental_bias, event_risk, reversal=None, session=None):
    tech_side = technical_bias["side"]
    fund_side = fundamental_bias["side"]

    tech_short = "SHORT" in tech_side
    tech_long = "LONG" in tech_side
    fund_short = "SHORT" in fund_side
    fund_long = "LONG" in fund_side

    if reversal and reversal.get("side") == "REVERSAL LONG WATCH":
        return "REVERSAL LONG WATCH: можливий розворот вгору — чекати підтвердження"
    if reversal and reversal.get("side") == "REVERSAL SHORT WATCH":
        return "REVERSAL SHORT WATCH: можливий розворот вниз — чекати підтвердження"

    if signal == "NO SIGNAL":
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


def setup_quality_rank(signal, signal_type, score, tech, news, orderflow, macro, event_risk, market, oi_analysis):
    if signal == "NO SIGNAL":
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

def format_trade_plan(plan):
    if not plan or plan.get("entry") is None:
        return "Входу немає — чекати підтвердження."
    return (
        f"Вхід: {plan['entry']} | Стоп: {plan['stop']} | "
        f"TP1: {plan['tp1']} | TP2: {plan['tp2']} | TP3: {plan['tp3']}"
    )


def format_time(dt):
    if not dt:
        return "час невідомий"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


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
# MAIN
# ==========================================================

def main():
    print("START BZU PROFESSIONAL FREE BOT UA REVERSAL-SESSION")

    tv = get_tradingview_market_data()
    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    fresh_news = get_all_fresh_news()
    event_items = get_event_news()

    tech = analyze_technical(tv)
    news = analyze_news(fresh_news)
    orderflow = analyze_free_orderflow(tv)
    macro_data = get_macro_quant_data()
    macro = analyze_macro_quant(macro_data)
    event_risk = analyze_event_risk(event_items)
    market = analyze_market_structure(tv, tech)
    oi_analysis = analyze_synthetic_open_interest(tv, tech, orderflow, market)
    session = analyze_session_context()
    reversal = analyze_reversal_watch(tv, tech, news, event_risk, orderflow, market, oi_analysis, session)

    signal, signal_type, score, confidence, risk_note = build_signal(
        tech, news, orderflow, macro, event_risk, market, oi_analysis, session, reversal
    )
    plan = make_trade_plan(signal, signal_type, tv["price"], tech)

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
    print(f"ORDERFLOW SCORE: {orderflow['score']} | {orderflow['bias']}")
    print(f"MACRO SCORE: {macro['score']} | {macro['regime']}")
    print(f"EVENT RISK: {event_risk['risk']} | SCORE: {event_risk['score']} | DIRECTION: {event_risk['direction']}")
    print(f"MARKET STRUCTURE SCORE: {market['score']} | VOL: {market['volatility']['regime']} | LIQ: {market['liquidation']['bias']}")
    print(f"OPEN INTEREST: {oi_analysis['summary']} | SCORE: {oi_analysis['score']}")
    print(f"SESSION: {session['session']} | SCORE: {session['score']} | {session['note']}")
    print(f"REVERSAL: {reversal['side']} | CONF: {reversal['confidence']} | SWEEP: {reversal['sweep']}")
    print(f"MOMENTUM: {tech['momentum']} | CHANGE: {tech['change']}%")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL TYPE: {signal_type}")
    print(f"SIGNAL: {signal}")

    if signal == "NO SIGNAL":
        decision = "НЕ ВХОДИТИ"
    elif "РИЗИКОВИЙ" in signal_type or "ІМПУЛЬСНИЙ" in signal_type:
        decision = f"{signal}, але обережно"
    else:
        decision = signal

    technical_bias = combined_technical_bias(tech, orderflow, market, oi_analysis)
    fundamental_bias = combined_fundamental_bias(news, event_risk, macro)
    market_decision = market_decision_from_bias(signal, signal_type, technical_bias, fundamental_bias, event_risk, reversal, session)

    message = f"""
<b>📊 BZU SIGNAL BOT</b>

<b>Рішення:</b> {market_decision}
<b>Якість:</b> {quality}
<b>Сигнал:</b> {signal} / {signal_type}
<b>Впевненість:</b> {confidence}%

<b>Ціна:</b> {tv['price']} | <b>Зміна:</b> {round(tv['change'], 4)}%
<b>План:</b> {format_trade_plan(plan)}

<b>TECHNICAL BIAS:</b> {technical_bias['side']}
{technical_bias['reason']}

<b>FUNDAMENTAL / NEWS BIAS:</b> {fundamental_bias['side']}
{fundamental_bias['reason']}

<b>Окремо:</b>
Новини: {news_side} ({news['sentiment']}, score {news['score']})
Події: {event_side} (ризик {event_risk['risk']})
Macro: {macro_side} ({macro['regime']})
Volatility: {market['volatility']['regime']}
Liquidation: {market['liquidation']['bias']}
Session: {session['session']} ({session['breakout_quality']})
Reversal: {reversal['side']} ({reversal['confidence']}%)

<b>Короткий висновок:</b>
{final_short_summary(signal, signal_type, tech, news, orderflow, macro, event_risk, market, oi_analysis, reversal, session)}
"""

    send_telegram(message.strip())
    print("TELEGRAM SENT")
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
