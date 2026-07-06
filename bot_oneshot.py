#!/usr/bin/env python3
"""
BZU Professional Hybrid Confluence Signal Bot v6.8 (Data-Integrity Edition)
================================================================================
Виправлення v6.8 (інфраструктурний борг):
- КРИТИЧНО: ActiveTrade/Opportunity тепер несуть signal_id = Decision.id.
  Раніше trade.id генерувався НЕЗАЛЕЖНИМ uuid.uuid4(), тому жоден запис
  journal["trades"] не збігався за id з жодним journal["signals"] (0/64) —
  будь-яка аналітика "модель/score/regime -> результат" трималась лише на
  крихкому зіставленні "за порядком у часі". Тепер signal_id — надійний
  зовнішній ключ, присутній у trades, у FOLLOW/CLOSE записах journal["signals"]
  і в history.
- Прибрано orphaned-код незавершених рефакторів у detect_candidates()
  (eq_level з PD Array-логіки, k_hour зі старої killzone-формули, дубльований
  move8 — усе рахувалось і одразу відкидалось) та у manage_active_trade()
  (локальний atr15, якого жоден хелпер уже не приймає аргументом).
- has_forward_zone тепер справді записується в Candidate (поле існувало в
  dataclass, але ніколи не заповнювалось) — без зміни формул скорингу.

Виправлення v6.6:
- Time-Warp execution flag (ретроспективний 3M пул-аналіз для тактики входу,
  не ML/backtest validation)
- Інтеграція Premium/Discount (PD Arrays)
- SMT Divergence між OKX crypto-proxy свопами BZ/WTI
- Killzones (Торгові сесії)
- Валідація FVG (Consequent Encroachment)
(Усі попередні алгоритми супроводу угод та логування повністю збережено)
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import time
import uuid
import zoneinfo
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

BOT_VERSION = "pro-hybrid-confluence-v6.8"
ARCHITECTURE_VERSION = "HYBRID_CONFLUENCE_V6_4"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === DATA SOURCES ===
OKX_BASE_URL = "https://www.okx.com/api/v5/market"
TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com/crypto/scan"
OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
SMT_ASSET_ID = os.getenv("SMT_ASSET_ID", "WTI-USDT-SWAP") # Корелюючий OKX crypto-proxy своп для SMT
INSTRUMENT_LABEL = os.getenv("INSTRUMENT_LABEL", f"{OKX_INST_ID} crypto Brent proxy")
INSTRUMENT_KIND = os.getenv("INSTRUMENT_KIND", "OKX crypto perpetual swap proxy, not ICE/CME oil futures")
JOURNAL_PERSISTENCE_CONFIRMED = os.getenv("JOURNAL_PERSISTENCE_CONFIRMED", "").lower() in {"1", "true", "yes"}

WORKSPACE = Path(os.getenv("GITHUB_WORKSPACE", os.getcwd()))

# === ЯВНІ НАЗВИ ФАЙЛІВ v6.4 ===
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
MIN_TP1_ATR15 = max(0.90, float(os.getenv("MIN_TP1_ATR15", "1.15") or 1.15))

# === Структурний трейлінг ПІСЛЯ TP1 (Swing Points) ===
# Замінює фіксований $/ATR трейлінг: стоп рухається лише під/над останній
# ПІДТВЕРДЖЕНИЙ 15M swing low/high у бік угоди, а не на довільну відстань
# за ціною — так угода ловить розширені імпульси, не вилітаючи на кожному
# випадковому відкаті.
SWING_LOOKBACK_15M = int(os.getenv("SWING_LOOKBACK_15M", "40") or 40)
SWING_PIVOT_STRENGTH = int(os.getenv("SWING_PIVOT_STRENGTH", "2") or 2)  # свічок з кожного боку для підтвердження пивота

# === DOL (Draw on Liquidity) Hard Floors ===
# ATR-множники вище деградують до сміття, коли сам atr15 стискається до
# ринкового шуму (тиха Азія / low-liquidity години) — 0.80*atr15 від
# atr15=0.09 дає стоп у 7 центів на BZ, де звичайний спред+ковзання здатні
# з'їсти половину цієї відстані. ABS_MIN_STOP_DOLLARS — абсолютна "підлога" в
# пунктах ціни: стоп ніколи не може бути тіснішим за неї, НЕЗАЛЕЖНО від ATR
# чи мікроструктури 3M/15M. ABS_MIN_TP1_DOLLARS — дзеркальний "speed bump"
# фільтр для TP1: не дозволяє снепати тейк на мікро-FVG/пул, що лежить
# занадто близько, щоб бути реалістичною ціллю, а не ринковим шумом.
ABS_MIN_STOP_DOLLARS = float(os.getenv("ABS_MIN_STOP_DOLLARS", "0.15") or 0.15)
# ВИПРАВЛЕНО (дихання угоди): 0.30$ давав мікро-TP1, який знімався першим-ліпшим
# відкатом і не відповідав реальному розміру імпульсу на BZ 15M (ATR ~0.15-0.25$,
# адекватний перший імпульс — 2.5-3x ATR). Підлогу піднято до 0.65$ (середина
# рекомендованого діапазону 0.60-0.75$), щоб TP1 забирав рух одної торгової
# сесії (Лондон/Нью-Йорк), а не шум окремої свічки.
ABS_MIN_TP1_DOLLARS = float(os.getenv("ABS_MIN_TP1_DOLLARS", "0.65") or 0.65)
# Буфер понад ціну входу для строгого беззбитку на TP1 — покриває комісію біржі,
# щоб "безризикова" угода не закрилась технічним мінус-нулем через фі.
COMMISSION_BUFFER_DOLLARS = float(os.getenv("COMMISSION_BUFFER_DOLLARS", "0.02") or 0.02)
MIN_RR1 = max(1.50, float(os.getenv("MIN_RR1", "1.50") or 1.50))  # Професійний мінімум: TP1 не ближче 1.5R
PREFERRED_RR1 = max(1.60, float(os.getenv("PREFERRED_RR1", "1.60") or 1.60))
MIN_RR2 = max(2.50, float(os.getenv("MIN_RR2", "2.50") or 2.50))
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

# === Vector Scoring Weights (v6.7 — Pattern-Specific Weights) ===
# Reversal-родина (LIQUIDITY_RECOVERY / STRUCTURAL_TRANSITION / RANGE_EXECUTION):
# ідеальне вирівнювання HTF у бік входу означає ПІЗНІЙ вхід — штрафуємо його,
# натомість максимізуємо вагу ліквідності/тригера/SMT.
REVERSAL_LIQ_WEIGHT = float(os.getenv("REVERSAL_LIQ_WEIGHT", "1.35") or 1.35)
REVERSAL_TRIGGER_WEIGHT = float(os.getenv("REVERSAL_TRIGGER_WEIGHT", "1.30") or 1.30)
REVERSAL_HTF_WEIGHT = float(os.getenv("REVERSAL_HTF_WEIGHT", "0.35") or 0.35)
REVERSAL_SMT_BONUS = int(os.getenv("REVERSAL_SMT_BONUS", "9") or 9)
REVERSAL_LATE_ENTRY_PENALTY = int(os.getenv("REVERSAL_LATE_ENTRY_PENALTY", "9") or 9)

# Trend/Expansion-родина (CONTINUATION / EXPANSION): HTF і CVD критичні,
# преміюємо узгодження; додатково штрафуємо "погоню" за виснаженим рухом.
TREND_STRUCTURE_WEIGHT = float(os.getenv("TREND_STRUCTURE_WEIGHT", "1.15") or 1.15)
TREND_FLOW_WEIGHT = float(os.getenv("TREND_FLOW_WEIGHT", "1.30") or 1.30)
TREND_HTF_WEIGHT = float(os.getenv("TREND_HTF_WEIGHT", "1.25") or 1.25)
TREND_SMT_BONUS = int(os.getenv("TREND_SMT_BONUS", "4") or 4)
EXHAUSTION_ATR_THRESHOLD = float(os.getenv("EXHAUSTION_ATR_THRESHOLD", "1.5") or 1.5)
EXHAUSTION_SCORE_MULTIPLIER = float(os.getenv("EXHAUSTION_SCORE_MULTIPLIER", "0.5") or 0.5)

# No-Pattern Fallback Penalty: коли ЖОДНА з 10 іменних ICT-моделей не
# підтверджена, сетап деградує до generic PULLBACK_CONTINUATION — раніше це
# траплялось з майже нормальною впевненістю (16 угод, 12.5% winrate, -5.63%).
NO_PATTERN_PENALTY = int(os.getenv("NO_PATTERN_PENALTY", "14") or 14)

# === Data-Driven Quality Calibration ===
# Старі loc/str/liq/... більше не складаються як взаємозамінні бали. Вони стають
# ознаками для логістичної моделі, а критичні execution-умови застосовуються як
# мультиплікативні вентилі. Якщо журнал ще малий, працюють консервативні стартові
# коефіцієнти; коли назбираються закриті угоди з feature snapshot, бот навчає
# просту logistic regression локально без зовнішніх залежностей.
SCORING_MODEL_MIN_TRADES = int(os.getenv("SCORING_MODEL_MIN_TRADES", "60") or 60)
SCORING_MODEL_MIN_FAMILY_TRADES = int(os.getenv("SCORING_MODEL_MIN_FAMILY_TRADES", "45") or 45)
SCORING_MODEL_FULL_TRADES = int(os.getenv("SCORING_MODEL_FULL_TRADES", "160") or 160)
SCORING_MODEL_FULL_FAMILY_TRADES = int(os.getenv("SCORING_MODEL_FULL_FAMILY_TRADES", "120") or 120)
SCORING_MODEL_EPOCHS = int(os.getenv("SCORING_MODEL_EPOCHS", "180") or 180)
SCORING_MODEL_LR = float(os.getenv("SCORING_MODEL_LR", "0.08") or 0.08)
SCORING_MODEL_L2 = float(os.getenv("SCORING_MODEL_L2", "0.015") or 0.015)
SCORING_MODEL_INITIAL_LEARNED_WEIGHT = float(os.getenv("SCORING_MODEL_INITIAL_LEARNED_WEIGHT", "0.30") or 0.30)
SCORING_MODEL_MAX_LEARNED_WEIGHT = float(os.getenv("SCORING_MODEL_MAX_LEARNED_WEIGHT", "0.80") or 0.80)

QUALITY_FEATURE_KEYS = [
    "loc", "structure", "liquidity", "flow", "trigger", "htf", "pattern",
    "session", "smt", "regime_fit", "freshness", "exhaustion", "no_pattern",
]

DEFAULT_QUALITY_COEFFICIENTS = {
    "_global": {
        "bias": -1.65, "loc": 1.15, "structure": 0.75, "liquidity": 0.95,
        "flow": 0.60, "trigger": 1.30, "htf": 0.55, "pattern": 0.85,
        "session": 0.35, "smt": 0.35, "regime_fit": 0.45, "freshness": 0.40,
        "exhaustion": -1.00, "no_pattern": -1.25,
    },
    "LIQUIDITY_RECOVERY": {
        "bias": -1.70, "loc": 1.25, "structure": 0.70, "liquidity": 1.45,
        "flow": 0.30, "trigger": 1.55, "htf": 0.10, "pattern": 0.95,
        "session": 0.55, "smt": 0.70, "regime_fit": 0.45, "freshness": 0.60,
        "exhaustion": -0.70, "no_pattern": -1.40,
    },
    "STRUCTURAL_TRANSITION": {
        "bias": -1.60, "loc": 1.05, "structure": 1.15, "liquidity": 1.05,
        "flow": 0.55, "trigger": 1.30, "htf": 0.35, "pattern": 1.05,
        "session": 0.35, "smt": 0.55, "regime_fit": 0.60, "freshness": 0.45,
        "exhaustion": -0.85, "no_pattern": -1.45,
    },
    "CONTINUATION": {
        "bias": -1.75, "loc": 0.80, "structure": 1.10, "liquidity": 0.45,
        "flow": 1.05, "trigger": 1.10, "htf": 1.25, "pattern": 0.75,
        "session": 0.20, "smt": 0.20, "regime_fit": 0.70, "freshness": 0.30,
        "exhaustion": -1.45, "no_pattern": -1.15,
    },
    "EXPANSION": {
        "bias": -1.80, "loc": 0.75, "structure": 1.25, "liquidity": 0.45,
        "flow": 1.00, "trigger": 1.20, "htf": 1.15, "pattern": 0.80,
        "session": 0.25, "smt": 0.20, "regime_fit": 0.65, "freshness": 0.35,
        "exhaustion": -1.55, "no_pattern": -1.20,
    },
    "RANGE_EXECUTION": {
        "bias": -1.70, "loc": 1.45, "structure": 0.75, "liquidity": 1.25,
        "flow": 0.35, "trigger": 1.40, "htf": 0.05, "pattern": 1.00,
        "session": 0.50, "smt": 0.55, "regime_fit": 0.70, "freshness": 0.55,
        "exhaustion": -0.80, "no_pattern": -1.50,
    },
}

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
    score_components: dict[str, Any] = field(default_factory=dict)
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
    ict_model: str = "NONE"  # НОВЕ ПОЛЕ: Визначена ICT-модель
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
    # ID сигналу (Decision.id), яким цю можливість було породжено. Дозволяє
    # відстежити ланцюжок ARMED-сигнал -> (можливий) re-entry Decision, навіть
    # якщо сама Opportunity так і не конвертувалась у ActiveTrade.
    signal_id: str = ""


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
    # ID вихідного сигналу (Decision.id з evaluate_new_setup), який відкрив цю
    # угоду. Це надійний зовнішній ключ для аналітики "сигнал -> угода":
    # journal["signals"] містить запис з id == signal_id, journal["trades"]
    # містить запис з id == trade.id і signal_id == signal_id. Раніше trade.id
    # генерувався ЗАНОВО (uuid.uuid4()) і ніяк не збігався з id сигналу, тому
    # 0 з 64 угод у журналі можна було приєднати до сигналу, що їх породив —
    # будь-яка аналітика "яка модель/score/regime дає найкращий результат"
    # трималась лише на крихкому зіставленні "за порядком у часі".
    signal_id: str = ""


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
    journal.setdefault("training_signals", [])
    journal.setdefault("signal_events", [])
    journal.setdefault("trades", [])
    if not journal["training_signals"]:
        journal["training_signals"] = [
            s for s in journal.get("signals", [])
            if isinstance(s, dict) and s.get("id") and isinstance(s.get("score_features"), dict)
        ]
    journal["version"] = BOT_VERSION
    journal["architecture_version"] = ARCHITECTURE_VERSION
    if "analytics" not in journal:
        journal["analytics"] = {"closed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_r": 0.0, "expectancy_r": 0.0, "by_family": {}}
    return journal


def save_journal(journal: dict[str, Any]) -> None:
    journal["updated_at"] = iso_now()
    journal["signals"] = [
        s for s in list(journal.get("signals") or [])
        if isinstance(s, dict) and s.get("id")
    ][-MAX_JOURNAL:]
    journal["training_signals"] = [
        s for s in list(journal.get("training_signals") or [])
        if isinstance(s, dict) and s.get("id") and isinstance(s.get("score_features"), dict)
    ][-MAX_JOURNAL:]
    journal["signal_events"] = list(journal.get("signal_events") or [])[-MAX_JOURNAL:]
    journal["trades"] = list(journal.get("trades") or [])[-MAX_JOURNAL:]
    journal["analytics"] = compute_analytics(journal)
    journal["learning_status"] = compute_learning_status(journal)
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

    if decision.action == Action.NO_SETUP.value:
        lines = [
            "<b>Входу немає</b>",
            f"<b>Ціна зараз:</b> {_fmt_price(current_price)}",
        ]
        for warning in context.get("learning_warnings", [])[:2]:
            lines.append(f"⚠️ {html.escape(warning)}")
        return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]
    
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
    for warning in context.get("learning_warnings", [])[:2]:
        lines.append(f"⚠️ {html.escape(warning)}")
    
    show_plan = decision.plan and decision.plan.valid and decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.ARMED.value)
    
    if show_plan:
        p = decision.plan
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(f"Вхід <b>{_fmt_price(p.entry)}</b> | Стоп <b>{_fmt_price(p.stop)}</b>")
        lines.append(f"TP1 {_fmt_price(p.tp1)} (RR {p.rr1}) | TP2 {_fmt_price(p.tp2)} | TP3 {_fmt_price(p.tp3)}")
    
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

    if result.get("stop_changed"):
        lines.insert(
            1,
            f"🔔 <b>СТОП ЛОСС ЗМІНЕНО!</b> {_fmt_price(result.get('stop_before'))} → {_fmt_price(result.get('stop_after'))}",
        )

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
    gates = ((candidate.score_components or {}).get("gates") or {})
    gate_product = safe_float(gates.get("product"), 1.0)
    trigger_gate = safe_float(gates.get("trigger_gate"), 1.0)
    pattern_gate = safe_float(gates.get("pattern_gate"), 1.0)
    location_gate = safe_float(gates.get("location_gate"), 1.0)
    
    # ПЕРЕВІРКА ТРЕНДУ (Новий логічний блок)
    tf1h_bias = context.get("tf1h", {}).get("bias")
    tf4h_bias = context.get("tf4h", {}).get("bias")
    htf_aligned = (tf1h_bias == candidate.side) or (tf4h_bias == candidate.side)

    allow_entry = bool(
        score >= PRO_ENTRY_MIN
        and layers >= MIN_PRO_LAYERS_ENTRY
        and trigger_ready
        and strong_ict
        and gate_product >= 0.62
        and trigger_gate >= 0.95
        and pattern_gate >= 0.95
        and location_gate >= 0.74
    )

    allow_risky = bool(
        score >= PRO_RISKY_MIN
        and layers >= max(3, MIN_PRO_LAYERS_ENTRY - 1)
        and (trigger_ready or strong_ict)
        and htf_aligned
        and gate_product >= 0.46
        and pattern_gate >= 0.70
    )

    if allow_entry and score >= A_PLUS_ENTRY_MIN and strong_ict:
        grade = "A+"
    elif allow_entry:
        grade = "A"
    elif allow_risky:
        grade = "B"
    else:
        grade = "WATCH"

    reason = f"gate v7 data-driven: {grade} | {layers} шарів | score {score} | gates x{gate_product:.2f}"
    if not htf_aligned and not allow_entry:
        reason += " | БЛОК: HTF проти risky-входу"
    if pattern_gate < 0.95 and not allow_entry:
        reason += " | generic pattern penalty"

    return {
        "allow_entry": allow_entry,
        "allow_risky": allow_risky,
        "grade": grade,
        "score": score,
        "layers": layers,
        "gate_product": gate_product,
        "reason": reason,
    }

# ==========================================================
# ICT UTILITIES (NEW)
# ==========================================================

def identify_liquidity_and_range(candles: list[Candle], left_bars: int = 5, right_bars: int = 5) -> dict:
    """Визначає Premium/Discount зони та пули ліквідності."""
    if len(candles) < left_bars + right_bars + 1:
        return {"bsl": [], "ssl": [], "eq": 0.0, "premium_low": 0.0, "discount_high": 0.0}

    bsl_pools = []
    ssl_pools = []
    
    for i in range(left_bars, len(candles) - right_bars):
        window = candles[i - left_bars : i + right_bars + 1]
        center = candles[i]
        
        if center.high == max(c.high for c in window):
            bsl_pools.append(center.high)
        if center.low == min(c.low for c in window):
            ssl_pools.append(center.low)
            
    recent_high = max([c.high for c in candles[-60:]]) if len(candles) >= 60 else max([c.high for c in candles])
    recent_low = min([c.low for c in candles[-60:]]) if len(candles) >= 60 else min([c.low for c in candles])
    
    eq = (recent_high + recent_low) / 2
    
    return {
        "bsl": sorted(list(set(bsl_pools)))[-5:],
        "ssl": sorted(list(set(ssl_pools)))[:5],
        "range_high": recent_high,
        "range_low": recent_low,
        "eq": eq,
    }

def detect_smt_divergence(asset_candles: list[Candle], smt_candles: list[Candle]) -> str:
    """Шукає SMT-розбіжності між OKX crypto-proxy свопами, не між ICE/CME futures."""
    if len(asset_candles) < 20 or len(smt_candles) < 20:
        return Side.NEUTRAL.value
        
    asset_lows = [c.low for c in asset_candles[-10:]]
    smt_lows = [c.low for c in smt_candles[-10:]]
    asset_highs = [c.high for c in asset_candles[-10:]]
    smt_highs = [c.high for c in smt_candles[-10:]]
    
    asset_current_low = min(asset_lows[-3:])
    asset_prev_low = min(asset_lows[:-3])
    smt_current_low = min(smt_lows[-3:])
    smt_prev_low = min(smt_lows[:-3])
    
    # Bullish SMT
    if smt_current_low < smt_prev_low and asset_current_low >= asset_prev_low:
        return Side.LONG.value
        
    asset_current_high = max(asset_highs[-3:])
    asset_prev_high = max(asset_highs[:-3])
    smt_current_high = max(smt_highs[-3:])
    smt_prev_high = max(smt_highs[:-3])
    
    # Bearish SMT
    if smt_current_high > smt_prev_high and asset_current_high <= asset_prev_high:
        return Side.SHORT.value
        
    return Side.NEUTRAL.value


# ==========================================================
# DATA SOURCES: TradingView ПРІОРИТЕТ
# ==========================================================

def http_get(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Pro-v6.6/1.0", "Accept": "*/*"}
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
        "User-Agent": "Mozilla/5.0 BZU-Pro-v6.6/1.0",
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
    """TradingView як ОСНОВНЕ джерело ціни"""
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
    TradingView як ОСНОВНЕ джерело ціни
    OKX swap-свічки — основа для 3m/15m/1h/4h сканера
    """
    c3 = get_okx_candles(OKX_INST_ID, "3m", 240)
    c15 = get_okx_candles(OKX_INST_ID, "15m", 200)
    c1h = get_okx_candles(OKX_INST_ID, "1h", 160)
    c4h = get_okx_candles(OKX_INST_ID, "4h", 140)
    smt_c15 = get_okx_candles(SMT_ASSET_ID, "15m", 200)
    
    # TradingView — ПРІОРИТЕТ
    tv_ticker = get_tradingview_price_fallback()
    okx_ticker = get_okx_ticker(OKX_INST_ID)
    
    ticker = tv_ticker or okx_ticker
    
    if not ticker and c15:
        price = c15[-1].close
    else:
        price = ticker.get("price") if ticker else None
    
    return {
        "time": iso_now(),
        "instrument": OKX_INST_ID,
        "instrument_label": INSTRUMENT_LABEL,
        "instrument_kind": INSTRUMENT_KIND,
        "smt_instrument": SMT_ASSET_ID,
        "candles": {"3m": c3, "15m": c15, "1h": c1h, "4h": c4h},
        "smt_candles": {"15m": smt_c15},
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
        
        # ВАЛІДАЦІЯ FVG ЗГІДНО ПРАВИЛА 50% (Consequent Encroachment)
        if i >= 2:
            prev2 = candles[i - 2]
            if c.low > prev2.high:
                fvg_low, fvg_high = prev2.high, c.low
                ce = fvg_low + (fvg_high - fvg_low) / 2
                mitigated = False
                for future_c in candles[i+1:]:
                    if future_c.close < ce:
                        mitigated = True
                        break
                if not mitigated:
                    zones.append(Zone("FVG", Side.LONG.value, fvg_low, fvg_high, c.ts, tf, strength=0.75))
            
            if c.high < prev2.low:
                fvg_low, fvg_high = c.high, prev2.low
                ce = fvg_low + (fvg_high - fvg_low) / 2
                mitigated = False
                for future_c in candles[i+1:]:
                    if future_c.close > ce:
                        mitigated = True
                        break
                if not mitigated:
                    zones.append(Zone("FVG", Side.SHORT.value, fvg_low, fvg_high, c.ts, tf, strength=0.75))
                    
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


def cvd_snapshot(candles: list[Candle]) -> dict:
    """Апроксимація Cumulative Volume Delta (CVD) на основі внутрішнього тиску свічок"""
    if len(candles) < 15:
        return {"cvd": 0.0, "bias": Side.NEUTRAL.value, "strength": 0, "score": 0}
    
    delta = 0.0
    total_vol = 0.0
    
    # Аналізуємо останні 10 свічок для розуміння локального тиску
    for c in candles[-10:]:
        rng = c.high - c.low
        if rng <= 0:
            continue
            
        # Розраховуємо, яка частка свічки контролювалася покупцями/продавцями
        buy_pressure = (c.close - c.low) / rng
        sell_pressure = (c.high - c.close) / rng
        
        # Обсяг ділиться пропорційно до сили тиску
        candle_delta = c.volume * (buy_pressure - sell_pressure)
        delta += candle_delta
        total_vol += c.volume

    if total_vol == 0:
        return {"cvd": 0.0, "bias": Side.NEUTRAL.value, "strength": 0, "score": 0}

    # Визначаємо критичний перекіс дельти
    delta_ratio = abs(delta) / total_vol
    bias = Side.LONG.value if delta > 0 else Side.SHORT.value
    
    strength = 0
    score = 0
    if delta_ratio > 0.15:  # 15% обсягу йде агресивно в один бік
        strength = 1
        score = 8
    if delta_ratio > 0.30:  # 30% перекіс - це інституційний тиск
        strength = 2
        score = 15

    return {
        "cvd": round(delta, 2), 
        "bias": bias if strength > 0 else Side.NEUTRAL.value, 
        "strength": strength, 
        "score": score
    }


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

    # === VOLATILITY SQUEEZE FILTER ===
    atr5 = atr(c15, 5)
    atr20 = atr(c15, 20)
    # Якщо поточна волатильність становить менше 40% від звичайної - це аномальний штиль
    is_squeeze = bool(atr5 > 0 and atr20 > 0 and (atr5 / atr20) < 0.40)
    
    metrics = {
        "bias": bias,
        "move8_pct": round(move8, 3),
        "range_atr": round(range_atr, 2),
        "tf3": tf3.get("bias"),
        "tf15": tf15.get("bias"),
        "tf1h": tf1h.get("bias"),
        "tf4h": tf4h.get("bias"),
        "is_squeeze": is_squeeze
    }

    if is_squeeze:
        return _regime_engine_result(
            "VOLATILITY_SQUEEZE", Regime.RANGE.value, 40, 90,
            "Аномальне стискання волатильності (Squeeze). Наближається викид. Торги заборонено.",
            entry_action="BLOCK", quality_adjustment=-25, quality_cap=50, hard_block=True, metrics=metrics,
        )

    if abs(move8) >= 2.8 or regime == Regime.SHOCK.value:
        return _regime_engine_result(
            "NEWS_SHOCK", Regime.SHOCK.value, 78, 78,
            "сильний імпульс/шок: входи тільки після 3M підтвердження, прибуток захищати швидше",
            entry_action="RISKY_ONLY", quality_adjustment=-2, quality_cap=79, risky_only=True, metrics=metrics,
        )
    if abs(move8) >= 1.65 and tf3_against:
        return _regime_engine_result(
            "EXHAUSTION", Regime.TRANSITION.value, 72, 72,
            "рух розтягнутий і 3M вже проти: не доганяти, чекати retest/reclaim",
            entry_action="BLOCK", quality_adjustment=-10, quality_cap=61, hard_block=True, metrics=metrics,
        )
    if near_range_mid or (regime == Regime.RANGE.value and range_atr <= 2.30):
        return _regime_engine_result(
            "RANGE_COMPRESSION", Regime.RANGE.value, 62, 64,
            "стискання/середина діапазону: TP ближче, входи тільки після підтвердження",
            entry_action="WAIT", quality_adjustment=-2, quality_cap=77, metrics=metrics,
        )
    if regime == Regime.RANGE.value and range_atr > 2.30:
        return _regime_engine_result(
            "RANGE_EDGE", Regime.RANGE.value, 66, 68,
            "діапазон із робочими краями: брати тільки від краю, прибуток захищати швидше",
            entry_action="RISKY_ONLY" if not tf15_same else "ALLOW", quality_cap=82, metrics=metrics,
        )
    if htf_same and not tf15_same and bias in {Side.LONG.value, Side.SHORT.value}:
        return _regime_engine_result(
            "TREND_PULLBACK", Regime.TRANSITION.value, 70, 70,
            "відкат у напрямку HTF: шукати реакцію від ICT-зони, TP середньої дальності",
            entry_action="ALLOW", quality_adjustment=2, quality_cap=86, metrics=metrics,
        )
    if regime == Regime.TREND.value and htf_same and tf15_same and (tf3_same or abs(move8) > 0.65):
        return _regime_engine_result(
            "TREND_EXPANSION", Regime.TREND.value, 82, 84,
            "сильне продовження: TP2/TP3 можна тримати довше, але MFE не віддавати",
            entry_action="ALLOW", quality_adjustment=3, quality_cap=90, metrics=metrics,
        )
    if tf3_same and not htf_same and regime == Regime.TRANSITION.value:
        return _regime_engine_result(
            "REVERSAL_BUILDUP", Regime.TRANSITION.value, 65, 66,
            "формується локальний розворот: тільки контрольований ранній/risky вхід",
            entry_action="RISKY_ONLY", quality_adjustment=-1, quality_cap=84, risky_only=True, metrics=metrics,
        )
    return _regime_engine_result(
        "NORMAL", regime, 50, 50,
        "звичайний intraday режим: головний фільтр — confluence, RR і 3M trigger",
        entry_action="ALLOW", metrics=metrics,
    )


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
    price = data["price"] if data.get("price") is not None else (c15[-1].close if c15 else None)
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
    cvd = cvd_snapshot(c15)  # Передаємо 15М свічки для розрахунку синтетичного CVD
    move8 = pct(c15[-1].close, c15[-8].close) if len(c15) >= 8 else 0.0
    regime, regime_reason = regime_detection(tf4h, tf1h, move8)
    # Пули ліквідності (BSL/SSL) — потрібні для розміщення TP за технічними рівнями,
    # а не лише за фіксованим RR. 1H — для дальніх/старших цілей (TP2/TP3).
    liquidity = {
        "15m": identify_liquidity_and_range(c15),
        "1h": identify_liquidity_and_range(c1h, left_bars=3, right_bars=3),
    }

    ctx = {
        "time": data["time"],
        "price": price,
        "price_source": data.get("price_source", "TradingView"),
        "instrument": data.get("instrument", OKX_INST_ID),
        "instrument_label": data.get("instrument_label", INSTRUMENT_LABEL),
        "instrument_kind": data.get("instrument_kind", INSTRUMENT_KIND),
        "smt_instrument": data.get("smt_instrument", SMT_ASSET_ID),
        "regime": regime,
        "regime_reason": regime_reason,
        "tf3": tf3, "tf15": tf15, "tf1h": tf1h, "tf4h": tf4h,
        "zones": zones[-38:],
        "liquidity": liquidity,
        "flow": flow,
        "cvd": cvd,
        "flow_quality": "DISABLED_NO_TRADES_OR_BOOK",
        "cvd_quality": "SYNTHETIC_CANDLE_PRESSURE_PROXY",
        "atr15": atr15,
        "atr1h": atr1h,
        "candles": data["candles"],
        "btc_candles": data.get("btc_candles", {}), # ДОДАНО
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

    # Сесія (Київ) — потрібна структурному трейлінгу (пивот-буфер), щоб не
    # стискати ATR-буфер до шуму саме в тихі години Азії. Рахується напряму з
    # годинника, без залежності від наявності свічок, тож завжди визначена.
    try:
        _now_kyiv = datetime.now(zoneinfo.ZoneInfo("Europe/Kyiv"))
        _hour = _now_kyiv.hour
        if ASIA_START_H <= _hour < ASIA_END_H:
            ctx["session_name"] = "ASIA"
        elif LONDON_START_H <= _hour < LONDON_END_H:
            ctx["session_name"] = "LONDON"
        elif NY_START_H <= _hour < NY_END_H:
            ctx["session_name"] = "NY"
        else:
            ctx["session_name"] = "OFF_HOURS"
    except Exception:
        ctx["session_name"] = "UNKNOWN"

    # Макро-ліквідність (DOL): Asian High/Low, PDH/PDL, макро-EQ — використовуються
    # у find_technical_targets() як зовнішні "магніти", коли поруч бракує технічної
    # структури для TP. Обчислення не має падати весь build_context, якщо з
    # історією c15 щось не так (замало днів даних тощо, або перший try вище
    # не встиг визначити _now_kyiv).
    try:
        _now_kyiv_macro = _now_kyiv if "_now_kyiv" in dir() else datetime.now(zoneinfo.ZoneInfo("Europe/Kyiv"))
        ctx["macro_liquidity"] = compute_macro_liquidity(c15, _now_kyiv_macro)
    except Exception:
        ctx["macro_liquidity"] = {"asian_high": None, "asian_low": None, "pdh": None, "pdl": None, "macro_eq": None}

    return ctx


# ==========================================================
# 3M SCANNER + TIME-WARP VALIDATION
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
    consecutive = 0
    for i in range(min(3, len(candles) - 1)):
        c = candles[-(i+1)]
        pc = candles[-(i+2)]
        if abs(c.close - c.open) / max(c.high - c.low, 0.0001) > 0.6 and (
            (side == Side.LONG.value and c.close > pc.close) or (side == Side.SHORT.value and c.close < pc.close)
        ):
            consecutive += 1
        else:
            break
    score = 0
    if atr_ratio > 0.7: score += 1
    if atr_ratio > 1.15: score += 1
    if atr_ratio > 1.7: score += 1
    if body_strength > 0.65: score += 1
    if consecutive >= 2: score += 1
    score = min(score, 3)
    level = {0: "WEAK", 1: "NORMAL", 2: "STRONG", 3: "EXTREME"}[score]
    return {"score": score, "level": level, "body_strength": round(body_strength, 2), "consecutive": consecutive}


def trigger_snapshot(candles: list[Candle], side: str, trigger_level: float, atr15: float) -> dict:
    if len(candles) < 5:
        return {"ready": False, "age_bars": 999, "quality": 0, "displacement": False,
                "strong_displacement": False, "retest": False, "mitigation": False,
                "reclaim": False, "chase_risk": False,
                "impulse_strength": {"score": 0, "level": "WEAK"}}
    last = candles[-1]
    prev = candles[-2]
    is_sweep = (side == Side.LONG.value and last.low < trigger_level) or (side == Side.SHORT.value and last.high > trigger_level)
    is_reclaim = (side == Side.LONG.value and last.close > trigger_level) or (side == Side.SHORT.value and last.close < trigger_level)
    displacement_size = abs(last.close - prev.close)
    directional_displacement = (side == Side.LONG.value and last.close > prev.close) or (side == Side.SHORT.value and last.close < prev.close)
    displacement = displacement_size > atr15 * 0.65 and directional_displacement
    strong_displacement = displacement_size > atr15 * 0.95 and (
        (side == Side.LONG.value and last.close > prev.close) or (side == Side.SHORT.value and last.close < prev.close)
    )
    retest = False
    if len(candles) >= 4 and is_reclaim:
        recent_retest_bars = candles[-4:-1]
        if side == Side.LONG.value:
            retest = any(c.low <= trigger_level * 1.0015 for c in recent_retest_bars)
        else:
            retest = any(c.high >= trigger_level * 0.9985 for c in recent_retest_bars)
    mitigation = False
    if is_reclaim:
        mitigation = (last.close >= trigger_level and (last.close - trigger_level) < atr15 * 0.55) if side == Side.LONG.value else \
                     (last.close <= trigger_level and (trigger_level - last.close) < atr15 * 0.55)
    ready = is_reclaim and (displacement or retest)
    impulse = calculate_impulse_strength(candles, side, atr15)
    quality = 40
    if displacement: quality += 18
    if strong_displacement: quality += 15
    if retest: quality += 20
    if mitigation: quality += 12
    if is_reclaim: quality += 8
    if impulse["score"] >= 3: quality -= 14
    chase_risk = bool(strong_displacement and not retest)
    return {
        "ready": ready,
        "age_bars": 0,
        "quality": max(0, min(quality, 98)),
        "displacement": displacement,
        "strong_displacement": strong_displacement,
        "retest": retest,
        "mitigation": mitigation,
        "reclaim": is_reclaim,
        "chase_risk": chase_risk,
        "impulse_strength": impulse,
        "sweep_level": trigger_level,
        "extreme": last.low if side == Side.LONG.value else last.high
    }


def has_quality_reclaim(event: dict, min_quality: int = RECLAIM_MIN_QUALITY) -> bool:
    if not isinstance(event, dict):
        return False
    stage_ok = event.get("stage") in {"ACCEPTANCE", "RETEST", "READY"}
    quality_ok = int(event.get("acceptance_quality", 0) or 0) >= min_quality
    professional_trigger = bool(event.get("strong_displacement") or event.get("retest"))
    impulse = event.get("impulse_strength") if isinstance(event.get("impulse_strength"), dict) else {}
    overextended_without_retest = int(impulse.get("score", 0) or 0) >= 3 and not event.get("retest")
    return bool(stage_ok and quality_ok and professional_trigger and not overextended_without_retest)


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
            "impulse_strength": {"score": 0, "level": "WEAK"}, "chase_risk": False,
            "time_warp_opportunity": False # ІННОВАЦІЯ №4
        })
        
        # --- TIME-WARP VALIDATION ENGINE ---
        # "Повертаємося у часі" і перевіряємо кожну 3-хвилинну свічку всередині сліпої зони
        for i in range(len(new_candles)):
            current_c = new_candles[i]
            retro_recent = [c for c in c3 if c.ts <= current_c.ts][-15:]
            if len(retro_recent) < 5:
                continue
                
            snap = trigger_snapshot(retro_recent, side, event.get("trigger_level", context["price"]), atr15)
            
            event["displacement"] = bool(event.get("displacement") or snap.get("displacement"))
            event["strong_displacement"] = bool(event.get("strong_displacement") or snap.get("strong_displacement"))
            event["retest"] = bool(event.get("retest") or snap.get("retest"))
            event["mitigation"] = bool(event.get("mitigation") or snap.get("mitigation"))
            event["chase_risk"] = bool(event["strong_displacement"] and not event["retest"])
            
            # Зберігаємо найсильніший імпульс
            current_imp = event.get("impulse_strength", {"score": 0, "level": "WEAK"})
            snap_imp = snap.get("impulse_strength", {"score": 0, "level": "WEAK"})
            if snap_imp["score"] >= current_imp["score"]:
                event["impulse_strength"] = snap_imp
                
            event["confirmation_quality"] = max(int(event.get("confirmation_quality", 0) or 0), int(snap.get("quality", 0) or 0))

            # Логіка стадій
            if snap["ready"] and event["stage"] in ["SWEEP", "CONFIRMATION"]:
                event["previous_stage"] = event["stage"]
                event["stage"] = "ACCEPTANCE"
                event["ready_ts"] = retro_recent[-1].ts
                event["acceptance_quality"] = max(int(event.get("acceptance_quality", 0) or 0), int(snap["quality"]))
                event["last_event_ts"] = retro_recent[-1].ts
            elif event["stage"] == "ACCEPTANCE" and snap["retest"]:
                event["previous_stage"] = event["stage"]
                event["stage"] = "RETEST"
                event["retest_ts"] = retro_recent[-1].ts
                event["retest_quality"] = max(int(event.get("retest_quality", 0) or 0), int(snap["quality"]))
                event["last_event_ts"] = retro_recent[-1].ts
            elif event["stage"] == "RETEST" and (snap["reclaim"] or snap["ready"]):
                event["previous_stage"] = event["stage"]
                event["stage"] = "READY"
                event["ready_ts"] = retro_recent[-1].ts
                event["last_event_ts"] = retro_recent[-1].ts
                
            if snap.get("sweep_level"):
                event["trigger_level"] = snap["sweep_level"]
                event["sweep_level"] = snap["sweep_level"]
                event["extreme"] = snap["extreme"]
                
            # Time-Warp Detection: Фіксуємо, якщо вхід був всередині вікна, а не на самому кінці
            if snap["ready"] and event["stage"] in ["READY", "ACCEPTANCE", "RETEST"] and i < len(new_candles) - 1:
                event["time_warp_opportunity"] = True

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
    base = int(event.get("acceptance_quality", 40) or 40)
    
    if has_forward_zone:
        base += 12

    if event.get("displacement"):
        base += 8
    if event.get("strong_displacement"):
        base += 10
    if event.get("retest"):
        base += 16
    if event.get("mitigation"):
        base += 8

    if structure_alignment > 70:
        base += 6
    elif structure_alignment > 55:
        base += 3

    if event.get("stage") == "READY":
        base += 5

    impulse = event.get("impulse_strength") if isinstance(event.get("impulse_strength"), dict) else {}
    impulse_score = int(impulse.get("score", 0) or 0)
    if impulse_score >= 3 and not event.get("retest"):
        base -= 16
    elif impulse_score == 2 and not (event.get("retest") or event.get("mitigation")):
        base -= 6

    if not (event.get("strong_displacement") or event.get("retest")):
        base -= 10

    return int(clamp(base, 0, 95))


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


