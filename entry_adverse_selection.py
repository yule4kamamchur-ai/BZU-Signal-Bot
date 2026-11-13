"""Conservative, causal admission checks for passive ICT retracement entries.

A nearby unswept HTF high/low is not by itself a reason to avoid a trade.
A repeated rejection *at that pool*, before a continuation entry, is a
specific warning that a passive fill might occur on a failed breakout.

No probabilities or claims of backtested profitability are produced here.
For every evaluation only closed bars that were already knowable at the
candidate decision timestamp are admitted. This module cannot reprice or
cancel an existing order, and never reads its eventual outcome.
"""
from __future__ import annotations

from math import isfinite

HOUR_MS=3_600_000
MIN_PROXIMITY_R=0.35
MIN_REJECTIONS=2
RECENT_HOURS=6


def approach(snapshot, atr15, side=0, zone=None, tick=.001, stop=None, *, trend=False):
    """Brake only a fresh, dominant adverse candle closing near the planned SL.

    The last candle is compared with the prior ATR, excluding that candle.
    Ordinary pullbacks and two aggregated moderate candles are not shocks.
    Closed M5 can release an observed shock; open candles never confirm entry.
    """
    if trend:
        import setup_scanner as scanner

        bars = scanner.closed_five_minutes(snapshot)
        bar = bars[-1] if bars else None
        blocked = bool(bar and 0 <= snapshot.now - (bar.ts + 300_000) < 300_000
                       and bar.high - bar.low > 1.20 + 1e-8 and side * (bar.close - bar.open) < 0)
        return {'blocked': blocked, 'fast': blocked, 'evaluated_timeframe': '5m',
                'shock_known_ts': bar.ts + 300_000 if blocked else None,
                'brake_model': 'TREND_M5_SPIKE_1_20',
                'action': 'CONFIRMATION_REQUIRED' if blocked else 'PRECISION_ENTRY'}

    rows=sorted((c for c in snapshot.candles.get('15m',[])
                 if c.confirmed and c.ts+900_000<=snapshot.now),key=lambda c:c.ts)
    base={'fast':False,'range_atr15':0.,'blocked':False,'action':'PRECISION_ENTRY',
          'veto_enabled':True,'shock_known_ts':None,'brake_model':'LAST_CANDLE_NEAR_STOP_V28'}
    if side not in (-1,1) or atr15<=0 or not zone:
        return base
    prior=rows[-15:-1]
    true_ranges=[max(c.high-c.low,abs(c.high-prior[i-1].close),abs(c.low-prior[i-1].close))
                 if i else c.high-c.low for i,c in enumerate(prior)]
    baseline=sum(true_ranges)/len(true_ranges) if len(true_ranges)>=5 else atr15
    if baseline<=0:
        return base
    stop=(zone['low']-.25*baseline if side==1 else zone['high']+.25*baseline) if stop is None else stop
    import setup_scanner as scanner
    micro=scanner.closed_five_minutes(snapshot)
    if rows:
        proof=intra_bar_reaction(snapshot,rows[-1],side,zone,baseline,tick)
        if proof:
            return {**base,'fast':True,'action':'CONFIRMED_RETEST','confirmation_timeframe':'5m',
                    'confirmation_known_ts':proof['known_ts'],'confirmation_pattern':proof['pattern']}
    quarter=snapshot.now//900_000*900_000
    sample=[c for c in micro if quarter<=c.ts and c.ts+300_000<=snapshot.now]
    expected=(snapshot.now-quarter)//300_000
    complete=bool(expected and [c.ts for c in sample]==[quarter+i*300_000 for i in range(expected)])
    if complete and len(sample)>=2:
        proof=_micro_reaction(snapshot,sample,side,zone,baseline,tick)
        if proof:
            return {**base,'fast':True,'action':'CONFIRMED_RETEST','confirmation_timeframe':'5m',
                    'confirmation_known_ts':proof['known_ts'],'confirmation_pattern':proof['pattern']}
    candidates=[]
    if rows and snapshot.now-(rows[-1].ts+900_000)<900_000:
        candidates.append((rows[-1],rows[-1].ts+900_000,'15m'))
    if complete:
        candidates.append((sample[-1],sample[-1].ts+300_000,'5m'))
    if not candidates:
        return {**base,'data_status':'NO_FRESH_CLOSED_CANDLE'}
    # A newer closed 5m prefix supersedes the preceding 15m candle.
    candle,known,tf=max(candidates,key=lambda x:x[1])
    width=max(candle.high-candle.low,tick)
    adverse_body=-side*(candle.close-candle.open)
    stop_side_wick=candle.close-candle.low if side==1 else candle.high-candle.close
    near_stop=side*(candle.close-stop)<=.25*baseline+tick*.5
    blocked=(width>1.8*baseline and adverse_body>=.7*width and
             stop_side_wick<=.1*width and near_stop)
    return {**base,'range_atr15':width/baseline,'atr_baseline':baseline,'stop_distance':side*(candle.close-stop),
            'near_stop':near_stop,'body_fraction':adverse_body/width,'stop_side_wick_fraction':stop_side_wick/width,
            'fast':bool(blocked),'blocked':bool(blocked),'shock_known_ts':known if blocked else None,
            'evaluated_timeframe':tf,'action':'CONFIRMATION_REQUIRED' if blocked else 'PRECISION_ENTRY'}


