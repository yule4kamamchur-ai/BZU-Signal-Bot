"""Comparative exploratory replay of four *new* entry hypotheses (NOT live).

No tuning, no lookahead and no claim of exchange execution. Signals are emitted
only after a 15m close; a passive 3m fill can occur at least a whole 3m bar
later, requires passing the limit by a full tick, and is rejected on expiration.
Stop wins OHLC ambiguity. Every split censors unresolved trades at its end.

Archive historical OHLC is not proof that the original bot observed it live.
Run: python strategy_research.py --output exploratory-results.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
from statistics import mean

FIFTEEN = 900_000
THREE = 180_000
ONE_HOUR = 3_600_000
TICK = .01  # fixed conservative illustrative tick; NOT verified exchange tick
MAKER_FEE = .0002
TAKER_FEE = .0005
SLIPPAGE = .0002


def atr(rows, n=14):
    if len(rows) < n+1:
        return 0.
    tr=[]
    for i in range(-n,0):
        c=rows[i];p=rows[i-1]
        tr.append(max(c[2]-c[3], abs(c[2]-p[4]),abs(c[3]-p[4])))
    return mean(tr)


def trend(rows):
    if len(rows)<13:return 0
    w=rows[-13:]; step=abs(w[-1][4]-w[0][4]); total=sum(abs(y[4]-x[4]) for x,y in zip(w,w[1:]))
    v=atr(rows,12)
    if v<=0 or step/v < 1.25 or (step/max(total,1e-10))<.31:return 0
    return 1 if w[-1][4]>w[0][4] else -1


def candidate(method, bars, h1, h4):
    """Fully determined from candles closed at or before the signal instant."""
    if len(bars)<26:return None
    c=bars[-1];old=bars[:-1]; v=atr(bars)
    if v<=0:return None
    trend1=trend(h1); trend4=trend(h4)
    side=0; entry=0.;reason=''
    if method=='SWEEP_RECLAIM_REACTION':
        lo=min(x[3] for x in old[-20:]); hi=max(x[2] for x in old[-20:])
        # NOT a complete ICT MSS: proxy requiring closed reclaim and impulse.
        if c[3] < lo-TICK and c[4]>lo and c[4]>c[1] and c[4]-c[1]>.30*v:
            side=1;entry=c[4]-.25*v
        elif c[2]>hi+TICK and c[4]<hi and c[4]<c[1] and c[1]-c[4]>.30*v:
            side=-1;entry=c[4]+.25*v
    elif method=='SWEEP_THEN_MSS_EVENT':
        # A completed sweep/reclaim must precede a distinct CLOSED structural
        # break. The pivot is frozen strictly before the sweep candle.
        for j in range(len(bars)-2, max(21,len(bars)-7), -1):
            raid=bars[j];pre=bars[j-20:j]
            low=min(x[3] for x in pre); high=max(x[2] for x in pre)
            # A pivot needs two right-hand bars CLOSED before the raid.
            ups=[bars[k][2] for k in range(max(2,j-20),j-2)
                 if bars[k][2]>max(b[2] for b in bars[k-2:k]+bars[k+1:k+3])]
            downs=[bars[k][3] for k in range(max(2,j-20),j-2)
                   if bars[k][3]<min(b[3] for b in bars[k-2:k]+bars[k+1:k+3])]
            up_break=ups[-1] if ups else None
            down_break=downs[-1] if downs else None
            if (up_break is not None and raid[3]<low-TICK and raid[4]>low and
                all(b[4]<=up_break+TICK for b in bars[j+1:-1]) and
                c[4]>up_break+TICK and c[4]-c[1]>.45*v):
                side=1;entry=c[4]-.30*v;break
            if (down_break is not None and raid[2]>high+TICK and raid[4]<high and
                all(b[4]>=down_break-TICK for b in bars[j+1:-1]) and
                c[4]<down_break-TICK and c[1]-c[4]>.45*v):
                side=-1;entry=c[4]+.30*v;break
    elif method=='HTF_TREND_FVG_RETEST':
        if len(bars)<3:return None
        a,m,z=bars[-3:]
        if trend1==1 and trend4!=-1 and z[3]>a[2]+2*TICK and m[4]-m[1]>.55*v:
            side=1;entry=(z[3]+a[2])/2
        elif trend1==-1 and trend4!=1 and z[2]<a[3]-2*TICK and m[1]-m[4]>.55*v:
            side=-1;entry=(z[2]+a[3])/2
    elif method=='ACCEPTED_BREAK_RETEST':
        hi=max(x[2] for x in old[-20:]);lo=min(x[3] for x in old[-20:])
        if trend1==1 and c[4]>hi+.1*v and c[4]-c[1]>.5*v:
            side=1;entry=hi
        elif trend1==-1 and c[4]<lo-.1*v and c[1]-c[4]>.5*v:
            side=-1;entry=lo
    elif method=='RANGE_EDGE_REJECTION':
        if trend1!=0:return None
        hi=max(x[2] for x in old[-20:]);lo=min(x[3] for x in old[-20:])
        if c[3]<lo-TICK and c[4]>lo and c[4]-c[3]>.55*v:
            side=1;entry=c[4]-.25*v
        elif c[2]>hi+TICK and c[4]<hi and c[2]-c[4]>.55*v:
            side=-1;entry=c[4]+.25*v
    if side==0 or side*(c[4]-entry) <= 2*TICK:
        return None
    # Fixed costs, fixed target to permit honest comparison of entries.
    risk=max(.01*entry,1.1*v)
    tp=entry+side*1.8*risk
    stop=entry-side*risk
    return {'side':side,'entry':entry,'tp':tp,'stop':stop,'risk':risk,
            'known_ts':c[0]+FIFTEEN,'method':method}


def execute(signal, futures, end_ts):
    """One passive limit; strict fill, conservative same-bar STOP, finite TTL."""
    side=signal['side'];entry=signal['entry'];tp=signal['tp'];stop=signal['stop'];r=signal['risk']
    born=signal['known_ts'];expiry=born+60*60_000
    fill=None;exit_price=None;reason=''
    for bar in futures:
        if bar[0]+THREE>end_ts:
            break
        if bar[0]<born+THREE:continue
        if fill is None:
            if bar[0]>expiry:return {'status':'UNFILLED'}
            touched=(bar[3]<entry-TICK if side==1 else bar[2]>entry+TICK)
            if not touched:continue
            fill=bar[0]
            # OHLC cannot prove whether stop preceded entry on the fill bar.
            if (bar[3]<=stop if side==1 else bar[2]>=stop):
                exit_price=stop;reason='FILL_BAR_STOP'
            elif (bar[4]>=tp if side==1 else bar[4]<=tp):
                # Conservative: don't infer the post-entry high/low in a bar.
                continue
        elif (bar[3]<=stop if side==1 else bar[2]>=stop):
            exit_price=stop;reason='STOP'
        elif (bar[2]>=tp if side==1 else bar[3]<=tp):
            exit_price=tp;reason='TP'
        elif bar[0]>=fill+360*60_000:
            exit_price=bar[4];reason='TIMEOUT'
        if exit_price is not None:
            cost=(MAKER_FEE*entry+TAKER_FEE*exit_price+SLIPPAGE*exit_price)/r
            net_r=side*(exit_price-entry)/r-cost
            return {'status':'CLOSED','net_r':round(net_r,5),'result':reason,'exit_ts':bar[0]+THREE,
                    'fill_ts':fill}
    return {'status':'CENSORED' if fill is not None else 'UNFILLED_OR_CENSORED'}


def evaluate(archive):
    three=sorted((c for c in archive['candles']['3m'] if str(c[8])=='1'),key=lambda r:r[0])
    quarter=sorted((c for c in archive['candles']['15m'] if str(c[8])=='1'),key=lambda r:r[0])
    hours=sorted((c for c in archive['candles']['1H'] if str(c[8])=='1'),key=lambda r:r[0])
    fours=sorted((c for c in archive['candles']['4H'] if str(c[8])=='1'),key=lambda r:r[0])
    dates=[three[0][0]+THREE]+[three[0][0]+THREE+int((three[-1][0]-three[0][0])*(i/3)) for i in (1,2)]+[three[-1][0]+THREE]
    methods=('SWEEP_RECLAIM_REACTION','SWEEP_THEN_MSS_EVENT',
             'HTF_TREND_FVG_RETEST',
             'ACCEPTED_BREAK_RETEST','RANGE_EDGE_REJECTION')
    reports={}
    # Each third is tested separately. No model is tuned/picked on the later third.
    for method in methods:
        results=[]
        for fold in range(3):
            start,end=dates[fold],dates[fold+1]
            closed=[];issued=0;unfilled=0;censored=0;busy_until=start
            for i in range(26,len(quarter)):
                decision=quarter[i][0]+FIFTEEN
                if not (start<=decision<end) or decision<busy_until:continue
                all15=quarter[:i+1]
                h1=[c for c in hours if c[0]+ONE_HOUR<=decision]
                h4=[c for c in fours if c[0]+4*ONE_HOUR<=decision]
                s=candidate(method,all15,h1,h4)
                if not s:continue
                issued+=1
                future=[c for c in three if decision+THREE<=c[0]<min(end,decision+420*60_000)]
                x=execute(s,future,end)
                if x['status']=='CLOSED':
                    closed.append(x['net_r']);busy_until=x['exit_ts']
                elif x['status']=='UNFILLED':
                    unfilled+=1;busy_until=decision+60*60_000
                else:
                    censored+=1;busy_until=min(end,decision+420*60_000)
            n=len(closed)
            results.append({'fold':fold+1,'signals':issued,'closed':n,
                'wins':sum(r>0 for r in closed),'win_rate':round(sum(r>0 for r in closed)/n,4) if n else None,
                'net_r':round(sum(closed),4),'mean_net_r':round(mean(closed),4) if n else None,
                'unfilled':unfilled,'censored':censored})
        reports[method]=results
    return {'schema':'BZU_FIXED_RULE_REPLAY_V1','disclaimer':(
        'Exploratory historical 15m->3m OHLC replay on ONE short market sample; '
        'NOT source-observed live, NOT optimized, NOT walk-forward certification. '
        'No fill queue data. Never automatically promote a strategy on these numbers.'),
        'period_utc_ms':{'first':dates[0],'last':dates[-1]},'folds':reports,
        'cost_assumptions':{'maker':MAKER_FEE,'taker':TAKER_FEE,'slippage':SLIPPAGE,'tick':TICK,
                            'stop':'>=1%','target':'1.8R','limit_ttl_min':60}}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',default='research_market_v12_6.json')
    parser.add_argument('--output',default='strategy-research-results.json')
    args=parser.parse_args()
    outcome=evaluate(json.loads(Path(args.archive).read_text()))
    Path(args.output).write_text(json.dumps(outcome,ensure_ascii=False,indent=2)+'\n')
    for name, folds in outcome['folds'].items():
        print(name,[(x['closed'],x['wins'],x['net_r']) for x in folds])


if __name__ == '__main__':
    main()
