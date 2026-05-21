"""
BZU Signal Bot — NEWS-PRIORITY ULTRA версія (БЕЗ TWITTER API)
МАКСИМАЛЬНА ПОТУЖНІСТЬ:
- NewsAPI (новини за 1 годину)
- EIA/OPEC макро-календар (HIGH IMPACT!)
- NLP аналіз sentiment (глибокий аналіз тексту)
- Multi-source цін (OKX + Binance)
- Strength Score (0-100)
"""

import requests
import os
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Optional, List

# ─── НАЛАШТУВАННЯ ────────────────────────────────────────────
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

# Новинні ключові слова про нафту — РОЗШИРЕНІ
OIL_KEYWORDS = {
    "bullish": [
        "production cut", "opec cuts", "supply disruption", "sanctions",
        "geopolitical risk", "conflict", "embargo", "refinery outage",
        "pipeline attack", "war", "iran", "russia sanctions",
        "demand surge", "supply shock", "inventory drop",
        "geopolitical tensions", "saudi arabia", "supply tightness",
        "shortage", "tight market", "support",
        "скорочення виробництва", "конфлікт", "санкції", "блокада",
        "中断", "衝突", "制裁", "供給"
    ],
    "bearish": [
        "production increase", "opec increases", "abundant supply", "oversupply",
        "price collapse", "shale boom", "surplus", "high inventory",
        "recession", "demand destruction", "economic slowdown",
        "demand weakness", "supply glut", "inventory build",
        "weak demand", "eia increase", "excess capacity",
        "збільшення виробництва", "надлишок", "рецесія",
        "増加", "過剰", "衰退"
    ]
}

# ═══════════════════════════════════════════════════════════════
# 1️⃣  НОВИННИЙ АНАЛІЗ (РОЗШИРЕНИЙ)
# ═══════════════════════════════════════════════════════════════

def get_last_hour_news() -> List[Dict]:
    """Завантажує новини за останню годину"""
    api_key = os.environ.get("NEWSAPI_KEY", "")
    if not api_key:
        print("[WARN] NEWSAPI_KEY не встановлено")
        return []

    url = "https://newsapi.org/v2/everything"
    now = datetime.utcnow()
    from_date = (now - timedelta(hours=1)).isoformat() + "Z"
    
    # РОЗШИРЕНІ ЗАПИТИ
    queries = [
        "crude oil price",
        "OPEC production",
        "WTI Brent oil",
        "Iran oil sanctions",
        "Russia oil embargo",
        "Middle East conflict",
        "Refinery outage",
        "Oil supply disruption",
        "Energy crisis",
        "Geopolitical tensions",
        "Saudi Arabia oil",
        "US shale production",
        "EIA inventory report",
        "Oil demand",
        "OPEC news"
    ]
    
    all_articles = []
    
    for query in queries:
        try:
            r = requests.get(url, params={
                "q": query,
                "from": from_date,
                "sortBy": "publishedAt",
                "language": "en",
                "apiKey": api_key,
                "pageSize": 5
            }, timeout=8)
            
            data = r.json()
            if data.get("status") == "ok":
                all_articles.extend(data.get("articles", []))
        except Exception as e:
            print(f"[ERROR] NewsAPI query '{query}': {e}")
    
    # Видаляємо дублікати за URL
    seen = set()
    unique = []
    for article in all_articles:
        url_key = article.get("url")
        if url_key not in seen:
            seen.add(url_key)
            unique.append(article)
    
    return unique[:40]


def simple_nlp_sentiment(text: str) -> float:
    """
    NLP sentiment аналіз (без бібліотек)
    Повертає: -1.0 (bearish) ... 0.0 (neutral) ... +1.0 (bullish)
    """
    text = text.lower()
    
    # Посилення слів (2x вплив)
    strong_bullish = [
        "surge", "soar", "spike", "rally", "jump", "gain", "explode",
        "positive", "strong", "bullish", "buy", "bullish trend"
    ]
    
    strong_bearish = [
        "crash", "plunge", "collapse", "tank", "nosedive",
        "negative", "weak", "bearish", "sell", "bearish trend"
    ]
    
    # Нормальні слова
    bullish_words = [
        "up", "rise", "increase", "support",
        "cut", "sanctions", "disruption", "embargo",
        "shortage", "tight", "risk premium", "geopolitical"
    ]
    
    bearish_words = [
        "fall", "drop", "decline", "down", "lower",
        "production up", "inventory up", "excess",
        "slowdown", "recession", "weak"
    ]
    
    # Розраховуємо score
    strong_bullish_count = sum(1 for word in strong_bullish if word in text)
    strong_bearish_count = sum(1 for word in strong_bearish if word in text)
    bullish_count = sum(1 for word in bullish_words if word in text)
    bearish_count = sum(1 for word in bearish_words if word in text)
    
    total_score = (strong_bullish_count * 2 + bullish_count) - (strong_bearish_count * 2 + bearish_count)
    total_count = (strong_bullish_count + strong_bearish_count) * 2 + bullish_count + bearish_count
    
    if total_count == 0:
        return 0.0
    
    normalized = total_score / total_count
    return max(-1.0, min(1.0, normalized))


