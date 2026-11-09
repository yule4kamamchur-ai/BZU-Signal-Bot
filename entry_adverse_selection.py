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
