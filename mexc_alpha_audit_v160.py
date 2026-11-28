#!/usr/bin/env python3
"""Seven-asset M1/M5 candidate audit, not a portfolio backtest.
Uses current scanner and every family, even when slots would be occupied.
Independent static brackets, observed quote proxy; no automatic policy promotion.
"""
from __future__ import annotations
import argparse,bisect,csv,hashlib,json,math,os,time
from collections import defaultdict,Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import mean
from mexc_replay_v154 import read_bars,iso_ms,download
import mexc_multi_scanner as sc
from mexc_opportunity import SOFT,cost_r
M=60000; DAY=1440*M
FAMILIES=('LIQUIDITY_SWEEP','TREND_PULLBACK','BREAKOUT_RETEST','MOMENTUM','FAILED_BREAKOUT','EXHAUSTION','EARLY_STRUCTURE_SHIFT','COUNTERTREND_WAVE','STRUCTURE_CONTINUATION')
DEVELOPMENT_END=iso_ms('2026-11-28')


def label(d,raw,idx,end,horizon=1080):
    """Causal, fixed bracket; unknown M1 sequence flagged, no pre-decision prices.
    LIMIT needs a tick penetration, stop-first, no same-bar favorable take.
    An unfilled order is not a win or loss; report zero opportunity R separately.
    """
    e,st,tp,tick,risk=[float(d[k]) for k in ('entry','stop','tp','tick','risk')]; side=d['side']; decision=d['decision_ts']
    if side not in (-1,1) or not all(math.isfinite(v) and v>0 for v in (e,st,tp,tick,risk)) or side*(e-st)<=0 or side*(tp-e)<=0: raise ValueError('invalid bracket')
    filled=d['order_type']=='MARKET'; opened=decision if filled else None
    expiry=decision+int(d.get('limit_ttl_min') or 8)*M; cursor=decision; quality='OBSERVED_M1'; mfe=mae=0.
    def done(reason,t,px=None):
        return {'filled':filled,'opened_ts':opened,'closed_ts':t,'exit':px,'reason':reason,'quality':quality,'net_r':side*(px-e)/risk-cost_r(d,px,partial=False) if px is not None else 0. if reason in ('UNFILLED','UNFILLED_TARGET') else None,'mfe_r':mfe if filled else None,'mae_r':mae if filled else None}
    for b in raw[idx:idx+horizon+int(d.get('limit_ttl_min') or 8)+2]:
        t=b.ts+M
        if t>end: break
        if b.ts!=cursor:
            quality='DATA_GAP';return done('CENSORED_GAP',None)
        cursor=t
        stop=b.low<=st if side==1 else b.high>=st; target=b.high>=tp if side==1 else b.low<=tp
        if not filled:
            if t>expiry:return done('UNFILLED',expiry)
            if (b.low<=e-tick if side==1 else b.high>=e+tick):
                filled=True;opened=t
                if target:quality='AMBIGUOUS_OHLC'
                if stop:
                    if target:quality='AMBIGUOUS_OHLC'
                    return done('STOP_ON_FILL',t,min(st,b.open) if side==1 else max(st,b.open))
                continue
            if target:return done('UNFILLED_TARGET',t)
            continue
        if stop:
            if target:quality='AMBIGUOUS_OHLC'
            px=min(st,b.open) if side==1 else max(st,b.open);mae=max(mae,-side*(px-e)/risk)
            return done('STOP',t,px)
        mae=max(mae,(e-b.low)/risk if side==1 else (b.high-e)/risk)
        mfe=max(mfe,min(side*(tp-e)/risk,(b.high-e)/risk if side==1 else (e-b.low)/risk))
        if target:return done('TARGET',t,tp)
        if t>=opened+horizon*M:return done('TIME',t,b.close)
    return done('CENSORED',None)


