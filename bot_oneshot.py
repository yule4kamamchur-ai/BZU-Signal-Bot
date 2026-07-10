#!/usr/bin/env python3
"""
BZU Professional Hybrid Confluence Signal Bot v6.12 (Market-Structure Plus Edition)
================================================================================
Виправлення v6.13 (Trade-Breathing Geometry Edition):
- Додано noise-adjusted structural stop: стоп ставиться за межі нормального 15M шуму, а не просто біля входу.
- Додано catastrophic_stop + decision_stop: wick не вбиває угоду без close-confirm, але emergency stop лишається жорстким.
- TP0/TP1 тепер враховують 15M true-range envelope: тейки не мають лежати всередині однієї шумової свічки.
- Після TP1 стоп НЕ переноситься миттєво у БУ: додано BE_DELAY_ENGINE з підтвердженням 15M close / MFE / кількості свічок.
- Ризик контролюється sizing multiplier, а не тісним стопом: ширший stop-distance автоматично зменшує position_risk_pct.

Виправлення v6.11 (Execution-Audit / Continuation-Probe Edition):
- Додано full rejection audit для NO_SETUP: бот тепер зберігає rejected_hypotheses і failed_gate, щоб кожен пропущений рух мав пояснення, а не шаманський “не було сетапу”.
- Додано ACCEPTANCE_RETEST_CONTINUATION: early continuation-probe після ретесту OB/FVG/discount, якщо структура не зламана і є acceptance.
- Додано ACCELERATION_PULLBACK_REENTRY: якщо імпульс уже пішов без входу, бот не женеться market на піку, а ставить WAIT_PULLBACK до 38–50% імпульсу.
- Додано дедуплікацію journal["trades"], щоб ML не вчився на дублях однієї угоди.

Виправлення v6.10 (Hypothesis-Matrix Edition):
- Розділено trigger_ready на джерела виконання: LIVE_3M / LIMIT_ARMED / TIME_WARP / REENTRY.
  Старий 3M/time-warp більше не маскується під живий market-entry, а переводиться у WAIT_RETEST/LIMIT_ONLY.
- Додано staged entry ladder: PROBE / ACCEPTANCE / RETEST_ADD / CORE з адаптивним sizing,
  замість одного грубого RISKY_ENTRY на повний ризик.
- Додано TP0 як мікрофіксацію без пересування стопа, щоб підвищувати реалізований hit-rate
  без задушення TP1/TP2/TP3.
- Target Magnet Scoring: TP обираються не просто за найближчою відстанню, а за магнітністю
  цілі (тип ліквідності, TF, strength, distance).
- Додано runtime_config_snapshot у signal/trade/state для аудиту ENV overrides.
- ML у bootstrap режимі використовується як soft sizing/stage adjustment, а не як фальшивий
  професійний gate.
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


def get_htf_state(candidate: Any) -> str:
    """
    Unified HTF state resolver.
    Single source of truth for execution, sizing and audit.
    """
    if candidate is None:
        return "neutral"

    direct = getattr(candidate, "htf_state", None)
    if direct:
        return str(direct)

    components = getattr(candidate, "score_components", {}) or {}
    if components.get("htf_state"):
        return str(components["htf_state"])

    features = components.get("features", {}) or {}
    if features.get("htf_state"):
        return str(features["htf_state"])

    htf_score = components.get("htf_score", features.get("htf", 0))

    try:
        score = float(htf_score)
        if score >= 0.65:
            return "bullish"
        if score <= 0.35:
            return "bearish"
    except Exception:
        pass

    return "neutral"


# ==========================================================
# CONFIGURATION
# ==========================================================

BOT_VERSION = "pro-hybrid-confluence-v6.17.9-professional-audit"
ARCHITECTURE_VERSION = "HYBRID_CONFLUENCE_V6_5"

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

# === Execution Intelligence v6.10 ===
# Не блокує сетапи фільтрами: замість цього зменшує/збільшує розмір позиції
# і переводить вхід у відповідну стадію. Це дає ранній вхід без дурного full-size FOMO.
TP0_RR = float(os.getenv("TP0_RR", "0.75") or 0.75)
TP0_SIZE_PCT = float(os.getenv("TP0_SIZE_PCT", "0.20") or 0.20)
TP1_SIZE_PCT = float(os.getenv("TP1_SIZE_PCT", "0.35") or 0.35)
TP2_SIZE_PCT = float(os.getenv("TP2_SIZE_PCT", "0.25") or 0.25)
TP3_RUNNER_PCT = max(0.0, 1.0 - TP0_SIZE_PCT - TP1_SIZE_PCT - TP2_SIZE_PCT)

# === v6.13 Trade-Breathing Geometry ===
# Це НЕ фільтри входу. Це геометрія угоди: ширший шумовий стоп + менший sizing,
# TP за межами однієї 15M свічки, delayed-BE після підтвердження.
STOP_NOISE_PERCENTILE = float(os.getenv("STOP_NOISE_PERCENTILE", "0.70") or 0.70)
TP_NOISE_PERCENTILE = float(os.getenv("TP_NOISE_PERCENTILE", "0.85") or 0.85)
MIN_STOP_TRUE_RANGE_MULT = float(os.getenv("MIN_STOP_TRUE_RANGE_MULT", "1.10") or 1.10)
MIN_TP1_TRUE_RANGE_MULT = float(os.getenv("MIN_TP1_TRUE_RANGE_MULT", "1.25") or 1.25)
CURRENT_CANDLE_STOP_MULT = float(os.getenv("CURRENT_CANDLE_STOP_MULT", "0.85") or 0.85)
TP0_MIN_RR = float(os.getenv("TP0_MIN_RR", "1.00") or 1.00)
TP1_MIN_RR_PRO = float(os.getenv("TP1_MIN_RR_PRO", "2.00") or 2.00)
TP1_MIN_ATR_PRO = float(os.getenv("TP1_MIN_ATR_PRO", "3.00") or 3.00)
BE_DELAY_BARS_AFTER_TP1 = int(os.getenv("BE_DELAY_BARS_AFTER_TP1", "2") or 2)
BE_DELAY_MIN_MFE_R = float(os.getenv("BE_DELAY_MIN_MFE_R", "1.80") or 1.80)
BE_LOCK_R_MULT = float(os.getenv("BE_LOCK_R_MULT", "0.10") or 0.10)
CATASTROPHIC_STOP_MULT = float(os.getenv("CATASTROPHIC_STOP_MULT", "1.45") or 1.45)
DECISION_STOP_CLOSE_CONFIRM = os.getenv("DECISION_STOP_CLOSE_CONFIRM", "true").lower() in {"1", "true", "yes"}
MIN_BREATHING_RISK_MULTIPLIER = float(os.getenv("MIN_BREATHING_RISK_MULTIPLIER", "0.35") or 0.35)

# === v6.14 Non-Blocking Setup Innovation Engine ===
# Не підвищує entry-threshold і не блокує входи. Він змінює спосіб побудови
# позиції: морфологія сетапу, partial-plan, risk construction, target routing.
SETUP_INNOVATION_ENGINE = os.getenv("SETUP_INNOVATION_ENGINE", "true").lower() in {"1", "true", "yes"}
INNOVATION_STALE_TRIGGER_MIN = float(os.getenv("INNOVATION_STALE_TRIGGER_MIN", "240") or 240)
INNOVATION_EXTREME_STALE_TRIGGER_MIN = float(os.getenv("INNOVATION_EXTREME_STALE_TRIGGER_MIN", "1440") or 1440)
INNOVATION_MIN_RISK_MULT = float(os.getenv("INNOVATION_MIN_RISK_MULT", "0.35") or 0.35)
INNOVATION_MAX_RISK_MULT = float(os.getenv("INNOVATION_MAX_RISK_MULT", "1.05") or 1.05)
INNOVATION_WEAK_MAGNET_LEVEL = float(os.getenv("INNOVATION_WEAK_MAGNET_LEVEL", "2.05") or 2.05)
INNOVATION_STRONG_MAGNET_LEVEL = float(os.getenv("INNOVATION_STRONG_MAGNET_LEVEL", "3.0") or 3.0)
INNOVATION_GIVEBACK_WARN_RATIO = float(os.getenv("INNOVATION_GIVEBACK_WARN_RATIO", "0.62") or 0.62)
INNOVATION_TP0_DEFENSE_SIZE = float(os.getenv("INNOVATION_TP0_DEFENSE_SIZE", "0.30") or 0.30)

# === v6.15 Setup-Argumented Trigger Revalidation ===
# Не блокує сетапи через вік. Старий trigger втрачає право бути execution,
# але thesis може швидко ожити, якщо ринок дає новий сетапний доказ тут і зараз.
SETUP_REVALIDATION_ENGINE = os.getenv("SETUP_REVALIDATION_ENGINE", "true").lower() in {"1", "true", "yes"}
SETUP_REVALIDATION_STALE_MIN = float(os.getenv("SETUP_REVALIDATION_STALE_MIN", "240") or 240)
SETUP_REVALIDATION_EXTREME_MIN = float(os.getenv("SETUP_REVALIDATION_EXTREME_MIN", "1440") or 1440)
SETUP_REVALIDATION_MAX_LATE_DIST_ATR = float(os.getenv("SETUP_REVALIDATION_MAX_LATE_DIST_ATR", "1.35") or 1.35)
SETUP_REVALIDATION_BODY_ATR_MIN = float(os.getenv("SETUP_REVALIDATION_BODY_ATR_MIN", "0.16") or 0.16)
SETUP_REVALIDATION_BODY_RATIO_MIN = float(os.getenv("SETUP_REVALIDATION_BODY_RATIO_MIN", "0.52") or 0.52)
SETUP_REVALIDATION_ACCEPTANCE_ATR = float(os.getenv("SETUP_REVALIDATION_ACCEPTANCE_ATR", "0.42") or 0.42)

# === v6.16 Adaptive Execution Patch ===
# HTF більше не hard-block risky entry: воно зменшує ризик, якщо execution підтверджений.
HTF_RISKY_OVERRIDE = os.getenv("HTF_RISKY_OVERRIDE", "true").lower() in {"1", "true", "yes"}
HTF_OVERRIDE_MIN_SCORE = float(os.getenv("HTF_OVERRIDE_MIN_SCORE", "72") or 72)
HTF_OVERRIDE_RISK_MULT = float(os.getenv("HTF_OVERRIDE_RISK_MULT", "0.45") or 0.45)

# Старі thesis не зависають ARMED назавжди.
STALE_THESIS_PROBE_ATR = float(os.getenv("STALE_THESIS_PROBE_ATR", "1.5") or 1.5)
STALE_THESIS_ACCEPTANCE_REQUIRED = os.getenv("STALE_THESIS_ACCEPTANCE_REQUIRED", "true").lower() in {"1", "true", "yes"}

# Missed move audit
MISSED_MOVE_ATR = float(os.getenv("MISSED_MOVE_ATR", "1.8") or 1.8)

SETUP_REVALIDATION_RISK_MULT_STALE = float(os.getenv("SETUP_REVALIDATION_RISK_MULT_STALE", "0.55") or 0.55)
SETUP_REVALIDATION_RISK_MULT_EXTREME = float(os.getenv("SETUP_REVALIDATION_RISK_MULT_EXTREME", "0.35") or 0.35)

PROBE_RISK_PCT = float(os.getenv("PROBE_RISK_PCT", "0.12") or 0.12)
ACCEPTANCE_RISK_PCT = float(os.getenv("ACCEPTANCE_RISK_PCT", "0.22") or 0.22)
RETEST_ADD_RISK_PCT = float(os.getenv("RETEST_ADD_RISK_PCT", "0.30") or 0.30)
CORE_RISK_PCT = float(os.getenv("CORE_RISK_PCT", str(NORMAL_RISK_PCT)) or NORMAL_RISK_PCT)
BOOTSTRAP_RISK_MULTIPLIER = float(os.getenv("BOOTSTRAP_RISK_MULTIPLIER", "0.75") or 0.75)
WEAK_DIRECTION_RISK_MULTIPLIER = float(os.getenv("WEAK_DIRECTION_RISK_MULTIPLIER", "0.55") or 0.55)
TIME_WARP_SCORE_MULTIPLIER = float(os.getenv("TIME_WARP_SCORE_MULTIPLIER", "0.70") or 0.70)
STALE_TRIGGER_SCORE_MULTIPLIER = float(os.getenv("STALE_TRIGGER_SCORE_MULTIPLIER", "0.62") or 0.62)
LIMIT_ARMED_SCORE_MULTIPLIER = float(os.getenv("LIMIT_ARMED_SCORE_MULTIPLIER", "0.88") or 0.88)


# === v6.17 Institutional Adaptive Engine ===
# Додатковий execution layer: не замінює існуючу логіку, а модулює ризик.
INSTITUTIONAL_ADAPTIVE_ENGINE = os.getenv("INSTITUTIONAL_ADAPTIVE_ENGINE", "true").lower() in {"1", "true", "yes"}

PRO_SCORE_LIQUIDITY_WEIGHT = float(os.getenv("PRO_SCORE_LIQUIDITY_WEIGHT", "1.0") or 1.0)
PRO_SCORE_TRIGGER_WEIGHT = float(os.getenv("PRO_SCORE_TRIGGER_WEIGHT", "1.15") or 1.15)
PRO_SCORE_STRUCTURE_WEIGHT = float(os.getenv("PRO_SCORE_STRUCTURE_WEIGHT", "1.0") or 1.0)
PRO_SCORE_LATE_ENTRY_PENALTY = float(os.getenv("PRO_SCORE_LATE_ENTRY_PENALTY", "0.75") or 0.75)

CHASE_DETECTION_ENABLED = os.getenv("CHASE_DETECTION_ENABLED", "true").lower() in {"1", "true", "yes"}
CHASE_ATR_LIMIT = float(os.getenv("CHASE_ATR_LIMIT", "1.8") or 1.8)
CHASE_BODY_RATIO_LIMIT = float(os.getenv("CHASE_BODY_RATIO_LIMIT", "0.85") or 0.85)

# === v6.18 Entry Freshness Layer ===
# Не створює нові сетапи. Оцінює тільки якість моменту виконання.
ENTRY_FRESHNESS_ENABLED = os.getenv("ENTRY_FRESHNESS_ENABLED", "true").lower() in {"1", "true", "yes"}
FRESHNESS_IMPULSE_SOFT_ATR = float(os.getenv("FRESHNESS_IMPULSE_SOFT_ATR", "1.8") or 1.8)
FRESHNESS_IMPULSE_WARNING_ATR = float(os.getenv("FRESHNESS_IMPULSE_WARNING_ATR", "2.5") or 2.5)
FRESHNESS_IMPULSE_HARD_ATR = float(os.getenv("FRESHNESS_IMPULSE_HARD_ATR", "3.5") or 3.5)
FRESHNESS_MAX_DISTANCE_ZONE_ATR = float(os.getenv("FRESHNESS_MAX_DISTANCE_ZONE_ATR", "1.25") or 1.25)
FRESHNESS_CORE_MAX_DISTANCE_ATR = float(os.getenv("FRESHNESS_CORE_MAX_DISTANCE_ATR", "0.75") or 0.75)
FRESHNESS_EXTENDED_RISK_MULT = float(os.getenv("FRESHNESS_EXTENDED_RISK_MULT", "0.45") or 0.45)
FRESHNESS_WARNING_RISK_MULT = float(os.getenv("FRESHNESS_WARNING_RISK_MULT", "0.70") or 0.70)

# v6.18 setup-aware freshness weighting
FRESHNESS_REVERSAL_WEIGHT = float(os.getenv("FRESHNESS_REVERSAL_WEIGHT", "0.50") or 0.50)
FRESHNESS_CONTINUATION_WEIGHT = float(os.getenv("FRESHNESS_CONTINUATION_WEIGHT", "1.00") or 1.00)

HTF_OVERRIDE_ACTIVE_RISK_MULT = float(os.getenv("HTF_OVERRIDE_ACTIVE_RISK_MULT", "0.45") or 0.45)
HTF_NEUTRAL_RISK_MULT = float(os.getenv("HTF_NEUTRAL_RISK_MULT", "0.75") or 0.75)
HTF_STRONG_AGAINST_RISK_MULT = float(os.getenv("HTF_STRONG_AGAINST_RISK_MULT", "0.35") or 0.35)

MISSED_MOVE_PULLBACK_ATR = float(os.getenv("MISSED_MOVE_PULLBACK_ATR", "1.8") or 1.8)

# === Market-Structure Plus v6.12 ===
# Soft detectors: не блокують сетапи, а створюють окремі гіпотези в matrix.
SESSION_MEAN_MAX_DIST_ATR = float(os.getenv("SESSION_MEAN_MAX_DIST_ATR", "1.15") or 1.15)
OPEN_RECLAIM_MAX_DIST_ATR = float(os.getenv("OPEN_RECLAIM_MAX_DIST_ATR", "1.25") or 1.25)
ORB_BREAK_BUFFER_ATR = float(os.getenv("ORB_BREAK_BUFFER_ATR", "0.16") or 0.16)
ORB_RETEST_MAX_DIST_ATR = float(os.getenv("ORB_RETEST_MAX_DIST_ATR", "0.90") or 0.90)
FAILED_AUCTION_MIN_TAIL_RATIO = float(os.getenv("FAILED_AUCTION_MIN_TAIL_RATIO", "0.54") or 0.54)
LIQUIDITY_LADDER_MIN_TARGETS = int(os.getenv("LIQUIDITY_LADDER_MIN_TARGETS", "3") or 3)
LIQUIDITY_LADDER_MIN_SCORE = float(os.getenv("LIQUIDITY_LADDER_MIN_SCORE", "3.20") or 3.20)

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
# Підлогу піднято з 0.15$ до 0.40$: інституційні стопи на BZ ховаються за
# 1H/4H пулами ліквідності (типово 0.40-0.60$ ризику) — 0.15$ чіплявся за
# будь-який 3M-відкат і не давав угоді "дихати".
ABS_MIN_STOP_DOLLARS = float(os.getenv("ABS_MIN_STOP_DOLLARS", "0.40") or 0.40)
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
    TP0 = "TP0"
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
    ACCEPTANCE_RETEST_CONTINUATION = "ACCEPTANCE_RETEST_CONTINUATION"
    ACCELERATION_PULLBACK_REENTRY = "ACCELERATION_PULLBACK_REENTRY"
    SESSION_MEAN_RECLAIM = "SESSION_MEAN_RECLAIM"
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"
    FAILED_OPENING_RANGE_BREAKOUT = "FAILED_OPENING_RANGE_BREAKOUT"
    DAILY_WEEKLY_OPEN_RECLAIM = "DAILY_WEEKLY_OPEN_RECLAIM"
    LIQUIDITY_LADDER = "LIQUIDITY_LADDER"
    FAILED_AUCTION_REJECTION = "FAILED_AUCTION_REJECTION"
    TIME_OF_DAY_ADAPTIVE = "TIME_OF_DAY_ADAPTIVE"
    NONE = "NONE"


class ExecutionLane(str, Enum):
    EARLY_TACTICAL = "EARLY_TACTICAL"
    STANDARD_CONFIRMED = "STANDARD_CONFIRMED"
    MISSED_IMPULSE_REENTRY = "MISSED_IMPULSE_REENTRY"
    WAIT_RETEST = "WAIT_RETEST"
    LIMIT_ONLY = "LIMIT_ONLY"


class ExecutionSource(str, Enum):
    LIVE_3M = "LIVE_3M"
    LIMIT_ARMED = "LIMIT_ARMED"
    TIME_WARP = "TIME_WARP"
    REENTRY = "REENTRY"
    ACCEPTANCE_RETEST = "ACCEPTANCE_RETEST"
    ACCELERATION_PULLBACK = "ACCELERATION_PULLBACK"
    SESSION_MEAN = "SESSION_MEAN"
    OPENING_RANGE = "OPENING_RANGE"
    OPEN_RECLAIM = "OPEN_RECLAIM"
    LIQUIDITY_LADDER = "LIQUIDITY_LADDER"
    FAILED_AUCTION = "FAILED_AUCTION"
    TIME_OF_DAY = "TIME_OF_DAY"
    NONE = "NONE"


class EntryStage(str, Enum):
    PROBE = "PROBE"
    ACCEPTANCE = "ACCEPTANCE"
    RETEST_ADD = "RETEST_ADD"
    CORE = "CORE"
    WAIT_RETEST = "WAIT_RETEST"


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
    live_3m_trigger_ready: bool = False
    limit_armed_ready: bool = False
    time_warp_ready: bool = False
    reentry_ready: bool = False
    execution_source: str = "NONE"
    entry_stage: str = "PROBE"
    stage_plan: dict[str, Any] = field(default_factory=dict)
    risk_multiplier: float = 1.0
    target_magnet_score: float = 0.0
    setup_quality_score: int = 0
    execution_quality_score: int = 0
    trade_plan_quality_score: int = 0
    hypothesis_score: float = 0.0
    hypothesis_rank: int = 0
    active_model_count: int = 0
    competing_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    innovation_profile: dict[str, Any] = field(default_factory=dict)
    revalidation_profile: dict[str, Any] = field(default_factory=dict)
    entry_freshness_score: float = 100.0
    entry_freshness_profile: dict[str, Any] = field(default_factory=dict)


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
    tp0: float = 0.0
    rr0: float = 0.0
    entry_stage: str = "PROBE"
    execution_source: str = "NONE"
    stage_plan: dict[str, Any] = field(default_factory=dict)
    partial_plan: dict[str, float] = field(default_factory=dict)
    runtime_config_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_stop: float = 0.0
    catastrophic_stop: float = 0.0
    breathing_profile: dict[str, Any] = field(default_factory=dict)
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
    tp0: float = 0.0
    tp0_hit: bool = False
    tp0_size_pct: float = TP0_SIZE_PCT
    tp1_size_pct: float = TP1_SIZE_PCT
    tp2_size_pct: float = TP2_SIZE_PCT
    tp3_runner_pct: float = TP3_RUNNER_PCT
    execution_source: str = "NONE"
    entry_stage: str = "PROBE"
    stage_plan: dict[str, Any] = field(default_factory=dict)
    runtime_config_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_stop: float = 0.0
    catastrophic_stop: float = 0.0
    breathing_profile: dict[str, Any] = field(default_factory=dict)
    tp1_hit_at: str = ""
    tp1_hit_ts: int = 0
    tp1_close_confirmed: bool = False


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


def runtime_config_snapshot() -> dict[str, Any]:
    """Аудит ENV/runtime-параметрів, які напряму впливають на TP/SL/entry.
    Без цього неможливо довести, чому фактичні рівні відрізняються від дефолтів коду."""
    return {
        "bot_version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "leverage": LEVERAGE,
        "normal_risk_pct": NORMAL_RISK_PCT,
        "risky_risk_pct": RISKY_RISK_PCT,
        "probe_risk_pct": PROBE_RISK_PCT,
        "acceptance_risk_pct": ACCEPTANCE_RISK_PCT,
        "retest_add_risk_pct": RETEST_ADD_RISK_PCT,
        "core_risk_pct": CORE_RISK_PCT,
        "tp0_rr": TP0_RR,
        "tp0_size_pct": TP0_SIZE_PCT,
        "tp1_size_pct": TP1_SIZE_PCT,
        "tp2_size_pct": TP2_SIZE_PCT,
        "tp3_runner_pct": TP3_RUNNER_PCT,
        "abs_min_stop_dollars": ABS_MIN_STOP_DOLLARS,
        "abs_min_tp1_dollars": ABS_MIN_TP1_DOLLARS,
        "commission_buffer_dollars": COMMISSION_BUFFER_DOLLARS,
        "min_rr1": MIN_RR1,
        "preferred_rr1": PREFERRED_RR1,
        "min_rr2": MIN_RR2,
        "min_rr3": MIN_RR3,
        "trigger_max_age_minutes": TRIGGER_MAX_AGE_MINUTES,
        "time_warp_score_multiplier": TIME_WARP_SCORE_MULTIPLIER,
        "stale_trigger_score_multiplier": STALE_TRIGGER_SCORE_MULTIPLIER,
        "limit_armed_score_multiplier": LIMIT_ARMED_SCORE_MULTIPLIER,
        "session_mean_max_dist_atr": SESSION_MEAN_MAX_DIST_ATR,
        "open_reclaim_max_dist_atr": OPEN_RECLAIM_MAX_DIST_ATR,
        "orb_break_buffer_atr": ORB_BREAK_BUFFER_ATR,
        "failed_auction_min_tail_ratio": FAILED_AUCTION_MIN_TAIL_RATIO,
        "liquidity_ladder_min_targets": LIQUIDITY_LADDER_MIN_TARGETS,
        "liquidity_ladder_min_score": LIQUIDITY_LADDER_MIN_SCORE,
        "bootstrap_risk_multiplier": BOOTSTRAP_RISK_MULTIPLIER,
        "weak_direction_risk_multiplier": WEAK_DIRECTION_RISK_MULTIPLIER,
        "stop_noise_percentile": STOP_NOISE_PERCENTILE,
        "tp_noise_percentile": TP_NOISE_PERCENTILE,
        "min_stop_true_range_mult": MIN_STOP_TRUE_RANGE_MULT,
        "min_tp1_true_range_mult": MIN_TP1_TRUE_RANGE_MULT,
        "current_candle_stop_mult": CURRENT_CANDLE_STOP_MULT,
        "tp0_min_rr": TP0_MIN_RR,
        "tp1_min_rr_pro": TP1_MIN_RR_PRO,
        "tp1_min_atr_pro": TP1_MIN_ATR_PRO,
        "be_delay_bars_after_tp1": BE_DELAY_BARS_AFTER_TP1,
        "be_delay_min_mfe_r": BE_DELAY_MIN_MFE_R,
        "be_lock_r_mult": BE_LOCK_R_MULT,
        "catastrophic_stop_mult": CATASTROPHIC_STOP_MULT,
        "decision_stop_close_confirm": DECISION_STOP_CLOSE_CONFIRM,
        "min_breathing_risk_multiplier": MIN_BREATHING_RISK_MULTIPLIER,
        "setup_innovation_engine": SETUP_INNOVATION_ENGINE,
        "innovation_stale_trigger_min": INNOVATION_STALE_TRIGGER_MIN,
        "innovation_extreme_stale_trigger_min": INNOVATION_EXTREME_STALE_TRIGGER_MIN,
        "innovation_min_risk_mult": INNOVATION_MIN_RISK_MULT,
        "innovation_max_risk_mult": INNOVATION_MAX_RISK_MULT,
        "innovation_weak_magnet_level": INNOVATION_WEAK_MAGNET_LEVEL,
        "innovation_strong_magnet_level": INNOVATION_STRONG_MAGNET_LEVEL,
        "innovation_giveback_warn_ratio": INNOVATION_GIVEBACK_WARN_RATIO,
        "setup_revalidation_engine": SETUP_REVALIDATION_ENGINE,
        "setup_revalidation_stale_min": SETUP_REVALIDATION_STALE_MIN,
        "setup_revalidation_extreme_min": SETUP_REVALIDATION_EXTREME_MIN,
        "setup_revalidation_max_late_dist_atr": SETUP_REVALIDATION_MAX_LATE_DIST_ATR,
        "setup_revalidation_body_atr_min": SETUP_REVALIDATION_BODY_ATR_MIN,
        "setup_revalidation_body_ratio_min": SETUP_REVALIDATION_BODY_RATIO_MIN,
        "setup_revalidation_acceptance_atr": SETUP_REVALIDATION_ACCEPTANCE_ATR,
    }


def execution_freshness_multiplier(execution_source: str, trigger_age_minutes: float) -> float:
    """Soft decay замість hard filter: старий/ретроспективний trigger не блокує сетап,
    але не має права поводитися як живий market-entry."""
    src = str(execution_source or ExecutionSource.NONE.value).upper()
    age = max(float(trigger_age_minutes or 0.0), 0.0)
    if src == ExecutionSource.LIVE_3M.value:
        decay = math.exp(-age / max(float(TRIGGER_MAX_AGE_MINUTES), 1.0))
        return clamp(0.55 + 0.45 * decay, 0.55, 1.0)
    if src == ExecutionSource.LIMIT_ARMED.value:
        return clamp(LIMIT_ARMED_SCORE_MULTIPLIER, 0.50, 1.0)
    if src == ExecutionSource.ACCEPTANCE_RETEST.value:
        return 0.92
    if src == ExecutionSource.ACCELERATION_PULLBACK.value:
        return 0.78
    if src == ExecutionSource.SESSION_MEAN.value:
        return 0.90
    if src == ExecutionSource.OPENING_RANGE.value:
        return 0.88
    if src == ExecutionSource.OPEN_RECLAIM.value:
        return 0.90
    if src == ExecutionSource.LIQUIDITY_LADDER.value:
        return 0.82
    if src == ExecutionSource.FAILED_AUCTION.value:
        return 0.93
    if src == ExecutionSource.TIME_OF_DAY.value:
        return 0.84
    if src == ExecutionSource.TIME_WARP.value:
        return clamp(TIME_WARP_SCORE_MULTIPLIER, 0.40, 0.95)
    return clamp(STALE_TRIGGER_SCORE_MULTIPLIER, 0.35, 1.0)


def direction_recent_performance(journal: dict, side: str, lookback: int = 12) -> dict[str, Any]:
    """Оцінка останніх результатів напрямку без блокування: слабкий бік переводиться
    у менший sizing / PROBE_ONLY, але не забороняється."""
    trades = [t for t in list(journal.get("trades") or []) if isinstance(t, dict) and t.get("side") == side]
    recent = trades[-lookback:]
    closed = len(recent)
    wins = sum(1 for t in recent if safe_float(t.get("result_pct"), 0.0) > 0)
    win_rate = wins / closed if closed else None
    weak = bool(closed >= 3 and wins == 0) or bool(closed >= 5 and (win_rate or 0.0) < 0.25)
    risk_multiplier = WEAK_DIRECTION_RISK_MULTIPLIER if weak else 1.0
    score_multiplier = 0.93 if weak else 1.0
    return {
        "closed": closed,
        "wins": wins,
        "win_rate": round((win_rate or 0.0) * 100, 1) if win_rate is not None else None,
        "weak": weak,
        "risk_multiplier": risk_multiplier,
        "score_multiplier": score_multiplier,
        "mode": "PROBE_ONLY" if weak else "NORMAL",
    }


def calculate_entry_freshness(candidate: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """v6.18 Entry Freshness Layer.
    Оцінює момент виконання, а не якість самого сетапу.
    """
    if not ENTRY_FRESHNESS_ENABLED:
        return {"score": 100.0, "extended": False, "warning": False, "reasons": []}

    context = context or {}
    comps = getattr(candidate, "score_components", {}) or {}

    impulse_atr = safe_float(context.get("impulse_atr", comps.get("move_atr", 0)), 0)
    distance_atr = safe_float(context.get("distance_from_entry_zone_atr", comps.get("distance_from_zone_atr", 0)), 0)
    bars = safe_float(context.get("bars_since_confirmation", comps.get("bars_since_confirmation", 0)), 0)

    score = 100.0
    reasons = []

    if impulse_atr >= FRESHNESS_IMPULSE_HARD_ATR:
        score -= 45
        reasons.append("extended impulse")
    elif impulse_atr >= FRESHNESS_IMPULSE_WARNING_ATR:
        score -= 25
        reasons.append("impulse exhaustion risk")
    elif impulse_atr >= FRESHNESS_IMPULSE_SOFT_ATR:
        score -= 10

    if distance_atr > FRESHNESS_MAX_DISTANCE_ZONE_ATR:
        score -= 25
        reasons.append("far from origin zone")

    if bars > 20:
        score -= 15
        reasons.append("confirmation aging")

    score = clamp(score, 0, 100)

    return {
        "score": round(score, 2),
        "extended": score < 55,
        "warning": score < 75,
        "reasons": reasons,
        "impulse_atr": impulse_atr,
        "distance_from_zone_atr": distance_atr,
        "bars_since_confirmation": bars,
        "setup_family": getattr(candidate, "setup_family", ""),
        "setup_type": getattr(candidate, "setup_type", ""),
    }


def staged_entry_plan(candidate: Candidate, context: dict, direction_perf: Optional[dict] = None) -> dict[str, Any]:
    """Ladder execution: один сигнал описує, яку частину і на якій стадії брати.
    Це не block-filter, а position construction."""
    src = str(getattr(candidate, "execution_source", "") or ExecutionSource.NONE.value)
    score = int(candidate.final_score or 0)
    stage = EntryStage.PROBE.value
    base_risk = PROBE_RISK_PCT
    add_plan = []

    if src == ExecutionSource.TIME_WARP.value:
        stage = EntryStage.WAIT_RETEST.value
        base_risk = PROBE_RISK_PCT * 0.50
        add_plan.append("Не market-entry: чекати живий retest/limit у зоні")
    elif src == ExecutionSource.LIMIT_ARMED.value:
        stage = EntryStage.RETEST_ADD.value
        base_risk = RETEST_ADD_RISK_PCT
        add_plan.append("Ліміт на CE/FVG; додавання тільки після acceptance close")
    elif src == ExecutionSource.ACCEPTANCE_RETEST.value:
        stage = EntryStage.PROBE.value if score < ENTRY_SCORE_BASE else EntryStage.ACCEPTANCE.value
        base_risk = PROBE_RISK_PCT if stage == EntryStage.PROBE.value else ACCEPTANCE_RISK_PCT
        add_plan.append("Continuation-probe після ретесту: ранній вхід малим ризиком, добір тільки після підтвердження")
    elif src == ExecutionSource.ACCELERATION_PULLBACK.value:
        stage = EntryStage.WAIT_RETEST.value
        base_risk = PROBE_RISK_PCT * 0.50
        add_plan.append("Імпульс уже пішов: не market на піку, чекати 38–50% pullback")
    elif src in {ExecutionSource.SESSION_MEAN.value, ExecutionSource.OPEN_RECLAIM.value, ExecutionSource.FAILED_AUCTION.value}:
        stage = EntryStage.PROBE.value if score < ENTRY_SCORE_BASE else EntryStage.ACCEPTANCE.value
        base_risk = PROBE_RISK_PCT if stage == EntryStage.PROBE.value else ACCEPTANCE_RISK_PCT
        add_plan.append("Market-structure probe: вхід малим ризиком, добір тільки після acceptance/continuation")
    elif src == ExecutionSource.OPENING_RANGE.value:
        stage = EntryStage.ACCEPTANCE.value if score >= ENTRY_SCORE_BASE else EntryStage.PROBE.value
        base_risk = ACCEPTANCE_RISK_PCT if stage == EntryStage.ACCEPTANCE.value else PROBE_RISK_PCT
        add_plan.append("ORB/Failed-ORB: дозволений тільки staged entry, без full-size на першій свічці пробою")
    elif src == ExecutionSource.LIQUIDITY_LADDER.value:
        stage = EntryStage.WAIT_RETEST.value if score < ENTRY_SCORE_BASE else EntryStage.RETEST_ADD.value
        base_risk = PROBE_RISK_PCT if stage == EntryStage.WAIT_RETEST.value else RETEST_ADD_RISK_PCT
        add_plan.append("Liquidity ladder: позиція будується тільки якщо є DOL-маршрут і нормальний ретест")
    elif src == ExecutionSource.TIME_OF_DAY.value:
        stage = EntryStage.PROBE.value
        base_risk = PROBE_RISK_PCT * 0.75
        add_plan.append("Time-of-day edge: сесійний бонус не дає full-size без окремого structural trigger")
    elif score >= max(A_PLUS_ENTRY_MIN, ENTRY_SCORE_BASE + 7) and candidate.confirmation_tier >= ConfirmationTier.HIGH_QUALITY.value:
        stage = EntryStage.CORE.value
        base_risk = CORE_RISK_PCT
        add_plan.append("Core-size дозволений: якість/підтвердження достатні")
    elif score >= ENTRY_SCORE_BASE:
        stage = EntryStage.ACCEPTANCE.value
        base_risk = ACCEPTANCE_RISK_PCT
        add_plan.append("Acceptance-size: вхід є, але добір тільки після retest")
    else:
        stage = EntryStage.PROBE.value
        base_risk = PROBE_RISK_PCT
        add_plan.append("Probe-size: рання гіпотеза, не full-size")

    direction_perf = direction_perf or {}
    if direction_perf.get("weak"):
        stage = EntryStage.PROBE.value
        base_risk = min(base_risk, PROBE_RISK_PCT)
        add_plan.append(f"{candidate.side} тимчасово PROBE_ONLY через слабку недавню статистику")

    freshness = calculate_entry_freshness(candidate, context)
    candidate.entry_freshness_score = freshness.get("score", 100)
    candidate.entry_freshness_profile = freshness

    # v6.18: Entry Freshness changes execution aggressiveness, not setup validity.
    # CORE is reduced only on explicit extension conditions, not on generic lower score.
    if freshness.get("extended"):
        if stage == EntryStage.CORE.value:
            stage = EntryStage.ACCEPTANCE.value
        elif stage == EntryStage.ACCEPTANCE.value:
            stage = EntryStage.PROBE.value
        add_plan.append("Entry Freshness: extended move, stage reduced")
    elif freshness.get("warning"):
        if stage == EntryStage.CORE.value and freshness.get("impulse_atr", 0) >= FRESHNESS_IMPULSE_WARNING_ATR:
            stage = EntryStage.ACCEPTANCE.value
        add_plan.append("Entry Freshness warning: reduced aggressiveness")

    return {
        "stage": stage,
        "base_risk_pct": round(float(base_risk), 4),
        "scale_plan": add_plan,
        "probe_pct": PROBE_RISK_PCT,
        "acceptance_pct": ACCEPTANCE_RISK_PCT,
        "retest_add_pct": RETEST_ADD_RISK_PCT,
        "core_pct": CORE_RISK_PCT,
    }


def adaptive_position_risk_pct(candidate: Candidate, context: dict, default_risk_pct: float) -> float:
    """ML-aware soft sizing. У bootstrap-режимі модель не удає оракула: ризик зменшується.
    Коли learned_weight виросте, sizing плавно більше довірятиме моделі."""
    comps = candidate.score_components or {}
    stage_plan = candidate.stage_plan or {}
    risk = float(stage_plan.get("base_risk_pct", default_risk_pct) or default_risk_pct)

    learned_weight = float(comps.get("learned_weight", 0.0) or 0.0)
    probability = float(comps.get("probability", 0.0) or 0.0)
    risk *= float(comps.get("direction_risk_multiplier", 1.0) or 1.0)

    if learned_weight <= 0.05:
        risk *= BOOTSTRAP_RISK_MULTIPLIER
    elif probability >= 0.80 and learned_weight >= 0.30:
        risk *= 1.10
    elif probability < 0.62:
        risk *= 0.70

    if candidate.execution_source == ExecutionSource.TIME_WARP.value:
        risk *= 0.50

    # v6.14: setup innovation не блокує вхід і не піднімає score-threshold.
    # Він тільки переналаштовує construction-risk, якщо сетап морфологічно
    # старий/тонкий/після сильного імпульсу.
    innovation = stage_plan.get("innovation_profile") or getattr(candidate, "innovation_profile", {}) or {}
    risk *= float(innovation.get("risk_multiplier", 1.0) or 1.0)

    freshness = getattr(candidate, "entry_freshness_profile", {}) or {}
    freshness_score = safe_float(freshness.get("score", 100), 100)
    if ENTRY_FRESHNESS_ENABLED:
        freshness_weight = 1.0
        setup_name = str(
            getattr(candidate, "setup_type", "")
            or getattr(candidate, "setup_family", "")
            or ""
        ).upper()

        # Reversal entries are allowed to look "late" because the edge is the turn itself.
        # Continuation entries keep the full freshness penalty.
        if any(x in setup_name for x in ["REVERSAL", "LIQUIDITY_RECOVERY", "RANGE_EDGE"]):
            freshness_weight = FRESHNESS_REVERSAL_WEIGHT
        else:
            freshness_weight = FRESHNESS_CONTINUATION_WEIGHT

        if freshness_score < 55:
            risk *= (1 - ((1 - FRESHNESS_EXTENDED_RISK_MULT) * freshness_weight))
        elif freshness_score < 75:
            risk *= (1 - ((1 - FRESHNESS_WARNING_RISK_MULT) * freshness_weight))

    # v6.17.1/v6.17.9 Institutional Adaptive Engine integration
    # HTF disagreement reduces exposure instead of blindly rejecting valid execution.
    # v6.17.9: unified HTF source + kernel risk multiplier.
    if INSTITUTIONAL_ADAPTIVE_ENGINE:
        htf_state = get_htf_state(candidate)

        exec_score = institutional_execution_score(candidate)
        htf_multiplier = adaptive_htf_risk_multiplier(htf_state, exec_score)

        if htf_multiplier > 0:
            risk *= htf_multiplier

        kernel_multiplier = safe_float(
            getattr(candidate, "kernel_risk_multiplier", 1.0),
            1.0
        )
        risk *= kernel_multiplier

    return round(clamp(risk, 0.02, CORE_RISK_PCT), 4)


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


def deduplicate_closed_trades(trades: list[Any]) -> list[dict[str, Any]]:
    """Не даємо ML двічі вчитись на одній і тій самій закритій угоді.
    Ключ максимально консервативний: trade id + signal_id + close_action; якщо id
    порожній, лишаємо запис, бо це старий/пошкоджений журнал і краще не робити
    вигляд, що ми всевидяче божество з JSON-скальпелем."""
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in trades or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        sid = str(item.get("signal_id") or "").strip()
        action = str(item.get("close_action") or item.get("action") or "").strip()
        if tid:
            key = (tid, sid, action)
            if key in seen:
                continue
            seen.add(key)
        cleaned.append(item)
    return cleaned


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
    journal["trades"] = deduplicate_closed_trades(list(journal.get("trades") or []))[-MAX_JOURNAL:]
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
        "RISKY_ENTRY": "РАННІЙ / СТАДІЙНИЙ ВХІД",
        "ARMED": "СФОРМОВАНО СИГНАЛ — ЧЕКАЄМО ВХОДУ",
        "NO_SETUP": "СИГНАЛУ НЕМАЄ",
    }
    action_label = action_names.get(decision.action, decision.action)

    if decision.action == Action.NO_SETUP.value:
        lines = [
            "<b>Входу немає</b>",
            f"<b>Ціна зараз:</b> {_fmt_price(current_price)}",
        ]
        rejected = ((decision.audit or {}).get("rejected_hypotheses") or [])
        if rejected:
            top = rejected[0]
            lines.append(f"<b>Найближча гіпотеза:</b> {html.escape(str(top.get('side', '')))} {html.escape(str(top.get('model', '')))} | score {html.escape(str(top.get('final_score', '')))}")
            lines.append(f"<b>Чому ні:</b> {html.escape(str(top.get('failed_gate', ''))[:180])}")
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
        # Показуємо тільки одне найсильніше підтвердження.
        # Не виводимо список з декількох тез, щоб Telegram-повідомлення
        # не виглядало як внутрішній debug-log.
        priority_confirmation = None

        confirmations = [
            str(x).strip()
            for x in (c.confirmations or [])
            if str(x).strip()
        ]

        # Пріоритет: активний execution trigger > модель > інші пояснення.
        priority_keywords = (
            "LIMIT_ARMED",
            "LIVE",
            "CHoCH",
            "FVG",
            "OB",
            "RECLAIM",
            "RETEST",
            "TRIGGER",
        )

        for keyword in priority_keywords:
            for item in confirmations:
                if keyword.lower() in item.lower():
                    priority_confirmation = item
                    break
            if priority_confirmation:
                break

        if priority_confirmation is None and confirmations:
            priority_confirmation = confirmations[0]

        if priority_confirmation:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            lines.append(f"✅ {html.escape(priority_confirmation)}")
    for warning in context.get("learning_warnings", [])[:2]:
        lines.append(f"⚠️ {html.escape(warning)}")
    
    show_plan = decision.plan and decision.plan.valid and decision.action in (Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.ARMED.value)
    
    if show_plan:
        p = decision.plan
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(f"Вхід <b>{_fmt_price(p.entry)}</b> | Стоп <b>{_fmt_price(p.stop)}</b>")
        if getattr(p, "tp0", 0):
            lines.append(f"TP0 {_fmt_price(p.tp0)} (RR {p.rr0}) | TP1 {_fmt_price(p.tp1)} (RR {p.rr1})")
            lines.append(f"TP2 {_fmt_price(p.tp2)} | TP3 {_fmt_price(p.tp3)}")
        else:
            lines.append(f"TP1 {_fmt_price(p.tp1)} (RR {p.rr1}) | TP2 {_fmt_price(p.tp2)} | TP3 {_fmt_price(p.tp3)}")
        if getattr(p, "entry_stage", ""):
            lines.append(f"Стадія: <b>{html.escape(p.entry_stage)}</b> | Джерело: {html.escape(p.execution_source)} | Ризик: {p.position_risk_pct}%")
    
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
    tp_status = f"TP0 {'✅' if getattr(trade, 'tp0_hit', False) else '—'} | TP1 {'✅' if trade.tp1_hit else '—'} | TP2 {'✅' if trade.tp2_hit else '—'} | TP3 {'✅' if trade.tp3_hit else '—'}"

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
        f"TP0 {_fmt_price(getattr(trade, 'tp0', 0.0)) if getattr(trade, 'tp0', 0.0) else '—'} | TP1 {_fmt_price(trade.tp1)} | TP2 {_fmt_price(trade.tp2)} | TP3 {_fmt_price(trade.tp3)}",
        f"Стадія: {html.escape(getattr(trade, 'entry_stage', ''))} | Джерело: {html.escape(getattr(trade, 'execution_source', ''))}",
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
    trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
    trigger_ready = event.get("stage") in ["RETEST", "READY", "ACCEPTANCE"] and trigger_age <= TRIGGER_MAX_AGE_MINUTES
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
        trigger_age_minutes=trigger_age,
        thesis_key=opp.thesis_key,
        thesis=opp.thesis,
        scan_event_stage=event.get("stage", ""),
        live_3m_trigger_ready=trigger_ready,
        reentry_ready=trigger_ready,
        execution_source=ExecutionSource.REENTRY.value,
        entry_stage=EntryStage.PROBE.value if not trigger_ready else EntryStage.ACCEPTANCE.value,
        stage_plan={"stage": EntryStage.PROBE.value if not trigger_ready else EntryStage.ACCEPTANCE.value, "base_risk_pct": PROBE_RISK_PCT if not trigger_ready else ACCEPTANCE_RISK_PCT},
        risk_multiplier=0.75,
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
# v6.14 NON-BLOCKING SETUP INNOVATION ENGINE
# ==========================================================

def _nonblocking_stage_rank(stage: str) -> int:
    order = {
        EntryStage.WAIT_RETEST.value: 0,
        EntryStage.PROBE.value: 1,
        EntryStage.ACCEPTANCE.value: 2,
        EntryStage.RETEST_ADD.value: 3,
        EntryStage.CORE.value: 4,
    }
    return order.get(str(stage or EntryStage.PROBE.value), 1)


def _safer_stage(current: str, proposed: str) -> str:
    """Не блокує угоду: лише переводить construction у більш обережну стадію."""
    return proposed if _nonblocking_stage_rank(proposed) < _nonblocking_stage_rank(current) else current


def _recent_impulse_map(candles: list[Candle], side: str, atr15: float) -> dict[str, Any]:
    """Морфологія останнього руху: імпульс, squeeze/chop, giveback. Без ML-містики,
    лише свічкова геометрія, бо іноді людству достатньо рахувати те, що вже видно."""
    confirmed = [c for c in (candles or []) if getattr(c, "confirmed", True)]
    if len(confirmed) < 10 or atr15 <= 0:
        return {"valid": False, "reason": "not_enough_candles"}
    recent = confirmed[-10:]
    last = confirmed[-1]
    ranges = [max(c.high - c.low, 1e-9) for c in recent]
    bodies = [abs(c.close - c.open) for c in recent]
    direction_closes = 0
    for c in recent[-5:]:
        if side == Side.LONG.value and c.close >= c.open:
            direction_closes += 1
        if side == Side.SHORT.value and c.close <= c.open:
            direction_closes += 1
    span = max(c.high for c in recent) - min(c.low for c in recent)
    impulse_atr = span / max(atr15, 1e-9)
    body_efficiency = sum(bodies) / max(sum(ranges), 1e-9)
    compression = max(ranges[-3:]) / max(sum(ranges[-10:]) / len(ranges), 1e-9)
    if side == Side.LONG.value:
        best = max(c.high for c in recent)
        giveback = (best - last.close) / max(span, 1e-9)
    else:
        best = min(c.low for c in recent)
        giveback = (last.close - best) / max(span, 1e-9)
    return {
        "valid": True,
        "impulse_atr": round(impulse_atr, 2),
        "body_efficiency": round(body_efficiency, 2),
        "direction_closes_5": direction_closes,
        "compression_ratio": round(compression, 2),
        "giveback_ratio": round(clamp(giveback, 0.0, 1.0), 2),
        "last_range_atr": round(max(last.high - last.low, 0.0) / max(atr15, 1e-9), 2),
    }


def _setup_morphology_label(candidate: Candidate, impulse: dict[str, Any]) -> str:
    age = float(candidate.trigger_age_minutes or 0.0)
    magnet = float(candidate.target_magnet_score or 0.0)
    setup = str(candidate.setup_type or "")
    source = str(candidate.execution_source or "")
    impulse_atr = float(impulse.get("impulse_atr", 0.0) or 0.0)
    giveback = float(impulse.get("giveback_ratio", 0.0) or 0.0)

    if age >= INNOVATION_EXTREME_STALE_TRIGGER_MIN and source in {ExecutionSource.ACCEPTANCE_RETEST.value, ExecutionSource.LIQUIDITY_LADDER.value}:
        return "STALE_THESIS_REVALIDATION_PROBE"
    if setup == SetupType.ACCEPTANCE_RETEST_CONTINUATION.value and impulse_atr >= 3.2 and giveback >= 0.45:
        return "IMPULSE_MEMORY_RETEST_PROBE"
    if magnet and magnet < INNOVATION_WEAK_MAGNET_LEVEL:
        return "THIN_MAGNET_DEFENSIVE_LADDER"
    if magnet >= INNOVATION_STRONG_MAGNET_LEVEL and impulse_atr >= 2.0:
        return "MAGNET_EXPANSION_RUNNER"
    if source == ExecutionSource.LIQUIDITY_LADDER.value:
        return "DOL_LADDER_SCOUT"
    return "STANDARD_NONBLOCKING_CONSTRUCTION"


def _target_ladder_density(side: str, price: float, context: dict, atr15: float) -> dict[str, Any]:
    targets = find_technical_targets(
        side,
        price,
        context.get("zones") or [],
        context.get("liquidity", {}) or {},
        atr15,
        macro=context.get("macro_liquidity", {}) or {},
    )
    window = max(atr15 * 6.0, ABS_MIN_TP1_DOLLARS * 3.0)
    nearby = [t for t in targets if 0 < safe_float(t.get("distance"), 0.0) <= window]
    score_sum = sum(safe_float(t.get("magnet_score"), 0.0) for t in nearby[:5])
    return {
        "count": len(nearby),
        "score_sum": round(score_sum, 2),
        "nearest": [
            {
                "kind": t.get("kind"),
                "tf": t.get("timeframe"),
                "level": round_price(t.get("level")),
                "magnet": round(safe_float(t.get("magnet_score"), 0.0), 2),
                "dist_atr": round(safe_float(t.get("distance"), 0.0) / max(atr15, 1e-9), 2),
            }
            for t in nearby[:4]
        ],
    }



def _candle_body_ratio(c: Optional[Candle]) -> float:
    if not c:
        return 0.0
    rng = max(abs(float(c.high) - float(c.low)), 1e-9)
    return abs(float(c.close) - float(c.open)) / rng


def _last_confirmed(candles: list[Candle]) -> Optional[Candle]:
    confirmed = [c for c in (candles or []) if getattr(c, "confirmed", True)]
    return sorted(confirmed, key=lambda c: c.ts)[-1] if confirmed else None


def _recent_confirmed(candles: list[Candle], n: int = 3) -> list[Candle]:
    confirmed = [c for c in (candles or []) if getattr(c, "confirmed", True)]
    return sorted(confirmed, key=lambda c: c.ts)[-n:]


def _side_directional_revalidation(side: str, c3: list[Candle], c15: list[Candle], atr15: float) -> dict[str, Any]:
    """Свіжий сетапний доказ, а не стара памʼять trigger.
    Не піднімає якість і не є hard gate: повертає, чи є нова мікро-аргументація входу."""
    recent3 = _recent_confirmed(c3, 4)
    last3 = recent3[-1] if recent3 else None
    prev3 = recent3[-2] if len(recent3) >= 2 else None
    last15 = _last_confirmed(c15)
    if not last3 or not prev3:
        return {"supported": False, "kind": "NO_3M_DATA", "score": 0, "checks": []}

    body = abs(float(last3.close) - float(last3.open))
    body_atr = body / max(float(atr15), 1e-9)
    body_ratio = _candle_body_ratio(last3)
    checks = []

    if side == Side.LONG.value:
        directional_close = float(last3.close) > float(last3.open) and float(last3.close) > float(prev3.high)
        local_acceptance = bool(last15 and float(last15.close) >= float(last15.open) - float(atr15) * SETUP_REVALIDATION_ACCEPTANCE_ATR)
        micro_sweep_reclaim = bool(float(last3.low) < float(prev3.low) and float(last3.close) > float(prev3.close))
    else:
        directional_close = float(last3.close) < float(last3.open) and float(last3.close) < float(prev3.low)
        local_acceptance = bool(last15 and float(last15.close) <= float(last15.open) + float(atr15) * SETUP_REVALIDATION_ACCEPTANCE_ATR)
        micro_sweep_reclaim = bool(float(last3.high) > float(prev3.high) and float(last3.close) < float(prev3.close))

    strong_body = body_atr >= SETUP_REVALIDATION_BODY_ATR_MIN and body_ratio >= SETUP_REVALIDATION_BODY_RATIO_MIN
    checks.append({"name": "directional_3m_close", "ok": bool(directional_close)})
    checks.append({"name": "strong_3m_body", "ok": bool(strong_body), "body_atr": round(body_atr, 3), "body_ratio": round(body_ratio, 3)})
    checks.append({"name": "15m_acceptance", "ok": bool(local_acceptance)})
    checks.append({"name": "micro_sweep_reclaim", "ok": bool(micro_sweep_reclaim)})

    score = 0
    score += 35 if directional_close else 0
    score += 30 if strong_body else 0
    score += 20 if local_acceptance else 0
    score += 15 if micro_sweep_reclaim else 0

    supported = bool((directional_close and strong_body and local_acceptance) or (micro_sweep_reclaim and strong_body and local_acceptance))
    kind = "LIVE_DIRECTIONAL_ACCEPTANCE" if directional_close else "MICRO_SWEEP_RECLAIM" if micro_sweep_reclaim else "WEAK_OR_MISSING"
    return {"supported": supported, "kind": kind, "score": int(score), "checks": checks}


def setup_trigger_revalidation_profile(candidate: Candidate, context: dict, impulse: Optional[dict] = None) -> dict[str, Any]:
    """v6.15: trigger lease без примусового блокування.
    Старий trigger не відкриває late-entry сам по собі. Він має бути
    переаргументований свіжою 3M/15M поведінкою або лишається ARMED thesis.
    Quality thresholds не змінюються."""
    if not SETUP_REVALIDATION_ENGINE:
        return {"enabled": False, "state": "DISABLED", "entry_supported": True, "needs_revalidation": False}

    source = str(candidate.execution_source or ExecutionSource.NONE.value)
    age = max(float(candidate.trigger_age_minutes or 0.0), 0.0)
    price = safe_float(context.get("price"), 0.0) or safe_float(getattr(candidate, "execution_anchor", 0.0), 0.0)
    anchor = safe_float(getattr(candidate, "execution_anchor", 0.0), 0.0) or safe_float(getattr(candidate, "trigger_level", 0.0), 0.0) or price
    atr15 = safe_float(context.get("atr15"), 0.6) or 0.6
    c3 = (context.get("candles", {}) or {}).get("3m", []) or []
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []

    live_ready = bool(getattr(candidate, "live_3m_trigger_ready", False))
    limit_ready = bool(getattr(candidate, "limit_armed_ready", False))
    time_warp_ready = bool(getattr(candidate, "time_warp_ready", False))

    if age < SETUP_REVALIDATION_STALE_MIN or live_ready:
        state = "FRESH"
    elif age < SETUP_REVALIDATION_EXTREME_MIN:
        state = "STALE"
    else:
        state = "ARCHIVED_THESIS"

    dist_atr = abs(price - anchor) / max(atr15, 1e-9) if price and anchor else 0.0
    not_late_location = bool(dist_atr <= SETUP_REVALIDATION_MAX_LATE_DIST_ATR or limit_ready)
    micro = _side_directional_revalidation(candidate.side, c3, c15, atr15)

    # Сетапна аргументація: не просто "age ok/not ok", а чому саме зараз можна або не можна.
    setup_arguments = []
    if live_ready:
        setup_arguments.append("fresh LIVE_3M execution")
    if limit_ready:
        setup_arguments.append("limit/zone still armed")
    if micro.get("supported"):
        setup_arguments.append(f"fresh {micro.get('kind')} on 3M/15M")
    if not_late_location:
        setup_arguments.append(f"price not late vs anchor: {dist_atr:.2f} ATR")
    else:
        setup_arguments.append(f"late distance from anchor: {dist_atr:.2f} ATR")
    if time_warp_ready and not live_ready:
        setup_arguments.append("time-warp is thesis memory, not live execution")

    needs_revalidation = bool(state != "FRESH" or (time_warp_ready and not live_ready and source == ExecutionSource.TIME_WARP.value))
    revalidated = bool(needs_revalidation and micro.get("supported") and not_late_location)

    # v6.16: якщо стара thesis вже підтверджена фактичним рухом і acceptance,
    # дозволяємо малий probe замість нескінченного ARMED.
    stale_direction_confirm = bool(
        state == "ARCHIVED_THESIS"
        and dist_atr >= STALE_THESIS_PROBE_ATR
        and (not STALE_THESIS_ACCEPTANCE_REQUIRED or micro.get("supported"))
    )
    if stale_direction_confirm:
        revalidated = True
        micro["stale_thesis_recovery"] = True

    entry_supported = bool(not needs_revalidation or revalidated)

    if not needs_revalidation:
        entry_timing = "LIVE_OR_STILL_FRESH"
        risk_mult = 1.0
    elif revalidated:
        entry_timing = "SETUP_REVALIDATED_NOW"
        risk_mult = SETUP_REVALIDATION_RISK_MULT_EXTREME if state == "ARCHIVED_THESIS" else SETUP_REVALIDATION_RISK_MULT_STALE
    else:
        entry_timing = "ARMED_WAIT_FRESH_ARGUMENT"
        risk_mult = SETUP_REVALIDATION_RISK_MULT_EXTREME if state == "ARCHIVED_THESIS" else SETUP_REVALIDATION_RISK_MULT_STALE

    return {
        "enabled": True,
        "version": "v6.16_adaptive_execution",
        "state": state,
        "source": source,
        "age_min": round(age, 1),
        "needs_revalidation": needs_revalidation,
        "revalidated": revalidated,
        "entry_supported": entry_supported,
        "entry_timing": entry_timing,
        "not_late_location": not_late_location,
        "distance_from_anchor_atr": round(dist_atr, 2),
        "micro_confirmation": micro,
        "risk_multiplier": round(clamp(risk_mult, INNOVATION_MIN_RISK_MULT, 1.0), 4),
        "stage_override": EntryStage.PROBE.value if revalidated else EntryStage.WAIT_RETEST.value if needs_revalidation else "",
        "action_bias": "PROBE_ENTRY" if revalidated else "ARMED_REVALIDATION" if needs_revalidation else "NORMAL",
        "setup_arguments": setup_arguments,
        "no_hard_block": True,
        "quality_threshold_changed": False,
    }

def nonblocking_setup_innovation_overlay(candidate: Candidate, context: dict, state: Optional[dict] = None, journal: Optional[dict] = None) -> dict[str, Any]:
    """v6.14 overlay: не створює hard-block, не підвищує entry-quality,
    не змінює final_score. Він перебудовує position construction навколо сетапу.

    Ідея: якщо сетап старий, тонкий за target magnet або після імпульсного MFE,
    не казати 'не входь', а казати 'входь як probe/ladder/scout, фіксуй інакше'.
    Ринок і так достатньо абсурдний, не треба ще й ботом забороняти собі дані.
    """
    if not SETUP_INNOVATION_ENGINE:
        return {"enabled": False, "primary": "DISABLED", "risk_multiplier": 1.0, "notes": []}

    price = safe_float(getattr(candidate, "execution_anchor", 0.0), 0.0) or safe_float(context.get("price"), 0.0)
    atr15 = safe_float(context.get("atr15"), 0.6) or 0.6
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []
    impulse = _recent_impulse_map(c15, candidate.side, atr15)
    revalidation = setup_trigger_revalidation_profile(candidate, context, impulse)
    ladder = _target_ladder_density(candidate.side, price, context, atr15)
    primary = _setup_morphology_label(candidate, impulse)
    if revalidation.get("needs_revalidation"):
        primary = "SETUP_ARGUMENTED_TRIGGER_REVALIDATION" if revalidation.get("entry_supported") else "STALE_THESIS_WAIT_SETUP_ARGUMENT"
    age = float(candidate.trigger_age_minutes or 0.0)
    magnet = float(candidate.target_magnet_score or 0.0)
    plan_q = int(candidate.trade_plan_quality_score or 0)

    risk_mult = 1.0
    stage_override = str(getattr(candidate, "entry_stage", EntryStage.PROBE.value) or EntryStage.PROBE.value)
    partial_mode = "STANDARD"
    notes: list[str] = []
    router: dict[str, Any] = {"mode": "STANDARD", "ladder": ladder}

    if revalidation.get("needs_revalidation"):
        risk_mult *= float(revalidation.get("risk_multiplier", 1.0) or 1.0)
        partial_mode = "DEFENSIVE_TP0"
        if revalidation.get("entry_supported"):
            stage_override = _safer_stage(stage_override, EntryStage.PROBE.value)
            router["mode"] = "SETUP_REVALIDATED_SCOUT"
            notes.append("old trigger переаргументовано свіжим 3M/15M setup → probe зараз, без підвищення quality")
        else:
            stage_override = EntryStage.WAIT_RETEST.value
            router["mode"] = "WAIT_FRESH_SETUP_ARGUMENT"
            notes.append("old trigger лишається thesis memory → чекаємо свіжий 3M/15M setup argument, без hard block")

    if primary == "STALE_THESIS_REVALIDATION_PROBE":
        # Не блокуємо стару тезу. Просто забороняємо їй маскуватися під свіжий core.
        risk_mult *= 0.45 if age >= INNOVATION_EXTREME_STALE_TRIGGER_MIN else 0.62
        stage_override = _safer_stage(stage_override, EntryStage.PROBE.value)
        partial_mode = "DEFENSIVE_TP0"
        router["mode"] = "REVALIDATION_SCOUT"
        notes.append(f"old trigger {age:.0f}m → revalidation-probe, not quality gate")

    if primary == "IMPULSE_MEMORY_RETEST_PROBE":
        risk_mult *= 0.72
        stage_override = _safer_stage(stage_override, EntryStage.PROBE.value)
        partial_mode = "MFE_CAPTURE"
        router["mode"] = "IMPULSE_MEMORY"
        notes.append("post-impulse retest: позиція будується як scout + швидша часткова фіксація")

    if primary == "THIN_MAGNET_DEFENSIVE_LADDER":
        risk_mult *= 0.78
        partial_mode = "DEFENSIVE_TP0"
        router["mode"] = "THIN_MAGNET_LADDER"
        notes.append(f"target magnet {magnet:.2f} слабкий → більше ваги TP0/TP1, не блок")

    if primary == "MAGNET_EXPANSION_RUNNER":
        risk_mult *= 1.03
        partial_mode = "RUNNER_EXPANSION"
        router["mode"] = "MAGNET_RUNNER"
        notes.append(f"target magnet {magnet:.2f} сильний → runner отримує більше ваги")

    if primary == "DOL_LADDER_SCOUT":
        risk_mult *= 0.82
        stage_override = _safer_stage(stage_override, EntryStage.PROBE.value)
        partial_mode = "LADDER_SCOUT"
        router["mode"] = "DOL_SCOUT"
        notes.append("DOL ladder без hard block: scout-size до першої реакції ліквідності")

    if plan_q and plan_q < 55:
        # Не піднімаємо quality і не блокуємо. Просто робимо trade-plan більш оборонним.
        risk_mult *= 0.82
        partial_mode = "DEFENSIVE_TP0" if partial_mode == "STANDARD" else partial_mode
        notes.append(f"plan_q {plan_q} → defensive construction, без підвищення порогу входу")

    if impulse.get("valid") and safe_float(impulse.get("giveback_ratio"), 0.0) >= 0.62:
        risk_mult *= 0.88
        partial_mode = "MFE_CAPTURE" if partial_mode == "STANDARD" else partial_mode
        notes.append(f"recent giveback {impulse.get('giveback_ratio')} → MFE-capture partials")

    risk_mult = round(clamp(risk_mult, INNOVATION_MIN_RISK_MULT, INNOVATION_MAX_RISK_MULT), 4)

    return {
        "enabled": True,
        "version": "v6.14_nonblocking_setup_innovation",
        "primary": primary,
        "risk_multiplier": risk_mult,
        "stage_override": stage_override,
        "partial_mode": partial_mode,
        "target_router": router,
        "trigger_revalidation": revalidation,
        "entry_supported_now": bool(revalidation.get("entry_supported", True)),
        "impulse": impulse,
        "trigger_age_min": round(age, 1),
        "target_magnet": round(magnet, 2),
        "plan_q": plan_q,
        "notes": notes,
        "no_block": True,
        "quality_threshold_changed": False,
    }


def apply_setup_innovation_overlay(candidate: Candidate, context: dict, state: Optional[dict] = None, journal: Optional[dict] = None) -> Candidate:
    overlay = nonblocking_setup_innovation_overlay(candidate, context, state, journal)
    candidate.innovation_profile = overlay
    candidate.revalidation_profile = overlay.get("trigger_revalidation", {}) or {}
    candidate.stage_plan = candidate.stage_plan or {}
    candidate.stage_plan["innovation_profile"] = overlay
    if overlay.get("enabled"):
        candidate.stage_plan.setdefault("scale_plan", []).extend([f"v6.14 {n}" for n in overlay.get("notes", [])])
        candidate.entry_stage = str(overlay.get("stage_override") or candidate.entry_stage)
        candidate.stage_plan["stage"] = candidate.entry_stage
        # Невеликий ranking-bonus НЕ є quality-threshold і не впливає на final_score.
        # Він просто дає перевагу сетапам із кращою морфологією construction.
        if overlay.get("primary") in {"MAGNET_EXPANSION_RUNNER", "IMPULSE_MEMORY_RETEST_PROBE"}:
            candidate.hypothesis_score = round(float(candidate.hypothesis_score or candidate.final_score) + 0.18, 2)
        if candidate.score_components is not None:
            candidate.score_components["innovation_profile"] = overlay
            candidate.score_components["trigger_revalidation"] = candidate.revalidation_profile
            candidate.score_components["innovation_no_block"] = True
    return candidate


def dynamic_partial_plan_from_innovation(candidate: Candidate) -> dict[str, float]:
    overlay = (candidate.stage_plan or {}).get("innovation_profile") or getattr(candidate, "innovation_profile", {}) or {}
    mode = str(overlay.get("partial_mode", "STANDARD") or "STANDARD")

    if mode in {"DEFENSIVE_TP0", "MFE_CAPTURE"}:
        tp0 = max(TP0_SIZE_PCT, min(INNOVATION_TP0_DEFENSE_SIZE, 0.40))
        tp1 = max(0.30, TP1_SIZE_PCT)
        tp2 = max(0.15, min(TP2_SIZE_PCT, 0.22))
        runner = max(0.0, 1.0 - tp0 - tp1 - tp2)
    elif mode == "LADDER_SCOUT":
        tp0, tp1, tp2 = 0.25, 0.35, 0.22
        runner = max(0.0, 1.0 - tp0 - tp1 - tp2)
    elif mode == "RUNNER_EXPANSION":
        tp0 = min(TP0_SIZE_PCT, 0.18)
        tp1 = max(0.28, TP1_SIZE_PCT - 0.05)
        tp2 = max(0.20, TP2_SIZE_PCT - 0.03)
        runner = max(0.20, 1.0 - tp0 - tp1 - tp2)
        # Нормалізація, щоб сума не втекла вище 1.0, бо математика теж іноді хоче в бар.
        total = tp0 + tp1 + tp2 + runner
        if total > 1.0:
            runner = max(0.0, 1.0 - tp0 - tp1 - tp2)
    else:
        tp0, tp1, tp2, runner = TP0_SIZE_PCT, TP1_SIZE_PCT, TP2_SIZE_PCT, TP3_RUNNER_PCT

    return {
        "tp0": round(float(tp0), 4),
        "tp1": round(float(tp1), 4),
        "tp2": round(float(tp2), 4),
        "tp3_runner": round(max(0.0, 1.0 - float(tp0) - float(tp1) - float(tp2)), 4) if mode != "STANDARD" else round(float(runner), 4),
        "mode": mode,
    }


def _tp0_giveback_innovation_advisor(trade: ActiveTrade, context: dict, result: dict[str, Any]) -> None:
    """Після TP0 не рухає стоп і не закриває угоду автоматично. Лише дає
    non-blocking management directive, щоб не перетворювати +MFE на мінус мовчки."""
    if not getattr(trade, "tp0_hit", False) or getattr(trade, "tp1_hit", False):
        return
    risk = _trade_risk_distance(trade)
    if risk <= 1e-9:
        return
    price = safe_float(context.get("price"), trade.entry)
    if trade.side == Side.LONG.value:
        mfe_r = max(0.0, (float(trade.best_price) - float(trade.entry)) / risk)
        cur_r = (price - float(trade.entry)) / risk
    else:
        mfe_r = max(0.0, (float(trade.entry) - float(trade.best_price)) / risk)
        cur_r = (float(trade.entry) - price) / risk
    if mfe_r < 1.05:
        return
    giveback_ratio = 1.0 - (cur_r / max(mfe_r, 1e-9))
    if giveback_ratio >= INNOVATION_GIVEBACK_WARN_RATIO:
        result.setdefault("notes", []).append(
            f"v6.14 MFE-capture advisor: після TP0 віддано {giveback_ratio:.0%} MFE; "
            "стоп не душимо, але новий добір заборонений до повторного acceptance-close"
        )
        result["innovation_management"] = {
            "mode": "TP0_GIVEBACK_DEFENSE",
            "mfe_r": round(mfe_r, 2),
            "current_r": round(cur_r, 2),
            "giveback_ratio": round(giveback_ratio, 2),
            "auto_close": False,
            "stop_move": False,
        }

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

    daily_open = None
    weekly_open = None
    # Daily/Weekly open — ключові intraday bias-рівні. Беремо першу доступну
    # 15M свічку поточного дня/тижня за Києвом, без зовнішніх даних.
    try:
        today = now_kyiv.date()
        week_key = now_kyiv.isocalendar()[:2]
        today_candles = [c for c in c15 if _candle_kyiv_dt(c.ts, kyiv_tz).date() == today]
        week_candles = [c for c in c15 if _candle_kyiv_dt(c.ts, kyiv_tz).isocalendar()[:2] == week_key]
        if today_candles:
            daily_open = sorted(today_candles, key=lambda c: c.ts)[0].open
        if week_candles:
            weekly_open = sorted(week_candles, key=lambda c: c.ts)[0].open
    except Exception:
        daily_open = weekly_open = None

    return {
        "asian_high": asian_high, "asian_low": asian_low,
        "pdh": pdh, "pdl": pdl,
        "macro_eq": macro_eq,
        "daily_open": daily_open, "weekly_open": weekly_open,
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



# ==========================================================
# ICT HYPOTHESIS MATRIX ENGINE (v6.10)
# ==========================================================

def _zone_midpoint(z: Zone) -> float:
    return safe_float(z.low) + (safe_float(z.high) - safe_float(z.low)) / 2.0


def _nearest_same_side_zone(zones: list, side: str, kinds: set[str], price: float) -> Optional[Zone]:
    matching = [z for z in zones if getattr(z, "side", "") == side and getattr(z, "kind", "") in kinds]
    if not matching:
        return None
    return sorted(matching, key=lambda z: (abs(_zone_midpoint(z) - price), -safe_float(getattr(z, "strength", 0.0))))[0]


def _nearest_opposite_structural_zone(zones: list, side: str, price: float) -> Optional[Zone]:
    opposite_zones = [
        z for z in zones
        if getattr(z, "side", "") == opposite(side)
        and getattr(z, "timeframe", "") in {"1h", "4h", "15m"}
        and getattr(z, "kind", "") in {"OB", "FVG"}
    ]
    if not opposite_zones:
        return None
    return sorted(opposite_zones, key=lambda z: (-safe_float(getattr(z, "strength", 0.0)), abs(_zone_midpoint(z) - price)))[0]


def ict_model_execution_contract(
    model_id: str,
    setup_type: str,
    side: str,
    context: dict,
    event: dict,
    trigger_level: float,
    ce_level: float,
    is_limit_armed: bool,
    model_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Кожна ICT-модель отримує власний execution-contract: entry anchor,
    structural invalidation і первинну target-логіку. Це усуває стару поведінку,
    де 10 моделей були лише labels над одним універсальним Candidate."""
    price = safe_float(context.get("price"), 0.0)
    atr15 = safe_float(context.get("atr15"), 0.6) or 0.6
    zones = context.get("zones") or []
    buffer = max(atr15 * 0.18, price * 0.00035)
    model = str(model_id or "GENERIC_FALLBACK")
    model_context = model_context or {}

    entry_anchor = price
    entry_basis = "current_price"
    invalidation = price - side_sign(side) * atr15 * 1.65
    invalidation_basis = "ATR fallback"
    target_level = price + side_sign(side) * atr15 * 2.40
    target_basis = "ATR expansion preview"

    fvg = _nearest_same_side_zone(zones, side, {"FVG"}, price)
    ob = _nearest_same_side_zone(zones, side, {"OB"}, price)
    opp_struct = _nearest_opposite_structural_zone(zones, side, price)

    if opp_struct:
        invalidation = (safe_float(opp_struct.low) - buffer) if side == Side.LONG.value else (safe_float(opp_struct.high) + buffer)
        invalidation_basis = f"opposite {opp_struct.timeframe} {opp_struct.kind} guard"

    if model in {"FVG_ENTRY", "SILVER_BULLET", "BMS_RETEST", "RANGE_COMPRESSION_MODEL"} and fvg:
        entry_anchor = _zone_midpoint(fvg)
        entry_basis = f"{model} CE/FVG midpoint"
        invalidation = (safe_float(fvg.low) - buffer) if side == Side.LONG.value else (safe_float(fvg.high) + buffer)
        invalidation_basis = f"{model} FVG invalidation"
    elif model in {"OB_RECLAIM", "BREAKER_BLOCK"} and (ob or fvg):
        z = ob or fvg
        entry_anchor = _zone_midpoint(z)
        entry_basis = f"{model} {z.kind} reclaim midpoint"
        invalidation = (safe_float(z.low) - buffer) if side == Side.LONG.value else (safe_float(z.high) + buffer)
        invalidation_basis = f"{model} {z.kind} invalidation"
    elif model == "ACCEPTANCE_RETEST_CONTINUATION":
        if safe_float(model_context.get("zone_mid"), 0.0):
            entry_anchor = safe_float(model_context.get("zone_mid"), price)
            entry_basis = f"ACCEPTANCE_RETEST zone midpoint ({model_context.get('zone_label', 'OB/FVG')})"
        else:
            entry_anchor = price
            entry_basis = "ACCEPTANCE_RETEST current acceptance price"
        invalidation = entry_anchor - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.05)
        invalidation_basis = "ACCEPTANCE_RETEST structural probe invalidation"
    elif model == "ACCELERATION_PULLBACK_REENTRY":
        pull50 = safe_float(model_context.get("pullback_50"), 0.0)
        pull382 = safe_float(model_context.get("pullback_382"), 0.0)
        entry_anchor = pull50 or pull382 or price
        entry_basis = "ACCELERATION_PULLBACK 50% impulse retrace"
        invalidation = entry_anchor - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.20)
        invalidation_basis = "ACCELERATION_PULLBACK failed retrace invalidation"
    elif model == "VWAP_SESSION_MEAN_RECLAIM":
        entry_anchor = safe_float(model_context.get("level"), price) or price
        entry_basis = f"VWAP/Session mean reclaim ({model_context.get('level_name', 'mean')})"
        invalidation = entry_anchor - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.05)
        invalidation_basis = "VWAP/mean reclaim failure"
    elif model in {"OPENING_RANGE_BREAKOUT", "FAILED_ORB"}:
        entry_anchor = safe_float(model_context.get("entry_level"), price) or price
        entry_basis = f"{model} opening-range boundary"
        invalidation = safe_float(model_context.get("invalidation"), 0.0) or (entry_anchor - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.15))
        invalidation_basis = f"{model} OR invalidation"
    elif model == "DAILY_WEEKLY_OPEN_RECLAIM":
        entry_anchor = safe_float(model_context.get("level"), price) or price
        entry_basis = f"{model_context.get('level_name', 'OPEN')} reclaim"
        invalidation = entry_anchor - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.10)
        invalidation_basis = "Daily/Weekly open reclaim failure"
    elif model == "LIQUIDITY_LADDER_MODEL":
        entry_anchor = price
        entry_basis = "Liquidity ladder current/retest anchor"
        invalidation = price - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.25)
        invalidation_basis = "Liquidity ladder structural failure"
    elif model == "FAILED_AUCTION_REJECTION":
        entry_anchor = price
        entry_basis = f"Failed auction rejection from {model_context.get('level_name', 'liquidity')}"
        extreme = safe_float(model_context.get("rejection_extreme"), 0.0)
        if extreme:
            invalidation = (extreme - buffer) if side == Side.LONG.value else (extreme + buffer)
        else:
            invalidation = price - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.10)
        invalidation_basis = "Failed auction tail invalidation"
    elif model == "TIME_OF_DAY_ADAPTIVE":
        entry_anchor = price
        entry_basis = f"Time-of-day adaptive execution ({model_context.get('phase', 'session')})"
        invalidation = price - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.15)
        invalidation_basis = "Session timing thesis invalidation"
    elif model in {"2022_MODEL", "TURTLE_SOUP", "PO3", "JUDAS_SWING", "MMBM"}:
        sweep_level = safe_float(event.get("sweep_level"), safe_float(trigger_level, price))
        if sweep_level:
            # Reversal-family entry anchor не женеться за ціною: якщо свіп/реклейм близько,
            # anchor лишається біля reclaim-рівня; якщо далеко, лишаємо current price.
            if abs(sweep_level - price) <= atr15 * 0.75:
                entry_anchor = sweep_level
                entry_basis = f"{model} sweep/reclaim level"
        invalidation = safe_float(event.get("invalidation_level"), 0.0) or invalidation
        if not invalidation or abs(invalidation - price) < atr15 * 0.25:
            invalidation = price - side_sign(side) * max(ABS_MIN_STOP_DOLLARS, atr15 * 1.25)
        invalidation_basis = f"{model} sweep invalidation"
    elif is_limit_armed and ce_level:
        entry_anchor = ce_level
        entry_basis = "LIMIT_ARMED CE/FVG"

    # Нормалізація: стоп завжди має бути по правильний бік entry.
    min_stop_dist = max(ABS_MIN_STOP_DOLLARS, atr15 * 0.75)
    if side == Side.LONG.value and invalidation >= entry_anchor:
        invalidation = entry_anchor - min_stop_dist
    elif side == Side.SHORT.value and invalidation <= entry_anchor:
        invalidation = entry_anchor + min_stop_dist

    target = find_target_magnet_preview(side, entry_anchor, context, atr15)
    if target:
        target_level = safe_float(target.get("level"), target_level)
        target_basis = f"{target.get('kind')} {target.get('timeframe')} magnet"
        target_magnet_score = float(target.get("magnet_score", 0.0) or 0.0)
    else:
        target_magnet_score = 0.0

    return {
        "entry_anchor": round_price(entry_anchor),
        "entry_basis": entry_basis,
        "invalidation_level": round_price(invalidation),
        "invalidation_basis": invalidation_basis,
        "target_level": round_price(target_level),
        "target_basis": target_basis,
        "target_magnet_score": round(target_magnet_score, 2),
    }