def analyze_news_sentiment(articles: List[Dict]) -> Dict:
    """Аналізує новини з NLP sentiment"""
    if not articles:
        return {
            "sentiment": "neutral",
            "score": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "top_news": [],
            "strength": "weak",
            "news_count": 0
        }
    
    bullish_articles = []
    bearish_articles = []
    
    for article in articles:
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        text = f"{title} {description}"
        
        # NLP sentiment (основне)
        nlp_score = simple_nlp_sentiment(text)
        
        # Ключові слова (підтвердження)
        bullish_kw = sum(1 for keyword in OIL_KEYWORDS["bullish"] if keyword in text)
        bearish_kw = sum(1 for keyword in OIL_KEYWORDS["bearish"] if keyword in text)
        
        # Комбінований score
        combined_score = nlp_score + (bullish_kw - bearish_kw) * 0.15
        
        # Breaking News Detector (< 5 хвилин)
        is_breaking = False
        try:
            pub_time = datetime.fromisoformat(article.get("publishedAt", "").replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - pub_time).total_seconds() / 60
            is_breaking = age_minutes < 5
        except:
            pass
        
        # Сортуємо
        if combined_score > 0.05:
            bullish_articles.append({
                "title": article.get("title"),
                "source": article.get("source", {}).get("name"),
                "published_at": article.get("publishedAt"),
                "score": combined_score,
                "breaking": is_breaking
            })
        elif combined_score < -0.05:
            bearish_articles.append({
                "title": article.get("title"),
                "source": article.get("source", {}).get("name"),
                "published_at": article.get("publishedAt"),
                "score": abs(combined_score),
                "breaking": is_breaking
            })
    
    bullish_count = len(bullish_articles)
    bearish_count = len(bearish_articles)
    total = bullish_count + bearish_count
    
    # Визначаємо sentiment та силу
    if bullish_count > bearish_count * 2.5:
        sentiment = "bullish"
        strength = "strong" if bullish_count >= 6 else "moderate"
    elif bearish_count > bullish_count * 2.5:
        sentiment = "bearish"
        strength = "strong" if bearish_count >= 6 else "moderate"
    else:
        sentiment = "neutral"
        strength = "weak"
    
    # Топ новини
    top_bullish = sorted(bullish_articles, key=lambda x: (x["breaking"], x["score"]), reverse=True)[:2]
    top_bearish = sorted(bearish_articles, key=lambda x: (x["breaking"], x["score"]), reverse=True)[:2]
    top_news = top_bullish + top_bearish
    
    return {
        "sentiment": sentiment,
        "strength": strength,
        "score": bullish_count - bearish_count,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "total_news": total,
        "top_news": top_news[:4]
    }


# ═══════════════════════════════════════════════════════════════
# 2️⃣  МАКРО КАЛЕНДАР (EIA, OPEC)
# ═══════════════════════════════════════════════════════════════

def check_macro_events() -> Dict:
    """
    Перевіряє макроекономічні события за останню годину
    VERY HIGH IMPACT!
    """
    
    now = datetime.utcnow()
    
    events = []
    warnings = []
    
    # EIA Crude Oil Inventory (КОЖНУ СЕРЕДУ О 15:30 UTC)
    if now.weekday() == 2 and 15 <= now.hour <= 16:
        events.append({
            "name": "🔥 EIA Crude Oil Inventory",
            "impact": "CRITICAL",
            "time": "15:30 UTC (середа)"
        })
    
    # API Petroleum (ВІВТОРОК О 22:30 UTC)
    if now.weekday() == 1 and 22 <= now.hour <= 23:
        events.append({
            "name": "API Petroleum Status",
            "impact": "HIGH",
            "time": "22:30 UTC (вівторок)"
        })
    
    # OPEC Monthly (15-Е ЧИСЛО О 12:00 UTC)
    if now.day == 15:
        events.append({
            "name": "📊 OPEC Monthly Oil Report",
            "impact": "CRITICAL",
            "time": "12:00 UTC (15-го)"
        })
    
    # Попередження
    if now.weekday() == 1 and now.hour >= 12:
        warnings.append("⚠️ EIA завтра о 15:30 UTC!")
    
    return {
        "events": events,
        "warnings": warnings,
        "upcoming": len(events) > 0,
        "high_impact": len(events) > 0
    }


