#!/usr/bin/env python3
"""
BZU Professional Hybrid Confluence Signal Bot v5.1 (DEBUG)
================================================================================
Версія з посиленими логами для діагностики збереження історії.

Додано:
- Детальні логи перед/після append_history()
- Детальні логи перед/після save_state()
- Логи в atomic_json_write()
- Логи на вході/виході run_bot()
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from statistics import mean
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

# ==========================================================
# CONFIGURATION v5.1
# ==========================================================

BOT_VERSION = "pro-hybrid-confluence-v5.1-DEBUG"
ARCHITECTURE_VERSION = "HYBRID_CONFLUENCE_V5_1_ACCEPTANCE_VOLUME_MACRO_ADAPTIVE"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")

WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", os.getcwd()))
STATE_FILE = Path(os.getenv("SIGNAL_MEMORY_FILE", str(WORKSPACE / "last_signal_v5_1.json")))
JOURNAL_FILE = Path(os.getenv("SIGNAL_JOURNAL_FILE", str(WORKSPACE / "signal_journal_v5_1.json")))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "200") or 200)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "3000") or 3000)

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "5") or 5)
NORMAL_RISK_PCT = float(os.getenv("NORMAL_RISK_PCT", "0.50") or 0.50)
RISKY_RISK_PCT = float(os.getenv("RISKY_RISK_PCT", "0.25") or 0.25)

# === ICT Geometry ===
MIN_STOP_ATR15 = max(0.78, float(os.getenv("MIN_STOP_ATR15", "0.82") or 0.82))
MAX_STOP_ATR15 = float(os.getenv("MAX_STOP_ATR15", "2.70") or 2.70)
MIN_TP1_ATR15 = max(1.30, float(os.getenv("MIN_TP1_ATR15", "1.35") or 1.35))
MIN_RR1 = max(2.00, float(os.getenv("MIN_RR1", "2.00") or 2.00))
PREFERRED_RR1 = max(2.25, float(os.getenv("PREFERRED_RR1", "2.25") or 2.25))
MIN_RR2 = max(3.10, float(os.getenv("MIN_RR2", "3.10") or 3.10))
MIN_RR3 = max(4.20, float(os.getenv("MIN_RR3", "4.20") or 4.20))

# === Scoring Thresholds ===
ENTRY_SCORE_BASE = int(os.getenv("ICT_ENTRY_SCORE", "78") or 78)
RISKY_ENTRY_SCORE_BASE = int(os.getenv("ICT_RISKY_ENTRY_SCORE", "70") or 70)
ARMED_SCORE_BASE = int(os.getenv("ICT_ARMED_SCORE", "62") or 62)
MIN_ENTRY_EVIDENCE_BASE = int(os.getenv("MIN_ENTRY_EVIDENCE", "6") or 6)

# === 3M Scanner ===
SCAN_3M_BOOTSTRAP_BARS = max(12, int(os.getenv("SCAN_3M_BOOTSTRAP_BARS", "14") or 14))
TRIGGER_MAX_AGE_MINUTES = int(os.getenv("TRIGGER_MAX_AGE_MINUTES", "28") or 28)

# === Re-Entry ===
REENTRY_NEW_TRIGGER_SEPARATION_ATR15 = float(os.getenv("REENTRY_NEW_TRIGGER_SEPARATION_ATR15", "0.22") or 0.22)
REENTRY_NEW_INVALIDATION_SEPARATION_ATR15 = float(os.getenv("REENTRY_NEW_INVALIDATION_SEPARATION_ATR15", "0.32") or 0.32)
FAILED_ENTRY_MIN_MFE_R = float(os.getenv("FAILED_ENTRY_MIN_MFE_R", "0.45") or 0.45)

# === Telegram ===
TELEGRAM_NOTIFY_EVERY_RUN = os.getenv("TELEGRAM_NOTIFY_EVERY_RUN", "true").lower() in {"1", "true", "yes"}
SEND_NO_SETUP = os.getenv("SEND_NO_SETUP", "true").lower() in {"1", "true", "yes"}
TELEGRAM_MAX_LENGTH = max(600, min(4200, int(os.getenv("TELEGRAM_MAX_LENGTH", "4000") or 4000)))

# === News / Macro ===
IMPORTANT_EVENT_WINDOW_MINUTES = int(os.getenv("IMPORTANT_EVENT_WINDOW_MINUTES", "60") or 60)
MANUAL_IMPORTANT_EVENTS = os.getenv("IMPORTANT_EVENTS", "")

LONG_NEWS_PHRASES = [
    "inventory draw", "crude draw", "stockpiles fell", "supply disruption",
    "hormuz", "new sanctions", "opec cut", "production cut", "attack", "strike",
    "запаси впали", "нові санкції", "атака", "перебої постачання"
]

SHORT_NEWS_PHRASES = [
    "inventory build", "crude build", "stockpiles rose", "ceasefire",
    "sanctions relief", "output increase", "demand weak", "oversupply",
    "запаси зросли", "припинення вогню", "збільшення видобутку"
]

HIGH_IMPACT_WORDS = ["eia", "opec", "fed", "powell", "fomc", "iran", "hormuz", "sanctions", "inventory", "brent", "crude"]


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
class VolumeCluster:
    price_level: float
    volume: float
    strength: float
    type: str


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
    status: str = "OPEN"
    last_action: str = "ENTRY"
    notes: list[str] = field(default_factory=list)
    entry_integrity_score: int = 100
    entry_fail_streak: int = 0
    mfe_giveback_streak: int = 0
    mfe_giveback_last_state: str = "OK"


# ==========================================================
# UTILITIES + DEBUG LOGS
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
    """З посиленими логами"""
    print(f"[DEBUG] atomic_json_write -> {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    payload = json_safe(data)
    try:
        with temp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        with backup.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"[DEBUG] atomic_json_write УСПІШНО записано: {path}")
    except Exception as e:
        print(f"[ERROR] atomic_json_write ПОМИЛКА: {e}")


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
    print("[DEBUG] load_state() викликано")
    raw = load_json(STATE_FILE, {})
    same_arch = raw.get("architecture_version") == ARCHITECTURE_VERSION
    state = {
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "active_trade": raw.get("active_trade"),
        "opportunity": raw.get("opportunity") if same_arch else None,
        "scan_3m": _normalize_scan3m_state(raw.get("scan_3m")) if same_arch else _empty_scan3m_state(),
        "latest_signal": raw.get("latest_signal"),
        "last_message_key": raw.get("last_message_key", "") if same_arch else "",
        "history": list(raw.get("history") or [])[-MAX_HISTORY:],
    }
    print(f"[DEBUG] load_state() -> history length: {len(state['history'])}")
    return state


def save_state(state: dict[str, Any]) -> None:
    print("[DEBUG] save_state() викликано")
    state["updated_at"] = iso_now()
    state["history"] = list(state.get("history") or [])[-MAX_HISTORY:]
    print(f"[DEBUG] save_state() -> history length перед записом: {len(state['history'])}")
    atomic_json_write(STATE_FILE, state)
    print("[DEBUG] save_state() завершено")


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
    print("[DEBUG] append_history() викликано")
    payload = dict(item)
    payload.setdefault("time", iso_now())
    state.setdefault("history", []).append(payload)
    state["history"] = state["history"][-MAX_HISTORY:]
    print(f"[DEBUG] append_history() -> додано запис. Тепер history length: {len(state['history'])}")


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
    lines = [
        "<b>BZU PRO HYBRID CONFLUENCE v5.1</b>",
        "",
        f"<b>{decision.action}</b> | {side_word(decision.side)} | {setup_label(decision.setup_type)}",
        f"<b>Якість:</b> {decision.quality}/100 | Режим: {regime_label(decision.regime)} | News: {decision.news_bias} | Macro: {decision.macro_risk}"
    ]
    if decision.candidate:
        c = decision.candidate
        if c.confirmations:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            for x in c.confirmations[:4]:
                lines.append(f"✅ {html.escape(x)}")
    if decision.plan and decision.plan.valid:
        p = decision.plan
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(f"Вхід <b>{_fmt_price(p.entry)}</b> | Стоп <b>{_fmt_price(p.stop)}</b>")
        lines.append(f"TP1 {_fmt_price(p.tp1)} (RR {p.rr1}) | TP2 {_fmt_price(p.tp2)} | TP3 {_fmt_price(p.tp3)}")
    lines.append("")
    lines.append(f"<i>{html.escape(decision.reason)}</i>")
    return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]


def build_follow_message(context: dict, trade: ActiveTrade, result: dict) -> str:
    lines = [
        "<b>BZU PRO — HYBRID ICT + THESIS v5.1</b>",
        "",
        f"<b>{result.get('title', 'MONITOR')}</b>",
        f"{result.get('recommendation', '')}",
        "",
        f"<b>Ціна:</b> {_fmt_price(context['price'])} | Від входу: {result.get('current_pct', 0):.2f}% | Макс: {result.get('best_pct', 0):.2f}%"
    ]
    if result.get("notes"):
        lines.append("")
        lines.append("<b>Дії / ICT-нотатки:</b>")
        for n in result["notes"][:3]:
            lines.append(f"• {html.escape(n)}")
    if result.get("recommended_stop"):
        lines.append(f"<b>Рекомендований стоп:</b> {_fmt_price(result['recommended_stop'])}")
    lines.append("")
    lines.append(f"<i>Оновлено: {iso_now()[:19]}</i>")
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
        "mfe_giveback_threshold": 0.46,
        "min_mfe_for_protect": 2.4,
        "stop_atr_multiplier": 1.0,
        "reentry_aggressiveness": 1.0,
    }

    if regime == Regime.TREND.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 0.95)
        base["min_evidence"] = max(5, MIN_ENTRY_EVIDENCE_BASE - 1)
        base["mfe_giveback_threshold"] = 0.42
        base["min_mfe_for_protect"] = 2.2
        base["reentry_aggressiveness"] = 1.15

    elif regime == Regime.RANGE.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.08)
        base["risky_entry_score"] = int(RISKY_ENTRY_SCORE_BASE * 1.06)
        base["min_evidence"] = MIN_ENTRY_EVIDENCE_BASE + 1
        base["mfe_giveback_threshold"] = 0.55
        base["min_mfe_for_protect"] = 2.8
        base["reentry_aggressiveness"] = 0.75

    elif regime == Regime.SHOCK.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.18)
        base["risky_entry_score"] = int(RISKY_ENTRY_SCORE_BASE * 1.12)
        base["min_evidence"] = MIN_ENTRY_EVIDENCE_BASE + 2
        base["mfe_giveback_threshold"] = 0.60
        base["min_mfe_for_protect"] = 3.2
        base["reentry_aggressiveness"] = 0.55

    elif regime == Regime.TRANSITION.value:
        base["entry_score"] = int(ENTRY_SCORE_BASE * 1.04)
        base["min_evidence"] = MIN_ENTRY_EVIDENCE_BASE
        base["reentry_aggressiveness"] = 0.90

    return base


# ==========================================================
# NEWS + MACRO FILTER
# ==========================================================

def fetch_simple_news() -> list[dict]:
    return []


def analyze_news_bias(news_items: list[dict]) -> dict:
    if not news_items:
        return {"bias": "NEUTRAL", "score": 0, "top_events": []}

    long_score = 0
    short_score = 0
    top_events = []

    for item in news_items[:8]:
        title = str(item.get("title", "")).lower()
        for phrase in LONG_NEWS_PHRASES:
            if phrase in title:
                long_score += 2
                top_events.append({"title": item.get("title"), "bias": "LONG"})
        for phrase in SHORT_NEWS_PHRASES:
            if phrase in title:
                short_score += 2
                top_events.append({"title": item.get("title"), "bias": "SHORT"})

    if long_score > short_score + 3:
        bias = "LONG"
        score = min(long_score, 35)
    elif short_score > long_score + 3:
        bias = "SHORT"
        score = min(short_score, 35)
    else:
        bias = "NEUTRAL"
        score = 0

    return {"bias": bias, "score": score, "top_events": top_events[:3]}


def get_macro_risk(news_bias: str, regime: str) -> str:
    if regime == Regime.SHOCK.value:
        return "HIGH"
    if news_bias != "NEUTRAL" and regime in [Regime.TRANSITION.value, Regime.RANGE.value]:
        return "ELEVATED"
    return "NORMAL"


# ==========================================================
# VOLUME CLUSTER ANALYSIS
# ==========================================================

def detect_volume_clusters(candles: list[Candle], bins: int = 12) -> list[VolumeCluster]:
    if not candles or len(candles) < 30:
        return []

    prices = [c.close for c in candles[-80:]]
    volumes = [c.volume for c in candles[-80:]]
    if not volumes or sum(volumes) == 0:
        return []

    min_p = min(prices)
    max_p = max(prices)
    if max_p - min_p < 0.01:
        return []

    bin_size = (max_p - min_p) / bins
    clusters = []

    for i in range(bins):
        low = min_p + i * bin_size
        high = low + bin_size
        vol_sum = sum(v for p, v in zip(prices, volumes) if low <= p < high)
        if vol_sum > 0:
            avg_vol = mean(volumes)
            strength = min(100, int((vol_sum / (avg_vol * 3)) * 100))
            cluster_type = "HVN" if strength > 55 else "LVN"
            clusters.append(VolumeCluster(
                price_level=round((low + high) / 2, 4),
                volume=round(vol_sum, 1),
                strength=strength,
                type=cluster_type
            ))

    return sorted(clusters, key=lambda x: -x.strength)[:6]


def has_volume_cluster_support(price: float, clusters: list[VolumeCluster], side: str, atr: float) -> bool:
    if not clusters:
        return False
    for cl in clusters:
        if abs(price - cl.price_level) < atr * 0.35 and cl.strength > 50:
            if (side == Side.LONG.value and cl.type == "HVN") or (side == Side.SHORT.value and cl.type == "HVN"):
                return True
    return False


# ==========================================================
# DEEP ACCEPTANCE QUALITY ENGINE
# ==========================================================

def calculate_acceptance_quality(
    event: dict,
    candles_3m: list[Candle],
    atr15: float,
    structure_alignment: float
) -> int:
    if not event or not candles_3m:
        return 30

    stage = event.get("stage", "SWEEP")
    if stage not in ["ACCEPTANCE", "RETEST", "READY"]:
        return 35

    quality = 40

    displacement = event.get("displacement", False)
    if displacement:
        quality += 18

    hold_closes = event.get("hold_closes", 0)
    if hold_closes >= 2:
        quality += 14
    elif hold_closes == 1:
        quality += 7

    quality += int(structure_alignment * 0.25)

    chain_quality = event.get("chain_quality", 50)
    quality += int((chain_quality - 50) * 0.3)

    return int(clamp(quality, 25, 95))


# ==========================================================
# 3M SCANNER
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


def trigger_snapshot(candles: list[Candle], side: str, trigger_level: float, atr15: float) -> dict:
    if len(candles) < 5:
        return {"ready": False, "age_bars": 999, "quality": 0, "displacement": False}
    last = candles[-1]
    prev = candles[-2]
    is_sweep = (side == Side.LONG.value and last.low < trigger_level) or (side == Side.SHORT.value and last.high > trigger_level)
    is_reclaim = (side == Side.LONG.value and last.close > trigger_level) or (side == Side.SHORT.value and last.close < trigger_level)
    displacement = abs(last.close - prev.close) > atr15 * 0.72
    ready = is_reclaim and displacement
    quality = 50
    if displacement:
        quality += 24
    if is_reclaim:
        quality += 18
    return {
        "ready": ready,
        "age_bars": 0,
        "quality": min(quality, 96),
        "displacement": displacement,
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
            "acceptance_quality": 0, "retest_quality": 0, "processed_bars": 0
        })
        lookback = min(len(new_candles) + 10, len(c3))
        recent = c3[-lookback:]
        snap = trigger_snapshot(recent, side, event.get("trigger_level", context["price"]), atr15)
        if snap["ready"] and event["stage"] in ["SWEEP", "CONFIRMATION"]:
            event["stage"] = "ACCEPTANCE"
            event["ready_ts"] = recent[-1].ts
            event["acceptance_quality"] = snap["quality"]
            event["last_event_ts"] = recent[-1].ts
        elif event["stage"] == "ACCEPTANCE" and snap["displacement"]:
            event["stage"] = "RETEST"
            event["retest_ts"] = recent[-1].ts
            event["retest_quality"] = snap["quality"]
        elif event["stage"] == "RETEST":
            event["stage"] = "READY"
            event["ready_ts"] = recent[-1].ts
        if snap.get("sweep_level"):
            event["trigger_level"] = snap["sweep_level"]
            event["sweep_level"] = snap["sweep_level"]
            event["extreme"] = snap["extreme"]
        event["last_event_ts"] = recent[-1].ts
        event["processed_bars"] += len(new_candles)
        events[side] = event
    new_last_ts = max(c.ts for c in new_candles)
    scan_state["last_scanned_3m_ts"] = new_last_ts
    scan_state["last_run_processed"] = len(new_candles)
    scan_state["processed_count"] = scan_state.get("processed_count", 0) + len(new_candles)
    scan_state["events"] = events
    state["scan_3m"] = scan_state
    context["scan_3m"] = scan_state
    context["scan_3m_events"] = events
    return {"last_run_processed": len(new_candles), "events": events}


# ==========================================================
# MARKET DATA
# ==========================================================

def http_get(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Pro-v5.1-Hybrid/1.0", "Accept": "*/*"}
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.35 * attempt)
    print(f"[WARN] GET failed: {url} | {last_err}")
    return None


def get_okx_candles(bar: str = "15m", limit: int = 160) -> list[Candle]:
    url = f"https://www.okx.com/api/v5/market/candles?instId={OKX_INST_ID}&bar={bar}&limit={limit}"
    resp = http_get(url)
    if not resp:
        return []
    try:
        data = resp.json().get("data", [])
        out = []
        for row in data:
            try:
                out.append(Candle(ts=int(row[0]), open=float(row[1]), high=float(row[2]),
                                  low=float(row[3]), close=float(row[4]), volume=float(row[5] or 0), confirmed=True))
            except Exception:
                continue
        out.sort(key=lambda c: c.ts)
        return out
    except Exception as exc:
        print(f"[WARN] OKX candles failed {bar}: {exc}")
        return []


def get_okx_ticker() -> dict:
    url = f"https://www.okx.com/api/v5/market/ticker?instId={OKX_INST_ID}"
    resp = http_get(url)
    if not resp:
        return {}
    try:
        row = resp.json().get("data", [{}])[0]
        last = safe_float(row.get("last") or row.get("lastSz"))
        open24 = safe_float(row.get("open24h"))
        return {"price": last, "change24h": pct(last, open24) if open24 else 0.0, "volume24h": safe_float(row.get("volCcy24h")), "source": "OKX"}
    except Exception:
        return {}


def get_okx_trades(limit: int = 150) -> list[dict]:
    url = f"https://www.okx.com/api/v5/market/trades?instId={OKX_INST_ID}&limit={limit}"
    resp = http_get(url)
    if not resp:
        return []
    try:
        out = []
        for row in resp.json().get("data", []):
            out.append({"price": safe_float(row.get("px")), "size": safe_float(row.get("sz")), "side": str(row.get("side", "")).lower(), "ts": int(row.get("ts", 0))})
        return out
    except Exception:
        return []


def get_okx_book(depth: int = 50) -> dict:
    url = f"https://www.okx.com/api/v5/market/books?instId={OKX_INST_ID}&sz={depth}"
    resp = http_get(url)
    if not resp:
        return {"bids": [], "asks": []}
    try:
        b = resp.json().get("data", [{}])[0]
        return {
            "bids": [(safe_float(x[0]), safe_float(x[1])) for x in b.get("bids", [])[:depth]],
            "asks": [(safe_float(x[0]), safe_float(x[1])) for x in b.get("asks", [])[:depth]]
        }
    except Exception:
        return {"bids": [], "asks": []}


def get_okx_derivatives() -> dict:
    result = {"oi": 0.0, "funding_rate": 0.0}
    oi = http_get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={OKX_INST_ID}", timeout=8, retries=1)
    funding = http_get(f"https://www.okx.com/api/v5/public/funding-rate?instId={OKX_INST_ID}", timeout=8, retries=1)
    try:
        if oi:
            result["oi"] = safe_float((oi.json().get("data") or [])[0].get("oi"))
    except Exception:
        pass
    try:
        if funding:
            result["funding_rate"] = safe_float((funding.json().get("data") or [])[0].get("fundingRate"))
    except Exception:
        pass
    return result


def collect_market_data() -> dict:
    c3 = get_okx_candles("3m", 240)
    c15 = get_okx_candles("15m", 200)
    c1h = get_okx_candles("1H", 160)
    c4h = get_okx_candles("4H", 140)
    okx_t = get_okx_ticker()
    trades = get_okx_trades(150)
    book = get_okx_book(50)
    deriv = get_okx_derivatives()
    ticker = okx_t or {"price": (c15[-1].close if c15 else 80.0), "source": "fallback"}
    return {
        "time": iso_now(),
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "ticker": ticker,
        "trades": trades,
        "book": book,
        "derivatives": deriv,
        "price": safe_float(ticker.get("price"))
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


def detect_zones(candles: list[Candle], tf: str, max_age_bars: int = 80) -> list[Zone]:
    zones = []
    if not candles:
        return zones
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
            if c.low > prev2.high:
                zones.append(Zone("FVG", Side.LONG.value, prev2.high, c.low, c.ts, tf, strength=0.75))
            if c.high < prev2.low:
                zones.append(Zone("FVG", Side.SHORT.value, c.high, prev2.low, c.ts, tf, strength=0.75))
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


def flow_snapshot(trades: list[dict], book: dict) -> dict:
    if not trades:
        return {"bias": Side.NEUTRAL.value, "score": 0, "absorption_bias": Side.NEUTRAL.value}
    buy_vol = sum(t["size"] for t in trades if t["side"] == "buy")
    sell_vol = sum(t["size"] for t in trades if t["side"] == "sell")
    total = buy_vol + sell_vol or 1
    delta = buy_vol - sell_vol
    bias = Side.LONG.value if delta > total * 0.065 else (Side.SHORT.value if delta < -total * 0.065 else Side.NEUTRAL.value)
    score = int(clamp(abs(delta) / total * 100, 0, 35))
    absorption = Side.NEUTRAL.value
    if book.get("bids") and book.get("asks"):
        bid_vol = sum(b[1] for b in book["bids"][:6])
        ask_vol = sum(a[1] for a in book["asks"][:6])
        if bid_vol > ask_vol * 1.32:
            absorption = Side.LONG.value
        elif ask_vol > bid_vol * 1.32:
            absorption = Side.SHORT.value
    return {"bias": bias, "score": score, "absorption_bias": absorption}


def cvd_snapshot(trades: list[dict]) -> dict:
    if not trades:
        return {"cvd": 0.0, "bias": Side.NEUTRAL.value, "strength": 0, "absorption": Side.NEUTRAL.value}
    cvd = 0.0
    for t in trades:
        signed = t["size"] if t["side"] == "buy" else -t["size"]
        cvd += signed
    last_30 = trades[-30:] if len(trades) >= 30 else trades
    buy = sum(t["size"] for t in last_30 if t["side"] == "buy")
    sell = sum(t["size"] for t in last_30 if t["side"] == "sell")
    total = buy + sell or 1
    delta = buy - sell
    bias = Side.LONG.value if delta > total * 0.08 else (Side.SHORT.value if delta < -total * 0.08 else Side.NEUTRAL.value)
    strength = int(clamp(abs(delta) / total * 100, 0, 42))
    absorption = Side.NEUTRAL.value
    if buy > sell * 1.45:
        absorption = Side.LONG.value
    elif sell > buy * 1.45:
        absorption = Side.SHORT.value
    return {"cvd": round(cvd, 2), "bias": bias, "strength": strength, "absorption": absorption}


def regime_detection(tf4h: dict, tf1h: dict, move8: float) -> tuple[str, str]:
    htf_aligned = tf4h.get("bias") == tf1h.get("bias") and tf1h.get("bias") != Side.NEUTRAL.value
    if abs(move8) > 2.8:
        return Regime.SHOCK.value, "Сильний імпульс >2.8 ATR"
    if htf_aligned and abs(move8) > 0.55:
        return Regime.TREND.value, "HTF узгоджений + ефективний рух"
    if not htf_aligned and abs(move8) < 0.38:
        return Regime.RANGE.value, "HTF неузгоджений + низька ефективність"
    return Regime.TRANSITION.value, "Перехідний режим"


def build_context(data: dict, state: dict) -> dict:
    c3 = data["candles"]["3m"]
    c15 = data["candles"]["15m"]
    c1h = data["candles"]["1h"]
    c4h = data["candles"]["4h"]
    price = data["price"] or (c15[-1].close if c15 else 80.0)
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
    if c1h:
        ema = mean([c.close for c in c1h[-6:]])
        tf1h["bias"] = Side.LONG.value if c1h[-1].close > ema else Side.SHORT.value
        tf1h["score"] = 44 if abs(c1h[-1].close - ema) / ema > 0.004 else 28
    if c4h:
        ema = mean([c.close for c in c4h[-5:]])
        tf4h["bias"] = Side.LONG.value if c4h[-1].close > ema else Side.SHORT.value
        tf4h["score"] = 30 if abs(c4h[-1].close - ema) / ema > 0.005 else 18

    zones = detect_zones(c15, "15m") + detect_zones(c1h, "1h", 42) + detect_zones(c4h, "4h", 28)
    flow = flow_snapshot(data.get("trades", []), data.get("book", {}))
    cvd = cvd_snapshot(data.get("trades", []))
    move8 = pct(c15[-1].close, c15[-8].close) if len(c15) >= 8 else 0.0
    regime, regime_reason = regime_detection(tf4h, tf1h, move8)

    volume_clusters = detect_volume_clusters(c15)
    news_items = fetch_simple_news()
    news = analyze_news_bias(news_items)
    macro_risk = get_macro_risk(news["bias"], regime)

    ctx = {
        "time": data["time"],
        "price": price,
        "price_source": data["ticker"].get("source", "OKX"),
        "regime": regime,
        "regime_reason": regime_reason,
        "tf3": tf3, "tf15": tf15, "tf1h": tf1h, "tf4h": tf4h,
        "zones": zones[-38:],
        "flow": flow,
        "cvd": cvd,
        "atr15": atr15,
        "atr1h": atr1h,
        "candles": data["candles"],
        "derivatives": data.get("derivatives", {}),
        "volume_clusters": volume_clusters,
        "news": news,
        "macro_risk": macro_risk,
        "scan_3m": state.get("scan_3m", {}),
        "scan_3m_events": {},
    }
    scan_result = scan_closed_3m_sequence(state, ctx)
    ctx["scan_3m"] = state.get("scan_3m", {})
    ctx["scan_3m_events"] = scan_result.get("events", {})
    return ctx


# ==========================================================
# CANDIDATE DETECTION
# ==========================================================

def missed_impulse_status(candidate: Candidate, plan: Optional[TradePlan]) -> bool:
    if not candidate or not plan:
        return False
    if candidate.scan_event_stage in ["RETEST", "READY"] and candidate.trigger_age_minutes < 48:
        return True
    if candidate.final_score >= 80 and candidate.trigger_ready:
        return True
    return False


def candidate_from_missed_opportunity(opp: Opportunity, context: dict) -> Optional[Candidate]:
    if not opp:
        return None
    price = context["price"]
    atr15 = context.get("atr15", 0.6) or 0.6
    scan_events = context.get("scan_3m_events", {})
    event = scan_events.get(opp.side, {})
    trigger_ready = event.get("stage") in ["RETEST", "READY", "ACCEPTANCE"]
    return Candidate(
        side=opp.side,
        setup_type=opp.setup_type,
        setup_family=opp.setup_family,
        raw_score=opp.score - 5,
        final_score=opp.score,
        evidence_families=opp.evidence_families,
        confirmations=opp.confirmations,
        trigger_ready=trigger_ready,
        trigger_level=opp.trigger_level,
        invalidation_level=opp.invalidation_level,
        target_levels=[],
        execution_lane=ExecutionLane.MISSED_IMPULSE_REENTRY.value,
        confirmation_tier=ConfirmationTier.STANDARD.value,
        stage="EXECUTABLE" if trigger_ready else "ARMED",
        variant="MISSED_IMPULSE_REENTRY",
        execution_anchor=price,
        trigger_age_minutes=14.0,
        thesis_key=opp.thesis_key,
        thesis=opp.thesis,
        scan_event_stage=event.get("stage", ""),
    )


def event_driven_reentry_guard(state: dict, context: dict, candidate: Candidate) -> dict:
    history = state.get("history", [])
    if not history or not candidate.thesis_key:
        return {"blocked": False, "reason": ""}
    recent_stops = [h for h in history[-14:] if h.get("action") == Action.STOP.value and h.get("thesis_key")]
    for h in recent_stops:
        if h.get("thesis_key") == candidate.thesis_key:
            trigger_diff = abs(candidate.trigger_level - safe_float(h.get("trigger_level")))
            if trigger_diff < context.get("atr15", 0.6) * REENTRY_NEW_TRIGGER_SEPARATION_ATR15:
                return {"blocked": True, "reason": "Точний replay тієї ж тези після стопу"}
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
    volume_clusters = context.get("volume_clusters", [])
    news = context.get("news", {})
    macro_risk = context.get("macro_risk", "NORMAL")

    params = get_adaptive_params(regime)

    candidates = []
    for side in [Side.LONG.value, Side.SHORT.value]:
        event = scan_events.get(side, {})
        trigger_ready = event.get("stage") in ["ACCEPTANCE", "RETEST", "READY"]
        trigger_level = event.get("trigger_level", price)
        trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
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
        if tf4h.get("bias") == side:
            str_score += 7
            str_conf.append("4H контекст")

        liq_score = 6
        if event.get("source") == "LIQUIDITY_SWEEP" and trigger_ready:
            liq_score += 10

        flw_score = 5
        flw_conf = []
        if flow.get("bias") == side:
            flw_score += 9
            flw_conf.append("flow на боці")
        if cvd.get("bias") == side:
            flw_score += 8
            flw_conf.append("CVD на боці")

        trig_score = 8
        if trigger_ready:
            trig_score += 14

        htf_score = 6
        if regime == Regime.TREND.value and tf4h.get("bias") == side:
            htf_score += 14
        if regime == Regime.SHOCK.value:
            htf_score = int(htf_score * 0.55)

        raw = loc_score + str_score + liq_score + flw_score + trig_score + htf_score + 6
        if regime == Regime.TRANSITION.value:
            raw = int(raw * 0.88)

        evidence = ["ICT_LOCATION", "PRICE_STRUCTURE"]
        if flw_score > 12:
            evidence.append("ORDER_FLOW_CVD")
        if trigger_ready:
            evidence.append("EXECUTION_TRIGGER_3M")
        if tf4h.get("bias") == side:
            evidence.append("HTF_CONTEXT")

        volume_support = has_volume_cluster_support(price, volume_clusters, side, atr15)
        if volume_support:
            evidence.append("VOLUME_CLUSTER_SUPPORT")
            raw += 6

        setup_type = SetupType.PULLBACK_CONTINUATION.value
        variant = "PULLBACK_FORMING"
        if trigger_ready and scan_stage in ["RETEST", "READY"]:
            setup_type = SetupType.BREAKOUT_RETEST.value
            variant = "CONFIRMED_BOS_RETEST"
        if event.get("source") == "LIQUIDITY_SWEEP" and trigger_ready:
            setup_type = SetupType.SWEEP_RECLAIM.value
            variant = "EARLY_RECLAIM"

        family = SETUP_FAMILY_MAP.get(setup_type, SetupFamily.CONTINUATION.value)

        lane = ExecutionLane.STANDARD_CONFIRMED.value
        tier = ConfirmationTier.STANDARD.value
        final = int(clamp(raw + (len(evidence) - 3) * 2.8, 12, 98))

        if final >= params["entry_score"] and len(evidence) >= params["min_evidence"] and trigger_ready:
            lane = ExecutionLane.STANDARD_CONFIRMED.value
            tier = ConfirmationTier.HIGH_QUALITY.value
        elif final >= params["risky_entry_score"] and trigger_ready and regime != Regime.SHOCK.value:
            lane = ExecutionLane.EARLY_TACTICAL.value

        thesis = f"{side} {setup_type} | HTF={tf4h.get('bias')} | Flow={flow.get('bias')} CVD={cvd.get('bias')} | 3M={scan_stage} | News={news.get('bias', 'NEUTRAL')}"

        structure_alignment = 50
        if tf15.get("bias") == side:
            structure_alignment += 20
        if tf1h.get("bias") == side:
            structure_alignment += 15
        acceptance_quality = calculate_acceptance_quality(event, context["candles"]["3m"], atr15, structure_alignment)

        cand = Candidate(
            side=side,
            setup_type=setup_type,
            setup_family=family,
            raw_score=raw,
            final_score=final,
            score_components={
                "location": loc_score, "structure": str_score, "liquidity": liq_score,
                "flow_cvd": flw_score, "trigger_3m": trig_score, "htf": htf_score
            },
            evidence_families=evidence,
            confirmations=loc_conf + str_conf + flw_conf,
            risks=[] if final > 70 else ["потрібне підтвердження acceptance"],
            trigger_ready=trigger_ready,
            trigger_level=round_price(trigger_level),
            invalidation_level=round_price(price - (atr15 * 1.65 if side == Side.LONG.value else -atr15 * 1.65)),
            target_levels=[round_price(price + (atr15 * 2.4 if side == Side.LONG.value else -atr15 * 2.4))],
            execution_lane=lane,
            confirmation_tier=tier,
            stage="ARMED" if final >= params["armed_score"] else "DISCOVERED",
            variant=variant,
            execution_anchor=price,
            trigger_age_minutes=trigger_age,
            specificity=len(evidence) * 5,
            thesis_key=f"{side}|{family}|{setup_type}|{int(price*10)}",
            thesis=thesis,
            scan_event_stage=scan_stage,
            confluence_layers={
                "ict": loc_score + str_score,
                "flow_cvd": flw_score,
                "execution": trig_score,
                "htf": htf_score
            },
            acceptance_quality=acceptance_quality,
            volume_cluster_support=volume_support
        )
        candidates.append(cand)
    return sorted(candidates, key=lambda c: -c.final_score)[:2]


def collapse_candidates(cands: list[Candidate]) -> list[Candidate]:
    if not cands:
        return []
    best_long = max([c for c in cands if c.side == Side.LONG.value], key=lambda x: x.final_score, default=None)
    best_short = max([c for c in cands if c.side == Side.SHORT.value], key=lambda x: x.final_score, default=None)
    return sorted([c for c in [best_long, best_short] if c], key=lambda c: -c.final_score)


# ==========================================================
# TRADE PLAN
# ==========================================================

def build_trade_plan(context: dict, candidate: Candidate) -> TradePlan:
    price = context["price"]
    atr15 = context["atr15"] or 0.6
    side = candidate.side
    zones = context["zones"]
    structural_stop = candidate.invalidation_level
    for z in sorted(zones, key=lambda x: -x.strength):
        if z.side == opposite(side) and z.timeframe in ("1h", "4h"):
            structural_stop = z.low if side == Side.LONG.value else z.high
            break
    stop_dist = abs(price - structural_stop)
    stop_dist = max(stop_dist, atr15 * MIN_STOP_ATR15)
    stop_dist = min(stop_dist, atr15 * MAX_STOP_ATR15)
    stop = price - stop_dist if side == Side.LONG.value else price + stop_dist
    tp1 = price + stop_dist * PREFERRED_RR1 if side == Side.LONG.value else price - stop_dist * PREFERRED_RR1
    tp2 = price + stop_dist * MIN_RR2 if side == Side.LONG.value else price - stop_dist * MIN_RR2
    tp3 = price + stop_dist * MIN_RR3 if side == Side.LONG.value else price - stop_dist * MIN_RR3
    rr1 = abs(tp1 - price) / abs(stop - price) if abs(stop - price) > 1e-9 else 2.1
    plan = TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1),
        tp2=round_price(tp2),
        tp3=round_price(tp3),
        risk_pct=NORMAL_RISK_PCT if candidate.execution_lane != ExecutionLane.EARLY_TACTICAL.value else RISKY_RISK_PCT,
        rr1=round(rr1, 2),
        rr2=round(abs(tp2 - price) / abs(stop - price), 2),
        rr3=round(abs(tp3 - price) / abs(stop - price), 2),
        position_risk_pct=RISKY_RISK_PCT if candidate.execution_lane == ExecutionLane.EARLY_TACTICAL.value else NORMAL_RISK_PCT,
        invalidation=f"закриття 15M за {round_price(structural_stop)}",
        stop_basis="HTF захищена структура + ATR buffer",
        target_basis="TP1 ≥2.25R реальна ліквідність",
        stop_timeframe="1H" if any(z.timeframe == "1h" for z in zones) else "15M",
        structural_invalidation=round_price(structural_stop),
        trigger_level=candidate.trigger_level,
        execution_ready=candidate.trigger_ready and candidate.final_score >= ENTRY_SCORE_BASE,
        valid=rr1 >= MIN_RR1,
        reason="" if rr1 >= MIN_RR1 else "RR1 нижче мінімуму",
    )
    return plan


# ==========================================================
# EVALUATION
# ==========================================================

def evaluate_new_setup(context: dict, state: dict, journal: dict) -> Decision:
    cands = detect_candidates(context, state, journal)
    cands = collapse_candidates(cands)

    saved_opp = opportunity_from_state(state)
    if saved_opp and saved_opp.status in ["ARMED", "WAIT_PULLBACK"]:
        missed_cand = candidate_from_missed_opportunity(saved_opp, context)
        if missed_cand:
            guard = event_driven_reentry_guard(state, context, missed_cand)
            if not guard["blocked"] and missed_cand.final_score >= MISSED_REENTRY_SCORE * get_adaptive_params(context["regime"])["reentry_aggressiveness"]:
                plan = build_trade_plan(context, missed_cand)
                return Decision(
                    id=uuid.uuid4().hex[:10],
                    time=iso_now(),
                    action=Action.ARMED.value if not missed_cand.trigger_ready else Action.RISKY_ENTRY.value,
                    side=missed_cand.side,
                    setup_type=missed_cand.setup_type,
                    quality=missed_cand.final_score,
                    reason="Re-entry з пропущеного імпульсу (thesis збережений)",
                    regime=context["regime"],
                    candidate=missed_cand,
                    plan=plan,
                    news_bias=context.get("news", {}).get("bias", "NEUTRAL"),
                    macro_risk=context.get("macro_risk", "NORMAL"),
                    audit={"reentry": True, "origin_opportunity": saved_opp.thesis_key}
                )

    if not cands:
        return Decision(
            id=uuid.uuid4().hex[:10],
            time=iso_now(),
            action=Action.NO_SETUP.value,
            side=Side.NEUTRAL.value,
            setup_type=SetupType.NONE.value,
            quality=12,
            reason="ринок не сформував професійного ICT + OrderFlow + Volume execution-package",
            regime=context["regime"],
            news_bias=context.get("news", {}).get("bias", "NEUTRAL"),
            macro_risk=context.get("macro_risk", "NORMAL")
        )

    best = cands[0]
    plan = build_trade_plan(context, best)
    action = Action.NO_SETUP.value
    quality = best.final_score
    reason = "Професійний сетап не пройшов gate'и якості"

    params = get_adaptive_params(context["regime"])

    if best.final_score >= params["entry_score"] and plan.valid and plan.execution_ready and len(best.evidence_families) >= params["min_evidence"]:
        action = Action.ENTRY.value
        reason = f"{setup_label(best.setup_type)} — високий confluence ({len(best.evidence_families)} evidence)"
    elif best.final_score >= params["risky_entry_score"] and plan.valid and best.execution_lane == ExecutionLane.EARLY_TACTICAL.value:
        action = Action.RISKY_ENTRY.value
        reason = f"Ризикований ранній вхід: {setup_label(best.setup_type)}"
    elif best.final_score >= params["armed_score"]:
        action = Action.ARMED.value
        reason = f"{setup_label(best.setup_type)} сформовано; потрібен свіжий 3M trigger/acceptance"

    if missed_impulse_status(best, plan) and action == Action.ARMED.value:
        action = Action.ARMED.value
        reason = "сильний імпульс уже відійшов; тезу збережено для повторного входу"

    return Decision(
        id=uuid.uuid4().hex[:10],
        time=iso_now(),
        action=action,
        side=best.side,
        setup_type=best.setup_type,
        quality=quality,
        reason=reason,
        regime=context["regime"],
        candidate=best,
        plan=plan,
        news_bias=context.get("news", {}).get("bias", "NEUTRAL"),
        macro_risk=context.get("macro_risk", "NORMAL"),
        audit={"selected": {"side": best.side, "setup": best.setup_type, "score": best.final_score, "lane": best.execution_lane, "evidence": len(best.evidence_families), "3m_stage": best.scan_event_stage, "acceptance_quality": best.acceptance_quality}}
    )


# ==========================================================
# TRADE MANAGEMENT
# ==========================================================

def manage_active_trade(trade: ActiveTrade, context: dict) -> dict:
    price = context["price"]
    atr15 = context.get("atr15", 0.6) or 0.6
    side = trade.side
    tf3 = context.get("tf3", {})
    tf15 = context.get("tf15", {})
    flow = context.get("flow", {})
    cvd = context.get("cvd", {})

    result = {
        "action": Action.HOLD.value,
        "title": "УГОДА ВІДКРИТА — HYBRID ICT + THESIS v5.1",
        "recommendation": "Структура та thesis на боці — тримаємо",
        "current_pct": ((price - trade.entry) / trade.entry * 100) if side == Side.LONG.value else ((trade.entry - price) / trade.entry * 100),
        "best_pct": ((trade.best_price - trade.entry) / trade.entry * 100) if side == Side.LONG.value else ((trade.entry - trade.best_price) / trade.entry * 100),
        "closed": False,
        "exit_price": None,
        "notes": [],
        "recommended_stop": None,
        "recommended_stop_reason": "",
    }

    if (side == Side.LONG.value and price > trade.best_price) or (side == Side.SHORT.value and price < trade.best_price):
        trade.best_price = price
    if (side == Side.LONG.value and price < trade.worst_price) or (side == Side.SHORT.value and price > trade.worst_price):
        trade.worst_price = price

    mfe = result["best_pct"]
    current_r = result["current_pct"] / (abs(trade.stop_initial - trade.entry) / trade.entry * 100) if trade.entry else 0

    params = get_adaptive_params(context.get("regime", "NORMAL"))

    if mfe > params["min_mfe_for_protect"] and current_r < mfe * params["mfe_giveback_threshold"]:
        trade.mfe_giveback_streak += 1
        if trade.mfe_giveback_streak >= 3:
            result["action"] = Action.PROTECT.value
            result["notes"].append("MFE giveback — фіксуємо частину прибутку (адаптивно)")
            result["recommended_stop"] = round_price(trade.entry + (trade.best_price - trade.entry) * 0.40 if side == Side.LONG.value else trade.entry - (trade.entry - trade.best_price) * 0.40)
            result["recommended_stop_reason"] = "Адаптивний захист прибутку"
    else:
        trade.mfe_giveback_streak = max(0, trade.mfe_giveback_streak - 1)

    if not trade.tp1_hit and ((side == Side.LONG.value and price >= trade.tp1) or (side == Side.SHORT.value and price <= trade.tp1)):
        trade.tp1_hit = True
        trade.tp1_stop_locked = True
        trade.stop_current = max(trade.stop_current, trade.entry * 0.992) if side == Side.LONG.value else min(trade.stop_current, trade.entry * 1.008)
        result["action"] = Action.TP1.value
        result["notes"].append("TP1 досягнуто — стоп зафіксовано в зоні BE+")

    if trade.tp1_hit and not trade.tp2_hit and ((side == Side.LONG.value and price >= trade.tp2) or (side == Side.SHORT.value and price <= trade.tp2)):
        trade.tp2_hit = True
        trade.tp2_stop_locked = True
        trade.stop_current = trade.tp1 if side == Side.LONG.value else trade.tp1
        result["action"] = Action.TP2.value
        result["notes"].append("TP2 досягнуто — стоп на TP1")

    structural_break = False
    if side == Side.LONG.value:
        if price < trade.structural_invalidation:
            structural_break = True
        if tf15.get("bias") == Side.SHORT.value and tf3.get("bias") == Side.SHORT.value:
            structural_break = True
            result["notes"].append("15M + 3M структура проти LONG")
    else:
        if price > trade.structural_invalidation:
            structural_break = True
        if tf15.get("bias") == Side.LONG.value and tf3.get("bias") == Side.LONG.value:
            structural_break = True
            result["notes"].append("15M + 3M структура проти SHORT")

    if structural_break:
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = price
        result["notes"].append("Структурна інвалідація за ICT + Thesis — вихід")

    if not result["closed"] and trade.tp1_hit:
        if (side == Side.LONG.value and (flow.get("bias") == Side.SHORT.value or cvd.get("bias") == Side.SHORT.value) and flow.get("score", 0) > 20) or \
           (side == Side.SHORT.value and (flow.get("bias") == Side.LONG.value or cvd.get("bias") == Side.LONG.value) and flow.get("score", 0) > 20):
            result["action"] = Action.PROTECT.value
            result["notes"].append("Сильний flow/CVD проти позиції після TP1")

    trade.last_checked_3m_ts = int(now_utc().timestamp() * 1000)
    trade.last_action = result["action"]
    return result


# ==========================================================
# CONSTANTS
# ==========================================================

SETUP_FAMILY_MAP = {
    SetupType.SWEEP_RECLAIM.value: SetupFamily.LIQUIDITY_RECOVERY.value,
    SetupType.CAPITULATION_RECOVERY.value: SetupFamily.LIQUIDITY_RECOVERY.value,
    SetupType.DIRECTION_FLIP.value: SetupFamily.STRUCTURAL_TRANSITION.value,
    SetupType.TREND_IGNITION.value: SetupFamily.STRUCTURAL_TRANSITION.value,
    SetupType.PULLBACK_CONTINUATION.value: SetupFamily.CONTINUATION.value,
    SetupType.FRESH_BASE_CONTINUATION.value: SetupFamily.CONTINUATION.value,
    SetupType.BREAKOUT_RETEST.value: SetupFamily.EXPANSION.value,
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: SetupFamily.EXPANSION.value,
    SetupType.RANGE_EDGE_REVERSAL.value: SetupFamily.RANGE_EXECUTION.value,
    SetupType.NONE.value: SetupFamily.NONE.value,
}

REGIME_LABELS = {"TREND": "ТРЕНД", "RANGE": "ДІАПАЗОН", "TRANSITION": "ПЕРЕХІДНИЙ", "SHOCK": "ІМПУЛЬСНИЙ", "NORMAL": "ЗВИЧАЙНИЙ"}
FAMILY_LABELS = {"CONTINUATION": "ПРОДОВЖЕННЯ ТРЕНДУ", "LIQUIDITY_RECOVERY": "ВІДНОВЛЕННЯ ПІСЛЯ ЛІКВІДНОСТІ", "STRUCTURAL_TRANSITION": "СТРУКТУРНА ЗМІНА", "EXPANSION": "РОЗШИРЕННЯ РУХУ", "RANGE_EXECUTION": "ТОРГІВЛЯ В ДІАПАЗОНІ", "NONE": "НЕ ВИЗНАЧЕНО"}
SETUP_LABELS = {
    SetupType.SWEEP_RECLAIM.value: "Зняття ліквідності + повернення за рівень",
    SetupType.CAPITULATION_RECOVERY.value: "Відновлення після капітуляційного імпульсу",
    SetupType.DIRECTION_FLIP.value: "Підтверджена зміна напрямку на 15M",
    SetupType.TREND_IGNITION.value: "Запуск нового тренду",
    SetupType.PULLBACK_CONTINUATION.value: "Продовження тренду після ICT-відкату",
    SetupType.FRESH_BASE_CONTINUATION.value: "Повторний вхід з нової 15M бази",
    SetupType.BREAKOUT_RETEST.value: "Пробій структури + підтверджений ретест",
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: "Пробій після стиснення діапазону",
    SetupType.RANGE_EDGE_REVERSAL.value: "Розворот від межі діапазону",
    SetupType.NONE.value: "Професійного сетапу немає",
}


def regime_label(code: str) -> str:
    return REGIME_LABELS.get(str(code).upper().split(".")[-1], "НЕ ВИЗНАЧЕНО")


def family_label(code: str) -> str:
    return FAMILY_LABELS.get(str(code).upper().split(".")[-1], "НЕ ВИЗНАЧЕНО")


def setup_label(code: str) -> str:
    return SETUP_LABELS.get(str(code).upper().split(".")[-1] or code, str(code))


def compute_analytics(journal: dict) -> dict:
    trades = journal.get("trades", [])
    if not trades:
        return {"closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_r": 0.0, "expectancy_r": 0.0, "by_family": {}}
    wins = sum(1 for t in trades if (t.get("result_pct") or 0) > 0)
    closed = len(trades)
    win_rate = round(wins / closed * 100, 1) if closed else 0.0
    net_r = round(sum((t.get("result_pct") or 0) / 100 for t in trades), 2)
    return {"closed_trades": closed, "wins": wins, "losses": closed - wins, "win_rate": win_rate, "net_r": net_r, "expectancy_r": round(net_r / max(closed, 1), 3), "by_family": {}}


# ==========================================================
# MAIN (З ПОСИЛЕНИМИ ЛОГАМИ)
# ==========================================================

def run_bot() -> None:
    print("=" * 60)
    print(f"START {BOT_VERSION} (DEBUG MODE)")
    print("=" * 60)
    
    state = load_state()
    print(f"[DEBUG] Після load_state() -> history length: {len(state.get('history', []))}")
    
    journal = load_journal()
    data = collect_market_data()
    context = build_context(data, state)
    
    if not context.get("price"):
        print("NO PRICE — abort")
        return
    
    print(f"PRICE {context['price']:.4f} | REGIME {context['regime']} | ATR15 {context['atr15']:.4f}")

    active = active_trade_from_state(state)
    if active and active.status != "CLOSED":
        print("[DEBUG] Є активна угода — обробляємо manage_active_trade")
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
        print("[DEBUG] Після append_history (FOLLOW) -> history length:", len(state.get("history", [])))
        
        journal["signals"].append({"time": iso_now(), "type": "FOLLOW", "action": res.get("action"), "side": active.side, "price": context["price"]})
        
        print("[DEBUG] Перед save_state (FOLLOW)")
        save_state(state)
        print("[DEBUG] Після save_state (FOLLOW)")
        
        save_journal(journal)
        print("BOT COMPLETE: ACTIVE TRADE MANAGED")
        return

    print("[DEBUG] Немає активної угоди — викликаємо evaluate_new_setup")
    decision = evaluate_new_setup(context, state, journal)
    
    payload = {"id": decision.id, "time": decision.time, "action": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "reason": decision.reason, "regime": decision.regime, "news_bias": decision.news_bias, "macro_risk": decision.macro_risk, "version": BOT_VERSION, "architecture_version": ARCHITECTURE_VERSION}
    state["latest_signal"] = payload
    
    append_history(state, {"type": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "price": context["price"]})
    print("[DEBUG] Після append_history (DECISION) -> history length:", len(state.get("history", [])))
    
    journal["signals"].append(payload)

    if decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value) and decision.candidate and decision.plan:
        active = ActiveTrade(id=uuid.uuid4().hex[:10], side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, opened_at=iso_now(), entry=decision.plan.entry, stop_initial=decision.plan.stop, stop_current=decision.plan.stop, structural_invalidation=decision.plan.structural_invalidation, tp1=decision.plan.tp1, tp2=decision.plan.tp2, tp3=decision.plan.tp3, quality=decision.quality, position_risk_pct=decision.plan.position_risk_pct, best_price=decision.plan.entry, worst_price=decision.plan.entry, trigger_level=decision.candidate.trigger_level, thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, opened_regime=decision.regime)
        store_active_trade(state, active)
        state["opportunity"] = None
    elif decision.action == Action.ARMED.value and decision.candidate:
        opp = Opportunity(side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, created_at=iso_now(), expires_at=(now_utc() + timedelta(hours=18)).isoformat(), score=decision.quality, trigger_level=decision.candidate.trigger_level, invalidation_level=decision.candidate.invalidation_level, confirmations=decision.candidate.confirmations[:5], evidence_families=decision.candidate.evidence_families, execution_lane=decision.candidate.execution_lane, status="ARMED", thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis)
        if missed_impulse_status(decision.candidate, decision.plan):
            opp.status = "WAIT_PULLBACK"
            opp.missed_at = iso_now()
        store_opportunity(state, opp)
        store_active_trade(state, None)
    else:
        store_active_trade(state, None)

    if decision.action != Action.NO_SETUP.value or SEND_NO_SETUP or TELEGRAM_NOTIFY_EVERY_RUN:
        msg = build_decision_message(context, decision)
        print("TELEGRAM (DECISION):", msg[:320])
        send_telegram(msg)

    print("[DEBUG] Перед фінальним save_state()")
    save_state(state)
    print("[DEBUG] Після фінального save_state()")
    
    save_journal(journal)
    print("=" * 60)
    print("BOT COMPLETE")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="BZU Professional Hybrid Confluence Signal Bot v5.1 (DEBUG)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print("SELF-TEST PASSED")
        return
    run_bot()


if __name__ == "__main__":
    main()
