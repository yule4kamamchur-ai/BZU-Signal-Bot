"""Download closed OKX history and compare a fixed policy on later periods.

No credentials, Telegram messages, orders or production state writes. One
predeclared quality policy, three expanding training windows, frozen test
models, a TTL + holding-period embargo, and an unchanged v12.1 baseline.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import bot_oneshot as core
import full_limit_engine as engine
import signal_quality as quality

DAY=86_400_000
ROOT=Path(__file__).resolve().parent
BASELINE_SHA256='3795a12fafd8be26a854cc7e64145a1b5c05d0a19678a61c970979a9681680f6'


def request_json(url: str) -> dict:
    for attempt in range(4):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'BZU-ICT-Research/12.2'})
            with urllib.request.urlopen(req,timeout=20) as response:
                result=json.load(response)
            if str(result.get('code'))!='0':
                raise ValueError('OKX API error: '+str(result.get('msg','unknown')))
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429,500,502,503,504) or attempt==3:
                raise RuntimeError(f'OKX history unavailable: HTTP {exc.code}') from None
        except (TimeoutError,urllib.error.URLError):
            if attempt==3:
                raise RuntimeError('OKX history download timed out') from None
        time.sleep(2**attempt)
    raise RuntimeError('History download failed')


def fetch_rows(instrument: str, timeframe: str, begin: int, end: int, fetch=request_json) -> list[dict]:
    cursor=end;found={}
    for _ in range(10000):
        url='https://www.okx.com/api/v5/market/history-candles?'+urllib.parse.urlencode(
            {'instId':instrument,'bar':timeframe,'after':str(cursor),'limit':100})
        body=fetch(url);raw=body.get('data',[])
        if not raw:
            break
        rows=[engine.Candle.parse(row) for row in raw]
        earliest=min(c.ts for c in rows)
        if earliest>=cursor:
            raise ValueError('History pagination did not move backwards')
        for c in rows:
            if c.confirmed and begin<=c.ts and c.ts+engine.TF[timeframe]<=end:
                found[c.ts]=asdict(c)
        if earliest<=begin:
            break
        cursor=earliest
        time.sleep(.12)
    else:
        raise ValueError('History download exceeded pagination limit')
    result=sorted(found.values(),key=lambda r:r['ts'])
    if not result:
        raise ValueError(f'No closed {timeframe} history for {instrument}')
    return result


def download(instrument: str, days: int, end: int, fetch=request_json) -> dict:
    if not 30<=days<=180:
        raise ValueError('Research period must be 30..180 days')
    metadata=fetch('https://www.okx.com/api/v5/public/instruments?'+urllib.parse.urlencode(
        {'instType':'SWAP' if instrument.endswith('-SWAP') else 'SPOT','instId':instrument}))
    record=next((r for r in metadata.get('data',[]) if r.get('instId')==instrument),None)
    if not record or record.get('state')!='live':
        raise ValueError('Instrument is not live on OKX')
    tick=engine.finite(record.get('tickSz',0))
    if tick<=0:
        raise ValueError('Invalid exchange tick')
    end=(end//engine.TF['15m'])*engine.TF['15m'];start=end-days*DAY
    # 60 x 4H structural lookback plus one extra day, excluded from evaluation.
    begin=start-11*DAY
    candles={tf:fetch_rows(instrument,tf,begin,end,fetch) for tf in engine.TF}
    smt=[]
    peer=core.resolve_smt_asset_id()
    if peer and peer!=instrument:
        try:
            smt=fetch_rows(peer,'15m',begin,end,fetch)
        except (RuntimeError,ValueError):
            smt=[]
    return {'instrument':instrument,'tick':tick,'start_ts':start,'end_ts':end,
            'spread_bps':2.,'report_delay_ms':180_000,'candles':candles,'smt_candles_15m':smt,
            'source':'OKX_PUBLIC_CONFIRMED_HISTORY','downloaded_at':engine.iso(int(time.time()*1000)),
            'limitations':['Uniform current tick and estimated historical spread',
                           'Reports every 15 minutes with an assumed 3-minute scheduler delay',
                           'No funding, queue position or market impact data']}


def audit_data(data: dict) -> dict:
    """Reject incomplete histories instead of measuring only convenient bars."""
    start,end=int(data['start_ts']),int(data['end_ts']);result={}
    for tf,step in engine.TF.items():
        rows=sorted({c.ts:c for row in data['candles'].get(tf,[]) for c in [engine.Candle.parse(row)]
                     if c.confirmed and c.ts+step<=end}.values(),key=lambda c:c.ts)
        warmup=[c for c in rows if c.ts+step<=start]
        if len(warmup)<engine.MIN_BARS[tf]:
            raise ValueError(f'Insufficient {tf} warmup history')
        needed=[c for c in rows if c.ts>=warmup[-engine.MIN_BARS[tf]].ts]
        if any(c.ts%step for c in needed) or any(b.ts-a.ts!=step for a,b in zip(needed,needed[1:])):
            raise ValueError(f'Incomplete or misaligned {tf} history; no result can be certified')
        if not needed or end-(needed[-1].ts+step)>=step:
            raise ValueError(f'{tf} history does not cover the evaluation end')
        result[tf]={'confirmed_bars':len(rows),'warmup_bars':len(warmup),'gaps':0}
    return result


def snapshots(data: dict):
    raw={tf:sorted({c.ts:c for row in data['candles'].get(tf,[]) for c in [engine.Candle.parse(row)] if c.confirmed}.values(),
                   key=lambda c:c.ts) for tf in engine.TF}
    if not raw['3m']:
        raise ValueError('3m history is required')
    available={tf:[c.ts+engine.TF[tf] for c in rows] for tf,rows in raw.items()}
    peer=sorted({c.ts:c for row in data.get('smt_candles_15m',[]) for c in [engine.Candle.parse(row)] if c.confirmed}.values(),key=lambda c:c.ts)
    peer_closed=[c.ts+engine.TF['15m'] for c in peer]
    delay=int(data.get('report_delay_ms',180_000))
    if not 0<=delay<engine.TF['15m']:
        raise ValueError('Report delay must be 0..<15 minutes')
    times=sorted({((c.ts+engine.TF['3m']+engine.TF['15m']-1)//engine.TF['15m'])*engine.TF['15m']+delay for c in raw['3m']})
    start=int(data['start_ts']);end=int(data['end_ts']);tick=engine.finite(data.get('tick',.001))
    for now in times:
        if not start<=now<=end:
            continue
        current={}
        for tf,rows in raw.items():
            index=bisect_right(available[tf],now)
            current[tf]=rows[max(0,index-300):index]
        if not current['3m']:
            continue
        price=current['3m'][-1].close
        spread=max(tick,price*engine.finite(data.get('spread_bps',2))/10_000)
        snap=engine.Snapshot(now,price,current,price-spread/2,price+spread/2,True,now,data['instrument'])
        index=bisect_right(peer_closed,now);snap.smt_candles=peer[max(0,index-300):index]
        yield snap


def baseline_module():
    path=ROOT/'research/baseline_v12_1.py'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=BASELINE_SHA256:
        raise ValueError('Frozen v12.1 baseline changed; restore the original research file')
    spec=importlib.util.spec_from_file_location('ict_frozen_baseline_v12_1',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def evaluate(module, cfg, samples: list, begin: int, end: int, observations=None, freeze=False) -> tuple[dict,dict]:
    state=module.new_state(cfg)
    if observations is not None:
        q=quality.ensure(state,cfg);q['observations']=copy.deepcopy(observations);q['freeze_observations']=freeze
    actions={};last=None
    for snapshot in samples:
        if not begin<=snapshot.now<=end:
            continue
        # Samples themselves are immutable; the collector/planner receives a copy
        # because older core helpers may normalize a mutable candle list.
        result=module.run_cycle(state,copy.deepcopy(snapshot),cfg)
        actions[result['action']]=actions.get(result['action'],0)+1;last=snapshot
    # Predetermined period boundary, applied identically to BOTH versions.
    # Include every filled position; do not inflate WR by dropping open losers.
    liquidated=[]
    if last:
        for number,view in module.slot_views(state):
            p=view.get('active')
            if p:
                module.close_trade(view,p,last.price*(1-p['side']*cfg.slippage_bps/10_000),last.now,'EVALUATION_END',cfg)
                module.store_slot(state,number,view);liquidated.append(p['id'])
    trades=state['trades']
    intervals=[{'id':t['id'],'decision_ts':t['placed_ts'],'closed_ts':t['closed_ts'],'net_r':t['net_r']} for t in trades]
    independent=quality.independent(intervals)
    report={'begin':engine.iso(begin),'end':engine.iso(end),'statistics':module.statistics(trades),
            'target':quality.validation_status(trades,independent),'actions':actions,
            'trades':trades,'open_at_end':module.open_plans(state),
            'filled_positions_liquidated_at_period_end':liquidated}
    return report,state


def validate(data: dict, folds: int=3) -> dict:
    if folds!=3:
        raise ValueError('This predeclared research uses three folds; no parameter sweep')
    start,end=int(data['start_ts']),int(data['end_ts'])
    if end<=start:
        raise ValueError('Invalid historical interval')
    cfg=replace(engine.Config.from_env(),core=core,instrument=data['instrument'],tick=engine.finite(data['tick']))
    cfg.validate();data_quality=audit_data(data);samples=list(snapshots(data))
    if not samples:
        raise ValueError('No evaluation snapshots')
    baseline=baseline_module();base_cfg=baseline.Config(**{k:v for k,v in vars(cfg).items() if k!='core'},core=core)
    embargo=(cfg.ttl_minutes+cfg.max_hold_minutes)*60_000+engine.TF['15m']
    middle=start+(end-start)//2
    cuts=[middle+(end-middle)*i//folds for i in range(folds+1)]
    reports=[];last_observations=[];last_training_end=0
    for index in range(folds):
        test_start,test_end=cuts[index],cuts[index+1]
        if index:
            test_start+=1  # adjacent folds cannot count the boundary twice
        train_end=test_start-embargo
        _,training=evaluate(engine,cfg,samples,start,train_end)
        rows=[r for r in training.get('quality',{}).get('observations',[]) if r['closed_ts']<train_end]
        improved,_=evaluate(engine,cfg,samples,test_start,test_end,rows,freeze=True)
        old,_=evaluate(baseline,base_cfg,samples,test_start,test_end)
        reports.append({'fold':index+1,'training_end':engine.iso(train_end),'embargo_ms':embargo,
                        'training_observations':len(rows),'baseline':old,'improved':improved})
        last_observations,last_training_end=rows,train_end
        print(f"Fold {index+1}/3 completed: {improved['statistics']['trades']} quality trades",flush=True)
    new_trades=[t for f in reports for t in f['improved']['trades']]
    old_trades=[t for f in reports for t in f['baseline']['trades']]
    independent=quality.independent([{'id':t['id'],'decision_ts':t['placed_ts'],'closed_ts':t['closed_ts'],'net_r':t['net_r']} for t in new_trades])
    evidence=quality.validation_status(new_trades,independent)
    artifact={'schema':quality.SCHEMA,'strategy':quality.STRATEGY,'signature':quality.signature(cfg),
              'trained_until_ts':last_training_end,'observations':last_observations,
              'evaluation':evidence,'settings':asdict(quality.Settings())}
    artifact['artifact_id']=quality.digest(artifact)
    return {'engine_version':engine.VERSION,'fixed_policy':quality.STRATEGY,
            'data_fingerprint':quality.digest(data),'source':data.get('source','USER_PROVIDED_OHLC'),
            'config':engine.config_dict(cfg),'data_quality':data_quality,'folds':reports,'aggregate_baseline':engine.statistics(old_trades),
            'aggregate_improved':engine.statistics(new_trades),'target_evidence':evidence,'model':artifact,
            'limitations':data.get('limitations',[])+['PAPER candle-based execution, not exchange fills',
                'One fixed research policy, no parameter grid was optimized',
                'Model frozen in each held-out fold; training labels before the embargo only',
                'Filled positions liquidated at fixed period boundaries in both versions',
                'At least 100 independent held-out trades and lower 95% bound >=70% required for target support',
                'Historical support never guarantees future results']}


def markdown(report: dict) -> str:
    e=report['target_evidence'];stats=report['aggregate_improved'];old=report['aggregate_baseline']
    pct=lambda x:'—' if x is None else f'{x*100:.2f}%'
    lines=['# Перевірка цілі 70%+','',f"Статус: **{e['status']}**.",'',
           '| Версія | Закриті угоди | Win rate | Сумарний R | Середній R |',
           '| --- | ---: | ---: | ---: | ---: |']
    for label,row in [('v12.1 — базова',old),('v12.2 — новий відбір',stats)]:
        expectation='—' if row['expectancy_r'] is None else f"{row['expectancy_r']:.3f}"
        lines.append(f"| {label} | {row['trades']} | {pct(row['win_rate'])} | {row['net_r']:.3f} | {expectation} |")
    lines += ['',f"Неперекритих завершених угод: {e['independent_trades']}.",
              f"Нижня 95% статистична межа: {pct(e['wilson_lower_95'])}.",'',
              'Ціль не вважається підтвердженою лише через кілька виграшів або відсоток у навчальній вибірці.',
              'Відхилені, невиконані та невизначені ордери не записуються як виграші.']
    lines += ['','Обмеження:']+['- '+line for line in report['limitations']]
    return '\n'.join(lines)+'\n'


def main() -> int:
    parser=argparse.ArgumentParser(description='Causal 70%+ research; no live orders or messages')
    parser.add_argument('--data',type=Path);parser.add_argument('--download',action='store_true')
    parser.add_argument('--days',type=int,default=90);parser.add_argument('--instrument',default=core.OKX_INST_ID)
    parser.add_argument('--output',type=Path,default=Path('research_output'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if args.download:
        data=download(args.instrument,args.days,int(time.time()*1000))
        engine.atomic_write(args.output/'candles.json',data)
    elif args.data:
        data=engine.read_json(args.data)
    else:
        parser.error('Provide --download or --data candles.json')
    report=validate(data)
    model=report.pop('model')
    engine.atomic_write(args.output/'validation.json',report)
    engine.atomic_write(args.output/'quality_model.json',model)
    (args.output/'validation_UA.md').write_text(markdown(report))
    print(json.dumps(report['target_evidence'],ensure_ascii=False,indent=2))
    return 0  # TARGET_NOT_CONFIRMED is a measured result, not a failed job.


if __name__=='__main__':
    raise SystemExit(main())