# ═══════════════════════════════════════════════════════════════
# 3️⃣  MULTI-SOURCE ЦІНИ
# ═══════════════════════════════════════════════════════════════

def get_candles_okx() -> Tuple[List[float], List[float]]:
    """OKX цін"""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": INSTRUMENT, "bar": BAR, "limit": "50"}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        candles = sorted(data, key=lambda x: int(x[0]))
        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        return closes, volumes
    except Exception as e:
        print(f"[ERROR] OKX: {e}")
        return [], []


def get_binance_price() -> Optional[float]:
    """Binance ціна"""
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BLUSDT"
        r = requests.get(url, timeout=5)
        return float(r.json().get("price", 0))
    except:
        return None


def get_multi_source_price() -> Dict:
    """Консенсус ціна з кількох джерел"""
    closes_okx, _ = get_candles_okx()
    binance_price = get_binance_price()
    
    prices = {"okx": None, "binance": None, "consensus": None}
    
    if closes_okx:
        prices["okx"] = round(closes_okx[-1], 2)
    
    if binance_price:
        prices["binance"] = round(binance_price, 2)
    
    valid_prices = [p for p in [prices["okx"], prices["binance"]] if p]
    if valid_prices:
        prices["consensus"] = round(sum(valid_prices) / len(valid_prices), 2)
    
    return prices


# ═══════════════════════════════════════════════════════════════
# 4️⃣  ТЕХНІЧНИЙ АНАЛІЗ
# ═══════════════════════════════════════════════════════════════

