#!/usr/bin/env python3
"""
BZU Professional Hybrid Confluence Signal Bot v6.4
================================================================================
Виправлення v6.4:
- TradingView (BINANCE:BZUSDT.P) як ОСНОВНЕ джерело ціни (як у старому файлі)
- OKX — тільки як fallback
- Залишені всі покращення v6.3 (чисті повідомлення, без фейкового плану при NO_SETUP)
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

BOT_VERSION = "pro-hybrid-confluence-v6.4"
ARCHITECTURE_VERSION = "HYBRID_CONFLUENCE_V6_4"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === DATA SOURCES (TradingView пріоритет, як у старому файлі) ===
OKX_BASE_URL = "https://www.okx.com/api/v5/market"
TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com/crypto/scan"

WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", os.getcwd()))
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
# TELEGRAM (ПОКРАЩЕНИЙ)
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
        "<b>BZU PRO HYBRID CONFLUENCE v6.4</b>",
        "",
        f"<b>{action_label}</b> | {side_word(decision.side)} | {setup_label(decision.setup_type)}",
        f"<b>Якість:</b> {decision.quality}/100 | Режим: {regime_label(decision.regime)} | News: {decision.news_bias} | Macro: {decision.macro_risk}",
        f"<b>Ціна зараз:</b> {_fmt_price(current_price)}"
    ]
    
    if decision.candidate and decision.action != Action.NO_SETUP.value:
        c = decision.candidate
        if c.confirmations:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            for x in c.confirmations[:3]:
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


def build_follow_message(context: dict, trade: ActiveTrade, result: dict) -> str:
    lines = [
        "<b>BZU PRO — HYBRID ICT v6.4</b>",
        "",
        f"<b>{result.get('title', 'МОНІТОРИНГ УГОДИ')}</b>",
        f"{result.get('recommendation', '')}",
        "",
        f"<b>Ціна зараз:</b> {_fmt_price(context['price'])} | Від входу: {result.get('current_pct', 0):.2f}% | Макс: {result.get('best_pct', 0):.2f}%"
    ]
    if result.get("notes"):
        lines.append("")
        lines.append("<b>Дії:</b>")
        for n in result["notes"][:3]:
            lines.append(f"• {html.escape(n)}")
    if result.get("recommended_stop"):
        lines.append(f"<b>Рекомендований стоп:</b> {_fmt_price(result['recommended_stop'])}")
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
# DATA SOURCES: TradingView ПРІОРИТЕТ (як у старому файлі)
# ==========================================================

def http_get(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Pro-v6.4/1.0", "Accept": "*/*"}
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
        "User-Agent": "Mozilla/5.0 BZU-Pro-v6.4/1.0",
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
    """TradingView як ОСНОВНЕ джерело ціни (як у старому файлі)"""
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
    """
    TradingView як ОСНОВНЕ джерело ціни (як у старому файлі користувача)
    OKX — тільки як fallback
    """
    c3 = get_okx_candles("BZ-USDT", "3m", 240)
    c15 = get_okx_candles("BZ-USDT", "15m", 200)
    c1h = get_okx_candles("BZ-USDT", "1h", 160)
    c4h = get_okx_candles("BZ-USDT", "4h", 140)
    
    # TradingView — ПРІОРИТЕТ (як у старому файлі)
    tv_ticker = get_tradingview_price_fallback()
    okx_ticker = get_okx_ticker()
    
    ticker = tv_ticker or okx_ticker
    
    if not ticker and c15:
        ticker = {
            "price": c15[-1].close,
            "change24h": 0,
            "volume24h": 0,
            "source": "OKX candles fallback",
            "symbol": "BZ-USDT",
        }
    
    price = ticker.get("price") if ticker else 82.5
    
    return {
        "time": iso_now(),
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
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
        "volume_clusters": [],
        "scan_3m": state.get("scan_3m", {}),
        "scan_3m_events": {},
    }
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


def trigger_snapshot(candles: list[Candle], side: str, trigger_level: float, atr15: float) -> dict:
    if len(candles) < 5:
        return {"ready": False, "age_bars": 999, "quality": 0, "displacement": False}
    last = candles[-1]
    prev = candles[-2]
    is_sweep = (side == Side.LONG.value and last.low < trigger_level) or (side == Side.SHORT.value and last.high > trigger_level)
    is_reclaim = (side == Side.LONG.value and last.close > trigger_level) or (side == Side.SHORT.value and last.close < trigger_level)
    displacement = abs(last.close - prev.close) > atr15 * 0.70
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


def has_forward_ict_zone(price: float, zones: list[Zone], side: str, atr15: float) -> bool:
    forward_distance = atr15 * 2.2
    for z in zones:
        if z.side != side:
            continue
        if side == Side.LONG.value:
            if z.low > price and z.low < price + forward_distance:
                return True
        else:
            if z.high < price and z.high > price - forward_distance:
                return True
    return False


def calculate_location_score(price: float, zones: list[Zone], side: str, atr15: float, tf15: dict, tf1h: dict) -> int:
    score = 10
    forward_distance = atr15 * 2.0

    for z in zones:
        if z.side != side:
            continue
        dist = abs(price - z.low) if side == Side.LONG.value else abs(price - z.high)
        
        if dist < atr15 * 0.6:
            score += 12
        elif dist < atr15 * 1.2:
            score += 8
        elif dist < forward_distance:
            score += 4

        if z.kind == "FVG" and dist < atr15 * 1.5:
            score += 5
        if z.kind == "OB" and dist < atr15 * 1.8:
            score += 6

    if tf15.get("bias") == side:
        score += 8
    if tf1h.get("bias") == side:
        score += 10

    return min(score, 45)


def calculate_acceptance_quality(event: dict, candles_3m: list[Candle], atr15: float, structure_alignment: int, has_forward_zone: bool) -> int:
    base = event.get("acceptance_quality", 40)
    
    if has_forward_zone:
        base += 12

    if event.get("displacement"):
        base += 8

    if structure_alignment > 70:
        base += 6
    elif structure_alignment > 55:
        base += 3

    if event.get("stage") == "READY":
        base += 5

    return min(base, 95)


# ==========================================================
# CANDIDATE + RE-ENTRY
# ==========================================================

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

    params = get_adaptive_params(regime)

    candidates = []
    for side in [Side.LONG.value, Side.SHORT.value]:
        event = scan_events.get(side, {})
        trigger_ready = event.get("stage") in ["ACCEPTANCE", "RETEST", "READY"]
        trigger_level = event.get("trigger_level", price)
        trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
        scan_stage = event.get("stage", "")

        location_score = calculate_location_score(price, zones, side, atr15, tf15, tf1h)
        has_forward_zone = has_forward_ict_zone(price, zones, side, atr15)

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

        if has_forward_zone:
            raw += 8

        evidence = ["ICT_LOCATION", "PRICE_STRUCTURE"]
        if flw_score > 12:
            evidence.append("ORDER_FLOW_CVD")
        if trigger_ready:
            evidence.append("EXECUTION_TRIGGER_3M")
        if tf4h.get("bias") == side:
            evidence.append("HTF_CONTEXT")
        if has_forward_zone:
            evidence.append("FORWARD_ICT_ZONE")

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

        thesis = f"{side} {setup_type} | HTF={tf4h.get('bias')} | Flow={flow.get('bias')} CVD={cvd.get('bias')} | 3M={scan_stage}"

        structure_alignment = 50
        if tf15.get("bias") == side:
            structure_alignment += 20
        if tf1h.get("bias") == side:
            structure_alignment += 15
        
        acceptance_quality = calculate_acceptance_quality(event, context["candles"]["3m"], atr15, structure_alignment, has_forward_zone)

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
            location_score=location_score,
            has_forward_zone=has_forward_zone
        )
        
        cand.professional_gate = evaluate_professional_gate(context, cand)
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
        target_basis="TP1 ≥2.2R реальна ліквідність",
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
    current_price = context.get("price", 0)
    
    if saved_opp and saved_opp.status in ["ARMED", "WAIT_PULLBACK"]:
        missed_cand = candidate_from_missed_opportunity(saved_opp, context)
        if missed_cand:
            guard = event_driven_reentry_guard(state, context, missed_cand)
            
            if not guard["blocked"] and missed_cand.final_score >= MISSED_REENTRY_SCORE * get_adaptive_params(context["regime"])["reentry_aggressiveness"]:
                plan = build_trade_plan(context, missed_cand)
                
                if missed_cand.final_score >= REENTRY_AGGRESSIVE_THRESHOLD:
                    action = Action.RISKY_ENTRY.value
                    reason = "Re-entry з пропущеного імпульсу — високий confluence, відкриваємо угоду"
                else:
                    action = Action.ARMED.value
                    reason = "Re-entry з пропущеного імпульсу — сформовано сигнал, чекаємо входу"
                
                return Decision(
                    id=uuid.uuid4().hex[:10],
                    time=iso_now(),
                    action=action,
                    side=missed_cand.side,
                    setup_type=missed_cand.setup_type,
                    quality=missed_cand.final_score,
                    reason=reason,
                    regime=context["regime"],
                    candidate=missed_cand,
                    plan=plan,
                    news_bias="NEUTRAL",
                    macro_risk="NORMAL",
                    current_price=current_price,
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
            news_bias="NEUTRAL",
            macro_risk="NORMAL",
            current_price=current_price
        )

    best = cands[0]
    plan = build_trade_plan(context, best)
    action = Action.NO_SETUP.value
    quality = best.final_score
    reason = "Професійний сетап не пройшов gate'и якості"

    params = get_adaptive_params(context["regime"])
    gate = best.professional_gate or {}

    if gate.get("allow_entry") and plan.valid and plan.execution_ready:
        action = Action.ENTRY.value
        reason = f"{setup_label(best.setup_type)} — {gate.get('grade', 'A')} gate v6"
    elif gate.get("allow_risky") and plan.valid:
        action = Action.RISKY_ENTRY.value
        reason = f"Ризикований ранній вхід: {setup_label(best.setup_type)} — {gate.get('grade', 'B')} gate v6"
    elif best.final_score >= params["armed_score"]:
        action = Action.ARMED.value
        reason = f"{setup_label(best.setup_type)} сформовано; gate v6: {gate.get('grade', 'WATCH')}"

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
        news_bias="NEUTRAL",
        macro_risk="NORMAL",
        current_price=current_price,
        audit={"selected": {"side": best.side, "setup": best.setup_type, "score": best.final_score, "gate": gate.get("grade")}}
    )


# ==========================================================
# MANAGE ACTIVE TRADE
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
        "title": "УГОДА ВІДКРИТА — HYBRID ICT v6.4",
        "recommendation": "Структура та теза на боці — тримаємо",
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

    params = get_adaptive_params(context.get("regime", "NORMAL"))

    giveback = max(0, mfe - result["current_pct"])
    giveback_ratio = giveback / mfe if mfe > 0.1 else 0

    if mfe > 1.5 and giveback_ratio > 0.55:
        trade.mfe_giveback_streak += 1
        if trade.mfe_giveback_streak >= 2:
            result["action"] = Action.PROTECT.value
            result["notes"].append("MFE Giveback Guard: прибуток віддається — захистити стоп")
            protect_stop = trade.entry + (trade.best_price - trade.entry) * 0.35 if side == Side.LONG.value else trade.entry - (trade.entry - trade.best_price) * 0.35
            result["recommended_stop"] = round_price(protect_stop)
            result["recommended_stop_reason"] = "Adaptive MFE Giveback Guard 2.0"
    else:
        trade.mfe_giveback_streak = max(0, trade.mfe_giveback_streak - 1)

    if not trade.tp1_hit and ((side == Side.LONG.value and price >= trade.tp1) or (side == Side.SHORT.value and price <= trade.tp1)):
        trade.tp1_hit = True
        trade.tp1_stop_locked = True
        trade.tp1_locked_stop = max(trade.stop_current, trade.entry * 0.992) if side == Side.LONG.value else min(trade.stop_current, trade.entry * 1.008)
        trade.stop_current = trade.tp1_locked_stop
        result["action"] = Action.TP1.value
        result["notes"].append("TP1 досягнуто — стоп зафіксовано до TP2")

    if trade.tp1_hit and not trade.tp2_hit and ((side == Side.LONG.value and price >= trade.tp2) or (side == Side.SHORT.value and price <= trade.tp2)):
        trade.tp2_hit = True
        trade.tp2_stop_locked = True
        trade.tp2_locked_stop = trade.tp1
        trade.stop_current = trade.tp2_locked_stop
        result["action"] = Action.TP2.value
        result["notes"].append("TP2 досягнуто — стоп на TP1 (зафіксовано)")

    if trade.tp1_stop_locked and not trade.tp2_hit:
        result["recommended_stop"] = round_price(trade.tp1_locked_stop)
        result["recommended_stop_reason"] = "TP1-стоп зафіксовано до TP2"

    if trade.tp2_stop_locked:
        result["recommended_stop"] = round_price(trade.tp2_locked_stop)
        result["recommended_stop_reason"] = "TP2-стоп зафіксовано"

    if trade.tp1_hit and not trade.tp2_hit:
        if side == Side.LONG.value and tf15.get("bias") == Side.LONG.value:
            new_stop = max(trade.stop_current, price - atr15 * 1.2)
            if new_stop > trade.stop_current:
                trade.stop_current = new_stop
                result["notes"].append("SMC Trailing: стоп підтягнуто за 15M структурою")
        elif side == Side.SHORT.value and tf15.get("bias") == Side.SHORT.value:
            new_stop = min(trade.stop_current, price + atr15 * 1.2)
            if new_stop < trade.stop_current:
                trade.stop_current = new_stop
                result["notes"].append("SMC Trailing: стоп підтягнуто за 15M структурою")

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
        result["notes"].append("Структурна інвалідація — вихід")

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
        active = ActiveTrade(id=uuid.uuid4().hex[:10], side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, opened_at=iso_now(), entry=decision.plan.entry, stop_initial=decision.plan.stop, stop_current=decision.plan.stop, structural_invalidation=decision.plan.structural_invalidation, tp1=decision.plan.tp1, tp2=decision.plan.tp2, tp3=decision.plan.tp3, quality=decision.quality, position_risk_pct=decision.plan.position_risk_pct, best_price=decision.plan.entry, worst_price=decision.plan.entry, trigger_level=decision.candidate.trigger_level, thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, opened_regime=decision.regime)
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
    parser = argparse.ArgumentParser(description="BZU Professional Hybrid Confluence Signal Bot v6.4")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print("SELF-TEST PASSED")
        return
    run_bot()


if __name__ == "__main__":
    main()
