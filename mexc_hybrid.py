"""Causal, multi-asset setup families and shrinkage ranking for MEXC paper trading.
Adapted concepts from BZU setup_paths / edge_intelligence / expectancy_model;
no transfer of oil-only thresholds or old OKX outcomes. No fitted edge claimed.
"""
from __future__ import annotations
import math
from collections import defaultdict
from statistics import mean

MODEL_SCHEMA = 'MEXC_HYBRID_15_0'
FAMILIES = {
    'M5_LIQUIDITY_SWEEP_RECLAIM': 'LIQUIDITY_SWEEP',
    'TREND_PULLBACK_FVG_VWAP': 'TREND_PULLBACK',
    'BREAKOUT_RETEST': 'BREAKOUT_RETEST',
    'MOMENTUM_NO_PULLBACK_CONTINUATION': 'MOMENTUM',
    'FAILED_BREAKOUT_TRAP': 'FAILED_BREAKOUT',
    'EXHAUSTION_REVERSAL': 'EXHAUSTION',
}


def trend_efficiency(rows, n=12):
    rows = rows[-(n+1):]
    if len(rows) < n+1:
        return 0, 0.
    closes = [b.close for b in rows]
    move = closes[-1] - closes[0]
    path = sum(abs(b-a) for a,b in zip(closes, closes[1:]))
    efficiency = abs(move)/path if path else 0.
    scale = mean(max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close)) for p,c in zip(rows,rows[1:]))
    side = (1 if move > 0 else -1) if scale and abs(move)/scale >= 1.25 and efficiency >= .31 else 0
    return side, efficiency


def detect(rows, *, side, atr, tick, min_rvol, trend, zone, timeframe):
    """All six families independently. Confirmation is a closed M1 or M5 bar.
    Return structural invalidation, event identity, and evidence; no look-ahead.
    """
    if len(rows) < 22 or atr <= 0:
        return []
    c = rows[-1]; p = rows[-2]; prev = rows[-11:-1]
    lo = min(x.low for x in prev); hi = max(x.high for x in prev)
    boundary = hi if side == 1 else lo
    opp = lo if side == 1 else hi
    body = side*(c.close-c.open)
    width = max(tick, c.high-c.low)
    wick = min(c.open,c.close)-c.low if side==1 else c.high-max(c.open,c.close)
    base_vol = mean(x.volume for x in rows[-21:-1]); rv = c.volume/base_vol if base_vol else 0.
    close_strength = side*(c.close-(c.high+c.low)/2) / width
    stop = c.low-tick if side==1 else c.high+tick
    out=[]
    def add(name, label, strength, anchor, *, retest=None):
        out.append({'setup':name,'family':FAMILIES[name], 'setup_ua':label,
                    'base_score':strength, 'structural_stop':anchor, 'reference':c.close,
                    'trigger_ts':c.ts, 'trigger_tf':timeframe, 'rvol':rv, 'retest_level':retest})
    swept = (c.low < lo-tick and c.close > lo+tick) if side==1 else (c.high > hi+tick and c.close < hi-tick)
    reject = wick/width >= .32 and body > 0 and close_strength >= .10
    if swept and body>=.35*atr and rv>=min_rvol and close_strength>=.10:
        add('M5_LIQUIDITY_SWEEP_RECLAIM',f'{timeframe} свіп + реклайм',76,stop)
    if trend and zone is not None and (c.low <= zone+.4*atr and c.high>=zone-.4*atr) and side*(c.close-zone)>=0 and (reject or body>=.45*atr) and rv>=.85:
        anchor=min(c.low,p.low,zone)-tick if side==1 else max(c.high,p.high,zone)+tick
        add('TREND_PULLBACK_FVG_VWAP',f'{timeframe} відкат до FVG/EMA/VWAP',74,anchor,retest=zone)
    # Break from a range that existed BEFORE the breakout, followed by a separate retest.
    for i in range(len(rows)-4,len(rows)-1):
        br=rows[i]; pre=rows[i-10:i]
        level=max(x.high for x in pre) if side==1 else min(x.low for x in pre)
        if side*(br.close-level)<.12*atr or side*(br.close-br.open)<.4*atr:
            continue
        between=rows[i+1:]
        touch = c.low<=level+.25*atr if side==1 else c.high>=level-.25*atr
        if touch and side*(c.close-level)>=.05*atr and body>0 and all(side*(x.close-level)>=-.35*atr for x in between):
            anchor=min(x.low for x in between)-tick if side==1 else max(x.high for x in between)+tick
            add('BREAKOUT_RETEST',f'{timeframe} пробій + ретест',76,anchor,retest=level)
            break
    if trend and side*(c.close-boundary)>=.12*atr and body>=.70*atr and close_strength>=.24 and rv>=min_rvol and side*(p.close-rows[-4].close)>=.15*atr:
        add('MOMENTUM_NO_PULLBACK_CONTINUATION',f'{timeframe} імпульс без відкату',77,stop)
    # Failed expansion in the opposite direction, then a close back inside the old range.
    pre=rows[-13:-2]
    level=min(x.low for x in pre) if side==1 else max(x.high for x in pre)
    if side*(p.close-level)<-.10*atr and side*(c.close-level)>.10*atr and body>=.45*atr and rv>=.95:
        anchor=min(p.low,c.low)-tick if side==1 else max(p.high,c.high)+tick
        add('FAILED_BREAKOUT_TRAP',f'{timeframe} хибний пробій / пастка',77,anchor)
    prior_move=side*(p.close-rows[-7].open)
    if prior_move <= -2.0*atr and reject and rv>=min_rvol and side*(c.close-p.open)>=.05*atr:
        anchor=min(x.low for x in rows[-4:])-tick if side==1 else max(x.high for x in rows[-4:])+tick
        add('EXHAUSTION_REVERSAL',f'{timeframe} виснаження + розворот',74,anchor)
    return out


def meta_estimate(history, candidate, as_of):
    """Only resolved prior MEXC v15 episodes. Shrink local mean toward global.
    Not calibrated probabilities: early samples are diagnostic, never a gate.
    """
    rows=[]; seen=set()
    for r in history:
        v=r.get('net_r'); end=r.get('closed_ts'); start=r.get('opened_ts'); ident=r.get('trade_id')
        if (r.get('model_schema')!=MODEL_SCHEMA or r.get('quality')!='OBSERVED_M1' or not ident or ident in seen
            or not isinstance(v,(int,float)) or not math.isfinite(v) or not isinstance(end,int) or not isinstance(start,int) or not start<end<as_of):
            continue
        seen.add(ident); rows.append(r)
    local=[r for r in rows if r.get('family')==candidate['family'] and r.get('regime')==candidate['regime']]
    n=len(local)
    result={'status':'COLLECTING_MEXC_LABELS','n':n,'total_n':len(rows),'win_rate_estimate':None,'expected_r_estimate':None,'rank_adjustment':0.}
    if len(rows)<40 or n<12:
        return result
    g=mean(r['net_r'] for r in rows); p=mean(r['net_r']>0 for r in rows)
    weight=n/(n+24.)
    exp=weight*mean(r['net_r'] for r in local)+(1-weight)*g
    win=weight*mean(r['net_r']>0 for r in local)+(1-weight)*p
    # Bounded influence; no hard admission or sizing decision from an unvalidated prior.
    result.update(status='EMPIRICAL_RANK_ONLY_UNVALIDATED',win_rate_estimate=round(win,4),expected_r_estimate=round(exp,4),rank_adjustment=round(max(-6,min(6,exp*6))*weight,3))
    return result
