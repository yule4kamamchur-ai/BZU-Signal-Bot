"""Experimental M15 reclaim/mean reversion. No active M1/M5 writes.
Prior 20 closes define a 2.2 sigma band. A closed candle sweeps the band
and reclaims it in the correct direction; the target is the prior mean.
All inputs are available at decision time. Parameters are hypotheses, not validated.
"""
from __future__ import annotations
import math
from statistics import mean, pstdev
from mexc_h1_trend import MINUTE, valid_bar, atr, tickround
import mexc_portfolio_intelligence as intel

STEP=15*MINUTE
SETUP='M15_MEAN_REVERSION_RECLAIM'


def signal(rows, now, n=20, sigma=2.2):
    q=sorted({int(b['t']):b for b in rows if int(b['t'])*1000+STEP<=now and valid_bar(b)}.values(),key=lambda b:b['t'])[-n-1:]
    if len(q)<n+1 or any((b['t']-a['t'])*1000!=STEP for a,b in zip(q,q[1:])): return None
    prev=q[:-1]; last=q[-1]; closes=[b['c'] for b in prev]; mu=mean(closes); sd=pstdev(closes); a=atr(q)
    if not a or a<=0 or sd<=0: return None
    upper=mu+sigma*sd; lower=mu-sigma*sd
    # Require actual reclaim, not just proximity to a band. Ignore ambiguous double sweeps.
    long=last['l']<lower<last['c']<mu and last['c']>last['o']
    short=last['h']>upper>last['c']>mu and last['c']<last['o']
    side=1 if long and not short else -1 if short and not long else 0
    path=sum(abs(y-x) for x,y in zip(closes,closes[1:])); efficiency=abs(closes[-1]-closes[0])/path if path else 0.
    reclaim=max(0.,side*(last['c']-(lower if side==1 else upper))/a) if side else 0.
    return {'side':side,'signal_ts':int(last['t'])*1000+STEP,'close':last['c'],'upper':upper,'lower':lower,'atr':a,
            'mean':mu,'swing':last['l'] if side==1 else last['h'],'efficiency':efficiency,
            'strength':reclaim*(1-.5*efficiency),'strategy':'RANGE_M15'}


def open_position(symbol,group,sig,price,now,*,tick,risk_budget,notional_budget,fee,slip,contract=None,require_contract=False):
    from mexc_h1_trend import SCHEMA
    s=int(sig['side']); p=float(price); a=float(sig['atr']); center=float(sig['mean'])
    if s not in (-1,1) or not all(math.isfinite(x) and x>0 for x in (p,a,tick,risk_budget,notional_budget,center)): return None,'INVALID_INPUT_OR_BUDGET'
    if not 0<=now-sig['signal_ts']<=STEP: return None,'STALE_M15_RECLAIM'
    band=sig['lower'] if s==1 else sig['upper']
    if s*(p-band)<=0 or s*(center-p)<=0: return None,'RECLAIM_NO_LONGER_VALID'
    if s*(p-sig['close'])>.5*a: return None,'PRICE_TOO_FAR'
    e=tickround(p,tick,s); raw_stop=min(e-1.6*a,sig['swing']-.25*a) if s==1 else max(e+1.6*a,sig['swing']+.25*a)
    stop=tickround(raw_stop,tick,-s); tp=tickround(center,tick,-s); risk=s*(e-stop); reward=s*(tp-e); cost=fee+slip
    if min(stop,tp,risk,reward)<=0: return None,'INVALID_RANGE_GEOMETRY'
    # Avoid targets consumed by costs. Geometry test, not a direction/TF gate.
    if reward-cost*(e+tp)<1.25*(risk+cost*(e+stop)): return None,'RANGE_INSUFFICIENT_NET_RR'
    sizing,reason=intel.size(e,stop,risk_budget,notional_budget,cost,contract,require_contract)
    if not sizing: return None,reason
    qty=sizing['qty']
    return {'symbol':symbol,'cluster':group,'setup':SETUP,'strategy':'RANGE_M15','model_schema':SCHEMA,'side':s,'entry':e,'stop':stop,'initial_stop':stop,'risk':risk,'tp':tp,'qty':qty,'notional':qty*e,'risk_usd':qty*risk,'reserve_usd':qty*(risk+cost*(e+stop)),
            'opened_ts':now,'processed_until_ts':now,'signal_ts':sig['signal_ts'],'stop_effective_ts':now,'atr_entry':a,'stop_mult':1.6,'trail_mult':0.,'tick':tick,'peak_price':e,'peak_r':0.,'mae_r':0.,'fee_rate':fee,'slippage_rate':slip,'quality':'OBSERVED_M1','funding_r':0.,'funding_status':'FUNDING_HISTORY_UNAVAILABLE',**sizing},'OPEN'
