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
# BZU PROFESSIONAL ICT SIGNAL BOT
# ==========================================================
# One market snapshot -> one multi-timeframe ICT map -> one execution decision.
# Setup variants share five semantic families, so overlapping detectors cannot
# issue competing permissions for the same market episode.
# ==========================================================

BOT_VERSION = "pro-ict-v3.2.7-event-chain-confirmation-tiers"
ARCHITECTURE_VERSION = "DETERMINISTIC_PRO_ICT_CORE_V3_2_7_EVENT_CHAIN_CONFIRMATION_TIERS"

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
# The public plan is built around a real 1:2 minimum. A stale environment
# variable may raise these floors, but it cannot silently lower them.
MIN_STOP_ATR15 = max(0.82, float(os.getenv("MIN_STOP_ATR15", "0.82") or 0.82))
MAX_STOP_ATR15 = float(os.getenv("MAX_STOP_ATR15", "2.60") or 2.60)
MIN_TP1_ATR15 = max(1.35, float(os.getenv("MIN_TP1_ATR15", "1.35") or 1.35))
MIN_RR1 = max(2.00, float(os.getenv("MIN_RR1", "2.00") or 2.00))
PREFERRED_RR1 = max(2.20, float(os.getenv("PREFERRED_RR1", "2.20") or 2.20))
MIN_RR2 = max(3.00, float(os.getenv("MIN_RR2", "3.00") or 3.00))
MIN_RR3 = max(4.00, float(os.getenv("MIN_RR3", "4.00") or 4.00))
ENTRY_ZONE_EXTENSION_ATR15 = float(os.getenv("ENTRY_ZONE_EXTENSION_ATR15", "0.24") or 0.24)
ENTRY_ZONE_RETEST_ATR15 = float(os.getenv("ENTRY_ZONE_RETEST_ATR15", "0.08") or 0.08)
OPPORTUNITY_TTL_MIN = int(os.getenv("OPPORTUNITY_TTL_MIN", "60") or 60)
LATE_EXTENSION_ATR15 = float(os.getenv("LATE_EXTENSION_ATR15", "1.05") or 1.05)

# Professional setup engine. These are detector parameters, not post-decision
# gates. Every candidate is evaluated once by the same evidence and geometry
# pipeline.
MIN_ENTRY_EVIDENCE = int(os.getenv("MIN_ENTRY_EVIDENCE", "4") or 4)
MIN_RISKY_EVIDENCE = int(os.getenv("MIN_RISKY_EVIDENCE", "3") or 3)
EARLY_DIRECTION_FLIP_MIN_ATR = float(os.getenv("EARLY_DIRECTION_FLIP_MIN_ATR", "0.90") or 0.90)
FULL_DIRECTION_FLIP_MIN_ATR = float(os.getenv("FULL_DIRECTION_FLIP_MIN_ATR", "1.20") or 1.20)
CAPITULATION_MIN_RUN_ATR = float(os.getenv("CAPITULATION_MIN_RUN_ATR", "2.40") or 2.40)
CAPITULATION_MIN_RUN_PCT = float(os.getenv("CAPITULATION_MIN_RUN_PCT", "2.80") or 2.80)
CAPITULATION_MAX_AGE_15M = int(os.getenv("CAPITULATION_MAX_AGE_15M", "7") or 7)
FRESH_BASE_MIN_BARS = int(os.getenv("FRESH_BASE_MIN_BARS", "3") or 3)
FRESH_BASE_MAX_BARS = int(os.getenv("FRESH_BASE_MAX_BARS", "6") or 6)
FRESH_BASE_MAX_WIDTH_ATR = float(os.getenv("FRESH_BASE_MAX_WIDTH_ATR", "1.60") or 1.60)
COMPRESSION_MAX_WIDTH_ATR = float(os.getenv("COMPRESSION_MAX_WIDTH_ATR", "2.35") or 2.35)
TRIGGER_MAX_AGE_MINUTES = int(os.getenv("TRIGGER_MAX_AGE_MINUTES", "27") or 27)
MAX_ADVERSE_EVIDENCE = int(os.getenv("MAX_ADVERSE_EVIDENCE", "1") or 1)

# Multi-timeframe professional execution. 4H defines the scenario, 1H defines
# the institutional area, 15M defines the trade invalidation, and 3M is only
# the execution trigger. A stop is never created by mechanically widening a
# micro 3M level: it must sit beyond an actual protected structure/zone.
HTF_ZONE_ACCEPTANCE_3M_CLOSES = int(os.getenv("HTF_ZONE_ACCEPTANCE_3M_CLOSES", "2") or 2)
HTF_ZONE_RETEST_ATR15 = float(os.getenv("HTF_ZONE_RETEST_ATR15", "0.14") or 0.14)
HTF_ZONE_BUFFER_ATR15 = float(os.getenv("HTF_ZONE_BUFFER_ATR15", "0.05") or 0.05)
MAX_ENTRY_EXTENSION_TACTICAL = float(os.getenv("MAX_ENTRY_EXTENSION_TACTICAL", "0.35") or 0.35)
MAX_ENTRY_EXTENSION_STANDARD = float(os.getenv("MAX_ENTRY_EXTENSION_STANDARD", "0.50") or 0.50)
MAX_ENTRY_EXTENSION_RECOVERY = float(os.getenv("MAX_ENTRY_EXTENSION_RECOVERY", "0.60") or 0.60)
MAX_ENTRY_EXTENSION_TRANSITION = float(os.getenv("MAX_ENTRY_EXTENSION_TRANSITION", "0.40") or 0.40)
MAX_ENTRY_EXTENSION_BASE = float(os.getenv("MAX_ENTRY_EXTENSION_BASE", "0.45") or 0.45)
MAX_ENTRY_EXTENSION_RANGE = float(os.getenv("MAX_ENTRY_EXTENSION_RANGE", "0.35") or 0.35)
FAILED_ENTRY_MIN_MFE_R = float(os.getenv("FAILED_ENTRY_MIN_MFE_R", "0.35") or 0.35)
# Re-entry is event-driven, never time-blocked. The bot may re-enter immediately
# after a stop when a genuinely new sweep/BOS/retest/base appears. It suppresses
# only an exact replay of the failed thesis with the same stale trigger.
REENTRY_NEW_TRIGGER_SEPARATION_ATR15 = float(os.getenv("REENTRY_NEW_TRIGGER_SEPARATION_ATR15", "0.18") or 0.18)
REENTRY_NEW_INVALIDATION_SEPARATION_ATR15 = float(os.getenv("REENTRY_NEW_INVALIDATION_SEPARATION_ATR15", "0.28") or 0.28)
EDGE_MIN_SAMPLE = int(os.getenv("EDGE_MIN_SAMPLE", "5") or 5)
EDGE_PRIOR_TRADES = float(os.getenv("EDGE_PRIOR_TRADES", "6") or 6)
EDGE_PRIOR_EXPECTANCY_R = float(os.getenv("EDGE_PRIOR_EXPECTANCY_R", "0.05") or 0.05)
EDGE_MAX_LOOKBACK = int(os.getenv("EDGE_MAX_LOOKBACK", "60") or 60)
REQUIRE_NATURAL_TP1 = os.getenv("REQUIRE_NATURAL_TP1", "true").strip().lower() in {"1", "true", "yes"}

# Three independent execution lanes. These are not generic score overrides:
# every lane keeps the same ICT detectors, evidence model, structural geometry,
# natural targets and anti-replay guard, but asks for a different execution
# package appropriate to the market event.
EARLY_TACTICAL_SCORE = max(64, int(os.getenv("EARLY_TACTICAL_SCORE", "64") or 64))
STANDARD_CONFIRMED_SCORE = max(72, int(os.getenv("STANDARD_CONFIRMED_SCORE", "72") or 72))
MISSED_REENTRY_SCORE = max(64, int(os.getenv("MISSED_REENTRY_SCORE", "64") or 64))
HIGH_QUALITY_CONTINUATION_SCORE = max(78, int(os.getenv("HIGH_QUALITY_CONTINUATION_SCORE", "78") or 78))
HIGH_QUALITY_HTF_BONUS = 12
TRANSITION_HTF_PENALTY = -20

# Confirmation Tiers (Етап 3)
TIER_MICRO = 1
TIER_STANDARD = 2
TIER_HIGH_QUALITY = 3
TIER_PREMIUM = 4
TACTICAL_MIN_STOP_ATR15 = max(0.58, float(os.getenv("TACTICAL_MIN_STOP_ATR15", "0.58") or 0.58))
TACTICAL_MAX_STOP_ATR15 = max(1.40, float(os.getenv("TACTICAL_MAX_STOP_ATR15", "1.90") or 1.90))
REENTRY_ZONE_TOLERANCE_ATR15 = float(os.getenv("REENTRY_ZONE_TOLERANCE_ATR15", "0.12") or 0.12)
SCAN_3M_BOOTSTRAP_BARS = max(8, int(os.getenv("SCAN_3M_BOOTSTRAP_BARS", "12") or 12))
SCAN_3M_MAX_EVENT_EXTENSION_ATR15 = float(os.getenv("SCAN_3M_MAX_EVENT_EXTENSION_ATR15", "1.80") or 1.80)

# Telegram delivery policy. The bot is scheduled every 15 minutes, therefore a
# status message is delivered on every successful run by default. Users may
# explicitly disable this with TELEGRAM_NOTIFY_EVERY_RUN=false.
TELEGRAM_NOTIFY_EVERY_RUN = os.getenv("TELEGRAM_NOTIFY_EVERY_RUN", "true").strip().lower() in {"1", "true", "yes"}
SEND_NO_SETUP = os.getenv("SEND_NO_SETUP", "true").strip().lower() in {"1", "true", "yes"}
SEND_DUPLICATE_STATUS = os.getenv("SEND_DUPLICATE_STATUS", "true").strip().lower() in {"1", "true", "yes"}
TELEGRAM_MAX_LENGTH = max(500, min(4000, int(os.getenv("TELEGRAM_MAX_LENGTH", "3900") or 3900)))


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
    DIRECTION_FLIP = "CLOSED_15M_DIRECTION_FLIP"
    TREND_IGNITION = "TREND_IGNITION_ENTRY"
    PULLBACK_CONTINUATION = "PULLBACK_CONTINUATION"
    FRESH_BASE_CONTINUATION = "FRESH_BASE_CONTINUATION_REENTRY"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    RANGE_COMPRESSION_BREAKOUT = "RANGE_COMPRESSION_BREAKOUT"
    RANGE_EDGE_REVERSAL = "RANGE_EDGE_REVERSAL"
    NONE = "NONE"



REGIME_LABELS = {
    "TREND": "ТРЕНД",
    "RANGE": "ДІАПАЗОН",
    "TRANSITION": "ПЕРЕХІДНИЙ РЕЖИМ",
    "SHOCK": "ІМПУЛЬСНИЙ РЕЖИМ",
    "NORMAL": "ЗВИЧАЙНИЙ РИНОК",
}

FAMILY_LABELS = {
    "CONTINUATION": "ПРОДОВЖЕННЯ ТРЕНДУ",
    "LIQUIDITY_RECOVERY": "ВІДНОВЛЕННЯ ПІСЛЯ ЛІКВІДНОСТІ",
    "STRUCTURAL_TRANSITION": "СТРУКТУРНА ЗМІНА",
    "EXPANSION": "РОЗШИРЕННЯ РУХУ",
    "RANGE_EXECUTION": "ТОРГІВЛЯ В ДІАПАЗОНІ",
    "NONE": "НЕ ВИЗНАЧЕНО",
}

STAGE_LABELS = {
    "DISCOVERED": "ВИЯВЛЕНО",
    "FORMING": "ФОРМУЄТЬСЯ",
    "ARMED": "ГОТОВИЙ ДО ПІДТВЕРДЖЕННЯ",
    "EXECUTABLE": "ГОТОВИЙ ДО ВХОДУ",
    "READY": "ГОТОВО",
    "INVALIDATED": "СКАСОВАНО",
    "EXPIRED": "ВТРАТИВ АКТУАЛЬНІСТЬ",
}

def _normalize_service_code(value: object) -> str:
    text = str(value or "").strip().upper()
    # Handles Enum values such as Regime.RANGE and serialized forms.
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text.strip().strip("<>'\"")


def regime_label(value: str) -> str:
    code = _normalize_service_code(value)
    return REGIME_LABELS.get(code, "НЕ ВИЗНАЧЕНО")

def family_label(value: str) -> str:
    code = _normalize_service_code(value)
    return FAMILY_LABELS.get(code, "НЕ ВИЗНАЧЕНО")

def stage_label(value: str) -> str:
    code = _normalize_service_code(value)
    return STAGE_LABELS.get(code, "НЕ ВИЗНАЧЕНО")

