"""Checklist v12.21 policy. Closed evidence, forward entries, honest R accounting.

This is a signal ledger, not an authenticated exchange execution adapter.
Minute OHLC cannot establish tick order: ambiguous bars always use stop first.
"""
from __future__ import annotations

import copy
import math

import directional_policy as direction
import entry_adverse_selection as adverse
import ict_methodology as ict
import limit_structure as structure
import research_zone_policy_1220 as zones
import setup_scanner as scanner

POLICY = 'BZU_PRECISION_V12_22'
LEGACY_POLICY = 'BZU_CHECKLIST_V12_21'
EXIT = 'BE_1R_TP1_50_AT_1_4R_TP2_50'
MINUTE = 60_000


def is_new(plan):
    return plan.get('policy') in (POLICY,LEGACY_POLICY)


def is_precision(plan):
    return plan.get('policy') == POLICY


def session_gate(ts):
    """Requested risk windows, not an exchange trading-hours calendar.

    09:30 New York is an operator-selected US volatility window. Brent/OKX
    hours are different; DST is resolved by the IANA New York timezone.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    utc=datetime.fromtimestamp(ts/1000,timezone.utc)
    minute=utc.hour*60+utc.minute
    night=minute>=21*60+30 or minute<3*60
    ny=utc.astimezone(ZoneInfo('America/New_York'))
    opening=ny.weekday()<5 and 9*60+30<=ny.hour*60+ny.minute<9*60+45
    reason='OIL_OVERNIGHT_BLACKOUT' if night else 'US_OPEN_CONFIRMATION_REQUIRED' if opening else 'SESSION_ALLOWED'
    return {'allowed':not (night or opening),'cancel_pending':night or opening,
            'reason':reason,'as_of_ts':ts}


def zone_for(anchor, snapshot, cfg, engine):
    """Use a witnessed zone, never invent a wide band around an entry quote."""
    evidence = anchor.evidence
    explicit = evidence.get('entry_zone')
    if isinstance(explicit, dict):
        low, high = explicit.get('low', 0), explicit.get('high', 0)
        if 0 < low < high and explicit.get('known_ts', snapshot.now) <= snapshot.now:
            return copy.deepcopy(explicit)
    side = cfg.core.side_sign(anchor.side)
    choices = []
    tf = evidence.get('formation_timeframe', evidence.get('setup_timeframe', '15m'))
    # Micro events keep their real 3m zone; native detectors keep their native TF.
    if evidence.get('source_strategy', '').startswith('MICRO_'):
        tf = '3m'
    rows = structure.closed(snapshot, tf)
    a = max(engine.atr(rows), cfg.tick)
    cache=vars(snapshot).setdefault('_zone_cache',{})
    key=(tf,snapshot.now)
    if key not in cache:
        cache[key]=cfg.core.detect_order_blocks(rows,a,limit=64)+cfg.core.detect_fvg(rows,a,limit=64)
    for zone in cache[key]:
        if cfg.core.side_sign(zone.side) != side or zone.high <= zone.low:
            continue
        known = zone.created_ts + 2 * engine.TF[tf]
        if known > snapshot.now:
            continue
        gap = max(zone.low-anchor.level, anchor.level-zone.high, 0)
        if gap > max(.5*a, 3*cfg.tick):
            continue
        choices.append((gap, -known, {'low':zone.low, 'high':zone.high,
            'known_ts':known, 'kind':zone.kind, 'timeframe':tf}))
    if choices:
        return min(choices, key=lambda x:x[:2])[2]
    # A level-only setup remains scannable, but cannot earn the 40 OB/FVG points.
    return {'low':anchor.level, 'high':anchor.level, 'known_ts':anchor.created_ts,
            'kind':'STRUCTURAL_LEVEL', 'timeframe':tf}


def technical_geometry(low, high, swing, side, a15, cfg, engine, deep=False):
    """One risk-bearing entry. Never move a structural stop toward the price."""
    narrow=high-low<=.35*a15
    first=(high+.02 if side==1 else low-.02) if narrow and not deep else (low+high)/2
    first=engine.round_tick(first,cfg.tick,up=side==1)
    stop = engine.round_tick(swing-side*.20*a15, cfg.tick, up=side == -1)
    stop = engine.round_tick(first-side*max(side*(first-stop),cfg.min_stop_absolute),
                             cfg.tick,up=side==-1)
    if side*(first-stop) > cfg.max_stop_absolute+1e-9:
        # Deepen at most to equilibrium; a still-expensive setup is rejected.
        first=engine.round_tick((low+high)/2,cfg.tick,up=side==1)
    risk = side*(first-stop)
    legs = [{'name':'L1', 'price':first, 'fraction':1., 'status':'PENDING'}]
    return first, stop, risk, legs


def plan(anchor, context, snapshot, cfg, stats, engine, diagnostics=None):
    diag = diagnostics if diagnostics is not None else {}
    diag['entry_checks'] = scanner.entry_checks()
    def mark(key, passed):
        next(c for c in diag['entry_checks'] if c['key'] == key)['met'] = bool(passed)
    side = cfg.core.side_sign(anchor.side)
    a15 = engine.atr(structure.closed(snapshot, '15m'))
    if side not in (-1, 1) or a15 <= 0:
        return None, 'INVALID_DIRECTION_OR_VOLATILITY'
    if anchor.evidence.get('activation_phase') == 'WAIT_BREAK':
        return None, 'WAIT_CONFIRMED_BREAK_FOR_LIMIT'
    if snapshot.now >= anchor.expires_ts or anchor.cooldown_until_ts > snapshot.now:
        return None, 'ANCHOR_TOO_OLD_TO_PLACE'
    session=session_gate(snapshot.now)
    diag['session_gate']=session
    if not session['allowed']:
        return None,session['reason']
    zone = zone_for(anchor, snapshot, cfg, engine)
    low, high = zone['low'], zone['high']
    origin=scanner.zone_origin(zone,snapshot,side,cfg.tick,engine)
    diag['zone_origin']=origin
    if not origin['confirmed']:
        return None,origin['reason']
    approach = adverse.approach(snapshot, a15,side,zone,cfg.tick)
    diag['approach']=approach
    if approach.get('blocked'):
        diag['status']='WATCHING'
        return None,'IMPULSE_CONFIRMATION_REQUIRED'
    tf=zone['timeframe']
    swings=[p for p in engine.pivots(structure.closed(snapshot,tf))
            if p['kind']==('LOW' if side==1 else 'HIGH')
            and p['known_ts']+engine.TF[tf]<=zone['known_ts']
            and side*((low if side==1 else high)-p['level'])>=0]
    structural=(min if side==1 else max)(anchor.invalidation,low if side==1 else high,
                                        swings[-1]['level'] if swings else anchor.invalidation)
    entry, stop, risk, legs = technical_geometry(low, high, structural,
        side, a15, cfg, engine, deep=approach['fast'])
    if side*(entry-anchor.invalidation)<=0:
        return None, 'INVALID_STRUCTURAL_FALSIFIER'
    diag.update(entry=entry, level=entry, stop=stop, risk=risk, entry_zone=zone,
                approach=approach, planning_policy=POLICY)
    mark('VALID_STOP', cfg.min_stop_absolute-1e-9 <= risk <= cfg.max_stop_absolute+1e-9)
    if risk < cfg.min_stop_absolute-1e-9 or risk > cfg.max_stop_absolute+1e-9:
        return None, 'STRUCTURAL_STOP_TOO_WIDE'
    # Intrabar High/Low, including an unfinished current minute, can invalidate
    # a stop. Open bars never establish a positive setup confirmation or fill.
    minute = scanner.audit_rows(snapshot, after=zone['known_ts'])
    # Older HTF zones may predate the fetched minute window. Their closed 3m
    # history can prove prior mitigation; it never fabricates minute precision.
    first_minute=min((c.ts for c in minute),default=snapshot.now)
    earlier=[c for c in structure.closed(snapshot,'3m')
             if zone['known_ts']<=c.ts and c.ts+180_000<=first_minute]
    minute=earlier+minute
    current = snapshot.metadata.get('current_candle_1m') or {}
    if any((c.low <= stop if side == 1 else c.high >= stop) for c in minute) or (
        current and zone['known_ts']<=current.get('ts',0)<=snapshot.now<current.get('ts',0)+MINUTE and
        (current.get('low', math.inf) <= stop if side == 1 else current.get('high', 0) >= stop)):
        return None, 'ANCHOR_INVALIDATED'
    if side*(snapshot.price-stop) <= 0:
        return None, 'ANCHOR_INVALIDATED'
    distance = abs(snapshot.price-entry)
    diag['distance_atr'] = distance/a15
    if distance > cfg.max_entry_distance_atr*a15+1e-9:
        diag['needs_new_structural_event'] = True
        return None, 'LIMIT_UNREACHABLE'
    audit = zones.first_test(zone, minute, side, entry, risk, snapshot.now)
    diag['zone_audit'] = audit
    mark('FRESH_LEVEL', not audit['mitigated'])
    if audit['mitigated']:
        return None, 'ZONE_MITIGATED'
    passive = scanner.passive_gate(entry, side, snapshot.bid, snapshot.ask, cfg.tick)['passive']
    catchup = scanner.catch_up(snapshot, entry, stop, side, zone['known_ts'], cfg.tick,zone)
    action = 'NEW_PENDING_LIMIT'
    confirmed_retest=bool(passive and audit['touched'] and approach.get('confirmation_known_ts'))
    if (audit['touched'] and not confirmed_retest) or not passive:
        if not catchup['allowed']:
            diag['catch_up'] = catchup
            return None, 'WAIT_RETEST'
        # Current quote execution is NEVER backdated to the missed historic touch.
        entry = snapshot.ask if side == 1 else snapshot.bid
        risk = side*(entry-stop)
        if not cfg.min_stop_absolute-1e-9 <= risk <= cfg.max_stop_absolute+1e-9:
            return None, 'STRUCTURAL_STOP_TOO_WIDE'
        action = 'ACTIVE_ENTRY'
        legs = [{'name':'ACTIVE', 'price':entry, 'fraction':1., 'status':'PENDING'}]
        diag.update(entry=entry, level=entry, risk=risk, catch_up=catchup)
    mark('PASSIVE_LIMIT', passive or action == 'ACTIVE_ENTRY')
    regime = direction.market_regime(snapshot, cfg.tick, engine)
    allowed, why = direction.regime_admission(side, entry, regime, snapshot, cfg.tick, engine,zone)
    mark('HTF_ALIGNMENT', allowed)
    if not allowed:
        return None, why
    targets = engine.full_targets(context, snapshot, anchor.side, entry, cfg)
    buffer = max(2*cfg.tick, .06*a15)
    eligible = [t for t in targets if side*(t['level']-entry)-buffer >= max(2.5,cfg.min_net_rr)*risk]
    chosen = min(eligible,key=lambda t:side*(t['level']-entry),default=None)
    if chosen is None:
        mark('TARGET_RR', False)
        return None, 'TARGET_RR_TOO_LOW'
    tp2 = engine.round_tick(entry+side*min(side*(chosen['level']-entry)-buffer, 3*risk),
                            cfg.tick, up=side == -1)
    tp1 = engine.round_tick(entry+side*1.4*risk, cfg.tick, up=side == 1)
    rr2 = side*(tp2-entry)/risk
    mark('TARGET_RR', rr2+1e-9 >= max(2.5,cfg.min_net_rr))
    if rr2+1e-9 < max(2.5,cfg.min_net_rr):
        return None, 'TARGET_RR_TOO_LOW'
    # Remaining models are evidence only. Their individual return values do
    # not veto the setup. All 24 named ICT confirmations are still validated.
    effective = ((snapshot.now+MINUTE-1)//MINUTE)*MINUTE
    expires = min(effective+cfg.ttl_minutes*60_000, engine.session_cutoff(snapshot.now,cfg))
    if expires <= effective:
        return None, 'SESSION_CLOSED'
    event = engine.anchor_key(anchor,cfg)
    proof = copy.deepcopy(anchor.evidence)
    p = {'id':engine.ident(event,snapshot.now), 'order_id':engine.ident(event,snapshot.now),
         'event_key':event, 'zone_key':engine.anchor_zone_key(anchor,cfg),
         'policy':POLICY, 'instrument':cfg.instrument, 'side':side,
         'setup':anchor.setup_type, 'setup_type':anchor.setup_type,
         'setup_family':cfg.core.journal_setup_family(anchor.setup_type),
         'canonical_setup_family':cfg.core.canonical_setup_family(anchor.setup_type),
         'timeframe':anchor.evidence.get('setup_timeframe','15m'),
         'setup_timeframe':anchor.evidence.get('setup_timeframe','15m'),
         'micro_timeframe':'3m', 'formation_timeframe':zone['timeframe'],
         'formation_micro_timeframe':anchor.evidence.get('formation_micro_timeframe','3m'),
         'entry_route':anchor.evidence.get('entry_route','NATIVE_PATTERN'),
         'source_strategy':anchor.evidence.get('source_strategy'),
         'planning_policy':POLICY, 'entry_policy':'CHECKLIST_CONFIRMED',
         'detector_confirmed':True, 'anchor_id':anchor.id, 'anchor_kind':anchor.kind,
         'anchor_proof':proof, 'pattern_proof':copy.deepcopy(proof.get('recovery_proof',{})),
         'anchor_known_ts':zone['known_ts'], 'zone_created_ts':zone['known_ts'],
         'entry_zone':zone, 'zone_audit':audit, 'zone_origin':origin, 'approach':approach,
         'entry':entry, 'limit_price':entry, 'entry_legs':legs,
         'stop':stop, 'initial_stop':stop, 'invalidation':structural,
         'risk':risk, 'tp':tp2, 'tp1':tp1, 'tp2':tp2, 'tp3':tp2,
         'rr1':side*(tp1-entry)/risk, 'rr2':rr2, 'rr3':rr2, 'gross_rr':rr2,
         'partials':{'TP1':.5,'TP2':.5}, 'exit_policy':EXIT,
         'breakeven_trigger_r':1.,
         'target_management':EXIT, 'tp1_decision_gate':False,
         'exit_path_barriers':[], 'full_exit_candidates':[],
         'net_rr':rr2-engine.fees_r(entry,tp2,risk,cfg),
         'costs_r':engine.fees_r(entry,tp2,risk,cfg),
         'stop_distance_pct':risk/entry*100, 'tp1_distance_pct':abs(tp1-entry)/entry*100,
         'include_commission':cfg.include_commission,
         'entry_fee_rate':engine.fee_rate(cfg,'taker' if action=='ACTIVE_ENTRY' else 'maker'),
         'exit_fee_rate':engine.fee_rate(cfg,'taker'), 'slippage_bps':cfg.slippage_bps,
         'placed_ts':snapshot.now, 'effective_ts':effective, 'expires_ts':expires,
         'expiry_reason':'SESSION_ENDED' if expires==engine.session_cutoff(snapshot.now,cfg) else 'LIMIT_TTL',
         'max_hold_minutes':cfg.max_hold_minutes, 'intraday':True,
         'time_exit_enabled':False,
         'placement_price':snapshot.price, 'target_cancel_armed':side*(snapshot.price-tp1)<0,
         'action_type':action, 'execution_source':action, 'entry_stage':'CORE',
         'preposition_window':'.15..1.20', 'preposition_distance':distance,
         'queue_execution_policy':'PASSIVE_ARM_V2', 'execution_timeframe':'1m',
         'readiness_pct':100., 'entry_checks':copy.deepcopy(diag['entry_checks']),
         'htf_1h':regime['side_1h'], 'htf_4h':regime['side_4h'],
         'market_regime':regime, 'session_liquidity':ict.session_liquidity(snapshot),
         'score':0., 'probability':None, 'probability_status':'UNVALIDATED_SCORE_NOT_WR',
         'evidence':[anchor.reason], 'candidate':{}, 'parent_keys':[],
         'status':'PENDING', 'last_bar':-1, 'version':engine.VERSION}
    return p, 'ACCEPTED'


def first_fill(plan, candle, engine, cfg, step):
    """Initialize actual filled quantity without counting an unfilled second half."""
    p = copy.deepcopy(plan)
    p.update(status='OPEN', filled_ts=candle.ts, opened_at=engine.iso(candle.ts),
        fill_time_status=f'WITHIN_{step//60_000}M_BAR', last_bar=-1,
        remaining=0., filled_fraction=0., realized_gross_r=0., realized_exit_fees_r=0.,
        realized_legs=[], mfe_r=0., mae_r=0., strong_closes=0,
        entry_version=p.get('version',engine.VERSION), entry_cost=0., risk_budget=0.)
    p['exit_deadline'] = min(candle.ts+cfg.max_hold_minutes*60_000,
                             engine.session_cutoff(candle.ts,cfg))
    fill_legs(p,candle,engine,cfg,initial=True)
    return p


def fill_legs(p, c, engine, cfg, initial=False):
    if p.get('tp1_hit'):
        return
    if not initial and c.ts >= p['expires_ts']:
        for leg in p['entry_legs']:
            if leg['status'] == 'PENDING': leg['status'] = 'EXPIRED'
        return
    previous = p.get('filled_fraction',0.)
    cost = p.get('entry_cost',0.)
    for leg in p['entry_legs']:
        touched = c.low <= leg['price'] if p['side']==1 else c.high >= leg['price']
        if p['action_type']=='ACTIVE_ENTRY' and initial:
            touched = True
        if leg['status']=='PENDING' and touched:
            leg.update(status='FILLED', filled_ts=c.ts)
            p['filled_fraction'] += leg['fraction']
            p['remaining'] += leg['fraction']
            cost += leg['fraction']*leg['price']
    if p.get('filled_fraction',0.) > previous:
        p['entry_cost'] = cost
        p['entry'] = cost/p['filled_fraction']
        p['risk'] = p['side']*(p['entry']-p['initial_stop'])
        p['risk_budget'] = p['risk']*p['filled_fraction']
        p['tp1'] = engine.round_tick(p['entry']+p['side']*p['risk']*p.get('rr1',1.),
                                    cfg.tick,up=p['side']==1 if is_precision(p) else p['side']==-1)


def breakeven_price(p,cfg,engine):
    """Cover stored entry/exit fees and configured exit slippage at the trigger."""
    sign=p['side'];entry_fee=p['entry_fee_rate'] if cfg.include_commission else 0.
    exit_fee=engine.fee_rate(cfg,'taker')
    price=p['entry']*(1+sign*entry_fee)/(1-sign*exit_fee)
    price/=1-sign*cfg.slippage_bps/10_000
    return engine.round_tick(price,cfg.tick,up=sign==1)


def close(state, p, price, ts, reason, cfg, engine, ambiguous=False):
    actual = engine.execution_config(p,cfg)
    denominator = p['risk_budget']
    remaining = p['remaining']
    gross = p.get('realized_gross_r',0.)+remaining*p['side']*(price-p['entry'])/denominator
    fees = (p['entry_cost']*p['entry_fee_rate']/denominator+
        p.get('realized_exit_fees_r',0.)+remaining*price*actual.taker_fee/denominator) if actual.include_commission else 0.
    t = {**copy.deepcopy(p), 'status':'CLOSED', 'remaining':0., 'exit':price,
         'closed_ts':ts, 'closed_at':engine.iso(ts), 'close_reason':reason,
         'gross_r':gross, 'fees_r':fees, 'net_r':gross-fees, 'pnl_r':gross-fees,
         'result':'WIN' if gross-fees>0 else 'LOSS' if gross-fees<0 else 'BREAKEVEN',
         'bot_version_at_entry':p.get('entry_version',p['version']),
         'exit_version':engine.VERSION, 'ambiguous_ohlc':ambiguous,
         'final_exit_fraction':remaining, 'stop_initial':p['initial_stop'],
         'stop_at_close':p['stop']}
    if not any(old['id']==p['id'] for old in state['trades']):
        state['trades'].append(t)
    for order in state['orders']:
        if order['id']==p['id']:
            order.update(position_status='CLOSED', closed_ts=ts, trade_net_r=t['net_r'],trade_result=t['result'])
    state['active'] = None
    engine.emit(state,'CLOSED',ts,order_id=p['id'],reason=reason,net_r=t['net_r'],ambiguous_ohlc=ambiguous)


def manage(state, c, cfg, engine, step, fill_bar=False):
    p = state.get('active')
    if not p or c.ts <= p.get('last_bar',-1):
        return
    cfg = engine.execution_config(p,cfg)
    ts, side = c.ts+step, p['side']
    if p.get('pending_stop') and c.ts >= p['pending_stop']['effective_ts']:
        p['stop'] = p.pop('pending_stop')['price']
    old_qty = p['filled_fraction']
    fill_legs(p,c,engine,cfg)
    added = p['filled_fraction'] > old_qty
    if added:
        engine.emit(state,'SECOND_LIMIT_FILLED',ts,order_id=p['id'],entry=p['entry'])
    stop_hit = c.low <= p['stop'] if side==1 else c.high >= p['stop']
    target_hit = c.high >= p['tp1'] if side==1 else c.low <= p['tp1']
    if stop_hit:
        raw = c.open if side*(c.open-p['stop'])<=0 else p['stop']
        close(state,p,raw*(1-side*cfg.slippage_bps/10_000),ts,
              'STOP_GAP' if raw==c.open else 'STOP',cfg,engine,
              ambiguous=target_hit or fill_bar or added)
        return
    # Favorable extremes in a fill bar can precede either fill. A close through
    # the target proves the favorable traversal after both entries.
    proved = lambda level: (not (fill_bar or added)) or side*(c.close-level)>=0
    if is_precision(p) and not p.get('breakeven_armed'):
        trigger=p['entry']+side*p['risk']*p.get('breakeven_trigger_r',1.)
        reached=c.high>=trigger if side==1 else c.low<=trigger
        if reached and proved(trigger):
            be=breakeven_price(p,cfg,engine)
            if side*(be-p['stop'])>0:
                p['pending_stop']={'price':be,'effective_ts':ts}
            p['breakeven_armed']=True
            engine.emit(state,'BREAKEVEN_ARMED',ts,order_id=p['id'],stop=be)
    if not p.get('tp1_hit') and target_hit and proved(p['tp1']):
        fraction=p.get('partials',{}).get('TP1',.6)
        size = fraction*p['filled_fraction']
        price = p['tp1']*(1-side*cfg.slippage_bps/10_000)
        p['realized_gross_r'] += size*side*(price-p['entry'])/p['risk_budget']
        p['realized_exit_fees_r'] += size*price*engine.fee_rate(cfg,'taker')/p['risk_budget']
        p['remaining'] -= size
        p.update(tp1_hit=True, tp1_reached_ts=ts, provisional_tp1_win=True)
        p['realized_legs'].append({'target':'TP1','price':price,'fraction':size,'ts':ts})
        # Never add the second half after taking profit from the first half.
        for leg in p['entry_legs']:
            if leg['status']=='PENDING': leg['status']='CANCELLED_AFTER_TP1'
        be = breakeven_price(p,cfg,engine) if is_precision(p) else engine.round_tick(p['entry']+side*.02,cfg.tick,up=side==1)
        if side*(be-p['stop'])>0:
            p['pending_stop'] = {'price':be,'effective_ts':ts}
        engine.emit(state,'TP1_PARTIAL',ts,order_id=p['id'],price=p['tp1'],fraction=fraction,
                    rr=p.get('rr1',1.),stop=be)
        # TP1 is a realized milestone. Aggregate WR gets ONE label only when
        # the remaining position closes, including all fees and gap losses.
    second = c.high >= p['tp2'] if side==1 else c.low <= p['tp2']
    if p.get('tp1_hit') and second and proved(p['tp2']):
        close(state,p,p['tp2']*(1-side*cfg.slippage_bps/10_000),ts,'TAKE_PROFIT',cfg,engine)
        return
    if p.get('time_exit_enabled') and ts >= p['exit_deadline']:
        close(state,p,c.close*(1-side*cfg.slippage_bps/10_000),ts,'INTRADAY_TIME_EXIT',cfg,engine)
        return
    if not fill_bar and not added:
        favorable = c.high if side==1 else c.low
        p['mfe_r'] = max(p['mfe_r'],side*(favorable-p['entry'])/p['risk'])
    p['last_bar'] = c.ts


def merge_plans(plans, snapshot, cfg, engine):
    """Merge confirmed, accepted same-side zones within half the real 15m ATR."""
    import trade_dependencies
    groups = []
    threshold = .5*engine.atr(structure.closed(snapshot,'15m'))
    for p in sorted(plans,key=lambda p:(p['side'],p['entry'])):
        group = next((g for g in groups if p['side']==g[0]['side'] and
            trade_dependencies.zone_related(p,g[-1],2*threshold) and
            all(x.get('action_type')==p.get('action_type') for x in g)),None)
        if group is None: groups.append([p])
        else: group.append(p)
    result=[]
    for group in groups:
        best = copy.deepcopy(max(group,key=lambda p:p['score']))
        best['supporting_setups'] = sorted({p['setup'] for p in group})
        best['supporting_families'] = sorted({p['canonical_setup_family'] for p in group})
        best['parent_keys'] = sorted({p['event_key'] for p in group if p['event_key']!=best['event_key']})
        best['alternative_scenarios'] = [{'id':p['id'],'setup':p['setup'],'reason':p['quality']['reason']} for p in group]
        # Correlated detections are supporting descriptions, not independent
        # evidence points. Preserve the best accepted zone's complete geometry
        # and origin witness; merging must not recreate a second entry leg.
        if len(group)>1:
            best['zone_cluster_members']=[p['id'] for p in group]
            best['cluster_geometry_status']='KEPT_BEST_VALID_ORIGINAL'
        result.append(best)
    return result


def session_anchors(snapshot, context, cfg, engine):
    """Prepare a new OB only after a session raid and a later 15m MSS."""
    rows=structure.closed(snapshot,'15m')
    found=[]
    for side in (1,-1):
        for event in ict.session_sweeps(snapshot,side,cfg.tick,engine):
            if snapshot.now-event['shift_ts']>30*60_000: continue
            before=rows[max(event['raid_index'],event['shift_index']-6):event['shift_index']]
            ob=next((c for c in reversed(before) if side*(c.close-c.open)<0),None)
            if ob is None: continue
            low,high=ob.low,ob.high
            zone={'low':low,'high':high,'known_ts':event['shift_ts'],
                  'kind':'SESSION_OB','timeframe':'15m'}
            evidence={'entry_route':'STRATEGY_EVENT','source_strategy':'SESSION_SWEEP_MSS',
                'setup_timeframe':'15m','formation_timeframe':'15m',
                'strategy_signal_ts':event['shift_ts'],'strategy_event_ts':event['sweep_ts'],
                'episode_ts':event['sweep_ts'],'session_sweep':event,'entry_zone':zone,
                'swept_level':event['level'],'sweep_extreme':event['extreme'],'wick_ratio':1.,
                'setup_conditions':[{'condition':'Зняття сесійної ліквідності та пізніший 15m MSS','met':True}]}
            anchor=cfg.core.make_anchor('SWEEP_RECLAIM' if side==1 else 'LIQUIDITY_SWEEP_REVERSAL_SHORT',
                'LONG' if side==1 else 'SHORT', high if side==1 else low,event['extreme'],
                'SESSION_OB',event['pool']+' sweep → 15m MSS → OB',context,score=80,evidence=evidence)
            if anchor is not None:
                anchor.created_ts=event['shift_ts']
                anchor.expires_ts=event['shift_ts']+120*60_000
                anchor.id=engine.ident('SESSION',side,event['sweep_ts'],event['shift_ts'],low,high)
                found.append(anchor)
    return found
