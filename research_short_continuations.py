#!/usr/bin/env python3
"""Offline, as-of structural short-entry research; DOES NOT trade or validate P(win).

Replay closes in chronological order. Only candles that have fully closed before
that decision are exposed to the signal detector; later bars are used strictly
for fill/outcome labels. OHLC ambiguity (SL and TP in the same bar) is a loss.
The market archive is historical/corrected data, not a guarantee that every bar
was actually available to the GitHub scanner at that historical instant.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from entry_playbooks import bos_fvg_continuations, MICRO, STEP, HOUR, FOUR_HOURS

TF={'3m':MICRO,'15m':STEP,'1H':HOUR,'4H':FOUR_HOURS}

@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    confirmed: bool=True


def load_archive(path):
    archive=json.loads(Path(path).read_text())
    store={}
    for tf,width in TF.items():
        rows={}
        for raw in archive.get('candles',{}).get(tf,[]):
            if len(raw)<5:
                continue
            ts=int(raw[0]); o,h,l,c=map(float,raw[1:5])
            if not (0<l<=min(o,c)<=max(o,c)<=h):
                continue
            if len(raw)>8 and str(raw[8])!='1':
                continue
            rows[ts]=Bar(ts,o,h,l,c)
        store[tf]=sorted(rows.values(),key=lambda b:b.ts)
    return store


def snapshot_asof(store, now):
    # Newest fully-closed candles only. Never include the still forming bar.
    window={'3m':240,'15m':90,'1H':80,'4H':70}
    candles={}
    for tf,all_rows in store.items():
        # In the caller these arrays are time-ordered, and no information from
        # the future is exposed to the detection function.
        i=bisect_right([b.ts for b in all_rows],now-TF[tf])
        candles[tf]=all_rows[max(0,i-window[tf]):i]
    m=candles['3m']
    return SimpleNamespace(now=now,price=m[-1].close if m else 0.,candles=candles)


def label_details(signal, future, tick=.01, max_wait_bars=12, max_hold_bars=120,
                  min_sl_pct=.010, min_tp1_pct=.020, min_net_rr=1.20,
                  maker_fee=.0002, taker_fee=.0005, slippage_bps=2.):
    """Asymmetric stop-first *proxy*, with the current 1%/2% geometry.

    Structural target existence, spread, opposing zones, ranking, slot capacity,
    and exchange queue priority are NOT inferred from future candles. Therefore
    the TP is a hypothetical minimum and never presented as an actual fill.
    """
    side=signal.side; entry=signal.zone
    if entry<=0 or side*(entry-signal.invalidation)<=0:
        return {'outcome':'REJECTED_INVALID_GEOMETRY','net_r':None,'filled':False}
    # Stop beyond both the causal invalidation and the user's absolute 1% floor.
    risk=max(abs(entry-signal.invalidation)+2*tick,min_sl_pct*entry)
    stop=entry-side*risk
    # Two percent is a FLOOR, not a synthetic verified liquidity target.
    minimum=max(min_tp1_pct*entry,min_net_rr*risk+(maker_fee+taker_fee)*entry+
                slippage_bps/10000*entry)
    tp1=entry+side*minimum
    raw={'entry':round(entry,5),'stop':round(stop,5),'tp1_proxy':round(tp1,5),
         'risk_pct':round(100*risk/entry,4),'target_pct':round(100*minimum/entry,4)}
    filled=None
    for i,c in enumerate(future[:max_wait_bars]):
        if (c.low<=entry-tick if side==1 else c.high>=entry+tick):
            filled=i;break
    if filled is None:
        return {**raw,'outcome':'UNFILLED','net_r':None,'filled':False}
    exit_index=filled+max_hold_bars
    outcome='TIMEOUT'
    for j,c in enumerate(future[filled:filled+max_hold_bars]):
        if (c.low<=stop if side==1 else c.high>=stop):
            outcome='STOP'; exit_index=filled+j;break
        # No TP credit on a potentially ambiguous fill candle.
        if j>0 and (c.high>=tp1 if side==1 else c.low<=tp1):
            outcome='TP_PROXY'; exit_index=filled+j;break
    if outcome=='TIMEOUT':
        # Unknown final price when candles end: do NOT manufacture a result.
        if exit_index>=len(future):
            return {**raw,'outcome':'UNRESOLVED','net_r':None,'filled':True}
        mark=future[min(exit_index,len(future)-1)].close
    else:
        mark=stop if outcome=='STOP' else tp1
    gross_r=side*(mark-entry)/risk
    expenses=(maker_fee*entry+taker_fee*mark+slippage_bps/10000*mark)/risk
    return {**raw,'outcome':outcome,'net_r':round(gross_r-expenses,4),
            'gross_r':round(gross_r,4),'filled':True,'fill_bar':filled,
            'exit_bar':exit_index}


def label(signal, future, tick=.01, max_wait_bars=12, max_hold_bars=120):
    """Backwards-compatible outcome label, using the 1%/2% study geometry."""
    return label_details(signal,future,tick,max_wait_bars,max_hold_bars)['outcome']


def evaluate(path):
    store=load_archive(path)
    times=[c.ts+MICRO for c in store['3m']]
    events=[]; seen=set()
    # Avoid O(n**2) as-of search by indexing once; only replay decisions.
    stamps={tf:[x.ts for x in bars] for tf,bars in store.items()}
    windows={'3m':240,'15m':90,'1H':80,'4H':70}
    for i,now in enumerate(times):
        if i<80:continue
        candles={}
        for tf,bars in store.items():
            j=bisect_right(stamps[tf],now-TF[tf]);candles[tf]=bars[max(0,j-windows[tf]):j]
        snap=SimpleNamespace(now=now,candles=candles,price=candles['3m'][-1].close)
        # Enforce closed, CONTIGUOUS last 3m observations, as in live.
        events_now=[e for e in bos_fvg_continuations(snap,.01) if e.side==-1 and e.signal_ts==now]
        for e in events_now:
            event=(e.signal_ts,e.event_ts,round(e.zone,4))
            if event in seen:continue
            seen.add(event)
            metrics=label_details(e,store['3m'][i+1:])
            outcome=metrics['outcome']
            events.append({'ts':now,'utc':datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),
                           'entry':round(e.zone,4),'stop_reference':round(e.invalidation,4),
                           'signal':e.family,'outcome_proxy':outcome,
                           'study_geometry':metrics})
    # Calendar-separated blocks: not a randomly mixed train/test.
    all_days=sorted({e['utc'][:10] for e in events})
    period_days=sorted({datetime.fromtimestamp(ts/1000,timezone.utc).date().isoformat() for ts in times})
    cut1=period_days[len(period_days)//3] if period_days else ''
    cut2=period_days[(2*len(period_days))//3] if period_days else ''
    periods=defaultdict(lambda:defaultdict(int))
    for event in events:
        day=event['utc'][:10]
        fold='early' if day<cut1 else 'middle' if day<cut2 else 'late_holdout'
        periods[fold][event['outcome_proxy']]+=1
    # A paper diagnostic has neither certified structural TP nor order queue.
    # Its R values are not evidence that the complete STRICT strategy trades.
    return {'source':str(path),'risk_constraints':{'minimum_sl_pct':1.0,'minimum_tp1_pct':2.0,
            'min_net_rr':1.2,'maker_fee':0.0002,'taker_fee':0.0005,'slippage_bps':2.},
            'data_range_utc':[period_days[0] if period_days else None,period_days[-1] if period_days else None],
            'fold_boundaries':[cut1,cut2],'method':'closed as-of BOS/FVG event; prospective 1%/2% proxy, net costs, no structural-target confirmation, NOT live or full-engine PNL',
            'events_count':len(events),'periods':{k:dict(v) for k,v in periods.items()},
            'net_r_by_fold':{fold:{'resolved_count':len([e for e in events if ('early' if e['utc'][:10]<cut1 else 'middle' if e['utc'][:10]<cut2 else 'late_holdout')==fold and e['study_geometry']['net_r'] is not None]),
                                  'total_net_r_proxy':round(sum(e['study_geometry']['net_r'] or 0 for e in events if ('early' if e['utc'][:10]<cut1 else 'middle' if e['utc'][:10]<cut2 else 'late_holdout')==fold),4)}
                             for fold in ('early','middle','late_holdout')},
            'events':events}


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--archive',type=Path,default=Path(__file__).parent/'research_market_v12_6.json')
    ap.add_argument('--output',type=Path)
    args=ap.parse_args()
    result=evaluate(args.archive)
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(output,encoding='utf-8')
    else:print(output)
