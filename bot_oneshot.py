import os
import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")

# Головний символ, який уже працював у вас через TradingView
TRADINGVIEW_SYMBOLS = [
    ("BINANCE:BZUSDT.P", "crypto"),
    ("BINANCE:BZUSDT", "crypto"),
    ("TVC:UKOIL", "cfd"),
    ("NYMEX:BZ1!", "futures"),
]

# Безкоштовні proxy-символи для orderflow / funding / OI.
# Якщо BZUSDT заблокований або недоступний у Binance API, бот автоматично пробує BTC/ETH як proxy ризику ринку.
BINANCE_PROXY_SYMBOLS = ["BZUSDT", "BTCUSDT", "ETHUSDT"]
BYBIT_PROXY_SYMBOLS = ["BTCUSDT", "ETHUSDT"]

BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"

NEWS_LOOKBACK_HOURS = 2
MAX_NEWS_SCORE = 80
MAX_ITEMS_PER_FEED = 15

STRONG_UP_MOVE_PERCENT = 1.2
VERY_STRONG_UP_MOVE_PERCENT = 1.8
STRONG_DOWN_MOVE_PERCENT = -1.2
VERY_STRONG_DOWN_MOVE_PERCENT = -1.8

MIN_CONFIDENCE_TO_SEND = 55

NEWS_SOURCES = [
    {
        "name": "EIA Today in Energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "type": "rss",
        "weight": 1.2,
    },
    {
        "name": "OilPrice Main",
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
        "weight": 0.7,
    },
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "type": "rss",
        "weight": 0.7,
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "type": "rss",
        "weight": 0.7,
    },
    {
        "name": "CryptoSlate",
        "url": "https://cryptoslate.com/feed/",
        "type": "rss",
        "weight": 0.6,
    },
]

BULLISH_WORDS = [
    "inventory draw",
    "crude draw",
    "stockpiles fell",
    "stockpiles decline",
    "supply disruption",
    "supply risk",
    "supply tight",
    "opec cut",
    "opec+ cut",
    "sanctions",
    "hormuz",
    "middle east tension",
    "russia supply",
    "ukraine attack",
    "refinery demand",
    "demand rises",
    "demand growth",
    "bullish",
    "rally",
    "surge",
    "higher",
    "jumps",
    "rebounds",
    "rate cut",
    "fed pause",
]

BEARISH_WORDS = [
    "inventory build",
    "crude build",
    "stockpiles rose",
    "stockpiles rise",
    "demand weak",
    "weak demand",
    "demand falls",
    "oversupply",
    "supply glut",
    "opec increase",
    "output hike",
    "ceasefire",
    "peace talks",
    "recession",
    "rate hike",
    "inflation rises",
    "bearish",
    "falls",
    "drops",
    "tumbles",
    "slides",
    "lower",
]

BREAKING_WORDS = [
    "trump",
    "white house",
    "president",
    "tariff",
    "sanctions",
    "iran",
    "russia",
    "ukraine",
    "war",
    "ceasefire",
    "hormuz",
    "opec",
    "opec+",
    "fed",
    "powell",
    "fomc",
    "cpi",
    "eia",
    "api",
    "inventory",
    "stockpiles",
    "breaking",
    "urgent",
]

HIGH_IMPACT_WORDS = [
    "eia",
    "api",
    "inventory",
    "stockpiles",
    "opec",
    "opec+",
    "hormuz",
    "iran",
    "russia",
    "ukraine",
    "sanctions",
    "fed",
    "fomc",
    "cpi",
    "powell",
    "inflation",
    "interest rate",
]


def now_utc():
    return datetime.now(timezone.utc)


def safe_get(url, timeout=15):
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
            print(response.text[:220])
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
            print(response.text[:220])
            return None

        return response

    except Exception as error:
        print(f"[WARN] {url}: {error}")
        return None


# ==========================================================
# TRADINGVIEW PRICE / TECHNICALS
# ==========================================================

