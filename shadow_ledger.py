"""Paper triple-barrier labels for admitted plans, independent of portfolio slots."""
from __future__ import annotations

from dataclasses import replace

MINUTE = 60_000
DAY = 1_440 * MINUTE
RETENTION = 120 * DAY
LABEL_SCHEMA = 'bzu-triple-barrier-m1-v1'


def ensure(state):
    return state.setdefault('shadow_trades', {'pending': {}, 'outcomes': [], 'seen': {}})


def watch(state, plans, snapshot, cfg, engine):
    if not plans:
        return
    ledger = ensure(state)
    for plan in plans:
        if not engine.bzu_policy.full_position(plan) or not plan.get('meta_context'):
            continue
        if plan['expires_ts'] <= snapshot.now:
            continue
        # Merge duplicate detectors of the same closed zone before assigning weight.
        zone = plan.get('entry_zone') or {}
        order_type = plan['order_type']
        trigger = (plan.get('market_trigger') or {}).get('known_ts') if order_type == 'MARKET' else None
        identity = engine.ident(plan['side'],order_type,trigger,zone.get('timeframe'),
                                zone.get('known_ts', plan['placed_ts']),
                                *(round(zone.get(k, plan['entry']) / cfg.tick) for k in ('low', 'high')))
        plan['calibration_id'] = str(identity)
        if identity in ledger['seen']:
            continue
        if not 0 < plan['risk'] or not 0 < plan['side'] * (plan['tp'] - plan['entry']):
            continue
        minute = (snapshot.now + MINUTE - 1) // MINUTE * MINUTE
        market = order_type == 'MARKET'
        fields = ('entry','stop','tp','risk','side','expires_ts','placed_ts','action_type','order_type',
                  'policy','exit_policy','post_only','invalidation','opening_sweep_confirmed',
                  'missed_target_fraction','limit_ttl_ms','missed_target_r','target_cancel_armed',
                  'entry_fee_rate','exit_fee_rate','slippage_bps','event_key','setup','htf_4h','range_pos',
                  'meta_context','meta_heuristic','meta_prob','risk_multiplier')
        row = {key: plan[key] for key in fields if key in plan}
        row.update(id=identity, calibration_id=identity, decision_ts=snapshot.now,
                   next_bar=minute if market else max(minute, plan.get('effective_ts', minute)),
                   filled_ts=snapshot.now if market else None, mfe_r=0., mae_r=0.)
        if market:
            row['deadline'] = minute + cfg.max_hold_minutes * MINUTE
        else:
            target = engine.bzu_policy.missed_target_level(row)
            if target is not None:
                row['target_cancel_armed'] = row['side']*(snapshot.price-target) < -1e-8
        ledger['seen'][identity] = snapshot.now
        ledger['pending'][identity] = row


def _outcome(row, ts, reason, exit_price=None, *, ambiguous=False):
    evidence = {k: row[k] for k in ('calibration_id','event_key','setup','side','htf_4h','range_pos',
               'meta_context','meta_heuristic','meta_prob','risk_multiplier','decision_ts','filled_ts',
               'mfe_r','mae_r','entry','risk','order_type') if k in row}
    evidence.update(id=row['id'], label_schema=LABEL_SCHEMA, source='SHADOW_PAPER', closed_ts=ts,
                    close_reason=reason, ambiguous_ohlc=ambiguous, net_r=None, session=row['meta_context'][-1],
                    side_4h=row['meta_context'][1])
    if exit_price is not None:
        entry, risk = row['entry'], row['risk']
        exit_fee = row.get('exit_fee_rate', 0.)
        entry_fee = row.get('entry_fee_rate', 0.)
        evidence['net_r'] = (row['side'] * (exit_price-entry)-entry*entry_fee-exit_price*exit_fee)/risk
        evidence['exit'] = exit_price
    return evidence


