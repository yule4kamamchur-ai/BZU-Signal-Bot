"""Causal one-direction-at-a-time portfolio policy and ICT confluence evidence.

This module never forecasts win rate. All observations come from closed candles
available at a decision instant. Mapped levels are not treated as confirmed
sweeps, and FVGs are not confused with liquidity events.
"""
from __future__ import annotations

from math import isfinite

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
    for c in rows[-6:]:
        if side*(c.close-c.open)>=.75*vol:
            span=max(c.high-c.low,tick)
            location=(c.close-c.low)/span if side==1 else (c.high-c.close)/span
            if location>=.70:
                impulse=True
                break
    sweep=bool(sweeps); fvg=bool(gaps)
    return {'timeframe':tf,'as_of_ts':snapshot.now,
            'sweep_reclaim':sweep,'latest_sweep':sweeps[-1] if sweep else None,
            'near_unmitigated_fvg':fvg,'nearest_fvg':min(gaps,key=lambda g:g['distance']) if fvg else None,
            'displacement':impulse,
            'ict_confluence_count':int(sweep)+int(fvg)+int(impulse),
            'proof_source':'CLOSED_CANDLES_ONLY'}


def admit(plan, proof):
    """Named claims need the named evidence; others stay eligible for ranking."""
    setup=plan.get('setup','')
    if setup in SWEEP_REQUIRED and not proof['sweep_reclaim']:
        return False, 'ICT_LIQUIDITY_SWEEP_NOT_CONFIRMED'
    if setup in RECLAIM_REQUIRED and not (proof['sweep_reclaim'] or proof['near_unmitigated_fvg']):
        return False, 'ICT_RECLAIM_LACKS_LIQUIDITY_OR_IMBALANCE'
    return True, 'CONFIRMED'


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
