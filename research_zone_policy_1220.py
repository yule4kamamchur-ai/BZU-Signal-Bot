#!/usr/bin/env python3
"""As-of strong-zone comparison; research ONLY, never relaxes production gate.

Candidate SHORT BOS/FVG events are built without future candles. At detection we
query the actual production zone-lifecycle code using an as-of snapshot. Only
AFTER that decision do we attach the conservative 1%/2% future proxy result.
Results are not full ICT fills, and a rejected signal is NOT an executable trade.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict,Counter
from dataclasses import replace
from pathlib import Path

import bot_oneshot as core
import full_limit_engine as engine
from research_short_continuations import evaluate,load_archive,snapshot_asof


def study(path):
    history=evaluate(path)
    store=load_archive(path)
    cfg=replace(engine.Config(tick=.01,include_commission=True),core=core)
    report=defaultdict(lambda: defaultdict(lambda: {'count':0,'filled':0,'resolved':0,'net_r_proxy':0.,'outcomes':Counter()}))
    rows=[]
    for event in history['events']:
        now=event['ts']
        snap=snapshot_asof(store,now)
        candles={tf:[engine.Candle(c.ts,c.open,c.high,c.low,c.close,1.,True)
                     for c in series] for tf,series in snap.candles.items()}
        quote=engine.Snapshot(now,snap.price,candles,snap.price-.01,snap.price+.01,
                              True,now,'BZ-USDT-SWAP')
        ctx=engine.build_full_context(quote,engine.new_state(cfg),cfg)
        targets=engine.full_targets(ctx,quote,'SHORT',event['entry'],cfg)
        required=max(cfg.min_target_pct*event['entry'],
                     event['entry']-event['study_geometry']['stop'])
        barriers=[x for x in targets if (x.get('hard_barrier') and
                  (x.get('entry_inside_zone') or x['distance']<=required+2*cfg.tick))]
        # Baseline: a strong unbroken zone before min TP blocks entry.
        # Counterfactual: ignore barrier, NOT a deployable rule.
        gate='BLOCK_STRONG_ZONE' if barriers else 'NO_NEAR_STRONG_ZONE'
        date=event['utc'][:10]
        boundaries=history['fold_boundaries']
        fold='early' if date<boundaries[0] else 'middle' if date<boundaries[1] else 'late_holdout'
        metrics=event['study_geometry']
        group=report[fold][gate]
        group['count']+=1
        group['filled']+=int(metrics['filled'])
        group['outcomes'][metrics['outcome']]+=1
        if metrics['net_r'] is not None:
            group['resolved']+=1
            group['net_r_proxy']+=metrics['net_r']
        rows.append({'ts':now,'utc':event['utc'],'fold':fold,'gate':gate,
                     'barrier_count':len(barriers),
                     'nearest_barrier':(round(min(x['distance'] for x in barriers),5) if barriers else None),
                     'asof_barrier_known_ts':([x['known_ts'] for x in barriers[:3]]),
                     'counterfactual_unprotected_outcome':metrics['outcome'],
                     'counterfactual_unprotected_net_r':metrics['net_r']})
    return {'source':str(path),'type':'RETROSPECTIVE_CAUSAL_POLICY_DIAGNOSTIC_ONLY',
            'method':'same confirmed as-of events, production structural zones, minimum 1% stop 2% TP proxy; late labels never used by early gate',
            'warnings':['NOT a full STRICT/replay execution or exchange fill',
                        'Unknown fills, two-slot collisions, real order book and true liquidity targets not simulated',
                        'Ignoring a strong unbroken zone is counterfactual and NOT enabled in production'],
            'comparison':{f:{k:{**{z:v for z,v in group.items() if z!='outcomes'},
                           'net_r_proxy':round(group['net_r_proxy'],4),'outcomes':dict(group['outcomes'])}
                        for k,group in gates.items()} for f,gates in report.items()},
            'events_count':len(rows),'events':rows}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path,default=Path(__file__).parent/'research_market_v12_6.json')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    content=json.dumps(study(args.archive),indent=2,ensure_ascii=False)
    if args.output:args.output.write_text(content,encoding='utf-8')
    else:print(content)
