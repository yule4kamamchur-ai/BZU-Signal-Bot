"""Checklist v12.21 policy. Closed evidence, forward entries, honest R accounting.

This is a signal ledger, not an authenticated exchange execution adapter.
Minute OHLC cannot establish tick order: ambiguous bars always use stop first.
"""
from __future__ import annotations

import copy
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import directional_policy as direction
import entry_adverse_selection as adverse
import ict_methodology as ict
import limit_structure as structure
import research_zone_policy_1220 as zones
import setup_scanner as scanner
import oil_risk
import scenario_planner
import strategy_direct_entries

POLICY = 'BZU_LIVE_V12_25'
MOMENTUM_POLICY = 'BZU_MOMENTUM_V12_24'
MASTER_POLICY = 'BZU_MASTER_V12_23'
PRECISION_POLICY = 'BZU_PRECISION_V12_22'
LEGACY_POLICY = 'BZU_CHECKLIST_V12_21'
EXIT = 'TP1_50_M5_BREAKEVEN_TP2_50'
STOP_POLICY = 'TP1_PARTIAL_M5_CONFIRMED'
MINUTE = 60_000


def is_new(plan):
    return plan.get('policy') in (POLICY,MOMENTUM_POLICY,MASTER_POLICY,PRECISION_POLICY,LEGACY_POLICY)


def is_precision(plan):
    return plan.get('policy') in (POLICY,MOMENTUM_POLICY,MASTER_POLICY,PRECISION_POLICY)


def round_target(entry, target, side, tick):
    """Front-run the last .00/.50 barrier crossed by the original target.

    Use the terminal crossed barrier (75.03 -> 74.97), rather than the first
    barrier after entry (74.50). Never move a target further from entry.
    Invalid/collapsed geometry is rejected by the planner, never repaired by
    inventing a distant target.
    """
    e,t,grid=map(lambda x:Decimal(str(x)),(entry,target,tick))
    half=Decimal('.50');buffer=Decimal('.03')
    rounding=ROUND_FLOOR if side==1 else ROUND_CEILING
    barrier=(t/half).to_integral_value(rounding=rounding)*half
    value=t
    if side*(barrier-e)>0 and side*(t-barrier)>=0:
        value=barrier-side*buffer
    value=(value/grid).to_integral_value(rounding=rounding)*grid
    return float(value)


def round_stop(stop, side, tick):
    """Extend a stop within .01 of .00/.50 by another .05 outward.

    A 73.99 LONG stop becomes 73.94; an exact 74.00 stop becomes 73.95.
    Tick rounding always goes away from entry.
    """
    value,grid=Decimal(str(stop)),Decimal(str(tick))
    half=Decimal('.50')
    nearby=(value/half).to_integral_value()*half
    if abs(value-nearby)<=Decimal('.01'):
        value=min(value-Decimal('.05'),nearby-Decimal('.05')) if side==1 else max(value+Decimal('.05'),nearby+Decimal('.05'))
    rounding=ROUND_FLOOR if side==1 else ROUND_CEILING
    return float((value/grid).to_integral_value(rounding=rounding)*grid)


def oil_round_protection(cfg):
    return cfg.instrument.upper().startswith(('BZ-','BRENT','UKOIL'))


