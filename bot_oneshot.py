from __future__ import annotations

import argparse
import html
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

import requests


# ==========================================================
# BZU CLEAN ICT SIGNAL BOT
# ==========================================================
# One market snapshot -> one ICT evaluation -> one decision.
# There are no post-decision patch chains and no shadowed functions.
# ==========================================================

BOT_VERSION = "pro-ict-v3.2.1-full-3m-scan-telegram-clean"
ARCHITECTURE_VERSION = "DETERMINISTIC_PRO_ICT_CORE_V3_2_1_FULL_3M_SCAN"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")

WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", os.getcwd()))
STATE_FILE = Path(os.getenv("SIGNAL_MEMORY_FILE", str(WORKSPACE / "last_signal.json")))
JOURNAL_FILE = Path(os.getenv("SIGNAL_JOURNAL_FILE", str(WORKSPACE / "signal_journal.json")))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "120") or 120)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "1000") or 1000)
LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "5") or 5)
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "0") or 0)

ENTRY_SCORE = int(os.getenv("ICT_ENTRY_SCORE", "74") or 74)
RISKY_ENTRY_SCORE = int(os.getenv("ICT_RISKY_ENTRY_SCORE", "66") or 66)
ARMED_SCORE = int(os.getenv("ICT_ARMED_SCORE", "56") or 56)
DIRECTION_MARGIN = int(os.getenv("ICT_DIRECTION_MARGIN", "8") or 8)

NORMAL_RISK_PCT = float(os.getenv("NORMAL_RISK_PCT", "0.50") or 0.50)
RISKY_RISK_PCT = float(os.getenv("RISKY_RISK_PCT", "0.25") or 0.25)
MIN_STOP_ATR15 = float(os.getenv("MIN_STOP_ATR15", "0.48") or 0.48)
MAX_STOP_ATR15 = float(os.getenv("MAX_STOP_ATR15", "2.40") or 2.40)
MIN_TP1_ATR15 = float(os.getenv("MIN_TP1_ATR15", "0.80") or 0.80)
MIN_RR1 = float(os.getenv("MIN_RR1", "2.00") or 2.00)
PREFERRED_RR1 = float(os.getenv("PREFERRED_RR1", "2.00") or 2.00)
OPPORTUNITY_TTL_MIN = int(os.getenv("OPPORTUNITY_TTL_MIN", "1440") or 1440)
LATE_EXTENSION_ATR15 = float(os.getenv("LATE_EXTENSION_ATR15", "0.65") or 0.65)
EARLY_ENTRY_SCORE = int(os.getenv("ICT_EARLY_ENTRY_SCORE", "64") or 64)
STANDARD_ENTRY_SCORE = int(os.getenv("ICT_STANDARD_ENTRY_SCORE", "72") or 72)
TACTICAL_MIN_STOP_ATR15 = float(os.getenv("TACTICAL_MIN_STOP_ATR15", "0.55") or 0.55)
TACTICAL_MAX_STOP_ATR15 = float(os.getenv("TACTICAL_MAX_STOP_ATR15", "1.45") or 1.45)
REENTRY_PULLBACK_ATR15 = float(os.getenv("REENTRY_PULLBACK_ATR15", "0.35") or 0.35)
FULL_3M_SCAN_SEED_BARS = int(os.getenv("FULL_3M_SCAN_SEED_BARS", "40") or 40)
FULL_3M_TRIGGER_MAX_BARS = int(os.getenv("FULL_3M_TRIGGER_MAX_BARS", "10") or 10)

SEND_NO_SETUP = os.getenv("SEND_NO_SETUP", "false").strip().lower() in {"1", "true", "yes"}
SEND_DUPLICATE_STATUS = os.getenv("SEND_DUPLICATE_STATUS", "false").strip().lower() in {"1", "true", "yes"}


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class Action(str, Enum):
    ENTRY = "ENTRY"
    RISKY_ENTRY = "RISKY_ENTRY"
    ARMED = "ARMED"
    NO_SETUP = "NO_SETUP"
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    STOP = "STOP"
    EXIT = "EXIT"


class Regime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    SHOCK = "SHOCK"
    NORMAL = "NORMAL"


class SetupType(str, Enum):
    SWEEP_REVERSAL = "SWEEP_RECLAIM"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    RANGE_COMPRESSION_BREAKOUT = "RANGE_COMPRESSION_BREAKOUT"
    TREND_IGNITION = "TREND_IGNITION"
    CLOSED_15M_DIRECTION_FLIP = "CLOSED_15M_DIRECTION_FLIP"
    CAPITULATION_RECOVERY = "CAPITULATION_RECOVERY"
    FRESH_BASE_CONTINUATION_REENTRY = "FRESH_BASE_CONTINUATION_REENTRY"
    RANGE_EDGE_REVERSAL = "RANGE_EDGE_REVERSAL"
    NONE = "NONE"


SETUP_LABELS = {
    SetupType.SWEEP_REVERSAL.value: "Зняття ліквідності та повернення за рівень",
    SetupType.PULLBACK_CONTINUATION.value: "Продовження тренду після ICT-відкату",
    SetupType.BREAKOUT_RETEST.value: "Пробій структури та підтверджений ретест",
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: "Пробій після стиснення діапазону",
    SetupType.TREND_IGNITION.value: "Запуск нового тренду",
    SetupType.CLOSED_15M_DIRECTION_FLIP.value: "Підтверджена зміна напрямку на 15M",
    SetupType.CAPITULATION_RECOVERY.value: "Відновлення після капітуляційного руху",
    SetupType.FRESH_BASE_CONTINUATION_REENTRY.value: "Повторний вхід із нової 15M бази",
    SetupType.RANGE_EDGE_REVERSAL.value: "Розворот від межі діапазону",
    SetupType.NONE.value: "Чистого ICT-сетапу немає",
}

EARLY_SETUP_TYPES = {
    SetupType.SWEEP_REVERSAL.value,
    SetupType.CAPITULATION_RECOVERY.value,
    SetupType.TREND_IGNITION.value,
    SetupType.CLOSED_15M_DIRECTION_FLIP.value,
}
STANDARD_SETUP_TYPES = {
    SetupType.PULLBACK_CONTINUATION.value,
    SetupType.BREAKOUT_RETEST.value,
    SetupType.RANGE_COMPRESSION_BREAKOUT.value,
    SetupType.FRESH_BASE_CONTINUATION_REENTRY.value,
    SetupType.RANGE_EDGE_REVERSAL.value,
}



@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True


@dataclass
class Zone:
    kind: str
    side: str
    low: float
    high: float
    created_ts: int
    timeframe: str
    strength: float = 0.0
    mitigated: bool = False


@dataclass
class Candidate:
    side: str
    setup_type: str
    raw_score: int
    final_score: int
    score_components: dict[str, int]
    trigger_ready: bool
    trigger_level: float
    invalidation_level: float
    target_levels: list[float]
    confirmations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    hard_reject_reason: str = ""
    risk_mode: str = "NORMAL"
    late_extension_atr: float = 0.0
    execution_lane: str = "STANDARD"
    stage: str = "DISCOVERED"
    tactical_stop: bool = False


@dataclass
class TradePlan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    risk_pct: float
    rr1: float
    rr2: float
    rr3: float
    position_risk_pct: float
    position_size: float = 0.0
    notional: float = 0.0
    invalidation: str = ""
    stop_basis: str = ""
    target_basis: str = ""
    checkpoints: list[float] = field(default_factory=list)
    valid: bool = True
    reason: str = ""
    better_entry: float = 0.0


@dataclass
class Decision:
    id: str
    time: str
    action: str
    side: str
    setup_type: str
    quality: int
    reason: str
    regime: str
    candidate: Optional[Candidate] = None
    plan: Optional[TradePlan] = None
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class Opportunity:
    side: str
    setup_type: str
    created_at: str
    expires_at: str
    score: int
    trigger_level: float
    invalidation_level: float
    confirmations: list[str] = field(default_factory=list)
    status: str = "WAIT_PULLBACK"
    execution_lane: str = "REENTRY"
    optimal_entry: float = 0.0
    target_level: float = 0.0
    fingerprint: str = ""


@dataclass
class ActiveTrade:
    id: str
    side: str
    setup_type: str
    opened_at: str
    entry: float
    stop_initial: float
    stop_current: float
    structural_invalidation: float
    tp1: float
    tp2: float
    tp3: float
    quality: int
    position_risk_pct: float
    best_price: float
    worst_price: float
    last_checked_3m_ts: int = 0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    tp1_stop_locked: bool = False
    tp2_stop_locked: bool = False
    status: str = "OPEN"
    last_action: str = "ENTRY"
    notes: list[str] = field(default_factory=list)


# ==========================================================
# UTILITIES / STORAGE
# ==========================================================


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def round_price(value: Any) -> float:
    value = safe_float(value)
    if abs(value) >= 100:
        return round(value, 3)
    if abs(value) >= 10:
        return round(value, 4)
    return round(value, 5)


def pct(new: float, old: float) -> float:
    return ((new - old) / old * 100.0) if old else 0.0


def side_sign(side: str) -> int:
    return 1 if side == Side.LONG.value else -1


def opposite(side: str) -> str:
    return Side.SHORT.value if side == Side.LONG.value else Side.LONG.value


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    payload = json_safe(data)
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    with backup.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else default
    except Exception as exc:
        print(f"[WARN] JSON read failed {path}: {exc}")
        return default


def load_state() -> dict[str, Any]:
    raw = load_json(STATE_FILE, {})
    # Deliberately discard every legacy lock/gate/patch field. Only stable public
    # state and a possibly active trade are migrated into the clean model.
    same_architecture = raw.get("architecture_version") == ARCHITECTURE_VERSION
    return {
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "active_trade": raw.get("active_trade"),
        "opportunity": raw.get("opportunity") if same_architecture else None,
        "latest_signal": raw.get("latest_signal"),
        "last_message_key": raw.get("last_message_key", "") if same_architecture else "",
        "scan_3m": raw.get("scan_3m") if same_architecture else None,
        "history": list(raw.get("history") or [])[-MAX_HISTORY:],
    }


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    state["history"] = list(state.get("history") or [])[-MAX_HISTORY:]
    atomic_json_write(STATE_FILE, state)


def load_journal() -> dict[str, Any]:
    journal = load_json(JOURNAL_FILE, {})
    journal.setdefault("signals", [])
    journal.setdefault("trades", [])
    journal["version"] = BOT_VERSION
    journal["architecture_version"] = ARCHITECTURE_VERSION
    return journal


def save_journal(journal: dict[str, Any]) -> None:
    journal["updated_at"] = iso_now()
    journal["signals"] = list(journal.get("signals") or [])[-MAX_JOURNAL:]
    journal["trades"] = list(journal.get("trades") or [])[-MAX_JOURNAL:]
    journal["analytics"] = compute_analytics(journal)
    atomic_json_write(JOURNAL_FILE, journal)


def append_history(state: dict[str, Any], item: dict[str, Any]) -> None:
    payload = dict(item)
    payload.setdefault("time", iso_now())
    state.setdefault("history", []).append(payload)
    state["history"] = state["history"][-MAX_HISTORY:]


def active_trade_from_state(state: dict[str, Any]) -> Optional[ActiveTrade]:
    raw = state.get("active_trade")
    if not isinstance(raw, dict):
        return None
    try:
        fields = ActiveTrade.__dataclass_fields__
        clean = {k: raw[k] for k in fields if k in raw}
        # Conservative migration from the previous bot.
        clean.setdefault("setup_type", str(raw.get("setup_type") or "MIGRATED"))
        clean.setdefault("structural_invalidation", safe_float(raw.get("structural_stop") or raw.get("stop_initial") or raw.get("stop_current")))
        clean.setdefault("position_risk_pct", RISKY_RISK_PCT if str(raw.get("entry_level", "")).upper() == "RISKY_ENTRY" else NORMAL_RISK_PCT)
        clean.setdefault("best_price", safe_float(raw.get("best_price") or raw.get("entry")))
        clean.setdefault("worst_price", safe_float(raw.get("entry")))
        clean.setdefault("last_checked_3m_ts", 0)
        return ActiveTrade(**clean)
    except Exception as exc:
        print(f"[WARN] Active trade migration skipped: {exc}")
        return None


def store_active_trade(state: dict[str, Any], trade: Optional[ActiveTrade]) -> None:
    state["active_trade"] = asdict(trade) if trade else None


def opportunity_from_state(state: dict[str, Any]) -> Optional[Opportunity]:
    raw = state.get("opportunity")
    if not isinstance(raw, dict):
        return None
    try:
        return Opportunity(**{k: raw[k] for k in Opportunity.__dataclass_fields__ if k in raw})
    except Exception:
        return None


# ==========================================================
# HTTP / MARKET DATA — ONE DECISION SOURCE
# ==========================================================


def http_get(url: str, timeout: Optional[int] = None, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Clean-ICT/1.0"}
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout or REQUEST_TIMEOUT)
            if response.ok:
                return response
            print(f"[WARN] HTTP {response.status_code}: {url}")
        except Exception as exc:
            print(f"[WARN] HTTP GET failed ({attempt + 1}/{retries}): {exc}")
        if attempt + 1 < retries:
            time.sleep(0.5 * (attempt + 1))
    return None


def http_post(url: str, payload: dict[str, Any], timeout: Optional[int] = None) -> Optional[requests.Response]:
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"User-Agent": "Mozilla/5.0 BZU-Clean-ICT/1.0"},
            timeout=timeout or REQUEST_TIMEOUT,
        )
        return response if response.ok else None
    except Exception as exc:
        print(f"[WARN] HTTP POST failed: {exc}")
        return None


