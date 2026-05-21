import os
import re
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

# Безкоштовний macro quant блок через TradingView scanner.
# Він допомагає зрозуміти загальний режим ринку: risk-on чи risk-off.
MACRO_SYMBOLS = [
    ("DXY", "TVC:DXY", "cfd"),          # індекс долара
    ("US10Y", "TVC:US10Y", "cfd"),      # дохідність 10-річних облігацій США
    ("VIX", "CBOE:VIX", "cfd"),         # індекс страху
    ("SPX", "SP:SPX", "cfd"),           # S&P 500
    ("NDX", "NASDAQ:NDX", "cfd"),       # Nasdaq 100
    ("BTC", "BINANCE:BTCUSDT", "crypto"), # risk appetite proxy
    ("UKOIL", "TVC:UKOIL", "cfd"),      # Brent / oil proxy
]

NEWS_LOOKBACK_HOURS = 2
MAX_NEWS_SCORE = 45
MAX_ITEMS_PER_FEED = 15
MIN_CONFIDENCE_TO_SEND = 55

STRONG_UP_MOVE_PERCENT = 1.2
VERY_STRONG_UP_MOVE_PERCENT = 1.8
STRONG_DOWN_MOVE_PERCENT = -1.2
VERY_STRONG_DOWN_MOVE_PERCENT = -1.8

# Безкоштовні real-time пошукові запити по нафті / BZ / Brent
GDELT_QUERIES = [
    '(oil OR crude OR Brent) (Trump OR tariff OR sanctions OR Iran OR Russia OR Ukraine OR war OR Hormuz OR OPEC OR EIA OR inventory OR Fed OR Powell)',
    '(Brent crude OR crude oil OR oil prices) (surge OR rally OR drop OR fall OR supply OR demand OR stockpiles OR sanctions)',
    '(OPEC OR OPEC+) (cut OR increase OR output OR production OR supply)',
    '(EIA OR API) (inventory OR stockpiles OR crude draw OR crude build)',
]

GOOGLE_NEWS_QUERIES = [
    'Brent crude oil Trump tariff sanctions OPEC EIA',
    'oil prices Brent crude breaking news today',
    'crude oil inventory EIA API OPEC',
    'Iran Russia Ukraine Hormuz oil sanctions',
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
        "weight": 0.6,
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "type": "rss",
        "weight": 0.4,
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "type": "rss",
        "weight": 0.4,
    },
]

