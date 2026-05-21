"""
BZU Signal Bot - NEWS-PRIORITY ULTRA (RSS, no API key needed)
"""

import requests
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Tuple, Dict, Optional, List

TELEGRAM_TOKEN   = "8801978809:AAEtCm3xzMHq5o6NPAkK6h9dy6PJByg-CvI"
TELEGRAM_CHAT_ID = "832100232"
INSTRUMENT  = "BZ-USDT-SWAP"
BAR         = "15m"
BALANCE     = float(os.environ.get("BALANCE", "5"))
LEVERAGE    = int(os.environ.get("LEVERAGE", "20"))
RISK_PCT    = 0.20
SL_PCT      = 0.018
TP1_PCT     = 0.030
TP2_PCT     = 0.055

OIL_KEYWORDS = {
    "bullish": [
        "production cut", "opec cuts", "supply disruption", "sanctions",
        "geopolitical risk", "conflict", "embargo", "refinery outage",
        "pipeline attack", "war", "iran", "russia sanctions",
        "demand surge", "supply shock", "inventory drop",
        "geopolitical tensions", "saudi arabia", "supply tightness",
        "shortage", "tight market", "support",
    ],
    "bearish": [
        "production increase", "opec increases", "abundant supply", "oversupply",
        "price collapse", "shale boom", "surplus", "high inventory",
        "recession", "demand destruction", "economic slowdown",
        "demand weakness", "supply glut", "inventory build",
        "weak demand", "eia increase", "excess capacity",
    ]
}

RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.oilprice.com/rss/main",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.cnn.com/rss/money_news_international.rss",
]

# ═══════════════════════════════════════════════════════════════
# 1 - NEWS (RSS, free, real-time)
# ═══════════════════════════════════════════════════════════════

