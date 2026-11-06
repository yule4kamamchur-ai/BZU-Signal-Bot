"""Allowlisted, read-only public projection for the v12.28 scored structural pre-plans and live tracking.

Candidate geometry is copied from the planner, never recalculated by the UI.
Unassessed market zones remain explicitly unassessed, not zero-score signals.
"""
from __future__ import annotations

from math import isfinite

RETIRED = frozenset({'ZONE_MITIGATED', 'ANCHOR_INVALIDATED', 'ANCHOR_TOO_OLD_TO_PLACE',
    'ANCHOR_EXPIRED_AWAITING_NEW_EVENT', 'LEVEL_ALREADY_VISITED_AFTER_OBSERVATION',
    'LEVEL_OR_EVENT_COOLDOWN'})


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def build(state, snapshot, result, cfg, engine):
    reasons = {**engine.REASON_UA,
        'NO_EVALUATED_SETUP':'сетап для цієї зони ще не оцінено',
        'TARGET_REACHED_WITHOUT_FILL':'ціль досягнута без виконання входу',
        'STRUCTURE_INVALIDATED_NOW':'торговий сценарій утратив чинність',
        'STRUCTURE_FLIPPED':'підтверджено зміну структури',
        'CONFIRMED_HTF_FLIP':'змінився напрямок старших графіків',
        'DYNAMIC_ORDER_STEP':'підтверджено новий ближчий рівень',
        'TAKE_PROFIT':'вихід за тейк-профітом', 'STOP':'вихід за стопом',
        'STOP_GAP':'вихід за стопом із ціновим розривом'}
    plans = engine.open_plans(state)
    orders = []
    for plan in plans:
        active = plan.get('status') == 'OPEN'
        orders.append({
            'id': plan['id'], 'number': plan.get('order_number'),
            'status': 'OPEN' if active else 'CANCELLING' if plan.get('cancel_requested_ts') else 'PENDING',
            'side': plan['side'], 'type': 'DIRECT' if plan.get('action_type') == 'ACTIVE_ENTRY' else 'LIMIT',
            'setup': plan.get('source_strategy') or plan.get('setup'),
            'entry': plan['entry'], 'stop': plan['stop'], 'initial_stop': plan.get('initial_stop', plan['entry']-plan['side']*plan['risk']),
            'tp1': plan.get('tp1', plan.get('tp')), 'tp2': plan.get('tp2'), 'risk': plan['risk'],
            'score': plan.get('score'), 'grade': plan.get('rating'),
            'be_trigger': plan.get('tp1') if plan.get('policy') == engine.bzu_policy.POLICY else
                plan['entry'] + plan['side'] * plan['risk'] * plan.get('breakeven_trigger_r', 1.),
            'be_stop': plan.get('breakeven_price'), 'breakeven_armed': bool(plan.get('breakeven_armed')),
            'structural_trail_trigger': (plan['entry'] + plan['side'] * plan['risk'] * plan['structural_trail_trigger_r']
                                         if 'structural_trail_trigger_r' in plan else None),
            'tp1_fraction': plan.get('partials', {}).get('TP1', 1.),
            'remaining': plan.get('remaining', 1.), 'tp1_hit': bool(plan.get('tp1_hit')),
            'effective_ts': plan.get('effective_ts', plan.get('placed_ts')),
            'expires_ts': plan.get('expires_ts'), 'filled_ts': plan.get('filled_ts'),
            'pending_stop': (plan.get('pending_stop') or {}).get('price'),
            'pending_stop_effective_ts': (plan.get('pending_stop') or {}).get('effective_ts'),
            'zone': {k:(plan.get('entry_zone') or {}).get(k) for k in ('kind','low','high','timeframe','known_ts')},
        })

    analysis = result.get('analysis', {})
    healthy = not analysis.get('health') and result.get('action') != 'DATA_HOLD'
    regime = engine.policy.market_regime(snapshot, cfg.tick, engine) if healthy else {}
    mode = {'BULLISH_TREND':'BULLISH_ACTIVE', 'BEARISH_TREND':'BEARISH_ACTIVE',
            'RANGE_LIQUIDITY_ROTATION':'RANGE'}.get(regime.get('state'), regime.get('state', 'UNKNOWN'))
    zones, candidates = {}, {}
    rejected = []
    atr15 = engine.atr(snapshot.candles.get('15m', []))
    atr15 = atr15 if numeric(atr15) and atr15 > 0 else 0
    primary_side = 1 if mode == 'BULLISH_ACTIVE' else -1 if mode == 'BEARISH_ACTIVE' else 0
    minimum_spread = max(.65, 1.2 * atr15)
    entry_mode = analysis.get('entry_mode', state.get('entry_mode', 'PASSIVE_LIMIT'))
    allowed_ids = ({p['id'] for p in analysis.get('admitted_plans', analysis.get('reaction_plans', []))}
                   if entry_mode in ('LIVE_REACTION','HYBRID') else None)
    zone_cache = {}
    closed_five = engine.scanner.closed_five_minutes(snapshot)

    def reject(row, reason):
        rejected.append({'kind':(row.get('entry_zone') or row).get('kind'),
                         'side':row.get('side'),'entry':row.get('entry',row.get('level')),'reason':reason})

    def zone_health(zone, side):
        key = (zone.get('kind'), side, zone.get('low'), zone.get('high'), zone.get('known_ts'))
        if key in zone_cache:
            return zone_cache[key]
        low, high = zone['low'], zone['high']
        threshold = high-.6*(high-low) if side == 1 else low+.6*(high-low)
        known = zone.get('known_ts')
        known = known if numeric(known) else snapshot.now
        # Closed bars only, after the zone became knowable; never the formation bar.
        breached = any(c.confirmed and c.ts >= known and c.ts+900_000 <= snapshot.now and
                      side*(c.close-threshold) <= 0 for c in snapshot.candles.get('15m', []))
        breached = breached or any(c.ts >= known and side*(c.close-threshold) <= 0
                                  for c in closed_five)
        zone_cache[key] = not breached
        return not breached

    def zone_data(zone, side):
        low, high = zone.get('low'), zone.get('high')
        known = zone.get('known_ts')
        if (zone.get('mitigated') or side not in (-1, 1) or not zone.get('kind') or not numeric(low) or
                not numeric(high) or not 0 < low < high or (numeric(known) and known > snapshot.now)):
            return None
        if not engine.scanner.zone_direction_matches(zone, side):
            return None
        if not zone_health(zone, side):
            return None
        result = {'kind': zone['kind'], 'timeframe': zone.get('timeframe'), 'side': side,
                  'low': low, 'high': high, 'known_ts': known, 'source_low':low, 'source_high':high}
        sweep=zone.get('sweep') or {}
        sweep_ts=sweep.get('known_ts',sweep.get('sweep_ts')) if isinstance(sweep,dict) else None
        if numeric(sweep_ts) and sweep_ts<=snapshot.now: result['sweep_known_ts']=sweep_ts
        return result

    def zone_key(zone):
        return (zone['kind'], zone.get('timeframe'), zone['side'], zone['low'], zone['high'])

    def distance(row):
        entry = row.get('entry')
        delta = entry - snapshot.price if numeric(entry) else None
        row.update(distance=delta, distance_pct=delta / snapshot.price * 100 if delta is not None else None,
                   distance_ticks=round(abs(delta) / cfg.tick) if delta is not None else None)
        return row

    # Raw structure is useful context, but it has no executable entry or quality score.
    market_zones = analysis.get('full_context', {}).get('zones', {})
    for name in ('order_blocks', 'fvg'):
        for zone in market_zones.get(name, []):
            side, step = cfg.core.side_sign(zone.side), engine.TF.get(zone.timeframe)
            if zone.mitigated or not step:
                continue
            z = zone_data({'kind': zone.kind, 'timeframe': zone.timeframe, 'low': zone.low,
                           'high': zone.high, 'known_ts': zone.created_ts + 2 * step}, side)
            if z:
                z.update(id=engine.ident(*zone_key(z)), score=None, grade=None, score_source='UNASSESSED',
                         entry=z['high'] if side == 1 else z['low'], entry_source='ZONE_BOUNDARY',
                         status='OBSERVING', reason='NO_EVALUATED_SETUP', stop=None, tp1=None, tp2=None)
                zones[zone_key(z)] = distance(z)

    # Low-detail paths first; real planner proposals take precedence. Never export internals.
    sources = [*analysis.get('watch', state.get('watch', [])),
               *analysis.get('setup_matrix', state.get('setup_matrix', [])),
               *analysis.get('poi_paths', []), *analysis.get('candidate_plans', []), *plans]
    for row in sources:
        side, reason = row.get('side'), row.get('reason', (row.get('quality') or {}).get('reason'))
        if side not in (-1, 1) or reason in RETIRED:
            continue
        raw_zone = row.get('entry_zone') or {}
        zone = zone_data(raw_zone, side)
        if raw_zone and not zone:
            reject(row, 'ZONE_DIRECTION_MISMATCH' if not engine.scanner.zone_direction_matches(raw_zone, side) else 'ZONE_MITIGATED_OR_INVALID_RANGE')
            continue
        if row.get('score_status') == 'UNAVAILABLE' or reason == 'SETUP_CONDITIONS_INCOMPLETE':
            continue
        entry = row.get('entry', row.get('level'))
        if not numeric(entry) or entry <= 0:
            continue
        if not zone and row.get('status') in ('NO_SETUP', 'ERROR'):
            continue
        identity = row.get('source_strategy') or row.get('setup')
        key = (*zone_key(zone), identity, entry) if zone else (identity, row.get('timeframe'), side, entry)
        item = {**(zone or {}), 'id': engine.ident(*key), 'side': side,
                'setup': row.get('source_strategy') or row.get('setup'),
                'engine_plan_id':row.get('id'),
                'engine_accepted':bool((row.get('quality') or {}).get('accepted') and (allowed_ids is None or row.get('id') in allowed_ids)),
                'timeframe': (zone or {}).get('timeframe') or row.get('timeframe'),
                'entry': entry, 'entry_source': row.get('entry_source') or
                    ('PLANNER' if numeric(row.get('stop')) else 'ANCHOR'),
                'reason': reason or 'CANDIDATE', 'score_source': row.get('score_status') or
                    ('CONTEXT' if row.get('quality') or row.get('rating') else 'PATTERN')}
        for field, alternate in (('stop','stop'), ('tp1','tp'), ('tp2','tp2'), ('risk','risk'),
                                 ('score','score'), ('grade','rating'), ('readiness_pct','readiness_pct')):
            value = row.get(field, row.get(alternate))
            if value is not None:
                item[field] = value
        approach = row.get('approach') or {}
        item['confirmation_timeframe'] = approach.get('confirmation_timeframe') or ('5m/15m' if 'IMPULSE' in (reason or '') else None)
        item['status'] = ('WAIT_CONFIRMATION' if approach.get('blocked') or
                          any(token in (reason or '') for token in ('CONFIRM', 'WAIT_RETEST', 'WAIT_BREAK')) else
                          'CANDIDATE' if reason == 'ACCEPTED' or row.get('status') == 'LIMIT_READY' or
                              (row.get('quality') or {}).get('accepted') else 'BLOCKED')
        item['reason_text'] = reasons.get(item['reason'], item['reason'])
        is_managed = row.get('status') == 'OPEN'
        if not is_managed and numeric(item.get('stop')) and side*(entry-item['stop']) <= 0:
            reject(row, 'INVALID_STOP_DIRECTION')
            continue
        if not is_managed and any(numeric(item.get(k)) and side*(item[k]-entry)<=0 for k in ('tp1','tp2')):
            reject(row, 'INVALID_TARGET_DIRECTION')
            continue
        error = engine.scanner.candidate_plan_error(item)
        if not numeric(item.get('known_ts')):
            error = error or 'ZONE_TIME_UNKNOWN'
        if item['status'] != 'CANDIDATE':
            error = error or 'ENGINE_CONFIRMATION_REQUIRED'
        if allowed_ids is not None and row.get('id') not in allowed_ids:
            error = error or 'NOT_IN_FINAL_REACTION_PLANS'
        if not atr15:
            error = error or 'ATR_UNAVAILABLE'
        if error:
            item.update(status='REFERENCE', gate_reason=error, reaction_eligible=False)
        else:
            item.update(risk=side*(entry-item['stop']), rr1=side*(item['tp1']-entry)/(side*(entry-item['stop'])),
                        status='CANDIDATE', reaction_eligible=True)
        if zone and zone.get('refined') and not zone['low'] <= entry <= zone['high']:
            item.update(status='REFERENCE',gate_reason='ENTRY_OUTSIDE_REFINED_ZONE',reaction_eligible=False)
        if item.get('reaction_eligible') and (side*(snapshot.price-entry)<=cfg.tick/2 or
                (zone['high']>snapshot.price if side==1 else zone['low']<snapshot.price)):
            item.update(status='REFERENCE',gate_reason='ZONE_NOT_AHEAD_OF_PRICE',reaction_eligible=False)
        item['priority'] = 'PRIMARY' if not primary_side or side==primary_side else 'COUNTER_TREND'
        if item.get('reaction_eligible') and primary_side and side != primary_side:
            item.update(status='REFERENCE',gate_reason='BLOCKED_BY_TREND',reaction_eligible=False)
        item['valid_until'] = min(row.get('expires_ts') or snapshot.now+27*60_000, snapshot.now+27*60_000)
        if item['valid_until'] <= snapshot.now:
            item.update(status='REFERENCE',gate_reason='EXPIRED',reaction_eligible=False)
        for order in orders:
            if order['id'] == row.get('id') or (zone and order['side'] == side and zone['low'] <= order['entry'] <= zone['high']):
                item.update(status=order['status'], order_id=order['id'], entry_source='ORDER',
                            setup=order['setup'], score_source='CONTEXT', reason='ORDER_' + order['status'],
                            reason_text='чинний план за сигнальним журналом')
                for field in ('entry', 'stop', 'tp1', 'tp2', 'risk', 'score', 'grade'):
                    item[field] = order.get(field)
                break
        ranks = {'OPEN':6, 'PENDING':5, 'CANCELLING':4, 'CANDIDATE':3, 'REFERENCE':1}
        def rank(z):
            return (ranks.get(z.get('status'), 0), sum(numeric(z.get(k)) for k in ('stop','tp1','tp2')), z.get('score') or 0)
        previous_candidate = candidates.get(key, {})
        if rank(item) < rank(previous_candidate):
            continue
        candidates[key] = distance(item)
        if zone:
            previous = zones.get(zone_key(zone), {})
            if rank(item) >= rank(previous):
                zones[zone_key(zone)] = item

    # Every valid zone is planned independently of an executable setup's admission.
    # Whole geometries are copied or calculated; partial plans are never spliced.
    planned=[];unplanned=[]
    by_order={o['id']:o for o in orders}
    context=analysis.get('full_context', {})
    for original in zones.values():
        z=dict(original)
        order=by_order.get(z.get('order_id'))
        geometry=dict(z)
        if order and order['status']=='OPEN': geometry['stop']=order['initial_stop']
        calc=engine.scanner.calculate_zone_trade_plan(z,snapshot,cfg,engine,primary_side,context,geometry)
        if not calc:
            unplanned.append({'id':z['id'],'kind':z['kind'],'reason':'INSUFFICIENT_PLAN_DATA'})
            continue
        z.update(engine_score=original.get('score'),engine_reason=original.get('reason'))
        z.update(calc)
        z['id']=engine.ident('ZONE_PLAN_V28',z['kind'],z.get('timeframe'),z['side'],z['low'],z['high'],z['known_ts'])
        z['entry_source']='ENGINE_PLAN' if calc['plan_basis']=='ENGINE_GEOMETRY' else 'STRUCTURAL_PREPLAN'
        z['valid_until']=original.get('valid_until',snapshot.now+27*60_000)
        z['priority']='PRIMARY' if not primary_side or z['side']==primary_side else 'COUNTER_TREND'
        z['execution_admitted']=bool(original.get('engine_accepted'))
        gate=calc['risk_gate']
        if primary_side and z['side']!=primary_side: gate=gate or 'BLOCKED_BY_TREND'
        if z['side']*(snapshot.price-z['stop'])<=0: gate='STOP_BREACHED'
        if z['score']<58: gate=gate or 'SCORE_BELOW_58'
        z.update(gate_reason=gate,status='REFERENCE' if gate else 'CANDIDATE' if z['execution_admitted'] else 'PREPLAN',
                 display_eligible=gate not in ('STOP_BREACHED','STOP_ALREADY_BREACHED') and z['priority']!='COUNTER_TREND',
                 reaction_eligible=not bool(gate),map_state='AHEAD' if z['side']*(snapshot.price-z['entry'])>0 else 'AT_OR_THROUGH_ENTRY')
        if gate: z['tactic']='OBSERVE'
        if order:
            # Existing ledger contracts win over newly calculated previews.
            z['ledger_score']=order.get('score')
            if calc['plan_basis']=='ENGINE_GEOMETRY':
                z.update({k:order.get(k) for k in ('entry','stop','tp1','tp2','risk','status') if order.get(k) is not None})
                z['rr1']=z['side']*(z['tp1']-z['entry'])/z['risk']
                z['rr2']=z['side']*(z['tp2']-z['entry'])/z['risk']
            else:
                z.update(status='REFERENCE',gate_reason='INCOMPLETE_LEDGER_PLAN',
                         reason_text='Повний попередній план зони; неповний план ордера наведено окремо за журналом.')
            z.update(order_id=order['id'],display_eligible=False,reaction_eligible=False,execution_admitted=True)
        planned.append(distance(z))
    all_zones=sorted(planned,key=lambda z:abs(z['distance']))
    candidate_rows=[z for z in all_zones if z.get('display_eligible')]
    def priority(z):
        return (z.get('gate_reason') is not None,not z.get('reaction_eligible'),abs(z['distance']) if z.get('gate_reason') else -(z.get('score') or 0),-z['rr1'],abs(z['distance']))
    candidate_rows.sort(key=priority)
    radar={label:next((z for z in candidate_rows if z['side']==side),None) for label,side in (('LONG',1),('SHORT',-1))}
    left,right=radar['LONG'],radar['SHORT']
    if left and right and (right['entry']-left['entry']<minimum_spread or left['high']>=right['low']):
        keep=min((left,right),key=priority)
        radar['SHORT' if keep is left else 'LONG']=None
    primary=next((z for z in radar.values() if z and z['side']==primary_side),None)
    if not primary and not primary_side: primary=min((z for z in radar.values() if z),key=priority,default=None)
    reference_rows=[z for z in all_zones if z.get('gate_reason')]

    records = {row['id']:row for row in [*state.get('orders', []), *state.get('trades', [])]}
    recent = []
    kinds = {'PLACED', 'FILLED', 'CANCEL_REQUESTED', 'CANCELLED', 'EXPIRED', 'CLOSED',
             'TP1', 'TP1_PARTIAL', 'TP1_REACHED', 'TP2', 'PAPER_STOP_AMENDED', 'UNRESOLVED'}
    for event in state.get('events', []):
        if event.get('kind') not in kinds or event.get('ts', 0) > snapshot.now:
            continue
        row = records.get(event.get('order_id'), {})
        item = {key: event.get(key) for key in ('order_id', 'ts', 'kind', 'reason', 'net_r', 'price', 'stop')}
        item.update(side=row.get('side', event.get('side')), entry=row.get('entry', event.get('entry')), number=row.get('order_number'))
        item['reason_text'] = reasons.get(event.get('reason'), event.get('reason'))
        recent.append(item)
    recent.sort(key=lambda e: e['ts'])

    def candles(rows, step):
        return [{'time':c.ts//1000,'open':c.open,'high':c.high,'low':c.low,'close':c.close}
                for c in rows[-180:] if c.confirmed and c.ts+step<=snapshot.now]

    system_mode = ('DATA_HOLD' if not healthy else 'ACTIVE' if any(o['status']=='OPEN' for o in orders) else
                   'PENDING' if any(o['status']=='PENDING' for o in orders) else
                   'CANCELLING' if orders else 'SCANNING')
    return {'schema':1, 'cockpit_schema':3, 'version':engine.REPORT_RELEASE,
            'instrument':cfg.instrument,'tick':cfg.tick,
            'snapshot_ts':snapshot.now,'ledger_ts':state.get('last_run_ts'),
            'price':snapshot.price,'ticker_ts':snapshot.ticker_ts,'market_regime':mode,
            'status':'READY' if healthy else 'DATA_HOLD','system_mode':system_mode,'orders':orders,
            'execution':'SIGNAL_LEDGER','daily':engine.day_stats(state,snapshot.now,cfg),
            'occupied_slots':engine.occupied_slots(state)+len(engine.pending_plans(state)),
            'recent_events':recent[-20:], 'last_event':recent[-1] if recent else None,
            'candidates':candidate_rows, 'plan_pool':all_zones, 'unplanned_zones':unplanned, 'radar':radar, 'primary_candidate_id':primary['id'] if primary else None,
            'primary_bias':primary_side,'atr15':atr15,'min_structural_spread':minimum_spread,
            'entry_mode':entry_mode,'reference_levels':reference_rows[:48],
            'candidate_rejections':rejected[:100],
            'candidate_rules':{'min_score':58,'direct_min_score':65,'min_rr':1.4,'max_zone_atr':1.5,'mitigation_depth':.6},
            'reaction_config':{'retracement':max(.06,3*cfg.tick),'max_sample_gap_ms':15000,
                               'min_samples':3,'ready_ttl_ms':15000,'max_chase_r':.25},
            'zones':sorted(all_zones,key=lambda z:abs(z['distance'])),
            'candles':{'5m':candles(engine.scanner.closed_five_minutes(snapshot),300_000),
                       '15m':candles(snapshot.candles.get('15m',[]),900_000)}}
