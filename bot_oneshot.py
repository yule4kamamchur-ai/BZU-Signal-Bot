import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")

# TradingView symbols are checked in this order.
# Your working symbol from the previous run was BINANCE:BZUSDT.P.
TRADINGVIEW_SYMBOLS = [
    ("BINANCE:BZUSDT.P", "crypto"),
    ("BINANCE:BZUSDT", "crypto"),
    ("TVC:UKOIL", "cfd"),
    ("NYMEX:BZ1!", "futures"),
]

# Only fresh news will be counted.
NEWS_LOOKBACK_HOURS = 2
MAX_NEWS_SCORE = 80
MAX_ITEMS_PER_FEED = 15

# Momentum thresholds from TradingView daily/change field.
# If BZUSDT.P moves fast, the bot must not ignore it just because EMA/RSI is mixed.
STRONG_UP_MOVE_PERCENT = 1.2
VERY_STRONG_UP_MOVE_PERCENT = 1.8
STRONG_DOWN_MOVE_PERCENT = -1.2
VERY_STRONG_DOWN_MOVE_PERCENT = -1.8

# RSS/API sources that are usually available from GitHub Actions.
# Removed broken oilprice.com/rss/oilprices.xml and direct ForexFactory scraping.
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
        "weight": 0.9,
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

DUPLICATE_MEMORY_SECONDS = 6 * 60 * 60


def now_utc():
    return datetime.now(timezone.utc)