def continuation_market_retracement(zone, side, quote):
    proof = zone.get('wave_proof') or {}
    start, end = proof.get('leg_start'), proof.get('leg_end')
    if start is None or end is None or side*(end-start) <= 0:
        return False
    retracement = side*(end-quote)/abs(end-start)
    return .35-1e-8 <= retracement <= .50+1e-8


def confirmed_breakout_limit(zone):
    """Keep the shallow retest as a LIMIT; an extension alone is not a veto."""
    proof = zone.get('wave_proof') or {}
    return zone.get('kind') == 'BOS_CONTINUATION' and proof.get('retracement') == .30


def intra_bar_reaction(snapshot, shock, side, zone, atr15, tick):
    """Match three independently closed M5 bars to a completed M15."""
    import setup_scanner as scanner

    end = shock.ts + 900_000
    if not shock.confirmed or not 0 <= snapshot.now - end < 300_000:
        return None
    bars = [c for c in scanner.closed_five_minutes(snapshot) if shock.ts <= c.ts < end]
    if [c.ts for c in bars] != [shock.ts + i*300_000 for i in range(3)]:
        return None
    combined = (bars[0].open, max(c.high for c in bars), min(c.low for c in bars), bars[-1].close)
    if any(abs(a-b) > tick*.51 for a,b in zip(combined, (shock.open, shock.high, shock.low, shock.close))):
        return None
    return _micro_reaction(snapshot, bars, side, zone, atr15, tick)


def _micro_reaction(snapshot, bars, side, zone, atr15, tick):
    """The last closed M5 bar reverses an earlier M5 move at the zone."""
    first, previous, last = bars[0], bars[-2], bars[-1]
    end = last.ts + 300_000
    if snapshot.now - end >= 300_000:
        return None
    early=max(-side*(first.close-first.open),
              -side*(previous.close-previous.open),
              -side*(previous.close-first.open))
    if early<=1.8*atr15 or last.low>zone['high'] or last.high<zone['low'] or end <= zone.get('known_ts',0):
        return None
    body=side*(last.close-last.open)
    wick=min(last.open,last.close)-last.low if side==1 else last.high-max(last.open,last.close)
    width=max(last.high-last.low,tick)
    engulf=(side*(previous.close-previous.open)<0 and body>0
            and side*(last.open-previous.close)<=tick*.5
            and side*(last.close-previous.open)>=-tick*.5)
    pin=body>0 and wick>=.5*width and wick>=2*max(body,tick)
    defended=side*(last.close-(zone['low']+zone['high'])/2)>0
    if not (defended and (engulf or pin)):
        return None
    # A later completed minute may already have invalidated the reaction.
    if any(c.confirmed and c.ts>=end and c.ts+60_000<=snapshot.now
           and side*(c.close-(zone['low'] if side==1 else zone['high']))<0
           for c in snapshot.candles.get('1m',[])):
        return None
    return {'timeframe':'5m','known_ts':end,'pattern':'ENGULFING' if engulf else 'PINBAR'}


