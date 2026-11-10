"""Causal one-direction-at-a-time portfolio policy and ICT confluence evidence.

This module never forecasts win rate. All observations come from closed candles
available at a decision instant. Mapped levels are not treated as confirmed
sweeps, and FVGs are not confused with liquidity events.
"""
from __future__ import annotations

from math import isfinite
import ict_methodology
from dataclasses import replace

TF = {'3m':180_000, '15m':900_000, '1H':3_600_000, '4H':14_400_000}
# These detectors can propose a useful *location* without an ICT trade trigger.
# Retain their original scans, but do not promote a map to an executable trade.
SWEEP_REQUIRED = frozenset({'LIQUIDITY_LADDER', 'MSS_REVERSAL_SHORT'})
RECLAIM_REQUIRED = frozenset({'SESSION_MEAN_RECLAIM', 'DAILY_WEEKLY_OPEN_RECLAIM'})


def session_vwap(snapshot):
    """HLC3 VWAP from 00:00 UTC, requiring non-overlapping day coverage.

    Full 15m history plus available 1m tail avoids another network request.
    Missing midnight/history/volume never creates a partial-day A+.
    """
    start=snapshot.now//86_400_000*86_400_000
    end=snapshot.now//60_000*60_000
    minutes={c.ts:c for c in snapshot.candles.get('1m',[])
        if c.confirmed and start<=c.ts and c.ts%60_000==0 and c.ts+60_000<=end}
    quarters={c.ts:c for c in snapshot.candles.get('15m',[])
        if c.confirmed and start<=c.ts and c.ts%900_000==0 and c.ts+900_000<=end}
    base={'value':None,'previous_value':None,'slope':None,'start_ts':start,
          'known_ts':None,'status':'INCOMPLETE_SESSION','priority_points':0}
    rows=[];cursor=start
    while cursor<end:
        stop=min(cursor+900_000,end)
        fine=[minutes.get(ts) for ts in range(cursor,stop,60_000)]
        if fine and all(c is not None for c in fine):
            rows.extend(fine)
        elif stop-cursor==900_000 and cursor in quarters:
            rows.append(quarters[cursor])
        else:
            return base
        cursor=stop
    if not rows or not all(isfinite(c.volume) and c.volume>=0 for c in rows):
        return base
    volume=sum(c.volume for c in rows)
    if volume<=0:
        return {**base,'status':'NO_VOLUME'}
    weighted=sum((c.high+c.low+c.close)/3*c.volume for c in rows)
    value=weighted/volume
    prior_volume=volume-rows[-1].volume
    previous=(weighted-(rows[-1].high+rows[-1].low+rows[-1].close)/3*rows[-1].volume)/prior_volume if prior_volume>0 else value
    return {**base,'value':value,'previous_value':previous,'slope':value-previous,
            'known_ts':end,'status':'AVAILABLE','price_basis':'HLC3','volume':volume}


def vwap_priority(plan, snapshot, tick):
    result=session_vwap(snapshot)
    aligned=(result['value'] is not None and
             plan['side']*(plan['entry']-result['value'])>=-tick*.5)
    return {**result,'aligned':aligned,'priority_points':3 if aligned else 0,
            'rating':'A+' if aligned else None}


