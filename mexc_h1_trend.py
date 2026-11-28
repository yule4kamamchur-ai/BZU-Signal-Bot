"""Independent H1 Donchian research/PAPER account; no real orders.
Closed H1 breakout versus PRIOR channel. Causal M1 stop execution, H1-only
ratchet, adverse gap fills, no TP by default. State isolated from M1/M5.
All prices/fees are estimates; funding uses public settled-rate proxy when present.
"""
from __future__ import annotations
import math
from collections import defaultdict
from copy import deepcopy
from mexc_opportunity import funding_accrual
import mexc_portfolio_intelligence as intel

MINUTE = 60_000
HOUR = 3_600_000
SCHEMA = 'H1_DONCHIAN_TREND_1'


def pack(rows):
    return [{'t': b.ts//1000, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close} for b in rows]


def valid_bar(b):
    try:
        o,h,l,c=(float(b[k]) for k in ('o','h','l','c'))
        return all(math.isfinite(x) for x in (o,h,l,c)) and 0<l<=min(o,c)<=max(o,c)<=h
    except (KeyError, ValueError, TypeError):
        return False


def atr(rows, n=14):
    if len(rows)<n+1: return None
    q=rows[-n-1:]
    if not all(valid_bar(b) for b in q): return None
    return sum(max(b['h']-b['l'],abs(b['h']-a['c']),abs(b['l']-a['c'])) for a,b in zip(q,q[1:]))/n


def signal(rows, now, n=30, prior_atr=False):
    # Only closed H1, remove duplicate times. No use of current bar in channel.
    q=sorted({int(b['t']):b for b in rows if int(b['t'])*1000+HOUR<=now and valid_bar(b)}.values(),key=lambda b:b['t'])
    if len(q)<n+1: return None
    last=q[-1]; hist=q[-n-1:-1]; a=atr(q[:-1] if prior_atr else q)
    if not a or a<=0: return None
    upper=max(b['h'] for b in hist); lower=min(b['l'] for b in hist)
    side=1 if last['c']>upper else -1 if last['c']<lower else 0
    return {'side':side,'signal_ts':int(last['t'])*1000+HOUR,'close':last['c'],'upper':upper,'lower':lower,'atr':a,
            'strength':max(0.,side*(last['c']-(upper if side==1 else lower))/a) if side else 0.}


def early_signal(rows,m5,now,n=30):
    """Experimental closed-M5 crossing of the prior H1 channel, before H1 close."""
    q=sorted({int(b['t']):b for b in rows if int(b['t'])*1000+HOUR<=now and valid_bar(b)}.values(),key=lambda b:b['t'])
    f=sorted({int(b['t']):b for b in m5 if int(b['t'])*1000+5*MINUTE<=now and valid_bar(b)}.values(),key=lambda b:b['t'])
    if len(q)<n or len(f)<2: return None
    hour_end=q[-1]['t']*1000+HOUR
    a=atr(q); upper=max(x['h'] for x in q[-n:]); lower=min(x['l'] for x in q[-n:])
    if not a or a<=0: return None
    for offset in (0,1):
        if len(f)<offset+2: continue
        last,prev=f[-1-offset],f[-2-offset]; end=last['t']*1000+5*MINUTE
        if not hour_end<end<hour_end+HOUR or end-prev['t']*1000!=10*MINUTE or not 0<=now-end<=10*MINUTE: continue
        s=1 if prev['c']<=upper<last['c'] else -1 if prev['c']>=lower>last['c'] else 0
        if not s: continue
        level=upper if s==1 else lower
        # A prior crossing may be used only if all subsequent closed M5 still hold the level.
        after=f[len(f)-offset:] if offset else []
        if any(s*(x['c']-level)<=0 for x in after) or any(b['t']-a0['t']!=300 for a0,b in zip([last,*after],after)): continue
        return {'side':s,'signal_ts':end,'close':last['c'],'upper':upper,'lower':lower,'atr':a,'strength':s*(last['c']-level)/a,'route':'EARLY_M5_H1_CHANNEL'}
    return None


def tickround(p,tick,side):
    return (math.ceil(p/tick-1e-9) if side>0 else math.floor(p/tick+1e-9))*tick


def open_position(symbol,group,sig,price,now,*,tick,risk_budget,notional_budget,fee,slip,stop_mult=2.5,trail_mult=3.5,tp_r=None,max_chase_atr=.5,contract=None,require_contract=False):
    s=int(sig['side']); a=float(sig['atr']); p=float(price)
    if s not in (-1,1) or not all(math.isfinite(x) and x>0 for x in (a,p,tick,risk_budget,notional_budget,stop_mult)):
        return None,'INVALID_INPUT_OR_BUDGET'
    if now<sig['signal_ts'] or now-sig['signal_ts']>15*MINUTE: return None,'STALE_H1_BREAKOUT'
    level=sig['upper'] if s==1 else sig['lower']
    if s*(p-level)<=0: return None,'BREAKOUT_NO_LONGER_VALID'
    if s*(p-sig['close'])>max_chase_atr*a: return None,'PRICE_TOO_FAR'
    e=tickround(p,tick,s); stop=tickround(e-s*stop_mult*a,tick,-s)
    risk=s*(e-stop)
    if stop<=0 or risk<=0: return None,'INVALID_STOP'
    cost=fee+slip
    # Reserve worst estimated stop execution and both side costs. Gaps can exceed this reserve.
    sizing,reason=intel.size(e,stop,risk_budget,notional_budget,cost,contract,require_contract)
    if not sizing: return None,reason
    qty=sizing['qty']
    tp=tickround(e+s*float(tp_r)*risk,tick,-s) if tp_r is not None else None
    if tp is not None and (tp<=0 or s*(tp-e)<=0): return None,'INVALID_TARGET'
    return {'symbol':symbol,'cluster':group,'setup':SCHEMA,'model_schema':SCHEMA,'side':s,'entry':e,'stop':stop,'initial_stop':stop,'risk':risk,'tp':tp,'qty':qty,'notional':qty*e,'risk_usd':qty*risk,'reserve_usd':qty*(risk+cost*(e+stop)),
            'opened_ts':now,'processed_until_ts':now,'signal_ts':sig['signal_ts'],'stop_effective_ts':now,'atr_entry':a,'stop_mult':stop_mult,'trail_mult':trail_mult,'tick':tick,'peak_price':e,'peak_r':0.,'mae_r':0.,'fee_rate':fee,'slippage_rate':slip,'quality':'OBSERVED_M1','funding_r':0.,'funding_status':'FUNDING_HISTORY_UNAVAILABLE',**sizing},'OPEN'


def pnl(p,price):
    q=p['qty']; e=p['entry']; r=p['risk']; s=p['side']; cost=(p['fee_rate']+p['slippage_rate'])*(e+price)
    fund=p.get('funding_r',0.)*r
    return q*(s*(price-e)-cost-fund)


def finish(p,price,ts,reason):
    out=dict(p); r=p['risk']; e=p['entry']; s=p['side']
    gross=s*(price-e)/r; cost=(p['fee_rate']+p['slippage_rate'])*(e+price)/r
    out.update(exit=float(price),closed_ts=int(ts),exit_reason=reason,gross_r=gross,cost_r=cost,net_r=gross-cost-p.get('funding_r',0.),net_usd=pnl(p,price))
    out.pop('funding_settlements',None)
    return out


def advance_bar(p,b,atr_after=None):
    """Process closed M1 using OLD stop, then update the H1 ratchet after close.
    No use of high/low from partial entry bars. Returns (exit, worst price, close mark).
    If both stop/target are touched, stop wins. Gap stop execution uses adverse open.
    """
    t=int(b['t'])*1000; end=t+MINUTE; cur=p['processed_until_ts']
    if end<=cur: return None,None,None
    if t>cur: p['quality']='DATA_GAP'
    if not valid_bar(b): p['quality']='DATA_GAP'; p['processed_until_ts']=end; return None,None,None
    if t<cur:
        if p['quality']=='OBSERVED_M1': p['quality']='PARTIAL_ENTRY_BAR'
        # The entire entry bar range is known at close. If it could not touch either
        # initial exit, this partial bar is usable at reduced weight in research only.
        p['entry_bar_range_clear']=bool((b['l']>p['stop'] if p['side']==1 else b['h']<p['stop']) and (p.get('tp') is None or (b['h']<p['tp'] if p['side']==1 else b['l']>p['tp'])))
        p['processed_until_ts']=end
        c=float(b['c'])
        # The closing price is after entry, but high/low may precede it.
        if p['side']*(c-p['stop'])<=0: return (c,end,'PARTIAL_BAR_CLOSE_STOP'),c,c
        return None,None,c
    o,h,l,c=(float(b[k]) for k in ('o','h','l','c')); s=p['side']; st=p['stop']; tp=p['tp']; e=p['entry']; risk=p['risk']
    adverse=l if s==1 else h
    stop_hit=adverse<=st if s==1 else adverse>=st
    target_hit=tp is not None and (h>=tp if s==1 else l<=tp)
    p['processed_until_ts']=end
    if stop_hit:
        if target_hit and p['quality']=='OBSERVED_M1': p['quality']='AMBIGUOUS_OHLC'
        x=min(o,st) if s==1 else max(o,st)
        p['mae_r']=max(p['mae_r'], -s*(x-e)/risk)
        return (x,end,('M15_PROFIT_LOCK' if p.get('range_exit_policy')=='M15_LOCK' else 'ATR_TRAIL') if s*(st-p['initial_stop'])>p['tick']/2 else 'INITIAL_STOP'),x,x
    p['mae_r']=max(p['mae_r'],-s*(adverse-e)/risk)
    if target_hit:
        p['peak_r']=max(p['peak_r'],s*(tp-e)/risk)
        return (tp,end,'FIXED_TP'),adverse,tp
    favorable=h if s==1 else l
    p['peak_price']=max(p['peak_price'],favorable) if s==1 else min(p['peak_price'],favorable)
    p['peak_r']=max(p['peak_r'],s*(p['peak_price']-e)/risk)
    if p.get('range_exit_policy')=='M15_LOCK' and end%(15*MINUTE)==0 and p['peak_r']>=.75:
        cost=p['fee_rate']+p['slippage_rate']
        # Solve net move = +0.10R after estimated round trip costs.
        lock=(e*(s+cost)+.10*risk)/(s-cost)
        new=tickround(lock,p['tick'],s)
        if new>0 and s*(new-st)>p['tick']/2:
            if s*(c-new)<=0: return (c,end,'M15_LOCK_CLOSE'),adverse,c
            p['stop']=new; p['stop_effective_ts']=end
    if atr_after and end%HOUR==0 and p.get('trail_mult',0)>0:
        anchor=c if p.get('trail_anchor')=='CLOSE' else p['peak_price']
        new=tickround(anchor-s*p['trail_mult']*atr_after,p['tick'],-s)
        # If the freshly computed ratchet is already crossed, exit only at the known close.
        if new>0 and s*(new-st)>p['tick']/2:
            if s*(c-new)<=0: return (c,end,'H1_TRAIL_CLOSE'),adverse,c
            p['stop']=new; p['stop_effective_ts']=end
    return None,adverse,c


def stats(hist):
    n=len(hist); wins=sum(x['net_usd']>0 for x in hist); gw=sum(max(0,x['net_usd']) for x in hist); gl=-sum(min(0,x['net_usd']) for x in hist)
    return {'trades':n,'wins':wins,'win_rate':wins/n if n else None,'net_r':sum(x['net_r'] for x in hist),'net_usd':gw-gl,'pf':gw/gl if gl else None}


def update(book,items,now,*,deposit=100.,risk_pct=3.,max_leverage=5.,fee=.0002,slip=.0001,profile='PEAK_CURRENT',allow_new=True):
    """PAPER-only portfolio, distinct hypothetical $100 from M1/M5. No risk stacking.
    Risk capped per cluster at 1/3 total; at most half crypto cluster per new crypto.
    Current equity below initial reduces budget; no profit compounding.
    """
    if not all(math.isfinite(x) for x in (deposit,risk_pct,max_leverage,fee,slip)) or not (deposit>0 and 0<risk_pct<=3 and 0<max_leverage<=5 and 0<=fee<=.02 and 0<=slip<=.02): raise ValueError('Invalid H1 research risk/cost configuration')
    if profile not in intel.PROFILES: raise ValueError('Invalid research profile')
    if book.get('profile','PEAK_CURRENT' if book.get('schema') else profile)!=profile: raise ValueError('Cannot change research profile mid-book')
    book['profile']=profile
    from mexc_range_research import signal as range_signal, open_position as open_range
    if book.get('schema') not in (None,SCHEMA): raise ValueError('Incompatible H1 book')
    book.setdefault('schema',SCHEMA); positions=book.setdefault('positions',{}); hist=book.setdefault('history',[]); seen=book.setdefault('seen',{}); seen_range=book.setdefault('seen_range',{})
    if 'initial_deposit' in book and book['initial_deposit']!=deposit: raise ValueError('H1 book deposit cannot change mid-experiment')
    book.setdefault('initial_deposit',deposit)
    # Only demonstrably numerical dust from legacy execution is voided. Keep an
    # audit record; real-sized legacy positions and all old closes are preserved.
    for sym,p in list(positions.items()):
        if p.get('execution_schema')!=intel.EXECUTION_SCHEMA and all(isinstance(p.get(k),(int,float)) and math.isfinite(p[k]) and 0<=p[k]<=1e-8 for k in ('notional','reserve_usd')):
            book.setdefault('voided_numerical_positions',[]).append({'voided_ts':now,'reason':'LEGACY_NUMERICAL_DUST_NOT_EXECUTABLE','position':deepcopy(p)})
            del positions[sym]
    result=[]; candidates=[]; marks={}; rows_by_symbol={}
    for item in items:
        sym=item['symbol']; group=item['cluster']; rows=item.get('_trend_h1',[]); m1=item.get('candles_1m',[])
        rows=sorted({int(b['t']):b for b in rows if int(b['t'])*1000+HOUR<=now and valid_bar(b)}.values(),key=lambda x:x['t'])
        rows_by_symbol[sym]=rows
        p=positions.get(sym); had_position=p is not None; rec={'symbol':sym,'cluster':group,'status':'WAIT','setup':SCHEMA}
        # Only ATR values at H1 closes traversed since the current position cursor.
        # This keeps 180-day portfolio replay fast without changing causality.
        aa={int(b['t'])*1000+HOUR:atr(rows[i-15:i] if p.get('trail_anchor')=='CLOSE' else rows[i-14:i+1]) for i,b in enumerate(rows) if p and i>=(15 if p.get('trail_anchor')=='CLOSE' else 14) and int(b['t'])*1000+HOUR>p['processed_until_ts']}
        qts=int(item.get('quote_ts',0)); quote_ok=bool(item.get('quote_valid') and 0<=now-qts<=MINUTE and item.get('bid',0)>0 and item.get('ask',0)>=item['bid'])
        m1=sorted({int(b['t']):b for b in m1 if int(b['t'])*1000+MINUTE<=now}.values(),key=lambda x:x['t'])
        if p:
            hit=None
            for b in m1:
                hit,_,mark=advance_bar(p,b,aa.get(int(b['t'])*1000+MINUTE))
                if mark is not None: p['last_mark']=mark
                if hit: break
            if not hit and now-p['processed_until_ts']>2*MINUTE and p['quality']!='DATA_GAP': p['quality']='DATA_GAP'
            if not hit and quote_ok and qts>=p['opened_ts'] and qts>=p['processed_until_ts']:
                px=float(item['bid'] if p['side']==1 else item['ask']); p['last_mark']=px
                if p['side']*(px-p['stop'])<=0:
                    if qts-p['processed_until_ts']>MINUTE: p['quality']='DATA_GAP'
                    elif qts%MINUTE and p['quality']=='OBSERVED_M1': p['quality']='QUOTE_EXIT'
                    hit=(px,qts,'QUOTE_STOP')
                elif p.get('tp') is not None and p['side']*(px-p['tp'])>=0:
                    if qts-p['processed_until_ts']>MINUTE: p['quality']='DATA_GAP'
                    elif qts%MINUTE and p['quality']=='OBSERVED_M1': p['quality']='QUOTE_EXIT'
                    hit=(p['tp'],qts,'QUOTE_TARGET')
            end=hit[1] if hit else now
            fund,fs=funding_accrual(p,item,end); p['funding_r']=fund
            if p.get('funding_status') in ('FUNDING_MARK_PROXY_PARTIAL','FUNDING_HISTORY_UNAVAILABLE') and p.get('funding_incomplete_seen'):
                fs='FUNDING_MARK_PROXY_PARTIAL'
            if fs in ('FUNDING_HISTORY_UNAVAILABLE','FUNDING_MARK_PROXY_PARTIAL'): p['funding_incomplete_seen']=True
            p['funding_status']=fs
            if hit:
                closed=finish(p,hit[0],hit[1],hit[2]); hist.append(closed); del positions[sym]; rec.update(status='CLOSED',last_exit=closed['exit_reason']); p=None
            else:
                mark=float(p.get('last_mark',p['entry'])); marks[sym]=pnl(p,mark)
                rec.update(status='OPEN' if quote_ok else 'OPEN_STALE',side=p['side'],entry=p['entry'],stop=p['stop'],tp=p['tp'],strategy=p.get('strategy','TREND_H1'),net_usd=marks[sym],risk_usd=p['reserve_usd'],quality=p['quality'],funding_status=p['funding_status'])
        sig=signal(rows,now,prior_atr=profile=='CLOSE_PRIOR') if profile in intel.H1_PROFILES else None
        rs=range_signal(item.get('candles_15m',[]),now) if profile in intel.RANGE_PROFILES else None
        if profile=='EARLY_H1_M5' and (not sig or not sig['side'] or now-sig['signal_ts']>15*MINUTE):
            sig=early_signal(rows,item.get('candles_5m',[]),now) or sig
        if sig: rec.update(channel_high=sig['upper'],channel_low=sig['lower'],atr_h1=sig['atr'],signal_side=sig['side'],signal_ts=sig['signal_ts'])
        if rs: rec.update(range_signal_side=rs['side'],range_mean=rs['mean'],range_efficiency=rs['efficiency'])
        if not had_position:
            # Compare the two strategies without forcing H1 confirmation of range trades.
            # Same-symbol simultaneous valid signals: prefer H1 if executable; otherwise range.
            common=quote_ok and item.get('trend_tick_valid') and m1 and 0<=now-(int(m1[-1]['t'])*1000+MINUTE)<=2*MINUTE
            opts=[]
            for st,z,events in (('TREND_H1',sig,seen),('RANGE_M15',rs,seen_range)):
                if z and z['side'] and 0<=now-z['signal_ts']<=15*MINUTE and events.get(sym,0)<z['signal_ts']:
                    opts.append((st,z))
            if not common or not (sig or rs): rec['status']='DATA_UNAVAILABLE'
            elif opts and not allow_new: rec['status']='RESEARCH_DRAIN_ONLY'
            elif opts: candidates.append((opts[0][1]['strength'],sym,group,opts,item,rec))
            elif any(z and z['side'] for z in (sig,rs)):
                rec['status']='ALREADY_EVALUATED' if any(z and z['side'] and events.get(sym,0)>=z['signal_ts'] for z,events in ((sig,seen),(rs,seen_range))) else 'STALE_H1_BREAKOUT'
            else: rec['status']='NO_BREAKOUT'
        # One position per symbol; do not re-enter the same signal after a close.
        else:
            if sig and sig['side']: seen[sym]=max(seen.get(sym,0),sig['signal_ts'])
            if rs and rs['side']: seen_range[sym]=max(seen_range.get(sym,0),rs['signal_ts'])
        result.append(rec)
    # Reserve initial stop risk conservatively until close, including gap/spread risk caveat.
    realised=sum(x['net_usd'] for x in hist)+book.get('archived_pnl',0.)
    for sym,p in positions.items():
        if sym not in marks:
            marks[sym]=pnl(p,float(p.get('last_mark',p['entry'])))
            result.append({'symbol':sym,'cluster':p['cluster'],'status':'OPEN_STALE','side':p['side'],'entry':p['entry'],'stop':p['stop'],'tp':p['tp'],'strategy':p.get('strategy','TREND_H1'),'net_usd':marks[sym],'quality':p['quality'],'funding_status':p.get('funding_status')})
    capital=max(0.,min(deposit,deposit+realised+sum(min(0.,v) for v in marks.values())))
    total_risk=capital*risk_pct/100; total_nominal=capital*max_leverage
    def used():
        by=defaultdict(float)
        for p in positions.values(): by[p['cluster']]+=p['reserve_usd']
        return sum(p['reserve_usd'] for p in positions.values()),sum(p['notional'] for p in positions.values()),by
    def budget(group):
        ur,un,groups=used(); group_cap=total_risk/3.
        return min(total_risk-ur,group_cap-groups[group],group_cap/2 if group=='CRYPTO' else group_cap),total_nominal-un

    def prepare(sym,group,strategy,sig,item,rec):
        s=sig['side']; px=float(item['ask'] if s==1 else item['bid']); spread=(item['ask']-item['bid'])/px
        if spread>.25*sig['atr']/px: return None,'WIDE_SPREAD',0.
        available,nominal=budget(group)
        contract=item.get('contract_spec'); require=bool(item.get('require_contract_spec'))
        c_spec=intel.contract_spec(contract)
        lev=min(max_leverage,c_spec.get('maxLeverage',max_leverage)) if c_spec else max_leverage
        # Per-symbol exchange leverage and portfolio margin cap. Old positions retain
        # their quantities; their reserved margin is estimated with the prior cap.
        margin_used=sum(p.get('margin_usd',p['notional']/max_leverage) for p in positions.values())
        nominal=min(nominal,max(0.,capital-margin_used)*lev)
        frows=rows_by_symbol[sym] if strategy=='TREND_H1' else [b for b in item.get('candles_15m',[]) if valid_bar(b) and int(b['t'])*1000+15*MINUTE<=now]
        features=intel.market_features(frows,sig)
        if sig.get('route')=='EARLY_M5_H1_CHANNEL':
            q5=[b for b in item.get('candles_5m',[]) if valid_bar(b) and int(b['t'])*1000+5*MINUTE<=now]
            early=intel.market_features(q5,sig)
            features.update({k:early[k] for k in ('breakout_quality','close_location','body_atr') if k in early})
        est=intel.estimate(hist,sym,s,features['regime'],strategy,now) if profile in intel.ADAPTIVE_PROFILES else None
        overlap=intel.signed_overlap(sym,s,positions,rows_by_symbol) if profile in intel.ADAPTIVE_PROFILES and available>=intel.MIN_RISK_BUDGET else 0.
        # Soft risk allocation for challengers only; never raises cluster/total cap.
        if est is not None:
            weight=max(.5,min(1.,.80+.20*features['breakout_quality']+.25*est['expected_r']-.25*overlap))
            if strategy=='RANGE_M15': weight*=1.-.25*max(0.,-features['directional_efficiency'])
            # If residual budget is barely executable, do not create a fractional ghost.
            available=min(available,max(intel.MIN_RISK_BUDGET,available*weight))
        anchor='CLOSE' if profile=='CLOSE_PRIOR' else 'PEAK'
        use_sig=sig
        if profile=='ADAPTIVE_H1':
            anchor=intel.choose_anchor(hist,sym,s,features,now)
            if anchor=='CLOSE':
                prior=signal(rows_by_symbol[sym],now,prior_atr=True)
                if prior and prior['side']==s: use_sig=prior
        opener=open_position if strategy=='TREND_H1' else open_range
        p,reason=opener(sym,group,use_sig,px,now,tick=float(item['tick']),risk_budget=available,notional_budget=nominal,fee=fee,slip=slip,contract=contract,require_contract=require)
        if not p: return None,reason,0.
        p.update(strategy=strategy,trail_anchor=anchor,margin_usd=p['notional']/lev,leverage_cap=lev,entry_regime=features['regime'],entry_features=features,entry_route=sig.get('route','CLOSED_H1' if strategy=='TREND_H1' else 'M15_RECLAIM'))
        if profile in ('ADAPTIVE_RANGE','RANGE_LOCK_ONLY'): p['range_exit_policy']='M15_LOCK'
        if est is not None:
            p.update(research_estimate=est,entry_overlap=overlap)
            # Rank in estimated USD on the proposed risk, with a soft evidence tie-break.
            # A zero-data prior does NOT assert profitable expectancy.
            p['expected_net_usdt']=est['expected_r']*p['risk_usd']
            priority=p['expected_net_usdt']+.12*p['risk_usd']*(features['breakout_quality']-.5*overlap)
        else: priority=sig['strength']
        return p,reason,priority

    def admit(p,sym,opts,item,rec):
        s=p['side']; p['last_mark']=float(item['bid'] if s==1 else item['ask']); positions[sym]=p
        for st,z in opts: (seen if st=='TREND_H1' else seen_range)[sym]=z['signal_ts']
        marks[sym]=pnl(p,p['last_mark'])
        rec.update(status='OPEN',side=s,entry=p['entry'],stop=p['stop'],tp=p['tp'],strategy=p['strategy'],net_usd=marks[sym],risk_usd=p['reserve_usd'],quality=p['quality'],funding_status=p['funding_status'],quantity_policy=p['quantity_policy'],expected_net_usdt=p.get('expected_net_usdt'),entry_route=p.get('entry_route'))

    if profile in intel.ADAPTIVE_PROFILES:
        # Re-evaluate feasible allocations after each admitted candidate; no use of future outcomes.
        remaining=list(candidates)
        while remaining:
            feasible=[]
            for row in remaining:
                _,sym,group,opts,item,rec=row
                for strategy,sig in opts:
                    p,reason,priority=prepare(sym,group,strategy,sig,item,rec)
                    rec.update(status=reason,candidate_strategy=strategy)
                    if p: feasible.append((priority,sym,p,row))
            if not feasible: break
            _,sym,p,row=sorted(feasible,key=lambda x:(-x[0],x[1]))[0]
            admit(p,sym,row[3],row[4],row[5]); remaining=[r for r in remaining if r[1]!=sym]
    else:
        for _,sym,group,opts,item,rec in sorted(candidates,key=lambda x:(-x[0],x[1])):
            for strategy,sig in opts:
                p,reason,_=prepare(sym,group,strategy,sig,item,rec)
                rec.update(status=reason,candidate_strategy=strategy)
                if p: admit(p,sym,opts,item,rec); break
    # Retain full cumulative stats even after bounded history. No mixing with old PAPER.
    summary=stats(hist); archived=book.setdefault('archive_stats',{'trades':0,'wins':0,'net_r':0.,'gross_w':0.,'gross_l':0.})
    all_n=archived['trades']+summary['trades']; all_w=archived['wins']+summary['wins']
    gw=archived['gross_w']+sum(max(0,x['net_usd']) for x in hist); gl=archived['gross_l']-sum(min(0,x['net_usd']) for x in hist)
    unreal=sum(marks.values()); # Missing symbol mark: retain last known, explicitly stale.
    for sym,p in positions.items():
        if sym not in marks: unreal+=pnl(p,float(p.get('last_mark',p['entry'])))
    equity=deposit+realised+unreal; book['peak_equity']=max(book.get('peak_equity',deposit),equity)
    book['scan_mdd']=max(book.get('scan_mdd',0.),book['peak_equity']-equity)
    ur,un,_=used()
    all_r=archived['net_r']+summary['net_r']
    if len(hist)>2000:
        old=hist[:-2000]; z=stats(old); archived['trades']+=z['trades']; archived['wins']+=z['wins']; archived['net_r']+=z['net_r']; archived['gross_w']+=sum(max(0,x['net_usd']) for x in old); archived['gross_l']-=sum(min(0,x['net_usd']) for x in old); book['archived_pnl']=book.get('archived_pnl',0.)+z['net_usd']; book['history']=hist[-2000:]
    return {'mode':'PAPER_SEPARATE_ACCOUNT','schema':SCHEMA,'profile':profile,'asof_ts':now,'initial_deposit':deposit,'realised_usdt':realised,'unrealised_usdt':unreal,'equity_usdt':equity,'scan_mdd_usdt':book['scan_mdd'],'trades':all_n,'wins':all_w,'win_rate':all_w/all_n if all_n else None,'pf':gw/gl if gl else None,'net_r':all_r,
            'execution_schema':intel.EXECUTION_SCHEMA,'allow_new':allow_new,'legacy_dust_voided':len(book.get('voided_numerical_positions',[])),'legacy_subminimum_retained':sum(x.get('notional',0)<intel.MIN_NOTIONAL-1e-9 or x.get('reserve_usd',0)<intel.MIN_RISK_BUDGET-1e-9 for x in hist), 'v159_closed_stats_retained':stats([x for x in hist if x.get('execution_schema')==intel.EXECUTION_SCHEMA]),'risk_budget_usdt':total_risk,'reserved_usdt':ur,'notional_cap_usdt':total_nominal,'notional_usdt':un,'open_count':len(positions),'assets':result,'cost_assumption':'TAKER_PLUS_SLIP_AND_QUOTED_SPREAD','mdd_type':'SCAN_MARKS_ONLY','recent_trades':book['history'][-10:]}