def get_tradingview_market_data():
    columns = [
        "close",
        "change",
        "volume",
        "Recommend.All|5",
        "Recommend.All|15",
        "Recommend.All|60",
        "RSI|5",
        "RSI|15",
        "RSI|60",
        "EMA20|5",
        "EMA50|5",
        "EMA20|15",
        "EMA50|15",
        "EMA20|60",
        "EMA50|60",
        "MACD.macd|5",
        "MACD.macd|15",
        "MACD.macd|60",
        "ATR|15",
    ]

    for symbol, screener in TRADINGVIEW_SYMBOLS:
        url = f"https://scanner.tradingview.com/{screener}/scan"
        payload = {
            "symbols": {
                "tickers": [symbol],
                "query": {"types": []},
            },
            "columns": columns,
        }

        response = safe_post(url, payload)
        if not response:
            continue

        try:
            data = response.json()
            rows = data.get("data", [])

            if not rows:
                print(f"[WARN] TradingView empty data for {symbol}")
                continue

            values = rows[0].get("d", [])
            if not values or values[0] is None:
                print(f"[WARN] TradingView no price for {symbol}")
                continue

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
            }

        except Exception as error:
            print(f"[WARN] TradingView parse error for {symbol}: {error}")

    return None


def analyze_technical(tv):
    price = tv["price"]
    change = tv.get("change") or 0
    atr = tv.get("atr_15m")

    score = 0
    confirmations = []
    warnings = []

    momentum = "NEUTRAL"

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 45
        momentum = "VERY STRONG UP"
        confirmations.append("very strong upside momentum")
    elif change >= STRONG_UP_MOVE_PERCENT:
        score += 30
        momentum = "STRONG UP"
        confirmations.append("strong upside momentum")
    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 45
        momentum = "VERY STRONG DOWN"
        confirmations.append("very strong downside momentum")
    elif change <= STRONG_DOWN_MOVE_PERCENT:
        score -= 30
        momentum = "STRONG DOWN"
        confirmations.append("strong downside momentum")

    # Multi-timeframe recommendations
    for tf, rec in [
        ("5m", tv.get("recommend_5m")),
        ("15m", tv.get("recommend_15m")),
        ("1h", tv.get("recommend_1h")),
    ]:
        if rec is not None:
            add_score = int(rec * 22)
            score += add_score
            if rec > 0.25:
                confirmations.append(f"TV {tf} buy bias")
            elif rec < -0.25:
                warnings.append(f"TV {tf} sell bias")

    # EMA trend by timeframe
    trend_5m = "UNKNOWN"
    trend_15m = "UNKNOWN"
    trend_1h = "UNKNOWN"

    def ema_trend(tf, ema20, ema50, weight):
        nonlocal score
        if ema20 is None or ema50 is None:
            return "UNKNOWN"
        if ema20 > ema50:
            score += weight
            return "UP"
        score -= weight
        return "DOWN"

    trend_5m = ema_trend("5m", tv.get("ema20_5m"), tv.get("ema50_5m"), 8)
    trend_15m = ema_trend("15m", tv.get("ema20_15m"), tv.get("ema50_15m"), 15)
    trend_1h = ema_trend("1h", tv.get("ema20_1h"), tv.get("ema50_1h"), 18)

    if trend_5m == trend_15m == trend_1h == "UP":
        score += 20
        confirmations.append("5m/15m/1h trend alignment UP")
        trend = "UP"
    elif trend_5m == trend_15m == trend_1h == "DOWN":
        score -= 20
        warnings.append("5m/15m/1h trend alignment DOWN")
        trend = "DOWN"
    elif trend_15m == "UP" and trend_1h == "UP":
        trend = "UP"
        confirmations.append("15m/1h trend UP")
    elif trend_15m == "DOWN" and trend_1h == "DOWN":
        trend = "DOWN"
        warnings.append("15m/1h trend DOWN")
    else:
        trend = "MIXED"

    # RSI exhaustion / reversal risk
    rsi_5m = tv.get("rsi_5m")
    rsi_15m = tv.get("rsi_15m")
    rsi_1h = tv.get("rsi_1h")

    for tf, rsi in [("5m", rsi_5m), ("15m", rsi_15m), ("1h", rsi_1h)]:
        if rsi is None:
            continue
        if rsi > 78:
            score -= 14
            warnings.append(f"{tf} RSI overbought")
        elif rsi < 22:
            score += 14
            confirmations.append(f"{tf} RSI oversold bounce")

    # MACD
    for tf, macd, weight in [
        ("5m", tv.get("macd_5m"), 6),
        ("15m", tv.get("macd_15m"), 10),
        ("1h", tv.get("macd_1h"), 12),
    ]:
        if macd is None:
            continue
        if macd > 0:
            score += weight
        else:
            score -= weight

    if atr is None or atr == 0:
        atr = price * 0.006

    return {
        "score": score,
        "trend": trend,
        "trend_5m": trend_5m,
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "momentum": momentum,
        "change": round(change, 4),
        "rsi_5m": round(rsi_5m, 2) if rsi_5m is not None else None,
        "rsi_15m": round(rsi_15m, 2) if rsi_15m is not None else None,
        "rsi_1h": round(rsi_1h, 2) if rsi_1h is not None else None,
        "ema20_15m": round(tv.get("ema20_15m"), 4) if tv.get("ema20_15m") is not None else None,
        "ema50_15m": round(tv.get("ema50_15m"), 4) if tv.get("ema50_15m") is not None else None,
        "macd_15m": round(tv.get("macd_15m"), 4) if tv.get("macd_15m") is not None else None,
        "recommend_5m": tv.get("recommend_5m"),
        "recommend_15m": tv.get("recommend_15m"),
        "recommend_1h": tv.get("recommend_1h"),
        "atr_15m": round(atr, 4),
        "confirmations": confirmations[:6],
        "warnings": warnings[:6],
    }