def active_structure(snapshot, tick, engine):
    """Prefer an intact, confirmed 15m break to a lagging HTF bias."""
    import strategy_organics as organic

    rows = organic.closed(snapshot, '15m')
    if len(rows) < 20 or snapshot.now - (rows[-1].ts + TF['15m']) >= 2 * TF['15m']:
        return {}
    if any(b.ts - a.ts != TF['15m'] for a, b in zip(rows[-20:-1], rows[-19:])):
        return {}
    from market_wave import fast_structure

    fast = fast_structure(rows, tick, engine)
    if fast:
        return fast
    breaks = list(organic.body_breaks(rows, tick, engine))
    if not breaks:
        return _trend_structure(snapshot, rows, tick, engine)
    index, side, level = breaks[-1]
    known = rows[index].ts + TF['15m']
    if snapshot.now - known >= 8 * TF['15m']:
        return _trend_structure(snapshot, rows, tick, engine)
    if any(side * (bar.close - level) <= 0 for bar in rows[index + 1:]):
        return _trend_structure(snapshot, rows, tick, engine)
    candle = rows[index]
    ema = organic.ema20(rows[:index + 1])
    previous_ema = organic.ema20(rows[:index])
    vwap = session_vwap(replace(snapshot, now=known))['value']
    previous_vwap = session_vwap(replace(snapshot, now=candle.ts))['value']
    volatility = max(tick, engine.atr(rows[:index]))
    slope_floor = max(tick, .04 * volatility)
    slope = any(current is not None and previous is not None
                and side * (current - previous) >= slope_floor
                for current, previous in ((ema, previous_ema), (vwap, previous_vwap)))
    range_atr = (candle.high - candle.low) / volatility
    if not (slope or range_atr >= 1.1) or not momentum_aligned(snapshot, side, max_age=2):
        return {}
    if not any(line is not None and side * (candle.close - line) > 0 for line in (ema, vwap)):
        return {}
    return {'side': side, 'known_ts': known, 'level': level, 'close': candle.close,
            'ema20': ema, 'vwap': vwap, 'qualified_slope': slope, 'break_range_atr': range_atr}


