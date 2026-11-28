"""Isolated matched-entry SL/TP research on newly opened PAPER MARKET trades.
Fixed horizons, no portfolio or learner writes, no retroactive replay, excludes funding.
Four arms use the same entry/decision time, same execution cost model, unit gross risk.
"""
from __future__ import annotations
from collections import defaultdict
from mexc_opportunity import cost_r
from mexc_h1_trend import MINUTE,HOUR,advance_bar,tickround

VARIANTS={'STATIC_CONTROL':(1.,1.),'SL_150':(1.5,1.),'SL_TP_150':(1.5,1.5),'TP_150':(1.,1.5)}


def update(book,items,active_positions,now):
    pending=book.setdefault('pending',{}); done=book.setdefault('resolved',[]); seen=book.setdefault('seen',{})
    by={x['symbol']:x for x in items}
    for ident,p in list(pending.items()):
        item=by.get(p['symbol'],{}); end=p['opened_ts']+18*HOUR; outcome=None
        for b in sorted(item.get('candles_1m',[]),key=lambda x:x['t']):
            t=int(b['t'])*1000
            if t+MINUTE>min(now,end+MINUTE): break
            prior=p['processed_until_ts']; oldq=p['quality']; outcome,_,mark=advance_bar(p,b,None)
            # If neither barrier could have been touched in the partial bar, the
            # static exit label remains observable despite unknown entry-minute wicks.
            if prior>t and prior<t+MINUTE and oldq=='OBSERVED_M1' and p['quality']=='PARTIAL_ENTRY_BAR':
                s=p['side']; lo=b['l']; hi=b['h']; touched=(lo<=p['stop'] or hi>=p['tp']) if s==1 else (hi>=p['stop'] or lo<=p['tp'])
                if not touched: p['quality']='OBSERVED_M1'
            if outcome: break
            if p['processed_until_ts']>prior and t+MINUTE>=end and mark is not None:
                outcome=(mark,t+MINUTE,'HORIZON_18H'); break
        if outcome:
            px,ts,reason=outcome; net=p['side']*(px-p['entry'])/p['risk']-cost_r(p,px,partial=False)
            done.append({**p,'exit':px,'closed_ts':ts,'exit_reason':reason,'net_r':net,'funding_status':'NOT_INCLUDED','source':'SHADOW_GEOMETRY'});del pending[ident]
        elif now>end+2*HOUR:
            done.append({**p,'closed_ts':now,'exit_reason':'UNOBSERVED_EXPIRED','net_r':None,'quality':'DATA_GAP','funding_status':'NOT_INCLUDED','source':'SHADOW_GEOMETRY'});del pending[ident]
    for a in active_positions:
        if not a or a.get('phase')!='OPEN' or a.get('order_type')!='MARKET' or a.get('opened_ts')!=now: continue
        key=f"{a['symbol']}:{a.get('trade_id')}:{now}"
        if key in seen or len(pending)>496: continue
        e=float(a['entry']); s=int(a['side']); tick=float(a['tick']); r=abs(e-float(a['initial_stop'])); target=abs(float(a['tp'])-e)
        if min(e,tick,r,target)<=0 or s not in (1,-1): continue
        arms={}
        for name,(sm,tm) in VARIANTS.items():
            st=tickround(e-s*r*sm,tick,-s); tp=tickround(e+s*target*tm,tick,-s); risk=abs(e-st)
            if min(st,tp,risk)<=0: break
            arms[key+':'+name]={k:a[k] for k in ('symbol','family','regime','trigger_tf','fee_rate','slippage_rate','entry_fee_rate','exit_fee_rate','entry_slip_rate','exit_slip_rate','spread_cost_rate') if k in a}
            arms[key+':'+name].update(pair_id=key,variant=name,entry=e,side=s,stop=st,initial_stop=st,tp=tp,risk=risk,tick=tick,trail_mult=0.,peak_price=e,peak_r=0.,mae_r=0.,opened_ts=now,processed_until_ts=now,stop_effective_ts=now,quality='OBSERVED_M1')
        if len(arms)==len(VARIANTS): pending.update(arms);seen[key]=now
    book['seen']={k:t for k,t in seen.items() if now-t<48*HOUR};book['resolved']=done=done[-4000:]
    pairs=defaultdict(dict)
    for d in done:
        if d.get('quality')=='OBSERVED_M1' and d.get('net_r') is not None: pairs[d['pair_id']][d['variant']]=d
    matched=[g for g in pairs.values() if set(g)==set(VARIANTS)]; totals={}
    for name in VARIANTS:
        vals=[g[name]['net_r'] for g in matched];totals[name]={'n':len(vals),'net_r':sum(vals),'wr':sum(v>0 for v in vals)/len(vals) if vals else None}
    return {'mode':'SHADOW_MATCHED_STATIC_EX_FUNDING','pending':len(pending),'resolved':len(done),'complete_observed_pairs':len(matched),'variants':totals}