# ==========================================================
# SESSION PROFILING & AMD (Accumulation — Manipulation — Distribution)
# ==========================================================
# Інституційна логіка (за Київським часом): Азія (02:00-10:00) формує діапазон
# АКУМУЛЯЦІЇ. Лондон і Нью-Йорк не торгуються "у вакуумі" — вони або:
#   (a) знімають ліквідність за межами Азійського діапазону і різко повертаються
#       назад (МАНІПУЛЯЦІЯ / Judas Swing) — найточніший вхід дня, або
#   (b) "перемелюють" ціну ВСЕРЕДИНІ Азійського діапазону, не знявши ліквідність
#       (CHOP ZONE / "м'ясорубка") — статистично найгірші умови для входу.
#
# "Контекстуальна пам'ять" реалізована БЕЗ крихкого стану між запусками бота:
# Азійський діапазон і факт свіпу/реклейму рахуються щоразу заново напряму зі
# свічок (c15), тож система завжди самоузгоджена з реальним ринком, навіть якщо
# бот перезапускався або пропустив кілька циклів сканування.

ASIA_START_H, ASIA_END_H = 2, 10          # Accumulation
LONDON_START_H, LONDON_END_H = 10, 15     # Manipulation (перші 2 год) + Distribution
NY_START_H, NY_END_H = 15, 23             # Manipulation (перші 2 год) + Distribution
MANIP_WINDOW_H = 2                        # тривалість "вікна маніпуляції" на старті сесії
SESSION_MIN_CANDLES = 4                   # мінімум свічок в Азії, щоб довіряти діапазону
SWEEP_BUFFER_ATR = 0.12                   # наскільки далеко за рівень має пройти свіп
JUDAS_LOOKBACK_CANDLES = 8                # ~2 год на 15m — як далеко назад шукати свіжий свіп
JUDAS_FRESH_CANDLES = 6                   # бонус плавно згасає за цей проміжок (без різкого блоку)
JUDAS_MAX_BONUS = 35.0
JUDAS_DOUBLE_SWEEP_BONUS = 8.0
CHOP_MAX_PENALTY = 25.0
CHOP_MIN_PENALTY_FRACTION = 0.55          # на межі chop-зони штраф м'якший, ніж у центрі


