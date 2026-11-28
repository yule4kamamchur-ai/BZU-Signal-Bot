"""v15.4 causal opportunity intelligence. PAPER only; estimates are not validated probabilities."""
from __future__ import annotations
import math, os
from statistics import mean
from mexc_direction import scale, contiguous

MODEL_SCHEMA='MEXC_OPPORTUNITY_15_4'
OLD_SCHEMA='MEXC_ADAPTIVE_DIRECTION_15_3'
SOFT={'LOW_EVIDENCE_SCORE','NO_LOCAL_SHIFT_EVIDENCE','CURRENT_WAVE_OPPOSES','M1_NO_M5_CONTEXT','M1_WEAK_REVERSAL'}


def market_state(m5,m15,local,htf,spread,oi_change=None):
    a=scale(m5); old=scale(m5[:-8]); ratio=a/old if old else 1.
    w=local.get('m5',{}); slow=local.get('m5_slow',{}); side=local.get('side',0)
    phase=('IMPULSE' if side and w.get('efficiency',0)>.65 and w.get('move_atr',0)>1.3 else
           'PULLBACK' if w.get('side',0) and slow.get('side',0)==-w['side'] else
           'EXHAUSTION' if slow.get('move_atr',0)>2.5 and w.get('efficiency',0)<.4 else
           'COMPRESSION' if ratio<.8 else 'BALANCED')
    return {'direction':side,'higher_direction':htf,'phase':phase,'volatility':'EXPANDING' if ratio>1.2 else 'COMPRESSED' if ratio<.8 else 'NORMAL',
            'volatility_ratio':round(ratio,3),'structure':'SHIFT' if local.get('breaks') else 'STABLE_OR_UNCLEAR',
            'liquidity':'WIDE' if a and spread/a>.2 else 'NORMAL' if a else 'UNKNOWN','oi_change_pct':oi_change}


def beta_relative(asset,btc,base):
    if not base.get('available'): return base
    a={x.ts:x.close for x in asset}; b={x.ts:x.close for x in btc}; end=max(set(a)&set(b)); ts=[end-i*900000 for i in range(41,-1,-1)]
    pairs=[(a[t]/a[t-900000]-1,b[t]/b[t-900000]-1) for t in ts[:-4] if t in a and t-900000 in a and t in b and t-900000 in b and min(a[t-900000],b[t-900000])>0]
    if len(pairs)<20: return {**base,'beta':None,'method':'UNIT_BETA_FALLBACK'}
    ma=mean(x for x,y in pairs); mb=mean(y for x,y in pairs); var=sum((y-mb)**2 for x,y in pairs)
    if var/len(pairs)<1e-8: return {**base,'beta':None,'method':'UNIT_BETA_FALLBACK'}
    beta=max(-2.,min(4.,sum((x-ma)*(y-mb) for x,y in pairs)/var)); beta=(len(pairs)*beta+16)/(len(pairs)+16)
    rel=(base['asset_return_pct']-beta*base['btc_return_pct'])/100*asset[-5].close/max(scale(asset),1e-12)
    return {**base,'beta':round(beta,3),'beta_n':len(pairs),'relative_atr':round(rel,3),'side':1 if rel>=.65 else -1 if rel<=-.65 else 0,'method':'SHRUNK_BETA_RESIDUAL'}


def rates(symbol,route='MARKET'):
    """Configured account estimates, never infer zero exit fee from a marketing tag."""
    prefix=symbol.replace('_USDT','')
    def rate(name,default):
        v=float(os.getenv(f'MEXC_{prefix}_{name}',os.getenv(f'MEXC_{name}',str(default))))
        if not math.isfinite(v) or not 0<=v<=.02: raise ValueError('Invalid cost assumption')
        return v
    taker=rate('TAKER_FEE_RATE',.0002); maker=rate('MAKER_FEE_RATE',.0002); slip=rate('SLIPPAGE_RATE',.0001)
    return {'entry_fee_rate':maker if route=='LIMIT' else taker,'exit_fee_rate':taker,'entry_slip_rate':0. if route=='LIMIT' else slip,'exit_slip_rate':slip}


def cost_r(a,exit_price,*,partial=True):
    entry=float(a['entry']); risk=float(a['risk']); old_fee=float(a.get('fee_rate',0)); old_slip=float(a.get('slippage_rate',0))
    in_rate=float(a.get('entry_fee_rate',old_fee))+float(a.get('entry_slip_rate',old_slip)); out_rate=float(a.get('exit_fee_rate',old_fee))+float(a.get('exit_slip_rate',old_slip))
    pp=float(a.get('partial_pct',0)) if partial and a.get('tp1_hit') and a.get('tp1_fill_price') else 0.
    exit_weight=pp*float(a.get('tp1_fill_price') or exit_price)+(1-pp)*exit_price
    return (in_rate*entry+(out_rate+float(a.get('spread_cost_rate',0.)))*exit_weight)/risk