def _active_bar(row, bar, *, fill_bar=False):
    end = bar.ts + MINUTE
    side, entry, stop, target, risk = (row[k] for k in ('side','entry','stop','tp','risk'))
    favorable, adverse = (bar.high, bar.low) if side == 1 else (bar.low, bar.high)
    stop_hit = side*(adverse-stop) <= 1e-8
    target_hit = side*(favorable-target) >= -1e-8
    slip = row.get('slippage_bps', 0.) / 10_000
    # A known opening gap precedes the bar extremes. For an unordered double
    # touch use the stop. A limit fill candle cannot prove a later take profit.
    if not fill_bar and side*(bar.open-target) >= -1e-8:
        row['mfe_r'] = max(row['mfe_r'], side*(target-entry)/risk)
        return _outcome(row,end,'TAKE_PROFIT',target*(1-side*slip))
    if stop_hit:
        price = stop if fill_bar else min((bar.open,stop), key=lambda level: side*level)
        row['mae_r'] = max(row['mae_r'], side*(entry-price)/risk)
        return _outcome(row,end,'STOP',price*(1-side*slip),ambiguous=target_hit)
    if target_hit and not fill_bar:
        row['mfe_r'] = max(row['mfe_r'], side*(target-entry)/risk)
        return _outcome(row,end,'TAKE_PROFIT',target*(1-side*slip))
    row['mae_r'] = max(row['mae_r'], side*(entry-adverse)/risk)
    if not fill_bar:
        row['mfe_r'] = max(row['mfe_r'], side*(favorable-entry)/risk)
    if end >= row['deadline']:
        return _outcome(row,end,'TIME_BARRIER',bar.close*(1-side*slip))
    return None


def advance(state, snapshot, cfg, engine):
    ledger = state.get('shadow_trades')
    if not ledger or not ledger['pending']:
        return
    boundary = snapshot.now // MINUTE * MINUTE
    earliest = min(p['next_bar'] for p in ledger['pending'].values())
    bars = {b.ts:b for b in snapshot.candles.get('1m', ())
            if b.confirmed and earliest <= b.ts < boundary}
    entry_gate = {}
    for identity, row in list(ledger['pending'].items()):
        resolution = None
        while row['next_bar'] < boundary:
            ts = row['next_bar']
            if row['filled_ts'] is None and ts >= row['expires_ts']:
                resolution = _outcome(row,row['expires_ts'],'LIMIT_TTL')
                break
            bar = bars.get(ts)
            if bar is None:
                # Give the provider one extra bar to publish the newest close.
                if ts < boundary-MINUTE:
                    resolution = _outcome(row,boundary,'DATA_GAP')
                break
            row['next_bar'] += MINUTE
            if row['filled_ts'] is not None:
                resolution = _active_bar(row,bar)
            else:
                # These factual checks use only information known at bar open.
                ny = engine.execution_rules.early_ny(ts) and not row.get('opening_sweep_confirmed')
                gate_key = (ts//engine.oil_risk.H1,row['side'])
                if gate_key not in entry_gate:
                    at_open = replace(snapshot,now=ts,price=bar.open)
                    entry_gate[gate_key] = engine.policy.opposing_four_hour(at_open,row['side'],engine)
                if ny or entry_gate[gate_key] or engine.resting_limit_invalidated(row,bar.open):
                    resolution = _outcome(row,ts,'ENTRY_INVALIDATED')
                elif ts+MINUTE > row['expires_ts']:
                    penetrated = engine.paper_limit_touch(row,bar,cfg)
                    resolution = _outcome(row,ts+MINUTE,'LIMIT_TTL',ambiguous=penetrated)
                else:
                    target = engine.bzu_policy.missed_target_level(row)
                    if target is not None and engine.missed_target_on_bar(row,bar,cfg,target):
                        resolution = _outcome(row,ts+MINUTE,'MISSED_TARGET')
                    elif engine.paper_limit_touch(row,bar,cfg):
                        row.update(filled_ts=ts+MINUTE,deadline=ts+MINUTE+cfg.max_hold_minutes*MINUTE)
                        resolution = _active_bar(row,bar,fill_bar=True)
            if resolution is not None:
                break
        if resolution is not None:
            ledger['outcomes'].append(resolution)
            del ledger['pending'][identity]
    ledger['seen'] = {key: ts for key, ts in ledger['seen'].items() if ts >= snapshot.now-7*DAY}
    if not state.get('calibration_replay'):
        ledger['outcomes'] = [r for r in ledger['outcomes'] if r['closed_ts'] >= snapshot.now-RETENTION]
