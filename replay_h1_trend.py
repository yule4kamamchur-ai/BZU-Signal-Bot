#!/usr/bin/env python3
"""Compare predeclared H1 variants on real M1 OHLC. No future candles.
Input: CSV timestamp_ms,open,high,low,close. First 5 calendar days warmup.
No funding/order book; constant cost per side. Last position closed at end.
Five variants fix $3 estimated stop-risk and $100 reference; sixth models
single ENERGY cluster of the live PAPER track ($1 risk cap, declining equity).
"""
import argparse,csv,json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import mexc_h1_trend as tr


def load(path):
    with open(path,newline='') as f:
        rows=[{'t':int(r['timestamp_ms'])//1000,'o':float(r['open']),'h':float(r['high']),'l':float(r['low']),'c':float(r['close'])} for r in csv.DictReader(f)]
    if not rows or any(not tr.valid_bar(x) for x in rows) or any(b['t']-a['t']!=60 for a,b in zip(rows,rows[1:])):
        raise ValueError('M1 source must be valid, sorted, unique and continuous; do not fill gaps')
    buckets=defaultdict(list)
    for b in rows: buckets[b['t']//3600*3600].append(b)
    hours=[{'t':t,'o':g[0]['o'],'h':max(x['h'] for x in g),'l':min(x['l'] for x in g),'c':g[-1]['c']} for t,g in sorted(buckets.items()) if len(g)==60]
    sigs={}; atrs={}
    for i,h in enumerate(hours):
        t=h['t']*1000+tr.HOUR; prefix=hours[max(0,i-100):i+1]
        a=tr.atr(prefix)
        if a: atrs[t]=a
        sig=tr.signal(prefix,t)
        if sig and sig['side']: sigs[t]=sig
    return rows,sigs,atrs


def run(rows,sigs,atrs,*,stop,trail,tp,leverage,cluster_budget=False,fee=.0003,warm_days=5,tick=.01):
    start=rows[0]['t']*1000+warm_days*86400000; end=(rows[-1]['t']+60)*1000
    hist=[]; p=None; realised=0.; high=0.; dd=0.; peak_closed=0.; dd_closed=0.; accepted=0; rejected=0; used_leverage=[]
    def mark(px):
        nonlocal high,dd
        value=realised+(tr.pnl(p,px) if p else 0.)
        high=max(high,value); dd=max(dd,high-value)
    def close(px,t,reason):
        nonlocal p,realised,high,dd,peak_closed,dd_closed
        x=tr.finish(p,px,t,reason); hist.append(x); realised+=x['net_usd']; p=None
        peak_closed=max(peak_closed,realised); dd_closed=max(dd_closed,peak_closed-realised)
        high=max(high,realised); dd=max(dd,high-realised)
    for b in rows:
        t=b['t']*1000
        if t<start: continue
        # Enter at the NEXT M1 open after the H1 close; never fill at an earlier price.
        if p is None and t in sigs:
            cap=max(0.,min(100.,100.+realised)) if cluster_budget else 100.
            p,reason=tr.open_position('UKOIL_USDT','ENERGY',sigs[t],b['o'],t,tick=tick,risk_budget=cap*(.01 if cluster_budget else .03),notional_budget=cap*leverage,fee=fee,slip=0.,stop_mult=stop,trail_mult=trail,tp_r=tp)
            if p: accepted+=1; used_leverage.append(p['notional']/100.)
            else: rejected+=1
        if p:
            mark(b['o']); hit,worst,last=tr.advance_bar(p,b,atrs.get(t+tr.MINUTE))
            if worst is not None: mark(worst)
            if hit:
                if hit[2] not in ('INITIAL_STOP','ATR_TRAIL'):
                    # Favorable extreme exists before H1 close; fixed target caps the mark.
                    mark(hit[0] if hit[2]=='FIXED_TP' else b['h'] if p['side']==1 else b['l'])
                close(hit[0],hit[1],hit[2])
            else:
                mark(b['h'] if p['side']==1 else b['l']); mark(b['c'])
    if p: close(rows[-1]['c'],end,'END_OF_SAMPLE')
    z=tr.stats(hist); z.update(mdd_m1_adverse_first_usdt=dd,mdd_closed_usdt=dd_closed,usd_per_week=z['net_usd']/((end-start)/86400000/7),avg_stop_pct=sum(x['risk']/x['entry']*100 for x in hist)/len(hist) if hist else None,avg_risk_usdt=sum(x['risk_usd'] for x in hist)/len(hist) if hist else None,avg_effective_leverage=sum(used_leverage)/len(used_leverage) if used_leverage else None,largest_win_r=max((x['net_r'] for x in hist),default=None),mean_win_r=sum(x['net_r'] for x in hist if x['net_r']>0)/max(1,sum(x['net_r']>0 for x in hist)),mean_loss_r=sum(x['net_r'] for x in hist if x['net_r']<0)/max(1,sum(x['net_r']<0 for x in hist)),rejected=rejected,accepted=accepted,assumption='Fixed reference balance $100 (no compounding)' if not cluster_budget else 'Live ENERGY budget $1 max, declines with equity')
    return z,hist


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('csv',type=Path); ap.add_argument('--out',type=Path,default=Path('h1_research_results')); args=ap.parse_args(); args.out.mkdir(parents=True,exist_ok=True)
    rows,sigs,atrs=load(args.csv)
    variants=[('H1_16_fixed_22',1.6,3.5,2.2,5,False),('H1_25_fixed_22',2.5,3.5,2.2,5,False),('H1_16_unlimited',1.6,3.5,None,5,False),('H1_25_unlimited_5x',2.5,3.5,None,5,False),('H1_25_unlimited_1x',2.5,3.5,None,1,False),('H1_25_live_energy_budget',2.5,3.5,None,5,True)]
    allz={}
    for k,stop,trail,tp,lev,live in variants:
        z,hist=run(rows,sigs,atrs,stop=stop,trail=trail,tp=tp,leverage=lev,cluster_budget=live)
        allz[k]=z; (args.out/f'trades_{k}.json').write_text(json.dumps(hist,indent=2)); print(k,json.dumps(z),flush=True)
    summary={'source':args.csv.name,'bars':len(rows),'first_utc':datetime.fromtimestamp(rows[0]['t'],timezone.utc).isoformat(),'last_utc':datetime.fromtimestamp(rows[-1]['t'],timezone.utc).isoformat(),'warm_days':5,'cost_per_side':.0003,'funding':'NOT_AVAILABLE','mdd':'M1 adverse-first path approximation, not exact tick path','models':allz}
    (args.out/'summary.json').write_text(json.dumps(summary,indent=2))
if __name__=='__main__': main()