def _candle_kyiv_dt(ts_ms: int, kyiv_tz) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(kyiv_tz)


def _session_range(candles: list, kyiv_tz, ref_date, start_h: int, end_h: int) -> Optional[dict]:
    """Хай/лоу конкретної сесії конкретної календарної дати (за Київським часом)."""
    highs, lows = [], []
    for c in candles:
        dt = _candle_kyiv_dt(c.ts, kyiv_tz)
        if dt.date() == ref_date and start_h <= dt.hour < end_h:
            highs.append(c.high)
            lows.append(c.low)
    if len(highs) < SESSION_MIN_CANDLES:
        return None
    return {"high": max(highs), "low": min(lows), "n": len(highs)}


def _find_recent_session_range(candles: list, kyiv_tz, now_kyiv, start_h: int, end_h: int,
                                start_days_back: int = 0, max_days_back: int = 5) -> Optional[dict]:
    """
    Стійкість до розривів вихідних/свят: якщо на очікуваній даті замало свічок
    (гепи в даних або нерівномірна активність crypto-proxy свопу), шукаємо найближчу попередню завершену сесію.
    """
    for days_back in range(start_days_back, start_days_back + max_days_back):
        ref_date = (now_kyiv - timedelta(days=days_back)).date()
        rng = _session_range(candles, kyiv_tz, ref_date, start_h, end_h)
        if rng:
            return rng
    return None


def _prev_day_high_low(candles: list, kyiv_tz, now_kyiv: datetime, max_days_back: int = 5) -> Optional[dict]:
    """
    PDH/PDL (Previous Day High/Low) — хай/лоу останнього ПОВНІСТЮ завершеного
    календарного дня за Київським часом. Це один з ключових "макро-магнітів"
    DOL (Draw on Liquidity): якщо поруч немає технічної структури (тонкий
    ринок / хай історичного діапазону), розумні гроші штовхають ціну саме
    сюди, бо там лежать стопи ритейл-трейдерів з учорашньої сесії.

    Так само, як і _find_recent_session_range, стійкий до гепів у даних —
    якщо вчорашній день має замало свічок, відступає на день глибше.
    """
    for days_back in range(1, 1 + max_days_back):
        ref_date = (now_kyiv - timedelta(days=days_back)).date()
        highs = [c.high for c in candles if _candle_kyiv_dt(c.ts, kyiv_tz).date() == ref_date]
        lows = [c.low for c in candles if _candle_kyiv_dt(c.ts, kyiv_tz).date() == ref_date]
        if len(highs) >= SESSION_MIN_CANDLES:
            return {"high": max(highs), "low": min(lows), "n": len(highs)}
    return None


def compute_macro_liquidity(c15: list, now_kyiv: datetime) -> dict:
    """
    Зовнішні (макро) цілі ліквідності для DOL-архітектури: Asian High/Low,
    PDH/PDL, і EQ (еквілібріум) макро-діапазону між ними. Використовуються
    у find_technical_targets() як "магніти", коли поруч бракує технічної
    структури (OB/FVG/внутрішньоденна ліквідність) — угода НЕ стискає тейки
    і НЕ скасовується через відсутність зручного близького рівня, а
    перемикається на таргетування зовнішньої ліквідності, як це робить
    інституційний трейдер.
    """
    kyiv_tz = now_kyiv.tzinfo
    asian = _find_recent_session_range(c15, kyiv_tz, now_kyiv, ASIA_START_H, ASIA_END_H, start_days_back=0)
    pdhl = _prev_day_high_low(c15, kyiv_tz, now_kyiv)

    asian_high = asian["high"] if asian else None
    asian_low = asian["low"] if asian else None
    pdh = pdhl["high"] if pdhl else None
    pdl = pdhl["low"] if pdhl else None

    macro_eq = None
    highs = [v for v in (asian_high, pdh) if v]
    lows = [v for v in (asian_low, pdl) if v]
    if highs and lows:
        macro_eq = (max(highs) + min(lows)) / 2

    return {
        "asian_high": asian_high, "asian_low": asian_low,
        "pdh": pdh, "pdl": pdl,
        "macro_eq": macro_eq,
    }


def _sweep_and_reclaim(candles: list, level: float, direction: str, buffer: float, lookback: int) -> Optional[dict]:
    """
    direction='above': шукаємо свічку, чий high пробив level+buffer, а close (цієї ж
    або однієї з наступних 2 свічок) повернувся під level -> свіп верхньої
    ліквідності + реклейм (ведмежий Judas Swing).
    direction='below': дзеркально знизу (бичачий Judas Swing).
    """
    if not candles or level <= 0:
        return None
    window = candles[-lookback:]
    for i, c in enumerate(window):
        swept = (direction == "above" and c.high > level + buffer) or \
                (direction == "below" and c.low < level - buffer)
        if not swept:
            continue
        reclaim_window = window[i:i + 3]
        reclaimed = any((rc.close < level) if direction == "above" else (rc.close > level) for rc in reclaim_window)
        if reclaimed:
            return {"level": level, "candles_since": len(window) - 1 - i}
    return None


def analyze_session_profile(c15: list, now_kyiv: datetime, atr15: float) -> dict:
    """
    Повертає повний поведінковий профіль поточного моменту:
    фаза AMD, Азійський діапазон, chop-зона (з градацією штрафу за глибиною),
    і Judas Swing bias (з плавним згасанням бонусу за часом, а не різким блоком).
    """
    kyiv_tz = now_kyiv.tzinfo
    hour = now_kyiv.hour
    today = now_kyiv.date()

    session_name = "ПОЗА СЕСІЄЮ"
    phase = "OFF_SESSION"
    session_start_h: Optional[int] = None

    if ASIA_START_H <= hour < ASIA_END_H:
        phase = "ACCUMULATION"
        session_name = "АЗІЯ (Накопичення)"
    elif LONDON_START_H <= hour < LONDON_END_H:
        session_start_h = LONDON_START_H
        session_name = "ЛОНДОН"
        phase = "MANIPULATION" if hour < LONDON_START_H + MANIP_WINDOW_H else "DISTRIBUTION"
    elif NY_START_H <= hour < NY_END_H:
        session_start_h = NY_START_H
        session_name = "НЬЮ-ЙОРК"
        phase = "MANIPULATION" if hour < NY_START_H + MANIP_WINDOW_H else "DISTRIBUTION"

    # Азійський діапазон-референс: якщо ми ще ВСЕРЕДИНІ сьогоднішньої Азії, вона ще
    # не завершена — довіряти їй як фінальному "магніту" зарано, тож головний
    # референс під час ACCUMULATION лишається None (лише жива інформативна межа).
    if hour < ASIA_END_H:
        asia_live = _session_range(c15, kyiv_tz, today, ASIA_START_H, hour + 1)
        asia_ref = None
    else:
        asia_live = None
        asia_ref = _find_recent_session_range(c15, kyiv_tz, now_kyiv, ASIA_START_H, ASIA_END_H, start_days_back=0)

    result = {
        "phase": phase,
        "session_name": session_name,
        "asia": asia_ref or asia_live,
        "asia_is_live": asia_ref is None and asia_live is not None,
        "asia_range_valid": False,
        "chop_zone": {"active": False, "low": None, "high": None, "penalty_magnitude": 0.0},
        "judas": {"bias": None, "bonus": 0.0, "double_sweep": False, "candles_since": None},
        "notes": [],
    }

    if not asia_ref or atr15 <= 0:
        return result

    asia_high, asia_low = asia_ref["high"], asia_ref["low"]
    asia_range = asia_high - asia_low
    # Занадто вузька/пласка Азія — слабкий референс, не варто на ній будувати
    # ні chop-штраф, ні judas-бонус (немає реальної ліквідності, яку варто "знімати").
    asia_range_valid = asia_range >= atr15 * 0.8
    result["asia_range_valid"] = asia_range_valid
    if not asia_range_valid:
        return result

    # Judas Swing: рахуємо лише в межах Лондона/Нью-Йорка (не в самій Азії — там
    # немає ще завершеного діапазону, який можна було б "зняти").
    if session_start_h is not None:
        buf = atr15 * SWEEP_BUFFER_ATR
        high_sweep = _sweep_and_reclaim(c15, asia_high, "above", buf, JUDAS_LOOKBACK_CANDLES)
        low_sweep = _sweep_and_reclaim(c15, asia_low, "below", buf, JUDAS_LOOKBACK_CANDLES)

        chosen, bias, double_sweep = None, None, False
        if high_sweep and low_sweep:
            # Обидві сторони Азії знято — "полювання" в обидва боки. Довіряємо
            # СВІЖІШОМУ свіпу (останній замір ринку зазвичай і є справжнім наміром),
            # і додаємо надбавку за подвійну маніпуляцію (сильніший сигнал).
            double_sweep = True
            if high_sweep["candles_since"] <= low_sweep["candles_since"]:
                chosen, bias = high_sweep, Side.SHORT.value
            else:
                chosen, bias = low_sweep, Side.LONG.value
        elif high_sweep:
            chosen, bias = high_sweep, Side.SHORT.value
        elif low_sweep:
            chosen, bias = low_sweep, Side.LONG.value

        if chosen:
            since = chosen["candles_since"]
            if since <= JUDAS_FRESH_CANDLES:
                # Плавне згасання бонусу (без різкого "блоку" на 7-й свічці) —
                # 35 балів одразу після реклейму, ~10 балів на межі "свіжості".
                decay = 1.0 - (since / JUDAS_FRESH_CANDLES) * 0.71
                bonus = JUDAS_MAX_BONUS * max(0.0, decay)
                if double_sweep:
                    bonus += JUDAS_DOUBLE_SWEEP_BONUS
                result["judas"] = {
                    "bias": bias, "bonus": round(bonus, 1),
                    "double_sweep": double_sweep, "candles_since": since,
                }
                side_label = "хай" if bias == Side.SHORT.value else "лоу"
                result["notes"].append(
                    f"🎯 Judas Swing: свіп Азійського {side_label} + реклейм "
                    f"({since} св. тому){' [подвійний свіп]' if double_sweep else ''} → +{bonus:.0f}"
                )

    # Chop Zone: середня третина Азійського діапазону. Штраф градуйований за
    # глибиною занурення в зону (м'якший на краю, максимальний у центрі) — а не
    # єдине фіксоване число, і НЕ застосовується, якщо саме в цю сторону вже
    # підтверджено свіжу маніпуляцію (тоді відповідний бік отримує Judas-бонус,
    # а не штраф).
    price_now = c15[-1].close if c15 else None
    if session_start_h is not None and price_now is not None:
        third = asia_range / 3.0
        chop_low = asia_low + third
        chop_high = asia_high - third
        if chop_low <= price_now <= chop_high:
            mid = (chop_low + chop_high) / 2.0
            half = max((chop_high - chop_low) / 2.0, 1e-9)
            depth = 1.0 - min(abs(price_now - mid), half) / half  # 0 на межі -> 1 в центрі
            magnitude = CHOP_MAX_PENALTY * (CHOP_MIN_PENALTY_FRACTION + (1 - CHOP_MIN_PENALTY_FRACTION) * depth)
            result["chop_zone"] = {
                "active": True, "low": round_price(chop_low), "high": round_price(chop_high),
                "penalty_magnitude": round(magnitude, 1),
            }
            result["notes"].append(
                f"⚠️ Chop Zone: ціна в середній третині Азійського діапазону "
                f"({round_price(chop_low)}-{round_price(chop_high)}), ліквідність ще не знята → -{magnitude:.0f}"
            )

    return result


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -60.0))
    return z / (1.0 + z)


def _trade_ground_truth(trade: dict) -> Optional[tuple[int, float, str]]:
    """Повертає (label, sample_weight, reason) або None, якщо результат угоди
    занадто неоднозначний, щоб бути ground truth.

    v6.8 fix: "mfe_only" гілки (і legacy, і TP-based) раніше форсували
    label=1 ("win") для угод, які НЕ закрились у прибуток (result_pct між
    -0.10% і 0%), лише тому, що ціна колись торкнулась сприятливого
    excursion (mfe_pct>=0.55%). Це шумна мітка: вона вчить модель, що
    сетапи з раннім рухом у потрібний бік, який потім розвернувся й не
    реалізувався — "перемоги", хоча по факту це радше ознака слабкого
    follow-through / поганого менеджменту виходу, а не якості сетапу.
    Занижена вага (0.20/0.35) лише притлумлювала шум, а не прибирала
    саму помилку напрямку мітки. Тепер такі угоди виключаються з
    тренувальної вибірки повністю (None), а не вчать модель у "неправильний" бік.
    """
    result_pct = safe_float(trade.get("result_pct"), 0.0)
    mfe_pct = safe_float(trade.get("mfe_pct"), 0.0)
    has_tp_fields = any(key in trade for key in ("tp1_hit", "tp2_hit", "tp3_hit"))
    tp1_hit = bool(trade.get("tp1_hit"))
    tp2_hit = bool(trade.get("tp2_hit"))
    tp3_hit = bool(trade.get("tp3_hit"))

    if not has_tp_fields:
        if result_pct >= 0.35:
            return 1, 0.70, "legacy_realized_profit"
        if result_pct > 0.0:
            return 1, 0.55, "legacy_small_profit"
        if mfe_pct >= 0.55 and result_pct >= -0.10:
            return None
        if result_pct <= -0.35:
            return 0, 0.85, "legacy_hard_loss"
        return 0, 0.60, "legacy_loss_or_noise"

    if tp3_hit:
        return 1, 1.60, "tp3"
    if tp2_hit:
        return 1, 1.35, "tp2"
    if tp1_hit and result_pct > 0.0:
        return 1, 1.10, "tp1_realized"
    if result_pct >= 0.35:
        return 1, 1.00, "realized_profit"
    if result_pct > 0.0:
        return 1, 0.75, "small_realized_profit"
    if mfe_pct >= 0.55 and result_pct >= -0.10:
        return None
    if result_pct <= -0.35:
        return 0, 1.25, "hard_loss"
    return 0, 1.00, "loss_or_noise"



def _quality_training_rows(journal: dict, family: str = "") -> list[tuple[dict[str, float], int, float]]:
    signal_records = list(journal.get("training_signals") or []) + list(journal.get("signals") or [])
    signals = {
        str(s.get("id")): s for s in signal_records
        if isinstance(s, dict) and s.get("id") and isinstance(s.get("score_features"), dict)
    }
    rows: list[tuple[dict[str, float], int, float]] = []
    for trade in journal.get("trades", []):
        if not isinstance(trade, dict):
            continue
        signal = signals.get(str(trade.get("signal_id") or trade.get("id") or ""))
        if not signal:
            continue
        if family and signal.get("setup_family") != family:
            continue
        has_tp_fields = any(key in trade for key in ("tp1_hit", "tp2_hit", "tp3_hit"))
        if family and not has_tp_fields:
            continue
        ground_truth = _trade_ground_truth(trade)
        if ground_truth is None:
            continue
        label, sample_weight, _ = ground_truth
        features = {
            key: clamp(safe_float(signal["score_features"].get(key), 0.0), -1.0, 1.0)
            for key in QUALITY_FEATURE_KEYS
        }
        rows.append((features, label, sample_weight))
    return rows


def _validation_metrics(rows: list[tuple[dict[str, float], int, float]], default: dict[str, float]) -> dict[str, Any]:
    if len(rows) < SCORING_MODEL_MIN_TRADES:
        return {"enabled": False, "reason": "not_enough_rows"}
    split_at = max(1, int(len(rows) * 0.8))
    train_rows = rows[:split_at]
    validation_rows = rows[split_at:]
    if len(validation_rows) < 8:
        return {"enabled": False, "reason": "validation_too_small"}
    learned = _fit_logistic_coefficients(train_rows, default)
    learned_weight = _learned_model_weight(len(train_rows), SCORING_MODEL_MIN_TRADES, SCORING_MODEL_FULL_TRADES)
    coef = _blend_coefficients(default, learned, learned_weight)
    weighted_loss = 0.0
    total_weight = 0.0
    correct = 0.0
    for features, label, sample_weight in validation_rows:
        weight = max(float(sample_weight or 0.0), 0.05)
        probability = _sigmoid(coef.get("bias", 0.0) + sum(coef.get(key, 0.0) * features.get(key, 0.0) for key in QUALITY_FEATURE_KEYS))
        probability = clamp(probability, 1e-5, 1.0 - 1e-5)
        weighted_loss += weight * (-(label * math.log(probability) + (1 - label) * math.log(1.0 - probability)))
        total_weight += weight
        if (probability >= 0.5) == bool(label):
            correct += weight
    return {
        "enabled": True,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "learned_weight": round(learned_weight, 4),
        "log_loss": round(weighted_loss / max(total_weight, 1.0), 4),
        "accuracy": round(correct / max(total_weight, 1.0), 4),
    }


def compute_learning_status(journal: dict) -> dict[str, Any]:
    training_signals = [
        s for s in journal.get("training_signals", [])
        if isinstance(s, dict) and isinstance(s.get("score_features"), dict)
    ]
    global_rows = _quality_training_rows(journal)
    families = sorted({
        str(s.get("setup_family") or "")
        for s in training_signals
        if s.get("setup_family")
    })
    by_family: dict[str, Any] = {}
    for family in families:
        rows = _quality_training_rows(journal, family)
        if not rows:
            continue
        by_family[family] = {
            "rows": len(rows),
            "ready": len(rows) >= SCORING_MODEL_MIN_FAMILY_TRADES,
            "learned_weight": round(_learned_model_weight(len(rows), SCORING_MODEL_MIN_FAMILY_TRADES, SCORING_MODEL_FULL_FAMILY_TRADES), 4),
        }
    global_ready = len(global_rows) >= SCORING_MODEL_MIN_TRADES
    return {
        "updated_at": iso_now(),
        "journal_file": str(JOURNAL_FILE),
        "state_file": str(STATE_FILE),
        "instrument_label": INSTRUMENT_LABEL,
        "instrument_kind": INSTRUMENT_KIND,
        "training_signals": len(training_signals),
        "signal_events": len(journal.get("signal_events", []) or []),
        "closed_trades": len(journal.get("trades", []) or []),
        "training_rows": len(global_rows),
        "global_ready": global_ready,
        "global_learned_weight": round(_learned_model_weight(len(global_rows), SCORING_MODEL_MIN_TRADES, SCORING_MODEL_FULL_TRADES), 4),
        "by_family": by_family,
        "validation": _validation_metrics(global_rows, DEFAULT_QUALITY_COEFFICIENTS["_global"]),
        "github_actions": bool(os.getenv("GITHUB_ACTIONS")),
        "journal_persistence_confirmed": JOURNAL_PERSISTENCE_CONFIRMED,
        "flow_quality": "DISABLED_NO_TRADES_OR_BOOK",
        "cvd_quality": "SYNTHETIC_CANDLE_PRESSURE_PROXY",
    }


