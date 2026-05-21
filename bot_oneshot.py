import os
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")

INST_ID = "BTC-USDT-SWAP"


CRYPTO_RSS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptoslate.com/feed/",
]


OIL_RSS = [
    "https://www.eia.gov/rss/todayinenergy.xml",
    "https://oilprice.com/rss/main",
    "https://oilprice.com/rss/oilprices.xml",
]


BULLISH = [
    "etf",
    "approval",
    "buy",
    "bullish",
    "surge",
    "rally",
    "breakout",
    "institutional",
    "accumulation",
    "rate cut",
    "fed pause",
]


BEARISH = [
    "crash",
    "sell",
    "bearish",
    "lawsuit",
    "hack",
    "liquidation",
    "war",
    "inflation",
    "rate hike",
    "recession",
]


HIGH_IMPACT = [
    "fed",
    "cpi",
    "fomc",
    "etf",
    "sec",
    "blackrock",
    "war",
    "opec",
    "inventory",
    "eia",
]


def safe_get(url, timeout=15):
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        return response
    except Exception as error:
        print(f"[WARN] {url}: {error}")
        return None


def get_okx_price():
    url = f"https://www.okx.com/api/v5/market/ticker?instId={INST_ID}"
    response = safe_get(url)

    if not response:
        return None

    data = response.json()["data"][0]

    return {
        "last": float(data["last"]),
        "bid": float(data["bidPx"]),
        "ask": float(data["askPx"]),
        "vol24h": float(data["vol24h"]),
    }


def get_okx_funding():
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={INST_ID}"
    response = safe_get(url)

    if not response:
        return 0.0

    data = response.json()["data"][0]
    return float(data["fundingRate"])


def get_okx_candles():
    url = (
        f"https://www.okx.com/api/v5/market/candles?"
        f"instId={INST_ID}&bar=15m&limit=100"
    )

    response = safe_get(url)

    if not response:
        return []

    raw = response.json()["data"]
    candles = []

    for candle in reversed(raw):
        candles.append(
            {
                "close": float(candle[4]),
                "volume": float(candle[5]),
            }
        )

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = values[0]

    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]

        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs_value = avg_gain / avg_loss
    return 100 - (100 / (1 + rs_value))


def macd(values):
    if len(values) < 35:
        return 0.0

    ema12 = ema(values[-35:], 12)
    ema26 = ema(values[-35:], 26)

    if ema12 is None or ema26 is None:
        return 0.0

    return ema12 - ema26


def analyze_technical(candles):
    if len(candles) < 80:
        return {
            "score": 0,
            "trend": "UNKNOWN",
            "rsi": None,
            "ema20": None,
            "ema50": None,
            "macd": None,
            "volume_spike": False,
        }

    closes = [item["close"] for item in candles]
    volumes = [item["volume"] for item in candles]

    current_price = closes[-1]

    ema20 = ema(closes[-50:], 20)
    ema50 = ema(closes[-80:], 50)
    current_rsi = rsi(closes)
    current_macd = macd(closes)

    avg_volume = sum(volumes[-20:]) / 20
    volume_spike = volumes[-1] > avg_volume * 1.5

    score = 0

    if ema20 is not None and current_price > ema20:
        score += 15
    else:
        score -= 15

    if ema20 is not None and ema50 is not None and ema20 > ema50:
        score += 20
        trend = "UP"
    else:
        score -= 20
        trend = "DOWN"

    if current_rsi is not None:
        if current_rsi < 30:
            score += 15
        elif current_rsi > 70:
            score -= 15

    if current_macd > 0:
        score += 15
    else:
        score -= 15

    if volume_spike:
        score += 10

    return {
        "score": score,
        "trend": trend,
        "rsi": round(current_rsi, 2) if current_rsi is not None else None,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "macd": round(current_macd, 2),
        "volume_spike": volume_spike,
    }


def parse_rss(url):
    response = safe_get(url)

    if not response:
        return []

    news = []

    try:
        root = ET.fromstring(response.content)

        for item in root.findall(".//item")[:20]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")

            if title:
                news.append(
                    {
                        "title": title,
                        "link": link,
                    }
                )

    except Exception as error:
        print(f"[WARN] RSS parse error: {error}")

    return news


