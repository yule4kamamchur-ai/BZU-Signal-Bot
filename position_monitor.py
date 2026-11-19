"""Causal position excursions and protective decisions from observed prices."""
from __future__ import annotations


from dataclasses import replace

import strategy_organics
import oil_risk

STEPS = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '1H': 3_600_000, '4H': 14_400_000}


def audit(position, snapshot, cfg, engine):
    """Use the actual engine lifecycle, without changing the supplied ledger."""
    import copy
    book={'active':copy.deepcopy(position),'pending':None,'reconciliation':None,
          'orders':[],'trades':[],'events':[],'used_events':{}}
    tf, step = engine.execution_series(snapshot)
    engine._advance_slot_lifecycle(book,snapshot,cfg,allow_fill=False,execution=(tf,step))
    return {'active':book['active'],'closed':book['trades'],'events':book['events'],
            'timeframe':tf}


def _recent(snapshot, tf, minimum):
    step = STEPS[tf]
    bars = sorted((b for b in snapshot.candles.get(tf, [])
                   if b.confirmed and b.ts + step <= snapshot.now), key=lambda b: b.ts)
    if len(bars) < minimum or snapshot.now - (bars[-1].ts + step) > 2 * step:
        return []
    tail = bars[-minimum:]
    if any(b.ts - a.ts != step for a, b in zip(tail, tail[1:])):
        return []
    return tail


def review(position, snapshot):
    side = int(position.get('side', 0))
    if side not in (-1, 1):
        return {'status': 'UNKNOWN', 'reason': 'Невідомий напрямок позиції', 'adverse': False}
    h1 = _recent(snapshot, '1H', 14)
    m15 = _recent(snapshot, '15m', 9)
    m3 = _recent(snapshot, '3m', 6)
    if not h1 or not m15 or not m3:
        return {'status': 'INSUFFICIENT', 'reason': 'Бракує свіжих закритих свічок для перевірки структури',
                'adverse': False, 'as_of_ts': snapshot.now}

    # Structural break is proved by a CLOSED 15m candle beyond extrema that
    # were known BEFORE the breakout candle. An intrabar wick is insufficient.
    prior = m15[-7:-1]
    last = m15[-1]
    adverse_15m = (last.close < min(b.low for b in prior) if side == 1
                   else last.close > max(b.high for b in prior))
    recent_hour = h1[-1]
    past_hours = h1[-7:-1]
    adverse_1h = (recent_hour.close < min(b.low for b in past_hours) if side == 1
                  else recent_hour.close > max(b.high for b in past_hours))
    regime = strategy_organics.classify(snapshot)
    contrary_htf = regime.state == ('TREND_SHORT' if side == 1 else 'TREND_LONG')
    micro_counter = sum(side * (b.close - b.open) < 0 for b in m3[-3:]) >= 2

    if adverse_1h and (adverse_15m or contrary_htf):
        status, reason = 'REVERSAL_RISK', '1H злам структури проти позиції; підтвердження на 15m або трендом'
    elif adverse_15m or adverse_1h or (contrary_htf and micro_counter):
        status, reason = 'WEAKENING', 'Є ознаки руху проти позиції; потрібне спостереження за структурою'
    else:
        status, reason = 'INTACT', 'Завершені 15m/1H свічки не підтверджують розворот проти позиції'
    return {'status': status, 'reason': reason, 'adverse': status in ('REVERSAL_RISK', 'WEAKENING'),
            'break_15m': adverse_15m, 'break_1h': adverse_1h,
            'regime': regime.state, 'as_of_ts': snapshot.now,
            'entry_risk_model': oil_risk.MODEL if oil_risk.is_organic(position) else 'LEGACY',
            'initial_risk': position.get('risk')}


ROUND_TRIP_MFE = .40
ROUND_TRIP_BARS = 6
STALE_MFE = .60
STALE_STOP_R = .35
STALE_BARS = 24
REPLACEMENT_BARS = 16
ADVERSE_HOLD_BARS = 6
ADVERSE_EXIT_R = -.35
EPS = 1e-8


def duration(position, ts):
    entered = position.get('filled_ts') or position.get('placed_ts') or ts
    elapsed = max(0, ts - entered)
    return elapsed, elapsed // STEPS['15m']


