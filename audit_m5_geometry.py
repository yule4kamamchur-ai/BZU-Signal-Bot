#!/usr/bin/env python3
"""Paired static geometry study on recorded MARKET entries only.
Independent overlapping trades, not a portfolio; 18h fixed horizon, stop-first.
Use recorded entry and timestamp. Keep same observations, per-trade fixed gross
risk R; costs 0.03% on each side. No trailing/TP1. No optimization or deployment.
"""
import argparse,csv,json,statistics
from datetime import datetime,timezone
from collections import Counter
from pathlib import Path
from bisect import bisect_left
import mexc_h1_trend as tr


def audit(candles,trades):
    with open(candles,newline='') as f: bars=[{'t':int(r['timestamp_ms'])//1000,'o':float(r['open']),'h':float(r['high']),'l':float(r['low']),'c':float(r['close'])} for r in csv.DictReader(f)]
    ts=[b['t'] for b in bars]
    if not all(tr.valid_bar(b) for b in bars) or any(y-x!=60 for x,y in zip(ts,ts[1:])): raise ValueError('Source has missing/invalid minutes')
    with open(trades,newline='') as f: source=list(csv.DictReader(f))
    sample=[]; horizon=18*60
    for r in source:
        if r['order_type']!='MARKET': continue
        t=int(datetime.strptime(r['opened_utc'],'%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp()); i=bisect_left(ts,t)
        if i>=len(ts) or ts[i]!=t or i+horizon>len(ts): continue
        sample.append((r,i))
    models={}; outputs={}
    for label,sm,tm in [('BASE_STATIC',1,1),('STOP_150_SAME_TP',1.5,1),('STOP_200_SAME_TP',2,1),('STOP_TP_150',1.5,1.5),('STOP_TP_200',2,2),('SAME_STOP_TP_150',1,1.5),('SAME_STOP_TP_200',1,2)]:
        out=[]
        for r,i in sample:
            e=float(r['entry']); s=int(r['side_num']); d=abs(e-float(r['initial_stop']))*sm; tp=e+s*abs(float(r['tp'])-e)*tm; stop=e-s*d
            # Stop and TP are rounded away from entry / toward entry respectively.
            stop=tr.tickround(stop,.01,-s); tp=tr.tickround(tp,.01,-s); d=abs(e-stop)
            px=None; reason='TIME_EXIT'; end=None
            for b in bars[i:i+horizon]:
                end=b['t']+60
                if (b['l']<=stop if s==1 else b['h']>=stop): px=min(b['o'],stop) if s==1 else max(b['o'],stop); reason='STOP'; break
                if (b['h']>=tp if s==1 else b['l']<=tp): px=tp; reason='TP'; break
            if px is None: px=bars[i+horizon-1]['c']
            net=(s*(px-e)-.0003*(e+px))/d
            out.append({'trade_num':r['trade_num'],'family':r['family'],'side':r['side'],'entry':e,'stop':stop,'tp':tp,'exit':px,'reason':reason,'net_r':net,'closed_ts':end*1000})
        wins=[x['net_r'] for x in out if x['net_r']>0]; losses=[x['net_r'] for x in out if x['net_r']<0]
        models[label]={'n':len(out),'wr':len(wins)/len(out),'pf':sum(wins)/abs(sum(losses)) if losses else None,'net_r':sum(x['net_r'] for x in out),'mean_r':statistics.mean(x['net_r'] for x in out),'stops':sum(x['reason']=='STOP' for x in out),'tp':sum(x['reason']=='TP' for x in out),'timeout':sum(x['reason']=='TIME_EXIT' for x in out)}
        outputs[label]=out
    base=outputs['BASE_STATIC']; wide=outputs['STOP_150_SAME_TP']; stats={'source_n':len(source),'market_n':sum(x['order_type']=='MARKET' for x in source),'paired_n':len(sample),'median_original_stop_pct':statistics.median(abs(float(x['entry'])-float(x['initial_stop']))/float(x['entry'])*100 for x in source),'median_original_rr':statistics.median(abs(float(x['tp'])-float(x['entry']))/abs(float(x['initial_stop'])-float(x['entry'])) for x in source),'mean_original_cost_r':statistics.mean(float(x['cost_r']) for x in source),'original_exit_counts':dict(Counter(x['exit_reason'] for x in source)),'base_stops_saved_to_target_by_wide_150':sum(a['reason']=='STOP' and b['reason']=='TP' for a,b in zip(base,wide)),'models':models,'limitations':'Fixed geometry, same recorded entries, overlapping trades, no trailing, no funding, stop-first, 18h horizon; not portfolio PnL or independent test'}
    return stats,outputs

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('candles');ap.add_argument('trades');ap.add_argument('--out',type=Path,default=Path('m5_geometry_audit.json')); a=ap.parse_args(); z,rows=audit(a.candles,a.trades);a.out.write_text(json.dumps({'summary':z,'paired_trades':rows},indent=2));print(json.dumps(z,indent=2))