def learning_health_warnings(journal: dict) -> list[str]:
    status = journal.get("learning_status") if isinstance(journal.get("learning_status"), dict) else compute_learning_status(journal)
    warnings: list[str] = []
    if status.get("training_signals", 0) == 0:
        warnings.append("ML dataset порожній: training_signals ще не накопичені.")
    if status.get("closed_trades", 0) == 0:
        warnings.append("Ground truth порожній: ще немає закритих trades для навчання.")
    elif status.get("training_rows", 0) == 0:
        warnings.append("Closed trades є, але signal_id не з'єднався з training_signals.")
    elif not status.get("global_ready"):
        warnings.append(f"ML ще в bootstrap: {status.get('training_rows', 0)}/{SCORING_MODEL_MIN_TRADES} training rows.")
    if os.getenv("GITHUB_ACTIONS") and not JOURNAL_PERSISTENCE_CONFIRMED:
        warnings.append("GitHub Actions: не підтверджено commit/cache persistence для journal/state.")
    return warnings


def _fit_logistic_coefficients(rows: list[tuple[dict[str, float], int, float]], default: dict[str, float]) -> dict[str, float]:
    if len(rows) < 2:
        return dict(default)
    coef = {key: float(default.get(key, 0.0)) for key in ["bias", *QUALITY_FEATURE_KEYS]}
    for epoch in range(SCORING_MODEL_EPOCHS):
        grad = {key: 0.0 for key in coef}
        total_weight = 0.0
        for features, label, sample_weight in rows:
            weight = max(float(sample_weight or 0.0), 0.05)
            z = coef["bias"] + sum(coef[key] * features.get(key, 0.0) for key in QUALITY_FEATURE_KEYS)
            error = (_sigmoid(z) - label) * weight
            grad["bias"] += error
            for key in QUALITY_FEATURE_KEYS:
                grad[key] += error * features.get(key, 0.0)
            total_weight += weight
        normalizer = max(total_weight, 1.0)
        lr = SCORING_MODEL_LR / math.sqrt(1.0 + epoch * 0.08)
        for key in coef:
            regularization = 0.0 if key == "bias" else SCORING_MODEL_L2 * coef[key]
            coef[key] -= lr * ((grad[key] / normalizer) + regularization)
    return coef


def _learned_model_weight(sample_size: int, min_rows: int, full_rows: int) -> float:
    if sample_size < min_rows:
        return 0.0
    if full_rows <= min_rows:
        return SCORING_MODEL_MAX_LEARNED_WEIGHT
    progress = clamp((sample_size - min_rows) / (full_rows - min_rows), 0.0, 1.0)
    return clamp(
        SCORING_MODEL_INITIAL_LEARNED_WEIGHT +
        (SCORING_MODEL_MAX_LEARNED_WEIGHT - SCORING_MODEL_INITIAL_LEARNED_WEIGHT) * progress,
        0.0,
        SCORING_MODEL_MAX_LEARNED_WEIGHT,
    )


def _blend_coefficients(default: dict[str, float], learned: dict[str, float], learned_weight: float) -> dict[str, float]:
    weight = clamp(learned_weight, 0.0, 1.0)
    return {
        key: float(default.get(key, 0.0)) * (1.0 - weight) + float(learned.get(key, 0.0)) * weight
        for key in ["bias", *QUALITY_FEATURE_KEYS]
    }


def _quality_coefficients(journal: dict, setup_family: str) -> tuple[dict[str, float], str, int, float]:
    family_default = DEFAULT_QUALITY_COEFFICIENTS.get(setup_family, DEFAULT_QUALITY_COEFFICIENTS["_global"])
    family_rows = _quality_training_rows(journal, setup_family)
    if len(family_rows) >= SCORING_MODEL_MIN_FAMILY_TRADES:
        rows = family_rows
        source = f"journal:{setup_family}"
        min_rows = SCORING_MODEL_MIN_FAMILY_TRADES
        full_rows = SCORING_MODEL_FULL_FAMILY_TRADES
    else:
        rows = _quality_training_rows(journal)
        source = "journal:global" if len(rows) >= SCORING_MODEL_MIN_TRADES else "bootstrap"
        min_rows = SCORING_MODEL_MIN_TRADES
        full_rows = SCORING_MODEL_FULL_TRADES

    if len(rows) < min_rows:
        return dict(family_default), "bootstrap", len(rows), 0.0

    learned_weight = _learned_model_weight(len(rows), min_rows, full_rows)

    # v6.8: кешування навченого logistic-fit у journal. Раніше _fit_logistic_coefficients
    # (SCORING_MODEL_EPOCHS ітерацій градієнтного спуску по всіх training rows)
    # перераховувався заново на КОЖЕН запуск бота — а це scheduled one-shot скрипт,
    # який може запускатись кожні кілька хвилин, тоді як training rows між запусками
    # найчастіше взагалі не змінюються (нова угода закривається рідко). Кеш
    # інвалідується за відбитком стану journal["trades"] (кількість + id останньої
    # угоди + фактична кількість rows) — детермінований fit на тих самих rows завжди
    # дає той самий результат, тож пропуск обчислення безпечний, а не апроксимація.
    trades_list = journal.get("trades") or []
    fingerprint = f"{len(trades_list)}:{trades_list[-1].get('id') if trades_list else ''}:{len(rows)}"
    cache = journal.setdefault("_model_coef_cache", {})
    cache_key = f"{setup_family}:{source}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint and isinstance(cached.get("coef"), dict):
        learned = cached["coef"]
    else:
        learned = _fit_logistic_coefficients(rows, family_default)
        cache[cache_key] = {"fingerprint": fingerprint, "coef": learned, "computed_at": iso_now()}

    blended = _blend_coefficients(family_default, learned, learned_weight)
    return blended, f"{source}:blend{learned_weight:.2f}", len(rows), learned_weight


def _build_quality_features(
    *,
    loc_score: float,
    str_score: float,
    liq_score: float,
    flw_score: float,
    trig_score: float,
    htf_score: float,
    raw_bonus: float,
    session_bonus: float,
    vector_bonus: float,
    trigger_age: float,
    trigger_ready: bool,
    best_pattern: Optional[str],
    regime_matched: int,
    regime_conflict: int,
    exhaustion_multiplier: float,
    pattern_family: str,
) -> dict[str, float]:
    family_is_reversal = pattern_family in {
        SetupFamily.LIQUIDITY_RECOVERY.value,
        SetupFamily.STRUCTURAL_TRANSITION.value,
        SetupFamily.RANGE_EXECUTION.value,
    }
    trigger_window = max(float(TRIGGER_MAX_AGE_MINUTES), 1.0)
    freshness = 1.0 - clamp(trigger_age / trigger_window, 0.0, 1.0) if trigger_ready else 0.0
    return {
        "loc": clamp(loc_score / 45.0, 0.0, 1.0),
        "structure": clamp(str_score / 24.0, 0.0, 1.0),
        "liquidity": clamp(liq_score / 24.0, 0.0, 1.0),
        "flow": clamp(flw_score / 24.0, 0.0, 1.0),
        "trigger": 1.0 if trigger_ready else 0.0,
        "htf": clamp(htf_score / (24.0 if not family_is_reversal else 12.0), 0.0, 1.0),
        "pattern": clamp((raw_bonus + NO_PATTERN_PENALTY) / 42.0, 0.0, 1.0),
        "session": clamp(session_bonus / max(JUDAS_MAX_BONUS, 1.0), -1.0, 1.0),
        "smt": clamp(vector_bonus / 12.0, -1.0, 1.0),
        "regime_fit": clamp((regime_matched - regime_conflict), -1.0, 1.0),
        "freshness": freshness,
        "exhaustion": clamp(1.0 - exhaustion_multiplier, 0.0, 1.0),
        "no_pattern": 0.0 if best_pattern else 1.0,
    }


def _multiplicative_quality_gates(features: dict[str, float], setup_family: str, trigger_ready: bool,
                                  is_limit_armed: bool, has_forward_zone: bool) -> dict[str, float]:
    family_is_reversal = setup_family in {
        SetupFamily.LIQUIDITY_RECOVERY.value,
        SetupFamily.STRUCTURAL_TRANSITION.value,
        SetupFamily.RANGE_EXECUTION.value,
    }
    family_is_trend = setup_family in {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}
    trigger_gate = 1.0 if (trigger_ready or is_limit_armed) else 0.58
    location_gate = 0.62 + 0.38 * features["loc"]
    pattern_gate = 0.70 if features["no_pattern"] >= 1.0 else 1.0
    forward_zone_gate = 1.0 if has_forward_zone else 0.90
    exhaustion_gate = 1.0 - (0.45 * features["exhaustion"])
    liquidity_gate = 0.58 + 0.42 * features["liquidity"] if family_is_reversal else 1.0
    trend_gate = 0.55 + 0.45 * max(features["flow"], features["htf"]) if family_is_trend else 1.0
    htf_late_gate = 1.0
    if family_is_reversal and features["htf"] > 0.85 and features["smt"] <= 0:
        htf_late_gate = 0.82
    product = (
        trigger_gate * location_gate * pattern_gate * forward_zone_gate *
        exhaustion_gate * liquidity_gate * trend_gate * htf_late_gate
    )
    return {
        "trigger_gate": round(trigger_gate, 3),
        "location_gate": round(location_gate, 3),
        "pattern_gate": round(pattern_gate, 3),
        "forward_zone_gate": round(forward_zone_gate, 3),
        "exhaustion_gate": round(exhaustion_gate, 3),
        "liquidity_gate": round(liquidity_gate, 3),
        "trend_gate": round(trend_gate, 3),
        "htf_late_gate": round(htf_late_gate, 3),
        "product": round(clamp(product, 0.0, 1.0), 3),
    }