BULLISH_WORDS = [
    "inventory draw", "crude draw", "stockpiles fell", "stockpiles decline",
    "supply disruption", "supply risk", "supply tight", "opec cut", "opec+ cut",
    "sanctions", "hormuz", "middle east tension", "russia supply", "ukraine attack",
    "refinery demand", "demand rises", "demand growth", "bullish", "rally",
    "surge", "higher", "jumps", "rebounds", "rate cut", "fed pause",
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


def now_utc():
    return datetime.now(timezone.utc)


def safe_get(url, timeout=15, retries=2):
    for attempt in range(retries):
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
# TRADINGVIEW PRICE / TECHNICAL ANALYSIS
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
# FREE MACRO QUANT MODEL
# ==========================================================

def get_macro_quant_data():
    columns = [
        "close",
        "change",
        "Recommend.All|15",
        "Recommend.All|60",
        "RSI|15",
        "EMA20|60",
        "EMA50|60",
        "MACD.macd|60",
    ]

    macro = {}

    for name, symbol, screener in MACRO_SYMBOLS:
        values = get_tradingview_scan(symbol, screener, columns)
        if not values:
            print(f"[WARN] Macro data unavailable for {name} / {symbol}")
            continue

        try:
            macro[name] = {
                "symbol": symbol,
                "price": float(values[0]) if values[0] is not None else None,
                "change": float(values[1]) if values[1] is not None else 0.0,
                "recommend_15m": values[2],
                "recommend_1h": values[3],
                "rsi_15m": values[4],
                "ema20_1h": values[5],
                "ema50_1h": values[6],
                "macd_1h": values[7],
            }
        except Exception as error:
            print(f"[WARN] Macro parse error {name}: {error}")

    return macro


def analyze_macro_quant(macro):
    score = 0
    regime = "НЕЙТРАЛЬНИЙ"
    confirmations = []
    warnings = []

    dxy = macro.get("DXY", {})
    us10y = macro.get("US10Y", {})
    vix = macro.get("VIX", {})
    spx = macro.get("SPX", {})
    ndx = macro.get("NDX", {})
    btc = macro.get("BTC", {})
    ukoil = macro.get("UKOIL", {})

    dxy_ch = dxy.get("change", 0) or 0
    us10y_ch = us10y.get("change", 0) or 0
    vix_ch = vix.get("change", 0) or 0
    spx_ch = spx.get("change", 0) or 0
    ndx_ch = ndx.get("change", 0) or 0
    btc_ch = btc.get("change", 0) or 0
    oil_ch = ukoil.get("change", 0) or 0

    # DXY: сильний долар часто тисне на ризикові активи й commodities.
    if dxy_ch <= -0.25:
        score += 12
        confirmations.append(f"DXY слабшає ({round(dxy_ch, 2)}%) — підтримка risk-on")
    elif dxy_ch >= 0.25:
        score -= 12
        warnings.append(f"DXY росте ({round(dxy_ch, 2)}%) — тиск на ризикові активи")

    # US10Y: ріст yields часто risk-off.
    if us10y_ch >= 1.0:
        score -= 10
        warnings.append(f"US10Y росте ({round(us10y_ch, 2)}%) — risk-off фактор")
    elif us10y_ch <= -1.0:
        score += 10
        confirmations.append(f"US10Y падає ({round(us10y_ch, 2)}%) — підтримка risk-on")

    # VIX: страх ринку.
    if vix_ch >= 4.0:
        score -= 18
        warnings.append(f"VIX сильно росте ({round(vix_ch, 2)}%) — ринок у стресі")
    elif vix_ch <= -3.0:
        score += 12
        confirmations.append(f"VIX падає ({round(vix_ch, 2)}%) — risk-on")

    # Акції / Nasdaq як risk appetite.
    if spx_ch > 0.25:
        score += 8
        confirmations.append(f"S&P 500 росте ({round(spx_ch, 2)}%)")
    elif spx_ch < -0.25:
        score -= 8
        warnings.append(f"S&P 500 падає ({round(spx_ch, 2)}%)")

    if ndx_ch > 0.35:
        score += 8
        confirmations.append(f"Nasdaq росте ({round(ndx_ch, 2)}%)")
    elif ndx_ch < -0.35:
        score -= 8
        warnings.append(f"Nasdaq падає ({round(ndx_ch, 2)}%)")

    # BTC як проксі risk appetite для crypto-style ф'ючерсів.
    if btc_ch > 0.6:
        score += 8
        confirmations.append(f"BTC росте ({round(btc_ch, 2)}%) — risk appetite")
    elif btc_ch < -0.6:
        score -= 8
        warnings.append(f"BTC падає ({round(btc_ch, 2)}%) — risk-off")

    # UKOIL/Brent — прямий macro proxy для BZ.
    if oil_ch > 0.5:
        score += 14
        confirmations.append(f"Brent/UKOIL росте ({round(oil_ch, 2)}%)")
    elif oil_ch < -0.5:
        score -= 14
        warnings.append(f"Brent/UKOIL падає ({round(oil_ch, 2)}%)")

    # Режим ринку
    if score >= 25:
        regime = "RISK-ON / БИЧАЧИЙ MACRO"
    elif score <= -25:
        regime = "RISK-OFF / ВЕДМЕЖИЙ MACRO"
    elif score >= 10:
        regime = "ПОМІРНО БИЧАЧИЙ MACRO"
    elif score <= -10:
        regime = "ПОМІРНО ВЕДМЕЖИЙ MACRO"

    return {
        "score": score,
        "regime": regime,
        "confirmations": confirmations[:8],
        "warnings": warnings[:8],
        "data": {
            "DXY": dxy_ch,
            "US10Y": us10y_ch,
            "VIX": vix_ch,
            "SPX": spx_ch,
            "NDX": ndx_ch,
            "BTC": btc_ch,
            "UKOIL": oil_ch,
        },
    }


# ==========================================================
# STABLE FREE ORDERFLOW FROM TRADINGVIEW ONLY
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
# MAX FREE NEWS: GDELT + GOOGLE NEWS RSS + RSS + CRYPTOPANIC
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


def get_gdelt_news():
    news = []
    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for query in GDELT_QUERIES:
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={quote_plus(query)}"
            "&mode=ArtList&format=json&maxrecords=50&sort=HybridRel&timespan=2h"
        )

        response = safe_get(url, timeout=10, retries=1)
        if not response:
            continue

        try:
            data = response.json()
            for article in data.get("articles", []):
                title = article.get("title", "")
                if not title:
                    continue

                published_at = parse_gdelt_date(article.get("seendate"))
                if published_at and published_at < cutoff:
                    continue

                news.append({
                    "title": title,
                    "link": article.get("url", ""),
                    "source": f"GDELT/{article.get('domain', 'news')}",
                    "published_at": published_at,
                    "weight": 1.2,
                })
        except Exception as error:
            print(f"[WARN] GDELT parse error: {error}")

    return news