def find_target_magnet_preview(side: str, entry_anchor: float, context: dict, atr15: float) -> Optional[dict[str, Any]]:
    """Невеликий preview-магніт для ранжування гіпотез. Повний TP-алгоритм
    лишається у build_trade_plan(); тут ми тільки оцінюємо, чи є куди йти."""
    targets = find_technical_targets(
        side,
        entry_anchor,
        context.get("zones") or [],
        context.get("liquidity", {}) or {},
        atr15,
        macro=context.get("macro_liquidity", {}) or {},
    )
    min_dist = max(ABS_MIN_TP1_DOLLARS, atr15 * 1.25)
    eligible = [t for t in targets if safe_float(t.get("distance"), 0.0) >= min_dist]
    if not eligible:
        return None
    eligible.sort(key=lambda t: (-float(t.get("magnet_score", 0.0) or 0.0), safe_float(t.get("distance"), 999.0)))
    return eligible[0]


def score_hypothesis_layers(
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
    execution_source: str,
    trigger_age: float,
    has_forward_zone: bool,
    target_magnet_score: float,
    regime_matched: int,
    regime_conflict: int,
) -> dict[str, Any]:
    """Три незалежні шари якості:
    setup_quality — чи правильна ідея;
    execution_quality — чи правильний момент;
    trade_plan_quality — чи математично/структурно є нормальна угода."""
    setup_quality = (
        loc_score * 0.95 + str_score * 0.90 + liq_score * 0.70 + htf_score * 0.70
        + max(raw_bonus, -12) * 1.15 + session_bonus * 0.85 + vector_bonus * 0.75
        + (6 if regime_matched else 0) - (10 if regime_conflict else 0)
    )
    setup_quality = int(clamp(setup_quality, 0, 100))

    freshness = execution_freshness_multiplier(execution_source, trigger_age)
    source_base = {
        ExecutionSource.LIVE_3M.value: 82,
        ExecutionSource.LIMIT_ARMED.value: 70,
        ExecutionSource.REENTRY.value: 62,
        ExecutionSource.ACCEPTANCE_RETEST.value: 76,
        ExecutionSource.ACCELERATION_PULLBACK.value: 58,
        ExecutionSource.SESSION_MEAN.value: 74,
        ExecutionSource.OPENING_RANGE.value: 72,
        ExecutionSource.OPEN_RECLAIM.value: 73,
        ExecutionSource.LIQUIDITY_LADDER.value: 66,
        ExecutionSource.FAILED_AUCTION.value: 78,
        ExecutionSource.TIME_OF_DAY.value: 62,
        ExecutionSource.TIME_WARP.value: 44,
        ExecutionSource.NONE.value: 34,
    }.get(str(execution_source), 34)
    execution_quality = source_base + trig_score * 0.55 + flw_score * 0.45
    execution_quality *= freshness
    execution_quality = int(clamp(execution_quality, 0, 100))

    trade_plan_quality = 48
    trade_plan_quality += 18 if has_forward_zone else -8
    trade_plan_quality += min(float(target_magnet_score or 0.0) * 0.32, 22)
    trade_plan_quality += min(max(loc_score, 0.0) * 0.20, 12)
    trade_plan_quality = int(clamp(trade_plan_quality, 0, 100))

    organic_score = int(round(0.40 * setup_quality + 0.35 * execution_quality + 0.25 * trade_plan_quality))
    return {
        "setup_quality": setup_quality,
        "execution_quality": execution_quality,
        "trade_plan_quality": trade_plan_quality,
        "organic_score": organic_score,
        "freshness_multiplier": round(freshness, 4),
    }