def metrics(position, ts):
    elapsed, bars = duration(position, ts)
    return {'max_favorable_r': max(position.get('max_favorable_r') or 0., position.get('mfe_r') or 0.),
            'max_adverse_r': max(position.get('max_adverse_r') or 0., position.get('mae_r') or 0.),
            'bars_in_trade': bars, 'time_in_trade_ms': elapsed}


def current_r(position, price):
    return position['side'] * (price - position['entry']) / position['risk']


def track(position, ts, favorable, adverse):
    values = metrics(position, ts)
    favorable_r, adverse_r = current_r(position, favorable), -current_r(position, adverse)
    new_peak = favorable_r > values['max_favorable_r'] + EPS
    if new_peak:
        values.update(max_favorable_r=favorable_r, max_favorable_ts=ts)
    values['max_adverse_r'] = max(values['max_adverse_r'], adverse_r)
    position.update(values, mfe_r=values['max_favorable_r'], mae_r=values['max_adverse_r'])
    return new_peak


def update_trade_excursions(position, candles_15m, as_of):
    """Merge fully post-fill M15 extremes as their candles become observable."""
    entered = position.get('filled_ts')
    if entered is None or position['risk'] <= 0:
        return
    step = STEPS['15m']
    fill_interval = {'WITHIN_1M_BAR': STEPS['1m'], 'WITHIN_3M_BAR': STEPS['3m']}
    first_complete = entered + fill_interval.get(position.get('fill_time_status'), 0)
    through = position.get('m15_excursion_through', entered)
    rows = []
    for bar in reversed(candles_15m):
        if bar.ts < first_complete or bar.ts + step <= through:
            break
        if bar.confirmed and bar.ts + step <= as_of:
            rows.append(bar)
    for bar in reversed(rows):
        favorable, adverse = (bar.high, bar.low) if position['side'] == 1 else (bar.low, bar.high)
        track(position, bar.ts + step, favorable, adverse)
        position['m15_excursion_through'] = bar.ts + step


def backfill(position, snapshot, checkpoint):
    """Observe already reconciled candles on upgrade without backdating exits."""
    if position.get('last_bar', -1) < 0:
        return
    step = position.get('last_execution_step_ms', STEPS['3m'])
    as_of = min(snapshot.now, checkpoint, position['last_bar'] + step)
    update_trade_excursions(position, snapshot.candles.get('15m', ()), as_of)


def partial_taken(position):
    return bool(position.get('tp1_hit') or position.get('partial_tp1_hit'))


def adverse_close_pair(position, snapshot, tick, engine):
    """Require two closes beyond one pivot known before the pair."""
    rows = _recent(snapshot,'15m',24)
    if not rows:
        return None
    first, last = rows[-2:]
    step = STEPS['15m']
    if first.ts < (position.get('filled_ts') or position.get('placed_ts') or snapshot.now):
        return None
    ema, vwap = engine.policy.trend_references(snapshot)
    side = position['side']
    prior_ema, prior_vwap = engine.policy.trend_references(replace(snapshot,now=first.ts+step))
    if any(ref is None or side*(bar.close-ref)>=0
           for bar, refs in ((first,(prior_ema,prior_vwap)),(last,(ema,vwap))) for ref in refs):
        return None
    kind = 'LOW' if side==1 else 'HIGH'
    swing = next((p for p in reversed(engine.pivots(rows[:-2],strength=2))
                  if p['kind']==kind and p['known_ts']+step<=first.ts),None)
    if not swing or any(side*(bar.close-swing['level'])>-tick+EPS for bar in (first,last)):
        return None
    return {'known_ts':last.ts+step, 'level':swing['level'],
            'ema20':ema, 'session_vwap':vwap}


