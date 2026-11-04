"""Read-only first-seen 15m replay; legacy ROOT OUTPUT CLI remains supported."""
from __future__ import annotations
import argparse
import collections
import contextlib
import hashlib
import io
import json
import time
from dataclasses import replace
from pathlib import Path

import bot_oneshot as core
import full_limit_engine as engine
import market_archive
import research_validate
import trade_dependencies


def replay(root, archive, progress=True):
    raw=json.loads(archive.read_text())
    source_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
    contamination=market_archive.known_test_contamination(raw)
    if contamination['known_test_quotes']:
        return {'status':'BLOCKED_CONTAMINATED_ARCHIVE','archive':archive.name,
                'archive_sha256':source_hash,'contamination':contamination,
                'profitability_validated':False,'production_state_written':False,
                'reason':'Known synthetic test quotes. No fabricated cleaning or profitability result.'}
    data=research_validate.archive_dataset(raw)
    cfg=replace(engine.Config.from_env(),core=core,tick=data['tick'],analysis_scan_minutes=15)
    state=engine.new_state(cfg);rows=[];started=time.monotonic();last_bucket=None
    for snap in research_validate.snapshots(data):
        bucket=snap.now//900_000
        if bucket==last_bucket: continue
        last_bucket=bucket
        with contextlib.redirect_stdout(io.StringIO()):
            result=engine.run_cycle(state,snap,cfg)
        analysis=result.get('analysis',{})
        rows.append({'ts':snap.now,'price':snap.price,'action':result['action'],
            'health':analysis.get('health',[]),'precision':engine.execution_series(snap)[0],
            'placed':[{'id':p['id'],'side':p['side'],'entry':p['entry'],'score':p.get('score'),
                'setup':p['setup'],'policy':p.get('policy'),'tf':p.get('setup_timeframe')}
                for p in result.get('selected_plans',[])],
            'admission':analysis.get('admission_by_side',{}),'funnel':analysis.get('funnel',{}),
            'rejections':analysis.get('rejections',{}),
            'session_gate':analysis.get('session_gate',{})})
        if progress and len(rows)%25==0:
            print(f"{len(rows)} scans; {engine.iso(snap.now)}; trades {len(state['trades'])}",flush=True)
    counts=collections.Counter(reason for row in rows for reason in row['health'])
    minute=all(row['precision']=='1m' for row in rows) if rows else False
    rejections=collections.Counter()
    for row in rows: rejections.update(row['rejections'])
    return {'status':'EXPLORATORY_REPLAY_COMPLETED','version':engine.VERSION,
        'archive':archive.name,'archive_sha256':source_hash,'production_state_written':False,
        'mode':'FIRST_SEEN_15M_OBSERVED_QUOTES_NO_HOLDOUT','profitability_validated':False,
        'minute_execution_validated':minute,'execution_timeframe':'1m' if minute else '3m_COARSE_FALLBACK',
        'limitations':['Same historical window used for diagnosis; not independent holdout.',
            'No 1m reconstruction: archives without minutes cannot validate minute ordering.',
            'OHLC stop-first on ambiguous bars; no actual exchange fill verification.',
            'Signals are observed at real quotes, never filled before publication.'],
        'configuration':engine.config_dict(cfg),'observations':len(rows),
        'first':engine.iso(rows[0]['ts']) if rows else None,
        'last':engine.iso(rows[-1]['ts']) if rows else None,
        'statistics':engine.statistics(state['trades']),
        'dependencies':trade_dependencies.summary(state['trades'],cfg.tick),
        'health_counts':dict(counts),
        'candidate_rejections':dict(rejections),
        'by_side':{str(side):engine.statistics([t for t in state['trades'] if t['side']==side]) for side in (-1,1)},
        'by_timeframe':{tf:engine.statistics([t for t in state['trades'] if t.get('setup_timeframe')==tf]) for tf in ('15m','1H','4H')},
        'orders':[{k:p.get(k) for k in ('id','side','entry','setup','placed_ts','resolved_ts','status','reason')} for p in state['orders']],
        'trades':[{k:p.get(k) for k in ('id','side','entry','setup','filled_ts','closed_ts','net_r','tp1_hit','close_reason','ambiguous_ohlc')} for p in state['trades']],
        'open_at_end':[{k:p.get(k) for k in ('id','side','entry','status')} for p in engine.open_plans(state)],
        'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')},
        'rows':rows,'elapsed_seconds':time.monotonic()-started}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('root',nargs='?',type=Path,default=Path(__file__).parent)
    parser.add_argument('output',nargs='?',type=Path)
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--baseline',type=Path)
    args=parser.parse_args()
    archive=args.archive or args.root/'market_history_v12.json'
    result=replay(args.root,archive)
    if args.baseline:
        baseline=json.loads(args.baseline.read_text())
        comparable=baseline.get('archive_sha256')==result.get('archive_sha256')
        result['baseline_comparison']={'same_archive':comparable,
            'baseline_version':baseline.get('version'),'before':baseline.get('statistics'),
            'after':result.get('statistics'),'independent_validation':False,
            'baseline_sha256':hashlib.sha256(args.baseline.read_bytes()).hexdigest()}
    if args.output:
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','orders','trades','code_sha256','dependencies')},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
