#!/usr/bin/env python3
"""BZU Signal Bot v10.0.0 "ORGANIC".

Одна органічна архітектура замість 55 000 рядків нашарувань.

Принцип входу (єдиний для всіх 24 сетапів):

    РІВЕНЬ (anchor)  ->  РЕАКЦІЯ на 3m  ->  ВХІД РИНКОМ

Старий бот чекав закриття 15m-свічки як підтвердження і дозволяв вхід на
відстані до 3.75 ATR від рівня. При 15-хвилинному cron це давало запізнення
15-30 хв і вхід уже після того, як рух відбувся: 18 з 30 угод мали MFE < 0.2R
і помирали як NO_FOLLOWTHROUGH_EXIT.

Нова схема робить пізній вхід структурно неможливим:
  1. Детектор знаходить ПРИЧИНУ і фіксує її як anchor (рівень + інвалідація).
  2. Anchor арміться і живе кілька запусків, поки ціна не підійде.
  3. Вхід дозволено лише коли ціна ВПРИТУЛ до рівня (<= 0.35 ATR15) і на 3m
     вже є відбій з викидом + імпульсним корпусом у бік угоди.
  4. Вхід ринком, негайно, без очікування закриття 15m.
  5. Перевірка runway: найближча протилежна ціль має бути досить далеко, щоб
     0.25R MFE був реальний у вікні no-followthrough.

Супровід угод (manage_active_trade та вся підсистема TP0/TP1/TP2/TP3,
BE_DELAY_ENGINE, структурний трейлінг, PROBE no-followthrough, path-decay,
класична політика стопа v9.5.70) перенесено ДОСЛІВНО з попереднього бота —
без жодної зміни поведінки, лише розкладено з п'яти вкладених обгорток у
явний ланцюжок з одним визначенням на ім'я.

Джерело ціни (OKX v5 market API, TradingView scanner як display-only fallback)
також перенесено дослівно. TradingView ніколи не використовується для виконання.

Файли стану та журналу, імена, схеми записів і GitHub Actions workflow —
без змін: last_signal_v6_4.json, signal_journal_v6_4.json, bot_oneshot.py.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from statistics import mean
from typing import Any, Optional

try:
    import requests
except ImportError:  # Production-safe stdlib fallback for clean runners.
    class _StdlibResponse:
        def __init__(self, status_code: int, body: bytes):
            self.status_code = int(status_code)
            self.content = body
            self.text = body.decode("utf-8", errors="replace")

        def json(self) -> Any:
            return json.loads(self.text)

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}: {self.text[:300]}")

    class _StdlibRequests:
        Response = _StdlibResponse

        @staticmethod
        def get(url: str, headers: Optional[dict[str, str]] = None, timeout: int = 12) -> _StdlibResponse:
            request = urllib.request.Request(url, headers=headers or {}, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return _StdlibResponse(response.status, response.read())
            except urllib.error.HTTPError as exc:
                return _StdlibResponse(exc.code, exc.read())

        @staticmethod
        def post(
            url: str,
            headers: Optional[dict[str, str]] = None,
            json: Optional[dict[str, Any]] = None,
            timeout: int = 12,
        ) -> _StdlibResponse:
            body = globals()["json"].dumps(json or {}, ensure_ascii=False).encode("utf-8")
            merged_headers = {"Content-Type": "application/json", **(headers or {})}
            request = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return _StdlibResponse(response.status, response.read())
            except urllib.error.HTTPError as exc:
                return _StdlibResponse(exc.code, exc.read())

    requests = _StdlibRequests()


# ==========================================================
# IDENTITY
# ==========================================================

BOT_VERSION = "pro-organic-v10.0.0-anchor-reaction-market-entry"
ARCHITECTURE_VERSION = "ORGANIC_ANCHOR_REACTION_V10_0_0_15M_CADENCE"
INSTRUMENT_LABEL = "BZ/USDT"
SCHEMA_VERSION = "organic_v10.0.0"

WORKSPACE = Path(__file__).resolve().parent


def _flag(name: str, default: str = "true") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}


# ==========================================================
# CREDENTIALS & FILES  (імена файлів і змінних середовища незмінні)
# ==========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path(os.getenv("SIGNAL_MEMORY_FILE", str(WORKSPACE / "last_signal_v6_4.json")))
JOURNAL_FILE = Path(os.getenv("SIGNAL_JOURNAL_FILE", str(WORKSPACE / "signal_journal_v6_4.json")))

JOURNAL_PERSISTENCE_CONFIRMED = _flag("JOURNAL_PERSISTENCE_CONFIRMED", "")
JOURNAL_VERBOSE = _flag("JOURNAL_VERBOSE", "false")
JOURNAL_HYPOTHESIS_TOP = max(0, int(os.getenv("JOURNAL_HYPOTHESIS_TOP", "3") or 3))

SEND_NO_SETUP = _flag("SEND_NO_SETUP")
TELEGRAM_NOTIFY_EVERY_RUN = _flag("TELEGRAM_NOTIFY_EVERY_RUN")
TELEGRAM_MAX_LENGTH = max(600, min(4096, int(os.getenv("TELEGRAM_MAX_LENGTH", "4000") or 4000)))

DEFAULT_SIGNAL_JOURNAL_LIMIT = 500
MAX_JOURNAL = max(1, int(os.getenv("SIGNAL_JOURNAL_LIMIT", str(DEFAULT_SIGNAL_JOURNAL_LIMIT)) or DEFAULT_SIGNAL_JOURNAL_LIMIT))
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "200") or 200)
JOURNAL_VERSION = 3


# ==========================================================
# PERSISTENCE  ("Історія + очистка легасі")
# ==========================================================

# Training rows keep whatever numeric features they measured. A fixed key list
# would zero out the preserved legacy rows, so the bound is on size, not schema.
MAX_JOURNAL_FEATURE_KEYS = max(4, min(64, int(os.getenv("MAX_JOURNAL_FEATURE_KEYS", "24") or 24)))

# Anchors live across runs: the 15-minute cron must still catch a reaction that
# happened between two invocations.
STATE_ANCHOR_KEY = "anchors_v10"

# The journal keeps outcome history and nothing else. Every version-specific
# audit blob the old bot accumulated is dropped on save; atomic_json_write still
# leaves one .bak generation behind, so a prune is recoverable once.
JOURNAL_CORE_KEYS = frozenset({
    "version", "architecture_version", "journal_version", "updated_at",
    "trades", "signals", "training_signals", "signal_events",
    "preconfirmation_events",
    "analytics", "setup_statistics", "entry_quality_audit",
    "calendar_statistics", "learning_status", "degradation", "migration",
})

PRECONFIRM_EMBEDDED_JOURNAL_LIMIT = max(100, int(os.getenv("PRECONFIRM_EMBEDDED_JOURNAL_LIMIT", "500") or 500))
REJECTED_HYPOTHESIS_SHADOW_LIMIT = min(40, max(1, int(os.getenv("REJECTED_HYPOTHESIS_SHADOW_LIMIT", "40") or 40)))

PRECONFIRMATION_LAYER_ENABLED = _flag("PRECONFIRMATION_LAYER_ENABLED")
PRECONFIRM_TERMINAL_STATUSES = {"CONFIRMED", "FAILED", "EXPIRED"}
PRECONFIRM_VALID_STATUSES = {"PENDING", *PRECONFIRM_TERMINAL_STATUSES}
# PRECONFIRM_EXPIRED_LABEL_MODE is gone on purpose. Its only legal value was
# NEGATIVE — the legacy runtime audit refused to boot on anything else — and with
# NEGATIVE the legacy test `status == "FAILED" or (status == "EXPIRED" and mode ==
# "NEGATIVE")` collapses to `status in {"FAILED", "EXPIRED"}`, which is what
# _preconfirm_event_status and the unchanged supervision layer already compute.
# The supervision layer keeps the fast no-followthrough exit unless the linked
# event is CONFIRMED, so this fixed window decides how much lease a PROBE gets.
# Values are the legacy ones: supervision was calibrated against them.
PRECONFIRM_WINDOW_MINUTES = max(6, int(os.getenv("PRECONFIRM_WINDOW_MINUTES", "45") or 45))
PRECONFIRM_ACCEPTANCE_CLOSES = max(1, int(os.getenv("PRECONFIRM_ACCEPTANCE_CLOSES", "2") or 2))
# The resolver reads confirmed 3m candles; OKX timestamps are bar-open times.
PRECONFIRM_RESOLUTION_BAR_MS = 3 * 60_000
PRECONFIRM_CONFIRM_BUFFER_ATR = max(0.02, float(os.getenv("PRECONFIRM_CONFIRM_BUFFER_ATR", "0.15") or 0.15))
PRECONFIRM_INVALIDATION_BUFFER_ATR = max(0.20, float(os.getenv("PRECONFIRM_INVALIDATION_BUFFER_ATR", "0.80") or 0.80))
PRECONFIRM_EVENT_SCHEMA_VERSION = "organic_preconfirm_event_v10.0.0"
# Exactly the fields v10 writes: the 34 keys make_preconfirmation_event creates
# plus the 6 that _preconfirm_set_status adds on resolution. Events inherited from
# the legacy journal also carry ~7.5 KB each of removed-ML residue
# (estimate_at_observation, features, probability, ict_model, dedup_key, ...),
# which nothing in v10 reads and which save_journal rewrites every 15 minutes.
# If make_preconfirmation_event grows a key, add it here — the self-test fails otherwise.
PRECONFIRM_EVENT_KEEP_KEYS = frozenset({
    "event_id", "id", "thesis_key", "signal_id", "signal_link", "side",
    "setup_type", "setup_family", "canonical_setup_family", "event_kind",
    "model_family", "anchor_id", "anchor_kind", "entry_stage",
    "created_at", "created_ts", "observed_at", "observed_ts", "observation_candle_ts",
    "resolve_after_ts", "expires_ts", "resolution_window_minutes",
    "acceptance_required_closes", "observed_price", "atr15_at_observation",
    "anchor", "confirmation_level", "invalidation_level", "status", "outcome",
    "outcome_contract", "bot_version_at_observation",
    "architecture_version_at_observation", "schema_version",
    "resolution_reason", "resolved_ts", "resolved_at", "outcome_ts",
    "resolved_price", "resolution_evidence",
})

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)


# ==========================================================
# PRICE SOURCE  (джерело ціни не змінюється)
# ==========================================================

OKX_BASE_URL = "https://www.okx.com/api/v5/market"
TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com/crypto/scan"
OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
SMT_ASSET_ID_CONFIGURED = str(os.getenv("SMT_ASSET_ID", "CL-USDT-SWAP") or "CL-USDT-SWAP").strip().upper()
SMT_ASSET_ID_ALIASES = {"WTI-USDT-SWAP": "CL-USDT-SWAP"}
TRADINGVIEW_SYMBOLS = ("BINANCE:BZUSDT.P", "BINANCE:BZUSDT")


# ==========================================================
# DATA VOLUME
# ==========================================================

CANDLES_3M_LIMIT = max(120, min(300, int(os.getenv("CANDLES_3M_LIMIT", "240") or 240)))
CANDLES_15M_LIMIT = max(120, min(300, int(os.getenv("CANDLES_15M_LIMIT", "200") or 200)))
CANDLES_1H_LIMIT = max(80, min(300, int(os.getenv("CANDLES_1H_LIMIT", "160") or 160)))
CANDLES_4H_LIMIT = max(60, min(300, int(os.getenv("CANDLES_4H_LIMIT", "140") or 140)))
SMT_CANDLES_15M_LIMIT = max(80, min(300, int(os.getenv("SMT_CANDLES_15M_LIMIT", "200") or 200)))
HTF_SOURCE_15M_LIMIT = max(240, min(300, int(os.getenv("HTF_SOURCE_15M_LIMIT", "300") or 300)))
HTF_RESAMPLE_1H_MIN_BARS = max(10, int(os.getenv("HTF_RESAMPLE_1H_MIN_BARS", "20") or 20))
HTF_RESAMPLE_4H_MIN_BARS = max(8, int(os.getenv("HTF_RESAMPLE_4H_MIN_BARS", "14") or 14))
HTF_DATA_HEALTH_SCHEMA_VERSION = "htf_data_health_v10.0.0_direct_or_confirmed_15m_resample"


# ==========================================================
# RISK & POSITION SIZING  (PROBE/CORE модель збережена)
# ==========================================================

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "5") or 5)
NORMAL_RISK_PCT = float(os.getenv("NORMAL_RISK_PCT", "0.50") or 0.50)
DAILY_RISK_CAP = max(0.01, float(os.getenv("DAILY_RISK_CAP", "1.00") or 1.00))
RISK_BUDGET_MIN_BUFFER = max(0.0, float(os.getenv("RISK_BUDGET_MIN_BUFFER", "0.30") or 0.30))

PROBE_RISK_PCT = float(os.getenv("PROBE_RISK_PCT", "0.12") or 0.12)
HIGH_CONVICTION_PROBE_RISK_PCT = min(PROBE_RISK_PCT, max(0.01, float(os.getenv("HIGH_CONVICTION_PROBE_RISK_PCT", "0.12") or 0.12)))
MEDIUM_CONVICTION_PROBE_RISK_PCT = min(HIGH_CONVICTION_PROBE_RISK_PCT, max(0.01, float(os.getenv("MEDIUM_CONVICTION_PROBE_RISK_PCT", "0.07") or 0.07)))
EXPERIMENTAL_PROBE_RISK_PCT = min(MEDIUM_CONVICTION_PROBE_RISK_PCT, max(0.005, float(os.getenv("EXPERIMENTAL_PROBE_RISK_PCT", "0.03") or 0.03)))
ACCEPTANCE_RISK_PCT = float(os.getenv("ACCEPTANCE_RISK_PCT", "0.22") or 0.22)
CORE_RISK_PCT = float(os.getenv("CORE_RISK_PCT", str(NORMAL_RISK_PCT)) or NORMAL_RISK_PCT)
RISKY_GRAY_RISK_PCT = float(os.getenv("RISKY_GRAY_RISK_PCT", str(min(PROBE_RISK_PCT, 0.10))) or min(PROBE_RISK_PCT, 0.10))
UNDERPERFORMING_SETUP_EXPERIMENTAL_RISK_PCT = min(EXPERIMENTAL_PROBE_RISK_PCT, max(0.005, float(os.getenv("UNDERPERFORMING_SETUP_EXPERIMENTAL_RISK_PCT", "0.03") or 0.03)))
UNDERPERFORMING_SETUP_VALIDATION_PROBE_RISK_PCT = min(PROBE_RISK_PCT, max(UNDERPERFORMING_SETUP_EXPERIMENTAL_RISK_PCT, float(os.getenv("UNDERPERFORMING_SETUP_VALIDATION_PROBE_RISK_PCT", "0.07") or 0.07)))
ENTRY_QUALITY_LOW_RISK_MULT = min(1.0, max(0.05, float(os.getenv("ENTRY_QUALITY_LOW_RISK_MULT", "0.40") or 0.40)))
ENTRY_QUALITY_VERY_LOW_RISK_MULT = min(ENTRY_QUALITY_LOW_RISK_MULT, max(0.01, float(os.getenv("ENTRY_QUALITY_VERY_LOW_RISK_MULT", "0.25") or 0.25)))
BOOTSTRAP_RISK_MULTIPLIER = float(os.getenv("BOOTSTRAP_RISK_MULTIPLIER", "0.75") or 0.75)
WEAK_DIRECTION_RISK_MULTIPLIER = float(os.getenv("WEAK_DIRECTION_RISK_MULTIPLIER", "0.55") or 0.55)

ABS_MIN_STOP_DOLLARS = float(os.getenv("ABS_MIN_STOP_DOLLARS", "0.40") or 0.40)
COMMISSION_BUFFER_DOLLARS = float(os.getenv("COMMISSION_BUFFER_DOLLARS", "0.02") or 0.02)


# ==========================================================
# TRADE PLAN GEOMETRY  (супровід незмінний — ці пороги його частина)
# ==========================================================

TP0_RR = float(os.getenv("TP0_RR", "0.75") or 0.75)
TP0_SIZE_PCT = float(os.getenv("TP0_SIZE_PCT", "0.20") or 0.20)
TP1_SIZE_PCT = float(os.getenv("TP1_SIZE_PCT", "0.35") or 0.35)
TP2_SIZE_PCT = float(os.getenv("TP2_SIZE_PCT", "0.25") or 0.25)
TP3_RUNNER_PCT = round(max(0.0, 1.0 - TP0_SIZE_PCT - TP1_SIZE_PCT - TP2_SIZE_PCT), 6)
TP0_MIN_RR = float(os.getenv("TP0_MIN_RR", "1.00") or 1.00)
TP1_MIN_RR_PRO = float(os.getenv("TP1_MIN_RR_PRO", "2.00") or 2.00)
TP1_MIN_ATR_PRO = float(os.getenv("TP1_MIN_ATR_PRO", "3.00") or 3.00)
STOP_NOISE_PERCENTILE = float(os.getenv("STOP_NOISE_PERCENTILE", "0.70") or 0.70)
TP_NOISE_PERCENTILE = float(os.getenv("TP_NOISE_PERCENTILE", "0.85") or 0.85)
MIN_STOP_TRUE_RANGE_MULT = float(os.getenv("MIN_STOP_TRUE_RANGE_MULT", "1.10") or 1.10)
MIN_TP1_TRUE_RANGE_MULT = float(os.getenv("MIN_TP1_TRUE_RANGE_MULT", "1.25") or 1.25)
CURRENT_CANDLE_STOP_MULT = float(os.getenv("CURRENT_CANDLE_STOP_MULT", "0.85") or 0.85)
ABS_MIN_TP1_DOLLARS = float(os.getenv("ABS_MIN_TP1_DOLLARS", "0.65") or 0.65)
CATASTROPHIC_STOP_MULT = float(os.getenv("CATASTROPHIC_STOP_MULT", "1.25") or 1.25)
CATASTROPHIC_STOP_MAX_EXTRA_ATR = max(0.10, float(os.getenv("CATASTROPHIC_STOP_MAX_EXTRA_ATR", "0.45") or 0.45))
MIN_BREATHING_RISK_MULTIPLIER = float(os.getenv("MIN_BREATHING_RISK_MULTIPLIER", "0.35") or 0.35)
MIN_STOP_ATR15 = max(0.75, float(os.getenv("MIN_STOP_ATR15", "0.80") or 0.80))
MIN_TP1_ATR15 = max(0.90, float(os.getenv("MIN_TP1_ATR15", "1.15") or 1.15))
MIN_RR1 = max(1.50, float(os.getenv("MIN_RR1", "1.50") or 1.50))
PREFERRED_RR1 = max(1.60, float(os.getenv("PREFERRED_RR1", "1.60") or 1.60))
MIN_RR2 = max(2.50, float(os.getenv("MIN_RR2", "2.50") or 2.50))
MIN_RR3 = max(4.00, float(os.getenv("MIN_RR3", "4.00") or 4.00))
TP0_PROTECT_MIN_MFE_PCT = max(0.0, float(os.getenv("TP0_PROTECT_MIN_MFE_PCT", "0.0") or 0.0))
DECISION_STOP_CLOSE_CONFIRM = _flag("DECISION_STOP_CLOSE_CONFIRM")
RUNWAY_SCOUT_TP0_MIN_R = min(1.0, max(0.10, float(os.getenv("RUNWAY_SCOUT_TP0_MIN_R", "0.18") or 0.18)))
INNOVATION_GIVEBACK_WARN_RATIO = float(os.getenv("INNOVATION_GIVEBACK_WARN_RATIO", "0.62") or 0.62)


# ==========================================================
# PROFIT PROTECTION / BE DELAY  (супровід незмінний)
# ==========================================================

BE_DELAY_BARS_AFTER_TP1 = int(os.getenv("BE_DELAY_BARS_AFTER_TP1", "2") or 2)
BE_DELAY_MIN_MFE_R = float(os.getenv("BE_DELAY_MIN_MFE_R", "1.80") or 1.80)
BE_LOCK_R_MULT = float(os.getenv("BE_LOCK_R_MULT", "0.10") or 0.10)
TP0_PROTECT_ENABLED = _flag("TP0_PROTECT_ENABLED")
TP0_PROTECT_GIVEBACK_RATIO = min(0.95, max(0.10, float(os.getenv("TP0_PROTECT_GIVEBACK_RATIO", "0.50") or 0.50)))
PROBE_TP0_PROTECT_GIVEBACK_RATIO = min(
    TP0_PROTECT_GIVEBACK_RATIO,
    max(0.10, float(os.getenv("PROBE_TP0_PROTECT_GIVEBACK_RATIO", "0.40") or 0.40)),
)
TP0_PROTECT_RATCHET_EVIDENCE_LIMIT = max(20, int(os.getenv("TP0_PROTECT_RATCHET_EVIDENCE_LIMIT", "80") or 80))
TP0_PROTECT_MIN_RATCHET_STEP_R = max(0.02, float(os.getenv("TP0_PROTECT_MIN_RATCHET_STEP_R", "0.10") or 0.10))
TP0_PROTECT_PRICE_BUFFER_DOLLARS = max(0.001, float(os.getenv("TP0_PROTECT_PRICE_BUFFER_DOLLARS", "0.02") or 0.02))
SWING_LOOKBACK_15M = int(os.getenv("SWING_LOOKBACK_15M", "40") or 40)
SWING_PIVOT_STRENGTH = int(os.getenv("SWING_PIVOT_STRENGTH", "2") or 2)

PROBE_NO_FOLLOWTHROUGH_ENABLED = _flag("PROBE_NO_FOLLOWTHROUGH_ENABLED")
PROBE_NO_FOLLOWTHROUGH_MINUTES = max(30, int(os.getenv("PROBE_NO_FOLLOWTHROUGH_MINUTES", "45") or 45))
PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MINUTES = max(
    PROBE_NO_FOLLOWTHROUGH_MINUTES,
    int(os.getenv("PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MINUTES", "60") or 60),
)
PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R = min(0.50, max(0.05, float(os.getenv("PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R", "0.25") or 0.25)))
PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MAX_MFE_R = min(
    PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R,
    max(0.05, float(os.getenv("PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MAX_MFE_R", "0.20") or 0.20)),
)
CONFIRMED_PROBE_STALE_MINUTES = max(
    PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MINUTES,
    int(os.getenv("CONFIRMED_PROBE_STALE_MINUTES", "240") or 240),
)
CONFIRMED_PROBE_STALE_MAX_MFE_R = min(
    1.0,
    max(PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R, float(os.getenv("CONFIRMED_PROBE_STALE_MAX_MFE_R", "0.50") or 0.50)),
)
CONFIRMED_PROBE_STALE_MAX_CURRENT_R = min(
    0.50,
    float(os.getenv("CONFIRMED_PROBE_STALE_MAX_CURRENT_R", "0.00") or 0.00),
)
CONFIRMED_PROBE_MIN_PATH_INTEGRITY = min(
    100.0,
    max(0.0, float(os.getenv("CONFIRMED_PROBE_MIN_PATH_INTEGRITY", "45") or 45)),
)

PATH_DECAY_DEFENSE_ENABLED = _flag("PATH_DECAY_DEFENSE_ENABLED")
PATH_DECAY_MIN_MFE_R = min(1.0, max(0.45, float(os.getenv("PATH_DECAY_MIN_MFE_R", "0.65") or 0.65)))
PATH_DECAY_MIN_GIVEBACK = min(0.90, max(0.35, float(os.getenv("PATH_DECAY_MIN_GIVEBACK", "0.58") or 0.58)))

V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_ENABLED = _flag("V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_ENABLED")
V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MINUTES = max(30, int(os.getenv("V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MINUTES", "45") or 45))
V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_MFE_R = min(0.40, max(0.10, float(os.getenv("V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_MFE_R", "0.25") or 0.25)))
V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_CURRENT_R = min(0.0, max(-0.50, float(os.getenv("V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_CURRENT_R", "-0.10") or -0.10)))
V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_PATH_INTEGRITY = min(60.0, max(20.0, float(os.getenv("V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_PATH_INTEGRITY", "45") or 45)))

V9552_SCHEMA_VERSION = "directional_control_transfer_v9.5.52"
V9570_SCHEMA_VERSION = "classic_trade_management_restore_v9.5.70"
V9571_SCHEMA_VERSION = "causal_entry_quality_repair_v9.5.71"
ACCEPTANCE_NO_FOLLOWTHROUGH_SCHEMA_VERSION = "acceptance_no_followthrough_v9.5.35"


# ==========================================================
# EARLY ENTRY ENGINE  (нове ядро: anchor -> 3m reaction -> market)
# ==========================================================

# Вхід лише впритул до рівня. Стара логіка дозволяла 3.75 ATR — звідси пізні входи.
ANCHOR_MAX_ATR = min(1.50, max(0.10, float(os.getenv("ANCHOR_MAX_ATR", "0.35") or 0.35)))
# Скільки 3m-свічок шукаємо дотик до рівня (10 x 3m = 30 хв вікно реакції).
TRIGGER_LOOKBACK_3M = max(3, min(20, int(os.getenv("TRIGGER_LOOKBACK_3M", "10") or 10)))
# Мінімальна частка тіні/тіла, яка вважається відбоєм від рівня.
REACTION_REJECTION_RATIO = min(0.95, max(0.30, float(os.getenv("REACTION_REJECTION_RATIO", "0.55") or 0.55)))
# Імпульсний корпус 3m-свічки у бік угоди, в одиницях ATR3.
REACTION_BODY_ATR3 = max(0.10, float(os.getenv("REACTION_BODY_ATR3", "0.45") or 0.45))
# Буфер зони рівня (в ATR15), у якому дотик взагалі шукається.
ANCHOR_ZONE_ATR = min(1.00, max(0.05, float(os.getenv("ANCHOR_ZONE_ATR", "0.25") or 0.25)))
# Anchor старіє: причина, якій понад N хвилин, уже не причина.
ANCHOR_MAX_AGE_MIN = max(15, int(os.getenv("ANCHOR_MAX_AGE_MIN", "180") or 180))
# Максимальна відстань стопа від входу (ATR15) — інакше вхід уже запізний.
MAX_STOP_ATR = min(4.0, max(0.5, float(os.getenv("MAX_STOP_ATR", "1.60") or 1.60)))
# Runway: найближча протилежна ціль має давати хоча б стільки R,
# щоб 0.25R MFE був досяжний у вікні no-followthrough.
MIN_RUNWAY_R = max(0.5, float(os.getenv("MIN_RUNWAY_R", "1.60") or 1.60))
MIN_RUNWAY_ATR = max(0.3, float(os.getenv("MIN_RUNWAY_ATR", "0.90") or 0.90))
# Скільки anchor-ів одного сетапу тримаємо в стані.
ANCHOR_MEMORY_LIMIT = max(1, min(40, int(os.getenv("ANCHOR_MEMORY_LIMIT", "12") or 12)))
# Після спрацювання або відбою не перевхідимо рівень ще N хвилин.
ANCHOR_COOLDOWN_MIN = max(0, int(os.getenv("ANCHOR_COOLDOWN_MIN", "90") or 90))


# ==========================================================
# SETUP AUTO-DEGRADATION  ("Фокус + авто-деградація")
# ==========================================================

SETUP_STATS_MIN_SAMPLE = max(5, int(os.getenv("SETUP_STATS_MIN_SAMPLE", "12") or 12))
SETUP_DEMOTE_WINRATE_FLOOR = min(0.60, max(0.05, float(os.getenv("SETUP_DEMOTE_WINRATE_FLOOR", "0.28") or 0.28)))
SETUP_DEMOTE_EXPECTANCY_R = min(0.0, max(-1.0, float(os.getenv("SETUP_DEMOTE_EXPECTANCY_R", "-0.15") or -0.15)))
SETUP_PROMOTE_EXPECTANCY_R = max(0.0, float(os.getenv("SETUP_PROMOTE_EXPECTANCY_R", "0.25") or 0.25))
SETUP_PROMOTE_WINRATE_FLOOR = min(0.90, max(0.10, float(os.getenv("SETUP_PROMOTE_WINRATE_FLOOR", "0.35") or 0.35)))
SETUP_WILSON_Z = max(0.5, float(os.getenv("SETUP_WILSON_Z", "1.96") or 1.96))
SETUP_DEGRADATION_SCHEMA_VERSION = "setup_auto_degradation_v10.0.0"


# ==========================================================
# ADMISSION THRESHOLDS
# ==========================================================

MIN_SCORE_PROBE = max(1, min(100, int(os.getenv("MIN_SCORE_PROBE", "58") or 58)))
MIN_SCORE_CORE = max(MIN_SCORE_PROBE, min(100, int(os.getenv("MIN_SCORE_CORE", "72") or 72)))
MIN_HTF_ALIGNMENT_SCORE = max(0, min(100, int(os.getenv("MIN_HTF_ALIGNMENT_SCORE", "45") or 45)))
MAX_SPREAD_ATR = max(0.05, float(os.getenv("MAX_SPREAD_ATR", "0.25") or 0.25))
CORE_MIN_RISK_PCT_EFFECTIVE = max(0.01, float(os.getenv("CORE_MIN_RISK_PCT_EFFECTIVE", "0.10") or 0.10))
MIN_SCORE_ACCEPTANCE = max(MIN_SCORE_PROBE, min(MIN_SCORE_CORE, int(os.getenv("MIN_SCORE_ACCEPTANCE", "65") or 65)))
ENTRY_QUALITY_LOW = max(1, min(100, int(os.getenv("ENTRY_QUALITY_LOW", "55") or 55)))
ENTRY_QUALITY_VERY_LOW = max(1, min(ENTRY_QUALITY_LOW, int(os.getenv("ENTRY_QUALITY_VERY_LOW", "40") or 40)))
BOOTSTRAP_MIN_CLOSED_TRADES = max(5, int(os.getenv("BOOTSTRAP_MIN_CLOSED_TRADES", "20") or 20))
SETUP_TRADE_PROFILE_MIN_CLOSED_TRADES = max(20, int(os.getenv("SETUP_TRADE_PROFILE_MIN_CLOSED_TRADES", "20") or 20))
ROUTE_GEOMETRY_MIN_TRADES = max(6, int(os.getenv("ROUTE_GEOMETRY_MIN_TRADES", "8") or 8))
SETUP_MANAGEMENT_SCHEMA_V9542 = "setup_management_calibration_v9.5.42_past_only"
# ==========================================================
# ENUMS
# ==========================================================

class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class Action(str, Enum):
    """Execution / active-trade lifecycle actions only."""
    ENTRY = "ENTRY"
    RISKY_ENTRY = "RISKY_ENTRY"
    PROBE_ENTRY = "PROBE_ENTRY"
    NO_SETUP = "NO_SETUP"
    HOLD = "HOLD"
    PROTECT = "PROTECT"
    TP0 = "TP0"
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    STOP = "STOP"
    EXIT = "EXIT"


EXECUTABLE_ENTRY_ACTIONS = frozenset({
    Action.ENTRY.value, Action.RISKY_ENTRY.value, Action.PROBE_ENTRY.value,
})


class Regime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    SHOCK = "SHOCK"
    NORMAL = "NORMAL"


class SetupFamily(str, Enum):
    """Journal-compatible family labels (used in trades[].setup_family)."""
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
    MOMENTUM_NO_PULLBACK_CONTINUATION = "MOMENTUM_NO_PULLBACK_CONTINUATION"
    ACCELERATION_PULLBACK_REENTRY = "ACCELERATION_PULLBACK_REENTRY"
    SESSION_MEAN_RECLAIM = "SESSION_MEAN_RECLAIM"
    OPENING_RANGE_BREAKOUT = "OPENING_RANGE_BREAKOUT"
    FAILED_OPENING_RANGE_BREAKOUT = "FAILED_OPENING_RANGE_BREAKOUT"
    DAILY_WEEKLY_OPEN_RECLAIM = "DAILY_WEEKLY_OPEN_RECLAIM"
    LIQUIDITY_LADDER = "LIQUIDITY_LADDER"
    FAILED_AUCTION_REJECTION = "FAILED_AUCTION_REJECTION"
    TIME_OF_DAY_ADAPTIVE = "TIME_OF_DAY_ADAPTIVE"
    LIQUIDITY_SWEEP_REVERSAL_SHORT = "LIQUIDITY_SWEEP_REVERSAL_SHORT"
    FAILED_BREAKOUT_SHORT = "FAILED_BREAKOUT_SHORT"
    MSS_REVERSAL_SHORT = "MSS_REVERSAL_SHORT"
    BUYER_EXHAUSTION_SHORT = "BUYER_EXHAUSTION_SHORT"
    OR_FAILURE_2_SHORT = "OR_FAILURE_2_SHORT"
    NONE = "NONE"


class EntryStage(str, Enum):
    """The three sizes the unchanged supervision layer already understands."""
    PROBE = "PROBE"
    ACCEPTANCE = "ACCEPTANCE"
    CORE = "CORE"


class AnchorKind(str, Enum):
    """What the anchor level economically IS. Drives the reaction test."""
    SWEEP_LOW = "SWEEP_LOW"
    SWEEP_HIGH = "SWEEP_HIGH"
    DEMAND_ZONE = "DEMAND_ZONE"
    SUPPLY_ZONE = "SUPPLY_ZONE"
    FAIR_VALUE_GAP = "FAIR_VALUE_GAP"
    BREAK_LEVEL = "BREAK_LEVEL"
    RANGE_EDGE = "RANGE_EDGE"
    VALUE_LEVEL = "VALUE_LEVEL"
    STRUCTURE_SHIFT = "STRUCTURE_SHIFT"


class AnchorState(str, Enum):
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


# ==========================================================
# CANONICAL TAXONOMY: 24 setups in 6 economically distinct families
# ==========================================================

CANONICAL_SETUP_FAMILY_MAP: dict[str, str] = {
    # Liquidity event followed by rejection/recovery.
    "SWEEP_RECLAIM": "LIQUIDITY_REVERSAL",
    "CAPITULATION_RECOVERY": "LIQUIDITY_REVERSAL",
    "RANGE_EDGE_REVERSAL": "LIQUIDITY_REVERSAL",
    "FAILED_AUCTION_REJECTION": "LIQUIDITY_REVERSAL",
    "LIQUIDITY_SWEEP_REVERSAL_SHORT": "LIQUIDITY_REVERSAL",
    "BUYER_EXHAUSTION_SHORT": "LIQUIDITY_REVERSAL",
    # Existing directional auction resumes from value/structure.
    "PULLBACK_CONTINUATION": "TREND_CONTINUATION",
    "FRESH_BASE_CONTINUATION": "TREND_CONTINUATION",
    "ACCEPTANCE_RETEST_CONTINUATION": "TREND_CONTINUATION",
    "MOMENTUM_NO_PULLBACK_CONTINUATION": "TREND_CONTINUATION",
    "ACCELERATION_PULLBACK_REENTRY": "TREND_CONTINUATION",
    # New directional expansion or structural control transfer.
    "DIRECTION_FLIP_15M": "STRUCTURAL_EXPANSION",
    "TREND_IGNITION": "STRUCTURAL_EXPANSION",
    "BREAKOUT_RETEST": "STRUCTURAL_EXPANSION",
    "RANGE_COMPRESSION_BREAKOUT": "STRUCTURAL_EXPANSION",
    # Session/open auction expansion.
    "OPENING_RANGE_BREAKOUT": "SESSION_EXPANSION",
    "LIQUIDITY_LADDER": "SESSION_EXPANSION",
    # Failed expansion and reversal back through structure.
    "FAILED_OPENING_RANGE_BREAKOUT": "FAILED_EXPANSION",
    "FAILED_BREAKOUT_SHORT": "FAILED_EXPANSION",
    "MSS_REVERSAL_SHORT": "FAILED_EXPANSION",
    "OR_FAILURE_2_SHORT": "FAILED_EXPANSION",
    # Reclaim/rotation around session or higher-timeframe value.
    "SESSION_MEAN_RECLAIM": "VALUE_RECLAIM",
    "DAILY_WEEKLY_OPEN_RECLAIM": "VALUE_RECLAIM",
    "TIME_OF_DAY_ADAPTIVE": "VALUE_RECLAIM",
}

CANONICAL_FAMILIES = tuple(sorted(set(CANONICAL_SETUP_FAMILY_MAP.values())))
CANONICAL_FAMILY_SCHEMA_VERSION = "canonical_setup_families_v9.5.59"
if len(CANONICAL_SETUP_FAMILY_MAP) != 24 or len(CANONICAL_FAMILIES) != 6:
    raise RuntimeError("canonical family registry must cover 24 setups in six families")

# Canonical family -> journal-compatible SetupFamily label.
CANONICAL_TO_JOURNAL_FAMILY = {
    "LIQUIDITY_REVERSAL": SetupFamily.LIQUIDITY_RECOVERY.value,
    "TREND_CONTINUATION": SetupFamily.CONTINUATION.value,
    "STRUCTURAL_EXPANSION": SetupFamily.EXPANSION.value,
    "SESSION_EXPANSION": SetupFamily.EXPANSION.value,
    "FAILED_EXPANSION": SetupFamily.STRUCTURAL_TRANSITION.value,
    "VALUE_RECLAIM": SetupFamily.RANGE_EXECUTION.value,
}


def canonical_setup_family(setup_type: Any) -> str:
    """Return the one research/execution family for a named setup."""
    return CANONICAL_SETUP_FAMILY_MAP.get(str(setup_type or "").upper(), "UNKNOWN")


def journal_setup_family(setup_type: Any) -> str:
    """Return the SetupFamily label written to journal trades[].setup_family."""
    return CANONICAL_TO_JOURNAL_FAMILY.get(canonical_setup_family(setup_type), SetupFamily.NONE.value)


# ==========================================================
# DATA CLASSES
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
class Anchor:
    """A causal price level the market must react to before we may enter.

    The anchor is the ONLY thing a detector is allowed to produce. It carries
    the reason (level + kind), the falsifier (invalidation) and the direction.
    Entry authority belongs to the reaction engine, never to the detector.
    """
    id: str
    setup_type: str
    setup_family: str
    side: str
    level: float
    kind: str
    invalidation: float
    created_ts: int
    expires_ts: int
    reason: str
    score: float = 0.0
    htf_state: str = "UNKNOWN"
    regime: str = ""
    state: str = AnchorState.ARMED.value
    evidence: dict[str, Any] = field(default_factory=dict)
    target_levels: list[float] = field(default_factory=list)
    touched: bool = False
    touched_ts: int = 0
    last_checked_ts: int = 0
    cooldown_until_ts: int = 0
    reject_reason: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass
class Reaction:
    """Result of the 3m reaction test on one armed anchor."""
    anchor_id: str
    ready: bool
    entry_price: float
    reason: str
    gates: dict[str, Any] = field(default_factory=dict)
    latency_minutes: float = 0.0
    schema_version: str = SCHEMA_VERSION


@dataclass
class Candidate:
    """An anchor that passed the reaction test and is ready to be planned."""
    side: str
    setup_type: str
    setup_family: str
    raw_score: int
    final_score: int
    score_components: dict[str, Any] = field(default_factory=dict)
    confirmations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    trigger_ready: bool = False
    trigger_level: float = 0.0
    invalidation_level: float = 0.0
    target_levels: list[float] = field(default_factory=list)
    confirmation_tier: int = 3
    stage: str = "EXECUTABLE"
    execution_anchor: float = 0.0
    anchor_id: str = ""
    anchor_kind: str = ""
    thesis_key: str = ""
    thesis: str = ""
    execution_source: str = "ANCHOR_REACTION_3M"
    entry_stage: str = EntryStage.PROBE.value
    stage_plan: dict[str, Any] = field(default_factory=dict)
    risk_multiplier: float = 1.0
    setup_quality_score: int = 0
    timing_quality_score: int = 0
    entry_quality_score: int = 0
    durability_quality_score: int = 0
    trade_quality_score: int = 0
    htf_fact: dict[str, Any] = field(default_factory=dict)
    reaction: dict[str, Any] = field(default_factory=dict)
    admission: dict[str, Any] = field(default_factory=dict)
    entry_quality: int = 0
    entry_freshness_score: float = 100.0
    evidence_adjusted_selection_score: float = 0.0
    hypothesis_rank: int = 0
    competing_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    canonical_setup_family: str = ""
    family_episode_key: str = ""
    probe_conviction_tier: str = ""


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
    entry_stage: str = EntryStage.PROBE.value
    execution_source: str = "NONE"
    stage_plan: dict[str, Any] = field(default_factory=dict)
    partial_plan: dict[str, float] = field(default_factory=dict)
    runtime_config_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_stop: float = 0.0
    catastrophic_stop: float = 0.0
    breathing_profile: dict[str, Any] = field(default_factory=dict)
    valid: bool = True
    reason: str = ""
    risk_ledger: dict[str, Any] = field(default_factory=dict)
    final_stage: str = "EXECUTABLE"
    immutable: bool = False


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
    thesis_family_key: str = ""
    primary_signal_id: str = ""
    primary_signal_price: float = 0.0
    entry_delay_directional_delta: float = 0.0
    entry_delay_minutes: float = 0.0
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
    signal_id: str = ""
    tp0: float = 0.0
    tp0_hit: bool = False
    tp0_hit_at: str = ""
    tp0_hit_ts: int = 0
    tp0_size_pct: float = TP0_SIZE_PCT
    tp1_size_pct: float = TP1_SIZE_PCT
    tp2_size_pct: float = TP2_SIZE_PCT
    tp3_runner_pct: float = TP3_RUNNER_PCT
    execution_source: str = "NONE"
    entry_stage: str = EntryStage.PROBE.value
    stage_plan: dict[str, Any] = field(default_factory=dict)
    runtime_config_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_stop: float = 0.0
    catastrophic_stop: float = 0.0
    breathing_profile: dict[str, Any] = field(default_factory=dict)
    tp1_hit_at: str = ""
    tp1_hit_ts: int = 0
    tp1_close_confirmed: bool = False
    management_state: str = "SUPPORTED"
    pre_tp1_protection_locked: bool = False
    pre_tp1_protection_at: str = ""
    pre_tp1_protection_ratio: float = 0.0
    pre_tp1_protection_threshold: float = 0.0
    pre_tp1_protection_scope: str = ""
    protection_activation_mfe_r: float = 0.0
    protection_activation_current_r: float = 0.0
    protection_activation_stop: float = 0.0
    protection_peak_mfe_r: float = 0.0
    protection_locked_r: float = 0.0
    protection_ratchet_count: int = 0
    protection_last_ratchet_at: str = ""
    protection_last_ratchet_stop: float = 0.0
    protection_ratchet_missed_due_to_price: int = 0
    protection_last_evaluated_mfe_r: float = 0.0
    protection_ratchet_new_peak_events: int = 0
    protection_ratchet_evidence: list[dict[str, Any]] = field(default_factory=list)
    management_evidence_schema_version: str = "management_evidence_v9.5.20_multistep_ratchet_evidence"
    trade_profile_source: str = ""
    trade_profile_calibration_status: str = ""
    trade_profile_fallback_used: bool = False
    trade_profile_empirical_review_ready: bool = False
    trade_profile_schema_version: str = "trade_profile_provenance_v9.5.18"
    entry_quality: int = 0
    evaluation_entry_quality: float = 0.0
    preplan_entry_quality: float = 0.0
    trade_entry_quality: float = 0.0
    setup_quality: float = 0.0
    timing_quality: float = 0.0
    trade_quality: float = 0.0
    durability_quality: int = 0
    scoring_mode: str = "NOT_LEARNED"
    short_reversal_profile: dict[str, Any] = field(default_factory=dict)
    bot_version_at_entry: str = ""
    architecture_version_at_entry: str = ""
    journal_schema_at_entry: int = 0
    preconfirmation_event_id: str = ""
    entry_score_source: str = ""
    execution_tier: str = ""
    canonical_setup_family: str = ""
    family_episode_key: str = ""
    planned_entry: float = 0.0
    planned_stop: float = 0.0


# ==========================================================
# LABELS
# ==========================================================

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
    "": "НЕВИЗНАЧЕНИЙ",
}

SETUP_LABELS = {
    SetupType.SWEEP_RECLAIM.value: "Зняття ліквідності + повернення за рівень",
    SetupType.CAPITULATION_RECOVERY.value: "Відновлення після капітуляційного імпульсу",
    SetupType.DIRECTION_FLIP.value: "Підтверджена зміна напрямку на 15M",
    SetupType.TREND_IGNITION.value: "Запуск нового тренду",
    SetupType.PULLBACK_CONTINUATION.value: "Продовження тренду після ICT-відкату",
    SetupType.FRESH_BASE_CONTINUATION.value: "Продовження тренду від свіжої бази",
    SetupType.BREAKOUT_RETEST.value: "Пробій структури + підтверджений ретест",
    SetupType.RANGE_COMPRESSION_BREAKOUT.value: "Пробій після стиснення діапазону",
    SetupType.RANGE_EDGE_REVERSAL.value: "Розворот від межі діапазону",
    SetupType.ACCEPTANCE_RETEST_CONTINUATION.value: "Ранній continuation-probe після acceptance-ретесту",
    SetupType.MOMENTUM_NO_PULLBACK_CONTINUATION.value: "Продовження тренду без глибокого відкату від свіжої micro-base",
    SetupType.ACCELERATION_PULLBACK_REENTRY.value: "Re-entry після пропущеного імпульсу через 38–50% pullback",
    SetupType.SESSION_MEAN_RECLAIM.value: "VWAP / Session Mean Reclaim",
    SetupType.OPENING_RANGE_BREAKOUT.value: "Opening Range Breakout з ретестом",
    SetupType.FAILED_OPENING_RANGE_BREAKOUT.value: "Failed ORB: фейковий пробій opening range",
    SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value: "Daily / Weekly Open Reclaim",
    SetupType.LIQUIDITY_LADDER.value: "Liquidity Ladder: каскад цілей ліквідності",
    SetupType.FAILED_AUCTION_REJECTION.value: "Failed Auction / Rejection Tail",
    SetupType.TIME_OF_DAY_ADAPTIVE.value: "Time-of-Day Adaptive Execution",
    SetupType.LIQUIDITY_SWEEP_REVERSAL_SHORT.value: "SHORT: buy-side liquidity sweep + rejection",
    SetupType.FAILED_BREAKOUT_SHORT.value: "SHORT: failed breakout above resistance",
    SetupType.MSS_REVERSAL_SHORT.value: "SHORT: bearish Market Structure Shift",
    SetupType.BUYER_EXHAUSTION_SHORT.value: "SHORT: buyer exhaustion + bearish response",
    SetupType.OR_FAILURE_2_SHORT.value: "SHORT: Opening Range Failure 2.0",
    SetupType.NONE.value: "Професійного сетапу немає",
}


def setup_label(setup_type: Any) -> str:
    key = str(setup_type or "NONE").upper()
    return SETUP_LABELS.get(key, key.replace("_", " ").title())


def regime_label(regime: Any) -> str:
    return REGIME_LABELS.get(str(regime or "").upper(), str(regime or "НЕВИЗНАЧЕНИЙ"))


def side_word(side: Any) -> str:
    """Verbatim from the legacy bot: supervision messages render ЛОНГ/ШОРТ."""
    return {"LONG": "ЛОНГ", "SHORT": "ШОРТ"}.get(str(side or ""), "НЕЙТРАЛЬНО")
# ==========================================================
# UTILS
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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        result = int(float(value))
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


def _opposite_side(side: str) -> str:
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


def _fmt_price(v: Any) -> str:
    if v is None:
        return "-"
    return f"{float(v):.4f}".rstrip("0").rstrip(".")


def _parse_time_any(value: Any) -> Optional[datetime]:
    try:
        if not value:
            return None
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def minutes_since(value: Any) -> float:
    """Age in minutes of an ISO timestamp; +inf when unparsable."""
    parsed = _parse_time_any(value)
    if parsed is None:
        return float("inf")
    return max(0.0, (now_utc() - parsed).total_seconds() / 60.0)


def new_id(prefix: str) -> str:
    return f"{prefix}-{now_utc().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


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


def atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    payload = json_safe(data)
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp, path)
    with backup.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile of a sample; 0.0 when the sample is empty."""
    clean = sorted(safe_float(v) for v in values if v is not None)
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    idx = clamp(q, 0.0, 1.0) * (len(clean) - 1)
    low = int(math.floor(idx))
    high = int(math.ceil(idx))
    if low == high:
        return clean[low]
    return clean[low] + (clean[high] - clean[low]) * (idx - low)


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Lower edge of the Wilson score interval — sample-size aware win rate."""
    if n <= 0:
        return 0.0
    p = clamp(wins / n, 0.0, 1.0)
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return clamp((centre - spread) / denom, 0.0, 1.0)


# ==========================================================
# HTTP
# ==========================================================

def http_get(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = 2) -> Optional[requests.Response]:
    headers = {"User-Agent": "Mozilla/5.0 BZU-Pro-v6.6/1.0", "Accept": "*/*"}
    for attempt in range(max(1, retries)):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code < 400:
                return resp
        except Exception:
            pass
        time.sleep(0.3 * attempt)
    return None


def http_post(url: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> Optional[requests.Response]:
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Pro-v6.6/1.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_error = ""
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code < 400:
            return resp
        last_error = f"HTTP {resp.status_code}"
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
    if os.getenv("BZU_HTTP_DEBUG", "").lower() in {"1", "true", "yes"}:
        print(f"[HTTP POST unavailable] {url}: {last_error}")
    return None


# ==========================================================
# MARKET DATA  — джерело ціни перенесено ДОСЛІВНО
# ==========================================================

def get_okx_candles(inst_id: str = "BZ-USDT", bar: str = "15m", limit: int = 200) -> list[Candle]:
    url = f"{OKX_BASE_URL}/candles?instId={inst_id}&bar={bar}&limit={limit}"
    resp = http_get(url)
    if not resp:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    if data.get("code") != "0":
        return []
    out: list[Candle] = []
    for row in data.get("data", []):
        try:
            out.append(Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5] or 0),
                confirmed=(len(row) > 8 and str(row[8]) == "1"),
            ))
        except Exception:
            continue
    out.sort(key=lambda c: c.ts)
    return out


def get_okx_ticker(inst_id: str = "BZ-USDT") -> dict:
    url = f"{OKX_BASE_URL}/ticker?instId={inst_id}"
    resp = http_get(url)
    if not resp:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    if data.get("code") != "0":
        return {}
    rows = data.get("data") or []
    if not rows:
        return {}
    ticker = rows[0]
    last = safe_float(ticker.get("last"))
    open24h = safe_float(ticker.get("open24h"))
    if last <= 0:
        return {}
    return {
        "price": last,
        "change24h": pct(last, open24h),
        "volume24h": safe_float(ticker.get("volCcy24h")),
        "bid": safe_float(ticker.get("bidPx")),
        "ask": safe_float(ticker.get("askPx")),
        "ts": safe_int(ticker.get("ts")),
        "source": "OKX",
    }


def get_tradingview_price_fallback() -> dict:
    """Display-only cross-venue fallback. Never trusted for execution."""
    payload = {
        "symbols": {"tickers": list(TRADINGVIEW_SYMBOLS), "query": {"types": []}},
        "columns": ["close", "change", "volume"],
    }
    resp = http_post(TRADINGVIEW_SCAN_URL, payload)
    if not resp:
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    for row in (data.get("data") or []):
        values = row.get("s") or []
        symbol = str(row.get("s") or "")
        if not values:
            continue
        last = safe_float(values[0])
        if last <= 0:
            continue
        return {
            "price": last,
            "change24h": safe_float(values[1]) if len(values) > 1 else 0.0,
            "volume24h": safe_float(values[2]) if len(values) > 2 else 0.0,
            "source": "TradingView",
            "symbol": symbol,
        }
    return {}


def resolve_smt_asset_id() -> str:
    configured = str(SMT_ASSET_ID_CONFIGURED or "").strip().upper()
    return SMT_ASSET_ID_ALIASES.get(configured, configured)


def resample_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    """Aggregate confirmed lower-timeframe candles into a higher timeframe."""
    if minutes <= 0 or not candles:
        return []
    step_ms = minutes * 60 * 1000
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        if not getattr(c, "confirmed", True):
            continue
        buckets.setdefault((int(c.ts) // step_ms) * step_ms, []).append(c)
    out: list[Candle] = []
    for bucket_ts in sorted(buckets):
        rows = sorted(buckets[bucket_ts], key=lambda c: int(c.ts))
        complete = len(rows) >= max(1, minutes // 3)
        out.append(Candle(
            ts=bucket_ts,
            open=rows[0].open,
            high=max(r.high for r in rows),
            low=min(r.low for r in rows),
            close=rows[-1].close,
            volume=sum(r.volume for r in rows),
            confirmed=complete,
        ))
    return out


def fetch_timeframe(bar: str, direct_limit: int, resample_minutes: int = 0) -> tuple[list[Candle], str]:
    """Prefer the native OKX timeframe; fall back to a confirmed 15m resample."""
    rows = get_okx_candles(OKX_INST_ID, bar, direct_limit)
    if rows:
        return rows, f"OKX_DIRECT_{bar}"
    if resample_minutes <= 0:
        return [], "UNAVAILABLE"
    source = get_okx_candles(OKX_INST_ID, "15m", HTF_SOURCE_15M_LIMIT)
    if not source:
        return [], "UNAVAILABLE"
    resampled = resample_candles(source, resample_minutes)
    min_bars = HTF_RESAMPLE_1H_MIN_BARS if resample_minutes == 60 else HTF_RESAMPLE_4H_MIN_BARS
    if len(resampled) < min_bars:
        return [], f"RESAMPLE_INSUFFICIENT_{len(resampled)}"
    return resampled, f"RESAMPLE_15M_TO_{resample_minutes}M"


def collect_market_data() -> dict[str, Any]:
    """One snapshot of every timeframe the bot reasons about."""
    c3m = get_okx_candles(OKX_INST_ID, "3m", CANDLES_3M_LIMIT)
    c15m = get_okx_candles(OKX_INST_ID, "15m", CANDLES_15M_LIMIT)
    c1h, src1h = fetch_timeframe("1H", CANDLES_1H_LIMIT, 60)
    c4h, src4h = fetch_timeframe("4H", CANDLES_4H_LIMIT, 240)

    ticker = get_okx_ticker(OKX_INST_ID)
    price = safe_float(ticker.get("price"))
    price_source = "OKX_TICKER"
    execution_price_trusted = True
    execution_price_quality = "TRUSTED"

    if price <= 0 and c3m:
        price = safe_float(c3m[-1].close)
        price_source = "OKX_CANDLE_3M_CLOSE"
        execution_price_quality = "TRUSTED"

    if price <= 0:
        fallback = get_tradingview_price_fallback()
        price = safe_float(fallback.get("price"))
        if price > 0:
            price_source = "TRADINGVIEW_FALLBACK"
            # Cross-venue display price cannot authorize a real fill.
            execution_price_trusted = False
            execution_price_quality = "DISPLAY_ONLY_UNTRUSTED"

    smt_id = resolve_smt_asset_id()
    smt = get_okx_candles(smt_id, "15m", SMT_CANDLES_15M_LIMIT) if smt_id else []

    spread = 0.0
    bid = safe_float(ticker.get("bid"))
    ask = safe_float(ticker.get("ask"))
    if bid > 0 and ask > 0 and ask >= bid:
        spread = ask - bid

    return {
        "price": price,
        "price_source": price_source,
        "execution_price_trusted": execution_price_trusted,
        "execution_price_quality": execution_price_quality,
        "execution_venue": "OKX" if price_source.startswith("OKX") else "TRADINGVIEW",
        "change24h": safe_float(ticker.get("change24h")),
        "volume24h": safe_float(ticker.get("volume24h")),
        "ticker_ts": safe_int(ticker.get("ts")),
        "spread": spread,
        "candles": {"3m": c3m, "15m": c15m, "1H": c1h, "4H": c4h},
        "htf_source": {"1H": src1h, "4H": src4h},
        "smt_candles_15m": smt,
        "smt_asset_id": smt_id,
        "instrument": OKX_INST_ID,
        "instrument_label": INSTRUMENT_LABEL,
        "fetched_at": iso_now(),
    }
# ==========================================================
# INDICATORS
# ==========================================================

def true_ranges(candles: list[Candle]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        out.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return out


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = true_ranges(candles)
    return mean(trs[-period:]) if trs else 0.0


def atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    """Wilder-smoothed ATR history, aligned to the tail of `candles`."""
    trs = true_ranges(candles)
    if len(trs) < period:
        return []
    out = [mean(trs[:period])]
    for tr in trs[period:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def ema_values(closes: list[float], period: int) -> list[float]:
    if not closes or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [closes[0]]
    for close in closes[1:]:
        out.append(close * k + out[-1] * (1.0 - k))
    return out


def ema(candles: list[Candle], period: int) -> float:
    closes = [c.close for c in candles]
    values = ema_values(closes, period)
    return values[-1] if values else 0.0


def efficiency_ratio(candles: list[Candle], lookback: int = 20) -> float:
    """Kaufman efficiency: |net move| / sum of |bar moves|, 0..1."""
    rows = candles[-max(2, lookback):]
    if len(rows) < 2:
        return 0.0
    net = abs(rows[-1].close - rows[0].close)
    path = sum(abs(rows[i].close - rows[i - 1].close) for i in range(1, len(rows)))
    return clamp(net / max(path, 1e-9), 0.0, 1.0)


def cvd_snapshot(candles: list[Candle], lookback: int = 20) -> dict[str, Any]:
    """Close-location volume delta proxy (no tick data on this endpoint)."""
    rows = [c for c in candles[-max(2, lookback):] if c.volume > 0]
    if len(rows) < 3:
        return {"available": False, "delta": 0.0, "slope": 0.0, "bars": len(rows),
                "bias": Side.NEUTRAL.value, "strength": 0}
    running = 0.0
    total_volume = 0.0
    series: list[float] = []
    for c in rows:
        span = max(c.high - c.low, 1e-9)
        location = clamp(((c.close - c.low) - (c.high - c.close)) / span, -1.0, 1.0)
        running += location * c.volume
        total_volume += c.volume
        series.append(running)
    half = max(1, len(series) // 2)
    slope = (mean(series[-half:]) - mean(series[:half])) / max(abs(mean(series[:half])), 1e-9)
    delta = series[-1]
    delta_ratio = abs(delta) / total_volume if total_volume > 0 else 0.0
    strength = 2 if delta_ratio > 0.30 else 1 if delta_ratio > 0.15 else 0
    return {
        "available": True,
        "delta": round(delta, 2),
        "slope": round(clamp(slope, -5.0, 5.0), 4),
        "rising": bool(delta > series[0]),
        "bars": len(rows),
        "bias": (Side.LONG.value if delta > 0 else Side.SHORT.value) if strength else Side.NEUTRAL.value,
        "strength": strength,
    }


def vwap_session(candles: list[Candle], day_ts: int) -> float:
    """Session-anchored VWAP over confirmed candles of the current UTC day."""
    num = 0.0
    den = 0.0
    for c in candles:
        if int(c.ts) < day_ts or not getattr(c, "confirmed", True):
            continue
        typical = (c.high + c.low + c.close) / 3.0
        volume = max(c.volume, 0.0)
        num += typical * volume
        den += volume
    return num / den if den > 0 else 0.0


# ==========================================================
# SWING STRUCTURE
# ==========================================================

@dataclass
class SwingPoint:
    ts: int
    price: float
    kind: str          # HIGH | LOW
    strength: int
    index: int = 0


def swing_points(candles: list[Candle], lookback: int, strength: int) -> list[SwingPoint]:
    """Confirmed fractal pivots: `strength` bars on each side must be weaker."""
    rows = [c for c in candles[-max(strength * 2 + 3, lookback):] if getattr(c, "confirmed", True)]
    if len(rows) < strength * 2 + 1:
        return []
    out: list[SwingPoint] = []
    for i in range(strength, len(rows) - strength):
        window_h = [rows[j].high for j in range(i - strength, i + strength + 1)]
        window_l = [rows[j].low for j in range(i - strength, i + strength + 1)]
        if rows[i].high >= max(window_h):
            out.append(SwingPoint(ts=int(rows[i].ts), price=float(rows[i].high), kind="HIGH", strength=strength, index=i))
        if rows[i].low <= min(window_l):
            out.append(SwingPoint(ts=int(rows[i].ts), price=float(rows[i].low), kind="LOW", strength=strength, index=i))
    out.sort(key=lambda p: p.ts)
    return out


def dedupe_swings(points: list[SwingPoint]) -> list[SwingPoint]:
    """Keep strict HIGH/LOW alternation, extreme wins on repeats."""
    out: list[SwingPoint] = []
    for point in points:
        if out and out[-1].kind == point.kind:
            better = (point.price > out[-1].price) if point.kind == "HIGH" else (point.price < out[-1].price)
            if better:
                out[-1] = point
            continue
        out.append(point)
    return out


def structure_snapshot(candles: list[Candle], lookback: int = None, strength: int = None) -> dict[str, Any]:
    """Trend state from the last confirmed swing sequence on this timeframe."""
    lookback = lookback or SWING_LOOKBACK_15M
    strength = strength or SWING_PIVOT_STRENGTH
    pivots = dedupe_swings(swing_points(candles, lookback, strength))
    highs = [p for p in pivots if p.kind == "HIGH"]
    lows = [p for p in pivots if p.kind == "LOW"]
    direction = "NEUTRAL"
    hh_hl = lh_ll = False
    if len(highs) >= 2 and len(lows) >= 2:
        hh_hl = highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price
        lh_ll = highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price
        if hh_hl:
            direction = "LONG"
        elif lh_ll:
            direction = "SHORT"
    last_high = highs[-1].price if highs else 0.0
    last_low = lows[-1].price if lows else 0.0
    close = safe_float(candles[-1].close) if candles else 0.0
    span = max(last_high - last_low, 1e-9)
    position = clamp((close - last_low) / span, 0.0, 1.0) if last_high > 0 else 0.5
    return {
        "direction": direction,
        "hh_hl": hh_hl,
        "lh_ll": lh_ll,
        "last_swing_high": round(last_high, 6),
        "last_swing_low": round(last_low, 6),
        "swing_highs": [round(p.price, 6) for p in highs[-6:]],
        "swing_lows": [round(p.price, 6) for p in lows[-6:]],
        "pivot_high_ts": int(highs[-1].ts) if highs else 0,
        "pivot_low_ts": int(lows[-1].ts) if lows else 0,
        "range_position": round(position, 4),
        "pivot_count": len(pivots),
        "strength": strength,
    }


def detect_structure_shift(candles: list[Candle], strength: int = None) -> dict[str, Any]:
    """CHoCH/MSS: did the last confirmed close take out the prior pivot?"""
    strength = strength or SWING_PIVOT_STRENGTH
    pivots = dedupe_swings(swing_points(candles, SWING_LOOKBACK_15M, strength))
    rows = [c for c in candles if getattr(c, "confirmed", True)]
    if len(pivots) < 2 or len(rows) < 2:
        return {"shift": False, "side": Side.NEUTRAL.value, "level": 0.0, "age_bars": 0}
    last = rows[-1]
    highs = [p for p in pivots if p.kind == "HIGH"]
    lows = [p for p in pivots if p.kind == "LOW"]
    # A pivot broken by the newest bar is no longer the reference; use the one before.
    prior_high = next((p for p in reversed(highs) if p.ts < last.ts), None)
    prior_low = next((p for p in reversed(lows) if p.ts < last.ts), None)
    for bars_back, row in enumerate(reversed(rows[-6:])):
        if prior_high and row.close > prior_high.price:
            return {
                "shift": True, "side": Side.LONG.value, "level": round(prior_high.price, 6),
                "age_bars": bars_back, "kind": "CHoCH_UP", "pivot_ts": int(prior_high.ts),
            }
        if prior_low and row.close < prior_low.price:
            return {
                "shift": True, "side": Side.SHORT.value, "level": round(prior_low.price, 6),
                "age_bars": bars_back, "kind": "CHoCH_DOWN", "pivot_ts": int(prior_low.ts),
            }
    return {"shift": False, "side": Side.NEUTRAL.value, "level": 0.0, "age_bars": 0}


# ==========================================================
# ZONES: order blocks, fair value gaps, consolidation bases
# ==========================================================

def displacement_bars(candles: list[Candle], atr_value: float, min_body_atr: float = 1.10) -> list[int]:
    """Indexes of confirmed bars whose body is impulsive relative to ATR."""
    if atr_value <= 0:
        return []
    out = []
    for i, c in enumerate(candles):
        if not getattr(c, "confirmed", True):
            continue
        body = abs(c.close - c.open)
        if body >= min_body_atr * atr_value and (c.close > c.open) != (body <= 0):
            out.append(i)
    return out


def detect_order_blocks(candles: list[Candle], atr_value: float, limit: int = 8) -> list[Zone]:
    """Last opposite-colour bar before an impulsive displacement."""
    if atr_value <= 0 or len(candles) < 6:
        return []
    zones: list[Zone] = []
    for i in displacement_bars(candles, atr_value, 1.10):
        if i < 1:
            continue
        impulse = candles[i]
        prior = candles[i - 1]
        bullish = impulse.close > impulse.open
        if bullish and prior.close < prior.open:
            zones.append(Zone(
                kind="DEMAND_OB", side=Side.LONG.value, low=prior.low, high=prior.high,
                created_ts=int(prior.ts), timeframe="15m",
                strength=round(clamp(abs(impulse.close - impulse.open) / atr_value, 0.0, 4.0), 3),
            ))
        elif not bullish and prior.close > prior.open:
            zones.append(Zone(
                kind="SUPPLY_OB", side=Side.SHORT.value, low=prior.low, high=prior.high,
                created_ts=int(prior.ts), timeframe="15m",
                strength=round(clamp(abs(impulse.close - impulse.open) / atr_value, 0.0, 4.0), 3),
            ))
    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones[:limit]


def detect_fvg(candles: list[Candle], atr_value: float, limit: int = 8) -> list[Zone]:
    """Three-candle fair value gaps that price has not yet filled."""
    if atr_value <= 0 or len(candles) < 5:
        return []
    rows = [c for c in candles if getattr(c, "confirmed", True)]
    zones: list[Zone] = []
    for i in range(2, len(rows)):
        a, b, c = rows[i - 2], rows[i - 1], rows[i]
        gap_up = c.low - a.high
        gap_down = a.low - c.high
        if gap_up > 0.25 * atr_value:
            zones.append(Zone(
                kind="FVG_BULL", side=Side.LONG.value, low=a.high, high=c.low,
                created_ts=int(b.ts), timeframe="15m",
                strength=round(clamp(gap_up / atr_value, 0.0, 4.0), 3),
                mitigated=any(r.low <= a.high for r in rows[i + 1:]),
            ))
        if gap_down > 0.25 * atr_value:
            zones.append(Zone(
                kind="FVG_BEAR", side=Side.SHORT.value, low=c.high, high=a.low,
                created_ts=int(b.ts), timeframe="15m",
                strength=round(clamp(gap_down / atr_value, 0.0, 4.0), 3),
                mitigated=any(r.high >= a.low for r in rows[i + 1:]),
            ))
    unmitigated = [z for z in zones if not z.mitigated]
    unmitigated.sort(key=lambda z: (z.strength, z.created_ts), reverse=True)
    return unmitigated[:limit]


def detect_consolidation(candles: list[Candle], atr_value: float, bars: int = 12) -> dict[str, Any]:
    """Tight base: the launch pad for compression-breakout setups."""
    rows = [c for c in candles[-bars:] if getattr(c, "confirmed", True)]
    if len(rows) < max(4, bars // 2) or atr_value <= 0:
        return {"found": False, "low": 0.0, "high": 0.0, "compression": 0.0}
    low = min(r.low for r in rows)
    high = max(r.high for r in rows)
    compression = (high - low) / atr_value
    return {
        "found": bool(0 < compression <= 2.2),
        "low": round(low, 6),
        "high": round(high, 6),
        "mid": round((low + high) / 2.0, 6),
        "compression": round(compression, 3),
        "bars": len(rows),
        "start_ts": int(rows[0].ts),
    }


def detect_liquidity_pools(candles: list[Candle], atr_value: float, tolerance_atr: float = 0.15) -> dict[str, Any]:
    """Equal highs/lows: resting stops the market tends to raid."""
    pivots = dedupe_swings(swing_points(candles, SWING_LOOKBACK_15M, SWING_PIVOT_STRENGTH))
    if atr_value <= 0 or not pivots:
        return {"equal_highs": [], "equal_lows": [], "swept_highs": [], "swept_lows": []}
    tol = tolerance_atr * atr_value

    def cluster(points: list[SwingPoint]) -> list[dict[str, Any]]:
        groups: list[list[SwingPoint]] = []
        for point in sorted(points, key=lambda p: p.price):
            if groups and abs(point.price - groups[-1][-1].price) <= tol:
                groups[-1].append(point)
            else:
                groups.append([point])
        out = []
        for group in groups:
            if len(group) < 2:
                continue
            out.append({
                "level": round(mean(p.price for p in group), 6),
                "touches": len(group),
                "last_ts": max(int(p.ts) for p in group),
                "kind": group[0].kind,
            })
        return out

    highs = cluster([p for p in pivots if p.kind == "HIGH"])
    lows = cluster([p for p in pivots if p.kind == "LOW"])
    last_price = safe_float(candles[-1].close) if candles else 0.0
    recent_high = max((c.high for c in candles[-10:]), default=0.0)
    recent_low = min((c.low for c in candles[-10:]), default=0.0)
    return {
        "equal_highs": [h for h in highs if h["level"] > last_price],
        "equal_lows": [l for l in lows if l["level"] < last_price],
        "swept_highs": [h for h in highs if recent_high > h["level"] >= last_price - tol],
        "swept_lows": [l for l in lows if recent_low < l["level"] <= last_price + tol],
    }


def detect_sweep(candles: list[Candle], lookback: int = 20) -> dict[str, Any]:
    """Liquidity sweep: a wick takes a prior extreme, the close refuses it."""
    rows = [c for c in candles if getattr(c, "confirmed", True)]
    if len(rows) < lookback + 2:
        return {"sweep": False}
    prior = rows[-(lookback + 1):-1]
    last = rows[-1]
    prior_high = max(c.high for c in prior)
    prior_low = min(c.low for c in prior)
    span = max(last.high - last.low, 1e-9)
    if last.high > prior_high and last.close < prior_high:
        return {
            "sweep": True, "side": Side.SHORT.value, "level": round(prior_high, 6),
            "wick_ratio": round((last.high - max(last.open, last.close)) / span, 4),
            "ts": int(last.ts), "kind": "SWEEP_HIGH",
        }
    if last.low < prior_low and last.close > prior_low:
        return {
            "sweep": True, "side": Side.LONG.value, "level": round(prior_low, 6),
            "wick_ratio": round((min(last.open, last.close) - last.low) / span, 4),
            "ts": int(last.ts), "kind": "SWEEP_LOW",
        }
    return {"sweep": False}


# ==========================================================
# REGIME / SESSION
# ==========================================================

def detect_regime(candles15: list[Candle], candles1h: list[Candle]) -> dict[str, Any]:
    """TREND / RANGE / TRANSITION / SHOCK from efficiency and ATR expansion."""
    atr15 = atr(candles15, 14)
    atr_long = atr(candles15, 50) if len(candles15) > 51 else atr15
    er15 = efficiency_ratio(candles15, 20)
    er1h = efficiency_ratio(candles1h, 20) if candles1h else er15
    expansion = (atr15 / atr_long) if atr_long > 0 else 1.0
    if expansion >= 1.9:
        regime = Regime.SHOCK.value
    elif er15 >= 0.42 and er1h >= 0.34:
        regime = Regime.TREND.value
    elif er15 <= 0.22:
        regime = Regime.RANGE.value
    else:
        regime = Regime.TRANSITION.value
    bias = Side.NEUTRAL.value
    snap = structure_snapshot(candles15)
    if regime in {Regime.TREND.value, Regime.SHOCK.value}:
        bias = snap["direction"] if snap["direction"] != "NEUTRAL" else Side.NEUTRAL.value
    return {
        "regime": regime,
        "bias": bias,
        "efficiency_15m": round(er15, 4),
        "efficiency_1h": round(er1h, 4),
        "atr_expansion": round(expansion, 4),
        "atr15": round(atr15, 6),
        "structure": snap,
        "schema_version": SCHEMA_VERSION,
    }


def utc_day_start_ms(when: Optional[datetime] = None) -> int:
    moment = when or now_utc()
    return int(datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc).timestamp() * 1000)


def session_profile(candles15: list[Candle], when: Optional[datetime] = None) -> dict[str, Any]:
    """Session identity, opening range and daily/weekly reference levels."""
    moment = when or now_utc()
    hour = moment.hour
    # Names are load-bearing: the unchanged supervision layer widens protective
    # stop buffers specifically for "ASIA" and "OFF_HOURS".
    if 7 <= hour < 10:
        name = "LONDON_OPEN"
    elif 10 <= hour < 13:
        name = "LONDON"
    elif 13 <= hour < 17:
        name = "NY_OVERLAP"
    elif 17 <= hour < 21:
        name = "NY_PM"
    elif hour >= 23 or hour < 2:
        name = "OFF_HOURS"
    else:
        name = "ASIA"

    day_ts = utc_day_start_ms(moment)
    monday_ts = int((datetime.combine(
        (moment - timedelta(days=moment.weekday())).date(), datetime.min.time(), tzinfo=timezone.utc,
    )).timestamp() * 1000)

    rows = [c for c in candles15 if int(c.ts) >= day_ts]
    day_open = safe_float(rows[0].open) if rows else 0.0
    day_high = max((c.high for c in rows), default=0.0)
    day_low = min((c.low for c in rows), default=0.0)

    prev_rows = [c for c in candles15 if day_ts - 86_400_000 <= int(c.ts) < day_ts]
    prev_high = max((c.high for c in prev_rows), default=0.0)
    prev_low = min((c.low for c in prev_rows), default=0.0)

    week_rows = [c for c in candles15 if int(c.ts) >= monday_ts]
    week_open = safe_float(week_rows[0].open) if week_rows else 0.0
    week_high = max((c.high for c in week_rows), default=0.0)
    week_low = min((c.low for c in week_rows), default=0.0)

    # Opening range = the first four 15m bars of the UTC day.
    or_rows = rows[:4]
    or_high = max((c.high for c in or_rows), default=0.0)
    or_low = min((c.low for c in or_rows), default=0.0)

    return {
        "session": name,
        "hour_utc": hour,
        "day_ts": day_ts,
        "week_ts": monday_ts,
        "day_open": round(day_open, 6),
        "day_high": round(day_high, 6),
        "day_low": round(day_low, 6),
        "prev_day_high": round(prev_high, 6),
        "prev_day_low": round(prev_low, 6),
        "week_open": round(week_open, 6),
        "week_high": round(week_high, 6),
        "week_low": round(week_low, 6),
        "opening_range_high": round(or_high, 6),
        "opening_range_low": round(or_low, 6),
        "opening_range_complete": len(or_rows) >= 4,
        "schema_version": SCHEMA_VERSION,
    }


def smt_divergence_profile(candles15: list[Candle], smt_candles: list[Candle], atr_value: float) -> dict[str, Any]:
    """Confirm/refute a sweep using the correlated crude-oil instrument."""
    if not smt_candles or not candles15 or atr_value <= 0:
        return {"available": False, "divergence": "NONE"}

    def tail_extreme(rows: list[Candle], n: int) -> tuple[float, float]:
        window = rows[-n:]
        if not window:
            return 0.0, 0.0
        return max(c.high for c in window), min(c.low for c in window)

    bz_high, bz_low = tail_extreme(candles15, 10)
    smt_high, smt_low = tail_extreme(smt_candles, 10)
    bz_prior_high, bz_prior_low = tail_extreme(candles15[:-10] or candles15, 20)
    smt_prior_high, smt_prior_low = tail_extreme(smt_candles[:-10] or smt_candles, 20)

    bearish_div = bool(bz_high > bz_prior_high and smt_high <= smt_prior_high)
    bullish_div = bool(bz_low < bz_prior_low and smt_low >= smt_prior_low)
    divergence = "BEARISH" if bearish_div and not bullish_div else "BULLISH" if bullish_div and not bearish_div else "NONE"
    return {
        "available": True,
        "divergence": divergence,
        "supports_short": bearish_div,
        "supports_long": bullish_div,
        "smt_bars": len(smt_candles),
        "schema_version": SCHEMA_VERSION,
    }
# ==========================================================
# HTF ALIGNMENT
# ==========================================================

def htf_fact(context15: dict[str, Any], candles1h: list[Candle], candles4h: list[Candle]) -> dict[str, Any]:
    """One honest HTF verdict: ALIGNED / MIXED / AGAINST / UNKNOWN per side."""
    snap1h = structure_snapshot(candles1h) if candles1h else {"direction": "NEUTRAL", "pivot_count": 0}
    snap4h = structure_snapshot(candles4h) if candles4h else {"direction": "NEUTRAL", "pivot_count": 0}
    if snap1h["pivot_count"] < 2 or snap4h["pivot_count"] < 2:
        return {"state": "UNKNOWN", "direction_1h": snap1h["direction"], "direction_4h": snap4h["direction"], "score": 0}
    directions = [snap1h["direction"], snap4h["direction"]]
    if directions[0] == directions[1] and directions[0] != "NEUTRAL":
        return {"state": "ALIGNED", "direction_1h": directions[0], "direction_4h": directions[1], "score": 100}
    if "NEUTRAL" in directions:
        known = next((d for d in directions if d != "NEUTRAL"), "NEUTRAL")
        return {"state": "MIXED", "direction_1h": directions[0], "direction_4h": directions[1], "score": 55, "known_bias": known}
    return {"state": "AGAINST", "direction_1h": directions[0], "direction_4h": directions[1], "score": 0}


def htf_alignment_for_side(htf: dict[str, Any], side: str) -> dict[str, Any]:
    """Project the HTF verdict onto one candidate direction."""
    state = str(htf.get("state") or "UNKNOWN").upper()
    direction_1h = str(htf.get("direction_1h") or "NEUTRAL").upper()
    direction_4h = str(htf.get("direction_4h") or "NEUTRAL").upper()
    if state == "ALIGNED":
        aligned = direction_1h == side
        return {"state": "ALIGNED" if aligned else "AGAINST", "score": 100 if aligned else 0, "supports": aligned}
    if state == "AGAINST":
        return {"state": "AGAINST", "score": 0, "supports": False}
    if state == "MIXED":
        supports = side in {direction_1h, direction_4h}
        contradicts = (side == Side.LONG.value and Side.SHORT.value in {direction_1h, direction_4h}) or \
                      (side == Side.SHORT.value and Side.LONG.value in {direction_1h, direction_4h})
        if supports and not contradicts:
            return {"state": "MIXED", "score": 70, "supports": True}
        if contradicts and not supports:
            return {"state": "MIXED", "score": 25, "supports": False}
        return {"state": "MIXED", "score": 50, "supports": True}
    return {"state": "UNKNOWN", "score": 45, "supports": True}


# ==========================================================
# CONTEXT
# ==========================================================

def _effective_atr15(atr15: float, price: float) -> float:
    """
    Нижня межа ATR15 у абсолютних одиницях ціни. Без цього floor'у будь-яка
    формула виду `atr15 * коефіцієнт` (розмір стопа, TP1-floor, буфер гардів)
    схлопується до шуму в тихі періоди (Азія, низька ліквідність), навіть якщо
    технічний/структурний рівень поруч цілком реальний і ширший за цей шум.
    """
    atr_floor_pct = 0.0006  # 0.06% від ціни — абсолютний мінімум "дихання"
    return max(atr15, price * atr_floor_pct)


def build_context(data: dict[str, Any], state: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    """Everything one decision needs, computed once per run.

    ``state`` and ``journal`` are accepted for regime memory continuity and for
    the supervision layer's preconfirmation linkage; neither can widen an entry.
    """
    candles = dict(data.get("candles") or {})
    c3m = list(candles.get("3m") or [])
    c15m = list(candles.get("15m") or [])
    c1h = list(candles.get("1H") or [])
    c4h = list(candles.get("4H") or [])

    price = safe_float(data.get("price"))
    atr15 = _effective_atr15(atr(c15m, 14), price)
    atr3 = _effective_atr15(atr(c3m, 14), price)
    atr1h = atr(c1h, 14) if c1h else atr15

    regime_profile = detect_regime(c15m, c1h)
    session = session_profile(c15m)
    structure15 = regime_profile["structure"]
    structure3m = structure_snapshot(c3m, 40, SWING_PIVOT_STRENGTH)
    htf = htf_fact(structure15, c1h, c4h)

    zones = {
        "order_blocks": detect_order_blocks(c15m, atr15),
        "fvg": detect_fvg(c15m, atr15),
        "consolidation": detect_consolidation(c15m, atr15),
    }
    liquidity = detect_liquidity_pools(c15m, atr15)
    sweep = detect_sweep(c15m, 20)
    shift15 = detect_structure_shift(c15m)
    cvd = cvd_snapshot(c3m, 20)
    smt = smt_divergence_profile(c15m, list(data.get("smt_candles_15m") or []), atr15)

    vwap = vwap_session(c15m, session["day_ts"])
    spread = safe_float(data.get("spread"))
    spread_atr = (spread / atr15) if atr15 > 0 else 0.0

    regime_memory = dict(state.get("regime_memory") or {})
    regime_memory["last_regime"] = regime_profile["regime"]
    regime_memory["last_bias"] = regime_profile["bias"]
    regime_memory["updated_at"] = iso_now()

    context: dict[str, Any] = {
        # --- price & execution trust (незмінне джерело) ---
        "price": price,
        "price_source": data.get("price_source"),
        "execution_price_trusted": bool(data.get("execution_price_trusted")),
        "execution_price_quality": data.get("execution_price_quality"),
        "execution_venue": data.get("execution_venue"),
        "instrument": data.get("instrument"),
        "instrument_label": data.get("instrument_label") or INSTRUMENT_LABEL,
        "change24h": safe_float(data.get("change24h")),
        "volume24h": safe_float(data.get("volume24h")),
        "ticker_ts": safe_int(data.get("ticker_ts")),
        "spread": spread,
        "spread_atr": round(spread_atr, 4),

        # --- candles ---
        "candles": {"3m": c3m, "15m": c15m, "1H": c1h, "4H": c4h},
        "htf_source": dict(data.get("htf_source") or {}),
        "htf_data_health": {
            "1H": {"source": (data.get("htf_source") or {}).get("1H"), "bars": len(c1h),
                   "ok": bool(c1h and (len(c1h) >= HTF_RESAMPLE_1H_MIN_BARS or str((data.get("htf_source") or {}).get("1H", "")).startswith("OKX_DIRECT")))},
            "4H": {"source": (data.get("htf_source") or {}).get("4H"), "bars": len(c4h),
                   "ok": bool(c4h and (len(c4h) >= HTF_RESAMPLE_4H_MIN_BARS or str((data.get("htf_source") or {}).get("4H", "")).startswith("OKX_DIRECT")))},
            "schema_version": HTF_DATA_HEALTH_SCHEMA_VERSION,
        },

        # --- volatility ---
        "atr15": atr15,
        "atr3": atr3,
        "atr1h": atr1h,
        "effective_atr15": atr15,

        # --- structure & regime ---
        "regime": regime_profile["regime"],
        "regime_bias": regime_profile["bias"],
        "regime_profile": regime_profile,
        "structure15": structure15,
        "structure3m": structure3m,
        "structure_shift_15m": shift15,
        "htf_fact": htf,

        # --- session & macro levels ---
        "session_name": session["session"],
        "session": session,
        "vwap": round(vwap, 6),
        "day_open": session["day_open"],
        "prev_day_high": session["prev_day_high"],
        "prev_day_low": session["prev_day_low"],
        "week_open": session["week_open"],

        # --- liquidity map ---
        "zones": zones,
        "liquidity": liquidity,
        "sweep": sweep,
        "cvd": cvd,
        "smt": smt,

        # Per-timeframe bias views. The unchanged follow message reads
        # context["tf3"]["bias"] / context["tf15"]["bias"]; structure_snapshot
        # exposes the same fact as "direction".
        "tf3": {"bias": structure3m.get("direction", Side.NEUTRAL.value), "atr": atr3},
        "tf15": {"bias": structure15.get("direction", Side.NEUTRAL.value), "atr": atr15},

        # --- memory ---
        "regime_memory": regime_memory,
        "journal_closed_trades": len(list(journal.get("trades") or [])),

        # --- supervision coupling (заповнюється в run_bot) ---
        "preconfirmation_events": [],
        "fresh_opposite_execution": {},

        "built_at": iso_now(),
        "schema_version": SCHEMA_VERSION,
    }
    return context


def technical_targets(context: dict[str, Any], side: str, entry: float) -> list[dict[str, Any]]:
    """Every real level the market could travel to, nearest-first.

    Used for two things only: the runway feasibility gate before entry, and the
    audit-only nearest-liquidity note in the trade plan. TP1/TP2/TP3 stay
    governed by structural RR, exactly as in the unchanged supervision layer.
    """
    sign = side_sign(side)
    price = safe_float(context.get("price"), entry)
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    session = dict(context.get("session") or {})
    structure = dict(context.get("structure15") or {})
    liquidity = dict(context.get("liquidity") or {})
    zones = dict(context.get("zones") or {})
    out: list[dict[str, Any]] = []

    def add(kind: str, level: float) -> None:
        level = safe_float(level)
        distance = sign * (level - price)
        if level <= 0 or distance <= 0.10 * atr15:
            return
        out.append({"kind": kind, "level": round(level, 6), "distance": round(distance, 6), "distance_atr": round(distance / atr15, 3)})

    if side == Side.LONG.value:
        for level in structure.get("swing_highs") or []:
            add("SWING_HIGH", level)
        for pool in liquidity.get("equal_highs") or []:
            add("EQUAL_HIGHS", pool.get("level"))
        add("DAY_HIGH", session.get("day_high"))
        add("WEEK_HIGH", session.get("week_high"))
        add("PREV_DAY_HIGH", session.get("prev_day_high"))
        for zone in zones.get("order_blocks") or []:
            if zone.side == Side.SHORT.value:
                add("SUPPLY_OB", zone.low)
        for zone in zones.get("fvg") or []:
            if zone.side == Side.SHORT.value:
                add("FVG_BEAR", zone.low)
    else:
        for level in structure.get("swing_lows") or []:
            add("SWING_LOW", level)
        for pool in liquidity.get("equal_lows") or []:
            add("EQUAL_LOWS", pool.get("level"))
        add("DAY_LOW", session.get("day_low"))
        add("WEEK_LOW", session.get("week_low"))
        add("PREV_DAY_LOW", session.get("prev_day_low"))
        for zone in zones.get("order_blocks") or []:
            if zone.side == Side.LONG.value:
                add("DEMAND_OB", zone.high)
        for zone in zones.get("fvg") or []:
            if zone.side == Side.LONG.value:
                add("FVG_BULL", zone.high)

    out.sort(key=lambda row: row["distance"])
    return out


def nearest_runway_r(context: dict[str, Any], side: str, entry: float, risk: float) -> dict[str, Any]:
    """Can this trade realistically reach the MFE the supervision layer needs?

    PROBE no-followthrough exits at 45-60 minutes with MFE < 0.25R. If the
    nearest real opposing level is closer than MIN_RUNWAY_R, that 0.25R was
    never available and the entry is rejected before it can become a loss.
    """
    targets = technical_targets(context, side, entry)
    if not targets or risk <= 1e-9:
        return {"available": False, "runway_r": 0.0, "nearest": None, "targets": []}
    nearest = targets[0]
    runway_r = nearest["distance"] / risk
    return {
        "available": True,
        "runway_r": round(runway_r, 4),
        "nearest": nearest,
        "targets": targets[:6],
        "meets_min_r": bool(runway_r >= MIN_RUNWAY_R),
        "meets_min_atr": bool(nearest["distance_atr"] >= MIN_RUNWAY_ATR),
    }


def _v9532_recent_confirmed(context: dict[str, Any], timeframe: str, n: int) -> list[Candle]:
    """Last n confirmed candles of one timeframe, oldest first.

    The supervision layer resolves this name through globals() at call time, so
    it must exist under exactly this spelling.
    """
    rows = [c for c in ((context.get("candles") or {}).get(timeframe) or []) if getattr(c, "confirmed", True)]
    return sorted(rows, key=lambda c: int(getattr(c, "ts", 0) or 0))[-n:]


def _preconfirm_event_status(event: dict[str, Any]) -> str:
    """Read the canonical lifecycle status with v9.4 outcome compatibility."""
    raw = str((event or {}).get("status") or (event or {}).get("outcome") or "PENDING").upper()
    aliases = {"INVALIDATED": "FAILED", "AMBIGUOUS": "FAILED", "SUCCESS": "CONFIRMED"}
    status = aliases.get(raw, raw)
    return status if status in PRECONFIRM_VALID_STATUSES else "PENDING"
# ==========================================================
# ANCHOR FACTORY
# ==========================================================
# A detector may ONLY name a price level and its falsifier. It never decides
# that a trade happens. Entry authority belongs exclusively to the reaction
# engine, which requires price to be at the level and to have rejected it on
# the 3m chart. That separation is what makes a late entry impossible: a
# detector that fires after the move has already run produces an anchor the
# proximity gate will refuse.

def make_anchor(
    setup_type: str,
    side: str,
    level: float,
    invalidation: float,
    kind: str,
    reason: str,
    context: dict[str, Any],
    score: float = 0.0,
    evidence: Optional[dict[str, Any]] = None,
    ttl_minutes: int = 0,
) -> Optional[Anchor]:
    price = safe_float(context.get("price"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    level = round_price(level)
    invalidation = round_price(invalidation)
    sign = side_sign(side)
    if price <= 0 or level <= 0:
        return None
    # The falsifier must sit on the wrong side of the level, at least a quarter
    # ATR away. An anchor with a degenerate invalidation cannot be risk-managed.
    if sign * (invalidation - level) > -0.25 * atr15:
        invalidation = round_price(level - sign * max(0.75 * atr15, ABS_MIN_STOP_DOLLARS))
    ttl = ttl_minutes or ANCHOR_MAX_AGE_MIN
    now_ms = int(now_utc().timestamp() * 1000)
    return Anchor(
        id=new_id("anc"),
        setup_type=str(setup_type),
        setup_family=canonical_setup_family(setup_type),
        side=str(side),
        level=level,
        kind=str(kind),
        invalidation=invalidation,
        created_ts=now_ms,
        expires_ts=now_ms + ttl * 60 * 1000,
        reason=str(reason),
        score=round(clamp(safe_float(score), 0.0, 100.0), 2),
        htf_state=str((context.get("htf_fact") or {}).get("state") or "UNKNOWN"),
        regime=str(context.get("regime") or ""),
        evidence=dict(evidence or {}),
        schema_version=SCHEMA_VERSION,
    )


def _confirmed(candles: list[Candle], n: int) -> list[Candle]:
    return [c for c in candles if getattr(c, "confirmed", True)][-n:]


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _is_bull(c: Candle) -> bool:
    return c.close > c.open


def _range(c: Candle) -> float:
    return max(c.high - c.low, 1e-9)


def _close_location(c: Candle) -> float:
    """Where the close sits in the bar: 0.0 = at the low, 1.0 = at the high."""
    return clamp((c.close - c.low) / _range(c), 0.0, 1.0)


def _lower_wick_ratio(c: Candle) -> float:
    return (min(c.open, c.close) - c.low) / _range(c)


def _upper_wick_ratio(c: Candle) -> float:
    return (c.high - max(c.open, c.close)) / _range(c)


# ==========================================================
# FAMILY 1 — LIQUIDITY_REVERSAL
# A liquidity raid happens, the market refuses the new price.
# ==========================================================

def detect_sweep_reclaim(context: dict[str, Any]) -> Optional[Anchor]:
    """Prior low swept by a wick, close reclaimed back above it."""
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if len(c15) < 24:
        return None
    rows = _confirmed(c15, 21)
    last = rows[-1]
    prior_low = min(c.low for c in rows[:-1])
    if not (last.low < prior_low and last.close > prior_low):
        return None
    reclaim_strength = _close_location(last)
    if reclaim_strength < REACTION_REJECTION_RATIO:
        return None
    extreme = last.low
    return make_anchor(
        SetupType.SWEEP_RECLAIM.value, Side.LONG.value, prior_low,
        extreme - 0.15 * atr15, AnchorKind.SWEEP_LOW.value,
        f"Ліквідність під {prior_low:.4f} знята, закриття повернулось вище",
        context,
        score=58 + 22 * reclaim_strength + (8 if (context.get("smt") or {}).get("supports_long") else 0),
        evidence={
            "swept_level": round(prior_low, 6), "sweep_extreme": round(extreme, 6),
            "wick_ratio": round(_lower_wick_ratio(last), 4),
            "close_location": round(reclaim_strength, 4),
            "smt_divergence": (context.get("smt") or {}).get("divergence", "NONE"),
        },
    )


def detect_capitulation_recovery(context: dict[str, Any]) -> Optional[Anchor]:
    """A high-volume capitulation cluster on 3m marks a level worth defending."""
    c3 = list((context.get("candles") or {}).get("3m") or [])
    atr3 = max(safe_float(context.get("atr3"), 0.0), 1e-9)
    if len(c3) < 20:
        return None
    rows = _confirmed(c3, 18)
    volumes = [c.volume for c in rows if c.volume > 0]
    if len(volumes) < 8:
        return None
    volume_median = mean(volumes)
    cluster = [c for c in rows if c.volume >= 1.9 * volume_median and _range(c) >= 1.6 * atr3]
    if len(cluster) < 2:
        return None
    low = min(c.low for c in cluster)
    last = rows[-1]
    if last.close <= low:
        return None
    return make_anchor(
        SetupType.CAPITULATION_RECOVERY.value, Side.LONG.value, low,
        low - 0.6 * atr3, AnchorKind.DEMAND_ZONE.value,
        f"Капітуляція на обсягах сформувала низ {low:.4f}",
        context,
        score=54 + min(18, 4 * len(cluster)),
        evidence={"cluster_bars": len(cluster), "capitulation_low": round(low, 6),
                  "volume_multiple": round(max(c.volume for c in cluster) / max(volume_median, 1e-9), 2)},
    )


def detect_range_edge_reversal(context: dict[str, Any]) -> Optional[Anchor]:
    """Established range: fade the edge, not the middle."""
    if str(context.get("regime")) not in {Regime.RANGE.value, Regime.TRANSITION.value}:
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    base = (context.get("zones") or {}).get("consolidation") or {}
    session = dict(context.get("session") or {})
    price = safe_float(context.get("price"))
    if len(c15) < 30 or price <= 0 or atr15 <= 0:
        return None
    low = safe_float(base.get("low")) or safe_float(session.get("day_low"))
    high = safe_float(base.get("high")) or safe_float(session.get("day_high"))
    if low <= 0 or high <= 0 or (high - low) < 2.0 * atr15:
        return None
    if abs(price - low) <= 0.60 * atr15:
        compression = safe_float(base.get("compression"), 2.2)
        return make_anchor(
            SetupType.RANGE_EDGE_REVERSAL.value, Side.LONG.value, low,
            low - 0.55 * atr15, AnchorKind.RANGE_EDGE.value,
            f"Нижня межа діапазону {low:.4f}",
            context, score=52 + 12 * clamp((2.2 - compression) / 2.2, 0.0, 1.0),
            evidence={"range_low": round(low, 6), "range_high": round(high, 6),
                      "range_width_atr": round((high - low) / atr15, 3)},
        )
    if abs(price - high) <= 0.60 * atr15:
        return make_anchor(
            SetupType.RANGE_EDGE_REVERSAL.value, Side.SHORT.value, high,
            high + 0.55 * atr15, AnchorKind.RANGE_EDGE.value,
            f"Верхня межа діапазону {high:.4f}",
            context, score=52,
            evidence={"range_low": round(low, 6), "range_high": round(high, 6),
                      "range_width_atr": round((high - low) / atr15, 3)},
        )
    return None


def detect_failed_auction_rejection(context: dict[str, Any]) -> Optional[Anchor]:
    """Auction pushed outside value and was immediately rejected back in."""
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if len(c15) < 12:
        return None
    rows = _confirmed(c15, 8)
    last = rows[-1]
    session = dict(context.get("session") or {})
    vwap = safe_float(context.get("vwap"))
    for level, side, kind in (
        (safe_float(session.get("prev_day_high")), Side.SHORT.value, AnchorKind.SWEEP_HIGH.value),
        (safe_float(session.get("prev_day_low")), Side.LONG.value, AnchorKind.SWEEP_LOW.value),
        (safe_float(session.get("week_high")), Side.SHORT.value, AnchorKind.SWEEP_HIGH.value),
        (safe_float(session.get("week_low")), Side.LONG.value, AnchorKind.SWEEP_LOW.value),
        (vwap, Side.LONG.value if last.close > vwap else Side.SHORT.value, AnchorKind.VALUE_LEVEL.value),
    ):
        if level <= 0:
            continue
        rejected_high = side == Side.SHORT.value and last.high > level and last.close < level and _upper_wick_ratio(last) >= REACTION_REJECTION_RATIO
        rejected_low = side == Side.LONG.value and last.low < level and last.close > level and _lower_wick_ratio(last) >= REACTION_REJECTION_RATIO
        if rejected_high:
            return make_anchor(
                SetupType.FAILED_AUCTION_REJECTION.value, Side.SHORT.value, level,
                last.high + 0.15 * atr15, kind,
                f"Аукціон відкинуто від {level:.4f}", context, score=60,
                evidence={"rejected_level": round(level, 6), "wick_ratio": round(_upper_wick_ratio(last), 4)},
            )
        if rejected_low:
            return make_anchor(
                SetupType.FAILED_AUCTION_REJECTION.value, Side.LONG.value, level,
                last.low - 0.15 * atr15, kind,
                f"Аукціон відкинуто від {level:.4f}", context, score=60,
                evidence={"rejected_level": round(level, 6), "wick_ratio": round(_lower_wick_ratio(last), 4)},
            )
    return None


def detect_liquidity_sweep_reversal_short(context: dict[str, Any]) -> Optional[Anchor]:
    """Equal highs raided and refused — resting buy-stops did not hold price."""
    sweep = dict(context.get("sweep") or {})
    liquidity = dict(context.get("liquidity") or {})
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if not sweep.get("sweep") or sweep.get("kind") != "SWEEP_HIGH":
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    rows = _confirmed(c15, 2)
    if not rows:
        return None
    extreme = max(c.high for c in rows)
    equal_highs = [p for p in (liquidity.get("equal_highs") or []) if safe_float(p.get("level")) > 0]
    bonus = 10 if equal_highs else 0
    return make_anchor(
        SetupType.LIQUIDITY_SWEEP_REVERSAL_SHORT.value, Side.SHORT.value,
        safe_float(sweep.get("level")), extreme + 0.15 * atr15, AnchorKind.SWEEP_HIGH.value,
        f"Зняття ліквідності над {safe_float(sweep.get('level')):.4f} без прийняття",
        context,
        score=58 + bonus + (10 if (context.get("smt") or {}).get("supports_short") else 0),
        evidence={"swept_level": safe_float(sweep.get("level")), "sweep_extreme": round(extreme, 6),
                  "wick_ratio": safe_float(sweep.get("wick_ratio")), "equal_high_pools": len(equal_highs)},
    )


def detect_buyer_exhaustion_short(context: dict[str, Any]) -> Optional[Anchor]:
    """Uptrend whose delta and bar bodies are both rolling over at the highs."""
    c15 = list((context.get("candles") or {}).get("15m") or [])
    c3 = list((context.get("candles") or {}).get("3m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    structure = dict(context.get("structure15") or {})
    cvd = dict(context.get("cvd") or {})
    if len(c15) < 30 or structure.get("direction") != Side.LONG.value:
        return None
    rows = _confirmed(c15, 12)
    bodies = [_body(c) / atr15 for c in rows]
    if len(bodies) < 8:
        return None
    late = mean(bodies[-4:])
    early = mean(bodies[:4])
    contraction = late < 0.62 * early
    delta_rolling = bool(cvd.get("available")) and not cvd.get("rising", True)
    wicks = [_upper_wick_ratio(c) for c in rows[-5:]]
    rejection = mean(wicks) >= 0.34 if wicks else False
    if not (contraction and (delta_rolling or rejection)):
        return None
    anchor_level = safe_float(structure.get("last_swing_low"))
    if anchor_level <= 0:
        anchor_level = min(c.low for c in rows[-4:])
    return make_anchor(
        SetupType.BUYER_EXHAUSTION_SHORT.value, Side.SHORT.value, anchor_level,
        max(c.high for c in rows[-6:]) + 0.30 * atr15, AnchorKind.SUPPLY_ZONE.value,
        "Виснаження покупця: корпуси стискаються, дельта не підтверджує",
        context,
        score=56 + (10 if delta_rolling else 0) + (8 if rejection else 0),
        evidence={"body_contraction": round(late / max(early, 1e-9), 3),
                  "cvd_rising": bool(cvd.get("rising")), "mean_upper_wick": round(mean(wicks) if wicks else 0.0, 3)},
    )


# ==========================================================
# FAMILY 2 — TREND_CONTINUATION
# The existing auction resumes from a level it already defended.
# ==========================================================

def _trend_side(context: dict[str, Any]) -> str:
    structure = dict(context.get("structure15") or {})
    direction = str(structure.get("direction") or "NEUTRAL").upper()
    if direction in {Side.LONG.value, Side.SHORT.value}:
        return direction
    return str(context.get("regime_bias") or Side.NEUTRAL.value).upper()


def _ema_ladder(candles: list[Candle]) -> dict[str, float]:
    return {"ema20": ema(candles, 20), "ema50": ema(candles, 50), "ema200": ema(candles, 200)}


def detect_pullback_continuation(context: dict[str, Any]) -> Optional[Anchor]:
    """Trend + a pullback into a level the trend already respects."""
    side = _trend_side(context)
    if side not in {Side.LONG.value, Side.SHORT.value}:
        return None
    if str(context.get("regime")) not in {Regime.TREND.value, Regime.NORMAL.value, Regime.TRANSITION.value}:
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if len(c15) < 60:
        return None
    ladder = _ema_ladder(c15)
    zones = dict(context.get("zones") or {})
    vwap = safe_float(context.get("vwap"))
    sign = side_sign(side)

    candidates: list[tuple[str, float, float]] = []
    for name, level in (("EMA20", ladder["ema20"]), ("EMA50", ladder["ema50"]), ("VWAP", vwap)):
        if level > 0:
            candidates.append((name, level, 58))
    for zone in zones.get("order_blocks") or []:
        if zone.side == side:
            candidates.append((zone.kind, (zone.low + zone.high) / 2.0, 56 + 8 * zone.strength))
    for zone in zones.get("fvg") or []:
        if zone.side == side:
            candidates.append((zone.kind, (zone.low + zone.high) / 2.0, 54 + 6 * zone.strength))
    structure = dict(context.get("structure15") or {})
    swing_key = "swing_lows" if side == Side.LONG.value else "swing_highs"
    for level in (structure.get(swing_key) or [])[-2:]:
        if safe_float(level) > 0:
            candidates.append(("SWING", safe_float(level), 55))

    best: Optional[tuple[str, float, float]] = None
    for name, level, score in candidates:
        if level <= 0:
            continue
        # The level must be behind price relative to the trend, i.e. a pullback
        # target rather than a chase target.
        if sign * (level - safe_float(context.get("price"))) > 0.10 * atr15:
            continue
        if best is None or score > best[2]:
            best = (name, level, score)
    if best is None:
        return None
    name, level, score = best
    invalidation = level - sign * max(0.90 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.PULLBACK_CONTINUATION.value, side, level, invalidation,
        AnchorKind.VALUE_LEVEL.value if name in {"EMA20", "EMA50", "VWAP"} else AnchorKind.DEMAND_ZONE.value if side == Side.LONG.value else AnchorKind.SUPPLY_ZONE.value,
        f"Відкат у {name} за трендом {side}", context, score=score,
        evidence={"pullback_target": name, "level": round(level, 6),
                  "trend_direction": side, "regime": context.get("regime")},
    )


def detect_fresh_base_continuation(context: dict[str, Any]) -> Optional[Anchor]:
    """A newly formed base in trend; the anchor is the base edge for a retest."""
    side = _trend_side(context)
    if side not in {Side.LONG.value, Side.SHORT.value}:
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    base = (context.get("zones") or {}).get("consolidation") or {}
    if not base.get("found") or len(c15) < 20:
        return None
    low = safe_float(base.get("low"))
    high = safe_float(base.get("high"))
    if low <= 0 or high <= 0:
        return None
    rows = _confirmed(c15, 4)
    level = low if side == Side.LONG.value else high
    invalidation = level - side_sign(side) * max(0.70 * atr15, ABS_MIN_STOP_DOLLARS)
    displaced = any(_body(c) >= 0.9 * atr15 and ((_is_bull(c) and side == Side.LONG.value) or (not _is_bull(c) and side == Side.SHORT.value)) for c in rows)
    return make_anchor(
        SetupType.FRESH_BASE_CONTINUATION.value, side, level, invalidation,
        AnchorKind.RANGE_EDGE.value,
        f"Свіжа база {low:.4f}-{high:.4f}, робоча межа {level:.4f}",
        context, score=58 + (8 if displaced else 0),
        evidence={"base_low": low, "base_high": high, "compression": base.get("compression"),
                  "displacement_seen": displaced, "base_age_bars": base.get("bars")},
    )


def detect_acceptance_retest_continuation(context: dict[str, Any]) -> Optional[Anchor]:
    """A level broken and then accepted; the retest of that level is the entry.

    The old detector demanded a closed 15m acceptance candle and allowed entry
    up to 3.75 ATR away. Here the broken level itself is the anchor, so the
    reaction engine decides — and only near the level.
    """
    shift = dict(context.get("structure_shift_15m") or {})
    if not shift.get("shift"):
        return None
    side = str(shift.get("side") or "").upper()
    level = safe_float(shift.get("level"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if side not in {Side.LONG.value, Side.SHORT.value} or level <= 0:
        return None
    sign = side_sign(side)
    invalidation = level - sign * max(0.85 * atr15, ABS_MIN_STOP_DOLLARS)
    age = safe_int(shift.get("age_bars"), 0)
    return make_anchor(
        SetupType.ACCEPTANCE_RETEST_CONTINUATION.value, side, level, invalidation,
        AnchorKind.BREAK_LEVEL.value,
        f"Рівень {level:.4f} пробито й прийнято, чекаємо ретест",
        context, score=max(50, 66 - 3 * age),
        evidence={"break_level": level, "shift_kind": shift.get("kind"), "age_bars": age},
    )


def detect_momentum_no_pullback_continuation(context: dict[str, Any]) -> Optional[Anchor]:
    """Strong impulse with no pullback: enter only at the shallow gap it left."""
    side = _trend_side(context)
    if side not in {Side.LONG.value, Side.SHORT.value}:
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if len(c15) < 20:
        return None
    rows = _confirmed(c15, 6)
    sign = side_sign(side)
    impulse = [c for c in rows if _body(c) >= 1.25 * atr15 and ((_is_bull(c) and sign > 0) or (not _is_bull(c) and sign < 0))]
    if not impulse:
        return None
    net = sign * (rows[-1].close - rows[0].close)
    if net < 2.0 * atr15:
        return None
    gaps = [z for z in ((context.get("zones") or {}).get("fvg") or []) if z.side == side and not z.mitigated]
    if gaps:
        zone = gaps[-1]
        level = zone.high if sign > 0 else zone.low
        strength = 60 + 8 * zone.strength
        kind = AnchorKind.FAIR_VALUE_GAP.value
        evidence = {"impulse_bars": len(impulse), "net_move_atr": round(net / atr15, 3),
                    "gap_low": zone.low, "gap_high": zone.high}
    else:
        last = impulse[-1]
        level = last.open
        strength = 54
        kind = AnchorKind.STRUCTURE_SHIFT.value
        evidence = {"impulse_bars": len(impulse), "net_move_atr": round(net / atr15, 3),
                    "impulse_open": round(last.open, 6)}
    invalidation = level - sign * max(0.75 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.MOMENTUM_NO_PULLBACK_CONTINUATION.value, side, level, invalidation, kind,
        f"Імпульс без відкату; робоча точка {level:.4f}", context, score=strength,
        evidence=evidence, ttl_minutes=max(45, ANCHOR_MAX_AGE_MIN // 2),
    )


def detect_acceleration_pullback_reentry(context: dict[str, Any]) -> Optional[Anchor]:
    """Second leg: the first impulse's pullback extreme becomes the re-entry."""
    side = _trend_side(context)
    if side not in {Side.LONG.value, Side.SHORT.value}:
        return None
    c3 = list((context.get("candles") or {}).get("3m") or [])
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    atr3 = max(safe_float(context.get("atr3"), 0.0), 1e-9)
    if len(c3) < 30 or len(c15) < 20:
        return None
    rows = _confirmed(c3, 24)
    sign = side_sign(side)
    legs = [c for c in rows if _body(c) >= 1.10 * atr3 and ((_is_bull(c) and sign > 0) or (not _is_bull(c) and sign < 0))]
    if len(legs) < 2:
        return None
    first_leg_end = legs[len(legs) // 2]
    pullback_rows = rows[rows.index(first_leg_end) + 1:] if first_leg_end in rows else []
    if not pullback_rows:
        return None
    level = min(c.low for c in pullback_rows) if sign > 0 else max(c.high for c in pullback_rows)
    depth = sign * (first_leg_end.close - level)
    if not (0.30 * atr15 <= depth <= 1.60 * atr15):
        return None
    invalidation = level - sign * max(0.55 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.ACCELERATION_PULLBACK_REENTRY.value, side, level, invalidation,
        AnchorKind.DEMAND_ZONE.value if sign > 0 else AnchorKind.SUPPLY_ZONE.value,
        f"Відкат другої ноги до {level:.4f}", context, score=57,
        evidence={"impulse_legs": len(legs), "pullback_depth_atr": round(depth / atr15, 3), "level": round(level, 6)},
    )


# ==========================================================
# FAMILY 3 — STRUCTURAL_EXPANSION
# Control transfers, or compression resolves.
# ==========================================================

def detect_direction_flip_15m(context: dict[str, Any]) -> Optional[Anchor]:
    """CHoCH on 15m: the broken pivot is the new reference level."""
    shift = dict(context.get("structure_shift_15m") or {})
    if not shift.get("shift") or safe_int(shift.get("age_bars"), 99) > 3:
        return None
    side = str(shift.get("side") or "").upper()
    level = safe_float(shift.get("level"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    structure = dict(context.get("structure15") or {})
    if side not in {Side.LONG.value, Side.SHORT.value} or level <= 0:
        return None
    sign = side_sign(side)
    htf = htf_alignment_for_side(dict(context.get("htf_fact") or {}), side)
    invalidation = level - sign * max(1.00 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.DIRECTION_FLIP.value, side, level, invalidation,
        AnchorKind.STRUCTURE_SHIFT.value,
        f"Зміна напрямку 15m: контроль перейшов через {level:.4f}",
        context, score=54 + (14 if htf.get("supports") else 0),
        evidence={"broken_pivot": level, "shift_kind": shift.get("kind"),
                  "htf_state": htf.get("state"), "structure_direction": structure.get("direction")},
    )


def detect_trend_ignition(context: dict[str, Any]) -> Optional[Anchor]:
    """Compression resolves with displacement; the breakout edge is the anchor."""
    base = (context.get("zones") or {}).get("consolidation") or {}
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    regime_profile = dict(context.get("regime_profile") or {})
    if not base.get("found") or len(c15) < 20:
        return None
    rows = _confirmed(c15, 3)
    if not rows:
        return None
    last = rows[-1]
    low = safe_float(base.get("low"))
    high = safe_float(base.get("high"))
    efficiency = safe_float(regime_profile.get("efficiency_15m"))
    if last.close > high and _body(last) >= 1.10 * atr15:
        side, level = Side.LONG.value, high
    elif last.close < low and _body(last) >= 1.10 * atr15:
        side, level = Side.SHORT.value, low
    else:
        return None
    sign = side_sign(side)
    invalidation = level - sign * max(0.80 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.TREND_IGNITION.value, side, level, invalidation,
        AnchorKind.BREAK_LEVEL.value,
        f"Стиснення {base.get('compression')} ATR розірвано через {level:.4f}",
        context, score=56 + 20 * clamp(efficiency, 0, 1),
        evidence={"compression": base.get("compression"), "break_body_atr": round(_body(last) / atr15, 3),
                  "efficiency_15m": round(efficiency, 3)},
    )


def detect_breakout_retest(context: dict[str, Any]) -> Optional[Anchor]:
    """Prior-day/week or range boundary taken; the retest is the entry."""
    session = dict(context.get("session") or {})
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if len(c15) < 20 or price <= 0:
        return None
    rows = _confirmed(c15, 6)
    for level_key, side, kind in (
        ("prev_day_high", Side.LONG.value, AnchorKind.BREAK_LEVEL.value),
        ("week_high", Side.LONG.value, AnchorKind.BREAK_LEVEL.value),
        ("prev_day_low", Side.SHORT.value, AnchorKind.BREAK_LEVEL.value),
        ("week_low", Side.SHORT.value, AnchorKind.BREAK_LEVEL.value),
    ):
        level = safe_float(session.get(level_key))
        if level <= 0:
            continue
        sign = side_sign(side)
        broke = any(sign * (c.close - level) > 0.10 * atr15 for c in rows)
        if not broke:
            continue
        invalidation = level - sign * max(0.70 * atr15, ABS_MIN_STOP_DOLLARS)
        return make_anchor(
            SetupType.BREAKOUT_RETEST.value, side, level, invalidation, kind,
            f"{level_key.replace('_', ' ')} {level:.4f} пробито, чекаємо ретест",
            context, score=57,
            evidence={"broken_level_key": level_key, "broken_level": level,
                      "distance_atr": round(sign * (price - level) / atr15, 3)},
        )
    return None


def detect_range_compression_breakout(context: dict[str, Any]) -> Optional[Anchor]:
    """Very tight base: trade the boundary in the direction of the first push."""
    base = (context.get("zones") or {}).get("consolidation") or {}
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    compression = safe_float(base.get("compression"), 99)
    if not base.get("found") or compression > 1.4 or price <= 0:
        return None
    low = safe_float(base.get("low"))
    high = safe_float(base.get("high"))
    mid = safe_float(base.get("mid"))
    side = Side.LONG.value if price >= mid else Side.SHORT.value
    level = low if side == Side.LONG.value else high
    sign = side_sign(side)
    invalidation = level - sign * max(0.55 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.RANGE_COMPRESSION_BREAKOUT.value, side, level, invalidation,
        AnchorKind.RANGE_EDGE.value,
        f"Дуже щільна база {compression:.2f} ATR, межа {level:.4f}",
        context, score=52 + 12 * clamp((1.4 - compression) / 1.4, 0, 1),
        evidence={"compression": compression, "base_low": low, "base_high": high},
        ttl_minutes=max(45, ANCHOR_MAX_AGE_MIN // 2),
    )


# ==========================================================
# FAMILY 4 — SESSION_EXPANSION
# ==========================================================

def detect_opening_range_breakout(context: dict[str, Any]) -> Optional[Anchor]:
    """Opening range is set; its boundaries are the day's first real levels."""
    session = dict(context.get("session") or {})
    if not session.get("opening_range_complete"):
        return None
    high = safe_float(session.get("opening_range_high"))
    low = safe_float(session.get("opening_range_low"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if high <= 0 or low <= 0 or price <= 0 or (high - low) < 0.5 * atr15:
        return None
    level = high if price >= (high + low) / 2 else low
    side = Side.LONG.value if level == high else Side.SHORT.value
    sign = side_sign(side)
    invalidation = level - sign * max(0.75 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.OPENING_RANGE_BREAKOUT.value, side, level, invalidation,
        AnchorKind.RANGE_EDGE.value,
        f"Opening range {low:.4f}-{high:.4f}, робоча межа {level:.4f}",
        context, score=53,
        evidence={"or_high": high, "or_low": low, "or_width_atr": round((high - low) / atr15, 3)},
    )


def detect_liquidity_ladder(context: dict[str, Any]) -> Optional[Anchor]:
    """Stacked equal highs/lows: the first rung is a raid-and-reclaim level."""
    liquidity = dict(context.get("liquidity") or {})
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if price <= 0:
        return None
    for key, side, kind in (
        ("equal_lows", Side.LONG.value, AnchorKind.SWEEP_LOW.value),
        ("equal_highs", Side.SHORT.value, AnchorKind.SWEEP_HIGH.value),
    ):
        pools = [p for p in (liquidity.get(key) or []) if safe_float(p.get("touches"), 0) >= 2]
        if not pools:
            continue
        pools.sort(key=lambda p: abs(safe_float(p.get("level")) - price))
        pool = pools[0]
        level = safe_float(pool.get("level"))
        if level <= 0 or abs(level - price) > 2.5 * atr15:
            continue
        sign = side_sign(side)
        invalidation = level - sign * max(0.85 * atr15, ABS_MIN_STOP_DOLLARS)
        return make_anchor(
            SetupType.LIQUIDITY_LADDER.value, side, level, invalidation, kind,
            f"Сходи ліквідності: {int(pool.get('touches', 0))} дотики до {level:.4f}",
            context, score=52 + min(14, 4 * safe_int(pool.get("touches"))),
            evidence={"pool_level": level, "touches": pool.get("touches"),
                      "distance_atr": round(abs(level - price) / atr15, 3)},
        )
    return None


# ==========================================================
# FAMILY 5 — FAILED_EXPANSION
# The breakout did not hold; trade back through structure.
# ==========================================================

def detect_failed_opening_range_breakout(context: dict[str, Any]) -> Optional[Anchor]:
    """OR boundary pierced and closed back inside — fade it."""
    session = dict(context.get("session") or {})
    if not session.get("opening_range_complete"):
        return None
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    high = safe_float(session.get("opening_range_high"))
    low = safe_float(session.get("opening_range_low"))
    if high <= 0 or low <= 0 or len(c15) < 8:
        return None
    rows = _confirmed(c15, 6)
    for level, side, extreme_key in ((high, Side.SHORT.value, "high"), (low, Side.LONG.value, "low")):
        pierced = [c for c in rows if (c.high > level if side == Side.SHORT.value else c.low < level)]
        closed_inside = [c for c in pierced if (c.close < level if side == Side.SHORT.value else c.close > level)]
        if not pierced or len(closed_inside) < len(pierced):
            continue
        last = pierced[-1]
        extreme = safe_float(getattr(last, extreme_key))
        sign = side_sign(side)
        invalidation = extreme + sign * 0.20 * atr15
        return make_anchor(
            SetupType.FAILED_OPENING_RANGE_BREAKOUT.value, side, level, invalidation,
            AnchorKind.RANGE_EDGE.value,
            f"Пробій opening range {level:.4f} не втримався",
            context, score=58,
            evidence={"or_level": level, "pierce_extreme": extreme,
                      "pierced_bars": len(pierced), "refused_bars": len(closed_inside)},
        )
    return None


def detect_failed_breakout_short(context: dict[str, Any]) -> Optional[Anchor]:
    """A high taken out and immediately refused: the break level is the anchor."""
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if len(c15) < 24:
        return None
    rows = _confirmed(c15, 21)
    last = rows[-1]
    prior_high = max(c.high for c in rows[:-1])
    if not (last.high > prior_high and last.close < prior_high):
        return None
    if _upper_wick_ratio(last) < REACTION_REJECTION_RATIO and _close_location(last) > 0.45:
        return None
    return make_anchor(
        SetupType.FAILED_BREAKOUT_SHORT.value, Side.SHORT.value, prior_high,
        last.high + 0.15 * atr15, AnchorKind.SWEEP_HIGH.value,
        f"Хибний пробій {prior_high:.4f}, закриття нижче",
        context, score=58 + (8 if (context.get("smt") or {}).get("supports_short") else 0),
        evidence={"failed_level": prior_high, "extreme": round(last.high, 6),
                  "upper_wick_ratio": round(_upper_wick_ratio(last), 4),
                  "close_location": round(_close_location(last), 4)},
    )


def detect_mss_reversal_short(context: dict[str, Any]) -> Optional[Anchor]:
    """Market structure shift down after an advance; retest the broken swing."""
    shift = dict(context.get("structure_shift_15m") or {})
    structure = dict(context.get("structure15") or {})
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    if shift.get("side") != Side.SHORT.value or len(c15) < 30:
        return None
    rows = _confirmed(c15, 20)
    advance = rows[-1].close < min(c.close for c in rows[:6])
    if not advance:
        return None
    level = safe_float(shift.get("level"))
    if level <= 0:
        level = max(c.high for c in rows[-6:])
    invalidation = max(c.high for c in rows[-8:]) + 0.25 * atr15
    return make_anchor(
        SetupType.MSS_REVERSAL_SHORT.value, Side.SHORT.value, level, invalidation,
        AnchorKind.STRUCTURE_SHIFT.value,
        f"MSS вниз через {level:.4f} після висхідної ноги",
        context, score=55,
        evidence={"broken_level": level, "shift_kind": shift.get("kind"),
                  "prior_structure_direction": structure.get("direction")},
    )


def detect_or_failure_2_short(context: dict[str, Any]) -> Optional[Anchor]:
    """Second failed attempt at the opening-range high is a stronger refusal."""
    session = dict(context.get("session") or {})
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    high = safe_float(session.get("opening_range_high"))
    if high <= 0 or len(c15) < 12 or not session.get("opening_range_complete"):
        return None
    rows = _confirmed(c15, 10)
    attempts = [c for c in rows if c.high > high]
    refused = [c for c in attempts if c.close < high]
    if len(attempts) < 2 or len(refused) < len(attempts):
        return None
    extreme = max(c.high for c in attempts)
    return make_anchor(
        SetupType.OR_FAILURE_2_SHORT.value, Side.SHORT.value, high,
        extreme + 0.20 * atr15, AnchorKind.RANGE_EDGE.value,
        f"Друга невдала спроба пробити OR-high {high:.4f}",
        context, score=60 + 4 * min(3, len(attempts) - 2),
        evidence={"or_high": high, "attempts": len(attempts), "refusals": len(refused), "extreme": round(extreme, 6)},
    )


# ==========================================================
# FAMILY 6 — VALUE_RECLAIM
# Rotation around session or higher-timeframe value.
# ==========================================================

def detect_session_mean_reclaim(context: dict[str, Any]) -> Optional[Anchor]:
    """Displacement away from VWAP, then a reclaim of it."""
    vwap = safe_float(context.get("vwap"))
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if vwap <= 0 or price <= 0 or len(c15) < 12:
        return None
    rows = _confirmed(c15, 10)
    sign = 1 if price >= vwap else -1
    side = Side.LONG.value if sign > 0 else Side.SHORT.value
    displaced = any(sign * (c.close - vwap) > 1.10 * atr15 for c in rows[:-1])
    if not displaced:
        return None
    invalidation = vwap - sign * max(0.70 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.SESSION_MEAN_RECLAIM.value, side, vwap, invalidation,
        AnchorKind.VALUE_LEVEL.value,
        f"Повернення до VWAP сесії {vwap:.4f}", context, score=55,
        evidence={"vwap": vwap, "max_displacement_atr": round(max(sign * (c.close - vwap) for c in rows) / atr15, 3)},
    )


def detect_daily_weekly_open_reclaim(context: dict[str, Any]) -> Optional[Anchor]:
    """Open of the day or week reclaimed after trading through it."""
    session = dict(context.get("session") or {})
    c15 = list((context.get("candles") or {}).get("15m") or [])
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if price <= 0 or len(c15) < 12:
        return None
    rows = _confirmed(c15, 10)
    for key, label in (("day_open", "відкриття дня"), ("week_open", "відкриття тижня")):
        level = safe_float(session.get(key))
        if level <= 0:
            continue
        sign = 1 if price >= level else -1
        side = Side.LONG.value if sign > 0 else Side.SHORT.value
        traded_through = any(sign * (c.close - level) < -0.35 * atr15 for c in rows[:-1])
        if not traded_through:
            continue
        invalidation = level - sign * max(0.75 * atr15, ABS_MIN_STOP_DOLLARS)
        return make_anchor(
            SetupType.DAILY_WEEKLY_OPEN_RECLAIM.value, side, level, invalidation,
            AnchorKind.VALUE_LEVEL.value,
            f"Reclaim {label} {level:.4f}", context, score=56,
            evidence={"reference": key, "level": level,
                      "max_adverse_atr": round(min(sign * (c.close - level) for c in rows) / atr15, 3)},
        )
    return None


def detect_time_of_day_adaptive(context: dict[str, Any]) -> Optional[Anchor]:
    """Session-specific value: the Asia range boundary during London/NY."""
    session = dict(context.get("session") or {})
    name = str(context.get("session_name") or "").upper()
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    if name not in {"LONDON_OPEN", "LONDON", "NY_OVERLAP", "NY_PM"} or price <= 0:
        return None
    asia_low = safe_float(session.get("day_low"))
    asia_high = safe_float(session.get("day_high"))
    if asia_low <= 0 or asia_high <= 0 or (asia_high - asia_low) < 0.8 * atr15:
        return None
    if abs(price - asia_low) <= 0.75 * atr15:
        side, level = Side.LONG.value, asia_low
    elif abs(price - asia_high) <= 0.75 * atr15:
        side, level = Side.SHORT.value, asia_high
    else:
        return None
    sign = side_sign(side)
    invalidation = level - sign * max(0.65 * atr15, ABS_MIN_STOP_DOLLARS)
    return make_anchor(
        SetupType.TIME_OF_DAY_ADAPTIVE.value, side, level, invalidation,
        AnchorKind.RANGE_EDGE.value,
        f"Межа азійського діапазону {level:.4f} у сесію {name}",
        context, score=52,
        evidence={"session": name, "asia_low": asia_low, "asia_high": asia_high},
        ttl_minutes=max(60, ANCHOR_MAX_AGE_MIN // 2),
    )


# ==========================================================
# DETECTOR REGISTRY
# ==========================================================

DETECTORS: list[Any] = [
    detect_sweep_reclaim,
    detect_capitulation_recovery,
    detect_range_edge_reversal,
    detect_failed_auction_rejection,
    detect_liquidity_sweep_reversal_short,
    detect_buyer_exhaustion_short,
    detect_pullback_continuation,
    detect_fresh_base_continuation,
    detect_acceptance_retest_continuation,
    detect_momentum_no_pullback_continuation,
    detect_acceleration_pullback_reentry,
    detect_direction_flip_15m,
    detect_trend_ignition,
    detect_breakout_retest,
    detect_range_compression_breakout,
    detect_opening_range_breakout,
    detect_liquidity_ladder,
    detect_failed_opening_range_breakout,
    detect_failed_breakout_short,
    detect_mss_reversal_short,
    detect_or_failure_2_short,
    detect_session_mean_reclaim,
    detect_daily_weekly_open_reclaim,
    detect_time_of_day_adaptive,
]

DETECTED_SETUP_TYPES = frozenset(CANONICAL_SETUP_FAMILY_MAP)


def detect_anchors(context: dict[str, Any]) -> list[Anchor]:
    """Run every detector once; a failure in one never blocks the others."""
    found: list[Anchor] = []
    for detector in DETECTORS:
        try:
            anchor = detector(context)
        except Exception as exc:
            print(f"[WARN] detector {getattr(detector, '__name__', '?')} failed: {exc}")
            continue
        if anchor is not None:
            found.append(anchor)
    return found


if len(DETECTORS) != 24:
    raise RuntimeError(f"organic registry must expose 24 detectors, got {len(DETECTORS)}")
# ==========================================================
# EARLY TRIGGER ENGINE
# ==========================================================
# The only place in the bot that can authorize an entry.
#
#   anchor (причина)  +  3m reaction (факт)  ->  market entry (виконання)
#
# Every gate below is a reason NOT to enter. Nothing here can be satisfied by
# a level the market has already left behind, because GATE_PROXITY measures the
# live distance to the anchor and GATE_STOP caps how wide the resulting risk may
# be. Together they replace the old "wait for a closed 15m candle, then enter up
# to 3.75 ATR from the level" behaviour that produced MFE < 0.2R on 18 of 30
# trades.

REACTION_SCHEMA_VERSION = "anchor_reaction_3m_v10.0.0"


def _anchor_zone(anchor: Anchor, atr15: float) -> tuple[float, float]:
    """Price band around the anchor inside which a touch counts as a touch."""
    half = max(ANCHOR_ZONE_ATR * atr15, ABS_MIN_STOP_DOLLARS * 0.5)
    return anchor.level - half, anchor.level + half


def _reaction_bars(context: dict[str, Any], anchor: Anchor) -> list[Candle]:
    """Confirmed 3m bars inside the reaction window, oldest first."""
    c3 = list((context.get("candles") or {}).get("3m") or [])
    rows = [c for c in c3 if getattr(c, "confirmed", True)]
    if not rows:
        return []
    horizon_ms = int(now_utc().timestamp() * 1000) - TRIGGER_LOOKBACK_3M * 3 * 60 * 1000
    window = [c for c in rows if int(c.ts) >= max(horizon_ms, anchor.created_ts)]
    return window[-TRIGGER_LOOKBACK_3M:] or rows[-TRIGGER_LOOKBACK_3M:]


def _touch_bars(bars: list[Candle], zone_low: float, zone_high: float) -> list[Candle]:
    return [c for c in bars if c.low <= zone_high and c.high >= zone_low]


def _rejection_evidence(bars: list[Candle], anchor: Anchor) -> dict[str, Any]:
    """Did the touch bars refuse the level in the anchor's direction?"""
    sign = side_sign(anchor.side)
    if not bars:
        return {"rejected": False, "reason": "NO_TOUCH_BARS"}
    best: Optional[dict[str, Any]] = None
    for c in bars:
        span = _range(c)
        if sign > 0:
            # Rejection of a demand level: the bar dipped and closed back up.
            wick = _lower_wick_ratio(c)
            directional_close = c.close > c.open
            closed_back = c.close > anchor.level
            strength = max(wick, 1.0 if closed_back else 0.0, 0.6 if directional_close else 0.0)
            extreme = c.low
        else:
            wick = _upper_wick_ratio(c)
            directional_close = c.close < c.open
            closed_back = c.close < anchor.level
            strength = max(wick, 1.0 if closed_back else 0.0, 0.6 if directional_close else 0.0)
            extreme = c.high
        candidate = {
            "ts": int(c.ts), "strength": round(strength, 4), "wick_ratio": round(wick, 4),
            "directional_close": bool(directional_close), "closed_back_inside": bool(closed_back),
            "extreme": round(extreme, 6), "body": round(_body(c), 6), "close_location": round(_close_location(c), 4),
        }
        # >= on purpose: ties are common, and the freshest rejection is the evidence.
        if best is None or candidate["strength"] >= best["strength"]:
            best = candidate
    if best is None:
        return {"rejected": False, "reason": "NO_TOUCH_BARS"}
    accepted = bool(
        best["strength"] >= REACTION_REJECTION_RATIO
        or best["closed_back_inside"]
        or (best["wick_ratio"] >= REACTION_REJECTION_RATIO and best["directional_close"])
    )
    best.update({"rejected": accepted, "reason": "" if accepted else "REJECTION_TOO_WEAK"})
    return best


def _displacement_evidence(bars: list[Candle], anchor: Anchor, atr3: float) -> dict[str, Any]:
    """Is the newest bar actually moving away from the level, not hovering on it?"""
    sign = side_sign(anchor.side)
    if not bars or atr3 <= 0:
        return {"displaced": False, "reason": "NO_BARS", "body_atr3": 0.0}
    tail = bars[-2:]
    for c in reversed(tail):
        directional = (_is_bull(c) and sign > 0) or (not _is_bull(c) and sign < 0)
        body_atr3 = _body(c) / atr3
        if directional and body_atr3 >= REACTION_BODY_ATR3:
            return {
                "displaced": True, "reason": "", "body_atr3": round(body_atr3, 3),
                "ts": int(c.ts), "close": round(c.close, 6), "bars_checked": len(tail),
            }
    last = tail[-1]
    body_atr3 = _body(last) / atr3
    directional = (_is_bull(last) and sign > 0) or (not _is_bull(last) and sign < 0)
    return {
        "displaced": False, "body_atr3": round(body_atr3, 3), "directional": bool(directional),
        "bars_checked": len(tail),
        "reason": "BODY_TOO_SMALL" if directional else "NOT_DIRECTIONAL",
    }


def _provisional_stop(anchor: Anchor, entry: float, reaction: dict[str, Any], atr15: float) -> dict[str, Any]:
    """Structural stop from the anchor's own falsifier, capped for early entry."""
    sign = side_sign(anchor.side)
    buffer_dollars = max(0.10 * atr15, COMMISSION_BUFFER_DOLLARS)
    structural = anchor.invalidation - sign * buffer_dollars
    extreme = safe_float(reaction.get("extreme"))
    if extreme > 0:
        # Never place the stop inside the rejection wick that justified the entry.
        guarded = extreme - sign * buffer_dollars
        structural = min(structural, guarded) if sign > 0 else max(structural, guarded)
    distance = abs(entry - structural)
    max_distance = MAX_STOP_ATR * atr15
    return {
        "stop": round_price(structural),
        "distance": round(distance, 6),
        "distance_atr": round(distance / atr15, 4) if atr15 > 0 else 0.0,
        "within_cap": bool(0 < distance <= max_distance),
        "max_distance": round(max_distance, 6),
        "basis": "ANCHOR_INVALIDATION_GUARDED_BY_REACTION_EXTREME",
    }


def evaluate_reaction(context: dict[str, Any], anchor: Anchor) -> Reaction:
    """Run every entry gate against one armed anchor."""
    price = safe_float(context.get("price"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    atr3 = max(safe_float(context.get("atr3"), 0.0), 1e-9)
    sign = side_sign(anchor.side)
    gates: dict[str, Any] = {"schema_version": REACTION_SCHEMA_VERSION}

    def refuse(gate: str, reason: str) -> Reaction:
        gates[gate] = {"pass": False, "reason": reason}
        return Reaction(anchor_id=anchor.id, ready=False, entry_price=price, reason=f"{gate}: {reason}", gates=gates)

    if price <= 0:
        return refuse("GATE_PRICE", "NO_TRUSTED_PRICE")

    now_ms = int(now_utc().timestamp() * 1000)
    age_minutes = (now_ms - int(anchor.created_ts)) / 60000.0
    gates["GATE_FRESH"] = {"pass": age_minutes <= ANCHOR_MAX_AGE_MIN, "age_minutes": round(age_minutes, 2)}
    if now_ms > int(anchor.expires_ts):
        return refuse("GATE_FRESH", "ANCHOR_EXPIRED")
    if age_minutes > ANCHOR_MAX_AGE_MIN:
        return refuse("GATE_FRESH", f"ANCHOR_AGE_{age_minutes:.0f}M")
    if int(anchor.cooldown_until_ts) > now_ms:
        return refuse("GATE_COOLDOWN", "LEVEL_RECENTLY_CONSUMED")

    # GATE_PROXITY — the structural reason a late entry cannot happen.
    distance = abs(price - anchor.level)
    distance_atr = distance / atr15
    gates["GATE_PROXIMITY"] = {
        "pass": distance_atr <= ANCHOR_MAX_ATR, "distance": round(distance, 6),
        "distance_atr": round(distance_atr, 4), "max_atr": ANCHOR_MAX_ATR,
    }
    if distance_atr > ANCHOR_MAX_ATR:
        return refuse("GATE_PROXIMITY", f"PRICE_{distance_atr:.2f}ATR_FROM_ANCHOR")

    # GATE_INTACT — the falsifier must not already have fired.
    if sign * (price - anchor.invalidation) <= 0:
        gates["GATE_INTACT"] = {"pass": False, "invalidation": anchor.invalidation}
        return refuse("GATE_INTACT", "ANCHOR_ALREADY_INVALIDATED")
    bars = _reaction_bars(context, anchor)
    beyond = [c for c in bars if sign * (c.close - anchor.invalidation) <= 0]
    if beyond:
        gates["GATE_INTACT"] = {"pass": False, "confirmed_beyond_invalidation": len(beyond)}
        return refuse("GATE_INTACT", "CONFIRMED_CLOSE_BEYOND_INVALIDATION")
    gates["GATE_INTACT"] = {"pass": True}

    # GATE_TOUCH — price must actually have visited the level on 3m.
    zone_low, zone_high = _anchor_zone(anchor, atr15)
    touches = _touch_bars(bars, zone_low, zone_high)
    gates["GATE_TOUCH"] = {
        "pass": bool(touches), "touches": len(touches),
        "zone": [round(zone_low, 6), round(zone_high, 6)], "window_bars": len(bars),
    }
    if not touches:
        return refuse("GATE_TOUCH", "NO_3M_TOUCH_OF_ANCHOR")

    # GATE_REJECTION — the visit must have been refused.
    rejection = _rejection_evidence(touches, anchor)
    rejection["pass"] = bool(rejection.get("rejected"))
    gates["GATE_REJECTION"] = rejection
    if not rejection.get("rejected"):
        return refuse("GATE_REJECTION", str(rejection.get("reason") or "REJECTION_TOO_WEAK"))

    # GATE_DISPLACEMENT — refusal must have turned into movement.
    displacement = _displacement_evidence(bars, anchor, atr3)
    displacement["pass"] = bool(displacement.get("displaced"))
    gates["GATE_DISPLACEMENT"] = displacement
    if not displacement.get("displaced"):
        return refuse("GATE_DISPLACEMENT", str(displacement.get("reason") or "NO_IMPULSE"))

    # GATE_SPREAD — an untradeable spread makes the R arithmetic fiction.
    spread_atr = safe_float(context.get("spread_atr"))
    gates["GATE_SPREAD"] = {"pass": spread_atr <= MAX_SPREAD_ATR, "spread_atr": round(spread_atr, 4)}
    if spread_atr > MAX_SPREAD_ATR:
        return refuse("GATE_SPREAD", f"SPREAD_{spread_atr:.2f}ATR")

    # GATE_STOP — risk must be tight enough that the entry is genuinely early.
    stop_profile = _provisional_stop(anchor, price, rejection, atr15)
    stop_profile["pass"] = bool(stop_profile.get("within_cap"))
    gates["GATE_STOP"] = stop_profile
    if not stop_profile["within_cap"]:
        return refuse("GATE_STOP", f"STOP_{stop_profile['distance_atr']:.2f}ATR_EXCEEDS_CAP")

    # GATE_RUNWAY — 0.25R of MFE must physically exist inside the no-followthrough window.
    risk = max(stop_profile["distance"], ABS_MIN_STOP_DOLLARS, 1e-9)
    runway = nearest_runway_r(context, anchor.side, price, risk)
    gates["GATE_RUNWAY"] = {
        "pass": bool(runway.get("meets_min_r") or runway.get("meets_min_atr")),
        "runway_r": runway.get("runway_r"),
        "nearest_kind": (runway.get("nearest") or {}).get("kind"),
        "nearest_distance_atr": (runway.get("nearest") or {}).get("distance_atr"),
        "min_runway_r": MIN_RUNWAY_R, "min_runway_atr": MIN_RUNWAY_ATR,
    }
    if not (runway.get("meets_min_r") or runway.get("meets_min_atr")):
        return refuse("GATE_RUNWAY", "NEAREST_TARGET_TOO_CLOSE_FOR_025R_MFE")

    reaction_ts = safe_int(rejection.get("ts"))
    latency = ((now_ms - reaction_ts) / 60000.0) if reaction_ts else 0.0
    gates["ALL_PASS"] = True
    gates["latency_minutes"] = round(latency, 2)
    return Reaction(
        anchor_id=anchor.id,
        ready=True,
        entry_price=price,
        reason="3M_REACTION_CONFIRMED_AT_ANCHOR",
        gates=gates,
        latency_minutes=round(latency, 2),
        schema_version=REACTION_SCHEMA_VERSION,
    )


# ==========================================================
# ANCHOR MEMORY
# ==========================================================

def anchor_from_dict(raw: Any) -> Optional[Anchor]:
    if not isinstance(raw, dict):
        return None
    try:
        fields = Anchor.__dataclass_fields__
        return Anchor(**{k: raw.get(k) for k in fields if k in raw})
    except Exception:
        return None


def anchor_to_dict(anchor: Anchor) -> dict[str, Any]:
    return asdict(anchor)


def _anchors_are_the_same(left: Anchor, right: Anchor, atr15: float) -> bool:
    """One economic level per setup per direction — never stack duplicates."""
    if left.setup_type != right.setup_type or left.side != right.side:
        return False
    if left.kind != right.kind:
        return False
    tolerance = max(0.20 * atr15, ABS_MIN_STOP_DOLLARS * 0.5)
    return abs(left.level - right.level) <= tolerance


def sync_anchors(
    context: dict[str, Any],
    stored: list[dict[str, Any]],
    fresh: list[Anchor],
) -> tuple[list[Anchor], dict[str, Any]]:
    """Merge persisted anchors with this run's detections and retire the dead."""
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    price = safe_float(context.get("price"))
    now_ms = int(now_utc().timestamp() * 1000)
    horizon_ms = now_ms - ANCHOR_MAX_AGE_MIN * 60 * 1000

    carried: list[Anchor] = []
    retired: dict[str, int] = {"expired": 0, "invalidated": 0, "cooldown": 0, "stale": 0}
    for raw in stored or []:
        anchor = anchor_from_dict(raw)
        if anchor is None:
            continue
        if int(anchor.expires_ts) < now_ms or int(anchor.created_ts) < horizon_ms:
            retired["expired"] += 1
            continue
        if anchor.state in {AnchorState.TRIGGERED.value, AnchorState.INVALIDATED.value, AnchorState.REJECTED.value}:
            retired["stale"] += 1
            continue
        sign = side_sign(anchor.side)
        if price > 0 and sign * (price - anchor.invalidation) <= 0:
            anchor.state = AnchorState.INVALIDATED.value
            retired["invalidated"] += 1
            continue
        carried.append(anchor)

    merged: list[Anchor] = []
    for anchor in fresh + carried:
        duplicate = next((m for m in merged if _anchors_are_the_same(m, anchor, atr15)), None)
        if duplicate is None:
            merged.append(anchor)
            continue
        # A freshly detected anchor restates the same economic level; keep the
        # newer evidence but preserve the earlier creation time and cooldown so
        # a level cannot be re-armed forever by a repeating print.
        duplicate.score = max(duplicate.score, anchor.score)
        duplicate.reason = anchor.reason
        duplicate.evidence = dict(anchor.evidence or {})
        duplicate.last_checked_ts = now_ms

    for anchor in merged:
        if int(anchor.cooldown_until_ts) > now_ms:
            retired["cooldown"] += 1
    merged = [a for a in merged if int(a.cooldown_until_ts) <= now_ms]

    merged.sort(key=lambda a: (a.score, -a.created_ts), reverse=True)
    merged = merged[:ANCHOR_MEMORY_LIMIT]
    audit = {
        "fresh_detected": len(fresh),
        "carried_over": len(carried),
        "armed": len(merged),
        "retired": retired,
        "atr15": round(atr15, 6),
        "schema_version": SCHEMA_VERSION,
    }
    return merged, audit


def consume_anchor(anchor: Anchor, outcome: str, reason: str = "") -> None:
    """Mark an anchor spent so the same level cannot be re-entered immediately."""
    anchor.state = outcome
    anchor.last_checked_ts = int(now_utc().timestamp() * 1000)
    anchor.reject_reason = reason
    cooldown_minutes = ANCHOR_COOLDOWN_MIN if outcome in {AnchorState.TRIGGERED.value, AnchorState.REJECTED.value} else 0
    anchor.cooldown_until_ts = anchor.last_checked_ts + cooldown_minutes * 60 * 1000
# ==========================================================
# SETUP STATISTICS & AUTO-DEGRADATION
# ==========================================================
# "Фокус + авто-деградація": усі 24 сетапи лишаються в таксономії і далі
# детектуються та пишуться в журнал, але ВИКОНУВАТИСЯ можуть лише ті, чия
# накопичена статистика не є від'ємною. Статус перераховується щоранку з
# журналу, тож деградований сетап сам повертається, щойно дані змінюються —
# жодного ручного списку і жодного переофіту на 30 угодах.

def _trade_result_r(trade: dict[str, Any]) -> Optional[float]:
    if not isinstance(trade, dict):
        return None
    if not bool(trade.get("ml_eligible", True)):
        return None
    for key in ("pnl_r", "result_r"):
        value = trade.get(key)
        if value is not None:
            number = safe_float(value, float("nan"))
            if math.isfinite(number):
                return number
    return None


def _trade_is_win(trade: dict[str, Any]) -> Optional[bool]:
    result_class = str(trade.get("result_class") or trade.get("result") or "").upper()
    if result_class == "WIN":
        return True
    if result_class in {"LOSS", "BREAKEVEN"}:
        return False
    result_r = _trade_result_r(trade)
    if result_r is None:
        return None
    return bool(result_r > 0.0)


def compute_setup_statistics(journal: dict[str, Any]) -> dict[str, Any]:
    """Per-setup outcome statistics from the closed-trade journal."""
    trades = [t for t in list(journal.get("trades") or []) if isinstance(t, dict)]
    buckets: dict[str, dict[str, Any]] = {}
    family_buckets: dict[str, dict[str, Any]] = {}

    def add(bucket: dict[str, Any], result_r: float, is_win: Optional[bool], mfe_r: float, mae_r: float) -> None:
        bucket["trades"] += 1
        if is_win is True:
            bucket["wins"] += 1
        elif is_win is False:
            bucket["losses"] += 1
        bucket["net_r"] += result_r
        bucket["mfe_r"].append(mfe_r)
        bucket["mae_r"].append(mae_r)

    for trade in trades:
        result_r = _trade_result_r(trade)
        if result_r is None:
            continue
        is_win = _trade_is_win(trade)
        setup_type = str(trade.get("setup_type") or "UNKNOWN").upper()
        family = str(trade.get("canonical_setup_family") or canonical_setup_family(setup_type)).upper()
        mfe_r = safe_float(trade.get("mfe_r"), 0.0)
        mae_r = safe_float(trade.get("mae_r"), 0.0)
        buckets.setdefault(setup_type, {"trades": 0, "wins": 0, "losses": 0, "net_r": 0.0, "mfe_r": [], "mae_r": []})
        add(buckets[setup_type], result_r, is_win, mfe_r, mae_r)
        family_buckets.setdefault(family, {"trades": 0, "wins": 0, "losses": 0, "net_r": 0.0, "mfe_r": [], "mae_r": []})
        add(family_buckets[family], result_r, is_win, mfe_r, mae_r)

    def finalize(rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, bucket in rows.items():
            n = int(bucket["trades"])
            wins = int(bucket["wins"])
            net_r = float(bucket["net_r"])
            mfe_values = [v for v in bucket["mfe_r"] if v is not None]
            out[key] = {
                "trades": n,
                "wins": wins,
                "losses": int(bucket["losses"]),
                "win_rate": round(wins / n, 4) if n else 0.0,
                "net_r": round(net_r, 4),
                "expectancy_r": round(net_r / n, 4) if n else 0.0,
                "wilson_lower": round(wilson_lower_bound(wins, n, SETUP_WILSON_Z), 4),
                "median_mfe_r": round(percentile(mfe_values, 0.5), 4) if mfe_values else None,
                "sample_sufficient": bool(n >= SETUP_STATS_MIN_SAMPLE),
            }
        return out

    return {
        "by_setup": finalize(buckets),
        "by_family": finalize(family_buckets),
        "closed_trades": len(trades),
        "measured_trades": sum(b["trades"] for b in buckets.values()),
        "min_sample": SETUP_STATS_MIN_SAMPLE,
        "schema_version": SETUP_DEGRADATION_SCHEMA_VERSION,
        "computed_at": iso_now(),
    }


def setup_degradation_status(setup_type: str, statistics: dict[str, Any]) -> dict[str, Any]:
    """PROMOTE / NEUTRAL / DEMOTE / INSUFFICIENT_SAMPLE for one setup."""
    key = str(setup_type or "").upper()
    by_setup = dict((statistics or {}).get("by_setup") or {})
    by_family = dict((statistics or {}).get("by_family") or {})
    family = canonical_setup_family(key)
    row = dict(by_setup.get(key) or {})
    family_row = dict(by_family.get(family) or {})
    trades = safe_int(row.get("trades"))
    profile: dict[str, Any] = {
        "setup_type": key,
        "setup_family": family,
        "trades": trades,
        "family_trades": safe_int(family_row.get("trades")),
        "win_rate": safe_float(row.get("win_rate")),
        "wilson_lower": safe_float(row.get("wilson_lower")),
        "expectancy_r": safe_float(row.get("expectancy_r")),
        "median_mfe_r": row.get("median_mfe_r"),
        "family_expectancy_r": safe_float(family_row.get("expectancy_r")),
        "family_wilson_lower": safe_float(family_row.get("wilson_lower")),
        "schema_version": SETUP_DEGRADATION_SCHEMA_VERSION,
    }

    if trades < SETUP_STATS_MIN_SAMPLE:
        # Not enough evidence to condemn a setup. It keeps trading at reduced
        # conviction, and the family record breaks a tie if it is clear.
        family_trades = safe_int(family_row.get("trades"))
        family_negative = bool(
            safe_int(family_row.get("trades")) >= SETUP_STATS_MIN_SAMPLE
            and safe_float(family_row.get("wilson_lower")) < SETUP_DEMOTE_WINRATE_FLOOR
            and safe_float(family_row.get("expectancy_r")) < SETUP_DEMOTE_EXPECTANCY_R
        )
        profile.update({
            "status": "INSUFFICIENT_SAMPLE",
            "executable": not family_negative,
            "risk_multiplier": 0.60 if family_negative else 1.00,
            "reason": (
                f"family {family} is negative-expectancy on {family_trades} trades"
                if family_negative else
                f"only {trades}/{SETUP_STATS_MIN_SAMPLE} closed trades — not enough evidence to demote"
            ),
        })
        return profile

    wilson = safe_float(row.get("wilson_lower"))
    expectancy = safe_float(row.get("expectancy_r"))
    win_rate = safe_float(row.get("win_rate"))
    if wilson < SETUP_DEMOTE_WINRATE_FLOOR and expectancy < SETUP_DEMOTE_EXPECTANCY_R:
        profile.update({
            "status": "DEMOTED",
            "executable": False,
            "risk_multiplier": 0.0,
            "reason": (
                f"{trades} trades: win_rate {win_rate:.0%} (Wilson lower {wilson:.2f}) "
                f"expectancy {expectancy:+.3f}R — виконання вимкнено, детекція й журнал лишаються"
            ),
        })
        return profile
    if expectancy >= SETUP_PROMOTE_EXPECTANCY_R and wilson >= SETUP_PROMOTE_WINRATE_FLOOR:
        profile.update({
            "status": "PROMOTED",
            "executable": True,
            "risk_multiplier": 1.00,
            "core_eligible": True,
            "reason": f"{trades} trades: expectancy {expectancy:+.3f}R, Wilson lower {wilson:.2f}",
        })
        return profile
    profile.update({
        "status": "NEUTRAL",
        "executable": True,
        "risk_multiplier": 1.00,
        "core_eligible": bool(expectancy > 0),
        "reason": f"{trades} trades: expectancy {expectancy:+.3f}R, Wilson lower {wilson:.2f}",
    })
    return profile


def compute_degradation_table(journal: dict[str, Any]) -> dict[str, Any]:
    """Full admission table for this run, recomputed from the journal."""
    statistics = compute_setup_statistics(journal)
    table = {
        setup_type: setup_degradation_status(setup_type, statistics)
        for setup_type in sorted(CANONICAL_SETUP_FAMILY_MAP)
    }
    return {
        "setups": table,
        "statistics": statistics,
        "executable_count": sum(1 for row in table.values() if row.get("executable")),
        "demoted": sorted(k for k, v in table.items() if v.get("status") == "DEMOTED"),
        "promoted": sorted(k for k, v in table.items() if v.get("status") == "PROMOTED"),
        "schema_version": SETUP_DEGRADATION_SCHEMA_VERSION,
        "computed_at": iso_now(),
    }


# ==========================================================
# CANDIDATE SCORING
# ==========================================================
# The old bot's trade_entry_quality sat at 57-58 on every single trade while
# its score swung 63-88: the metric carried no information. Every component
# below is a measured quantity with real variance, and the weights put the
# majority on timing and location — the two things that decided whether a trade
# ever reached 0.25R of MFE.

def _reaction_timing_quality(reaction: Reaction) -> dict[str, Any]:
    gates = dict(reaction.gates or {})
    rejection = dict(gates.get("GATE_REJECTION") or {})
    displacement = dict(gates.get("GATE_DISPLACEMENT") or {})
    rejection_strength = clamp(safe_float(rejection.get("strength")) / max(REACTION_REJECTION_RATIO, 1e-9), 0.0, 1.6)
    body_atr3 = clamp(safe_float(displacement.get("body_atr3")) / max(REACTION_BODY_ATR3, 1e-9), 0.0, 2.0)
    closed_back = 1.0 if rejection.get("closed_back_inside") else 0.0
    latency = safe_float(reaction.latency_minutes)
    latency_score = clamp(1.0 - (latency / max(TRIGGER_LOOKBACK_3M * 3.0, 1.0)), 0.0, 1.0)
    quality = clamp(
        34 * min(rejection_strength, 1.0)
        + 26 * min(body_atr3, 1.0)
        + 14 * closed_back
        + 26 * latency_score,
        0.0, 100.0,
    )
    return {
        "quality": round(quality, 2),
        "rejection_strength": round(safe_float(rejection.get("strength")), 4),
        "body_atr3": round(safe_float(displacement.get("body_atr3")), 3),
        "closed_back_inside": bool(rejection.get("closed_back_inside")),
        "latency_minutes": round(latency, 2),
        "latency_score": round(latency_score, 4),
    }


def _location_quality(reaction: Reaction, runway: dict[str, Any]) -> dict[str, Any]:
    gates = dict(reaction.gates or {})
    proximity = dict(gates.get("GATE_PROXIMITY") or {})
    distance_atr = safe_float(proximity.get("distance_atr"), ANCHOR_MAX_ATR)
    near = clamp(1.0 - distance_atr / max(ANCHOR_MAX_ATR, 1e-9), 0.0, 1.0)
    runway_r = safe_float(runway.get("runway_r"))
    runway_score = clamp(runway_r / max(MIN_RUNWAY_R * 1.5, 1e-9), 0.0, 1.0)
    quality = clamp(55 * near + 45 * runway_score, 0.0, 100.0)
    return {
        "quality": round(quality, 2),
        "distance_atr": round(distance_atr, 4),
        "proximity_score": round(near, 4),
        "runway_r": round(runway_r, 4),
        "runway_score": round(runway_score, 4),
    }


def _context_quality(context: dict[str, Any], anchor: Anchor, smt: dict[str, Any]) -> dict[str, Any]:
    htf = htf_alignment_for_side(dict(context.get("htf_fact") or {}), anchor.side)
    regime = str(context.get("regime") or "")
    structure = dict(context.get("structure15") or {})
    regime_fit = 100.0
    if regime == Regime.SHOCK.value:
        regime_fit = 45.0
    elif regime == Regime.RANGE.value and anchor.setup_family == "TREND_CONTINUATION":
        regime_fit = 40.0
    elif regime == Regime.TREND.value and anchor.setup_family in {"LIQUIDITY_REVERSAL", "FAILED_EXPANSION"}:
        regime_fit = 62.0
    structure_fit = 100.0 if structure.get("direction") == anchor.side else (
        45.0 if structure.get("direction") == "NEUTRAL" else 30.0
    )
    smt_support = 100.0
    if smt.get("available"):
        supports = smt.get("supports_long") if anchor.side == Side.LONG.value else smt.get("supports_short")
        contradicts = smt.get("supports_short") if anchor.side == Side.LONG.value else smt.get("supports_long")
        smt_support = 100.0 if supports else (25.0 if contradicts else 60.0)
    quality = clamp(
        0.40 * safe_float(htf.get("score"))
        + 0.22 * regime_fit
        + 0.20 * structure_fit
        + 0.18 * smt_support,
        0.0, 100.0,
    )
    return {
        "quality": round(quality, 2),
        "htf_state": htf.get("state"),
        "htf_score": safe_float(htf.get("score")),
        "htf_supports": bool(htf.get("supports")),
        "regime": regime,
        "regime_fit": round(regime_fit, 2),
        "structure_fit": round(structure_fit, 2),
        "smt_support": round(smt_support, 2),
    }


def build_candidate(
    context: dict[str, Any],
    anchor: Anchor,
    reaction: Reaction,
    degradation: dict[str, Any],
) -> Optional[Candidate]:
    """Turn a reacted anchor into a rankable, plannable candidate."""
    setup_type = anchor.setup_type
    profile = dict((degradation.get("setups") or {}).get(setup_type) or {})
    if not profile.get("executable", True):
        return None

    risk = max(safe_float((reaction.gates.get("GATE_STOP") or {}).get("distance")), ABS_MIN_STOP_DOLLARS, 1e-9)
    runway = nearest_runway_r(context, anchor.side, reaction.entry_price, risk)

    timing = _reaction_timing_quality(reaction)
    location = _location_quality(reaction, runway)
    smt = dict(context.get("smt") or {})
    context_quality = _context_quality(context, anchor, smt)
    setup_quality = clamp(safe_float(anchor.score), 0.0, 100.0)

    final_score = int(round(clamp(
        0.30 * setup_quality
        + 0.30 * timing["quality"]
        + 0.25 * location["quality"]
        + 0.15 * context_quality["quality"],
        0.0, 100.0,
    )))
    # entry_quality is the discriminative part: how good is THIS entry, right
    # now, at THIS level. It deliberately excludes the setup's static score.
    entry_quality = int(round(clamp(0.55 * timing["quality"] + 0.45 * location["quality"], 0.0, 100.0)))
    durability_quality = int(round(clamp(
        0.5 * context_quality["quality"] + 0.3 * location["quality"] + 0.2 * setup_quality, 0.0, 100.0,
    )))
    trade_quality = int(round(clamp(
        0.35 * setup_quality + 0.30 * entry_quality + 0.20 * durability_quality + 0.15 * context_quality["quality"],
        0.0, 100.0,
    )))

    episode_key = f"{anchor.setup_family}:{anchor.side}:{round(anchor.level, 4)}"
    freshness = round(clamp(100.0 - 6.0 * reaction.latency_minutes, 0.0, 100.0), 2)
    entry_distance_atr = safe_float((reaction.gates.get("GATE_PROXIMITY") or {}).get("distance_atr"))
    stop_distance_atr = safe_float((reaction.gates.get("GATE_STOP") or {}).get("distance_atr"))
    candidate = Candidate(
        side=anchor.side,
        setup_type=setup_type,
        setup_family=journal_setup_family(setup_type),
        raw_score=int(round(setup_quality)),
        final_score=final_score,
        score_components={
            "setup_quality": round(setup_quality, 2),
            "timing_quality": timing["quality"],
            "location_quality": location["quality"],
            "context_quality": context_quality["quality"],
            "weights": {"setup": 0.30, "timing": 0.30, "location": 0.25, "context": 0.15},
            "reaction": timing,
            "location": location,
            "context": context_quality,
            "runway": runway,
            "degradation": profile,
            "features": {
                "setup_quality": round(setup_quality / 100.0, 4),
                "timing_quality": round(timing["quality"] / 100.0, 4),
                "location_quality": round(location["quality"] / 100.0, 4),
                "context_quality": round(context_quality["quality"] / 100.0, 4),
                "entry_quality": round(entry_quality / 100.0, 4),
                "durability_quality": round(durability_quality / 100.0, 4),
                "htf_score": round(context_quality["htf_score"] / 100.0, 4),
                "regime_fit": round(context_quality["regime_fit"] / 100.0, 4),
                "structure_fit": round(context_quality["structure_fit"] / 100.0, 4),
                "smt_support": round(context_quality["smt_support"] / 100.0, 4),
                "freshness": round(freshness / 100.0, 4),
                "runway_r": round(safe_float(runway.get("runway_r")), 4),
                "entry_distance_atr": round(entry_distance_atr, 4),
                "stop_distance_atr": round(stop_distance_atr, 4),
                "reaction_latency_minutes": round(reaction.latency_minutes, 2),
            },
        },
        confirmations=[
            f"ANCHOR {anchor.kind} @ {_fmt_price(anchor.level)}",
            f"3M REJECTION strength={timing['rejection_strength']:.2f} body={timing['body_atr3']:.2f}ATR3",
            f"RUNWAY {runway.get('runway_r', 0.0):.2f}R to {(runway.get('nearest') or {}).get('kind', 'TARGET')}",
            f"HTF {context_quality['htf_state']} | REGIME {context_quality['regime']}",
        ],
        risks=[] if context_quality["htf_supports"] else [f"HTF {context_quality['htf_state']} проти напрямку"],
        trigger_ready=True,
        trigger_level=anchor.level,
        invalidation_level=anchor.invalidation,
        target_levels=[row["level"] for row in (runway.get("targets") or [])],
        confirmation_tier=3,
        stage="EXECUTABLE",
        execution_anchor=anchor.level,
        anchor_id=anchor.id,
        anchor_kind=anchor.kind,
        thesis_key=episode_key,
        thesis=anchor.reason,
        execution_source="ANCHOR_REACTION_3M",
        stage_plan={
            "anchor": anchor_to_dict(anchor),
            "reaction": dict(reaction.gates or {}),
            "reaction_latency_minutes": reaction.latency_minutes,
            "runway": runway,
            "innovation_profile": {"setup_trade_profile": {}, "profile_source": "organic_v10"},
        },
        risk_multiplier=safe_float(profile.get("risk_multiplier"), 1.0),
        setup_quality_score=int(round(setup_quality)),
        timing_quality_score=int(round(timing["quality"])),
        entry_quality_score=entry_quality,
        durability_quality_score=durability_quality,
        trade_quality_score=trade_quality,
        htf_fact=dict(context.get("htf_fact") or {}),
        reaction=dict(reaction.gates or {}),
        admission=profile,
        entry_quality=entry_quality,
        entry_freshness_score=freshness,
        evidence_adjusted_selection_score=round(final_score * safe_float(profile.get("risk_multiplier"), 1.0), 4),
        canonical_setup_family=anchor.setup_family,
        family_episode_key=episode_key,
    )
    return candidate


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Deterministic order: promoted setups first, then evidence-adjusted score."""
    def key(candidate: Candidate) -> tuple[float, float, str]:
        admission = dict(candidate.admission or {})
        status_rank = {"PROMOTED": 2.0, "NEUTRAL": 1.0, "INSUFFICIENT_SAMPLE": 0.0}.get(str(admission.get("status")), 0.0)
        return (status_rank, candidate.evidence_adjusted_selection_score, candidate.setup_type)

    ordered = sorted(candidates, key=key, reverse=True)
    for rank, candidate in enumerate(ordered, start=1):
        candidate.hypothesis_rank = rank
        candidate.competing_hypotheses = [
            {
                "setup_type": other.setup_type,
                "side": other.side,
                "final_score": other.final_score,
                "status": (other.admission or {}).get("status"),
            }
            for other in ordered if other is not candidate
        ][:JOURNAL_HYPOTHESIS_TOP]
    return ordered
# ==========================================================
# TRADE PLAN: GEOMETRY, TARGETS, PROBE/CORE RISK
# ==========================================================
# Ризик-модель PROBE/CORE збережена без змін. Змінилося лише те, звідки
# береться вхід: не "після закриття 15m-свічки", а ринком одразу після
# 3m-реакції на anchor. Геометрія стопів і тейків лишається структурною, бо її
# читає незмінний супровід (stop_initial, tp0..tp3, rr1..rr3, decision_stop,
# catastrophic_stop, breathing_profile, stage_plan).

TRADE_PLAN_SCHEMA_VERSION = "organic_trade_plan_v10.0.0"
EXECUTION_INTELLIGENCE_SCHEMA = "xi40"
RUNWAY_TARGET_SCHEMA_VERSION = "runway_target_management_v9.5.54"


def _quantile_v9542(values: list[float], q: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if value is not None and math.isfinite(safe_float(value, float("nan"))))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = clamp(float(q), 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def candle_noise_profile(context: dict[str, Any], price: float, atr15: float) -> dict[str, float]:
    """15M noise envelope used for the stop floor and the TP1 floor.

    Це не фільтр: широкий шум не скасовує угоду, він лише піднімає нижню межу
    стопа і зменшує sizing.
    """
    recent = _v9532_recent_confirmed(context, "15m", 48)
    tr_values = true_ranges(recent)
    fallback = max(float(atr15 or 0.0), price * 0.0012, ABS_MIN_STOP_DOLLARS * 0.50)
    p70 = percentile(tr_values, STOP_NOISE_PERCENTILE) or fallback
    p85 = max(percentile(tr_values, TP_NOISE_PERCENTILE) or 0.0, fallback, p70)
    current = 0.0
    rows = list((context.get("candles") or {}).get("15m") or [])
    if rows:
        last = rows[-1]
        current = max(safe_float(last.high) - safe_float(last.low), 0.0)
    return {
        "tr_p70": round(float(max(p70, fallback * 0.75)), 6),
        "tr_p85": round(float(p85), 6),
        "current_tr": round(float(max(current, fallback * 0.75)), 6),
        "fallback": round(float(fallback), 6),
        "sample_size": len(tr_values),
    }


# ==========================================================
# DAILY RISK ACCOUNTING
# ==========================================================

def compute_daily_risk_used(journal: dict[str, Any], at: Optional[datetime] = None) -> float:
    """Risk already consumed by trades closed on this UTC day.

    Open exposure is excluded on purpose — compute_open_position_risk() owns it,
    so one trade is never counted twice. Rows without a timestamp cannot be
    assigned to a day safely and are ignored rather than guessed.
    """
    day = (at or now_utc()).astimezone(timezone.utc).date()
    total = 0.0
    seen: set[tuple[str, str]] = set()
    for trade in (journal or {}).get("trades", []) or []:
        if not isinstance(trade, dict):
            continue
        closed_at = _parse_time_any(trade.get("closed_at"))
        if closed_at is None or closed_at.date() != day:
            continue
        key = (str(trade.get("id") or ""), str(trade.get("signal_id") or ""))
        if key != ("", "") and key in seen:
            continue
        seen.add(key)
        total += max(0.0, safe_float(trade.get("position_risk_pct", trade.get("risk_pct")), 0.0))
    return round(total, 4)


def compute_open_position_risk(state: dict[str, Any]) -> float:
    """Current open risk in the same percentage-point units as the cap."""
    raw = (state or {}).get("active_trade")
    if not isinstance(raw, dict):
        return 0.0
    if str(raw.get("status", "OPEN")).upper() == "CLOSED":
        return 0.0
    return round(max(0.0, safe_float(raw.get("position_risk_pct"), 0.0)), 4)


def daily_risk_budget(journal: dict[str, Any], state: dict[str, Any], requested_risk: float) -> dict[str, Any]:
    """How much of DAILY_RISK_CAP is still free, and what it does to the size."""
    used = compute_daily_risk_used(journal)
    open_risk = compute_open_position_risk(state)
    left_before = max(0.0, DAILY_RISK_CAP - used - open_risk)
    requested = max(0.0, safe_float(requested_risk))
    granted = min(requested, left_before)
    left_after = left_before - granted
    return {
        "daily_risk_cap": DAILY_RISK_CAP,
        "daily_risk_used": round(used, 4),
        "open_position_risk": round(open_risk, 4),
        "requested_risk_pct": round(requested, 6),
        "granted_risk_pct": round(granted, 6),
        "risk_budget_left_before": round(left_before, 4),
        "risk_budget_left": round(left_after, 4),
        "budget_min_buffer": RISK_BUDGET_MIN_BUFFER,
        "exhausted": bool(left_before <= 1e-9),
        "trimmed": bool(granted < requested - 1e-9),
        "thin": bool(left_before <= RISK_BUDGET_MIN_BUFFER),
        "schema_version": TRADE_PLAN_SCHEMA_VERSION,
    }


# ==========================================================
# ENTRY STAGE & PROBE CONVICTION  (модель збережена)
# ==========================================================

def classify_probe_conviction(candidate: Candidate, context: dict[str, Any]) -> dict[str, Any]:
    """HIGH / MEDIUM / EXPERIMENTAL — how much a PROBE is allowed to risk."""
    htf = htf_alignment_for_side(dict(context.get("htf_fact") or {}), candidate.side)
    state = str(htf.get("state") or "UNKNOWN").upper()
    timing = safe_float(candidate.timing_quality_score)
    entry_quality = safe_float(candidate.entry_quality)
    setup_quality = safe_float(candidate.setup_quality_score)
    displacement = dict((candidate.reaction or {}).get("GATE_DISPLACEMENT") or {})
    rejection = dict((candidate.reaction or {}).get("GATE_REJECTION") or {})
    trigger_strong = bool(
        safe_float(displacement.get("body_atr3")) >= REACTION_BODY_ATR3
        and safe_float(rejection.get("strength")) >= REACTION_REJECTION_RATIO
    )
    reversal_evidence = candidate.setup_family in {
        SetupFamily.LIQUIDITY_RECOVERY.value, SetupFamily.STRUCTURAL_TRANSITION.value,
    } or candidate.anchor_kind in {AnchorKind.SWEEP_LOW.value, AnchorKind.SWEEP_HIGH.value}

    if state == "ALIGNED" and timing >= 68 and entry_quality >= 60 and trigger_strong:
        tier, reason = "HIGH", "HTF ALIGNED + strong 3m rejection and displacement"
    elif state in {"MIXED", "NEUTRAL", "UNKNOWN"} and timing >= 58 and entry_quality >= 50 and trigger_strong:
        tier, reason = "MEDIUM", f"HTF {state} + strong 3m reaction"
    elif state == "AGAINST" and reversal_evidence:
        tier, reason = "EXPERIMENTAL", "HTF AGAINST but the setup is a reversal off swept liquidity"
    elif setup_quality >= 65 and timing >= 55:
        tier, reason = "MEDIUM", "setup quality carries a moderate-timing probe"
    else:
        tier, reason = "EXPERIMENTAL", "no tier earned — smallest probe only"
    return {
        "tier": tier,
        "reason": reason,
        "htf_state": state,
        "timing_quality": round(timing, 2),
        "entry_quality": round(entry_quality, 2),
        "setup_quality": round(setup_quality, 2),
        "trigger_strong": trigger_strong,
        "reversal_evidence": bool(reversal_evidence),
    }


def _probe_risk_pct(conviction: dict[str, Any], admission: dict[str, Any]) -> float:
    tier = str(conviction.get("tier") or "EXPERIMENTAL").upper()
    base = {
        "HIGH": HIGH_CONVICTION_PROBE_RISK_PCT,
        "MEDIUM": MEDIUM_CONVICTION_PROBE_RISK_PCT,
    }.get(tier, EXPERIMENTAL_PROBE_RISK_PCT)
    status = str(admission.get("status") or "").upper()
    if status == "INSUFFICIENT_SAMPLE" and safe_float(admission.get("risk_multiplier"), 1.0) < 1.0:
        # A setup from a negative-expectancy family gets the validation ladder,
        # never the full probe size.
        base = min(base, UNDERPERFORMING_SETUP_VALIDATION_PROBE_RISK_PCT)
    elif status == "NEUTRAL" and not admission.get("core_eligible", True):
        base = min(base, UNDERPERFORMING_SETUP_VALIDATION_PROBE_RISK_PCT)
    return base


def resolve_entry_stage(
    candidate: Candidate, context: dict[str, Any], journal: dict[str, Any],
) -> dict[str, Any]:
    """CORE / ACCEPTANCE / PROBE — the three stages the unchanged supervision knows.

    ACCEPTANCE is not a waiting room here: the 3m reaction already IS the
    acceptance of the level. It is the stage for a reaction good enough to trade
    at acceptance size but not good enough for full CORE risk, and it activates
    the v9.5.35 fixed-horizon no-followthrough exit in supervision.
    """
    admission = dict(candidate.admission or {})
    conviction = classify_probe_conviction(candidate, context)
    score = safe_float(candidate.final_score)
    htf = htf_alignment_for_side(dict(context.get("htf_fact") or {}), candidate.side)
    regime = str(context.get("regime") or "").upper()
    measured = safe_int(((compute_setup_statistics(journal) or {}).get("measured_trades")))

    core_ok = bool(
        score >= MIN_SCORE_CORE
        and htf.get("supports")
        and str(htf.get("state")) == "ALIGNED"
        and regime != Regime.SHOCK.value
        and admission.get("core_eligible", True)
        and str(admission.get("status")) in {"PROMOTED", "NEUTRAL"}
        and measured >= BOOTSTRAP_MIN_CLOSED_TRADES
    )
    acceptance_ok = bool(
        not core_ok
        and score >= MIN_SCORE_ACCEPTANCE
        and htf.get("supports")
        and regime != Regime.SHOCK.value
        and admission.get("executable", True)
    )
    if core_ok:
        stage, reason = EntryStage.CORE.value, "CORE: full-size aligned entry"
    elif acceptance_ok:
        stage, reason = EntryStage.ACCEPTANCE.value, "ACCEPTANCE: level accepted, sub-core conviction"
    else:
        stage, reason = EntryStage.PROBE.value, f"PROBE ({conviction.get('tier')}): {conviction.get('reason')}"
    return {
        "entry_stage": stage,
        "reason": reason,
        "probe_conviction": conviction,
        "core_eligible": core_ok,
        "acceptance_eligible": acceptance_ok,
        "score": round(score, 2),
        "htf_state": htf.get("state"),
        "regime": regime,
        "bootstrap_complete": bool(measured >= BOOTSTRAP_MIN_CLOSED_TRADES),
    }


def position_risk_pct(stage: str, conviction: dict[str, Any], admission: dict[str, Any], candidate: Candidate, context: dict[str, Any], journal: dict[str, Any]) -> dict[str, Any]:
    """Base stage risk, then every multiplier that can only shrink it."""
    stage = str(stage or EntryStage.PROBE.value).upper()
    if stage == EntryStage.CORE.value:
        base = CORE_RISK_PCT
    elif stage == EntryStage.ACCEPTANCE.value:
        base = ACCEPTANCE_RISK_PCT
    else:
        base = _probe_risk_pct(conviction, admission)

    breakdown: list[dict[str, Any]] = [{"factor": "STAGE_BASE", "multiplier": 1.0, "value": round(base, 6)}]
    effective = base

    def apply(name: str, multiplier: float, note: str) -> None:
        nonlocal effective
        multiplier = clamp(safe_float(multiplier, 1.0), 0.0, 1.0)
        if multiplier >= 1.0 - 1e-9:
            return
        effective *= multiplier
        breakdown.append({"factor": name, "multiplier": round(multiplier, 4), "value": round(effective, 6), "note": note})

    entry_quality = safe_float(candidate.entry_quality)
    if entry_quality < ENTRY_QUALITY_VERY_LOW:
        apply("ENTRY_QUALITY_VERY_LOW", ENTRY_QUALITY_VERY_LOW_RISK_MULT, f"entry_quality {entry_quality:.0f} < {ENTRY_QUALITY_VERY_LOW}")
    elif entry_quality < ENTRY_QUALITY_LOW:
        apply("ENTRY_QUALITY_LOW", ENTRY_QUALITY_LOW_RISK_MULT, f"entry_quality {entry_quality:.0f} < {ENTRY_QUALITY_LOW}")

    htf = htf_alignment_for_side(dict(context.get("htf_fact") or {}), candidate.side)
    if not htf.get("supports"):
        apply("WEAK_DIRECTION", WEAK_DIRECTION_RISK_MULTIPLIER, f"HTF {htf.get('state')} does not support {candidate.side}")

    measured = safe_int((compute_setup_statistics(journal) or {}).get("measured_trades"))
    if measured < BOOTSTRAP_MIN_CLOSED_TRADES:
        apply("BOOTSTRAP", BOOTSTRAP_RISK_MULTIPLIER, f"only {measured}/{BOOTSTRAP_MIN_CLOSED_TRADES} measured closed trades")

    degradation_multiplier = clamp(safe_float(admission.get("risk_multiplier"), 1.0), 0.0, 1.0)
    apply("SETUP_DEGRADATION", degradation_multiplier, f"auto-degradation status {admission.get('status')}")

    if str(context.get("regime") or "").upper() == Regime.SHOCK.value:
        apply("SHOCK_REGIME", RISKY_GRAY_RISK_PCT / max(PROBE_RISK_PCT, 1e-9), "impulse regime — risky-gray sizing")

    return {
        "stage": stage,
        "base_risk_pct": round(base, 6),
        "effective_risk_pct": round(effective, 6),
        "breakdown": breakdown,
        "entry_quality": round(entry_quality, 2),
        "htf_supports": bool(htf.get("supports")),
        "bootstrap_complete": bool(measured >= BOOTSTRAP_MIN_CLOSED_TRADES),
        "degradation_multiplier": round(degradation_multiplier, 4),
        "schema_version": TRADE_PLAN_SCHEMA_VERSION,
    }


# ==========================================================
# PAST-ONLY MANAGEMENT CALIBRATION (читає незмінний супровід)
# ==========================================================

def setup_management_calibration_profile_v9542(
    journal: Optional[dict[str, Any]], setup_type: str, side: str = "", execution_source: str = "",
) -> dict[str, Any]:
    """Past-only calibration for positive-excursion protection.

    The profile cannot change admission, initial stop, targets, RR or risk. Live
    authority starts only at the exact-setup geometry sample floor and with
    enough winning paths to estimate their giveback distribution.
    """
    setup = str(setup_type or "").upper()
    side = str(side or "").upper()
    source = str(execution_source or "")
    rows: list[dict[str, Any]] = []
    for trade in ((journal or {}).get("trades") or []):
        if not isinstance(trade, dict) or str(trade.get("setup_type") or "").upper() != setup:
            continue
        result_r = _journal_result_r(trade)
        if result_r is None or trade.get("mfe_r") is None or trade.get("mae_r") is None:
            continue
        rows.append(trade)

    scopes: list[tuple[str, list[dict[str, Any]]]] = []
    if side and source and source != "NONE":
        scopes.append((
            "EXACT_SETUP_SIDE_SOURCE",
            [row for row in rows if str(row.get("side") or "").upper() == side and str(row.get("execution_source") or "") == source],
        ))
    if side:
        scopes.append(("EXACT_SETUP_SIDE", [row for row in rows if str(row.get("side") or "").upper() == side]))
    scopes.append(("EXACT_SETUP", rows))

    diagnostic_scope, diagnostic_rows = scopes[0]
    chosen_scope = ""
    chosen_rows: list[dict[str, Any]] = []
    for scope, scoped_rows in scopes:
        winners = [row for row in scoped_rows if safe_float(_journal_result_r(row), 0.0) > 0.0]
        if len(scoped_rows) >= SETUP_TRADE_PROFILE_MIN_CLOSED_TRADES and len(winners) >= ROUTE_GEOMETRY_MIN_TRADES:
            chosen_scope, chosen_rows = scope, scoped_rows
            break

    evidence_rows = chosen_rows or diagnostic_rows
    winner_rows = [row for row in evidence_rows if safe_float(_journal_result_r(row), 0.0) > 0.0]
    winner_mfe = [max(0.0, safe_float(row.get("mfe_r"), 0.0)) for row in winner_rows]
    winner_giveback: list[float] = []
    for row in winner_rows:
        mfe = max(safe_float(row.get("mfe_r"), 0.0), 1e-9)
        explicit = row.get("mfe_giveback_ratio")
        if explicit is None:
            capture = row.get("mfe_capture_ratio")
            if capture is None:
                capture = max(0.0, safe_float(_journal_result_r(row), 0.0)) / mfe
            explicit = 1.0 - clamp(safe_float(capture, 0.0), 0.0, 1.0)
        winner_giveback.append(clamp(safe_float(explicit, 0.0), 0.0, 1.0))

    activation_raw = _quantile_v9542(winner_mfe, 0.25)
    giveback_raw = _quantile_v9542(winner_giveback, 0.50)
    authority_active = bool(chosen_rows and activation_raw is not None and giveback_raw is not None)
    activation = clamp(safe_float(activation_raw, PATH_DECAY_MIN_MFE_R), PATH_DECAY_MIN_MFE_R, 1.25)
    giveback = clamp(safe_float(giveback_raw, PATH_DECAY_MIN_GIVEBACK), 0.45, 0.68)
    return {
        "setup_type": setup,
        "side": side,
        "execution_source": source,
        "selected_scope": chosen_scope or f"{diagnostic_scope}_AUDIT_ONLY",
        "geometry_complete_rows": len(evidence_rows),
        "winner_rows": len(winner_rows),
        "minimum_rows": SETUP_TRADE_PROFILE_MIN_CLOSED_TRADES,
        "minimum_winner_paths": ROUTE_GEOMETRY_MIN_TRADES,
        "authority_active": authority_active,
        "activation_mfe_r": round(activation, 4),
        "giveback_threshold": round(giveback, 4),
        "bootstrap_activation_mfe_r": PATH_DECAY_MIN_MFE_R,
        "bootstrap_giveback_threshold": PATH_DECAY_MIN_GIVEBACK,
        "initial_stop_mutated": False,
        "targets_mutated": False,
        "rr_floor_mutated": False,
        "position_risk_mutated": False,
        "setup_blocked": False,
        "policy": "PAST_ONLY_POSITIVE_EXCURSION_PROTECTION; MONOTONIC_STOP_ONLY; FAILS_TO_BOOTSTRAP",
        "schema_version": SETUP_MANAGEMENT_SCHEMA_V9542,
    }


def execution_intelligence_profile(
    context: dict[str, Any], candidate: Candidate, journal: dict[str, Any], runway: dict[str, Any],
) -> dict[str, Any]:
    """The compact xi40 record supervision reads for reaction_window and hazard_30.

    Everything here is measured, never forecast: the hazard rows come from this
    setup's own closed trades, and the defaults are the ones supervision already
    falls back to when there is no history.
    """
    family = str(candidate.canonical_setup_family or canonical_setup_family(candidate.setup_type))
    rows: list[dict[str, Any]] = []
    for trade in (journal or {}).get("trades", []) or []:
        if not isinstance(trade, dict):
            continue
        setup_type = str(trade.get("setup_type") or "").upper()
        if canonical_setup_family(setup_type) != family:
            continue
        if _journal_result_r(trade) is None or trade.get("mfe_r") is None:
            continue
        rows.append(trade)

    hazard = {key: 0.0 for key in ("15", "30", "60")}
    reaction_window = 45.0
    reach_times: list[float] = []
    if rows:
        for row in rows:
            if safe_float(row.get("tp0_hit_ts"), 0.0) <= 0.0 or safe_float(row.get("opened_at_ms"), 0.0) <= 0.0:
                continue
            minutes = (safe_float(row.get("tp0_hit_ts")) - safe_float(row.get("opened_at_ms"))) / 60000.0
            if 0.0 < minutes < 600.0:
                reach_times.append(minutes)
        for row in rows:
            age = safe_float(row.get("age_minutes"), 0.0)
            reached = safe_float(row.get("mfe_r"), 0.0) >= PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R
            if age <= 0.0:
                continue
            for key, horizon in (("15", 15.0), ("30", 30.0), ("60", 60.0)):
                if age >= horizon:
                    hazard[key] = clamp(hazard[key] + (1.0 if reached else 0.0), 0.0, float(len(rows)))
        for key in hazard:
            hazard[key] = round(clamp(hazard[key] / max(len(rows), 1), 0.0, 1.0), 3)
        if reach_times:
            reaction_window = round(clamp(percentile(reach_times, 0.5), 12.0, 120.0), 1)

    return {
        "router": "ANCHOR_REACTION_3M",
        "state": "EXECUTABLE",
        "kind": str(candidate.anchor_kind or ""),
        "asi": round(clamp(100.0 - safe_float(candidate.entry_freshness_score), 0.0, 100.0), 2),
        "structural": round(safe_float(candidate.setup_quality_score), 2),
        "runway_r": round(safe_float(runway.get("runway_r"), MIN_RUNWAY_R), 3),
        "regime": str(context.get("regime") or Regime.NORMAL.value),
        "regime_bias": str((context.get("structure15") or {}).get("direction") or "NEUTRAL"),
        "regime_fit": round(safe_float((candidate.score_components.get("context") or {}).get("regime_fit"), 50.0) / 100.0, 3),
        "regime_uncertainty": 0.5,
        "setup_pct": round(clamp(safe_float(candidate.final_score) / 100.0, 0.0, 1.0), 3),
        "ctx_edge": round(clamp(safe_float((candidate.score_components.get("context") or {}).get("quality"), 50.0) / 100.0, 0.0, 1.0), 3),
        "outcome_p": 0.5,
        "outcome_uncertainty": 0.5,
        "hazard_15": hazard["15"],
        "hazard_30": hazard["30"],
        "hazard_60": hazard["60"],
        "hazard_reliability": round(clamp(len(rows) / max(BOOTSTRAP_MIN_CLOSED_TRADES, 1), 0.0, 1.0), 3),
        "reaction_window": reaction_window,
        "geo_n": len(rows),
        "assistant_opinions": {},
        "assistant_metrics": {},
        "assistant_feedback_complete": False,
        "schema": EXECUTION_INTELLIGENCE_SCHEMA,
    }


def runway_target_management_profile(
    context: dict[str, Any], candidate: Candidate, entry: float, stop: float,
    tp0: float, tp1: float, technical_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep the structural TP0; sub-1R liquidity is audit-only in classic mode.

    Supervision (v9.5.70) owns the stop until the delayed TP1 lock, so a nearby
    liquidity pool must never reprice the first partial. It is reported so the
    journal can show what the market actually had in front of the trade.
    """
    sign = side_sign(str(candidate.side or Side.NEUTRAL.value))
    atr15 = max(safe_float(context.get("atr15"), 0.0), abs(entry) * 0.001, 1e-6)
    risk = max(abs(entry - stop), ABS_MIN_STOP_DOLLARS, 1e-9)
    stage = str(candidate.entry_stage or (candidate.stage_plan or {}).get("stage") or "").upper()

    live: list[dict[str, Any]] = []
    for target in technical_targets or []:
        level = safe_float(target.get("level"), 0.0)
        directional = sign * (level - entry)
        if level <= 0.0 or directional <= max(0.01 * atr15, 1e-6):
            continue
        live.append({**target, "directional_distance": directional})
    live.sort(key=lambda row: safe_float(row.get("directional_distance"), float("inf")))
    selected = live[0] if live else None
    runway_r = safe_float(selected.get("directional_distance"), 0.0) / risk if selected else 0.0
    selected_level = safe_float(selected.get("level"), 0.0) if selected else 0.0

    classic_distance = max(abs(tp0 - entry), risk * TP0_MIN_RR)
    classic_tp0 = entry + sign * classic_distance if sign else tp0
    tp1_distance = sign * (tp1 - entry) if sign else 0.0
    if tp1_distance > 0.0 and classic_distance >= tp1_distance:
        # Target ordering stays fail-closed if a custom configuration violates
        # the RR ratchet premise.
        classic_tp0, classic_distance = tp0, abs(tp0 - entry)

    return {
        "state": "STRUCTURAL_TP0_CLASSIC",
        "entry_stage": stage,
        "tp0": round_price(classic_tp0),
        "tp0_rr": round(classic_distance / risk, 4),
        "runway_r": round(runway_r, 4) if selected else None,
        "nearest_target_level": round_price(selected_level) if selected else None,
        "nearest_target_kind": str((selected or {}).get("kind") or "UNAVAILABLE"),
        "scout_partial": False,
        "explicit_reprice": False,
        "nearest_liquidity_audit_only": bool(selected_level),
        "original_scout_partial_suppressed": bool(
            stage == EntryStage.PROBE.value and selected
            and RUNWAY_SCOUT_TP0_MIN_R <= runway_r < TP0_MIN_RR
        ),
        "setup_blocked": False,
        "stop_policy": "INITIAL_STRUCTURAL_UNTIL_DELAYED_TP1_LOCK",
        "schema_version": RUNWAY_TARGET_SCHEMA_VERSION,
        "schema_version_v9570": V9570_SCHEMA_VERSION,
    }


# ==========================================================
# PLAN BUILDER
# ==========================================================

def _plan_geometry(
    context: dict[str, Any], candidate: Candidate, entry: float, sign: int,
) -> dict[str, Any]:
    """Stop, catastrophic stop and TP0..TP3 distances in dollars."""
    atr15 = _effective_atr15(safe_float(context.get("atr15"), 0.0), entry)
    noise = candle_noise_profile(context, entry, atr15)
    reaction_stop = dict((candidate.reaction or {}).get("GATE_STOP") or {})
    structural_stop = safe_float(reaction_stop.get("stop"), 0.0)
    if structural_stop <= 0.0:
        structural_stop = safe_float(candidate.invalidation_level, 0.0)
    structural_distance = abs(entry - structural_stop)

    # The reaction gate already proved the structural stop is inside
    # MAX_STOP_ATR. The noise floor can only lift an absurdly tight stop; if it
    # has to lift past the early-entry cap, the level is not tradeable early and
    # the plan fails closed instead of silently becoming a late, wide entry.
    noise_floor = max(
        ABS_MIN_STOP_DOLLARS,
        noise["tr_p70"] * MIN_STOP_TRUE_RANGE_MULT * 0.60,
        noise["current_tr"] * CURRENT_CANDLE_STOP_MULT * 0.60,
        atr15 * MIN_STOP_ATR15 * 0.45,
    )
    decision_distance = max(structural_distance, noise_floor)
    cap_distance = MAX_STOP_ATR * atr15
    if decision_distance > cap_distance:
        return {
            "valid": False,
            "reason": f"STOP_NOISE_FLOOR_{decision_distance / max(atr15, 1e-9):.2f}ATR_EXCEEDS_EARLY_ENTRY_CAP",
            "atr15": atr15, "noise": noise,
        }

    min_extra = max(entry * 0.0005, atr15 * 0.15)
    requested_extra = max(decision_distance * max(CATASTROPHIC_STOP_MULT - 1.0, 0.0), min_extra)
    capped_extra = min(requested_extra, atr15 * CATASTROPHIC_STOP_MAX_EXTRA_ATR)
    catastrophic_distance = max(noise_floor, decision_distance + capped_extra)

    tp1_floor = max(
        catastrophic_distance * TP1_MIN_RR_PRO,
        ABS_MIN_TP1_DOLLARS,
        noise["tr_p85"] * MIN_TP1_TRUE_RANGE_MULT,
        atr15 * TP1_MIN_ATR_PRO,
    )
    tp0_floor = max(
        catastrophic_distance * TP0_MIN_RR,
        noise["tr_p70"],
        atr15 * 1.10,
    )
    tp1_distance = max(tp1_floor, decision_distance * max(MIN_RR1, PREFERRED_RR1))
    tp2_distance = max(tp1_distance * 1.05, decision_distance * MIN_RR2, atr15 * max(MIN_TP1_ATR15, TP1_MIN_ATR_PRO))
    tp3_distance = max(tp2_distance * 1.05, decision_distance * MIN_RR3)

    return {
        "valid": True,
        "reason": "",
        "atr15": atr15,
        "noise": noise,
        "structural_stop": round_price(structural_stop),
        "structural_distance": round(structural_distance, 6),
        "decision_distance": round(decision_distance, 6),
        "catastrophic_distance": round(catastrophic_distance, 6),
        "noise_floor_distance": round(noise_floor, 6),
        "cap_distance": round(cap_distance, 6),
        "decision_stop": round_price(entry - sign * decision_distance),
        "catastrophic_stop": round_price(entry - sign * catastrophic_distance),
        "tp0_distance": round(tp0_floor, 6),
        "tp1_distance": round(tp1_distance, 6),
        "tp2_distance": round(tp2_distance, 6),
        "tp3_distance": round(tp3_distance, 6),
        "risk_size_multiplier": round(
            clamp(structural_distance / max(catastrophic_distance, structural_distance, 1e-9), MIN_BREATHING_RISK_MULTIPLIER, 1.0)
            if structural_distance > 1e-9 else 1.0, 4,
        ),
        "stop_basis": "ANCHOR_INVALIDATION_GUARDED_BY_3M_REJECTION_EXTREME",
        "target_basis": "STRUCTURAL_RR_LADDER_WITH_15M_NOISE_FLOOR",
    }


def build_trade_plan(
    context: dict[str, Any],
    candidate: Candidate,
    entry_price_override: Optional[float] = None,
    journal: Optional[dict[str, Any]] = None,
    state: Optional[dict[str, Any]] = None,
) -> TradePlan:
    """One executable plan: market entry at the reacted anchor, structural ladder."""
    journal = journal if isinstance(journal, dict) else {}
    persisted_state = state if isinstance(state, dict) else {}
    price = safe_float(context.get("price"), 0.0)
    entry = safe_float(entry_price_override, 0.0) or price
    sign = side_sign(str(candidate.side or ""))
    conviction = dict((candidate.stage_plan or {}).get("probe_conviction") or {})
    if not conviction:
        conviction = classify_probe_conviction(candidate, context)
        candidate.stage_plan = dict(candidate.stage_plan or {})
        candidate.stage_plan["probe_conviction"] = copy.deepcopy(conviction)
    risk_ledger = position_risk_pct(
        candidate.entry_stage, conviction, dict(candidate.admission or {}),
        candidate, context, journal,
    )
    geometry = _plan_geometry(context, candidate, entry, sign)
    if not sign or entry <= 0.0 or not geometry.get("valid"):
        return TradePlan(
            entry=round_price(entry), stop=0.0, tp1=0.0, tp2=0.0, tp3=0.0,
            risk_pct=0.0, rr1=0.0, rr2=0.0, rr3=0.0,
            entry_stage=candidate.entry_stage, execution_source=candidate.execution_source,
            stage_plan=dict(candidate.stage_plan or {}), valid=False,
            reason=str(geometry.get("reason") or "NO_DIRECTIONAL_PRICE"),
            risk_ledger=risk_ledger, final_stage="NOT_EXECUTABLE",
        )

    risk = max(geometry["decision_distance"], ABS_MIN_STOP_DOLLARS, 1e-9)
    stop = geometry["decision_stop"]
    tp0 = round_price(entry + sign * geometry["tp0_distance"])
    tp1 = round_price(entry + sign * geometry["tp1_distance"])
    tp2 = round_price(entry + sign * geometry["tp2_distance"])
    tp3 = round_price(entry + sign * geometry["tp3_distance"])

    targets = technical_targets(context, candidate.side, entry)
    runway = nearest_runway_r(context, candidate.side, entry, risk)
    runway_profile = runway_target_management_profile(context, candidate, entry, stop, tp0, tp1, targets)
    calibration = setup_management_calibration_profile_v9542(
        journal, candidate.setup_type, candidate.side, candidate.execution_source,
    )
    intelligence = execution_intelligence_profile(context, candidate, journal, runway)

    budget = daily_risk_budget(journal, persisted_state, risk_ledger["effective_risk_pct"])
    granted = safe_float(budget.get("granted_risk_pct"))
    stage = str(candidate.entry_stage or EntryStage.PROBE.value).upper()
    if budget.get("exhausted"):
        return TradePlan(
            entry=round_price(entry), stop=round_price(stop), tp1=tp1, tp2=tp2, tp3=tp3,
            risk_pct=0.0, rr1=0.0, rr2=0.0, rr3=0.0, tp0=tp0,
            entry_stage=stage, execution_source=candidate.execution_source,
            stage_plan=dict(candidate.stage_plan or {}), valid=False,
            reason="DAILY_RISK_BUDGET_EXHAUSTED", risk_ledger=risk_ledger,
            final_stage="NOT_EXECUTABLE",
        )
    if stage == EntryStage.CORE.value and granted < CORE_MIN_RISK_PCT_EFFECTIVE:
        # A CORE entry the budget cannot actually fund is demoted, not blocked:
        # the reaction is still valid, only the size is not. Re-running the full
        # ladder matters — a demoted CORE must not end up larger than the PROBE
        # it becomes.
        stage = EntryStage.PROBE.value
        candidate.entry_stage = stage
        risk_ledger = position_risk_pct(
            stage, conviction, dict(candidate.admission or {}),
            candidate, context, journal,
        )
        risk_ledger["breakdown"].append({
            "factor": "CORE_DEMOTED_BY_BUDGET", "multiplier": 1.0,
            "value": risk_ledger["effective_risk_pct"],
            "note": f"budget granted {granted:.4f}% < CORE_MIN_RISK_PCT_EFFECTIVE",
        })
        budget = daily_risk_budget(journal, persisted_state, risk_ledger["effective_risk_pct"])
        granted = safe_float(budget.get("granted_risk_pct"))

    position_risk = round(min(granted, safe_float(risk_ledger["effective_risk_pct"])), 6)
    if position_risk <= 0.0:
        return TradePlan(
            entry=round_price(entry), stop=round_price(stop), tp1=tp1, tp2=tp2, tp3=tp3,
            risk_pct=0.0, rr1=0.0, rr2=0.0, rr3=0.0, tp0=tp0,
            entry_stage=stage, execution_source=candidate.execution_source,
            stage_plan=dict(candidate.stage_plan or {}), valid=False,
            reason="POSITION_RISK_ROUNDED_TO_ZERO", risk_ledger=risk_ledger,
            final_stage="NOT_EXECUTABLE",
        )

    stage_plan = dict(candidate.stage_plan or {})
    stage_plan.update({
        "stage": stage,
        "execution_route_contract_v9568": {
            "mode": "MARKET_ON_3M_REACTION",
            "queue_for_one_material_event": False,
            "required_next_event": "NONE",
            "filled_by": "ANCHOR_REACTION_3M",
        },
        "router_requirement_type": "MARKET_ON_3M_REACTION",
        "required_next_event": "NONE",
        "queue_for_one_material_event": False,
        "setup_management_calibration_v9542": copy.deepcopy(calibration),
        "runway_target_management": copy.deepcopy(runway_profile),
        "execution_intelligence_v9532": copy.deepcopy(intelligence),
        "risk_ledger": copy.deepcopy(risk_ledger),
        "daily_risk_budget": copy.deepcopy(budget),
        "geometry": {
            key: value for key, value in geometry.items() if key not in {"noise"}
        },
        "noise_profile": geometry["noise"],
        "schema_version": TRADE_PLAN_SCHEMA_VERSION,
    })

    breathing_profile = {
        "decision_distance": geometry["decision_distance"],
        "catastrophic_distance": geometry["catastrophic_distance"],
        "tp0_floor_distance": geometry["tp0_distance"],
        "tp1_floor_distance": geometry["tp1_distance"],
        "risk_size_multiplier": geometry["risk_size_multiplier"],
        "noise": geometry["noise"],
        "setup_management_calibration_v9542": copy.deepcopy(calibration),
        "schema_version": TRADE_PLAN_SCHEMA_VERSION,
    }

    candidate.entry_stage = stage
    candidate.stage_plan = stage_plan

    return TradePlan(
        entry=round_price(entry),
        stop=round_price(stop),
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_pct=position_risk,
        rr1=round(geometry["tp1_distance"] / risk, 4),
        rr2=round(geometry["tp2_distance"] / risk, 4),
        rr3=round(geometry["tp3_distance"] / risk, 4),
        position_risk_pct=position_risk,
        invalidation="STRUCTURAL_CLOSE_BEYOND_ANCHOR_INVALIDATION",
        stop_basis=geometry["stop_basis"],
        target_basis=geometry["target_basis"],
        stop_timeframe="15M",
        structural_invalidation=round_price(safe_float(candidate.invalidation_level)),
        trigger_level=round_price(safe_float(candidate.trigger_level)),
        execution_ready=True,
        tp0=tp0,
        rr0=round(geometry["tp0_distance"] / risk, 4),
        entry_stage=stage,
        execution_source=str(candidate.execution_source or "ANCHOR_REACTION_3M"),
        stage_plan=stage_plan,
        partial_plan={
            "TP0": TP0_SIZE_PCT, "TP1": TP1_SIZE_PCT, "TP2": TP2_SIZE_PCT, "TP3": TP3_RUNNER_PCT,
        },
        runtime_config_snapshot={
            "leverage": LEVERAGE,
            "daily_risk_cap": DAILY_RISK_CAP,
            "normal_risk_pct": NORMAL_RISK_PCT,
            "probe_risk_pct": PROBE_RISK_PCT,
            "acceptance_risk_pct": ACCEPTANCE_RISK_PCT,
            "core_risk_pct": CORE_RISK_PCT,
            "tp0_rr": TP0_RR,
            "tp0_min_rr": TP0_MIN_RR,
            "min_rr1": MIN_RR1,
            "min_rr2": MIN_RR2,
            "min_rr3": MIN_RR3,
            "max_stop_atr": MAX_STOP_ATR,
            "min_runway_r": MIN_RUNWAY_R,
            "bot_version": BOT_VERSION,
            "architecture_version": ARCHITECTURE_VERSION,
        },
        decision_stop=round_price(stop),
        catastrophic_stop=round_price(geometry["catastrophic_stop"]),
        breathing_profile=breathing_profile,
        valid=True,
        reason="ANCHOR_REACTION_MARKET_ENTRY",
        risk_ledger=risk_ledger,
        final_stage=stage,
        immutable=True,
    )
# ==========================================================
# TRADE SUPERVISION  (НЕЗМІННИЙ БЛОК)
# ==========================================================
# Цей розділ перенесено дослівно зі старого бота: стопи, часткові фіксації
# TP0/TP1/TP2/TP3, BE-delay, структурний трейлінг, TP0 high-water ratchet,
# path-decay defense, acceptance no-followthrough і thesis-invalidation exit.
# Змінено лише одне — ланцюг перевизначень manage_active_trade розгорнуто в
# одну функцію на ім'я. Порядок викликів і тіла функцій ідентичні:
#
#   _manage_active_trade_core                 (був manage_active_trade @22496)
#     -> _manage_active_trade_path_decay       (v9531 @26134)
#     -> _manage_active_trade_acceptance_exit  (v9534 @29807)
#     -> _manage_active_trade_thesis_invalidation (v9552 @33498)
#     -> manage_active_trade_v9570             (ефективний, @47564)
#
# Ім'я _active_path_integrity_v9532 збережене навмисно: супровід шукає його
# через globals() під час виклику.


def _tp0_giveback_innovation_advisor(trade: ActiveTrade, context: dict, result: dict[str, Any]) -> None:
    """Secondary audit note after v8.11 protection. The actual stop movement is
    owned by _tp0_profit_protection; this advisor only records the construction context."""
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
            f"v8.11 MFE-capture audit: після TP0 віддано {giveback_ratio:.0%} MFE; "
            "новий добір заборонений до повторного acceptance-close"
        )
        result["innovation_management"] = {
            "mode": "TP0_GIVEBACK_DEFENSE",
            "mfe_r": round(mfe_r, 2),
            "current_r": round(cur_r, 2),
            "giveback_ratio": round(giveback_ratio, 2),
            "auto_close": False,
            "stop_move": bool(getattr(trade, "pre_tp1_protection_locked", False)),
        }


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


def _journal_result_r(record: dict[str, Any]) -> Optional[float]:
    """Read only an explicitly stored R-multiple; never reinterpret percentages as R."""
    if not isinstance(record, dict):
        return None
    raw = record.get("result_r", record.get("pnl_r"))
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _journal_realized_return_pct(record: dict[str, Any]) -> Optional[float]:
    """Read the weighted realized price return, with legacy result_pct fallback."""
    if not isinstance(record, dict):
        return None
    raw = record.get("realized_return_pct")
    if raw is None:
        raw = record.get("result_pct")
    if raw is None:
        raw = record.get("pnl")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _directional_price_move(side: str, entry: float, exit_price: float) -> float:
    return (exit_price - entry) if side == Side.LONG.value else (entry - exit_price)


def _realized_trade_metrics(
    trade: ActiveTrade,
    final_exit_price: Optional[float],
    *,
    outcome_status: str = "RESOLVED",
    ambiguous_stop_price: Optional[float] = None,
    ambiguous_target_price: Optional[float] = None,
) -> dict[str, Any]:
    """Calculate weighted realized return while preserving known partial exits.

    Ambiguous same-bar ordering keeps the total result unresolved and ML-ineligible,
    but earlier confirmed TP legs remain economically real and are never erased.
    """
    entry = float(trade.entry)
    risk = abs(entry - float(trade.stop_initial or 0.0))
    if entry <= 0 or risk <= 1e-12:
        return {
            "result_r": None,
            "realized_return_pct": None,
            "known_realized_r": None,
            "known_realized_return_pct": None,
            "result_unit": "R",
            "outcome_status": "INVALID_GEOMETRY",
            "ml_eligible": False,
            "realized_legs": [],
            "known_realized_legs": [],
            "remaining_size_pct": None,
            "result_r_min": None,
            "result_r_max": None,
        }

    legs: list[dict[str, Any]] = []
    closed_size = 0.0

    def add_leg(label: str, size: float, price: float) -> None:
        nonlocal closed_size
        size = clamp(safe_float(size, 0.0), 0.0, max(0.0, 1.0 - closed_size))
        if size <= 0:
            return
        move = _directional_price_move(trade.side, entry, float(price))
        leg_r = size * move / risk
        leg_return_pct = size * move / entry * 100.0
        legs.append({
            "label": label,
            "size_pct": round(size, 6),
            "exit_price": round_price(float(price)),
            "result_r": round(leg_r, 6),
            "realized_return_pct": round(leg_return_pct, 6),
        })
        closed_size += size

    if getattr(trade, "tp0_hit", False) and safe_float(getattr(trade, "tp0", 0.0), 0.0):
        add_leg("TP0", getattr(trade, "tp0_size_pct", TP0_SIZE_PCT), trade.tp0)
    if getattr(trade, "tp1_hit", False):
        add_leg("TP1", getattr(trade, "tp1_size_pct", TP1_SIZE_PCT), trade.tp1)
    if getattr(trade, "tp2_hit", False):
        add_leg("TP2", getattr(trade, "tp2_size_pct", TP2_SIZE_PCT), trade.tp2)

    known_r = sum(safe_float(leg.get("result_r"), 0.0) for leg in legs)
    known_return_pct = sum(safe_float(leg.get("realized_return_pct"), 0.0) for leg in legs)
    remaining = max(0.0, 1.0 - closed_size)

    if outcome_status != "RESOLVED" or final_exit_price is None:
        result_r_min = None
        result_r_max = None
        return_min = None
        return_max = None
        if (
            str(outcome_status).upper() == "AMBIGUOUS_INTRABAR"
            and remaining > 1e-9
            and ambiguous_stop_price is not None
            and ambiguous_target_price is not None
        ):
            stop_move = _directional_price_move(trade.side, entry, float(ambiguous_stop_price))
            target_move = _directional_price_move(trade.side, entry, float(ambiguous_target_price))
            bound_a = known_r + remaining * stop_move / risk
            bound_b = known_r + remaining * target_move / risk
            result_r_min, result_r_max = round(min(bound_a, bound_b), 6), round(max(bound_a, bound_b), 6)
            ret_a = known_return_pct + remaining * stop_move / entry * 100.0
            ret_b = known_return_pct + remaining * target_move / entry * 100.0
            return_min, return_max = round(min(ret_a, ret_b), 6), round(max(ret_a, ret_b), 6)
        return {
            "result_r": None,
            "realized_return_pct": None,
            "known_realized_r": round(known_r, 6),
            "known_realized_return_pct": round(known_return_pct, 6),
            "result_unit": "R",
            "outcome_status": outcome_status,
            "ml_eligible": False,
            "realized_legs": legs,
            "known_realized_legs": legs,
            "remaining_size_pct": round(remaining, 6),
            "result_r_min": result_r_min,
            "result_r_max": result_r_max,
            "realized_return_pct_min": return_min,
            "realized_return_pct_max": return_max,
        }

    if remaining > 1e-9:
        add_leg("RUNNER" if closed_size > 0 else "FULL", remaining, float(final_exit_price))

    result_r = sum(safe_float(leg.get("result_r"), 0.0) for leg in legs)
    realized_return_pct = sum(safe_float(leg.get("realized_return_pct"), 0.0) for leg in legs)
    return {
        "result_r": round(result_r, 6),
        "realized_return_pct": round(realized_return_pct, 6),
        "known_realized_r": round(result_r, 6),
        "known_realized_return_pct": round(realized_return_pct, 6),
        "result_unit": "R",
        "outcome_status": "RESOLVED",
        "ml_eligible": True,
        "realized_legs": legs,
        "known_realized_legs": legs,
        "remaining_size_pct": 0.0,
        "result_r_min": round(result_r, 6),
        "result_r_max": round(result_r, 6),
        "realized_return_pct_min": round(realized_return_pct, 6),
        "realized_return_pct_max": round(realized_return_pct, 6),
    }


def _finalize_closed_trade_result(trade: ActiveTrade, result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("outcome_status") or "RESOLVED")
    result.update(_realized_trade_metrics(
        trade,
        result.get("exit_price"),
        outcome_status=status,
        ambiguous_stop_price=result.get("ambiguous_stop_level"),
        ambiguous_target_price=result.get("ambiguous_target_level"),
    ))
    # Legacy compatibility only. R analytics must never read this field.
    result["result_pct"] = result.get("realized_return_pct")
    return result


def _opened_at_ms(opened_at: str) -> int:
    """Парсить ActiveTrade.opened_at (iso_now()) у мілісекунди epoch.
    При будь-якій помилці парсингу повертає 0 (тобто "без нижньої межі"),
    щоб не ламати перевірку стопу через дефектний/відсутній timestamp."""
    try:
        return int(datetime.fromisoformat(str(opened_at)).timestamp() * 1000)
    except Exception:
        return 0


def _candle_touches_level(side: str, candle: Candle, level: float, *, is_stop: bool) -> bool:
    if side == Side.LONG.value:
        return float(candle.low) <= level if is_stop else float(candle.high) >= level
    return float(candle.high) >= level if is_stop else float(candle.low) <= level


def _scan_unchecked_trade_events(trade: ActiveTrade, context: dict[str, Any]) -> dict[str, Any]:
    """Resolve post-open 3M events chronologically and flag OHLC ambiguity.

    A candle that touches both the active stop and one or more still-open targets
    has no knowable intrabar ordering from OHLC alone. Such a record is excluded
    from ML instead of being silently labelled STOP_FIRST.
    """
    lower_bound = max(
        int(getattr(trade, "last_checked_3m_ts", 0) or 0),
        _opened_at_ms(getattr(trade, "opened_at", "")),
    )
    candles = sorted(
        [
            c for c in ((context.get("candles", {}) or {}).get("3m", []) or [])
            if int(getattr(c, "ts", 0) or 0) > lower_bound
        ],
        key=lambda c: int(c.ts),
    )

    tp0_hit = bool(getattr(trade, "tp0_hit", False))
    tp1_hit = bool(getattr(trade, "tp1_hit", False))
    tp2_hit = bool(getattr(trade, "tp2_hit", False))
    tp3_hit = bool(getattr(trade, "tp3_hit", False))
    simulated_stop = float(getattr(trade, "stop_current", 0.0) or 0.0)
    events: list[dict[str, Any]] = []

    for candle in candles:
        touched: list[dict[str, Any]] = []

        if not tp0_hit and safe_float(getattr(trade, "tp0", 0.0), 0.0):
            if _candle_touches_level(trade.side, candle, float(trade.tp0), is_stop=False):
                touched.append({"target": "TP0", "price": float(trade.tp0), "ts": int(candle.ts)})
                tp0_hit = True
        if not tp1_hit and _candle_touches_level(trade.side, candle, float(trade.tp1), is_stop=False):
            touched.append({"target": "TP1", "price": float(trade.tp1), "ts": int(candle.ts)})
            tp1_hit = True
        if tp1_hit and not tp2_hit and _candle_touches_level(trade.side, candle, float(trade.tp2), is_stop=False):
            touched.append({"target": "TP2", "price": float(trade.tp2), "ts": int(candle.ts)})
            tp2_hit = True
        if tp2_hit and not tp3_hit and _candle_touches_level(trade.side, candle, float(trade.tp3), is_stop=False):
            touched.append({"target": "TP3", "price": float(trade.tp3), "ts": int(candle.ts)})
            tp3_hit = True

        stop_touched = bool(simulated_stop) and _candle_touches_level(
            trade.side, candle, simulated_stop, is_stop=True
        )

        # TP2 immediately ratchets the stop to TP1. If that prospective stop and
        # TP2 are both inside the same OHLC bar, ordering is unknowable too.
        prospective_tp2_stop = simulated_stop
        if any(event["target"] == "TP2" for event in touched):
            prospective_tp2_stop = (
                max(float(trade.tp1), simulated_stop)
                if trade.side == Side.LONG.value
                else min(float(trade.tp1), simulated_stop)
            )
            if _candle_touches_level(trade.side, candle, prospective_tp2_stop, is_stop=True):
                stop_touched = True

        if stop_touched and touched:
            return {
                "status": "AMBIGUOUS_INTRABAR",
                "events": events,
                "ambiguous_targets": [event["target"] for event in touched],
                "candle_ts": int(candle.ts),
                "stop_level": round_price(prospective_tp2_stop),
            }

        if stop_touched:
            return {
                "status": "STOP",
                "events": events,
                "candle_ts": int(candle.ts),
                "stop_level": round_price(simulated_stop),
            }

        events.extend(touched)
        if any(event["target"] == "TP2" for event in touched):
            simulated_stop = prospective_tp2_stop
        if any(event["target"] == "TP3" for event in touched):
            return {
                "status": "TP3",
                "events": events,
                "candle_ts": int(candle.ts),
                "exit_price": round_price(float(trade.tp3)),
            }

    return {"status": "OPEN", "events": events}


def _apply_scanned_target_events(
    trade: ActiveTrade,
    events: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    for event in events or []:
        target = str(event.get("target") or "")
        ts = int(event.get("ts") or 0)
        if target == "TP0" and not getattr(trade, "tp0_hit", False):
            trade.tp0_hit = True
            trade.tp0_hit_ts = ts or int(time.time() * 1000)
            trade.tp0_hit_at = datetime.fromtimestamp(trade.tp0_hit_ts / 1000.0, tz=timezone.utc).isoformat()
            result["action"] = Action.TP0.value
            result["notes"].append(
                f"TP0 досягнуто — зафіксуйте ~{int(getattr(trade, 'tp0_size_pct', TP0_SIZE_PCT) * 100)}% позиції"
            )
        elif target == "TP1" and not trade.tp1_hit:
            trade.tp1_hit = True
            trade.tp1_hit_at = iso_now()
            trade.tp1_hit_ts = ts or int(time.time() * 1000)
            trade.tp1_stop_locked = False
            result["action"] = Action.TP1.value
            result["notes"].append(
                f"TP1 досягнуто — зафіксуйте ~{int(getattr(trade, 'tp1_size_pct', TP1_SIZE_PCT) * 100)}% позиції"
            )
        elif target == "TP2" and not trade.tp2_hit:
            trade.tp2_hit = True
            trade.tp2_stop_locked = True
            if trade.side == Side.LONG.value:
                trade.tp2_locked_stop = max(float(trade.tp1), float(trade.stop_current))
            else:
                trade.tp2_locked_stop = min(float(trade.tp1), float(trade.stop_current))
            trade.stop_current = round_price(trade.tp2_locked_stop)
            result["action"] = Action.TP2.value
            result["notes"].append("TP2 досягнуто — стоп перенесено на TP1")
        elif target == "TP3" and not trade.tp3_hit:
            trade.tp3_hit = True
            result["action"] = Action.TP3.value
            result["notes"].append(f"TP3 досягнуто ({round_price(trade.tp3)})")


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
    # Якщо нових post-open свічок немає, старі OHLC не можна повторно
    # використовувати проти щойно відкритої або вже перевіреної угоди.
    # Поточний стан у такому випадку контролює лише жива ціна нижче.
    candles_to_check = unchecked

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


def _target_hit(trade: ActiveTrade, context: dict, level: float, lookback: int = 4) -> bool:
    """Return True only for a live price touch or a post-open unchecked 3M touch."""
    price = float(context.get("price") or 0)
    c3 = (context.get("candles", {}) or {}).get("3m", []) or []
    lower_bound = max(
        int(getattr(trade, "last_checked_3m_ts", 0) or 0),
        _opened_at_ms(getattr(trade, "opened_at", "")),
    )
    recent = [c for c in c3 if int(getattr(c, "ts", 0) or 0) > lower_bound][-lookback:]

    if trade.side == Side.LONG.value:
        if price >= level:
            return True
        return max((c.high for c in recent), default=float("-inf")) >= level

    if price <= level:
        return True
    return min((c.low for c in recent), default=float("inf")) <= level


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


def _update_trade_extremes_from_context(trade: ActiveTrade, context: dict[str, Any]) -> None:
    """Update MFE/MAE from every new 3M candle between polling runs."""
    price = safe_float(context.get("price"), trade.entry)
    lower_bound = max(int(trade.last_checked_3m_ts or 0), _opened_at_ms(trade.opened_at))
    c3 = (context.get("candles", {}) or {}).get("3m", []) or []
    new_candles = [c for c in c3 if int(getattr(c, "ts", 0) or 0) > lower_bound]

    if trade.side == Side.LONG.value:
        highs = [price, float(trade.best_price)] + [safe_float(c.high, price) for c in new_candles]
        lows = [price, float(trade.worst_price)] + [safe_float(c.low, price) for c in new_candles]
        trade.best_price = max(highs)
        trade.worst_price = min(lows)
    else:
        lows = [price, float(trade.best_price)] + [safe_float(c.low, price) for c in new_candles]
        highs = [price, float(trade.worst_price)] + [safe_float(c.high, price) for c in new_candles]
        trade.best_price = min(lows)
        trade.worst_price = max(highs)


def _strict_breakeven_stop(trade: ActiveTrade) -> float:
    if trade.side == Side.LONG.value:
        return round_price(float(trade.entry) + COMMISSION_BUFFER_DOLLARS)
    return round_price(float(trade.entry) - COMMISSION_BUFFER_DOLLARS)


def _protection_mfe_r(trade: ActiveTrade) -> float:
    risk_distance = _trade_risk_distance(trade)
    if trade.side == Side.LONG.value:
        return max(0.0, (float(trade.best_price) - float(trade.entry)) / risk_distance)
    return max(0.0, (float(trade.entry) - float(trade.best_price)) / risk_distance)


def _protection_current_r(trade: ActiveTrade, price: float) -> float:
    risk_distance = _trade_risk_distance(trade)
    if trade.side == Side.LONG.value:
        return (float(price) - float(trade.entry)) / risk_distance
    return (float(trade.entry) - float(price)) / risk_distance


def _protection_stop_for_locked_r(trade: ActiveTrade, locked_r: float) -> float:
    risk_distance = _trade_risk_distance(trade)
    if trade.side == Side.LONG.value:
        return round_price(float(trade.entry) + max(0.0, locked_r) * risk_distance)
    return round_price(float(trade.entry) - max(0.0, locked_r) * risk_distance)


def _stop_locked_r(trade: ActiveTrade, stop: Optional[float] = None) -> float:
    value = safe_float(stop, safe_float(getattr(trade, "stop_current", 0.0), 0.0))
    if value <= 0:
        return 0.0
    return max(0.0, _protection_current_r(trade, value))


def _ratchet_observation_ts(context: dict[str, Any]) -> int:
    candles = ((context.get("candles") or {}).get("3m") or [])
    confirmed = [int(getattr(candle, "ts", 0) or 0) for candle in candles if getattr(candle, "confirmed", True)]
    return max(confirmed, default=int(now_utc().timestamp() * 1000))


def _append_ratchet_evidence(
    trade: ActiveTrade,
    context: dict[str, Any],
    event_type: str,
    **details: Any,
) -> dict[str, Any]:
    evidence = [dict(item) for item in (getattr(trade, "protection_ratchet_evidence", []) or []) if isinstance(item, dict)]
    row = {
        "sequence": len(evidence) + 1,
        "time": iso_now(),
        "observation_ts": _ratchet_observation_ts(context),
        "event_type": str(event_type),
        "trade_id": str(getattr(trade, "id", "") or ""),
        "side": str(getattr(trade, "side", "") or ""),
        "peak_mfe_r": round(safe_float(details.pop("peak_mfe_r", getattr(trade, "protection_peak_mfe_r", 0.0)), 0.0), 6),
        "locked_r": round(safe_float(details.pop("locked_r", getattr(trade, "protection_locked_r", 0.0)), 0.0), 6),
        "stop": round_price(details.pop("stop", getattr(trade, "stop_current", 0.0))),
        "retroactive": False,
        "schema_version": "ratchet_step_evidence_v9.5.20",
    }
    row.update(json_safe(details))
    signature = (
        row.get("observation_ts"), row.get("event_type"), row.get("peak_mfe_r"),
        row.get("locked_r"), row.get("stop"), row.get("requested_locked_r"),
    )
    if evidence:
        last = evidence[-1]
        last_signature = (
            last.get("observation_ts"), last.get("event_type"), last.get("peak_mfe_r"),
            last.get("locked_r"), last.get("stop"), last.get("requested_locked_r"),
        )
        if signature == last_signature:
            return last
    evidence.append(row)
    if len(evidence) > TP0_PROTECT_RATCHET_EVIDENCE_LIMIT:
        evidence = evidence[-TP0_PROTECT_RATCHET_EVIDENCE_LIMIT:]
        for index, item in enumerate(evidence, 1):
            item["sequence"] = index
    trade.protection_ratchet_evidence = evidence
    trade.management_evidence_schema_version = "management_evidence_v9.5.20_multistep_ratchet_evidence"
    return row


def _tp0_profit_protection(trade: ActiveTrade, context: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Trail a proven pre-TP1 position from its MFE high-water mark.

    Historical unchecked candles are replayed against the stop that existed at
    that time before this function is called. Any stop change here therefore
    applies only from the current observation forward. After the first giveback
    activation the lock is recomputed whenever MFE makes a new high-water mark:
    PROBE retains 1-40%=60% of peak MFE, STANDARD retains 1-50%=50%.
    """
    is_probe = str(getattr(trade, "entry_stage", "") or "").upper() == EntryStage.PROBE.value
    giveback_threshold = PROBE_TP0_PROTECT_GIVEBACK_RATIO if is_probe else TP0_PROTECT_GIVEBACK_RATIO
    threshold_scope = "PROBE" if is_probe else "STANDARD"
    price = safe_float(context.get("price"), float(trade.entry))
    risk_distance = _trade_risk_distance(trade)
    peak_mfe_r = _protection_mfe_r(trade)
    current_r = _protection_current_r(trade, price)
    stop_before_evaluation = round_price(trade.stop_current)
    previous_peak_r = max(0.0, safe_float(getattr(trade, "protection_peak_mfe_r", 0.0), 0.0))
    prior_locked_r = max(
        0.0,
        safe_float(getattr(trade, "protection_locked_r", 0.0), 0.0),
        _stop_locked_r(trade),
    )
    new_peak = peak_mfe_r > previous_peak_r + 1e-9
    if new_peak:
        trade.protection_ratchet_new_peak_events = int(getattr(trade, "protection_ratchet_new_peak_events", 0) or 0) + 1
    trade.protection_peak_mfe_r = round(max(previous_peak_r, peak_mfe_r), 6)
    trade.protection_last_evaluated_mfe_r = round(peak_mfe_r, 6)

    best_pct = max(0.0, safe_float(result.get("best_pct"), 0.0))
    current_pct = safe_float(result.get("current_pct"), 0.0)
    giveback_ratio = max(0.0, (peak_mfe_r - current_r) / max(peak_mfe_r, 1e-9)) if peak_mfe_r > 0 else 0.0
    profile = {
        "eligible": False,
        "activated": bool(getattr(trade, "pre_tp1_protection_locked", False)),
        "ratcheted": False,
        "state": "PROTECT" if getattr(trade, "pre_tp1_protection_locked", False) else "SUPPORTED",
        "giveback_ratio": round(giveback_ratio, 6),
        "mfe_pct": round(best_pct, 6),
        "current_pct": round(current_pct, 6),
        "peak_mfe_r": round(peak_mfe_r, 6),
        "current_r": round(current_r, 6),
        "locked_r": round(prior_locked_r, 6),
        "threshold": giveback_threshold,
        "threshold_scope": threshold_scope,
        "new_mfe_high_water": new_peak,
        "ratchet_count": int(getattr(trade, "protection_ratchet_count", 0) or 0),
        "missed_due_to_price": int(getattr(trade, "protection_ratchet_missed_due_to_price", 0) or 0),
        "new_peak_events": int(getattr(trade, "protection_ratchet_new_peak_events", 0) or 0),
        "evidence_count": len(getattr(trade, "protection_ratchet_evidence", []) or []),
        "policy": "HIGH_WATER_MFE_TRAILING_FROM_CURRENT_OBSERVATION_ONLY",
    }
    result["mfe_giveback_ratio"] = round(giveback_ratio, 6)

    if not TP0_PROTECT_ENABLED or not getattr(trade, "tp0_hit", False) or getattr(trade, "tp1_hit", False):
        trade.management_state = "PROTECT" if getattr(trade, "pre_tp1_protection_locked", False) else "SUPPORTED"
        result["management_state"] = trade.management_state
        result["profit_protection"] = profile
        return profile

    # Preserve the original configuration contract: this floor is expressed
    # in percentage move, not R. The ratchet itself is R-based, but converting
    # the percentage threshold to R would silently change behavior whenever the
    # trade stop distance differs from 1% of entry.
    if best_pct <= max(TP0_PROTECT_MIN_MFE_PCT, 1e-9):
        trade.management_state = "PROTECT" if getattr(trade, "pre_tp1_protection_locked", False) else "SUPPORTED"
        result["management_state"] = trade.management_state
        result["profit_protection"] = profile
        return profile

    profile["eligible"] = True
    locked = bool(getattr(trade, "pre_tp1_protection_locked", False))
    if new_peak:
        high_water_evidence = _append_ratchet_evidence(
            trade, context, "MFE_HIGH_WATER_OBSERVED",
            previous_peak_mfe_r=round(previous_peak_r, 6),
            peak_mfe_r=round(peak_mfe_r, 6),
            current_r=round(current_r, 6),
            giveback_ratio=round(giveback_ratio, 6),
            locked_r=round(prior_locked_r, 6),
            stop=stop_before_evaluation,
            threshold=giveback_threshold,
            threshold_scope=threshold_scope,
        )
        profile["evidence_count"] = len(getattr(trade, "protection_ratchet_evidence", []) or [])
        profile["last_evidence"] = high_water_evidence
    if not locked and giveback_ratio < giveback_threshold:
        trade.management_state = "SUPPORTED"
        result["management_state"] = "SUPPORTED"
        result["profit_protection"] = profile
        return profile

    be_stop = _strict_breakeven_stop(trade)
    be_locked_r = _stop_locked_r(trade, be_stop)
    requested_locked_r = max(be_locked_r, peak_mfe_r * (1.0 - giveback_threshold))
    price_buffer_r = TP0_PROTECT_PRICE_BUFFER_DOLLARS / max(risk_distance, 1e-9)
    market_feasible_locked_r = current_r - price_buffer_r
    feasible_locked_r = min(requested_locked_r, market_feasible_locked_r)
    profile.update({
        "requested_locked_r": round(requested_locked_r, 6),
        "market_feasible_locked_r": round(market_feasible_locked_r, 6),
        "price_buffer_r": round(price_buffer_r, 6),
        "clamped_to_current_price": market_feasible_locked_r + 1e-9 < requested_locked_r,
    })

    # Initial activation may use a smaller improvement than later ratchets, but
    # it must at least establish commission-adjusted breakeven. Subsequent moves
    # require a material R-step to avoid journal noise and rounding churn.
    minimum_improvement = 1e-9 if not locked else TP0_PROTECT_MIN_RATCHET_STEP_R
    if feasible_locked_r < be_locked_r - 1e-9 or feasible_locked_r <= prior_locked_r + minimum_improvement:
        if requested_locked_r > prior_locked_r + minimum_improvement and feasible_locked_r <= prior_locked_r + minimum_improvement:
            trade.protection_ratchet_missed_due_to_price = int(getattr(trade, "protection_ratchet_missed_due_to_price", 0) or 0) + 1
            profile["missed_due_to_price"] = trade.protection_ratchet_missed_due_to_price
            profile["state"] = "PROTECT" if locked else "WEAKENING"
            result.setdefault("notes", []).append(
                f"RATCHET WAIT: peak={peak_mfe_r:.2f}R просить lock={requested_locked_r:.2f}R, "
                f"але поточна ціна дозволяє лише {market_feasible_locked_r:.2f}R"
            )
        trade.management_state = "PROTECT" if locked else "WEAKENING"
        event_type = (
            "RATCHET_DEFERRED_PRICE"
            if requested_locked_r > prior_locked_r + minimum_improvement
            else "RATCHET_NO_MATERIAL_STEP"
        )
        deferred_evidence = _append_ratchet_evidence(
            trade, context, event_type,
            previous_peak_mfe_r=round(previous_peak_r, 6),
            peak_mfe_r=round(peak_mfe_r, 6),
            current_r=round(current_r, 6),
            giveback_ratio=round(giveback_ratio, 6),
            prior_locked_r=round(prior_locked_r, 6),
            requested_locked_r=round(requested_locked_r, 6),
            market_feasible_locked_r=round(market_feasible_locked_r, 6),
            feasible_locked_r=round(feasible_locked_r, 6),
            locked_r=round(prior_locked_r, 6),
            stop=stop_before_evaluation,
            minimum_improvement_r=minimum_improvement,
            missed_due_to_price=trade.protection_ratchet_missed_due_to_price,
        )
        profile["evidence_count"] = len(getattr(trade, "protection_ratchet_evidence", []) or [])
        profile["last_evidence"] = deferred_evidence
        result["management_state"] = trade.management_state
        if locked:
            result["recommended_stop"] = round_price(trade.stop_current)
            result["recommended_stop_reason"] = "TP0 high-water trailing ratchet active"
        result["profit_protection"] = profile
        return profile

    target_stop = _protection_stop_for_locked_r(trade, feasible_locked_r)
    applied = _apply_protective_stop(trade, context, target_stop)
    already_protected = _stop_locked_r(trade) >= feasible_locked_r - 1e-6
    if not (applied or already_protected):
        trade.protection_ratchet_missed_due_to_price = int(getattr(trade, "protection_ratchet_missed_due_to_price", 0) or 0) + 1
        trade.management_state = "PROTECT" if locked else "WEAKENING"
        result["management_state"] = trade.management_state
        profile.update({
            "state": trade.management_state,
            "requested_stop": target_stop,
            "missed_due_to_price": trade.protection_ratchet_missed_due_to_price,
        })
        result.setdefault("notes", []).append(
            f"RATCHET WAIT: stop {target_stop} не можна коректно застосувати від поточної ціни {round_price(price)}"
        )
        rejected_evidence = _append_ratchet_evidence(
            trade, context, "RATCHET_APPLY_REJECTED",
            previous_peak_mfe_r=round(previous_peak_r, 6), peak_mfe_r=round(peak_mfe_r, 6),
            current_r=round(current_r, 6), prior_locked_r=round(prior_locked_r, 6),
            requested_locked_r=round(requested_locked_r, 6), feasible_locked_r=round(feasible_locked_r, 6),
            requested_stop=target_stop, stop=stop_before_evaluation, locked_r=round(prior_locked_r, 6),
            missed_due_to_price=trade.protection_ratchet_missed_due_to_price,
        )
        profile["evidence_count"] = len(getattr(trade, "protection_ratchet_evidence", []) or [])
        profile["last_evidence"] = rejected_evidence
        result["profit_protection"] = profile
        return profile

    now = iso_now()
    first_activation = not locked
    trade.pre_tp1_protection_locked = True
    if first_activation:
        trade.pre_tp1_protection_at = now
        trade.protection_activation_mfe_r = round(peak_mfe_r, 6)
        trade.protection_activation_current_r = round(current_r, 6)
        trade.protection_activation_stop = round_price(trade.stop_current)
    trade.pre_tp1_protection_ratio = round(giveback_ratio, 6)
    trade.pre_tp1_protection_threshold = round(giveback_threshold, 6)
    trade.pre_tp1_protection_scope = threshold_scope
    trade.protection_locked_r = round(max(prior_locked_r, _stop_locked_r(trade)), 6)
    trade.protection_ratchet_count = int(getattr(trade, "protection_ratchet_count", 0) or 0) + int(applied or first_activation)
    trade.protection_last_ratchet_at = now
    trade.protection_last_ratchet_stop = round_price(trade.stop_current)
    trade.management_evidence_schema_version = "management_evidence_v9.5.20_multistep_ratchet_evidence"
    trade.management_state = "PROTECT"
    result["management_state"] = "PROTECT"
    result["action"] = Action.PROTECT.value
    result["recommended_stop"] = round_price(trade.stop_current)
    result["recommended_stop_reason"] = "TP0 high-water MFE trailing ratchet"
    result.setdefault("notes", []).append(
        (
            f"PROTECT ACTIVATE: giveback={giveback_ratio:.0%}, peak={peak_mfe_r:.2f}R, "
            f"lock={trade.protection_locked_r:.2f}R, stop={round_price(trade.stop_current)}"
            if first_activation
            else f"PROTECT RATCHET #{trade.protection_ratchet_count}: peak={peak_mfe_r:.2f}R, "
                 f"lock={trade.protection_locked_r:.2f}R, stop={round_price(trade.stop_current)}"
        )
    )
    profile.update({
        "activated": True,
        "ratcheted": bool(applied),
        "first_activation": first_activation,
        "state": "PROTECT",
        "stop": round_price(trade.stop_current),
        "locked_r": trade.protection_locked_r,
        "activation_mfe_r": getattr(trade, "protection_activation_mfe_r", 0.0),
        "activation_current_r": getattr(trade, "protection_activation_current_r", 0.0),
        "ratchet_count": trade.protection_ratchet_count,
        "last_ratchet_at": trade.protection_last_ratchet_at,
        "missed_due_to_price": trade.protection_ratchet_missed_due_to_price,
    })
    step_event = _append_ratchet_evidence(
        trade, context, "RATCHET_ACTIVATED" if first_activation else "RATCHET_APPLIED",
        previous_peak_mfe_r=round(previous_peak_r, 6),
        peak_mfe_r=round(peak_mfe_r, 6),
        current_r=round(current_r, 6),
        giveback_ratio=round(giveback_ratio, 6),
        prior_locked_r=round(prior_locked_r, 6),
        requested_locked_r=round(requested_locked_r, 6),
        market_feasible_locked_r=round(market_feasible_locked_r, 6),
        feasible_locked_r=round(feasible_locked_r, 6),
        locked_r=round(trade.protection_locked_r, 6),
        stop_before=stop_before_evaluation,
        requested_stop=target_stop,
        stop=round_price(trade.stop_current),
        applied=bool(applied or already_protected),
        ratchet_count=trade.protection_ratchet_count,
        threshold=giveback_threshold,
        threshold_scope=threshold_scope,
    )
    profile["evidence_count"] = len(getattr(trade, "protection_ratchet_evidence", []) or [])
    profile["last_evidence"] = step_event
    result["profit_protection"] = profile
    return profile


def _active_trade_age_minutes(trade: ActiveTrade) -> float:
    try:
        opened = datetime.fromisoformat(str(trade.opened_at))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc() - opened.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _trade_mfe_r(trade: ActiveTrade) -> float:
    risk = abs(safe_float(trade.entry) - safe_float(trade.stop_initial))
    if risk <= 1e-9:
        return 0.0
    if trade.side == Side.LONG.value:
        return max(0.0, (safe_float(trade.best_price, trade.entry) - safe_float(trade.entry)) / risk)
    return max(0.0, (safe_float(trade.entry) - safe_float(trade.best_price, trade.entry)) / risk)


def _trade_current_r(trade: ActiveTrade, price: float) -> float:
    risk = abs(safe_float(trade.entry) - safe_float(trade.stop_initial))
    if risk <= 1e-9:
        return 0.0
    return side_sign(trade.side) * (safe_float(price, trade.entry) - safe_float(trade.entry)) / risk


def _linked_preconfirmation_event(trade: ActiveTrade, context: dict[str, Any]) -> Optional[dict[str, Any]]:
    event_id = str(getattr(trade, "preconfirmation_event_id", "") or "").strip()
    if not event_id:
        return None
    events = list(context.get("preconfirmation_events") or [])
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        current_id = str(event.get("event_id") or event.get("id") or "").strip()
        if current_id == event_id:
            return event
    return None


def probe_no_followthrough_exit_profile(trade: ActiveTrade, context: dict[str, Any]) -> dict[str, Any]:
    """Return a fail-closed management recommendation for an unproven PROBE.

    FAILED/EXPIRED precursors retain the fast exit. CONFIRMED precursors receive a
    longer but finite lease: after that lease, a weak non-profitable probe yields
    only when path integrity has decayed or a fresh opposite executable structure
    is present. This prevents both immortal HOLDs and blind time-based exits.
    """
    profile: dict[str, Any] = {
        "applies": False,
        "exit": False,
        "reason_code": "",
        "age_minutes": round(_active_trade_age_minutes(trade), 2),
        "mfe_r": round(_trade_mfe_r(trade), 4),
        "current_r": round(_trade_current_r(trade, safe_float(context.get("price"), trade.entry)), 4),
        "preconfirmation_status": "UNAVAILABLE",
    }
    if not PROBE_NO_FOLLOWTHROUGH_ENABLED:
        profile["reason_code"] = "DISABLED"
        return profile
    if str(getattr(trade, "entry_stage", "") or "").upper() != EntryStage.PROBE.value:
        profile["reason_code"] = "NOT_PROBE"
        return profile
    if any((getattr(trade, "tp0_hit", False), getattr(trade, "tp1_hit", False), getattr(trade, "tp2_hit", False), getattr(trade, "tp3_hit", False))):
        profile["reason_code"] = "FOLLOWTHROUGH_ALREADY_PROVEN"
        return profile

    profile["applies"] = True
    linked_event_id = str(getattr(trade, "preconfirmation_event_id", "") or "").strip()
    event = _linked_preconfirmation_event(trade, context)
    status = _preconfirm_event_status(event) if event else ("UNAVAILABLE" if linked_event_id else "UNLINKED_LEGACY")
    profile["preconfirmation_status"] = status
    profile["preconfirmation_event_id"] = linked_event_id
    age = safe_float(profile["age_minutes"])
    mfe_r = safe_float(profile["mfe_r"])
    current_r = safe_float(profile["current_r"])
    opposite_evidence = dict(context.get("fresh_opposite_execution") or {})
    opposite_executable = bool(
        opposite_evidence.get("executable")
        and opposite_evidence.get("opposite", str(opposite_evidence.get("side") or "").upper() == _opposite_side(trade.side))
    )
    integrity_fn = globals().get("_active_path_integrity_v9532")
    path_integrity = safe_float(integrity_fn(trade, context), 50.0) if callable(integrity_fn) else 50.0
    profile.update({
        "opposite_executable": opposite_executable,
        "opposite_structure": opposite_evidence,
        "path_integrity": round(path_integrity, 2),
    })

    event_failed = status in {"FAILED", "EXPIRED"}
    if (
        event_failed
        and age >= PROBE_NO_FOLLOWTHROUGH_MINUTES
        and mfe_r < PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R
    ):
        profile.update({
            "exit": True,
            "reason_code": "PROBE_FIXED_HORIZON_FAILED_NO_FOLLOWTHROUGH",
            "threshold_minutes": PROBE_NO_FOLLOWTHROUGH_MINUTES,
            "max_mfe_r": PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R,
        })
        return profile

    linkage_unresolved = bool(linked_event_id and status in {"PENDING", "UNAVAILABLE"})
    if (
        linkage_unresolved
        and age >= PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MINUTES
        and mfe_r < PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MAX_MFE_R
        and current_r <= 0.0
    ):
        profile.update({
            "exit": True,
            "reason_code": "PROBE_STALE_NO_FOLLOWTHROUGH_FAILSAFE",
            "threshold_minutes": PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MINUTES,
            "max_mfe_r": PROBE_NO_FOLLOWTHROUGH_FAILSAFE_MAX_MFE_R,
        })
        return profile

    confirmed_stale = bool(
        status == "CONFIRMED"
        and age >= CONFIRMED_PROBE_STALE_MINUTES
        and mfe_r < CONFIRMED_PROBE_STALE_MAX_MFE_R
        and current_r <= CONFIRMED_PROBE_STALE_MAX_CURRENT_R
    )
    control_transfer = bool(
        confirmed_stale
        and (opposite_executable or path_integrity < CONFIRMED_PROBE_MIN_PATH_INTEGRITY)
    )
    if control_transfer:
        profile.update({
            "exit": True,
            "reason_code": "CONFIRMED_PROBE_STALE_CONTROL_TRANSFER",
            "threshold_minutes": CONFIRMED_PROBE_STALE_MINUTES,
            "max_mfe_r": CONFIRMED_PROBE_STALE_MAX_MFE_R,
            "max_current_r": CONFIRMED_PROBE_STALE_MAX_CURRENT_R,
            "minimum_path_integrity": CONFIRMED_PROBE_MIN_PATH_INTEGRITY,
            "control_transfer_source": "FRESH_OPPOSITE_EXECUTABLE" if opposite_executable else "PATH_DECAY",
        })
        return profile
    if confirmed_stale:
        profile["reason_code"] = "CONFIRMED_PROBE_STALE_BUT_STRUCTURE_NOT_INVALIDATED"
        return profile

    profile["reason_code"] = "FOLLOWTHROUGH_WINDOW_STILL_VALID"
    return profile


def _manage_active_trade_core(trade: ActiveTrade, context: dict) -> dict:
    price = context.get("price")
    # Якщо ціна None через збій API, тримаємо позицію, щоб не наробити помилок
    if price is None:
        return {"action": Action.HOLD.value, "closed": False, "notes": ["Очікування даних ціни..."]}
        
    # ПРИМІТКА: atr15 тут раніше рахувався локально, але жоден з хелперів
    # нижче (_stop_hit, _target_hit, _structural_trailing_stop) не приймає
    # його аргументом — усі й так читають atr15 напряму з context.
    # Orphaned-змінна від старішої версії видалена.
    side = trade.side

    # 1. Оновлюємо екстремуми за ВСІМА новими 3M свічками між polling-запусками.
    _update_trade_extremes_from_context(trade, context)

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
        "management_state": getattr(trade, "management_state", "SUPPORTED") or "SUPPORTED",
    }

    # Resolve all unchecked post-open candles chronologically before using
    # aggregate stop/target helpers. This preserves partial exits that happened
    # on earlier bars and detects same-bar stop/target ambiguity.
    event_scan = _scan_unchecked_trade_events(trade, context)
    _apply_scanned_target_events(trade, event_scan.get("events") or [], result)

    if event_scan.get("status") == "AMBIGUOUS_INTRABAR":
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = None
        result["outcome_status"] = "AMBIGUOUS_INTRABAR"
        result["ml_eligible"] = False
        ambiguous_targets = list(event_scan.get("ambiguous_targets") or [])
        target_order = {"TP0": 0, "TP1": 1, "TP2": 2, "TP3": 3}
        highest_target = max(ambiguous_targets, key=lambda name: target_order.get(name, -1), default="")
        result["ambiguous_targets"] = ambiguous_targets
        result["ambiguous_stop_level"] = event_scan.get("stop_level")
        result["ambiguous_target_level"] = safe_float(getattr(trade, highest_target.lower(), 0.0), 0.0) if highest_target else None
        result["notes"].append(
            "AMBIGUOUS_INTRABAR: одна 3M-свічка торкнулася стопа і "
            + ", ".join(event_scan.get("ambiguous_targets") or [])
            + "; порядок подій з OHLC не визначається"
        )
        trade.status = "CLOSED"
        trade.last_action = Action.STOP.value
        return _finalize_closed_trade_result(trade, result)

    if event_scan.get("status") == "STOP":
        exit_price = round_price(safe_float(event_scan.get("stop_level"), trade.stop_current))
        result["closed"] = True
        result["action"] = Action.STOP.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(
            f"Stop hit після хронологічного 3M replay, ts={event_scan.get('candle_ts')}"
        )
        trade.status = "CLOSED"
        trade.last_action = Action.STOP.value
        return _finalize_closed_trade_result(trade, result)

    if event_scan.get("status") == "TP3":
        exit_price = round_price(safe_float(event_scan.get("exit_price"), trade.tp3))
        result["closed"] = True
        result["action"] = Action.TP3.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(f"TP3 досягнуто ({exit_price}) — угоду повністю закрито")
        trade.status = "CLOSED"
        trade.last_action = Action.TP3.value
        return _finalize_closed_trade_result(trade, result)

    # v8.11: unchecked historical candles were evaluated against the stop that
    # was active at that time. New protection is applied only from now onward.

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
        return _finalize_closed_trade_result(trade, result)

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
        return _finalize_closed_trade_result(trade, result)

    # PROBE is an information-gathering stage. Once its fixed confirmation
    # horizon has failed and MFE stayed tiny, holding to the full structural stop
    # adds risk without new evidence.
    no_followthrough = probe_no_followthrough_exit_profile(trade, context)
    result["probe_no_followthrough"] = no_followthrough
    if no_followthrough.get("exit"):
        exit_price = round_price(price)
        result["closed"] = True
        result["action"] = Action.EXIT.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["management_state"] = "NO_FOLLOWTHROUGH_EXIT"
        result["notes"].append(
            f"PROBE early exit: {no_followthrough.get('reason_code')} | "
            f"age={no_followthrough.get('age_minutes')}m, MFE={no_followthrough.get('mfe_r')}R, "
            f"preconfirmation={no_followthrough.get('preconfirmation_status')}"
        )
        trade.management_state = "NO_FOLLOWTHROUGH_EXIT"
        trade.status = "CLOSED"
        trade.last_action = Action.EXIT.value
        return _finalize_closed_trade_result(trade, result)

    # --- Структурний трейлінг ПІСЛЯ TP1 ---
    if trade.tp1_hit:
        delayed_stop = _delayed_tp1_lock_stop(trade, context)
        ready, ready_reason = _tp1_protection_ready(trade, context)
        if delayed_stop is not None and _apply_protective_stop(trade, context, delayed_stop):
            trade.tp1_stop_locked = True
            trade.tp1_locked_stop = round_price(trade.stop_current)
            trade.management_state = "PROTECT"
            result["management_state"] = "PROTECT"
            result["notes"].append(f"Delayed BE/structural lock активовано: стоп перенесено до {trade.stop_current} | {ready_reason}")
            result["recommended_stop"] = round_price(trade.stop_current)
            result["recommended_stop_reason"] = "v6.13 BE_DELAY + structural swing lock після TP1"
        elif not ready:
            result["notes"].append(ready_reason)

    # --- 0. TP0: службова фіксація + v8.11 MFE protection ---
    # На самому факті TP0 стоп не рухається миттєво: спочатку лишається структурним.
    # Якщо після TP0 ринок віддає задану частку MFE, v8.11 переводить стан у
    # WEAKENING/PROTECT і підтягує стоп до комісійно-скоригованого беззбитку.
    if not result["closed"] and getattr(trade, "tp0", 0.0) and not getattr(trade, "tp0_hit", False) and _target_hit(trade, context, trade.tp0):
        trade.tp0_hit = True
        c3_for_tp0 = (context.get("candles", {}) or {}).get("3m", []) or []
        trade.tp0_hit_ts = int(c3_for_tp0[-1].ts) if c3_for_tp0 else int(time.time() * 1000)
        trade.tp0_hit_at = datetime.fromtimestamp(trade.tp0_hit_ts / 1000.0, tz=timezone.utc).isoformat()
        result["action"] = Action.TP0.value
        result["notes"].append(
            f"TP0 досягнуто — зафіксуйте ~{int(getattr(trade, 'tp0_size_pct', TP0_SIZE_PCT) * 100)}% позиції; "
            f"до {(PROBE_TP0_PROTECT_GIVEBACK_RATIO if str(getattr(trade, 'entry_stage', '')).upper() == EntryStage.PROBE.value else TP0_PROTECT_GIVEBACK_RATIO):.0%} giveback стоп лишається структурним"
        )

    _tp0_profit_protection(trade, context, result)
    _tp0_giveback_innovation_advisor(trade, context, result)

    # --- 1. Фіксація TP1 (v6.13 delayed protection, НЕ миттєвий BE) ---
    if not result["closed"] and not trade.tp1_hit and _target_hit(trade, context, trade.tp1):
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
    if not result["closed"] and trade.tp1_hit and not trade.tp2_hit and _target_hit(trade, context, trade.tp2):
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
    if not result["closed"] and trade.tp2_hit and not trade.tp3_hit and _target_hit(trade, context, trade.tp3):
        trade.tp3_hit = True
        exit_price = round_price(trade.tp3)
        result["closed"] = True
        result["action"] = Action.TP3.value
        result["exit_price"] = exit_price
        result["current_pct"] = _trade_pct(side, trade.entry, exit_price)
        result["notes"].append(f"TP3 досягнуто ({exit_price}) — угоду повністю закрито")
        trade.status = "CLOSED"
        trade.last_action = Action.TP3.value
        return _finalize_closed_trade_result(trade, result)

    # --- Відображення рекомендованого стопу ---
    if not result["closed"]:
        result["recommended_stop"] = round_price(trade.stop_current)
        if trade.tp2_stop_locked:
            result["recommended_stop_reason"] = "TP2-стоп зафіксовано"
        elif trade.tp1_stop_locked:
            result["recommended_stop_reason"] = "Delayed BE/structural lock активний після TP1"
        elif getattr(trade, "pre_tp1_protection_locked", False):
            result["recommended_stop_reason"] = "TP0 high-water MFE trailing ratchet активний"
        elif trade.tp1_hit:
            result["recommended_stop_reason"] = "TP1 взято, але BE_DELAY_ENGINE ще не підтвердив перенос стопа"

    # Structural invalidation is handled only by
    # _decision_stop_breached_by_close(), which uses a post-open confirmed 15M close.

    trade.last_checked_3m_ts = int(now_utc().timestamp() * 1000)
    trade.last_action = result["action"]
    return result


def _active_path_integrity_v9532(trade: ActiveTrade, context: dict[str,Any]) -> float:
    q=_v9532_recent_confirmed(context,"3m",7)
    if len(q)<4:return 50.0
    sign=side_sign(trade.side); atr=max(safe_float(context.get("atr15"),0.0),1e-9); closes=[safe_float(c.close) for c in q]
    net=sign*(closes[-1]-closes[0])/atr; path=sum(abs(closes[i]-closes[i-1]) for i in range(1,len(closes)))/atr; eff=clamp(max(0.0,net)/max(path,.05),0,1); dirs=sum(1 for c in q if sign*(safe_float(c.close)-safe_float(c.open))>0)/len(q)
    return clamp(55.0*eff+45.0*dirs,0.0,100.0)


def _manage_active_trade_path_decay(trade: ActiveTrade, context: dict) -> dict:
    result=_manage_active_trade_core(trade,context)
    if result.get("closed") or not PATH_DECAY_DEFENSE_ENABLED:return result
    risk=max(abs(trade.entry-trade.stop_initial),1e-9); peak=abs(trade.best_price-trade.entry)/risk; price=safe_float(context.get("price"),trade.entry); current=side_sign(trade.side)*(price-trade.entry)/risk; giveback=max(0.0,(peak-current)/max(peak,1e-9)) if peak>0 else 0.0; integrity=_active_path_integrity_v9532(trade,context)
    xi=dict((getattr(trade,"stage_plan",{}) or {}).get("execution_intelligence_v9532") or {})
    management_calibration=dict(
        ((getattr(trade,"breathing_profile",{}) or {}).get("setup_management_calibration_v9542") or {})
    )
    opened=_parse_time_any(getattr(trade,"opened_at","") or ""); elapsed=max(0.0,(now_utc()-opened).total_seconds()/60.0) if opened else 0.0
    reaction_window=max(12.0,safe_float(xi.get("reaction_window"),45.0)); hazard30=safe_float(xi.get("hazard_30"),0.0)
    # Hazard changes how quickly already-created profit is defended, never the
    # trade's original stop or setup admission. After the route's expected edge
    # window has elapsed, a decaying path gets a slightly earlier giveback lock.
    activation_mfe_r=PATH_DECAY_MIN_MFE_R
    effective_giveback=PATH_DECAY_MIN_GIVEBACK
    if xi and elapsed>reaction_window and hazard30>=0.30:
        effective_giveback=max(0.45,PATH_DECAY_MIN_GIVEBACK-0.08)
    if management_calibration.get("authority_active"):
        activation_mfe_r=clamp(
            safe_float(management_calibration.get("activation_mfe_r"),activation_mfe_r),
            PATH_DECAY_MIN_MFE_R,1.25,
        )
        effective_giveback=clamp(
            safe_float(management_calibration.get("giveback_threshold"),effective_giveback),
            0.45,0.68,
        )
    result.setdefault("path_decay_defense",{"peak_mfe_r":round(peak,4),"current_r":round(current,4),"giveback":round(giveback,4),"activation_mfe_r":round(activation_mfe_r,4),"effective_giveback_threshold":round(effective_giveback,4),"setup_management_calibration":management_calibration,"path_integrity":round(integrity,2),"elapsed_minutes":round(elapsed,2),"reaction_window_minutes":round(reaction_window,2),"hazard_30":round(hazard30,3),"activated":False,"schema_version":"path_decay_management_v9.5.42_setup_calibrated"})
    if peak>=activation_mfe_r and giveback>=effective_giveback and integrity<35.0 and not getattr(trade,"pre_tp1_protection_locked",False):
        target=_strict_breakeven_stop(trade); applied=_apply_protective_stop(trade,context,target)
        if applied:
            trade.management_state="PROTECT"; result["management_state"]="PROTECT"; result["recommended_stop"]=round_price(trade.stop_current); result["recommended_stop_reason"]="v9.5.42 setup-calibrated path-decay defense after positive excursion"; result["path_decay_defense"].update({"activated":True,"new_stop":round_price(trade.stop_current)})
    return result


def acceptance_no_followthrough_exit_profile_v9535(trade: ActiveTrade, context: dict[str, Any]) -> dict[str, Any]:
    price = safe_float(context.get("price"),trade.entry)
    profile = {
        "applies":False,
        "exit":False,
        "reason_code":"",
        "age_minutes":round(_active_trade_age_minutes(trade),2),
        "mfe_r":round(_trade_mfe_r(trade),4),
        "current_r":round(_trade_current_r(trade,price),4),
        "path_integrity":round(_active_path_integrity_v9532(trade,context),2),
        "preconfirmation_status":"UNAVAILABLE",
        "schema_version":ACCEPTANCE_NO_FOLLOWTHROUGH_SCHEMA_VERSION,
    }
    if not V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_ENABLED:
        profile["reason_code"]="DISABLED"; return profile
    if str(getattr(trade,"entry_stage","") or "").upper() != EntryStage.ACCEPTANCE.value:
        profile["reason_code"]="NOT_ACCEPTANCE_STAGE"; return profile
    if any((getattr(trade,"tp0_hit",False),getattr(trade,"tp1_hit",False),getattr(trade,"tp2_hit",False),getattr(trade,"tp3_hit",False))):
        profile["reason_code"]="FOLLOWTHROUGH_ALREADY_PROVEN"; return profile
    event = _linked_preconfirmation_event(trade,context)
    status = _preconfirm_event_status(event) if event else "UNAVAILABLE"
    profile["preconfirmation_status"] = status
    profile["preconfirmation_event_id"] = str(getattr(trade,"preconfirmation_event_id","") or "")
    if status not in {"FAILED","EXPIRED"}:
        profile["reason_code"]="THESIS_NOT_FACTUALLY_FAILED"; return profile
    profile["applies"] = True
    age = safe_float(profile["age_minutes"])
    mfe = safe_float(profile["mfe_r"])
    current_r = safe_float(profile["current_r"])
    integrity = safe_float(profile["path_integrity"])
    if (
        age >= V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MINUTES
        and mfe < V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_MFE_R
        and current_r <= V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_CURRENT_R
        and integrity < V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_PATH_INTEGRITY
    ):
        profile.update({
            "exit":True,
            "reason_code":"ACCEPTANCE_FIXED_HORIZON_FAILED_NO_FOLLOWTHROUGH",
            "threshold_minutes":V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MINUTES,
            "max_mfe_r":V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_MFE_R,
            "max_current_r":V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_CURRENT_R,
            "max_path_integrity":V9535_ACCEPTANCE_NO_FOLLOWTHROUGH_MAX_PATH_INTEGRITY,
        })
        return profile
    profile["reason_code"]="FAILED_EVENT_BUT_EARLY_EXIT_CONDITIONS_NOT_MET"
    return profile


def _manage_active_trade_acceptance_exit(trade: ActiveTrade, context: dict) -> dict:
    result = _manage_active_trade_path_decay(trade,context)
    if result.get("closed"):
        return result
    profile = acceptance_no_followthrough_exit_profile_v9535(trade,context)
    result["acceptance_no_followthrough"] = profile
    if not profile.get("exit"):
        return result
    price = safe_float(context.get("price"),trade.entry)
    result.setdefault("notes",[])
    result["closed"] = True
    result["action"] = Action.EXIT.value
    result["exit_price"] = round_price(price)
    result["current_pct"] = _trade_pct(trade.side,trade.entry,price)
    result["management_state"] = "THESIS_FAILURE_EARLY_EXIT"
    result["notes"].append(
        f"v9.5.35 early thesis-failure exit: {profile.get('reason_code')} | "
        f"age={profile.get('age_minutes')}m MFE={profile.get('mfe_r')}R "
        f"current={profile.get('current_r')}R integrity={profile.get('path_integrity')}"
    )
    trade.management_state = "THESIS_FAILURE_EARLY_EXIT"
    trade.status = "CLOSED"
    trade.last_action = Action.EXIT.value
    return _finalize_closed_trade_result(trade,result)


def model_local_thesis_invalidation_profile_base(
    trade: ActiveTrade, context: dict[str, Any],
) -> dict[str, Any]:
    """Resolve only factual invalidation of the exact linked live thesis.

    A forecast probability, calibration state or opposite candidate cannot close
    the trade here. Authority comes only from the already-linked precursor event
    recording an observed price invalidation of this exact side/thesis.
    """
    event = _linked_preconfirmation_event(trade, context)
    status = _preconfirm_event_status(event) if event else "UNAVAILABLE"
    outcome = str((event or {}).get("outcome") or "").upper()
    resolution_reason = str((event or {}).get("resolution_reason") or "").lower()
    evidence = dict((event or {}).get("resolution_evidence") or {})
    event_side = str((event or {}).get("side") or "").upper()
    same_side = bool(event_side and event_side == str(trade.side or "").upper())
    factual_invalidation = bool(
        status == "FAILED"
        and (
            outcome == "INVALIDATED"
            or bool(evidence.get("thesis_invalidated"))
            or "thesis_invalidated" in resolution_reason
        )
    )
    return {
        "applies": bool(event and same_side),
        "exit": bool(event and same_side and factual_invalidation),
        "status": status,
        "outcome": outcome or "UNAVAILABLE",
        "event_side": event_side or "UNKNOWN",
        "trade_side": str(trade.side or "UNKNOWN").upper(),
        "same_side": same_side,
        "factual_market_invalidation": factual_invalidation,
        "invalidation_level": safe_float((event or {}).get("invalidation_level"), 0.0),
        "invalidation_ts": int(safe_float(evidence.get("invalidation_ts"), 0.0)),
        "resolved_at": str((event or {}).get("resolved_at") or ""),
        "preconfirmation_event_id": str(getattr(trade, "preconfirmation_event_id", "") or ""),
        "prediction_or_calibration_can_exit": False,
        "opposite_candidate_can_open_second_position": False,
        "post_close_fresh_rescan_required": True,
        "reason_code": (
            "MODEL_LOCAL_THESIS_FACTUALLY_INVALIDATED"
            if factual_invalidation and same_side
            else "EVENT_SIDE_MISMATCH"
            if event and not same_side
            else "NO_FACTUAL_LINKED_INVALIDATION"
        ),
        "schema_version": V9552_SCHEMA_VERSION,
    }


def _manage_active_trade_thesis_invalidation(trade: ActiveTrade, context: dict) -> dict:
    """Close a factually invalidated thesis before it occupies the next setup."""
    result = _manage_active_trade_acceptance_exit(trade, context)
    if result.get("closed"):
        return result
    invalidation = model_local_thesis_invalidation_profile_v9552(trade, context)
    result["model_local_thesis_invalidation"] = invalidation
    if not invalidation.get("exit"):
        return result
    price = safe_float(context.get("price"), trade.entry)
    result.setdefault("notes", [])
    result.update({
        "closed": True,
        "action": Action.EXIT.value,
        "exit_price": round_price(price),
        "current_pct": _trade_pct(trade.side, trade.entry, price),
        "management_state": "MODEL_LOCAL_THESIS_INVALIDATION_EXIT",
        "close_reason": "MODEL_LOCAL_THESIS_INVALIDATION",
    })
    result["notes"].append(
        "v9.5.52 factual exit: linked model-local thesis was invalidated; "
        "capacity must be released for a fresh same-cycle market rescan"
    )
    trade.management_state = "MODEL_LOCAL_THESIS_INVALIDATION_EXIT"
    trade.status = "CLOSED"
    trade.last_action = Action.EXIT.value
    return _finalize_closed_trade_result(trade, result)


def model_local_thesis_invalidation_profile_v9552(
    trade: ActiveTrade, context: dict[str, Any],
) -> dict[str, Any]:
    """Treat later observed invalidation as factual even after confirmation."""
    profile = dict(model_local_thesis_invalidation_profile_base(trade, context) or {})
    event = _linked_preconfirmation_event(trade, context)
    if not event:
        profile["schema_version_v9571"] = V9571_SCHEMA_VERSION
        return profile
    evidence = dict(event.get("resolution_evidence") or {})
    event_side = str(event.get("side") or "").upper()
    same_side = bool(event_side and event_side == str(trade.side or "").upper())
    outcome = str(event.get("outcome") or "").upper()
    resolution_reason = str(event.get("resolution_reason") or "").lower()
    factual_invalidation = bool(
        outcome == "INVALIDATED"
        or evidence.get("thesis_invalidated")
        or evidence.get("post_confirmation_invalidation")
        or "thesis_invalidated" in resolution_reason
    )
    exit_trade = bool(same_side and factual_invalidation)
    profile.update({
        "applies": same_side,
        "exit": exit_trade,
        "same_side": same_side,
        "factual_market_invalidation": factual_invalidation,
        "post_confirmation_invalidation": bool(evidence.get("post_confirmation_invalidation")),
        "invalidation_ts": int(safe_float(evidence.get("invalidation_ts"), 0.0)),
        "status_label_is_not_exit_authority": True,
        "reason_code": (
            "MODEL_LOCAL_THESIS_FACTUALLY_INVALIDATED"
            if exit_trade else "EVENT_SIDE_MISMATCH" if not same_side
            else "NO_FACTUAL_LINKED_INVALIDATION"
        ),
        "schema_version_v9571": V9571_SCHEMA_VERSION,
    })
    return profile


def _classic_stop_is_delayed_ready_v9570(trade: ActiveTrade) -> bool:
    """Only an established delayed TP1 lock (or TP2 lock) may trail the stop."""
    return bool(
        getattr(trade, "tp1_stop_locked", False)
        or getattr(trade, "tp2_stop_locked", False)
    )


def _restore_initial_stop_v9570(trade: ActiveTrade) -> bool:
    """Migrate an open pre-lock trade back to its original structural stop."""
    if _classic_stop_is_delayed_ready_v9570(trade):
        return False
    initial = safe_float(getattr(trade, "stop_initial", 0.0), 0.0)
    current = safe_float(getattr(trade, "stop_current", 0.0), 0.0)
    if initial <= 0.0:
        return False
    changed = abs(current - initial) > 1e-9
    trade.stop_current = round_price(initial)
    trade.pre_tp1_protection_locked = False
    trade.pre_tp1_protection_at = ""
    trade.pre_tp1_protection_ratio = 0.0
    trade.pre_tp1_protection_threshold = 0.0
    trade.pre_tp1_protection_scope = ""
    trade.protection_activation_mfe_r = 0.0
    trade.protection_activation_current_r = 0.0
    trade.protection_activation_stop = 0.0
    trade.protection_locked_r = 0.0
    return changed


def manage_active_trade_v9570(trade: ActiveTrade, context: dict) -> dict:
    """Run the supplied old management core with a strict pre-lock stop gate."""
    migrated = _restore_initial_stop_v9570(trade)
    result = _manage_active_trade_thesis_invalidation(trade, context)
    if not isinstance(result, dict):
        return result
    result["classic_management_v9570"] = {
        "old_core": "v9.5.52_effective_management_from_supplied_legacy_bot",
        "v9567_live_probe_guard_enabled": False,
        "pre_lock_stop_policy": "INITIAL_STRUCTURAL_STOP_ONLY",
        "migrated_from_early_ratchet": migrated,
        "schema_version": V9570_SCHEMA_VERSION,
    }
    if result.get("closed") or _classic_stop_is_delayed_ready_v9570(trade):
        return result

    suppressed = _restore_initial_stop_v9570(trade)
    path_decay = result.get("path_decay_defense")
    if isinstance(path_decay, dict) and path_decay.get("activated"):
        path_decay.update({
            "activated": False,
            "suppressed_by_v9570": True,
            "new_stop": round_price(trade.stop_current),
        })
        suppressed = True

    if result.get("action") == Action.PROTECT.value:
        result["action"] = Action.HOLD.value
        trade.last_action = Action.HOLD.value
    if result.get("action") not in {Action.TP0.value, Action.TP1.value, Action.TP2.value}:
        result["management_state"] = "SUPPORTED" if not trade.tp1_hit else "TP1_BE_DELAY_WAIT"
        trade.management_state = result["management_state"]
    result["recommended_stop"] = round_price(trade.stop_current)
    result["recommended_stop_reason"] = (
        "v9.5.70 classic: початковий структурний стоп до активації "
        "BE_DELAY_ENGINE після TP1"
    )
    if migrated or suppressed:
        result.setdefault("notes", []).append(
            "v9.5.70: раннє TP0/probe/path-decay перенесення стопа скасовано; "
            "відновлено початковий структурний stop"
        )
    return result


manage_active_trade = manage_active_trade_v9570
# ==========================================================
# PERSISTENCE  (ті самі імена файлів, та сама схема угоди)
# ==========================================================
# Рішення "Історія + очистка легасі": масиви trades і signals зберігаються
# повністю, а ~40 версійно-специфічних аудит-блобів більше не пишуться.
# Схема закритої угоди лишається сумісною, щоб супровід і аналітика читали
# journal без змін.

TRADE_REGIME_LINEAGE_SCHEMA_VERSION = "trade_regime_lineage_v10.0.0"
STATE_SCHEMA_VERSION = "organic_state_v10.0.0"


def classify_trade_result(result_r: Any = None, pnl_pct: Any = None, outcome_status: str = "") -> str:
    """Classify economic outcome independently from the mechanical close action."""
    value = None
    for raw in (result_r, pnl_pct):
        try:
            if raw is not None and raw != "":
                candidate = float(raw)
                if math.isfinite(candidate):
                    value = candidate
                    break
        except Exception:
            continue
    if value is None:
        return "UNRESOLVED" if str(outcome_status or "").upper() not in {"RESOLVED", "CLOSED"} else "BREAKEVEN"
    if value > 1e-9:
        return "WIN"
    if value < -1e-9:
        return "LOSS"
    return "BREAKEVEN"


def trade_open_regime_lineage(record: Any) -> dict[str, Any]:
    """The regime the trade was opened in, and where that value came from."""
    if not isinstance(record, dict):
        return {"regime": "", "source": "MISSING_OR_INVALID", "conflict": False}
    resolved = str(record.get("opened_regime") or "").strip().upper()
    legacy = str(record.get("regime") or "").strip().upper()
    if resolved and legacy and resolved != legacy:
        return {
            "regime": resolved, "source": "OPENED_REGIME_FIELD",
            "conflict": True, "legacy_regime_raw": legacy,
        }
    if resolved:
        return {"regime": resolved, "source": "OPENED_REGIME_FIELD", "conflict": False}
    if legacy:
        return {"regime": legacy, "source": "LEGACY_REGIME_FIELD", "conflict": False}
    return {"regime": "", "source": "MISSING_OR_INVALID", "conflict": False}


def compact_execution_intelligence_v9532(intel: dict[str, Any]) -> dict[str, Any]:
    """Normalize an execution-intelligence record to the xi40 allow-list.

    Idempotent by construction: compact(compact(x)) must not replace measured
    values with neutral defaults, because the supervision layer reads
    reaction_window and hazard_30 straight out of this record.
    """
    if not isinstance(intel, dict) or not intel:
        return {}
    keys = (
        "router", "state", "kind", "asi", "structural", "runway_r",
        "regime", "regime_bias", "regime_fit", "regime_uncertainty",
        "setup_pct", "ctx_edge", "outcome_p", "outcome_uncertainty",
        "hazard_15", "hazard_30", "hazard_60", "hazard_reliability",
        "reaction_window", "geo_n", "assistant_opinions", "assistant_metrics",
        "assistant_feedback_complete", "schema",
    )
    normalized = {key: copy.deepcopy(intel.get(key)) for key in keys if key in intel}
    normalized.setdefault("schema", EXECUTION_INTELLIGENCE_SCHEMA)
    return normalized


def compact_trade_for_journal(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact a closed trade while preserving outcome learning and joins."""
    if not isinstance(payload, dict):
        return {}
    pnl = payload.get("pnl")
    if pnl is None:
        pnl = payload.get("realized_return_pct", payload.get("result_pct"))
    pnl_r = payload.get("pnl_r", payload.get("result_r"))
    raw_result = str(payload.get("result") or "").upper()
    close_action = payload.get("close_action") or payload.get("action")
    if not close_action and raw_result in {"STOP", "EXIT", "TP0", "TP1", "TP2", "TP3", "PROTECT"}:
        close_action = raw_result
    result_class = payload.get("result_class")
    if not result_class or str(result_class).upper() not in {"WIN", "LOSS", "BREAKEVEN", "UNRESOLVED"}:
        result_class = classify_trade_result(pnl_r, pnl, str(payload.get("outcome_status") or ""))
    regime_lineage = trade_open_regime_lineage(payload)
    resolved_open_regime = str(regime_lineage.get("regime") or "")
    stored_lineage_source = str(payload.get("regime_lineage_source") or regime_lineage.get("source") or "MISSING_OR_INVALID")
    prior_conflict = bool(payload.get("regime_lineage_conflict"))
    legacy_conflict_value = payload.get("regime_lineage_legacy_value")
    if legacy_conflict_value in (None, "") and regime_lineage.get("conflict"):
        legacy_conflict_value = regime_lineage.get("legacy_regime_raw")
    compact = {
        "id": payload.get("id"),
        "signal_id": payload.get("signal_id", payload.get("primary_signal_id")),
        "time": payload.get("time", payload.get("opened_at")),
        "closed_at": payload.get("closed_at"),
        "side": payload.get("side"),
        "setup_family": payload.get("setup_family"),
        "setup_type": payload.get("setup_type"),
        "score": payload.get("score", payload.get("quality")),
        "entry_score": payload.get("entry_score", payload.get("entry_quality", payload.get("quality"))),
        "entry_score_source": payload.get("entry_score_source"),
        "evaluation_entry_quality": payload.get("evaluation_entry_quality", payload.get("entry_score", payload.get("entry_quality"))),
        "preplan_entry_quality": payload.get("preplan_entry_quality"),
        "trade_entry_quality": payload.get("trade_entry_quality", payload.get("entry_score", payload.get("entry_quality"))),
        "setup_quality": payload.get("setup_quality"),
        "timing_quality": payload.get("timing_quality"),
        "trade_quality": payload.get("trade_quality"),
        "trade_profile_source": payload.get("trade_profile_source"),
        "trade_profile_calibration_status": payload.get("trade_profile_calibration_status"),
        "trade_profile_empirical_review_ready": payload.get("trade_profile_empirical_review_ready"),
        "trade_profile_schema_version": payload.get("trade_profile_schema_version"),
        "trade_profile_fallback_used": payload.get("trade_profile_fallback_used"),
        "entry": payload.get("entry", payload.get("entry_price")),
        "stop_initial": payload.get("stop_initial", payload.get("initial_stop")),
        "stop_at_close": payload.get("stop_at_close", payload.get("stop_current")),
        "best_price": payload.get("best_price"),
        "worst_price": payload.get("worst_price"),
        "tp0": payload.get("tp0"),
        "tp1": payload.get("tp1"),
        "tp2": payload.get("tp2"),
        "tp3": payload.get("tp3"),
        "rr0": payload.get("rr0"),
        "rr1": payload.get("rr1"),
        "rr2": payload.get("rr2"),
        "rr3": payload.get("rr3"),
        "mfe_r": payload.get("mfe_r"),
        "mae_r": payload.get("mae_r"),
        "mfe_capture_ratio": payload.get("mfe_capture_ratio"),
        "mfe_giveback_ratio": payload.get("mfe_giveback_ratio"),
        "tp0_protect_threshold": payload.get("tp0_protect_threshold"),
        "pre_tp1_protection_locked": payload.get("pre_tp1_protection_locked"),
        "pre_tp1_protection_at": payload.get("pre_tp1_protection_at"),
        "pre_tp1_protection_ratio": payload.get("pre_tp1_protection_ratio"),
        "pre_tp1_protection_threshold": payload.get("pre_tp1_protection_threshold"),
        "pre_tp1_protection_scope": payload.get("pre_tp1_protection_scope"),
        "protection_activation_mfe_r": payload.get("protection_activation_mfe_r"),
        "protection_activation_current_r": payload.get("protection_activation_current_r"),
        "protection_activation_stop": payload.get("protection_activation_stop"),
        "protection_peak_mfe_r": payload.get("protection_peak_mfe_r"),
        "protection_locked_r": payload.get("protection_locked_r"),
        "protection_ratchet_count": payload.get("protection_ratchet_count"),
        "protection_last_ratchet_at": payload.get("protection_last_ratchet_at"),
        "protection_last_ratchet_stop": payload.get("protection_last_ratchet_stop"),
        "protection_ratchet_missed_due_to_price": payload.get("protection_ratchet_missed_due_to_price"),
        "protection_last_evaluated_mfe_r": payload.get("protection_last_evaluated_mfe_r"),
        "protection_ratchet_new_peak_events": payload.get("protection_ratchet_new_peak_events"),
        "protection_ratchet_evidence": payload.get("protection_ratchet_evidence"),
        "protection_ratchet_evidence_count": payload.get("protection_ratchet_evidence_count", len(payload.get("protection_ratchet_evidence") or [])),
        "management_state": payload.get("management_state"),
        "management_evidence_schema_version": payload.get("management_evidence_schema_version"),
        "geometry_complete": payload.get("geometry_complete"),
        "tp0_hit": payload.get("tp0_hit"),
        "tp1_hit": payload.get("tp1_hit"),
        "tp2_hit": payload.get("tp2_hit"),
        "tp3_hit": payload.get("tp3_hit"),
        "execution_stage": payload.get("execution_stage", payload.get("entry_stage")),
        "execution_source": payload.get("execution_source"),
        "execution_tier": payload.get("execution_tier"),
        "canonical_setup_family": payload.get("canonical_setup_family"),
        "family_episode_key": payload.get("family_episode_key"),
        "anchor_id": payload.get("anchor_id"),
        "anchor_kind": payload.get("anchor_kind"),
        "anchor_level": payload.get("anchor_level"),
        "reaction_latency_minutes": payload.get("reaction_latency_minutes"),
        "entry_distance_atr": payload.get("entry_distance_atr"),
        "stop_distance_atr": payload.get("stop_distance_atr"),
        "planned_entry": payload.get("planned_entry"),
        "planned_stop": payload.get("planned_stop"),
        "result": result_class,
        "result_class": result_class,
        "close_action": close_action,
        "close_reason": payload.get("close_reason", close_action),
        "pnl": pnl,
        "pnl_r": pnl_r,
        "bot_version_at_entry": payload.get("bot_version_at_entry"),
        "architecture_version_at_entry": payload.get("architecture_version_at_entry"),
        "journal_schema_at_entry": payload.get("journal_schema_at_entry"),
        "preconfirmation_event_id": payload.get("preconfirmation_event_id"),
        "mfe": payload.get("mfe", payload.get("mfe_pct")),
        "risk_pct": payload.get("risk_pct", payload.get("position_risk_pct")),
        "position_risk_pct": payload.get("position_risk_pct", payload.get("risk_pct")),
        "opened_regime": resolved_open_regime,
        "regime": resolved_open_regime,  # compatibility alias; opened_regime is canonical
        "regime_lineage_source": stored_lineage_source,
        "regime_lineage_conflict": True if (prior_conflict or bool(regime_lineage.get("conflict"))) else None,
        "regime_lineage_legacy_value": legacy_conflict_value,
        "regime_lineage_schema_version": TRADE_REGIME_LINEAGE_SCHEMA_VERSION if resolved_open_regime else None,
        "ml_eligible": payload.get("ml_eligible", True),
        "loss_code": payload.get("loss_code"),
        "loss_secondary": payload.get("loss_secondary"),
        "tp0_hit_at": payload.get("tp0_hit_at"),
        "tp0_hit_ts": int(safe_float(payload.get("tp0_hit_ts"), 0.0)) or None,
        "tp1_hit_at": payload.get("tp1_hit_at"),
        "tp1_hit_ts": int(safe_float(payload.get("tp1_hit_ts"), 0.0)) or None,
        "opened_at_ms": payload.get("opened_at_ms", _opened_at_ms(payload.get("opened_at"))),
        "age_minutes": payload.get("age_minutes"),
        "compact_trade_schema_version": TRADE_PLAN_SCHEMA_VERSION,
    }
    intelligence = payload.get("execution_intelligence")
    if isinstance(intelligence, dict) and intelligence:
        compact["execution_intelligence"] = compact_execution_intelligence_v9532(intelligence)
    return {key: value for key, value in compact.items() if value not in (None, "", {}, [])}


def compact_signal_for_journal(payload: dict[str, Any]) -> dict[str, Any]:
    """One signal row: enough to audit the decision, nothing to bloat the file.

    The old journal stored 100+ per-version keys on every one of 500 signals.
    Everything here is either an identity/join key or a measured number.
    """
    if not isinstance(payload, dict):
        return {}
    compact = {
        "id": payload.get("id") or payload.get("signal_id"),
        "time": payload.get("time"),
        "side": payload.get("side"),
        "setup_type": payload.get("setup_type"),
        "setup_family": payload.get("setup_family"),
        "canonical_setup_family": payload.get("canonical_setup_family"),
        "score": payload.get("score", payload.get("quality")),
        "entry_quality": payload.get("entry_quality"),
        "setup_quality": payload.get("setup_quality"),
        "timing_quality": payload.get("timing_quality"),
        "trade_quality": payload.get("trade_quality"),
        "regime": payload.get("regime"),
        "session": payload.get("session"),
        "price": payload.get("price", payload.get("current_price")),
        "anchor_id": payload.get("anchor_id"),
        "anchor_kind": payload.get("anchor_kind"),
        "anchor_level": payload.get("anchor_level"),
        "anchor_state": payload.get("anchor_state"),
        "reaction_latency_minutes": payload.get("reaction_latency_minutes"),
        "entry_distance_atr": payload.get("entry_distance_atr"),
        "runway_r": payload.get("runway_r"),
        "stop_distance_atr": payload.get("stop_distance_atr"),
        "action": payload.get("action", payload.get("decision")),
        "reason": payload.get("reason"),
        "executed": payload.get("executed"),
        "preconfirmation_event_id": payload.get("preconfirmation_event_id"),
        "entry_stage": payload.get("entry_stage"),
        "risk_pct": payload.get("risk_pct", payload.get("position_risk_pct")),
        "degradation_status": payload.get("degradation_status"),
        "htf_state": payload.get("htf_state"),
        "score_components": payload.get("score_components"),
        "reaction_gates": payload.get("reaction_gates"),
        "competing_hypotheses": payload.get("competing_hypotheses"),
        "hypothesis_rank": payload.get("hypothesis_rank"),
        "bot_version": payload.get("bot_version"),
        "architecture_version": payload.get("architecture_version"),
        "schema_version": SCHEMA_VERSION,
    }
    return {key: value for key, value in compact.items() if value not in (None, "", {}, [])}


def _journal_feature_map(payload: dict[str, Any]) -> dict[str, Any]:
    """Numeric feature vector of one row, kept as measured and size-bounded."""
    if not isinstance(payload, dict):
        return {}
    components = payload.get("score_components")
    sources = [
        payload.get("features"),
        payload.get("score_features"),
        components.get("features") if isinstance(components, dict) else None,
    ]
    raw = next((source for source in sources if isinstance(source, dict) and source), {})
    features: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        features[str(key)] = round(float(value), 6)
        if len(features) >= MAX_JOURNAL_FEATURE_KEYS:
            break
    return features


def lean_training_signal(payload: dict[str, Any]) -> dict[str, Any]:
    """One training row: identity, taxonomy and the numeric features it measured."""
    if not isinstance(payload, dict):
        return {}
    signal_id = str(payload.get("id") or payload.get("signal_id") or "").strip()
    features = _journal_feature_map(payload)
    if not signal_id or not features:
        return {}
    return {
        "id": signal_id,
        "time": payload.get("time"),
        "side": payload.get("side"),
        "setup_family": payload.get("setup_family"),
        "setup_type": payload.get("setup_type"),
        "features": features,
    }


def compact_preconfirmation_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the thesis and its resolution; drop the removed-ML residue.

    Events inherited from the legacy journal average ~9.6 KB, of which ~7.5 KB is
    forecast machinery v10 deleted. Supervision only ever asks whether the event
    linked to the active trade is CONFIRMED, so the residue was being rewritten and
    re-committed every fifteen minutes for nothing.
    """
    if not isinstance(payload, dict):
        return {}
    if not str(payload.get("event_id") or payload.get("id") or "").strip():
        return {}
    return {key: payload[key] for key in PRECONFIRM_EVENT_KEEP_KEYS if key in payload}


def deduplicate_closed_trades(trades: list[Any]) -> list[dict[str, Any]]:
    """Не даємо ML двічі вчитись на одній і тій самій закритій угоді.

    Ключ максимально консервативний: trade id + signal_id + close_action; якщо id
    порожній, запис лишається, бо це старий/пошкоджений журнал.
    """
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


def _retain_signal_records(records: list[Any], protected_signal_ids: set[str], cap: int) -> list[dict[str, Any]]:
    """Retain records referenced by trades before applying FIFO.

    If protected records alone exceed the cap they are all kept: referential
    integrity beats a round number when silent ML label loss is expensive.
    """
    cleaned = [r for r in records or [] if isinstance(r, dict) and r.get("id")]
    protected = [r for r in cleaned if str(r.get("id") or "") in protected_signal_ids]
    protected_keys = {str(r.get("id") or "") for r in protected}
    unprotected = [r for r in cleaned if str(r.get("id") or "") not in protected_keys]
    room = max(0, int(cap) - len(protected))
    kept = protected + (unprotected[-room:] if room else [])
    order = {id(record): idx for idx, record in enumerate(cleaned)}
    return sorted(kept, key=lambda record: order.get(id(record), 0))


def _retain_signal_events(events: list[Any], protected_signal_ids: set[str], cap: int) -> list[dict[str, Any]]:
    cleaned = [e for e in events or [] if isinstance(e, dict)]
    protected = [e for e in cleaned if str(e.get("signal_id") or "") in protected_signal_ids]
    protected_obj_ids = {id(e) for e in protected}
    unprotected = [e for e in cleaned if id(e) not in protected_obj_ids]
    room = max(0, int(cap) - len(protected))
    kept = protected + (unprotected[-room:] if room else [])
    order = {id(record): idx for idx, record in enumerate(cleaned)}
    return sorted(kept, key=lambda record: order.get(id(record), 0))


# ==========================================================
# STATE
# ==========================================================

def load_state() -> dict[str, Any]:
    """Read last_signal_v6_4.json.

    Only three things survive a version change on purpose: the open trade (the
    unchanged supervision layer owns it), the anchor memory (an early entry must
    not be lost because the reaction happened between two cron runs), and the
    regime memory. Everything else is recomputed from the market every run.
    """
    raw = load_json(STATE_FILE, {})
    source_arch = str(raw.get("architecture_version") or "")
    compatible = source_arch == ARCHITECTURE_VERSION
    anchors_raw = raw.get(STATE_ANCHOR_KEY)
    anchors = [
        anchor_to_dict(anchor)
        for anchor in (anchor_from_dict(item) for item in (anchors_raw or []) if isinstance(item, dict))
        if anchor is not None
    ][-ANCHOR_MEMORY_LIMIT:]
    regime_memory = raw.get("regime_memory") if compatible else {}
    return {
        "version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "active_trade": raw.get("active_trade"),
        STATE_ANCHOR_KEY: anchors,
        "regime_memory": regime_memory if isinstance(regime_memory, dict) else {},
        "latest_signal": raw.get("latest_signal"),
        "last_message_key": raw.get("last_message_key", "") if compatible else "",
        "history": list(raw.get("history") or [])[-MAX_HISTORY:],
        "state_migration": {
            "source_architecture": source_arch or "NONE",
            "memory_compatible": compatible,
            "anchors_preserved": len(anchors),
            "preserve_active_trade": True,
            "schema_version": STATE_SCHEMA_VERSION,
        },
    }


def save_state(state: dict[str, Any]) -> None:
    state["version"] = BOT_VERSION
    state["architecture_version"] = ARCHITECTURE_VERSION
    state["updated_at"] = iso_now()
    state["history"] = list(state.get("history") or [])[-MAX_HISTORY:]
    state[STATE_ANCHOR_KEY] = list(state.get(STATE_ANCHOR_KEY) or [])[-ANCHOR_MEMORY_LIMIT:]
    atomic_json_write(STATE_FILE, state)


def active_trade_from_state(state: dict[str, Any]) -> Optional[ActiveTrade]:
    raw = (state or {}).get("active_trade")
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
        if not str(clean.get("opened_regime") or "").strip():
            clean["opened_regime"] = str(trade_open_regime_lineage(raw).get("regime") or "")
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


def store_anchors(state: dict[str, Any], anchors: list[Anchor]) -> None:
    state[STATE_ANCHOR_KEY] = [anchor_to_dict(anchor) for anchor in anchors or []][-ANCHOR_MEMORY_LIMIT:]


def stored_anchors(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in list((state or {}).get(STATE_ANCHOR_KEY) or []) if isinstance(item, dict)]


# ==========================================================
# JOURNAL
# ==========================================================

def load_journal() -> dict[str, Any]:
    """Read signal_journal_v6_4.json and normalize the arrays the bot owns."""
    journal = load_json(JOURNAL_FILE, {})
    previous_version = safe_int(journal.get("journal_version"), 1)
    for key in ("signals", "training_signals", "signal_events", "trades",
                "preconfirmation_events"):
        journal.setdefault(key, [])
    for key in ("setup_statistics", "entry_quality_audit", "calendar_statistics",
                "learning_status", "degradation"):
        journal.setdefault(key, {})
    journal.setdefault("analytics", {
        "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "wilson_lower": 0.0,
        "net_r": 0.0, "expectancy_r": 0.0, "by_family": {}, "by_setup": {},
    })
    dropped = sorted(key for key in journal if key not in JOURNAL_CORE_KEYS)
    journal["journal_version"] = JOURNAL_VERSION
    journal["version"] = BOT_VERSION
    journal["architecture_version"] = ARCHITECTURE_VERSION
    journal.setdefault("migration", {}).update({
        "mode": "ORGANIC_V10_LEGACY_BLOB_PRUNE",
        "previous_journal_version": previous_version,
        "legacy_keys_found_on_load": dropped,
        "trades_preserved": len(list(journal.get("trades") or [])),
        "signals_preserved": len(list(journal.get("signals") or [])),
        "schema_version": SCHEMA_VERSION,
    })
    return journal


def save_journal(journal: dict[str, Any]) -> None:
    """Write outcome history only; every legacy audit blob is dropped here."""
    journal["updated_at"] = iso_now()
    journal["journal_version"] = JOURNAL_VERSION
    journal["version"] = BOT_VERSION
    journal["architecture_version"] = ARCHITECTURE_VERSION

    journal["trades"] = deduplicate_closed_trades([
        compact for item in list(journal.get("trades") or [])
        if isinstance(item, dict)
        for compact in [compact_trade_for_journal(item)] if compact
    ])[-MAX_JOURNAL:]

    protected = {
        str(t.get("signal_id") or "") for t in journal.get("trades", [])
        if isinstance(t, dict) and str(t.get("signal_id") or "").strip()
    }
    journal["signals"] = _retain_signal_records(
        [
            compact for item in list(journal.get("signals") or [])
            if isinstance(item, dict)
            for compact in [compact_signal_for_journal(item)] if compact
        ],
        protected, MAX_JOURNAL,
    )
    journal["training_signals"] = _retain_signal_records(
        [
            lean for item in list(journal.get("training_signals") or [])
            if isinstance(item, dict)
            for lean in [lean_training_signal(item)] if lean
        ],
        protected, MAX_JOURNAL,
    )
    journal["signal_events"] = _retain_signal_events(
        list(journal.get("signal_events") or []), protected, MAX_JOURNAL,
    )
    journal["preconfirmation_events"] = [
        compact for item in list(journal.get("preconfirmation_events") or [])
        if isinstance(item, dict)
        for compact in [compact_preconfirmation_event(item)] if compact
    ][-PRECONFIRM_EMBEDDED_JOURNAL_LIMIT:]

    pruned = {key: value for key, value in journal.items() if key in JOURNAL_CORE_KEYS}
    pruned.setdefault("migration", {}).update({
        "legacy_keys_dropped_on_save": sorted(key for key in journal if key not in JOURNAL_CORE_KEYS),
        "completed_at": iso_now(),
    })
    atomic_json_write(JOURNAL_FILE, pruned)
    journal.clear()
    journal.update(pruned)
# ==========================================================
# TELEGRAM
# ==========================================================
# Two audiences, two builders:
#   build_decision_message — the entry side, rewritten for the v10
#       anchor -> 3m reaction -> market architecture. It must also produce the
#       "no entry" report, because that message is the user's only view of the
#       bot on a 15-minute cron when nothing trades.
#   build_follow_message — the supervision side, carried over unchanged. The
#       trade-management layer is deliberately untouched, so its report is too.


def send_telegram(text: str) -> bool:
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


def plain_telegram_text(text: str) -> str:
    return html.unescape(str(text or "").replace("<b>", "").replace("</b>", ""))


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ==========================================================
# DECISION MESSAGE  (entry side)
# ==========================================================

ACTION_LABELS = {
    Action.ENTRY.value: "ВХІД У УГОДУ",
    Action.RISKY_ENTRY.value: "РАННІЙ / СТАДІЙНИЙ ВХІД",
    Action.PROBE_ENTRY.value: "ПРОБНИЙ ВХІД",
    Action.NO_SETUP.value: "СИГНАЛУ НЕМАЄ",
}

ANCHOR_KIND_LABELS = {
    AnchorKind.SWEEP_LOW.value: "ЗНЯТИЙ ЛОУ",
    AnchorKind.SWEEP_HIGH.value: "ЗНЯТИЙ ХАЙ",
    AnchorKind.DEMAND_ZONE.value: "ЗОНА ПОПИТУ",
    AnchorKind.SUPPLY_ZONE.value: "ЗОНА ПРОПОЗИЦІЇ",
    AnchorKind.FAIR_VALUE_GAP.value: "FVG",
    AnchorKind.BREAK_LEVEL.value: "РІВЕНЬ ПРОБОЮ",
    AnchorKind.RANGE_EDGE.value: "КРАЙ ДІАПАЗОНУ",
    AnchorKind.VALUE_LEVEL.value: "РІВЕНЬ ВАРТОСТІ",
    AnchorKind.STRUCTURE_SHIFT.value: "ЗЛАМ СТРУКТУРИ",
}


def _anchor_kind_label(kind: Any) -> str:
    return ANCHOR_KIND_LABELS.get(str(kind or ""), str(kind or "").replace("_", " ").title())


def _reaction_summary(candidate: Optional[Candidate]) -> str:
    """One human line for why the 3m reaction counted as an entry."""
    gates = dict((candidate.reaction if candidate else {}) or {})
    rejection = dict(gates.get("GATE_REJECTION") or {})
    displacement = dict(gates.get("GATE_DISPLACEMENT") or {})
    proximity = dict(gates.get("GATE_PROXIMITY") or {})
    parts: list[str] = []
    if rejection.get("wick_ratio") is not None:
        parts.append(f"відбій: тінь {safe_float(rejection.get('wick_ratio')):.0%}")
    if rejection.get("closed_back_inside"):
        parts.append("закриття всередині рівня")
    if displacement.get("body_atr3"):
        parts.append(f"імпульс {safe_float(displacement.get('body_atr3')):.2f} ATR3")
    if proximity.get("distance_atr") is not None:
        parts.append(f"{safe_float(proximity.get('distance_atr')):.2f} ATR від рівня")
    latency = safe_float(gates.get("latency_minutes"))
    if latency > 0:
        parts.append(f"латентність {latency:.0f} хв")
    return " | ".join(parts) if parts else "3M-РЕАКЦІЯ ПІДТВЕРДЖЕНА"


def _approaching_entry(audit: dict[str, Any]) -> dict[str, Any]:
    nearest = dict((audit.get("anchor_watch") or {}).get("nearest") or {})
    if not nearest:
        return {}
    # Inside twice the reaction zone the level is being tested but the 3m
    # evidence is not there yet — worth telling the user an entry is forming.
    if safe_float(nearest.get("distance_atr"), 9.9) <= 2.0 * ANCHOR_ZONE_ATR:
        return nearest
    return {}


def _risk_budget_line(audit: dict[str, Any]) -> str:
    budget = dict(audit.get("daily_risk") or {})
    if not budget:
        return ""
    used = safe_float(budget.get("daily_risk_used"))
    open_risk = safe_float(budget.get("open_position_risk"))
    cap = safe_float(budget.get("daily_risk_cap"), DAILY_RISK_CAP)
    line = (
        f"<b>Ризик сьогодні:</b> {used:.2f}% закрито + {open_risk:.2f}% у позиції з {cap:.2f}% — "
        f"вільно {safe_float(budget.get('risk_budget_left_before')):.2f}%"
    )
    if budget.get("exhausted"):
        line += " — бюджет вичерпано"
    return line


def _plan_lines(plan: TradePlan) -> list[str]:
    stage_plan = dict(plan.stage_plan or {})
    geometry = dict(stage_plan.get("geometry") or {})
    atr15 = safe_float(geometry.get("atr15"))
    stop_atr = safe_float(geometry.get("decision_distance")) / atr15 if atr15 > 0 else 0.0
    lines = [
        "",
        "<b>План:</b>",
        f"Вхід <b>{_fmt_price(plan.entry)}</b> | Стоп <b>{_fmt_price(plan.stop)}</b>",
    ]
    if plan.tp0:
        lines.append(f"TP0 {_fmt_price(plan.tp0)} (RR {plan.rr0}) | TP1 {_fmt_price(plan.tp1)} (RR {plan.rr1})")
    else:
        lines.append(f"TP1 {_fmt_price(plan.tp1)} (RR {plan.rr1})")
    lines.append(f"TP2 {_fmt_price(plan.tp2)} (RR {plan.rr2}) | TP3 {_fmt_price(plan.tp3)} (RR {plan.rr3})")
    lines.append(
        f"Стадія: <b>{_esc(plan.entry_stage)}</b> | Ризик: {safe_float(plan.position_risk_pct):.3f}% | "
        f"Стоп: {stop_atr:.2f} ATR"
    )
    runway = dict(stage_plan.get("runway_target_management") or {})
    if runway.get("runway_r") is not None:
        lines.append(
            f"<b>Runway:</b> {safe_float(runway.get('runway_r')):.2f}R до "
            f"{_esc(str(runway.get('nearest_target_kind') or '').replace('_', ' ').title())}"
        )
    return lines


def build_decision_message(context: dict[str, Any], decision: Decision) -> str:
    audit = dict(decision.audit or {})
    price = safe_float(decision.current_price, safe_float(context.get("price"), 0.0))
    regime_line = f"<b>Режим:</b> {regime_label(decision.regime)} | <b>Сесія:</b> {_esc(str(context.get('session_name') or ''))}"

    if decision.action == Action.NO_SETUP.value:
        approaching = _approaching_entry(audit)
        if str(decision.reason or "") == "ACTIVE_TRADE_OPEN":
            # With the "чому ні" line gone this has to say so itself: an open position,
            # not a missing level, is why there is no new entry.
            lines = [
                f"<b>Тримаємо позицію</b> ({_esc(side_word(decision.side))}) — нового входу немає.",
            ]
        elif approaching:
            lines = [
                "🟡 <b>Вхід наближається: рівень сформовано.</b>",
                f"{_esc(_anchor_kind_label(approaching.get('kind')))} на {_fmt_price(approaching.get('level'))} "
                f"({_esc(side_word(approaching.get('side')))}) — ціна за "
                f"{safe_float(approaching.get('distance_atr')):.2f} ATR, чекаємо 3m-реакцію.",
            ]
        else:
            lines = ["<b>Входу зараз немає.</b>"]
            if decision.side and decision.side != Side.NEUTRAL.value:
                lines.append(
                    f"{_esc(side_word(decision.side))}-сценарій залишається, "
                    "але 3m-реакції біля рівня не відбулося."
                )
            else:
                lines.append("Немає рівня з підтвердженою 3m-реакцією — вхід не виконується.")

        lines.append(f"<b>Ціна зараз:</b> {_fmt_price(price)}")

        if decision.candidate:
            quality = safe_int(decision.candidate.entry_quality, safe_int(decision.candidate.final_score))
            if quality > 0:
                lines.append(f"<b>Якість:</b> {quality}/100")

        risk_line = _risk_budget_line(audit)
        if risk_line:
            lines.append(risk_line)
        for warning in list(context.get("learning_warnings") or [])[:2]:
            lines.append(f"⚠️ {_esc(warning)}")
        return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]

    candidate = decision.candidate
    lines = [
        f"<b>{ACTION_LABELS.get(decision.action, decision.action)}</b> | "
        f"{side_word(decision.side)} | {setup_label(decision.setup_type)}",
        f"<b>Ціна зараз:</b> {_fmt_price(price)}",
    ]
    if candidate:
        anchor_level = safe_float(candidate.execution_anchor, safe_float(candidate.trigger_level))
        if anchor_level > 0:
            lines.append(
                f"<b>Anchor:</b> {_esc(_anchor_kind_label(candidate.anchor_kind))} "
                f"{_fmt_price(anchor_level)} | {_esc(candidate.anchor_id[:12])}"
            )
        lines.append(f"<b>Реакція 3m:</b> {_esc(_reaction_summary(candidate))}")
        lines.append(
            f"<b>Скор:</b> {safe_int(candidate.final_score)}/100 | "
            f"<b>Якість входу:</b> {safe_int(candidate.entry_quality)}/100"
        )
    lines.append(regime_line)

    plan = decision.plan
    if plan and plan.valid and decision.action in EXECUTABLE_ENTRY_ACTIONS:
        lines.extend(_plan_lines(plan))

    if candidate:
        confirmations = [str(x).strip() for x in (candidate.confirmations or []) if str(x).strip()]
        if confirmations:
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            lines.append(f"✅ {_esc(confirmations[0])}")
        for risk in list(candidate.risks or [])[:2]:
            lines.append(f"⚠️ {_esc(str(risk)[:160])}")

    for warning in list(context.get("learning_warnings") or [])[:2]:
        lines.append(f"⚠️ {_esc(warning)}")
    return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]


# ==========================================================
# FOLLOW MESSAGE  (supervision side — unchanged)
# ==========================================================

def _bias_label(block: dict) -> str:
    value = str((block or {}).get("bias") or Side.NEUTRAL.value).upper()
    if value in {Side.LONG.value, Side.SHORT.value}:
        return value
    return Side.NEUTRAL.value


def _follow_reversal_risk(trade: ActiveTrade, result: dict, context: dict) -> tuple[str, int]:
    # The legacy weight table also carried a "flow" term; flow_snapshot was
    # hardwired to NEUTRAL ("UNIMPLEMENTED_REAL_FLOW_CALCULATION") because this
    # price source has no order-book feed, so the term could never fire.
    opposite = _opposite_side(trade.side)
    current_pct = float(result.get("current_pct") or 0)
    score = 10
    if current_pct < 0:
        score += 16
    if current_pct <= -0.50:
        score += 10
    for block, weight in ((context.get("tf3", {}), 18), (context.get("tf15", {}), 22), (context.get("cvd", {}), 8)):
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
    management_state = str(result.get("management_state") or getattr(trade, "management_state", "SUPPORTED"))
    if action == Action.PROTECT.value or management_state == "PROTECT":
        return f"🟠 СУПРОВІД {side} — PROTECT / ПРИБУТОК ЗАХИЩЕНО"
    if management_state == "WEAKENING":
        return f"🟠 СУПРОВІД {side} — WEAKENING / СЕТАП СЛАБШАЄ"
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
        f"Стан супроводу: <b>{html.escape(str(result.get('management_state') or getattr(trade, 'management_state', 'SUPPORTED')))}</b>",
        tp_status,
    ]

    if result.get("stop_changed"):
        lines.insert(
            1,
            f"🔔 <b>СТОП ЛОСС ЗМІНЕНО!</b> {_fmt_price(result.get('stop_before'))} → {_fmt_price(result.get('stop_after'))}",
        )

    return "\n".join(lines)[:TELEGRAM_MAX_LENGTH]
# ==========================================================
# ANALYTICS & LEARNING STATUS
# ==========================================================
# Everything here is derived from the closed-trade journal; nothing feeds back
# into an entry decision except compute_learning_status, whose warnings are
# advisory only. The one input that DOES gate execution is the setup
# degradation table, computed in the admission section.
#
# compute_entry_quality_audit exists because the previous architecture had no
# way to answer "are entries actually early?". It measures the three numbers
# that decide it: reaction latency, distance from the anchor at entry, and the
# share of trades whose MFE never reached 0.25R.

ANALYTICS_SCHEMA_VERSION = "organic_analytics_v10.0.0"
ENTRY_AUDIT_SCHEMA_VERSION = "organic_entry_quality_audit_v10.0.0"
CALENDAR_SCHEMA_VERSION = "organic_calendar_statistics_v10.0.0"
LEARNING_SCHEMA_VERSION = "organic_learning_status_v10.0.0"

MFE_EARLY_ENTRY_R = 0.25
LATENCY_EARLY_MINUTES = 10.0
RECENT_TRADE_WINDOW = 20


def _closed_trades(journal: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in list((journal or {}).get("trades") or []) if isinstance(t, dict)]


def _outcome_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trades whose R outcome is actually measurable."""
    rows = []
    for trade in trades:
        result_r = _trade_result_r(trade)
        if result_r is None:
            continue
        rows.append({"trade": trade, "r": result_r, "win": _trade_is_win(trade)})
    return rows


def _bucket_outcome(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "wilson_lower": 0.0,
                "net_r": 0.0, "expectancy_r": 0.0}
    wins = sum(1 for row in rows if row["win"] is True)
    net_r = sum(row["r"] for row in rows)
    return {
        "trades": n,
        "wins": wins,
        "losses": sum(1 for row in rows if row["win"] is False),
        "win_rate": round(wins / n, 4),
        "wilson_lower": round(wilson_lower_bound(wins, n, SETUP_WILSON_Z), 4),
        "net_r": round(net_r, 4),
        "expectancy_r": round(net_r / n, 4),
    }


def _group_by(rows: list[dict[str, Any]], key_fn: Any) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row["trade"]) or "UNKNOWN"), []).append(row)
    return {key: _bucket_outcome(group) for key, group in sorted(groups.items())}


def compute_analytics(journal: dict[str, Any]) -> dict[str, Any]:
    """Portfolio-level outcome picture plus the distributions behind it."""
    trades = _closed_trades(journal)
    rows = _outcome_rows(trades)
    overall = _bucket_outcome(rows)

    r_values = [row["r"] for row in rows]
    mfe_values = [safe_float(row["trade"].get("mfe_r"), 0.0) for row in rows]
    mae_values = [safe_float(row["trade"].get("mae_r"), 0.0) for row in rows]
    gross_win = sum(r for r in r_values if r > 0)
    gross_loss = abs(sum(r for r in r_values if r < 0))

    recent = rows[-RECENT_TRADE_WINDOW:]
    return {
        **overall,
        "breakeven": sum(1 for row in rows if row["win"] is None),
        "unmeasured": len(trades) - len(rows),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 1e-9 else None,
        "avg_win_r": round(gross_win / max(overall["wins"], 1), 4) if overall["wins"] else 0.0,
        "avg_loss_r": round(-gross_loss / max(overall["losses"], 1), 4) if overall["losses"] else 0.0,
        "median_r": round(percentile(r_values, 0.5), 4) if r_values else None,
        "best_r": round(max(r_values), 4) if r_values else None,
        "worst_r": round(min(r_values), 4) if r_values else None,
        "median_mfe_r": round(percentile(mfe_values, 0.5), 4) if mfe_values else None,
        "median_mae_r": round(percentile(mae_values, 0.5), 4) if mae_values else None,
        "share_mfe_below_025r": round(
            sum(1 for v in mfe_values if v < MFE_EARLY_ENTRY_R) / len(mfe_values), 4
        ) if mfe_values else None,
        "share_tp1_hit": round(
            sum(1 for row in rows if row["trade"].get("tp1_hit")) / len(rows), 4
        ) if rows else None,
        "recent": {
            "window": RECENT_TRADE_WINDOW,
            **_bucket_outcome(recent),
            "trend_r": round(sum(row["r"] for row in recent), 4),
        },
        "by_family": _group_by(rows, lambda t: t.get("canonical_setup_family") or canonical_setup_family(str(t.get("setup_type") or ""))),
        "by_setup": _group_by(rows, lambda t: str(t.get("setup_type") or "").upper()),
        "by_stage": _group_by(rows, lambda t: str(t.get("execution_stage") or "").upper()),
        "by_regime": _group_by(rows, lambda t: str(t.get("opened_regime") or t.get("regime") or "").upper()),
        "by_anchor_kind": _group_by(rows, lambda t: str(t.get("anchor_kind") or "").upper()),
        "by_close_action": _group_by(rows, lambda t: str(t.get("close_action") or "").upper()),
        "by_side": _group_by(rows, lambda t: str(t.get("side") or "").upper()),
        "computed_at": iso_now(),
        "schema_version": ANALYTICS_SCHEMA_VERSION,
    }


def compute_calendar_statistics(journal: dict[str, Any]) -> dict[str, Any]:
    """When the bot makes money: by UTC date, hour and weekday (Kyiv sessions)."""
    rows = _outcome_rows(_closed_trades(journal))
    by_date: dict[str, list[dict[str, Any]]] = {}
    by_hour: dict[str, list[dict[str, Any]]] = {}
    by_weekday: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        moment = _parse_time_any(row["trade"].get("time") or row["trade"].get("closed_at"))
        if moment is None:
            continue
        by_date.setdefault(moment.strftime("%Y-%m-%d"), []).append(row)
        by_hour.setdefault(f"{moment.hour:02d}", []).append(row)
        by_weekday.setdefault(moment.strftime("%A").upper(), []).append(row)

    daily = {day: {**_bucket_outcome(group), "date": day} for day, group in sorted(by_date.items())}
    net_series = [daily[day]["net_r"] for day in sorted(daily)]
    streak = 0
    best_streak = 0
    worst_streak = 0
    for value in net_series:
        streak = streak + 1 if value > 0 else (streak - 1 if value < 0 else 0)
        best_streak = max(best_streak, streak)
        worst_streak = min(worst_streak, streak)

    hourly = {hour: _bucket_outcome(group) for hour, group in sorted(by_hour.items())}
    ranked_hours = sorted(
        (hour for hour, row in hourly.items() if row["trades"] >= 3),
        key=lambda hour: hourly[hour]["expectancy_r"], reverse=True,
    )
    return {
        "days": len(daily),
        "by_date": daily,
        "by_hour": hourly,
        "by_weekday": {day: _bucket_outcome(group) for day, group in sorted(by_weekday.items())},
        "profitable_days": sum(1 for value in net_series if value > 0),
        "losing_days": sum(1 for value in net_series if value < 0),
        "best_day_net_r": round(max(net_series), 4) if net_series else None,
        "worst_day_net_r": round(min(net_series), 4) if net_series else None,
        "best_day": max(daily, key=lambda day: daily[day]["net_r"]) if daily else None,
        "worst_day": min(daily, key=lambda day: daily[day]["net_r"]) if daily else None,
        "best_day_streak": best_streak,
        "worst_day_streak": worst_streak,
        "best_hours": ranked_hours[:3],
        "worst_hours": ranked_hours[-3:] if len(ranked_hours) > 3 else [],
        "computed_at": iso_now(),
        "schema_version": CALENDAR_SCHEMA_VERSION,
    }


def compute_entry_quality_audit(journal: dict[str, Any]) -> dict[str, Any]:
    """Did the entry arrive early, tight to the level, with room to run?

    These are the failure modes of the previous architecture: 15-30 minute
    confirmation latency, entries up to 3.75 ATR away from the level, and
    18/30 trades whose MFE never reached 0.25R. Each metric below names one of
    them, so a regression shows up as a number rather than as a losing month.
    """
    rows = _outcome_rows(_closed_trades(journal))

    def collect(fn: Any) -> list[float]:
        values = [fn(row["trade"]) for row in rows]
        return [v for v in values if v is not None]

    latency = collect(lambda t: safe_float(t.get("reaction_latency_minutes")) or None)
    entry_distance = collect(lambda t: safe_float(t.get("entry_distance_atr")) or None)
    stop_distance = collect(lambda t: safe_float(t.get("stop_distance_atr")) or None)
    mfe = collect(lambda t: safe_float(t.get("mfe_r"), 0.0) if t.get("mfe_r") is not None else None)
    entry_scores = collect(lambda t: safe_float(t.get("entry_score")) or None)

    # Supervision names this exit in management_state; close_action only says EXIT.
    def _is_no_followthrough(trade: dict[str, Any]) -> bool:
        for key in ("management_state", "close_reason", "close_action"):
            if "NO_FOLLOWTHROUGH" in str(trade.get(key) or "").upper():
                return True
        return False

    no_followthrough = [row for row in rows if _is_no_followthrough(row["trade"])]

    # Does a higher entry score actually predict a better R? If not, the score
    # is decoration — which is exactly what the old trade_entry_quality was.
    score_buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        score = safe_int(row["trade"].get("entry_score"))
        if score <= 0:
            continue
        bucket = "LOW" if score < 60 else "MID" if score < 75 else "HIGH"
        score_buckets.setdefault(bucket, []).append(row)

    return {
        "measured_trades": len(rows),
        "median_reaction_latency_minutes": round(percentile(latency, 0.5), 2) if latency else None,
        "share_latency_under_10m": round(
            sum(1 for v in latency if v <= LATENCY_EARLY_MINUTES) / len(latency), 4
        ) if latency else None,
        "median_entry_distance_atr": round(percentile(entry_distance, 0.5), 3) if entry_distance else None,
        "max_entry_distance_atr": round(max(entry_distance), 3) if entry_distance else None,
        "median_stop_distance_atr": round(percentile(stop_distance, 0.5), 3) if stop_distance else None,
        "share_stop_within_cap": round(
            sum(1 for v in stop_distance if v <= MAX_STOP_ATR) / len(stop_distance), 4
        ) if stop_distance else None,
        "median_mfe_r": round(percentile(mfe, 0.5), 4) if mfe else None,
        "share_mfe_below_025r": round(
            sum(1 for v in mfe if v < MFE_EARLY_ENTRY_R) / len(mfe), 4
        ) if mfe else None,
        "share_mfe_ge_1r": round(sum(1 for v in mfe if v >= 1.0) / len(mfe), 4) if mfe else None,
        "no_followthrough_exit_share": round(len(no_followthrough) / len(rows), 4) if rows else None,
        "no_followthrough_expectancy_r": round(
            sum(row["r"] for row in no_followthrough) / len(no_followthrough), 4
        ) if no_followthrough else None,
        "expectancy_by_entry_score": {
            bucket: _bucket_outcome(group) for bucket, group in sorted(score_buckets.items())
        },
        "entry_score_is_discriminative": bool(
            len(score_buckets) >= 2
            and max(_bucket_outcome(g)["expectancy_r"] for g in score_buckets.values())
            - min(_bucket_outcome(g)["expectancy_r"] for g in score_buckets.values()) > 0.15
        ),
        "computed_at": iso_now(),
        "schema_version": ENTRY_AUDIT_SCHEMA_VERSION,
    }


def compute_learning_status(journal: dict[str, Any]) -> dict[str, Any]:
    """What the bot currently knows, and how much of that knowledge is usable."""
    statistics = dict(journal.get("setup_statistics") or {}) or compute_setup_statistics(journal)
    by_setup = dict(statistics.get("by_setup") or {})
    measured = safe_int(statistics.get("measured_trades"))
    min_sample = max(1, safe_int(statistics.get("min_sample"), SETUP_STATS_MIN_SAMPLE))

    demoted, promoted, executable = [], [], 0
    for setup_type in DETECTED_SETUP_TYPES:
        status = setup_degradation_status(setup_type, statistics)
        if status.get("status") == "DEMOTED":
            demoted.append(setup_type)
        elif status.get("status") == "PROMOTED":
            promoted.append(setup_type)
        if status.get("executable"):
            executable += 1

    sufficient = sum(1 for row in by_setup.values() if safe_int(row.get("trades")) >= min_sample)
    if not measured:
        mode, label = "BOOTSTRAP", "НЕМАЄ ІСТОРІЇ — усі сетапи виконувані за замовчуванням"
    elif sufficient == 0:
        mode, label = "ACCUMULATING", f"НАКОПИЧЕННЯ ІСТОРІЇ — {measured} угод, жоден сетап ще не досяг {min_sample}"
    elif not demoted:
        mode, label = "CALIBRATED", f"КАЛІБРОВАНО — {sufficient} сетапів з достатньою вибіркою, жодної деградації"
    else:
        mode, label = "DEGRADATION_ACTIVE", f"АВТО-ДЕГРАДАЦІЯ АКТИВНА — {len(demoted)} сетап(ів) заблоковано за від'ємним expectancy"

    if executable == 0:
        mode, label = "ALL_SETUPS_DEMOTED", "УСІ СЕТАПИ ДЕГРАДОВАНО — виконання призупинено до нової вибірки"

    return {
        "mode": mode,
        "label": label,
        "measured_trades": measured,
        "closed_trades": safe_int(statistics.get("closed_trades")),
        "min_sample": min_sample,
        "setups_with_sufficient_sample": sufficient,
        "total_setups": len(DETECTED_SETUP_TYPES),
        "executable_setups": executable,
        "demoted_setups": demoted,
        "promoted_setups": promoted,
        "execution_suspended": bool(executable == 0),
        "computed_at": iso_now(),
        "schema_version": LEARNING_SCHEMA_VERSION,
    }


def learning_health_warnings(learning_status: dict[str, Any], entry_audit: dict[str, Any]) -> list[str]:
    """Two or three sentences the user should see, not a diagnostics dump."""
    status = dict(learning_status or {})
    audit = dict(entry_audit or {})
    warnings: list[str] = []

    if status.get("execution_suspended"):
        warnings.append("Усі сетапи деградовано: виконання призупинено, журнали продовжують збирати вибірку.")
    elif status.get("mode") == "ACCUMULATING":
        warnings.append(
            f"Історії замало для авто-деградації ({status.get('measured_trades')} угод) — "
            f"рішення ухвалюються на структурі, не на статистиці."
        )
    if status.get("demoted_setups"):
        warnings.append(f"Заблоковано за від'ємним expectancy: {', '.join(list(status['demoted_setups'])[:4])}.")

    if audit.get("measured_trades"):
        share = audit.get("share_mfe_below_025r")
        if share is not None and share > 0.45:
            warnings.append(
                f"{share:.0%} угод не дійшли навіть до 0.25R MFE — вхід запізній або ринок без продовження."
            )
        latency = audit.get("median_reaction_latency_minutes")
        if latency is not None and latency > LATENCY_EARLY_MINUTES:
            warnings.append(f"Медіанна латентність реакції {latency:.0f} хв — вище цільових {LATENCY_EARLY_MINUTES:.0f} хв.")
        if audit.get("entry_score_is_discriminative") is False and safe_int(audit.get("measured_trades")) >= SETUP_STATS_MIN_SAMPLE:
            warnings.append("Оцінка якості входу не розрізнює результати — ваги потребують перегляду.")
    return warnings[:3]


# ==========================================================
# SIGNAL RECORD  (journal rows + dashboard payload)
# ==========================================================

def build_signal_record(
    context: dict[str, Any],
    decision: Decision,
    plan: Optional[TradePlan],
    audit: dict[str, Any],
) -> dict[str, Any]:
    """One decision, serialized once and reused for journal, training and UI.

    index.html is shipped unchanged, so this row keeps the field names the
    dashboard reads: action, type, entry_level, total_score, regime_type, plan
    and context. Everything else is v10 evidence.
    """
    candidate = decision.candidate
    reaction = dict((candidate.reaction if candidate else {}) or {})
    gates = dict(reaction.get("GATE_PROXIMITY") or {})
    stage_plan = dict((plan.stage_plan if plan else {}) or {})
    geometry = dict(stage_plan.get("geometry") or {})
    runway = dict(stage_plan.get("runway_target_management") or {})
    conviction = dict(stage_plan.get("probe_conviction") or {})
    budget = dict(stage_plan.get("daily_risk_budget") or {})
    nearest = dict((audit.get("anchor_watch") or {}).get("nearest") or {})

    record: dict[str, Any] = {
        "id": decision.id,
        "time": decision.time,
        "action": decision.action,
        "type": decision.action,
        "side": decision.side,
        "setup_type": decision.setup_type,
        "setup_family": journal_setup_family(decision.setup_type),
        "canonical_setup_family": canonical_setup_family(decision.setup_type),
        "reason": decision.reason,
        "score": safe_int(decision.quality),
        "total_score": safe_int(decision.quality),
        "quality": safe_int(decision.quality),
        "entry_quality": safe_int(getattr(candidate, "entry_quality", 0)) if candidate else 0,
        "setup_quality": safe_int(getattr(candidate, "setup_quality_score", 0)) if candidate else 0,
        "timing_quality": safe_int(getattr(candidate, "timing_quality_score", 0)) if candidate else 0,
        "trade_quality": safe_int(getattr(candidate, "trade_quality_score", 0)) if candidate else 0,
        "regime": decision.regime,
        "regime_type": decision.regime,
        "session": context.get("session_name"),
        "price": safe_float(decision.current_price, safe_float(context.get("price"))),
        "entry_level": round_price(plan.entry) if plan else 0.0,
        "atr15": round(safe_float(context.get("atr15")), 6),
        "spread_atr": round(safe_float(context.get("spread_atr")), 4),
        "htf_state": str((getattr(candidate, "htf_fact", {}) or {}).get("state") or "") if candidate else "",
        "anchor_id": str(getattr(candidate, "anchor_id", "")) if candidate else "",
        "anchor_kind": str(getattr(candidate, "anchor_kind", "")) if candidate else "",
        "anchor_level": round_price(getattr(candidate, "execution_anchor", 0.0)) if candidate else 0.0,
        "anchor_state": str(nearest.get("state") or ""),
        "entry_distance_atr": safe_float(gates.get("distance_atr")),
        "reaction_latency_minutes": safe_float(reaction.get("latency_minutes")),
        "reaction_gates": reaction,
        "runway_r": runway.get("runway_r"),
        "stop_distance_atr": round(
            safe_float(geometry.get("decision_distance")) / safe_float(geometry.get("atr15")), 4
        ) if safe_float(geometry.get("atr15")) > 0 else None,
        "entry_stage": str(getattr(candidate, "entry_stage", "")) if candidate else "",
        "probe_conviction": conviction.get("tier"),
        "risk_pct": round(safe_float(plan.position_risk_pct) if plan else 0.0, 6),
        "daily_risk": {
            "used": budget.get("daily_risk_used"),
            "granted": budget.get("granted_risk_pct"),
            "left": budget.get("risk_budget_left"),
        } if budget else {},
        "degradation_status": str(dict(getattr(candidate, "admission", {}) or {}).get("status") or "") if candidate else "",
        "score_components": dict(getattr(candidate, "score_components", {}) or {}) if candidate else {},
        "competing_hypotheses": list(getattr(candidate, "competing_hypotheses", []) or [])[:JOURNAL_HYPOTHESIS_TOP] if candidate else [],
        "hypothesis_rank": safe_int(getattr(candidate, "hypothesis_rank", 0)) if candidate else 0,
        "executed": bool(plan and plan.valid and plan.execution_ready and decision.action in EXECUTABLE_ENTRY_ACTIONS),
        "preconfirmation_event_id": str(audit.get("preconfirmation_event_id") or ""),
        "bot_version": BOT_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    if plan and plan.valid:
        record["plan"] = {
            "entry": round_price(plan.entry),
            "stop": round_price(plan.stop),
            "tp0": round_price(plan.tp0),
            "tp1": round_price(plan.tp1),
            "tp2": round_price(plan.tp2),
            "tp3": round_price(plan.tp3),
            "rr0": plan.rr0,
            "rr1": plan.rr1,
            "rr2": plan.rr2,
            "rr3": plan.rr3,
            "risk_pct": round(safe_float(plan.position_risk_pct), 6),
            "entry_stage": plan.entry_stage,
            "execution_source": plan.execution_source,
            "stop_basis": plan.stop_basis,
        }

    # index.html is shipped unchanged and resolves these paths itself.
    executable = bool(record["executed"])
    regime_profile = dict(context.get("regime_profile") or {})
    regime_engine = {
        "regime_type": decision.regime,
        "label": regime_label(decision.regime),
        "reason": " | ".join(part for part in (
            f"bias={regime_profile.get('bias')}" if regime_profile.get("bias") else "",
            f"ER15={regime_profile.get('efficiency_15m')}" if regime_profile.get("efficiency_15m") is not None else "",
            f"ATR expansion={regime_profile.get('atr_expansion')}" if regime_profile.get("atr_expansion") is not None else "",
        ) if part),
        "entry_action": "ALLOW_ENTRY" if executable else "WAIT_FOR_REACTION",
    }
    sweep = dict(context.get("sweep") or {})
    record["regime_engine"] = regime_engine
    record["confirmations"] = list(getattr(candidate, "confirmations", []) or [])[:8] if candidate else []
    record["conflicts"] = list(getattr(candidate, "risks", []) or [])[:4] if candidate else []
    record["context"] = {
        "total_score": record["total_score"],
        "market_regime": regime_engine,
        "session": {"name": str(context.get("session_name") or "")},
        "liquidity": {
            "event": str(sweep.get("kind") or ""),
            "bias": str(sweep.get("side") or ""),
            "state": "SWEEP" if sweep.get("sweep") else "QUIET",
        },
        "price": record["price"],
        "atr15": record["atr15"],
    }
    return record
# ==========================================================
# PRECONFIRMATION LIFECYCLE
# ==========================================================
# The unchanged supervision layer asks this layer exactly one question: "is the
# event linked to this PROBE CONFIRMED?" If it is not, the probe keeps the fast
# no-followthrough exit. The legacy answered that question with a logistic fit,
# Platt calibration, hierarchical priors, AUC/Brier tracking and an authority
# gate — and then resolved the label from price anyway. Only the resolution is
# load-bearing, so only the resolution stays: a fixed window of confirmed 3m
# candles, first of acceptance vs invalidation, no terminal evidence = EXPIRED.

_PRECONFIRM_KIND_BY_ANCHOR = {
    AnchorKind.SWEEP_LOW.value: "LIQUIDITY_SWEEP_REVERSAL",
    AnchorKind.SWEEP_HIGH.value: "LIQUIDITY_SWEEP_REVERSAL",
    AnchorKind.FAIR_VALUE_GAP.value: "FVG_REACTION",
    AnchorKind.DEMAND_ZONE.value: "OB_REACTION",
    AnchorKind.SUPPLY_ZONE.value: "OB_REACTION",
    AnchorKind.BREAK_LEVEL.value: "STRUCTURE_CONFIRMATION",
    AnchorKind.STRUCTURE_SHIFT.value: "STRUCTURE_CONFIRMATION",
}


def _preconfirm_event_kind(candidate: Candidate) -> str:
    """In v10 the anchor kind IS the event kind — no string sniffing needed."""
    return _PRECONFIRM_KIND_BY_ANCHOR.get(
        str(getattr(candidate, "anchor_kind", "") or "").upper(), "DIRECTIONAL_ACCEPTANCE",
    )


def _preconfirm_as_of_ts(context: dict[str, Any]) -> int:
    candles = (context.get("candles") or {}).get("3m", []) or []
    confirmed = [int(getattr(c, "ts", 0) or 0) for c in candles if getattr(c, "confirmed", True)]
    return max(confirmed, default=int(now_utc().timestamp() * 1000))


def _preconfirm_candles(context: dict[str, Any], timeframe: str, as_of_ts: int) -> list[Candle]:
    candles = (context.get("candles") or {}).get(timeframe, []) or []
    return sorted(
        [c for c in candles if getattr(c, "confirmed", True) and int(getattr(c, "ts", 0) or 0) <= int(as_of_ts)],
        key=lambda c: int(getattr(c, "ts", 0) or 0),
    )


def _preconfirm_legacy_outcome(status: str) -> str:
    """Keep v9.4 readers alive while status is the source of truth."""
    canonical = str(status or "PENDING").upper()
    return "INVALIDATED" if canonical == "FAILED" else canonical


def _preconfirm_set_status(
    event: dict[str, Any],
    status: str,
    *,
    reason: str = "",
    resolved_ts: Optional[int] = None,
    outcome_ts: Optional[int] = None,
    resolved_price: Optional[float] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> None:
    canonical = str(status or "PENDING").upper()
    if canonical not in PRECONFIRM_VALID_STATUSES:
        raise ValueError(f"Unsupported preconfirmation status: {canonical}")
    event["status"] = canonical
    event["outcome"] = _preconfirm_legacy_outcome(canonical)
    if reason:
        event["resolution_reason"] = reason
    if canonical != "PENDING":
        event["resolved_ts"] = int(resolved_ts or int(now_utc().timestamp() * 1000))
        event["resolved_at"] = iso_now()
        if outcome_ts is not None:
            event["outcome_ts"] = int(outcome_ts)
        if resolved_price is not None:
            event["resolved_price"] = round_price(resolved_price)
        if evidence is not None:
            event["resolution_evidence"] = dict(evidence)


def make_preconfirmation_event(context: dict[str, Any], candidate: Candidate, signal_id: str) -> dict[str, Any]:
    """Freeze the entry thesis at the moment of entry, so it can be judged later."""
    observation_candle_ts = _preconfirm_as_of_ts(context)
    # OKX candle timestamps are bar-open times: the thesis exists only once the
    # latest confirmed 3m candle has closed, so the lifecycle starts there.
    observed_ts = observation_candle_ts + PRECONFIRM_RESOLUTION_BAR_MS
    created_at = iso_now()
    price = safe_float(context.get("price"), 0.0)
    confirmed_3m = _preconfirm_candles(context, "3m", observed_ts)
    if confirmed_3m:
        price = safe_float(confirmed_3m[-1].close, price)
    atr15 = max(safe_float(context.get("atr15"), 0.0), price * 0.001, 1e-6)

    side = str(candidate.side or "").upper()
    sign = side_sign(side)
    anchor_level = safe_float(candidate.execution_anchor, safe_float(candidate.trigger_level, price)) or price
    confirmation_level = (
        max(anchor_level, price + atr15 * PRECONFIRM_CONFIRM_BUFFER_ATR) if sign > 0
        else min(anchor_level, price - atr15 * PRECONFIRM_CONFIRM_BUFFER_ATR)
    )
    invalidation = safe_float(candidate.invalidation_level, 0.0)
    if sign > 0 and (invalidation <= 0 or invalidation >= price):
        invalidation = price - atr15 * PRECONFIRM_INVALIDATION_BUFFER_ATR
    if sign < 0 and (invalidation <= 0 or invalidation <= price):
        invalidation = price + atr15 * PRECONFIRM_INVALIDATION_BUFFER_ATR

    event_id = uuid.uuid4().hex[:16]
    resolve_after_ts = observed_ts + PRECONFIRM_WINDOW_MINUTES * 60_000
    event_kind = _preconfirm_event_kind(candidate)
    setup_family = str(candidate.setup_family or "UNKNOWN").upper()
    return {
        "event_id": event_id,
        "id": event_id,
        "thesis_key": str(candidate.thesis_key or f"{side}|{candidate.setup_type}|{anchor_level}"),
        "signal_id": str(signal_id or ""),
        "signal_link": str(signal_id or ""),
        "side": side,
        "setup_type": str(candidate.setup_type or ""),
        "setup_family": setup_family,
        "canonical_setup_family": str(candidate.canonical_setup_family or ""),
        "event_kind": event_kind,
        "model_family": f"{setup_family}:{event_kind}",
        "anchor_id": str(candidate.anchor_id or ""),
        "anchor_kind": str(candidate.anchor_kind or ""),
        "entry_stage": str(candidate.entry_stage or ""),
        "created_at": created_at,
        "created_ts": observed_ts,
        "observed_at": created_at,
        "observed_ts": observed_ts,
        "observation_candle_ts": observation_candle_ts,
        "resolve_after_ts": resolve_after_ts,
        "expires_ts": resolve_after_ts,
        "resolution_window_minutes": PRECONFIRM_WINDOW_MINUTES,
        "acceptance_required_closes": PRECONFIRM_ACCEPTANCE_CLOSES,
        "observed_price": round_price(price),
        "atr15_at_observation": round(atr15, 8),
        "anchor": round_price(anchor_level),
        "confirmation_level": round_price(confirmation_level),
        "invalidation_level": round_price(invalidation),
        "status": "PENDING",
        "outcome": "PENDING",
        "outcome_contract": (
            f"FIXED_{PRECONFIRM_WINDOW_MINUTES}M_WINDOW; "
            "FIRST_ACCEPTANCE_VS_INVALIDATION; NO_TERMINAL_EVIDENCE=EXPIRED"
        ),
        "bot_version_at_observation": BOT_VERSION,
        "architecture_version_at_observation": ARCHITECTURE_VERSION,
        "schema_version": PRECONFIRM_EVENT_SCHEMA_VERSION,
    }


def resolve_preconfirmation_events(journal: dict[str, Any], context: dict[str, Any]) -> int:
    """Label PENDING events from the confirmed 3m candles of their own window.

    The realized trade result is deliberately irrelevant here: the question is
    whether the level did what the thesis said, not whether the exit was good.
    """
    as_of_candle_ts = _preconfirm_as_of_ts(context)
    data_available_through_ts = as_of_candle_ts + PRECONFIRM_RESOLUTION_BAR_MS
    all_candles = _preconfirm_candles(context, "3m", as_of_candle_ts)
    resolved_count = 0

    for event in list(journal.get("preconfirmation_events") or []):
        if not isinstance(event, dict) or _preconfirm_event_status(event) != "PENDING":
            continue
        observed_ts = safe_int(event.get("observed_ts") or event.get("created_ts"))
        resolve_after_ts = safe_int(event.get("resolve_after_ts") or event.get("expires_ts"))
        if observed_ts <= 0:
            _preconfirm_set_status(
                event, "FAILED", reason="MISSING_OBSERVED_TS",
                resolved_ts=data_available_through_ts, outcome_ts=data_available_through_ts,
                evidence={"data_error": True},
            )
            resolved_count += 1
            continue
        if resolve_after_ts <= observed_ts:
            resolve_after_ts = observed_ts + PRECONFIRM_WINDOW_MINUTES * 60_000
            event["resolve_after_ts"] = resolve_after_ts
            event["expires_ts"] = resolve_after_ts
        if data_available_through_ts < resolve_after_ts:
            continue

        candles = [
            candle for candle in all_candles
            if observed_ts <= int(getattr(candle, "ts", 0) or 0)
            and int(getattr(candle, "ts", 0) or 0) + PRECONFIRM_RESOLUTION_BAR_MS <= resolve_after_ts
        ]
        if not candles:
            _preconfirm_set_status(
                event, "EXPIRED", reason="RESOLUTION_WINDOW_ELAPSED_WITHOUT_CANDLES",
                resolved_ts=data_available_through_ts, outcome_ts=resolve_after_ts,
                evidence={"directional_close": False, "acceptance": False,
                          "price_moved_direction": False, "thesis_invalidated": False},
            )
            resolved_count += 1
            continue

        side = str(event.get("side") or "").upper()
        sign = side_sign(side)
        observed_price = safe_float(event.get("observed_price"), safe_float(candles[0].open, 0.0))
        atr15 = max(safe_float(event.get("atr15_at_observation"), 0.0), observed_price * 0.001, 1e-6)
        confirm_level = safe_float(
            event.get("confirmation_level"), observed_price + sign * atr15 * PRECONFIRM_CONFIRM_BUFFER_ATR,
        )
        invalidation_level = safe_float(
            event.get("invalidation_level"), observed_price - sign * atr15 * PRECONFIRM_INVALIDATION_BUFFER_ATR,
        )
        required_closes = max(1, safe_int(event.get("acceptance_required_closes"), PRECONFIRM_ACCEPTANCE_CLOSES))

        directional_close_ts: Optional[int] = None
        acceptance_ts: Optional[int] = None
        invalidation_ts: Optional[int] = None
        acceptance_streak = 0
        max_favorable_price = observed_price
        final_close = safe_float(candles[-1].close, observed_price)

        for candle in candles:
            candle_ts = int(getattr(candle, "ts", 0) or 0)
            close = safe_float(candle.close)
            low = safe_float(candle.low)
            high = safe_float(candle.high)
            directional_close = close >= confirm_level if sign > 0 else close <= confirm_level
            invalidated = low <= invalidation_level if sign > 0 else high >= invalidation_level
            if directional_close:
                directional_close_ts = directional_close_ts or candle_ts
                acceptance_streak += 1
                if acceptance_streak >= required_closes and acceptance_ts is None:
                    acceptance_ts = candle_ts
            else:
                acceptance_streak = 0
            if invalidated and invalidation_ts is None:
                invalidation_ts = candle_ts
            max_favorable_price = max(max_favorable_price, high) if sign > 0 else min(max_favorable_price, low)

        mfe_atr = sign * (max_favorable_price - observed_price) / atr15
        final_move_atr = sign * (final_close - observed_price) / atr15
        price_moved_direction = bool(mfe_atr >= PRECONFIRM_CONFIRM_BUFFER_ATR)
        directional_close_seen = directional_close_ts is not None
        acceptance_seen = acceptance_ts is not None
        thesis_invalidated = invalidation_ts is not None
        evidence = {
            "directional_close": directional_close_seen,
            "directional_close_ts": directional_close_ts,
            "acceptance": acceptance_seen,
            "acceptance_ts": acceptance_ts,
            "acceptance_required_closes": required_closes,
            "price_moved_direction": price_moved_direction,
            "mfe_atr": round(mfe_atr, 6),
            "final_move_atr": round(final_move_atr, 6),
            "thesis_invalidated": thesis_invalidated,
            "invalidation_ts": invalidation_ts,
            "window_candles": len(candles),
            "window_end_ts": resolve_after_ts,
        }

        if thesis_invalidated and (acceptance_ts is None or int(invalidation_ts or 0) <= int(acceptance_ts or 0)):
            status, reason = "FAILED", "THESIS_INVALIDATED_BEFORE_DIRECTIONAL_ACCEPTANCE"
            outcome_ts = invalidation_ts
        elif acceptance_seen and directional_close_seen and price_moved_direction:
            status, reason = "CONFIRMED", "DIRECTIONAL_CLOSE_AND_ACCEPTANCE_COMPLETED_WITHIN_FIXED_WINDOW"
            outcome_ts = acceptance_ts
            if invalidation_ts is not None and acceptance_ts is not None and invalidation_ts > acceptance_ts:
                evidence["post_confirmation_invalidation"] = True
        elif thesis_invalidated:
            status, reason = "FAILED", "THESIS_INVALIDATED_WITHOUT_VALID_ACCEPTANCE"
            outcome_ts = invalidation_ts
        else:
            status, reason = "EXPIRED", "FIXED_WINDOW_ELAPSED_WITHOUT_COMPLETE_DIRECTIONAL_ACCEPTANCE"
            outcome_ts = resolve_after_ts

        _preconfirm_set_status(
            event, status, reason=reason, resolved_ts=data_available_through_ts,
            outcome_ts=outcome_ts, resolved_price=final_close, evidence=evidence,
        )
        resolved_count += 1
    return resolved_count


def link_preconfirmation_event_to_trade(
    journal: dict[str, Any],
    active: Optional[ActiveTrade],
    result_class: str,
    close_action: str,
) -> str:
    """Close the event -> signal -> trade lineage once the trade is realized."""
    if active is None:
        return ""
    event_id = str(getattr(active, "preconfirmation_event_id", "") or "").strip()
    if not event_id:
        return ""
    for event in reversed(list(journal.get("preconfirmation_events") or [])):
        if not isinstance(event, dict):
            continue
        if str(event.get("event_id") or event.get("id") or "").strip() != event_id:
            continue
        event["trade_link"] = str(getattr(active, "id", "") or "")
        event["trade_id"] = str(getattr(active, "id", "") or "")
        event["trade_result_class"] = str(result_class or "")
        event["trade_close_action"] = str(close_action or "")
        event["trade_linked_at"] = iso_now()
        return event_id
    return ""


# ==========================================================
# SUPERVISION -> JOURNAL
# ==========================================================

def _management_event(
    active: ActiveTrade,
    res: dict[str, Any],
    context: dict[str, Any],
    price: float,
    *,
    kind: str,
    stop_before: float,
) -> dict[str, Any]:
    """One supervision step, journaled so a stop move stays explainable afterwards.

    The field names are the legacy ones on purpose: signal_events already holds
    hundreds of inherited rows of exactly this shape, and supervision is the part of
    the bot that stays unchanged. stop_before is a parameter because
    manage_active_trade mutates trade.stop_current in place.
    """
    event: dict[str, Any] = {
        "time": iso_now(),
        "type": kind,
        "action": res.get("action"),
        "side": str(active.side or ""),
        "price": round_price(price),
        "trade_id": str(active.id or ""),
        "signal_id": str(active.signal_id or ""),
        "management_state": res.get("management_state"),
        "stop_before": round_price(stop_before),
        "stop_after": round_price(active.stop_current),
        "profit_protection": res.get("profit_protection") or {},
        "ratchet_evidence": [
            dict(item) for item in (getattr(active, "protection_ratchet_evidence", []) or [])
            if isinstance(item, dict)
        ],
        "management_evidence_schema_version": str(
            getattr(active, "management_evidence_schema_version", "") or ""
        ),
    }
    for key in ("probe_no_followthrough", "path_decay_defense", "acceptance_no_followthrough"):
        if isinstance(res.get(key), dict):
            event[key] = res[key]
    opposite = context.get("fresh_opposite_execution")
    if isinstance(opposite, dict) and opposite:
        event["fresh_opposite_execution"] = opposite
    return event


def _closed_trade_row(active: ActiveTrade, res: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """One closed trade: outcome, path, and the anchor facts that produced it.

    Written rich, stored lean — save_journal compacts every row through
    compact_trade_for_journal, so this is the only place the schema is defined.
    The anchor facts are not ActiveTrade fields; they live in stage_plan, which
    is where the entry put them.
    """
    risk = max(abs(float(active.entry) - float(active.stop_initial)), 1e-9)
    mfe_r = abs(float(active.best_price) - float(active.entry)) / risk
    mae_r = abs(float(active.worst_price) - float(active.entry)) / risk
    result_r = res.get("result_r")

    stage_plan = dict(getattr(active, "stage_plan", {}) or {})
    anchor = dict(stage_plan.get("anchor") or {})
    gates = dict(stage_plan.get("reaction") or {})
    geometry = dict(stage_plan.get("geometry") or {})
    atr15 = max(safe_float(geometry.get("atr15"), safe_float(context.get("atr15"))), 1e-9)
    proximity_atr = safe_float((gates.get("GATE_PROXIMITY") or {}).get("distance_atr"))

    row = asdict(active)
    row.update({
        "closed_at": iso_now(),
        "age_minutes": round(_active_trade_age_minutes(active), 2),

        "result_r": result_r,
        "pnl_r": result_r,
        "realized_return_pct": res.get("realized_return_pct"),
        "result_unit": res.get("result_unit", "R"),
        "outcome_status": str(res.get("outcome_status") or "RESOLVED"),
        "ml_eligible": bool(res.get("ml_eligible", result_r is not None)),
        "realized_legs": list(res.get("realized_legs") or []),
        "known_realized_legs": list(res.get("known_realized_legs") or []),
        "known_realized_r": res.get("known_realized_r"),
        "known_realized_return_pct": res.get("known_realized_return_pct"),
        "remaining_size_pct": res.get("remaining_size_pct"),
        "result_r_min": res.get("result_r_min"),
        "result_r_max": res.get("result_r_max"),
        "realized_return_pct_min": res.get("realized_return_pct_min"),
        "realized_return_pct_max": res.get("realized_return_pct_max"),

        "close_action": str(res.get("action") or ""),
        "close_reason": str(res.get("recommended_stop_reason") or res.get("action") or ""),

        "mfe_pct": res.get("best_pct"),
        "mae_pct": res.get("worst_pct"),
        "mfe_giveback_pct": res.get("giveback_pct"),
        "mfe_giveback_ratio": res.get("mfe_giveback_ratio"),
        "mfe_r": round(mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "mfe_capture_ratio": (
            round(safe_float(result_r) / mfe_r, 4)
            if result_r is not None and mfe_r > 1e-9 else None
        ),
        "tp0_protect_threshold": (
            PROBE_TP0_PROTECT_GIVEBACK_RATIO
            if str(getattr(active, "entry_stage", "") or "").upper() == EntryStage.PROBE.value
            else TP0_PROTECT_GIVEBACK_RATIO
        ),

        "rr0": round(abs(float(active.tp0) - float(active.entry)) / risk, 4) if active.tp0 else None,
        "rr1": round(abs(float(active.tp1) - float(active.entry)) / risk, 4),
        "rr2": round(abs(float(active.tp2) - float(active.entry)) / risk, 4),
        "rr3": round(abs(float(active.tp3) - float(active.entry)) / risk, 4),

        "execution_stage": str(getattr(active, "entry_stage", "") or ""),
        "anchor_id": str(anchor.get("id") or getattr(active, "anchor_id", "") or ""),
        "anchor_kind": str(anchor.get("kind") or ""),
        "anchor_level": anchor.get("level"),
        "reaction_latency_minutes": stage_plan.get("reaction_latency_minutes"),
        "entry_distance_atr": round(proximity_atr, 4) if proximity_atr > 0 else None,
        "stop_distance_atr": round(safe_float(geometry.get("decision_distance")) / atr15, 4),
        "execution_intelligence": stage_plan.get("execution_intelligence_v9532"),
    })
    return row


# ==========================================================
# RUN
# ==========================================================

def append_history(state: dict[str, Any], row: dict[str, Any]) -> None:
    history = list(state.get("history") or [])
    history.append({"ts": iso_now(), **row})
    state["history"] = history[-MAX_HISTORY:]


def _entry_action_for_stage(stage: str) -> str:
    stage = str(stage or "").upper()
    if stage == EntryStage.CORE.value:
        return Action.ENTRY.value
    if stage == EntryStage.ACCEPTANCE.value:
        return Action.RISKY_ENTRY.value
    return Action.PROBE_ENTRY.value


def _anchor_watch(anchors: list[Anchor], context: dict[str, Any]) -> dict[str, Any]:
    """What the no-entry message reports: how many reasons exist, and the nearest."""
    price = safe_float(context.get("price"))
    atr15 = max(safe_float(context.get("atr15"), 0.0), 1e-9)
    now_ms = int(now_utc().timestamp() * 1000)
    armed = [a for a in anchors if a.state == AnchorState.ARMED.value]
    if not armed:
        return {"count": 0, "nearest": {}}
    nearest = min(armed, key=lambda a: abs(price - a.level))
    return {
        "count": len(armed),
        "nearest": {
            "id": nearest.id,
            "kind": nearest.kind,
            "level": round_price(nearest.level),
            "side": nearest.side,
            "setup_type": nearest.setup_type,
            "state": nearest.state,
            "score": safe_int(nearest.score),
            "age_minutes": round((now_ms - safe_int(nearest.created_ts)) / 60000.0, 1),
            "distance_atr": round(abs(price - nearest.level) / atr15, 4),
        },
    }


def _rejected_hypotheses(refusals: list[tuple[Anchor, Reaction]]) -> list[dict[str, Any]]:
    """Every refused anchor, best first, each with the single gate that stopped it."""
    rows: list[dict[str, Any]] = []
    for anchor, reaction in refusals:
        gate, _, reason = str(reaction.reason or "").partition(": ")
        rows.append({
            "anchor_id": anchor.id,
            "anchor_kind": anchor.kind,
            "side": anchor.side,
            "setup_type": anchor.setup_type,
            "setup_family": anchor.setup_family,
            "level": round_price(anchor.level),
            "score": safe_int(anchor.score),
            "final_score": safe_int(anchor.score),
            "failed_gate": gate,
            "reason": reason or str(reaction.reason or ""),
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:REJECTED_HYPOTHESIS_SHADOW_LIMIT]


def _select_candidate(
    candidates: list[Candidate], context: dict[str, Any],
) -> tuple[Optional[Candidate], str]:
    """Admission over the ranked list: score floor, HTF floor, one entry per run."""
    htf = dict(context.get("htf_fact") or {})
    blocked: list[str] = []
    for candidate in candidates:
        score = safe_float(candidate.final_score)
        if score < MIN_SCORE_PROBE:
            blocked.append(f"SCORE_{score:.0f}_BELOW_{MIN_SCORE_PROBE}")
            continue
        alignment = htf_alignment_for_side(htf, candidate.side)
        alignment_score = safe_float(alignment.get("score"))
        if alignment_score < MIN_HTF_ALIGNMENT_SCORE:
            blocked.append(f"HTF_{alignment.get('state')}_{alignment_score:.0f}_BELOW_{MIN_HTF_ALIGNMENT_SCORE}")
            continue
        return candidate, ""
    return None, "; ".join(blocked[:2]) if blocked else "NO_ANCHOR_WITH_CONFIRMED_3M_REACTION"


def _make_decision(
    context: dict[str, Any],
    journal: dict[str, Any],
    state: dict[str, Any],
    candidate: Optional[Candidate],
    rejection_reason: str,
    audit: dict[str, Any],
) -> tuple[Decision, Optional[TradePlan]]:
    """Turn the selected candidate into a Decision plus, when tradable, a plan."""
    price = safe_float(context.get("price"))
    regime = str(context.get("regime") or "")
    now = iso_now()

    if candidate is None:
        nearest = dict((audit.get("anchor_watch") or {}).get("nearest") or {})
        return Decision(
            id=new_id("sig"), time=now, action=Action.NO_SETUP.value,
            side=str(nearest.get("side") or Side.NEUTRAL.value),
            setup_type=SetupType.NONE.value, quality=0,
            reason=rejection_reason or "NO_ANCHOR_WITH_CONFIRMED_3M_REACTION",
            regime=regime, audit=audit, current_price=price,
        ), None

    stage_resolution = resolve_entry_stage(candidate, context, journal)
    conviction = dict(stage_resolution.get("probe_conviction") or {})
    candidate.entry_stage = str(stage_resolution.get("entry_stage") or EntryStage.PROBE.value)
    candidate.probe_conviction_tier = str(conviction.get("tier") or "")
    candidate.stage_plan = dict(candidate.stage_plan or {})
    candidate.stage_plan["probe_conviction"] = conviction
    candidate.stage_plan["entry_stage_resolution"] = stage_resolution

    plan = build_trade_plan(context, candidate, journal=journal, state=state)
    if not (plan.valid and plan.execution_ready):
        # The reaction was real but the trade is not: no geometry, or the daily
        # budget is gone. The anchor stays ARMED so the next run can retry.
        return Decision(
            id=new_id("sig"), time=now, action=Action.NO_SETUP.value,
            side=str(candidate.side), setup_type=str(candidate.setup_type),
            quality=safe_int(candidate.final_score),
            reason=str(plan.reason or "PLAN_NOT_EXECUTABLE"),
            regime=regime, candidate=candidate, plan=plan, audit=audit, current_price=price,
        ), plan

    return Decision(
        id=new_id("sig"), time=now, action=_entry_action_for_stage(plan.entry_stage),
        side=str(candidate.side), setup_type=str(candidate.setup_type),
        quality=safe_int(candidate.final_score), reason=str(plan.reason),
        regime=regime, candidate=candidate, plan=plan, audit=audit, current_price=price,
    ), plan


def _open_active_trade(
    context: dict[str, Any],
    candidate: Candidate,
    plan: TradePlan,
    decision: Decision,
    learning_mode: str,
    event_id: str,
) -> ActiveTrade:
    """Hand the plan to the unchanged supervision layer as an ActiveTrade."""
    return ActiveTrade(
        id=uuid.uuid4().hex[:10],
        signal_id=decision.id,
        side=str(candidate.side),
        setup_type=str(candidate.setup_type),
        setup_family=journal_setup_family(candidate.setup_type),
        canonical_setup_family=str(
            candidate.canonical_setup_family or canonical_setup_family(candidate.setup_type)
        ),
        family_episode_key=str(candidate.family_episode_key or candidate.thesis_key),
        opened_at=iso_now(),
        entry=round_price(plan.entry),
        stop_initial=round_price(plan.stop),
        stop_current=round_price(plan.stop),
        decision_stop=round_price(plan.decision_stop or plan.stop),
        catastrophic_stop=round_price(plan.catastrophic_stop or plan.stop),
        structural_invalidation=round_price(plan.structural_invalidation or candidate.invalidation_level),
        trigger_level=round_price(plan.trigger_level or candidate.trigger_level),
        planned_entry=round_price(plan.entry),
        planned_stop=round_price(plan.stop),
        tp0=round_price(plan.tp0),
        tp1=round_price(plan.tp1),
        tp2=round_price(plan.tp2),
        tp3=round_price(plan.tp3),
        tp0_size_pct=safe_float((plan.partial_plan or {}).get("TP0"), TP0_SIZE_PCT),
        tp1_size_pct=safe_float((plan.partial_plan or {}).get("TP1"), TP1_SIZE_PCT),
        tp2_size_pct=safe_float((plan.partial_plan or {}).get("TP2"), TP2_SIZE_PCT),
        tp3_runner_pct=safe_float((plan.partial_plan or {}).get("TP3"), TP3_RUNNER_PCT),
        quality=safe_int(decision.quality),
        position_risk_pct=safe_float(plan.position_risk_pct),
        best_price=round_price(plan.entry),
        worst_price=round_price(plan.entry),
        thesis_key=str(candidate.thesis_key),
        thesis=str(candidate.thesis),
        thesis_family_key=str(candidate.family_episode_key or candidate.thesis_key),
        primary_signal_id=decision.id,
        primary_signal_price=round_price(plan.entry),
        entry_delay_minutes=safe_float((candidate.reaction or {}).get("latency_minutes")),
        opened_regime=str(decision.regime),
        entry_level=str(decision.action),
        execution_source=str(plan.execution_source or candidate.execution_source),
        entry_stage=str(plan.entry_stage),
        stage_plan=dict(plan.stage_plan or {}),
        runtime_config_snapshot=dict(plan.runtime_config_snapshot or {}),
        breathing_profile=dict(plan.breathing_profile or {}),
        entry_quality=safe_int(candidate.entry_quality),
        evaluation_entry_quality=safe_float(candidate.entry_quality),
        trade_entry_quality=safe_float(candidate.entry_quality),
        preplan_entry_quality=safe_float(candidate.entry_freshness_score),
        setup_quality=safe_float(candidate.setup_quality_score),
        timing_quality=safe_float(candidate.timing_quality_score),
        trade_quality=safe_float(candidate.trade_quality_score),
        durability_quality=safe_int(candidate.durability_quality_score),
        entry_score_source="ANCHOR_REACTION_3M_GATES",
        scoring_mode=str(learning_mode or "NOT_LEARNED"),
        bot_version_at_entry=BOT_VERSION,
        architecture_version_at_entry=ARCHITECTURE_VERSION,
        journal_schema_at_entry=JOURNAL_VERSION,
        preconfirmation_event_id=str(event_id or ""),
    )


def run_bot() -> int:
    """One 15-minute cycle: read the market, judge the levels, act, report, save."""
    state = load_state()
    journal = load_journal()

    data = collect_market_data()
    price = safe_float(data.get("price"))
    if price <= 0:
        print("[ERROR] Немає ціни з жодного джерела — рішення не ухвалюється.")
        return 1

    context = build_context(data, state, journal)
    state["regime_memory"] = dict(context.get("regime_memory") or {})
    print(
        f"[INFO] Ціна {_fmt_price(price)} ({data.get('price_source')}) | "
        f"ATR15 {safe_float(context.get('atr15')):.6f} | regime={context.get('regime')} "
        f"session={context.get('session_name')}"
    )

    degradation = compute_degradation_table(journal)
    journal["degradation"] = degradation
    journal["setup_statistics"] = dict(degradation.get("statistics") or {})

    # --- 1. рівні та 3m-реакції -------------------------------------------
    # Detection runs BEFORE supervision on purpose: the unchanged PROBE
    # no-followthrough exit reads context["fresh_opposite_execution"], which only
    # exists once this run's reactions have been evaluated.
    anchors, anchor_audit = sync_anchors(context, stored_anchors(state), detect_anchors(context))
    anchors_by_id = {anchor.id: anchor for anchor in anchors}
    candidates: list[Candidate] = []
    refusals: list[tuple[Anchor, Reaction]] = []
    for anchor in anchors:
        reaction = evaluate_reaction(context, anchor)
        if not reaction.ready:
            refusals.append((anchor, reaction))
            continue
        candidate = build_candidate(context, anchor, reaction, degradation)
        if candidate is None:
            refusals.append((anchor, Reaction(
                anchor_id=anchor.id, ready=False, entry_price=price,
                reason="GATE_ADMISSION: SETUP_DEMOTED_BY_OWN_STATISTICS",
            )))
            continue
        candidates.append(candidate)
    ranked = rank_candidates(candidates)

    # --- 2. життєвий цикл попереднього підтвердження -----------------------
    resolved_events = resolve_preconfirmation_events(journal, context) if PRECONFIRMATION_LAYER_ENABLED else 0
    context["preconfirmation_events"] = list(journal.get("preconfirmation_events") or [])[-PRECONFIRM_EMBEDDED_JOURNAL_LIMIT:]

    # --- 3. супровід відкритої угоди (НЕЗМІННИЙ) ---------------------------
    active = active_trade_from_state(state)
    follow_result: dict[str, Any] = {}
    if active is not None:
        if ranked:
            opposite = next((c for c in ranked if c.side == _opposite_side(active.side)), None)
            if opposite is not None:
                context["fresh_opposite_execution"] = {
                    "executable": True,
                    "opposite": True,
                    "side": opposite.side,
                    "setup_type": opposite.setup_type,
                    "final_score": safe_int(opposite.final_score),
                    "anchor_id": opposite.anchor_id,
                    "source": "ANCHOR_REACTION_3M",
                }

        closed_side = str(active.side)
        # manage_active_trade moves trade.stop_current in place, so the "before" value
        # has to be read first or the journal cannot show what the stop did.
        stop_before = safe_float(active.stop_current)
        follow_result = manage_active_trade(active, context)
        if TELEGRAM_NOTIFY_EVERY_RUN or follow_result.get("closed") or follow_result.get("stop_changed"):
            send_telegram(build_follow_message(context, active, follow_result))

        if follow_result.get("closed"):
            result_class = classify_trade_result(
                follow_result.get("result_r"),
                follow_result.get("realized_return_pct"),
                str(follow_result.get("outcome_status") or "RESOLVED"),
            )
            close_action = str(follow_result.get("action") or "")
            link_preconfirmation_event_to_trade(journal, active, result_class, close_action)
            journal["trades"].append(_closed_trade_row(active, follow_result, context))
            journal.setdefault("signal_events", []).append(_management_event(
                active, follow_result, context, price, kind="CLOSE", stop_before=stop_before,
            ))
            store_active_trade(state, None)
            active = None
            append_history(state, {
                "type": "CLOSE", "side": closed_side, "action": close_action,
                "price": price, "result": result_class,
            })
            print(f"[INFO] Угоду закрито: {close_action} -> {result_class}")
        else:
            journal.setdefault("signal_events", []).append(_management_event(
                active, follow_result, context, price, kind="FOLLOW", stop_before=stop_before,
            ))
            store_active_trade(state, active)
            append_history(state, {
                "type": "FOLLOW", "side": closed_side, "action": follow_result.get("action"),
                "price": price, "trade_id": active.id, "signal_id": active.signal_id,
            })

    # --- 4. підсумки журналу (після закриття, щоб нова угода вже рахувалась) --
    journal["analytics"] = compute_analytics(journal)
    journal["entry_quality_audit"] = compute_entry_quality_audit(journal)
    journal["calendar_statistics"] = compute_calendar_statistics(journal)
    journal["learning_status"] = compute_learning_status(journal)
    learning_mode = str((journal["learning_status"] or {}).get("mode") or "")
    context["learning_warnings"] = learning_health_warnings(
        journal["learning_status"], journal["entry_quality_audit"],
    )

    # --- 5. рішення про вхід (лише без відкритої позиції) -------------------
    audit: dict[str, Any] = {
        "anchor_sync": anchor_audit,
        "anchor_watch": _anchor_watch(anchors, context),
        "rejected_hypotheses": _rejected_hypotheses(refusals),
        "candidates_reacted": len(candidates),
        "candidates_ranked": len(ranked),
        "daily_risk": daily_risk_budget(journal, state, 0.0),
        "degradation": {
            "executable_count": safe_int(degradation.get("executable_count")),
            "demoted": list(degradation.get("demoted") or []),
            "promoted": list(degradation.get("promoted") or []),
        },
        "preconfirmation": {
            "resolved_this_run": resolved_events,
            "pending": sum(
                1 for event in list(journal.get("preconfirmation_events") or [])
                if isinstance(event, dict) and _preconfirm_event_status(event) == "PENDING"
            ),
        },
        "price_source": context.get("price_source"),
        "execution_price_trusted": bool(context.get("execution_price_trusted")),
        "learning_mode": learning_mode,
        "schema_version": SCHEMA_VERSION,
    }

    if active is not None:
        # A trade is still open. Step 3 already wrote this cycle's history row
        # and already owns state["active_trade"]; nothing here may touch either.
        deferred_to_open_trade = True
        plan: Optional[TradePlan] = None
        decision = Decision(
            id=new_id("sig"), time=iso_now(), action=Action.NO_SETUP.value,
            side=str(active.side), setup_type=str(active.setup_type), quality=0,
            reason="ACTIVE_TRADE_OPEN", regime=str(context.get("regime") or ""),
            audit=audit, current_price=price,
        )
    else:
        deferred_to_open_trade = False
        selected, rejection_reason = _select_candidate(ranked, context)
        decision, plan = _make_decision(context, journal, state, selected, rejection_reason, audit)
        audit["selected"] = {
            "setup_type": decision.setup_type,
            "side": decision.side,
            "final_score": safe_int(decision.quality),
            "entry_stage": str(plan.entry_stage) if plan else "",
            "reason": decision.reason,
        }

    # --- 6. виконання -------------------------------------------------------
    opened: Optional[ActiveTrade] = None
    executable = bool(
        plan and plan.valid and plan.execution_ready
        and decision.action in EXECUTABLE_ENTRY_ACTIONS
    )
    if executable and not bool(context.get("execution_price_trusted")):
        # A cross-venue display price can describe the market but cannot fill an
        # order. The plan stays in the journal; the entry does not happen.
        decision.action = Action.NO_SETUP.value
        decision.reason = "PRICE_SOURCE_DISPLAY_ONLY_NOT_EXECUTABLE"
        executable = False

    if executable and plan is not None and decision.candidate is not None:
        event_id = ""
        if PRECONFIRMATION_LAYER_ENABLED:
            event = make_preconfirmation_event(context, decision.candidate, decision.id)
            journal.setdefault("preconfirmation_events", []).append(event)
            context["preconfirmation_events"] = list(journal["preconfirmation_events"])[-PRECONFIRM_EMBEDDED_JOURNAL_LIMIT:]
            event_id = event["event_id"]
            audit["preconfirmation_event_id"] = event_id

        opened = _open_active_trade(context, decision.candidate, plan, decision, learning_mode, event_id)
        store_active_trade(state, opened)
        anchor = anchors_by_id.get(str(decision.candidate.anchor_id))
        if anchor is not None:
            consume_anchor(anchor, AnchorState.TRIGGERED.value, "ENTRY_EXECUTED")
        append_history(state, {
            "type": decision.action, "side": decision.side, "setup_type": decision.setup_type,
            "quality": safe_int(decision.quality), "price": price,
            "trade_id": opened.id, "signal_id": decision.id,
        })
        print(
            f"[INFO] Угода відкрита: {decision.side} {decision.setup_type} "
            f"stage={opened.entry_stage} entry={_fmt_price(opened.entry)} "
            f"stop={_fmt_price(opened.stop_initial)} risk={opened.position_risk_pct:.4f}% "
            f"signal_id={opened.signal_id} trade_id={opened.id}"
        )
    elif not deferred_to_open_trade:
        append_history(state, {
            "type": decision.action, "side": decision.side, "setup_type": decision.setup_type,
            "quality": safe_int(decision.quality), "price": price, "reason": decision.reason,
        })

    # --- 7. один запис сигналу для журналу, навчання та дашборду ------------
    # signal_events belongs to supervision (section 3); the decision row below already
    # carries every field a lifecycle event would have repeated back.
    record = build_signal_record(context, decision, plan, audit)
    journal.setdefault("signals", []).append(record)
    lean = lean_training_signal(record)
    if lean:
        journal.setdefault("training_signals", []).append(lean)
    state["latest_signal"] = compact_signal_for_journal(record)

    # --- 8. повідомлення (без входу — так само кожні п'ятнадцять хвилин) -----
    if decision.action != Action.NO_SETUP.value or SEND_NO_SETUP or TELEGRAM_NOTIFY_EVERY_RUN:
        message = build_decision_message(context, decision)
        print("TELEGRAM (DECISION):", plain_telegram_text(message)[:320])
        send_telegram(message)

    store_anchors(state, anchors)
    save_state(state)
    save_journal(journal)
    if JOURNAL_VERBOSE:
        print("AUDIT:", json.dumps(json_safe(audit), ensure_ascii=False, indent=2))
    print(
        f"[INFO] Підсумок: anchors={anchor_audit.get('armed', 0)} reacted={len(candidates)} "
        f"executed={bool(opened)} closed_trades={len(list(journal.get('trades') or []))}"
    )
    print("BOT COMPLETE")
    return 0
# ==========================================================
# RUNTIME CONFIGURATION, JOURNAL AUDIT, SELF-TEST, ENTRYPOINT
# ==========================================================
# The previous file carried eighteen copies of validate_runtime_configuration_*
# and a self-test nested sixteen layers deep, most of it asserting that the last
# version's own audit blobs were still present. What follows validates only the
# invariants that can break the strategy *silently* — a bot that keeps running and
# keeps sending messages while never taking a valid trade is worse than one that
# refuses to start — and exercises the real chain on a synthetic market with no
# network and no writes to the production journal.

CONFIG_SCHEMA_VERSION = "organic_runtime_config_v10.0.0"
SELF_TEST_SCHEMA_VERSION = "organic_self_test_v10.0.0"
JOURNAL_AUDIT_SCHEMA_VERSION = "organic_journal_audit_v10.0.0"

CRON_CADENCE_MINUTES = 15


def validate_runtime_configuration() -> dict[str, Any]:
    """Fail closed on configuration that quietly disables the strategy."""
    problems: list[str] = []
    warnings: list[str] = []

    partial_total = TP0_SIZE_PCT + TP1_SIZE_PCT + TP2_SIZE_PCT + TP3_RUNNER_PCT
    if abs(partial_total - 1.0) > 1e-6:
        problems.append(
            f"TP partials sum to {partial_total:.4f}, not 1.0 — position accounting is wrong."
        )

    if DAILY_RISK_CAP < CORE_RISK_PCT:
        problems.append(
            f"DAILY_RISK_CAP {DAILY_RISK_CAP:.2f}% < CORE_RISK_PCT {CORE_RISK_PCT:.2f}% — "
            "a CORE entry could never be funded and would silently demote to PROBE every time."
        )

    # The entry engine refuses any stop wider than MAX_STOP_ATR; supervision will
    # not accept one tighter than MIN_STOP_ATR15. If those cross, every plan fails
    # closed and the bot reports NO_SETUP forever without ever naming a reason.
    if MIN_STOP_ATR15 > MAX_STOP_ATR:
        problems.append(
            f"MIN_STOP_ATR15 {MIN_STOP_ATR15:.2f} > MAX_STOP_ATR {MAX_STOP_ATR:.2f} — "
            "the supervision stop floor exceeds the early-entry stop cap, so no plan can ever be valid."
        )

    if not MIN_SCORE_PROBE <= MIN_SCORE_ACCEPTANCE <= MIN_SCORE_CORE:
        problems.append(
            f"score floors not ordered: PROBE {MIN_SCORE_PROBE} <= ACCEPTANCE {MIN_SCORE_ACCEPTANCE} "
            f"<= CORE {MIN_SCORE_CORE} is required."
        )

    bar_minutes = PRECONFIRM_RESOLUTION_BAR_MS / 60000.0
    if PRECONFIRM_ACCEPTANCE_CLOSES * bar_minutes > PRECONFIRM_WINDOW_MINUTES:
        problems.append(
            f"PRECONFIRM_ACCEPTANCE_CLOSES {PRECONFIRM_ACCEPTANCE_CLOSES} needs "
            f"{PRECONFIRM_ACCEPTANCE_CLOSES * bar_minutes:.0f} min but the window is "
            f"{PRECONFIRM_WINDOW_MINUTES} min — no event could ever reach CONFIRMED, so every PROBE "
            "would keep the fast no-followthrough exit."
        )

    if PRECONFIRM_WINDOW_MINUTES < CRON_CADENCE_MINUTES:
        warnings.append(
            f"PRECONFIRM_WINDOW_MINUTES {PRECONFIRM_WINDOW_MINUTES} is shorter than the "
            f"{CRON_CADENCE_MINUTES}-minute cron cadence: an event can go terminal between two runs."
        )

    if ANCHOR_COOLDOWN_MIN >= ANCHOR_MAX_AGE_MIN:
        warnings.append(
            f"ANCHOR_COOLDOWN_MIN {ANCHOR_COOLDOWN_MIN} >= ANCHOR_MAX_AGE_MIN {ANCHOR_MAX_AGE_MIN}: "
            "a consumed anchor expires before its cooldown ends, so the level can never re-arm."
        )

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        warnings.append("Telegram credentials absent — decisions are printed to the log instead of sent.")

    if os.getenv("GITHUB_ACTIONS") and not JOURNAL_PERSISTENCE_CONFIRMED:
        warnings.append("GitHub Actions: не підтверджено commit/cache persistence для journal/state.")

    for label, path in (("SIGNAL_MEMORY_FILE", STATE_FILE), ("SIGNAL_JOURNAL_FILE", JOURNAL_FILE)):
        parent = Path(path).parent
        if not parent.is_dir():
            warnings.append(f"{label} directory does not exist: {parent}")

    return {
        "passed": not problems,
        "problems": problems,
        "warnings": warnings,
        "config": {
            "bot_version": BOT_VERSION,
            "architecture_version": ARCHITECTURE_VERSION,
            "journal_version": JOURNAL_VERSION,
            "entry_route": "ANCHOR_REACTION_3M_MARKET",
            "anchor_max_atr": ANCHOR_MAX_ATR,
            "anchor_max_age_min": ANCHOR_MAX_AGE_MIN,
            "anchor_cooldown_min": ANCHOR_COOLDOWN_MIN,
            "trigger_lookback_3m": TRIGGER_LOOKBACK_3M,
            "max_stop_atr": MAX_STOP_ATR,
            "min_runway_r": MIN_RUNWAY_R,
            "score_floors": {"probe": MIN_SCORE_PROBE, "acceptance": MIN_SCORE_ACCEPTANCE, "core": MIN_SCORE_CORE},
            "risk_pct": {"probe": PROBE_RISK_PCT, "acceptance": ACCEPTANCE_RISK_PCT, "core": CORE_RISK_PCT},
            "daily_risk_cap": DAILY_RISK_CAP,
            "tp_partials": {"TP0": TP0_SIZE_PCT, "TP1": TP1_SIZE_PCT, "TP2": TP2_SIZE_PCT, "TP3": TP3_RUNNER_PCT},
            "preconfirmation": {
                "enabled": PRECONFIRMATION_LAYER_ENABLED,
                "window_minutes": PRECONFIRM_WINDOW_MINUTES,
                "acceptance_closes": PRECONFIRM_ACCEPTANCE_CLOSES,
            },
            "probe_no_followthrough": {
                "enabled": PROBE_NO_FOLLOWTHROUGH_ENABLED,
                "minutes": PROBE_NO_FOLLOWTHROUGH_MINUTES,
                "max_mfe_r": PROBE_NO_FOLLOWTHROUGH_MAX_MFE_R,
            },
            "persistence": {
                "state_file": str(STATE_FILE),
                "journal_file": str(JOURNAL_FILE),
                "journal_limit": MAX_JOURNAL,
                "persistence_confirmed": JOURNAL_PERSISTENCE_CONFIRMED,
            },
            "notifications": {
                "send_no_setup": SEND_NO_SETUP,
                "notify_every_run": TELEGRAM_NOTIFY_EVERY_RUN,
                "cron_cadence_minutes": CRON_CADENCE_MINUTES,
            },
        },
        "schema_version": CONFIG_SCHEMA_VERSION,
    }


# ==========================================================
# JOURNAL AUDIT  (--audit-journal: replay without trading)
# ==========================================================

def run_audit_journal(path: str) -> dict[str, Any]:
    """Re-derive every statistic from a stored journal. No network, no writes.

    The point is drift: if the analytics stored in the file disagree with the
    analytics recomputed from the trades in the same file, some past run wrote
    them from a different view of the data.
    """
    raw = load_json(Path(path), {})
    if not isinstance(raw, dict) or not raw:
        return {"ok": False, "path": str(path), "error": "EMPTY_OR_UNREADABLE",
                "schema_version": JOURNAL_AUDIT_SCHEMA_VERSION}

    journal = {
        "trades": [t for t in (raw.get("trades") or []) if isinstance(t, dict)],
        "signals": [s for s in (raw.get("signals") or []) if isinstance(s, dict)],
        "training_signals": [s for s in (raw.get("training_signals") or []) if isinstance(s, dict)],
        "signal_events": [e for e in (raw.get("signal_events") or []) if isinstance(e, dict)],
        "preconfirmation_events": [e for e in (raw.get("preconfirmation_events") or []) if isinstance(e, dict)],
    }

    analytics = compute_analytics(journal)
    degradation = compute_degradation_table(journal)
    entry_audit = compute_entry_quality_audit(journal)
    learning = compute_learning_status(journal)

    stored = dict(raw.get("analytics") or {})
    stored_is_v10 = str(stored.get("schema_version") or "") == ANALYTICS_SCHEMA_VERSION
    drift = {
        key: {"stored": stored.get(key), "recomputed": analytics.get(key)}
        for key in ("trades", "wins", "losses", "win_rate", "net_r", "expectancy_r")
        if abs(safe_float(stored.get(key)) - safe_float(analytics.get(key))) > 1e-6
    } if stored_is_v10 else {}

    setups = [
        {
            "setup_type": name,
            "family": journal_setup_family(name),
            "trades": safe_int(row.get("trades")),
            "expectancy_r": round(safe_float(row.get("expectancy_r")), 4),
            "win_rate": round(safe_float(row.get("win_rate")), 4),
            "wilson_lower": round(safe_float(row.get("wilson_lower")), 4),
            "status": str((degradation["setups"].get(name) or {}).get("status") or ""),
            "executable": bool((degradation["setups"].get(name) or {}).get("executable", True)),
        }
        for name, row in (analytics.get("by_setup") or {}).items()
        if isinstance(row, dict)
    ]
    setups.sort(key=lambda row: row["expectancy_r"], reverse=True)

    signals = len(journal["signals"])
    # `executed` is v10's own flag; rows inherited from the legacy journal carry the
    # same fact in `action`. Reading only the flag reported a 0% execution rate
    # against a journal that had 30 closed trades in it.
    executed = sum(
        1 for s in journal["signals"]
        if s.get("executed") or str(s.get("action") or "") in EXECUTABLE_ENTRY_ACTIONS
    )
    return {
        "ok": True,
        "path": str(path),
        "source_version": str(raw.get("version") or ""),
        "source_architecture": str(raw.get("architecture_version") or ""),
        "journal_version": safe_int(raw.get("journal_version"), 1),
        "legacy_keys_present": sorted(key for key in raw if key not in JOURNAL_CORE_KEYS),
        "funnel": {
            "signals": signals,
            "executed": executed,
            "closed_trades": len(journal["trades"]),
            "execution_rate": round(executed / signals, 4) if signals else 0.0,
            "preconfirmation_events": len(journal["preconfirmation_events"]),
        },
        "analytics": analytics,
        "analytics_drift": drift,
        "analytics_from_this_schema": stored_is_v10,
        "entry_quality_audit": entry_audit,
        "learning_status": learning,
        "learning_warnings": learning_health_warnings(learning, entry_audit),
        "degradation": {
            "executable_count": degradation["executable_count"],
            "demoted": degradation["demoted"],
            "promoted": degradation["promoted"],
        },
        "setups": setups,
        "schema_version": JOURNAL_AUDIT_SCHEMA_VERSION,
    }


# ==========================================================
# SYNTHETIC MARKET  (self-test only: no network, no I/O)
# ==========================================================

def _synthetic_bars(interval_min: int, count: int, builder: Any, now_ms: int) -> list[Candle]:
    step = interval_min * 60_000
    end = (now_ms // step) * step
    return [builder(end - (count - 1 - index) * step, index) for index in range(count)]


def _synthetic_market(side: str, *, reaction: bool, distance_atr: float = 0.10) -> dict[str, Any]:
    """A deterministic market with a known anchor level and a known ATR.

    The 15m walk gives atr15 ~= 0.30 and the 3m walk atr3 ~= 0.20, so every
    threshold the reaction engine tests can be expressed in cents. Nothing here
    is random: the self-test must not be able to flake.
    """
    long_side = str(side).upper() == Side.LONG.value
    price = 70.00
    level = price - distance_atr * 0.30 if long_side else price + distance_atr * 0.30
    now_ms = int(now_utc().timestamp() * 1000)

    def drift15(ts: int, index: int) -> Candle:
        base = price - 0.60 + 0.006 * index
        return Candle(ts=ts, open=base, high=base + 0.18, low=base - 0.12, close=base + 0.05)

    def drift3(ts: int, index: int) -> Candle:
        base = price - 0.30 + 0.004 * index
        return Candle(ts=ts, open=base, high=base + 0.12, low=base - 0.08, close=base + 0.03)

    candles3 = _synthetic_bars(3, 60, drift3, now_ms)
    if reaction:
        # The last two confirmed 3m bars ARE the entry evidence: one visits the
        # level and closes back off it, the next displaces away from it.
        if long_side:
            candles3[-2] = Candle(ts=candles3[-2].ts, open=69.90, high=70.05, low=69.85, close=70.02)
            candles3[-1] = Candle(ts=candles3[-1].ts, open=70.02, high=70.20, low=70.00, close=70.16)
        else:
            candles3[-2] = Candle(ts=candles3[-2].ts, open=70.10, high=70.15, low=69.95, close=69.98)
            candles3[-1] = Candle(ts=candles3[-1].ts, open=69.98, high=70.00, low=69.80, close=69.84)

    def drift1h(ts: int, index: int) -> Candle:
        base = price - 1.00 + 0.010 * index
        return Candle(ts=ts, open=base, high=base + 0.10, low=base - 0.10, close=base + 0.05)

    def drift4h(ts: int, index: int) -> Candle:
        base = price - 2.00 + 0.020 * index
        return Candle(ts=ts, open=base, high=base + 0.20, low=base - 0.20, close=base + 0.10)

    candles15 = _synthetic_bars(15, 200, drift15, now_ms)
    return {
        "price": price,
        "price_source": "SELF_TEST_SYNTHETIC",
        "execution_price_trusted": True,
        "execution_price_quality": "TRUSTED",
        "execution_venue": "SELF_TEST",
        "change24h": 0.40,
        "volume24h": 1_000_000.0,
        "ticker_ts": now_ms,
        "spread": 0.01,
        "candles": {
            "3m": candles3,
            "15m": candles15,
            "1H": _synthetic_bars(60, 160, drift1h, now_ms),
            "4H": _synthetic_bars(240, 140, drift4h, now_ms),
        },
        "htf_source": {"1H": "SELF_TEST", "4H": "SELF_TEST"},
        "smt_candles_15m": list(candles15),
        "smt_asset_id": "CL-USDT-SWAP",
        "instrument": "BZ-USDT-SWAP",
        "instrument_label": INSTRUMENT_LABEL,
        "fetched_at": iso_now(),
        "_anchor_level": level,
    }


def _synthetic_context(side: str, *, reaction: bool, distance_atr: float = 0.10) -> tuple[dict[str, Any], Anchor, dict[str, Any]]:
    data = _synthetic_market(side, reaction=reaction, distance_atr=distance_atr)
    level = float(data.pop("_anchor_level"))
    context = build_context(data, {"regime_memory": {}}, {"trades": [], "signals": []})
    context["preconfirmation_events"] = []
    context["fresh_opposite_execution"] = {}
    context["learning_warnings"] = []
    atr15 = safe_float(context["atr15"])
    sign = side_sign(side)
    anchor = make_anchor(
        SetupType.SWEEP_RECLAIM.value, str(side), level, level - sign * 0.30 * atr15 / 0.30,
        AnchorKind.DEMAND_ZONE.value if sign > 0 else AnchorKind.SUPPLY_ZONE.value,
        "self-test synthetic level", context, score=70.0,
    )
    if anchor is None:
        raise AssertionError("make_anchor refused a valid synthetic level")
    return context, anchor, data


# ==========================================================
# SELF-TEST
# ==========================================================
# Each check returns a list of problems; an empty list means it passed. A check
# that raises is a failure, not a skip.

def _check_configuration() -> list[str]:
    report = validate_runtime_configuration()
    problems = [str(row) for row in report["problems"]]

    budget = daily_risk_budget({"trades": []}, {"active_trade": None}, 0.0)
    if budget["exhausted"]:
        problems.append("a flat book with the whole daily cap free reports the risk budget exhausted")
    if abs(safe_float(budget["risk_budget_left_before"]) - DAILY_RISK_CAP) > 1e-6:
        problems.append(
            f"a flat book shows {budget['risk_budget_left_before']}% free, expected {DAILY_RISK_CAP:.2f}%"
        )
    funded = daily_risk_budget({"trades": []}, {"active_trade": None}, PROBE_RISK_PCT)
    if funded["exhausted"] or safe_float(funded["granted_risk_pct"]) <= 0:
        problems.append("a PROBE-sized request against an empty book was not funded")
    return problems


def _check_late_entry_is_impossible() -> list[str]:
    """The core fix, stated as a property rather than as one happy path.

    Across every synthetic scenario, no reaction may be READY unless price is
    within ANCHOR_MAX_ATR of the level and the evidence is recent. The old bot
    allowed an entry up to 3.75 ATR from the level after a closed 15m candle,
    which is what produced MFE < 0.2R on 18 of 30 trades.
    """
    problems: list[str] = []
    saw_ready = False
    max_latency = TRIGGER_LOOKBACK_3M * 3.0
    for side in (Side.LONG.value, Side.SHORT.value):
        for reaction in (True, False):
            for distance in (0.10, 0.34, 1.20, 3.75):
                context, anchor, _ = _synthetic_context(side, reaction=reaction, distance_atr=distance)
                result = evaluate_reaction(context, anchor)
                proximity = safe_float((result.gates.get("GATE_PROXIMITY") or {}).get("distance_atr"), 99.0)
                if not result.ready:
                    # A far level must be refused BY the proximity gate, not by
                    # some later gate that happens to fail too.
                    if distance > ANCHOR_MAX_ATR and not result.reason.startswith("GATE_PROXIMITY"):
                        problems.append(
                            f"{side} @ {distance:.2f}ATR refused by {result.reason}, expected GATE_PROXIMITY"
                        )
                    continue
                saw_ready = True
                if proximity > ANCHOR_MAX_ATR:
                    problems.append(f"READY at {proximity:.2f} ATR from the anchor (cap {ANCHOR_MAX_ATR})")
                if result.latency_minutes > max_latency:
                    problems.append(
                        f"READY on evidence {result.latency_minutes:.1f} min old (window {max_latency:.0f} min)"
                    )
                if not result.gates.get("ALL_PASS"):
                    problems.append("READY without ALL_PASS in the gate audit")
    if not saw_ready:
        problems.append("no synthetic scenario produced a READY reaction — the entry path is dead")
    return problems


def _check_entry_chain() -> list[str]:
    """anchor -> reaction -> candidate -> stage -> plan, all the way to a fill."""
    context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=True)
    reaction = evaluate_reaction(context, anchor)
    if not reaction.ready:
        return [f"synthetic reaction not ready: {reaction.reason}"]

    journal: dict[str, Any] = {"trades": [], "signals": [], "training_signals": [],
                               "signal_events": [], "preconfirmation_events": []}
    degradation = compute_degradation_table(journal)
    candidate = build_candidate(context, anchor, reaction, degradation)
    if candidate is None:
        return ["build_candidate refused a ready reaction"]

    ranked = rank_candidates([candidate])
    if not ranked:
        return ["rank_candidates dropped the only candidate"]

    selected, reason = _select_candidate(ranked, context)
    if selected is None:
        return [f"_select_candidate refused: {reason}"]

    stage = resolve_entry_stage(selected, context, journal)
    selected.entry_stage = str(stage["entry_stage"])
    plan = build_trade_plan(context, selected, journal=journal, state={"active_trade": None})
    problems: list[str] = []
    if not plan.valid:
        problems.append(f"plan invalid: {plan.reason}")
    if not plan.execution_ready:
        problems.append(f"plan not execution_ready: {plan.reason}")
    if plan.reason != "ANCHOR_REACTION_MARKET_ENTRY":
        problems.append(f"unexpected plan reason {plan.reason}")

    risk = abs(plan.entry - plan.stop)
    if risk <= 0:
        problems.append("plan has no risk distance")
    else:
        atr15 = safe_float(context["atr15"])
        if risk > MAX_STOP_ATR * atr15 + 1e-9:
            problems.append(f"stop {risk / atr15:.2f} ATR exceeds the early-entry cap {MAX_STOP_ATR}")
        if plan.rr1 < MIN_RR1 - 1e-9:
            problems.append(f"rr1 {plan.rr1:.2f} below MIN_RR1 {MIN_RR1}")
    for name, level in (("tp1", plan.tp1), ("tp2", plan.tp2), ("tp3", plan.tp3)):
        if level <= plan.entry:
            problems.append(f"{name} {level} is not above the long entry {plan.entry}")
    if not (plan.tp1 <= plan.tp2 <= plan.tp3):
        problems.append(f"long targets out of order: {plan.tp1} {plan.tp2} {plan.tp3}")
    partials = dict(plan.partial_plan or {})
    if abs(sum(partials.values()) - 1.0) > 1e-6:
        problems.append(f"partial_plan sums to {sum(partials.values()):.6f}, not 1.0")
    if set(partials) != {"TP0", "TP1", "TP2", "TP3"}:
        problems.append(f"partial_plan keys {sorted(partials)} != TP0..TP3")
    return problems


def _check_supervision_unchanged() -> list[str]:
    """The supervision layer must still run, byte-ported, inside the new file."""
    context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=True)
    reaction = evaluate_reaction(context, anchor)
    journal: dict[str, Any] = {"trades": [], "signals": [], "training_signals": [],
                               "signal_events": [], "preconfirmation_events": []}
    candidate = build_candidate(context, anchor, reaction, compute_degradation_table(journal))
    if candidate is None:
        return ["no candidate to open a trade from"]
    candidate.entry_stage = EntryStage.PROBE.value
    plan = build_trade_plan(context, candidate, journal=journal, state={"active_trade": None})
    if not plan.valid:
        return [f"no plan to supervise: {plan.reason}"]

    decision = Decision(
        id=new_id("sig"), time=iso_now(), action=Action.PROBE_ENTRY.value,
        side=candidate.side, setup_type=candidate.setup_type,
        quality=safe_int(candidate.final_score), reason=plan.reason,
        regime=str(context.get("regime") or ""), candidate=candidate, plan=plan,
        current_price=plan.entry,
    )
    trade = _open_active_trade(context, candidate, plan, decision, "BOOTSTRAP", "selftest-event")
    problems: list[str] = []
    if trade.entry_stage != EntryStage.PROBE.value:
        problems.append(f"opened stage {trade.entry_stage}, expected PROBE")
    if trade.best_price != plan.entry or trade.worst_price != plan.entry:
        problems.append("MFE/MAE tracking did not start at the entry price")

    # A price through the stop must close the trade. This is the single most
    # important behaviour in the whole file and it belongs to the unchanged layer.
    stopped = dict(context)
    stopped["price"] = plan.stop - 0.05
    result = manage_active_trade(trade, stopped)
    if not result.get("closed"):
        problems.append(f"price below the stop did not close the trade: {result.get('action')} {result.get('reason')}")
    if str(result.get("action") or "") != Action.STOP.value:
        problems.append(f"closed with action {result.get('action')}, expected STOP")
    if safe_float(result.get("result_r")) > 0:
        problems.append(f"a stopped-out long reported result_r {result.get('result_r')}")

    # Hold: a price between the stop and TP0 must not close anything.
    holding = dict(context)
    holding["price"] = plan.entry + (plan.tp0 - plan.entry) * 0.5 if plan.tp0 > plan.entry else plan.entry
    hold_result = manage_active_trade(_open_active_trade(
        context, candidate, plan, decision, "BOOTSTRAP", "selftest-event"), holding)
    if hold_result.get("closed"):
        problems.append(f"a trade inside its own range was closed: {hold_result.get('reason')}")

    # The journaled management event keeps the legacy field names, because
    # signal_events already holds hundreds of inherited rows of exactly this shape.
    watched = _open_active_trade(context, candidate, plan, decision, "BOOTSTRAP", "selftest-event")
    event = _management_event(watched, hold_result, holding, safe_float(holding["price"]),
                              kind="FOLLOW", stop_before=safe_float(watched.stop_current))
    for key in ("time", "type", "action", "side", "price", "trade_id", "signal_id",
                "management_state", "stop_before", "stop_after", "profit_protection",
                "ratchet_evidence", "management_evidence_schema_version"):
        if key not in event:
            problems.append(f"management event is missing the legacy field {key}")
    if event.get("type") != "FOLLOW" or event.get("trade_id") != watched.id:
        problems.append("management event does not identify the trade and step it came from")

    # Ordering hazard, made explicit: supervision moves trade.stop_current in place, so
    # reading it twice would report no move and the journal could never explain a stop.
    captured = safe_float(watched.stop_current)
    watched.stop_current = round_price(captured + 1.0)
    moved = _management_event(watched, {"action": "PROTECT", "management_state": "PROTECT"},
                              holding, safe_float(holding["price"]),
                              kind="FOLLOW", stop_before=captured)
    if safe_float(moved["stop_before"]) != captured:
        problems.append(f"stop_before {moved['stop_before']} is not the pre-supervision value {captured}")
    if safe_float(moved["stop_after"]) != safe_float(watched.stop_current):
        problems.append("stop_after is not the post-supervision value")
    if moved["stop_before"] == moved["stop_after"]:
        problems.append("a stop move is invisible in the journaled management event")
    return problems


def _check_probe_no_followthrough() -> list[str]:
    """A PROBE whose preconfirmation FAILED keeps the fast exit; CONFIRMED does not."""
    context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=True)
    reaction = evaluate_reaction(context, anchor)
    journal: dict[str, Any] = {"trades": [], "signals": [], "training_signals": [],
                               "signal_events": [], "preconfirmation_events": []}
    candidate = build_candidate(context, anchor, reaction, compute_degradation_table(journal))
    if candidate is None:
        return ["no candidate to supervise"]
    candidate.entry_stage = EntryStage.PROBE.value
    plan = build_trade_plan(context, candidate, journal=journal, state={"active_trade": None})
    if not plan.valid:
        return [f"no plan: {plan.reason}"]

    decision = Decision(
        id=new_id("sig"), time=iso_now(), action=Action.PROBE_ENTRY.value,
        side=candidate.side, setup_type=candidate.setup_type,
        quality=safe_int(candidate.final_score), reason=plan.reason,
        regime=str(context.get("regime") or ""), candidate=candidate, plan=plan,
        current_price=plan.entry,
    )
    event = make_preconfirmation_event(context, candidate, decision.id)
    aged = (now_utc() - timedelta(minutes=PROBE_NO_FOLLOWTHROUGH_MINUTES + 30)).isoformat()

    def probe_with(status: str) -> dict[str, Any]:
        trade = _open_active_trade(context, candidate, plan, decision, "BOOTSTRAP", event["event_id"])
        trade.opened_at = aged
        trade.best_price = plan.entry + abs(plan.entry - plan.stop) * 0.10
        row = dict(event)
        _preconfirm_set_status(row, status, reason=f"SELF_TEST_{status}",
                               resolved_ts=int(now_utc().timestamp() * 1000))
        local = dict(context)
        local["price"] = plan.entry
        local["preconfirmation_events"] = [row]
        return probe_no_followthrough_exit_profile(trade, local)

    problems: list[str] = []
    for status in ("FAILED", "EXPIRED"):
        profile = probe_with(status)
        if not profile.get("applies"):
            problems.append(f"{status}: profile does not apply ({profile.get('reason_code')})")
        if not profile.get("exit"):
            problems.append(f"{status}: aged unprofitable PROBE was not exited ({profile.get('reason_code')})")
        if profile.get("preconfirmation_status") != status:
            problems.append(f"{status}: event read back as {profile.get('preconfirmation_status')}")

    confirmed = probe_with("CONFIRMED")
    if confirmed.get("preconfirmation_status") != "CONFIRMED":
        problems.append(f"CONFIRMED event read back as {confirmed.get('preconfirmation_status')}")
    if confirmed.get("exit"):
        problems.append("a CONFIRMED PROBE inside its lease was exited — supervision behaviour changed")
    return problems


def _check_preconfirmation_lifecycle() -> list[str]:
    """The resolver labels an event from price alone: CONFIRMED / FAILED / EXPIRED."""
    context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=True)
    reaction = evaluate_reaction(context, anchor)
    journal: dict[str, Any] = {"trades": [], "signals": [], "training_signals": [],
                               "signal_events": [], "preconfirmation_events": []}
    candidate = build_candidate(context, anchor, reaction, compute_degradation_table(journal))
    if candidate is None:
        return ["no candidate"]
    event = make_preconfirmation_event(context, candidate, new_id("sig"))
    problems: list[str] = []

    for key in ("event_id", "thesis_key", "side", "setup_type", "event_kind", "anchor_id",
                "confirmation_level", "invalidation_level", "resolve_after_ts", "expires_ts"):
        if event.get(key) in (None, ""):
            problems.append(f"event missing {key}")
    if event.get("status") != "PENDING":
        problems.append(f"new event status {event.get('status')}, expected PENDING")
    if safe_float(event["confirmation_level"]) <= safe_float(context["price"]):
        problems.append("confirmation level is not beyond the entry for a long")
    if safe_float(event["invalidation_level"]) >= safe_float(context["price"]):
        problems.append("invalidation level is not behind the entry for a long")
    for banned in ("probability", "features", "forecast_schema_version", "model_coefficients"):
        if banned in event:
            problems.append(f"event still carries the removed ML field {banned}")

    # Drift guard: save_journal keeps only PRECONFIRM_EVENT_KEEP_KEYS, so a key the
    # producer writes but the keep-list omits would vanish silently on the first save.
    unkept = sorted(set(event) - PRECONFIRM_EVENT_KEEP_KEYS)
    if unkept:
        problems.append(f"make_preconfirmation_event writes keys the keep-list would drop: {unkept}")
    for key in ("resolution_reason", "resolved_ts", "resolved_at", "outcome_ts",
                "resolved_price", "resolution_evidence"):
        if key not in PRECONFIRM_EVENT_KEEP_KEYS:
            problems.append(f"keep-list omits the resolution field {key}")

    # Events inherited from the legacy journal average ~9.6 KB, of which ~6.7 KB sits in
    # estimate_at_observation alone; compaction must strip that and keep the verdict.
    legacy_event = dict(event)
    legacy_event.update({
        "estimate_at_observation": {"bloated": [0.0] * 1200},
        "features": {"f0": 1.0, "f1": 2.0},
        "probability": 0.61,
        "ict_model": {"bias": "bullish"},
        "dedup_key": "x" * 200,
        "status": "CONFIRMED",
        "resolution_reason": "ACCEPTANCE_CLOSES",
        "resolution_evidence": {"acceptance_closes": 2},
        "resolved_price": safe_float(context["price"]),
    })
    compacted = compact_preconfirmation_event(legacy_event)
    for residue in ("estimate_at_observation", "features", "probability", "ict_model", "dedup_key"):
        if residue in compacted:
            problems.append(f"compaction kept the removed ML field {residue}")
    for kept in ("event_id", "confirmation_level", "invalidation_level", "status",
                 "resolution_reason", "resolution_evidence", "resolved_price"):
        if compacted.get(kept) in (None, "", {}):
            problems.append(f"compaction dropped {kept}")
    # Measured on the real inherited rows: 4.67 MB -> 0.85 MB. A 4x floor keeps the
    # assertion meaningful without making it sensitive to the synthetic blob's size.
    if len(json.dumps(compacted)) * 4 >= len(json.dumps(legacy_event)):
        problems.append("compaction did not meaningfully shrink a legacy event")
    if compact_preconfirmation_event({"status": "CONFIRMED"}):
        problems.append("an event with no identity survived compaction; it cannot be linked to a trade")

    # An event whose window has fully elapsed with no terminal evidence expires.
    stale = dict(event)
    stale["observed_ts"] = int(now_utc().timestamp() * 1000) - (PRECONFIRM_WINDOW_MINUTES + 20) * 60_000
    stale["created_ts"] = stale["observed_ts"]
    stale["resolve_after_ts"] = stale["observed_ts"] + PRECONFIRM_WINDOW_MINUTES * 60_000
    stale["expires_ts"] = stale["resolve_after_ts"]
    book = {"preconfirmation_events": [stale]}
    resolve_preconfirmation_events(book, context)
    if _preconfirm_event_status(stale) != "EXPIRED":
        problems.append(f"an event past its window resolved to {_preconfirm_event_status(stale)}, expected EXPIRED")

    # A fresh event with no evidence yet stays PENDING.
    book = {"preconfirmation_events": [dict(event)]}
    resolve_preconfirmation_events(book, context)
    if _preconfirm_event_status(book["preconfirmation_events"][0]) != "PENDING":
        problems.append("a fresh event with no terminal evidence left PENDING")
    return problems


def _check_persistence_roundtrip() -> list[str]:
    """State and journal survive a save/load cycle, and legacy blobs are pruned."""
    real_state, real_journal = STATE_FILE, JOURNAL_FILE
    workdir = tempfile.mkdtemp(prefix="bzu-selftest-")
    problems: list[str] = []
    try:
        globals()["STATE_FILE"] = Path(workdir) / "last_signal_v6_4.json"
        globals()["JOURNAL_FILE"] = Path(workdir) / "signal_journal_v6_4.json"

        context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=True)
        anchor.state = AnchorState.ARMED.value
        state = load_state()
        store_anchors(state, [anchor])
        state["regime_memory"] = {"last_regime": "TREND", "last_bias": "LONG"}
        trade = ActiveTrade(
            id="selftest-trade", side=Side.LONG.value,
            setup_type=SetupType.SWEEP_RECLAIM.value,
            setup_family=canonical_setup_family(SetupType.SWEEP_RECLAIM.value),
            opened_at=iso_now(), entry=70.0, stop_initial=69.60, stop_current=69.60,
            structural_invalidation=69.50, tp1=71.0, tp2=71.5, tp3=72.0,
            quality=70, position_risk_pct=0.12, best_price=70.10, worst_price=69.95,
        )
        store_active_trade(state, trade)
        save_state(state)

        reloaded = load_state()
        if not reloaded.get("active_trade"):
            problems.append("an open trade did not survive the state round-trip")
        else:
            restored = active_trade_from_state(reloaded)
            if restored is None:
                problems.append("active_trade_from_state could not rebuild the stored trade")
            elif (restored.id, restored.entry, restored.stop_current) != (trade.id, trade.entry, trade.stop_current):
                problems.append("the restored trade differs from the one that was stored")
        carried = stored_anchors(reloaded)
        if len(carried) != 1 or str(carried[0].get("id")) != anchor.id:
            problems.append("anchor memory did not survive the state round-trip")
        if dict(reloaded.get("regime_memory") or {}).get("last_regime") != "TREND":
            problems.append("regime memory did not survive the state round-trip")

        journal = load_journal()
        journal["trades"].append({
            "id": "t1", "signal_id": "s1", "side": "LONG",
            "setup_type": SetupType.SWEEP_RECLAIM.value,
            "result_r": 1.4, "pnl_r": 1.4, "realized_return_pct": 0.9,
            "outcome_status": "RESOLVED", "close_action": "TP1", "closed_at": iso_now(),
        })
        journal["signals"].append({"id": "s1", "type": Action.ENTRY.value, "executed": True,
                                   "preconfirmation_event_id": "e1", "time": iso_now()})
        journal["preconfirmation_events"].append({"event_id": "e1", "status": "CONFIRMED"})
        journal["trade_entry_quality_v9532"] = {"legacy": True}
        journal["some_removed_audit_blob"] = [1, 2, 3]
        save_journal(journal)

        dropped = list((journal.get("migration") or {}).get("legacy_keys_dropped_on_save") or [])
        if "some_removed_audit_blob" not in dropped:
            problems.append(f"save_journal did not record the prune: {dropped}")
        on_disk = load_json(JOURNAL_FILE, {})
        for blob in ("trade_entry_quality_v9532", "some_removed_audit_blob"):
            if blob in on_disk:
                problems.append(f"legacy audit blob {blob} is still on disk after save_journal")
        if not on_disk.get("trades"):
            problems.append("the closed trade did not reach the journal file")

        reread = load_journal()
        if len(reread["trades"]) != 1:
            problems.append(f"journal kept {len(reread['trades'])} trades, expected 1")
        if "trade_entry_quality_v9532" in reread or "some_removed_audit_blob" in reread:
            problems.append("legacy audit blobs came back on reload")
        # The signal a closed trade points at must not be FIFO-evicted.
        if not any(str(row.get("id")) == "s1" for row in reread["signals"]):
            problems.append("a signal referenced by a closed trade was evicted")
        if reread.get("journal_version") != JOURNAL_VERSION:
            problems.append(f"journal_version {reread.get('journal_version')}, expected {JOURNAL_VERSION}")

        # The audit funnel must also count executions in rows that predate v10's
        # `executed` flag: an inherited legacy signal records the same fact in `action`.
        reread["signals"].append({"id": "s0", "action": Action.PROBE_ENTRY.value, "time": iso_now()})
        reread["trades"].append({
            "id": "t0", "signal_id": "s0", "side": "LONG",
            "setup_type": SetupType.SWEEP_RECLAIM.value,
            "result_r": -0.4, "close_action": "STOP", "closed_at": iso_now(),
        })
        save_journal(reread)
        stored_legacy = next(
            (row for row in load_json(JOURNAL_FILE, {}).get("signals", [])
             if str(row.get("id")) == "s0"), {},
        )
        if "executed" in stored_legacy:
            problems.append("the legacy fixture gained an executed flag, so this test proves nothing")
        funnel = run_audit_journal(JOURNAL_FILE)["funnel"]
        if funnel["executed"] != 2:
            problems.append(
                f"audit funnel counted {funnel['executed']} executions, expected 2 — a legacy row "
                "whose action is an entry must count even without v10's executed flag"
            )
        if funnel["closed_trades"] != 2:
            problems.append(f"audit funnel saw {funnel['closed_trades']} closed trades, expected 2")
    finally:
        globals()["STATE_FILE"] = real_state
        globals()["JOURNAL_FILE"] = real_journal
        for name in os.listdir(workdir):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass
    return problems


def _check_analytics_and_degradation() -> list[str]:
    """Statistics must demote a losing setup and keep a proven one executable."""
    def trade_row(index: int, setup: str, result_r: float) -> dict[str, Any]:
        return {
            "id": f"t{index}", "signal_id": f"s{index}", "side": "LONG", "setup_type": setup,
            "setup_family": canonical_setup_family(setup), "result_r": result_r,
            "pnl_r": result_r, "realized_return_pct": result_r * 0.5,
            "outcome_status": "RESOLVED", "close_action": "TP1" if result_r > 0 else "STOP",
            "mfe_r": max(result_r, 0.0), "mae_r": max(-result_r, 0.0),
            "closed_at": iso_now(), "entry_stage": EntryStage.PROBE.value,
        }

    trades = [trade_row(i, SetupType.SWEEP_RECLAIM.value, -0.9) for i in range(12)]
    trades += [trade_row(100 + i, SetupType.FRESH_BASE_CONTINUATION.value, 1.6) for i in range(12)]
    # Supervision names this exit in management_state, not in close_action.
    for row in trades[:4]:
        row["management_state"] = "NO_FOLLOWTHROUGH_EXIT"
        row["close_action"] = "EXIT"
    journal = {"trades": trades, "signals": [], "training_signals": [],
               "signal_events": [], "preconfirmation_events": []}

    problems: list[str] = []
    analytics = compute_analytics(journal)
    if safe_int(analytics.get("trades")) != 24:
        problems.append(f"trades {analytics.get('trades')}, expected 24")
    if safe_int(analytics.get("unmeasured")) != 0:
        problems.append(f"{analytics.get('unmeasured')} trades were not measurable")
    if safe_int(analytics.get("wins")) != 12 or safe_int(analytics.get("losses")) != 12:
        problems.append(f"win/loss split {analytics.get('wins')}/{analytics.get('losses')}, expected 12/12")
    if abs(safe_float(analytics.get("win_rate")) - 0.5) > 1e-6:
        problems.append(f"win_rate {analytics.get('win_rate')}, expected 0.5")
    if abs(safe_float(analytics.get("net_r")) - (12 * 1.6 - 12 * 0.9)) > 1e-6:
        problems.append(f"net_r {analytics.get('net_r')}, expected {12 * 1.6 - 12 * 0.9:.4f}")

    table = compute_degradation_table(journal)
    loser = dict((table["setups"] or {}).get(SetupType.SWEEP_RECLAIM.value) or {})
    winner = dict((table["setups"] or {}).get(SetupType.FRESH_BASE_CONTINUATION.value) or {})
    if loser.get("executable", True):
        problems.append("a consistently losing setup is still executable — auto-degradation is dead")
    if not winner.get("executable", False):
        problems.append("a consistently winning setup was demoted")
    if SetupType.SWEEP_RECLAIM.value not in list(table.get("demoted") or []):
        problems.append(f"demoted list {table.get('demoted')} does not name the losing setup")
    if safe_float(loser.get("risk_multiplier"), 1.0) >= 1.0:
        problems.append("a demoted setup did not get a reduced risk multiplier")

    entry_audit = compute_entry_quality_audit(journal)
    for key in ("median_reaction_latency_minutes", "share_mfe_below_025r", "expectancy_by_entry_score"):
        if key not in entry_audit:
            problems.append(f"entry_quality_audit is missing {key}")
    share = entry_audit.get("no_followthrough_exit_share")
    if abs(safe_float(share) - 4.0 / 24.0) > 1e-4:
        problems.append(
            f"no_followthrough_exit_share {share}, expected {4.0 / 24.0:.4f} — "
            "the audit is not reading management_state, so the early-entry KPI is blind"
        )
    if entry_audit.get("no_followthrough_expectancy_r") is None:
        problems.append("no_followthrough_expectancy_r was not computed")
    learning = compute_learning_status(journal)
    if not str(learning.get("mode") or ""):
        problems.append("learning_status has no mode")
    if not isinstance(learning_health_warnings(learning, entry_audit), list):
        problems.append("learning_health_warnings did not return a list")
    return problems


def _check_messages() -> list[str]:
    """The no-entry message must exist and fit: it is what arrives every 15 minutes."""
    problems: list[str] = []
    # A far level says plainly that there is no entry; a level inside twice the
    # reaction zone says an entry is forming instead. Both must be sendable.
    for distance, expected in ((1.20, "немає"), (0.10, "наближається")):
        context, anchor, _ = _synthetic_context(Side.LONG.value, reaction=False, distance_atr=distance)
        audit = {
            "anchor_watch": _anchor_watch([anchor], context),
            # A SHORT hypothesis ranked above a LONG nearest anchor is the exact
            # contradiction the operator reported: two different selections, one message.
            "rejected_hypotheses": [{
                "side": Side.SHORT.value,
                "setup_type": SetupType.FRESH_BASE_CONTINUATION.value,
                "final_score": 66,
                "failed_gate": "GATE_PROXIMITY",
            }],
            "daily_risk": daily_risk_budget({"trades": []}, {"active_trade": None}, 0.0),
            "price_source": context.get("price_source"),
            "execution_price_trusted": True,
        }
        decision = Decision(
            id=new_id("sig"), time=iso_now(), action=Action.NO_SETUP.value,
            side=Side.NEUTRAL.value, setup_type=SetupType.NONE.value, quality=0,
            reason="NO_ANCHOR_WITH_CONFIRMED_3M_REACTION", regime=str(context.get("regime") or ""),
            audit=audit, current_price=safe_float(context.get("price")),
        )
        message = build_decision_message(context, decision)
        plain = plain_telegram_text(message)
        label = f"{distance:.2f} ATR"
        if not plain.strip():
            problems.append(f"the no-entry message at {label} is empty")
            continue
        if expected not in plain.lower():
            problems.append(f"the no-entry message at {label} does not mention '{expected}'")
        if len(message) > TELEGRAM_MAX_LENGTH:
            problems.append(f"message at {label} is {len(message)} chars, over {TELEGRAM_MAX_LENGTH}")
        for tag in ("<b>", "</b>", "<i>", "&lt;b&gt;"):
            if tag in plain:
                problems.append(f"plain_telegram_text left {tag} in the message at {label}")

        # The operator asked for these out of the every-15-minutes message: too much
        # text, and the hypothesis line contradicted the level the message was about.
        for removed in ("Режим:", "Сесія:", "Найсвіжіший anchor", "Anchor-и в пам'яті",
                        "Найближча гіпотеза", "Чому ні", "GATE_PROXIMITY"):
            if removed in plain:
                problems.append(f"the no-entry message at {label} still shows '{removed}'")
        if side_word(Side.SHORT.value) in plain:
            problems.append(
                f"the no-entry message at {label} names the SHORT hypothesis while the "
                "nearest level is LONG — the two selections must not both be shown"
            )

        record = build_signal_record(context, decision, None, audit)
        # index.html reads these to render a signal row; without them the dashboard
        # shows an empty card for every 15-minute cycle.
        for key in ("id", "time", "type", "action", "side", "setup_type", "reason",
                    "executed", "entry_level", "quality", "price", "context"):
            if key not in record:
                problems.append(f"signal record is missing {key}")
        if record.get("executed"):
            problems.append("a NO_SETUP signal record claims it executed")

    if TELEGRAM_MAX_LENGTH > 4096:
        problems.append(f"TELEGRAM_MAX_LENGTH {TELEGRAM_MAX_LENGTH} exceeds the Telegram API limit of 4096")
    if not SEND_NO_SETUP and not TELEGRAM_NOTIFY_EVERY_RUN:
        problems.append("with SEND_NO_SETUP and TELEGRAM_NOTIFY_EVERY_RUN both off, a no-entry run sends nothing")
    return problems


def _check_detectors_cover_taxonomy() -> list[str]:
    """All 24 setups must still be reachable through the anchor factory."""
    problems: list[str] = []
    canonical = set(CANONICAL_SETUP_FAMILY_MAP)
    if len(DETECTORS) != len(canonical):
        problems.append(f"{len(DETECTORS)} detectors for {len(canonical)} canonical setups")

    # Each detector names its own SetupType member, so the union of the compiled
    # names is proof that every setup still has a code path that can produce it.
    # A detector may reference the member by name (DIRECTION_FLIP) or the taxonomy
    # may key it by value (DIRECTION_FLIP_15M); either counts.
    referenced: set[str] = set()
    for detector in DETECTORS:
        if not callable(detector):
            problems.append(f"DETECTORS contains a non-callable: {detector!r}")
            continue
        referenced.update(getattr(detector, "__code__", None).co_names if hasattr(detector, "__code__") else ())
    member_names = {member.value: member.name for member in SetupType}
    missing = sorted(
        name for name in canonical
        if name not in referenced and member_names.get(name, "") not in referenced
    )
    if missing:
        problems.append(f"setups no detector can produce: {', '.join(missing)}")

    for setup in sorted(canonical):
        if not journal_setup_family(setup):
            problems.append(f"{setup} has no journal family label")

    for side in (Side.LONG.value, Side.SHORT.value):
        context, _, _ = _synthetic_context(side, reaction=False, distance_atr=1.20)
        try:
            anchors = detect_anchors(context)
        except Exception as exc:
            problems.append(f"detect_anchors raised on a {side} context: {type(exc).__name__}: {exc}")
            continue
        for anchor in anchors:
            if anchor.setup_type not in canonical:
                problems.append(f"detect_anchors produced {anchor.setup_type}, outside the taxonomy")
            if side_sign(anchor.side) * (anchor.invalidation - anchor.level) >= 0:
                problems.append(f"{anchor.setup_type}: invalidation is on the wrong side of the level")
    return problems


def _run_self_test() -> bool:
    checks: list[tuple[str, Any]] = [
        ("конфігурація", _check_configuration),
        ("таксономія сетапів", _check_detectors_cover_taxonomy),
        ("пізній вхід неможливий", _check_late_entry_is_impossible),
        ("ланцюжок входу", _check_entry_chain),
        ("супровід незмінний", _check_supervision_unchanged),
        ("PROBE no-followthrough", _check_probe_no_followthrough),
        ("життєвий цикл preconfirmation", _check_preconfirmation_lifecycle),
        ("персистенція та очистка легасі", _check_persistence_roundtrip),
        ("аналітика та авто-деградація", _check_analytics_and_degradation),
        ("повідомлення без входу", _check_messages),
    ]
    failed: list[str] = []
    print(f"SELF-TEST {BOT_VERSION} ({len(checks)} перевірок, офлайн)")
    for name, check in checks:
        try:
            problems = list(check() or [])
        except Exception as exc:
            problems = [f"{type(exc).__name__}: {exc}"]
        if problems:
            failed.append(name)
            print(f"  [FAIL] {name}")
            for row in problems:
                print(f"         - {row}")
        else:
            print(f"  [ OK ] {name}")
    print(f"SELF-TEST: {len(checks) - len(failed)}/{len(checks)} перевірок пройдено")
    if failed:
        print("SELF-TEST FAILED CHECKS:", ", ".join(failed))
    return not failed


# ==========================================================
# ENTRYPOINT
# ==========================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BZU Professional Oil 15M Signal Bot v10.0.0 ORGANIC — Journal v3"
    )
    parser.add_argument("--self-test", action="store_true",
                        help="Run the offline self-test and exit")
    parser.add_argument("--audit-journal", type=str,
                        help="Replay journal decisions without trading")
    args = parser.parse_args()

    report = validate_runtime_configuration()
    for warning in report["warnings"]:
        print(f"[WARN] {warning}", file=sys.stderr)
    if not report["passed"]:
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit("Runtime configuration invalid")

    if args.audit_journal:
        print(json.dumps(run_audit_journal(args.audit_journal), ensure_ascii=False, indent=2))
        return

    if args.self_test:
        if not _run_self_test():
            raise SystemExit(1)
        print("SELF-TEST PASSED")
        return

    raise SystemExit(run_bot())


if __name__ == "__main__":
    main()
