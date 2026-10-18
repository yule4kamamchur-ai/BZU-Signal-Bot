"""Causal target freshness and distinct setup episodes, using closed OHLC only.

Liquidity pools are destinations, not automatic reversal barriers. A fresh,
strong opposing displacement zone can still block the path to a distant TP.
"""
from __future__ import annotations

TF={'3m':180_000,'15m':900_000,'1H':3_600_000,'4H':14_400_000}
MICRO={'15m':'3m','1H':'15m','4H':'1H'}
SESSION_SETUPS={'OPENING_RANGE_BREAKOUT','FAILED_OPENING_RANGE_BREAKOUT',
                'OR_FAILURE_2_SHORT','TIME_OF_DAY_ADAPTIVE','DAILY_WEEKLY_OPEN_RECLAIM'}


def closed(snapshot,tf):
    return [c for c in snapshot.candles.get(tf,[]) if c.confirmed and c.ts+TF[tf]<=snapshot.now]


def session_epoch(anchor,context):
    if anchor.setup_type not in SESSION_SETUPS:
        return 0
    field='week_ts' if anchor.evidence.get('reference')=='week_open' else 'day_ts'
    return int(context.get('session',{}).get(field,0))


def renewal_event(anchor,previous,context,snapshot,cfg,after=None):
    """No timer-only renewal: require a new session reference or rejection.

    Return the availability time of actual new information. Orders always
    start at the current decision time, never on the historical signal bar.
    """
    tf=anchor.evidence.get('setup_timeframe','15m');micro=MICRO[tf]
    boundary=max(previous['expires_ts'],previous['created_ts']) if after is None else after
    if anchor.setup_type in SESSION_SETUPS:
        epoch=session_epoch(anchor,context)
        if previous['created_ts']<epoch<=snapshot.now:
            return {'known_ts':epoch,'kind':'NEW_SESSION_REFERENCE'}
    side=cfg.core.side_sign(anchor.side);entry=anchor.level
    noise=max(2*cfg.tick,.10*context.get('atr3',0))
    rows=closed(snapshot,micro)[-24:]
    events=[]
    for c in rows:
        known=c.ts+TF[micro]
        if known<=boundary or snapshot.now-known>2*TF[micro]:
            continue
        touched=c.low<=entry+cfg.tick if side==1 else c.high>=entry-cfg.tick
        rejects=side*(c.close-entry)>=noise and side*(c.close-c.open)>0
        intact=side*(c.close-anchor.invalidation)>0
        if touched and rejects and intact:
            events.append({'known_ts':known,'kind':'NEW_CLOSED_REJECTION'})
    return events[-1] if events else None


def structural_targets(context,snapshot,side,entry,cfg,engine):
    """Discard spent pivots/zones and retain source TF and availability.

    A pivot's liquidity is consumed by a later wick beyond it, one tick away.
    A displacement zone is consumed by a later full traversal. Only opposing
    order blocks of >=1.10 native ATR are hard barriers; incidental pivots
    remain intermediate liquidity rather than imposing an artificial TP1.
    """
    sign=cfg.core.side_sign(side);native=context.get('analysis_timeframe','15m')
    targets=[]
    for tf in dict.fromkeys((native,'1H','4H')):
        rows=closed(snapshot,tf)
        if not rows:
            continue
        for point in engine.pivots(rows[-60:],2):
            if point['kind']!=('HIGH' if sign==1 else 'LOW'):
                continue
            level=point['level']
            later=[c for c in rows if c.ts>point['ts']]
            consumed=any(c.high>=level+cfg.tick if sign==1 else c.low<=level-cfg.tick for c in later)
            if not consumed and sign*(level-entry)>2*cfg.tick:
                targets.append({'kind':tf+'_LIQUIDITY','level':level,
                                'timeframe':tf,'known_ts':point['known_ts']+TF[tf],
                                'hard_barrier':False,'freshness':'UNSWEPT'})
        volatility=max(engine.atr(rows),cfg.tick)
        gaps=cfg.core.detect_fvg(rows,volatility,limit=64)
        # The original FVG helper discards any gap crossed by a wick. Restore
        # its geometry for target-risk analysis; acceptance is decided below.
        known_gaps={(z.kind,z.created_ts,z.low,z.high) for z in gaps}
        for first,middle,last in zip(rows,rows[1:],rows[2:]):
            for kind,sd,low,high in (('FVG_BULL','LONG',first.high,last.low),
                                     ('FVG_BEAR','SHORT',last.high,first.low)):
                if high-low<.75*volatility:
                    continue
                key=(kind,middle.ts,low,high)
                if key not in known_gaps:
                    gaps.append(cfg.core.Zone(kind,sd,low,high,middle.ts,tf,min(4.,(high-low)/volatility)))
                    known_gaps.add(key)
        for zone in cfg.core.detect_order_blocks(rows,volatility,limit=64)+gaps:
            if cfg.core.side_sign(zone.side)!=-sign or zone.mitigated:
                continue
            # OB is known after the next displacement candle; FVG after its
            # third candle. Exclude formation candles from mitigation checks.
            known=zone.created_ts+2*TF[tf]
            if known>snapshot.now:
                continue
            life=zone_lifecycle(zone,rows,tf,snapshot.now,volatility,cfg.tick)
            if life['state']=='BROKEN_ACCEPTED':
                continue
            level=zone.low if sign==1 else zone.high
            inside=zone.low<=entry<=zone.high
            if inside:
                level=zone.high if sign==1 else zone.low
            if sign*(level-entry)>2*cfg.tick:
                targets.append({'kind':zone.kind,'level':level,'timeframe':tf,
                                'known_ts':known,'strength':zone.strength,
                                'hard_barrier':life['hard_barrier'],
                                'freshness':life['state'],'role':life['role'],
                                'entry_inside_zone':inside,
                                'zone_lifecycle':life})
    # Session reference levels remain valid targets, but a current day's
    # evolving extrema must not redefine the original fixed entry.
    session=context.get('session',{})
    names=('day_high','week_high','prev_day_high') if sign==1 else ('day_low','week_low','prev_day_low')
    for name in names:
        level=float(session.get(name,0) or 0)
        if level>0 and sign*(level-entry)>2*cfg.tick:
            targets.append({'kind':name.upper(),'level':level,'timeframe':native,
                            'hard_barrier':False,'freshness':'SESSION_REFERENCE'})
    unique={}
    for target in targets:
        target['distance']=sign*(target['level']-entry)
        key=round(target['level']/cfg.tick)
        previous=unique.get(key)
        if previous is None or (target['hard_barrier'],TF.get(target['timeframe'],0))>(previous['hard_barrier'],TF.get(previous['timeframe'],0)):
            unique[key]=target
    return sorted(unique.values(),key=lambda t:t['distance'])


