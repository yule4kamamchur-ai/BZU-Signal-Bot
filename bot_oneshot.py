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
    - absorption: large volume but weak close/progress
    """
    if not candles or len(candles) < 25:
        return {
            "available": False,
            "score": 0,
            "bias": "NEUTRAL",
            "spike": False,
            "absorption": "NONE",
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
            "absorption": "NONE",
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
    absorption = "NONE"
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
        absorption = "BEARISH ABSORPTION"
        score -= 14
        bias = "SHORT"
        notes.append("absorption зверху: покупців поглинули")
    elif spike and last["low"] < min(c["low"] for c in recent[-10:]) and close_location >= 0.55:
        absorption = "BULLISH ABSORPTION"
        score += 14
        bias = "LONG"
        notes.append("absorption знизу: продавців поглинули")

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
        "absorption": absorption,
        "note": "; ".join(notes[:3]) if notes else "volume neutral",
    }

def analyze_smc_structure(candles):
    """Real price action + SMC structure:
    - BOS / CHoCH
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

    # BOS: close beyond recent structure.
    if close > last_swing_high:
        bos = "BOS LONG"
        score += 24
        notes.append("BOS LONG: закриття вище swing high")
    elif close < last_swing_low:
        bos = "BOS SHORT"
        score -= 24
        notes.append("BOS SHORT: закриття нижче swing low")

    # Liquidity sweep: wick takes high/low but close returns back.
    if last["high"] > recent_high and close < recent_high:
        sweep = "UPSIDE SWEEP / SHORT RISK"
        score -= 18
        notes.append("зняли ліквідність зверху — ризик SHORT-відкату")
    elif last["low"] < recent_low and close > recent_low:
        sweep = "DOWNSIDE SWEEP / LONG RISK"
        score += 18
        notes.append("зняли ліквідність знизу — ризик LONG-відскоку")

    # CHoCH approximation: previous candle broke one way, current close reverses through midpoint/structure.
    mid = (recent_high + recent_low) / 2
    if prev["low"] <= recent_low + atr * 0.15 and close > mid:
        choch = "CHoCH LONG"
        score += 16
        notes.append("CHoCH LONG: після sweep ціна повернулась у діапазон")
    elif prev["high"] >= recent_high - atr * 0.15 and close < mid:
        choch = "CHoCH SHORT"
        score -= 16
        notes.append("CHoCH SHORT: після sweep ціна повернулась у діапазон")

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

    # Fake BOS protection: BOS without volume confirmation is weaker.
    if bos == "BOS LONG" and volume_confirmation.get("available"):
        if not volume_confirmation.get("spike") and volume_confirmation.get("bias") != "LONG":
            score -= 8
            notes.append("BOS LONG без сильного обсягу — ризик fake breakout")
        elif volume_confirmation.get("bias") == "LONG":
            score += 6
            notes.append("BOS LONG підтверджений обсягом")

    if bos == "BOS SHORT" and volume_confirmation.get("available"):
        if not volume_confirmation.get("spike") and volume_confirmation.get("bias") != "SHORT":
            score += 8
            notes.append("BOS SHORT без сильного обсягу — ризик fake breakdown")
        elif volume_confirmation.get("bias") == "SHORT":
            score -= 6
            notes.append("BOS SHORT підтверджений обсягом")

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
        phase = "BREAKOUT / BOS"
    elif choch != "NONE":
        phase = "REVERSAL / CHoCH"
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

    if signal == "LONG" and volume.get("absorption") == "BEARISH ABSORPTION":
        adjust -= 10
    if signal == "SHORT" and volume.get("absorption") == "BULLISH ABSORPTION":
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
    absorption = volume.get("absorption", "NONE")
    vol_bias = volume.get("bias", "NEUTRAL")

    if absorption == "BEARISH ABSORPTION":
        return "Структура: absorption зверху — ризик відкату"
    if absorption == "BULLISH ABSORPTION":
        return "Структура: absorption знизу — ризик відскоку"

    vol_note = ""
    if volume.get("spike") and vol_bias in ["LONG", "SHORT"]:
        vol_note = " + volume"

    if phase == "BREAKOUT / BOS":
        return f"Структура: {bias} BOS{vol_note}"
    if phase == "REVERSAL / CHoCH":
        return f"Структура: можливий розворот {bias}{vol_note}"
    if phase == "LIQUIDITY SWEEP":
        return "Структура: liquidity sweep — чекати підтвердження"
    return "Структура: діапазон — краще чекати"



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
        smc_bias == "SHORT" or bos == "BOS SHORT" or
        (trend_5m == "DOWN" and trend_15m == "DOWN") or
        order_score <= -20 or vol_bias == "SHORT"
    )
    bullish_structure = (
        smc_bias == "LONG" or bos == "BOS LONG" or
        (trend_5m == "UP" and trend_15m == "UP") or
        order_score >= 20 or vol_bias == "LONG"
    )

    bullish_reclaim = choch == "CHoCH LONG" or bos == "BOS LONG" or vol_bias == "LONG" or (smc_phase == "REVERSAL / CHoCH" and smc_bias == "LONG")
    bearish_reclaim = choch == "CHoCH SHORT" or bos == "BOS SHORT" or vol_bias == "SHORT" or (smc_phase == "REVERSAL / CHoCH" and smc_bias == "SHORT")

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

    has_long_reclaim = bos == "BOS LONG" or choch == "CHoCH LONG" or vol_bias == "LONG"
    has_short_reclaim = bos == "BOS SHORT" or choch == "CHoCH SHORT" or vol_bias == "SHORT"

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
    absorption = volume.get("absorption", "NONE")

    news_score = news.get("score", 0) if isinstance(news, dict) else 0
    event_side = event_risk.get("direction", "MIXED") if isinstance(event_risk, dict) else "MIXED"

    strong_dump = change <= -1.0 or momentum in ["STRONG DOWN", "VERY STRONG DOWN"]
    strong_pump = change >= 1.0 or momentum in ["STRONG UP", "VERY STRONG UP"]

    short_confirmed = (
        smc_bias == "SHORT"
        or bos == "BOS SHORT"
        or vol_bias == "SHORT"
        or absorption == "BEARISH ABSORPTION"
    )
    long_confirmed = (
        smc_bias == "LONG"
        or bos == "BOS LONG"
        or vol_bias == "LONG"
        or absorption == "BULLISH ABSORPTION"
    )

    # SHORT after a dump is dangerous if structure/volume did not confirm continuation.
    if signal == "SHORT" and strong_dump:
        if not short_confirmed:
            cap = 54
            reason = "SHORT після сильного дампу без SMC/BOS/volume підтвердження — ризик відскоку"
            if news_score >= 35 or event_side == "LONG" or long_confirmed or choch == "CHoCH LONG":
                cap = 49
                reason = "SHORT запізнений: дамп уже був, bullish-новини/відскок проти входу"
            return {"active": True, "cap": cap, "reason": reason}

    # LONG after a pump is dangerous if structure/volume did not confirm continuation.
    if signal == "LONG" and strong_pump:
        if not long_confirmed:
            cap = 54
            reason = "LONG після сильного пампу без SMC/BOS/volume підтвердження — ризик відкату"
            if news_score <= -35 or event_side == "SHORT" or short_confirmed or choch == "CHoCH SHORT":
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
    absorption = volume.get("absorption", "NONE")

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
        if sweep.startswith("DOWNSIDE") or choch == "CHoCH LONG":
            score += 18
            reasons.append("sweep/CHoCH LONG")
        if absorption == "BULLISH ABSORPTION" or vol_bias == "LONG":
            score += 16
            reasons.append("обсяг/absorption за покупців")
        if bos == "BOS LONG" or smc_bias == "LONG":
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
        if bos == "BOS SHORT" or smc_bias == "SHORT" or vol_bias == "SHORT":
            score -= 14
            reasons.append("частина структури ще bearish")

    elif strong_pump:
        side = "SHORT"
        score += 12
        reasons.append("після сильного пампу шукаємо відкат")
        if news_score <= -35 or event_side == "SHORT":
            score += 14
            reasons.append("bearish news/event підтримує відкат")
        if sweep.startswith("UPSIDE") or choch == "CHoCH SHORT":
            score += 18
            reasons.append("sweep/CHoCH SHORT")
        if absorption == "BEARISH ABSORPTION" or vol_bias == "SHORT":
            score += 16
            reasons.append("обсяг/absorption за продавців")
        if bos == "BOS SHORT" or smc_bias == "SHORT":
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
        if bos == "BOS LONG" or smc_bias == "LONG" or vol_bias == "LONG":
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
            f"ENTRY WATCH LONG — вхід не по ринку. "
            f"Тригер: закріплення вище {trigger} або ретест/утримання {pullback_zone}. "
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
            f"ENTRY WATCH SHORT — вхід не по ринку. "
            f"Тригер: закріплення нижче {trigger} або ретест/утримання {pullback_zone}. "
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
    """Telegram plan text. TRADE only if confirmed; otherwise conditional entry watch."""
    if show_trade_plan:
        return format_trade_plan(plan)

    if entry_watch and entry_watch.get("active"):
        return entry_watch.get("text")

    return "WAIT — немає якісного входу; чекати новий тригер"