def parse_okx_candles(rows: Iterable[list[Any]]) -> list[Candle]:
    result: list[Candle] = []
    for row in rows or []:
        try:
            result.append(Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5] or 0),
                confirmed=(len(row) <= 8 or str(row[8]) == "1"),
            ))
        except Exception:
            continue
    return sorted(result, key=lambda c: c.ts)


def get_okx_candles(bar: str, limit: int) -> list[Candle]:
    url = f"https://www.okx.com/api/v5/market/candles?instId={OKX_INST_ID}&bar={bar}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        return parse_okx_candles(response.json().get("data", []))
    except Exception as exc:
        print(f"[WARN] Candle parse failed {bar}: {exc}")
        return []


def get_okx_ticker() -> dict[str, Any]:
    response = http_get(f"https://www.okx.com/api/v5/market/ticker?instId={OKX_INST_ID}")
    if not response:
        return {}
    try:
        row = (response.json().get("data") or [])[0]
        price = safe_float(row.get("last"))
        open24 = safe_float(row.get("open24h"))
        return {
            "price": price,
            "change24h": pct(price, open24) if open24 else 0.0,
            "volume24h": safe_float(row.get("volCcy24h")),
            "source": "OKX",
            "symbol": OKX_INST_ID,
        }
    except Exception as exc:
        print(f"[WARN] Ticker parse failed: {exc}")
        return {}


def get_okx_trades(limit: int = 200) -> list[dict[str, Any]]:
    response = http_get(f"https://www.okx.com/api/v5/market/trades?instId={OKX_INST_ID}&limit={limit}")
    if not response:
        return []
    try:
        return [
            {
                "price": safe_float(row.get("px")),
                "size": safe_float(row.get("sz")),
                "side": str(row.get("side") or "").lower(),
                "ts": int(row.get("ts") or 0),
            }
            for row in response.json().get("data", [])
        ]
    except Exception:
        return []


def get_okx_book(depth: int = 50) -> dict[str, list[tuple[float, float]]]:
    response = http_get(f"https://www.okx.com/api/v5/market/books?instId={OKX_INST_ID}&sz={depth}")
    if not response:
        return {"bids": [], "asks": []}
    try:
        row = (response.json().get("data") or [])[0]
        bids = [(safe_float(x[0]), safe_float(x[1])) for x in row.get("bids", []) if len(x) >= 2]
        asks = [(safe_float(x[0]), safe_float(x[1])) for x in row.get("asks", []) if len(x) >= 2]
        return {"bids": bids, "asks": asks}
    except Exception:
        return {"bids": [], "asks": []}


def get_okx_derivatives() -> dict[str, Any]:
    result: dict[str, Any] = {"oi": 0.0, "funding_rate": 0.0}
    oi = http_get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={OKX_INST_ID}", timeout=8, retries=1)
    funding = http_get(f"https://www.okx.com/api/v5/public/funding-rate?instId={OKX_INST_ID}", timeout=8, retries=1)
    try:
        result["oi"] = safe_float((oi.json().get("data") or [])[0].get("oi")) if oi else 0.0
    except Exception:
        pass
    try:
        result["funding_rate"] = safe_float((funding.json().get("data") or [])[0].get("fundingRate")) if funding else 0.0
    except Exception:
        pass
    return result


def get_tradingview_reference_price() -> dict[str, Any]:
    """Reference only. It never participates in stop/TP geometry."""
    payload = {
        "symbols": {"tickers": ["BINANCE:BZUSDT.P", "BINANCE:BZUSDT"], "query": {"types": []}},
        "columns": ["close", "change", "volume"],
    }
    response = http_post("https://scanner.tradingview.com/crypto/scan", payload)
    if not response:
        return {}
    try:
        row = (response.json().get("data") or [])[0]
        values = row.get("d") or []
        return {"price": safe_float(values[0]), "source": "TradingView", "symbol": row.get("s")}
    except Exception:
        return {}


def collect_market_data() -> dict[str, Any]:
    candles = {
        "3m": get_okx_candles("3m", 240),
        "15m": get_okx_candles("15m", 220),
        "1h": get_okx_candles("1H", 180),
        "4h": get_okx_candles("4H", 160),
    }
    ticker = get_okx_ticker()
    if not ticker and candles["15m"]:
        ticker = {"price": candles["15m"][-1].close, "source": "OKX candles", "symbol": OKX_INST_ID}
    tv = get_tradingview_reference_price()
    price = safe_float(ticker.get("price"))
    reference_price = safe_float(tv.get("price"))
    cross_diff = abs(reference_price - price) / price * 100 if price and reference_price else 0.0
    return {
        "candles": candles,
        "ticker": ticker,
        "reference": tv,
        "cross_price_diff_pct": round(cross_diff, 4),
        "trades": get_okx_trades(),
        "book": get_okx_book(),
        "derivatives": get_okx_derivatives(),
    }


# ==========================================================
# INDICATORS
# ==========================================================


def closed(candles: list[Candle]) -> list[Candle]:
    confirmed = [c for c in candles if c.confirmed]
    return confirmed if confirmed else list(candles)


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = float(values[0])
    for item in values[1:]:
        value = alpha * float(item) + (1.0 - alpha) * value
    return value


def rma(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) <= period:
        return mean(values)
    value = mean(values[:period])
    for item in values[period:]:
        value = (value * (period - 1) + item) / period
    return value


def atr(candles: list[Candle], period: int = 14) -> float:
    items = closed(candles)
    if len(items) < 2:
        return 0.0
    trs = []
    for i in range(1, len(items)):
        cur, prev = items[i], items[i - 1]
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return rma(trs[-max(period * 3, period):], period)


def rsi(candles: list[Candle], period: int = 14) -> float:
    items = closed(candles)
    closes = [c.close for c in items]
    if len(closes) < 2:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(v, 0.0) for v in changes]
    losses = [max(-v, 0.0) for v in changes]
    avg_gain = rma(gains[-max(period * 3, period):], period)
    avg_loss = rma(losses[-max(period * 3, period):], period)
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def efficiency_ratio(candles: list[Candle], lookback: int = 12) -> float:
    items = closed(candles)[-lookback:]
    if len(items) < 3:
        return 0.0
    net = abs(items[-1].close - items[0].open)
    path = sum(abs(items[i].close - items[i - 1].close) for i in range(1, len(items)))
    return net / path if path else 0.0


def volume_ratio(candles: list[Candle], short: int = 3, long: int = 20) -> float:
    items = closed(candles)
    if not items:
        return 1.0
    recent = mean([c.volume for c in items[-short:]]) if len(items) >= short else mean([c.volume for c in items])
    base = mean([c.volume for c in items[-long:]]) if len(items) >= long else mean([c.volume for c in items])
    return recent / base if base else 1.0

# ==========================================================
# MARKET STRUCTURE / ICT FEATURES
# ==========================================================


def pivot_points(candles: list[Candle], left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    items = closed(candles)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, len(items) - right):
        window = items[i - left:i + right + 1]
        if items[i].high == max(c.high for c in window):
            highs.append((i, items[i].high))
        if items[i].low == min(c.low for c in window):
            lows.append((i, items[i].low))
    return highs, lows


