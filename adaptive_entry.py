"""Causal, risk-conditioned entry timing. No future bars or probability vetoes.

M15 remains the structural/high-risk clock. Micro triggers use complete UTC
buckets, never a forming bar or a bucket across missing minute data.
"""
from __future__ import annotations

ROUTES = ('EARLY_LIMIT', 'M1', 'M3', 'M5', 'M15')
STEPS = {'1m':60000,'3m':180000,'5m':300000}
PATTERN = 'ADAPTIVE_CLOSED_RECLAIM'
MAX_RECOVERY_MS = 20 * 60_000


def rows(snapshot, tf):
    step=STEPS[tf]
    # Never cache across dataclasses.replace(as_of) without the timestamp key.
    cache=vars(snapshot).setdefault('_adaptive_rows',{})
    key=(snapshot.now,tf)
    if key in cache:
        return cache[key]
    from dataclasses import replace
    minutes={c.ts:c for c in snapshot.candles.get('1m',())
             if c.confirmed and c.ts%60000==0 and c.ts+60000<=snapshot.now}
    if tf=='1m':
        result=[minutes[k] for k in sorted(minutes)]
    elif minutes or snapshot.candles.get('1m'):
        result=[]
        for start in sorted({t//step*step for t in minutes}):
            if start+step>snapshot.now: continue
            group=[minutes.get(t) for t in range(start,start+step,60000)]
            if any(c is None for c in group): continue
            result.append(replace(group[0],ts=start,high=max(c.high for c in group),
                                  low=min(c.low for c in group),close=group[-1].close,
                                  volume=sum(c.volume for c in group),confirmed=True))
    else:
        result=sorted({c.ts:c for c in snapshot.candles.get(tf,()) if c.confirmed and
                       c.ts%step==0 and c.ts+step<=snapshot.now}.values(),key=lambda c:c.ts)
    cache[key]=result
    return result


def approach(snapshot, side, zone, entry, tick, engine):
    from entry_adverse_selection import approach_velocity
    macro=approach_velocity(snapshot,side,zone,entry,tick,engine)
    minute=rows(snapshot,'1m')[-24:]
    a15=engine.atr(engine.policy.closed(snapshot,'15m'))
    recent=minute[-3:]; prior=minute[:-3]
    complete=(len(recent)==3 and len(prior)>=10 and snapshot.now-recent[-1].ts-60000<120000
              and all(b.ts-a.ts==60000 for a,b in zip(minute,minute[1:])))
    if not complete or a15<=0:
        return {**macro,'micro_available':False,'calm':False,'micro_shock':False,'adverse_atr':None}
    a1=max(tick,engine.atr(prior))
    adverse=max(0.,-side*(recent[-1].close-recent[0].open))
    last=recent[-1]; body=-side*(last.close-last.open)
    width=max(tick,last.high-last.low)
    near=abs(snapshot.price-entry)<=1.2*a15 or last.low<=entry<=last.high
    # Normalize to both local and structural volatility; avoid a fixed dollar threshold.
    shock=near and ((body>=max(1.7*a1,.25*a15) and body/width>=.65) or
                    (adverse>=max(3*a1,.65*a15) and sum(side*(c.close-c.open)<0 for c in recent)>=2))
    baseline=sum(c.volume for c in prior[-15:])/min(15,len(prior))
    rvol=last.volume/baseline if baseline>0 else None
    calm=(not macro['blocked'] and not shock and adverse<=.35*a15 and
          max(0.,body)<=1.3*a1 and (rvol is None or body<=0 or rvol<1.8))
    return {**macro,'blocked':bool(macro['blocked'] or shock),'micro_available':True,
            'micro_shock':shock,'calm':calm,'adverse_atr':adverse/a15,
            'adverse_per_minute_atr':adverse/(3*a15),'micro_rvol':rvol,
            'shock_known_ts':last.ts+60000 if shock else macro.get('shock_known_ts'),
            'brake_model':'ADAPTIVE_M1_M15_V1'}


def early_allowed(snapshot,side,entry,zone,regime,sizing,velocity,tick,engine):
    atr=engine.atr(engine.policy.closed(snapshot,'15m'))
    if not (snapshot.trusted and 0<=snapshot.now-snapshot.ticker_ts<=120000 and
            0<snapshot.bid<=snapshot.ask and atr>0 and snapshot.ask-snapshot.bid<=max(2*tick,.12*atr)):
        return False
    quote=snapshot.ask if side==1 else snapshot.bid
    return (regime.get('side_4h')==side and regime.get('side_1h') in (0,side) and
            (regime.get('side_1h')==side or regime.get('trend_anchor_side')==side or
             regime.get('side_15m')==side or (regime.get('local_structure') or {}).get('side')==side) and
            not sizing.get('rejection_required') and not sizing.get('late_entry') and
            velocity.get('calm') is True and 0<=snapshot.now-zone['known_ts']<=8*900000 and
            tick<=side*(quote-entry)<=1.2*atr and
            zone.get('execution_low',zone['low'])-tick<=entry<=zone.get('execution_high',zone['high'])+tick)


def trigger(snapshot,side,zone,entry,risk,cfg,engine,tf):
    """Recover a closed witness since the previous scan, never a historical fill.

    After a missed trigger the current quote must still be close to the level;
    every subsequent minute must be present and preserve the reclaim/stop.
    A bounded lookback prevents resurrecting old signals after long outages.
    """
    step=STEPS[tf]; bars=rows(snapshot,tf)
    if (len(bars)<16 or not snapshot.trusted or risk<=0 or
            not 0<=snapshot.now-snapshot.ticker_ts<=120000 or
            not 0<snapshot.bid<=snapshot.ask):
        return None
    a15=engine.atr(engine.policy.closed(snapshot,'15m'))
    quote=snapshot.ask if side==1 else snapshot.bid
    radius=min(.25*a15,.18*risk)
    if a15<=0 or not -cfg.tick/2<=side*(quote-entry)<=radius:
        return None
    gap=max(0,int(snapshot.metadata.get('scan_gap_ms') or 0))
    horizon=min(MAX_RECOVERY_MS,max(step,gap))
    minutes=rows(snapshot,'1m')
    last_closed=snapshot.now//60000*60000
    for i in range(len(bars)-1,14,-1):
        bar,prev=bars[i],bars[i-1]; known=bar.ts+step
        age=snapshot.now-known
        if age>=horizon: break
        if (age<0 or snapshot.ticker_ts<known or bar.ts<zone['known_ts'] or
                any(b.ts-a.ts!=step for a,b in zip(bars[i-15:i],bars[i-14:i+1]))):
            continue
        if not (bar.low<=entry+cfg.tick and bar.high>=entry-cfg.tick and side*(bar.close-entry)>=cfg.tick):
            continue
        atr=engine.atr(bars[max(0,i-15):i])
        if atr<=0: continue
        width=max(cfg.tick,bar.high-bar.low)
        wick=min(bar.open,bar.close)-bar.low if side==1 else bar.high-max(bar.open,bar.close)
        body=side*(bar.close-bar.open)
        edge=prev.high if side==1 else prev.low
        shift=side*(bar.close-edge)>=cfg.tick and body>=.5*atr
        rejection=wick>=.35*width and body>=cfg.tick and side*(bar.close-entry)>=max(cfg.tick,.15*atr)
        if not (shift or (tf!='1m' and rejection)):
            continue
        # Require complete M1 evidence after the witness, including all minutes
        # between it and the actual scan; never use the historical close as fill.
        later=[c for c in minutes if known<=c.ts<last_closed]
        if [c.ts for c in later]!=list(range(known,last_closed,60000)):
            continue
        stop=entry-side*risk
        if any(side*(c.close-entry)<-cfg.tick or
               (c.low<=stop if side==1 else c.high>=stop) for c in later):
            continue
        if side*(quote-bar.close)>radius:
            continue
        baseline=sum(c.volume for c in bars[i-15:i])/15
        return {'pattern':PATTERN,'known_ts':known,'limit_entry':entry,'initial_risk':risk,
                'max_distance':radius,'reference_close':bar.close,'confirmation_timeframe':tf,
                'strict_rejection':False,'micro_shift':shift,'wick_ratio':wick/width,
                'rvol':bar.volume/baseline if baseline>0 else None,
                'body_atr':body/atr,'recovered_between_scans':age>=step,
                'confirmation_age_ms':age,'scan_gap_ms':gap}
    return None


def counter_m5_context_allowed(side, regime, sizing, velocity):
    return bool(regime.get('side_4h')==-side and regime.get('side_1h')==side and
                not sizing.get('late_entry') and not sizing.get('exhausted_wave') and
                velocity.get('micro_available') and not velocity.get('blocked'))


def counter_m5_allowed(snapshot, side, regime, sizing, velocity, proof):
    """Counter-H4 MARKET needs H1 alignment and affirmative local reversal.

    M15 is retained for shock, late entry, exhaustion and NY opening policy.
    The existing countertrend risk cap is unchanged.
    """
    return bool(counter_m5_context_allowed(side,regime,sizing,velocity) and
                proof and proof.get('confirmation_timeframe')=='5m' and
                proof.get('micro_shift') and (proof.get('rvol') or 0)>=1.2 and
                (proof.get('body_atr') or 0)>=.65)


def route(plan):
    if plan.get('timing_route') in (*ROUTES,'LEGACY_LIMIT'): return plan['timing_route']
    if plan.get('order_type')=='LIMIT' and not plan.get('entry_confirmation') and not plan.get('entry_retest_reason'):
        return 'LEGACY_LIMIT'
    proof=plan.get('entry_confirmation') or plan.get('market_trigger') or plan.get('timing_witness') or {}
    tf=proof.get('confirmation_timeframe')
    if tf in STEPS: return {'1m':'M1','3m':'M3','5m':'M5'}[tf]
    return 'M5' if str(proof.get('pattern','')).startswith(('M5_','MARKET_FRONT_RUN')) else 'M15'
