"""Public allowlist for the static terminal. No credentials or internal ledger."""
from __future__ import annotations


def build(state, snapshot, result, cfg, engine):
    orders = []
    for plan in engine.open_plans(state):
        active = plan.get('status') == 'OPEN'
        orders.append({
            'id': plan['id'], 'number': plan.get('order_number'),
            'status': 'OPEN' if active else 'CANCELLING' if plan.get('cancel_requested_ts') else 'PENDING',
            'side': plan['side'], 'type': 'DIRECT' if plan.get('action_type') == 'ACTIVE_ENTRY' else 'LIMIT',
            'setup': plan.get('source_strategy') or plan.get('setup'),
            'entry': plan['entry'], 'stop': plan['stop'], 'tp1': plan.get('tp1', plan.get('tp')),
            'tp2': plan.get('tp2'), 'risk': plan['risk'],
            'score': plan.get('score'), 'grade': plan.get('rating'),
            'be_trigger': plan.get('tp1') if plan.get('policy') == engine.bzu_policy.POLICY else
                plan['entry'] + plan['side'] * plan['risk'] * plan.get('breakeven_trigger_r', 1.),
            'be_stop': plan.get('breakeven_price'),
            'structural_trail_trigger': (plan['entry'] + plan['side'] * plan['risk'] * plan['structural_trail_trigger_r']
                                         if 'structural_trail_trigger_r' in plan else None),
            'tp1_fraction': plan.get('partials', {}).get('TP1', 1.),
            'tp1_hit': bool(plan.get('tp1_hit')),
            'effective_ts': plan.get('effective_ts', plan.get('placed_ts')),
            'expires_ts': plan.get('expires_ts'), 'filled_ts': plan.get('filled_ts'),
            'pending_stop': (plan.get('pending_stop') or {}).get('price'),
        })

    analysis = result.get('analysis', {})
    healthy = not analysis.get('health') and result.get('action') != 'DATA_HOLD'
    regime = engine.policy.market_regime(snapshot, cfg.tick, engine) if healthy else {}
    mode = regime.get('state', 'UNKNOWN')
    mode = {'BULLISH_TREND':'BULLISH_ACTIVE', 'BEARISH_TREND':'BEARISH_ACTIVE',
            'RANGE_LIQUIDITY_ROTATION':'RANGE'}.get(mode, mode)
    zones = {}
    paths = analysis.get('poi_paths', [])
    for row in [*paths, *engine.open_plans(state)]:
        zone = row.get('entry_zone') or {}
        kind, low, high = zone.get('kind'), zone.get('low', 0), zone.get('high', 0)
        side = row.get('side')
        if not kind or side not in (-1,1) or not 0 < low < high:
            continue
        if row.get('reason') in ('ZONE_MITIGATED','ANCHOR_INVALIDATED','ANCHOR_TOO_OLD_TO_PLACE'):
            continue
        key = (kind, zone.get('timeframe'), side, low, high)
        zones[key] = {'kind':kind,'timeframe':zone.get('timeframe'),'side':side,'low':low,'high':high,
                      'known_ts':zone.get('known_ts'),'score':row.get('score'),'grade':row.get('rating')}
    market_zones = analysis.get('full_context', {}).get('zones', {})
    for key in ('order_blocks', 'fvg'):
        for zone in market_zones.get(key, []):
            side = cfg.core.side_sign(zone.side)
            if zone.mitigated or not 0 < zone.low < zone.high or side not in (-1,1):
                continue
            tf = zone.timeframe
            step = engine.TF.get(tf)
            if not step:
                continue
            known = zone.created_ts + 2 * step
            if known <= snapshot.now:
                zones.setdefault((zone.kind,tf,side,zone.low,zone.high),
                    {'kind':zone.kind,'timeframe':tf,'side':side,'low':zone.low,
                     'high':zone.high,'known_ts':known,'score':None,'grade':None})

    recent = []
    cancelled = {row['id']:row for row in state.get('orders', [])[-100:]
                 if row.get('status') in ('CANCELLED', 'EXPIRED')}
    for event in state.get('events', [])[-100:]:
        if event.get('kind') not in ('CANCELLED','EXPIRED') or event.get('ts',0) < snapshot.now - 90*60_000:
            continue
        row = cancelled.get(event.get('order_id'))
        if row:
            recent.append({'order_id':row['id'],'side':row['side'],'entry':row['entry'],
                           'ts':event['ts'],'kind':event['kind']})

    def candles(rows, step):
        return [{'time':c.ts//1000,'open':c.open,'high':c.high,'low':c.low,'close':c.close}
                for c in rows[-180:] if c.confirmed and c.ts+step<=snapshot.now]

    return {'schema':1,'version':engine.REPORT_RELEASE,'instrument':cfg.instrument,'tick':cfg.tick,
            'snapshot_ts':snapshot.now,'ledger_ts':state.get('last_run_ts'),
            'price':snapshot.price,'ticker_ts':snapshot.ticker_ts,'market_regime':mode,
            'status':'READY' if healthy else 'DATA_HOLD','orders':orders,
            'execution':'SIGNAL_LEDGER','daily':engine.day_stats(state,snapshot.now,cfg),
            'occupied_slots':engine.occupied_slots(state)+len(engine.pending_plans(state)),
            'recent_events':recent[-20:],
            'zones':sorted(zones.values(),key=lambda z:abs(snapshot.price-(z['high']+z['low'])/2)),
            'candles':{'5m':candles(engine.scanner.closed_five_minutes(snapshot),300_000),
                       '15m':candles(snapshot.candles.get('15m',[]),900_000)}}
