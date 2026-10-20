"""Exhaust actual detector branches and adapt breakout signals to resting limits.

Original detector bodies stay frozen. No pattern condition, market candle or
risk filter is fabricated: branches use subsets of the same known references.
"""
from __future__ import annotations
import copy
import execution_atlas

SINGLE_SIDE={'SWEEP_RECLAIM':'LONG','CAPITULATION_RECOVERY':'LONG',
 'LIQUIDITY_SWEEP_REVERSAL_SHORT':'SHORT','BUYER_EXHAUSTION_SHORT':'SHORT',
 'FAILED_BREAKOUT_SHORT':'SHORT','MSS_REVERSAL_SHORT':'SHORT','OR_FAILURE_2_SHORT':'SHORT'}
BREAK_RETEST={'BUYER_EXHAUSTION_SHORT','OPENING_RANGE_BREAKOUT'}


def capitulation_recovery(context,core):
    """Volume/range locates capitulation; a closed body reclaim proves recovery.

    The original detector accepted a close anywhere above the cluster LOW,
    including an ongoing liquidation candle. Require a subsequent closed
    recovery through the last cluster body's midpoint, or a rejecting bullish
    capitulation bar that itself closes in its upper half.
    """
    rows=[c for c in context.get('candles',{}).get('3m',[]) if c.confirmed][-18:]
    volumes=[c.volume for c in rows if c.volume>0]
    result={'confirmed':False,'source_timeframe':context.get('formation_micro_timeframe',context.get('micro_timeframe','3m'))}
    if len(rows)<18 or len(volumes)<8:return result
    average=sum(volumes)/len(volumes);vol=context.get('atr3',0)
    clusters=[c for c in rows if c.volume>=1.9*average and c.high-c.low>=1.6*vol]
    if len(clusters)<2:return result
    last=rows[-1];last_cluster=clusters[-1]
    line=(last_cluster.open+last_cluster.close)/2
    after=last.ts>last_cluster.ts
    self_rejection=(last.close>last.open and last.close>(last.high+last.low)/2
                    and last.open-last.low>=abs(last.close-last.open))
    confirmed=last.close>line and (after or self_rejection)
    step=execution_atlas.TF[result['source_timeframe']]
    return {**result,'confirmed':confirmed,'recovery_line':line,
            'cluster_known_ts':last_cluster.ts+step,'recovery_known_ts':last.ts+step,
            'cluster_low':min(c.low for c in clusters)}


def recovery_failed(plan,snapshot,cfg,engine):
    """A new displacement can invalidate only an UNFILLED recovery pattern."""
    proof=plan.get('pattern_proof',{})
    if plan.get('setup')!='CAPITULATION_RECOVERY' or not proof.get('confirmed'):return False
    tf=proof['source_timeframe'];step=execution_atlas.TF[tf]
    rows=[c for c in snapshot.candles.get(tf,[]) if c.confirmed and c.ts+step<=snapshot.now]
    line=proof['recovery_line'];vol=max(engine.atr(rows),cfg.tick)
    return any(c.ts+step>plan['placed_ts'] and previous.close>=line
               and c.close<line-cfg.tick and c.open-c.close>=.65*vol
               for previous,c in zip(rows,rows[1:]))


def _context(ctx,session=None,liquidity=None,vwap=None):
    out=dict(ctx)
    if session is not None:out['session']=session
    if liquidity is not None:out['liquidity']=liquidity
    if vwap is not None:out['vwap']=vwap
    return out