def zone_lifecycle(zone,rows,tf,now,volatility,tick):
    """A rejection wick does not erase supply/demand. Acceptance does.

    Age and repeat tests describe freshness; age alone cannot dissolve a
    strong barrier. Two closes outside, or a full displacement close outside,
    establish a break using information available at this decision only.
    """
    known=zone.created_ts+2*TF[tf]
    later=[c for c in rows if c.confirmed and known<=c.ts and c.ts+TF[tf]<=now]
    support=1 if zone.side=='LONG' else -1
    far=zone.low if support==1 else zone.high
    beyond=lambda c:support*(c.close-far)<-tick
    consecutive=0;broken=False;touches=0;inside=False;penetration=0.
    width=max(zone.high-zone.low,tick)
    for c in later:
        touched=c.low<=zone.high and c.high>=zone.low
        if touched and not inside:
            touches+=1
        inside=touched
        if touched:
            depth=(zone.high-c.low)/width if support==1 else (c.high-zone.low)/width
            penetration=max(penetration,max(0.,min(1.,depth)))
        consecutive=consecutive+1 if beyond(c) else 0
        displacement=beyond(c) and support*(c.close-c.open)<-1.1*volatility
        if consecutive>=2 or displacement:
            broken=True
    age=max(0,(now-known)//TF[tf])
    is_ob=zone.kind.endswith('_OB')
    formation=[c for c in rows if zone.created_ts<=c.ts<known and c.ts+TF[tf]<=now and c.confirmed]
    displacement=max((abs(c.close-c.open)/max(volatility,tick) for c in formation),default=0.)
    strong=(is_ob and zone.strength>=1.10) or (not is_ob and zone.strength>=.75 and displacement>=1.10)
    state='BROKEN_ACCEPTED' if broken else 'FRESH' if touches==0 and age<=24 else 'TESTED' if touches else 'AGED_UNTESTED'
    role='STRONG_OPPOSING_BARRIER' if strong and not broken else 'STRUCTURAL_DESTINATION'
    return {'known_ts':known,'age_bars':age,'touch_episodes':touches,
            'penetration_fraction':round(penetration,4),'state':state,'role':role,
            'hard_barrier':strong and not broken,'formation_strength':zone.strength,
            'formation_displacement_atr':round(displacement,4)}


def choose_target(targets,entry,risk,buffer,side,cfg):
    """Pick the nearest sufficiently distant REAL destination.

    Legacy/untyped targets remain hard barriers. Skip only explicitly typed
    intermediate liquidity; never pretend it disappears or invent a far TP.
    """
    required=max(cfg.min_target_pct*entry,cfg.min_net_rr*risk)
    eligible=[t for t in targets if t['distance']-buffer+1e-9>=required]
    if not eligible:
        return (targets[0] if targets else None),[],None
    chosen=eligible[0]
    desired=min(chosen['distance']-buffer,cfg.max_target_r*risk)
    intermediate=[t for t in targets if t['distance']-buffer<desired-1e-9]
    barrier=next((t for t in intermediate if t.get('hard_barrier',True)),None)
    return chosen,intermediate,barrier