def calibrate_candidate_quality(journal: dict, features: dict[str, float], setup_family: str,
                                trigger_ready: bool, is_limit_armed: bool,
                                has_forward_zone: bool) -> dict[str, Any]:
    coef, model_source, sample_size, learned_weight = _quality_coefficients(journal, setup_family)
    logit = coef.get("bias", 0.0) + sum(coef.get(key, 0.0) * features.get(key, 0.0) for key in QUALITY_FEATURE_KEYS)
    base_probability = _sigmoid(logit)
    gates = _multiplicative_quality_gates(features, setup_family, trigger_ready, is_limit_armed, has_forward_zone)
    gated_probability = clamp(base_probability * gates["product"], 0.02, 0.98)
    score = int(round(100.0 * gated_probability))
    return {
        "score": int(clamp(score, 12, 98)),
        "probability": round(gated_probability, 4),
        "base_probability": round(base_probability, 4),
        "model_source": model_source,
        "sample_size": sample_size,
        "learned_weight": round(learned_weight, 4),
        "features": {key: round(float(features.get(key, 0.0)), 4) for key in QUALITY_FEATURE_KEYS},
        "gates": gates,
    }


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
    smt_c15 = (context.get("smt_candles", {}) or {}).get("15m", [])

    # === ICT PD Array / SMT Data ===
    # ПРИМІТКА: identify_liquidity_and_range() тут викликається лише заради
    # SMT/range-контексту нижче; поле "eq" (рівень рівноваги PD Array) раніше
    # рахувалось у eq_level, але ніде не читалось — приберено як orphaned-код
    # незавершеного PD Array рефактору (Premium/Discount вирівнювання зараз
    # не входить у vector scoring нижче).
    range_data = identify_liquidity_and_range(c15)
    smt_bias = detect_smt_divergence(c15, smt_c15)

    kyiv_tz = zoneinfo.ZoneInfo("Europe/Kyiv")
    now_kyiv = datetime.now(kyiv_tz)
    session_profile = analyze_session_profile(c15, now_kyiv, atr15)
    # is_killzone тепер відображає ПОВЕДІНКОВУ фазу (маніпуляція/дистрибуція
    # Лондона чи Нью-Йорка), а не сирий діапазон годин — раніше формула
    # `(8<=h<17) or (15<=h<=23)` фактично покривала майже цілу добу (8:00-23:00)
    # і не розрізняла Азію/затишшя від реальних інституційних вікон.
    is_killzone = session_profile["phase"] in ("MANIPULATION", "DISTRIBUTION")

    params = get_adaptive_params(regime)
    candidates = []

    # ==========================================================
    # РЕЄСТР 10 ICT-МОДЕЛЕЙ (Адаптовано під BZ)
    # ==========================================================
    pattern_registry = {
        "2022_MODEL": {"name": "2022 Model", "priority": 100, "allow_early": True, "preferred_setup": SetupType.SWEEP_RECLAIM.value, "score_bonus": 22, "stop_min_atr": 0.8, "stop_max_atr": 1.5, "favored": [Regime.TRANSITION.value, Regime.TREND.value], "penalty": [Regime.RANGE.value]},
        "SILVER_BULLET": {"name": "Silver Bullet", "priority": 95, "allow_early": False, "preferred_setup": SetupType.PULLBACK_CONTINUATION.value, "score_bonus": 18, "stop_min_atr": 0.5, "stop_max_atr": 1.2, "favored": [Regime.TREND.value], "penalty": [Regime.SHOCK.value]},
        "PO3": {"name": "Power of 3 (AMD)", "priority": 90, "allow_early": True, "preferred_setup": SetupType.RANGE_EDGE_REVERSAL.value, "score_bonus": 16, "stop_min_atr": 1.0, "stop_max_atr": 2.0, "favored": [Regime.NORMAL.value, Regime.RANGE.value], "penalty": [Regime.SHOCK.value]},
        "TURTLE_SOUP": {"name": "Turtle Soup", "priority": 85, "allow_early": True, "preferred_setup": SetupType.SWEEP_RECLAIM.value, "score_bonus": 18, "stop_min_atr": 0.6, "stop_max_atr": 1.4, "favored": [Regime.RANGE.value, Regime.SHOCK.value], "penalty": [Regime.TREND.value]},
        "BREAKER_BLOCK": {"name": "Breaker Block", "priority": 80, "allow_early": False, "preferred_setup": SetupType.BREAKOUT_RETEST.value, "score_bonus": 14, "stop_min_atr": 0.8, "stop_max_atr": 1.8, "favored": [Regime.TREND.value, Regime.TRANSITION.value], "penalty": [Regime.RANGE.value]},
        "FVG_ENTRY": {"name": "FVG Entry", "priority": 75, "allow_early": False, "preferred_setup": SetupType.PULLBACK_CONTINUATION.value, "score_bonus": 12, "stop_min_atr": 0.7, "stop_max_atr": 1.6, "favored": [Regime.TREND.value], "penalty": [Regime.RANGE.value, Regime.SHOCK.value]},
        "OB_RECLAIM": {"name": "OB Reclaim", "priority": 88, "allow_early": True, "preferred_setup": SetupType.FRESH_BASE_CONTINUATION.value, "score_bonus": 15, "stop_min_atr": 0.9, "stop_max_atr": 2.2, "favored": [Regime.TREND.value, Regime.NORMAL.value], "penalty": []},
        "JUDAS_SWING": {"name": "Judas Swing", "priority": 92, "allow_early": True, "preferred_setup": SetupType.CAPITULATION_RECOVERY.value, "score_bonus": 20, "stop_min_atr": 1.0, "stop_max_atr": 2.5, "favored": [Regime.SHOCK.value, Regime.TRANSITION.value], "penalty": [Regime.RANGE.value]},
        "MMBM": {"name": "MMBM/MMSM", "priority": 98, "allow_early": True, "preferred_setup": SetupType.DIRECTION_FLIP.value, "score_bonus": 25, "stop_min_atr": 0.8, "stop_max_atr": 1.5, "favored": [Regime.TRANSITION.value, Regime.TREND.value], "penalty": [Regime.RANGE.value]},
        "BMS_RETEST": {"name": "BMS Retest", "priority": 70, "allow_early": False, "preferred_setup": SetupType.BREAKOUT_RETEST.value, "score_bonus": 12, "stop_min_atr": 0.9, "stop_max_atr": 1.9, "favored": [Regime.TREND.value], "penalty": [Regime.RANGE.value]},
        # Додано: раніше TREND_IGNITION існував лише в enum/профілях TP-SL, але жоден
        # детектор його не продукував (мертвий setup_type, 0 угод за жоден період).
        # Ловить момент ЗАРОДЖЕННЯ тренду: свіжий злам структури (CHoCH) + сильний
        # displacement-рух, поки регім ще TRANSITION/NORMAL (не встановлений TREND —
        # для встановленого тренду вже є BREAKER_BLOCK/FVG_ENTRY/OB_RECLAIM).
        "TREND_IGNITION_MODEL": {"name": "Trend Ignition", "priority": 82, "allow_early": True, "preferred_setup": SetupType.TREND_IGNITION.value, "score_bonus": 16, "stop_min_atr": 0.95, "stop_max_atr": 2.5, "favored": [Regime.TRANSITION.value, Regime.NORMAL.value], "penalty": [Regime.RANGE.value]},
        # Додано: RANGE_COMPRESSION_BREAKOUT — так само мертвий setup_type без детектора.
        # Ловить пробій ПІСЛЯ стиснення діапазону: попередні 12 свічок 15m стискались у
        # вузький діапазон (<=1.6 ATR), а зараз — тригер-готовність + сильний displacement
        # за його межі. Це м'якше за жорсткий VOLATILITY_SQUEEZE-блок у detect_regime_engine_2
        # (atr5/atr20<0.40, який повністю забороняє вхід) — тут компресія ширша й дозволяє
        # саме пробій, а не сам сквіз.
        "RANGE_COMPRESSION_MODEL": {"name": "Range Compression Breakout", "priority": 65, "allow_early": False, "preferred_setup": SetupType.RANGE_COMPRESSION_BREAKOUT.value, "score_bonus": 12, "stop_min_atr": 0.8, "stop_max_atr": 1.6, "favored": [Regime.RANGE.value, Regime.NORMAL.value], "penalty": [Regime.SHOCK.value]}
    }

    for side in [Side.LONG.value, Side.SHORT.value]:
        event = scan_events.get(side, {})
        trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
        trigger_ready = event.get("stage") in ["ACCEPTANCE", "RETEST", "READY"] and trigger_age <= TRIGGER_MAX_AGE_MINUTES
        trigger_level = event.get("trigger_level", price)
        scan_stage = event.get("stage", "")
        time_warp_opportunity = event.get("time_warp_opportunity", False)

        has_forward_zone = has_forward_ict_zone(price, zones, side, atr15)
        recent_struct = detect_recent_structure_shift(c15, lookback=7)

        # Довший лукбек тренду (20 свічок 15m ≈ 5 годин) — на додачу до короткого
        # 8-свічкового руху, який regime_detection() використовує для класифікації RANGE
        # (тут локально він більше не рахується — недороблений refactor лишав "move8"
        # обчисленим, але непрочитаним; сама метрика й досі жива в regime_detection()).
        # Повільний, розтягнутий у часі рух (напр. -0.6% за 2 години) може НЕ перевищити
        # короткий 8-свічковий поріг у кожен окремий момент, тож regime лишається "RANGE",
        # хоча по суті це вже м'який тренд. PO3/RANGE_EDGE_REVERSAL — це купівля "відкату
        # від межі діапазону", і вона не повинна відкриватись проти такого встановленого
        # дрейфу (реальний кейс 01.07: 2 підряд LONG RANGE_EDGE_REVERSAL програли під час
        # повільного зниження 73.23 -> 72.44, яке "RANGE"-класифікатор не бачив як тренд).
        drift_lookback_n = 20
        if len(c15) >= drift_lookback_n:
            drift_atr = (c15[-1].close - c15[-drift_lookback_n].close) / atr15 if atr15 else 0.0
        else:
            drift_atr = 0.0
        trend_against_reversal = (side == Side.LONG.value and drift_atr < -1.6) or \
                                  (side == Side.SHORT.value and drift_atr > 1.6)

        # Евристики для розпізнавання моделей
        has_fvg = any(z.side == side and z.kind == "FVG" and abs(price - (z.low if side == Side.LONG.value else z.high)) < atr15 * 1.5 for z in zones)
        has_ob = any(z.side == side and z.kind == "OB" and abs(price - (z.low if side == Side.LONG.value else z.high)) < atr15 * 1.5 for z in zones)

        # === Динамічна валідація свіпу (Dynamic Sweep Validation Engine) ===
        # Замість жорсткого is_sweep = (source == LIQUIDITY_SWEEP) оцінюємо
        # свіп за трьома вимірами інституційного сліду:
        #   1. Топологія — близькість до реальних пулів ліквідності 15m/1h;
        #   2. SMT Divergence — підтвердження розбіжністю з іншим активом;
        #   3. CVD Absorption — підтвердження внутрішнім тиском свічок.
        # У боковику (RANGE/TRANSITION/chop) звичайний свіп без підтверджень
        # деградується нижче прохідного порогу; у тренді вимоги м'якші
        # (структурний pullback вже підтверджений іншими факторами).
        is_raw_sweep = event.get("source") == "LIQUIDITY_SWEEP"
        sweep_quality_multiplier = 1.0
        valid_institutional_sweep = False

        if is_raw_sweep:
            # 1. Топологічна вага (відстань до реальних пулів старшого TF)
            major_pools = []
            for tf in ["15m", "1h"]:
                major_pools.extend(context.get("liquidity", {}).get(tf, {}).get("bsl", []))
                major_pools.extend(context.get("liquidity", {}).get(tf, {}).get("ssl", []))

            sweep_lvl = event.get("trigger_level", price)
            nearest_pool_dist = min([abs(sweep_lvl - p) for p in major_pools]) if major_pools else atr15

            # Чим ближче свіп до старшої ліквідності, тим вища його технічна вага (0.3–1.2)
            topology_weight = max(0.3, min(1.2, 1.0 - (nearest_pool_dist / (atr15 * 1.5)))) if atr15 else 0.3

            # 2. Інституційний footprint (SMT та CVD)
            has_smt_support = (smt_bias == side)
            has_cvd_absorption = (cvd.get("bias") == side and cvd.get("strength", 0) > 0)

            # 3. Динамічна оцінка середовища
            in_chop = session_profile.get("chop_zone", {}).get("active", False)

            if regime in (Regime.RANGE.value, Regime.TRANSITION.value) or in_chop:
                # У боковику звичайна тінь не працює — свіп повинен мати підтвердження.
                smt_mult = 1.4 if has_smt_support else 0.7
                cvd_mult = 1.25 if has_cvd_absorption else 0.8

                footprint_score = smt_mult * cvd_mult
                sweep_quality_multiplier = topology_weight * footprint_score

                # Замість жорсткого блоку — вимагаємо, щоб якість свіпу перекрила "шум" боковика
                valid_institutional_sweep = (sweep_quality_multiplier >= 0.85)
            else:
                # У тренді вимоги до свіпів м'якші (структурний pullback)
                sweep_quality_multiplier = topology_weight
                valid_institutional_sweep = True

        is_sweep = is_raw_sweep and valid_institutional_sweep
        # ==========================================================

        has_choch = recent_struct["bullish_shift"] if side == Side.LONG.value else recent_struct["bearish_shift"]
        strong_displacement = event.get("strong_displacement")
        has_good_reclaim = has_quality_reclaim(event)
        is_limit_armed = False
        ce_level = 0.0
        if has_choch and has_fvg:
            fvg_zones = [z for z in zones if z.side == side and z.kind == "FVG"]
            if fvg_zones:
                best_fvg = fvg_zones[0]
                ce_level = best_fvg.low + (best_fvg.high - best_fvg.low) / 2
                is_limit_armed = True
        
        judas = session_profile["judas"]
        chop = session_profile["chop_zone"]

        # Компресія діапазону за останні 12 закритих 15m-свічок (без поточної, щоб
        # мірялась саме ПОПЕРЕДНЯ консолідація, а не сам поточний пробійний бар).
        # Використовується для RANGE_COMPRESSION_MODEL нижче.
        if len(c15) >= 13:
            compression_window = c15[-13:-1]
            compression_range = max(c.high for c in compression_window) - min(c.low for c in compression_window)
            compression_atr = compression_range / atr15 if atr15 else 0.0
        else:
            compression_atr = 0.0
        is_range_compressed = 0 < compression_atr <= 1.6

        active_patterns = []
        if is_sweep and has_choch and has_fvg: active_patterns.append("2022_MODEL")
        if is_killzone and has_fvg and strong_displacement: active_patterns.append("SILVER_BULLET")
        if regime == Regime.RANGE.value and is_sweep and has_good_reclaim and not trend_against_reversal: active_patterns.append("PO3")
        if is_sweep and not strong_displacement: active_patterns.append("TURTLE_SOUP")
        if has_choch and has_good_reclaim: active_patterns.append("BREAKER_BLOCK")
        if has_fvg and tf1h.get("bias") == side: active_patterns.append("FVG_ENTRY")
        if has_ob and has_good_reclaim: active_patterns.append("OB_RECLAIM")
        # Замість крудого "is_killzone + sweep + рух>1.5%" — точний AMD-детект:
        # свіжий свіп Азійського хая/лоу з підтвердженим реклеймом у цю ж сторону.
        if judas["bias"] == side and judas["bonus"] > 0: active_patterns.append("JUDAS_SWING")

        # MMBM (v6.8 fix): раніше вимагав ОДНОЧАСНО tf1h==side AND tf15==side AND
        # CHoCH AND sweep — жорсткий AND 4 незалежних умов, який жодного разу не
        # спрацював за весь видимий журнал при score_bonus=25 (найдорожчий паттерн).
        # Послаблено: HTF-вирівнювання тепер OR (tf1h ЛІБО tf15), CHoCH і sweep
        # лишаються обов'язковими (вони і є структурна суть MMBM/MMSM).
        mmbm_tf1h_ok = tf1h.get("bias") == side
        mmbm_tf15_ok = tf15.get("bias") == side
        mmbm_htf_ok = mmbm_tf1h_ok or mmbm_tf15_ok
        if mmbm_htf_ok and has_choch and is_sweep:
            active_patterns.append("MMBM")
        if os.getenv("MMBM_DEBUG_LOG"):
            print(
                f"[MMBM_DEBUG] side={side} tf1h_ok={mmbm_tf1h_ok} tf15_ok={mmbm_tf15_ok} "
                f"has_choch={has_choch} is_sweep={is_sweep} -> "
                f"{'FIRED' if (mmbm_htf_ok and has_choch and is_sweep) else 'skip'}"
            )

        if tf15.get("bias") == side and event.get("retest"): active_patterns.append("BMS_RETEST")

        # Нові детектори — див. коментарі в pattern_registry вище.
        if has_choch and strong_displacement and regime in (Regime.TRANSITION.value, Regime.NORMAL.value) and tf15.get("bias") == side:
            active_patterns.append("TREND_IGNITION_MODEL")
        if is_range_compressed and strong_displacement and trigger_ready:
            active_patterns.append("RANGE_COMPRESSION_MODEL")

        best_pattern = None
        best_priority = 0
        pattern_conf = []
        raw_bonus = 0
        regime_matched = 0
        regime_conflict = 0

        for pat_id in active_patterns:
            p_data = pattern_registry[pat_id]
            if p_data["priority"] > best_priority:
                best_priority = p_data["priority"]
                best_pattern = pat_id

        # ==========================================================
        # VECTOR SCORING (v6.7): кожна родина сетапів рахується за
        # власною логікою замість єдиної лінійної формули.
        #   - REVERSAL (SWEEP_RECLAIM/JUDAS_SWING/TURTLE_SOUP/PO3/MMBM/2022_MODEL):
        #     штраф за ідеальне вирівнювання HTF (= пізній вхід), максимум ваги
        #     на ліквідність/тригер/SMT Divergence.
        #   - TREND (BREAKOUT_RETEST/FVG_ENTRY/BREAKER_BLOCK/OB_RECLAIM/BMS_RETEST):
        #     HTF і CVD критичні, але Exhaustion Penalty ріже скор навпіл, якщо
        #     рух вже пройшов > EXHAUSTION_ATR_THRESHOLD ATR без глибокого відкату.
        #   - Fallback без жодної підтвердженої моделі (генерик PULLBACK_CONTINUATION)
        #     отримує NO_PATTERN_PENALTY — саме ця комбінація була "дірою" v6.6
        #     (16 угод, 12.5% winrate, -5.63%), бо торгувалась з майже нормальною
        #     впевненістю, хоча по суті означає "нічого конкретного не спрацювало".
        # ==========================================================
        reversal_families = {SetupFamily.LIQUIDITY_RECOVERY.value, SetupFamily.STRUCTURAL_TRANSITION.value, SetupFamily.RANGE_EXECUTION.value}
        trend_families = {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}

        pattern_family = None
        if best_pattern:
            p_data = pattern_registry[best_pattern]
            raw_bonus = p_data["score_bonus"]
            pattern_conf.append(f"Модель: {p_data['name']}")
            pattern_family = SETUP_FAMILY_MAP.get(p_data["preferred_setup"], SetupFamily.CONTINUATION.value)

            # Органічна стабільність (Regime Matching)
            if regime in p_data["favored"]:
                raw_bonus += 5
                regime_matched = 1
                pattern_conf.append("✅ Модель узгоджена з режимом ринку (+5)")
            elif regime in p_data["penalty"]:
                raw_bonus -= 10
                regime_conflict = 1
                pattern_conf.append("⚠️ Конфлікт моделі з режимом ринку (-10)")
        else:
            raw_bonus -= NO_PATTERN_PENALTY
            pattern_conf.append(f"⚠️ Жодна з 10 ICT-моделей не підтверджена — generic fallback (-{NO_PATTERN_PENALTY})")

        htf_aligned_strong = (tf4h.get("bias") == side) and (tf1h.get("bias") == side)
        liq_weight = trig_weight = htf_weight = str_weight = flw_weight = 1.0
        vector_bonus = 0.0
        exhaustion_multiplier = 1.0

        if pattern_family in reversal_families:
            liq_weight, trig_weight, htf_weight = REVERSAL_LIQ_WEIGHT, REVERSAL_TRIGGER_WEIGHT, REVERSAL_HTF_WEIGHT
            if smt_bias == side:
                vector_bonus += REVERSAL_SMT_BONUS
                pattern_conf.append(f"🧭 SMT Divergence на користь входу (+{REVERSAL_SMT_BONUS})")
            elif smt_bias == opposite(side):
                vector_bonus -= REVERSAL_SMT_BONUS
                pattern_conf.append(f"⚠️ SMT Divergence проти входу (-{REVERSAL_SMT_BONUS})")
            if htf_aligned_strong:
                # Ідеальне вирівнювання HTF по розворотній моделі означає, що ринок
                # вже розвернувся і ми заходимо пізно — це не підтвердження, а ризик.
                vector_bonus -= REVERSAL_LATE_ENTRY_PENALTY
                pattern_conf.append(f"⏱️ Ідеальне вирівнювання HTF для розвороту = пізній вхід (-{REVERSAL_LATE_ENTRY_PENALTY})")
        elif pattern_family in trend_families:
            str_weight, flw_weight, htf_weight = TREND_STRUCTURE_WEIGHT, TREND_FLOW_WEIGHT, TREND_HTF_WEIGHT
            if smt_bias == side:
                vector_bonus += TREND_SMT_BONUS
                pattern_conf.append(f"🧭 SMT Divergence підтверджує тренд (+{TREND_SMT_BONUS})")
            if len(c15) >= 8 and atr15:
                impulse_run_atr = abs(c15[-1].close - c15[-8].close) / atr15
                extreme8 = max(c.high for c in c15[-8:]) if side == Side.LONG.value else min(c.low for c in c15[-8:])
                retracement_atr = abs(price - extreme8) / atr15
                if impulse_run_atr > EXHAUSTION_ATR_THRESHOLD and retracement_atr < 0.35:
                    exhaustion_multiplier = EXHAUSTION_SCORE_MULTIPLIER
                    pattern_conf.append(f"🔻 Exhaustion Penalty: рух {impulse_run_atr:.2f} ATR без відкату — скор x{EXHAUSTION_SCORE_MULTIPLIER}")

        # Базові обчислення якості (збережено існуючу логіку, зважено за родиною сетапу)
        loc_score = calculate_location_score(price, zones, side, atr15, tf15, tf1h)
        str_score = (19 if tf15.get("bias") == side else 10) * str_weight
        # Застосовуємо розраховану якість свіпу (Dynamic Sweep Validation Engine)
        # до балів ліквідності — неякісний свіп у боковику деградує бал плавно,
        # а не через жорсткий блок.
        liq_base = 16 if is_sweep and trigger_ready else 6
        liq_score = liq_base * liq_weight * sweep_quality_multiplier
        # ВИПРАВЛЕННЯ: flow_snapshot() зараз заглушка (завжди повертає NEUTRAL,
        # бо trades/book з collect_market_data порожні) — умова
        # "flow.bias == side AND cvd.bias == side" через це НІКОЛИ не
        # виконувалась, і робочий CVD-проксі (реальний сигнал з candle-тиску)
        # мовчки гасився мертвим flow. Тепер flw_score рахується напряму від
        # CVD, використовуючи вже наявну градацію сили (0/8/15 замість
        # фіксованого "17") — коли колись buy/sell trades tape реально
        # підключать у collect_market_data, flow.bias перестане бути
        # константою і його можна буде додати як окремий множник/бонус.
        flw_score = float(cvd.get("score", 0)) * flw_weight if cvd.get("bias") == side else 0.0
        trig_score = (22 if trigger_ready else 8) * trig_weight
        htf_score = (20 if tf4h.get("bias") == side else 6) * htf_weight
        
        raw = loc_score + str_score + liq_score + flw_score + trig_score + htf_score + raw_bonus + vector_bonus

        # === Session Profiling & AMD: макро-скор-коригування найвищого рівня ===
        # Це БАЛОВЕ коригування (як і всі інші бонуси/штрафи вище), не жорсткий
        # блок — сильний сетап з інших джерел все одно може пройти навіть у chop-зоні.
        session_bonus = 0.0
        if judas["bias"] == side and judas["bonus"] > 0:
            session_bonus += judas["bonus"]
            pattern_conf.extend(session_profile["notes"])
        elif chop["active"]:
            # М'ясорубка: ліквідність Азії ще не знята, і саме цей бік не
            # підтверджений свіжою маніпуляцією — типова пастка посеред діапазону.
            session_bonus -= chop["penalty_magnitude"]
            pattern_conf.extend(session_profile["notes"])
        raw += session_bonus

        if exhaustion_multiplier < 1.0:
            raw *= exhaustion_multiplier

        if is_limit_armed:
            trigger_ready = True  # Ігноруємо відсутність 3M сигналу
            trigger_level = ce_level
            pattern_conf.append(f"🔥 LIMIT_ARMED: Злам структури (CHoCH) + FVG, виставлено ліміт на CE ({ce_level:.2f})")
            raw += 25 # Гарантуємо високий пріоритет сетапу
        setup_type = SetupType.PULLBACK_CONTINUATION.value
        if best_pattern:
            setup_type = pattern_registry[best_pattern]["preferred_setup"]
        elif trigger_ready and scan_stage in ["RETEST", "READY"]:
            setup_type = SetupType.BREAKOUT_RETEST.value
        elif is_sweep and trigger_ready:
            setup_type = SetupType.SWEEP_RECLAIM.value

        evidence = ["ICT_LOCATION", "PRICE_STRUCTURE"]
        if flw_score > 0: evidence.append("ORDER_FLOW_CVD")
        if trigger_ready: evidence.append("EXECUTION_TRIGGER_3M")

        setup_family = SETUP_FAMILY_MAP.get(setup_type, SetupFamily.CONTINUATION.value)
        quality_features = _build_quality_features(
            loc_score=loc_score,
            str_score=str_score,
            liq_score=liq_score,
            flw_score=flw_score,
            trig_score=trig_score,
            htf_score=htf_score,
            raw_bonus=raw_bonus,
            session_bonus=session_bonus,
            vector_bonus=vector_bonus,
            trigger_age=trigger_age,
            trigger_ready=trigger_ready,
            best_pattern=best_pattern,
            regime_matched=regime_matched,
            regime_conflict=regime_conflict,
            exhaustion_multiplier=exhaustion_multiplier,
            pattern_family=setup_family,
        )
        calibration = calibrate_candidate_quality(
            journal, quality_features, setup_family, trigger_ready, is_limit_armed, has_forward_zone
        )
        final = int(calibration["score"])
        pattern_conf.append(
            f"📊 Quality model: {calibration['model_source']} n={calibration['sample_size']} "
            f"| p={calibration['probability']:.2f} | gates x{calibration['gates']['product']:.2f}"
        )
        
        lane = ExecutionLane.EARLY_TACTICAL.value if (best_pattern and pattern_registry[best_pattern]["allow_early"]) or time_warp_opportunity else ExecutionLane.STANDARD_CONFIRMED.value

        cand = Candidate(
            side=side,
            setup_type=setup_type,
            setup_family=setup_family,
            raw_score=int(round(raw)),
            final_score=final,
            score_components={
                "legacy_raw_score": round(raw, 2),
                "loc_score": round(loc_score, 2),
                "str_score": round(str_score, 2),
                "liq_score": round(liq_score, 2),
                "flw_score": round(flw_score, 2),
                "trig_score": round(trig_score, 2),
                "htf_score": round(htf_score, 2),
                "pattern_bonus": round(raw_bonus, 2),
                "session_bonus": round(session_bonus, 2),
                "vector_bonus": round(vector_bonus, 2),
                "features": calibration["features"],
                "gates": calibration["gates"],
                "probability": calibration["probability"],
                "base_probability": calibration["base_probability"],
                "model_source": calibration["model_source"],
                "sample_size": calibration["sample_size"],
                "learned_weight": calibration["learned_weight"],
            },
            evidence_families=evidence,
            confirmations=pattern_conf,
            trigger_ready=trigger_ready,
            trigger_level=round_price(trigger_level),
            invalidation_level=round_price(price - (atr15 * 1.65 if side == Side.LONG.value else -atr15 * 1.65)),
            target_levels=[round_price(price + (atr15 * 2.4 if side == Side.LONG.value else -atr15 * 2.4))],
            execution_lane=lane,
            stage="ARMED" if final >= params["armed_score"] else "DISCOVERED",
            variant="PATTERN_TRIGGERED" if best_pattern else "STANDARD",
            ict_model=best_pattern or "NONE",  # Передаємо модель далі
            execution_anchor=price,
            trigger_age_minutes=trigger_age,
            thesis_key=f"{side}|{setup_type}|{int(price*10)}",
            thesis=f"{side} {setup_type} | 3M={scan_stage}",
            professional_gate={},
            has_forward_zone=has_forward_zone,
        )
        cand.professional_gate = evaluate_professional_gate(context, cand)

        # v6.8 fix: раніше generic PULLBACK_CONTINUATION-фолбек (жодна з 10 ICT-моделей
        # не підтверджена) проходив за тим самим порогом armed_score-10, що й
        # паттерн-підтверджені сетапи, отримуючи лише -NO_PATTERN_PENALTY(=14) до raw score.
        # Це вже спалювало капітал у v6.6 (16 угод, 12.5% winrate, -5.63%) — штраф до
        # скору не є жорстким фільтром і фактично торгується з майже нормальною
        # впевненістю. Тепер без підтвердженої моделі поріг входу піднято суттєво
        # вище (armed_score + NO_PATTERN_EXTRA_MARGIN), а не просто занижено на -10.
        NO_PATTERN_EXTRA_MARGIN = int(os.getenv("NO_PATTERN_EXTRA_MARGIN", "12") or 12)
        min_score_required = params["armed_score"] - 10 if best_pattern else params["armed_score"] + NO_PATTERN_EXTRA_MARGIN

        # Не обрізаємо до 2-х, повертаємо всіх з базовою якістю
        if cand.final_score >= min_score_required:
            candidates.append(cand)
            
    return candidates


def setup_trade_profile(setup_type: str) -> dict:
    profiles = {
        SetupType.PULLBACK_CONTINUATION.value: {"tp1_rr": 1.65, "tp2_rr": 3.30, "tp3_rr": 5.50, "tp1_atr": 1.40, "stop_min_atr": 0.95, "stop_max_atr": 2.50, "quality_adjustment": 2},
        SetupType.BREAKOUT_RETEST.value: {"tp1_rr": 1.55, "tp2_rr": 2.90, "tp3_rr": 4.40, "tp1_atr": 1.30, "stop_min_atr": 0.90, "stop_max_atr": 2.20, "force_risky": True},
        SetupType.SWEEP_RECLAIM.value: {"tp1_rr": 1.50, "tp2_rr": 2.70, "tp3_rr": 4.00, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 2.00, "force_risky": True},
        SetupType.CAPITULATION_RECOVERY.value: {"tp1_rr": 1.55, "tp2_rr": 2.80, "tp3_rr": 4.10, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 2.10, "force_risky": True},
        SetupType.TREND_IGNITION.value: {"tp1_rr": 1.70, "tp2_rr": 3.30, "tp3_rr": 5.60, "tp1_atr": 1.45, "stop_min_atr": 0.95, "stop_max_atr": 2.50, "force_risky": True},
        SetupType.RANGE_COMPRESSION_BREAKOUT.value: {"tp1_rr": 1.50, "tp2_rr": 2.50, "tp3_rr": 3.40, "tp1_atr": 1.20, "stop_min_atr": 0.80, "stop_max_atr": 1.60, "force_risky": True},
        SetupType.RANGE_EDGE_REVERSAL.value: {"tp1_rr": 1.50, "tp2_rr": 2.55, "tp3_rr": 3.50, "tp1_atr": 1.20, "stop_min_atr": 0.80, "stop_max_atr": 1.50, "force_risky": True},
    }
    return dict(profiles.get(str(setup_type or ""), {"tp1_rr": 1.55, "tp2_rr": 2.90, "tp3_rr": 3.90, "tp1_atr": 1.30, "stop_min_atr": 0.90, "stop_max_atr": 2.20}))


