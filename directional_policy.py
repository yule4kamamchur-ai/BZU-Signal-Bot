"""Causal one-direction-at-a-time portfolio policy and ICT confluence evidence.

This module never forecasts win rate. All observations come from closed candles
available at a decision instant. Mapped levels are not treated as confirmed
sweeps, and FVGs are not confused with liquidity events.
"""
from __future__ import annotations

from math import isfinite
import ict_methodology

TF = {'3m':180_000, '15m':900_000, '1H':3_600_000, '4H':14_400_000}
# These detectors can propose a useful *location* without an ICT trade trigger.
# Retain their original scans, but do not promote a map to an executable trade.
SWEEP_REQUIRED = frozenset({'LIQUIDITY_LADDER', 'MSS_REVERSAL_SHORT'})
RECLAIM_REQUIRED = frozenset({'SESSION_MEAN_RECLAIM', 'DAILY_WEEKLY_OPEN_RECLAIM'})


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
    for i in range(max(20,len(rows)-8),len(rows)):
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
                         'high':round(high,8), 'distance':round(distance,8)})
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
    # Strictly historical confirmed pivot: two CLOSED right-hand bars are
    # required before a level may be broken. A close beyond the level alone
    # cannot retroactively establish that the pivot was known at that moment.
    shifts=[]
    if len(rows)>=12:
        for j in range(3,len(rows)-3):
            window=rows[j-2:j+3]
            pivot=(rows[j].high==max(b.high for b in window) if side==1
                   else rows[j].low==min(b.low for b in window))
            if not pivot:continue
            level=rows[j].high if side==1 else rows[j].low
            known=rows[j+2].ts+TF[tf]
            for k in range(max(j+3,len(rows)-8),len(rows)):
                if rows[k].ts < known:continue
                if side*(rows[k-1].close-level)<=0 and side*(rows[k].close-level)>tick:
                    shifts.append({'known_ts':rows[k].ts+TF[tf], 'level':round(level,8)})
    # The price must FIRST sweep/reclaim liquidity and only THEN break the
    # known structure. An FVG without this order is not an ICT sweep+MSS.
    sequence=any(t['known_ts']>w['known_ts'] for w in sweeps for t in shifts)
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
    return {'timeframe':tf,'as_of_ts':snapshot.now,
            'sweep_reclaim':sweep,'latest_sweep':sweeps[-1] if sweep else None,
            'near_unmitigated_fvg':fvg,'nearest_fvg':min(gaps,key=lambda g:g['distance']) if fvg else None,
            'displacement':impulse,'latest_displacement_known_ts':last_impulse_ts,
            'structure_shift':bool(shifts),
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
    against=int(plan.get('htf_1h',0)==-side)+int(plan.get('htf_4h',0)==-side)
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
    if second['side']!=first['side'] or second.get('event_key')==first.get('event_key'):
        return False
    # Do not double the exposure to one nearly identical price zone.
    risk=min(float(first.get('risk',0)),float(second.get('risk',0)))
    if abs(second['entry']-first['entry'])<=max(3*tick,.5*risk):
        return False
    return second.get('setup')!=first.get('setup') or second.get('setup_timeframe')!=first.get('setup_timeframe')
