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


ROUND_TRIP_MFE = .55
ROUND_TRIP_BARS = 6
STALE_MFE = .60
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


def backfill(position, snapshot, checkpoint):
    """Migrate excursions of an existing fill through its processed cursor."""
    if 'max_adverse_r' in position or not position.get('filled_ts') or position.get('last_bar', -1) < 0:
        return
    step = position.get('last_execution_step_ms', STEPS['3m'])
    as_of = min(snapshot.now, checkpoint, position['last_bar'] + step)
    entered = position['filled_ts']
    if position['risk'] <= 0 or as_of <= entered:
        return
    rows = [bar for bar in snapshot.candles.get('15m', []) if bar.confirmed and
            entered <= bar.ts and bar.ts + STEPS['15m'] <= as_of]
    if rows:
        high, low = max(bar.high for bar in rows), min(bar.low for bar in rows)
        favorable, adverse = (high, low) if position['side'] == 1 else (low, high)
        track(position, as_of, favorable, adverse)
    else:
        position.update(metrics(position, as_of))


def _stale_stop(position, snapshot, ts, price, cfg, engine):
    step = STEPS['15m']
    rows = [bar for bar in snapshot.candles.get('15m', []) if bar.confirmed and
            ts - 8 * step <= bar.ts and bar.ts + step <= ts]
    side, entry, risk = position['side'], position['entry'], position['risk']
    kind = 'LOW' if side == 1 else 'HIGH'
    for point in reversed(engine.pivots(rows, strength=1)):
        if point['kind'] != kind:
            continue
        stop = engine.round_tick(round(point['level'] - side * cfg.tick, 10), cfg.tick, up=side == -1)
        if (side * (stop - entry) >= -.35 * risk - EPS
                and side * (stop - position['stop']) >= cfg.tick - EPS
                and side * (price - stop) >= cfg.tick - EPS):
            return stop
    return None


def guard(position, ts):
    values = metrics(position, ts)
    peak, bars = values['max_favorable_r'], values['bars_in_trade']
    if position.get('tp1_hit'):
        return {**values, 'mode': 'POST_TP1', 'entry_price': position['entry']}
    if position.get('adverse_choch_ts'):
        mode = 'ADVERSE_CHOCH'
    elif bars >= STALE_BARS and peak < STALE_MFE:
        mode = 'STALE_TIMEOUT'
    elif bars >= ROUND_TRIP_BARS and peak >= ROUND_TRIP_MFE - EPS:
        mode = 'ROUND_TRIP'
    elif bars >= REPLACEMENT_BARS and peak < STALE_MFE:
        mode = 'STALE_WARNING'
    elif peak >= ROUND_TRIP_MFE - EPS:
        mode = 'ROUND_TRIP_TIMER'
    else:
        mode = 'OBSERVING'
    entered = position.get('filled_ts') or position.get('placed_ts') or ts
    return {**values, 'mode': mode, 'entry_price': position['entry'],
            'round_trip_at': entered + ROUND_TRIP_BARS * STEPS['15m'],
            'stale_at': entered + STALE_BARS * STEPS['15m'],
            'stale_stop': position.get('stale_stop'),
            'retest_radius': .10 * position['risk']}


def protect(state, position, snapshot, ts, price, cfg, engine, *, new_peak=False):
    """Evaluate after hard SL and TP. Exits use an observed price and normal costs."""
    if position.get('tp1_hit') or position['risk'] <= 0:
        return
    info = guard(position, ts)
    side, entry = position['side'], position['entry']
    rr = current_r(position, price)
    reason = None
    if (position.get('adverse_choch_ts', ts) < ts and -.10 - EPS <= rr <= EPS):
        reason = 'CLOSED_ADVERSE_CHOCH'
    elif info['bars_in_trade'] >= STALE_BARS and info['max_favorable_r'] < STALE_MFE and abs(price-entry) <= cfg.tick/2 + EPS:
        reason = 'CLOSED_STALE_TIMEOUT'
    elif (info['bars_in_trade'] >= ROUND_TRIP_BARS and info['max_favorable_r'] >= ROUND_TRIP_MFE - EPS
          and abs(rr) <= .10 + EPS and not new_peak and position.get('max_favorable_ts', 0) < ts):
        reason = 'CLOSED_ROUND_TRIP_BE'
    if reason:
        actual = engine.execution_config(position, cfg)
        engine.close_trade(state, position, price * (1 - side * actual.slippage_bps / 10_000), ts, reason, cfg)
        return
    if snapshot is None:
        return
    bucket = ts // STEPS['15m']
    if position.get('protection_structure_bucket') != bucket:
        import directional_policy as direction

        position['protection_structure_bucket'] = bucket
        proof = direction.structural_flip(replace(snapshot, now=ts), -side,
                    position.get('filled_ts', ts), cfg.tick, engine, timeframes=('15m',))
        if proof['confirmed'] and proof['known_ts'] > position.get('adverse_choch_ts', 0):
            position.update(adverse_choch_ts=proof['known_ts'], adverse_choch_level=proof['level'])
    if info['bars_in_trade'] >= STALE_BARS and info['max_favorable_r'] < STALE_MFE and abs(rr) <= .25 + EPS:
        stop = _stale_stop(position, snapshot, ts, price, cfg, engine)
        if stop is not None:
            position.update(stop=stop, stale_stop=stop, stale_stop_ts=ts)
            engine.emit(state, 'PAPER_STOP_AMENDED', ts, order_id=position['id'], stop=stop, reason='STALE_SWING_STOP')


def observe_quote(state, snapshot, cfg, engine):
    """Observe executable bid/ask after all intervening bars have reconciled."""
    if not snapshot.trusted or not 0 <= snapshot.now - snapshot.ticker_ts <= 120_000:
        return
    if not 0 < snapshot.bid <= snapshot.ask:
        return
    for number, view in engine.slot_views(state):
        p = view.get('active')
        if not p or p['risk'] <= 0:
            continue
        price = snapshot.bid if p['side'] == 1 else snapshot.ask
        closed_through = p.get('last_bar', -1) + p.get('last_execution_step_ms', 0)
        if snapshot.ticker_ts < max(closed_through, p.get('filled_ts', 0)):
            continue
        peak = track(p, snapshot.ticker_ts, price, price)
        if p['side'] * (price - p['stop']) <= 0:
            # The resting stop has priority. Let the normal lifecycle reconcile it.
            continue
        protect(view, p, snapshot, snapshot.ticker_ts, price, cfg, engine, new_peak=peak)
        engine.store_slot(state, number, view)


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