def get_last_hour_news() -> List[Dict]:
    all_articles = []
    now = datetime.now(timezone.utc)
    for feed_url in RSS_FEEDS:
        try:
            r = requests.get(feed_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = item.findtext("title") or ""
                description = item.findtext("description") or ""
                pub_date = item.findtext("pubDate") or ""
                link = item.findtext("link") or ""
                text = f"{title} {description}".lower()
                oil_related = any(kw in text for kw in [
                    "oil", "crude", "opec", "brent", "wti", "energy",
                    "petroleum", "barrel", "refinery", "iran", "saudi"
                ])
                if not oil_related:
                    continue
                try:
                    pub_time = parsedate_to_datetime(pub_date)
                    age_hours = (now - pub_time).total_seconds() / 3600
                    if age_hours > 24:
                        continue
                except Exception:
                    pass
                all_articles.append({
                    "title": title,
                    "description": description,
                    "publishedAt": pub_date,
                    "url": link,
                    "source": {"name": feed_url.split("/")[2]}
                })
        except Exception as e:
            print(f"[WARN] RSS {feed_url}: {e}")
    print(f"  RSS: {len(all_articles)} oil news found")
    return all_articles[:40]


def simple_nlp_sentiment(text: str) -> float:
    text = text.lower()
    strong_bullish = ["surge", "soar", "spike", "rally", "jump", "gain", "explode", "positive", "strong", "bullish", "buy"]
    strong_bearish = ["crash", "plunge", "collapse", "tank", "nosedive", "negative", "weak", "bearish", "sell"]
    bullish_words  = ["up", "rise", "increase", "support", "cut", "sanctions", "disruption", "embargo", "shortage", "tight", "geopolitical"]
    bearish_words  = ["fall", "drop", "decline", "down", "lower", "excess", "slowdown", "recession"]
    sb = sum(1 for w in strong_bullish if w in text)
    sw = sum(1 for w in strong_bearish if w in text)
    b  = sum(1 for w in bullish_words  if w in text)
    s  = sum(1 for w in bearish_words  if w in text)
    total_score = (sb * 2 + b) - (sw * 2 + s)
    total_count = (sb + sw) * 2 + b + s
    if total_count == 0:
        return 0.0
    return max(-1.0, min(1.0, total_score / total_count))


def analyze_news_sentiment(articles: List[Dict]) -> Dict:
    if not articles:
        return {
            "sentiment": "neutral", "score": 0,
            "bullish_count": 0, "bearish_count": 0,
            "top_news": [], "strength": "weak", "total_news": 0
        }
    bullish_articles = []
    bearish_articles = []
    for article in articles:
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        text = f"{title} {description}"
        nlp_score  = simple_nlp_sentiment(text)
        bullish_kw = sum(1 for kw in OIL_KEYWORDS["bullish"] if kw in text)
        bearish_kw = sum(1 for kw in OIL_KEYWORDS["bearish"] if kw in text)
        combined   = nlp_score + (bullish_kw - bearish_kw) * 0.15
        is_breaking = False
        try:
            pub_time = parsedate_to_datetime(article.get("publishedAt", ""))
            age_minutes = (datetime.now(timezone.utc) - pub_time).total_seconds() / 60
            is_breaking = age_minutes < 30
        except Exception:
            pass
        entry = {
            "title": article.get("title"),
            "source": article.get("source", {}).get("name"),
            "published_at": article.get("publishedAt"),
            "score": abs(combined),
            "breaking": is_breaking
        }
        if combined > 0.05:
            bullish_articles.append(entry)
        elif combined < -0.05:
            bearish_articles.append(entry)
    bullish_count = len(bullish_articles)
    bearish_count = len(bearish_articles)
    total = bullish_count + bearish_count
    if bullish_count > bearish_count * 2.5:
        sentiment = "bullish"
        strength  = "strong" if bullish_count >= 6 else "moderate"
    elif bearish_count > bullish_count * 2.5:
        sentiment = "bearish"
        strength  = "strong" if bearish_count >= 6 else "moderate"
    else:
        sentiment = "neutral"
        strength  = "weak"
    top_bullish = sorted(bullish_articles, key=lambda x: (x["breaking"], x["score"]), reverse=True)[:2]
    top_bearish = sorted(bearish_articles, key=lambda x: (x["breaking"], x["score"]), reverse=True)[:2]
    return {
        "sentiment": sentiment, "strength": strength,
        "score": bullish_count - bearish_count,
        "bullish_count": bullish_count, "bearish_count": bearish_count,
        "total_news": total, "top_news": (top_bullish + top_bearish)[:4]
    }

# ═══════════════════════════════════════════════════════════════
# 2 - MACRO CALENDAR
# ═══════════════════════════════════════════════════════════════

def check_macro_events() -> Dict:
    now = datetime.utcnow()
    events = []
    warnings = []
    if now.weekday() == 2 and 15 <= now.hour <= 16:
        events.append({"name": "EIA Crude Oil Inventory", "impact": "CRITICAL", "time": "15:30 UTC"})
    if now.weekday() == 1 and 22 <= now.hour <= 23:
        events.append({"name": "API Petroleum Status", "impact": "HIGH", "time": "22:30 UTC"})
    if now.day == 15:
        events.append({"name": "OPEC Monthly Oil Report", "impact": "CRITICAL", "time": "12:00 UTC"})
    if now.weekday() == 1 and now.hour >= 12:
        warnings.append("EIA tomorrow at 15:30 UTC!")
    return {"events": events, "warnings": warnings, "upcoming": len(events) > 0, "high_impact": len(events) > 0}

# ═══════════════════════════════════════════════════════════════
# 3 - PRICES
# ═══════════════════════════════════════════════════════════════

def get_candles_okx() -> Tuple[List[float], List[float]]:
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": INSTRUMENT, "bar": BAR, "limit": "50"}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        candles = sorted(data, key=lambda x: int(x[0]))
        return [float(c[4]) for c in candles], [float(c[5]) for c in candles]
    except Exception as e:
        print(f"[ERROR] OKX: {e}")
        return [], []


def get_binance_price() -> Optional[float]:
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=BLUSDT", timeout=5)
        return float(r.json().get("price", 0))
    except Exception:
        return None


def get_multi_source_price() -> Dict:
    closes_okx, _ = get_candles_okx()
    binance_price  = get_binance_price()
    prices = {"okx": None, "binance": None, "consensus": None}
    if closes_okx:
        prices["okx"] = round(closes_okx[-1], 2)
    if binance_price:
        prices["binance"] = round(binance_price, 2)
    valid = [p for p in [prices["okx"], prices["binance"]] if p]
    if valid:
        prices["consensus"] = round(sum(valid) / len(valid), 2)
    return prices

# ═══════════════════════════════════════════════════════════════
# 4 - TECHNICAL ANALYSIS
# ═══════════════════════════════════════════════════════════════

def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return values
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def analyze_ta(closes: List[float], volumes: List[float]) -> Dict:
    if len(closes) < 30:
        return {"error": "Not enough data"}
    ef    = ema(closes, 9)[-1]
    es    = ema(closes, 21)[-1]
    price = closes[-1]
    rsi_v = rsi(closes, 14)
    vol_avg = sum(volumes[-20:]) / 20
    vol_cur = volumes[-1]
    return {
        "price": round(price, 2), "ema9": round(ef, 2), "ema21": round(es, 2),
        "rsi": rsi_v, "trend": "UP" if ef > es else "DOWN",
        "momentum": "STRONG" if abs(ef - es) / es > 0.005 else "WEAK",
        "vol_ratio": round(vol_cur / vol_avg, 2) if vol_avg > 0 else 1.0
    }

# ═══════════════════════════════════════════════════════════════
# 5 - STRENGTH SCORE
# ═══════════════════════════════════════════════════════════════

def calculate_strength_score(news_data: Dict, macro_data: Dict, ta_meta: Dict) -> float:
    score = 0.0
    details = []
    if news_data["sentiment"] == "bullish":
        ns = min(40, news_data["bullish_count"] * 6)
        score += ns; details.append(f"NEWS:{ns}")
    elif news_data["sentiment"] == "bearish":
        ns = min(40, news_data["bearish_count"] * 6)
        score += ns; details.append(f"NEWS:{ns}")
    if macro_data["high_impact"]:
        score += 30; details.append("MACRO:30")
    if ta_meta.get("trend") == "UP" and news_data["sentiment"] == "bullish":
        pts = 20 if ta_meta.get("momentum") == "STRONG" else 10
        score += pts; details.append(f"TA:{pts}")
    elif ta_meta.get("trend") == "DOWN" and news_data["sentiment"] == "bearish":
        pts = 20 if ta_meta.get("momentum") == "STRONG" else 10
        score += pts; details.append(f"TA:{pts}")
    for article in news_data.get("top_news", []):
        if article.get("breaking"):
            score += 10; details.append("BREAKING:10"); break
    print(f"  Score: {' + '.join(details) or '0'} = {min(100, score)}")
    return min(100, score)

# ═══════════════════════════════════════════════════════════════
# 6 - SIGNAL GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_signal(news_data: Dict, macro_data: Dict, ta_meta: Dict) -> Optional[str]:
    strength_score = calculate_strength_score(news_data, macro_data, ta_meta)
    if strength_score >= 70:
        if news_data["sentiment"] == "bullish": return "LONG"
        if news_data["sentiment"] == "bearish": return "SHORT"
    if strength_score >= 50:
        trend = ta_meta.get("trend", "?")
        if news_data["sentiment"] == "bullish" and trend == "UP": return "LONG"
        if news_data["sentiment"] == "bearish" and trend == "DOWN": return "SHORT"
    return None

# ═══════════════════════════════════════════════════════════════
# 7 - TELEGRAM
# ═══════════════════════════════════════════════════════════════

def send_signal(signal: str, news_data: Dict, macro_data: Dict, ta_meta: Dict, prices: Dict):
    price = prices.get("consensus") or ta_meta["price"]
    now   = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
    if signal == "SHORT":
        sl  = round(price * (1 + SL_PCT), 2)
        tp1 = round(price * (1 - TP1_PCT), 2)
        tp2 = round(price * (1 - TP2_PCT), 2)
        emoji, label = "🔴", "SHORT"
    else:
        sl  = round(price * (1 - SL_PCT), 2)
        tp1 = round(price * (1 + TP1_PCT), 2)
        tp2 = round(price * (1 + TP2_PCT), 2)
        emoji, label = "🟢", "LONG"
    margin   = round(BALANCE * RISK_PCT, 2)
    position = round(margin * LEVERAGE, 2)
    top_news_text = ""
    for i, a in enumerate(news_data.get("top_news", [])[:2], 1):
        brk = "🔥 " if a.get("breaking") else ""
        top_news_text += f"  {i}. {brk}{a['title'][:55]}...\n"
    macro_text = ""
    if macro_data["high_impact"]:
        for e in macro_data["events"]:
            macro_text += f"⚠️ {e['name']}\n"
    msg = (
        f"{emoji} *{label}*  |  `{INSTRUMENT}`\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Ціна: ${price}\n"
        f"🛡 SL: ${sl}  |  🎯 TP1: ${tp1}  |  🎯 TP2: ${tp2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 Новини: *{news_data['sentiment'].upper()}* ({news_data['bullish_count']}↑ {news_data['bearish_count']}↓)\n"
        f"{top_news_text}"
        f"📈 TA: {ta_meta['trend']} | EMA: {ta_meta['ema9']}/{ta_meta['ema21']} | RSI: {ta_meta['rsi']}\n"
        f"{macro_text}"
        f"💰 Позиція: ${position} (×{LEVERAGE})\n"
        f"⚠️ СТОП ОДРАЗУ!\n"
    )
    send_telegram(msg)
    print(f"✅ [SIGNAL] {signal} @ ${price}")


def send_status(news_data: Dict, macro_data: Dict, ta_meta: Dict, prices: Dict):
    now = datetime.now(timezone.utc)
    if now.minute > 2:
        return
    price = prices.get("consensus") or ta_meta.get("price", "—")
    msg = (
        f"⏳ Чекаємо сигналу | {now.strftime('%d.%m %H:%M UTC')}\n"
        f"💰 BZU: ${price}  |  {ta_meta.get('trend')}  |  RSI: {ta_meta.get('rsi')}\n"
        f"📰 Новини: {news_data['sentiment'].upper()} ({news_data['total_news']} новин)\n"
    )
    if macro_data["upcoming"]:
        msg += "⚠️ Макро события\n"
    send_telegram(msg)


def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            print("✅ Telegram OK")
        else:
            print(f"❌ Telegram HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"❌ Telegram: {e}")

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n🚀 [START] BZU Signal Bot ULTRA v2.0\n")

    print("📰 Новини (RSS, реальний час)...")
    articles  = get_last_hour_news()
    news_data = analyze_news_sentiment(articles)
    print(f"✅ Sentiment: {news_data['sentiment'].upper()} | {news_data['total_news']} новин")
    print(f"   Bullish: {news_data['bullish_count']} | Bearish: {news_data['bearish_count']}\n")

    print("📅 Макро-календар...")
    macro_data = check_macro_events()
    if macro_data["high_impact"]:
        for event in macro_data["events"]:
            print(f"   🔥 {event['name']}")
    print()

    print("💱 Ціни...")
    prices = get_multi_source_price()
    print(f"✅ Consensus: ${prices['consensus']}\n")

    print("📊 Технічний аналіз...")
    closes, volumes = get_candles_okx()
    if not closes:
        print("[ERROR] OKX даних немає")
        exit(1)
    ta_meta = analyze_ta(closes, volumes)
    print(f"✅ {ta_meta['trend']} | RSI: {ta_meta['rsi']}\n")

    print("🎯 Генеруємо сигнал...")
    signal = generate_signal(news_data, macro_data, ta_meta)

    if signal:
        print(f"\n✅✅✅ СИГНАЛ: {signal} ✅✅✅\n")
        send_signal(signal, news_data, macro_data, ta_meta, prices)
    else:
        print(f"\n⏳ Сигналу немає\n")
        send_status(news_data, macro_data, ta_meta, prices)

    print(f"\n✨ [END] Bot completed\n")