def observe_structure(position, snapshot, ts, cfg, engine):
    if snapshot is None:
        return
    bucket = ts // STEPS['15m']
    if position.get('protection_structure_bucket') == bucket:
        return
    position['protection_structure_bucket'] = bucket
    entered = position.get('filled_ts') or ts
    as_of = replace(snapshot, now=ts)
    if oil_risk.enabled(cfg):
        pair = adverse_close_pair(position,as_of,cfg.tick,engine)
        if pair is None:
            position.pop('adverse_close_pair',None)
        else:
            position['adverse_close_pair'] = pair
    rows = _recent(as_of, '15m', 15)
    atr = engine.atr(rows[:-1])
    position.pop('adverse_impulse_ts', None)
    position.pop('entry_engulfed_ts', None)
    recent = _recent(as_of, '15m', 1)
    zone = position.get('entry_zone') or {}
    if recent:
        bar = recent[-1]
        side = position['side']
        entry = position['entry']
        low, high = zone.get('low',entry), zone.get('high',entry)
        proximal, distal = (high,low) if side==1 else (low,high)
        if (bar.ts >= entered and side*(bar.open-proximal) >= -cfg.tick/2
                and side*(bar.close-distal) <= -cfg.tick):
            position['entry_engulfed_ts'] = bar.ts + STEPS['15m']
    if rows and atr > 0:
        impulse = rows[-1]
        if impulse.ts >= entered and -position['side'] * (impulse.close - impulse.open) >= max(cfg.tick, .60 * atr):
            position['adverse_impulse_ts'] = impulse.ts + STEPS['15m']
    proofs = [engine.policy.structural_flip(as_of, side, entered, cfg.tick, engine)
              for side in (-position['side'], position['side'])]
    proof = max((p for p in proofs if p['confirmed']),
                key=lambda p: (p['known_ts'], p['timeframe'] == '15m'), default=None)
    previous = position.get('protection_structure_ts', position.get('adverse_choch_ts', 0))
    if proof and (proof['known_ts'] > previous or
                  (proof['known_ts'] == previous and not position.get('protection_structure_timeframe'))):
        position.update(protection_structure_ts=proof['known_ts'], protection_structure_side=proof['side'],
                        protection_structure_timeframe=proof['timeframe'], protection_structure_level=proof['level'])

def adverse_structure(position, ts):
    side = position.get('protection_structure_side')
    known = position.get('protection_structure_ts', position.get('adverse_choch_ts', 0))
    entered = position.get('filled_ts') or 0
    return entered < known <= ts and (side is None or side == -position['side'])


def guard(position, ts, *, structural_only=False):
    values = metrics(position, ts)
    peak, bars = values['max_favorable_r'], values['bars_in_trade']
    entered = position.get('filled_ts') or position.get('placed_ts') or ts
    regime = position.get('position_regime', 'UNKNOWN')
    regime_opposes = position.get('position_regime_side') == -position['side']
    confirmed_break = adverse_structure(position, ts)
    if partial_taken(position):
        mode = 'POST_TP1'
    elif structural_only:
        mode = ('STRUCTURAL_HOLD' if bars<ADVERSE_HOLD_BARS else
                'ADVERSE_CHOCH' if position.get('adverse_close_pair') else 'STRUCTURAL_WATCH')
    elif position.get('stale_stop') is not None:
        mode = 'STALE_TIGHTENED'
    elif bars >= STALE_BARS and peak < STALE_MFE - EPS:
        mode = 'STALE_TIMEOUT'
    elif confirmed_break or regime_opposes:
        mode = 'ADVERSE_CHOCH'
    elif bars >= ROUND_TRIP_BARS and peak >= ROUND_TRIP_MFE - EPS:
        mode = 'ROUND_TRIP'
    elif bars >= REPLACEMENT_BARS and peak < STALE_MFE - EPS:
        mode = 'STALE_WARNING'
    elif peak >= ROUND_TRIP_MFE - EPS:
        mode = 'ROUND_TRIP_TIMER'
    else:
        mode = 'OBSERVING'
    return {**values, 'mode': mode, 'entry_price': position['entry'],
            'peak_price': position['entry'] + position['side'] * peak * position['risk'],
            'round_trip_at': entered + ROUND_TRIP_BARS * STEPS['15m'],
            'stale_at': None if structural_only else entered + STALE_BARS * STEPS['15m'],
            'structural_only': structural_only, 'structural_hold_until': entered + ADVERSE_HOLD_BARS * STEPS['15m'],
            'stale_stop': position.get('stale_stop'), 'stale_stop_r': -STALE_STOP_R,
            'round_trip_trigger_r': .10, 'round_trip_mfe_r': ROUND_TRIP_MFE,
            'stale_mfe_r': STALE_MFE, 'structure': 'ADVERSE' if confirmed_break else (
                'COUNTER_TREND' if regime_opposes else position.get('position_structure_status', 'UNKNOWN')),
            'adverse_hold_bars': ADVERSE_HOLD_BARS,
            'adverse_close_pair': position.get('adverse_close_pair'), 'regime': regime}


