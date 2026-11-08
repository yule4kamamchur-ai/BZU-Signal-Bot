"""Allowlisted, read-only public projection for the v12.29 actionable plans and live tracking.

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


def zone_rank(zone):
    return (bool(zone.get('order_id')), bool(zone.get('engine_accepted')),
            sum(numeric(zone.get(k)) for k in ('stop', 'tp1', 'tp2')), zone.get('score') or 0)


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
        stop_amendment = engine.bzu_policy.protective_amendment(plan) or {}
        partial = bool(active and 0 < plan.get('remaining', 1.) < plan.get('filled_fraction', 1.)
                       and any(leg.get('target') == 'TP1' and leg.get('fraction', 0) > 0
                               for leg in plan.get('realized_legs', [])))
        orders.append({
            'id': plan['id'], 'number': plan.get('order_number'),
            'status': 'OPEN' if active else 'CANCELLING' if plan.get('cancel_requested_ts') else 'PENDING',
            'side': plan['side'], 'type': 'DIRECT' if plan.get('action_type') == 'ACTIVE_ENTRY' else 'LIMIT',
            'setup': plan.get('source_strategy') or plan.get('setup'),
            'entry': plan['entry'], 'stop': plan['stop'], 'initial_stop': plan.get('initial_stop', plan['entry']-plan['side']*plan['risk']),
            'tp1': plan.get('tp1', plan.get('tp')), 'tp2': plan.get('tp2'), 'risk': plan['risk'],
            'score': plan.get('score'), 'grade': plan.get('rating'),
            'be_stop': plan.get('breakeven_price') if plan.get('be_confirmed_ts') else None,
            'be_confirmed_ts': plan.get('be_confirmed_ts'),
            'breakeven_armed': bool(plan.get('be_confirmed_ts') and plan.get('breakeven_armed')),
            'tp1_fraction': plan.get('partials', {}).get('TP1', 1.),
            'remaining': plan.get('remaining', 1.), 'tp1_hit': bool(plan.get('tp1_hit')),
            'tp1_partial': partial,
            'be_waits_for_structure': engine.bzu_policy.partial_tp1_time(plan) is not None,
            'effective_ts': plan.get('effective_ts', plan.get('placed_ts')),
            'expires_ts': plan.get('expires_ts'), 'filled_ts': plan.get('filled_ts'),
            'pending_stop': stop_amendment.get('price'),
            'pending_stop_effective_ts': stop_amendment.get('effective_ts'),
            'zone': {k:(plan.get('entry_zone') or {}).get(k) for k in ('kind','low','high','timeframe','known_ts')},
        })

    analysis = result.get('analysis', {})
    healthy = not analysis.get('health') and result.get('action') != 'DATA_HOLD'
    regime = engine.policy.market_regime(snapshot, cfg.tick, engine) if healthy else {}
    mode = {'BULLISH_TREND':'BULLISH_ACTIVE', 'BEARISH_TREND':'BEARISH_ACTIVE',
            'RANGE_LIQUIDITY_ROTATION':'RANGE'}.get(regime.get('state'), regime.get('state', 'UNKNOWN'))
    zones, context_zones = {}, []
    admitted_geometry = {p['id']: p for p in analysis.get('admitted_plans', [])}
    rejected = []
    atr15 = engine.atr([c for c in snapshot.candles.get('15m', [])
                       if c.confirmed and c.ts + 900_000 <= snapshot.now])
    atr15 = atr15 if numeric(atr15) and atr15 > 0 else 0
    primary_side = 1 if mode == 'BULLISH_ACTIVE' else -1 if mode == 'BEARISH_ACTIVE' else 0
    minimum_spread = max(.65, 1.2 * atr15)
    entry_mode = analysis.get('entry_mode', state.get('entry_mode', 'PASSIVE_LIMIT'))
    admitted = analysis.get('admitted_plans', [])
    admitted_rows = {id(plan) for plan in admitted}

    def reject(row, reason):
        rejected.append({'kind':(row.get('entry_zone') or row).get('kind'),
                         'side':row.get('side'),'entry':row.get('entry',row.get('level')),'reason':reason})

    def zone_data(zone, side):
        low, high = zone.get('low'), zone.get('high')
        known = zone.get('known_ts')
        if not engine.scanner.zone_direction_matches(zone, side):
            return None
        if engine.scanner.standalone_zone_error(
                {**zone, 'side': side}, snapshot, cfg, engine, atr15, mode):
            return None
        result = {'kind': zone['kind'], 'timeframe': zone.get('timeframe'), 'side': side,
                  'low': low, 'high': high, 'known_ts': known, 'source_low':low, 'source_high':high}
        if numeric(zone.get('impulse_known_ts')):
            result['impulse_known_ts'] = zone['impulse_known_ts']
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
                           'high': zone.high, 'known_ts': zone.created_ts + 2 * step,
                           'impulse_known_ts': zone.created_ts + (step if name == 'fvg' else 2 * step)}, side)
            if z:
                z.update(id=engine.ident(*zone_key(z)), score=None, grade=None, score_source='UNASSESSED',
                         entry=z['high'] if side == 1 else z['low'], entry_source='ZONE_BOUNDARY',
                         status='OBSERVING', reason='NO_EVALUATED_SETUP', stop=None, tp1=None, tp2=None)
                zones[zone_key(z)] = distance(z)
                if z['timeframe'] in ('15m', '1H'):
                    context_zones.append({k: z[k] for k in ('id', 'kind', 'timeframe', 'side', 'low', 'high', 'known_ts')})

    # Low-detail paths first; real planner proposals take precedence. Never export internals.
    sources = [*analysis.get('watch', state.get('watch', [])),
               *analysis.get('setup_matrix', state.get('setup_matrix', [])),
               *analysis.get('poi_paths', []), *analysis.get('candidate_plans', []), *admitted, *plans]
    for row in sources:
        side, reason = row.get('side'), row.get('reason', (row.get('quality') or {}).get('reason'))
        if side not in (-1, 1) or reason in RETIRED:
            continue
        raw_zone = row.get('entry_zone') or {}
        zone = zone_data(raw_zone, side)
        if raw_zone and not zone:
            reject(row, 'ZONE_DIRECTION_MISMATCH' if not engine.scanner.zone_direction_matches(raw_zone, side)
                   else engine.scanner.standalone_zone_error(
                {**raw_zone, 'side': side}, snapshot, cfg, engine, atr15, mode))
            continue
        if row.get('score_status') == 'UNAVAILABLE' or reason == 'SETUP_CONDITIONS_INCOMPLETE':
            continue
        entry = row.get('entry', row.get('level'))
        if not numeric(entry) or entry <= 0:
            continue
        if not zone:
            continue
        identity = row.get('source_strategy') or row.get('setup')
        key = (*zone_key(zone), identity, entry)
        item = {**zone, 'id': engine.ident(*key), 'side': side,
                'setup': row.get('source_strategy') or row.get('setup'),
                'engine_plan_id':row.get('id'),
                'engine_accepted':id(row) in admitted_rows,
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
        item['priority'] = 'PRIMARY' if not primary_side or side==primary_side else 'COUNTER_TREND'
        item['valid_until'] = min(row.get('expires_ts') or snapshot.now + 27 * 60_000,
                                  snapshot.now + 27 * 60_000)
        for order in orders:
            if order['id'] == row.get('id'):
                item.update(status=order['status'], order_id=order['id'], entry_source='ORDER',
                            setup=order['setup'], score_source='CONTEXT', reason='ORDER_' + order['status'],
                            reason_text='чинний план за сигнальним журналом')
                for field in ('entry', 'stop', 'tp1', 'tp2', 'risk', 'score', 'grade'):
                    item[field] = order.get(field)
                break
        previous = zones.get(zone_key(zone), {})
        if zone_rank(item) >= zone_rank(previous):
            zones[zone_key(zone)] = item

    # Every valid zone is planned independently of an executable setup's admission.
    # Whole geometries are copied or calculated; partial plans are never spliced.
    planned=[];unplanned=[]
    by_order={o['id']:o for o in orders}
    context=analysis.get('full_context', {})
    for original in zones.values():
        z=dict(original)
        order=by_order.get(z.get('order_id'))
        geometry = (dict(z) if order else admitted_geometry.get(z.get('engine_plan_id'))
                    if z.get('engine_accepted') else None)
        if order and order['status'] == 'OPEN':
            geometry['stop'] = order['initial_stop']
        calc = engine.scanner.calculate_zone_trade_plan(
            z, snapshot, cfg, engine, primary_side, context, geometry, regime=mode)
        if not calc:
            unplanned.append({'id':z['id'],'kind':z['kind'],'reason':'INSUFFICIENT_PLAN_DATA'})
            continue
        z.update(engine_score=original.get('score'),engine_reason=original.get('reason'))
        z.update(calc)
        z['id']=engine.ident('ZONE_PLAN_V29',z['kind'],z.get('timeframe'),z['side'],z['low'],z['high'],z['known_ts'])
        z['entry_source']='ENGINE_PLAN' if calc['plan_basis']=='ENGINE_GEOMETRY' else 'STRUCTURAL_PREPLAN'
        z['valid_until']=original.get('valid_until',snapshot.now+27*60_000)
        z['priority']='PRIMARY' if not primary_side or z['side']==primary_side else 'COUNTER_TREND'
        z['execution_admitted']=bool(original.get('engine_accepted'))
        gate = calc['risk_gate']
        if z.get('setup') == 'SESSION_MEAN_RECLAIM':
            gate = 'SESSION_MEAN_LIMIT_DISABLED'
        if z['priority'] == 'COUNTER_TREND':
            gate = 'BLOCKED_BY_TREND'
        elif not healthy:
            gate = 'DATA_HOLD'
        elif z['side'] * (snapshot.price - z['entry']) <= cfg.tick / 2:
            gate = 'ZONE_NOT_AHEAD_OF_PRICE'
        elif z['valid_until'] <= snapshot.now:
            gate = 'EXPIRED'
        elif gate is None and (z['score'] < 60 or (z['execution_admitted'] and (z['engine_score'] or 0) < 60)):
            gate = 'SCORE_BELOW_60'
        elif calc['impulse_blocked']:
            gate = 'IMPULSE_CONFIRMATION_REQUIRED'
        elif not z['execution_admitted'] or calc['plan_basis'] != 'ENGINE_GEOMETRY':
            gate = gate or 'NOT_IN_FINAL_REACTION_PLANS'
        elif engine.scanner.candidate_plan_error({**z, 'score_source': 'CONTEXT'},
                                                min_score=60, min_rr=1.40):
            gate = gate or 'INVALID_PLAN_GEOMETRY'
        actionable = gate is None and order is None
        z.update(gate_reason=gate, status='CANDIDATE' if actionable else 'REFERENCE',
                 actionable=actionable, display_eligible=actionable, reaction_eligible=actionable,
                 map_state='AHEAD' if z['side'] * (snapshot.price - z['entry']) > 0 else 'AT_OR_THROUGH_ENTRY')
        if not actionable:
            z['tactic'] = 'OBSERVE'
        elif entry_mode == 'LIVE_REACTION':
            z['tactic'] = 'REACTION_M5'
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
    candidate_rows=[z for z in all_zones if z['actionable']]
    def priority(z):
        return (-z['score'], abs(z['distance']), -z['rr1'])
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
    last_closed = next((e for e in reversed(recent) if e['kind'] == 'CLOSED'), None)
    def context_distance(zone):
        return max(zone['low'] - snapshot.price, snapshot.price - zone['high'], 0)
    # Only independently observed market bands may appear as quiet chart context.
    context_zones = [z for z in context_zones
                     if context_distance(z) <= engine.scanner.max_zone_distance(atr15, cfg)]
    context_zones.sort(key=lambda z: (context_distance(z), z['timeframe'] != '1H'))
    context_bands = []
    for side in (1, -1):
        nearest = next((z for z in context_zones if z['side'] == side), None)
        if nearest:
            context_bands.append(nearest)

    def candles(rows, step):
        return [{'time':c.ts//1000,'open':c.open,'high':c.high,'low':c.low,'close':c.close}
                for c in rows[-180:] if c.confirmed and c.ts+step<=snapshot.now]

    system_mode = ('DATA_HOLD' if not healthy else 'ACTIVE' if any(o['status']=='OPEN' for o in orders) else
                   'PENDING' if any(o['status']=='PENDING' for o in orders) else
                   'CANCELLING' if orders else 'SCANNING')
    return {'schema':1, 'cockpit_schema':4, 'version':engine.REPORT_RELEASE,
            'instrument':cfg.instrument,'tick':cfg.tick,
            'snapshot_ts':snapshot.now,'ledger_ts':state.get('last_run_ts'),
            'price':snapshot.price,'ticker_ts':snapshot.ticker_ts,'market_regime':mode,
            'status':'READY' if healthy else 'DATA_HOLD','system_mode':system_mode,'orders':orders,
            'execution':'SIGNAL_LEDGER','daily':engine.day_stats(state,snapshot.now,cfg),
            'occupied_slots':engine.occupied_slots(state)+len(engine.pending_plans(state)),
            'recent_events':recent[-20:], 'last_event':recent[-1] if recent else None,
            'last_closed':last_closed, 'context_bands':context_bands,
            'candidates':candidate_rows, 'plan_pool':all_zones, 'unplanned_zones':unplanned, 'radar':radar, 'primary_candidate_id':primary['id'] if primary else None,
            'primary_bias':primary_side,'atr15':atr15,'min_structural_spread':minimum_spread,
            'entry_mode':entry_mode,'reference_levels':reference_rows[:48],
            'candidate_rejections':rejected[:100],
            'candidate_rules':{'min_score':60,'direct_min_score':65,'min_rr':1.4,'max_zone_atr':1.5,'mitigation_depth':.6},
            'reaction_config':{'retracement':max(.06,3*cfg.tick),'max_sample_gap_ms':15000,
                               'min_samples':3,'ready_ttl_ms':15000,'max_chase_r':.25},
            'zones':sorted(all_zones,key=lambda z:abs(z['distance'])),
            'candles':{'5m':candles(engine.scanner.closed_five_minutes(snapshot),300_000),
                       '15m':candles(snapshot.candles.get('15m',[]),900_000)}}
