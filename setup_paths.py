"""Exhaust actual detector branches and adapt breakout signals to resting limits.

Original detector bodies stay frozen. No pattern condition, market candle or
risk filter is fabricated: branches use subsets of the same known references.
"""
from __future__ import annotations
import copy

SINGLE_SIDE={'SWEEP_RECLAIM':'LONG','CAPITULATION_RECOVERY':'LONG',
 'LIQUIDITY_SWEEP_REVERSAL_SHORT':'SHORT','BUYER_EXHAUSTION_SHORT':'SHORT',
 'FAILED_BREAKOUT_SHORT':'SHORT','MSS_REVERSAL_SHORT':'SHORT','OR_FAILURE_2_SHORT':'SHORT'}
BREAK_RETEST={'BUYER_EXHAUSTION_SHORT','OPENING_RANGE_BREAKOUT'}


def _context(ctx,session=None,liquidity=None,vwap=None):
    out=dict(ctx)
    if session is not None:out['session']=session
    if liquidity is not None:out['liquidity']=liquidity
    if vwap is not None:out['vwap']=vwap
    return out


def candidates(detector,setup,ctx,core):
    raw=detector(ctx)
    anchors=[raw] if raw is not None else []
    session=dict(ctx.get('session') or {})
    if setup=='LIQUIDITY_LADDER':
        for key in ('equal_lows','equal_highs'):
            liq={**ctx.get('liquidity',{}),'equal_lows':[],'equal_highs':[]}
            liq[key]=ctx.get('liquidity',{}).get(key,[])
            anchors.append(detector(_context(ctx,liquidity=liq)))
    elif setup in ('BREAKOUT_RETEST','FAILED_AUCTION_REJECTION','DAILY_WEEKLY_OPEN_RECLAIM'):
        keys={'BREAKOUT_RETEST':('prev_day_high','week_high','prev_day_low','week_low'),
              'FAILED_AUCTION_REJECTION':('prev_day_high','prev_day_low','week_high','week_low'),
              'DAILY_WEEKLY_OPEN_RECLAIM':('day_open','week_open')}[setup]
        for key in keys:
            refs={**session,**dict.fromkeys(keys,0)};refs[key]=session.get(key,0)
            anchors.append(detector(_context(ctx,session=refs,vwap=0 if setup=='FAILED_AUCTION_REJECTION' else ctx.get('vwap',0))))
        if setup=='FAILED_AUCTION_REJECTION':
            refs={**session,**dict.fromkeys(keys,0)}
            anchors.append(detector(_context(ctx,session=refs)))
    elif setup=='FAILED_OPENING_RANGE_BREAKOUT':
        # The original returns the high refusal first. Evaluate its independent
        # low-refusal branch as well, against exactly the same actual candles.
        rows=core._confirmed(ctx.get('candles',{}).get('15m',[]),6)
        low=core.safe_float(session.get('opening_range_low'))
        high=core.safe_float(session.get('opening_range_high'))
        if session.get('opening_range_complete') and low>0 and high>0 and len(ctx.get('candles',{}).get('15m',[]))>=8:
            pierced=[c for c in rows if c.low<low]
            if pierced and all(c.close>low for c in pierced):
                extreme=pierced[-1].low
                anchors.append(core.make_anchor(setup,'LONG',low,extreme-.2*ctx['atr15'],
                    core.AnchorKind.RANGE_EDGE.value,'Відмова від OR-low; лімітний ретест',ctx,score=58,
                    evidence={'or_level':low,'pierce_extreme':extreme,'pierced_bars':len(pierced),'refused_bars':len(pierced)}))
    elif setup=='OPENING_RANGE_BREAKOUT':
        low=core.safe_float(session.get('opening_range_low'));high=core.safe_float(session.get('opening_range_high'))
        if session.get('opening_range_complete') and low>0 and high>0 and ctx.get('price',0)>0 and high-low>=.5*ctx['atr15']:
            for side,level,sign in (('LONG',high,1),('SHORT',low,-1)):
                anchors.append(core.make_anchor(setup,side,level,level-sign*max(.75*ctx['atr15'],core.min_stop_distance(ctx['price'])),
                    core.AnchorKind.RANGE_EDGE.value,'Пробій OR, потім лімітний ретест',ctx,score=53,
                    evidence={'or_high':high,'or_low':low,'or_width_atr':(high-low)/ctx['atr15']}))
    elif setup=='TIME_OF_DAY_ADAPTIVE':
        name=str(ctx.get('session_name','')).upper();low=core.safe_float(session.get('asia_low'));high=core.safe_float(session.get('asia_high'))
        if name in {'LONDON_OPEN','LONDON','NY_OVERLAP','NY_PM'} and session.get('asia_complete') and high-low>=.8*ctx['atr15'] and low>0:
            for side,level,sign in (('LONG',low,1),('SHORT',high,-1)):
                if abs(ctx['price']-level)<=.75*ctx['atr15']:
                    anchors.append(core.make_anchor(setup,side,level,level-sign*max(.65*ctx['atr15'],core.min_stop_distance(ctx['price'])),
                        core.AnchorKind.RANGE_EDGE.value,'Лімітний ретест межі азійського діапазону',ctx,score=52,
                        evidence={'session':name,'asia_low':low,'asia_high':high},ttl_minutes=max(60,core.ANCHOR_MAX_AGE_MIN//2)))
    unique={}
    for anchor in anchors:
        if anchor is None:continue
        a=copy.deepcopy(anchor)
        if setup=='RANGE_EDGE_REVERSAL' and a.side=='SHORT':
            compression=core.safe_float(ctx.get('zones',{}).get('consolidation',{}).get('compression'),2.2)
            a.score=round(52+12*core.clamp((2.2-compression)/2.2,0.,1.),2)
            a.evidence['symmetric_range_score']=True
        if setup in BREAK_RETEST:
            a.evidence.update(activation_policy='CLOSED_BREAK_THEN_LIMIT_RETEST',activation_phase='WAIT_BREAK')
        key=(a.side,round(a.level,8),round(a.invalidation,8))
        unique.setdefault(key,a)
    return list(unique.values())


def activate(anchor,context,snapshot,cfg,initial=False,observed_after=0):
    """A confirmed break creates a NEW retest observation, never a past fill."""
    if anchor.evidence.get('activation_phase')!='WAIT_BREAK':return False
    tf=anchor.evidence.get('setup_timeframe',context.get('analysis_timeframe','15m'))
    micro={'15m':'3m','1H':'15m','4H':'1H'}[tf]
    step={'3m':180000,'15m':900000,'1H':3600000,'4H':14400000}[micro]
    rows=[c for c in snapshot.candles.get(micro,[]) if c.confirmed and c.ts+step<=snapshot.now]
    sign=cfg.core.side_sign(anchor.side)
    eligible=[c for previous,c in zip(rows,rows[1:])
              if sign*(previous.close-anchor.level)<=0 and sign*(c.close-anchor.level)>cfg.tick
              and ((initial and c is rows[-1]) or c.ts+step>max(anchor.created_ts,observed_after))]
    if not eligible or sign*(snapshot.price-anchor.level)<=0:return False
    # The new limit only exists from the decision that observes this break.
    event=eligible[-1].ts+step
    lifetime=anchor.expires_ts-anchor.created_ts
    anchor.evidence.update(activation_phase='LIMIT_RETEST',episode_ts=event,
                           break_known_ts=event,break_observed_ts=snapshot.now,
                           original_created_ts=anchor.created_ts)
    anchor.created_ts=snapshot.now;anchor.expires_ts=snapshot.now+lifetime
    return True