def estimate(history,candidate,as_of):
    """Directional empirical Bayes: prior mean 0R, 0.5 win probability; small samples weakly affect rank.
    Old v15.3 is a discounted transfer, static shadow labels have separate discounted weight.
    One resolved market event per symbol/direction/5m, prefer paper over shadow.
    """
    groups={}; side=candidate['side']
    for r in history:
        v=r.get('net_r'); end=r.get('closed_ts'); start=r.get('opened_ts'); schema=r.get('model_schema'); source=r.get('source','PAPER')
        if schema not in (MODEL_SCHEMA,OLD_SCHEMA) or r.get('side')!=side or r.get('quality') not in ('OBSERVED_M1','SHADOW_M1_MODEL') or not isinstance(v,(float,int)) or not math.isfinite(v) or not isinstance(start,int) or not isinstance(end,int) or not start<end<as_of: continue
        if not r.get('symbol') or not r.get('family'): continue
        if source in ('SHADOW','HISTORICAL_SHADOW') and (schema!=MODEL_SCHEMA or r.get('route')!=candidate.get('order_type','MARKET')): continue
        key=r.get('signal_event_key') or (r['symbol'],side,(r.get('signal_known_ts') or start)//300000)
        w=(.35 if source=='SHADOW' else .15 if source=='HISTORICAL_SHADOW' else .25 if source=='HISTORICAL_REPLAY' else 1. if schema==MODEL_SCHEMA else .25)
        if r.get('cost_status') in ('FUNDING_HISTORY_UNAVAILABLE','FUNDING_MARK_PROXY_PARTIAL'): w*=.5
        w*=1. if r.get('route') in (None,candidate.get('order_type','MARKET')) else .45
        w*=1. if r['family']==candidate['family'] else .20
        w*=1. if r.get('regime')==candidate.get('regime') else .5
        w*=1. if r['symbol']==candidate.get('symbol') else .65
        # Prefer actual, same-family observations over alternative routes from the same episode.
        priority=(source=='PAPER',r['family']==candidate['family'],w,-end,str(r.get('trade_id','')))
        if key not in groups or priority>groups[key][0]: groups[key]=(priority,r,w)
    rows=[(r,w) for _,r,w in groups.values()]; n=len(rows); weight=sum(w for r,w in rows)
    # Prior strength prevents one lucky trade from dominating. Wins and losses already net.
    exp=sum(w*max(-4.,min(4.,r['net_r'])) for r,w in rows)/(8+weight)
    p=(4+sum(w*(r['net_r']>0) for r,w in rows))/(8+weight)
    return {'status':'SHRUNK_ESTIMATE_UNVALIDATED' if n else 'COLD_START_UNVALIDATED','n':sum(r['family']==candidate['family'] for r,w in rows),'total_n':n,'effective_n':round(weight,3),
            'shadow_n':sum(r.get('source') in ('SHADOW','HISTORICAL_SHADOW') for r,w in rows),'win_rate_estimate':round(p,4) if n else None,'expected_r_estimate':round(exp,4),'rank_adjustment':round(exp,4),'prior_strength':8,'validated':False}


def objective(meta,score,net_rr,route,rvol,phase,side=1):
    # Unknown edge: neutral, modest quality tie-break in R units, not a made-up probability.
    quality=max(-.1,min(.1,(score-78)/200))
    return meta['expected_r_estimate']+quality+max(-.05,min(.05,(net_rr-1.7)*.035))


def policy(family,phase,rr,funding_regime):
    fast=family in ('MOMENTUM','EARLY_STRUCTURE_SHIFT','COUNTERTREND_WAVE','STRUCTURE_CONTINUATION')
    tp1= .9 if funding_regime=='CROWDED_WITH_US' else 1.25 if fast and phase=='IMPULSE' else 1.05
    enabled=rr>tp1+.45
    return dict(tp1_enabled=enabled,tp1_r=tp1,partial_pct=.30 if fast else .4,time_stop_enabled=True,
                stale_tighten_min=45 if fast else 90,stale_exit_min=75 if fast else 150,idea_review_enabled=True)


def shadow_step(memory,items,now):
    """Resolve old hypothetical trades using only subsequently closed bars, before registering new ones.
    Static full SL/TP outcome = counterfactual route label; never real PnL.
    """
    ledger=memory.setdefault('opportunity_ledger',{'pending':{},'resolved':[],'observations':[],'events':{}})
    pending=ledger['pending']; by={x['symbol']:x for x in items}
    for key,a in list(pending.items()):
        item=by.get(a['symbol'],{}); bars=item.get('candles_1m') or []; cursor=a['cursor']; side=a['side']; done=None
        for c in bars:
            t=int(c['t'])*1000; end=t+60000
            if end<=cursor or end>now: continue
            if t<cursor:
                # Never use a pre-registration wick. At the next complete minute, evaluate the close only.
                if t!=cursor//60000*60000: continue
                o=h=l=cl=float(c['c'])
                if not a['filled'] or (float(c['l'])<=a['stop'] or float(c['h'])>=a['tp'] if side==1 else float(c['h'])>=a['stop'] or float(c['l'])<=a['tp']): a['quality']='SHADOW_PARTIAL_BAR'
            else:
                if t!=cursor: a['quality']='SHADOW_DATA_GAP'
                o,h,l,cl=(float(c[x]) for x in ('o','h','l','c'))
            a['cursor']=end; cursor=end
            if not a['filled']:
                if end>a['expires_ts']: done=('LIMIT_EXPIRED',None,end); break
                # Require penetration, not touch; limit fills are still only proxies.
                if (l<a['entry']-a['tick']*.5 if side==1 else h>a['entry']+a['tick']*.5):
                    a['filled']=True; a['opened_ts']=end
                    if (l<=a['stop'] if side==1 else h>=a['stop']): done=('STOP',min(o,a['stop']) if side==1 else max(o,a['stop']),end); break
                    continue
                if (h>=a['tp'] if side==1 else l<=a['tp']): done=('LIMIT_MISSED_TARGET',None,end); break
                continue
            stop_hit=l<=a['stop'] if side==1 else h>=a['stop']; tp_hit=h>=a['tp'] if side==1 else l<=a['tp']
            if stop_hit:
                if tp_hit: a['quality']='SHADOW_AMBIGUOUS'
                done=('STOP',min(o,a['stop']) if side==1 else max(o,a['stop']),end); break
            if tp_hit: done=('TP',a['tp'],end); break
            if end-a['opened_ts']>=18*3600000: done=('TIME',cl,end); break
        if not done and now-a['created_ts']>20*3600000: done=('UNOBSERVED_EXPIRED',None,now)
        if done:
            reason,px,end=done; v=None if px is None else side*(px-a['entry'])/a['risk']-cost_r(a,px,partial=False)
            ledger['resolved'].append({**a,'closed_ts':end,'net_r':round(v,4) if v is not None else None,'reason':reason,'source':'SHADOW','trade_id':key,'route':a['order_type']})
            pending.pop(key)
    ledger['resolved']=ledger['resolved'][-4000:]; ledger['observations']=ledger['observations'][-3000:]
    return ledger


def register(memory,items,now):
    ledger=memory['opportunity_ledger']; pending=ledger['pending']; seen=set(pending)|{x['trade_id'] for x in ledger['resolved']}; events=ledger['events']
    for item in items:
        if not item.get('analysis_fresh'): continue
        for d in item.get('candidate_diagnostics',[]):
            key=d.get('signal_id') or str(d.get('signal_known_ts'))+str(d.get('family'))
            key=item['symbol']+':'+key+':'+str(d.get('order_type'))
            if key not in events:
                events[key]=now; ledger['observations'].append({'ts':now,'symbol':item['symbol'],**d})
            # Non-executable or invalidated candidates remain diagnostic only, not fabricated fills.
            if key in seen or len(pending)>=500 or set(d.get('rejection_codes',[]))-SOFT or not d.get('valid'): continue
            if not all(d.get(k) for k in ('entry','stop','tp','risk','tick')): continue
            pending[key]={k:d[k] for k in ('family','regime','side','entry','stop','tp','risk','tick','order_type','signal_event_key','signal_known_ts','entry_fee_rate','exit_fee_rate','entry_slip_rate','exit_slip_rate','spread_cost_rate') if k in d}
            pending[key].update(symbol=item['symbol'],model_schema=MODEL_SCHEMA,created_ts=now,opened_ts=now,cursor=now,filled=d['order_type']=='MARKET',expires_ts=now+int(d.get('limit_ttl_min',8))*60000,quality='SHADOW_M1_MODEL',rejection_codes=d.get('rejection_codes',[]))
        for w in item.get('elapsed_m1_windows',[]):
            key=f"{item['symbol']}:{w['side']}:{w['ts']}:ELAPSED"
            if key not in events:
                events[key]=now;ledger['observations'].append({'observed_at':now,'symbol':item['symbol'],**w})
        for side,diag in item.get('direction_diagnostics',{}).items():
            for w in diag.get('windows_checked',[]):
                key=f"{item['symbol']}:{side}:{w['tf']}:{w['known_ts']}:WINDOW"
                if key not in events:
                    events[key]=now; ledger['observations'].append({'ts':now,'symbol':item['symbol'],'side':side,'window':w,'kind':'WINDOW_DIAGNOSTIC'})
    ledger['events']={k:t for k,t in events.items() if now-t<48*3600000}
    return {'pending':len(pending),'resolved':len(ledger['resolved']),'usable_labels':sum(x.get('quality')=='SHADOW_M1_MODEL' and x.get('net_r') is not None for x in ledger['resolved']),'observations':len(ledger['observations']),'status':'RESEARCH_UNVALIDATED'}


def break_memory(rows, prior, now, tick):
    """Keep closed M5 break events for 90m; invalidate on a close back through the level.
    Rebuild from actual recent M5 when a worker starts; persisted older events survive scans.
    """
    found={str(x['ts'])+':'+str(x['side']):dict(x) for x in prior or [] if 0<=now-x['ts']-300000<90*60000}
    for i in range(max(10,len(rows)-18),len(rows)):
        prefix=rows[:i+1]; a=scale(prefix[:-1]); c=rows[i]
        if not a or not contiguous(prefix,300000,11): continue
        pre=rows[i-10:i]
        for side in (1,-1):
            level=max(x.high for x in pre) if side==1 else min(x.low for x in pre)
            if side*(c.close-level)>.12*a and side*(c.close-c.open)>.45*a:
                found[str(c.ts)+':'+str(side)]={'ts':c.ts,'side':side,'level':level,'atr':a}
    return [x for x in found.values() if x['ts']+300000<=now and 0<=now-x['ts']-300000<90*60000
            and all(side_valid(x,b) for b in rows if b.ts>x['ts']) and any(b.ts==x['ts'] for b in rows)]


def side_valid(event,b): return event['side']*(b.close-event['level'])>=-.28*event['atr']


def memory_retest(rows,events,side,atr,tick,tf):
    if tf!='M5' or not rows or atr<=0: return []
    c=rows[-1]; p=rows[-2]; out=[]
    for e in events:
        if e['side']!=side or e['ts']>=c.ts or c.ts-e['ts']>90*60000: continue
        level=e['level']; between=[b for b in rows if e['ts']<b.ts<=c.ts]
        if not between or not all(side_valid(e,b) for b in between): continue
        # Only a NEW retest, not every bar that remains beyond the old level.
        touch=(c.low<=level+.25*atr if side==1 else c.high>=level-.25*atr)
        if not touch or side*(c.close-level)<.08*atr or side*(c.close-c.open)<.25*atr: continue
        anchor=(min(c.low,p.low,level)-tick if side==1 else max(c.high,p.high,level)+tick)
        avg=mean(b.volume for b in rows[-21:-1]); rv=c.volume/avg if avg else 0.
        out.append({'setup':'BREAKOUT_RETEST','family':'BREAKOUT_RETEST','setup_ua':'M5 ретест з пам’яті пробою','base_score':76,'structural_stop':anchor,'reference':c.close,'trigger_ts':c.ts,'trigger_tf':tf,'rvol':rv,'retest_level':level,'opportunity_origin_ts':e['ts']})
    return out[-1:]


def funding_accrual(active,item,end):
    """Public settled funding rates, with M1 price proxy; never use current rate as past payment.
    No private account fee claims. Track event ids and keep unresolved price/rate gaps explicit.
    """
    opened=active.get('opened_ts')
    if opened is None: return 0.,'NOT_OPEN'
    if 'funding_events' not in item: return float(active.get('funding_r',0.)),'FUNDING_HISTORY_UNAVAILABLE'
    known=active.setdefault('funding_settlements',{})
    bars={int(c['t'])*1000:c for c in item.get('candles_1m',[])}
    missing=False
    for e in item['funding_events']:
        t=int(e['settleTime']); rate=float(e['fundingRate'])
        if not opened<t<=end or str(t) in known: continue
        # Use last completed minute, not a future price. Unknown mark price explicitly modelled.
        bar=bars.get(t-60000)
        if bar is None: missing=True; continue
        fraction=1.-float(active.get('partial_pct',0.)) if active.get('tp1_hit_ts',end+1)<t else 1.
        known[str(t)]=active['side']*rate*float(bar['c'])/active['risk']*fraction
    total=sum(known.values()); active['funding_r']=total
    status='FUNDING_MARK_PROXY_PARTIAL' if missing or item.get('funding_history_incomplete') else 'FUNDING_SETTLED_RATE_MARK_PROXY'
    return total,status