def structure_snapshot(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    items = closed(candles)
    if len(items) < 10:
        return {
            "timeframe": timeframe,
            "bias": Side.NEUTRAL.value,
            "trend": Side.NEUTRAL.value,
            "bos": Side.NEUTRAL.value,
            "choch": Side.NEUTRAL.value,
            "swing_high": 0.0,
            "swing_low": 0.0,
            "recent_highs": [],
            "recent_lows": [],
        }
    highs, lows = pivot_points(items)
    recent_highs = highs[-4:]
    recent_lows = lows[-4:]
    swing_high = recent_highs[-1][1] if recent_highs else max(c.high for c in items[-10:])
    swing_low = recent_lows[-1][1] if recent_lows else min(c.low for c in items[-10:])

    trend = Side.NEUTRAL.value
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        hh = recent_highs[-1][1] > recent_highs[-2][1]
        hl = recent_lows[-1][1] > recent_lows[-2][1]
        lh = recent_highs[-1][1] < recent_highs[-2][1]
        ll = recent_lows[-1][1] < recent_lows[-2][1]
        if hh and hl:
            trend = Side.LONG.value
        elif lh and ll:
            trend = Side.SHORT.value

    last_two = items[-2:]
    prior_high = recent_highs[-1][1] if recent_highs and recent_highs[-1][0] < len(items) - 2 else (
        recent_highs[-2][1] if len(recent_highs) >= 2 else max(c.high for c in items[-12:-2])
    )
    prior_low = recent_lows[-1][1] if recent_lows and recent_lows[-1][0] < len(items) - 2 else (
        recent_lows[-2][1] if len(recent_lows) >= 2 else min(c.low for c in items[-12:-2])
    )
    bos = Side.NEUTRAL.value
    if all(c.close > prior_high for c in last_two):
        bos = Side.LONG.value
    elif all(c.close < prior_low for c in last_two):
        bos = Side.SHORT.value

    choch = Side.NEUTRAL.value
    if trend == Side.SHORT.value and bos == Side.LONG.value:
        choch = Side.LONG.value
    elif trend == Side.LONG.value and bos == Side.SHORT.value:
        choch = Side.SHORT.value

    closes = [c.close for c in items]
    e20 = ema(closes[-80:], 20)
    e50 = ema(closes[-120:], 50)
    indicator_bias = Side.NEUTRAL.value
    if closes[-1] > e20 > e50:
        indicator_bias = Side.LONG.value
    elif closes[-1] < e20 < e50:
        indicator_bias = Side.SHORT.value

    bias = trend if trend != Side.NEUTRAL.value else indicator_bias
    if bos != Side.NEUTRAL.value:
        bias = bos
    return {
        "timeframe": timeframe,
        "bias": bias,
        "trend": trend,
        "bos": bos,
        "choch": choch,
        "swing_high": round_price(swing_high),
        "swing_low": round_price(swing_low),
        "prior_break_high": round_price(prior_high),
        "prior_break_low": round_price(prior_low),
        "recent_highs": [round_price(v) for _, v in recent_highs],
        "recent_lows": [round_price(v) for _, v in recent_lows],
        "ema20": round_price(e20),
        "ema50": round_price(e50),
    }


def detect_fvgs(candles: list[Candle], timeframe: str, max_age: int = 40) -> list[Zone]:
    items = closed(candles)
    local_atr = atr(items, 14)
    zones: list[Zone] = []
    if len(items) < 3 or local_atr <= 0:
        return zones
    start = max(2, len(items) - max_age)
    for i in range(start, len(items)):
        left, middle, right = items[i - 2], items[i - 1], items[i]
        if right.low > left.high:
            gap = right.low - left.high
            if gap >= 0.06 * local_atr:
                zones.append(Zone(
                    kind="FVG",
                    side=Side.LONG.value,
                    low=left.high,
                    high=right.low,
                    created_ts=middle.ts,
                    timeframe=timeframe,
                    strength=gap / local_atr,
                ))
        if right.high < left.low:
            gap = left.low - right.high
            if gap >= 0.06 * local_atr:
                zones.append(Zone(
                    kind="FVG",
                    side=Side.SHORT.value,
                    low=right.high,
                    high=left.low,
                    created_ts=middle.ts,
                    timeframe=timeframe,
                    strength=gap / local_atr,
                ))
    for zone in zones:
        after = [c for c in items if c.ts > zone.created_ts]
        if zone.side == Side.LONG.value:
            zone.mitigated = any(c.low <= zone.low for c in after)
        else:
            zone.mitigated = any(c.high >= zone.high for c in after)
    return [z for z in zones if not z.mitigated][-12:]


def detect_order_blocks(candles: list[Candle], timeframe: str, max_age: int = 35) -> list[Zone]:
    items = closed(candles)
    local_atr = atr(items, 14)
    zones: list[Zone] = []
    if len(items) < 5 or local_atr <= 0:
        return zones
    for i in range(max(1, len(items) - max_age), len(items) - 1):
        candle = items[i]
        next1 = items[i + 1]
        body_next = abs(next1.close - next1.open)
        if candle.close < candle.open and next1.close > next1.open and body_next >= 0.70 * local_atr and next1.close > candle.high:
            zones.append(Zone(
                kind="OB",
                side=Side.LONG.value,
                low=candle.low,
                high=max(candle.open, candle.close),
                created_ts=candle.ts,
                timeframe=timeframe,
                strength=body_next / local_atr,
            ))
        if candle.close > candle.open and next1.close < next1.open and body_next >= 0.70 * local_atr and next1.close < candle.low:
            zones.append(Zone(
                kind="OB",
                side=Side.SHORT.value,
                low=min(candle.open, candle.close),
                high=candle.high,
                created_ts=candle.ts,
                timeframe=timeframe,
                strength=body_next / local_atr,
            ))
    for zone in zones:
        after = [c for c in items if c.ts > zone.created_ts]
        if zone.side == Side.LONG.value:
            zone.mitigated = any(c.close < zone.low for c in after)
        else:
            zone.mitigated = any(c.close > zone.high for c in after)
    return [z for z in zones if not z.mitigated][-8:]


def zone_distance(price: float, zone: Zone) -> float:
    if zone.low <= price <= zone.high:
        return 0.0
    return min(abs(price - zone.low), abs(price - zone.high))


def nearest_zone(price: float, zones: list[Zone], side: str, max_distance: float) -> Optional[Zone]:
    matches = [z for z in zones if z.side == side and zone_distance(price, z) <= max_distance]
    return min(matches, key=lambda z: zone_distance(price, z), default=None)


def liquidity_sweep_snapshot(candles: list[Candle], lookback: int = 18) -> dict[str, Any]:
    items = closed(candles)
    if len(items) < lookback + 2:
        return {"side": Side.NEUTRAL.value, "level": 0.0, "reclaim": False, "age": 999}
    local_atr = atr(items, 14)
    for age, candle in enumerate(reversed(items[-6:])):
        idx = len(items) - 1 - age
        prior = items[max(0, idx - lookback):idx]
        if len(prior) < 6:
            continue
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        wick_up = candle.high > prior_high + 0.05 * local_atr and candle.close < prior_high
        wick_down = candle.low < prior_low - 0.05 * local_atr and candle.close > prior_low
        if wick_down:
            return {
                "side": Side.LONG.value,
                "level": round_price(prior_low),
                "extreme": round_price(candle.low),
                "reclaim": True,
                "age": age,
                "ts": candle.ts,
            }
        if wick_up:
            return {
                "side": Side.SHORT.value,
                "level": round_price(prior_high),
                "extreme": round_price(candle.high),
                "reclaim": True,
                "age": age,
                "ts": candle.ts,
            }
    return {"side": Side.NEUTRAL.value, "level": 0.0, "reclaim": False, "age": 999}


def breakout_retest_snapshot(candles_15m: list[Candle], candles_3m: list[Candle], side: str) -> dict[str, Any]:
    c15 = closed(candles_15m)
    c3 = closed(candles_3m)
    s15 = structure_snapshot(c15, "15m")
    if len(c15) < 5 or len(c3) < 5 or s15.get("bos") != side:
        return {"confirmed": False, "level": 0.0, "hold": False}
    level = safe_float(s15.get("prior_break_high" if side == Side.LONG.value else "prior_break_low"))
    local_atr3 = atr(c3, 14)
    if not level or local_atr3 <= 0:
        return {"confirmed": False, "level": level, "hold": False}
    recent = c3[-8:]
    if side == Side.LONG.value:
        touched = any(c.low <= level + 0.18 * local_atr3 for c in recent)
        hold = recent[-1].close > level and sum(c.close > level for c in recent[-3:]) >= 2
    else:
        touched = any(c.high >= level - 0.18 * local_atr3 for c in recent)
        hold = recent[-1].close < level and sum(c.close < level for c in recent[-3:]) >= 2
    return {"confirmed": bool(touched and hold), "level": round_price(level), "hold": bool(hold), "touched": bool(touched)}


def _latest_trigger_snapshot(candles_3m: list[Candle], side: str, reference_level: float = 0.0) -> dict[str, Any]:
    items = closed(candles_3m)
    if len(items) < 8:
        return {"ready": False, "score": 0, "level": reference_level, "reason": "недостатньо 3M даних"}
    local_atr = atr(items, 14)
    s3 = structure_snapshot(items, "3m")
    last = items[-1]
    prev = items[-2]
    body = abs(last.close - last.open)
    displacement = body >= 0.55 * local_atr if local_atr else False
    momentum = last.close > last.open and last.close > prev.high if side == Side.LONG.value else last.close < last.open and last.close < prev.low
    structure_ok = s3.get("bos") == side or s3.get("choch") == side or s3.get("bias") == side
    level_hold = True
    if reference_level:
        level_hold = last.close > reference_level if side == Side.LONG.value else last.close < reference_level
    ready = bool(structure_ok and level_hold and (displacement or momentum))
    score = 0
    if structure_ok:
        score += 7
    if level_hold:
        score += 3
    if displacement:
        score += 3
    if momentum:
        score += 2
    return {
        "ready": ready,
        "score": min(15, score),
        "level": round_price(reference_level or last.close),
        "event_price": round_price(last.close),
        "displacement": displacement,
        "momentum": momentum,
        "structure": s3,
        "reason": "3M BOS/CHOCH + displacement" if ready else "потрібен закритий 3M BOS/CHOCH або displacement",
    }


def _empty_3m_scan_memory() -> dict[str, Any]:
    return {
        "last_scanned_3m_ts": 0,
        "events": {},
        "processed_count": 0,
        "last_run_processed": 0,
        "updated_at": "",
    }


def _normalise_3m_scan_memory(raw: Any) -> dict[str, Any]:
    memory = _empty_3m_scan_memory()
    if isinstance(raw, dict):
        memory["last_scanned_3m_ts"] = int(raw.get("last_scanned_3m_ts") or 0)
        memory["processed_count"] = int(raw.get("processed_count") or 0)
        memory["events"] = dict(raw.get("events") or {})
        memory["updated_at"] = str(raw.get("updated_at") or "")
    return memory


def _event_invalidated_by_candle(event: dict[str, Any], candle: Candle, prefix: list[Candle]) -> bool:
    side = str(event.get("side") or "")
    invalidation = safe_float(event.get("invalidation_level"))
    if invalidation:
        if side == Side.LONG.value and candle.close <= invalidation:
            return True
        if side == Side.SHORT.value and candle.close >= invalidation:
            return True
    if candle.ts <= int(event.get("last_event_ts") or event.get("trigger_ts") or 0):
        return False
    structure = structure_snapshot(prefix, "3m")
    return structure.get("bos") == opposite(side) or structure.get("choch") == opposite(side)


def scan_closed_3m_sequence(candles_3m: list[Candle], previous: Any = None) -> dict[str, Any]:
    """Process every newly closed 3M candle in chronological order.

    The scanner stores a valid trigger even when it occurred several 3M candles
    before the 15-minute cron run. Later candles can refresh it by a retest/hold
    or invalidate it structurally. Only closed OKX candles are processed.
    """
    items = closed(candles_3m)
    memory = _normalise_3m_scan_memory(previous)
    events = memory["events"]
    last_ts = int(memory.get("last_scanned_3m_ts") or 0)
    if not items:
        memory["last_run_processed"] = 0
        return memory

    seed_from = max(20, len(items) - max(FULL_3M_SCAN_SEED_BARS, 24))
    processed = 0
    for idx, candle in enumerate(items):
        if idx < 20:
            continue
        if last_ts:
            if candle.ts <= last_ts:
                continue
        elif idx < seed_from:
            continue

        prefix = items[: idx + 1]
        local_atr = max(atr(prefix, 14), candle.close * 0.0005)

        # First invalidate old packages with the newly closed candle.
        for event_side in (Side.LONG.value, Side.SHORT.value):
            existing = events.get(event_side)
            if isinstance(existing, dict) and _event_invalidated_by_candle(existing, candle, prefix):
                existing["stage"] = "INVALIDATED"
                existing["invalidated_ts"] = candle.ts
                existing["invalidated_price"] = round_price(candle.close)
                events.pop(event_side, None)

        # Then evaluate this exact closed candle as a trigger event for both sides.
        for event_side in (Side.LONG.value, Side.SHORT.value):
            sweep = liquidity_sweep_snapshot(prefix, 18)
            existing = events.get(event_side) if isinstance(events.get(event_side), dict) else None
            reference = 0.0
            if sweep.get("side") == event_side:
                reference = safe_float(sweep.get("level"))
            elif existing:
                reference = safe_float(existing.get("reference_level") or existing.get("trigger_level"))

            snap = _latest_trigger_snapshot(prefix, event_side, reference)
            if snap.get("ready"):
                structure = snap.get("structure") or {}
                if sweep.get("side") == event_side and safe_float(sweep.get("extreme")):
                    invalidation = safe_float(sweep.get("extreme"))
                elif event_side == Side.LONG.value:
                    invalidation = min(c.low for c in prefix[max(0, idx - 6):idx])
                else:
                    invalidation = max(c.high for c in prefix[max(0, idx - 6):idx])
                event = {
                    "side": event_side,
                    "stage": "READY",
                    "trigger_ts": candle.ts,
                    "last_event_ts": candle.ts,
                    "last_retest_ts": 0,
                    "trigger_level": round_price(safe_float(snap.get("level")) or candle.close),
                    "event_price": round_price(candle.close),
                    "reference_level": round_price(reference),
                    "invalidation_level": round_price(invalidation),
                    "sweep_level": round_price(safe_float(sweep.get("level"))) if sweep.get("side") == event_side else 0.0,
                    "sweep_extreme": round_price(safe_float(sweep.get("extreme"))) if sweep.get("side") == event_side else 0.0,
                    "displacement": bool(snap.get("displacement")),
                    "momentum": bool(snap.get("momentum")),
                    "bos": structure.get("bos"),
                    "choch": structure.get("choch"),
                    "fingerprint": f"{event_side}:{candle.ts}:{round_price(reference)}:{round_price(invalidation)}",
                }
                events[event_side] = event
                continue

            # A later retest/hold refreshes the event without requiring the
            # original displacement candle to still be the latest candle.
            existing = events.get(event_side)
            if not isinstance(existing, dict) or existing.get("stage") != "READY":
                continue
            level = safe_float(existing.get("trigger_level") or existing.get("reference_level"))
            if not level:
                continue
            if event_side == Side.LONG.value:
                touched = candle.low <= level + 0.18 * local_atr
                held = candle.close > level
            else:
                touched = candle.high >= level - 0.18 * local_atr
                held = candle.close < level
            if touched and held:
                existing["last_retest_ts"] = candle.ts
                existing["last_event_ts"] = candle.ts
                existing["event_price"] = round_price(candle.close)
                existing["stage"] = "READY"

        processed += 1

    memory["events"] = events
    memory["last_scanned_3m_ts"] = items[-1].ts
    memory["last_run_processed"] = processed
    memory["processed_count"] = int(memory.get("processed_count") or 0) + processed
    memory["updated_at"] = iso_now()
    return memory


def trigger_snapshot(
    candles_3m: list[Candle],
    side: str,
    reference_level: float = 0.0,
    scan_memory: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    latest = _latest_trigger_snapshot(candles_3m, side, reference_level)
    items = closed(candles_3m)
    if not items or not isinstance(scan_memory, dict):
        return latest
    event = (scan_memory.get("events") or {}).get(side)
    if not isinstance(event, dict) or event.get("stage") != "READY":
        return latest

    event_ts = int(event.get("last_event_ts") or event.get("trigger_ts") or 0)
    age_bars = sum(1 for c in items if c.ts > event_ts)
    invalidation = safe_float(event.get("invalidation_level"))
    last = items[-1]
    still_valid = (
        (side == Side.LONG.value and (not invalidation or last.close > invalidation))
        or (side == Side.SHORT.value and (not invalidation or last.close < invalidation))
    )
    structure = structure_snapshot(items, "3m")
    still_valid = still_valid and structure.get("bos") != opposite(side) and structure.get("choch") != opposite(side)
    fresh = age_bars <= FULL_3M_TRIGGER_MAX_BARS
    if not (still_valid and fresh):
        return latest

    scanned = {
        "ready": True,
        "score": max(int(latest.get("score") or 0), 13),
        "level": round_price(safe_float(event.get("trigger_level")) or reference_level or last.close),
        "event_price": round_price(safe_float(event.get("event_price")) or last.close),
        "displacement": bool(event.get("displacement")),
        "momentum": bool(event.get("momentum")),
        "structure": structure,
        "reason": "повний 3M scan знайшов закритий BOS/CHOCH/displacement між запусками",
        "from_full_scan": True,
        "event_ts": event_ts,
        "age_bars": age_bars,
        "fingerprint": str(event.get("fingerprint") or ""),
    }
    if latest.get("ready") and int(latest.get("score") or 0) >= scanned["score"]:
        latest["from_full_scan"] = False
        latest["age_bars"] = 0
        return latest
    return scanned


def timeframe_snapshot(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    items = closed(candles)
    if len(items) < 5:
        return {"timeframe": timeframe, "available": False, "bias": Side.NEUTRAL.value}
    closes = [c.close for c in items]
    e20 = ema(closes[-80:], 20)
    e50 = ema(closes[-120:], 50)
    local_atr = atr(items, 14)
    structure = structure_snapshot(items, timeframe)
    score = 0
    if closes[-1] > e20:
        score += 10
    elif closes[-1] < e20:
        score -= 10
    if e20 > e50:
        score += 10
    elif e20 < e50:
        score -= 10
    if structure.get("bias") == Side.LONG.value:
        score += 15
    elif structure.get("bias") == Side.SHORT.value:
        score -= 15
    move8 = pct(closes[-1], closes[-9]) if len(closes) >= 9 else pct(closes[-1], closes[0])
    if move8 > 0.5:
        score += 5
    elif move8 < -0.5:
        score -= 5
    bias = Side.LONG.value if score >= 12 else Side.SHORT.value if score <= -12 else Side.NEUTRAL.value
    return {
        "timeframe": timeframe,
        "available": True,
        "bias": bias,
        "score": score,
        "close": round_price(closes[-1]),
        "ema20": round_price(e20),
        "ema50": round_price(e50),
        "rsi": round(rsi(items), 1),
        "atr": round_price(local_atr),
        "move8_pct": round(move8, 3),
        "efficiency": round(efficiency_ratio(items), 3),
        "volume_ratio": round(volume_ratio(items), 3),
        "structure": structure,
    }


def dealing_range_snapshot(candles_15m: list[Candle], price: float, lookback: int = 24) -> dict[str, Any]:
    items = closed(candles_15m)[-lookback:]
    if not items:
        return {"low": price, "high": price, "equilibrium": price, "position": 0.5, "zone": "MID"}
    low = min(c.low for c in items)
    high = max(c.high for c in items)
    width = max(high - low, 1e-9)
    position = clamp((price - low) / width, 0.0, 1.0)
    zone = "DISCOUNT" if position <= 0.40 else "PREMIUM" if position >= 0.60 else "MID"
    return {
        "low": round_price(low),
        "high": round_price(high),
        "equilibrium": round_price((low + high) / 2.0),
        "position": round(position, 3),
        "zone": zone,
        "at_lower_edge": position <= 0.20,
        "at_upper_edge": position >= 0.80,
    }


def flow_snapshot(trades: list[dict[str, Any]], book: dict[str, Any]) -> dict[str, Any]:
    buy = sum(safe_float(t.get("size")) for t in trades if str(t.get("side")) == "buy")
    sell = sum(safe_float(t.get("size")) for t in trades if str(t.get("side")) == "sell")
    total = buy + sell
    delta_pct = (buy - sell) / total * 100 if total else 0.0
    bid = sum(size for _, size in book.get("bids", [])[:20])
    ask = sum(size for _, size in book.get("asks", [])[:20])
    book_total = bid + ask
    book_delta = (bid - ask) / book_total * 100 if book_total else 0.0
    combined = 0.65 * delta_pct + 0.35 * book_delta
    bias = Side.LONG.value if combined >= 18 else Side.SHORT.value if combined <= -18 else Side.NEUTRAL.value
    confidence = "HIGH" if total > 0 and abs(combined) >= 35 else "MEDIUM" if total > 0 else "LOW"
    return {
        "bias": bias,
        "score": int(round(clamp(combined / 4.0, -10, 10))),
        "trade_delta_pct": round(delta_pct, 2),
        "book_delta_pct": round(book_delta, 2),
        "confidence": confidence,
        "sample_size": len(trades),
    }


def detect_regime(context: dict[str, Any]) -> tuple[str, str]:
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    c15 = context["candles"]["15m"]
    local_atr = safe_float(tf15.get("atr"))
    items = closed(c15)
    move3 = abs(items[-1].close - items[-4].open) / local_atr if len(items) >= 4 and local_atr else 0.0
    if move3 >= 2.0:
        return Regime.SHOCK.value, "останні три 15M свічки пройшли понад 2 ATR"
    same_side = tf15.get("bias") in {Side.LONG.value, Side.SHORT.value} and tf15.get("bias") == tf1h.get("bias")
    if same_side and safe_float(tf15.get("efficiency")) >= 0.42:
        return Regime.TREND.value, "15M і 1H структура узгоджені та рух ефективний"
    if safe_float(tf15.get("efficiency")) <= 0.28 and abs(safe_float(tf15.get("move8_pct"))) <= 1.0:
        return Regime.RANGE.value, "15M рух перекривається і не має ефективного напрямку"
    s15 = tf15.get("structure") or {}
    if s15.get("choch") in {Side.LONG.value, Side.SHORT.value} or (tf15.get("bias") != tf1h.get("bias") and tf15.get("bias") != Side.NEUTRAL.value):
        return Regime.TRANSITION.value, "15M структура змінюється або не узгоджена з 1H"
    return Regime.NORMAL.value, "ринок без стійкого тренду або чистого діапазону"


def collect_liquidity_targets(context: dict[str, Any], side: str) -> list[float]:
    price = context["price"]
    levels: list[float] = []
    for key in ("s3", "s15", "s1h"):
        structure = context[key]
        values = structure.get("recent_highs", []) if side == Side.LONG.value else structure.get("recent_lows", [])
        levels.extend(safe_float(v) for v in values)
    dr = context["dealing_range"]
    levels.append(safe_float(dr.get("high" if side == Side.LONG.value else "low")))
    for zone in context["zones"]:
        if side == Side.LONG.value and zone.side == Side.SHORT.value:
            levels.extend([zone.low, zone.high])
        if side == Side.SHORT.value and zone.side == Side.LONG.value:
            levels.extend([zone.low, zone.high])
    unique = sorted({round_price(v) for v in levels if v > 0})
    if side == Side.LONG.value:
        return [v for v in unique if v > price]
    return sorted([v for v in unique if v < price], reverse=True)


def build_context(data: dict[str, Any], state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    candles = data.get("candles") or {}
    c3 = closed(candles.get("3m", []))
    c15 = closed(candles.get("15m", []))
    c1h = closed(candles.get("1h", []))
    c4h = closed(candles.get("4h", []))
    if min(len(c3), len(c15), len(c1h)) < 20:
        raise RuntimeError("Недостатньо закритих свічок для чистої ICT-моделі")
    scan_3m = scan_closed_3m_sequence(c3, (state or {}).get("scan_3m"))
    if state is not None:
        state["scan_3m"] = scan_3m
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price")) or c3[-1].close
    tf3 = timeframe_snapshot(c3, "3m")
    tf15 = timeframe_snapshot(c15, "15m")
    tf1h = timeframe_snapshot(c1h, "1h")
    tf4h = timeframe_snapshot(c4h, "4h")
    s3 = tf3.get("structure") or structure_snapshot(c3, "3m")
    s15 = tf15.get("structure") or structure_snapshot(c15, "15m")
    s1h = tf1h.get("structure") or structure_snapshot(c1h, "1h")
    zones = (
        detect_fvgs(c3, "3m")
        + detect_order_blocks(c3, "3m")
        + detect_fvgs(c15, "15m")
        + detect_order_blocks(c15, "15m")
    )
    context: dict[str, Any] = {
        "time": iso_now(),
        "price": round_price(price),
        "price_source": ticker.get("source", "OKX"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "reference_price": round_price((data.get("reference") or {}).get("price")),
        "cross_price_diff_pct": safe_float(data.get("cross_price_diff_pct")),
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "scan_3m": scan_3m,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "s3": s3,
        "s15": s15,
        "s1h": s1h,
        "zones": zones,
        "sweep3": liquidity_sweep_snapshot(c3, 18),
        "sweep15": liquidity_sweep_snapshot(c15, 16),
        "dealing_range": dealing_range_snapshot(c15, price),
        "flow": flow_snapshot(data.get("trades") or [], data.get("book") or {}),
        "derivatives": data.get("derivatives") or {},
    }
    regime, reason = detect_regime(context)
    context["regime"] = regime
    context["regime_reason"] = reason
    context["targets_long"] = collect_liquidity_targets(context, Side.LONG.value)
    context["targets_short"] = collect_liquidity_targets(context, Side.SHORT.value)
    return context

# ==========================================================
# MUTUALLY EXCLUSIVE ICT SETUPS
# ==========================================================


def side_bias_points(bias: str, side: str, positive: int, neutral: int = 0, opposite_points: int = 0) -> int:
    if bias == side:
        return positive
    if bias == Side.NEUTRAL.value:
        return neutral
    return opposite_points


def relevant_zone(context: dict[str, Any], side: str, distance_atr: float = 0.55) -> Optional[Zone]:
    price = context["price"]
    atr15 = safe_float(context["tf15"].get("atr"))
    return nearest_zone(price, context["zones"], side, max(atr15 * distance_atr, price * 0.0015))


def side_location_score(context: dict[str, Any], side: str, zone: Optional[Zone]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    dr = context["dealing_range"]
    if side == Side.LONG.value and dr.get("zone") == "DISCOUNT":
        score += 12
        reasons.append("ціна в discount частині 15M dealing range")
    elif side == Side.SHORT.value and dr.get("zone") == "PREMIUM":
        score += 12
        reasons.append("ціна в premium частині 15M dealing range")
    elif dr.get("zone") == "MID":
        score += 3
    if zone:
        score += 9 if zone.timeframe == "15m" else 7
        reasons.append(f"ціна біля свіжої {zone.timeframe} {zone.kind} зони")
    if side == Side.LONG.value and dr.get("at_lower_edge"):
        score += 4
        reasons.append("ціна біля нижньої межі діапазону")
    if side == Side.SHORT.value and dr.get("at_upper_edge"):
        score += 4
        reasons.append("ціна біля верхньої межі діапазону")
    return min(25, score), reasons


def side_flow_score(context: dict[str, Any], side: str) -> tuple[int, list[str], list[str]]:
    flow = context["flow"]
    confirmations: list[str] = []
    risks: list[str] = []
    if flow.get("bias") == side:
        confirmations.append("поточний flow підтримує напрям")
        return 10, confirmations, risks
    if flow.get("bias") == Side.NEUTRAL.value:
        return 5, confirmations, risks
    risks.append("короткий flow зараз проти напрямку")
    return 0, confirmations, risks


def htf_score(context: dict[str, Any], side: str) -> tuple[int, list[str], list[str]]:
    tf15 = context["tf15"].get("bias")
    tf1h = context["tf1h"].get("bias")
    tf4h = context["tf4h"].get("bias")
    confirmations: list[str] = []
    risks: list[str] = []
    if tf15 == side and tf1h == side:
        confirmations.append("15M і 1H узгоджені")
        points = 5
    elif tf15 == side or tf1h == side:
        confirmations.append("один старший таймфрейм підтримує")
        points = 3
    elif tf15 == Side.NEUTRAL.value and tf1h == Side.NEUTRAL.value:
        points = 2
    else:
        points = 0
        risks.append("старший таймфрейм не підтримує напрям")
    if tf4h == opposite(side):
        risks.append("4H контекст проти")
    return points, confirmations, risks


def candidate_sweep_reversal(context: dict[str, Any], side: str) -> Optional[Candidate]:
    sweep3 = context["sweep3"]
    sweep15 = context["sweep15"]
    sweep = sweep3 if sweep3.get("side") == side else sweep15 if sweep15.get("side") == side else None
    if not sweep:
        return None
    zone = relevant_zone(context, side, 0.75)
    location, confirmations = side_location_score(context, side, zone)
    liquidity = 20 if sweep3.get("side") == side else 16
    confirmations.append("знято зовнішню ліквідність і ціна повернулася за рівень")

    structure = 0
    s3 = context["s3"]
    s15 = context["s15"]
    if s3.get("choch") == side:
        structure += 13
        confirmations.append("3M CHOCH підтвердив розворот")
    elif s3.get("bos") == side:
        structure += 11
        confirmations.append("3M BOS підтвердив розворот")
    elif s3.get("bias") == side:
        structure += 7
    if s15.get("choch") == side or s15.get("bos") == side:
        structure += 10
        confirmations.append("15M структура вже змінилася")
    elif context["tf15"].get("bias") != opposite(side):
        structure += 5
    structure = min(25, structure)

    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(sweep.get("level")), context.get("scan_3m"))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    risks = flow_risks + htf_risks
    components = {
        "location": location,
        "structure": structure,
        "liquidity": liquidity,
        "trigger": int(trigger["score"]),
        "flow": flow_points,
        "htf": htf_points,
    }
    raw = sum(components.values())
    invalidation = safe_float(sweep.get("extreme"))
    if zone:
        invalidation = min(invalidation, zone.low) if side == Side.LONG.value else max(invalidation, zone.high)
    targets = context["targets_long" if side == Side.LONG.value else "targets_short"]
    candidate = Candidate(
        side=side,
        setup_type=SetupType.SWEEP_REVERSAL.value,
        raw_score=raw,
        final_score=raw,
        score_components=components,
        trigger_ready=bool(trigger["ready"]),
        trigger_level=safe_float(trigger.get("event_price")) or context["price"],
        invalidation_level=invalidation,
        target_levels=targets,
        confirmations=confirmations,
        risks=risks,
        risk_mode="RISKY" if context["tf1h"].get("bias") == opposite(side) else "NORMAL",
    )
    return apply_candidate_risk_adjustments(context, candidate)


def candidate_pullback_continuation(context: dict[str, Any], side: str) -> Optional[Candidate]:
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    if tf15.get("bias") != side and tf1h.get("bias") != side:
        return None
    if context["regime"] == Regime.RANGE.value:
        return None
    zone = relevant_zone(context, side, 0.70)
    dr = context["dealing_range"]
    correct_half = (side == Side.LONG.value and dr.get("position", 0.5) <= 0.58) or (
        side == Side.SHORT.value and dr.get("position", 0.5) >= 0.42
    )
    if not zone and not correct_half:
        return None

    location, confirmations = side_location_score(context, side, zone)
    if correct_half:
        location = min(25, location + 4)
    structure = 0
    if tf15.get("bias") == side:
        structure += 12
        confirmations.append("15M структура тримає напрям")
    if tf1h.get("bias") == side:
        structure += 8
        confirmations.append("1H підтримує продовження")
    if context["s3"].get("bias") == side:
        structure += 5
    structure = min(25, structure)

    # Internal liquidity is considered taken when a pullback touched a zone or
    # briefly swept against the trend.
    counter_sweep = context["sweep3"].get("side") == side
    liquidity = 10 + (6 if counter_sweep else 0) + (4 if zone else 0)
    if counter_sweep:
        confirmations.append("відкат забрав внутрішню ліквідність")

    reference = (zone.low + zone.high) / 2.0 if zone else safe_float(
        context["s15"].get("swing_low" if side == Side.LONG.value else "swing_high")
    )
    trigger = trigger_snapshot(context["candles"]["3m"], side, reference, context.get("scan_3m"))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    risks = flow_risks + htf_risks
    components = {
        "location": location,
        "structure": structure,
        "liquidity": min(20, liquidity),
        "trigger": int(trigger["score"]),
        "flow": flow_points,
        "htf": htf_points,
    }
    raw = sum(components.values())
    if zone:
        invalidation = zone.low if side == Side.LONG.value else zone.high
    else:
        invalidation = safe_float(context["s15"].get("swing_low" if side == Side.LONG.value else "swing_high"))
    candidate = Candidate(
        side=side,
        setup_type=SetupType.PULLBACK_CONTINUATION.value,
        raw_score=raw,
        final_score=raw,
        score_components=components,
        trigger_ready=bool(trigger["ready"]),
        trigger_level=safe_float(trigger.get("event_price")) or context["price"],
        invalidation_level=round_price(invalidation),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=risks,
        risk_mode="NORMAL",
    )
    return apply_candidate_risk_adjustments(context, candidate)


def candidate_breakout_retest(context: dict[str, Any], side: str) -> Optional[Candidate]:
    retest = breakout_retest_snapshot(context["candles"]["15m"], context["candles"]["3m"], side)
    if not retest.get("level"):
        return None
    zone = relevant_zone(context, side, 0.55)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 10)
    structure = 23 if retest.get("confirmed") else 15
    liquidity = 16
    confirmations.append("15M закрив BOS за напрямком")
    if retest.get("confirmed"):
        confirmations.append("3M повернувся до BOS-рівня і втримав його")
    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(retest.get("level")), context.get("scan_3m"))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    risks = flow_risks + htf_risks
    components = {
        "location": location,
        "structure": structure,
        "liquidity": liquidity,
        "trigger": max(int(trigger["score"]), 13 if retest.get("confirmed") else int(trigger["score"])),
        "flow": flow_points,
        "htf": htf_points,
    }
    raw = sum(components.values())
    atr15 = safe_float(context["tf15"].get("atr"))
    level = safe_float(retest.get("level"))
    invalidation = level - 0.30 * atr15 if side == Side.LONG.value else level + 0.30 * atr15
    candidate = Candidate(
        side=side,
        setup_type=SetupType.BREAKOUT_RETEST.value,
        raw_score=raw,
        final_score=raw,
        score_components=components,
        trigger_ready=bool(retest.get("confirmed") and trigger.get("ready")),
        trigger_level=safe_float(trigger.get("event_price")) or context["price"],
        invalidation_level=round_price(invalidation),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=risks,
        risk_mode="NORMAL",
    )
    return apply_candidate_risk_adjustments(context, candidate)


def candidate_range_edge(context: dict[str, Any], side: str) -> Optional[Candidate]:
    if context["regime"] != Regime.RANGE.value:
        return None
    dr = context["dealing_range"]
    at_edge = dr.get("at_lower_edge") if side == Side.LONG.value else dr.get("at_upper_edge")
    if not at_edge:
        return None
    sweep = context["sweep3"] if context["sweep3"].get("side") == side else context["sweep15"]
    if sweep.get("side") != side:
        return None
    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(sweep.get("level")), context.get("scan_3m"))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    components = {
        "location": 25,
        "structure": 14 + (6 if context["s3"].get("choch") == side else 0),
        "liquidity": 20,
        "trigger": int(trigger["score"]),
        "flow": flow_points,
        "htf": 2,
    }
    confirmations = ["ціна на зовнішньому краї 15M діапазону", "край діапазону зняв ліквідність"] + flow_conf
    candidate = Candidate(
        side=side,
        setup_type=SetupType.RANGE_EDGE_REVERSAL.value,
        raw_score=sum(components.values()),
        final_score=sum(components.values()),
        score_components=components,
        trigger_ready=bool(trigger["ready"]),
        trigger_level=safe_float(trigger.get("event_price")) or context["price"],
        invalidation_level=safe_float(sweep.get("extreme")),
        target_levels=[safe_float(dr.get("equilibrium")), safe_float(dr.get("high" if side == Side.LONG.value else "low"))],
        confirmations=confirmations,
        risks=flow_risks,
        risk_mode="RISKY",
    )
    return apply_candidate_risk_adjustments(context, candidate)



def clone_candidate(base: Candidate, setup_type: str, score_delta: int = 0, lane: str = "STANDARD", extra_confirmation: str = "") -> Candidate:
    copied = Candidate(
        side=base.side,
        setup_type=setup_type,
        raw_score=max(0, min(100, base.raw_score + score_delta)),
        final_score=max(0, min(100, base.final_score + score_delta)),
        score_components=dict(base.score_components),
        trigger_ready=base.trigger_ready,
        trigger_level=base.trigger_level,
        invalidation_level=base.invalidation_level,
        target_levels=list(base.target_levels),
        confirmations=list(base.confirmations),
        risks=list(base.risks),
        hard_reject_reason=base.hard_reject_reason,
        risk_mode=base.risk_mode,
        late_extension_atr=base.late_extension_atr,
        execution_lane=lane,
        stage=base.stage,
        tactical_stop=(lane == "EARLY"),
    )
    if extra_confirmation:
        copied.confirmations.insert(0, extra_confirmation)
    return copied


def candidate_capitulation_recovery(context: dict[str, Any], side: str) -> Optional[Candidate]:
    if context.get("regime") != Regime.SHOCK.value:
        return None
    base = candidate_sweep_reversal(context, side)
    if not base:
        return None
    move = abs(safe_float(context["tf15"].get("move8_pct")))
    if move < 0.45:
        return None
    return clone_candidate(base, SetupType.CAPITULATION_RECOVERY.value, 4, "EARLY", "капітуляційний рух повернувся за ліквідність")


def candidate_closed_15m_direction_flip(context: dict[str, Any], side: str) -> Optional[Candidate]:
    s15 = context["s15"]
    if s15.get("bos") != side and s15.get("choch") != side:
        return None
    base = candidate_breakout_retest(context, side) or candidate_pullback_continuation(context, side)
    if not base:
        return None
    return clone_candidate(base, SetupType.CLOSED_15M_DIRECTION_FLIP.value, 3, "EARLY", "15M закрив підтверджену зміну напрямку")


def candidate_range_compression_breakout(context: dict[str, Any], side: str) -> Optional[Candidate]:
    c15 = closed(context["candles"]["15m"])
    if len(c15) < 8:
        return None
    recent = c15[-6:]
    widths = [max(c.high-c.low, 1e-9) for c in recent]
    compressed = mean(widths[-3:]) <= mean(widths[:3]) * 0.78
    if not compressed:
        return None
    base = candidate_breakout_retest(context, side)
    if not base:
        return None
    return clone_candidate(base, SetupType.RANGE_COMPRESSION_BREAKOUT.value, 2, "STANDARD", "стиснення діапазону завершилось прийнятим пробоєм")


def candidate_trend_ignition(context: dict[str, Any], side: str) -> Optional[Candidate]:
    tf3, tf15 = context["tf3"], context["tf15"]
    if tf3.get("bias") != side or tf15.get("bias") != side:
        return None
    if safe_float(tf3.get("efficiency")) < 0.55 or safe_float(tf15.get("volume_ratio")) < 1.1:
        return None
    base = candidate_breakout_retest(context, side) or candidate_pullback_continuation(context, side)
    if not base:
        return None
    return clone_candidate(base, SetupType.TREND_IGNITION.value, 3, "EARLY", "новий імпульс має displacement, efficiency і обсяг")


def candidate_fresh_base_reentry(context: dict[str, Any], side: str) -> Optional[Candidate]:
    c15 = closed(context["candles"]["15m"])
    if len(c15) < 10:
        return None
    recent = c15[-6:]
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"]*0.001)
    base_range = max(c.high for c in recent)-min(c.low for c in recent)
    if base_range > 2.2*atr15:
        return None
    base = candidate_pullback_continuation(context, side)
    if not base or not base.trigger_ready:
        return None
    return clone_candidate(base, SetupType.FRESH_BASE_CONTINUATION_REENTRY.value, 2, "STANDARD", "нова 15M база сформувала окремий continuation-цикл")


def assign_execution_lane(candidate: Candidate) -> Candidate:
    if candidate.setup_type in EARLY_SETUP_TYPES:
        candidate.execution_lane = "EARLY"
        candidate.tactical_stop = True
    else:
        candidate.execution_lane = "STANDARD"
    return candidate

def apply_candidate_risk_adjustments(context: dict[str, Any], candidate: Candidate) -> Candidate:
    penalty = 0
    price = context["price"]
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    anchor = candidate.trigger_level or price
    extension = abs(price - anchor) / atr15
    candidate.late_extension_atr = round(extension, 3)
    if extension > LATE_EXTENSION_ATR15:
        penalty += 12
        candidate.risks.append("ціна відійшла від ICT-тригера; вхід тільки після нового відкату")
    if extension > 1.65:
        candidate.hard_reject_reason = "сетап уже запізнений відносно свого тригера"

    if context["flow"].get("bias") == opposite(candidate.side):
        penalty += 5
    if context["tf4h"].get("bias") == opposite(candidate.side):
        penalty += 3
    if safe_float(context.get("cross_price_diff_pct")) > 0.35:
        penalty += 5
        candidate.risks.append("TradingView reference відрізняється від джерела розрахунку")
        candidate.risk_mode = "RISKY"

    regime = context["regime"]
    if regime == Regime.SHOCK.value:
        penalty += 8
        candidate.risks.append("після шокового руху потрібне швидке підтвердження")
        candidate.risk_mode = "RISKY"
    if candidate.setup_type == SetupType.PULLBACK_CONTINUATION.value and regime == Regime.TRANSITION.value:
        penalty += 8
        candidate.risks.append("15M ще у перехідній фазі")
    if candidate.setup_type in {SetupType.SWEEP_REVERSAL.value, SetupType.RANGE_EDGE_REVERSAL.value} and regime == Regime.TREND.value:
        if context["s15"].get("choch") != candidate.side:
            penalty += 8
            candidate.risk_mode = "RISKY"
            candidate.risks.append("розворот проти усталеного тренду")
    if candidate.setup_type == SetupType.BREAKOUT_RETEST.value and regime == Regime.RANGE.value:
        penalty += 5

    if candidate.invalidation_level <= 0:
        candidate.hard_reject_reason = "немає об'єктивної ICT-інвалідації"
    elif candidate.side == Side.LONG.value and candidate.invalidation_level >= price:
        candidate.hard_reject_reason = "LONG-інвалідація знаходиться вище ціни"
    elif candidate.side == Side.SHORT.value and candidate.invalidation_level <= price:
        candidate.hard_reject_reason = "SHORT-інвалідація знаходиться нижче ціни"

    candidate.final_score = max(0, min(100, candidate.raw_score - penalty))
    return candidate


def build_candidates(context: dict[str, Any]) -> list[Candidate]:
    candidates: list[Candidate] = []
    builders = (
        candidate_sweep_reversal,
        candidate_pullback_continuation,
        candidate_breakout_retest,
        candidate_range_compression_breakout,
        candidate_trend_ignition,
        candidate_closed_15m_direction_flip,
        candidate_capitulation_recovery,
        candidate_fresh_base_reentry,
        candidate_range_edge,
    )
    for side in (Side.LONG.value, Side.SHORT.value):
        for builder in builders:
            try:
                candidate = builder(context, side)
                if candidate:
                    candidates.append(assign_execution_lane(candidate))
            except Exception as exc:
                print(f"[WARN] Candidate {builder.__name__}/{side} failed: {exc}")
    # One candidate per semantic setup and side. There is no rescue lane that
    # can re-label the same event after the selector.
    unique: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        key = (candidate.side, candidate.setup_type)
        if key not in unique or candidate.final_score > unique[key].final_score:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda c: (c.final_score, c.raw_score), reverse=True)


# ==========================================================
# RISK / STOP / TARGET GEOMETRY
# ==========================================================


def target_rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk else 0.0


def level_in_direction(level: float, entry: float, side: str) -> bool:
    return level > entry if side == Side.LONG.value else level < entry


def choose_target(levels: list[float], entry: float, side: str, minimum_distance: float) -> tuple[float, list[float]]:
    directional = [safe_float(v) for v in levels if level_in_direction(safe_float(v), entry, side)]
    directional = sorted(set(directional), reverse=(side == Side.SHORT.value))
    checkpoints: list[float] = []
    for level in directional:
        if abs(level - entry) >= minimum_distance:
            return level, checkpoints
        checkpoints.append(level)
    return 0.0, checkpoints


def build_trade_plan(context: dict[str, Any], candidate: Candidate) -> TradePlan:
    entry = context["price"]
    side = candidate.side
    sign = side_sign(side)
    atr15 = max(safe_float(context["tf15"].get("atr")), entry * 0.001)
    buffer = 0.12 * atr15
    structural = candidate.invalidation_level
    raw_stop = structural - buffer if side == Side.LONG.value else structural + buffer
    raw_risk = abs(entry - raw_stop)
    if candidate.execution_lane in {"EARLY", "REENTRY"}:
        min_risk = TACTICAL_MIN_STOP_ATR15 * atr15
        max_risk = TACTICAL_MAX_STOP_ATR15 * atr15
    else:
        min_risk = MIN_STOP_ATR15 * atr15
        max_risk = MAX_STOP_ATR15 * atr15
    stop = raw_stop
    stop_basis = ("тактична структурна інвалідація 3M/15M + 0.12 ATR" if candidate.execution_lane in {"EARLY", "REENTRY"} else "стандартна ICT-інвалідація 15M/1H + 0.12 ATR")
    if raw_risk < min_risk:
        stop = entry - sign * min_risk
        stop_basis = "ICT invalidation розширена до мінімального anti-noise stop"
    risk = abs(entry - stop)

    position_risk = RISKY_RISK_PCT if candidate.risk_mode == "RISKY" else NORMAL_RISK_PCT
    invalid = ""
    better_entry = 0.0
    if risk > max_risk:
        invalid = "структурний стоп надто широкий для поточної ціни"
        better_entry = stop + sign * max_risk
    if not ((side == Side.LONG.value and stop < entry) or (side == Side.SHORT.value and stop > entry)):
        invalid = "стоп знаходиться з неправильної сторони від входу"

    min_tp1_distance = max(MIN_TP1_ATR15 * atr15, MIN_RR1 * risk)
    tp1, checkpoints = choose_target(candidate.target_levels, entry, side, min_tp1_distance)
    if not tp1:
        tp1 = entry + sign * max(PREFERRED_RR1 * risk, 1.05 * atr15)
    rr1 = target_rr(entry, stop, tp1)

    later_levels = [v for v in candidate.target_levels if level_in_direction(v, tp1, side)]
    tp2, checkpoints2 = choose_target(later_levels, entry, side, max(2.20 * risk, 1.60 * atr15))
    if not tp2:
        tp2 = entry + sign * max(2.30 * risk, 1.70 * atr15)
    later_levels_3 = [v for v in candidate.target_levels if level_in_direction(v, tp2, side)]
    tp3, checkpoints3 = choose_target(later_levels_3, entry, side, max(3.20 * risk, 2.50 * atr15))
    if not tp3:
        tp3 = entry + sign * max(3.50 * risk, 2.70 * atr15)

    # Enforce monotonic targets once, centrally.
    if side == Side.LONG.value:
        tp2 = max(tp2, tp1 + 0.45 * atr15)
        tp3 = max(tp3, tp2 + 0.55 * atr15)
    else:
        tp2 = min(tp2, tp1 - 0.45 * atr15)
        tp3 = min(tp3, tp2 - 0.55 * atr15)

    rr2 = target_rr(entry, stop, tp2)
    rr3 = target_rr(entry, stop, tp3)
    if rr1 < MIN_RR1:
        invalid = f"TP1 дає лише {rr1:.2f}R"
        required_entry = (tp1 + MIN_RR1 * stop) / (1 + MIN_RR1) if side == Side.LONG.value else (tp1 + MIN_RR1 * stop) / (1 + MIN_RR1)
        better_entry = required_entry

    position_size = 0.0
    notional = 0.0
    if ACCOUNT_BALANCE > 0 and risk > 0:
        cash_risk = ACCOUNT_BALANCE * position_risk / 100.0
        position_size = cash_risk / risk
        notional = position_size * entry

    return TradePlan(
        entry=round_price(entry),
        stop=round_price(stop),
        tp1=round_price(tp1),
        tp2=round_price(tp2),
        tp3=round_price(tp3),
        risk_pct=round(abs(entry - stop) / entry * 100.0, 3),
        rr1=round(rr1, 2),
        rr2=round(rr2, 2),
        rr3=round(rr3, 2),
        position_risk_pct=position_risk,
        position_size=round(position_size, 6),
        notional=round(notional, 2),
        invalidation=f"закриття 15M за ICT-інвалідацією {round_price(structural)}",
        stop_basis=stop_basis,
        target_basis="перша реальна протилежна ліквідність, що дає мінімум 2R; ближчі рівні — checkpoints",
        checkpoints=[round_price(v) for v in (checkpoints + checkpoints2 + checkpoints3)[:5]],
        valid=not invalid,
        reason=invalid,
        better_entry=round_price(better_entry),
    )


def opportunity_is_valid(opportunity: Opportunity, context: dict[str, Any]) -> bool:
    expires = parse_iso(opportunity.expires_at)
    if not expires or now_utc() >= expires:
        return False
    price = context["price"]
    if opportunity.side == Side.LONG.value and price <= opportunity.invalidation_level:
        return False
    if opportunity.side == Side.SHORT.value and price >= opportunity.invalidation_level:
        return False
    return True


def make_opportunity(candidate: Candidate) -> Opportunity:
    created = now_utc()
    expires = created.timestamp() + OPPORTUNITY_TTL_MIN * 60
    return Opportunity(
        side=candidate.side,
        setup_type=candidate.setup_type,
        created_at=created.isoformat(),
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        score=candidate.final_score,
        trigger_level=round_price(candidate.trigger_level),
        invalidation_level=round_price(candidate.invalidation_level),
        confirmations=candidate.confirmations[:6],
        status="WAIT_PULLBACK" if candidate.execution_lane == "REENTRY" else "ARMED",
        execution_lane=candidate.execution_lane,
        optimal_entry=round_price(candidate.trigger_level),
        target_level=round_price(candidate.target_levels[0] if candidate.target_levels else 0.0),
        fingerprint=f"{candidate.side}:{candidate.setup_type}:{round_price(candidate.trigger_level)}:{round_price(candidate.invalidation_level)}",
    )


def evaluate_new_setup(context: dict[str, Any], state: dict[str, Any]) -> Decision:
    candidates = build_candidates(context)
    viable = [c for c in candidates if not c.hard_reject_reason]
    audit_candidates = [
        {
            "side": c.side,
            "setup_type": c.setup_type,
            "raw_score": c.raw_score,
            "final_score": c.final_score,
            "trigger_ready": c.trigger_ready,
            "risk_mode": c.risk_mode,
            "late_extension_atr": c.late_extension_atr,
            "hard_reject_reason": c.hard_reject_reason,
            "score_components": c.score_components,
            "execution_lane": c.execution_lane,
            "stage": c.stage,
        }
        for c in candidates
    ]

    if not viable or viable[0].final_score < ARMED_SCORE:
        saved = opportunity_from_state(state)
        if saved and opportunity_is_valid(saved, context):
            return Decision(
                id=uuid.uuid4().hex[:12],
                time=iso_now(),
                action=Action.ARMED.value,
                side=saved.side,
                setup_type=saved.setup_type,
                quality=saved.score,
                reason="раніше сформований ICT-сценарій ще чинний, але свіжого 3M-тригера немає",
                regime=context["regime"],
                audit={"candidates": audit_candidates, "opportunity_memory": asdict(saved)},
            )
        return Decision(
            id=uuid.uuid4().hex[:12],
            time=iso_now(),
            action=Action.NO_SETUP.value,
            side=Side.NEUTRAL.value,
            setup_type=SetupType.NONE.value,
            quality=viable[0].final_score if viable else 0,
            reason="ринок не дав повного ICT-ланцюжка: локація → ліквідність → структура → 3M-тригер",
            regime=context["regime"],
            audit={"candidates": audit_candidates},
        )

    best = viable[0]
    strongest_opposite = max((c for c in viable if c.side == opposite(best.side)), key=lambda c: c.final_score, default=None)
    if strongest_opposite and strongest_opposite.final_score >= ARMED_SCORE and best.final_score - strongest_opposite.final_score < DIRECTION_MARGIN:
        return Decision(
            id=uuid.uuid4().hex[:12],
            time=iso_now(),
            action=Action.NO_SETUP.value,
            side=Side.NEUTRAL.value,
            setup_type=SetupType.NONE.value,
            quality=best.final_score,
            reason=f"LONG і SHORT ICT-сценарії надто близькі ({best.final_score} проти {strongest_opposite.final_score}); переваги немає",
            regime=context["regime"],
            audit={"candidates": audit_candidates, "direction_conflict": True},
        )

    saved = opportunity_from_state(state)
    saved_reentry = bool(
        saved
        and opportunity_is_valid(saved, context)
        and saved.status == "WAIT_PULLBACK"
        and saved.side == best.side
        and saved.setup_type == best.setup_type
    )
    if saved_reentry and best.trigger_ready and best.late_extension_atr <= LATE_EXTENSION_ATR15:
        best.execution_lane = "REENTRY"
        best.tactical_stop = True
        best.stage = "RETEST_HOLD"

    plan = build_trade_plan(context, best)
    reason = best.confirmations[0] if best.confirmations else "ICT-сетап сформований"
    action = Action.ARMED.value
    execution_lane = best.execution_lane
    if best.late_extension_atr > LATE_EXTENSION_ATR15:
        execution_lane = "REENTRY"
        best.execution_lane = "REENTRY"
        best.stage = "WAIT_PULLBACK"
    elif best.trigger_ready and plan.valid:
        if execution_lane == "EARLY" and best.final_score >= EARLY_ENTRY_SCORE and len([v for v in best.score_components.values() if v > 0]) >= 3:
            action = Action.RISKY_ENTRY.value
            best.stage = "EXECUTABLE"
        elif execution_lane == "STANDARD" and best.final_score >= STANDARD_ENTRY_SCORE:
            action = Action.ENTRY.value
            best.stage = "EXECUTABLE"
        elif execution_lane == "REENTRY" and best.final_score >= EARLY_ENTRY_SCORE:
            action = Action.RISKY_ENTRY.value
            best.stage = "EXECUTABLE_REENTRY"
    if not best.trigger_ready:
        reason = "ICT-локація і структура готові; потрібен свіжий закритий 3M-тригер"
    elif not plan.valid:
        reason = f"сетап готовий, але поточна ціна не дає безпечної геометрії: {plan.reason}"
    elif best.late_extension_atr > LATE_EXTENSION_ATR15:
        reason = "первинний імпульс пропущено; сценарій збережено для повторного входу після нового відкату й утримання рівня"
    elif action in {Action.ENTRY.value, Action.RISKY_ENTRY.value}:
        reason = f"{SETUP_LABELS.get(best.setup_type, best.setup_type)} підтверджено єдиним ICT-рішенням"

    return Decision(
        id=uuid.uuid4().hex[:12],
        time=iso_now(),
        action=action,
        side=best.side,
        setup_type=best.setup_type,
        quality=best.final_score,
        reason=reason,
        regime=context["regime"],
        candidate=best,
        plan=plan,
        audit={
            "candidates": audit_candidates,
            "selected": {"side": best.side, "setup_type": best.setup_type, "score": best.final_score, "execution_lane": best.execution_lane, "stage": best.stage},
            "direction_margin": best.final_score - (strongest_opposite.final_score if strongest_opposite else 0),
            "invariants": {
                "single_selector": True,
                "single_geometry_builder": True,
                "no_post_decision_mutation": True,
                "news_cannot_create_direction": True,
                "flow_cannot_create_direction": True,
            },
        },
    )

# ==========================================================
# ACTIVE TRADE MANAGEMENT — SIMPLE STATE MACHINE
# ==========================================================


def new_active_trade(decision: Decision) -> ActiveTrade:
    if not decision.plan or not decision.candidate:
        raise ValueError("Entry decision has no plan/candidate")
    plan = decision.plan
    return ActiveTrade(
        id=uuid.uuid4().hex[:10],
        side=decision.side,
        setup_type=decision.setup_type,
        opened_at=decision.time,
        entry=plan.entry,
        stop_initial=plan.stop,
        stop_current=plan.stop,
        structural_invalidation=decision.candidate.invalidation_level,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        quality=decision.quality,
        position_risk_pct=plan.position_risk_pct,
        best_price=plan.entry,
        worst_price=plan.entry,
        last_action=decision.action,
        notes=[
            f"ENTRY_ACTION:{decision.action}",
            f"REGIME:{decision.regime}",
            f"RR:{plan.rr1}/{plan.rr2}/{plan.rr3}",
        ],
    )


def trade_pct(trade: ActiveTrade, price: float) -> float:
    sign = side_sign(trade.side)
    return sign * (price - trade.entry) / trade.entry * 100.0 if trade.entry else 0.0


def trade_r_multiple(trade: ActiveTrade, price: float) -> float:
    risk = abs(trade.entry - trade.stop_initial)
    return side_sign(trade.side) * (price - trade.entry) / risk if risk else 0.0


def update_trade_extremes(trade: ActiveTrade, candles: list[Candle], current_price: float) -> None:
    for candle in candles:
        if trade.side == Side.LONG.value:
            trade.best_price = max(trade.best_price, candle.high)
            trade.worst_price = min(trade.worst_price, candle.low)
        else:
            trade.best_price = min(trade.best_price, candle.low)
            trade.worst_price = max(trade.worst_price, candle.high)
    if trade.side == Side.LONG.value:
        trade.best_price = max(trade.best_price, current_price)
        trade.worst_price = min(trade.worst_price, current_price)
    else:
        trade.best_price = min(trade.best_price, current_price)
        trade.worst_price = max(trade.worst_price, current_price)


def recent_candles_for_trade(trade: ActiveTrade, candles_3m: list[Candle]) -> list[Candle]:
    opened = parse_iso(trade.opened_at)
    opened_ms = int(opened.timestamp() * 1000) if opened else 0
    minimum_ts = max(opened_ms, int(trade.last_checked_3m_ts or 0))
    result = [c for c in closed(candles_3m) if c.ts > minimum_ts]
    if result:
        trade.last_checked_3m_ts = result[-1].ts
    return result


def hit_level(candle: Candle, level: float) -> bool:
    return candle.low <= level <= candle.high


def latest_protective_swing(candles: list[Candle], side: str, fallback: float) -> float:
    structure = structure_snapshot(candles, "management")
    value = safe_float(structure.get("swing_low" if side == Side.LONG.value else "swing_high"))
    return value or fallback


def stop_after_tp1(trade: ActiveTrade, context: dict[str, Any], current_price: float) -> float:
    atr15 = max(safe_float(context["tf15"].get("atr")), trade.entry * 0.001)
    swing = latest_protective_swing(context["candles"]["3m"], trade.side, trade.entry)
    if trade.side == Side.LONG.value:
        planned = max(trade.entry + 0.35 * (trade.tp1 - trade.entry), swing - 0.10 * atr15)
        planned = min(planned, current_price - 0.25 * atr15)
        return round_price(max(trade.entry, planned))
    planned = min(trade.entry - 0.35 * (trade.entry - trade.tp1), swing + 0.10 * atr15)
    planned = max(planned, current_price + 0.25 * atr15)
    return round_price(min(trade.entry, planned))


def stop_after_tp2(trade: ActiveTrade, context: dict[str, Any], current_price: float) -> float:
    atr15 = max(safe_float(context["tf15"].get("atr")), trade.entry * 0.001)
    swing = latest_protective_swing(context["candles"]["15m"], trade.side, trade.tp1)
    if trade.side == Side.LONG.value:
        planned = max(trade.tp1 - 0.10 * atr15, swing - 0.10 * atr15)
        planned = min(planned, current_price - 0.30 * atr15)
        return round_price(max(trade.stop_current, planned))
    planned = min(trade.tp1 + 0.10 * atr15, swing + 0.10 * atr15)
    planned = max(planned, current_price + 0.30 * atr15)
    return round_price(min(trade.stop_current, planned))


def thesis_broken(trade: ActiveTrade, context: dict[str, Any]) -> bool:
    c15 = closed(context["candles"]["15m"])
    if len(c15) < 2:
        return False
    if trade.side == Side.LONG.value:
        closed_break = c15[-1].close < trade.structural_invalidation and c15[-2].close < trade.structural_invalidation
    else:
        closed_break = c15[-1].close > trade.structural_invalidation and c15[-2].close > trade.structural_invalidation
    return bool(closed_break and context["s3"].get("bias") == opposite(trade.side))


def close_result(trade: ActiveTrade, action: str, exit_price: float, reason: str, context: dict[str, Any]) -> dict[str, Any]:
    result_pct = trade_pct(trade, exit_price)
    mfe_pct = max(0.0, trade_pct(trade, trade.best_price))
    mae_pct = abs(min(0.0, trade_pct(trade, trade.worst_price)))
    trade.status = "CLOSED"
    trade.last_action = action
    return {
        "closed": True,
        "action": action,
        "side": trade.side,
        "title": f"{trade.side} — {action}",
        "reason": reason,
        "exit_price": round_price(exit_price),
        "current_pct": round(result_pct, 3),
        "leveraged_pct": round(result_pct * LEVERAGE, 3),
        "mfe_pct": round(mfe_pct, 3),
        "mae_pct": round(mae_pct, 3),
        "trade": asdict(trade),
        "context_price": context["price"],
    }


def manage_active_trade(trade: ActiveTrade, context: dict[str, Any]) -> dict[str, Any]:
    current_price = context["price"]
    recent = recent_candles_for_trade(trade, context["candles"]["3m"])
    update_trade_extremes(trade, recent, current_price)

    # Chronological event processing. If stop and target are inside the same
    # candle before any target was secured, the conservative stop-first rule is
    # used because intrabar order is unknown.
    event_action = ""
    event_price = 0.0
    for candle in recent:
        stop_hit = hit_level(candle, trade.stop_current)
        tp1_hit = (not trade.tp1_hit) and hit_level(candle, trade.tp1)
        tp2_hit = trade.tp1_hit and (not trade.tp2_hit) and hit_level(candle, trade.tp2)
        tp3_hit = trade.tp2_hit and (not trade.tp3_hit) and hit_level(candle, trade.tp3)
        if stop_hit and (tp1_hit or tp2_hit or tp3_hit):
            return close_result(trade, Action.STOP.value, trade.stop_current, "стоп і ціль були в одній 3M свічці; застосовано консервативний порядок", context)
        if stop_hit:
            return close_result(trade, Action.STOP.value, trade.stop_current, "фактичний structural/profit stop зачеплено", context)
        if tp1_hit:
            trade.tp1_hit = True
            event_action = Action.TP1.value
            event_price = trade.tp1
        if tp2_hit:
            trade.tp2_hit = True
            event_action = Action.TP2.value
            event_price = trade.tp2
        if tp3_hit:
            trade.tp3_hit = True
            return close_result(trade, Action.TP3.value, trade.tp3, "TP3 досягнуто", context)

    # Direct current-price fallback if there are no fresh candles in a delayed run.
    if trade.side == Side.LONG.value and current_price <= trade.stop_current:
        return close_result(trade, Action.STOP.value, trade.stop_current, "поточна ціна нижче стопа", context)
    if trade.side == Side.SHORT.value and current_price >= trade.stop_current:
        return close_result(trade, Action.STOP.value, trade.stop_current, "поточна ціна вище стопа", context)

    if trade.tp1_hit and not trade.tp1_stop_locked:
        trade.stop_current = stop_after_tp1(trade, context, current_price)
        trade.tp1_stop_locked = True
        trade.last_action = Action.TP1.value
        trade.notes.append(f"TP1_STOP_LOCK:{trade.stop_current}")
        event_action = Action.TP1.value
        event_price = event_price or trade.tp1
    if trade.tp2_hit and not trade.tp2_stop_locked:
        trade.stop_current = stop_after_tp2(trade, context, current_price)
        trade.tp2_stop_locked = True
        trade.last_action = Action.TP2.value
        trade.notes.append(f"TP2_STOP_LOCK:{trade.stop_current}")
        event_action = Action.TP2.value
        event_price = event_price or trade.tp2

    if thesis_broken(trade, context):
        return close_result(trade, Action.EXIT.value, current_price, "два закриті 15M close зламали ICT-інвалідацію та 3M підтвердив напрям проти", context)

    current_pct = trade_pct(trade, current_price)
    mfe_pct = max(0.0, trade_pct(trade, trade.best_price))
    giveback_ratio = (mfe_pct - current_pct) / mfe_pct if mfe_pct > 0 else 0.0
    structure_against_3m = context["s3"].get("bos") == opposite(trade.side) or context["s3"].get("choch") == opposite(trade.side)
    structure_against_15m = context["s15"].get("bos") == opposite(trade.side) or context["s15"].get("choch") == opposite(trade.side)

    # Before TP1 there is no noise-based stop tightening. A full exit requires
    # meaningful MFE plus a confirmed 15M structural reversal.
    if not trade.tp1_hit and trade_r_multiple(trade, trade.best_price) >= 1.0 and giveback_ratio >= 0.72 and structure_against_15m:
        return close_result(trade, Action.EXIT.value, current_price, "після ≥1R ринок віддав понад 72% MFE і 15M структура зламалася", context)

    action = event_action or Action.HOLD.value
    reason = "структурна теза чинна; до TP1 стоп не рухається через локальний шум"
    if trade.tp1_hit:
        reason = "TP1 зафіксовано; встановлено один захисний стоп, повторно до TP2 він не перераховується"
    if trade.tp2_hit:
        reason = "TP2 зафіксовано; стоп один раз перенесено за 15M/ICT структуру"
    if trade.tp1_hit and giveback_ratio >= 0.45 and structure_against_3m:
        action = Action.PROTECT.value
        reason = "після TP1 є MFE giveback і 3M структура проти; захисний стоп уже зафіксований"
    if trade.tp1_hit and giveback_ratio >= 0.65 and structure_against_15m:
        return close_result(trade, Action.EXIT.value, current_price, "після TP1 віддано понад 65% MFE і 15M підтвердив зворотний BOS/CHOCH", context)

    trade.last_action = action
    return {
        "closed": False,
        "action": action,
        "side": trade.side,
        "title": f"{trade.side} — {action}",
        "reason": reason,
        "event_price": round_price(event_price),
        "current_price": round_price(current_price),
        "current_pct": round(current_pct, 3),
        "leveraged_pct": round(current_pct * LEVERAGE, 3),
        "mfe_pct": round(mfe_pct, 3),
        "giveback_ratio": round(giveback_ratio, 3),
        "stop_current": round_price(trade.stop_current),
        "tp1_hit": trade.tp1_hit,
        "tp2_hit": trade.tp2_hit,
        "tp3_hit": trade.tp3_hit,
        "trade": asdict(trade),
    }


# ==========================================================
# JOURNAL / ANALYTICS
# ==========================================================


def journal_trade_close(trade: ActiveTrade, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": trade.id,
        "opened_at": trade.opened_at,
        "closed_at": iso_now(),
        "side": trade.side,
        "setup_type": trade.setup_type,
        "entry": trade.entry,
        "exit_price": result.get("exit_price"),
        "stop_initial": trade.stop_initial,
        "stop_final": trade.stop_current,
        "tp1": trade.tp1,
        "tp2": trade.tp2,
        "tp3": trade.tp3,
        "quality": trade.quality,
        "result_action": result.get("action"),
        "result_pct": result.get("current_pct"),
        "leveraged_pct": result.get("leveraged_pct"),
        "mfe_pct": result.get("mfe_pct"),
        "mae_pct": result.get("mae_pct"),
        "position_risk_pct": trade.position_risk_pct,
        "reason": result.get("reason"),
    }


def compute_analytics(journal: dict[str, Any]) -> dict[str, Any]:
    trades = list(journal.get("trades") or [])
    closed = [t for t in trades if t.get("result_pct") is not None]
    wins = [t for t in closed if safe_float(t.get("result_pct")) > 0]
    losses = [t for t in closed if safe_float(t.get("result_pct")) < 0]
    total_pct = sum(safe_float(t.get("result_pct")) for t in closed)
    leveraged = sum(safe_float(t.get("leveraged_pct")) for t in closed)
    by_setup: dict[str, dict[str, Any]] = {}
    for trade in closed:
        key = str(trade.get("setup_type") or "UNKNOWN")
        bucket = by_setup.setdefault(key, {"trades": 0, "wins": 0, "result_pct": 0.0})
        bucket["trades"] += 1
        bucket["wins"] += int(safe_float(trade.get("result_pct")) > 0)
        bucket["result_pct"] += safe_float(trade.get("result_pct"))
    for bucket in by_setup.values():
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"] * 100, 1) if bucket["trades"] else 0.0
        bucket["result_pct"] = round(bucket["result_pct"], 3)
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "net_result_pct": round(total_pct, 3),
        "net_leveraged_pct": round(leveraged, 3),
        "by_setup": by_setup,
    }

