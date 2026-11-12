"""Build independently evidenced passive-entry anchors from completed ICT events.

These anchors are explicitly marked STRATEGY_EVENT. They are NOT claims that
one of the twenty-four original detectors fired. Geometry, HTF opposition,
real targets, costs and live-book arbitration remain with the existing engine.
"""
from __future__ import annotations

import hashlib
from typing import Optional


def proxy_setup(family: str, side: int) -> Optional[str]:
    # Legacy type is used ONLY for the shared stop/target/risk interface.
    if family in ('MICRO_SWEEP_MSS', 'LIQUIDITY_MSS'):
        return 'SWEEP_RECLAIM' if side == 1 else 'MSS_REVERSAL_SHORT'
    if family in ('MICRO_RANGE_REJECTION',):
        return 'FAILED_AUCTION_REJECTION' if side == 1 else 'FAILED_BREAKOUT_SHORT'
    if family in ('MICRO_HTF_FVG', 'HTF_FVG_RETEST', 'MICRO_BOS_FVG'):
        return 'PULLBACK_CONTINUATION'
    if family in ('MICRO_ACCEPTED_BREAK', 'ACCEPTED_BREAK_RETEST'):
        return 'BREAKOUT_RETEST'
    return None


def from_signal(signal, snapshot, cfg, context, core, regime, playbooks, active_side=0):
    """A stable first-observed anchor, never a backdated executed order."""
    source = proxy_setup(signal.family, signal.side)
    if source is None or signal.side not in (1, -1):
        return None
    if not (0 <= snapshot.now - signal.signal_ts <= playbooks.freshness_ms(signal)):
        return None
    checked = playbooks.organic.qualify(signal, snapshot, cfg.tick, regime, active_side)
    if not checked['accepted']:
        return None
    # The signal's own closed-bar invalidation must be correctly oriented.
    if signal.side * (signal.zone - signal.invalidation) <= 2 * cfg.tick:
        return None
    evidence = {
        'entry_route': 'STRATEGY_EVENT', 'planning_policy': 'DIRECT_CONFIRMED_STRATEGY',
        'setup_timeframe': '15m', 'micro_timeframe': '3m',
        'formation_timeframe': '15m', 'formation_micro_timeframe': '3m',
        'source_strategy': signal.family, 'strategy_signal_ts': signal.signal_ts,
        'strategy_event_ts': signal.event_ts, 'episode_ts': signal.event_ts,
        'organic_confluence': True,
        'strategy_regime': checked['regime'], 'strategy_evidence': signal.note,
        'setup_conditions': [
            {'condition': 'Структурна подія підтверджена закритими свічками', 'met': True},
            {'condition': 'Зона ліміту ще не відвідана', 'met': True},
            {'condition': 'Ринковий режим відповідає стратегії', 'met': True},
        ],
    }
    if signal.zone_high>signal.zone_low>0:
        evidence['entry_zone']={'low':signal.zone_low,'high':signal.zone_high,
            'known_ts':signal.signal_ts,'timeframe':signal.zone_timeframe,'kind':'CONFIRMED_FVG'}
    anchor = core.make_anchor(source, 'LONG' if signal.side == 1 else 'SHORT',
                              signal.zone, signal.invalidation, 'STRATEGY_EVENT',
                              'Незалежний ICT-сценарій: ' + signal.family,
                              context, score=min(85., checked['evidence_score'] + 9.),
                              evidence=evidence, ttl_minutes=90)
    if anchor is None:
        return None
    anchor.id = hashlib.sha256(
        f'{signal.family}|{signal.side}|{signal.signal_ts}|{signal.event_ts}|{signal.zone:.8f}'.encode()
    ).hexdigest()[:18]
    anchor.created_ts = signal.signal_ts
    anchor.expires_ts = signal.signal_ts + 90 * 60_000
    return anchor



def momentum_quote(snapshot, side, entry, stop, zone, origin, tick):
    """Return current executable quote after a fresh closed M15 impulse."""
    import strategy_organics as organic

    rows = organic.closed(snapshot, '15m')
    if not rows or not snapshot.trusted:
        return None
    candle = rows[-1]
    known = candle.ts + organic.STEP['15m']
    if (snapshot.now - known >= organic.STEP['15m']
            or not 0 <= snapshot.now - snapshot.ticker_ts <= 120_000):
        return None
    span = max(candle.high - candle.low, tick)
    close_location = (candle.close - candle.low if side == 1 else candle.high - candle.close) / span
    if side * (candle.close - candle.open) <= tick or close_location < 2 / 3:
        return None
    boundary = zone['high'] if side == 1 else zone['low']
    if side * (candle.close - boundary) <= 0:
        return None
    reference = origin.get('reference')
    breakout = (origin.get('break_known_ts') == known and reference is not None
                and side * (candle.close - reference) > tick)
    bounce = candle.ts >= zone['known_ts'] and candle.low <= zone['high'] and candle.high >= zone['low']
    if not (breakout or bounce):
        return None
    quote = snapshot.ask if side == 1 else snapshot.bid
    risk = side * (entry - stop)
    move = side * (quote - entry)
    if snapshot.bid <= 0 or snapshot.ask < snapshot.bid or risk <= 0:
        return None
    if not 0 <= move <= .35 * risk + 1e-9:
        return None
    return {'price': quote, 'move_r': move / risk, 'known_ts': known,
            'pattern': 'BREAKOUT' if breakout else 'ZONE_REACTION'}

def shallow_bos_retest(candle, side, atr, tick):
    """A close near the impulse extreme leaves a 25–35% body retracement."""
    body = side * (candle.close - candle.open)
    width = max(tick, candle.high - candle.low)
    tail = candle.high - candle.close if side == 1 else candle.close - candle.low
    if atr <= 0 or body < .85 * atr or body < 10 * tick - 1e-8 or tail > .30 * width:
        return None
    bounds = sorted(candle.close - side * fraction * body for fraction in (.25, .35))
    return (*bounds, candle.close - side * .30 * body)


def adaptive_limit(snapshot, zone, origin, side, tick, engine):
    from directional_policy import closed

    low, high = zone['low'], zone['high']
    proof = zone.get('wave_proof') or {}
    tf = proof.get('timeframe', zone.get('timeframe', '15m'))
    step = engine.TF.get(tf)
    rows = closed(snapshot, tf) if step else []
    ts = proof.get('impulse_ts') or ((origin.get('displacement_known_ts') or 0) - step if step else 0)
    index = next((i for i, c in enumerate(rows) if c.ts == ts), 0)
    atr = engine.atr(rows[:index])
    if atr > 0 and side*(rows[index].close-rows[index].open) > 1.4*atr:
        if 'FVG' in zone['kind']:
            entry = high if side==1 else low
        else:
            impulse = rows[index]
            retracement = impulse.close - .268*(impulse.close-impulse.open)
            entry = min(high, max(low, retracement))
        basis = 'STRONG_DISPLACEMENT'
    else:
        entry, basis = (low+high)/2, 'NORMAL_ROTATION_50'
    return engine.round_tick(round(entry + tick/2, 10), tick), basis