def apply_entry_watch_quality_floor(signal, trade_probability, tech, news, event_risk, smc, entry_watch):
    """ENTRY WATCH should not be shown as 0/5 when the setup has a real directional reason.

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
        tech_strong_against = tech_score <= -55 or smc_bias == "SHORT" or bos == "BOS SHORT" or vol_bias == "SHORT"
        news_support = news_score >= 45 or event_side == "LONG"
        mild_tech_support = tech_score >= 15 or smc_bias == "LONG" or bos == "BOS LONG" or vol_bias == "LONG"

        if news_support and not tech_strong_against:
            trade_probability = max(trade_probability, 52)  # 2/5 watch
        if news_support and mild_tech_support and not tech_strong_against:
            trade_probability = max(trade_probability, 58)  # 3/5 watch

    elif signal == "SHORT":
        tech_strong_against = tech_score >= 55 or smc_bias == "LONG" or bos == "BOS LONG" or vol_bias == "LONG"
        news_support = news_score <= -45 or event_side == "SHORT"
        mild_tech_support = tech_score <= -15 or smc_bias == "SHORT" or bos == "BOS SHORT" or vol_bias == "SHORT"

        if news_support and not tech_strong_against:
            trade_probability = max(trade_probability, 52)
        if news_support and mild_tech_support and not tech_strong_against:
            trade_probability = max(trade_probability, 58)

    # ENTRY WATCH is still not a confirmed trade.
    return min(trade_probability, 64)



def apply_confirmed_trade_quality_floor(signal, trade_probability, tech, news, event_risk, smc, orderflow=None):
    """Fair quality floor for real TRADE setups.

    Difference from ENTRY WATCH:
    - WATCH can be 2/5–3/5 without full structure confirmation.
    - TRADE needs confirmation: BOS/SMC/volume/orderflow/EMA trend.
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
    absorption = volume.get("absorption", "NONE")
    order_score = orderflow.get("score", 0) if isinstance(orderflow, dict) else 0

    if signal == "LONG":
        strong_against = (
            tech_score <= -55
            or bos == "BOS SHORT"
            or smc_bias == "SHORT"
            or vol_bias == "SHORT"
            or absorption == "BEARISH ABSORPTION"
        )
        news_support = news_score >= 45 or event_side == "LONG"
        tech_support = tech_score >= 35 or (trend_5m == "UP" and trend_15m == "UP") or order_score >= 15
        structure_confirmed = (
            bos == "BOS LONG"
            or smc_bias == "LONG"
            or vol_bias == "LONG"
            or absorption == "BULLISH ABSORPTION"
            or phase == "BREAKOUT / BOS"
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
            or bos == "BOS LONG"
            or smc_bias == "LONG"
            or vol_bias == "LONG"
            or absorption == "BULLISH ABSORPTION"
        )
        news_support = news_score <= -45 or event_side == "SHORT"
        tech_support = tech_score <= -35 or (trend_5m == "DOWN" and trend_15m == "DOWN") or order_score <= -15
        structure_confirmed = (
            bos == "BOS SHORT"
            or smc_bias == "SHORT"
            or vol_bias == "SHORT"
            or absorption == "BEARISH ABSORPTION"
            or phase == "BREAKOUT / BOS"
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
    Confirmed BOS/trend/news alignment = wider targets.
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
    if "BOS" in st or "BREAKOUT" in st or abs(tech.get("score", 0) or 0) >= 85:
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
        return "ГОТУЄМОСЬ ДО SHORT — SMC/3m проти LONG"
    if "STRUCTURE LONG WATCH" in st:
        return "ГОТУЄМОСЬ ДО LONG — SMC/3m проти SHORT"
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

    if signal == "LONG":
        if "EARLY NEWS" in signal_type:
            return "TRADE LONG — робочий вхід"
        if "ІМПУЛЬСНИЙ" in signal_type:
            return "TRADE LONG — імпульсний"
        if "РИЗИКОВИЙ" in signal_type:
            return "TRADE LONG — обережно"
        return "TRADE LONG"

    if signal == "SHORT":
        if "EARLY NEWS" in signal_type:
            return "TRADE SHORT — робочий вхід"
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


def entry_quality_scale(probability, late_entry=None):
    """User-friendly entry quality scale for Telegram.
    This separates market direction from actual entry quality.
    """
    if probability is None:
        return "0/5 — немає входу"
    try:
        probability = int(probability)
    except Exception:
        return "0/5 — немає входу"

    suffix = ""
    if late_entry and late_entry.get("late"):
        suffix = " — пізній вхід, краще чекати відкат"

    if probability < 50:
        return f"0/5 — немає входу ({probability}%){suffix}"
    if probability < 55:
        return f"2/5 — спостерігаємо, входу ще немає ({probability}%){suffix}"
    if probability < 65:
        return f"3/5 — готуємось до входу ({probability}%){suffix}"
    if probability < 75:
        return f"4/5 — робочий вхід, тільки зі стопом ({probability}%){suffix}"
    return f"5/5 — найкращий підтверджений вхід ({probability}%){suffix}"


def smc_conflict_note(smc):
    """Explain mixed SMC signals instead of showing contradictory BOS/volume silently."""
    if not smc or not isinstance(smc, dict) or not smc.get("available"):
        return ""

    bias = smc.get("bias", "NEUTRAL")
    bos = smc.get("bos", "NONE")
    choch = smc.get("choch", "NONE")
    summary = str(smc.get("summary", "")).lower()
    volume = smc.get("volume", {}) if isinstance(smc.get("volume", {}), dict) else {}
    vol_bias = volume.get("bias", "NEUTRAL")
    absorption = volume.get("absorption", "NONE")

    long_structure = bias == "LONG" or bos == "BOS LONG" or choch == "CHoCH LONG"
    short_structure = bias == "SHORT" or bos == "BOS SHORT" or choch == "CHoCH SHORT"

    bearish_candle = "bearish candle" in summary or "продавців" in summary or absorption == "BEARISH ABSORPTION"
    bullish_candle = "bullish candle" in summary or "покупців" in summary or absorption == "BULLISH ABSORPTION"

    if long_structure and (vol_bias == "SHORT" or bearish_candle):
        return "SMC: змішано — структура LONG, але обсяг/свічка проти. LONG ще не підтверджений."
    if short_structure and (vol_bias == "LONG" or bullish_candle):
        return "SMC: змішано — структура SHORT, але обсяг/свічка проти. SHORT ще не підтверджений."
    if bias in ["LONG", "SHORT"]:
        return f"SMC: {bias} підтвердження структури."
    return "SMC: нейтрально / чекати підтвердження."


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
        return "Сетап слабкий — чекати ретест/підтвердження."
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
    # prevents strong 5/5 entries after an extended move without SMC/BOS/volume confirmation.
    exhaustion = extension_exhaustion_filter(signal, tech or {}, smc, news, event_risk)
    if exhaustion.get("active") and exhaustion.get("cap") is not None:
        prob = min(prob, int(exhaustion["cap"]))

    # Early reversal is only a WATCH until SMC/BOS/volume fully confirms it.
    early = early_reversal_engine({}, tech or {}, smc, news, event_risk)
    if early.get("active") and early.get("side") == signal and early.get("quality_cap") is not None:
        prob = min(prob, int(early["quality_cap"]))

    return int(max(30, min(82, round(prob))))

def compact_telegram_message(tv, signal, signal_type, confidence, quality, plan, technical_bias, fundamental_bias, news, event_risk, macro, orderflow, oi_analysis, market, session, reversal, priority, final_summary, weekend=None, cross_market=None, rr=None, chase=None, pos_note='', late_entry=None, cooling=None, smc=None, tech=None):
    decision = human_decision_line(signal, signal_type, reversal, technical_bias, news, event_risk)
    if late_entry and late_entry.get("late"):
        decision = late_entry.get("label") or decision
    tech_label = short_bias_label(technical_bias.get("side", "NEUTRAL"))
    fund_label = short_bias_label(fundamental_bias.get("side", "NEUTRAL"))
    priority_label = compact_priority_label(priority, reversal)
    driver = select_main_driver(technical_bias, news, event_risk, macro, orderflow, market, session, priority)
    trade_probability = estimate_trade_probability(signal, confidence, quality, technical_bias, fundamental_bias, news, event_risk, orderflow, market, reversal, chase, weekend, late_entry, smc, tech)
    exhaustion = extension_exhaustion_filter(signal, tech or {}, smc, news, event_risk)
    early_reversal = early_reversal_engine(tv, tech or {}, smc, news, event_risk)
    show_trade_plan = should_show_trade_plan(signal, trade_probability, late_entry)

    if show_trade_plan and signal in ["LONG", "SHORT"] and not str(decision).startswith("TRADE"):
        decision = f"TRADE {signal} — підтверджений вхід"
    entry_watch = proactive_entry_watch(signal, tv, tech or {}, smc, news, event_risk, early_reversal, trade_probability)
    trade_probability = apply_entry_watch_quality_floor(signal, trade_probability, tech or {}, news, event_risk, smc, entry_watch)
    trade_probability = apply_confirmed_trade_quality_floor(signal, trade_probability, tech or {}, news, event_risk, smc, orderflow)
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
            decision = "ENTRY WATCH LONG — тільки після ретесту/утримання"
        elif signal == "SHORT":
            decision = "ENTRY WATCH SHORT — тільки після ретесту/утримання"
    if cooling and cooling.get("active"):
        if signal == "LONG":
            decision = "ENTRY WATCH LONG — тільки після ретесту/утримання"
        elif signal == "SHORT":
            decision = "ENTRY WATCH SHORT — тільки після ретесту/утримання"

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
    driver_text = f"{driver.get('type', '')} {driver.get('expectation', '')} {driver.get('summary', '')}".upper()

    if signal in ["LONG", "SHORT"]:
        market_bias = signal
    elif "LONG" in decision_upper:
        market_bias = "LONG"
    elif "SHORT" in decision_upper:
        market_bias = "SHORT"
    elif "LONG" in driver_text and "SHORT" not in driver_text:
        market_bias = "LONG"
    elif "SHORT" in driver_text and "LONG" not in driver_text:
        market_bias = "SHORT"
    elif fundamental_bias.get("side") in ["LONG", "SHORT"] and abs(fundamental_bias.get("score", 0)) >= abs(technical_bias.get("score", 0)):
        market_bias = fundamental_bias.get("side")
    elif technical_bias.get("side") in ["LONG", "SHORT"]:
        market_bias = technical_bias.get("side")
    else:
        market_bias = "НЕЙТРАЛЬНО"

    if market_bias in ["LONG", "SHORT"]:
        market_bias_text = f"{market_bias} ({confidence}%)"
    else:
        market_bias_text = "НЕЙТРАЛЬНО"

    lines = [
        "<b>📊 BZU SIGNAL BOT</b>",
        "",
        f"<b>Рішення:</b> {decision}",
        f"<b>Напрямок ринку:</b> {market_bias_text}",
        # Якість входу = наскільки хороший поточний сетап для входу
        f"<b>Якість входу:</b> {entry_quality_scale(trade_probability, late_entry)}",
        "",
        f"<b>Ціна:</b> {tv['price']} | <b>Зміна:</b> {round(tv['change'], 4)}%",
        "",
        "<b>Драйвер:</b>",
        f"{driver['type']}",
        f"{driver['summary']}",
        f"<b>Час:</b> {driver['time']}",
        f"<b>Очікування:</b> {driver['expectation']}",
        f"<b>Джерело:</b> {format_driver_source(driver)}",
        "",
        f"<b>План:</b> {proactive_plan_text(signal, trade_probability, show_trade_plan, plan, entry_watch)}",
        "",
        f"<b>TECH:</b> {tech_label} ({technical_bias.get('score')})",
        f"<b>NEWS:</b> {fund_label} ({fundamental_bias.get('score')})",    ]

    smc_note = smc_conflict_note(smc)
    if smc_note:
        lines.append(f"<b>{smc_note}</b>")

    micro_line = micro_structure_text((tech or {}).get("micro_3m"))
    if micro_line:
        lines.append(micro_line)

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

    if cooling and cooling.get("active"):
        lines.append(f"<b>Вхід:</b> {cooling.get('note')}")
    elif late_entry and late_entry.get("late"):
        lines.append(f"<b>Вхід:</b> {late_entry.get('note')}")
    if pos_note:
        lines.append(f"<b>Позиція:</b> {pos_note}")

    lines.extend([
        "",
        f"<b>Висновок:</b> {compact_final_summary_text(final_summary, market_bias, trade_probability)}",
    ])

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
        f"Вхід: {plan.get('entry')} | "
        f"Стоп: {plan.get('stop')} | "
        f"TP1: {plan.get('tp1')} | "
        f"TP2: {plan.get('tp2')} | "
        f"TP3: {plan.get('tp3')}"
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
# MICRO 3M STRUCTURE
# ==========================================================

def analyze_micro_structure(candles):
    """3m microstructure filter."""
    if not candles or len(candles) < 20:
        return {"available": False, "bias": "NEUTRAL", "score": 0}

    recent = candles[-15:]
    last = candles[-1]

    highs = [c["high"] for c in recent[:-1]]
    lows = [c["low"] for c in recent[:-1]]

    recent_high = max(highs)
    recent_low = min(lows)

    score = 0

    if last["close"] > recent_high:
        score += 22
    elif last["close"] < recent_low:
        score -= 22

    if last["close"] > last["open"]:
        score += 8
    else:
        score -= 8

    if score >= 18:
        bias = "LONG"
    elif score <= -18:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "note": f"3m {bias}"
    }



