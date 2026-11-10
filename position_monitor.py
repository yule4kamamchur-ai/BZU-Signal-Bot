"""Causal position excursions and protective decisions from observed prices."""
from __future__ import annotations


from dataclasses import replace

import strategy_organics
import oil_risk

STEPS = {'3m': 180_000, '15m': 900_000, '1H': 3_600_000, '4H': 14_400_000}


def audit(position, snapshot, cfg, engine):
    """Use the actual engine lifecycle, without changing the supplied ledger."""
    import copy
    book={'active':copy.deepcopy(position),'pending':None,'reconciliation':None,
          'orders':[],'trades':[],'events':[],'used_events':{}}
    engine._advance_slot_lifecycle(book,snapshot,cfg,allow_fill=False)
    return {'active':book['active'],'closed':book['trades'],'events':book['events'],
            'timeframe':engine.execution_series(snapshot)[0]}


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
EPS = 1e-8


def duration(position, ts):
    entered = position.get('filled_ts') or position.get('placed_ts') or ts
    elapsed = max(0, ts - entered)
    return elapsed, elapsed // STEPS['15m']


def metrics(position, ts):
    elapsed, bars = duration(position, ts)
    return {'max_favorable_r': position.get('max_favorable_r', position.get('mfe_r', 0.)),
            'max_adverse_r': position.get('max_adverse_r', position.get('mae_r', 0.)),
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
    through = position.get('m15_excursion_through', entered)
    rows = []
    for bar in reversed(candles_15m):
        if bar.ts < entered or bar.ts + step <= through:
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


def observe_structure(position, snapshot, ts, cfg, engine):
    if snapshot is None:
        return
    bucket = ts // STEPS['15m']
    if position.get('protection_structure_bucket') == bucket:
        return
    position['protection_structure_bucket'] = bucket
    entered = position.get('filled_ts') or ts
    as_of = replace(snapshot, now=ts)
    for side in (-position['side'], position['side']):
        proof = engine.policy.structural_flip(as_of, side, entered, cfg.tick, engine, timeframes=('15m', '1H'))
        if not proof['confirmed'] or proof['known_ts'] <= position.get(
                'protection_structure_ts', position.get('adverse_choch_ts', 0)):
            continue
        position.update(protection_structure_ts=proof['known_ts'], protection_structure_side=side)


def adverse_structure(position, ts):
    side = position.get('protection_structure_side')
    known = position.get('protection_structure_ts', position.get('adverse_choch_ts', 0))
    entered = position.get('filled_ts') or 0
    return entered < known <= ts and (side is None or side == -position['side'])


def guard(position, ts):
    values = metrics(position, ts)
    peak, bars = values['max_favorable_r'], values['bars_in_trade']
    entered = position.get('filled_ts') or position.get('placed_ts') or ts
    regime = position.get('position_regime', 'UNKNOWN')
    regime_opposes = position.get('position_regime_side') == -position['side']
    confirmed_break = adverse_structure(position, ts)
    if position.get('tp1_hit'):
        mode = 'POST_TP1'
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
            'stale_at': entered + STALE_BARS * STEPS['15m'],
            'stale_stop': position.get('stale_stop'), 'stale_stop_r': -STALE_STOP_R,
            'round_trip_trigger_r': .10, 'round_trip_mfe_r': ROUND_TRIP_MFE,
            'stale_mfe_r': STALE_MFE, 'structure': 'ADVERSE' if confirmed_break else (
                'COUNTER_TREND' if regime_opposes else position.get('position_structure_status', 'UNKNOWN')),
            'regime': regime}


def evaluate_active_trade_lifecycle(position, ts, price, cfg, engine):
    """Decide from information already known at the executable observation."""
    decision = {'should_exit': False, 'exit_reason': None, 'stop': None}
    if position.get('tp1_hit') or position['risk'] <= 0:
        return decision
    info = guard(position, ts)
    side, entry, risk = position['side'], position['entry'], position['risk']
    bars, peak, rr = info['bars_in_trade'], info['max_favorable_r'], current_r(position, price)
    reason = None
    if bars >= STALE_BARS and peak < STALE_MFE - EPS:
        stop = engine.round_tick(round(entry - side * STALE_STOP_R * risk + cfg.tick / 2, 10), cfg.tick)
        if stop > 0 and side * (stop - position['stop']) >= cfg.tick - EPS:
            if side * (price - stop) <= EPS:
                reason = 'CLOSED_STALE_TIME_STOP'
                decision['exit_trigger_level'] = stop
            else:
                decision['stop'] = stop
    if reason is None and info['structure'] in ('ADVERSE', 'COUNTER_TREND') and (
            (peak >= .30 - EPS and rr < -EPS) or (bars >= 12 and rr <= -.15 + EPS)):
        reason = 'CLOSED_ADVERSE_STRUCTURE_SHIFT'
    if reason is None and bars >= ROUND_TRIP_BARS and peak >= ROUND_TRIP_MFE - EPS and rr <= .10 + EPS:
        reason = 'CLOSED_ROUND_TRIP_BE'
    if reason:
        decision.update(should_exit=True, exit_reason=reason, stop=None)
    return decision


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