# ==========================================================
# TELEGRAM / PRESENTATION
# ==========================================================


def side_icon(side: str) -> str:
    return "🟢" if side == Side.LONG.value else "🔴" if side == Side.SHORT.value else "⚪"


def action_label(action: str) -> str:
    return {
        Action.ENTRY.value: "ВХІД",
        Action.RISKY_ENTRY.value: "РИЗИКОВАНИЙ РАННІЙ ВХІД",
        Action.ARMED.value: "ICT-СЦЕНАРІЙ СФОРМОВАНИЙ",
        Action.NO_SETUP.value: "ЧИСТОГО СЕТАПУ НЕМАЄ",
        Action.HOLD.value: "УТРИМУВАТИ",
        Action.PROTECT.value: "ЗАХИСТ ПРИБУТКУ",
        Action.TP1.value: "TP1 ДОСЯГНУТО",
        Action.TP2.value: "TP2 ДОСЯГНУТО",
        Action.TP3.value: "TP3 ДОСЯГНУТО",
        Action.STOP.value: "СТОП",
        Action.EXIT.value: "ВИХІД ЗА ЗЛАМОМ ТЕЗИ",
    }.get(action, action)


REGIME_LABELS_UA = {
    Regime.TREND.value: "ТРЕНД",
    Regime.RANGE.value: "ДІАПАЗОН",
    Regime.TRANSITION.value: "ПЕРЕХІДНИЙ РЕЖИМ",
    Regime.SHOCK.value: "ІМПУЛЬСНИЙ РЕЖИМ",
    Regime.NORMAL.value: "ЗВИЧАЙНИЙ РИНОК",
}