def collect(args):
    path,symbol,start,end,interval,spread=args; path=Path(path);spec=next(x for x in sc.INSTRUMENTS if x['symbol']==symbol)
    os.environ.update(BZU_ALLOW_M1_ENTRY='true',MEXC_TAKER_FEE_RATE='.0002',MEXC_MAKER_FEE_RATE='.0002',MEXC_SLIPPAGE_RATE='.0001')
    ticks=json.loads((path/'ticks.json').read_text()); series={};indexes={}; coverage={}
    for sym in (symbol,'BTC_USDT'):
        raw=read_bars(sc,path/(sym+'.csv')); raw=[b for b in raw if start-8*DAY<=b.ts<end]
        if not raw or raw[0].ts>start-5*DAY or raw[-1].ts+M<end: raise ValueError('Incomplete coverage: '+sym)
        gaps=sum(b.ts-a.ts!=M for a,b in zip(raw,raw[1:])); coverage[sym]={'rows':len(raw),'first':raw[0].ts,'last_close':raw[-1].ts+M,'gaps':gaps,'sha256':hashlib.sha256((path/(sym+'.csv')).read_bytes()).hexdigest()}
        for tf in (60,300,900,3600,14400):
            rows=raw if tf==60 else sc.aggregate_bars(raw,60,tf,end);series[sym,tf]=rows;indexes[sym,tf]=[b.ts for b in rows]
    def closed(sym,tf,t,n):
        j=bisect.bisect_right(indexes[sym,tf],t-tf*1000);return series[sym,tf][max(0,j-n):j]
    def quote(sym,t):
        ix=bisect.bisect_left(indexes[sym,60],t);rows=series[sym,60]
        if ix>=len(rows) or rows[ix].ts!=t:return {},ix
        q=rows[ix].open;half=q*spread/20000;return {'lastPrice':q,'bid1':q-half,'ask1':q+half,'_observed_ts':t},ix
    seen=set(); detected={}; labels=[]; prior=[]; fresh=0; scans=0
    for t in range(start,end,interval*1000):
        b15=closed('BTC_USDT',900,t,110);b1=closed('BTC_USDT',3600,t,145);bq,_=quote('BTC_USDT',t)
        macro=sc.analyze_btc_macro(b15,b1,bq.get('lastPrice',b15[-1].close if b15 else 0.)); qt,ix=quote(symbol,t)
        res=sc.analyze_instrument(spec,ticker=qt,bars_1m=closed(symbol,60,t,300),bars_5m=closed(symbol,300,t,130),bars_15m=closed(symbol,900,t,110),bars_1h=closed(symbol,3600,t,145),bars_4h=closed(symbol,14400,t,35),btc_bars_15m=b15,tick=float(ticks[symbol]),btc_macro=macro,now_ms=t,history=[],consumed_signals={},prior_breaks=prior)
        fresh+=bool(res.get('analysis_fresh')); scans+=1; prior=res.get('opportunity_breaks',prior)
        for d in res.get('candidate_diagnostics',[]):
            if not d.get('signal_id') or not d.get('family'):continue
            key=(d['signal_id'],d['order_type']); reasons=set(d.get('rejection_codes') or [])
            state=detected.setdefault(key,{'symbol':symbol,'family':d['family'],'side':d['side'],'trigger_tf':d['trigger_tf'],'variant':d.get('research_variant') or 'ACTIVE','detected_ts':t,'reasons':sorted(reasons),'executable':False})
            # All nine families, including experimental ones. Other strategy-only rejections
            # can be explored without asserting executable live orders. Hard execution guards remain.
            executable=d.get('valid') and not reasons-(SOFT|{'RESEARCH_ONLY'}) and all(isinstance(d.get(k),(int,float)) and math.isfinite(d[k]) and d[k]>0 for k in ('entry','stop','tp','tick','risk'))
            if not executable or key in seen:continue
            if d['side']*(d['entry']-d['stop'])<=0 or d['side']*(d['tp']-d['entry'])<=0:continue
            state['executable']=True;seen.add(key)
            x={**d,'symbol':symbol,'decision_ts':t,'active_eligible':d['status']=='READY','phase':res.get('market_state',{}).get('phase')}
            # Price/geometry/route chosen without reading future bars; only label computation looks ahead.
            x.update(label(x,series[symbol,60],ix,end)); labels.append(x)
        if scans%1000==0:print(symbol,scans,'/',math.ceil((end-start)/(interval*1000)),len(labels),flush=True)
    print(symbol,'complete',scans,'candidates',len(detected),'routes',len(labels),flush=True)
    return {'symbol':symbol,'scans':scans,'fresh':fresh,'coverage':coverage,'detections':list(detected.values()),'routes':labels}