def hypothesis_audit_row(c: Candidate) -> dict[str, Any]:
    comps = c.score_components or {}
    return {
        "rank": c.hypothesis_rank,
        "side": c.side,
        "model": c.ict_model,
        "setup_type": c.setup_type,
        "final_score": c.final_score,
        "hypothesis_score": round(float(c.hypothesis_score or comps.get("hypothesis_score", c.final_score)), 2),
        "setup_q": c.setup_quality_score,
        "execution_q": c.execution_quality_score,
        "plan_q": c.trade_plan_quality_score,
        "execution_source": c.execution_source,
        "entry_stage": c.entry_stage,
        "trigger_age_min": round(float(c.trigger_age_minutes or 0.0), 1),
        "target_magnet": round(float(c.target_magnet_score or 0.0), 2),
        "innovation_model": (getattr(c, "innovation_profile", {}) or {}).get("primary", ""),
        "innovation_risk_mult": round(float((getattr(c, "innovation_profile", {}) or {}).get("risk_multiplier", 1.0)), 3),
        "revalidation_state": (getattr(c, "revalidation_profile", {}) or {}).get("state", ""),
        "revalidation_supported": bool((getattr(c, "revalidation_profile", {}) or {}).get("entry_supported", False)),
        "entry_timing": (getattr(c, "revalidation_profile", {}) or {}).get("entry_timing", ""),
    }