def _number(value):
    try:
        n=float(value)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def rejected_nearby_liquidity(side, entry, risk, targets, hour_bars, as_of_ts, tick):
    """Return one observable trap or None; do not infer from missing bars.

    A near unraided 1H liquidity pool plus repeated completed wick rejections
    with no accepted closes through it is required. No veto on plain equal
    highs/lows, single-candle noise, or a successfully broken pool.
    """
    sign=int(side)
    e=_number(entry); r=_number(risk); now=_number(as_of_ts); tick=_number(tick)
    if sign not in (-1,1) or None in (e,r,now,tick) or r<=0 or tick<=0:
        return None
    rows=[c for c in hour_bars if getattr(c,'confirmed',False)
          and c.ts+HOUR_MS<=now]
    if len(rows)<RECENT_HOURS:
        return None
    recent=rows[-RECENT_HOURS:]
    spans=[max(c.high-c.low,tick) for c in recent]
    avg_span=sum(spans)/len(spans)
    for zone in targets:
        if zone.get('kind')!='1H_LIQUIDITY' or zone.get('freshness')!='UNSWEPT':
            continue
        level=_number(zone.get('level')); known=_number(zone.get('known_ts'))
        if level is None or (known is not None and known>now):
            continue
        distance=sign*(level-e)
        if not (2*tick < distance <= MIN_PROXIMITY_R*r):
            continue
        # Two closes outside the pool invalidate the notion of rejection.
        if all(sign*(c.close-level)>tick for c in recent[-2:]):
            continue
        # Don't penalize legitimate acceptance after the high/low is raided.
        if any(sign*(c.close-level)>max(tick,.08*r) for c in recent[-3:]):
            continue
        wick_min=max(2*tick,.15*avg_span)
        proximity=max(.25*r,.35*avg_span,3*tick)
        rejects=[]
        for c in recent:
            width=max(c.high-c.low,tick)
            wick=(c.high-max(c.open,c.close) if sign==1
                  else min(c.open,c.close)-c.low)
            extreme=c.high if sign==1 else c.low
            below=sign*(c.close-level)<-tick
            near=abs(extreme-level)<=proximity
            if below and near and wick>=wick_min and wick/width>=.4:
                rejects.append(c.ts+HOUR_MS)
        if len(rejects)>=MIN_REJECTIONS:
            return {'risk':'HTF_REJECTED_NEAR_LIQUIDITY',
                    'liquidity_level':level,'headroom_r':round(distance/r,4),
                    'rejected_closes':len(rejects),'latest_rejection_known_ts':rejects[-1],
                    'observation_ts':now,'timeframe':'1H'}
    return None


def admission(anchor, snapshot, targets, entry, risk, tick):
    """Only new trend-continuation limit proposals are subject to this veto."""
    family=anchor.evidence.get('canonical_setup_family', '')
    setup=anchor.setup_type
    continuation=family=='TREND_CONTINUATION' or setup in {
        'PULLBACK_CONTINUATION','FRESH_BASE_CONTINUATION',
        'ACCELERATION_PULLBACK_REENTRY','ACCEPTANCE_RETEST_CONTINUATION',
        'MOMENTUM_NO_PULLBACK_CONTINUATION'}
    if not continuation:
        return None
    side=1 if str(anchor.side).upper() in ('LONG','1') else -1 if str(anchor.side).upper() in ('SHORT','-1') else 0
    return rejected_nearby_liquidity(side,entry,risk,targets,
                                      snapshot.candles.get('1H',[]),snapshot.now,tick)


def failed_thesis_after_deadline(plan, snapshot, tick):
    """Review a losing thesis once its originally configured horizon expires.

    A TP1-gated full-volume position must not be held indefinitely just
    because it has not hit TP1. A clock ALONE does not authorize an exit:
    require a material adverse move, a fresh 1H downside/upside close, and
    agreement from four completed 15m bars. All checks use as-of closed bars.
    The engine separately locks this policy to entries made with its version.
    """
    side=plan.get('side'); entry=_number(plan.get('entry'))
    risk=_number(plan.get('risk')); deadline=_number(plan.get('exit_deadline'))
    price=_number(getattr(snapshot,'price',None)); now=_number(getattr(snapshot,'now',None))
    if side not in (-1,1) or None in (entry,risk,deadline,price,now) or risk<=0:
        return None
    # Intraday session_cutoff may be EARLIER than the configured holding
    # horizon. TP1-gated legacy plans ignored it. For this new risk review,
    # never exit early simply because the Kyiv session crossed midnight.
    fill=_number(plan.get('filled_ts'))
    hold=_number(plan.get('max_hold_minutes'))
    if fill is not None and hold is not None and hold>0:
        deadline=max(deadline,fill+hold*60_000)
    if now<deadline:
        return None
    # A trade which already demonstrated strong favorable follow-through is
    # NOT a stalled entry. Its later pullback belongs to the existing TP1
    # management contract; a timeout must not erase such recoveries.
    achieved=_number(plan.get('mfe_r',0.))
    if side*(price-entry)/risk > -0.25 or plan.get('tp1_hit') or (achieved is not None and achieved>=0.8):
        return None
    def read(tf,step):
        return [c for c in snapshot.candles.get(tf,[]) if getattr(c,'confirmed',False)
                and c.ts+step<=now]
    rows15=read('15m',900_000);rows1=read('1H',HOUR_MS)
    if len(rows15)<6 or len(rows1)<5:
        return None
    # No stale hourly confirmation: the hourly bar must have become available
    # within the last 90 minutes. A delayed workflow cannot imply fresh data.
    if now-(rows1[-1].ts+HOUR_MS)>90*60_000:
        return None
    # Require close through recent higher-TF price progress, NOT just an
    # intrabar wick that might reverse before the next closed observation.
    h_reference=(min(c.close for c in rows1[-4:-1]) if side==1
                 else max(c.close for c in rows1[-4:-1]))
    hourly_break=side*(rows1[-1].close-h_reference) < -max(2*tick,.07*risk)
    ltf_adverse=side*(rows15[-1].close-rows15[-5].close)<-max(2*tick,.07*risk)
    if hourly_break and ltf_adverse:
        return {'risk':'POST_DEADLINE_THESIS_INVALIDATED',
                'loss_at_review_r':round(side*(price-entry)/risk,4),
                'h1_reference':h_reference,'last_hourly_close':rows1[-1].close,
                'latest_15m_close':rows15[-1].close,'as_of_ts':now}
    return None


