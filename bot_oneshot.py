import os
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_KEY", "")

TRADINGVIEW_SYMBOLS = [
    ("BINANCE:BZUSDT.P", "crypto"),
    ("BINANCE:BZUSDT", "crypto"),
    ("NYMEX:BZ1!", "futures"),
    ("TVC:UKOIL", "cfd"),
]

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
    "etf", "approval", "buy", "bullish", "surge", "rally",
    "breakout", "institutional", "accumulation", "rate cut",
    "fed pause", "inventory draw", "crude draw", "supply disruption",
    "opec cut", "sanctions",
]

BEARISH = [
    "crash", "sell", "bearish", "lawsuit", "hack", "liquidation",
    "war", "inflation", "rate hike", "recession", "inventory build",
    "crude build", "demand weak", "opec increase",
]

HIGH_IMPACT = [
    "fed", "cpi", "fomc", "etf", "sec", "blackrock", "war",
    "opec", "inventory", "eia", "api", "powell", "inflation",
]


def safe_get(url, timeout=15):
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code >= 400:
            print(f"[WARN] HTTP {r.status_code}: {url}")
            print(r.text[:300])
            return None

        return r

    except Exception as e:
        print(f"[WARN] {url}: {e}")
        return None


def safe_post(url, payload, timeout=15):
    try:
        r = requests.post(
            url,
            json=payload,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        if r.status_code >= 400:
            print(f"[WARN] HTTP {r.status_code}: {url}")
            print(r.text[:300])
            return None

        return r

    except Exception as e:
        print(f"[WARN] {url}: {e}")
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

        except Exception as e:
            print(f"[WARN] TradingView parse error for {symbol}: {e}")

    return None


def analyze_technical(tv):
    price = tv["price"]
    ema20 = tv["ema20"]
    ema50 = tv["ema50"]
    rsi = tv["rsi"]
    macd = tv["macd"]
    recommend = tv["recommend"]

    score = 0

    if recommend is not None:
        score += int(recommend * 40)

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
            score += 15
        elif rsi > 70:
            score -= 15

    if macd is not None:
        if macd > 0:
            score += 15
        else:
            score -= 15

    return {
        "score": score,
        "trend": trend,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "macd": round(macd, 4) if macd is not None else None,
        "recommend": recommend,
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
                news.append({
                    "title": title,
                    "link": link,
                })

    except Exception as e:
        print(f"[WARN] RSS parse error: {e}")

    return news


def get_crypto_news():
    news = []

    for url in CRYPTO_RSS:
        news.extend(parse_rss(url))

    if CRYPTOPANIC_KEY:
        url = (
            "https://cryptopanic.com/api/v1/posts/"
            f"?auth_token={CRYPTOPANIC_KEY}&filter=hot"
        )

        response = safe_get(url)

        if response:
            try:
                data = response.json()

                for item in data.get("results", [])[:20]:
                    title = item.get("title", "")
                    link = item.get("url", "")

                    if title:
                        news.append({
                            "title": title,
                            "link": link,
                        })

            except Exception as e:
                print(f"[WARN] CryptoPanic error: {e}")

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
        "Crude Oil Inventories",
        "EIA",
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


def build_signal(tech, news, forex_events):
    score = 0

    score += tech["score"]
    score += news["score"]

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

    except Exception as e:
        print(f"[WARN] Telegram error: {e}")


def main():
    print("START BZU TRADINGVIEW SIGNAL BOT")

    tv = get_tradingview_market_data()

    if not tv:
        print("TRADINGVIEW PRICE ERROR")
        return

    crypto_news = get_crypto_news()
    oil_news = get_oil_news()
    all_news = crypto_news + oil_news

    forex_events = get_forex_factory_events()

    tech = analyze_technical(tv)
    news = analyze_news(all_news)

    signal, score, confidence = build_signal(
        tech=tech,
        news=news,
        forex_events=forex_events,
    )

    print(f"SOURCE: {tv['source']}")
    print(f"SYMBOL: {tv['symbol']}")
    print(f"PRICE: {tv['price']}")
    print(f"CHANGE: {tv['change']}")
    print(f"TECH SCORE: {tech['score']}")
    print(f"NEWS SCORE: {news['score']}")
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

<b>Trend:</b> {tech['trend']}
<b>RSI 15m:</b> {tech['rsi']}
<b>MACD 15m:</b> {tech['macd']}
<b>EMA20 15m:</b> {tech['ema20']}
<b>EMA50 15m:</b> {tech['ema50']}
<b>TradingView Recommend 15m:</b> {tech['recommend']}

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