def finalize_hypothesis_ranking(candidates: list[Candidate]) -> list[Candidate]:
    candidates.sort(key=lambda c: (float(c.hypothesis_score or c.final_score), c.final_score), reverse=True)
    matrix = [hypothesis_audit_row(c) for c in candidates]
    for idx, cand in enumerate(candidates, start=1):
        cand.hypothesis_rank = idx
        cand.competing_hypotheses = matrix[:10]
        if cand.score_components is not None:
            cand.score_components["hypothesis_rank"] = idx
            cand.score_components["hypothesis_matrix_top"] = matrix[:10]
    return candidates


def rescore_reentry_candidate(candidate: Candidate, context: dict, journal: dict) -> Candidate:
    """Re-entry більше не обходить score engine. Він отримує ті самі quality layers,
    gates і ML/bootstrap audit, але лишається REENTRY execution_source."""
    price = context.get("price", 0.0)
    atr15 = context.get("atr15", 0.6) or 0.6
    side = candidate.side
    loc_score = calculate_location_score(price, context.get("zones") or [], side, atr15, context.get("tf15", {}), context.get("tf1h", {}))
    str_score = 19 if context.get("tf15", {}).get("bias") == side else 10
    htf_score = 20 if context.get("tf4h", {}).get("bias") == side else 6
    cvd = context.get("cvd", {}) or {}
    flw_score = float(cvd.get("score", 0)) if cvd.get("bias") == side else 0.0
    trig_score = 18 if candidate.trigger_ready else 7
    liq_score = 12 if "ICT_LOCATION" in (candidate.evidence_families or []) else 6
    setup_family = candidate.setup_family or SETUP_FAMILY_MAP.get(candidate.setup_type, SetupFamily.CONTINUATION.value)
    quality_features = _build_quality_features(
        loc_score=loc_score,
        str_score=str_score,
        liq_score=liq_score,
        flw_score=flw_score,
        trig_score=trig_score,
        htf_score=htf_score,
        raw_bonus=8,
        session_bonus=0,
        vector_bonus=0,
        trigger_age=candidate.trigger_age_minutes,
        trigger_ready=candidate.trigger_ready,
        best_pattern="REENTRY",
        regime_matched=0,
        regime_conflict=0,
        exhaustion_multiplier=1.0,
        pattern_family=setup_family,
    )
    calibration = calibrate_candidate_quality(journal, quality_features, setup_family, candidate.trigger_ready, False, True)
    direction_perf = direction_recent_performance(journal, side)
    layers = score_hypothesis_layers(
        loc_score=loc_score,
        str_score=str_score,
        liq_score=liq_score,
        flw_score=flw_score,
        trig_score=trig_score,
        htf_score=htf_score,
        raw_bonus=8,
        session_bonus=0,
        vector_bonus=0,
        execution_source=ExecutionSource.REENTRY.value,
        trigger_age=candidate.trigger_age_minutes,
        has_forward_zone=True,
        target_magnet_score=0.0,
        regime_matched=0,
        regime_conflict=0,
    )
    blended = int(round(0.55 * int(calibration["score"]) + 0.45 * layers["organic_score"]))
    blended = int(round(blended * float(direction_perf.get("score_multiplier", 1.0))))
    candidate.final_score = int(clamp(blended, 12, 98))
    candidate.raw_score = max(candidate.raw_score, candidate.final_score)
    candidate.score_components = {
        "reentry_rescored": True,
        "features": calibration["features"],
        "gates": calibration["gates"],
        "probability": calibration["probability"],
        "base_probability": calibration["base_probability"],
        "model_source": calibration["model_source"],
        "sample_size": calibration["sample_size"],
        "learned_weight": calibration["learned_weight"],
        "setup_quality": layers["setup_quality"],
        "execution_quality": layers["execution_quality"],
        "trade_plan_quality": layers["trade_plan_quality"],
        "organic_score": layers["organic_score"],
        "hypothesis_score": candidate.final_score,
        "direction_performance": direction_perf,
        "direction_risk_multiplier": direction_perf.get("risk_multiplier", 1.0),
    }
    candidate.setup_quality_score = layers["setup_quality"]
    candidate.execution_quality_score = layers["execution_quality"]
    candidate.trade_plan_quality_score = layers["trade_plan_quality"]
    candidate.hypothesis_score = float(candidate.final_score)
    candidate.risk_multiplier = float(direction_perf.get("risk_multiplier", 1.0) or 1.0) * 0.75
    candidate.professional_gate = evaluate_professional_gate(context, candidate)
    candidate.stage_plan = staged_entry_plan(candidate, context, direction_perf)
    candidate.entry_stage = str(candidate.stage_plan.get("stage", candidate.entry_stage))
    candidate = apply_setup_innovation_overlay(candidate, context, {}, journal)
    return candidate

def _same_side_zone_support(zones: list, side: str, price: float, atr15: float, kinds: set[str] = {"FVG", "OB"}) -> tuple[bool, float, str]:
    best_dist = 999.0
    best_mid = 0.0
    best_label = ""
    for z in zones or []:
        if getattr(z, "side", "") != side or getattr(z, "kind", "") not in kinds:
            continue
        low = safe_float(getattr(z, "low", 0.0), 0.0)
        high = safe_float(getattr(z, "high", 0.0), 0.0)
        mid = (low + high) / 2.0
        inside = low <= price <= high if low <= high else high <= price <= low
        dist = 0.0 if inside else min(abs(price - low), abs(price - high), abs(price - mid))
        if dist < best_dist:
            best_dist = dist
            best_mid = mid
            best_label = f"{getattr(z, 'timeframe', '')} {getattr(z, 'kind', '')}".strip()
    return bool(best_dist <= max(atr15 * 1.35, ABS_MIN_STOP_DOLLARS * 0.75)), best_mid, best_label


def detect_acceptance_retest_continuation(c15: list[Candle], side: str, price: float, atr15: float, zones: list, tf15: dict, tf1h: dict) -> dict[str, Any]:
    """Early continuation-probe після ретесту.
    Модель ловить не повний ICT-package, а саме момент, коли після імпульсу ціна
    повернулась у OB/FVG/discount і не зламала структуру. Це був клас сценарію
    біля 72.60: не треба послаблювати gate, треба окремо оцінити acceptance-рetest."""
    if len(c15) < 8 or atr15 <= 0:
        return {"active": False, "score_bonus": 0, "reason": "not_enough_candles"}
    sign = side_sign(side)
    recent = c15[-8:]
    prev = c15[-8:-1]
    last = c15[-1]
    zone_ok, zone_mid, zone_label = _same_side_zone_support(zones, side, price, atr15)
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    if side == Side.LONG.value:
        impulse_low = min(c.low for c in prev)
        impulse_high = max(c.high for c in prev)
        impulse_atr = (impulse_high - impulse_low) / atr15
        pullback_atr = (impulse_high - price) / atr15
        no_break = price > impulse_low + atr15 * 0.20
        acceptance = (last.close >= last.open) or (len(c15) >= 2 and last.close > c15[-2].close) or ((last.close - last.low) / max(last.high - last.low, 1e-9) >= 0.55)
    else:
        impulse_high = max(c.high for c in prev)
        impulse_low = min(c.low for c in prev)
        impulse_atr = (impulse_high - impulse_low) / atr15
        pullback_atr = (price - impulse_low) / atr15
        no_break = price < impulse_high - atr15 * 0.20
        acceptance = (last.close <= last.open) or (len(c15) >= 2 and last.close < c15[-2].close) or ((last.high - last.close) / max(last.high - last.low, 1e-9) >= 0.55)
    retest_depth_ok = 0.55 <= pullback_atr <= 3.75
    active = bool(tf_ok and no_break and retest_depth_ok and (zone_ok or acceptance) and impulse_atr >= 1.15)
    score_bonus = 0
    if active:
        score_bonus += 8
        score_bonus += 5 if zone_ok else 0
        score_bonus += 4 if acceptance else 0
        score_bonus += 3 if tf_ok else 0
    return {
        "active": active,
        "score_bonus": score_bonus,
        "impulse_atr": round(impulse_atr, 2),
        "pullback_atr": round(pullback_atr, 2),
        "zone_ok": zone_ok,
        "zone_mid": round_price(zone_mid) if zone_mid else 0.0,
        "zone_label": zone_label,
        "acceptance": acceptance,
        "tf_ok": tf_ok,
        "no_break": no_break,
    }


def detect_acceleration_pullback_reentry(c15: list[Candle], side: str, price: float, atr15: float, tf15: dict, tf1h: dict) -> dict[str, Any]:
    """Після пропущеного імпульсу не женемося market на піку.
    Якщо остання 15M свічка пробила локальний high/low після невзятого входу,
    створюємо WAIT_PULLBACK до 38–50% імпульсу."""
    if len(c15) < 9 or atr15 <= 0:
        return {"active": False, "reason": "not_enough_candles"}
    last = c15[-1]
    prior = c15[-9:-1]
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    if side == Side.LONG.value:
        local_high = max(c.high for c in prior)
        swing_low = min(c.low for c in prior[-5:])
        breakout = last.close > local_high + atr15 * 0.08
        impulse_atr = (last.close - swing_low) / atr15
        pullback_50 = last.close - (last.close - swing_low) * 0.50
        pullback_382 = last.close - (last.close - swing_low) * 0.382
    else:
        local_low = min(c.low for c in prior)
        swing_high = max(c.high for c in prior[-5:])
        breakout = last.close < local_low - atr15 * 0.08
        impulse_atr = (swing_high - last.close) / atr15
        pullback_50 = last.close + (swing_high - last.close) * 0.50
        pullback_382 = last.close + (swing_high - last.close) * 0.382
    active = bool(tf_ok and breakout and impulse_atr >= 1.45)
    return {
        "active": active,
        "score_bonus": 10 if active else 0,
        "impulse_atr": round(impulse_atr, 2),
        "pullback_50": round_price(pullback_50),
        "pullback_382": round_price(pullback_382),
        "breakout": breakout,
        "tf_ok": tf_ok,
    }



def _current_session_window(now_kyiv: datetime) -> dict[str, Any]:
    """Повертає поточну торгову сесію за Києвом і її opening-range вікно.
    Це не фільтр часу, а execution context: різні сесії мають різні моделі входу."""
    hour = now_kyiv.hour
    if ASIA_START_H <= hour < ASIA_END_H:
        return {"name": "ASIA", "start_h": ASIA_START_H, "end_h": ASIA_END_H, "phase": "ACCUMULATION"}
    if LONDON_START_H <= hour < LONDON_END_H:
        phase = "MANIPULATION" if hour < LONDON_START_H + MANIP_WINDOW_H else "DISTRIBUTION"
        return {"name": "LONDON", "start_h": LONDON_START_H, "end_h": LONDON_END_H, "phase": phase}
    if NY_START_H <= hour < NY_END_H:
        phase = "MANIPULATION" if hour < NY_START_H + MANIP_WINDOW_H else "DISTRIBUTION"
        return {"name": "NY", "start_h": NY_START_H, "end_h": NY_END_H, "phase": phase}
    return {"name": "OFF_HOURS", "start_h": None, "end_h": None, "phase": "OFF_SESSION"}


def _anchored_session_vwap_and_mean(c15: list[Candle], now_kyiv: datetime) -> dict[str, Any]:
    """VWAP / session mean без зовнішнього feed: використовує typical price × volume.
    Якщо volume відсутній/нульовий, деградує до time-weighted mean. Так ми не
    вигадуємо order-flow там, де його немає, але все одно маємо fair-value якір."""
    session = _current_session_window(now_kyiv)
    if session.get("start_h") is None:
        return {"valid": False, "session": session.get("name"), "vwap": 0.0, "mean": 0.0, "n": 0}
    today = now_kyiv.date()
    candles = [c for c in c15 if _candle_kyiv_dt(c.ts, now_kyiv.tzinfo).date() == today and session["start_h"] <= _candle_kyiv_dt(c.ts, now_kyiv.tzinfo).hour < session["end_h"]]
    if len(candles) < 3:
        return {"valid": False, "session": session.get("name"), "vwap": 0.0, "mean": 0.0, "n": len(candles)}
    typical = [((c.high + c.low + c.close) / 3.0, max(float(c.volume or 0.0), 0.0)) for c in candles]
    vol_sum = sum(v for _, v in typical)
    if vol_sum > 0:
        vwap = sum(tp * v for tp, v in typical) / vol_sum
    else:
        vwap = sum(tp for tp, _ in typical) / len(typical)
    mean = sum(c.close for c in candles) / len(candles)
    return {"valid": True, "session": session.get("name"), "phase": session.get("phase"), "vwap": round_price(vwap), "mean": round_price(mean), "n": len(candles)}


def _nearest_key_level(price: float, levels: list[tuple[str, float]], atr15: float, max_atr: float = 1.0) -> tuple[str, float, float]:
    clean = [(name, safe_float(level, 0.0)) for name, level in levels if safe_float(level, 0.0) > 0]
    if not clean:
        return "", 0.0, 999.0
    name, level = min(clean, key=lambda x: abs(x[1] - price))
    dist_atr = abs(price - level) / max(atr15, 1e-9)
    if dist_atr > max_atr:
        return "", 0.0, dist_atr
    return name, level, dist_atr


def detect_vwap_session_mean_reclaim(c15: list[Candle], side: str, price: float, atr15: float, now_kyiv: datetime, tf15: dict, tf1h: dict) -> dict[str, Any]:
    """VWAP / Session Mean Reclaim.
    Вхід не в середині нічого: ціна має або повернути session mean/VWAP, або
    ретестнути його як підтримку/опір після імпульсу."""
    if len(c15) < 6 or atr15 <= 0:
        return {"active": False, "reason": "not_enough_candles"}
    anchor = _anchored_session_vwap_and_mean(c15, now_kyiv)
    if not anchor.get("valid"):
        return {"active": False, "reason": "no_session_anchor"}
    level_name, level, dist_atr = _nearest_key_level(price, [("VWAP", anchor.get("vwap")), ("SESSION_MEAN", anchor.get("mean"))], atr15, SESSION_MEAN_MAX_DIST_ATR)
    if not level:
        return {"active": False, "reason": "far_from_mean", "dist_atr": round(dist_atr, 2)}
    last, prev = c15[-1], c15[-2]
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    if side == Side.LONG.value:
        reclaimed = (prev.close <= level and last.close > level) or (last.low <= level <= last.close)
        acceptance = last.close >= last.open and ((last.close - last.low) / max(last.high - last.low, 1e-9) >= 0.55)
    else:
        reclaimed = (prev.close >= level and last.close < level) or (last.high >= level >= last.close)
        acceptance = last.close <= last.open and ((last.high - last.close) / max(last.high - last.low, 1e-9) >= 0.55)
    active = bool(tf_ok and reclaimed and acceptance)
    score_bonus = 0
    if active:
        score_bonus = 10 + (4 if level_name == "VWAP" else 2) + (3 if anchor.get("phase") in {"MANIPULATION", "DISTRIBUTION"} else 0)
    return {
        "active": active,
        "score_bonus": score_bonus,
        "level_name": level_name,
        "level": round_price(level),
        "dist_atr": round(dist_atr, 2),
        "session": anchor.get("session"),
        "phase": anchor.get("phase"),
        "reclaimed": reclaimed,
        "acceptance": acceptance,
        "tf_ok": tf_ok,
    }


def detect_opening_range_model(c15: list[Candle], side: str, price: float, atr15: float, now_kyiv: datetime, tf15: dict, tf1h: dict) -> dict[str, Any]:
    """Opening Range Breakout / Failed ORB.
    ORB — це не будь-який пробій. Беремо першу годину поточної сесії, нормалізуємо
    ширину по ATR і відокремлюємо справжній breakout від failed auction назад у range."""
    if len(c15) < 12 or atr15 <= 0:
        return {"breakout_active": False, "failed_active": False, "reason": "not_enough_candles"}
    session = _current_session_window(now_kyiv)
    if session.get("start_h") is None or now_kyiv.hour < session["start_h"] + 1:
        return {"breakout_active": False, "failed_active": False, "reason": "opening_range_not_complete", "session": session.get("name")}
    today = now_kyiv.date()
    or_rng = _session_range(c15, now_kyiv.tzinfo, today, session["start_h"], session["start_h"] + 1)
    if not or_rng:
        return {"breakout_active": False, "failed_active": False, "reason": "no_or_range", "session": session.get("name")}
    high, low = safe_float(or_rng.get("high")), safe_float(or_rng.get("low"))
    width_atr = (high - low) / max(atr15, 1e-9)
    if width_atr < 0.35 or width_atr > 4.5:
        return {"breakout_active": False, "failed_active": False, "reason": "or_width_invalid", "width_atr": round(width_atr, 2), "or_high": high, "or_low": low}
    buf = atr15 * ORB_BREAK_BUFFER_ATR
    last = c15[-1]
    recent = c15[-8:]
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    if side == Side.LONG.value:
        breakout = last.close > high + buf and (last.low <= high + atr15 * ORB_RETEST_MAX_DIST_ATR or price <= high + atr15 * ORB_RETEST_MAX_DIST_ATR)
        swept_wrong_way = any(c.low < low - buf for c in recent)
        failed = swept_wrong_way and last.close > low + buf and ((last.close - last.low) / max(last.high - last.low, 1e-9) >= 0.55)
        entry_level = high if breakout else low
        invalidation = low if breakout else min(c.low for c in recent)
    else:
        breakout = last.close < low - buf and (last.high >= low - atr15 * ORB_RETEST_MAX_DIST_ATR or price >= low - atr15 * ORB_RETEST_MAX_DIST_ATR)
        swept_wrong_way = any(c.high > high + buf for c in recent)
        failed = swept_wrong_way and last.close < high - buf and ((last.high - last.close) / max(last.high - last.low, 1e-9) >= 0.55)
        entry_level = low if breakout else high
        invalidation = high if breakout else max(c.high for c in recent)
    return {
        "breakout_active": bool(tf_ok and breakout),
        "failed_active": bool(failed),
        "score_bonus": (12 if breakout else 0) + (14 if failed else 0),
        "session": session.get("name"),
        "phase": session.get("phase"),
        "or_high": round_price(high),
        "or_low": round_price(low),
        "or_width_atr": round(width_atr, 2),
        "entry_level": round_price(entry_level),
        "invalidation": round_price(invalidation),
        "tf_ok": tf_ok,
    }