def metrics(rows):
    resolved=[r for r in rows if r['net_r'] is not None and r['quality']=='OBSERVED_M1'];tr=[r for r in resolved if r['filled']];vals=[r['net_r'] for r in tr];gw=sum(max(0,v) for v in vals);gl=-sum(min(0,v) for v in vals)
    # Routes may share the same market event. Event-weighted averages avoid multiplying evidence.
    episodes=defaultdict(list)
    for r in resolved:episodes[(r['symbol'],r['side'],r.get('signal_event_key') or r['signal_id'])].append(r['net_r'])
    return {'attempts':len(rows),'usable_resolved':len(resolved),'filled':len(tr),'unfilled':len(resolved)-len(tr),'censored_or_ambiguous':len(rows)-len(resolved),'win_rate':sum(v>0 for v in vals)/len(vals) if vals else None,'net_r_routes':sum(vals),'pf_r':gw/gl if gl else None,'mean_r_filled':mean(vals) if vals else None,'events':len(episodes),'mean_r_per_event_attempt':mean(mean(v) for v in episodes.values()) if episodes else None}


def summarize(results,start,end,interval,spread,unseen):
    routes=[r for a in results for r in a['routes']];ds=[r for a in results for r in a['detections']]; grouped=defaultdict(list)
    for r in routes:
        for k in ((r['family'],),(r['symbol'],r['family']),(r['family'],str(r['side']),r['trigger_tf']),(r['family'],str(r['research_variant'] or 'ACTIVE'))):grouped['|'.join(k)].append(r)
    families={f:{**metrics(grouped[f]),'detected':sum(d['family']==f for d in ds),'executable':sum(d['family']==f and d['executable'] for d in ds)} for f in FAMILIES}
    return {'schema':'ALPHA_CANDIDATE_AUDIT_160','scope':'STATIC_COUNTERFACTUAL_NOT_PORTFOLIO_NO_META','period_status':'POST_DEVELOPMENT_WINDOW' if unseen else 'DEVELOPMENT_REUSED_HISTORY','start_ts':start,'end_exclusive_ts':end,'cadence_sec':interval,'spread_bps':spread,'costs':'0.02% maker/taker; 0.01% slippage except passive entry; no funding','coverage':{a['symbol']:a['coverage'] for a in results},'scans':sum(a['scans'] for a in results),'fresh_scans':sum(a['fresh'] for a in results),'total':metrics(routes),'families':families,'by_group':{k:metrics(v) for k,v in sorted(grouped.items())},'by_asset':{a['symbol']:metrics(a['routes']) for a in results},'rejections_first_observation':dict(Counter(z for d in ds for z in d['reasons'])),'notes':'Cold detector audit (history=[] intentionally); no slots or capital, fixed brackets, no trailing. Non-independent route outcomes cannot be summed as portfolio profit. 18h horizon; ambiguous/censored excluded; no fabricated trades for physically invalid candidates.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--download',action='store_true');p.add_argument('--interval',type=int,default=180);p.add_argument('--spread-bps',type=float,default=0.);p.add_argument('--workers',type=int,default=3);p.add_argument('--forward',action='store_true');p.add_argument('--out',type=Path,required=True);a=p.parse_args();start,end=map(iso_ms,(a.start,a.end))
    if start>=end or a.interval<60 or a.interval%60 or not math.isfinite(a.spread_bps) or a.spread_bps<0 or not 1<=a.workers<=7: p.error('invalid interval, dates, spread or workers')
    if a.forward and (start<DEVELOPMENT_END or end-start<7*DAY):p.error('Forward validation needs at least 7 days starting 2026-11-28 or later')
    if a.download:download(sc,a.data,start-8*DAY,end,export_contracts=True)
    a.out.mkdir(parents=True,exist_ok=True); inputs=[(a.data,s['symbol'],start,end,a.interval,a.spread_bps) for s in sc.INSTRUMENTS]
    results=[]
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        for row in pool.map(collect,inputs):
            (a.out/(row['symbol']+'.json')).write_text(json.dumps(row,ensure_ascii=False));results.append(row)
    report=summarize(results,start,end,a.interval,a.spread_bps,a.forward);(a.out/'ALPHA_AUDIT_REPORT.json').write_text(json.dumps(report,indent=2,ensure_ascii=False));print(json.dumps(report['families'],indent=2),flush=True)
if __name__=='__main__':main()
