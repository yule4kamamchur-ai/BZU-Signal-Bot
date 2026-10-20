"""Closed-bar preparation for native patterns and their 3m execution route.

Parent context and formation clock are explicit. A 3m precursor is never
represented as a confirmed 15m/1H/4H candle. No future bar is synthesized.
"""
from __future__ import annotations

TF = {'3m':180000, '15m':900000, '1H':3600000, '4H':14400000}


def causal_shift(rows, timeframe, engine, tick):
    """A crossing of a pivot available before the crossing candle opened."""
    points = engine.pivots(rows, 2)
    events = []
    step = TF[timeframe]
    for i in range(max(1, len(rows)-6), len(rows)):
        previous, current = rows[i-1:i+1]
        for side, kind in ((1,'HIGH'),(-1,'LOW')):
            known = [p for p in points if p['kind']==kind and p['known_ts']+step<=current.ts]
            if not known:
                continue
            pivot = known[-1]; level = pivot['level']
            if (side*(previous.close-level)<=0 and side*(current.close-level)>=tick
                    and side*(rows[-1].close-level)>0):
                events.append({'shift':True,'side':'LONG' if side==1 else 'SHORT',
                    'level':level,'age_bars':len(rows)-1-i,'kind':'CAUSAL_PIVOT_CROSS',
                    'pivot_ts':pivot['ts'],'known_ts':current.ts+step,
                    'pivot_known_ts':pivot['known_ts']+step})
    return events[-1] if events else {'shift':False,'side':'NEUTRAL','level':0.,'age_bars':0}


def prepare(context, snapshot, cfg, engine, formation=None):
    out = dict(context)
    formation = formation or context.get('analysis_timeframe','15m')
    rows = list(context.get('candles',{}).get('15m',[]))
    if rows:
        out['structure_shift_15m'] = causal_shift(rows, formation, engine, cfg.tick)
    out['formation_timeframe'] = formation
    out['formation_micro_timeframe'] = context.get('micro_timeframe','3m')
    return out


def micro_context(snapshot, parent, state, cfg, engine):
    """All 24 actual pattern detectors can inspect the latest closed 3m bars."""
    bars = {**snapshot.candles, '15m':snapshot.candles['3m']}
    data = {**snapshot.metadata,'price':snapshot.price,'spread':snapshot.ask-snapshot.bid,
        'candles':bars,'execution_price_trusted':snapshot.trusted,
        'ticker_ts':snapshot.ticker_ts,'instrument':cfg.instrument}
    context = cfg.core.build_context(data,state,{'trades':state.get('trades',[])})
    # Session levels and VWAP retain the real 15m session, not a relabeled 3m OR.
    for key in ('session','session_name','vwap','day_open','week_open','prev_day_high','prev_day_low','smt'):
        if key in parent:
            context[key] = parent[key]
    context.update(analysis_timeframe='15m',micro_timeframe='3m',
                   entry_route='MICRO_PRECURSOR',formation_timeframe='3m')
    return prepare(context,snapshot,cfg,engine,'3m')


def frozen_week(context):
    """Freeze this week's extrema BEFORE the detector's six-bar event window."""
    rows = context.get('candles',{}).get('15m',[])
    hours = context.get('candles',{}).get('1H',[])
    session = context.get('session',{})
    start = session.get('week_ts',0)
    if len(rows)<6 or not start or not session.get('week_complete'):
        return None
    cutoff = rows[-6].ts
    prior = [c for c in hours if start<=c.ts and c.ts+TF['1H']<=cutoff]
    if not prior or prior[0].ts!=start:
        return None
    return {'week_high':max(c.high for c in prior),'week_low':min(c.low for c in prior),
            'reference_as_of_ts':cutoff}


def entry_rank(plan, price, scanner, tick):
    key = scanner.selection_key(plan,price)
    gap_r = abs(price-plan['entry'])/max(plan.get('risk',tick),tick)
    return (key[0],key[1]-min(30.,10.*gap_r),*key[2:])


def passive_at_open(plan, price, tick):
    # At this point only OPEN is known; no bar high, low or close is inspected.
    return plan['side']*(price-plan['entry'])>=-tick*1e-8


def control_reversed(plan, snapshot, cfg, engine):
    """Cancel an unfilled continuation after a NEW confirmed execution flip.

    A normal countertrend pullback that existed at placement is not silently
    redefined as an invalid setup. Active trades are never passed here.
    """
    contract=engine.quality.CONTRACTS.get(plan.get('setup'))
    if not contract or contract[1] not in ('continuation','breakout'):
        return False
    primary=plan.get('formation_timeframe',plan.get('setup_timeframe','15m'))
    if primary not in TF:primary='15m'
    micro=plan.get('formation_micro_timeframe',{'3m':'3m','15m':'3m','1H':'15m','4H':'1H'}[primary])
    if any(len(snapshot.candles.get(tf,[]))<12 for tf in (primary,micro)):
        return False
    rows=[c for c in snapshot.candles.get(primary,[]) if c.confirmed and c.ts+TF[primary]<=snapshot.now]
    smaller=[c for c in snapshot.candles.get(micro,[]) if c.confirmed and c.ts+TF[micro]<=snapshot.now]
    cross=causal_shift(rows,primary,engine,cfg.tick)
    other=-plan['side']
    return bool(cross.get('shift') and cfg.core.side_sign(cross['side'])==other
        and cross['known_ts']>plan['placed_ts']
        and cfg.core.side_sign(cfg.core.structure_snapshot(smaller,60,2)['direction'])==other)