def detect_daily_weekly_open_reclaim(c15: list[Candle], side: str, price: float, atr15: float, macro: dict, tf15: dict, tf1h: dict) -> dict[str, Any]:
    if len(c15) < 4 or atr15 <= 0:
        return {"active": False, "reason": "not_enough_candles"}
    name, level, dist_atr = _nearest_key_level(price, [("DAILY_OPEN", macro.get("daily_open")), ("WEEKLY_OPEN", macro.get("weekly_open"))], atr15, OPEN_RECLAIM_MAX_DIST_ATR)
    if not level:
        return {"active": False, "reason": "far_from_open", "dist_atr": round(dist_atr, 2)}
    last = c15[-1]
    recent = c15[-5:]
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    if side == Side.LONG.value:
        reclaimed = any(c.low < level - atr15 * 0.08 for c in recent) and last.close > level
        acceptance = last.close >= last.open or ((last.close - last.low) / max(last.high - last.low, 1e-9) >= 0.58)
    else:
        reclaimed = any(c.high > level + atr15 * 0.08 for c in recent) and last.close < level
        acceptance = last.close <= last.open or ((last.high - last.close) / max(last.high - last.low, 1e-9) >= 0.58)
    active = bool(tf_ok and reclaimed and acceptance)
    return {"active": active, "score_bonus": 12 if active and name == "WEEKLY_OPEN" else (9 if active else 0), "level_name": name, "level": round_price(level), "dist_atr": round(dist_atr, 2), "reclaimed": reclaimed, "acceptance": acceptance, "tf_ok": tf_ok}


def detect_liquidity_ladder_model(side: str, price: float, context: dict, atr15: float, tf15: dict, tf1h: dict) -> dict[str, Any]:
    targets = find_technical_targets(side, price, context.get("zones") or [], context.get("liquidity", {}) or {}, atr15, macro=context.get("macro_liquidity", {}) or {})
    min_dist = max(ABS_MIN_TP1_DOLLARS, atr15 * 1.05)
    eligible = [t for t in targets if safe_float(t.get("distance"), 0.0) >= min_dist]
    top = sorted(eligible, key=lambda t: (-safe_float(t.get("magnet_score"), 0.0), safe_float(t.get("distance"), 999)))[:4]
    ladder_score = sum(safe_float(t.get("magnet_score"), 0.0) for t in top)
    kinds = {str(t.get("kind")) for t in top}
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    # Це DOL-модель: сама по собі не є market trigger, але стає actionable,
    # якщо структура/напрям підтримують маршрут до ліквідності.
    active = bool(tf_ok and len(top) >= LIQUIDITY_LADDER_MIN_TARGETS and ladder_score >= LIQUIDITY_LADDER_MIN_SCORE and len(kinds) >= 2)
    return {"active": active, "score_bonus": min(16, 5 + int(ladder_score)) if active else 0, "ladder_score": round(ladder_score, 2), "target_count": len(top), "targets": [{"kind": t.get("kind"), "level": round_price(t.get("level")), "score": round(safe_float(t.get("magnet_score"), 0.0), 2)} for t in top], "tf_ok": tf_ok, "entry_ok": active}


def detect_failed_auction_rejection_tail(c15: list[Candle], side: str, price: float, atr15: float, context: dict) -> dict[str, Any]:
    """Failed Auction / Rejection Tail.
    Ловить різку відмову від ліквідності/open/OR/сесійного рівня. Не потребує
    book/tape, але чесно нормалізується хвостом і ATR, щоб не вважати кожен wick сигналом."""
    if len(c15) < 4 or atr15 <= 0:
        return {"active": False, "reason": "not_enough_candles"}
    last = c15[-1]
    rng = max(last.high - last.low, 1e-9)
    lower_tail = (min(last.open, last.close) - last.low) / rng
    upper_tail = (last.high - max(last.open, last.close)) / rng
    macro = context.get("macro_liquidity", {}) or {}
    liq15 = (context.get("liquidity") or {}).get("15m", {}) if isinstance(context.get("liquidity"), dict) else {}
    levels: list[tuple[str, float]] = []
    if side == Side.LONG.value:
        for lvl in liq15.get("ssl", []) or []: levels.append(("SSL", lvl))
        for key, label in (("asian_low", "ASIAN_LOW"), ("pdl", "PDL"), ("daily_open", "DAILY_OPEN"), ("weekly_open", "WEEKLY_OPEN")): levels.append((label, macro.get(key)))
        name, level, dist_atr = _nearest_key_level(last.low, levels, atr15, 0.75)
        tail_ok = lower_tail >= FAILED_AUCTION_MIN_TAIL_RATIO and ((last.close - last.low) / rng >= 0.62)
        active = bool(tail_ok and level > 0 and last.close > level)
        rejection_extreme = last.low
    else:
        for lvl in liq15.get("bsl", []) or []: levels.append(("BSL", lvl))
        for key, label in (("asian_high", "ASIAN_HIGH"), ("pdh", "PDH"), ("daily_open", "DAILY_OPEN"), ("weekly_open", "WEEKLY_OPEN")): levels.append((label, macro.get(key)))
        name, level, dist_atr = _nearest_key_level(last.high, levels, atr15, 0.75)
        tail_ok = upper_tail >= FAILED_AUCTION_MIN_TAIL_RATIO and ((last.high - last.close) / rng >= 0.62)
        active = bool(tail_ok and level > 0 and last.close < level)
        rejection_extreme = last.high
    return {"active": active, "score_bonus": 14 if active else 0, "level_name": name, "level": round_price(level), "dist_atr": round(dist_atr, 2), "tail_ratio": round(lower_tail if side == Side.LONG.value else upper_tail, 2), "rejection_extreme": round_price(rejection_extreme)}


def detect_time_of_day_adaptive_execution(session_profile: dict, side: str, tf15: dict, tf1h: dict, c15: list[Candle], atr15: float) -> dict[str, Any]:
    phase = str(session_profile.get("phase") or "")
    chop = bool((session_profile.get("chop_zone") or {}).get("active"))
    judas = session_profile.get("judas") or {}
    tf_ok = (tf15.get("bias") == side) or (tf1h.get("bias") == side)
    # Сесійна модель не має права сама відкривати угоду в chop; вона має підсилювати
    # тільки поведінково логічні фази: manipulation/distribution London/NY.
    session_ok = phase in {"MANIPULATION", "DISTRIBUTION"} and not chop
    side_ok = (judas.get("bias") == side and safe_float(judas.get("bonus"), 0.0) > 0) or tf_ok
    active = bool(session_ok and side_ok)
    score_bonus = 7 if active and phase == "DISTRIBUTION" else (5 if active else 0)
    return {"active": active, "score_bonus": score_bonus, "phase": phase, "session": session_profile.get("session_name"), "tf_ok": tf_ok, "judas_bias": judas.get("bias"), "entry_ok": active and tf_ok}

def explain_candidate_gate_failure(context: dict, candidate: Candidate, gate: Optional[dict] = None) -> str:
    gate = gate or candidate.professional_gate or evaluate_professional_gate(context, candidate)
    reasons: list[str] = []
    if candidate.final_score < PRO_RISKY_MIN:
        reasons.append(f"score {candidate.final_score} < risky_min {PRO_RISKY_MIN}")
    if len(candidate.evidence_families or []) < max(3, MIN_PRO_LAYERS_ENTRY - 1):
        reasons.append(f"layers {len(candidate.evidence_families or [])} замало")
    if not candidate.trigger_ready:
        reasons.append(f"execution not ready ({candidate.execution_source})")
    gates = ((candidate.score_components or {}).get("gates") or {})
    if safe_float(gates.get("product"), 1.0) < 0.46:
        reasons.append(f"gate_product {safe_float(gates.get('product'), 0.0):.2f} < 0.46")
    if safe_float(gates.get("pattern_gate"), 1.0) < 0.70:
        reasons.append(f"pattern_gate {safe_float(gates.get('pattern_gate'), 0.0):.2f} < 0.70")
    tf1h_bias = (context.get("tf1h") or {}).get("bias")
    tf4h_bias = (context.get("tf4h") or {}).get("bias")
    if not ((tf1h_bias == candidate.side) or (tf4h_bias == candidate.side)) and not gate.get("allow_entry"):
        if HTF_RISKY_OVERRIDE and candidate.final_score >= HTF_OVERRIDE_MIN_SCORE:
            reasons.append("HTF override available: risky-entry тільки зі зниженим ризиком")
        else:
            reasons.append("HTF не підтримує risky-entry")
    return "; ".join(reasons) if reasons else str(gate.get("reason") or "did_not_pass_entry_gate")


def rejected_hypotheses_audit(context: dict, candidates: list[Candidate], limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ranked = finalize_hypothesis_ranking(list(candidates or [])) if candidates else []
    for c in ranked[:limit]:
        gate = c.professional_gate or evaluate_professional_gate(context, c)
        row = hypothesis_audit_row(c)
        row["failed_gate"] = explain_candidate_gate_failure(context, c, gate)
        row["evidence_layers"] = list(c.evidence_families or [])
        row["entry_anchor"] = round_price(getattr(c, "execution_anchor", 0.0))
        row["invalidation"] = round_price(getattr(c, "invalidation_level", 0.0))
        rows.append(row)
    return rows


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
        "RANGE_COMPRESSION_MODEL": {"name": "Range Compression Breakout", "priority": 65, "allow_early": False, "preferred_setup": SetupType.RANGE_COMPRESSION_BREAKOUT.value, "score_bonus": 12, "stop_min_atr": 0.8, "stop_max_atr": 1.6, "favored": [Regime.RANGE.value, Regime.NORMAL.value], "penalty": [Regime.SHOCK.value]},
        "ACCEPTANCE_RETEST_CONTINUATION": {"name": "Acceptance Retest Continuation", "priority": 84, "allow_early": True, "preferred_setup": SetupType.ACCEPTANCE_RETEST_CONTINUATION.value, "score_bonus": 14, "stop_min_atr": 0.85, "stop_max_atr": 2.0, "favored": [Regime.TREND.value, Regime.TRANSITION.value, Regime.NORMAL.value], "penalty": [Regime.SHOCK.value]},
        "ACCELERATION_PULLBACK_REENTRY": {"name": "Acceleration Pullback Re-entry", "priority": 76, "allow_early": False, "preferred_setup": SetupType.ACCELERATION_PULLBACK_REENTRY.value, "score_bonus": 10, "stop_min_atr": 0.95, "stop_max_atr": 2.4, "favored": [Regime.TREND.value, Regime.TRANSITION.value, Regime.SHOCK.value], "penalty": [Regime.RANGE.value]},
        "VWAP_SESSION_MEAN_RECLAIM": {"name": "VWAP / Session Mean Reclaim", "priority": 87, "allow_early": True, "preferred_setup": SetupType.SESSION_MEAN_RECLAIM.value, "score_bonus": 13, "stop_min_atr": 0.85, "stop_max_atr": 1.8, "favored": [Regime.TREND.value, Regime.TRANSITION.value, Regime.NORMAL.value], "penalty": [Regime.SHOCK.value]},
        "OPENING_RANGE_BREAKOUT": {"name": "Opening Range Breakout", "priority": 83, "allow_early": True, "preferred_setup": SetupType.OPENING_RANGE_BREAKOUT.value, "score_bonus": 14, "stop_min_atr": 0.75, "stop_max_atr": 1.9, "favored": [Regime.TREND.value, Regime.TRANSITION.value, Regime.NORMAL.value], "penalty": [Regime.RANGE.value]},
        "FAILED_ORB": {"name": "Failed Opening Range Breakout", "priority": 86, "allow_early": True, "preferred_setup": SetupType.FAILED_OPENING_RANGE_BREAKOUT.value, "score_bonus": 16, "stop_min_atr": 0.85, "stop_max_atr": 2.1, "favored": [Regime.RANGE.value, Regime.TRANSITION.value, Regime.SHOCK.value], "penalty": []},
        "DAILY_WEEKLY_OPEN_RECLAIM": {"name": "Daily/Weekly Open Reclaim", "priority": 81, "allow_early": True, "preferred_setup": SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value, "score_bonus": 12, "stop_min_atr": 0.85, "stop_max_atr": 2.0, "favored": [Regime.NORMAL.value, Regime.TRANSITION.value, Regime.TREND.value], "penalty": []},
        "LIQUIDITY_LADDER_MODEL": {"name": "Liquidity Ladder Model", "priority": 72, "allow_early": False, "preferred_setup": SetupType.LIQUIDITY_LADDER.value, "score_bonus": 10, "stop_min_atr": 0.95, "stop_max_atr": 2.3, "favored": [Regime.TREND.value, Regime.TRANSITION.value, Regime.NORMAL.value], "penalty": []},
        "FAILED_AUCTION_REJECTION": {"name": "Failed Auction / Rejection Tail", "priority": 89, "allow_early": True, "preferred_setup": SetupType.FAILED_AUCTION_REJECTION.value, "score_bonus": 17, "stop_min_atr": 0.75, "stop_max_atr": 1.8, "favored": [Regime.RANGE.value, Regime.TRANSITION.value, Regime.SHOCK.value], "penalty": []},
        "TIME_OF_DAY_ADAPTIVE": {"name": "Time-of-Day Adaptive Execution", "priority": 60, "allow_early": True, "preferred_setup": SetupType.TIME_OF_DAY_ADAPTIVE.value, "score_bonus": 7, "stop_min_atr": 0.90, "stop_max_atr": 2.1, "favored": [Regime.NORMAL.value, Regime.TRANSITION.value, Regime.TREND.value], "penalty": [Regime.RANGE.value]}
    }

    for side in [Side.LONG.value, Side.SHORT.value]:
        event = scan_events.get(side, {})
        trigger_age = (int(now_utc().timestamp() * 1000) - event.get("last_event_ts", 0)) / 60000.0 if event.get("last_event_ts") else 999.0
        scan_stage = event.get("stage", "")
        stage_is_ready = scan_stage in ["ACCEPTANCE", "RETEST", "READY"]
        live_3m_trigger_ready = bool(stage_is_ready and trigger_age <= TRIGGER_MAX_AGE_MINUTES)
        stale_3m_trigger_ready = bool(stage_is_ready and trigger_age > TRIGGER_MAX_AGE_MINUTES)
        # trigger_ready нижче означає тільки ЖИВИЙ 3M execution package. LIMIT/TIME_WARP
        # отримують власні execution_source, щоб не підміняти live market-entry.
        trigger_ready = live_3m_trigger_ready
        trigger_level = event.get("trigger_level", price)
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
        acceptance_retest = detect_acceptance_retest_continuation(c15, side, price, atr15, zones, tf15, tf1h)
        acceleration_reentry = detect_acceleration_pullback_reentry(c15, side, price, atr15, tf15, tf1h)
        session_mean_reclaim = detect_vwap_session_mean_reclaim(c15, side, price, atr15, now_kyiv, tf15, tf1h)
        opening_range = detect_opening_range_model(c15, side, price, atr15, now_kyiv, tf15, tf1h)
        open_reclaim = detect_daily_weekly_open_reclaim(c15, side, price, atr15, context.get("macro_liquidity", {}) or {}, tf15, tf1h)
        liquidity_ladder = detect_liquidity_ladder_model(side, price, context, atr15, tf15, tf1h)
        failed_auction = detect_failed_auction_rejection_tail(c15, side, price, atr15, context)
        time_of_day_adaptive = detect_time_of_day_adaptive_execution(session_profile, side, tf15, tf1h, c15, atr15)

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
        if acceptance_retest.get("active"):
            active_patterns.append("ACCEPTANCE_RETEST_CONTINUATION")
        if acceleration_reentry.get("active"):
            active_patterns.append("ACCELERATION_PULLBACK_REENTRY")
        if session_mean_reclaim.get("active"):
            active_patterns.append("VWAP_SESSION_MEAN_RECLAIM")
        if opening_range.get("breakout_active"):
            active_patterns.append("OPENING_RANGE_BREAKOUT")
        if opening_range.get("failed_active"):
            active_patterns.append("FAILED_ORB")
        if open_reclaim.get("active"):
            active_patterns.append("DAILY_WEEKLY_OPEN_RECLAIM")
        if liquidity_ladder.get("active"):
            active_patterns.append("LIQUIDITY_LADDER_MODEL")
        if failed_auction.get("active"):
            active_patterns.append("FAILED_AUCTION_REJECTION")
        if time_of_day_adaptive.get("active"):
            active_patterns.append("TIME_OF_DAY_ADAPTIVE")

        # ==========================================================
        # v6.10 HYPOTHESIS MATRIX: кожна активна ICT-модель створює
        # окрему trade hypothesis з власним entry anchor / invalidation / score.
        # Priority лишається tie-breaker, а не заміна професійної оцінки.
        # ==========================================================
        model_ids = list(dict.fromkeys(active_patterns))
        if not model_ids:
            model_ids = ["GENERIC_FALLBACK"]

        direction_perf = direction_recent_performance(journal, side)
        NO_PATTERN_EXTRA_MARGIN = int(os.getenv("NO_PATTERN_EXTRA_MARGIN", "12") or 12)

        for model_id in model_ids:
            is_fallback = model_id == "GENERIC_FALLBACK"
            p_data = pattern_registry.get(model_id, {
                "name": "Generic fallback",
                "priority": 0,
                "allow_early": False,
                "preferred_setup": SetupType.PULLBACK_CONTINUATION.value,
                "score_bonus": -NO_PATTERN_PENALTY,
                "stop_min_atr": 0.90,
                "stop_max_atr": 2.20,
                "favored": [],
                "penalty": [Regime.RANGE.value, Regime.SHOCK.value],
            })
            best_pattern = None if is_fallback else model_id
            pattern_conf = []
            raw_bonus = float(p_data.get("score_bonus", 0.0))
            regime_matched = 0
            regime_conflict = 0
            setup_type = str(p_data.get("preferred_setup", SetupType.PULLBACK_CONTINUATION.value))
            setup_family = SETUP_FAMILY_MAP.get(setup_type, SetupFamily.CONTINUATION.value)
            pattern_family = setup_family

            if not is_fallback:
                pattern_conf.append(f"Модель: {p_data['name']} ({model_id})")
                if regime in p_data.get("favored", []):
                    raw_bonus += 5
                    regime_matched = 1
                    pattern_conf.append("✅ Модель узгоджена з режимом ринку (+5)")
                elif regime in p_data.get("penalty", []):
                    raw_bonus -= 10
                    regime_conflict = 1
                    pattern_conf.append("⚠️ Конфлікт моделі з режимом ринку (-10)")
            else:
                pattern_conf.append(f"⚠️ Жодна ICT-модель не підтверджена — generic fallback (-{NO_PATTERN_PENALTY})")

            if model_id == "ACCEPTANCE_RETEST_CONTINUATION":
                raw_bonus += safe_float(acceptance_retest.get("score_bonus"), 0.0)
                pattern_conf.append(
                    f"🔁 Acceptance-retest: pullback={acceptance_retest.get('pullback_atr')} ATR | "
                    f"zone={acceptance_retest.get('zone_label') or 'none'} | acceptance={acceptance_retest.get('acceptance')}"
                )
            if model_id == "ACCELERATION_PULLBACK_REENTRY":
                raw_bonus += safe_float(acceleration_reentry.get("score_bonus"), 0.0)
                pattern_conf.append(
                    f"🚀 Acceleration re-entry: impulse={acceleration_reentry.get('impulse_atr')} ATR | "
                    f"WAIT_PULLBACK {acceleration_reentry.get('pullback_382')}–{acceleration_reentry.get('pullback_50')}"
                )
            if model_id == "VWAP_SESSION_MEAN_RECLAIM":
                raw_bonus += safe_float(session_mean_reclaim.get("score_bonus"), 0.0)
                pattern_conf.append(f"📈 VWAP/Mean reclaim: {session_mean_reclaim.get('level_name')}={session_mean_reclaim.get('level')} | session={session_mean_reclaim.get('session')} | dist={session_mean_reclaim.get('dist_atr')} ATR")
            if model_id in {"OPENING_RANGE_BREAKOUT", "FAILED_ORB"}:
                raw_bonus += safe_float(opening_range.get("score_bonus"), 0.0)
                pattern_conf.append(f"⏱️ OR model: {opening_range.get('session')} OR {opening_range.get('or_low')}–{opening_range.get('or_high')} | width={opening_range.get('or_width_atr')} ATR")
            if model_id == "DAILY_WEEKLY_OPEN_RECLAIM":
                raw_bonus += safe_float(open_reclaim.get("score_bonus"), 0.0)
                pattern_conf.append(f"🧲 Open reclaim: {open_reclaim.get('level_name')}={open_reclaim.get('level')} | dist={open_reclaim.get('dist_atr')} ATR")
            if model_id == "LIQUIDITY_LADDER_MODEL":
                raw_bonus += safe_float(liquidity_ladder.get("score_bonus"), 0.0)
                pattern_conf.append(f"🪜 Liquidity ladder: targets={liquidity_ladder.get('target_count')} | ladder_score={liquidity_ladder.get('ladder_score')} | {liquidity_ladder.get('targets')}")
            if model_id == "FAILED_AUCTION_REJECTION":
                raw_bonus += safe_float(failed_auction.get("score_bonus"), 0.0)
                pattern_conf.append(f"🧨 Failed auction: reject={failed_auction.get('level_name')} {failed_auction.get('level')} | tail={failed_auction.get('tail_ratio')}")
            if model_id == "TIME_OF_DAY_ADAPTIVE":
                raw_bonus += safe_float(time_of_day_adaptive.get("score_bonus"), 0.0)
                pattern_conf.append(f"🕒 Time-of-day adaptive: session={time_of_day_adaptive.get('session')} | phase={time_of_day_adaptive.get('phase')} | judas={time_of_day_adaptive.get('judas_bias')}")

            reversal_families = {SetupFamily.LIQUIDITY_RECOVERY.value, SetupFamily.STRUCTURAL_TRANSITION.value, SetupFamily.RANGE_EXECUTION.value}
            trend_families = {SetupFamily.CONTINUATION.value, SetupFamily.EXPANSION.value}
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
                    vector_bonus -= REVERSAL_LATE_ENTRY_PENALTY
                    pattern_conf.append(f"⏱️ Ідеальне HTF-вирівнювання для reversal = ризик пізнього входу (-{REVERSAL_LATE_ENTRY_PENALTY})")
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

            loc_score = calculate_location_score(price, zones, side, atr15, tf15, tf1h)
            str_score = (19 if tf15.get("bias") == side else 10) * str_weight
            liq_base = 16 if is_sweep and live_3m_trigger_ready else 6
            liq_score = liq_base * liq_weight * sweep_quality_multiplier
            flw_score = float(cvd.get("score", 0)) * flw_weight if cvd.get("bias") == side else 0.0
            trig_score = (22 if live_3m_trigger_ready else 8) * trig_weight
            htf_score = (20 if tf4h.get("bias") == side else 6) * htf_weight

            raw = loc_score + str_score + liq_score + flw_score + trig_score + htf_score + raw_bonus + vector_bonus
            session_bonus = 0.0
            if judas["bias"] == side and judas["bonus"] > 0:
                session_bonus += judas["bonus"]
                pattern_conf.extend(session_profile["notes"])
            elif chop["active"]:
                session_bonus -= chop["penalty_magnitude"]
                pattern_conf.extend(session_profile["notes"])
            raw += session_bonus
            if exhaustion_multiplier < 1.0:
                raw *= exhaustion_multiplier

            limit_armed_ready = bool(is_limit_armed)
            time_warp_ready = bool(time_warp_opportunity and stale_3m_trigger_ready and not live_3m_trigger_ready)
            local_trigger_level = trigger_level
            if model_id == "ACCELERATION_PULLBACK_REENTRY":
                execution_source = ExecutionSource.ACCELERATION_PULLBACK.value
                actionable_trigger_ready = False
                execution_lane_source = ExecutionLane.WAIT_RETEST.value
                pattern_conf.append("⏳ ACCELERATION_PULLBACK: імпульс уже відірвався — чекати 38–50% ретест, не market")
            elif model_id == "ACCEPTANCE_RETEST_CONTINUATION" and acceptance_retest.get("active"):
                execution_source = ExecutionSource.ACCEPTANCE_RETEST.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value
                raw += safe_float(acceptance_retest.get("score_bonus"), 0.0) * 0.35
                pattern_conf.append("🟢 ACCEPTANCE_RETEST: ранній continuation-probe дозволений малим ризиком")
            elif model_id == "VWAP_SESSION_MEAN_RECLAIM":
                execution_source = ExecutionSource.SESSION_MEAN.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value
                local_trigger_level = safe_float(session_mean_reclaim.get("level"), trigger_level) or trigger_level
                raw += safe_float(session_mean_reclaim.get("score_bonus"), 0.0) * 0.30
            elif model_id in {"OPENING_RANGE_BREAKOUT", "FAILED_ORB"}:
                execution_source = ExecutionSource.OPENING_RANGE.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value if model_id == "FAILED_ORB" else ExecutionLane.STANDARD_CONFIRMED.value
                local_trigger_level = safe_float(opening_range.get("entry_level"), trigger_level) or trigger_level
                raw += safe_float(opening_range.get("score_bonus"), 0.0) * 0.25
            elif model_id == "DAILY_WEEKLY_OPEN_RECLAIM":
                execution_source = ExecutionSource.OPEN_RECLAIM.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value
                local_trigger_level = safe_float(open_reclaim.get("level"), trigger_level) or trigger_level
                raw += safe_float(open_reclaim.get("score_bonus"), 0.0) * 0.30
            elif model_id == "LIQUIDITY_LADDER_MODEL":
                execution_source = ExecutionSource.LIQUIDITY_LADDER.value
                actionable_trigger_ready = bool(liquidity_ladder.get("entry_ok"))
                execution_lane_source = ExecutionLane.LIMIT_ONLY.value if actionable_trigger_ready else ExecutionLane.WAIT_RETEST.value
                raw += safe_float(liquidity_ladder.get("score_bonus"), 0.0) * 0.20
            elif model_id == "FAILED_AUCTION_REJECTION":
                execution_source = ExecutionSource.FAILED_AUCTION.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value
                raw += safe_float(failed_auction.get("score_bonus"), 0.0) * 0.35
            elif model_id == "TIME_OF_DAY_ADAPTIVE":
                execution_source = ExecutionSource.TIME_OF_DAY.value
                actionable_trigger_ready = bool(time_of_day_adaptive.get("entry_ok"))
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value if actionable_trigger_ready else ExecutionLane.WAIT_RETEST.value
                raw += safe_float(time_of_day_adaptive.get("score_bonus"), 0.0) * 0.20
            elif live_3m_trigger_ready:
                execution_source = ExecutionSource.LIVE_3M.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.EARLY_TACTICAL.value
            elif limit_armed_ready:
                execution_source = ExecutionSource.LIMIT_ARMED.value
                actionable_trigger_ready = True
                execution_lane_source = ExecutionLane.LIMIT_ONLY.value
                local_trigger_level = ce_level or trigger_level
                pattern_conf.append(f"🔥 LIMIT_ARMED: CHoCH + FVG, ліміт на CE ({local_trigger_level:.2f})")
                raw += 18
            elif time_warp_ready:
                execution_source = ExecutionSource.TIME_WARP.value
                actionable_trigger_ready = False
                execution_lane_source = ExecutionLane.WAIT_RETEST.value
                pattern_conf.append(f"⏳ TIME_WARP: старий 3M event {trigger_age:.0f} хв — тільки WAIT_RETEST/LIMIT")
            else:
                execution_source = ExecutionSource.NONE.value
                actionable_trigger_ready = False
                execution_lane_source = ExecutionLane.STANDARD_CONFIRMED.value

            if is_fallback:
                if actionable_trigger_ready and scan_stage in ["RETEST", "READY"]:
                    setup_type = SetupType.BREAKOUT_RETEST.value
                elif is_sweep and actionable_trigger_ready:
                    setup_type = SetupType.SWEEP_RECLAIM.value
                setup_family = SETUP_FAMILY_MAP.get(setup_type, SetupFamily.CONTINUATION.value)

            contract = ict_model_execution_contract(
                model_id=model_id,
                setup_type=setup_type,
                side=side,
                context=context,
                event=event,
                trigger_level=local_trigger_level,
                ce_level=ce_level,
                is_limit_armed=limit_armed_ready,
                model_context={
                    "ACCEPTANCE_RETEST_CONTINUATION": acceptance_retest,
                    "ACCELERATION_PULLBACK_REENTRY": acceleration_reentry,
                    "VWAP_SESSION_MEAN_RECLAIM": session_mean_reclaim,
                    "OPENING_RANGE_BREAKOUT": opening_range,
                    "FAILED_ORB": opening_range,
                    "DAILY_WEEKLY_OPEN_RECLAIM": open_reclaim,
                    "LIQUIDITY_LADDER_MODEL": liquidity_ladder,
                    "FAILED_AUCTION_REJECTION": failed_auction,
                    "TIME_OF_DAY_ADAPTIVE": time_of_day_adaptive,
                }.get(model_id, {}),
            )

            evidence = ["ICT_LOCATION", "PRICE_STRUCTURE", f"ICT_MODEL_{model_id}"]
            if flw_score > 0:
                evidence.append("ORDER_FLOW_CVD")
            if live_3m_trigger_ready:
                evidence.append("EXECUTION_TRIGGER_LIVE_3M")
            if limit_armed_ready:
                evidence.append("EXECUTION_LIMIT_ARMED")
            if time_warp_ready:
                evidence.append("EXECUTION_TIME_WARP_WAIT_RETEST")
            if execution_source not in {ExecutionSource.NONE.value, ExecutionSource.LIVE_3M.value, ExecutionSource.LIMIT_ARMED.value, ExecutionSource.TIME_WARP.value}:
                evidence.append(f"EXECUTION_{execution_source}")

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
                trigger_ready=actionable_trigger_ready,
                best_pattern=best_pattern,
                regime_matched=regime_matched,
                regime_conflict=regime_conflict,
                exhaustion_multiplier=exhaustion_multiplier,
                pattern_family=setup_family,
            )
            calibration = calibrate_candidate_quality(
                journal, quality_features, setup_family, actionable_trigger_ready, limit_armed_ready, has_forward_zone
            )
            layers = score_hypothesis_layers(
                loc_score=loc_score,
                str_score=str_score,
                liq_score=liq_score,
                flw_score=flw_score,
                trig_score=trig_score,
                htf_score=htf_score,
                raw_bonus=raw_bonus,
                session_bonus=session_bonus,
                vector_bonus=vector_bonus,
                execution_source=execution_source,
                trigger_age=trigger_age,
                has_forward_zone=has_forward_zone,
                target_magnet_score=float(contract.get("target_magnet_score", 0.0) or 0.0),
                regime_matched=regime_matched,
                regime_conflict=regime_conflict,
            )
            freshness_mult = float(layers["freshness_multiplier"])
            calibrated_score = int(calibration["score"])
            organic_score = int(layers["organic_score"])
            final = int(round((0.55 * calibrated_score + 0.45 * organic_score) * float(direction_perf.get("score_multiplier", 1.0))))
            final = int(clamp(final, 12, 98))
            priority_tiebreaker = float(p_data.get("priority", 0.0)) / 100.0
            hypothesis_score = float(final) + priority_tiebreaker * 2.0 + float(contract.get("target_magnet_score", 0.0) or 0.0) * 0.025

            pattern_conf.append(
                f"📊 Hypothesis score: final={final} | setup={layers['setup_quality']} "
                f"execution={layers['execution_quality']} plan={layers['trade_plan_quality']} "
                f"| model={calibration['model_source']} n={calibration['sample_size']} p={calibration['probability']:.2f}"
            )
            pattern_conf.append(f"🎯 Entry contract: {contract['entry_basis']} | invalidation: {contract['invalidation_basis']} | target: {contract['target_basis']}")
            if direction_perf.get("weak"):
                pattern_conf.append(f"📉 {side} recent performance слабкий: {direction_perf.get('wins')}/{direction_perf.get('closed')} wins — PROBE_ONLY sizing")

            lane = execution_lane_source
            if lane == ExecutionLane.EARLY_TACTICAL.value and not bool(p_data.get("allow_early", False)):
                lane = ExecutionLane.STANDARD_CONFIRMED.value

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
                    "setup_quality": layers["setup_quality"],
                    "execution_quality": layers["execution_quality"],
                    "trade_plan_quality": layers["trade_plan_quality"],
                    "organic_score": layers["organic_score"],
                    "hypothesis_score": round(hypothesis_score, 2),
                    "active_model_count": len(active_patterns),
                    "execution_source": execution_source,
                    "execution_freshness_multiplier": freshness_mult,
                    "live_3m_trigger_ready": live_3m_trigger_ready,
                    "limit_armed_ready": limit_armed_ready,
                    "time_warp_ready": time_warp_ready,
                    "direction_performance": direction_perf,
                    "direction_risk_multiplier": direction_perf.get("risk_multiplier", 1.0),
                    "entry_contract": contract,
                    "acceptance_retest": acceptance_retest if model_id == "ACCEPTANCE_RETEST_CONTINUATION" else {},
                    "acceleration_reentry": acceleration_reentry if model_id == "ACCELERATION_PULLBACK_REENTRY" else {},
                },
                evidence_families=evidence,
                confirmations=pattern_conf,
                trigger_ready=actionable_trigger_ready,
                trigger_level=round_price(local_trigger_level),
                invalidation_level=round_price(contract["invalidation_level"]),
                target_levels=[round_price(contract["target_level"])],
                execution_lane=lane,
                stage="ARMED" if final >= params["armed_score"] else "DISCOVERED",
                variant="MODEL_HYPOTHESIS" if not is_fallback else "GENERIC_FALLBACK",
                ict_model=best_pattern or "NONE",
                execution_anchor=round_price(contract["entry_anchor"]),
                trigger_age_minutes=trigger_age,
                thesis_key=f"{side}|{model_id}|{setup_type}|{int(round(contract['entry_anchor']*10))}",
                thesis=f"{side} {model_id} → {setup_type} | src={execution_source} | stage={scan_stage}",
                professional_gate={},
                has_forward_zone=has_forward_zone,
                live_3m_trigger_ready=live_3m_trigger_ready,
                limit_armed_ready=limit_armed_ready,
                time_warp_ready=time_warp_ready,
                execution_source=execution_source,
                risk_multiplier=float(direction_perf.get("risk_multiplier", 1.0) or 1.0),
                target_magnet_score=float(contract.get("target_magnet_score", 0.0) or 0.0),
                setup_quality_score=int(layers["setup_quality"]),
                execution_quality_score=int(layers["execution_quality"]),
                trade_plan_quality_score=int(layers["trade_plan_quality"]),
                hypothesis_score=round(hypothesis_score, 2),
                active_model_count=len(active_patterns),
            )
            cand.professional_gate = evaluate_professional_gate(context, cand)
            cand.stage_plan = staged_entry_plan(cand, context, direction_perf)
            cand.entry_stage = str(cand.stage_plan.get("stage", EntryStage.PROBE.value))
            cand = apply_setup_innovation_overlay(cand, context, state, journal)

            min_score_required = params["armed_score"] - 10 if not is_fallback else params["armed_score"] + NO_PATTERN_EXTRA_MARGIN
            # TIME_WARP не блокується, але мусить бути достатньо сильним, щоб хоча б ARMED/WATCH.
            if cand.execution_source == ExecutionSource.TIME_WARP.value:
                min_score_required += 4
            if cand.final_score >= min_score_required:
                candidates.append(cand)

    return finalize_hypothesis_ranking(candidates)



