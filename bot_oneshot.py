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

NEWS_LOOKBACK_HOURS = 2
MAX_NEWS_SCORE = 90
MIN_CONFIDENCE_TO_SEND = 55

STRONG_UP_MOVE_PERCENT = 1.2
VERY_STRONG_UP_MOVE_PERCENT = 1.8
STRONG_DOWN_MOVE_PERCENT = -1.2
VERY_STRONG_DOWN_MOVE_PERCENT = -1.8

GDELT_QUERIES = [
    '(oil OR crude OR Brent OR BZ) (Trump OR tariff OR sanctions OR Iran OR Russia OR Ukraine OR war OR Hormuz OR OPEC OR OPEC+ OR EIA OR inventory OR Fed OR Powell)',
]

NEWS_SOURCES = [
    {
        "name": "EIA",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "weight": 1.2,
    },
    {
        "name": "OilPrice",
        "url": "https://oilprice.com/rss/main",
        "weight": 1.0,
    },
]

BULLISH_WORDS = [
    "inventory draw",
    "opec cut",
    "sanctions",
    "war",
    "supply disruption",
    "bullish",
    "surge",
    "rally",
]

BEARISH_WORDS = [
    "inventory build",
    "oversupply",
    "ceasefire",
    "recession",
    "bearish",
    "drop",
]

BREAKING_WORDS = [
    "trump",
    "white house",
    "opec",
    "iran",
    "russia",
    "ukraine",
    "fed",
    "powell",
    "inventory",
    "eia",
]

def now_utc():
    return datetime.now(timezone.utc)

def safe_get(url, timeout=15):
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
            },
        )

        if response.status_code >= 400:
            print(f"[WARN] HTTP {response.status_code}: {url}")
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
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
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