def safe_get(url, timeout=15):
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, text/html, application/json, */*",
            },
        )

        if response.status_code >= 400:
            print(f"[WARN] HTTP {response.status_code}: {url}")
            print(response.text[:250])
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
            print(response.text[:250])
            return None

        return response

    except Exception as error:
        print(f"[WARN] {url}: {error}")
        return None


def get_tradingview_market_data():
    columns = [
        "close",
        "change",
        "volume",
        "Recommend.All|15",
        "RSI|15",
        "EMA20|15",
        "EMA50|15",
        "MACD.macd|15",
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
                "recommend": values[3],
                "rsi": values[4],
                "ema20": values[5],
                "ema50": values[6],
                "macd": values[7],
            }

        except Exception as error:
            print(f"[WARN] TradingView parse error for {symbol}: {error}")

    return None


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
    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)

    try:
        soup = BeautifulSoup(html, "html.parser")
        candidates = []

        for tag in soup.find_all(["a", "h2", "h3"]):
            text = tag.get_text(" ", strip=True)
            if not text or len(text) < 18:
                continue

            lower = text.lower()
            if not any(key in lower for key in ["oil", "crude", "brent", "opec", "eia", "inventory", "gas", "energy"]):
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
                    "weight": source.get("weight", 1.0) * 0.5,
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
        content = response.content.strip()
        root = ET.fromstring(content)
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


def get_all_fresh_news():
    all_news = []

    for source in NEWS_SOURCES:
        all_news.extend(parse_rss(source))

    all_news.extend(get_cryptopanic_news())
    return deduplicate_news(all_news)


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


def keyword_score(title, words):
    title_lower = title.lower()
    return sum(1 for word in words if word in title_lower)


def analyze_news(news):
    bullish = 0
    bearish = 0
    impact = 0
    raw_score = 0
    important = []

    for item in news:
        title = item["title"]
        weight = item.get("weight", 1.0)

        bull_hits = keyword_score(title, BULLISH_WORDS)
        bear_hits = keyword_score(title, BEARISH_WORDS)
        impact_hits = keyword_score(title, HIGH_IMPACT_WORDS)

        if bull_hits:
            bullish += 1
            raw_score += 6 * bull_hits * weight

        if bear_hits:
            bearish += 1
            raw_score -= 6 * bear_hits * weight

        breaking_hits = keyword_score(title, BREAKING_WORDS)

        if impact_hits:
            impact += 1
            raw_score += 4 * impact_hits * weight
            important.append(item)

        if breaking_hits:
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
        "important": important[:7],
        "total": len(news),
    }


def get_macro_events():
    # ForexFactory blocks GitHub Actions with Cloudflare.
    # To avoid false signals, direct scraping was removed.
    # Use RSS/news keywords instead: CPI, FOMC, Fed, Powell, inventories, EIA, OPEC.
    return []


def analyze_technical(tv):
    price = tv["price"]
    change = tv.get("change")
    ema20 = tv["ema20"]
    ema50 = tv["ema50"]
    rsi = tv["rsi"]
    macd = tv["macd"]
    recommend = tv["recommend"]

    score = 0

    if recommend is not None:
        score += int(recommend * 35)

    momentum = "NEUTRAL"

    if change is not None:
        if change >= VERY_STRONG_UP_MOVE_PERCENT:
            score += 45
            momentum = "VERY STRONG UP"
        elif change >= STRONG_UP_MOVE_PERCENT:
            score += 30
            momentum = "STRONG UP"
        elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
            score -= 45
            momentum = "VERY STRONG DOWN"
        elif change <= STRONG_DOWN_MOVE_PERCENT:
            score -= 30
            momentum = "STRONG DOWN"

    if ema20 is not None and price > ema20:
        score += 15
    else:
        score -= 15

    if ema20 is not None and ema50 is not None and ema20 > ema50:
        score += 20
        trend = "UP"
    else:
        score -= 20
        trend = "DOWN"

    if rsi is not None:
        if rsi < 30:
            score += 12
        elif rsi > 70:
            score -= 12

    if macd is not None:
        if macd > 0:
            score += 12
        else:
            score -= 12

    return {
        "score": score,
        "trend": trend,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "macd": round(macd, 4) if macd is not None else None,
        "recommend": recommend,
        "momentum": momentum,
        "change": round(change, 4) if change is not None else None,
    }


def build_signal(tech, news):
    score = tech["score"] + news["score"]

    # Require at least some agreement. If technicals are against the news,
    # confidence is reduced instead of letting news force a signal every time.
    if news["total"] >= 3 and news["score"] >= 55:
        score += 20

    if tech["score"] < 0 and news["score"] > 40:
        score -= 10

    if tech["score"] > 0 and news["score"] < -40:
        score += 20

    if tech.get("momentum") == "VERY STRONG UP" and score >= 25:
        signal = "LONG"
    elif tech.get("momentum") == "VERY STRONG DOWN" and score <= -25:
        signal = "SHORT"
    elif score >= 55:
        signal = "LONG"
    elif score <= -55:
        signal = "SHORT"
    else:
        signal = "NO SIGNAL"

    confidence = min(95, max(0, abs(score)))
    return signal, score, confidence


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
    print("START BZU TRADINGVIEW FRESH NEWS BOT")

    tv = get_tradingview_market_data()
    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    fresh_news = get_all_fresh_news()
    macro_events = get_macro_events()

    tech = analyze_technical(tv)
    news = analyze_news(fresh_news)

    signal, score, confidence = build_signal(tech, news)

    print(f"SOURCE: {tv['source']}")
    print(f"SYMBOL: {tv['symbol']}")
    print(f"PRICE: {tv['price']}")
    print(f"CHANGE: {tv['change']}")
    print(f"FRESH NEWS COUNT: {news['total']}")
    print(f"NEWS RAW SCORE: {news['raw_score']}")
    print(f"NEWS CAPPED SCORE: {news['score']}")
    print(f"TECH SCORE: {tech['score']}")
    print(f"MOMENTUM: {tech['momentum']} | CHANGE: {tech['change']}%")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL: {signal}")

    if signal == "NO SIGNAL":
        print("NO SIGNAL")
        return

    message = f"""
<b>BZU SIGNAL BOT ULTRA</b>

<b>Source:</b> TradingView
<b>Symbol:</b> {tv['symbol']}
<b>Signal:</b> {signal}
<b>Confidence:</b> {confidence}%

<b>Price:</b> {tv['price']}
<b>Change:</b> {tv['change']}%
<b>Momentum:</b> {tech['momentum']}

<b>Trend:</b> {tech['trend']}
<b>RSI 15m:</b> {tech['rsi']}
<b>MACD 15m:</b> {tech['macd']}
<b>EMA20 15m:</b> {tech['ema20']}
<b>EMA50 15m:</b> {tech['ema50']}
<b>TradingView Recommend 15m:</b> {tech['recommend']}
<b>Momentum change:</b> {tech['change']}%

<b>News lookback:</b> last {NEWS_LOOKBACK_HOURS}h / breaking mode
<b>Fresh news:</b> {news['total']}
<b>News Sentiment:</b> {news['sentiment']}
<b>Bullish:</b> {news['bullish']}
<b>Bearish:</b> {news['bearish']}
<b>Impact:</b> {news['impact']}
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
