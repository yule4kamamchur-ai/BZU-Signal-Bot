"""Causal, regime-specific entry *selection* for BZU scheduled ICT limit orders.

Design sources: modular directional controllers, strict as-of multi-timeframe
information, and passive first-retest execution. This is NOT a trained
win-probability, exchange fill predictor, or certification of profitability.
No I/O, no mutable global state, no dependency on observed future returns.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
                  'MICRO_ACCEPTED_BREAK', 'ACCEPTED_BREAK_RETEST',
                  'MICRO_BOS_FVG'):
        return 'TREND_CONTINUATION'
    if family == 'MICRO_RANGE_REJECTION':
        return 'RANGE_REJECTION'
    return None


def _confirmed_mixed_continuation(signal, snapshot, tick, regime):
    """Allow a 3m BOS/FVG when 15m and 1H agree but the 4H lags.

    A generic bullish candle is NOT confirmation: the setup must be an
    independently evidenced, CLOSED 3m BOS/FVG, backed by a directional,
    contiguous 15m sequence and a fresh confirmed 1H trend. Opposing 1H+4H,
    missing HTF data, and other strategy families stay blocked.
    """
    if (signal.family != 'MICRO_BOS_FVG' or regime.state != 'MIXED'
            or regime.trend_1h != signal.side or regime.trend_4h != -signal.side
            or tick <= 0 or not (0 <= snapshot.now - signal.signal_ts <= 2 * STEP['15m'])):
        return False
    # Distinct 15m confirmation, not the unfinished 15m candle or a future
    # timestamp. An hour-old directional bias cannot justify a new limit.
    bars15 = closed(snapshot, '15m')
    if len(bars15) < 16 or snapshot.now - (bars15[-1].ts + STEP['15m']) > STEP['15m']:
        return False
    if any(b.ts - a.ts != STEP['15m']
           for a, b in zip(bars15[-16:-1], bars15[-15:])):
        return False
    closes = [x.close for x in bars15[-13:]]
    path = sum(abs(b-a) for a,b in zip(closes, closes[1:]))
    move = closes[-1] - closes[0]
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
              for a,b in zip(bars15[-13:-1], bars15[-12:])]
    a15 = sum(ranges)/len(ranges) if ranges else 0.
    return bool(a15 > 0 and signal.side * move >= 1.25 * a15
                and signal.side * (bars15[-1].close-bars15[-1].open) > -0.35*a15
                and abs(move)/max(path,tick) >= .31)


def qualify(signal, snapshot, tick, regime=None, active_side=0):
    """Return independently attributable strategy rationale, or a veto.

    A rule applies only to NEW candidate entries, not legacy open positions.
    An UNKNOWN HTF state cannot count as evidence for trend continuation;
    a genuine 3m MSS may still be tracked, conservatively, as reversal.
    """
    regime = regime or classify(snapshot)
    strategy = strategy_for(signal.family)
    reason = 'STRATEGY_UNKNOWN'
    if strategy == 'TREND_CONTINUATION':
        if active_side == signal.side:
            reason = 'STRATEGY_M15_OVERRIDE_CONFIRMED'
        elif regime.state == ('TREND_LONG' if signal.side == 1 else 'TREND_SHORT'):
            reason = 'STRATEGY_TREND_CONFIRMED'
        elif _confirmed_mixed_continuation(signal, snapshot, tick, regime):
            reason = 'STRATEGY_MIXED_1H_15M_BOS_CONFIRMED'
        else:
            reason = 'STRATEGY_TREND_REGIME_NOT_CONFIRMED'
    elif strategy == 'RANGE_REJECTION':
        reason = ('STRATEGY_RANGE_CONFIRMED' if regime.state == 'RANGE'
                  else 'STRATEGY_RANGE_REGIME_NOT_CONFIRMED')
    elif strategy == 'LIQUIDITY_REVERSAL':
        # Sideways, transitional, or counter-trend sweep/MSS. Aligned reversal
        # can still be a lower-timeframe liquidity continuation.
        reason = 'STRATEGY_SWEEP_MSS_CONFIRMED'
    allow = reason in ('STRATEGY_TREND_CONFIRMED', 'STRATEGY_M15_OVERRIDE_CONFIRMED',
                       'STRATEGY_MIXED_1H_15M_BOS_CONFIRMED',
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


def ema20(rows):
    if len(rows) < 20:
        return None
    value = mean(c.close for c in rows[:20])
    for candle in rows[20:]:
        value += (candle.close - value) * 2 / 21
    return value


def body_breaks(rows, tick, engine):
    """Use one closed right bar. The breakout itself cannot confirm its pivot."""
    import ict_methodology as ict

    points = engine.pivots(rows[-80:], strength=1)
    for index in range(max(7, len(rows) - 8), len(rows)):
        for side in (1, -1):
            pivot = ict.body_break(rows, index, points, side, tick, STEP['15m'])
            if pivot:
                yield index, side, pivot['level']


def formations(snapshot, tick, engine):
    """Closed M15 displacement or same-bar session sweep and reversal."""
    import directional_policy as direction
    import ict_methodology as ict

    rows = closed(snapshot, '15m')
    step = STEP['15m']
    if len(rows) < 2 or not 0 <= snapshot.now - (rows[-1].ts + step) < step:
        return []
    latest = rows[-1]
    known = latest.ts + step
    result = []

    def add(family, side, proximal, distal, stop, reference, sweep=None):
        low, high = sorted((proximal, distal))
        if low <= 0 or high <= low or side * (proximal - stop) <= tick:
            return
        result.append({
            'family': family, 'side': side, 'signal_ts': known,
            'event_ts': known, 'invalidation': stop,
            'origin_broke_structure': sweep is None, 'organic_confluence': True,
            'zone_freshness': 'UNMITIGATED',
            'entry_zone': {'low': low, 'high': high, 'known_ts': known,
                           'timeframe': '15m', 'kind': family,
                           'reference': reference, 'sweep': sweep},
        })

    for side in (1, -1):
        sweep = ict.immediate_session_sweep(snapshot, rows, side, tick)
        if sweep:
            add('SESSION_SWEEP_ENGULFING_15M', side, sweep['level'],
                sweep['level'] - side * tick, sweep['extreme'], sweep['level'], sweep)

    if len(rows) < 20 or any(b.ts - a.ts != step for a, b in zip(rows[-20:-1], rows[-19:])):
        return result
    volatility = max(engine.atr(rows[:-1]), tick)
    span = latest.high - latest.low
    body = abs(latest.close - latest.open)
    if span < .85 * volatility or body < .65 * span:
        return result
    vwap = direction.session_vwap(replace(snapshot, now=known))['value']
    average = ema20(rows)
    for index, side, level in body_breaks(rows, tick, engine):
        if index != len(rows) - 1 or not any(
                line is not None and side * (latest.close - line) > 0
                for line in (average, vwap)):
            continue
        extreme = latest.low if side == 1 else latest.high
        add('BREAKER_FLIP_15M', side, level, level - side * tick, extreme, level)
        add('DISPLACEMENT_CATCHER_15M', side, latest.close - side * .25 * body,
            latest.close - side * .30 * body, extreme, level)
    return result