def get_tradingview_market_data():

    columns = [
        "close",
        "change",
        "volume",
        "Recommend.All|5",
        "Recommend.All|15",
        "Recommend.All|60",
        "RSI|15",
        "EMA20|15",
        "EMA50|15",
        "MACD.macd|15",
        "ATR|15",
        "ADX|15",
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
                continue

            values = rows[0]["d"]

            return {
                "source": "TradingView",
                "symbol": symbol,
                "price": float(values[0]),
                "change": values[1],
                "volume": values[2],
                "recommend_5m": values[3],
                "recommend_15m": values[4],
                "recommend_1h": values[5],
                "rsi_15m": values[6],
                "ema20_15m": values[7],
                "ema50_15m": values[8],
                "macd_15m": values[9],
                "atr_15m": values[10],
                "adx_15m": values[11],
            }

        except Exception as error:
            print(f"[WARN] TV parse error: {error}")

    return None

# ==========================================================
# TECHNICAL ANALYSIS
# ==========================================================

def analyze_technical(tv):

    score = 0
    confirmations = []
    warnings = []

    change = tv.get("change") or 0

    momentum = "NEUTRAL"

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 45
        momentum = "VERY STRONG UP"
        confirmations.append("strong bullish momentum")

    elif change >= STRONG_UP_MOVE_PERCENT:
        score += 30
        momentum = "STRONG UP"

    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 45
        momentum = "VERY STRONG DOWN"

    elif change <= STRONG_DOWN_MOVE_PERCENT:
        score -= 30
        momentum = "STRONG DOWN"

    ema20 = tv.get("ema20_15m")
    ema50 = tv.get("ema50_15m")

    if ema20 and ema50:

        if ema20 > ema50:
            score += 20
            trend = "UP"

        else:
            score -= 20
            trend = "DOWN"

    else:
        trend = "UNKNOWN"

    rsi = tv.get("rsi_15m")

    if rsi:

        if rsi > 75:
            score -= 10
            warnings.append("RSI overbought")

        elif rsi < 25:
            score += 10
            confirmations.append("RSI oversold bounce")

    macd = tv.get("macd_15m")

    if macd:

        if macd > 0:
            score += 10
        else:
            score -= 10

    adx = tv.get("adx_15m")

    if adx:

        if adx > 25:
            score += 10
            confirmations.append("strong trend ADX")

    return {
        "score": score,
        "trend": trend,
        "momentum": momentum,
        "change": round(change, 4),
        "confirmations": confirmations,
        "warnings": warnings,
        "rsi_15m": rsi,
        "ema20_15m": ema20,
        "ema50_15m": ema50,
        "macd_15m": macd,
        "adx_15m": adx,
        "atr_15m": tv.get("atr_15m"),
    }

# ==========================================================
# ORDERFLOW
# ==========================================================

def analyze_free_orderflow(tv):

    score = 0
    details = []
    warnings = []

    change = tv.get("change") or 0
    volume = tv.get("volume") or 0

    recommend_5m = tv.get("recommend_5m") or 0
    recommend_15m = tv.get("recommend_15m") or 0
    recommend_1h = tv.get("recommend_1h") or 0

    if change >= VERY_STRONG_UP_MOVE_PERCENT:
        score += 25
        details.append("very strong bullish momentum")

    elif change <= VERY_STRONG_DOWN_MOVE_PERCENT:
        score -= 25
        warnings.append("very strong bearish momentum")

    if (
        recommend_5m > 0.2
        and recommend_15m > 0.2
        and recommend_1h > 0.2
    ):
        score += 20
        details.append("multi timeframe bullish alignment")

    elif (
        recommend_5m < -0.2
        and recommend_15m < -0.2
        and recommend_1h < -0.2
    ):
        score -= 20
        warnings.append("multi timeframe bearish alignment")

    if volume and volume > 0:

        details.append(f"TV volume {round(volume, 2)}")

        if abs(change) > 1.5:
            score += 10 if change > 0 else -10
            details.append("volume confirms breakout")

    bias = "NEUTRAL"

    if score >= 25:
        bias = "BULLISH ORDERFLOW"

    elif score <= -25:
        bias = "BEARISH ORDERFLOW"

    return {
        "score": score,
        "bias": bias,
        "used_symbol": "TradingView",
        "details": details[:7],
        "warnings": warnings[:7],
    }

# ==========================================================
# NEWS
# ==========================================================

def parse_gdelt_date(value):

    try:
        return datetime.strptime(
            value[:14],
            "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)

    except Exception:
        return None

def get_gdelt_news():

    news = []

    for query in GDELT_QUERIES:

        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={quote_plus(query)}"
            "&mode=ArtList"
            "&format=json"
            "&maxrecords=50"
            "&sort=HybridRel"
            "&timespan=2h"
        )

        response = safe_get(url, timeout=20)

        if not response:
            continue

        try:
            data = response.json()

            for article in data.get("articles", []):

                title = article.get("title", "")

                if not title:
                    continue

                news.append({
                    "title": title,
                    "source": article.get("domain", "GDELT"),
                    "published_at": parse_gdelt_date(
                        article.get("seendate")
                    ),
                    "weight": 1.2,
                })

        except Exception as error:
            print(f"[WARN] GDELT parse: {error}")

    return news

def get_rss_news():

    news = []

    cutoff = now_utc() - timedelta(hours=NEWS_LOOKBACK_HOURS)

    for source in NEWS_SOURCES:

        response = safe_get(source["url"])

        if not response:
            continue

        try:
            root = ET.fromstring(response.content)

            items = root.findall(".//item")

            for item in items[:15]:

                title = item.findtext("title")

                pub_date = item.findtext("pubDate")

                if not title:
                    continue

                published = None

                try:
                    published = parsedate_to_datetime(pub_date)

                    if published.tzinfo is None:
                        published = published.replace(
                            tzinfo=timezone.utc
                        )

                except Exception:
                    pass

                if published and published < cutoff:
                    continue

                news.append({
                    "title": title,
                    "source": source["name"],
                    "published_at": published,
                    "weight": source["weight"],
                })

        except Exception as error:
            print(f"[WARN] RSS parse: {error}")

    return news

def get_all_fresh_news():

    news = []

    news.extend(get_gdelt_news())
    news.extend(get_rss_news())

    return news

def keyword_score(title, words):

    lower = title.lower()

    return sum(
        1 for word in words
        if word in lower
    )

def analyze_news(news):

    bullish = 0
    bearish = 0
    breaking = 0

    raw_score = 0

    important = []

    for item in news:

        title = item["title"]

        weight = item.get("weight", 1.0)

        bull_hits = keyword_score(
            title,
            BULLISH_WORDS
        )

        bear_hits = keyword_score(
            title,
            BEARISH_WORDS
        )

        breaking_hits = keyword_score(
            title,
            BREAKING_WORDS
        )

        if bull_hits:
            bullish += 1
            raw_score += 6 * bull_hits * weight

        if bear_hits:
            bearish += 1
            raw_score -= 6 * bear_hits * weight

        if breaking_hits:
            breaking += 1
            raw_score += 7 * breaking_hits * weight
            important.append(item)

    if bullish > bearish:
        sentiment = "BULLISH"

    elif bearish > bullish:
        sentiment = "BEARISH"

    else:
        sentiment = "NEUTRAL"

    capped_score = int(
        max(
            -MAX_NEWS_SCORE,
            min(MAX_NEWS_SCORE, raw_score)
        )
    )

    return {
        "score": capped_score,
        "raw_score": round(raw_score, 2),
        "sentiment": sentiment,
        "bullish": bullish,
        "bearish": bearish,
        "breaking": breaking,
        "important": important[:8],
        "total": len(news),
    }

# ==========================================================
# SIGNAL ENGINE
# ==========================================================

def build_signal(tech, news, orderflow):

    score = (
        tech["score"]
        + news["score"]
        + orderflow["score"]
    )

    signal = "NO SIGNAL"
    signal_type = "NO TRADE"

    if (
        tech["momentum"] == "VERY STRONG UP"
        and score >= 35
    ):
        signal = "LONG"
        signal_type = "MOMENTUM LONG"

    elif (
        tech["momentum"] == "VERY STRONG DOWN"
        and score <= -35
    ):
        signal = "SHORT"
        signal_type = "MOMENTUM SHORT"

    elif (
        score >= 70
        and tech["trend"] == "UP"
    ):
        signal = "LONG"
        signal_type = "TREND LONG"

    elif (
        score <= -70
        and tech["trend"] == "DOWN"
    ):
        signal = "SHORT"
        signal_type = "TREND SHORT"

    confidence = min(
        95,
        max(0, abs(score))
    )

    return signal, signal_type, score, confidence

# ==========================================================
# ENTRY / STOP / TP
# ==========================================================

def make_trade_plan(signal, price, tech):

    atr = tech.get("atr_15m") or price * 0.006

    if signal == "NO SIGNAL":
        return None

    if signal == "LONG":

        return {
            "entry": round(price, 4),
            "stop": round(price - atr * 1.3, 4),
            "tp1": round(price + atr * 1.2, 4),
            "tp2": round(price + atr * 2.0, 4),
        }

    return {
        "entry": round(price, 4),
        "stop": round(price + atr * 1.3, 4),
        "tp1": round(price - atr * 1.2, 4),
        "tp2": round(price - atr * 2.0, 4),
    }

# ==========================================================
# TELEGRAM
# ==========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        requests.post(url, json=payload, timeout=10)

    except Exception as error:
        print(f"[WARN] TG: {error}")

# ==========================================================
# MAIN
# ==========================================================

def main():

    print("START BZU PROFESSIONAL FREE BOT")

    tv = get_tradingview_market_data()

    if not tv:
        print("TRADINGVIEW ERROR")
        return

    fresh_news = get_all_fresh_news()

    tech = analyze_technical(tv)

    news = analyze_news(fresh_news)

    orderflow = analyze_free_orderflow(tv)

    signal, signal_type, score, confidence = build_signal(
        tech,
        news,
        orderflow,
    )

    plan = make_trade_plan(
        signal,
        tv["price"],
        tech,
    )

    print(f"SOURCE: {tv['source']}")
    print(f"SYMBOL: {tv['symbol']}")
    print(f"PRICE: {tv['price']}")
    print(f"CHANGE: {tv['change']}")
    print(f"FRESH NEWS COUNT: {news['total']}")
    print(f"NEWS SCORE: {news['score']}")
    print(f"TECH SCORE: {tech['score']}")
    print(f"ORDERFLOW SCORE: {orderflow['score']}")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL: {signal}")

    if (
        signal == "NO SIGNAL"
        or confidence < MIN_CONFIDENCE_TO_SEND
    ):
        print("NO SIGNAL")
        return

    message = f"""
<b>BZU SIGNAL BOT</b>

<b>Signal:</b> {signal}
<b>Type:</b> {signal_type}
<b>Confidence:</b> {confidence}%

<b>Price:</b> {tv['price']}
<b>Change:</b> {tv['change']}%
<b>Trend:</b> {tech['trend']}
<b>Momentum:</b> {tech['momentum']}

<b>Entry:</b> {plan['entry']}
<b>Stop:</b> {plan['stop']}
<b>TP1:</b> {plan['tp1']}
<b>TP2:</b> {plan['tp2']}

<b>News sentiment:</b> {news['sentiment']}
<b>News score:</b> {news['score']}
<b>Fresh news:</b> {news['total']}

<b>Orderflow:</b> {orderflow['bias']}
"""

    send_telegram(message)

    print("TELEGRAM SENT")
    print("BOT COMPLETE")

if __name__ == "__main__":
    main()
