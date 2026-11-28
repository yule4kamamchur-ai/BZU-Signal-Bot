#!/usr/bin/env python3
"""Research replay on MEXC M1 CSV with volume. No network on replay; no trading side effects.
CSV schema: ts (UTC ms or seconds),open,high,low,close,volume. Exact tick sizes in ticks.json.
Use --engine-dir to compare unmodified v15.3 using the SAME cache/parameters.
Download from documented public API using --download; cache is resumable in chunks.
"""
from __future__ import annotations
import argparse,bisect,csv,datetime,json,math,os,sys,time
from pathlib import Path
from collections import defaultdict


def iso_ms(s):
    d=datetime.datetime.fromisoformat(s.replace('Z','+00:00')); return int((d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()*1000)


def download(engine,path,start,end,symbols=None,export_contracts=False):
    path.mkdir(parents=True,exist_ok=True)
    contract_specs={}
    ticks=engine.fetch_contract_ticks(contract_specs=contract_specs) if export_contracts else engine.fetch_contract_ticks(); symbols=symbols if symbols is not None else [x['symbol'] for x in engine.INSTRUMENTS]+['BTC_USDT']
    if not all(s in ticks for s in symbols): raise RuntimeError('Missing exact tick metadata; cannot fabricate ticks')
    (path/'ticks.json').write_text(json.dumps(ticks))
    if export_contracts:
        (path/'contract_specs.json').write_text(json.dumps({'source':'MEXC_PUBLIC_CONTRACT_DETAIL','observed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'specs':{s:contract_specs[s] for s in symbols if s in contract_specs}},indent=2))
        print('Contract quantity metadata:',len([s for s in symbols if s in contract_specs]),'/',len(symbols),flush=True)
    for sym in symbols:
        chunks=path/'chunks'/sym;chunks.mkdir(parents=True,exist_ok=True)
        # v15.9 portfolio cache retains previously exported observed candles,
        # including days that have since expired from the exchange API window.
        existing=path/(sym+'.csv')
        allrows={b.ts:b for b in read_bars(engine,existing)} if export_contracts and existing.exists() else {}
        for t in range(start,end,1440*60000):
            stop=min(end,t+1440*60000); file=chunks/f'{t}_{stop}.json'
            rows=[]
            if file.exists():
                try:
                    raw=json.loads(file.read_text())
                    if isinstance(raw,dict) and raw.get('success') is not False:
                        rows=[b for b in engine.parse_mexc_klines(raw,60,stop) if t<=b.ts<stop]
                except (ValueError,OSError,TypeError):
                    rows=[]
            if not rows and export_contracts and all(t0 in allrows for t0 in range(t,stop,60000)):
                rows=[allrows[t0] for t0 in range(t,stop,60000)]
            if not rows:
                url=f'{engine.MEXC_BASE}/api/v1/contract/kline/{sym}?interval=Min1&start={t//1000}&end={stop//1000}'
                raw=engine._http_json(url)
                rows=[b for b in engine.parse_mexc_klines(raw,60,stop) if t<=b.ts<stop]
                # Empty/out-of-window results are not reusable cache hits.
                if rows:
                    tmp=file.with_suffix('.tmp');tmp.write_text(json.dumps(raw));tmp.replace(file)
            for b in rows: allrows[b.ts]=b
        if not allrows or not any(start<=t<end for t in allrows): raise RuntimeError(f'No candles for requested interval: {sym}')
        file=path/(sym+'.csv');tmp=file.with_suffix('.tmp')
        with tmp.open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['ts','open','high','low','close','volume'])
            w.writerows((b.ts,b.open,b.high,b.low,b.close,b.volume) for b in sorted(allrows.values(),key=lambda b:b.ts))
        tmp.replace(file)
        first,last=min(allrows),max(allrows)
        fmt=lambda x:datetime.datetime.fromtimestamp(x/1000,datetime.timezone.utc).isoformat()
        print(f'{sym}: {len(allrows)} M1 cached; first={fmt(first)} last_close={fmt(last+60000)}; requested={fmt(start)} -- {fmt(end)}',flush=True)


def read_bars(engine,file):
    seen={}
    with file.open() as f:
        for r in csv.DictReader(f):
            ts=int(r['ts']);ts=ts*1000 if ts<10_000_000_000 else ts
            o,h,l,c,v=(float(r[x]) for x in ('open','high','low','close','volume'))
            if ts%60000 or not all(math.isfinite(x) for x in (o,h,l,c,v)) or not 0<l<=min(o,c)<=max(o,c)<=h or v<0: raise ValueError(f'Invalid candle {file} {ts}')
            b=engine.Bar(ts,o,h,l,c,v)
            if ts in seen and seen[ts]!=b: raise ValueError(f'Conflicting duplicate {file} {ts}')
            seen[ts]=b
    return sorted(seen.values(),key=lambda b:b.ts)


def metrics(rows):
    vals=[float(r['net_r']) for r in rows]; n=len(vals); wins=sum(x>0 for x in vals); gains=sum(x for x in vals if x>0);loss=-sum(x for x in vals if x<0)
    usd=[float(r['net_usd']) for r in rows];equity=peak=dd=0.
    for v in usd: equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
    return {'trades':n,'wins':wins,'win_rate':wins/n if n else None,'net_r':round(sum(vals),4),'mean_r':sum(vals)/n if n else None,'profit_factor':gains/loss if loss else None,'profit_factor_usdt':sum(v for v in usd if v>0)/(-sum(v for v in usd if v<0)) if any(v<0 for v in usd) else None,'net_usdt':round(sum(usd),2),'max_closed_equity_drawdown_usdt':round(dd,2)}


def replay(engine,path,start,split,end,interval,spread_bps,export_prior=None):
    if not start<split<end: raise ValueError('require start < split < end')
    symbols=[x['symbol'] for x in engine.INSTRUMENTS]+['BTC_USDT']; ticks=json.loads((path/'ticks.json').read_text()); series={};indexes={}; coverage={}
    for sym in symbols:
        raw=read_bars(engine,path/(sym+'.csv'))
        if not raw or raw[0].ts>start-5*86400000 or raw[-1].ts<end-3*86400000: raise ValueError(f'Insufficient warm-up/end coverage: {sym}')
        coverage[sym]={'m1':len(raw),'first':raw[0].ts,'last':raw[-1].ts,'gaps':sum(b.ts-a.ts!=60000 for a,b in zip(raw,raw[1:]))}
        for tf in (60,300,900,3600,14400):
            rows=raw if tf==60 else engine.aggregate_bars(raw,60,tf,end)
            series[sym,tf]=rows;indexes[sym,tf]=[b.ts for b in rows]
    def closed(sym,tf,t,n):
        ix=bisect.bisect_right(indexes[sym,tf],t-tf*1000);return series[sym,tf][max(0,ix-n):ix]
    def quote(sym,t):
        ix=bisect.bisect_left(indexes[sym,60],t);rows=series[sym,60]
        if ix>=len(rows) or rows[ix].ts!=t:return {}
        # The open at decision time is observable; no subsequent high/low/close used for entry.
        mid=rows[ix].open;half=mid*spread_bps/20000
        return {'lastPrice':mid,'bid1':mid-half,'ask1':mid+half,'_observed_ts':t}
    new=hasattr(engine,'shadow_step');memory={};cycles=0;valid_counts=defaultdict(int)
    for t in range(start,end,interval*1000):
        b15=closed('BTC_USDT',900,t,110);b1=closed('BTC_USDT',3600,t,145);bq=quote('BTC_USDT',t)
        macro=engine.analyze_btc_macro(b15,b1,bq.get('lastPrice',b15[-1].close if b15 else 0.))
        hist=sum([memory.get(k,[]) for k in ('oil_history','cm_history','metal_history')],[])
        if new and getattr(engine,'SHADOW_IN_ACTIVE_HISTORY',True):hist+=memory.get('opportunity_ledger',{}).get('resolved',[])
        items=[]
        for spec in engine.INSTRUMENTS:
            sym=spec['symbol']; extra={'prior_breaks':memory.get('opportunity_breaks',{}).get(sym,[])} if new else {}
            item=engine.analyze_instrument(spec,ticker=quote(sym,t),bars_1m=closed(sym,60,t,300),bars_5m=closed(sym,300,t,130),bars_15m=closed(sym,900,t,110),bars_1h=closed(sym,3600,t,145),bars_4h=closed(sym,14400,t,35),btc_bars_15m=b15,tick=float(ticks[sym]),btc_macro=macro,now_ms=t,history=hist,consumed_signals=memory.get('consumed_signals',{}),**extra)
            valid_counts[sym]+=bool(item.get('analysis_fresh'));items.append(item)
        state={'instruments':items,'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}}
        if new:
            engine.shadow_step(memory,items,t);engine.register(memory,items,t)
            for item in items:
                if item.get('analysis_fresh'):memory.setdefault('opportunity_breaks',{})[item['symbol']]=item.get('opportunity_breaks',[])
        engine.update_dual_paper_slots(state,memory,now_ms=t);cycles+=1
        if cycles%300==0: print(f'Replay {cycles}/{math.ceil((end-start)/(interval*1000))} cycles; {datetime.datetime.fromtimestamp(t/1000,datetime.timezone.utc).isoformat()}',flush=True)
    hist=sorted(sum([memory.get(k,[]) for k in ('oil_history','cm_history','metal_history')],[]),key=lambda r:r['closed_ts'])
    train=[r for r in hist if r['closed_ts']<split];test=[r for r in hist if r['opened_ts']>=split and r['closed_ts']<end]
    if export_prior is not None:
        if not new: raise ValueError('Prior export requires opportunity engine')
        # Separate historical source, no holdout outcome imported, no automatic validation.
        rows=[{**r,'source':'HISTORICAL_REPLAY'} for r in train if r.get('quality')=='OBSERVED_M1']
        rows += [{**r,'source':'HISTORICAL_SHADOW'} for r in memory.get('opportunity_ledger',{}).get('resolved',[]) if r.get('quality')=='SHADOW_M1_MODEL' and r.get('net_r') is not None and r['closed_ts']<split]
        rows.sort(key=lambda r:r['closed_ts'])
        export_prior.write_text(json.dumps({'schema':engine.MODEL_SCHEMA,'status':'HISTORICAL_REPLAY_UNVALIDATED','train_end':split,'rows':rows[-1500:]},ensure_ascii=False,indent=2))
    weekly=defaultdict(list); families=defaultdict(list)
    for r in test:
        day=datetime.datetime.fromtimestamp(r['closed_ts']/1000,datetime.timezone.utc).isocalendar();weekly[f'{day.year}-W{day.week:02}'].append(r);families[f"{r['family']}:{r['side']}:{r.get('regime')}"] .append(r)
    seven_day={}
    for t in range(split,end,7*86400000):
        stop=min(end,t+7*86400000); label=datetime.datetime.fromtimestamp(t/1000,datetime.timezone.utc).date().isoformat()
        met=metrics([r for r in test if t<=r['closed_ts']<stop]); seven_day[label]={**met,'days':(stop-t)/86400000,'target_20_met':met['net_usdt']>=20 if stop-t==7*86400000 else None}
    return {'seven_day_holdout':seven_day,'version':engine.SCANNER_VERSION,'status':'HISTORICAL_REPLAY_ASSUMPTIONS_NOT_LIVE_VALIDATION','coverage':coverage,'cycles':cycles,'fresh_cycles_by_symbol':dict(valid_counts),'interval_seconds':interval,'assumed_spread_bps':spread_bps,'funding':'NOT_AVAILABLE_IN_CSV_REPLAY','train':metrics(train),'holdout':metrics(test),'weekly':{k:metrics(v) for k,v in weekly.items()},'by_family_side_regime':{k:metrics(v) for k,v in families.items()},'open_at_end':sum(bool(memory.get(k)) for k in ('oil_active','cm_active','metal_active')),'shadow_summary':{k:len(memory.get('opportunity_ledger',{}).get(k,[])) for k in ('resolved','pending','observations')},'paper_trades':hist,'shadow_trades':memory.get('opportunity_ledger',{}).get('resolved',[]),'notes':'Prequential: past resolved labels only. Positions opened before holdout excluded from holdout. Weekly buckets may be partial; no forced liquidation at end. Static account $100; no compounding. Funding not included; spread/slippage/fee assumptions not true fills.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--start',required=True);p.add_argument('--split',required=True);p.add_argument('--end',required=True);p.add_argument('--engine-dir',type=Path);p.add_argument('--download',action='store_true');p.add_argument('--download-only',action='store_true');p.add_argument('--interval',type=int,default=180);p.add_argument('--spread-bps',type=float,default=2.);p.add_argument('--export-prior',type=Path);p.add_argument('--output',type=Path,default=Path('MEXC_REPLAY_REPORT.json'));a=p.parse_args()
    if a.engine_dir:sys.path.insert(0,str(a.engine_dir.resolve()))
    import mexc_multi_scanner as engine
    start,split,end=map(iso_ms,(a.start,a.split,a.end))
    if a.interval<60 or a.interval%60 or a.spread_bps<0:raise ValueError('interval must be whole minutes and spread nonnegative')
    if a.download or a.download_only:download(engine,a.data,start-8*86400000,end)
    if not a.download_only:
        result=replay(engine,a.data,start,split,end,a.interval,a.spread_bps,a.export_prior);a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:result[k] for k in ('version','train','holdout','weekly')},indent=2))

if __name__=='__main__':main()