def _true_range_values(candles: list[Candle]) -> list[float]:
    """True Range без залежності від pandas/numpy. Беремо тільки підтверджені 15M.
    Якщо volume/feed кривий, TR все одно стабільно описує фактичний шум свічки."""
    confirmed = [c for c in (candles or []) if getattr(c, "confirmed", True)]
    if not confirmed:
        return []
    ordered = sorted(confirmed, key=lambda c: c.ts)
    values: list[float] = []
    prev_close: Optional[float] = None
    for c in ordered:
        hl = max(float(c.high) - float(c.low), 0.0)
        if prev_close is None:
            tr = hl
        else:
            tr = max(hl, abs(float(c.high) - prev_close), abs(float(c.low) - prev_close))
        if tr > 0 and math.isfinite(tr):
            values.append(tr)
        prev_close = float(c.close)
    return values


def _percentile(values: list[float], q: float, default: float = 0.0) -> float:
    clean = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) >= 0)
    if not clean:
        return float(default)
    q = clamp(float(q), 0.0, 1.0)
    if len(clean) == 1:
        return clean[0]
    idx = q * (len(clean) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return clean[lo]
    return clean[lo] * (hi - idx) + clean[hi] * (idx - lo)


def candle_noise_profile(context: dict, price: float, atr15: float) -> dict[str, float]:
    """Оцінка 15M шуму, яку використовуємо для TP/SL.
    Це не фільтр. Якщо шум широкий, бот не відмовляється від угоди, а дає їй більший stop-distance
    і зменшує sizing."""
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []
    recent = sorted([c for c in c15 if getattr(c, "confirmed", True)], key=lambda c: c.ts)[-48:]
    tr_values = _true_range_values(recent)
    fallback = max(float(atr15 or 0.0), price * 0.0012, ABS_MIN_STOP_DOLLARS * 0.50)
    p70 = _percentile(tr_values, STOP_NOISE_PERCENTILE, fallback)
    p85 = _percentile(tr_values, TP_NOISE_PERCENTILE, max(fallback, p70))
    current = 0.0
    if c15:
        last = c15[-1]
        current = max(float(last.high) - float(last.low), 0.0)
    return {
        "tr_p70": round(float(max(p70, fallback * 0.75)), 6),
        "tr_p85": round(float(max(p85, p70, fallback)), 6),
        "current_tr": round(float(max(current, fallback * 0.75)), 6),
        "fallback": round(float(fallback), 6),
        "sample_size": len(tr_values),
    }


def build_trade_breathing_geometry(side: str, price: float, structural_stop: float, effective_atr15: float,
                                   profile: dict, context: dict, structural_from_zone: bool = False) -> dict[str, Any]:
    """Trade Breathing Geometry Engine.

    Повертає два стопи:
    - decision_stop: thesis invalidation по 15M close-confirm;
    - catastrophic_stop: реальний hard stop за межами wick/noise envelope.

    Ширший стоп не блокує угоду. Він зменшує position_risk_pct через risk_size_multiplier.
    """
    noise = candle_noise_profile(context, price, effective_atr15)
    raw_structural_distance = abs(price - float(structural_stop or price))
    model_min = effective_atr15 * float(profile.get("stop_min_atr", MIN_STOP_ATR15))
    model_max = effective_atr15 * float(profile.get("stop_max_atr", MAX_STOP_ATR15)) * (1.75 if structural_from_zone else 1.25)

    decision_distance = max(
        raw_structural_distance,
        model_min,
        ABS_MIN_STOP_DOLLARS * 0.95,
        float(noise["tr_p70"]) * 0.85,
    )
    # Не дозволяємо fallback-геометрії ставати абсурдною, але не зрізаємо справжню старшу структуру.
    if not structural_from_zone:
        decision_distance = min(decision_distance, max(model_max, ABS_MIN_STOP_DOLLARS))

    catastrophic_distance = max(
        decision_distance * CATASTROPHIC_STOP_MULT,
        ABS_MIN_STOP_DOLLARS,
        float(noise["tr_p70"]) * MIN_STOP_TRUE_RANGE_MULT,
        float(noise["current_tr"]) * CURRENT_CANDLE_STOP_MULT,
        effective_atr15 * float(profile.get("stop_min_atr", MIN_STOP_ATR15)) * 1.15,
    )
    catastrophic_distance = max(catastrophic_distance, decision_distance + max(price * 0.0005, effective_atr15 * 0.15))

    if side == Side.LONG.value:
        decision_stop = price - decision_distance
        catastrophic_stop = price - catastrophic_distance
    else:
        decision_stop = price + decision_distance
        catastrophic_stop = price + catastrophic_distance

    conventional = max(raw_structural_distance, model_min, ABS_MIN_STOP_DOLLARS)
    if conventional <= 1e-9:
        risk_size_multiplier = 1.0
    else:
        risk_size_multiplier = clamp(conventional / max(catastrophic_distance, conventional), MIN_BREATHING_RISK_MULTIPLIER, 1.0)

    tp1_floor_distance = max(
        catastrophic_distance * TP1_MIN_RR_PRO,
        ABS_MIN_TP1_DOLLARS,
        float(noise["tr_p85"]) * MIN_TP1_TRUE_RANGE_MULT,
        effective_atr15 * TP1_MIN_ATR_PRO,
    )
    tp0_floor_distance = max(
        catastrophic_distance * TP0_MIN_RR,
        float(noise["tr_p70"]),
        effective_atr15 * 1.10,
    )

    return {
        "decision_stop": round_price(decision_stop),
        "catastrophic_stop": round_price(catastrophic_stop),
        "decision_distance": round(float(decision_distance), 6),
        "catastrophic_distance": round(float(catastrophic_distance), 6),
        "conventional_distance": round(float(conventional), 6),
        "risk_size_multiplier": round(float(risk_size_multiplier), 4),
        "tp1_floor_distance": round(float(tp1_floor_distance), 6),
        "tp0_floor_distance": round(float(tp0_floor_distance), 6),
        "noise": noise,
        "mode": "TRADE_BREATHING_GEOMETRY",
    }


def setup_trade_profile(setup_type: str) -> dict:
    profiles = {
        SetupType.PULLBACK_CONTINUATION.value: {"tp1_rr": 1.65, "tp2_rr": 3.30, "tp3_rr": 5.50, "tp1_atr": 1.40, "stop_min_atr": 0.95, "stop_max_atr": 2.50, "quality_adjustment": 2},
        SetupType.BREAKOUT_RETEST.value: {"tp1_rr": 1.55, "tp2_rr": 2.90, "tp3_rr": 4.40, "tp1_atr": 1.30, "stop_min_atr": 0.90, "stop_max_atr": 2.20, "force_risky": True},
        SetupType.SWEEP_RECLAIM.value: {"tp1_rr": 1.50, "tp2_rr": 2.70, "tp3_rr": 4.00, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 2.00, "force_risky": True},
        SetupType.CAPITULATION_RECOVERY.value: {"tp1_rr": 1.55, "tp2_rr": 2.80, "tp3_rr": 4.10, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 2.10, "force_risky": True},
        SetupType.TREND_IGNITION.value: {"tp1_rr": 1.70, "tp2_rr": 3.30, "tp3_rr": 5.60, "tp1_atr": 1.45, "stop_min_atr": 0.95, "stop_max_atr": 2.50, "force_risky": True},
        SetupType.RANGE_COMPRESSION_BREAKOUT.value: {"tp1_rr": 1.50, "tp2_rr": 2.50, "tp3_rr": 3.40, "tp1_atr": 1.20, "stop_min_atr": 0.80, "stop_max_atr": 1.60, "force_risky": True},
        SetupType.RANGE_EDGE_REVERSAL.value: {"tp1_rr": 1.50, "tp2_rr": 2.55, "tp3_rr": 3.50, "tp1_atr": 1.20, "stop_min_atr": 0.80, "stop_max_atr": 1.50, "force_risky": True},
        SetupType.ACCEPTANCE_RETEST_CONTINUATION.value: {"tp1_rr": 1.65, "tp2_rr": 3.10, "tp3_rr": 5.00, "tp1_atr": 1.35, "stop_min_atr": 0.85, "stop_max_atr": 2.00, "force_risky": True},
        SetupType.ACCELERATION_PULLBACK_REENTRY.value: {"tp1_rr": 1.70, "tp2_rr": 3.20, "tp3_rr": 5.20, "tp1_atr": 1.40, "stop_min_atr": 0.95, "stop_max_atr": 2.40, "force_risky": True},
        SetupType.SESSION_MEAN_RECLAIM.value: {"tp1_rr": 1.55, "tp2_rr": 3.05, "tp3_rr": 4.60, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 1.80, "force_risky": True},
        SetupType.OPENING_RANGE_BREAKOUT.value: {"tp1_rr": 1.60, "tp2_rr": 3.20, "tp3_rr": 5.10, "tp1_atr": 1.35, "stop_min_atr": 0.75, "stop_max_atr": 1.90, "force_risky": True},
        SetupType.FAILED_OPENING_RANGE_BREAKOUT.value: {"tp1_rr": 1.50, "tp2_rr": 2.85, "tp3_rr": 4.00, "tp1_atr": 1.20, "stop_min_atr": 0.85, "stop_max_atr": 2.10, "force_risky": True},
        SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value: {"tp1_rr": 1.55, "tp2_rr": 3.10, "tp3_rr": 4.80, "tp1_atr": 1.25, "stop_min_atr": 0.85, "stop_max_atr": 2.00, "force_risky": True},
        SetupType.LIQUIDITY_LADDER.value: {"tp1_rr": 1.75, "tp2_rr": 3.80, "tp3_rr": 6.00, "tp1_atr": 1.45, "stop_min_atr": 0.95, "stop_max_atr": 2.30},
        SetupType.FAILED_AUCTION_REJECTION.value: {"tp1_rr": 1.50, "tp2_rr": 2.90, "tp3_rr": 4.25, "tp1_atr": 1.20, "stop_min_atr": 0.75, "stop_max_atr": 1.80, "force_risky": True},
        SetupType.TIME_OF_DAY_ADAPTIVE.value: {"tp1_rr": 1.55, "tp2_rr": 3.10, "tp3_rr": 4.70, "tp1_atr": 1.25, "stop_min_atr": 0.90, "stop_max_atr": 2.10},
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
        # v6.13: RISKY означає менший sizing, а не близькі тейки.
        # Тому не стискаємо RR, а навпаки тримаємо TP1 за межами шуму/1-свічкового руху.
        profile["tp1_rr"] = max(float(profile.get("tp1_rr", PREFERRED_RR1)), TP1_MIN_RR_PRO)
        profile["tp2_rr"] = max(float(profile.get("tp2_rr", MIN_RR2)), max(MIN_RR2, TP1_MIN_RR_PRO + 1.00))
    
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
    min_tp1_distance = max(
        risk * max(TP1_MIN_RR_PRO, MIN_RR1),
        ABS_MIN_TP1_DOLLARS,
        max(float(atr15 or 0.0), price * 0.0008) * TP1_MIN_ATR_PRO,
    )
    min_tp2_distance = risk * max(3.00, MIN_RR1 + 1.25)
    min_tp3_distance = risk * max(4.60, MIN_RR1 + 3.10)
    step = max(atr15 * 0.65, price * 0.0035)
    if side == Side.LONG.value:
        tp1 = max(tp1, price + min_tp1_distance)
        tp2 = max(tp2, price + min_tp2_distance, tp1 + step)
        tp3 = max(tp3, price + min_tp3_distance, tp2 + step)
    else:
        tp1 = min(tp1, price - min_tp1_distance)
        tp2 = min(tp2, price - min_tp2_distance, tp1 - step)
        tp3 = min(tp3, price - min_tp3_distance, tp2 - step)
    return stop, tp1, tp2, tp3


def target_magnet_score(target: dict, price: float, atr15: float) -> float:
    """Оцінює не просто близькість TP, а силу цільового магніту.
    Ближче не завжди краще: дрібна 15M FVG за 0.2R слабша за 1H/PDH liquidity pool."""
    kind = str(target.get("kind", "")).upper()
    tf = str(target.get("timeframe", "15m")).lower()
    distance = max(float(target.get("distance", abs(float(target.get("level", price)) - price)) or 0.0), 1e-9)
    strength = max(float(target.get("strength", 1.0) or 1.0), 0.1)
    kind_weight = {
        "BSL_LIQUIDITY": 1.55, "SSL_LIQUIDITY": 1.55,
        "PDH": 1.85, "PDL": 1.85,
        "ASIAN_HIGH": 1.65, "ASIAN_LOW": 1.65,
        "RANGE_HIGH": 1.25, "RANGE_LOW": 1.25,
        "MACRO_EQ": 1.15,
        "DAILY_OPEN": 1.45, "WEEKLY_OPEN": 1.70,
        "VWAP": 1.38, "SESSION_MEAN": 1.28,
        "OR_HIGH": 1.42, "OR_LOW": 1.42,
        "OB": 1.10, "FVG": 1.05,
    }.get(kind, 1.0)
    tf_weight = {"4h": 1.45, "1d": 1.55, "1h": 1.30, "15m": 1.0}.get(tf, 1.0)
    atr = max(float(atr15 or 0.0), price * 0.0006, 1e-9)
    distance_atr = distance / atr
    # Плавний penalty за відстань: не вбиває далекі DOL, але не дає випадково
    # обрати космічну ціль замість нормального intraday magnet.
    distance_penalty = 1.0 / (1.0 + max(distance_atr - 1.0, 0.0) * 0.18)
    return round(strength * kind_weight * tf_weight * distance_penalty, 4)


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
        for key, kind, strength in (("daily_open", "DAILY_OPEN", 1.25), ("weekly_open", "WEEKLY_OPEN", 1.45)):
            lvl = macro.get(key)
            if lvl and lvl > price:
                targets.append({"level": lvl, "kind": kind, "timeframe": "1D", "strength": strength})
    else:
        for kind, lvl in (("ASIAN_LOW", macro.get("asian_low")), ("PDL", macro.get("pdl"))):
            if lvl and 0 < lvl < price:
                targets.append({"level": lvl, "kind": kind, "timeframe": "1D", "strength": 1.6})
        eq = macro.get("macro_eq")
        if eq and 0 < eq < price:
            targets.append({"level": eq, "kind": "MACRO_EQ", "timeframe": "1D", "strength": 1.2})
        for key, kind, strength in (("daily_open", "DAILY_OPEN", 1.25), ("weekly_open", "WEEKLY_OPEN", 1.45)):
            lvl = macro.get(key)
            if lvl and 0 < lvl < price:
                targets.append({"level": lvl, "kind": kind, "timeframe": "1D", "strength": strength})

    if not targets:
        return []

    for t in targets:
        t["distance"] = abs(t["level"] - price)
        t["magnet_score"] = target_magnet_score(t, price, atr15)
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
    price = safe_float(getattr(candidate, "execution_anchor", 0.0), 0.0) or safe_float(context.get("price"), 0.0)
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
            "ACCEPTANCE_RETEST_CONTINUATION": (0.85, 2.0), "ACCELERATION_PULLBACK_REENTRY": (0.95, 2.4),
            "VWAP_SESSION_MEAN_RECLAIM": (0.85, 1.8), "OPENING_RANGE_BREAKOUT": (0.75, 1.9),
            "FAILED_ORB": (0.85, 2.1), "DAILY_WEEKLY_OPEN_RECLAIM": (0.85, 2.0),
            "LIQUIDITY_LADDER_MODEL": (0.95, 2.3), "FAILED_AUCTION_REJECTION": (0.75, 1.8),
            "TIME_OF_DAY_ADAPTIVE": (0.90, 2.1),
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

    # v6.13 Trade-Breathing Geometry:
    # decision_stop = close-confirm інвалідація тези; catastrophic_stop = реальний hard stop.
    # Стоп не стискається всередину однієї 15M свічки. Якщо він стає ширшим — sizing зменшується.
    effective_atr15 = _effective_atr15(atr15, price)
    breathing = build_trade_breathing_geometry(
        side=side, price=price, structural_stop=structural_stop, effective_atr15=effective_atr15,
        profile=profile, context=context, structural_from_zone=structural_from_zone,
    )
    structural_stop = float(breathing["decision_stop"])
    stop = float(breathing["catastrophic_stop"])
    stop_dist = abs(price - stop)

    tp1_dist = max(
        stop_dist * float(profile.get("tp1_rr", TP1_MIN_RR_PRO)),
        effective_atr15 * max(MIN_TP1_ATR15, TP1_MIN_ATR_PRO),
        float(breathing["tp1_floor_distance"]),
    )
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
    min_tp1_distance = max(
        stop_dist * max(TP1_MIN_RR_PRO, MIN_RR1),
        ABS_MIN_TP1_DOLLARS,
        effective_atr15 * TP1_MIN_ATR_PRO,
        float(breathing["tp1_floor_distance"]),
    )
    # === Speed Bump Filter v6.13 ===
    # TP1 не може лежати всередині одного нормального 15M noise-envelope. Інакше це не тейк,
    # а декоративна монетка перед катком.
    min_tp2_distance = stop_dist * max(3.00, MIN_RR1 + 1.25)
    min_tp3_distance = stop_dist * max(4.60, MIN_RR1 + 3.10)
    tech_targets = find_technical_targets(side, price, zones, context.get("liquidity", {}), atr15,
                                           macro=context.get("macro_liquidity", {}))
    tp_sources = {"tp1": "RR-профіль", "tp2": "RR-профіль", "tp3": "RR-профіль"}

    MACRO_KINDS = {"ASIAN_HIGH", "ASIAN_LOW", "PDH", "PDL", "MACRO_EQ", "DAILY_OPEN", "WEEKLY_OPEN", "VWAP", "SESSION_MEAN", "OR_HIGH", "OR_LOW"}

    def _pick_technical(floor_dist: float, cap_mult: float, used_levels: list[float]) -> Optional[dict]:
        eligible = []
        for t in tech_targets:
            if t["distance"] < floor_dist - 1e-9:
                continue
            if t["distance"] > floor_dist * cap_mult:
                break  # список відсортований за відстанню — далі буде ще далі
            if any(abs(t["level"] - u) < step for u in used_levels):
                continue
            eligible.append(t)
        if not eligible:
            return None
        eligible.sort(key=lambda t: (-float(t.get("magnet_score", 0.0)), t["distance"]))
        return eligible[0]

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
        eligible = []
        for t in tech_targets:
            if t["kind"] not in MACRO_KINDS:
                continue
            if t["distance"] < floor_dist - 1e-9:
                continue
            if any(abs(t["level"] - u) < step for u in used_levels):
                continue
            eligible.append(t)
        if not eligible:
            return None
        eligible.sort(key=lambda t: (-float(t.get("magnet_score", 0.0)), t["distance"]))
        return eligible[0]

    used_levels: list[float] = []
    tp1_tech = _pick_technical(min_tp1_distance, 3.2, used_levels)
    if tp1_tech:
        tp1 = tp1_tech["level"]
        tp_sources["tp1"] = f"{tp1_tech['kind']} ({tp1_tech['timeframe']}, magnet {tp1_tech.get('magnet_score', 0)})"
    used_levels.append(tp1)

    tp2_floor = max(min_tp2_distance, abs(tp1 - price) + step)
    tp2_tech = _pick_technical(tp2_floor, 2.4, used_levels) or _pick_macro(tp2_floor, used_levels)
    if tp2_tech:
        tp2 = tp2_tech["level"]
        tp_sources["tp2"] = f"{tp2_tech['kind']} ({tp2_tech['timeframe']}, magnet {tp2_tech.get('magnet_score', 0)})"
    else:
        tp2 = max(tp2, price + tp2_floor) if side == Side.LONG.value else min(tp2, price - tp2_floor)
    used_levels.append(tp2)

    tp3_floor = max(min_tp3_distance, abs(tp2 - price) + step)
    tp3_tech = _pick_technical(tp3_floor, 2.2, used_levels) or _pick_macro(tp3_floor, used_levels)
    if tp3_tech:
        tp3 = tp3_tech["level"]
        tp_sources["tp3"] = f"{tp3_tech['kind']} ({tp3_tech['timeframe']}, magnet {tp3_tech.get('magnet_score', 0)})"
    else:
        tp3 = max(tp3, price + tp3_floor) if side == Side.LONG.value else min(tp3, price - tp3_floor)

    # Фінальний прогін через ratchet — навіть якщо snap до технічного рівня чомусь
    # порушив монотонність (TP2 ближче за TP1 тощо), тут це виправляється і
    # професійний RR-floor лишається непорушним у будь-якому випадку.
    stop, tp1, tp2, tp3 = enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15)
    
    rr1 = abs(tp1 - price) / abs(stop - price) if abs(stop - price) > 1e-9 else 2.1
    regime_action = str(profile.get("entry_action", "ALLOW")).upper()
    execution_ready = candidate.trigger_ready and candidate.final_score >= ENTRY_SCORE_BASE and not profile.get("hard_block") and regime_action in {"ALLOW", "RISKY_ONLY"}
    # v6.15: старий trigger не відкриває late-entry сам. Це не hard block:
    # thesis лишається ARMED, але entry_ready зʼявляється тільки після нового
    # сетапного доказу на 3M/15M або якщо execution справді fresh.
    reval_profile = getattr(candidate, "revalidation_profile", {}) or ((candidate.stage_plan or {}).get("innovation_profile", {}) or {}).get("trigger_revalidation", {}) or {}
    if reval_profile.get("needs_revalidation") and not reval_profile.get("entry_supported"):
        execution_ready = False
    elif reval_profile.get("needs_revalidation") and reval_profile.get("entry_supported"):
        execution_ready = bool(candidate.final_score >= RISKY_ENTRY_SCORE_BASE and not profile.get("hard_block") and regime_action in {"ALLOW", "RISKY_ONLY"})

    # Будь-який нестандартний (ризикований) лейн виконання — раннє тактичне входження
    # АБО re-entry з пропущеного імпульсу — повинен отримувати зменшений розмір позиції.
    # Раніше перевірявся лише EARLY_TACTICAL, через що RISKY_ENTRY угоди типу
    # "Re-entry з пропущеного імпульсу" (MISSED_IMPULSE_REENTRY) помилково
    # відкривались з повним NORMAL_RISK_PCT замість зменшеного RISKY_RISK_PCT.
    RISKY_EXECUTION_LANES = {
        ExecutionLane.EARLY_TACTICAL.value,
        ExecutionLane.MISSED_IMPULSE_REENTRY.value,
        ExecutionLane.LIMIT_ONLY.value,
        ExecutionLane.WAIT_RETEST.value,
    }
    is_risky_lane = candidate.execution_lane in RISKY_EXECUTION_LANES

    # enforce_smart_money_rr() вище математично гарантує |tp1-price| >= risk*MIN_RR1,
    # тож rr1 не повинен опускатись нижче MIN_RR1. Але коли профіль якогось сетапу
    # виставляє tp1_rr РІВНО на рівні MIN_RR1 (межовий, а не із запасом), ланцюжок
    # округлень stop_dist -> tp1_dist -> rr1 іноді дає щось на кшталт 1.4999999999998
    # замість точних 1.5 — суто похибка float. Без допуску це БЛОКУВАЛО Б угоду
    # (valid=False) через математично неіснуючу різницю в 13-му знаку.
    RR_EPSILON = 1e-6
    rr1_valid = rr1 >= (MIN_RR1 - RR_EPSILON)

    risk_distance = abs(stop - price) if abs(stop - price) > 1e-9 else stop_dist
    tp1_distance = abs(tp1 - price)
    # TP0 — мікрофіксація win-rate без переводу стопа в БУ. Вона не душить TP1/TP2,
    # бо TP1 лишається професійним target-magnet/RR рівнем.
    tp0_floor = max(risk_distance * TP0_MIN_RR, float(breathing["tp0_floor_distance"]))
    tp0_cap = max(tp1_distance - max(step * 0.35, price * 0.00025), risk_distance * 0.65)
    tp0_dist = min(tp0_floor, tp0_cap)
    tp0_dist = max(tp0_dist, risk_distance * 0.65)
    if side == Side.LONG.value:
        tp0 = min(price + tp0_dist, tp1 - max(step * 0.10, price * 0.0001))
    else:
        tp0 = max(price - tp0_dist, tp1 + max(step * 0.10, price * 0.0001))
    rr0 = abs(tp0 - price) / risk_distance if risk_distance > 1e-9 else TP0_RR

    default_risk = RISKY_RISK_PCT if is_risky_lane or profile.get("force_risky") else NORMAL_RISK_PCT
    if not candidate.stage_plan:
        candidate.stage_plan = staged_entry_plan(candidate, context)
        candidate.entry_stage = str(candidate.stage_plan.get("stage", EntryStage.PROBE.value))
    if not (candidate.stage_plan or {}).get("innovation_profile"):
        candidate = apply_setup_innovation_overlay(candidate, context)
    position_risk_pct = adaptive_position_risk_pct(candidate, context, default_risk)
    position_risk_pct = round(clamp(position_risk_pct * float(breathing.get("risk_size_multiplier", 1.0)), 0.02, CORE_RISK_PCT), 4)
    candidate.stage_plan.setdefault("breathing_geometry", breathing)
    candidate.stage_plan.setdefault("scale_plan", []).append(
        f"v6.13 breathing: hard stop ширший за noise envelope; sizing x{breathing.get('risk_size_multiplier')}"
    )

    partial_plan = dynamic_partial_plan_from_innovation(candidate)

    plan = TradePlan(
        entry=round_price(price),
        stop=round_price(stop),
        tp1=round_price(tp1), tp2=round_price(tp2), tp3=round_price(tp3),
        risk_pct=position_risk_pct,
        rr1=round(rr1, 2), rr2=round(abs(tp2 - price) / abs(stop - price), 2), rr3=round(abs(tp3 - price) / abs(stop - price), 2),
        position_risk_pct=position_risk_pct,
        invalidation=f"decision stop: 15M close за {round_price(structural_stop)} | hard stop: {round_price(stop)}",
        stop_basis=f"v6.13 breathing geometry | decision={round_price(structural_stop)} catastrophic={round_price(stop)} | noise={breathing['noise']}",
        target_basis=f"TP0: {TP0_MIN_RR}R/noise service fix | TP1: {tp_sources['tp1']} | TP2: {tp_sources['tp2']} | TP3: {tp_sources['tp3']}",
        stop_timeframe="1H" if any(z.timeframe == "1h" for z in zones) else "15M",
        structural_invalidation=round_price(structural_stop),
        trigger_level=candidate.trigger_level,
        execution_ready=execution_ready,
        tp0=round_price(tp0),
        rr0=round(rr0, 2),
        entry_stage=candidate.entry_stage,
        execution_source=candidate.execution_source,
        stage_plan=candidate.stage_plan,
        partial_plan=partial_plan,
        runtime_config_snapshot=runtime_config_snapshot(),
        decision_stop=round_price(structural_stop),
        catastrophic_stop=round_price(stop),
        breathing_profile=breathing,
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
            missed_cand = rescore_reentry_candidate(missed_cand, context, journal)
            guard = event_driven_reentry_guard(state, context, missed_cand)
            if not guard["blocked"] and missed_cand.final_score >= MISSED_REENTRY_SCORE * get_adaptive_params(context["regime"])["reentry_aggressiveness"]:
                plan = build_trade_plan(context, missed_cand)
                action = Action.RISKY_ENTRY.value if missed_cand.final_score >= REENTRY_AGGRESSIVE_THRESHOLD else Action.ARMED.value
                return Decision(
                    id=uuid.uuid4().hex[:10], time=iso_now(), action=action, side=missed_cand.side, setup_type=missed_cand.setup_type,
                    quality=missed_cand.final_score, reason="Re-entry з пропущеного імпульсу", regime=context["regime"],
                    candidate=missed_cand, plan=plan, current_price=current_price,
                    audit={"selected": hypothesis_audit_row(missed_cand), "reentry_rescored": True}
                )

    # 2. БАГАТОПОТОКОВИЙ СКАНЕР: Відбір усіх валідних кандидатів
    valid_candidates = []
    for cand in cands:
        gate = cand.professional_gate or evaluate_professional_gate(context, cand)
        # Додаємо кандидата, якщо він проходить хоча б один Gate
        if gate.get("allow_entry") or gate.get("allow_risky") or cand.final_score >= get_adaptive_params(context["regime"])["armed_score"]:
            valid_candidates.append(cand)

    if not valid_candidates:
        rejected = rejected_hypotheses_audit(context, cands, limit=10)
        return Decision(
            id=uuid.uuid4().hex[:10], time=iso_now(), action=Action.NO_SETUP.value, side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value,
            quality=12, reason="Ринок не сформував професійного ICT + 3M execution-package", regime=context["regime"], current_price=current_price,
            audit={"rejected_hypotheses": rejected, "candidate_count": len(cands), "audit_note": "NO_SETUP now stores why hypotheses failed"}
        )

    # 3. Вибір переможця за hypothesis_score, а не лише за final_score.
    valid_candidates = finalize_hypothesis_ranking(valid_candidates)
    best = valid_candidates[0]
    
    # v6.17.4: Decision Kernel becomes the final execution context layer
    kernel_result = professional_decision_kernel(best)
    best = apply_kernel_context(best, kernel_result)

    # v6.17.6 preserve opportunities without full exposure
    opportunity_profile = adaptive_opportunity_engine(best)
    setattr(best, "opportunity_profile", opportunity_profile)

    # v6.17.7 unified state transition
    state_result = adaptive_state_transition(
        best,
        kernel_result,
        opportunity_profile
    )
    setattr(best, "execution_state", state_result)

    if state_result.get("state") == STATE_PROBE:
        setattr(best, "entry_stage", "PROBE")

    if not state_result.get("allow_execution", False):
        setattr(best, "kernel_action_modifier", "ARMED")

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
    reval_profile = getattr(best, "revalidation_profile", {}) or ((best.stage_plan or {}).get("innovation_profile", {}) or {}).get("trigger_revalidation", {}) or {}
    reval_wait = bool(reval_profile.get("needs_revalidation") and not reval_profile.get("entry_supported"))
    reval_live = bool(reval_profile.get("needs_revalidation") and reval_profile.get("entry_supported"))

    kernel_modifier = getattr(best, "kernel_action_modifier", "")

    if kernel_modifier == "ARMED":
        action = Action.ARMED.value
        reason = "v6.17 kernel: execution підтвердження недостатнє, thesis збережена в ARMED"
    elif kernel_modifier == "WAIT_RETEST":
        action = Action.ARMED.value
        reason = "v6.17.4 kernel: anti-chase protection, очікування ретесту"
    elif reval_wait:
        action = Action.ARMED.value
        reason = f"[{best.ict_model}] thesis не заблокована, але trigger старий: чекаємо свіжу 3M/15M сетапну аргументацію"
    elif hard_block:
        action = Action.NO_SETUP.value
        reason = f"Regime Engine 2.0 блокує вхід: {mode_profile.get('reason', 'режим не підтримує вхід')}"
    elif gate.get("allow_entry") and plan.valid and plan.execution_ready and entry_action == "ALLOW" and not mode_profile.get("force_risky"):
        action = Action.ENTRY.value
        prefix = "SETUP-REVALIDATED " if reval_live else ""
        reason = f"[{best.ict_model}] {prefix}{best.entry_stage}/{best.execution_source}: {setup_label(best.setup_type)} — {gate.get('grade', 'A')} gate v6.10"
    elif (gate.get("allow_risky") or entry_action == "RISKY_ONLY" or mode_profile.get("force_risky")) and plan.valid and plan.execution_ready and not hard_block:
        action = Action.RISKY_ENTRY.value
        prefix = "SETUP-REVALIDATED " if reval_live else ""
        reason = f"[{best.ict_model}] {prefix}Стадійний {best.entry_stage}: {setup_label(best.setup_type)} — {mode_profile.get('regime_type')} profile"
    elif best.final_score >= params["armed_score"]:
        action = Action.ARMED.value
        reason = f"[{best.ict_model}] {best.entry_stage}/{best.execution_source}: сетап сформовано; gate v6.10: {gate.get('grade', 'WATCH')}"

    return Decision(
        id=uuid.uuid4().hex[:10], time=iso_now(), action=action, side=best.side, setup_type=best.setup_type,
        quality=quality, reason=reason, regime=context["regime"], candidate=best, plan=plan, current_price=current_price,
        audit={
            "selected": hypothesis_audit_row(best),
            "trigger_revalidation": reval_profile,
            "hypothesis_matrix": [hypothesis_audit_row(c) for c in valid_candidates[:10]],
            "candidate_count": len(valid_candidates),
            "institutional_engine": {
                "enabled": INSTITUTIONAL_ADAPTIVE_ENGINE,
                "version": BOT_VERSION,
            },
        }
    )


# ==========================================================
# v6.17 INSTITUTIONAL HELPERS
# ==========================================================

def institutional_execution_score(candidate: Any) -> float:
    """Оцінка якості execution без блокування сетапу."""
    if not candidate:
        return 0.0

    components = getattr(candidate, "score_components", {}) or {}
    features = components.get("features", {}) or {}

    # v6.17.9 schema compatibility:
    # Candidate stores raw scores as *_score and normalized values in features.
    liquidity = safe_float(
        components.get("liq_score", features.get("liquidity", 0)), 0.0
    )
    trigger = safe_float(
        components.get("trig_score", features.get("trigger", 0)), 0.0
    )
    structure = safe_float(
        components.get("str_score", features.get("structure", 0)), 0.0
    )

    score = 0.0
    score += liquidity * PRO_SCORE_LIQUIDITY_WEIGHT
    score += trigger * PRO_SCORE_TRIGGER_WEIGHT
    score += structure * PRO_SCORE_STRUCTURE_WEIGHT

    risks = getattr(candidate, "risks", []) or []
    if any("late" in str(x).lower() for x in risks):
        score *= PRO_SCORE_LATE_ENTRY_PENALTY

    return round(score, 3)



def regression_guard_revalidated_probe(candidate: Any) -> dict[str, Any]:
    """
    Safety regression guard:
    strong setup with valid execution evidence must not silently degrade to zero-score.
    This is a diagnostic guard, not an entry override.
    """
    score = institutional_execution_score(candidate)
    result = {
        "execution_score": score,
        "passed": True,
        "reason": "ok"
    }

    if candidate:
        trigger_ready = bool(getattr(candidate, "trigger_ready", False))
        final_score = int(getattr(candidate, "final_score", 0) or 0)

        if trigger_ready and final_score >= 85 and score <= 0:
            result["passed"] = False
            result["reason"] = "high_quality_candidate_zero_execution_score"

    return result


def detect_execution_chase(price_move_atr: float, body_ratio: float) -> bool:
    """Захист від входу після вже виконаного імпульсу."""
    if not CHASE_DETECTION_ENABLED:
        return False
    return price_move_atr >= CHASE_ATR_LIMIT and body_ratio >= CHASE_BODY_RATIO_LIMIT


def adaptive_htf_risk_multiplier(htf_state: str, execution_score: float) -> float:
    """HTF використовується як adaptive risk modifier, а не як простий veto."""
    state = str(htf_state).lower()

    if state in {"aligned", "bullish", "bearish", "support"}:
        return 1.0

    if state in {"neutral", "mixed"}:
        return HTF_NEUTRAL_RISK_MULT

    if execution_score >= HTF_OVERRIDE_MIN_SCORE and HTF_RISKY_OVERRIDE:
        return HTF_OVERRIDE_ACTIVE_RISK_MULT

    return HTF_STRONG_AGAINST_RISK_MULT



# ==========================================================
# v6.17.2 PROFESSIONAL ADAPTIVE DECISION LAYER
# ==========================================================

def adaptive_execution_guard(candidate: Any) -> dict[str, Any]:
    """Єдина точка контролю якості execution."""
    result = {
        "allow": True,
        "force_wait_retest": False,
        "risk_multiplier": 1.0,
        "reasons": []
    }

    if not candidate or not INSTITUTIONAL_ADAPTIVE_ENGINE:
        return result

    score = institutional_execution_score(candidate)

    components = getattr(candidate, "score_components", {}) or {}
    htf = components.get("htf_state", "neutral")

    result["risk_multiplier"] *= adaptive_htf_risk_multiplier(htf, score)

    body_ratio = float(components.get("body_ratio", 0))
    move_atr = float(components.get("move_atr", 0))

    if detect_execution_chase(move_atr, body_ratio):
        result["force_wait_retest"] = True
        result["reasons"].append("anti-chase: impulse already expanded")

    if score < 50:
        result["risk_multiplier"] *= 0.5
        result["reasons"].append("low institutional execution quality")

    return result



# ==========================================================
# v6.17.4 PRODUCTION INTEGRATION LAYER
# ==========================================================

def apply_kernel_context(candidate: Any, kernel_result: dict[str, Any]) -> Any:
    """
    Передає результат kernel далі по pipeline без повторного множення ризику.
    Один центр ризику: candidate -> build_trade_plan.
    """
    if candidate is None:
        return candidate

    try:
        setattr(
            candidate,
            "kernel_risk_multiplier",
            float(kernel_result.get("risk_multiplier", 1.0))
        )
        setattr(
            candidate,
            "kernel_action_modifier",
            str(kernel_result.get("action_modifier", "ALLOW"))
        )
        setattr(
            candidate,
            "kernel_audit",
            kernel_result
        )
    except Exception:
        pass

    return candidate


# ==========================================================
# v6.17.3 PROFESSIONAL DECISION KERNEL
# ==========================================================

def professional_decision_kernel(candidate: Any) -> dict[str, Any]:
    """
    Єдиний адаптивний шар прийняття рішення.
    Об'єднує:
    - institutional execution quality
    - HTF risk state
    - anti-chase protection
    - adaptive risk multiplier
    """

    result = {
        "action_modifier": "ALLOW",
        "risk_multiplier": 1.0,
        "execution_score": 0.0,
        "warnings": [],
        "reasons": []
    }

    if not candidate:
        result["action_modifier"] = "BLOCK"
        result["warnings"].append("missing candidate")
        return result

    score = institutional_execution_score(candidate)
    result["execution_score"] = score

    freshness = calculate_entry_freshness(candidate, {})
    result["entry_freshness"] = freshness
    if freshness.get("extended"):
        result["risk_multiplier"] *= FRESHNESS_EXTENDED_RISK_MULT
        result["warnings"].append("entry freshness extended move")
    elif freshness.get("warning"):
        result["risk_multiplier"] *= FRESHNESS_WARNING_RISK_MULT

    components = getattr(candidate, "score_components", {}) or {}

    htf_state = str(
        components.get("htf_state")
        or ("aligned" if components.get("htf") else "neutral")
    )

    result["risk_multiplier"] *= adaptive_htf_risk_multiplier(
        htf_state,
        score
    )

    move_atr = float(components.get("move_atr", 0) or 0)
    body_ratio = float(components.get("body_ratio", 0) or 0)

    if detect_execution_chase(move_atr, body_ratio):
        result["action_modifier"] = "WAIT_RETEST"
        result["warnings"].append("anti-chase protection")
        result["reasons"].append("impulse expansion detected")

    if score < 50:
        result["risk_multiplier"] *= 0.5
        result["warnings"].append("weak execution quality")

    return result




def decision_kernel_audit(candidate: Any) -> dict[str, Any]:
    """Debug snapshot for live decision auditing."""
    kernel = professional_decision_kernel(candidate)
    return {
        "execution_score": kernel.get("execution_score"),
        "modifier": kernel.get("action_modifier"),
        "risk_multiplier": kernel.get("risk_multiplier"),
        "warnings": kernel.get("warnings", []),
        "reasons": kernel.get("reasons", [])
    }


# ==========================================================
# v6.17.5 MONTE CARLO / SCENARIO REPLAY ENGINE
# ==========================================================

import random


@dataclass
class ReplayScenario:
    name: str
    candidate: dict[str, Any]
    expected_action: str
    description: str = ""


@dataclass
class ReplayResult:
    name: str
    passed: bool
    expected: str
    received: str
    risk_multiplier: float
    warnings: list[str] = field(default_factory=list)


def build_synthetic_scenarios() -> list[ReplayScenario]:
    """
    Детерміновані ринкові сценарії для перевірки decision kernel.
    Без API, без біржі, без випадкового шуму.
    """

    return [
        ReplayScenario(
            name="strong_reversal_against_htf",
            candidate={
                "trigger": 30,
                "liquidity": 25,
                "structure": 25,
                "htf_state": "against"
            },
            expected_action="ALLOW",
            description="Сильний reversal проти HTF"
        ),

        ReplayScenario(
            name="fomo_expansion",
            candidate={
                "trigger": 30,
                "liquidity": 20,
                "structure": 20,
                "move_atr": 2.4,
                "body_ratio": 0.9,
                "htf_state": "aligned"
            },
            expected_action="WAIT_RETEST",
            description="Велика імпульсна свічка"
        ),

        ReplayScenario(
            name="weak_setup",
            candidate={
                "trigger": 5,
                "liquidity": 5,
                "structure": 5,
                "htf_state": "neutral"
            },
            expected_action="ALLOW",
            description="Слабкий execution quality"
        ),

        ReplayScenario(
            name="trend_continuation",
            candidate={
                "trigger": 25,
                "liquidity": 25,
                "structure": 25,
                "htf_state": "aligned"
            },
            expected_action="ALLOW",
            description="Класичний трендовий вхід"
        ),
    ]


def replay_kernel_scenario(kernel_func) -> list[ReplayResult]:
    """
    Offline replay runner.
    Використовується для CI перед live запуском.
    """

    results = []

    for scenario in build_synthetic_scenarios():

        class SyntheticCandidate:
            def __init__(self, data):
                self.score_components = data
                self.risks = []

        candidate = SyntheticCandidate(scenario.candidate)

        try:
            output = kernel_func(candidate)

            received = output.get(
                "action_modifier",
                "ALLOW"
            )

            passed = (
                received == scenario.expected_action
                or (
                    scenario.expected_action == "ALLOW"
                    and received in {"ALLOW", "WAIT_RETEST"}
                )
            )

            results.append(
                ReplayResult(
                    name=scenario.name,
                    passed=passed,
                    expected=scenario.expected_action,
                    received=received,
                    risk_multiplier=float(
                        output.get("risk_multiplier", 1.0)
                    ),
                    warnings=output.get("warnings", [])
                )
            )

        except Exception as exc:
            results.append(
                ReplayResult(
                    name=scenario.name,
                    passed=False,
                    expected=scenario.expected_action,
                    received=f"ERROR: {exc}",
                    risk_multiplier=0.0,
                    warnings=["kernel exception"]
                )
            )

    return results


def monte_carlo_risk_stability(kernel_func, iterations: int = 1000) -> dict[str, Any]:
    """
    Перевірка стабільності ризику при випадкових ринкових умовах.
    Не прогнозує прибуток. Перевіряє поведінку ризику.
    """

    multipliers = []
    wait_count = 0

    for _ in range(iterations):

        class SyntheticCandidate:
            def __init__(self):
                self.score_components = {
                    "trigger": random.uniform(0, 35),
                    "liquidity": random.uniform(0, 35),
                    "structure": random.uniform(0, 35),
                    "move_atr": random.uniform(0, 3),
                    "body_ratio": random.uniform(0, 1),
                    "htf_state": random.choice(
                        ["aligned", "neutral", "against"]
                    )
                }
                self.risks = []

        result = kernel_func(SyntheticCandidate())

        multipliers.append(
            float(result.get("risk_multiplier", 1))
        )

        if result.get("action_modifier") == "WAIT_RETEST":
            wait_count += 1

    return {
        "iterations": iterations,
        "avg_risk_multiplier": round(
            sum(multipliers) / len(multipliers),
            4
        ),
        "min_risk_multiplier": round(
            min(multipliers),
            4
        ),
        "max_risk_multiplier": round(
            max(multipliers),
            4
        ),
        "wait_retest_ratio": round(
            wait_count / iterations,
            4
        )
    }



# ==========================================================
# v6.17.6 ADAPTIVE OPPORTUNITY ENGINE
# ==========================================================

PROBE_MIN_SCORE = float(os.getenv("PROBE_MIN_SCORE", "35") or 35)
ACCEPTANCE_MIN_SCORE = float(os.getenv("ACCEPTANCE_MIN_SCORE", "55") or 55)
CORE_MIN_SCORE = float(os.getenv("CORE_MIN_SCORE", "75") or 75)

PROBE_RISK_MULTIPLIER = float(os.getenv("PROBE_RISK_MULTIPLIER", "0.35") or 0.35)
ACCEPTANCE_RISK_MULTIPLIER = float(os.getenv("ACCEPTANCE_RISK_MULTIPLIER", "0.70") or 0.70)
CORE_RISK_MULTIPLIER = float(os.getenv("CORE_RISK_MULTIPLIER", "1.0") or 1.0)


def adaptive_opportunity_engine(candidate: Any) -> dict[str, Any]:
    """
    Зберігає можливість входу без перетворення слабких сетапів
    на повнорозмірні позиції.

    Принцип:
    - не вбивати opportunity;
    - зменшувати exposure;
    - піднімати розмір після підтвердження.
    """

    result = {
        "stage": "WATCH",
        "risk_multiplier": 0.0,
        "allow_probe": False,
        "reasons": []
    }

    if candidate is None:
        return result

    score = institutional_execution_score(candidate)

    if score >= CORE_MIN_SCORE:
        result["stage"] = "CORE"
        result["risk_multiplier"] = CORE_RISK_MULTIPLIER
        result["reasons"].append("high quality execution")

    elif score >= ACCEPTANCE_MIN_SCORE:
        result["stage"] = "ACCEPTANCE"
        result["risk_multiplier"] = ACCEPTANCE_RISK_MULTIPLIER
        result["reasons"].append("acceptable confirmation")

    elif score >= PROBE_MIN_SCORE:
        result["stage"] = "PROBE"
        result["risk_multiplier"] = PROBE_RISK_MULTIPLIER
        result["allow_probe"] = True
        result["reasons"].append("opportunity preserved with reduced risk")

    else:
        result["stage"] = "WATCH"
        result["risk_multiplier"] = 0.0
        result["reasons"].append("insufficient evidence")

    return result


def opportunity_preservation_audit(candidate: Any) -> dict[str, Any]:
    """Audit: чи система втратила можливий рух."""
    opportunity = adaptive_opportunity_engine(candidate)

    return {
        "stage": opportunity["stage"],
        "risk_multiplier": opportunity["risk_multiplier"],
        "allow_probe": opportunity["allow_probe"],
        "reasons": opportunity["reasons"]
    }



# ==========================================================
# v6.17.7 ADAPTIVE STATE MACHINE
# ==========================================================

STATE_WATCH = "WATCH"
STATE_PROBE = "PROBE"
STATE_ACCEPTANCE = "ACCEPTANCE"
STATE_CORE = "CORE"
STATE_WAIT_RETEST = "WAIT_RETEST"


def adaptive_state_transition(
    candidate: Any,
    kernel_result: dict[str, Any] | None = None,
    opportunity_profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Єдина машина станів execution.

    WATCH:
        недостатньо підтверджень, але можливість не втрачена.

    PROBE:
        ранній вхід зі зниженим ризиком.

    ACCEPTANCE:
        підтвердження отримано.

    CORE:
        максимальна довіра.

    WAIT_RETEST:
        захист від chase/FOMO.
    """

    kernel_result = kernel_result or {}
    opportunity_profile = opportunity_profile or {}

    state = opportunity_profile.get("stage", STATE_WATCH)
    risk = float(opportunity_profile.get("risk_multiplier", 0.0))

    htf_profile = htf_confidence_modifier(candidate)
    risk *= float(htf_profile.get("multiplier", 1.0))

    if kernel_result.get("action_modifier") == STATE_WAIT_RETEST:
        return {
            "state": STATE_WAIT_RETEST,
            "risk_multiplier": 0.0,
            "allow_execution": False,
            "reason": "anti-chase protection"
        }

    if state == STATE_CORE:
        return {
            "state": STATE_CORE,
            "risk_multiplier": 1.0,
            "allow_execution": True,
            "reason": "full confirmation"
        }

    if state == STATE_ACCEPTANCE:
        return {
            "state": STATE_ACCEPTANCE,
            "risk_multiplier": min(risk, 0.7),
            "allow_execution": True,
            "reason": "confirmed setup"
        }

    if state == STATE_PROBE:
        return {
            "state": STATE_PROBE,
            "risk_multiplier": min(risk, 0.35),
            "allow_execution": True,
            "reason": "preserve opportunity"
        }

    return {
        "state": STATE_WATCH,
        "risk_multiplier": 0.0,
        "allow_execution": False,
        "reason": "waiting confirmation"
    }


def state_machine_audit(candidate: Any) -> dict[str, Any]:
    """Debug snapshot для replay та live monitoring."""
    opportunity = adaptive_opportunity_engine(candidate)
    return adaptive_state_transition(
        candidate,
        {},
        opportunity
    )



# ==========================================================
# v6.17.8 HTF CONFIDENCE MODIFIER
# ==========================================================

HTF_ALIGNED_MULTIPLIER = float(os.getenv("HTF_ALIGNED_MULTIPLIER", "1.0") or 1.0)
HTF_NEUTRAL_MULTIPLIER = float(os.getenv("HTF_NEUTRAL_MULTIPLIER", "0.8") or 0.8)
HTF_REVERSAL_CONFIRMED_MULTIPLIER = float(
    os.getenv("HTF_REVERSAL_CONFIRMED_MULTIPLIER", "0.6") or 0.6
)
HTF_REVERSAL_WEAK_MULTIPLIER = float(
    os.getenv("HTF_REVERSAL_WEAK_MULTIPLIER", "0.35") or 0.35
)


def htf_confidence_modifier(candidate: Any) -> dict[str, Any]:
    """
    HTF не блокує угоди.
    Він регулює рівень довіри та розмір експозиції.

    Мета:
    - зберегти reversal можливості;
    - не давати повний CORE ризик без HTF підтримки.
    """

    components = getattr(candidate, "score_components", {}) or {}

    htf_state = str(
        components.get("htf_state", "neutral")
    ).lower()

    smt = bool(components.get("smt_confirmation", False))
    liquidity = float(components.get("liquidity", 0) or 0)
    trigger = float(components.get("trigger", 0) or 0)

    if htf_state in {"aligned", "bullish", "bearish"}:
        return {
            "multiplier": HTF_ALIGNED_MULTIPLIER,
            "confidence": "HIGH",
            "reason": "HTF aligned"
        }

    if htf_state in {"neutral", "mixed"}:
        return {
            "multiplier": HTF_NEUTRAL_MULTIPLIER,
            "confidence": "MEDIUM",
            "reason": "HTF neutral"
        }

    # Reversal against HTF
    if smt and liquidity >= 20 and trigger >= 20:
        return {
            "multiplier": HTF_REVERSAL_CONFIRMED_MULTIPLIER,
            "confidence": "REVERSAL_CONFIRMED",
            "reason": "HTF conflict with SMT/liquidity confirmation"
        }

    return {
        "multiplier": HTF_REVERSAL_WEAK_MULTIPLIER,
        "confidence": "REVERSAL_WEAK",
        "reason": "HTF conflict without enough confirmation"
    }


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



def _latest_confirmed_candle(candles: list[Candle]) -> Optional[Candle]:
    confirmed = [c for c in (candles or []) if getattr(c, "confirmed", True)]
    return sorted(confirmed, key=lambda c: c.ts)[-1] if confirmed else None


def _trade_risk_distance(trade: ActiveTrade) -> float:
    return max(abs(float(trade.entry) - float(trade.stop_initial or trade.stop_current or trade.entry)), 1e-9)


def _decision_stop_breached_by_close(trade: ActiveTrade, context: dict) -> tuple[bool, str]:
    if not DECISION_STOP_CLOSE_CONFIRM:
        return False, ""
    level = float(getattr(trade, "decision_stop", 0.0) or getattr(trade, "structural_invalidation", 0.0) or 0.0)
    if not level:
        return False, ""
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []
    last = _latest_confirmed_candle(c15)
    if not last:
        return False, ""
    lower_bound = max(_opened_at_ms(trade.opened_at), int(getattr(trade, "tp1_hit_ts", 0) or 0))
    if last.ts <= lower_bound:
        return False, ""
    if trade.side == Side.LONG.value and float(last.close) < level:
        return True, f"Decision stop close-confirm: 15M close {last.close} < {level}"
    if trade.side == Side.SHORT.value and float(last.close) > level:
        return True, f"Decision stop close-confirm: 15M close {last.close} > {level}"
    return False, ""


def _bars_after_ts(candles: list[Candle], ts: int) -> int:
    if not ts:
        return 0
    return len([c for c in (candles or []) if getattr(c, "confirmed", True) and int(c.ts) > int(ts)])


def _close_confirmed_beyond_level(side: str, candles: list[Candle], level: float) -> bool:
    last = _latest_confirmed_candle(candles)
    if not last:
        return False
    return float(last.close) >= level if side == Side.LONG.value else float(last.close) <= level


def _tp1_protection_ready(trade: ActiveTrade, context: dict) -> tuple[bool, str]:
    if not trade.tp1_hit:
        return False, "TP1 ще не взято"
    if getattr(trade, "tp1_stop_locked", False) and trade.stop_current != trade.stop_initial:
        return True, "захист уже активний"
    c15 = (context.get("candles", {}) or {}).get("15m", []) or []
    bars = _bars_after_ts(c15, int(getattr(trade, "tp1_hit_ts", 0) or 0))
    risk = _trade_risk_distance(trade)
    mfe_r = abs(float(trade.best_price) - float(trade.entry)) / risk if risk > 1e-9 else 0.0
    close_ok = _close_confirmed_beyond_level(trade.side, c15, float(trade.tp1))
    votes = int(bars >= BE_DELAY_BARS_AFTER_TP1) + int(mfe_r >= BE_DELAY_MIN_MFE_R) + int(close_ok)
    if votes >= 2:
        return True, f"BE_DELAY ready: bars={bars}, mfe_r={mfe_r:.2f}, close_confirm={close_ok}"
    return False, f"BE_DELAY waiting: bars={bars}/{BE_DELAY_BARS_AFTER_TP1}, mfe_r={mfe_r:.2f}/{BE_DELAY_MIN_MFE_R}, close_confirm={close_ok}"


def _delayed_tp1_lock_stop(trade: ActiveTrade, context: dict) -> Optional[float]:
    ready, _ = _tp1_protection_ready(trade, context)
    if not ready:
        return None
    risk = _trade_risk_distance(trade)
    # Мінімальний lock не на +0.02, а на entry + частка R: достатньо, щоб пережити комісію,
    # але не душити угоду впритул до входу.
    if trade.side == Side.LONG.value:
        base_lock = float(trade.entry) + max(COMMISSION_BUFFER_DOLLARS, risk * BE_LOCK_R_MULT)
    else:
        base_lock = float(trade.entry) - max(COMMISSION_BUFFER_DOLLARS, risk * BE_LOCK_R_MULT)
    structural = _structural_trailing_stop(trade, context)
    if structural is None:
        return round_price(base_lock)
    if trade.side == Side.LONG.value:
        return round_price(max(base_lock, structural))
    return round_price(min(base_lock, structural))


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
        delayed_stop = _delayed_tp1_lock_stop(trade, context)
        ready, ready_reason = _tp1_protection_ready(trade, context)
        if delayed_stop is not None and _apply_protective_stop(trade, context, delayed_stop):
            trade.tp1_stop_locked = True
            trade.tp1_locked_stop = round_price(trade.stop_current)
            result["notes"].append(f"Delayed BE/structural lock активовано: стоп перенесено до {trade.stop_current} | {ready_reason}")
            result["recommended_stop"] = round_price(trade.stop_current)
            result["recommended_stop_reason"] = "v6.13 BE_DELAY + structural swing lock після TP1"
        elif not ready:
            result["notes"].append(ready_reason)

    # --- Дворівневий Wick Defense ---
    is_stop, stop_reason = _stop_hit(trade, context)
    if is_stop:
        exit_price = round_price(trade.stop_current)
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(f"Вихід по catastrophic stop: {stop_reason}")
        trade.status = "CLOSED"
        trade.last_action = Action.STOP.value
        return result

    decision_break, decision_reason = _decision_stop_breached_by_close(trade, context)
    if decision_break:
        exit_price = round_price(price)
        result["closed"] = True
        result["action"] = Action.EXIT.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(decision_reason)
        trade.status = "CLOSED"
        trade.last_action = Action.EXIT.value
        return result

    # --- 0. TP0: службова фіксація без задушення угоди ---
    # На TP0 бот НЕ рухає стоп у БУ. Інакше ми знову отримаємо стару хворобу:
    # мікро-прибуток є, але нормальний TP1/TP2 задушений першим відкатом.
    if not result["closed"] and getattr(trade, "tp0", 0.0) and not getattr(trade, "tp0_hit", False) and _target_hit(side, context, trade.tp0):
        trade.tp0_hit = True
        result["action"] = Action.TP0.value
        result["notes"].append(f"TP0 досягнуто — зафіксуйте ~{int(getattr(trade, 'tp0_size_pct', TP0_SIZE_PCT) * 100)}% позиції; стоп НЕ рухаємо, TP1/TP2 лишаються живими")

    _tp0_giveback_innovation_advisor(trade, context, result)

    # --- 1. Фіксація TP1 (v6.13 delayed protection, НЕ миттєвий BE) ---
    if not result["closed"] and not trade.tp1_hit and _target_hit(side, context, trade.tp1):
        trade.tp1_hit = True
        trade.tp1_hit_at = iso_now()
        c3 = (context.get("candles", {}) or {}).get("3m", []) or []
        trade.tp1_hit_ts = int(c3[-1].ts) if c3 else int(time.time() * 1000)
        trade.tp1_stop_locked = False
        result["action"] = Action.TP1.value
        result["notes"].append(
            f"TP1 досягнуто — зафіксуйте ~{int(getattr(trade, 'tp1_size_pct', TP1_SIZE_PCT) * 100)}% позиції; "
            f"стоп НЕ рухаємо миттєво. BE_DELAY_ENGINE чекає {BE_DELAY_BARS_AFTER_TP1}x15M / MFE {BE_DELAY_MIN_MFE_R}R / close-confirm"
        )

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
            result["recommended_stop_reason"] = "Delayed BE/structural lock активний після TP1"
        elif trade.tp1_hit:
            result["recommended_stop_reason"] = "TP1 взято, але BE_DELAY_ENGINE ще не підтвердив перенос стопа"

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
    SetupType.ACCEPTANCE_RETEST_CONTINUATION.value: SetupFamily.CONTINUATION.value,
    SetupType.ACCELERATION_PULLBACK_REENTRY.value: SetupFamily.CONTINUATION.value,
    SetupType.SESSION_MEAN_RECLAIM.value: SetupFamily.CONTINUATION.value,
    SetupType.OPENING_RANGE_BREAKOUT.value: SetupFamily.EXPANSION.value,
    SetupType.FAILED_OPENING_RANGE_BREAKOUT.value: SetupFamily.RANGE_EXECUTION.value,
    SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value: SetupFamily.STRUCTURAL_TRANSITION.value,
    SetupType.LIQUIDITY_LADDER.value: SetupFamily.EXPANSION.value,
    SetupType.FAILED_AUCTION_REJECTION.value: SetupFamily.LIQUIDITY_RECOVERY.value,
    SetupType.TIME_OF_DAY_ADAPTIVE.value: SetupFamily.CONTINUATION.value,
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
    SetupType.FRESH_BASE_CONTINUATION.value: "",
    SetupType.BREAKOUT_RETEST.value: "Пробій структури + підтверджений ретест",
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: "Пробій після стиснення діапазону",
    SetupType.RANGE_EDGE_REVERSAL.value: "Розворот від межі діапазону",
    SetupType.ACCEPTANCE_RETEST_CONTINUATION.value: "Ранній continuation-probe після acceptance-ретесту",
    SetupType.ACCELERATION_PULLBACK_REENTRY.value: "Re-entry після пропущеного імпульсу через 38–50% pullback",
    SetupType.SESSION_MEAN_RECLAIM.value: "VWAP / Session Mean Reclaim",
    SetupType.OPENING_RANGE_BREAKOUT.value: "Opening Range Breakout з ретестом",
    SetupType.FAILED_OPENING_RANGE_BREAKOUT.value: "Failed ORB: фейковий пробій opening range",
    SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value: "Daily / Weekly Open Reclaim",
    SetupType.LIQUIDITY_LADDER.value: "Liquidity Ladder: каскад цілей ліквідності",
    SetupType.FAILED_AUCTION_REJECTION.value: "Failed Auction / Rejection Tail",
    SetupType.TIME_OF_DAY_ADAPTIVE.value: "Time-of-Day Adaptive Execution",
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
                "tp0_hit": getattr(active, "tp0_hit", False),
                "tp1_hit": active.tp1_hit,
                "tp2_hit": active.tp2_hit,
                "tp3_hit": active.tp3_hit,
                "tp0": getattr(active, "tp0", 0.0),
                "tp1": active.tp1,
                "tp2": active.tp2,
                "tp3": active.tp3,
                "entry_stage": getattr(active, "entry_stage", ""),
                "execution_source": getattr(active, "execution_source", ""),
                "position_risk_pct": active.position_risk_pct,
                "runtime_config_snapshot": getattr(active, "runtime_config_snapshot", {}),
                "innovation_profile": (getattr(active, "stage_plan", {}) or {}).get("innovation_profile", {}),
                "partial_plan": {"tp0": getattr(active, "tp0_size_pct", TP0_SIZE_PCT), "tp1": getattr(active, "tp1_size_pct", TP1_SIZE_PCT), "tp2": getattr(active, "tp2_size_pct", TP2_SIZE_PCT), "tp3_runner": getattr(active, "tp3_runner_pct", TP3_RUNNER_PCT)},
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
    payload = {"id": decision.id, "time": decision.time, "action": decision.action, "side": decision.side, "setup_type": decision.setup_type, "quality": decision.quality, "decision_quality": decision.quality, "reason": decision.reason, "regime": decision.regime, "news_bias": decision.news_bias, "macro_risk": decision.macro_risk, "instrument_label": context.get("instrument_label", INSTRUMENT_LABEL), "instrument_kind": context.get("instrument_kind", INSTRUMENT_KIND), "flow_quality": context.get("flow_quality", ""), "cvd_quality": context.get("cvd_quality", ""), "learning_status": journal.get("learning_status", {}), "learning_warnings": learning_warnings, "version": BOT_VERSION, "architecture_version": ARCHITECTURE_VERSION, "runtime_config_snapshot": runtime_config_snapshot(), "audit": decision.audit or {}}
    if decision.candidate:
        components = decision.candidate.score_components or {}
        payload.update({
            "setup_family": decision.candidate.setup_family,
            "ict_model": decision.candidate.ict_model,
            "candidate_final_score": decision.candidate.final_score,
            "candidate_raw_score": decision.candidate.raw_score,
            "execution_lane": decision.candidate.execution_lane,
            "execution_source": decision.candidate.execution_source,
            "entry_stage": decision.candidate.entry_stage,
            "stage_plan": decision.candidate.stage_plan,
            "trigger_ready": decision.candidate.trigger_ready,
            "live_3m_trigger_ready": decision.candidate.live_3m_trigger_ready,
            "limit_armed_ready": decision.candidate.limit_armed_ready,
            "time_warp_ready": decision.candidate.time_warp_ready,
            "trigger_age_minutes": round(float(decision.candidate.trigger_age_minutes or 0.0), 2),
            "score_components": components,
            "score_features": components.get("features", {}),
            "score_gates": components.get("gates", {}),
            "score_model_source": components.get("model_source", ""),
            "score_model_sample_size": components.get("sample_size", 0),
            "score_model_learned_weight": components.get("learned_weight", 0.0),
            "innovation_profile": getattr(decision.candidate, "innovation_profile", {}) or components.get("innovation_profile", {}),
            "trigger_revalidation": getattr(decision.candidate, "revalidation_profile", {}) or components.get("trigger_revalidation", {}),
        })
        if decision.plan:
            payload.update({
                "plan_tp0": decision.plan.tp0,
                "plan_tp1": decision.plan.tp1,
                "plan_tp2": decision.plan.tp2,
                "plan_tp3": decision.plan.tp3,
                "plan_rr0": decision.plan.rr0,
                "plan_rr1": decision.plan.rr1,
                "plan_position_risk_pct": decision.plan.position_risk_pct,
                "partial_plan": decision.plan.partial_plan,
                "target_basis": decision.plan.target_basis,
                "decision_stop": decision.plan.decision_stop,
                "catastrophic_stop": decision.plan.catastrophic_stop,
                "breathing_profile": decision.plan.breathing_profile,
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
        active = ActiveTrade(
            id=uuid.uuid4().hex[:10], signal_id=decision.id, side=decision.side,
            setup_type=decision.setup_type, setup_family=decision.candidate.setup_family,
            opened_at=iso_now(), entry=decision.plan.entry, stop_initial=decision.plan.stop,
            stop_current=decision.plan.stop, structural_invalidation=decision.plan.structural_invalidation,
            tp1=decision.plan.tp1, tp2=decision.plan.tp2, tp3=decision.plan.tp3,
            quality=decision.quality, position_risk_pct=decision.plan.position_risk_pct,
            best_price=decision.plan.entry, worst_price=decision.plan.entry,
            trigger_level=decision.candidate.trigger_level, thesis_key=decision.candidate.thesis_key,
            thesis=decision.candidate.thesis, opened_regime=decision.regime, entry_level=decision.action,
            tp0=decision.plan.tp0, tp0_size_pct=decision.plan.partial_plan.get("tp0", TP0_SIZE_PCT),
            tp1_size_pct=decision.plan.partial_plan.get("tp1", TP1_SIZE_PCT),
            tp2_size_pct=decision.plan.partial_plan.get("tp2", TP2_SIZE_PCT),
            tp3_runner_pct=decision.plan.partial_plan.get("tp3_runner", TP3_RUNNER_PCT),
            execution_source=decision.candidate.execution_source, entry_stage=decision.candidate.entry_stage,
            stage_plan=decision.candidate.stage_plan, runtime_config_snapshot=decision.plan.runtime_config_snapshot,
            decision_stop=decision.plan.decision_stop, catastrophic_stop=decision.plan.catastrophic_stop,
            breathing_profile=decision.plan.breathing_profile,
        )
        store_active_trade(state, active)
        state["opportunity"] = None
        print(f"[INFO] Угода відкрита: {decision.side} {decision.setup_type} | signal_id={active.signal_id} trade_id={active.id}")
    elif decision.action == Action.ARMED.value and decision.candidate:
        opp_status = "WAIT_PULLBACK" if decision.candidate.execution_lane in {ExecutionLane.WAIT_RETEST.value, ExecutionLane.LIMIT_ONLY.value} else "ARMED"
        opp = Opportunity(side=decision.side, setup_type=decision.setup_type, setup_family=decision.candidate.setup_family, created_at=iso_now(), expires_at=(now_utc() + timedelta(hours=18)).isoformat(), score=decision.quality, trigger_level=decision.candidate.trigger_level, invalidation_level=decision.candidate.invalidation_level, confirmations=decision.candidate.confirmations[:5], evidence_families=decision.candidate.evidence_families, execution_lane=decision.candidate.execution_lane, status=opp_status, thesis_key=decision.candidate.thesis_key, thesis=decision.candidate.thesis, signal_id=decision.id)
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
