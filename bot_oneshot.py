"""
BZU Signal Bot — NEWS-PRIORITY версія
Пріоритет: новини про нафту та геополітику
Технічний аналіз — допоміжний інструмент
Запускається кожні 15 хв через cron
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

# Новинні ключові слова про нафту — ПРІОРИТЕТ #1
OIL_KEYWORDS = {
    "bullish": [
        "production cut", "opec cuts", "supply disruption", "sanctions",
        "geopolitical risk", "conflict", "embargo", "refinery outage",
        "freeze", "pipeline attack", "war", "iran", "russia sanctions",
        "скорочення виробництва", "конфлікт", "санкції", "блокада",
        "中断", "衝突", "制裁"
    ],
    "bearish": [
        "production increase", "opec increases", "abundant supply", "oversupply",
        "price collapse", "shale boom", "surplus", "high inventory",
        "recession", "demand destruction", "economic slowdown",
        "збільшення виробництва", "надлишок", "рецесія",
        "增加", "過剩", "衰退"
    ]
}

# ─────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════
# 1️⃣  НОВИННИЙ АНАЛІЗ (PRIORITY #1)
# ══════════════════════════════════════════════════════════════

def get_last_hour_news() -> List[Dict]:
    """
    Завантажує новини за останню годину про нафту + геополітику
    Використовує NewsAPI та пошук за часом
    """
    api_key = os.environ.get("NEWSAPI_KEY", "")
    if not api_key:
        print("[WARN] NEWSAPI_KEY не встановлено")
        return []

    url = "https://newsapi.org/v2/everything"
    now = datetime.utcnow()
    from_date = (now - timedelta(hours=1)).isoformat() + "Z"  # Тільки за останню годину
    
    queries = [
        "crude oil price",
        "OPEC production",
        "WTI Brent",
        "Iran sanctions",
        "Russia oil embargo",
        "Middle East conflict",
        "Refinery outage",
        "Oil supply disruption"
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
    
    return unique[:20]  # Топ 20 унікальних за годину


def analyze_news_sentiment(articles: List[Dict]) -> Dict:
    """
    Аналізує новини за останню годину
    Повертає: sentiment (bullish/bearish), score, топ новини
    ПРІОРИТЕТ: новини > технічний аналіз
    """
    if not articles:
        return {
            "sentiment": "neutral",
            "score": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "top_news": [],
            "strength": "weak"
        }
    
    bullish_articles = []
    bearish_articles = []
    
    for article in articles:
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        content = (article.get("content") or "").lower()
        
        full_text = f"{title} {description} {content}"
        
        bullish_score = sum(1 for keyword in OIL_KEYWORDS["bullish"] if keyword in full_text)
        bearish_score = sum(1 for keyword in OIL_KEYWORDS["bearish"] if keyword in full_text)
        
        if bullish_score > bearish_score:
            bullish_articles.append({
                "title": article.get("title"),
                "source": article.get("source", {}).get("name"),
                "published_at": article.get("publishedAt"),
                "score": bullish_score - bearish_score
            })
        elif bearish_score > bullish_score:
            bearish_articles.append({
                "title": article.get("title"),
                "source": article.get("source", {}).get("name"),
                "published_at": article.get("publishedAt"),
                "score": bearish_score - bullish_score
            })
    
    bullish_count = len(bullish_articles)
    bearish_count = len(bearish_articles)
    total = bullish_count + bearish_count
    
    # Визначаємо sentiment та силу сигналу
    if bullish_count > bearish_count * 2:
        sentiment = "bullish"
        strength = "strong" if bullish_count >= 5 else "moderate"
    elif bearish_count > bullish_count * 2:
        sentiment = "bearish"
        strength = "strong" if bearish_count >= 5 else "moderate"
    else:
        sentiment = "neutral"
        strength = "weak"
    
    # Топ новини (сортовані за score)
    top_bullish = sorted(bullish_articles, key=lambda x: x["score"], reverse=True)[:3]
    top_bearish = sorted(bearish_articles, key=lambda x: x["score"], reverse=True)[:3]
    top_news = top_bullish + top_bearish
    
    return {
        "sentiment": sentiment,
        "strength": strength,
        "score": bullish_count - bearish_count,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "total_news": total,
        "top_news": top_news[:5]
    }


# ══════════════════════════════════════════════════════════════
# 2️⃣  ТЕХНІЧНИЙ АНАЛІЗ (допоміжний)
# ══════════════════════════════════════════════════════════════

def get_candles() -> Tuple[List[float], List[float]]:
    """Завантажує 50 свічок 15m для BZU з OKX"""
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
        print(f"[ERROR] get_candles: {e}")
        return [], []


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
    """Технічний аналіз (допоміжний)"""
    if len(closes) < 30:
        return {"error": "Недостатньо даних"}

    ef    = ema(closes, 9)[-1]
    es    = ema(closes, 21)[-1]
    price = closes[-1]
    rsi_v = rsi(closes, 14)
    
    vol_avg = sum(volumes[-20:]) / 20
    vol_cur = volumes[-1]
    
    trend = "UP" if ef > es else "DOWN"
    
    return {
        "price": round(price, 2),
        "ema9": round(ef, 2),
        "ema21": round(es, 2),
        "rsi": rsi_v,
        "trend": trend,
        "vol_ratio": round(vol_cur / vol_avg, 2) if vol_avg > 0 else 1.0
    }


# ══════════════════════════════════════════════════════════════
# 3️⃣  СИГНАЛІЗАЦІЯ (НОВИНИ = ПРІОРИТЕТ)
# ══════════════════════════════════════════════════════════════

def generate_signal(news_sentiment: str, strength: str, ta_meta: Dict) -> Optional[str]:
    """
    Генерує сигнал на основі НОВИН!
    TA тільки для фільтрації помилкових сигналів
    """
    
    # ПРІОРИТЕТ: якщо новини СИЛЬНІ → беремо сигнал
    if strength == "strong":
        if news_sentiment == "bullish":
            return "LONG"  # Купуй
        elif news_sentiment == "bearish":
            return "SHORT"  # Продавай
    
    # ПОМІРНА сила + тренд підтримує
    if strength == "moderate":
        trend = ta_meta.get("trend", "?")
        if news_sentiment == "bullish" and trend == "UP":
            return "LONG"
        elif news_sentiment == "bearish" and trend == "DOWN":
            return "SHORT"
    
    return None


def send_signal(signal: str, news_data: Dict, ta_meta: Dict):
    """Надсилає торговельний сигнал новинами + TA"""
    price = ta_meta["price"]
    now = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")

    if signal == "SHORT":
        sl  = round(price * (1 + SL_PCT), 2)
        tp1 = round(price * (1 - TP1_PCT), 2)
        tp2 = round(price * (1 - TP2_PCT), 2)
        emoji = "🔴"
        label = "SHORT · Продавай"
    else:
        sl  = round(price * (1 - SL_PCT), 2)
        tp1 = round(price * (1 + TP1_PCT), 2)
        tp2 = round(price * (1 + TP2_PCT), 2)
        emoji = "🟢"
        label = "LONG · Купуй"

    margin   = round(BALANCE * RISK_PCT, 2)
    position = round(margin * LEVERAGE, 2)

    # Новинна обгрунтування
    top_news_text = ""
    if news_data.get("top_news"):
        top = news_data["top_news"][0]
        top_news_text = f"  📄 {top['title'][:70]}...\n  📰 {top['source']}\n"

    msg = (
        f"{emoji} *{label}*  |  `{INSTRUMENT}`\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 *Ціна входу:*      ${price}\n"
        f"🛡 *Stop Loss:*       ${sl}\n"
        f"🎯 *TP1 (50%):*       ${tp1}\n"
        f"🎯 *TP2 (50%):*       ${tp2}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 *СИГНАЛ НА ОСНОВІ НОВИН:*\n"
        f"  Sentiment: {news_data['sentiment'].upper()} ({news_data['strength'].upper()})\n"
        f"  Bullish новин: {news_data['bullish_count']} | Bearish: {news_data['bearish_count']}\n"
        f"{top_news_text}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Технічне підтвердження:*\n"
        f"  Ціна: ${ta_meta['price']}  Тренд: {ta_meta['trend']}\n"
        f"  EMA9: {ta_meta['ema9']} | EMA21: {ta_meta['ema21']}\n"
        f"  RSI: {ta_meta['rsi']}  Обсяг: ×{ta_meta['vol_ratio']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Позиція:*\n"
        f"  Маржа: ${margin} | Позиція: ${position}\n"
        f"  Плече: ×{LEVERAGE}\n"
        f"⚠️ *ВИСТАВЛЯЙ СТОП ОДРАЗУ!*\n"
    )

    send_telegram(msg)
    print(f"[SIGNAL] {signal} @ ${price} (NEWS: {news_data['sentiment'].upper()})")


def send_status(news_data: Dict, ta_meta: Dict):
    """Статус без сигналу — раз на годину"""
    now = datetime.now(timezone.utc)
    if now.minute > 2:
        return

    price = ta_meta.get("price", "—")
    trend = ta_meta.get("trend", "?")
    
    emoji = "📈" if trend == "UP" else "📉"
    news_emoji = "🟢" if news_data["sentiment"] == "bullish" else ("🔴" if news_data["sentiment"] == "bearish" else "⚪")
    
    msg = (
        f"⏳ *Чекаємо сигналу*  |  {now.strftime('%d.%m %H:%M UTC')}\n"
        f"💰 BZU: `${price}`\n"
        f"{emoji} Тренд: {trend}  RSI: {ta_meta.get('rsi', '—')}\n"
        f"{news_emoji} Новини за годину: {news_data['sentiment'].upper()}\n"
        f"📰 Bullish: {news_data['bullish_count']} | Bearish: {news_data['bearish_count']}"
    )
    
    send_telegram(msg)
    print(f"[STATUS] No signal @ ${price}")


def send_telegram(msg: str):
    """Надсилає повідомлення в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            print("[OK] Telegram message sent")
        else:
            print(f"[WARN] Telegram error: {r.status_code}")
    except Exception as e:
        print(f"[ERROR] send_telegram: {e}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"[START] BZU Signal Bot @ {datetime.now(timezone.utc).isoformat()}")
    print("[MODE] NEWS-PRIORITY (технічний аналіз — допоміжний)\n")
    
    # НОВИННИЙ АНАЛІЗ (ПРІОРИТЕТ #1)
    print("📰 Завантажуємо новини за останню годину...")
    articles = get_last_hour_news()
    news_data = analyze_news_sentiment(articles)
    print(f"[NEWS] Sentiment: {news_data['sentiment'].upper()} | Strength: {news_data['strength'].upper()}")
    print(f"[NEWS] Bullish: {news_data['bullish_count']} | Bearish: {news_data['bearish_count']}\n")
    
    # ТЕХНІЧНИЙ АНАЛІЗ (допоміжний)
    print("📊 Технічний аналіз (допоміжний)...")
    closes, volumes = get_candles()
    if not closes:
        print("[ERROR] Не вдалося завантажити дані")
        exit(1)
    
    ta_meta = analyze_ta(closes, volumes)
    print(f"[TA] Price: ${ta_meta['price']} | Trend: {ta_meta['trend']} | RSI: {ta_meta['rsi']}\n")
    
    # ГЕНЕРУЄМО СИГНАЛ НА ОСНОВІ НОВИН
    signal = generate_signal(news_data["sentiment"], news_data["strength"], ta_meta)
    
    if signal:
        print(f"✅ СИГНАЛ ЗГЕНЕРОВАНИЙ: {signal}\n")
        send_signal(signal, news_data, ta_meta)
    else:
        print("❌ Сигналу немає (новини нейтральні або слабкі)\n")
        send_status(news_data, ta_meta)
    
    print(f"[END] BZU Signal Bot completed @ {datetime.now(timezone.utc).isoformat()}")