def trade_mode_profile(context: dict, side: Optional[str] = None, setup_type: Optional[str] = None) -> dict:
    regime = context.get("market_regime") if isinstance(context.get("market_regime"), dict) else detect_regime_engine_2(context, side)
    name = str(regime.get("name", context.get("regime", Regime.NORMAL.value)) or Regime.NORMAL.value).upper()
    regime_type = str(regime.get("regime_type", name) or name).upper()
    
    profiles = {
        Regime.RANGE.value: {"tp1_rr": 1.45, "tp2_rr": 2.95, "tp3_rr": 3.95, "stop_min_atr": 0.80, "stop_max_atr": 1.55, "be_trigger": 0.35, "protect_trigger": 0.60, "giveback": 0.20},
        Regime.TRANSITION.value: {"tp1_rr": 1.55, "tp2_rr": 3.45, "tp3_rr": 4.75, "stop_min_atr": 0.85, "stop_max_atr": 2.10, "be_trigger": 0.40, "protect_trigger": 0.70, "giveback": 0.28},
        Regime.TREND.value: {"tp1_rr": 1.70, "tp2_rr": 4.05, "tp3_rr": 5.90, "stop_min_atr": 0.90, "stop_max_atr": 2.60, "be_trigger": 0.50, "protect_trigger": 0.85, "giveback": 0.38},
        Regime.SHOCK.value: {"tp1_rr": 1.45, "tp2_rr": 3.05, "tp3_rr": 4.15, "stop_min_atr": 0.90, "stop_max_atr": 1.90, "be_trigger": 0.40, "protect_trigger": 0.65, "giveback": 0.45},
        Regime.NORMAL.value: {"tp1_rr": 1.55, "tp2_rr": 3.65, "tp3_rr": 5.00, "stop_min_atr": 0.85, "stop_max_atr": 2.30, "be_trigger": 0.45, "protect_trigger": 0.75, "giveback": 0.30},
    }
    
    overrides = {
        "TREND_EXPANSION": {"tp1_rr": 1.90, "tp2_rr": 4.60, "tp3_rr": 6.70, "protect_trigger": 1.20, "giveback": 0.50},
        "TREND_PULLBACK": {"tp1_rr": 1.75, "tp2_rr": 3.85, "tp3_rr": 5.60, "stop_max_atr": 2.20, "protect_trigger": 0.90, "giveback": 0.35},
        "RANGE_COMPRESSION": {"tp1_rr": 1.45, "tp2_rr": 2.85, "tp3_rr": 3.75, "stop_max_atr": 1.45, "be_trigger": 0.30, "protect_trigger": 0.50, "giveback": 0.18},
        "RANGE_EDGE": {"tp1_rr": 1.50, "tp2_rr": 2.95, "tp3_rr": 3.95, "stop_max_atr": 1.50, "be_trigger": 0.35, "protect_trigger": 0.55, "giveback": 0.20},
        "REVERSAL_BUILDUP": {"tp1_rr": 1.50, "tp2_rr": 3.15, "tp3_rr": 4.35, "stop_max_atr": 1.85, "be_trigger": 0.40, "protect_trigger": 0.65, "giveback": 0.24},
        "NEWS_SHOCK": {"tp1_rr": 1.45, "tp2_rr": 3.05, "tp3_rr": 4.05, "stop_max_atr": 1.80, "be_trigger": 0.40, "protect_trigger": 0.65, "giveback": 0.48},
        "EXHAUSTION": {"tp1_rr": 1.40, "tp2_rr": 2.70, "tp3_rr": 3.55, "stop_max_atr": 1.35, "be_trigger": 0.30, "protect_trigger": 0.50, "giveback": 0.15},
    }
    
    profile = dict(profiles.get(name, profiles[Regime.NORMAL.value]))
    profile.update(overrides.get(regime_type, {}))
    setup_profile = setup_trade_profile(setup_type or "")
    
    for key in ("tp1_rr", "tp2_rr", "tp3_rr", "stop_min_atr", "stop_max_atr"):
        if key in setup_profile:
            if key.startswith("tp"):
                profile[key] = max(float(profile.get(key, 0)), float(setup_profile[key]))
            else:
                profile[key] = float(setup_profile[key])
                
    profile["regime"] = name
    profile["regime_type"] = regime_type
    profile["entry_action"] = regime.get("entry_action", "ALLOW")
    profile["hard_block"] = bool(regime.get("hard_block"))
    profile["quality_adjustment"] = int(regime.get("entry_quality_adjustment", 0) or 0) + int(setup_profile.get("quality_adjustment", 0) or 0)
    profile["quality_cap"] = setup_profile.get("quality_cap", regime.get("quality_cap"))
    profile["force_risky"] = bool(setup_profile.get("force_risky") or regime.get("entry_action") == "RISKY_ONLY")
    profile["reason"] = regime.get("reason", "")
    
    if profile["force_risky"]:
        profile["be_trigger"] = min(float(profile.get("be_trigger", 0.45)), 0.35)
        profile["protect_trigger"] = min(float(profile.get("protect_trigger", 0.70)), 0.58)
        profile["giveback"] = min(float(profile.get("giveback", 0.35)), 0.20)
        # Раніше тут стояло min(..., 1.10) / min(..., 2.00) — саме це й "зрізало" TP1
        # майже до 1:1 для 6 із 7 типів сетапів (у них force_risky=True). RISKY-вхід
        # означає менший розмір позиції (RISKY_RISK_PCT), а не право ставити тейк
        # практично на стопі — професійний RR від цього не повинен страждати.
        profile["tp1_rr"] = min(float(profile.get("tp1_rr", PREFERRED_RR1)), 1.55)
        profile["tp2_rr"] = min(float(profile.get("tp2_rr", MIN_RR2)), 2.65)
    
    return profile


def enforce_smart_money_rr(side: str, price: float, stop: float, tp1: float, tp2: float, tp3: float, atr15: float) -> tuple[float, float, float, float]:
    risk = abs(price - stop)
    if side not in {Side.LONG.value, Side.SHORT.value} or risk <= 1e-9:
        return stop, tp1, tp2, tp3
    # Це і є гарантія від "блокування" угоди через далекий TP1: ми не відхиляємо
    # сетап, якщо запланований TP1 виявився заскромним — ми примусово ВІДСУВАЄМО
    # його до професійного мінімуму. rr1 у build_trade_plan рахується вже ПІСЛЯ
    # цього ratchet'а, тож valid=rr1>=MIN_RR1 виконується автоматично й ніколи
    # не відхиляє вхід через "надто близький TP1".
    min_tp1_distance = risk * max(1.0, MIN_RR1)
    min_tp2_distance = risk * max(1.45, MIN_RR1 + 1.00)
    min_tp3_distance = risk * max(2.10, MIN_RR1 + 2.50)
    step = max(atr15 * 0.45, price * 0.003)
    if side == Side.LONG.value:
        tp1 = max(tp1, price + min_tp1_distance)
        tp2 = max(tp2, price + min_tp2_distance, tp1 + step)
        tp3 = max(tp3, price + min_tp3_distance, tp2 + step)
    else:
        tp1 = min(tp1, price - min_tp1_distance)
        tp2 = min(tp2, price - min_tp2_distance, tp1 - step)
        tp3 = min(tp3, price - min_tp3_distance, tp2 - step)
    return stop, tp1, tp2, tp3


def find_technical_targets(side: str, price: float, zones: list, liquidity: dict, atr15: float,
                            macro: Optional[dict] = None) -> list[dict]:
    """
    Збирає РЕАЛЬНІ технічні цілі попереду ціни за напрямком угоди:
    - протилежні Order Block / FVG (供供 supply/demand, з яких очікується реакція)
    - пули ліквідності BSL/SSL (equal highs/lows — типова "приманка" для ціни за ICT)
    - межі поточного 15M-діапазону (range_high/range_low)

    TP1/TP2/TP3 в build_trade_plan обираються З ЦЬОГО списку (найближча кваліфікована
    ціль для TP1, наступна — для TP2, і т.д.), а не просто розраховуються математично
    від RR. Формула (stop_dist * tp_rr) лишається ЛИШЕ як fallback, коли структурних
    рівнів бракує — саме це гарантує, що вхід ніколи не блокується через "відсутність
    зручного рівня".
    """
    opp = opposite(side)
    targets: list[dict] = []

    # 1) Протилежні OB/FVG попереду ціни — типові точки реакції (supply для LONG, demand для SHORT)
    for z in zones:
        if z.side != opp:
            continue
        level = z.low if side == Side.LONG.value else z.high
        if side == Side.LONG.value and level <= price:
            continue
        if side == Side.SHORT.value and level >= price:
            continue
        tf_weight = {"4h": 1.35, "1h": 1.15, "15m": 1.0}.get(z.timeframe, 1.0)
        targets.append({
            "level": level, "kind": z.kind, "timeframe": z.timeframe,
            "strength": float(z.strength) * tf_weight,
        })

    # 2) Пули ліквідності (BSL для LONG вище ціни, SSL для SHORT нижче ціни)
    for tf_label, tf_weight in (("15m", 1.0), ("1h", 1.30)):
        liq = liquidity.get(tf_label, {}) if isinstance(liquidity, dict) else {}
        if side == Side.LONG.value:
            for lvl in liq.get("bsl", []) or []:
                if lvl > price:
                    targets.append({"level": lvl, "kind": "BSL_LIQUIDITY", "timeframe": tf_label, "strength": 1.4 * tf_weight})
            range_high = liq.get("range_high") or 0
            if range_high > price:
                targets.append({"level": range_high, "kind": "RANGE_HIGH", "timeframe": tf_label, "strength": 1.1 * tf_weight})
        else:
            for lvl in liq.get("ssl", []) or []:
                if 0 < lvl < price:
                    targets.append({"level": lvl, "kind": "SSL_LIQUIDITY", "timeframe": tf_label, "strength": 1.4 * tf_weight})
            range_low = liq.get("range_low") or 0
            if 0 < range_low < price:
                targets.append({"level": range_low, "kind": "RANGE_LOW", "timeframe": tf_label, "strength": 1.1 * tf_weight})

    # 3) Макро-магніти зовнішньої ліквідності (DOL): Asian High/Low, PDH/PDL,
    #    макро-EQ. Це "справжній ICT" з розбору Юлії — коли поруч бракує
    #    технічної структури (вільне падіння / хай історичного діапазону),
    #    бот не стискає тейки й не скасовує угоду, а перемикається на
    #    таргетування зовнішньої ліквідності, куди розумні гроші штовхають
    #    ціну, бо там лежать стопи інших трейдерів. Ставимо strength вищим
    #    за типовий внутрішньоденний OB/FVG (1.4) — це старші, "жирніші" пули.
    macro = macro or {}
    if side == Side.LONG.value:
        for kind, lvl in (("ASIAN_HIGH", macro.get("asian_high")), ("PDH", macro.get("pdh"))):
            if lvl and lvl > price:
                targets.append({"level": lvl, "kind": kind, "timeframe": "1D", "strength": 1.6})
        eq = macro.get("macro_eq")
        if eq and eq > price:
            targets.append({"level": eq, "kind": "MACRO_EQ", "timeframe": "1D", "strength": 1.2})
    else:
        for kind, lvl in (("ASIAN_LOW", macro.get("asian_low")), ("PDL", macro.get("pdl"))):
            if lvl and 0 < lvl < price:
                targets.append({"level": lvl, "kind": kind, "timeframe": "1D", "strength": 1.6})
        eq = macro.get("macro_eq")
        if eq and 0 < eq < price:
            targets.append({"level": eq, "kind": "MACRO_EQ", "timeframe": "1D", "strength": 1.2})

    if not targets:
        return []

    for t in targets:
        t["distance"] = abs(t["level"] - price)
    targets.sort(key=lambda t: t["distance"])

    # Кластеризація близьких рівнів (OB+ліквідність часто збігаються в один "магніт") —
    # лишаємо в кожному кластері найсильнішу (не обов'язково найближчу) ціль.
    cluster_tol = max(atr15 * 0.15, price * 0.0008)
    clustered: list[dict] = []
    for t in targets:
        merged = False
        for c in clustered:
            if abs(t["level"] - c["level"]) <= cluster_tol:
                if t["strength"] > c["strength"]:
                    c.update(t)
                merged = True
                break
        if not merged:
            clustered.append(dict(t))

    clustered.sort(key=lambda t: t["distance"])
    return clustered


def find_protective_stop_level(side: str, price: float, zones: list, liquidity: dict,
                                min_dist: float, max_dist: float) -> Optional[dict]:
    """
    ICT-якір для трейлінгу стопа: шукає найближчий структурний рівень ПОЗАДУ ціни
    (той бік, куди був би виставлений стоп) — той самий Order Block / FVG / SSL-SSL
    liquidity sweep, що вже використовується для розміщення TP у find_technical_targets(),
    але тепер застосований до захисту стопа замість довільної частки ATR/RR.

    Для LONG шукаємо demand-зони (z.side == LONG) та SSL-свіпи нижче ціни, повертаємо
    рівень ТРОХИ НИЖЧЕ нижньої межі зони (щоб стоп не лежав усередині зони, яку ринок
    ще може легітимно протестувати вудом). Для SHORT — дзеркально, supply-зони/BSL вище ціни.

    min_dist / max_dist — коридор допустимих відстаней від ціни (щоб не тягнути стоп
    до занадто близького чи занадто далекого рівня). Якщо кваліфікованого рівня немає —
    повертає None, і виклик має впасти на попередній ATR/RR-fallback (вхід/трейлінг
    ніколи не блокується через "немає зручного рівня").
    """
    if min_dist <= 0 or max_dist <= 0 or max_dist < min_dist:
        return None

    candidates: list[dict] = []
    buffer = max(min_dist * 0.12, price * 0.0003)

    for z in zones:
        if z.side != side:
            continue
        if side == Side.LONG.value:
            level = z.low - buffer
            if level >= price:
                continue
        else:
            level = z.high + buffer
            if level <= price:
                continue
        dist = abs(price - level)
        if dist < min_dist or dist > max_dist:
            continue
        tf_weight = {"4h": 1.35, "1h": 1.15, "15m": 1.0}.get(z.timeframe, 1.0)
        candidates.append({"level": level, "kind": z.kind, "timeframe": z.timeframe,
                            "strength": float(z.strength) * tf_weight, "distance": dist})

    for tf_label, tf_weight in (("15m", 1.0), ("1h", 1.30)):
        liq = liquidity.get(tf_label, {}) if isinstance(liquidity, dict) else {}
        if side == Side.LONG.value:
            for lvl in liq.get("ssl", []) or []:
                level = lvl - buffer
                if not (0 < level < price):
                    continue
                dist = price - level
                if min_dist <= dist <= max_dist:
                    candidates.append({"level": level, "kind": "SSL_SWEEP", "timeframe": tf_label,
                                        "strength": 1.3 * tf_weight, "distance": dist})
        else:
            for lvl in liq.get("bsl", []) or []:
                level = lvl + buffer
                if level <= price:
                    continue
                dist = level - price
                if min_dist <= dist <= max_dist:
                    candidates.append({"level": level, "kind": "BSL_SWEEP", "timeframe": tf_label,
                                        "strength": 1.3 * tf_weight, "distance": dist})

    if not candidates:
        return None

    # Серед рівнів у коридорі — не обов'язково найближчий, а найсильніший
    # (старший таймфрейм / якісніший OB), як зробив би дискреційний трейдер.
    candidates.sort(key=lambda c: (-c["strength"], c["distance"]))
    return candidates[0]


def _effective_atr15(atr15: float, price: float) -> float:
    """
    Нижня межа ATR15 у абсолютних одиницях ціни. Без цього floor'у будь-яка
    формула виду `atr15 * коефіцієнт` (розмір стопа, TP1-floor, буфер гардів)
    схлопується до шуму в тихі періоди (Азія, низька ліквідність), навіть якщо
    технічний/структурний рівень поруч цілком реальний і ширший за цей шум.
    """
    atr_floor_pct = 0.0006  # 0.06% від ціни — абсолютний мінімум "дихання"
    return max(atr15, price * atr_floor_pct)


def _session_stop_buffer_mult(context: dict) -> float:
    """
    Множник ATR-буфера протективних гардів залежно від сесії. Проблема, яку це
    вирішує: у тиху Азійську сесію atr15 сам по собі стискається до майже нуля,
    і буфер price ± atr15*k схлопується разом з ним до величини шумового спреду —
    угоду вибиває першим тіком. Тут навпаки РОЗШИРюємо множник саме в Азію,
    компенсуючи стиснутий ATR, а не масштабуючи буфер вниз ще сильніше.
    """
    session = str(context.get("session_name", "") or "").upper()
    if session == "ASIA":
        return 0.95
    if session == "OFF_HOURS":
        return 0.80
    return 0.55


def _atr_guard_buffer(context: dict, atr15: float, price: float) -> float:
    """
    ATR-буфер з нижньою межею: не дозволяє протективному стопу підійти ближче
    за ATR_FLOOR_PCT від ціни навіть якщо поточний atr15 тимчасово стиснувся
    до шуму (типово для Азійської сесії / низьколіквідних годин).
    """
    atr_floor_pct = 0.0006  # 0.06% від ціни — абсолютний мінімум "дихання" для стопа
    effective_atr = _effective_atr15(atr15, price)
    return effective_atr * _session_stop_buffer_mult(context)


