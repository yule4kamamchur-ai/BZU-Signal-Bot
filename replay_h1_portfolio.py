#!/usr/bin/env python3
"""Independent H1 PAPER portfolio replay on cached MEXC M1 CSVs.
Uses EXACT live portfolio update, next M1 open quote, closed bars only.
Missing minutes are not interpolated. Ex-funding; scan-mark MDD not tick MDD.
No live memory mutation or calibration import. Reuse mexc_replay_v154 cache.
"""
from __future__ import annotations
import argparse,bisect,json,datetime,math,os
from pathlib import Path
from collections import defaultdict
import mexc_multi_scanner as sc
import mexc_h1_trend as h
from mexc_replay_v154 import read_bars,download,iso_ms


def replay(path,start,end,interval=180,spread_bps=0.,deposit=100.,risk=3.,leverage=5.,profile="PEAK_CURRENT",symbols=None):
    if not start<end or interval<60 or interval%60 or spread_bps<0: raise ValueError('Invalid period/cadence/spread')
    ticks=json.loads((path/'ticks.json').read_text()); series={}; coverage={}; book={}; cycles=0; status=None; observations=defaultdict(int)
    specs=[x for x in sc.INSTRUMENTS if symbols is None or x['symbol'] in symbols]
    if not specs or (symbols is not None and set(symbols)!={x['symbol'] for x in specs}): raise ValueError('Invalid symbol selection')
    for spec in specs:
        sym=spec['symbol']; tick=float(ticks[sym]); rows=read_bars(sc,path/(sym+'.csv'))
        if not rows or not math.isfinite(tick) or tick<=0: raise ValueError(f'No valid bars/tick: {sym}')
        raw=[b for b in rows if b.ts<end]; hrs=sc.aggregate_bars(raw,60,3600,end); m15=sc.aggregate_bars(raw,60,900,end) if profile in ('MIXED_PEAK_RANGE','RANGE_ONLY') else []
        if len([b for b in hrs if b.ts+h.HOUR<=start])<31: raise ValueError(f'Insufficient H1 warmup: {sym}')
        if not any(start<=b.ts<end for b in raw): raise ValueError(f'No test-period bars: {sym}')
        series[sym]=(raw,[b.ts for b in raw],hrs,[b.ts for b in hrs],m15,[b.ts for b in m15]);coverage[sym]={'rows':len(raw),'first_ts':raw[0].ts,'last_ts':raw[-1].ts,'gaps_between_rows':sum(b.ts-a.ts!=h.MINUTE for a,b in zip(raw,raw[1:])),'test_period_minutes_observed':sum(start<=b.ts<end for b in raw)}
    fee,slip=sc.cost_rates()
    for t in range(start,end,interval*1000):
        items=[]
        for spec in specs:
            sym=spec['symbol']; raw,ix,hrs,hix,m15,m15ix=series[sym]; m=bisect.bisect_left(ix,t); j=bisect.bisect_right(hix,t-h.HOUR)
            q=raw[m].open if m<len(raw) and raw[m].ts==t else 0.
            p=book.get('positions',{}).get(sym); cursor=p['processed_until_ts'] if p else t-h.MINUTE
            a=max(0,bisect.bisect_left(ix,cursor-h.MINUTE),m-1400)
            half=q*spread_bps/20000; k=bisect.bisect_right(m15ix,t-15*h.MINUTE)
            item={'symbol':sym,'cluster':spec['cluster'],'tick':float(ticks[sym]),'trend_tick_valid':True,'quote_valid':q>0,'quote_ts':t,'bid':q-half,'ask':q+half,'candles_1m':h.pack(raw[a:m]),'_trend_h1':h.pack(hrs[max(0,j-145):j]),'candles_15m':h.pack(m15[max(0,k-25):k])}
            items.append(item)
        status=h.update(book,items,t,deposit=deposit,risk_pct=risk,max_leverage=leverage,fee=fee,slip=slip,profile=profile);cycles+=1
        for x in status['assets']: observations[x['symbol']+':'+x['status']]+=1
        if cycles%3000==0: print(f'H1 replay {cycles}/{math.ceil((end-start)/(interval*1000))}',flush=True)
    hist=book.get('history',[]); by=defaultdict(list); by_strategy=defaultdict(list); weekly=defaultdict(list)
    for x in hist:
        by[x['symbol']].append(x); by_strategy[x.get('strategy','TREND_H1')].append(x); weekly[(x['closed_ts']-start)//(7*86400000)].append(x)
    weeks={}
    for k,t in enumerate(range(start,end,7*86400000)):
        stop=min(end,t+7*86400000); z=h.stats(weekly.get(k,[])); weeks[str(k+1)]={**z,'start_ts':t,'end_ts':stop,'days':(stop-t)/86400000,'target_20_met':z['net_usd']>=20 if stop-t==7*86400000 else None}
    return {'status':'RESEARCH_ONLY_NO_FUNDING','profile':profile,'coverage':coverage,'cadence_sec':interval,'cycles':cycles,'estimated_taker_plus_slip_per_side':fee+slip,'assumed_spread_bps':spread_bps,'final_snapshot':status,'by_symbol':{k:h.stats(v) for k,v in by.items()},'by_strategy':{k:h.stats(v) for k,v in by_strategy.items()},'weekly':weeks,'status_counts':dict(observations),'closed_trades_retained':hist,'open_positions':book.get('positions',{}),'notes':'Historical quotes proxy M1 open. Funding not available. No forced liquidation; final equity includes last-known marks. Scan-level drawdown only. Each separate research book uses max 3% estimated stop risk, max 5x gross notional, cluster caps, reduced risk after loss; no compounding profits. Gaps may represent market closures or missing data. Retains last 2000 closes; cumulative headline stats remain complete.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--download',action='store_true');p.add_argument('--interval',type=int,default=180);p.add_argument('--spread-bps',type=float,default=0.);p.add_argument('--profile',choices=['PEAK_CURRENT','CLOSE_PRIOR','MIXED_PEAK_RANGE','RANGE_ONLY'],default='MIXED_PEAK_RANGE');p.add_argument('--symbols',nargs='+');p.add_argument('--out',type=Path,default=Path('H1_PORTFOLIO_REPORT.json'));a=p.parse_args();start,end=map(iso_ms,(a.start,a.end))
    if a.download:download(sc,a.data,start-8*86400000,end)
    result=replay(a.data,start,end,a.interval,a.spread_bps,profile=a.profile,symbols=a.symbols);a.out.write_text(json.dumps(result,indent=2));print(json.dumps({k:result['final_snapshot'][k] for k in ('trades','realised_usdt','equity_usdt','pf','scan_mdd_usdt','open_count')},indent=2))
if __name__=='__main__':main()