def get_crypto_news():
    news = []

    for url in CRYPTO_RSS:
        news.extend(parse_rss(url))

    if CRYPTOPANIC_KEY:
        cryptopanic_url = (
            "https://cryptopanic.com/api/v1/posts/"
            f"?auth_token={CRYPTOPANIC_KEY}&currencies=BTC&filter=hot"
        )

        response = safe_get(cryptopanic_url)

        if response:
            try:
                data = response.json()

                for item in data.get("results", [])[:20]:
                    title = item.get("title", "")
                    link = item.get("url", "")

                    if title:
                        news.append(
                            {
                                "title": title,
                                "link": link,
                            }
                        )

            except Exception as error:
                print(f"[WARN] CryptoPanic error: {error}")

    return news


def get_oil_news():
    news = []

    for url in OIL_RSS:
        news.extend(parse_rss(url))

    return news


def get_forex_factory_events():
    url = "https://www.forexfactory.com/calendar"
    response = safe_get(url)

    if not response:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    keywords = [
        "CPI",
        "FOMC",
        "Interest Rate",
        "NFP",
        "Powell",
        "Inflation",
        "Fed",
    ]

    events = []

    for keyword in keywords:
        if keyword.lower() in text.lower():
            events.append(keyword)

    return events


def analyze_news(news):
    bullish = 0
    bearish = 0
    impact = 0
    important = []

    for item in news:
        title = item["title"].lower()

        if any(word in title for word in BULLISH):
            bullish += 1

        if any(word in title for word in BEARISH):
            bearish += 1

        if any(word in title for word in HIGH_IMPACT):
            impact += 1
            important.append(item["title"])

    score = bullish * 6
    score -= bearish * 6
    score += impact * 10

    if bullish > bearish:
        sentiment = "BULLISH"
    elif bearish > bullish:
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    return {
        "score": score,
        "sentiment": sentiment,
        "bullish": bullish,
        "bearish": bearish,
        "impact": impact,
        "important": important[:5],
        "total": len(news),
    }


def build_signal(tech, news, funding, forex_events):
    score = 0

    score += tech["score"]
    score += news["score"]

    if funding > 0.0008:
        score -= 10

    if funding < -0.0005:
        score += 10

    if forex_events:
        score += 15

    if score >= 55:
        signal = "LONG"
    elif score <= -55:
        signal = "SHORT"
    else:
        signal = "NO SIGNAL"

    confidence = min(95, abs(score))

    return signal, score, confidence


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
    print("START BZU ULTRA BOT")

    ticker = get_okx_price()

    if not ticker:
        print("OKX ERROR")
        return

    funding = get_okx_funding()
    candles = get_okx_candles()

    crypto_news = get_crypto_news()
    oil_news = get_oil_news()
    all_news = crypto_news + oil_news

    forex_events = get_forex_factory_events()

    tech = analyze_technical(candles)
    news = analyze_news(all_news)

    signal, score, confidence = build_signal(
        tech=tech,
        news=news,
        funding=funding,
        forex_events=forex_events,
    )

    print(f"OKX PRICE: {ticker['last']}")
    print(f"TECH SCORE: {tech['score']}")
    print(f"NEWS SCORE: {news['score']}")
    print(f"FINAL SCORE: {score}")
    print(f"SIGNAL: {signal}")

    if signal == "NO SIGNAL":
        print("NO SIGNAL")
        return

    icon = "LONG" if signal == "LONG" else "SHORT"

    message = f"""
<b>BZU SIGNAL BOT ULTRA</b>

<b>Signal:</b> {icon}
<b>Confidence:</b> {confidence}%

<b>OKX Price:</b> {ticker['last']}

<b>Trend:</b> {tech['trend']}
<b>RSI:</b> {tech['rsi']}
<b>MACD:</b> {tech['macd']}
<b>EMA20:</b> {tech['ema20']}
<b>EMA50:</b> {tech['ema50']}
<b>Volume spike:</b> {tech['volume_spike']}

<b>Funding:</b> {funding}

<b>News Sentiment:</b> {news['sentiment']}
<b>Bullish:</b> {news['bullish']}
<b>Bearish:</b> {news['bearish']}
<b>Impact:</b> {news['impact']}
<b>Total news:</b> {news['total']}

<b>Forex Factory:</b>
{', '.join(forex_events) if forex_events else 'None'}

<b>Important News:</b>
"""

    for title in news["important"]:
        message += f"\n- {title}"

    send_telegram(message.strip())

    print("TELEGRAM SENT")
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