def _trend_structure(snapshot, rows, tick, engine):
    """Keep an established M15 trend while both swings and EMA agree."""
    import strategy_organics as organic

    points = engine.pivots(rows[-32:], strength=1)
    highs = [p for p in points if p['kind'] == 'HIGH'][-2:]
    lows = [p for p in points if p['kind'] == 'LOW'][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return {}
    average, previous = organic.ema20(rows), organic.ema20(rows[:-1])
    if previous is None:
        return {}
    slope_floor = max(tick, .04 * engine.atr(rows[:-1]))
    for side, bounds in ((1, lows), (-1, highs)):
        level = bounds[-1]['level']
        if (all(side * (pair[-1]['level'] - pair[0]['level']) >= tick for pair in (highs, lows))
                and side * (average - previous) >= slope_floor
                and all(side * (price - average) > 0 and side * (price - level) > 0
                        for price in (rows[-1].close, snapshot.price))):
            return {'side':side, 'known_ts':max(highs[-1]['known_ts'],lows[-1]['known_ts']) + TF['15m'],
                    'level':level, 'close':rows[-1].close, 'ema20':average, 'qualified_slope':True,
                    'proof':'CONTINUING_SWING_STRUCTURE'}
    return {}


def momentum_aligned(snapshot, side, max_age=1):
    import strategy_organics as organic

    rows = organic.closed(snapshot, '15m')
    ema = organic.ema20(rows)
    if ema is None or snapshot.now - (rows[-1].ts + TF['15m']) >= max_age * TF['15m']:
        return False
    vwap = session_vwap(snapshot)['value']
    return any(line is not None and side * (rows[-1].close - line) > 0
               and side * (snapshot.price - line) > 0 for line in (ema, vwap))


def day_extrema(snapshot):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo('Europe/Kyiv')
    day = datetime.fromtimestamp(snapshot.now / 1000, tz).date()
    rows = [c for c in closed(snapshot, '15m')
            if datetime.fromtimestamp(c.ts / 1000, tz).date() == day]
    if not rows:
        return []
    return [{'name': 'DAY_' + kind, 'level': getattr(candle, kind.lower()),
             'known_ts': candle.ts + TF['15m']}
            for kind, candle in (('LOW', min(rows, key=lambda c: c.low)),
                                 ('HIGH', max(rows, key=lambda c: c.high)))]


def regime_side(regime):
    state = regime.get('state', '')
    return 1 if state in ('BULLISH_TREND', 'BULLISH_ACTIVE') else (
        -1 if state in ('BEARISH_TREND', 'BEARISH_ACTIVE') else 0)


def market_regime(snapshot, tick, engine):
    import strategy_organics
    r=strategy_organics.classify(snapshot)
    one=engine.bias(closed(snapshot,'1H'))['side'] or r.trend_1h
    four=engine.bias(closed(snapshot,'4H'))['side'] or r.trend_4h
    if one and four in (0,one): label='BULLISH_TREND' if one==1 else 'BEARISH_TREND'
    elif one==0 and four: label='BULLISH_TREND' if four==1 else 'BEARISH_TREND'
    else: label='RANGE_LIQUIDITY_ROTATION'
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day=datetime.fromtimestamp(snapshot.now/1000,ZoneInfo('Europe/Kyiv')).date()
    today=[c for c in closed(snapshot,'15m') if datetime.fromtimestamp(c.ts/1000,ZoneInfo('Europe/Kyiv')).date()==day]
    rows=today or closed(snapshot,'15m')[-32:]
    local=local_structure(snapshot,tick,engine)
    conflict=local['side'] and (one or four)==-local['side']
    active = active_structure(snapshot, tick, engine)
    state = ('BULLISH_ACTIVE' if active['side'] == 1 else 'BEARISH_ACTIVE') if active else (
        'PULLBACK_PHASE' if conflict else label)
    return {'state':state,'trend_state':label,'active_structure':active,
            'local_structure':local,'side_1h':one,'side_4h':four,'as_of_ts':snapshot.now,
            'range_low':min((c.low for c in rows),default=snapshot.price),
            'range_high':max((c.high for c in rows),default=snapshot.price)}


def execution_regime(snapshot, cfg, engine):
    """Keep the confirmed hourly direction through an intraday retracement."""
    import oil_risk

    regime = market_regime(snapshot, cfg.tick, engine)
    if not oil_risk.enabled(cfg):
        return regime
    one, four = regime.get('side_1h', 0), regime.get('side_4h', 0)
    side = one or four
    if not side or (one and four and one != four):
        return regime
    m15 = closed(snapshot, '15m')
    local = regime.get('active_structure', {}).get('side') or regime.get('local_structure', {}).get('side')
    pullback = local == -side or (len(m15) >= 3 and side * (snapshot.price - m15[-3].close) < -cfg.tick)
    return {**regime, 'state': 'BULLISH_ACTIVE' if side == 1 else 'BEARISH_ACTIVE',
            'trend_anchor_side': side, 'trend_anchor_timeframe': '1H' if one else '4H',
            'trend_phase': ('BULLISH_PULLBACK_OPPORTUNITY' if side == 1 else 'BEARISH_PULLBACK_OPPORTUNITY')
                           if pullback else 'TREND_CONTINUATION'}


def structural_flip(snapshot, side, since, tick, engine, timeframes=('15m', '1H')):
    """Only a newly observed CLOSED 15m/1H BOS authorizes a pending flip."""
    for tf in timeframes:
        rows=closed(snapshot,tf)
        points = engine.pivots(rows, strength=1 if tf == '15m' else 2)
        for i in range(max(7,len(rows)-3),len(rows)):
            known = rows[i].ts + TF[tf]
            if known <= since:
                continue
            pivot = ict_methodology.body_break(rows, i, points, side, tick, TF[tf])
            if pivot:
                return {'confirmed':True,'timeframe':tf,'known_ts':known,'side':side,'level':pivot['level']}
    return {'confirmed':False,'side':side}


def local_structure(snapshot, tick, engine):
    rows=closed(snapshot,'15m')
    if len(rows)<50:
        return {'side':0,'ema50':None,'known_ts':None}
    ema=rows[0].close
    for c in rows[1:]: ema+=(c.close-ema)*2/51
    points=engine.pivots(rows)
    for sign,kind in ((-1,'LOW'),(1,'HIGH')):
        swings=[p for p in points if p['kind']==kind and p['known_ts']+900_000<=snapshot.now][-3:]
        if (len(swings)==3 and all(sign*(b['level']-a['level'])>tick for a,b in zip(swings,swings[1:]))
                and sign*(rows[-1].close-ema)>tick):
            since=swings[-1]['known_ts']+900_000
            if not structural_flip(snapshot,-sign,since,tick,engine)['confirmed']:
                return {'side':sign,'ema50':ema,'known_ts':since,'swing_levels':[p['level'] for p in swings]}
    return {'side':0,'ema50':ema,'known_ts':rows[-1].ts+900_000}


def reversal_sweep_override(side, zone, snapshot, tick):
    """Use a fresh witnessed wick/reclaim to override only the local wave."""
    zone = zone or {}
    sweep = zone.get('sweep') or {}
    if (zone.get('kind') not in ('SWEEP_REVERSAL', 'TURTLE_SOUP')
            or sweep.get('side') != side or sweep.get('pattern') != 'WICK_RECLAIM'
            or not 0 <= snapshot.now - zone.get('known_ts', 0) < 30 * 60_000):
        return False
    timeframe = (zone.get('wave_proof') or {}).get('timeframe', '15m')
    if timeframe == '5m':
        from setup_scanner import closed_five_minutes

        rows, step = closed_five_minutes(snapshot), 300_000
    else:
        rows, step = closed(snapshot, '15m'), 900_000
    raid_ts = sweep.get('raid_ts', 0)
    if not 0 < sweep.get('reference_known_ts', 0) <= raid_ts or raid_ts + step != zone['known_ts']:
        return False
    candle = next((bar for bar in reversed(rows) if bar.ts == raid_ts and bar.confirmed), None)
    return bool(candle and ict_methodology.wick_reclaim(candle, side, sweep['level'], tick) >= .45 - 1e-8
                and side * (snapshot.price - sweep['extreme']) > tick / 2)


def regime_admission(side, entry, regime, snapshot, tick, engine, zone=None, *, require_macro=True):
    from market_wave import reversal_override

    if reversal_override(side, zone, snapshot, tick, require_macro=require_macro):
        return True, 'CONFIRMED_EXTREME_REVERSAL'
    local_override = reversal_sweep_override(side, zone, snapshot, tick)
    macro_opposes = (regime.get('trend_anchor_side') == -side or
                     regime.get('side_1h') == regime.get('side_4h') == -side)
    if local_override and not macro_opposes:
        return True, 'REVERSAL_SWEEP_LOCAL_OVERRIDE'
    if require_macro:
        if regime.get('trend_anchor_side') == side:
            return True, 'HTF_TREND_ANCHOR'
        dominant = regime_side(regime) or regime_side({'state': regime.get('trend_state')})
        if dominant == -side or (not dominant and regime.get('side_1h') == regime.get('side_4h') == -side):
            return False, 'MACRO_REVERSAL_REQUIRED'
    else:
        sweep = (zone or {}).get('sweep')
        if sweep and sweep.get('pattern') == 'ENGULFING' and sweep.get('side') == side:
            return True, 'SESSION_SWEEP_ENGULFING'
    active = regime.get('active_structure', {}).get('side', 0)
    if active == side:
        return True, 'LTF_STRUCTURE_OVERRIDE'
    if active == -side:
        return False, 'LOCAL_STRUCTURE_CONFLICT'
    state=regime.get('trend_state',regime['state'])
    if regime['state']=='PULLBACK_PHASE' and side==-regime['local_structure']['side']:
        reached_htf=bool(zone and zone.get('timeframe') in ('1H','4H')
                         and zone['low']<=snapshot.price<=zone['high'])
        if not reached_htf:
            return False,'LOCAL_STRUCTURE_CONFLICT'
    contrary=(state=='BULLISH_TREND' and side!=1) or (state=='BEARISH_TREND' and side!=-1)
    if contrary and not require_macro:
        # A high score alone cannot unlock a countertrend order. Require a
        # real reversal on 1H, or a reached HTF POI with a 15m sweep then MSS.
        template={'side':side,'entry':entry}
        h1=evidence({**template,'formation_timeframe':'1H'},snapshot,tick)
        m15=evidence({**template,'formation_timeframe':'15m'},snapshot,tick)
        reached=bool(zone and zone.get('timeframe') in ('1H','4H')
                     and zone['low']<=snapshot.price<=zone['high'])
        reversal=(h1['structure_shift'] and (h1['sweep_then_shift'] or h1['displacement_bos_fvg']))
        if not (reversal or (reached and m15['sweep_then_shift'])):
            return False,'REGIME_LONG_ONLY' if state=='BULLISH_TREND' else 'REGIME_SHORT_ONLY'
    if state=='RANGE_LIQUIDITY_ROTATION':
        if (zone and zone.get('kind') in ('BREAKER_FLIP_15M', 'DISPLACEMENT_CATCHER_15M')
                and momentum_aligned(snapshot, side)):
            return True, 'ORGANIC_M15_MOMENTUM'
        width=max(regime['range_high']-regime['range_low'],tick)
        location=(entry-regime['range_low'])/width
        if .30<=location<=.70: return False,'RANGE_MIDDLE_BLOCKED'
        # Only the correct range edge, after a witnessed liquidity raid.
        if (side==1 and location>.30) or (side==-1 and location<.70):
            return False,'RANGE_WRONG_EDGE'
        proof=evidence({'side':side,'entry':entry,'formation_timeframe':'15m'},snapshot,tick)
        if not (proof['sweep_reclaim'] or (zone and zone.get('kind') == 'SWEEP_RECLAIM_ZONE')
                or ict_methodology.session_sweeps(snapshot,side,tick,engine)):
            return False,'RANGE_SWEEP_REQUIRED'
    return True,'CONFIRMED'


def closed(snapshot, tf):
    now = snapshot.now
    return [c for c in snapshot.candles.get(tf, [])
            if c.confirmed and c.ts + TF[tf] <= now]


def _atr(rows, period=14):
    if len(rows) < 2:
        return 0.
    spans = [max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
             for p,c in zip(rows[-period-1:-1],rows[-period:])]
    return sum(spans)/len(spans) if spans else 0.


def evidence(plan, snapshot, tick):
    """Geometric FVG, raid/reclaim and displacement with strict as-of timing.

    Sweep reference is from candles strictly BEFORE the event; an unclosed
    forming bar, last trade quote, and subsequently revised OHLC are excluded.
    """
    side=int(plan['side']); entry=float(plan['entry'])
    tf=plan.get('formation_timeframe',plan.get('setup_timeframe','15m'))
    if tf not in TF:
        tf='15m'
    rows=closed(snapshot,tf)[-90:]
    vol=max(_atr(rows),tick)
    sweeps=[]
    for i in range(max(20,len(rows)-(18 if tf=='15m' else 8)),len(rows)):
        c=rows[i]; previous=rows[max(0,i-20):i]
        if len(previous)<20:
            continue
        level=min(x.low for x in previous) if side==1 else max(x.high for x in previous)
        crossed=(c.low<=level-tick if side==1 else c.high>=level+tick)
        reclaimed=side*(c.close-level)>tick*.5
        if crossed and reclaimed and abs(level-entry)<=max(vol*1.5,3*tick):
            sweeps.append({'known_ts':c.ts+TF[tf], 'level':round(level,8),
                           'extreme':round(c.low if side==1 else c.high,8)})
    gaps=[]
    for i in range(max(2,len(rows)-24),len(rows)):
        left,mid,right=rows[i-2:i+1]
        low,high=(left.high,right.low) if side==1 else (right.high,left.low)
        if high-low < max(2*tick,.10*vol):
            continue
        # Wick-to-wick 3-candle imbalance; never use unconfirmed candles.
        middle_body=side*(mid.close-mid.open)
        if middle_body < .60*vol:
            continue
        later=rows[i+1:]
        # A zone is spent only when price travels THROUGH the distal edge.
        used=any(c.low<=low if side==1 else c.high>=high for c in later)
        if used:
            continue
        distance=0. if low<=entry<=high else min(abs(entry-low),abs(entry-high))
        if distance<=max(.70*vol,3*tick):
            gaps.append({'known_ts':right.ts+TF[tf], 'low':round(low,8),
                         'high':round(high,8), 'distance':round(distance,8),
                         'displacement_known_ts':mid.ts+TF[tf],
                         'strong_displacement':middle_body>=.75*vol})
    impulse=False
    last_impulse_ts=0
    for c in rows[-6:]:
        if side*(c.close-c.open)>=.75*vol:
            span=max(c.high-c.low,tick)
            location=(c.close-c.low)/span if side==1 else (c.high-c.close)/span
            if location>=.70:
                impulse=True
                last_impulse_ts=c.ts+TF[tf]

    sweep=bool(sweeps); fvg=bool(gaps)
    # M15 can use one closed right bar. The pivot still predates the break.
    shifts=[]
    if len(rows)>=12:
        right = 1 if tf == '15m' else 2
        for j in range(2,len(rows)-right-1):
            window=rows[j-2:j+right+1]
            pivot=(rows[j].high==max(b.high for b in window) if side==1
                   else rows[j].low==min(b.low for b in window))
            if not pivot:continue
            level=rows[j].high if side==1 else rows[j].low
            known=rows[j+right].ts+TF[tf]
            for k in range(max(j+right+1,len(rows)-8),len(rows)):
                if rows[k].ts < known:continue
                if (rows[k].ts - rows[k-1].ts == TF[tf]
                        and side*(rows[k-1].close-level)<=0
                        and side*(rows[k].open-level)<=tick*.5
                        and side*(rows[k].close-level)>tick):
                    shifts.append({'known_ts':rows[k].ts+TF[tf], 'level':round(level,8)})
    # The price must FIRST sweep/reclaim liquidity and only THEN break the
    # known structure. An FVG without this order is not an ICT sweep+MSS.
    sequence=any(0 < t['known_ts']-w['known_ts'] <= ict_methodology.SWEEP_TO_BOS_M15_BARS * TF[tf]
                 for w in sweeps for t in shifts)
    # A rejection is a completed wick-through and close-back at the PLANNED
    # entry level; a nearby price quote without a candle is not a rejection.
    rejected=[]; reclaimed=[]; accepted=[]
    for i in range(max(1,len(rows)-8),len(rows)):
        c,previous=rows[i],rows[i-1]
        hit=(c.low<entry-tick if side==1 else c.high>entry+tick)
        ret=side*(c.close-entry)>tick*.5
        wick=(min(c.open,c.close)-c.low if side==1 else c.high-max(c.open,c.close))
        if hit and ret and wick>=max(.12*vol,2*tick):
            rejected.append(c.ts+TF[tf])
        if side*(previous.close-entry)<-tick and ret:
            reclaimed.append(c.ts+TF[tf])
        if side*(previous.close-entry)<=tick and ret and \
                side*(c.close-c.open)>=.50*vol:
            accepted.append(c.ts+TF[tf])
    # Breakout acceptance also recognizes 2 consecutive closed candles on
    # the breakout side of a pre-mapped reference. This is *not* inferred from
    # the current unconfirmed quote.
    if len(rows)>=8 and any(side*(c.close-entry)<=0 for c in rows[-8:-2]) and \
            all(side*(c.close-entry)>tick for c in rows[-2:]):
        accepted.append(rows[-1].ts+TF[tf])
    bos_fvg=any(g['strong_displacement'] and
                g['displacement_known_ts']<=b['known_ts']<=g['known_ts']
                for g in gaps for b in shifts)
    return {'timeframe':tf,'as_of_ts':snapshot.now,
            'sweep_reclaim':sweep,'latest_sweep':sweeps[-1] if sweep else None,
            'near_unmitigated_fvg':fvg,'nearest_fvg':min(gaps,key=lambda g:g['distance']) if fvg else None,
            'displacement':impulse,'latest_displacement_known_ts':last_impulse_ts,
            'structure_shift':bool(shifts),
            'displacement_bos_fvg':bos_fvg,
            'latest_structure_shift':shifts[-1] if shifts else None,
            'sweep_then_shift':sequence,'level_rejection':bool(rejected),
            'reference_reclaim':bool(reclaimed),'break_acceptance':bool(accepted),
            'ict_confluence_count':int(sweep)+int(fvg)+int(impulse)+int(bool(shifts)),
            'proof_source':'CLOSED_CANDLES_ONLY'}


def admit(plan, proof):
    """Every original detector has a distinct, auditable ICT contract."""
    accepted,reason,details=ict_methodology.validate(plan,proof)
    plan['ict_contract']=details
    # Preserve the historical external reason for the original liquidity map.
    if not accepted and plan.get('setup')=='LIQUIDITY_LADDER' and not proof.get('sweep_reclaim'):
        reason='ICT_LIQUIDITY_SWEEP_NOT_CONFIRMED'
    return accepted,reason


def adjusted_score(plan, proof):
    """Comparative score (not a probability or a fitted trading parameter)."""
    support=4.*int(proof['sweep_reclaim']) + 5.*int(proof['near_unmitigated_fvg']) + 3.*int(proof['displacement'])
    side=plan['side']
    htf=int(plan.get('htf_1h',0)==side)+int(plan.get('htf_4h',0)==side)
    against=0 if plan.get('reversal_sweep_override') else int(plan.get('htf_1h',0)==-side)+int(plan.get('htf_4h',0)==-side)
    support+=3.*htf-4.*against
    if against and not proof['sweep_reclaim'] and not proof['displacement']:
        support-=5.
    base=float(plan.get('decision_score',plan.get('score',0)) or 0)
    return round(base+support,4)


def ranking(plan, price, tick):
    score=float(plan.get('direction_score',plan.get('decision_score',plan.get('score',0))) or 0)
    distance=abs(plan.get('entry',price)-price)/max(plan.get('risk',tick),tick)
    decision=plan.get('meta_decision') or {}
    if decision.get('validated') and decision.get('expected_net_r') is not None:
        # Once a probability model has cleared chronological holdout, rank
        # competing entries by estimated *net* return, not arbitrary score.
        expected=float(decision['expected_net_r'])
        if isfinite(expected):
            return (100.+max(-50.,min(50.,10.*expected)),score,
                    -distance,str(plan.get('id','')))
    return (score-min(12.,3.*distance),float(plan.get('net_rr',0)),
            -distance,str(plan.get('id','')))


def choose(plans, price, tick, margin=8., locked_side=0):
    """One directional thesis per cycle; abstain on close opposite scores."""
    ranked=sorted(plans,key=lambda p:ranking(p,price,tick),reverse=True)
    if locked_side in (-1,1):
        return locked_side,[p for p in ranked if p['side']==locked_side], 'OPEN_BOOK_DIRECTION_LOCK'
    if not ranked:
        return 0,[], 'NO_VALID_ICT_SCENARIO'
    winner=ranked[0]
    rival=next((p for p in ranked if p['side']!=winner['side']),None)
    # No compelling directional edge => abstain, not a coin flip.
    # Only the best INDEPENDENT opposing side counts as a rival.
    proven=bool(rival and winner.get('meta_decision',{}).get('validated')
                and rival.get('meta_decision',{}).get('validated'))
    if proven:
        # Expected-net-R is the first ranking component scaled by ten.
        close=(ranking(winner,price,tick)[0]-ranking(rival,price,tick)[0]<1.2)
    else:
        # Four comparative score points is a tie; the wider user-configured
        # margin remains the required hysteresis to switch pending sides.
        close=bool(rival and ranking(winner,price,tick)[0]-ranking(rival,price,tick)[0]<min(4.,margin))
    if close:
        return 0,[], 'DIRECTION_AMBIGUOUS_NO_TRADE'
    return winner['side'],[p for p in ranked if p['side']==winner['side']], 'DIRECTION_SELECTED' 


def compatible(second, first, tick):
    """Conservative independence test for a SECOND risk-bearing entry.

    Comparing the detector name is not sufficient: two different detectors
    may describe the same liquidity sweep, level or parent HTF event.
    Conversely, two truly distinct confirmed events may share a setup name.
    Missing event identity cannot be used as positive proof of independence.
    """
    if second.get('side') not in (-1, 1) or second.get('side') != first.get('side'):
        return False
    if not second.get('event_key') or not first.get('event_key'):
        return False
    if second['event_key'] == first['event_key']:
        return False
    identities = lambda p: {str(k) for k in (p.get('event_key'), p.get('zone_key'),
                         *(p.get('parent_keys') or [])) if k}
    if identities(first) & identities(second):
        return False
    # Shared origin of an actual sweep/BOS, when the source records it.
    for key in ('strategy_event_ts', 'episode_ts'):
        a = (first.get('anchor_proof') or {}).get(key, first.get(key))
        b = (second.get('anchor_proof') or {}).get(key, second.get(key))
        if a is not None and b is not None and a == b:
            return False
    try:
        risk=min(float(first['risk']), float(second['risk']))
        entry_gap=abs(float(second['entry'])-float(first['entry']))
        if not isfinite(risk) or not isfinite(entry_gap) or risk <= 0:
            return False
        if entry_gap <= max(3*tick, .5*risk):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return True