def regime_label_ua(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().upper()
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return REGIME_LABELS_UA.get(raw, raw or "НЕВИЗНАЧЕНИЙ")


_TELEGRAM_REMOVED_PREFIXES = (
    "Сімейство:",
    "Стадія:",
    "Основа стопа:",
    "Старший сценарій:",
    "Прийняття:",
    "Основа тейків:",
    "Протилежна зона:",
    "Проміжні бар'єри:",
    "Проміжні барєри:",
    "Оптимальний вхід:",
    "Краща ціна для входу:",
    "Контрольні ліквідності:",
    "Скасування:",
    "Ризики:",
)


def sanitize_telegram_message(text: str) -> str:
    """Presentation-only cleanup. Internal plan/audit fields remain intact."""
    kept: list[str] = []
    for line in str(text).splitlines():
        plain = line.replace("<b>", "").replace("</b>", "").strip()
        if any(plain.startswith(prefix) for prefix in _TELEGRAM_REMOVED_PREFIXES):
            continue
        if plain.startswith("⚠️"):
            continue
        kept.append(line)
    # Collapse excessive blank lines after removing blocks.
    result: list[str] = []
    for line in kept:
        if not line.strip() and result and not result[-1].strip():
            continue
        result.append(line)
    return "\n".join(result).strip()


def candidate_reason_lines(candidate: Optional[Candidate], limit: int = 4) -> list[str]:
    if not candidate:
        return []
    return [f"✅ {item}" for item in candidate.confirmations[:limit]]


def build_decision_message(context: dict[str, Any], decision: Decision) -> str:
    title = f"{side_icon(decision.side)} {decision.side if decision.side != Side.NEUTRAL.value else ''} — {action_label(decision.action)}".replace("  ", " ")
    lines = ["<b>BZU SIGNAL BOT — CLEAN ICT</b>", "", f"<b>{html.escape(title)}</b>"]
    if decision.side != Side.NEUTRAL.value:
        lines.extend([
            f"Сетап: {html.escape(SETUP_LABELS.get(decision.setup_type, decision.setup_type))}",
            f"Ціна: {round_price(context['price'])}",
            f"Якість: {decision.quality}/100",
            f"Режим: {html.escape(regime_label_ua(decision.regime))}",
        ])
    else:
        lines.extend([
            f"Ціна: {round_price(context['price'])}",
            f"Режим: {html.escape(regime_label_ua(decision.regime))}",
        ])
    lines.extend(["", f"Причина: {html.escape(decision.reason)}"])
    if decision.candidate:
        reasons = candidate_reason_lines(decision.candidate)
        if reasons:
            lines.extend(["", "Підтвердження:", *[html.escape(x) for x in reasons]])
    if decision.plan and decision.action in {Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.ARMED.value}:
        p = decision.plan
        lines.extend([
            "",
            "План:",
            f"Вхід: {p.entry}",
            f"Стоп: {p.stop}",
            f"TP1: {p.tp1}",
            f"TP2: {p.tp2}",
            f"TP3: {p.tp3}",
            f"RR: {p.rr1} / {p.rr2} / {p.rr3}",
            f"Ризик позиції: {p.position_risk_pct}%",
        ])
    return "\n".join(lines)


def build_follow_message(trade: ActiveTrade, result: dict[str, Any]) -> str:
    lines = [
        "<b>BZU SIGNAL BOT — CLEAN ICT</b>",
        "",
        f"<b>{side_icon(trade.side)} {trade.side} — {html.escape(action_label(str(result.get('action'))))}</b>",
        f"Сетап: {html.escape(SETUP_LABELS.get(trade.setup_type, trade.setup_type))}",
        f"Ціна: {result.get('exit_price') or result.get('current_price')}",
        f"Результат: {result.get('current_pct', 0)}% | з плечем {result.get('leveraged_pct', 0)}%",
        f"MFE: {result.get('mfe_pct', 0)}%",
        "",
        f"Дія: {html.escape(str(result.get('reason') or ''))}",
    ]
    if not result.get("closed"):
        lines.extend([
            f"Стоп: {result.get('stop_current')}",
            f"TP1/TP2/TP3: {trade.tp1} / {trade.tp2} / {trade.tp3}",
        ])
    return "\n".join(lines)


def message_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def should_send_message(state: dict[str, Any], key: str, force: bool = False) -> bool:
    if force or SEND_DUPLICATE_STATUS:
        state["last_message_key"] = key
        return True
    if state.get("last_message_key") == key:
        return False
    state["last_message_key"] = key
    return True


def send_telegram(text: str) -> None:
    text = sanitize_telegram_message(text)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[INFO] Telegram credentials absent; message not sent")
        print(text.replace("<b>", "").replace("</b>", ""))
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if not response.ok:
            print(f"[WARN] Telegram failed {response.status_code}: {response.text[:300]}")
    except Exception as exc:
        print(f"[WARN] Telegram exception: {exc}")


def public_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": context["time"],
        "price": context["price"],
        "price_source": context["price_source"],
        "reference_price": context.get("reference_price"),
        "cross_price_diff_pct": context.get("cross_price_diff_pct"),
        "regime": context["regime"],
        "regime_reason": context["regime_reason"],
        "tf3": context["tf3"],
        "tf15": context["tf15"],
        "tf1h": context["tf1h"],
        "tf4h": context["tf4h"],
        "sweep3": context["sweep3"],
        "sweep15": context["sweep15"],
        "dealing_range": context["dealing_range"],
        "flow": context["flow"],
        "derivatives": context["derivatives"],
        "scan_3m": {
            "last_scanned_3m_ts": int((context.get("scan_3m") or {}).get("last_scanned_3m_ts") or 0),
            "last_run_processed": int((context.get("scan_3m") or {}).get("last_run_processed") or 0),
            "processed_count": int((context.get("scan_3m") or {}).get("processed_count") or 0),
            "events": dict((context.get("scan_3m") or {}).get("events") or {}),
        },
        "zones": [asdict(z) for z in context["zones"][-12:]],
    }


