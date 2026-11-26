"""Causal, shrinkage-based P(win), payoff and P(fill) estimates for BZU.

Only same-lifecycle v14.4.6 route labels qualify. Decisions are NEVER rejected by
this module. A new prior can influence sizing/ranking only after chronological
validation. All R values are net of the configured paper execution costs.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict

SCHEMA = 'bzu-expectancy-v3-routes'
LABEL_SCHEMA = 'bzu-shared-lifecycle-m1-v3-routes'
SHARED_LABEL_SCHEMAS = frozenset((LABEL_SCHEMA, 'bzu-shared-lifecycle-m1-v2'))
DAY = 86_400_000
MIN_HOLDOUT = 20
SHRINK = 12.


def number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def context(plan, snapshot, cfg, engine, rvol=None):
    rows = [c for c in snapshot.candles.get('15m', ())
            if c.confirmed and c.ts + 900_000 <= snapshot.now]
    atr = engine.atr(rows) if rows else 0.
    vol = atr / max(plan['entry'], cfg.tick)
    side = plan['side']
    recent = rows[-3:]
    impulse = 'WITH' if recent and side*(recent[-1].close-recent[0].open) > atr else (
        'AGAINST' if recent and side*(recent[-1].close-recent[0].open) < -atr else 'MIXED')
    regime = plan.get('market_regime') or {}
    rr = plan.get('net_rr', 0.)
    from adaptive_entry import route
    return [str(plan.get('setup','UNKNOWN')), side, plan.get('order_type','LIMIT'),
            side * (plan.get('htf_4h') or 0), str(regime.get('state','UNKNOWN')),
            plan.get('meta_context',['UNKNOWN',0,'UNKNOWN','UNKNOWN'])[2],
            plan.get('meta_context',['UNKNOWN',0,'UNKNOWN','UNKNOWN'])[3],
            'UNKNOWN' if not number(rvol) else 'HIGH' if rvol>=1.2 else 'NORMAL',
            'HIGH' if vol>=.006 else 'LOW' if vol<.002 else 'NORMAL',
            'HIGH' if rr>=2. else 'NORMAL', impulse, route(plan)]


def keys(ctx):
    setup, side, order, alignment, regime, location, session, volume, vol, rr, impulse, route = ctx
    return [json.dumps(k,separators=(',',':')) for k in
            ((),(side,order),(setup,side,order),(setup,side,order,alignment,regime),
             (setup,side,order,alignment,regime,location,session,volume,vol,rr,impulse),
             ('ROUTE',route),('ROUTE',route,side,order),('ROUTE',route,setup,side,order))]


def labels(rows, before, after=0):
    """Use only known resolutions, merging a real portfolio outcome over its proxy.

    Correlated same-minute/side episodes with overlapping entry geometry use one
    vote, not one vote for every detector. Distinct non-overlapping levels survive.
    Censored and data-gap rows never count as failed fills.
    """
    usable = []
    for r in rows:
        ctx = r.get('expectancy_context')
        start = r.get('decision_ts', r.get('placed_ts'))
        end = r.get('closed_ts')
        if (r.get('expectancy_label_schema') != LABEL_SCHEMA or not isinstance(ctx,(list,tuple))
                or len(ctx)!=12 or not isinstance(start,int) or not isinstance(end,int)
                or not after<=start<end<before):
            continue
        net = r.get('net_r')
        if not number(net) and r.get('close_reason') not in ('LIMIT_TTL','ENTRY_INVALIDATED','MISSED_TARGET'):
            continue
        # An intrabar TTL touch is not an observed failure to fill.
        if not number(net) and (r.get('ambiguous_ohlc') or r.get('filled_ts') is not None):
            continue
        usable.append({**r,'decision_ts':start,'is_filled':number(net)})
    # Portfolio is preferred when it has a realized result. Keep first-seen
    # information within each source, never select by whether it won.
    usable.sort(key=lambda r:(r.get('source')=='SHADOW_PAPER',r['decision_ts'],r['closed_ts'],str(r.get('id',''))))
    selected=[]; ids=set(); buckets=defaultdict(list)
    for r in usable:
        identity=r.get('timing_episode_id') or r.get('calibration_id') or r.get('event_key') or r.get('id')
        identity=(identity,r['expectancy_context'][-1]) if identity else None
        if not identity or identity in ids:
            continue
        k=(r['decision_ts']//60_000,r['expectancy_context'][1],r['expectancy_context'][2],r['expectancy_context'][-1])
        entry,risk=r.get('entry'),r.get('risk')
        if number(entry) and number(risk) and any(abs(entry-x)<=.25*min(risk,y) for x,y in buckets[k]):
            continue
        ids.add(identity)
        if number(entry) and number(risk):
            buckets[k].append((entry,risk))
        selected.append(r)
    return sorted(selected,key=lambda r:(r['decision_ts'],r['closed_ts'],str(r.get('id',''))))


def independent(rows):
    end=-1
    for r in sorted(rows,key=lambda r:(r['decision_ts'],r['closed_ts'],str(r.get('id','')))):
        if r['decision_ts']>=end:
            yield r
            end=r['closed_ts']


def counts(rows, as_of=None):
    """One independent vote per interval globally, and separately per route.

    Correlated alternative executions cannot inflate pooled evidence. Route
    samples are not pooled into the global validation as independent votes.
    """
    rows=list(rows)
    out={}
    def accumulate(r, ks):
        if as_of is not None and r['closed_ts']>=as_of:
            return
        w=.985**((as_of-r['closed_ts'])/DAY) if as_of is not None else 1.
        for k in ks:
            a=out.setdefault(k,[0.]*6); a[0]+=w
            if r['is_filled']:
                a[1]+=w; net=r['net_r']; a[2 if net>0 else 3]+=w; a[4 if net>0 else 5]+=w*abs(net)
    # Global estimate excludes experimental alternatives when a primary route
    # exists. No multiple votes from the same overlapping event.
    for r in independent(r for r in rows if not r.get('shadow_timing_only')):
        accumulate(r,keys(r['expectancy_context'])[:5])
    for route in sorted({r['expectancy_context'][-1] for r in rows}):
        for r in independent(r for r in rows if r['expectancy_context'][-1]==route):
            accumulate(r,keys(r['expectancy_context'])[5:])
    return out


def valid_counts(value):
    return (isinstance(value,dict) and '[]' in value and all(
        isinstance(a,list) and len(a)==6 and all(number(x) and x>=0 for x in a)
        and a[0]+1e-7>=a[1] and abs(a[1]-a[2]-a[3])<1e-6 for a in value.values()))


def estimate(prior, recent, ctx):
    p,win,loss,fill=.5,1.,1.,.5
    n=0.; global_n=0.; route_n=0.
    for i,k in enumerate(keys(ctx)):
        a=[x+y for x,y in zip(prior.get(k,[0.]*6),recent.get(k,[0.]*6))]
        attempts,fills,wins,losses,positive,negative=a
        if i==0:
            global_n=fills
        # Shrink each more-specific context into its parent.
        p=(wins+SHRINK*p)/(fills+SHRINK)
        win=(positive+SHRINK*win)/(wins+SHRINK)
        loss=(negative+SHRINK*loss)/(losses+SHRINK)
        fill=(fills+SHRINK*fill)/(attempts+SHRINK)
        if i>0:
            n=max(n,fills)
        if i==5:
            route_n=fills
    if ctx[2]=='MARKET':
        fill=1.
    ev=p*win-(1-p)*loss
    return {'p_win':p,'avg_win_r':win,'avg_loss_r':loss,'p_fill':fill,
            'expected_net_r':ev,'expected_attempt_r':fill*ev,
            'confidence':route_n/(route_n+SHRINK),'context_n':n,'global_n':global_n,
            'route_n':route_n,'timing_route':ctx[-1]}


def risk_scale(pred):
    # Never zero risk or use an uncapped Kelly bet; uncertain context tends to 0.65.
    ev=pred['expected_net_r']
    target=max(.35,min(1.,.65+.7*ev))
    return .65+pred['confidence']*(target-.65)


def walk_forward(rows, begin, end, *, route=None):
    hold=begin+(end-begin)*70//100
    folds=[]; ps=[]; evs=[]; weighted=[]; raw=[]; fill_errors=[]
    for i in range(3):
        start=hold+(end-hold)*i//3; finish=hold+(end-hold)*(i+1)//3
        train=[r for r in rows if r['closed_ts']<start]
        test=list(independent(r for r in rows if start<=r['decision_ts']<finish and r['closed_ts']<finish
                              and (r['expectancy_context'][-1]==route if route else not r.get('shadow_timing_only'))))
        prior=counts(train); n=0
        if prior.get('[]',[0,0])[1]>0:
            a=prior['[]']; base_p=(a[2]+.5*SHRINK)/(a[1]+SHRINK)
            base_ev=(a[4]-a[5])/(a[1]+SHRINK)
            for r in test:
                pred=estimate(prior,{},r['expectancy_context'])
                fill_errors.append((pred['p_fill']-int(r['is_filled']))**2)
                if not r['is_filled']:
                    continue
                net=r['net_r']; y=int(net>0)
                ps.append(((pred['p_win']-y)**2,(base_p-y)**2))
                evs.append((abs(pred['expected_net_r']-net),abs(base_ev-net)))
                weighted.append(risk_scale(pred)*net); raw.append(.65*net); n+=1
        folds.append({'train_labels':len(train),'test_labels':n,'test_start_ts':start,'test_end_ts':finish})
    n=len(ps)
    b,b0=(sum(x)/n for x in zip(*ps)) if n else (None,None)
    e,e0=(sum(x)/n for x in zip(*evs)) if n else (None,None)
    enough=n>=MIN_HOLDOUT and sum(f['test_labels']>0 for f in folds)>=2
    valid=bool(enough and b<b0 and e<=e0 and sum(weighted)>=sum(raw))
    return {'validated':valid,'test_labels':n,'brier':b,'baseline_brier':b0,
            'ev_mae':e,'baseline_ev_mae':e0,'sized_net_r':sum(weighted),'constant_risk_net_r':sum(raw),
            'fill_brier':sum(fill_errors)/len(fill_errors) if fill_errors else None,'folds':folds,
            'status':'VALIDATED' if valid else 'INSUFFICIENT_HOLDOUT' if not enough else 'NO_HOLDOUT_IMPROVEMENT'}


def apply(plan, snapshot, cfg, engine, rvol=None):
    ctx=context(plan,snapshot,cfg,engine,rvol)
    plan['timing_route']=ctx[-1]
    plan['expectancy_signature']=engine.quality.calibration_signature(cfg)
    profile=('COUNTER' if ctx[3]<0 else 'TREND' if ctx[4] in ('BULLISH_TREND','BEARISH_TREND','BULLISH_ACTIVE','BEARISH_ACTIVE','TREND_LONG','TREND_SHORT') and ctx[3]>0 else 'RANGE')
    plan.update(expectancy_context=ctx,expectancy_label_schema=LABEL_SCHEMA,
                adaptive_exit_profile=profile,adaptive_exit_enabled=os.getenv('BZU_ADAPTIVE_EXITS','SHADOW').upper()=='ACTIVE')
    if 0<snapshot.bid<=snapshot.ask and plan.get('risk',0)>0:
        plan['spread_r']=(snapshot.ask-snapshot.bid)/plan['risk']
    cal=getattr(snapshot,'_expectancy_calibration',None)
    if not cal:
        plan.pop('expectancy',None)
        monitor=getattr(snapshot,'_expectancy_monitor',{})
        if monitor.get('[]',[0,0])[1]>0 or monitor.get(keys(ctx)[5],[0,0])[1]>0:
            plan.update(expectancy=estimate({},monitor,ctx),expectancy_status='MONITOR_ONLY_ONLINE')
        else:
            plan['expectancy_status']='MONITOR_ONLY_NO_VALIDATED_PRIOR'
        return
    prediction=estimate(*cal,ctx)
    if ctx[-1] not in getattr(snapshot,'_validated_timing_routes',()):
        plan.update(expectancy_status='MONITOR_ONLY_ROUTE_UNVALIDATED',expectancy=prediction)
        return
    plan.update(expectancy_status='VALIDATED',expectancy=prediction,
                meta_prob=prediction['p_win'],meta_prob_calibrated=True)
    plan['risk_multiplier']=min(plan.get('structural_risk_cap',1.),risk_scale(prediction))


def ranking_adjustment(plan):
    p=plan.get('expectancy') or {}
    if plan.get('expectancy_status')!='VALIDATED' or not number(p.get('expected_attempt_r')):
        return 0.
    return max(-4.,min(4.,8*p.get('confidence',0.)*p['expected_attempt_r']))
