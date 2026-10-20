"""Closed-bar preparation for native patterns and their 3m execution route.

Parent context and formation clock are explicit. A 3m precursor is never
represented as a confirmed 15m/1H/4H candle. No future bar is synthesized.
"""
from __future__ import annotations

TF = {'3m':180000, '15m':900000, '1H':3600000, '4H':14400000}


def causal_shift(rows, timeframe, engine, tick, lookback=6):
    """A crossing of a pivot available before the crossing candle opened."""
    points = engine.pivots(rows, 2)
    events = []
    step = TF[timeframe]
    for i in range(max(1, len(rows)-lookback), len(rows)):
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


def control_state(rows, timeframe, cfg, engine):
    """Separate the historical swing sequence from its latest accepted break.

    A HH/HL sequence is history after its protected low has broken. Waiting
    for two new lows AND two new highs kept the old LONG vote during a selloff.
    A break remains control until its level is reclaimed; all pivots must have
    been available before the crossing bar opened.
    """
    rows=list(rows)[-80:]
    swings=cfg.core.structure_snapshot(rows,60,2) if rows else {'direction':'NEUTRAL'}
    direction=swings['direction'];event={'shift':False,'side':'NEUTRAL','known_ts':0}
    if rows:
        broken=(direction=='LONG' and swings.get('last_swing_low',0)>0
                and rows[-1].close<swings['last_swing_low']-cfg.tick) or (
                direction=='SHORT' and swings.get('last_swing_high',0)>0
                and rows[-1].close>swings['last_swing_high']+cfg.tick)
        if broken or direction=='NEUTRAL':
            event=causal_shift(rows,timeframe,engine,cfg.tick,lookback=60 if broken else 6)
            if event['shift'] and (direction=='NEUTRAL' or event['side']!=direction):
                direction=event['side']
    accepted=event.get('shift') and event['side']==direction
    return {**event,'side':direction,
            'source':'CONFIRMED_PIVOT_BREAK' if accepted else 'SWING_SEQUENCE',
            'swing_direction':swings['direction']}


def snapshot_control(snapshot,timeframe,cfg,engine,as_of=None):
    cutoff=snapshot.now if as_of is None else as_of
    rows=[c for c in snapshot.candles.get(timeframe,[])
          if c.confirmed and c.ts+TF[timeframe]<=cutoff]
    key=(timeframe,cutoff,cfg.tick,tuple((c.ts,c.high,c.low,c.close) for c in rows[-80:]))
    cache=getattr(snapshot,'_closed_control_cache',None)
    if cache is None:
        cache={};snapshot._closed_control_cache=cache
    if key not in cache:
        cache[key]=control_state(rows,timeframe,cfg,engine)
    return cache[key]


def prepare(context, snapshot, cfg, engine, formation=None):
    out = dict(context)
    formation = formation or context.get('analysis_timeframe','15m')
    rows = [c for c in context.get('candles',{}).get('15m',[])
            if c.confirmed and c.ts+TF[formation]<=snapshot.now]
    if rows:
        out['structure_shift_15m'] = causal_shift(rows, formation, engine, cfg.tick)
        control=control_state(rows,formation,cfg,engine)
        out['swing_structure15']=dict(context.get('structure15',{}))
        out['structure15']={**context.get('structure15',{}),'direction':control['side'],
                            'direction_source':control['source']}
        out['market_control']=control
    micro=context.get('micro_timeframe','3m')
    child_rows=[c for c in context.get('candles',{}).get('3m',[])
                if c.confirmed and c.ts+TF[micro]<=snapshot.now]
    child=control_state(child_rows,micro,cfg,engine)
    out['structure3m']={**context.get('structure3m',{}),'direction':child['side'],
                       'direction_source':child['source']}
    first=snapshot_control(snapshot,'1H',cfg,engine)['side']
    fourth=snapshot_control(snapshot,'4H',cfg,engine)['side']
    state='ALIGNED' if first==fourth and first!='NEUTRAL' else 'UNKNOWN' if first==fourth=='NEUTRAL' else 'MIXED' if 'NEUTRAL' in (first,fourth) else 'AGAINST'
    out['htf_fact']={**context.get('htf_fact',{}),'direction_1h':first,'direction_4h':fourth,
                     'state':state,'score':100 if state=='ALIGNED' else 55 if state=='MIXED' else 0}
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