def ema(values: List[float], period: int) -> List[float]:
    """EMA"""
    if len(values) < period:
        return values
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes: List[float], period: int = 14) -> float:
    """RSI"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def analyze_ta(closes: List[float], volumes: List[float]) -> Dict:
    """Технічний аналіз"""
    if len(closes) < 30:
        return {"error": "Недостатньо даних"}

    ef    = ema(closes, 9)[-1]
    es    = ema(closes, 21)[-1]
    price = closes[-1]
    rsi_v = rsi(closes, 14)
    
    vol_avg = sum(volumes[-20:]) / 20
    vol_cur = volumes[-1]
    
    trend = "UP" if ef > es else "DOWN"
    momentum = "STRONG" if abs(ef - es) / es > 0.005 else "WEAK"
    
    return {
        "price": round(price, 2),
        "ema9": round(ef, 2),
        "ema21": round(es, 2),
        "rsi": rsi_v,
        "trend": trend,
        "momentum": momentum,
        "vol_ratio": round(vol_cur / vol_avg, 2) if vol_avg > 0 else 1.0
    }


# ═══════════════════════════════════════════════════════════════
# 5️⃣  STRENGTH SCORE (0-100)
# ═══════════════════════════════════════════════════════════════

def calculate_strength_score(news_data: Dict, macro_data: Dict, ta_meta: Dict) -> float:
    """
    Комбінований strength score (0-100)
    > 70 = VERY STRONG сигнал
    50-70 = STRONG сигнал
    30-50 = MODERATE сигнал
    < 30 = WEAK (не беремо)
    """
    score = 0.0
    details = []
    
    # 1️⃣ NEWS (40%)
    if news_data["sentiment"] == "bullish":
        news_score = min(40, news_data["bullish_count"] * 6)
        score += news_score
        details.append(f"NEWS:{news_score}")
    elif news_data["sentiment"] == "bearish":
        news_score = min(40, news_data["bearish_count"] * 6)
        score += news_score
        details.append(f"NEWS:{news_score}")
    
    # 2️⃣ MACRO (30%)
    if macro_data["high_impact"]:
        score += 30
        details.append("MACRO:30")
    
    # 3️⃣ TA (20%)
    if ta_meta.get("trend") == "UP" and news_data["sentiment"] == "bullish":
        if ta_meta.get("momentum") == "STRONG":
            score += 20
            details.append("TA:20")
        else:
            score += 10
            details.append("TA:10")
    elif ta_meta.get("trend") == "DOWN" and news_data["sentiment"] == "bearish":
        if ta_meta.get("momentum") == "STRONG":
            score += 20
            details.append("TA:20")
        else:
            score += 10
            details.append("TA:10")
    
    # 4️⃣ BREAKING NEWS (10%)
    for article in news_data.get("top_news", []):
        if article.get("breaking"):
            score += 10
            details.append("BREAKING:10")
            break
    
    print(f"  📊 Score: {' + '.join(details)} = {min(100, score)}")
    
    return min(100, score)


# ═══════════════════════════════════════════════════════════════
# 6️⃣  ГЕНЕРАЦІЯ СИГНАЛУ
# ═══════════════════════════════════════════════════════════════

def generate_signal(news_data: Dict, macro_data: Dict, ta_meta: Dict) -> Optional[str]:
    """Генерує сигнал"""
    
    strength_score = calculate_strength_score(news_data, macro_data, ta_meta)
    
    if strength_score >= 70:
        if news_data["sentiment"] == "bullish":
            return "LONG"
        elif news_data["sentiment"] == "bearish":
            return "SHORT"
    
    if strength_score >= 50:
        trend = ta_meta.get("trend", "?")
        if news_data["sentiment"] == "bullish" and trend == "UP":
            return "LONG"
        elif news_data["sentiment"] == "bearish" and trend == "DOWN":
            return "SHORT"
    
    return None


# ═══════════════════════════════════════════════════════════════
# 7️⃣  TELEGRAM
# ════════════════════════���══════════════════════════════════════

def send_signal(signal: str, news_data: Dict, macro_data: Dict, ta_meta: Dict, prices: Dict):
    """Надсилає сигнал"""
    price = prices.get("consensus") or ta_meta["price"]
    now = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")

    if signal == "SHORT":
        sl  = round(price * (1 + SL_PCT), 2)
        tp1 = round(price * (1 - TP1_PCT), 2)
        tp2 = round(price * (1 - TP2_PCT), 2)
        emoji = "🔴"
        label = "SHORT"
    else:
        sl  = round(price * (1 - SL_PCT), 2)
        tp1 = round(price * (1 + TP1_PCT), 2)
        tp2 = round(price * (1 + TP2_PCT), 2)
        emoji = "🟢"
        label = "LONG"

    margin   = round(BALANCE * RISK_PCT, 2)
    position = round(margin * LEVERAGE, 2)

    top_news_text = ""
    if news_data.get("top_news"):
        for i, article in enumerate(news_data["top_news"][:2], 1):
            breaking = "🔥 " if article.get("breaking") else ""
            top_news_text += f"  {i}. {breaking}{article['title'][:55]}...\n"

    macro_text = ""
    if macro_data["high_impact"]:
        for event in macro_data["events"]:
            macro_text += f"⚠️ {event['name']}\n"

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
    """Статус (раз на годину)"""
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
        msg += f"⚠️ Макро события\n"
    
    send_telegram(msg)


def send_telegram(msg: str):
    """Надсилає в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            print("✅ Telegram OK")
    except Exception as e:
        print(f"❌ Telegram: {e}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n🚀 [START] BZU Signal Bot ULTRA v2.0\n")
    
    # 1️⃣ НОВИНИ
    print("📰 Новини (за 1 годину)...")
    articles = get_last_hour_news()
    news_data = analyze_news_sentiment(articles)
    print(f"✅ Sentiment: {news_data['sentiment'].upper()} | {news_data['total_news']} новин")
    print(f"   Bullish: {news_data['bullish_count']} | Bearish: {news_data['bearish_count']}\n")
    
    # 2️⃣ МАКРО
    print("📅 Макро-календар...")
    macro_data = check_macro_events()
    if macro_data["high_impact"]:
        print("⚠️ GAME-CHANGER СОБЫТИЯ:")
        for event in macro_data["events"]:
            print(f"   🔥 {event['name']}")
    print()
    
    # 3️⃣ ЦІНИ
    print("💱 Ціни...")
    prices = get_multi_source_price()
    print(f"✅ Consensus: ${prices['consensus']}\n")
    
    # 4️⃣ ТА
    print("📊 Технічний аналіз...")
    closes, volumes = get_candles_okx()
    if not closes:
        print("[ERROR] OKX даних немає")
        exit(1)
    
    ta_meta = analyze_ta(closes, volumes)
    print(f"✅ {ta_meta['trend']} | RSI: {ta_meta['rsi']}\n")
    
    # 5️⃣ СИГНАЛ
    print("🎯 Генеруємо сигнал...")
    signal = generate_signal(news_data, macro_data, ta_meta)
    
    if signal:
        print(f"\n✅✅✅ СИГНАЛ: {signal} ✅✅✅\n")
        send_signal(signal, news_data, macro_data, ta_meta, prices)
    else:
        print(f"\n⏳ Сигналу немає\n")
        send_status(news_data, macro_data, ta_meta, prices)
    
    print(f"\n✨ [END] Bot completed\n")