def evaluate_active_trade_lifecycle(position, ts, price, cfg, engine):
    """Decide from information already known at the executable observation."""
    decision = {'should_exit': False, 'exit_reason': None, 'stop': None}
    if position['risk'] <= 0 or partial_taken(position):
        return decision
    info = guard(position, ts)
    side, entry, risk = position['side'], position['entry'], position['risk']
    bars, peak, rr = info['bars_in_trade'], info['max_favorable_r'], current_r(position, price)
    if oil_risk.enabled(cfg):
        proof = position.get('adverse_close_pair')
        if (bars>=ADVERSE_HOLD_BARS and proof and 0<=ts-proof['known_ts']<STEPS['15m']
                and all(side*(price-proof[key])<0 for key in ('level','ema20','session_vwap'))):
            decision.update(should_exit=True,exit_reason='CLOSED_ADVERSE_STRUCTURE_SHIFT')
        return decision
    swing = position.get('protection_structure_level')
    beyond_swing = swing is None or side*(price-swing)<=EPS
    adverse_exit = (bars>=3 and info['structure']=='ADVERSE'
                    and beyond_swing and rr<=ADVERSE_EXIT_R+EPS)
    reason = None
    if bars >= STALE_BARS and peak < STALE_MFE - EPS:
        stop = engine.round_tick(round(entry - side * STALE_STOP_R * risk + cfg.tick / 2, 10), cfg.tick)
        if stop > 0 and side * (stop - position['stop']) >= cfg.tick - EPS:
            if side * (price - stop) <= EPS:
                reason = 'CLOSED_STALE_TIME_STOP'
                decision['exit_trigger_level'] = stop
            else:
                decision['stop'] = stop
    impulse_ts = position.get('adverse_impulse_ts', 0)
    market = position.get('order_type') == 'MARKET' or (
        not position.get('order_type') and position.get('action_type') == 'ACTIVE_ENTRY')
    if (reason is None and market and 2 <= bars <= 6 and info['time_in_trade_ms'] <= 6 * STEPS['15m']
            and peak < .15 - EPS and rr <= -.45 + EPS
            and impulse_ts and 0 <= ts - impulse_ts < STEPS['15m']):
        reason = 'CLOSED_EARLY_IMPULSE_FAILURE'
    if reason is None and adverse_exit:
        reason = 'CLOSED_ADVERSE_STRUCTURE_SHIFT'
    if reason is None and bars >= ROUND_TRIP_BARS and peak >= ROUND_TRIP_MFE - EPS and rr <= .10 + EPS:
        reason = 'CLOSED_ROUND_TRIP_BE'
    if reason:
        decision.update(should_exit=True, exit_reason=reason, stop=None)
    return decision


def bounce_breakeven(position, snapshot, ts, price, tick):
    """Return the newly confirmed adverse M15 engulfing timestamp."""
    if (snapshot is None or partial_taken(position)
            or metrics(position,ts)['max_favorable_r'] < .40-EPS):
        return None
    side = position['side']
    entry = position['entry']
    if side*(entry-position['stop']) < tick-EPS or side*(price-entry) < tick-EPS:
        return None
    from ict_methodology import reversal_engulfing

    step = STEPS['15m']
    entered = position.get('filled_ts') or ts
    rows = [bar for bar in snapshot.candles.get('15m',()) if bar.confirmed
            and max(entered,ts-3*step) <= bar.ts and bar.ts+step <= ts]
    if (len(rows)>=2 and ts-rows[-1].ts-step < step
            and reversal_engulfing(rows[-2],rows[-1],-side,tick,step)):
        return rows[-1].ts+step
    return None


MFE_STOP_POLICY = 'MFE_PROTECTION'
MFE_LOCK_STEPS = ((1.25, .35),)


def mfe_protection(position, ts, price, cfg, engine):
    """Calculate an improving lock from already observed excursions."""
    if not oil_risk.enabled(cfg) or position['risk'] <= 0 or ts <= (position.get('filled_ts') or 0):
        return None
    peak = metrics(position, ts)['max_favorable_r']
    locked_r = next((lock for trigger, lock in reversed(MFE_LOCK_STEPS) if peak >= trigger - EPS), None)
    if locked_r is None:
        return None
    side, entry = position['side'], position['entry']
    raw_stop = entry + side * locked_r * position['risk']
    rounded = engine.round_tick(round(raw_stop, 10), cfg.tick, up=side == 1)
    cost_stop = engine.bzu_policy.breakeven_price(position, engine.execution_config(position, cfg), engine)
    stop = max(rounded, cost_stop) if side == 1 else min(rounded, cost_stop)
    existing = engine.bzu_policy.protective_amendment(position)
    if side * (stop - position['stop']) <= EPS or (existing and side * (stop - existing['price']) <= EPS):
        return None
    return {'policy': MFE_STOP_POLICY, 'price': stop, 'effective_ts': ts, 'peak_r': peak,
            'exit_now': side * (price - stop) <= EPS}