def approach_velocity(snapshot, side, zone, entry, tick, engine, *, shock_ts=0):
    """Hold after a closed adverse burst until a newer M15 confirms braking."""
    from directional_policy import closed
    from oil_risk import M15
    from market_wave import BASE

    rows = closed(snapshot, '15m')[-32:]
    if (zone.get('kind') in (BASE,'RECLAIM_BREAKER','SR_FLIP_RECLAIM')
            and zone.get('wave_proof') and 0 < shock_ts <= zone.get('known_ts',0) <= snapshot.now):
        shock_ts = 0
    for i in range(max(15,len(rows)-16),len(rows)):
        bar, prev = rows[i], rows[i-1]
        known = bar.ts+M15
        if known <= max(shock_ts,zone.get('known_ts',0)):
            continue
        atr = engine.atr(rows[i-15:i])
        if atr <= 0:
            continue
        width = max(tick,bar.high-bar.low)
        wick = (bar.close-bar.low if side==1 else bar.high-bar.close)/width
        body = -side*(bar.close-bar.open)
        two = (-side*(bar.close-prev.open) if bar.ts-prev.ts==M15 and
               side*(prev.close-prev.open)<0 else 0)
        near = abs(bar.close-entry)<=.65*atr or bar.low<=entry<=bar.high
        if near and max(body,two)>1.3*atr and body>=.65*width and wick<.20:
            shock_ts = known
        elif shock_ts and near and side*(bar.close-entry)>=-tick/2 and (
                side*(bar.close-bar.open)>tick or wick>=.30):
            shock_ts = 0
    return {'blocked': bool(shock_ts), 'shock_known_ts': shock_ts or None,
            'action': 'CONFIRMATION_REQUIRED' if shock_ts else 'PRECISION_ENTRY',
            'brake_model': 'M15_APPROACH_VELOCITY'}


def rejection_trigger(snapshot, side, zone, entry, risk, quote, tick):
    from directional_policy import closed
    from oil_risk import M15

    if (not snapshot.trusted or not 0 <= snapshot.now-snapshot.ticker_ts <= 120_000
            or risk <= 0 or abs(quote-entry) > .20*risk + 1e-8):
        return None
    rows = closed(snapshot, '15m')
    if not rows:
        return None
    bar = rows[-1]
    known = bar.ts+M15
    origin = (zone.get('wave_proof') or {}).get('break_ts') or zone['known_ts']
    if origin > bar.ts:
        return None
    if not 0 <= snapshot.ticker_ts-known < M15:
        return None
    extreme = bar.low if side==1 else bar.high
    wick = min(bar.open,bar.close)-bar.low if side==1 else bar.high-max(bar.open,bar.close)
    if (not zone['low']-tick <= extreme <= zone['high']+tick or
            side*(bar.close-entry) <= tick/2 or abs(bar.close-entry) > .20*risk + 1e-8):
        return None
    if wick < .20*max(tick,bar.high-bar.low) and side*(bar.close-bar.open) <= tick:
        return None
    return {'pattern':'M15_LEVEL_REJECTION','known_ts':known, 'limit_entry':entry,
            'initial_risk':risk, 'deviation_r':abs(quote-entry)/risk}


