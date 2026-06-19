import html
import json
import math
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from copy import deepcopy
from enum import Enum
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from statistics import mean
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests


# ==========================================================
# BZU PROFESSIONAL SIGNAL BOT
# ==========================================================
BOT_VERSION = "pro-v3.12-no-clean-setup-canonicalization"
ARCHITECTURE_VERSION = "SINGLE_FILE_CLEAN_V3_12_NO_CLEAN_SETUP_CANONICALIZATION"

# Version upgrade: Single-File Clean Architecture V3 + deterministic decision pipeline.
# Entry package: Persistent Exhaustion / Shock Release 2.0 / Directional News
# Consensus / Strong ICT Override / Location Viability / Composite Exhaustion.
# Core idea:
# 1. Price action is the base.
# 2. News is fuel/filter, not a standalone trade.
# 3. If the bot gives an entry, the next runs must manage that trade until
#    HOLD / PROTECT / EXIT / TP / STOP. A trade is never replaced by WAIT.
# ==========================================================


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OKX_INST_ID = os.getenv("OKX_INST_ID", "BZ-USDT-SWAP")
STATE_FILE = os.getenv("SIGNAL_MEMORY_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "last_signal.json"))
JOURNAL_FILE = os.getenv("SIGNAL_JOURNAL_FILE", os.path.join(os.getenv("GITHUB_WORKSPACE", os.getcwd()), "signal_journal.json"))

LEVERAGE = float(os.getenv("POSITION_LEVERAGE", "10") or 10)
ENTRY_QUALITY_MIN = int(os.getenv("ENTRY_QUALITY_MIN", "68") or 68)
RISKY_QUALITY_MIN = int(os.getenv("RISKY_QUALITY_MIN", "62") or 62)
MAX_HISTORY = int(os.getenv("SIGNAL_HISTORY_LIMIT", "80") or 80)
MAX_JOURNAL = int(os.getenv("SIGNAL_JOURNAL_LIMIT", "500") or 500)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12") or 12)
UA_TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "UTC")

EIA_WPSR_URL = "https://www.eia.gov/petroleum/supply/weekly/index.php"
EIA_WPSR_SCHEDULE_URL = "https://www.eia.gov/petroleum/supply/weekly/schedule.php"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
IMPORTANT_EVENT_WINDOW_MINUTES = int(os.getenv("IMPORTANT_EVENT_WINDOW_MINUTES", "60") or 60)
MANUAL_IMPORTANT_EVENTS = os.getenv("IMPORTANT_EVENTS", "")

# BZU is volatile. TP1 must be a real 15M intraday objective, not a tiny
# 3M scalp created only by a close stop. These are baseline floors; the final
# distance is raised dynamically by regime, 15M ATR and the actual 1R stop.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "2.12") or 2.12)
MIN_TP1_DISTANCE_PCT = float(os.getenv("MIN_TP1_DISTANCE_PCT", "1.10") or 1.10)
MIN_TP2_DISTANCE_PCT = float(os.getenv("MIN_TP2_DISTANCE_PCT", "2.40") or 2.40)
MIN_TP3_DISTANCE_PCT = float(os.getenv("MIN_TP3_DISTANCE_PCT", "3.60") or 3.60)

# Professional 15M + ICT geometry target.
# 1R is the technical invalidation distance. TP1 must satisfy all three:
# 1) a real ICT/technical objective, 2) about 1.5R or better, and
# 3) enough absolute/15M-ATR travel to be meaningful for an intraday trade.
MIN_RR1_ENTRY = float(os.getenv("MIN_RR1_ENTRY", "1.00") or 1.00)
TARGET_RR1_ENTRY = float(os.getenv("TARGET_RR1_ENTRY", "1.50") or 1.50)
MIN_RR2_ENTRY = float(os.getenv("MIN_RR2_ENTRY", "2.20") or 2.20)
MIN_RR3_ENTRY = float(os.getenv("MIN_RR3_ENTRY", "3.00") or 3.00)
MIN_TP1_ATR_15M = float(os.getenv("MIN_TP1_ATR_15M", "1.50") or 1.50)
MIN_TP2_ATR_15M = float(os.getenv("MIN_TP2_ATR_15M", "2.20") or 2.20)
MIN_TP3_ATR_15M = float(os.getenv("MIN_TP3_ATR_15M", "3.10") or 3.10)
MIN_FULL_POSITION_STOP_ATR_15M = float(os.getenv("MIN_FULL_POSITION_STOP_ATR_15M", "0.32") or 0.32)
STRONG_BARRIER_PRIORITY = int(os.getenv("STRONG_BARRIER_PRIORITY", "94") or 94)
FAST_REVERSAL_MIN_SCORE = int(os.getenv("FAST_REVERSAL_MIN_SCORE", "68") or 68)
# The GitHub workflow runs once every 15 minutes. Scan the complete sequence of
# closed 3M candles formed between runs, so a sweep/reclaim or breakout/retest
# is not lost merely because the bot did not execute at the exact trigger minute.
ENTRY_RESCUE_SCAN_BARS_3M = int(os.getenv("ENTRY_RESCUE_SCAN_BARS_3M", "8") or 8)
ENTRY_RESCUE_MIN_SCORE = int(os.getenv("ENTRY_RESCUE_MIN_SCORE", "66") or 66)
ENTRY_RESCUE_MAX_EXTENSION_ATR15 = float(os.getenv("ENTRY_RESCUE_MAX_EXTENSION_ATR15", "0.95") or 0.95)
ENTRY_RESCUE_MAX_AGE_MINUTES = int(os.getenv("ENTRY_RESCUE_MAX_AGE_MINUTES", "24") or 24)
# Opportunity-preserving geometry. The 1.5R / full 15M travel values remain
# preferred, but a technically valid setup is not discarded merely because a
# nearby breakout gate sits before the main objective. Viable fallback geometry
# is still required to have meaningful full-position travel.
MIN_VIABLE_RR1_ENTRY = float(os.getenv("MIN_VIABLE_RR1_ENTRY", "1.25") or 1.25)
MIN_VIABLE_TP1_DISTANCE_PCT = float(os.getenv("MIN_VIABLE_TP1_DISTANCE_PCT", "0.95") or 0.95)
MIN_VIABLE_TP1_ATR_15M = float(os.getenv("MIN_VIABLE_TP1_ATR_15M", "1.15") or 1.15)
BREAKOUT_GATE_MAX_ATR15 = float(os.getenv("BREAKOUT_GATE_MAX_ATR15", "0.90") or 0.90)
BREAKOUT_GATE_MAX_DISTANCE_PCT = float(os.getenv("BREAKOUT_GATE_MAX_DISTANCE_PCT", "0.75") or 0.75)
# Balanced opportunity guard. A full-position 15M trade must not use a
# micro-stop hidden inside normal 3M noise, and a post-spike rejection must not
# be re-labelled as a fresh breakout merely because price pulled back from the high.
MIN_ROBUST_STOP_ATR_15M = float(os.getenv("MIN_ROBUST_STOP_ATR_15M", "0.55") or 0.55)
MIN_TACTICAL_STOP_ATR_15M = float(os.getenv("MIN_TACTICAL_STOP_ATR_15M", "0.45") or 0.45)
EXTREME_RR_SANITY_LIMIT = float(os.getenv("EXTREME_RR_SANITY_LIMIT", "8.0") or 8.0)
RR_SANITY_REBUILD_LIMIT = float(os.getenv("RR_SANITY_REBUILD_LIMIT", "5.0") or 5.0)
CONTINUATION_HYSTERESIS_CHECKS = int(os.getenv("CONTINUATION_HYSTERESIS_CHECKS", "2") or 2)
CONTINUATION_HYSTERESIS_MINUTES = float(os.getenv("CONTINUATION_HYSTERESIS_MINUTES", "35") or 35)
STRUCTURAL_RESET_MIN_15M_CANDLES = int(os.getenv("STRUCTURAL_RESET_MIN_15M_CANDLES", "2") or 2)
MFE_LOCK_TRIGGER_PCT = float(os.getenv("MFE_LOCK_TRIGGER_PCT", "1.0") or 1.0)
MFE_LOCK_GIVEBACK_RATIO = float(os.getenv("MFE_LOCK_GIVEBACK_RATIO", "0.35") or 0.35)
MFE_LOCK_EXIT_GIVEBACK_RATIO = float(os.getenv("MFE_LOCK_EXIT_GIVEBACK_RATIO", "0.55") or 0.55)
ABSOLUTE_RR_SANITY_LIMIT = float(os.getenv("ABSOLUTE_RR_SANITY_LIMIT", "12.0") or 12.0)
POST_IMPULSE_MIN_RUN_ATR15 = float(os.getenv("POST_IMPULSE_MIN_RUN_ATR15", "1.00") or 1.00)
POST_IMPULSE_MODERATE_PULLBACK_ATR15 = float(os.getenv("POST_IMPULSE_MODERATE_PULLBACK_ATR15", "0.50") or 0.50)
POST_IMPULSE_REJECTION_PULLBACK_ATR15 = float(os.getenv("POST_IMPULSE_REJECTION_PULLBACK_ATR15", "0.70") or 0.70)
POST_IMPULSE_REJECTION_BODY_ATR15 = float(os.getenv("POST_IMPULSE_REJECTION_BODY_ATR15", "0.42") or 0.42)
POST_IMPULSE_RECOVERY_MIN_SCORE = int(os.getenv("POST_IMPULSE_RECOVERY_MIN_SCORE", "70") or 70)
POST_IMPULSE_RECOVERY_MAX_EXTENSION_ATR15 = float(os.getenv("POST_IMPULSE_RECOVERY_MAX_EXTENSION_ATR15", "0.78") or 0.78)
# A post-rejection re-entry is a separate setup, not a resurrection of the old
# trigger. It needs a fresh closed-3M event, follow-through and renewed flow.
POST_REJECTION_STRICT_EVENT_SCORE = int(os.getenv("POST_REJECTION_STRICT_EVENT_SCORE", "76") or 76)
POST_REJECTION_MIN_FOLLOW_ATR3 = float(os.getenv("POST_REJECTION_MIN_FOLLOW_ATR3", "0.12") or 0.12)
POST_REJECTION_TRIGGER_HOLD_ATR3 = float(os.getenv("POST_REJECTION_TRIGGER_HOLD_ATR3", "0.10") or 0.10)
POST_REJECTION_MAX_OPPOSING_FAST_LAYERS = int(os.getenv("POST_REJECTION_MAX_OPPOSING_FAST_LAYERS", "1") or 1)
RECOVERY_ENTRY_VALIDATION_MINUTES = int(os.getenv("RECOVERY_ENTRY_VALIDATION_MINUTES", "45") or 45)
RECOVERY_ENTRY_WARNING_RISK_FRACTION = float(os.getenv("RECOVERY_ENTRY_WARNING_RISK_FRACTION", "0.38") or 0.38)
RECOVERY_ENTRY_EXIT_RISK_FRACTION = float(os.getenv("RECOVERY_ENTRY_EXIT_RISK_FRACTION", "0.52") or 0.52)

# Direction/transition package. These rules accelerate recognition of a genuine
# closed-15M direction shift, while requiring a retest after vertical shocks and
# a confirmed base after capitulation.
CLOSED_15M_DISPLACEMENT_MIN_ATR = float(os.getenv("CLOSED_15M_DISPLACEMENT_MIN_ATR", "1.20") or 1.20)
CLOSED_15M_DISPLACEMENT_MIN_BREAK_CLOSES = int(os.getenv("CLOSED_15M_DISPLACEMENT_MIN_BREAK_CLOSES", "2") or 2)
CLOSED_15M_DISPLACEMENT_MAX_EMA_EXTENSION_ATR = float(os.getenv("CLOSED_15M_DISPLACEMENT_MAX_EMA_EXTENSION_ATR", "1.75") or 1.75)
POST_SHOCK_MIN_RUN_ATR15 = float(os.getenv("POST_SHOCK_MIN_RUN_ATR15", "2.00") or 2.00)
POST_SHOCK_MIN_RUN_PCT = float(os.getenv("POST_SHOCK_MIN_RUN_PCT", "2.50") or 2.50)
POST_SHOCK_MIN_RETEST_ATR15 = float(os.getenv("POST_SHOCK_MIN_RETEST_ATR15", "0.35") or 0.35)
ROLLING_BALANCE_WINDOW = int(os.getenv("ROLLING_BALANCE_WINDOW", "10") or 10)
ROLLING_BALANCE_MIN_OVERLAP = float(os.getenv("ROLLING_BALANCE_MIN_OVERLAP", "0.55") or 0.55)
ROLLING_BALANCE_MAX_EFFICIENCY = float(os.getenv("ROLLING_BALANCE_MAX_EFFICIENCY", "0.30") or 0.30)
CAPITULATION_MIN_RUN_ATR15 = float(os.getenv("CAPITULATION_MIN_RUN_ATR15", "2.40") or 2.40)
CAPITULATION_MIN_RUN_PCT = float(os.getenv("CAPITULATION_MIN_RUN_PCT", "2.80") or 2.80)
CAPITULATION_MAX_AGE_15M_BARS = int(os.getenv("CAPITULATION_MAX_AGE_15M_BARS", "7") or 7)

# Final entry-consensus package. These settings make shock locks releasable,
# keep Prime ICT subordinate to regime/flow, and split balance into edge/mid/outside zones.
SHOCK_LOCK_RELEASE_CLOSES = int(os.getenv("SHOCK_LOCK_RELEASE_CLOSES", "2") or 2)
SHOCK_LOCK_MAX_15M_BARS = int(os.getenv("SHOCK_LOCK_MAX_15M_BARS", "10") or 10)
SHOCK_RELOCK_EXTENSION_ATR = float(os.getenv("SHOCK_RELOCK_EXTENSION_ATR", "0.80") or 0.80)
RANGE_EDGE_FRACTION = float(os.getenv("RANGE_EDGE_FRACTION", "0.18") or 0.18)
FAST_BRIDGE_MIN_DIRECTIONAL_MARGIN = int(os.getenv("FAST_BRIDGE_MIN_DIRECTIONAL_MARGIN", "20") or 20)
PRIME_ICT_MAX_ADVERSE_LAYERS = int(os.getenv("PRIME_ICT_MAX_ADVERSE_LAYERS", "1") or 1)
GOOD_RR1_BONUS = float(os.getenv("GOOD_RR1_BONUS", "1.50") or 1.50)
STRONG_RR1_BONUS = float(os.getenv("STRONG_RR1_BONUS", "2.00") or 2.00)

# Opportunity/recovery package: reopen only after a genuinely new base, allow
# confirmed transition entries through stale regime/range locks, and keep early
# continuation trades alive through the first normal pullback.
FRESH_BASE_MIN_15M_CANDLES = int(os.getenv("FRESH_BASE_MIN_15M_CANDLES", "4") or 4)
FRESH_BASE_MAX_15M_CANDLES = int(os.getenv("FRESH_BASE_MAX_15M_CANDLES", "10") or 10)
FRESH_BASE_MAX_WIDTH_ATR15 = float(os.getenv("FRESH_BASE_MAX_WIDTH_ATR15", "2.80") or 2.80)
FRESH_BASE_MIN_RETRACE_ATR15 = float(os.getenv("FRESH_BASE_MIN_RETRACE_ATR15", "0.22") or 0.22)
FRESH_BASE_MAX_EMA_EXTENSION_ATR15 = float(os.getenv("FRESH_BASE_MAX_EMA_EXTENSION_ATR15", "1.65") or 1.65)
EARLY_CONTINUATION_HOLD_CHECKS = int(os.getenv("EARLY_CONTINUATION_HOLD_CHECKS", "3") or 3)
EARLY_CONTINUATION_HOLD_MINUTES = float(os.getenv("EARLY_CONTINUATION_HOLD_MINUTES", "50") or 50)

# Opportunity Persistence & Tiered Entry.
OPPORTUNITY_MEMORY_DEFAULT_MINUTES = int(os.getenv("OPPORTUNITY_MEMORY_DEFAULT_MINUTES", "45") or 45)
OPPORTUNITY_MEMORY_FRESH_BASE_MINUTES = int(os.getenv("OPPORTUNITY_MEMORY_FRESH_BASE_MINUTES", "60") or 60)
OPPORTUNITY_MEMORY_RECOVERY_MINUTES = int(os.getenv("OPPORTUNITY_MEMORY_RECOVERY_MINUTES", "40") or 40)
OPPORTUNITY_MEMORY_MAX_EXTENSION_ATR15 = float(os.getenv("OPPORTUNITY_MEMORY_MAX_EXTENSION_ATR15", "0.72") or 0.72)
EARLY_DIRECTION_FLIP_MIN_ATR = float(os.getenv("EARLY_DIRECTION_FLIP_MIN_ATR", "0.90") or 0.90)
EARLY_DIRECTION_FLIP_MIN_SCORE = int(os.getenv("EARLY_DIRECTION_FLIP_MIN_SCORE", "66") or 66)
ADAPTIVE_FRESH_BASE_MIN_CANDLES = int(os.getenv("ADAPTIVE_FRESH_BASE_MIN_CANDLES", "3") or 3)
ADAPTIVE_FRESH_BASE_MAX_WIDTH_ATR15 = float(os.getenv("ADAPTIVE_FRESH_BASE_MAX_WIDTH_ATR15", "1.40") or 1.40)
AGED_3M_EVENT_MINUTES = int(os.getenv("AGED_3M_EVENT_MINUTES", "18") or 18)
AGED_3M_EVENT_MAX_EXTENSION_ATR15 = float(os.getenv("AGED_3M_EVENT_MAX_EXTENSION_ATR15", "0.70") or 0.70)
ALTERNATIVE_GEOMETRY_MIN_RR1 = float(os.getenv("ALTERNATIVE_GEOMETRY_MIN_RR1", "1.20") or 1.20)
ALTERNATIVE_GEOMETRY_MIN_ATR15 = float(os.getenv("ALTERNATIVE_GEOMETRY_MIN_ATR15", "1.00") or 1.00)
ALTERNATIVE_GEOMETRY_MIN_PCT = float(os.getenv("ALTERNATIVE_GEOMETRY_MIN_PCT", "0.85") or 0.85)

# State Transition Memory Reset package.
FRESH_BASE_EXHAUSTION_MIN_CANDLES = int(os.getenv("FRESH_BASE_EXHAUSTION_MIN_CANDLES", "6") or 6)
FRESH_BASE_EXHAUSTION_MAX_CANDLES = int(os.getenv("FRESH_BASE_EXHAUSTION_MAX_CANDLES", "12") or 12)
FRESH_BASE_EXHAUSTION_MAX_WIDTH_ATR15 = float(os.getenv("FRESH_BASE_EXHAUSTION_MAX_WIDTH_ATR15", "3.20") or 3.20)
RANGE_BOUNDARY_ROLLOVER_HOLD_BARS = int(os.getenv("RANGE_BOUNDARY_ROLLOVER_HOLD_BARS", "8") or 8)
PRE_GATE_MEMORY_MIN_QUALITY = int(os.getenv("PRE_GATE_MEMORY_MIN_QUALITY", "57") or 57)
RECOVERY_EXPIRY_MIN_FAST_SCORE = int(os.getenv("RECOVERY_EXPIRY_MIN_FAST_SCORE", "8") or 8)

# Entry Consensus Guard Package.
# These are not global anti-entry filters. They become hard only when several
# independent signs agree that the market location is exhausted/late and no new
# professional base, retest or ICT event has rebuilt the entry thesis.
PERSISTENT_EXHAUSTION_RELOCK_ATR15 = float(os.getenv("PERSISTENT_EXHAUSTION_RELOCK_ATR15", "0.80") or 0.80)
SHOCK_RELEASE_MIN_CONFIRMATIONS = int(os.getenv("SHOCK_RELEASE_MIN_CONFIRMATIONS", "2") or 2)
SHOCK_RELEASE_FLOW_AGAINST_SCORE = int(os.getenv("SHOCK_RELEASE_FLOW_AGAINST_SCORE", "14") or 14)
SHOCK_RELEASE_CVD_AGAINST_SCORE = int(os.getenv("SHOCK_RELEASE_CVD_AGAINST_SCORE", "16") or 16)
NEWS_DIRECTIONAL_MIN_SCORE = int(os.getenv("NEWS_DIRECTIONAL_MIN_SCORE", "35") or 35)
NEWS_OPPOSING_BLOCK_SCORE = int(os.getenv("NEWS_OPPOSING_BLOCK_SCORE", "35") or 35)
# News Reaction Lifecycle. A headline is a temporary catalyst, not a permanent
# veto. During the initial window it can block the opposite NEWS_IMPULSE. After
# that window the market reaction becomes the authority: only accepted price
# action keeps the directional block alive; absorbed/rejected news is context only.
NEWS_REACTION_STANDARD_HARD_MINUTES = int(os.getenv("NEWS_REACTION_STANDARD_HARD_MINUTES", "30") or 30)
NEWS_REACTION_STRUCTURAL_HARD_MINUTES = int(os.getenv("NEWS_REACTION_STRUCTURAL_HARD_MINUTES", "45") or 45)
NEWS_REACTION_MIN_ACCEPTANCE_CLOSES = int(os.getenv("NEWS_REACTION_MIN_ACCEPTANCE_CLOSES", "2") or 2)
NEWS_REACTION_CONFIRM_ATR15 = float(os.getenv("NEWS_REACTION_CONFIRM_ATR15", "0.30") or 0.30)
NEWS_REACTION_REJECT_ATR15 = float(os.getenv("NEWS_REACTION_REJECT_ATR15", "0.24") or 0.24)
NEWS_REACTION_MIN_MOVE_PCT = float(os.getenv("NEWS_REACTION_MIN_MOVE_PCT", "0.12") or 0.12)
NEWS_REACTION_DIGESTED_GIVEBACK = float(os.getenv("NEWS_REACTION_DIGESTED_GIVEBACK", "0.62") or 0.62)
NEWS_REACTION_MEMORY_HOURS = int(os.getenv("NEWS_REACTION_MEMORY_HOURS", "12") or 12)
NEWS_REACTION_UNVERIFIED_FACTOR = float(os.getenv("NEWS_REACTION_UNVERIFIED_FACTOR", "0.28") or 0.28)
COMPOSITE_EXHAUSTION_BLOCK_COUNT = int(os.getenv("COMPOSITE_EXHAUSTION_BLOCK_COUNT", "3") or 3)
LOCATION_MAX_EMA_EXTENSION_ATR15 = float(os.getenv("LOCATION_MAX_EMA_EXTENSION_ATR15", "1.35") or 1.35)
LOCATION_VERTICAL_3M_PCT = float(os.getenv("LOCATION_VERTICAL_3M_PCT", "0.55") or 0.55)
LOCATION_MATURE_15M_MOVE_PCT = float(os.getenv("LOCATION_MATURE_15M_MOVE_PCT", "0.75") or 0.75)
LOCATION_LOW_PARTICIPATION_RATIO = float(os.getenv("LOCATION_LOW_PARTICIPATION_RATIO", "0.18") or 0.18)

# Lock Release Opportunity Bridge.
# A professional setup that was blocked by persistent exhaustion/shock or by
# missing geometry is preserved through the lock lifecycle. Releasing the lock
# never opens a trade by itself: a fresh 3M hold/BOS/retest and viable RR are
# still mandatory.
LOCK_RELEASE_BRIDGE_TTL_MINUTES = int(os.getenv("LOCK_RELEASE_BRIDGE_TTL_MINUTES", "105") or 105)
LOCK_RELEASE_BRIDGE_RELEASE_GRACE_MINUTES = int(os.getenv("LOCK_RELEASE_BRIDGE_RELEASE_GRACE_MINUTES", "45") or 45)
LOCK_RELEASE_BRIDGE_MIN_QUALITY = int(os.getenv("LOCK_RELEASE_BRIDGE_MIN_QUALITY", "57") or 57)
LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE = int(os.getenv("LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE", "64") or 64)
LOCK_RELEASE_BRIDGE_MAX_EXTENSION_ATR15 = float(os.getenv("LOCK_RELEASE_BRIDGE_MAX_EXTENSION_ATR15", "0.72") or 0.72)
LOCK_RELEASE_BRIDGE_MIN_RR1 = float(os.getenv("LOCK_RELEASE_BRIDGE_MIN_RR1", "1.20") or 1.20)
LOCK_RELEASE_BRIDGE_MIN_3M_SCORE = int(os.getenv("LOCK_RELEASE_BRIDGE_MIN_3M_SCORE", "18") or 18)
LOCK_RELEASE_BRIDGE_MAX_ADVERSE_LAYERS = int(os.getenv("LOCK_RELEASE_BRIDGE_MAX_ADVERSE_LAYERS", "1") or 1)
LOCK_RELEASE_BRIDGE_EVENT_MAX_AGE_MINUTES = int(os.getenv("LOCK_RELEASE_BRIDGE_EVENT_MAX_AGE_MINUTES", "24") or 24)

# Geometry Persistence & Missed Continuation Recovery.
# The package preserves a confirmed continuation when only stop/TP geometry is
# missing, retries real 15M/ICT invalidations on subsequent runs, and never
# converts a late move near the target into a chase entry.
GEOMETRY_PERSISTENCE_TTL_MINUTES = int(os.getenv("GEOMETRY_PERSISTENCE_TTL_MINUTES", "55") or 55)
GEOMETRY_ESCALATION_FAILURES = int(os.getenv("GEOMETRY_ESCALATION_FAILURES", "2") or 2)
GEOMETRY_RECOVERY_MIN_RR1 = float(os.getenv("GEOMETRY_RECOVERY_MIN_RR1", "1.20") or 1.20)
GEOMETRY_RECOVERY_MIN_ATR15 = float(os.getenv("GEOMETRY_RECOVERY_MIN_ATR15", "0.90") or 0.90)
GEOMETRY_RECOVERY_MIN_PCT = float(os.getenv("GEOMETRY_RECOVERY_MIN_PCT", "0.72") or 0.72)
GEOMETRY_RECOVERY_ESCALATED_MIN_RR1 = float(os.getenv("GEOMETRY_RECOVERY_ESCALATED_MIN_RR1", "1.10") or 1.10)
GEOMETRY_RECOVERY_ESCALATED_MIN_ATR15 = float(os.getenv("GEOMETRY_RECOVERY_ESCALATED_MIN_ATR15", "0.80") or 0.80)
GEOMETRY_RECOVERY_ESCALATED_MIN_PCT = float(os.getenv("GEOMETRY_RECOVERY_ESCALATED_MIN_PCT", "0.60") or 0.60)
GEOMETRY_RECOVERY_MAX_STOP_ATR15 = float(os.getenv("GEOMETRY_RECOVERY_MAX_STOP_ATR15", "2.40") or 2.40)
GEOMETRY_RECOVERY_MAX_EXTENSION_ATR15 = float(os.getenv("GEOMETRY_RECOVERY_MAX_EXTENSION_ATR15", "0.72") or 0.72)
PRIME_ICT_HTF_CONFLICT_QUALITY_CAP = int(os.getenv("PRIME_ICT_HTF_CONFLICT_QUALITY_CAP", "66") or 66)
GEOMETRY_PERSISTENCE_SETUP_TYPES = {
    "TREND_IGNITION_ENTRY", "TREND_CONTINUATION", "PULLBACK_CONTINUATION",
    "PULLBACK_CONTINUATION_FAST_ENTRY", "FRESH_BASE_CONTINUATION_REENTRY",
    "RANGE_COMPRESSION_BREAKOUT",
}
OPPORTUNITY_MEMORY_SETUP_TYPES = {
    "CLOSED_15M_DIRECTION_FLIP", "CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY",
    "TREND_IGNITION_ENTRY", "TREND_CONTINUATION", "PULLBACK_CONTINUATION",
    "PULLBACK_CONTINUATION_FAST_ENTRY", "RANGE_COMPRESSION_BREAKOUT", "SWEEP_REVERSAL",
    "SWEEP_RECLAIM_EARLY_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE",
}
TRANSITION_WHITELIST_REGIMES = {"REVERSAL_BUILDUP", "EXHAUSTION", "RANGE_COMPRESSION", "COMPRESSION", "PULLBACK", "NEWS_SHOCK"}
TRANSITION_PRIORITY_SETUPS = {"CLOSED_15M_DIRECTION_FLIP", "CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY"}
LOCK_RELEASE_BRIDGE_SETUP_TYPES = set(OPPORTUNITY_MEMORY_SETUP_TYPES) | {
    "COUNTERTREND_SCALP", "RANGE_EDGE_TRADE", "TREND_IGNITION_ENTRY",
    "NEWS_IMPULSE", "CAPITULATION_RECOVERY", "CLOSED_15M_DIRECTION_FLIP",
}

# Intraday filters
# Volume is a bonus only. Low volume must NOT block entries for BZ intraday,
# because compression often happens before the best impulse move.
VOLUME_ACTIVE_RATIO = float(os.getenv("VOLUME_ACTIVE_RATIO", "1.30") or 1.30)
VOLUME_STRONG_RATIO = float(os.getenv("VOLUME_STRONG_RATIO", "1.70") or 1.70)
CVD_STATE_MAX_AGE_MINUTES = int(os.getenv("CVD_STATE_MAX_AGE_MINUTES", "360") or 360)
CVD_MIN_FRESH_TRADES = int(os.getenv("CVD_MIN_FRESH_TRADES", "80") or 80)
CVD_LOW_CONFIDENCE_FACTOR = float(os.getenv("CVD_LOW_CONFIDENCE_FACTOR", "0.45") or 0.45)
CVD_MEDIUM_CONFIDENCE_FACTOR = float(os.getenv("CVD_MEDIUM_CONFIDENCE_FACTOR", "0.70") or 0.70)
ICT_FVG_MAX_AGE_HOURS = float(os.getenv("ICT_FVG_MAX_AGE_HOURS", "18") or 18)
ICT_OB_MAX_AGE_HOURS = float(os.getenv("ICT_OB_MAX_AGE_HOURS", "36") or 36)
PRICE_SOURCE_MAX_DIFF_PCT = float(os.getenv("PRICE_SOURCE_MAX_DIFF_PCT", "0.18") or 0.18)
LOW_LIQUIDITY_VOLUME_RATIO = float(os.getenv("LOW_LIQUIDITY_VOLUME_RATIO", "0.35") or 0.35)
# Reuters is priority, but not the only source. EIA/Fed/OPEC/event headlines remain useful.
REUTERS_PRIORITY_NEWS = os.getenv("REUTERS_PRIORITY_NEWS", "1").lower() not in ["0", "false", "no"]


NEWS_QUERIES = [
    "Brent crude oil Reuters OPEC EIA inventories sanctions Hormuz",
    "oil prices Brent crude today Reuters energy",
    "EIA crude oil inventories API crude draw build",
    "OPEC production cut increase crude oil",
    "Iran sanctions Hormuz oil supply disruption",
    "Fed Powell dollar yields oil prices",
]

LONG_NEWS_PHRASES = [
    "inventory draw", "crude draw", "stockpiles fell", "stockpiles decline",
    "inventories fell", "inventories decline", "crude inventories fell",
    "crude inventories decline", "oil inventories fell", "oil inventories decline",
    "supply disruption", "supply risk", "supply outage",
    "hormuz closure", "hormuz disruption", "hormuz tensions", "hormuz risk",
    "strait closed", "shipping halted", "shipping disrupted",
    "new sanctions", "fresh sanctions", "opec cut", "production cut", "output cut",
    "attack on oil", "attack on tanker", "attack on refinery", "war escalates", "talks fail",
    "запаси впали", "скорочення запасів", "нові санкції", "атака на нафтов",
    "удар по нафтов", "закриття ормуз", "перебої постачання",
]

SHORT_NEWS_PHRASES = [
    "inventory build", "crude build", "stockpiles rose", "stockpiles rise",
    "inventories rose", "inventories rise", "crude inventories rose",
    "crude inventories rise", "oil inventories rose", "oil inventories rise",
    "ceasefire", "cease-fire", "truce", "peace deal", "talks progress",
    "sanctions relief", "sanctions lifted", "output increase",
    "production increase", "opec increase", "demand weak", "oversupply",
    "oil surplus", "crude surplus", "significant surplus",
    "hormuz recovery", "hormuz reopens", "strait reopens",
    "shipping resumes", "shipping restored", "supply resumes", "exports resume",
    "запаси зросли", "припинення вогню", "перемир", "мирна угода",
    "послаблення санкцій", "збільшення видобутку", "слабкий попит",
    "профіцит нафти", "відновлення ормуз", "постачання відновлено",
]

HIGH_IMPACT_WORDS = [
    "reuters", "eia", "api", "opec", "opec+", "fed", "powell", "cpi",
    "fomc", "iran", "hormuz", "sanctions", "inventory", "stockpiles",
    "brent", "crude", "oil", "tariff", "trump",
]


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    # OKX row[8]: 1 = candle is closed, 0 = candle is still forming.
    # Defaults keep backward compatibility with older saved/tests candles.
    confirmed: bool = True
    confirm_known: bool = False


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
    invalidation: str
    # Internal audit fields. They are written to the journal/state when useful,
    # but do not add any new lines to Telegram messages.
    valid: bool = True
    validation_reason: str = ""
    technical_stop_basis: str = ""
    technical_tp1_basis: str = ""
    technical_tp2_basis: str = ""
    technical_tp3_basis: str = ""
    geometry_mode: str = ""
    technical_rr_target: float = 1.50
    better_entry: float = 0.0
    # Internal only: Telegram formatting remains unchanged.
    geometry_grade: str = "OPTIMAL"
    barrier_mode: str = "DIRECT"
    preferred_geometry_met: bool = True
    breakout_gates: list = field(default_factory=list)


@dataclass
class ActiveTrade:
    id: str
    side: str
    opened_at: str
    entry: float
    stop_initial: float
    stop_current: float
    tp1: float
    tp2: float
    tp3: float
    quality: int
    stop_updated_at: str = ""
    status: str = "OPEN"
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    # Stop-management locks. After TP1 the first protective stop is fixed and
    # must not be recalculated on every 15m run. It can be changed only after
    # TP2 or after a hard exit/close decision.
    tp1_stop_locked: bool = False
    tp2_stop_locked: bool = False
    tp1_locked_stop: float = 0.0
    tp2_locked_stop: float = 0.0
    best_price: float = 0.0
    last_action: str = "OPEN"
    last_message_key: str = ""
    # Entry point lifecycle: used by supervision to decide whether the original
    # entry idea is still alive, weak, recovered, or broken. This prevents noisy
    # contradictions like "entry not confirmed" while risk still looks moderate.
    entry_integrity_score: int = 100
    entry_fail_streak: int = 0
    entry_recovery_checks: int = 0
    entry_last_state: str = "СИЛЬНА"
    # Adaptive MFE Giveback Guard 2.0 memory. It requires repeated confirmation
    # before closing on profit giveback, so one noisy 3M candle does not kick us
    # out of a still-valid trend.
    mfe_giveback_streak: int = 0
    mfe_giveback_last_state: str = "OK"
    # Direct identity fields. Older states are migrated from notes.
    setup_type: str = ""
    regime_type: str = ""
    entry_level: str = ""
    # Professional lifecycle memory. The trade is managed as one scenario,
    # not re-invented from scratch on every 15-minute run.
    lifecycle_stage: str = "ENTRY_VALIDATION"
    lifecycle_score: int = 50
    lifecycle_confirmations: int = 0
    lifecycle_failures: int = 0
    lifecycle_last_transition: str = ""
    # Two-level stop architecture:
    # structural_stop = objective invalidation of the setup;
    # profit_stop = dynamic protection after MFE/TP/confirmed pressure.
    structural_stop: float = 0.0
    profit_stop: float = 0.0
    active_stop_source: str = "STRUCTURAL"
    # Post-rejection recovery identity. Such entries must prove themselves fast
    # and are supervised by the trigger that created the *new* setup.
    recovery_after_rejection: bool = False
    recovery_trigger_ts: int = 0
    recovery_trigger_level: float = 0.0
    recovery_event_type: str = ""
    recovery_entry_checks: int = 0
    # Full transition patch suite memory.
    management_checks: int = 0
    mfe_profit_lock_streak: int = 0
    mfe_profit_lock_active: bool = False
    notes: list = field(default_factory=list)


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def eastern_tz():
    return ZoneInfo("America/New_York")


def et_to_utc(date_obj, hour, minute=0):
    return datetime(date_obj.year, date_obj.month, date_obj.day, hour, minute, tzinfo=eastern_tz()).astimezone(timezone.utc)


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, lo=0, hi=100):
    """Clamp numeric value into [lo, hi].

    Used by risk/MFE engines. Kept global so helper functions defined before
    assess_trade_risk can call it safely in GitHub Actions.
    """
    try:
        return max(lo, min(hi, value))
    except Exception:
        return lo


def round_price(value):
    if value is None:
        return None
    return round(float(value), 4)


def pct(a, b):
    if not b:
        return 0.0
    return (a - b) / b * 100.0


def signed_pct(side, entry, price):
    raw = pct(price, entry)
    return raw if side == "LONG" else -raw


def side_word(side):
    if side == "LONG":
        return "лонг"
    if side == "SHORT":
        return "шорт"
    return "нейтрально"


def opposite(side):
    return "SHORT" if side == "LONG" else "LONG"


def http_get(url, timeout=REQUEST_TIMEOUT, retries=2):
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Signal-Bot/2.0",
        "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html, */*",
    }
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code < 400:
                return response
            last_error = f"HTTP {response.status_code}"
        except Exception as error:
            last_error = str(error)
        time.sleep(0.4 * attempt)
    print(f"[WARN] GET failed: {url} | {last_error}")
    return None


def http_post(url, payload, timeout=REQUEST_TIMEOUT):
    headers = {
        "User-Agent": "Mozilla/5.0 BZU-Signal-Bot/2.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code < 400:
            return response
        print(f"[WARN] POST HTTP {response.status_code}: {url}")
    except Exception as error:
        print(f"[WARN] POST failed: {url} | {error}")
    return None


# ==========================================================
# STATE
# ==========================================================


def make_json_safe(value):
    """Recursively convert bot state/journal values to JSON-safe types.

    Some market/calendar/news helpers can leave datetime objects inside the
    context saved to signal_journal.json. GitHub Actions then crashes on
    json.dump(). This sanitizer keeps the journal useful and prevents one
    non-serializable value from killing the bot run.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return str(value)


def atomic_json_write(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    bak = path + ".bak"
    safe_data = make_json_safe(data)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with open(bak, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception as error:
        print(f"[WARN] JSON read failed {path}: {error}")
        return default


def load_state():
    state = load_json(STATE_FILE, {"version": "pro-v2", "active_trade": None, "history": []})
    if "active_trade" not in state:
        state["active_trade"] = None
    if "history" not in state or not isinstance(state["history"], list):
        state["history"] = []
    state["version"] = BOT_VERSION
    return state


def save_state(state):
    state["updated_at"] = iso_now()
    state["history"] = (state.get("history") or [])[-MAX_HISTORY:]
    atomic_json_write(STATE_FILE, state)


def load_journal():
    journal = load_json(JOURNAL_FILE, {"version": "pro-v2", "trades": [], "signals": []})
    journal["version"] = BOT_VERSION
    if "trades" not in journal or not isinstance(journal["trades"], list):
        journal["trades"] = []
    if "signals" not in journal or not isinstance(journal["signals"], list):
        journal["signals"] = []
    return journal


def save_journal(journal):
    journal["updated_at"] = iso_now()
    journal["trades"] = (journal.get("trades") or [])[-MAX_JOURNAL:]
    journal["signals"] = (journal.get("signals") or [])[-MAX_JOURNAL:]
    update_outcome_analytics(journal)
    atomic_json_write(JOURNAL_FILE, journal)


def _state_note_value(notes, prefix):
    prefix = str(prefix)
    for item in notes or []:
        value = str(item)
        if value.startswith(prefix + ":"):
            return value.split(":", 1)[1].strip()
    return ""


def active_trade_from_state(state):
    raw = (state or {}).get("active_trade")
    if not isinstance(raw, dict):
        return None
    try:
        return ActiveTrade(
            id=str(raw.get("id") or uuid.uuid4().hex[:10]),
            side=str(raw.get("side")),
            opened_at=str(raw.get("opened_at") or iso_now()),
            entry=float(raw.get("entry")),
            stop_initial=float(raw.get("stop_initial", raw.get("stop_current"))),
            stop_current=float(raw.get("stop_current", raw.get("stop_initial"))),
            tp1=float(raw.get("tp1")),
            tp2=float(raw.get("tp2")),
            tp3=float(raw.get("tp3")),
            quality=int(raw.get("quality") or 0),
            stop_updated_at=str(raw.get("stop_updated_at") or raw.get("last_stop_update") or raw.get("opened_at") or iso_now()),
            status=str(raw.get("status") or "OPEN"),
            tp1_hit=bool(raw.get("tp1_hit")),
            tp2_hit=bool(raw.get("tp2_hit")),
            tp3_hit=bool(raw.get("tp3_hit")),
            tp1_stop_locked=bool(raw.get("tp1_stop_locked", False)),
            tp2_stop_locked=bool(raw.get("tp2_stop_locked", False)),
            tp1_locked_stop=float(raw.get("tp1_locked_stop") or 0),
            tp2_locked_stop=float(raw.get("tp2_locked_stop") or 0),
            best_price=float(raw.get("best_price") or raw.get("entry")),
            last_action=str(raw.get("last_action") or "OPEN"),
            last_message_key=str(raw.get("last_message_key") or ""),
            entry_integrity_score=int(raw.get("entry_integrity_score", 100) or 100),
            entry_fail_streak=int(raw.get("entry_fail_streak", 0) or 0),
            entry_recovery_checks=int(raw.get("entry_recovery_checks", 0) or 0),
            entry_last_state=str(raw.get("entry_last_state") or "СИЛЬНА"),
            mfe_giveback_streak=int(raw.get("mfe_giveback_streak", 0) or 0),
            mfe_giveback_last_state=str(raw.get("mfe_giveback_last_state") or "OK"),
            setup_type=str(raw.get("setup_type") or _state_note_value(raw.get("notes") or [], "SETUP_CLASSIFIER")),
            regime_type=str(raw.get("regime_type") or _state_note_value(raw.get("notes") or [], "REGIME_TYPE")),
            entry_level=str(raw.get("entry_level") or _state_note_value(raw.get("notes") or [], "ENTRY_LEVEL")),
            lifecycle_stage=str(raw.get("lifecycle_stage") or ("EARLY_VALIDATION" if str(raw.get("entry_level") or _state_note_value(raw.get("notes") or [], "ENTRY_LEVEL")).upper() == "RISKY_ENTRY" else "ENTRY_VALIDATION")),
            lifecycle_score=int(raw.get("lifecycle_score", 50) or 50),
            lifecycle_confirmations=int(raw.get("lifecycle_confirmations", 0) or 0),
            lifecycle_failures=int(raw.get("lifecycle_failures", 0) or 0),
            lifecycle_last_transition=str(raw.get("lifecycle_last_transition") or raw.get("opened_at") or iso_now()),
            structural_stop=float(raw.get("structural_stop") or raw.get("stop_initial") or raw.get("stop_current")),
            # Preserve a previously tightened legacy stop as the profit layer.
            profit_stop=float(raw.get("profit_stop") or raw.get("stop_current") or 0),
            active_stop_source=str(raw.get("active_stop_source") or "MIGRATED"),
            recovery_after_rejection=bool(raw.get("recovery_after_rejection", False)),
            recovery_trigger_ts=int(raw.get("recovery_trigger_ts", 0) or 0),
            recovery_trigger_level=float(raw.get("recovery_trigger_level", 0) or 0),
            recovery_event_type=str(raw.get("recovery_event_type") or ""),
            recovery_entry_checks=int(raw.get("recovery_entry_checks", 0) or 0),
            management_checks=int(raw.get("management_checks", 0) or 0),
            mfe_profit_lock_streak=int(raw.get("mfe_profit_lock_streak", 0) or 0),
            mfe_profit_lock_active=bool(raw.get("mfe_profit_lock_active", False)),
            notes=list(raw.get("notes") or []),
        )
    except Exception as error:
        print(f"[WARN] active trade migration failed: {error}")
        return None


def store_active_trade(state, trade):
    state["active_trade"] = asdict(trade) if trade else None


def append_history(state, item):
    history = state.get("history") or []
    item["time"] = iso_now()
    history.append(item)
    state["history"] = history[-MAX_HISTORY:]


# ==========================================================
# MARKET DATA
# ==========================================================


def parse_okx_candles(rows):
    candles = []
    for row in rows or []:
        try:
            confirm_known = len(row) > 8 and str(row[8]) in ["0", "1"]
            confirmed = str(row[8]) == "1" if confirm_known else True
            candles.append(Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5] or 0),
                confirmed=confirmed,
                confirm_known=confirm_known,
            ))
        except Exception:
            continue
    candles.sort(key=lambda c: c.ts)
    return candles


BAR_MINUTES = {"3m": 3, "15m": 15, "1h": 60, "4h": 240}


def _candle_closed_by_clock(candle, bar_minutes, now_ms=None):
    """Clock fallback for feeds/tests that do not expose OKX confirm."""
    if candle is None:
        return False
    now_ms = int(now_ms or now_utc().timestamp() * 1000)
    return int(candle.ts) + int(bar_minutes * 60 * 1000) <= now_ms - 1500


def candle_is_closed(candle, bar_minutes, now_ms=None):
    if candle is None:
        return False
    if bool(getattr(candle, "confirm_known", False)):
        # A delayed API flag must not keep an already elapsed candle live forever.
        return bool(getattr(candle, "confirmed", False)) or _candle_closed_by_clock(candle, bar_minutes, now_ms)
    return bool(getattr(candle, "confirmed", True)) and _candle_closed_by_clock(candle, bar_minutes, now_ms)


def closed_candles(candles, bar_minutes, min_required=1):
    """Return only completed candles; safely fall back for legacy data."""
    candles = list(candles or [])
    if not candles:
        return []
    closed = [c for c in candles if candle_is_closed(c, bar_minutes)]
    if len(closed) >= min_required:
        return closed
    # Legacy/manual arrays may not carry real timestamps. Preserve old behavior
    # rather than making the whole model unavailable.
    if not any(bool(getattr(c, "confirm_known", False)) for c in candles):
        return candles
    return closed


def _live_candle_snapshot(candles, bar_minutes, atr_reference=None):
    candles = list(candles or [])
    if not candles:
        return {"active": False}
    live = None
    for candle in reversed(candles):
        if not candle_is_closed(candle, bar_minutes):
            live = candle
            break
    if live is None:
        return {"active": False}
    age_min = max(0.0, (now_utc().timestamp() * 1000 - int(live.ts)) / 60000.0)
    move_pct = pct(live.close, live.open) if live.open else 0.0
    atr_reference = safe_float(atr_reference, None)
    body_atr = abs(live.close - live.open) / atr_reference if atr_reference else 0.0
    direction = "LONG" if move_pct > 0.05 else ("SHORT" if move_pct < -0.05 else "NEUTRAL")
    return {
        "active": True,
        "ts": int(live.ts),
        "age_min": round(age_min, 2),
        "open": round_price(live.open),
        "close": round_price(live.close),
        "high": round_price(live.high),
        "low": round_price(live.low),
        "move_pct": round(move_pct, 3),
        "body_atr": round(body_atr, 3),
        "bias": direction,
    }


def build_closed_candle_mtf_guard(data, closed_15m, closed_1h, closed_4h):
    """Metadata layer: closed candles drive decisions; live HTF is warning only."""
    atr15_ref = atr(closed_15m, 14) if len(closed_15m) >= 2 else None
    atr1h_ref = atr(closed_1h, 14) if len(closed_1h) >= 2 else None
    atr4h_ref = atr(closed_4h, 14) if len(closed_4h) >= 2 else None
    live15 = _live_candle_snapshot(data.get("candles_15m") or [], 15, atr15_ref)
    live1h = _live_candle_snapshot(data.get("candles_1h") or [], 60, atr1h_ref)
    live4h = _live_candle_snapshot(data.get("candles_4h") or [], 240, atr4h_ref)
    return {
        "active": True,
        "closed_15m_count": len(closed_15m),
        "closed_1h_count": len(closed_1h),
        "closed_4h_count": len(closed_4h),
        "live_15m": live15,
        "live_1h": live1h,
        "live_4h": live4h,
        # No time pause. These flags only identify a noisy boundary window.
        "new_1h_boundary": bool(live1h.get("active") and safe_float(live1h.get("age_min"), 99) <= 15),
        "new_4h_boundary": bool(live4h.get("active") and safe_float(live4h.get("age_min"), 99) <= 20),
        "policy": "CLOSED_15M_1H_4H_FOR_STRUCTURE; LIVE_HTF_WARNING_ONLY",
    }



# ==========================================================
# DUAL-SPEED MTF PROFESSIONAL ENGINE
# ==========================================================


def _directional_vote(block, side, min_score=0, weight=1.0):
    """Return support/against vote from a normalized analysis block."""
    if side not in ["LONG", "SHORT"] or not isinstance(block, dict):
        return 0.0, 0.0
    bias = str(block.get("bias") or "NEUTRAL").upper()
    score = abs(safe_float(block.get("score"), 0.0) or 0.0)
    if min_score and score < min_score:
        return 0.0, 0.0
    if bias == side:
        return float(weight), 0.0
    if bias == opposite(side):
        return 0.0, float(weight)
    return 0.0, 0.0




def _recent_closed_3m_scan(context, scan_bars=None):
    """Return enough closed 3M candles to reconstruct the interval between runs."""
    candles = list((context or {}).get("candles_3m") or [])
    if not candles:
        return []
    closed = closed_candles(candles, 3, min_required=4)
    if len(closed) < 8:
        return []
    scan_bars = max(4, int(scan_bars or ENTRY_RESCUE_SCAN_BARS_3M))
    # Keep a baseline before the 15-minute scan window. Six scan bars provide
    # an 18-minute overlap, preventing a boundary trigger from falling through.
    return closed[-(scan_bars + 10):]


def _event_directional_flow_score(context, side):
    score = 0
    evidence = []
    for key, threshold, points, label in [
        ("cvd", 12, 7, "CVD"),
        ("flow", 10, 6, "FLOW"),
        ("liquidity", 9, 4, "LIQUIDITY"),
        ("derivatives", 9, 3, "DERIVATIVES"),
    ]:
        block = (context or {}).get(key) or {}
        magnitude = abs(int(block.get("score", 0) or 0))
        if block.get("bias") == side and magnitude >= threshold:
            score += points
            evidence.append(label)
        elif block.get("bias") == opposite(side) and magnitude >= threshold + 4:
            score -= points
    return score, evidence




def _strict_post_rejection_recovery_event(side, context, event, rejection_ts=0, severity="STRONG"):
    """Validate a genuinely new setup after a post-impulse rejection.

    An ICT location or two green/red candles alone are not enough. The new event
    must occur after rejection, hold its trigger on closed 3M candles, show
    follow-through and regain at least one independent flow layer. This gate is
    active only after rejection, so it cannot suppress the original early entry.
    """
    context = context or {}
    event = event or {}
    side = str(side or "").upper()
    if side not in ["LONG", "SHORT"] or not event.get("confirmed"):
        return {"passed": False, "reason": "немає нового підтвердженого 3M сетапу"}
    event_ts = int(event.get("trigger_ts", 0) or 0)
    if rejection_ts and event_ts <= int(rejection_ts):
        return {"passed": False, "reason": "3M тригер сформувався до відхилення і вже неактуальний"}
    event_type = str(event.get("type") or "")
    if event_type not in ["SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"]:
        return {"passed": False, "reason": "після відхилення потрібен sweep/reclaim, breakout-retest або ICT-zone reclaim"}
    if not event.get("anchor_confirmed") or not event.get("professional_location"):
        return {"passed": False, "reason": "новий 3M тригер не має 15M/ICT опори"}

    score_floor = POST_REJECTION_STRICT_EVENT_SCORE if str(severity).upper() == "STRONG" else POST_IMPULSE_RECOVERY_MIN_SCORE
    event_score = int(event.get("score", 0) or 0)
    if event_score < score_floor:
        return {"passed": False, "reason": f"сила нового 3M сетапу {event_score} нижча потрібної {score_floor}"}
    if safe_float(event.get("extension_atr15"), 99) > min(POST_IMPULSE_RECOVERY_MAX_EXTENSION_ATR15, 0.72):
        return {"passed": False, "reason": "ціна вже надто далеко відійшла від нового 3M тригера"}

    closed = closed_candles(list(context.get("candles_3m") or []), 3, min_required=4)
    closed = closed[-12:]
    post = [c for c in closed if int(c.ts) > event_ts]
    if not post:
        return {"passed": False, "reason": "після нового 3M тригера ще немає закритої свічки підтвердження"}
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None)
    price = safe_float(context.get("price"), 0) or 0
    atr15 = atr15 or max(price * 0.006, 0.01)
    atr3 = safe_float(atr(closed, 14), atr15 * 0.32) or atr15 * 0.32
    trigger_level = safe_float(event.get("trigger_level"), safe_float(event.get("trigger_close"), price))
    trigger_close = safe_float(event.get("trigger_close"), trigger_level)
    last = post[-1]

    if side == "LONG":
        trigger_hold = bool(last.close >= trigger_level - atr3 * POST_REJECTION_TRIGGER_HOLD_ATR3)
        follow_through = any(
            c.close >= max(trigger_level, trigger_close) + atr3 * POST_REJECTION_MIN_FOLLOW_ATR3
            and close_location(c) >= 0.52 for c in post
        )
        structure_rebuilt = bool(
            min(c.low for c in post) > safe_float(event.get("stop_level"), trigger_level - atr3) + atr3 * 0.12
            and last.close > trigger_level
        )
    else:
        trigger_hold = bool(last.close <= trigger_level + atr3 * POST_REJECTION_TRIGGER_HOLD_ATR3)
        follow_through = any(
            c.close <= min(trigger_level, trigger_close) - atr3 * POST_REJECTION_MIN_FOLLOW_ATR3
            and close_location(c) <= 0.48 for c in post
        )
        structure_rebuilt = bool(
            max(c.high for c in post) < safe_float(event.get("stop_level"), trigger_level + atr3) - atr3 * 0.12
            and last.close < trigger_level
        )

    tf3 = context.get("tf3") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    liquidity = context.get("liquidity") or {}
    clusters = context.get("clusters") or {}
    tf3_support = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22)
    flow_support_layers = sum([
        cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 12,
        flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10,
        liquidity.get("bias") == side and abs(int(liquidity.get("score", 0) or 0)) >= 9,
        clusters.get("bias") == side and abs(int(clusters.get("score", 0) or 0)) >= 5,
    ])
    opposing_layers = sum([
        tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 34,
        cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 16,
        flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 12,
        liquidity.get("bias") == opposite(side) and abs(int(liquidity.get("score", 0) or 0)) >= 10,
        clusters.get("bias") == opposite(side) and abs(int(clusters.get("score", 0) or 0)) >= 7,
    ])

    regime = context.get("regime_engine") or context.get("market_regime") or {}
    unstable_flip = bool(
        str(regime.get("regime_type") or "").upper() == "TREND_EXPANSION"
        and str(regime.get("previous_regime_type") or "").upper() in ["EXHAUSTION", "REVERSAL_BUILDUP", "NEWS_SHOCK"]
        and (not regime.get("is_stable") or int(regime.get("stable_count", 0) or 0) < 2)
    )
    flow_needed = 1
    if event_type == "ICT_ZONE_RECLAIM" or unstable_flip:
        flow_needed = 1
        if not tf3_support:
            return {"passed": False, "reason": "після відхилення ICT-зона без нового сильного 3M напряму не є входом"}

    passed = bool(
        trigger_hold and follow_through and structure_rebuilt
        and (tf3_support or event_score >= 82)
        and flow_support_layers >= flow_needed
        and opposing_layers <= POST_REJECTION_MAX_OPPOSING_FAST_LAYERS
    )
    reason = "новий post-rejection сетап підтверджено закритим 3M follow-through і потоком" if passed else (
        "після відхилення немає одночасно утримання рівня, 3M follow-through та відновлення потоку"
    )
    return {
        "passed": passed,
        "reason": reason,
        "event_type": event_type,
        "event_ts": event_ts,
        "trigger_level": round_price(trigger_level),
        "trigger_hold": trigger_hold,
        "follow_through": follow_through,
        "structure_rebuilt": structure_rebuilt,
        "tf3_support": tf3_support,
        "flow_support_layers": int(flow_support_layers),
        "opposing_layers": int(opposing_layers),
        "unstable_regime_flip": unstable_flip,
        "event_score": event_score,
    }


def post_impulse_rejection_snapshot(side, context):
    """Classify post-impulse behavior as MODERATE, STRONG or RECOVERED.

    A moderate rejection is caution, not a hard ban: it forces any new entry to
    remain RISKY and requires a fresh trigger. A strong rejection invalidates all
    triggers that occurred before the rejection. A new sweep/reclaim,
    breakout-retest or ICT-zone reclaim *after* the rejection can reopen the
    scenario as a new RISKY setup.
    """
    context = context or {}
    side = str(side or "").upper()
    price = safe_float(context.get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return {
            "active": False, "block_entry": False, "force_risky": False,
            "severity": "NONE", "reason": "", "rejection_ts": 0,
            "fresh_recovery": False, "invalidate_old_triggers": False,
        }

    candles = list(context.get("candles_3m") or [])
    closed = closed_candles(candles, 3, min_required=4) if candles else []
    closed = closed[-8:]
    if len(closed) < 5:
        return {
            "active": False, "block_entry": False, "force_risky": False,
            "severity": "NONE", "reason": "", "rejection_ts": 0,
            "fresh_recovery": False, "invalidate_old_triggers": False,
        }

    atr15 = safe_float(context.get("atr15"), None)
    if not atr15:
        atr15 = safe_float((context.get("tf15") or {}).get("atr"), price * 0.006) or price * 0.006
    atr15 = max(float(atr15), price * 0.0015)
    live15 = ((context.get("mtf_guard") or {}).get("live_15m") or {})

    if side == "LONG":
        peak_idx = max(range(len(closed)), key=lambda i: closed[i].high)
        peak = float(closed[peak_idx].high)
        pre_start = max(0, peak_idx - 4)
        pre_base = min(float(c.low) for c in closed[pre_start:peak_idx + 1])
        run_atr = max(0.0, (peak - pre_base) / atr15)
        pullback_atr = max(0.0, (peak - price) / atr15)
        after = closed[peak_idx + 1:]
        rejection_candles = [
            c for c in after
            if c.close < c.open
            and abs(c.close - c.open) / atr15 >= POST_IMPULSE_REJECTION_BODY_ATR15
            and close_location(c) <= 0.42
        ]
        live_rejection = bool(
            live15.get("active") and live15.get("bias") == "SHORT"
            and safe_float(live15.get("body_atr"), 0.0) >= 0.55
        )
        last2 = closed[-2:]
        two_candle_recovery = bool(
            len(after) >= 2
            and all(c.close > c.open for c in last2)
            and last2[-1].close > last2[-2].close
            and last2[-1].low >= min(c.low for c in after) + atr15 * 0.08
            and price >= peak - atr15 * 0.42
        )
    else:
        peak_idx = min(range(len(closed)), key=lambda i: closed[i].low)
        peak = float(closed[peak_idx].low)
        pre_start = max(0, peak_idx - 4)
        pre_base = max(float(c.high) for c in closed[pre_start:peak_idx + 1])
        run_atr = max(0.0, (pre_base - peak) / atr15)
        pullback_atr = max(0.0, (price - peak) / atr15)
        after = closed[peak_idx + 1:]
        rejection_candles = [
            c for c in after
            if c.close > c.open
            and abs(c.close - c.open) / atr15 >= POST_IMPULSE_REJECTION_BODY_ATR15
            and close_location(c) >= 0.58
        ]
        live_rejection = bool(
            live15.get("active") and live15.get("bias") == "LONG"
            and safe_float(live15.get("body_atr"), 0.0) >= 0.55
        )
        last2 = closed[-2:]
        two_candle_recovery = bool(
            len(after) >= 2
            and all(c.close < c.open for c in last2)
            and last2[-1].close < last2[-2].close
            and last2[-1].high <= max(c.high for c in after) - atr15 * 0.08
            and price <= peak + atr15 * 0.42
        )

    impulse_complete = bool(peak_idx <= len(closed) - 2 and run_atr >= POST_IMPULSE_MIN_RUN_ATR15)
    rejection = bool(rejection_candles or live_rejection)
    rejection_ts = max([int(c.ts) for c in rejection_candles] + ([int(live15.get("ts") or 0)] if live_rejection else [0]))

    opposite_side = opposite(side)
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    tf3 = context.get("tf3") or {}
    hard_against = bool(
        (structure.get("bias") == opposite_side and abs(int(structure.get("score", 0) or 0)) >= 18)
        or (ict.get("bias") == opposite_side and abs(int(ict.get("score", 0) or 0)) >= 18)
    )
    fast_against_layers = sum([
        tf3.get("bias") == opposite_side and abs(int(tf3.get("score", 0) or 0)) >= 34,
        (context.get("cvd") or {}).get("bias") == opposite_side and abs(int((context.get("cvd") or {}).get("score", 0) or 0)) >= 16,
        (context.get("flow") or {}).get("bias") == opposite_side and abs(int((context.get("flow") or {}).get("score", 0) or 0)) >= 12,
        (context.get("liquidity") or {}).get("bias") == opposite_side and abs(int((context.get("liquidity") or {}).get("score", 0) or 0)) >= 10,
    ])

    moderate = bool(
        impulse_complete and rejection
        and pullback_atr >= POST_IMPULSE_MODERATE_PULLBACK_ATR15
    )
    strong = bool(
        moderate
        and pullback_atr >= POST_IMPULSE_REJECTION_PULLBACK_ATR15
        and (
            hard_against
            or fast_against_layers >= 2
            or len(rejection_candles) >= 2
            or (live_rejection and safe_float(live15.get("body_atr"), 0.0) >= 0.72)
            or pullback_atr >= 0.95
        )
    )

    fresh_recovery_event = None
    recovery_quality = {"passed": False, "reason": "нового post-rejection сетапу немає"}
    if moderate and rejection_ts:
        for event in scan_15m_interval_entry_events(context, side):
            quality = _strict_post_rejection_recovery_event(
                side, context, event, rejection_ts=rejection_ts,
                severity="STRONG" if strong else "MODERATE",
            )
            if quality.get("passed"):
                fresh_recovery_event = event
                recovery_quality = quality
                break

    fresh_recovery = bool(fresh_recovery_event and recovery_quality.get("passed"))
    # Two same-colour candles only show stabilization. After a STRONG rejection
    # they cannot reopen the trade without a new strict trigger + follow-through.
    tf3_same = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22)
    flow_same = sum([
        (context.get("cvd") or {}).get("bias") == side and abs(int((context.get("cvd") or {}).get("score", 0) or 0)) >= 12,
        (context.get("flow") or {}).get("bias") == side and abs(int((context.get("flow") or {}).get("score", 0) or 0)) >= 10,
    ]) >= 1
    moderate_stabilized = bool(moderate and not strong and two_candle_recovery and tf3_same and flow_same)
    recovered = bool(fresh_recovery or moderate_stabilized)
    if fresh_recovery and moderate:
        severity = "RECOVERED"
    elif strong:
        severity = "STRONG"
    elif moderate:
        severity = "MODERATE"
    else:
        severity = "NONE"

    block = bool((strong and not fresh_recovery) or (moderate and not recovered))
    force_risky = bool(moderate or fresh_recovery)
    reason = ""
    if block:
        reason = (
            "після різкого імпульсу є сильне відхилення; старий тригер анульовано — "
            "потрібен новий 3M sweep/reclaim або breakout-retest" if side == "LONG" else
            "після різкого імпульсу є сильне відхилення; старий тригер анульовано — "
            "потрібен новий 3M rejection/reclaim або breakout-retest"
        )
    elif severity == "MODERATE":
        reason = "є помірне відхилення після імпульсу; дозволений лише новий ризикований вхід після свіжого 3M підтвердження"
    elif severity == "RECOVERED":
        reason = "після відхилення сформовано новий 3M сетап; старий тригер не використовується"

    return {
        "active": moderate,
        "block_entry": block,
        "force_risky": force_risky,
        "severity": severity,
        "recovered": recovered,
        "two_candle_recovery": two_candle_recovery,
        "fresh_recovery": fresh_recovery,
        "fresh_recovery_event": fresh_recovery_event,
        "recovery_quality": recovery_quality,
        "moderate_stabilized": moderate_stabilized,
        "invalidate_old_triggers": bool(moderate and not fresh_recovery),
        "rejection_ts": int(rejection_ts or 0),
        "run_atr15": round(run_atr, 3),
        "pullback_atr15": round(pullback_atr, 3),
        "rejection_count": len(rejection_candles),
        "live_rejection": live_rejection,
        "hard_against": hard_against,
        "fast_against_layers": int(fast_against_layers),
        "reason": reason,
    }

def scan_15m_interval_entry_events(context, side):
    """Reconstruct professional 3M entry events that occurred between 15M runs.

    This does not turn the bot into a 3M scalper. The 15M/ICT thesis and targets
    remain primary; the closed 3M sequence is used only to recover timing that a
    15-minute scheduler could otherwise miss.
    """
    context = context or {}
    side = str(side or "").upper()
    price = safe_float(context.get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return []
    series = _recent_closed_3m_scan(context)
    scan_bars = max(4, int(ENTRY_RESCUE_SCAN_BARS_3M))
    if len(series) < scan_bars + 5:
        return []
    window = series[-scan_bars:]
    baseline = series[:-scan_bars][-8:]
    if len(baseline) < 4:
        return []

    atr15 = safe_float(context.get("atr15"), price * 0.006) or price * 0.006
    atr3 = safe_float(atr(series, 14), atr15 * 0.32) or atr15 * 0.32
    buffer3 = max(atr3 * 0.24, price * 0.00035)
    prior_high = max(c.high for c in baseline)
    prior_low = min(c.low for c in baseline)
    latest_ts = int(window[-1].ts)
    flow_score, flow_evidence = _event_directional_flow_score(context, side)
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    closed_anchor = bool(
        tf15.get("bias") == side
        or structure.get("bias") == side
        or ict.get("bias") == side
        or (tf1h.get("bias") == side and abs(int(tf1h.get("score", 0) or 0)) >= 18)
    )

    events = []

    def add_event(event_type, trigger_idx, trigger_level, stop_level, base_score, evidence, anchor, reset_confirmed=True):
        if trigger_idx is None or trigger_idx < 0 or trigger_idx >= len(window):
            return
        trigger_candle = window[trigger_idx]
        age_min = max(0.0, (latest_ts - int(trigger_candle.ts)) / 60000.0)
        if age_min > ENTRY_RESCUE_MAX_AGE_MINUTES + 0.1:
            return
        stop_level = safe_float(stop_level)
        trigger_level = safe_float(trigger_level, trigger_candle.close)
        if stop_level is None or trigger_level is None:
            return
        # The event must still be alive at the current price.
        invalidated = (side == "LONG" and price <= stop_level) or (side == "SHORT" and price >= stop_level)
        if invalidated:
            return
        favourable_extension = (price - trigger_level) if side == "LONG" else (trigger_level - price)
        extension_atr15 = max(0.0, favourable_extension / atr15) if atr15 else 0.0
        if extension_atr15 > ENTRY_RESCUE_MAX_EXTENSION_ATR15:
            return
        current_against = bool(
            ((context.get("tf3") or {}).get("bias") == opposite(side))
            and abs(int((context.get("tf3") or {}).get("score", 0) or 0)) >= 42
        )
        later = window[trigger_idx + 1:]
        path_invalidated = bool(
            (side == "LONG" and any(c.low <= stop_level for c in later))
            or (side == "SHORT" and any(c.high >= stop_level for c in later))
        )
        if path_invalidated:
            return
        aged_event = age_min > AGED_3M_EVENT_MINUTES
        if aged_event:
            # An older event is reusable only while the closed-15M thesis is
            # still alive, price remains close, and no later rejection appeared.
            # In a rolling balance an old micro event is stale noise; only a
            # fresh edge trigger may be used there.
            rolling = rolling_balance_detector(context)
            if rolling.get("balance"):
                return
            lost_trigger = bool(
                (side == "LONG" and any(c.close < trigger_level - atr3 * 0.15 for c in later))
                or (side == "SHORT" and any(c.close > trigger_level + atr3 * 0.15 for c in later))
            )
            if not closed_anchor or current_against or lost_trigger or extension_atr15 > AGED_3M_EVENT_MAX_EXTENSION_ATR15:
                return
        score = int(clamp(base_score + flow_score + (7 if closed_anchor else 0) - (12 if current_against else 0) - (3 if aged_event else 0), 0, 100))
        if score < ENTRY_RESCUE_MIN_SCORE:
            return
        events.append({
            "confirmed": True,
            "side": side,
            "type": event_type,
            "score": score,
            "trigger_ts": int(trigger_candle.ts),
            "age_min": round(age_min, 2),
            "trigger_level": round_price(trigger_level),
            "trigger_close": round_price(trigger_candle.close),
            "stop_level": round_price(stop_level),
            "extension_atr15": round(extension_atr15, 3),
            "anchor_confirmed": bool(anchor or closed_anchor),
            "closed_anchor": closed_anchor,
            "reset_confirmed": bool(reset_confirmed),
            "professional_location": bool(anchor or closed_anchor),
            "evidence": list(dict.fromkeys(list(evidence) + flow_evidence)),
            "source": "CLOSED_3M_SEQUENCE_BETWEEN_15M_RUNS",
            "age_tier": "AGED_VALID" if aged_event else "FRESH",
            "path_invalidation_checked": True,
        })

    # 1) Liquidity sweep followed by reclaim inside the scan interval.
    if side == "LONG":
        sweep_idxs = [i for i, c in enumerate(window) if c.low < prior_low - atr3 * 0.06]
        for sweep_idx in sweep_idxs:
            reclaim_idx = next((j for j in range(sweep_idx, len(window)) if window[j].close > prior_low + atr3 * 0.03), None)
            if reclaim_idx is not None:
                stop = min(c.low for c in window[sweep_idx:reclaim_idx + 1]) - buffer3
                add_event("SWEEP_RECLAIM", reclaim_idx, prior_low, stop, 73,
                          ["DOWNSIDE_SWEEP", "RECLAIM"], True)
                break
    else:
        sweep_idxs = [i for i, c in enumerate(window) if c.high > prior_high + atr3 * 0.06]
        for sweep_idx in sweep_idxs:
            reclaim_idx = next((j for j in range(sweep_idx, len(window)) if window[j].close < prior_high - atr3 * 0.03), None)
            if reclaim_idx is not None:
                stop = max(c.high for c in window[sweep_idx:reclaim_idx + 1]) + buffer3
                add_event("SWEEP_RECLAIM", reclaim_idx, prior_high, stop, 73,
                          ["UPSIDE_SWEEP", "RECLAIM"], True)
                break

    # 2) Breakout followed by a retest/acceptance. This is especially important
    # for a 15-minute runner because both events may happen before the next run.
    if side == "LONG":
        breakout_idx = next((i for i, c in enumerate(window) if c.close > prior_high + atr3 * 0.06), None)
        if breakout_idx is not None:
            retest_idx = next((j for j in range(breakout_idx + 1, len(window))
                               if window[j].low <= prior_high + atr3 * 0.24 and window[j].close >= prior_high - atr3 * 0.03), None)
            closes_above = sum(1 for c in window[breakout_idx:] if c.close > prior_high)
            if retest_idx is not None:
                stop = min(c.low for c in window[breakout_idx:retest_idx + 1]) - buffer3
                add_event("BREAKOUT_RETEST", retest_idx, prior_high, stop, 76,
                          ["MICRO_BOS", "RETEST_HOLD"], True)
            elif closes_above >= 2:
                stop = min(prior_high - atr3 * 0.34, min(c.low for c in window[breakout_idx:]) - buffer3 * 0.35)
                add_event("BREAKOUT_ACCEPTANCE", breakout_idx, prior_high, stop, 68,
                          ["MICRO_BOS", "TWO_CLOSE_ACCEPTANCE"], closed_anchor, False)
    else:
        breakout_idx = next((i for i, c in enumerate(window) if c.close < prior_low - atr3 * 0.06), None)
        if breakout_idx is not None:
            retest_idx = next((j for j in range(breakout_idx + 1, len(window))
                               if window[j].high >= prior_low - atr3 * 0.24 and window[j].close <= prior_low + atr3 * 0.03), None)
            closes_below = sum(1 for c in window[breakout_idx:] if c.close < prior_low)
            if retest_idx is not None:
                stop = max(c.high for c in window[breakout_idx:retest_idx + 1]) + buffer3
                add_event("BREAKOUT_RETEST", retest_idx, prior_low, stop, 76,
                          ["MICRO_BOS", "RETEST_HOLD"], True)
            elif closes_below >= 2:
                stop = max(prior_low + atr3 * 0.34, max(c.high for c in window[breakout_idx:]) + buffer3 * 0.35)
                add_event("BREAKOUT_ACCEPTANCE", breakout_idx, prior_low, stop, 68,
                          ["MICRO_BOS", "TWO_CLOSE_ACCEPTANCE"], closed_anchor, False)

    # 3) Touch and reclaim of the active ICT zone during the interval.
    zone_keys = ["bull_ob", "bull_fvg"] if side == "LONG" else ["bear_ob", "bear_fvg"]
    for key in zone_keys:
        low, high = _zone_bounds(ict.get(key))
        if low is None or high is None:
            continue
        touch_idx = next((i for i, c in enumerate(window) if c.high >= low - atr3 * 0.08 and c.low <= high + atr3 * 0.08), None)
        if touch_idx is None:
            continue
        mid = (low + high) / 2.0
        if side == "LONG":
            reclaim_idx = next((j for j in range(touch_idx, len(window)) if window[j].close > max(mid, low)), None)
            stop = min(low, min(c.low for c in window[touch_idx:(reclaim_idx + 1) if reclaim_idx is not None else len(window)])) - buffer3
        else:
            reclaim_idx = next((j for j in range(touch_idx, len(window)) if window[j].close < min(mid, high)), None)
            stop = max(high, max(c.high for c in window[touch_idx:(reclaim_idx + 1) if reclaim_idx is not None else len(window)])) + buffer3
        if reclaim_idx is not None:
            add_event("ICT_ZONE_RECLAIM", reclaim_idx, mid, stop, 72,
                      [key.upper(), "ZONE_RECLAIM"], True)
            break

    # Strongest, freshest and least extended event first.
    events.sort(key=lambda e: (e["score"] - e["extension_atr15"] * 12 - e["age_min"] * 0.15), reverse=True)
    return events


def best_15m_interval_entry_event(context, side):
    events = scan_15m_interval_entry_events(context, side)
    return events[0] if events else None


def professional_fast_reversal_bridge(context, target_side):
    """Strict fast bridge with independent adverse layers and direction exclusivity.

    A local 3M pattern cannot confirm a reversal when flow and clusters both point
    the other way. When both LONG and SHORT bridges look plausible, the chosen
    direction must lead by a clear score margin.
    """
    context = context or {}
    target_side = str(target_side or "").upper()
    if target_side not in ["LONG", "SHORT"]:
        return {"confirmed": False, "emergency": False, "score": 0, "evidence": []}

    opposite_side = opposite(target_side)
    tf3 = context.get("tf3") or {}
    ict = context.get("ict") or {}
    structure = context.get("structure") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    liquidity = context.get("liquidity") or {}
    clusters = context.get("clusters") or {}
    guard = context.get("mtf_guard") or {}
    live15 = guard.get("live_15m") or {}

    def score_abs(block):
        return abs(int((block or {}).get("score", 0) or 0))

    interval_event = best_15m_interval_entry_event(context, target_side)
    interval_reversal = bool(
        interval_event and interval_event.get("confirmed") and interval_event.get("anchor_confirmed")
        and interval_event.get("type") in ["SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"]
        and int(interval_event.get("score", 0) or 0) >= ENTRY_RESCUE_MIN_SCORE
    )
    tf3_trigger = bool((tf3.get("bias") == target_side and score_abs(tf3) >= 34) or interval_reversal)
    ict_setup = str(ict.get("setup") or "").upper()
    ict_event = bool(
        ict.get("bias") == target_side and score_abs(ict) >= 18
        and (bool(ict.get("entry_ok")) or any(k in ict_setup for k in ["SWEEP", "CHOCH", "BOS", "RECLAIM", "FVG", "OB"]))
    )
    phase = str(structure.get("phase") or "").upper()
    structure_event = bool(
        structure.get("bias") == target_side and score_abs(structure) >= 18
        and any(k in phase for k in ["BOS", "CHOCH", "SWEEP"])
    )
    live15_event = bool(
        live15.get("active") and live15.get("bias") == target_side
        and (safe_float(live15.get("body_atr"), 0.0) or 0.0) >= 0.58
        and abs(safe_float(live15.get("move_pct"), 0.0) or 0.0) >= 0.22
    )
    cvd_confirm = bool(cvd.get("bias") == target_side and score_abs(cvd) >= 16 and cvd.get("confidence", "HIGH") != "LOW")
    flow_confirm = bool(flow.get("bias") == target_side and score_abs(flow) >= 12)
    liquidity_confirm = bool(liquidity.get("bias") == target_side and score_abs(liquidity) >= 10)
    clusters_confirm = bool(clusters.get("bias") == target_side and score_abs(clusters) >= 5)

    adverse_map = {
        "TF3": bool(tf3.get("bias") == opposite_side and score_abs(tf3) >= 34),
        "CVD": bool(cvd.get("bias") == opposite_side and score_abs(cvd) >= 18),
        "FLOW": bool(flow.get("bias") == opposite_side and score_abs(flow) >= 14),
        "LIQUIDITY": bool(liquidity.get("bias") == opposite_side and score_abs(liquidity) >= 12),
        "CLUSTERS": bool(clusters.get("bias") == opposite_side and score_abs(clusters) >= 5),
    }
    adverse_fast = sum(adverse_map.values())
    independent_confirms = sum([ict_event, structure_event, live15_event, cvd_confirm, flow_confirm, liquidity_confirm, clusters_confirm, interval_reversal])
    structural_anchor = bool(ict_event or structure_event or live15_event or interval_reversal)

    score = 0
    evidence = []
    for ok, pts, label in [
        (tf3_trigger, 30, "3M_TRIGGER"), (interval_reversal, 12, "15M_INTERVAL_3M_EVENT"),
        (ict_event, 22, "ICT_EVENT"), (structure_event, 20, "STRUCTURE_EVENT"),
        (live15_event, 16, "LIVE_15M_DISPLACEMENT"), (cvd_confirm, 10, "CVD"),
        (flow_confirm, 8, "FLOW"), (liquidity_confirm, 6, "LIQUIDITY"),
        (clusters_confirm, 5, "CLUSTERS"),
    ]:
        if ok:
            score += pts
            evidence.append(label)
    score -= adverse_fast * 14
    score = int(clamp(score, 0, 100))

    # Raw opposite-direction score, computed without recursively calling this bridge.
    opp_tf3 = tf3.get("bias") == opposite_side and score_abs(tf3) >= 34
    opp_ict = ict.get("bias") == opposite_side and score_abs(ict) >= 18
    opp_structure = structure.get("bias") == opposite_side and score_abs(structure) >= 18
    opp_cvd = cvd.get("bias") == opposite_side and score_abs(cvd) >= 16
    opp_flow = flow.get("bias") == opposite_side and score_abs(flow) >= 12
    opp_liq = liquidity.get("bias") == opposite_side and score_abs(liquidity) >= 10
    opp_clusters = clusters.get("bias") == opposite_side and score_abs(clusters) >= 5
    opposite_score = int(clamp(
        (30 if opp_tf3 else 0) + (22 if opp_ict else 0) + (20 if opp_structure else 0)
        + (10 if opp_cvd else 0) + (8 if opp_flow else 0) + (6 if opp_liq else 0)
        + (5 if opp_clusters else 0), 0, 100
    ))

    flow_cluster_veto = bool(adverse_map["FLOW"] and adverse_map["CLUSTERS"])
    directional_margin = score - opposite_score
    regime = context.get("regime_engine") or {}
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    countertrend_or_range = bool(
        regime_type in {"RANGE", "RANGE_COMPRESSION", "COMPRESSION", "REVERSAL_BUILDUP"}
        or ((context.get("tf1h") or {}).get("bias") == opposite_side and (context.get("tf4h") or {}).get("bias") == opposite_side)
    )
    exclusivity_ok = bool(directional_margin >= FAST_BRIDGE_MIN_DIRECTIONAL_MARGIN or opposite_score < 35)
    if countertrend_or_range:
        exclusivity_ok = bool(exclusivity_ok and adverse_fast <= 1 and (flow_confirm or cvd_confirm or clusters_confirm))

    confirmed = bool(
        tf3_trigger and structural_anchor and independent_confirms >= 2
        and adverse_fast <= 1 and score >= FAST_REVERSAL_MIN_SCORE
        and not flow_cluster_veto and exclusivity_ok
    )
    emergency = bool(
        confirmed and score >= 82 and independent_confirms >= 3
        and (ict_event or structure_event) and directional_margin >= FAST_BRIDGE_MIN_DIRECTIONAL_MARGIN
    )
    return {
        "confirmed": confirmed, "emergency": emergency, "score": score,
        "target_side": target_side, "evidence": evidence,
        "independent_confirmations": int(independent_confirms),
        "adverse_fast_layers": int(adverse_fast), "adverse_layer_map": adverse_map,
        "tf3_trigger": tf3_trigger, "structural_anchor": structural_anchor,
        "interval_event": interval_event, "opposite_bridge_score": opposite_score,
        "directional_margin": int(directional_margin), "directional_exclusivity": exclusivity_ok,
        "flow_cluster_veto": flow_cluster_veto,
    }




def dual_speed_mtf_snapshot(context, side):
    """Combine closed thesis, live pressure and fast execution into one snapshot.

    Closed 15M/1H/4H define the durable thesis. Live HTF never flips the thesis
    by itself. A strict fast-reversal bridge can, however, confirm a real early
    reversal before the next 15M close when 3M + ICT/structure/live displacement
    + order flow agree.
    """
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {"side": side or "NEUTRAL", "available": False}

    closed_support = 0.0
    closed_against = 0.0
    for block, threshold, weight in [
        (context.get("tf15") or {}, 18, 1.45),
        (context.get("tf1h") or {}, 18, 0.95),
        (context.get("tf4h") or {}, 26, 0.55),
        (context.get("structure") or {}, 10, 1.25),
        (context.get("ict") or {}, 10, 1.25),
    ]:
        s, a = _directional_vote(block, side, threshold, weight)
        closed_support += s
        closed_against += a

    fast_support = 0.0
    fast_against = 0.0
    for block, threshold, weight in [
        (context.get("tf3") or {}, 16, 1.45),
        (context.get("cvd") or {}, 8, 0.80),
        (context.get("flow") or {}, 8, 0.60),
        (context.get("derivatives") or {}, 8, 0.35),
        (context.get("liquidity") or {}, 8, 0.35),
        (context.get("clusters") or {}, 5, 0.25),
    ]:
        s, a = _directional_vote(block, side, threshold, weight)
        fast_support += s
        fast_against += a

    guard = context.get("mtf_guard") or {}
    live_support = 0.0
    live_against = 0.0
    live_details = []
    for key, weight in [("live_15m", 0.75), ("live_1h", 0.28), ("live_4h", 0.17)]:
        live = guard.get(key) or {}
        if not live.get("active"):
            continue
        bias = str(live.get("bias") or "NEUTRAL").upper()
        body_atr = safe_float(live.get("body_atr"), 0.0) or 0.0
        effective_weight = weight * min(1.35, max(0.35, body_atr + 0.35))
        if bias == side:
            live_support += effective_weight
        elif bias == opposite(side):
            live_against += effective_weight
        live_details.append({
            "timeframe": key.replace("live_", "").upper(),
            "bias": bias,
            "body_atr": round(body_atr, 3),
            "age_min": safe_float(live.get("age_min"), 0.0),
        })

    bridge_to_side = professional_fast_reversal_bridge(context, side)
    bridge_against = professional_fast_reversal_bridge(context, opposite(side))
    rescue_event = context.get("entry_rescue_event") or {}
    rescue_to_side = bool(
        rescue_event.get("confirmed")
        and rescue_event.get("side") == side
        and rescue_event.get("professional_location")
        and str(rescue_event.get("type") or "") == "SWEEP_RECLAIM"
        and int(rescue_event.get("score", 0) or 0) >= ENTRY_RESCUE_MIN_SCORE
    )
    rescue_against = bool(
        rescue_event.get("confirmed")
        and rescue_event.get("side") == opposite(side)
        and int(rescue_event.get("score", 0) or 0) >= ENTRY_RESCUE_MIN_SCORE
    )

    ict = context.get("ict") or {}
    structure = context.get("structure") or {}
    ict_setup = str(ict.get("setup") or "").upper()
    professional_location = bool(
        (ict.get("bias") == side and (ict.get("entry_ok") or any(k in ict_setup for k in ["FVG", "OB", "SWEEP", "RETRACE", "HOLD"])))
        or structure.get("bias") == side
        or bridge_to_side.get("confirmed")
        or rescue_to_side
    )
    closed_confirmed = bool(closed_support >= 2.45 and closed_against < 1.25)
    regular_fast_trigger = bool(fast_support + live_support >= 1.65 and fast_against + live_against < 1.35)
    fast_trigger = bool(regular_fast_trigger or bridge_to_side.get("confirmed") or rescue_to_side)
    live_pressure = bool(live_against >= 0.55 or fast_against >= 1.45 or bridge_against.get("confirmed") or rescue_against)

    adverse_fast_blocks = sum([
        (context.get("tf3") or {}).get("bias") == opposite(side) and abs(int((context.get("tf3") or {}).get("score", 0) or 0)) >= 42,
        (context.get("cvd") or {}).get("bias") == opposite(side) and abs(int((context.get("cvd") or {}).get("score", 0) or 0)) >= 16,
        (context.get("flow") or {}).get("bias") == opposite(side) and abs(int((context.get("flow") or {}).get("score", 0) or 0)) >= 14,
        (context.get("liquidity") or {}).get("bias") == opposite(side) and abs(int((context.get("liquidity") or {}).get("score", 0) or 0)) >= 12,
    ])
    live15 = guard.get("live_15m") or {}
    emergency_against = bool(
        bridge_against.get("emergency")
        or adverse_fast_blocks >= 3
        or (
            live15.get("active")
            and live15.get("bias") == opposite(side)
            and (safe_float(live15.get("body_atr"), 0.0) or 0.0) >= 0.95
            and adverse_fast_blocks >= 2
        )
    )

    net = (closed_support - closed_against) * 12.0 + (fast_support - fast_against) * 8.0 + (live_support - live_against) * 5.0
    net += (bridge_to_side.get("score", 0) - bridge_against.get("score", 0)) * 0.16
    confidence = int(clamp(50 + net, 0, 100))
    return {
        "available": True,
        "side": side,
        "closed_support": round(closed_support, 2),
        "closed_against": round(closed_against, 2),
        "fast_support": round(fast_support, 2),
        "fast_against": round(fast_against, 2),
        "live_support": round(live_support, 2),
        "live_against": round(live_against, 2),
        "closed_confirmed": closed_confirmed,
        "fast_trigger": fast_trigger,
        "regular_fast_trigger": regular_fast_trigger,
        "professional_location": professional_location,
        "live_pressure": live_pressure,
        "emergency_against": emergency_against,
        "fast_reversal_to_side": bridge_to_side,
        "fast_reversal_against": bridge_against,
        "entry_rescue_event": rescue_event if rescue_to_side else None,
        "confidence": confidence,
        "live_details": live_details,
        "policy": "UNIFIED_CLOSED_THESIS + LIVE_PRESSURE + STRICT_FAST_REVERSAL_BRIDGE",
    }

def build_dual_speed_mtf(context):
    return {
        "LONG": dual_speed_mtf_snapshot(context, "LONG"),
        "SHORT": dual_speed_mtf_snapshot(context, "SHORT"),
    }

def get_okx_candles(bar="15m", limit=160, inst_id=None):
    inst_id = inst_id or OKX_INST_ID
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        data = response.json().get("data", [])
        return parse_okx_candles(data)
    except Exception as error:
        print(f"[WARN] OKX candles parse failed {bar}: {error}")
        return []


def get_okx_ticker(inst_id=None):
    inst_id = inst_id or OKX_INST_ID
    url = f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}"
    response = http_get(url)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        last = float(row.get("last") or row.get("lastSz") or 0)
        open24h = float(row.get("open24h") or 0)
        change = pct(last, open24h) if open24h else 0
        return {
            "price": last,
            "change24h": change,
            "volume24h": safe_float(row.get("volCcy24h"), 0),
            "source": "OKX",
            "symbol": inst_id,
        }
    except Exception as error:
        print(f"[WARN] OKX ticker parse failed: {error}")
        return {}


def get_okx_trades(limit=120):
    url = f"https://www.okx.com/api/v5/market/trades?instId={OKX_INST_ID}&limit={limit}"
    response = http_get(url)
    if not response:
        return []
    try:
        trades = []
        for row in response.json().get("data", []):
            trades.append({
                "price": float(row.get("px") or 0),
                "size": float(row.get("sz") or 0),
                "side": str(row.get("side") or "").lower(),
                "ts": int(row.get("ts") or 0),
            })
        return trades
    except Exception as error:
        print(f"[WARN] OKX trades parse failed: {error}")
        return []


def get_okx_book(depth=50):
    url = f"https://www.okx.com/api/v5/market/books?instId={OKX_INST_ID}&sz={depth}"
    response = http_get(url)
    if not response:
        return {"bids": [], "asks": []}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {"bids": [], "asks": []}
        book = rows[0]
        bids = [(float(x[0]), float(x[1])) for x in book.get("bids", []) if len(x) >= 2]
        asks = [(float(x[0]), float(x[1])) for x in book.get("asks", []) if len(x) >= 2]
        return {"bids": bids, "asks": asks}
    except Exception as error:
        print(f"[WARN] OKX book parse failed: {error}")
        return {"bids": [], "asks": []}


def get_okx_open_interest():
    """Current futures/swap open interest from OKX.

    This is used as a derivatives confirmation layer:
    - price up + OI up = stronger real LONG participation
    - price down + OI up = stronger real SHORT participation
    - price moves with OI down = mostly closing/liquidation, do not chase late
    """
    url = f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={OKX_INST_ID}"
    response = http_get(url, timeout=8, retries=1)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        return {
            "oi": safe_float(row.get("oi"), 0),
            "oi_ccy": safe_float(row.get("oiCcy"), 0),
            "ts": int(row.get("ts") or 0),
            "source": "OKX open interest",
        }
    except Exception as error:
        print(f"[WARN] OKX open interest parse failed: {error}")
        return {}


def get_okx_funding_rate():
    """Current funding rate from OKX.

    Funding is not a standalone entry. It is a risk/positioning filter:
    very positive funding warns against chasing LONG; very negative funding warns
    against chasing SHORT, especially after a squeeze/liquidation move.
    """
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={OKX_INST_ID}"
    response = http_get(url, timeout=8, retries=1)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
        if not rows:
            return {}
        row = rows[0]
        return {
            "funding_rate": safe_float(row.get("fundingRate"), 0),
            "next_funding_rate": safe_float(row.get("nextFundingRate"), None),
            "funding_time": int(row.get("fundingTime") or 0),
            "source": "OKX funding",
        }
    except Exception as error:
        print(f"[WARN] OKX funding parse failed: {error}")
        return {}


def get_tradingview_price_fallback():
    url = "https://scanner.tradingview.com/crypto/scan"
    payload = {
        "symbols": {"tickers": ["BINANCE:BZUSDT.P", "BINANCE:BZUSDT"], "query": {"types": []}},
        "columns": ["close", "change", "volume"],
    }
    response = http_post(url, payload)
    if not response:
        return {}
    try:
        rows = response.json().get("data", [])
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


def collect_market_data():
    candles_3m = get_okx_candles("3m", 220)
    candles_15m = get_okx_candles("15m", 180)
    candles_1h = get_okx_candles("1H", 140)
    candles_4h = get_okx_candles("4H", 140)

    # IMPORTANT: the chart the user watches is BINANCE:BZUSDT.P on TradingView.
    # Use TradingView/Binance as the primary displayed/current price and OKX only
    # as a fallback. This prevents Telegram from showing an OKX price that is
    # already different from the TradingView chart.
    tv_ticker = get_tradingview_price_fallback()
    okx_ticker = get_okx_ticker()
    ticker = tv_ticker or okx_ticker
    if ticker:
        ticker["checked_at"] = iso_now()
        ticker["price_status"] = "primary"
    if tv_ticker and okx_ticker:
        tv_price = safe_float(tv_ticker.get("price"))
        okx_price = safe_float(okx_ticker.get("price"))
        if tv_price and okx_price:
            diff_pct = abs(tv_price - okx_price) / tv_price * 100
            ticker["okx_reference_price"] = okx_price
            ticker["cross_price_diff_pct"] = round(diff_pct, 4)
            ticker["price_status"] = "cross_checked" if diff_pct <= PRICE_SOURCE_MAX_DIFF_PCT else "price_source_warning"
            ticker["price_warning"] = diff_pct > PRICE_SOURCE_MAX_DIFF_PCT
    elif okx_ticker:
        ticker["okx_reference_price"] = okx_ticker.get("price")
        if not tv_ticker:
            ticker["price_status"] = "tradingview_unavailable_okx_fallback"
    if not ticker and candles_15m:
        ticker = {
            "price": candles_15m[-1].close,
            "change24h": pct(candles_15m[-1].close, candles_15m[0].open),
            "volume24h": 0,
            "source": "OKX candles",
            "symbol": OKX_INST_ID,
            "checked_at": iso_now(),
        }
    return {
        "ticker": ticker,
        "candles_3m": candles_3m,
        "candles_15m": candles_15m,
        "candles_1h": candles_1h,
        "candles_4h": candles_4h,
        "trades": get_okx_trades(),
        "book": get_okx_book(),
        "open_interest": get_okx_open_interest(),
        "funding": get_okx_funding_rate(),
    }


def refresh_context_price(context):
    """Refresh the current TradingView price immediately before decision/message.

    The bot can spend several seconds collecting news/candles/orderbook. During
    that time BZU can move fast, so the entry plan must be based on the newest
    ticker, not on the price captured at the beginning of the run.
    """
    if not isinstance(context, dict):
        return context
    old_price = safe_float(context.get("price"))
    tv_latest = get_tradingview_price_fallback()
    okx_latest = get_okx_ticker()
    latest = tv_latest or okx_latest
    new_price = safe_float((latest or {}).get("price"))
    if not new_price:
        context["price_checked_at"] = iso_now()
        context["price_status"] = "refresh_failed_using_previous"
        return context

    context["price_checked_at"] = iso_now()
    context["price_source"] = latest.get("source") or context.get("price_source") or "ticker"
    context["price_symbol"] = latest.get("symbol") or context.get("price_symbol") or ""
    if tv_latest and okx_latest:
        tv_price = safe_float(tv_latest.get("price"))
        okx_price = safe_float(okx_latest.get("price"))
        if tv_price and okx_price:
            cross_diff = abs(tv_price - okx_price) / tv_price * 100
            context["cross_price_diff_pct"] = round(cross_diff, 4)
            context["okx_reference_price"] = round_price(okx_price)
            context["price_warning"] = cross_diff > PRICE_SOURCE_MAX_DIFF_PCT

    if old_price:
        diff_pct = abs(new_price - old_price) / old_price * 100
        context["price_refresh_diff_pct"] = round(diff_pct, 4)
        context["price_before_refresh"] = round_price(old_price)
    else:
        diff_pct = 0
        context["price_refresh_diff_pct"] = 0
        context["price_before_refresh"] = None

    # Always use the freshest ticker for the Telegram price and any new plan.
    context["price"] = round_price(new_price)
    context["price_status"] = "fresh" if diff_pct <= 0.08 else "updated_before_signal"
    return context


# ==========================================================
# INDICATORS / CONTEXT
# ==========================================================


def ema(values, period):
    values = [float(x) for x in values if x is not None]
    if not values:
        return None
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(values, period=14):
    """TradingView-style RSI using Wilder/RMA smoothing.

    The previous version used a simple average of the last N gains/losses.
    TradingView's default RSI uses Wilder smoothing, so this keeps bot RSI
    closer to the chart the user watches.
    """
    values = [float(x) for x in values if x is not None]
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    if len(gains) < period:
        return 50.0

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder RMA: next = (prev * (period - 1) + current) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    """TradingView-style ATR using Wilder/RMA smoothing."""
    if not candles or len(candles) < 2:
        return None

    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))

    if not trs:
        return None

    if len(trs) < period:
        return mean(trs)

    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = ((atr_val * (period - 1)) + tr) / period
    return atr_val


def slope_pct(values, bars=12):
    values = [float(v) for v in values if v is not None]
    if len(values) < bars + 1:
        return 0.0
    start = mean(values[-bars - 1:-bars + 2])
    end = mean(values[-3:])
    return pct(end, start)


def candle_body_ratio(candle):
    rng = max(candle.high - candle.low, 1e-9)
    return abs(candle.close - candle.open) / rng


def close_location(candle):
    rng = max(candle.high - candle.low, 1e-9)
    return (candle.close - candle.low) / rng


def analyze_timeframe(candles, label):
    if not candles or len(candles) < 30:
        return {
            "label": label, "available": False, "bias": "NEUTRAL", "score": 0,
            "trend": "NO DATA", "note": "даних мало",
        }

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    last = candles[-1]
    ema20 = ema(closes[-60:], 20)
    ema50 = ema(closes[-80:], 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14) or max(last.close * 0.004, 0.01)
    slope = slope_pct(closes, 12)
    move_8 = pct(closes[-1], closes[-9]) if len(closes) >= 9 else 0
    range_20 = max(highs[-20:]) - min(lows[-20:])
    atr_range = range_20 / atr14 if atr14 else 0

    score = 0
    reasons = []

    if ema20 and ema50:
        if last.close > ema20 > ema50:
            score += 24
            reasons.append("ціна вище EMA20/50")
        elif last.close < ema20 < ema50:
            score -= 24
            reasons.append("ціна нижче EMA20/50")
        elif ema20 > ema50:
            score += 8
            reasons.append("EMA20 вище EMA50")
        elif ema20 < ema50:
            score -= 8
            reasons.append("EMA20 нижче EMA50")

    if slope >= 0.35:
        score += 18
        reasons.append("нахил вгору")
    elif slope <= -0.35:
        score -= 18
        reasons.append("нахил вниз")
    elif slope >= 0.12:
        score += 8
    elif slope <= -0.12:
        score -= 8

    if move_8 >= 0.45:
        score += 14
        reasons.append("імпульс вгору")
    elif move_8 <= -0.45:
        score -= 14
        reasons.append("імпульс вниз")

    if rsi14 >= 76:
        score -= 10
        reasons.append("перекупленість")
    elif rsi14 <= 24:
        score += 10
        reasons.append("перепроданість")

    if candle_body_ratio(last) >= 0.55:
        if last.close > last.open and close_location(last) >= 0.62:
            score += 7
        elif last.close < last.open and close_location(last) <= 0.38:
            score -= 7

    if atr_range < 2.3:
        score = int(score * 0.75)
        reasons.append("стиснення/боковик")

    if score >= 26:
        bias = "LONG"
    elif score <= -26:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    trend = bias
    if atr_range < 2.0:
        trend = "RANGE"

    return {
        "label": label,
        "available": True,
        "bias": bias,
        "score": int(score),
        "trend": trend,
        "close": last.close,
        "ema20": round_price(ema20),
        "ema50": round_price(ema50),
        "rsi": round(rsi14, 1),
        "atr": round_price(atr14),
        "slope_pct": round(slope, 3),
        "move_8_pct": round(move_8, 3),
        "range_atr": round(atr_range, 2),
        "note": "; ".join(reasons[:4]) if reasons else "без сильного перекосу",
    }


def swing_points(candles, lookback=2):
    highs = []
    lows = []
    if not candles or len(candles) < lookback * 2 + 8:
        return highs, lows
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        left = candles[i - lookback:i]
        right = candles[i + 1:i + 1 + lookback]
        if all(c.high > x.high for x in left + right):
            highs.append({"idx": i, "price": c.high, "ts": c.ts})
        if all(c.low < x.low for x in left + right):
            lows.append({"idx": i, "price": c.low, "ts": c.ts})
    return highs, lows


def analyze_structure(candles):
    if not candles or len(candles) < 35:
        return {"available": False, "bias": "NEUTRAL", "score": 0, "phase": "NO DATA", "note": "структура недоступна"}

    recent = candles[-32:]
    last = recent[-1]
    prev = recent[-2]
    highs, lows = swing_points(candles[-80:], 2)
    recent_high = max(c.high for c in recent[:-1])
    recent_low = min(c.low for c in recent[:-1])
    swing_high = highs[-1]["price"] if highs else recent_high
    swing_low = lows[-1]["price"] if lows else recent_low
    atr14 = atr(candles, 14) or last.close * 0.005

    score = 0
    notes = []
    phase = "RANGE / WAIT"

    if last.close > swing_high:
        score += 32
        phase = "BOS LONG"
        notes.append(f"закриття вище swing high {round_price(swing_high)}")
    elif last.close < swing_low:
        score -= 32
        phase = "BOS SHORT"
        notes.append(f"закриття нижче swing low {round_price(swing_low)}")

    if last.high > recent_high and last.close < recent_high:
        score -= 22
        phase = "UPSIDE SWEEP"
        notes.append("зняли ліквідність зверху і закрились нижче")
    elif last.low < recent_low and last.close > recent_low:
        score += 22
        phase = "DOWNSIDE SWEEP"
        notes.append("зняли ліквідність знизу і повернулись вище")

    mid = (recent_high + recent_low) / 2
    if prev.low <= recent_low + atr14 * 0.15 and last.close > mid:
        score += 16
        phase = "CHOCH LONG"
        notes.append("після зняття low повернення в діапазон")
    elif prev.high >= recent_high - atr14 * 0.15 and last.close < mid:
        score -= 16
        phase = "CHOCH SHORT"
        notes.append("після зняття high повернення в діапазон")

    last_range = max(last.high - last.low, 1e-9)
    body = abs(last.close - last.open)
    if body / last_range >= 0.6:
        if last.close > last.open:
            score += 8
            notes.append("сильне bullish-закриття")
        else:
            score -= 8
            notes.append("сильне bearish-закриття")

    if score >= 22:
        bias = "LONG"
    elif score <= -22:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "phase": phase,
        "swing_high": round_price(swing_high),
        "swing_low": round_price(swing_low),
        "recent_high": round_price(recent_high),
        "recent_low": round_price(recent_low),
        "atr": round_price(atr14),
        "note": "; ".join(notes[:3]) if notes else "структура без чистого пробою",
    }



# ==========================================================
# ICT INTRADAY MODEL
# ==========================================================

def _candle_dir(c):
    if c.close > c.open:
        return "UP"
    if c.close < c.open:
        return "DOWN"
    return "DOJI"


def _zone_contains(zone, price, pad=0.0):
    if not zone or price is None:
        return False
    low = min(zone.get("low", 0), zone.get("high", 0)) - pad
    high = max(zone.get("low", 0), zone.get("high", 0)) + pad
    return low <= price <= high


def _nearest_zone(zones, price):
    if not zones or price is None:
        return None
    return min(zones, key=lambda z: abs(((z["low"] + z["high"]) / 2) - price))


def detect_fvg_zones(candles, lookback=42):
    """ICT Fair Value Gap / imbalance detector.

    Bullish FVG: candle[i-2].high < candle[i].low.
    Bearish FVG: candle[i-2].low > candle[i].high.

    The bot uses FVG as a retracement destination, not as a reason to chase.
    """
    zones = []
    if not candles or len(candles) < 8:
        return zones
    sample = candles[-lookback:]
    for i in range(2, len(sample)):
        c0 = sample[i - 2]
        c1 = sample[i - 1]
        c2 = sample[i]
        mid_body = abs(c1.close - c1.open)
        avg_body = mean([abs(x.close - x.open) for x in sample[max(0, i - 10):i]]) if i > 2 else mid_body
        displacement = mid_body >= max(avg_body * 1.25, 1e-9)

        if c0.high < c2.low:
            zones.append({
                "side": "LONG",
                "low": round_price(c0.high),
                "high": round_price(c2.low),
                "mid": round_price((c0.high + c2.low) / 2),
                "created_ts": c2.ts,
                "displacement": displacement,
                "type": "BULLISH_FVG",
            })
        if c0.low > c2.high:
            zones.append({
                "side": "SHORT",
                "low": round_price(c2.high),
                "high": round_price(c0.low),
                "mid": round_price((c2.high + c0.low) / 2),
                "created_ts": c2.ts,
                "displacement": displacement,
                "type": "BEARISH_FVG",
            })
    return zones[-12:]


def detect_orderblock_zone(candles, side, lookback=34):
    """Simple ICT orderblock proxy.

    LONG: last down candle before bullish displacement.
    SHORT: last up candle before bearish displacement.
    Zone is candle range with the midpoint emphasized for lower risk.
    """
    if not candles or len(candles) < 12:
        return None
    sample = candles[-lookback:]
    bodies = [abs(c.close - c.open) for c in sample]
    avg_body = mean(bodies[-20:]) if len(bodies) >= 20 else mean(bodies)
    for idx in range(len(sample) - 3, 2, -1):
        c = sample[idx]
        next2 = sample[idx + 1:min(len(sample), idx + 4)]
        if not next2:
            continue
        if side == "LONG" and c.close < c.open:
            displacement = max(x.close for x in next2) > c.high and any(abs(x.close - x.open) >= avg_body * 1.15 and x.close > x.open for x in next2)
            if displacement:
                return {
                    "side": "LONG",
                    "low": round_price(c.low),
                    "high": round_price(c.high),
                    "mid": round_price((c.low + c.high) / 2),
                    "created_ts": c.ts,
                    "type": "BULLISH_OB",
                }
        if side == "SHORT" and c.close > c.open:
            displacement = min(x.close for x in next2) < c.low and any(abs(x.close - x.open) >= avg_body * 1.15 and x.close < x.open for x in next2)
            if displacement:
                return {
                    "side": "SHORT",
                    "low": round_price(c.low),
                    "high": round_price(c.high),
                    "mid": round_price((c.low + c.high) / 2),
                    "created_ts": c.ts,
                    "type": "BEARISH_OB",
                }
    return None




# ==========================================================
# ICT PERSISTENT ZONES / MITIGATION
# ==========================================================

def _zone_key(zone):
    if not zone:
        return ""
    return f"{zone.get('side')}:{zone.get('type')}:{round_price(zone.get('low'))}:{round_price(zone.get('high'))}:{zone.get('created_ts', '')}"


def _normalize_zone(zone):
    if not zone:
        return None
    try:
        low = safe_float(zone.get("low"))
        high = safe_float(zone.get("high"))
        if low is None or high is None:
            return None
        low, high = min(low, high), max(low, high)
        return {
            "side": str(zone.get("side", "NEUTRAL")),
            "type": str(zone.get("type", "ZONE")),
            "low": round_price(low),
            "high": round_price(high),
            "mid": round_price(safe_float(zone.get("mid"), (low + high) / 2)),
            "created_ts": int(zone.get("created_ts") or 0),
            "displacement": bool(zone.get("displacement", False)),
            "mitigated": bool(zone.get("mitigated", False)),
            "last_seen": iso_now(),
        }
    except Exception:
        return None


def load_ict_memory(state):
    raw = (state or {}).get("ict_memory") or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("fvg_zones", [])
    raw.setdefault("ob_zones", [])
    raw.setdefault("mitigated_ob_keys", [])
    raw.setdefault("mitigated_fvg_keys", [])
    return raw


def _merge_zone_history(existing, fresh, max_items=32):
    merged = {}
    for zone in existing or []:
        nz = _normalize_zone(zone)
        if nz:
            merged[_zone_key(nz)] = nz
    for zone in fresh or []:
        nz = _normalize_zone(zone)
        if nz:
            key = _zone_key(nz)
            if key in merged:
                nz["mitigated"] = bool(merged[key].get("mitigated", False))
            merged[key] = nz
    zones = list(merged.values())
    zones.sort(key=lambda z: z.get("created_ts", 0), reverse=True)
    return zones[:max_items]


def _mark_mitigated_zones(zones, candles, side=None):
    """Mark FVG/OB zones as mitigated if price entered and then closed away.

    LONG zone mitigation:
      - candle trades into zone
      - later closes above zone high
    SHORT zone mitigation:
      - candle trades into zone
      - later closes below zone low

    Once mitigated, it is not used for a fresh signal again.
    """
    if not zones or not candles:
        return zones
    out = []
    recent = candles[-45:]
    for z in zones:
        nz = dict(z)
        if side and nz.get("side") != side:
            out.append(nz)
            continue
        low = safe_float(nz.get("low"))
        high = safe_float(nz.get("high"))
        if low is None or high is None:
            out.append(nz)
            continue
        low, high = min(low, high), max(low, high)
        touched = False
        mitigated = bool(nz.get("mitigated", False))
        for c in recent:
            if c.high >= low and c.low <= high:
                touched = True
            if touched:
                if nz.get("side") == "LONG" and c.close > high:
                    mitigated = True
                    break
                if nz.get("side") == "SHORT" and c.close < low:
                    mitigated = True
                    break
        nz["mitigated"] = mitigated
        out.append(nz)
    return out


def _filter_fresh_ict_zones(zones, candles, max_age_hours):
    """Keep ICT zones fresh only. Old FVG/OB zones often become noise after many runs."""
    if not zones:
        return []
    if not candles:
        return [z for z in zones if not z.get("mitigated")][:24]
    try:
        last_ts = max(int(c.ts) for c in candles if getattr(c, "ts", None))
    except Exception:
        return [z for z in zones if not z.get("mitigated")][:24]
    max_age_ms = int(max_age_hours * 60 * 60 * 1000)
    fresh = []
    for z in zones:
        if z.get("mitigated"):
            continue
        created = int(z.get("created_ts") or 0)
        if not created or last_ts - created <= max_age_ms:
            fresh.append(z)
    return fresh


def update_ict_memory(state, context, candles_15m):
    """Persist FVG/OB zones so pending ICT zones are stable between runs."""
    memory = load_ict_memory(state)
    ict = context.get("ict") or {}

    fresh_fvgs = []
    for key in ["bull_fvg", "bear_fvg"]:
        if ict.get(key):
            fresh_fvgs.append(ict[key])

    fresh_obs = []
    for key in ["bull_ob", "bear_ob"]:
        if ict.get(key):
            fresh_obs.append(ict[key])

    memory["fvg_zones"] = _merge_zone_history(memory.get("fvg_zones", []), fresh_fvgs, 36)
    memory["ob_zones"] = _merge_zone_history(memory.get("ob_zones", []), fresh_obs, 24)

    memory["fvg_zones"] = _mark_mitigated_zones(memory["fvg_zones"], candles_15m)
    memory["ob_zones"] = _mark_mitigated_zones(memory["ob_zones"], candles_15m)
    memory["fvg_zones"] = _filter_fresh_ict_zones(memory["fvg_zones"], candles_15m, ICT_FVG_MAX_AGE_HOURS)
    memory["ob_zones"] = _filter_fresh_ict_zones(memory["ob_zones"], candles_15m, ICT_OB_MAX_AGE_HOURS)

    memory["mitigated_ob_keys"] = [_zone_key(z) for z in memory["ob_zones"] if z.get("mitigated")][-60:]
    memory["mitigated_fvg_keys"] = [_zone_key(z) for z in memory["fvg_zones"] if z.get("mitigated")][-80:]
    memory["updated_at"] = iso_now()
    state["ict_memory"] = memory
    return memory


def apply_ict_memory_to_context(context, memory):
    """Attach persistent zones to context and avoid reusing mitigated OBs/FVGs."""
    ict = context.get("ict") or {}
    memory = memory or {}

    fvg_zones = [z for z in memory.get("fvg_zones", []) if not z.get("mitigated")]
    ob_zones = [z for z in memory.get("ob_zones", []) if not z.get("mitigated")]

    price = context.get("price")
    if price:
        bull_fvgs = [z for z in fvg_zones if z.get("side") == "LONG"]
        bear_fvgs = [z for z in fvg_zones if z.get("side") == "SHORT"]
        bull_obs = [z for z in ob_zones if z.get("side") == "LONG"]
        bear_obs = [z for z in ob_zones if z.get("side") == "SHORT"]

        # Use persistent zone if current one is missing or already mitigated.
        if not ict.get("bull_fvg") and bull_fvgs:
            ict["bull_fvg"] = _nearest_zone(bull_fvgs, price)
        if not ict.get("bear_fvg") and bear_fvgs:
            ict["bear_fvg"] = _nearest_zone(bear_fvgs, price)
        if not ict.get("bull_ob") and bull_obs:
            ict["bull_ob"] = _nearest_zone(bull_obs, price)
        if not ict.get("bear_ob") and bear_obs:
            ict["bear_ob"] = _nearest_zone(bear_obs, price)

    ict["persistent_fvg_count"] = len(fvg_zones)
    ict["persistent_ob_count"] = len(ob_zones)
    context["ict"] = ict
    context["ict_memory"] = {
        "active_fvg": len(fvg_zones),
        "active_ob": len(ob_zones),
        "mitigated_ob": len(memory.get("mitigated_ob_keys", [])),
        "mitigated_fvg": len(memory.get("mitigated_fvg_keys", [])),
    }
    return context

def analyze_ict_model(candles_3m, candles_15m, structure, price):
    """ICT intraday decision layer.

    Important safety rule:
    ICT direction is NOT assigned from premium/discount alone.
    It needs an actual model:
    - liquidity sweep + reclaim
    - BOS/MSS + retracement into FVG/OB
    - clear FVG/OB reaction zone

    If price is in the middle of the dealing range and no model is active,
    return NEUTRAL instead of forcing SHORT/LONG.
    """
    if not candles_15m or len(candles_15m) < 45 or not price:
        return {
            "available": False,
            "bias": "NEUTRAL",
            "score": 0,
            "state": "NO_DATA",
            "setup": "NONE",
            "entry_ok": False,
            "no_chase": False,
            "note": "ICT: даних мало",
        }

    recent = candles_15m[-48:]
    last = recent[-1]
    atr15 = atr(candles_15m, 14) or max(price * 0.005, 0.01)

    # Use completed candles for the dealing range so current candle does not
    # falsely create a sweep signal.
    range_sample = recent[:-1]
    range_high = max(c.high for c in range_sample)
    range_low = min(c.low for c in range_sample)
    eq = (range_high + range_low) / 2
    range_size = max(range_high - range_low, atr15)
    pd_pos = (price - range_low) / range_size

    discount = pd_pos <= 0.45
    premium = pd_pos >= 0.55
    midrange = 0.45 < pd_pos < 0.55

    fvg15 = detect_fvg_zones(candles_15m, 42)
    fvg3 = detect_fvg_zones(candles_3m or [], 55)
    bullish_fvgs = [z for z in (fvg15 + fvg3) if z["side"] == "LONG"]
    bearish_fvgs = [z for z in (fvg15 + fvg3) if z["side"] == "SHORT"]
    nearest_bull_fvg = _nearest_zone(bullish_fvgs, price)
    nearest_bear_fvg = _nearest_zone(bearish_fvgs, price)

    bull_ob = detect_orderblock_zone(candles_15m, "LONG") or detect_orderblock_zone(candles_3m or [], "LONG")
    bear_ob = detect_orderblock_zone(candles_15m, "SHORT") or detect_orderblock_zone(candles_3m or [], "SHORT")

    pad = atr15 * 0.14
    in_bull_fvg = _zone_contains(nearest_bull_fvg, price, pad)
    in_bear_fvg = _zone_contains(nearest_bear_fvg, price, pad)
    in_bull_ob = _zone_contains(bull_ob, price, pad)
    in_bear_ob = _zone_contains(bear_ob, price, pad)

    phase = (structure or {}).get("phase", "")
    bos_long = phase == "BOS LONG"
    bos_short = phase == "BOS SHORT"
    choch_long = phase in ["CHOCH LONG", "DOWNSIDE SWEEP"]
    choch_short = phase in ["CHOCH SHORT", "UPSIDE SWEEP"]

    # Sweep must close back inside the previous range.
    swept_low = last.low < range_low and last.close > range_low
    swept_high = last.high > range_high and last.close < range_high

    move_3 = pct(last.close, recent[-4].close) if len(recent) >= 4 else 0
    body = abs(last.close - last.open)
    avg_body = mean([abs(c.close - c.open) for c in recent[-21:-1]]) if len(recent) >= 22 else body
    displacement_up = last.close > last.open and body >= max(avg_body * 1.45, atr15 * 0.36)
    displacement_down = last.close < last.open and body >= max(avg_body * 1.45, atr15 * 0.36)

    # BOS continuation without perfect retest:
    # If BOS has happened and the next candles hold the broken level instead of
    # fully returning into FVG/OB, the bot can treat this as continuation.
    recent3 = recent[-3:]
    recent5 = recent[-5:]
    swing_high = safe_float((structure or {}).get("swing_high"))
    swing_low = safe_float((structure or {}).get("swing_low"))
    bos_long_hold = bool(
        bos_long and swing_high and
        min(c.close for c in recent3) >= swing_high - atr15 * 0.12 and
        last.close >= swing_high and
        not displacement_up
    )
    bos_short_hold = bool(
        bos_short and swing_low and
        max(c.close for c in recent3) <= swing_low + atr15 * 0.12 and
        last.close <= swing_low and
        not displacement_down
    )
    bos_long_continuation = bool(
        bos_long and swing_high and
        last.close > swing_high and
        min(c.low for c in recent5) >= swing_high - atr15 * 0.28 and
        abs(move_3) <= 0.55
    )
    bos_short_continuation = bool(
        bos_short and swing_low and
        last.close < swing_low and
        max(c.high for c in recent5) <= swing_low + atr15 * 0.28 and
        abs(move_3) <= 0.55
    )

    highs_near_above = sum(1 for c in recent[-28:-1] if price < c.high <= price + atr15 * 1.25)
    lows_near_below = sum(1 for c in recent[-28:-1] if price - atr15 * 1.25 <= c.low < price)

    score = 0
    notes = []
    setup = "NONE"
    bias = "NEUTRAL"
    entry_ok = False
    no_chase = False

    # 1) Liquidity sweep model.
    if swept_low or choch_long:
        score = 26
        bias = "LONG"
        setup = "LIQUIDITY_SWEEP_LONG"
        notes.append("sell-side liquidity sweep + reclaim")
        if discount:
            score += 7
            notes.append("discount")
        if highs_near_above <= 3:
            score += 5
            notes.append("шлях вгору з меншим опором")
        entry_ok = True

    elif swept_high or choch_short:
        score = -26
        bias = "SHORT"
        setup = "LIQUIDITY_SWEEP_SHORT"
        notes.append("buy-side liquidity sweep + reclaim вниз")
        if premium:
            score -= 7
            notes.append("premium")
        if lows_near_below <= 3:
            score -= 5
            notes.append("шлях вниз з меншим опором")
        entry_ok = True

    # 2) Continuation after BOS: only entry if price returns to FVG/OB.
    if bos_long:
        local = 14
        if in_bull_fvg or in_bull_ob:
            local += 18
            entry_ok = True
            setup = "BOS_LONG_RETRACE_FVG_OB"
            notes.append("BOS LONG + повернення у bullish FVG/OB")
        elif bos_long_hold or bos_long_continuation:
            local += 12
            entry_ok = True
            setup = "BOS_LONG_CONTINUATION_HOLD"
            notes.append("BOS LONG утримує пробитий рівень — continuation без повного ретесту")
        elif displacement_up and move_3 > 0.55:
            no_chase = True
            local -= 10
            setup = "BOS_LONG_WAIT_PULLBACK"
            notes.append("BOS LONG, але імпульс вже відбувся — чекати FVG/OB")
        if discount:
            local += 4
        score += local
        if score > 0:
            bias = "LONG"

    if bos_short:
        local = -14
        if in_bear_fvg or in_bear_ob:
            local -= 18
            entry_ok = True
            setup = "BOS_SHORT_RETRACE_FVG_OB"
            notes.append("BOS SHORT + повернення у bearish FVG/OB")
        elif bos_short_hold or bos_short_continuation:
            local -= 12
            entry_ok = True
            setup = "BOS_SHORT_CONTINUATION_HOLD"
            notes.append("BOS SHORT утримує пробитий рівень — continuation без повного ретесту")
        elif displacement_down and move_3 < -0.55:
            no_chase = True
            local += 10
            setup = "BOS_SHORT_WAIT_PULLBACK"
            notes.append("BOS SHORT, але імпульс вже відбувся — чекати FVG/OB")
        if premium:
            local -= 4
        score += local
        if score < 0:
            bias = "SHORT"

    # 3) FVG/OB reaction model without fresh BOS is weaker.
    # It must be in correct premium/discount area. Midrange alone is no trade.
    if bias == "NEUTRAL":
        if (in_bull_fvg or in_bull_ob) and discount:
            bias = "LONG"
            score = 16
            setup = "DISCOUNT_FVG_OB_LONG"
            entry_ok = True
            notes.append("discount + bullish FVG/OB")
        elif (in_bear_fvg or in_bear_ob) and premium:
            bias = "SHORT"
            score = -16
            setup = "PREMIUM_FVG_OB_SHORT"
            entry_ok = True
            notes.append("premium + bearish FVG/OB")
        elif midrange:
            bias = "NEUTRAL"
            score = 0
            setup = "BALANCE_MIDRANGE"
            notes.append("ціна біля equilibrium — ICT входу немає")
        elif discount:
            bias = "NEUTRAL"
            score = 0
            setup = "DISCOUNT_CONTEXT"
            notes.append("discount є, але немає sweep/FVG/OB реакції")
        elif premium:
            bias = "NEUTRAL"
            score = 0
            setup = "PREMIUM_CONTEXT"
            notes.append("premium є, але немає sweep/FVG/OB реакції")

    # Context penalty only. Premium/discount cannot create direction by itself.
    if bias == "LONG" and premium and not (swept_low or in_bull_fvg or in_bull_ob):
        score -= 6
        notes.append("лонг у premium — якість нижча")
    if bias == "SHORT" and discount and not (swept_high or in_bear_fvg or in_bear_ob):
        score += 6
        notes.append("шорт у discount — якість нижча")

    if score >= 16:
        bias = "LONG"
    elif score <= -16:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
        if setup == "NONE":
            setup = "CONTEXT_ONLY"

    return {
        "available": True,
        "bias": bias,
        "score": int(max(-38, min(38, score))),
        "state": "NO_CHASE" if no_chase else ("ENTRY_MODEL" if entry_ok else "CONTEXT"),
        "setup": setup,
        "entry_ok": bool(entry_ok and not no_chase),
        "no_chase": bool(no_chase),
        "equilibrium": round_price(eq),
        "range_high": round_price(range_high),
        "range_low": round_price(range_low),
        "premium_discount": "MIDRANGE" if midrange else ("DISCOUNT" if discount else "PREMIUM"),
        "pd_pos": round(pd_pos, 3),
        "bull_fvg": nearest_bull_fvg,
        "bear_fvg": nearest_bear_fvg,
        "bull_ob": bull_ob,
        "bear_ob": bear_ob,
        "note": "; ".join(notes[:4]) if notes else "ICT: контекст без готового сетапу",
    }

def build_pending_ict_zones(context):
    """Create forward-looking ICT zones for the next runs.

    These are not market orders. They are zones where the bot should watch for
    reaction/confirmation, so the message becomes predictive instead of late.
    """
    ict = context.get("ict") or {}
    price = context.get("price")
    atr15 = context.get("atr15") or (price or 90) * 0.006
    side = context.get("bias", "NEUTRAL")
    if not ict or not price:
        return []

    zones = []

    def add_zone(side_, zone, label, priority, condition, reason):
        if not zone or zone.get("mitigated"):
            return
        low = safe_float(zone.get("low"))
        high = safe_float(zone.get("high"))
        mid = safe_float(zone.get("mid"), (low + high) / 2 if low and high else None)
        if low is None or high is None:
            return
        low, high = min(low, high), max(low, high)
        distance_pct = abs(((low + high) / 2) - price) / price * 100 if price else 0
        zones.append({
            "side": side_,
            "type": label,
            "low": round_price(low),
            "high": round_price(high),
            "mid": round_price(mid),
            "priority": int(priority),
            "distance_pct": round(distance_pct, 3),
            "condition": condition,
            "reason": reason,
        })

    premium_discount = ict.get("premium_discount", "")
    ict_bias = ict.get("bias", "NEUTRAL")

    # Main forward zones from ICT model.
    add_zone(
        "LONG",
        ict.get("bull_fvg"),
        "Bullish FVG",
        82 if premium_discount == "DISCOUNT" else 58,
        "реакція в зоні + 3M перестає робити lower low + CVD/потік не проти",
        "покупка не після імпульсу, а на поверненні в imbalance",
    )
    add_zone(
        "LONG",
        ict.get("bull_ob"),
        "Bullish OB",
        78 if premium_discount == "DISCOUNT" else 64,
        "утримання OB + reclaim середини зони + стоп нижче OB",
        "order block для low-risk LONG",
    )
    add_zone(
        "SHORT",
        ict.get("bear_fvg"),
        "Bearish FVG",
        82 if premium_discount == "PREMIUM" else 58,
        "реакція в зоні + 3M перестає робити higher high + CVD/потік не проти",
        "продаж не після падіння, а на поверненні в imbalance",
    )
    add_zone(
        "SHORT",
        ict.get("bear_ob"),
        "Bearish OB",
        78 if premium_discount == "PREMIUM" else 64,
        "утримання OB + rejection середини зони + стоп вище OB",
        "order block для low-risk SHORT",
    )

    # Liquidity objectives / warning zones.
    eq = safe_float(ict.get("equilibrium"))
    rh = safe_float(ict.get("range_high"))
    rl = safe_float(ict.get("range_low"))
    if rh and rl and eq:
        if price < eq:
            zones.append({
                "side": "LONG",
                "type": "Discount → EQ",
                "low": round_price(rl),
                "high": round_price(eq),
                "mid": round_price((rl + eq) / 2),
                "priority": 58,
                "distance_pct": round(abs(eq - price) / price * 100, 3),
                "condition": "після sweep low або реакції в bullish FVG/OB",
                "reason": "ціна у discount, перша магніт-зона — equilibrium",
            })
        if price > eq:
            zones.append({
                "side": "SHORT",
                "type": "Premium → EQ",
                "low": round_price(eq),
                "high": round_price(rh),
                "mid": round_price((rh + eq) / 2),
                "priority": 58,
                "distance_pct": round(abs(price - eq) / price * 100, 3),
                "condition": "після sweep high або реакції в bearish FVG/OB",
                "reason": "ціна у premium, перша магніт-зона — equilibrium",
            })

    # Prefer zones aligned with current context, but keep opposite zones as warnings.
    for z in zones:
        if side in ["LONG", "SHORT"] and z["side"] == side:
            z["priority"] += 8
        elif ict_bias in ["LONG", "SHORT"] and z["side"] == ict_bias:
            z["priority"] += 5

        # Do not give high priority to zones that are too far for intraday.
        if z["distance_pct"] > 1.35:
            z["priority"] -= 12
        elif z["distance_pct"] <= 0.45:
            z["priority"] += 4

    zones = sorted(zones, key=lambda z: (z["priority"], -z["distance_pct"]), reverse=True)
    # Deduplicate similar zones.
    unique = []
    for z in zones:
        duplicate = False
        for u in unique:
            if z["side"] == u["side"] and abs((z["mid"] or 0) - (u["mid"] or 0)) <= atr15 * 0.12:
                duplicate = True
                break
        if not duplicate:
            unique.append(z)
    return unique[:4]


def pending_zones_text(context, limit=3):
    zones = context.get("pending_ict_zones") or []
    if not zones:
        return ""
    lines = ["<b>Pending ICT зони:</b>"]
    for z in zones[:limit]:
        side = z["side"]
        icon = "🟢" if side == "LONG" else "🔴"
        lines.append(
            f"{icon} {side}: {z['low']}–{z['high']} | {z['type']} | P{z['priority']}"
        )
    return "\n".join(lines)

def analyze_volume_guard(candles_15m):
    """Intraday volume context.

    Low volume is NOT a blocker. For BZ, quiet compression often comes right
    before a strong intraday move. Volume only adds quality when participation
    is clearly above normal.
    """
    if not candles_15m or len(candles_15m) < 23:
        return {
            "available": False,
            "ok": True,
            "score": 0,
            "ratio": 1.0,
            "state": "NO DATA",
            "note": "обсяг: даних мало",
        }

    vols = [max(0.0, float(c.volume or 0)) for c in candles_15m]
    avg20 = mean(vols[-23:-3]) if len(vols[-23:-3]) else 0
    avg3 = mean(vols[-3:]) if len(vols[-3:]) else 0
    ratio = (avg3 / avg20) if avg20 else 1.0

    if ratio >= VOLUME_STRONG_RATIO:
        score = 10
        state = "STRONG_VOLUME"
        note = f"обсяг сильний: {round(ratio * 100, 1)}% від середнього 20"
    elif ratio >= VOLUME_ACTIVE_RATIO:
        score = 6
        state = "ACTIVE_VOLUME"
        note = f"обсяг активний: {round(ratio * 100, 1)}% від середнього 20"
    else:
        score = 0
        state = "LOW_OR_NORMAL_VOLUME" if ratio < 1.0 else "NORMAL_VOLUME"
        note = f"обсяг без бонусу: {round(ratio * 100, 1)}% від середнього 20"

    return {
        "available": True,
        "ok": True,
        "score": score,
        "ratio": round(ratio, 3),
        "state": state,
        "note": note,
    }


def analyze_micro(candles):
    if not candles or len(candles) < 35:
        return {"available": False, "bias": "NEUTRAL", "score": 0, "state": "NO DATA", "note": "3m недоступний"}
    recent = candles[-28:]
    fast = candles[-12:]
    last = candles[-1]
    closes = [c.close for c in recent]
    fast_closes = [c.close for c in fast]
    fast_highs = [c.high for c in fast]
    fast_lows = [c.low for c in fast]
    fast_move = pct(fast_closes[-1], fast_closes[0])
    lower_closes = sum(1 for i in range(1, len(fast_closes)) if fast_closes[i] < fast_closes[i - 1])
    higher_closes = sum(1 for i in range(1, len(fast_closes)) if fast_closes[i] > fast_closes[i - 1])
    lower_highs = sum(1 for i in range(1, len(fast_highs)) if fast_highs[i] < fast_highs[i - 1])
    higher_lows = sum(1 for i in range(1, len(fast_lows)) if fast_lows[i] > fast_lows[i - 1])
    red = sum(1 for c in fast if c.close < c.open)
    green = sum(1 for c in fast if c.close > c.open)
    drift = slope_pct(closes, 10)
    avg_vol = mean([c.volume for c in recent[:-1]]) if len(recent) > 1 else 0
    vol_ratio = last.volume / avg_vol if avg_vol else 1

    score = 0
    notes = []
    if fast_move >= 0.28:
        score += 26
        notes.append("3m швидко йде вгору")
    elif fast_move <= -0.28:
        score -= 26
        notes.append("3m швидко йде вниз")

    if higher_closes >= 7 or higher_lows >= 5:
        score += 22
        notes.append("3m higher lows/closes")
    if lower_closes >= 7 or lower_highs >= 5:
        score -= 22
        notes.append("3m lower highs/closes")

    if green >= 8:
        score += 10
    elif red >= 8:
        score -= 10

    if drift >= 0.18:
        score += 10
    elif drift <= -0.18:
        score -= 10

    if vol_ratio >= 1.5 and candle_body_ratio(last) >= 0.45:
        if last.close > last.open:
            score += 8
            notes.append("обсяг за покупців")
        else:
            score -= 8
            notes.append("обсяг за продавців")

    if score >= 22:
        bias = "LONG"
        state = "LONG_STRENGTHENING"
    elif score <= -22:
        bias = "SHORT"
        state = "SHORT_STRENGTHENING"
    else:
        bias = "NEUTRAL"
        state = "RANGE"
        total_move = pct(closes[-1], closes[0])
        if total_move >= 0.45 and drift < 0:
            state = "LONG_COOLING"
            notes.append("покупці охолоджуються")
        elif total_move <= -0.45 and drift > 0:
            state = "SHORT_COOLING"
            notes.append("продавці охолоджуються")

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "state": state,
        "fast_move_pct": round(fast_move, 3),
        "drift_pct": round(drift, 3),
        "vol_ratio": round(vol_ratio, 2),
        "note": "; ".join(notes[:3]) if notes else "3m без чіткого тригера",
    }


def analyze_flow(trades, book, price):
    buy_vol = sum(t["size"] for t in trades if t.get("side") == "buy")
    sell_vol = sum(t["size"] for t in trades if t.get("side") == "sell")
    total = buy_vol + sell_vol
    delta = (buy_vol - sell_vol) / total * 100 if total else 0

    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    near_bid = near_ask = 0
    wall = ""
    if bids and asks and price:
        lower = price * 0.996
        upper = price * 1.004
        near_bids = [(p, s) for p, s in bids if p >= lower]
        near_asks = [(p, s) for p, s in asks if p <= upper]
        near_bid = sum(s for _, s in near_bids)
        near_ask = sum(s for _, s in near_asks)
        biggest_bid = max(near_bids, key=lambda x: x[1]) if near_bids else None
        biggest_ask = max(near_asks, key=lambda x: x[1]) if near_asks else None
        if biggest_bid and biggest_ask:
            if biggest_bid[1] > biggest_ask[1] * 1.8:
                wall = f"підтримка покупців біля {round_price(biggest_bid[0])}"
            elif biggest_ask[1] > biggest_bid[1] * 1.8:
                wall = f"опір продавців біля {round_price(biggest_ask[0])}"

    book_total = near_bid + near_ask
    book_delta = (near_bid - near_ask) / book_total * 100 if book_total else 0
    score = 0
    notes = []
    if delta >= 18:
        score += 16
        notes.append("угоди за покупців")
    elif delta <= -18:
        score -= 16
        notes.append("угоди за продавців")
    elif delta >= 8:
        score += 7
    elif delta <= -8:
        score -= 7

    if book_delta >= 18:
        score += 10
        notes.append("стакан підтримує покупців")
    elif book_delta <= -18:
        score -= 10
        notes.append("стакан тисне продавцями")
    if wall:
        notes.append(wall)

    if score >= 15:
        bias = "LONG"
    elif score <= -15:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "trade_delta_pct": round(delta, 2),
        "book_delta_pct": round(book_delta, 2),
        "note": "; ".join(notes[:3]) if notes else "потік без явної переваги",
    }



def analyze_cvd(trades, candles_3m, price, previous_snapshot=None):
    """Persistent CVD proxy from OKX public trades.

    Public trades are limited, so every run adds the current trade delta to the
    stored CVD in state. To reduce double-counting overlapping trades between
    15-minute runs, only trades newer than the previous saved trade timestamp
    are added when timestamps are available.
    """
    previous_snapshot = previous_snapshot or {}
    prev_cvd = safe_float(previous_snapshot.get("cvd"), 0) or 0
    prev_trade_ts = int(previous_snapshot.get("last_trade_ts") or 0)
    prev_cvd_time = previous_snapshot.get("cvd_time") or previous_snapshot.get("time")

    cvd_stale = False
    try:
        if prev_cvd_time:
            dt = datetime.fromisoformat(str(prev_cvd_time).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (now_utc() - dt.astimezone(timezone.utc)).total_seconds() / 60
            cvd_stale = age_min > CVD_STATE_MAX_AGE_MINUTES
    except Exception:
        cvd_stale = False

    if cvd_stale:
        prev_cvd = 0
        prev_trade_ts = 0

    fresh_trades = []
    for t in trades or []:
        ts = int(t.get("ts") or 0)
        if prev_trade_ts and ts and ts <= prev_trade_ts:
            continue
        fresh_trades.append(t)
    if not fresh_trades and trades:
        fresh_trades = trades

    buy_vol = sum(t.get("size", 0) for t in fresh_trades if t.get("side") == "buy")
    sell_vol = sum(t.get("size", 0) for t in fresh_trades if t.get("side") == "sell")
    delta = buy_vol - sell_vol
    total = buy_vol + sell_vol
    delta_pct = (delta / total * 100) if total else 0
    cvd_value = prev_cvd + delta
    last_trade_ts = max([int(t.get("ts") or 0) for t in trades or []] or [prev_trade_ts])

    price_move = 0
    if candles_3m and len(candles_3m) >= 10:
        price_move = pct(candles_3m[-1].close, candles_3m[-10].close)

    cvd_change = delta
    cvd_change_pct = 0
    if abs(prev_cvd) > 1e-9:
        cvd_change_pct = cvd_change / abs(prev_cvd) * 100

    score = 0
    state = "BALANCED"
    notes = []

    if delta_pct >= 18:
        score += 14
        state = "BUYERS_DOMINATE"
        notes.append("CVD росте")
    elif delta_pct <= -18:
        score -= 14
        state = "SELLERS_DOMINATE"
        notes.append("CVD падає")

    if price_move <= -0.35 and delta_pct >= 10:
        score += 18
        state = "BULLISH_ABSORPTION"
        notes.append("ціна вниз, CVD вгору")
    elif price_move >= 0.35 and delta_pct <= -10:
        score -= 18
        state = "BEARISH_ABSORPTION"
        notes.append("ціна вгору, CVD вниз")

    sample_size = len(fresh_trades)
    confidence = "HIGH"
    confidence_factor = 1.0
    if sample_size < max(20, int(CVD_MIN_FRESH_TRADES * 0.5)):
        confidence = "LOW"
        confidence_factor = CVD_LOW_CONFIDENCE_FACTOR
        notes.append("CVD low confidence: мало свіжих трейдів")
    elif sample_size < CVD_MIN_FRESH_TRADES:
        confidence = "MEDIUM"
        confidence_factor = CVD_MEDIUM_CONFIDENCE_FACTOR
        notes.append("CVD medium confidence: вибірка обмежена")

    score = int(score * confidence_factor)

    if score >= 16:
        bias = "LONG"
    elif score <= -16:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "state": state,
        "confidence": confidence,
        "confidence_factor": round(confidence_factor, 2),
        "sample_size": sample_size,
        "delta": round(delta, 4),
        "delta_pct": round(delta_pct, 2),
        "cvd": round(cvd_value, 4),
        "cvd_change_pct": round(cvd_change_pct, 3),
        "last_trade_ts": last_trade_ts,
        "price_move_pct": round(price_move, 3),
        "note": "; ".join(notes[:4]) if notes else "CVD без явної переваги",
    }


def analyze_clusters(trades, book, price):
    """Lightweight cluster / footprint proxy for intraday BZ trading.

    Important: this is NOT a real footprint chart. It uses public OKX trades and
    the order book as a local microstructure hint. Therefore clusters are a
    small helper, not a trade blocker. They can warn about a local buyer/seller,
    but they must not overrule 15m structure, 3m trigger, CVD, or OI.
    """
    if not trades or not price:
        return {"bias": "NEUTRAL", "score": 0, "state": "NO DATA", "zones": [], "note": "кластерних даних мало"}

    bin_size = max(price * 0.0008, 0.01)
    clusters = {}
    for t in trades:
        px = safe_float(t.get("price"), None)
        size = safe_float(t.get("size"), 0) or 0
        side = t.get("side")
        if px is None or abs(px - price) / price > 0.012:
            continue
        level = round(round(px / bin_size) * bin_size, 4)
        row = clusters.setdefault(level, {"buy": 0.0, "sell": 0.0, "total": 0.0})
        if side == "buy":
            row["buy"] += size
        elif side == "sell":
            row["sell"] += size
        row["total"] += size

    if not clusters:
        return {"bias": "NEUTRAL", "score": 0, "state": "NO CLUSTERS", "zones": [], "note": "немає активних кластерів біля ціни"}

    zones = []
    for level, row in clusters.items():
        total = row["total"]
        if total <= 0:
            continue
        imbalance = (row["buy"] - row["sell"]) / total * 100
        if abs(imbalance) >= 22:
            zones.append({"price": level, "imbalance_pct": round(imbalance, 1), "volume": round(total, 4)})
    zones.sort(key=lambda x: x["volume"], reverse=True)

    top = zones[0] if zones else None
    score = 0
    state = "BALANCED"
    notes = []
    if top:
        below = top["price"] <= price
        imb = top["imbalance_pct"]
        # Cluster influence is intentionally capped. It is a warning/helper.
        if below and imb > 0:
            score += 5
            state = "LOCAL_BUYER_SUPPORT"
            notes.append(f"локальний покупець біля {top['price']}")
        elif (not below) and imb < 0:
            score -= 5
            state = "LOCAL_SELLER_RESISTANCE"
            notes.append(f"локальний продавець біля {top['price']}")
        elif below and imb < 0:
            score -= 3
            state = "LOCAL_SELL_PRESSURE_BELOW"
            notes.append("локальні продажі нижче ціни")
        elif (not below) and imb > 0:
            score += 3
            state = "LOCAL_BUY_PRESSURE_ABOVE"
            notes.append("локальні покупки вище ціни")

    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    if bids and asks:
        bid_wall = max(bids[:15], key=lambda x: x[1], default=None)
        ask_wall = max(asks[:15], key=lambda x: x[1], default=None)
        if bid_wall and ask_wall:
            if bid_wall[1] > ask_wall[1] * 2:
                score += 2
                notes.append(f"bid-стіна {round_price(bid_wall[0])}")
            elif ask_wall[1] > bid_wall[1] * 2:
                score -= 2
                notes.append(f"ask-стіна {round_price(ask_wall[0])}")

    score = max(-8, min(8, int(score)))
    if score >= 5:
        bias = "LONG"
    elif score <= -5:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": score,
        "state": state,
        "zones": zones[:3],
        "note": "; ".join(notes[:3]) if notes else "кластери без явної переваги",
    }

def analyze_derivatives(open_interest, funding, price, previous_snapshot=None):
    previous_snapshot = previous_snapshot or {}
    oi_value = safe_float((open_interest or {}).get("oi_ccy"), None)
    if not oi_value:
        oi_value = safe_float((open_interest or {}).get("oi"), None)
    prev_oi = safe_float(previous_snapshot.get("oi"), None)
    prev_price = safe_float(previous_snapshot.get("price"), None)
    funding_rate = safe_float((funding or {}).get("funding_rate"), 0) or 0

    oi_change = pct(oi_value, prev_oi) if oi_value and prev_oi else 0
    price_change = pct(price, prev_price) if price and prev_price else 0
    funding_pct = funding_rate * 100

    score = 0
    state = "POSITIONING_UNKNOWN"
    notes = []

    if oi_value and prev_oi and abs(oi_change) >= 0.35 and abs(price_change) >= 0.18:
        if price_change > 0 and oi_change > 0:
            score += 18
            state = "LONG_BUILDUP"
            notes.append("ціна росте + OI росте — нові лонги/попит")
        elif price_change < 0 and oi_change > 0:
            score -= 18
            state = "SHORT_BUILDUP"
            notes.append("ціна падає + OI росте — нові шорти/тиск")
        elif price_change > 0 and oi_change < 0:
            score += 6
            state = "SHORTS_CLOSING"
            notes.append("ріст на падінні OI — шорти закриваються, не доганяти без ретесту")
        elif price_change < 0 and oi_change < 0:
            score -= 6
            state = "LONGS_CLOSING"
            notes.append("падіння на падінні OI — лонги закриваються, не доганяти без ретесту")
    else:
        notes.append("OI ще накопичує історію для порівняння")

    if funding_pct >= 0.035:
        score -= 6
        notes.append("funding перегрітий у лонг — лонг не доганяти")
    elif funding_pct <= -0.035:
        score += 6
        notes.append("funding перегрітий у шорт — шорт не доганяти")

    if score >= 12:
        bias = "LONG"
    elif score <= -12:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "score": int(score),
        "state": state,
        "oi": round(oi_value, 4) if oi_value is not None else None,
        "oi_change_pct": round(oi_change, 3),
        "funding_pct": round(funding_pct, 5),
        "price_change_pct": round(price_change, 3),
        "note": "; ".join(notes[:3]),
    }

def analyze_liquidations(candles_3m, candles_15m, flow, structure, price):
    """Liquidation / stop-run proxy from public data.

    We do not have exchange liquidation feed here, so this uses:
    - fast candle move
    - volume spike
    - candle close location
    - SMC sweep
    - trades/book flow

    It is a practical proxy for: long liquidation, short squeeze, high/low sweep,
    and post-sweep reclaim.
    """
    candles = candles_3m if candles_3m and len(candles_3m) >= 25 else candles_15m
    if not candles or len(candles) < 25 or not price:
        return {
            "available": False,
            "bias": "NEUTRAL",
            "score": 0,
            "event": "NO DATA",
            "blocks": [],
            "note": "даних для ліквідацій мало",
        }

    recent = candles[-24:]
    last = recent[-1]
    prev = recent[-2]
    avg_vol = mean([c.volume for c in recent[:-1]]) if len(recent) > 1 else 0
    vol_ratio = last.volume / avg_vol if avg_vol else 1.0
    move_1 = pct(last.close, prev.close)
    move_6 = pct(last.close, recent[-7].close) if len(recent) >= 7 else move_1
    body = last.close - last.open
    close_pos = close_location(last)
    prev_high = max(c.high for c in recent[:-1])
    prev_low = min(c.low for c in recent[:-1])

    flow_bias = (flow or {}).get("bias", "NEUTRAL")
    phase = (structure or {}).get("phase", "")
    score = 0
    event = "QUIET"
    blocks = []
    notes = []

    downside_sweep = last.low < prev_low and last.close > prev_low
    upside_sweep = last.high > prev_high and last.close < prev_high

    if downside_sweep or phase == "DOWNSIDE SWEEP":
        event = "DOWNSIDE SWEEP / LONG RECLAIM"
        score += 18
        blocks.append("SHORT")
        notes.append("зняли low і повернулись вище — шорт не доганяти")
        if flow_bias == "LONG":
            score += 6
            notes.append("покупці відкуповують після зняття low")

    elif upside_sweep or phase == "UPSIDE SWEEP":
        event = "UPSIDE SWEEP / SHORT RECLAIM"
        score -= 18
        blocks.append("LONG")
        notes.append("зняли high і закрились нижче — лонг не доганяти")
        if flow_bias == "SHORT":
            score -= 6
            notes.append("продавці тиснуть після зняття high")

    strong_down = (move_1 <= -0.42 or move_6 <= -0.70) and vol_ratio >= 1.35 and body < 0
    strong_up = (move_1 >= 0.42 or move_6 >= 0.70) and vol_ratio >= 1.35 and body > 0

    if strong_down:
        if close_pos <= 0.28 and flow_bias != "LONG":
            event = "LONG LIQUIDATION"
            score -= 18
            notes.append("дамп на обсязі — ймовірно вибивають лонги")
        elif close_pos >= 0.52 or flow_bias == "LONG":
            event = "LONG LIQUIDATION ABSORBED"
            score += 10
            blocks.append("SHORT")
            notes.append("дамп викупили — новий шорт тільки після ретесту")

    if strong_up:
        if close_pos >= 0.72 and flow_bias != "SHORT":
            event = "SHORT SQUEEZE"
            score += 18
            notes.append("памп на обсязі — ймовірно шорт-сквіз")
        elif close_pos <= 0.48 or flow_bias == "SHORT":
            event = "SHORT SQUEEZE ABSORBED"
            score -= 10
            blocks.append("LONG")
            notes.append("памп поглинули — новий лонг тільки після ретесту")

    if score >= 12:
        bias = "LONG"
    elif score <= -12:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "available": True,
        "bias": bias,
        "score": int(score),
        "event": event,
        "blocks": sorted(set(blocks)),
        "vol_ratio": round(vol_ratio, 2),
        "move_1_pct": round(move_1, 3),
        "move_6_pct": round(move_6, 3),
        "note": "; ".join(notes[:3]) if notes else "ліквідаційного тиску не видно",
    }


def market_session():
    hour = now_utc().hour
    # BZU is much cleaner during London/NY. Asia/quiet hours are not forbidden,
    # but entries must be treated as lower-liquidity / higher slippage risk.
    if 12 <= hour < 20:
        return {"name": "NEW YORK / LONDON", "score": 4, "liquidity_risk": False, "note": "ліквідна сесія"}
    if 7 <= hour < 12:
        return {"name": "LONDON", "score": 2, "liquidity_risk": False, "note": "європейська сесія"}
    return {"name": "ASIA / QUIET", "score": -6, "liquidity_risk": True, "note": "тиха сесія: ризик спреду/slippage"}


# ==========================================================
# NEWS
# ==========================================================


def parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        dt = parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalized(text):
    return clean_text(text).lower().replace("’", "'").replace("–", "-").replace("—", "-")


def parse_google_rss(query, lookback_hours=3):
    url = f"https://news.google.com/rss/search?q={quote_plus(query + f' when:{lookback_hours}h')}&hl=en-US&gl=US&ceid=US:en"
    response = http_get(url, timeout=10, retries=1)
    if not response:
        return []
    cutoff = now_utc() - timedelta(hours=lookback_hours)
    items = []
    try:
        root = ET.fromstring(response.content)
        for node in root.findall(".//item")[:12]:
            title = clean_text(node.findtext("title", ""))
            link = node.findtext("link", "") or ""
            dt = parse_date(node.findtext("pubDate", ""))
            if dt and dt < cutoff:
                continue
            if title:
                items.append({"title": title, "link": link, "published_at": dt, "source": "Google News"})
    except Exception as error:
        print(f"[WARN] Google RSS parse failed: {error}")
    return items


def dedupe_news(items):
    seen = set()
    out = []
    for item in items:
        key = re.sub(r"[^a-z0-9а-яіїєґ]+", " ", normalized(item.get("title", "")))[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda x: x.get("published_at") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return out


OIL_NEWS_CORE_TERMS = [
    "brent", "crude", "oil price", "oil prices", "petroleum", "opec", "opec+",
    "eia", "api crude", "crude inventories", "oil inventories", "stockpiles",
    "refinery", "refining", "gasoline inventories", "distillate", "hormuz",
    "oil supply", "oil demand", "energy market", "barrel", "bpd",
]

OIL_NEWS_CONTEXT_TERMS = [
    "iran", "russia", "sanctions", "middle east", "fed", "powell", "dollar",
    "yields", "tariff", "ceasefire", "strike", "attack", "production", "output",
]


def oil_news_relevance(title):
    """Return a strict oil-market relevance score for a headline.

    Reuters attribution alone is never relevance. Broad macro/geopolitical words
    count only when the headline also contains a direct oil/energy anchor. This
    prevents unrelated headlines (for example pharma news) from creating an oil
    NEWS_SHOCK or directional bias.
    """
    text = normalized(title)
    direct_hits = sum(1 for term in OIL_NEWS_CORE_TERMS if term in text)
    context_hits = sum(1 for term in OIL_NEWS_CONTEXT_TERMS if term in text)
    if direct_hits <= 0:
        return 0
    return int(min(6, direct_hits * 2 + min(2, context_hits)))


def get_news():
    """News feed for the bot.

    Reuters is prioritized, but not exclusive. For oil/BZ intraday trading,
    official EIA/Fed/OPEC/event headlines can matter even when Reuters has not
    republished them yet. Reuters items are marked and sorted first.
    """
    items = []

    if REUTERS_PRIORITY_NEWS:
        reuters_queries = [
            "site:reuters.com oil Brent crude OPEC EIA inventories",
            "site:reuters.com oil prices Brent crude today",
            "site:reuters.com EIA crude inventories API crude",
            "site:reuters.com OPEC production cut increase oil",
            "site:reuters.com Iran sanctions Hormuz oil",
            "site:reuters.com Fed Powell dollar oil prices",
        ]
        for query in reuters_queries:
            items.extend(parse_google_rss(query, 6))

    for query in NEWS_QUERIES:
        items.extend(parse_google_rss(query, 3))

    out = []
    for item in dedupe_news(items):
        title = normalized(item.get("title", ""))
        link = normalized(item.get("link", ""))
        is_reuters = "reuters" in title or "reuters.com" in link
        relevance = oil_news_relevance(item.get("title", ""))
        if relevance <= 0:
            continue
        item["source"] = "Reuters" if is_reuters else item.get("source", "Google News")
        item["priority"] = 1 if is_reuters else 0
        item["oil_relevance"] = relevance
        out.append(item)

    out.sort(key=lambda x: (x.get("priority", 0), x.get("published_at") or datetime(1970, 1, 1, tzinfo=timezone.utc)), reverse=True)
    return out


def score_news_item(title):
    text = normalized(title)
    relevance = oil_news_relevance(title)
    if relevance <= 0:
        return 0, 0
    long_hits = sum(1 for p in LONG_NEWS_PHRASES if p in text)
    short_hits = sum(1 for p in SHORT_NEWS_PHRASES if p in text)
    impact = sum(1 for p in HIGH_IMPACT_WORDS if p in text)

    score = 0
    if long_hits:
        score += long_hits * (12 + min(6, impact))
    if short_hits:
        score -= short_hits * (12 + min(6, impact))
    # Source quality can strengthen a relevant oil headline, never create relevance.
    if "reuters" in text:
        score = int(score * 1.2)
    score = int(score * min(1.25, 0.85 + relevance * 0.07))
    return score, max(impact, relevance)


def _news_event_profile(title):
    """Classify a headline and define how long its first reaction may dominate.

    The windows are deliberately event-specific. Weekly inventory/macro prints
    are usually digested faster than structural supply-policy or geopolitical
    changes. Even structural news loses its hard veto when price rejects it.
    """
    text = normalized(title)
    if any(x in text for x in ["inventory", "inventories", "stockpiles", "eia", "api crude", "crude draw", "crude build"]):
        return {"event_class": "INVENTORY", "hard_minutes": 30, "context_minutes": 90, "half_life_minutes": 45}
    if any(x in text for x in ["fed", "powell", "fomc", "cpi", "interest rate", "rates", "dollar", "yields"]):
        return {"event_class": "MACRO", "hard_minutes": 30, "context_minutes": 120, "half_life_minutes": 60}
    if any(x in text for x in ["opec", "production cut", "output cut", "production increase", "output increase", "quota"]):
        return {"event_class": "SUPPLY_POLICY", "hard_minutes": NEWS_REACTION_STRUCTURAL_HARD_MINUTES, "context_minutes": 240, "half_life_minutes": 120}
    if any(x in text for x in ["hormuz", "sanctions", "attack", "strike", "ceasefire", "cease-fire", "truce", "war", "supply disruption"]):
        return {"event_class": "GEOPOLITICAL", "hard_minutes": NEWS_REACTION_STRUCTURAL_HARD_MINUTES, "context_minutes": 240, "half_life_minutes": 120}
    if any(x in text for x in ["surplus", "oversupply", "demand weak", "forecast", "outlook", "iea", "demand growth"]):
        return {"event_class": "OUTLOOK", "hard_minutes": NEWS_REACTION_STANDARD_HARD_MINUTES, "context_minutes": 120, "half_life_minutes": 60}
    return {"event_class": "GENERAL", "hard_minutes": NEWS_REACTION_STANDARD_HARD_MINUTES, "context_minutes": 90, "half_life_minutes": 45}


def _news_memory_key(title):
    return re.sub(r"[^a-z0-9а-яіїєґ]+", " ", normalized(title)).strip()[:180]


def _news_reference_from_candles(candles, event_dt, current_price):
    closed = closed_candles(candles or [], 3, min_required=1)
    if not closed or event_dt is None:
        return safe_float(current_price), False
    event_ms = int(event_dt.timestamp() * 1000)
    before = [c for c in closed if int(c.ts) <= event_ms]
    after = [c for c in closed if int(c.ts) > event_ms]
    candidate = before[-1] if before else (after[0] if after else None)
    if candidate is None:
        return safe_float(current_price), False
    distance_min = abs(int(candidate.ts) - event_ms) / 60000.0
    # If the feed headline is older than the available intraday candles, do not
    # invent a market reaction from the current price.
    if distance_min > 35:
        return safe_float(current_price), False
    return safe_float(candidate.close, current_price), True


def _news_market_reaction(base_score, event_dt, reference_price, candles_3m, candles_15m, current_price, age_min, profile, reaction_known=True):
    direction = 1 if base_score > 0 else -1
    reference_price = safe_float(reference_price)
    current_price = safe_float(current_price)
    atr15 = atr(closed_candles(candles_15m or [], 15, min_required=2))
    if not atr15 and current_price:
        atr15 = current_price * 0.005
    threshold_pct = max(
        NEWS_REACTION_MIN_MOVE_PCT,
        (safe_float(atr15, 0.0) / current_price * 100.0 * NEWS_REACTION_CONFIRM_ATR15) if current_price else NEWS_REACTION_MIN_MOVE_PCT,
    )
    reject_threshold_pct = max(
        NEWS_REACTION_MIN_MOVE_PCT * 0.8,
        (safe_float(atr15, 0.0) / current_price * 100.0 * NEWS_REACTION_REJECT_ATR15) if current_price else NEWS_REACTION_MIN_MOVE_PCT * 0.8,
    )
    if not reaction_known or not reference_price or not current_price or event_dt is None:
        return {
            "status": "UNVERIFIED", "reaction_known": False, "current_signed_pct": 0.0,
            "max_favorable_pct": 0.0, "max_adverse_pct": 0.0, "giveback_ratio": 0.0,
            "acceptance_closes": 0, "opposite_closes": 0,
            "threshold_pct": round(threshold_pct, 3), "hard_block": bool(age_min <= profile["hard_minutes"]),
        }

    event_ms = int(event_dt.timestamp() * 1000)
    sequence = [c for c in closed_candles(candles_3m or [], 3, min_required=1) if int(c.ts) >= event_ms - 180000]
    if not sequence:
        return {
            "status": "UNVERIFIED", "reaction_known": False, "current_signed_pct": 0.0,
            "max_favorable_pct": 0.0, "max_adverse_pct": 0.0, "giveback_ratio": 0.0,
            "acceptance_closes": 0, "opposite_closes": 0,
            "threshold_pct": round(threshold_pct, 3), "hard_block": bool(age_min <= profile["hard_minutes"]),
        }

    def signed_move(price):
        return direction * pct(float(price), reference_price)

    current_signed = signed_move(current_price)
    favorable_values = []
    adverse_values = []
    signed_closes = []
    for candle in sequence:
        favorable_price = candle.high if direction > 0 else candle.low
        adverse_price = candle.low if direction > 0 else candle.high
        favorable_values.append(signed_move(favorable_price))
        adverse_values.append(-signed_move(adverse_price))
        signed_closes.append(signed_move(candle.close))
    max_favorable = max([0.0] + favorable_values)
    max_adverse = max([0.0] + adverse_values)
    last_closes = signed_closes[-3:]
    acceptance = sum(1 for move in last_closes if move >= threshold_pct * 0.60)
    opposite_acceptance = sum(1 for move in last_closes if move <= -reject_threshold_pct * 0.60)
    giveback = max(0.0, (max_favorable - current_signed) / max(max_favorable, 1e-9)) if max_favorable > 0 else 0.0

    confirmed = bool(current_signed >= threshold_pct and acceptance >= NEWS_REACTION_MIN_ACCEPTANCE_CLOSES)
    rejected = bool(current_signed <= -reject_threshold_pct and opposite_acceptance >= NEWS_REACTION_MIN_ACCEPTANCE_CLOSES)
    absorbed = bool(
        age_min >= min(20, profile["hard_minutes"])
        and max_favorable >= threshold_pct
        and giveback >= NEWS_REACTION_DIGESTED_GIVEBACK
        and current_signed < threshold_pct * 0.35
    )
    digested = bool(
        age_min > profile["hard_minutes"]
        and len(sequence) >= 6
        and abs(current_signed) < threshold_pct * 0.45
        and not confirmed
    )

    if rejected:
        status = "REJECTED"
    elif absorbed:
        status = "ABSORBED"
    elif confirmed:
        status = "CONFIRMED"
    elif digested:
        status = "DIGESTED"
    elif age_min <= profile["hard_minutes"]:
        status = "FRESH"
    else:
        status = "UNRESOLVED"

    hard_block = bool(
        status == "CONFIRMED"
        or (age_min <= profile["hard_minutes"] and status not in {"REJECTED", "ABSORBED"})
    )
    return {
        "status": status, "reaction_known": True,
        "current_signed_pct": round(current_signed, 3),
        "max_favorable_pct": round(max_favorable, 3),
        "max_adverse_pct": round(max_adverse, 3),
        "giveback_ratio": round(giveback, 3),
        "acceptance_closes": int(acceptance), "opposite_closes": int(opposite_acceptance),
        "threshold_pct": round(threshold_pct, 3), "hard_block": hard_block,
    }


def _news_lifecycle_label(status):
    return {
        "FRESH": "первинна реакція",
        "CONFIRMED": "підтверджена ціною",
        "REJECTED": "відхилена ринком",
        "ABSORBED": "поглинута ринком",
        "DIGESTED": "вже відіграна",
        "UNRESOLVED": "не отримала продовження",
        "UNVERIFIED": "реакцію не підтверджено",
        "NONE": "немає активної реакції",
    }.get(str(status or "NONE").upper(), str(status or "NONE"))


def _news_lifecycle_factor(age_min, profile, reaction):
    half_life = max(1.0, float(profile.get("half_life_minutes", 45)))
    time_factor = math.exp(-math.log(2.0) * max(0.0, age_min) / half_life)
    status = str((reaction or {}).get("status") or "UNVERIFIED")
    if age_min > float(profile.get("context_minutes", 90)):
        return 0.0
    if status == "CONFIRMED":
        return max(0.55, time_factor)
    if status in {"REJECTED", "ABSORBED", "DIGESTED"}:
        return min(0.18, time_factor * 0.25)
    if status == "UNRESOLVED" and age_min > float(profile.get("hard_minutes", 30)):
        return min(NEWS_REACTION_UNVERIFIED_FACTOR, time_factor * 0.45)
    if status == "UNVERIFIED" and age_min > float(profile.get("hard_minutes", 30)):
        return min(NEWS_REACTION_UNVERIFIED_FACTOR, time_factor * 0.35)
    if age_min <= float(profile.get("hard_minutes", 30)):
        return max(0.72, time_factor)
    return min(0.45, time_factor)


def analyze_news(items, candles_3m=None, candles_15m=None, current_price=None, state=None):
    """Score oil news by source, age and the market's observed reaction.

    First 30-45 minutes: a material headline may block the opposite news trade.
    Afterwards: only confirmed price acceptance keeps that block. If price has
    absorbed, rejected or fully digested the headline, technical structure gets
    priority and the old headline becomes low-weight context.
    """
    raw = 0
    effective_raw = 0
    blocking_raw = 0
    important = []
    long_count = 0
    short_count = 0
    newest_age_min = None
    now = now_utc()
    memory = state.setdefault("news_reaction_memory", {}) if isinstance(state, dict) else {}

    # Keep state compact and discard old headlines.
    for key, value in list(memory.items()):
        try:
            seen = parse_date(value.get("first_seen_at")) if isinstance(value, dict) else None
            if not seen or (now - seen).total_seconds() > NEWS_REACTION_MEMORY_HOURS * 3600:
                memory.pop(key, None)
        except Exception:
            memory.pop(key, None)

    for item in items[:35]:
        title = item.get("title", "")
        base_score, impact = score_news_item(title)
        if not base_score:
            continue
        published = item.get("published_at")
        key = _news_memory_key(title)
        remembered = memory.get(key) if isinstance(memory.get(key), dict) else {}
        first_seen = parse_date(remembered.get("first_seen_at")) if remembered else None
        if first_seen is None:
            first_seen = now
        event_dt = published.astimezone(timezone.utc) if published else first_seen
        age_min = max(0.0, (now - event_dt).total_seconds() / 60.0)
        newest_age_min = age_min if newest_age_min is None else min(newest_age_min, age_min)
        profile = _news_event_profile(title)

        reference_price = safe_float(remembered.get("reference_price"), None)
        reaction_known = bool(remembered.get("reaction_known", False))
        if reference_price is None:
            reference_price, reaction_known = _news_reference_from_candles(candles_3m, event_dt, current_price)
        reaction = _news_market_reaction(
            base_score, event_dt, reference_price, candles_3m, candles_15m,
            current_price, age_min, profile, reaction_known=reaction_known,
        )
        factor = _news_lifecycle_factor(age_min, profile, reaction)
        item_score = int(round(base_score * factor))
        raw += base_score
        effective_raw += item_score
        if reaction.get("hard_block"):
            # Hard-block strength represents a still-live catalyst. Do not let
            # ordinary time decay silently erase a headline that price is still
            # accepting; confirmed reaction keeps at least 80% directional force.
            blocking_item_score = int(round(base_score * max(factor, 0.80)))
            blocking_raw += blocking_item_score
        if item_score > 0:
            long_count += 1
        elif item_score < 0:
            short_count += 1

        memory[key] = {
            "first_seen_at": first_seen.isoformat(),
            "published_at": event_dt.isoformat(),
            "reference_price": round_price(reference_price),
            "reaction_known": bool(reaction.get("reaction_known")),
            "last_seen_at": now.isoformat(),
            "last_status": reaction.get("status"),
        }
        enriched = {
            **item, "base_score": base_score, "score": item_score,
            "age_min": round(age_min, 1), "age_factor": round(factor, 3),
            "event_class": profile.get("event_class"),
            "hard_window_min": profile.get("hard_minutes"),
            "lifecycle": reaction.get("status"),
            "hard_block": bool(reaction.get("hard_block")),
            "reaction": reaction,
        }
        if impact or abs(item_score) >= 8:
            important.append(enriched)

    score = max(-45, min(45, int(effective_raw)))
    blocking_score = max(-45, min(45, int(blocking_raw)))
    if score >= 18:
        bias = "LONG"
    elif score <= -18:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"
    if blocking_score >= NEWS_DIRECTIONAL_MIN_SCORE:
        blocking_bias = "LONG"
    elif blocking_score <= -NEWS_DIRECTIONAL_MIN_SCORE:
        blocking_bias = "SHORT"
    else:
        blocking_bias = "NEUTRAL"

    important.sort(key=lambda x: (bool(x.get("hard_block")), abs(int(x.get("score", 0) or 0)), -(safe_float(x.get("age_min"), 9999) or 9999)), reverse=True)
    top_item = important[0] if important else None
    top = top_item.get("title") if top_item else "свіжого сильного драйвера немає"
    top_age = safe_float(top_item.get("age_min"), None) if top_item else None
    top_lifecycle = str(top_item.get("lifecycle") or "NONE") if top_item else "NONE"
    top_lifecycle_label = _news_lifecycle_label(top_lifecycle)
    hard_block_active = blocking_bias in {"LONG", "SHORT"}
    headline_short = clean_text(top)[:110]
    if hard_block_active and top_item:
        block_reason = f"активна {blocking_bias}-новина: {round(top_age or 0)} хв, {top_lifecycle_label} — {headline_short}"
    elif top_item and top_lifecycle in {"REJECTED", "ABSORBED", "DIGESTED", "UNRESOLVED"}:
        block_reason = f"новина вже не має жорсткого пріоритету: {round(top_age or 0)} хв, {top_lifecycle_label} — {headline_short}"
    else:
        block_reason = "активного новинного блокування немає"

    return {
        "bias": bias, "score": score,
        "raw_score": raw, "effective_raw_score": effective_raw,
        "blocking_score": blocking_score, "blocking_bias": blocking_bias,
        "hard_block_active": hard_block_active,
        "directional_driver_active": hard_block_active,
        "technical_priority": not hard_block_active,
        "total": len(items), "long_count": long_count, "short_count": short_count,
        "important": important[:7], "top": top,
        "top_age_min": round(top_age, 1) if top_age is not None else None,
        "top_lifecycle": top_lifecycle,
        "top_lifecycle_label": top_lifecycle_label,
        "newest_age_min": round(newest_age_min, 1) if newest_age_min is not None else None,
        "block_reason": block_reason,
        "note": f"{side_word(bias)} ({score}); {block_reason}; {top}",
    }


def parse_month_date(text):
    clean = re.sub(r"[^A-Za-z0-9, ]+", "", str(text or "")).strip()
    for fmt in ["%B %d, %Y", "%b %d, %Y"]:
        try:
            return datetime.strptime(clean, fmt).date()
        except Exception:
            pass
    return None


def parse_us_time(text, default_hour=10, default_minute=30):
    match = re.search(r"(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", str(text or ""), re.I)
    if not match:
        return default_hour, default_minute
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()
    if ampm == "P" and hour != 12:
        hour += 12
    if ampm == "A" and hour == 12:
        hour = 0
    return hour, minute


def event_minutes(event_time):
    try:
        return int((event_time - now_utc()).total_seconds() / 60)
    except Exception:
        return None


def event_alert_status(event_time):
    minutes = event_minutes(event_time)
    if minutes is None:
        return False, ""
    if 0 <= minutes <= IMPORTANT_EVENT_WINDOW_MINUTES:
        return True, f"через {minutes} хв"
    if -45 <= minutes < 0:
        return True, f"вийшла {abs(minutes)} хв тому"
    return False, ""


def get_eia_event():
    text = ""
    for url in [EIA_WPSR_URL, EIA_WPSR_SCHEDULE_URL]:
        response = http_get(url, timeout=8, retries=1)
        if response:
            text += "\n" + clean_text(response.text)
    match = re.search(r"Next Release Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if not match:
        return None
    release_date = parse_month_date(match.group(1))
    if not release_date:
        return None
    idx = text.find(match.group(1))
    hour, minute = parse_us_time(text[idx:idx + 260] if idx >= 0 else text, 10, 30)
    event_time = et_to_utc(release_date, hour, minute)
    return {
        "name": "EIA",
        "title": "EIA crude inventories",
        "time": event_time,
        "risk": "HIGH",
        "source": "EIA official",
    }


def get_fomc_events():
    response = http_get(FED_FOMC_CALENDAR_URL, timeout=8, retries=1)
    if not response:
        return []
    text = clean_text(response.text)
    year = now_utc().year
    events = []

    month_names = (
        "January|February|March|April|May|June|July|August|September|October|November|December|"
        "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    for match in re.finditer(rf"({month_names})\s+(\d{{1,2}})(?:-(\d{{1,2}}))?", text):
        raw = f"{match.group(1)} {match.group(3) or match.group(2)}, {year}"
        date_obj = parse_month_date(raw)
        if not date_obj:
            continue
        event_time = et_to_utc(date_obj, 14, 0)
        active, _ = event_alert_status(event_time)
        if active:
            events.append({
                "name": "Fed",
                "title": "Fed / FOMC decision or minutes",
                "time": event_time,
                "risk": "HIGH",
                "source": "Federal Reserve calendar",
            })
    return events[:3]


def get_manual_events():
    events = []
    for chunk in [x.strip() for x in MANUAL_IMPORTANT_EVENTS.split(";") if x.strip()]:
        parts = [x.strip() for x in chunk.split("|")]
        if len(parts) < 2:
            continue
        title = parts[0]
        try:
            dt = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            events.append({
                "name": "Manual",
                "title": title,
                "time": dt.astimezone(timezone.utc),
                "risk": parts[2] if len(parts) > 2 else "HIGH",
                "source": "IMPORTANT_EVENTS",
            })
        except Exception:
            continue
    return events


def analyze_calendar_alerts():
    events = []
    try:
        eia = get_eia_event()
        if eia:
            events.append(eia)
    except Exception as error:
        print(f"[WARN] EIA calendar failed: {error}")
    try:
        events.extend(get_fomc_events())
    except Exception as error:
        print(f"[WARN] Fed calendar failed: {error}")
    events.extend(get_manual_events())

    alerts = []
    for event in events:
        active, status = event_alert_status(event.get("time"))
        if not active:
            continue
        item = dict(event)
        item["status"] = status
        item["minutes_to_event"] = event_minutes(event.get("time"))
        alerts.append(item)

    return {
        "active": bool(alerts),
        "alerts": alerts[:4],
        "score": -12 if alerts else 0,
        "note": "; ".join(f"{x['title']} — {x['status']}" for x in alerts[:3]) if alerts else "",
    }




# ==========================================================
# SETUP ENGINE
# ==========================================================


def _parse_iso_time(value):
    try:
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def analyze_reentry_quality_review(state, context=None):
    """Event-driven review after a weak/failed trade.

    There is deliberately no timer, waiting period, fading penalty or fixed
    cooldown. A previous STOP/weak exit matters only while the bot is trying to
    repeat the same broken thesis without new market evidence.

    The review clears immediately when the market produces a genuinely new
    setup: a fresh closed-3M event, a rebuilt 15M base, a new BOS/CHOCH/reclaim,
    or a fully realigned structure/ICT/flow stack. The previous loss never
    lowers the quality of an opposite-side setup.
    """
    state = state or {}
    context = context or {}
    history = state.get("history") or []
    weak_actions = {
        "EXIT", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK",
        "EXIT_STRUCTURE_BREAK", "EXIT_WARNING_CONFIRM",
        "EXIT_AFTER_TP1_GIVEBACK", "PROTECT_OR_EXIT", "STOP",
    }

    # Merge the richer state snapshot with the latest history close. This keeps
    # backward compatibility with older last_signal.json files.
    latest_history = None
    for item in reversed(history[-MAX_HISTORY:]):
        if isinstance(item, dict) and item.get("type") == "TRADE_CLOSED":
            latest_history = dict(item)
            break
    latest_state = state.get("last_closed_trade") if isinstance(state.get("last_closed_trade"), dict) else None
    candidates = []
    for item in [latest_history, latest_state]:
        if not isinstance(item, dict):
            continue
        stamp = item.get("closed_at") or item.get("time")
        dt = _parse_iso_time(stamp)
        candidates.append((dt.timestamp() if dt else 0.0, item))
    if not candidates:
        return {"active": False, "status": "NO_PREVIOUS_WEAK_EXIT", "quality_adjustment": 0}
    candidates.sort(key=lambda row: row[0], reverse=True)
    close_item = dict(candidates[0][1])
    if latest_history and latest_state:
        # Prefer the newest timestamp, but preserve richer identity/context.
        same_side = latest_history.get("side") == latest_state.get("side")
        if same_side:
            close_item = {**latest_history, **latest_state}

    failed_side = str(close_item.get("side") or "").upper()
    action = str(close_item.get("action") or close_item.get("result_action") or "")
    result_pct = safe_float(close_item.get("result_pct"), 0.0) or 0.0
    weak = bool(
        failed_side in ["LONG", "SHORT"]
        and action in weak_actions
        and (
            result_pct <= 0.25
            or action in {
                "STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK",
                "EXIT_STRUCTURE_BREAK", "EXIT_WARNING_CONFIRM", "EXIT_AFTER_TP1_GIVEBACK",
            }
        )
    )
    if not weak:
        return {"active": False, "status": "PREVIOUS_TRADE_NOT_WEAK", "quality_adjustment": 0}

    # Recover the failed setup identity from the close snapshot or the preceding
    # same-side ENTRY. This is used only to detect an exact thesis replay.
    failed_setup = str(close_item.get("setup_type") or "").upper()
    failed_entry_quality = int(close_item.get("entry_quality") or close_item.get("quality") or 0)
    if not failed_setup or not failed_entry_quality:
        for item in reversed(history[-MAX_HISTORY:]):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "ENTRY" and str(item.get("side") or "").upper() == failed_side:
                classifier = item.get("setup_classifier") or {}
                failed_setup = failed_setup or str(classifier.get("type") or item.get("setup_type") or "").upper()
                failed_entry_quality = failed_entry_quality or int(item.get("quality") or 0)
                break

    closed_at = close_item.get("closed_at") or close_item.get("time")
    closed_dt = _parse_iso_time(closed_at)
    closed_ms = int(closed_dt.timestamp() * 1000) if closed_dt else 0
    setup_classifier = context.get("setup_classifier") or {}
    current_setup = str(setup_classifier.get("type") or "").upper()
    current_setup_score = int(setup_classifier.get("score") or 0)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}
    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()

    tf3_same = tf3.get("bias") == failed_side and abs(int(tf3.get("score", 0) or 0)) >= 20
    tf15_same = tf15.get("bias") == failed_side and abs(int(tf15.get("score", 0) or 0)) >= 22
    structure_same = bool(
        structure.get("bias") == failed_side
        or (failed_side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase))
        or (failed_side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase))
    )
    ict_strong = bool(
        ict.get("bias") == failed_side
        and (
            ict.get("entry_ok")
            or ict_setup in {
                "BOS_LONG_RETRACE_FVG_OB", "BOS_LONG_CONTINUATION_HOLD", "LIQUIDITY_SWEEP_LONG",
                "BOS_SHORT_RETRACE_FVG_OB", "BOS_SHORT_CONTINUATION_HOLD", "LIQUIDITY_SWEEP_SHORT",
                "DISCOUNT_FVG_OB_LONG", "PREMIUM_FVG_OB_SHORT",
            }
        )
    )
    flow_support = flow.get("bias") == failed_side and abs(int(flow.get("score", 0) or 0)) >= 12
    cvd_support = cvd.get("bias") == failed_side and abs(int(cvd.get("score", 0) or 0)) >= 12
    flow_against = flow.get("bias") == opposite(failed_side) and abs(int(flow.get("score", 0) or 0)) >= 18
    cvd_against = cvd.get("bias") == opposite(failed_side) and abs(int(cvd.get("score", 0) or 0)) >= 20
    oi_against = derivatives.get("bias") == opposite(failed_side) and abs(int(derivatives.get("score", 0) or 0)) >= 14
    liquidity_block = failed_side in (liquidity.get("blocks") or [])
    adverse_layers = sum(bool(x) for x in [flow_against, cvd_against, oi_against, liquidity_block])

    fresh_events = []
    try:
        fresh_events = [
            event for event in scan_15m_interval_entry_events(context, failed_side)
            if event.get("confirmed") and (not closed_ms or int(event.get("trigger_ts", 0) or 0) > closed_ms)
        ]
    except Exception:
        fresh_events = []
    fresh_event = max(fresh_events, key=lambda event: int(event.get("score", 0) or 0), default=None)

    fresh_base = {"allowed": False}
    reset_base = {"fresh_base_valid": False}
    try:
        fresh_base = fresh_base_continuation_snapshot(context, failed_side)
    except Exception:
        pass
    try:
        reset_base = fresh_base_exhaustion_reset_snapshot(context, failed_side)
    except Exception:
        pass
    fresh_base_ok = bool(
        fresh_base.get("allowed")
        or reset_base.get("fresh_base_valid")
        or reset_base.get("exhaustion_reset_allowed")
    )

    location = analyze_entry_location_score(failed_side, context)
    location_state = str(location.get("state") or "NORMAL").upper()
    location_bad = location_state in {"LATE", "VERY_LATE"}

    independent_setup_types = {
        "SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "CLOSED_15M_DIRECTION_FLIP",
        "CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY",
        "RANGE_COMPRESSION_BREAKOUT", "PULLBACK_CONTINUATION_FAST_ENTRY",
        "TREND_IGNITION_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE",
    }
    different_professional_setup = bool(
        current_setup
        and current_setup not in {"NO_CLEAN_SETUP", "LATE_IMPULSE_CHASE"}
        and current_setup != failed_setup
        and current_setup_score >= 62
        and (tf3_same or fresh_event)
        and (structure_same or ict_strong or fresh_base_ok)
    )
    fully_rebuilt_stack = bool(
        tf3_same
        and tf15_same
        and (structure_same or ict_strong)
        and adverse_layers <= 1
        and not location_bad
        and current_setup_score >= 60
    )
    new_independent_setup = bool(fresh_event or fresh_base_ok or different_professional_setup or fully_rebuilt_stack)

    same_setup_family = bool(failed_setup and current_setup and failed_setup == current_setup)
    no_fresh_rebuild = not new_independent_setup
    exact_failed_thesis_replay = bool(
        same_setup_family
        and no_fresh_rebuild
        and (
            not tf3_same
            or not (structure_same or ict_strong)
            or adverse_layers >= 2
            or location_bad
        )
    )

    evidence = []
    if fresh_event:
        evidence.append(f"fresh_{fresh_event.get('type')}")
    if fresh_base_ok:
        evidence.append("fresh_15m_base")
    if different_professional_setup:
        evidence.append("different_professional_setup")
    if fully_rebuilt_stack:
        evidence.append("fully_rebuilt_stack")
    if tf3_same:
        evidence.append("3m_realigned")
    if structure_same:
        evidence.append("structure_realigned")
    if ict_strong:
        evidence.append("strong_ict")
    if flow_support or cvd_support:
        evidence.append("flow_or_cvd_support")

    unresolved = []
    if same_setup_family:
        unresolved.append("same_setup_family")
    if not fresh_event and not fresh_base_ok:
        unresolved.append("no_fresh_event_or_base")
    if not tf3_same:
        unresolved.append("3m_not_realigned")
    if not (structure_same or ict_strong):
        unresolved.append("structure_ict_not_rebuilt")
    if adverse_layers >= 2:
        unresolved.append("multiple_adverse_layers")
    if location_bad:
        unresolved.append("poor_entry_location")

    if new_independent_setup:
        status = "NEW_INDEPENDENT_SETUP"
        reason = (
            f"попередній слабкий вихід {failed_side} не впливає на новий вхід: "
            "ринок сформував новий незалежний BOS/retest/ICT/base сценарій"
        )
        return {
            "active": True, "side": failed_side, "status": status,
            "block_reentry": False, "quality_adjustment": 0,
            "reset_confirmed": True, "reason": reason,
            "previous_action": action, "previous_result_pct": round(result_pct, 3),
            "previous_setup_type": failed_setup, "current_setup_type": current_setup,
            "previous_entry_quality": failed_entry_quality,
            "fresh_event": fresh_event, "fresh_base": fresh_base_ok,
            "evidence": evidence, "unresolved": unresolved,
            "policy": "EVENT_DRIVEN_NO_TIME_COOLDOWN",
        }

    if exact_failed_thesis_replay:
        reason = (
            f"новий {failed_side} повторює ту саму зламану тезу після {action} "
            "без нового BOS/CHOCH, ретесту, свіжої бази або повного ICT-перезбору"
        )
        return {
            "active": True, "side": failed_side, "status": "SAME_FAILED_THESIS_REPLAY",
            "block_reentry": True, "quality_adjustment": 0,
            "reset_confirmed": False, "reason": reason,
            "previous_action": action, "previous_result_pct": round(result_pct, 3),
            "previous_setup_type": failed_setup, "current_setup_type": current_setup,
            "previous_entry_quality": failed_entry_quality,
            "fresh_event": None, "fresh_base": False,
            "evidence": evidence, "unresolved": unresolved,
            "policy": "EVENT_DRIVEN_NO_TIME_COOLDOWN",
        }

    return {
        "active": True, "side": failed_side, "status": "REASSESSED_FROM_ZERO",
        "block_reentry": False, "quality_adjustment": 0,
        "reset_confirmed": False,
        "reason": (
            f"попередній {failed_side} {action} не створює часової заборони; "
            "поточний сетап оцінюється заново за структурою, 3M, ICT, flow і геометрією"
        ),
        "previous_action": action, "previous_result_pct": round(result_pct, 3),
        "previous_setup_type": failed_setup, "current_setup_type": current_setup,
        "previous_entry_quality": failed_entry_quality,
        "fresh_event": fresh_event, "fresh_base": fresh_base_ok,
        "evidence": evidence, "unresolved": unresolved,
        "policy": "EVENT_DRIVEN_NO_TIME_COOLDOWN",
    }


def analyze_reentry_cooldown(state, context=None):
    """Backward-compatible alias for older state/report integrations."""
    return analyze_reentry_quality_review(state, context or {})


# ==========================================================
# FULL TRANSITION PATCH SUITE
# ==========================================================

CONTINUATION_HYSTERESIS_SETUPS = {
    "TREND_CONTINUATION", "TREND_IGNITION_ENTRY",
    "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY",
    "CLOSED_15M_DIRECTION_FLIP",
}
STRICT_TRANSITION_REGIMES = {"EXHAUSTION"}
STRICT_RESCUE_EVENTS = {"SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"}


def _utc_ms_from_iso(value):
    dt = _parse_iso_time(value)
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _same_side_candle(candle, side):
    if candle is None:
        return False
    return candle.close > candle.open if side == "LONG" else candle.close < candle.open


def _opposite_side_candle(candle, side):
    if candle is None:
        return False
    return candle.close < candle.open if side == "LONG" else candle.close > candle.open


def structural_reset_gate_snapshot(state, context, side):
    """Require a new 15M base after TP2/TP3 before same-side re-entry.

    This is structural, not a time cooldown. The gate opens after a real pullback
    plus renewed acceptance/BOS with fast confirmation. The first 79.62-style
    entry is unaffected because no completed TP2/TP3 gate exists yet.
    """
    gate = (state or {}).get("structural_reset_gate") if isinstance(state, dict) else None
    if not isinstance(gate, dict) or not gate.get("active") or side != gate.get("side"):
        return {"active": False, "allowed": True, "reason": "", "side": side}
    closed_ms = _utc_ms_from_iso(gate.get("closed_at"))
    candles = list((context or {}).get("candles_15m_closed") or [])
    after = [c for c in candles if int(getattr(c, "ts", 0) or 0) > closed_ms]
    atr15 = safe_float((context or {}).get("atr15"), 0.0) or 0.0
    if len(after) < STRUCTURAL_RESET_MIN_15M_CANDLES:
        return {"active": True, "allowed": False, "reason": "після TP2/TP3 ще не сформована нова 15M база", "candles_after": len(after), "side": side}

    pullback = any(_opposite_side_candle(c, side) and abs(c.close-c.open) >= atr15*0.12 for c in after)
    close_price = safe_float(gate.get("close_price"), after[0].open) or after[0].open
    if side == "LONG":
        retrace = max((close_price - min(c.low for c in after)), 0.0)
        renewed = _same_side_candle(after[-1], side) and after[-1].close > after[-2].high
    else:
        retrace = max((max(c.high for c in after) - close_price), 0.0)
        renewed = _same_side_candle(after[-1], side) and after[-1].close < after[-2].low
    meaningful_retrace = retrace >= atr15 * 0.22 if atr15 else pullback
    structure = (context or {}).get("structure") or {}
    phase = str(structure.get("phase") or "").upper()
    fresh_bos = bool(
        structure.get("bias") == side and
        ((side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase))
    )
    tf3 = (context or {}).get("tf3") or {}
    flow = (context or {}).get("flow") or {}
    cvd = (context or {}).get("cvd") or {}
    fast_support = sum([
        tf3.get("bias") == side and abs(int(tf3.get("score",0) or 0)) >= 22,
        flow.get("bias") == side and abs(int(flow.get("score",0) or 0)) >= 10,
        cvd.get("bias") == side and abs(int(cvd.get("score",0) or 0)) >= 10,
    ])
    allowed = bool(pullback and meaningful_retrace and (renewed or fresh_bos) and fast_support >= 1)
    return {
        "active": True, "allowed": allowed, "candles_after": len(after), "side": side,
        "pullback": pullback, "meaningful_retrace": meaningful_retrace,
        "renewed": renewed, "fresh_bos": fresh_bos, "fast_support": int(fast_support),
        "reason": ("нова 15M база, повторне прийняття і підтвердження сформовані" if allowed
                   else "після TP2/TP3 потрібні нова 15M база, відкат і новий BOS/reclaim"),
    }



def apply_structural_reset_gate_to_setup(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    side = setup.get("side")
    snap = (context or {}).get("structural_reset_gate_snapshot") or {}
    if side not in ["LONG", "SHORT"]:
        return setup
    if snap.get("side") in ["LONG", "SHORT"] and side != snap.get("side"):
        return setup
    if _entry_setup_type(setup) == "FRESH_BASE_CONTINUATION_REENTRY" and setup.get("fresh_base_reentry_confirmed"):
        own = setup.get("fresh_base_snapshot") or fresh_base_continuation_snapshot(context, side)
        if own.get("allowed"):
            return setup
    if not snap.get("active") or snap.get("allowed"):
        return setup
    out = dict(setup)
    out.update({"action": "WATCH", "entry_level": "WATCH_TRIGGER", "entry_level_label": _entry_level_label("WATCH_TRIGGER"), "quality": min(64, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"), "structural_reset_block": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
    return out





def _range_boundary_rollover_apply(context, snapshot, atr15, candles):
    """Retire stale range boundaries after an accepted breakout and retest.

    Two closes outside the stored range plus a boundary retest/re-acceptance and
    supportive fast flow roll the old range forward. For a limited number of
    15M bars the retired range can no longer classify the new trend as its middle.
    """
    context = context or {}
    snapshot = dict(snapshot or {})
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    if state is None or not candles or not snapshot.get("available"):
        return snapshot
    price = safe_float(context.get("price"), candles[-1].close) or candles[-1].close
    last_ts = int(candles[-1].ts)
    boundary = state.get("range_boundary_state") if isinstance(state.get("range_boundary_state"), dict) else None

    # Seed a persistent range only when a genuine balance exists.
    if not boundary and snapshot.get("balance"):
        boundary = {
            "status": "ACTIVE", "high": snapshot.get("high"), "low": snapshot.get("low"),
            "created_ts": last_ts, "updated_ts": last_ts, "rollover_ts": 0,
            "rollover_side": "", "old_high": None, "old_low": None,
        }
        state["range_boundary_state"] = boundary

    if not boundary:
        return snapshot

    status = str(boundary.get("status") or "ACTIVE").upper()
    old_high = safe_float(boundary.get("high"), None)
    old_low = safe_float(boundary.get("low"), None)

    if status == "ROLLED_OVER":
        side = str(boundary.get("rollover_side") or "").upper()
        rollover_ts = int(boundary.get("rollover_ts", 0) or 0)
        bars_since = sum(1 for c in candles if int(c.ts) > rollover_ts)
        retired_high = safe_float(boundary.get("old_high"), old_high)
        retired_low = safe_float(boundary.get("old_low"), old_low)
        returned_inside = bool(
            retired_high is not None and retired_low is not None
            and retired_low + atr15 * 0.08 <= price <= retired_high - atr15 * 0.08
        )
        new_range_beyond = bool(
            snapshot.get("balance") and (
                (side == "LONG" and safe_float(snapshot.get("low"), -1e18) > (retired_high or 1e18) - atr15 * 0.18)
                or (side == "SHORT" and safe_float(snapshot.get("high"), 1e18) < (retired_low or -1e18) + atr15 * 0.18)
            )
        )
        if returned_inside:
            # Breakout failed: the old range becomes relevant again.
            boundary.update({"status": "ACTIVE", "high": retired_high, "low": retired_low,
                             "updated_ts": last_ts, "rollover_side": "", "rollover_ts": 0})
            state["range_boundary_state"] = boundary
            snapshot["boundary_rollover_failed"] = True
            return snapshot
        if new_range_beyond and bars_since >= 3:
            # A new balance has formed fully beyond the retired boundary.
            boundary = {"status": "ACTIVE", "high": snapshot.get("high"), "low": snapshot.get("low"),
                        "created_ts": last_ts, "updated_ts": last_ts, "rollover_ts": 0,
                        "rollover_side": "", "old_high": None, "old_low": None}
            state["range_boundary_state"] = boundary
            snapshot["new_range_after_rollover"] = True
            return snapshot
        still_outside = bool(
            (side == "LONG" and retired_high is not None and price > retired_high - atr15 * 0.05)
            or (side == "SHORT" and retired_low is not None and price < retired_low + atr15 * 0.05)
        )
        if bars_since <= RANGE_BOUNDARY_ROLLOVER_HOLD_BARS and still_outside:
            snapshot.update({
                "balance": False, "breakout_accepted": True, "accepted_side": side,
                "boundary_rollover": True, "retired_high": round_price(retired_high),
                "retired_low": round_price(retired_low), "rollover_bars_since": bars_since,
            })
            return snapshot
        if bars_since > RANGE_BOUNDARY_ROLLOVER_HOLD_BARS:
            state["range_boundary_state"] = None
        return snapshot

    if old_high is None or old_low is None:
        if snapshot.get("balance"):
            boundary.update({"high": snapshot.get("high"), "low": snapshot.get("low"), "updated_ts": last_ts})
            state["range_boundary_state"] = boundary
        return snapshot

    last2 = candles[-2:] if len(candles) >= 2 else []
    support_long, against_long = _fast_layer_counts(context, "LONG")
    support_short, against_short = _fast_layer_counts(context, "SHORT")
    event = context.get("entry_rescue_event") or {}
    event_long = bool(event.get("confirmed") and event.get("side") == "LONG" and event.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"})
    event_short = bool(event.get("confirmed") and event.get("side") == "SHORT" and event.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"})
    long_roll = bool(
        len(last2) == 2
        and all(c.close > old_high + atr15 * 0.04 for c in last2)
        and (min(c.low for c in last2) <= old_high + atr15 * 0.30 or event_long)
        and last2[-1].close >= last2[-2].close
        and support_long >= 1 and against_long <= 1
    )
    short_roll = bool(
        len(last2) == 2
        and all(c.close < old_low - atr15 * 0.04 for c in last2)
        and (max(c.high for c in last2) >= old_low - atr15 * 0.30 or event_short)
        and last2[-1].close <= last2[-2].close
        and support_short >= 1 and against_short <= 1
    )
    if long_roll or short_roll:
        side = "LONG" if long_roll else "SHORT"
        boundary.update({
            "status": "ROLLED_OVER", "rollover_side": side, "rollover_ts": last_ts,
            "old_high": old_high, "old_low": old_low, "updated_ts": last_ts,
        })
        state["range_boundary_state"] = boundary
        snapshot.update({
            "balance": False, "breakout_accepted": True, "accepted_side": side,
            "boundary_rollover": True, "retired_high": round_price(old_high),
            "retired_low": round_price(old_low), "rollover_bars_since": 0,
        })
        return snapshot

    # Refresh active boundaries only while the market is still a balance and the
    # new detector substantially overlaps the stored range.
    if snapshot.get("balance"):
        new_high = safe_float(snapshot.get("high"), old_high)
        new_low = safe_float(snapshot.get("low"), old_low)
        overlap = max(0.0, min(old_high, new_high) - max(old_low, new_low))
        denom = max(min(old_high - old_low, new_high - new_low), 1e-9)
        if overlap / denom >= 0.50:
            boundary.update({"high": round_price(new_high), "low": round_price(new_low), "updated_ts": last_ts})
            state["range_boundary_state"] = boundary
    return snapshot


def fresh_base_exhaustion_reset_snapshot(context, side):
    """Confirm a genuinely new 6-12 candle base after stale EXHAUSTION."""
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {
            "active": False, "fresh_base_valid": False,
            "exhaustion_reset_allowed": False, "allowed": False,
        }
    candles = list(context.get("candles_15m_closed") or [])
    if len(candles) < FRESH_BASE_EXHAUSTION_MIN_CANDLES + 2:
        return {
            "active": False, "fresh_base_valid": False,
            "exhaustion_reset_allowed": False, "allowed": False,
            "reason": "для reset потрібні 6-12 закритих 15M свічок бази",
        }
    price = safe_float(context.get("price"), candles[-1].close) or candles[-1].close
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or max(price * 0.006, 0.01)
    phase = str((context.get("structure") or {}).get("phase") or "").upper()
    structure_bias = (context.get("structure") or {}).get("bias")
    regime = context.get("regime_engine") or {}
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    runtime_state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else {}
    persistent_store = runtime_state.get("persistent_exhaustion_locks") if isinstance(runtime_state, dict) else {}
    persistent_lock = (persistent_store or {}).get(side) if isinstance(persistent_store, dict) else {}
    persistent_active = bool(isinstance(persistent_lock, dict) and persistent_lock.get("status") == "LOCKED")
    active = bool(
        regime_type == "EXHAUSTION"
        or _entry_consensus_raw_post_shock_snapshot(context, side).get("active")
        or persistent_active
    )
    best = None
    max_n = min(FRESH_BASE_EXHAUSTION_MAX_CANDLES, len(candles) - 2)
    for n in range(max_n, FRESH_BASE_EXHAUSTION_MIN_CANDLES - 1, -1):
        base = candles[-(n + 2):-2]
        confirm = candles[-2:]
        if len(base) != n:
            continue
        base_high = max(c.high for c in base); base_low = min(c.low for c in base)
        width_atr = (base_high - base_low) / max(atr15, 1e-9)
        if width_atr > FRESH_BASE_EXHAUSTION_MAX_WIDTH_ATR15:
            continue
        if side == "LONG":
            local_swing = any(base[i].low <= base[i-1].low and base[i].low <= base[i+1].low for i in range(1, len(base)-1))
            two_outside = all(c.close > base_high + atr15 * 0.03 for c in confirm)
            retest = min(c.low for c in confirm) <= base_high + atr15 * 0.30
            reaccept = confirm[-1].close >= confirm[-2].close and confirm[-1].low >= base_high - atr15 * 0.12
            structural = structure_bias == side or "BOS LONG" in phase or "CHOCH LONG" in phase
            stop_level = min(c.low for c in base[-4:] + confirm) - max(atr15 * 0.14, price * 0.0007)
            trigger_level = base_high
        else:
            local_swing = any(base[i].high >= base[i-1].high and base[i].high >= base[i+1].high for i in range(1, len(base)-1))
            two_outside = all(c.close < base_low - atr15 * 0.03 for c in confirm)
            retest = max(c.high for c in confirm) >= base_low - atr15 * 0.30
            reaccept = confirm[-1].close <= confirm[-2].close and confirm[-1].high <= base_low + atr15 * 0.12
            structural = structure_bias == side or "BOS SHORT" in phase or "CHOCH SHORT" in phase
            stop_level = max(c.high for c in base[-4:] + confirm) + max(atr15 * 0.14, price * 0.0007)
            trigger_level = base_low
        flow = context.get("flow") or {}; cvd = context.get("cvd") or {}
        flow_support = bool(
            (flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= RECOVERY_EXPIRY_MIN_FAST_SCORE)
            or (cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= RECOVERY_EXPIRY_MIN_FAST_SCORE)
        )
        _, against = _fast_layer_counts(context, side)
        accepted = bool(two_outside and (retest or reaccept))
        fresh_base_valid = bool(local_swing and accepted and flow_support and against <= 1 and structural)
        exhaustion_reset_allowed = bool(active and fresh_base_valid)
        score = int(70 + min(8, n - 6) + (4 if retest else 2) + (4 if flow_support else 0) - against * 5)
        candidate = {
            # ``allowed`` is retained as a backward-compatible alias for the
            # geometry of the new base.  Only ``exhaustion_reset_allowed`` may
            # bypass an old EXHAUSTION/post-shock lock.
            "active": active, "fresh_base_valid": fresh_base_valid,
            "exhaustion_reset_allowed": exhaustion_reset_allowed,
            "allowed": fresh_base_valid, "side": side, "base_candles": n,
            "base_high": round_price(base_high), "base_low": round_price(base_low),
            "width_atr": round(width_atr, 3), "new_local_swing": bool(local_swing),
            "accepted_breakout": bool(two_outside), "retest": bool(retest),
            "reacceptance": bool(reaccept), "flow_support": bool(flow_support),
            "fast_against": int(against), "structural": bool(structural),
            "stop_level": round_price(stop_level), "trigger_level": round_price(trigger_level),
            "trigger_ts": int(confirm[-1].ts), "score": score,
            "reason": (
                "нова 6-12 свічкова 15M база скинула старий EXHAUSTION: accepted breakout + ретест/повторне прийняття + flow"
                if exhaustion_reset_allowed else
                ("нова 6-12 свічкова 15M база підтверджена; EXHAUSTION/post-shock reset не потрібен"
                 if fresh_base_valid else
                 ("старий EXHAUSTION діє, доки нова 6-12 свічкова база не дасть swing, accepted breakout, ретест і flow"
                  if active else
                  "нова 6-12 свічкова база ще не підтвердила continuation"))
            ),
        }
        if fresh_base_valid:
            return candidate
        if best is None or score > best.get("score", 0):
            best = candidate
    return best or {
        "active": active, "fresh_base_valid": False,
        "exhaustion_reset_allowed": False, "allowed": False,
        "reason": "нова 6-12 свічкова база ще не підтвердила continuation",
    }


def _fresh_base_valid(snapshot):
    """Whether the new 6-12 candle base itself is technically valid.

    Older persisted snapshots only contain ``allowed``; the fallback preserves
    compatibility without granting them an exhaustion override automatically.
    """
    snapshot = snapshot or {}
    return bool(snapshot.get("fresh_base_valid", snapshot.get("allowed", False)))


def _exhaustion_reset_allowed(snapshot):
    """Whether a valid fresh base may specifically retire an old lock.

    The reset requires both a real stale EXHAUSTION/post-shock state and a valid
    new base.  For older state files, ``active and allowed`` is the safe
    equivalent of the new explicit field.
    """
    snapshot = snapshot or {}
    if "exhaustion_reset_allowed" in snapshot:
        return bool(snapshot.get("exhaustion_reset_allowed"))
    return bool(snapshot.get("active") and snapshot.get("allowed"))


def recovery_thesis_expiry_snapshot(context, recovery_side):
    """Expire a stale recovery thesis after a closed opposite 15M transition."""
    context = context or {}
    if recovery_side not in ["LONG", "SHORT"]:
        return {"active": False, "expired": False}
    side = opposite(recovery_side)
    candles = list(context.get("candles_15m_closed") or [])
    if len(candles) < 4:
        return {"active": True, "expired": False, "recovery_side": recovery_side, "new_side": side}
    recent = candles[-4:]
    structure = context.get("structure") or {}
    phase = str(structure.get("phase") or "").upper()
    structure_break = bool(
        structure.get("bias") == side and (
            (side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase))
            or (side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase))
        )
    )
    last2 = recent[-2:]
    if side == "SHORT":
        two_closes = all(c.close < c.open for c in last2) and last2[-1].close < last2[-2].close
        pivot = recent[-1].high < max(recent[-2].high, recent[-3].high)
    else:
        two_closes = all(c.close > c.open for c in last2) and last2[-1].close > last2[-2].close
        pivot = recent[-1].low > min(recent[-2].low, recent[-3].low)
    flow = context.get("flow") or {}; cvd = context.get("cvd") or {}
    fast = bool(
        (flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= RECOVERY_EXPIRY_MIN_FAST_SCORE)
        or (cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= RECOVERY_EXPIRY_MIN_FAST_SCORE)
    )
    expired = bool(structure_break and pivot and two_closes and fast)
    return {
        "active": True, "expired": expired, "recovery_side": recovery_side, "new_side": side,
        "structure_break": structure_break, "new_pivot": pivot, "two_directional_closes": two_closes,
        "flow_or_cvd": fast,
        "reason": (f"стара {recovery_side} recovery-гіпотеза анульована: закритий {side} BOS/CHOCH, новий pivot, два close і flow/CVD" if expired
                   else f"для ануляції {recovery_side} recovery потрібні закритий {side} BOS/CHOCH, pivot, два close і flow/CVD"),
    }


def expire_recovery_thesis_if_invalid(context, setup=None):
    """Clear stale recovery memory before it can block an opposite direction flip."""
    context = context or {}
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    recovery_sides = []
    memory = (state or {}).get("opportunity_memory") if state is not None else None
    if isinstance(memory, dict) and str(memory.get("setup_type") or "").upper() == "CAPITULATION_RECOVERY":
        recovery_sides.append(memory.get("side"))
    if isinstance(setup, dict) and _entry_setup_type(setup) == "CAPITULATION_RECOVERY":
        recovery_sides.append(setup.get("side"))
    current_classifier = context.get("setup_classifier") or {}
    if str(current_classifier.get("type") or "").upper() == "CAPITULATION_RECOVERY":
        recovery_sides.append(current_classifier.get("side"))
    for recovery_side in [x for x in dict.fromkeys(recovery_sides) if x in ["LONG", "SHORT"]]:
        snap = recovery_thesis_expiry_snapshot(context, recovery_side)
        if not snap.get("expired"):
            continue
        context["recovery_thesis_expiry"] = snap
        if state is not None:
            if isinstance(state.get("opportunity_memory"), dict) and state["opportunity_memory"].get("side") == recovery_side:
                state["opportunity_memory"] = None
            if isinstance(state.get("pending_trigger"), dict) and state["pending_trigger"].get("side") == recovery_side:
                state["pending_trigger"] = None
        if isinstance(setup, dict) and _entry_setup_type(setup) == "CAPITULATION_RECOVERY" and setup.get("side") == recovery_side:
            out = dict(setup)
            out.update({"action": "WATCH", "entry_level": "WATCH_TRIGGER", "entry_level_label": _entry_level_label("WATCH_TRIGGER"),
                        "quality": min(59, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"),
                        "recovery_thesis_expired": True})
            out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
            return out
    return setup


def capture_pre_gate_opportunity_memory(context, candidate, blocked, gate_name):
    """Persist the professional candidate before a hard gate converts it to WAIT."""
    context = context or {}
    if not isinstance(candidate, dict) or not isinstance(blocked, dict):
        return
    if candidate.get("action") not in ["ENTRY", "RISKY_ENTRY"] or blocked.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return
    side = candidate.get("side"); setup_type = _entry_setup_type(candidate)
    if side not in ["LONG", "SHORT"] or setup_type not in OPPORTUNITY_MEMORY_SETUP_TYPES:
        return
    quality = int(candidate.get("quality", 0) or 0)
    if quality < PRE_GATE_MEMORY_MIN_QUALITY:
        return
    fast = _fast_layer_state(context, side)
    if fast.get("against", 0) >= 2:
        return
    price = safe_float(context.get("price"), None)
    atr15 = safe_float(context.get("atr15"), (price or 90) * 0.006) or (price or 90) * 0.006
    event = candidate.get("entry_rescue_event") or context.get("entry_rescue_event") or {}
    trigger = safe_float(event.get("trigger_level"), None)
    if price is not None and trigger is not None:
        extension = max(0.0, ((price - trigger) if side == "LONG" else (trigger - price)) / max(atr15, 1e-9))
        if extension > OPPORTUNITY_MEMORY_MAX_EXTENSION_ATR15:
            return
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    if state is None:
        return
    plan = candidate.get("plan")
    old = state.get("opportunity_memory") if isinstance(state.get("opportunity_memory"), dict) else None
    same = bool(old and old.get("side") == side and str(old.get("setup_type") or "") == setup_type and old.get("source") == "PRE_GATE")
    created = old.get("created_at") if same else iso_now()
    gates = list(old.get("blocked_gates") or []) if same else []
    if gate_name not in gates:
        gates.append(gate_name)
    missing = str(blocked.get("reason") or "потрібне повторне підтвердження hard gate")
    state["opportunity_memory"] = {
        "active": True, "source": "PRE_GATE", "created_at": created,
        "updated_at": iso_now(), "ttl_minutes": _opportunity_memory_ttl(setup_type),
        "side": side, "setup_type": setup_type,
        "setup_label": (candidate.get("setup_classifier") or {}).get("label"),
        "initial_action": candidate.get("action"), "quality": quality,
        "price": round_price(price), "trigger_level": round_price(trigger),
        "trigger_ts": int(event.get("trigger_ts", 0) or 0),
        "trigger_type": event.get("type"), "retest_confirmed": bool(event.get("confirmed") and event.get("anchor_confirmed")),
        "invalidation": _opportunity_invalidation_from_setup(candidate, context),
        "initial_reason": candidate.get("reason"), "missing_condition": missing,
        "blocking_gate": gate_name, "blocked_gates": gates,
        "missing_conditions": list(dict.fromkeys([missing] + list(blocked.get("conflicts") or [])))[:5],
        "plan": asdict(plan) if plan else None,
    }


def _apply_gate_with_pre_memory(context, setup, gate_func, gate_name):
    before = setup
    after = gate_func(before, context)
    capture_pre_gate_opportunity_memory(context, before, after, gate_name)
    # Keep a dedicated candidate through persistent/shock lock release. This is
    # separate from generic opportunity memory because the lock may outlive the
    # normal 45-55 minute geometry TTL.
    capture_lock_release_bridge_candidate(context, before, after, gate_name)
    return after



def rolling_balance_detector(context):
    """Detect persistent 15M balance and retire stale boundaries after rollover."""
    context = context or {}
    candles = list(context.get("candles_15m_closed") or [])
    if len(candles) < 8:
        return {"available": False, "balance": False, "breakout_accepted": False}
    price = safe_float(context.get("price"), candles[-1].close) or candles[-1].close
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None)
    atr15 = atr15 or max(price * 0.006, 0.01)

    def evaluate_window(w):
        hi = max(c.high for c in w); lo = min(c.low for c in w); width=max(hi-lo,1e-9)
        path=sum(abs(w[i].close-w[i-1].close) for i in range(1,len(w)))
        efficiency=abs(w[-1].close-w[0].close)/max(path,1e-9)
        overlap_hits=0
        for a,b in zip(w[:-1],w[1:]):
            overlap=max(0.0,min(a.high,b.high)-max(a.low,b.low))
            denom=max(min(a.high-a.low,b.high-b.low),1e-9)
            if overlap/denom>=0.35: overlap_hits+=1
        overlap_ratio=overlap_hits/max(len(w)-1,1)
        closes=[c.close for c in candles[-max(30,len(w)+20):]]
        ema20_value=ema(closes,20) if closes else None
        crossings=0
        if ema20_value is not None:
            signs=[1 if c.close>=ema20_value else -1 for c in w]
            crossings=sum(1 for a,b in zip(signs[:-1],signs[1:]) if a!=b)
        failed=0
        for i in range(3,len(w)):
            ph=max(c.high for c in w[max(0,i-3):i]); pl=min(c.low for c in w[max(0,i-3):i]); c=w[i]
            failed += int(c.high>ph and c.close<ph)
            failed += int(c.low<pl and c.close>pl)
        score=0
        score += 2 if overlap_ratio>=ROLLING_BALANCE_MIN_OVERLAP else 0
        score += 2 if efficiency<=ROLLING_BALANCE_MAX_EFFICIENCY else 0
        score += 1 if crossings>=2 else 0
        score += 1 if failed>=1 else 0
        score += 1 if width/atr15<=4.8 else 0
        return {"w":w,"high":hi,"low":lo,"width":width,"efficiency":efficiency,
                "overlap_ratio":overlap_ratio,"crossings":crossings,"failed":failed,"score":score,
                "balance":score>=4}

    windows=[]
    fast_n=min(8,len(candles)); windows.append(evaluate_window(candles[-fast_n:]))
    cfg_n=min(max(8,min(12,int(ROLLING_BALANCE_WINDOW or 10))),len(candles))
    if cfg_n!=fast_n: windows.append(evaluate_window(candles[-cfg_n:]))
    chosen=max(windows,key=lambda x:(int(x["balance"]),x["score"],-x["efficiency"]))
    w=chosen["w"]; hi=chosen["high"]; lo=chosen["low"]; width=chosen["width"]

    base=w[:-2] if len(w)>=8 else w[:-1]
    base_hi=max(c.high for c in base); base_lo=min(c.low for c in base); last2=w[-2:]
    long_accept=bool(
        all(c.close>base_hi+atr15*0.08 for c in last2)
        and min(c.low for c in last2)<=base_hi+atr15*0.30
        and last2[-1].close>=last2[-2].close
        and last2[-1].close-base_hi>=atr15*0.12
    )
    short_accept=bool(
        all(c.close<base_lo-atr15*0.08 for c in last2)
        and max(c.high for c in last2)>=base_lo-atr15*0.30
        and last2[-1].close<=last2[-2].close
        and base_lo-last2[-1].close>=atr15*0.12
    )
    pos=(price-lo)/width
    snapshot = {
        "available":True,"balance":bool(chosen["balance"]),"score":int(chosen["score"]),
        "window":len(w),"high":round_price(hi),"low":round_price(lo),"position":round(pos,3),
        "overlap_ratio":round(chosen["overlap_ratio"],3),"efficiency":round(chosen["efficiency"],3),
        "ema_crossings":int(chosen["crossings"]),"failed_breaks":int(chosen["failed"]),
        "range_atr":round(width/atr15,3),"breakout_accepted":bool(long_accept or short_accept),
        "accepted_side":"LONG" if long_accept else ("SHORT" if short_accept else "NEUTRAL"),
        "base_high":round_price(base_hi),"base_low":round_price(base_lo),
    }
    return _range_boundary_rollover_apply(context, snapshot, atr15, candles)



def range_midpoint_gate_snapshot(context, side):
    """Final range gate using rolling balance and accepted breakout logic.

    The middle is always blocked in a genuine balance. In a very tight balance,
    LONG directly under the upper edge and SHORT directly above the lower edge
    are also blocked unless a breakout has actually closed and held outside.
    """
    if side not in ["LONG", "SHORT"]:
        return {"active": False, "allowed": True}
    structure = (context or {}).get("structure") or {}
    tf15 = (context or {}).get("tf15") or {}
    regime = (context or {}).get("regime_engine") or {}
    ict = (context or {}).get("ict") or {}
    phase = str(structure.get("phase") or "").upper()
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    price = safe_float((context or {}).get("price"))
    rolling = rolling_balance_detector(context)

    if rolling.get("available"):
        hi = safe_float(rolling.get("high")); lo = safe_float(rolling.get("low")); pos = safe_float(rolling.get("position"), 0.5)
    else:
        hi = safe_float(structure.get("swing_high"), safe_float(structure.get("recent_high")))
        lo = safe_float(structure.get("swing_low"), safe_float(structure.get("recent_low")))
        if not price or hi is None or lo is None or hi <= lo:
            return {"active": False, "allowed": True, "rolling_balance": rolling}
        pos = (price-lo)/(hi-lo)

    balance = bool(
        rolling.get("balance") or "RANGE" in phase or regime_type in {"RANGE", "COMPRESSION", "RANGE_COMPRESSION"}
        or (tf15.get("bias") == "NEUTRAL" and abs(int(tf15.get("score",0) or 0)) < 28)
    )
    in_mid = 0.34 <= pos <= 0.66 or str(ict.get("premium_discount") or "").upper() == "MIDRANGE"
    event = (context or {}).get("entry_rescue_event") or {}
    accepted_breakout = bool(rolling.get("breakout_accepted") and rolling.get("accepted_side") == side)
    event_ok = bool(
        event.get("confirmed") and event.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"}
        and (not rolling.get("balance") or rolling.get("accepted_side") == side)
    )
    bos_raw = structure.get("bias") == side and ((side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase))
    bos_ok = bool(bos_raw and (not rolling.get("balance") or accepted_breakout))
    tight_balance = bool(
        rolling.get("balance")
        and safe_float(rolling.get("range_atr"), 99) <= 2.0
        and safe_float(rolling.get("overlap_ratio"), 0) >= 0.70
        and safe_float(rolling.get("efficiency"), 1) <= 0.25
    )
    wrong_edge = bool(tight_balance and ((side == "LONG" and pos > 0.72) or (side == "SHORT" and pos < 0.28)))
    blocked = bool(balance and (in_mid or wrong_edge) and not event_ok and not bos_ok and not accepted_breakout)
    reason = (
        "вхід у бік зовнішньої межі щільного balance без прийнятого breakout" if wrong_edge else
        "середина стійкого 15M балансу: потрібен край range або закритий breakout-retest із прийняттям поза діапазоном"
    )
    return {
        "active": blocked, "allowed": not blocked, "position": round(float(pos),3),
        "rolling_balance": rolling, "accepted_breakout": accepted_breakout,
        "tight_balance": tight_balance, "wrong_edge": wrong_edge, "reason": reason,
    }








def strict_transition_rescue_snapshot(context, side, event=None):
    """After exhaustion/rejection, rescue requires BOS/retest + follow-through + flow."""
    event = event or (context or {}).get("entry_rescue_event") or {}
    regime = (context or {}).get("regime_engine") or {}
    current = str(regime.get("regime_type") or regime.get("name") or "").upper()
    previous = str(regime.get("previous_regime_type") or "").upper()
    rejection = post_impulse_rejection_snapshot(side, context)
    strict = bool(current in STRICT_TRANSITION_REGIMES or previous in STRICT_TRANSITION_REGIMES or rejection.get("active"))
    if not strict:
        return {"active":False, "allowed":True}
    etype = str(event.get("type") or "")
    tf3 = (context or {}).get("tf3") or {}
    structure = (context or {}).get("structure") or {}
    flow = (context or {}).get("flow") or {}
    cvd = (context or {}).get("cvd") or {}
    phase = str(structure.get("phase") or "").upper()
    event_ok = bool(event.get("confirmed") and event.get("anchor_confirmed") and etype in STRICT_RESCUE_EVENTS)
    bos_or_reclaim = bool(
        structure.get("bias") == side
        or (side == "LONG" and any(x in phase for x in ["BOS LONG","CHOCH LONG","DOWNSIDE SWEEP"]))
        or (side == "SHORT" and any(x in phase for x in ["BOS SHORT","CHOCH SHORT","UPSIDE SWEEP"]))
    )
    follow = bool(event.get("follow_through") or event.get("post_confirmed") or int(event.get("score",0) or 0) >= 74)
    fast = tf3.get("bias") == side and abs(int(tf3.get("score",0) or 0)) >= 28
    flow_support = sum([
        flow.get("bias") == side and abs(int(flow.get("score",0) or 0)) >= 10,
        cvd.get("bias") == side and abs(int(cvd.get("score",0) or 0)) >= 10,
    ]) >= 1
    allowed = bool(event_ok and bos_or_reclaim and follow and fast and flow_support)
    return {"active":True, "allowed":allowed, "event_type":etype, "event_ok":event_ok,
            "bos_or_reclaim":bos_or_reclaim, "follow":follow, "fast":fast, "flow_support":flow_support,
            "reason": ("новий post-exhaustion/rejection BOS/retest підтверджено" if allowed
                       else "після exhaustion/rejection потрібні новий BOS/retest, follow-through і CVD/flow")}



def apply_strict_transition_gate_to_setup(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    side = setup.get("side")
    setup_type = _entry_setup_type(setup)
    shock_side = _dominant_shock_side(context)
    priority = _transition_priority_confirmed(setup, context)
    # A confirmed opposite direction flip is exactly the evidence that a prior
    # same-side shock/rejection has ended; it must not wait for the old side's retest.
    if priority and setup_type == "CLOSED_15M_DIRECTION_FLIP" and shock_side in ["LONG", "SHORT"] and side != shock_side:
        out = dict(setup); out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        return out
    if priority and setup_type in {"CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY"}:
        out = dict(setup); out["action"] = "RISKY_ENTRY" if setup_type == "CAPITULATION_RECOVERY" else out.get("action")
        out["entry_level"] = out["action"]; out["entry_level_label"] = _entry_level_label(out["action"])
        return out
    regime = (context or {}).get("regime_engine") or {}
    rejection = post_impulse_rejection_snapshot(side, context)
    current = str(regime.get("regime_type") or regime.get("name") or "").upper()
    previous = str(regime.get("previous_regime_type") or "").upper()
    strict = current in STRICT_TRANSITION_REGIMES or previous in STRICT_TRANSITION_REGIMES or rejection.get("active")
    if not strict:
        return setup
    event = setup.get("entry_rescue_event") or (context or {}).get("entry_rescue_event") or {}
    snap = strict_transition_rescue_snapshot(context, side, event)
    if snap.get("allowed"):
        out = dict(setup)
        out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["quality"] = min(79, max(62, int(out.get("quality", 0) or 0)))
        return out
    out = dict(setup)
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"), "quality": min(57, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"), "strict_transition_block": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
    return out




def _fast_layer_counts(context, side):
    support = 0
    against = 0
    for key, threshold in [("tf3",22),("flow",10),("cvd",10),("liquidity",9),("clusters",5)]:
        block = (context or {}).get(key) or {}
        score = abs(int(block.get("score",0) or 0))
        if block.get("bias") == side and score >= threshold:
            support += 1
        elif block.get("bias") == opposite(side) and score >= threshold:
            against += 1
    return support, against


def _fast_layer_state(context, side):
    """Separate true opposition from neutral/missing fast data.

    Neutral flow is not a veto. A confirmed 15M/ICT setup may enter as RISKY
    when fast data is neutral, while two independent adverse layers still block.
    """
    details = {}
    support = against = neutral = missing = 0
    for key, threshold in [("tf3",22),("flow",10),("cvd",10),("liquidity",9),("clusters",5)]:
        block = (context or {}).get(key) or {}
        bias = str(block.get("bias") or "NEUTRAL").upper()
        score = abs(int(block.get("score", 0) or 0))
        if not block:
            status = "MISSING"; missing += 1
        elif bias == side and score >= threshold:
            status = "SUPPORT"; support += 1
        elif bias == opposite(side) and score >= threshold:
            status = "AGAINST"; against += 1
        else:
            status = "NEUTRAL"; neutral += 1
        details[key] = {"status": status, "bias": bias, "score": score, "threshold": threshold}
    return {"support": support, "against": against, "neutral": neutral, "missing": missing, "details": details}


def _professional_fast_layer_permission(context, side, strong_15m=False):
    state = _fast_layer_state(context, side)
    # One real supporting layer is enough. With strong closed-15M structure,
    # fully neutral fast data is acceptable only as RISKY, never as full ENTRY.
    risky_ok = bool(state["against"] <= 1 and (state["support"] >= 1 or (strong_15m and state["against"] == 0)))
    full_ok = bool(state["support"] >= 1 and state["against"] == 0)
    state.update({"risky_ok": risky_ok, "full_ok": full_ok, "neutral_only": state["support"] == 0 and state["against"] == 0})
    return state


def closed_15m_displacement_snapshot(context, side):
    """Tiered direction flip: early risky lane and fully confirmed lane."""
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {"confirmed": False, "early_confirmed": False, "full_confirmed": False}
    candles = list(context.get("candles_15m_closed") or [])
    if len(candles) < 10:
        return {"confirmed": False, "early_confirmed": False, "full_confirmed": False, "reason": "мало закритих 15M свічок"}
    w = candles[-10:]
    baseline = w[:-3]
    recent = w[-3:]
    price = safe_float(context.get("price"), recent[-1].close) or recent[-1].close
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or max(price * 0.006, 0.01)
    prior_high = max(c.high for c in baseline); prior_low = min(c.low for c in baseline)
    phase = str(((context.get("structure") or {}).get("phase") or "")).upper()
    structure_side = (context.get("structure") or {}).get("bias")
    if side == "LONG":
        break_closes = sum(c.close > prior_high + atr15 * 0.02 for c in recent)
        directional = sum(c.close > c.open for c in recent)
        displacement_atr = max(0.0, (recent[-1].close - w[-4].close) / atr15)
        retest = any(c.low <= prior_high + atr15 * 0.35 and c.close > prior_high for c in recent[1:])
        pivot = recent[-1].low > min(recent[-2].low, recent[-3].low) and recent[-1].close > recent[-2].close
        structure_break = "BOS LONG" in phase or "CHOCH LONG" in phase or structure_side == "LONG"
        level = prior_high
    else:
        break_closes = sum(c.close < prior_low - atr15 * 0.02 for c in recent)
        directional = sum(c.close < c.open for c in recent)
        displacement_atr = max(0.0, (w[-4].close - recent[-1].close) / atr15)
        retest = any(c.high >= prior_low - atr15 * 0.35 and c.close < prior_low for c in recent[1:])
        pivot = recent[-1].high < max(recent[-2].high, recent[-3].high) and recent[-1].close < recent[-2].close
        structure_break = "BOS SHORT" in phase or "CHOCH SHORT" in phase or structure_side == "SHORT"
        level = prior_low
    fast = _professional_fast_layer_permission(context, side, strong_15m=bool(structure_break and directional >= 2))
    rolling = rolling_balance_detector(context)
    early_balance_ok = bool(not rolling.get("balance") or (rolling.get("breakout_accepted") and rolling.get("accepted_side") == side))
    ema20_value = safe_float((context.get("tf15") or {}).get("ema20"), None)
    extension_atr = abs(price - ema20_value) / atr15 if ema20_value else 0.0

    early_confirmed = bool(
        displacement_atr >= EARLY_DIRECTION_FLIP_MIN_ATR
        and break_closes >= 1 and structure_break and directional >= 2
        and (retest or pivot)
        and fast.get("risky_ok")
        and early_balance_ok
        and extension_atr <= CLOSED_15M_DISPLACEMENT_MAX_EMA_EXTENSION_ATR
    )
    two_breaks = bool(
        break_closes >= CLOSED_15M_DISPLACEMENT_MIN_BREAK_CLOSES
        or (break_closes >= 1 and structure_break and directional >= 2)
    )
    full_confirmed = bool(
        displacement_atr >= CLOSED_15M_DISPLACEMENT_MIN_ATR
        and two_breaks and directional >= 2 and (retest or pivot)
        and fast.get("support", 0) >= 1 and fast.get("against", 0) <= 1
        and extension_atr <= CLOSED_15M_DISPLACEMENT_MAX_EMA_EXTENSION_ATR
    )
    tier = "FULL" if full_confirmed else ("EARLY" if early_confirmed else "NONE")
    confirmed = bool(early_confirmed or full_confirmed)
    score = int(clamp(60 + displacement_atr * 8 + break_closes * 5 + fast.get("support", 0) * 4 - fast.get("against", 0) * 6 + (5 if full_confirmed else 0), 0, 90))
    return {
        "confirmed": confirmed, "early_confirmed": early_confirmed, "full_confirmed": full_confirmed,
        "tier": tier, "side": side, "score": score,
        "displacement_atr": round(displacement_atr, 3), "break_closes": int(break_closes),
        "directional_closes": int(directional), "retest": bool(retest), "higher_low_or_lower_high": bool(pivot),
        "structure_break": bool(structure_break), "fast_support": fast.get("support", 0),
        "fast_against": fast.get("against", 0), "fast_neutral": fast.get("neutral", 0),
        "neutral_fast_allowed": bool(fast.get("neutral_only") and early_confirmed),
        "early_balance_ok": early_balance_ok,
        "extension_atr": round(extension_atr, 3), "trigger_level": round_price(level),
        "trigger_ts": int(recent[-1].ts),
        "reason": ("повна 15M зміна напрямку: два break-close + displacement + ретест + fast-підтвердження" if full_confirmed
                   else ("рання 15M зміна напрямку: break-close + BOS/CHOCH + ретест; вхід лише ризикований" if early_confirmed
                         else "для зміни напрямку потрібні 15M break-close, displacement, ретест/HL-LH і відсутність сильного потоку проти")),
    }


def _displacement_event(snapshot):
    return {
        "confirmed":True, "anchor_confirmed":True, "professional_location":True,
        "type":"BREAKOUT_RETEST", "side":snapshot.get("side"),
        "score":snapshot.get("score",72), "trigger_ts":snapshot.get("trigger_ts",0),
        "trigger_level":snapshot.get("trigger_level"), "trigger_close":snapshot.get("trigger_level"),
        "follow_through":True, "post_confirmed":True, "reset_confirmed":True,
        "extension_atr15":snapshot.get("extension_atr",0.0),
        "evidence":["закритий 15M displacement", ("два break-close" if snapshot.get("full_confirmed") else "ранній break-close + BOS/CHOCH"), "ретест/HL-LH", ("flow/CVD підтверджує" if snapshot.get("fast_support", 0) else "fast-шари нейтральні, не проти")],
        "source":"CLOSED_15M_DISPLACEMENT_OVERRIDE",
    }


def resolve_closed_15m_displacement_opportunity(context, base_setup):
    """Dedicated CLOSED_15M_DIRECTION_FLIP setup before higher-timeframe confirmation."""
    if not isinstance(context, dict) or not isinstance(base_setup, dict):
        return base_setup
    candidates = []
    for side in ["LONG", "SHORT"]:
        snap = closed_15m_displacement_snapshot(context, side)
        if not snap.get("confirmed"):
            continue
        event = _displacement_event(snap)
        work = dict(context)
        work["bias"] = side
        work["entry_rescue_event"] = event
        work["closed_15m_displacement_override"] = snap
        work["force_durable_stop_role"] = True
        setup_info = {
            "type": "CLOSED_15M_DIRECTION_FLIP", "label": "🟢 Закрита 15M зміна напрямку",
            "side": side, "score": snap.get("score", 72), "entry_allowed": True,
            "block_entry": False, "risk_mode": ("NORMAL" if snap.get("full_confirmed") else "RISKY"), "force_risky": not bool(snap.get("full_confirmed")),
            "reason": snap.get("reason"), "quality_adjustment": 1, "quality_cap": (84 if snap.get("full_confirmed") else 79),
            "profile": setup_trade_profile("TREND_IGNITION_ENTRY"),
            "entry_rule": "два закриті 15M break-close + displacement + ретест/HL-LH + flow/CVD",
            "stop_rule": "лише 15M/ICT інвалідація нової бази",
            "tp_rule": "перша реальна 15M/1H ліквідність без погоні",
            "management_rule": "ризиковий до підтвердження 1H",
            "closed_15m_direction_flip": True,
        }
        work["setup_classifier"] = setup_info
        work["tf3"] = dict(work.get("tf3") or {})
        work["tf3"]["bias"] = side
        work["tf3"]["score"] = max(28, abs(int(work["tf3"].get("score", 0) or 0))) * (1 if side == "LONG" else -1)
        work["dual_speed_mtf"] = build_dual_speed_mtf(work)
        plan = make_plan(side, work)
        if not plan or not getattr(plan, "valid", False):
            continue
        candidate = dict(base_setup)
        regime = context.get("regime_engine") or {}
        full_entry = bool(snap.get("full_confirmed") and (context.get("tf1h") or {}).get("bias") == side and str(regime.get("entry_action") or "ALLOW").upper() == "ALLOW")
        action = "ENTRY" if full_entry else "RISKY_ENTRY"
        candidate.update({
            "action": action, "side": side,
            "quality": int(max(RISKY_QUALITY_MIN, min(84 if full_entry else 79, snap.get("score", 72)))),
            "title": ("ВХІД " if full_entry else "РИЗИКОВАНИЙ ВХІД ") + side, "reason": snap.get("reason"),
            "plan": plan, "setup_classifier": setup_info, "entry_rescue_event": event,
            "closed_15m_displacement_override": snap, "closed_15m_direction_flip": True,
            "entry_level": action, "entry_level_label": _entry_level_label(action),
            "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + event["evidence"])),
            "conflicts": [x for x in (base_setup.get("conflicts") or []) if "1H" not in str(x)],
        })
        utility = candidate["quality"] + min(getattr(plan, "rr1", 0), 3) * 4 + snap.get("displacement_atr", 0) * 3
        candidates.append((utility, candidate, work))
    if not candidates:
        return base_setup
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, candidate, work = candidates[0]
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and base_setup.get("side") == candidate.get("side"):
        if int(base_setup.get("quality", 0) or 0) >= int(candidate.get("quality", 0) or 0) + 5:
            return base_setup
    context["entry_rescue_event"] = candidate.get("entry_rescue_event")
    context["setup_classifier"] = candidate.get("setup_classifier")
    context["closed_15m_displacement_override"] = candidate.get("closed_15m_displacement_override")
    return candidate



def _entry_consensus_raw_post_shock_snapshot(context, side):
    """Require a new pullback/retest after a vertical same-side move."""
    context=context or {}
    candles=list(context.get("candles_15m_closed") or [])
    if side not in ["LONG","SHORT"] or len(candles)<9:
        return {"active":False,"allowed":True,"retest_confirmed":False}
    w=candles[-12:]
    price=safe_float(context.get("price"),w[-1].close) or w[-1].close
    atr15=safe_float(context.get("atr15"),None) or safe_float((context.get("tf15") or {}).get("atr"),None) or max(price*0.006,0.01)
    if side=="LONG":
        start_idx=min(range(len(w)-1),key=lambda i:w[i].low)
        end_idx=max(range(start_idx+1,len(w)),key=lambda i:w[i].high) if start_idx<len(w)-1 else start_idx
        start=w[start_idx].low; extreme=w[end_idx].high
        run=extreme-start
        after=w[end_idx+1:]
        pullback=(extreme-min([c.low for c in after],default=extreme)) if after else 0.0
        pattern=bool(len(after)>=2 and pullback>=atr15*POST_SHOCK_MIN_RETEST_ATR15 and after[-1].low>min(c.low for c in after[:-1]) and after[-1].close>after[-2].high)
    else:
        start_idx=max(range(len(w)-1),key=lambda i:w[i].high)
        end_idx=min(range(start_idx+1,len(w)),key=lambda i:w[i].low) if start_idx<len(w)-1 else start_idx
        start=w[start_idx].high; extreme=w[end_idx].low
        run=start-extreme
        after=w[end_idx+1:]
        pullback=(max([c.high for c in after],default=extreme)-extreme) if after else 0.0
        pattern=bool(len(after)>=2 and pullback>=atr15*POST_SHOCK_MIN_RETEST_ATR15 and after[-1].high<max(c.high for c in after[:-1]) and after[-1].close<after[-2].low)
    run_atr=run/atr15
    run_pct=run/max(abs(start),1e-9)*100
    recent_extreme=end_idx>=len(w)-6
    shock=bool(recent_extreme and (run_atr>=POST_SHOCK_MIN_RUN_ATR15 or run_pct>=POST_SHOCK_MIN_RUN_PCT))
    event=(context.get("entry_rescue_event") or {})
    event_fresh=bool(
        event.get("confirmed") and int(event.get("trigger_ts",0) or 0)>int(w[end_idx].ts)
        and event.get("type") in {"BREAKOUT_RETEST","ICT_ZONE_RECLAIM","SWEEP_RECLAIM"}
        and (event.get("follow_through") or event.get("post_confirmed"))
    )
    displacement=context.get("closed_15m_displacement_override") or {}
    displacement_retest=bool(displacement.get("confirmed") and displacement.get("side")==side and displacement.get("retest"))
    retest=bool(pattern or event_fresh or displacement_retest)
    return {
        "active":shock, "allowed":not shock or retest, "retest_confirmed":retest,
        "run_atr":round(run_atr,3), "run_pct":round(run_pct,3),
        "pullback_atr":round(pullback/atr15,3), "extreme_ts":int(w[end_idx].ts),
        "event_fresh":event_fresh, "pattern_retest":pattern,
        "reason":("post-shock ретест підтверджено" if retest else "після вертикального руху потрібен новий 15M/3M pullback-retest; не доганяти shock"),
    }




def apply_post_shock_retest_gate_to_setup(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    side = setup.get("side")
    st = _entry_setup_type(setup)
    shock_side = _dominant_shock_side(context)
    reset = setup.get("fresh_base_exhaustion_reset_snapshot") or context.get("fresh_base_exhaustion_reset") or fresh_base_exhaustion_reset_snapshot(context, side)
    if _exhaustion_reset_allowed(reset) and st in {"FRESH_BASE_CONTINUATION_REENTRY", "RANGE_COMPRESSION_BREAKOUT"}:
        out = dict(setup); out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["fresh_base_exhaustion_reset"] = True
        return out
    if st == "CLOSED_15M_DIRECTION_FLIP" and _transition_priority_confirmed(setup, context) and shock_side in ["LONG", "SHORT"] and side != shock_side:
        out = dict(setup); out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["shock_direction_isolation"] = True
        return out
    if st in {"CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY"} and _transition_priority_confirmed(setup, context):
        return setup
    snap = post_shock_retest_snapshot(context, side)
    if not snap.get("active") or snap.get("allowed"):
        return setup
    out = dict(setup)
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"), "quality": min(58, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"), "post_shock_block": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
    return out




def capitulation_recovery_snapshot(context, side):
    """Confirm recovery and expose a new 15M reclaim-base stop geometry."""
    context = context or {}
    candles = list(context.get("candles_15m_closed") or [])
    if side not in ["LONG", "SHORT"] or len(candles) < 10:
        return {"active": False, "allowed": True, "geometry_ready": False}
    w = candles[-14:]
    price = safe_float(context.get("price"), w[-1].close) or w[-1].close
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or max(price * 0.006, 0.01)
    if side == "LONG":
        hi_idx = max(range(len(w)-1), key=lambda i: w[i].high)
        ex_idx = min(range(hi_idx+1, len(w)), key=lambda i: w[i].low) if hi_idx < len(w)-1 else hi_idx
        run = w[hi_idx].high - w[ex_idx].low; extreme = w[ex_idx]; after = w[ex_idx+1:]
        direction_ok = ex_idx > hi_idx
        reclaim = bool(len(after) >= 2 and after[-1].close > extreme.close + atr15*0.35 and after[-1].close > after[-2].high)
        pivot_ok = bool(len(after) >= 3 and after[-1].low > min(c.low for c in after[:-1]) + atr15*0.06 and after[-1].low >= after[-2].low)
        base_slice = after[-4:-1] if len(after) >= 4 else after[:-1]
        base_extreme = min((c.low for c in base_slice), default=None)
        stop_level = base_extreme - max(atr15*0.14, price*0.0007) if base_extreme is not None else None
        reclaim_level = max((c.high for c in after[:-1]), default=extreme.close)
    else:
        lo_idx = min(range(len(w)-1), key=lambda i: w[i].low)
        ex_idx = max(range(lo_idx+1, len(w)), key=lambda i: w[i].high) if lo_idx < len(w)-1 else lo_idx
        run = w[ex_idx].high - w[lo_idx].low; extreme = w[ex_idx]; after = w[ex_idx+1:]
        direction_ok = ex_idx > lo_idx
        reclaim = bool(len(after) >= 2 and after[-1].close < extreme.close - atr15*0.35 and after[-1].close < after[-2].low)
        pivot_ok = bool(len(after) >= 3 and after[-1].high < max(c.high for c in after[:-1]) - atr15*0.06 and after[-1].high <= after[-2].high)
        base_slice = after[-4:-1] if len(after) >= 4 else after[:-1]
        base_extreme = max((c.high for c in base_slice), default=None)
        stop_level = base_extreme + max(atr15*0.14, price*0.0007) if base_extreme is not None else None
        reclaim_level = min((c.low for c in after[:-1]), default=extreme.close)
    run_atr = run / atr15; run_pct = run / max(abs(w[0].close), 1e-9) * 100
    active = bool(direction_ok and ex_idx >= len(w)-CAPITULATION_MAX_AGE_15M_BARS and (run_atr >= CAPITULATION_MIN_RUN_ATR15 or run_pct >= CAPITULATION_MIN_RUN_PCT))
    events = [e for e in scan_15m_interval_entry_events(context, side) if int(e.get("trigger_ts", 0) or 0) > int(extreme.ts)] if active else []
    event = next((e for e in reversed(events) if e.get("confirmed") and e.get("anchor_confirmed") and e.get("type") in {"SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"} and (e.get("follow_through") or e.get("post_confirmed") or int(e.get("score", 0) or 0) >= 76)), None)
    support, against = _fast_layer_counts(context, side)
    flow_ok = support >= 1 and against <= 1
    geometry_ready = bool(stop_level is not None and abs(price-stop_level) >= atr15*MIN_ROBUST_STOP_ATR_15M)
    allowed = bool(not active or (reclaim and pivot_ok and event and flow_ok and geometry_ready))
    return {
        "active": active, "allowed": allowed, "reclaim_15m": reclaim, "higher_low_or_lower_high": pivot_ok,
        "event_ok": bool(event), "event": event, "flow_ok": flow_ok, "support_layers": support, "against_layers": against,
        "run_atr": round(run_atr, 3), "run_pct": round(run_pct, 3), "capitulation_ts": int(extreme.ts),
        "base_stop_level": round_price(stop_level) if stop_level is not None else None,
        "reclaim_level": round_price(reclaim_level), "trigger_ts": int((event or {}).get("trigger_ts", 0) or 0),
        "geometry_ready": geometry_ready,
        "reason": ("capitulation recovery підтверджено: 15M reclaim + нова база + 3M BOS/retest + flow" if allowed and active else "після капітуляції потрібні закритий 15M reclaim, HL/LH, новий 3M BOS/retest, flow і стоп за новою базою"),
    }




def apply_capitulation_recovery_gate_to_setup(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    st = _entry_setup_type(setup)
    if st == "CAPITULATION_RECOVERY" and setup.get("capitulation_recovery_confirmed"):
        out = dict(setup); out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        return out
    if st == "CLOSED_15M_DIRECTION_FLIP" and _transition_priority_confirmed(setup, context):
        return setup
    snap = capitulation_recovery_snapshot(context, setup.get("side"))
    if not snap.get("active"):
        return setup
    if snap.get("allowed"):
        out = dict(setup)
        out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["quality"] = min(79, max(RISKY_QUALITY_MIN, int(out.get("quality", 0) or 0)))
        out["capitulation_recovery_confirmed"] = True
        return out
    out = dict(setup)
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"), "quality": min(57, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"), "capitulation_recovery_block": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
    return out




def apply_final_same_side_exhaustion_lock(setup,context):
    """Final persistent same-side exhaustion lock.

    The previous implementation looked only at the *current* regime name. This
    version reads persistent state, so EXHAUSTION cannot disappear merely because
    the next run is labelled TREND_EXPANSION or NEWS_SHOCK.
    """
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    side = setup.get("side")
    snap = persistent_exhaustion_lock_snapshot(context, side, setup)
    if not snap.get("active"):
        out = dict(setup)
        if snap.get("release_reason"):
            out["persistent_exhaustion_release"] = snap.get("release_reason")
            if out.get("action") == "ENTRY":
                out["action"] = "RISKY_ENTRY"
                out["entry_level"] = "RISKY_ENTRY"
                out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
                out["quality"] = min(79, max(RISKY_QUALITY_MIN, int(out.get("quality", 0) or 0)))
        return out

    out = dict(setup)
    reason = snap.get("reason") or (
        "виснажений рух у тому самому напрямку збережений у памʼяті: "
        "потрібен новий pullback/retest, окремий continuation/reversal сетап "
        "або підтверджена 6-12 свічкова база"
    )
    out.update({
        "action": "WATCH", "entry_level": "BLOCK",
        "entry_level_label": _entry_level_label("BLOCK"),
        "quality": min(55, int(out.get("quality", 0) or 0)),
        "reason": reason, "same_side_exhaustion_lock": True,
        "persistent_exhaustion_lock": snap, "show_wait_plan": False,
    })
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
    return out

def _entry_setup_type(setup):
    return str(((setup or {}).get("setup_classifier") or {}).get("type") or "").upper()


def _directional_layer_map(context, side):
    result = {}
    for key, threshold in [("flow", 12), ("cvd", 14), ("clusters", 5), ("liquidity", 10), ("derivatives", 10), ("tf3", 24)]:
        block = (context or {}).get(key) or {}
        result[key] = {
            "support": bool(block.get("bias") == side and abs(int(block.get("score", 0) or 0)) >= threshold),
            "against": bool(block.get("bias") == opposite(side) and abs(int(block.get("score", 0) or 0)) >= threshold),
            "score": int(block.get("score", 0) or 0),
        }
    return result


def shock_lock_release_state_machine(context, side):
    """Persistent shock lock that releases on a genuinely new retest/acceptance.

    The lock is tied to one extreme. Ordinary continuation candles do not restart
    the timer. A new materially farther extreme may re-lock the side.
    """
    context = context or {}
    raw = _entry_consensus_raw_post_shock_snapshot(context, side)
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    candles = list(context.get("candles_15m_closed") or [])
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None)
    price = safe_float(context.get("price"), candles[-1].close if candles else None)
    atr15 = atr15 or max((price or 90) * 0.006, 0.01)
    if side not in ["LONG", "SHORT"]:
        return {"active": False, "allowed": True, "retest_confirmed": False}

    lock = ((state or {}).get("shock_lock") or {}) if state is not None else {}
    if lock.get("side") not in [None, side]:
        lock = {}
    raw_active = bool(raw.get("active"))
    extreme_ts = int(raw.get("extreme_ts", 0) or 0)
    extreme_price = None
    if candles and extreme_ts:
        hit = next((c for c in candles if int(c.ts) == extreme_ts), None)
        if hit:
            extreme_price = hit.high if side == "LONG" else hit.low

    if raw_active and not lock:
        lock = {
            "side": side, "status": "LOCKED", "origin_ts": extreme_ts,
            "extreme_ts": extreme_ts, "extreme_price": round_price(extreme_price),
            "released_at": "", "released_extreme": None,
        }
    elif raw_active and lock:
        old_extreme = safe_float(lock.get("extreme_price"), extreme_price)
        material_extension = bool(
            extreme_price is not None and old_extreme is not None and (
                (side == "LONG" and extreme_price - old_extreme >= atr15 * SHOCK_RELOCK_EXTENSION_ATR)
                or (side == "SHORT" and old_extreme - extreme_price >= atr15 * SHOCK_RELOCK_EXTENSION_ATR)
            )
        )
        if lock.get("status") == "RELEASED" and material_extension:
            lock.update({"status": "LOCKED", "origin_ts": extreme_ts, "extreme_ts": extreme_ts,
                         "extreme_price": round_price(extreme_price), "released_at": ""})
        elif lock.get("status") == "LOCKED" and material_extension:
            lock.update({"extreme_ts": extreme_ts, "extreme_price": round_price(extreme_price)})

    if not lock:
        return dict(raw, state="CLEAR")

    lock_extreme_ts = int(lock.get("extreme_ts", 0) or 0)
    events = [e for e in scan_15m_interval_entry_events(context, side) if int(e.get("trigger_ts", 0) or 0) > lock_extreme_ts]
    event = next((e for e in events if e.get("confirmed") and e.get("anchor_confirmed")
                  and e.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM", "SWEEP_RECLAIM"}
                  and (e.get("follow_through") or e.get("post_confirmed") or int(e.get("score", 0) or 0) >= 76)), None)
    after = [c for c in candles if int(c.ts) > lock_extreme_ts]
    acceptance = False
    if len(after) >= SHOCK_LOCK_RELEASE_CLOSES:
        last = after[-SHOCK_LOCK_RELEASE_CLOSES:]
        if side == "LONG":
            acceptance = all(c.close > c.open for c in last) and last[-1].close > last[-2].high and last[-1].low > min(c.low for c in last[:-1])
        else:
            acceptance = all(c.close < c.open for c in last) and last[-1].close < last[-2].low and last[-1].high < max(c.high for c in last[:-1])

    # Shock Release 2.0. A single directional 3M layer is no longer enough.
    # Require a structural anchor plus at least one independent confirmation,
    # and never release while real flow/CVD is strongly adverse.
    release_consensus = _shock_release_consensus_snapshot(
        context, side, event=bool(event), acceptance=bool(acceptance)
    )
    flow_release = bool(release_consensus.get("allowed"))
    released = bool(flow_release)
    if released:
        lock.update({"status": "RELEASED", "released_at": iso_now(), "released_extreme": lock.get("extreme_price")})
    elif len(after) > SHOCK_LOCK_MAX_15M_BARS and not raw_active:
        lock.update({"status": "EXPIRED"})

    if state is not None:
        state["shock_lock"] = lock

    locked = lock.get("status") == "LOCKED"
    return {
        "active": bool(locked or raw_active), "allowed": bool(not locked or released),
        "retest_confirmed": bool(released), "state": lock.get("status", "CLEAR"),
        "extreme_ts": lock_extreme_ts or extreme_ts, "event_fresh": bool(event),
        "acceptance_15m": bool(acceptance), "flow_release": bool(flow_release),
        "release_consensus": release_consensus,
        "run_atr": raw.get("run_atr", 0), "run_pct": raw.get("run_pct", 0),
        "reason": ("shock lock знято: структурний ретест/прийняття + щонайменше два незалежні підтвердження" if released
                   else "після shock потрібен новий breakout-retest/reclaim або закрите 15M прийняття + друге незалежне підтвердження; flow/CVD не можуть бути сильно проти"),
    }


def post_shock_retest_snapshot(context, side):
    return shock_lock_release_state_machine(context, side)


# ==========================================================
# ENTRY CONSENSUS GUARD PACKAGE
# ==========================================================


def _latest_context_ts(context):
    """Best available closed-market timestamp for persistent state updates."""
    for key in ["candles_3m", "candles_15m_closed", "candles_15m"]:
        rows = list((context or {}).get(key) or [])
        if rows:
            try:
                return int(getattr(rows[-1], "ts", 0) or 0)
            except Exception:
                pass
    return int(now_utc().timestamp() * 1000)


def _persistent_exhaustion_store(context):
    state = (context or {}).get("runtime_state") if isinstance((context or {}).get("runtime_state"), dict) else None
    if state is None:
        return None, {}
    store = state.get("persistent_exhaustion_locks")
    if not isinstance(store, dict):
        store = {}
        state["persistent_exhaustion_locks"] = store
    return state, store


def _entry_event_is_professional(event):
    event = event or {}
    return bool(
        event.get("confirmed")
        and event.get("anchor_confirmed")
        and event.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM", "SWEEP_RECLAIM"}
        and (
            event.get("follow_through")
            or event.get("post_confirmed")
            or int(event.get("score", 0) or 0) >= 76
        )
    )


def _fresh_professional_entry_evidence(context, side):
    """Independent evidence that a late/exhausted move has built a new setup.

    This is deliberately stricter than a directional 3M candle. It distinguishes
    a new professional location from simple continuation of the old impulse.
    """
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {
            "professional_exception": False, "fresh_event": False,
            "fresh_base": False, "micro_base_breakout": False,
            "strong_ict_location": False,
        }
    event = context.get("entry_rescue_event") or best_15m_interval_entry_event(context, side) or {}
    fresh_event = _entry_event_is_professional(event)
    ict = context.get("ict") or {}
    ict_setup = str(ict.get("setup") or "").upper()
    strong_setups = {
        "LONG": {"LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"},
        "SHORT": {"LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"},
    }
    strong_ict_location = bool(
        ict.get("bias") == side
        and ict.get("entry_ok")
        and ict_setup in strong_setups.get(side, set())
        and not bool(ict.get("no_chase"))
    )
    try:
        fresh_base_snapshot = fresh_base_exhaustion_reset_snapshot(context, side)
    except Exception:
        fresh_base_snapshot = {}
    fresh_base = bool(_fresh_base_valid(fresh_base_snapshot))
    try:
        micro = _setup_micro_breakout_state(context, side)
    except Exception:
        micro = {}
    micro_base_breakout = bool(
        micro.get("micro_bos")
        and micro.get("micro_base")
        and safe_float(micro.get("extension_from_base_atr"), 99.0) <= 1.20
    )
    professional_exception = bool(
        fresh_event or fresh_base or micro_base_breakout or strong_ict_location
    )
    return {
        "professional_exception": professional_exception,
        "fresh_event": fresh_event,
        "event": event,
        "fresh_base": fresh_base,
        "fresh_base_snapshot": fresh_base_snapshot,
        "micro_base_breakout": micro_base_breakout,
        "micro": micro,
        "strong_ict_location": strong_ict_location,
    }


def _shock_release_consensus_snapshot(context, side, event=False, acceptance=False):
    """Shock Release 2.0: require two independent confirmations.

    A directional 3M layer alone never releases the lock. A structural event or
    closed-15M acceptance is required, plus at least one other independent layer,
    while real flow/CVD must not be strongly adverse.
    """
    context = context or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    liquidity = context.get("liquidity") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}

    flow_support = bool(flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10)
    cvd_support = bool(
        cvd.get("confidence", "HIGH") != "LOW"
        and cvd.get("bias") == side
        and abs(int(cvd.get("score", 0) or 0)) >= 12
    )
    liquidity_support = bool(liquidity.get("bias") == side and abs(int(liquidity.get("score", 0) or 0)) >= 10)
    clusters_support = bool(clusters.get("bias") == side and abs(int(clusters.get("score", 0) or 0)) >= 5)
    derivatives_support = bool(derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10)

    flow_against = bool(
        flow.get("bias") == opposite(side)
        and abs(int(flow.get("score", 0) or 0)) >= SHOCK_RELEASE_FLOW_AGAINST_SCORE
    )
    cvd_against = bool(
        cvd.get("confidence", "HIGH") != "LOW"
        and cvd.get("bias") == opposite(side)
        and abs(int(cvd.get("score", 0) or 0)) >= SHOCK_RELEASE_CVD_AGAINST_SCORE
    )
    # Some feeds keep bias NEUTRAL but explicitly report dominant opposite trades.
    cvd_state = str(cvd.get("state") or "").upper()
    if side == "SHORT" and cvd_state in {"BUYERS_DOMINATE", "BULLISH_ABSORPTION"} and abs(int(cvd.get("score", 0) or 0)) >= 12:
        cvd_against = True
    if side == "LONG" and cvd_state in {"SELLERS_DOMINATE", "BEARISH_ABSORPTION"} and abs(int(cvd.get("score", 0) or 0)) >= 12:
        cvd_against = True

    confirmations = {
        "professional_event": bool(event),
        "closed_15m_acceptance": bool(acceptance),
        "flow": flow_support,
        "cvd": cvd_support,
        "liquidity": liquidity_support,
        "clusters": clusters_support,
        "derivatives": derivatives_support,
    }
    confirmation_count = sum(bool(v) for v in confirmations.values())
    structural_anchor = bool(event or acceptance)
    allowed = bool(
        structural_anchor
        and confirmation_count >= SHOCK_RELEASE_MIN_CONFIRMATIONS
        and not flow_against
        and not cvd_against
    )
    return {
        "allowed": allowed,
        "confirmation_count": int(confirmation_count),
        "confirmations": confirmations,
        "structural_anchor": structural_anchor,
        "strong_flow_against": flow_against,
        "strong_cvd_against": cvd_against,
    }


def persistent_exhaustion_lock_snapshot(context, side, setup=None):
    """Persist same-side EXHAUSTION/impulse risk across regime-name changes.

    The lock is retired only by a genuinely new base, a Shock Release 2.0
    retest/acceptance, or a separate continuation/reversal event with a fresh
    professional anchor. Merely renaming EXHAUSTION to TREND_EXPANSION does not
    erase the old market location.
    """
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {"active": False, "allowed": True, "state": "CLEAR"}
    state, store = _persistent_exhaustion_store(context)
    lock = dict(store.get(side) or {}) if isinstance(store, dict) else {}
    regime = context.get("regime_engine") or context.get("market_regime") or {}
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    regime_bias = str(((regime.get("metrics") or {}).get("bias") or context.get("bias") or "")).upper()
    raw_shock = _entry_consensus_raw_post_shock_snapshot(context, side)
    try:
        detected_exhausted, detected_reason = detect_exhausted_move(side, context)
    except Exception:
        detected_exhausted, detected_reason = False, ""
    current_exhausted = bool(
        (regime_type == "EXHAUSTION" and regime_bias == side)
        or raw_shock.get("active")
        or detected_exhausted
    )
    price = safe_float(context.get("price"), None)
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or ((price or 90) * 0.006)
    market_ts = _latest_context_ts(context)

    if not lock and current_exhausted:
        lock = {
            "side": side, "status": "LOCKED", "origin_ts": market_ts,
            "updated_ts": market_ts, "origin_price": round_price(price),
            "extreme_price": round_price(price), "released_at": "",
            "reason": detected_reason or raw_shock.get("reason") or "same-side impulse/exhaustion",
        }
    elif lock:
        old_extreme = safe_float(lock.get("extreme_price"), price)
        if price is not None:
            if side == "LONG":
                new_extreme = max(old_extreme if old_extreme is not None else price, price)
            else:
                new_extreme = min(old_extreme if old_extreme is not None else price, price)
            lock["extreme_price"] = round_price(new_extreme)
        lock["updated_ts"] = market_ts
        if lock.get("status") == "RELEASED" and current_exhausted and price is not None:
            # A confirmed release must remain stable for the whole market bar.
            # Several gates call this snapshot during one bot run; without this
            # guard the first call can RELEASE the lock and a later call can
            # immediately LOCK it again from the same candle/context.
            released_ts = int(lock.get("released_ts", 0) or 0)
            same_release_bar = bool(released_ts and market_ts <= released_ts)
            released_extreme = safe_float(lock.get("released_extreme"), old_extreme)
            material_extension = bool(
                not same_release_bar
                and released_extreme is not None and (
                    (side == "LONG" and price - released_extreme >= atr15 * PERSISTENT_EXHAUSTION_RELOCK_ATR15)
                    or (side == "SHORT" and released_extreme - price >= atr15 * PERSISTENT_EXHAUSTION_RELOCK_ATR15)
                )
            )
            if material_extension:
                lock.update({
                    "status": "LOCKED", "origin_ts": market_ts,
                    "origin_price": round_price(price), "released_at": "",
                    "reason": detected_reason or "нове матеріальне продовження після попереднього reset",
                })

    if not lock:
        return {"active": False, "allowed": True, "state": "CLEAR", "current_exhausted": current_exhausted}

    fresh_base = fresh_base_exhaustion_reset_snapshot(context, side)
    shock = shock_lock_release_state_machine(context, side)
    evidence = _fresh_professional_entry_evidence(context, side)
    setup_type = _entry_setup_type(setup) if isinstance(setup, dict) else ""
    separate_types = {
        "FRESH_BASE_CONTINUATION_REENTRY", "RANGE_COMPRESSION_BREAKOUT",
        "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY",
        "TREND_CONTINUATION", "SWEEP_RECLAIM_EARLY_ENTRY", "SWEEP_REVERSAL",
        "CLOSED_15M_DIRECTION_FLIP", "CAPITULATION_RECOVERY",
    }
    separate_setup_release = bool(
        setup_type in separate_types
        and (evidence.get("fresh_event") or evidence.get("fresh_base") or evidence.get("micro_base_breakout"))
    )
    release_reason = ""
    released = False
    if lock.get("status") == "LOCKED":
        if _exhaustion_reset_allowed(fresh_base):
            released = True
            release_reason = "нова 6-12 свічкова база скинула persistent EXHAUSTION"
        elif shock.get("retest_confirmed"):
            released = True
            release_reason = "новий breakout-retest/reclaim пройшов Shock Release 2.0"
        elif separate_setup_release:
            released = True
            release_reason = "окремий свіжий continuation/reversal сетап перебудував точку входу"
        if released:
            lock.update({
                "status": "RELEASED", "released_at": iso_now(),
                "released_ts": market_ts, "released_extreme": lock.get("extreme_price"),
                "release_reason": release_reason,
            })

    if state is not None:
        store[side] = lock
        state["persistent_exhaustion_locks"] = store

    active = lock.get("status") == "LOCKED"
    return {
        "active": active,
        "allowed": not active,
        "state": lock.get("status", "CLEAR"),
        "side": side,
        "current_exhausted": current_exhausted,
        "fresh_base_reset": bool(_exhaustion_reset_allowed(fresh_base)),
        "shock_retest": bool(shock.get("retest_confirmed")),
        "separate_setup_release": separate_setup_release,
        "release_reason": lock.get("release_reason", ""),
        "lock": lock,
        "reason": (
            "persistent EXHAUSTION/impulse lock: потрібна нова база, ретест або окремий свіжий сетап"
            if active else lock.get("release_reason", "")
        ),
    }


def refresh_persistent_exhaustion_locks(context):
    """Refresh both sides even on WATCH runs, so regime renaming cannot erase state."""
    if not isinstance(context, dict):
        return {}
    snapshots = {}
    for side in ["LONG", "SHORT"]:
        snapshots[side] = persistent_exhaustion_lock_snapshot(context, side)
    context["persistent_exhaustion_locks"] = snapshots
    return snapshots


def strong_ict_early_permission_snapshot(context, side):
    """Only a full FVG/OB/sweep model or a fresh hold/retest may early-override."""
    context = context or {}
    ict = context.get("ict") or {}
    setup = str(ict.get("setup") or "").upper()
    strong = {
        "LONG": {"LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"},
        "SHORT": {"LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"},
    }
    weak_hold = {
        "LONG": {"BOS_LONG_CONTINUATION_HOLD"},
        "SHORT": {"BOS_SHORT_CONTINUATION_HOLD"},
    }
    evidence = _fresh_professional_entry_evidence(context, side)
    strong_model = bool(
        ict.get("bias") == side and ict.get("entry_ok")
        and setup in strong.get(side, set()) and not ict.get("no_chase")
    )
    fresh_hold_retest = bool(
        ict.get("bias") == side
        and setup in weak_hold.get(side, set())
        and evidence.get("fresh_event")
    )
    return {
        "allowed": bool(strong_model or fresh_hold_retest),
        "strong_model": strong_model,
        "fresh_hold_retest": fresh_hold_retest,
        "evidence": evidence,
        "reason": (
            "сильний FVG/OB/sweep або свіжий hold/retest підтверджено"
            if (strong_model or fresh_hold_retest)
            else "Early ICT override заборонено: часткового ICT-контексту недостатньо; потрібен сильний FVG/OB/sweep або свіжий hold/retest"
        ),
    }


def composite_exhaustion_snapshot(context, side):
    """Contextual hard block from 3+ independent late/exhaustion signs."""
    context = context or {}
    if side not in ["LONG", "SHORT"]:
        return {"hard_block": False, "count": 0, "signals": []}
    price = safe_float(context.get("price"), None)
    tf15 = context.get("tf15") or {}
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    liquidity = context.get("liquidity") or {}
    atr15 = safe_float(context.get("atr15"), None) or safe_float(tf15.get("atr"), None) or ((price or 90) * 0.006)
    ema20 = safe_float(tf15.get("ema20"), None)
    distance_atr = abs(price - ema20) / max(atr15, 1e-9) if price is not None and ema20 is not None else 0.0
    rsi15 = safe_float(tf15.get("rsi"), 50) or 50
    fast = safe_float(tf3.get("fast_move_pct"), 0.0) or 0.0
    drift = safe_float(tf3.get("drift_pct"), 0.0) or 0.0
    move8 = safe_float(tf15.get("move_8_pct"), 0.0) or 0.0
    signals = []

    if distance_atr >= LOCATION_MAX_EMA_EXTENSION_ATR15:
        signals.append(f"ціна розтягнута від EMA20 на {round(distance_atr, 2)} ATR")
    if (side == "LONG" and rsi15 >= 70) or (side == "SHORT" and rsi15 <= 30):
        signals.append(f"15M RSI екстремальний: {round(rsi15, 1)}")
    vertical = bool(
        (side == "LONG" and fast >= LOCATION_VERTICAL_3M_PCT)
        or (side == "SHORT" and fast <= -LOCATION_VERTICAL_3M_PCT)
    )
    if vertical:
        signals.append(f"3M вертикальний імпульс {round(fast, 3)}%")
    mature = bool(
        (side == "LONG" and move8 >= LOCATION_MATURE_15M_MOVE_PCT)
        or (side == "SHORT" and move8 <= -LOCATION_MATURE_15M_MOVE_PCT)
    )
    if mature:
        signals.append(f"15M рух уже зрілий: move8 {round(move8, 3)}%")
    directional_drift = bool(
        (side == "LONG" and drift >= 0.65)
        or (side == "SHORT" and drift <= -0.65)
    )
    if directional_drift:
        signals.append(f"3M drift уже відпрацював {round(drift, 3)}%")

    flow_against = bool(flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10)
    cvd_state = str(cvd.get("state") or "").upper()
    cvd_against = bool(
        (cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 12)
        or (side == "SHORT" and cvd_state in {"BUYERS_DOMINATE", "BULLISH_ABSORPTION"} and abs(int(cvd.get("score", 0) or 0)) >= 12)
        or (side == "LONG" and cvd_state in {"SELLERS_DOMINATE", "BEARISH_ABSORPTION"} and abs(int(cvd.get("score", 0) or 0)) >= 12)
    )
    cluster_against = bool(clusters.get("bias") == opposite(side) or side_score(int(clusters.get("score", 0) or 0), side) <= -4)
    if flow_against or cvd_against or cluster_against:
        signals.append("flow/CVD/кластери показують поглинання проти входу")

    recent_extreme = safe_float(structure.get("recent_high" if side == "LONG" else "recent_low"), None)
    if price is not None and recent_extreme is not None:
        near_extreme = bool(
            (side == "LONG" and recent_extreme - price <= atr15 * 0.35)
            or (side == "SHORT" and price - recent_extreme <= atr15 * 0.35)
        )
        if near_extreme:
            signals.append("вхід біля/за свіжим локальним extreme")

    liquidity_vol_ratio = safe_float(liquidity.get("vol_ratio"), 99.0)
    tf3_vol_ratio = safe_float(tf3.get("vol_ratio"), 99.0)
    vol_ratio = min(
        99.0 if liquidity_vol_ratio is None else liquidity_vol_ratio,
        99.0 if tf3_vol_ratio is None else tf3_vol_ratio,
    )
    if vertical and vol_ratio <= LOCATION_LOW_PARTICIPATION_RATIO:
        signals.append(f"вертикальний рух на слабкій участі: vol ratio {round(vol_ratio, 2)}")

    evidence = _fresh_professional_entry_evidence(context, side)
    count = len(signals)
    hard_reset = bool(
        evidence.get("fresh_event")
        or evidence.get("fresh_base")
        or evidence.get("micro_base_breakout")
    )
    # A current strong ICT model can rescue a borderline 3-4 factor case, but
    # it cannot legalize an extreme 5+ factor sell-low/buy-high location without
    # an actual new event/base/retest.
    ict_exception = bool(evidence.get("strong_ict_location") and count <= 4)
    professional_exception = bool(hard_reset or ict_exception)
    hard_block = bool(
        count >= COMPOSITE_EXHAUSTION_BLOCK_COUNT
        and not professional_exception
    )
    return {
        "hard_block": hard_block,
        "count": int(count),
        "signals": signals,
        "distance_atr": round(distance_atr, 3),
        "rsi15": round(rsi15, 2),
        "professional_exception": professional_exception,
        "hard_reset_evidence": hard_reset,
        "ict_exception": ict_exception,
        "professional_evidence": evidence,
        "reason": (
            "композитна заборона: " + "; ".join(signals[:4])
            if hard_block else
            ("ознаки виснаження є, але новий професійний сетап перебудував локацію" if count >= COMPOSITE_EXHAUSTION_BLOCK_COUNT else "")
        ),
    }


def news_directional_consensus_snapshot(context, side):
    """Directional consensus where time and market reaction outrank the headline."""
    context = context or {}
    news = context.get("news") or {}
    calendar = context.get("calendar") or {}
    blocking_score = abs(int(news.get("blocking_score", 0) or 0))
    blocking_bias = str(news.get("blocking_bias") or "NEUTRAL")
    directional_driver = bool(news.get("hard_block_active") and blocking_bias in {"LONG", "SHORT"})
    same = bool(directional_driver and blocking_bias == side and blocking_score >= NEWS_DIRECTIONAL_MIN_SCORE)
    opposing = bool(directional_driver and blocking_bias == opposite(side) and blocking_score >= NEWS_OPPOSING_BLOCK_SCORE)
    neutral_calendar = bool(calendar.get("active") and not same and not opposing)
    evidence = _fresh_professional_entry_evidence(context, side)
    shock = post_shock_retest_snapshot(context, side)
    retest = bool(evidence.get("fresh_event") or shock.get("retest_confirmed"))
    try:
        late, _ = is_late_chase(side, context)
    except Exception:
        late = False
    composite = composite_exhaustion_snapshot(context, side)
    late = bool(late or composite.get("count", 0) >= COMPOSITE_EXHAUSTION_BLOCK_COUNT)

    # A NEWS_IMPULSE requires a currently active directional catalyst. An old,
    # absorbed headline cannot manufacture a news setup; technical setups remain free.
    active = bool(directional_driver or calendar.get("active"))
    allowed = bool(
        active and not opposing and (
            (same and (not late or retest))
            or (neutral_calendar and retest)
        )
    )
    age = news.get("top_age_min")
    lifecycle = str(news.get("top_lifecycle") or "NONE")
    lifecycle_label = str(news.get("top_lifecycle_label") or _news_lifecycle_label(lifecycle))
    age_text = f"{round(float(age))} хв" if age is not None else "час невідомий"
    if opposing:
        reason = f"активна новина проти цього NEWS_IMPULSE ({age_text}, {lifecycle_label})"
    elif same and late and not retest:
        reason = f"новина ще активна ({age_text}, {lifecycle_label}), але імпульс запізнілий — потрібен post-news retest"
    elif neutral_calendar and not retest:
        reason = "календарна подія нейтральна за напрямом — потрібен підтверджений post-news retest"
    elif same:
        reason = f"напрям новини ще підтверджений ринком ({age_text}, {lifecycle_label})"
    elif not active and lifecycle in {"REJECTED", "ABSORBED", "DIGESTED", "UNRESOLVED"}:
        reason = f"новина втратила жорсткий пріоритет ({age_text}, {lifecycle_label}); технічний сетап має перевагу"
    else:
        reason = "активного directional news driver немає; режим NEWS_SHOCK сам не підтверджує напрям"
    return {
        "active": active, "allowed": allowed, "directional_same": same,
        "opposing": opposing, "neutral_calendar": neutral_calendar,
        "retest_confirmed": retest, "late": late, "reason": reason,
        "news_score": abs(int(news.get("score", 0) or 0)),
        "blocking_score": blocking_score, "news_bias": news.get("bias"),
        "blocking_bias": blocking_bias, "top_age_min": age,
        "top_lifecycle": lifecycle, "top_lifecycle_label": lifecycle_label,
        "technical_priority": bool(news.get("technical_priority", True)),
    }


def location_viability_snapshot(context, side, setup=None):
    """Location viability is checked before any RR/TP projection is accepted."""
    context = context or {}
    setup_info = ((setup or {}).get("setup_classifier") or context.get("setup_classifier") or {}) if isinstance(setup, dict) else (context.get("setup_classifier") or {})
    setup_type = str(setup_info.get("type") or "").upper()
    persistent = persistent_exhaustion_lock_snapshot(context, side, setup)
    composite = composite_exhaustion_snapshot(context, side)
    strong_ict = strong_ict_early_permission_snapshot(context, side)
    news_consensus = news_directional_consensus_snapshot(context, side)
    ict = context.get("ict") or {}
    pd = str(ict.get("pd") or ict.get("pd_zone") or "").upper()
    wrong_pd = bool((side == "LONG" and pd == "PREMIUM") or (side == "SHORT" and pd == "DISCOUNT"))
    evidence = composite.get("professional_evidence") or _fresh_professional_entry_evidence(context, side)

    reasons = []
    if persistent.get("active"):
        reasons.append(persistent.get("reason") or "persistent exhaustion lock")
    if composite.get("hard_block"):
        reasons.append(composite.get("reason") or "композитна заборона запізнілої локації")
    if setup_type == "NEWS_IMPULSE" and news_consensus.get("active") and not news_consensus.get("allowed"):
        reasons.append(news_consensus.get("reason"))
    if setup_type == "PRIME_ICT_LOCATION_OVERRIDE" and not strong_ict.get("allowed"):
        reasons.append(strong_ict.get("reason"))
    if wrong_pd and composite.get("count", 0) >= 2 and not evidence.get("professional_exception"):
        reasons.append("ціна знаходиться у неправильній premium/discount зоні для цього напрямку")

    allowed = not reasons
    return {
        "allowed": allowed,
        "reason": "; ".join(dict.fromkeys([r for r in reasons if r])),
        "persistent_exhaustion": persistent,
        "composite_exhaustion": composite,
        "news_consensus": news_consensus,
        "strong_ict_permission": strong_ict,
        "wrong_pd": wrong_pd,
        "setup_type": setup_type,
    }


def entry_risk_package_snapshot(context, side, setup=None):
    location = location_viability_snapshot(context, side, setup)
    return {
        "allowed": bool(location.get("allowed")),
        "reason": location.get("reason") or "",
        "location_viability": location,
        "composite_exhaustion": location.get("composite_exhaustion") or {},
        "persistent_exhaustion": location.get("persistent_exhaustion") or {},
        "news_consensus": location.get("news_consensus") or {},
        "strong_ict_permission": location.get("strong_ict_permission") or {},
    }


def apply_entry_risk_package_gate(setup, context):
    """Final non-bypassable gate for memory/rescue/override entry lanes."""
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    side = setup.get("side")
    snap = entry_risk_package_snapshot(context, side, setup)
    if snap.get("allowed"):
        out = dict(setup)
        out["entry_risk_package"] = snap
        return out
    out = dict(setup)
    reason = snap.get("reason") or "поточна локація не дає професійного входу"
    out.update({
        "action": "WATCH", "entry_level": "BLOCK",
        "entry_level_label": _entry_level_label("BLOCK"),
        "quality": min(55, int(out.get("quality", 0) or 0)),
        "title": f"ЧЕКАТИ — {side} ЛОКАЦІЯ НЕЖИТТЄЗДАТНА",
        "reason": reason, "entry_risk_package_block": True,
        "show_wait_plan": False, "entry_risk_package": snap,
    })
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
    return out



def range_zone_segmentation_snapshot(context, side):
    rolling = rolling_balance_detector(context)
    if side not in ["LONG", "SHORT"] or not rolling.get("available"):
        return {"active": False, "allowed": True, "zone": "UNKNOWN", "rolling_balance": rolling}
    pos = safe_float(rolling.get("position"), 0.5)
    balance = bool(rolling.get("balance"))
    if pos < 0: zone = "OUTSIDE_LOW"
    elif pos > 1: zone = "OUTSIDE_HIGH"
    elif pos <= RANGE_EDGE_FRACTION: zone = "LOWER_EDGE"
    elif pos >= 1.0 - RANGE_EDGE_FRACTION: zone = "UPPER_EDGE"
    else: zone = "MIDDLE"
    event = (context or {}).get("entry_rescue_event") or {}
    displacement = (context or {}).get("closed_15m_displacement_override") or {}
    cap = capitulation_recovery_snapshot(context, side)
    reset = (context or {}).get("fresh_base_exhaustion_reset") or fresh_base_exhaustion_reset_snapshot(context, side)
    transition_override = bool(
        rolling.get("boundary_rollover") and rolling.get("accepted_side") == side
        or (displacement.get("confirmed") and displacement.get("side") == side and displacement.get("retest") and displacement.get("fast_support", 0) >= 1)
        or (cap.get("active") and cap.get("allowed"))
        or _exhaustion_reset_allowed(reset)
        or (event.get("confirmed") and event.get("side") == side and event.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM", "SWEEP_RECLAIM"} and (event.get("follow_through") or event.get("post_confirmed")) and _fast_layer_counts(context, side)[0] >= 1)
    )
    accepted = bool(rolling.get("breakout_accepted") and rolling.get("accepted_side") == side or transition_override)
    correct_edge = bool((side == "LONG" and zone == "LOWER_EDGE") or (side == "SHORT" and zone == "UPPER_EDGE"))
    outside_correct = bool((side == "LONG" and zone == "OUTSIDE_HIGH") or (side == "SHORT" and zone == "OUTSIDE_LOW"))
    allowed = bool(not balance or correct_edge or (outside_correct and accepted) or accepted)
    return {"active": balance, "allowed": allowed, "zone": zone, "position": round(float(pos), 3), "correct_edge": correct_edge, "accepted_breakout": accepted, "transition_override": transition_override, "boundary_rollover": bool(rolling.get("boundary_rollover")), "rolling_balance": rolling, "reason": ("старі межі range списані після двох close, accepted breakout і ретесту" if rolling.get("boundary_rollover") else ("range transition підтверджено закритим BOS/retest + flow" if transition_override else ("range edge або accepted breakout підтверджено" if allowed else "середина/неприйнятий вихід із 15M range: потрібен край діапазону або закритий breakout-retest")))}



def apply_range_midpoint_gate_to_setup(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    snap = range_zone_segmentation_snapshot(context, setup.get("side"))
    setup_type = _entry_setup_type(setup)
    if not snap.get("active"):
        return setup
    # Prime ICT in balance never bypasses zone segmentation. A generic rescue
    # setup may not trade a range edge merely because an old/local 3M sweep was
    # found; range entries must be classified explicitly as RANGE_EDGE_TRADE or
    # be an accepted breakout/retest.
    explicit_range_edge = setup_type == "RANGE_EDGE_TRADE"
    generic_edge_rescue = setup_type in {"SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "PULLBACK_CONTINUATION_FAST_ENTRY"}
    if snap.get("allowed") and (setup_type != "PRIME_ICT_LOCATION_OVERRIDE" or snap.get("correct_edge") or snap.get("accepted_breakout")):
        if snap.get("active") and snap.get("correct_edge") and generic_edge_rescue and not snap.get("accepted_breakout") and not explicit_range_edge:
            pass
        else:
            return setup
    out = dict(setup)
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"),
                "quality": min(57, int(out.get("quality", 0) or 0)), "reason": snap.get("reason"),
                "range_zone_block": True, "range_zone": snap.get("zone")})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [snap.get("reason")]))
    return out




def apply_regime_allowed_setup_whitelist(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    regime = (context or {}).get("regime_engine") or (context or {}).get("market_regime") or {}
    allowed = [str(x).upper() for x in (regime.get("allowed_setups") or [])]
    setup_type = _entry_setup_type(setup)
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    priority = _transition_priority_confirmed(setup, context)
    reset = setup.get("fresh_base_exhaustion_reset_snapshot") or (context or {}).get("fresh_base_exhaustion_reset") or fresh_base_exhaustion_reset_snapshot(context, setup.get("side"))
    reset_exception = bool(_exhaustion_reset_allowed(reset) and setup_type in {"FRESH_BASE_CONTINUATION_REENTRY", "RANGE_COMPRESSION_BREAKOUT"})
    event = setup.get("entry_rescue_event") or (context or {}).get("entry_rescue_event") or {}
    support, against = _fast_layer_counts(context, setup.get("side"))
    transition_event = bool(event.get("confirmed") and event.get("anchor_confirmed") and event.get("type") in {"BREAKOUT_RETEST", "SWEEP_RECLAIM", "ICT_ZONE_RECLAIM"} and (event.get("follow_through") or event.get("post_confirmed")) and support >= 1 and against <= 1)
    transition_types = {"CLOSED_15M_DIRECTION_FLIP", "CAPITULATION_RECOVERY", "FRESH_BASE_CONTINUATION_REENTRY", "SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "RANGE_COMPRESSION_BREAKOUT", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION_FAST_ENTRY"}
    transition_exception = bool(priority or reset_exception or (regime_type in TRANSITION_WHITELIST_REGIMES and setup_type in transition_types and transition_event))
    if transition_exception:
        out = dict(setup)
        snap = out.get("closed_15m_displacement_override") or (context or {}).get("closed_15m_displacement_override") or {}
        full_flip = bool(
            setup_type == "CLOSED_15M_DIRECTION_FLIP" and snap.get("full_confirmed")
            and ((context or {}).get("tf1h") or {}).get("bias") == out.get("side")
            and regime_type not in TRANSITION_WHITELIST_REGIMES
            and str(regime.get("entry_action") or "ALLOW").upper() == "ALLOW"
        )
        if not full_flip and (regime_type in TRANSITION_WHITELIST_REGIMES or setup_type in {"CLOSED_15M_DIRECTION_FLIP", "CAPITULATION_RECOVERY"} or reset_exception):
            out["action"] = "RISKY_ENTRY"; out["entry_level"] = "RISKY_ENTRY"; out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
            out["quality"] = min(79, max(RISKY_QUALITY_MIN, int(out.get("quality", 0) or 0)))
        elif full_flip:
            out["action"] = "ENTRY"; out["entry_level"] = "ENTRY"; out["entry_level_label"] = _entry_level_label("ENTRY")
            out["quality"] = min(84, max(ENTRY_QUALITY_MIN, int(out.get("quality", 0) or 0)))
            out["full_direction_flip_entry"] = True
        out["regime_transition_exception"] = True
        out["fresh_base_exhaustion_reset"] = bool(reset_exception)
        return out
    hard_block = bool(regime.get("hard_block") or str(regime.get("entry_action") or "").upper() == "BLOCK")
    if hard_block or (allowed and setup_type not in allowed):
        out = dict(setup)
        reason = "поточний режим не дозволяє цей тип сетапу; override не може обійти фінальний whitelist"
        out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"), "quality": min(59, int(out.get("quality", 0) or 0)), "reason": reason, "regime_whitelist_block": True})
        out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
        return out
    return setup



def apply_adverse_flow_veto_prime_ict(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    if _entry_setup_type(setup) != "PRIME_ICT_LOCATION_OVERRIDE":
        return setup
    side = setup.get("side")
    layers = _directional_layer_map(context, side)
    adverse = sum(v["against"] for v in layers.values())
    support = sum(v["support"] for v in layers.values())
    flow_cluster_veto = bool(layers["flow"]["against"] and layers["clusters"]["against"])
    regime = (context or {}).get("regime_engine") or {}
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    needs_positive_flow = regime_type in {"RANGE", "RANGE_COMPRESSION", "COMPRESSION", "REVERSAL_BUILDUP"}
    positive_flow = bool(layers["flow"]["support"] or layers["cvd"]["support"])
    veto = bool(flow_cluster_veto or adverse > PRIME_ICT_MAX_ADVERSE_LAYERS or (needs_positive_flow and not positive_flow) or (adverse >= 2 and support <= 1))
    if not veto:
        return setup
    out = dict(setup)
    reason = "Prime ICT заблоковано: локальна ICT-локація не може переважити flow/CVD/clusters проти"
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"),
                "quality": min(58, int(out.get("quality", 0) or 0)), "reason": reason,
                "prime_ict_adverse_flow_veto": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
    return out




def apply_prime_ict_htf_conflict_cap(setup, context):
    """Prevent a local Prime ICT zone from overruling a strong 4H conflict.

    A fresh 3M sweep/reclaim or breakout-retest can still unlock a risky entry;
    otherwise 4H strongly against + weak 3M + adverse sweep/premium location is
    a WATCH_TRIGGER, not a market entry.
    """
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    if _entry_setup_type(setup) != "PRIME_ICT_LOCATION_OVERRIDE":
        return setup
    side = setup.get("side")
    tf4h = (context or {}).get("tf4h") or {}
    tf3 = (context or {}).get("tf3") or {}
    ict = (context or {}).get("ict") or {}
    structure = (context or {}).get("structure") or {}
    liquidity = (context or {}).get("liquidity") or {}
    event = setup.get("entry_rescue_event") or (context or {}).get("entry_rescue_event") or {}
    htf_against = side_score(int(tf4h.get("score", 0) or 0), side) <= -35
    tf3_confirmed = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 24)
    pd = str(ict.get("premium_discount") or "").upper()
    phase = str(structure.get("phase") or "").upper()
    liquidity_event = str(liquidity.get("event") or "").upper()
    adverse_location = bool(
        side in (liquidity.get("blocks") or [])
        or (side == "LONG" and (pd == "PREMIUM" or "UPSIDE SWEEP" in phase or "SHORT RECLAIM" in liquidity_event))
        or (side == "SHORT" and (pd == "DISCOUNT" or "DOWNSIDE SWEEP" in phase or "LONG RECLAIM" in liquidity_event))
    )
    fresh_unlock = bool(
        event.get("confirmed") and event.get("anchor_confirmed") and event.get("side") == side
        and event.get("type") in {"SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"}
        and (event.get("follow_through") or event.get("post_confirmed"))
    )
    if not (htf_against and not tf3_confirmed and adverse_location and not fresh_unlock):
        return setup
    out = dict(setup)
    reason = "Prime ICT проти сильного 4H і без нового 3M retest/reclaim — лише очікування тригера"
    out.update({
        "action": "WATCH", "entry_level": "WATCH_TRIGGER", "entry_level_label": _entry_level_label("WATCH_TRIGGER"),
        "quality": min(PRIME_ICT_HTF_CONFLICT_QUALITY_CAP, int(out.get("quality", 0) or 0)),
        "reason": reason, "prime_ict_htf_conflict_cap": True,
    })
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
    return out


def final_stop_role_rebuild(setup, context):
    if not isinstance(setup, dict) or setup.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return setup
    setup_type = _entry_setup_type(setup)
    durable = {"TREND_CONTINUATION", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "RANGE_COMPRESSION_BREAKOUT", "CLOSED_15M_DIRECTION_FLIP", "FRESH_BASE_CONTINUATION_REENTRY", "CAPITULATION_RECOVERY"}
    plan = setup.get("plan")
    geometry_mode = str(getattr(plan, "geometry_mode", "") or "").upper() if plan else ""
    stop_basis = str(getattr(plan, "technical_stop_basis", "") or "").upper() if plan else ""
    needs_rebuild = bool(setup_type in durable and ("3M" in geometry_mode or "3M" in stop_basis))
    if not needs_rebuild:
        return setup
    work = dict(context)
    work["setup_classifier"] = setup.get("setup_classifier") or work.get("setup_classifier")
    work["force_durable_stop_role"] = True
    if setup.get("fresh_base_snapshot"):
        work["fresh_base_geometry"] = {"confirmed": True, "side": setup.get("side"), "stop_level": setup["fresh_base_snapshot"].get("stop_level")}
    if setup.get("capitulation_recovery_snapshot"):
        work["capitulation_recovery_geometry"] = {"confirmed": True, "side": setup.get("side"), "stop_level": setup["capitulation_recovery_snapshot"].get("base_stop_level")}
    rebuilt = make_plan(setup.get("side"), work)
    if rebuilt and getattr(rebuilt, "valid", False) and "3M" not in str(getattr(rebuilt, "geometry_mode", "") or "").upper():
        out = dict(setup); out["plan"] = rebuilt; out["final_stop_role_rebuilt"] = True
        return out
    out = dict(setup)
    reason = "фінальний тип сетапу потребує 15M/ICT стоп; 3M tactical план відхилено і не вдалося перебудувати"
    out.update({"action": "WATCH", "entry_level": "BLOCK", "entry_level_label": _entry_level_label("BLOCK"), "quality": min(60, int(out.get("quality", 0) or 0)), "reason": reason, "final_stop_role_block": True})
    out["conflicts"] = list(dict.fromkeys((out.get("conflicts") or []) + [reason]))
    return out



def continuation_exit_hysteresis_snapshot(trade, context, current_pct=0.0, confirmed_ict_reversal=False):
    setup_type = str(getattr(trade, "setup_type", "") or _state_note_value(getattr(trade, "notes", []), "SETUP_CLASSIFIER")).upper()
    continuation = set(CONTINUATION_HYSTERESIS_SETUPS) | {"FRESH_BASE_CONTINUATION_REENTRY"}
    checks = int(getattr(trade, "management_checks", 0) or 0)
    age = _trade_open_elapsed_minutes(trade)
    if setup_type not in continuation or (checks > EARLY_CONTINUATION_HOLD_CHECKS and age > EARLY_CONTINUATION_HOLD_MINUTES):
        return {"active": False, "allow_close": True}
    br = closed_mtf_break_snapshot(trade.side, context, confirmed_ict_reversal)
    rev = professional_fast_reversal_bridge(context, opposite(trade.side))
    hard = bool(br.get("closed_15m_break") and (br.get("structure_lost") or br.get("ict_lost"))) or bool(rev.get("emergency"))
    return {"active": True, "allow_close": hard, "break_snapshot": br, "checks": checks, "age_minutes": age, "reason": ("закрита 15M/ICT інвалідація підтверджена" if hard else "перші 2–3 цикли continuation: нормальний відкат не закриває угоду без закритого 15M/ICT зламу")}



def mandatory_mfe_profit_lock_snapshot(trade, context, current_pct, best_pct, giveback_ratio, opposing_layers):
    """Tiered MFE capture floor for risky/reversal/shock trades.

    1-2% MFE keeps at least 35%, 2-3% keeps 50%, and >3% keeps 60-65%.
    Trend continuation keeps its separate wider management unless it was opened
    as a risky/countertrend/recovery trade.
    """
    setup_type = str(getattr(trade,"setup_type","") or "").upper()
    regime_type = str(getattr(trade,"regime_type","") or "").upper()
    risky = str(getattr(trade,"entry_level","") or "").upper()=="RISKY_ENTRY"
    special = setup_type in {"SWEEP_REVERSAL","SWEEP_RECLAIM_EARLY_ENTRY","COUNTERTREND_SCALP","PRIME_ICT_LOCATION_OVERRIDE"}
    special = special or "REVERSAL" in regime_type or "SHOCK" in regime_type or getattr(trade,"recovery_after_rejection",False)
    eligible = bool(risky or special)
    if not eligible or best_pct < MFE_LOCK_TRIGGER_PCT:
        trade.mfe_profit_lock_streak = 0
        return {"active":False}

    if best_pct < 2.0:
        capture_ratio = 0.35
    elif best_pct < 3.0:
        capture_ratio = 0.50
    else:
        capture_ratio = 0.65 if special else 0.60
    floor_pct = max(0.10, best_pct * capture_ratio)
    floor_breached = current_pct <= floor_pct + 0.03
    active = bool(floor_breached or giveback_ratio >= max(0.25, 1.0-capture_ratio-0.03))
    if not active:
        trade.mfe_profit_lock_streak = 0
        return {"active":False, "capture_ratio":capture_ratio, "floor_pct":floor_pct}

    trade.mfe_profit_lock_streak = int(getattr(trade,"mfe_profit_lock_streak",0) or 0)+1
    trade.mfe_profit_lock_active = True
    if trade.side=="LONG":
        stop = trade.entry*(1+floor_pct/100.0)
    else:
        stop = trade.entry*(1-floor_pct/100.0)
    close = bool(
        trade.mfe_profit_lock_streak>=2
        and current_pct <= floor_pct
        and opposing_layers>=2
        and current_pct>0
    )
    return {
        "active":True, "stop":stop, "close":close, "lock_pct":floor_pct,
        "capture_ratio":capture_ratio, "floor_pct":floor_pct,
        "reason":f"ступінчастий MFE floor: після +{round(best_pct,2)}% потрібно зберегти щонайменше {round(capture_ratio*100)}% руху",
    }


def _tf_side_support(block, side, threshold=18):
    """Return True when a timeframe/feature block supports side strongly enough."""
    if side not in ["LONG", "SHORT"] or not isinstance(block, dict):
        return False
    try:
        return block.get("bias") == side or side_score(int(block.get("score", 0) or 0), side) >= threshold
    except Exception:
        return block.get("bias") == side


def _tf_side_against(block, side, threshold=18):
    if side not in ["LONG", "SHORT"] or not isinstance(block, dict):
        return False
    try:
        return block.get("bias") == opposite(side) or side_score(int(block.get("score", 0) or 0), side) <= -threshold
    except Exception:
        return block.get("bias") == opposite(side)


def _regime_engine_result(regime_type, legacy_name, score, confidence, reason, *,
                          entry_action="ALLOW", quality_adjustment=0, quality_cap=None,
                          hard_block=False, allowed_setups=None, risky_only=False,
                          notes=None, metrics=None):
    """Create a backward-compatible Regime Engine 2.0 result.

    `regime_type` is the professional phase. `name` stays legacy-compatible so
    existing TP/MFE logic that expects TREND/RANGE/PULLBACK/REVERSAL does not break.
    """
    regime_type = str(regime_type or "NORMAL").upper()
    legacy_name = str(legacy_name or "NORMAL").upper()
    confidence = int(max(0, min(100, confidence or 0)))
    score = int(max(0, min(100, score or 0)))
    return {
        "name": legacy_name,
        "regime_type": regime_type,
        "label": regime_label(regime_type),
        "score": score,
        "confidence": confidence,
        "reason": reason or "звичайний intraday режим",
        "entry_action": "RISKY_ONLY" if risky_only else str(entry_action or "ALLOW").upper(),
        "entry_quality_adjustment": int(quality_adjustment or 0),
        "quality_cap": quality_cap,
        "hard_block": bool(hard_block),
        "allowed_setups": list(allowed_setups or []),
        "notes": list(notes or []),
        "metrics": dict(metrics or {}),
    }


def detect_regime_engine_2(context, side=None):
    """Light, rule-based, controlled Regime Engine 2.0.

    It detects the market phase first, while keeping the old regime `name` for
    compatibility with stop/TP/MFE blocks. It should guide the Setup Classifier,
    not duplicate it.
    """
    if not isinstance(context, dict):
        return _regime_engine_result("UNKNOWN", "UNKNOWN", 0, 0, "немає контексту", entry_action="WAIT")

    price = safe_float(context.get("price")) or 0.0
    atr15 = safe_float(context.get("atr15")) or max(price * 0.006, 0.01)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    derivatives = context.get("derivatives") or {}
    news = context.get("news") or {}
    calendar = context.get("calendar") or {}
    liquidity = context.get("liquidity") or {}

    bias = side if side in ["LONG", "SHORT"] else context.get("bias")
    if bias not in ["LONG", "SHORT"]:
        # For neutral contexts use the stronger side only for regime orientation.
        total = int(context.get("total_score", 0) or 0)
        bias = "LONG" if total > 12 else ("SHORT" if total < -12 else "NEUTRAL")

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    pd = str(ict.get("premium_discount") or "").upper()
    range_atr = safe_float(tf15.get("range_atr"), 0) or 0
    move8 = abs(safe_float(tf15.get("move_8_pct"), 0) or 0)
    slope15 = abs(safe_float(tf15.get("slope_pct"), 0) or 0)
    rsi15 = safe_float(tf15.get("rsi"), 50) or 50
    score_total = abs(int(context.get("total_score", 0) or 0))
    tf15_score = abs(int(tf15.get("score", 0) or 0))
    tf3_score = abs(int(tf3.get("score", 0) or 0))
    news_score = abs(int(news.get("blocking_score", 0) or 0))
    news_driver_active = bool(news.get("hard_block_active"))

    near_eq = pd == "MIDRANGE" or ict_setup == "BALANCE_MIDRANGE"
    tf15_neutral = tf15.get("bias") == "NEUTRAL" or tf15.get("trend") == "RANGE" or tf15_score < 26
    structure_neutral = structure.get("bias") == "NEUTRAL" or phase in ["RANGE / WAIT", "NO DATA", ""]
    tf3_choppy = tf3.get("bias") == "NEUTRAL" or tf3_score < 30
    compression = bool((range_atr and range_atr <= 2.25 and move8 <= 0.55) or (near_eq and tf15_neutral and tf3_choppy))

    same15 = bias in ["LONG", "SHORT"] and _tf_side_support(tf15, bias, 18)
    same1h = bias in ["LONG", "SHORT"] and _tf_side_support(tf1h, bias, 18)
    same4h = bias in ["LONG", "SHORT"] and _tf_side_support(tf4h, bias, 16)
    same3 = bias in ["LONG", "SHORT"] and _tf_side_support(tf3, bias, 18)
    same_struct = bias in ["LONG", "SHORT"] and (structure.get("bias") == bias)
    htf_same = same1h or same4h
    correct_pd = bool(bias == "LONG" and pd == "DISCOUNT" or bias == "SHORT" and pd == "PREMIUM")

    flow_same = bias in ["LONG", "SHORT"] and (flow.get("bias") == bias or side_score(int(flow.get("score", 0) or 0), bias) >= 10)
    cvd_same = bias in ["LONG", "SHORT"] and cvd.get("confidence", "HIGH") != "LOW" and (cvd.get("bias") == bias or side_score(int(cvd.get("score", 0) or 0), bias) >= 10)
    oi_same = bias in ["LONG", "SHORT"] and (derivatives.get("bias") == bias or side_score(int(derivatives.get("score", 0) or 0), bias) >= 10)
    participation_same = sum([bool(flow_same), bool(cvd_same), bool(oi_same)])

    sweep_or_choch = any(x in phase for x in ["CHOCH", "SWEEP"]) or ict_setup in ["LIQUIDITY_SWEEP_LONG", "LIQUIDITY_SWEEP_SHORT"]
    bos_same = bool((bias == "LONG" and "BOS LONG" in phase) or (bias == "SHORT" and "BOS SHORT" in phase))

    exhausted = False
    exhaustion_reason = ""
    late = False
    late_penalty = 0
    late_reason = ""
    if bias in ["LONG", "SHORT"]:
        try:
            exhausted, exhaustion_reason = detect_exhausted_move(bias, context)
        except Exception:
            exhausted, exhaustion_reason = False, ""
        try:
            late, late_reason = is_late_chase(bias, context)
            late_penalty = soft_late_entry_penalty(bias, context, late_reason) if late else 0
        except Exception:
            late, late_reason, late_penalty = False, "", 0

    metrics = {
        "bias": bias,
        "range_atr": round(range_atr, 2),
        "move8_abs_pct": round(move8, 3),
        "slope15_abs_pct": round(slope15, 3),
        "rsi15": round(rsi15, 1),
        "score_total_abs": score_total,
        "participation_same": participation_same,
        "late_penalty": int(late_penalty),
        "pd": pd,
        "ict_setup": ict_setup,
    }

    # 1) Highest priority: News shock. It changes noise/stop/MFE assumptions.
    if calendar.get("active") or (news_driver_active and news_score >= NEWS_DIRECTIONAL_MIN_SCORE and (move8 >= 0.75 or same3 or participation_same >= 1)):
        return _regime_engine_result(
            "NEWS_SHOCK", "NEWS_IMPULSE", 78, 78,
            "новинний шок/подія: вхід тільки після 3M/структурного підтвердження, не доганяти першу свічку",
            entry_action="RISKY_ONLY", quality_adjustment=-2, quality_cap=79, risky_only=True,
            allowed_setups=["NEWS_IMPULSE", "TREND_IGNITION_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "PULLBACK_CONTINUATION_FAST_ENTRY"],
            metrics=metrics,
        )

    # 2) Exhaustion. Direction can be correct, but fresh entries must not chase.
    if exhausted or (late_penalty >= 15 and not (bos_same or sweep_or_choch)) or (move8 >= 1.55 and ((bias == "LONG" and rsi15 >= 74) or (bias == "SHORT" and rsi15 <= 26))):
        return _regime_engine_result(
            "EXHAUSTION", "IMPULSE", 72, 72,
            exhaustion_reason or late_reason or "рух виснажений: нові входи за імпульсом тільки після retest/FVG/OB/reclaim",
            entry_action="BLOCK", quality_adjustment=-10, quality_cap=61, hard_block=True,
            allowed_setups=["SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "RANGE_EDGE_TRADE"],
            metrics=metrics,
        )

    # 3) Compression/range states before trend states, because they control where NOT to enter.
    range_context = bool(tf15.get("trend") == "RANGE" or near_eq or (range_atr and range_atr <= 2.3))
    at_edge = False
    edge_reason = ""
    if bias in ["LONG", "SHORT"]:
        try:
            at_edge, edge_reason = _setup_range_edge(context, bias)
        except Exception:
            at_edge, edge_reason = False, ""
    metrics["range_edge"] = bool(at_edge)

    if range_context and at_edge:
        return _regime_engine_result(
            "RANGE_EDGE", "RANGE", 66, 68,
            edge_reason or "ціна біля краю діапазону: входи тільки від краю, TP захищати швидше",
            entry_action="RISKY_ONLY" if not same15 else "ALLOW", quality_adjustment=0, quality_cap=82,
            allowed_setups=["RANGE_EDGE_TRADE", "SWEEP_REVERSAL"], metrics=metrics,
        )

    if compression:
        return _regime_engine_result(
            "RANGE_COMPRESSION", "RANGE", 62, 64,
            "стискання діапазону: середина range заборонена, дозволений лише підтверджений compression breakout",
            entry_action="WAIT", quality_adjustment=-2, quality_cap=77,
            allowed_setups=["RANGE_COMPRESSION_BREAKOUT", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION_FAST_ENTRY"], metrics=metrics,
        )

    # 4) Reversal buildup: a reversal is forming, but it needs sweep/reclaim/CHOCH.
    if sweep_or_choch or (same_struct and not htf_same and (same3 or participation_same >= 1)):
        return _regime_engine_result(
            "REVERSAL_BUILDUP", "REVERSAL", 65, 66,
            "формується розворот: потрібні sweep/CHOCH + 3M/flow підтвердження, прибуток захищати швидше",
            entry_action="RISKY_ONLY", quality_adjustment=-1, quality_cap=84, risky_only=True,
            allowed_setups=["SWEEP_REVERSAL", "COUNTERTREND_SCALP", "PRIME_ICT_LOCATION_OVERRIDE"], metrics=metrics,
        )

    # 5) Trend pullback: trend context is alive, price is at a better ICT side.
    if bias in ["LONG", "SHORT"] and htf_same and correct_pd and not same15 and not near_eq:
        return _regime_engine_result(
            "TREND_PULLBACK", "PULLBACK", 70, 70,
            "відкат у тренді: шукати FVG/OB/reclaim, не плутати з боковиком",
            entry_action="ALLOW", quality_adjustment=2, quality_cap=86,
            allowed_setups=["PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY"], metrics=metrics,
        )

    # 6) Trend expansion: continuation regime, but still no naked chase.
    if bias in ["LONG", "SHORT"] and same15 and htf_same and (same_struct or same3 or bos_same) and range_atr >= 2.65 and score_total >= 62:
        confidence = 78 + (6 if participation_same >= 1 else 0) + (4 if bos_same else 0)
        return _regime_engine_result(
            "TREND_EXPANSION", "TREND", 82, min(92, confidence),
            "сильне продовження: continuation дозволений, TP2/TP3 можна тримати довше, але без доганяння",
            entry_action="ALLOW", quality_adjustment=3, quality_cap=90,
            allowed_setups=["TREND_CONTINUATION", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY"], metrics=metrics,
        )

    # 7) Balanced normal intraday.
    if range_context or (near_eq and tf15_neutral and (structure_neutral or tf3_choppy)):
        return _regime_engine_result(
            "RANGE_COMPRESSION", "RANGE", 56, 55,
            "баланс/EQ: входи тільки після краю діапазону або BOS/reclaim",
            entry_action="WAIT", quality_adjustment=-2, quality_cap=77,
            allowed_setups=["RANGE_EDGE_TRADE", "RANGE_COMPRESSION_BREAKOUT", "SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY"], metrics=metrics,
        )

    return _regime_engine_result(
        "NORMAL", "NORMAL", 50, 50,
        "звичайний intraday режим: головний фільтр — Setup Classifier і RR",
        entry_action="ALLOW", quality_adjustment=0, quality_cap=None, metrics=metrics,
    )


def stabilize_regime_engine(state, detected):
    """Attach simple stability memory without hiding fresh high-confidence changes."""
    if not isinstance(detected, dict):
        return detected
    if not isinstance(state, dict):
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
        stable_count = 1 if confidence >= 78 or pending_count >= 2 or current_type in ["NEWS_SHOCK", "EXHAUSTION"] else int(memory.get("stable_count", 0) or 0)
    else:
        pending_count = 1
        stable_count = 1 if confidence >= 82 or current_type in ["NEWS_SHOCK", "EXHAUSTION"] else 0

    is_stable = bool(stable_count >= 1 if current_type in ["NEWS_SHOCK", "EXHAUSTION"] else stable_count >= 2)
    detected = dict(detected)
    detected["stable_count"] = int(stable_count)
    detected["pending_count"] = int(pending_count)
    detected["is_stable"] = bool(is_stable)
    detected["previous_regime_type"] = prev_type

    state["regime_memory"] = {
        "type": current_type if stable_count >= 1 else prev_type,
        "pending_type": current_type if stable_count < 1 else "",
        "stable_count": int(stable_count),
        "pending_count": int(pending_count),
        "updated_at": iso_now(),
    }
    return detected


def detect_market_regime(context, side=None):
    """Backward-compatible wrapper around Regime Engine 2.0."""
    return detect_regime_engine_2(context, side)


def trade_mode_profile(context, side=None):
    """Dynamic TP/Stop profile by Regime Engine 2.0.

    The returned `regime` remains legacy-compatible for old MFE/stop code, while
    `regime_type` keeps the professional Regime Engine 2.0 phase.
    """
    regime = context.get("market_regime") if isinstance(context, dict) else None
    if not isinstance(regime, dict):
        regime = detect_market_regime(context, side)
    name = str(regime.get("name", "NORMAL") or "NORMAL").upper()
    regime_type = str(regime.get("regime_type", name) or name).upper()

    profiles = {
        "RANGE": {"tp1_pct": 0.72, "tp2_pct": 1.15, "tp3_pct": 1.80, "max_stop_pct": 1.05, "be_trigger": 0.40, "protect_trigger": 0.62, "giveback": 0.20},
        "PULLBACK": {"tp1_pct": 0.95, "tp2_pct": 1.75, "tp3_pct": 3.00, "max_stop_pct": 1.35, "be_trigger": 0.52, "protect_trigger": 0.82, "giveback": 0.30},
        "TREND": {"tp1_pct": 0.00, "tp2_pct": 2.45, "tp3_pct": 4.80, "max_stop_pct": MAX_STOP_DISTANCE_PCT, "be_trigger": 0.70, "protect_trigger": 1.00, "giveback": 0.40},
        "REVERSAL": {"tp1_pct": 0.95, "tp2_pct": 1.80, "tp3_pct": 3.10, "max_stop_pct": 1.45, "be_trigger": 0.48, "protect_trigger": 0.78, "giveback": 0.25},
        "NEWS_IMPULSE": {"tp1_pct": 0.85, "tp2_pct": 1.55, "tp3_pct": 2.70, "max_stop_pct": 1.25, "be_trigger": 0.45, "protect_trigger": 0.75, "giveback": 0.50},
        "IMPULSE": {"tp1_pct": 0.92, "tp2_pct": 1.65, "tp3_pct": 2.80, "max_stop_pct": 1.35, "be_trigger": 0.48, "protect_trigger": 0.78, "giveback": 0.35},
        "NORMAL": {"tp1_pct": 1.05, "tp2_pct": 1.95, "tp3_pct": 3.40, "max_stop_pct": 1.55, "be_trigger": 0.55, "protect_trigger": 0.88, "giveback": 0.35},
        "UNKNOWN": {"tp1_pct": 1.05, "tp2_pct": 1.95, "tp3_pct": 3.40, "max_stop_pct": 1.55, "be_trigger": 0.55, "protect_trigger": 0.88, "giveback": 0.35},
    }
    regime_overrides = {
        "TREND_EXPANSION": {"tp1_pct": 0.00, "tp2_pct": 2.55, "tp3_pct": 4.90, "be_trigger": 0.72, "protect_trigger": 1.05, "giveback": 0.44},
        "TREND_PULLBACK": {"tp1_pct": 0.98, "tp2_pct": 1.82, "tp3_pct": 3.15, "max_stop_pct": 1.38, "be_trigger": 0.52, "protect_trigger": 0.84, "giveback": 0.31},
        "RANGE_COMPRESSION": {"tp1_pct": 0.78, "tp2_pct": 1.22, "tp3_pct": 1.88, "max_stop_pct": 1.08, "be_trigger": 0.38, "protect_trigger": 0.60, "giveback": 0.18},
        "RANGE_EDGE": {"tp1_pct": 0.82, "tp2_pct": 1.28, "tp3_pct": 1.95, "max_stop_pct": 1.06, "be_trigger": 0.40, "protect_trigger": 0.62, "giveback": 0.20},
        "REVERSAL_BUILDUP": {"tp1_pct": 0.92, "tp2_pct": 1.68, "tp3_pct": 2.75, "max_stop_pct": 1.38, "be_trigger": 0.46, "protect_trigger": 0.74, "giveback": 0.24},
        "NEWS_SHOCK": {"tp1_pct": 0.90, "tp2_pct": 1.65, "tp3_pct": 2.85, "max_stop_pct": 1.32, "be_trigger": 0.48, "protect_trigger": 0.78, "giveback": 0.52},
        "EXHAUSTION": {"tp1_pct": 0.78, "tp2_pct": 1.22, "tp3_pct": 1.90, "max_stop_pct": 1.08, "be_trigger": 0.38, "protect_trigger": 0.58, "giveback": 0.18},
    }
    profile = dict(profiles.get(name, profiles["NORMAL"]))
    profile.update(regime_overrides.get(regime_type, {}))
    profile["regime"] = name
    profile["regime_type"] = regime_type
    profile["regime_label"] = regime_label(regime)
    profile["regime_engine"] = regime
    profile["reason"] = regime.get("reason", "")
    return profile



def regime_label(regime):
    """User-facing Ukrainian label for legacy or Regime Engine 2.0 codes."""
    if isinstance(regime, dict):
        name = regime.get("regime_type") or regime.get("name")
    else:
        name = regime
    name = str(name or "NORMAL").upper()
    labels = {
        "TREND_EXPANSION": "сильне продовження тренду",
        "TREND_PULLBACK": "відкат у тренді",
        "RANGE_COMPRESSION": "стискання діапазону",
        "RANGE_EDGE": "край діапазону",
        "REVERSAL_BUILDUP": "формується розворот",
        "NEWS_SHOCK": "новинний шок",
        "EXHAUSTION": "виснажений рух",
        "TREND": "сильний тренд",
        "RANGE": "боковик",
        "PULLBACK": "відкат у тренді",
        "REVERSAL": "розворот",
        "NEWS_IMPULSE": "новинний імпульс",
        "IMPULSE": "імпульсний / виснажений рух",
        "NORMAL": "звичайний intraday режим",
        "UNKNOWN": "звичайний intraday режим",
    }
    return labels.get(name, "звичайний intraday режим")


# ==========================================================
# SETUP CLASSIFIER
# ==========================================================

SETUP_CLASS_LABELS = {
    "TREND_CONTINUATION": "🟢 Продовження тренду",
    "TREND_IGNITION_ENTRY": "🟢 Ранній старт тренду / пробій",
    "PULLBACK_CONTINUATION": "🟢 Відкат у тренді",
    "PULLBACK_CONTINUATION_FAST_ENTRY": "🟢 Ранній вхід на відкаті",
    "PRIME_ICT_LOCATION_OVERRIDE": "🟢 Сильна ICT-локація",
    "RANGE_COMPRESSION_BREAKOUT": "🟢 Пробій після стискання діапазону",
    "SWEEP_REVERSAL": "🟡 Зняття ліквідності + розворот",
    "SWEEP_RECLAIM_EARLY_ENTRY": "🟡 Раннє зняття ліквідності + повернення",
    "RANGE_EDGE_TRADE": "🟡 Вхід від краю діапазону",
    "COUNTERTREND_SCALP": "🟠 Скальп проти тренду",
    "NEWS_IMPULSE": "🟠 Новинний імпульс",
    "LATE_IMPULSE_CHASE": "🔴 Пізній імпульс / доганяння",
    "COUNTERTREND_PULLBACK_WAIT": "🟡 Відкат проти основного тренду — чекати",
    "RANGE_MIDDLE_BLOCK": "🔴 Середина діапазону — входу немає",
    "NO_CLEAN_SETUP": "⚪ Чистого сетапу немає",
}


def _setup_label(setup_type):
    return SETUP_CLASS_LABELS.get(str(setup_type or "NO_CLEAN_SETUP"), "⚪ Чистого сетапу немає")


def _setup_side_score(block, side):
    return side_score(int((block or {}).get("score", 0) or 0), side)


def _setup_has_zone_for_side(ict, side):
    """Return whether current ICT model has a usable FVG/OB/retrace setup."""
    setup = str((ict or {}).get("setup", "") or "").upper()
    strong = {
        "LONG": ["BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG", "LIQUIDITY_SWEEP_LONG"],
        "SHORT": ["BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT", "LIQUIDITY_SWEEP_SHORT"],
    }
    weak_hold = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    return setup in strong.get(side, []), setup in weak_hold.get(side, [])


def _setup_range_edge(context, side):
    ict = context.get("ict") or {}
    price = safe_float(context.get("price"))
    pd_pos = safe_float(ict.get("pd_pos"), 0.5) or 0.5
    range_high = safe_float(ict.get("range_high"))
    range_low = safe_float(ict.get("range_low"))
    near_edge = False
    edge_reason = ""
    if range_high and range_low and price:
        width = max(range_high - range_low, price * 0.003)
        low_edge = price <= range_low + width * 0.24
        high_edge = price >= range_high - width * 0.24
        if side == "LONG" and low_edge:
            near_edge = True
            edge_reason = "ціна біля нижнього краю діапазону"
        elif side == "SHORT" and high_edge:
            near_edge = True
            edge_reason = "ціна біля верхнього краю діапазону"
    if side == "LONG" and pd_pos <= 0.26:
        near_edge = True
        edge_reason = edge_reason or "ціна у глибокому discount"
    if side == "SHORT" and pd_pos >= 0.74:
        near_edge = True
        edge_reason = edge_reason or "ціна у глибокому premium"
    return near_edge, edge_reason or "ціна не біля краю діапазону"




def _setup_micro_breakout_state(context, side):
    """Detect a professional early breakout/ignition state from 3m candles.

    This is not a generic momentum chase detector. It looks for a small base /
    compression and then a micro-BOS close through the local 3m range. It is
    used only as an override when the usual retest/FVG confirmation has not
    happened yet.
    """
    price = safe_float((context or {}).get("price"))
    atr15 = safe_float((context or {}).get("atr15"), (price or 90) * 0.006) or ((price or 90) * 0.006)
    candles = (context or {}).get("candles_3m") or []
    if not candles or len(candles) < 14 or side not in ["LONG", "SHORT"]:
        return {"micro_bos": False, "micro_base": False, "compression": False, "reason": "3M бази/пробою не видно"}

    last = candles[-1]
    prev_window = candles[-11:-1]
    base_window = candles[-7:-1]
    if len(prev_window) < 8 or len(base_window) < 4:
        return {"micro_bos": False, "micro_base": False, "compression": False, "reason": "3M бази мало"}

    local_high = max(c.high for c in prev_window)
    local_low = min(c.low for c in prev_window)
    base_high = max(c.high for c in base_window)
    base_low = min(c.low for c in base_window)
    base_range = max(base_high - base_low, 0.0)
    base_body_avg = mean([abs(c.close - c.open) for c in base_window]) if base_window else 0.0
    full_range = max(local_high - local_low, 0.0)

    # BZ is volatile; compression must be relative to ATR, not a fixed tick size.
    compression = bool(base_range <= atr15 * 0.92 or full_range <= atr15 * 1.35)
    micro_base = bool(compression and base_body_avg <= atr15 * 0.34)

    if side == "LONG":
        micro_bos = bool(last.close > local_high and last.close > last.open and close_location(last) >= 0.58)
        direction_reason = f"3M close вище локального high {round_price(local_high)}"
    else:
        micro_bos = bool(last.close < local_low and last.close < last.open and close_location(last) <= 0.42)
        direction_reason = f"3M close нижче локального low {round_price(local_low)}"

    # Avoid calling a huge already-finished candle an ignition. The 3m candle may
    # be strong, but if it is far beyond the local base, this becomes a chase.
    extension_from_base = abs((last.close - ((base_high + base_low) / 2)) / atr15) if atr15 else 0.0
    not_too_far = extension_from_base <= 1.65

    reason = direction_reason if micro_bos else "мікро-BOS ще не закрився"
    if micro_base:
        reason += "; перед цим була проторговка/стиснення"
    if not not_too_far:
        reason += "; але свічка вже надто далеко від бази"

    return {
        "micro_bos": bool(micro_bos and not_too_far),
        "micro_base": bool(micro_base),
        "compression": bool(compression),
        "extension_from_base_atr": round(extension_from_base, 2),
        "local_high": round_price(local_high),
        "local_low": round_price(local_low),
        "reason": reason,
    }


def _setup_range_compression_breakout(context, side):
    """Allow a rare breakout-start from range middle only after compression.

    Range middle remains blocked by default. This override is only for the case
    where the middle of the range is no longer random balance, but a confirmed
    3m compression breakout with participation.
    """
    if side not in ["LONG", "SHORT"]:
        return False, "сторона не визначена"
    tf3 = (context or {}).get("tf3") or {}
    tf15 = (context or {}).get("tf15") or {}
    cvd = (context or {}).get("cvd") or {}
    flow = (context or {}).get("flow") or {}
    derivatives = (context or {}).get("derivatives") or {}
    micro = _setup_micro_breakout_state(context, side)
    range_atr = safe_float(tf15.get("range_atr"), 3.0) or 3.0

    tf3_same = tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 18
    tf3_strong_against = tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 42
    pressure_same = (
        (cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 8)
        or (flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 8)
        or (derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10)
    )
    pressure_against_count = sum([
        cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 16,
        flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 14,
        derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14,
    ])

    compressed_range = bool(range_atr <= 2.35 or micro.get("compression"))
    ok = bool(
        compressed_range
        and micro.get("micro_bos")
        and tf3_same
        and pressure_against_count < 2
        and not tf3_strong_against
        and (pressure_same or abs(int(tf3.get("score", 0) or 0)) >= 32)
    )
    reason = "range compression breakout: " + micro.get("reason", "3M пробій")
    if pressure_same:
        reason += "; CVD/flow/OI не проти"
    return ok, reason

def _setup_rules(setup_type, side):
    side_word_text = side_word(side)
    rules = {
        "TREND_CONTINUATION": {
            "entry_rule": "BOS/утримання пробитого рівня + FVG/OB або утримання після пробою + продовження на 3M",
            "stop_rule": "стоп за локальний swing/OB/FVG, без підтягування до TP1",
            "tp_rule": "TP1 ATR-реалістичний, TP2/TP3 далі по тренду",
            "management_rule": "тримати довше, але після TP1 захистити частину прибутку",
        },
        "TREND_IGNITION_ENTRY": {
            "entry_rule": "ранній старт пробою після проторговки/мікро-BOS; тільки якщо це старт руху, а не кінець імпульсу",
            "stop_rule": "стоп за 3M базу / мікро-swing, не всередині імпульсної свічки",
            "tp_rule": "TP1 не ближче ризику, TP2/TP3 як продовження тренду або відкату",
            "management_rule": "вести як ризиковий ранній вхід: якщо після входу немає продовження руху — швидке попередження на вихід",
        },
        "PULLBACK_CONTINUATION": {
            "entry_rule": "відкат у правильну premium/discount зону + реакція FVG/OB + 3M повернення рівня/відбій",
            "stop_rule": "стоп за low/high відкату або за OB/FVG",
            "tp_rule": "TP1 ближче, TP2 до продовження тренду",
            "management_rule": "якщо 3M не підтвердив після входу — швидке попередження на вихід",
        },
        "PULLBACK_CONTINUATION_FAST_ENTRY": {
            "entry_rule": "ранній вхід на відкаті: 1H/15M або структура за напрям, ціна в FVG/OB/правильній PD-зоні, 3M не сильно проти",
            "stop_rule": "стоп за low/high відкату або за межу FVG/OB з ATR-буфером",
            "tp_rule": "TP1 близько 1.5R або більше; TP2/TP3 тільки якщо 3M підтвердив продовження",
            "management_rule": "вести як ризиковий ранній вхід: якщо після входу немає реакції — швидко підвищувати ризик",
        },
        "PRIME_ICT_LOCATION_OVERRIDE": {
            "entry_rule": "сильна ICT-локація: FVG/OB/зняття ліквідності у правильній зоні + структура за напрям; 3M може бути нейтральним, але не сильно проти",
            "stop_rule": "стоп за екстремум FVG/OB/зняття ліквідності з ATR-буфером",
            "tp_rule": "TP1 до першої ліквідності/EQ, далі тільки якщо 3M підтвердив продовження",
            "management_rule": "ранній ICT-вхід; якщо 3M не підтверджує після входу — ризик швидко підвищується",
        },
        "RANGE_COMPRESSION_BREAKOUT": {
            "entry_rule": "середина діапазону дозволена тільки після стискання + 3M мікро-BOS + участь CVD/потік/OI",
            "stop_rule": "стоп за межу бази стискання / мікро-swing",
            "tp_rule": "TP1 до краю діапазону або першої ліквідності; не тримати як сильний тренд без нового BOS",
            "management_rule": "якщо пробій не продовжився за 1–2 перевірки — переводити в захист або попередження на вихід",
        },
        "SWEEP_REVERSAL": {
            "entry_rule": "зняття high/low + повернення в діапазон + 3M/CVD/потік розворот",
            "stop_rule": "стоп за хвіст зняття ліквідності з ATR-буфером",
            "tp_rule": "TP1 до EQ/першої ліквідності, TP2 до протилежного краю",
            "management_rule": "прибуток захищати швидше, ніж у тренді",
        },
        "SWEEP_RECLAIM_EARLY_ENTRY": {
            "entry_rule": "раннє зняття ліквідності + повернення: ліквідність уже знята, ціна повернулась у діапазон, 3M/потік не проти, але повний CHOCH ще може формуватись",
            "stop_rule": "стоп за хвіст sweep з ATR-буфером; не усереднювати",
            "tp_rule": "TP1 до EQ/першої ліквідності; TP2 тільки після підтвердження 3M/структури",
            "management_rule": "якщо повернення в діапазон не отримало продовження — швидко переводити в попередження або вихід",
        },
        "RANGE_EDGE_TRADE": {
            "entry_rule": "тільки від краю діапазону; середина діапазону заборонена",
            "stop_rule": "стоп за край діапазону, коротший ніж у тренді",
            "tp_rule": "TP1 до EQ, TP2 до протилежного краю діапазону",
            "management_rule": "у боковику фіксувати швидше, не чекати великий трендовий рух",
        },
        "COUNTERTREND_SCALP": {
            "entry_rule": "тільки сильне зняття ліквідності/CHOCH + 3M розворот + CVD/потік не проти",
            "stop_rule": "короткий стоп за екстремум, без усереднення",
            "tp_rule": "швидший TP1, TP2 тільки якщо структура розвертається",
            "management_rule": "агресивний контроль; якщо немає швидкого підтвердження — вихід",
        },
        "NEWS_IMPULSE": {
            "entry_rule": "новинний драйвер + 3M/структура в той самий бік; не доганяти без ретесту",
            "stop_rule": "стоп ширший за шум, але не більше новинного максимального ризику",
            "tp_rule": "TP ближче; новина може дати різкий відкат",
            "management_rule": "швидкий захист прибутку, уважно до розвороту після імпульсу",
        },
        "COUNTERTREND_PULLBACK_WAIT": {
            "entry_rule": "сильний локальний рух проти 1H/4H тренду вважається відкатом, а не новим трендом; вхід тільки після окремого розворотного ICT-сетапу або завершення відкату",
            "stop_rule": "стоп і TP не активувати всередині вже розігнаного відкату",
            "tp_rule": "чекати нову базу, FVG/OB реакцію або повернення за основним трендом",
            "management_rule": "не плутати сильну 3M свічку з новим трендом, коли 1H/4H залишаються проти",
        },
        "LATE_IMPULSE_CHASE": {
            "entry_rule": "вхід заборонений до проторговки, ретесту або нового FVG/OB повернення рівня",
            "stop_rule": "план лише після нового рівня для стопу",
            "tp_rule": "не рахувати TP від поганої локації",
            "management_rule": "чекати перебудову; напрям може бути правильний, але точка входу погана",
        },
        "RANGE_MIDDLE_BLOCK": {
            "entry_rule": "середина діапазону/EQ — входу немає; чекати край або BOS з ретестом",
            "stop_rule": "стоп не визначати з середини діапазону",
            "tp_rule": "математика RR з середини діапазону слабка",
            "management_rule": "тільки чекати",
        },
        "NO_CLEAN_SETUP": {
            "entry_rule": f"для {side_word_text} потрібен один з топ-сетапів: продовження тренду, відкат, зняття ліквідності, край діапазону або новинний імпульс",
            "stop_rule": "стоп/TP не активувати без сетапу",
            "tp_rule": "чекати чистішу структуру",
            "management_rule": "чекати до появи топового сетапу",
        },
    }
    return rules.get(setup_type, rules["NO_CLEAN_SETUP"])


def setup_trade_profile(setup_type):
    """Compact TP/stop overrides by setup class.

    This keeps the bot professional without adding seven independent engines.
    The market regime still exists; setup class only nudges TP/stop/supervision.
    """
    profiles = {
        # min_stop_pct prevents tiny stops when ATR temporarily compresses.
        # Technical RR guard validates TP1 without moving it; weak geometry waits for a better 3M entry.
        "TREND_CONTINUATION": {"regime_override": "TREND", "tp1_atr_mult": 1.45, "tp2_atr_mult": 3.10, "tp3_tail_atr": 1.45, "min_stop_pct": 0.85, "max_stop_pct": MAX_STOP_DISTANCE_PCT, "quality_adjustment": 4, "quality_cap": 92},
        "TREND_IGNITION_ENTRY": {"regime_override": "PULLBACK", "tp1_atr_mult": 1.20, "tp2_atr_mult": 2.35, "tp3_tail_atr": 1.00, "min_stop_pct": 0.82, "max_stop_pct": 1.28, "quality_adjustment": 1, "quality_cap": 79, "force_risky": True},
        "PULLBACK_CONTINUATION": {"regime_override": "PULLBACK", "tp1_atr_mult": 1.30, "tp2_atr_mult": 2.45, "tp3_tail_atr": 1.10, "min_stop_pct": 0.78, "max_stop_pct": 1.45, "quality_adjustment": 3, "quality_cap": 86},
        "PULLBACK_CONTINUATION_FAST_ENTRY": {"regime_override": "PULLBACK", "tp1_atr_mult": 1.18, "tp2_atr_mult": 2.20, "tp3_tail_atr": 0.95, "min_stop_pct": 0.76, "max_stop_pct": 1.34, "quality_adjustment": 1, "quality_cap": 76, "force_risky": True},
        "PRIME_ICT_LOCATION_OVERRIDE": {"regime_override": "REVERSAL", "tp1_pct": 0.92, "tp2_pct": 1.55, "tp3_pct": 2.55, "min_stop_pct": 0.76, "max_stop_pct": 1.22, "quality_adjustment": 1, "quality_cap": 79, "force_risky": True},
        "RANGE_COMPRESSION_BREAKOUT": {"regime_override": "RANGE", "tp1_pct": 0.88, "tp2_pct": 1.35, "tp3_pct": 2.10, "min_stop_pct": 0.74, "max_stop_pct": 1.10, "quality_adjustment": 0, "quality_cap": 77, "force_risky": True},
        "SWEEP_REVERSAL": {"regime_override": "REVERSAL", "tp1_pct": 0.95, "tp2_pct": 1.70, "tp3_pct": 2.85, "min_stop_pct": 0.76, "max_stop_pct": 1.35, "quality_adjustment": 3, "quality_cap": 84},
        "SWEEP_RECLAIM_EARLY_ENTRY": {"regime_override": "REVERSAL", "tp1_pct": 0.86, "tp2_pct": 1.45, "tp3_pct": 2.35, "min_stop_pct": 0.74, "max_stop_pct": 1.18, "quality_adjustment": 0, "quality_cap": 74, "force_risky": True},
        "RANGE_EDGE_TRADE": {"regime_override": "RANGE", "tp1_pct": 0.82, "tp2_pct": 1.25, "tp3_pct": 1.95, "min_stop_pct": 0.72, "max_stop_pct": 1.05, "quality_adjustment": 1, "quality_cap": 80},
        "COUNTERTREND_SCALP": {"regime_override": "REVERSAL", "tp1_pct": 0.80, "tp2_pct": 1.22, "tp3_pct": 2.00, "min_stop_pct": 0.70, "max_stop_pct": 1.05, "quality_adjustment": -3, "quality_cap": 76, "force_risky": True},
        "NEWS_IMPULSE": {"regime_override": "NEWS_IMPULSE", "tp1_pct": 0.90, "tp2_pct": 1.60, "tp3_pct": 2.60, "min_stop_pct": 0.82, "max_stop_pct": 1.30, "quality_adjustment": -2, "quality_cap": 79, "force_risky": True},
        "LATE_IMPULSE_CHASE": {"quality_adjustment": -12, "quality_cap": 61, "block_entry": True},
        "COUNTERTREND_PULLBACK_WAIT": {"quality_adjustment": -10, "quality_cap": 60, "block_entry": True},
        "RANGE_MIDDLE_BLOCK": {"quality_adjustment": -10, "quality_cap": 55, "block_entry": True},
        "NO_CLEAN_SETUP": {"quality_adjustment": -6, "quality_cap": 66, "block_entry": True},
    }
    return dict(profiles.get(str(setup_type or "NO_CLEAN_SETUP"), profiles["NO_CLEAN_SETUP"]))


def classify_setup_candidate(context, side):
    """Classify one side into the dominant professional setup type.

    The classifier is intentionally compact: it reuses existing ICT/structure/3M/CVD
    features and produces one top setup, not many competing labels.
    """
    if side not in ["LONG", "SHORT"] or not isinstance(context, dict):
        return {"type": "NO_CLEAN_SETUP", "side": side or "NEUTRAL", "score": 0, "entry_allowed": False, "block_entry": True, "reason": "немає сторони для класифікації"}

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    derivatives = context.get("derivatives") or {}
    news = context.get("news") or {}
    calendar = context.get("calendar") or {}
    liquidity = context.get("liquidity") or {}

    phase = str(structure.get("phase", "") or "").upper()
    ict_setup = str(ict.get("setup", "") or "").upper()
    pd = str(ict.get("premium_discount", "") or "").upper()
    market_regime = context.get("market_regime") or detect_market_regime(context, side)
    regime_name = str((market_regime or {}).get("name", "NORMAL") or "NORMAL").upper()

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    tf3_same = tf3.get("bias") == side and tf3_score_abs >= 18
    tf3_strong_same = tf3.get("bias") == side and tf3_score_abs >= 32
    tf3_neutral = tf3.get("bias") == "NEUTRAL" or tf3_score_abs < 18
    tf3_weak_pullback = tf3.get("bias") == opposite(side) and tf3_score_abs < 32
    tf3_strong_against = tf3.get("bias") == opposite(side) and tf3_score_abs >= 42
    tf15_same = tf15.get("bias") == side and abs(int(tf15.get("score", 0) or 0)) >= 18
    tf15_strong_same = tf15.get("bias") == side and abs(int(tf15.get("score", 0) or 0)) >= 30
    structure_same = structure.get("bias") == side or (side == "LONG" and any(x in phase for x in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"])) or (side == "SHORT" and any(x in phase for x in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]))
    htf_same = tf1h.get("bias") == side or tf4h.get("bias") == side or _setup_side_score(tf1h, side) >= 18 or _setup_side_score(tf4h, side) >= 18
    htf_against = tf1h.get("bias") == opposite(side) or tf4h.get("bias") == opposite(side) or _setup_side_score(tf1h, side) <= -18 or _setup_side_score(tf4h, side) <= -28
    cvd_same = cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 8
    flow_same = flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 8
    oi_same = derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10
    pressure_same = cvd_same or flow_same or oi_same
    pressure_against_count = sum([
        cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 16,
        flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 14,
        derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14,
    ])

    ict_strong_zone, ict_hold = _setup_has_zone_for_side(ict, side)
    ict_same = ict.get("bias") == side
    ict_entry_model = bool(ict_same and ict.get("entry_ok"))
    correct_pd = (side == "LONG" and pd == "DISCOUNT") or (side == "SHORT" and pd == "PREMIUM")
    bos_same = (side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase)
    sweep_same = (
        (side == "LONG" and ("DOWNSIDE SWEEP" in phase or "CHOCH LONG" in phase or ict_setup == "LIQUIDITY_SWEEP_LONG"))
        or (side == "SHORT" and ("UPSIDE SWEEP" in phase or "CHOCH SHORT" in phase or ict_setup == "LIQUIDITY_SWEEP_SHORT"))
    )
    continuation_hold = bool(ict_hold or (bos_same and tf3_same and structure_same))
    range_context = bool(regime_name == "RANGE" or tf15.get("trend") == "RANGE" or ict_setup == "BALANCE_MIDRANGE" or pd == "MIDRANGE")
    at_range_edge, range_edge_reason = _setup_range_edge(context, side)
    micro_breakout = _setup_micro_breakout_state(context, side)
    range_breakout_ok, range_breakout_reason = _setup_range_compression_breakout(context, side)

    late, late_reason = is_late_chase(side, context)
    late_penalty = soft_late_entry_penalty(side, context, late_reason) if late else 0
    exhausted, exhausted_reason = detect_exhausted_move(side, context)
    hard_liquidity_block = side in (liquidity.get("blocks") or [])

    # Distinguish a fast countertrend rebound from a genuine new trend.
    # A strong 3M candle is not enough to call TREND_IGNITION when both 1H and
    # 4H still point the other way, the move is already extended, and there is
    # no fresh ICT reversal/retest location. In that case the correct state is
    # WAIT: this is a pullback against the main trend, not a new market entry.
    fast_move_3m = abs(safe_float(tf3.get("fast_move_pct"), 0.0) or 0.0)
    drift_3m = abs(safe_float(tf3.get("drift_pct"), 0.0) or 0.0)
    htf_both_against = bool(tf1h.get("bias") == opposite(side) and tf4h.get("bias") == opposite(side))
    countertrend_pullback_wait = bool(
        htf_both_against
        and tf3_same
        and (structure_same or bos_same)
        and not ict_entry_model
        and not ict_strong_zone
        and not sweep_same
        and (late or fast_move_3m >= 0.75 or drift_3m >= 0.80)
        and (
            pd in ["PREMIUM", "MIDRANGE", ""]
            if side == "LONG"
            else pd in ["DISCOUNT", "MIDRANGE", ""]
        )
    )

    # Early Professional Override Layer. These are narrow exceptions, not a
    # weakening of the whole filter stack. They let the bot take a real early
    # professional setup while still blocking naked late/chase entries.
    ignition_from_base = bool(
        micro_breakout.get("micro_bos")
        and micro_breakout.get("micro_base")
        and safe_float(micro_breakout.get("extension_from_base_atr"), 99.0) <= 1.20
    )
    fresh_bos_not_extended = bool(
        bos_same
        and tf3_same
        and fast_move_3m <= 0.68
        and drift_3m <= 0.78
        and late_penalty <= 7
    )
    trend_ignition_ok = bool(
        not range_context
        and not countertrend_pullback_wait
        and tf3_same
        and (ignition_from_base or fresh_bos_not_extended)
        and (tf15_same or structure_same or htf_same)
        and (bos_same or structure_same or continuation_hold or ignition_from_base)
        and pressure_against_count < 2
        and not tf3_strong_against
        and not exhausted
        and not (late and not ignition_from_base)
    )
    strong_ict_permission = strong_ict_early_permission_snapshot(context, side)
    professional_entry_evidence = _fresh_professional_entry_evidence(context, side)
    composite_guard = composite_exhaustion_snapshot(context, side)
    persistent_guard = persistent_exhaustion_lock_snapshot(context, side)
    context["strong_ict_early_permission"] = strong_ict_permission
    context["composite_exhaustion"] = composite_guard
    context.setdefault("persistent_exhaustion_locks", {})[side] = persistent_guard

    prime_ict_override_ok = bool(
        strong_ict_permission.get("allowed")
        and (correct_pd or sweep_same or professional_entry_evidence.get("fresh_event"))
        and structure_same
        and pressure_against_count < 2
        and not tf3_strong_against
        and not exhausted
        and not composite_guard.get("hard_block")
        and not persistent_guard.get("active")
        and late_penalty < 18
    )
    pullback_fast_entry_ok = bool(
        htf_same
        and correct_pd
        and (ict_strong_zone or ict_entry_model or ict_hold)
        and (tf15_same or structure_same or bos_same or continuation_hold)
        and (tf3_same or tf3_neutral or tf3_weak_pullback)
        and pressure_against_count < 2
        and not tf3_strong_against
        and not exhausted
        and late_penalty < 15
    )
    sweep_reclaim_early_ok = bool(
        sweep_same
        and (ict_same or structure_same or ict_entry_model)
        and (tf3_same or tf3_neutral or pressure_same)
        and pressure_against_count < 2
        and not tf3_strong_against
        and not exhausted
        and late_penalty < 15
    )
    professional_chase_exception = bool(
        (trend_ignition_ok or prime_ict_override_ok or range_breakout_ok or pullback_fast_entry_ok or sweep_reclaim_early_ok)
        and professional_entry_evidence.get("professional_exception")
        and late_penalty < 18
        and not exhausted
        and not composite_guard.get("hard_block")
        and not persistent_guard.get("active")
    )

    def result(setup_type, score, entry_allowed, reason, block_entry=False, risk_mode="NORMAL", extra=None):
        profile = setup_trade_profile(setup_type)
        rules = _setup_rules(setup_type, side)
        out = {
            "type": setup_type,
            "label": _setup_label(setup_type),
            "side": side,
            "score": int(max(0, min(100, score))),
            "entry_allowed": bool(entry_allowed and not hard_liquidity_block),
            "block_entry": bool(block_entry or profile.get("block_entry") or hard_liquidity_block),
            "risk_mode": risk_mode,
            "reason": reason if not hard_liquidity_block else "ліквідність/ринковий блок проти цього входу",
            "quality_adjustment": int(profile.get("quality_adjustment", 0) or 0),
            "quality_cap": profile.get("quality_cap"),
            "force_risky": bool(profile.get("force_risky") or risk_mode == "RISKY"),
            "profile": profile,
        }
        out.update(rules)
        if extra:
            out.update(extra)
        return out

    # 1) Hard location filters first: correct direction is not enough if location is bad.
    if hard_liquidity_block:
        return result("NO_CLEAN_SETUP", 35, False, "ліквідність блокує цей бік", block_entry=True)

    if countertrend_pullback_wait:
        return result(
            "COUNTERTREND_PULLBACK_WAIT",
            36,
            False,
            "це сильний локальний відкат проти основного 1H/4H тренду, а не новий Trend Ignition; імпульс уже відбувся — чекати завершення відкату, нову базу або окремий ICT-розворот",
            block_entry=True,
            extra={
                "market_move_type": "COUNTERTREND_PULLBACK",
                "late_penalty": int(late_penalty),
                "fast_move_3m": round(fast_move_3m, 3),
            },
        )

    # Hard anti-chase: any late impulse is blocked unless the price has already
    # created a professional reason to enter from the current location:
    # fresh FVG/OB reaction, BOS hold/continuation-hold, or a real sweep/reclaim.
    # This keeps early ICT entries, but stops naked market entries after a vertical candle.
    late_without_reset = bool(late and not (professional_entry_evidence.get("professional_exception") or professional_chase_exception))
    package_block_reason = (
        persistent_guard.get("reason") if persistent_guard.get("active") else
        composite_guard.get("reason") if composite_guard.get("hard_block") else
        exhausted_reason or late_reason or "рух уже розтягнутий — вхід буде доганянням; потрібен retest/FVG-OB/reclaim або проторговка"
    )
    if (
        persistent_guard.get("active")
        or composite_guard.get("hard_block")
        or exhausted
        or late_without_reset
        or (late_penalty >= 15 and not professional_chase_exception)
        or (ict_same and ict.get("no_chase") and not strong_ict_permission.get("allowed") and not professional_chase_exception)
    ):
        return result(
            "LATE_IMPULSE_CHASE",
            30,
            False,
            package_block_reason,
            block_entry=True,
            extra={
                "late_penalty": int(late_penalty), "late_without_reset": late_without_reset,
                "composite_exhaustion": composite_guard,
                "persistent_exhaustion": persistent_guard,
            },
        )

    # 2) Range: only edge trades, except a confirmed compression breakout.
    if range_context and not at_range_edge and range_breakout_ok:
        score = 62 + (8 if tf3_strong_same else 0) + (6 if pressure_same else 0) + (4 if structure_same else 0)
        return result(
            "RANGE_COMPRESSION_BREAKOUT",
            score,
            True,
            range_breakout_reason,
            risk_mode="RISKY",
            extra={"professional_override": True, "range_breakout": True, "late_penalty": int(late_penalty)},
        )

    if range_context and not at_range_edge and not sweep_same and not bos_same:
        return result("RANGE_MIDDLE_BLOCK", 38, False, "ціна в середині range/EQ — входити не професійно", block_entry=True)

    if range_context and at_range_edge:
        entry_allowed = bool((tf3_same or sweep_same or ict_entry_model) and pressure_against_count < 2 and not tf3_strong_against)
        score = 58 + (10 if tf3_same else 0) + (8 if sweep_same else 0) + (6 if pressure_same else 0)
        return result("RANGE_EDGE_TRADE", score, entry_allowed, range_edge_reason if entry_allowed else "край діапазону є, але потрібен 3M повернення/відбій або зняття ліквідності", risk_mode="RISKY" if not tf15_same else "NORMAL")

    # 3) Prime ICT location override: do not wait for a perfect 3M if the location is already professional.
    if prime_ict_override_ok and not sweep_same:
        score = 64 + (8 if tf3_same else 0) + (7 if pressure_same else 0) + (5 if correct_pd else 0)
        return result(
            "PRIME_ICT_LOCATION_OVERRIDE",
            score,
            True,
            "prime ICT location: FVG/OB/sweep зона + структура за напрям; 3M не сильно проти",
            risk_mode="RISKY",
            extra={"professional_override": True, "prime_ict_override": True, "late_penalty": int(late_penalty)},
        )

    # 4) Trend ignition / breakout-start before a full retest.
    if trend_ignition_ok and not (ict_strong_zone or ict_hold):
        score = 63 + (8 if tf3_strong_same else 0) + (7 if pressure_same else 0) + (5 if htf_same else 0)
        ignition_reason = (
            micro_breakout.get("reason", "3M мікро-BOS після бази")
            if ignition_from_base
            else "свіжий BOS за напрямом без розтягнутого 3M імпульсу"
        )
        return result(
            "TREND_IGNITION_ENTRY",
            score,
            True,
            "ранній старт тренду: " + ignition_reason,
            risk_mode="RISKY",
            extra={"professional_override": True, "trend_ignition": True, "late_penalty": int(late_penalty)},
        )

    # 5) Sweep Reclaim Early Entry: sweep/reclaim can be taken before a perfect CHOCH if risk is controlled.
    # Priority is above generic pullback, because a real liquidity sweep has its own stop/TP logic.
    if sweep_reclaim_early_ok and not tf3_same:
        score = 61 + (7 if pressure_same else 0) + (6 if ict_entry_model else 0) + (4 if correct_pd else 0)
        return result(
            "SWEEP_RECLAIM_EARLY_ENTRY",
            score,
            True,
            "раннє зняття ліквідності + повернення: ліквідність знята, ціна повернулась у діапазон, 3M/потік не проти",
            risk_mode="RISKY",
            extra={"professional_override": True, "sweep_reclaim_early": True, "late_penalty": int(late_penalty)},
        )

    # 6) Pullback Continuation Fast Entry: increase good entries without lowering global thresholds.
    if pullback_fast_entry_ok and not tf3_same:
        score = 62 + (6 if ict_strong_zone else 3) + (5 if structure_same else 0) + (4 if tf15_same else 0) + (4 if pressure_same else 0)
        return result(
            "PULLBACK_CONTINUATION_FAST_ENTRY",
            score,
            True,
            "ранній вхід на відкаті: HTF/структура за напрям, ціна у правильній ICT-зоні, 3M не сильно проти",
            risk_mode="RISKY",
            extra={"professional_override": True, "pullback_fast_entry": True, "late_penalty": int(late_penalty)},
        )

    # 7) Directional News Consensus. NEWS_SHOCK is context, not directional proof.
    news_consensus = news_directional_consensus_snapshot(context, side)
    if news_consensus.get("active"):
        entry_allowed = bool(
            news_consensus.get("allowed")
            and tf3_same
            and (structure_same or ict_entry_model or tf15_same)
            and pressure_against_count < 2
        )
        score = 56 + (12 if tf3_same else 0) + (8 if structure_same else 0) + (5 if pressure_same else 0)
        return result(
            "NEWS_IMPULSE", score, entry_allowed,
            news_consensus.get("reason"),
            block_entry=not entry_allowed,
            risk_mode="RISKY",
            extra={"news_consensus": news_consensus, "mandatory_retest": bool(news_consensus.get("late"))},
        )

    # 4) Sweep reversal: priority over generic countertrend, because it is a real ICT model.
    if sweep_same or (ict_setup in ["LIQUIDITY_SWEEP_LONG", "LIQUIDITY_SWEEP_SHORT"] and ict_same):
        entry_allowed = bool((tf3_same or ict_entry_model) and pressure_against_count < 2 and not tf3_strong_against)
        score = 62 + (12 if tf3_same else 0) + (8 if pressure_same else 0) + (5 if correct_pd else 0)
        return result("SWEEP_REVERSAL", score, entry_allowed, "зняття ліквідності + повернення є, але потрібне 3M або потік/CVD підтвердження" if not entry_allowed else "зняття ліквідності + повернення в діапазон", risk_mode="RISKY" if htf_against else "NORMAL")

    # 5) Countertrend scalp: only if there is a real local reversal model.
    if htf_against and (ict_entry_model or structure_same) and (tf3_same or pressure_same):
        entry_allowed = bool((tf3_same or pressure_same) and pressure_against_count < 2 and not tf3_strong_against)
        score = 55 + (10 if tf3_same else 0) + (8 if ict_entry_model else 0) + (5 if pressure_same else 0)
        return result("COUNTERTREND_SCALP", score, entry_allowed, "контртренд дозволений тільки як scalp після локального розвороту", risk_mode="RISKY")

    # 6) Trend continuation: needs BOS/hold + ICT location/hold + 3M continuation.
    if (tf15_strong_same or (tf15_same and htf_same)) and structure_same and (bos_same or continuation_hold) and (ict_strong_zone or ict_hold) and tf3_same:
        score = 70 + (8 if htf_same else 0) + (6 if pressure_same else 0) + (6 if ict_strong_zone else 2)
        return result("TREND_CONTINUATION", score, True, "BOS/утримання рівня + ICT-зона/hold + 3M continuation")

    # 7) Pullback continuation: HTF trend is alive, price is in correct PD zone, entry needs reclaim.
    if htf_same and correct_pd and (ict_strong_zone or ict_entry_model or structure_same) and not tf3_strong_against:
        entry_allowed = bool((tf3_same or ict_entry_model) and pressure_against_count < 2)
        score = 60 + (10 if tf3_same else 0) + (8 if ict_strong_zone else 0) + (5 if pressure_same else 0)
        return result("PULLBACK_CONTINUATION", score, entry_allowed, "відкат у правильну ICT-зону; чекати 3M повернення/відбій" if not entry_allowed else "відкат у правильну ICT-зону + реакція")

    # Fallback: no top setup. The bot may still show WATCH, but should not open.
    fallback_score = 42 + (8 if tf3_same else 0) + (6 if tf15_same else 0) + (5 if structure_same else 0)
    return result("NO_CLEAN_SETUP", fallback_score, False, "немає одного з топових сетапів для професійного входу", block_entry=True)


def classify_setup(context, preferred_side=None):
    """Return the dominant setup class for the preferred side or best side.

    If no side is known yet, both LONG and SHORT are checked and the stronger
    non-blocked candidate is returned. This makes the classifier a first-class
    layer instead of just a label printed after direction.
    """
    if preferred_side in ["LONG", "SHORT"]:
        return classify_setup_candidate(context, preferred_side)
    candidates = [classify_setup_candidate(context, side) for side in ["LONG", "SHORT"]]
    candidates.sort(key=lambda x: (bool(x.get("entry_allowed")), int(x.get("score", 0) or 0)), reverse=True)
    best = candidates[0] if candidates else {"type": "NO_CLEAN_SETUP", "side": "NEUTRAL", "score": 0, "entry_allowed": False, "block_entry": True}
    if int(best.get("score", 0) or 0) < 55 and not best.get("entry_allowed"):
        best = dict(best)
        best["side"] = "NEUTRAL"
    return best


def setup_classifier_text(setup_info, short=False):
    if not setup_info:
        return ""
    label = setup_info.get("label") or _setup_label(setup_info.get("type"))
    score = int(setup_info.get("score", 0) or 0)
    if short:
        return f"{label} ({score}/100)"
    # Keep user-facing Telegram compact: no extra status like "вхід дозволений"/"тільки чекати".
    # The final entry level already tells the user whether this is full entry, risky entry, wait, or block.
    return f"<b>Сетап:</b> {label} | {score}/100"


def setup_watch_title(side, setup_info):
    setup_type = str((setup_info or {}).get("type") or "NO_CLEAN_SETUP")
    side_text = _side_label(side)
    if setup_type == "NO_CLEAN_SETUP":
        return "ВХОДУ НЕМАЄ — ЧИСТОГО СЕТАПУ НЕМАЄ"
    if setup_type == "LATE_IMPULSE_CHASE":
        return f"ЧЕКАТИ — {side_text} НЕ ДОГАНЯТИ"
    if setup_type == "RANGE_MIDDLE_BLOCK":
        return f"ЧЕКАТИ — {side_text} СЕРЕДИНА ДІАПАЗОНУ"
    if setup_type == "RANGE_EDGE_TRADE":
        return f"ЧЕКАТИ — {side_text} КРАЙ ДІАПАЗОНУ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ"
    if setup_type == "RANGE_COMPRESSION_BREAKOUT":
        return f"ЧЕКАТИ — {side_text} ПРОБІЙ ПІСЛЯ СТИСКАННЯ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ"
    if setup_type == "TREND_IGNITION_ENTRY":
        return f"ЧЕКАТИ — {side_text} РАННІЙ СТАРТ ТРЕНДУ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ"
    if setup_type == "PRIME_ICT_LOCATION_OVERRIDE":
        return f"ЧЕКАТИ — {side_text} СИЛЬНА ICT-ЛОКАЦІЯ ЩЕ НЕ ПІДТВЕРДЖЕНА"
    if setup_type == "PULLBACK_CONTINUATION_FAST_ENTRY":
        return f"ЧЕКАТИ — {side_text} РАННІЙ ВІДКАТ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ"
    if setup_type == "SWEEP_RECLAIM_EARLY_ENTRY":
        return f"ЧЕКАТИ — {side_text} ЗНЯТТЯ ЛІКВІДНОСТІ + ПОВЕРНЕННЯ ЩЕ НЕ ПІДТВЕРДЖЕНЕ"
    if setup_type == "NEWS_IMPULSE":
        return f"ЧЕКАТИ — {side_text} НОВИННИЙ ІМПУЛЬС БЕЗ РЕТЕСТУ"
    return f"ЧЕКАТИ — {side_text} СЕТАП НЕ ГОТОВИЙ"


def _zone_bounds(zone):
    if not isinstance(zone, dict):
        return None, None
    low = safe_float(zone.get("low"))
    high = safe_float(zone.get("high"))
    if low is None or high is None:
        return None, None
    return min(low, high), max(low, high)


def _append_level(items, level, basis, price, side, priority=50):
    level = safe_float(level)
    if level is None or not price:
        return
    if side == "LONG" and level <= price:
        return
    if side == "SHORT" and level >= price:
        return
    items.append({"level": float(level), "basis": str(basis), "priority": int(priority)})


def _dedupe_target_levels(items, side, atr15):
    if side == "LONG":
        ordered = sorted(items, key=lambda x: (x["level"], -x["priority"]))
    else:
        ordered = sorted(items, key=lambda x: (-x["level"], -x["priority"]))
    merged = []
    merge_gap = max((atr15 or 0) * 0.18, 1e-8)
    for item in ordered:
        if merged and abs(item["level"] - merged[-1]["level"]) <= merge_gap:
            if item["priority"] > merged[-1]["priority"]:
                merged[-1] = item
            continue
        merged.append(item)
    return merged


def _recent_swing_liquidity(candles, side, max_items=8):
    candles = list(candles or [])
    if len(candles) < 12:
        return []
    sample = candles[-110:]
    highs, lows = swing_points(sample, 2)
    points = highs if side == "LONG" else lows
    result = []
    for point in points[-max_items:]:
        result.append((safe_float(point.get("price")), int(point.get("idx", 0))))
    return result



def _technical_stop_candidates(side, context, atr15):
    """Return several real invalidation choices instead of one rigid stop.

    The list contains tactical 3M trigger invalidations and durable 15M/ICT
    invalidations. Every candidate is tied to an observed swing, FVG, OB or
    closed-candle structure; no stop is invented just to improve RR.
    """
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return []
    ict = (context or {}).get("ict") or {}
    structure = (context or {}).get("structure") or {}
    candles15 = list((context or {}).get("candles_15m_closed") or (context or {}).get("candles_15m") or [])
    candles3 = list((context or {}).get("candles_3m") or [])
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    atr3 = atr(candles3, 14) if len(candles3) >= 3 else None
    atr3 = safe_float(atr3, atr15 * 0.32) or atr15 * 0.32
    buffer15 = max(atr15 * 0.14, price * 0.0007)
    buffer3 = max(atr3 * 0.24, price * 0.00035)
    min_noise = max(atr3 * 0.80, atr15 * 0.20, price * 0.0018)
    out = []

    def add(level, basis, priority, timeframe, kind):
        level = safe_float(level)
        if level is None:
            return
        if side == "LONG" and level >= price:
            return
        if side == "SHORT" and level <= price:
            return
        distance = abs(price - level)
        if distance < min_noise:
            level = price - min_noise if side == "LONG" else price + min_noise
            distance = min_noise
        out.append({
            "level": float(level),
            "basis": basis,
            "priority": int(priority),
            "timeframe": timeframe,
            "kind": kind,
            "distance": float(distance),
        })

    # Specialized 15M bases are explicit technical invalidations, not arbitrary
    # stop compression. They outrank generic recent swings but remain subject to
    # the same noise and geometry checks as every other candidate.
    fresh_geo = (context or {}).get("fresh_base_geometry") or {}
    if fresh_geo.get("side") == side and fresh_geo.get("confirmed"):
        add(fresh_geo.get("stop_level"), "fresh 15M continuation base invalidation", 110, "15M", "FRESH_BASE")
    cap_geo = (context or {}).get("capitulation_recovery_geometry") or {}
    if cap_geo.get("side") == side and cap_geo.get("confirmed"):
        add(cap_geo.get("stop_level"), "post-capitulation 15M reclaim base invalidation", 112, "15M", "CAPITULATION_BASE")

    # Exact micro invalidation recovered from the closed 3M sequence between
    # 15-minute workflow runs. It is eligible only after the resolver confirms
    # that the event is still alive and not extended.
    rescue_event = (context or {}).get("entry_rescue_event") or {}
    if (
        rescue_event.get("confirmed")
        and rescue_event.get("side") == side
        and rescue_event.get("anchor_confirmed")
        and safe_float(rescue_event.get("extension_atr15"), 99) <= ENTRY_RESCUE_MAX_EXTENSION_ATR15
    ):
        add(
            rescue_event.get("stop_level"),
            "3M interval " + str(rescue_event.get("type") or "trigger") + " invalidation",
            106,
            "3M",
            "INTERVAL_RESCUE_EVENT",
        )

    # 3M trigger structure: preferred for early entries, but still technically grounded.
    for level, idx3 in _recent_swing_liquidity(candles3, "SHORT" if side == "LONG" else "LONG", 8):
        if level is None:
            continue
        if abs(price - level) <= atr15 * 1.65:
            stop_level = level - buffer3 if side == "LONG" else level + buffer3
            add(stop_level, "3M trigger swing invalidation", 94 + min(3, idx3 // 35), "3M", "TRIGGER_SWING")

    # ICT zones can define both fast and structural invalidation.
    if side == "LONG":
        for key, label, priority in [("bull_ob", "bullish OB invalidation", 100), ("bull_fvg", "bullish FVG invalidation", 97)]:
            low, high = _zone_bounds(ict.get(key))
            if low is not None and low < price and price - high <= atr15 * 1.65:
                add(low - buffer15, label, priority, "ICT", key.upper())
        for level, idx15 in _recent_swing_liquidity(candles15, "SHORT", 8):
            if level is not None and price - level <= atr15 * 3.4:
                add(level - buffer15, "closed 15M swing low invalidation", 90 + min(4, idx15 // 25), "15M", "SWING")
        add(safe_float(structure.get("swing_low"), None) - buffer15 if safe_float(structure.get("swing_low"), None) is not None else None,
            "15M structural swing low", 88, "15M", "STRUCTURE")
        add(safe_float(structure.get("recent_low"), None) - buffer15 if safe_float(structure.get("recent_low"), None) is not None else None,
            "15M recent structure low", 78, "15M", "STRUCTURE")
        if candles15:
            add(candles15[-1].low - buffer15, "last closed 15M low", 68, "15M", "CANDLE")
    else:
        for key, label, priority in [("bear_ob", "bearish OB invalidation", 100), ("bear_fvg", "bearish FVG invalidation", 97)]:
            low, high = _zone_bounds(ict.get(key))
            if high is not None and high > price and low - price <= atr15 * 1.65:
                add(high + buffer15, label, priority, "ICT", key.upper())
        for level, idx15 in _recent_swing_liquidity(candles15, "LONG", 8):
            if level is not None and level - price <= atr15 * 3.4:
                add(level + buffer15, "closed 15M swing high invalidation", 90 + min(4, idx15 // 25), "15M", "SWING")
        add(safe_float(structure.get("swing_high"), None) + buffer15 if safe_float(structure.get("swing_high"), None) is not None else None,
            "15M structural swing high", 88, "15M", "STRUCTURE")
        add(safe_float(structure.get("recent_high"), None) + buffer15 if safe_float(structure.get("recent_high"), None) is not None else None,
            "15M recent structure high", 78, "15M", "STRUCTURE")
        if candles15:
            add(candles15[-1].high + buffer15, "last closed 15M high", 68, "15M", "CANDLE")

    # Deduplicate almost identical levels and keep the strongest explanation.
    out.sort(key=lambda x: (x["distance"], -x["priority"]))
    merged = []
    gap = max(atr3 * 0.22, price * 0.00035)
    for item in out:
        same = next((m for m in merged if abs(m["level"] - item["level"]) <= gap), None)
        if same is None:
            merged.append(item)
        elif item["priority"] > same["priority"]:
            merged[merged.index(same)] = item
    return merged[:14]


def _tp1_travel_profile(context, setup_type, price, atr15):
    """Preferred and viable travel profiles for a full-position 15M target.

    Preferred geometry targets the user's normal 15M objective. Viable geometry
    is a controlled fallback for a real technical setup, not a tiny scalp: it
    still requires a meaningful percentage move, ATR travel and RR. This avoids
    the old deadlock where a nearby breakout gate made every farther TP illegal.
    """
    context = context or {}
    setup_type = str(setup_type or "").upper()
    regime = context.get("regime_engine") or context.get("market_regime") or {}
    if isinstance(regime, dict):
        regime_name = str(regime.get("regime_type") or regime.get("name") or "NORMAL").upper()
    else:
        regime_name = str(regime or "NORMAL").upper()

    preferred_pct = float(MIN_TP1_DISTANCE_PCT)
    preferred_atr = float(MIN_TP1_ATR_15M)
    viable_pct = float(MIN_VIABLE_TP1_DISTANCE_PCT)
    viable_atr = float(MIN_VIABLE_TP1_ATR_15M)

    if any(k in regime_name for k in ["TREND_EXPANSION", "IMPULSE", "NEWS_SHOCK"]):
        preferred_pct = max(preferred_pct, 1.35)
        preferred_atr = max(preferred_atr, 1.65)
        viable_pct = max(viable_pct, 1.00)
        viable_atr = max(viable_atr, 1.20)
    elif any(k in regime_name for k in ["TREND_PULLBACK", "PULLBACK"]):
        preferred_pct = max(preferred_pct, 1.20)
        preferred_atr = max(preferred_atr, 1.50)
    elif any(k in regime_name for k in ["RANGE", "COMPRESSION"]):
        preferred_pct = max(0.95, min(preferred_pct, 1.10))
        preferred_atr = max(1.30, min(preferred_atr, 1.45))
        viable_pct = max(0.80, min(viable_pct, 0.95))
        viable_atr = max(1.00, min(viable_atr, 1.15))

    if setup_type in {"TREND_IGNITION_ENTRY", "NEWS_IMPULSE", "RANGE_COMPRESSION_BREAKOUT"}:
        preferred_pct = max(preferred_pct, 1.35)
        preferred_atr = max(preferred_atr, 1.65)
        viable_pct = max(viable_pct, 1.00)
        viable_atr = max(viable_atr, 1.20)
    elif setup_type in {"PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "TREND_CONTINUATION"}:
        preferred_pct = max(preferred_pct, 1.20)
        preferred_atr = max(preferred_atr, 1.50)
    elif setup_type in {"COUNTERTREND_SCALP", "RANGE_EDGE_TRADE"}:
        preferred_pct = max(1.00, min(preferred_pct, 1.15))
        preferred_atr = max(1.35, min(preferred_atr, 1.50))

    preferred_pct_distance = float(price) * preferred_pct / 100.0
    preferred_atr_distance = float(atr15) * preferred_atr
    viable_pct_distance = float(price) * viable_pct / 100.0
    viable_atr_distance = float(atr15) * viable_atr
    return {
        "min_pct": round(preferred_pct, 3),
        "min_atr": round(preferred_atr, 3),
        "pct_distance": preferred_pct_distance,
        "atr_distance": preferred_atr_distance,
        "absolute_floor": max(preferred_pct_distance, preferred_atr_distance),
        "viable_min_pct": round(viable_pct, 3),
        "viable_min_atr": round(viable_atr, 3),
        "viable_pct_distance": viable_pct_distance,
        "viable_atr_distance": viable_atr_distance,
        "viable_absolute_floor": max(viable_pct_distance, viable_atr_distance),
    }

def _measured_move_target(side, context, price, atr15, required_reward):
    """Create a technical 15M projection when visible liquidity is too close.

    This is not an arbitrary farther TP. It uses the recent closed-15M dealing
    range/impulse and ATR expansion. Nearby real levels remain checkpoints and
    barrier penalties still apply.
    """
    candles15 = list((context or {}).get("candles_15m_closed") or (context or {}).get("candles_15m") or [])
    recent = candles15[-20:] if candles15 else []
    range_height = 0.0
    impulse_height = 0.0
    if len(recent) >= 6:
        range_height = max(c.high for c in recent) - min(c.low for c in recent)
        last8 = recent[-8:]
        impulse_height = abs(last8[-1].close - last8[0].open)
    technical_projection = max(
        float(required_reward),
        float(atr15) * 1.65,
        range_height * 0.55,
        impulse_height * 1.10,
    )
    direction = 1.0 if side == "LONG" else -1.0
    return {
        "level": float(price) + direction * technical_projection,
        "basis": "15M measured range/impulse expansion objective",
        "priority": 78,
        "projected": True,
    }



def _strong_barriers_between(side, price, selected_level, targets, exclude_level=None):
    """Return strong unbroken technical barriers between entry and a target."""
    barriers = []
    tolerance = max(abs(float(price)) * 0.00015, 1e-8)
    for item in targets or []:
        level = safe_float(item.get("level"))
        if level is None:
            continue
        if exclude_level is not None and abs(level - float(exclude_level)) <= tolerance:
            continue
        between = price < level < selected_level if side == "LONG" else selected_level < level < price
        if between and int(item.get("priority", 0) or 0) >= STRONG_BARRIER_PRIORITY:
            barriers.append(item)
    barriers.sort(key=lambda x: abs(float(x["level"]) - float(price)))
    return barriers



def _barrier_clearance_state(side, context, barrier, price, atr15, setup_type, snapshot=None):
    """Classify a strong level as hard, cleared, or an active breakout gate.

    A nearby 15M swing can be the trigger level of a trend-ignition setup rather
    than a reason to declare every farther objective invalid. Major 1H/external
    levels and opposing ICT zones stay hard until price actually clears/retests
    them. The classification is internal and never changes Telegram formatting.
    """
    context = context or {}
    snapshot = snapshot or ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    level = safe_float((barrier or {}).get("level"))
    if side not in ["LONG", "SHORT"] or level is None or not price:
        return {"status": "HARD", "reason": "invalid barrier"}
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    basis = str((barrier or {}).get("basis") or "").lower()
    priority = int((barrier or {}).get("priority", 0) or 0)
    distance = abs(level - price)
    distance_atr = distance / atr15 if atr15 else 99.0
    distance_pct = distance / price * 100.0 if price else 99.0
    buffer = max(atr15 * 0.05, price * 0.00025)

    c3 = list(context.get("candles_3m") or [])
    c15 = list(context.get("candles_15m_closed") or context.get("candles_15m") or [])
    c3_closed = closed_candles(c3, 3, min_required=1)[-5:] if c3 else []
    c15_closed = closed_candles(c15, 15, min_required=1)[-3:] if c15 else []
    through = (lambda c: c.close > level + buffer) if side == "LONG" else (lambda c: c.close < level - buffer)
    touches = (lambda c: c.high >= level - buffer) if side == "LONG" else (lambda c: c.low <= level + buffer)
    close_through_count = sum(1 for c in c3_closed if through(c))
    closed15_through = any(through(c) for c in c15_closed)
    touch_count = sum(1 for c in c3_closed if touches(c))
    tested = bool(touch_count or any(touches(c) for c in c15_closed))
    if closed15_through or close_through_count >= 2:
        return {"status": "CLEARED", "reason": "closed acceptance beyond barrier", "distance_atr": distance_atr}

    setup_type = str(setup_type or "").upper()
    breakout_types = {
        "TREND_IGNITION_ENTRY", "TREND_CONTINUATION", "NEWS_IMPULSE",
        "RANGE_COMPRESSION_BREAKOUT", "PRIME_ICT_LOCATION_OVERRIDE",
        "PULLBACK_CONTINUATION_FAST_ENTRY", "PULLBACK_CONTINUATION",
    }
    structure = context.get("structure") or {}
    phase = str(structure.get("phase") or "").upper()
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    rescue = context.get("entry_rescue_event") or {}
    rescue_type = str(rescue.get("type") or "")
    rescue_breakout = bool(
        rescue.get("confirmed") and rescue.get("side") == side
        and rescue_type in ["BREAKOUT_RETEST", "BREAKOUT_ACCEPTANCE", "ICT_ZONE_RECLAIM"]
    )
    phase_support = bool(
        structure.get("bias") == side
        or (side == "LONG" and any(x in phase for x in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"]))
        or (side == "SHORT" and any(x in phase for x in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]))
    )
    tf3_support = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22)
    tf15_support = bool(tf15.get("bias") == side and abs(int(tf15.get("score", 0) or 0)) >= 26)
    cvd_block = context.get("cvd") or {}
    clusters_block = context.get("clusters") or {}
    cvd_state_support = bool(
        (side == "LONG" and str(cvd_block.get("state") or "").upper() == "BUYERS_DOMINATE")
        or (side == "SHORT" and str(cvd_block.get("state") or "").upper() == "SELLERS_DOMINATE")
    )
    flow_support = sum([
        (cvd_block.get("bias") == side and abs(int(cvd_block.get("score", 0) or 0)) >= 12)
        or (cvd_state_support and abs(int(cvd_block.get("score", 0) or 0)) >= 10),
        (context.get("flow") or {}).get("bias") == side and abs(int((context.get("flow") or {}).get("score", 0) or 0)) >= 10,
        (context.get("liquidity") or {}).get("bias") == side and abs(int((context.get("liquidity") or {}).get("score", 0) or 0)) >= 9,
        clusters_block.get("bias") == side and abs(int(clusters_block.get("score", 0) or 0)) >= 4,
    ])
    near_gate = bool(distance_atr <= BREAKOUT_GATE_MAX_ATR15 and distance_pct <= BREAKOUT_GATE_MAX_DISTANCE_PCT)
    major_1h = "1h" in basis or (priority >= 95 and "external" in basis)
    opposing_ict = bool(
        (side == "LONG" and any(x in basis for x in ["bearish ob", "bearish fvg"]))
        or (side == "SHORT" and any(x in basis for x in ["bullish ob", "bullish fvg"]))
    )
    breakout_location_ok = bool(
        snapshot.get("professional_location")
        or (setup_type in {"TREND_IGNITION_ENTRY", "NEWS_IMPULSE", "RANGE_COMPRESSION_BREAKOUT"}
            and tf15_support and flow_support >= 2)
    )
    directional_stack = bool(
        snapshot.get("fast_trigger") and breakout_location_ok
        and tf3_support and tf15_support and (phase_support or rescue_breakout or flow_support >= 2)
        and not snapshot.get("emergency_against")
    )
    rejection_guard = post_impulse_rejection_snapshot(side, context)
    gate = bool(
        setup_type in breakout_types and near_gate and tested and directional_stack
        and not rejection_guard.get("block_entry")
    )
    absorption_stack = bool(
        touch_count >= 2
        and abs(int(tf3.get("score", 0) or 0)) >= 42
        and abs(int(tf15.get("score", 0) or 0)) >= 45
        and flow_support >= 3
    )
    # An opposing ICT zone still needs an actual breakout/retest. A major 1H
    # liquidity level may be treated as a risky breakout gate only after clear
    # repeated absorption plus strong 3M/15M/order-flow participation.
    if opposing_ict and not rescue_breakout:
        gate = False
    if major_1h and not (rescue_breakout or absorption_stack):
        gate = False
    if gate:
        return {
            "status": "BREAKOUT_GATE",
            "reason": "near technical gate with 15M/3M acceptance stack",
            "distance_atr": round(distance_atr, 3),
            "distance_pct": round(distance_pct, 3),
        }
    return {"status": "HARD", "reason": "unbroken strong barrier", "distance_atr": distance_atr}


def _blocking_barriers_between(side, price, selected_level, targets, context, atr15, setup_type, snapshot=None, exclude_level=None):
    raw = _strong_barriers_between(side, price, selected_level, targets, exclude_level=exclude_level)
    blocking = []
    gates = []
    cleared = []
    for item in raw:
        state = _barrier_clearance_state(side, context, item, price, atr15, setup_type, snapshot)
        enriched = dict(item)
        enriched["barrier_state"] = state
        if state.get("status") == "HARD":
            blocking.append(enriched)
        elif state.get("status") == "BREAKOUT_GATE":
            gates.append(enriched)
        else:
            cleared.append(enriched)
    return blocking, gates, cleared


def _target_barrier_penalty(side, price, selected, targets):
    """Weak levels are checkpoints; strong 15M/1H barriers are hard limits."""
    level = selected["level"]
    penalty = 0.0
    strong = _strong_barriers_between(side, price, level, targets, exclude_level=selected.get("level"))
    for item in targets or []:
        item_level = safe_float(item.get("level"))
        if item_level is None or abs(item_level - level) <= max(abs(price) * 0.00015, 1e-8):
            continue
        between = price < item_level < level if side == "LONG" else level < item_level < price
        if not between:
            continue
        priority = int(item.get("priority", 0) or 0)
        if priority >= 86:
            penalty += 6.0
        elif priority >= 76:
            penalty += 2.0
    return penalty, len(strong)


def _three_minute_stop_allowed(side, context, setup_type, snapshot):
    """Reserve 3M tactical invalidation for genuine sweep/reclaim families only."""
    setup_type = str(setup_type or "").upper()
    if (context or {}).get("force_durable_stop_role"):
        return False
    tactical_types = {"SWEEP_RECLAIM_EARLY_ENTRY", "SWEEP_REVERSAL", "COUNTERTREND_SCALP"}
    if setup_type not in tactical_types:
        return False
    event = (context or {}).get("entry_rescue_event") or {}
    event_ok = bool(
        event.get("confirmed") and event.get("side") == side and event.get("anchor_confirmed")
        and event.get("professional_location") and event.get("type") in {"SWEEP_RECLAIM", "ICT_ZONE_RECLAIM"}
        and int(event.get("score", 0) or 0) >= ENTRY_RESCUE_MIN_SCORE
        and safe_float(event.get("extension_atr15"), 99) <= ENTRY_RESCUE_MAX_EXTENSION_ATR15
    )
    tf3 = (context or {}).get("tf3") or {}
    ict = (context or {}).get("ict") or {}
    structure = (context or {}).get("structure") or {}
    tf3_ok = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 30)
    ict_or_structure = bool(
        (ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 22))
        or (structure.get("bias") == side and abs(int(structure.get("score", 0) or 0)) >= 18)
    )
    return bool(event_ok and tf3_ok and ict_or_structure and not snapshot.get("emergency_against"))




def _select_adaptive_technical_geometry(side, context, atr15, setup_type):
    """Select technical stop/TP geometry without sacrificing a valid opportunity.

    Preferred geometry remains the goal. If a close strong level is actually the
    trigger gate of a confirmed breakout, it is tracked internally and the next
    technical objective may be used. If preferred geometry is unavailable, a
    meaningful VIABLE plan can still be returned and later classified as risky.
    True hard 1H/ICT barriers are never skipped.
    """
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return None
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    rejection_guard = post_impulse_rejection_snapshot(side, context)
    context["post_impulse_rejection"] = rejection_guard
    if rejection_guard.get("block_entry"):
        return None
    stops = _technical_stop_candidates(side, context, atr15)
    targets = _technical_target_candidates(side, context, atr15)
    snapshot = ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    profile = _tp1_travel_profile(context, setup_type, price, atr15)
    allow_3m_stop = _three_minute_stop_allowed(side, context, setup_type, snapshot)

    setup_type = str(setup_type or "").upper()
    no_projection_types = {"RANGE_EDGE_TRADE", "COUNTERTREND_SCALP"}
    expansion_confirmed = bool(
        snapshot.get("closed_confirmed")
        or (snapshot.get("fast_reversal_to_side") or {}).get("confirmed")
        or (tf15.get("bias") == side and structure.get("bias") == side)
        or (
            tf15.get("bias") == side and snapshot.get("fast_trigger")
            and snapshot.get("professional_location")
            and setup_type in {"TREND_IGNITION_ENTRY", "NEWS_IMPULSE", "RANGE_COMPRESSION_BREAKOUT", "PRIME_ICT_LOCATION_OVERRIDE"}
        )
    )
    projection_allowed = bool(expansion_confirmed and setup_type not in no_projection_types)

    filtered_stops = []
    for stop in stops:
        risk = abs(price - float(stop["level"]))
        risk_atr = risk / atr15 if atr15 else 0.0
        is_tactical_3m = stop.get("timeframe") == "3M"
        if is_tactical_3m and not allow_3m_stop:
            continue
        min_stop_atr = MIN_TACTICAL_STOP_ATR_15M if is_tactical_3m else MIN_ROBUST_STOP_ATR_15M
        # Never manufacture a wider stop. A candidate inside normal market noise
        # is discarded and the optimizer must choose another real ICT/15M level.
        if risk_atr + 1e-9 < min_stop_atr:
            continue
        filtered_stops.append(stop)
    stops = filtered_stops
    if not stops:
        return None

    best = None
    for stop in stops:
        risk = abs(price - stop["level"])
        if risk <= 0:
            continue
        risk_atr = risk / atr15
        preferred_reward = max(profile["absolute_floor"], risk * TARGET_RR1_ENTRY)
        viable_reward = max(profile["viable_absolute_floor"], risk * MIN_VIABLE_RR1_ENTRY)

        preferred_visible = [t for t in targets if abs(float(t["level"]) - price) + 1e-9 >= preferred_reward]
        viable_visible = [
            dict(t, viable_fallback=True) for t in targets
            if viable_reward <= abs(float(t["level"]) - price) + 1e-9 < preferred_reward
        ]
        checkpoints = [
            dict(t, checkpoint=True) for t in targets
            if abs(float(t["level"]) - price) + 1e-9 < viable_reward
        ]
        candidate_targets = list(preferred_visible) + list(viable_visible)

        if projection_allowed:
            projected = _measured_move_target(side, context, price, atr15, preferred_reward)
            blocking, gates, _ = _blocking_barriers_between(
                side, price, projected["level"], targets, context, atr15, setup_type, snapshot
            )
            if not blocking:
                projected = dict(projected, breakout_gates=gates)
                candidate_targets.append(projected)

        for target in candidate_targets:
            reward = abs(float(target["level"]) - price)
            if reward <= 0 or reward + 1e-9 < viable_reward:
                continue
            blocking, gates, cleared = _blocking_barriers_between(
                side, price, target["level"], targets, context, atr15, setup_type, snapshot, exclude_level=target.get("level")
            )
            if blocking:
                continue
            rr = reward / risk
            reward_atr = reward / atr15
            reward_pct = reward / price * 100.0
            # RR of 10-20R in a normal 15M intraday plan is usually a geometry
            # distortion: a micro-stop paired with a distant projection. Reject
            # it instead of rewarding the plan as exceptionally strong.
            if rr > ABSOLUTE_RR_SANITY_LIMIT:
                continue
            if rr > EXTREME_RR_SANITY_LIMIT and (risk_atr < 0.85 or target.get("projected")):
                continue
            # RR Sanity Rebuild: the optimizer keeps searching alternative 15M/ICT
            # stops and nearer real targets when RR1 exceeds 5R. A >5R plan is
            # allowed only for a fully confirmed closed-15M continuation with a
            # non-projected target and durable non-3M stop. Rescue/Prime overrides
            # may not manufacture exceptional RR from a micro invalidation.
            if rr > RR_SANITY_REBUILD_LIMIT:
                confirmed_rr_exception = bool(
                    snapshot.get("closed_confirmed")
                    and setup_type in {"TREND_CONTINUATION", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION"}
                    and stop.get("timeframe") in ["15M", "ICT"]
                    and not target.get("projected")
                    and not gates
                    and not (context or {}).get("entry_rescue_event")
                )
                if not confirmed_rr_exception:
                    continue
            preferred_met = bool(
                reward + 1e-9 >= preferred_reward
                and rr + 1e-9 >= TARGET_RR1_ENTRY
                and reward_atr + 1e-9 >= profile["min_atr"]
                and reward_pct + 1e-9 >= profile["min_pct"]
            )
            viable_met = bool(
                rr + 1e-9 >= MIN_VIABLE_RR1_ENTRY
                and reward_atr + 1e-9 >= profile["viable_min_atr"]
                and reward_pct + 1e-9 >= profile["viable_min_pct"]
            )
            if not viable_met:
                continue

            grade = "OPTIMAL" if preferred_met else "VIABLE"
            barrier_mode = "BREAKOUT_GATE" if gates else "DIRECT"
            if target.get("projected") and gates:
                grade = "GATE_EXPANSION" if preferred_met else "VIABLE_GATE_EXPANSION"

            barrier_penalty, _ = _target_barrier_penalty(side, price, target, targets)
            score = stop["priority"] * 0.22 + int(target.get("priority", 0) or 0) * 0.18
            if stop.get("timeframe") == "3M":
                score += 10.0 if allow_3m_stop else -40.0
            elif snapshot.get("closed_confirmed") and stop.get("timeframe") in ["15M", "ICT"]:
                score += 11.0
            score += 28.0 if rr >= 2.0 else (24.0 if rr >= TARGET_RR1_ENTRY else 17.0)
            score += 12.0 if reward_atr >= profile["min_atr"] else 5.0
            score += 12.0 if reward_pct >= profile["min_pct"] else 5.0
            if target.get("projected"):
                score -= 6.0
            if gates:
                score -= 3.0 * len(gates)
            if not preferred_met:
                score -= 8.0
            if rr > RR_SANITY_REBUILD_LIMIT:
                score -= 18.0 + (rr - RR_SANITY_REBUILD_LIMIT) * 5.0
            if 0.32 <= risk_atr <= 1.35:
                score += 10.0
            elif risk_atr < MIN_FULL_POSITION_STOP_ATR_15M:
                score -= 18.0
            elif risk_atr > 1.80:
                score -= 10.0
            score -= min(barrier_penalty, 6.0)

            candidate = {
                "score": score, "stop": stop, "target": target, "risk": risk,
                "reward": reward, "rr": rr, "risk_atr": risk_atr,
                "reward_atr": reward_atr, "reward_pct": reward_pct,
                "stops": stops, "targets": list(targets) + ([target] if target.get("projected") else []),
                "checkpoints": checkpoints, "tp1_profile": profile,
                "preferred_reward": preferred_reward, "viable_reward": viable_reward,
                "allow_3m_stop": allow_3m_stop, "projection_allowed": projection_allowed,
                "geometry_grade": grade, "barrier_mode": barrier_mode,
                "preferred_geometry_met": preferred_met, "breakout_gates": gates,
                "cleared_barriers": cleared,
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    if best is None:
        return _select_alternative_technical_geometry(side, context, atr15, setup_type, stops=stops, targets=targets, snapshot=snapshot)
    return best


def _select_alternative_technical_geometry(side, context, atr15, setup_type, stops=None, targets=None, snapshot=None):
    """Last technical fallback using only real ICT/15M invalidation and liquidity.

    Order is naturally enforced by stop priority: OB/FVG, closed 15M swing,
    breakout/fresh-base extreme. No arbitrary widened stop or projected TP.
    """
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return None
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    stops = list(stops or _technical_stop_candidates(side, context, atr15))
    targets = [t for t in list(targets or _technical_target_candidates(side, context, atr15)) if not t.get("projected")]
    snapshot = snapshot or ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    durable = [x for x in stops if x.get("timeframe") in {"ICT", "15M"} or x.get("kind") in {"FRESH_BASE", "CAPITULATION_BASE", "DISPLACEMENT_BASE"}]
    durable.sort(key=lambda x: (-int(x.get("priority", 0) or 0), abs(float(x.get("level")) - price)))
    best = None
    for stop in durable:
        risk = abs(price - float(stop["level"]))
        if risk <= 0:
            continue
        risk_atr = risk / atr15
        risk_pct = risk / price * 100.0
        if risk_atr < 0.50 or risk_pct > MAX_STOP_DISTANCE_PCT + 1e-9:
            continue
        min_reward = max(risk * ALTERNATIVE_GEOMETRY_MIN_RR1, atr15 * ALTERNATIVE_GEOMETRY_MIN_ATR15, price * ALTERNATIVE_GEOMETRY_MIN_PCT / 100.0)
        for target in targets:
            reward = abs(float(target["level"]) - price)
            if reward + 1e-9 < min_reward:
                continue
            rr = reward / risk
            if rr < ALTERNATIVE_GEOMETRY_MIN_RR1 or rr > RR_SANITY_REBUILD_LIMIT:
                continue
            blocking, gates, cleared = _blocking_barriers_between(side, price, target["level"], targets, context, atr15, setup_type, snapshot, exclude_level=target.get("level"))
            if blocking:
                continue
            score = int(stop.get("priority", 0) or 0) * 0.35 + int(target.get("priority", 0) or 0) * 0.25 + min(rr, 3.0) * 10 - risk_atr * 2
            candidate = {
                "score": score, "stop": stop, "target": target, "risk": risk, "reward": reward,
                "rr": rr, "risk_atr": risk_atr, "reward_atr": reward / atr15,
                "reward_pct": reward / price * 100.0, "stops": durable, "targets": targets,
                "checkpoints": [], "tp1_profile": _tp1_travel_profile(context, setup_type, price, atr15),
                "preferred_reward": min_reward, "viable_reward": min_reward,
                "allow_3m_stop": False, "projection_allowed": False,
                "geometry_grade": "ALTERNATIVE_TECHNICAL", "barrier_mode": "DIRECT",
                "preferred_geometry_met": False, "breakout_gates": gates,
                "cleared_barriers": cleared, "alternative_geometry_path": True,
            }
            if best is None or score > best["score"]:
                best = candidate
    return best



def _geometry_setup_family(setup_type):
    setup_type = str(setup_type or "").upper()
    if setup_type in {"TREND_IGNITION_ENTRY", "TREND_CONTINUATION", "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "FRESH_BASE_CONTINUATION_REENTRY", "RANGE_COMPRESSION_BREAKOUT"}:
        return "CONTINUATION"
    return setup_type


def _continuation_thesis_alignment(context, side):
    """Return whether a missed continuation is still structurally alive."""
    context = context or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    structure = context.get("structure") or {}
    phase = str(structure.get("phase") or "").upper()
    tf15_same = bool(tf15.get("bias") == side and abs(int(tf15.get("score", 0) or 0)) >= 34)
    tf1_same = bool(tf1h.get("bias") == side and abs(int(tf1h.get("score", 0) or 0)) >= 20)
    structure_same = bool(
        structure.get("bias") == side
        and (
            (side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase))
            or (side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase))
            or abs(int(structure.get("score", 0) or 0)) >= 18
        )
    )
    fast = _fast_layer_state(context, side)
    closed = ((context.get("dual_speed_mtf") or {}).get(side) or {}).get("closed_confirmed")
    alive = bool(tf15_same and (tf1_same or structure_same or closed) and fast.get("against", 0) <= 1)
    return {
        "alive": alive, "tf15_same": tf15_same, "tf1_same": tf1_same,
        "structure_same": structure_same, "closed_confirmed": bool(closed),
        "fast_support": int(fast.get("support", 0) or 0),
        "fast_against": int(fast.get("against", 0) or 0),
    }


def _geometry_candidate_snapshot(side, context, atr15=None):
    """Persist real stop/target alternatives even when no final plan exists."""
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return {"stops": [], "targets": []}
    atr15 = safe_float(atr15, (context or {}).get("atr15")) or price * 0.006
    stops = [x for x in _technical_stop_candidates(side, context, atr15) if x.get("timeframe") in {"15M", "ICT"}]
    targets = [x for x in _technical_target_candidates(side, context, atr15) if not x.get("projected")]
    return {
        "stops": [
            {"level": round_price(x.get("level")), "basis": x.get("basis"), "timeframe": x.get("timeframe"), "kind": x.get("kind"), "priority": x.get("priority")}
            for x in stops[:6]
        ],
        "targets": [
            {"level": round_price(x.get("level")), "basis": x.get("basis"), "priority": x.get("priority")}
            for x in targets[:8]
        ],
    }


def _geometry_recovery_thresholds(context):
    memory = (context or {}).get("geometry_persistence_memory") or ((context or {}).get("runtime_state") or {}).get("opportunity_memory") or {}
    failures = int(memory.get("geometry_fail_count", 0) or 0) if isinstance(memory, dict) else 0
    escalated = bool((context or {}).get("force_geometry_recovery") or failures >= GEOMETRY_ESCALATION_FAILURES)
    return {
        "escalated": escalated,
        "failures": failures,
        "min_rr": GEOMETRY_RECOVERY_ESCALATED_MIN_RR1 if escalated else GEOMETRY_RECOVERY_MIN_RR1,
        "min_atr": GEOMETRY_RECOVERY_ESCALATED_MIN_ATR15 if escalated else GEOMETRY_RECOVERY_MIN_ATR15,
        "min_pct": GEOMETRY_RECOVERY_ESCALATED_MIN_PCT if escalated else GEOMETRY_RECOVERY_MIN_PCT,
    }


def _select_geometry_candidate_recovery(side, context, atr15, setup_type):
    """Recover a missed continuation using only real 15M/ICT levels.

    This path is intentionally unavailable to Prime ICT/reversal setups. It is
    a continuation-only repair for the exact case where direction and trigger
    are correct but the normal optimizer cannot pair a durable stop with the
    nearest real liquidity objective.
    """
    setup_type = str(setup_type or "").upper()
    if setup_type not in GEOMETRY_PERSISTENCE_SETUP_TYPES:
        return None
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return None
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    thesis = _continuation_thesis_alignment(context, side)
    if not thesis.get("alive"):
        return None
    late, _ = is_late_chase(side, context)
    memory = (context or {}).get("geometry_persistence_memory") or ((context or {}).get("runtime_state") or {}).get("opportunity_memory") or {}
    origin = safe_float(memory.get("trigger_level"), None) if isinstance(memory, dict) else None
    if origin is None and isinstance(memory, dict):
        origin = safe_float(memory.get("price"), None)
    if origin is not None:
        extension = max(0.0, ((price - origin) if side == "LONG" else (origin - price)) / max(atr15, 1e-9))
        if extension > GEOMETRY_RECOVERY_MAX_EXTENSION_ATR15:
            return None
    elif late and not (context or {}).get("force_geometry_recovery"):
        return None

    thresholds = _geometry_recovery_thresholds(context)
    stops = [x for x in _technical_stop_candidates(side, context, atr15) if x.get("timeframe") in {"15M", "ICT"}]
    targets = [x for x in _technical_target_candidates(side, context, atr15) if not x.get("projected")]

    # Explicit retest/base invalidation from the most recent closed 15M cluster.
    candles = list((context or {}).get("candles_15m_closed") or [])
    if len(candles) >= 4:
        recent = candles[-min(6, len(candles)):]
        buffer15 = max(atr15 * 0.14, price * 0.0007)
        if side == "LONG":
            level = min(c.low for c in recent) - buffer15
        else:
            level = max(c.high for c in recent) + buffer15
        if (side == "LONG" and level < price) or (side == "SHORT" and level > price):
            stops.append({"level": float(level), "basis": "15M lower-high/higher-low retest base invalidation", "priority": 104, "timeframe": "15M", "kind": "RETEST_BASE", "distance": abs(price-level)})

    # Deduplicate and prefer the strongest nearby durable levels.
    uniq = []
    for item in sorted(stops, key=lambda x: (-int(x.get("priority", 0) or 0), abs(float(x.get("level")) - price))):
        if not any(abs(float(x.get("level")) - float(item.get("level"))) <= max(atr15 * 0.08, price * 0.0003) for x in uniq):
            uniq.append(item)
    stops = uniq[:12]
    best = None
    snapshot = ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    for stop in stops:
        risk = abs(price - float(stop["level"]))
        if risk <= 0:
            continue
        risk_atr = risk / atr15
        risk_pct = risk / price * 100.0
        if risk_atr < 0.50 or risk_atr > GEOMETRY_RECOVERY_MAX_STOP_ATR15 or risk_pct > MAX_STOP_DISTANCE_PCT:
            continue
        for target in targets:
            reward = abs(float(target["level"]) - price)
            reward_atr = reward / atr15
            reward_pct = reward / price * 100.0
            rr = reward / risk
            if rr + 1e-9 < thresholds["min_rr"] or rr > RR_SANITY_REBUILD_LIMIT:
                continue
            if reward_atr + 1e-9 < thresholds["min_atr"] or reward_pct + 1e-9 < thresholds["min_pct"]:
                continue
            blocking, gates, cleared = _blocking_barriers_between(
                side, price, target["level"], targets, context, atr15, setup_type, snapshot, exclude_level=target.get("level")
            )
            if blocking:
                continue
            score = (
                int(stop.get("priority", 0) or 0) * 0.35
                + int(target.get("priority", 0) or 0) * 0.25
                + min(rr, 3.0) * 9.0
                - risk_atr * 2.0
                - len(gates) * 2.0
            )
            candidate = {
                "score": score, "stop": stop, "target": target, "risk": risk, "reward": reward,
                "rr": rr, "risk_atr": risk_atr, "reward_atr": reward_atr, "reward_pct": reward_pct,
                "stops": stops, "targets": targets, "checkpoints": [],
                "tp1_profile": _tp1_travel_profile(context, setup_type, price, atr15),
                "preferred_reward": reward, "viable_reward": reward,
                "allow_3m_stop": False, "projection_allowed": False,
                "geometry_grade": "MISSED_CONTINUATION_ESCALATED" if thresholds["escalated"] else "MISSED_CONTINUATION_RECOVERY",
                "barrier_mode": "DIRECT", "preferred_geometry_met": False,
                "breakout_gates": gates, "cleared_barriers": cleared,
                "geometry_candidate_recovery": True,
                "hard_min_rr": thresholds["min_rr"], "hard_min_atr": thresholds["min_atr"], "hard_min_pct": thresholds["min_pct"],
                "geometry_failures": thresholds["failures"],
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def _technical_stop_from_context(side, context, atr15):
    """Compatibility wrapper returning the strongest nearby technical stop."""
    candidates = _technical_stop_candidates(side, context, atr15)
    if not candidates:
        return None, "не знайдено технічного рівня інвалідації"
    # Relevance first, then distance. The full plan uses the geometry optimizer.
    candidates = sorted(candidates, key=lambda x: (-x["priority"], x["distance"]))
    item = candidates[0]
    return float(item["level"]), str(item["basis"])


def _technical_target_candidates(side, context, atr15):
    """Collect real 15M/1H liquidity and ICT objectives in travel order."""
    price = safe_float((context or {}).get("price"))
    if side not in ["LONG", "SHORT"] or not price:
        return []
    atr15 = safe_float(atr15, price * 0.006) or price * 0.006
    structure = (context or {}).get("structure") or {}
    ict = (context or {}).get("ict") or {}
    candles15 = list((context or {}).get("candles_15m_closed") or (context or {}).get("candles_15m") or [])
    candles1h = list((context or {}).get("candles_1h_closed") or [])
    items = []

    if side == "LONG":
        for key, label, priority in [
            ("recent_high", "15M recent high / buy-side liquidity", 92),
            ("swing_high", "15M swing high / buy-side liquidity", 96),
        ]:
            _append_level(items, structure.get(key), label, price, side, priority)
        _append_level(items, ict.get("equilibrium"), "ICT equilibrium", price, side, 72)
        _append_level(items, ict.get("range_high"), "ICT range high / external liquidity", price, side, 98)
        for key, label in [("bear_fvg", "протилежний bearish FVG"), ("bear_ob", "протилежний bearish OB")]:
            low, high = _zone_bounds(ict.get(key))
            _append_level(items, low, label + " (перший край)", price, side, 88)
            _append_level(items, (low + high) / 2 if low is not None else None, label + " (mid)", price, side, 80)
        for level, _ in _recent_swing_liquidity(candles15, "LONG", 10):
            _append_level(items, level, "закритий 15M swing high", price, side, 90)
        for level, _ in _recent_swing_liquidity(candles1h, "LONG", 8):
            _append_level(items, level, "закритий 1H swing high", price, side, 94)
        if candles15:
            _append_level(items, max(c.high for c in candles15[-48:]), "48-bar 15M external high", price, side, 86)
        if candles1h:
            _append_level(items, max(c.high for c in candles1h[-36:]), "36-bar 1H external high", price, side, 95)
    else:
        for key, label, priority in [
            ("recent_low", "15M recent low / sell-side liquidity", 92),
            ("swing_low", "15M swing low / sell-side liquidity", 96),
        ]:
            _append_level(items, structure.get(key), label, price, side, priority)
        _append_level(items, ict.get("equilibrium"), "ICT equilibrium", price, side, 72)
        _append_level(items, ict.get("range_low"), "ICT range low / external liquidity", price, side, 98)
        for key, label in [("bull_fvg", "протилежний bullish FVG"), ("bull_ob", "протилежний bullish OB")]:
            low, high = _zone_bounds(ict.get(key))
            _append_level(items, high, label + " (перший край)", price, side, 88)
            _append_level(items, (low + high) / 2 if low is not None else None, label + " (mid)", price, side, 80)
        for level, _ in _recent_swing_liquidity(candles15, "SHORT", 10):
            _append_level(items, level, "закритий 15M swing low", price, side, 90)
        for level, _ in _recent_swing_liquidity(candles1h, "SHORT", 8):
            _append_level(items, level, "закритий 1H swing low", price, side, 94)
        if candles15:
            _append_level(items, min(c.low for c in candles15[-48:]), "48-bar 15M external low", price, side, 86)
        if candles1h:
            _append_level(items, min(c.low for c in candles1h[-36:]), "36-bar 1H external low", price, side, 95)

    return _dedupe_target_levels(items, side, atr15)


def _required_entry_for_rr(side, stop, target, rr):
    stop = safe_float(stop)
    target = safe_float(target)
    rr = safe_float(rr, TARGET_RR1_ENTRY) or TARGET_RR1_ENTRY
    if side not in ["LONG", "SHORT"] or stop is None or target is None or rr <= 0:
        return 0.0
    # Same algebra for LONG and SHORT: reward = rr * risk.
    return float((target + rr * stop) / (1.0 + rr))


def enforce_smart_money_rr(side, price, stop, tp1, tp2, tp3, atr15=None):
    """Backward-compatible name: validate only, never manufacture farther TP.

    Earlier versions moved TP1/TP2/TP3 to make RR look acceptable. This created
    a conflict with actual liquidity. The professional version preserves every
    technical level exactly; weak RR is handled by WAIT / a better 3M entry.
    """
    return stop, tp1, tp2, tp3


def smart_money_rr_status(plan):
    """Describe RR quality without blocking an otherwise valid technical setup."""
    if not plan:
        return {"ok": False, "rr1": 0, "label": "RR недоступний"}
    if not bool(getattr(plan, "valid", True)):
        return {
            "ok": False,
            "rr1": safe_float(getattr(plan, "rr1", 0), 0) or 0,
            "label": str(getattr(plan, "validation_reason", "") or "технічний план не пройшов перевірку"),
        }
    rr1 = safe_float(getattr(plan, "rr1", 0), 0) or 0
    if rr1 >= STRONG_RR1_BONUS:
        label = f"Smart Money RR сильний: {round(rr1, 2)}R"
    elif rr1 >= TARGET_RR1_ENTRY:
        label = f"Smart Money RR добрий: {round(rr1, 2)}R"
    else:
        # Advisory remains internal so Telegram formatting/messages do not change.
        label = ""
    return {"ok": True, "rr1": rr1, "label": label, "advisory": rr1 < TARGET_RR1_ENTRY}


def analyze_failed_trade_reversal(state, context, max_age_minutes=150):
    """Detect a professional reversal setup after a failed trade.

    This module is intentionally NOT an automatic flip. It activates only after
    a weak/failed close (STOP, EXIT_LOCAL_BREAK, EXIT_MFE_GIVEBACK, etc.) and
    then watches the opposite side for 3M structure + ICT/SMC confirmation.

    Example:
      failed SHORT -> watch LONG only if 3M turns up, there is no new lower low,
      and CVD/flow are not strongly against the reversal.
    """
    if not isinstance(state, dict) or not isinstance(context, dict):
        return {"active": False}

    history = state.get("history") or []
    failed_actions = {
        "STOP", "EXIT", "EXIT_LOCAL_BREAK", "EXIT_STRUCTURE_BREAK",
        "EXIT_WARNING_CONFIRM", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK",
        "EXIT_AFTER_TP1_GIVEBACK", "PROTECT_OR_EXIT",
    }

    now = now_utc()
    last_close = None
    for item in reversed(history[-MAX_HISTORY:]):
        if isinstance(item, dict) and item.get("type") == "TRADE_CLOSED":
            last_close = item
            break
    if not last_close:
        return {"active": False}

    failed_side = last_close.get("side")
    action = str(last_close.get("action") or "")
    result_pct = safe_float(last_close.get("result_pct"), 0.0) or 0.0
    ts = _parse_iso_time(last_close.get("time"))
    age_min = ((now - ts).total_seconds() / 60.0) if ts else 99999.0

    # Reversal mode is for recent failed/weak exits only.
    weak_failed_close = action in failed_actions and (result_pct <= 0.35 or action in {
        "STOP", "EXIT_LOCAL_BREAK", "EXIT_STRUCTURE_BREAK", "EXIT_WARNING_CONFIRM",
        "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_AFTER_TP1_GIVEBACK",
    })
    if failed_side not in ["LONG", "SHORT"] or not weak_failed_close or age_min > max_age_minutes:
        return {"active": False}

    side = opposite(failed_side)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    clusters = context.get("clusters") or {}
    candles_3m = context.get("candles_3m") or []

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()

    bos_reclaim = (
        (side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase))
        or (side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase))
    )
    ict_reclaim = (
        (side == "LONG" and ict_setup in ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "BOS_LONG_CONTINUATION_HOLD"])
        or (side == "SHORT" and ict_setup in ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "BOS_SHORT_CONTINUATION_HOLD"])
    )

    # 3M micro reversal shape: higher low/no lower low after failed SHORT,
    # lower high/no higher high after failed LONG.
    micro_reversal = False
    micro_reason = ""
    try:
        recent = candles_3m[-10:]
        if len(recent) >= 6:
            first = recent[:len(recent)//2]
            last = recent[len(recent)//2:]
            if side == "LONG":
                micro_reversal = min(c.low for c in last) >= min(c.low for c in first) and recent[-1].close > mean([c.close for c in recent[-4:]])
                micro_reason = "3M робить higher low після зламаного SHORT"
            else:
                micro_reversal = max(c.high for c in last) <= max(c.high for c in first) and recent[-1].close < mean([c.close for c in recent[-4:]])
                micro_reason = "3M робить lower high після зламаного LONG"
    except Exception:
        micro_reversal = False

    score = 0
    confirmations = []
    conflicts = []

    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22:
        score += 24
        confirmations.append("3M розвернувся після невдалої угоди")
    elif tf3.get("bias") == side:
        score += 14
        confirmations.append("3M починає розвертатись")
    elif tf3.get("bias") == failed_side and abs(int(tf3.get("score", 0) or 0)) >= 32:
        score -= 20
        conflicts.append("3M ще не зламав попередній напрям")

    if tf15.get("bias") == side:
        score += 18
        confirmations.append("15M вже підтримує розворот")
    elif tf15.get("bias") == "NEUTRAL":
        score += 6
        confirmations.append("15M нейтральний — розворот можливий, але потрібен 3M/BOS")
    elif tf15.get("bias") == failed_side:
        score -= 14
        conflicts.append("15M ще тримає старий напрям")

    if bos_reclaim:
        score += 24
        confirmations.append("SMC/BOS підтверджує розворот")
    elif structure.get("bias") == side:
        score += 14
        confirmations.append("структура вже за новий напрям")
    elif structure.get("bias") == failed_side:
        score -= 12
        conflicts.append("структура ще проти розвороту")

    if ict_reclaim or (ict.get("bias") == side and bool(ict.get("entry_ok"))):
        score += 18
        confirmations.append("ICT дає sweep/reclaim або continuation для розвороту")
    elif ict.get("bias") == side:
        score += 8
        confirmations.append("ICT підтримує ідею розвороту")
    elif ict.get("bias") == failed_side and ict.get("state") == "ENTRY_MODEL":
        score -= 16
        conflicts.append("ICT ще підтримує старий напрям")

    if micro_reversal:
        score += 10
        confirmations.append(micro_reason)

    # Flow/CVD must not be strongly against the reversal. They are not required
    # to be perfect, because reversal often starts before all flow confirms.
    cvd_against = cvd.get("bias") == failed_side and abs(int(cvd.get("score", 0) or 0)) >= 22
    flow_against = flow.get("bias") == failed_side and abs(int(flow.get("score", 0) or 0)) >= 14
    if cvd.get("bias") == side:
        score += 10
        confirmations.append("CVD підтримує розворот")
    elif cvd_against:
        score -= 12
        conflicts.append("CVD ще проти розвороту")
    else:
        score += 3

    if flow.get("bias") == side:
        score += 8
        confirmations.append("потік підтримує розворот")
    elif flow_against:
        score -= 10
        conflicts.append("потік ще проти розвороту")
    else:
        score += 2

    if clusters.get("bias") == side:
        score += 4

    score = int(max(0, min(100, score)))
    allow_watch = score >= 52 and (tf3.get("bias") == side or bos_reclaim or ict_reclaim or micro_reversal)
    allow_entry = score >= 68 and (bos_reclaim or ict_reclaim or tf15.get("bias") == side) and not (cvd_against and flow_against)

    return {
        "active": bool(allow_watch),
        "side": side,
        "failed_side": failed_side,
        "failed_action": action,
        "failed_result_pct": round(result_pct, 3),
        "age_minutes": round(age_min, 1),
        "score": score,
        "allow_entry": bool(allow_entry),
        "reason": f"попередній {failed_side} зламано ({action}, {round(result_pct, 3)}%); шукати {side} тільки після 3M + BOS/reclaim",
        "confirmations": confirmations[:5],
        "conflicts": conflicts[:5],
    }


def build_context(data, state=None):
    ticker = data.get("ticker") or {}
    price = safe_float(ticker.get("price"))
    context_change24h = safe_float(ticker.get("change24h"), 0)

    candles_3m_all = data.get("candles_3m") or []
    candles_15m_all = data.get("candles_15m") or []
    candles_1h_all = data.get("candles_1h") or []
    candles_4h_all = data.get("candles_4h") or []

    # Closed-Candle MTF Guard:
    # 3M remains the live timing trigger; 15M/1H/4H trend, structure and regime
    # are calculated from completed candles only.
    candles_15m_closed = closed_candles(candles_15m_all, 15, min_required=30)
    candles_1h_closed = closed_candles(candles_1h_all, 60, min_required=30)
    candles_4h_closed = closed_candles(candles_4h_all, 240, min_required=30)
    mtf_guard = build_closed_candle_mtf_guard(data, candles_15m_closed, candles_1h_closed, candles_4h_closed)

    tf3 = analyze_micro(candles_3m_all)
    tf15 = analyze_timeframe(candles_15m_closed, "15m")
    tf1h = analyze_timeframe(candles_1h_closed, "1h")
    tf4h = analyze_timeframe(candles_4h_closed, "4h")
    structure = analyze_structure(candles_15m_closed)
    ict = analyze_ict_model(candles_3m_all, candles_15m_closed, structure, price)
    volume_guard = analyze_volume_guard(candles_15m_closed)
    previous_snapshot = (state or {}).get("last_market_snapshot") or {}
    flow = analyze_flow(data.get("trades") or [], data.get("book") or {}, price)
    cvd = analyze_cvd(data.get("trades") or [], candles_3m_all, price, previous_snapshot)
    clusters = analyze_clusters(data.get("trades") or [], data.get("book") or {}, price)
    derivatives = analyze_derivatives(data.get("open_interest") or {}, data.get("funding") or {}, price, previous_snapshot)
    liquidity = analyze_liquidations(candles_3m_all, candles_15m_closed, flow, structure, price)
    news_items = get_news()
    news = analyze_news(
        news_items, candles_3m=candles_3m_all, candles_15m=candles_15m_closed,
        current_price=price, state=state,
    )
    calendar = analyze_calendar_alerts()
    session = market_session()

    price = price or safe_float(tf15.get("close"))
    price_warning = bool(ticker.get("price_warning"))
    low_liquidity_risk = bool(session.get("liquidity_risk")) and safe_float(volume_guard.get("ratio"), 1.0) <= LOW_LIQUIDITY_VOLUME_RATIO

    atr15 = safe_float(structure.get("atr")) or safe_float(tf15.get("atr")) or (price or 90) * 0.006

    # Intraday architecture for trades lasting a few hours:
    # 15m/structure/3m are the engine. CVD and OI confirm quality.
    # 4H is background, but a strong opposite 4H/1H trend must reduce ICT-only
    # entries. This prevents the bot from printing market ENTRY when only ICT is
    # bullish/bearish while 4H is strongly against and 15M/3M are still neutral.
    ict_score_raw = safe_float(ict.get("score"), 0) or 0
    ict_score_used = ict_score_raw
    ict_bias = ict.get("bias", "NEUTRAL")
    if ict_bias == "LONG":
        if (tf4h.get("score", 0) or 0) <= -35 and tf15.get("bias") != "LONG":
            ict_score_used *= 0.55
        if (tf1h.get("score", 0) or 0) <= -15 and tf15.get("bias") != "LONG":
            ict_score_used *= 0.75
        if tf15.get("bias") != "LONG" and tf3.get("bias") != "LONG" and structure.get("bias") != "LONG":
            ict_score_used *= 0.70
    elif ict_bias == "SHORT":
        if (tf4h.get("score", 0) or 0) >= 35 and tf15.get("bias") != "SHORT":
            ict_score_used *= 0.55
        if (tf1h.get("score", 0) or 0) >= 15 and tf15.get("bias") != "SHORT":
            ict_score_used *= 0.75
        if tf15.get("bias") != "SHORT" and tf3.get("bias") != "SHORT" and structure.get("bias") != "SHORT":
            ict_score_used *= 0.70

    # BTC↔BZ filter is intentionally removed.
    # Quality/market bias is recalculated from BZU-native data only:
    # ICT/SMC + 15M/3M/1H + CVD/Flow/OI/Liquidity/Clusters.
    tech_score = (
        tf15.get("score", 0) * 1.50
        + structure.get("score", 0) * 1.28
        + ict_score_used * 1.38
        + tf3.get("score", 0) * 0.52
        + tf1h.get("score", 0) * 0.48
        + cvd.get("score", 0) * 0.78
        + derivatives.get("score", 0) * 0.65
        + liquidity.get("score", 0) * 0.72
        + flow.get("score", 0) * 0.52
        + clusters.get("score", 0) * 0.18
        + tf4h.get("score", 0) * 0.10
        + volume_guard.get("score", 0)
    )
    total_score = tech_score + news.get("score", 0) * 0.22 + calendar.get("score", 0) + session.get("score", 0)
    if low_liquidity_risk:
        total_score -= 10 if tech_score > 0 else -10
    if price_warning:
        total_score *= 0.92

    if total_score >= 42:
        bias = "LONG"
    elif total_score <= -42:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    pending_ict_zones = build_pending_ict_zones({
        "price": price,
        "bias": bias,
        "atr15": atr15,
        "ict": ict,
    })
    temp_context_for_regime = {
        "price": price,
        "atr15": atr15,
        "change24h": context_change24h,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "structure": structure,
        "ict": ict,
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
        "news": news,
        "calendar": calendar,
        "liquidity": liquidity,
        "mtf_guard": mtf_guard,
        "candles_3m": candles_3m_all,
        "candles_15m": candles_15m_all,
        "candles_15m_closed": candles_15m_closed,
        "candles_1h_closed": candles_1h_closed,
        "candles_4h_closed": candles_4h_closed,
        "total_score": int(total_score),
        "bias": bias,
    }
    market_regime = detect_market_regime(temp_context_for_regime, bias if bias in ["LONG", "SHORT"] else None)
    market_regime = stabilize_regime_engine(state, market_regime)
    temp_context_for_regime["market_regime"] = market_regime
    temp_context_for_regime["regime_engine"] = market_regime
    dual_speed_mtf = build_dual_speed_mtf(temp_context_for_regime)
    temp_context_for_regime["dual_speed_mtf"] = dual_speed_mtf
    setup_classifier = classify_setup(temp_context_for_regime, bias if bias in ["LONG", "SHORT"] else None)
    temp_context_for_regime["setup_classifier"] = setup_classifier
    temp_context_for_regime["runtime_state"] = state
    reentry_review = analyze_reentry_quality_review(state, temp_context_for_regime)

    # Reversal engine after a failed/weak trade. It is attached to context so
    # setup evaluation can show REVERSAL WATCH or allow a confirmed opposite entry.
    temp_context_for_reversal = dict(temp_context_for_regime)
    temp_context_for_reversal.update({
        "candles_3m": candles_3m_all,
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
    })
    reversal_after_failed_trade = analyze_failed_trade_reversal(state, temp_context_for_reversal)
    reset_gate_side = ((state or {}).get("structural_reset_gate") or {}).get("side") if isinstance(state, dict) else None
    structural_reset_snapshot = structural_reset_gate_snapshot(
        state, temp_context_for_regime, reset_gate_side if reset_gate_side in ["LONG", "SHORT"] else (bias if bias in ["LONG", "SHORT"] else None)
    )

    return {
        "price": price,
        "change24h": safe_float(ticker.get("change24h"), 0),
        "source": ticker.get("source", "unknown"),
        "symbol": ticker.get("symbol", OKX_INST_ID),
        "atr15": atr15,
        "candles_3m": candles_3m_all,
        "candles_15m": candles_15m_all,
        "candles_15m_closed": candles_15m_closed,
        "candles_1h_closed": candles_1h_closed,
        "candles_4h_closed": candles_4h_closed,
        "mtf_guard": mtf_guard,
        "dual_speed_mtf": dual_speed_mtf,
        "tf3": tf3,
        "tf15": tf15,
        "tf1h": tf1h,
        "tf4h": tf4h,
        "structure": structure,
        "ict": ict,
        "ict_score_raw": int(ict_score_raw),
        "ict_score_used": int(ict_score_used),
        "pending_ict_zones": pending_ict_zones,
        "market_regime": market_regime,
        "regime_engine": market_regime,
        "setup_classifier": setup_classifier,
        "reentry_review": reentry_review,
        "reentry_cooldown": reentry_review,  # compatibility alias
        "reversal_after_failed_trade": reversal_after_failed_trade,
        "pending_trigger_memory": (state or {}).get("pending_trigger") if isinstance(state, dict) else None,
        "opportunity_memory": (state or {}).get("opportunity_memory") if isinstance(state, dict) else None,
        "structural_reset_gate_snapshot": structural_reset_snapshot,
        "runtime_state": state,
        "volume_guard": volume_guard,
        "flow": flow,
        "cvd": cvd,
        "clusters": clusters,
        "derivatives": derivatives,
        "liquidity": liquidity,
        "news": news,
        "calendar": calendar,
        "session": session,
        "low_liquidity_risk": low_liquidity_risk,
        "price_warning": price_warning,
        "cross_price_diff_pct": ticker.get("cross_price_diff_pct"),
        "tech_score": int(tech_score),
        "total_score": int(total_score),
        "bias": bias,
    }

def side_score(value, side):
    if side == "LONG":
        return value
    if side == "SHORT":
        return -value
    return 0


def detect_ict_balance_market(context, side=None):
    """ICT balance / midrange filter.

    ICT idea used here:
    - the middle of the dealing range around equilibrium is low edge;
    - in balance we do NOT open market entries from the middle;
    - valid trades come from sweep/reclaim at range edges or BOS + FVG/OB retest.

    Important: this must not confuse a normal pullback with a range.
    A pullback is allowed when price is in discount for LONG / premium for SHORT
    and structure/HTF direction still supports continuation.
    """
    if not isinstance(context, dict):
        return False, ""

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}

    if not price or not atr15:
        return False, ""

    phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    pd = str(ict.get("premium_discount") or "").upper()
    pd_pos = safe_float(ict.get("pd_pos"), 0.5)
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    has_bos_or_sweep = any(x in phase for x in ["BOS LONG", "BOS SHORT", "UPSIDE SWEEP", "DOWNSIDE SWEEP", "CHOCH LONG", "CHOCH SHORT"])
    has_ict_entry_model = ict.get("state") == "ENTRY_MODEL" and bool(ict.get("entry_ok"))
    near_equilibrium = pd == "MIDRANGE" or 0.43 <= pd_pos <= 0.57 or ict_setup == "BALANCE_MIDRANGE"

    tf15_neutral_or_range = tf15.get("bias") == "NEUTRAL" or tf15.get("trend") == "RANGE" or abs(int(tf15.get("score", 0) or 0)) < 26
    structure_neutral = structure.get("bias") == "NEUTRAL" or phase in ["RANGE / WAIT", "NO DATA", ""]
    tf3_choppy = tf3.get("bias") == "NEUTRAL" or abs(int(tf3.get("score", 0) or 0)) < 32

    # Pullback exception: do not call it balance if the market has a clear HTF/structure side
    # and price is pulling back into the correct ICT side of the range.
    if side in ["LONG", "SHORT"]:
        htf_support = (tf1h.get("bias") == side or side_score(int(tf1h.get("score", 0) or 0), side) >= 18)
        structure_support = structure.get("bias") == side or (side == "LONG" and "BOS LONG" in phase) or (side == "SHORT" and "BOS SHORT" in phase)
        correct_side_of_range = (side == "LONG" and pd == "DISCOUNT") or (side == "SHORT" and pd == "PREMIUM")
        has_retest_zone = bool(ict.get("bull_fvg") or ict.get("bull_ob")) if side == "LONG" else bool(ict.get("bear_fvg") or ict.get("bear_ob"))
        if correct_side_of_range and (structure_support or htf_support) and (has_retest_zone or has_bos_or_sweep):
            return False, ""

    balance_score = 0
    if near_equilibrium:
        balance_score += 2
    if tf15_neutral_or_range:
        balance_score += 1
    if structure_neutral:
        balance_score += 1
    if tf3_choppy:
        balance_score += 1
    if distance_atr <= 0.95:
        balance_score += 1
    if has_bos_or_sweep or has_ict_entry_model:
        balance_score -= 2

    if balance_score >= 4:
        return True, "ICT: ціна біля equilibrium / середина діапазону — входу немає; чекати sweep high/low або BOS + FVG/OB retest"
    return False, ""



def detect_exhausted_move(side, context):
    """Detect when a directional move is already mostly played out.

    This does NOT create an opposite entry. It only prevents the bot from
    opening a fresh LONG after a vertical daily pump, or a fresh SHORT after a
    vertical daily dump. The correct state is WAIT / new structure, not chase.
    """
    if side not in ["LONG", "SHORT"] or not isinstance(context, dict):
        return False, ""

    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}
    candles_15m = context.get("candles_15m") or []

    if not price or not atr15:
        return False, ""

    ema20 = safe_float(tf15.get("ema20"))
    rsi15 = safe_float(tf15.get("rsi"), 50)
    rsi1h = safe_float(tf1h.get("rsi"), 50)
    change24h = safe_float(context.get("change24h"), 0)
    move_8 = safe_float(tf15.get("move_8_pct"), 0)
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    recent = candles_15m[-48:] if candles_15m else []
    recent_low = min((c.low for c in recent), default=None)
    recent_high = max((c.high for c in recent), default=None)
    move_from_low = pct(price, recent_low) if recent_low else 0
    move_from_high = pct(price, recent_high) if recent_high else 0

    cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 10
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10
    cluster_against = clusters.get("bias") == opposite(side) or side_score(int(clusters.get("score", 0) or 0), side) <= -4
    liquidity_against = side in (liquidity.get("blocks") or []) or liquidity.get("bias") == opposite(side)
    shorts_closing_or_longs_closing = str(derivatives.get("state", "")).upper() in ["SHORTS_CLOSING", "LONGS_CLOSING"]
    pressure_against_count = sum([bool(cvd_against), bool(flow_against), bool(cluster_against), bool(liquidity_against), bool(shorts_closing_or_longs_closing)])

    if side == "LONG":
        big_daily = change24h >= 3.0 or move_from_low >= 3.2 or move_8 >= 3.0
        stretched = distance_atr >= 1.45 or rsi15 >= 72 or rsi1h >= 74
        near_top = bool(recent_high and (recent_high - price) / price * 100 <= 0.75)
        tired_price_action = structure.get("phase") == "UPSIDE SWEEP" or tf3.get("bias") in ["SHORT", "NEUTRAL"]
        if big_daily and stretched and (near_top or pressure_against_count >= 1 or tired_price_action):
            reasons = []
            if change24h >= 3.0:
                reasons.append(f"за добу вже +{round(change24h, 2)}%")
            elif move_from_low >= 3.2:
                reasons.append(f"від локального low вже +{round(move_from_low, 2)}%")
            if rsi15 >= 72:
                reasons.append(f"15M RSI {round(rsi15, 1)}")
            if distance_atr >= 1.45:
                reasons.append(f"ціна далеко від EMA20 ({round(distance_atr, 2)} ATR)")
            if pressure_against_count >= 1:
                reasons.append("CVD/потік/кластери вже попереджають")
            return True, "LONG вже відпрацював основний імпульс — не доганяти; чекати нову базу/відкат або окремий SHORT-сетап. " + "; ".join(reasons[:4])

    else:
        big_daily = change24h <= -3.0 or move_from_high <= -3.2 or move_8 <= -3.0
        stretched = distance_atr >= 2.0 or rsi15 <= 28 or rsi1h <= 26
        near_bottom = bool(recent_low and (price - recent_low) / price * 100 <= 0.75)
        tired_price_action = structure.get("phase") == "DOWNSIDE SWEEP" or tf3.get("bias") in ["LONG", "NEUTRAL"]
        if big_daily and stretched and (near_bottom or pressure_against_count >= 1 or tired_price_action):
            reasons = []
            if change24h <= -3.0:
                reasons.append(f"за добу вже {round(change24h, 2)}%")
            elif move_from_high <= -3.2:
                reasons.append(f"від локального high вже {round(move_from_high, 2)}%")
            if rsi15 <= 28:
                reasons.append(f"15M RSI {round(rsi15, 1)}")
            if distance_atr >= 1.45:
                reasons.append(f"ціна далеко від EMA20 ({round(distance_atr, 2)} ATR)")
            if pressure_against_count >= 1:
                reasons.append("CVD/потік/кластери вже попереджають")
            return True, "SHORT вже відпрацював основний імпульс — не доганяти; чекати нову базу/відкат або окремий LONG-сетап. " + "; ".join(reasons[:4])

    return False, ""

def is_late_chase(side, context):
    """Anti-chase guard for intraday entries.

    This is intentionally NOT a full Retest Engine.
    It only blocks fresh market entries when the move has already gone vertical
    and the risk/reward is likely worse. Direction may still be correct, but
    the bot should wait for a pullback or a short consolidation instead of
    chasing the candle.
    """
    price = context["price"]
    atr15 = context["atr15"]
    tf15 = context["tf15"]
    tf3 = context["tf3"]
    structure = context["structure"]
    liquidity = context.get("liquidity") or {}
    ict = context.get("ict") or {}
    ema20 = safe_float(tf15.get("ema20"))
    rsi15 = safe_float(tf15.get("rsi"), 50)
    move_8 = safe_float(tf15.get("move_8_pct"), 0)
    fast_move_3m = safe_float(tf3.get("fast_move_pct"), 0)
    drift_3m = safe_float(tf3.get("drift_pct"), 0)
    score_3m = int(tf3.get("score", 0) or 0)

    if not price or not atr15:
        return False, ""

    if ict.get("no_chase") and ict.get("bias") == side:
        return True, "ICT: імпульс вже відбувся — не доганяти; чекати FVG/OB або нову проторговку"

    distance_atr = abs(price - ema20) / atr15 if ema20 else 0

    # Strong 3m impulse already happened. For a 15m intraday strategy this is
    # the classic place where market entries become late/chasing.
    long_vertical_3m = fast_move_3m >= 0.62 and score_3m >= 34
    short_vertical_3m = fast_move_3m <= -0.62 and score_3m <= -34

    # Price is stretched away from 15m EMA20 while 3m is already impulsive.
    long_stretched = distance_atr >= 1.25 and fast_move_3m >= 0.42
    short_stretched = distance_atr >= 1.25 and fast_move_3m <= -0.42

    # 15m move is already mature. This is not a hard reversal signal, just
    # a warning not to enter after the candle has done the easy part.
    mature_15m_long = rsi15 >= 72 and move_8 >= 0.85
    mature_15m_short = rsi15 <= 28 and move_8 <= -0.85

    if side == "LONG":
        if "LONG" in liquidity.get("blocks", []):
            return True, "ліквідність проти: high зняли/памп поглинули, лонг не доганяти"
        if long_vertical_3m:
            return True, "3M вже дав різкий імпульс вгору — лонг не доганяти; чекати проторговку або відкат"
        if long_stretched:
            return True, "ціна розтягнута від EMA20 після 3M-імпульсу — лонг тільки після охолодження"
        if mature_15m_long:
            return True, "лонг після сильного 15M імпульсу: чекати відкат/проторговку"
        if distance_atr >= 1.75 and tf3.get("state") != "LONG_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3M не підсилює"
        if structure.get("phase") == "UPSIDE SWEEP":
            return True, "зверху зняли ліквідність, лонг не доганяти"
    else:
        if "SHORT" in liquidity.get("blocks", []):
            return True, "ліквідність проти: low зняли/дамп викупили, шорт не доганяти"
        if short_vertical_3m:
            return True, "3M вже дав різкий імпульс вниз — шорт не доганяти; чекати проторговку або відкат"
        if short_stretched:
            return True, "ціна розтягнута від EMA20 після 3M-імпульсу — шорт тільки після охолодження"
        if mature_15m_short:
            return True, "шорт після сильного 15M імпульсу: чекати відкат/проторговку"
        if distance_atr >= 1.75 and tf3.get("state") != "SHORT_STRENGTHENING":
            return True, "ціна далеко від EMA20, 3M не підсилює"
        if structure.get("phase") == "DOWNSIDE SWEEP":
            return True, "знизу зняли ліквідність, шорт не доганяти"
    return False, ""


def soft_late_entry_penalty(side, context, late_reason=""):
    """Soft anti-chase penalty.

    It lowers quality after an extended 3M/15M impulse, but it does not hard
    block the entry. Strong ICT + structure can still open an early/reversal
    trade, just with a more honest quality score.
    """
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or (price or 90) * 0.006
    tf15 = context.get("tf15") or {}
    tf3 = context.get("tf3") or {}
    ict = context.get("ict") or {}
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if price and ema20 and atr15 else 0
    fast = abs(safe_float(tf3.get("fast_move_pct"), 0) or 0)
    move8 = abs(safe_float(tf15.get("move_8_pct"), 0) or 0)

    penalty = 0
    if distance_atr >= 1.5:
        penalty += 7
    elif distance_atr >= 1.0:
        penalty += 5
    elif distance_atr >= 0.55:
        penalty += 3

    if fast >= 0.75:
        penalty += 6
    elif fast >= 0.45:
        penalty += 4

    if move8 >= 1.2:
        penalty += 5
    elif move8 >= 0.8:
        penalty += 3

    if ict.get("no_chase") and ict.get("bias") == side:
        penalty += 5

    # A real ICT entry model can justify entering an impulse continuation/reversal,
    # so reduce but never remove the anti-chase discount.
    strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    setup = str(ict.get("setup", "") or "").upper()
    if ict.get("bias") == side and ict.get("entry_ok") and setup in strong_setups.get(side, []):
        penalty = max(3, int(penalty * 0.55))

    return int(max(0, min(18, penalty)))


def analyze_entry_location_score(side, context):
    """Internal Entry Location Score (ELS), hidden from Telegram.

    Purpose: separate a correct direction from a good entry location.
    The score is deliberately SOFT: it does not block trades and it must not
    kill early 3M/ICT entries. It only adjusts quality and supervision risk.

    High score = entry is close to a professional location: ICT zone/reclaim,
    fresh BOS, not too stretched from 15M EMA20/ATR, no obvious liquidity block.
    Low score = direction may still be correct, but the market entry is late or
    far from the smart-money area.
    """
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15")) or (price or 90) * 0.006
    tf15 = context.get("tf15") or {}
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    liquidity = context.get("liquidity") or {}

    opp = opposite(side)
    score = 62.0
    factors = []

    def add(points, key):
        nonlocal score
        score += points
        factors.append((key, points))

    if not price or not atr15:
        return {"score": 62, "adjustment": 0, "state": "NORMAL", "penalty": 0, "bonus": 0, "factors": factors}

    # 1) Stretch from 15M mean. This catches late entries without blocking them.
    ema20 = safe_float(tf15.get("ema20"))
    distance_atr = abs(price - ema20) / atr15 if ema20 else 0.0
    if distance_atr <= 0.35:
        add(8, "near_ema20")
    elif distance_atr <= 0.75:
        add(4, "fair_distance")
    elif distance_atr >= 1.75:
        add(-10, "very_stretched")
    elif distance_atr >= 1.20:
        add(-7, "stretched")
    elif distance_atr >= 0.95:
        add(-3, "mild_stretch")

    # 2) ICT entry model. This is the best location anchor.
    setup = str(ict.get("setup", "") or "").upper()
    strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    ict_same = ict.get("bias") == side
    ict_against = ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16
    ict_strong = bool(ict_same and ict.get("entry_ok") and setup in strong_setups.get(side, []))
    ict_weak = bool(ict_same and setup in weak_setups.get(side, []))
    if ict_strong:
        add(16, "strong_ict_location")
    elif ict_weak:
        add(7, "weak_ict_location")
    elif ict_same:
        add(2, "ict_context")
    elif ict_against:
        add(-12, "ict_against_location")

    if ict.get("no_chase") and ict_same:
        add(-8, "ict_no_chase")

    # 3) Freshness of BOS / reclaim. Entering close to a fresh broken level is good;
    # chasing far from it is not. This is still soft and cannot block a trade.
    phase = str(structure.get("phase", "") or "").upper()
    swing_high = safe_float(structure.get("swing_high"))
    swing_low = safe_float(structure.get("swing_low"))
    if side == "LONG" and phase in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"]:
        ref = swing_high or swing_low
        if ref:
            d = abs(price - ref) / atr15
            if d <= 0.35:
                add(10, "fresh_reclaim")
            elif d <= 0.75:
                add(5, "acceptable_reclaim")
            elif d >= 1.45:
                add(-7, "late_from_reclaim")
            elif d >= 1.0:
                add(-4, "extended_from_reclaim")
        else:
            add(4, "structure_same")
    elif side == "SHORT" and phase in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]:
        ref = swing_low or swing_high
        if ref:
            d = abs(price - ref) / atr15
            if d <= 0.35:
                add(10, "fresh_reclaim")
            elif d <= 0.75:
                add(5, "acceptable_reclaim")
            elif d >= 1.45:
                add(-7, "late_from_reclaim")
            elif d >= 1.0:
                add(-4, "extended_from_reclaim")
        else:
            add(4, "structure_same")
    elif structure.get("bias") == opp:
        add(-8, "structure_against")

    # 4) 3M impulse quality. Good early 3M trigger is ok; vertical candle gets a small discount.
    fast = safe_float(tf3.get("fast_move_pct"), 0) or 0
    drift = safe_float(tf3.get("drift_pct"), 0) or 0
    if tf3.get("bias") == side:
        if (side == "LONG" and fast >= 0.70) or (side == "SHORT" and fast <= -0.70):
            add(-5, "vertical_3m")
        elif (side == "LONG" and 0.12 <= fast <= 0.45) or (side == "SHORT" and -0.45 <= fast <= -0.12):
            add(4, "clean_3m_trigger")
        elif abs(drift) <= 0.18:
            add(2, "controlled_3m")
    elif tf3.get("bias") == opp:
        add(-6, "3m_against")

    # 5) Liquidity / micro pressure. These are small adjustments, not blockers.
    if side in (liquidity.get("blocks") or []):
        add(-10, "liquidity_blocks_side")
    elif liquidity.get("bias") == side:
        add(4, "liquidity_support")
    elif liquidity.get("bias") == opp:
        add(-5, "liquidity_against")

    pressure_against = 0
    pressure_for = 0
    for block in [cvd, flow, clusters]:
        if block.get("bias") == side:
            pressure_for += 1
        elif block.get("bias") == opp:
            pressure_against += 1
    if pressure_for >= 2:
        add(4, "micro_support")
    if pressure_against >= 2:
        add(-6, "micro_against")
    elif pressure_against == 1:
        add(-3, "one_micro_against")

    # Keep ELS soft. A bad location should not delete the trade; it should only
    # make the score more honest. Strong ICT/early 3M further softens penalties.
    score = int(max(0, min(100, round(score))))

    if score >= 84:
        adjustment = 4
        state = "STRONG"
    elif score >= 72:
        adjustment = 2
        state = "GOOD"
    elif score >= 58:
        adjustment = 0
        state = "NORMAL"
    elif score >= 46:
        adjustment = -3
        state = "WEAK"
    elif score >= 34:
        adjustment = -6
        state = "LATE"
    else:
        adjustment = -9
        state = "VERY_LATE"

    # Do not kill early quality entries: if full ICT + 3M agree, cap the ELS
    # penalty to -4 even if the location model is cautious.
    if adjustment < -4 and ict_strong and tf3.get("bias") == side:
        adjustment = -4
    # Trend continuation without ICT can still be traded, but keep the penalty moderate.
    if adjustment < -7 and tf15.get("bias") == side and tf3.get("bias") == side:
        adjustment = -7

    return {
        "score": score,
        "adjustment": int(adjustment),
        "state": state,
        "penalty": int(abs(adjustment)) if adjustment < 0 else 0,
        "bonus": int(adjustment) if adjustment > 0 else 0,
        "distance_atr": round(distance_atr, 3),
        "factors": factors[-8:],
    }


def entry_quality_adjustment(side, context, late_penalty=0):
    """Unified entry-location quality adjustment.

    This merges Anti-chase + ELS + ICT cap into ONE controlled correction.
    The goal is to avoid double punishment for the same problem:
      - late impulse / stretched price,
      - weak location away from ICT/SMC,
      - no full ICT model.

    It never blocks an entry. It only adjusts quality softly:
      - strong ICT + structure/3M keeps early entries alive;
      - very late non-ICT entries cannot look like high-quality setups;
      - total location penalty is capped so the bot does not overreact.
    """
    context = context if isinstance(context, dict) else {}
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    entry_location = context.get("entry_location") or analyze_entry_location_score(side, context)
    context["entry_location"] = entry_location

    els_adj = int(entry_location.get("adjustment", 0) or 0)
    late_penalty = int(max(0, late_penalty or 0))
    raw_adjustment = els_adj - late_penalty

    ict_setup = str(ict.get("setup", "") or "").upper()
    strong_ict_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_strong = bool(ict.get("bias") == side and ict.get("entry_ok") and ict_setup in strong_ict_setups.get(side, []))
    structure_same = structure.get("bias") == side
    tf3_same = tf3.get("bias") == side
    tf15_same = tf15.get("bias") == side
    trend_stack = bool(tf3_same and tf15_same and (structure_same or ict.get("bias") == side))

    # Compensation rules: early ICT entries should not be killed, while late
    # non-ICT continuation entries still receive a real quality discount.
    if ict_strong and (structure_same or tf3_same):
        max_penalty = 7
    elif trend_stack:
        max_penalty = 10
    elif ict.get("bias") == side:
        max_penalty = 12
    else:
        max_penalty = 15

    # Bonus is deliberately tiny. ELS should not inflate weak signals.
    max_bonus = 5 if ict_strong else 3
    adjustment = max(-max_penalty, min(max_bonus, raw_adjustment))

    state = "NEUTRAL"
    if adjustment >= 3:
        state = "LOCATION_BONUS"
    elif adjustment <= -10:
        state = "LATE_OR_WEAK_LOCATION"
    elif adjustment < 0:
        state = "SOFT_LOCATION_PENALTY"

    result = {
        "adjustment": int(adjustment),
        "raw_adjustment": int(raw_adjustment),
        "late_penalty_used": late_penalty,
        "els_adjustment_used": els_adj,
        "max_penalty": int(max_penalty),
        "state": state,
        "els_score": int(entry_location.get("score", 62) or 62),
    }
    context["entry_quality_adjustment"] = result
    return result


def entry_confirmations(side, context):
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context.get("tf4h") or {}
    structure = context["structure"]
    ict = context.get("ict") or {}
    flow = context["flow"]
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}
    calendar = context.get("calendar") or {}
    news = context["news"]

    confirmations = []
    conflicts = []

    volume_guard = context.get("volume_guard") or {}

    if calendar.get("active"):
        conflicts.append("важлива новина у найближчу годину")
    if volume_guard.get("score", 0) >= 10:
        confirmations.append("сильний обсяг")
    elif volume_guard.get("score", 0) >= 6:
        confirmations.append("активний обсяг")

    # 4H is deliberately background only for this intraday bot.
    # It can add context when it agrees, but it must not block an entry.
    if tf4h.get("bias") == side and abs(tf4h.get("score", 0)) >= 35:
        confirmations.append("4h фон підтримує")

    if tf15.get("bias") == side:
        confirmations.append("15m за напрямом")
    elif tf15.get("bias") == opposite(side):
        conflicts.append("15m проти")

    if tf1h.get("bias") == side:
        confirmations.append("1h підтримує")
    elif tf1h.get("bias") == opposite(side):
        conflicts.append("1h проти")

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    if tf3.get("bias") == side:
        confirmations.append("3m дав тригер")
    elif tf3.get("bias") == opposite(side) and tf3_score_abs >= 32:
        conflicts.append("3m сильно проти")
    elif tf3.get("bias") == opposite(side):
        # Weak opposite 3M is often just a pullback inside the 15M move.
        conflicts.append("3m локальний відкат")

    if structure.get("bias") == side:
        confirmations.append("структура підтверджує")
    elif structure.get("bias") == opposite(side):
        conflicts.append("структура проти")

    ict_setup = str(ict.get("setup", "") or "").upper()
    ict_strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    if ict.get("bias") == side and ict.get("entry_ok") and ict_setup in ict_strong_setups.get(side, []):
        confirmations.append("ICT сетап готовий")
    elif ict.get("bias") == side and ict_setup in ict_weak_setups.get(side, []):
        confirmations.append("ICT частково підтримує")
    elif ict.get("bias") == side:
        confirmations.append("ICT контекст підтримує")
    elif ict.get("bias") == opposite(side) and abs(int(ict.get("score", 0) or 0)) >= 20:
        conflicts.append("ICT проти")
    if ict.get("no_chase") and ict.get("bias") == side:
        conflicts.append("ICT не доганяти")

    if cvd.get("bias") == side:
        confirmations.append("CVD підтверджує")
    elif cvd.get("bias") == opposite(side):
        conflicts.append("CVD проти")

    if derivatives.get("bias") == side:
        confirmations.append("OI/Funding підтримує")
    elif derivatives.get("bias") == opposite(side):
        conflicts.append("OI/Funding проти")


    if liquidity.get("bias") == side:
        confirmations.append("ліквідність підтримує")
    elif liquidity.get("bias") == opposite(side):
        conflicts.append("ліквідність проти")
    if side in liquidity.get("blocks", []):
        conflicts.append("після зняття ліквідності цей напрям не доганяти")

    # Flow and clusters are micro-hints, not blockers.
    if flow.get("bias") == side:
        confirmations.append("потік підтримує")
    elif flow.get("bias") == opposite(side):
        conflicts.append("потік локально проти")

    if clusters.get("bias") == side:
        confirmations.append("кластери локально підтримують")
    elif clusters.get("bias") == opposite(side):
        if side == "SHORT":
            conflicts.append("локальний покупець у стакані")
        else:
            conflicts.append("локальний продавець у стакані")

    if news.get("hard_block_active") and news.get("blocking_bias") == side:
        confirmations.append(f"активна новина дає паливо ({news.get('top_age_min')} хв, {news.get('top_lifecycle_label')})")
    elif news.get("hard_block_active") and news.get("blocking_bias") == opposite(side):
        conflicts.append(news.get("block_reason") or "активні новини проти")

    return confirmations, conflicts

def _select_followup_target(side, context, price, previous_level, targets, risk, atr15,
                            min_rr, min_pct, min_atr, stage_name, projection_allowed):
    """Select TP2/TP3 using configured floors while respecting strong barriers.

    A strong barrier is selected as the stage target even when it is slightly
    inside the numeric floor, because technical structure has priority over an
    approximate percentage. The engine never projects beyond that barrier.
    """
    direction_ok = (lambda level: level > previous_level) if side == "LONG" else (lambda level: level < previous_level)
    ordered = [t for t in (targets or []) if safe_float(t.get("level")) is not None and direction_ok(float(t["level"]))]
    ordered.sort(key=lambda t: abs(float(t["level"]) - float(price)))

    required = max(
        float(risk) * float(min_rr),
        float(price) * float(min_pct) / 100.0,
        float(atr15) * float(min_atr),
    )
    nearest_strong = next((t for t in ordered if int(t.get("priority", 0) or 0) >= STRONG_BARRIER_PRIORITY), None)

    for item in ordered:
        level = float(item["level"])
        travel = abs(level - price)
        if nearest_strong is not None:
            strong_travel = abs(float(nearest_strong["level"]) - price)
            if travel > strong_travel + max(atr15 * 0.05, price * 0.0001):
                break
        if travel + 1e-9 >= required:
            return level, str(item.get("basis") or stage_name), False

    if nearest_strong is not None:
        return float(nearest_strong["level"]), str(nearest_strong.get("basis") or f"{stage_name} strong barrier"), True

    if projection_allowed:
        projected = _measured_move_target(side, context, price, atr15, required)
        if not _strong_barriers_between(side, previous_level, projected["level"], targets):
            return float(projected["level"]), str(projected.get("basis") or stage_name), False

    # Conservative technical extension from the prior target; no hidden barrier is skipped.
    fallback_distance = max(required, abs(previous_level - price) + atr15 * (0.75 if stage_name == "TP2" else 1.05))
    level = price + (fallback_distance if side == "LONG" else -fallback_distance)
    if _strong_barriers_between(side, previous_level, level, targets):
        barrier = _strong_barriers_between(side, previous_level, level, targets)[0]
        return float(barrier["level"]), str(barrier.get("basis") or f"{stage_name} strong barrier"), True
    return float(level), f"{stage_name} 15M ATR/structure extension", False



def make_plan(side, context):
    """Build a barrier-aware full-position 15M/ICT plan.

    Preferred 1.5R / 15M travel remains the target. A technically valid VIABLE
    plan is allowed as risky rather than being converted into a zero-price block.
    Breakout-gate handling prevents the exact deadlock that missed the 79.62 LONG.
    """
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15"), (price or 90) * 0.006) or ((price or 90) * 0.006)

    setup_info = context.get("setup_classifier") or classify_setup(context, side)
    if setup_info.get("side") != side:
        setup_info = classify_setup(context, side)
    context["setup_classifier"] = setup_info
    setup_profile = setup_trade_profile(setup_info.get("type"))
    profile = trade_mode_profile(context, side)
    if setup_profile.get("regime_override"):
        profile["regime"] = setup_profile.get("regime_override")
        profile["reason"] = setup_info.get("label") or profile.get("reason", "")
    regime = profile.get("regime", "NORMAL")
    setup_type = str(setup_info.get("type") or "NO_CLEAN_SETUP")
    setup_label_text = setup_info.get("label") or _setup_label(setup_type)

    # Location Viability is evaluated BEFORE RR. A mathematically attractive
    # projected TP may not legalize selling an exhausted low or buying an
    # exhausted high without a new base/retest/professional ICT anchor.
    location_viability = location_viability_snapshot(
        context, side, {"setup_classifier": setup_info}
    )
    context["location_viability"] = location_viability
    if not location_viability.get("allowed"):
        base_price = float(price or 0.0)
        location_reason = location_viability.get("reason") or "поточна локація не дає життєздатного технічного входу"
        return TradePlan(
            entry=round_price(base_price), stop=round_price(base_price),
            tp1=round_price(base_price), tp2=round_price(base_price), tp3=round_price(base_price),
            risk_pct=0.0, rr1=0, rr2=0, rr3=0,
            invalidation=(
                f"Локація заблокована до нової бази/retest. Сетап: {setup_label_text}. "
                f"Причина: {location_reason}"
            ),
            valid=False,
            validation_reason="локація нежиттєздатна до побудови RR: " + location_reason,
            technical_stop_basis="", technical_tp1_basis="",
            technical_tp2_basis="", technical_tp3_basis="",
            geometry_mode="LOCATION_VIABILITY_FIRST", technical_rr_target=TARGET_RR1_ENTRY,
            better_entry=0.0, geometry_grade="INVALID_LOCATION",
            barrier_mode="LOCATION_BLOCK", preferred_geometry_met=False,
            breakout_gates=[],
        )

    geometry = _select_adaptive_technical_geometry(side, context, atr15, setup_type)
    if geometry is None:
        geometry = _select_geometry_candidate_recovery(side, context, atr15, setup_type)
    valid = bool(side in ["LONG", "SHORT"] and price and geometry)
    validation_reasons = []
    if not valid:
        validation_reasons.append("не вдалося побудувати життєздатну технічну геометрію")

    if geometry:
        stop = float(geometry["stop"]["level"])
        tp1 = float(geometry["target"]["level"])
        stop_basis = str(geometry["stop"].get("basis") or "")
        tp1_basis = str(geometry["target"].get("basis") or "")
        risk = float(geometry["risk"])
        reward1 = float(geometry["reward"])
        rr1 = float(geometry["rr"])
        risk_pct = risk / price * 100 if price else 0.0
        targets = geometry.get("targets") or _technical_target_candidates(side, context, atr15)
        tp1_profile = geometry.get("tp1_profile") or {}
        # Only the viable floors are hard. Geometry-recovery plans use a
        # dedicated continuation floor, but still require real liquidity,
        # durable 15M/ICT invalidation and at least 1.10-1.20R.
        hard_min_pct = safe_float(geometry.get("hard_min_pct"), safe_float(tp1_profile.get("viable_min_pct"), MIN_VIABLE_TP1_DISTANCE_PCT))
        hard_min_atr = safe_float(geometry.get("hard_min_atr"), safe_float(tp1_profile.get("viable_min_atr"), MIN_VIABLE_TP1_ATR_15M))
        hard_min_rr = safe_float(geometry.get("hard_min_rr"), MIN_VIABLE_RR1_ENTRY)
        if reward1 / price * 100 + 1e-9 < hard_min_pct:
            valid = False
            validation_reasons.append("TP1 не має мінімальної повноцінної intraday-відстані")
        if reward1 / atr15 + 1e-9 < hard_min_atr:
            valid = False
            validation_reasons.append("TP1 не має мінімального 15M ATR-руху")
        if rr1 + 1e-9 < hard_min_rr:
            valid = False
            validation_reasons.append("TP1 нижче мінімальної життєздатної RR-геометрії")
        snapshot = ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
        blocking, _, _ = _blocking_barriers_between(
            side, price, tp1, targets, context, atr15, setup_type, snapshot, exclude_level=tp1
        )
        if blocking:
            valid = False
            validation_reasons.append("TP1 пропускає непідтверджений сильний 15M/1H барʼєр")
    else:
        stop = price
        tp1 = price
        stop_basis = tp1_basis = ""
        risk = reward1 = rr1 = risk_pct = 0.0
        targets = []

    snapshot = ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    projection_allowed = bool(
        setup_type not in {"RANGE_EDGE_TRADE", "COUNTERTREND_SCALP"}
        and (
            snapshot.get("closed_confirmed")
            or (snapshot.get("fast_reversal_to_side") or {}).get("confirmed")
            or (tf15.get("bias") == side and structure.get("bias") == side)
            or (geometry and geometry.get("barrier_mode") == "BREAKOUT_GATE")
        )
    )

    tp2_basis = ""
    tp3_basis = ""
    if geometry and risk > 0:
        tp2, tp2_basis, tp2_barrier_override = _select_followup_target(
            side, context, price, tp1, targets, risk, atr15,
            MIN_RR2_ENTRY, MIN_TP2_DISTANCE_PCT, MIN_TP2_ATR_15M,
            "TP2", projection_allowed,
        )
        tp3, tp3_basis, tp3_barrier_override = _select_followup_target(
            side, context, price, tp2, targets, risk, atr15,
            MIN_RR3_ENTRY, MIN_TP3_DISTANCE_PCT, MIN_TP3_ATR_15M,
            "TP3", projection_allowed,
        )
        if side == "LONG":
            if not tp2_barrier_override:
                tp2 = max(tp2, tp1 + max(atr15 * 0.18, price * 0.0008))
            if not tp3_barrier_override:
                tp3 = max(tp3, tp2 + max(atr15 * 0.22, price * 0.0010))
        else:
            if not tp2_barrier_override:
                tp2 = min(tp2, tp1 - max(atr15 * 0.18, price * 0.0008))
            if not tp3_barrier_override:
                tp3 = min(tp3, tp2 - max(atr15 * 0.22, price * 0.0010))
    else:
        tp2 = tp1
        tp3 = tp1

    if side == "LONG":
        invalidation = (
            f"15m закриття нижче {round_price(stop)} або злам 3m/структури проти LONG. "
            f"Сетап: {setup_label_text}. Режим: {regime_label(regime)}. "
            f"{setup_info.get('stop_rule', 'стоп за структурою')} | {setup_info.get('tp_rule', 'TP динамічні')}"
        )
    else:
        invalidation = (
            f"15m закриття вище {round_price(stop)} або злам 3m/структури проти SHORT. "
            f"Сетап: {setup_label_text}. Режим: {regime_label(regime)}. "
            f"{setup_info.get('stop_rule', 'стоп за структурою')} | {setup_info.get('tp_rule', 'TP динамічні')}"
        )

    reward2 = abs(tp2 - price) if price and tp2 is not None else 0.0
    reward3 = abs(tp3 - price) if price and tp3 is not None else 0.0
    geometry_mode = "3M_TACTICAL" if geometry and geometry["stop"].get("timeframe") == "3M" else "15M_ICT_STRUCTURAL"
    return TradePlan(
        entry=round_price(price), stop=round_price(stop), tp1=round_price(tp1),
        tp2=round_price(tp2), tp3=round_price(tp3), risk_pct=round(risk_pct, 3),
        rr1=round(rr1, 2) if risk else 0, rr2=round(reward2 / risk, 2) if risk else 0,
        rr3=round(reward3 / risk, 2) if risk else 0, invalidation=invalidation,
        valid=bool(valid), validation_reason="; ".join(dict.fromkeys(validation_reasons)),
        technical_stop_basis=stop_basis, technical_tp1_basis=tp1_basis,
        technical_tp2_basis=tp2_basis, technical_tp3_basis=tp3_basis,
        geometry_mode=geometry_mode, technical_rr_target=TARGET_RR1_ENTRY, better_entry=0.0,
        geometry_grade=str((geometry or {}).get("geometry_grade") or "INVALID"),
        barrier_mode=str((geometry or {}).get("barrier_mode") or "NONE"),
        preferred_geometry_met=bool((geometry or {}).get("preferred_geometry_met", False)),
        breakout_gates=[
            {"level": round_price(g.get("level")), "basis": str(g.get("basis") or "")}
            for g in ((geometry or {}).get("breakout_gates") or [])
        ],
    )

def _evaluate_new_setup_core(context):
    side = context["bias"]
    if side not in ["LONG", "SHORT"] or not context.get("price"):
        # Diagnostic quality: not an entry probability, but a market readiness score.
        # Do not show 0/100 when the market is simply balanced.
        tf3 = context.get("tf3") or {}
        tf15 = context.get("tf15") or {}
        structure = context.get("structure") or {}
        ict = context.get("ict") or {}
        cvd = context.get("cvd") or {}
        flow = context.get("flow") or {}
        tf1h = context.get("tf1h") or {}
        tf4h = context.get("tf4h") or {}
        ict_balance, ict_balance_reason = detect_ict_balance_market(context)
        readiness = 28
        for block, weight in [
            (tf15, 0.22),
            (structure, 0.26),
            (ict, 0.28),
            (cvd, 0.14),
            (flow, 0.10),
            (tf1h, 0.08),
            (tf4h, 0.04),
        ]:
            readiness += min(8, abs(int(block.get("score", 0) or 0)) * weight)
        readiness_quality = int(max(25, min(55, readiness)))

        if ict_balance:
            return {
                "action": "NO_TRADE",
                "side": "NEUTRAL",
                "quality": min(readiness_quality, 45),
                "title": "ВХОДУ НЕМАЄ",
                "reason": ict_balance_reason,
                "plan": None,
                "confirmations": [],
                "conflicts": [ict_balance_reason, "Не плутати з відкатом: для входу потрібен вихід з EQ, sweep або BOS + retest"],
                "show_wait_plan": False,
            }

        reversal_after_failed = context.get("reversal_after_failed_trade") or {}
        if reversal_after_failed.get("active"):
            rev_side = reversal_after_failed.get("side")
            rev_plan = make_plan(rev_side, context) if rev_side in ["LONG", "SHORT"] else None
            return {
                "action": "WATCH",
                "side": rev_side,
                "quality": int(max(readiness_quality, min(66, reversal_after_failed.get("score", 55)))),
                "title": f"ЧЕКАТИ — {rev_side} РОЗВОРОТ ПІСЛЯ НЕВДАЛОГО ВИХОДУ",
                "reason": reversal_after_failed.get("reason") or "після слабкого EXIT шукаємо протилежний розворот",
                "plan": rev_plan,
                "confirmations": reversal_after_failed.get("confirmations") or [],
                "conflicts": reversal_after_failed.get("conflicts") or [],
                "reversal_after_failed_trade": True,
                "show_wait_plan": True,
            }

        # Preparation mode even when the global bias is still NEUTRAL.
        # Example: 4H/1H are against or neutral, total_score is not enough for a market bias,
        # but 3M and ICT already point the same way. In this case the user should not see only
        # "ВХОДУ НЕМАЄ". The bot must show what direction is preparing and from which price
        # the setup becomes active.
        prep_side = None
        prep_confirmations = []
        prep_conflicts = []
        for candidate in ["LONG", "SHORT"]:
            tf3_same = tf3.get("bias") == candidate and abs(int(tf3.get("score", 0) or 0)) >= 18
            ict_same = ict.get("bias") == candidate and abs(int(ict.get("score", 0) or 0)) >= 12
            structure_same = structure.get("bias") == candidate and abs(int(structure.get("score", 0) or 0)) >= 18
            tf15_same = tf15.get("bias") == candidate and abs(int(tf15.get("score", 0) or 0)) >= 18
            flow_same = flow.get("bias") == candidate and abs(int(flow.get("score", 0) or 0)) >= 8
            cvd_same = cvd.get("bias") == candidate and abs(int(cvd.get("score", 0) or 0)) >= 8

            tf3_against_strong = tf3.get("bias") == opposite(candidate) and abs(int(tf3.get("score", 0) or 0)) >= 32
            ict_against_entry = ict.get("bias") == opposite(candidate) and ict.get("state") == "ENTRY_MODEL"

            prep_score = 0
            if tf3_same:
                prep_score += 3
            if ict_same:
                prep_score += 3
            if structure_same:
                prep_score += 2
            if tf15_same:
                prep_score += 2
            if flow_same or cvd_same:
                prep_score += 1

            if prep_score >= 5 and not tf3_against_strong and not ict_against_entry:
                prep_side = candidate
                if tf3_same:
                    prep_confirmations.append("3M вже показує напрям, але ще потрібен тригер ціни")
                if ict_same:
                    prep_confirmations.append("ICT підтримує ідею")
                if structure_same:
                    prep_confirmations.append("структура підтримує напрям")
                if tf15.get("bias") == "NEUTRAL":
                    prep_conflicts.append("15M ще нейтральний — вхід тільки після 3M reclaim/ретесту")
                if tf1h.get("bias") == opposite(candidate) or int(tf1h.get("score", 0) or 0) * (1 if candidate == "LONG" else -1) < -15:
                    prep_conflicts.append("1H слабкий/проти — не заходити без чіткого тригера")
                if tf4h.get("bias") == opposite(candidate) or int(tf4h.get("score", 0) or 0) * (1 if candidate == "LONG" else -1) < -35:
                    prep_conflicts.append("4H проти — це підготовка, не агресивний вхід")
                break

        if prep_side:
            plan = make_plan(prep_side, context)
            return {
                "action": "WATCH",
                "side": prep_side,
                "quality": int(max(readiness_quality, min(66, readiness_quality + 8))),
                "title": f"ЧЕКАТИ — {prep_side} ГОТУЄТЬСЯ",
                "reason": "напрям формується, але потрібна ціна-тригер: reclaim/ретест на 3M",
                "plan": plan,
                "confirmations": prep_confirmations,
                "conflicts": prep_conflicts,
            }

        long_exhausted, long_exhausted_reason = detect_exhausted_move("LONG", context)
        short_exhausted, short_exhausted_reason = detect_exhausted_move("SHORT", context)

        no_trade_conflicts = []
        if long_exhausted:
            no_trade_conflicts.append("LONG вже відіграний після сильного імпульсу")
            no_trade_conflicts.append("SHORT ще не підтверджений структурою / ICT")
            reason = long_exhausted_reason or "LONG вже відіграний, а SHORT ще не готовий"
        elif short_exhausted:
            no_trade_conflicts.append("SHORT вже відіграний після сильного імпульсу")
            no_trade_conflicts.append("LONG ще не підтверджений структурою / ICT")
            reason = short_exhausted_reason or "SHORT вже відіграний, а LONG ще не готовий"
        elif ict.get("setup") in ["BALANCE_MIDRANGE", "CONTEXT_ONLY", "DISCOUNT_CONTEXT", "PREMIUM_CONTEXT"]:
            reason = "ринок у балансі / ICT-сетап не готовий"
        else:
            reason = "перевага нечітка, немає професійного входу"
        return {
            "action": "NO_TRADE",
            "side": "NEUTRAL",
            "quality": readiness_quality,
            "title": "ВХОДУ НЕМАЄ",
            "reason": reason,
            "plan": None,
            "confirmations": [],
            "conflicts": no_trade_conflicts,
        }

    confirmations, conflicts = entry_confirmations(side, context)
    exhausted, exhausted_reason = detect_exhausted_move(side, context)
    late, late_reason = is_late_chase(side, context)
    late_penalty = soft_late_entry_penalty(side, context, late_reason) if late else 0
    if late and late_reason:
        conflicts.append(f"мʼякий anti-chase штраф -{late_penalty}: {late_reason}")

    # Internal Entry Location Score: improves the bot brain without adding
    # a new noisy Telegram line. It does not block entries; it only makes
    # quality/risk more honest when the direction is right but the location is late.
    entry_location = analyze_entry_location_score(side, context)
    context["entry_location"] = entry_location

    # Setup Classifier is now the first gate before plan/ENTRY logic.
    # Direction can be correct, but without a top setup the bot must stay in WATCH.
    setup_classifier = classify_setup(context, side)
    context["setup_classifier"] = setup_classifier
    reentry_review = analyze_reentry_quality_review(context.get("runtime_state") or {}, context)
    context["reentry_review"] = reentry_review
    context["reentry_cooldown"] = reentry_review  # compatibility alias
    entry_risk_package = entry_risk_package_snapshot(
        context, side, {"setup_classifier": setup_classifier}
    )
    context["entry_risk_package"] = entry_risk_package

    # Geometry memory is captured before make_plan, so a confirmed continuation
    # is not lost merely because the first stop/TP pairing is invalid.
    capture_pre_geometry_opportunity(context, side, setup_classifier)
    plan = make_plan(side, context)
    rr_status = smart_money_rr_status(plan)
    plan_invalid = bool(not plan or not bool(getattr(plan, "valid", False)))
    if plan_invalid:
        mark_geometry_opportunity_failure(context, side, setup_classifier, plan)
    mode_profile = trade_mode_profile(context, side)
    mode_profile["atr15"] = context.get("atr15") or safe_float((context.get("tf15") or {}).get("atr"), None)
    market_regime = mode_profile.get("regime", "NORMAL")
    regime_engine = mode_profile.get("regime_engine") or context.get("regime_engine") or context.get("market_regime") or {}
    if not isinstance(regime_engine, dict):
        regime_engine = {"name": str(market_regime or "NORMAL"), "regime_type": str(market_regime or "NORMAL")}
    context["regime_engine"] = regime_engine
    context["market_regime"] = regime_engine
    regime_type = str(regime_engine.get("regime_type") or regime_engine.get("name") or market_regime or "NORMAL").upper()
    regime_entry_action = str(regime_engine.get("entry_action") or "ALLOW").upper()
    regime_allowed_setups = set(regime_engine.get("allowed_setups") or [])
    regime_quality_cap = regime_engine.get("quality_cap")
    reentry_review = context.get("reentry_review") or context.get("reentry_cooldown") or {}
    reversal_after_failed = context.get("reversal_after_failed_trade") or {}
    reversal_active_for_side = bool(reversal_after_failed.get("active") and reversal_after_failed.get("side") == side)
    reversal_entry_allowed = bool(reversal_active_for_side and reversal_after_failed.get("allow_entry"))

    tf3 = context["tf3"]
    tf15 = context["tf15"]
    tf1h = context["tf1h"]
    tf4h = context.get("tf4h") or {}
    structure = context["structure"]
    ict = context.get("ict") or {}
    flow = context["flow"]
    cvd = context.get("cvd") or {}
    clusters = context.get("clusters") or {}
    derivatives = context.get("derivatives") or {}
    liquidity = context.get("liquidity") or {}
    calendar = context.get("calendar") or {}
    news = context.get("news") or {}
    ict_balance, ict_balance_reason = detect_ict_balance_market(context, side)

    def block_points(block, same, opposite_penalty, neutral=0, weak_opposite_penalty=None):
        bias = block.get("bias")
        score = abs(int(block.get("score", 0) or 0))
        if bias == side:
            return same
        if bias == opposite(side):
            if weak_opposite_penalty is not None and score < 32:
                return -weak_opposite_penalty
            return -opposite_penalty
        return neutral

    # Intraday quality model WITHOUT BTC↔BZ:
    # ICT + SMC/structure define the idea.
    # 3M is the main timing trigger for early entries.
    # 15M/1H confirm strength. CVD/Flow/Clusters/OI adjust risk, but do not fully block ICT+3M.
    score_for_side = side_score(context["total_score"], side)
    quality = 46 + min(9, max(0, score_for_side // 10))
    quality += block_points(tf15, 10, 12)
    quality += block_points(structure, 12, 14)
    if ict.get("state") == "ENTRY_MODEL":
        quality += block_points(ict, 16, 16)
    elif ict.get("bias") == side and ict.get("score", 0):
        quality += block_points(ict, 6, 6)
    if ict.get("entry_ok") and ict.get("bias") == side:
        quality += 8
    if ict.get("no_chase") and ict.get("bias") == side:
        quality -= 18
    # 3M has strong weight: it is the timing trigger.
    quality += block_points(tf3, 16, 16, neutral=0, weak_opposite_penalty=6)
    quality += block_points(tf1h, 4, 6)
    quality += block_points(tf4h, 1, 2)
    quality += block_points(cvd, 8, 9)
    quality += block_points(derivatives, 6, 7)
    quality += block_points(liquidity, 7, 10)
    quality += block_points(flow, 6, 6)
    quality += block_points(clusters, 2, 2)

    # Setup-specific quality nudge. This is intentionally smaller than ICT/3M:
    # the classifier gates the type of trade, while the existing engine still scores strength.
    quality += int(setup_classifier.get("quality_adjustment", 0) or 0)

    setup_type_for_regime = str(setup_classifier.get("type") or "NO_CLEAN_SETUP")
    early_fast_path_types = {
        "TREND_IGNITION_ENTRY",
        "PRIME_ICT_LOCATION_OVERRIDE",
        "RANGE_COMPRESSION_BREAKOUT",
        "PULLBACK_CONTINUATION_FAST_ENTRY",
        "SWEEP_RECLAIM_EARLY_ENTRY",
    }
    regime_pending = bool((not regime_engine.get("is_stable", True)) or int(regime_engine.get("pending_count", 0) or 0) >= 1)
    early_fast_path_ok = bool(
        regime_pending
        and setup_type_for_regime in early_fast_path_types
        and setup_classifier.get("entry_allowed")
        and not setup_classifier.get("block_entry")
    )
    regime_override_allowed = bool(
        setup_classifier.get("professional_override")
        or setup_type_for_regime in regime_allowed_setups
        or early_fast_path_ok
        or (regime_type == "TREND_EXPANSION" and setup_type_for_regime in ["TREND_CONTINUATION", "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY"])
    )
    regime_hard_block = bool(regime_engine.get("hard_block") and not regime_override_allowed)
    regime_risky_only = bool(regime_entry_action == "RISKY_ONLY" and not (regime_type == "TREND_EXPANSION" and setup_type_for_regime == "TREND_CONTINUATION"))
    regime_wait_only = bool(regime_entry_action == "WAIT" and not regime_override_allowed)

    regime_adj = int(regime_engine.get("entry_quality_adjustment", 0) or 0)
    if regime_adj:
        quality += regime_adj
    if regime_quality_cap is not None:
        try:
            quality = min(quality, int(regime_quality_cap))
        except Exception:
            pass

    if regime_type and regime_type not in ["NORMAL", "UNKNOWN"]:
        confirmations.append(f"режим ринку: {regime_label(regime_engine)}")
    if regime_risky_only:
        conflicts.append("режим ринку дозволяє тільки ризиковий/обережний вхід")
    if regime_wait_only:
        conflicts.append("режим ринку: потрібне підтвердження, не відкривати з поточної точки")
    if early_fast_path_ok:
        confirmations.append("швидкий шлях: сильний ранній сетап дозволено навіть поки режим ринку ще підтверджується")
    if regime_hard_block:
        conflicts.append(regime_engine.get("reason") or "режим ринку блокує вхід")

    if reversal_active_for_side:
        quality += 8 if reversal_entry_allowed else 4
        confirmations.append("REVERSAL: попередня угода зламалась, шукаємо протилежний рух")
        for item in reversal_after_failed.get("confirmations") or []:
            if item not in confirmations:
                confirmations.append(item)
        for item in reversal_after_failed.get("conflicts") or []:
            if item not in conflicts:
                conflicts.append(item)

    if news.get("hard_block_active") and news.get("blocking_bias") == side:
        quality += 2
    elif news.get("hard_block_active") and news.get("blocking_bias") == opposite(side):
        quality -= 7

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    tf3_same = tf3.get("bias") == side
    tf3_neutral = tf3.get("bias") == "NEUTRAL"
    tf3_weak_pullback = tf3.get("bias") == opposite(side) and tf3_score_abs < 32
    tf3_strong_against = tf3.get("bias") == opposite(side) and tf3_score_abs >= 42

    tf15_same = tf15.get("bias") == side
    tf15_neutral = tf15.get("bias") == "NEUTRAL"
    tf15_against = tf15.get("bias") == opposite(side)

    structure_same = structure.get("bias") == side
    structure_neutral = structure.get("bias") == "NEUTRAL"
    structure_against = structure.get("bias") == opposite(side)

    ict_same = ict.get("bias") == side
    ict_entry_ok = ict_same and bool(ict.get("entry_ok"))
    ict_no_chase = ict_same and bool(ict.get("no_chase"))
    ict_against = ict.get("bias") == opposite(side) and ict.get("state") == "ENTRY_MODEL" and abs(int(ict.get("score", 0) or 0)) >= 22

    # ICT priority layer. ICT is not a hard blocker for every intraday trend
    # continuation, because BZU can move without a perfect FVG/OB retest.
    # But a normal 90+ quality ENTRY must have a real ICT model or a very strong
    # trend stack. Without ICT, the bot may still enter, but quality is capped
    # and the signal is treated as trend/risky rather than ideal.
    ict_score_abs = abs(int(ict.get("score", 0) or 0))
    ict_entry_model = ict_same and ict.get("state") == "ENTRY_MODEL" and bool(ict.get("entry_ok"))
    ict_context_support = ict_same and ict_score_abs >= 16
    ict_context_against = ict.get("bias") == opposite(side) and ict_score_abs >= 16

    cvd_reliable = cvd.get("confidence", "HIGH") != "LOW"
    cvd_same = cvd_reliable and cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 10
    flow_same = flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10
    oi_same = derivatives.get("bias") == side and abs(int(derivatives.get("score", 0) or 0)) >= 10

    strong_cvd_against = cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 22
    strong_oi_against = derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 14

    # Micro-pressure risk layer.
    # ICT/SMC + 3M is allowed to open early, but if CVD/flow/clusters are against,
    # the bot must label the entry as risky instead of pretending it is a 90+ setup.
    cvd_score_side = side_score(int(cvd.get("score", 0) or 0), side)
    flow_score_side = side_score(int(flow.get("score", 0) or 0), side)
    clusters_score_side = side_score(int(clusters.get("score", 0) or 0), side)
    cvd_pressure_against = cvd_reliable and (cvd_score_side <= -10 or str(cvd.get("state", "")).upper() in [
        "SELLERS_DOMINATE" if side == "LONG" else "BUYERS_DOMINATE"
    ])
    flow_pressure_against = flow_score_side <= -10 or flow.get("bias") == opposite(side)
    clusters_pressure_against = clusters_score_side <= -4 or clusters.get("bias") == opposite(side)
    pressure_risk_count = sum([
        bool(cvd_pressure_against),
        bool(flow_pressure_against),
        bool(clusters_pressure_against),
    ])
    pressure_risk = pressure_risk_count >= 1
    heavy_pressure_risk = pressure_risk_count >= 2

    if cvd_pressure_against and "CVD проти" not in conflicts:
        conflicts.append("CVD/дельта поки проти — ранній вхід тільки як ризиковий")
    if flow_pressure_against and "потік локально проти" not in conflicts:
        conflicts.append("потік локально проти — потрібен швидкий розворот після входу")
    if clusters_pressure_against:
        if side == "LONG" and "локальний продавець у стакані" not in conflicts:
            conflicts.append("локальний продавець у стакані")
        elif side == "SHORT" and "локальний покупець у стакані" not in conflicts:
            conflicts.append("локальний покупець у стакані")

    if market_regime in ["RANGE", "NEWS_IMPULSE", "REVERSAL"]:
        confirmations.append(f"режим: {regime_label(market_regime)} — TP/стоп адаптовані")

    setup_line = setup_classifier_text(setup_classifier, short=True)
    if setup_line:
        confirmations.append(f"Сетап: {setup_line}")
    if not setup_classifier.get("entry_allowed"):
        conflicts.append(setup_classifier.get("reason") or "сетап ще не дає право на вхід")
    elif setup_classifier.get("risk_mode") == "RISKY":
        conflicts.append("класифікатор: сетап дозволений тільки як ризиковий/швидкий")

    if rr_status.get("ok"):
        if rr_status.get("label"):
            confirmations.append(rr_status.get("label"))
        if rr_status.get("rr1", 0) >= STRONG_RR1_BONUS:
            quality += 4
        elif rr_status.get("rr1", 0) >= GOOD_RR1_BONUS:
            quality += 2
    else:
        conflicts.append(rr_status.get("label") or "RR не відповідає Smart Money мінімуму")
    if plan_invalid:
        conflicts.append(getattr(plan, "validation_reason", "") or "технічний план не пройшов перевірку")

    review_active = bool(reentry_review.get("active") and reentry_review.get("side") == side)
    reentry_replay_guard = bool(review_active and reentry_review.get("block_reentry"))
    reentry_rebuilt = bool(review_active and reentry_review.get("reset_confirmed"))
    if reentry_rebuilt:
        msg = reentry_review.get("reason") or "попередня невдача не впливає: сформовано новий незалежний сетап"
        if msg not in confirmations:
            confirmations.append(msg)
    elif reentry_replay_guard:
        conflicts.append(reentry_review.get("reason") or "повторюється та сама зламана теза без нового ринкового підтвердження")

    if context.get("low_liquidity_risk"):
        conflicts.append("низька ліквідність/тиха сесія — ризик спреду і slippage")
    if context.get("price_warning"):
        conflicts.append("ціна TV/OKX розходиться — вхід тільки обережно")
    if cvd.get("confidence") == "LOW":
        confirmations.append("CVD має низьку довіру — не є головним фільтром")

    # Higher timeframe counter-trend gate.
    # 4H is context/filter only. It must not forbid every intraday reversal,
    # but ICT alone is not enough for a market ENTRY.
    # A real entry needs 3M confirmation; 15M only upgrades strength.
    tf4h_score_side = side_score(int(tf4h.get("score", 0) or 0), side)
    tf1h_score_side = side_score(int(tf1h.get("score", 0) or 0), side)
    tf4h_strong_against = tf4h_score_side <= -35
    tf1h_against_or_heavy = tf1h_score_side <= -15 or tf1h.get("bias") == opposite(side)
    htf_countertrend = tf4h_strong_against and tf1h_against_or_heavy
    intraday_confirmation_present = tf3_same or tf15_same or structure_same
    real_pressure_present = cvd_same or flow_same or oi_same
    countertrend_wait_required = bool(
        htf_countertrend
        and not tf3_same
        and not tf15_same
        and not structure_same
        and not real_pressure_present
    )
    if countertrend_wait_required:
        conflicts.append("4H/1H проти — ICT сам не дає вхід, потрібне 3M підтвердження")

    core_direction_ok = (
        (tf15_same and not structure_against)
        or (structure_same and not tf15_against)
        or (ict_same and not tf15_against and not structure_against)
        or (tf15_neutral and (structure_same or ict_same))
    )

    pressure_ok = cvd_same or flow_same or oi_same or (ict_entry_ok and tf3_same)

    # Professional structure gate.
    # ICT remains the entry model, but ICT + 3M alone must not open a trade
    # from the middle of balance. A real ENTRY needs at least one structural
    # confirmation so the bot does not buy/sell every small 3M twitch:
    #   - 15M already agrees, OR
    #   - SMC structure agrees (BOS/CHOCH/sweep reclaim), OR
    #   - ICT itself is a liquidity sweep/reclaim model, OR
    #   - price holds a BOS continuation level.
    # If this is missing, the bot keeps WATCH/activation, so the user is not
    # late: it shows the idea, but waits for reclaim/rejection instead of ENTRY.
    structure_phase = str(structure.get("phase", "") or "").upper()
    ict_setup = str(ict.get("setup", "") or "").upper()
    side_structure_phases = {
        "LONG": ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"],
        "SHORT": ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"],
    }
    structure_reclaim_same = structure_phase in side_structure_phases.get(side, [])
    # ICT quality hierarchy.
    # STRONG ICT = real Smart Money entry model: sweep/reclaim, FVG/OB retrace,
    # or clear discount/premium FVG/OB reaction. Only this can justify 90+ quality.
    # WEAK ICT = BOS continuation/hold or directional ICT context without a clean
    # FVG/OB/sweep entry. It supports a trend trade, but must not be called
    # "ICT сетап готовий" and must not inflate quality to 90+.
    ict_strong_setups = {
        "LONG": ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "DISCOUNT_FVG_OB_LONG"],
        "SHORT": ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "PREMIUM_FVG_OB_SHORT"],
    }
    ict_weak_setups = {
        "LONG": ["BOS_LONG_CONTINUATION_HOLD"],
        "SHORT": ["BOS_SHORT_CONTINUATION_HOLD"],
    }
    ict_strong_model = bool(ict_same and ict.get("entry_ok") and ict_setup in ict_strong_setups.get(side, []))
    ict_weak_model = bool(ict_same and ict_setup in ict_weak_setups.get(side, []))
    ict_full_model = bool(ict_strong_model)
    ict_reclaim_same = bool(ict_strong_model or ict_weak_model)
    trend_stack_same = bool(tf3_same and tf15_same and (tf1h.get("bias") == side or tf4h.get("bias") == side or structure_same))
    strong_trend_stack = bool(tf3_same and tf15_same and structure_same and (tf1h.get("bias") == side or tf4h.get("bias") == side))
    professional_override_types = ["TREND_IGNITION_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "RANGE_COMPRESSION_BREAKOUT", "PULLBACK_CONTINUATION_FAST_ENTRY", "SWEEP_RECLAIM_EARLY_ENTRY"]
    dual_snapshot_for_entry = ((context.get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side))
    fast_reversal_bridge_entry = dual_snapshot_for_entry.get("fast_reversal_to_side") or {}
    fast_reversal_entry_ok = bool(
        fast_reversal_bridge_entry.get("confirmed")
        and setup_type_for_regime in {"SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "COUNTERTREND_SCALP", "PRIME_ICT_LOCATION_OVERRIDE"}
        and setup_classifier.get("entry_allowed")
        and not setup_classifier.get("block_entry")
    )
    strong_ict_permission_for_entry = strong_ict_early_permission_snapshot(context, side)
    prime_override_safe = bool(
        setup_classifier.get("type") != "PRIME_ICT_LOCATION_OVERRIDE"
        or strong_ict_permission_for_entry.get("allowed")
    )
    classifier_professional_override = bool(
        setup_classifier.get("type") in professional_override_types
        and setup_classifier.get("entry_allowed")
        and setup_classifier.get("professional_override")
        and prime_override_safe
        and entry_risk_package.get("allowed")
    )

    if ict_strong_model:
        quality += 8
        if "ICT сетап готовий" not in confirmations:
            confirmations.append("ICT сетап готовий")
    elif ict_weak_model:
        quality += 3
        confirmations.append("ICT частково підтримує напрям, але це не повний FVG/OB/sweep сетап")
    elif ict_context_support:
        quality += 1
        confirmations.append("ICT лише контекстно підтримує напрям")
    elif ict_context_against:
        quality -= 12
        conflicts.append("ICT проти напрямку — якість входу нижча")
    else:
        conflicts.append("немає повного ICT-сетапу — якість входу обмежена")

    structural_entry_ok = bool(tf15_same or structure_same or structure_reclaim_same or ict_reclaim_same or reversal_entry_allowed or classifier_professional_override or fast_reversal_entry_ok)
    structure_gate_missing = bool(tf3_same and ict_entry_ok and not structural_entry_ok and not classifier_professional_override)
    if structure_gate_missing:
        conflicts.append("ICT+3M є, але немає 15M/BOS/reclaim — чекати структурне підтвердження")

    # Classic/early entry: 3M must confirm the direction, but not alone.
    # 15M may still be NEUTRAL only if BOS/reclaim/ICT sweep has confirmed
    # structure. This avoids late entries while filtering mid-range false starts.
    trigger_entry_ok = (
        (tf3_same or classifier_professional_override or fast_reversal_entry_ok)
        and (core_direction_ok or classifier_professional_override or fast_reversal_entry_ok)
        and structural_entry_ok
        # CVD/flow against must NOT forbid an ICT+3M early entry.
        # It changes the signal to RISKY_ENTRY and caps quality, so we do not enter late.
        and not strong_oi_against
        and not ict_against
        and not countertrend_wait_required
        and side not in liquidity.get("blocks", [])
    )

    # Pullback / pre-trigger state:
    # 3M RANGE or weak 3M counter-move is useful for WATCH, but it must not open
    # an immediate trade. The bot waits for 3M to turn with the setup.
    pullback_watch_ok = (
        core_direction_ok
        and pressure_ok
        and (tf3_neutral or tf3_weak_pullback)
        and not tf3_strong_against
        and not strong_cvd_against
        and not strong_oi_against
        and not ict_against
        and side not in liquidity.get("blocks", [])
    )

    trigger_ok = trigger_entry_ok

    # 4H is not included here. It is only context.
    trend_ok = core_direction_ok

    hard_conflict = (
        regime_hard_block
        or not entry_risk_package.get("allowed")
        or side in liquidity.get("blocks", [])
        or ict_against
        or (tf15_against and structure_against)
        or (strong_cvd_against and strong_oi_against)
        or (tf3_strong_against and strong_cvd_against)
        or reentry_replay_guard
        or plan_invalid
    )

    # EARLY ICT ENTRY GATE.
    # Purpose: do not wait for the full confirmation stack after ICT + SMC
    # already agree. This keeps the user's preferred early entries.
    # It does NOT create a normal high-confidence ENTRY; it only allows
    # RISKY_ENTRY when:
    #   - ICT is aligned with the trade side,
    #   - market structure is aligned with the trade side,
    #   - the setup quality remains above the risky threshold,
    #   - anti-chase did not mark the move as heavily overextended,
    #   - there is no hard conflict/liquidity block.
    # 3M may be neutral/weak during the retest; it must simply not be strongly
    # against together with strong CVD pressure.
    early_ict_entry_ok = bool(
        strong_ict_permission_for_entry.get("allowed")
        and ict_same
        and structure_same
        and (setup_classifier.get("entry_allowed") or setup_classifier.get("prime_ict_override"))
        and setup_classifier.get("type") != "TREND_CONTINUATION"
        # A partial ICT context + directional 3M is no longer enough. Early
        # override requires a full FVG/OB/sweep model or a fresh hold/retest.
        and (tf3_same or (ict_strong_model and not tf3_strong_against))
        and entry_risk_package.get("allowed")
        and not ict_against
        and not hard_conflict
        and late_penalty < 15
        and not exhausted
        and not ict_balance
        and side not in liquidity.get("blocks", [])
        and not (tf3_strong_against and strong_cvd_against)
        and not countertrend_wait_required
    )

    if not tf3_same and not classifier_professional_override and not fast_reversal_entry_ok:
        # Without 3M confirmation this is preparation, not an entry.
        quality = min(quality, 66)
    elif structure_gate_missing:
        # ICT + 3M without 15M/BOS/reclaim can be a good idea, but not a market entry.
        quality = min(quality, 64)
    elif htf_countertrend and not (tf15_same or structure_same or real_pressure_present):
        # 3M may catch the turn early, but against 4H/1H keep quality realistic.
        quality = min(quality, 78)
    elif htf_countertrend and not tf15_same:
        quality = min(quality, 84)

    # Counter-trend quality honesty. A trade against both 1H and 4H can still be
    # taken only as a risky bounce/reversal attempt, but it must not look like
    # a premium 85-90+ setup in Telegram. This prevents cases like a
    # counter-trend LONG showing 88/100 after a broad downtrend.
    both_htf_against = bool(tf1h.get("bias") == opposite(side) and tf4h.get("bias") == opposite(side))
    one_htf_against = bool(tf1h.get("bias") == opposite(side) or tf4h.get("bias") == opposite(side))
    countertrend_entry = bool(one_htf_against or htf_countertrend)
    if setup_classifier.get("type") == "COUNTERTREND_SCALP":
        countertrend_entry = True
    if both_htf_against and not (ict_strong_model and structure_same and tf15_same):
        quality = min(quality, 76)
    elif one_htf_against and not (ict_strong_model and (structure_same or tf15_same)):
        quality = min(quality, 82)

    setup_quality_cap = setup_classifier.get("quality_cap")
    if setup_quality_cap is not None:
        quality = min(quality, int(setup_quality_cap))
    if setup_classifier.get("force_risky"):
        quality = min(quality, 79)
    if regime_risky_only:
        quality = min(quality, 79)
    if regime_wait_only:
        quality = min(quality, 67)

    if heavy_pressure_risk:
        # Keep the early entry available, but never call it an ideal/strong trade
        # while CVD/flow/clusters show absorption or seller/buyer pressure against it.
        quality = min(quality, 76)
    elif pressure_risk:
        quality = min(quality, 80)

    # ICT priority caps: ICT is the quality anchor, but not a hard blocker.
    # Goal: keep good trend entries, but stop printing 90+ when there is no
    # real ICT setup. Only a STRONG ICT model may score above 88-90.
    if ict_context_against:
        quality = min(quality, 72)
    elif ict_weak_model:
        if strong_trend_stack:
            quality = min(quality, 86)
        elif trend_stack_same:
            quality = min(quality, 83)
        else:
            quality = min(quality, 80)
    elif not ict_strong_model:
        if strong_trend_stack:
            quality = min(quality, 82)
        elif trend_stack_same:
            quality = min(quality, 80)
        else:
            quality = min(quality, 76)

    if context.get("low_liquidity_risk"):
        quality = min(quality, 74)
    if context.get("price_warning"):
        quality = min(quality, 72)

    # Unified entry-location correction. This replaces separate Anti-chase + ELS
    # deductions so the bot does not punish the same late/chase problem twice.
    eq_adjust = entry_quality_adjustment(side, context, late_penalty)
    quality += int(eq_adjust.get("adjustment", 0) or 0)

    # Previous trade outcome never subtracts points by time. The new setup keeps
    # its independently calculated quality. Only an exact replay of the same
    # broken thesis is stopped by the event-driven guard above.

    if calendar.get("active"):
        quality -= 8
        if quality < 75:
            hard_conflict = True
    if hard_conflict:
        quality = min(quality, 56)
    if not trend_ok:
        quality = min(quality, 59)
    elif not trigger_ok:
        # If 3M is RANGE/pullback, the setup can be good, but entry is not active yet.
        quality = min(quality, 66)

    # Late or weak-location entries may still be traded, but they must not look
    # like strong clean setups. This preserves early entries without chasing.
    eq_state = (context.get("entry_quality_adjustment") or {}).get("state")
    if eq_state == "LATE_OR_WEAK_LOCATION" and not ict_strong_model:
        quality = min(quality, 74)
    elif eq_state == "SOFT_LOCATION_PENALTY" and not ict_strong_model:
        quality = min(quality, 82)

    # Final cap must be applied AFTER every bonus/penalty. Previously a setup
    # capped at 86 could receive later bonuses and print 90/100.
    final_caps = []
    if setup_quality_cap is not None:
        final_caps.append(int(setup_quality_cap))
    if regime_quality_cap is not None:
        try:
            final_caps.append(int(regime_quality_cap))
        except Exception:
            pass
    if setup_classifier.get("force_risky") or regime_risky_only:
        final_caps.append(79)
    if regime_wait_only:
        final_caps.append(67)

    pullback_setup = setup_type_for_regime in ["PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY"]
    pullback_confirmation_incomplete = bool(
        pullback_setup
        and (not tf15_same or not structure_same or not bool(regime_engine.get("is_stable", True)))
    )
    if pullback_confirmation_incomplete:
        # A valid early pullback may still be traded, but it is RISKY until a
        # closed 15M/structure and stable regime confirm the continuation.
        if not tf15_same and not structure_same:
            final_caps.append(79)
        else:
            final_caps.append(82)
        conflicts.append("відкат ще не підтверджений одночасно закритою 15M, структурою та стабільним режимом — тільки ризиковий вхід")

    if final_caps:
        quality = min(quality, min(final_caps))
    quality = int(max(0, min(92, quality)))

    if regime_hard_block:
        reason = regime_engine.get("reason") or "режим ринку блокує вхід"
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} РЕЖИМ БЛОКУЄ ВХІД",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": list(dict.fromkeys(conflicts + [reason])),
            "setup_classifier": setup_classifier,
            "regime_engine": regime_engine,
            "regime_gate": True,
            "show_wait_plan": False,
        }

    setup_blocks_entry = bool(setup_classifier.get("block_entry") and not setup_classifier.get("entry_allowed"))
    if setup_blocks_entry:
        reason = setup_classifier.get("reason") or "класифікатор не дав права на вхід"
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, int(setup_classifier.get("quality_cap") or 61)),
            "title": setup_watch_title(side, setup_classifier),
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": list(dict.fromkeys(conflicts + [reason])),
            "setup_classifier": setup_classifier,
            "setup_gate": True,
            "hard_no_chase": setup_classifier.get("type") == "LATE_IMPULSE_CHASE",
            "show_wait_plan": setup_classifier.get("type") not in ["LATE_IMPULSE_CHASE", "COUNTERTREND_PULLBACK_WAIT", "RANGE_MIDDLE_BLOCK"],
        }

    # A plan without valid technical geometry is a hard safety gate.
    # No early override, pending trigger or lifecycle stage may bypass it.
    if plan_invalid:
        reason = getattr(plan, "validation_reason", "") or "технічний план не пройшов перевірку"
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} ТЕХНІЧНИЙ ПЛАН НЕ ГОТОВИЙ",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": list(dict.fromkeys(conflicts + [reason])),
            "plan_geometry_gate": True,
            "show_wait_plan": False,
            "setup_classifier": setup_classifier,
        }

    if structure_gate_missing:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 64),
            "title": f"ЧЕКАТИ — {side} ПОТРІБЕН RECLAIM/BOS",
            "reason": "ICT і 3M вже показують ідею, але структури ще нема: чекати 15M підтвердження, BOS або reclaim/rejection",
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "structure_gate": True,
            "show_wait_plan": True,
        }

    if ict_balance and not classifier_professional_override:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} ТІЛЬКИ ПІСЛЯ ВИХОДУ З БАЛАНСУ",
            "reason": ict_balance_reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": [ict_balance_reason],
            "ict_balance": True,
            "show_wait_plan": True,
        }

    if exhausted:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 55),
            "title": f"ЧЕКАТИ — {side} ВЖЕ ВІДІГРАНИЙ",
            "reason": exhausted_reason,
            "plan": plan,
            "confirmations": [],
            "conflicts": [exhausted_reason],
            "exhausted_move": True,
            "show_wait_plan": False,
        }

    # HARD NO-CHASE GUARD.
    # If the move is already extended enough to receive a heavy anti-chase
    # penalty, the bot must not still print ENTRY/RISKY_ENTRY in the same
    # direction. Direction can be correct, but location is bad. The proper
    # state is WATCH until a pullback/protorgovka/new FVG-OB retest appears.
    # This prevents signals like: "anti-chase -18, не доганяти" +
    # "РИЗИКОВАНИЙ ВХІД SHORT" in the same message.
    composite_guard_for_entry = entry_risk_package.get("composite_exhaustion") or {}
    persistent_guard_for_entry = entry_risk_package.get("persistent_exhaustion") or {}
    heavy_chase = bool(late_penalty >= 15 or composite_guard_for_entry.get("hard_block"))
    professional_chase_exception = bool(
        classifier_professional_override
        and entry_risk_package.get("allowed")
        and not persistent_guard_for_entry.get("active")
        and late_penalty < 18
        and not exhausted
    )
    chase_reason = late_reason or "рух уже розтягнутий — не доганяти; чекати відкат/проторговку або новий ICT/FVG/OB retest"
    if heavy_chase and not professional_chase_exception:
        return {
            "action": "WATCH",
            "side": side,
            "quality": min(quality, 61),
            "title": f"ЧЕКАТИ — {side} НЕ ДОГАНЯТИ",
            "reason": chase_reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": list(dict.fromkeys(conflicts + [chase_reason])),
            "hard_no_chase": True,
            "show_wait_plan": True,
        }

    if quality >= RISKY_QUALITY_MIN and classifier_professional_override:
        setup_type = setup_classifier.get("type")
        if setup_type == "TREND_IGNITION_ENTRY":
            quality = min(quality, 79)
            reason = "ранній Trend Ignition: micro-BOS/старт руху після бази; це не chase, але режим тільки ризиковий"
        elif setup_type == "PRIME_ICT_LOCATION_OVERRIDE":
            quality = min(quality, 76 if not tf3_same else 79)
            reason = "ранній Prime ICT: локація FVG/OB/sweep вже професійна, 3M не сильно проти"
        elif setup_type == "RANGE_COMPRESSION_BREAKOUT":
            quality = min(quality, 77)
            reason = "ранній Range Compression Breakout: середина range дозволена тільки через compression + 3M micro-BOS"
        else:
            quality = min(quality, 77)
            reason = "ранній професійний override: вхід дозволений тільки як ризиковий"
        if late_penalty:
            reason += f"; anti-chase штраф врахований (-{late_penalty})"
        if setup_classifier.get("reason") and setup_classifier.get("reason") not in reason:
            confirmations.append(setup_classifier.get("reason"))
        confirmations.append("Early Professional Override: якість обмежена, стоп/TP адаптовані")
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": list(dict.fromkeys(confirmations)),
            "conflicts": conflicts,
            "countertrend_entry": countertrend_entry,
            "early_professional_override": True,
            "setup_classifier": setup_classifier,
        }

    if quality >= RISKY_QUALITY_MIN and early_ict_entry_ok:
        # Early ICT/SMC is allowed to preserve early entries, but it must not
        # look like a clean 90+ confirmed trade. If it is counter-trend, without
        # 3M confirmation, or without 15M agreement, keep the score in the risky
        # band so Telegram does not overstate the setup quality.
        if countertrend_entry:
            quality = min(quality, 76)
        elif not tf3_same:
            quality = min(quality, 72)
        elif not tf15_same:
            quality = min(quality, 79)
        else:
            quality = min(quality, 82)
        reason = "ранній ICT/SMC вхід: ICT і структура вже за напрям, не чекаємо повного набору підтверджень"
        if late_penalty:
            reason += f"; anti-chase штраф уже врахований (-{late_penalty})"
        if not tf3_same:
            reason += "; 3M ще не ідеальний, тому тільки ризиковий режим"
        if "ранній ICT/SMC: ICT + структура за напрям" not in confirmations:
            confirmations.append("ранній ICT/SMC: ICT + структура за напрям")
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "countertrend_entry": countertrend_entry,
            "early_ict_entry": True,
            "setup_classifier": setup_classifier,
        }


    if quality >= ENTRY_QUALITY_MIN and trigger_ok and not hard_conflict and not pressure_risk and not regime_risky_only and not regime_wait_only and not pullback_confirmation_incomplete:
        if reversal_entry_allowed:
            if ict_strong_model:
                reason = "REVERSAL ENTRY: попередній напрям зламано, 3M + структура + повний ICT підтвердили розворот"
            else:
                reason = "REVERSAL ENTRY: попередній напрям зламано, але ICT не повний — якість обмежена"
        elif not ict_strong_model:
            reason = "трендовий вхід без повного ICT-сетапу: напрям/структура/3M підтверджують, якість обмежена"
            if late_penalty:
                reason += f"; anti-chase штраф уже врахований (-{late_penalty})"
        elif not tf15_same:
            reason = "ранній вхід: 3M підтвердив напрям, 15M ще не запізнився/нейтральний"
        else:
            reason = "сигнал підтверджений: " + " | ".join(confirmations[:4])
        return {
            "action": "ENTRY",
            "side": side,
            "quality": quality,
            "title": f"ВХІД Є — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "countertrend_entry": countertrend_entry,
            "setup_classifier": setup_classifier,
        }

    if quality >= RISKY_QUALITY_MIN and trigger_ok and not hard_conflict:
        # Any RISKY_ENTRY must remain visually honest: it should not print
        # 85-92/100 like a fully confirmed signal. Normal high scores are
        # reserved for ENTRY, not risky/early attempts.
        quality = min(quality, 79 if not countertrend_entry else 76)
        if reversal_active_for_side and not reversal_entry_allowed:
            return {
                "action": "WATCH",
                "side": side,
                "quality": min(quality, 66),
                "title": f"ЧЕКАТИ — {side} РОЗВОРОТ ЩЕ НЕ ПІДТВЕРДЖЕНИЙ",
                "reason": reversal_after_failed.get("reason") or "після невдалої угоди потрібен BOS/reclaim перед переворотом",
                "plan": plan,
                "confirmations": confirmations,
                "conflicts": conflicts,
                "reversal_after_failed_trade": True,
                "show_wait_plan": True,
            }
        if pullback_confirmation_incomplete:
            reason = "відкат у тренді ще не підтверджений закритою 15M/структурою та стабільним режимом — дозволено лише ризиковий вхід"
        elif pressure_risk:
            reason = "ICT/SMC + 3M дають ранній вхід, але CVD/потік/кластери ще не підтвердили — ризиковий режим"
        elif not tf15_same:
            reason = "ранній 3M-тригер; 15M ще не підтвердив повністю, тому ризик вищий"
        else:
            reason = "є ранній тригер, але підтверджень ще не максимум: " + " | ".join(confirmations[:4])
        return {
            "action": "RISKY_ENTRY",
            "side": side,
            "quality": quality,
            "title": f"РИЗИКОВАНИЙ ВХІД — {side}",
            "reason": reason,
            "plan": plan,
            "confirmations": confirmations,
            "conflicts": conflicts,
            "countertrend_entry": countertrend_entry,
            "setup_classifier": setup_classifier,
        }

    if pullback_watch_ok:
        wait_reason = "напрям є, але 3M ще не підтвердив; чекати 3M higher low/reclaim для LONG або lower high/rejection для SHORT"
    elif conflicts:
        wait_reason = "напрям є, але входу ще немає: " + "; ".join(conflicts[:3])
    else:
        wait_reason = "напрям є, але бракує якості входу; орієнтир — Pending ICT зона"

    return {
        "action": "WATCH",
        "side": side,
        "quality": quality,
        "title": f"ЧЕКАТИ — {side} ГОТУЄТЬСЯ",
        "reason": wait_reason,
        "plan": plan,
        "confirmations": confirmations,
        "conflicts": conflicts,
    }
def trade_hit_level(side, price, level):
    if level is None:
        return False
    return price >= level if side == "LONG" else price <= level


def trade_hit_stop(side, price, stop):
    if stop is None:
        return False
    return price <= stop if side == "LONG" else price >= stop


def _parse_iso_ts_ms(value):
    try:
        if not value:
            return 0
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _trade_extremes_since_open(trade, context):
    """High/low actually traded after entry.

    The bot runs every ~15 minutes, while TP can be touched inside a candle and
    disappear before the next run. For TP/MFE accounting we must use candle
    high/low since the trade was opened, not only the latest ticker price.
    We intentionally do NOT use this to trigger newly moved stops, because the
    candle may have traded that level before the stop was moved.
    """
    opened_ms = _parse_iso_ts_ms(getattr(trade, "opened_at", ""))
    highs = []
    lows = []
    for candle in (context.get("candles_3m") or []):
        try:
            if opened_ms and int(candle.ts) + 3 * 60 * 1000 < opened_ms:
                continue
            highs.append(float(candle.high))
            lows.append(float(candle.low))
        except Exception:
            continue
    price = safe_float(context.get("price"))
    if price:
        highs.append(price)
        lows.append(price)
    if not highs or not lows:
        return price, price
    return max(highs), min(lows)




def _trade_extremes_since_stop_update(trade, context):
    """High/low after the current stop became active.

    STOP must be checked by candle high/low, not only by the latest close/price.
    But for a newly moved stop we must not use old candles from before the stop
    was moved. Therefore every stop update stores stop_updated_at, and this
    helper only scans 3M candles after that moment.
    """
    anchor_ms = _parse_iso_ts_ms(getattr(trade, "stop_updated_at", ""))
    if not anchor_ms:
        anchor_ms = _parse_iso_ts_ms(getattr(trade, "opened_at", ""))
    highs = []
    lows = []
    for candle in (context.get("candles_3m") or []):
        try:
            # include candles that overlap the active-stop window
            if anchor_ms and int(candle.ts) + 3 * 60 * 1000 < anchor_ms:
                continue
            highs.append(float(candle.high))
            lows.append(float(candle.low))
        except Exception:
            continue
    price = safe_float(context.get("price"))
    if price:
        highs.append(price)
        lows.append(price)
    if not highs or not lows:
        return price, price
    return max(highs), min(lows)


def trade_hit_stop_by_extreme(side, price, stop, high_since_stop=None, low_since_stop=None):
    if stop is None:
        return False
    if trade_hit_stop(side, price, stop):
        return True
    if side == "LONG":
        return low_since_stop is not None and low_since_stop <= stop
    return high_since_stop is not None and high_since_stop >= stop

def trade_hit_level_by_extreme(side, price, level, high_since_open=None, low_since_open=None):
    if level is None:
        return False
    if trade_hit_level(side, price, level):
        return True
    if side == "LONG":
        return high_since_open is not None and high_since_open >= level
    return low_since_open is not None and low_since_open <= level


def best_trade_price(side, trade, current_price, high_since_open=None, low_since_open=None):
    candidate = current_price
    if side == "LONG" and high_since_open is not None:
        candidate = max(current_price, high_since_open)
    elif side == "SHORT" and low_since_open is not None:
        candidate = min(current_price, low_since_open)
    if not trade.best_price:
        return candidate
    if side == "LONG":
        return max(trade.best_price, candidate)
    return min(trade.best_price, candidate)


def _valid_stop_for_side(side, price, stop):
    if stop is None or price is None:
        return False
    return stop < price if side == "LONG" else stop > price


def _last_smc_trailing_level(side, candles, price, entry, atr_value):
    """Find a practical SMC trailing stop from recent 3M swing structure.

    LONG: trail under the latest meaningful 3M higher low.
    SHORT: trail above the latest meaningful 3M lower high.
    The level must be on the safe side of current price; after TP1 we prefer
    levels that also protect at least breakeven/partial profit.
    """
    if not candles or len(candles) < 20 or not price:
        return None, ""
    highs, lows = swing_points(candles[-110:], 2)
    buffer = max((atr_value or price * 0.004) * 0.12, price * 0.00035)

    if side == "LONG":
        candidates = [safe_float(x.get("price")) for x in lows[-12:]]
        candidates = [x for x in candidates if x and x < price]
        protected = [x for x in candidates if x >= entry * 0.999]
        base = max(protected or candidates) if candidates else None
        if base:
            return round_price(base - buffer), f"SMC: стоп під останній 3M higher low {round_price(base)}"
    else:
        candidates = [safe_float(x.get("price")) for x in highs[-12:]]
        candidates = [x for x in candidates if x and x > price]
        protected = [x for x in candidates if x <= entry * 1.001]
        base = min(protected or candidates) if candidates else None
        if base:
            return round_price(base + buffer), f"SMC: стоп над останній 3M lower high {round_price(base)}"
    return None, ""




def _best_smc_trailing_level(side, context, price, entry, atr_value):
    """Best SMC trailing stop from 3M + 15M swings.

    3M gives the active intraday structure; 15M gives wider structure so the
    stop is not placed inside ordinary 3M noise. After TP1/TP2 this lets the
    bot protect profit while still keeping the stop behind real structure.
    """
    candidates = []
    tf3_level, tf3_reason = _last_smc_trailing_level(side, (context or {}).get("candles_3m") or [], price, entry, atr_value)
    if tf3_level is not None:
        candidates.append((tf3_level, tf3_reason))

    # Wider 15M swing anchor. Use a slightly larger ATR buffer so it does not
    # hug a 15M wick. This is intentionally not used alone if it would worsen
    # protection too much; the final clamp/air rules still apply.
    tf15_level, tf15_reason = _last_smc_trailing_level(side, (context or {}).get("candles_15m") or [], price, entry, (atr_value or price * 0.006) * 1.35)
    if tf15_level is not None:
        candidates.append((tf15_level, tf15_reason.replace("3M", "15M")))

    valid = [(lvl, why) for lvl, why in candidates if _valid_stop_for_side(side, price, lvl)]
    if not valid:
        return None, ""
    if side == "LONG":
        lvl, why = max(valid, key=lambda x: x[0])
    else:
        lvl, why = min(valid, key=lambda x: x[0])
    return round_price(lvl), why


def _tp_hit_note(side, label, level, high_since_open, low_since_open, price):
    """Explain whether TP was reached by live price or candle high/low."""
    level = round_price(level)
    if side == "LONG":
        if high_since_open is not None and high_since_open >= level and (price is None or price < level):
            return f"{label} {level} взято по high свічки 3M/між запусками"
        return f"{label} {level} взято"
    if low_since_open is not None and low_since_open <= level and (price is None or price > level):
        return f"{label} {level} взято по low свічки 3M/між запусками"
    return f"{label} {level} взято"

def _ict_zone_trailing_level(side, ict, price, atr_value):
    """Find an ICT/FVG/OB based protective stop if the zone is usable."""
    if not ict or not price:
        return None, ""
    buffer = max((atr_value or price * 0.004) * 0.10, price * 0.0003)
    zones = []
    if side == "LONG":
        for key, label in [("bull_ob", "bullish OB"), ("bull_fvg", "bullish FVG")]:
            z = ict.get(key)
            if z and not z.get("mitigated"):
                low = safe_float(z.get("low"))
                high = safe_float(z.get("high"))
                if low and high and min(low, high) < price:
                    zones.append((min(low, high) - buffer, label, min(low, high), max(low, high)))
        if zones:
            level, label, low, high = max(zones, key=lambda x: x[0])
            return round_price(level), f"ICT: стоп під {label} {round_price(low)}–{round_price(high)}"
    else:
        for key, label in [("bear_ob", "bearish OB"), ("bear_fvg", "bearish FVG")]:
            z = ict.get(key)
            if z and not z.get("mitigated"):
                low = safe_float(z.get("low"))
                high = safe_float(z.get("high"))
                if low and high and max(low, high) > price:
                    zones.append((max(low, high) + buffer, label, min(low, high), max(low, high)))
        if zones:
            level, label, low, high = min(zones, key=lambda x: x[0])
            return round_price(level), f"ICT: стоп над {label} {round_price(low)}–{round_price(high)}"
    return None, ""



def _stop_air_profile(context, mode_profile=None, stage="PRE_TP1"):
    """Minimum safe air between current price and a moved stop.

    BZU 3M candles can wick 0.15-0.35% without a real reversal.
    A protective stop therefore must stay outside normal 3M noise and should be
    wider before TP1 than after TP1. This function uses both regime and ATR.
    """
    mode_profile = mode_profile or {}
    regime = mode_profile.get("regime", "NORMAL")
    price = safe_float((context or {}).get("price"), 0) or 0
    atr_value = safe_float((context or {}).get("atr15"), 0) or (price * 0.006 if price else 0)
    atr_pct = (atr_value / price * 100.0) if price and atr_value else 0.55

    if stage == "PRE_TP1":
        base = {
            "RANGE": 0.24,
            "REVERSAL": 0.26,
            "PULLBACK": 0.30,
            "TREND": 0.34,
            "NEWS_IMPULSE": 0.42,
            "IMPULSE": 0.34,
            "NORMAL": 0.30,
            "UNKNOWN": 0.30,
        }.get(regime, 0.30)
        atr_mult = 0.46
        cap = 0.58
    elif stage == "POST_TP2":
        base = {
            "RANGE": 0.20,
            "REVERSAL": 0.22,
            "PULLBACK": 0.25,
            "TREND": 0.30,
            "NEWS_IMPULSE": 0.36,
            "IMPULSE": 0.30,
            "NORMAL": 0.26,
            "UNKNOWN": 0.26,
        }.get(regime, 0.26)
        atr_mult = 0.34
        cap = 0.48
    else:  # POST_TP1
        base = {
            "RANGE": 0.22,
            "REVERSAL": 0.24,
            "PULLBACK": 0.28,
            "TREND": 0.32,
            "NEWS_IMPULSE": 0.38,
            "IMPULSE": 0.32,
            "NORMAL": 0.28,
            "UNKNOWN": 0.28,
        }.get(regime, 0.28)
        atr_mult = 0.38
        cap = 0.54

    gap_pct = max(base, atr_pct * atr_mult)
    return round(min(gap_pct, cap), 4)


def _apply_stop_air(side, stop, price, entry, context, mode_profile=None, stage="PRE_TP1"):
    """Push a proposed stop away from current price if it sits inside noise."""
    stop = safe_float(stop)
    price = safe_float(price)
    entry = safe_float(entry)
    if stop is None or price is None or entry is None or not price or not entry:
        return stop, ""
    gap_pct = _stop_air_profile(context, mode_profile, stage)
    gap_abs = price * gap_pct / 100.0
    adjusted = stop
    if side == "LONG":
        max_allowed = price - gap_abs
        if stop > max_allowed:
            adjusted = max_allowed
    else:
        min_allowed = price + gap_abs
        if stop < min_allowed:
            adjusted = min_allowed
    adjusted = round_price(adjusted)
    if adjusted != round_price(stop):
        return adjusted, f"стоп відсунуто від ціни: мінімум ~{gap_pct}% / ATR-buffer, щоб не вибило 1-2 свічками"
    return round_price(stop), f"стоп має запас від ціни ~{gap_pct}%"

def _clamp_stop_between_entry_tp(side, raw_level, entry, tp1, tp2=None, after_tp="TP1"):
    """Clamp protective stop so it is not too tight after TP1.

    After TP1 the stop should usually sit around the middle of entry→TP1,
    while still respecting ICT/SMC context. It must not jump directly under TP1
    on every new 3M higher-low. After TP2 we allow a tighter lock, but still
    avoid placing the stop exactly on the current noise.
    """
    if raw_level is None or not entry or not tp1:
        return raw_level

    if side == "LONG":
        move1 = max(tp1 - entry, entry * 0.002)
        tp1_floor = entry + move1 * 0.50
        tp1_ceiling = entry + move1 * 0.68
        if after_tp == "TP2" and tp2:
            move2 = max(tp2 - entry, move1)
            floor = max(tp1, entry + move2 * 0.50)
            ceiling = entry + move2 * 0.78
            return round_price(min(max(raw_level, floor), ceiling))
        return round_price(min(max(raw_level, tp1_floor), tp1_ceiling))

    move1 = max(entry - tp1, entry * 0.002)
    tp1_floor = entry - move1 * 0.68   # lower number = tighter for SHORT
    tp1_ceiling = entry - move1 * 0.50 # higher number = looser for SHORT
    if after_tp == "TP2" and tp2:
        move2 = max(entry - tp2, move1)
        floor = entry - move2 * 0.78
        ceiling = min(tp1, entry - move2 * 0.50)
        return round_price(max(min(raw_level, ceiling), floor))
    return round_price(max(min(raw_level, tp1_ceiling), tp1_floor))



def _tp_noise_context_votes(side, context):
    """Small helper for TP noise filters.

    It separates a normal pullback from a real reversal. A stop may be tightened
    aggressively only when there is hard structure/ICT evidence or several soft
    warnings. Otherwise TP1/TP2 locks get more air so one 3M wick does not kick
    the bot out of a valid runner.
    """
    context = context or {}
    opp = opposite(side)
    hard = 0
    soft = 0
    support = 0
    hard_blocks = [context.get("structure") or {}, context.get("ict") or {}, context.get("liquidity") or {}]
    soft_blocks = [context.get("tf3") or {}, context.get("cvd") or {}, context.get("flow") or {}, context.get("news") or {}]
    support_blocks = [context.get("tf15") or {}, context.get("tf1h") or {}, context.get("structure") or {}, context.get("ict") or {}, context.get("cvd") or {}, context.get("flow") or {}]
    for block in hard_blocks:
        if block.get("bias") == opp and abs(int(block.get("score", 0) or 0)) >= 12:
            hard += 1
    for block in soft_blocks:
        if block.get("bias") == opp and abs(int(block.get("score", 0) or 0)) >= 10:
            soft += 1
    for block in support_blocks:
        if block.get("bias") == side:
            support += 1
    return {"hard": hard, "soft": soft, "support": support, "opposite": hard + soft}


def _tp_lock_fraction_profile(side, context, after_tp="TP1"):
    """Return how much of the TP leg should be locked by the first TP stop.

    This is the main TP-noise improvement:
    - TREND runner: lock less aggressively, keep stop outside normal pullback.
    - RANGE/REVERSAL/NEWS/EXHAUSTION: lock faster because giveback is dangerous.
    - Hard reversal evidence overrides the runner mode and allows tighter stop.
    """
    mode_profile = trade_mode_profile(context or {}, side)
    regime = str(mode_profile.get("regime", "NORMAL") or "NORMAL").upper()
    votes = _tp_noise_context_votes(side, context or {})
    hard = votes.get("hard", 0)
    soft = votes.get("soft", 0)
    support = votes.get("support", 0)
    hard_reversal = bool(hard >= 1 or soft >= 3)

    if after_tp == "TP2":
        if hard_reversal:
            return 0.82, "TP2: є підтверджений тиск проти — стоп може бути щільнішим"
        if regime == "TREND" and support >= 2:
            return 0.62, "TP2 TREND_RUNNER: стоп не ставити всередину нормального відкату"
        if regime == "PULLBACK":
            return 0.68, "TP2 PULLBACK: захистити прибуток, але залишити простір"
        if regime in ["RANGE", "REVERSAL", "NEWS_IMPULSE", "IMPULSE"]:
            return 0.76, "TP2 FAST_CAPTURE: ринок може швидко віддати рух"
        return 0.70, "TP2 BALANCED: середній захист без шумового затиску"

    # POST_TP1: softer than TP2. TP1 confirms idea, but TP2 is not reached yet.
    if hard_reversal:
        return 0.70, "TP1: є тиск проти — стоп можна підтягнути швидше"
    if regime == "TREND" and support >= 2:
        return 0.50, "TP1 TREND_RUNNER: перший стоп не душити шумом"
    if regime == "PULLBACK":
        return 0.56, "TP1 PULLBACK: помірний захист"
    if regime in ["RANGE", "REVERSAL", "NEWS_IMPULSE", "IMPULSE"]:
        return 0.64, "TP1 FAST_CAPTURE: перший прибуток захищати швидше"
    return 0.58, "TP1 BALANCED: середній захист"


def _apply_tp_lock_noise_profile(side, clamped_stop, trade, context, after_tp="TP1"):
    """Soften the first TP1/TP2 lock when it sits inside normal pullback noise.

    It never loosens an already active stop by itself; manage_active_trade still
    applies only more-protective stops. This function only prevents a NEW TP lock
    from being calculated too tightly when the market model is still a trend.
    """
    if clamped_stop is None or not trade or not getattr(trade, "entry", None):
        return clamped_stop, ""
    entry = safe_float(trade.entry, 0) or 0
    tp1 = safe_float(getattr(trade, "tp1", 0), 0) or 0
    tp2 = safe_float(getattr(trade, "tp2", 0), 0) or 0
    if not entry or not tp1:
        return clamped_stop, ""

    fraction, reason = _tp_lock_fraction_profile(side, context or {}, after_tp)
    if side == "LONG":
        if after_tp == "TP2" and tp2:
            move = max(tp2 - entry, tp1 - entry, entry * 0.002)
            model_stop = entry + move * fraction
        else:
            move = max(tp1 - entry, entry * 0.002)
            model_stop = entry + move * fraction
        # For LONG, higher stop = tighter. If classic clamp is too tight for the
        # current model, use the softer model stop.
        adjusted = min(safe_float(clamped_stop), model_stop)
    else:
        if after_tp == "TP2" and tp2:
            move = max(entry - tp2, entry - tp1, entry * 0.002)
            model_stop = entry - move * fraction
        else:
            move = max(entry - tp1, entry * 0.002)
            model_stop = entry - move * fraction
        # For SHORT, lower stop = tighter. If classic clamp is too tight for the
        # current model, use the softer model stop.
        adjusted = max(safe_float(clamped_stop), model_stop)
    adjusted = round_price(adjusted)
    if adjusted != round_price(clamped_stop):
        return adjusted, reason
    return round_price(clamped_stop), ""


def _recent_tp_noise_safe_stop(side, proposed, trade, context, stage="POST_TP1", hard_reversal=False, broad_warning=False):
    """Avoid moving dynamic TP stop directly into recent 3M/15M wick noise.

    If there is no confirmed reversal, the stop should sit behind the recent
    pullback high/low plus ATR buffer. If that safe level would not improve the
    current stop, we simply do not move the stop on this run.
    """
    proposed = safe_float(proposed)
    if proposed is None or not trade:
        return proposed, ""
    context = context or {}
    price = safe_float(context.get("price"), 0) or 0
    entry = safe_float(getattr(trade, "entry", 0), 0) or 0
    if not price or not entry:
        return proposed, ""
    # Hard reversal means the market is no longer a normal pullback; allow the
    # tighter stop/exit layer to protect immediately.
    if hard_reversal:
        return round_price(proposed), "hard reversal: шумовий фільтр не розширює стоп"

    candles3 = list(context.get("candles_3m") or [])[-9:]
    candles15 = list(context.get("candles_15m") or [])[-4:]
    candles = candles3 + candles15
    if len(candles) < 4:
        return round_price(proposed), ""

    highs = [safe_float(getattr(c, "high", None)) for c in candles]
    lows = [safe_float(getattr(c, "low", None)) for c in candles]
    highs = [x for x in highs if x]
    lows = [x for x in lows if x]
    if not highs or not lows:
        return round_price(proposed), ""

    ranges_pct = sorted([abs(h - l) / price * 100.0 for h, l in zip(highs, lows) if h and l and h >= l])
    median_range = ranges_pct[len(ranges_pct) // 2] if ranges_pct else 0.22
    atr_value = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or price * 0.006
    atr_pct = atr_value / price * 100.0 if price and atr_value else 0.55
    mode_profile = trade_mode_profile(context, side)
    regime = str(mode_profile.get("regime", "NORMAL") or "NORMAL").upper()

    if stage == "POST_TP2":
        base = 0.18
        atr_mult = 0.30
        cap = 0.46
    else:
        base = 0.22
        atr_mult = 0.36
        cap = 0.54
    if regime == "TREND":
        base += 0.05
    elif regime in ["RANGE", "REVERSAL", "NEWS_IMPULSE", "IMPULSE"]:
        base += 0.02
    if broad_warning:
        # Warning allows a little less air, but still not inside wick-noise.
        base -= 0.03
        cap -= 0.04
    buffer_pct = min(cap, max(base, median_range * 0.80, atr_pct * atr_mult))
    buffer_abs = price * buffer_pct / 100.0

    if side == "LONG":
        safe_stop = min(proposed, min(lows) - buffer_abs)
        # A dynamic long stop must still be below price.
        safe_stop = min(safe_stop, price - buffer_abs)
    else:
        safe_stop = max(proposed, max(highs) + buffer_abs)
        # A dynamic short stop must still be above price.
        safe_stop = max(safe_stop, price + buffer_abs)
    safe_stop = round_price(safe_stop)
    if safe_stop != round_price(proposed):
        return safe_stop, f"TP-noise guard: стоп винесено за останній pullback/wick + ATR buffer (~{round(buffer_pct, 3)}%), щоб не вибило шумом"
    return round_price(proposed), f"TP-noise guard: стоп поза останнім wick-шумом (~{round(buffer_pct, 3)}%)"


def protective_stop_ict_smc(trade, context, after_tp="TP1"):
    """Return a staged protective stop recommendation using ICT + SMC.

    Important management rule for BZU:
    - After TP1, calculate ONE protective stop and lock it until TP2.
      It should be around the middle of entry→TP1, adjusted by ICT/SMC/tech.
      Do not trail directly under every new 3M higher-low/lower-high.
    - After TP2, calculate ONE new tighter stop and lock it until TP3/exit.
    """
    price = context.get("price")
    side = trade.side
    atr_value = context.get("atr15") or (price or trade.entry) * 0.006

    if side == "LONG":
        move1 = max(trade.tp1 - trade.entry, trade.entry * 0.002)
        midpoint = trade.entry + move1 * 0.50
        base_level = midpoint
        base_reason = "TP1 взято: стоп у середині руху entry→TP1, щоб не віддати прибуток і не вибило шумом"
    else:
        move1 = max(trade.entry - trade.tp1, trade.entry * 0.002)
        midpoint = trade.entry - move1 * 0.50
        base_level = midpoint
        base_reason = "TP1 взято: стоп у середині руху entry→TP1, щоб не віддати прибуток і не вибило шумом"

    candidates = [(base_level, base_reason)]
    smc_level, smc_reason = _best_smc_trailing_level(side, context, price, trade.entry, atr_value)
    ict_level, ict_reason = _ict_zone_trailing_level(side, context.get("ict") or {}, price, atr_value)
    if smc_level is not None:
        candidates.append((smc_level, smc_reason))
    if ict_level is not None:
        candidates.append((ict_level, ict_reason))

    if after_tp == "TP2":
        if side == "LONG":
            candidates.append((trade.tp1, "TP2 взято: мінімум захистити TP1"))
        else:
            candidates.append((trade.tp1, "TP2 взято: мінімум захистити TP1"))

    valid = [(lvl, why) for lvl, why in candidates if _valid_stop_for_side(side, price, lvl)]
    if not valid:
        raw_level, why = base_level, base_reason
    elif side == "LONG":
        # Use the most protective valid ICT/SMC level, but then cap it so TP1
        # stop remains around the middle of entry→TP1 instead of right under TP1.
        raw_level, why = max(valid, key=lambda x: x[0])
    else:
        raw_level, why = min(valid, key=lambda x: x[0])

    clamped = _clamp_stop_between_entry_tp(side, raw_level, trade.entry, trade.tp1, trade.tp2, after_tp)
    if clamped != round_price(raw_level):
        why = f"{why}; обмежено базовим TP-lock"

    # TP noise profile: in a clean TREND runner the first TP1/TP2 lock must not
    # be calculated as if every pullback is a reversal. In range/news/reversal it
    # stays tighter. This only affects the newly calculated lock; the caller still
    # never loosens an already active stop.
    noise_adjusted, noise_reason = _apply_tp_lock_noise_profile(side, clamped, trade, context, after_tp)
    if noise_reason:
        clamped = noise_adjusted
        why = f"{why}; {noise_reason}"

    # Final safety: even after TP1/TP2 do not place the stop inside ordinary
    # 3M wick-noise. This keeps the stop behind SMC/ICT structure with ATR air.
    stage = "POST_TP2" if after_tp == "TP2" else "POST_TP1"
    clamped, air_reason = _apply_stop_air(side, clamped, price, trade.entry, context, trade_mode_profile(context, side), stage)
    if air_reason and "відсунуто" in air_reason:
        why = f"{why}; {air_reason}"
    return round_price(clamped), why


def _profit_lock_stop_level(side, entry, price, best_pct, current_pct, profile=None):
    """Regime-aware MFE protective stop for BZU intraday trades.

    The purpose is not to scalp every small pullback. It is to stop the bot from
    giving back most of a real MFE. Protection is stricter in RANGE/REVERSAL,
    moderate in PULLBACK/NORMAL, and softer in TREND/NEWS_IMPULSE so big moves
    still have room to continue.
    """
    profile = profile or {}
    regime = profile.get("regime", "NORMAL")
    be_trigger = safe_float(profile.get("be_trigger"), 0.65) or 0.65
    protect_trigger = safe_float(profile.get("protect_trigger"), 1.05) or 1.05
    if not entry or not price or best_pct < be_trigger or current_pct < 0.12:
        return None, ""

    lock_pct = 0.08
    label = f"{regime}: MFE +{round(best_pct, 2)}% — перший захист прибутку"

    if regime == "RANGE":
        # In balance the market often gives a short move and returns. Capture more.
        if best_pct >= 1.05 and current_pct >= 0.55:
            lock_pct = 0.72
            label = f"{regime}: MFE у боковику — агресивно захистити прибуток"
        elif best_pct >= 0.75 and current_pct >= 0.35:
            lock_pct = 0.52
            label = f"{regime}: MFE у боковику — не віддавати короткий рух"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.30
            label = f"{regime}: перший захист у боковику"
    elif regime == "REVERSAL":
        # Reversal setups often fail after the first push, so protect sooner.
        if best_pct >= 1.05 and current_pct >= 0.55:
            lock_pct = 0.70
            label = f"{regime}: розворотний MFE — захистити більшу частину"
        elif best_pct >= 0.72 and current_pct >= 0.34:
            lock_pct = 0.50
            label = f"{regime}: розворотний MFE — стоп у плюс"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.32
            label = f"{regime}: перший захист розвороту"
    elif regime == "PULLBACK":
        # Pullback in trend: protect profit, but leave room for continuation.
        if best_pct >= 1.30 and current_pct >= 0.78:
            lock_pct = 0.82
            label = f"{regime}: відкат дав MFE — захистити основний рух"
        elif best_pct >= 0.85 and current_pct >= 0.42:
            lock_pct = 0.55
            label = f"{regime}: MFE на відкаті — стоп у плюс"
        elif best_pct >= be_trigger and current_pct >= 0.20:
            lock_pct = 0.34
            label = f"{regime}: перший захист на відкаті"
    elif regime == "TREND":
        # Trend keeps wider room than RANGE, but now captures at least ~60% of MFE
        # when the move is meaningful. This protects cases like +0.8% MFE -> +0.2% exit.
        if best_pct >= 1.70 and current_pct >= 0.95:
            lock_pct = 1.08
            label = f"{regime}: сильний трендовий MFE — захистити 1%+"
        elif best_pct >= 1.15 and current_pct >= 0.62:
            lock_pct = 0.78
            label = f"{regime}: трендовий MFE — стоп у хороший плюс"
        elif best_pct >= 0.72 and current_pct >= 0.30:
            lock_pct = 0.52
            label = f"{regime}: трендовий MFE — не віддавати рух назад"
        elif best_pct >= be_trigger and current_pct >= 0.16:
            lock_pct = 0.34
            label = f"{regime}: перший трендовий захист"
    elif regime == "NEWS_IMPULSE":
        # News needs the most air. Do not over-tighten on normal news volatility.
        if best_pct >= 1.80 and current_pct >= 1.00:
            lock_pct = 0.95
            label = f"{regime}: новинний MFE — захистити прибуток, але дати простір"
        elif best_pct >= 1.10 and current_pct >= 0.55:
            lock_pct = 0.55
            label = f"{regime}: новинний MFE — мʼякий захист"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.28
            label = f"{regime}: перший новинний захист"
    else:
        # NORMAL / IMPULSE / UNKNOWN: medium protection. Stronger than before,
        # especially for LONGs, but not a hard exit on every pullback.
        if best_pct >= protect_trigger + 0.60 and current_pct >= protect_trigger * 0.75:
            lock_pct = 0.78
            label = f"{regime}: сильний MFE — захистити більшу частину руху"
        elif best_pct >= 0.75 and current_pct >= 0.35:
            lock_pct = 0.50
            label = f"{regime}: MFE — захистити прибуток стопом"
        elif best_pct >= be_trigger and current_pct >= 0.18:
            lock_pct = 0.32
            label = f"{regime}: перший захист прибутку"

    # LONG pullbacks on BZU often return faster than shorts. Add a small extra
    # lock only after meaningful MFE; the validity check below prevents putting
    # the stop above current market price.
    if side == "LONG" and best_pct >= 0.70:
        lock_pct += 0.06

    # Never place the protective stop inside ordinary BZU candle noise.
    # Use ATR/regime air. Before TP1 the stop must be wider than after TP1,
    # because there has not yet been a partial fix and BZU often retests.
    temp_context = {"price": price, "atr15": safe_float(profile.get("atr15"), None)}
    if not temp_context.get("atr15"):
        temp_context["atr15"] = price * 0.006
    air_profile = dict(profile or {})
    min_gap_pct = _stop_air_profile(temp_context, air_profile, "PRE_TP1")

    if side == "LONG":
        max_lock_now = current_pct - min_gap_pct
        if max_lock_now <= 0.08:
            return None, ""
        lock_pct = min(lock_pct, max_lock_now)
        level = entry * (1 + lock_pct / 100.0)
        level, air_reason = _apply_stop_air(side, level, price, entry, temp_context, air_profile, "PRE_TP1")
        if level is None or level >= price:
            return None, ""
    else:
        max_lock_now = current_pct - min_gap_pct
        if max_lock_now <= 0.08:
            return None, ""
        lock_pct = min(lock_pct, max_lock_now)
        level = entry * (1 - lock_pct / 100.0)
        level, air_reason = _apply_stop_air(side, level, price, entry, temp_context, air_profile, "PRE_TP1")
        if level is None or level <= price:
            return None, ""
    return round_price(level), label + f"; {air_reason}"

def _stop_update_urgency(side, context=None):
    """Classify whether the market requires a faster protective stop reaction.

    This is event-driven, not time-driven. A new 15-minute run may immediately
    tighten the stop when the market structure changes materially. Ordinary
    noise still needs a meaningful stop improvement before it is accepted.
    """
    context = context or {}
    against = opposite(side)

    hard_votes = 0
    soft_votes = 0

    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    liquidity = context.get("liquidity") or {}
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}

    structure_phase = str(structure.get("phase") or "").upper()
    ict_setup = str(ict.get("setup") or "").upper()
    ict_state = str(ict.get("state") or "").upper()

    if structure.get("bias") == against:
        hard_votes += 1
    if side == "LONG" and any(x in structure_phase for x in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]):
        hard_votes += 1
    if side == "SHORT" and any(x in structure_phase for x in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"]):
        hard_votes += 1

    if ict.get("bias") == against:
        if bool(ict.get("entry_ok")) or ict_state == "ENTRY_MODEL":
            hard_votes += 1
        else:
            soft_votes += 1
    if side == "LONG" and any(x in ict_setup for x in ["SHORT", "BEARISH"]):
        hard_votes += 1
    if side == "SHORT" and any(x in ict_setup for x in ["LONG", "BULLISH"]):
        hard_votes += 1

    if liquidity.get("bias") == against:
        hard_votes += 1

    for block in [tf3, tf15, cvd, flow]:
        if block.get("bias") == against:
            soft_votes += 1

    if hard_votes >= 2 or (hard_votes >= 1 and soft_votes >= 2):
        level = "CRITICAL"
    elif hard_votes >= 1 or soft_votes >= 2:
        level = "ELEVATED"
    else:
        level = "NORMAL"

    return {
        "level": level,
        "hard_votes": hard_votes,
        "soft_votes": soft_votes,
    }


def _stop_update_policy(trade, side, new_stop, context=None, stage="PRE_TP1", force=False):
    """Return whether a proposed stop update is materially useful.

    The old logic accepted every numerically better stop, including tiny
    0.02-0.05 USDT micro-moves. The previous patch added a time cooldown, but a
    fixed pause is unsafe because the market can change sharply on the very next
    supervision run.

    The current policy is fully event-driven:
    - there is no time pause and no cooldown;
    - every run is evaluated immediately;
    - ordinary updates need a meaningful entry/ATR improvement;
    - elevated or critical reversal pressure lowers the threshold immediately;
    - TP1/TP2 locks and the first move from risk to breakeven/profit bypass the
      threshold;
    - the stop is never loosened.
    """
    if new_stop is None or side not in ["LONG", "SHORT"]:
        return {"apply": False, "reason": "invalid"}

    old_stop = safe_float(getattr(trade, "stop_current", None), None)
    proposed = safe_float(new_stop, None)
    entry = safe_float(getattr(trade, "entry", None), None)
    if old_stop is None or proposed is None or not entry:
        return {"apply": False, "reason": "missing_values"}

    improvement = (proposed - old_stop) if side == "LONG" else (old_stop - proposed)
    if improvement <= 0:
        return {"apply": False, "reason": "not_more_protective"}

    context = context or {}
    atr15 = safe_float(context.get("atr15"), None)
    if not atr15:
        atr15 = safe_float((context.get("tf15") or {}).get("atr"), None)
    if not atr15:
        atr15 = entry * 0.0045

    stage_name = str(stage or "PRE_TP1").upper()
    if "TP2" in stage_name:
        min_pct, atr_mult = 0.14, 0.42
    elif "TP1" in stage_name and "PRE" not in stage_name:
        min_pct, atr_mult = 0.12, 0.38
    else:
        min_pct, atr_mult = 0.10, 0.32

    base_minimum = max(entry * min_pct / 100.0, atr15 * atr_mult)
    urgency = _stop_update_urgency(side, context)
    urgency_level = urgency.get("level", "NORMAL")

    # A confirmed market deterioration must be able to react on the next run,
    # but even a critical state should not generate meaningless micro-trailing.
    if urgency_level == "CRITICAL":
        urgency_factor = 0.35
    elif urgency_level == "ELEVATED":
        urgency_factor = 0.65
    else:
        urgency_factor = 1.0

    absolute_noise_floor = max(entry * 0.035 / 100.0, atr15 * 0.12)
    min_improvement = max(absolute_noise_floor, base_minimum * urgency_factor)

    # First transition from loss-side stop to breakeven/profit is important even
    # when the numeric delta is smaller than a later trailing step.
    crosses_risk_floor = bool(
        (side == "LONG" and old_stop < entry and proposed >= entry * 0.9995)
        or (side == "SHORT" and old_stop > entry and proposed <= entry * 1.0005)
    )

    if not (force or crosses_risk_floor) and improvement < min_improvement:
        return {
            "apply": False,
            "reason": "below_dynamic_minimum",
            "improvement": improvement,
            "minimum": min_improvement,
            "base_minimum": base_minimum,
            "urgency": urgency_level,
            "hard_votes": urgency.get("hard_votes", 0),
            "soft_votes": urgency.get("soft_votes", 0),
        }

    return {
        "apply": True,
        "improvement": improvement,
        "minimum": min_improvement,
        "base_minimum": base_minimum,
        "urgency": urgency_level,
        "hard_votes": urgency.get("hard_votes", 0),
        "soft_votes": urgency.get("soft_votes", 0),
        "force": bool(force or crosses_risk_floor),
    }

def _sync_two_level_stops(trade, side=None):
    """Synchronize structural and profit stops without ever loosening risk."""
    side = side or getattr(trade, "side", "")
    structural = safe_float(getattr(trade, "structural_stop", 0), 0.0) or safe_float(getattr(trade, "stop_initial", 0), 0.0) or 0.0
    current = safe_float(getattr(trade, "stop_current", 0), 0.0) or structural
    profit = safe_float(getattr(trade, "profit_stop", 0), 0.0) or 0.0
    trade.structural_stop = float(structural)

    # Legacy compatibility: a previously tightened stop must survive migration.
    if side == "LONG" and current > structural and (profit <= 0 or current > profit):
        profit = current
    elif side == "SHORT" and current < structural and current > 0 and (profit <= 0 or current < profit):
        profit = current

    valid_profit = profit > 0 and (
        (side == "LONG" and profit >= structural)
        or (side == "SHORT" and profit <= structural)
    )
    if valid_profit:
        active = max(structural, profit) if side == "LONG" else min(structural, profit)
        trade.profit_stop = float(profit)
        trade.active_stop_source = "PROFIT" if abs(active - profit) < 1e-9 else "STRUCTURAL"
    else:
        active = structural
        trade.profit_stop = 0.0
        trade.active_stop_source = "STRUCTURAL"
    trade.stop_current = float(active)
    return float(active)


def _set_profit_stop_level(trade, side, stop, update_time=False):
    stop = safe_float(stop, None)
    if stop is None or stop <= 0:
        return False
    structural = safe_float(getattr(trade, "structural_stop", 0), 0.0) or safe_float(getattr(trade, "stop_initial", 0), 0.0) or 0.0
    current_profit = safe_float(getattr(trade, "profit_stop", 0), 0.0) or 0.0
    if side == "LONG":
        if stop < structural:
            return False
        trade.profit_stop = float(max(current_profit, stop)) if current_profit > 0 else float(stop)
    elif side == "SHORT":
        if stop > structural:
            return False
        trade.profit_stop = float(min(current_profit, stop)) if current_profit > 0 else float(stop)
    else:
        return False
    _sync_two_level_stops(trade, side)
    if update_time:
        trade.stop_updated_at = iso_now()
    return True


def _apply_more_protective_stop(trade, side, new_stop, context=None, stage="PRE_TP1", force=False):
    """Apply a material profit-protection stop; structural invalidation stays intact."""
    _sync_two_level_stops(trade, side)
    policy = _stop_update_policy(trade, side, new_stop, context=context, stage=stage, force=force)
    if not policy.get("apply"):
        return False
    if not _set_profit_stop_level(trade, side, new_stop, update_time=True):
        return False
    return True


def _mfe_exit_floor(best_pct, market_regime):
    """Minimum acceptable captured MFE before closing/protecting a profitable trade.

    This is the regime-aware MFE Giveback Guard:
    - TREND: can give back about 40% of MFE, capture ~60%.
    - RANGE: can give back about 20%, capture ~80%.
    - PULLBACK/NORMAL: can give back about 30–35%.
    - REVERSAL: can give back about 25%.
    - NEWS_IMPULSE: can give back about 50%, because news moves are noisy.
    """
    best_pct = safe_float(best_pct, 0.0) or 0.0
    if best_pct <= 0:
        return 0.0

    allowed_giveback = {
        "TREND": 0.40,
        "RANGE": 0.20,
        "PULLBACK": 0.30,
        "REVERSAL": 0.25,
        "NEWS_IMPULSE": 0.50,
        "IMPULSE": 0.35,
        "NORMAL": 0.35,
        "UNKNOWN": 0.35,
    }.get(market_regime, 0.35)

    capture_ratio = 1.0 - allowed_giveback
    # For tiny MFE do not force unrealistic exits. Once MFE is meaningful,
    # the ratio becomes the main rule.
    min_absolute = 0.18
    if market_regime == "RANGE":
        min_absolute = 0.22
    elif market_regime == "TREND":
        min_absolute = 0.28
    elif market_regime == "NEWS_IMPULSE":
        min_absolute = 0.20
    elif market_regime == "REVERSAL":
        min_absolute = 0.22

    return max(min_absolute, best_pct * capture_ratio)


def _adaptive_mfe_guard_snapshot(trade, context, market_regime, current_pct, best_pct, giveback,
                                 giveback_ratio, post_tp1, tf3_against=False,
                                 structure_against=False, ict_against=False,
                                 flow_against=False, cvd_against=False,
                                 liquidity_against=False, news_against=False,
                                 confirmed_ict_reversal=False, support_votes=0.0,
                                 opposite_votes=0.0):
    """Adaptive MFE Giveback Guard 2.0.

    Protect real profit without killing good trends:
    - before TP1: mostly protect with stop, not panic close;
    - after TP1 / MFE > 2%: giveback threshold is stricter;
    - close after two confirmed warnings, except hard ICT/SMC reversal;
    - valid trend gets PROTECT first, not immediate close.
    """
    best_pct = safe_float(best_pct, 0.0) or 0.0
    current_pct = safe_float(current_pct, 0.0) or 0.0
    giveback = safe_float(giveback, 0.0) or 0.0
    giveback_ratio = safe_float(giveback_ratio, 0.0) or 0.0
    regime = str(market_regime or "NORMAL").upper()

    if best_pct < 0.45 or giveback <= 0:
        trade.mfe_giveback_streak = 0
        trade.mfe_giveback_last_state = "OK"
        return {"active": False, "streak": 0, "severity": "OK"}

    tp1_distance_pct = abs(pct(trade.tp1, trade.entry)) if getattr(trade, "entry", 0) else 0.0
    reached_tp1_zone = bool(post_tp1 or (tp1_distance_pct and best_pct >= tp1_distance_pct * 0.92))
    big_mfe = best_pct >= 2.0
    meaningful_mfe = best_pct >= max(0.65, tp1_distance_pct * 0.70 if tp1_distance_pct else 0.65)

    if not reached_tp1_zone and not big_mfe:
        allowed_giveback = 0.68
        stage = "PRE_TP1"
    elif getattr(trade, "tp2_hit", False):
        allowed_giveback = 0.45
        stage = "POST_TP2"
    elif reached_tp1_zone:
        allowed_giveback = 0.60
        stage = "POST_TP1"
    else:
        allowed_giveback = 0.55
        stage = "MFE"

    if big_mfe:
        allowed_giveback = min(allowed_giveback, 0.35)
        stage = "BIG_MFE"

    if regime == "TREND":
        allowed_giveback += 0.08
    elif regime in ["RANGE", "REVERSAL"]:
        allowed_giveback -= 0.08
    elif regime == "NEWS_IMPULSE":
        allowed_giveback += 0.08
    allowed_giveback = clamp(allowed_giveback, 0.28, 0.72)

    hard_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
    soft_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
    warning_score = hard_votes * 2 + soft_votes
    trend_still_valid = bool(
        regime == "TREND"
        and (support_votes >= 2.0 or (context.get("tf15") or {}).get("bias") == trade.side or context.get("bias") == trade.side)
        and not (structure_against or ict_against or confirmed_ict_reversal)
    )

    triggered = bool(meaningful_mfe and giveback_ratio >= allowed_giveback)
    if not triggered:
        trade.mfe_giveback_streak = max(0, int(getattr(trade, "mfe_giveback_streak", 0) or 0) - 1)
        trade.mfe_giveback_last_state = "COOLING" if trade.mfe_giveback_streak else "OK"
        return {
            "active": False,
            "streak": trade.mfe_giveback_streak,
            "severity": trade.mfe_giveback_last_state,
            "allowed_giveback": round(allowed_giveback, 3),
            "stage": stage,
        }

    trade.mfe_giveback_streak = int(getattr(trade, "mfe_giveback_streak", 0) or 0) + 1
    hard_reversal = bool(confirmed_ict_reversal or hard_votes >= 1)
    min_capture = _mfe_exit_floor(best_pct, regime)
    close_now = bool(
        hard_reversal
        or (
            trade.mfe_giveback_streak >= 2
            and warning_score >= 3
            and not trend_still_valid
            and (post_tp1 or big_mfe or current_pct <= min_capture)
        )
        or (big_mfe and trade.mfe_giveback_streak >= 2 and warning_score >= 2 and current_pct <= min_capture)
    )
    trade.mfe_giveback_last_state = "CLOSE" if close_now else "PROTECT"

    reason_bits = [
        f"MFE було +{round(best_pct, 3)}%, зараз {round(current_pct, 3)}%",
        f"віддано {round(giveback_ratio * 100, 1)}% руху; ліміт {round(allowed_giveback * 100, 1)}%",
        f"перевірка {trade.mfe_giveback_streak}/2",
    ]
    if hard_reversal:
        reason_bits.append("ICT/SMC злам підтверджений")
    elif trend_still_valid:
        reason_bits.append("тренд ще живий — спершу захист, не панічний вихід")

    return {
        "active": True,
        "close": close_now,
        "protect": not close_now,
        "stage": stage,
        "streak": trade.mfe_giveback_streak,
        "allowed_giveback": round(allowed_giveback, 3),
        "warning_score": warning_score,
        "hard_reversal": hard_reversal,
        "trend_still_valid": trend_still_valid,
        "reason": "; ".join(reason_bits),
        "reasons": reason_bits,
    }

def active_trade_message_key(trade, action):
    return f"{trade.id}:{action}:{trade.tp1_hit}:{trade.tp2_hit}:{trade.tp3_hit}:{trade.tp1_stop_locked}:{trade.tp2_stop_locked}:{round_price(trade.stop_current)}"


def _has_confirmed_ict_reversal(side, context):
    """True only when the opposite side has real ICT/SMC reversal evidence."""
    opp = opposite(side)
    structure = (context or {}).get("structure") or {}
    ict = (context or {}).get("ict") or {}
    liquidity = (context or {}).get("liquidity") or {}
    phase = str(structure.get("phase", "")).upper()
    ict_setup = str(ict.get("setup", "")).upper()
    liq_event = str(liquidity.get("event", "")).upper()
    if opp == "LONG":
        choch = "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase
        sweep = liquidity.get("bias") == "LONG" and any(x in liq_event for x in ["SWEEP", "RECLAIM"])
        model = ict.get("bias") == "LONG" and (ict.get("entry_ok") or any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]))
    else:
        choch = "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase
        sweep = liquidity.get("bias") == "SHORT" and any(x in liq_event for x in ["SWEEP", "RECLAIM"])
        model = ict.get("bias") == "SHORT" and (ict.get("entry_ok") or any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]))
    components = int(bool(choch)) + int(bool(sweep)) + int(bool(model))
    return components >= 2, components

def _should_apply_pre_tp_profit_stop(trade, context, proposed_stop, best_pct, current_pct, market_regime):
    """Avoid moving pre-TP1 stops on every small MFE."""
    if proposed_stop is None or trade is None:
        return False, ""
    if getattr(trade, "tp1_hit", False):
        return True, "після TP1 стоп керується TP-lock логікою"
    price = safe_float((context or {}).get("price"), 0.0) or 0.0
    if not price or not trade.entry:
        return False, "немає ціни для перевірки стопу"
    atr_value = safe_float((context or {}).get("atr15"), price * 0.006) or price * 0.006
    min_gap_pct = _stop_air_profile(context, trade_mode_profile(context, trade.side), "PRE_TP1")
    gap_pct = ((price - proposed_stop) / trade.entry * 100.0) if trade.side == "LONG" else ((proposed_stop - price) / trade.entry * 100.0)
    if gap_pct < min_gap_pct:
        return False, f"стоп занадто близько до ціни до TP1 ({round(gap_pct, 3)}% < {round(min_gap_pct, 3)}%)"
    smc_level, _ = _last_smc_trailing_level(trade.side, (context or {}).get("candles_3m") or [], price, trade.entry, atr_value)
    ict_level, _ = _ict_zone_trailing_level(trade.side, (context or {}).get("ict") or {}, price, atr_value)
    has_structure_anchor = smc_level is not None or ict_level is not None
    if best_pct >= 1.05 and current_pct >= 0.45:
        return True, "MFE достатній для першого захисту до TP1"
    if has_structure_anchor and best_pct >= 0.65 and current_pct >= 0.25:
        return True, "є SMC/ICT опора для стопу до TP1"
    return False, "до TP1 стоп не рухати без нового swing/ICT або достатнього MFE"


def _trade_notes_text(trade):
    return " | ".join(str(x) for x in (getattr(trade, "notes", None) or [])).upper()


def _is_risky_pre_tp1_guard_trade(trade):
    """Trades where pre-TP1 profit must be protected faster.

    Applies to the exact cases that tend to give a quick impulse and then return:
    RISKY_ENTRY, NEWS_SHOCK/NEWS_IMPULSE and early ignition setups.
    """
    notes = _trade_notes_text(trade)
    risky_entry = "RISKY_ENTRY" in notes or "ENTRY_LEVEL: RISKY_ENTRY" in notes
    early_setup = any(x in notes for x in [
        "SETUP_CLASSIFIER: TREND_IGNITION_ENTRY",
        "SETUP_CLASSIFIER: NEWS_IMPULSE",
        "SETUP_CLASSIFIER: SWEEP_RECLAIM_EARLY_ENTRY",
        "SETUP_CLASSIFIER: COUNTERTREND_SCALP",
    ])
    shock_regime = any(x in notes for x in [
        "REGIME_TYPE: NEWS_SHOCK",
        "REGIME_TYPE: NEWS_IMPULSE",
        "REGIME_TYPE: EXHAUSTION",
    ])
    return bool(risky_entry or early_setup or shock_regime)


def _context_regime_type(context):
    """Current regime label used by protective stop logic."""
    regime_engine = (context or {}).get("regime_engine") or {}
    if isinstance(regime_engine, dict):
        regime_type = str(regime_engine.get("regime_type") or regime_engine.get("name") or "").upper()
        name = str(regime_engine.get("name") or "").upper()
        if regime_type:
            return regime_type, name
    market_regime = (context or {}).get("market_regime") or {}
    if isinstance(market_regime, dict):
        return str(market_regime.get("type") or market_regime.get("name") or "").upper(), str(market_regime.get("name") or "").upper()
    return "", ""


def _pre_tp1_guard_profile(trade, context, side, best_pct, current_pct, support_votes, opposite_votes,
                           hard_votes, soft_votes, entry_weak=False, confirmed_ict_reversal=False):
    """Market-model profile for the RISKY pre-TP1 profit guard.

    The same +0.40% MFE should not be managed identically in every market:
    - NEWS / REVERSAL / RANGE: protect faster because reversals are abrupt.
    - NORMAL / PULLBACK: balanced protection.
    - TREND_IGNITION + confirmed TREND_EXPANSION: give the trade more air,
      because early BE+ can close the first position right before continuation.
    """
    notes = _trade_notes_text(trade)
    regime_type, regime_name = _context_regime_type(context)
    setup_trend_ignition = "SETUP_CLASSIFIER: TREND_IGNITION_ENTRY" in notes
    setup_news = "SETUP_CLASSIFIER: NEWS_IMPULSE" in notes
    entry_news = any(x in notes for x in ["REGIME_TYPE: NEWS_SHOCK", "REGIME_TYPE: NEWS_IMPULSE"]) or setup_news
    entry_exhaustion = "REGIME_TYPE: EXHAUSTION" in notes or regime_type == "EXHAUSTION"
    entry_reversal = any(x in notes for x in ["SETUP_CLASSIFIER: COUNTERTREND_SCALP", "SETUP_CLASSIFIER: SWEEP_REVERSAL", "REGIME_TYPE: REVERSAL"])
    current_trend = regime_type == "TREND_EXPANSION" or regime_name == "TREND"
    current_range = "RANGE" in regime_type or regime_name == "RANGE"
    current_news = regime_type in ["NEWS_SHOCK", "NEWS_IMPULSE"] or regime_name == "NEWS_IMPULSE"

    tf15_same = ((context or {}).get("tf15") or {}).get("bias") == side
    tf1h_same = ((context or {}).get("tf1h") or {}).get("bias") == side
    tf4h_same = ((context or {}).get("tf4h") or {}).get("bias") == side
    context_same = (context or {}).get("bias") == side
    total_score = abs(int((context or {}).get("total_score", 0) or 0))
    htf_support_count = int(bool(tf15_same)) + int(bool(tf1h_same)) + int(bool(tf4h_same)) + int(bool(context_same))
    trend_support = bool(
        support_votes >= max(2.0, opposite_votes + 0.65)
        or htf_support_count >= 2
        or (context_same and total_score >= 75)
    )

    tp1_distance_pct = abs(pct(getattr(trade, "tp1", 0), getattr(trade, "entry", 0))) if getattr(trade, "entry", 0) else 0.0
    progress_to_tp1 = best_pct / tp1_distance_pct if tp1_distance_pct > 0.05 else 0.0

    strong_trend_ignition = bool(
        setup_trend_ignition
        and current_trend
        and trend_support
        and not entry_news
        and not entry_exhaustion
        and hard_votes == 0
        and not confirmed_ict_reversal
    )

    if strong_trend_ignition:
        min_best = max(0.70, tp1_distance_pct * 0.62 if tp1_distance_pct else 0.70)
        min_current = 0.36
        min_gap = 0.24
        close_giveback = 0.74
        model = "TREND_IGNITION_RUNNER"
        reason = "сильний TREND_IGNITION у TREND_EXPANSION: не душити ранній шорт/лонг BE-стопом на малому MFE"
    elif entry_news or current_news:
        min_best = 0.40
        min_current = 0.20
        min_gap = 0.16
        close_giveback = 0.58
        model = "NEWS_FAST_PROTECT"
        reason = "новинний імпульс: прибуток захищати швидше"
    elif entry_reversal or current_range:
        min_best = 0.40
        min_current = 0.18
        min_gap = 0.14
        close_giveback = 0.55
        model = "RANGE_REVERSAL_FAST_PROTECT"
        reason = "range/reversal: не давати MFE швидко повернутись у мінус"
    elif entry_exhaustion:
        min_best = 0.40
        min_current = 0.16
        min_gap = 0.18
        close_giveback = 0.54
        model = "EXHAUSTION_DEFENSIVE"
        reason = "виснажений рух: захист агресивніший"
    else:
        min_best = 0.45
        min_current = 0.22
        min_gap = 0.16
        close_giveback = 0.62
        model = "BALANCED_PROTECT"
        reason = "звичайний ранній/ризиковий вхід: збалансований захист"

    if hard_votes >= 1 or entry_weak:
        min_best = min(min_best, 0.40)
        min_current = min(min_current, 0.14)
        close_giveback = min(close_giveback, 0.55)
        model += "_WEAK_EDGE"
        reason += "; є злам/слабкість — захист не відкладати"
    elif soft_votes >= 2 and not strong_trend_ignition:
        min_best = min(min_best, 0.40)
        min_current = min(min_current, 0.18)
        close_giveback = min(close_giveback, 0.58)

    # Risk Floor після MFE: пом'якшення для trend runner не повинно дозволяти
    # плюсовій до TP1 угоді перейти у нормальний мінус. Для живого тренду даємо
    # трохи більше повітря, для новини/range/reversal — вихід ближче до нуля.
    if strong_trend_ignition:
        risk_floor_pct = -0.10
        near_zero_floor_pct = 0.02
    elif entry_news or current_news or entry_reversal or current_range or entry_exhaustion:
        risk_floor_pct = -0.06
        near_zero_floor_pct = 0.04
    else:
        risk_floor_pct = -0.08
        near_zero_floor_pct = 0.03

    return {
        "model": model,
        "reason": reason,
        "min_best": round(float(min_best), 4),
        "min_current": round(float(min_current), 4),
        "min_gap": round(float(min_gap), 4),
        "close_giveback": round(float(close_giveback), 4),
        "risk_floor_pct": round(float(risk_floor_pct), 4),
        "near_zero_floor_pct": round(float(near_zero_floor_pct), 4),
        "strong_trend_ignition": strong_trend_ignition,
        "tp1_distance_pct": round(float(tp1_distance_pct), 4),
        "progress_to_tp1": round(float(progress_to_tp1), 4),
        "trend_support": trend_support,
        "current_regime_type": regime_type,
    }


def _near_entry_lock_stop(side, entry, price, lock_pct, min_gap_pct=0.12):
    """Small breakeven-plus stop used only by the pre-TP1 risky profit guard."""
    entry = safe_float(entry, 0.0) or 0.0
    price = safe_float(price, 0.0) or 0.0
    lock_pct = max(0.0, safe_float(lock_pct, 0.0) or 0.0)
    min_gap_pct = max(0.06, safe_float(min_gap_pct, 0.12) or 0.12)
    if not entry or not price or lock_pct <= 0:
        return None

    max_lock_now = safe_float(signed_pct(side, entry, price), 0.0) - min_gap_pct
    if max_lock_now < 0.035:
        return None
    lock_pct = min(lock_pct, max_lock_now)

    if side == "LONG":
        stop = entry * (1.0 + lock_pct / 100.0)
    else:
        stop = entry * (1.0 - lock_pct / 100.0)
    stop = round_price(stop)
    return stop if _valid_stop_for_side(side, price, stop) else None


def risky_pre_tp1_profit_guard_snapshot(trade, context, current_pct, best_pct, giveback, giveback_ratio,
                                        support_votes=0.0, opposite_votes=0.0,
                                        tf3_against=False, structure_against=False, ict_against=False,
                                        flow_against=False, cvd_against=False, liquidity_against=False,
                                        news_against=False, confirmed_ict_reversal=False,
                                        entry_state=None):
    """Professional pre-TP1 protection for RISKY / NEWS / ignition entries.

    Decision logic:
    1) If MFE is only noise -> do nothing.
    2) If MFE >= 0.40% and trade is still working -> move stop near BE+.
    3) If most of that MFE is given back and warnings appear -> exit near entry.

    This prevents a RISKY_ENTRY that already gave +0.40-0.60% from becoming a
    full negative trade when TP1 is intentionally farther away.
    """
    if not trade or getattr(trade, "tp1_hit", False):
        return {"active": False}
    if not _is_risky_pre_tp1_guard_trade(trade):
        return {"active": False}

    best_pct = safe_float(best_pct, 0.0) or 0.0
    current_pct = safe_float(current_pct, 0.0) or 0.0
    giveback = safe_float(giveback, max(0.0, best_pct - current_pct)) or 0.0
    giveback_ratio = safe_float(giveback_ratio, (giveback / best_pct if best_pct > 0.1 else 0.0)) or 0.0
    if best_pct < 0.40:
        return {"active": False}

    side = getattr(trade, "side", "NEUTRAL")
    price = safe_float((context or {}).get("price"), 0.0) or 0.0
    hard_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
    soft_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
    entry_state_name = str((entry_state or {}).get("state") or "").upper()
    entry_weak = entry_state_name in ["WEAK", "BROKEN"]
    support_ok = bool(support_votes >= max(1.8, opposite_votes + 0.30) or ((context or {}).get("tf15") or {}).get("bias") == side)
    guard_profile = _pre_tp1_guard_profile(
        trade, context, side, best_pct, current_pct, support_votes, opposite_votes,
        hard_votes, soft_votes, entry_weak=entry_weak, confirmed_ict_reversal=confirmed_ict_reversal,
    )
    min_best_for_guard = safe_float(guard_profile.get("min_best"), 0.40) or 0.40
    min_current_for_guard = safe_float(guard_profile.get("min_current"), 0.20) or 0.20
    close_giveback_limit = safe_float(guard_profile.get("close_giveback"), 0.62) or 0.62
    risk_floor_pct = safe_float(guard_profile.get("risk_floor_pct"), -0.08) or -0.08
    near_zero_floor_pct = safe_float(guard_profile.get("near_zero_floor_pct"), 0.03) or 0.03

    # Hard Safety Risk Floor: якщо рання/ризикова угода вже мала MFE >= 0.40%,
    # пом'якшення trend-runner не має права дозволити їй перейти у нормальний мінус.
    # Це не рухає стоп і не конфліктує з TP1/TP2 locks: працює тільки до TP1.
    risk_floor_broken = bool(current_pct <= risk_floor_pct)
    near_zero_after_giveback = bool(
        current_pct <= near_zero_floor_pct
        and giveback_ratio >= min(0.82, max(0.62, close_giveback_limit + 0.10))
    )
    weak_near_zero_after_mfe = bool(
        current_pct <= max(0.06, near_zero_floor_pct)
        and (hard_votes >= 1 or soft_votes >= 2 or entry_weak)
        and giveback_ratio >= 0.50
    )
    if risk_floor_broken or near_zero_after_giveback or weak_near_zero_after_mfe:
        floor_reason = "risk floor після MFE" if risk_floor_broken else "майже весь MFE віддано біля входу"
        return {
            "active": True,
            "close": True,
            "action": "EXIT_RISKY_PRE_TP1_RISK_FLOOR",
            "title": f"{side} ЗАКРИТИ — RISK FLOOR ПІСЛЯ MFE",
            "recommendation": "угода вже давала плюс до TP1, але повернулась до входу/в мінус: закрити біля нуля, не чекати дальній стоп",
            "reason": f"{guard_profile.get('model')}: {floor_reason}; MFE +{round(best_pct, 3)}%, зараз {round(current_pct, 3)}%, floor {round(risk_floor_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}%",
            "notes": [
                f"MFE було +{round(best_pct, 3)}%, TP1 ще не взято",
                f"risk floor: {round(risk_floor_pct, 3)}% | near-zero floor: {round(near_zero_floor_pct, 3)}%",
                f"модель захисту: {guard_profile.get('model')}",
                "пом'якшення тренду не дозволяє перетворити плюс у мінус",
                f"hard/soft warnings: {hard_votes}/{soft_votes}",
            ],
        }

    if best_pct < min_best_for_guard:
        return {
            "active": bool(best_pct >= 0.40),
            "protect": False,
            "reason": f"{guard_profile.get('model')}: MFE +{round(best_pct, 3)}% ще замалий для стопу; risk floor активний до ≥ {round(min_best_for_guard, 3)}%",
            "notes": [
                str(guard_profile.get("reason") or ""),
                f"MFE +{round(best_pct, 3)}%, до TP1 прогрес {round((guard_profile.get('progress_to_tp1') or 0) * 100, 1)}%",
                f"risk floor не дасть угоді піти гірше {round(risk_floor_pct, 3)}% після MFE",
            ],
        }

    # Exit near BE instead of letting a RISKY/NEWS/ignition trade fall into loss.
    close_near_entry = bool(
        current_pct <= 0.08
        and (hard_votes >= 1 or soft_votes >= 2 or entry_weak or giveback_ratio >= close_giveback_limit)
        and not (support_ok and guard_profile.get("strong_trend_ignition"))
    )
    close_negative_after_mfe = bool(
        current_pct <= -0.03
        and (hard_votes >= 1 or soft_votes >= 1 or entry_weak or giveback_ratio >= min(0.55, close_giveback_limit))
    )
    if close_near_entry or close_negative_after_mfe:
        return {
            "active": True,
            "close": True,
            "action": "EXIT_RISKY_PRE_TP1_BE_GUARD",
            "title": f"{side} ЗАКРИТИ — РИЗИКОВИЙ ВХІД ВІДДАВ MFE",
            "recommendation": "угода вже давала хороший плюс до TP1, але продовження не підтвердилось: закрити біля входу, не чекати дальній стоп",
            "reason": f"RISKY/NEWS Guard: MFE +{round(best_pct, 3)}%, зараз {round(current_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}%",
            "notes": [
                f"MFE було +{round(best_pct, 3)}%, TP1 ще не взято",
                "для ризикового/новинного раннього входу не віддавати рух назад у мінус",
                f"hard/soft warnings: {hard_votes}/{soft_votes}",
            ],
        }

    # Decide how much to lock. This is intentionally small: BE+ first, not TP1 trailing.
    # In confirmed TREND_IGNITION + TREND_EXPANSION we wait for larger progress
    # toward TP1, so the first entry is not closed right before continuation.
    if current_pct < min_current_for_guard and not (soft_votes >= 1 or hard_votes >= 1 or giveback_ratio >= 0.45):
        return {
            "active": True,
            "protect": False,
            "reason": f"{guard_profile.get('model')}: MFE є, але поточного плюса замало для валідного стопу",
            "notes": [str(guard_profile.get("reason") or "")],
        }

    if guard_profile.get("strong_trend_ignition"):
        if best_pct >= max(0.95, (guard_profile.get("tp1_distance_pct") or 0.0) * 0.78) and current_pct >= 0.50:
            desired_lock = 0.14
            mode = "TREND_RUNNER_PROTECT"
        elif best_pct >= min_best_for_guard and current_pct >= min_current_for_guard:
            desired_lock = 0.065
            mode = "TREND_SOFT_BE"
        elif soft_votes >= 1 or giveback_ratio >= 0.48:
            desired_lock = 0.04
            mode = "TREND_DEFENSIVE_BE"
        else:
            return {"active": True, "protect": False, "reason": "TREND_IGNITION ще живий: стоп до TP1 не рухати без більшого MFE або слабкості"}
    elif best_pct >= 0.85 and current_pct >= 0.50 and soft_votes == 0 and hard_votes == 0:
        desired_lock = 0.22
        mode = "RUNNER_PROTECT"
    elif best_pct >= 0.60 and current_pct >= 0.36:
        desired_lock = 0.14
        mode = "MFE_PROTECT"
    elif current_pct >= max(0.24, min_current_for_guard):
        desired_lock = 0.08
        mode = "BREAKEVEN_PLUS"
    elif current_pct >= min(0.16, min_current_for_guard) and (soft_votes >= 1 or giveback_ratio >= 0.35):
        desired_lock = 0.045
        mode = "DEFENSIVE_BE"
    else:
        return {"active": True, "protect": False, "reason": "MFE є, але ще мало простору для валідного BE+ стопу"}

    # News / early ignition gets a little more room from the current price, but still protects BE+.
    notes_text = _trade_notes_text(trade)
    min_gap = safe_float(guard_profile.get("min_gap"), 0.16) or 0.16
    stop = _near_entry_lock_stop(side, getattr(trade, "entry", 0.0), price, desired_lock, min_gap_pct=min_gap)
    if stop is None:
        if current_pct <= 0.12 and (soft_votes >= 1 or hard_votes >= 1 or giveback_ratio >= 0.45):
            return {
                "active": True,
                "close": True,
                "action": "EXIT_RISKY_PRE_TP1_BE_GUARD",
                "title": f"{side} ЗАКРИТИ — НЕМАЄ МІСЦЯ ДЛЯ BE-СТОПУ",
                "recommendation": "ціна вже близько до входу: стоп біля входу технічно запізнився, краще закрити біля нуля",
                "reason": f"MFE +{round(best_pct, 3)}%, зараз {round(current_pct, 3)}%, стоп BE+ вже занадто близько",
                "notes": ["RISKY/NEWS Guard: стоп біля входу не має достатнього зазору від ціни"],
            }
        return {"active": True, "protect": False, "reason": "стоп BE+ занадто близько до поточної ціни"}

    return {
        "active": True,
        "protect": True,
        "mode": mode,
        "stop": stop,
        "reason": f"{guard_profile.get('model')}: MFE +{round(best_pct, 3)}%, TP1 ще не взято — стоп біля входу",
        "notes": [
            f"MFE було +{round(best_pct, 3)}%, TP1 ще не взято",
            f"модель захисту: {guard_profile.get('model')}",
            str(guard_profile.get("reason") or ""),
            f"режим захисту: {mode}",
            f"стоп біля входу: {round_price(stop)}",
        ],
    }



def _post_tp1_dynamic_profit_manager_snapshot(trade, context, current_pct, best_pct, giveback, giveback_ratio,
                                             support_votes=0.0, opposite_votes=0.0,
                                             tf3_against=False, structure_against=False, ict_against=False,
                                             flow_against=False, cvd_against=False,
                                             liquidity_against=False, news_against=False,
                                             confirmed_ict_reversal=False, entry_state=None,
                                             action="HOLD"):
    """Professional dynamic protection after TP1 and before TP2.

    TP1 is the first confirmation that the idea worked. The classic bot logic
    locked one ICT/SMC stop and kept it unchanged until TP2 to avoid being
    shaken out by ordinary BZU 3M noise. That is good for clean trends, but it
    can be too passive when price travels far beyond TP1, almost reaches TP2,
    then starts giving the move back.

    This layer works only between TP1 and TP2. It never loosens the stop, never
    touches pre-TP1 Risk Floor, and stops working as soon as TP2 is hit. Its job:
    - keep the first TP1 lock if the trend is still clean;
    - tighten the stop only after meaningful progress beyond TP1 or warnings;
    - close only on confirmed reversal / deep giveback near the protective stop.
    """
    if (
        not trade
        or not getattr(trade, "tp1_hit", False)
        or getattr(trade, "tp2_hit", False)
        or getattr(trade, "tp3_hit", False)
    ):
        return {"active": False}

    side = getattr(trade, "side", "NEUTRAL")
    entry = safe_float(getattr(trade, "entry", 0.0), 0.0) or 0.0
    price = safe_float((context or {}).get("price"), 0.0) or 0.0
    if side not in ["LONG", "SHORT"] or not entry or not price:
        return {"active": False}

    current_pct = safe_float(current_pct, 0.0) or 0.0
    best_pct = safe_float(best_pct, 0.0) or 0.0
    giveback = safe_float(giveback, max(0.0, best_pct - current_pct)) or 0.0
    giveback_ratio = safe_float(giveback_ratio, (giveback / best_pct if best_pct > 0.05 else 0.0)) or 0.0
    if best_pct <= 0 or current_pct <= 0:
        return {"active": False}

    tp1_pct = signed_pct(side, entry, getattr(trade, "tp1", entry))
    tp2_pct = signed_pct(side, entry, getattr(trade, "tp2", entry))
    if tp1_pct <= 0:
        return {"active": False}
    # Start dynamic management only when the market has paid meaningfully beyond TP1.
    if best_pct < max(tp1_pct * 1.10, tp1_pct + 0.22, 0.72):
        return {"active": False}

    mode_profile = trade_mode_profile(context or {}, side)
    market_regime = str(mode_profile.get("regime", "NORMAL") or "NORMAL").upper()
    hard_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
    soft_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
    entry_state_name = str((entry_state or {}).get("state") or "").upper()
    entry_weak = entry_state_name in ["WEAK", "BROKEN"] or str(action or "").upper() in ["EXIT_WARNING", "PROTECT_OR_EXIT"]

    tf15_same = ((context or {}).get("tf15") or {}).get("bias") == side
    tf1h_same = ((context or {}).get("tf1h") or {}).get("bias") == side
    structure_same = ((context or {}).get("structure") or {}).get("bias") == side
    ict_same = ((context or {}).get("ict") or {}).get("bias") == side
    trend_support = bool(support_votes >= max(2.35, opposite_votes + 0.35) or tf15_same or tf1h_same or structure_same or ict_same)

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - safe_float(getattr(trade, "stop_current", entry), entry)) / entry * 100.0)
        candidate_from_lock = lambda lock: entry * (1 + lock / 100.0)
    else:
        stop_distance_pct = max(0.0, (safe_float(getattr(trade, "stop_current", entry), entry) - price) / entry * 100.0)
        candidate_from_lock = lambda lock: entry * (1 - lock / 100.0)

    tp2_span = max(0.01, (tp2_pct - tp1_pct)) if tp2_pct > tp1_pct else max(0.01, tp1_pct * 0.85)
    progress_to_tp2 = clamp((best_pct - tp1_pct) / tp2_span, 0.0, 1.25)

    # A pullback after TP1 becomes important earlier than before TP1, but later
    # than after TP2. We scale by progress toward TP2 so the bot does not overreact
    # right after TP1, yet does protect if price almost touched TP2.
    gave_back_meaningful = bool(best_pct >= max(tp1_pct + 0.35, 0.95) and giveback >= max(0.30, best_pct * 0.22))
    gave_back_deep = bool(best_pct >= max(tp1_pct + 0.55, 1.15) and giveback >= max(0.46, best_pct * 0.34))
    near_protective_stop = bool(stop_distance_pct <= 0.16)
    broad_warning = bool(hard_votes >= 1 or soft_votes >= 2 or opposite_votes >= support_votes + 0.50 or entry_weak)
    hard_reversal = bool(confirmed_ict_reversal or (hard_votes >= 1 and (soft_votes >= 1 or gave_back_meaningful)))

    if market_regime in ["RANGE", "REVERSAL", "NEWS_IMPULSE", "IMPULSE"]:
        capture_ratio = 0.66
        model = "POST_TP1_FAST_CAPTURE"
    elif market_regime == "TREND":
        capture_ratio = 0.50
        model = "POST_TP1_TREND_RUNNER"
    elif market_regime == "PULLBACK":
        capture_ratio = 0.58
        model = "POST_TP1_PULLBACK_CAPTURE"
    else:
        capture_ratio = 0.58
        model = "POST_TP1_BALANCED_CAPTURE"

    if progress_to_tp2 >= 0.70:
        capture_ratio = max(capture_ratio, 0.66)
        model += "_NEAR_TP2"
    elif progress_to_tp2 >= 0.45:
        capture_ratio = max(capture_ratio, 0.60)
        model += "_MIDWAY_TO_TP2"

    if hard_reversal:
        capture_ratio = max(capture_ratio, 0.74)
        model += "_HARD_REVERSAL"
    elif broad_warning or gave_back_meaningful:
        capture_ratio = max(capture_ratio, 0.64)
        model += "_WARNING"

    # Closing after TP1 should be rarer than after TP2. It is allowed only when
    # reversal evidence is confirmed or price is already close to the locked stop
    # after giving back a meaningful move.
    close_now = bool(
        hard_reversal
        or (near_protective_stop and (broad_warning or gave_back_meaningful or str(action or "").upper() == "EXIT_WARNING"))
        or (gave_back_deep and progress_to_tp2 >= 0.45 and (soft_votes >= 1 or opposite_votes >= support_votes))
    )
    if close_now:
        return {
            "active": True,
            "close": True,
            "action": "EXIT_AFTER_TP1_PROFIT_PROTECT",
            "title": f"{side} ЗАКРИТИ — TP1 ВЗЯТО, ПРИБУТОК ВІДДАЄТЬСЯ",
            "recommendation": "TP1 уже виконано, а ринок почав повертати прибуток до стоп-зони: краще зафіксувати позицію у плюсі, ніж чекати втрату більшої частини руху",
            "reason": f"{model}: MFE +{round(best_pct, 3)}%, зараз +{round(current_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}%, progress до TP2 {round(progress_to_tp2 * 100, 1)}%",
            "notes": [
                f"TP1 взято; MFE було +{round(best_pct, 3)}%",
                f"поточний плюс +{round(current_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}% MFE",
                f"прогрес до TP2: {round(progress_to_tp2 * 100, 1)}%",
                f"модель: {model}",
                "після TP1 головне — не віддати підтверджений прибуток назад",
            ],
        }

    # Dynamic stop after TP1: do not trail on every candle. Tighten only when the
    # trade made real progress beyond TP1, or warnings appear. Keep ATR/SMC air.
    min_lock = max(tp1_pct * 0.55, 0.18)
    if progress_to_tp2 >= 0.70:
        min_lock = max(min_lock, tp1_pct * 0.92)
    elif progress_to_tp2 >= 0.45:
        min_lock = max(min_lock, tp1_pct * 0.78)

    desired_lock = max(min_lock, best_pct * capture_ratio)
    if trend_support and not broad_warning and market_regime == "TREND":
        desired_lock = min(desired_lock, max(min_lock, best_pct * 0.58))
    desired_lock = min(desired_lock, max(current_pct - 0.10, 0.0)) if current_pct > 0.16 else desired_lock

    proposed = candidate_from_lock(desired_lock)
    proposed, air_reason = _apply_stop_air(side, proposed, price, entry, context or {}, mode_profile, "POST_TP1")
    proposed, noise_reason = _recent_tp_noise_safe_stop(
        side, proposed, trade, context or {}, stage="POST_TP1",
        hard_reversal=hard_reversal, broad_warning=broad_warning,
    )
    if noise_reason:
        air_reason = f"{air_reason}; {noise_reason}" if air_reason else noise_reason
    if proposed is None or not _valid_stop_for_side(side, price, proposed):
        return {"active": True, "protect": False, "reason": "POST_TP1: немає валідного місця для нового стопу без вибивання шумом"}

    if side == "LONG":
        improves = proposed > safe_float(getattr(trade, "stop_current", entry), entry)
    else:
        improves = proposed < safe_float(getattr(trade, "stop_current", entry), entry)

    if improves:
        return {
            "active": True,
            "protect": True,
            "stop": round_price(proposed),
            "action": "TP1_DYNAMIC_PROTECT",
            "title": f"{side} — TP1 ВЗЯТО, СТОП ДИНАМІЧНО ПІДТЯГНУТО",
            "recommendation": "після TP1 стоп реагує на прогрес до TP2, MFE і попередження ринку; захист підтягнуто, але без трейлінгу кожної 3M свічки",
            "reason": f"{model}: захистити ~{round(capture_ratio * 100, 1)}% MFE; {air_reason}",
            "notes": [
                f"MFE +{round(best_pct, 3)}%, поточний плюс +{round(current_pct, 3)}%",
                f"прогрес до TP2: {round(progress_to_tp2 * 100, 1)}%",
                f"віддано {round(giveback_ratio * 100, 1)}% руху після TP1",
                f"новий TP1-dynamic стоп: {round_price(proposed)}",
                f"модель: {model}",
            ],
        }

    return {
        "active": True,
        "protect": False,
        "reason": f"POST_TP1: поточний стоп уже достатньо захищає прибуток ({round_price(getattr(trade, 'stop_current', None))})",
    }

def _post_tp2_dynamic_profit_manager_snapshot(trade, context, current_pct, best_pct, giveback, giveback_ratio,
                                             support_votes=0.0, opposite_votes=0.0,
                                             tf3_against=False, structure_against=False, ict_against=False,
                                             flow_against=False, cvd_against=False,
                                             liquidity_against=False, news_against=False,
                                             confirmed_ict_reversal=False, entry_state=None,
                                             action="HOLD"):
    """Professional dynamic protection after TP2.

    After TP2 the trade is no longer an ordinary runner. The bot has already
    received a large market gift, so the stop must be allowed to react again
    when the market gives warning signs. This layer never loosens a stop and
    never conflicts with TP1 logic: it works only after TP2 and before TP3.

    Decisions:
    - if TP2 was hit and the market gives back too much / reversal appears -> close in profit;
    - if TP2 was hit and price still has room -> trail the stop more tightly;
    - if the trend is still clean -> keep the TP2 lock and let TP3 breathe.
    """
    if not trade or not getattr(trade, "tp2_hit", False) or getattr(trade, "tp3_hit", False):
        return {"active": False}

    side = getattr(trade, "side", "NEUTRAL")
    entry = safe_float(getattr(trade, "entry", 0.0), 0.0) or 0.0
    price = safe_float((context or {}).get("price"), 0.0) or 0.0
    if side not in ["LONG", "SHORT"] or not entry or not price:
        return {"active": False}

    current_pct = safe_float(current_pct, 0.0) or 0.0
    best_pct = safe_float(best_pct, 0.0) or 0.0
    giveback = safe_float(giveback, max(0.0, best_pct - current_pct)) or 0.0
    giveback_ratio = safe_float(giveback_ratio, (giveback / best_pct if best_pct > 0.05 else 0.0)) or 0.0
    if best_pct < 0.80 or current_pct <= 0:
        return {"active": False}

    mode_profile = trade_mode_profile(context or {}, side)
    market_regime = str(mode_profile.get("regime", "NORMAL") or "NORMAL").upper()
    hard_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
    soft_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
    entry_state_name = str((entry_state or {}).get("state") or "").upper()
    entry_weak = entry_state_name in ["WEAK", "BROKEN"] or str(action or "").upper() in ["EXIT_WARNING", "PROTECT_OR_EXIT"]

    tf15_same = ((context or {}).get("tf15") or {}).get("bias") == side
    tf1h_same = ((context or {}).get("tf1h") or {}).get("bias") == side
    structure_same = ((context or {}).get("structure") or {}).get("bias") == side
    ict_same = ((context or {}).get("ict") or {}).get("bias") == side
    trend_support = bool(support_votes >= max(2.4, opposite_votes + 0.45) or tf15_same or tf1h_same or structure_same or ict_same)

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - safe_float(getattr(trade, "stop_current", entry), entry)) / entry * 100.0)
        tp2_pct = signed_pct(side, entry, getattr(trade, "tp2", entry))
        candidate_from_lock = lambda lock: entry * (1 + lock / 100.0)
    else:
        stop_distance_pct = max(0.0, (safe_float(getattr(trade, "stop_current", entry), entry) - price) / entry * 100.0)
        tp2_pct = signed_pct(side, entry, getattr(trade, "tp2", entry))
        candidate_from_lock = lambda lock: entry * (1 - lock / 100.0)

    # After TP2, a pullback of 25-35% of the whole MFE is already meaningful.
    gave_back_meaningful = bool(best_pct >= 1.25 and giveback >= max(0.45, best_pct * 0.24))
    gave_back_deep = bool(best_pct >= 1.50 and giveback >= max(0.70, best_pct * 0.34))
    near_protective_stop = bool(stop_distance_pct <= 0.14)
    broad_warning = bool(hard_votes >= 1 or soft_votes >= 2 or opposite_votes >= support_votes + 0.55 or entry_weak)
    hard_reversal = bool(confirmed_ict_reversal or (hard_votes >= 1 and (soft_votes >= 1 or gave_back_meaningful)))

    # Profit capture model. TREND can breathe; NEWS/RANGE/REVERSAL/EXHAUSTION must protect more.
    if market_regime in ["RANGE", "REVERSAL", "NEWS_IMPULSE", "IMPULSE"]:
        capture_ratio = 0.76
        model = "POST_TP2_FAST_CAPTURE"
    elif market_regime == "TREND":
        capture_ratio = 0.66
        model = "POST_TP2_TREND_RUNNER"
    elif market_regime == "PULLBACK":
        capture_ratio = 0.70
        model = "POST_TP2_PULLBACK_CAPTURE"
    else:
        capture_ratio = 0.70
        model = "POST_TP2_BALANCED_CAPTURE"

    if hard_reversal:
        capture_ratio = max(capture_ratio, 0.82)
        model += "_HARD_REVERSAL"
    elif broad_warning or gave_back_meaningful:
        capture_ratio = max(capture_ratio, 0.74)
        model += "_WARNING"

    # If the bounce/reversal has already reached the active protective stop area,
    # closing now is better than waiting for another 15m cycle.
    close_now = bool(
        hard_reversal
        or (near_protective_stop and (broad_warning or gave_back_meaningful or str(action or "").upper() == "EXIT_WARNING"))
        or (gave_back_deep and (soft_votes >= 1 or opposite_votes >= support_votes))
    )
    if close_now:
        return {
            "active": True,
            "close": True,
            "action": "EXIT_AFTER_TP2_PROFIT_PROTECT",
            "title": f"{side} ЗАКРИТИ — TP2 ВЗЯТО, РОЗВОРОТ/ВІДКАТ ПОСИЛИВСЯ",
            "recommendation": "TP2 вже виконано і ринок почав віддавати прибуток: зафіксувати позицію у плюсі, не чекати дальній стоп/TP3 без нового імпульсу",
            "reason": f"{model}: MFE +{round(best_pct, 3)}%, зараз +{round(current_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}%, stop-gap {round(stop_distance_pct, 3)}%",
            "notes": [
                f"TP2 вже взято; MFE було +{round(best_pct, 3)}%",
                f"поточний плюс +{round(current_pct, 3)}%, віддано {round(giveback_ratio * 100, 1)}% MFE",
                f"модель: {model}",
                f"hard/soft warnings: {hard_votes}/{soft_votes}",
                "після TP2 головне — не віддати основний зароблений рух",
            ],
        }

    # Dynamic stop: protect a defined part of MFE, but keep ATR/SMC air.
    min_lock = max(0.0, tp2_pct * 0.78 if tp2_pct else 0.0)
    desired_lock = max(min_lock, best_pct * capture_ratio)
    if trend_support and not broad_warning and market_regime == "TREND":
        desired_lock = min(desired_lock, max(min_lock, best_pct * 0.68))
    desired_lock = min(desired_lock, max(current_pct - 0.08, 0.0)) if current_pct > 0.10 else desired_lock

    proposed = candidate_from_lock(desired_lock)
    proposed, air_reason = _apply_stop_air(side, proposed, price, entry, context or {}, mode_profile, "POST_TP2")
    proposed, noise_reason = _recent_tp_noise_safe_stop(
        side, proposed, trade, context or {}, stage="POST_TP2",
        hard_reversal=hard_reversal, broad_warning=broad_warning,
    )
    if noise_reason:
        air_reason = f"{air_reason}; {noise_reason}" if air_reason else noise_reason
    if proposed is None or not _valid_stop_for_side(side, price, proposed):
        return {"active": True, "protect": False, "reason": "POST_TP2: немає валідного місця для нового стопу без миттєвого вибивання"}

    if side == "LONG":
        improves = proposed > safe_float(getattr(trade, "stop_current", entry), entry)
    else:
        improves = proposed < safe_float(getattr(trade, "stop_current", entry), entry)

    if improves:
        return {
            "active": True,
            "protect": True,
            "stop": round_price(proposed),
            "action": "TP2_DYNAMIC_PROTECT",
            "title": f"{side} — TP2 ВЗЯТО, СТОП ДИНАМІЧНО ПІДТЯГНУТО",
            "recommendation": "після TP2 стоп реагує на MFE/giveback і умови ринку; захист підтягнуто, але без душіння нормального тренду",
            "reason": f"{model}: захистити ~{round(capture_ratio * 100, 1)}% MFE; {air_reason}",
            "notes": [
                f"MFE +{round(best_pct, 3)}%, поточний плюс +{round(current_pct, 3)}%",
                f"віддано {round(giveback_ratio * 100, 1)}% руху після TP2",
                f"новий динамічний стоп: {round_price(proposed)}",
                f"модель: {model}",
            ],
        }

    return {
        "active": True,
        "protect": False,
        "reason": f"POST_TP2: поточний стоп уже достатньо захищає прибуток ({round_price(getattr(trade, 'stop_current', None))})",
    }

def _exit_reason_code(result, context=None, trade=None):
    action = str((result or {}).get("action") or "")
    notes = " ".join(str(x) for x in ((result or {}).get("notes") or [])).lower()
    if action.startswith("TP"):
        return "TP_HIT"
    if action == "STOP":
        return "STOP_BY_HIGH_LOW"
    if "MFE" in action or "giveback" in notes or "відда" in notes:
        return "MFE_GIVEBACK"
    if "LOCAL_BREAK" in action or "3m" in notes or "структ" in notes:
        return "STRUCTURE_BREAK"
    if "EXIT" in action:
        return "MANUAL_EXIT_SIGNAL"
    return action or "UNKNOWN"

def _exit_quality_score(trade, result, context):
    """0-100 quality of the exit, separate from entry quality."""
    action = str((result or {}).get("action") or "")
    current_pct = safe_float((result or {}).get("current_pct"), 0.0) or 0.0
    best_pct = safe_float((result or {}).get("best_pct"), 0.0) or 0.0
    captured = (current_pct / best_pct * 100.0) if best_pct > 0.1 else None
    confirmed_rev, _ = _has_confirmed_ict_reversal(getattr(trade, "side", "NEUTRAL"), context or {})
    score = 55.0
    if action.startswith("TP"):
        score += 25
    if action == "STOP" and current_pct >= 0:
        score += 16
    elif action == "STOP" and current_pct < 0:
        score -= 18
    if captured is not None:
        if captured >= 70:
            score += 14
        elif captured >= 50:
            score += 6
        elif captured < 30:
            score -= 16
    if confirmed_rev:
        score += 10
    elif "EXIT" in action and current_pct > 0:
        score -= 6
    return int(max(0, min(100, round(score))))

def active_trade_risk_snapshot(trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, action):
    """Professional, dynamic supervision risk model.

    This is informational only: it does not open, close or move stops by itself.
    It is recalculated on EVERY follow message from the fresh context and answers:
      1) Current trade risk: how safe it is to keep holding the open position.
      2) Reversal risk: whether a real opposite setup is forming.

    The model is intentionally organic, not a hard one-factor switch:
    - 15M / structure / ICT have the largest weight.
    - 3M is important, but can be noisy.
    - CVD / flow / clusters confirm or warn, but do not dominate alone.
    - MFE giveback raises current trade risk, especially after meaningful profit.
    - In a trend, normal pullbacks are tolerated more than in range/reversal modes.
    """
    side = trade.side
    opp = opposite(side)

    def block(name):
        return context.get(name) or {}

    tf3 = block("tf3")
    tf15 = block("tf15")
    tf1h = block("tf1h")
    tf4h = block("tf4h")
    structure = block("structure")
    ict = block("ict")
    cvd = block("cvd")
    flow = block("flow")
    clusters = block("clusters")
    derivatives = block("derivatives")
    liquidity = block("liquidity")
    news = block("news")

    mode_profile = trade_mode_profile(context, side)
    market_regime = mode_profile.get("regime", "NORMAL")

    def clamp(value, lo=0, hi=100):
        return max(lo, min(hi, value))

    def strength(b, norm=40):
        try:
            return clamp(abs(float((b or {}).get("score", 0) or 0)) / float(norm), 0.35, 1.25)
        except Exception:
            return 0.65

    def risk_label(score):
        if score >= 82:
            return "КРИТИЧНИЙ"
        if score >= 64:
            return "ВИСОКИЙ"
        if score >= 36:
            return "СЕРЕДНІЙ"
        return "НИЗЬКИЙ"

    def reversal_label(score):
        if score >= 82:
            return "ДУЖЕ ВИСОКИЙ"
        if score >= 64:
            return "ВИСОКИЙ"
        if score >= 42:
            return "СЕРЕДНІЙ"
        if score >= 24:
            return "ПОМІРНИЙ"
        return "НИЗЬКИЙ"

    def ict_reversal_evidence():
        """Return how strong the opposite ICT reversal is.

        CVD/flow/news may warn, but they must not create HIGH/CRITICAL
        reversal labels by themselves. For a scary label the bot needs actual
        ICT/structure evidence: CHOCH/sweep/reclaim/FVG/OB against the trade.
        """
        phase = str((structure or {}).get("phase", "")).upper()
        ict_setup = str((ict or {}).get("setup", "")).upper()
        ict_state = str((ict or {}).get("state", "")).upper()
        ict_note = str((ict or {}).get("note", "")).lower()

        components = []
        if opp == "LONG":
            if "CHOCH LONG" in phase or "DOWNSIDE SWEEP" in phase:
                components.append("CHOCH/SWEEP LONG")
            if (liquidity or {}).get("bias") == "LONG" and any(x in str((liquidity or {}).get("event", "")).upper() for x in ["SWEEP", "RECLAIM"]):
                components.append("LIQUIDITY RECLAIM LONG")
            if (ict or {}).get("bias") == "LONG" and any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]):
                components.append("ICT MODEL LONG")
            if (ict or {}).get("entry_ok") and (ict or {}).get("bias") == "LONG":
                components.append("ICT ENTRY LONG")
        else:
            if "CHOCH SHORT" in phase or "UPSIDE SWEEP" in phase:
                components.append("CHOCH/SWEEP SHORT")
            if (liquidity or {}).get("bias") == "SHORT" and any(x in str((liquidity or {}).get("event", "")).upper() for x in ["SWEEP", "RECLAIM"]):
                components.append("LIQUIDITY RECLAIM SHORT")
            if (ict or {}).get("bias") == "SHORT" and any(x in ict_setup for x in ["SWEEP", "FVG", "OB", "RETRACE", "RECLAIM"]):
                components.append("ICT MODEL SHORT")
            if (ict or {}).get("entry_ok") and (ict or {}).get("bias") == "SHORT":
                components.append("ICT ENTRY SHORT")

        # Avoid duplicate inflation when the same idea is seen by two modules.
        return len(set(components)), list(dict.fromkeys(components))

    ict_rev_count, ict_rev_components = ict_reversal_evidence()

    # -------------------------
    # 1) Current trade risk
    # -------------------------
    # Start from a neutral-but-not-scary baseline. The score then moves up/down
    # according to fresh market evidence.
    trade_risk_score = 30.0

    # If the bot itself is no longer in a clean HOLD state, risk should rise,
    # but not automatically become HIGH; strong 15M/ICT can still keep it moderate.
    if action in ["EXIT_WARNING", "PROTECT_OR_EXIT"]:
        trade_risk_score += 18
    elif action in ["PROTECT", "TP1_PROTECT", "TP2_PROTECT", "TRAIL_ICT_SMC"]:
        trade_risk_score += 6
    elif action in ["EXIT_AFTER_TP1_GIVEBACK", "EXIT_MFE_GIVEBACK"]:
        trade_risk_score += 30

    # Position result / stop proximity.
    if current_pct <= -0.45:
        trade_risk_score += 22
    elif current_pct <= -0.18:
        trade_risk_score += 13
    elif current_pct < 0:
        trade_risk_score += 7
    elif current_pct >= 0.55:
        trade_risk_score -= 6
    elif current_pct >= 0.25:
        trade_risk_score -= 3

    # MFE giveback: if the market gave profit and is taking it back, risk rises.
    if best_pct > 0.20:
        giveback_ratio = clamp(giveback / best_pct if best_pct else 0.0, 0.0, 1.0)
        if best_pct >= 0.90:
            trade_risk_score += giveback_ratio * 22
        elif best_pct >= 0.55:
            trade_risk_score += giveback_ratio * 17
        else:
            trade_risk_score += giveback_ratio * 10
        if current_pct > 0 and giveback_ratio < 0.25:
            trade_risk_score -= 4

    # Follow-through failure: if after several runs the trade did not move toward TP1,
    # the position risk must rise even if 3M is not fully reversed yet. This catches
    # situations like SHORT made after a drop: price gave only small MFE, then bounced
    # back toward/above entry. It is informational only, not an auto-exit.
    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    weak_followthrough = bool(elapsed_min >= 45 and best_pct < 0.30 and current_pct <= 0.08)
    no_followthrough = bool(elapsed_min >= 75 and best_pct < 0.22 and current_pct <= 0.05)
    gave_back_small_mfe = bool(best_pct >= 0.12 and giveback >= max(0.12, best_pct * 0.65))
    post_tp1_tail_warning = bool(
        getattr(trade, "tp1_hit", False)
        and not getattr(trade, "tp2_hit", False)
        and best_pct >= 0.90
        and giveback >= max(0.32, best_pct * 0.24)
    )
    post_tp1_tail_strong_warning = bool(
        getattr(trade, "tp1_hit", False)
        and not getattr(trade, "tp2_hit", False)
        and best_pct >= 1.15
        and giveback >= max(0.50, best_pct * 0.36)
    )
    post_tp2_tail_warning = bool(
        getattr(trade, "tp2_hit", False)
        and best_pct >= 1.20
        and giveback >= max(0.42, best_pct * 0.22)
    )
    post_tp2_tail_strong_warning = bool(
        getattr(trade, "tp2_hit", False)
        and best_pct >= 1.50
        and giveback >= max(0.70, best_pct * 0.32)
    )

    if weak_followthrough:
        trade_risk_score += 8
    if no_followthrough:
        trade_risk_score += 10
    if gave_back_small_mfe:
        trade_risk_score += 7

    # Universal trade validation: does the opened idea behave as expected?
    # This is integrated into "Поточний ризик угоди"; it is not displayed as a
    # separate noisy Telegram block. It works for all regimes, not only countertrend.
    trade_validation = trade_entry_validation_snapshot(trade, context, current_pct, best_pct, giveback)
    if trade_validation:
        trade_risk_score += float(trade_validation.get("risk_adjustment", 0) or 0)
        if trade_validation.get("severity") == "BAD":
            reversal_score_floor_from_validation = 26
        elif trade_validation.get("severity") == "WARN":
            reversal_score_floor_from_validation = 20
        else:
            reversal_score_floor_from_validation = 0
    else:
        reversal_score_floor_from_validation = 0

    # Hidden ELS support: if the current position was opened/managed from a poor
    # location, the informational risk should not stay unrealistically low.
    # This is only a small organic adjustment; it never moves stop/TP by itself.
    entry_location = context.get("entry_location") or analyze_entry_location_score(side, context)
    els_score = int(entry_location.get("score", 62) or 62)
    if els_score < 38:
        trade_risk_score += 9
        reversal_score_floor_from_els = 26
    elif els_score < 50:
        trade_risk_score += 5
        reversal_score_floor_from_els = 22
    elif els_score >= 78 and current_pct >= 0:
        trade_risk_score -= 3
        reversal_score_floor_from_els = 0
    else:
        reversal_score_floor_from_els = 0

    # Distance to active stop: when price is already near the protective stop, risk is not low.
    if trade.entry:
        if side == "LONG":
            stop_dist_pct = max(0.0, (safe_float(context.get("price"), trade.entry) - trade.stop_current) / trade.entry * 100)
        else:
            stop_dist_pct = max(0.0, (trade.stop_current - safe_float(context.get("price"), trade.entry)) / trade.entry * 100)
        if stop_dist_pct <= 0.12:
            trade_risk_score += 12
        elif stop_dist_pct <= 0.28:
            trade_risk_score += 7
        if getattr(trade, "tp1_hit", False) and not getattr(trade, "tp2_hit", False) and current_pct > 0 and post_tp1_tail_warning:
            trade_risk_score += 7
            if stop_dist_pct <= 0.18 or post_tp1_tail_strong_warning:
                trade_risk_score += 5
        if getattr(trade, "tp2_hit", False) and current_pct > 0 and post_tp2_tail_warning:
            trade_risk_score += 10
            if stop_dist_pct <= 0.18 or post_tp2_tail_strong_warning:
                trade_risk_score += 7

    # Fresh market alignment. 15M, structure and ICT dominate; flow confirms.
    risk_blocks = [
        (tf15, 22, 45),
        (structure, 22, 35),
        (ict, 22, 38),
        (tf3, 13, 65),
        # Flow/CVD/news are warning layers, not reasons for HIGH/CRITICAL
        # by themselves. Their weights stay soft to avoid psychological noise.
        (cvd, 4, 25),
        (flow, 4, 25),
        (clusters, 3, 14),
        (derivatives, 3, 18),
        (liquidity, 7, 18),
        (tf1h, 8, 55),
        (tf4h, 5, 60),
        (news, 1, 35),
    ]
    for b, weight, norm in risk_blocks:
        bias = (b or {}).get("bias")
        s = strength(b, norm)
        if bias == opp:
            trade_risk_score += weight * s
        elif bias == side:
            trade_risk_score -= weight * min(s, 1.0)

    # Regime smoothing: do not panic in a real trend, but be stricter in range/reversal.
    if market_regime == "TREND" and (tf15.get("bias") == side or ict.get("bias") == side or structure.get("bias") == side):
        trade_risk_score -= 7
    elif market_regime == "RANGE":
        trade_risk_score += 7
    elif market_regime == "REVERSAL":
        trade_risk_score += 5
    elif market_regime == "NEWS_IMPULSE":
        trade_risk_score += 3

    if trade.tp1_hit:
        trade_risk_score -= 6
    if "RISKY_ENTRY" in (trade.notes or []):
        trade_risk_score += 5

    # Counter-trend context floor: if both 1H and 4H are against the open trade,
    # the trade can still be valid, but risk should not be shown as ultra-low.
    higher_tf_against = (tf1h.get("bias") == opp and tf4h.get("bias") == opp)
    intraday_supports = sum([
        tf15.get("bias") == side,
        structure.get("bias") == side,
        ict.get("bias") == side,
        tf3.get("bias") == side,
        flow.get("bias") == side or cvd.get("bias") == side,
    ])
    if higher_tf_against:
        floor_value = 24 if (current_pct > 0.45 and intraday_supports >= 3) else 32
        trade_risk_score = max(trade_risk_score, floor_value)

    # Support/opposition aggregate from manage_active_trade, used as soft confirmation.
    trade_risk_score += max(0.0, opposite_votes - support_votes) * 4.5
    trade_risk_score -= max(0.0, support_votes - opposite_votes) * 2.5
    if higher_tf_against:
        floor_value = 24 if (current_pct > 0.45 and intraday_supports >= 3) else 32
        trade_risk_score = max(trade_risk_score, floor_value)

    # Do not show scary current-risk labels when there is no real ICT/structure
    # reversal. CVD/flow/news can lift risk to MEDIUM, but HIGH/CRITICAL should
    # require ICT/structure evidence or the trade actually being close to stop/deep red.
    if ict_rev_count == 0:
        # Without ICT/structure reversal evidence, CVD/flow/news can warn,
        # but must not print scary HIGH/CRITICAL risk. If the trade is not
        # materially losing, cap it at MEDIUM.
        if current_pct > -0.25 and action not in ["EXIT_AFTER_TP1_GIVEBACK", "EXIT_MFE_GIVEBACK"]:
            trade_risk_score = min(trade_risk_score, 52)
    elif ict_rev_count == 1:
        # One ICT clue means caution, not panic. HIGH requires a cluster of
        # real reversal evidence or actual stop/deep loss conditions.
        trade_risk_score = min(trade_risk_score, 62 if current_pct > -0.35 else 68)

    trade_risk_score = int(round(clamp(trade_risk_score)))
    trade_risk = risk_label(trade_risk_score)

    # -------------------------
    # 2) Opposite/reversal risk
    # -------------------------
    # This is not just "blocks against position". It asks whether the opposite
    # side is becoming a tradable setup: 3M + structure/ICT + flow/confirmation.
    reversal_score = 8.0

    reversal_blocks = [
        (tf3, 16, 65),
        (tf15, 20, 45),
        (structure, 24, 35),
        (ict, 24, 38),
        # These are confirmations/warnings only. They should not create a
        # reversal call without ICT/structure.
        (cvd, 6, 25),
        (flow, 6, 25),
        (clusters, 4, 14),
        (liquidity, 8, 18),
        (derivatives, 3, 18),
        (tf1h, 5, 55),
        (tf4h, 2, 60),
    ]
    opposite_core = 0
    support_core = 0
    soft_opposite_pressure = 0
    for b, weight, norm in reversal_blocks:
        bias = (b or {}).get("bias")
        s = strength(b, norm)
        signed_to_opp = side_score(int((b or {}).get("score", 0) or 0), opp)
        if bias == opp:
            reversal_score += weight * s
            soft_opposite_pressure += 1
            if b in [tf3, tf15, structure, ict]:
                opposite_core += 1
        elif signed_to_opp >= max(6, int(norm * 0.22)):
            # Some modules keep bias NEUTRAL while their signed score already
            # shows pressure for the opposite side (common in CVD/flow/clusters).
            reversal_score += weight * 0.45 * min(s, 1.0)
            soft_opposite_pressure += 1
        elif bias == side:
            reversal_score -= (weight * 0.65) * min(s, 1.0)
            if b in [tf3, tf15, structure, ict]:
                support_core += 1

    # A real reversal normally needs at least two core components.
    if opposite_core >= 3:
        reversal_score += 12
    elif opposite_core == 2:
        reversal_score += 6
    elif opposite_core == 0:
        reversal_score -= 10

    if support_core >= 3:
        reversal_score -= 12
    elif support_core == 2:
        reversal_score -= 6

    # If the open trade fails to follow through, the opposite side becomes more
    # credible even before a textbook opposite ICT setup appears. This prevents
    # unrealistic readings like "LONG reversal 5%" after a weak SHORT that failed
    # to expand.
    if weak_followthrough:
        reversal_score += 7
    if no_followthrough:
        reversal_score += 10
    if gave_back_small_mfe:
        reversal_score += 6
    if soft_opposite_pressure >= 3:
        reversal_score += 5

    # MFE and PnL context: when a trade gives back profit or goes red, opposite scenario gains credibility.
    if current_pct < -0.15:
        reversal_score += 8
    if best_pct > 0.45:
        giveback_ratio = clamp(giveback / best_pct if best_pct else 0.0, 0.0, 1.0)
        reversal_score += giveback_ratio * 12
    if trade.tp1_hit and current_pct > 0:
        reversal_score -= 6
    if getattr(trade, "tp1_hit", False) and not getattr(trade, "tp2_hit", False) and current_pct > 0:
        # After TP1, a meaningful giveback before TP2 should not be displayed as
        # unrealistically low reversal risk. It is softer than the TP2 floor, but
        # enough to print at least MODERATE/MEDIUM when the market starts turning.
        if post_tp1_tail_strong_warning:
            reversal_score += 16
            post_tp1_reversal_floor = 40
        elif post_tp1_tail_warning:
            reversal_score += 10
            post_tp1_reversal_floor = 32
        else:
            post_tp1_reversal_floor = 0
    else:
        post_tp1_reversal_floor = 0
    if getattr(trade, "tp2_hit", False) and current_pct > 0:
        # After TP2, a strong bounce/giveback is a real tail-risk even if ICT has
        # not yet printed a textbook reversal. This prevents messages like
        # "LONG reversal LOW 22%" while price is already bouncing hard into a
        # protected SHORT stop.
        if post_tp2_tail_strong_warning:
            reversal_score += 22
            post_tp2_reversal_floor = 48
        elif post_tp2_tail_warning:
            reversal_score += 14
            post_tp2_reversal_floor = 42
        else:
            post_tp2_reversal_floor = 0
    else:
        post_tp2_reversal_floor = 0

    # In a clean trend with higher timeframe still supporting the position, avoid overcalling reversals.
    if market_regime == "TREND" and (tf15.get("bias") == side or tf1h.get("bias") == side) and opposite_core < 3:
        reversal_score -= 8
    if market_regime == "RANGE":
        reversal_score += 4
    if higher_tf_against and opposite_core >= 1:
        reversal_score = max(reversal_score, 22)

    # Floors for weak/failed positions. These floors are intentionally moderate:
    # they do not force exits, but they stop the bot from displaying an
    # unrealistically low reversal probability while the trade is already stalling.
    if no_followthrough:
        reversal_score = max(reversal_score, 28)
    elif weak_followthrough or gave_back_small_mfe:
        reversal_score = max(reversal_score, 22)
    if current_pct < 0 and best_pct < 0.25 and elapsed_min >= 60:
        reversal_score = max(reversal_score, 25)
    if 'reversal_score_floor_from_els' in locals() and reversal_score_floor_from_els:
        reversal_score = max(reversal_score, reversal_score_floor_from_els)
    if 'reversal_score_floor_from_validation' in locals() and reversal_score_floor_from_validation:
        reversal_score = max(reversal_score, reversal_score_floor_from_validation)
    if 'post_tp1_reversal_floor' in locals() and post_tp1_reversal_floor:
        reversal_score = max(reversal_score, post_tp1_reversal_floor)
    if 'post_tp2_reversal_floor' in locals() and post_tp2_reversal_floor:
        reversal_score = max(reversal_score, post_tp2_reversal_floor)

    # ICT-gated labels: without a real opposite ICT/structure setup, do not
    # frighten the trader with HIGH/CRITICAL reversal labels.
    # 0 ICT components -> max low/moderate warning.
    # 1 component -> max medium.
    # 2 components -> high is possible.
    # 3+ components -> very high is possible.
    if ict_rev_count == 0:
        cap0 = 36
        if 'post_tp1_reversal_floor' in locals() and post_tp1_reversal_floor:
            cap0 = max(cap0, 46 if post_tp1_tail_strong_warning else 40)
        if 'post_tp2_reversal_floor' in locals() and post_tp2_reversal_floor:
            cap0 = 58 if post_tp2_tail_strong_warning else 48
        reversal_score = min(reversal_score, cap0)
    elif ict_rev_count == 1:
        cap1 = 48
        if 'post_tp1_reversal_floor' in locals() and post_tp1_reversal_floor:
            cap1 = max(cap1, 56 if post_tp1_tail_strong_warning else 50)
        if 'post_tp2_reversal_floor' in locals() and post_tp2_reversal_floor:
            cap1 = 66 if post_tp2_tail_strong_warning else 56
        reversal_score = min(reversal_score, cap1)
    elif ict_rev_count == 2:
        reversal_score = min(reversal_score, 78 if ('post_tp2_reversal_floor' in locals() and post_tp2_reversal_floor) else 72)

    reversal_score = int(round(clamp(reversal_score)))
    rev_label = reversal_label(reversal_score)

    return {
        "trade_risk": trade_risk,
        "trade_risk_score": trade_risk_score,
        "trade_risk_reasons": [],
        "reversal_side": opp,
        "reversal_score": reversal_score,
        "reversal_label": rev_label,
        "reversal_reasons": [],
        "ict_reversal_components": ict_rev_components,
        "ict_reversal_count": ict_rev_count,
        "trade_validation": trade_validation,
    }



def trade_entry_validation_snapshot(trade, context, current_pct, best_pct, giveback,
                                    tf3_against=False, structure_against=False,
                                    ict_against=False, flow_against=False, cvd_against=False):
    """Universal trade validation used inside current trade risk.

    It answers one question: is the opened idea behaving as expected after entry?
    This is NOT a separate Telegram block and it does NOT delay entries. It only
    adjusts "Поточний ризик угоди" and gives manage_active_trade a soft warning
    when a position fails to develop.
    """
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}

    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    notes = [str(x).upper() for x in (getattr(trade, "notes", None) or [])]
    flagged_countertrend = any("COUNTERTREND_ENTRY" in n or "КОНТРТРЕНД" in n for n in notes)
    htf_against_now = bool(tf1h.get("bias") == opp or tf4h.get("bias") == opp)
    is_countertrend = bool(flagged_countertrend or ("RISKY_ENTRY" in notes and htf_against_now))

    confirmations = 0
    warnings = 0

    # Price/action validation. A normal trend trade gets more time; a countertrend
    # trade must start working sooner, but this still remains informational.
    if current_pct >= 0.18:
        confirmations += 1
    if best_pct >= (0.24 if is_countertrend else 0.32):
        confirmations += 1
    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 24:
        confirmations += 1
    if tf15.get("bias") == side:
        confirmations += 1
    if structure.get("bias") == side:
        confirmations += 1
    if ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16):
        confirmations += 1
    if flow.get("bias") == side or cvd.get("bias") == side:
        confirmations += 1

    if current_pct <= (-0.16 if is_countertrend else -0.22):
        warnings += 1
    if best_pct < (0.16 if is_countertrend else 0.20) and elapsed_min >= 14:
        warnings += 1
    if elapsed_min >= 45 and best_pct < 0.25 and current_pct <= 0.05:
        warnings += 1
    if tf3_against or tf3.get("bias") == opp:
        warnings += 1
    if structure_against or structure.get("bias") == opp:
        warnings += 1
    if ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16):
        warnings += 1
    if flow_against and cvd_against:
        warnings += 1
    if giveback >= max(0.18, best_pct * 0.65) and best_pct >= 0.20:
        warnings += 1

    raw = 50 + confirmations * 8 - warnings * 10
    if is_countertrend:
        raw -= 6
        # Countertrend entries that do not develop by the first follow message
        # should be treated as higher current-risk, not as a normal trade.
        if elapsed_min >= 14 and confirmations <= 1 and warnings >= 2:
            raw -= 10

    score = int(max(0, min(100, round(raw))))
    if score >= 70:
        status = "CONFIRMED"
        risk_adjustment = -8
        severity = "OK"
    elif score >= 52:
        status = "VALIDATING"
        risk_adjustment = 0
        severity = "WATCH"
    elif score >= 36:
        status = "WEAK"
        risk_adjustment = 8 if not is_countertrend else 12
        severity = "WARN"
    else:
        status = "FAILED"
        risk_adjustment = 16 if not is_countertrend else 22
        severity = "BAD"

    return {
        "active": True,
        "status": status,
        "severity": severity,
        "score": score,
        "risk_adjustment": risk_adjustment,
        "is_countertrend": is_countertrend,
        "elapsed_min": round(elapsed_min, 1),
        "confirmations": confirmations,
        "warnings": warnings,
    }


def entry_point_state_snapshot(trade, context, current_pct, best_pct, giveback,
                               high_since_open=None, low_since_open=None,
                               trade_validation=None,
                               tf3_against=False, structure_against=False,
                               ict_against=False, flow_against=False, cvd_against=False):
    """Professional lifecycle model for the original entry point.

    This is not a duplicate of generic risk. It answers: is the exact entry
    price/idea still valid, damaged but recoverable, or broken?

    It uses:
    - MFE/MAE relative to the initial stop;
    - whether price recovered back to entry after a bad move;
    - 3M/15M/ICT/structure/flow confirmation;
    - deep adverse move before the formal stop.
    """
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)
    price = safe_float(context.get("price"), getattr(trade, "entry", 0)) or getattr(trade, "entry", 0)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}

    entry = safe_float(getattr(trade, "entry", 0), 0) or 0
    stop_initial = safe_float(getattr(trade, "stop_initial", getattr(trade, "stop_current", 0)), 0) or 0
    stop_current = safe_float(getattr(trade, "stop_current", stop_initial), stop_initial) or stop_initial
    risk_distance_pct = abs(entry - stop_initial) / entry * 100 if entry else 0.0
    risk_distance_pct = max(risk_distance_pct, 0.01)

    if side == "LONG":
        worst_price = safe_float(low_since_open, min(price, entry))
        adverse_pct = max(0.0, (entry - worst_price) / entry * 100 if entry else 0.0)
        stop_distance_now_pct = max(0.0, (price - stop_current) / entry * 100 if entry else 99.0)
    else:
        worst_price = safe_float(high_since_open, max(price, entry))
        adverse_pct = max(0.0, (worst_price - entry) / entry * 100 if entry else 0.0)
        stop_distance_now_pct = max(0.0, (stop_current - price) / entry * 100 if entry else 99.0)

    adverse_fraction = adverse_pct / risk_distance_pct if risk_distance_pct else 0.0
    near_entry = abs(current_pct) <= 0.16
    recovered_to_entry = bool(adverse_fraction >= 0.34 and near_entry)
    deep_adverse = bool(adverse_fraction >= 0.60)
    near_stop = bool(stop_distance_now_pct <= max(0.12, risk_distance_pct * 0.18))

    core_support = 0
    core_against = 0
    soft_support = 0
    soft_against = 0
    for block in [tf3, tf15, structure, ict]:
        if block.get("bias") == side:
            core_support += 1
        elif block.get("bias") == opp:
            core_against += 1
    for block in [flow, cvd]:
        if block.get("bias") == side:
            soft_support += 1
        elif block.get("bias") == opp:
            soft_against += 1

    ict_valid = bool(ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16))
    structure_valid = bool(structure.get("bias") == side)
    structure_broken = bool(structure_against or structure.get("bias") == opp)
    ict_broken = bool(ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16))

    base = int((trade_validation or {}).get("score", getattr(trade, "entry_integrity_score", 100) or 100) or 50)
    score = float(base)

    # Price lifecycle.
    if current_pct >= 0.45:
        score += 10
    elif current_pct >= 0.18:
        score += 5
    elif current_pct <= -0.45:
        score -= 16
    elif current_pct <= -0.22:
        score -= 9

    if best_pct >= 0.55:
        score += 7
    elif best_pct >= 0.25:
        score += 3
    elif best_pct < 0.16:
        score -= 6

    if deep_adverse:
        score -= 18
    elif adverse_fraction >= 0.42:
        score -= 10

    if core_support >= 3:
        score += 12
    elif core_support == 2:
        score += 6
    if core_against >= 3:
        score -= 18
    elif core_against == 2:
        score -= 10
    if soft_support >= 1:
        score += 3
    if soft_against >= 2:
        score -= 5

    if giveback >= max(0.20, best_pct * 0.65) and best_pct >= 0.25:
        score -= 7

    recovery_mode = "NONE"
    reason = "точка входу працює штатно"
    advice = "можна залишатися в угоді, поки ICT/структура не зламаються"
    auto_exit = False
    hard_broken = False

    if recovered_to_entry:
        recovery_mode = "RECHECK_AT_ENTRY"
        if (core_support >= 2 or ict_valid or structure_valid) and core_against <= 1:
            # Price came back to entry after a bad move, but the setup rebuilt.
            # Re-rate from the current context instead of punishing it forever.
            score = max(score, 62)
            reason = "ціна повернулась до входу після просадки; сетап повторно перевірений і ще має підтвердження"
            advice = "залишатися можна, але без усереднення; якщо наступний супровід знову слабшає — вихід біля входу"
        else:
            score = min(score, 48)
            reason = "ціна повернулась до входу після просадки, але ICT/структура не відновили сетап"
            advice = "краще закрити біля входу, якщо наступний імпульс не підтвердить напрям"
    elif deep_adverse:
        recovery_mode = "DEEP_ADVERSE_MOVE"
        if structure_broken or ict_broken or core_against >= 2:
            score = min(score, 34)
            reason = "ціна пройшла понад 60% шляху до стопа і структура/ICT проти точки входу"
            advice = "вихід раніше повного стопа; точка входу втратила актуальність"
            auto_exit = True
            hard_broken = True
        else:
            score = min(score, 55)
            reason = "глибока просадка до стопа, але повного ICT/SMC зламу ще немає"
            advice = "тримати тільки до наступної перевірки; без reclaim/підтвердження не чекати повний стоп"
    elif (trade_validation or {}).get("severity") == "BAD":
        reason = "угода не розкривається після входу: MFE слабкий або структура/3M проти"
        advice = "не чекати дальній стоп; шукати вихід біля входу або при наступному слабкому супроводі"
    elif (trade_validation or {}).get("severity") == "WARN":
        reason = "точка входу слабшає, але ще не зламана"
        advice = "залишатися тільки якщо наступний супровід покаже відновлення"

    score = int(max(0, min(100, round(score))))
    if score >= 80:
        label = "🟢 Сильна"
        state = "STRONG"
    elif score >= 60:
        label = "🟡 Робоча"
        state = "WORKING"
    elif score >= 40:
        label = "🟠 Слабка"
        state = "WEAK"
    else:
        label = "🔴 Зламана"
        state = "BROKEN"
        if core_against >= 2 or hard_broken:
            auto_exit = True

    fail_now = state in ["WEAK", "BROKEN"] and not (recovered_to_entry and state == "WORKING")

    return {
        "active": True,
        "score": score,
        "label": label,
        "state": state,
        "reason": reason,
        "advice": advice,
        "fail_now": fail_now,
        "auto_exit": bool(auto_exit),
        "recovery_mode": recovery_mode,
        "adverse_pct": round(adverse_pct, 3),
        "adverse_fraction": round(adverse_fraction, 3),
        "near_entry": near_entry,
        "deep_adverse": deep_adverse,
        "near_stop": near_stop,
        "core_support": core_support,
        "core_against": core_against,
        "soft_support": soft_support,
        "soft_against": soft_against,
    }


def countertrend_entry_validation(trade, context, current_pct, best_pct, giveback, tf3_against=False,
                                  structure_against=False, ict_against=False, flow_against=False, cvd_against=False):
    """Status block for early counter-trend entries.

    It is informational for Telegram supervision. It does NOT close the trade by itself
    and it does NOT delay the entry. Its purpose is to tell the user on the next
    15-minute follow message whether a risky counter-trend reversal is confirming
    or failing, so the user is not forced to watch 3M candles manually.
    """
    notes = [str(x).upper() for x in (getattr(trade, "notes", None) or [])]
    side = getattr(trade, "side", "")
    if side not in ["LONG", "SHORT"]:
        return None
    opp = opposite(side)

    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    tf1h = context.get("tf1h") or {}
    tf4h = context.get("tf4h") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}

    flagged_countertrend = any("COUNTERTREND_ENTRY" in n or "КОНТРТРЕНД" in n for n in notes)
    htf_against_now = bool(tf1h.get("bias") == opp or tf4h.get("bias") == opp)
    is_countertrend = bool(flagged_countertrend or ("RISKY_ENTRY" in notes and htf_against_now))
    if not is_countertrend:
        return None

    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        elapsed_min = max(0.0, (now_utc() - opened_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_min = 0.0

    # Confirmation means the reversal started to work, not merely that it is not stopped.
    confirms = []
    warnings = []
    if current_pct >= 0.18:
        confirms.append("ціна вже утримується в плюс")
    if best_pct >= 0.28:
        confirms.append("угода дала достатній MFE для раннього розвороту")
    if tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 24:
        confirms.append("3M підтримує напрям")
    if tf15.get("bias") == side:
        confirms.append("15M підтверджує")
    if structure.get("bias") == side:
        confirms.append("структура підтримує")
    if ict.get("bias") == side and (ict.get("entry_ok") or abs(int(ict.get("score", 0) or 0)) >= 16):
        confirms.append("ICT підтримує")

    if current_pct <= -0.18:
        warnings.append("ціна проти входу")
    if best_pct < 0.18 and elapsed_min >= 14:
        warnings.append("після входу немає нормального розкриття")
    if tf3_against or tf3.get("bias") == opp:
        warnings.append("3M вже проти")
    if structure_against or structure.get("bias") == opp:
        warnings.append("структура проти")
    if ict_against or (ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16):
        warnings.append("ICT проти")
    if flow_against and cvd_against:
        warnings.append("CVD і потік проти")

    # First follow after 15 minutes is the important user-facing checkpoint.
    if len(confirms) >= 3 and current_pct > -0.05:
        status = "ПІДТВЕРДЖУЄТЬСЯ ✅"
        advice = "можна залишатися в угоді; далі супровід як звичайна позиція, але стоп обовʼязковий"
        severity = "OK"
        score = 28
    elif (len(warnings) >= 3 and len(confirms) <= 1) or (current_pct <= -0.28 and best_pct < 0.25):
        status = "НЕ ПІДТВЕРДЖУЄТЬСЯ ⚠️"
        advice = "контртрендовий розворот не розкрився; краще захистити/закрити біля входу або не чекати повний стоп"
        severity = "BAD"
        score = 72
    elif len(warnings) >= 2 and len(confirms) <= 2:
        status = "СЛАБКЕ ПІДТВЕРДЖЕННЯ ⚠️"
        advice = "залишатися тільки якщо наступний супровід покаже покращення; не розширювати стоп"
        severity = "WARN"
        score = 56
    else:
        status = "ЩЕ ПЕРЕВІРЯЄТЬСЯ"
        advice = "чекати наступне підтвердження; якщо ціна не дає MFE і 3M стане проти — виходити раніше"
        severity = "WATCH"
        score = 42

    return {
        "active": True,
        "status": status,
        "advice": advice,
        "severity": severity,
        "score": score,
        "elapsed_min": round(elapsed_min, 1),
        "confirmations": confirms[:3],
        "warnings": warnings[:3],
    }


def _trade_open_elapsed_minutes(trade):
    try:
        opened = datetime.fromisoformat(str(getattr(trade, "opened_at", "")).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc() - opened).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _candle_ts_to_dt(ts):
    try:
        ts = float(ts)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _candles_after_trade_open(trade, candles):
    try:
        opened = datetime.fromisoformat(str(getattr(trade, "opened_at", "")).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
    except Exception:
        opened = None
    out = []
    for c in (candles or []):
        dt = _candle_ts_to_dt(getattr(c, "ts", None))
        if opened is None or dt is None or dt >= opened:
            out.append(c)
    return out


def _close_mfe_mae_after_open(trade, context, current_price=None):
    """Close-based MFE/MAE for phase management.

    Wick MFE is useful for TP/stop detection, but professional supervision should
    not tighten or close too early from a single wick. This helper separates:
    - close_mfe_pct: best favorable candle close after entry;
    - confirmed_mfe_pct: close MFE confirmed by at least two favorable closes;
    - close_mae_pct: worst adverse candle close after entry.
    """
    side = getattr(trade, "side", "")
    entry = safe_float(getattr(trade, "entry", None), 0.0) or 0.0
    if side not in ["LONG", "SHORT"] or not entry:
        return {"close_mfe_pct": 0.0, "confirmed_mfe_pct": 0.0, "close_mae_pct": 0.0, "follow_closes": 0}
    candles = _candles_after_trade_open(trade, (context or {}).get("candles_3m") or [])
    vals = []
    for c in candles:
        cl = safe_float(getattr(c, "close", None), None)
        if cl:
            vals.append(signed_pct(side, entry, cl))
    cp = safe_float(current_price if current_price is not None else (context or {}).get("price"), None)
    if cp:
        vals.append(signed_pct(side, entry, cp))
    if not vals:
        return {"close_mfe_pct": 0.0, "confirmed_mfe_pct": 0.0, "close_mae_pct": 0.0, "follow_closes": 0}
    close_mfe = max(0.0, max(vals))
    close_mae = abs(min(0.0, min(vals)))
    # Confirmed MFE: do not treat a single close or wick as a stable expansion.
    confirmation_floor = max(0.10, close_mfe * 0.55)
    favorable_closes = sum(1 for v in vals if v >= confirmation_floor)
    if favorable_closes >= 2:
        confirmed = close_mfe
    elif close_mfe >= 0.35 and vals[-1] >= close_mfe * 0.55:
        confirmed = close_mfe * 0.75
    else:
        confirmed = min(close_mfe, close_mfe * 0.45)
    return {
        "close_mfe_pct": round(close_mfe, 3),
        "confirmed_mfe_pct": round(max(0.0, confirmed), 3),
        "close_mae_pct": round(close_mae, 3),
        "follow_closes": len(vals),
    }


def _phase_label_name(phase):
    return {
        "ENTRY_VALIDATION": "валідація входу",
        "EXPANSION": "розвиток імпульсу",
        "PROFIT_PROTECTION": "захист прибутку",
        "CONTINUATION_OR_EXIT": "продовження / вихід",
    }.get(str(phase or ""), "супровід")


def analyze_trade_phase(trade, context, current_pct, best_pct, giveback,
                        high_since_open=None, low_since_open=None,
                        entry_state=None, trade_validation=None,
                        support_votes=0.0, opposite_votes=0.0,
                        action="HOLD", market_regime=None,
                        tf3_against=False, structure_against=False, ict_against=False,
                        flow_against=False, cvd_against=False, liquidity_against=False,
                        news_against=False, confirmed_ict_reversal=False,
                        closed=False, exit_reason_code=""):
    """MFE/MAE Intelligence: professional phase model for open trade supervision.

    It does not replace the stop. It explains and soft-gates decisions by phase:
    1) ENTRY_VALIDATION      — first 1–3 3M candles after entry;
    2) EXPANSION             — price should start moving in our direction;
    3) PROFIT_PROTECTION     — meaningful MFE exists; avoid giving it back;
    4) CONTINUATION_OR_EXIT  — decide whether the setup still deserves TP2/TP3.
    """
    side = getattr(trade, "side", "NEUTRAL")
    opp = opposite(side) if side in ["LONG", "SHORT"] else "NEUTRAL"
    price = safe_float((context or {}).get("price"), safe_float(getattr(trade, "entry", None), 0.0)) or 0.0
    entry = safe_float(getattr(trade, "entry", 0), 0.0) or 0.0
    stop_initial = safe_float(getattr(trade, "stop_initial", getattr(trade, "stop_current", 0)), 0.0) or 0.0
    tp1 = safe_float(getattr(trade, "tp1", 0), 0.0) or 0.0
    risk_distance_pct = abs(pct(stop_initial, entry)) if entry and stop_initial else 0.65
    risk_distance_pct = max(0.10, risk_distance_pct)
    tp1_distance_pct = abs(pct(tp1, entry)) if entry and tp1 else 0.0
    elapsed_min = _trade_open_elapsed_minutes(trade)
    close_stats = _close_mfe_mae_after_open(trade, context, price)
    close_mfe = safe_float(close_stats.get("close_mfe_pct"), 0.0) or 0.0
    confirmed_mfe = safe_float(close_stats.get("confirmed_mfe_pct"), 0.0) or 0.0
    close_mae = safe_float(close_stats.get("close_mae_pct"), 0.0) or 0.0

    if side == "LONG":
        adverse_extreme = safe_float(low_since_open, min(price, entry))
        wick_mae = max(0.0, (entry - adverse_extreme) / entry * 100.0) if entry else close_mae
    elif side == "SHORT":
        adverse_extreme = safe_float(high_since_open, max(price, entry))
        wick_mae = max(0.0, (adverse_extreme - entry) / entry * 100.0) if entry else close_mae
    else:
        wick_mae = close_mae
    mae_pct = round(max(close_mae, wick_mae), 3)
    mae_risk_used = round(mae_pct / risk_distance_pct, 3) if risk_distance_pct else 0.0

    best_pct = safe_float(best_pct, 0.0) or 0.0
    current_pct = safe_float(current_pct, 0.0) or 0.0
    giveback = max(0.0, safe_float(giveback, best_pct - current_pct) or 0.0)
    giveback_ratio = giveback / best_pct if best_pct > 0.10 else 0.0
    regime = str(market_regime or (trade_mode_profile(context or {}, side).get("regime") if side in ["LONG", "SHORT"] else "NORMAL") or "NORMAL").upper()

    phase = "ENTRY_VALIDATION"
    if getattr(trade, "tp1_hit", False) or (tp1_distance_pct and best_pct >= tp1_distance_pct * 0.85):
        phase = "CONTINUATION_OR_EXIT"
    elif best_pct >= max(0.60, risk_distance_pct * 0.70) or confirmed_mfe >= max(0.45, risk_distance_pct * 0.50):
        phase = "PROFIT_PROTECTION"
    elif elapsed_min > 12 or close_stats.get("follow_closes", 0) >= 4:
        phase = "EXPANSION"

    hard_against = sum([bool(structure_against), bool(ict_against), bool(confirmed_ict_reversal)])
    soft_against = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(liquidity_against), bool(news_against)])
    support_ok = bool(support_votes >= max(1.8, opposite_votes) or ((context or {}).get("tf15") or {}).get("bias") == side)
    entry_state_name = str((entry_state or {}).get("state") or "")
    validation_severity = str((trade_validation or {}).get("severity") or "")

    # Exit permission: never allow one weak metric to close by itself.
    exit_permission = False
    if confirmed_ict_reversal or hard_against >= 2:
        exit_permission = True
    elif phase == "ENTRY_VALIDATION":
        exit_permission = bool(mae_risk_used >= 0.70 and (hard_against >= 1 or soft_against >= 2))
    elif phase == "EXPANSION":
        exit_permission = bool(
            (entry_state_name == "BROKEN" and (hard_against >= 1 or soft_against >= 2))
            or (validation_severity == "BAD" and current_pct <= -0.18 and soft_against >= 2)
            or (best_pct < 0.18 and elapsed_min >= 30 and opposite_votes >= 2.2 and support_votes <= 1.2)
        )
    elif phase == "PROFIT_PROTECTION":
        # Giveback is actionable only when plus was real enough and weakness is confirmed.
        allowed = 0.72 if regime in ["TREND", "NEWS_IMPULSE"] else 0.62
        exit_permission = bool(giveback_ratio >= allowed and (hard_against >= 1 or soft_against >= 3) and not support_ok)
    elif phase == "CONTINUATION_OR_EXIT":
        allowed = 0.62 if regime in ["TREND", "NEWS_IMPULSE"] else 0.52
        exit_permission = bool(
            confirmed_ict_reversal
            or (giveback_ratio >= allowed and (hard_against >= 1 or soft_against >= 2) and not support_ok)
            or (entry_state_name == "BROKEN" and current_pct <= 0.18 and hard_against >= 1)
        )
    if str(action or "").startswith("TP") or str(action or "") == "STOP":
        exit_permission = True

    # Human title / status.
    action_s = str(action or "")
    title = f"🟡 СУПРОВІД {side} — ПОТРІБНЕ ПРОДОВЖЕННЯ"
    status = "NEEDS_CONTINUATION"
    if closed or action_s in ["STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK", "EXIT_ENTRY_POINT_BROKEN", "EXIT_AFTER_TP1_GIVEBACK", "EXIT_RISKY_PRE_TP1_BE_GUARD", "EXIT_RISKY_PRE_TP1_RISK_FLOOR", "EXIT_AFTER_TP2_PROFIT_PROTECT", "EXIT"]:
        status = "EDGE_LOST"
        if action_s.startswith("TP"):
            title = f"🔵 {side} ЗАКРИТО — ЦІЛЬ ВИКОНАНА"
            status = "TARGET_DONE"
        elif action_s == "STOP":
            title = f"🔴 {side} ЗАКРИТО — СТОП СПРАЦЮВАВ"
        elif best_pct >= 0.35 and current_pct >= -0.05:
            title = f"🔴 {side} ЗАКРИТО — ПРОДОВЖЕННЯ НЕ ПІДТВЕРДИЛОСЬ"
        else:
            title = f"🔴 {side} ЗАКРИТО — ПЕРЕВАГА ВТРАЧЕНА"
    elif action_s.startswith("TP"):
        title = f"🔵 СУПРОВІД {side} — ЦІЛЬ ВЗЯТО, ВЕСТИ ЗАЛИШОК"
        status = "TARGET_PROTECTION"
    elif phase == "ENTRY_VALIDATION":
        if current_pct >= 0.12 and not (hard_against or soft_against >= 2):
            title = f"🟢 СУПРОВІД {side} — ВХІД ПІДТВЕРДЖУЄТЬСЯ"
            status = "ENTRY_VALIDATING_POSITIVE"
        else:
            title = f"🟡 СУПРОВІД {side} — ЙДЕ ПЕРЕВІРКА ВХОДУ"
            status = "ENTRY_VALIDATION"
    elif phase == "EXPANSION":
        if current_pct > 0 and (support_votes >= opposite_votes or confirmed_mfe >= 0.25) and not hard_against:
            title = f"🟢 СУПРОВІД {side} — ІМПУЛЬС ПІДТВЕРДЖУЄТЬСЯ"
            status = "EXPANSION_CONFIRMED"
        elif entry_state_name in ["WEAK", "BROKEN"] or soft_against >= 2:
            title = f"🟠 СУПРОВІД {side} — СЕТАП СЛАБШАЄ"
            status = "SETUP_WEAKENING"
        else:
            title = f"🟡 СУПРОВІД {side} — ПОТРІБНЕ ПРОДОВЖЕННЯ"
            status = "NEEDS_CONTINUATION"
    elif phase == "PROFIT_PROTECTION":
        if giveback_ratio >= 0.55 or action_s in ["PROTECT", "PROTECT_OR_EXIT", "EXIT_WARNING"]:
            title = f"🟠 СУПРОВІД {side} — ПРИБУТОК СЛАБШАЄ"
            status = "PROFIT_WEAKENING"
        else:
            title = f"🟢 СУПРОВІД {side} — ПРИБУТОК ПІД КОНТРОЛЕМ"
            status = "PROFIT_CONTROLLED"
    elif phase == "CONTINUATION_OR_EXIT":
        if exit_permission or action_s in ["PROTECT_OR_EXIT", "EXIT_WARNING"]:
            title = f"🟠 СУПРОВІД {side} — ПЕРЕВАГА СЛАБШАЄ"
            status = "EDGE_WEAKENING"
        else:
            title = f"🟢 СУПРОВІД {side} — ПРОДОВЖЕННЯ ЩЕ АКТУАЛЬНЕ"
            status = "CONTINUATION_VALID"

    # One concise reason for Telegram. Full diagnostics stay in the journal.
    if closed or action_s in ["STOP", "EXIT_MFE_GIVEBACK", "EXIT_GIVEBACK", "EXIT_LOCAL_BREAK", "EXIT_ENTRY_POINT_BROKEN", "EXIT_AFTER_TP1_GIVEBACK", "EXIT_RISKY_PRE_TP1_BE_GUARD", "EXIT_RISKY_PRE_TP1_RISK_FLOOR", "EXIT_AFTER_TP2_PROFIT_PROTECT", "EXIT"]:
        if action_s == "STOP":
            reason = "стопова зона була зачеплена; сценарій закрито за планом."
        elif best_pct >= 0.35 and current_pct >= -0.05:
            reason = f"угода давала +{round(best_pct, 3)}% MFE, але продовження {side} не підтвердилось; вихід біля входу замість очікування дальнього стопа."
        else:
            reason = f"перевага {side} втрачена: структура/імпульс не дали продовження, ризик утримання став вищий за потенціал."
    elif phase == "ENTRY_VALIDATION":
        reason = f"перші 1–3 свічки після входу ще перевіряють точку; MFE {round(best_pct, 3)}%, MAE {round(mae_pct, 3)}%."
        if hard_against or soft_against >= 2:
            reason += " Є ранні попередження, але без повного зламу це ще не самостійна причина для виходу."
    elif phase == "EXPANSION":
        if status == "EXPANSION_CONFIRMED":
            reason = "ціна утримує напрям після входу; 3M закриття підтверджують рух, структура ще не зламана."
        elif status == "SETUP_WEAKENING":
            reason = f"після входу немає достатнього розвитку: MFE {round(best_pct, 3)}%, ціна повертається до входу, частина підтверджень вже проти."
        else:
            reason = f"угода ще не дала достатнього розвитку до TP1; потрібне продовження імпульсу без зламу 3M/ICT."
    elif phase == "PROFIT_PROTECTION":
        if status == "PROFIT_WEAKENING":
            reason = f"угода вже давала +{round(best_pct, 3)}% MFE, зараз {round(current_pct, 3)}%; віддано {round(giveback_ratio*100, 1)}% руху."
        else:
            reason = f"MFE вже змістовний (+{round(best_pct, 3)}%), але ціна ще утримує робочу зону; без confirmed-розвороту даємо простір."
    else:
        if status == "CONTINUATION_VALID":
            reason = "після першого руху структура/фон ще не зламали сценарій; є підстава вести позицію до наступної цілі."
        else:
            reason = f"після MFE +{round(best_pct, 3)}% перевага слабшає: потрібне відновлення структури, інакше краще не віддавати рух назад."

    return {
        "phase": phase,
        "phase_label": _phase_label_name(phase),
        "status": status,
        "telegram_title": title,
        "telegram_reason": reason,
        "exit_permission": bool(exit_permission),
        "elapsed_min": round(elapsed_min, 1),
        "current_pct": round(current_pct, 3),
        "best_pct": round(best_pct, 3),
        "close_mfe_pct": round(close_mfe, 3),
        "confirmed_mfe_pct": round(confirmed_mfe, 3),
        "mae_pct": round(mae_pct, 3),
        "mae_risk_used": mae_risk_used,
        "giveback_pct": round(giveback, 3),
        "giveback_ratio": round(giveback_ratio, 3),
        "support_votes": round(float(support_votes or 0.0), 2),
        "opposite_votes": round(float(opposite_votes or 0.0), 2),
        "hard_against": int(hard_against),
        "soft_against": int(soft_against),
    }

def _active_trade_setup_type(trade):
    value = str(getattr(trade, "setup_type", "") or "").strip()
    if value:
        return value.upper()
    return str(_state_note_value(getattr(trade, "notes", []) or [], "SETUP_CLASSIFIER") or "").upper()


def closed_mtf_break_snapshot(side, context, confirmed_ict_reversal=False):
    """Hard exit evidence from completed 15M/HTF structure.

    Live 1H/4H candles are intentionally warning-only; they cannot independently
    close a trade at the start of a new hour/four-hour candle.
    """
    tf15 = (context or {}).get("tf15") or {}
    structure = (context or {}).get("structure") or {}
    ict = (context or {}).get("ict") or {}
    tf3 = (context or {}).get("tf3") or {}
    guard = (context or {}).get("mtf_guard") or {}
    opp = opposite(side)

    tf15_against = tf15.get("bias") == opp and abs(int(tf15.get("score", 0) or 0)) >= 26
    structure_against = structure.get("bias") == opp and abs(int(structure.get("score", 0) or 0)) >= 18
    ict_against = ict.get("bias") == opp and abs(int(ict.get("score", 0) or 0)) >= 16
    tf3_against = tf3.get("bias") == opp and abs(int(tf3.get("score", 0) or 0)) >= 42

    closed_15m_break = bool(
        confirmed_ict_reversal
        or (tf15_against and (structure_against or ict_against))
        or (structure_against and ict_against)
    )
    live1h = guard.get("live_1h") or {}
    live4h = guard.get("live_4h") or {}
    live_htf_against = bool(
        (live1h.get("active") and live1h.get("bias") == opp)
        or (live4h.get("active") and live4h.get("bias") == opp)
    )
    return {
        "closed_15m_break": closed_15m_break,
        "tf15_against": tf15_against,
        "structure_against": structure_against,
        "ict_against": ict_against,
        "tf3_against": tf3_against,
        "live_htf_against_warning": live_htf_against,
        "new_1h_boundary": bool(guard.get("new_1h_boundary")),
        "new_4h_boundary": bool(guard.get("new_4h_boundary")),
    }


def _setup_recovery_snapshot(trade, context, break_snapshot):
    side = trade.side
    setup_type = _active_trade_setup_type(trade)
    regime = (context or {}).get("regime_engine") or (context or {}).get("market_regime") or {}
    regime_type = str(regime.get("regime_type") or regime.get("name") or "").upper()
    regime_bias = str((regime.get("metrics") or {}).get("bias") or "").upper()
    tf3_same = ((context or {}).get("tf3") or {}).get("bias") == side
    tf15_same = ((context or {}).get("tf15") or {}).get("bias") == side
    structure_same = ((context or {}).get("structure") or {}).get("bias") == side
    ict_same = ((context or {}).get("ict") or {}).get("bias") == side
    closed_support = tf15_same or structure_same or ict_same
    regime_same = regime_bias == side and regime_type in ["TREND_PULLBACK", "TREND_EXPANSION", "NORMAL"]
    pullback_recovered = setup_type in ["PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY"] and regime_type == "TREND_PULLBACK" and regime_bias == side
    hard_break = bool((break_snapshot or {}).get("closed_15m_break"))
    strong_recovery = bool(not hard_break and tf3_same and closed_support)
    soft_recovery = bool(not hard_break and (pullback_recovered or (regime_same and (tf3_same or closed_support))))
    return {
        "strong": strong_recovery,
        "soft": soft_recovery,
        "pullback_recovered": pullback_recovered,
        "regime_type": regime_type,
    }


def setup_aware_exit_decision(trade, context, entry_state, phase_snapshot, current_pct,
                              tf3_against=False, flow_against=False, cvd_against=False,
                              confirmed_ict_reversal=False):
    """Choose early-exit strictness according to the setup that opened the trade."""
    setup_type = _active_trade_setup_type(trade)
    streak = int(getattr(trade, "entry_fail_streak", 0) or 0)
    break_snapshot = closed_mtf_break_snapshot(trade.side, context, confirmed_ict_reversal)
    hard_break = bool(break_snapshot.get("closed_15m_break"))
    phase_exit = bool((phase_snapshot or {}).get("exit_permission"))
    auto_exit = bool((entry_state or {}).get("auto_exit"))
    soft_against_count = sum([bool(tf3_against), bool(flow_against), bool(cvd_against)])

    pullback_family = setup_type in ["PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "TREND_CONTINUATION", "CLOSED_15M_DIRECTION_FLIP"]
    fast_family = setup_type in ["COUNTERTREND_SCALP", "NEWS_IMPULSE", "SWEEP_RECLAIM_EARLY_ENTRY", "RANGE_COMPRESSION_BREAKOUT"]
    medium_family = setup_type in ["TREND_IGNITION_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE", "SWEEP_REVERSAL", "CLOSED_15M_DIRECTION_FLIP"]

    close = False
    reason = ""
    if pullback_family:
        # A pullback setup is expected to retest and fluctuate. Streak alone is
        # never enough: require completed 15M/ICT/SMC break.
        close = bool(hard_break and (auto_exit or streak >= 1 or current_pct <= -0.35))
        reason = "для відкатного/трендового сетапу потрібен підтверджений злам закритою 15M + ICT/структурою"
    elif fast_family:
        close = bool(
            hard_break
            or (auto_exit and soft_against_count >= 2 and phase_exit)
            or (streak >= 2 and soft_against_count >= 2 and current_pct <= 0.05 and phase_exit)
        )
        reason = "ранній/контртрендовий сетап контролюється швидше, але не лише незакритою HTF-свічкою"
    elif medium_family:
        close = bool(
            hard_break
            or (auto_exit and break_snapshot.get("tf15_against") and phase_exit)
            or (streak >= 3 and break_snapshot.get("tf15_against") and current_pct <= 0.10 and phase_exit)
        )
        reason = "для раннього ICT/Trend Ignition потрібне закрите 15M-підтвердження або повний ICT/SMC злам"
    else:
        close = bool(
            hard_break
            or (auto_exit and break_snapshot.get("tf15_against") and phase_exit)
            or (streak >= 3 and break_snapshot.get("tf15_against") and current_pct <= 0.18 and phase_exit)
        )
        reason = "універсальний вихід вимагає закритої 15M або підтвердженого ICT/SMC зламу"

    return {
        "close": close,
        "setup_type": setup_type or "UNKNOWN",
        "reason": reason,
        "break_snapshot": break_snapshot,
        "streak": streak,
        "warning_only": bool((entry_state or {}).get("state") in ["BROKEN", "WEAK"] and not close),
    }




def _quality_components_for_setup(setup, context, snapshot):
    """Transparent 4-pillar quality decomposition for journal/audit."""
    setup_info = (setup or {}).get("setup_classifier") or (context or {}).get("setup_classifier") or {}
    ict = (context or {}).get("ict") or {}
    structure = (context or {}).get("structure") or {}
    plan = (setup or {}).get("plan")
    location = 8
    if snapshot.get("professional_location"):
        location += 10
    if ict.get("entry_ok"):
        location += 5
    if structure.get("bias") == (setup or {}).get("side"):
        location += 2
    direction = int(clamp(8 + snapshot.get("closed_support", 0) * 4 - snapshot.get("closed_against", 0) * 3, 0, 25))
    trigger = int(clamp(7 + snapshot.get("fast_support", 0) * 5 + snapshot.get("live_support", 0) * 2 - snapshot.get("fast_against", 0) * 3, 0, 25))
    risk = 10
    try:
        rr1 = safe_float(plan.get("rr1") if isinstance(plan, dict) else getattr(plan, "rr1", None), 0) if plan else 0
        rr2 = safe_float(plan.get("rr2") if isinstance(plan, dict) else getattr(plan, "rr2", None), 0) if plan else 0
        if rr1 >= TARGET_RR1_ENTRY:
            risk += 8
        elif rr1 >= MIN_RR1_ENTRY:
            risk += 5
        if rr2 >= MIN_RR2_ENTRY:
            risk += 4
        if not (context or {}).get("price_warning"):
            risk += 3
    except Exception:
        pass
    return {
        "ict_location": int(clamp(location, 0, 25)),
        "direction_regime": int(clamp(direction, 0, 25)),
        "entry_trigger": int(clamp(trigger, 0, 25)),
        "risk_rr": int(clamp(risk, 0, 25)),
        "setup_type": str(setup_info.get("type") or "UNKNOWN"),
    }



def apply_professional_lifecycle_to_setup(setup, context):
    """Final canonical entry decision shared by quality and Dual-Speed layers.

    The old score is no longer allowed to say ENTRY while lifecycle silently says
    RISKY. One consensus gate combines base quality, closed thesis, fast trigger,
    reversal bridge and plan geometry, then writes the final action once.
    """
    if not isinstance(setup, dict):
        return setup
    out = dict(setup)
    side = str(out.get("side") or "NEUTRAL").upper()
    if side not in ["LONG", "SHORT"]:
        out["lifecycle"] = {"stage": "BLOCKED" if out.get("entry_level") == "BLOCK" else "WATCH", "score": 0}
        return out

    plan = out.get("plan")
    plan_valid = bool(plan and getattr(plan, "valid", False))
    snapshot = ((context or {}).get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side)
    base_action = str(out.get("action") or "WATCH").upper()
    base_level = str(out.get("entry_level") or ("ENTRY" if base_action == "ENTRY" else "RISKY_ENTRY" if base_action == "RISKY_ENTRY" else "WATCH_TRIGGER")).upper()
    setup_info = out.get("setup_classifier") or {}
    setup_type = str(setup_info.get("type") or "").upper()
    geometry_grade = str(getattr(plan, "geometry_grade", "OPTIMAL") or "OPTIMAL").upper() if plan else "INVALID"
    regime_info = (context or {}).get("regime_engine") or (context or {}).get("market_regime") or {}
    regime_risky_only = bool(isinstance(regime_info, dict) and str(regime_info.get("entry_action") or "").upper() == "RISKY_ONLY")
    event_risky_only = bool(((context or {}).get("calendar") or {}).get("active"))
    rejection_guard = post_impulse_rejection_snapshot(side, context)
    recovery_quality = rejection_guard.get("recovery_quality") or {}
    post_rejection_entry = bool(
        rejection_guard.get("active")
        and rejection_guard.get("fresh_recovery")
        and recovery_quality.get("passed")
    )
    forced_risky = bool(setup_info.get("force_risky")) or regime_risky_only or event_risky_only or rejection_guard.get("force_risky") or geometry_grade != "OPTIMAL" or setup_type in {
        "TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION_FAST_ENTRY", "PRIME_ICT_LOCATION_OVERRIDE",
        "SWEEP_RECLAIM_EARLY_ENTRY", "COUNTERTREND_SCALP", "NEWS_IMPULSE", "RANGE_COMPRESSION_BREAKOUT",
    }

    bridge = snapshot.get("fast_reversal_to_side") or {}
    full_consensus = bool(
        plan_valid
        and snapshot.get("closed_confirmed")
        and snapshot.get("fast_trigger")
        and not snapshot.get("emergency_against")
        and not forced_risky
        and not rejection_guard.get("block_entry")
    )
    breakout_geometry = bool(plan and str(getattr(plan, "barrier_mode", "") or "").upper() == "BREAKOUT_GATE")
    early_consensus = bool(
        plan_valid
        and (snapshot.get("professional_location") or breakout_geometry)
        and (snapshot.get("fast_trigger") or bridge.get("confirmed"))
        and not snapshot.get("emergency_against")
    )
    reversal_types = {"SWEEP_REVERSAL", "SWEEP_RECLAIM_EARLY_ENTRY", "COUNTERTREND_SCALP", "PRIME_ICT_LOCATION_OVERRIDE"}
    reversal_fast_path = bool(
        plan_valid
        and setup_type in reversal_types
        and bridge.get("confirmed")
        and int(bridge.get("score", 0) or 0) >= FAST_REVERSAL_MIN_SCORE
        and not out.get("hard_no_chase")
        and not out.get("regime_gate")
        and not out.get("setup_gate")
        and base_level != "BLOCK"
    )

    base_quality = int(out.get("quality", 0) or 0)
    dual_quality = int(snapshot.get("confidence", 50) or 50)
    geometry_bonus = 0
    if plan_valid:
        rr1 = safe_float(getattr(plan, "rr1", 0), 0) or 0
        if rr1 >= 2.0:
            geometry_bonus = 4
        elif rr1 >= TARGET_RR1_ENTRY:
            geometry_bonus = 2
    geometry_penalty = 0 if geometry_grade == "OPTIMAL" else (2 if "GATE_EXPANSION" in geometry_grade else 5)
    unified_quality = int(clamp(round(base_quality * 0.72 + dual_quality * 0.28) + geometry_bonus - geometry_penalty, 0, 92))

    final_action = base_action
    stage = "WATCH_TRIGGER"
    explicit_block = bool(
        out.get("hard_no_chase") or out.get("exhausted_move") or out.get("regime_gate")
        or out.get("setup_gate") or out.get("entry_blocked") or base_level == "BLOCK"
        or rejection_guard.get("block_entry")
    )
    recovery_gate_failed = bool(rejection_guard.get("active") and not post_rejection_entry)
    if recovery_gate_failed:
        final_action = "WATCH"
        stage = "WATCH_RETEST"
        out["reason"] = (recovery_quality.get("reason") or rejection_guard.get("reason") or
                         "після відхилення потрібен новий закритий 3M сетап із follow-through і потоком")
        out["show_wait_plan"] = False
    elif rejection_guard.get("block_entry"):
        final_action = "WATCH"
        stage = "WATCH_RETEST"
        out["reason"] = rejection_guard.get("reason")
        out["show_wait_plan"] = False
    elif not plan_valid and base_action in ["ENTRY", "RISKY_ENTRY"]:
        final_action = "WATCH"
        stage = "BLOCKED"
        out["reason"] = getattr(plan, "validation_reason", "") or "технічний план не пройшов перевірку"
        out["show_wait_plan"] = False
    elif base_action == "ENTRY":
        if full_consensus and not forced_risky:
            final_action = "ENTRY"
            stage = "CONFIRMED_ENTRY"
        elif early_consensus:
            final_action = "RISKY_ENTRY"
            stage = "EARLY_CONFIRMED" if bridge.get("confirmed") else "EARLY_ENTRY"
        else:
            final_action = "WATCH"
            stage = "WATCH_TRIGGER"
    elif base_action == "RISKY_ENTRY":
        if full_consensus or early_consensus:
            final_action = "RISKY_ENTRY"
            stage = "EARLY_CONFIRMED"
        else:
            final_action = "WATCH"
            stage = "WATCH_TRIGGER"
    elif base_action in ["WATCH", "WAIT"] and plan_valid and not explicit_block and setup_info.get("entry_allowed"):
        # One canonical consensus may activate an otherwise valid WATCH. This
        # prevents Quality, Geometry and Dual-Speed from vetoing each other in
        # sequence after all professional requirements are already present.
        if full_consensus and not forced_risky and unified_quality >= ENTRY_QUALITY_MIN:
            final_action = "ENTRY"
            stage = "CONFIRMED_ENTRY"
        elif (early_consensus or reversal_fast_path) and unified_quality >= RISKY_QUALITY_MIN:
            final_action = "RISKY_ENTRY"
            stage = "EARLY_CONFIRMED"
            if reversal_fast_path:
                out["reason"] = out.get("reason") or "сильний ранній розворот підтверджений 3M + ICT/структурою + потоком"

    if final_action == "ENTRY":
        out["action"] = "ENTRY"
        out["entry_level"] = "ENTRY"
        out["entry_level_label"] = _entry_level_label("ENTRY")
        out["quality"] = max(ENTRY_QUALITY_MIN, unified_quality)
    elif final_action == "RISKY_ENTRY":
        out["action"] = "RISKY_ENTRY"
        out["entry_level"] = "RISKY_ENTRY"
        out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["quality"] = int(max(RISKY_QUALITY_MIN, min(79, unified_quality)))
    else:
        out["action"] = "WATCH"
        out["entry_level"] = "WATCH_TRIGGER" if plan_valid else "BLOCK"
        out["entry_level_label"] = _entry_level_label(out["entry_level"])
        out["quality"] = int(min(67, unified_quality))

    if post_rejection_entry and out.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        event = rejection_guard.get("fresh_recovery_event") or {}
        out["action"] = "RISKY_ENTRY"
        out["entry_level"] = "RISKY_ENTRY"
        out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
        out["quality"] = int(min(79, max(RISKY_QUALITY_MIN, out.get("quality", RISKY_QUALITY_MIN))))
        out["post_rejection_recovery"] = {
            "active": True,
            "trigger_ts": int(event.get("trigger_ts", 0) or 0),
            "trigger_level": round_price(event.get("trigger_level")),
            "event_type": str(event.get("type") or ""),
            "quality": recovery_quality,
        }
        stage = "RECOVERY_VALIDATION"

    components = _quality_components_for_setup(out, context, snapshot)
    risk_fraction = 1.0 if out["action"] == "ENTRY" else (0.5 if out["action"] == "RISKY_ENTRY" else 0.0)
    out["lifecycle"] = {
        "stage": stage,
        "score": unified_quality,
        "entry_level": out.get("entry_level"),
        "suggested_risk_fraction": risk_fraction,
        "closed_confirmed": bool(snapshot.get("closed_confirmed")),
        "fast_trigger": bool(snapshot.get("fast_trigger")),
        "professional_location": bool(snapshot.get("professional_location")),
        "fast_reversal_bridge": bridge,
        "quality_components": components,
        "decision_source": "UNIFIED_ENTRY_CONSENSUS",
        "geometry_grade": geometry_grade,
        "barrier_mode": str(getattr(plan, "barrier_mode", "") or "") if plan else "",
        "regime_risky_only": regime_risky_only,
        "event_risky_only": event_risky_only,
        "post_impulse_rejection": rejection_guard,
    }
    out["dual_speed_snapshot"] = snapshot
    return out


def update_trade_lifecycle(trade, context, current_pct, best_pct, phase_snapshot, break_snapshot,
                           support_votes=0.0, opposite_votes=0.0):
    side = getattr(trade, "side", "")
    snapshot = ((context or {}).get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side)
    old_stage = str(getattr(trade, "lifecycle_stage", "ENTRY_VALIDATION") or "ENTRY_VALIDATION")
    hard_break = bool((break_snapshot or {}).get("closed_15m_break"))
    reversal_against = snapshot.get("fast_reversal_against") or {}
    if hard_break or (reversal_against.get("emergency") and current_pct <= -0.20):
        stage = "INVALIDATED"
    elif getattr(trade, "tp2_hit", False):
        stage = "CONTINUATION_TO_TP3"
    elif getattr(trade, "tp1_hit", False):
        stage = "PROFIT_PROTECTION"
    elif best_pct >= 0.45 and current_pct >= 0.05:
        stage = "WORKING"
    elif snapshot.get("closed_confirmed") and snapshot.get("fast_trigger"):
        stage = "CONFIRMED"
    elif reversal_against.get("confirmed") or snapshot.get("emergency_against") or (opposite_votes >= 2.6 and current_pct < 0):
        stage = "UNDER_PRESSURE"
    elif str(getattr(trade, "entry_level", "")).upper() == "RISKY_ENTRY":
        stage = "EARLY_VALIDATION"
    else:
        stage = "ENTRY_VALIDATION"

    confirmation = int(snapshot.get("closed_confirmed")) + int(snapshot.get("fast_trigger")) + int(support_votes >= 2.0)
    failure = int(snapshot.get("live_pressure")) + int(snapshot.get("emergency_against")) + int(opposite_votes > support_votes + 0.8) + int(reversal_against.get("confirmed"))
    score = int(clamp(50 + confirmation * 14 - failure * 14 + max(-12, min(12, current_pct * 10)), 0, 100))
    if stage != old_stage:
        trade.lifecycle_last_transition = iso_now()
    trade.lifecycle_stage = stage
    trade.lifecycle_score = score
    trade.lifecycle_confirmations = confirmation
    trade.lifecycle_failures = failure
    return {
        "stage": stage,
        "previous_stage": old_stage,
        "score": score,
        "confirmations": confirmation,
        "failures": failure,
        "dual_speed": snapshot,
        "fast_reversal_against": reversal_against,
        "phase": (phase_snapshot or {}).get("phase"),
    }


def decision_coherence_guard(trade, context, candidate_action, current_pct, best_pct,
                             giveback_ratio=0.0, phase_snapshot=None,
                             confirmed_ict_reversal=False, profit_exit=False):
    """Consistency gate with a faster but strict reversal path."""
    action = str(candidate_action or "").upper()
    if action in ["STOP", "TP1", "TP2", "TP3"] or action.startswith("TP"):
        return {"allow_close": True, "reason_code": "HARD_LEVEL_EVENT", "reason": "фактичний стоп/тейк має пріоритет"}

    side = getattr(trade, "side", "")
    break_snapshot = closed_mtf_break_snapshot(side, context, confirmed_ict_reversal)
    snapshot = ((context or {}).get("dual_speed_mtf") or {}).get(side) or dual_speed_mtf_snapshot(context, side)
    reversal_against = snapshot.get("fast_reversal_against") or professional_fast_reversal_bridge(context, opposite(side))
    setup_type = _active_trade_setup_type(trade)
    pullback_family = setup_type in ["PULLBACK_CONTINUATION", "PULLBACK_CONTINUATION_FAST_ENTRY", "TREND_CONTINUATION", "CLOSED_15M_DIRECTION_FLIP"]
    closed_support_alive = bool(
        not break_snapshot.get("closed_15m_break")
        and (snapshot.get("closed_support", 0) >= snapshot.get("closed_against", 0) + 0.6)
    )

    initial_risk_pct = abs(pct(getattr(trade, "stop_initial", trade.entry), trade.entry)) if getattr(trade, "entry", 0) else 0.0
    adverse_r = abs(min(0.0, current_pct)) / initial_risk_pct if initial_risk_pct > 0 else 0.0
    fast_reversal_exit = bool(
        reversal_against.get("confirmed")
        and int(reversal_against.get("score", 0) or 0) >= FAST_REVERSAL_MIN_SCORE
        and (
            current_pct <= -0.20
            or adverse_r >= 0.35
            or reversal_against.get("emergency")
        )
    )
    emergency = bool(snapshot.get("emergency_against") and (current_pct <= -0.25 or adverse_r >= 0.45))
    broad_profit_failure = bool(
        (getattr(trade, "tp1_hit", False) or best_pct >= 0.55 or profit_exit)
        and giveback_ratio >= (0.58 if pullback_family else 0.52)
        and (snapshot.get("fast_against", 0) + snapshot.get("live_against", 0) >= 1.6)
        and not (snapshot.get("fast_support", 0) >= snapshot.get("fast_against", 0) + 0.8)
    )

    if break_snapshot.get("closed_15m_break"):
        return {"allow_close": True, "reason_code": "CLOSED_15M_STRUCTURE_BREAK", "reason": "закрита 15M + ICT/структура підтвердили злам", "break_snapshot": break_snapshot}
    if fast_reversal_exit:
        return {"allow_close": True, "reason_code": "STRICT_FAST_REVERSAL_BRIDGE", "reason": "3M + ICT/структура/live-15M + потік підтвердили реальний ранній розворот", "break_snapshot": break_snapshot, "fast_reversal": reversal_against}
    if emergency:
        return {"allow_close": True, "reason_code": "EMERGENCY_FAST_REVERSAL", "reason": "кілька незалежних швидких шарів підтвердили аварійний розворот", "break_snapshot": break_snapshot}
    if broad_profit_failure:
        return {"allow_close": True, "reason_code": "CONFIRMED_PROFIT_GIVEBACK", "reason": "реальний MFE віддається, швидкі шари узгоджено проти", "break_snapshot": break_snapshot}
    if pullback_family and closed_support_alive:
        return {"allow_close": False, "reason_code": "PULLBACK_THESIS_STILL_ALIVE", "reason": "відкатний/трендовий сценарій ще підтриманий закритими шарами; максимум захист/попередження", "replacement_action": "PROTECT_OR_EXIT", "break_snapshot": break_snapshot}
    if closed_support_alive and not (phase_snapshot or {}).get("exit_permission"):
        return {"allow_close": False, "reason_code": "DECISION_CONFLICT", "reason": "повний вихід суперечить власному аналізу: закрита структура жива, підтвердженого зламу немає", "replacement_action": "EXIT_WARNING", "break_snapshot": break_snapshot}
    return {"allow_close": False, "reason_code": "INSUFFICIENT_EXIT_EVIDENCE", "reason": "недостатньо незалежних підтверджень для повного виходу", "replacement_action": "EXIT_WARNING", "break_snapshot": break_snapshot}


def _post_rejection_recovery_failure_snapshot(trade, context, current_pct, entry_state=None, lifecycle_snapshot=None):
    """Fast validation for a recovery entry after a prior rejection.

    This does not affect ordinary trend entries. A recovery entry must hold the
    fresh reclaim/retest level; losing it while fast layers turn against means
    the *new* setup failed and should not be carried to the distant 15M stop.
    """
    if not bool(getattr(trade, "recovery_after_rejection", False)) or trade.tp1_hit:
        return {"active": False, "close": False, "warning": False}
    try:
        opened = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        age_min = max(0.0, (now_utc() - opened.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        age_min = 999.0
    if age_min > RECOVERY_ENTRY_VALIDATION_MINUTES:
        return {"active": False, "close": False, "warning": False, "age_min": age_min}

    side = trade.side
    trigger = safe_float(getattr(trade, "recovery_trigger_level", 0), 0) or 0
    closed3 = closed_candles(list((context or {}).get("candles_3m") or []), 3, min_required=2)
    last3 = closed3[-1] if closed3 else None
    atr15 = safe_float((context.get("tf15") or {}).get("atr"), abs(trade.entry - trade.stop_initial)) or abs(trade.entry - trade.stop_initial)
    atr3 = safe_float(atr(closed3, 14), atr15 * 0.32) if closed3 else atr15 * 0.32
    trigger_lost = False
    if trigger and last3:
        if side == "LONG":
            trigger_lost = bool(last3.close < trigger - atr3 * 0.10)
        else:
            trigger_lost = bool(last3.close > trigger + atr3 * 0.10)

    tf3 = context.get("tf3") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    liquidity = context.get("liquidity") or {}
    opposing = sum([
        tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 30,
        cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 14,
        flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10,
        liquidity.get("bias") == opposite(side) and abs(int(liquidity.get("score", 0) or 0)) >= 10,
    ])
    support = sum([
        tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= 22,
        cvd.get("bias") == side and abs(int(cvd.get("score", 0) or 0)) >= 12,
        flow.get("bias") == side and abs(int(flow.get("score", 0) or 0)) >= 10,
    ])
    risk_abs = max(abs(float(trade.entry) - float(trade.stop_initial)), 1e-9)
    adverse_abs = max(0.0, float(trade.entry) - safe_float(context.get("price"), trade.entry)) if side == "LONG" else max(0.0, safe_float(context.get("price"), trade.entry) - float(trade.entry))
    risk_fraction = adverse_abs / risk_abs
    broken_state = bool(
        (entry_state or {}).get("state") == "BROKEN"
        or int((lifecycle_snapshot or {}).get("score", 50) or 50) <= 25
        or int(getattr(trade, "lifecycle_failures", 0) or 0) >= 2
    )
    close = bool(
        (trigger_lost and opposing >= 1 and broken_state)
        or (risk_fraction >= RECOVERY_ENTRY_EXIT_RISK_FRACTION and opposing >= 2 and support == 0 and broken_state)
    )
    warning = bool(not close and (
        trigger_lost or risk_fraction >= RECOVERY_ENTRY_WARNING_RISK_FRACTION or broken_state
    ))
    return {
        "active": True,
        "close": close,
        "warning": warning,
        "trigger_lost": trigger_lost,
        "opposing_layers": int(opposing),
        "support_layers": int(support),
        "risk_fraction": round(risk_fraction, 3),
        "age_min": round(age_min, 1),
        "reason": ("новий post-rejection reclaim/retest втрачено і швидкі шари підтвердили провал" if close else
                   "post-rejection recovery ще не підтвердила продовження; потрібне утримання нового тригера"),
    }


def manage_active_trade(trade, context):
    trade.management_checks = int(getattr(trade, "management_checks", 0) or 0) + 1
    price = context["price"]
    side = trade.side
    _sync_two_level_stops(trade, side)
    tf3 = context["tf3"]
    tf15 = context["tf15"]
    structure = context["structure"]
    ict = context.get("ict") or {}
    flow = context["flow"]
    mode_profile = trade_mode_profile(context, side)
    market_regime = mode_profile.get("regime", "NORMAL")

    # Notification support: remember the active stop at the start of this run.
    # If management improves it later, Telegram will show a highlighted
    # "УВАГА: СТОП ЗМІНЕНО" block with old/new stop only.
    previous_stop_at_run_start = round_price(getattr(trade, "stop_current", None))

    high_since_open, low_since_open = _trade_extremes_since_open(trade, context)
    high_since_stop, low_since_stop = _trade_extremes_since_stop_update(trade, context)
    trade.best_price = best_trade_price(side, trade, price, high_since_open, low_since_open)
    current_pct = signed_pct(side, trade.entry, price)
    best_pct = signed_pct(side, trade.entry, trade.best_price)
    giveback = max(0, best_pct - current_pct)
    giveback_ratio = giveback / best_pct if best_pct > 0 else 0

    notes = []
    if market_regime in ["RANGE", "NEWS_IMPULSE", "REVERSAL"]:
        notes.append(f"режим: {regime_label(market_regime)} — прибуток захищати швидше")
    elif market_regime == "TREND":
        notes.append("режим: сильний тренд — можна тримати довше, але MFE не віддавати")
    action = "HOLD"
    title = f"СУПРОВІД {side}"
    recommendation = "утримувати, поки 3m/15m не ламаються"

    # Geometry anomaly guard. A micro-stop + extreme RR is a warning signal,
    # not an automatic close by itself. Full exit requires an actual structural
    # invalidation, strict fast reversal, or the factual stop event handled below.
    atr15_now = safe_float(context.get("atr15"), None)
    if not atr15_now:
        atr15_now = safe_float((context.get("tf15") or {}).get("atr"), price * 0.006) or price * 0.006
    initial_risk_abs = abs(float(trade.entry) - float(trade.stop_initial))
    initial_risk_atr = initial_risk_abs / max(atr15_now, 1e-9)
    initial_rr1 = abs(float(trade.tp1) - float(trade.entry)) / max(initial_risk_abs, 1e-9)
    rejection_guard = post_impulse_rejection_snapshot(side, context)
    try:
        opened_dt = datetime.fromisoformat(str(trade.opened_at).replace("Z", "+00:00"))
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        trade_age_min = max(0.0, (now_utc() - opened_dt.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        trade_age_min = 999.0
    anomalous_fresh_entry = bool(
        not trade.tp1_hit
        and trade_age_min <= 60.0
        and rejection_guard.get("active")
        and initial_risk_atr < MIN_ROBUST_STOP_ATR_15M
        and initial_rr1 >= EXTREME_RR_SANITY_LIMIT
    )
    if anomalous_fresh_entry:
        anomaly_break = closed_mtf_break_snapshot(side, context, False)
        reversal_against = professional_fast_reversal_bridge(context, opposite(side))
        opposing_layers = sum([
            (context.get("tf3") or {}).get("bias") == opposite(side) and abs(int((context.get("tf3") or {}).get("score", 0) or 0)) >= 34,
            (context.get("cvd") or {}).get("bias") == opposite(side) and abs(int((context.get("cvd") or {}).get("score", 0) or 0)) >= 16,
            (context.get("flow") or {}).get("bias") == opposite(side) and abs(int((context.get("flow") or {}).get("score", 0) or 0)) >= 12,
            (context.get("liquidity") or {}).get("bias") == opposite(side) and abs(int((context.get("liquidity") or {}).get("score", 0) or 0)) >= 10,
        ])
        real_invalidation = bool(
            anomaly_break.get("closed_15m_break")
            or reversal_against.get("emergency")
            or (
                reversal_against.get("confirmed")
                and opposing_layers >= 2
                and current_pct < 0
            )
        )
        if real_invalidation:
            trade.status = "CLOSED"
            trade.last_action = "EXIT_ENTRY_POINT_BROKEN"
            return {
                "closed": True,
                "action": "EXIT_ENTRY_POINT_BROKEN",
                "exit_reason_code": "ANOMALOUS_ENTRY_WITH_REAL_INVALIDATION",
                "exit_quality": "STRUCTURAL",
                "title": f"{side} ЗАКРИТИ — ТОЧКА ВХОДУ ВТРАТИЛА АКТУАЛЬНІСТЬ",
                "recommendation": "аномальна геометрія підтверджена реальним структурним зламом/швидким розворотом",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "exit_price": round_price(price),
                "notes": [
                    rejection_guard.get("reason"),
                    f"початковий стоп: {round(initial_risk_atr, 2)} ATR15",
                    f"початковий RR1: {round(initial_rr1, 2)}R",
                    "є незалежне підтвердження інвалідації",
                ],
            }
        warning_key = "ANOMALOUS_GEOMETRY_WARNING"
        if warning_key not in (trade.notes or []):
            trade.notes.append(warning_key)
            action = "EXIT_WARNING"
            title = f"{side} — КРИТИЧНА ПЕРЕВІРКА ТОЧКИ ВХОДУ"
            recommendation = "мікростоп і надмірний RR підозрілі, але без структурного зламу угоду автоматично не закривати"
            notes.extend([
                rejection_guard.get("reason"),
                f"початковий стоп: {round(initial_risk_atr, 2)} ATR15",
                f"початковий RR1: {round(initial_rr1, 2)}R",
                "потрібен ICT/структурний злам або фактичний стоп для повного виходу",
            ])

    if trade_hit_stop_by_extreme(side, price, trade.stop_current, high_since_stop, low_since_stop):
        trade.status = "CLOSED"
        trade.last_action = "STOP"
        stop_exit_pct = signed_pct(side, trade.entry, trade.stop_current)
        return {
            "closed": True,
            "action": "STOP",
            "title": f"УГОДУ {side} ЗАКРИТО — STOP",
            "recommendation": "стоп/зона зламу пробита, сценарій закрито",
            "current_pct": stop_exit_pct,
            "market_price_pct": current_pct,
            "best_pct": best_pct,
            "exit_price": round_price(trade.stop_current),
            "notes": [f"стоп {round_price(trade.stop_current)} зачеплено по high/low 3M після його встановлення; ринкова ціна {round_price(price)}"],
        }

    if trade_hit_level_by_extreme(side, price, trade.tp3, high_since_open, low_since_open):
        trade.tp3_hit = True
        trade.status = "CLOSED"
        trade.last_action = "TP3"
        tp3_pct = signed_pct(side, trade.entry, trade.tp3)
        return {
            "closed": True,
            "action": "TP3",
            "exit_reason_code": "TP3_BY_HIGH_LOW",
            "exit_price": round_price(trade.tp3),
            "title": f"УГОДУ {side} ЗАКРИТО — TP3",
            "recommendation": "основна ціль виконана, сценарій закрито",
            "current_pct": tp3_pct,
            "best_pct": max(best_pct, tp3_pct),
            "notes": [_tp_hit_note(side, "TP3", trade.tp3, high_since_open, low_since_open, price)],
        }

    recommended_stop = None
    recommended_stop_reason = ""

    if trade_hit_level_by_extreme(side, price, trade.tp2, high_since_open, low_since_open):
        trade.tp2_hit = True
        trade.tp1_hit = True
        action = "TP2_PROTECT"
        title = f"{side} — TP2 ВЗЯТО, НОВИЙ СТОП ЗАФІКСОВАНО"
        recommendation = "TP2 взято: позицію не закривати; новий ICT/SMC стоп рахується один раз і далі не рухається до TP3/виходу"
        if not trade.tp2_stop_locked:
            recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP2")
            _apply_more_protective_stop(trade, side, recommended_stop, context=context, stage="TP2_LOCK", force=True)
            trade.tp2_locked_stop = float(trade.stop_current)
            trade.tp2_stop_locked = True
            notes.append(_tp_hit_note(side, "TP2", trade.tp2, high_since_open, low_since_open, price))
            notes.append(f"зафіксувати стоп до TP3: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)
        else:
            recommended_stop = trade.tp2_locked_stop or trade.stop_current
            recommended_stop_reason = "TP2-стоп уже зафіксований; до TP3 не перераховувати на кожній свічці"
            _set_profit_stop_level(trade, side, recommended_stop)
            notes.append(f"стоп вже зафіксовано до TP3: {round_price(trade.stop_current)}")
            notes.append(recommended_stop_reason)
    elif trade_hit_level_by_extreme(side, price, trade.tp1, high_since_open, low_since_open):
        trade.tp1_hit = True
        action = "TP1_PROTECT"
        title = f"{side} — TP1 ВЗЯТО, СТОП ЗАФІКСОВАНО ДО TP2"
        recommendation = "TP1 взято: позицію не закривати; один раз переставити стоп у зону між входом і TP1 з урахуванням ICT/SMC, далі не рухати до TP2"
        if not trade.tp1_stop_locked:
            recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
            _apply_more_protective_stop(trade, side, recommended_stop, context=context, stage="TP1_LOCK", force=True)
            trade.tp1_locked_stop = float(trade.stop_current)
            trade.tp1_stop_locked = True
            notes.append(_tp_hit_note(side, "TP1", trade.tp1, high_since_open, low_since_open, price))
            notes.append(f"зафіксувати стоп до TP2: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)
        else:
            recommended_stop = trade.tp1_locked_stop or trade.stop_current
            recommended_stop_reason = "TP1-стоп уже зафіксований; до TP2 не перераховувати на кожній свічці"
            _set_profit_stop_level(trade, side, recommended_stop)
            notes.append(f"стоп вже зафіксовано до TP2: {round_price(trade.stop_current)}")
            notes.append(recommended_stop_reason)

    # Weighted management pressure for intraday trades.
    # 3m/15m/structure matter most. 4H and clusters are only small background hints.
    opposite_votes = 0.0
    support_votes = 0.0
    weighted_blocks = [
        (tf3, 1.20),
        (tf15, 1.00),
        (structure, 1.00),
        (ict, 0.90),
        (context.get("liquidity") or {}, 0.75),
        (context.get("news") or {}, 0.35),
        (context.get("cvd") or {}, 0.80),
        (context.get("derivatives") or {}, 0.55),
        (flow, 0.35),
        (context.get("clusters") or {}, 0.20),
        (context.get("tf4h") or {}, 0.20),
    ]
    for block, weight in weighted_blocks:
        if block.get("bias") == side:
            support_votes += weight
        elif block.get("bias") == opposite(side):
            opposite_votes += weight

    near_entry = abs(current_pct) <= 0.18
    post_tp1 = bool(trade.tp1_hit)
    profile_be = safe_float(mode_profile.get("be_trigger"), 0.65) or 0.65
    profile_giveback = safe_float(mode_profile.get("giveback"), 0.55) or 0.55
    lost_after_profit = best_pct >= max(0.35, profile_be * 0.75) and giveback_ratio >= profile_giveback and current_pct <= (0.22 if market_regime in ["RANGE", "NEWS_IMPULSE"] else 0.18)
    tp1_giveback_to_entry = post_tp1 and best_pct >= max(0.55, profile_be) and giveback_ratio >= min(0.60, profile_giveback + 0.08) and current_pct <= 0.22

    # Intraday early-exit logic.
    # For trades that should last only a few hours, do not wait for the formal stop
    # if 3m has already broken against the position and order-flow/CVD confirms it.
    tf3_against = tf3.get("bias") == opposite(side) and abs(int(tf3.get("score", 0) or 0)) >= 42
    flow_against = flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 10
    cvd_block = context.get("cvd") or {}
    cvd_against = cvd_block.get("bias") == opposite(side) and abs(int(cvd_block.get("score", 0) or 0)) >= 10
    structure_against = structure.get("bias") == opposite(side)
    ict_against = ict.get("bias") == opposite(side) and abs(int(ict.get("score", 0) or 0)) >= 16
    liquidity_block = context.get("liquidity") or {}
    liquidity_against = liquidity_block.get("bias") == opposite(side) and abs(int(liquidity_block.get("score", 0) or 0)) >= 12
    news_block = context.get("news") or {}
    news_against = news_block.get("bias") == opposite(side) and abs(int(news_block.get("score", 0) or 0)) >= 18
    confirmed_ict_reversal, ict_reversal_components = _has_confirmed_ict_reversal(side, context)
    real_structure_break = bool(structure_against or ict_against or confirmed_ict_reversal)
    soft_warning_only = bool((cvd_against or flow_against or news_against or liquidity_against) and not real_structure_break)
    countertrend_validation = countertrend_entry_validation(
        trade, context, current_pct, best_pct, giveback,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    trade_validation = trade_entry_validation_snapshot(
        trade, context, current_pct, best_pct, giveback,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    entry_state = entry_point_state_snapshot(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        trade_validation=trade_validation,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
    )
    closed_break = closed_mtf_break_snapshot(side, context, confirmed_ict_reversal)
    setup_recovery = _setup_recovery_snapshot(trade, context, closed_break)
    if entry_state:
        previous_streak = int(getattr(trade, "entry_fail_streak", 0) or 0)
        if entry_state.get("fail_now") and not trade.tp1_hit:
            if setup_recovery.get("strong"):
                trade.entry_fail_streak = 0
            elif setup_recovery.get("soft"):
                trade.entry_fail_streak = max(0, previous_streak - 1)
            else:
                trade.entry_fail_streak = previous_streak + 1
        elif entry_state.get("state") in ["STRONG", "WORKING"] or setup_recovery.get("strong"):
            trade.entry_fail_streak = 0
        elif setup_recovery.get("soft"):
            trade.entry_fail_streak = max(0, previous_streak - 1)
        if entry_state.get("recovery_mode") == "RECHECK_AT_ENTRY":
            trade.entry_recovery_checks = int(getattr(trade, "entry_recovery_checks", 0) or 0) + 1
        trade.entry_integrity_score = int(entry_state.get("score") or 50)
        trade.entry_last_state = str(entry_state.get("label") or "")
    exhausted_trade, exhausted_trade_reason = detect_exhausted_move(side, context)

    phase_snapshot = analyze_trade_phase(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        entry_state=entry_state,
        trade_validation=trade_validation,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
        action=action,
        market_regime=market_regime,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
    )
    lifecycle_snapshot = update_trade_lifecycle(
        trade, context, current_pct, best_pct, phase_snapshot, closed_break,
        support_votes=support_votes, opposite_votes=opposite_votes,
    )

    recovery_validation = _post_rejection_recovery_failure_snapshot(
        trade, context, current_pct, entry_state=entry_state, lifecycle_snapshot=lifecycle_snapshot,
    )
    if recovery_validation.get("active"):
        trade.recovery_entry_checks = int(getattr(trade, "recovery_entry_checks", 0) or 0) + 1
        if recovery_validation.get("close"):
            trade.status = "CLOSED"
            trade.last_action = "EXIT_ENTRY_POINT_BROKEN"
            return {
                "closed": True,
                "action": "EXIT_ENTRY_POINT_BROKEN",
                "exit_reason_code": "POST_REJECTION_RECOVERY_FAILED",
                "exit_quality": "FRESH_TRIGGER_INVALIDATION",
                "title": f"{side} ЗАКРИТИ — НОВИЙ RECOVERY-СЕТАП НЕ ВТРИМАВСЯ",
                "recommendation": recovery_validation.get("reason"),
                "current_pct": current_pct,
                "best_pct": best_pct,
                "exit_price": round_price(price),
                "notes": [
                    f"втрачено trigger-рівень: {recovery_validation.get('trigger_lost')}",
                    f"використано ризику: {round(recovery_validation.get('risk_fraction', 0) * 100, 1)}%",
                    f"швидких шарів проти: {recovery_validation.get('opposing_layers')}",
                    "це окремий post-rejection сетап; далекий структурний стоп не виправдовує провал нового тригера",
                ],
                "entry_state": entry_state,
                "trade_phase": phase_snapshot,
                "recovery_validation": recovery_validation,
            }
        elif recovery_validation.get("warning") and action == "HOLD":
            action = "EXIT_WARNING"
            title = f"{side} — RECOVERY-СЕТАП ПОТРЕБУЄ НЕГАЙНОГО ПІДТВЕРДЖЕННЯ"
            recommendation = recovery_validation.get("reason")
            notes.append(f"використано ризику: {round(recovery_validation.get('risk_fraction', 0) * 100, 1)}%")

    # Mandatory MFE Profit Lock for risky/reversal/recovery trades.
    opposing_layers_for_lock = sum([tf3_against, flow_against, cvd_against, liquidity_against, structure_against, ict_against])
    mandatory_lock = mandatory_mfe_profit_lock_snapshot(
        trade, context, current_pct, best_pct, giveback_ratio, opposing_layers_for_lock,
    )
    if mandatory_lock.get("active"):
        if mandatory_lock.get("close"):
            trade.status = "CLOSED"
            trade.last_action = "EXIT_MANDATORY_MFE_LOCK"
            return {
                "closed": True, "action": trade.last_action,
                "exit_reason_code": "MANDATORY_MFE_PROFIT_LOCK",
                "exit_quality": "PROFIT_PROTECTION",
                "title": f"{side} ЗАКРИТИ — MFE ПРИБУТОК ЗАХИЩЕНО",
                "recommendation": mandatory_lock.get("reason"),
                "current_pct": current_pct, "best_pct": best_pct,
                "exit_price": round_price(price),
                "notes": [f"макс. прибуток: {round(best_pct,3)}%", f"віддано: {round(giveback_ratio*100,1)}% MFE", "два підтвердження слабкості"],
                "entry_state": entry_state, "trade_phase": phase_snapshot,
            }
        if _apply_more_protective_stop(trade, side, mandatory_lock.get("stop"), context=context, stage="PRE_TP1", force=True):
            action = "PROTECT"
            title = f"{side} — MFE ПРИБУТОК ОБОВʼЯЗКОВО ЗАХИЩЕНО"
            recommendation = mandatory_lock.get("reason")
            recommended_stop = trade.stop_current
            recommended_stop_reason = "Mandatory MFE Profit Lock"
            notes.append(f"зафіксовано приблизно {round(mandatory_lock.get('lock_pct',0),2)}% від входу")

    # Professional RISKY/NEWS pre-TP1 profit guard.
    # If an early/risky/news entry already gave meaningful MFE but TP1 is still
    # untouched, the bot must not wait until the full stop. It either moves stop
    # near BE+, or exits near entry when the edge is fading.
    risky_guard = risky_pre_tp1_profit_guard_snapshot(
        trade, context, current_pct, best_pct, giveback, giveback_ratio,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
        entry_state=entry_state,
    )
    if risky_guard.get("active"):
        if risky_guard.get("close"):
            coherence = decision_coherence_guard(
                trade, context, risky_guard.get("action") or "EXIT_RISKY_PRE_TP1_BE_GUARD",
                current_pct, best_pct, giveback_ratio, phase_snapshot,
                confirmed_ict_reversal=confirmed_ict_reversal, profit_exit=best_pct >= 0.45,
            )
            if not coherence.get("allow_close"):
                risky_guard["close"] = False
                risky_guard["protect"] = True
                notes.append("Coherence Guard: " + str(coherence.get("reason")))
            else:
                trade.status = "CLOSED"
                trade.last_action = risky_guard.get("action") or "EXIT_RISKY_PRE_TP1_BE_GUARD"
                return {
                "closed": True,
                "action": trade.last_action,
                "exit_reason_code": "RISKY_PRE_TP1_RISK_FLOOR" if trade.last_action == "EXIT_RISKY_PRE_TP1_RISK_FLOOR" else "RISKY_PRE_TP1_BE_GUARD",
                "exit_quality": "PROTECTIVE_BE_EXIT",
                "title": risky_guard.get("title") or f"{side} ЗАКРИТИ — РИЗИКОВИЙ ВХІД ВІДДАВ MFE",
                "recommendation": risky_guard.get("recommendation") or "закрити біля входу, не чекати дальній стоп",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "notes": list(risky_guard.get("notes") or [])[:5],
                "entry_state": entry_state,
                "trade_phase": analyze_trade_phase(
                    trade, context, current_pct, best_pct, giveback,
                    high_since_open=high_since_open, low_since_open=low_since_open,
                    entry_state=entry_state, trade_validation=trade_validation,
                    support_votes=support_votes, opposite_votes=opposite_votes,
                    action=trade.last_action, market_regime=market_regime,
                    tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
                    flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
                    news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
                    closed=True, exit_reason_code="RISKY_PRE_TP1_BE_GUARD",
                ),
            }
        guard_stop = risky_guard.get("stop")
        if risky_guard.get("protect") and guard_stop is not None and _apply_more_protective_stop(trade, side, guard_stop, context=context, stage="PRE_TP1"):
            action = "PROTECT"
            title = f"{side} — РИЗИКОВИЙ ВХІД: MFE ЗАХИЩЕНО"
            recommendation = "ранній/новинний вхід уже дав хороший рух до TP1: стоп підтягнуто біля входу, щоб не перетворити плюс у мінус"
            recommended_stop = trade.stop_current
            recommended_stop_reason = risky_guard.get("reason") or "RISKY/NEWS pre-TP1 profit guard"
            notes.extend(list(risky_guard.get("notes") or [])[:4])

    # Universal validation of the opened idea. This does not delay entries.
    # It changes the supervision wording through "Стан точки входу", without
    # adding a duplicated generic trade-risk block to Telegram.
    if entry_state and entry_state.get("state") in ["BROKEN", "WEAK"] and action == "HOLD" and not trade.tp1_hit:
        if closed_break.get("closed_15m_break"):
            action = "EXIT_WARNING"
            title = f"{side} — ЗАКРИТА 15M/ICT ПІДТВЕРДЖУЄ ЗЛАМ"
            recommendation = entry_state.get("advice") or "структурний злам підтверджено; готувати ранній вихід"
        else:
            action = "PROTECT_OR_EXIT"
            title = f"{side} — ТОЧКА ВХОДУ ПІД ТИСКОМ, АЛЕ ЗЛАМ НЕ ПІДТВЕРДЖЕНО"
            recommendation = "є локальна слабкість, але закрита 15M/ICT/структура ще не зламали сетап; не закривати лише через нову незавершену 1H/4H свічку"
        notes.append("стан точки входу: " + str(entry_state.get("label")))

    # Setup-aware entry integrity exit. A pullback/continuation trade is not
    # closed merely because three noisy checks accumulated; the opening setup
    # defines how much confirmation an early exit requires.
    if entry_state and not trade.tp1_hit:
        setup_exit = setup_aware_exit_decision(
            trade, context, entry_state, phase_snapshot, current_pct,
            tf3_against=tf3_against, flow_against=flow_against,
            cvd_against=cvd_against, confirmed_ict_reversal=confirmed_ict_reversal,
        )
        if setup_exit.get("close"):
            continuation_hyst = continuation_exit_hysteresis_snapshot(
                trade, context, current_pct=current_pct, confirmed_ict_reversal=confirmed_ict_reversal
            )
            if continuation_hyst.get("active") and not continuation_hyst.get("allow_close"):
                setup_exit["close"] = False
                setup_exit["warning_only"] = True
                action = "UNDER_PRESSURE"
                title = f"{side} — CONTINUATION ПІД ТИСКОМ, АЛЕ 15M СЕТАП ЩЕ ЖИВИЙ"
                recommendation = continuation_hyst.get("reason")
                notes.append("Continuation Exit Hysteresis: " + str(continuation_hyst.get("reason")))
            else:
                trade.status = "CLOSED"
                trade.last_action = "EXIT_ENTRY_POINT_BROKEN"
                break_info = setup_exit.get("break_snapshot") or {}
                return {
                    "closed": True,
                    "action": "EXIT_ENTRY_POINT_BROKEN",
                    "exit_reason_code": "SETUP_AWARE_CLOSED_MTF_BREAK",
                    "exit_quality": "SETUP_AWARE_ENTRY_POINT",
                    "title": f"{side} ЗАКРИТИ — СЕТАП ЗЛАМАНО ЗАКРИТОЮ 15M/ICT",
                    "recommendation": setup_exit.get("reason") or "структурний злам підтверджено; вийти раніше повного стопа",
                    "current_pct": current_pct,
                    "best_pct": best_pct,
                    "notes": [
                        entry_state.get("reason", "точка входу втратила актуальність"),
                        f"сетап: {setup_exit.get('setup_type')}",
                        f"слабких перевірок підряд: {getattr(trade, 'entry_fail_streak', 0)}",
                        f"закрита 15M/ICT структура: {'зламана' if break_info.get('closed_15m_break') else 'не зламана'}",
                    ],
                    "entry_state": entry_state,
                    "setup_aware_exit": setup_exit,
                    "trade_phase": analyze_trade_phase(
                        trade, context, current_pct, best_pct, giveback,
                        high_since_open=high_since_open, low_since_open=low_since_open,
                        entry_state=entry_state, trade_validation=trade_validation,
                        support_votes=support_votes, opposite_votes=opposite_votes,
                        action="EXIT_ENTRY_POINT_BROKEN", market_regime=market_regime,
                        tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
                        flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
                        news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
                        closed=True, exit_reason_code="SETUP_AWARE_CLOSED_MTF_BREAK",
                    ),
                }
        elif setup_exit.get("warning_only"):
            notes.append("setup-aware: ранній вихід не підтверджено закритою 15M/ICT; позицію не закривати лише за серією слабких перевірок")

    # Smart post-TP1 supervision. This does not close by itself; it labels the
    # trade correctly and prevents panic exits when TP1 is done but ICT/15M is
    # still valid. Closing after TP1 still requires ICT/SMC reversal, deep MFE
    # giveback, stop, or a broad confirmed failure.
    post_tp1_trend_valid = bool(
        post_tp1
        and (tf15.get("bias") == side or context.get("bias") == side or support_votes >= 2.6)
        and not (structure_against or ict_against)
        and not confirmed_ict_reversal
    )
    post_tp1_warning = bool(
        post_tp1
        and (tf3_against or flow_against or cvd_against or news_against)
        and not (structure_against or ict_against or confirmed_ict_reversal)
    )

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - trade.stop_current) / trade.entry * 100) if trade.entry else 99.0
    else:
        stop_distance_pct = max(0.0, (trade.stop_current - price) / trade.entry * 100) if trade.entry else 99.0

    # MFE protection before formal TP1: do not allow a good intraday move
    # to come back to zero just because TP1 is still far away.
    # Use confirmed/close MFE for pre-TP1 stop tightening so one wick does not
    # over-tighten the trade. Big wick MFE still matters, but only after the
    # candle closes/follow-through validate it.
    effective_mfe_for_lock = best_pct
    if not trade.tp1_hit:
        close_mfe_for_lock = safe_float(phase_snapshot.get("close_mfe_pct"), 0.0) or 0.0
        confirmed_mfe_for_lock = safe_float(phase_snapshot.get("confirmed_mfe_pct"), 0.0) or 0.0
        if best_pct >= 0.45 and confirmed_mfe_for_lock < best_pct * 0.45:
            effective_mfe_for_lock = max(confirmed_mfe_for_lock, close_mfe_for_lock, min(best_pct, 0.65))
    mfe_stop, mfe_stop_reason = _profit_lock_stop_level(side, trade.entry, price, effective_mfe_for_lock, current_pct, mode_profile)
    if action == "HOLD" and mfe_stop is not None:
        if _apply_more_protective_stop(trade, side, mfe_stop, context=context, stage="PRE_TP1" if not trade.tp1_hit else ("POST_TP2" if trade.tp2_hit else "POST_TP1")):
            action = "PROTECT"
            title = f"{side} — ПРИБУТОК ЗАХИЩЕНО"
            recommendation = "ціна вже давала хороший плюс: стоп підтягнуто, щоб не віддати угоду назад"
            recommended_stop = trade.stop_current
            recommended_stop_reason = mfe_stop_reason
            notes.append(f"MFE-захист: {mfe_stop_reason}")
            notes.append(f"новий стоп: {round_price(trade.stop_current)}")

    if side == "LONG":
        stop_distance_pct = max(0.0, (price - trade.stop_current) / trade.entry * 100) if trade.entry else 99.0
    else:
        stop_distance_pct = max(0.0, (trade.stop_current - price) / trade.entry * 100) if trade.entry else 99.0

    near_stop = stop_distance_pct <= 0.30
    clearly_losing = current_pct <= -0.30

    if action == "HOLD" and post_tp1_trend_valid:
        action = "HOLD_TO_TP2"
        title = f"{side} — TP1 ВЗЯТО, ТРЕНД ЩЕ ЖИВИЙ"
        recommendation = "TP1 виконано: якщо ICT/15M структура не зламана, не закривати позицію через шум; тримати до TP2 зі зафіксованим стопом"
        notes.append("після TP1 структура/15M ще підтримують угоду")
    elif action == "HOLD" and post_tp1_warning:
        action = "PROTECT"
        title = f"{side} — TP1 ВЗЯТО, Є ЛОКАЛЬНИЙ ВІДКАТ"
        recommendation = "TP1 виконано: є локальні попередження, але без ICT/SMC зламу це не команда закривати; стоп має бути зафіксований"
        notes.append("після TP1 є CVD/flow/3M шум, але ICT-розворот не підтверджений")

    if tf3_against and (flow_against or cvd_against) and (near_stop or clearly_losing):
        min_capture_pct = _mfe_exit_floor(best_pct, market_regime)
        trend_soft_break = (
            market_regime == "TREND"
            and current_pct > 0
            and best_pct >= profile_be
            and current_pct >= min_capture_pct
            and not (structure_against or ict_against)
        )
        if trend_soft_break:
            action = "PROTECT"
            title = f"{side} — ЛОКАЛЬНИЙ ЗЛАМ, АЛЕ ТРЕНД ЩЕ ЖИВИЙ"
            recommendation = "3M/потік дають відкат проти, але 15M/ICT структура ще не зламана: стоп у прибуток і дати тренду шанс"
            protect_stop, protect_reason = _profit_lock_stop_level(side, trade.entry, price, best_pct, current_pct, mode_profile)
            if protect_stop is not None and _apply_more_protective_stop(trade, side, protect_stop, context=context, stage="PRE_TP1" if not trade.tp1_hit else ("POST_TP2" if trade.tp2_hit else "POST_TP1")):
                recommended_stop = trade.stop_current
                recommended_stop_reason = protect_reason
                notes.append(f"новий захисний стоп: {round_price(trade.stop_current)}")
            notes.append(f"MFE було +{round(best_pct, 3)}%, захоплено {round((current_pct / best_pct * 100), 1) if best_pct > 0 else 0}%")
        else:
            # Local 3M + flow/CVD noise must not close a trade by itself.
            # Hard exit requires ICT/structure break, deep loss, or stop danger
            # after the trade failed to develop. Otherwise protect/hold.
            hard_local_exit = bool(
                real_structure_break
                or (clearly_losing and near_stop)
                or (near_stop and best_pct < 0.18 and (tf3_against or cvd_against or flow_against))
            )
            coherence = decision_coherence_guard(
                trade, context, "EXIT_LOCAL_BREAK", current_pct, best_pct, giveback_ratio, phase_snapshot,
                confirmed_ict_reversal=confirmed_ict_reversal, profit_exit=best_pct >= 0.55,
            ) if hard_local_exit else {"allow_close": False}
            if hard_local_exit and coherence.get("allow_close"):
                trade.status = "CLOSED"
                trade.last_action = "EXIT_LOCAL_BREAK"
                reasons = ["3M зламався проти позиції"]
                if real_structure_break:
                    reasons.append("ICT/SMC структура підтвердила злам")
                if flow_against:
                    reasons.append("потік проти")
                if cvd_against:
                    reasons.append("CVD проти")
                if near_stop:
                    reasons.append("ціна близько до стопу")
                if best_pct > 0.1:
                    reasons.append(f"MFE було +{round(best_pct, 3)}%, захоплено мало")
                risk_snapshot = active_trade_risk_snapshot(
                    trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, "EXIT_WARNING"
                )
                return {
                    "closed": True,
                    "action": "EXIT_LOCAL_BREAK",
                    "exit_reason_code": "STRUCTURE_OR_STOP_LOCAL_BREAK",
                    "exit_quality": "HARD",
                    "title": f"{side} ЗАКРИТИ — ЛОКАЛЬНИЙ ЗЛАМ",
                    "recommendation": "закрити біля поточної / не чекати дальній стоп",
                    "current_pct": current_pct,
                    "best_pct": best_pct,
                    "notes": reasons[:4],
                    "trade_risk": risk_snapshot.get("trade_risk"),
                    "trade_risk_reasons": risk_snapshot.get("trade_risk_reasons"),
                    "reversal_side": risk_snapshot.get("reversal_side"),
                    "reversal_score": risk_snapshot.get("reversal_score"),
                    "reversal_label": risk_snapshot.get("reversal_label"),
                    "reversal_reasons": risk_snapshot.get("reversal_reasons"),
                    "exit_score": risk_snapshot.get("exit_score"),
                    "exit_signal": risk_snapshot.get("exit_signal"),
                }
            else:
                action = "PROTECT" if current_pct > 0 else "EXIT_WARNING"
                title = f"{side} — ЛОКАЛЬНИЙ ШУМ ПРОТИ, ICT ЩЕ НЕ ЗЛАМАНИЙ"
                recommendation = "не закривати лише через CVD/потік; чекати ICT/SMC злам або спрацювання стопу"
                notes.append("3M/потік проти, але ICT/структура ще не дали повний розворот")
                if hard_local_exit and coherence.get("reason"):
                    notes.append("Coherence Guard: " + str(coherence.get("reason")))

    if tf3_against and (near_stop or clearly_losing):
        if real_structure_break or clearly_losing:
            action = "EXIT_WARNING"
            title = f"{side} ПІД ЗАГРОЗОЮ"
            recommendation = "3M вже проти позиції: захистити або закрити, не усереднювати"
            notes.append("3M зламався проти позиції")
            if near_stop:
                notes.append("ціна близько до стопу")
        else:
            action = "PROTECT"
            title = f"{side} — 3M ВІДКАТ, ICT ЩЕ НЕ ЗЛАМАНИЙ"
            recommendation = "3M шумить проти позиції, але ICT/15M структура ще не дали підтверджений розворот"
            notes.append("3M проти, але без ICT-зламу")

    if exhausted_trade and action == "HOLD" and (current_pct <= 0.15 or tf3_against or cvd_against or flow_against):
        action = "EXIT_WARNING"
        title = f"{side} ПІД ЗАГРОЗОЮ — РУХ ВІДІГРАНИЙ"
        recommendation = "рух уже відпрацював імпульс: захистити або закрити, не чекати дальній стоп без нового 3M/15M підтвердження"
        notes.append(exhausted_trade_reason)
        if tf3_against:
            notes.append("3M вже проти позиції")
        if cvd_against or flow_against:
            notes.append("CVD/потік проти супроводу")

    if tp1_giveback_to_entry:
        # After TP1, do not close just because soft layers disagree.
        # Close requires ICT/structure reversal or a broad confirmed failure.
        hard_reversal_votes = sum([bool(structure_against), bool(ict_against), bool(liquidity_against), bool(confirmed_ict_reversal)])
        soft_warning_votes = sum([bool(tf3_against), bool(flow_against), bool(cvd_against), bool(news_against)])
        warning_votes = hard_reversal_votes * 2 + soft_warning_votes
        if confirmed_ict_reversal or (hard_reversal_votes >= 1 and warning_votes >= 3) or opposite_votes >= 2.6:
            trade.status = "CLOSED"
            trade.last_action = "EXIT_AFTER_TP1_GIVEBACK"
            reasons = ["після TP1 ціна повертається майже до входу"]
            if tf3_against:
                reasons.append("3M вже проти")
            if structure_against or ict_against:
                reasons.append("ICT/SMC структура слабшає")
            if flow_against or cvd_against:
                reasons.append("потік/CVD проти")
            if liquidity_against or news_against:
                reasons.append("ліквідність/новини проти")
            return {
                "closed": True,
                "action": "EXIT_AFTER_TP1_GIVEBACK",
                "title": f"{side} ЗАКРИТИ — ПРИБУТОК НЕ ВІДДАВАТИ",
                "recommendation": "TP1 вже був, ціна майже повернулась до входу: краще закрити позицію або мінімум не тримати без стопу в плюсі",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": trade.stop_current,
                "recommended_stop_reason": "після TP1 не віддавати угоду назад до входу",
                "notes": reasons[:4],
            }
        elif action == "HOLD":
            action = "PROTECT"
            title = f"{side} — ПІСЛЯ TP1 ВІДКАТ, ICT НЕ ЗЛАМАНИЙ"
            recommendation = "після TP1 ціна відкотилася, але без ICT-розвороту це не команда закривати; тримати тільки зі зафіксованим стопом"
            if trade.tp1_stop_locked:
                recommended_stop = trade.tp1_locked_stop or trade.stop_current
                recommended_stop_reason = "TP1-стоп уже зафіксований; якщо ціна повертається до входу — аналізуємо закриття, а не підтягування стопу"
                _set_profit_stop_level(trade, side, recommended_stop)
            else:
                recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
                _apply_more_protective_stop(trade, side, recommended_stop, context=context, stage="TP1_LOCK", force=True)
                trade.tp1_locked_stop = float(trade.stop_current)
                trade.tp1_stop_locked = True
            notes.append(f"стоп для захисту: {round_price(trade.stop_current)}")
            if recommended_stop_reason:
                notes.append(recommended_stop_reason)

    # Adaptive MFE Giveback Guard 2.0.
    # Protects real profit without killing normal trend retests:
    # - before TP1: mostly protect with stop, no panic close;
    # - after TP1 / after big MFE: close only after 2 confirmed warnings;
    # - hard ICT/SMC reversal can close immediately.
    effective_giveback = max(0.0, effective_mfe_for_lock - current_pct)
    effective_giveback_ratio = effective_giveback / effective_mfe_for_lock if effective_mfe_for_lock > 0 else 0.0
    mfe_guard = _adaptive_mfe_guard_snapshot(
        trade, context, market_regime, current_pct, effective_mfe_for_lock, effective_giveback, effective_giveback_ratio,
        post_tp1=post_tp1,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
    )
    if mfe_guard.get("active") and action in ["HOLD", "PROTECT", "HOLD_TO_TP2", "TP1_PROTECT"]:
        close_allowed = False
        coherence = None
        if mfe_guard.get("close"):
            coherence = decision_coherence_guard(
                trade, context, "EXIT_MFE_GIVEBACK", current_pct, best_pct, effective_giveback_ratio, phase_snapshot,
                confirmed_ict_reversal=confirmed_ict_reversal, profit_exit=True,
            )
            close_allowed = bool(coherence.get("allow_close"))
            if not close_allowed:
                mfe_guard["close"] = False
                mfe_guard["protect"] = True
                notes.append("Coherence Guard: " + str(coherence.get("reason")))

        if close_allowed:
            trade.status = "CLOSED"
            trade.last_action = "EXIT_MFE_GIVEBACK"
            reasons = list(mfe_guard.get("reasons") or [])
            if tf3_against:
                reasons.append("3M вже проти")
            if flow_against or cvd_against:
                reasons.append("потік/CVD проти")
            return {
                "closed": True,
                "action": "EXIT_MFE_GIVEBACK",
                "exit_reason_code": (coherence or {}).get("reason_code") or "ADAPTIVE_MFE_GIVEBACK_2_CONFIRMED",
                "exit_quality": "CONFIRMED" if mfe_guard.get("streak", 0) >= 2 else "HARD_ICT_SMC",
                "title": f"{side} ЗАКРИТИ — ПРИБУТОК ВІДДАЄТЬСЯ",
                "recommendation": "адаптивний MFE Guard підтвердив, що рух у плюс віддається і є слабкість: краще закрити/зафіксувати, ніж чекати дальній TP або повний стоп",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": trade.stop_current,
                "recommended_stop_reason": "Adaptive MFE Giveback Guard 2.0",
                "notes": reasons[:5],
            }

        if not mfe_guard.get("close"):
            action = "PROTECT"
            title = f"{side} — MFE ВІДДАЄТЬСЯ, ПОТРІБЕН ЗАХИСТ"
            recommendation = "угода вже давала хороший плюс і частину руху віддала; це ще не обовʼязковий вихід, але стоп треба захистити"
            protect_stop, protect_reason = _profit_lock_stop_level(side, trade.entry, price, best_pct, max(current_pct, 0.20), mode_profile)
            if protect_stop is not None and _apply_more_protective_stop(trade, side, protect_stop, context=context, stage="PRE_TP1" if not trade.tp1_hit else ("POST_TP2" if trade.tp2_hit else "POST_TP1")):
                recommended_stop = trade.stop_current
                recommended_stop_reason = protect_reason or "Adaptive MFE Giveback Guard 2.0"
                notes.append(f"новий захисний стоп: {round_price(trade.stop_current)}")
            notes.extend(list(mfe_guard.get("reasons") or [])[:3])

    if lost_after_profit and opposite_votes >= 2.2:
        coherence = decision_coherence_guard(
            trade, context, "EXIT_GIVEBACK", current_pct, best_pct, giveback_ratio, phase_snapshot,
            confirmed_ict_reversal=confirmed_ict_reversal, profit_exit=True,
        )
        if coherence.get("allow_close"):
            trade.status = "CLOSED"
            trade.last_action = "EXIT_GIVEBACK"
            return {
                "closed": True,
                "action": "EXIT",
                "exit_reason_code": coherence.get("reason_code"),
                "title": f"УГОДУ {side} ЗАКРИТО — АКТУАЛЬНІСТЬ ВТРАЧЕНА",
                "recommendation": "рух у плюс майже віддали назад, підтвердження зникло",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "notes": [coherence.get("reason") or "краще закрити біля входу / не чекати дальній стоп"],
            }
        action = "PROTECT"
        title = f"{side} — ПРИБУТОК СЛАБШАЄ, АЛЕ ПОВНИЙ ЗЛАМ НЕ ПІДТВЕРДЖЕНО"
        recommendation = "захистити угоду стопом; повний вихід поки суперечить закритій структурі"
        notes.append("Coherence Guard: " + str(coherence.get("reason")))

    if post_tp1 and action in ["HOLD", "HOLD_TO_TP2"]:
        # Do NOT trail every 15 minutes after TP1. Keep the first TP1 stop
        # locked until TP2. This avoids getting stopped by normal BZU noise
        # right after the first take-profit.
        if trade.tp2_hit:
            if trade.tp2_stop_locked:
                recommended_stop = trade.tp2_locked_stop or trade.stop_current
                recommended_stop_reason = "TP2-стоп зафіксований; до TP3 не рухати без окремого exit-сигналу"
                _set_profit_stop_level(trade, side, recommended_stop)
        else:
            if trade.tp1_stop_locked:
                recommended_stop = trade.tp1_locked_stop or trade.stop_current
                recommended_stop_reason = "TP1-стоп зафіксований; до TP2 не рухати, супровід оцінює тільки утримувати чи закривати"
                _set_profit_stop_level(trade, side, recommended_stop)
            else:
                recommended_stop, recommended_stop_reason = protective_stop_ict_smc(trade, context, after_tp="TP1")
                _apply_more_protective_stop(trade, side, recommended_stop, context=context, stage="TP1_LOCK", force=True)
                trade.tp1_locked_stop = float(trade.stop_current)
                trade.tp1_stop_locked = True
                action = "TP1_PROTECT"
                title = f"{side} — TP1 ВЗЯТО, СТОП ЗАФІКСОВАНО ДО TP2"
                recommendation = "TP1 взято: стоп зафіксовано один раз до TP2"
                notes.append(_tp_hit_note(side, "TP1", trade.tp1, high_since_open, low_since_open, price))
                notes.append(f"зафіксувати стоп до TP2: {round_price(trade.stop_current)}")
                if recommended_stop_reason:
                    notes.append(recommended_stop_reason)


    # Dynamic post-TP1 supervision. After TP1 the initial stop remains locked by
    # default, but if the trade makes real progress toward TP2 or starts giving
    # back a confirmed MFE, the stop is allowed to improve. This never loosens
    # the TP1 stop and stops working immediately after TP2.
    post_tp1_manager = _post_tp1_dynamic_profit_manager_snapshot(
        trade, context, current_pct, best_pct, giveback, giveback_ratio,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
        entry_state=entry_state,
        action=action,
    )
    if post_tp1_manager.get("active"):
        if post_tp1_manager.get("close"):
            trade.status = "CLOSED"
            trade.last_action = post_tp1_manager.get("action") or "EXIT_AFTER_TP1_PROFIT_PROTECT"
            risk_snapshot = active_trade_risk_snapshot(
                trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, trade.last_action
            )
            return {
                "closed": True,
                "action": trade.last_action,
                "exit_reason_code": "POST_TP1_DYNAMIC_PROFIT_PROTECT",
                "exit_quality": "PROFIT_PROTECT_AFTER_TP1",
                "title": post_tp1_manager.get("title") or f"{side} ЗАКРИТИ — TP1 PROFIT PROTECT",
                "recommendation": post_tp1_manager.get("recommendation") or "TP1 взято: прибуток захистити, не чекати повний відкат до стопу",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": round_price(trade.stop_current),
                "recommended_stop_reason": post_tp1_manager.get("reason"),
                "notes": list(post_tp1_manager.get("notes") or [])[:5],
                "trade_risk": risk_snapshot.get("trade_risk"),
                "trade_risk_score": risk_snapshot.get("trade_risk_score"),
                "reversal_side": risk_snapshot.get("reversal_side"),
                "reversal_score": risk_snapshot.get("reversal_score"),
                "reversal_label": risk_snapshot.get("reversal_label"),
                "entry_state": entry_state,
                "trade_phase": analyze_trade_phase(
                    trade, context, current_pct, best_pct, giveback,
                    high_since_open=high_since_open, low_since_open=low_since_open,
                    entry_state=entry_state, trade_validation=trade_validation,
                    support_votes=support_votes, opposite_votes=opposite_votes,
                    action=trade.last_action, market_regime=market_regime,
                    tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
                    flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
                    news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
                    closed=True, exit_reason_code="POST_TP1_DYNAMIC_PROFIT_PROTECT",
                ),
            }
        new_tp1_stop = post_tp1_manager.get("stop")
        if post_tp1_manager.get("protect") and new_tp1_stop is not None and _apply_more_protective_stop(trade, side, new_tp1_stop, context=context, stage="POST_TP1"):
            # Keep the TP1 lock in sync with the improved protection so the
            # later locked-stop block does not restore the older, looser level.
            trade.tp1_locked_stop = float(trade.stop_current)
            trade.tp1_stop_locked = True
            action = post_tp1_manager.get("action") or "TP1_DYNAMIC_PROTECT"
            title = post_tp1_manager.get("title") or f"{side} — TP1 ДИНАМІЧНИЙ ЗАХИСТ"
            recommendation = post_tp1_manager.get("recommendation") or "TP1 взято: стоп підтягнуто за прогресом до TP2 і умовами ринку"
            recommended_stop = trade.stop_current
            recommended_stop_reason = post_tp1_manager.get("reason") or "Post-TP1 Dynamic Profit Protect"
            notes.extend(list(post_tp1_manager.get("notes") or [])[:4])



    # Dynamic post-TP2 supervision. After TP2 the stop is allowed to react again
    # to MFE giveback / reversal warnings. It never loosens the stop and it does
    # not touch TP1 logic. If price is already close to the protective stop and
    # the market is reversing, it can close the remaining position in profit.
    post_tp2_manager = _post_tp2_dynamic_profit_manager_snapshot(
        trade, context, current_pct, best_pct, giveback, giveback_ratio,
        support_votes=support_votes,
        opposite_votes=opposite_votes,
        tf3_against=tf3_against,
        structure_against=structure_against,
        ict_against=ict_against,
        flow_against=flow_against,
        cvd_against=cvd_against,
        liquidity_against=liquidity_against,
        news_against=news_against,
        confirmed_ict_reversal=confirmed_ict_reversal,
        entry_state=entry_state,
        action=action,
    )
    if post_tp2_manager.get("active"):
        if post_tp2_manager.get("close"):
            trade.status = "CLOSED"
            trade.last_action = post_tp2_manager.get("action") or "EXIT_AFTER_TP2_PROFIT_PROTECT"
            risk_snapshot = active_trade_risk_snapshot(
                trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, trade.last_action
            )
            return {
                "closed": True,
                "action": trade.last_action,
                "exit_reason_code": "POST_TP2_DYNAMIC_PROFIT_PROTECT",
                "exit_quality": "PROFIT_PROTECT_AFTER_TP2",
                "title": post_tp2_manager.get("title") or f"{side} ЗАКРИТИ — TP2 PROFIT PROTECT",
                "recommendation": post_tp2_manager.get("recommendation") or "TP2 взято: прибуток захистити, не чекати дальній стоп",
                "current_pct": current_pct,
                "best_pct": best_pct,
                "recommended_stop": round_price(trade.stop_current),
                "recommended_stop_reason": post_tp2_manager.get("reason"),
                "notes": list(post_tp2_manager.get("notes") or [])[:5],
                "trade_risk": risk_snapshot.get("trade_risk"),
                "trade_risk_score": risk_snapshot.get("trade_risk_score"),
                "reversal_side": risk_snapshot.get("reversal_side"),
                "reversal_score": risk_snapshot.get("reversal_score"),
                "reversal_label": risk_snapshot.get("reversal_label"),
                "entry_state": entry_state,
                "trade_phase": analyze_trade_phase(
                    trade, context, current_pct, best_pct, giveback,
                    high_since_open=high_since_open, low_since_open=low_since_open,
                    entry_state=entry_state, trade_validation=trade_validation,
                    support_votes=support_votes, opposite_votes=opposite_votes,
                    action=trade.last_action, market_regime=market_regime,
                    tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
                    flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
                    news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
                    closed=True, exit_reason_code="POST_TP2_DYNAMIC_PROFIT_PROTECT",
                ),
            }
        new_tp2_stop = post_tp2_manager.get("stop")
        if post_tp2_manager.get("protect") and new_tp2_stop is not None and _apply_more_protective_stop(trade, side, new_tp2_stop, context=context, stage="POST_TP2"):
            trade.tp2_locked_stop = float(trade.stop_current)
            trade.tp2_stop_locked = True
            action = post_tp2_manager.get("action") or "TP2_DYNAMIC_PROTECT"
            title = post_tp2_manager.get("title") or f"{side} — TP2 ДИНАМІЧНИЙ ЗАХИСТ"
            recommendation = post_tp2_manager.get("recommendation") or "TP2 взято: стоп підтягнуто за поточними умовами ринку"
            recommended_stop = trade.stop_current
            recommended_stop_reason = post_tp2_manager.get("reason") or "Post-TP2 Dynamic Profit Protect"
            notes.extend(list(post_tp2_manager.get("notes") or [])[:4])

    if action == "HOLD" and near_entry and opposite_votes >= 2.2 and support_votes <= 1.2:
        action = "PROTECT_OR_EXIT"
        title = f"{side} ПІД ЗАГРОЗОЮ"
        recommendation = "ціна біля входу, підтвердження слабке: захистити позицію або закрити біля входу"
        notes.append("не усереднювати; чекати нового тригера після виходу")
    elif action == "HOLD" and current_pct < -0.28 and opposite_votes >= 2.2:
        action = "EXIT_WARNING"
        title = f"{side} ПІД ЗАГРОЗОЮ"
        recommendation = "позиція під тиском: краще виходити раніше, не чекати дальній стоп"
        notes.append("злам 3m/структури проти позиції")
    elif action == "HOLD" and current_pct > 0 and tf3.get("bias") == opposite(side):
        action = "PROTECT"
        recommendation = "позиція у плюсі, але 3m слабшає: підтягнути стоп"
        notes.append("прибуток не віддавати назад")
    elif action == "HOLD":
        if current_pct > 0:
            recommendation = "утримувати, напрям ще працює"
        else:
            recommendation = "тримати тільки поки 15m/3m не зламаються; стоп обов'язковий"

    trade.last_action = action
    key = active_trade_message_key(trade, action)
    trade.last_message_key = key
    risk_snapshot = active_trade_risk_snapshot(
        trade, context, current_pct, best_pct, giveback, support_votes, opposite_votes, action
    )
    return {
        "closed": False,
        "action": action,
        "title": title,
        "recommendation": recommendation,
        "current_pct": current_pct,
        "best_pct": best_pct,
        "recommended_stop": round_price(trade.stop_current),
        "recommended_stop_reason": recommended_stop_reason,
        "stop_changed": _stop_changed(previous_stop_at_run_start, trade.stop_current),
        "previous_stop": previous_stop_at_run_start,
        "new_stop": round_price(trade.stop_current),
        "stop_change_reason": recommended_stop_reason or _latest_stop_reason_from_notes(notes),
        "notes": notes,
        "trade_risk": risk_snapshot.get("trade_risk"),
        "trade_risk_score": risk_snapshot.get("trade_risk_score"),
        "trade_risk_reasons": risk_snapshot.get("trade_risk_reasons"),
        "reversal_side": risk_snapshot.get("reversal_side"),
        "reversal_score": risk_snapshot.get("reversal_score"),
        "reversal_label": risk_snapshot.get("reversal_label"),
        "reversal_reasons": risk_snapshot.get("reversal_reasons"),
        "exit_score": risk_snapshot.get("exit_score"),
        "exit_signal": risk_snapshot.get("exit_signal"),
        "countertrend_validation": None,
        "trade_validation": risk_snapshot.get("trade_validation"),
        "entry_state": entry_state,
        "lifecycle": lifecycle_snapshot,
        "stop_architecture": {
            "structural_stop": round_price(getattr(trade, "structural_stop", trade.stop_initial)),
            "profit_stop": round_price(getattr(trade, "profit_stop", 0)) if safe_float(getattr(trade, "profit_stop", 0), 0) > 0 else None,
            "active_stop": round_price(trade.stop_current),
            "active_source": getattr(trade, "active_stop_source", "STRUCTURAL"),
        },
        "trade_phase": analyze_trade_phase(
            trade, context, current_pct, best_pct, giveback,
            high_since_open=high_since_open, low_since_open=low_since_open,
            entry_state=entry_state, trade_validation=trade_validation,
            support_votes=support_votes, opposite_votes=opposite_votes,
            action=action, market_regime=market_regime,
            tf3_against=tf3_against, structure_against=structure_against, ict_against=ict_against,
            flow_against=flow_against, cvd_against=cvd_against, liquidity_against=liquidity_against,
            news_against=news_against, confirmed_ict_reversal=confirmed_ict_reversal,
        ),
    }


# ==========================================================
# ENTRY LEVEL GATE
# ==========================================================

ENTRY_LEVEL_LABELS = {
    "ENTRY": "🟢 ПОВНИЙ ВХІД — повний сетап + 3M + структура",
    "RISKY_ENTRY": "🟠 РИЗИКОВАНИЙ ВХІД — рання ICT-точка / старт тренду",
    "WATCH_TRIGGER": "🟡 ЧЕКАТИ ПІДТВЕРДЖЕННЯ — майже готово, потрібне повернення рівня або закриття 3M",
    "BLOCK": "🔴 ВХОДУ НЕМАЄ — сигнал не підтверджений",
}


def _entry_level_label(level):
    return ENTRY_LEVEL_LABELS.get(str(level or "BLOCK"), ENTRY_LEVEL_LABELS["BLOCK"])


def _has_text(items, needles):
    text = " ".join([str(x) for x in (items or [])]).lower()
    return any(str(n).lower() in text for n in needles)


def apply_entry_level_gate(setup, context=None):
    """Normalize the final decision into the 4 professional entry levels.

    This is a final safety/clarity layer. It does not weaken any filter.
    It only guarantees that:
      - clean ENTRY never prints above 90/100;
      - early/risky entries stay inside 62-79/100;
      - almost-ready setups are WATCH_TRIGGER 55-67/100;
      - late chase / range-middle / bad RR / no clean setup are BLOCK <=55/100.
    """
    if not isinstance(setup, dict):
        return setup

    out = dict(setup)
    context = context or {}
    action = str(out.get("action") or "NO_TRADE").upper()
    setup_info = out.get("setup_classifier") or context.get("setup_classifier") or {}
    setup_type = str((setup_info or {}).get("type") or "")
    if setup_type == "NO_CLEAN_SETUP":
        return _v3_canonicalize_no_clean_setup(out, stage="ENTRY_LEVEL_GATE")
    conflicts = out.get("conflicts") or []
    reason = str(out.get("reason") or "")
    quality = int(max(0, min(100, out.get("quality", 0) or 0)))

    rr_bad = _has_text(conflicts + [reason], ["RR", "Smart Money", "risk-reward", "ризик/прибуток"])
    hard_block = bool(
        action in ["NO_TRADE", "BLOCK"]
        or out.get("hard_no_chase")
        or out.get("exhausted_move")
        or out.get("ict_balance")
        or rr_bad
        or setup_type in ["LATE_IMPULSE_CHASE", "COUNTERTREND_PULLBACK_WAIT", "RANGE_MIDDLE_BLOCK"]
        or (setup_type == "NO_CLEAN_SETUP" and not setup_info.get("entry_allowed"))
        or (setup_info.get("block_entry") and not setup_info.get("entry_allowed") and action not in ["ENTRY", "RISKY_ENTRY"])
    )

    if action == "ENTRY":
        level = "ENTRY"
        quality = int(max(68, min(90, quality)))
    elif action == "RISKY_ENTRY":
        level = "RISKY_ENTRY"
        quality = int(max(62, min(79, quality)))
    elif hard_block:
        level = "BLOCK"
        quality = int(min(55, quality))
    else:
        # WATCH that has a side/plan is an activation setup, not a dead block.
        # Keep it visible as preparation, but cap the score so it is not confused
        # with a real entry.
        level = "WATCH_TRIGGER"
        quality = int(max(55, min(67, quality)))

    out["entry_level"] = level
    out["entry_level_label"] = _entry_level_label(level)
    out["quality"] = quality

    # Preserve legacy action values for the rest of the bot, but make blocks explicit.
    if level == "BLOCK":
        out["entry_blocked"] = True
        out["show_wait_plan"] = False if setup_type in ["LATE_IMPULSE_CHASE", "COUNTERTREND_PULLBACK_WAIT", "RANGE_MIDDLE_BLOCK", "NO_CLEAN_SETUP"] else out.get("show_wait_plan", False)
    elif level == "WATCH_TRIGGER":
        out["entry_blocked"] = False
        out["watch_trigger"] = True
        out["show_wait_plan"] = True if out.get("side") in ["LONG", "SHORT"] and out.get("plan") else out.get("show_wait_plan", True)
    return out


PENDING_TRIGGER_SETUP_TYPES = {
    "TREND_CONTINUATION",
    "TREND_IGNITION_ENTRY",
    "PULLBACK_CONTINUATION",
    "PULLBACK_CONTINUATION_FAST_ENTRY",
    "PRIME_ICT_LOCATION_OVERRIDE",
    "RANGE_COMPRESSION_BREAKOUT",
    "SWEEP_REVERSAL",
    "SWEEP_RECLAIM_EARLY_ENTRY",
    "RANGE_EDGE_TRADE",
    "NEWS_IMPULSE",
}

PENDING_TRIGGER_BLOCK_TYPES = {"LATE_IMPULSE_CHASE", "COUNTERTREND_PULLBACK_WAIT", "RANGE_MIDDLE_BLOCK", "NO_CLEAN_SETUP"}


def _pending_trigger_age_minutes(pending):
    ts = _parse_iso_time((pending or {}).get("created_at"))
    if not ts:
        return 99999.0
    return max(0.0, (now_utc() - ts).total_seconds() / 60.0)


def _pending_trigger_price_ok(pending, context, setup):
    """Do not activate an old WATCH trigger after price has already run away."""
    if not isinstance(pending, dict) or not isinstance(context, dict):
        return False, "попередній тригер недоступний"
    side = pending.get("side")
    price = safe_float(context.get("price"))
    old_price = safe_float(pending.get("price"))
    atr15 = safe_float(context.get("atr15")) or ((price or 90) * 0.006)
    if side not in ["LONG", "SHORT"] or not price or not old_price:
        return False, "попередній тригер без ціни/сторони"

    drift_pct = abs(price - old_price) / old_price * 100.0
    max_drift_pct = max(0.58, min(0.95, (atr15 / price * 100.0) * 1.65 if price else 0.75))
    if drift_pct > max_drift_pct:
        return False, f"ціна відійшла від попереднього тригера на {round(drift_pct, 3)}% — не доганяти"

    old_plan = pending.get("plan") if isinstance(pending.get("plan"), dict) else {}
    old_tp1 = safe_float(old_plan.get("tp1"))
    if side == "LONG" and old_tp1 and price >= old_tp1:
        return False, "ціна вже біля/вище старого TP1 — це буде доганяння"
    if side == "SHORT" and old_tp1 and price <= old_tp1:
        return False, "ціна вже біля/нижче старого TP1 — це буде доганяння"
    return True, f"ціна ще в межах попереднього тригера ({round(drift_pct, 3)}%)"


def _pending_trigger_confirmation_ok(side, context, setup):
    if side not in ["LONG", "SHORT"] or not isinstance(context, dict):
        return False, "сторона не підтверджена"
    tf3 = context.get("tf3") or {}
    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    cvd = context.get("cvd") or {}
    flow = context.get("flow") or {}
    derivatives = context.get("derivatives") or {}
    setup_info = (setup or {}).get("setup_classifier") or context.get("setup_classifier") or {}

    tf3_score_abs = abs(int(tf3.get("score", 0) or 0))
    tf3_same = tf3.get("bias") == side and tf3_score_abs >= 18
    tf3_strong_against = tf3.get("bias") == opposite(side) and tf3_score_abs >= 42
    phase = str(structure.get("phase") or "").upper()
    structure_reclaim = bool(
        structure.get("bias") == side
        or (side == "LONG" and any(x in phase for x in ["BOS LONG", "CHOCH LONG", "DOWNSIDE SWEEP"]))
        or (side == "SHORT" and any(x in phase for x in ["BOS SHORT", "CHOCH SHORT", "UPSIDE SWEEP"]))
    )
    ict_setup = str(ict.get("setup") or "").upper()
    ict_reclaim = bool(
        ict.get("bias") == side
        and (
            ict.get("entry_ok")
            or (side == "LONG" and ict_setup in ["LIQUIDITY_SWEEP_LONG", "BOS_LONG_RETRACE_FVG_OB", "BOS_LONG_CONTINUATION_HOLD", "DISCOUNT_FVG_OB_LONG"])
            or (side == "SHORT" and ict_setup in ["LIQUIDITY_SWEEP_SHORT", "BOS_SHORT_RETRACE_FVG_OB", "BOS_SHORT_CONTINUATION_HOLD", "PREMIUM_FVG_OB_SHORT"])
        )
    )
    classifier_ready = bool(setup_info.get("entry_allowed") and setup_info.get("type") in PENDING_TRIGGER_SETUP_TYPES)
    pressure_against_count = sum([
        cvd.get("bias") == opposite(side) and abs(int(cvd.get("score", 0) or 0)) >= 22,
        flow.get("bias") == opposite(side) and abs(int(flow.get("score", 0) or 0)) >= 18,
        derivatives.get("bias") == opposite(side) and abs(int(derivatives.get("score", 0) or 0)) >= 16,
    ])

    if tf3_strong_against and pressure_against_count >= 1:
        return False, "3M і потік вже проти pending-trigger"
    if pressure_against_count >= 2:
        return False, "CVD/flow/OI разом проти pending-trigger"
    if tf3_same:
        return True, "3M підтвердив попередній тригер"
    if structure_reclaim:
        return True, "структура підтвердила попередній тригер"
    if ict_reclaim:
        return True, "ICT/reclaim підтвердив попередній тригер"
    if classifier_ready:
        return True, "класифікатор сетапу підтвердив попередній тригер"
    return False, "попередній тригер ще не підтверджений"


def activate_pending_trigger_if_ready(context, setup):
    """Turn a recent WATCH_TRIGGER 60-67 into RISKY_ENTRY only after confirmation.

    This increases the number of good entries without lowering global thresholds.
    Hard no-chase, RR, liquidity and exhaustion filters still win.
    """
    if not isinstance(context, dict) or not isinstance(setup, dict):
        return setup
    if setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return setup

    pending = context.get("pending_trigger_memory") or {}
    if not isinstance(pending, dict) or not pending.get("active"):
        return setup
    age_min = _pending_trigger_age_minutes(pending)
    if age_min > 90:
        return setup

    side = pending.get("side")
    if side not in ["LONG", "SHORT"] or setup.get("side") != side:
        return setup
    setup_info = setup.get("setup_classifier") or context.get("setup_classifier") or {}
    setup_type = str(setup_info.get("type") or pending.get("setup_type") or "")
    if setup_type in PENDING_TRIGGER_BLOCK_TYPES:
        return setup
    if setup.get("entry_level") == "BLOCK" or setup.get("hard_no_chase") or setup.get("exhausted_move") or setup.get("entry_blocked"):
        return setup

    price_ok, price_reason = _pending_trigger_price_ok(pending, context, setup)
    if not price_ok:
        return setup
    confirm_ok, confirm_reason = _pending_trigger_confirmation_ok(side, context, setup)
    if not confirm_ok:
        return setup

    late, late_reason = is_late_chase(side, context)
    late_penalty = soft_late_entry_penalty(side, context, late_reason) if late else 0
    exhausted, exhausted_reason = detect_exhausted_move(side, context)
    if exhausted or late_penalty >= 15:
        return setup

    plan = setup.get("plan") or make_plan(side, context)
    rr = smart_money_rr_status(plan)
    if not rr.get("ok"):
        return setup

    cap_by_type = {
        "SWEEP_RECLAIM_EARLY_ENTRY": 74,
        "SWEEP_REVERSAL": 74,
        "PULLBACK_CONTINUATION_FAST_ENTRY": 76,
        "PULLBACK_CONTINUATION": 76,
        "RANGE_COMPRESSION_BREAKOUT": 77,
        "TREND_IGNITION_ENTRY": 79,
        "PRIME_ICT_LOCATION_OVERRIDE": 79,
        "TREND_CONTINUATION": 79,
    }
    cap = cap_by_type.get(setup_type, 76)
    quality = int(max(62, min(cap, max(int(setup.get("quality", 0) or 0), int(pending.get("quality", 0) or 0)))))

    out = dict(setup)
    out.update({
        "action": "RISKY_ENTRY",
        "entry_level": "RISKY_ENTRY",
        "entry_level_label": _entry_level_label("RISKY_ENTRY"),
        "quality": quality,
        "title": f"РИЗИКОВАНИЙ ВХІД {side} — ПІДТВЕРДЖЕНО ПОПЕРЕДНІЙ ТРИГЕР",
        "reason": f"попередній тригер підтверджено: {confirm_reason}; {price_reason}",
        "plan": plan,
        "pending_trigger_activated": True,
        "pending_trigger_age_min": round(age_min, 1),
        "setup_classifier": setup_info,
        "confirmations": list(dict.fromkeys((setup.get("confirmations") or []) + [confirm_reason, price_reason])),
        "conflicts": setup.get("conflicts") or [],
    })
    out.pop("watch_trigger", None)
    out.pop("entry_blocked", None)
    return out


def should_store_pending_trigger(setup):
    if not isinstance(setup, dict):
        return False
    if setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return False
    if setup.get("entry_level") != "WATCH_TRIGGER":
        return False
    quality = int(setup.get("quality", 0) or 0)
    if not (60 <= quality <= 67):
        return False
    if setup.get("hard_no_chase") or setup.get("exhausted_move") or setup.get("entry_blocked"):
        return False
    side = setup.get("side")
    setup_info = setup.get("setup_classifier") or {}
    setup_type = str(setup_info.get("type") or "")
    return bool(side in ["LONG", "SHORT"] and setup.get("plan") and setup_type in PENDING_TRIGGER_SETUP_TYPES and setup_type not in PENDING_TRIGGER_BLOCK_TYPES)


def update_pending_trigger_memory(state, setup, context):
    """Persist only high-quality almost-ready triggers; clear stale/dangerous ones."""
    if not isinstance(state, dict):
        return
    old = state.get("pending_trigger") if isinstance(state.get("pending_trigger"), dict) else None
    if old and _pending_trigger_age_minutes(old) > 90:
        state["pending_trigger"] = None
        old = None

    if isinstance(setup, dict) and setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        state["pending_trigger"] = None
        return

    if should_store_pending_trigger(setup):
        plan = setup.get("plan")
        setup_info = setup.get("setup_classifier") or {}
        state["pending_trigger"] = {
            "active": True,
            "created_at": iso_now(),
            "side": setup.get("side"),
            "price": round_price((context or {}).get("price")),
            "quality": int(setup.get("quality", 0) or 0),
            "setup_type": setup_info.get("type"),
            "setup_label": setup_info.get("label"),
            "reason": setup.get("reason"),
            "regime_engine": setup.get("regime_engine") or (context or {}).get("regime_engine"),
            "plan": asdict(plan) if plan else None,
        }
        return

    if old:
        # Keep a valid pending trigger while the bot still watches the same side;
        # clear it if the current state becomes a block, opposite side, or no setup.
        current_side = setup.get("side") if isinstance(setup, dict) else None
        current_level = setup.get("entry_level") if isinstance(setup, dict) else None
        if current_side not in [old.get("side"), "NEUTRAL"] or current_level == "BLOCK":
            state["pending_trigger"] = None



def _rescue_setup_type(event, snapshot):
    event_type = str((event or {}).get("type") or "")
    closed = bool((snapshot or {}).get("closed_confirmed"))
    if event_type == "SWEEP_RECLAIM":
        return "SWEEP_REVERSAL" if not closed else "SWEEP_RECLAIM_EARLY_ENTRY"
    if event_type == "BREAKOUT_RETEST":
        return "TREND_CONTINUATION" if closed else "PULLBACK_CONTINUATION_FAST_ENTRY"
    if event_type == "BREAKOUT_ACCEPTANCE":
        return "TREND_IGNITION_ENTRY"
    if event_type == "ICT_ZONE_RECLAIM":
        return "PULLBACK_CONTINUATION" if closed else "PULLBACK_CONTINUATION_FAST_ENTRY"
    return "PRIME_ICT_LOCATION_OVERRIDE"


def _build_rescue_setup_classifier(side, event, snapshot):
    setup_type = _rescue_setup_type(event, snapshot)
    profile = setup_trade_profile(setup_type)
    rules = _setup_rules(setup_type, side)
    force_risky = bool(
        not snapshot.get("closed_confirmed")
        or setup_type in {"TREND_IGNITION_ENTRY", "PULLBACK_CONTINUATION_FAST_ENTRY", "SWEEP_REVERSAL"}
    )
    result = {
        "type": setup_type,
        "label": _setup_label(setup_type),
        "side": side,
        "score": int(event.get("score", ENTRY_RESCUE_MIN_SCORE) or ENTRY_RESCUE_MIN_SCORE),
        "entry_allowed": True,
        "block_entry": False,
        "risk_mode": "RISKY" if force_risky else "NORMAL",
        "reason": "3M сетап відновлено з повної послідовності свічок між 15-хвилинними запусками",
        "quality_adjustment": int(profile.get("quality_adjustment", 0) or 0),
        "quality_cap": profile.get("quality_cap"),
        "force_risky": force_risky,
        "profile": profile,
        "interval_rescue": True,
    }
    result.update(rules)
    return result


def _rescue_candidate_utility(setup, event, snapshot):
    plan = (setup or {}).get("plan")
    if not plan or not getattr(plan, "valid", False):
        return -999.0
    rr1 = safe_float(getattr(plan, "rr1", 0), 0) or 0
    event_score = int((event or {}).get("score", 0) or 0)
    confidence = int((snapshot or {}).get("confidence", 50) or 50)
    extension = safe_float((event or {}).get("extension_atr15"), 0) or 0
    age = safe_float((event or {}).get("age_min"), 0) or 0
    return event_score * 0.48 + confidence * 0.30 + min(rr1, 3.0) * 8.0 - extension * 12.0 - age * 0.12


def resolve_15m_scheduler_entry_opportunity(context, base_setup):
    """Try all professional entry paths visible inside the last 15 minutes.

    It is invoked only when the normal engine did not already produce a valid
    ENTRY/RISKY_ENTRY. Therefore it raises entry coverage without disturbing a
    good existing setup and without requiring 3-minute workflow notifications.
    """
    if not isinstance(context, dict) or not isinstance(base_setup, dict):
        return base_setup
    base_plan = base_setup.get("plan")
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and base_plan and getattr(base_plan, "valid", False):
        return base_setup
    if context.get("price_warning"):
        return base_setup

    candidate_sides = []
    for side in [base_setup.get("side"), context.get("bias"), "LONG", "SHORT"]:
        if side in ["LONG", "SHORT"] and side not in candidate_sides:
            candidate_sides.append(side)

    candidates = []
    for side in candidate_sides:
        # Explicit liquidity blocks stay hard. Post-impulse rejection is
        # timestamp-aware: every trigger from before the rejection is cancelled,
        # while a genuinely new reclaim/retest after it may reopen only a RISKY setup.
        if side in ((context.get("liquidity") or {}).get("blocks") or []):
            continue
        rejection_guard = post_impulse_rejection_snapshot(side, context)
        rejection_ts = int(rejection_guard.get("rejection_ts", 0) or 0)
        rejection_active = rejection_guard.get("severity") in ["MODERATE", "STRONG", "RECOVERED"]
        for event in scan_15m_interval_entry_events(context, side):
            if not event.get("anchor_confirmed") or not event.get("professional_location"):
                continue
            event_ts = int(event.get("trigger_ts", 0) or 0)
            recovery_event_quality = _strict_post_rejection_recovery_event(
                side, context, event, rejection_ts=rejection_ts,
                severity="STRONG" if rejection_guard.get("severity") == "STRONG" else "MODERATE",
            ) if rejection_active else {"passed": False}
            fresh_after_rejection = bool(rejection_active and recovery_event_quality.get("passed"))
            if rejection_active and not fresh_after_rejection:
                # Old interval triggers are invalid once a rejection happened.
                continue
            if rejection_guard.get("block_entry") and not fresh_after_rejection:
                continue
            # A pure two-close breakout is not enough against both 1H and 4H.
            both_htf_against = bool(
                (context.get("tf1h") or {}).get("bias") == opposite(side)
                and (context.get("tf4h") or {}).get("bias") == opposite(side)
            )
            if both_htf_against and event.get("type") == "BREAKOUT_ACCEPTANCE":
                continue

            work = dict(context)
            work["entry_rescue_event"] = event
            tf3 = dict(work.get("tf3") or {})
            directional_score = max(28, min(68, int(event.get("score", 66) or 66) - 10))
            tf3["bias"] = side
            tf3["score"] = directional_score if side == "LONG" else -directional_score
            tf3["state"] = "INTERVAL_TRIGGER_RECOVERED"
            tf3["note"] = "3M тригер зафіксований у послідовності між 15-хвилинними запусками"
            work["tf3"] = tf3
            # Rebuild Dual-Speed after injecting the recovered event.
            work["dual_speed_mtf"] = build_dual_speed_mtf(work)
            snapshot = (work["dual_speed_mtf"] or {}).get(side) or {}
            # Countertrend rescue needs the strict multi-layer reversal bridge;
            # a candle pattern alone must not override both higher timeframes.
            if both_htf_against and not (snapshot.get("fast_reversal_to_side") or {}).get("confirmed"):
                continue
            if context.get("bias") == opposite(side) and not snapshot.get("closed_confirmed") and not (snapshot.get("fast_reversal_to_side") or {}).get("confirmed"):
                continue
            setup_info = _build_rescue_setup_classifier(side, event, snapshot)
            work["setup_classifier"] = setup_info
            plan = make_plan(side, work)
            if not plan or not getattr(plan, "valid", False):
                continue
            if snapshot.get("emergency_against"):
                continue

            closed_confirmed = bool(snapshot.get("closed_confirmed"))
            event_type = str(event.get("type") or "")
            full_entry_event = bool(
                closed_confirmed
                and event_type in ["BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"]
                and int(event.get("score", 0) or 0) >= 72
                and safe_float(event.get("extension_atr15"), 99) <= 0.70
                and not setup_info.get("force_risky")
                and not rejection_guard.get("force_risky")
                and not fresh_after_rejection
            )
            action = "ENTRY" if full_entry_event else "RISKY_ENTRY"
            raw_quality = int(round(
                int(event.get("score", 0) or 0) * 0.55
                + int(snapshot.get("confidence", 50) or 50) * 0.30
                + int(base_setup.get("quality", 50) or 50) * 0.15
            ))
            if safe_float(getattr(plan, "rr1", 0), 0) >= 2.0:
                raw_quality += 3
            quality = int(max(ENTRY_QUALITY_MIN, min(90, raw_quality))) if action == "ENTRY" else int(max(RISKY_QUALITY_MIN, min(79, raw_quality)))
            candidate = dict(base_setup)
            candidate.update({
                "action": action,
                "side": side,
                "quality": quality,
                "title": ("ВХІД " if action == "ENTRY" else "РИЗИКОВАНИЙ ВХІД ") + side,
                "reason": (
                    "новий 3M сетап сформувався після відхилення; старий тригер анульовано"
                    if fresh_after_rejection else
                    "професійний 3M тригер знайдено у свічках між 15-хвилинними перевірками"
                ),
                "plan": plan,
                "setup_classifier": setup_info,
                "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + list(event.get("evidence") or []))),
                "conflicts": [x for x in (base_setup.get("conflicts") or []) if "тригер" not in str(x).lower()],
                "entry_rescue_event": event,
                "scheduler_rescue": True,
                "post_rejection_recovery": ({
                    "active": True,
                    "trigger_ts": event_ts,
                    "trigger_level": round_price(event.get("trigger_level")),
                    "event_type": str(event.get("type") or ""),
                    "quality": recovery_event_quality,
                } if fresh_after_rejection else None),
                "entry_level": action,
                "entry_level_label": _entry_level_label(action),
            })
            utility = _rescue_candidate_utility(candidate, event, snapshot)
            candidates.append((utility, candidate, work, snapshot))

    if not candidates:
        return base_setup
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, selected, selected_context, snapshot = candidates[0]
    # Persist the selected internal interpretation so the unified lifecycle sees
    # the same setup, rather than re-evaluating it from the old current-only 3M state.
    context["entry_rescue_event"] = selected.get("entry_rescue_event")
    context["tf3"] = selected_context.get("tf3")
    context["setup_classifier"] = selected.get("setup_classifier")
    context["dual_speed_mtf"] = selected_context.get("dual_speed_mtf")
    selected["dual_speed_snapshot"] = snapshot
    return selected



def _latest_closed_trade_snapshot(context, side):
    state = (context or {}).get("runtime_state") if isinstance((context or {}).get("runtime_state"), dict) else {}
    item = state.get("last_closed_trade") if isinstance(state, dict) else None
    if not isinstance(item, dict) or item.get("side") != side:
        item = None
        for row in reversed((state or {}).get("history") or []):
            if row.get("type") == "TRADE_CLOSED" and row.get("side") == side:
                item = row
                break
    if not isinstance(item, dict):
        return None
    closed_at = item.get("closed_at") or item.get("time")
    closed_ms = _utc_ms_from_iso(closed_at)
    if not closed_ms:
        return None
    return {**item, "closed_at": closed_at, "closed_ms": closed_ms}


def fresh_base_continuation_snapshot(context, side):
    """Adaptive new-base continuation: 3 compact candles or normal 4-6 candle base."""
    if side not in ["LONG", "SHORT"]:
        return {"active": False, "allowed": False}
    last = _latest_closed_trade_snapshot(context, side)
    if not last:
        return {"active": False, "allowed": False, "reason": "немає попередньої закритої угоди цього напрямку"}
    candles = [c for c in list((context or {}).get("candles_15m_closed") or []) if int(getattr(c, "ts", 0) or 0) > int(last["closed_ms"])]
    if len(candles) < ADAPTIVE_FRESH_BASE_MIN_CANDLES:
        return {"active": True, "allowed": False, "candles_after": len(candles), "reason": "після виходу ще не сформована нова 15M база"}
    candles = candles[-FRESH_BASE_MAX_15M_CANDLES:]
    price = safe_float((context or {}).get("price"), candles[-1].close) or candles[-1].close
    atr15 = safe_float((context or {}).get("atr15"), None) or safe_float(((context or {}).get("tf15") or {}).get("atr"), None) or max(price * 0.006, 0.01)
    events = [e for e in scan_15m_interval_entry_events(context, side) if int(e.get("trigger_ts", 0) or 0) > int(last["closed_ms"])]
    event = next((e for e in events if e.get("confirmed") and e.get("anchor_confirmed") and e.get("type") in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM", "SWEEP_RECLAIM"} and (e.get("follow_through") or e.get("post_confirmed") or int(e.get("score", 0) or 0) >= 74)), None)

    fast_mode = len(candles) == ADAPTIVE_FRESH_BASE_MIN_CANDLES
    if fast_mode:
        base = candles[:-1]; confirm = candles[-1:]
    else:
        base = candles[:-2] if len(candles) >= 4 else candles[:-1]
        confirm = candles[-2:]
    if len(base) < 2 or not confirm:
        return {"active": True, "allowed": False, "reason": "нова база ще надто коротка"}
    base_high = max(c.high for c in base); base_low = min(c.low for c in base)
    width_atr = (base_high - base_low) / max(atr15, 1e-9)
    pullback = any(_opposite_side_candle(c, side) for c in base)
    ranges = [max(c.high - c.low, 1e-9) for c in base]
    compacting = bool(len(ranges) < 2 or ranges[-1] <= ranges[0] * 1.05)
    if side == "LONG":
        retrace = max(0.0, safe_float(last.get("close_price"), base[0].open) - base_low)
        if fast_mode:
            accepted = bool(confirm[-1].close > base_high + atr15 * 0.03 and event)
            renewed_pivot = confirm[-1].close > base[-1].high and confirm[-1].low >= base_low
        else:
            accepted = bool(all(c.close > base_high + atr15 * 0.03 for c in confirm) and min(c.low for c in confirm) <= base_high + atr15 * 0.32 and confirm[-1].close >= confirm[-2].close)
            renewed_pivot = confirm[-1].low >= confirm[-2].low and confirm[-1].close > confirm[-2].high
        stop_level = min(c.low for c in candles[-5:]) - max(atr15 * 0.14, price * 0.0007)
        trigger_level = base_high
    else:
        retrace = max(0.0, base_high - safe_float(last.get("close_price"), base[0].open))
        if fast_mode:
            accepted = bool(confirm[-1].close < base_low - atr15 * 0.03 and event)
            renewed_pivot = confirm[-1].close < base[-1].low and confirm[-1].high <= base_high
        else:
            accepted = bool(all(c.close < base_low - atr15 * 0.03 for c in confirm) and max(c.high for c in confirm) >= base_low - atr15 * 0.32 and confirm[-1].close <= confirm[-2].close)
            renewed_pivot = confirm[-1].high <= confirm[-2].high and confirm[-1].close < confirm[-2].low
        stop_level = max(c.high for c in candles[-5:]) + max(atr15 * 0.14, price * 0.0007)
        trigger_level = base_low
    fast_layers = _professional_fast_layer_permission(context, side, strong_15m=bool(accepted or renewed_pivot))
    ema20v = safe_float(((context or {}).get("tf15") or {}).get("ema20"), None)
    extension = abs(price - ema20v) / atr15 if ema20v else 0.0
    structure = (context or {}).get("structure") or {}
    structural = bool(structure.get("bias") == side or renewed_pivot or accepted)
    meaningful_retrace = bool(retrace >= atr15 * FRESH_BASE_MIN_RETRACE_ATR15 or pullback)
    rolling = rolling_balance_detector(context)
    balance_ok = bool(not rolling.get("balance") or (rolling.get("breakout_accepted") and rolling.get("accepted_side") == side))
    trend_context = bool(((context or {}).get("tf15") or {}).get("bias") == side and (((context or {}).get("tf1h") or {}).get("bias") == side or str((((context or {}).get("regime_engine") or {}).get("regime_type") or "")).upper() in {"TREND_EXPANSION", "PULLBACK"}))
    compact_fast_base = bool(fast_mode and width_atr <= ADAPTIVE_FRESH_BASE_MAX_WIDTH_ATR15 and compacting and event)
    width_ok = bool(width_atr <= (ADAPTIVE_FRESH_BASE_MAX_WIDTH_ATR15 if fast_mode else FRESH_BASE_MAX_WIDTH_ATR15))
    allowed = bool(width_ok and meaningful_retrace and structural and accepted and fast_layers.get("risky_ok") and extension <= FRESH_BASE_MAX_EMA_EXTENSION_ATR15 and balance_ok and trend_context and (not fast_mode or compact_fast_base))
    return {
        "active": True, "allowed": allowed, "side": side, "candles_after": len(candles),
        "adaptive_fast_base": fast_mode, "required_candles": 3 if fast_mode else FRESH_BASE_MIN_15M_CANDLES,
        "base_high": round_price(base_high), "base_low": round_price(base_low), "width_atr": round(width_atr, 3),
        "compacting": compacting, "meaningful_retrace": meaningful_retrace, "accepted_breakout": accepted, "event": event,
        "fast_support": fast_layers.get("support", 0), "fast_against": fast_layers.get("against", 0),
        "neutral_fast_allowed": bool(fast_layers.get("neutral_only") and allowed), "extension_atr": round(extension, 3),
        "balance_ok": balance_ok, "trend_context": trend_context,
        "stop_level": round_price(stop_level), "trigger_level": round_price(trigger_level),
        "trigger_ts": int((event or {}).get("trigger_ts", confirm[-1].ts) or confirm[-1].ts),
        "reason": ("компактна 3-свічкова 15M база + 3M breakout-retest + flow/нейтральні fast-шари" if allowed and fast_mode
                   else ("нова 15M база + accepted breakout/retest + flow сформовані" if allowed
                         else "для continuation потрібні компактна 3-свічкова або звичайна 4-6 свічкова база, ретест і відсутність сильного потоку проти")),
    }



def resolve_fresh_base_continuation_reentry(context, base_setup):
    if not isinstance(context, dict) or not isinstance(base_setup, dict):
        return base_setup
    candidates = []
    for side in ["LONG", "SHORT"]:
        standard = fresh_base_continuation_snapshot(context, side)
        reset = fresh_base_exhaustion_reset_snapshot(context, side)
        standard_valid = bool(standard.get("allowed"))
        extended_base_valid = _fresh_base_valid(reset)
        snap = standard if standard_valid else reset
        if not (standard_valid or extended_base_valid):
            continue
        # A 6-12 candle base may be a normal continuation base.  It becomes an
        # EXHAUSTION reset only when an old EXHAUSTION/post-shock lock is active.
        reset_mode = bool(snap is reset and _exhaustion_reset_allowed(reset))
        last_same_side = _latest_closed_trade_snapshot(context, side)
        setup_type = "FRESH_BASE_CONTINUATION_REENTRY" if last_same_side else "RANGE_COMPRESSION_BREAKOUT"
        event = snap.get("event") or {
            "confirmed": True, "anchor_confirmed": True, "professional_location": True,
            "type": "BREAKOUT_RETEST", "side": side, "score": int(snap.get("score", 74) or 74),
            "trigger_ts": snap.get("trigger_ts"), "trigger_level": snap.get("trigger_level"),
            "follow_through": True, "post_confirmed": True,
            "evidence": (["нова 6-12 свічкова 15M база", "старий EXHAUSTION скинуто", "accepted breakout + ретест/прийняття", "flow підтверджує"] if reset_mode else ["нова 15M база", "accepted breakout/retest", "flow підтверджує"]),
            "source": "FRESH_BASE_EXHAUSTION_RESET" if reset_mode else "FRESH_BASE_CONTINUATION",
        }
        work = dict(context)
        work["bias"] = side
        work["entry_rescue_event"] = event
        work["fresh_base_geometry"] = {"confirmed": True, "side": side, "stop_level": snap.get("stop_level")}
        setup_info = {
            "type": setup_type, "label": ("🟢 Нове продовження після бази" if setup_type == "FRESH_BASE_CONTINUATION_REENTRY" else "🟢 Пробій після нової бази"),
            "side": side, "score": int(snap.get("score", 75) or 75), "entry_allowed": True, "block_entry": False,
            "risk_mode": "RISKY", "force_risky": True, "professional_override": True,
            "reason": snap.get("reason"), "quality_adjustment": 2, "quality_cap": 82,
            "profile": setup_trade_profile("TREND_CONTINUATION"),
            "entry_rule": "нова 6-12 свічкова 15M база + accepted breakout + ретест/прийняття + flow" if reset_mode else "після попереднього виходу сформована нова 15M база + breakout/retest + flow",
            "stop_rule": "стоп за новою 15M/ICT базою",
            "tp_rule": "цілі до наступної зовнішньої ліквідності",
            "management_rule": "вести як окремий continuation, а не доганяння старого імпульсу",
        }
        work["setup_classifier"] = setup_info
        work["force_durable_stop_role"] = True
        work["dual_speed_mtf"] = build_dual_speed_mtf(work)
        plan = make_plan(side, work)
        if not plan or not getattr(plan, "valid", False):
            continue
        tf1 = (context.get("tf1h") or {}).get("bias") == side
        regime = context.get("regime_engine") or {}
        stable = bool(regime.get("is_stable", True))
        fast_against = int(snap.get("fast_against", 0) or 0)
        full = bool(not reset_mode and tf1 and stable and str(regime.get("entry_action") or "ALLOW").upper() == "ALLOW" and fast_against == 0)
        action = "ENTRY" if full else "RISKY_ENTRY"
        quality = 82 if full else min(79, max(74, int(snap.get("score", 76) or 76)))
        candidate = dict(base_setup)
        candidate.update({
            "action": action, "side": side, "quality": quality,
            "title": ("ВХІД " if full else "РИЗИКОВАНИЙ ВХІД ") + side,
            "reason": snap.get("reason"), "plan": plan, "setup_classifier": setup_info,
            "entry_rescue_event": event, "fresh_base_reentry_confirmed": setup_type == "FRESH_BASE_CONTINUATION_REENTRY",
            "fresh_base_snapshot": snap, "fresh_base_exhaustion_reset_snapshot": reset if reset_mode else {},
            "fresh_base_exhaustion_reset": reset_mode, "transition_override_confirmed": reset_mode,
            "entry_level": action, "entry_level_label": _entry_level_label(action),
            "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + list(event.get("evidence") or []))),
        })
        candidates.append((quality + min(getattr(plan, "rr1", 0), 3) * 4 + (4 if reset_mode else 0), candidate, work, reset_mode))
    if not candidates:
        return base_setup
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, candidate, work, reset_mode = candidates[0]
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and base_setup.get("side") != candidate.get("side"):
        return base_setup
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and int(base_setup.get("quality", 0) or 0) >= candidate["quality"] + 4:
        return base_setup
    context["entry_rescue_event"] = candidate.get("entry_rescue_event")
    context["setup_classifier"] = candidate.get("setup_classifier")
    context["fresh_base_geometry"] = work.get("fresh_base_geometry")
    context["dual_speed_mtf"] = work.get("dual_speed_mtf")
    if reset_mode:
        context["fresh_base_exhaustion_reset"] = candidate.get("fresh_base_exhaustion_reset_snapshot")
    return candidate


def _dominant_shock_side(context):
    snaps = {side: _entry_consensus_raw_post_shock_snapshot(context, side) for side in ["LONG", "SHORT"]}
    active = [(safe_float(s.get("run_atr"), 0.0), side) for side, s in snaps.items() if s.get("active")]
    if active:
        return max(active)[1]
    regime = (context or {}).get("regime_engine") or {}
    bias = str(((regime.get("metrics") or {}).get("bias") or "")).upper()
    return bias if bias in ["LONG", "SHORT"] else None


def _transition_priority_confirmed(setup, context):
    st = _entry_setup_type(setup)
    if st == "CLOSED_15M_DIRECTION_FLIP":
        snap = setup.get("closed_15m_displacement_override") or context.get("closed_15m_displacement_override") or {}
        return bool(snap.get("confirmed"))
    if st == "CAPITULATION_RECOVERY":
        return bool(setup.get("capitulation_recovery_confirmed"))
    if st == "FRESH_BASE_CONTINUATION_REENTRY":
        return bool(setup.get("fresh_base_reentry_confirmed"))
    return False


def resolve_capitulation_recovery_geometry_opportunity(context, base_setup):
    if not isinstance(context, dict) or not isinstance(base_setup, dict):
        return base_setup
    candidates = []
    for side in ["LONG", "SHORT"]:
        snap = capitulation_recovery_snapshot(context, side)
        if not (snap.get("active") and snap.get("allowed") and snap.get("geometry_ready")):
            continue
        event = snap.get("event") or {
            "confirmed": True, "anchor_confirmed": True, "professional_location": True,
            "type": "SWEEP_RECLAIM", "side": side, "score": 76,
            "trigger_ts": snap.get("trigger_ts"), "trigger_level": snap.get("reclaim_level"),
            "follow_through": True, "post_confirmed": True,
            "evidence": ["15M capitulation reclaim", "higher low/lower high", "3M BOS/retest", "flow/CVD"],
            "source": "CAPITULATION_RECOVERY",
        }
        work = dict(context)
        work["bias"] = side
        work["entry_rescue_event"] = event
        work["capitulation_recovery_geometry"] = {"confirmed": True, "side": side, "stop_level": snap.get("base_stop_level")}
        setup_info = {
            "type": "CAPITULATION_RECOVERY", "label": "🟢 Відновлення після капітуляції",
            "side": side, "score": 77, "entry_allowed": True, "block_entry": False,
            "risk_mode": "RISKY", "force_risky": True, "professional_override": True,
            "reason": snap.get("reason"), "quality_adjustment": 2, "quality_cap": 79,
            "profile": setup_trade_profile("SWEEP_REVERSAL"),
            "entry_rule": "закритий 15M reclaim + HL/LH + новий 3M BOS/retest + flow",
            "stop_rule": "стоп за новою reclaim-базою, не за всім extreme капітуляції",
            "tp_rule": "TP1 до першої реальної протилежної ліквідності",
            "management_rule": "перший recovery-вхід завжди ризикований і має підтвердитися швидко",
        }
        work["setup_classifier"] = setup_info
        work["force_durable_stop_role"] = True
        work["dual_speed_mtf"] = build_dual_speed_mtf(work)
        plan = make_plan(side, work)
        if not plan or not getattr(plan, "valid", False):
            continue
        candidate = dict(base_setup)
        candidate.update({
            "action": "RISKY_ENTRY", "side": side, "quality": 77,
            "title": "РИЗИКОВАНИЙ ВХІД " + side, "reason": snap.get("reason"),
            "plan": plan, "setup_classifier": setup_info, "entry_rescue_event": event,
            "capitulation_recovery_confirmed": True, "capitulation_recovery_snapshot": snap,
            "entry_level": "RISKY_ENTRY", "entry_level_label": _entry_level_label("RISKY_ENTRY"),
            "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + list(event.get("evidence") or []))),
        })
        candidates.append((77 + min(getattr(plan, "rr1", 0), 3) * 4, candidate, work))
    if not candidates:
        return base_setup
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, candidate, work = candidates[0]
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and base_setup.get("side") != candidate.get("side"):
        return base_setup
    context["entry_rescue_event"] = candidate.get("entry_rescue_event")
    context["setup_classifier"] = candidate.get("setup_classifier")
    context["capitulation_recovery_geometry"] = work.get("capitulation_recovery_geometry")
    context["dual_speed_mtf"] = work.get("dual_speed_mtf")
    return candidate



def resolve_direction_flip_priority_lane(context, base_setup):
    base_setup = expire_recovery_thesis_if_invalid(context, base_setup)
    out = resolve_closed_15m_displacement_opportunity(context, base_setup)
    if isinstance(out, dict) and _entry_setup_type(out) == "CLOSED_15M_DIRECTION_FLIP":
        out = dict(out)
        snap = out.get("closed_15m_displacement_override") or {}
        expiry = context.get("recovery_thesis_expiry") or {}
        out["direction_flip_priority_lane"] = True
        out["transition_override_confirmed"] = True
        if expiry.get("expired") and out.get("side") == expiry.get("new_side"):
            out["recovery_thesis_expiry_priority"] = True
            out["reason"] = str(expiry.get("reason") or out.get("reason") or "")
            out["conflicts"] = [x for x in (out.get("conflicts") or []) if "recovery" not in str(x).lower()]
        if not snap.get("full_confirmed"):
            out["action"] = "RISKY_ENTRY"
            out["entry_level"] = "RISKY_ENTRY"
            out["entry_level_label"] = _entry_level_label("RISKY_ENTRY")
            out["quality"] = min(79, max(RISKY_QUALITY_MIN, int(out.get("quality", 0) or 0)))
        else:
            out["direction_flip_tier"] = "FULL"
    return out


def _opportunity_memory_ttl(setup_type):
    st = str(setup_type or "").upper()
    if st == "FRESH_BASE_CONTINUATION_REENTRY":
        return OPPORTUNITY_MEMORY_FRESH_BASE_MINUTES
    if st == "CAPITULATION_RECOVERY":
        return OPPORTUNITY_MEMORY_RECOVERY_MINUTES
    return OPPORTUNITY_MEMORY_DEFAULT_MINUTES


def _opportunity_memory_age_minutes(memory):
    try:
        dt = datetime.fromisoformat(str((memory or {}).get("created_at") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc() - dt.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return 9999.0


def _opportunity_invalidation_from_setup(setup, context):
    plan = (setup or {}).get("plan")
    if plan:
        return round_price(getattr(plan, "stop", None))
    side = (setup or {}).get("side")
    event = (setup or {}).get("entry_rescue_event") or (context or {}).get("entry_rescue_event") or {}
    return round_price(event.get("stop_level")) if side in ["LONG", "SHORT"] else None



def capture_pre_geometry_opportunity(context, side, setup_info):
    """Persist a confirmed continuation before plan construction can fail."""
    context = context or {}
    if side not in ["LONG", "SHORT"] or not isinstance(setup_info, dict):
        return
    setup_type = str(setup_info.get("type") or "").upper()
    if setup_type not in GEOMETRY_PERSISTENCE_SETUP_TYPES or not setup_info.get("entry_allowed") or setup_info.get("block_entry"):
        return
    thesis = _continuation_thesis_alignment(context, side)
    if not thesis.get("alive") or thesis.get("fast_against", 0) >= 2:
        return
    price = safe_float(context.get("price"))
    if not price:
        return
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    if state is None:
        return
    event = context.get("entry_rescue_event") or best_15m_interval_entry_event(context, side) or {}
    trigger = safe_float(event.get("trigger_level"), None)
    if trigger is None:
        trigger = price
    atr15 = safe_float(context.get("atr15"), price * 0.006) or price * 0.006
    extension = max(0.0, ((price-trigger) if side == "LONG" else (trigger-price)) / max(atr15, 1e-9))
    if extension > GEOMETRY_RECOVERY_MAX_EXTENSION_ATR15:
        return
    old = state.get("opportunity_memory") if isinstance(state.get("opportunity_memory"), dict) else None
    family = _geometry_setup_family(setup_type)
    same = bool(old and old.get("source") == "GEOMETRY_PENDING" and old.get("side") == side and old.get("setup_family") == family)
    if old and not same and old.get("source") == "PRE_GATE":
        return
    observations = int(old.get("observation_count", 0) or 0) + 1 if same else 1
    created = old.get("created_at") if same else iso_now()
    candidates = _geometry_candidate_snapshot(side, context, atr15)
    provisional_invalidation = _opportunity_invalidation_from_setup({"side": side, "setup_classifier": setup_info}, context)
    if provisional_invalidation is None and candidates.get("stops"):
        provisional_invalidation = round_price(candidates["stops"][0].get("level"))
    state["opportunity_memory"] = {
        "active": True, "source": "GEOMETRY_PENDING", "created_at": created, "updated_at": iso_now(),
        "ttl_minutes": GEOMETRY_PERSISTENCE_TTL_MINUTES, "side": side,
        "setup_type": setup_type, "setup_family": family,
        "setup_label": setup_info.get("label"), "quality": int(setup_info.get("score", 0) or 0),
        "price": round_price(price), "trigger_level": round_price(trigger),
        "trigger_ts": int(event.get("trigger_ts", 0) or 0), "trigger_type": event.get("type"),
        "retest_confirmed": bool(event.get("confirmed") and event.get("anchor_confirmed")),
        "invalidation": provisional_invalidation,
        "initial_reason": setup_info.get("reason"), "missing_condition": "VALID_TECHNICAL_GEOMETRY",
        "blocking_gate": "PLAN_GEOMETRY", "blocked_gates": ["PLAN_GEOMETRY"],
        "observation_count": observations,
        "geometry_fail_count": int(old.get("geometry_fail_count", 0) or 0) if same else 0,
        "geometry_candidates": candidates, "plan": old.get("plan") if same else None,
        "thesis": thesis,
    }
    # Geometry may be missing while an exhaustion/shock lock is still active.
    # Preserve the professional thesis in the longer lock-release bridge too.
    capture_lock_release_bridge_from_setup_info(context, side, setup_info, source="GEOMETRY_PENDING")


def mark_geometry_opportunity_failure(context, side, setup_info, plan):
    if plan and getattr(plan, "valid", False):
        return
    capture_pre_geometry_opportunity(context, side, setup_info)
    state = (context or {}).get("runtime_state") if isinstance((context or {}).get("runtime_state"), dict) else None
    memory = state.get("opportunity_memory") if state and isinstance(state.get("opportunity_memory"), dict) else None
    if not memory or memory.get("source") != "GEOMETRY_PENDING" or memory.get("side") != side:
        return
    memory["geometry_fail_count"] = int(memory.get("geometry_fail_count", 0) or 0) + 1
    memory["last_geometry_failure_at"] = iso_now()
    memory["last_geometry_reason"] = str(getattr(plan, "validation_reason", "") or "не вдалося побудувати життєздатну технічну геометрію")
    memory["missing_condition"] = "VALID_TECHNICAL_GEOMETRY"
    memory["updated_at"] = iso_now()
    state["opportunity_memory"] = memory


def resolve_geometry_persistence_entry(context, base_setup, memory):
    """Rebuild the same continuation instead of forgetting it next run."""
    side = memory.get("side")
    setup_type = str(memory.get("setup_type") or "").upper()
    if side not in ["LONG", "SHORT"] or setup_type not in GEOMETRY_PERSISTENCE_SETUP_TYPES:
        return base_setup
    thesis = _continuation_thesis_alignment(context, side)
    if not thesis.get("alive") or thesis.get("fast_against", 0) >= 2:
        return base_setup
    price = safe_float((context or {}).get("price"))
    atr15 = safe_float((context or {}).get("atr15"), (price or 90) * 0.006) or (price or 90) * 0.006
    origin = safe_float(memory.get("trigger_level"), safe_float(memory.get("price"), price))
    extension = max(0.0, ((price-origin) if side == "LONG" else (origin-price)) / max(atr15, 1e-9)) if price and origin else 0.0
    if extension > GEOMETRY_RECOVERY_MAX_EXTENSION_ATR15:
        return base_setup
    late, _ = is_late_chase(side, context)
    if late and extension > 0.45:
        return base_setup
    work = dict(context)
    work["bias"] = side
    work["geometry_persistence_memory"] = memory
    work["force_geometry_recovery"] = int(memory.get("geometry_fail_count", 0) or 0) >= GEOMETRY_ESCALATION_FAILURES
    work["force_durable_stop_role"] = True
    setup_info = _memory_setup_classifier(memory, side)
    work["setup_classifier"] = setup_info
    event = best_15m_interval_entry_event(context, side)
    if event:
        work["entry_rescue_event"] = event
    plan = make_plan(side, work)
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else None
    if not plan or not getattr(plan, "valid", False):
        if state is not None:
            memory["geometry_fail_count"] = int(memory.get("geometry_fail_count", 0) or 0) + 1
            memory["updated_at"] = iso_now()
            memory["last_geometry_reason"] = str(getattr(plan, "validation_reason", "") or "геометрія ще не готова")
            state["opportunity_memory"] = memory
        return base_setup
    quality = int(max(RISKY_QUALITY_MIN, min(79, max(int(memory.get("quality", 0) or 0), 68))))
    out = dict(base_setup)
    out.update({
        "action": "RISKY_ENTRY", "side": side, "quality": quality,
        "title": "РИЗИКОВАНИЙ ВХІД " + side + " — ПРОПУЩЕНЕ ПРОДОВЖЕННЯ ВІДНОВЛЕНО",
        "reason": "попередній continuation збережено; технічну 15M/ICT геометрію відновлено до того, як ціна втекла",
        "plan": plan, "setup_classifier": setup_info, "entry_rescue_event": event,
        "geometry_persistence_activated": True,
        "geometry_fail_count": int(memory.get("geometry_fail_count", 0) or 0),
        "missed_continuation_recovery": True,
        "entry_level": "RISKY_ENTRY", "entry_level_label": _entry_level_label("RISKY_ENTRY"),
        "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + ["15M/1H continuation залишається чинним", "реальний 15M/ICT стоп знайдено", "TP1 — найближча реальна ліквідність"])),
    })
    return out


def should_store_opportunity_memory(setup, context):
    if not isinstance(setup, dict) or setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return False
    side = setup.get("side"); st = _entry_setup_type(setup)
    if side not in ["LONG", "SHORT"] or st not in OPPORTUNITY_MEMORY_SETUP_TYPES:
        return False
    quality = int(setup.get("quality", 0) or 0)
    if quality < 57 or setup.get("hard_no_chase") or setup.get("exhausted_move"):
        return False
    # Do not persist a genuinely adverse opportunity. Neutral/missing fast data
    # is exactly what memory is designed to wait for.
    fast = _fast_layer_state(context, side)
    if fast.get("against", 0) >= 2:
        return False
    reason = str(setup.get("reason") or "")
    one_condition_wait = any(k in reason.lower() for k in ["потріб", "чекат", "flow", "cvd", "ретест", "підтвердж", "геометр", "стоп", "tp"])
    return bool(one_condition_wait or setup.get("entry_level") == "WATCH_TRIGGER")



def update_opportunity_memory(state, setup, context):
    if not isinstance(state, dict):
        return
    old = state.get("opportunity_memory") if isinstance(state.get("opportunity_memory"), dict) else None
    if old and _opportunity_memory_age_minutes(old) > int(old.get("ttl_minutes", OPPORTUNITY_MEMORY_DEFAULT_MINUTES) or OPPORTUNITY_MEMORY_DEFAULT_MINUTES):
        state["opportunity_memory"] = None; old = None
    if isinstance(setup, dict) and setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        state["opportunity_memory"] = None
        return
    # A pre-gate candidate is more informative than the final WATCH result.
    # Keep its original setup, trigger, invalidation and geometry while updating
    # the last blocking reason.
    if old and old.get("source") in {"PRE_GATE", "GEOMETRY_PENDING"}:
        if isinstance(setup, dict):
            side = setup.get("side")
            if side in ["LONG", "SHORT"] and side != old.get("side") and int(setup.get("quality", 0) or 0) >= 62:
                state["opportunity_memory"] = None
                return
            if side == old.get("side"):
                old["updated_at"] = iso_now()
                old["last_final_reason"] = setup.get("reason")
                old["last_final_action"] = setup.get("action")
                state["opportunity_memory"] = old
        return
    if should_store_opportunity_memory(setup, context):
        st = _entry_setup_type(setup); side = setup.get("side")
        plan = setup.get("plan")
        state["opportunity_memory"] = {
            "active": True, "source": "POST_GATE", "created_at": iso_now(), "ttl_minutes": _opportunity_memory_ttl(st),
            "side": side, "setup_type": st, "setup_label": ((setup.get("setup_classifier") or {}).get("label")),
            "quality": int(setup.get("quality", 0) or 0), "price": round_price((context or {}).get("price")),
            "trigger_level": round_price(((setup.get("entry_rescue_event") or {}).get("trigger_level"))),
            "invalidation": _opportunity_invalidation_from_setup(setup, context),
            "reason": setup.get("reason"), "missing_conditions": list(setup.get("conflicts") or [])[:4],
            "plan": asdict(plan) if plan else None,
        }
        return
    if old and isinstance(setup, dict):
        side = setup.get("side")
        if side in ["LONG", "SHORT"] and side != old.get("side") and int(setup.get("quality", 0) or 0) >= 62:
            state["opportunity_memory"] = None


def _memory_setup_classifier(memory, side):
    st = str((memory or {}).get("setup_type") or "TREND_CONTINUATION")
    profile_type = "TREND_IGNITION_ENTRY" if st == "CLOSED_15M_DIRECTION_FLIP" else ("SWEEP_REVERSAL" if st == "CAPITULATION_RECOVERY" else st)
    return {
        "type": st, "label": (memory or {}).get("setup_label") or _setup_label(st), "side": side,
        "score": int((memory or {}).get("quality", 65) or 65), "entry_allowed": True, "block_entry": False,
        "risk_mode": "RISKY", "force_risky": True, "professional_override": True,
        "reason": "професійний сценарій збережено між 15-хвилинними запусками; відсутня умова тепер підтверджена",
        "quality_adjustment": 0, "quality_cap": 79, "profile": setup_trade_profile(profile_type),
    }



def activate_opportunity_memory_if_ready(context, base_setup):
    if not isinstance(context, dict) or not isinstance(base_setup, dict) or base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return base_setup
    state = context.get("runtime_state") if isinstance(context.get("runtime_state"), dict) else {}
    memory = state.get("opportunity_memory") if isinstance(state.get("opportunity_memory"), dict) else context.get("opportunity_memory")
    if not isinstance(memory, dict) or not memory.get("active"):
        return base_setup
    if str(memory.get("setup_type") or "").upper() == "CAPITULATION_RECOVERY":
        expiry = recovery_thesis_expiry_snapshot(context, memory.get("side"))
        if expiry.get("expired"):
            state["opportunity_memory"] = None
            context["recovery_thesis_expiry"] = expiry
            return base_setup
    age = _opportunity_memory_age_minutes(memory)
    if age > int(memory.get("ttl_minutes", OPPORTUNITY_MEMORY_DEFAULT_MINUTES) or OPPORTUNITY_MEMORY_DEFAULT_MINUTES):
        state["opportunity_memory"] = None
        return base_setup
    side = memory.get("side"); st = str(memory.get("setup_type") or "")
    price = safe_float(context.get("price")); atr15 = safe_float(context.get("atr15"), (price or 90) * 0.006) or (price or 90) * 0.006
    if side not in ["LONG", "SHORT"] or not price:
        return base_setup
    invalidation = safe_float(memory.get("invalidation"), None)
    if invalidation is not None and ((side == "LONG" and price <= invalidation) or (side == "SHORT" and price >= invalidation)):
        state["opportunity_memory"] = None
        return base_setup
    origin = safe_float(memory.get("trigger_level"), None)
    if origin is None:
        origin = safe_float(memory.get("price"), price) or price
    extension = max(0.0, ((price - origin) if side == "LONG" else (origin - price)) / max(atr15, 1e-9))
    if extension > OPPORTUNITY_MEMORY_MAX_EXTENSION_ATR15:
        return base_setup

    if str(memory.get("source") or "").upper() == "GEOMETRY_PENDING":
        return resolve_geometry_persistence_entry(context, base_setup, memory)

    fast = _professional_fast_layer_permission(context, side, strong_15m=True)
    if not fast.get("risky_ok") or fast.get("against", 0) >= 2:
        return base_setup
    snap = None
    if st == "CLOSED_15M_DIRECTION_FLIP":
        snap = closed_15m_displacement_snapshot(context, side); ready = snap.get("confirmed")
    elif st == "FRESH_BASE_CONTINUATION_REENTRY":
        standard = fresh_base_continuation_snapshot(context, side)
        reset = fresh_base_exhaustion_reset_snapshot(context, side)
        snap = standard if standard.get("allowed") else reset
        ready = bool(standard.get("allowed") or _fresh_base_valid(reset))
    elif st == "CAPITULATION_RECOVERY":
        snap = capitulation_recovery_snapshot(context, side); ready = snap.get("active") and snap.get("allowed")
    elif st == "RANGE_COMPRESSION_BREAKOUT":
        snap = fresh_base_exhaustion_reset_snapshot(context, side)
        ready = _fresh_base_valid(snap) or bool(rolling_balance_detector(context).get("breakout_accepted") and rolling_balance_detector(context).get("accepted_side") == side)
    else:
        event = best_15m_interval_entry_event(context, side)
        structure = context.get("structure") or {}; tf15 = context.get("tf15") or {}
        ready = bool(event and event.get("confirmed") and event.get("anchor_confirmed") and (structure.get("bias") == side or tf15.get("bias") == side))
        snap = event
    if not ready:
        return base_setup

    work = dict(context); work["bias"] = side
    event = best_15m_interval_entry_event(context, side)
    if event: work["entry_rescue_event"] = event
    setup_info = _memory_setup_classifier(memory, side)
    work["setup_classifier"] = setup_info; work["force_durable_stop_role"] = True
    plan = make_plan(side, work)
    if not plan or not getattr(plan, "valid", False):
        return base_setup
    quality = int(max(RISKY_QUALITY_MIN, min(79, max(int(memory.get("quality", 0) or 0), 66))))
    out = dict(base_setup)
    out.update({
        "action": "RISKY_ENTRY", "side": side, "quality": quality,
        "title": "РИЗИКОВАНИЙ ВХІД " + side + " — ЗБЕРЕЖЕНА МОЖЛИВІСТЬ ПІДТВЕРДЖЕНА",
        "reason": "збережений pre-gate сценарій підтверджено; інвалідація не пробита, два adverse-шари відсутні, ціна не втекла",
        "plan": plan, "setup_classifier": setup_info, "entry_rescue_event": event,
        "opportunity_memory_activated": True, "opportunity_memory_source": memory.get("source"),
        "opportunity_memory_blocking_gate": memory.get("blocking_gate"),
        "opportunity_memory_age_min": round(age, 1),
        "entry_level": "RISKY_ENTRY", "entry_level_label": _entry_level_label("RISKY_ENTRY"),
        "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + ["професійний сценарій збережено до hard gate", "відсутня умова підтверджена", "інвалідація не пробита"])),
    })
    if (
        st in {"FRESH_BASE_CONTINUATION_REENTRY", "RANGE_COMPRESSION_BREAKOUT"}
        and isinstance(snap, dict)
        and snap.get("base_candles")
        and _exhaustion_reset_allowed(snap)
    ):
        out["fresh_base_exhaustion_reset_snapshot"] = snap
        out["fresh_base_exhaustion_reset"] = True
        context["fresh_base_exhaustion_reset"] = snap
    return out


def _bridge_iso_age_minutes(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_utc() - dt.astimezone(timezone.utc)).total_seconds() / 60.0)
    except Exception:
        return 99999.0


def _lock_release_bridge_store(context):
    state = (context or {}).get("runtime_state") if isinstance((context or {}).get("runtime_state"), dict) else None
    if state is None:
        return None, {}
    store = state.get("lock_release_opportunities")
    if not isinstance(store, dict):
        store = {}
        state["lock_release_opportunities"] = store
    return state, store


def _bridge_classifier_is_professional(info):
    if not isinstance(info, dict):
        return False
    st = str(info.get("type") or "").upper()
    return bool(
        st in LOCK_RELEASE_BRIDGE_SETUP_TYPES
        and info.get("entry_allowed")
        and not info.get("block_entry")
        and int(info.get("score", 0) or 0) >= LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE
    )


def _bridge_memory_from_candidate(context, candidate, source, gate_name=""):
    if not isinstance(candidate, dict):
        return None
    side = candidate.get("side")
    info = candidate.get("setup_classifier") or ((context or {}).get("setup_classifier") or {})
    if side not in ["LONG", "SHORT"] or not _bridge_classifier_is_professional(info):
        return None
    quality = int(candidate.get("quality", 0) or 0)
    if quality < LOCK_RELEASE_BRIDGE_MIN_QUALITY and int(info.get("score", 0) or 0) < LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE + 8:
        return None
    fast = _fast_layer_state(context, side)
    if int(fast.get("against", 0) or 0) > LOCK_RELEASE_BRIDGE_MAX_ADVERSE_LAYERS:
        return None
    price = safe_float((context or {}).get("price"), None)
    event = candidate.get("entry_rescue_event") or (context or {}).get("entry_rescue_event") or {}
    trigger = safe_float(event.get("trigger_level"), None)
    if trigger is None:
        trigger = safe_float(candidate.get("price"), price)
    plan = candidate.get("plan")
    invalidation = _opportunity_invalidation_from_setup(candidate, context)
    return {
        "active": True,
        "status": "ARMED",
        "source": source,
        "gate_name": gate_name,
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "ttl_minutes": LOCK_RELEASE_BRIDGE_TTL_MINUTES,
        "side": side,
        "setup_type": str(info.get("type") or "").upper(),
        "setup_label": info.get("label") or _setup_label(info.get("type")),
        "classifier": make_json_safe(info),
        "quality": max(quality, int(info.get("score", 0) or 0)),
        "origin_price": round_price(price),
        "trigger_level": round_price(trigger),
        "trigger_ts": int(event.get("trigger_ts", 0) or 0),
        "trigger_type": event.get("type"),
        "invalidation": round_price(invalidation),
        "initial_reason": candidate.get("reason") or info.get("reason"),
        "plan": asdict(plan) if plan and hasattr(plan, "entry") else (make_json_safe(plan) if isinstance(plan, dict) else None),
    }


def _save_lock_release_bridge_candidate(context, memory):
    if not isinstance(memory, dict) or memory.get("side") not in ["LONG", "SHORT"]:
        return
    state, store = _lock_release_bridge_store(context)
    if state is None:
        return
    side = memory["side"]
    old = store.get(side) if isinstance(store.get(side), dict) else None
    same = bool(old and old.get("setup_type") == memory.get("setup_type"))
    if old and not same:
        old_score = int(old.get("quality", 0) or 0)
        new_score = int(memory.get("quality", 0) or 0)
        # Do not replace a stronger recent professional thesis with a weaker one.
        if _bridge_iso_age_minutes(old.get("created_at")) <= LOCK_RELEASE_BRIDGE_TTL_MINUTES and old_score > new_score + 4:
            return
    if same:
        memory["created_at"] = old.get("created_at") or memory.get("created_at")
        memory["release_seen_at"] = old.get("release_seen_at") or ""
        memory["release_reason"] = old.get("release_reason") or ""
        memory["activation_count"] = int(old.get("activation_count", 0) or 0)
    store[side] = memory
    state["lock_release_opportunities"] = store


def capture_lock_release_bridge_candidate(context, candidate, blocked, gate_name):
    """Capture the professional setup before a lock/risk gate converts it to WATCH."""
    if not isinstance(candidate, dict) or candidate.get("action") not in ["ENTRY", "RISKY_ENTRY"]:
        return
    if not isinstance(blocked, dict) or blocked.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return
    side = candidate.get("side")
    if side not in ["LONG", "SHORT"]:
        return
    snap = ((blocked.get("entry_risk_package") or {}).get("persistent_exhaustion") or blocked.get("persistent_exhaustion_lock") or {})
    reason = " ".join([str(blocked.get("reason") or ""), *[str(x) for x in (blocked.get("conflicts") or [])]]).lower()
    lock_related = bool(
        snap.get("active")
        or blocked.get("same_side_exhaustion_lock")
        or "persistent exhaustion" in reason
        or "impulse lock" in reason
        or "post-rejection" in reason
        or "геометр" in reason
        or gate_name in {"ENTRY_RISK_PACKAGE_GATE", "FINAL_SAME_SIDE_EXHAUSTION_LOCK", "FINAL_STOP_ROLE_REBUILD"}
    )
    if not lock_related:
        return
    memory = _bridge_memory_from_candidate(context, candidate, source="PRE_LOCK_GATE", gate_name=gate_name)
    if memory:
        _save_lock_release_bridge_candidate(context, memory)


def capture_lock_release_bridge_from_setup_info(context, side, setup_info, source="SETUP_INFO"):
    if side not in ["LONG", "SHORT"] or not _bridge_classifier_is_professional(setup_info):
        return
    candidate = {
        "action": "RISKY_ENTRY",
        "side": side,
        "quality": int(setup_info.get("score", 0) or 0),
        "reason": setup_info.get("reason"),
        "setup_classifier": setup_info,
        "entry_rescue_event": (context or {}).get("entry_rescue_event") or {},
    }
    memory = _bridge_memory_from_candidate(context, candidate, source=source)
    if memory:
        _save_lock_release_bridge_candidate(context, memory)


def _backfill_lock_release_bridge_from_history(context):
    """Migration path for states created before this package was installed."""
    state, store = _lock_release_bridge_store(context)
    if state is None:
        return
    history = list(state.get("history") or [])
    for item in reversed(history[-40:]):
        if not isinstance(item, dict):
            continue
        age = _bridge_iso_age_minutes(item.get("time"))
        if age > LOCK_RELEASE_BRIDGE_TTL_MINUTES:
            continue
        side = item.get("side")
        info = item.get("setup_classifier") or {}
        if side not in ["LONG", "SHORT"] or side in store or not _bridge_classifier_is_professional(info):
            continue
        quality = int(item.get("quality", 0) or 0)
        if quality < LOCK_RELEASE_BRIDGE_MIN_QUALITY and int(info.get("score", 0) or 0) < LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE + 8:
            continue
        reason = str(item.get("reason") or "").lower()
        if not any(key in reason for key in ["геометр", "post-rejection", "retest", "bos", "reclaim", "lock", "підтвердж"]):
            continue
        memory = {
            "active": True, "status": "ARMED", "source": "HISTORY_BACKFILL",
            "created_at": item.get("time") or iso_now(), "updated_at": iso_now(),
            "ttl_minutes": LOCK_RELEASE_BRIDGE_TTL_MINUTES, "side": side,
            "setup_type": str(info.get("type") or "").upper(),
            "setup_label": info.get("label") or _setup_label(info.get("type")),
            "classifier": make_json_safe(info),
            "quality": max(quality, int(info.get("score", 0) or 0)),
            "origin_price": round_price(item.get("price")), "trigger_level": round_price(item.get("price")),
            "trigger_ts": 0, "trigger_type": "HISTORY_BACKFILL", "invalidation": None,
            "initial_reason": item.get("reason"), "plan": None,
        }
        store[side] = memory
        state["lock_release_opportunities"] = store
        break


def update_lock_release_opportunity_bridge_memory(state, setup, context):
    """Maintain bridge memory after each completed evaluation."""
    if not isinstance(state, dict):
        return
    store = state.get("lock_release_opportunities")
    if not isinstance(store, dict):
        store = {}
        state["lock_release_opportunities"] = store
    for side in list(store):
        memory = store.get(side) or {}
        if _bridge_iso_age_minutes(memory.get("created_at")) > int(memory.get("ttl_minutes", LOCK_RELEASE_BRIDGE_TTL_MINUTES) or LOCK_RELEASE_BRIDGE_TTL_MINUTES):
            store.pop(side, None)
    if isinstance(setup, dict) and setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        store.pop(setup.get("side"), None)
        state["lock_release_opportunities"] = store
        return
    if isinstance(setup, dict) and setup.get("side") in ["LONG", "SHORT"]:
        info = setup.get("setup_classifier") or {}
        if _bridge_classifier_is_professional(info):
            reason = str(setup.get("reason") or "").lower()
            lock = ((state.get("persistent_exhaustion_locks") or {}).get(setup.get("side")) or {})
            shock = state.get("shock_lock") or {}
            relevant = bool(
                lock.get("status") in {"LOCKED", "RELEASED"}
                or (shock.get("side") == setup.get("side") and shock.get("status") in {"LOCKED", "RELEASED"})
                or any(k in reason for k in ["геометр", "post-rejection", "retest", "bos", "reclaim", "lock"])
            )
            if relevant:
                memory = _bridge_memory_from_candidate(context, setup, source="POST_EVALUATION")
                if memory:
                    _save_lock_release_bridge_candidate(context, memory)
    state["lock_release_opportunities"] = store


def _bridge_trigger_snapshot(context, side):
    event = best_15m_interval_entry_event(context, side) or {}
    latest_ts = _latest_context_ts(context)
    event_ts = int(event.get("trigger_ts", 0) or 0)
    event_age = max(0.0, (latest_ts - event_ts) / 60000.0) if latest_ts and event_ts else 9999.0
    event_ok = bool(
        event
        and event.get("confirmed")
        and event.get("anchor_confirmed")
        and event_age <= LOCK_RELEASE_BRIDGE_EVENT_MAX_AGE_MINUTES
        and event.get("type") in {"SWEEP_RECLAIM", "BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"}
        and (event.get("follow_through") or event.get("post_confirmed") or int(event.get("score", 0) or 0) >= 72)
    )
    structure = (context or {}).get("structure") or {}
    phase = str(structure.get("phase") or "").upper()
    tf3 = (context or {}).get("tf3") or {}
    bos = bool(
        structure.get("bias") == side
        and ((side == "LONG" and ("BOS LONG" in phase or "CHOCH LONG" in phase))
             or (side == "SHORT" and ("BOS SHORT" in phase or "CHOCH SHORT" in phase)))
    )
    tf3_ok = bool(tf3.get("bias") == side and abs(int(tf3.get("score", 0) or 0)) >= LOCK_RELEASE_BRIDGE_MIN_3M_SCORE)
    evidence = _fresh_professional_entry_evidence(context, side)
    hold_ok = bool(tf3_ok and (evidence.get("fresh_event") or evidence.get("micro_base_breakout") or evidence.get("fresh_base")))
    ready = bool(event_ok or (bos and tf3_ok) or hold_ok)
    return {
        "ready": ready, "event": event if event_ok else {}, "event_ok": event_ok,
        "event_age_min": round(event_age, 1) if event_age < 9999 else None,
        "bos_or_choch": bos, "tf3_hold": tf3_ok, "fresh_hold": hold_ok,
        "reason": ("свіжий 3M hold/BOS/retest підтверджено" if ready else "потрібен свіжий 3M hold, BOS/CHOCH або breakout-retest після зняття lock"),
    }


def _bridge_revalidated_classifier(memory, context, trigger):
    original = dict(memory.get("classifier") or {})
    side = memory.get("side")
    event_type = str(((trigger or {}).get("event") or {}).get("type") or "").upper()
    tf1 = (context or {}).get("tf1h") or {}
    tf4 = (context or {}).get("tf4h") or {}
    both_htf_against = bool(tf1.get("bias") == opposite(side) and tf4.get("bias") == opposite(side))
    if both_htf_against:
        st = "COUNTERTREND_SCALP"
    elif event_type == "SWEEP_RECLAIM":
        st = "SWEEP_RECLAIM_EARLY_ENTRY"
    elif event_type in {"BREAKOUT_RETEST", "ICT_ZONE_RECLAIM"}:
        st = "PULLBACK_CONTINUATION_FAST_ENTRY"
    else:
        st = "TREND_IGNITION_ENTRY"
    original.update({
        "type": st, "label": _setup_label(st), "side": side,
        "score": max(LOCK_RELEASE_BRIDGE_MIN_CLASSIFIER_SCORE, int(memory.get("quality", 0) or 0)),
        "entry_allowed": True, "block_entry": False, "risk_mode": "RISKY",
        "force_risky": True, "professional_override": True,
        "reason": "збережений сетап переоцінено після зняття lock; свіжий 3M/BOS/retest підтвердив нову точку входу",
        "quality_adjustment": 0, "quality_cap": 79, "profile": setup_trade_profile(st),
    })
    return original


def _bridge_conflict_mentions_side(text, side):
    value = str(text or "").upper()
    return side in ["LONG", "SHORT"] and side in value


def _bridge_clean_conflicts(base_setup, context, final_side, lock_released=True):
    """Rebuild WATCH conflicts for the final Bridge side.

    Opposite-side failed trades never contaminate the restored candidate. There
    is no timed cooldown or automatic quality deduction. The only re-entry
    warning retained is an event-driven exact replay of the same broken thesis.
    """
    base_setup = base_setup or {}
    context = context or {}
    base_side = base_setup.get("side")
    side_changed = bool(base_side in ["LONG", "SHORT"] and base_side != final_side)
    raw = [] if side_changed else list(base_setup.get("conflicts") or [])
    cleaned = []
    stale_fragments = [
        "persistent exhaustion/impulse lock",
        "локація нежиттєздатна до побудови rr: persistent",
        "не вдалося побудувати життєздатну технічну геометрію",
        "немає одного з топових сетапів",
        "штраф якості",
        "тимчасово знижена",
        "ліміт повторних входів",
    ]
    review_context = dict(context)
    classifier = dict((base_setup.get("setup_classifier") or context.get("setup_classifier") or {}))
    classifier["side"] = final_side
    review_context["setup_classifier"] = classifier
    review = analyze_reentry_quality_review(context.get("runtime_state") or {}, review_context)
    context["reentry_review"] = review
    context["reentry_cooldown"] = review
    review_side = review.get("side") if review.get("active") else None

    for item in raw:
        value = str(item or "").strip()
        lower = value.lower()
        if not value:
            continue
        if lock_released and any(fragment in lower for fragment in stale_fragments):
            continue
        if "після слабкого виходу" in lower or "попередній" in lower and "stop" in lower:
            if review_side != final_side:
                continue
        if value not in cleaned:
            cleaned.append(value)

    if review_side == final_side and review.get("block_reentry"):
        msg = review.get("reason") or "повторюється та сама зламана теза без нового BOS/retest/ICT-перезбору"
        if msg not in cleaned:
            cleaned.append(msg)
    return cleaned


def _bridge_clean_confirmations(base_setup, final_side):
    base_setup = base_setup or {}
    base_side = base_setup.get("side")
    if base_side in ["LONG", "SHORT"] and base_side != final_side:
        return []
    return list(base_setup.get("confirmations") or [])


def _bridge_watch_setup(base_setup, memory, reason, quality=None, trigger=None, context=None):
    out = dict(base_setup or {})
    side = memory.get("side")
    saved_score = int(memory.get("quality", 0) or 0)
    saved_label = memory.get("setup_label") or _setup_label(memory.get("setup_type"))
    readiness = int(max(55, min(67, quality if quality is not None else max(58, saved_score - 12))))
    info = dict(memory.get("classifier") or {})
    info.update({
        "side": side, "entry_allowed": True, "block_entry": False,
        "risk_mode": "RISKY", "force_risky": True,
        "reason": reason,
        # Keep the preserved classifier score for the engine/audit, while the
        # public notification labels it explicitly as SAVED, not current.
        "bridge_saved_score": saved_score,
        "bridge_current_readiness": readiness,
    })
    out.update({
        "action": "WATCH", "side": side,
        "quality": readiness,
        "title": f"ЧЕКАТИ — {side} ПІСЛЯ ЗНЯТТЯ LOCK",
        "reason": reason, "setup_classifier": info,
        "entry_level": "WATCH_TRIGGER", "entry_level_label": _entry_level_label("WATCH_TRIGGER"),
        "lock_release_bridge": True, "lock_release_bridge_activated": False,
        "lock_release_bridge_status": "WAITING_TRIGGER",
        "lock_release_bridge_saved_setup_score": saved_score,
        "lock_release_bridge_saved_setup_label": saved_label,
        "lock_release_bridge_current_readiness": readiness,
        "show_wait_plan": False,
        # The old plan belonged to the pre-release/old-side evaluation. A new
        # plan is built only after the fresh 3M/BOS/retest trigger.
        "plan": None,
        "plan_rebuild_required": True,
        "plan_rebuild_status": "WAITING_FRESH_TRIGGER",
        "entry_risk_package": None,
        "persistent_exhaustion_lock": None,
        "same_side_exhaustion_lock": False,
    })
    if trigger:
        out["lock_release_bridge_trigger"] = trigger
    out["confirmations"] = _bridge_clean_confirmations(base_setup, side)
    out["conflicts"] = _bridge_clean_conflicts(base_setup, context, side, lock_released=True)
    if reason not in out["conflicts"]:
        out["conflicts"].append(reason)
    return out


def activate_lock_release_opportunity_bridge(context, base_setup, finalize_watch_only=False):
    """Reconnect a preserved professional setup after its lock is released.

    Release alone produces WATCH. Entry is possible only after a fresh 3M
    hold/BOS/retest, no more than one adverse fast layer, no chase extension,
    and a newly rebuilt plan with RR1 >= 1.20.
    """
    if not isinstance(context, dict) or not isinstance(base_setup, dict):
        return base_setup
    if base_setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        return base_setup
    _backfill_lock_release_bridge_from_history(context)
    state, store = _lock_release_bridge_store(context)
    if state is None or not store:
        return base_setup
    candidates = []
    for side, memory in list(store.items()):
        if not isinstance(memory, dict) or not memory.get("active"):
            continue
        age = _bridge_iso_age_minutes(memory.get("created_at"))
        if age > int(memory.get("ttl_minutes", LOCK_RELEASE_BRIDGE_TTL_MINUTES) or LOCK_RELEASE_BRIDGE_TTL_MINUTES):
            store.pop(side, None)
            continue
        lock = ((state.get("persistent_exhaustion_locks") or {}).get(side) or {})
        shock = state.get("shock_lock") or {}
        released = bool(lock.get("status") == "RELEASED" or (shock.get("side") == side and shock.get("status") == "RELEASED"))
        if not released:
            continue
        release_at = lock.get("released_at") or (shock.get("released_at") if shock.get("side") == side else "")
        release_age = _bridge_iso_age_minutes(release_at)
        if release_age > LOCK_RELEASE_BRIDGE_RELEASE_GRACE_MINUTES:
            store.pop(side, None)
            continue
        candidates.append((age, side, memory, lock, shock, release_age))
    if not candidates:
        state["lock_release_opportunities"] = store
        return base_setup
    candidates.sort(key=lambda x: (x[0], -int(x[2].get("quality", 0) or 0)))
    age, side, memory, lock, shock, release_age = candidates[0]
    if base_setup.get("side") in ["LONG", "SHORT"] and base_setup.get("side") != side and int(base_setup.get("quality", 0) or 0) >= 62:
        store.pop(side, None)
        state["lock_release_opportunities"] = store
        return base_setup
    price = safe_float(context.get("price"), None)
    atr15 = safe_float(context.get("atr15"), None) or safe_float((context.get("tf15") or {}).get("atr"), None) or ((price or 90) * 0.006)
    invalidation = safe_float(memory.get("invalidation"), None)
    if price is None:
        return base_setup
    if invalidation is not None and ((side == "LONG" and price <= invalidation) or (side == "SHORT" and price >= invalidation)):
        store.pop(side, None)
        state["lock_release_opportunities"] = store
        return base_setup
    origin = safe_float(memory.get("trigger_level"), None) or safe_float(memory.get("origin_price"), price) or price
    extension = max(0.0, ((price - origin) if side == "LONG" else (origin - price)) / max(atr15, 1e-9))
    if extension > LOCK_RELEASE_BRIDGE_MAX_EXTENSION_ATR15:
        reason = f"lock знято, але ціна вже втекла на {round(extension, 2)} ATR15 — не доганяти; чекати нову базу/ретест"
        memory.update({"status": "EXPIRED_EXTENSION", "updated_at": iso_now(), "last_reason": reason})
        store[side] = memory
        state["lock_release_opportunities"] = store
        return _bridge_watch_setup(base_setup, memory, reason, quality=55, context=context)
    fast = _fast_layer_state(context, side)
    against = int(fast.get("against", 0) or 0)
    trigger = _bridge_trigger_snapshot(context, side)
    release_reason = lock.get("release_reason") or ("shock lock released" if shock.get("status") == "RELEASED" else "lock released")
    memory.update({
        "status": "WAITING_TRIGGER", "updated_at": iso_now(),
        "release_seen_at": memory.get("release_seen_at") or iso_now(),
        "release_reason": release_reason, "release_age_min": round(release_age, 1),
        "current_extension_atr15": round(extension, 3),
    })
    store[side] = memory
    state["lock_release_opportunities"] = store
    if against > LOCK_RELEASE_BRIDGE_MAX_ADVERSE_LAYERS:
        return _bridge_watch_setup(base_setup, memory, "lock знято, але два швидкі шари ще проти збереженого сетапу", trigger=trigger, context=context)
    if not trigger.get("ready"):
        return _bridge_watch_setup(base_setup, memory, f"lock знято; збережений {side} сценарій ще актуальний, але {trigger.get('reason')}", trigger=trigger, context=context)
    if finalize_watch_only:
        # This final pass runs after every hard gate. It may clean and restore a
        # Bridge WATCH, but it must never reopen an entry that a later consensus
        # gate rejected. The next run will rebuild the plan from fresh data.
        final_reason = "lock знято і свіжий тригер уже є, але фінальний консенсус ще не дозволив вхід; план буде перебудовано на наступній перевірці"
        return _bridge_watch_setup(base_setup, memory, final_reason, trigger=trigger, context=context)
    work = dict(context)
    work["bias"] = side
    setup_info = _bridge_revalidated_classifier(memory, context, trigger)
    work["setup_classifier"] = setup_info
    if trigger.get("event"):
        work["entry_rescue_event"] = trigger.get("event")
    work["force_durable_stop_role"] = True
    plan = make_plan(side, work)
    if not plan or not getattr(plan, "valid", False):
        plan_reason = str(getattr(plan, "validation_reason", "") or "після зняття lock геометрія ще не життєздатна")
        return _bridge_watch_setup(base_setup, memory, plan_reason, trigger=trigger, context=context)
    if safe_float(getattr(plan, "rr1", 0), 0.0) < LOCK_RELEASE_BRIDGE_MIN_RR1:
        return _bridge_watch_setup(base_setup, memory, f"свіжий тригер є, але RR1 {round(safe_float(getattr(plan, 'rr1', 0), 0.0), 2)} нижче мінімуму {LOCK_RELEASE_BRIDGE_MIN_RR1}", trigger=trigger, context=context)
    quality = int(max(RISKY_QUALITY_MIN, min(79, max(64, int(memory.get("quality", 0) or 0)))))
    out = dict(base_setup)
    out.update({
        "action": "RISKY_ENTRY", "side": side, "quality": quality,
        "title": f"РИЗИКОВАНИЙ ВХІД {side} — LOCK RELEASE BRIDGE ПІДТВЕРДЖЕНО",
        "reason": "після зняття lock збережений сценарій отримав новий 3M hold/BOS/retest і життєздатну геометрію",
        "plan": plan, "setup_classifier": setup_info,
        "entry_rescue_event": trigger.get("event") or {},
        "lock_release_bridge": True, "lock_release_bridge_activated": True,
        "lock_release_bridge_status": "CONFIRMED_ENTRY",
        "lock_release_bridge_age_min": round(age, 1),
        "lock_release_bridge_release_age_min": round(release_age, 1),
        "entry_level": "RISKY_ENTRY", "entry_level_label": _entry_level_label("RISKY_ENTRY"),
        "confirmations": list(dict.fromkeys((base_setup.get("confirmations") or []) + [
            "persistent/shock lock знято професійним reset",
            trigger.get("reason"),
            f"нова геометрія RR1 {round(safe_float(getattr(plan, 'rr1', 0), 0.0), 2)}",
        ])),
    })
    memory["status"] = "CONFIRMED_ENTRY"
    memory["activation_count"] = int(memory.get("activation_count", 0) or 0) + 1
    memory["updated_at"] = iso_now()
    store[side] = memory
    state["lock_release_opportunities"] = store
    return out


def detect_key_opportunities(context):
    """Internal registry of stable professional opportunity episodes.

    Raw 3M events are intentionally excluded because they can flicker every
    run and would inflate coverage statistics. Only structural transition,
    recovery/base setups and a stable high-quality classifier are counted.
    """
    found = []
    latest_ts = int(getattr((list((context or {}).get("candles_15m_closed") or []) or [None])[-1], "ts", 0) or 0)
    for side in ["LONG", "SHORT"]:
        checks = [
            ("CLOSED_15M_DIRECTION_FLIP", closed_15m_displacement_snapshot(context, side), "confirmed", "trigger_ts"),
            ("CAPITULATION_RECOVERY", capitulation_recovery_snapshot(context, side), "allowed", "trigger_ts"),
            ("FRESH_BASE_CONTINUATION_REENTRY", fresh_base_continuation_snapshot(context, side), "allowed", "trigger_ts"),
        ]
        for typ, snap, key, ts_key in checks:
            if snap.get(key):
                found.append({"side": side, "type": typ, "ts": int(snap.get(ts_key, snap.get("capitulation_ts", latest_ts)) or latest_ts)})
    classifier = (context or {}).get("setup_classifier") or {}
    cside = classifier.get("side") or (context or {}).get("bias")
    ctype = str(classifier.get("type") or "")
    cscore = int(classifier.get("score", 0) or 0)
    stable_types = {"TREND_IGNITION_ENTRY", "TREND_CONTINUATION", "PULLBACK_CONTINUATION", "RANGE_COMPRESSION_BREAKOUT", "SWEEP_REVERSAL"}
    structure = (context or {}).get("structure") or {}; tf15 = (context or {}).get("tf15") or {}
    rolling = rolling_balance_detector(context)
    stable_anchor = bool(cside in ["LONG", "SHORT"] and (structure.get("bias") == cside or tf15.get("bias") == cside))
    range_ok = bool(not rolling.get("balance") or ctype == "RANGE_COMPRESSION_BREAKOUT")
    if cside in ["LONG", "SHORT"] and ctype in stable_types and classifier.get("entry_allowed") and cscore >= 72 and stable_anchor and range_ok:
        found.append({"side": cside, "type": ctype, "ts": latest_ts})
    unique = {}
    for x in found:
        unique[f"{x['side']}:{x['type']}"] = x
    return list(unique.values())


def update_opportunity_coverage(state, context, setup=None, active_trade=None):
    """Count opportunity episodes and upgrade WATCH→covered when entry follows.

    A setup first seen while waiting is temporarily marked MISSED. If the bot
    opens it later during the same structural episode—or already holds the
    correct side—the episode is upgraded rather than counted twice.
    """
    if not isinstance(state, dict):
        return
    analytics = state.get("opportunity_analytics") if isinstance(state.get("opportunity_analytics"), dict) else {}
    analytics.setdefault("new_entries", 0); analytics.setdefault("covered_by_active_trade", 0); analytics.setdefault("missed", 0)
    analytics.setdefault("active_episode_keys", []); analytics.setdefault("episode_status", {}); analytics.setdefault("recent", [])
    previous = set(analytics.get("active_episode_keys") or [])
    statuses = dict(analytics.get("episode_status") or {})
    current_opps = detect_key_opportunities(context)
    current = {f"{opp['side']}:{opp['type']}" for opp in current_opps}

    for opp in current_opps:
        episode_key = f"{opp['side']}:{opp['type']}"
        if active_trade is not None and getattr(active_trade, "side", None) == opp["side"]:
            desired = "COVERED_BY_ACTIVE_TRADE"
        elif isinstance(setup, dict) and setup.get("action") in ["ENTRY", "RISKY_ENTRY"] and setup.get("side") == opp["side"]:
            desired = "NEW_ENTRY"
        else:
            desired = "MISSED"
        old = statuses.get(episode_key)
        if episode_key not in previous or old is None:
            statuses[episode_key] = desired
            if desired == "NEW_ENTRY": analytics["new_entries"] += 1
            elif desired == "COVERED_BY_ACTIVE_TRADE": analytics["covered_by_active_trade"] += 1
            else: analytics["missed"] += 1
            analytics["recent"].append(dict(opp, episode_key=episode_key, status=desired, time=iso_now()))
        elif old == "MISSED" and desired in {"NEW_ENTRY", "COVERED_BY_ACTIVE_TRADE"}:
            analytics["missed"] = max(0, analytics["missed"] - 1)
            if desired == "NEW_ENTRY": analytics["new_entries"] += 1
            else: analytics["covered_by_active_trade"] += 1
            statuses[episode_key] = desired
            analytics["recent"].append(dict(opp, episode_key=episode_key, status="UPGRADED_TO_" + desired, time=iso_now()))

    # An episode may count again only after disappearing and re-forming.
    for key in list(statuses):
        if key not in current:
            statuses.pop(key, None)
    analytics["episode_status"] = statuses
    analytics["active_episode_keys"] = sorted(current)
    analytics["recent"] = analytics["recent"][-100:]
    analytics["covered_total"] = analytics["new_entries"] + analytics["covered_by_active_trade"]
    analytics["total_opportunities"] = analytics["covered_total"] + analytics["missed"]
    analytics["coverage_pct"] = round(analytics["covered_total"] / analytics["total_opportunities"] * 100, 1) if analytics["total_opportunities"] else 0.0
    state["opportunity_analytics"] = analytics



# ==========================================================
# SINGLE-FILE CLEAN DECISION ARCHITECTURE V3
# ==========================================================
# The bot remains one deployable file, but the entry decision is now processed
# as an internal clean architecture:
#   independent candidate lanes -> one deterministic selector -> one lifecycle
#   pass -> one monotonic gate pass -> one geometry rebuild -> immutable result.
# No bridge/memory/lock module may rewrite a finished decision.


class V3DecisionAction(str, Enum):
    NO_TRADE = "NO_TRADE"
    WATCH = "WATCH"
    RISKY_ENTRY = "RISKY_ENTRY"
    ENTRY = "ENTRY"


class V3CandidateSource(str, Enum):
    LIVE_CORE = "LIVE_CORE"
    CLOSED_15M_DIRECTION = "CLOSED_15M_DIRECTION"
    CAPITULATION_RECOVERY = "CAPITULATION_RECOVERY"
    FRESH_BASE = "FRESH_BASE"
    OPPORTUNITY_MEMORY = "OPPORTUNITY_MEMORY"
    LOCK_RELEASE_BRIDGE = "LOCK_RELEASE_BRIDGE"
    PENDING_TRIGGER = "PENDING_TRIGGER"
    SCHEDULER_RESCUE = "SCHEDULER_RESCUE"


@dataclass(frozen=True)
class V3EntryProposal:
    source: str
    side: str
    action: str
    quality: int
    setup_score: int
    direction_margin: float
    geometry_valid: bool
    selection_score: float
    setup: dict = field(compare=False, repr=False)
    context: dict = field(compare=False, repr=False)


V3_SOURCE_PRIORITY = {
    V3CandidateSource.LIVE_CORE.value: 80,
    V3CandidateSource.CLOSED_15M_DIRECTION.value: 98,
    V3CandidateSource.CAPITULATION_RECOVERY.value: 97,
    V3CandidateSource.FRESH_BASE.value: 96,
    V3CandidateSource.OPPORTUNITY_MEMORY.value: 90,
    V3CandidateSource.LOCK_RELEASE_BRIDGE.value: 89,
    V3CandidateSource.PENDING_TRIGGER.value: 86,
    V3CandidateSource.SCHEDULER_RESCUE.value: 84,
}

V3_ACTION_RANK = {
    "NO_TRADE": 0,
    "WATCH": 1,
    "RISKY_ENTRY": 2,
    "ENTRY": 3,
}

V3_VALID_ACTIONS = set(V3_ACTION_RANK)
V3_TRANSITION_SETUPS = {
    "CLOSED_15M_DIRECTION_FLIP",
    "CAPITULATION_RECOVERY",
    "FRESH_BASE_CONTINUATION_REENTRY",
    "RANGE_COMPRESSION_BREAKOUT",
    "SWEEP_REVERSAL",
}


def _v3_clone_context(context):
    """Cheap isolated lane copy: share immutable candle arrays, copy decision state."""
    source = context or {}
    out = dict(source)
    for key, value in list(source.items()):
        if str(key).startswith("candles_"):
            # Candle objects are treated as immutable market facts in decision lanes.
            out[key] = value
        elif isinstance(value, (dict, list, set, tuple)):
            out[key] = deepcopy(value)
    return out


def _v3_action_rank(action):
    return V3_ACTION_RANK.get(str(action or "NO_TRADE").upper(), 0)


def _v3_setup_type(setup):
    info = (setup or {}).get("setup_classifier") or {}
    return str(info.get("type") or (setup or {}).get("setup_type") or "NO_CLEAN_SETUP").upper()


def _v3_setup_score(setup):
    info = (setup or {}).get("setup_classifier") or {}
    return int(clamp(safe_float(info.get("score"), safe_float((setup or {}).get("quality"), 0)) or 0, 0, 100))


def _v3_plan_values(plan):
    if plan is None:
        return None
    getter = plan.get if isinstance(plan, dict) else lambda key, default=None: getattr(plan, key, default)
    try:
        return {
            "entry": float(getter("entry")),
            "stop": float(getter("stop")),
            "tp1": float(getter("tp1")),
            "tp2": float(getter("tp2")),
            "tp3": float(getter("tp3")),
            "rr1": float(getter("rr1", 0) or 0),
            "valid": bool(getter("valid", True)),
            "validation_reason": str(getter("validation_reason", "") or ""),
        }
    except Exception:
        return None


def _v3_plan_valid_for_side(plan, side):
    values = _v3_plan_values(plan)
    if not values or not values["valid"] or side not in ["LONG", "SHORT"]:
        return False
    e, s, t1, t2, t3 = values["entry"], values["stop"], values["tp1"], values["tp2"], values["tp3"]
    if not all(math.isfinite(x) for x in [e, s, t1, t2, t3]):
        return False
    if values["rr1"] <= 0:
        return False
    if side == "LONG":
        return bool(s < e < t1 <= t2 <= t3)
    return bool(s > e > t1 >= t2 >= t3)


def _v3_direction_margin(context, side):
    if side not in ["LONG", "SHORT"]:
        return -100.0
    weights = {
        "tf3": 3.0,
        "tf15": 2.4,
        "structure": 2.4,
        "ict": 2.2,
        "flow": 1.5,
        "cvd": 1.4,
        "liquidity": 1.2,
        "clusters": 0.8,
        "derivatives": 0.7,
        "tf1h": 1.0,
        "tf4h": 0.45,
    }
    margin = 0.0
    for key, weight in weights.items():
        block = (context or {}).get(key) or {}
        bias = str(block.get("bias") or "NEUTRAL").upper()
        magnitude = min(40.0, abs(safe_float(block.get("score"), 0.0) or 0.0)) / 10.0
        if bias == side:
            margin += weight * max(1.0, magnitude)
        elif bias == opposite(side):
            margin -= weight * max(1.0, magnitude)
    regime = (context or {}).get("regime_engine") or (context or {}).get("market_regime") or {}
    regime_bias = str(((regime.get("metrics") or {}).get("bias") or "")).upper()
    if regime_bias == side:
        margin += 2.0
    elif regime_bias == opposite(side):
        margin -= 2.0
    return round(margin, 3)


def _v3_dedupe_text(items):
    out = []
    seen = set()
    for item in items or []:
        text = str(item or "").strip()
        key = re.sub(r"\s+", " ", text).lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _v3_clean_side_messages(items, final_side):
    """Remove only stale directional metadata, not legitimate opposite evidence."""
    if final_side not in ["LONG", "SHORT"]:
        return _v3_dedupe_text(items)
    other = opposite(final_side)
    stale_patterns = [
        "після слабкого виходу",
        "штраф якості",
        "re-entry cooldown",
        "same-side cooldown",
        "тимчасово знижена",
    ]
    cleaned = []
    for item in items or []:
        text = str(item or "")
        low = text.lower()
        if any(pattern in low for pattern in stale_patterns) and other.lower() in low:
            continue
        cleaned.append(text)
    return _v3_dedupe_text(cleaned)


def _v3_watch_title(side, setup):
    info = (setup or {}).get("setup_classifier") or {}
    setup_type = str(info.get("type") or "").upper()
    if setup_type == "NO_CLEAN_SETUP":
        return "ВХОДУ НЕМАЄ — ЧИСТОГО СЕТАПУ НЕМАЄ"
    label = str(info.get("label") or "").strip()
    if label:
        return f"ЧЕКАТИ — {side} НЕ ПІДТВЕРДЖЕНИЙ"
    return f"ЧЕКАТИ — {side} ГОТУЄТЬСЯ"


def _v3_is_no_clean_setup(setup):
    info = (setup or {}).get("setup_classifier") or {}
    return str(info.get("type") or "").upper() == "NO_CLEAN_SETUP"


def _v3_canonicalize_no_clean_setup(setup, audit=None, stage="CANONICALIZE"):
    """Convert NO_CLEAN_SETUP into a neutral no-trade decision.

    A directional bias may still exist internally, but it is market context, not
    an entry candidate. This prevents Telegram and the journal from presenting
    LONG/SHORT as "preparing" when the classifier explicitly blocks entry.
    """
    if not isinstance(setup, dict) or not _v3_is_no_clean_setup(setup):
        return setup

    out = dict(setup)
    original_side = str(out.get("side") or "NEUTRAL").upper()
    if original_side in ["LONG", "SHORT"]:
        out["market_direction_context"] = original_side

    info = dict(out.get("setup_classifier") or {})
    info["type"] = "NO_CLEAN_SETUP"
    info["label"] = info.get("label") or "⚪ Чистого сетапу немає"
    info["side"] = "NEUTRAL"
    info["entry_allowed"] = False
    info["block_entry"] = True
    out["setup_classifier"] = info

    quality = int(clamp(safe_float(out.get("quality"), 0) or 0, 0, 55))
    prior_reason = str(out.get("reason") or "").strip()
    if "чистого сетапу" not in prior_reason.lower():
        prior_reason = ("чистого сетапу немає; " + prior_reason).strip(" ;")

    out.update({
        "action": "NO_TRADE",
        "side": "NEUTRAL",
        "quality": quality,
        "current_readiness": quality,
        "plan": None,
        "title": "ВХОДУ НЕМАЄ — ЧИСТОГО СЕТАПУ НЕМАЄ",
        "reason": prior_reason or "чистого сетапу немає",
        "entry_level": "BLOCK",
        "entry_level_label": "⚪ ВХОДУ НЕМАЄ — чистого сетапу немає",
        "entry_blocked": True,
        "watch_trigger": False,
        "show_wait_plan": False,
        "pending_trigger_activated": False,
        "lock_release_bridge_activated": False,
    })
    out["reason_codes"] = _v3_dedupe_text(list(out.get("reason_codes") or []) + ["NO_CLEAN_SETUP_CANONICALIZED"])

    if isinstance(audit, list):
        audit.append({
            "stage": stage,
            "code": "NO_CLEAN_SETUP_TO_NEUTRAL_NO_TRADE",
            "market_direction_context": original_side if original_side in ["LONG", "SHORT"] else None,
        })
    return out


def _v3_safe_no_trade(reason, error_code="SAFE_MODE"):
    return {
        "action": "NO_TRADE",
        "side": "NEUTRAL",
        "quality": 0,
        "current_readiness": 0,
        "title": "ВХОДУ НЕМАЄ — БЕЗПЕЧНИЙ РЕЖИМ",
        "reason": reason,
        "plan": None,
        "confirmations": [],
        "conflicts": [reason],
        "setup_classifier": None,
        "entry_level": "BLOCK",
        "reason_codes": [error_code],
        "architecture_version": ARCHITECTURE_VERSION,
    }


def _v3_normalize_setup(setup, context, source, audit=None):
    audit = audit if isinstance(audit, list) else []
    raw = dict(setup or {})
    action = str(raw.get("action") or "NO_TRADE").upper()
    if action not in V3_VALID_ACTIONS:
        audit.append({"stage": "NORMALIZE", "code": "UNKNOWN_ACTION", "value": action})
        action = "NO_TRADE"
    side = str(raw.get("side") or "NEUTRAL").upper()
    if side not in ["LONG", "SHORT", "NEUTRAL"]:
        audit.append({"stage": "NORMALIZE", "code": "UNKNOWN_SIDE", "value": side})
        side = "NEUTRAL"
    if action in ["ENTRY", "RISKY_ENTRY", "WATCH"] and side not in ["LONG", "SHORT"]:
        action = "NO_TRADE"
        side = "NEUTRAL"
        raw["reason"] = "сторона кандидата не визначена; вхід не дозволено"
        audit.append({"stage": "NORMALIZE", "code": "MISSING_DIRECTION"})

    quality = int(clamp(safe_float(raw.get("quality"), 0) or 0, 0, 100))
    raw["action"] = action
    raw["side"] = side
    raw["quality"] = quality

    # The formatter and journal must never re-classify after the decision.
    # Ensure the selected side owns its classifier before proposal scoring.
    if side in ["LONG", "SHORT"]:
        info = raw.get("setup_classifier") if isinstance(raw.get("setup_classifier"), dict) else None
        info_side = str((info or {}).get("side") or "").upper()
        if info is None or info_side != side:
            if info is not None and info_side in ["LONG", "SHORT"] and info_side != side:
                audit.append({"stage": "NORMALIZE", "code": "CLASSIFIER_SIDE_MISMATCH", "from": info_side, "to": side})
            try:
                refreshed = classify_setup(context, side) if isinstance(context, dict) and context.get("price") else None
            except Exception:
                refreshed = None
            if not isinstance(refreshed, dict):
                refreshed = {
                    "type": "NO_CLEAN_SETUP", "label": "⚪ Чистого сетапу немає", "side": side,
                    "score": min(55, quality), "entry_allowed": False, "block_entry": True,
                    "risk_mode": "NORMAL", "reason": "класифікатор сторони не підтверджений",
                    "quality_adjustment": 0, "quality_cap": 55, "force_risky": False,
                }
            refreshed["side"] = side
            raw["setup_classifier"] = refreshed
            if raw["action"] in ["ENTRY", "RISKY_ENTRY"] and refreshed.get("block_entry"):
                raw["action"] = "WATCH"
                raw["plan"] = None
                raw["reason"] = "обраний кандидат не має узгодженого класифікатора для своєї сторони"
                raw.setdefault("reason_codes", []).append("CLASSIFIER_SIDE_NOT_CONFIRMED")

    raw = _v3_canonicalize_no_clean_setup(raw, audit, "NORMALIZE")
    action = str(raw.get("action") or "NO_TRADE").upper()
    side = str(raw.get("side") or "NEUTRAL").upper()
    quality = int(clamp(safe_float(raw.get("quality"), 0) or 0, 0, 100))
    raw["current_readiness"] = quality
    raw["candidate_source"] = str(source)
    raw["architecture_version"] = ARCHITECTURE_VERSION
    raw["confirmations"] = _v3_clean_side_messages(raw.get("confirmations"), side)
    raw["conflicts"] = _v3_clean_side_messages(raw.get("conflicts"), side)
    raw.setdefault("reason_codes", [])

    plan_ok = _v3_plan_valid_for_side(raw.get("plan"), side)
    if action in ["ENTRY", "RISKY_ENTRY"] and not plan_ok:
        raw["action"] = "WATCH"
        raw["title"] = _v3_watch_title(side, raw)
        raw["reason"] = str(raw.get("reason") or "") + ("; " if raw.get("reason") else "") + "технічний план не пройшов фінальну перевірку сторони/геометрії"
        raw["plan"] = None
        raw["entry_level"] = "WATCH_TRIGGER"
        raw["reason_codes"] = _v3_dedupe_text(list(raw.get("reason_codes") or []) + ["PLAN_SIDE_OR_GEOMETRY_INVALID"])
        audit.append({"stage": "NORMALIZE", "code": "ENTRY_DOWNGRADED_INVALID_PLAN", "side": side})
    elif raw["action"] not in ["ENTRY", "RISKY_ENTRY"] and raw.get("plan") is not None and not plan_ok:
        raw["plan"] = None

    if raw["action"] == "NO_TRADE" and side == "NEUTRAL":
        raw["plan"] = None
    if raw["action"] in ["ENTRY", "RISKY_ENTRY"]:
        raw["entry_level"] = raw["action"]
        raw["entry_level_label"] = _entry_level_label(raw["action"])
    elif raw["action"] == "WATCH":
        raw["entry_level"] = "WATCH_TRIGGER"
        raw["entry_level_label"] = _entry_level_label("WATCH_TRIGGER")
        raw.setdefault("title", _v3_watch_title(side, raw))
    else:
        raw["entry_level"] = "BLOCK"
        if _v3_is_no_clean_setup(raw):
            raw["entry_level_label"] = "⚪ ВХОДУ НЕМАЄ — чистого сетапу немає"
            raw["title"] = "ВХОДУ НЕМАЄ — ЧИСТОГО СЕТАПУ НЕМАЄ"
        else:
            raw["entry_level_label"] = _entry_level_label("BLOCK")
            raw.setdefault("title", "ВХОДУ НЕМАЄ")
    raw.setdefault("reason", "перевага нечітка, немає професійного входу")
    return raw


def _v3_proposal_score(source, setup, context):
    action = str((setup or {}).get("action") or "NO_TRADE").upper()
    side = str((setup or {}).get("side") or "NEUTRAL").upper()
    quality = int((setup or {}).get("quality", 0) or 0)
    setup_score = _v3_setup_score(setup)
    direction_margin = _v3_direction_margin(context, side)
    geometry_valid = _v3_plan_valid_for_side((setup or {}).get("plan"), side)
    source_priority = V3_SOURCE_PRIORITY.get(str(source), 70)
    score = (
        _v3_action_rank(action) * 1000.0
        + quality * 8.0
        + setup_score * 3.0
        + direction_margin * 6.0
        + source_priority
        + (85.0 if geometry_valid else 0.0)
        + (35.0 if _v3_setup_type(setup) in V3_TRANSITION_SETUPS else 0.0)
    )
    return quality, setup_score, direction_margin, geometry_valid, round(score, 3)


def _v3_make_proposal(source, setup, context, audit):
    normalized = _v3_normalize_setup(setup, context, source, audit)
    quality, setup_score, direction_margin, geometry_valid, selection_score = _v3_proposal_score(source, normalized, context)
    return V3EntryProposal(
        source=str(source),
        side=str(normalized.get("side") or "NEUTRAL"),
        action=str(normalized.get("action") or "NO_TRADE"),
        quality=quality,
        setup_score=setup_score,
        direction_margin=direction_margin,
        geometry_valid=geometry_valid,
        selection_score=selection_score,
        setup=normalized,
        context=context,
    )


def _v3_semantic_setup_signature(setup):
    raw = setup or {}
    plan = _v3_plan_values(raw.get("plan")) or {}
    return (
        str(raw.get("action") or "NO_TRADE").upper(),
        str(raw.get("side") or "NEUTRAL").upper(),
        int(raw.get("quality", 0) or 0),
        _v3_setup_type(raw),
        round(safe_float(plan.get("entry"), 0.0) or 0.0, 4),
        round(safe_float(plan.get("stop"), 0.0) or 0.0, 4),
        round(safe_float(plan.get("tp1"), 0.0) or 0.0, 4),
        bool(raw.get("pending_trigger_activated")),
        bool(raw.get("lock_release_bridge_activated")),
        bool(raw.get("geometry_persistence_activated")),
        bool(raw.get("transition_override_confirmed")),
    )


def _v3_run_candidate_lane(source, func, seed_context, seed_setup, audit):
    lane_context = _v3_clone_context(seed_context)
    lane_setup = deepcopy(seed_setup)
    try:
        out = func(lane_context, lane_setup)
        if _v3_semantic_setup_signature(out) == _v3_semantic_setup_signature(seed_setup):
            return None
        return _v3_make_proposal(source, out, lane_context, audit)
    except Exception as error:
        audit.append({"stage": "CANDIDATE_LANE", "source": str(source), "code": "LANE_ERROR", "error": str(error)[:240]})
        return None


def _v3_collect_proposals(context, base_setup, audit):
    seed_context = _v3_clone_context(context)
    seed_setup = deepcopy(base_setup)
    proposals = [_v3_make_proposal(V3CandidateSource.LIVE_CORE.value, seed_setup, seed_context, audit)]
    lanes = [
        (V3CandidateSource.CLOSED_15M_DIRECTION.value, resolve_direction_flip_priority_lane),
        (V3CandidateSource.CAPITULATION_RECOVERY.value, resolve_capitulation_recovery_geometry_opportunity),
        (V3CandidateSource.FRESH_BASE.value, resolve_fresh_base_continuation_reentry),
        (V3CandidateSource.OPPORTUNITY_MEMORY.value, activate_opportunity_memory_if_ready),
        (V3CandidateSource.LOCK_RELEASE_BRIDGE.value, activate_lock_release_opportunity_bridge),
        (V3CandidateSource.PENDING_TRIGGER.value, activate_pending_trigger_if_ready),
        (V3CandidateSource.SCHEDULER_RESCUE.value, resolve_15m_scheduler_entry_opportunity),
    ]
    for source, func in lanes:
        proposal = _v3_run_candidate_lane(source, func, seed_context, seed_setup, audit)
        if proposal is not None:
            proposals.append(proposal)

    # Deduplicate identical proposals generated by more than one memory lane.
    unique = {}
    for proposal in proposals:
        plan = _v3_plan_values(proposal.setup.get("plan")) or {}
        signature = (
            proposal.side,
            proposal.action,
            _v3_setup_type(proposal.setup),
            round(safe_float(plan.get("entry"), 0.0) or 0.0, 4),
            round(safe_float(plan.get("stop"), 0.0) or 0.0, 4),
        )
        old = unique.get(signature)
        if old is None or proposal.selection_score > old.selection_score:
            unique[signature] = proposal
    return list(unique.values())


def _v3_select_proposal(proposals, audit):
    if not proposals:
        return None
    live = next((p for p in proposals if p.source == V3CandidateSource.LIVE_CORE.value), None)
    filtered = []
    for proposal in proposals:
        if live and _v3_action_rank(live.action) >= _v3_action_rank("RISKY_ENTRY") and proposal.side != live.side:
            st = _v3_setup_type(proposal.setup)
            confirmed_transition = bool(
                st in V3_TRANSITION_SETUPS
                and (
                    proposal.setup.get("transition_override_confirmed")
                    or proposal.setup.get("capitulation_recovery_confirmed")
                    or proposal.setup.get("fresh_base_reentry_confirmed")
                    or (proposal.setup.get("closed_15m_displacement_override") or {}).get("confirmed")
                )
            )
            if not confirmed_transition or proposal.quality < live.quality + 4:
                audit.append({
                    "stage": "SELECTOR", "code": "OPPOSITE_STALE_PROPOSAL_REJECTED",
                    "source": proposal.source, "side": proposal.side, "live_side": live.side,
                })
                continue
        filtered.append(proposal)
    if not filtered:
        filtered = proposals
    selected = max(filtered, key=lambda p: (p.selection_score, p.source))
    audit.append({
        "stage": "SELECTOR", "code": "SELECTED", "source": selected.source,
        "side": selected.side, "action": selected.action,
        "score": selected.selection_score, "quality": selected.quality,
        "setup_score": selected.setup_score, "direction_margin": selected.direction_margin,
    })
    return selected


def _v3_enforce_stage_invariants(previous, current, fixed_side, stage_name, allow_upgrade, audit):
    prev = dict(previous or {})
    cur = _v3_normalize_setup(current, {}, "FINAL_PIPELINE", audit)
    canonical_no_clean = bool(
        _v3_is_no_clean_setup(cur)
        and cur.get("action") == "NO_TRADE"
        and cur.get("side") == "NEUTRAL"
    )
    if fixed_side in ["LONG", "SHORT"] and cur.get("side") != fixed_side and not canonical_no_clean:
        audit.append({
            "stage": stage_name, "code": "SIDE_MUTATION_BLOCKED",
            "from": fixed_side, "to": cur.get("side"),
        })
        cur = dict(prev)
        cur["action"] = "WATCH"
        cur["side"] = fixed_side
        cur["plan"] = None
        cur["entry_level"] = "WATCH_TRIGGER"
        cur["entry_level_label"] = _entry_level_label("WATCH_TRIGGER")
        cur["title"] = _v3_watch_title(fixed_side, cur)
        cur["reason"] = "модуль спробував змінити вже вибрану сторону; рішення зупинено до нового незалежного кандидата"
        cur["reason_codes"] = _v3_dedupe_text(list(cur.get("reason_codes") or []) + ["SIDE_MUTATION_PREVENTED"])
    elif canonical_no_clean and fixed_side in ["LONG", "SHORT"]:
        audit.append({
            "stage": stage_name,
            "code": "NO_CLEAN_NEUTRALIZATION_ALLOWED",
            "market_direction_context": fixed_side,
        })
    if not allow_upgrade and _v3_action_rank(cur.get("action")) > _v3_action_rank(prev.get("action")):
        audit.append({
            "stage": stage_name, "code": "NON_MONOTONIC_UPGRADE_BLOCKED",
            "from": prev.get("action"), "to": cur.get("action"),
        })
        cur["action"] = prev.get("action")
        cur["plan"] = prev.get("plan") if _v3_action_rank(prev.get("action")) >= 2 else None
        cur["entry_level"] = prev.get("entry_level")
        cur["entry_level_label"] = prev.get("entry_level_label")
        cur["title"] = prev.get("title")
        cur["reason"] = prev.get("reason")
        cur["reason_codes"] = _v3_dedupe_text(list(cur.get("reason_codes") or []) + ["NON_MONOTONIC_UPGRADE_PREVENTED"])
    cur["confirmations"] = _v3_clean_side_messages(cur.get("confirmations"), fixed_side)
    cur["conflicts"] = _v3_clean_side_messages(cur.get("conflicts"), fixed_side)
    return cur


def _v3_run_gate_once(context, setup, gate_name, gate_func, fixed_side, audit):
    previous = deepcopy(setup)
    try:
        current = _apply_gate_with_pre_memory(context, deepcopy(setup), gate_func, gate_name)
    except Exception as error:
        audit.append({"stage": gate_name, "code": "GATE_ERROR", "error": str(error)[:240]})
        current = dict(previous)
        current["action"] = "WATCH" if fixed_side in ["LONG", "SHORT"] else "NO_TRADE"
        current["plan"] = None
        current["reason"] = f"внутрішня перевірка {gate_name} не завершена; вхід безпечно відкладено"
        current["reason_codes"] = _v3_dedupe_text(list(current.get("reason_codes") or []) + [f"{gate_name}_ERROR"])
    current = _v3_enforce_stage_invariants(previous, current, fixed_side, gate_name, False, audit)
    audit.append({
        "stage": gate_name,
        "from_action": previous.get("action"),
        "to_action": current.get("action"),
        "quality": current.get("quality"),
    })
    return current


def _v3_apply_gates_once(context, setup, fixed_side, audit):
    gates = [
        ("STRICT_TRANSITION_GATE", apply_strict_transition_gate_to_setup),
        ("POST_SHOCK_RETEST_GATE", apply_post_shock_retest_gate_to_setup),
        ("CAPITULATION_RECOVERY_GATE", apply_capitulation_recovery_gate_to_setup),
        ("RANGE_SEGMENTATION_GATE", apply_range_midpoint_gate_to_setup),
        ("STRUCTURAL_RESET_GATE", apply_structural_reset_gate_to_setup),
        ("REGIME_WHITELIST_GATE", apply_regime_allowed_setup_whitelist),
        ("PRIME_ICT_HTF_CONFLICT_CAP", apply_prime_ict_htf_conflict_cap),
        ("PRIME_ICT_ADVERSE_FLOW_GATE", apply_adverse_flow_veto_prime_ict),
        ("ENTRY_RISK_PACKAGE_GATE", apply_entry_risk_package_gate),
        ("FINAL_SAME_SIDE_EXHAUSTION_LOCK", apply_final_same_side_exhaustion_lock),
    ]
    current = setup
    for gate_name, gate_func in gates:
        current = _v3_run_gate_once(context, current, gate_name, gate_func, fixed_side, audit)
    return current


def _v3_lock_record(context, side):
    state = (context or {}).get("runtime_state") if isinstance((context or {}).get("runtime_state"), dict) else {}
    locks = (context or {}).get("persistent_exhaustion_locks") or state.get("persistent_exhaustion_locks") or {}
    persistent = locks.get(side) if isinstance(locks, dict) and isinstance(locks.get(side), dict) else {}
    shock = state.get("shock_lock") if isinstance(state.get("shock_lock"), dict) else {}
    p_status = str(persistent.get("status") or ("ACTIVE" if persistent.get("active") else "INACTIVE")).upper()
    s_status = str(shock.get("status") or ("ACTIVE" if shock.get("active") else "INACTIVE")).upper()
    p_active = bool(persistent.get("active") or p_status in {"ACTIVE", "LOCKED", "RELEASE_PENDING"})
    s_active = bool((shock.get("side") == side) and (shock.get("active") or s_status in {"ACTIVE", "LOCKED", "RELEASE_PENDING"}))
    p_released = bool(p_status == "RELEASED" or persistent.get("released"))
    s_released = bool(shock.get("side") == side and (s_status == "RELEASED" or shock.get("released")))
    return {
        "persistent_status": p_status,
        "shock_status": s_status,
        "active": bool(p_active or s_active),
        "released": bool((p_released or s_released) and not (p_active or s_active)),
        "persistent": persistent,
        "shock": shock,
    }


def _v3_canonical_lock_guard(context, setup, fixed_side, audit):
    current = dict(setup or {})
    if fixed_side not in ["LONG", "SHORT"]:
        return current
    lock = _v3_lock_record(context, fixed_side)
    reset_allowed = bool(
        current.get("transition_override_confirmed")
        or current.get("fresh_base_exhaustion_reset")
        or current.get("capitulation_recovery_confirmed")
        or current.get("fresh_base_reentry_confirmed")
    )
    current["canonical_lock_state"] = lock
    if lock["active"] and not reset_allowed:
        if _v3_action_rank(current.get("action")) >= 2:
            audit.append({"stage": "CANONICAL_LOCK", "code": "ACTIVE_LOCK_DOWNGRADE", "side": fixed_side})
        current["action"] = "WATCH"
        current["plan"] = None
        current["title"] = _v3_watch_title(effective_side, current)
        current["reason"] = "активний exhaustion/shock lock: потрібен новий незалежний ретест, база або reversal/continuation event"
        current["reason_codes"] = _v3_dedupe_text(list(current.get("reason_codes") or []) + ["CANONICAL_LOCK_ACTIVE"])
    elif lock["released"]:
        stale_lock_text = []
        for item in current.get("conflicts") or []:
            low = str(item).lower()
            if "persistent exhaustion" in low or "impulse lock" in low or "shock lock" in low:
                continue
            stale_lock_text.append(item)
        current["conflicts"] = _v3_dedupe_text(stale_lock_text)
        plan_values = _v3_plan_values(current.get("plan")) or {}
        validation_reason = str(plan_values.get("validation_reason") or "").lower()
        if "persistent exhaustion" in validation_reason or "impulse lock" in validation_reason or "shock lock" in validation_reason:
            current["plan"] = None
            if _v3_action_rank(current.get("action")) >= 2:
                current["action"] = "WATCH"
            current["plan_rebuild_required"] = True
            current["plan_rebuild_status"] = "WAIT_FRESH_TRIGGER_AFTER_RELEASE"
            current["reason_codes"] = _v3_dedupe_text(list(current.get("reason_codes") or []) + ["STALE_PRE_RELEASE_PLAN_REMOVED"])
            audit.append({"stage": "CANONICAL_LOCK", "code": "STALE_PRE_RELEASE_PLAN_REMOVED", "side": fixed_side})
    return _v3_normalize_setup(current, context, "FINAL_PIPELINE", audit)


def _v3_final_geometry_and_entry_level(context, setup, fixed_side, audit):
    current = deepcopy(setup)
    before = deepcopy(current)
    try:
        rebuilt = final_stop_role_rebuild(current, context)
    except Exception as error:
        audit.append({"stage": "FINAL_GEOMETRY_REBUILD", "code": "ERROR", "error": str(error)[:240]})
        rebuilt = dict(current)
        rebuilt["action"] = "WATCH" if fixed_side in ["LONG", "SHORT"] else "NO_TRADE"
        rebuilt["plan"] = None
        rebuilt["reason"] = "фінальну геометрію не вдалося безпечно перебудувати"
        rebuilt["reason_codes"] = _v3_dedupe_text(list(rebuilt.get("reason_codes") or []) + ["FINAL_GEOMETRY_ERROR"])
    capture_pre_gate_opportunity_memory(context, before, rebuilt, "V3_FINAL_STOP_ROLE_REBUILD")
    rebuilt = _v3_enforce_stage_invariants(before, rebuilt, fixed_side, "FINAL_GEOMETRY_REBUILD", False, audit)
    rebuilt = _v3_run_gate_once(context, rebuilt, "FINAL_ENTRY_LEVEL_GATE", apply_entry_level_gate, fixed_side, audit)
    return rebuilt


def _v3_decision_fingerprint(setup):
    raw = setup or {}
    plan = _v3_plan_values(raw.get("plan"))
    info = raw.get("setup_classifier") if isinstance(raw.get("setup_classifier"), dict) else {}
    payload = {
        "action": raw.get("action"),
        "side": raw.get("side"),
        "quality": int(raw.get("quality", 0) or 0),
        "entry_level": raw.get("entry_level"),
        "setup_type": info.get("type"),
        "setup_score": int(info.get("score", 0) or 0),
        "plan": plan,
        "reason_codes": list(raw.get("reason_codes") or []),
    }
    return json.dumps(make_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _v3_verify_post_decision_integrity(setup):
    if not isinstance(setup, dict):
        return _v3_safe_no_trade("фінальне рішення має некоректний формат", "POST_DECISION_FORMAT_ERROR")
    expected = setup.get("decision_fingerprint")
    if not expected:
        return setup
    actual = _v3_decision_fingerprint(setup)
    if actual == expected:
        return setup
    safe = _v3_safe_no_trade(
        "після фінального рішення виявлено спробу його змінити; вхід скасовано безпечним режимом",
        "POST_DECISION_MUTATION_PREVENTED",
    )
    safe["decision_pipeline_v3"] = deepcopy(setup.get("decision_pipeline_v3") or {})
    safe["decision_pipeline_v3"]["post_decision_mutation_detected"] = True
    safe["decision_pipeline_v3"]["expected_fingerprint"] = expected
    safe["decision_pipeline_v3"]["actual_fingerprint"] = actual
    return safe


def _v3_finalize_decision(context, setup, fixed_side, source, proposals, audit):
    current = _v3_normalize_setup(setup, context, source, audit)
    current = _v3_canonical_lock_guard(context, current, fixed_side, audit)
    current = _v3_canonicalize_no_clean_setup(current, audit, "FINALIZE")
    effective_side = str(current.get("side") or "NEUTRAL").upper()
    current["confirmations"] = _v3_clean_side_messages(current.get("confirmations"), effective_side)
    current["conflicts"] = _v3_clean_side_messages(current.get("conflicts"), effective_side)

    if current.get("action") in ["ENTRY", "RISKY_ENTRY"] and not _v3_plan_valid_for_side(current.get("plan"), effective_side):
        current["action"] = "WATCH"
        current["plan"] = None
        current["title"] = _v3_watch_title(fixed_side, current)
        current["reason"] = "фінальна геометрія не відповідає вибраній стороні; потрібна нова точка входу"
        current["reason_codes"] = _v3_dedupe_text(list(current.get("reason_codes") or []) + ["FINAL_PLAN_INVARIANT_FAILED"])
        audit.append({"stage": "FINALIZE", "code": "FINAL_PLAN_INVARIANT_FAILED"})

    current["quality"] = int(clamp(current.get("quality", 0), 0, 100))
    current["current_readiness"] = current["quality"]
    current["selected_setup_score"] = _v3_setup_score(current)
    current["candidate_source"] = source
    current["architecture_version"] = ARCHITECTURE_VERSION
    current["decision_id"] = uuid.uuid4().hex[:12]
    current["decision_pipeline_v3"] = {
        "version": ARCHITECTURE_VERSION,
        "decision_id": current["decision_id"],
        "selected_source": source,
        "selected_side": effective_side,
        "selected_action": current.get("action"),
        "candidate_count": len(proposals),
        "candidates": [
            {
                "source": p.source,
                "side": p.side,
                "action": p.action,
                "quality": p.quality,
                "setup_score": p.setup_score,
                "direction_margin": p.direction_margin,
                "geometry_valid": p.geometry_valid,
                "selection_score": p.selection_score,
                "setup_type": _v3_setup_type(p.setup),
            }
            for p in sorted(proposals, key=lambda x: x.selection_score, reverse=True)
        ],
        "audit": audit[-80:],
        "invariants": {
            "side_frozen": (
                current.get("side") == fixed_side
                or (current.get("side") == "NEUTRAL" and _v3_is_no_clean_setup(current))
                or fixed_side not in ["LONG", "SHORT"]
            ),
            "entry_requires_valid_plan": current.get("action") not in ["ENTRY", "RISKY_ENTRY"] or _v3_plan_valid_for_side(current.get("plan"), effective_side),
            "no_clean_setup_is_neutral_no_trade": not _v3_is_no_clean_setup(current) or (current.get("action") == "NO_TRADE" and current.get("side") == "NEUTRAL"),
            "single_gate_pass": True,
            "single_geometry_rebuild": True,
            "post_decision_mutation_allowed": False,
        },
    }
    current["decision_fingerprint"] = _v3_decision_fingerprint(current)
    current["decision_pipeline_v3"]["decision_fingerprint"] = current["decision_fingerprint"]
    return current


def _v3_commit_context(target, selected_context):
    if not isinstance(target, dict) or not isinstance(selected_context, dict):
        return
    real_state = target.get("runtime_state") if isinstance(target.get("runtime_state"), dict) else None
    selected_state = selected_context.get("runtime_state") if isinstance(selected_context.get("runtime_state"), dict) else None
    if real_state is not None and selected_state is not None:
        real_state.clear()
        real_state.update(deepcopy(selected_state))
        selected_context = _v3_clone_context(selected_context)
        selected_context["runtime_state"] = real_state
    else:
        selected_context = _v3_clone_context(selected_context)
    target.clear()
    target.update(selected_context)


def evaluate_new_setup(context):
    """Deterministic one-file entry pipeline.

    Analytical engines are preserved, but candidate lanes are isolated. Only one
    candidate is selected, its side is frozen, gates run once and may only
    downgrade, geometry is rebuilt once, and the final decision becomes immutable
    for Telegram, journal and memory updates.
    """
    audit = []
    try:
        refresh_persistent_exhaustion_locks(context)
        base_setup = _evaluate_new_setup_core(context)
        base_setup = expire_recovery_thesis_if_invalid(context, base_setup)
        seed_context = _v3_clone_context(context)
        proposals = _v3_collect_proposals(seed_context, base_setup, audit)
        selected = _v3_select_proposal(proposals, audit)
        if selected is None:
            return _v3_safe_no_trade("жоден кандидат не пройшов внутрішню побудову", "NO_CANDIDATE")

        working_context = _v3_clone_context(selected.context)
        working_setup = deepcopy(selected.setup)
        fixed_side = selected.side if selected.side in ["LONG", "SHORT"] else "NEUTRAL"

        # Lifecycle may refine readiness once before gates. It is the only stage
        # allowed to upgrade the selected proposal. Every later stage is monotonic.
        before_lifecycle = deepcopy(working_setup)
        try:
            lifecycle_setup = apply_professional_lifecycle_to_setup(deepcopy(working_setup), working_context)
        except Exception as error:
            audit.append({"stage": "LIFECYCLE", "code": "ERROR", "error": str(error)[:240]})
            lifecycle_setup = before_lifecycle
        working_setup = _v3_enforce_stage_invariants(
            before_lifecycle, lifecycle_setup, fixed_side, "LIFECYCLE", True, audit
        )

        working_setup = _v3_apply_gates_once(working_context, working_setup, fixed_side, audit)
        working_setup = _v3_final_geometry_and_entry_level(working_context, working_setup, fixed_side, audit)
        final_setup = _v3_finalize_decision(
            working_context, working_setup, fixed_side, selected.source, proposals, audit
        )
        _v3_commit_context(context, working_context)
        context["setup_classifier"] = final_setup.get("setup_classifier")
        context["decision_pipeline_v3"] = final_setup.get("decision_pipeline_v3")
        return final_setup
    except Exception as error:
        print(f"[WARN] {ARCHITECTURE_VERSION} safe-mode: {error}")
        safe = _v3_safe_no_trade(
            "внутрішній pipeline не завершив усі перевірки; угода не відкривається",
            "PIPELINE_SAFE_MODE",
        )
        safe["pipeline_error"] = str(error)[:400]
        safe["decision_pipeline_v3"] = {
            "version": ARCHITECTURE_VERSION,
            "safe_mode": True,
            "audit": audit[-40:],
        }
        return safe




def new_active_trade(setup):
    plan = setup["plan"]
    return ActiveTrade(
        id=uuid.uuid4().hex[:10],
        side=setup["side"],
        opened_at=iso_now(),
        entry=plan.entry,
        stop_initial=plan.stop,
        stop_current=plan.stop,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        quality=setup["quality"],
        tp1_stop_locked=False,
        tp2_stop_locked=False,
        tp1_locked_stop=0.0,
        tp2_locked_stop=0.0,
        best_price=plan.entry,
        entry_integrity_score=100,
        entry_fail_streak=0,
        entry_recovery_checks=0,
        entry_last_state="СИЛЬНА",
        setup_type=str((setup.get("setup_classifier") or {}).get("type") or ""),
        regime_type=str((setup.get("regime_engine") or {}).get("regime_type") or (setup.get("regime_engine") or {}).get("name") or ""),
        entry_level=str(setup.get("entry_level") or setup.get("action") or ""),
        lifecycle_stage=str((setup.get("lifecycle") or {}).get("stage") or ("EARLY_VALIDATION" if setup.get("entry_level") == "RISKY_ENTRY" else "ENTRY_VALIDATION")),
        lifecycle_score=int((setup.get("lifecycle") or {}).get("score", 50) or 50),
        lifecycle_confirmations=0,
        lifecycle_failures=0,
        lifecycle_last_transition=iso_now(),
        structural_stop=float(plan.stop),
        profit_stop=0.0,
        active_stop_source="STRUCTURAL",
        recovery_after_rejection=bool((setup.get("post_rejection_recovery") or {}).get("active")),
        recovery_trigger_ts=int((setup.get("post_rejection_recovery") or {}).get("trigger_ts", 0) or 0),
        recovery_trigger_level=float((setup.get("post_rejection_recovery") or {}).get("trigger_level", 0) or 0),
        recovery_event_type=str((setup.get("post_rejection_recovery") or {}).get("event_type") or ""),
        recovery_entry_checks=0,
        management_checks=0,
        mfe_profit_lock_streak=0,
        mfe_profit_lock_active=False,
        notes=(
            (["RISKY_ENTRY"] if setup.get("action") == "RISKY_ENTRY" else [])
            + (["COUNTERTREND_ENTRY"] if setup.get("countertrend_entry") else [])
            + (["ENTRY_LEVEL: " + str(setup.get("entry_level"))] if setup.get("entry_level") else [])
            + (["SETUP_CLASSIFIER: " + str((setup.get("setup_classifier") or {}).get("type"))] if setup.get("setup_classifier") else [])
            + (["REGIME_TYPE: " + str((setup.get("regime_engine") or {}).get("regime_type") or (setup.get("regime_engine") or {}).get("name"))] if setup.get("regime_engine") else [])
            + (["LIFECYCLE_STAGE: " + str((setup.get("lifecycle") or {}).get("stage"))] if setup.get("lifecycle") else [])
            + (["ENGINE_VERSION: GEOMETRY_PERSISTENCE_ENTRY_CONSENSUS_GUARDS"])
            + (["POST_REJECTION_RECOVERY"] if (setup.get("post_rejection_recovery") or {}).get("active") else [])
            + (["RECOVERY_TRIGGER_LEVEL: " + str((setup.get("post_rejection_recovery") or {}).get("trigger_level"))] if (setup.get("post_rejection_recovery") or {}).get("active") else [])
            + ([setup.get("reason", "")])
        ),
    )




def localize_user_text(text):
    """Фінальне очищення Telegram-тексту українською.

    Внутрішні ключі залишаються англійською для стабільності коду, але в
    сповіщенні користувач бачить українські назви рівнів і сетапів.
    """
    if text is None:
        return ""
    text = str(text)
    replacements = [
        ("WATCH_TRIGGER", "ЧЕКАТИ ПІДТВЕРДЖЕННЯ"),
        ("RISKY_ENTRY", "РИЗИКОВАНИЙ ВХІД"),
        ("ENTRY_LEVEL", "РІВЕНЬ ВХОДУ"),
        ("NO_TRADE", "ВХОДУ НЕМАЄ"),
        ("WAIT_RETEST", "ЧЕКАТИ РЕТЕСТ"),
        ("EXIT_WARNING", "ПОПЕРЕДЖЕННЯ НА ВИХІД"),
        ("EXIT WARNING", "попередження на вихід"),
        ("Trend ignition", "ранній старт тренду"),
        ("trend ignition", "ранній старт тренду"),
        ("Trend continuation", "продовження тренду"),
        ("trend continuation", "продовження тренду"),
        ("Pullback continuation fast entry", "ранній вхід на відкаті"),
        ("pullback continuation fast entry", "ранній вхід на відкаті"),
        ("Pullback continuation", "відкат у тренді"),
        ("pullback continuation", "відкат у тренді"),
        ("Prime ICT location override", "сильна ICT-локація"),
        ("Prime ICT", "сильна ICT-локація"),
        ("prime ICT", "сильна ICT-локація"),
        ("Range compression breakout", "пробій після стискання діапазону"),
        ("range compression breakout", "пробій після стискання діапазону"),
        ("Range edge", "край діапазону"),
        ("range edge", "край діапазону"),
        ("Late impulse / chase", "пізній імпульс / доганяння"),
        ("late/chase", "пізній імпульс / доганяння"),
        ("no chase", "не доганяти"),
        ("No clean setup", "чистого сетапу немає"),
        ("no clean setup", "чистого сетапу немає"),
        ("trigger", "підтвердження"),
        ("Trigger", "Підтвердження"),
        ("WATCH", "ЧЕКАТИ"),
        ("BLOCK", "ВХОДУ НЕМАЄ"),
        ("ENTRY", "ВХІД"),
        ("LONG", "ЛОНГ"),
        ("SHORT", "ШОРТ"),
        ("NEUTRAL", "НЕЙТРАЛЬНО"),
        ("entry→TP1", "вхід→TP1"),
        ("Entry", "Вхід"),
        ("entry", "вхід"),
        ("follow-through", "продовження руху"),
        ("reset", "перебудови"),
        ("retest", "ретест"),
        ("chase", "доганяння"),
    ]

    for old, new in replacements:
        text = text.replace(old, new)
    return text


# ==========================================================
# OUTCOME ANALYTICS ENGINE
# ==========================================================
# Analytics only: this block records performance statistics and does NOT affect
# entries, stops, TP, supervision, alerts, or risk filters.
OUTCOME_ANALYTICS_LOCAL_TZ = os.getenv("OUTCOME_ANALYTICS_LOCAL_TZ", "Europe/Uzhgorod")
OUTCOME_ANALYTICS_LOOKBACK = int(os.getenv("OUTCOME_ANALYTICS_LOOKBACK", "120") or 120)


def _outcome_local_tz_safe():
    try:
        return ZoneInfo(OUTCOME_ANALYTICS_LOCAL_TZ)
    except Exception:
        return timezone.utc


def _outcome_parse_dt_safe(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _outcome_same_local_day(value, now=None):
    dt = _outcome_parse_dt_safe(value)
    if not dt:
        return False
    tz = _outcome_local_tz_safe()
    now = now or now_utc()
    return dt.astimezone(tz).date() == now.astimezone(tz).date()


def _note_value(notes, prefix):
    prefix = str(prefix)
    for item in notes or []:
        s = str(item)
        if s.startswith(prefix + ":"):
            return s.split(":", 1)[1].strip()
    return None


def _trade_result_pct(trade):
    return safe_float((trade or {}).get("result_pct"), 0.0) or 0.0


def _trade_leveraged_pct(trade):
    val = safe_float((trade or {}).get("leveraged_pct"), None)
    if val is not None:
        return val
    return _trade_result_pct(trade) * LEVERAGE


def _tp1_distance_pct_for_trade(trade):
    entry = safe_float((trade or {}).get("entry"), None)
    tp1 = safe_float((trade or {}).get("tp1"), None)
    side = str((trade or {}).get("side") or "").upper()
    if not entry or not tp1 or side not in ["LONG", "SHORT"]:
        return None
    raw = pct(tp1, entry)
    return raw if side == "LONG" else -raw


def _tp1_was_reached_proxy(trade):
    action = str((trade or {}).get("result_action") or "").upper()
    if action.startswith("TP"):
        return True
    mfe = safe_float((trade or {}).get("mfe_pct"), safe_float((trade or {}).get("best_pct"), 0.0)) or 0.0
    tp1_dist = _tp1_distance_pct_for_trade(trade)
    if tp1_dist is not None and tp1_dist > 0:
        return mfe >= tp1_dist * 0.96
    return mfe >= 0.75


def _analytics_bucket_stats(trades):
    trades = [t for t in trades or [] if isinstance(t, dict)]
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "expectancy_pct": 0.0,
            "total_pct": 0.0,
            "total_leveraged_pct": 0.0,
            "avg_mfe_pct": 0.0,
            "avg_mae_pct": 0.0,
            "avg_mfe_capture_pct": None,
            "tp1_proxy_rate": None,
            "stop_or_negative_rate": None,
        }

    results = [_trade_result_pct(t) for t in trades]
    wins = [x for x in results if x > 0]
    mfe_vals = [safe_float(t.get("mfe_pct"), safe_float(t.get("best_pct"), 0.0)) or 0.0 for t in trades]
    mae_vals = [safe_float(t.get("mae_pct"), 0.0) or 0.0 for t in trades]
    capture_vals = [safe_float(t.get("mfe_captured_pct"), None) for t in trades]
    capture_vals = [x for x in capture_vals if x is not None]
    tp1_hits = sum(1 for t in trades if _tp1_was_reached_proxy(t))
    stop_count = sum(
        1 for t in trades
        if "STOP" in str(t.get("result_action") or "").upper() or _trade_result_pct(t) < 0
    )

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "expectancy_pct": round(sum(results) / len(results), 3),
        "total_pct": round(sum(results), 3),
        "total_leveraged_pct": round(sum(_trade_leveraged_pct(t) for t in trades), 2),
        "avg_mfe_pct": round(sum(mfe_vals) / len(mfe_vals), 3) if mfe_vals else 0.0,
        "avg_mae_pct": round(sum(mae_vals) / len(mae_vals), 3) if mae_vals else 0.0,
        "avg_mfe_capture_pct": round(sum(capture_vals) / len(capture_vals), 1) if capture_vals else None,
        "tp1_proxy_rate": round(tp1_hits / len(trades) * 100, 1),
        "stop_or_negative_rate": round(stop_count / len(trades) * 100, 1),
    }


def _analytics_group_by(trades, field):
    grouped = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        key = t.get(field) or "UNKNOWN"
        if isinstance(key, dict):
            key = key.get("regime_type") or key.get("type") or key.get("name") or "UNKNOWN"
        key = str(key or "UNKNOWN")
        grouped.setdefault(key, []).append(t)
    return {k: _analytics_bucket_stats(v) for k, v in sorted(grouped.items())}


def _signal_key_from_setup(signal):
    setup = (signal or {}).get("setup_classifier") or {}
    if isinstance(setup, dict):
        return setup.get("type") or setup.get("label") or "UNKNOWN"
    return str(setup or "UNKNOWN")


def _signal_key_from_regime(signal):
    regime = (signal or {}).get("regime_engine") or {}
    if isinstance(regime, dict):
        return regime.get("regime_type") or regime.get("name") or "UNKNOWN"
    return str(regime or "UNKNOWN")


def compute_outcome_analytics(journal):
    """Build compact statistics from the existing journal.

    This engine is passive. It only writes analytics into the journal so later
    reviews can see which regimes, setups and entry levels actually worked.
    """
    journal = journal or {}
    trades = [t for t in list(journal.get("trades") or [])[-OUTCOME_ANALYTICS_LOOKBACK:] if isinstance(t, dict)]
    signals = [s for s in list(journal.get("signals") or [])[-OUTCOME_ANALYTICS_LOOKBACK:] if isinstance(s, dict)]
    today = [t for t in trades if _outcome_same_local_day(t.get("closed_at") or t.get("time"))]

    signal_counts = {
        "total": len(signals),
        "entries": sum(1 for s in signals if str(s.get("type") or "").upper() in ["ENTRY", "RISKY_ENTRY"]),
        "full_entries": sum(1 for s in signals if str(s.get("type") or "").upper() == "ENTRY"),
        "risky_entries": sum(1 for s in signals if str(s.get("type") or "").upper() == "RISKY_ENTRY"),
        "watch": sum(1 for s in signals if str(s.get("type") or "").upper() in ["WATCH", "WAIT_RETEST"]),
        "no_trade": sum(1 for s in signals if str(s.get("type") or "").upper() == "NO_TRADE"),
        "follow": sum(1 for s in signals if str(s.get("type") or "").upper() == "FOLLOW"),
        "closed": sum(1 for s in signals if str(s.get("type") or "").upper() == "CLOSE"),
        "pending_trigger_activations": sum(1 for s in signals if bool(s.get("pending_trigger_activated"))),
    }

    setup_signal_counts = {}
    regime_signal_counts = {}
    for s in signals:
        setup_key = str(_signal_key_from_setup(s) or "UNKNOWN")
        regime_key = str(_signal_key_from_regime(s) or "UNKNOWN")
        setup_signal_counts[setup_key] = setup_signal_counts.get(setup_key, 0) + 1
        regime_signal_counts[regime_key] = regime_signal_counts.get(regime_key, 0) + 1

    return {
        "updated_at": iso_now(),
        "lookback_limit": OUTCOME_ANALYTICS_LOOKBACK,
        "closed_trades_count": len(trades),
        "signals_count": len(signals),
        "overall": _analytics_bucket_stats(trades),
        "today": _analytics_bucket_stats(today),
        "by_side": _analytics_group_by(trades, "side"),
        "by_setup": _analytics_group_by(trades, "setup_type"),
        "by_regime": _analytics_group_by(trades, "regime_type"),
        "by_entry_level": _analytics_group_by(trades, "entry_level"),
        "by_exit_reason": _analytics_group_by(trades, "exit_reason_code"),
        "signals": signal_counts,
        "signal_counts_by_setup": dict(sorted(setup_signal_counts.items())),
        "signal_counts_by_regime": dict(sorted(regime_signal_counts.items())),
    }


def update_outcome_analytics(journal):
    if isinstance(journal, dict):
        journal["outcome_analytics"] = compute_outcome_analytics(journal)
    return journal

# ==========================================================
# MESSAGES
# ==========================================================


def _fmt_price(value):
    value = round_price(value)
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _side_label(side):
    value = str(side or "NEUTRAL").upper()
    if value == "LONG":
        return "ЛОНГ"
    if value == "SHORT":
        return "ШОРТ"
    return "НЕЙТРАЛЬНО"


def _bias_label(value):
    return _side_label(value)


def _score_line(label, block):
    block = block or {}
    return f"<b>{label}:</b> {_bias_label(block.get('bias'))} ({block.get('score', 0)})"


def _short_list(items, limit=3):
    out = []
    for item in items or []:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def price_line(context):
    # Current price is refreshed immediately before the signal is built.
    line = f"<b>Ціна:</b> {_fmt_price(context.get('price'))}"
    if context.get("price_status") == "updated_before_signal" and context.get("price_before_refresh") is not None:
        line += f" <i>(оновлено з {_fmt_price(context.get('price_before_refresh'))})</i>"
    return line


def context_lines(context):
    # User requested to remove the detailed dashboard block from Telegram messages.
    # Keep all calculations inside the bot, but do not print 4H/1H/15M/3M/ICT/CVD/etc.
    return []

def plan_text(plan, multiline=False):
    if not plan:
        return "плану немає"
    if multiline:
        return "\n".join([
            f"<b>Вхід:</b> {_fmt_price(plan.entry)}",
            f"<b>Стоп:</b> {_fmt_price(plan.stop)}",
            f"<b>TP1:</b> {_fmt_price(plan.tp1)}",
            f"<b>TP2:</b> {_fmt_price(plan.tp2)}",
            f"<b>TP3:</b> {_fmt_price(plan.tp3)}",
            f"<b>Ризик:</b> {plan.risk_pct}% | <b>RR:</b> {plan.rr1}/{plan.rr2}/{plan.rr3}",
        ])
    return (
        f"Вхід {_fmt_price(plan.entry)} | Стоп {_fmt_price(plan.stop)} | "
        f"TP1 {_fmt_price(plan.tp1)} | TP2 {_fmt_price(plan.tp2)} | TP3 {_fmt_price(plan.tp3)} | "
        f"ризик {plan.risk_pct}% | RR {plan.rr1}/{plan.rr2}/{plan.rr3}"
    )



def _side_icon(side):
    return "🟢" if side == "LONG" else "🔴"


def _zone_line(zone):
    if not zone:
        return ""
    low = zone.get("low")
    high = zone.get("high")
    ztype = zone.get("type") or "зона"
    if low is None or high is None:
        return ""
    return f"{_fmt_price(low)}–{_fmt_price(high)} ({ztype})"


def planned_wait_text(context, setup):
    """Human-readable waiting plan for WATCH / WAIT_RETEST states.

    The goal is to avoid vague messages like "чекати LONG". The bot should
    tell the user what exactly confirms the idea and from what price the trade
    becomes active.
    """
    side = setup.get("side")
    plan = setup.get("plan")
    price = safe_float(context.get("price"))
    atr15 = safe_float(context.get("atr15"), (price or 90) * 0.006) or ((price or 90) * 0.006)
    if side not in ["LONG", "SHORT"] or not price or not plan:
        return ""

    structure = context.get("structure") or {}
    ict = context.get("ict") or {}
    tf3 = context.get("tf3") or {}
    tf15 = context.get("tf15") or {}
    flow = context.get("flow") or {}
    cvd = context.get("cvd") or {}
    pending = context.get("pending_ict_zones") or []
    same_zones = [z for z in pending if z.get("side") == side]
    best_zone = same_zones[0] if same_zones else None

    recent_high = safe_float(structure.get("recent_high"))
    recent_low = safe_float(structure.get("recent_low"))
    swing_high = safe_float(structure.get("swing_high"))
    swing_low = safe_float(structure.get("swing_low"))

    if side == "LONG":
        # Prefer a nearby reclaim/trigger, not a far 15m swing if it is too stretched.
        candidates = [price + atr15 * 0.16]
        for lvl in [recent_high, swing_high, safe_float(ict.get("equilibrium"))]:
            if lvl and lvl > price and (lvl - price) / price * 100 <= 1.15:
                candidates.append(lvl + atr15 * 0.04)
        trigger = min(candidates) if candidates else price + atr15 * 0.16
        entry_low = trigger
        entry_high = trigger + atr15 * 0.22
        retest_zone = _zone_line(best_zone)
        if not retest_zone:
            support = max(safe_float(getattr(plan, "stop", None), price - atr15), price - atr15 * 0.55)
            retest_zone = f"{_fmt_price(support)}–{_fmt_price(price)}"
        wait_items = [
            f"3M reclaim вище {_fmt_price(trigger)} і закриття/утримання над рівнем",
            "після reclaim — 3M higher low, без нового lower low",
            f"або ретест зони {retest_zone} з викупом",
        ]
        activation = f"якщо 3M закриється вище {_fmt_price(trigger)} або дасть retest→reclaim цієї зони"
        cancel = f"якщо 3M/15M закриється нижче {_fmt_price(plan.stop)} або CVD/потік різко стане проти LONG"
    else:
        candidates = [price - atr15 * 0.16]
        for lvl in [recent_low, swing_low, safe_float(ict.get("equilibrium"))]:
            if lvl and lvl < price and (price - lvl) / price * 100 <= 1.15:
                candidates.append(lvl - atr15 * 0.04)
        trigger = max(candidates) if candidates else price - atr15 * 0.16
        entry_high = trigger
        entry_low = trigger - atr15 * 0.22
        retest_zone = _zone_line(best_zone)
        if not retest_zone:
            resistance = min(safe_float(getattr(plan, "stop", None), price + atr15), price + atr15 * 0.55)
            retest_zone = f"{_fmt_price(price)}–{_fmt_price(resistance)}"
        wait_items = [
            f"3M rejection/пробій нижче {_fmt_price(trigger)} і утримання під рівнем",
            "після пробою — 3M lower high, без нового higher high",
            f"або ретест зони {retest_zone} з відбоєм вниз",
        ]
        activation = f"якщо 3M закриється нижче {_fmt_price(trigger)} або дасть retest→rejection цієї зони"
        cancel = f"якщо 3M/15M закриється вище {_fmt_price(plan.stop)} або CVD/потік різко стане проти SHORT"

    why = []
    if tf3.get("bias") == side:
        why.append("3M вже показує напрям")
    elif tf3.get("bias") == "NEUTRAL":
        why.append("3M ще без повного тригера")
    else:
        why.append("3M поки дає відкат — потрібен розворот назад")

    if ict.get("bias") == side:
        why.append("ICT контекст підтримує ідею")
    if tf15.get("bias") == side:
        why.append("15M підтримує напрям")
    elif tf15.get("bias") == "NEUTRAL":
        why.append("15M ще нейтральний, тому вхід тільки по 3M")
    if flow.get("bias") == side or cvd.get("bias") == side:
        why.append("потік/CVD не проти входу")

    lines = []
    if why:
        lines.append("<b>Чому готується:</b>")
        lines.extend([f"✅ {x}" for x in why[:4]])
    # User requested to remove all "Що чекаємо" / activation blocks from Telegram alerts.
    # The bot still calculates wait_items/activation internally, but does not print them.
    return localize_user_text("\n".join(lines).strip())

def _compact_title(setup):
    action = setup.get("action")
    side = setup.get("side")
    side_text = _side_label(side)
    level = setup.get("entry_level")
    setup_type = str(((setup.get("setup_classifier") or {}).get("type") or "")).upper()
    if setup_type == "NO_CLEAN_SETUP":
        return "⚪ ВХОДУ НЕМАЄ — ЧИСТОГО СЕТАПУ НЕМАЄ"
    if level == "BLOCK" and side in ["LONG", "SHORT"]:
        return f"🔴 ВХОДУ НЕМАЄ — {side_text} НЕ ПІДТВЕРДЖЕНИЙ"
    if level == "BLOCK":
        return "🔴 ВХОДУ НЕМАЄ"
    if action == "ENTRY":
        return f"🟢 ВХІД {side_text}"
    if action == "RISKY_ENTRY":
        return f"🟠 РИЗИКОВАНИЙ ВХІД {side_text}"
    if action == "WAIT_RETEST":
        return f"🟡 НЕ ДОГАНЯТИ {side_text}"
    if action == "WATCH" and setup.get("exhausted_move") and side in ["LONG", "SHORT"]:
        return f"🟡 ЧЕКАТИ — {side_text} ВЖЕ ВІДІГРАНИЙ"
    if action == "WATCH" and side in ["LONG", "SHORT"]:
        return f"🟡 ЧЕКАТИ — {side_text} ГОТУЄТЬСЯ"
    if action == "NO_TRADE":
        return "⚪ " + str(setup.get("title") or "ЧЕКАТИ — ВХОДУ НЕМАЄ")
    return str(setup.get("title") or "СИГНАЛ")


def entry_type_text(context, setup):
    """Human-readable setup classifier for Telegram.

    The old line only said trend/countertrend. Now the bot shows the actual
    Setup Classifier result: continuation, pullback, sweep, range edge, chase, etc.
    """
    side = (setup or {}).get("side")
    if side not in ["LONG", "SHORT"]:
        return ""

    if (setup or {}).get("lock_release_bridge"):
        saved_label = (setup or {}).get("lock_release_bridge_saved_setup_label") or ((setup or {}).get("setup_classifier") or {}).get("label") or "збережений професійний сетап"
        saved_score = int((setup or {}).get("lock_release_bridge_saved_setup_score", 0) or ((setup or {}).get("setup_classifier") or {}).get("score", 0) or 0)
        return f"<b>Збережений сетап:</b> {html.escape(str(saved_label))} | {saved_score}/100"

    setup_info = (setup or {}).get("setup_classifier") or {}
    if str(setup_info.get("type") or "").upper() == "NO_CLEAN_SETUP":
        return ""
    if not setup_info or str(setup_info.get("side") or "").upper() != side:
        return "<b>Сетап:</b> ⚪ класифікатор сторони не підтверджений"

    line = setup_classifier_text(setup_info)
    # User requested to remove the public "Правило" line from Telegram alerts.
    # Internal entry_rule remains in setup_classifier for the engine/journal,
    # but it is not printed in notifications.
    return line


def _compact_notification_text(value, limit=320):
    text = " ".join(str(value or "").split()).strip(" ;")
    if not text:
        return ""
    if len(text) <= limit:
        return text
    shortened = text[: max(1, limit - 1)].rsplit(" ", 1)[0].rstrip(" ,;:")
    return shortened + "…"


def _notification_text_key(value):
    text = _compact_notification_text(value, 1000).lower()
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()


def _wait_reason_items(setup, limit=3):
    """Return the clearest non-duplicated reasons why an entry is not taken."""
    setup = setup or {}
    plan = setup.get("plan") or {}
    classifier = setup.get("setup_classifier") or {}
    plan_validation_reason = plan.get("validation_reason") if isinstance(plan, dict) else getattr(plan, "validation_reason", "")
    classifier_reason = classifier.get("reason") if isinstance(classifier, dict) else getattr(classifier, "reason", "")
    candidates = [
        setup.get("reason"),
        plan_validation_reason,
        *(setup.get("conflicts") or []),
        classifier_reason,
    ]
    result = []
    keys = []
    for raw in candidates:
        item = _compact_notification_text(raw, 240)
        key = _notification_text_key(item)
        if not key:
            continue
        # Do not repeat the same reason in slightly different wording.
        if any(key == old or key in old or old in key for old in keys):
            continue
        result.append(item)
        keys.append(key)
        if len(result) >= limit:
            break
    return result


def _follow_situation_icon(result, phase):
    action = str((result or {}).get("action") or "").upper()
    status = str((phase or {}).get("status") or "").upper()
    if (result or {}).get("closed") or action.startswith("EXIT") or action == "STOP" or status == "EDGE_LOST":
        return "🔴"
    if action in {"PROTECT", "PROTECT_OR_EXIT", "EXIT_WARNING", "UNDER_PRESSURE"} or "WEAK" in status:
        return "🟠"
    if action.startswith("TP") or status in {"EXPANSION_CONFIRMED", "PROFIT_CONTROLLED", "CONTINUATION_VALID", "TARGET_PROTECTION"}:
        return "🟢"
    return "🟡"


def _distinct_notification_text(first, second):
    a = _notification_text_key(first)
    b = _notification_text_key(second)
    if not a or not b:
        return bool(b)
    return not (a == b or a in b or b in a)


def build_new_setup_message(context, setup):
    plan = setup.get("plan")
    conflicts = _short_list(setup.get("conflicts"), 3)
    confirmations = _short_list(setup.get("confirmations"), 3)

    if setup.get("entry_level") == "BLOCK":
        quality_label = "Оцінка"
    elif setup.get("entry_level") == "WATCH_TRIGGER":
        quality_label = "Готовність"
    else:
        quality_label = "Якість"
    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{_compact_title(setup)}</b>",
        "",
        price_line(context),
        f"<b>{quality_label}:</b> {setup['quality']}/100",
    ]

    if setup.get("entry_level_label"):
        lines.append(f"<b>Рівень входу:</b> {html.escape(str(setup.get('entry_level_label')))}")

    entry_type = entry_type_text(context, setup)
    if entry_type:
        lines.append(entry_type)

    if setup.get("action") in ["ENTRY", "RISKY_ENTRY"]:
        # For RISKY_ENTRY keep the alert compact: no "why entry is allowed" block
        # and no supervision/control block, because supervision comes in the next messages.
        if confirmations and setup.get("action") == "ENTRY":
            lines.append("")
            lines.append("<b>Підтвердження:</b>")
            lines.extend([f"✅ {x}" for x in confirmations])
        if conflicts:
            lines.append("")
            lines.append("<b>Ризики:</b>")
            lines.extend([f"⚠️ {x}" for x in conflicts])
        lines.append("")
        lines.append("<b>План:</b>")
        lines.append(plan_text(plan, multiline=True))
    else:
        reason_items = _wait_reason_items(setup, 3)
        if reason_items:
            lines.append("")
            lines.append("<b>Чому очікуємо, а не заходимо:</b>")
            for x in reason_items:
                lower = x.lower()
                icon = "⚠️" if any(key in lower for key in ["локаль", "4h", "1h", "потік", "cvd", "ризик", "новин", "очіку"] ) else "❌"
                lines.append(f"{icon} {html.escape(x)}")
        # WAIT/ЧЕКАТИ alerts stay compact: no activation and no tentative
        # entry/stop/TP plan until a real ENTRY/RISKY_ENTRY appears.

    lines.append("")
    lines.extend(context_lines(context))
    return localize_user_text("\n".join(lines).strip())


def _stop_changed(old_stop, new_stop):
    old_stop = safe_float(old_stop, None)
    new_stop = safe_float(new_stop, None)
    if old_stop is None or new_stop is None:
        return False
    return abs(new_stop - old_stop) > max(0.0001, abs(old_stop) * 0.00005)


def _latest_stop_reason_from_notes(notes):
    """Extract a compact stop-update reason from management notes for Telegram."""
    for item in reversed(list(notes or [])):
        text = str(item or "")
        lower = text.lower()
        if any(key in lower for key in ["stop", "стоп", "tp-noise", "mfe", "protect"]):
            return text[:180]
    return "стоп оновлено за поточним супроводом"


def _stop_update_alert_lines(trade, result):
    """Highlighted Telegram block shown only when the active stop changed this run."""
    result = result or {}
    if not result.get("stop_changed"):
        return []

    old_stop = safe_float(result.get("previous_stop"), None)
    new_stop = safe_float(result.get("new_stop") or result.get("recommended_stop") or getattr(trade, "stop_current", None), None)
    if new_stop is None:
        return []

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔒 <b>Новий стоп:</b> {_fmt_price(new_stop)}",
    ]
    if old_stop is not None:
        lines.append(f"↪️ <b>Було:</b> {_fmt_price(old_stop)} → <b>Стало:</b> {_fmt_price(new_stop)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return lines

def build_follow_message(context, trade, result):
    current_pct = safe_float((result or {}).get("current_pct"), 0.0) or 0.0
    best_pct = safe_float((result or {}).get("best_pct"), 0.0) or 0.0
    giveback = max(0.0, best_pct - current_pct)
    high_since_open, low_since_open = _trade_extremes_since_open(trade, context)
    phase = (result or {}).get("trade_phase") or analyze_trade_phase(
        trade, context, current_pct, best_pct, giveback,
        high_since_open=high_since_open,
        low_since_open=low_since_open,
        entry_state=(result or {}).get("entry_state"),
        trade_validation=(result or {}).get("trade_validation"),
        action=(result or {}).get("action"),
        closed=bool((result or {}).get("closed")),
        exit_reason_code=(result or {}).get("exit_reason_code"),
    )
    title = phase.get("telegram_title") or (result or {}).get("title") or f"СУПРОВІД {trade.side}"
    phase_reason = phase.get("telegram_reason") or ""
    recommendation = (result or {}).get("recommendation") or ""
    situation_reason = phase_reason or recommendation or "супровід оновлено за поточним контекстом."
    mae_pct = safe_float(phase.get("mae_pct"), 0.0) or 0.0
    close_mfe = safe_float(phase.get("close_mfe_pct"), 0.0) or 0.0

    lines = [
        "<b>BZU SIGNAL BOT PRO</b>",
        "",
        f"<b>{html.escape(str(title))}</b>",
        "",
        price_line(context),
        f"<b>Від входу:</b> {round(current_pct, 3)}% | <b>Макс. прибуток:</b> {round(best_pct, 3)}% | <b>Макс. просадка:</b> {round(mae_pct, 3)}%",
        "",
        "<b>Ситуація з угодою:</b>",
        f"{_follow_situation_icon(result, phase)} {html.escape(_compact_notification_text(situation_reason, 360))}",
    ]
    # Close/confirmed MFE is used by the engine internally, but not printed by
    # default to keep Telegram clean and fast to read.

    # If the stop was updated in this run, show only the old and new stop values.
    # This block is intentionally visual and placed above risk/position details.
    lines.extend(_stop_update_alert_lines(trade, result))

    if result.get("reversal_label"):
        lines.append("")
        rev_side = result.get("reversal_side") or opposite(trade.side)
        lines.append(f"<b>Ризик розвороту в {rev_side}:</b> {result.get('reversal_label')} ({int(result.get('reversal_score') or 0)}%)")

    lines.extend([
        "",
        "<b>Позиція:</b>",
        f"Вхід {_fmt_price(trade.entry)} | Стоп {_fmt_price(trade.stop_current)}",
        f"TP1 {_fmt_price(trade.tp1)} | TP2 {_fmt_price(trade.tp2)} | TP3 {_fmt_price(trade.tp3)}",
        f"TP1 {'✅' if trade.tp1_hit else '—'} | TP2 {'✅' if trade.tp2_hit else '—'} | TP3 {'✅' if trade.tp3_hit else '—'}",
    ])

    # Show stop update only if it really changed from the initial stop; do not
    # repeat a "control stop" when it is identical to the entry stop.
    rec_stop = result.get("recommended_stop")
    if rec_stop is not None:
        rec_stop = safe_float(rec_stop, None)
        stop_initial = safe_float(getattr(trade, "stop_initial", None), None)
        if rec_stop is not None and stop_initial is not None and abs(rec_stop - stop_initial) > max(0.0001, abs(stop_initial) * 0.00005):
            if not (result or {}).get("stop_changed"):
                lines.append(f"🔒 <b>Активний стоп:</b> {_fmt_price(rec_stop)}")

    return localize_user_text("\n".join(lines).strip())

def build_closed_trade_journal_item(trade, result, context):
    """Build a closed-trade journal row with MFE analytics.

    best_pct is kept for backward compatibility, but the same value is also
    stored as mfe_pct so later analysis can directly answer:
    - how much profit the trade had at its best moment;
    - how much of that MFE was captured at exit;
    - how much profit was given back before closing.
    """
    actual_pct = round(safe_float(result.get("current_pct"), 0.0) or 0.0, 3)
    mfe_pct = round(safe_float(result.get("best_pct"), 0.0) or 0.0, 3)
    mfe_captured_pct = round(actual_pct / mfe_pct * 100, 1) if mfe_pct > 0.1 else None
    mfe_giveback_pct = round(mfe_pct - actual_pct, 3) if mfe_pct > 0.1 else None

    notes = list(getattr(trade, "notes", []) or [])
    entry_level = str(getattr(trade, "entry_level", "") or _note_value(notes, "ENTRY_LEVEL") or "")
    setup_type = str(getattr(trade, "setup_type", "") or _note_value(notes, "SETUP_CLASSIFIER") or "")
    regime_type = str(getattr(trade, "regime_type", "") or _note_value(notes, "REGIME_TYPE") or "")

    return {
        "id": trade.id,
        "opened_at": trade.opened_at,
        "closed_at": iso_now(),
        "side": trade.side,
        "entry_level": entry_level,
        "setup_type": setup_type,
        "regime_type": regime_type,
        "entry": trade.entry,
        "close_price": round_price(result.get("exit_price") or context["price"]),
        "market_close_price": round_price(context["price"]),
        "stop_initial": trade.stop_initial,
        "stop_final": round_price(trade.stop_current),
        "tp1": trade.tp1,
        "tp2": trade.tp2,
        "tp3": trade.tp3,
        "quality": trade.quality,
        "result_action": result["action"],
        "result_pct": actual_pct,
        "leveraged_pct": round(actual_pct * LEVERAGE, 2),
        "best_pct": mfe_pct,
        "mfe_pct": mfe_pct,
        "mfe_captured_pct": mfe_captured_pct,
        "mfe_giveback_pct": mfe_giveback_pct,
        "trade_phase": (result.get("trade_phase") or {}).get("phase"),
        "phase_status": (result.get("trade_phase") or {}).get("status"),
        "mae_pct": (result.get("trade_phase") or {}).get("mae_pct"),
        "confirmed_mfe_pct": (result.get("trade_phase") or {}).get("confirmed_mfe_pct"),
        "exit_reason_code": result.get("exit_reason_code") or _exit_reason_code(result, context, trade),
        "exit_quality": result.get("exit_quality") or _exit_quality_score(trade, result, context),
        "exit_score": result.get("exit_score"),
        "missed_continuation_check": "pending_next_runs",
        "notes": result.get("notes", []),
    }


def send_telegram(message):
    message = localize_user_text(message)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram token/chat missing. Message:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        print(f"Telegram status: {response.status_code}")
        print(response.text[:300])
    except Exception as error:
        print(f"[WARN] Telegram send failed: {error}")


def update_market_snapshot(state, context):
    derivatives = context.get("derivatives") or {}
    cvd = context.get("cvd") or {}
    state["last_market_snapshot"] = {
        "time": iso_now(),
        "price": round_price(context.get("price")),
        "oi": derivatives.get("oi"),
        "cvd": cvd.get("cvd"),
        "cvd_time": iso_now(),
        "last_trade_ts": cvd.get("last_trade_ts"),
        "bias": context.get("bias"),
        "tech_score": context.get("tech_score"),
        "total_score": context.get("total_score"),
    }


# ==========================================================
# SINGLE-FILE ACTIVE-TRADE INVARIANT WRAPPER V3
# ==========================================================


def manage_active_trade_v3(trade, context):
    """Run the legacy/proven manager once, then enforce non-conflicting invariants."""
    before_side = str(getattr(trade, "side", "") or "")
    before_stop = safe_float(getattr(trade, "stop_current", None), None)
    try:
        result = manage_active_trade(trade, context)
    except Exception as error:
        current_pct = signed_pct(before_side, safe_float(getattr(trade, "entry", 0), 0), safe_float((context or {}).get("price"), 0))
        return {
            "action": "HOLD",
            "title": f"СУПРОВІД {before_side}",
            "recommendation": "модуль супроводу не завершив перевірку; позиція не замінена новим сигналом, потрібен ручний контроль",
            "closed": False,
            "current_pct": current_pct,
            "best_pct": max(0.0, signed_pct(before_side, safe_float(getattr(trade, "entry", 0), 0), safe_float(getattr(trade, "best_price", getattr(trade, "entry", 0)), 0))),
            "notes": [f"MANAGEMENT_SAFE_MODE: {str(error)[:240]}"],
            "architecture_audit": {"version": ARCHITECTURE_VERSION, "safe_mode": True},
        }

    if str(getattr(trade, "side", "") or "") != before_side:
        trade.side = before_side
        result.setdefault("notes", []).append("SIDE_MUTATION_PREVENTED")

    after_stop = safe_float(getattr(trade, "stop_current", None), before_stop)
    stop_restored = False
    if before_stop is not None and after_stop is not None:
        if before_side == "LONG" and after_stop < before_stop - max(0.0001, abs(before_stop) * 0.00001):
            trade.stop_current = before_stop
            stop_restored = True
        elif before_side == "SHORT" and after_stop > before_stop + max(0.0001, abs(before_stop) * 0.00001):
            trade.stop_current = before_stop
            stop_restored = True
    if stop_restored:
        result["stop_changed"] = False
        result["recommended_stop"] = before_stop
        result.setdefault("notes", []).append("STOP_LOOSENING_PREVENTED")

    result.setdefault("action", "HOLD")
    result.setdefault("closed", False)
    result.setdefault("current_pct", signed_pct(before_side, trade.entry, safe_float((context or {}).get("price"), trade.entry)))
    result.setdefault("best_pct", max(0.0, safe_float(result.get("current_pct"), 0.0) or 0.0))
    result["architecture_audit"] = {
        "version": ARCHITECTURE_VERSION,
        "side_immutable": str(getattr(trade, "side", "") or "") == before_side,
        "stop_never_loosened": not stop_restored,
        "active_trade_never_replaced_by_watch": True,
    }
    return result

# ==========================================================
# MAIN
# ==========================================================


def main():
    print("START BZU SIGNAL BOT PRO")
    state = load_state()
    journal = load_journal()

    data = collect_market_data()
    context = build_context(data, state)
    context = refresh_context_price(context)
    if not context.get("price"):
        print("NO PRICE DATA")
        return

    print(f"PRICE {context['price']} | BIAS {context['bias']} | TECH {context['tech_score']} | TOTAL {context['total_score']}")
    print(f"4h {context['tf4h'].get('bias')} {context['tf4h'].get('score')} | 15m {context['tf15'].get('bias')} {context['tf15'].get('score')} | 3m {context['tf3'].get('bias')} {context['tf3'].get('score')}")
    print(f"STRUCTURE {context['structure'].get('phase')} {context['structure'].get('bias')} | CVD {context['cvd'].get('bias')} | CLUSTERS {context['clusters'].get('bias')} | OI {context['derivatives'].get('bias')} | NEWS {context['news'].get('bias')}")

    active = active_trade_from_state(state)
    if active and active.status != "CLOSED":
        update_opportunity_coverage(state, context, active_trade=active)
        state["opportunity_memory"] = None
        result = manage_active_trade_v3(active, context)
        message = build_follow_message(context, active, result)
        if result.get("closed"):
            journal["trades"].append(build_closed_trade_journal_item(active, result, context))
            closed_mfe_pct = round(safe_float(result.get("best_pct"), 0.0) or 0.0, 3)
            closed_result_pct = round(safe_float(result.get("current_pct"), 0.0) or 0.0, 3)
            append_history(state, {
                "type": "TRADE_CLOSED",
                "side": active.side,
                "action": result["action"],
                "price": round_price(context["price"]),
                "result_pct": closed_result_pct,
                "mfe_pct": closed_mfe_pct,
                "mfe_captured_pct": round(closed_result_pct / closed_mfe_pct * 100, 1) if closed_mfe_pct > 0.1 else None,
                "mfe_giveback_pct": round(closed_mfe_pct - closed_result_pct, 3) if closed_mfe_pct > 0.1 else None,
            })
            state["last_closed_trade"] = {
                "side": active.side, "closed_at": iso_now(),
                "entry": round_price(getattr(active, "entry", None)),
                "stop_initial": round_price(getattr(active, "stop_initial", None)),
                "close_price": round_price(result.get("exit_price") or context.get("price")),
                "result_pct": closed_result_pct, "action": result.get("action"),
                "entry_quality": int(getattr(active, "quality", 0) or 0),
                "tp1_hit": bool(active.tp1_hit), "tp2_hit": bool(active.tp2_hit), "tp3_hit": bool(active.tp3_hit),
                "setup_type": str(getattr(active, "setup_type", "") or ""),
                "failure_context": {
                    "price": round_price(context.get("price")),
                    "regime_type": str(((context.get("regime_engine") or {}).get("regime_type") or "")),
                    "structure_phase": str((context.get("structure") or {}).get("phase") or ""),
                    "structure_bias": (context.get("structure") or {}).get("bias"),
                    "ict_setup": str((context.get("ict") or {}).get("setup") or ""),
                    "ict_bias": (context.get("ict") or {}).get("bias"),
                    "tf3_bias": (context.get("tf3") or {}).get("bias"),
                    "tf3_score": int((context.get("tf3") or {}).get("score", 0) or 0),
                    "tf15_bias": (context.get("tf15") or {}).get("bias"),
                    "tf15_score": int((context.get("tf15") or {}).get("score", 0) or 0),
                    "flow_bias": (context.get("flow") or {}).get("bias"),
                    "cvd_bias": (context.get("cvd") or {}).get("bias"),
                    "entry_location": context.get("entry_location"),
                },
            }
            if active.tp2_hit or active.tp3_hit or str(result.get("action")) == "TP3":
                structure_now = context.get("structure") or {}
                state["structural_reset_gate"] = {
                    "active": True, "side": active.side, "closed_at": iso_now(),
                    "close_price": round_price(result.get("exit_price") or context.get("price")),
                    "baseline_recent_high": round_price(structure_now.get("recent_high")),
                    "baseline_recent_low": round_price(structure_now.get("recent_low")),
                    "source_action": result.get("action"),
                }
            store_active_trade(state, None)
        else:
            store_active_trade(state, active)
            append_history(state, {
                "type": "FOLLOW",
                "side": active.side,
                "action": result["action"],
                "price": round_price(context["price"]),
                "result_pct": round(result["current_pct"], 3),
                "stop_current": round_price(active.stop_current),
            })
        journal["signals"].append({
            "time": iso_now(),
            "type": "FOLLOW" if not result.get("closed") else "CLOSE",
            "side": active.side,
            "action": result["action"],
            "price": round_price(context["price"]),
            "quality": active.quality,
            "entry_level": _note_value(getattr(active, "notes", []), "ENTRY_LEVEL"),
            "setup_type": _note_value(getattr(active, "notes", []), "SETUP_CLASSIFIER"),
            "regime_type": _note_value(getattr(active, "notes", []), "REGIME_TYPE"),
            "context_bias": context["bias"],
            "tech_score": context["tech_score"],
            "total_score": context["total_score"],
            "regime_engine": context.get("regime_engine") or context.get("market_regime"),
        })
        state["pending_trigger"] = None
        update_market_snapshot(state, context)
        save_state(state)
        save_journal(journal)
        send_telegram(message)
        print("BOT COMPLETE: ACTIVE TRADE MANAGED")
        return

    setup = evaluate_new_setup(context)
    if setup.get("side") in ["LONG", "SHORT"] and isinstance(setup.get("setup_classifier"), dict):
        # Decision is immutable after evaluate_new_setup: only mirror the already
        # selected classifier into context for presentation/journal compatibility.
        context["setup_classifier"] = setup.get("setup_classifier")
    update_lock_release_opportunity_bridge_memory(state, setup, context)
    update_opportunity_memory(state, setup, context)
    update_pending_trigger_memory(state, setup, context)
    update_opportunity_coverage(state, context, setup=setup)
    setup = _v3_verify_post_decision_integrity(setup)
    message = build_new_setup_message(context, setup)

    if setup["action"] in ["ENTRY", "RISKY_ENTRY"]:
        state["opportunity_memory"] = None
        active = new_active_trade(setup)
        store_active_trade(state, active)
        append_history(state, {
            "type": "ENTRY",
            "side": setup["side"],
            "action": setup["action"],
            "price": round_price(context["price"]),
            "quality": setup["quality"],
            "setup_classifier": setup.get("setup_classifier"),
            "regime_engine": setup.get("regime_engine") or context.get("regime_engine"),
            "entry": active.entry,
            "stop": active.stop_current,
            "tp1": active.tp1,
            "tp2": active.tp2,
            "tp3": active.tp3,
        })
    else:
        store_active_trade(state, None)
        append_history(state, {
            "type": setup["action"],
            "side": setup["side"],
            "price": round_price(context["price"]),
            "quality": setup["quality"],
            "reason": setup["reason"],
            "market_direction_context": setup.get("market_direction_context"),
            "setup_classifier": setup.get("setup_classifier"),
            "regime_engine": setup.get("regime_engine") or context.get("regime_engine"),
        })

    journal["signals"].append({
        "time": iso_now(),
        "type": setup["action"],
        "side": setup["side"],
        "price": round_price(context["price"]),
        "quality": setup["quality"],
        "reason": setup["reason"],
        "entry_level": setup.get("entry_level"),
        "pending_trigger_activated": bool(setup.get("pending_trigger_activated")),
        "lock_release_bridge": bool(setup.get("lock_release_bridge")),
        "lock_release_bridge_activated": bool(setup.get("lock_release_bridge_activated")),
        "lock_release_bridge_status": setup.get("lock_release_bridge_status"),
        "lock_release_bridge_saved_setup_score": setup.get("lock_release_bridge_saved_setup_score"),
        "lock_release_bridge_saved_setup_label": setup.get("lock_release_bridge_saved_setup_label"),
        "lock_release_bridge_current_readiness": setup.get("lock_release_bridge_current_readiness"),
        "plan_rebuild_required": bool(setup.get("plan_rebuild_required")),
        "plan_rebuild_status": setup.get("plan_rebuild_status"),
        "architecture_version": setup.get("architecture_version"),
        "decision_id": setup.get("decision_id"),
        "decision_fingerprint": setup.get("decision_fingerprint"),
        "candidate_source": setup.get("candidate_source"),
        "current_readiness": setup.get("current_readiness"),
        "selected_setup_score": setup.get("selected_setup_score"),
        "market_direction_context": setup.get("market_direction_context"),
        "reason_codes": setup.get("reason_codes", []),
        "decision_pipeline_v3": setup.get("decision_pipeline_v3"),
        "setup_classifier": setup.get("setup_classifier"),
        "regime_engine": setup.get("regime_engine") or context.get("regime_engine"),
        "plan": asdict(setup["plan"]) if setup.get("plan") else None,
        "confirmations": setup.get("confirmations", []),
        "conflicts": setup.get("conflicts", []),
        "context": {
            "bias": context["bias"],
            "tech_score": context["tech_score"],
            "total_score": context["total_score"],
            "tf3": context["tf3"],
            "tf15": context["tf15"],
            "tf1h": context["tf1h"],
            "tf4h": context["tf4h"],
            "structure": context["structure"],
            "flow": context["flow"],
            "cvd": context["cvd"],
            "clusters": context["clusters"],
            "derivatives": context["derivatives"],
            "liquidity": context["liquidity"],
            "news": {
                "bias": context["news"]["bias"],
                "score": context["news"]["score"],
                "blocking_bias": context["news"].get("blocking_bias"),
                "blocking_score": context["news"].get("blocking_score"),
                "hard_block_active": context["news"].get("hard_block_active"),
                "technical_priority": context["news"].get("technical_priority"),
                "top": context["news"]["top"],
                "top_age_min": context["news"].get("top_age_min"),
                "top_lifecycle": context["news"].get("top_lifecycle"),
                "top_lifecycle_label": context["news"].get("top_lifecycle_label"),
                "block_reason": context["news"].get("block_reason"),
                "important": context["news"].get("important", [])[:3],
                "total": context["news"]["total"],
            },
            "calendar": context["calendar"],
            "reentry_review": context.get("reentry_review"),
            "reentry_cooldown": context.get("reentry_review") or context.get("reentry_cooldown"),  # compatibility alias
            "persistent_exhaustion_locks": context.get("persistent_exhaustion_locks") or (state.get("persistent_exhaustion_locks") if isinstance(state, dict) else None),
            "shock_lock": state.get("shock_lock") if isinstance(state, dict) else None,
            "setup_classifier": context.get("setup_classifier"),
            "regime_engine": context.get("regime_engine") or context.get("market_regime"),
        },
    })

    update_market_snapshot(state, context)
    save_state(state)
    save_journal(journal)
    send_telegram(message)
    print("BOT COMPLETE")


if __name__ == "__main__":
    main()
