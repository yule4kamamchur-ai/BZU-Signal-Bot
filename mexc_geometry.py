"""Causal SL/TP geometry and confirmed structural trailing, v15.1 PAPER.
All coefficients are research defaults, not optimized or a promise of edge.
"""
from __future__ import annotations
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

POLICY = 'STRUCTURAL_V15_1'
# M5 floor, M15 floor, ATR M5 buffer behind invalidation, preferred target R.
PROFILES = {
    'UKOIL_USDT': (1.70,1.40,.25,2.5), 'XPT_USDT': (1.65,1.35,.25,2.4),
    'SOL_USDT': (1.60,1.25,.25,2.5), 'LINK_USDT': (1.60,1.25,.25,2.5),
    'LTC_USDT': (1.65,1.30,.25,2.4), 'APT_USDT': (1.75,1.35,.30,2.5),
    'ORDI_USDT': (1.80,1.40,.30,2.5),
}
FAMILY_TARGET={'LIQUIDITY_SWEEP':2.25,'TREND_PULLBACK':2.5,'BREAKOUT_RETEST':2.6,
               'MOMENTUM':2.8,'FAILED_BREAKOUT':2.25,'EXHAUSTION':2.2}


def directional_round(price, tick, direction):
    p,t=Decimal(str(price)),Decimal(str(tick))
    return float((p/t).to_integral_value(rounding=ROUND_CEILING if direction>0 else ROUND_FLOOR)*t)


def pivots(rows, side, depth=2, prominence=0.):
    """Confirmed pivots only; no unclosed or future bars."""
    out=[]
    for i in range(depth,len(rows)-depth):
        b=rows[i]; other=rows[i-depth:i]+rows[i+1:i+depth+1]
        x=b.high if side==1 else b.low
        if all(side*(x-(r.high if side==1 else r.low))>0 for r in other):
            left=rows[i-depth:i+1]; right=rows[i:i+depth+1]
            # Avoid treating every tiny local high/low as a major target.
            distance=min(side*(x-(min(r.low for r in leg) if side==1 else max(r.high for r in leg))) for leg in (left,right))
            if distance>=prominence: out.append(x)
    return out


def geometry(spec, thesis, entry, side, tick, a5, a15, m15, h1, fee, slip, aligned):
    mult5,mult15,buffer_mult,preferred=PROFILES[spec['symbol']]
    anchor=float(thesis['structural_stop']); family=thesis['family']
    buffer=max(2*tick,buffer_mult*a5,.10*a15)
    minimum=max(entry*spec['min_stop_pct'],mult5*a5,mult15*a15,side*(entry-anchor)+buffer)
    stop=directional_round(entry-side*minimum,tick,-side)
    risk=side*(entry-stop)
    # Risk budget is handled through notional. Never clamp stop inside invalidation.
    # A much larger distance indicates a stale/poor location, not permission to squeeze the stop.
    max_risk=max(entry*spec['max_stop_pct'],3.0*a15)
    cost_r=2*(fee+slip)*entry/max(risk,tick)
    min_rr=max(1.75,spec['min_rr'],1.35+cost_r)
    target_r=max(preferred if aligned else 2.1,FAMILY_TARGET[family] if aligned else 2.15)
    target_r=max(min_rr,target_r)
    # Search nearby confirmed liquidity on M15/H1. Leave a tick/volatility buffer before it.
    pad=max(tick,.08*a15)
    ahead=sorted({side*(x-entry) for x in pivots(m15[-96:],side,prominence=.65*a15)+pivots(h1[-36:],side,prominence=1.0*a15)
                  if side*(x-entry)>pad})
    ideal=target_r*risk
    # A nearby major pivot is an obstacle; never silently assume a breakout through it.
    if ahead and ahead[0]-pad<=ideal:
        distance=ahead[0]-pad; target_kind='CONFIRMED_LIQUIDITY'
    else:
        distance=ideal; target_kind='VOLATILITY_PROJECTION'
    tp=directional_round(entry+side*distance,tick,-side)
    rr=side*(tp-entry)/risk if risk>0 else 0.
    reasons=[]
    if not (0<stop and 0<tp and risk>tick and side*(anchor-stop)>=buffer-1e-8): reasons.append('INVALID_STRUCTURE')
    if risk>max_risk+tick/2: reasons.append('DISTANT_INVALIDATION')
    if rr+1e-9<min_rr: reasons.append('NEAR_LIQUIDITY')
    return {'stop':stop,'tp':tp,'risk':risk,'rr':rr,'net_rr':rr-cost_r,'valid':not reasons,
            'geometry_reason':','.join(reasons),'geometry_policy':POLICY,'target_kind':target_kind,
            'atr_m5':a5,'atr_m15':a15,'structural_anchor':anchor,'stop_buffer':buffer,
            'risk_floor':minimum,'risk_ceiling':max_risk,'min_rr':min_rr,
            'trail_atr_m5':a5,'trail_start_r':1.25,'target_r_preferred':target_r}


def structural_trail(active, bar, stop, peak, end, tick, step):
    """Process M1 only; aggregate complete five-minute blocks from post-entry bars.
    Three CLOSED M5 blocks confirm a swing; stop may only move after confirmation.
    No automatic break-even at 0.9R, no retroactive swing use.
    """
    if step!=60000 or end-60000<int(active.get('trail_start_ts',active.get('opened_ts') or end)):
        return stop
    side=int(active['side']); entry=float(active['entry']); risk=float(active['risk'])
    o,h,l,c=bar; ts=end-step; bucket=ts//300000*300000
    partial=active.get('trail_partial')
    if not partial or partial['bucket']!=bucket or partial['last']!=ts:
        partial={'bucket':bucket,'last':ts,'count':0,'o':o,'h':h,'l':l,'c':c}
    partial.update(last=end,count=partial['count']+1,h=max(partial['h'],h),l=min(partial['l'],l),c=c)
    active['trail_partial']=partial
    if end!=bucket+300000: return stop
    active.pop('trail_partial',None)
    if partial['count']!=5: return stop
    bars=active.setdefault('trail_m5',[])
    if bars and bars[-1]['bucket']+300000!=bucket: bars.clear()
    bars.append(partial); del bars[:-16]
    if len(bars)<3 or peak<float(active.get('trail_start_r',1.25)): return stop
    a,b,d=bars[-3:]
    if side==1:
        confirmed=b['l']<min(a['l'],d['l']) and d['c']>b['h']
        anchor=b['l']
    else:
        confirmed=b['h']>max(a['h'],d['h']) and d['c']<b['l']
        anchor=b['h']
    if not confirmed: return stop
    tr=[max(y['h']-y['l'],abs(y['h']-x['c']),abs(y['l']-x['c'])) for x,y in zip(bars,bars[1:])]
    vol=max(float(active.get('trail_atr_m5') or risk/1.7),sum(tr)/len(tr) if tr else 0.)
    candidate=directional_round(anchor-side*max(2*tick,.30*vol),tick,-side)
    # Require structural confirmation, room for ordinary noise and positive net lock.
    cost=float(active.get('fee_rate',0.))+float(active.get('slippage_rate',0.))
    net_be=entry*(1+cost)/(1-cost) if side==1 else entry*(1-cost)/(1+cost)
    if side*(candidate-net_be)<tick or side*(c-candidate)<max(2*tick,.35*vol): return stop
    if side*(candidate-stop)>tick/2:
        active.update(be_armed=True,stop_effective_ts=end,trail_reason='CONFIRMED_M5_SWING',trail_anchor=anchor)
        return candidate
    return stop
