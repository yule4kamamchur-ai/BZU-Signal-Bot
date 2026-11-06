"""Read-only, as-of structural review of a single filled position.

This is NOT a price forecast, position exit command, or training label. The
existing TP/SL lifecycle retains authority; review reports adverse structure
from already closed bars without retroactively adjusting risk or fills.
"""
from __future__ import annotations


import strategy_organics

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
            'regime': regime.state, 'as_of_ts': snapshot.now}


def structural_stop(position, snapshot, as_of, close, tick, engine):
    """A completed post-fill M15 swing can tighten the next execution bar."""
    step = STEPS['15m']
    side = position['side']
    cache = vars(snapshot).setdefault('_position_pivots', {})
    if step not in cache:
        rows = strategy_organics.closed(snapshot, '15m')
        starts = {c.ts for c in rows}
        cache[step] = [p for p in engine.pivots(rows, strength=1)
                       if p['known_ts'] == p['ts'] + step and p['ts'] - step in starts]
    kind = 'LOW' if side == 1 else 'HIGH'
    filled = position['filled_ts']
    swing = next((p for p in reversed(cache[step]) if p['kind'] == kind
                  and filled < p['ts'] and p['known_ts'] + step <= as_of), None)
    if swing is None:
        return None
    candidate = engine.round_tick(round(swing['level'] - side*tick, 10), tick, up=side == -1)
    if (side*(candidate-position['entry']) > 0
            and side*(candidate-position['stop']) > tick*.5
            and side*(close-candidate) > tick*.5):
        return candidate
    return None