def is_mfe_stop(position):
    return (position.get('mfe_protection') or {}).get('price') == position['stop']


def is_bounce_stop(position):
    return bool(position.get('bounce_stop_ts')) and abs(position['stop']-position['entry']) <= EPS


def is_stale_stop(position):
    return position.get('stale_stop') == position['stop']


def confirmed_m5_structure(snapshot, after, as_of, side, stop, tick):
    """A closed post-TP1 swing and a close through its opposite edge."""
    from setup_scanner import closed_five_minutes

    step = 300_000
    rows = [bar for bar in closed_five_minutes(snapshot)
            if after <= bar.ts and bar.ts + step <= as_of]
    for i in range(len(rows) - 3, -1, -1):
        left, swing, right = rows[i:i + 3]
        if swing.ts != left.ts + step or right.ts != swing.ts + step:
            continue
        low_side = side == 1
        extremes = [bar.low if low_side else bar.high for bar in (left, swing, right)]
        level = extremes[1]
        opposite = swing.high if low_side else swing.low
        if (side * (extremes[0] - level) >= tick - 1e-8
                and side * (extremes[2] - level) >= tick - 1e-8
                and side * (level - stop) >= tick - 1e-8
                and side * (right.close - opposite) >= tick - 1e-8
                and all(side * ((bar.low if low_side else bar.high) - level) > 0
                        for bar in rows[i + 3:])):
            return right.ts + step
    return None


LIQUIDITY_STOP_POLICY = 'LIQUIDITY_SWEEP_REJECTION_070R'


def liquidity_protection(position, snapshot, ts, price, cfg, engine):
    """Arm only after a post-fill liquidity raid and a later closed M5 rejection."""
    if snapshot is None or partial_taken(position) or position.get('liquidity_protection'):
        return None
    from limit_structure import local_liquidity
    from setup_scanner import closed_five_minutes

    entered = position.get('filled_ts')
    if entered is None or entered >= ts or position.get('risk', 0) <= 0:
        return None
    side, entry, risk = position['side'], position['entry'], position['risk']
    as_of = replace(snapshot, now=ts)
    pools = local_liquidity(replace(snapshot, now=entered), engine, cfg.tick)
    pools = [pool for pool in pools if pool['side'] == -side
             and 0 < side * (pool['level'] - entry) < risk]
    bars = [bar for bar in closed_five_minutes(as_of) if bar.ts >= entered]
    if len(bars) < 2:
        return None
    reversal = bars[-1]
    if ts - reversal.ts - 300_000 >= 300_000:
        return None
    prior = bars[:-1]
    if reversal.ts != prior[-1].ts + 300_000:
        return None
    peak = max(side * ((bar.high if side == 1 else bar.low) - entry) / risk for bar in prior)
    if peak < .70 - EPS or side * (reversal.close - reversal.open) >= -cfg.tick:
        return None
    for pool in sorted(pools, key=lambda row: side * (row['level'] - entry), reverse=True):
        swept = next((bar for bar in prior if
                      side * ((bar.high if side == 1 else bar.low) - pool['level']) >= cfg.tick), None)
        if swept is None or side * (reversal.close - pool['level']) >= -cfg.tick:
            continue
        costs = engine.execution_config(position, cfg)
        cost_stop = engine.bzu_policy.breakeven_price(position, costs, engine)
        stop = engine.round_tick(round(entry + side * .05 * risk, 10), cfg.tick, up=side == 1)
        stop = max(stop, cost_stop) if side == 1 else min(stop, cost_stop)
        if side * (stop - position['stop']) <= EPS:
            return None
        return {'policy': LIQUIDITY_STOP_POLICY, 'price': stop, 'effective_ts': ts,
                'known_ts': reversal.ts + 300_000, 'pool': pool,
                'sweep_ts': swept.ts + 300_000, 'peak_r': peak,
                'exit_now': side * (price - stop) <= cfg.tick}
    return None