def candidates(detector,setup,ctx,core):
    raw=detector(ctx)
    if raw is not None and raw.setup_type=='CAPITULATION_RECOVERY':
        proof=capitulation_recovery(ctx,core)
        if proof['confirmed']:raw.evidence['recovery_proof']=proof
        else:raw=None
    anchors=[raw] if raw is not None else []
    if setup=='BUYER_EXHAUSTION_SHORT' and ctx.get('swing_structure15'):
        # Exhaustion describes the preceding uptrend, even when its protected
        # low has just broken. Preserve that original detector's native route.
        anchors.append(detector({**ctx,'structure15':ctx['swing_structure15']}))
    session=dict(ctx.get('session') or {})
    if setup in ('BREAKOUT_RETEST','FAILED_AUCTION_REJECTION'):
        frozen=execution_atlas.frozen_week(ctx)
        if frozen:
            refs={**session,**{k:frozen[k] for k in ('week_high','week_low')}}
            a=detector(_context(ctx,session=refs))
            if a is not None:
                a.evidence.update(reference_kind='WEEK_BEFORE_EVENT_WINDOW',
                                  reference_as_of_ts=frozen['reference_as_of_ts'])
                anchors.append(a)
            # A first-return daily branch must not hide the independent week branch.
            for key in ('week_high','week_low'):
                refs={**session,**dict.fromkeys(('prev_day_high','prev_day_low','week_high','week_low'),0)}
                refs[key]=frozen[key]
                a=detector(_context(ctx,session=refs,vwap=0 if setup=='FAILED_AUCTION_REJECTION' else ctx.get('vwap',0)))
                if a is not None:
                    a.evidence.update(reference_kind='WEEK_BEFORE_EVENT_WINDOW',reference_as_of_ts=frozen['reference_as_of_ts'])
                    anchors.append(a)
    if setup=='PULLBACK_CONTINUATION' and raw is not None:
        # Original detector chooses only the strongest level, even if far away.
        # Enumerate its actual EMA/value/swing/zone references with the SAME
        # direction, regime prerequisite and structural invalidation formula.
        sign=core.side_sign(raw.side);price=ctx['price'];vol=ctx['atr15']
        levels=[(name,value,58.) for name,value in core._ema_ladder(ctx['candles']['15m']).items()
                if name in ('ema20','ema50')]
        levels.append(('VWAP',ctx.get('vwap',0),58.))
        structure=ctx.get('structure15',{})
        levels.extend(('SWING',x,55.) for x in structure.get('swing_lows' if sign==1 else 'swing_highs',[])[-2:])
        for name in ('order_blocks','fvg'):
            for zone in ctx.get('zones',{}).get(name,[]):
                if zone.side!=raw.side or zone.mitigated:continue
                # Proximal edge is a real boundary, not an invented midpoint.
                level=zone.high if sign==1 else zone.low
                levels.append((zone.kind,level,(56+8*zone.strength) if name=='order_blocks' else (54+6*zone.strength)))
        for name,level,score in levels:
            if level<=0 or sign*(level-price)>.10*vol:continue
            anchors.append(core.make_anchor(setup,raw.side,level,
                level-sign*max(.9*vol,core.min_stop_distance(price)),raw.kind,
                'Лімітний відкат до '+name,ctx,score=min(100,score),
                evidence={'pullback_target':name,'enumerated_reference':True,'trend_direction':raw.side}))
    if setup=='FRESH_BASE_CONTINUATION':
        rows=ctx.get('candles',{}).get('15m',[])
        # The displacement candle must not enlarge its own preceding base.
        if len(rows)>=20:
            base=core.detect_consolidation(rows[:-1],ctx['atr15'])
            altered={**ctx,'zones':{**ctx.get('zones',{}),'consolidation':base}}
            a=detector(altered)
            if a is not None:
                a.evidence['base_known_before_displacement']=True
                anchors.append(a)
    if setup=='MOMENTUM_NO_PULLBACK_CONTINUATION':
        for zone in ctx.get('zones',{}).get('fvg',[]):
            if zone.mitigated:continue
            altered={**ctx,'zones':{**ctx.get('zones',{}),'fvg':[zone]}}
            anchors.append(detector(altered))
    if setup=='RANGE_EDGE_REVERSAL':
        base=ctx.get('zones',{}).get('consolidation',{});vol=ctx.get('atr15',0)
        low=base.get('low') or session.get('day_low',0);high=base.get('high') or session.get('day_high',0)
        if ctx.get('regime') in ('RANGE','TRANSITION') and len(ctx['candles']['15m'])>=30 and low>0 and high-low>=2*vol:
            for side,level,sign in (('LONG',low,1),('SHORT',high,-1)):
                # Form the edge scenario before the final approach, rather than
                # waiting until <=0.6 ATR. The shared planner still checks reach.
                if 0<=sign*(ctx['price']-level)<=3*vol:
                    anchors.append(core.make_anchor(setup,side,level,level-sign*.55*vol,
                        core.AnchorKind.RANGE_EDGE.value,'Заздалегідь визначена межа діапазону',ctx,
                        score=52+12*core.clamp((2.2-base.get('compression',2.2))/2.2,0.,1.),
                        evidence={'range_low':low,'range_high':high,'planning_policy':'PREMAPPED_RANGE_EDGE'}))
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
        a.evidence.setdefault('formation_timeframe',ctx.get('formation_timeframe',ctx.get('analysis_timeframe','15m')))
        a.evidence.setdefault('formation_micro_timeframe',ctx.get('formation_micro_timeframe',ctx.get('micro_timeframe','3m')))
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
              and ((initial and c in rows[-6:]) or c.ts+step>max(anchor.created_ts,observed_after))]
    if not eligible or sign*(snapshot.price-anchor.level)<=0:return False
    # The new limit only exists from the decision that observes this break.
    event=eligible[-1].ts+step
    lifetime=anchor.expires_ts-anchor.created_ts
    anchor.evidence.update(activation_phase='LIMIT_RETEST',episode_ts=event,
                           break_known_ts=event,break_observed_ts=snapshot.now,
                           original_created_ts=anchor.created_ts)
    anchor.created_ts=snapshot.now;anchor.expires_ts=snapshot.now+lifetime
    return True
