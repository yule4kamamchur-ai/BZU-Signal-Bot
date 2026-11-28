"""v15.9 PAPER execution and isolated experimental portfolio policy.
No private orders. Contract quantities are base-asset qty = contracts * contractSize.
Experimental estimates are descriptive, not calibrated forecasts or eligibility gates.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import math
from statistics import mean

MIN_NOTIONAL=5.0
MIN_RISK_BUDGET=0.25
EXECUTION_SCHEMA='PORTFOLIO_EXECUTION_159'
ADAPTIVE_PROFILES=('ADAPTIVE_H1','ADAPTIVE_RANGE')
PROFILES=('PEAK_CURRENT','CLOSE_PRIOR','MIXED_PEAK_RANGE','RANGE_ONLY','EARLY_H1_M5','RANGE_LOCK_ONLY',*ADAPTIVE_PROFILES)
RANGE_PROFILES=('MIXED_PEAK_RANGE','RANGE_ONLY','RANGE_LOCK_ONLY','ADAPTIVE_RANGE')
H1_PROFILES=tuple(p for p in PROFILES if p not in ('RANGE_ONLY','RANGE_LOCK_ONLY','ADAPTIVE_RANGE'))


def contract_spec(row):
    """Public metadata in contract units; missing fields are never inferred from priceUnit."""
    try:
        vals={k:float(row[k]) for k in ('contractSize','volUnit','minVol')}
        if not all(math.isfinite(v) and v>0 for v in vals.values()): return None
        for k in ('maxVol','maxLeverage'):
            if row.get(k) is not None:
                v=float(row[k])
                if not math.isfinite(v) or v<=0: return None
                vals[k]=v
        if vals.get('maxVol',vals['minVol'])<vals['minVol']: return None
        return vals
    except (TypeError,ValueError,KeyError,OverflowError,AttributeError): return None


def size(entry, stop, risk_budget, notional_budget, cost, contract=None, require_contract=False):
    """Round DOWN by exchange quantity step, enforce actual USD floor and cap.
    Old local CSV without contract metadata can explicitly use continuous PAPER proxy.
    """
    try:
        if not all(math.isfinite(float(x)) for x in (entry,stop,risk_budget,notional_budget,cost)) or min(entry,stop)<=0 or cost<0:
            return None,'INVALID_INPUT_OR_BUDGET'
        if risk_budget<MIN_RISK_BUDGET-1e-10: return None,'BELOW_MIN_RISK_BUDGET'
        if notional_budget<MIN_NOTIONAL-1e-10: return None,'BELOW_MIN_NOTIONAL'
        d=lambda x:Decimal(str(x))
        e,st,rb,nb,c=map(d,(entry,stop,risk_budget,notional_budget,cost))
        per=abs(e-st)+c*(e+st)
        if per<=0: return None,'INVALID_STOP'
        qty=min(rb/per,nb/e)
        spec=contract_spec(contract) if contract is not None else None
        if spec is None and (require_contract or contract is not None): return None,'MISSING_CONTRACT_SPEC'
        out={'execution_schema':EXECUTION_SCHEMA,'quantity_policy':'CONTINUOUS_RESEARCH_PROXY'}
        if spec:
            cs,step,mn=map(d,(spec['contractSize'],spec['volUnit'],spec['minVol']))
            n=qty/cs
            if 'maxVol' in spec: n=min(n,d(spec['maxVol']))
            n=(n/step).to_integral_value(rounding=ROUND_FLOOR)*step
            if n<mn: return None,'BELOW_MIN_CONTRACTS'
            qty=n*cs
            out.update(contracts=float(n),contract_size=float(cs),contract_step=float(step),min_contracts=float(mn),quantity_policy='MEXC_CONTRACT_STEP')
        notional=qty*e; reserve=qty*per
        if notional<d(MIN_NOTIONAL)-d('0.0000000001'): return None,'BELOW_MIN_NOTIONAL'
        # Actual allocated risk floor as well as remaining budget floor; no rounding up.
        if reserve<d(MIN_RISK_BUDGET)-d('0.0000000001'): return None,'BELOW_MIN_ALLOCATED_RISK'
        if reserve>rb+d('0.0000000001') or notional>nb+d('0.0000000001'): return None,'BUDGET_ROUNDING_ERROR'
        out.update(qty=float(qty),notional=float(notional),risk_usd=float(qty*abs(e-st)),reserve_usd=float(reserve))
        return out,'OPEN'
    except (InvalidOperation,TypeError,ValueError,OverflowError,ZeroDivisionError): return None,'INVALID_INPUT_OR_BUDGET'


def market_features(rows, sig):
    """Features from closed bars only, independent of H1 entry gate."""
    q=rows[-21:]; a=max(float(sig['atr']),1e-12); s=int(sig['side'])
    if len(q)<3: return {'regime':'UNKNOWN','efficiency':0.,'directional_efficiency':0.,'breakout_quality':0.}
    path=sum(abs(b['c']-p['c']) for p,b in zip(q,q[1:])); drift=q[-1]['c']-q[0]['c']; eff=abs(drift)/path if path else 0.
    last=q[-1]; width=max(last['h']-last['l'],1e-12)
    close_location=(last['c']-last['l'])/width if s==1 else (last['h']-last['c'])/width
    body=s*(last['c']-last['o'])/a
    strength=float(sig.get('strength',0.))
    quality=.40*min(1.,max(0.,strength))+.35*close_location+.25*min(1.,max(0.,body))
    return {'regime':('TREND_UP' if drift>0 else 'TREND_DOWN') if eff>=.35 else 'RANGE_TRANSITION','efficiency':eff,'directional_efficiency':s*drift/path if path else 0.,'breakout_quality':quality,'close_location':close_location,'body_atr':body}


def estimate(hist,symbol,side,regime,strategy,now,anchor=None):
    """Shrink side-specific closed, executable, observed PAPER samples to zero.
    Group by asset/direction/regime; related samples have limited influence.
    Do not learn on legacy or non-executable positions, future labels or ambiguous bars.
    """
    total=weighted=0.; n=0
    for p in hist:
        if p.get('execution_schema')!=EXECUTION_SCHEMA or p.get('strategy')!=strategy or p.get('side')!=side or p.get('closed_ts',now+1)>now: continue
        if p.get('opened_ts',now)>=p.get('closed_ts',0): continue
        quality=p.get('quality'); reliability=1. if quality=='OBSERVED_M1' else .25 if quality=='PARTIAL_ENTRY_BAR' and p.get('entry_bar_range_clear') else 0.
        if not reliability: continue
        if anchor and p.get('trail_anchor')!=anchor: continue
        try: r=float(p['net_r'])
        except (TypeError,ValueError,KeyError): continue
        if not math.isfinite(r): continue
        w=reliability*(1. if p.get('symbol')==symbol else .20)
        if p.get('entry_regime')!=regime: w*=.35
        total+=w; weighted+=w*max(-3.,min(6.,r)); n+=1
    return {'expected_r':weighted/(8.+total),'effective_n':total,'samples':n,'confidence':total/(8.+total)}


def _returns(rows):
    q=rows[-49:]; return {b['t']:math.log(b['c']/a['c']) for a,b in zip(q,q[1:]) if b['t']-a['t']==3600 and a['c']>0 and b['c']>0}


def correlation(rows,other):
    x,y=_returns(rows),_returns(other); keys=sorted(x.keys()&y.keys())
    if len(keys)<16: return None
    a=[x[k] for k in keys]; b=[y[k] for k in keys]; ma,mb=mean(a),mean(b)
    va=sum((v-ma)**2 for v in a); vb=sum((v-mb)**2 for v in b)
    return max(-1.,min(1.,sum((u-ma)*(v-mb) for u,v in zip(a,b))/math.sqrt(va*vb))) if va and vb else None


def signed_overlap(symbol,side,positions,rows_by_symbol):
    vals=[]
    for sym,p in positions.items():
        if sym==symbol: continue
        c=correlation(rows_by_symbol.get(symbol,[]),rows_by_symbol.get(sym,[]))
        if c is not None: vals.append(max(0.,c*side*p['side']))
    return max(vals,default=0.)


def choose_anchor(hist,symbol,side,features,now):
    peak=estimate(hist,symbol,side,features['regime'],'TREND_H1',now,'PEAK')
    close=estimate(hist,symbol,side,features['regime'],'TREND_H1',now,'CLOSE')
    if min(peak['effective_n'],close['effective_n'])>=3 and abs(peak['expected_r']-close['expected_r'])>.1:
        return 'CLOSE' if close['expected_r']>peak['expected_r'] else 'PEAK'
    # Experimental close-based policy for weak/volatile closes, not an active rule.
    return 'CLOSE' if features.get('efficiency',0)<.30 and features.get('close_location',1)<.7 else 'PEAK'
