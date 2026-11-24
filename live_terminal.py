"""Paper journal synchronization and an allowlisted public cockpit projection.

Candidate geometry is copied from the planner, never recalculated by the UI.
Unassessed market zones remain explicitly unassessed, not zero-score signals.
"""
from __future__ import annotations

from math import isfinite
import os

import oil_risk
import position_monitor

RETIRED = frozenset({'ZONE_MITIGATED', 'ANCHOR_INVALIDATED', 'ANCHOR_TOO_OLD_TO_PLACE',
    'ANCHOR_EXPIRED_AWAITING_NEW_EVENT', 'LEVEL_ALREADY_VISITED_AFTER_OBSERVATION',
    'LEVEL_OR_EVENT_COOLDOWN'})


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def zone_rank(zone):
    return (bool(zone.get('order_id')), bool(zone.get('engine_accepted')),
            sum(numeric(zone.get(k)) for k in ('stop', 'tp1', 'tp2')), zone.get('score') or 0)


def sync_active_signal_to_journal(state, snapshot, result, cfg, engine, *, state_path, journal_path,
                                  live_path=None, persist_state=True, persist_journal=True):
    """Commit the selected paper order and its UI projection under the engine lock."""
    if persist_state:
        engine.atomic_write(state_path, state)
    if persist_journal:
        engine.export_journal(journal_path, state, cfg.core)
    if live_path:
        duplicate = result['action'] == 'DUPLICATE_OR_OUT_OF_ORDER'
        recover_projection = (duplicate and snapshot.now == state.get('last_run_ts')
                              and engine.read_json(live_path).get('ledger_ts') != snapshot.now)
        if not duplicate or recover_projection:
            engine.atomic_write(live_path,result.get('public_projection') or build(state,snapshot,result,cfg,engine))


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
    liquidity = (engine.limit_structure.unswept_minute_pools(
        snapshot, engine.scenario_planner.structure_levels(snapshot, engine), cfg.tick) if plans else ())
    for plan in plans:
        active = plan.get('status') == 'OPEN'
        order_type = plan.get('order_type') or ('MARKET' if plan.get('action_type') == 'ACTIVE_ENTRY' else 'LIMIT')
        stop_amendment = engine.bzu_policy.protective_amendment(plan) or {}
        execution = engine.execution_config(plan, cfg)
        partial = bool(active and 0 < plan.get('remaining', 1.) < plan.get('filled_fraction', 1.)
                       and any(leg.get('target') == 'TP1' and leg.get('fraction', 0) > 0
                               for leg in plan.get('realized_legs', [])))
        entry = plan['entry']
        zone = plan.get('entry_zone') or {}
        proof = zone.get('wave_proof') or {}
        step = {'5m':300_000, **engine.TF}.get(zone.get('timeframe'))
        known = zone.get('known_ts')
        origin = (zone.get('origin_ts') or proof.get('ob_ts') or proof.get('shelf_start_ts')
                  or proof.get('impulse_ts') or (known-step if known and step else None))
        liquidity_lines = {}
        for label, side in (('bsl',-1),('ssl',1)):
            level = min((row for row in liquidity if row['side']==side and row.get('known_ts',0)<=snapshot.now
                         and side*(entry-row['level'])>cfg.tick/2),
                        key=lambda row:abs(entry-row['level']),default=None)
            if level:
                liquidity_lines[label] = {'price':level['level'],'known_ts':level.get('known_ts')}
        orders.append({
            'id': plan['id'], 'number': plan.get('order_number'),
            'status': 'OPEN' if active else 'CANCELLING' if plan.get('cancel_requested_ts') else 'PENDING',
            'side': plan['side'],
            'type': order_type, 'order_type': order_type,
            'market_trigger': plan.get('market_trigger'), 'admission_status': plan.get('admission_status'),
            'setup': plan.get('source_strategy') or plan.get('setup'),
            'entry': plan['entry'], 'stop': plan['stop'], 'invalidation': plan.get('invalidation'), 'initial_stop': plan.get('initial_stop', plan['entry']-plan['side']*plan['risk']),
            'tp1': engine.bzu_policy.partial_target(plan),
            'missed_target_level': None if active else engine.bzu_policy.missed_target_level(plan),
            'target_cancel_armed': not active and plan.get('target_cancel_armed', True),
            'qualification_tp1': plan.get('tp1'), 'scale_policy': plan.get('scale_policy'),
            'exit_policy':plan.get('exit_policy'), 'tp':plan.get('tp'),
            'entry_retest_reason':plan.get('entry_retest_reason'),
            'opening_sweep_confirmed':plan.get('opening_sweep_confirmed'),
            'protection_structure_level':plan.get('protection_structure_level'),
            'post_entry_protected_swing':plan.get('post_entry_protected_swing'),
            'meta_prob':plan.get('meta_prob'), 'meta_prob_calibrated':plan.get('meta_prob_calibrated'),
            'risk_multiplier':plan.get('risk_multiplier',1.), 'range_pos':plan.get('range_pos'),
            'late_entry':bool(plan.get('late_entry')), 'mfe_lock_steps':position_monitor.mfe_lock_steps(plan,engine),
            'institutional_family': plan.get('institutional_family'), 'tp2': plan.get('tp2'), 'risk': plan['risk'],
            'score': plan.get('score'), 'grade': plan.get('rating'),
            'organic_risk': plan.get('organic_risk'), 'tp1_source': plan.get('tp1_source'),
            'tp2_source': plan.get('tp2_source'), 'cancel_reason': plan.get('cancel_reason'),
            'trend_rel': plan.get('trend_rel'), 'trend_phase': plan.get('trend_phase'),
            'entry_max_distance': plan.get('entry_max_distance'), 'entry_hold':plan.get('entry_hold'),
            'min_score': engine.scanner.min_plan_score(plan.get('trend_rel'), plan.get('market_trigger')),
            'be_stop': plan.get('breakeven_price') if plan.get('be_confirmed_ts') else None,
            'be_confirmed_ts': plan.get('be_confirmed_ts'),
            'liquidity_protection': plan.get('liquidity_protection'), 'mfe_protection': plan.get('mfe_protection'),
            'time_decay_protection':plan.get('time_decay_protection'),
            'entry_fee_rate': (plan.get('entry_fee_rate', engine.fee_rate(
                execution, 'maker' if order_type == 'LIMIT' else 'taker')) if execution.include_commission else 0.),
            'exit_fee_rate': engine.fee_rate(execution, 'taker'),
            'slippage_bps': execution.slippage_bps,
            'exit_deadline': plan.get('exit_deadline'),
            'time_exit_enabled': bool(active and not engine.bzu_policy.full_position(plan) and (
                plan.get('time_exit_enabled') and not (partial or plan.get('scale_policy')==engine.bzu_policy.SCALE_POLICY)
                if engine.bzu_policy.is_new(plan) else not plan.get('tp1_decision_gate'))),
            'paper_touch_tolerance': oil_risk.PAPER_TOUCH_TOLERANCE if (
                oil_risk.is_organic(plan) and not engine.bzu_policy.full_position(plan)) else 0.,
            'reconciled_through_ts': (plan['last_bar']+plan.get('last_execution_step_ms', 180_000)
                                      if plan.get('last_bar', -1)>=0 else None),
            'expiry_reason': plan.get('expiry_reason'),
            'breakeven_armed': bool(plan.get('be_confirmed_ts') and plan.get('breakeven_armed')),
            'tp1_fraction': plan.get('partials', {}).get('TP1', 1.),
            'remaining': plan.get('remaining', 1.), 'tp1_hit': position_monitor.partial_taken(plan),
            'paper_realized_r': plan.get('realized_gross_r'),
            'tp1_partial': partial,
            'be_waits_for_structure': not plan.get('scale_policy') and engine.bzu_policy.partial_tp1_time(plan) is not None,
            'placed_ts': plan.get('placed_ts'),
            'effective_ts': plan.get('effective_ts', plan.get('placed_ts')),
            'expires_ts': plan.get('expires_ts'), 'filled_ts': plan.get('filled_ts'),
            'pending_stop': stop_amendment.get('price'),
            'pending_stop_effective_ts': stop_amendment.get('effective_ts'),
            **({**position_monitor.metrics(plan, snapshot.now),
                'position_guard': position_monitor.guard(
                    plan,snapshot.now,structural_only=engine.oil_risk.enabled(cfg))} if active else {}),
            'zone': {**{k:zone.get(k) for k in ('kind','low','high','timeframe','known_ts','liquidity_anchor')}, 'origin_ts':origin},
            'liquidity':liquidity_lines,
        })

    analysis = result.get('analysis', {})
    healthy = not analysis.get('health') and result.get('action') != 'DATA_HOLD'
    regime = engine.policy.execution_regime(snapshot, cfg, engine) if healthy else {}
    mode = {'BULLISH_TREND':'BULLISH_ACTIVE', 'BEARISH_TREND':'BEARISH_ACTIVE',
            'RANGE_LIQUIDITY_ROTATION':'RANGE'}.get(regime.get('state'), regime.get('state', 'UNKNOWN'))
    zones, context_keys = {}, set()
    radar_plans = analysis.get('radar_plans', [])
    admitted_geometry = {p['id']: p for p in [*radar_plans, *analysis.get('admitted_plans', [])]}
    rejected = []
    atr15 = engine.atr([c for c in snapshot.candles.get('15m', [])
                       if c.confirmed and c.ts + 900_000 <= snapshot.now])
    atr15 = atr15 if numeric(atr15) and atr15 > 0 else 0
    primary_side = engine.policy.regime_side(regime)
    profile = oil_risk.snapshot_profile(snapshot) if oil_risk.enabled(cfg) else None
    minimum_spread = (max(1., .8 * profile['floor']) if profile else
                      max(.65, 1.2 * atr15))
    entry_mode = analysis.get('entry_mode', state.get('entry_mode', 'PASSIVE_LIMIT'))
    admitted = analysis.get('admitted_plans', [])
    admitted_rows = {id(plan) for plan in [*radar_plans, *admitted]}

    def reject(row, reason):
        rejected.append({'kind':(row.get('entry_zone') or row).get('kind'),
                         'side':row.get('side'),'entry':row.get('entry',row.get('level')),'reason':reason})

    def zone_data(zone, side):
        low, high = zone.get('low'), zone.get('high')
        known = zone.get('known_ts')
        if not engine.scanner.zone_direction_matches(zone, side):
            return None
        error = engine.scanner.standalone_zone_error(
            {**zone, 'side': side}, snapshot, cfg, engine, atr15, mode)
        if error and error != 'RANGE_ZONE_TOO_CLOSE':
            return None
        result = {'kind': zone['kind'], 'timeframe': zone.get('timeframe'), 'side': side,
                  'low': low, 'high': high, 'known_ts': known, 'source_low':low, 'source_high':high}
        if zone.get('wave_proof'):
            result.update(wave_proof=zone['wave_proof'], sweep=zone.get('sweep'))
        if numeric(zone.get('impulse_known_ts')):
            result['impulse_known_ts'] = zone['impulse_known_ts']
        for key in ('execution_low','execution_high'):
            if numeric(zone.get(key)):
                result[key]=zone[key]
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

    # Raw structure receives reference geometry in the same projection pass.
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
                    context_keys.add(zone_key(z))

    # Low-detail paths first; real planner proposals take precedence. Never export internals.
    sources = [*analysis.get('watch', state.get('watch', [])),
               *analysis.get('setup_matrix', state.get('setup_matrix', [])),
               *analysis.get('poi_paths', []), *analysis.get('candidate_plans', []), *radar_plans, *admitted, *plans]
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
    ledger_geometry={p['id']:p for p in plans}
    context=analysis.get('full_context', {})
    for original in zones.values():
        z=dict(original)
        order=by_order.get(z.get('order_id'))
        geometry = (dict(ledger_geometry[order['id']]) if order else
                    admitted_geometry.get(z.get('engine_plan_id')) if z.get('engine_accepted') else None)
        if order and order['status'] == 'OPEN':
            geometry['stop'] = order['initial_stop']
        if order:
            geometry.setdefault('tp2', order.get('tp2'))
        calc = engine.scanner.calculate_zone_trade_plan(
            z, snapshot, cfg, engine, primary_side, context, geometry, regime=mode)
        if not calc:
            unplanned.append({'id':z['id'],'kind':z['kind'],'reason':'INSUFFICIENT_PLAN_DATA'})
            continue
        z.update(engine_score=original.get('score'),engine_reason=original.get('reason'))
        z.update(calc)
        z['institutional_family'] = (geometry or {}).get('institutional_family')
        z['scale_policy'] = (geometry or {}).get('scale_policy')
        if profile and engine.scanner.structural_pullback(z):
            z['market_pullback_max'] = .15*atr15
        z['id']=engine.ident('ZONE_PLAN_V30',z['kind'],z.get('timeframe'),z['side'],z['low'],z['high'],z['known_ts'])
        z['entry_source']='ENGINE_PLAN' if calc['plan_basis']=='ENGINE_GEOMETRY' else 'STRUCTURAL_PREPLAN'
        z['valid_until']=original.get('valid_until',snapshot.now+27*60_000)
        z['reversal_override'] = bool(calc['reversal_override'] and geometry and
                                      ((geometry.get('zone_origin') or {}).get('wave_confirmed')
                                       or geometry.get('reversal_exhaustion_bonus')
                                       or geometry.get('opening_sweep_confirmed')))
        z['priority'] = ('REVERSAL' if z['reversal_override'] else
                         'PRIMARY' if not primary_side or z['side'] == primary_side else 'COUNTER_TREND')
        z['execution_admitted']=bool(original.get('engine_accepted'))
        gate = calc['risk_gate']
        if original.get('reason') == 'EXHAUSTED_MOVE':
            z['risk_gate'] = gate = 'EXHAUSTED_MOVE'
        if (z.get('setup') == 'SESSION_MEAN_RECLAIM' and not engine.scanner.market_plan(z)
                and not z.get('entry_retest_reason')):
            gate = 'SESSION_MEAN_LIMIT_DISABLED'
        if gate == 'EXHAUSTED_MOVE':
            pass
        elif z['priority'] == 'COUNTER_TREND':
            gate = 'BLOCKED_BY_TREND'
        elif not healthy:
            gate = 'DATA_HOLD'
        elif not engine.scanner.market_plan(z) and z['side'] * (snapshot.price - z['entry']) <= cfg.tick / 2:
            gate = 'ZONE_NOT_AHEAD_OF_PRICE'
        elif z['valid_until'] <= snapshot.now:
            gate = 'EXPIRED'
        elif gate is None and (z['score'] < z['min_score'] or
                                (z['execution_admitted'] and (z['engine_score'] or 0) < z['min_score'])):
            gate = 'SCORE_BELOW_THRESHOLD' if z.get('trend_rel') else 'SCORE_BELOW_60'
        elif calc['impulse_blocked']:
            gate = 'TREND_M5_SPIKE' if z.get('trend_rel') == 'WITH_TREND' else 'IMPULSE_CONFIRMATION_REQUIRED'
        elif not z['execution_admitted'] or calc['plan_basis'] != 'ENGINE_GEOMETRY':
            gate = gate or 'NOT_IN_FINAL_REACTION_PLANS'
        elif engine.scanner.candidate_plan_error({**z, 'score_source': 'CONTEXT'},
                                                min_score=max(60,z['min_score']),
                                                min_rr=engine.scanner.MARKET_IMPULSE_MIN_RR if engine.scanner.market_plan(z) else engine.scanner.LIMIT_NEW_MIN_RR):
            gate = gate or 'INVALID_PLAN_GEOMETRY'
        actionable = gate is None and order is None
        z.update(gate_reason=gate, status='CANDIDATE' if actionable else 'REFERENCE',
                 limit_ready=bool(actionable and calc['tactic'] == 'LIMIT' and entry_mode != 'LIVE_REACTION'),
                 market_now=bool(actionable and calc['tactic'] == 'MARKET' and entry_mode != 'LIVE_REACTION'),
                 actionable=actionable, display_eligible=actionable,
                 reaction_eligible=actionable and (z.get('trend_rel') != 'WITH_TREND' or entry_mode == 'LIVE_REACTION'),
                 map_state=('IN_ZONE' if z['in_zone'] else
                            'AHEAD' if z['side'] * (snapshot.price - z['entry']) > 0 else 'AT_OR_THROUGH_ENTRY'))
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
    def priority(zone):
        return (abs(zone['distance']), -zone['score'], -zone['rr1'])

    def zone_order(zone):
        return (zone['priority'] == 'COUNTER_TREND', not zone['actionable'],
                not zone['in_zone'], *priority(zone))

    all_zones = sorted(planned, key=zone_order)
    candidate_rows = sorted((z for z in all_zones if z['actionable'] and
                             (engine.scanner.market_plan(z) or
                              engine.scanner.entry_window(z['side'], snapshot.price, z['entry'], atr15, cfg,
                                                          trend=z.get('trend_rel') == 'WITH_TREND') is None)),
                            key=priority)
    radar = {label: next((z for z in candidate_rows if z['side'] == side), None)
             for label, side in (('LONG', 1), ('SHORT', -1))}
    left, right = radar['LONG'], radar['SHORT']
    if left and right and (right['entry'] - left['entry'] < max(
            minimum_spread, .8 * max(left['risk'], right['risk']) if profile else 0.)
            or left['high'] >= right['low']):
        keep = min((left, right), key=priority)
        radar['SHORT' if keep is left else 'LONG'] = None
    primary = next((z for z in radar.values() if z and z['side'] == primary_side), None)
    if not primary:
        primary = min((z for z in radar.values() if z), key=priority, default=None)

    # Advisory tiers are deliberately separate from the executable radar.
    watch_levels, reserves = {}, {}
    for label, side in (('LONG', 1), ('SHORT', -1)):
        intact = [z for z in all_zones if z['side'] == side and not z.get('order_id')
                  and z['risk_gate'] not in ('STOP_ALREADY_BREACHED', 'EXHAUSTED_MOVE')
                  and side * (snapshot.price - (z['low'] if side == 1 else z['high'])) >= 0]
        nearby = [z for z in intact if engine.scanner.entry_window(
            side, snapshot.price, z['entry'], atr15, cfg, trend=z.get('trend_rel') == 'WITH_TREND') is None]
        watch = radar[label] or min(nearby, key=priority, default=None)
        watch_levels[label] = watch['id'] if watch else None
        leader = radar[label]
        distant = [z for z in intact if side * (snapshot.price - z['entry']) >
                   z['entry_max_distance'] + 1e-8 and
                   (not profile or not leader or abs(z['entry'] - leader['entry']) >=
                    max(1., .8 * max(z['risk'], leader['risk'])) - 1e-8)]
        reserve = min(distant, key=priority, default=None)
        reserves[label] = reserve['id'] if reserve else None
    watch_id = primary['id'] if primary else None
    reference_rows = [z for z in all_zones if z.get('gate_reason')]

    records = {row['id']:row for row in [*state.get('orders', []), *state.get('trades', [])]}
    recent = []
    kinds = {'PLACED', 'FILLED', 'CANCEL_REQUESTED', 'CANCELLED', 'EXPIRED', 'CLOSED',
             'TP1', 'TP1_PARTIAL', 'TP1_REACHED', 'TP2', 'PAPER_STOP_AMENDED', 'BREAKEVEN_ARMED', 'UNRESOLVED'}
    for event in state.get('events', []):
        if event.get('kind') not in kinds or event.get('ts', 0) > snapshot.now:
            continue
        row = records.get(event.get('order_id'), {})
        item = {key: event.get(key) for key in ('order_id', 'ts', 'kind', 'reason', 'net_r', 'price', 'stop')}
        item.update(side=row.get('side', event.get('side')), entry=row.get('entry', event.get('entry')), number=row.get('order_number'))
        if item['kind'] == 'CLOSED':
            item['price'] = row.get('exit', item['price'])
        item['reason_text'] = reasons.get(event.get('reason'), event.get('reason'))
        recent.append(item)
    recent.sort(key=lambda e: e['ts'])
    last_closed = next((e for e in reversed(recent) if e['kind'] == 'CLOSED'), None)
    def context_distance(zone):
        return max(zone['low'] - snapshot.price, snapshot.price - zone['high'], 0)
    context_zones = sorted((z for z in all_zones if zone_key(z) in context_keys and
                            z['risk_gate'] not in ('EXHAUSTED_MOVE', 'STOP_ALREADY_BREACHED') and
                            engine.scanner.entry_window(z['side'], snapshot.price, z['entry'], atr15, cfg) is None),
                           key=lambda z: (context_distance(z), z['timeframe'] != '1H'))
    context_bands = []
    for side in (1, -1):
        nearest = next((z for z in context_zones if z['side'] == side), None)
        if nearest:
            context_bands.append({k: nearest[k] for k in
                ('id', 'kind', 'timeframe', 'side', 'low', 'high', 'known_ts', 'score', 'grade')})

    def candles(rows, step):
        return [{'time':c.ts//1000,'open':c.open,'high':c.high,'low':c.low,'close':c.close}
                for c in rows[-180:] if c.confirmed and c.ts+step<=snapshot.now]

    system_mode = ('DATA_HOLD' if not healthy else 'ACTIVE' if any(o['status']=='OPEN' for o in orders) else
                   'PENDING' if any(o['status']=='PENDING' for o in orders) else
                   'CANCELLING' if orders else 'SCANNING')
    focus = None
    if healthy and oil_risk.enabled(cfg):
        focus = analysis.get('scenario_plan') or engine.scenario_planner.focus(
            snapshot,cfg,engine,regime,orders=plans,admitted=admitted,action=result.get('action'))
    ranked_zones = sorted((z for z in all_zones if (z['actionable'] or z.get('order_id')) and z.get('status') != 'CANCELLING'
                          and by_order.get(z.get('order_id'), {}).get('status') != 'CANCELLING'),
                         key=lambda z:(not bool(z.get('order_id')),
        not z['actionable'], z['side']!=primary_side if primary_side else False,
        z.get('risk_gate')=='EXHAUSTED_MOVE', abs(snapshot.price-z['entry']),-z['score']))
    scanner_top = [primary] if primary else []
    for zone in ranked_zones:
        if any(row['id']==zone['id'] for row in scanner_top):
            continue
        if profile and not zone.get('order_id') and any(
                engine.scanner.nearby_setup(zone, row, cfg.tick, atr15) for row in scanner_top):
            continue
        scanner_top.append(zone)
        if len(scanner_top)==5:
            break
    if focus and focus['primary'] and focus['primary']['status']=='DEVELOPING' and scanner_top:
        best = scanner_top[0]
        focus = {**focus,'primary':{**focus['primary'],'side':best['side'],'level':best['entry'],
                 'distance':abs(snapshot.price-best['entry']),'setup':best['kind'],
                 'label':best['kind'],'preview':{'entry':best['entry'],'stop':best['stop'],'tp1':best['tp1'],
                    'rr1':best['rr1'],'gate':best['gate_reason'],'preview_only':True},
                 'trigger':best.get('reason_text') or 'Чекаємо підтвердження рівня.'}}
        focus['developing_setup'] = focus['primary']
    entry_scenarios = (engine.scenario_planner.entry_scenarios(
        snapshot,cfg,engine,scanner_top,primary_side or (primary or {}).get('side'))
        if healthy and profile else {})
    reentry_history = {}
    if oil_risk.enabled(cfg):
        for side in (1, -1):
            previous = engine.bzu_policy.latest_same_side_exit(side, state.get('trades', []), snapshot.now)
            if previous:
                reentry_history[str(side)] = {'id': previous.get('id'), 'ts': previous['closed_ts'],
                                              'net_r': previous.get('net_r')}
    return {'schema':1, 'cockpit_schema':5, 'version':engine.REPORT_RELEASE,
            'instrument':cfg.instrument,'tick':cfg.tick,
            'snapshot_ts':snapshot.now,'ledger_ts':state.get('last_run_ts'),
            'price':snapshot.price,'ticker_ts':snapshot.ticker_ts,'market_regime':mode,
            'scenario_plan':focus,'entry_scenarios':entry_scenarios,'developing_setup':focus['developing_setup'] if focus else None,
            'stop_and_reverse': analysis.get('stop_and_reverse'),
            'trend_phase':regime.get('trend_phase'), 'wave_phase':regime.get('wave_phase'), 'structure_sync':regime.get('structure_sync') or None,
            'trend_matrix':{**(regime.get('trend_matrix') or {}), 'side_4h':regime.get('side_4h'),
                            'side_1h':regime.get('side_1h')} if regime else None,
            'status':'READY' if healthy else 'DATA_HOLD','system_mode':system_mode,'orders':orders,
            'execution':'SIGNAL_LEDGER','execution_audit':state.get('execution_audit'),
            'exchange_status':state.get('exchange_status'),
            'meta_calibration_status':state.get('meta_calibration_status','NO_PRIORS'),
            'daily':engine.day_stats(state,snapshot.now,cfg),
            'dealing_range':engine.scenario_planner.build(snapshot,cfg,engine)['range'] if healthy and profile else None,
            'brain_config':{
                'schema':1, 'missed_target_r':engine.bzu_policy.MISSED_TARGET_R,
                'market_band_atr':engine.scanner.MARKET_IMPULSE_BAND_ATR,
                'mfe_lock_steps':position_monitor.MFE_LOCK_STEPS,
                'late_mfe_lock_steps':position_monitor.LATE_MFE_LOCK_STEPS,
                'time_decay':{'wait_ms':cfg.max_hold_minutes*60_000,'step_ms':position_monitor.TIME_DECAY_STEP_MS,
                              'start_r':position_monitor.TIME_DECAY_START_R,
                              'increment_r':position_monitor.TIME_DECAY_INCREMENT_R},
                'full_target_min_r':engine.execution_rules.FULL_TARGET_MIN_R,
                'full_target_max_r':engine.execution_rules.FULL_TARGET_MAX_R,
                'adverse_lock_ms':engine.bzu_policy.ADVERSE_LOCK_BARS * oil_risk.M15,
                'maker_fee':engine.fee_rate(cfg,'maker'), 'taker_fee':engine.fee_rate(cfg,'taker'),
                'slippage_bps':cfg.slippage_bps, 'reentry_history':reentry_history,
                'market_min_rr':engine.scanner.MARKET_IMPULSE_MIN_RR,
                'limit_min_rr':engine.scanner.LIMIT_NEW_MIN_RR,
                'market_touch_atr':engine.scanner.BROWSER_MARKET_TOUCH_ATR,
                'limit_approach_atr':engine.scanner.BROWSER_LIMIT_APPROACH_ATR,
                'risk_floor':profile['floor'], 'entry_policy':'SERVER_FULL_POSITION',
                'ttl_ms':engine.bzu_policy.PENDING_LIMIT_TTL_MS,
                'worker_interval_ms':int(os.getenv('BZU_WORKER_INTERVAL_MS','0')),
                'uncertain':sum(bool(view.get('reconciliation')) for _,view in engine.slot_views(state)),
            } if oil_risk.enabled(cfg) else None,
            'occupied_slots':engine.occupied_slots(state)+len(engine.pending_plans(state)),
            'max_new_pending': 1 if profile else 2, 'organic_risk_profile': profile,
            'recent_events':recent[-20:], 'last_event':recent[-1] if recent else None,
            'last_closed':last_closed, 'context_bands':context_bands,
            'watch_levels':watch_levels, 'watch_primary_id':watch_id, 'reserve_levels':reserves,
            'candidates':candidate_rows, 'plan_pool':all_zones, 'unplanned_zones':unplanned, 'radar':radar, 'primary_candidate_id':primary['id'] if primary else None,
            'primary_bias':primary_side,'atr15':atr15,'min_structural_spread':minimum_spread,
            'entry_mode':entry_mode,'reference_levels':reference_rows[:48],
            'candidate_rejections':rejected[:100],
            'candidate_rules':{'min_score':oil_risk.STRUCTURAL_SCORE_MIN if profile else 60,
                               'direct_min_score':oil_risk.STRUCTURAL_SCORE_MIN if profile else 65,
                               'min_rr':oil_risk.ENTRY_TP1_MIN if profile else 1.4,
                               'max_rr1':oil_risk.TP1_MAX if profile else None,
                               'min_rr2':oil_risk.TP2_MIN if profile else 2.2,'wide_zone_usd':.45,'wide_zone_atr':.8,
                               'min_distance':.10,'max_distance':engine.scanner.max_zone_distance(atr15, cfg)},
            'reaction_config':{'execution_confirmation':'CLOSED_M15_REJECTION' if profile else None,'retracement':max(.06,3*cfg.tick),'max_sample_gap_ms':15000,
                               'min_samples':3,'ready_ttl_ms':15000,'max_chase_r':.25,
                               'min_net_rr':cfg.min_net_rr, 'taker_fee':engine.fee_rate(cfg,'taker')},
            'zones':all_zones,'scanner_top':scanner_top,
            'candles':{'5m':candles(engine.scanner.closed_five_minutes(snapshot),300_000),
                       '15m':candles(snapshot.candles.get('15m',[]),900_000)}}