def decision_payload(decision: Decision, context: dict[str, Any]) -> dict[str, Any]:
    payload = asdict(decision)
    payload["version"] = BOT_VERSION
    payload["architecture_version"] = ARCHITECTURE_VERSION
    payload["context"] = public_context(context)
    return payload


# ==========================================================
# MAIN
# ==========================================================


def run_bot() -> None:
    print(f"START {BOT_VERSION}")
    state = load_state()
    journal = load_journal()
    data = collect_market_data()
    context = build_context(data, state)
    active = active_trade_from_state(state)

    if active:
        result = manage_active_trade(active, context)
        state["latest_signal"] = {
            "version": BOT_VERSION,
            "architecture_version": ARCHITECTURE_VERSION,
            "time": iso_now(),
            "type": "CLOSE" if result.get("closed") else "FOLLOW",
            "action": result.get("action"),
            "side": active.side,
            "setup_type": active.setup_type,
            "price": result.get("exit_price") or result.get("current_price"),
            "result": result,
            "context": public_context(context),
        }
        append_history(state, {
            "type": "CLOSE" if result.get("closed") else "FOLLOW",
            "side": active.side,
            "action": result.get("action"),
            "price": result.get("exit_price") or result.get("current_price"),
            "result_pct": result.get("current_pct"),
            "stop_current": active.stop_current,
        })
        journal["signals"].append(state["latest_signal"])
        if result.get("closed"):
            journal["trades"].append(journal_trade_close(active, result))
            store_active_trade(state, None)
            state["opportunity"] = None
        else:
            store_active_trade(state, active)
        key = message_key({"action": result.get("action"), "side": active.side, "stop": active.stop_current, "tp1": active.tp1_hit, "tp2": active.tp2_hit})
        force = bool(result.get("closed") or result.get("action") in {Action.TP1.value, Action.TP2.value, Action.TP3.value, Action.STOP.value, Action.EXIT.value})
        if should_send_message(state, key, force=force):
            send_telegram(build_follow_message(active, result))
        save_state(state)
        save_journal(journal)
        print("BOT COMPLETE: ACTIVE TRADE")
        return

    decision = evaluate_new_setup(context, state)
    payload = decision_payload(decision, context)
    state["latest_signal"] = payload
    append_history(state, {
        "type": decision.action,
        "side": decision.side,
        "setup_type": decision.setup_type,
        "price": context["price"],
        "quality": decision.quality,
        "reason": decision.reason,
        "plan": asdict(decision.plan) if decision.plan else None,
    })
    journal["signals"].append(payload)

    if decision.action in {Action.ENTRY.value, Action.RISKY_ENTRY.value}:
        active = new_active_trade(decision)
        store_active_trade(state, active)
        state["opportunity"] = None
    elif decision.action == Action.ARMED.value and decision.candidate:
        state["opportunity"] = asdict(make_opportunity(decision.candidate))
        store_active_trade(state, None)
    else:
        saved = opportunity_from_state(state)
        if not saved or not opportunity_is_valid(saved, context):
            state["opportunity"] = None
        store_active_trade(state, None)

    key = message_key({
        "action": decision.action,
        "side": decision.side,
        "setup_type": decision.setup_type,
        "quality_bucket": decision.quality // 5,
        "plan_valid": decision.plan.valid if decision.plan else None,
    })
    force = decision.action in {Action.ENTRY.value, Action.RISKY_ENTRY.value}
    send_allowed = decision.action != Action.NO_SETUP.value or SEND_NO_SETUP
    if send_allowed and should_send_message(state, key, force=force):
        send_telegram(build_decision_message(context, decision))

    save_state(state)
    save_journal(journal)
    print("BOT COMPLETE")