# ==========================================================
# FREE DERIVATIVES / ORDERFLOW DATA
# ==========================================================

def get_json(url):
    response = safe_get(url)
    if not response:
        return None
    try:
        return response.json()
    except Exception as error:
        print(f"[WARN] JSON parse error: {error}")
        return None


def try_binance_endpoint(path, symbol):
    url = f"{BINANCE_FAPI}{path}{symbol}"
    return get_json(url)


def get_binance_orderbook(symbol):
    url = f"{BINANCE_FAPI}/fapi/v1/depth?symbol={symbol}&limit=100"
    data = get_json(url)
    if not data or "bids" not in data or "asks" not in data:
        return None

    try:
        bids = sum(float(price) * float(qty) for price, qty in data["bids"][:30])
        asks = sum(float(price) * float(qty) for price, qty in data["asks"][:30])
        total = bids + asks
        imbalance = 0 if total == 0 else (bids - asks) / total
        return {
            "symbol": symbol,
            "bid_value": bids,
            "ask_value": asks,
            "imbalance": imbalance,
        }
    except Exception as error:
        print(f"[WARN] Orderbook parse error {symbol}: {error}")
        return None


def get_binance_open_interest(symbol):
    data = get_json(f"{BINANCE_FAPI}/fapi/v1/openInterest?symbol={symbol}")
    if not data or "openInterest" not in data:
        return None
    try:
        return {
            "symbol": symbol,
            "open_interest": float(data["openInterest"]),
        }
    except Exception:
        return None


def get_binance_funding(symbol):
    data = get_json(f"{BINANCE_FAPI}/fapi/v1/fundingRate?symbol={symbol}&limit=1")
    if not isinstance(data, list) or not data:
        return None
    try:
        return {
            "symbol": symbol,
            "funding": float(data[0].get("fundingRate", 0.0)),
        }
    except Exception:
        return None


