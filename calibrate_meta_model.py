#!/usr/bin/env python3
"""Fit an offline, walk-forward validated execution prior without exchange orders."""
from __future__ import annotations

import argparse
import os
import time
import urllib.parse
from collections import Counter
from dataclasses import replace
from pathlib import Path

import bot_oneshot as core
import full_limit_engine as engine
import research_validate as research
import shadow_ledger as shadow
import signal_quality as quality
import trade_meta_model as model
import expectancy_model as ev
import adaptive_entry

INSTRUMENT = 'BZ-USDT-SWAP'
WARMUP = 11 * shadow.DAY


def download_history(days, end, scan_minutes, fetch=research.request_json):
    """Obtain native, closed OKX candles with an instrument-aware start."""
    query = urllib.parse.urlencode({'instType':'SWAP','instId':INSTRUMENT})
    instruments = fetch('https://www.okx.com/api/v5/public/instruments?' + query)['data']
    spec = next((row for row in instruments if row['instId'] == INSTRUMENT),None)
    if spec is None:
        raise ValueError(f'{INSTRUMENT} missing from the public instrument catalog')
    listing = int(spec.get('listTime') or 0)
    tick = engine.finite(spec['tickSz'])
    if tick <= 0:
        raise ValueError('Invalid OKX tick size')
    hour4 = engine.TF['4H']
    end = end // hour4 * hour4
    start = max(end-days*shadow.DAY, (listing+WARMUP+hour4-1)//hour4*hour4)
    if start >= end:
        raise ValueError('Not enough history after the warm-up interval')
    candles = {}
    for tf in engine.CANDLE_TF:
        candles[tf] = research.fetch_rows(INSTRUMENT,tf,start-WARMUP,end,fetch,page_size=300)
        print(f'OKX {tf}: {len(candles[tf])} completed bars',flush=True)
    return {'instrument':INSTRUMENT,'tick':tick,'start_ts':start,'end_ts':end,
            'candles':candles,'source':'OKX_PUBLIC_CONFIRMED_HISTORY',
            'scan_interval_ms':scan_minutes*shadow.MINUTE,'report_delay_ms':0,
            'price_from_1m':True,'spread_bps':2.,
            'limitations':['Approximate bid/ask from M1 closes and a fixed 2 bp spread',
                           'No historical order-book depth, fees from the configured contract, no funding',
                           'Bar-level fills and OHLC excursions are modelled']}


def archive_history(path, days, scan_minutes, end=None):
    data = research.archive_dataset(engine.read_json(path))
    minute = data['candles'].get('1m', [])
    if not minute:
        raise ValueError('M1 evidence is required for execution labels')
    first_minute = engine.Candle.parse(minute[0]).ts
    data['end_ts'] = min(data['end_ts'],end) if end is not None else data['end_ts']
    data['start_ts'] = max(data['start_ts'],first_minute+60*shadow.MINUTE,
                           data['end_ts']-days*shadow.DAY)
    data['scan_interval_ms'] = scan_minutes * shadow.MINUTE
    if data['start_ts'] >= data['end_ts']:
        raise ValueError('No common M1/quote interval in the requested archive range')
    return data


def run(data, cfg, *, progress=100):
    if data['instrument'] != INSTRUMENT or cfg.instrument != INSTRUMENT:
        raise ValueError(f'Expected {INSTRUMENT} in the input and configuration')
    if not data.get('quotes'):
        research.audit_data(data)
    state = engine.new_state(cfg)
    state['calibration_replay'] = True
    actions = Counter()
    rejections = Counter()
    shadow_routes = Counter()
    scans = 0
    censored = 0
    for snapshot in research.snapshots(data):
        snapshot.metadata['require_1m'] = True
        result = engine.run_cycle(state,snapshot,cfg)
        actions[result['action']] += 1
        rejections.update(result['analysis'].get('rejections',{}))
        shadow_routes.update(p.get('timing_route','UNKNOWN') for p in result['analysis'].get('shadow_timing_plans',[]))
        faults = result['analysis'].get('health',())
        if ('LIFECYCLE_HISTORY_GAP' in faults and
                snapshot.now-state['last_run_ts'] >= 2*cfg.analysis_scan_minutes*shadow.MINUTE):
            # A lasting gap cannot certify an open portfolio trade. Keep the
            # finished labels and resume risk-free from the next observed scan.
            shadow.advance(state,snapshot,cfg,engine)
            censored += len(engine.open_plans(state))
            engine.park_pending(state,[])
            for number, view in engine.slot_views(state):
                view.update(active=None,reconciliation=None)
                engine.store_slot(state,number,view)
        scans += 1
        if progress and scans % progress == 0:
            print(f'Replay {scans} scans through {engine.iso(snapshot.now)}',flush=True)
    if not scans:
        raise ValueError('The selected history contains no executable scan snapshots')
    ledger = shadow.ensure(state)
    end = data['end_ts']
    # Prefer a portfolio outcome when the same episode was actually selected
    # by the replay. Unfilled, incomplete or open episodes carry no label.
    labels = ev.labels([*ledger['outcomes'],*state['trades']],before=end+1)
    validation = ev.walk_forward(labels,data['start_ts'],end)
    route_validation={route:ev.walk_forward(labels,data['start_ts'],end,route=route) for route in adaptive_entry.ROUTES}
    route_statistics={}
    for route in adaptive_entry.ROUTES:
        attempts=[r for r in labels if r['expectancy_context'][-1]==route]
        filled=[r for r in attempts if r['is_filled']]
        route_statistics[route]={'resolved_attempts':len(attempts),'filled':len(filled),
            'shadow_alternatives':sum(bool(r.get('shadow_timing_only')) for r in attempts),
            'statistics':engine.statistics(filled), 'independent_attempts':len(list(ev.independent(attempts)))}
    groups={}
    for r in labels:
        if r.get('timing_episode_id'):
            groups.setdefault(r['timing_episode_id'],[]).append(r)
    matched_groups=[g for g in groups.values() if len({r['expectancy_context'][-1] for r in g})>1]
    paired={}
    from itertools import combinations
    for group in matched_groups:
        by_route={r['expectancy_context'][-1]:r for r in group}
        for a,b in combinations(sorted(by_route),2):
            # Nonfills have zero realized R, censored/unresolved rows are excluded by labels().
            ra,rb=(by_route[k]['net_r'] if by_route[k]['is_filled'] else 0. for k in (a,b))
            v=paired.setdefault(a+' vs '+b,{'episodes':0,'net_r_first':0.,'net_r_second':0.})
            v['episodes']+=1; v['net_r_first']+=ra; v['net_r_second']+=rb
    for v in paired.values():
        v['mean_delta_first_minus_second_r']=(v['net_r_first']-v['net_r_second'])/v['episodes']
    artifact = {'schema':ev.SCHEMA,'label_schema':ev.LABEL_SCHEMA,
                'engine_version':engine.VERSION,'instrument':cfg.instrument,
                'signature':quality.calibration_signature(cfg),'data_start_ts':data['start_ts'],
                'data_end_ts':end,'source':data['source'],'prior_strength':ev.SHRINK,
                'daily_decay':model.DAILY_DECAY,'validation':validation,
                'counts':ev.counts(labels),'route_validation':route_validation}
    report = {'observed_range':[engine.iso(data['start_ts']),engine.iso(end)],
              'source':data['source'],'scan_count':scans,'actions':dict(actions),
              'training_labels':len(labels),'independent_labels':len(list(ev.independent(labels))),
              'closed_portfolio':len(state['trades']),
              'portfolio_statistics':engine.statistics(state['trades']),
              'risk_weighted_statistics':engine.statistics([{**r,'weighted':r['net_r']*r.get('risk_multiplier',1.)} for r in state['trades']],key='weighted'),
              'placed_orders':sum(e['kind']=='PLACED' for e in state['events']),
              'portfolio_open_at_end':len(engine.active_plans(state)),
              'portfolio_pending_at_end':len(engine.pending_plans(state)),
              'portfolio_censored_by_data_gap':censored,
              'shadow_realized':sum(r.get('net_r') is not None for r in ledger['outcomes']),
              'shadow_open_or_pending':len(ledger['pending']),
              'shadow_outcomes':dict(Counter(r['close_reason'] for r in ledger['outcomes'])),
              'data_bars':{tf:len(rows) for tf,rows in data['candles'].items()},
              'limitations':data.get('limitations', []),'validation':validation,
              'route_validation':route_validation,'route_statistics':route_statistics,
              'matched_route_episodes':len(matched_groups),'matched_route_comparison':paired,
              'candidate_rejections':dict(rejections),'shadow_route_candidates':dict(shadow_routes)}
    return artifact, report, labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,help='Use the repository’s captured candle and quote archive')
    parser.add_argument('--days',type=int,default=180,help='Requested history, up to 365 days')
    parser.add_argument('--scan-minutes',type=int,choices=(3,5,15),default=5)
    parser.add_argument('--end-ms',type=int,help='End timestamp in Unix milliseconds')
    parser.add_argument('--adaptive-exits', choices=('SHADOW','ACTIVE'), default='SHADOW')
    parser.add_argument('--output',type=Path,default=Path('calibration/meta_priors.json'))
    parser.add_argument('--report',type=Path,default=Path('calibration/report.json'))
    parser.add_argument('--labels',type=Path,help='Optional completed replay labels for inspection')
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error('--days must be 1..365')
    os.environ.update(BZU_ENTRY_MODE='HYBRID',BZU_EDGE_MODE='SHADOW',BZU_ENTRY_PLAYBOOK_MODE='STRICT',
                      BZU_ADAPTIVE_EXITS=args.adaptive_exits,BZU_OKX_EXECUTION='PAPER')
    end = args.end_ms or int(time.time()*1000)
    data = (archive_history(args.archive,args.days,args.scan_minutes,args.end_ms) if args.archive
            else download_history(args.days,end,args.scan_minutes))
    cfg = replace(engine.Config.from_env(),core=core,instrument=data['instrument'],tick=data['tick'],
                  analysis_scan_minutes=args.scan_minutes)
    artifact, report, labels = run(data,cfg)
    for path, content in ((args.output,artifact),(args.report,report),(args.labels,{'labels':labels})):
        if path is not None:
            path.parent.mkdir(parents=True,exist_ok=True)
            engine.atomic_write(path,content)
    print(f"{report['scan_count']} scans, {len(labels)} completed labels: {artifact['validation']['status']}",
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
