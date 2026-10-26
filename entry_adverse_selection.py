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