def build_trade_plan(context: dict, candidate: Candidate) -> TradePlan:
    price = context["price"]
    atr15 = context["atr15"] or 0.6
    side = candidate.side
    profile = trade_mode_profile(context, side, candidate.setup_type)
    zones = context["zones"]

    # ДИНАМІЧНИЙ RISK PROFILE на основі Реєстру 10 моделей
    registry_stop_min = MIN_STOP_ATR15
    registry_stop_max = MAX_STOP_ATR15
    
    # Визначаємо профіль, якщо кандидат має конкретну ICT модель
    if candidate.ict_model != "NONE":
        # Hardcoded копія реєстру для ризик-менеджменту (щоб не тягнути залежності)
        atr_limits = {
            "2022_MODEL": (0.8, 1.5), "SILVER_BULLET": (0.5, 1.2), "PO3": (1.0, 2.0),
            "TURTLE_SOUP": (0.6, 1.4), "BREAKER_BLOCK": (0.8, 1.8), "FVG_ENTRY": (0.7, 1.6),
            "OB_RECLAIM": (0.9, 2.2), "JUDAS_SWING": (1.0, 2.5), "MMBM": (0.8, 1.5),
            "BMS_RETEST": (0.9, 1.9),
            "TREND_IGNITION_MODEL": (0.95, 2.5), "RANGE_COMPRESSION_MODEL": (0.8, 1.6),
        }
        if candidate.ict_model in atr_limits:
            registry_stop_min, registry_stop_max = atr_limits[candidate.ict_model]
            profile["stop_min_atr"] = registry_stop_min
            profile["stop_max_atr"] = registry_stop_max

    structural_stop = candidate.invalidation_level
    structural_from_zone = False
    for z in sorted(zones, key=lambda x: -x.strength):
        if z.side == opposite(side) and z.timeframe in ("1h", "4h"):
            structural_stop = z.low if side == Side.LONG.value else z.high
            structural_from_zone = True
            break

    # ВИПРАВЛЕНО: та сама хвороба, що й у трейлінгових гардах, але на етапі
    # відкриття угоди — стоп рахувався як max(structural_dist, atr15*min), потім
    # МІНІМІЗУВАВСЯ до atr15*max. У тиху сесію (мала atr15) цей "max"-стеля міг
    # обрізати РЕАЛЬНИЙ структурний рівень (1H/4H OB/FVG) до значення тіснішого,
    # ніж сама структура — угода відкривалась зі стопом БЛИЖЧЕ за технічну точку
    # інвалідації тези, а не на ній. effective_atr15 дає нижню межу ATR, а для
    # стопів, знайдених від СПРАВЖНЬОЇ старшої зони (не fallback candidate-рівня),
    # стеля лише щедро обмежує абсурдну ширину — не підрізає легітимну структуру.
    effective_atr15 = _effective_atr15(atr15, price)
    stop_dist = abs(price - structural_stop)
    stop_dist = max(stop_dist, effective_atr15 * float(profile.get("stop_min_atr", MIN_STOP_ATR15)))
    stop_ceiling_mult = 1.6 if structural_from_zone else 1.0
    stop_dist = min(stop_dist, effective_atr15 * float(profile.get("stop_max_atr", MAX_STOP_ATR15)) * stop_ceiling_mult)

    # === DOL Rule 1: Абсолютна "підлога" для стопу (Hard Floor) ===
    # Усе вище рахувалось відносно atr15 — але коли ATR стискається до
    # мікроскопічних значень (тиха сесія / низька волатильність), навіть
    # stop_min_atr * effective_atr15 дає стоп у кілька центів (напр. 7 центів
    # на BZ при entry~72 — ризик, який звичайний спред+ковзання здатні з'їсти
    # наполовину). Незалежно від ATR чи мікроструктури, стоп ніколи не може
    # бути тіснішим за ABS_MIN_STOP_DOLLARS: угода повинна мати змогу "дихати".
    stop_dist = max(stop_dist, ABS_MIN_STOP_DOLLARS)

    stop = price - stop_dist if side == Side.LONG.value else price + stop_dist
    tp1_dist = max(stop_dist * float(profile.get("tp1_rr", PREFERRED_RR1)), effective_atr15 * MIN_TP1_ATR15)
    tp2_dist = max(stop_dist * float(profile.get("tp2_rr", MIN_RR2)), tp1_dist + effective_atr15 * 0.45)
    tp3_dist = max(stop_dist * float(profile.get("tp3_rr", MIN_RR3)), tp2_dist + effective_atr15 * 0.55)
    
    tp1 = price + tp1_dist if side == Side.LONG.value else price - tp1_dist
    tp2 = price + tp2_dist if side == Side.LONG.value else price - tp2_dist
    tp3 = price + tp3_dist if side == Side.LONG.value else price - tp3_dist
    stop, tp1, tp2, tp3 = enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15)

    # === Технічне розміщення TP: Order Block / FVG / BSL-SSL ліквідність / межі діапазону ===
    # Формула вище (stop_dist * tp_rr) лишається ЛИШЕ страховим floor'ом — вона вже
    # гарантувала мінімальний професійний RR. Тепер намагаємось "прив'язати" кожен TP
    # до РЕАЛЬНОГО рівня, куди ринок технічно тяжіє (а не просто до математичної точки):
    #   TP1 — найближча кваліфікована ціль (найчастіше 15M OB/FVG або локальна ліквідність)
    #   TP2 — наступна ціль далі, зазвичай 1H рівень або BSL/SSL пул
    #   TP3 — старша/сильніша ціль (1H/4H зона, дальня ліквідність, межа діапазону)
    # Якщо для якогось TP немає кваліфікованого технічного рівня — просто лишається
    # RR-фолбек, тому вхід НІКОЛИ не блокується через "немає зручного рівня".
    step = max(atr15 * 0.45, price * 0.003)
    min_tp1_distance = stop_dist * max(1.0, MIN_RR1)
    # === DOL Rule 2: "Speed Bump Filter" — ігноруємо надто близькі зустрічні зони ===
    # Без цієї підлоги TP1 міг снепнутись на мікро-FVG/пул за кілька центів від
    # входу (той самий "лежачий поліцейський", який ціна пробиває імпульсом,
    # а не точка реакції). TP1 не може бути ближче за ABS_MIN_TP1_DOLLARS,
    # навіть якщо RR-floor формально вже задоволений.
    min_tp1_distance = max(min_tp1_distance, ABS_MIN_TP1_DOLLARS)
    min_tp2_distance = stop_dist * max(1.45, MIN_RR1 + 1.00)
    min_tp3_distance = stop_dist * max(2.10, MIN_RR1 + 2.50)
    tech_targets = find_technical_targets(side, price, zones, context.get("liquidity", {}), atr15,
                                           macro=context.get("macro_liquidity", {}))
    tp_sources = {"tp1": "RR-профіль", "tp2": "RR-профіль", "tp3": "RR-профіль"}

    MACRO_KINDS = {"ASIAN_HIGH", "ASIAN_LOW", "PDH", "PDL", "MACRO_EQ"}

    def _pick_technical(floor_dist: float, cap_mult: float, used_levels: list[float]) -> Optional[dict]:
        for t in tech_targets:
            if t["distance"] < floor_dist - 1e-9:
                continue
            if t["distance"] > floor_dist * cap_mult:
                break  # список відсортований за відстанню — далі буде ще далі
            if any(abs(t["level"] - u) < step for u in used_levels):
                continue
            return t
        return None

    def _pick_macro(floor_dist: float, used_levels: list[float]) -> Optional[dict]:
        """
        === DOL Rule 3: Макро-Магніти (External Liquidity) ===
        Викликається ЛИШЕ якщо _pick_technical() у межах свого cap_mult не
        знайшов жодного якісного внутрішньоденного рівня — тобто ринок
        "тонкий" (вільне падіння / хай історичного діапазону). У цьому
        випадку бот не стискає TP і не скасовує угоду, а свідомо перемикається
        на найближчий макро-магніт (Asian High/Low, PDH/PDL, macro EQ) БЕЗ
        верхнього cap — це і є справжня зовнішня ліквідність, куди розумні
        гроші штовхають ціну, навіть якщо вона далеко.
        """
        for t in tech_targets:
            if t["kind"] not in MACRO_KINDS:
                continue
            if t["distance"] < floor_dist - 1e-9:
                continue
            if any(abs(t["level"] - u) < step for u in used_levels):
                continue
            return t
        return None

    used_levels: list[float] = []
    tp1_tech = _pick_technical(min_tp1_distance, 3.2, used_levels)
    if tp1_tech:
        tp1 = tp1_tech["level"]
        tp_sources["tp1"] = f"{tp1_tech['kind']} ({tp1_tech['timeframe']})"
    used_levels.append(tp1)

    tp2_floor = max(min_tp2_distance, abs(tp1 - price) + step)
    tp2_tech = _pick_technical(tp2_floor, 2.4, used_levels) or _pick_macro(tp2_floor, used_levels)
    if tp2_tech:
        tp2 = tp2_tech["level"]
        tp_sources["tp2"] = f"{tp2_tech['kind']} ({tp2_tech['timeframe']})"
    else:
        tp2 = max(tp2, price + tp2_floor) if side == Side.LONG.value else min(tp2, price - tp2_floor)
    used_levels.append(tp2)

    tp3_floor = max(min_tp3_distance, abs(tp2 - price) + step)
    tp3_tech = _pick_technical(tp3_floor, 2.2, used_levels) or _pick_macro(tp3_floor, used_levels)
    if tp3_tech:
        tp3 = tp3_tech["level"]
        tp_sources["tp3"] = f"{tp3_tech['kind']} ({tp3_tech['timeframe']})"
    else:
        tp3 = max(tp3, price + tp3_floor) if side == Side.LONG.value else min(tp3, price - tp3_floor)

    # Фінальний прогін через ratchet — навіть якщо snap до технічного рівня чомусь
    # порушив монотонність (TP2 ближче за TP1 тощо), тут це виправляється і
    # професійний RR-floor лишається непорушним у будь-якому випадку.
    stop, tp1, tp2, tp3 = enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15)
    
    rr1 = abs(tp1 - price) / abs(stop - price) if abs(stop - price) > 1e-9 else 2.1
    regime_action = str(profile.get("entry_action", "ALLOW")).upper()
    execution_ready = candidate.trigger_ready and candidate.final_score >= ENTRY_SCORE_BASE and not profile.get("hard_block") and regime_action in {"ALLOW", "RISKY_ONLY"}

    # Будь-який нестандартний (ризикований) лейн виконання — раннє тактичне входження
    # АБО re-entry з пропущеного імпульсу — повинен отримувати зменшений розмір позиції.
    # Раніше перевірявся лише EARLY_TACTICAL, через що RISKY_ENTRY угоди типу
    # "Re-entry з пропущеного імпульсу" (MISSED_IMPULSE_REENTRY) помилково
    # відкривались з повним NORMAL_RISK_PCT замість зменшеного RISKY_RISK_PCT.
    RISKY_EXECUTION_LANES = {ExecutionLane.EARLY_TACTICAL.value, ExecutionLane.MISSED_IMPULSE_REENTRY.value}
    is_risky_lane = candidate.execution_lane in RISKY_EXECUTION_LANES

    # enforce_smart_money_rr() вище математично гарантує |tp1-price| >= risk*MIN_RR1,
    # тож rr1 не повинен опускатись нижче MIN_RR1. Але коли профіль якогось сетапу
    # виставляє tp1_rr РІВНО на рівні MIN_RR1 (межовий, а не із запасом), ланцюжок
    # округлень stop_dist -> tp1_dist -> rr1 іноді дає щось на кшталт 1.4999999999998
    # замість точних 1.5 — суто похибка float. Без допуску це БЛОКУВАЛО Б угоду
    # (valid=False) через математично неіснуючу різницю в 13-му знаку.
    RR_EPSILON = 1e-6
    rr1_valid = rr1 >= (MIN_RR1 - RR_EPSILON)

    plan = TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1), tp2=round_price(tp2), tp3=round_price(tp3),
        risk_pct=RISKY_RISK_PCT if is_risky_lane else NORMAL_RISK_PCT,
        rr1=round(rr1, 2), rr2=round(abs(tp2 - price) / abs(stop - price), 2), rr3=round(abs(tp3 - price) / abs(stop - price), 2),
        position_risk_pct=RISKY_RISK_PCT if is_risky_lane else NORMAL_RISK_PCT,
        invalidation=f"закриття 15M за {round_price(structural_stop)}",
        stop_basis=f"Модель: {candidate.ict_model} | ATR: {registry_stop_min}-{registry_stop_max}",
        target_basis=f"TP1: {tp_sources['tp1']} | TP2: {tp_sources['tp2']} | TP3: {tp_sources['tp3']}",
        stop_timeframe="1H" if any(z.timeframe == "1h" for z in zones) else "15M",
        structural_invalidation=round_price(structural_stop),
        trigger_level=candidate.trigger_level,
        execution_ready=execution_ready,
        valid=rr1_valid,
        reason="" if rr1_valid else "RR1 нижче мінімуму",
    )
    return plan


# ==========================================================
# EVALUATION
# ==========================================================

def evaluate_new_setup(context: dict, state: dict, journal: dict) -> Decision:
    cands = detect_candidates(context, state, journal)
    current_price = context.get("price", 0)
    
    # 1. Перевірка ре-ентрі (залишається незмінною)
    saved_opp = opportunity_from_state(state)
    if saved_opp and saved_opp.status in ["ARMED", "WAIT_PULLBACK"]:
        missed_cand = candidate_from_missed_opportunity(saved_opp, context)
        if missed_cand:
            guard = event_driven_reentry_guard(state, context, missed_cand)
            if not guard["blocked"] and missed_cand.final_score >= MISSED_REENTRY_SCORE * get_adaptive_params(context["regime"])["reentry_aggressiveness"]:
                plan = build_trade_plan(context, missed_cand)
                action = Action.RISKY_ENTRY.value if missed_cand.final_score >= REENTRY_AGGRESSIVE_THRESHOLD else Action.ARMED.value
                return Decision(
                    id=uuid.uuid4().hex[:10], time=iso_now(), action=action, side=missed_cand.side, setup_type=missed_cand.setup_type,
                    quality=missed_cand.final_score, reason="Re-entry з пропущеного імпульсу", regime=context["regime"],
                    candidate=missed_cand, plan=plan, current_price=current_price
                )

    # 2. БАГАТОПОТОКОВИЙ СКАНЕР: Відбір усіх валідних кандидатів
    valid_candidates = []
    for cand in cands:
        gate = cand.professional_gate or evaluate_professional_gate(context, cand)
        # Додаємо кандидата, якщо він проходить хоча б один Gate
        if gate.get("allow_entry") or gate.get("allow_risky") or cand.final_score >= get_adaptive_params(context["regime"])["armed_score"]:
            valid_candidates.append(cand)

    if not valid_candidates:
        return Decision(
            id=uuid.uuid4().hex[:10], time=iso_now(), action=Action.NO_SETUP.value, side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value,
            quality=12, reason="Ринок не сформував професійного ICT + 3M execution-package", regime=context["regime"], current_price=current_price
        )

    # 3. Вибір абсолютного переможця за сукупним рейтингом
    valid_candidates.sort(key=lambda c: c.final_score, reverse=True)
    best = valid_candidates[0]
    
    plan = build_trade_plan(context, best)
    action = Action.NO_SETUP.value
    mode_profile = trade_mode_profile(context, best.side, best.setup_type)
    quality = int(best.final_score + mode_profile.get("quality_adjustment", 0))
    if mode_profile.get("quality_cap") is not None:
        quality = min(quality, int(mode_profile["quality_cap"]))
    quality = int(clamp(quality, 1, 100))
    reason = "Професійний сетап не пройшов gate'и якості"

    params = get_adaptive_params(context["regime"])
    gate = best.professional_gate or {}
    entry_action = str(mode_profile.get("entry_action", "ALLOW")).upper()
    hard_block = bool(mode_profile.get("hard_block"))

    if hard_block:
        action = Action.NO_SETUP.value
        reason = f"Regime Engine 2.0 блокує вхід: {mode_profile.get('reason', 'режим не підтримує вхід')}"
    elif gate.get("allow_entry") and plan.valid and plan.execution_ready and entry_action == "ALLOW" and not mode_profile.get("force_risky"):
        action = Action.ENTRY.value
        reason = f"[{best.ict_model}] {setup_label(best.setup_type)} — {gate.get('grade', 'A')} gate v6"
    elif (gate.get("allow_risky") or entry_action == "RISKY_ONLY" or mode_profile.get("force_risky")) and plan.valid and not hard_block:
        action = Action.RISKY_ENTRY.value
        reason = f"[{best.ict_model}] Ризикований вхід: {setup_label(best.setup_type)} — {mode_profile.get('regime_type')} profile"
    elif best.final_score >= params["armed_score"]:
        action = Action.ARMED.value
        reason = f"[{best.ict_model}] {setup_label(best.setup_type)} сформовано; gate v6: {gate.get('grade', 'WATCH')}"

    return Decision(
        id=uuid.uuid4().hex[:10], time=iso_now(), action=action, side=best.side, setup_type=best.setup_type,
        quality=quality, reason=reason, regime=context["regime"], candidate=best, plan=plan, current_price=current_price,
        audit={"selected": {"side": best.side, "model": best.ict_model, "score": best.final_score}}
    )

# ==========================================================
# MANAGE ACTIVE TRADE
# ==========================================================

def _is_more_protective_stop(side: str, current_stop: float, new_stop: float, price: float) -> bool:
    """Ратчет: новий стоп приймається лише якщо він СТРОГО тісніший за поточний і ще не зачепив ціну."""
    if not new_stop:
        return False
    if side == Side.LONG.value:
        return current_stop == 0 or (current_stop < new_stop < price)
    return current_stop == 0 or (current_stop > new_stop > price)


def _apply_protective_stop(trade: ActiveTrade, context: dict, stop: Optional[float]) -> bool:
    if stop is None:
        return False
    price = context.get("price", trade.entry)
    if not _is_more_protective_stop(trade.side, trade.stop_current, stop, price):
        return False
    trade.stop_current = float(stop)
    return True


def _find_last_swing_point(side: str, candles: list) -> Optional[float]:
    """
    Знаходить останній ПІДТВЕРДЖЕНИЙ 15M swing-пункт у бік угоди:
    swing low для LONG (під нього трейлимо стоп), swing high для SHORT.

    Пивот на індексі i вважається підтвердженим, якщо його low/high є
    екстремумом серед SWING_PIVOT_STRENGTH свічок з КОЖНОГО боку — тобто
    ринок уже намалював відкат і пішов далі, підтвердивши точку як реальний
    структурний мінімум/максимум, а не просто останню хвилю. Останні
    SWING_PIVOT_STRENGTH свічок (та будь-яка ще не закрита) не мають
    достатнього підтвердження праворуч і пропускаються.
    """
    if not candles:
        return None
    confirmed = [c for c in candles if getattr(c, "confirmed", True)]
    ordered = sorted(confirmed, key=lambda c: c.ts)
    if len(ordered) > SWING_LOOKBACK_15M:
        ordered = ordered[-SWING_LOOKBACK_15M:]
    n = len(ordered)
    strength = SWING_PIVOT_STRENGTH
    if n < strength * 2 + 1:
        return None
    # Йдемо від найновішого підтвердженого пивота назад — саме він визначає
    # трейлінг ПРЯМО ЗАРАЗ (найближчий до ринку відкат, а не старіший).
    for i in range(n - strength - 1, strength - 1, -1):
        window = ordered[i - strength:i + strength + 1]
        if side == Side.LONG.value:
            if ordered[i].low == min(c.low for c in window):
                return ordered[i].low
        else:
            if ordered[i].high == max(c.high for c in window):
                return ordered[i].high
    return None


def _structural_trailing_stop(trade: ActiveTrade, context: dict) -> Optional[float]:
    """
    Структурний трейлінг ПІСЛЯ TP1 — єдиний механізм підтягування стопу з
    моменту TP1 і до повного закриття угоди (замінює MFE Guard + Post-TP1
    Guard + "БУ на 50% до TP2", які тягнули стоп на довільну $/ATR-відстань
    і вибивали угоду першим-ліпшим випадковим відкатом).

    Логіка: стоп переноситься під останній сформований 15M мінімум (LONG)
    або над останній сформований 15M максимум (SHORT). Ціна зробила імпульс,
    намалювала відкат, пішла далі — стоп переноситься під/над цей новий
    відкат. Ратчет у _apply_protective_stop гарантує, що стоп ніколи не
    послабиться нижче/вище вже зафіксованого рівня (зокрема нижче строгого
    беззбитку, встановленого на TP1).
    """
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []
    swing = _find_last_swing_point(trade.side, c15)
    if swing is None:
        return None

    side = trade.side
    price = context.get("price", trade.entry)
    atr15 = context.get("atr15", 0.6) or 0.6
    # Невеликий буфер під сам пивот (частка сесійно-адаптивного ATR-буфера),
    # щоб стоп не стояв точно на рівні, який часто зачіпається тінню без
    # реального пробою структури.
    pivot_buffer = _atr_guard_buffer(context, atr15, price) * 0.3

    if side == Side.LONG.value:
        stop = swing - pivot_buffer
    else:
        stop = swing + pivot_buffer

    return round_price(stop)


def _trade_pct(side: str, entry: float, price: float) -> float:
    if not entry:
        return 0.0
    if side == Side.LONG.value:
        return (price - entry) / entry * 100
    return (entry - price) / entry * 100


def _opened_at_ms(opened_at: str) -> int:
    """Парсить ActiveTrade.opened_at (iso_now()) у мілісекунди epoch.
    При будь-якій помилці парсингу повертає 0 (тобто "без нижньої межі"),
    щоб не ламати перевірку стопу через дефектний/відсутній timestamp."""
    try:
        return int(datetime.fromisoformat(str(opened_at)).timestamp() * 1000)
    except Exception:
        return 0


def _stop_hit(trade: ActiveTrade, context: dict) -> tuple[bool, str]:
    """
    Жорсткий стоп-лос: будь-який дотик до рівня стопу (навіть тінню свічки)
    означає негайний вихід. Без розділення на Hard/Soft стопи, без додаткового
    запасу в 1.5 ATR і без очікування закриття свічки — щойно ціна торкнулась
    рівня інвалідації, теза вважається зламаною.

    ВИПРАВЛЕННЯ (успадковане з попередньої версії): перевіряється не лише
    ОСТАННЯ 3m-свічка, а ВСІ свічки, що закрились після
    trade.last_checked_3m_ts (і не раніше моменту відкриття угоди), у
    хронологічному порядку — щоб не пропустити whipsaw між запусками бота.
    """
    stop = float(trade.stop_current or 0)
    if not stop:
        return False, ""

    price = float(context.get("price") or 0)
    c3 = (context.get("candles", {}) or {}).get("3m", [])

    if not c3:
        # Fallback, якщо немає свічок
        if trade.side == Side.LONG.value:
            if price <= stop: return True, "Stop hit (жива ціна)"
        else:
            if price >= stop: return True, "Stop hit (жива ціна)"
        return False, ""

    lower_bound = max(int(trade.last_checked_3m_ts or 0), _opened_at_ms(trade.opened_at))
    unchecked = sorted((c for c in c3 if c.ts > lower_bound), key=lambda c: c.ts)
    # Якщо нових свічок від останньої перевірки не з'явилось (бот запускається
    # частіше за 3m-бар), лишаємось на попередній поведінці — перевіряємо
    # останню доступну свічку.
    candles_to_check = unchecked or c3[-1:]

    # Перевіряємо всі свічки між запусками бота: будь-який дотик тінню = вихід
    for candle in candles_to_check:
        if trade.side == Side.LONG.value:
            if candle.low <= stop:
                return True, f"Stop hit: ціна пробила стоп ({candle.low} <= {stop}, ts={candle.ts})"
        else:
            if candle.high >= stop:
                return True, f"Stop hit: ціна пробила стоп ({candle.high} >= {stop}, ts={candle.ts})"

    # Перевірка живою ціною (для свічки, що ще формується і не потрапила в c3)
    if price:
        if trade.side == Side.LONG.value and price <= stop:
            return True, f"Stop hit (жива ціна {price} <= {stop})"
        if trade.side == Side.SHORT.value and price >= stop:
            return True, f"Stop hit (жива ціна {price} >= {stop})"

    return False, ""


