"""BZU Edge Intelligence: causal regime-dependent ICT admission, v1.

The 24 native detectors are *hypotheses*, not 24 proven strategies.  This
layer deliberately has no fitted parameters and reports no win probability.
Never read unclosed candles, a future quote, or realised outcomes here.

ENFORCE is an explicit deployment choice. SHADOW computes the same diagnostics
without altering admission, enabling before/after research and rollbacks.
Existing positions/limits are never changed by this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

HOUR_MS = 3_600_000
FOUR_HOUR_MS = 4 * HOUR_MS


@dataclass(frozen=True)
class Trend:
    side: int = 0
    strength: float = 0.0
    efficiency: float = 0.0
    last_known_ts: int = 0
    usable: bool = False


def _numeric(value, fallback=0.):
    try:
        n = float(value)
        return n if math.isfinite(n) else fallback
    except (TypeError, ValueError):
        return fallback


def closed(snapshot, tf, step):
    now = int(snapshot.now)
    rows = snapshot.candles.get(tf, [])
    # Defence-in-depth: Snapshot.parse normally performs this filtering.
    return sorted((c for c in rows if getattr(c, 'confirmed', False)
                   and int(c.ts) + step <= now), key=lambda c: c.ts)


def trend(snapshot, tf, step, bars):
    rows = closed(snapshot, tf, step)
    if len(rows) < bars + 1:
        return Trend()
    w = rows[-(bars + 1):]
    end = w[-1].ts + step
    # Stale HTF price action is not a directional confirmation.
    if snapshot.now - end > 2 * step:
        return Trend(last_known_ts=end)
    closes = [c.close for c in w]
    ranges = [max(c.high - c.low, abs(c.high - w[i-1].close),
                  abs(c.low - w[i-1].close)) for i, c in enumerate(w[1:], 1)]
    atr = max(mean(ranges), 1e-12)
    move = closes[-1] - closes[0]
    total = sum(abs(b-a) for a, b in zip(closes, closes[1:]))
    efficiency = abs(move) / total if total else 0.
    strength = abs(move) / atr
    # Direction is accepted only if a material, reasonably orderly trend exists.
    side = (1 if move > 0 else -1) if strength >= 1.25 and efficiency >= .31 else 0
    return Trend(side, round(strength, 4), round(efficiency, 4), end, True)


def assess(plan, snapshot, proof=None):
    """Pure pre-placement verdict (no PnL, no changes to trade state).

    Fails closed for absent HTF data in ENFORCE mode. Continuations need an
    objective aligned trend; counter-trend reversals need sequenced liquidity
    and structural proof. Contextual location labels are never stand-alone.
    """
    import ict_methodology

    proof = proof if isinstance(proof, dict) else plan.get('ict_evidence') or {}
    name = str(plan.get('setup', ''))
    contract = ict_methodology.CONTRACTS.get(name)
    t1 = trend(snapshot, '1H', HOUR_MS, 12)
    t4 = trend(snapshot, '4H', FOUR_HOUR_MS, 8)
    side = int(_numeric(plan.get('side')))
    result = {
        'schema': 'BZU_EDGE_REGIME_V1', 'as_of_ts': snapshot.now,
        'setup': name, 'family': contract.family if contract else 'UNKNOWN',
        'side': side, 'h1_direction': t1.side, 'h4_direction': t4.side,
        'h1_strength_atr': t1.strength, 'h4_strength_atr': t4.strength,
        'h1_efficiency': t1.efficiency, 'h4_efficiency': t4.efficiency,
        'h1_known_ts': t1.last_known_ts, 'h4_known_ts': t4.last_known_ts,
        'status': 'REJECT', 'reason': 'UNVERIFIED_EDGE',
        'estimated_win_rate': None,
    }
    def verdict(reason, accept=False):
        result.update(status='ALLOW' if accept else 'REJECT', reason=reason)
        return result
    if contract is None or side not in (-1, 1):
        return verdict('EDGE_UNKNOWN_SETUP_OR_SIDE')
    if not (t1.usable and t4.usable):
        return verdict('EDGE_INSUFFICIENT_OR_STALE_HTF')
    if not plan.get('ict_admission_reason', 'CONFIRMED') == 'CONFIRMED':
        return verdict('EDGE_ICT_SEQUENCE_INCOMPLETE')
    if _numeric(plan.get('net_rr', 0)) < 1.2:
        return verdict('EDGE_INSUFFICIENT_NET_REWARD')
    if _numeric(plan.get('risk', 0)) <= 0:
        return verdict('EDGE_INVALID_STOP_GEOMETRY')

    continuation = contract.family in ('TREND_CONTINUATION', 'STRUCTURE_BREAK',
                                        'SESSION_EXPANSION')
    reversal = contract.family in ('LIQUIDITY_REVERSAL', 'RANGE_REVERSAL',
                                   'FAILED_EXPANSION')
    opposite_1 = t1.side == -side
    opposite_4 = t4.side == -side
    aligned_1 = t1.side == side
    aligned_4 = t4.side == side
    sweep = bool(proof.get('sweep_reclaim'))
    shift = bool(proof.get('structure_shift'))
    sequence = bool(proof.get('sweep_then_shift'))
    displacement = bool(proof.get('displacement'))
    acceptance = bool(proof.get('break_acceptance'))
    fvg = bool(proof.get('near_unmitigated_fvg'))

    # No 'continuation' when the entry is fighting both actual HTF trends.
    if continuation:
        if opposite_1 or (opposite_4 and not aligned_1):
            return verdict('EDGE_HTF_AGAINST_CONTINUATION')
        if not (aligned_1 or aligned_4):
            return verdict('EDGE_CONTINUATION_LACKS_TREND')
        if name == 'MOMENTUM_NO_PULLBACK_CONTINUATION' and not (fvg and acceptance):
            return verdict('EDGE_MOMENTUM_WITHOUT_CONFIRMED_RETEST')
        if not (displacement or acceptance or shift):
            return verdict('EDGE_CONTINUATION_NOT_CONFIRMED')
        return verdict('EDGE_HTF_CONTINUATION_CONFIRMED', True)
    if reversal:
        # Contrary 1H / 4H price flow demands the full sweep => MSS order.
        if opposite_1 or opposite_4:
            if not (sweep and shift and sequence):
                return verdict('EDGE_COUNTERTREND_NEEDS_SWEEP_THEN_MSS')
        elif not (sweep or (shift and displacement)):
            return verdict('EDGE_REVERSAL_HAS_NO_MARKET_EVENT')
        return verdict('EDGE_LIQUIDITY_REVERSAL_CONFIRMED', True)
    # Value/session labels and equal highs/lows supply *context*, not entry.
    if contract.status == 'ICT_CONTEXT_ONLY':
        if not ((sweep and shift and sequence) or
                ((aligned_1 or aligned_4) and shift and displacement and fvg)):
            return verdict('EDGE_CONTEXT_WITHOUT_INDEPENDENT_ICT_SETUP')
        return verdict('EDGE_CONTEXT_WITH_CONFIRMED_ICT_TRIGGER', True)
    return verdict('EDGE_UNSUPPORTED_STRATEGY_FAMILY')