def ua_service_text(value: str) -> str:
    text = str(value or "")
    replacements = {
        "RANGE": "діапазон",
        "TREND": "тренд",
        "SHOCK": "імпульсний режим",
        "NORMAL": "звичайний ринок",
        "LIQUIDITY_RECOVERY": "відновлення після ліквідності",
        "STRUCTURAL_TRANSITION": "структурна зміна",
        "CONTINUATION": "продовження тренду",
        "EXPANSION": "розширення руху",
        "RANGE_EXECUTION": "торгівля в діапазоні",
        "micro-break": "локальний мікропробій",
        "acceptance": "прийняття рівня",
        "ARMED": "очікування підтвердження",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text

SETUP_LABELS = {
    SetupType.SWEEP_RECLAIM.value: "Зняття ліквідності та повернення за рівень",
    SetupType.CAPITULATION_RECOVERY.value: "Відновлення після капітуляційного руху",
    SetupType.DIRECTION_FLIP.value: "Підтверджена зміна напрямку на 15M",
    SetupType.TREND_IGNITION.value: "Запуск нового тренду",
    SetupType.PULLBACK_CONTINUATION.value: "Продовження тренду після ICT-відкату",
    SetupType.FRESH_BASE_CONTINUATION.value: "Повторний вхід із нової 15M бази",
    SetupType.BREAKOUT_RETEST.value: "Пробій структури та підтверджений ретест",
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: "Пробій після стиснення діапазону",
    SetupType.RANGE_EDGE_REVERSAL.value: "Розворот від межі діапазону",
    SetupType.NONE.value: "Чистого професійного сетапу немає",
}


SETUP_FAMILY = {
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
    setup_family: str = ""
    variant: str = ""
    stage: str = "DISCOVERED"
    evidence_families: list[str] = field(default_factory=list)
    trigger_ts: int = 0
    execution_profile: str = "STANDARD"
    specificity: int = 0
    execution_anchor: float = 0.0
    trigger_age_minutes: float = 0.0
    edge_sample_size: int = 0
    edge_expectancy_r: float = 0.0
    edge_adjustment: int = 0
    edge_risk_multiplier: float = 1.0
    thesis_key: str = ""
    execution_lane: str = "STANDARD_CONFIRMED"
    scan_event_stage: str = ""
    confirmation_tier: int = 1  # 1=Micro, 2=Standard, 3=High Quality, 4=Premium


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
    structural_invalidation: float = 0.0
    wait_zone_low: float = 0.0
    wait_zone_high: float = 0.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    trigger_level: float = 0.0
    trigger_condition: str = ""
    execution_ready: bool = False
    execution_reason: str = ""
    stop_timeframe: str = ""
    stop_source_level: float = 0.0
    htf_scenario: str = ""
    blocking_zone: str = ""
    acceptance_status: str = ""
    natural_tp1: bool = False
    max_entry_extension_atr: float = 0.0
    optimal_entry_for_rr: float = 0.0
    natural_target_level: float = 0.0
    entry_logic: str = ""


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
    setup_family: str = ""
    variant: str = ""
    stage: str = "ARMED"
    evidence_families: list[str] = field(default_factory=list)
    execution_profile: str = "STANDARD"
    execution_anchor: float = 0.0
    status: str = "ARMED"
    execution_lane: str = "STANDARD_CONFIRMED"
    origin_trigger_ts: int = 0
    missed_at: str = ""
    reentry_zone_low: float = 0.0
    reentry_zone_high: float = 0.0
    natural_target_level: float = 0.0
    optimal_entry_for_rr: float = 0.0


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
    setup_family: str = ""
    variant: str = ""
    execution_profile: str = "STANDARD"
    trigger_level: float = 0.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    initial_atr15: float = 0.0
    opened_regime: str = ""
    failure_checks: int = 0
    initial_risk: float = 0.0
    thesis_key: str = ""


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
    same_architecture = raw.get("architecture_version") == ARCHITECTURE_VERSION
    return {
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "active_trade": raw.get("active_trade"),
        "opportunity": raw.get("opportunity") if same_architecture else None,
        # A scanner timestamp/event belongs to one architecture only. Carrying
        # it across versions can skip fresh candles or revive an incompatible
        # event shape, so a version migration deliberately bootstraps anew.
        "scan_3m": _normalize_scan3m_state(raw.get("scan_3m")) if same_architecture else _empty_scan3m_state(),
        "latest_signal": raw.get("latest_signal"),
        "last_message_key": raw.get("last_message_key", "") if same_architecture else "",
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
        clean.setdefault("trigger_level", safe_float(raw.get("trigger_level") or raw.get("entry")))
        clean.setdefault("entry_zone_low", safe_float(raw.get("entry_zone_low") or raw.get("entry")))
        clean.setdefault("entry_zone_high", safe_float(raw.get("entry_zone_high") or raw.get("entry")))
        clean.setdefault("initial_atr15", 0.0)
        clean.setdefault("opened_regime", str(raw.get("opened_regime") or "MIGRATED"))
        clean.setdefault("failure_checks", 0)
        clean.setdefault("initial_risk", abs(safe_float(raw.get("entry")) - safe_float(raw.get("stop_initial") or raw.get("stop_current"))))
        clean.setdefault("thesis_key", str(raw.get("thesis_key") or ""))
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
    headers = {"User-Agent": "Mozilla/5.0 BZU-Professional-ICT/3.0"}
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


def trigger_snapshot(candles_3m: list[Candle], side: str, reference_level: float = 0.0) -> dict[str, Any]:
    """Scan the complete 3M interval between scheduled bot runs.

    A trigger may occur before the newest candle. It remains valid only while
    subsequent closed candles preserve the level and the event is not stale.
    This replaces the old rescue lanes with one central execution model.
    """
    items = closed(candles_3m)
    if len(items) < 10:
        return {"ready": False, "score": 0, "level": reference_level, "reason": "недостатньо 3M даних"}
    local_atr = max(atr(items, 14), items[-1].close * 0.0005)
    latest_ts = int(items[-1].ts)
    scan_from = max(2, len(items) - 9)
    fallback_structure = structure_snapshot(items, "3m")

    for idx in range(len(items) - 1, scan_from - 1, -1):
        event = items[idx]
        prev = items[idx - 1]
        history = items[:idx + 1]
        structure = structure_snapshot(history[-80:], "3m")
        body = abs(event.close - event.open)
        displacement = body >= 0.55 * local_atr
        momentum = (
            event.close > event.open and event.close > prev.high
            if side == Side.LONG.value
            else event.close < event.open and event.close < prev.low
        )
        structure_ok = structure.get("bos") == side or structure.get("choch") == side or structure.get("bias") == side
        event_level_hold = True
        if reference_level:
            event_level_hold = event.close > reference_level if side == Side.LONG.value else event.close < reference_level
        event_ready = bool(structure_ok and event_level_hold and (displacement or momentum))
        if not event_ready:
            continue

        after = items[idx:]
        current_hold = True
        invalidated = False
        if reference_level:
            if side == Side.LONG.value:
                current_hold = items[-1].close > reference_level
                invalidated = any(c.close < reference_level - 0.12 * local_atr for c in after[1:])
            else:
                current_hold = items[-1].close < reference_level
                invalidated = any(c.close > reference_level + 0.12 * local_atr for c in after[1:])
        else:
            if side == Side.LONG.value:
                invalidated = items[-1].close < event.low - 0.10 * local_atr
            else:
                invalidated = items[-1].close > event.high + 0.10 * local_atr

        age_minutes = max(0.0, (latest_ts - int(event.ts)) / 60000.0)
        age_bars = len(items) - 1 - idx
        if invalidated or not current_hold or age_minutes > TRIGGER_MAX_AGE_MINUTES:
            continue

        follow_closes = sum(
            c.close > reference_level if side == Side.LONG.value else c.close < reference_level
            for c in after
        ) if reference_level else len(after)
        score = 0
        if structure_ok:
            score += 7
        if event_level_hold:
            score += 3
        if displacement:
            score += 3
        if momentum:
            score += 2
        if age_bars > 0 and follow_closes >= min(2, len(after)):
            score += 1
        return {
            "ready": True,
            "score": min(15, score),
            "level": round_price(reference_level or event.close),
            "event_price": round_price(event.close),
            "event_ts": int(event.ts),
            "age_bars": age_bars,
            "age_minutes": round(age_minutes, 2),
            "displacement": displacement,
            "momentum": momentum,
            "follow_through": bool(age_bars == 0 or follow_closes >= min(2, len(after))),
            "structure": structure,
            "reason": "закритий 3M BOS/CHOCH + displacement утримується після події",
        }

    return {
        "ready": False,
        "score": 0,
        "level": round_price(reference_level),
        "event_price": 0.0,
        "event_ts": 0,
        "age_bars": 999,
        "age_minutes": 999.0,
        "displacement": False,
        "momentum": False,
        "follow_through": False,
        "structure": fallback_structure,
        "reason": "у останніх закритих 3M свічках немає чинного BOS/CHOCH-displacement trigger",
    }

def _empty_scan3m_state() -> dict[str, Any]:
    return {
        "last_scanned_3m_ts": 0,
        "last_run_processed": 0,
        "processed_count": 0,
        "events": {},
    }


def _normalize_scan3m_state(raw: Any) -> dict[str, Any]:
    result = _empty_scan3m_state()
    if not isinstance(raw, dict):
        return result
    result["last_scanned_3m_ts"] = int(raw.get("last_scanned_3m_ts", 0) or 0)
    result["last_run_processed"] = int(raw.get("last_run_processed", 0) or 0)
    result["processed_count"] = int(raw.get("processed_count", 0) or 0)
    events = raw.get("events") if isinstance(raw.get("events"), dict) else {}
    for side in (Side.LONG.value, Side.SHORT.value):
        event = events.get(side)
        if isinstance(event, dict):
            result["events"][side] = dict(event)
    return result


def _new_scan3m_event(
    side: str,
    candle: Candle,
    trigger_level: float,
    invalidation_level: float,
    stage: str,
    source: str,
    displacement: bool,
    sweep_level: float = 0.0,
) -> dict[str, Any]:
    return {
        "side": side,
        "stage": stage,
        "source": source,
        "event_ts": int(candle.ts),
        "last_event_ts": int(candle.ts),
        "trigger_level": round_price(trigger_level or candle.close),
        "event_price": round_price(candle.close),
        "invalidation_level": round_price(invalidation_level),
        "sweep_level": round_price(sweep_level),
        "extreme": round_price(candle.low if side == Side.LONG.value else candle.high),
        "displacement": bool(displacement),
        "retest_ts": 0,
        "ready_ts": 0,
        "hold_closes": 0,
        "previous_stage": "",
        "chain_quality": 50,           # 0-100, якість ланцюжка подій
        "confirmation_quality": 40,    # 0-100, наскільки добре підтверджена подія
        "acceptance_quality": 0,       # 0-100, якість acceptance
        "retest_quality": 0,           # 0-100, якість retest/hold
    }
        "processed_bars": 1,
        "valid": True,
    }


def scan_closed_3m_sequence(state: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Process every newly closed 3M candle in chronological order.

    The scanner persists a compact event state machine across 15-minute cron
    runs. A trigger found on the first candle of the interval is therefore not
    lost when the newest candle is only a retest or hold. Events are removed by
    structural invalidation, never merely because a fixed number of minutes
    elapsed.
    """
    memory = _normalize_scan3m_state(state.get("scan_3m"))
    items = closed((context.get("candles") or {}).get("3m", []))
    if not items:
        context["scan_3m"] = memory
        context["scan_3m_events"] = memory["events"]
        state["scan_3m"] = memory
        return memory

    last_ts = int(memory.get("last_scanned_3m_ts", 0) or 0)
    if last_ts <= 0:
        selected = items[-SCAN_3M_BOOTSTRAP_BARS:]
    else:
        selected = [c for c in items if int(c.ts) > last_ts]

    index_by_ts = {int(c.ts): i for i, c in enumerate(items)}
    events = dict(memory.get("events") or {})

    for candle in selected:
        idx = index_by_ts.get(int(candle.ts), -1)
        if idx < 2:
            continue
        history = items[: idx + 1]
        prior = history[max(0, len(history) - 19):-1]
        if len(prior) < 5:
            continue
        prev = history[-2]
        atr3 = max(atr(history[-80:], 14), candle.close * 0.00045)
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        body = abs(candle.close - candle.open)

        long_sweep = candle.low < prior_low - 0.03 * atr3 and candle.close > prior_low
        short_sweep = candle.high > prior_high + 0.03 * atr3 and candle.close < prior_high
        long_shift = body >= 0.52 * atr3 and candle.close > max(prev.high, prior_high + 0.01 * atr3)
        short_shift = body >= 0.52 * atr3 and candle.close < min(prev.low, prior_low - 0.01 * atr3)

        for side in (Side.LONG.value, Side.SHORT.value):
            event = dict(events.get(side) or {})
            if event:
                invalidation = safe_float(event.get("invalidation_level"))
                trigger = safe_float(event.get("trigger_level"))
                invalidated = (
                    candle.close < invalidation - 0.08 * atr3
                    if side == Side.LONG.value
                    else candle.close > invalidation + 0.08 * atr3
                )
                opposite_acceptance = (
                    candle.close < trigger - 0.18 * atr3
                    if side == Side.LONG.value
                    else candle.close > trigger + 0.18 * atr3
                )
                if invalidated or (event.get("stage") in {"RETEST", "READY"} and opposite_acceptance):
                    events.pop(side, None)
                    event = {}

            sweep = long_sweep if side == Side.LONG.value else short_sweep
            shift = long_shift if side == Side.LONG.value else short_shift
            sweep_level = prior_low if side == Side.LONG.value else prior_high
            shift_level = prior_high if side == Side.LONG.value else prior_low
            invalidation = candle.low if side == Side.LONG.value else candle.high

            if sweep:
                stage = "STRUCTURE_SHIFT" if shift else "SWEEP"
                event = _new_scan3m_event(
                    side, candle,
                    shift_level if shift else sweep_level,
                    invalidation,
                    stage,
                    "SWEEP_AND_DISPLACEMENT" if shift else "LIQUIDITY_SWEEP",
                    shift,
                    sweep_level,
                )
                events[side] = event
            elif shift and not event:
                event = _new_scan3m_event(
                    side, candle, shift_level, invalidation,
                    "STRUCTURE_SHIFT", "DISPLACEMENT_BOS", True,
                )
                events[side] = event
            elif event:
                event["processed_bars"] = int(event.get("processed_bars", 0) or 0) + 1
                event["last_event_ts"] = int(candle.ts)
                event["event_price"] = round_price(candle.close)
                stage = str(event.get("stage") or "")
                trigger = safe_float(event.get("trigger_level"))

                if stage == "SWEEP" and shift:
                    event["stage"] = "STRUCTURE_SHIFT"
                    event["source"] = "SWEEP_THEN_DISPLACEMENT"
                    event["trigger_level"] = round_price(shift_level)
                    event["displacement"] = True
                    event["event_ts"] = int(candle.ts)
                    event["event_price"] = round_price(candle.close)
                    trigger = shift_level
                elif stage == "STRUCTURE_SHIFT" and int(candle.ts) > int(event.get("event_ts", 0) or 0):
                    touched = (
                        candle.low <= trigger + 0.20 * atr3 and candle.close > trigger
                        if side == Side.LONG.value
                        else candle.high >= trigger - 0.20 * atr3 and candle.close < trigger
                    )
                    if touched:
                        event["stage"] = "RETEST"
                        event["retest_ts"] = int(candle.ts)
                        event["hold_closes"] = 1
                elif stage in {"RETEST", "READY"}:
                    held = candle.close > trigger if side == Side.LONG.value else candle.close < trigger
                    if held:
                        event["hold_closes"] = int(event.get("hold_closes", 0) or 0) + 1
                        if int(event.get("hold_closes", 0) or 0) >= 2:
                            event["stage"] = "READY"
                            event["ready_ts"] = int(candle.ts)
                events[side] = event

    memory["events"] = events
    memory["last_run_processed"] = len(selected)
    memory["processed_count"] = int(memory.get("processed_count", 0) or 0) + len(selected)
    if selected:
        memory["last_scanned_3m_ts"] = int(selected[-1].ts)
    elif items:
        memory["last_scanned_3m_ts"] = max(last_ts, int(items[-1].ts))

    state["scan_3m"] = memory
    context["scan_3m"] = memory
    context["scan_3m_events"] = events
    return memory


def scan_execution_event_ts(event: dict[str, Any]) -> int:
    """Timestamp of the newest execution-relevant event, not the old impulse.

    A missed-impulse re-entry must be unlocked by a genuinely new retest/hold.
    Using the original displacement timestamp would incorrectly suppress that
    re-entry even though the market produced a fresh execution event.
    """
    if not isinstance(event, dict):
        return 0
    stage = str(event.get("stage") or "")
    if stage == "READY":
        return max(
            int(event.get("ready_ts", 0) or 0),
            int(event.get("retest_ts", 0) or 0),
            int(event.get("event_ts", 0) or 0),
        )
    if stage == "RETEST":
        return max(int(event.get("retest_ts", 0) or 0), int(event.get("event_ts", 0) or 0))
    return int(event.get("event_ts", 0) or 0)


def scan_event_for_side(context: dict[str, Any], side: str) -> dict[str, Any]:
    events = context.get("scan_3m_events") or (context.get("scan_3m") or {}).get("events") or {}
    event = events.get(side) if isinstance(events, dict) else None
    if not isinstance(event, dict) or not event.get("valid", True):
        return {}
    price = safe_float(context.get("price"))
    atr15 = max(safe_float((context.get("tf15") or {}).get("atr")), price * 0.001)
    anchor = safe_float(event.get("trigger_level") or event.get("event_price"))
    extension = abs(price - anchor) / atr15 if anchor and atr15 else 0.0
    result = dict(event)
    result["extension_atr15"] = round(extension, 3)
    result["within_live_window"] = extension <= SCAN_3M_MAX_EVENT_EXTENSION_ATR15
    return result


def apply_scan_event_to_candidate(context: dict[str, Any], candidate: Candidate) -> Candidate:
    event = scan_event_for_side(context, candidate.side)
    if not event or not event.get("within_live_window"):
        return candidate
    atr15 = max(safe_float(context["tf15"].get("atr")), safe_float(context.get("price")) * 0.001)
    event_level = safe_float(event.get("trigger_level"))
    candidate_level = safe_float(candidate.trigger_level or candidate.execution_anchor)
    proximity = abs(event_level - candidate_level) / atr15 if event_level and candidate_level else 0.0
    compatible = proximity <= 0.75 or candidate.setup_family in {
        SetupFamily.LIQUIDITY_RECOVERY.value,
        SetupFamily.STRUCTURAL_TRANSITION.value,
    }
    if not compatible:
        return candidate

    early_types = {
        SetupType.SWEEP_RECLAIM.value,
        SetupType.CAPITULATION_RECOVERY.value,
        SetupType.DIRECTION_FLIP.value,
        SetupType.TREND_IGNITION.value,
    }
    stage = str(event.get("stage") or "")
    early_ready = stage in {"RETEST", "READY"} or (
        candidate.setup_type in early_types
        and stage == "STRUCTURE_SHIFT"
        and bool(event.get("displacement"))
    )
    standard_ready = stage in {"RETEST", "READY"}
    ready = early_ready if candidate.setup_type in early_types else standard_ready
    if ready:
        candidate.trigger_ready = True
        candidate.trigger_ts = max(int(candidate.trigger_ts or 0), scan_execution_event_ts(event))
        candidate.trigger_level = round_price(candidate.trigger_level or event_level)
        candidate.execution_anchor = round_price(event.get("event_price") or candidate.execution_anchor or event_level)
        latest_ts = int(closed(context["candles"]["3m"])[-1].ts)
        candidate.trigger_age_minutes = max(0.0, (latest_ts - candidate.trigger_ts) / 60000.0)
        candidate.score_components["trigger"] = max(int(candidate.score_components.get("trigger", 0)), 13 if stage == "READY" else 11)
        candidate.scan_event_stage = stage
        note = f"повний 3M scan зберіг подію {stage} між 15-хвилинними запусками"
        if note not in candidate.confirmations:
            candidate.confirmations.append(note)
    return candidate


def classify_execution_lane(candidate: Candidate) -> str:
    early_types = {
        SetupType.SWEEP_RECLAIM.value,
        SetupType.CAPITULATION_RECOVERY.value,
        SetupType.DIRECTION_FLIP.value,
        SetupType.TREND_IGNITION.value,
    }
    if candidate.execution_lane in {"MISSED_IMPULSE_REENTRY", "HIGH_QUALITY_CONTINUATION"}:
        return candidate.execution_lane
    if candidate.setup_type in early_types and (
        candidate.risk_mode == "RISKY"
        or "EARLY" in candidate.variant
        or candidate.setup_type in {SetupType.CAPITULATION_RECOVERY.value, SetupType.TREND_IGNITION.value}
    ):
        return "EARLY_TACTICAL"
    return "STANDARD_CONFIRMED"


def update_event_chain_quality(event: dict, new_stage: str, candle: Candle, atr: float) -> None:
    """
    Покращує якість ланцюжка подій (event-chain).
    Кожна нова стадія (особливо ACCEPTANCE і RETEST) підвищує confirmation_quality.
    """
    prev_stage = event.get("stage", "")
    event["previous_stage"] = prev_stage

    quality = int(event.get("confirmation_quality", 40))

    # Покращення якості при переході по ланцюжку
    if new_stage == "STRUCTURE_SHIFT" and event.get("displacement"):
        quality = max(quality, 55)
    if new_stage == "ACCEPTANCE":
        quality = max(quality, 70)
        event["acceptance_quality"] = 75
    if new_stage == "RETEST":
        quality = max(quality, 82)
        event["retest_quality"] = 80
    if new_stage == "READY":
        quality = max(quality, 90)

    # Бонус за тривале утримання (hold)
    hold = int(event.get("hold_closes", 0))
    if hold >= 2:
        quality = min(100, quality + 5)

    event["confirmation_quality"] = min(100, quality)
    event["chain_quality"] = min(100, quality + (5 if event.get("displacement") else 0))


def calculate_confirmation_tier(context: dict[str, Any], candidate: Candidate, htf_result: dict) -> int:
    """
    Confirmation Tiers system (Етап 1 + 3).
    Determines how strongly the setup is confirmed on 3M microstructure + HTF confluence.
    Higher tier = higher confidence, better position sizing, slightly more aggressive execution.
    """
    tier = TIER_MICRO

    evidence_count = len(getattr(candidate, 'evidence_families', []))
    has_strong_3m_event = False
    has_good_acceptance = False

    # Check 3M scan event quality
    scan_events = context.get("scan_3m_events") or {}
    event = scan_events.get(candidate.side) or {}
    if event:
        stage = str(event.get("stage", ""))
        displacement = bool(event.get("displacement"))
        has_strong_3m_event = displacement or stage in {"READY", "RETEST"}
        has_good_acceptance = stage in {"ACCEPTANCE", "RETEST", "READY"}

    htf_aligned = htf_result.get("htf_aligned", False)
    score = getattr(candidate, 'final_score', 0) or getattr(candidate, 'raw_score', 0)

    # Tier assignment logic
    if evidence_count >= 5 and htf_aligned and has_strong_3m_event and score >= 78:
        tier = TIER_PREMIUM
    elif evidence_count >= 5 and htf_aligned and has_strong_3m_event:
        tier = TIER_HIGH_QUALITY
    elif evidence_count >= 4 and (htf_aligned or has_good_acceptance) and score >= 65:
        tier = TIER_STANDARD
    else:
        tier = TIER_MICRO

    return tier


def evaluate_htf_confluence(context: dict[str, Any], candidate: Candidate, regime: str) -> dict[str, Any]:
    """
    Compromise HTF Confluence Gate (regime-aware).
    - In TRANSITION: strict for CONTINUATION/EXPANSION (hard penalty or block)
    - In TREND / aligned SHOCK: light bonus only
    - Does NOT add extra waiting time — only affects score and lane assignment.
    Goal: improve quality without making good entries too late.
    """
    side = candidate.side
    htf1 = str((context.get("tf1h") or {}).get("bias", "")).upper()
    htf4 = str((context.get("tf4h") or {}).get("bias", "")).upper()

    htf_aligned = (htf1 == side) or (htf4 == side)
    is_continuation_family = candidate.setup_family in {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}
    is_transition = regime.upper() == "TRANSITION"

    bonus = 0
    hard_block = False
    reason = ""

    if is_continuation_family:
        if htf_aligned:
            bonus = HIGH_QUALITY_HTF_BONUS
        else:
            if is_transition:
                bonus = TRANSITION_HTF_PENALTY
                # In Transition we penalize heavily but do not always hard-block
                # so that very strong setups (high raw_score) can still pass.
                if candidate.raw_score < 68:
                    hard_block = True
                    reason = "немає confluence з 1H/4H у Transition режимі"
            else:
                bonus = -8  # light penalty outside Transition

    return {
        "htf_aligned": htf_aligned,
        "bonus": bonus,
        "hard_block": hard_block,
        "reason": reason,
        "regime": regime,
    }


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
    ordered = sorted(trades, key=lambda row: int(row.get("ts") or 0))
    buy = sum(safe_float(t.get("size")) for t in ordered if str(t.get("side")) == "buy")
    sell = sum(safe_float(t.get("size")) for t in ordered if str(t.get("side")) == "sell")
    total = buy + sell
    delta = buy - sell
    delta_pct = delta / total * 100 if total else 0.0
    bid = sum(size for _, size in book.get("bids", [])[:20])
    ask = sum(size for _, size in book.get("asks", [])[:20])
    book_total = bid + ask
    book_delta = (bid - ask) / book_total * 100 if book_total else 0.0

    first_price = safe_float(ordered[0].get("price")) if ordered else 0.0
    last_price = safe_float(ordered[-1].get("price")) if ordered else 0.0
    price_move_pct = pct(last_price, first_price) if first_price and last_price else 0.0
    mid = max(1, len(ordered) // 2)
    first_delta = sum(
        safe_float(t.get("size")) * (1 if str(t.get("side")) == "buy" else -1)
        for t in ordered[:mid]
    )
    second_delta = sum(
        safe_float(t.get("size")) * (1 if str(t.get("side")) == "buy" else -1)
        for t in ordered[mid:]
    )
    cvd_acceleration = second_delta - first_delta

    # Absorption is deliberately separate from raw aggressive delta. Aggressive
    # sells with rising price imply passive buyers absorbed supply; the reverse
    # implies passive sellers absorbed demand.
    absorption_bias = Side.NEUTRAL.value
    if price_move_pct >= 0.08 and delta_pct <= -12:
        absorption_bias = Side.LONG.value
    elif price_move_pct <= -0.08 and delta_pct >= 12:
        absorption_bias = Side.SHORT.value

    combined = 0.65 * delta_pct + 0.35 * book_delta
    bias = Side.LONG.value if combined >= 18 else Side.SHORT.value if combined <= -18 else Side.NEUTRAL.value
    confidence = "HIGH" if len(ordered) >= 120 and abs(combined) >= 35 else "MEDIUM" if len(ordered) >= 60 else "LOW"
    return {
        "bias": bias,
        "score": int(round(clamp(combined / 4.0, -10, 10))),
        "trade_delta": round(delta, 3),
        "trade_delta_pct": round(delta_pct, 2),
        "book_delta_pct": round(book_delta, 2),
        "price_move_pct": round(price_move_pct, 3),
        "cvd_acceleration": round(cvd_acceleration, 3),
        "absorption_bias": absorption_bias,
        "confidence": confidence,
        "sample_size": len(ordered),
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
    for key in ("s3", "s15", "s1h", "s4h"):
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


def build_context(data: dict[str, Any]) -> dict[str, Any]:
    candles = data.get("candles") or {}
    c3 = closed(candles.get("3m", []))
    c15 = closed(candles.get("15m", []))
    c1h = closed(candles.get("1h", []))
    c4h = closed(candles.get("4h", []))
    if min(len(c3), len(c15), len(c1h)) < 20:
        raise RuntimeError("Недостатньо закритих свічок для чистої ICT-моделі")
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price")) or c3[-1].close
    tf3 = timeframe_snapshot(c3, "3m")
    tf15 = timeframe_snapshot(c15, "15m")
    tf1h = timeframe_snapshot(c1h, "1h")
    tf4h = timeframe_snapshot(c4h, "4h")
    s3 = tf3.get("structure") or structure_snapshot(c3, "3m")
    s15 = tf15.get("structure") or structure_snapshot(c15, "15m")
    s1h = tf1h.get("structure") or structure_snapshot(c1h, "1h")
    s4h = tf4h.get("structure") or structure_snapshot(c4h, "4h")
    zones = (
        detect_fvgs(c3, "3m")
        + detect_order_blocks(c3, "3m")
        + detect_fvgs(c15, "15m")
        + detect_order_blocks(c15, "15m")
        + detect_fvgs(c1h, "1h", max_age=48)
        + detect_order_blocks(c1h, "1h", max_age=42)
        + detect_fvgs(c4h, "4h", max_age=36)
        + detect_order_blocks(c4h, "4h", max_age=30)
    )
    context: dict[str, Any] = {
        "time": iso_now(),
        "price": round_price(price),
        "price_source": ticker.get("source", "OKX"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "reference_price": round_price((data.get("reference") or {}).get("price")),
        "cross_price_diff_pct": safe_float(data.get("cross_price_diff_pct")),
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "s3": s3,
        "s15": s15,
        "s1h": s1h,
        "s4h": s4h,
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
# PROFESSIONAL ICT OPPORTUNITY ENGINE
# ==========================================================
# The old bot had many good detectors, but each detector later acquired its
# own override/gate chain. Here detectors only create candidates. Candidates
# are grouped into five semantic families and one deterministic selector makes
# the final decision.


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
    raw_bias = flow.get("bias")
    absorption = flow.get("absorption_bias")
    confidence = str(flow.get("confidence") or "LOW")

    if raw_bias == side and absorption != opposite(side):
        confirmations.append("агресивний flow і стакан підтримують напрям")
        return (10 if confidence != "LOW" else 8), confirmations, risks
    if absorption == side:
        confirmations.append("CVD/ціна показують поглинання потоку на користь напрямку")
        if raw_bias == opposite(side):
            risks.append("агресивний delta проти, але його поглинає ціна")
            return 7, confirmations, risks
        return 9, confirmations, risks
    if raw_bias == Side.NEUTRAL.value and absorption == Side.NEUTRAL.value:
        return 5, confirmations, risks
    if absorption == opposite(side):
        risks.append("CVD/ціна показують поглинання на користь протилежного напрямку")
    else:
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


def directional_candle(candle: Candle, side: str) -> bool:
    return candle.close > candle.open if side == Side.LONG.value else candle.close < candle.open


def candidate_evidence_families(context: dict[str, Any], candidate: Candidate) -> list[str]:
    components = candidate.score_components
    evidence: list[str] = []
    if int(components.get("location", 0)) >= 10:
        evidence.append("ICT_LOCATION")
    if int(components.get("structure", 0)) >= 12:
        evidence.append("PRICE_STRUCTURE")
    if int(components.get("liquidity", 0)) >= 10:
        evidence.append("LIQUIDITY")
    if candidate.trigger_ready and int(components.get("trigger", 0)) >= 8:
        evidence.append("EXECUTION_TRIGGER")
    if (
        context["flow"].get("bias") == candidate.side
        or context["flow"].get("absorption_bias") == candidate.side
    ) and int(components.get("flow", 0)) >= 8:
        evidence.append("ORDER_FLOW")
    if context["tf15"].get("bias") == candidate.side or context["tf1h"].get("bias") == candidate.side:
        evidence.append("HTF_CONTEXT")
    return list(dict.fromkeys(evidence))


def candidate_adverse_families(context: dict[str, Any], candidate: Candidate) -> list[str]:
    side = candidate.side
    adverse: list[str] = []
    if (
        context["flow"].get("bias") == opposite(side)
        or context["flow"].get("absorption_bias") == opposite(side)
    ):
        adverse.append("ORDER_FLOW")
    funding = safe_float((context.get("derivatives") or {}).get("funding_rate"))
    if (side == Side.LONG.value and funding >= 0.0008) or (side == Side.SHORT.value and funding <= -0.0008):
        adverse.append("DERIVATIVES_CROWDING")
    if context["tf3"].get("bias") == opposite(side) and abs(int(context["tf3"].get("score", 0) or 0)) >= 28:
        adverse.append("FAST_STRUCTURE")
    if context["tf15"].get("bias") == opposite(side) and context["tf1h"].get("bias") == opposite(side):
        adverse.append("HTF_CONTEXT")
    if safe_float(context.get("cross_price_diff_pct")) > 0.35:
        adverse.append("PRICE_SOURCE")
    return adverse


def micro_compression_snapshot(candles_3m: list[Candle]) -> dict[str, Any]:
    items = closed(candles_3m)
    if len(items) < 12:
        return {"active": False, "side": Side.NEUTRAL.value}
    atr3 = max(atr(items, 14), items[-1].close * 0.0005)
    base = items[-10:-2]
    confirm = items[-2:]
    high = max(c.high for c in base)
    low = min(c.low for c in base)
    width_atr = (high - low) / atr3
    ranges = [max(c.high - c.low, 1e-9) for c in base]
    contracting = mean(ranges[-3:]) <= mean(ranges[:3]) * 0.88
    overlap = sum(1 for a, b in zip(base, base[1:]) if min(a.high, b.high) > max(a.low, b.low)) / max(len(base) - 1, 1)
    compressed = bool(width_atr <= 4.20 and overlap >= 0.55 and (contracting or width_atr <= 1.80))
    long_break = compressed and confirm[-1].close > high + 0.08 * atr3 and sum(c.close > high for c in confirm) >= 1
    short_break = compressed and confirm[-1].close < low - 0.08 * atr3 and sum(c.close < low for c in confirm) >= 1
    side = Side.LONG.value if long_break else Side.SHORT.value if short_break else Side.NEUTRAL.value
    boundary = high if side == Side.LONG.value else low if side == Side.SHORT.value else 0.0
    invalidation = low if side == Side.LONG.value else high if side == Side.SHORT.value else 0.0
    return {
        "active": compressed,
        "accepted": side != Side.NEUTRAL.value,
        "side": side,
        "boundary": round_price(boundary),
        "invalidation": round_price(invalidation),
        "high": round_price(high),
        "low": round_price(low),
        "width_atr3": round(width_atr, 3),
        "contracting": contracting,
        "overlap": round(overlap, 3),
        "trigger_ts": int(confirm[-1].ts),
    }


def direction_flip_snapshot(context: dict[str, Any], side: str) -> dict[str, Any]:
    candles = closed(context["candles"]["15m"])
    if len(candles) < 10:
        return {"confirmed": False, "tier": "NONE"}
    w = candles[-10:]
    baseline = w[:-3]
    recent = w[-3:]
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    prior_high = max(c.high for c in baseline)
    prior_low = min(c.low for c in baseline)
    if side == Side.LONG.value:
        level = prior_high
        break_closes = sum(c.close > level + 0.02 * atr15 for c in recent)
        directional = sum(directional_candle(c, side) for c in recent)
        displacement_atr = max(0.0, (recent[-1].close - w[-4].close) / atr15)
        retest = any(c.low <= level + 0.35 * atr15 and c.close > level for c in recent[1:])
        pivot = recent[-1].low > min(recent[-2].low, recent[-3].low) and recent[-1].close > recent[-2].close
        invalidation = min(c.low for c in recent)
    else:
        level = prior_low
        break_closes = sum(c.close < level - 0.02 * atr15 for c in recent)
        directional = sum(directional_candle(c, side) for c in recent)
        displacement_atr = max(0.0, (w[-4].close - recent[-1].close) / atr15)
        retest = any(c.high >= level - 0.35 * atr15 and c.close < level for c in recent[1:])
        pivot = recent[-1].high < max(recent[-2].high, recent[-3].high) and recent[-1].close < recent[-2].close
        invalidation = max(c.high for c in recent)
    s15 = context["s15"]
    structure_break = s15.get("bos") == side or s15.get("choch") == side or s15.get("bias") == side
    trigger = trigger_snapshot(context["candles"]["3m"], side, level)
    extension = abs(context["price"] - safe_float(context["tf15"].get("ema20"))) / atr15
    early = bool(
        displacement_atr >= EARLY_DIRECTION_FLIP_MIN_ATR
        and break_closes >= 1
        and directional >= 2
        and structure_break
        and (retest or pivot)
        and (trigger.get("ready") or context["s3"].get("bias") == side)
        and extension <= 2.70
    )
    full = bool(
        displacement_atr >= FULL_DIRECTION_FLIP_MIN_ATR
        and break_closes >= 2
        and directional >= 2
        and structure_break
        and (retest or pivot)
        and trigger.get("ready")
        and extension <= 2.40
    )
    return {
        "confirmed": bool(early or full),
        "tier": "FULL" if full else "EARLY" if early else "NONE",
        "level": round_price(level),
        "invalidation": round_price(invalidation),
        "break_closes": break_closes,
        "directional_closes": directional,
        "displacement_atr": round(displacement_atr, 3),
        "retest": retest,
        "pivot": pivot,
        "trigger": trigger,
        "extension_atr": round(extension, 3),
        "trigger_ts": int(recent[-1].ts),
    }


def capitulation_snapshot(context: dict[str, Any], side: str) -> dict[str, Any]:
    candles = closed(context["candles"]["15m"])
    if len(candles) < 12:
        return {"active": False}
    w = candles[-14:]
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    if side == Side.LONG.value:
        start_idx = max(range(len(w) - 2), key=lambda i: w[i].high)
        extreme_idx = min(range(start_idx + 1, len(w)), key=lambda i: w[i].low) if start_idx < len(w) - 1 else start_idx
        run = w[start_idx].high - w[extreme_idx].low
        extreme = w[extreme_idx]
        after = w[extreme_idx + 1:]
        reclaim = bool(len(after) >= 2 and after[-1].close > extreme.close + 0.35 * atr15 and after[-1].close > after[-2].high)
        pivot = bool(len(after) >= 3 and after[-1].low >= after[-2].low and after[-1].low > min(c.low for c in after[:-1]) + 0.05 * atr15)
        base = after[-4:-1] if len(after) >= 4 else after[:-1]
        invalidation = min((c.low for c in base), default=extreme.low)
        reclaim_level = max((c.high for c in after[:-1]), default=extreme.close)
    else:
        start_idx = min(range(len(w) - 2), key=lambda i: w[i].low)
        extreme_idx = max(range(start_idx + 1, len(w)), key=lambda i: w[i].high) if start_idx < len(w) - 1 else start_idx
        run = w[extreme_idx].high - w[start_idx].low
        extreme = w[extreme_idx]
        after = w[extreme_idx + 1:]
        reclaim = bool(len(after) >= 2 and after[-1].close < extreme.close - 0.35 * atr15 and after[-1].close < after[-2].low)
        pivot = bool(len(after) >= 3 and after[-1].high <= after[-2].high and after[-1].high < max(c.high for c in after[:-1]) - 0.05 * atr15)
        base = after[-4:-1] if len(after) >= 4 else after[:-1]
        invalidation = max((c.high for c in base), default=extreme.high)
        reclaim_level = min((c.low for c in after[:-1]), default=extreme.close)
    run_atr = run / atr15
    run_pct = run / max(abs(w[0].close), 1e-9) * 100.0
    run_segment = w[start_idx:extreme_idx + 1]
    expected_run_side = Side.SHORT.value if side == Side.LONG.value else Side.LONG.value
    directional_ratio = (
        sum(directional_candle(c, expected_run_side) for c in run_segment) / max(len(run_segment), 1)
    )
    run_efficiency = abs(run_segment[-1].close - run_segment[0].open) / max(
        sum(abs(c.close - c.open) for c in run_segment), 1e-9
    )
    bars_after_extreme = len(after)
    recent_extreme = extreme_idx >= len(w) - CAPITULATION_MAX_AGE_15M
    active = bool(
        recent_extreme
        and 2 <= len(run_segment) <= 9
        and 2 <= bars_after_extreme <= 5
        and directional_ratio >= 0.54
        and run_efficiency >= 0.58
        and (run_atr >= CAPITULATION_MIN_RUN_ATR or run_pct >= CAPITULATION_MIN_RUN_PCT)
        and (reclaim or pivot)
    )
    trigger = trigger_snapshot(context["candles"]["3m"], side, reclaim_level) if active else {"ready": False, "score": 0}
    return {
        "active": active,
        "reclaim": reclaim,
        "pivot": pivot,
        "run_atr": round(run_atr, 3),
        "run_pct": round(run_pct, 3),
        "directional_ratio": round(directional_ratio, 3),
        "run_efficiency": round(run_efficiency, 3),
        "bars_after_extreme": bars_after_extreme,
        "extreme": round_price(extreme.low if side == Side.LONG.value else extreme.high),
        "invalidation": round_price(invalidation),
        "reclaim_level": round_price(reclaim_level),
        "trigger": trigger,
        "trigger_ts": int((closed(context["candles"]["3m"]) or [extreme])[-1].ts),
    }


def fresh_base_snapshot(context: dict[str, Any], side: str) -> dict[str, Any]:
    candles = closed(context["candles"]["15m"])
    if len(candles) < 12:
        return {"active": False}
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    best: Optional[dict[str, Any]] = None
    for confirm_count in (2, 1):
        for base_len in range(FRESH_BASE_MIN_BARS, FRESH_BASE_MAX_BARS + 1):
            base_end = len(candles) - confirm_count
            base_start = base_end - base_len
            if base_start < 3:
                continue
            base = candles[base_start:base_end]
            confirm = candles[base_end:]
            impulse = candles[max(0, base_start - 4):base_start]
            if len(impulse) < 2 or not confirm:
                continue
            base_high = max(c.high for c in base)
            base_low = min(c.low for c in base)
            width_atr = (base_high - base_low) / atr15
            impulse_move = (impulse[-1].close - impulse[0].open) if side == Side.LONG.value else (impulse[0].open - impulse[-1].close)
            impulse_atr = impulse_move / atr15
            counter_candle = any(not directional_candle(c, side) for c in base)
            ranges = [max(c.high - c.low, 1e-9) for c in base]
            compacting = len(ranges) < 3 or mean(ranges[-2:]) <= mean(ranges[:2]) * 1.05
            if side == Side.LONG.value:
                retrace = max(0.0, impulse[-1].high - base_low) / atr15
                accepted = all(c.close > base_high + 0.03 * atr15 for c in confirm)
                boundary = base_high
                invalidation = base_low
            else:
                retrace = max(0.0, base_high - impulse[-1].low) / atr15
                accepted = all(c.close < base_low - 0.03 * atr15 for c in confirm)
                boundary = base_low
                invalidation = base_high
            trigger = trigger_snapshot(context["candles"]["3m"], side, boundary)
            meaningful_retrace = counter_candle or retrace >= 0.22
            fast = confirm_count == 1 or base_len == 3
            allowed_shape = bool(
                impulse_atr >= 0.80
                and width_atr <= (FRESH_BASE_MAX_WIDTH_ATR if fast else FRESH_BASE_MAX_WIDTH_ATR * 1.35)
                and compacting
                and meaningful_retrace
                and accepted
            )
            score = impulse_atr * 8 + (12 if accepted else 0) + (5 if trigger.get("ready") else 0) - width_atr * 3
            snap = {
                "active": bool(impulse_atr >= 0.65 and meaningful_retrace),
                "allowed_shape": allowed_shape,
                "fast": fast,
                "base_len": base_len,
                "confirm_count": confirm_count,
                "base_high": round_price(base_high),
                "base_low": round_price(base_low),
                "boundary": round_price(boundary),
                "invalidation": round_price(invalidation),
                "width_atr": round(width_atr, 3),
                "impulse_atr": round(impulse_atr, 3),
                "retrace_atr": round(retrace, 3),
                "compacting": compacting,
                "accepted": accepted,
                "trigger": trigger,
                "trigger_ts": int(confirm[-1].ts),
                "rank": score,
            }
            if allowed_shape and (best is None or score > safe_float(best.get("rank"))):
                best = snap
    return best or {"active": False}


def trend_ignition_snapshot(context: dict[str, Any], side: str) -> dict[str, Any]:
    candles = closed(context["candles"]["15m"])
    if len(candles) < 9:
        return {"confirmed": False}
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    compression = candles[-7:-3]
    launch = candles[-3:]
    high = max(c.high for c in compression)
    low = min(c.low for c in compression)
    width_atr = (high - low) / atr15
    directional = sum(directional_candle(c, side) for c in launch)
    if side == Side.LONG.value:
        accepted = launch[-1].close > high + 0.05 * atr15 and sum(c.close > high for c in launch) >= 1
        displacement = max(0.0, (launch[-1].close - (high + low) / 2.0) / atr15)
        invalidation = low
        boundary = high
    else:
        accepted = launch[-1].close < low - 0.05 * atr15 and sum(c.close < low for c in launch) >= 1
        displacement = max(0.0, ((high + low) / 2.0 - launch[-1].close) / atr15)
        invalidation = high
        boundary = low
    structure_same = context["s15"].get("bos") == side or context["s15"].get("choch") == side or context["s15"].get("bias") == side
    participation = context["flow"].get("bias") == side or safe_float(context["tf15"].get("volume_ratio")) >= 1.08
    trigger = trigger_snapshot(context["candles"]["3m"], side, boundary)
    extension = abs(context["price"] - safe_float(context["tf15"].get("ema20"))) / atr15
    confirmed = bool(
        width_atr <= 1.90
        and directional >= 2
        and accepted
        and displacement >= 0.90
        and structure_same
        and participation
        and trigger.get("ready")
        and extension <= 2.60
    )
    return {
        "confirmed": confirmed,
        "width_atr": round(width_atr, 3),
        "directional": directional,
        "displacement_atr": round(displacement, 3),
        "boundary": round_price(boundary),
        "invalidation": round_price(invalidation),
        "trigger": trigger,
        "trigger_ts": int(launch[-1].ts),
    }


def candidate_sweep_reclaim(context: dict[str, Any], side: str) -> Optional[Candidate]:
    sweep3 = context["sweep3"]
    sweep15 = context["sweep15"]
    sweep = sweep3 if sweep3.get("side") == side else sweep15 if sweep15.get("side") == side else None
    if not sweep:
        return None
    zone = relevant_zone(context, side, 0.80)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 10)
    liquidity = 20 if sweep15.get("side") == side else 18
    confirmations.append("зовнішню ліквідність знято і рівень повернуто")
    s3 = context["s3"]
    s15 = context["s15"]
    structure = 0
    if s3.get("choch") == side or s3.get("bos") == side:
        structure += 14
        confirmations.append("3M BOS/CHOCH підтвердив reclaim")
    elif s3.get("bias") == side:
        structure += 9
    confirmed_15m = s15.get("choch") == side or s15.get("bos") == side
    if confirmed_15m:
        structure += 11
        confirmations.append("15M структура прийняла новий напрям")
    elif context["tf15"].get("bias") != opposite(side):
        structure += 5
    structure = min(25, structure)
    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(sweep.get("level")))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    invalidation = safe_float(sweep.get("extreme"))
    if zone:
        invalidation = min(invalidation, zone.low) if side == Side.LONG.value else max(invalidation, zone.high)
    dr = context.get("dealing_range") or {}
    prime_early_location = bool(
        confirmed_15m
        or sweep15.get("side") == side
        or (zone is not None and timeframe_rank(zone.timeframe) >= 3)
        or (side == Side.LONG.value and dr.get("at_lower_edge"))
        or (side == Side.SHORT.value and dr.get("at_upper_edge"))
    )
    if not confirmed_15m and not prime_early_location:
        confirmations.append("ранній reclaim збережено як ARMED: потрібна 1H/4H POI, 15M sweep або край dealing range")
    candidate = Candidate(
        side=side,
        setup_type=SetupType.SWEEP_RECLAIM.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": structure,
            "liquidity": liquidity,
            "trigger": int(trigger.get("score", 0)),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(trigger.get("ready") and prime_early_location),
        trigger_level=safe_float(sweep.get("level")) or context["price"],
        invalidation_level=round_price(invalidation),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if confirmed_15m and context["tf1h"].get("bias") != opposite(side) else "RISKY",
        setup_family=SetupFamily.LIQUIDITY_RECOVERY.value,
        variant="CONFIRMED_REVERSAL" if confirmed_15m else "EARLY_RECLAIM",
        trigger_ts=int(sweep.get("ts", 0) or 0),
        execution_profile="RECOVERY" if confirmed_15m else "TACTICAL",
        specificity=32 if confirmed_15m else 24,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_capitulation_recovery(context: dict[str, Any], side: str) -> Optional[Candidate]:
    snap = capitulation_snapshot(context, side)
    if not snap.get("active"):
        return None
    zone = relevant_zone(context, side, 1.00)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 18)
    confirmations.append(f"капітуляційний рух {snap.get('run_atr')} ATR сформував новий extreme")
    structure = 8
    if snap.get("reclaim"):
        structure += 8
        confirmations.append("15M закрив reclaim після extreme")
    if snap.get("pivot"):
        structure += 6
        confirmations.append("сформовано новий HL/LH")
    if context["s15"].get("choch") == side or context["s15"].get("bos") == side:
        structure += 5
    structure = min(25, structure)
    trigger = snap.get("trigger") or {}
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    candidate = Candidate(
        side=side,
        setup_type=SetupType.CAPITULATION_RECOVERY.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": structure,
            "liquidity": 20,
            "trigger": int(trigger.get("score", 0)),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(snap.get("reclaim") and snap.get("pivot") and trigger.get("ready")),
        trigger_level=safe_float(snap.get("reclaim_level")) or context["price"],
        invalidation_level=safe_float(snap.get("invalidation")),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="RISKY",
        setup_family=SetupFamily.LIQUIDITY_RECOVERY.value,
        variant="CONFIRMED_BASE_RECOVERY" if snap.get("reclaim") and snap.get("pivot") else "RECOVERY_FORMING",
        trigger_ts=int(snap.get("trigger_ts", 0) or 0),
        execution_profile="RECOVERY",
        specificity=42,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_direction_flip(context: dict[str, Any], side: str) -> Optional[Candidate]:
    snap = direction_flip_snapshot(context, side)
    if not snap.get("confirmed"):
        return None
    zone = relevant_zone(context, side, 0.90)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 9)
    tier = str(snap.get("tier"))
    confirmations.extend([
        f"15M displacement {snap.get('displacement_atr')} ATR",
        f"закритих break-close: {snap.get('break_closes')}",
        "ретест або новий HL/LH підтвердив зміну напрямку",
    ])
    structure = 25 if tier == "FULL" else 21
    liquidity = 17 if tier == "FULL" else 14
    trigger = snap.get("trigger") or {}
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    candidate = Candidate(
        side=side,
        setup_type=SetupType.DIRECTION_FLIP.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": structure,
            "liquidity": liquidity,
            "trigger": max(int(trigger.get("score", 0)), 11 if tier == "EARLY" else 13),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(trigger.get("ready") or (tier == "EARLY" and context["s3"].get("bias") == side)),
        trigger_level=safe_float(snap.get("level")) or context["price"],
        invalidation_level=safe_float(snap.get("invalidation")),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if tier == "FULL" and context["flow"].get("bias") != opposite(side) else "RISKY",
        setup_family=SetupFamily.STRUCTURAL_TRANSITION.value,
        variant=f"{tier}_15M_FLIP",
        trigger_ts=int(snap.get("trigger_ts", 0) or 0),
        execution_profile="TRANSITION",
        specificity=38 if tier == "FULL" else 31,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_trend_ignition(context: dict[str, Any], side: str) -> Optional[Candidate]:
    snap = trend_ignition_snapshot(context, side)
    if not snap.get("confirmed"):
        return None
    zone = relevant_zone(context, side, 0.75)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 10)
    confirmations.extend([
        "15M стиснення завершилось прийнятим displacement",
        f"перший імпульс пройшов {snap.get('displacement_atr')} ATR",
        "3M тригер підтвердив запуск нового тренду",
    ])
    trigger = snap.get("trigger") or {}
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    candidate = Candidate(
        side=side,
        setup_type=SetupType.TREND_IGNITION.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": 23,
            "liquidity": 15,
            "trigger": max(13, int(trigger.get("score", 0))),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=True,
        trigger_level=safe_float(snap.get("boundary")) or context["price"],
        invalidation_level=safe_float(snap.get("invalidation")),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if context["tf1h"].get("bias") == side else "RISKY",
        setup_family=SetupFamily.STRUCTURAL_TRANSITION.value,
        variant="COMPRESSION_LAUNCH",
        trigger_ts=int(snap.get("trigger_ts", 0) or 0),
        execution_profile="TRANSITION",
        specificity=27,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_pullback_continuation(context: dict[str, Any], side: str) -> Optional[Candidate]:
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    if tf15.get("bias") != side and tf1h.get("bias") != side:
        return None
    if context["regime"] == Regime.RANGE.value:
        return None
    zone = relevant_zone(context, side, 0.75)
    dr = context["dealing_range"]
    correct_half = (side == Side.LONG.value and dr.get("position", 0.5) <= 0.60) or (
        side == Side.SHORT.value and dr.get("position", 0.5) >= 0.40
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
    counter_sweep = context["sweep3"].get("side") == side
    liquidity = 10 + (6 if counter_sweep else 0) + (4 if zone else 0)
    if counter_sweep:
        confirmations.append("відкат забрав внутрішню ліквідність")
    reference = (zone.low + zone.high) / 2.0 if zone else safe_float(
        context["s15"].get("swing_low" if side == Side.LONG.value else "swing_high")
    )
    trigger = trigger_snapshot(context["candles"]["3m"], side, reference)
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    full = tf15.get("bias") == side and tf1h.get("bias") == side and trigger.get("ready")
    fast_poi = bool(
        not full
        and trigger.get("ready")
        and counter_sweep
        and zone is not None
        and timeframe_rank(zone.timeframe) >= 2
        and context["s3"].get("bias") == side
        and (context["flow"].get("bias") in {side, Side.NEUTRAL.value} or context["flow"].get("absorption_bias") == side)
    )
    if not full and not fast_poi:
        confirmations.append("ранній відкат збережено як ARMED: потрібен sweep у 15M/1H POI + 3M shift, без входу навмання")
    invalidation = zone.low if zone and side == Side.LONG.value else zone.high if zone else safe_float(
        context["s15"].get("swing_low" if side == Side.LONG.value else "swing_high")
    )
    candidate = Candidate(
        side=side,
        setup_type=SetupType.PULLBACK_CONTINUATION.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": structure,
            "liquidity": min(20, liquidity),
            "trigger": int(trigger.get("score", 0)),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(trigger.get("ready") and (full or fast_poi)),
        trigger_level=round_price(reference) or context["price"],
        invalidation_level=round_price(invalidation),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if full else "RISKY",
        setup_family=SetupFamily.CONTINUATION.value,
        variant="FULL_PULLBACK" if full else "EARLY_POI_SWEEP_RECLAIM" if fast_poi else "PULLBACK_FORMING",
        trigger_ts=int((closed(context["candles"]["3m"]) or [Candle(0,0,0,0,0)])[-1].ts),
        execution_profile="STANDARD" if full else "TACTICAL",
        specificity=25 if full else 19,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_fresh_base_continuation(context: dict[str, Any], side: str) -> Optional[Candidate]:
    snap = fresh_base_snapshot(context, side)
    if not snap.get("allowed_shape"):
        return None
    trend_context = context["tf15"].get("bias") == side and context["tf1h"].get("bias") in {side, Side.NEUTRAL.value}
    if not trend_context:
        return None
    zone = relevant_zone(context, side, 0.85)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 11)
    confirmations.extend([
        f"нова компактна 15M база: {snap.get('base_len')} свічок",
        f"ширина бази {snap.get('width_atr')} ATR після імпульсу {snap.get('impulse_atr')} ATR",
        "breakout бази прийнято закритою 15M свічкою",
    ])
    trigger = snap.get("trigger") or {}
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    fast = bool(snap.get("fast"))
    candidate = Candidate(
        side=side,
        setup_type=SetupType.FRESH_BASE_CONTINUATION.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": 24 if not fast else 20,
            "liquidity": 16,
            "trigger": max(int(trigger.get("score", 0)), 11 if fast else 13),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(trigger.get("ready")),
        trigger_level=safe_float(snap.get("boundary")) or context["price"],
        invalidation_level=safe_float(snap.get("invalidation")),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="RISKY" if fast else "NORMAL",
        setup_family=SetupFamily.CONTINUATION.value,
        variant="COMPACT_3M_CONFIRMED_BASE" if fast else "FULL_15M_BASE",
        trigger_ts=int(snap.get("trigger_ts", 0) or 0),
        execution_profile="BASE_REENTRY",
        specificity=36 if not fast else 31,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_breakout_retest(context: dict[str, Any], side: str) -> Optional[Candidate]:
    retest = breakout_retest_snapshot(context["candles"]["15m"], context["candles"]["3m"], side)
    level = safe_float(retest.get("level"))
    if not level:
        return None
    c3 = closed(context["candles"]["3m"])
    recent = c3[-8:]
    atr3 = max(atr(c3, 14), context["price"] * 0.0005)
    if side == Side.LONG.value:
        accepted_back = len(recent) >= 2 and all(c.close < level for c in recent[-2:])
        touch_indices = [i for i, c in enumerate(recent) if c.low <= level + atr3 * 0.25]
        touch_idx = touch_indices[-1] if touch_indices else max(0, len(recent) - 3)
        local_extreme = min((c.low for c in recent[touch_idx:]), default=level)
    else:
        accepted_back = len(recent) >= 2 and all(c.close > level for c in recent[-2:])
        touch_indices = [i for i, c in enumerate(recent) if c.high >= level - atr3 * 0.25]
        touch_idx = touch_indices[-1] if touch_indices else max(0, len(recent) - 3)
        local_extreme = max((c.high for c in recent[touch_idx:]), default=level)
    if accepted_back:
        return None
    zone = relevant_zone(context, side, 0.60)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 10)
    confirmations.append("15M закрив BOS за напрямком")
    if retest.get("confirmed"):
        confirmations.append("3M повернувся до BOS-рівня і втримав його")
    trigger = trigger_snapshot(context["candles"]["3m"], side, level)
    structure = 24 if retest.get("confirmed") else 17
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    atr15 = safe_float(context["tf15"].get("atr"))
    invalidation = local_extreme - 0.10 * atr15 if side == Side.LONG.value else local_extreme + 0.10 * atr15
    candidate = Candidate(
        side=side,
        setup_type=SetupType.BREAKOUT_RETEST.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": structure,
            "liquidity": 17,
            "trigger": max(int(trigger.get("score", 0)), 13 if retest.get("confirmed") else int(trigger.get("score", 0))),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(retest.get("confirmed") and trigger.get("ready")),
        trigger_level=round_price(level),
        invalidation_level=round_price(invalidation),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if context["tf15"].get("bias") == side and context["tf1h"].get("bias") != opposite(side) else "RISKY",
        setup_family=SetupFamily.EXPANSION.value,
        variant="CONFIRMED_BOS_RETEST" if retest.get("confirmed") else "RETEST_FORMING",
        trigger_ts=int((recent[-1].ts if recent else 0)),
        execution_profile="TACTICAL",
        specificity=39,
        execution_anchor=round_price(level),
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


def candidate_range_compression_breakout(context: dict[str, Any], side: str) -> Optional[Candidate]:
    micro = micro_compression_snapshot(context["candles"]["3m"])
    if not micro.get("accepted") or micro.get("side") != side:
        return None
    c15 = closed(context["candles"]["15m"])
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    recent15 = c15[-6:]
    width15 = (max(c.high for c in recent15) - min(c.low for c in recent15)) / atr15
    compressed_15m = width15 <= COMPRESSION_MAX_WIDTH_ATR or safe_float(context["tf15"].get("efficiency")) <= 0.30
    if not compressed_15m or context["regime"] == Regime.SHOCK.value:
        return None
    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(micro.get("boundary")))
    accepted15 = context["s15"].get("bos") == side
    zone = relevant_zone(context, side, 0.65)
    location, confirmations = side_location_score(context, side, zone)
    location = max(location, 10)
    confirmations.extend([
        f"3M compression overlap {micro.get('overlap')}",
        "micro-BOS закрився за межею стиснення",
        f"15M баланс стиснений до {round(width15, 2)} ATR",
    ])
    if accepted15:
        confirmations.append("15M також прийняв breakout")
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    htf_points, htf_conf, htf_risks = htf_score(context, side)
    confirmations.extend(flow_conf + htf_conf)
    candidate = Candidate(
        side=side,
        setup_type=SetupType.RANGE_COMPRESSION_BREAKOUT.value,
        raw_score=0,
        final_score=0,
        score_components={
            "location": location,
            "structure": 23 if accepted15 else 19,
            "liquidity": 16,
            "trigger": max(int(trigger.get("score", 0)), 13),
            "flow": flow_points,
            "htf": htf_points,
        },
        trigger_ready=bool(trigger.get("ready")),
        trigger_level=safe_float(micro.get("boundary")) or context["price"],
        invalidation_level=safe_float(micro.get("invalidation")),
        target_levels=context["targets_long" if side == Side.LONG.value else "targets_short"],
        confirmations=confirmations,
        risks=flow_risks + htf_risks,
        risk_mode="NORMAL" if accepted15 and context["flow"].get("bias") != opposite(side) else "RISKY",
        setup_family=SetupFamily.EXPANSION.value,
        variant="ACCEPTED_15M_COMPRESSION_BREAK" if accepted15 else "EARLY_MICRO_COMPRESSION_BREAK",
        trigger_ts=int(micro.get("trigger_ts", 0) or 0),
        execution_profile="TACTICAL",
        specificity=32 if accepted15 else 27,
        execution_anchor=safe_float(micro.get("boundary")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    candidate.raw_score = sum(candidate.score_components.values())
    candidate.final_score = candidate.raw_score
    return finalize_candidate(context, candidate)


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
    trigger = trigger_snapshot(context["candles"]["3m"], side, safe_float(sweep.get("level")))
    flow_points, flow_conf, flow_risks = side_flow_score(context, side)
    components = {
        "location": 25,
        "structure": 14 + (6 if context["s3"].get("choch") == side else 0),
        "liquidity": 20,
        "trigger": int(trigger.get("score", 0)),
        "flow": flow_points,
        "htf": 2,
    }
    candidate = Candidate(
        side=side,
        setup_type=SetupType.RANGE_EDGE_REVERSAL.value,
        raw_score=sum(components.values()),
        final_score=sum(components.values()),
        score_components=components,
        trigger_ready=bool(trigger.get("ready")),
        trigger_level=safe_float(sweep.get("level")) or context["price"],
        invalidation_level=safe_float(sweep.get("extreme")),
        target_levels=[safe_float(dr.get("equilibrium")), safe_float(dr.get("high" if side == Side.LONG.value else "low"))],
        confirmations=["ціна на зовнішньому краї 15M діапазону", "край діапазону зняв ліквідність"] + flow_conf,
        risks=flow_risks,
        risk_mode="RISKY",
        setup_family=SetupFamily.RANGE_EXECUTION.value,
        variant="EDGE_SWEEP_MEAN_REVERSION",
        trigger_ts=int(sweep.get("ts", 0) or 0),
        execution_profile="RANGE",
        specificity=30,
        execution_anchor=safe_float(trigger.get("event_price")) or context["price"],
        trigger_age_minutes=safe_float(trigger.get("age_minutes")),
    )
    return finalize_candidate(context, candidate)



def timeframe_rank(timeframe: str) -> int:
    return {"3m": 1, "15m": 2, "1h": 3, "4h": 4}.get(str(timeframe).lower(), 0)


def entry_extension_limit(candidate: Candidate) -> float:
    # High Quality Continuation uses standard/good limits so entries are not delayed
    if candidate.execution_lane == "HIGH_QUALITY_CONTINUATION":
        return max(MAX_ENTRY_EXTENSION_STANDARD, 0.55)
    return {
        "TACTICAL": MAX_ENTRY_EXTENSION_TACTICAL,
        "STANDARD": MAX_ENTRY_EXTENSION_STANDARD,
        "RECOVERY": MAX_ENTRY_EXTENSION_RECOVERY,
        "TRANSITION": MAX_ENTRY_EXTENSION_TRANSITION,
        "BASE_REENTRY": MAX_ENTRY_EXTENSION_BASE,
        "RANGE": MAX_ENTRY_EXTENSION_RANGE,
    }.get(candidate.execution_profile, MAX_ENTRY_EXTENSION_STANDARD)


def zone_text(zone: Optional[Zone]) -> str:
    if not zone:
        return ""
    return f"{zone.timeframe} {zone.kind} {round_price(zone.low)}–{round_price(zone.high)}"


def zones_relevant_to_price(context: dict[str, Any], side: str, price: float, max_ahead_atr: float = 3.5) -> list[Zone]:
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    result: list[Zone] = []
    for zone in _zone_objects(context):
        if zone.mitigated or zone.side != opposite(side) or zone.timeframe not in {"15m", "1h", "4h"}:
            continue
        if zone.low <= price <= zone.high:
            result.append(zone)
            continue
        if side == Side.LONG.value and zone.low > price and (zone.low - price) / atr15 <= max_ahead_atr:
            result.append(zone)
        if side == Side.SHORT.value and zone.high < price and (price - zone.high) / atr15 <= max_ahead_atr:
            result.append(zone)
    return result


def strongest_opposing_zone(context: dict[str, Any], side: str, price: float) -> Optional[Zone]:
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    zones = zones_relevant_to_price(context, side, price)
    if not zones:
        return None
    def score(zone: Zone) -> float:
        inside = zone.low <= price <= zone.high
        if inside:
            distance = 0.0
        elif side == Side.LONG.value:
            distance = max(0.0, zone.low - price) / atr15
        else:
            distance = max(0.0, price - zone.high) / atr15
        return timeframe_rank(zone.timeframe) * 100 + min(30.0, safe_float(zone.strength) * 8.0) + (45 if inside else 0) - distance * 12.0
    return max(zones, key=score)


def zone_acceptance_snapshot(context: dict[str, Any], side: str, zone: Optional[Zone]) -> dict[str, Any]:
    if not zone:
        return {"required": False, "accepted": True, "reason": "немає протилежної HTF-зони"}
    price = safe_float(context["price"])
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    boundary = zone.high if side == Side.LONG.value else zone.low
    c3 = closed(context.get("candles", {}).get("3m", []))[-12:]
    c15 = closed(context.get("candles", {}).get("15m", []))[-4:]
    def beyond(c: Candle) -> bool:
        return c.close > boundary + HTF_ZONE_BUFFER_ATR15 * atr15 if side == Side.LONG.value else c.close < boundary - HTF_ZONE_BUFFER_ATR15 * atr15
    beyond_idx = [i for i, c in enumerate(c3) if beyond(c)]
    consecutive = 0
    for c in reversed(c3):
        if beyond(c):
            consecutive += 1
        else:
            break
    retest = False
    if beyond_idx:
        first = beyond_idx[0]
        for c in c3[first + 1:]:
            if side == Side.LONG.value:
                if c.low <= boundary + HTF_ZONE_RETEST_ATR15 * atr15 and c.close > boundary:
                    retest = True
                    break
            else:
                if c.high >= boundary - HTF_ZONE_RETEST_ATR15 * atr15 and c.close < boundary:
                    retest = True
                    break
    accepted15 = bool(c15 and beyond(c15[-1]))
    accepted = bool((consecutive >= HTF_ZONE_ACCEPTANCE_3M_CLOSES or accepted15) and retest)
    inside_or_near = zone.low - 0.10 * atr15 <= price <= zone.high + 0.10 * atr15
    return {
        "required": inside_or_near,
        "accepted": accepted,
        "boundary": round_price(boundary),
        "consecutive_3m": consecutive,
        "accepted_15m": accepted15,
        "retest": retest,
        "zone": zone_text(zone),
        "reason": (
            f"{zone_text(zone)} прийнята і підтверджена ретестом"
            if accepted
            else f"потрібне прийняття {zone_text(zone)}: 2 закриті 3M або 15M close за межею {round_price(boundary)} + ретест"
        ),
    }


def setup_acceptance_snapshot(context: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    family = candidate.setup_family or SETUP_FAMILY.get(candidate.setup_type, SetupFamily.NONE.value)
    if family not in {SetupFamily.EXPANSION.value, SetupFamily.STRUCTURAL_TRANSITION.value}:
        return {"required": False, "accepted": True, "reason": "окреме breakout acceptance не потрібне"}
    level = safe_float(candidate.trigger_level)
    if level <= 0:
        return {"required": True, "accepted": False, "reason": "немає структурного breakout-рівня"}
    atr15 = max(safe_float(context["tf15"].get("atr")), context["price"] * 0.001)
    c3 = closed(context["candles"]["3m"])[-12:]
    c15 = closed(context["candles"]["15m"])[-4:]
    def beyond(c: Candle) -> bool:
        return c.close > level + 0.03 * atr15 if candidate.side == Side.LONG.value else c.close < level - 0.03 * atr15
    beyond_idx = [i for i, c in enumerate(c3) if beyond(c)]
    consecutive = 0
    for c in reversed(c3):
        if beyond(c):
            consecutive += 1
        else:
            break
    retest = False
    if beyond_idx:
        first = beyond_idx[0]
        for c in c3[first + 1:]:
            if candidate.side == Side.LONG.value:
                if c.low <= level + 0.16 * atr15 and c.close > level:
                    retest = True
                    break
            else:
                if c.high >= level - 0.16 * atr15 and c.close < level:
                    retest = True
                    break
    accepted15 = bool(c15 and beyond(c15[-1]) and context["s15"].get("bos") == candidate.side)
    detector_retest = candidate.setup_type == SetupType.BREAKOUT_RETEST.value and "CONFIRMED" in candidate.variant
    accepted = bool(detector_retest or ((consecutive >= 2 or accepted15) and retest))
    if context["regime"] == Regime.RANGE.value and candidate.setup_type in {
        SetupType.TREND_IGNITION.value, SetupType.RANGE_COMPRESSION_BREAKOUT.value
    }:
        accepted = bool(accepted15 and retest)
    return {
        "required": True,
        "accepted": accepted,
        "level": round_price(level),
        "consecutive_3m": consecutive,
        "accepted_15m": accepted15,
        "retest": retest or detector_retest,
        "reason": (
            "breakout прийнятий і ретест підтверджений"
            if accepted
            else f"потрібне прийняття рівня {round_price(level)}: 2 закриті 3M/15M BOS та ретест; ранній micro-break не є входом"
        ),
    }


def higher_timeframe_scenario(context: dict[str, Any], side: str) -> str:
    b4 = (context.get("tf4h") or {}).get("bias", Side.NEUTRAL.value)
    b1 = (context.get("tf1h") or {}).get("bias", Side.NEUTRAL.value)
    b15 = (context.get("tf15") or {}).get("bias", Side.NEUTRAL.value)
    if b4 == side and b1 == side:
        return f"4H/1H підтримують {side}; 15M визначає інвалідацію"
    if b4 == opposite(side) and b1 == opposite(side):
        return f"4H/1H проти {side}; потрібен повний 15M direction flip"
    if b1 == side:
        return f"1H підтримує {side}, 4H нейтральний/перехідний"
    return "старший контекст змішаний; тільки підтверджена 15M структура"


def natural_target_blocker(context: dict[str, Any], side: str, entry: float, required_distance: float) -> Optional[Zone]:
    for zone in zones_relevant_to_price(context, side, entry, max_ahead_atr=8.0):
        near_edge = zone.low if side == Side.LONG.value else zone.high
        distance = side_sign(side) * (near_edge - entry)
        if 0 < distance < required_distance and timeframe_rank(zone.timeframe) >= 2:
            acceptance = zone_acceptance_snapshot(context, side, zone)
            if not acceptance.get("accepted"):
                return zone
    return None


def latest_same_side_failure(state: dict[str, Any], side: str) -> Optional[dict[str, Any]]:
    for item in reversed(list(state.get("history") or [])):
        if item.get("type") != "CLOSE" or item.get("side") != side:
            continue
        if item.get("action") in {Action.STOP.value, Action.EXIT.value}:
            return item
        # A profitable/neutral close ends the failed-thesis chain.
        return None
    return None


def candidate_thesis_key(context: dict[str, Any], candidate: Candidate) -> str:
    atr15 = max(safe_float(context["tf15"].get("atr")), safe_float(context.get("price")) * 0.001)
    trigger_bucket = round(safe_float(candidate.trigger_level) / max(atr15 * 0.20, 1e-9))
    invalid_bucket = round(safe_float(candidate.invalidation_level) / max(atr15 * 0.25, 1e-9))
    return "|".join([
        candidate.side,
        candidate.setup_family or SETUP_FAMILY.get(candidate.setup_type, SetupFamily.NONE.value),
        candidate.setup_type,
        str(trigger_bucket),
        str(invalid_bucket),
    ])


def event_driven_reentry_guard(state: dict[str, Any], context: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    """Suppress only an exact stale replay; never block by elapsed minutes."""
    failure = latest_same_side_failure(state, candidate.side)
    candidate.thesis_key = candidate_thesis_key(context, candidate)
    if not failure:
        return {"blocked": False, "reason": "", "events": ["немає попередньої невдалої тези"]}

    closed_at = parse_iso(str(failure.get("time") or ""))
    close_ms = int(closed_at.timestamp() * 1000) if closed_at else 0
    atr15 = max(safe_float(context["tf15"].get("atr")), safe_float(context.get("price")) * 0.001)
    previous_key = str(failure.get("thesis_key") or "")
    previous_trigger = safe_float(failure.get("trigger_level"))
    previous_invalid = safe_float(failure.get("structural_invalidation"))
    previous_family = str(failure.get("setup_family") or "")
    previous_type = str(failure.get("setup_type") or "")

    events: list[str] = []
    if candidate.trigger_ts and candidate.trigger_ts > close_ms:
        events.append("новий закритий 3M execution-event після стопа")
    for key in ("sweep3", "sweep15"):
        sweep = context.get(key) or {}
        if sweep.get("side") == candidate.side and int(sweep.get("ts", 0) or 0) > close_ms:
            events.append(f"новий {key} liquidity sweep/reclaim")
    c15 = closed((context.get("candles") or {}).get("15m", []))
    if c15 and c15[-1].ts > close_ms and (
        context.get("s15", {}).get("bos") == candidate.side
        or context.get("s15", {}).get("choch") == candidate.side
    ):
        events.append("новий закритий 15M BOS/CHOCH")
    if candidate.setup_family and previous_family and candidate.setup_family != previous_family:
        events.append("нова незалежна сім'я сетапу")
    elif candidate.setup_type and previous_type and candidate.setup_type != previous_type:
        events.append("новий тип сетапу в межах напрямку")
    if previous_trigger and abs(safe_float(candidate.trigger_level) - previous_trigger) >= REENTRY_NEW_TRIGGER_SEPARATION_ATR15 * atr15:
        events.append("новий структурний trigger-рівень")
    if previous_invalid and abs(safe_float(candidate.invalidation_level) - previous_invalid) >= REENTRY_NEW_INVALIDATION_SEPARATION_ATR15 * atr15:
        events.append("нова ICT-інвалідація/нова база")
    if any(token in candidate.variant for token in ("CONFIRMED", "FULL_15M_BASE", "COMPACT_3M_CONFIRMED_BASE")) and candidate.trigger_ts > close_ms:
        events.append("підтверджений reset замість повтору старої тези")

    if events:
        return {"blocked": False, "reason": "", "events": list(dict.fromkeys(events))}

    same_key = bool(previous_key and previous_key == candidate.thesis_key)
    reason = (
        "це той самий невдалий trigger/рівень без нового sweep, BOS, ретесту або бази; "
        "часової заборони немає — вхід дозволиться одразу після нової ринкової події"
    )
    return {"blocked": True, "reason": reason, "events": [], "same_key": same_key}


def _trade_result_r(trade: dict[str, Any]) -> float:
    direct = trade.get("result_r")
    if direct is not None:
        return safe_float(direct)
    entry = safe_float(trade.get("entry"))
    stop = safe_float(trade.get("stop_initial"))
    exit_price = safe_float(trade.get("exit_price"))
    side = str(trade.get("side") or "")
    risk = abs(entry - stop)
    if not entry or not exit_price or risk <= 0 or side not in {Side.LONG.value, Side.SHORT.value}:
        return 0.0
    return side_sign(side) * (exit_price - entry) / risk


def setup_edge_snapshot(journal: Optional[dict[str, Any]], candidate: Candidate, regime: str) -> dict[str, Any]:
    trades = list((journal or {}).get("trades") or [])[-EDGE_MAX_LOOKBACK:]
    # Do not let legacy trades with tiny/foreign stop geometry train the new
    # expectancy model. Only outcomes produced by this architecture are used.
    closed_trades = [
        t for t in trades
        if t.get("result_pct") is not None
        and str(t.get("architecture_version") or "") == ARCHITECTURE_VERSION
    ]

    def match(t: dict[str, Any], tier: int) -> bool:
        if str(t.get("side") or "") != candidate.side:
            return False
        if tier == 0:
            return str(t.get("variant") or "") == candidate.variant and str(t.get("regime") or "") == regime
        if tier == 1:
            return str(t.get("setup_type") or "") == candidate.setup_type and str(t.get("regime") or "") == regime
        if tier == 2:
            return str(t.get("setup_family") or "") == candidate.setup_family and str(t.get("regime") or "") == regime
        return str(t.get("setup_family") or "") == candidate.setup_family

    selected: list[dict[str, Any]] = []
    source = "NO_SAMPLE"
    for tier, label in enumerate(("VARIANT_REGIME", "SETUP_REGIME", "FAMILY_REGIME", "FAMILY_ALL")):
        bucket = [t for t in closed_trades if match(t, tier)]
        if len(bucket) >= EDGE_MIN_SAMPLE or tier == 3:
            selected = bucket
            source = label
            break

    values = [_trade_result_r(t) for t in selected]
    n = len(values)
    raw = sum(values) / n if n else 0.0
    shrunk = (
        (sum(values) + EDGE_PRIOR_TRADES * EDGE_PRIOR_EXPECTANCY_R) / (n + EDGE_PRIOR_TRADES)
        if n or EDGE_PRIOR_TRADES else 0.0
    )
    wins = sum(1 for v in values if v > 0)
    gross_win = sum(v for v in values if v > 0)
    gross_loss = abs(sum(v for v in values if v < 0))
    return {
        "sample_size": n,
        "raw_expectancy_r": round(raw, 3),
        "expectancy_r": round(shrunk, 3),
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
        "source": source,
    }


def apply_setup_edge_feedback(candidate: Candidate, context: dict[str, Any], journal: Optional[dict[str, Any]]) -> Candidate:
    snap = setup_edge_snapshot(journal, candidate, str(context.get("regime") or ""))
    n = int(snap["sample_size"])
    expectancy = safe_float(snap["expectancy_r"])
    adjustment = 0
    risk_multiplier = 1.0
    if n >= EDGE_MIN_SAMPLE:
        if expectancy <= -0.35:
            adjustment = -8
            risk_multiplier = 0.35
        elif expectancy <= -0.15:
            adjustment = -5
            risk_multiplier = 0.55
        elif expectancy >= 0.45:
            adjustment = 4
            risk_multiplier = 1.10
        elif expectancy >= 0.20:
            adjustment = 2
            risk_multiplier = 1.0
    candidate.edge_sample_size = n
    candidate.edge_expectancy_r = round(expectancy, 3)
    candidate.edge_adjustment = adjustment
    candidate.edge_risk_multiplier = risk_multiplier
    candidate.final_score = int(clamp(candidate.final_score + adjustment, 0, 100))
    if n >= EDGE_MIN_SAMPLE:
        if adjustment < 0:
            candidate.risks.append(
                f"адаптивний edge: {n} угод, expectancy {expectancy:.2f}R; ризик позиції знижено, але сетап не блокується"
            )
            candidate.risk_mode = "RISKY"
        elif adjustment > 0:
            candidate.confirmations.append(
                f"адаптивний edge: {n} угод, expectancy {expectancy:.2f}R"
            )
    evidence_count = len(candidate.evidence_families)
    if candidate.hard_reject_reason:
        candidate.stage = "REJECTED"
    elif candidate.trigger_ready and evidence_count >= MIN_RISKY_EVIDENCE:
        candidate.stage = "EXECUTABLE" if evidence_count >= MIN_ENTRY_EVIDENCE else "TRIGGERED"
    elif candidate.final_score >= ARMED_SCORE:
        candidate.stage = "ARMED"
    else:
        candidate.stage = "DISCOVERED"
    return candidate


def apply_candidate_risk_adjustments(context: dict[str, Any], candidate: Candidate) -> Candidate:
    penalty = 0
    price = context["price"]
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    family = candidate.setup_family or SETUP_FAMILY.get(candidate.setup_type, SetupFamily.NONE.value)
    # Expansion/transition must be measured from the structural level, not from
    # the latest event candle. Otherwise a late candle can redefine the anchor
    # and make a chase look timely.
    if family in {SetupFamily.EXPANSION.value, SetupFamily.STRUCTURAL_TRANSITION.value}:
        anchor = candidate.trigger_level or candidate.execution_anchor or price
    else:
        anchor = candidate.execution_anchor or candidate.trigger_level or price
    extension = abs(price - anchor) / atr15
    candidate.late_extension_atr = round(extension, 3)
    if candidate.trigger_ready and candidate.trigger_age_minutes > TRIGGER_MAX_AGE_MINUTES:
        candidate.hard_reject_reason = "3M execution-trigger застарів; потрібна нова закрита подія"
    extension_limit = entry_extension_limit(candidate)
    if extension > extension_limit:
        penalty += 12
        candidate.risks.append(
            f"ціна відійшла від структурного trigger-рівня на {extension:.2f} ATR; максимум {extension_limit:.2f} ATR"
        )
    if extension > extension_limit + 0.55:
        candidate.hard_reject_reason = "execution window закрилось; новий вхід тільки після окремого ретесту"

    adverse = candidate_adverse_families(context, candidate)
    penalty += 5 * len(adverse)
    if len(adverse) > MAX_ADVERSE_EVIDENCE:
        candidate.risk_mode = "RISKY"
        candidate.risks.append("кілька незалежних шарів зараз проти входу")
    if len(adverse) >= 3 and candidate.variant.startswith("EARLY"):
        candidate.hard_reject_reason = "ранній варіант має три незалежні шари проти"

    blocking = strongest_opposing_zone(context, candidate.side, price)
    acceptance = zone_acceptance_snapshot(context, candidate.side, blocking)
    if blocking and acceptance.get("required") and not acceptance.get("accepted"):
        penalty += 10 + timeframe_rank(blocking.timeframe) * 2
        candidate.risk_mode = "RISKY"
        candidate.risks.append(acceptance["reason"])

    regime = context["regime"]
    preferred = {
        Regime.TREND.value: {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value},
        Regime.RANGE.value: {SetupFamily.RANGE_EXECUTION.value, SetupFamily.EXPANSION.value},
        Regime.TRANSITION.value: {SetupFamily.STRUCTURAL_TRANSITION.value, SetupFamily.LIQUIDITY_RECOVERY.value},
        Regime.SHOCK.value: {SetupFamily.LIQUIDITY_RECOVERY.value, SetupFamily.STRUCTURAL_TRANSITION.value},
        Regime.NORMAL.value: set(x.value for x in SetupFamily if x != SetupFamily.NONE),
    }
    if family not in preferred.get(regime, set()):
        penalty += 8
        candidate.risks.append(f"сімейство {family} не є пріоритетним для режиму {regime}")
    if regime == Regime.RANGE.value and candidate.setup_type in {
        SetupType.TREND_IGNITION.value, SetupType.RANGE_COMPRESSION_BREAKOUT.value
    } and context["s15"].get("bos") != candidate.side:
        penalty += 9
        candidate.risk_mode = "RISKY"
        candidate.risks.append("у RANGE ранній micro-break лише ARMED; потрібне 15M acceptance + ретест")
    if regime == Regime.SHOCK.value and candidate.setup_type not in {
        SetupType.CAPITULATION_RECOVERY.value,
        SetupType.SWEEP_RECLAIM.value,
        SetupType.DIRECTION_FLIP.value,
    }:
        penalty += 8
        candidate.risk_mode = "RISKY"
    if context["tf4h"].get("bias") == opposite(candidate.side) and context["tf1h"].get("bias") == opposite(candidate.side):
        if candidate.setup_type not in {SetupType.DIRECTION_FLIP.value, SetupType.CAPITULATION_RECOVERY.value}:
            penalty += 12
            candidate.risks.append("4H і 1H одночасно проти; звичайний continuation не дозволяється")

    if candidate.invalidation_level <= 0:
        candidate.hard_reject_reason = "немає об'єктивної ICT-інвалідації"
    elif candidate.side == Side.LONG.value and candidate.invalidation_level >= price:
        candidate.hard_reject_reason = "LONG-інвалідація знаходиться вище ціни"
    elif candidate.side == Side.SHORT.value and candidate.invalidation_level <= price:
        candidate.hard_reject_reason = "SHORT-інвалідація знаходиться нижче ціни"

    candidate.final_score = max(0, min(100, candidate.raw_score - penalty))
    return candidate

def finalize_candidate(context: dict[str, Any], candidate: Candidate) -> Candidate:
    candidate.setup_family = candidate.setup_family or SETUP_FAMILY.get(candidate.setup_type, SetupFamily.NONE.value)
    candidate = apply_scan_event_to_candidate(context, candidate)
    candidate = apply_candidate_risk_adjustments(context, candidate)
    candidate.evidence_families = candidate_evidence_families(context, candidate)

    # === Компромісний HTF Confluence Gate (regime-aware) ===
    regime = str(context.get("regime", "NORMAL")).upper()
    htf_result = evaluate_htf_confluence(context, candidate, regime)

    if htf_result["hard_block"]:
        candidate.hard_reject_reason = htf_result["reason"]
        candidate.final_score = 0
    else:
        candidate.final_score = max(0, candidate.raw_score + htf_result["bonus"])

    # === Confirmation Tiers (Етап 1 + 3) ===
    candidate.confirmation_tier = calculate_confirmation_tier(context, candidate, htf_result)

    # High Quality Continuation lane assignment (тільки для Tier 3+)
    is_high_quality = (
        candidate.setup_family in {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}
        and candidate.final_score >= HIGH_QUALITY_CONTINUATION_SCORE
        and htf_result["htf_aligned"]
        and candidate.confirmation_tier >= TIER_HIGH_QUALITY
        and not candidate.hard_reject_reason
    )
    if is_high_quality:
        candidate.execution_lane = "HIGH_QUALITY_CONTINUATION"
    else:
        candidate.execution_lane = classify_execution_lane(candidate)

    evidence_count = len(candidate.evidence_families)
    if candidate.hard_reject_reason:
        candidate.stage = "REJECTED"
    elif candidate.trigger_ready and evidence_count >= MIN_RISKY_EVIDENCE:
        candidate.stage = "EXECUTABLE" if evidence_count >= MIN_ENTRY_EVIDENCE else "TRIGGERED"
    elif candidate.final_score >= ARMED_SCORE:
        candidate.stage = "ARMED"
    else:
        candidate.stage = "DISCOVERED"
    if candidate.trigger_ready and evidence_count < MIN_RISKY_EVIDENCE:
        candidate.risks.append("тригер є, але незалежних сімейств доказів недостатньо")
    return candidate



def candidate_selection_score(candidate: Candidate) -> float:
    return (
        candidate.final_score
        + len(candidate.evidence_families) * 2.5
        + (5.0 if candidate.trigger_ready else 0.0)
        + candidate.specificity * 0.20
        + (2.0 if "FULL" in candidate.variant or "CONFIRMED" in candidate.variant else 0.0)
    )


def collapse_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Keep one dominant detector per semantic side/family episode."""
    exact: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        key = (candidate.side, candidate.setup_type)
        if key not in exact or candidate_selection_score(candidate) > candidate_selection_score(exact[key]):
            exact[key] = candidate
    family_best: dict[tuple[str, str], Candidate] = {}
    for candidate in exact.values():
        key = (candidate.side, candidate.setup_family)
        if key not in family_best or candidate_selection_score(candidate) > candidate_selection_score(family_best[key]):
            family_best[key] = candidate
    return sorted(family_best.values(), key=lambda c: (candidate_selection_score(c), c.final_score), reverse=True)


def build_candidates(context: dict[str, Any]) -> list[Candidate]:
    candidates: list[Candidate] = []
    builders = (
        candidate_capitulation_recovery,
        candidate_sweep_reclaim,
        candidate_direction_flip,
        candidate_trend_ignition,
        candidate_fresh_base_continuation,
        candidate_pullback_continuation,
        candidate_breakout_retest,
        candidate_range_compression_breakout,
        candidate_range_edge,
    )
    for side in (Side.LONG.value, Side.SHORT.value):
        for builder in builders:
            try:
                candidate = builder(context, side)
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                print(f"[WARN] Candidate {builder.__name__}/{side} failed: {exc}")

    # First remove exact duplicates. Then keep only the dominant detector inside
    # each semantic family and side. Thus a capitulation sweep cannot compete
    # with an ordinary sweep, and a fresh-base re-entry cannot compete with a
    # generic pullback for the same directional episode.
    return collapse_candidates(candidates)


def setup_thresholds(candidate: Candidate) -> tuple[int, int]:
    table = {
        SetupType.SWEEP_RECLAIM.value: (76, 67),
        SetupType.CAPITULATION_RECOVERY.value: (80, 70),
        SetupType.DIRECTION_FLIP.value: (79, 68),
        SetupType.TREND_IGNITION.value: (78, 70),
        SetupType.PULLBACK_CONTINUATION.value: (76, 66),
        SetupType.FRESH_BASE_CONTINUATION.value: (76, 68),
        SetupType.BREAKOUT_RETEST.value: (75, 68),
        SetupType.RANGE_COMPRESSION_BREAKOUT.value: (77, 69),
        SetupType.RANGE_EDGE_REVERSAL.value: (75, 68),
    }
    full, risky = table.get(candidate.setup_type, (ENTRY_SCORE, RISKY_ENTRY_SCORE))
    if "FULL" in candidate.variant or "CONFIRMED" in candidate.variant:
        full -= 1
    return max(ENTRY_SCORE, full), max(RISKY_ENTRY_SCORE, risky)


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


def _context_structure(context: dict[str, Any], timeframe: str) -> dict[str, Any]:
    direct = context.get(f"s{timeframe}")
    if isinstance(direct, dict):
        return direct
    tf = context.get(f"tf{timeframe}") or {}
    return tf.get("structure") or {}


def _zone_objects(context: dict[str, Any]) -> list[Zone]:
    result: list[Zone] = []
    for item in context.get("zones") or []:
        if isinstance(item, Zone):
            result.append(item)
            continue
        if isinstance(item, dict):
            try:
                clean = {k: item[k] for k in Zone.__dataclass_fields__ if k in item}
                result.append(Zone(**clean))
            except Exception:
                continue
    return result


def select_robust_invalidation(
    context: dict[str, Any], candidate: Candidate, entry: float, atr15: float,
    minimum_stop_atr: float, maximum_stop_atr: float, buffer_atr: float
) -> tuple[float, str, str]:
    """Select the nearest defensible protected level in the correct hierarchy.

    Filtering uses the *final* stop distance (structural level plus buffer), so a
    valid 15M swing is not skipped merely because the bare level is slightly
    closer than the anti-noise floor. Distance is heavily penalized to avoid
    choosing a remote old OB when a nearer protected FVG/swing is sufficient.
    """
    side = candidate.side
    sign = side_sign(side)
    minimum = minimum_stop_atr * atr15
    maximum = maximum_stop_atr * atr15
    family = candidate.setup_family or SETUP_FAMILY.get(candidate.setup_type, SetupFamily.NONE.value)
    options: list[tuple[float, float, str, str]] = []

    def add(level: float, basis: str, timeframe: str, quality: float) -> None:
        level = safe_float(level)
        if level <= 0:
            return
        structure_distance = sign * (entry - level)
        final_distance = structure_distance + buffer_atr * atr15
        if structure_distance <= 0 or final_distance < minimum or final_distance > maximum:
            return
        distance_atr = final_distance / max(atr15, 1e-9)
        options.append((quality - distance_atr * 2.6, level, basis, timeframe))

    raw = safe_float(candidate.invalidation_level)
    add(raw, f"{candidate.variant or candidate.setup_type}: власна setup-інвалідація", "SETUP", 16.0)

    # A tactical stop is allowed only behind a confirmed 3M structural event,
    # never at an arbitrary micro distance. Standard entries continue to use
    # the 15M/1H hierarchy below.
    if candidate.execution_lane in {"EARLY_TACTICAL", "MISSED_IMPULSE_REENTRY"}:
        scan_event = scan_event_for_side(context, side)
        if scan_event.get("stage") in {"RETEST", "READY", "STRUCTURE_SHIFT"}:
            add(scan_event.get("invalidation_level"), "підтверджена 3M інвалідація події", "3M", 43.0)
        s3 = _context_structure(context, "3")
        add(s3.get("swing_low" if side == Side.LONG.value else "swing_high"),
            "закритий 3M protected swing після execution-event", "3M", 36.0)
        for value in s3.get("recent_lows" if side == Side.LONG.value else "recent_highs", [])[-3:]:
            add(value, "закритий 3M structural pivot після trigger", "3M", 31.0)

    s15 = _context_structure(context, "15")
    add(s15.get("swing_low" if side == Side.LONG.value else "swing_high"),
        "закритий 15M protected swing", "15M", 38.0)
    for value in s15.get("recent_lows" if side == Side.LONG.value else "recent_highs", [])[-4:]:
        add(value, "закритий 15M структурний pivot", "15M", 33.0)

    s1h = _context_structure(context, "1h")
    add(s1h.get("swing_low" if side == Side.LONG.value else "swing_high"),
        "закритий 1H protected swing", "1H", 31.0)
    for value in s1h.get("recent_lows" if side == Side.LONG.value else "recent_highs", [])[-4:]:
        add(value, "закритий 1H структурний pivot", "1H", 27.0)

    for zone in _zone_objects(context):
        if zone.mitigated or zone.side != side or zone.timeframe not in {"15m", "1h", "4h"}:
            continue
        boundary = zone.low if side == Side.LONG.value else zone.high
        if zone.timeframe == "15m":
            quality = 41.0 + min(5.0, safe_float(zone.strength) * 1.5)
        elif zone.timeframe == "1h":
            quality = 33.0 + min(6.0, safe_float(zone.strength) * 1.5)
        else:
            if family not in {
                SetupFamily.LIQUIDITY_RECOVERY.value,
                SetupFamily.STRUCTURAL_TRANSITION.value,
                SetupFamily.RANGE_EXECUTION.value,
            }:
                continue
            quality = 24.0 + min(6.0, safe_float(zone.strength) * 1.5)
        add(boundary, f"{zone.timeframe} {zone.kind} outer boundary", zone.timeframe.upper(), quality)

    if not options:
        return 0.0, "не знайдено реальної 15M/1H ICT-інвалідації в допустимому ризику", ""
    _, level, basis, timeframe = max(options, key=lambda item: item[0])
    return level, basis, timeframe


def build_entry_map(
    context: dict[str, Any], candidate: Candidate, structural: float, atr15: float
) -> dict[str, Any]:
    """Build location -> event -> execution zone for the selected lane."""
    current = safe_float(context.get("price"))
    side = candidate.side
    trigger = safe_float(candidate.trigger_level or candidate.execution_anchor or current)
    anchor = trigger if candidate.setup_family in {SetupFamily.EXPANSION.value, SetupFamily.STRUCTURAL_TRANSITION.value} else safe_float(candidate.execution_anchor or trigger)
    limit = entry_extension_limit(candidate)
    lane = candidate.execution_lane or classify_execution_lane(candidate)
    early_lane = lane == "EARLY_TACTICAL"
    reentry_lane = lane == "MISSED_IMPULSE_REENTRY"

    ranked: list[tuple[float, Zone]] = []
    for zone in _zone_objects(context):
        if zone.mitigated or zone.side != side or zone.timeframe not in {"3m", "15m", "1h"}:
            continue
        if side == Side.LONG.value and zone.low <= structural - 0.10 * atr15:
            continue
        if side == Side.SHORT.value and zone.high >= structural + 0.10 * atr15:
            continue
        reference = trigger or anchor or current
        distance = zone_distance(reference, zone) / max(atr15, 1e-9)
        if distance > 1.40:
            continue
        contains_trigger = zone.low - 0.08 * atr15 <= trigger <= zone.high + 0.08 * atr15
        score = (28 if contains_trigger else 0) + timeframe_rank(zone.timeframe) * 7
        score += min(10.0, safe_float(zone.strength) * 3.0) - distance * 10.0
        ranked.append((score, zone))

    if ranked:
        wait = max(ranked, key=lambda item: item[0])[1]
        wait_low, wait_high = wait.low, wait.high
    else:
        wait_low = trigger - 0.18 * atr15
        wait_high = trigger + 0.18 * atr15

    if side == Side.LONG.value:
        entry_low = min(trigger, wait_high) - ENTRY_ZONE_RETEST_ATR15 * atr15
        entry_high = trigger + limit * atr15
        condition = f"закрита 3M подія вище {round_price(trigger)} та утримання {round_price(entry_low)}–{round_price(entry_high)}"
        extension = max(0.0, current - trigger) / atr15
    else:
        entry_low = trigger - limit * atr15
        entry_high = max(trigger, wait_low) + ENTRY_ZONE_RETEST_ATR15 * atr15
        condition = f"закрита 3M подія нижче {round_price(trigger)} та утримання {round_price(entry_low)}–{round_price(entry_high)}"
        extension = max(0.0, trigger - current) / atr15

    entry_low, entry_high = sorted((entry_low, entry_high))
    tolerance = 0.04 * atr15
    inside = entry_low - tolerance <= current <= entry_high + tolerance
    blocking = strongest_opposing_zone(context, side, current)
    zone_acceptance = zone_acceptance_snapshot(context, side, blocking)
    setup_acceptance = setup_acceptance_snapshot(context, candidate)
    event = scan_event_for_side(context, side)
    event_stage = str(event.get("stage") or candidate.scan_event_stage or "")
    event_acceptance = bool(
        event_stage in {"RETEST", "READY"}
        or (event_stage == "STRUCTURE_SHIFT" and event.get("displacement") and candidate.trigger_ready)
    )

    hard_opposing_zone = bool(
        blocking
        and blocking.timeframe in {"1h", "4h"}
        and safe_float(blocking.strength) >= 0.80
        and zone_acceptance.get("required")
        and not zone_acceptance.get("accepted")
    )
    if early_lane or reentry_lane:
        setup_ready = bool(setup_acceptance.get("accepted") or event_acceptance)
        zone_ready = bool(
            not zone_acceptance.get("required")
            or zone_acceptance.get("accepted")
            or (event_acceptance and not hard_opposing_zone)
        )
    else:
        setup_ready = bool(setup_acceptance.get("accepted"))
        zone_ready = bool(not zone_acceptance.get("required") or zone_acceptance.get("accepted"))

    acceptance_ready = bool(zone_ready and setup_ready)
    execution_ready = bool(candidate.trigger_ready and inside and extension <= limit and acceptance_ready)

    reasons: list[str] = []
    if not candidate.trigger_ready:
        reasons.append("execution-trigger ще не закритий")
    if candidate.trigger_ready and not setup_ready:
        reasons.append(setup_acceptance["reason"])
    if not zone_ready:
        reasons.append(zone_acceptance["reason"])
    if extension > limit:
        reasons.append(f"ціна відійшла на {extension:.2f} ATR при максимумі {limit:.2f}; не наздоганяємо")
    if not inside:
        reasons.append(f"ціна {round_price(current)} поза робочою зоною {round_price(entry_low)}–{round_price(entry_high)}")
    if not reasons:
        if early_lane:
            reasons.append("ранній structural event підтверджений acceptance або retest")
        elif reentry_lane:
            reasons.append("новий 3M ретест підтвердив повторний вхід після пропущеного імпульсу")
        else:
            reasons.append("location, trigger, acceptance і retest узгоджені")

    ideal_entry = (entry_low + entry_high) / 2.0
    return {
        "wait_zone_low": round_price(wait_low),
        "wait_zone_high": round_price(wait_high),
        "entry_zone_low": round_price(entry_low),
        "entry_zone_high": round_price(entry_high),
        "trigger_level": round_price(trigger),
        "trigger_condition": condition,
        "execution_ready": execution_ready,
        "acceptance_ready": acceptance_ready,
        "base_ready": bool(candidate.trigger_ready and extension <= limit and acceptance_ready),
        "execution_reason": "; ".join(reasons),
        "ideal_entry": round_price(ideal_entry),
        "blocking_zone": zone_text(blocking),
        "acceptance_status": f"SETUP: {setup_acceptance['reason']} | HTF: {zone_acceptance['reason']}",
        "max_entry_extension_atr": round(limit, 2),
        "event_stage": event_stage,
        "execution_lane": lane,
    }


def nearest_natural_target(levels: list[float], reference: float, side: str) -> float:
    clean = [safe_float(v) for v in levels if safe_float(v) > 0]
    if side == Side.LONG.value:
        ahead = sorted(v for v in clean if v > reference)
    else:
        ahead = sorted((v for v in clean if v < reference), reverse=True)
    return ahead[0] if ahead else 0.0


def optimal_entry_for_target_rr(stop: float, target: float, rr: float, side: str) -> float:
    if stop <= 0 or target <= 0 or rr <= 0:
        return 0.0
    entry = (target + rr * stop) / (1.0 + rr)
    if side == Side.LONG.value and stop < entry < target:
        return entry
    if side == Side.SHORT.value and target < entry < stop:
        return entry
    return 0.0


def _merge_optimal_entry_zone(entry_map: dict[str, Any], optimal: float, atr15: float, side: str) -> dict[str, Any]:
    result = dict(entry_map)
    if optimal <= 0:
        return result
    half = 0.10 * atr15
    low, high = optimal - half, optimal + half
    wait_low = safe_float(result.get("wait_zone_low"))
    wait_high = safe_float(result.get("wait_zone_high"))
    # Keep the mathematical 2R entry near the real ICT POI when they overlap;
    # otherwise display both and wait for a new retest around the 2R level.
    if wait_low and wait_high and not (high < wait_low - 0.20 * atr15 or low > wait_high + 0.20 * atr15):
        low = max(low, wait_low - 0.08 * atr15)
        high = min(high, wait_high + 0.08 * atr15)
    if low > high:
        low, high = optimal - half, optimal + half
    result["entry_zone_low"] = round_price(low)
    result["entry_zone_high"] = round_price(high)
    result["ideal_entry"] = round_price(optimal)
    current = safe_float(result.get("current_price"))
    return result


def build_trade_plan(context: dict[str, Any], candidate: Candidate) -> TradePlan:
    """Build a profit-seeking plan around real invalidation and real liquidity.

    A good setup is not discarded merely because the current market price gives
    less than 2R. The engine derives the exact entry that makes the nearest real
    target worth at least 2R, keeps the opportunity ARMED and waits for a retest.
    """
    current_price = safe_float(context["price"])
    side = candidate.side
    sign = side_sign(side)
    atr15 = max(safe_float(context["tf15"].get("atr")), current_price * 0.001)
    profiles = {
        "TACTICAL": {"buffer": 0.12, "min_stop": TACTICAL_MIN_STOP_ATR15, "max_stop": TACTICAL_MAX_STOP_ATR15, "min_tp_atr": 1.20, "min_rr": 2.00},
        "RECOVERY": {"buffer": 0.14, "min_stop": 0.65, "max_stop": 2.80, "min_tp_atr": 1.30, "min_rr": 2.00},
        "TRANSITION": {"buffer": 0.14, "min_stop": 0.68, "max_stop": 2.60, "min_tp_atr": 1.30, "min_rr": 2.00},
        "BASE_REENTRY": {"buffer": 0.16, "min_stop": 1.05, "max_stop": 2.80, "min_tp_atr": 1.55, "min_rr": 2.00},
        "RANGE": {"buffer": 0.15, "min_stop": 1.00, "max_stop": 2.50, "min_tp_atr": 1.45, "min_rr": 2.00},
        "STANDARD": {"buffer": 0.16, "min_stop": 1.10, "max_stop": 3.00, "min_tp_atr": 1.60, "min_rr": 2.00},
    }
    cfg = dict(profiles.get(candidate.execution_profile, profiles["STANDARD"]))
    if candidate.execution_lane in {"EARLY_TACTICAL", "MISSED_IMPULSE_REENTRY"}:
        cfg["min_stop"] = min(cfg["min_stop"], TACTICAL_MIN_STOP_ATR15)
        cfg["max_stop"] = min(cfg["max_stop"], TACTICAL_MAX_STOP_ATR15)
        cfg["buffer"] = min(cfg["buffer"], 0.14)
    required_rr = max(MIN_RR1, cfg["min_rr"])

    provisional, _, _ = select_robust_invalidation(
        context, candidate, current_price, atr15, cfg["min_stop"], cfg["max_stop"], cfg["buffer"]
    )
    entry_map = build_entry_map(context, candidate, provisional or candidate.invalidation_level, atr15)
    entry_map["current_price"] = current_price
    seed_entry = current_price if entry_map["execution_ready"] else safe_float(entry_map["ideal_entry"])

    structural, structural_basis, stop_timeframe = select_robust_invalidation(
        context, candidate, seed_entry, atr15, cfg["min_stop"], cfg["max_stop"], cfg["buffer"]
    )
    hard_invalid = ""
    if structural <= 0:
        hard_invalid = structural_basis
        structural = safe_float(candidate.invalidation_level)
    buffer_mult = cfg["buffer"]
    stop = structural - buffer_mult * atr15 if side == Side.LONG.value else structural + buffer_mult * atr15
    stop_basis = f"{structural_basis}; стоп ЗА рівнем + {buffer_mult:.2f} ATR, не всередині зони"

    nearest_target = nearest_natural_target(candidate.target_levels, current_price, side)
    optimal_rr_entry = optimal_entry_for_target_rr(stop, nearest_target, required_rr, side) if nearest_target else 0.0
    chosen_entry = seed_entry
    wait_reasons: list[str] = []

    if optimal_rr_entry:
        current_rr_to_nearest = target_rr(current_price, stop, nearest_target)
        if current_rr_to_nearest < required_rr:
            chosen_entry = optimal_rr_entry
            entry_map = _merge_optimal_entry_zone(entry_map, optimal_rr_entry, atr15, side)
            wait_reasons.append(
                f"поточна ціна дає {current_rr_to_nearest:.2f}R до найближчої реальної ліквідності {round_price(nearest_target)}; "
                f"оптимальний 2R-вхід близько {round_price(optimal_rr_entry)}"
            )

    # Re-evaluate the structural hierarchy at the planned entry. One refinement
    # keeps the target-derived entry and the ICT stop mutually consistent.
    structural2, basis2, timeframe2 = select_robust_invalidation(
        context, candidate, chosen_entry, atr15, cfg["min_stop"], cfg["max_stop"], cfg["buffer"]
    )
    if structural2 > 0:
        structural, structural_basis, stop_timeframe = structural2, basis2, timeframe2
        stop = structural - buffer_mult * atr15 if side == Side.LONG.value else structural + buffer_mult * atr15
        stop_basis = f"{structural_basis}; стоп ЗА рівнем + {buffer_mult:.2f} ATR, не всередині зони"
        if nearest_target:
            refined = optimal_entry_for_target_rr(stop, nearest_target, required_rr, side)
            if refined and target_rr(chosen_entry, stop, nearest_target) < required_rr:
                chosen_entry = refined
                optimal_rr_entry = refined
                entry_map = _merge_optimal_entry_zone(entry_map, refined, atr15, side)

    risk = abs(chosen_entry - stop)
    max_risk = cfg["max_stop"] * atr15
    min_risk = cfg["min_stop"] * atr15
    if risk > max_risk:
        risk_limited_entry = stop + sign * max_risk
        if nearest_target:
            target_limited = optimal_entry_for_target_rr(stop, nearest_target, required_rr, side)
            if target_limited:
                risk_limited_entry = min(risk_limited_entry, target_limited) if side == Side.LONG.value else max(risk_limited_entry, target_limited)
        chosen_entry = risk_limited_entry
        optimal_rr_entry = chosen_entry
        risk = abs(chosen_entry - stop)
        entry_map = _merge_optimal_entry_zone(entry_map, chosen_entry, atr15, side)
        wait_reasons.append(
            f"структурний стоп потребує кращої ціни; чекати близько {round_price(chosen_entry)}, а не стискати стоп"
        )
    if risk < min_risk:
        hard_invalid = hard_invalid or "не знайдено достатньо глибокої 15M/1H ICT-інвалідації; 3M micro-stop не використовується"
    if not ((side == Side.LONG.value and stop < chosen_entry) or (side == Side.SHORT.value and stop > chosen_entry)):
        hard_invalid = "стоп знаходиться з неправильної сторони від планового входу"

    # Execution is allowed only at the planned price zone. A setup remains valid
    # and visible while waiting; it is not deleted merely because price is late.
    zone_low = safe_float(entry_map["entry_zone_low"])
    zone_high = safe_float(entry_map["entry_zone_high"])
    tolerance = 0.04 * atr15
    inside_planned_zone = zone_low - tolerance <= current_price <= zone_high + tolerance
    execution_ready = bool(entry_map.get("base_ready") and inside_planned_zone)
    execution_reason_parts = [str(entry_map.get("execution_reason") or "")]
    execution_reason_parts.extend(wait_reasons)
    if not inside_planned_zone:
        execution_reason_parts.append(
            f"чекати ціну в оптимальній зоні {round_price(zone_low)}–{round_price(zone_high)}; не доганяти"
        )

    position_risk_base = (
        RISKY_RISK_PCT
        if candidate.risk_mode == "RISKY" or candidate.execution_lane in {"EARLY_TACTICAL", "MISSED_IMPULSE_REENTRY"}
        else NORMAL_RISK_PCT
    )
    # Higher Confirmation Tiers get better position sizing
    if getattr(candidate, 'confirmation_tier', 1) >= TIER_HIGH_QUALITY:
        position_risk_base = NORMAL_RISK_PCT
    if candidate.execution_lane == "HIGH_QUALITY_CONTINUATION":
        position_risk_base = NORMAL_RISK_PCT
    position_risk = round(position_risk_base * clamp(candidate.edge_risk_multiplier, 0.35, 1.10), 3)

    min_tp1_distance = max(cfg["min_tp_atr"] * atr15, required_rr * risk)
    tp1, checkpoints = choose_target(candidate.target_levels, chosen_entry, side, min_tp1_distance)
    tp1_natural = bool(tp1)
    if not tp1:
        if candidate.setup_family in {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}:
            dr = context.get("dealing_range") or {}
            width = max(atr15, safe_float(dr.get("high")) - safe_float(dr.get("low")))
            tp1 = chosen_entry + sign * max(PREFERRED_RR1 * risk, width)
        else:
            hard_invalid = hard_invalid or "для reversal після 2R немає реальної ICT/1H ліквідності"
            tp1 = chosen_entry + sign * max(PREFERRED_RR1 * risk, 2.0 * atr15)
    rr1 = target_rr(chosen_entry, stop, tp1)
    if rr1 < required_rr - 0.02:
        optimal = optimal_entry_for_target_rr(stop, tp1, required_rr, side)
        if optimal:
            chosen_entry = optimal
            optimal_rr_entry = optimal
            risk = abs(chosen_entry - stop)
            entry_map = _merge_optimal_entry_zone(entry_map, optimal, atr15, side)
            zone_low = safe_float(entry_map["entry_zone_low"])
            zone_high = safe_float(entry_map["entry_zone_high"])
            inside_planned_zone = zone_low - tolerance <= current_price <= zone_high + tolerance
            execution_ready = False
            wait_reasons.append(f"для 2R чекати кращий вхід близько {round_price(optimal)}")
            rr1 = target_rr(chosen_entry, stop, tp1)
        else:
            hard_invalid = hard_invalid or f"не вдалося побудувати 2R до реальної цілі {round_price(tp1)}"

    later_levels = [v for v in candidate.target_levels if level_in_direction(v, tp1, side)]
    tp2, checkpoints2 = choose_target(later_levels, chosen_entry, side, max(MIN_RR2 * risk, 2.60 * atr15))
    if not tp2:
        tp2 = chosen_entry + sign * max(MIN_RR2 * risk, 2.80 * atr15)
    later_levels_3 = [v for v in candidate.target_levels if level_in_direction(v, tp2, side)]
    tp3, checkpoints3 = choose_target(later_levels_3, chosen_entry, side, max(MIN_RR3 * risk, 3.40 * atr15))
    if not tp3:
        tp3 = chosen_entry + sign * max(MIN_RR3 * risk, 3.60 * atr15)
    if side == Side.LONG.value:
        tp2 = max(tp2, tp1 + 0.70 * atr15)
        tp3 = max(tp3, tp2 + 0.90 * atr15)
    else:
        tp2 = min(tp2, tp1 - 0.70 * atr15)
        tp3 = min(tp3, tp2 - 0.90 * atr15)
    rr2 = target_rr(chosen_entry, stop, tp2)
    rr3 = target_rr(chosen_entry, stop, tp3)

    position_size = 0.0
    notional = 0.0
    if ACCOUNT_BALANCE > 0 and risk > 0:
        cash_risk = ACCOUNT_BALANCE * position_risk / 100.0
        position_size = cash_risk / risk
        notional = position_size * chosen_entry

    entry_logic = {
        "EARLY_TACTICAL": "ICT location → liquidity/displacement event → acceptance АБО retest → tactical structural stop → 2R",
        "MISSED_IMPULSE_REENTRY": "пропущений імпульс → збережена теза → новий 3M retest/hold → structural re-entry → 2R",
        "STANDARD_CONFIRMED": "ICT location → 3M trigger → acceptance → retest/hold → standard structural stop → 2R",
    }.get(candidate.execution_lane, "ICT location → trigger → structural execution → 2R")
    return TradePlan(
        entry=round_price(chosen_entry), stop=round_price(stop), tp1=round_price(tp1), tp2=round_price(tp2), tp3=round_price(tp3),
        risk_pct=round(abs(chosen_entry - stop) / chosen_entry * 100.0, 3), rr1=round(rr1, 2), rr2=round(rr2, 2), rr3=round(rr3, 2),
        position_risk_pct=position_risk, position_size=round(position_size, 6), notional=round(notional, 2),
        invalidation=(f"закриття 15M за {round_price(structural)}; 1H/4H outer boundary має пріоритет лише коли саме вона захищає тезу"),
        stop_basis=stop_basis,
        target_basis=("TP1 — реальна ICT/1H ліквідність ≥2R" if tp1_natural else "TP1 — валідована measured expansion ≥2.2R") + "; TP2/TP3 — наступна зовнішня ліквідність/3R/4R",
        checkpoints=[round_price(v) for v in (checkpoints + checkpoints2 + checkpoints3)[:8]],
        valid=not hard_invalid, reason=hard_invalid,
        better_entry=round_price(optimal_rr_entry or (0.0 if execution_ready else safe_float(entry_map["ideal_entry"]))),
        structural_invalidation=round_price(structural),
        wait_zone_low=entry_map["wait_zone_low"], wait_zone_high=entry_map["wait_zone_high"],
        entry_zone_low=entry_map["entry_zone_low"], entry_zone_high=entry_map["entry_zone_high"],
        trigger_level=entry_map["trigger_level"], trigger_condition=entry_map["trigger_condition"],
        execution_ready=execution_ready,
        execution_reason="; ".join(x for x in execution_reason_parts if x),
        stop_timeframe=stop_timeframe, stop_source_level=round_price(structural),
        htf_scenario=higher_timeframe_scenario(context, side), blocking_zone=entry_map["blocking_zone"],
        acceptance_status=entry_map["acceptance_status"], natural_tp1=tp1_natural,
        max_entry_extension_atr=entry_map["max_entry_extension_atr"],
        optimal_entry_for_rr=round_price(optimal_rr_entry), natural_target_level=round_price(nearest_target),
        entry_logic=entry_logic,
    )


def opportunity_is_valid(opportunity: Opportunity, context: dict[str, Any]) -> bool:
    price = context["price"]
    if opportunity.side == Side.LONG.value and price <= opportunity.invalidation_level:
        return False
    if opportunity.side == Side.SHORT.value and price >= opportunity.invalidation_level:
        return False
    if opportunity.status == "WAIT_PULLBACK":
        s15 = context.get("s15") or {}
        # Either a closed opposite BOS or CHOCH is enough to cancel the stored
        # thesis. Requiring both simultaneously kept broken opportunities alive.
        if s15.get("bos") == opposite(opportunity.side) or s15.get("choch") == opposite(opportunity.side):
            return False
        return True
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    anchor = opportunity.execution_anchor or opportunity.trigger_level
    if anchor and abs(price - anchor) / atr15 > 1.60:
        return False
    return True



def make_opportunity(candidate: Candidate, plan: Optional[TradePlan] = None, status: str = "ARMED") -> Opportunity:
    created = now_utc()
    expires = created.timestamp() + 24 * 60 * 60
    return Opportunity(
        side=candidate.side,
        setup_type=candidate.setup_type,
        created_at=created.isoformat(),
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        score=candidate.final_score,
        trigger_level=round_price(candidate.trigger_level),
        invalidation_level=round_price((plan.structural_invalidation if plan else 0.0) or candidate.invalidation_level),
        confirmations=candidate.confirmations[:6],
        setup_family=candidate.setup_family,
        variant=candidate.variant,
        stage=candidate.stage,
        evidence_families=candidate.evidence_families,
        execution_profile=candidate.execution_profile,
        execution_anchor=round_price(candidate.execution_anchor),
        status=status,
        execution_lane=("MISSED_IMPULSE_REENTRY" if status == "WAIT_PULLBACK" else candidate.execution_lane),
        origin_trigger_ts=int(candidate.trigger_ts or 0),
        missed_at=(created.isoformat() if status == "WAIT_PULLBACK" else ""),
        reentry_zone_low=round_price(plan.entry_zone_low if plan else 0.0),
        reentry_zone_high=round_price(plan.entry_zone_high if plan else 0.0),
        natural_target_level=round_price(plan.natural_target_level if plan else 0.0),
        optimal_entry_for_rr=round_price(plan.optimal_entry_for_rr if plan else 0.0),
    )



def choose_persisted_opportunity(
    existing: Optional[Opportunity], incoming: Opportunity, context: dict[str, Any]
) -> Opportunity:
    """Protect a valid missed-impulse thesis from weaker ARMED overwrites.

    A stored WAIT_PULLBACK survives ordinary discovered/armed candidates. It is
    replaced only by another missed-impulse thesis of equal or greater quality,
    or by a confirmed opposite 15M structural shift with a clear score margin.
    """
    if not existing or not opportunity_is_valid(existing, context):
        return incoming
    if existing.status != "WAIT_PULLBACK":
        return incoming
    if incoming.status == "WAIT_PULLBACK":
        if incoming.side == existing.side:
            return incoming if incoming.score >= existing.score else existing
        s15 = context.get("s15") or {}
        opposite_shift = s15.get("bos") == incoming.side or s15.get("choch") == incoming.side
        return incoming if opposite_shift and incoming.score >= existing.score + DIRECTION_MARGIN else existing
    if incoming.side == existing.side:
        return existing
    s15 = context.get("s15") or {}
    opposite_shift = s15.get("bos") == incoming.side or s15.get("choch") == incoming.side
    if opposite_shift and incoming.score >= existing.score + DIRECTION_MARGIN:
        return incoming
    return existing


def candidate_from_missed_opportunity(opportunity: Opportunity, context: dict[str, Any]) -> Optional[Candidate]:
    if opportunity.status != "WAIT_PULLBACK" or not opportunity_is_valid(opportunity, context):
        return None
    event = scan_event_for_side(context, opportunity.side)
    if not event or str(event.get("stage") or "") not in {"RETEST", "READY"}:
        return None
    event_ts = scan_execution_event_ts(event)
    if event_ts <= int(opportunity.origin_trigger_ts or 0):
        return None
    price = safe_float(context.get("price"))
    atr15 = max(safe_float(context["tf15"].get("atr")), price * 0.001)
    low = safe_float(opportunity.reentry_zone_low)
    high = safe_float(opportunity.reentry_zone_high)
    if low and high:
        inside = low - REENTRY_ZONE_TOLERANCE_ATR15 * atr15 <= price <= high + REENTRY_ZONE_TOLERANCE_ATR15 * atr15
        if not inside:
            return None
    components = {"location": 14, "structure": 19, "liquidity": 14, "trigger": 14, "flow": 5, "htf": 5}
    candidate = Candidate(
        side=opportunity.side,
        setup_type=opportunity.setup_type,
        raw_score=max(opportunity.score, sum(components.values())),
        final_score=max(opportunity.score, sum(components.values())),
        score_components=components,
        trigger_ready=True,
        trigger_level=safe_float(event.get("trigger_level") or opportunity.trigger_level),
        invalidation_level=opportunity.invalidation_level,
        target_levels=context["targets_long" if opportunity.side == Side.LONG.value else "targets_short"],
        confirmations=list(opportunity.confirmations[:4]) + ["новий 3M retest/hold після пропущеного імпульсу"],
        risks=[],
        risk_mode="RISKY",
        setup_family=opportunity.setup_family,
        variant="MISSED_IMPULSE_RETEST",
        stage="EXECUTABLE",
        evidence_families=list(dict.fromkeys(opportunity.evidence_families + ["EXECUTION_TRIGGER"])),
        trigger_ts=event_ts,
        execution_profile=opportunity.execution_profile or "TACTICAL",
        specificity=60,
        execution_anchor=safe_float(event.get("event_price") or price),
        trigger_age_minutes=0.0,
        execution_lane="MISSED_IMPULSE_REENTRY",
        scan_event_stage=str(event.get("stage") or ""),
    )
    return finalize_candidate(context, candidate)


def execution_lane_snapshot(candidate: Candidate, plan: Optional[TradePlan]) -> dict[str, Any]:
    lane = candidate.execution_lane or classify_execution_lane(candidate)
    evidence = len(candidate.evidence_families)
    full_threshold, _ = setup_thresholds(candidate)
    if lane == "EARLY_TACTICAL":
        threshold = max(EARLY_TACTICAL_SCORE, min(full_threshold - 8, 68))
        ready = bool(candidate.trigger_ready and candidate.final_score >= threshold and evidence >= 3 and plan and plan.valid and plan.execution_ready)
        return {"lane": lane, "threshold": threshold, "evidence_required": 3, "ready": ready, "action": Action.RISKY_ENTRY.value}
    if lane == "MISSED_IMPULSE_REENTRY":
        threshold = MISSED_REENTRY_SCORE
        ready = bool(candidate.trigger_ready and candidate.final_score >= threshold and evidence >= 3 and plan and plan.valid and plan.execution_ready)
        return {"lane": lane, "threshold": threshold, "evidence_required": 3, "ready": ready, "action": Action.RISKY_ENTRY.value}
    threshold = max(STANDARD_CONFIRMED_SCORE, min(full_threshold, 76))
    ready = bool(candidate.trigger_ready and candidate.final_score >= threshold and evidence >= 4 and plan and plan.valid and plan.execution_ready)
    return {"lane": "STANDARD_CONFIRMED", "threshold": threshold, "evidence_required": 4, "ready": ready, "action": Action.ENTRY.value}


def missed_impulse_status(candidate: Candidate, plan: Optional[TradePlan]) -> bool:
    if not plan or not plan.valid or candidate.final_score < ARMED_SCORE or not candidate.trigger_ready:
        return False
    text = f"{plan.execution_reason} {plan.reason}".lower()
    return bool(
        not plan.execution_ready
        and any(token in text for token in ("не наздоганяємо", "поза робочою зоною", "чекати ціну", "кращий вхід", "оптимальній зоні"))
    )

def candidate_is_executable(candidate: Candidate, plan: Optional[TradePlan] = None) -> bool:
    return bool(execution_lane_snapshot(candidate, plan).get("ready"))



def evaluate_new_setup(context: dict[str, Any], state: dict[str, Any], journal: Optional[dict[str, Any]] = None) -> Decision:
    candidates = [apply_setup_edge_feedback(c, context, journal) for c in build_candidates(context)]
    saved = opportunity_from_state(state)
    if saved and saved.status == "WAIT_PULLBACK":
        recovered = candidate_from_missed_opportunity(saved, context)
        if recovered:
            candidates.append(recovered)
            candidates = collapse_candidates(candidates)

    viable = [c for c in candidates if not c.hard_reject_reason]
    planned: list[tuple[Candidate, TradePlan]] = []
    for candidate in viable:
        try:
            candidate.execution_lane = classify_execution_lane(candidate) if candidate.execution_lane != "MISSED_IMPULSE_REENTRY" else candidate.execution_lane
            planned.append((candidate, build_trade_plan(context, candidate)))
        except Exception as exc:
            candidate.risks.append(f"помилка побудови плану: {exc}")

    audit_candidates = []
    for c in candidates:
        plan = next((p for cc, p in planned if cc is c), None)
        lane_info = execution_lane_snapshot(c, plan)
        audit_candidates.append({
            "side": c.side, "setup_type": c.setup_type, "setup_family": c.setup_family, "variant": c.variant,
            "stage": c.stage, "execution_lane": lane_info["lane"], "lane_threshold": lane_info["threshold"],
            "raw_score": c.raw_score, "final_score": c.final_score,
            "selection_score": round(candidate_selection_score(c), 2), "trigger_ready": c.trigger_ready,
            "risk_mode": c.risk_mode, "evidence_families": c.evidence_families,
            "evidence_count": len(c.evidence_families), "late_extension_atr": c.late_extension_atr,
            "execution_anchor": c.execution_anchor, "trigger_level": c.trigger_level,
            "trigger_age_minutes": c.trigger_age_minutes, "scan_event_stage": c.scan_event_stage,
            "hard_reject_reason": c.hard_reject_reason, "score_components": c.score_components,
            "geometry_valid": bool(plan and plan.valid), "execution_ready": bool(plan and plan.execution_ready),
            "lane_ready": bool(lane_info["ready"]), "geometry_reason": plan.reason if plan else "",
            "blocking_zone": plan.blocking_zone if plan else "", "acceptance_status": plan.acceptance_status if plan else "",
        })

    entry_pool: list[tuple[Candidate, TradePlan, dict[str, Any]]] = []
    for candidate, plan in planned:
        reentry_guard = event_driven_reentry_guard(state, context, candidate)
        if reentry_guard.get("blocked"):
            candidate.risks.append(reentry_guard["reason"])
            continue
        lane_info = execution_lane_snapshot(candidate, plan)
        if lane_info["ready"]:
            entry_pool.append((candidate, plan, lane_info))

    if entry_pool:
        best, plan, lane_info = max(entry_pool, key=lambda pair: candidate_selection_score(pair[0]))
    elif planned:
        best, plan = max(planned, key=lambda pair: candidate_selection_score(pair[0]))
        lane_info = execution_lane_snapshot(best, plan)
    else:
        best, plan, lane_info = None, None, {"lane": "", "threshold": 0, "ready": False, "action": Action.ARMED.value}

    if best is None or best.final_score < ARMED_SCORE:
        if saved and opportunity_is_valid(saved, context):
            return Decision(
                id=uuid.uuid4().hex[:12], time=iso_now(), action=Action.ARMED.value,
                side=saved.side, setup_type=saved.setup_type, quality=saved.score,
                reason=("пропущений імпульс збережено; очікується новий 3M retest/hold у зоні повторного входу"
                        if saved.status == "WAIT_PULLBACK" else
                        f"{saved.variant or saved.setup_type} ще чинний, але нового execution-trigger немає"),
                regime=context["regime"], audit={"candidates": audit_candidates, "opportunity_memory": asdict(saved)}
            )
        return Decision(
            id=uuid.uuid4().hex[:12], time=iso_now(), action=Action.NO_SETUP.value,
            side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value, quality=best.final_score if best else 0,
            reason="ринок не сформував професійний ICT execution-package",
            regime=context["regime"], audit={"candidates": audit_candidates}
        )

    executable_opposite = [(c, p, info) for c, p, info in entry_pool if c.side == opposite(best.side)]
    strongest_opposite = max(executable_opposite, key=lambda pair: candidate_selection_score(pair[0]), default=None)
    if strongest_opposite:
        margin = candidate_selection_score(best) - candidate_selection_score(strongest_opposite[0])
        if margin < DIRECTION_MARGIN:
            return Decision(
                id=uuid.uuid4().hex[:12], time=iso_now(), action=Action.NO_SETUP.value,
                side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value,
                quality=max(best.final_score, strongest_opposite[0].final_score),
                reason=f"два виконувані напрями мають недостатню перевагу: {best.side} проти {strongest_opposite[0].side}",
                regime=context["regime"], audit={"candidates": audit_candidates, "direction_conflict": True}
            )

    action = lane_info.get("action", Action.ARMED.value) if lane_info.get("ready") else Action.ARMED.value
    if action in {Action.ENTRY.value, Action.RISKY_ENTRY.value}:
        reason = {
            "EARLY_TACTICAL": "ранній тактичний структурний вхід: ICT-подія підтверджена acceptance або retest, стоп за реальною структурою",
            "STANDARD_CONFIRMED": "підтверджений стандартний вхід: trigger, acceptance, retest і 2R-геометрія узгоджені",
            "MISSED_IMPULSE_REENTRY": "повторний вхід: пропущений імпульс збережено, новий 3M retest/hold підтвердив execution",
        }.get(lane_info.get("lane"), "професійний ICT-вхід підтверджено")
    elif plan and missed_impulse_status(best, plan):
        reason = "сильний імпульс уже відійшов; тезу збережено для повторного входу після нового 3M ретесту"
    elif not best.trigger_ready:
        reason = f"{SETUP_LABELS.get(best.setup_type, best.setup_type)} сформовано; потрібен свіжий закритий 3M trigger"
    elif plan and not plan.valid:
        reason = f"сетап підтверджений, але структурна геометрія ще не готова: {plan.reason}"
    elif plan and not plan.execution_ready:
        reason = f"сетап сформований; зараз не входити: {plan.execution_reason}"
    else:
        reason = f"якість {best.final_score}; очікується виконання маршруту {lane_info.get('lane')}"

    return Decision(
        id=uuid.uuid4().hex[:12], time=iso_now(), action=action, side=best.side,
        setup_type=best.setup_type, quality=best.final_score, reason=reason,
        regime=context["regime"], candidate=best, plan=plan,
        audit={
            "candidates": audit_candidates,
            "selected": {
                "side": best.side, "setup_type": best.setup_type, "setup_family": best.setup_family,
                "variant": best.variant, "stage": best.stage, "score": best.final_score,
                "execution_lane": lane_info.get("lane"), "lane_threshold": lane_info.get("threshold"),
                "evidence_count": len(best.evidence_families), "missed_impulse": missed_impulse_status(best, plan),
            },
            "scan_3m": context.get("scan_3m") or {},
            "invariants": {
                "all_nine_independent_detectors": True,
                "single_selector": True,
                "full_sequential_3m_scan": True,
                "early_tactical_lane": True,
                "standard_confirmed_lane": True,
                "missed_impulse_reentry_lane": True,
                "event_driven_reentry_no_time_cooldown": True,
                "target_derived_optimal_entry": True,
                "adaptive_expectancy_feedback": True,
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
        structural_invalidation=plan.structural_invalidation or decision.candidate.invalidation_level,
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
            f"FAMILY:{decision.candidate.setup_family}",
            f"VARIANT:{decision.candidate.variant}",
            f"EVIDENCE:{','.join(decision.candidate.evidence_families)}",
            f"RR:{plan.rr1}/{plan.rr2}/{plan.rr3}",
        ],
        setup_family=decision.candidate.setup_family,
        variant=decision.candidate.variant,
        execution_profile=decision.candidate.execution_profile,
        trigger_level=plan.trigger_level,
        entry_zone_low=plan.entry_zone_low,
        entry_zone_high=plan.entry_zone_high,
        initial_atr15=max(0.0, abs(plan.entry - plan.stop) / max(plan.rr1 / 2.0, 1.0)),
        opened_regime=decision.regime,
        failure_checks=0,
        initial_risk=abs(plan.entry - plan.stop),
        thesis_key=(decision.candidate.thesis_key or "|".join([decision.side, decision.candidate.setup_family, decision.setup_type, str(plan.trigger_level), str(plan.structural_invalidation)])),
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
    c3 = closed(context["candles"]["3m"])
    if len(c15) < 2 or len(c3) < 3:
        return False
    if trade.side == Side.LONG.value:
        closed_break = c15[-1].close < trade.structural_invalidation and c15[-2].close < trade.structural_invalidation
        trigger_lost = sum(c.close < trade.trigger_level for c in c3[-3:]) >= 2 if trade.trigger_level else False
    else:
        closed_break = c15[-1].close > trade.structural_invalidation and c15[-2].close > trade.structural_invalidation
        trigger_lost = sum(c.close > trade.trigger_level for c in c3[-3:]) >= 2 if trade.trigger_level else False
    fast_against = context["s3"].get("bias") == opposite(trade.side)
    if trade.setup_family in {SetupFamily.EXPANSION.value, SetupFamily.STRUCTURAL_TRANSITION.value}:
        return bool((closed_break and fast_against) or (trigger_lost and context["s15"].get("bias") == opposite(trade.side)))
    return bool(closed_break and fast_against)


def failed_entry_snapshot(trade: ActiveTrade, context: dict[str, Any]) -> dict[str, Any]:
    """Event/candle-based failure, not a wall-clock timeout or entry cooldown."""
    opened = parse_iso(trade.opened_at)
    opened_ms = int(opened.timestamp() * 1000) if opened else 0
    c15_all = closed(context["candles"]["15m"])
    post_15m = [c for c in c15_all if c.ts > opened_ms]
    bars15 = len(post_15m)
    mfe_r = trade_r_multiple(trade, trade.best_price)
    c3 = closed(context["candles"]["3m"])[-4:]
    if trade.side == Side.LONG.value:
        trigger_lost = bool(trade.trigger_level and sum(c.close < trade.trigger_level for c in c3) >= 2)
    else:
        trigger_lost = bool(trade.trigger_level and sum(c.close > trade.trigger_level for c in c3) >= 2)
    adverse = 0
    if context["tf3"].get("bias") == opposite(trade.side): adverse += 1
    if context["flow"].get("bias") == opposite(trade.side) or context["flow"].get("absorption_bias") == opposite(trade.side): adverse += 1
    if context["s15"].get("choch") == opposite(trade.side) or context["s15"].get("bos") == opposite(trade.side): adverse += 1
    required_bars = 2 if trade.setup_family in {SetupFamily.EXPANSION.value, SetupFamily.STRUCTURAL_TRANSITION.value} else 3
    failed = bool(bars15 >= required_bars and mfe_r < FAILED_ENTRY_MIN_MFE_R and trigger_lost and adverse >= 2)
    return {
        "failed": failed,
        "post_entry_15m_bars": bars15,
        "required_bars": required_bars,
        "mfe_r": mfe_r,
        "trigger_lost": trigger_lost,
        "adverse": adverse,
        "reason": (
            f"після {bars15} закритих 15M свічок угода не дала {FAILED_ENTRY_MIN_MFE_R:.2f}R, "
            f"втратила trigger і має {adverse} незалежні шари проти"
        ),
    }


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

    failed_entry = failed_entry_snapshot(trade, context)
    if not trade.tp1_hit and failed_entry.get("failed"):
        trade.failure_checks += 1
        return close_result(trade, Action.EXIT.value, current_price, f"failed expansion/entry guard: {failed_entry['reason']}", context)

    if thesis_broken(trade, context):
        return close_result(trade, Action.EXIT.value, current_price, "setup-aware ICT thesis broken: 15M/trigger structure підтвердила напрям проти", context)

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
    exit_price = safe_float(result.get("exit_price") or result.get("current_price"))
    initial_risk = trade.initial_risk or abs(trade.entry - trade.stop_initial)
    result_r = side_sign(trade.side) * (exit_price - trade.entry) / initial_risk if initial_risk > 0 else 0.0
    mfe_r = side_sign(trade.side) * (trade.best_price - trade.entry) / initial_risk if initial_risk > 0 else 0.0
    mae_r = abs(side_sign(trade.side) * (trade.worst_price - trade.entry) / initial_risk) if initial_risk > 0 else 0.0
    return {
        "id": trade.id,
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "opened_at": trade.opened_at,
        "closed_at": iso_now(),
        "side": trade.side,
        "setup_type": trade.setup_type,
        "setup_family": trade.setup_family,
        "variant": trade.variant,
        "execution_profile": trade.execution_profile,
        "regime": trade.opened_regime,
        "thesis_key": trade.thesis_key,
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
        "result_r": round(result_r, 3),
        "mfe_r": round(mfe_r, 3),
        "mae_r": round(mae_r, 3),
        "leveraged_pct": result.get("leveraged_pct"),
        "mfe_pct": result.get("mfe_pct"),
        "mae_pct": result.get("mae_pct"),
        "position_risk_pct": trade.position_risk_pct,
        "reason": result.get("reason"),
    }


def _analytics_bucket_add(store: dict[str, dict[str, Any]], key: str, trade: dict[str, Any]) -> None:
    bucket = store.setdefault(key or "UNKNOWN", {"trades": 0, "wins": 0, "result_pct": 0.0, "mfe_pct": 0.0, "result_r": 0.0, "gross_win_r": 0.0, "gross_loss_r": 0.0})
    result = safe_float(trade.get("result_pct"))
    bucket["trades"] += 1
    bucket["wins"] += int(result > 0)
    bucket["result_pct"] += result
    bucket["mfe_pct"] += safe_float(trade.get("mfe_pct"))
    result_r = _trade_result_r(trade)
    bucket["result_r"] += result_r
    if result_r > 0:
        bucket["gross_win_r"] += result_r
    elif result_r < 0:
        bucket["gross_loss_r"] += abs(result_r)


def _analytics_finalize(store: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for bucket in store.values():
        trades = int(bucket.get("trades", 0) or 0)
        bucket["win_rate"] = round(bucket["wins"] / trades * 100, 1) if trades else 0.0
        bucket["result_pct"] = round(bucket["result_pct"], 3)
        bucket["avg_mfe_pct"] = round(bucket.pop("mfe_pct") / trades, 3) if trades else 0.0
        bucket["expectancy_r"] = round(bucket.pop("result_r") / trades, 3) if trades else 0.0
        gross_win = bucket.pop("gross_win_r")
        gross_loss = bucket.pop("gross_loss_r")
        bucket["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    return store


def compute_analytics(journal: dict[str, Any]) -> dict[str, Any]:
    trades = list(journal.get("trades") or [])
    closed_trades = [t for t in trades if t.get("result_pct") is not None]
    wins = [t for t in closed_trades if safe_float(t.get("result_pct")) > 0]
    losses = [t for t in closed_trades if safe_float(t.get("result_pct")) < 0]
    total_pct = sum(safe_float(t.get("result_pct")) for t in closed_trades)
    leveraged = sum(safe_float(t.get("leveraged_pct")) for t in closed_trades)
    total_r = sum(_trade_result_r(t) for t in closed_trades)
    gross_win_r = sum(_trade_result_r(t) for t in closed_trades if _trade_result_r(t) > 0)
    gross_loss_r = abs(sum(_trade_result_r(t) for t in closed_trades if _trade_result_r(t) < 0))
    by_setup: dict[str, dict[str, Any]] = {}
    by_family: dict[str, dict[str, Any]] = {}
    by_variant: dict[str, dict[str, Any]] = {}
    for trade in closed_trades:
        _analytics_bucket_add(by_setup, str(trade.get("setup_type") or "UNKNOWN"), trade)
        _analytics_bucket_add(by_family, str(trade.get("setup_family") or SETUP_FAMILY.get(str(trade.get("setup_type") or ""), "UNKNOWN")), trade)
        _analytics_bucket_add(by_variant, str(trade.get("variant") or trade.get("setup_type") or "UNKNOWN"), trade)
    return {
        "closed_trades": len(closed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0.0,
        "net_result_pct": round(total_pct, 3),
        "net_leveraged_pct": round(leveraged, 3),
        "net_r": round(total_r, 3),
        "expectancy_r": round(total_r / len(closed_trades), 3) if closed_trades else 0.0,
        "profit_factor_r": round(gross_win_r / gross_loss_r, 2) if gross_loss_r > 0 else (99.0 if gross_win_r > 0 else 0.0),
        "by_setup": _analytics_finalize(by_setup),
        "by_family": _analytics_finalize(by_family),
        "by_variant": _analytics_finalize(by_variant),
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
        Action.ARMED.value: "ЗОНА ОЧІКУВАННЯ — ВХІД ЩЕ НЕ АКТИВНИЙ",
        Action.NO_SETUP.value: "ЧИСТОГО СЕТАПУ НЕМАЄ",
        Action.HOLD.value: "УТРИМУВАТИ",
        Action.PROTECT.value: "ЗАХИСТ ПРИБУТКУ",
        Action.TP1.value: "TP1 ДОСЯГНУТО",
        Action.TP2.value: "TP2 ДОСЯГНУТО",
        Action.TP3.value: "TP3 ДОСЯГНУТО",
        Action.STOP.value: "СТОП",
        Action.EXIT.value: "ВИХІД ЗА ЗЛАМОМ ТЕЗИ",
    }.get(action, action)


def candidate_reason_lines(candidate: Optional[Candidate], limit: int = 4) -> list[str]:
    if not candidate:
        return []
    return [f"✅ {item}" for item in candidate.confirmations[:limit]]


def build_decision_message(context: dict[str, Any], decision: Decision) -> str:
    title = f"{side_icon(decision.side)} {decision.side if decision.side != Side.NEUTRAL.value else ''} — {action_label(decision.action)}".replace("  ", " ")
    lines = ["<b>BZU SIGNAL BOT — PROFESSIONAL ICT</b>", "", f"<b>{html.escape(title)}</b>"]

    if decision.side != Side.NEUTRAL.value:
        lines.extend([
            f"Сетап: {html.escape(SETUP_LABELS.get(decision.setup_type, decision.setup_type))}",
            f"Незалежні докази: {len(decision.candidate.evidence_families) if decision.candidate else 0}",
            f"Ціна: {round_price(context['price'])}",
            f"Якість: {decision.quality}/100",
            f"Режим: {html.escape(regime_label(decision.regime))}",
        ])
    else:
        lines.extend([
            f"Ціна: {round_price(context['price'])}",
            f"Режим: {html.escape(regime_label(decision.regime))}",
        ])

    lines.extend(["", f"Причина: {html.escape(ua_service_text(decision.reason))}"])
    if decision.candidate:
        reasons = candidate_reason_lines(decision.candidate)
        if reasons:
            tier = getattr(decision.candidate, 'confirmation_tier', 1)
            tier_name = {1: "Micro", 2: "Standard", 3: "High Quality", 4: "Premium"}.get(tier, "Standard")
            tier_emoji = {1: "🔹", 2: "🔸", 3: "⭐", 4: "🌟"}.get(tier, "🔸")
            lines.extend(["", f"Рівень підтвердження: {tier_emoji} Tier {tier} — {tier_name}"])
            lines.extend(["Підтвердження:", *[html.escape(x) for x in reasons]])

    if decision.plan and decision.action in {Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.ARMED.value}:
        p = decision.plan
        lines.extend([
            "",
            "<b>План:</b>",
            f"Вхід: {p.entry}",
            f"Стоп: {p.stop} ({p.risk_pct}%)",
            f"TP1: {p.tp1} — {p.rr1}R",
            f"TP2: {p.tp2} — {p.rr2}R",
            f"TP3: {p.tp3} — {p.rr3}R",
            f"Ризик позиції: {p.position_risk_pct}%",
        ])
    return clean_telegram_message("\n".join(lines))



def build_follow_message(trade: ActiveTrade, result: dict[str, Any]) -> str:
    lines = [
        "<b>BZU SIGNAL BOT — PROFESSIONAL ICT</b>",
        "",
        f"<b>{side_icon(trade.side)} {trade.side} — {html.escape(action_label(str(result.get('action'))))}</b>",
        f"Сетап: {html.escape(SETUP_LABELS.get(trade.setup_type, trade.setup_type))}",
        f"Ціна: {result.get('exit_price') or result.get('current_price')}",
        f"Результат: {result.get('current_pct', 0)}% | з плечем {result.get('leveraged_pct', 0)}%",
        f"MFE: {result.get('mfe_pct', 0)}%",
        "",
        f"Дія: {html.escape(ua_service_text(str(result.get('reason') or '')))}",
    ]
    if not result.get("closed"):
        lines.extend([
            f"Стоп: {result.get('stop_current')}",
            f"TP1/TP2/TP3: {trade.tp1} / {trade.tp2} / {trade.tp3}",
        ])
    return clean_telegram_message("\n".join(lines))



def message_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def should_send_message(state: dict[str, Any], key: str, force: bool = False) -> bool:
    # This function is intentionally side-effect free. The delivery key is
    # persisted only after Telegram confirms a successful send. Otherwise a
    # temporary API/network error would suppress the retry on the next run.
    if force or TELEGRAM_NOTIFY_EVERY_RUN or SEND_DUPLICATE_STATUS:
        return True
    return state.get("last_message_key") != key


def mark_message_sent(state: dict[str, Any], key: str) -> None:
    state["last_message_key"] = key
    state["last_telegram_success_at"] = iso_now()
    state["last_telegram_error"] = ""


def telegram_chunks(text: str, limit: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    # Messages are built line-by-line and every HTML tag is closed on the same
    # line, so splitting only between lines keeps HTML valid in each chunk.
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        addition = len(line) + (1 if current else 0)
        if current and size + addition > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                size = 0
            for start in range(0, len(line), limit):
                chunks.append(line[start:start + limit])
            continue
        current.append(line)
        size += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def clean_telegram_message(text: str) -> str:
    """Last delivery guard: keep audit fields internally, hide them in Telegram."""
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
        "RANGE": "ДІАПАЗОН", "TREND": "ТРЕНД", "TRANSITION": "ПЕРЕХІДНИЙ РЕЖИМ",
        "SHOCK": "ІМПУЛЬСНИЙ РЕЖИМ", "NORMAL": "ЗВИЧАЙНИЙ РИНОК",
    }
    for raw, translated in replacements.items():
        message = message.replace(raw, translated)
    while "\n\n\n" in message:
        message = message.replace("\n\n\n", "\n\n")
    return message.strip()

def plain_telegram_text(text: str) -> str:
    return html.unescape(text.replace("<b>", "").replace("</b>", ""))


def send_telegram(text: str) -> bool:
    text = clean_telegram_message(text)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        missing = []
        if not TELEGRAM_TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        print(f"[ERROR] Telegram credentials absent: {', '.join(missing)}")
        print(plain_telegram_text(text))
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = telegram_chunks(text)
    for index, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            print(f"[ERROR] Telegram exception on chunk {index}/{len(chunks)}: {exc}")
            return False

        if response.ok:
            print(f"Telegram status: {response.status_code} chunk {index}/{len(chunks)}")
            continue

        # Telegram may reject malformed HTML. Retry the same chunk as plain
        # text before declaring delivery failure.
        print(f"[WARN] Telegram HTML failed {response.status_code}: {response.text[:300]}")
        fallback = dict(payload)
        fallback.pop("parse_mode", None)
        fallback["text"] = plain_telegram_text(chunk)
        try:
            retry = requests.post(url, json=fallback, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            print(f"[ERROR] Telegram plain-text retry exception: {exc}")
            return False
        if not retry.ok:
            print(f"[ERROR] Telegram retry failed {retry.status_code}: {retry.text[:300]}")
            return False
        print(f"Telegram fallback status: {retry.status_code} chunk {index}/{len(chunks)}")
    return True


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
        "zones": [asdict(z) for z in context["zones"][-28:]],
        "scan_3m": context.get("scan_3m") or {},
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
    print(
        "Telegram delivery: "
        f"token={'OK' if TELEGRAM_TOKEN else 'MISSING'}, "
        f"chat_id={'OK' if TELEGRAM_CHAT_ID else 'MISSING'}, "
        f"every_run={TELEGRAM_NOTIFY_EVERY_RUN}, "
        f"no_setup={SEND_NO_SETUP}, duplicates={SEND_DUPLICATE_STATUS}"
    )
    state = load_state()
    journal = load_journal()
    data = collect_market_data()
    context = build_context(data)
    scan_closed_3m_sequence(state, context)
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
            "setup_type": active.setup_type,
            "setup_family": active.setup_family,
            "variant": active.variant,
            "trigger_level": active.trigger_level,
            "structural_invalidation": active.structural_invalidation,
            "opened_at": active.opened_at,
            "thesis_key": active.thesis_key,
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
            if send_telegram(build_follow_message(active, result)):
                mark_message_sent(state, key)
            else:
                state["last_telegram_error"] = "FOLLOW_DELIVERY_FAILED"
        save_state(state)
        save_journal(journal)
        print("BOT COMPLETE: ACTIVE TRADE")
        return

    decision = evaluate_new_setup(context, state, journal)
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
        status = "WAIT_PULLBACK" if missed_impulse_status(decision.candidate, decision.plan) else "ARMED"
        incoming = make_opportunity(decision.candidate, decision.plan, status=status)
        existing = opportunity_from_state(state)
        state["opportunity"] = asdict(choose_persisted_opportunity(existing, incoming, context))
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
    send_allowed = decision.action != Action.NO_SETUP.value or SEND_NO_SETUP or TELEGRAM_NOTIFY_EVERY_RUN
    if send_allowed and should_send_message(state, key, force=force):
        if send_telegram(build_decision_message(context, decision)):
            mark_message_sent(state, key)
        else:
            state["last_telegram_error"] = "DECISION_DELIVERY_FAILED"

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
    def flat_candles(price: float, count: int, interval_ms: int) -> list[Candle]:
        start = int(now_utc().timestamp() * 1000) - count * interval_ms
        return [Candle(start + i * interval_ms, price, price + 0.08, price - 0.08, price, 100 + i, True) for i in range(count)]

    c3 = flat_candles(100.0, 70, 180_000)
    c15 = flat_candles(100.0, 70, 900_000)
    c1h = flat_candles(100.0, 60, 3_600_000)
    c4h = flat_candles(100.0, 50, 14_400_000)
    base_context = {
        "price": 100.0,
        "tf3": {"atr": 0.25, "bias": Side.LONG.value, "score": 30},
        "tf15": {"atr": 1.0, "bias": Side.LONG.value},
        "tf1h": {"atr": 2.0, "bias": Side.LONG.value},
        "tf4h": {"atr": 4.0, "bias": Side.NEUTRAL.value},
        "flow": {"bias": Side.LONG.value, "absorption_bias": Side.NEUTRAL.value},
        "regime": Regime.TREND.value,
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "s3": {"bias": Side.LONG.value, "bos": Side.NEUTRAL.value, "choch": Side.NEUTRAL.value},
        "s15": {"bias": Side.LONG.value, "bos": Side.NEUTRAL.value, "choch": Side.NEUTRAL.value,
                 "swing_low": 98.8, "swing_high": 101.0, "recent_lows": [98.8, 99.1], "recent_highs": [101.0]},
        "s1h": {"bias": Side.LONG.value, "bos": Side.NEUTRAL.value, "choch": Side.NEUTRAL.value,
                 "swing_low": 97.5, "swing_high": 103.0, "recent_lows": [97.5], "recent_highs": [103.0]},
        "dealing_range": {"low": 98.8, "high": 103.0, "equilibrium": 100.9},
        "zones": [Zone("OB", Side.LONG.value, 98.9, 99.2, c15[-8].ts, "15m", 1.4, False)],
    }
    candidate = Candidate(
        side=Side.LONG.value, setup_type=SetupType.PULLBACK_CONTINUATION.value,
        raw_score=82, final_score=82, score_components={"location": 14, "structure": 20, "liquidity": 14, "trigger": 14, "flow": 10, "htf": 5},
        trigger_ready=True, trigger_level=99.9, invalidation_level=99.75,
        target_levels=[102.5, 103.5, 105.0], setup_family=SetupFamily.CONTINUATION.value,
        variant="FULL_PULLBACK", execution_profile="STANDARD", execution_anchor=99.9,
    )
    plan = build_trade_plan(base_context, candidate)
    assert plan.valid, plan
    assert plan.stop_timeframe == "15M", plan
    assert plan.stop < 98.9, "stop must be behind the 15M zone, not inside it"
    assert abs(plan.entry - plan.stop) >= 1.10, plan
    assert plan.rr1 >= 2.0 and plan.tp1 < plan.tp2 < plan.tp3, plan

    # Opposing 15M/1H zone: trigger alone cannot open a position inside it.
    blocked_context = dict(base_context)
    blocked_context["zones"] = list(base_context["zones"]) + [Zone("FVG", Side.SHORT.value, 99.85, 100.25, c15[-5].ts, "15m", 1.2, False)]
    blocked_plan = build_trade_plan(blocked_context, candidate)
    assert not blocked_plan.execution_ready and blocked_plan.blocking_zone, blocked_plan

    # Late entry is measured from the structural trigger, not the newest event candle.
    late_context = dict(base_context)
    late_context["price"] = 100.75
    late_candidate = Candidate(**{**asdict(candidate), "trigger_level": 100.0, "execution_anchor": 100.7})
    late_plan = build_trade_plan(late_context, late_candidate)
    assert not late_plan.execution_ready and "не наздоганяємо" in late_plan.execution_reason, late_plan

    # RANGE micro-break remains ARMED until 15M acceptance plus retest.
    range_context = dict(base_context)
    range_context["regime"] = Regime.RANGE.value
    range_context["s15"] = dict(base_context["s15"], bos=Side.NEUTRAL.value)
    breakout_candidate = Candidate(
        side=Side.LONG.value, setup_type=SetupType.RANGE_COMPRESSION_BREAKOUT.value,
        raw_score=75, final_score=75, score_components={"location": 12, "structure": 19, "liquidity": 16, "trigger": 14, "flow": 9, "htf": 5},
        trigger_ready=True, trigger_level=100.0, invalidation_level=98.8, target_levels=[103.0, 104.0],
        setup_family=SetupFamily.EXPANSION.value, variant="EARLY_MICRO_COMPRESSION_BREAK",
        execution_profile="TACTICAL", execution_anchor=100.0,
    )
    range_plan = build_trade_plan(range_context, breakout_candidate)
    assert not range_plan.execution_ready, range_plan
    assert "потрібне прийняття" in range_plan.execution_reason, range_plan

    # Failed-entry guard exits a breakout that never expands and loses its trigger.
    lost_c3 = c3[:-4] + [
        Candle(c3[-4].ts, 100.0, 100.05, 99.85, 99.90, 150, True),
        Candle(c3[-3].ts, 99.90, 99.95, 99.75, 99.82, 160, True),
        Candle(c3[-2].ts, 99.82, 99.88, 99.70, 99.78, 170, True),
        Candle(c3[-1].ts, 99.78, 99.84, 99.65, 99.72, 180, True),
    ]
    old_open = datetime.fromtimestamp((lost_c3[-1].ts / 1000) - 3600, tz=timezone.utc).isoformat()
    failed_trade = ActiveTrade(
        id="failed", side=Side.LONG.value, setup_type=SetupType.RANGE_COMPRESSION_BREAKOUT.value,
        opened_at=old_open, entry=100.0, stop_initial=98.8, stop_current=98.8,
        structural_invalidation=98.9, tp1=102.4, tp2=103.6, tp3=104.8,
        quality=75, position_risk_pct=0.25, best_price=100.1, worst_price=99.7,
        setup_family=SetupFamily.EXPANSION.value, variant="EARLY_MICRO_COMPRESSION_BREAK",
        execution_profile="TACTICAL", trigger_level=100.0,
    )
    failed_context = dict(base_context)
    failed_context["price"] = 99.72
    failed_context["candles"] = {"3m": lost_c3, "15m": c15, "1h": c1h, "4h": c4h}
    failed_context["tf3"] = {"bias": Side.SHORT.value, "score": -35}
    failed_context["flow"] = {"bias": Side.SHORT.value, "absorption_bias": Side.NEUTRAL.value}
    snap = failed_entry_snapshot(failed_trade, failed_context)
    assert snap["failed"], snap

    # Management: no pre-TP1 stop tightening, one stop lock after TP1.
    opened = datetime.fromtimestamp((c3[-3].ts - 60_000) / 1000, tz=timezone.utc).isoformat()
    trade = ActiveTrade(id="selftest", side=Side.LONG.value, setup_type=SetupType.PULLBACK_CONTINUATION.value,
        opened_at=opened, entry=100.0, stop_initial=98.8, stop_current=98.8, structural_invalidation=98.9,
        tp1=102.4, tp2=103.6, tp3=104.8, quality=80, position_risk_pct=0.5,
        best_price=100.0, worst_price=100.0, setup_family=SetupFamily.CONTINUATION.value,
        trigger_level=99.9)
    quiet_context = dict(base_context)
    quiet_context["price"] = 100.4
    quiet_context["candles"] = {"3m": c3[-2:], "15m": c15[-5:], "1h": c1h, "4h": c4h}
    first = manage_active_trade(trade, quiet_context)
    assert not first["closed"] and trade.stop_current == 98.8
    tp_candle = Candle(ts=trade.last_checked_3m_ts + 180_000, open=100.4, high=102.5, low=100.2, close=102.3, volume=150)
    tp_context = dict(quiet_context)
    tp_context["price"] = 102.3
    tp_context["candles"] = {"3m": c3[-8:] + [tp_candle], "15m": c15[-8:], "1h": c1h, "4h": c4h}
    second = manage_active_trade(trade, tp_context)
    assert not second["closed"] and trade.tp1_hit and trade.tp1_stop_locked
    locked_stop = trade.stop_current
    manage_active_trade(trade, tp_context)
    assert trade.stop_current == locked_stop

    # Central 3M scanner retains a valid event and drops it after invalidation.
    start = 1_780_000_000_000
    trigger_candles = [Candle(start + i * 180_000, 100.0, 100.05, 99.95, 100.0, 100, True) for i in range(18)]
    trigger_candles.extend([
        Candle(start + 18 * 180_000, 100.0, 100.35, 99.98, 100.30, 200, True),
        Candle(start + 19 * 180_000, 100.30, 100.62, 100.26, 100.58, 210, True),
        Candle(start + 20 * 180_000, 100.58, 100.64, 100.50, 100.55, 150, True),
        Candle(start + 21 * 180_000, 100.55, 100.63, 100.51, 100.59, 140, True),
    ])
    scanned = trigger_snapshot(trigger_candles, Side.LONG.value, 100.05)
    assert scanned.get("ready") and scanned.get("age_bars") == 2, scanned
    invalidated = trigger_snapshot(trigger_candles + [Candle(start + 22 * 180_000, 100.59, 100.61, 99.70, 99.80, 300, True)], Side.LONG.value, 100.05)
    assert not invalidated.get("ready"), invalidated

    low_rank = Candidate(side=Side.LONG.value, setup_type=SetupType.SWEEP_RECLAIM.value,
        raw_score=70, final_score=70, score_components={}, trigger_ready=True, trigger_level=100.0,
        invalidation_level=99.0, target_levels=[102.0], setup_family=SetupFamily.LIQUIDITY_RECOVERY.value, specificity=20)
    high_rank = Candidate(side=Side.LONG.value, setup_type=SetupType.CAPITULATION_RECOVERY.value,
        raw_score=82, final_score=82, score_components={}, trigger_ready=True, trigger_level=100.0,
        invalidation_level=99.0, target_levels=[102.0], setup_family=SetupFamily.LIQUIDITY_RECOVERY.value, specificity=40)
    collapsed = collapse_candidates([low_rank, high_rank])
    assert len(collapsed) == 1 and collapsed[0].setup_type == SetupType.CAPITULATION_RECOVERY.value

    absorption_trades = [
        {"price": 100.00 + i * 0.01, "size": 2.0, "side": "sell", "ts": start + i * 1_000} for i in range(20)
    ] + [{"price": 100.20 + i * 0.01, "size": 0.5, "side": "buy", "ts": start + (20 + i) * 1_000} for i in range(10)]
    absorption = flow_snapshot(absorption_trades, {"bids": [(100.0, 20.0)], "asks": [(100.5, 20.0)]})
    assert absorption.get("absorption_bias") == Side.LONG.value, absorption

    # Re-entry is market-event based: stale replay is suppressed, but a new
    # trigger two minutes after a stop is immediately eligible.
    failed_at = now_utc()
    reentry_state = {"history": [{
        "type": "CLOSE", "side": Side.LONG.value, "action": Action.STOP.value,
        "time": failed_at.isoformat(), "setup_type": SetupType.PULLBACK_CONTINUATION.value,
        "setup_family": SetupFamily.CONTINUATION.value, "trigger_level": 99.9,
        "structural_invalidation": 99.75, "thesis_key": "legacy-test",
    }]}
    reentry_context = dict(base_context)
    reentry_context["sweep3"] = {"side": Side.NEUTRAL.value, "ts": 0}
    reentry_context["sweep15"] = {"side": Side.NEUTRAL.value, "ts": 0}
    stale_candidate = Candidate(**{**asdict(candidate), "trigger_ts": int(failed_at.timestamp() * 1000) - 1})
    assert event_driven_reentry_guard(reentry_state, reentry_context, stale_candidate)["blocked"]
    fresh_candidate = Candidate(**{**asdict(candidate), "trigger_level": 100.25, "trigger_ts": int(failed_at.timestamp() * 1000) + 1})
    assert not event_driven_reentry_guard(reentry_state, reentry_context, fresh_candidate)["blocked"]

    # Target-derived execution: the formula gives exactly 2R for both sides.
    assert abs(optimal_entry_for_target_rr(99.0, 102.0, 2.0, Side.LONG.value) - 100.0) < 1e-9
    assert abs(optimal_entry_for_target_rr(101.0, 98.0, 2.0, Side.SHORT.value) - 100.0) < 1e-9

    # Legacy outcomes cannot train the new adaptive edge; only this architecture counts.
    edge_candidate = Candidate(**asdict(candidate))
    legacy_journal = {"trades": [{
        "side": Side.LONG.value, "setup_type": candidate.setup_type, "setup_family": candidate.setup_family,
        "variant": candidate.variant, "regime": Regime.TREND.value, "result_pct": -1.0, "result_r": -1.0,
        "architecture_version": "OLD",
    } for _ in range(10)]}
    edge_candidate = apply_setup_edge_feedback(edge_candidate, base_context, legacy_journal)
    assert edge_candidate.edge_sample_size == 0 and edge_candidate.edge_risk_multiplier == 1.0

    # Persistent full 3M scan processes each new candle exactly once.
    scan_state: dict[str, Any] = {}
    scan_context = dict(base_context)
    scan_context["candles"] = dict(base_context["candles"])
    scan_context["candles"]["3m"] = trigger_candles
    first_scan = scan_closed_3m_sequence(scan_state, scan_context)
    assert first_scan["last_run_processed"] == min(SCAN_3M_BOOTSTRAP_BARS, len(trigger_candles)), first_scan
    second_scan = scan_closed_3m_sequence(scan_state, scan_context)
    assert second_scan["last_run_processed"] == 0, second_scan
    extra_candle = Candle(start + 22 * 180_000, 100.59, 100.66, 100.52, 100.63, 180, True)
    scan_context["candles"]["3m"] = trigger_candles + [extra_candle]
    third_scan = scan_closed_3m_sequence(scan_state, scan_context)
    assert third_scan["last_run_processed"] == 1, third_scan

    # A new retest/hold timestamp, not the old displacement timestamp, unlocks
    # missed-impulse re-entry. This catches the former event-time conflict.
    retest_event = {
        "side": Side.LONG.value, "stage": "READY",
        "event_ts": start + 18 * 180_000,
        "retest_ts": start + 22 * 180_000,
        "ready_ts": start + 23 * 180_000,
        "trigger_level": 100.05, "event_price": 100.08,
        "invalidation_level": 99.70, "valid": True,
    }
    assert scan_execution_event_ts(retest_event) == start + 23 * 180_000
    missed_opportunity = Opportunity(
        side=Side.LONG.value, setup_type=SetupType.SWEEP_RECLAIM.value,
        created_at=iso_now(), expires_at=iso_now(), score=70,
        trigger_level=100.05, invalidation_level=99.70,
        setup_family=SetupFamily.LIQUIDITY_RECOVERY.value,
        status="WAIT_PULLBACK", execution_lane="MISSED_IMPULSE_REENTRY",
        origin_trigger_ts=start + 18 * 180_000,
        reentry_zone_low=99.8, reentry_zone_high=100.3,
        evidence_families=["ICT_LOCATION", "LIQUIDITY", "EXECUTION_TRIGGER"],
    )
    reentry_scan_context = dict(base_context)
    reentry_scan_context["price"] = 100.08
    reentry_scan_context["targets_long"] = [102.5, 103.5]
    reentry_scan_context["targets_short"] = [98.0, 97.0]
    reentry_scan_context["scan_3m_events"] = {Side.LONG.value: retest_event}
    reentry_scan_context["scan_3m"] = {"events": {Side.LONG.value: retest_event}}
    recovered_candidate = candidate_from_missed_opportunity(missed_opportunity, reentry_scan_context)
    assert recovered_candidate is not None and recovered_candidate.execution_lane == "MISSED_IMPULSE_REENTRY"

    # WAIT_PULLBACK memory is not overwritten by a weaker ordinary ARMED idea,
    # but a confirmed opposite structural shift invalidates the old thesis.
    weak_armed = Opportunity(
        side=Side.LONG.value, setup_type=SetupType.PULLBACK_CONTINUATION.value,
        created_at=iso_now(), expires_at=iso_now(), score=60,
        trigger_level=100.0, invalidation_level=99.6, status="ARMED",
    )
    kept = choose_persisted_opportunity(missed_opportunity, weak_armed, reentry_scan_context)
    assert kept.status == "WAIT_PULLBACK" and kept.setup_type == missed_opportunity.setup_type
    invalidation_context = dict(reentry_scan_context)
    invalidation_context["s15"] = {"bos": Side.SHORT.value, "choch": Side.NEUTRAL.value}
    assert not opportunity_is_valid(missed_opportunity, invalidation_context)

    # Three execution lanes keep separate thresholds and actions.
    early_test = Candidate(**{**asdict(candidate), "setup_type": SetupType.SWEEP_RECLAIM.value,
        "setup_family": SetupFamily.LIQUIDITY_RECOVERY.value, "risk_mode": "RISKY",
        "variant": "EARLY_RECLAIM", "execution_lane": "EARLY_TACTICAL"})
    early_test.evidence_families = ["ICT_LOCATION", "LIQUIDITY", "EXECUTION_TRIGGER"]
    early_test.final_score = max(EARLY_TACTICAL_SCORE, 66)
    early_plan = TradePlan(**{**asdict(plan), "valid": True, "execution_ready": True})
    assert execution_lane_snapshot(early_test, early_plan)["action"] == Action.RISKY_ENTRY.value
    standard_test = Candidate(**{**asdict(candidate), "execution_lane": "STANDARD_CONFIRMED"})
    standard_test.evidence_families = ["ICT_LOCATION", "PRICE_STRUCTURE", "EXECUTION_TRIGGER", "HTF_CONTEXT"]
    standard_test.final_score = max(STANDARD_CONFIRMED_SCORE, 74)
    assert execution_lane_snapshot(standard_test, early_plan)["action"] == Action.ENTRY.value

    # Telegram guard removes every user-hidden field while retaining plan data.
    probe = "Сімейство: X\nСтадія: Y\nОснова стопа: Z\nСтарший сценарій: A\nПрийняття рівня: B\nОснова тейків: C\nПротилежна зона: D\nПроміжні бар’єри: E\nОптимальний вхід: F\nСкасування: G\nРизики:\n⚠️ H\nСтоп: 99"
    cleaned_probe = clean_telegram_message(probe)
    assert cleaned_probe.strip() == "Стоп: 99", cleaned_probe

    print("SELF TEST PASSED")
    print(json.dumps({"structural_plan": asdict(plan), "blocked_plan": asdict(blocked_plan), "failed_guard": snap}, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="BZU Professional ICT signal bot")
    parser.add_argument("--self-test", action="store_true", help="run deterministic internal tests without network")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
    else:
        run_bot()


if __name__ == "__main__":
    main()
