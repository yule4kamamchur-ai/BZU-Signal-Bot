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
import mexc_portfolio_intelligence as intel
import mexc_trend_research as tr
import hashlib
from mexc_replay_v154 import read_bars,download,iso_ms


DAY=86400000


def utc(ts):
    return datetime.datetime.fromtimestamp(ts/1000,datetime.timezone.utc).isoformat()


class CoverageError(ValueError):
    pass


def select_window(series,start,end,profile,mode):
    """Only use observed data. Available mode clips to a common, explicit UTC interval."""
    if mode not in ('strict','available'):
        raise ValueError('Invalid coverage mode')
    need_h1=profile in intel.H1_PROFILES
    need_m15=profile in intel.RANGE_PROFILES
    h1_warmup=41 if profile in (*tr.CHANNEL_PROFILES,'H1_OIL_CONTEXT') else 31
    manifest={'mode':mode,'requested_start_utc':utc(start),'requested_end_utc':utc(end),
              'requested_start_ts':start,'requested_end_ts':end,'assets':{},'errors':[]}
    earliest=[]; latest=[]
    for sym,(raw,ix,hrs,hix,m15,m15ix) in series.items():
        first=raw[0].ts; last=raw[-1].ts+h.MINUTE
        info={'first_m1_utc':utc(first),'last_m1_close_utc':utc(last),
              'h1_before_requested_start':bisect.bisect_right(hix,start-h.HOUR),
              'm15_before_requested_start':bisect.bisect_right(m15ix,start-15*h.MINUTE),
              'm1_rows':len(raw)}
        manifest['assets'][sym]=info
        ready=first
        for needed,bars,count,step,label in ((need_h1,hrs,h1_warmup,h.HOUR,'H1'),(need_m15,m15,21,15*h.MINUTE,'M15')):
            if not needed: continue
            if len(bars)<count:
                manifest['errors'].append(f'{sym}: only {len(bars)} complete {label} bars, need {count}')
                continue
            ready=max(ready,bars[count-1].ts+step)
        info['earliest_warmed_start_utc']=utc(ready)
        earliest.append(ready);latest.append(last)
    if manifest['errors']: return manifest,None,None
    common_start=max(earliest);common_end=min(latest)
    actual_start,actual_end=start,end
    if mode=='available':
        # Choose whole UTC days when clipping a date-based request, for interpretable weekly reports.
        if common_start>start: actual_start=(common_start+DAY-1)//DAY*DAY
        if common_end<end: actual_end=common_end//DAY*DAY
    elif common_start>start or common_end<end:
        manifest['errors'].append('Requested interval unavailable: insufficient warmup or end coverage. Use a shorter date range or explicitly choose available mode.')
    manifest.update(effective_start_ts=actual_start,effective_end_ts=actual_end,
                    effective_start_utc=utc(actual_start),effective_end_utc=utc(actual_end),
                    duration_days=max(0,actual_end-actual_start)/DAY,
                    clipped=(actual_start!=start or actual_end!=end),
                    common_earliest_warmed_utc=utc(common_start),common_last_m1_close_utc=utc(common_end))
    if actual_end<=actual_start:
        manifest['errors'].append('No common warmed period for all selected instruments')
    if mode=='available' and manifest['clipped'] and actual_end-actual_start<7*DAY:
        manifest['errors'].append('Clipped interval shorter than 7 days; supply more historical data')
    for sym,(raw,ix,hrs,hix,m15,m15ix) in series.items():
        info=manifest['assets'][sym]; a=bisect.bisect_left(ix,actual_start); b=bisect.bisect_left(ix,actual_end)
        info.update(h1_before_effective_start=bisect.bisect_right(hix,actual_start-h.HOUR),
                    m15_before_effective_start=bisect.bisect_right(m15ix,actual_start-15*h.MINUTE),
                    test_m1_observed=max(0,b-a),expected_calendar_minutes=max(0,(actual_end-actual_start)//h.MINUTE),
                    test_gap_count=sum(y-x!=h.MINUTE for x,y in zip(ix[a:b],ix[a+1:b])))
        if a<b:
            info.update(first_test_m1_utc=utc(ix[a]),last_test_m1_close_utc=utc(ix[b-1]+h.MINUTE))
        if (need_h1 and info['h1_before_effective_start']<h1_warmup) or (need_m15 and info['m15_before_effective_start']<21):
            manifest['errors'].append(f'{sym}: insufficient closed warmup bars before effective start')
        if a>=b: manifest['errors'].append(f'{sym}: no test-period bars')
    manifest['notes']='Calendar-minute gaps may be market closures or missing data; never filled. Requested/effective end is exclusive.'
    return manifest,actual_start,actual_end


def replay(path,start,end,interval=180,spread_bps=0.,deposit=100.,risk=3.,leverage=5.,profile="PEAK_CURRENT",symbols=None,coverage_mode='strict',coverage_out=None,require_contract_specs=False,forward=False):
    if forward and (start<iso_ms('2026-11-28') or end-start<7*DAY or coverage_mode!='strict'):
        raise ValueError('Post-development validation requires strict coverage, start >= 2026-11-28, >= 7 days')
    if not start<end or interval<60 or interval%60 or not math.isfinite(spread_bps) or spread_bps<0: raise ValueError('Invalid period/cadence/spread')
    ticks=json.loads((path/'ticks.json').read_text()); metadata=json.loads((path/'contract_specs.json').read_text()) if (path/'contract_specs.json').exists() else {}; contracts=metadata.get('specs',{}); m5_series={}; series={}; coverage={}; book={}; cycles=0; status=None; observations=defaultdict(int)
    specs=[x for x in sc.INSTRUMENTS if symbols is None or x['symbol'] in symbols]
    if not specs or (symbols is not None and set(symbols)!={x['symbol'] for x in specs}): raise ValueError('Invalid symbol selection')
    missing=[x['symbol'] for x in specs if intel.contract_spec(contracts.get(x['symbol'])) is None]
    for spec in specs:
        sym=spec['symbol']; tick=float(ticks[sym]); rows=read_bars(sc,path/(sym+'.csv'))
        raw=[b for b in rows if b.ts+h.MINUTE<=end]
        if not raw or not math.isfinite(tick) or tick<=0: raise ValueError(f'No valid bars/tick: {sym}')
        hrs=sc.aggregate_bars(raw,60,3600,end); m15=sc.aggregate_bars(raw,60,900,end) if profile in intel.RANGE_PROFILES else []
        series[sym]=(raw,[b.ts for b in raw],hrs,[b.ts for b in hrs],m15,[b.ts for b in m15])
        if profile=='EARLY_H1_M5':
            m5=sc.aggregate_bars(raw,60,300,end); m5_series[sym]=(m5,[b.ts for b in m5])
    window,start,end=select_window(series,start,end,profile,coverage_mode)
    if require_contract_specs and missing: window['errors'].append('Missing contract quantity metadata: '+', '.join(missing))
    window['period_status']='POST_DEVELOPMENT_WINDOW' if forward else 'DEVELOPMENT_REUSED_HISTORY'
    window['quantity_metadata']=metadata
    window['missing_contract_specs']=missing
    window['quantity_policy']='CONTINUOUS_PROXY_FOR_MISSING' if missing else 'MEXC_CONTRACT_STEP_CURRENT_SNAPSHOT'
    window['status']='INSUFFICIENT_COVERAGE' if window['errors'] else ('AVAILABLE_INTERVAL' if window['clipped'] else 'REQUESTED_INTERVAL')
    if coverage_out is not None: Path(coverage_out).write_text(json.dumps(window,indent=2))
    print('Coverage preflight:',json.dumps(window,indent=2),flush=True)
    if window['errors']: raise CoverageError('; '.join(window['errors']))
    if window['clipped']: print(f"WARNING: requested interval shortened to {utc(start)} -- {utc(end)}; {window['duration_days']:.1f} days, not the originally requested period",flush=True)
    for sym,(raw,ix,hrs,hix,m15,m15ix) in series.items():
        coverage[sym]={'rows':len(raw),'first_ts':raw[0].ts,'last_ts':raw[-1].ts,'gaps_between_rows':sum(b.ts-a.ts!=h.MINUTE for a,b in zip(raw,raw[1:])),'test_period_minutes_observed':window['assets'][sym]['test_m1_observed'],'csv_sha256':hashlib.sha256((path/(sym+'.csv')).read_bytes()).hexdigest()}
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
            item['contract_spec']=contracts.get(sym); item['require_contract_spec']=require_contract_specs
            if profile=='EARLY_H1_M5':
                m5,m5ix=m5_series[sym]; k5=bisect.bisect_right(m5ix,t-5*h.MINUTE); item['candles_5m']=h.pack(m5[max(0,k5-15):k5])
            items.append(item)
        status=h.update(book,items,t,deposit=deposit,risk_pct=risk,max_leverage=leverage,fee=fee,slip=slip,profile=profile);cycles+=1
        for x in status['assets']: observations[x['symbol']+':'+x['status']]+=1
        if cycles%3000==0: print(f'H1 replay {cycles}/{math.ceil((end-start)/(interval*1000))}',flush=True)
    hist=book.get('history',[]); by=defaultdict(list); by_strategy=defaultdict(list); by_side=defaultdict(list); by_route=defaultdict(list); by_regime=defaultdict(list); weekly=defaultdict(list)
    for x in hist:
        by_side[x['symbol']+':'+('LONG' if x['side']==1 else 'SHORT')].append(x); by_route[x.get('entry_route','UNKNOWN')].append(x); by_regime[x.get('entry_regime','UNKNOWN')].append(x)
        by[x['symbol']].append(x); by_strategy[x.get('strategy','TREND_H1')].append(x); weekly[(x['closed_ts']-start)//(7*86400000)].append(x)
    weeks={}
    for k,t in enumerate(range(start,end,7*86400000)):
        stop=min(end,t+7*86400000); z=h.stats(weekly.get(k,[])); weeks[str(k+1)]={**z,'start_ts':t,'end_ts':stop,'days':(stop-t)/86400000,'target_20_met':z['net_usd']>=20 if stop-t==7*86400000 else None}
    return {'status':'RESEARCH_ONLY_NO_FUNDING','execution_schema':intel.EXECUTION_SCHEMA,'git_commit':os.getenv('GITHUB_SHA'),'quantity_policy':window['quantity_policy'],'profile':profile,'period_status':window['period_status'],'coverage_window':window,'coverage':coverage,'cadence_sec':interval,'cycles':cycles,'estimated_taker_plus_slip_per_side':fee+slip,'assumed_spread_bps':spread_bps,'final_snapshot':status,'by_symbol':{k:h.stats(v) for k,v in by.items()},'by_strategy':{k:h.stats(v) for k,v in by_strategy.items()},'by_symbol_side':{k:h.stats(v) for k,v in by_side.items()},'by_route':{k:h.stats(v) for k,v in by_route.items()},'by_regime':{k:h.stats(v) for k,v in by_regime.items()},'weekly':weeks,'status_counts':dict(observations),'closed_trades_retained':hist,'open_positions':book.get('positions',{}),'notes':'Historical quotes proxy M1 open. Funding not available. No forced liquidation; final equity includes last-known marks. Scan-level drawdown only. Each separate research book uses max 3% estimated stop risk, max 5x gross notional, cluster caps, reduced risk after loss; no compounding profits. Gaps may represent market closures or missing data. Retains last 2000 closes; cumulative headline stats remain complete.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--download',action='store_true');p.add_argument('--interval',type=int,default=180);p.add_argument('--spread-bps',type=float,default=0.);p.add_argument('--profile',choices=intel.PROFILES,default='PEAK_CURRENT');p.add_argument('--symbols',nargs='+');p.add_argument('--require-contract-specs',action='store_true');p.add_argument('--forward',action='store_true');p.add_argument('--coverage-mode',choices=['strict','available'],default='strict');p.add_argument('--coverage-out',type=Path,default=Path('H1_M15_COVERAGE.json'));p.add_argument('--out',type=Path,default=Path('H1_PORTFOLIO_REPORT.json'));a=p.parse_args();start,end=map(iso_ms,(a.start,a.end))
    if not start<end: p.error('start must be before end')
    if a.forward and (start<iso_ms('2026-11-28') or end-start<7*DAY or a.coverage_mode!='strict'): p.error('Forward: strict, at least 7 days from 2026-11-28 or later')
    if a.symbols is not None and not set(a.symbols).issubset({x['symbol'] for x in sc.INSTRUMENTS}): p.error('Invalid symbol selection')
    if a.download:download(sc,a.data,start-8*DAY,end,symbols=(a.symbols or [x['symbol'] for x in sc.INSTRUMENTS])+['BTC_USDT'],export_contracts=True)
    result=replay(a.data,start,end,a.interval,a.spread_bps,profile=a.profile,symbols=a.symbols,coverage_mode=a.coverage_mode,coverage_out=a.coverage_out,require_contract_specs=a.require_contract_specs,forward=a.forward);a.out.write_text(json.dumps(result,indent=2));print(json.dumps({k:result['final_snapshot'][k] for k in ('trades','realised_usdt','equity_usdt','pf','scan_mdd_usdt','open_count')},indent=2))
if __name__=='__main__':main()