def get_binance_long_short_ratio(symbol):
    data = get_json(f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")
    if not isinstance(data, list) or not data:
        return None
    try:
        row = data[0]
        return {
            "symbol": symbol,
            "long_short_ratio": float(row.get("longShortRatio", 0.0)),
            "long_account": float(row.get("longAccount", 0.0)),
            "short_account": float(row.get("shortAccount", 0.0)),
        }
    except Exception:
        return None


def get_binance_taker_ratio(symbol):
    data = get_json(f"{BINANCE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period=15m&limit=1")
    if not isinstance(data, list) or not data:
        return None
    try:
        row = data[0]
        return {
            "symbol": symbol,
            "buy_sell_ratio": float(row.get("buySellRatio", 0.0)),
            "buy_vol": float(row.get("buyVol", 0.0)),
            "sell_vol": float(row.get("sellVol", 0.0)),
        }
    except Exception:
        return None


def get_bybit_ticker(symbol):
    data = get_json(f"{BYBIT_API}/v5/market/tickers?category=linear&symbol={symbol}")
    try:
        rows = data.get("result", {}).get("list", []) if data else []
        if not rows:
            return None
        row = rows[0]
        return {
            "symbol": symbol,
            "funding": float(row.get("fundingRate", 0.0)),
            "open_interest": float(row.get("openInterest", 0.0)) if row.get("openInterest") else None,
            "turnover_24h": float(row.get("turnover24h", 0.0)) if row.get("turnover24h") else None,
        }
    except Exception as error:
        print(f"[WARN] Bybit ticker parse error {symbol}: {error}")
        return None


def analyze_free_orderflow(tv):
    score = 0
    details = []
    warnings = []

    orderbook = None
    oi = None
    funding = None
    long_short = None
    taker = None
    used_symbol = None

    for symbol in BINANCE_PROXY_SYMBOLS:
        if orderbook is None:
            orderbook = get_binance_orderbook(symbol)
        if oi is None:
            oi = get_binance_open_interest(symbol)
        if funding is None:
            funding = get_binance_funding(symbol)
        if long_short is None:
            long_short = get_binance_long_short_ratio(symbol)
        if taker is None:
            taker = get_binance_taker_ratio(symbol)

        if orderbook or oi or funding or long_short or taker:
            used_symbol = symbol
            break

    # If Binance is blocked from GitHub, try Bybit proxy.
    bybit = None
    if not any([orderbook, oi, funding, long_short, taker]):
        for symbol in BYBIT_PROXY_SYMBOLS:
            bybit = get_bybit_ticker(symbol)
            if bybit:
                used_symbol = f"BYBIT:{symbol}"
                break

    # Orderbook imbalance
    if orderbook:
        imb = orderbook["imbalance"]
        if imb > 0.12:
            score += 18
            details.append(f"bid wall/orderbook bullish imbalance {round(imb, 3)}")
        elif imb < -0.12:
            score -= 18
            warnings.append(f"ask wall/orderbook bearish imbalance {round(imb, 3)}")
        else:
            details.append(f"orderbook neutral {round(imb, 3)}")

    # Funding
    actual_funding = None
    if funding:
        actual_funding = funding["funding"]
    elif bybit:
        actual_funding = bybit.get("funding")

    if actual_funding is not None:
        if actual_funding > 0.0008:
            score -= 12
            warnings.append(f"funding overheated long {actual_funding}")
        elif actual_funding < -0.0005:
            score += 12
            details.append(f"negative funding short squeeze risk {actual_funding}")
        else:
            details.append(f"funding neutral {actual_funding}")

    # Long/short crowd positioning
    if long_short:
        ratio = long_short["long_short_ratio"]
        if ratio > 1.8:
            score -= 10
            warnings.append(f"crowd too long ratio {ratio}")
        elif ratio < 0.75:
            score += 10
            details.append(f"crowd too short ratio {ratio}")
        else:
            details.append(f"long/short neutral {ratio}")

    # Taker buy/sell ratio
    if taker:
        ratio = taker["buy_sell_ratio"]
        if ratio > 1.15:
            score += 16
            details.append(f"taker aggressive buying {ratio}")
        elif ratio < 0.85:
            score -= 16
            warnings.append(f"taker aggressive selling {ratio}")
        else:
            details.append(f"taker flow neutral {ratio}")

    # Open interest availability
    open_interest = None
    if oi:
        open_interest = oi.get("open_interest")
    elif bybit:
        open_interest = bybit.get("open_interest")

    if open_interest:
        details.append(f"open interest available {round(open_interest, 2)}")

    # TradingView proxy volume + momentum
    change = tv.get("change") or 0
    tv_volume = tv.get("volume") or 0

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 15
        details.append("TV momentum confirms buyers")
    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 15
        warnings.append("TV momentum confirms sellers")

    if tv_volume:
        details.append(f"TV volume {round(tv_volume, 2)}")

    if not used_symbol:
        used_symbol = "TradingView only"
        details.append("Binance/Bybit public data unavailable, using TradingView only")

    bias = "NEUTRAL"
    if score >= 25:
        bias = "BULLISH ORDERFLOW"
    elif score <= -25:
        bias = "BEARISH ORDERFLOW"

    return {
        "score": score,
        "bias": bias,
        "used_symbol": used_symbol,
        "orderbook": orderbook,
        "open_interest": open_interest,
        "funding": actual_funding,
        "long_short": long_short,
        "taker": taker,
        "details": details[:7],
        "warnings": warnings[:7],
    }


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
        pass

    try:
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


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
            news.append(
                {
                    "title": title,
                    "link": link,
                    "source": source["name"],
                    "published_at": None,
                    "weight": source.get("weight", 1.0) * 0.4,
                }
            )

    except Exception as error:
        print(f"[WARN] HTML parse error {source['name']}: {error}")

    return news


def parse_rss(source):
    response = safe_get(source["url"])
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

            date_text = (
                get_item_text(item, "pubDate")
                or get_item_text(item, "published")
                or get_item_text(item, "updated")
                or get_item_text(item, "dc:date")
            )

            published_at = parse_date(date_text)

            if published_at and published_at < cutoff:
                continue

            if title:
                news.append(
                    {
                        "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                        "link": link,
                        "source": source["name"],
                        "published_at": published_at,
                        "weight": source.get("weight", 1.0),
                    }
                )

    except Exception as error:
        print(f"[WARN] RSS parse error {source['name']}: {error}")
        return parse_html_news(source, response.text)

    return news


def get_cryptopanic_news():
    if not CRYPTOPANIC_KEY:
        return []

    url = "https://cryptopanic.com/api/v1/posts/"
    params = f"?auth_token={CRYPTOPANIC_KEY}&filter=hot&public=true"
    response = safe_get(url + params)

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
                news.append(
                    {
                        "title": title,
                        "link": item.get("url", ""),
                        "source": "CryptoPanic",
                        "published_at": published_at,
                        "weight": 0.7,
                    }
                )

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

    for source in NEWS_SOURCES:
        all_news.extend(parse_rss(source))

    all_news.extend(get_cryptopanic_news())
    return deduplicate_news(all_news)


def keyword_score(title, words):
    title_lower = title.lower()
    return sum(1 for word in words if word in title_lower)


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

        if bull_hits:
            bullish += 1
            raw_score += 6 * bull_hits * weight

        if bear_hits:
            bearish += 1
            raw_score -= 6 * bear_hits * weight

        if impact_hits:
            impact += 1
            raw_score += 4 * impact_hits * weight
            important.append(item)

        if breaking_hits:
            breaking += 1
            raw_score += 8 * breaking_hits * weight
            if item not in important:
                important.append(item)

    if bullish > bearish:
        sentiment = "BULLISH"
    elif bearish > bullish:
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    capped_score = int(max(-MAX_NEWS_SCORE, min(MAX_NEWS_SCORE, raw_score)))

    return {
        "score": capped_score,
        "raw_score": round(raw_score, 2),
        "sentiment": sentiment,
        "bullish": bullish,
        "bearish": bearish,
        "impact": impact,
        "breaking": breaking,
        "important": important[:7],
        "total": len(news),
    }


# ==========================================================
# SIGNAL ENGINE + ENTRY / STOP / TAKE PROFIT
# ==========================================================

def build_signal(tech, news, orderflow):
    score = tech["score"] + news["score"] + orderflow["score"]

    signal_type = "NO TRADE"
    signal = "NO SIGNAL"

    # Confirmation boosts
    if news["total"] >= 3 and news["score"] >= 55:
        score += 12

    # If technicals are against the news, reduce instead of blindly entering.
    if tech["score"] < 0 and news["score"] > 40:
        score -= 10
    if tech["score"] > 0 and news["score"] < -40:
        score += 10

    if tech.get("momentum") == "VERY STRONG UP" and score >= 35:
        signal = "LONG"
        signal_type = "MOMENTUM LONG / BREAKOUT SCALP"
    elif tech.get("momentum") == "VERY STRONG DOWN" and score <= -35:
        signal = "SHORT"
        signal_type = "MOMENTUM SHORT / BREAKDOWN SCALP"
    elif score >= 70 and tech.get("trend") == "UP" and orderflow["score"] >= 0:
        signal = "LONG"
        signal_type = "TREND LONG"
    elif score <= -70 and tech.get("trend") == "DOWN" and orderflow["score"] <= 0:
        signal = "SHORT"
        signal_type = "TREND SHORT"
    elif score >= 65:
        signal = "LONG"
        signal_type = "HIGH RISK LONG / MIXED CONFIRMATION"
    elif score <= -65:
        signal = "SHORT"
        signal_type = "HIGH RISK SHORT / MIXED CONFIRMATION"

    confidence = min(95, max(0, abs(score)))

    risk_note = "NORMAL"
    if "HIGH RISK" in signal_type:
        risk_note = "HIGH RISK: confirmations are mixed"
    if tech.get("trend") == "DOWN" and signal == "LONG":
        risk_note = "SCALP ONLY: higher timeframe trend is not confirmed"
    if tech.get("trend") == "UP" and signal == "SHORT":
        risk_note = "SCALP ONLY: higher timeframe trend is not confirmed"

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
            "note": "No entry. Wait for confirmation.",
        }

    if signal == "LONG":
        if "MOMENTUM" in signal_type:
            stop = price - atr * 1.1
            tp1 = price + atr * 0.9
            tp2 = price + atr * 1.6
            tp3 = price + atr * 2.4
            note = "Momentum scalp: do not chase after a huge candle; better entry on pullback/retest."
        else:
            stop = price - atr * 1.5
            tp1 = price + atr * 1.2
            tp2 = price + atr * 2.0
            tp3 = price + atr * 3.0
            note = "Trend long: entry only if price holds above EMA20/EMA50 zone."

    else:
        if "MOMENTUM" in signal_type:
            stop = price + atr * 1.1
            tp1 = price - atr * 0.9
            tp2 = price - atr * 1.6
            tp3 = price - atr * 2.4
            note = "Momentum scalp: do not chase after a huge candle; better entry on pullback/retest."
        else:
            stop = price + atr * 1.5
            tp1 = price - atr * 1.2
            tp2 = price - atr * 2.0
            tp3 = price - atr * 3.0
            note = "Trend short: entry only if price rejects EMA20/EMA50 zone."

    return {
        "entry": round(price, 4),
        "stop": round(stop, 4),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
        "tp3": round(tp3, 4),
        "note": note,
    }