# ==========================================================
# SELF TESTS
# ==========================================================


def synthetic_candles(base: float, count: int, step: float = 0.05, interval_ms: int = 180_000) -> list[Candle]:
    start = int(now_utc().timestamp() * 1000) - count * interval_ms
    candles: list[Candle] = []
    price = base
    for i in range(count):
        close_price = price + step
        candles.append(Candle(
            ts=start + i * interval_ms,
            open=price,
            high=max(price, close_price) + 0.08,
            low=min(price, close_price) - 0.08,
            close=close_price,
            volume=100 + i,
            confirmed=True,
        ))
        price = close_price
    return candles


def run_self_test() -> None:
    # Geometry: a microscopic structural stop must be expanded once, not used to
    # manufacture absurd 10R+ plans.
    c3 = synthetic_candles(99.0, 60, 0.02)
    c15 = synthetic_candles(96.0, 60, 0.07, 900_000)
    context = {
        "price": 100.0,
        "tf15": {"atr": 1.0, "bias": Side.LONG.value},
        "tf4h": {"bias": Side.NEUTRAL.value},
        "flow": {"bias": Side.LONG.value},
        "regime": Regime.TREND.value,
        "candles": {"3m": c3, "15m": c15},
        "s3": {"bias": Side.LONG.value, "bos": Side.NEUTRAL.value, "choch": Side.NEUTRAL.value},
        "s15": {"bias": Side.LONG.value, "bos": Side.NEUTRAL.value, "choch": Side.NEUTRAL.value},
    }
    candidate = Candidate(
        side=Side.LONG.value,
        setup_type=SetupType.PULLBACK_CONTINUATION.value,
        raw_score=80,
        final_score=80,
        score_components={},
        trigger_ready=True,
        trigger_level=99.8,
        invalidation_level=99.75,
        target_levels=[100.4, 101.5, 102.4, 103.6],
    )
    plan = build_trade_plan(context, candidate)
    assert plan.stop <= 99.52 + 1e-6, plan
    assert plan.rr1 >= MIN_RR1, plan
    assert plan.tp1 < plan.tp2 < plan.tp3, plan

    opened = datetime.fromtimestamp((c3[-3].ts - 60_000) / 1000, tz=timezone.utc).isoformat()
    trade = ActiveTrade(
        id="selftest",
        side=Side.LONG.value,
        setup_type=SetupType.PULLBACK_CONTINUATION.value,
        opened_at=opened,
        entry=100.0,
        stop_initial=99.4,
        stop_current=99.4,
        structural_invalidation=99.55,
        tp1=101.0,
        tp2=102.0,
        tp3=103.0,
        quality=80,
        position_risk_pct=0.5,
        best_price=100.0,
        worst_price=100.0,
    )
    quiet_context = dict(context)
    quiet_context["price"] = 100.4
    quiet_context["candles"] = {"3m": c3[-2:], "15m": c15[-5:]}
    first = manage_active_trade(trade, quiet_context)
    assert not first["closed"]
    assert trade.stop_current == 99.4, "Pre-TP1 stop changed without structural invalidation"

    # Force a TP1 candle and verify the stop is updated once and then locked.
    tp_candle = Candle(ts=trade.last_checked_3m_ts + 180_000, open=100.4, high=101.1, low=100.2, close=100.9, volume=150)
    tp_context = dict(quiet_context)
    tp_context["price"] = 100.9
    tp_context["candles"] = {"3m": c3[-8:] + [tp_candle], "15m": c15[-8:]}
    second = manage_active_trade(trade, tp_context)
    assert not second["closed"] and trade.tp1_hit and trade.tp1_stop_locked
    locked_stop = trade.stop_current
    third = manage_active_trade(trade, tp_context)
    assert trade.stop_current == locked_stop, "TP1 stop was recalculated twice"

    # Full 3M scan: a displacement that happened between cron runs must remain
    # available even when the last 3M candle is already quiet.
    scan_candles: list[Candle] = []
    scan_start = 1_700_000_000_000
    for i in range(25):
        scan_candles.append(Candle(
            ts=scan_start + i * 180_000,
            open=99.99 if i % 2 == 0 else 100.01,
            high=100.08,
            low=99.92,
            close=100.01 if i % 2 == 0 else 99.99,
            volume=100,
            confirmed=True,
        ))
    scan_candles.append(Candle(scan_start + 25 * 180_000, 100.0, 100.65, 99.98, 100.58, 300, True))
    for j, close_price in enumerate((100.52, 100.48, 100.50, 100.47), start=26):
        scan_candles.append(Candle(scan_start + j * 180_000, 100.52, 100.56, 100.42, close_price, 100, True))
    prior_scan = {"last_scanned_3m_ts": scan_candles[24].ts, "events": {}, "processed_count": 0}
    scan_memory = scan_closed_3m_sequence(scan_candles, prior_scan)
    scanned_trigger = trigger_snapshot(scan_candles, Side.LONG.value, 0.0, scan_memory)
    assert scan_memory["last_run_processed"] == 5, scan_memory
    assert scan_memory["last_scanned_3m_ts"] == scan_candles[-1].ts, scan_memory
    assert scanned_trigger.get("ready") and scanned_trigger.get("from_full_scan"), scanned_trigger
    second_scan = scan_closed_3m_sequence(scan_candles, scan_memory)
    assert second_scan["last_run_processed"] == 0, second_scan

    # Telegram presentation cleanup must never remove internal data, only lines.
    dirty_message = "\n".join([
        "Сімейство: CONTINUATION", "Стадія: READY", "План:", "Вхід: 100",
        "Основа стопа: structural", "Старший сценарій: trend", "Прийняття: yes",
        "Основа тейків: liquidity", "Протилежна зона: FVG", "Проміжні барєри: 101",
        "Оптимальний вхід: 99.8", "Скасування: 99", "Ризики:", "⚠️ test", "Стоп: 99",
    ])
    clean_message = sanitize_telegram_message(dirty_message)
    for removed in (
        "Сімейство", "Стадія", "Основа стопа", "Старший сценарій", "Прийняття",
        "Основа тейків", "Протилежна зона", "Проміжні барєри", "Оптимальний вхід",
        "Скасування", "Ризики", "⚠️",
    ):
        assert removed not in clean_message, (removed, clean_message)

    print("SELF TEST PASSED")
    print(json.dumps({
        "plan": asdict(plan),
        "tp1_result": second,
        "repeat_result": third,
        "full_3m_scan": {
            "processed": scan_memory["last_run_processed"],
            "trigger": scanned_trigger,
        },
        "telegram_message": clean_message,
    }, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="BZU Clean ICT signal bot")
    parser.add_argument("--self-test", action="store_true", help="run deterministic internal tests without network")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
    else:
        run_bot()


if __name__ == "__main__":
    main()
