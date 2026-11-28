#!/usr/bin/env python3
"""Descriptive H1 failure/phase and correlation audit on exported M1 history.
Not a gate or a learner; post-entry failure labels never feed live scores.
"""
from __future__ import annotations
import argparse,bisect,json,math
from pathlib import Path
from itertools import combinations
from statistics import median,mean
from collections import defaultdict
import mexc_multi_scanner as sc
import mexc_h1_trend as h
import mexc_portfolio_intelligence as intel
import mexc_trend_research as tr
from mexc_replay_v154 import read_bars


def audit(path,report):
    window=report['coverage_window'];start=window['effective_start_ts'];end=window['effective_end_ts']
    symbols=list(report['coverage']);symbols+=['BTC_USDT'] if (path/'BTC_USDT.csv').exists() and 'BTC_USDT' not in symbols else []
    series={};index={};m1={};mi={}
    for sym in symbols:
        raw=[b for b in read_bars(sc,path/(sym+'.csv')) if b.ts<end];m1[sym]=raw;mi[sym]=[b.ts for b in raw]
        series[sym]=h.pack(sc.aggregate_bars(raw,60,3600,end));index[sym]=[b['t']*1000 for b in series[sym]]
    def closed(sym,ts,n=145):
        j=bisect.bisect_right(index[sym],ts-h.HOUR);return series[sym][max(0,j-n):j]
    matrix={}
    for a,b in combinations(symbols,2):
        vals=[]
        for ts in range(start,end,h.HOUR):
            v=intel.correlation(closed(a,ts,49),closed(b,ts,49))
            if v is not None:vals.append(v)
        matrix[f'{a}|{b}']={'windows':len(vals),'median_rho':median(vals) if vals else None,'min_rho':min(vals) if vals else None,'max_rho':max(vals) if vals else None}
    examples=[]
    for p in report['closed_trades_retained']:
        if p.get('strategy','TREND_H1')!='TREND_H1' or p.get('entry_route')=='EARLY_M5_H1_CHANNEL':continue
        sym=p['symbol'];now=p['opened_ts'];q=closed(sym,now);n=p.get('channel_n',30);sig=h.signal(q,now,n=n,prior_atr=p.get('trail_anchor')=='CLOSE')
        if not sig or sig['side']!=p['side'] or sig['signal_ts']!=p['signal_ts']:continue
        j=bisect.bisect_left(mi[sym],now);prev=h.pack(m1[sym][max(0,j-300):j]);f=tr.breakout_features(q,sig,now,prev)
        # A return inside the original channel within 60 minutes is a DESCRIPTIVE label,
        # not necessarily a stop-loss or a failed trade. Require full observed horizon.
        after=m1[sym][j:j+60];complete=len(after)==60 and after[0].ts==now and after[-1].ts+h.MINUTE<=end and all(b.ts-a.ts==h.MINUTE for a,b in zip(after,after[1:]))
        level=sig['upper'] if p['side']==1 else sig['lower']
        back=any(p['side']*(b.close-level)<=0 for b in after) if complete else None
        examples.append({'symbol':sym,'side':p['side'],'opened_ts':now,'channel_n':n,'net_r':p['net_r'],'net_usd':p['net_usd'],'exit_reason':p['exit_reason'],'returned_inside_60m':back,**f})
    def stats(rows):
        labels=[r['returned_inside_60m'] for r in rows if r['returned_inside_60m'] is not None];n=len(rows)
        return {'n':n,'net_usd':sum(r['net_usd'] for r in rows),'mean_net_r':mean(r['net_r'] for r in rows) if n else None,'initial_stops':sum(r['exit_reason']=='INITIAL_STOP' for r in rows),'observed_60m':len(labels),'back_inside_rate':sum(labels)/len(labels) if labels else None}
    groups=defaultdict(list)
    for r in examples:
        groups[r['trend_phase']].append(r);groups['HA_SUPPORT' if r['ha_support']>0 else 'HA_NEUTRAL_OR_OPPOSED'].append(r)
        groups['LOW_QUALITY' if r['quality_v160']<.5 else 'HIGH_QUALITY'].append(r)
        groups['OIL' if r['symbol']=='UKOIL_USDT' else 'OTHER'].append(r)
    return {'schema':'TREND_DIAGNOSTICS_160','profile':report['profile'],'start_ts':start,'end_exclusive_ts':end,'rolling_correlation_48h':matrix,'by_feature_group':{k:stats(v) for k,v in groups.items()},'examples':examples,'notes':'Descriptive, reused development period; 48 synchronized H1 returns, min 16; return inside channel within next 60m is post-entry label only. Features use closed candles at decision; no automatic promotion.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();z=audit(a.data,json.loads(a.report.read_text()));a.out.write_text(json.dumps(z,indent=2));print(json.dumps(z['by_feature_group'],indent=2))
if __name__=='__main__':main()
