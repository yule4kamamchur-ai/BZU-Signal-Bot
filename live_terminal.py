"""Allowlisted, read-only public projection for the v12.26 Pro Cockpit.

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
            'entry': plan['entry'], 'stop': plan['stop'], 'initial_stop': plan.get('initial_stop', plan['stop']),
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
        })

    analysis = result.get('analysis', {})
    healthy = not analysis.get('health') and result.get('action') != 'DATA_HOLD'
    regime = engine.policy.market_regime(snapshot, cfg.tick, engine) if healthy else {}
    mode = {'BULLISH_TREND':'BULLISH_ACTIVE', 'BEARISH_TREND':'BEARISH_ACTIVE',
            'RANGE_LIQUIDITY_ROTATION':'RANGE'}.get(regime.get('state'), regime.get('state', 'UNKNOWN'))
    zones, candidates = {}, {}

    def zone_data(zone, side):
        low, high = zone.get('low'), zone.get('high')
        known = zone.get('known_ts')
        if (side not in (-1, 1) or not zone.get('kind') or not numeric(low) or
                not numeric(high) or not 0 < low < high or (numeric(known) and known > snapshot.now)):
            return None
        return {'kind': zone['kind'], 'timeframe': zone.get('timeframe'), 'side': side,
                'low': low, 'high': high, 'known_ts': known}

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
        for order in orders:
            if order['id'] == row.get('id') or (zone and order['side'] == side and zone['low'] <= order['entry'] <= zone['high']):
                item.update(status=order['status'], order_id=order['id'], entry_source='ORDER',
                            setup=order['setup'], score_source='CONTEXT', reason='ORDER_' + order['status'],
                            reason_text='чинний план за сигнальним журналом')
                for field in ('entry', 'stop', 'tp1', 'tp2', 'risk', 'score', 'grade'):
                    item[field] = order.get(field)
                break
        ranks = {'OPEN':6, 'PENDING':5, 'CANCELLING':4, 'CANDIDATE':3, 'WAIT_CONFIRMATION':2, 'BLOCKED':1}
        previous_candidate = candidates.get(key, {})
        if (ranks.get(item['status'], 0), item.get('score') or 0) < (ranks.get(previous_candidate.get('status'), 0), previous_candidate.get('score') or 0):
            continue
        candidates[key] = distance(item)
        if zone:
            previous = zones.get(zone_key(zone), {})
            if (ranks.get(item['status'], 0), item.get('score') or 0) >= (ranks.get(previous.get('status'), 0), previous.get('score') or 0):
                zones[zone_key(zone)] = item

    # Bound the public payload, independently for both directions. The scanner ledger retains every path.
    candidate_rows = []
    for side in (1, -1):
        rows = [z for z in candidates.values() if z['side'] == side]
        rows.sort(key=lambda z: (z['status'] == 'BLOCKED', abs(z['distance']), -(z.get('score') or 0)))
        candidate_rows.extend(rows[:48])
    all_zones = list(zones.values())
    radar = {}
    for label, side in (('LONG', 1), ('SHORT', -1)):
        def on_side(z):
            # Inside a zone also counts as an approach; spent zones behind price do not.
            return z['side'] == side and (z.get('low', z['entry']) <= snapshot.price if side == 1
                                          else z.get('high', z['entry']) >= snapshot.price)
        choices = [z for z in candidate_rows if on_side(z) and z['status'] not in ('BLOCKED', 'CANCELLING')]
        if not choices:
            choices = [z for z in all_zones if on_side(z) and z['status'] == 'OBSERVING']
        if not choices:
            choices = [z for z in candidate_rows if on_side(z) and z['status'] == 'BLOCKED']
        radar[label] = min(choices, key=lambda z: (abs(z['distance']), -(z.get('score') or 0)), default=None)

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
    return {'schema':1, 'cockpit_schema':1, 'version':engine.REPORT_RELEASE,
            'instrument':cfg.instrument,'tick':cfg.tick,
            'snapshot_ts':snapshot.now,'ledger_ts':state.get('last_run_ts'),
            'price':snapshot.price,'ticker_ts':snapshot.ticker_ts,'market_regime':mode,
            'status':'READY' if healthy else 'DATA_HOLD','system_mode':system_mode,'orders':orders,
            'execution':'SIGNAL_LEDGER','daily':engine.day_stats(state,snapshot.now,cfg),
            'occupied_slots':engine.occupied_slots(state)+len(engine.pending_plans(state)),
            'recent_events':recent[-20:], 'last_event':recent[-1] if recent else None,
            'candidates':sorted(candidate_rows,key=lambda z:abs(z['distance'])), 'radar':radar,
            'zones':sorted(all_zones,key=lambda z:abs(z['distance'])),
            'candles':{'5m':candles(engine.scanner.closed_five_minutes(snapshot),300_000),
                       '15m':candles(snapshot.candles.get('15m',[]),900_000)}}
