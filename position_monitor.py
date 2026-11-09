"""Read-only, as-of structural review of a single filled position.

This is NOT a price forecast, position exit command, or training label. The
existing TP/SL lifecycle retains authority; review reports adverse structure
from already closed bars without retroactively adjusting risk or fills.
"""
from __future__ import annotations


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