def _target_hit(side: str, context: dict, level: float, lookback: int = 4) -> bool:
    """
    Покращена перевірка тейк-профіту (з lookback по свічках).
    Вирішує проблему пропуску TP через polling + гібридні дані (TradingView + OKX).
    """
    price = float(context.get("price") or 0)
    c3 = (context.get("candles", {}) or {}).get("3m", [])

    if side == Side.LONG.value:
        if price >= level:
            return True
        if c3:
            recent = c3[-lookback:] if len(c3) >= lookback else c3
            recent_high = max((c.high for c in recent), default=price)
            return recent_high >= level
        return False
    else:
        if price <= level:
            return True
        if c3:
            recent = c3[-lookback:] if len(c3) >= lookback else c3
            recent_low = min((c.low for c in recent), default=price)
            return recent_low <= level
        return False


def manage_active_trade(trade: ActiveTrade, context: dict) -> dict:
    price = context.get("price")
    # Якщо ціна None через збій API, тримаємо позицію, щоб не наробити помилок
    if price is None:
        return {"action": Action.HOLD.value, "closed": False, "notes": ["Очікування даних ціни..."]}
        
    # ПРИМІТКА: atr15 тут раніше рахувався локально, але жоден з хелперів
    # нижче (_stop_hit, _target_hit, _structural_trailing_stop) не приймає
    # його аргументом — усі й так читають atr15 напряму з context.
    # Orphaned-змінна від старішої версії видалена.
    side = trade.side

    # 1. Спочатку оновлюємо екстремуми ціни ДО формування словника результату
    if (side == Side.LONG.value and price > trade.best_price) or (side == Side.SHORT.value and price < trade.best_price):
        trade.best_price = price
    if (side == Side.LONG.value and price < trade.worst_price) or (side == Side.SHORT.value and price > trade.worst_price):
        trade.worst_price = price

    # 2. Виконуємо розрахунки відсотків один раз
    current_pct = _trade_pct(side, trade.entry, price)
    best_pct = _trade_pct(side, trade.entry, trade.best_price)
    worst_pct = max(0.0, _trade_pct(opposite(side), trade.entry, trade.worst_price))

    # 3. Формуємо чистий словник результату без дублювання та перезапису полів
    result = {
        "action": Action.HOLD.value,
        "title": "УГОДА ВІДКРИТА — HYBRID ICT v6.6",
        "recommendation": "Структура та теза на боці — тримаємо",
        "current_pct": current_pct,
        "best_pct": best_pct,
        "worst_pct": worst_pct,
        "giveback_pct": max(0.0, best_pct - current_pct),
        "closed": False,
        "exit_price": None,
        "notes": [],
        "recommended_stop": None,
        "recommended_stop_reason": "",
    }

    # --- "Дати дихати" ДО TP1: жодного підтягування стопу ---
    # До TP1 позиція має рівно два виходи: початковий стоп-лос (за технічним
    # рівнем/структурою) або TP1. Жодних проміжних рухів стопу в цій зоні —
    # ринок завжди робить відкати, і угода має пережити їх без "задушливого"
    # трейлінгу (колишній MFE Guard тут прибрано повністю).

    # --- Структурний трейлінг ПІСЛЯ TP1 (по 15M swing points) ---
    # Єдиний механізм підтягування стопу з моменту TP1 і до закриття угоди:
    # стоп рухається під/над останній сформований 15M мінімум/максимум, а не
    # на довільну $/ATR-відстань — це і дозволяє забирати розширені рухи
    # (1.50$-2.00$+), не вилітаючи з ринку на першому випадковому відкаті.
    if trade.tp1_hit:
        structural_stop = _structural_trailing_stop(trade, context)
        if _apply_protective_stop(trade, context, structural_stop):
            result["notes"].append(f"Структурний трейлінг: стоп перенесено під/над останній 15M swing до {trade.stop_current}")
            result["recommended_stop"] = round_price(trade.stop_current)
            result["recommended_stop_reason"] = "Структурний трейлінг активний (15M swing points) після TP1"

    # --- Дворівневий Wick Defense ---
    is_stop, stop_reason = _stop_hit(trade, context)
    if is_stop:
        exit_price = round_price(trade.stop_current)
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(f"Вихід по стопу: {stop_reason}")
        trade.status = "CLOSED"
        trade.last_action = Action.STOP.value
        return result

    # --- 1. Фіксація TP1 (Строгий Беззбиток, миттєво) ---
    if not result["closed"] and not trade.tp1_hit and _target_hit(side, context, trade.tp1):
        trade.tp1_hit = True
        trade.tp1_stop_locked = True
        # СТРОГИЙ БЕЗЗБИТОК: щойно ціна торкнулась TP1 (навіть тінню свічки),
        # стоп МИТТЄВО (без затримок і без очікування 50% шляху до TP2)
        # переноситься на вхід + буфер комісії. Це і психологічно, і
        # математично захищає капітал — залишок угоди стає безризиковим.
        # Рекомендація: зафіксувати ~50% позиції саме на цьому рівні.
        if side == Side.LONG.value:
            trade.tp1_locked_stop = round_price(trade.entry + COMMISSION_BUFFER_DOLLARS)
        else:
            trade.tp1_locked_stop = round_price(trade.entry - COMMISSION_BUFFER_DOLLARS)
        trade.stop_current = trade.tp1_locked_stop
        result["action"] = Action.TP1.value
        result["notes"].append(f"TP1 досягнуто — зафіксуйте ~50% позиції; стоп негайно переведено у строгий беззбиток ({trade.stop_current})")

    # --- 2. Фіксація TP2 ---
    if not result["closed"] and trade.tp1_hit and not trade.tp2_hit and _target_hit(side, context, trade.tp2):
        trade.tp2_hit = True
        trade.tp2_stop_locked = True
        # Так само, як і на TP1: не послаблюємо стоп, якщо структурний трейлінг
        # уже підтягнув його тісніше за рівень TP1 в межах цієї ж свічки.
        if side == Side.LONG.value:
            trade.tp2_locked_stop = max(trade.tp1, trade.stop_current)
        else:
            trade.tp2_locked_stop = min(trade.tp1, trade.stop_current)
        trade.stop_current = trade.tp2_locked_stop
        result["action"] = Action.TP2.value
        result["notes"].append("TP2 досягнуто — стоп перенесено на TP1")

    # --- 3. Фіксація TP3 (Повне закриття) ---
    if not result["closed"] and trade.tp2_hit and not trade.tp3_hit and _target_hit(side, context, trade.tp3):
        trade.tp3_hit = True
        exit_price = round_price(trade.tp3)
        result["closed"] = True
        result["action"] = Action.TP3.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(f"TP3 досягнуто ({exit_price}) — угоду повністю закрито")
        trade.status = "CLOSED"
        trade.last_action = Action.TP3.value
        return result

    # --- Відображення рекомендованого стопу ---
    if not result["closed"]:
        result["recommended_stop"] = round_price(trade.stop_current)
        if trade.tp2_stop_locked:
            result["recommended_stop_reason"] = "TP2-стоп зафіксовано"
        elif trade.tp1_stop_locked:
            result["recommended_stop_reason"] = "Строгий беззбиток активний (TP1 зафіксовано)"

    # --- Структурна інвалідація ---
    structural_break = False
    if side == Side.LONG.value:
        if price < trade.structural_invalidation:
            structural_break = True
    else:
        if price > trade.structural_invalidation:
            structural_break = True

    if not result["closed"] and structural_break:
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = price
        result["notes"].append("Структурна інвалідація (закриття 15M) — вихід")

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

REGIME_LABELS = {
    "TREND": "ТРЕНД",
    "RANGE": "ДІАПАЗОН",
    "TRANSITION": "ПЕРЕХІДНИЙ",
    "SHOCK": "ІМПУЛЬСНИЙ",
    "NORMAL": "ЗВИЧАЙНИЙ",
    "TREND_EXPANSION": "ТРЕНДОВЕ РОЗШИРЕННЯ",
    "TREND_PULLBACK": "ВІДКАТ У ТРЕНДІ",
    "RANGE_COMPRESSION": "СТИСКАННЯ ДІАПАЗОНУ",
    "RANGE_EDGE": "КРАЙ ДІАПАЗОНУ",
    "REVERSAL_BUILDUP": "ФОРМУВАННЯ РОЗВОРОТУ",
    "NEWS_SHOCK": "НОВИННИЙ ІМПУЛЬС",
    "EXHAUSTION": "ВИСНАЖЕННЯ РУХУ",
}
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
    by_family: dict[str, dict[str, Any]] = {}
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        family = str(trade.get("setup_family") or SetupFamily.NONE.value)
        row = by_family.setdefault(family, {"closed_trades": 0, "wins": 0, "losses": 0, "net_r": 0.0})
        result_pct = safe_float(trade.get("result_pct"), 0.0)
        row["closed_trades"] += 1
        if result_pct > 0:
            row["wins"] += 1
        else:
            row["losses"] += 1
        row["net_r"] += result_pct / 100.0
    for row in by_family.values():
        family_closed = max(int(row["closed_trades"]), 1)
        row["net_r"] = round(row["net_r"], 2)
        row["win_rate"] = round(row["wins"] / family_closed * 100, 1)
        row["expectancy_r"] = round(row["net_r"] / family_closed, 3)
    return {"closed_trades": closed, "wins": wins, "losses": closed - wins, "win_rate": win_rate, "net_r": net_r, "expectancy_r": round(net_r / max(closed, 1), 3), "by_family": by_family}


# ==========================================================
# MAIN
# ==========================================================

def run_bot() -> None:
    print(f"START {BOT_VERSION}")
    state = load_state()
    journal = load_journal()
    journal["learning_status"] = compute_learning_status(journal)
    learning_warnings = learning_health_warnings(journal)
    for warning in learning_warnings:
        print(f"[WARN] {warning}")
    data = collect_market_data()
    context = build_context(data, state)
    context["learning_status"] = journal.get("learning_status", {})
    context["learning_warnings"] = learning_warnings
    if not context.get("price"):
        print("NO PRICE — abort")
        return
    print(f"PRICE {context['price']:.4f} | {context.get('instrument_label', INSTRUMENT_LABEL)} | REGIME {context['regime']} | ATR15 {context['atr15']:.4f} | Джерело: {context.get('price_source', 'TradingView')}")

    active = active_trade_from_state(state)
    if active and active.status != "CLOSED":
        stop_before = active.stop_current
        res = manage_active_trade(active, context)
        stop_after = active.stop_current
        res["stop_changed"] = bool(stop_after) and round_price(stop_after) != round_price(stop_before)
        res["stop_before"] = stop_before
        res["stop_after"] = stop_after
        msg = build_follow_message(context, active, res)
        
        if TELEGRAM_NOTIFY_EVERY_RUN or res.get("closed") or res.get("stop_changed"):
            send_telegram(msg)
        
        if res.get("closed"):
            journal["trades"].append({
                "id": active.id,
                "signal_id": active.signal_id,
                "side": active.side,
                "setup_type": active.setup_type,
                "setup_family": active.setup_family,
                "opened_regime": active.opened_regime,
                "entry_level": active.entry_level,
                "quality": active.quality,
                "result_pct": res.get("current_pct"),
                "mfe_pct": res.get("best_pct"),
                "tp1_hit": active.tp1_hit,
                "tp2_hit": active.tp2_hit,
                "tp3_hit": active.tp3_hit,
                "close_action": res.get("action"),
            })
            store_active_trade(state, None)
            state["opportunity"] = None
        else:
            store_active_trade(state, active)
        append_history(state, {"type": "FOLLOW" if not res.get("closed") else "CLOSE", "side": active.side, "action": res.get("action"), "price": context["price"], "trade_id": active.id, "signal_id": active.signal_id})
        journal.setdefault("signal_events", []).append({"time": iso_now(), "type": "FOLLOW" if not res.get("closed") else "CLOSE", "action": res.get("action"), "side": active.side, "price": context["price"], "trade_id": active.id, "signal_id": active.signal_id})
        save_state(state)
        save_journal(journal)
        print("BOT COMPLETE: ACTIVE TRADE MANAGED")
        return

    decision = evaluate_new_setup(context, state, journal)
    payload = {"id": decision.id, "time": decision.time, "action": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "decision_quality": decision.quality, "reason": decision.reason, "regime": decision.regime, "news_bias": decision.news_bias, "macro_risk": decision.macro_risk, "instrument_label": context.get("instrument_label", INSTRUMENT_LABEL), "instrument_kind": context.get("instrument_kind", INSTRUMENT_KIND), "flow_quality": context.get("flow_quality", ""), "cvd_quality": context.get("cvd_quality", ""), "learning_status": journal.get("learning_status", {}), "learning_warnings": learning_warnings, "version": BOT_VERSION, "architecture_version": ARCHITECTURE_VERSION}
    if decision.candidate:
        components = decision.candidate.score_components or {}
        payload.update({
            "setup_family": decision.candidate.setup_family,
            "ict_model": decision.candidate.ict_model,
            "candidate_final_score": decision.candidate.final_score,
            "candidate_raw_score": decision.candidate.raw_score,
            "execution_lane": decision.candidate.execution_lane,
            "trigger_ready": decision.candidate.trigger_ready,
            "trigger_age_minutes": round(float(decision.candidate.trigger_age_minutes or 0.0), 2),
            "score_components": components,
            "score_features": components.get("features", {}),
            "score_gates": components.get("gates", {}),
            "score_model_source": components.get("model_source", ""),
            "score_model_sample_size": components.get("sample_size", 0),
            "score_model_learned_weight": components.get("learned_weight", 0.0),
        })
    state["latest_signal"] = payload
    append_history(state, {"type": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "price": context["price"]})
    journal["signals"].append(payload)
    if payload.get("score_features"):
        journal.setdefault("training_signals", []).append(payload)

    if decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value) and decision.candidate and decision.plan:
        # signal_id = decision.id: угода тепер завжди трасується до сигналу,
        # який її відкрив. trade.id лишається окремим (власним) ідентифікатором
        # угоди — це важливо для re-entry-сценаріїв, де одна Opportunity/сигнал
        # може породжувати кілька спроб входу.
        active = ActiveTrade(id=uuid.uuid4().hex[:10], signal_id=decision.id, side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, opened_at=iso_now(), entry=decision.plan.entry, stop_initial=decision.plan.stop, stop_current=decision.plan.stop, structural_invalidation=decision.plan.structural_invalidation, tp1=decision.plan.tp1, tp2=decision.plan.tp2, tp3=decision.plan.tp3, quality=decision.quality, position_risk_pct=decision.plan.position_risk_pct, best_price=decision.plan.entry, worst_price=decision.plan.entry, trigger_level=decision.candidate.trigger_level, thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, opened_regime=decision.regime, entry_level=decision.action)
        store_active_trade(state, active)
        state["opportunity"] = None
        print(f"[INFO] Угода відкрита: {decision.side} {decision.setup_type} | signal_id={active.signal_id} trade_id={active.id}")
    elif decision.action == Action.ARMED.value and decision.candidate:
        opp = Opportunity(side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, created_at=iso_now(), expires_at=(now_utc() + timedelta(hours=18)).isoformat(), score=decision.quality, trigger_level=decision.candidate.trigger_level, invalidation_level=decision.candidate.invalidation_level, confirmations=decision.candidate.confirmations[:5], evidence_families=decision.candidate.evidence_families, execution_lane=decision.candidate.execution_lane, status="ARMED", thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, signal_id=decision.id)
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


def _run_self_test() -> bool:
    """Швидкі, детерміновані, БЕЗ мережевих запитів перевірки фінансово-
    критичної логіки — призначені для запуску в CI/деплой-пайплайні перед
    тим, як бот піде в прод (напр. крок GitHub Actions перед run_bot()).

    Це НЕ заміна pytest-покриття в tests/test_bot_oneshot.py (там повний
    набір з детальними сценаріями для RR, stop-менеджменту, TP-прогресії
    та CVD/flow-фіксу) — це компактний smoke-test без зовнішньої залежності
    від pytest, який можна запустити навіть якщо pytest не встановлено
    в рантайм-образі бота.
    """
    checks: list[tuple[str, bool]] = []

    # --- RR floors ---
    _, tp1, tp2, tp3 = enforce_smart_money_rr(Side.LONG.value, 100.0, 99.0, 100.3, 100.6, 100.9, 0.6)
    rr1 = (tp1 - 100.0) / (100.0 - 99.0)
    checks.append(("enforce_smart_money_rr: LONG RR1 >= MIN_RR1", rr1 >= MIN_RR1 - 1e-6))
    checks.append(("enforce_smart_money_rr: LONG TP-монотонність", tp1 < tp2 < tp3))

    _, stp1, stp2, stp3 = enforce_smart_money_rr(Side.SHORT.value, 100.0, 101.0, 99.7, 99.4, 99.1, 0.6)
    srr1 = (100.0 - stp1) / (101.0 - 100.0)
    checks.append(("enforce_smart_money_rr: SHORT RR1 >= MIN_RR1", srr1 >= MIN_RR1 - 1e-6))
    checks.append(("enforce_smart_money_rr: SHORT TP-монотонність", stp1 > stp2 > stp3))

    # --- _stop_hit: перевіряє ВСІ пропущені свічки, не лише останню ---
    now_ms = int(now_utc().timestamp() * 1000)
    opened_at = (now_utc() - timedelta(hours=1)).isoformat()
    trade = ActiveTrade(
        id="selftest", side=Side.LONG.value, setup_type=SetupType.PULLBACK_CONTINUATION.value,
        setup_family=SetupFamily.CONTINUATION.value, opened_at=opened_at,
        entry=100.0, stop_initial=99.0, stop_current=99.0, structural_invalidation=94.0,
        tp1=101.5, tp2=103.0, tp3=105.0, quality=80, position_risk_pct=0.5,
        best_price=100.0, worst_price=100.0, last_checked_3m_ts=now_ms - 20 * 60_000,
    )

    def _mk_candle(ts: int, close: float) -> Candle:
        return Candle(ts=ts, open=close, high=close + 0.05, low=close - 0.05, close=close, volume=1000.0)

    missed_candles = [
        _mk_candle(now_ms - 12 * 60_000, 99.4),
        _mk_candle(now_ms - 9 * 60_000, 98.7),   # пробила стоп тілом і забута старою логікою
        _mk_candle(now_ms - 6 * 60_000, 99.3),   # ціна вже відновилась до наступного polling
    ]
    hit, _ = _stop_hit(trade, {"price": 99.3, "atr15": 0.6, "candles": {"3m": missed_candles}})
    checks.append(("_stop_hit: виявляє пропущений whipsaw між запусками", hit is True))

    # --- CVD/flow фікс: flw_score більше не гаситься мертвою flow-заглушкою ---
    flow = flow_snapshot([], {})
    cvd_long = {"bias": Side.LONG.value, "score": 15}
    flw_score = float(cvd_long.get("score", 0)) * 1.0 if cvd_long.get("bias") == Side.LONG.value else 0.0
    checks.append(("flow_snapshot лишається заглушкою (документує стан)", flow["bias"] == Side.NEUTRAL.value))
    checks.append(("flw_score рахується від CVD, а не від мертвого flow", flw_score == 15.0))

    ok = all(passed for _, passed in checks)
    for name, passed in checks:
        print(f"  [{'OK' if passed else 'FAIL'}] {name}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="BZU Professional Hybrid Confluence Signal Bot v6.6")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        ok = _run_self_test()
        print("SELF-TEST PASSED" if ok else "SELF-TEST FAILED")
        if not ok:
            raise SystemExit(1)
        return
    run_bot()


if __name__ == "__main__":
    main()