def get_google_news_rss():
    news = []
    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for query in GOOGLE_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:2h')}&hl=en-US&gl=US&ceid=US:en"
        response = safe_get(url, timeout=10, retries=1)
        if not response:
            continue

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
                        "source": "Google News RSS",
                        "published_at": published_at,
                        "weight": 1.0,
                    })
        except Exception as error:
            print(f"[WARN] Google News RSS parse error: {error}")

    return news


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
            if not link:
                link_node = item.find("{http://www.w3.org/2005/Atom}link")
                if link_node is not None:
                    link = link_node.attrib.get("href", "")

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
    unique.sort(key=lambda x: x["published_at"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return unique


def get_all_fresh_news():
    all_news = []
    all_news.extend(get_gdelt_news())
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
    }


# ==========================================================
# SIGNAL ENGINE + ENTRY PLAN
# ==========================================================

def news_noise_warning(total_news, raw_score, capped_score):
    if total_news >= 60 and abs(raw_score) > abs(capped_score) * 4:
        return "Високий новинний шум: багато заголовків, score обмежено"
    if total_news < 3:
        return "Мало свіжих новин: новинне підтвердження слабке"
    return "Нормально"


def build_signal(tech, news, orderflow, macro, event_risk):
    score = tech["score"] + news["score"] + orderflow["score"] + macro["score"] + event_risk["score"]
    signal_type = "НЕМАЄ УГОДИ"
    signal = "NO SIGNAL"

    # News should confirm the trade, not dominate it.
    # If too many headlines appear in 2h, it may be noisy, so we cap/penalize it.
    if news["total"] >= 5 and news["score"] >= 35:
        score += 6
    if news["total"] >= 60:
        score -= 8
    if macro["score"] >= 25 and tech["momentum"] in ["STRONG UP", "VERY STRONG UP"]:
        score += 10
    if macro["score"] <= -25 and tech["momentum"] in ["STRONG DOWN", "VERY STRONG DOWN"]:
        score -= 10
    if tech["score"] < 0 and news["score"] > 45:
        score -= 10
    if tech["score"] > 0 and news["score"] < -45:
        score += 10
    if macro["score"] <= -25 and signal != "SHORT":
        score -= 12
    if macro["score"] >= 25 and signal != "LONG":
        score += 12

    if tech.get("momentum") == "VERY STRONG UP" and score >= 35:
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
    elif score >= 90 and (tech.get("trend") in ["UP", "MIXED"]) and orderflow["score"] >= 8:
        signal = "LONG"
        signal_type = "РИЗИКОВИЙ LONG / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"
    elif score <= -90 and (tech.get("trend") in ["DOWN", "MIXED"]) and orderflow["score"] <= -8:
        signal = "SHORT"
        signal_type = "РИЗИКОВИЙ SHORT / ЗМІШАНІ ПІДТВЕРДЖЕННЯ"

    confidence = min(95, max(0, abs(score)))

    risk_note = "Нормальний ризик"
    if "РИЗИКОВИЙ" in signal_type:
        risk_note = "Підтвердження змішані — зменшити розмір позиції"
    if macro["score"] <= -25 and signal == "LONG":
        risk_note = "Macro risk-off проти LONG — тільки малий обʼєм або пропуск"
    if macro["score"] >= 25 and signal == "SHORT":
        risk_note = "Macro risk-on проти SHORT — тільки малий обʼєм або пропуск"
    if tech.get("trend") == "DOWN" and signal == "LONG":
        risk_note = "Тільки скальп: старший тренд не підтвердив LONG"
    if tech.get("trend") == "UP" and signal == "SHORT":
        risk_note = "Тільки скальп: старший тренд не підтвердив SHORT"
    if event_risk["risk_level"] in ["ВИСОКИЙ", "ДУЖЕ ВИСОКИЙ"] and signal != "NO SIGNAL":
        risk_note = f"Подієвий ризик {event_risk['risk_level']} — краще чекати або зменшити позицію"

    return signal, signal_type, score, confidence, risk_note


def make_trade_plan(signal, signal_type, price, tech):
    atr = tech.get("atr_15m") or price * 0.006
    if signal == "NO SIGNAL":
        return {"entry": None, "stop": None, "tp1": None, "tp2": None, "tp3": None, "note": "Не входити. Чекати підтвердження."}

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



# ==========================================================
# UPCOMING EVENT RISK MODEL
# Sources used:
# - EIA schedule logic: Wednesday 10:30 ET
# - BLS CPI known release dates / schedule page fallback
# - FOMC known meeting dates
# - Google News RSS / GDELT for Powell, Fed, OPEC, Iran/US talks
# ==========================================================

FOMC_EVENTS_2026 = [
    ("FOMC rate decision", "2026-06-17 18:00", "UTC", "HIGH"),
    ("FOMC rate decision", "2026-07-29 18:00", "UTC", "HIGH"),
    ("FOMC rate decision", "2026-09-16 18:00", "UTC", "HIGH"),
    ("FOMC rate decision", "2026-10-28 18:00", "UTC", "HIGH"),
    ("FOMC rate decision", "2026-12-09 19:00", "UTC", "HIGH"),
]

CPI_EVENTS_2026 = [
    ("US CPI release", "2026-06-10 12:30", "UTC", "HIGH"),
    ("US CPI release", "2026-07-15 12:30", "UTC", "HIGH"),
    ("US CPI release", "2026-08-12 12:30", "UTC", "HIGH"),
    ("US CPI release", "2026-09-10 12:30", "UTC", "HIGH"),
    ("US CPI release", "2026-10-15 12:30", "UTC", "HIGH"),
    ("US CPI release", "2026-11-12 13:30", "UTC", "HIGH"),
    ("US CPI release", "2026-12-10 13:30", "UTC", "HIGH"),
]

OPEC_EVENTS_2026 = [
    ("OPEC Monthly Oil Market Report", "2026-06-11 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-07-13 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-08-12 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-09-11 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-10-13 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-11-12 12:00", "UTC", "HIGH"),
    ("OPEC Monthly Oil Market Report", "2026-12-11 12:00", "UTC", "HIGH"),
]

EVENT_NEWS_QUERIES = [
    "Powell speech today tomorrow Federal Reserve oil dollar",
    "Fed speaker today Powell speech FOMC market calendar",
    "EIA crude oil inventories today forecast",
    "OPEC meeting next oil output decision",
    "Iran US talks oil sanctions Hormuz today",
]


def parse_event_dt(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def next_wednesday_eia_event():
    now = now_utc()
    # EIA Weekly Petroleum Status Report is normally Wednesday 10:30 ET.
    # During US daylight saving time this is 14:30 UTC; otherwise 15:30 UTC.
    # For simplicity in May-October use 14:30 UTC.
    days_ahead = (2 - now.weekday()) % 7  # Monday=0, Wednesday=2
    event_day = (now + timedelta(days=days_ahead)).date()
    event_dt = datetime(event_day.year, event_day.month, event_day.day, 14, 30, tzinfo=timezone.utc)

    if event_dt <= now:
        event_dt = event_dt + timedelta(days=7)

    return {
        "name": "EIA crude oil inventories",
        "time": event_dt,
        "impact": "HIGH",
        "source": "EIA schedule rule",
        "note": "Щотижневий звіт EIA по запасах нафти, сильний ризик волатильності для Brent/BZ.",
    }


def next_nfp_event():
    now = now_utc()
    # NFP is usually first Friday of each month at 08:30 ET.
    year = now.year
    month = now.month

    for _ in range(14):
        first_day = datetime(year, month, 1, 12, 30, tzinfo=timezone.utc)
        days_until_friday = (4 - first_day.weekday()) % 7
        event_dt = first_day + timedelta(days=days_until_friday)

        if event_dt > now:
            return {
                "name": "US Nonfarm Payrolls / NFP",
                "time": event_dt,
                "impact": "HIGH",
                "source": "NFP schedule rule",
                "note": "NFP може різко рухати DXY, yields, risk assets і непрямо BZ.",
            }

        month += 1
        if month > 12:
            month = 1
            year += 1

    return None


def static_future_events():
    now = now_utc()
    events = []

    for name, time_text, tz, impact in FOMC_EVENTS_2026 + CPI_EVENTS_2026 + OPEC_EVENTS_2026:
        event_dt = parse_event_dt(time_text)
        if event_dt and event_dt > now:
            events.append({
                "name": name,
                "time": event_dt,
                "impact": impact,
                "source": "official/known schedule",
                "note": "Запланована макро/нафтова подія високого впливу.",
            })

    eia = next_wednesday_eia_event()
    if eia:
        events.append(eia)

    nfp = next_nfp_event()
    if nfp:
        events.append(nfp)

    events.sort(key=lambda x: x["time"])
    return events


def get_upcoming_event_news():
    news = []
    cutoff = now_utc() - timedelta(hours=24)

    for query in EVENT_NEWS_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote_plus(query + ' when:24h')}&hl=en-US&gl=US&ceid=US:en"
        response = safe_get(url, timeout=10, retries=1)
        if not response:
            continue

        try:
            root = ET.fromstring(response.content)
            for item in root.findall(".//item")[:8]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                link = item.findtext("link", "")
                published_at = parse_date(pub_date)

                if published_at and published_at < cutoff:
                    continue

                if title:
                    news.append({
                        "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                        "source": "Google Event RSS",
                        "link": link,
                        "published_at": published_at,
                    })
        except Exception as error:
            print(f"[WARN] Event Google RSS parse error: {error}")

    return deduplicate_news(news)


def analyze_event_risk():
    upcoming = static_future_events()
    event_news = get_upcoming_event_news()
    now = now_utc()

    score = 0
    risk_level = "НИЗЬКИЙ"
    warnings = []
    confirmations = []

    important_upcoming = []

    for event in upcoming[:10]:
        hours_to_event = (event["time"] - now).total_seconds() / 3600

        if hours_to_event <= 2:
            score -= 30
            warnings.append(f"Через {round(hours_to_event, 1)} год: {event['name']} — дуже високий ризик волатильності")
            important_upcoming.append(event)
        elif hours_to_event <= 8:
            score -= 18
            warnings.append(f"Сьогодні/скоро: {event['name']} — краще зменшити плече")
            important_upcoming.append(event)
        elif hours_to_event <= 24:
            score -= 10
            warnings.append(f"Протягом 24 год: {event['name']}")
            important_upcoming.append(event)
        elif hours_to_event <= 72:
            confirmations.append(f"Найближча подія: {event['name']} ({format_time(event['time'])})")
            important_upcoming.append(event)

    event_keywords = [
        "powell", "fed", "fomc", "cpi", "nfp", "payrolls", "eia",
        "inventory", "inventories", "opec", "iran", "us talks",
        "hormuz", "sanctions", "tariff", "trump",
    ]

    for item in event_news[:12]:
        title = item["title"].lower()
        hits = sum(1 for word in event_keywords if word in title)
        if hits >= 2:
            score -= 5
            warnings.append(f"Подієвий headline: {item['title']}")

    if score <= -35:
        risk_level = "ДУЖЕ ВИСОКИЙ"
    elif score <= -20:
        risk_level = "ВИСОКИЙ"
    elif score <= -10:
        risk_level = "ПІДВИЩЕНИЙ"

    return {
        "score": score,
        "risk_level": risk_level,
        "warnings": warnings[:8],
        "confirmations": confirmations[:5],
        "events": important_upcoming[:6],
        "event_news": event_news[:6],
    }


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
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as error:
        print(f"[WARN] Telegram error: {error}")


def main():
    print("START BZU PROFESSIONAL FREE BOT UA")
    tv = get_tradingview_market_data()
    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    fresh_news = get_all_fresh_news()
    tech = analyze_technical(tv)
    news = analyze_news(fresh_news)
    orderflow = analyze_free_orderflow(tv)
    macro_data = get_macro_quant_data()
    macro = analyze_macro_quant(macro_data)
    event_risk = analyze_event_risk()
    signal, signal_type, score, confidence, risk_note = build_signal(tech, news, orderflow, macro, event_risk)
    plan = make_trade_plan(signal, signal_type, tv["price"], tech)

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
    print(f"EVENT RISK: {event_risk['risk_level']} | SCORE: {event_risk['score']}")
    print(f"MOMENTUM: {tech['momentum']} | CHANGE: {tech['change']}%")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL TYPE: {signal_type}")
    print(f"SIGNAL: {signal}")

    if signal == "NO SIGNAL" or confidence < MIN_CONFIDENCE_TO_SEND:
        print("NO SIGNAL")
        return

    message = f"""
<b>📊 BZU SIGNAL BOT ULTRA</b>

<b>Сигнал:</b> {signal}
<b>Тип сигналу:</b> {signal_type}
<b>Впевненість:</b> {confidence}%
<b>Ризик:</b> {risk_note}

<b>Інструмент:</b> {tv['symbol']}
<b>Ціна:</b> {tv['price']}
<b>Зміна:</b> {tv['change']}%
<b>Імпульс:</b> {tech['momentum']}

<b>Вхід:</b> {plan['entry']}
<b>Стоп:</b> {plan['stop']}
<b>TP1:</b> {plan['tp1']}
<b>TP2:</b> {plan['tp2']}
<b>TP3:</b> {plan['tp3']}
<b>План:</b> {plan['note']}

<b>Тренд:</b> {tech['trend']}
<b>Тренд 5m:</b> {tech['trend_5m']}
<b>Тренд 15m:</b> {tech['trend_15m']}
<b>Тренд 1h:</b> {tech['trend_1h']}
<b>RSI 5m:</b> {tech['rsi_5m']}
<b>RSI 15m:</b> {tech['rsi_15m']}
<b>RSI 1h:</b> {tech['rsi_1h']}
<b>MACD 15m:</b> {tech['macd_15m']}
<b>ADX 15m:</b> {tech['adx_15m']}
<b>ATR 15m:</b> {tech['atr_15m']}

<b>Orderflow:</b> {orderflow['bias']}
<b>Orderflow score:</b> {orderflow['score']}

<b>Macro regime:</b> {macro['regime']}
<b>Macro score:</b> {macro['score']}
<b>DXY:</b> {macro['data']['DXY']}%
<b>US10Y:</b> {macro['data']['US10Y']}%
<b>VIX:</b> {macro['data']['VIX']}%
<b>S&P 500:</b> {macro['data']['SPX']}%
<b>Nasdaq:</b> {macro['data']['NDX']}%
<b>BTC:</b> {macro['data']['BTC']}%
<b>UKOIL/Brent:</b> {macro['data']['UKOIL']}%

<b>Event risk:</b> {event_risk['risk_level']}
<b>Event score:</b> {event_risk['score']}
"""

    if orderflow["details"]:
        message += "\n<b>Деталі orderflow:</b>"
        for item in orderflow["details"]:
            message += f"\n- {item}"

    if macro["confirmations"]:
        message += "\n\n<b>Macro підтвердження:</b>"
        for item in macro["confirmations"]:
            message += f"\n- {item}"

    if event_risk["confirmations"]:
        message += "\n\n<b>Майбутні події:</b>"
        for item in event_risk["confirmations"]:
            message += f"\n- {item}"

    if event_risk["events"]:
        message += f"\n\n<b>Event risk:</b> {event_risk['risk_level']} / score {event_risk['score']}"
        for event in event_risk["events"]:
            message += (
                f"\n- {event['name']} — {format_time(event['time'])} "
                f"({event['impact']})"
            )

    warnings = tech["warnings"] + orderflow["warnings"] + macro["warnings"] + event_risk["warnings"]
    if warnings:
        message += "\n\n<b>Попередження:</b>"
        for item in warnings:
            message += f"\n- {item}"

    message += f"""

<b>Новини:</b> останні {NEWS_LOOKBACK_HOURS} год
<b>Кількість свіжих новин:</b> {news['total']}
<b>Настрій новин:</b> {news['sentiment']}
<b>Bullish:</b> {news['bullish']}
<b>Bearish:</b> {news['bearish']}
<b>Impact:</b> {news['impact']}
<b>Breaking:</b> {news['breaking']}
<b>News score:</b> {news['score']} / raw {news['raw_score']}
<b>Якість news-score:</b> {news['noise_warning']}

<b>Важливі свіжі новини:</b>
"""

    if news["important"]:
        for item in news["important"]:
            message += (
                f"\n- [{item['source']}] "
                f"{item['title']} "
                f"({format_time(item['published_at'])})"
            )
    else:
        message += "\nНемає"

    if event_risk["event_news"]:
        message += "\n\n<b>Headlines по майбутніх подіях:</b>"
        for item in event_risk["event_news"]:
            message += (
                f"\n- [{item['source']}] "
                f"{item['title']} "
                f"({format_time(item['published_at'])})"
            )

    send_telegram(message.strip())
    print("TELEGRAM SENT")
    print("BOT COMPLETE")

if __name__ == "__main__":
    main()