def session_gate(ts):
    """Requested risk windows, not an exchange trading-hours calendar.

    09:30 New York is an operator-selected US volatility window. Brent/OKX
    hours are different; DST is resolved by the IANA New York timezone.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    utc=datetime.fromtimestamp(ts/1000,timezone.utc)
    minute=utc.hour*60+utc.minute
    night=minute>=22*60 or minute<60
    ny=utc.astimezone(ZoneInfo('America/New_York'))
    opening=ny.weekday()<5 and 9*60+30<=ny.hour*60+ny.minute<9*60+45
    ny_minute = ny.hour * 60 + ny.minute
    eia = ny.weekday() == 2 and 10 * 60 + 15 <= ny_minute < 10 * 60 + 45
    reason = ('OIL_OVERNIGHT_BLACKOUT' if night else 'EIA_INVENTORY_WINDOW' if eia else
              'US_OPEN_CONFIRMATION_REQUIRED' if opening else 'SESSION_ALLOWED')
    return {'allowed': not (night or opening or eia), 'cancel_pending': night or opening or eia,
            'reason': reason, 'as_of_ts': ts}


def entry_cutoff(ts):
    return ts // 86_400_000 * 86_400_000 + 22 * 3_600_000


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


def technical_geometry(low, high, swing, side, a15, cfg, engine, deep=False, front_run=None, exact_entry=None):
    """One risk-bearing entry. Never move a structural stop toward the price."""
    narrow=high-low<=.35*a15
    first=(high+.02 if side==1 else low-.02) if narrow and not deep else (low+high)/2
    if front_run is not None and not deep:
        first = (high if side == 1 else low) + side * front_run
    first = exact_entry if exact_entry is not None else engine.round_tick(round(first, 10), cfg.tick, up=side==1)
    stop, risk, _ = scanner.normalized_zone_stop(first, swing, side, a15, cfg, engine)
    legs = [{'name':'L1', 'price':first, 'fraction':1., 'status':'PENDING'}]
    return first, stop, risk, legs


def protected_targets(context, snapshot, anchor, cfg, engine, entry, stop, side, a15, active, zone=None, *, trend=False, market_trigger=None):
    """Price structural targets on the final entry and execution fee contract."""
    risk = side * (entry - stop)
    organic = oil_risk.enabled(cfg)
    targets = engine.full_targets({**context, 'organic_oil_targets': organic}, snapshot, anchor.side, entry, cfg)
    geometry = (scanner.organic_targets(entry, risk, side, targets, snapshot, cfg.tick, engine, trend=trend) if organic else
                scanner.planned_targets(entry, risk, side, targets, snapshot, cfg, engine, zone or {}, a15))
    obstacle, room_r = (oil_risk.structural_room(side, entry, risk, scenario_planner.structure_levels(snapshot, engine))
                        if organic else (None, None))
    if room_r is not None and room_r < 1.35-1e-8:
        return None, 'BLOCKED_NO_ROOM_TO_STRUCTURE'
    tp1_gate = oil_risk.entry_tp1_error(side, entry, risk, geometry['tp1'])
    if tp1_gate:
        return None, tp1_gate
    known = market_trigger['known_ts'] if market_trigger else (zone or {}).get('known_ts', snapshot.now)
    if scanner.target_crossed(side, snapshot, geometry['tp1'], known):
        return None, 'EXHAUSTED_MOVE'
    projection = geometry.get('target_projection', {})
    if not geometry['path_clear']:
        return None, 'INSUFFICIENT_ROOM'
    if not geometry['structural_tp1'] and not projection.get('tp1'):
        return None, 'STRUCTURAL_TP1_REQUIRED'
    if not geometry['structural_tp2'] and not projection.get('tp2'):
        return None, 'STRUCTURAL_TP2_REQUIRED'
    tp1, tp2 = geometry['tp1'], geometry['tp2']
    entry_fee = engine.fee_rate(cfg, 'taker' if active else 'maker')
    exit_fee = engine.fee_rate(cfg, 'taker')
    rr2 = side * (tp2 - entry) / risk
    costs = (entry * entry_fee + tp2 * exit_fee) / risk
    rr1 = side * (tp1 - entry) / risk
    if organic:
        if not (oil_risk.TP1_MIN - 1e-8 <= rr1 <= oil_risk.TP1_MAX + 1e-8 and
                oil_risk.TP2_MIN - 1e-8 <= rr2 <= oil_risk.TP2_MAX + 1e-8):
            return None, 'ORGANIC_TARGET_RANGE'
        if rr1 <= (entry * entry_fee + tp1 * exit_fee) / risk:
            return None, 'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS'
    elif rr2 - costs + 1e-9 < cfg.min_net_rr:
        return None, 'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS'
    return {**geometry, 'rr2': rr2, 'costs_r': costs, 'entry_fee': entry_fee,
            'exit_fee': exit_fee, 'raw_targets': {'tp1': tp1, 'tp2': tp2}, 'obstacle': obstacle, 'room_r': room_r}, 'ACCEPTED'


ADVERSE_LOCK_BARS = 16


def adverse_reentry_reason(side, trades, snapshot, cfg, engine):
    """Reconstruct the same-side cooldown from observed journal exits and closed M15."""
    if not oil_risk.enabled(cfg):
        return None
    duration = ADVERSE_LOCK_BARS * oil_risk.M15
    previous = max((t for t in trades if t.get('side') == side
                    and 0 < (t.get('closed_ts') or 0) <= snapshot.now),
                   key=lambda t: t['closed_ts'], default=None)
    if (previous is None or ((previous.get('net_r') or 0) >= 0
            and previous.get('close_reason') != 'CLOSED_ADVERSE_STRUCTURE_SHIFT') or not previous.get('entry')
            or snapshot.now >= previous['closed_ts'] + duration):
        return None
    if previous.get('close_reason') in ('STOP','STOP_GAP','SL','STOP_LOSS'):
        stop = previous.get('stop_at_close') or previous.get('stop') or previous.get('initial_stop')
        if stop is not None and ict.stop_sweep_recovery(direction.closed(snapshot,'15m')[-96:],
                side,previous['closed_ts'],stop,cfg.tick,engine):
            return None
    if direction.bos_recovers_level(snapshot, side, previous['closed_ts'], None, cfg.tick, engine):
        return None
    return 'ADVERSE_STRUCTURE_LOCKOUT'


def breaker_structure_error(plan, snapshot, cfg, engine):
    zone = plan.get('entry_zone') or {}
    if not oil_risk.enabled(cfg) or 'BREAKER_BLOCK' not in (plan.get('source_strategy'), zone.get('kind')):
        return None
    side = plan['side']
    direction_side, averages = direction.m15_ema_structure(snapshot, cfg.tick, engine, breaker_side=side)
    if direction_side == -side and all(direction_side*(snapshot.price-ema)>0 for ema in averages):
        return 'M15_STRUCTURE_OPPOSES_ENTRY'
    rows = direction.closed(snapshot, '15m')[-96:]
    known = (zone.get('wave_proof') or {}).get('impulse_ts')
    index = next((i for i,c in enumerate(rows) if c.ts==known), 0)
    if not ict.breaker_evidence(rows,index,engine.pivots(rows,strength=1),side,cfg.tick):
        return 'BREAKER_SWEEP_FVG_REQUIRED'
    return None


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
    if zone.get('timeframe') not in scanner.PRIMARY or zone.get('kind') == 'MICRO_BOS_FVG':
        return None, 'MICRO_ZONE_EXCLUDED'
    if not scanner.zone_direction_matches(zone, side):
        return None, 'ZONE_DIRECTION_MISMATCH'
    regime = direction.execution_regime(snapshot, cfg, engine)
    breaker_error = breaker_structure_error(
        {'side': side, 'source_strategy': anchor.evidence.get('source_strategy'), 'entry_zone': zone},
        snapshot, cfg, engine)
    if breaker_error:
        return None, breaker_error
    origin = scanner.zone_origin(zone, snapshot, side, cfg.tick, engine, cfg, regime)
    if not origin['confirmed']:
        if zone.get('wave_proof'):
            return None, origin['reason']
        sweep_zone, sweep_origin = scanner.sweep_reclaim_zone(anchor, snapshot, side, cfg, engine)
        if sweep_origin is None:
            diag['zone_origin'] = origin
            return None, origin['reason']
        zone, origin = sweep_zone, sweep_origin
    impulse_known = origin.get('displacement_known_ts') or zone['known_ts']
    zone = {**zone, 'side': side, 'impulse_known_ts': impulse_known}
    low, high = zone['low'], zone['high']
    # Preserve the observed zone even when Impulse Brake stops planning early.
    diag['entry_zone'] = dict(zone)
    diag['zone_origin'] = origin
    zone_error = scanner.standalone_zone_error({**zone, 'side': side}, snapshot, cfg, engine, a15, regime.get('state'))
    if zone_error:
        return None, zone_error
    trend = oil_risk.enabled(cfg) and scanner.wave.trend_zone(zone, regime, origin)
    relation = 'WITH_TREND' if trend else None
    counter_confirmation = None
    sweep_override = direction.reversal_sweep_override(side, zone, snapshot, cfg.tick)
    local_reversal = (sweep_override and regime.get('trend_anchor_side') != -side
                      and not regime.get('side_1h') == regime.get('side_4h') == -side)
    breakdown = zone['kind'] == scanner.wave.BREAKDOWN and origin.get('wave_confirmed')
    multibar = breakdown or bool(sweep_override and zone['kind'] in ('MULTIBAR_SWEEP','RECLAIM_BREAKER','SR_FLIP_RECLAIM')
                    and origin.get('wave_confirmed'))
    reclaim = multibar and bool(zone['wave_proof'].get('structural_reclaim'))
    linked_sweep = None
    if oil_risk.enabled(cfg) and zone['kind'] in (
            'DEMAND_OB','SUPPLY_OB','SESSION_OB','BREAKER_BLOCK','BULLISH_BREAKER','BEARISH_BREAKER'):
        linked_sweep = next((z for z in scanner.wave.zones(snapshot,cfg,engine,regime)
            if z['kind']=='MULTIBAR_SWEEP' and z['side']==side and z['wave_proof']['exhausted_adr']
            and abs(z['known_ts']-zone['known_ts']) <= 3*scanner.wave.M15
            and max(low,z['low']) <= min(high,z['high'])), None)
    if linked_sweep:
        origin = {**origin,'sweep':linked_sweep['sweep']}
        sweep_override = multibar = True
    exhaustion_bonus = (oil_risk.enabled(cfg) and sweep_override and (
        bool((zone.get('wave_proof') or {}).get('exhausted_adr')) or linked_sweep is not None or
        scanner.wave.exhausted_adr(structure.closed(snapshot,'15m'),zone.get('sweep') or {},
                                       oil_risk.snapshot_profile(snapshot)['adr_d1'])))
    if multibar:
        relation = scanner.REVERSAL_RELATION
    if oil_risk.enabled(cfg) and not (local_reversal or multibar):
        anchor_side = (regime.get('trend_anchor_side') or direction.regime_side(regime) or
                       direction.regime_side({'state': regime.get('trend_state')}))
        if anchor_side == -side:
            if not scanner.wave.reversal_override(side, zone, snapshot, cfg.tick):
                return None, 'MACRO_REVERSAL_REQUIRED'
            sweep = zone['sweep']
            shift = direction.structural_flip(snapshot, side, sweep['raid_ts'], cfg.tick, engine)
            if not shift['confirmed']:
                return None, 'COUNTER_CHOCH_REQUIRED'
            bars = scanner.closed_five_minutes(snapshot)
            boundary = high if side == 1 else low
            if len(bars) < 2:
                return None, 'COUNTER_M5_REQUIRED'
            previous, confirm = bars[-2:]
            m5_known = confirm.ts + 300_000
            previous_edge = previous.high if side == 1 else previous.low
            if (m5_known < shift['known_ts'] or snapshot.now - m5_known >= 2 * 300_000
                    or confirm.ts - previous.ts != 300_000 or side * (confirm.close - confirm.open) <= 0
                    or side * (confirm.close - boundary) < cfg.tick - 1e-8
                    or side * (confirm.close - previous_edge) < cfg.tick - 1e-8):
                return None, 'COUNTER_M5_REQUIRED'
            relation = 'COUNTER_TREND'
            counter_confirmation = {'choch': shift, 'm5_known_ts': m5_known}
    active_side = regime.get('active_structure', {}).get('side')
    aligned = direction.momentum_aligned(snapshot, side)
    momentum_zone = zone.get('kind') in scanner.ORGANIC_MOMENTUM
    countertrend = active_side != side and any(regime[key] == -side for key in ('side_1h', 'side_4h'))
    organic_formation = momentum_zone or zone.get('kind') == scanner.ORGANIC_REVERSAL or origin.get('wave_confirmed')
    tf = zone['timeframe']
    distal = low if side == 1 else high
    structural = min(anchor.invalidation, distal) if side == 1 else max(anchor.invalidation, distal)
    if not organic_formation:
        swings = [p for p in engine.pivots(structure.closed(snapshot, tf))
                  if p['kind'] == ('LOW' if side == 1 else 'HIGH')
                  and p['known_ts'] + engine.TF[tf] <= zone['known_ts']
                  and side * ((low if side == 1 else high) - p['level']) >= 0]
        structural = (min if side == 1 else max)(
            anchor.invalidation, low if side == 1 else high,
            swings[-1]['level'] if swings else anchor.invalidation)
    preferred = (anchor.level if organic_formation else (low + high) / 2 if countertrend else None)
    oil = (oil_risk.snapshot_profile(snapshot), oil_risk.macro_anchors(snapshot, cfg, engine, side)) if oil_risk.enabled(cfg) else None
    if oil:
        entry, entry_basis = (zone['reference'],'BREAKDOWN_MICRO_RETEST') if breakdown else (
            strategy_direct_entries.adaptive_limit(snapshot,zone,origin,side,cfg.tick,engine))
        stop, risk, organic_risk = oil_risk.stop_geometry(
            entry, structural, side, *oil, cfg.tick, engine, allow_local=bool(trend or multibar or local_reversal))
        inside = low <= snapshot.price <= high
        gate = scenario_planner.entry_error(snapshot,cfg,engine,side,entry,zone,
            trend=bool(breakdown or trend or scanner.continuation_setup(anchor.evidence.get('source_strategy'),zone['kind'],anchor.setup_type)))
        if gate:
            diag.update(entry=entry, stop=stop, risk=risk)
            return None, gate
    else:
        entry, stop, risk, entry_basis, _, inside = scanner.zone_entry_geometry(
            low, high, snapshot.price, structural, side, a15, cfg, engine, preferred=preferred, oil=oil, allow_local=local_reversal)
        organic_risk = oil_risk.stop_geometry(entry, structural, side, *oil, cfg.tick, engine,
                                             allow_local=local_reversal)[2] if oil else None
    legs = [{'name':'L1', 'price':entry, 'fraction':1., 'status':'PENDING'}]
    breaker = scanner.m5_breaker({**zone, 'side': side}, snapshot, cfg.tick)
    reaction_only = not oil and relation is None and (inside or bool(breaker))
    entry_condition = ('IN_ZONE_M5_RECLAIM' if reaction_only and inside else
                       'M5_BREAKER_RETEST' if reaction_only and breaker else 'RETEST')
    raw_stop=stop
    min_risk, max_risk = ((organic_risk['floor'], organic_risk['cap']) if organic_risk else
                         scanner.zone_risk_bounds(a15, cfg))
    if not oil and oil_round_protection(cfg):
        protected_stop = round_stop(stop, side, cfg.tick)
        if side * (entry - protected_stop) <= max_risk + 1e-9:
            stop = protected_stop
            risk = side * (entry - stop)
    if side*(entry-anchor.invalidation)<=0:
        return None, 'INVALID_STRUCTURAL_FALSIFIER'
    approach = (adverse.approach_velocity(snapshot,side,zone,entry,cfg.tick,engine) if oil else
                adverse.approach(snapshot,a15,side,zone,cfg.tick,stop=stop,trend=trend or multibar))
    diag.update(entry=entry, level=entry, stop=stop, risk=risk, entry_zone=zone,
                approach=approach, planning_policy=POLICY, entry_basis=entry_basis,
                entry_condition=entry_condition, organic_risk=organic_risk)
    if organic_risk and organic_risk['gate']:
        mark('VALID_STOP', False)
        return None, organic_risk['gate']
    valid_stop = bool(organic_risk) or (min_risk - 1e-9 <= risk <= max_risk + 1e-9
                  and side * (structural - stop) >= max(cfg.tick, .25 * a15) - 1e-9)
    mark('VALID_STOP', valid_stop)
    if not valid_stop:
        return None, 'STRUCTURAL_STOP_TOO_WIDE'
    if approach.get('blocked'):
        diag['status']='WATCHING'
        return None, 'CONFIRMATION_REQUIRED' if oil else 'TREND_M5_SPIKE' if trend else 'IMPULSE_CONFIRMATION_REQUIRED'
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
    quote = snapshot.ask if side == 1 else snapshot.bid
    market_trigger = (adverse.breakdown_market_trigger(snapshot,zone,entry,risk,quote,cfg.tick) if breakdown else
        adverse.rejection_trigger(snapshot,side,zone,entry,risk,quote,cfg.tick) if oil else None)
    if breakdown and not market_trigger and snapshot.price < entry - adverse.breakdown_limit_distance(zone):
        return None, 'DISTANT_BREAKDOWN_RETEST'
    if market_trigger:
        entry_condition = 'RETEST'
    if not market_trigger and scanner.entry_window(side, snapshot.price, entry, a15, cfg, minimum=False, trend=trend or multibar) == 'DISTANT_ZONE':
        return None, 'DISTANT_ZONE'
    test_zone = {**zone, 'low': entry, 'high': entry} if trend else zone
    audit = zones.first_test(test_zone, minute, side, entry, risk, snapshot.now)
    diag['zone_audit'] = audit
    mark('FRESH_LEVEL', bool(market_trigger) or not audit['mitigated'])
    if not market_trigger and (audit['mitigated'] or (trend and audit['touched'])):
        return None, 'ZONE_MITIGATED'
    passive = scanner.passive_gate(entry, side, snapshot.bid, snapshot.ask, cfg.tick)['passive']
    confirmed_retest = bool(passive and audit['touched'] and
                            (approach.get('confirmation_known_ts') or 0) > audit['touch_ts'])
    can_wait = passive and (not audit['touched'] or confirmed_retest or reaction_only or counter_confirmation)
    if reaction_only and side * (snapshot.price - entry) < cfg.tick - 1e-8:
        return None, 'ZONE_NOT_AHEAD_OF_PRICE'
    allowed, why = direction.regime_admission(
        side, entry, regime, snapshot, cfg.tick, engine, linked_sweep or zone, require_macro=bool(oil))
    mark('HTF_ALIGNMENT', allowed)
    if not allowed:
        return None, why

    organic = bool(anchor.evidence.get('organic_confluence') or momentum_zone)
    trending = active_side == side or (aligned and (organic or 'FVG' in zone.get('kind', '')))
    direct = strategy_direct_entries.momentum_quote(
        snapshot, side, entry, stop, zone, origin, cfg.tick) if not oil and trending and not organic_formation and not reaction_only and relation is None else None
    if not oil and not organic_formation and not can_wait and direct is None and not reaction_only and relation is None:
        catchup = scanner.catch_up(snapshot, entry, stop, side, zone['known_ts'], cfg.tick, zone)
        diag['catch_up'] = catchup
        if catchup['allowed']:
            direct = {'price': snapshot.ask if side == 1 else snapshot.bid,
                      'move_r': catchup['move_r'], 'pattern': 'CONFIRMED_RETEST'}
    action = 'NEW_PENDING_LIMIT'
    geometry = None
    if market_trigger:
        if breakdown:
            gate = scenario_planner.entry_error(snapshot,cfg,engine,side,quote,zone,trend=True)
            if gate:
                return None, gate
        # Re-price at executable bid/ask. Retain the structural boundary and
        # preserve the original stop whenever it still fits the risk budget.
        quote_stop, quote_risk, quote_profile = oil_risk.stop_geometry(
            quote, organic_risk['boundary'], side, *oil, cfg.tick, engine, allow_local=True)
        if quote_profile['gate']:
            return None, quote_profile['gate']
        fixed_risk = side * (quote - stop)
        if (quote_profile['floor'] - 1e-8 <= fixed_risk <= quote_profile['cap'] + 1e-8
                and side * (quote_profile['boundary'] - stop) >= quote_profile['buffer'] - 1e-8):
            quote_stop, quote_risk = stop, fixed_risk
        geometry, why = protected_targets(
            context, snapshot, anchor, cfg, engine, quote, quote_stop, side, a15, active=True,
            zone=zone, trend=trend or exhaustion_bonus or reclaim, market_trigger=market_trigger)
        if geometry is None:
            return None, why
        entry, stop, risk = quote, quote_stop, quote_risk
        organic_risk = quote_profile
        action = 'ACTIVE_ENTRY'
        entry_basis = 'EXECUTABLE_QUOTE'
        legs = [{'name': 'ACTIVE', 'price': entry, 'fraction': 1., 'status': 'PENDING'}]
        diag.update(entry=entry, level=entry, stop=stop, risk=risk, organic_risk=organic_risk,
                    entry_basis=entry_basis, market_trigger=market_trigger)
    if direct:
        quote = direct['price']
        quote_stop, quote_risk, quote_profile = (oil_risk.stop_geometry(
            quote, structural, side, *oil, cfg.tick, engine) if oil else (stop, side * (quote - stop), None))
        direct_allowed, why = direction.regime_admission(
            side, quote, regime, snapshot, cfg.tick, engine, linked_sweep or zone, require_macro=bool(oil))
        if not direct_allowed:
            direct_failure = why
        elif quote_profile and (quote_profile['gate'] or side * (quote_stop - stop) > 1e-8):
            direct_failure = quote_profile['gate'] or 'STRUCTURAL_STOP_TOO_WIDE'
        elif not quote_profile and not min_risk - 1e-9 <= quote_risk <= max_risk + 1e-9:
            direct_failure = 'STRUCTURAL_STOP_TOO_WIDE'
        else:
            geometry, direct_failure = protected_targets(
                context, snapshot, anchor, cfg, engine, quote, quote_stop, side, a15, active=True, zone=zone)
        if geometry:
            entry, stop, risk = quote, quote_stop, quote_risk
            organic_risk = quote_profile
            action = 'ACTIVE_ENTRY'
            legs = [{'name': 'ACTIVE', 'price': entry, 'fraction': 1., 'status': 'PENDING'}]
            diag.update(entry=entry, level=entry, stop=stop, risk=risk, direct_entry=direct, organic_risk=organic_risk)
        elif not can_wait:
            return None, direct_failure
    if geometry is None:
        if anchor.setup_type == 'SESSION_MEAN_RECLAIM':
            return None, 'SESSION_MEAN_LIMIT_DISABLED'
        if not can_wait:
            return None, 'WAIT_RETEST'
        geometry, why = protected_targets(
            context, snapshot, anchor, cfg, engine, entry, stop, side, a15, active=False, zone=zone, trend=trend or exhaustion_bonus or reclaim)
        if geometry is None:
            mark('TARGET_RR', False)
            return None, why
        distance_gate = scanner.entry_window(side, snapshot.price, entry, a15, cfg, trend=trend or multibar)
        if (zone.get('wave_proof') and zone['kind'] in ('BULLISH_BREAKER', 'BEARISH_BREAKER')
                and distance < .20 - 1e-8 and not reaction_only):
            distance_gate = distance_gate or 'ENTRY_TOO_CLOSE'
        if distance_gate and not ((reaction_only or breakdown) and distance_gate == 'ENTRY_TOO_CLOSE'):
            return None, distance_gate
    volume = scanner.formation_volume(zone, snapshot)
    mark('PASSIVE_LIMIT', True)
    mark('TARGET_RR', True)
    tp1, tp2, rr2 = geometry['tp1'], geometry['tp2'], geometry['rr2']
    has_runner = side * (tp2 - tp1) > 0
    diag['round_level_protection'] = {'original_stop': raw_stop, 'stop': stop,
        'original_targets': geometry['raw_targets'], 'tp1': tp1, 'tp2': tp2}
    # Remaining models are evidence only. Their individual return values do
    # not veto the setup. All 24 named ICT confirmations are still validated.
    effective = snapshot.now if action == 'ACTIVE_ENTRY' else ((snapshot.now+MINUTE-1)//MINUTE)*MINUTE
    cutoff = entry_cutoff(snapshot.now)
    horizon = anchor.expires_ts if trend or multibar else effective + cfg.ttl_minutes * 60_000
    expires = min(horizon, cutoff)
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
         'source_strategy':anchor.evidence.get('source_strategy') or (zone['kind'] if trend else None),
         'planning_policy':POLICY, 'entry_policy':'CHECKLIST_CONFIRMED',
         'detector_confirmed':True, 'anchor_id':anchor.id, 'anchor_kind':anchor.kind,
         'anchor_proof':proof, 'pattern_proof':copy.deepcopy(proof.get('recovery_proof',{})),
         'origin_broke_structure':bool(origin.get('break_known_ts')), 'organic_confluence':organic,
         'zone_freshness':'UNMITIGATED', 'm15_momentum_aligned':aligned,
         'direct_entry':diag.get('direct_entry'),
         'anchor_known_ts':zone['known_ts'], 'zone_created_ts':zone['known_ts'],
         'entry_zone':zone, 'zone_audit':audit, 'zone_origin':origin, 'approach':approach,
         'entry_basis':entry_basis, 'entry_condition':entry_condition, 'reaction_only':reaction_only,
         'round_level_protection':copy.deepcopy(diag['round_level_protection']),
         'entry':entry, 'limit_price':entry, 'entry_legs':legs,
         'order_type':'MARKET' if action == 'ACTIVE_ENTRY' else 'LIMIT',
         'market_trigger':market_trigger, 'obstacle':geometry.get('obstacle'), 'structural_room_r':geometry.get('room_r'), 'checkpoints':geometry.get('checkpoints', []),
         'stop':stop, 'initial_stop':stop, 'invalidation':structural,
         'risk':risk, 'organic_risk':organic_risk, 'tp':tp2, 'tp1':tp1, 'tp2':tp2, 'tp3':tp2,
         'rr1':side*(tp1-entry)/risk, 'rr2':rr2, 'rr3':rr2, 'gross_rr':rr2,
         'partials':{'TP1':.5,'TP2':.5} if has_runner else {'TP1':1.},
         'exit_policy':EXIT, 'tp1_alignment':geometry['tp1_alignment'],
         'tp2_alignment':geometry['tp2_alignment'], 'formation_volume':volume,
         'tp1_source':geometry['tp1_source'], 'tp2_source':geometry['tp2_source'],
         'target_projection':geometry.get('target_projection'),
         'reversal_sweep_override': sweep_override, 'local_reversal_override': local_reversal or multibar,
         'structure_reclaim':reclaim, 'linked_reversal_zone':linked_sweep,
         'reversal_exhaustion_bonus':10 if exhaustion_bonus else 0,
         'reversal_override':local_reversal or multibar or bool(origin.get('wave_confirmed') and
                                  scanner.wave.reversal_override(side, zone, snapshot, cfg.tick, require_macro=bool(oil))),
         'breakeven_trigger_r':side*(tp1-entry)/risk,
         'target_management':EXIT, 'tp1_decision_gate':False,
         'exit_path_barriers':[], 'full_exit_candidates':[],
         'net_rr':rr2-geometry['costs_r'], 'costs_r':geometry['costs_r'],
         'stop_distance_pct':risk/entry*100, 'tp1_distance_pct':abs(tp1-entry)/entry*100,
         'include_commission':cfg.include_commission,
         'entry_fee_rate':geometry['entry_fee'], 'exit_fee_rate':geometry['exit_fee'], 'slippage_bps':cfg.slippage_bps,
         'placed_ts':snapshot.now, 'effective_ts':effective, 'expires_ts':expires,
         'expiry_reason':'SESSION_ENDED' if expires==cutoff else 'LIMIT_TTL',
         'max_hold_minutes':cfg.max_hold_minutes, 'intraday':True,
         'time_exit_enabled':False,
         'placement_price':snapshot.price, 'target_cancel_armed':side*(snapshot.price-tp1)<0,
         'action_type':action, 'execution_source':action, 'entry_stage':'CORE',
         'preposition_window':'.08..2.20' if trend else '.10..min(.65,1.2*ATR15)', 'preposition_distance':distance,
         'queue_execution_policy':'PASSIVE_ARM_V2', 'execution_timeframe':'1m',
         'readiness_pct':100., 'entry_checks':copy.deepcopy(diag['entry_checks']),
         'htf_1h':regime['side_1h'], 'htf_4h':regime['side_4h'],
         'market_regime':regime, 'session_liquidity':ict.session_liquidity(snapshot),
         'trend_rel':relation, 'trend_phase':regime.get('trend_phase'),
         'counter_confirmation':counter_confirmation, 'entry_max_distance':scanner.TREND_LIMIT_MAX if trend or multibar else scanner.max_zone_distance(a15, cfg),
         'score':0., 'probability':None, 'probability_status':'UNVALIDATED_SCORE_NOT_WR',
         'evidence':[anchor.reason], 'candidate':{}, 'parent_keys':[],
         'status':'PENDING', 'last_bar':-1, 'version':engine.VERSION}
    p['breakeven_price'] = breakeven_price(p, engine.execution_config(p, cfg), engine)
    return p, 'ACCEPTED'


def first_fill(plan, candle, engine, cfg, step):
    """Initialize actual filled quantity without counting an unfilled second half."""
    p = copy.deepcopy(plan)
    p.update(status='OPEN', filled_ts=candle.ts, opened_at=engine.iso(candle.ts),
        fill_time_status=f'WITHIN_{step//60_000}M_BAR', last_bar=-1,
        remaining=0., filled_fraction=0., realized_gross_r=0., realized_exit_fees_r=0.,
        realized_legs=[], mfe_r=0., mae_r=0., max_favorable_r=0., max_adverse_r=0.,
        bars_in_trade=0, time_in_trade_ms=0, strong_closes=0,
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
        if p.get('policy') in (POLICY, MOMENTUM_POLICY, MASTER_POLICY):
            # One full-size leg: retain exact tick-aligned, protected targets.
            return
        p['tp1'] = engine.round_tick(p['entry']+p['side']*p['risk']*p.get('rr1',1.),
                                    cfg.tick,up=p['side']==1 if is_precision(p) else p['side']==-1)


def breakeven_price(p, cfg, engine):
    """Cover stored entry and exit fees and modeled stop slippage per unit."""
    side = p['side']
    entry_fee = p.get('entry_fee_rate', engine.fee_rate(cfg, 'maker')) if cfg.include_commission else 0.
    exit_fee = engine.fee_rate(cfg, 'taker')
    price = p['entry'] * (1 + side * entry_fee) / (1 - side * exit_fee)
    price /= 1 - side * cfg.slippage_bps / 10_000
    return engine.round_tick(round(price, 10), cfg.tick, up=side == 1)


def partial_tp1_time(position):
    """A TP1 price touch alone never proves a partial execution."""
    filled = position.get('filled_fraction', 1.)
    remaining = position.get('remaining', 1.)
    tp1 = position.get('tp1')
    if not position.get('tp1_hit') or not 0 < remaining < filled or tp1 is None:
        return None
    minimum = oil_risk.TP1_MIN if oil_risk.is_organic(position) else 1.40
    if position['side'] * (tp1 - position['entry']) < minimum * position['risk'] - 1e-8:
        return None
    return next((leg['ts'] for leg in position.get('realized_legs', [])
                 if leg.get('target') == 'TP1' and 0 < leg.get('fraction', 0) < filled
                 and isinstance(leg.get('ts'), int)), None)


def protective_amendment(position):
    amendment = position.get('pending_stop')
    if not amendment or amendment.get('policy') != STOP_POLICY:
        return None
    partial_ts = partial_tp1_time(position)
    known = position.get('be_confirmed_ts')
    if partial_ts is None or not isinstance(known, int) or known < partial_ts + 900_000:
        return None
    effective, price = amendment.get('effective_ts'), amendment.get('price')
    if not isinstance(effective, int) or effective < known:
        return None
    return amendment if isinstance(price, (int, float)) and math.isfinite(price) and price > 0 else None


def apply_protective_stop(position, bar_start):
    if not position.get('pending_stop'):
        return
    amendment = protective_amendment(position)
    if amendment is None:
        # Retire legacy unexecuted amendments. Preserve the currently effective SL.
        position['retired_stop_amendment'] = position.pop('pending_stop')
        position['breakeven_armed'] = False
        return
    if bar_start >= amendment['effective_ts']:
        if position['side'] * (amendment['price'] - position['stop']) > 0:
            position['stop'] = amendment['price']
        position.pop('pending_stop')


def arm_breakeven(state, position, snapshot, bar_end, close_price, cfg, engine):
    if snapshot is None or position.get('pending_stop'):
        return
    partial_ts = partial_tp1_time(position)
    if partial_ts is None or partial_ts >= bar_end:
        return
    stop = breakeven_price(position, cfg, engine)
    side = position['side']
    if side * (stop - position['stop']) <= 0 or side * (close_price - stop) < cfg.tick - 1e-8:
        return
    import position_monitor
    known = position_monitor.confirmed_m5_structure(
        snapshot, partial_ts, bar_end, side, stop, cfg.tick)
    if known is None:
        return
    position.update(breakeven_armed=True, breakeven_price=stop, be_confirmed_ts=known,
                    pending_stop={'price': stop, 'effective_ts': bar_end, 'policy': STOP_POLICY})
    engine.emit(state, 'BREAKEVEN_ARMED', bar_end, order_id=position['id'], stop=stop, m5_known_ts=known)


def close(state, p, price, ts, reason, cfg, engine, ambiguous=False):
    from position_monitor import track

    track(p, ts, price, price)
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


def manage(state, c, cfg, engine, step, fill_bar=False, snapshot=None):
    p = state.get('active')
    if not p or c.ts <= p.get('last_bar',-1):
        return
    cfg = engine.execution_config(p,cfg)
    ts, side = c.ts+step, p['side']
    apply_protective_stop(p, c.ts)
    old_qty = p['filled_fraction']
    fill_legs(p,c,engine,cfg)
    added = p['filled_fraction'] > old_qty
    if added:
        engine.emit(state,'SECOND_LIMIT_FILLED',ts,order_id=p['id'],entry=p['entry'])
    stop_hit = c.low <= p['stop'] if side==1 else c.high >= p['stop']
    target_hit = c.high >= p['tp1'] if side==1 else c.low <= p['tp1']
    if stop_hit:
        from position_monitor import is_bounce_stop, is_stale_stop

        raw = c.open if side*(c.open-p['stop'])<=0 else p['stop']
        reason = ('CLOSED_STALE_TIME_STOP' if is_stale_stop(p) else
                  'CLOSED_BOUNCE_BREAKEVEN' if is_bounce_stop(p) else 'STOP_GAP' if raw==c.open else 'STOP')
        close(state,p,raw*(1-side*cfg.slippage_bps/10_000),ts,reason,cfg,engine,
              ambiguous=target_hit or fill_bar or added)
        return
    # Favorable extremes in a fill bar can precede either fill. A close through
    # the target proves the favorable traversal after both entries.
    proved = lambda level: (not (fill_bar or added)) or side*(c.close-level)>=0
    if not fill_bar and not added:
        favorable = c.high if side == 1 else c.low
        p['mfe_r'] = max(p['mfe_r'], side*(favorable-p['entry'])/p['risk'])
    if not p.get('tp1_hit') and target_hit and proved(p['tp1']):
        fraction=p.get('partials',{}).get('TP1',.6)
        if fraction == 1.:
            p.update(tp1_hit=True, tp1_reached_ts=ts)
            close(state,p,p['tp1']*(1-side*cfg.slippage_bps/10_000),ts,'TAKE_PROFIT',cfg,engine)
            return
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
        engine.emit(state, 'TP1_PARTIAL', ts, order_id=p['id'], price=p['tp1'],
                    fraction=fraction, rr=side * (p['tp1'] - p['entry']) / p['risk'])
        # TP1 is a realized milestone. Aggregate WR gets ONE label only when
        # the remaining position closes, including all fees and gap losses.
    second = c.high >= p['tp2'] if side==1 else c.low <= p['tp2']
    if p.get('tp1_hit') and second and proved(p['tp2']):
        close(state,p,p['tp2']*(1-side*cfg.slippage_bps/10_000),ts,'TAKE_PROFIT',cfg,engine)
        return
    if p.get('time_exit_enabled') and ts >= p['exit_deadline']:
        close(state,p,c.close*(1-side*cfg.slippage_bps/10_000),ts,'INTRADAY_TIME_EXIT',cfg,engine)
        return
    arm_breakeven(state, p, snapshot, ts, c.close, cfg, engine)
    p['last_bar'] = c.ts


def merge_plans(plans, snapshot, cfg, engine):
    """Merge confirmed, accepted same-side zones within half the real 15m ATR."""
    import portfolio_arbitration as arbitration
    import trade_dependencies
    groups = []
    threshold = .5*engine.atr(structure.closed(snapshot,'15m'))
    def same_impulse(first, second):
        if not (first.get('source_strategy') in scanner.ORGANIC_MOMENTUM
                and second.get('source_strategy') in scanner.ORGANIC_MOMENTUM):
            return False
        first_event = first.get('anchor_proof', {}).get('strategy_event_ts')
        return first_event is not None and first_event == second.get('anchor_proof', {}).get('strategy_event_ts')

    for p in sorted(plans,key=lambda p:(p['side'],p['entry'])):
        group = next((g for g in groups if p['side'] == g[0]['side'] and (
            any(same_impulse(p, other) for other in g)
            or (trade_dependencies.zone_related(p, g[-1], 2 * threshold)
                and all(x.get('action_type') == p.get('action_type') for x in g)))), None)
        if group is None:
            groups.append([p])
        else:
            group.append(p)
    result=[]
    for group in groups:
        best = copy.deepcopy(max(group, key=lambda p: arbitration.cluster_rank(p, snapshot, cfg.tick)))
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
