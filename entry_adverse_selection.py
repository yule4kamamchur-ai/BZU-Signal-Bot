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


def approach(snapshot, atr15, side=0, zone=None, tick=.001):
    """Brake only an adverse impulse; release on a later witnessed reclaim.

    A favorable expansion must not block a continuation limit. An open candle
    cannot release the brake. The shock candle itself is never its own proof
    of a subsequent reaction. An earlier impulse followed by a separate
    closed 5m reaction inside that same 15m bar is valid ordered evidence.
    """
    rows=sorted((c for c in snapshot.candles.get('15m', [])
          if c.confirmed and c.ts+900_000 <= snapshot.now),key=lambda c:c.ts)
    ratio=max(((c.high-c.low)/atr15 for c in rows[-2:]),default=0.) if atr15>0 else 0.
    base={'fast':False,'range_atr15':ratio,'blocked':False,
          'action':'PRECISION_ENTRY','veto_enabled':True,'shock_known_ts':None}
    if side not in (-1,1) or atr15<=0:
        return base
    shocks=[]
    for i in range(max(0,len(rows)-8),len(rows)):
        c=rows[i];body=-side*(c.close-c.open)
        pair=(rows[i-1] if i and c.ts-rows[i-1].ts==900_000 else None)
        combined=body+(max(0.,-side*(pair.close-pair.open)) if pair else 0.)
        wick=c.close-c.low if side==1 else c.high-c.close
        if body>0 and max(body,combined)>1.8*atr15 and wick<=.10*max(c.high-c.low,tick):
            shocks.append(c)
    forming_brake = None
    if zone:
        import setup_scanner as scanner
        micro_rows = scanner.closed_five_minutes(snapshot)
        quarter = snapshot.now // 900_000 * 900_000
        for start in (quarter, quarter - 900_000):
            sample = [c for c in micro_rows if start <= c.ts < start + 900_000]
            expected = min(3, (snapshot.now - start) // 300_000)
            if expected < 1 or [c.ts for c in sample] != [start + i*300_000 for i in range(expected)]:
                continue
            if start == quarter and any(
                    -side*(bar.close-sample[0].open) > 1.8*atr15
                    and bar.low <= zone['high'] and bar.high >= zone['low']
                    and bar.ts + 300_000 > zone.get('known_ts',0) for bar in sample):
                forming_brake = sample[-1].ts + 300_000
            if expected < 2:
                continue
            closed_quarter = next((c for c in reversed(rows) if c.ts == start), None)
            reaction = (intra_bar_reaction(snapshot, closed_quarter, side, zone, atr15, tick)
                        if closed_quarter is not None else
                        _micro_reaction(snapshot, sample, side, zone, atr15, tick))
            if reaction and (not forming_brake or reaction['known_ts'] >= forming_brake) and (
                    not shocks or shocks[-1].ts <= start):
                return {**base, 'fast':True,'blocked':False,'action':'CONFIRMED_RETEST',
                        'shock_known_ts':sample[-1].ts, 'confirmation_timeframe':'5m',
                        'confirmation_known_ts':reaction['known_ts'], 'confirmation_pattern':reaction['pattern']}
    if forming_brake:
        return {**base, 'fast':True, 'blocked':True, 'action':'CONFIRMATION_REQUIRED',
                'shock_known_ts':forming_brake}
    if not shocks:
        return base
    shock=shocks[-1];known=shock.ts+900_000
    latest = rows[-1]
    if latest.ts >= known:
        span = max(latest.high - latest.low, tick)
        favorable_close = (latest.close - latest.low if side == 1
                           else latest.high - latest.close)
        boundary = (zone['high'] if side == 1 else zone['low']) if zone else latest.open
        if (side * (latest.close - latest.open) >= .6 * atr15
                and favorable_close >= 2 * span / 3 and side * (latest.close - boundary) > 0):
            return {**base, 'action': 'FAVORABLE_DISPLACEMENT'}
    confirmation=None
    confirmation_tf=None
    if zone:
        for c in rows:
            if c.ts<known or c.low>zone['high'] or c.high<zone['low']:
                continue
            wick=min(c.open,c.close)-c.low if side==1 else c.high-max(c.open,c.close)
            boundary=zone['high'] if side==1 else zone['low']
            reclaim=side*(c.close-boundary)>0 and side*(c.close-c.open)>0
            pin=wick>=.5*max(c.high-c.low,tick) and side*(c.close-(zone['low']+zone['high'])/2)>0
            if reclaim or pin:
                confirmation=c.ts+900_000
                confirmation_tf='15m'
        if confirmation and any(c.ts>=confirmation and
                side*(c.close-(zone['low'] if side==1 else zone['high']))<0 for c in rows):
            confirmation=None
    return {**base,'fast':True,'blocked':confirmation is None,
            'shock_known_ts':known,'confirmation_known_ts':confirmation,
            'confirmation_timeframe':confirmation_tf,
            'action':'CONFIRMED_RETEST' if confirmation else 'CONFIRMATION_REQUIRED'}


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
