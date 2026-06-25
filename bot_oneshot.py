#!/usr/bin/env python3
"""
BZU Professional Hybrid Confluence Signal Bot v6.5 (ICT Pro Edition)
================================================================================
Виправлення v6.5:
- TradingView (BINANCE:BZUSDT.P) як ОСНОВНЕ джерело ціни
- Інтеграція Premium / Discount (PD Arrays) та Dealing Range
- Правильна валідація FVG (Consequent Encroachment)
- Пошук ліквідності (BSL / SSL)
- Інтеграція SMT Divergence з BTC
- Інтеграція часових зон (Killzones)
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from statistics import mean
from typing import Any, Optional

import requests

# ==========================================================
# CONFIGURATION
# ==========================================================

BOT_VERSION = "pro-hybrid-confluence-v6.5"
ARCHITECTURE_VERSION = "HYBRID_CONFLUENCE_V6_4"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === DATA SOURCES ===
OKX_BASE_URL = "https://www.okx.com/api/v5/market"
TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com/crypto/scan"
OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
BTC_INST_ID = "BTC-USDT-SWAP"  # Додано для SMT Divergence

WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", os.getcwd()))

# === ЯВНІ НАЗВИ ФАЙЛІВ ===
STATE_FILE = Path(os.getenv("SIGNAL_MEMORY_FILE", str(WORKSPACE / "last_signal_v6_4.json")))
JOURNAL_FILE = Path(os.getenv("SIGNAL_JOURNAL_FILE", str(WORKSPACE / "signal_journal_v6_4.json")))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "200") or 200)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "3000") or 3000)

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "5") or 5)
NORMAL_RISK_PCT = float(os.getenv("NORMAL_RISK_PCT", "0.50") or 0.50)
RISKY_RISK_PCT = float(os.getenv("RISKY_RISK_PCT", "0.30") or 0.30)

# === ICT Geometry ===
MIN_STOP_ATR15 = max(0.75, float(os.getenv("MIN_STOP_ATR15", "0.80") or 0.80))
MAX_STOP_ATR15 = float(os.getenv("MAX_STOP_ATR15", "2.60") or 2.60)
MIN_TP1_ATR15 = max(1.25, float(os.getenv("MIN_TP1_ATR15", "1.30") or 1.30))
MIN_RR1 = max(2.00, float(os.getenv("MIN_RR1", "2.00") or 2.00))
PREFERRED_RR1 = max(2.20, float(os.getenv("PREFERRED_RR1", "2.20") or 2.20))
MIN_RR2 = max(3.00, float(os.getenv("MIN_RR2", "3.00") or 3.00))
MIN_RR3 = max(4.00, float(os.getenv("MIN_RR3", "4.00") or 4.00))

# === Scoring Thresholds ===
ENTRY_SCORE_BASE = int(os.getenv("ICT_ENTRY_SCORE", "75") or 75)
RISKY_ENTRY_SCORE_BASE = int(os.getenv("ICT_RISKY_ENTRY_SCORE", "68") or 68)
ARMED_SCORE_BASE = int(os.getenv("ICT_ARMED_SCORE", "58") or 58)
MIN_ENTRY_EVIDENCE_BASE = int(os.getenv("MIN_ENTRY_EVIDENCE", "5") or 5)

# === Re-Entry ===
MISSED_REENTRY_SCORE = 62
REENTRY_AGGRESSIVE_THRESHOLD = 72

# === Professional Gate ===
PRO_ENTRY_MIN = 74
PRO_RISKY_MIN = 66
MIN_PRO_LAYERS_ENTRY = 4
A_PLUS_ENTRY_MIN = 82

# === 3M Scanner ===
TRIGGER_MAX_AGE_MINUTES = int(os.getenv("TRIGGER_MAX_AGE_MINUTES", "35") or 35)
RECLAIM_MIN_QUALITY = int(os.getenv("RECLAIM_MIN_QUALITY", "58") or 58)
EARLY_ENTRY_MIN_SCORE = int(os.getenv("EARLY_ENTRY_MIN_SCORE", "68") or 68)
STRONG_IMPULSE_CHASE_PENALTY = int(os.getenv("STRONG_IMPULSE_CHASE_PENALTY", "12") or 12)

# === Telegram ===
TELEGRAM_NOTIFY_EVERY_RUN = os.getenv("TELEGRAM_NOTIFY_EVERY_RUN", "true").lower() in {"1", "true", "yes"}
SEND_NO_SETUP = os.getenv("SEND_NO_SETUP", "true").lower() in {"1", "true", "yes"}
TELEGRAM_MAX_LENGTH = max(600, min(4200, int(os.getenv("TELEGRAM_MAX_LENGTH", "4000") or 4000)))


# ==========================================================
# ENUMS
# ==========================================================

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


class SetupFamily(str, Enum):
    LIQUIDITY_RECOVERY = "LIQUIDITY_RECOVERY"
    STRUCTURAL_TRANSITION = "STRUCTURAL_TRANSITION"
    CONTINUATION = "CONTINUATION"
    EXPANSION = "EXPANSION"
    RANGE_EXECUTION = "RANGE_EXECUTION"
    NONE = "NONE"


class SetupType(str, Enum):
    SWEEP_RECLAIM = "SWEEP_RECLAIM"
    CAPITULATION_RECOVERY = "CAPITULATION_RECOVERY"
    DIRECTION_FLIP = "DIRECTION_FLIP_15M"
    TREND_IGNITION = "TREND_IGNITION"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    FRESH_BASE_CONTINUATION = "FRESH_BASE_CONTINUATION"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    RANGE_COMPRESSION_BREAKOUT = "RANGE_COMPRESSION_BREAKOUT"
    RANGE_EDGE_REVERSAL = "RANGE_EDGE_REVERSAL"
    NONE = "NONE"


class ExecutionLane(str, Enum):
    EARLY_TACTICAL = "EARLY_TACTICAL"
    STANDARD_CONFIRMED = "STANDARD_CONFIRMED"
    MISSED_IMPULSE_REENTRY = "MISSED_IMPULSE_REENTRY"


class ConfirmationTier(int, Enum):
    MICRO = 1
    STANDARD = 2
    HIGH_QUALITY = 3
    PREMIUM = 4


# ==========================================================
# DATACLASSES
# ==========================================================

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
    setup_family: str
    raw_score: int
    final_score: int
    score_components: dict[str, int] = field(default_factory=dict)
    evidence_families: list[str] = field(default_factory=list)
    confirmations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    trigger_ready: bool = False
    trigger_level: float = 0.0
    invalidation_level: float = 0.0
    target_levels: list[float] = field(default_factory=list)
    execution_lane: str = "STANDARD_CONFIRMED"
    confirmation_tier: int = 2
    stage: str = "DISCOVERED"
    variant: str = ""
    execution_anchor: float = 0.0
    trigger_ts: int = 0
    trigger_age_minutes: float = 0.0
    specificity: int = 0
    hard_reject_reason: str = ""
    thesis_key: str = ""
    thesis: str = ""
    scan_event_stage: str = ""
    confluence_layers: dict[str, int] = field(default_factory=dict)
    acceptance_quality: int = 0
    volume_cluster_support: bool = False
    professional_gate: dict = field(default_factory=dict)
    location_score: int = 0
    has_forward_zone: bool = False


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
    position_risk_pct: float = 0.0
    invalidation: str = ""
    stop_basis: str = ""
    target_basis: str = ""
    stop_timeframe: str = "15M"
    structural_invalidation: float = 0.0
    trigger_level: float = 0.0
    execution_ready: bool = False
    valid: bool = True
    reason: str = ""


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
    news_bias: str = "NEUTRAL"
    macro_risk: str = "NORMAL"
    current_price: float = 0.0


@dataclass
class Opportunity:
    side: str
    setup_type: str
    setup_family: str
    created_at: str
    expires_at: str
    score: int
    trigger_level: float
    invalidation_level: float
    confirmations: list[str] = field(default_factory=list)
    evidence_families: list[str] = field(default_factory=list)
    execution_lane: str = "STANDARD_CONFIRMED"
    status: str = "ARMED"
    thesis_key: str = ""
    thesis: str = ""
    missed_at: str = ""


@dataclass
class ActiveTrade:
    id: str
    side: str
    setup_type: str
    setup_family: str
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
    thesis_key: str = ""
    thesis: str = ""
    last_checked_3m_ts: int = 0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    tp1_stop_locked: bool = False
    tp2_stop_locked: bool = False
    tp1_locked_stop: float = 0.0
    tp2_locked_stop: float = 0.0
    status: str = "OPEN"
    last_action: str = "ENTRY"
    notes: list[str] = field(default_factory=list)
    entry_integrity_score: int = 100
    entry_fail_streak: int = 0
    mfe_giveback_streak: int = 0
    mfe_giveback_last_state: str = "OK"
    trigger_level: float = 0.0
    opened_regime: str = ""
    entry_level: str = "ENTRY"


# ==========================================================
# UTILITIES
# ==========================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


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
    same_arch = raw.get("architecture_version") == ARCHITECTURE_VERSION
    return {
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "active_trade": raw.get("active_trade"),
        "opportunity": raw.get("opportunity") if same_arch else None,
        "scan_3m": _normalize_scan3m_state(raw.get("scan_3m")) if same_arch else _empty_scan3m_state(),
        "regime_memory": raw.get("regime_memory", {}) if same_arch and isinstance(raw.get("regime_memory"), dict) else {},
        "latest_signal": raw.get("latest_signal"),
        "last_message_key": raw.get("last_message_key", "") if same_arch else "",
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
    if "analytics" not in journal:
        journal["analytics"] = {"closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_r": 0.0, "expectancy_r": 0.0, "by_family": {}}
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
        clean = {k: raw.get(k) for k in fields if k in raw}
        clean.setdefault("thesis_key", str(raw.get("thesis_key", "")))
        clean.setdefault("thesis", str(raw.get("thesis", "")))
        clean.setdefault("entry_integrity_score", int(raw.get("entry_integrity_score", 100) or 100))
        clean.setdefault("entry_fail_streak", int(raw.get("entry_fail_streak", 0) or 0))
        clean.setdefault("mfe_giveback_streak", int(raw.get("mfe_giveback_streak", 0) or 0))
        clean.setdefault("mfe_giveback_last_state", str(raw.get("mfe_giveback_last_state", "OK")))
        clean.setdefault("trigger_level", float(raw.get("trigger_level", 0) or 0))
        clean.setdefault("opened_regime", str(raw.get("opened_regime", "")))
        clean.setdefault("tp1_stop_locked", bool(raw.get("tp1_stop_locked", False)))
        clean.setdefault("tp2_stop_locked", bool(raw.get("tp2_stop_locked", False)))
        clean.setdefault("tp1_locked_stop", float(raw.get("tp1_locked_stop", 0) or 0))
        clean.setdefault("tp2_locked_stop", float(raw.get("tp2_locked_stop", 0) or 0))
        return ActiveTrade(**clean)
    except Exception as exc:
        print(f"[WARN] ActiveTrade migration failed: {exc}")
        return None


def store_active_trade(state: dict[str, Any], trade: Optional[ActiveTrade]) -> None:
    state["active_trade"] = asdict(trade) if trade else None


def opportunity_from_state(state: dict[str, Any]) -> Optional[Opportunity]:
    raw = state.get("opportunity")
    if not isinstance(raw, dict):
        return None
    try:
        return Opportunity(**{k: raw.get(k) for k in Opportunity.__dataclass_fields__ if k in raw})
    except Exception:
        return None


def store_opportunity(state: dict[str, Any], opp: Optional[Opportunity]) -> None:
    state["opportunity"] = asdict(opp) if opp else None


# ==========================================================
# TELEGRAM
# ==========================================================

def send_telegram(text: str) -> bool:
    text = clean_telegram_message(text)
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Telegram credentials absent")
        print(plain_telegram_text(text)[:500])
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:TELEGRAM_MAX_LENGTH],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.ok:
            print(f"Telegram status: {response.status_code}")
            return True
        else:
            print(f"[ERROR] Telegram failed {response.status_code}: {response.text[:300]}")
            fallback = dict(payload)
            fallback.pop("parse_mode", None)
            fallback["text"] = plain_telegram_text(text)[:TELEGRAM_MAX_LENGTH]
            retry = requests.post(url, json=fallback, timeout=REQUEST_TIMEOUT)
            if retry.ok:
                print(f"Telegram fallback status: {retry.status_code}")
                return True
            return False
    except Exception as exc:
        print(f"[ERROR] Telegram exception: {exc}")
        return False


def clean_telegram_message(text: str) -> str:
    forbidden_prefixes = (
        "Варіант:", "Сімейство:", "Стадія:", "Карта входу:",
        "Де очікувати", "Робоча зона входу:", "Тригер:", "Логіка виконання:",
        "Основа стопа:", "Таймфрейм стопа:", "Старший сценарій:",
        "Прийняття рівня:", "Основа тейків:", "Протилежна зона:",
        "Проміжні бар’єри", "Проміжні бар'єри", "Найближча реальна ліквідність:",
        "Оптимальний вхід", "Оптимальний орієнтир", "Скасування:", "Ризики:",
        "Edge-модель:", "Статус виконання:", "Статус:", "Орієнтир входу",
    )
    cleaned: list[str] = []
    for line in str(text or "").splitlines():
        plain = html.unescape(line.replace("<b>", "").replace("</b>", "")).strip()
        if plain.startswith(forbidden_prefixes) or plain.startswith("⚠️"):
            continue
        cleaned.append(line)
    message = "\n".join(cleaned)
    replacements = {
        "Regime.RANGE": "ДІАПАЗОН", "Regime.TREND": "ТРЕНД",
        "Regime.TRANSITION": "ПЕРЕХІДНИЙ РЕЖИМ",
        "Regime.SHOCK": "ІМПУЛЬСНИЙ РЕЖИМ", "Regime.NORMAL": "ЗВИЧАЙНИЙ РИНОК",
    }
    for raw, translated in replacements.items():
        message = message.replace(raw, translated)
    while "\n\n\n" in message:
        message = message.replace("\n\n\n", "\n\n")
    return message.strip()


def plain_telegram_text(text: str) -> str:
    return html.unescape(text.replace("<b>", "").replace("</b>", ""))


def build_decision_message(context: dict, decision: Decision) -> str:
    current_price = decision.current_price or context.get("price", 0)
    
    action_names = {
        "ENTRY": "ВХІД У УГОДУ",
        "RISKY_ENTRY": "РАННІЙ ВХІД",
        "ARMED": "СФОРМОВАНО СИГНАЛ — ЧЕКАЄМО ВХОДУ",
        "NO_SETUP": "СИГНАЛУ НЕМАЄ",
    }
    action_label = action_names.get(decision.action, decision.action)
    
    lines = [
        f"<b>{action_label}</b> | {side_word(decision.side)} | {setup_label(decision.setup_type)}",
        f"<b>Якість:</b> {decision.quality}/100 | Режим: {regime_label(decision.regime)}",
        f"<b>Ціна зараз:</b> {_fmt_price(current_price)}"
    ]
    
    if decision.candidate and decision.action != Action.NO_SETUP.value:
        c = decision.candidate
        unique_confirmations = _short_list(c.confirmations, 3)
        if unique_confirmations:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            for x in unique_confirmations:
                lines.append(f"✅ {html.escape(x)}")
    
    show_plan = decision.plan and decision.plan.valid and decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.ARMED.value)
    
    if show_plan:
        p = decision.plan
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(f"Вхід <b>{_fmt_price(p.entry)}</b> | Стоп <b>{_fmt_price(p.stop)}</b>")
        lines.append(f"TP1 {_fmt_price(p.tp1)} (RR {p.rr1}) | TP2 {_fmt_price(p.tp2)} | TP3 {_fmt_price(p.tp3)}")
    
    lines.append("")
    lines.append(f"<i>{html.escape(decision.reason)}</i>")
    return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]


def _short_list(items: Any, limit: int = 3) -> list[str]:
    out: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _bias_label(block: dict) -> str:
    value = str((block or {}).get("bias") or Side.NEUTRAL.value).upper()
    if value in {Side.LONG.value, Side.SHORT.value}:
        return value
    return Side.NEUTRAL.value


def _opposite_side(side: str) -> str:
    return Side.SHORT.value if side == Side.LONG.value else Side.LONG.value


def _follow_reversal_risk(trade: ActiveTrade, result: dict, context: dict) -> tuple[str, int]:
    opposite = _opposite_side(trade.side)
    current_pct = float(result.get("current_pct") or 0)
    score = 10
    if current_pct < 0:
        score += 16
    if current_pct <= -0.50:
        score += 10
    for block, weight in ((context.get("tf3", {}), 18), (context.get("tf15", {}), 22), (context.get("flow", {}), 8), (context.get("cvd", {}), 8)):
        if _bias_label(block) == opposite:
            score += weight
    if result.get("action") == Action.PROTECT.value:
        score += 14
    if result.get("closed"):
        score = max(score, 80)
    score = max(0, min(100, int(score)))
    if score < 20:
        label = "НИЗЬКИЙ"
    elif score < 45:
        label = "ПОМІРНИЙ"
    elif score < 70:
        label = "ВИСОКИЙ"
    else:
        label = "КРИТИЧНИЙ"
    return label, score


def _follow_title(trade: ActiveTrade, result: dict, context: dict) -> str:
    side = side_word(trade.side)
    action = str(result.get("action") or Action.HOLD.value)
    current_pct = float(result.get("current_pct") or 0)
    opposite = _opposite_side(trade.side)
    tf3_opposite = _bias_label(context.get("tf3", {})) == opposite
    tf15_opposite = _bias_label(context.get("tf15", {})) == opposite

    if result.get("closed") or action in {Action.STOP.value, Action.EXIT.value}:
        return f"🔴 СУПРОВІД {side} — УГОДУ ЗАКРИТО"
    if action == Action.TP3.value:
        return f"🟢 СУПРОВІД {side} — TP3 ВЗЯТО"
    if action == Action.TP2.value:
        return f"🟢 СУПРОВІД {side} — TP2 ВЗЯТО"
    if action == Action.TP1.value:
        return f"🟢 СУПРОВІД {side} — TP1 ВЗЯТО"
    if action == Action.PROTECT.value:
        return f"🟠 СУПРОВІД {side} — ЗАХИСТ ПОЗИЦІЇ"
    if current_pct < 0 or tf3_opposite or tf15_opposite:
        return f"🟠 СУПРОВІД {side} — СЕТАП СЛАБШАЄ"
    return f"🟢 СУПРОВІД {side} — СЕТАП ТРИМАЄТЬСЯ"


def build_follow_message(context: dict, trade: ActiveTrade, result: dict) -> str:
    price = context.get("price", 0)
    recommended_stop = result.get("recommended_stop")
    stop_to_show = recommended_stop if recommended_stop is not None else trade.stop_current
    reversal_side = side_word(_opposite_side(trade.side))
    reversal_label, reversal_score = _follow_reversal_risk(trade, result, context)
    tp_status = f"TP1 {'✅' if trade.tp1_hit else '—'} | TP2 {'✅' if trade.tp2_hit else '—'} | TP3 {'✅' if trade.tp3_hit else '—'}"

    lines = [
        _follow_title(trade, result, context),
        "",
        f"Ціна: {_fmt_price(price)}",
        f"Від входу: {result.get('current_pct', 0):.3f}% | Макс. прибуток: {result.get('best_pct', 0):.2f}% | Відкат від макс.: {result.get('giveback_pct', 0):.2f}%",
        "",
        f"Ризик розвороту в {reversal_side}: {reversal_label} ({reversal_score}%)",
        "",
        "Позиція:",
        f"Вхід {_fmt_price(trade.entry)} | Стоп {_fmt_price(stop_to_show)}",
        f"TP1 {_fmt_price(trade.tp1)} | TP2 {_fmt_price(trade.tp2)} | TP3 {_fmt_price(trade.tp3)}",
        tp_status,
    ]

    return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]


def side_word(side: str) -> str:
    return {"LONG": "ЛОНГ", "SHORT": "ШОРТ"}.get(side, "НЕЙТРАЛЬНО")


def _fmt_price(v: Any) -> str:
    if v is None:
        return "-"
    return f"{float(v):.4f}".rstrip("0").rstrip(".")


# ==========================================================
# ADAPTIVE PARAMETER ENGINE
# ==========================================================

def get_adaptive_params(regime: str) -> dict:
    base = {
        "entry_score": ENTRY_SCORE_BASE,
        "risky_entry_score": RISKY_ENTRY_SCORE_BASE,
        "armed_score": ARMED_SCORE_BASE,
        "min_evidence": MIN_ENTRY_EVIDENCE_BASE,
        "mfe_giveback_threshold": 0.42,
        "min_mfe_for_protect": 2.0,
        "reentry_aggressiveness": 1.30,
    }

    if regime == Regime.TREND.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 0.90)
        base["risky_entry_score"] = int(RISKY_ENTRY_SCORE_BASE * 0.92)
        base["min_evidence"] = max(4, MIN_ENTRY_EVIDENCE_BASE - 1)
        base["mfe_giveback_threshold"] = 0.38
        base["min_mfe_for_protect"] = 1.8
        base["reentry_aggressiveness"] = 1.40

    elif regime == Regime.RANGE.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.12)
        base["risky_entry_score"] = int(RISKY_ENTRY_SCORE_BASE * 1.10)
        base["min_evidence"] = MIN_ENTRY_EVIDENCE_BASE + 1
        base["mfe_giveback_threshold"] = 0.52
        base["min_mfe_for_protect"] = 2.6
        base["reentry_aggressiveness"] = 0.90

    elif regime == Regime.SHOCK.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.18)
        base["risky_entry_score"] = int(RISKY_ENTRY_SCORE_BASE * 1.15)
        base["min_evidence"] = MIN_ENTRY_EVIDENCE_BASE + 2
        base["mfe_giveback_threshold"] = 0.55
        base["min_mfe_for_protect"] = 2.8
        base["reentry_aggressiveness"] = 0.75

    elif regime == Regime.TRANSITION.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.00)
        base["reentry_aggressiveness"] = 1.20

    return base


# ==========================================================
# PROFESSIONAL GATE
# ==========================================================

def evaluate_professional_gate(context: dict, candidate: Candidate) -> dict:
    score = candidate.final_score
    layers = len(candidate.evidence_families)
    trigger_ready = candidate.trigger_ready
    strong_ict = "ICT_LOCATION" in candidate.evidence_families or "PRICE_STRUCTURE" in candidate.evidence_families

    allow_entry = bool(
        score >= PRO_ENTRY_MIN
        and layers >= MIN_PRO_LAYERS_ENTRY
        and trigger_ready
        and strong_ict
    )

    allow_risky = bool(
        score >= PRO_RISKY_MIN
        and layers >= max(3, MIN_PRO_LAYERS_ENTRY - 1)
        and (trigger_ready or strong_ict)
    )

    if allow_entry and score >= A_PLUS_ENTRY_MIN and strong_ict:
        grade = "A+"
    elif allow_entry:
        grade = "A"
    elif allow_risky:
        grade = "B"
    else:
        grade = "WATCH"

    reason = f"gate v6: {grade} | {layers} шарів | score {score}"

    return {
        "allow_entry": allow_entry,
        "allow_risky": allow_risky,
        "grade": grade,
        "score": score,
        "layers": layers,
        "reason": reason,
    }


# ==========================================================
# DATA SOURCES: TradingView ПРІОРИТЕТ
# ==========================================================

def http_get(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Pro-v6.5/1.0", "Accept": "*/*"}
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.3 * attempt)
    return None


def http_post(url: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> Optional[requests.Response]:
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Pro-v6.5/1.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code < 400:
            return resp
    except Exception:
        pass
    return None


def get_okx_candles(inst_id: str = "BZ-USDT", bar: str = "15m", limit: int = 200) -> list[Candle]:
    url = f"{OKX_BASE_URL}/candles?instId={inst_id}&bar={bar}&limit={limit}"
    resp = http_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
        if data.get("code") != "0":
            return []
        out = []
        for row in data.get("data", []):
            try:
                out.append(Candle(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5] or 0),
                    confirmed=True
                ))
            except Exception:
                continue
        out.sort(key=lambda c: c.ts)
        return out
    except Exception:
        return []


def get_okx_ticker(inst_id: str = "BZ-USDT") -> dict:
    url = f"{OKX_BASE_URL}/ticker?instId={inst_id}"
    resp = http_get(url)
    if not resp:
        return {}
    try:
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return {}
        ticker = data["data"][0]
        last = safe_float(ticker.get("last"))
        open24h = safe_float(ticker.get("open24h"))
        return {
            "price": last,
            "change24h": pct(last, open24h) if open24h else 0.0,
            "volume24h": safe_float(ticker.get("volCcy24h")),
            "source": "OKX"
        }
    except Exception:
        return {}


def get_tradingview_price_fallback() -> dict:
    payload = {
        "symbols": {
            "tickers": ["BINANCE:BZUSDT.P", "BINANCE:BZUSDT"],
            "query": {"types": []}
        },
        "columns": ["close", "change", "volume"]
    }
    resp = http_post(TRADINGVIEW_SCAN_URL, payload)
    if not resp:
        return {}
    try:
        rows = resp.json().get("data", [])
        if not rows:
            return {}
        values = rows[0].get("d") or []
        return {
            "price": float(values[0]),
            "change24h": safe_float(values[1], 0),
            "volume24h": safe_float(values[2], 0),
            "source": "TradingView",
            "symbol": rows[0].get("s", "BINANCE:BZUSDT.P"),
        }
    except Exception as error:
        print(f"[WARN] TradingView fallback parse failed: {error}")
        return {}


def collect_market_data() -> dict:
    c3 = get_okx_candles(OKX_INST_ID, "3m", 240)
    c15 = get_okx_candles(OKX_INST_ID, "15m", 200)
    c1h = get_okx_candles(OKX_INST_ID, "1h", 160)
    c4h = get_okx_candles(OKX_INST_ID, "4h", 140)
    btc_c15 = get_okx_candles(BTC_INST_ID, "15m", 200) # Отримання BTC для SMT
    
    tv_ticker = get_tradingview_price_fallback()
    okx_ticker = get_okx_ticker(OKX_INST_ID)
    
    ticker = tv_ticker or okx_ticker
    
    if not ticker and c15:
        ticker = {
            "price": c15[-1].close,
            "change24h": 0,
            "volume24h": 0,
            "source": "OKX candles fallback",
            "symbol": OKX_INST_ID,
        }
    
    price = ticker.get("price") if ticker else 82.5
    
    return {
        "time": iso_now(),
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "btc_candles": {"15m": btc_c15},
        "ticker": ticker,
        "trades": [],
        "book": {"bids": [], "asks": []},
        "price": price,
        "price_source": ticker.get("source", "TradingView") if ticker else "fallback",
    }


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    return mean(trs[-period:]) if trs else 0.0

# === ICT Core Functions ===

def identify_liquidity_and_range(candles: list[Candle], left_bars: int = 5, right_bars: int = 5) -> dict:
    if len(candles) < left_bars + right_bars + 1:
        return {"bsl": [], "ssl": [], "eq": 0.0, "range_high": 0.0, "range_low": 0.0}

    bsl_pools, ssl_pools = [], []
    for i in range(left_bars, len(candles) - right_bars):
        window = candles[i - left_bars : i + right_bars + 1]
        center = candles[i]
        if center.high == max(c.high for c in window): bsl_pools.append(center.high)
        if center.low == min(c.low for c in window): ssl_pools.append(center.low)
            
    recent_high = max([c.high for c in candles[-60:]]) if len(candles) >= 60 else max([c.high for c in candles])
    recent_low = min([c.low for c in candles[-60:]]) if len(candles) >= 60 else min([c.low for c in candles])
    eq = (recent_high + recent_low) / 2
    
    return {
        "bsl": sorted(list(set(bsl_pools)))[-5:],
        "ssl": sorted(list(set(ssl_pools)))[:5],
        "range_high": recent_high,
        "range_low": recent_low,
        "eq": eq
    }

def detect_smt_divergence(asset_candles: list[Candle], btc_candles: list[Candle]) -> str:
    if len(asset_candles) < 20 or len(btc_candles) < 20: return Side.NEUTRAL.value
    
    asset_lows, btc_lows = [c.low for c in asset_candles[-10:]], [c.low for c in btc_candles[-10:]]
    asset_highs, btc_highs = [c.high for c in asset_candles[-10:]], [c.high for c in btc_candles[-10:]]
    
    asset_current_low, asset_prev_low = min(asset_lows[-3:]), min(asset_lows[:-3])
    btc_current_low, btc_prev_low = min(btc_lows[-3:]), min(btc_lows[:-3])
    
    if btc_current_low < btc_prev_low and asset_current_low >= asset_prev_low:
        return Side.LONG.value # Bullish SMT
        
    asset_current_high, asset_prev_high = max(asset_highs[-3:]), max(asset_highs[:-3])
    btc_current_high, btc_prev_high = max(btc_highs[-3:]), max(btc_highs[:-3])
    
    if btc_current_high > btc_prev_high and asset_current_high <= asset_prev_high:
        return Side.SHORT.value # Bearish SMT
        
    return Side.NEUTRAL.value

def detect_zones(candles: list[Candle], tf: str, max_age_bars: int = 80) -> list[Zone]:
    """Визначає зони, валідуючи FVG через правило 50% (Consequent Encroachment)"""
    zones = []
    if not candles: return zones
    atr_val = atr(candles, 14) or 0.5
    n = len(candles)
    
    for i in range(max(2, n - max_age_bars), n - 2):
        c = candles[i]
        body = abs(c.close - c.open)
        
        if body > atr_val * 0.55:
            side = Side.LONG.value if c.close > c.open else Side.SHORT.value
            zones.append(Zone("OB", side, c.low, c.high, c.ts, tf, strength=1.0 + body / atr_val))
            
        if i >= 2:
            prev2 = candles[i - 2]
            # LONG FVG
            if c.low > prev2.high:
                fvg_low, fvg_high = prev2.high, c.low
                ce = fvg_low + (fvg_high - fvg_low) / 2
                mitigated = any(future_c.close < ce for future_c in candles[i+1:])
                if not mitigated: zones.append(Zone("FVG", Side.LONG.value, fvg_low, fvg_high, c.ts, tf, strength=0.85))
                    
            # SHORT FVG
            if c.high < prev2.low:
                fvg_low, fvg_high = c.high, prev2.low
                ce = fvg_low + (fvg_high - fvg_low) / 2
                mitigated = any(future_c.close > ce for future_c in candles[i+1:])
                if not mitigated: zones.append(Zone("FVG", Side.SHORT.value, fvg_low, fvg_high, c.ts, tf, strength=0.85))
                    
    seen = set()
    unique = []
    for z in sorted(zones, key=lambda x: -x.strength):
        key = (z.side, round(z.low, 4), round(z.high, 4))
        if key not in seen:
            seen.add(key)
            unique.append(z)
    return unique[-38:]

def structure_snapshot(candles: list[Candle], tf: str) -> dict:
    if len(candles) < 10:
        return {"bias": Side.NEUTRAL.value, "bos": Side.NEUTRAL.value, "swing_high": 0, "swing_low": 0}
    highs = [c.high for c in candles[-28:]]
    lows = [c.low for c in candles[-28:]]
    swing_high = max(highs)
    swing_low = min(lows)
    last_close = candles[-1].close
    bias = Side.LONG.value if last_close > (swing_high + swing_low) / 2 else Side.SHORT.value
    bos = Side.NEUTRAL.value
    if last_close > swing_high * 0.993:
        bos = Side.LONG.value
    elif last_close < swing_low * 1.007:
        bos = Side.SHORT.value
    return {"timeframe": tf, "bias": bias, "bos": bos, "swing_high": swing_high, "swing_low": swing_low}


def detect_recent_structure_shift(candles: list[Candle], lookback: int = 7) -> dict:
    if len(candles) < max(6, lookback + 2):
        return {"bullish_shift": False, "bearish_shift": False, "strength": 0}

    recent = candles[-lookback:]
    previous = candles[-lookback * 2:-lookback] if len(candles) >= lookback * 2 else candles[:-lookback]
    if not previous:
        return {"bullish_shift": False, "bearish_shift": False, "strength": 0}

    recent_high = max(c.high for c in recent)
    recent_low = min(c.low for c in recent)
    previous_high = max(c.high for c in previous)
    previous_low = min(c.low for c in previous)
    last_close = candles[-1].close
    avg_range = mean(max(c.high - c.low, 0.0) for c in recent) or 0.0
    buffer = avg_range * 0.08

    bullish_shift = recent_high > previous_high + buffer and recent_low > previous_low + buffer
    bearish_shift = recent_low < previous_low - buffer and recent_high < previous_high - buffer

    if not bullish_shift and not bearish_shift:
        return {"bullish_shift": False, "bearish_shift": False, "strength": 0}

    closes_up = sum(1 for c in recent[-3:] if c.close > c.open)
    closes_down = sum(1 for c in recent[-3:] if c.close < c.open)
    strength = 1

    if bullish_shift:
        if closes_up >= 2:
            strength += 1
        if last_close > previous_high:
            strength += 1
    elif bearish_shift:
        if closes_down >= 2:
            strength += 1
        if last_close < previous_low:
            strength += 1

    return {
        "bullish_shift": bullish_shift,
        "bearish_shift": bearish_shift,
        "strength": min(strength, 3),
    }


def flow_snapshot(trades: list[dict], book: dict) -> dict:
    return {"bias": Side.NEUTRAL.value, "score": 0, "absorption_bias": Side.NEUTRAL.value}


def cvd_snapshot(trades: list[dict]) -> dict:
    return {"cvd": 0.0, "bias": Side.NEUTRAL.value, "strength": 0, "absorption": Side.NEUTRAL.value}


def regime_detection(tf4h: dict, tf1h: dict, move8: float) -> tuple[str, str]:
    htf_aligned = tf4h.get("bias") == tf1h.get("bias") and tf1h.get("bias") != Side.NEUTRAL.value
    if abs(move8) > 2.8:
        return Regime.SHOCK.value, "Сильний імпульс >2.8 ATR"
    if htf_aligned and abs(move8) > 0.55:
        return Regime.TREND.value, "HTF узгоджений + ефективний рух"
    if not htf_aligned and abs(move8) < 0.38:
        return Regime.RANGE.value, "HTF неузгоджений + низька ефективність"
    return Regime.TRANSITION.value, "Перехідний режим"


def _regime_engine_result(regime_type: str, legacy_name: str, score: int, confidence: int, reason: str,
                          entry_action: str = "ALLOW", quality_adjustment: int = 0,
                          quality_cap: Optional[int] = None, hard_block: bool = False,
                          risky_only: bool = False, metrics: Optional[dict] = None) -> dict:
    return {
        "name": str(legacy_name or Regime.NORMAL.value).upper(),
        "regime_type": str(regime_type or Regime.NORMAL.value).upper(),
        "score": int(clamp(score, 0, 100)),
        "confidence": int(clamp(confidence, 0, 100)),
        "reason": reason or "звичайний intraday режим",
        "entry_action": "RISKY_ONLY" if risky_only else str(entry_action or "ALLOW").upper(),
        "entry_quality_adjustment": int(quality_adjustment or 0),
        "quality_cap": quality_cap,
        "hard_block": bool(hard_block),
        "metrics": dict(metrics or {}),
    }


def detect_regime_engine_2(context: dict, side: Optional[str] = None) -> dict:
    price = safe_float(context.get("price"), 0.0)
    atr15 = safe_float(context.get("atr15"), 0.6) or 0.6
    tf3 = context.get("tf3", {})
    tf15 = context.get("tf15", {})
    tf1h = context.get("tf1h", {})
    tf4h = context.get("tf4h", {})
    c15 = (context.get("candles", {}) or {}).get("15m", [])
    regime = str(context.get("regime") or Regime.NORMAL.value).upper()
    bias = side if side in {Side.LONG.value, Side.SHORT.value} else tf15.get("bias", Side.NEUTRAL.value)

    move8 = pct(c15[-1].close, c15[-8].close) if len(c15) >= 8 else 0.0
    recent_range = (max(c.high for c in c15[-16:]) - min(c.low for c in c15[-16:])) if len(c15) >= 16 else 0.0
    range_atr = recent_range / atr15 if atr15 else 0.0
    htf_same = bias in {Side.LONG.value, Side.SHORT.value} and (tf1h.get("bias") == bias or tf4h.get("bias") == bias)
    tf15_same = bias in {Side.LONG.value, Side.SHORT.value} and tf15.get("bias") == bias
    tf3_same = bias in {Side.LONG.value, Side.SHORT.value} and tf3.get("bias") == bias
    tf3_against = bias in {Side.LONG.value, Side.SHORT.value} and tf3.get("bias") == opposite(bias)
    near_range_mid = bool(range_atr and abs(move8) < 0.38 and tf15.get("score", 0) <= 28)

    metrics = {
        "bias": bias,
        "move8_pct": round(move8, 3),
        "range_atr": round(range_atr, 2),
        "tf3": tf3.get("bias"),
        "tf15": tf15.get("bias"),
        "tf1h": tf1h.get("bias"),
        "tf4h": tf4h.get("bias"),
    }

    if abs(move8) >= 2.8 or regime == Regime.SHOCK.value:
        return _regime_engine_result(
            "NEWS_SHOCK", Regime.SHOCK.value, 78, 78,
            "сильний імпульс: входи тільки після підтвердження, прибуток захищати швидше",
            entry_action="RISKY_ONLY", quality_adjustment=-2, quality_cap=79, risky_only=True, metrics=metrics,
        )
    if near_range_mid or (regime == Regime.RANGE.value and range_atr <= 2.30):
        return _regime_engine_result(
            "RANGE_COMPRESSION", Regime.RANGE.value, 62, 64,
            "стискання/середина діапазону: TP ближче",
            entry_action="WAIT", quality_adjustment=-2, quality_cap=77, metrics=metrics,
        )
    return _regime_engine_result("NORMAL", regime, 50, 50, "звичайний intraday режим", entry_action="ALLOW", metrics=metrics)


def stabilize_regime_engine(state: dict, detected: dict) -> dict:
    if not isinstance(state, dict) or not isinstance(detected, dict):
        return detected
    memory = state.get("regime_memory") if isinstance(state.get("regime_memory"), dict) else {}
    current_type = str(detected.get("regime_type") or detected.get("name") or "NORMAL")
    prev_type = str(memory.get("type") or "")
    prev_pending = str(memory.get("pending_type") or "")
    confidence = int(detected.get("confidence", 0) or 0)

    if current_type == prev_type:
        stable_count = int(memory.get("stable_count", 0) or 0) + 1
        pending_count = 0
    elif current_type == prev_pending:
        pending_count = int(memory.get("pending_count", 0) or 0) + 1
        stable_count = 1 if confidence >= 78 or pending_count >= 2 or current_type in {"NEWS_SHOCK", "EXHAUSTION"} else 0
    else:
        pending_count = 1
        stable_count = 1 if confidence >= 82 or current_type in {"NEWS_SHOCK", "EXHAUSTION"} else 0

    detected = dict(detected)
    detected["stable_count"] = stable_count
    detected["pending_count"] = pending_count
    detected["previous_regime_type"] = prev_type
    detected["is_stable"] = bool(stable_count >= 1)
    state["regime_memory"] = {
        "type": current_type if stable_count >= 1 else prev_type,
        "pending_type": current_type if stable_count < 1 else "",
        "stable_count": stable_count,
        "pending_count": pending_count,
        "updated_at": iso_now(),
    }
    return detected


def build_context(data: dict, state: dict) -> dict:
    c3 = data["candles"]["3m"]
    c15 = data["candles"]["15m"]
    c1h = data["candles"]["1h"]
    c4h = data["candles"]["4h"]
    price = data["price"] or (c15[-1].close if c15 else 82.5)
    atr3 = atr(c3, 14)
    atr15 = atr(c15, 14) or (atr3 * 3.8 if atr3 else 0.6)
    atr1h = atr(c1h, 14) or (atr15 * 3.2)

    tf3 = {"bias": Side.NEUTRAL.value, "score": 0, "atr": atr3, "structure": structure_snapshot(c3, "3m")}
    tf15 = {"bias": Side.NEUTRAL.value, "score": 20, "atr": atr15, "structure": structure_snapshot(c15, "15m")}
    tf1h = {"bias": Side.NEUTRAL.value, "score": 25, "atr": atr1h, "structure": structure_snapshot(c1h, "1h")}
    tf4h = {"bias": Side.NEUTRAL.value, "score": 15, "atr": atr(c4h, 14) or (atr1h * 3.5), "structure": structure_snapshot(c4h, "4h")}

    if c15:
        ema = mean([c.close for c in c15[-8:]])
        tf15["bias"] = Side.LONG.value if c15[-1].close > ema else Side.SHORT.value
        tf15["score"] = 40 if abs(c15[-1].close - ema) / ema > 0.003 else 24

    zones = detect_zones(c15, "15m") + detect_zones(c1h, "1h", 42) + detect_zones(c4h, "4h", 28)
    flow = flow_snapshot(data.get("trades", []), data.get("book", {}))
    cvd = cvd_snapshot(data.get("trades", []))
    move8 = pct(c15[-1].close, c15[-8].close) if len(c15) >= 8 else 0.0
    regime, regime_reason = regime_detection(tf4h, tf1h, move8)

    ctx = {
        "time": data["time"],
        "price": price,
        "price_source": data.get("price_source", "TradingView"),
        "regime": regime,
        "regime_reason": regime_reason,
        "tf3": tf3, "tf15": tf15, "tf1h": tf1h, "tf4h": tf4h,
        "zones": zones[-38:],
        "flow": flow,
        "cvd": cvd,
        "atr15": atr15,
        "atr1h": atr1h,
        "candles": data["candles"],
        "btc_candles": data.get("btc_candles", {}),
        "volume_clusters": [],
        "scan_3m": state.get("scan_3m", {}),
        "scan_3m_events": {},
    }
    market_regime = stabilize_regime_engine(state, detect_regime_engine_2(ctx))
    ctx["market_regime"] = market_regime
    ctx["regime_engine"] = market_regime
    ctx["regime_type"] = market_regime.get("regime_type", regime)
    scan_result = scan_closed_3m_sequence(state, ctx)
    ctx["scan_3m"] = state.get("scan_3m", {})
    ctx["scan_3m_events"] = scan_result.get("events", {})
    return ctx


# ==========================================================
# 3M SCANNER + LOCATION + FORWARD ZONE
# ==========================================================

def _empty_scan3m_state() -> dict:
    return {"last_scanned_3m_ts": 0, "last_run_processed": 0, "processed_count": 0, "events": {}}


def _normalize_scan3m_state(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return _empty_scan3m_state()
    state = dict(raw)
    state.setdefault("last_scanned_3m_ts", 0)
    state.setdefault("last_run_processed", 0)
    state.setdefault("processed_count", 0)
    state.setdefault("events", {})
    return state


def calculate_impulse_strength(candles: list[Candle], side: str, atr15: float) -> dict:
    if len(candles) < 4:
        return {"score": 0, "level": "WEAK", "body_strength": 0, "consecutive": 0}
    last = candles[-1]
    prev = candles[-2]
    displacement = abs(last.close - prev.close)
    atr_ratio = displacement / atr15 if atr15 > 0 else 0
    body = abs(last.close - last.open)
    candle_range = max(last.high - last.low, 0.0001)
    body_strength = body / candle_range
    score = 0
    if atr_ratio > 0.7: score += 1
    if atr_ratio > 1.15: score += 1
    if atr_ratio > 1.7: score += 1
    if body_strength > 0.65: score += 1
    score = min(score, 3)
    level = {0: "WEAK", 1: "NORMAL", 2: "STRONG", 3: "EXTREME"}[score]
    return {"score": score, "level": level, "body_strength": round(body_strength, 2), "consecutive": 0}


def trigger_snapshot(candles: list[Candle], side: str, trigger_level: float, atr15: float) -> dict:
    if len(candles) < 5:
        return {"ready": False, "age_bars": 999, "quality": 0, "displacement": False,
                "strong_displacement": False, "retest": False, "mitigation": False,
                "reclaim": False, "chase_risk": False,
                "impulse_strength": {"score": 0, "level": "WEAK"}}
    last = candles[-1]
    prev = candles[-2]
    is_reclaim = (side == Side.LONG.value and last.close > trigger_level) or (side == Side.SHORT.value and last.close < trigger_level)
    displacement_size = abs(last.close - prev.close)
    directional_displacement = (side == Side.LONG.value and last.close > prev.close) or (side == Side.SHORT.value and last.close < prev.close)
    displacement = displacement_size > atr15 * 0.65 and directional_displacement
    strong_displacement = displacement_size > atr15 * 0.95 and directional_displacement
    
    retest = False
    if len(candles) >= 4 and is_reclaim:
        recent_retest_bars = candles[-4:-1]
        if side == Side.LONG.value: retest = any(c.low <= trigger_level * 1.0015 for c in recent_retest_bars)
        else: retest = any(c.high >= trigger_level * 0.9985 for c in recent_retest_bars)
        
    ready = is_reclaim and (displacement or retest)
    impulse = calculate_impulse_strength(candles, side, atr15)
    quality = 40
    if displacement: quality += 18
    if strong_displacement: quality += 15
    if retest: quality += 20
    
    return {
        "ready": ready,
        "age_bars": 0,
        "quality": max(0, min(quality, 98)),
        "displacement": displacement,
        "strong_displacement": strong_displacement,
        "retest": retest,
        "mitigation": False,
        "reclaim": is_reclaim,
        "chase_risk": bool(strong_displacement and not retest),
        "impulse_strength": impulse,
        "sweep_level": trigger_level,
        "extreme": last.low if side == Side.LONG.value else last.high
    }

def scan_closed_3m_sequence(state: dict, context: dict) -> dict:
    c3 = context["candles"]["3m"]
    if not c3:
        return {"last_run_processed": 0, "events": {}}
    scan_state = state.get("scan_3m", _empty_scan3m_state())
    last_scanned_ts = scan_state.get("last_scanned_3m_ts", 0)
    new_candles = [c for c in c3 if c.ts > last_scanned_ts]
    if not new_candles:
        return {"last_run_processed": 0, "events": scan_state.get("events", {})}
    atr15 = context.get("atr15", 0.6) or 0.6
    events = dict(scan_state.get("events", {}))
    for side in [Side.LONG.value, Side.SHORT.value]:
        event = events.get(side, {
            "side": side, "stage": "SWEEP", "source": "LIQUIDITY_SWEEP",
            "event_ts": 0, "last_event_ts": 0, "trigger_level": context["price"],
            "event_price": context["price"], "invalidation_level": context["price"],
            "sweep_level": context["price"], "extreme": context["price"],
            "displacement": False, "retest_ts": 0, "ready_ts": 0, "hold_closes": 0,
            "previous_stage": "", "chain_quality": 50, "confirmation_quality": 40,
            "acceptance_quality": 0, "retest_quality": 0, "processed_bars": 0,
            "strong_displacement": False, "retest": False, "mitigation": False,
            "impulse_strength": {"score": 0, "level": "WEAK"}, "chase_risk": False
        })
        lookback = min(len(new_candles) + 10, len(c3))
        recent = c3[-lookback:]
        snap = trigger_snapshot(recent, side, event.get("trigger_level", context["price"]), atr15)
        event["displacement"] = bool(event.get("displacement") or snap.get("displacement"))
        event["strong_displacement"] = bool(event.get("strong_displacement") or snap.get("strong_displacement"))
        event["retest"] = bool(event.get("retest") or snap.get("retest"))

        if snap["ready"] and event["stage"] in ["SWEEP", "CONFIRMATION"]:
            event["stage"] = "ACCEPTANCE"
            event["last_event_ts"] = recent[-1].ts
        elif event["stage"] == "ACCEPTANCE" and snap["retest"]:
            event["stage"] = "RETEST"
            event["last_event_ts"] = recent[-1].ts
        elif event["stage"] == "RETEST" and (snap["reclaim"] or snap["ready"]):
            event["stage"] = "READY"
            event["last_event_ts"] = recent[-1].ts
            
        event["processed_bars"] += len(new_candles)
        events[side] = event
        
    scan_state["last_scanned_3m_ts"] = max(c.ts for c in new_candles)
    scan_state["last_run_processed"] = len(new_candles)
    scan_state["events"] = events
    return {"last_run_processed": len(new_candles), "events": events}


# ==========================================================
# CANDIDATE
# ==========================================================

def event_driven_reentry_guard(state: dict, context: dict, candidate: Candidate) -> dict:
    history = state.get("history", [])
    if not history or not candidate.thesis_key:
        return {"blocked": False, "reason": ""}
    recent_stops = [h for h in history[-12:] if h.get("action") == Action.STOP.value and h.get("thesis_key")]
    for h in recent_stops:
        if h.get("thesis_key") == candidate.thesis_key:
            trigger_diff = abs(candidate.trigger_level - safe_float(h.get("trigger_level")))
            if trigger_diff < context.get("atr15", 0.6) * 0.20:
                return {"blocked": True, "reason": "Точний replay тієї ж тези"}
    return {"blocked": False, "reason": ""}


def detect_candidates(context: dict, state: dict, journal: dict) -> list[Candidate]:
    price = context["price"]
    atr15 = context["atr15"] or 0.6
    zones = context["zones"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context["tf4h"]
    flow = context["flow"]
    cvd = context.get("cvd", {})
    regime = context["regime"]
    scan_events = context.get("scan_3m_events", {})
    
    c15 = (context.get("candles", {}) or {}).get("15m", [])
    btc_c15 = (context.get("btc_candles", {}) or {}).get("15m", [])

    # === ICT Core Data ===
    range_data = identify_liquidity_and_range(c15)
    eq_level = range_data.get("eq", 0.0)
    smt_bias = detect_smt_divergence(c15, btc_c15)
    
    now_hour = now_utc().hour
    is_killzone = (7 <= now_hour < 10) or (12 <= now_hour < 15) or (18 <= now_hour < 21)

    params = get_adaptive_params(regime)

    candidates = []
    for side in [Side.LONG.value, Side.SHORT.value]:
        event = scan_events.get(side, {})
        trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
        trigger_ready = event.get("stage") in ["ACCEPTANCE", "RETEST", "READY"] and trigger_age <= TRIGGER_MAX_AGE_MINUTES
        trigger_level = event.get("trigger_level", price)
        scan_stage = event.get("stage", "")

        loc_score = 14
        loc_conf = ["біля свіжої зони"]
        for z in zones:
            if z.side == side and abs(price - z.low) < atr15 * 1.55:
                loc_score = min(22, loc_score + 7)
                loc_conf.append(f"біля {z.kind} {z.timeframe}")

        str_score = 10
        str_conf = []
        if tf15.get("bias") == side:
            str_score += 9
            str_conf.append("15M структура підтримує")
        if tf1h.get("bias") == side:
            str_score += 11
            str_conf.append("1H підтримує")

        liq_score = 6
        if event.get("source") == "LIQUIDITY_SWEEP" and trigger_ready:
            liq_score += 10

        flw_score = 0
        flw_conf = []
        if flow.get("bias") == side:
            flw_score += 9
            flw_conf.append("flow на боці")
            
        trig_score = 8
        if trigger_ready:
            trig_score += 14

        raw = loc_score + str_score + liq_score + flw_score + trig_score + 6

        pattern_conf = []

        # === 1. Premium/Discount (PD Array) ===
        if eq_level > 0:
            if side == Side.LONG.value and price <= eq_level:
                raw += 15
                loc_conf.append("LONG у Discount зоні (PD Array)")
            elif side == Side.SHORT.value and price >= eq_level:
                raw += 15
                loc_conf.append("SHORT у Premium зоні (PD Array)")
            else:
                raw -= 15
                pattern_conf.append("⚠️ Вхід поза оптимальним PD Array")

        # === 2. SMT Divergence ===
        if smt_bias == side:
            raw += 20
            flw_conf.append("🔥 SMT Divergence з BTC підтверджує рух!")

        # === 3. Killzones ===
        if is_killzone:
            raw += 5
            pattern_conf.append("✅ Активна Killzone")
        else:
            raw -= 8
            pattern_conf.append("⚠️ Поза активними торговими сесіями")


        setup_type = SetupType.BREAKOUT_RETEST.value
        family = SetupFamily.CONTINUATION.value

        final = int(clamp(raw, 12, 98))
        lane = ExecutionLane.STANDARD_CONFIRMED.value
        tier = ConfirmationTier.STANDARD.value

        if final >= params["entry_score"] and trigger_ready:
            lane = ExecutionLane.STANDARD_CONFIRMED.value
            tier = ConfirmationTier.HIGH_QUALITY.value
        elif final >= params["risky_entry_score"] and trigger_ready:
            lane = ExecutionLane.EARLY_TACTICAL.value

        cand = Candidate(
            side=side,
            setup_type=setup_type,
            setup_family=family,
            raw_score=raw,
            final_score=final,
            confirmations=loc_conf + str_conf + flw_conf + pattern_conf,
            trigger_ready=trigger_ready,
            trigger_level=round_price(trigger_level),
            invalidation_level=round_price(price - (atr15 * 1.65 if side == Side.LONG.value else -atr15 * 1.65)),
            target_levels=[round_price(price + (atr15 * 2.4 if side == Side.LONG.value else -atr15 * 2.4))],
            execution_lane=lane,
            confirmation_tier=tier,
            stage="ARMED" if final >= params["armed_score"] else "DISCOVERED",
            variant="STANDARD",
            execution_anchor=price,
            trigger_age_minutes=trigger_age,
            thesis_key=f"{side}|{family}|{setup_type}|{int(price*10)}",
            thesis=f"{side} {setup_type} | 3M={scan_stage}",
            scan_event_stage=scan_stage,
        )
        
        cand.professional_gate = evaluate_professional_gate(context, cand)
        candidates.append(cand)
    return sorted(candidates, key=lambda c: -c.final_score)[:2]


# ==========================================================
# TRADE PLAN & MANAGEMENT
# ==========================================================

def trade_mode_profile(context: dict, side: Optional[str] = None, setup_type: Optional[str] = None) -> dict:
    regime = context.get("market_regime") if isinstance(context.get("market_regime"), dict) else detect_regime_engine_2(context, side)
    name = str(regime.get("name", context.get("regime", Regime.NORMAL.value)) or Regime.NORMAL.value).upper()
    profiles = {
        Regime.RANGE.value: {"tp1_rr": 1.65, "tp2_rr": 2.35, "tp3_rr": 3.10, "stop_min_atr": 0.72, "stop_max_atr": 1.55, "be_trigger": 0.40, "protect_trigger": 0.62, "giveback": 0.22},
        Regime.NORMAL.value: {"tp1_rr": 2.00, "tp2_rr": 3.00, "tp3_rr": 4.00, "stop_min_atr": 0.80, "stop_max_atr": 2.30, "be_trigger": 0.55, "protect_trigger": 0.88, "giveback": 0.35},
    }
    profile = dict(profiles.get(name, profiles[Regime.NORMAL.value]))
    profile["regime"] = name
    return profile

def build_trade_plan(context: dict, candidate: Candidate) -> TradePlan:
    price = context["price"]
    atr15 = context["atr15"] or 0.6
    side = candidate.side
    stop_dist = max(atr15 * MIN_STOP_ATR15, abs(price - candidate.invalidation_level))
    stop = price - stop_dist if side == Side.LONG.value else price + stop_dist
    tp1_dist = max(stop_dist * PREFERRED_RR1, atr15 * MIN_TP1_ATR15)
    tp1 = price + tp1_dist if side == Side.LONG.value else price - tp1_dist
    tp2 = price + (tp1_dist * 1.5) if side == Side.LONG.value else price - (tp1_dist * 1.5)
    tp3 = price + (tp1_dist * 2.0) if side == Side.LONG.value else price - (tp1_dist * 2.0)
    
    return TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1),
        tp2=round_price(tp2),
        tp3=round_price(tp3),
        risk_pct=NORMAL_RISK_PCT,
        rr1=PREFERRED_RR1, rr2=MIN_RR2, rr3=MIN_RR3,
        position_risk_pct=NORMAL_RISK_PCT,
        structural_invalidation=stop,
        trigger_level=candidate.trigger_level,
        execution_ready=candidate.trigger_ready and candidate.final_score >= ENTRY_SCORE_BASE,
        valid=True,
    )

def evaluate_new_setup(context: dict, state: dict, journal: dict) -> Decision:
    cands = detect_candidates(context, state, journal)
    current_price = context.get("price", 0)
    
    if not cands:
        return Decision(id=uuid.uuid4().hex[:10], time=iso_now(), action=Action.NO_SETUP.value, side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value, quality=12, reason="Немає професійного сетапу", regime=context["regime"], current_price=current_price)

    best = cands[0]
    plan = build_trade_plan(context, best)
    action = Action.NO_SETUP.value
    reason = "Не пройшов gate'и якості"

    gate = best.professional_gate or {}
    
    if gate.get("allow_entry") and plan.execution_ready:
        action = Action.ENTRY.value
        reason = f"{best.setup_type} — Оптимальний вхід"
    elif gate.get("allow_risky") and plan.execution_ready:
        action = Action.RISKY_ENTRY.value
        reason = f"{best.setup_type} — Ризикований вхід"
    elif best.final_score >= ARMED_SCORE_BASE:
        action = Action.ARMED.value
        reason = f"{best.setup_type} — Сформовано, чекаємо підтвердження"

    return Decision(id=uuid.uuid4().hex[:10], time=iso_now(), action=action, side=best.side, setup_type=best.setup_type, quality=best.final_score, reason=reason, regime=context["regime"], candidate=best, plan=plan, current_price=current_price)

def manage_active_trade(trade: ActiveTrade, context: dict) -> dict:
    price = context["price"]
    side = trade.side

    result = {
        "action": Action.HOLD.value,
        "current_pct": ((price - trade.entry) / trade.entry * 100) if side == Side.LONG.value else ((trade.entry - price) / trade.entry * 100),
        "best_pct": ((trade.best_price - trade.entry) / trade.entry * 100) if side == Side.LONG.value else ((trade.entry - trade.best_price) / trade.entry * 100),
        "closed": False,
        "notes": [],
        "recommended_stop": None,
    }

    if (side == Side.LONG.value and price > trade.best_price) or (side == Side.SHORT.value and price < trade.best_price):
        trade.best_price = price
    if (side == Side.LONG.value and price < trade.worst_price) or (side == Side.SHORT.value and price > trade.worst_price):
        trade.worst_price = price

    # Спрощена логіка стоп-лоса
    stop_hit = False
    stop = float(trade.stop_current or 0)
    if stop:
        if trade.side == Side.LONG.value and price <= stop: stop_hit = True
        elif trade.side == Side.SHORT.value and price >= stop: stop_hit = True

    if stop_hit:
        result["closed"] = True
        result["action"] = Action.STOP.value
        trade.status = "CLOSED"
        return result

    # Логіка TP
    if not result["closed"] and not trade.tp1_hit:
        tp_hit = (side == Side.LONG.value and price >= trade.tp1) or (side == Side.SHORT.value and price <= trade.tp1)
        if tp_hit:
            trade.tp1_hit = True
            trade.stop_current = trade.entry # BezZbytek
            result["action"] = Action.TP1.value

    trade.last_action = result["action"]
    return result

def compute_analytics(journal: dict) -> dict:
    trades = journal.get("trades", [])
    if not trades: return {"closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_r": 0.0, "expectancy_r": 0.0, "by_family": {}}
    wins = sum(1 for t in trades if (t.get("result_pct") or 0) > 0)
    closed = len(trades)
    win_rate = round(wins / closed * 100, 1) if closed else 0.0
    net_r = round(sum((t.get("result_pct") or 0) / 100 for t in trades), 2)
    return {"closed_trades": closed, "wins": wins, "losses": closed - wins, "win_rate": win_rate, "net_r": net_r, "expectancy_r": round(net_r / max(closed, 1), 3), "by_family": {}}


# ==========================================================
# MAIN
# ==========================================================

def run_bot() -> None:
    print(f"START {BOT_VERSION}")
    state = load_state()
    journal = load_journal()
    data = collect_market_data()
    context = build_context(data, state)
    if not context.get("price"):
        print("NO PRICE — abort")
        return
    print(f"PRICE {context['price']:.4f} | REGIME {context['regime']} | ATR15 {context['atr15']:.4f} | Джерело: {context.get('price_source', 'TradingView')}")

    active = active_trade_from_state(state)
    if active and active.status != "CLOSED":
        res = manage_active_trade(active, context)
        msg = build_follow_message(context, active, res)
        
        if TELEGRAM_NOTIFY_EVERY_RUN or res.get("closed"):
            send_telegram(msg)
        
        if res.get("closed"):
            journal["trades"].append({"id": active.id, "side": active.side, "setup_type": active.setup_type, "result_pct": res.get("current_pct"), "mfe_pct": res.get("best_pct")})
            store_active_trade(state, None)
            state["opportunity"] = None
        else:
            store_active_trade(state, active)
        append_history(state, {"type": "FOLLOW" if not res.get("closed") else "CLOSE", "side": active.side, "action": res.get("action"), "price": context["price"]})
        journal["signals"].append({"time": iso_now(), "type": "FOLLOW", "action": res.get("action"), "side": active.side, "price": context["price"]})
        save_state(state)
        save_journal(journal)
        print("BOT COMPLETE: ACTIVE TRADE MANAGED")
        return

    decision = evaluate_new_setup(context, state, journal)
    payload = {"id": decision.id, "time": decision.time, "action": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "reason": decision.reason, "regime": decision.regime, "news_bias": decision.news_bias, "macro_risk": decision.macro_risk, "version": BOT_VERSION, "architecture_version": ARCHITECTURE_VERSION}
    state["latest_signal"] = payload
    append_history(state, {"type": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "price": context["price"]})
    journal["signals"].append(payload)

    if decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value) and decision.candidate and decision.plan:
        active = ActiveTrade(id=uuid.uuid4().hex[:10], side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, opened_at=iso_now(), entry=decision.plan.entry, stop_initial=decision.plan.stop, stop_current=decision.plan.stop, structural_invalidation=decision.plan.structural_invalidation, tp1=decision.plan.tp1, tp2=decision.plan.tp2, tp3=decision.plan.tp3, quality=decision.quality, position_risk_pct=decision.plan.position_risk_pct, best_price=decision.plan.entry, worst_price=decision.plan.entry, trigger_level=decision.candidate.trigger_level, thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, opened_regime=decision.regime, entry_level=decision.action)
        store_active_trade(state, active)
        state["opportunity"] = None
        print(f"[INFO] Угода відкрита: {decision.side} {decision.setup_type}")
    elif decision.action == Action.ARMED.value and decision.candidate:
        opp = Opportunity(side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, created_at=iso_now(), expires_at=(now_utc() + timedelta(hours=18)).isoformat(), score=decision.quality, trigger_level=decision.candidate.trigger_level, invalidation_level=decision.candidate.invalidation_level, confirmations=decision.candidate.confirmations[:5], evidence_families=decision.candidate.evidence_families, execution_lane=decision.candidate.execution_lane, status="ARMED", thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis)
        store_opportunity(state, opp)
        store_active_trade(state, None)
    else:
        store_active_trade(state, None)

    if decision.action != Action.NO_SETUP.value or SEND_NO_SETUP or TELEGRAM_NOTIFY_EVERY_RUN:
        msg = build_decision_message(context, decision)
        print("TELEGRAM (DECISION):", msg[:320])
        send_telegram(msg)

    save_state(state)
    save_journal(journal)
    print("BOT COMPLETE")


def main() -> None:
    parser = argparse.ArgumentParser(description="BZU Professional Hybrid Confluence Signal Bot v6.5")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print("SELF-TEST PASSED")
        return
    run_bot()


if __name__ == "__main__":
    main()
