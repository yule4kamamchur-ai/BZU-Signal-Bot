"""Canonical setup families and cross-setup market-episode deduplication.

The old bot used several different family labels for scoring, calibration, and
Router context.  This module provides one execution/research identity for all
24 named setups.  It does not lower a threshold or create trade authority.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


SCHEMA_VERSION = "canonical_setup_families_v9.5.59"

# Six economically distinct families.  Every named setup appears exactly once.
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
if len(CANONICAL_SETUP_FAMILY_MAP) != 24 or len(CANONICAL_FAMILIES) != 6:
    raise RuntimeError("v9.5.59 canonical family registry must cover 24 setups in six families")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def canonical_setup_family(setup_type: Any) -> str:
    """Return the one research/execution family for a named setup."""
    return CANONICAL_SETUP_FAMILY_MAP.get(str(setup_type or "").upper(), "UNKNOWN")


def _candidate_anchor(candidate: Any, context: dict[str, Any]) -> float:
    components = getattr(candidate, "score_components", {}) or {}
    contract = components.get("entry_contract") or {}
    values = (
        getattr(candidate, "execution_anchor", 0.0),
        contract.get("execution_anchor"),
        contract.get("entry_anchor"),
        getattr(candidate, "trigger_level", 0.0),
        context.get("price"),
    )
    return next((_float(value) for value in values if _float(value) > 0.0), 0.0)


def _candidate_evidence_ts(candidate: Any, context: dict[str, Any]) -> int:
    stage = getattr(candidate, "stage_plan", {}) or {}
    components = getattr(candidate, "score_components", {}) or {}
    intelligence = components.get("execution_intelligence_v9532") or {}
    router = intelligence.get("execution_router") or {}
    values = (
        getattr(candidate, "trigger_ts", 0),
        stage.get("evidence_ts"),
        router.get("evidence_ts"),
        context.get("as_of_ts"),
    )
    return max((int(_float(value)) for value in values), default=0)


def family_episode_key(candidate: Any, context: dict[str, Any]) -> str:
    """Build a cross-setup identity for the same directional market episode.

    The anchor is normalized by ATR, while a one-hour evidence bucket prevents
    an old structural level from suppressing a genuinely new session event.
    """
    setup_type = str(getattr(candidate, "setup_type", "") or "").upper()
    family = canonical_setup_family(setup_type)
    side = str(getattr(candidate, "side", "UNKNOWN") or "UNKNOWN").upper()
    price = _float(context.get("price"), 0.0)
    atr = max(_float(context.get("atr15"), 0.0), price * 0.0005, 1e-9)
    anchor = _candidate_anchor(candidate, context)
    anchor_step = max(atr * 0.35, price * 0.0005, 1e-9)
    anchor_bucket = int(round(anchor / anchor_step)) if anchor > 0.0 else 0
    evidence_ts = _candidate_evidence_ts(candidate, context)
    if 0 < evidence_ts < 10_000_000_000:
        evidence_ts *= 1000
    hour_bucket = evidence_ts // 3_600_000 if evidence_ts > 0 else 0
    return f"{family}|{side}|A{anchor_bucket}|H{hour_bucket}"


def deduplicate_candidates_by_family_episode(
    candidates: Iterable[Any],
    context: dict[str, Any],
    score_fn: Callable[[Any], float],
) -> tuple[list[Any], dict[str, Any]]:
    """Keep one strongest hypothesis per family/side/anchor/evidence episode."""
    rows = list(candidates or [])
    winners: dict[str, tuple[int, Any, tuple[float, ...]]] = {}
    suppressed: dict[str, list[dict[str, Any]]] = {}
    for index, candidate in enumerate(rows):
        key = family_episode_key(candidate, context)
        rank = (
            _float(score_fn(candidate)),
            _float(getattr(candidate, "final_score", 0.0)),
            _float(getattr(candidate, "setup_quality_score", 0.0)),
            _float(getattr(candidate, "execution_quality_score", 0.0)),
        )
        existing = winners.get(key)
        if existing is None or rank > existing[2]:
            if existing is not None:
                previous = existing[1]
                suppressed.setdefault(key, []).append({
                    "setup_type": str(getattr(previous, "setup_type", "") or ""),
                    "selection_score": round(existing[2][0], 4),
                    "reason": "WEAKER_SAME_FAMILY_MARKET_EPISODE",
                })
            winners[key] = (index, candidate, rank)
        else:
            suppressed.setdefault(key, []).append({
                "setup_type": str(getattr(candidate, "setup_type", "") or ""),
                "selection_score": round(rank[0], 4),
                "reason": "WEAKER_SAME_FAMILY_MARKET_EPISODE",
            })

    selected = [value[1] for value in sorted(winners.values(), key=lambda value: value[0])]
    for candidate in selected:
        key = family_episode_key(candidate, context)
        family = canonical_setup_family(getattr(candidate, "setup_type", ""))
        stage = dict(getattr(candidate, "stage_plan", {}) or {})
        stage.update({
            "canonical_setup_family_v9559": family,
            "family_episode_key_v9559": key,
            "suppressed_same_episode_setups_v9559": suppressed.get(key, []),
            "schema_version_v9559": SCHEMA_VERSION,
        })
        candidate.stage_plan = stage

    report = {
        "input_candidates": len(rows),
        "unique_family_episodes": len(selected),
        "suppressed_candidates": sum(len(values) for values in suppressed.values()),
        "suppressed_by_episode": suppressed,
        "families": list(CANONICAL_FAMILIES),
        "trade_authority": False,
        "schema_version": SCHEMA_VERSION,
    }
    return selected, report
