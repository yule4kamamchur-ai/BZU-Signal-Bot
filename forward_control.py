"""Forward-only performance control for canonical execution tiers.

Metrics are descriptive.  They never promote a setup, change a threshold, or
manufacture a trade.  Every trade is joined to its originating signal so tier,
family, requested price, and realized execution remain causally attributable.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Iterable

from .setup_families import CANONICAL_FAMILIES, canonical_setup_family


SCHEMA_VERSION = "forward_control_v9.5.59"
ENTRY_TIERS = ("PREMIUM_FULL", "STANDARD_ENTRY", "EARLY_PROBE")
CURRENT_POLICY_TAG = "v9.5.59"


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tier(signal: dict[str, Any], trade: dict[str, Any]) -> str:
    for value in (
        trade.get("execution_tier"),
        signal.get("execution_tier"),
        trade.get("final_execution_tier_v9559"),
        signal.get("final_execution_tier_v9559"),
        signal.get("final_execution_tier_v9558"),
    ):
        normalized = str(value or "").upper()
        if normalized in ENTRY_TIERS:
            return normalized
    action = str(signal.get("action") or trade.get("entry_level") or "").upper()
    stage = str(trade.get("execution_stage") or trade.get("entry_stage") or "").upper()
    if action == "PROBE_ENTRY" or stage == "PROBE":
        return "EARLY_PROBE"
    if action in {"ENTRY", "RISKY_ENTRY"}:
        return "STANDARD_ENTRY"
    return "UNKNOWN"


def _result_r(trade: dict[str, Any]) -> float | None:
    for key in ("pnl_r", "result_r", "realized_r"):
        value = _float(trade.get(key))
        if value is not None:
            return value
    return None


def _slippage_r(signal: dict[str, Any], trade: dict[str, Any]) -> float | None:
    planned = _float(
        trade.get("planned_entry"),
        _float(signal.get("planned_entry"), _float(signal.get("entry"))),
    )
    actual = _float(trade.get("entry"), _float(trade.get("entry_price")))
    stop = _float(trade.get("stop_initial"), _float(signal.get("planned_stop")))
    if planned is None or actual is None or stop is None:
        return None
    risk = abs(planned - stop)
    if risk <= 1e-12:
        return None
    side = str(trade.get("side") or signal.get("side") or "").upper()
    adverse = actual - planned if side == "LONG" else planned - actual
    return adverse / risk


def _row(signal: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any]:
    setup_type = str(trade.get("setup_type") or signal.get("setup_type") or "UNKNOWN").upper()
    family = str(
        trade.get("canonical_setup_family")
        or signal.get("canonical_setup_family")
        or canonical_setup_family(setup_type)
    ).upper()
    return {
        "trade_id": str(trade.get("id") or ""),
        "signal_id": str(trade.get("signal_id") or signal.get("id") or ""),
        "closed_at": trade.get("closed_at"),
        "tier": _tier(signal, trade),
        "family": family,
        "setup_type": setup_type,
        "side": str(trade.get("side") or signal.get("side") or "UNKNOWN").upper(),
        "bot_version": str(
            trade.get("bot_version_at_entry")
            or signal.get("bot_version_at_signal")
            or signal.get("version")
            or ""
        ),
        "r": _result_r(trade),
        "mfe_r": _float(trade.get("mfe_r")),
        "mae_r": _float(trade.get("mae_r")),
        "slippage_r": _slippage_r(signal, trade),
    }


def _maturity(n: int) -> str:
    if n >= 50:
        return "FORWARD_REVIEW_READY"
    if n >= 20:
        return "PROVISIONAL_FORWARD_SAMPLE"
    return "INSUFFICIENT_FORWARD_SAMPLE"


def _metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    material = list(rows)
    resolved = [row for row in material if row.get("r") is not None]
    returns = [float(row["r"]) for row in resolved]
    wins = [value for value in returns if value > 1e-9]
    losses = [value for value in returns if value < -1e-9]
    flats = len(returns) - len(wins) - len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    mfe = [float(row["mfe_r"]) for row in resolved if row.get("mfe_r") is not None]
    mae = [float(row["mae_r"]) for row in resolved if row.get("mae_r") is not None]
    slippage = [float(row["slippage_r"]) for row in resolved if row.get("slippage_r") is not None]
    return {
        "trade_rows": len(material),
        "resolved_trades": len(returns),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": flats,
        "win_rate": round(len(wins) / len(returns), 6) if returns else None,
        "expectancy_r": round(mean(returns), 6) if returns else None,
        "net_r": round(sum(returns), 6) if returns else None,
        "gross_profit_r": round(gross_profit, 6),
        "gross_loss_r": round(gross_loss, 6),
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 1e-12 else None,
        "profit_factor_state": "CALCULATED" if gross_loss > 1e-12 else "NO_LOSS_DENOMINATOR",
        "avg_mfe_r": round(mean(mfe), 6) if mfe else None,
        "avg_mae_r": round(mean(mae), 6) if mae else None,
        "avg_adverse_slippage_r": round(mean(slippage), 6) if slippage else None,
        "mfe_sample": len(mfe),
        "mae_sample": len(mae),
        "slippage_sample": len(slippage),
        "maturity": _maturity(len(returns)),
        "can_change_live_thresholds": False,
    }


def build_forward_control_snapshot(journal: dict[str, Any]) -> dict[str, Any]:
    """Build tier/family/setup forward statistics from actual closed trades."""
    signals = {
        str(row.get("id") or ""): row
        for row in (journal.get("signals") or [])
        if isinstance(row, dict) and str(row.get("id") or "")
    }
    rows: list[dict[str, Any]] = []
    unlinked = 0
    for trade in (journal.get("trades") or []):
        if not isinstance(trade, dict):
            continue
        signal_id = str(trade.get("signal_id") or "")
        signal = signals.get(signal_id, {})
        if not signal:
            unlinked += 1
        rows.append(_row(signal, trade))

    # Promotion evidence begins only with trades opened by this exact policy.
    # Earlier actual trades remain visible as a baseline, never as v9.5.59
    # forward proof.
    current_rows = [row for row in rows if CURRENT_POLICY_TAG in row["bot_version"]]
    by_tier = {tier: _metrics(row for row in current_rows if row["tier"] == tier) for tier in ENTRY_TIERS}
    by_family = {
        family: _metrics(row for row in current_rows if row["family"] == family)
        for family in CANONICAL_FAMILIES
    }
    setup_names = sorted({row["setup_type"] for row in current_rows if row["setup_type"] != "UNKNOWN"})
    by_setup = {name: _metrics(row for row in current_rows if row["setup_type"] == name) for name in setup_names}
    unknown_tier = sum(1 for row in current_rows if row["tier"] == "UNKNOWN")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "actual_closed_trades_only": True,
        "tier_metrics": by_tier,
        "family_metrics": by_family,
        "setup_metrics": by_setup,
        "historical_actual_baseline": {
            "all_tiers": _metrics(rows),
            "tier_metrics": {
                tier: _metrics(row for row in rows if row["tier"] == tier)
                for tier in ENTRY_TIERS
            },
            "can_promote_v9559_policy": False,
        },
        "coverage": {
            "historical_closed_trade_rows": len(rows),
            "v9559_forward_trade_rows": len(current_rows),
            "linked_signal_rows": len(rows) - unlinked,
            "unlinked_trade_rows": unlinked,
            "unknown_tier_rows": unknown_tier,
        },
        "slippage_semantics": {
            "metric": "adverse difference between requested signal entry and recorded runtime entry, normalized by initial R",
            "broker_fill_slippage_available": False,
            "upgrade_path": "persist exchange fill price when broker execution is connected",
        },
        "promotion_policy": {
            "automatic_threshold_mutation": False,
            "minimum_provisional_resolved_trades": 20,
            "minimum_forward_review_resolved_trades": 50,
            "chronological_forward_only": True,
        },
        "schema_version": SCHEMA_VERSION,
    }