def micro_structure_text(micro):
    if not micro or not micro.get("available"):
        return ""
    if micro.get("bias") == "LONG":
        return f"<b>MICRO 3m:</b> LONG тригер ({micro.get('score')})"
    if micro.get("bias") == "SHORT":
        return f"<b>MICRO 3m:</b> SHORT тригер ({micro.get('score')})"
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
        or bos == "BOS SHORT"
        or choch == "CHoCH SHORT"
        or smc_score <= -22
    )
    structure_long = (
        smc_bias == "LONG"
        or bos == "BOS LONG"
        or choch == "CHoCH LONG"
        or smc_score >= 22
    )

    micro_short = micro_bias == "SHORT" and micro_score <= -18
    micro_long = micro_bias == "LONG" and micro_score >= 18

    if signal == "LONG" and structure_short:
        if micro_short or tech_score <= -35:
            return {
                "active": True,
                "signal": "SHORT",
                "signal_type": "STRUCTURE SHORT WATCH / NEWS INVALIDATED",
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
                "signal_type": "STRUCTURE LONG WATCH / NEWS INVALIDATED",
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
    
    real_candles = get_real_candles()
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
    macro_data = get_macro_quant_data()
    macro = analyze_macro_quant(macro_data)
    event_risk = analyze_event_risk(event_items)
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

    current_truth_filter = price_action_truth_filter(signal, tech, smc, news, event_risk, orderflow)
    if current_truth_filter.get('blocked'):
        signal = 'НЕЙТРАЛЬНО'
        signal_type = 'НЕ ВХОДИТИ — ціна не підтвердила новини'
        risk_note = current_truth_filter.get('reason')
        confidence = min(confidence, 50)
    elif current_truth_filter.get('bonus'):
        score += current_truth_filter.get('bonus', 0)
        confidence = min(95, max(0, abs(score)))
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
    print(f"SMC VOLUME: {smc.get('volume', {}).get('bias', 'NEUTRAL')} | {smc.get('volume', {}).get('absorption', 'NONE')} | {smc.get('volume', {}).get('note', '')}")
    print(f"ORDERFLOW SCORE: {orderflow['score']} | {orderflow['bias']}")
    print(f"MACRO SCORE: {macro['score']} | {macro['regime']}")
    print(f"EVENT RISK: {event_risk['risk']} | SCORE: {event_risk['score']} | DIRECTION: {event_risk['direction']}")
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

    send_telegram(message.strip())
    print("TELEGRAM SENT")
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
