# 🛢️ BZU Signal Bot — NEWS-PRIORITY Edition

**Торговельний бот для ф'ючерсів на нафту (BZ-USDT-SWAP)**, який працює на основі **новин як основного сигналу**, з технічним аналізом як допоміжним інструментом.

## 🎯 Архітектура

### 1️⃣ **НОВИННИЙ АНАЛІЗ (ПРІОРИТЕТ)**
- ✅ Монітор новин за **останню годину**
- ✅ Шукає ключові слова про нафту, ОПЕК, геополітику
- ✅ Класифікує як **bullish** або **bearish**
- ✅ Оцінює силу сигналу: `strong` / `moderate` / `weak`

**Bullish новини:**
- `production cut`, `OPEC cuts`, `supply disruption`
- `sanctions`, `embargo`, `refinery outage`
- `conflict`, `geopolitical risk`

**Bearish новини:**
- `production increase`, `OPEC increases`, `oversupply`
- `price collapse`, `recession`, `demand destruction`

### 2️⃣ **ТЕХНІЧНИЙ АНАЛІЗ (Допоміжний)**
- EMA crossover (9/21)
- RSI (35-60 зона)
- Тренд аналіз
- Обсяг

**Використання:** Фільтрація помилкових новинних сигналів

### 3️⃣ **ГЕНЕРАЦІЯ СИГНАЛУ**

```python
if strength == "strong":
    if news_sentiment == "bullish" → LONG (Купуй)
    if news_sentiment == "bearish" → SHORT (Продавай)

if strength == "moderate" AND trend підтримує → LONG/SHORT

if strength == "weak" → Немає сигналу
