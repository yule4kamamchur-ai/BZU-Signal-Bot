"""Causal, regime-specific entry *selection* for BZU scheduled ICT limit orders.

Design sources: modular directional controllers, strict as-of multi-timeframe
information, and passive first-retest execution. This is NOT a trained
win-probability, exchange fill predictor, or certification of profitability.
No I/O, no mutable global state, no dependency on observed future returns.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

STEP = {'3m': 180_000, '15m': 900_000, '1H': 3_600_000, '4H': 14_400_000}


@dataclass(frozen=True)
class Regime:
    state: str  # TREND_LONG, TREND_SHORT, RANGE, MIXED, UNKNOWN
    trend_1h: int
    trend_4h: int
    efficiency_1h: float
    as_of_ts: int


def closed(snapshot, timeframe):
    duration = STEP[timeframe]
    return sorted((bar for bar in snapshot.candles.get(timeframe, ())
                   if getattr(bar, 'confirmed', False) and bar.ts + duration <= snapshot.now),
                  key=lambda bar: bar.ts)


def _trend(rows, now, duration, window=12):
    if len(rows) < window + 1 or now - (rows[-1].ts + duration) > 2 * duration:
        return 0, 0.
    sample = rows[-(window + 1):]
    closes = [c.close for c in sample]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    move = closes[-1] - closes[0]
    efficiency = abs(move) / path if path else 0.
    ranges = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
              for p, c in zip(sample, sample[1:])]
    atr = mean(ranges) if ranges else 0.
    if atr <= 0 or abs(move) < 1.25 * atr or efficiency < .31:
        return 0, efficiency
    return (1 if move > 0 else -1), efficiency


def classify(snapshot):
    h1 = closed(snapshot, '1H')
    h4 = closed(snapshot, '4H')
    if len(h1) < 14 or len(h4) < 14:
        # A short 4H warmup can be genuinely insufficient. Don't invent bias.
        return Regime('UNKNOWN', 0, 0, 0., snapshot.now)
    # Missing HTF bars can hide a structural reversal between observed bars.
    if (any(b.ts - a.ts != STEP['1H'] for a, b in zip(h1[-14:-1], h1[-13:]))
            or any(b.ts - a.ts != STEP['4H'] for a, b in zip(h4[-14:-1], h4[-13:]))
            or snapshot.now - (h1[-1].ts + STEP['1H']) > 2 * STEP['1H']
            or snapshot.now - (h4[-1].ts + STEP['4H']) > 2 * STEP['4H']):
        return Regime('UNKNOWN', 0, 0, 0., snapshot.now)
    t1, efficiency = _trend(h1, snapshot.now, STEP['1H'])
    t4, _ = _trend(h4, snapshot.now, STEP['4H'])
    if t1 and t4 == -t1:
        label = 'MIXED'
    elif t1 and (t4 == t1 or t4 == 0):
        label = 'TREND_LONG' if t1 == 1 else 'TREND_SHORT'
    elif t1 == 0:
        label = 'RANGE' if efficiency < .31 and t4 == 0 else 'MIXED'
    else:
        label = 'MIXED'
    return Regime(label, t1, t4, round(efficiency, 4), snapshot.now)


def strategy_for(family):
    if family in ('MICRO_SWEEP_MSS', 'LIQUIDITY_MSS'):
        return 'LIQUIDITY_REVERSAL'
    if family in ('MICRO_HTF_FVG', 'HTF_FVG_RETEST',
                  'MICRO_ACCEPTED_BREAK', 'ACCEPTED_BREAK_RETEST'):
        return 'TREND_CONTINUATION'
    if family == 'MICRO_RANGE_REJECTION':
        return 'RANGE_REJECTION'
    return None


def qualify(signal, snapshot, tick, regime=None):
    """Return independently attributable strategy rationale, or a veto.

    A rule applies only to NEW candidate entries, not legacy open positions.
    An UNKNOWN HTF state cannot count as evidence for trend continuation;
    a genuine 3m MSS may still be tracked, conservatively, as reversal.
    """
    regime = regime or classify(snapshot)
    strategy = strategy_for(signal.family)
    reason = 'STRATEGY_UNKNOWN'
    if strategy == 'TREND_CONTINUATION':
        if regime.state != ('TREND_LONG' if signal.side == 1 else 'TREND_SHORT'):
            reason = 'STRATEGY_TREND_REGIME_NOT_CONFIRMED'
        else:
            reason = 'STRATEGY_TREND_CONFIRMED'
    elif strategy == 'RANGE_REJECTION':
        reason = ('STRATEGY_RANGE_CONFIRMED' if regime.state == 'RANGE'
                  else 'STRATEGY_RANGE_REGIME_NOT_CONFIRMED')
    elif strategy == 'LIQUIDITY_REVERSAL':
        # Sideways, transitional, or counter-trend sweep/MSS. Aligned reversal
        # can still be a lower-timeframe liquidity continuation.
        reason = 'STRATEGY_SWEEP_MSS_CONFIRMED'
    allow = reason in ('STRATEGY_TREND_CONFIRMED',
                       'STRATEGY_RANGE_CONFIRMED', 'STRATEGY_SWEEP_MSS_CONFIRMED')
    # Quality is a deterministic *evidence order*, not an empirical win rate.
    # More recent completed structure gets higher priority, never hindsight.
    age = max(0, snapshot.now - signal.signal_ts)
    freshness = max(0., 1. - age / (2 * STEP['15m']))
    base = {'LIQUIDITY_REVERSAL': 63., 'TREND_CONTINUATION': 58.,
            'RANGE_REJECTION': 60.}.get(strategy, 0.)
    evidence_score = round(base + 8 * freshness +
                           (3 if strategy == 'TREND_CONTINUATION' and regime.trend_4h == signal.side else 0), 2)
    return {'accepted': allow, 'reason': reason,
            'strategy': strategy, 'regime': regime.state,
            'evidence_score': evidence_score if allow else None,
            'as_of_ts': snapshot.now, 'estimated_win_rate': None}