def trend_zone_market_trigger(snapshot, side, zone, risk, quote, tick, engine):
    """A closed M15 continuation may execute only near its witnessed level."""
    from directional_policy import closed
    from market_wave import BASE, M15

    if zone.get('kind') not in (BASE,'RECLAIM_BREAKER','SR_FLIP_RECLAIM'):
        return None
    rows = closed(snapshot,'15m')
    if len(rows)<2 or not snapshot.trusted or not 0<=snapshot.now-snapshot.ticker_ts<=120_000 or risk<=0:
        return None
    bar = rows[-1]
    known = bar.ts+M15
    proof = zone.get('wave_proof') or {}
    if not 0<=snapshot.ticker_ts-known<M15 or side*(bar.close-bar.open)<tick-1e-8:
        return None
    base = zone['kind']==BASE
    if base and proof.get('break_ts')!=known:
        return None
    if not base and proof.get('break_ts',known)>bar.ts:
        return None
    reference = zone['reference']
    distance = min(.32,.45*engine.atr(rows[:-1]))
    if (abs(quote-reference)>distance+1e-8 or abs(bar.close-reference)>distance+1e-8
            or min(side*(bar.close-reference),side*(quote-reference))<-tick/2):
        return None
    # A witnessed base may be close to the session extreme. Its own closed
    # breakout/hold and quote distance decide entry, without a peak veto.
    extreme = bar.low if side==1 else bar.high
    if not base and not zone['low']-tick<=extreme<=zone['high']+tick:
        return None
    return {'pattern':'M15_TREND_ZONE','known_ts':known,'limit_entry':reference,
            'initial_risk':risk,'max_distance':distance,'deviation_r':abs(quote-reference)/risk}


def structural_impulse_market_trigger(snapshot, side, zone, entry, risk, quote, tick, engine):
    from directional_policy import closed
    from market_wave import M15

    rows = closed(snapshot,'15m')
    if (len(rows)<2 or risk<=0 or not snapshot.trusted
            or not 0<=snapshot.now-snapshot.ticker_ts<=120_000):
        return None
    bar = rows[-1]
    known = bar.ts+M15
    atr = engine.atr(rows[:-1])
    kind = zone.get('kind','')
    structural = any(token in kind for token in ('SWEEP','OB','FVG','CHOCH','BREAKER'))
    if (not 0<=snapshot.ticker_ts-known<M15
            or not structural or atr<=0
            or known not in (zone['known_ts'],zone.get('impulse_known_ts'))
            or side*(bar.close-bar.open)<max(tick,.60*atr)-1e-8):
        return None
    distance = min(.32,.45*atr)
    if (abs(quote-entry)>distance+1e-8 or abs(bar.close-entry)>distance+1e-8
            or min(side*(quote-entry),side*(bar.close-entry)) < -tick/2):
        return None
    return {'pattern':'M15_STRUCTURAL_IMPULSE','known_ts':known,'limit_entry':entry,
            'initial_risk':risk,'max_distance':distance,'deviation_r':abs(quote-entry)/risk}


def breakdown_limit_distance(zone):
    atr = (zone.get('wave_proof') or {}).get('breakdown_atr') or 0
    return max(.10,min(.50,atr))


def breakdown_market_trigger(snapshot, zone, entry, risk, quote, tick):
    """Only a fresh executable quote close to the broken support."""
    from directional_policy import closed
    from market_wave import M15, breakdown_intact

    known = (zone.get('wave_proof') or {}).get('break_ts',0)
    if (not snapshot.trusted or not 0 <= snapshot.now-snapshot.ticker_ts <= 120_000
            or not 0 <= snapshot.ticker_ts-known < M15
            or risk <= 0 or not breakdown_intact(zone,snapshot,tick)):
        return None
    rows = closed(snapshot,'15m')
    if not rows or rows[-1].ts+M15 != known:
        return None
    distance = min(.20*risk, max(.10,min(.15,.30*zone['wave_proof']['breakdown_atr'])))
    if not tick/2 <= entry-quote <= distance+1e-8 or entry-rows[-1].close > distance+1e-8:
        return None
    return {'pattern':'M15_BREAKDOWN_MOMENTUM','known_ts':known,'limit_entry':entry,
            'initial_risk':risk,'deviation_r':(entry-quote)/risk}