def format_time(dt):
    if not dt:
        return "time unknown"
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
    print("START BZU FREE ORDERFLOW BOT")

    tv = get_tradingview_market_data()
    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    fresh_news = get_all_fresh_news()
    tech = analyze_technical(tv)
    news = analyze_news(fresh_news)
    orderflow = analyze_free_orderflow(tv)

    signal, signal_type, score, confidence, risk_note = build_signal(tech, news, orderflow)
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
    print(f"MOMENTUM: {tech['momentum']} | CHANGE: {tech['change']}%")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL TYPE: {signal_type}")
    print(f"SIGNAL: {signal}")

    if signal == "NO SIGNAL" or confidence < MIN_CONFIDENCE_TO_SEND:
        print("NO SIGNAL")
        return

    message = f"""
<b>BZU SIGNAL BOT ULTRA</b>

<b>Source:</b> TradingView + free orderflow
<b>Symbol:</b> {tv['symbol']}
<b>Signal:</b> {signal}
<b>Signal type:</b> {signal_type}
<b>Confidence:</b> {confidence}%
<b>Risk:</b> {risk_note}

<b>Price:</b> {tv['price']}
<b>Change:</b> {tv['change']}%
<b>Momentum:</b> {tech['momentum']}

<b>Entry:</b> {plan['entry']}
<b>Stop:</b> {plan['stop']}
<b>TP1:</b> {plan['tp1']}
<b>TP2:</b> {plan['tp2']}
<b>TP3:</b> {plan['tp3']}
<b>Plan:</b> {plan['note']}

<b>Trend:</b> {tech['trend']}
<b>Trend 5m:</b> {tech['trend_5m']}
<b>Trend 15m:</b> {tech['trend_15m']}
<b>Trend 1h:</b> {tech['trend_1h']}
<b>RSI 5m:</b> {tech['rsi_5m']}
<b>RSI 15m:</b> {tech['rsi_15m']}
<b>RSI 1h:</b> {tech['rsi_1h']}
<b>MACD 15m:</b> {tech['macd_15m']}
<b>EMA20 15m:</b> {tech['ema20_15m']}
<b>EMA50 15m:</b> {tech['ema50_15m']}
<b>ATR 15m:</b> {tech['atr_15m']}

<b>Orderflow:</b> {orderflow['bias']}
<b>Orderflow proxy:</b> {orderflow['used_symbol']}
<b>Orderflow score:</b> {orderflow['score']}
<b>Funding:</b> {orderflow['funding']}
<b>Open Interest:</b> {orderflow['open_interest']}
"""

    if orderflow["details"]:
        message += "\n<b>Orderflow details:</b>"
        for item in orderflow["details"]:
            message += f"\n- {item}"

    if orderflow["warnings"]:
        message += "\n\n<b>Warnings:</b>"
        for item in orderflow["warnings"]:
            message += f"\n- {item}"

    message += f"""

<b>News lookback:</b> last {NEWS_LOOKBACK_HOURS}h / breaking mode
<b>Fresh news:</b> {news['total']}
<b>News Sentiment:</b> {news['sentiment']}
<b>Bullish:</b> {news['bullish']}
<b>Bearish:</b> {news['bearish']}
<b>Impact:</b> {news['impact']}
<b>Breaking:</b> {news['breaking']}
<b>News score:</b> {news['score']} / raw {news['raw_score']}

<b>Important fresh news:</b>
"""

    if news["important"]:
        for item in news["important"]:
            message += f"\n- [{item['source']}] {item['title']} ({format_time(item['published_at'])})"
    else:
        message += "\nNone"

    send_telegram(message.strip())
    print("TELEGRAM SENT")
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
