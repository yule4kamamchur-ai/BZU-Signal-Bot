"""Native candle contexts and explicit forming conditions for 24 ICT setups.

Readiness is a fraction of observed prerequisites. Only the ORIGINAL detector
can confirm a setup. It is never a success probability or an execution signal.
"""
from __future__ import annotations

from statistics import mean
from dataclasses import replace
from math import isfinite
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import market_wave as wave
import oil_risk

TREND_TP1_R = 1.10
TREND_LIMIT_MIN = .08
TREND_LIMIT_MAX = 2.20
MARKET_CHASE_R = .45
MARKET_ZONE_TOLERANCE = .08
MARKET_TOUCH_TOLERANCE = .06
MARKET_PATTERNS = frozenset({'IN_ZONE_TOUCH', 'POST_TOUCH_REJECTION', 'BREAKOUT_CONTINUATION'})

PRIMARY = ('15m', '1H', '4H')
MICRO = {'15m': '3m', '1H': '15m', '4H': '1H'}
MINUTES = {'15m': 15, '1H': 60, '4H': 240}


# Shared v12.27 direction and plan-integrity contracts. These are deterministic
# validity checks, not a win-probability model or an alternative trading signal.
ZONE_DIRECTION = {
    'DEMAND_OB':1, 'FVG_BULL':1, 'BULLISH_BREAKER':1, 'BULL_BREAKER':1,
    'BREAKER_BULL':1, 'SWEEP_LOW':1, 'IFVG_BULL':1,
    'SUPPLY_OB':-1, 'FVG_BEAR':-1, 'BEARISH_BREAKER':-1, 'BEAR_BREAKER':-1,
    'BREAKER_BEAR':-1, 'SWEEP_HIGH':-1, 'IFVG_BEAR':-1,
}
DIRECTIONAL_FORMATIONS = frozenset({'CONFIRMED_FVG', 'BREAKER_FLIP_15M',
    'MOMENTUM_STAIRCASE_15M', 'DISPLACEMENT_CATCHER_15M',
    'LIQUIDITY_RAID_ENGULFING_15M', 'STRUCTURAL_LEVEL', 'SESSION_SWEEP_ENGULFING',
    'SESSION_SWEEP_ENGULFING_15M', 'SWEEP_RECLAIM_ZONE', 'SESSION_OB', 'IMPULSE_OTE_15M'}) | wave.REVERSALS | wave.CONTINUATIONS | wave.TREND_ZONES
PLAN_DIAGNOSTICS = ('entry', 'stop', 'risk', 'tp', 'tp1', 'tp2', 'entry_zone',
    'zone_origin', 'approach', 'session_gate', 'planning_policy', 'entry_checks',
    'confluence', 'ict_evidence', 'rating', 'grade', 'entry_basis', 'entry_condition')


def zone_direction_matches(zone, side):
    kind = zone.get('kind', '')
    expected = ZONE_DIRECTION.get(kind)
    declared = zone.get('side')
    if declared in ('LONG', 'SHORT'):
        declared = 1 if declared == 'LONG' else -1
    return (not isinstance(side,bool) and side in (-1, 1) and (declared not in (-1, 1) or declared == side)
            and (expected == side if expected else kind in DIRECTIONAL_FORMATIONS))


def replace_plan_diagnostics(row, diagnostics):
    """A new branch owns its entire geometry; partial updates must not inherit SL."""
    from copy import deepcopy
    for key in PLAN_DIAGNOSTICS:
        row.pop(key, None)
    row.update(deepcopy(diagnostics))
    row.setdefault('entry_checks', entry_checks())


def candidate_plan_error(row, min_score=65, min_rr=1.35):
    """Full, directional candidate contract; never apply to a managed OPEN stop."""
    def finite_number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
    side = row.get('side')
    if not zone_direction_matches(row, side):
        return 'ZONE_DIRECTION_MISMATCH'
    values = [row.get(k) for k in ('low', 'high', 'entry', 'stop', 'tp1', 'tp2')]
    if not all(finite_number(v) and v > 0 for v in values):
        return 'INCOMPLETE_PLAN'
    low, high, entry, stop, tp1, tp2 = values
    if low >= high:
        return 'INVALID_ZONE_RANGE'
    risk = side * (entry-stop)
    if risk <= 0 or side*(tp1-entry) <= 0 or side*(tp2-tp1) < -1e-8:
        return 'INVALID_PLAN_GEOMETRY'
    if not finite_number(row.get('score')) or not min_score <= row['score'] <= 100:
        return 'SCORE_BELOW_65'
    if row.get('score_source') != 'CONTEXT':
        return 'CONTEXT_NOT_EVALUATED'
    gate = oil_risk.entry_tp1_error(side, entry, risk, tp1)
    if gate:
        return gate
    if oil_risk.is_organic(row):
        rr1, rr2 = side * (tp1 - entry) / risk, side * (tp2 - entry) / risk
        if not (oil_risk.TP1_MIN - 1e-8 <= rr1 <= oil_risk.TP1_MAX + 1e-8 and
                oil_risk.TP2_MIN - 1e-8 <= rr2 <= oil_risk.TP2_MAX + 1e-8):
            return 'ORGANIC_TARGET_RANGE'
    elif side*(tp1-entry)/risk < min_rr-1e-8:
        return 'RR_BELOW_1_35'
    if market_plan(row):
        trigger = row['market_trigger']
        proximal = high if side == 1 else low
        if trigger['pattern'] == 'CONFIRMED_REVERSAL':
            if not low-1e-8 <= entry <= high+1e-8:
                return 'ENTRY_OUTSIDE_ZONE'
        elif trigger['pattern'] == 'IN_ZONE_TOUCH':
            if not low - MARKET_ZONE_TOLERANCE - 1e-8 <= entry <= high + MARKET_ZONE_TOLERANCE + 1e-8:
                return 'ENTRY_OUTSIDE_ZONE'
        elif not 0 <= side * (entry - proximal) <= MARKET_CHASE_R * risk + 1e-8:
            return 'MARKET_CHASE_LIMIT'
    elif not low-1e-8 <= entry <= high+1e-8:
        # Planner front-running of a narrow zone is explicit, bounded and causal.
        proximal = high if side == 1 else low
        if not 0 <= side*(entry-proximal) <= .03000001:
            return 'ENTRY_OUTSIDE_ZONE'
    return None


def standalone_zone_error(zone, snapshot, cfg, engine, atr15, regime):
    """Require a native zone, minimum tick width and intact closed-candle structure."""
    timeframe = zone.get('timeframe')
    if timeframe not in PRIMARY or zone.get('kind') == 'MICRO_BOS_FVG':
        return 'MICRO_ZONE_EXCLUDED'
    low, high, known = (zone.get(key) for key in ('low', 'high', 'known_ts'))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)
               for v in (low, high, known)):
        return 'INCOMPLETE_PLAN'
    if low <= 0 or high - low < cfg.tick - 1e-8 or known > snapshot.now:
        return 'INVALID_ZONE_RANGE'
    side = zone.get('side')
    if not zone_direction_matches(zone, side):
        return 'ZONE_DIRECTION_MISMATCH'
    # Penetration is a test. A closed body through the distal edge invalidates it.
    threshold = low if side == 1 else high
    timeframes = set(('15m', timeframe))
    closed_rows = (c for tf in timeframes for c in snapshot.candles.get(tf, [])
                   if c.confirmed and known <= c.ts and c.ts + engine.TF[tf] <= snapshot.now)
    if (zone.get('mitigated') or any(side * (c.close - threshold) < -1e-8 for c in closed_rows)
            or any(c.ts >= known and side * (c.close - threshold) < -1e-8
                   for c in closed_five_minutes(snapshot))):
        return 'ZONE_MITIGATED'
    distance = max(low - snapshot.price, snapshot.price - high, 0)
    if regime in ('RANGE', 'RANGE_LIQUIDITY_ROTATION') and distance < .15 * atr15 - 1e-8:
        return 'RANGE_ZONE_TOO_CLOSE'
    return None


def max_zone_distance(atr15, cfg):
    return min(.65, min(1.2, cfg.max_entry_distance_atr) * atr15)


def exhausted_move(side, price, target):
    return (isinstance(target, (int, float)) and isfinite(target)
            and side * (price - target) >= -1e-8)


def target_crossed(side, snapshot, target, known):
    if exhausted_move(side, snapshot.price, target):
        return True
    minute = audit_rows(snapshot, after=known)
    m15 = (bar for bar in snapshot.candles.get('15m', ()) if bar.confirmed
           and known <= bar.ts and bar.ts + wave.M15 <= snapshot.now)
    return any(exhausted_move(side, bar.high if side == 1 else bar.low, target)
               for bar in (*minute, *m15))


CONTINUATION_SETUPS = frozenset({'CONTINUATION_PULLBACK','PULLBACK_CONTINUATION','TREND_PULLBACK',
    'BOS_CONTINUATION','FLAG_RETEST','FRESH_BASE_CONTINUATION','SWEEP_CONTINUATION',
    'ACCELERATION_PULLBACK_REENTRY','ACCEPTANCE_RETEST_CONTINUATION','MOMENTUM_NO_PULLBACK_CONTINUATION'})
REVERSAL_RELATION = 'CONFIRMED_REVERSAL'


def continuation_setup(source, kind, setup):
    return (source or (kind if kind != 'STRUCTURAL_LEVEL' else None) or setup or '').upper() in CONTINUATION_SETUPS


def pending_tp1_error(plan):
    target = plan.get('tp1') or plan.get('tp')
    risk = plan.get('risk')
    if target is not None and risk is not None:
        return oil_risk.entry_tp1_error(plan['side'], plan['entry'], risk, target)
    return None


def post_tp_exhaustion(side, snapshot, trades, adr):
    """Hold a new continuation until a completed TP wave has reset."""
    if not adr or adr <= 0:
        return False
    last = max((trade for trade in trades if trade.get('side')==side and
                trade.get('close_reason')=='TAKE_PROFIT' and
                0 <= snapshot.now-trade.get('closed_ts',0) < 8*wave.M15),
               key=lambda trade:trade['closed_ts'], default=None)
    if not last:
        return False
    exit_price = last['exit']
    move = side*(exit_price-last['entry'])
    if move < .80*adr-1e-8:
        return False
    corrected = -side*(snapshot.price-exit_price)
    for timeframe,step in (('1m',60_000),('15m',wave.M15)):
        for bar in snapshot.candles.get(timeframe,()):
            if bar.confirmed and last['closed_ts'] <= bar.ts and bar.ts+step <= snapshot.now:
                extreme = bar.low if side==1 else bar.high
                corrected = max(corrected,-side*(extreme-exit_price))
    return corrected < .382*move-1e-8


def reversal_market_trigger(snapshot, zone, quote, confirmation_ts):
    known = max(zone['known_ts'],confirmation_ts)
    as_of = snapshot.ticker_ts
    if (zone['kind']!='RECLAIM_BREAKER' and snapshot.trusted
            and known <= as_of <= snapshot.now
            and zone['low'] <= quote <= zone['high']):
        return {'pattern':'CONFIRMED_REVERSAL','known_ts':known}
    return None


def market_plan(plan):
    pattern = (plan.get('market_trigger') or {}).get('pattern')
    return (plan.get('order_type') == 'MARKET' and (
        (plan.get('trend_rel') == 'WITH_TREND' and pattern in MARKET_PATTERNS) or
        (plan.get('trend_rel') == REVERSAL_RELATION and pattern == 'CONFIRMED_REVERSAL')))


def min_plan_score(relation, market_trigger=None):
    if relation == 'WITH_TREND' and (market_trigger or {}).get('pattern') == 'BREAKOUT_CONTINUATION':
        return 60
    return {'WITH_TREND': 52, 'COUNTER_TREND': 78, REVERSAL_RELATION: 75}.get(relation, 60)


def entry_window(side, price, entry, atr15, cfg, *, minimum=True, trend=False):
    distance = side * (price - entry)
    if abs(distance) > (TREND_LIMIT_MAX if trend else max_zone_distance(atr15, cfg)) + 1e-8:
        return 'DISTANT_ZONE'
    if minimum and distance < (TREND_LIMIT_MIN if trend else .10) - 1e-8:
        return 'ZONE_NOT_AHEAD_OF_PRICE' if distance <= 0 else 'ENTRY_TOO_CLOSE'
    return None


def trend_market_trigger(snapshot, zone, side, quote, risk, tick, engine):
    """Classify a current executable quote using only already observed evidence."""
    low, high, known = (zone[k] for k in ('low', 'high', 'known_ts'))
    as_of = snapshot.ticker_ts or snapshot.now
    if zone['kind'] == 'BOS_CONTINUATION' or not snapshot.trusted or not isfinite(quote) or quote <= 0 or not known <= as_of <= snapshot.now:
        return None
    if low - MARKET_ZONE_TOLERANCE - 1e-8 <= quote <= high + MARKET_ZONE_TOLERANCE + 1e-8:
        return {'pattern': 'IN_ZONE_TOUCH', 'known_ts': as_of}
    proximal = high if side == 1 else low
    if not 0 < side * (quote - proximal) <= MARKET_CHASE_R * risk + 1e-8:
        return None
    m5 = [c for c in closed_five_minutes(snapshot) if c.ts + wave.M5 <= as_of]
    m15 = [c for c in snapshot.candles.get('15m', []) if c.confirmed and c.ts + wave.M15 <= as_of]
    for rows, step, count in ((m5, wave.M5, 6), (m15, wave.M15, 3)):
        for bar in reversed(rows[-count:]):
            if (bar.ts >= known and as_of - (bar.ts + step) < count * step
                    and bar.low <= high + MARKET_TOUCH_TOLERANCE + 1e-8
                    and bar.high >= low - MARKET_TOUCH_TOLERANCE - 1e-8):
                return {'pattern': 'POST_TOUCH_REJECTION', 'known_ts': bar.ts + step, 'touch_ts': bar.ts}
    proof = zone.get('wave_proof') or {}
    if zone['kind'] not in ('BREAKER_BLOCK', 'FLAG_RETEST') or proof.get('timeframe') not in ('5m', '15m'):
        return None
    rows, step = (m5, wave.M5) if proof['timeframe'] == '5m' else (m15, wave.M15)
    prefix = [c for c in rows if c.ts + step <= known]
    if len(prefix) < 5 or prefix[-1].ts + step != known or as_of - known >= step:
        return None
    bar = prefix[-1]
    atr = engine.atr(prefix[:-1])
    width = max(tick, bar.high - bar.low)
    body = side * (bar.close - bar.open)
    volume = mean(c.volume for c in prefix[-21:-1])
    if (atr > 0 and body >= .60 * width and side * (bar.close - proximal) >= tick - 1e-8
            and (body >= .85 * atr or (volume > 0 and bar.volume >= 1.20 * volume))):
        return {'pattern': 'BREAKOUT_CONTINUATION', 'known_ts': known}
    return None


def proximity_bonus(side, price, entry, aligned):
    return 10 if aligned and .15 - 1e-8 <= side * (price - entry) <= .55 + 1e-8 else 0


def zone_risk_bounds(atr15, cfg):
    floor = max(cfg.min_stop_absolute, .22, .40 * atr15)
    return floor, min(.65, cfg.max_stop_absolute)


def normalized_zone_stop(entry, structural, side, atr15, cfg, engine):
    """Clip risk to the tick grid, while flagging a stop inside the invalidation."""
    floor, cap = zone_risk_bounds(atr15, cfg)
    raw_stop = structural - side * max(cfg.tick, .25 * atr15)
    structural_distance = side * (entry - raw_stop)
    risk = min(cap, max(floor, structural_distance))
    stop = engine.round_tick(round(entry - side * risk, 10), cfg.tick, up=side == -1)
    if side * (entry - stop) > cap + 1e-8:
        stop = engine.round_tick(round(entry - side * cap, 10), cfg.tick, up=side == 1)
    risk = side * (entry - stop)
    safe = (floor - 1e-8 <= risk <= cap + 1e-8
            and side * (raw_stop - stop) >= -1e-8 and stop > 0)
    return stop, risk, safe


def zone_entry_geometry(low, high, price, structural, side, atr15, cfg, engine, *, preferred=None, oil=None, allow_local=False):
    """Work inside the native zone without shrinking its structural falsifier."""
    width = high - low
    wide = width > .45 + 1e-8 or width > .80 * atr15 + 1e-8
    inside = low <= price <= high
    distal, proximal = (low, high) if side == 1 else (high, low)
    depth_prices = [(distal + side * width * fraction, basis)
                    for fraction, basis in ((.50, 'EQUILIBRIUM'), (.35, 'DISTAL_35'), (.25, 'DISTAL_25'))]
    if inside:
        if side * (price - distal) <= width / 3:
            depth_prices.insert(0, (price - side * .05, 'INSIDE_NEAR_QUOTE'))
        choices = depth_prices
    elif wide:
        choices = depth_prices[:2]
    else:
        first = preferred if preferred is not None else proximal + side * .03
        choices = [(first, 'DETECTOR_LEVEL' if preferred is not None else 'PROXIMAL_PLUS_0_03'), *depth_prices]
    # In a shallow zone near the distal edge the only passive tick may be its boundary.
    if inside:
        choices = choices + [(distal, 'DISTAL_EDGE')]
    fallback = None
    for raw, basis in choices:
        if (inside or basis != 'PROXIMAL_PLUS_0_03') and not low - 1e-8 <= raw <= high + 1e-8:
            continue
        entry = engine.round_tick(round(raw, 10), cfg.tick, up=(side == 1 and not inside) or (side == -1 and inside))
        if inside and (not low - 1e-8 <= entry <= high + 1e-8 or side * (price - entry) < cfg.tick - 1e-8):
            continue
        if oil:
            stop, risk, details = oil_risk.stop_geometry(entry, structural, side, *oil, cfg.tick, engine, allow_local=allow_local)
            safe = details['gate'] is None
        else:
            stop, risk, safe = normalized_zone_stop(entry, structural, side, atr15, cfg, engine)
        if safe:
            return entry, stop, risk, basis, wide, inside
        fallback = (entry, basis)
    if fallback is None:
        fallback = (engine.round_tick(round(distal, 10), cfg.tick, up=side == 1), 'DISTAL_EDGE')
    entry, basis = fallback
    if oil:
        stop, risk, _ = oil_risk.stop_geometry(entry, structural, side, *oil, cfg.tick, engine, allow_local=allow_local)
        return entry, stop, risk, basis, wide, inside
    floor, _ = zone_risk_bounds(atr15, cfg)
    # Show the real required protection in a reference plan even when it exceeds the cap.
    buffer = max(cfg.tick, .25 * atr15)
    needed = max(floor, side * (entry - structural) + buffer)
    stop = engine.round_tick(round(entry - side * needed, 10), cfg.tick, up=side == -1)
    return entry, stop, side * (entry - stop), basis, wide, inside


def structural_target_price(target, now, side, tick, engine):
    """Front-run a known structural level by one tick."""
    if not isinstance(target, dict):
        return None
    level, known = target.get('level'), target.get('known_ts')
    if not isinstance(level, (int, float)) or not isfinite(level) or level <= 0:
        return None
    if known is not None and (not isinstance(known, (int, float)) or not isfinite(known) or known > now):
        return None
    return engine.round_tick(round(level - side * tick, 10), tick, up=side == -1)


def zone_targets(entry, risk, side, targets, now, tick, engine, quote=None):
    """Select the first eligible buffered targets and keep hard barriers causal."""
    levels = []
    for target in targets:
        price = structural_target_price(target, now, side, tick, engine)
        if price is None:
            continue
        distance = side * (price - entry)
        if distance > 0:
            levels.append((distance, price, target))
    levels.sort(key=lambda row: row[0])
    barrier = next((row for row in levels if row[2].get('hard_barrier')), None)
    reachable = [row for row in levels if barrier is None or row[0] <= barrier[0]]
    first = next((row for row in reachable if row[0] >= 1.40 * risk - 1e-8), None)
    quote_price = (engine.round_tick(quote, tick, up=side == -1)
                   if isinstance(quote, (int, float)) and isfinite(quote) else None)
    quote_reachable = (quote_price is not None and side * (quote_price - entry) >= 1.40 * risk - 1e-8
                       and (barrier is None or side * (quote_price - entry) <= barrier[0]))
    projection = engine.round_tick(round(entry + side * 1.50 * risk, 10), tick, up=side == 1)
    tp1 = first[1] if first else quote_price if quote_reachable else projection
    second = next((row for row in reachable
                   if row[0] >= 2.20 * risk - 1e-8 and side * (row[1] - tp1) >= tick - 1e-8), None)
    tp2 = second[1] if second else engine.round_tick(
        round(entry + side * max(2.20 * risk, side * (tp1 - entry) + tick), 10), tick, up=side == 1)
    return {'tp1': tp1, 'tp2': tp2,
            'tp1_source': first[2].get('kind', 'STRUCTURE') if first else
                          'QUOTE_REFERENCE' if quote_reachable else 'R_MULTIPLE_1_50',
            'tp2_source': second[2].get('kind', 'STRUCTURE') if second else 'R_MULTIPLE_2_20',
            'tp1_alignment': first[2] if first else None,
            'tp2_alignment': second[2] if second else None,
            'structural_tp1': first is not None, 'structural_tp2': second is not None,
            'barrier_price': barrier[1] if barrier else None,
            'path_clear': barrier is None or side * (tp1 - entry) <= barrier[0] + 1e-8}


def planned_targets(entry, risk, side, targets, snapshot, cfg, engine, zone, atr15, quote=None):
    """Permit a fresh projection near a confirmed day extreme with clear runway."""
    base = zone_targets(entry, risk, side, targets, snapshot.now, cfg.tick, engine, quote=quote)
    if not zone.get('wave_proof') or zone.get('kind') not in wave.CONTINUATIONS:
        return base
    import directional_policy as direction

    extreme = next((p for p in direction.day_extrema(snapshot)
                    if p['name'] == ('DAY_HIGH' if side == 1 else 'DAY_LOW')), None)
    # The running M5 leg may confirm a newer session extreme before M15 closes.
    m5 = [c for c in closed_five_minutes(snapshot) if c.ts >= zone['known_ts'] - 6 * wave.M5]
    if extreme and m5:
        candle = max(m5, key=lambda c: c.high) if side == 1 else min(m5, key=lambda c: c.low)
        level = candle.high if side == 1 else candle.low
        if side * (level - extreme['level']) > 0:
            extreme = {**extreme, 'level': level, 'known_ts': candle.ts + wave.M5}
    if (not extreme or snapshot.now - extreme['known_ts'] >= 60 * 60_000
            or abs(snapshot.price - extreme['level']) > max(.15, .45 * atr15) + 1e-8):
        return base
    ahead = [t for t in targets
             if (p := structural_target_price(t, snapshot.now, side, cfg.tick, engine)) is not None
             and (t.get('hard_barrier') or not exhausted_move(side, snapshot.price, p))]
    destination = zone_targets(entry, risk, side, ahead, snapshot.now, cfg.tick, engine)
    if destination['structural_tp1'] and destination['structural_tp2'] and destination['path_clear']:
        return destination
    projections = [engine.round_tick(round(entry + side * risk * r, 10), cfg.tick, up=side == 1)
                   for r in (1.50, 2.30)]
    tp1 = destination['tp1'] if destination['structural_tp1'] else projections[0]
    tp2 = destination['tp2'] if destination['structural_tp2'] else projections[1]
    barrier = destination['barrier_price']
    if (min(tp1, tp2) <= 0 or exhausted_move(side, snapshot.price, tp1)
            or side * (tp2 - tp1) < cfg.tick - 1e-8
            or (barrier is not None and side * (tp2 - barrier) > 1e-8)):
        return base
    return {**destination, 'tp1': tp1, 'tp2': tp2, 'path_clear': True,
            'tp1_source': destination['tp1_source'] if destination['structural_tp1'] else 'R_MULTIPLE_1_50',
            'tp2_source': destination['tp2_source'] if destination['structural_tp2'] else 'R_MULTIPLE_2_30',
            'target_projection': {'as_of_ts': snapshot.now, 'extreme': extreme,
                                  'tp1': not destination['structural_tp1'],
                                  'tp2': not destination['structural_tp2']}}


def organic_targets(entry, risk, side, targets, snapshot, tick, engine, *, trend=False, fixed_tp1=None):
    """Prefer macro liquidity, with a labelled R projection for an intact trend."""
    levels = []
    for target in targets:
        if target.get('timeframe') not in ('1H', '4H', 'D1', 'SESSION') or target.get('known_ts') is None:
            continue
        price = structural_target_price(target, snapshot.now, side, tick, engine)
        if price is not None and side * (price - entry) > 0:
            levels.append((side * (price - entry) / risk, price, target))
    levels.sort(key=lambda x: x[0])
    barrier = next((price for _, price, target in levels if target.get('hard_barrier')), None)
    first_floor = TREND_TP1_R if trend else oil_risk.TP1_MIN
    first = next((x for x in levels if first_floor - 1e-8 <= x[0] <= oil_risk.TP1_MAX + 1e-8
                  and (x[2]['timeframe'] != 'D1' or x[2]['kind'].startswith('PD_'))), None)
    second = next((x for x in levels if oil_risk.TP2_MIN - 1e-8 <= x[0] <= oil_risk.TP2_MAX + 1e-8
                   and x[2]['timeframe'] in ('4H', 'D1')), None)

    def projected(multiplier):
        return engine.round_tick(round(entry + side * multiplier * risk, 10), tick, up=side == 1)

    tp1 = first[1] if first else projected(TREND_TP1_R if trend else oil_risk.TARGET_1_FALLBACK)
    tp2 = second[1] if second else projected(1.90 if trend else 2.)
    checkpoints = []
    if trend:
        first_exit = tp1 if fixed_tp1 is None else fixed_tp1
        checkpoints = {price for rr, price, _ in levels if rr < 1.05}
        m15 = [bar for bar in snapshot.candles.get('15m', [])
               if bar.confirmed and bar.ts + wave.M15 <= snapshot.now]
        for point in engine.pivots(m15[-48:], strength=1):
            if point['kind'] == ('HIGH' if side == 1 else 'LOW') and 0 < side * (point['level'] - entry) < 1.05 * risk:
                checkpoints.add(point['level'])
        checkpoints = sorted(checkpoints, key=lambda price: side * (price - entry))[:8]
        # Tested opposing zones and runner obstacles remain visible. A fresh
        # barrier before the first exit or one containing entry still vetoes.
        clear = not any(target.get('hard_barrier') and
                        target.get('freshness') != 'TESTED' and
                        (target.get('entry_inside_zone') or side * (first_exit - price) > 1e-8)
                        for _, price, target in levels)
    else:
        clear = barrier is None or side * (tp2 - barrier) <= 1e-8
    return {'tp1': tp1, 'tp2': tp2,
            'tp1_source': first[2]['kind'] if first else ('R_MULTIPLE_1_10' if trend else 'R_MULTIPLE_1_15'),
            'tp2_source': second[2]['kind'] if second else ('R_MULTIPLE_1_90' if trend else 'REFERENCE_2R'),
            'tp1_alignment': first[2] if first else None, 'tp2_alignment': second[2] if second else None,
            'structural_tp1': bool(first), 'structural_tp2': bool(second),
            'target_projection': {'tp1': not first, 'tp2': trend and not second},
            'barrier_price': barrier,
            'checkpoints': checkpoints,
            'path_clear': clear}


def calculate_zone_trade_plan(zone, snapshot, cfg, engine, primary_side=0,
                              context=None, geometry=None, targets=None, regime=None):
    """Causal geometric pre-plan + transparent 5-factor ranking, never execution.

    Existing complete engine geometry is frozen. A missing plan is calculated
    as one whole unit, so a preview's TP cannot be attached to another SL.
    Fallback R targets are explicitly labelled projections, not liquidity.
    """
    def valid(v):
        return isinstance(v,(int,float)) and not isinstance(v,bool) and isfinite(v)
    side=zone.get('side');low=zone.get('source_low',zone.get('low'));high=zone.get('source_high',zone.get('high'))
    known=zone.get('known_ts');tick=cfg.tick
    if (not zone_direction_matches(zone,side) or not all(valid(v) and v>0 for v in (low,high,tick)) or
            low>=high or not valid(known) or known>snapshot.now or zone.get('mitigated')):
        return None
    closed15=[c for c in snapshot.candles.get('15m',[]) if c.confirmed and c.ts+900_000<=snapshot.now]
    a=engine.atr(closed15)
    if not valid(a) or a<=0:
        return None
    zone_error = standalone_zone_error(zone, snapshot, cfg, engine, a, regime)
    if zone_error and zone_error != 'RANGE_ZONE_TOO_CLOSE':
        return None
    relation = (geometry or {}).get('trend_rel')
    trend = relation == 'WITH_TREND'
    runner = trend or relation == REVERSAL_RELATION
    projected_runner = trend or (runner and bool((geometry or {}).get('reversal_exhaustion_bonus')
                                                  or (geometry or {}).get('structure_reclaim')))
    market = market_plan(geometry or {})
    trigger = (geometry or {}).get('market_trigger') if market else None
    score_floor = min_plan_score(relation, trigger)
    distal = low if side == 1 else high
    near_swings = [point['level'] for point in engine.pivots(closed15)
                   if point['kind'] == ('LOW' if side == 1 else 'HIGH')
                   and point['known_ts'] + 900_000 <= known
                   and 0 <= side * (distal - point['level']) <= .25 * a]
    structural = min(near_swings, key=lambda level: abs(level - distal), default=distal)
    oil = (oil_risk.snapshot_profile(snapshot), oil_risk.macro_anchors(snapshot, cfg, engine, side)) if oil_risk.enabled(cfg) else None
    entry, stop, risk, basis, wide, inside = zone_entry_geometry(
        low, high, snapshot.price, structural, side, a, cfg, engine, oil=oil)
    profile = oil_risk.stop_geometry(entry, structural, side, *oil, tick, engine)[2] if oil else None
    floor, cap = (profile['floor'], profile['cap']) if profile else zone_risk_bounds(a, cfg)
    safe = (profile['gate'] is None if profile else floor - 1e-8 <= risk <= cap + 1e-8)
    frozen = False
    if geometry and all(valid(geometry.get(k)) and geometry[k] > 0 for k in ('entry', 'stop', 'tp1', 'tp2')):
        ge, gs, g1, g2 = (geometry[k] for k in ('entry', 'stop', 'tp1', 'tp2'))
        if side * (ge - gs) > 0 and side * (g1 - ge) > 0 and side * (g2 - g1) >= -tick / 2:
            entry, stop = ge, gs
            risk = side * (entry - stop)
            if oil:
                profile = geometry.get('organic_risk') or {**profile, 'gate': 'LEGACY_MICRO_RISK'}
            floor, cap = (profile['floor'], profile['cap']) if profile else zone_risk_bounds(a, cfg)
            boundary = profile['boundary'] if profile else geometry.get('invalidation', distal)
            buffer = profile['buffer'] if profile else max(tick, .25 * a)
            safe = (floor - 1e-8 <= risk <= cap + 1e-8 and valid(boundary) and
                    min(side * (distal - stop), side * (boundary - stop)) >= buffer - 1e-8 and
                    (not profile or profile['gate'] is None))
            frozen = True
    if risk <= 0 or stop <= 0:
        return None
    target_context = {**(context or {}), 'organic_oil_targets': bool(oil)}
    available = (engine.full_targets(target_context, snapshot, 'LONG' if side == 1 else 'SHORT', entry, cfg)
                 if targets is None else targets)
    destination = (organic_targets(entry, risk, side, available, snapshot, tick, engine, trend=projected_runner,
                                   fixed_tp1=geometry['tp1'] if frozen else None) if oil else
                   planned_targets(entry, risk, side, available, snapshot, cfg, engine, zone, a,
                                   quote=snapshot.price if not frozen else None))
    if frozen:
        tp1, tp2 = geometry['tp1'], geometry['tp2']
        tp1_source = geometry.get('tp1_source') or 'ENGINE_PLAN'
        tp2_source = geometry.get('tp2_source') or 'ENGINE_PLAN'
    else:
        tp1, tp2 = destination['tp1'], destination['tp2']
        tp1_source, tp2_source = destination['tp1_source'], destination['tp2_source']
    if min(tp1,tp2)<=0 or side*(tp1-entry)<=0 or side*(tp2-tp1)<-tick/2:
        return None
    rr1,rr2=side*(tp1-entry)/risk,side*(tp2-entry)/risk
    native=zone.get('timeframe','15m');step=engine.TF.get(native,300_000 if native=='5m' else 900_000)
    rows=[c for c in snapshot.candles.get(native,[]) if c.confirmed and c.ts+step<=snapshot.now]
    after=[c for c in rows if c.ts>=known]
    touched=low<=snapshot.price<=high or any(c.low<=high and c.high>=low for c in after)
    first_after=((known+step-1)//step)*step
    complete=bool(rows and rows[0].ts<=known and (not after or after[0].ts<=first_after) and not any(b.ts-a0.ts!=step for a0,b in zip(after,after[1:])))
    freshness='TESTED' if touched else 'UNTESTED' if complete else 'UNKNOWN_HISTORY'
    sweep=zone.get('sweep') or {}
    sweep_ts=sweep.get('known_ts',sweep.get('sweep_ts',known)) if isinstance(sweep,dict) else snapshot.now+1
    explicit_sweep=zone.get('sweep_known_ts')
    htf_or_sweep=native in ('1H','4H') or (valid(explicit_sweep) and explicit_sweep<=snapshot.now) or bool(sweep and any(k in sweep for k in ('known_ts','sweep_ts')) and valid(sweep_ts) and sweep_ts<=snapshot.now)
    departure=[c for c in rows if known-2*step<=c.ts<known+3*step]
    displacement=('FVG' in zone['kind']
                  or (zone.get('wave_proof') and zone['kind'] in wave.CONTINUATIONS) or any(side*(c.close-c.open)>=.6*a and
                  side*(c.close-(high if side==1 else low))>0 for c in departure))
    barrier = destination['barrier_price']
    target_range = (oil_risk.TP1_MIN - 1e-8 <= rr1 <= oil_risk.TP1_MAX + 1e-8 and
                    oil_risk.TP2_MIN - 1e-8 <= rr2 <= oil_risk.TP2_MAX + 1e-8) if oil else (
                    rr1 >= 1.4 - 1e-8 and rr2 >= 2.2 - 1e-8)
    room = target_range and (destination['path_clear'] if projected_runner else
                             barrier is None or side * (tp2 - barrier) <= 1e-8)
    if frozen:
        structural_tp1 = structural_target_price(
            geometry.get('tp1_alignment'), snapshot.now, side, tick, engine) == round(tp1, 10)
        structural_tp2 = structural_target_price(
            geometry.get('tp2_alignment'), snapshot.now, side, tick, engine) == round(tp2, 10)
    else:
        structural_tp1 = destination['structural_tp1']
        structural_tp2 = destination['structural_tp2']
    projection = (geometry if frozen else destination).get('target_projection') or {}
    if oil:
        if projection.get('tp1'):
            multiple = oil_risk.TARGET_1_FALLBACK
            if projected_runner:
                multiple = 1.05 if tp1_source == 'R_MULTIPLE_1_05' else TREND_TP1_R
            structural_tp1 = abs(tp1 - engine.round_tick(
                round(entry + side * multiple * risk, 10), tick, up=side == 1)) < 1e-8
        if frozen:
            structural_tp2 = structural_tp2 and (geometry.get('tp2_alignment') or {}).get('timeframe') in ('4H', 'D1')
        if projected_runner and projection.get('tp2'):
            structural_tp2 = abs(tp2 - engine.round_tick(
                round(entry + side * 1.90 * risk, 10), tick, up=side == 1)) < 1e-8
    elif projection and zone.get('wave_proof') and zone.get('kind') in wave.CONTINUATIONS:
        if projection.get('tp1'):
            structural_tp1 = abs(tp1 - engine.round_tick(
                round(entry + side * 1.50 * risk, 10), tick, up=side == 1)) < 1e-8
        if projection.get('tp2'):
            structural_tp2 = abs(tp2 - engine.round_tick(
                round(entry + side * 2.30 * risk, 10), tick, up=side == 1)) < 1e-8
    reversal = (wave.reversal_override(side, zone, snapshot, tick, require_macro=bool(oil))
                or bool((geometry or {}).get('reversal_override')))
    volume = formation_volume(zone, snapshot)
    criteria = [('REGIME_ALIGNMENT', 25, primary_side == side or reversal),
                ('UNTESTED', 20, freshness == 'UNTESTED'), ('HTF_OR_SWEEP', 20, htf_or_sweep),
                ('DISPLACEMENT_OR_FVG', 20, displacement), ('ROOM_TO_RUN', 15, room),
                ('VOLUME_BONUS', 6, volume['confirmed']),
                ('PROXIMITY_BONUS', 10, bool(proximity_bonus(side, snapshot.price, entry, primary_side == side)))]
    if trend:
        criteria.append(('TREND_PULLBACK', 12, ((geometry or {}).get('trend_phase') or '').endswith('PULLBACK_OPPORTUNITY')))
    if relation == 'COUNTER_TREND':
        criteria.append(('CONFIRMED_CHOCH_M5', 12, bool((geometry or {}).get('counter_confirmation'))))
    if (geometry or {}).get('reversal_exhaustion_bonus'):
        criteria.append(('ADR_EXHAUSTION_REVERSAL', 10, True))
    score = min(100, sum(weight for _, weight, passed in criteria if passed))
    import entry_adverse_selection as adverse
    brake=adverse.approach(snapshot,a,side,{'low':low,'high':high,'known_ts':known},tick,stop=stop,trend=runner)
    stop_breached=any((c.low<=stop if side==1 else c.high>=stop) for c in after)
    tracking_only = inside or (geometry or {}).get('reaction_only')
    window = None if market else entry_window(side, snapshot.price, entry, a, cfg,
                          minimum=(geometry or {}).get('action_type') != 'ACTIVE_ENTRY' and not tracking_only,
                          trend=runner)
    gate=('EXHAUSTED_MOVE' if target_crossed(side, snapshot, tp1, trigger['known_ts'] if trigger else known) else
          'STOP_ALREADY_BREACHED' if stop_breached or side * (snapshot.price - stop) <= 0 else
          profile['gate'] if profile and profile['gate'] else
          'RISK_BELOW_FLOOR' if risk < floor - 1e-8 else
          'RISK_TOO_WIDE' if not safe else
          zone_error if zone_error else
          oil_risk.entry_tp1_error(side, entry, risk, tp1) or ('INSUFFICIENT_ROOM' if not room else
          window if window else
          'STRUCTURAL_TP1_REQUIRED' if not structural_tp1 else
          'STRUCTURAL_TP2_REQUIRED' if not structural_tp2 else None))
    breaker = m5_breaker(zone, snapshot, tick)
    reaction_only = relation is None and (inside or bool(breaker) or bool((geometry or {}).get('reaction_only')))
    condition = ('IN_ZONE_M5_RECLAIM' if reaction_only and inside else 'M5_BREAKER_RETEST' if reaction_only and breaker else
                 (geometry or {}).get('entry_condition', 'RETEST'))
    return {'low':low,'high':high,'entry':entry,'stop':stop,'tp1':tp1,'tp2':tp2,'risk':risk,'rr1':rr1,'rr2':rr2,
        'score':score,'grade':'A' if score>=65 else 'B+' if score>=score_floor else 'C',
        'trend_rel': relation, 'trend_phase': (geometry or {}).get('trend_phase'),
        'min_score': score_floor, 'entry_max_distance': TREND_LIMIT_MAX if runner else max_zone_distance(a, cfg),
        'order_type': 'MARKET' if market else 'LIMIT', 'market_trigger': trigger,
        'entry_status': 'MARKET_NOW' if market else 'LIMIT_READY',
        'checkpoints': (geometry or {}).get('checkpoints', destination.get('checkpoints', [])),
        'score_source':'ZONE_HEURISTIC','score_model':'ZONE_ORGANIC_OIL_V32' if oil else 'ZONE_WAVE_V31',
        'score_cap':100, 'formation_volume':volume,
        'score_breakdown':[{'criterion':key,'maximum':weight,'earned':weight if passed else 0,'met':bool(passed)}
                           for key,weight,passed in criteria],
        'freshness':freshness,'atr15':a,'wide_zone':wide,'entry_basis':'ENGINE_PLAN' if frozen else basis, 'in_zone':inside,
        'entry_condition':condition, 'm5_breaker':breaker,
        'risk_floor':floor, 'risk_cap':cap, 'target_barrier':barrier, 'organic_risk':profile,
        'target_projection':projection or None, 'reversal_override':reversal,
        'tp1_source':tp1_source,'tp2_source':tp2_source,'plan_basis':'ENGINE_GEOMETRY' if frozen else 'STRUCTURAL_PREPLAN',
        'risk_gate':gate,'impulse_blocked':bool(brake['blocked']),
        'tactic':('OBSERVE' if gate or brake['blocked'] or score < score_floor else
                  'MARKET' if market else
                  'REACTION_M5' if reaction_only or (relation is None and score < 65) else 'LIMIT'),
        'calculated_ts':snapshot.now}


def closed_five_minutes(snapshot):
    """UTC-aligned 5m bars, requiring all five confirmed constituent minutes.

    Live snapshots already fetch 1m. No new network request or worker is
    needed. Explicit 5m data is useful for archived snapshots without 1m.
    If minute data exists, a gap cannot be hidden by a coarser candle.
    """
    if not snapshot.candles.get('1m'):
        return sorted({c.ts:c for c in snapshot.candles.get('5m', [])
            if c.confirmed and c.ts % 300_000 == 0
            and c.ts+300_000 <= snapshot.now}.values(), key=lambda c:c.ts)
    groups={}
    for c in snapshot.candles['1m']:
        if c.confirmed and c.ts % 60_000 == 0 and c.ts+60_000 <= snapshot.now:
            groups.setdefault(c.ts//300_000*300_000, {})[c.ts]=c
    result=[]
    for start,minutes in sorted(groups.items()):
        if set(minutes) != {start+i*60_000 for i in range(5)}:
            continue
        bars=[minutes[start+i*60_000] for i in range(5)]
        result.append(replace(bars[0], ts=start, high=max(c.high for c in bars),
            low=min(c.low for c in bars), close=bars[-1].close,
            volume=sum(c.volume for c in bars), confirmed=True))
    return result


def m5_breaker(zone, snapshot, tick):
    """A closed M5 body break inside a native zone, usable only as timing evidence."""
    rows = closed_five_minutes(snapshot)[-12:]
    side = zone['side']
    step = 300_000
    for i in range(len(rows) - 1, 2, -1):
        previous = rows[i - 3:i]
        bar = rows[i]
        known = bar.ts + step
        if known <= zone['known_ts'] or snapshot.now - known > 3 * step:
            break
        if any(y.ts - x.ts != step for x, y in zip(previous, [*previous[1:], bar])):
            continue
        level = (max(c.high for c in previous) if side == 1 else min(c.low for c in previous))
        if not zone['low'] <= level <= zone['high']:
            continue
        if side * (bar.open - level) <= tick / 2 and side * (bar.close - level) >= tick:
            return {'level': level, 'known_ts': known, 'side': side}
    return None


def event_rvol(snapshot, timeframe, known_ts):
    """Event volume / the preceding 20 volumes, excluding the event itself."""
    step={'1m':60_000,'3m':180_000,'5m':300_000,
          '15m':900_000,'1H':3_600_000,'4H':14_400_000}.get(timeframe)
    result={'timeframe':timeframe,'known_ts':known_ts,'rvol':None,
            'sma_volume_20':None,'status':'EVENT_OR_HISTORY_MISSING'}
    if not step or not isinstance(known_ts,(int,float)) or known_ts>snapshot.now:
        return result
    bars = closed_five_minutes(snapshot) if timeframe == '5m' else snapshot.candles.get(timeframe, [])
    rows={c.ts:c for c in bars
          if c.confirmed and c.ts+step<=known_ts}
    event=rows.get(known_ts-step)
    prior=[rows.get(known_ts-step-i*step) for i in range(20,0,-1)]
    if event is None or any(c is None for c in prior):
        return result
    volumes=[c.volume for c in prior]
    if not all(isfinite(v) and v>=0 for v in [event.volume,*volumes]):
        return {**result,'status':'INVALID_VOLUME'}
    average=sum(volumes)/20
    if average<=0:
        return {**result,'status':'NO_VOLUME_BASELINE'}
    return {**result,'rvol':event.volume/average,'volume':event.volume,
            'sma_volume_20':average,'status':'AVAILABLE'}


def formation_volume(zone, snapshot):
    """Validate only the closed impulse that formed this native zone."""
    timeframe = (zone.get('wave_proof') or {}).get('timeframe', zone.get('timeframe'))
    known = zone.get('impulse_known_ts', zone.get('known_ts'))
    result = event_rvol(snapshot, timeframe, known)
    confirmed = (result['status'] == 'AVAILABLE'
                 and result['volume'] > 1.35 * result['sma_volume_20'])
    return {**result, 'confirmed': confirmed}


def audit_rows(snapshot, after=0):
    """Prefer closed minutes; explicitly retain coarse history for old archives."""
    tf, step = ('1m', 60_000) if snapshot.candles.get('1m') else ('3m', 180_000)
    return sorted((c for c in snapshot.candles.get(tf, [])
                   if c.confirmed and c.ts >= after and c.ts+step <= snapshot.now), key=lambda c:c.ts)


def catch_up(snapshot, entry, stop, side, known_ts, tick, zone=None):
    """Authorize a new CURRENT entry, never a retroactive historic fill."""
    risk = side*(entry-stop)
    now = snapshot.now
    start = max(known_ts, (now//60_000)*60_000-15*60_000)
    rows = [c for c in snapshot.candles.get('1m', [])
            if c.confirmed and start <= c.ts and c.ts+60_000 <= now]
    rows.sort(key=lambda c:c.ts)
    complete = bool(rows and rows[0].ts <= ((start+59999)//60_000)*60_000 and
        rows[-1].ts+60_000 == (now//60_000)*60_000 and
        all(b.ts-a.ts==60_000 for a,b in zip(rows,rows[1:])))
    touch = next((i for i,c in enumerate(rows) if c.low<=entry<=c.high), None)
    price = snapshot.ask if side==1 else snapshot.bid
    moved = side*(price-entry)/risk if risk>0 else float('inf')
    intact = touch is not None and all((c.low>stop if side==1 else c.high<stop) for c in rows[touch:])
    spent = touch is not None and any((c.high>=entry+side*.5*risk if side==1
        else c.low<=entry+side*.5*risk) for c in rows[touch+1:])
    confirmation=closed_reaction(snapshot,side,zone or {'low':entry,'high':entry},
                                 rows[touch].ts if touch is not None else now)
    boundary=(zone or {}).get('high' if side==1 else 'low',entry)
    returning=side*(price-boundary)<0
    allowed = bool(risk>0 and complete and touch is not None and intact and not spent and
                   confirmation and not returning and
                   -tick/risk <= moved <= .25+1e-9)
    return {'allowed':allowed, 'move_r':moved, 'complete_1m':complete,
            'touch_ts':rows[touch].ts if touch is not None else None,
            'confirmation':confirmation,'returning_against_entry':returning,
            'status':'ACTIVE_ENTRY' if allowed else 'WAIT_RETEST', 'never_backdated':True}


def closed_reaction(snapshot, side, zone, touch_ts):
    """Latest closed 5m/15m bar must reclaim the zone after its actual touch."""
    for tf,step in (('5m',300_000),('15m',900_000)):
        rows = closed_five_minutes(snapshot) if tf == '5m' else sorted((c for c in snapshot.candles.get(tf,[]) if c.confirmed
                     and c.ts+step<=snapshot.now),key=lambda c:c.ts)
        if not rows:
            continue
        c=rows[-1];boundary=zone['high'] if side==1 else zone['low']
        if (c.ts+step>touch_ts+60_000 and snapshot.now-(c.ts+step)<step
                and side*(c.close-c.open)>0 and side*(c.close-boundary)>0):
            return {'timeframe':tf,'known_ts':c.ts+step,'close':c.close}
    return None


ORGANIC_MOMENTUM = frozenset({'BREAKER_FLIP_15M', 'DISPLACEMENT_CATCHER_15M', 'IMPULSE_OTE_15M'})

ORGANIC_REVERSAL = 'SESSION_SWEEP_ENGULFING_15M'
ORGANIC_FORMATIONS = ORGANIC_MOMENTUM | {ORGANIC_REVERSAL}


def organic_anchors(snapshot, cfg, context, engine):
    import strategy_organics as organic

    import directional_policy as direction

    anchors = []
    wave_setups = [{'entry_zone': zone, 'side': zone['side'], 'family': zone['kind'],
                    'signal_ts': zone['known_ts'],
                    'invalidation': zone['low'] if zone['side'] == 1 else zone['high'],
                    'organic_confluence': True}
                   for zone in wave.zones(snapshot, cfg, engine,
                                         direction.execution_regime(snapshot, cfg, engine))]
    for setup in [*wave_setups, *organic.formations(snapshot, cfg.tick, engine)]:
        zone = setup['entry_zone']
        side = setup['side']
        family = setup['family']
        proximal = zone['high'] if side == 1 else zone['low']
        raw_entry = (zone['reference'] if family in wave.TREND_ZONES else
                     (zone['low'] + zone['high']) / 2 if family in ('DISPLACEMENT_CATCHER_15M', 'IMPULSE_OTE_15M', 'CONTINUATION_PULLBACK')
                     else proximal)
        entry = engine.round_tick(round(raw_entry + cfg.tick/2, 10), cfg.tick)
        if family == ORGANIC_REVERSAL or family in wave.REVERSALS:
            name = 'SWEEP_RECLAIM' if side == 1 else 'LIQUIDITY_SWEEP_REVERSAL_SHORT'
        else:
            name = 'BREAKOUT_RETEST' if family in ('BREAKER_FLIP_15M', 'BEARISH_BREAKER', 'BULLISH_BREAKER', 'FLAG_RETEST', 'BREAKER_BLOCK', 'BOS_CONTINUATION') else 'PULLBACK_CONTINUATION'
        known = setup['signal_ts']
        evidence = {**setup, 'entry_route': 'STRATEGY_EVENT', 'setup_timeframe': '15m',
                    'formation_timeframe': zone['timeframe'], 'source_strategy': family,
                    'strategy_signal_ts': known, 'strategy_event_ts': known,
                    'episode_ts': known,
                    'setup_conditions': [{'condition': 'Патерн підтверджено закритими свічками',
                                          'met': True}]}
        anchor = cfg.core.make_anchor(
            name,
            'LONG' if side == 1 else 'SHORT', entry, setup['invalidation'],
            family, family, context, score=84 if family == 'RECLAIM_BREAKER' else 75,
            evidence=evidence, ttl_minutes=30)
        if anchor is not None:
            anchor.id = engine.ident(family, side, known, entry)
            anchor.created_ts = known
            anchor.expires_ts = known + (wave.TREND_ZONE_AGE if family in wave.TREND_ZONES or family == 'RECLAIM_BREAKER' else 2 * engine.TF['15m'])
            anchors.append(anchor)
    return anchors


def organic_zone_origin(zone, snapshot, side, tick, engine):
    from dataclasses import replace
    import strategy_organics as organic

    known = zone['known_ts']
    candle = next((c for c in organic.closed(snapshot, '15m')
                   if c.ts + engine.TF['15m'] == known), None)
    if candle is not None:
        at_origin = replace(snapshot, now=known, price=candle.close)
        for witnessed in organic.formations(at_origin, tick, engine):
            if witnessed['side'] == side and witnessed['entry_zone'] == zone:
                if zone.get('sweep'):
                    return {'confirmed': True, 'timeframe': '15m', 'reference': zone['reference'],
                            'sweep': zone['sweep'], 'reason': 'SESSION_SWEEP_ENGULFING_CONFIRMED'}
                return {'confirmed': True, 'timeframe': '15m', 'reference': zone['reference'],
                        'break_known_ts': known, 'displacement_known_ts': known,
                        'reason': 'ORGANIC_M15_ORIGIN_CONFIRMED'}
    return {'confirmed': False, 'reason': 'ZONE_ORIGIN_BOS_REQUIRED'}


def sweep_reclaim_zone(anchor, snapshot, side, cfg, engine):
    """Build a reversal zone from the raid wick and its reclaimed reference."""
    import directional_policy as direction
    import signal_quality as quality

    if quality.CONTRACTS.get(anchor.setup_type, ('', ''))[1] not in ('reversal', 'range'):
        return None, None
    tf = anchor.evidence.get('formation_timeframe', anchor.evidence.get('setup_timeframe', '15m'))
    if tf not in engine.TF:
        return None, None
    proof = direction.evidence({'side': side, 'entry': anchor.level, 'formation_timeframe': tf},
                               snapshot, cfg.tick)
    sweep = proof['latest_sweep']
    if not sweep or snapshot.now - sweep['known_ts'] >= 2 * engine.TF[tf]:
        return None, None
    low, high = sorted((sweep['level'], sweep['extreme']))
    if low <= 0 or high <= low or not low - cfg.tick <= anchor.level <= high + cfg.tick:
        return None, None
    zone = {'kind': 'SWEEP_RECLAIM_ZONE', 'low': low, 'high': high,
            'known_ts': sweep['known_ts'], 'timeframe': tf}
    origin = {'confirmed': True, 'timeframe': tf, 'reference': sweep['level'],
              'sweep': sweep, 'reason': 'ZONE_ORIGIN_SWEEP_CONFIRMED'}
    return zone, origin


def zone_origin(zone, snapshot, side, tick, engine, cfg=None, regime=None):
    """Prove departure from this zone broke a pre-existing local reference.

    The reference is frozen before formation. Neither a later pivot nor an
    unrelated break elsewhere can turn a weak historical bounce into an OB.
    """
    if zone.get('wave_proof'):
        witness = wave.witnessed(zone, snapshot, cfg, engine, regime) if cfg and regime else None
        if witness:
            sweep = witness.get('sweep') or {}
            return {'confirmed': True, 'timeframe': witness['wave_proof']['timeframe'],
                    'reference': witness['reference'], 'sweep': sweep,
                    'break_known_ts': witness['wave_proof'].get('break_ts', witness['known_ts'])
                                      if sweep.get('pattern') != 'WICK_RECLAIM' else None,
                    'displacement_known_ts': witness['wave_proof'].get('break_ts', witness['known_ts']),
                    'wave_confirmed': True, 'reason': 'CURRENT_WAVE_CONFIRMED'}
        return {'confirmed': False, 'reason': 'CURRENT_WAVE_PROOF_EXPIRED'}
    if zone.get('kind') in ORGANIC_FORMATIONS:
        return organic_zone_origin(zone, snapshot, side, tick, engine)
    tf=zone.get('timeframe','15m');step=engine.TF.get(tf)
    if not step or zone['high']<=zone['low']:
        return {'confirmed':False,'reason':'ZONE_ORIGIN_BOS_REQUIRED'}
    rows=sorted((c for c in snapshot.candles.get(tf,[]) if c.confirmed
                 and c.ts+step<=snapshot.now),key=lambda c:c.ts)
    origin=zone.get('origin_ts',zone['known_ts']-2*step)
    before=[c for c in rows if c.ts<origin]
    if len(before)<6:
        return {'confirmed':False,'reason':'ZONE_ORIGIN_HISTORY_MISSING'}
    refs=[p for p in engine.pivots(before, strength=1 if tf == '15m' else 2) if p['kind']==('HIGH' if side==1 else 'LOW')
          and p['known_ts']+step<=origin]
    reference=refs[-1]['level'] if refs else (max(c.high for c in before[-6:])
                                                     if side==1 else min(c.low for c in before[-6:]))
    volatility=max(engine.atr(before),tick)
    formation=[c for c in rows if origin<=c.ts<=zone['known_ts']+3*step]
    departed=False
    departure_known=None
    previous=before[-1]
    for c in formation:
        near=any(b.low<=zone['high'] and b.high>=zone['low'] for b in rows
                 if origin<=b.ts<=c.ts)
        crossed=side*(previous.close-reference)<=0 and side*(c.close-reference)>tick
        displaced=side*(c.close-c.open)>=.6*volatility
        if near and displaced and not departed:
            departure_known=c.ts+step
        departed=departed or (near and displaced)
        if departed and crossed:
            return {'confirmed':True,'timeframe':tf,'reference':reference,
                    'reference_known_before_ts':origin,'break_known_ts':c.ts+step,
                    'displacement_known_ts':departure_known,
                    'reason':'ZONE_ORIGIN_BOS_CONFIRMED'}
        previous=c
    return {'confirmed':False,'reason':'ZONE_ORIGIN_BOS_REQUIRED','reference':reference}


def passive_gate(entry, side, bid, ask, tick):
    """Marketable limits cross the opposite quote, not the last trade price."""
    e=Decimal(str(entry));t=Decimal(str(tick))
    quote=ask if side==1 else bid
    passive=entry<ask if side==1 else entry>bid
    boundary=((e/t).to_integral_value(rounding=ROUND_FLOOR)+1)*t if side==1 else ((e/t).to_integral_value(rounding=ROUND_CEILING)-1)*t
    return {'passive':passive,'quote_name':'ASK' if side==1 else 'BID',
            'current_quote':quote,'required_quote':float(boundary),
            'operator':'≥' if side==1 else '≤'}


def distance_details(price, entry, tick):
    gap=abs(Decimal(str(price))-Decimal(str(entry)))
    percentage=float(gap/Decimal(str(price))*100) if price>0 else None
    ticks=float(gap/Decimal(str(tick))) if tick>0 else None
    return {'at_level':gap==0,'distance':float(gap),'distance_pct':percentage,'ticks':ticks}


def distance_text(price, entry, tick):
    detail=distance_details(price,entry,tick)
    if detail['at_level']:
        return f'📍 Ціна на рівні входу {entry:.8g} • 0 тиків'
    pct=detail['distance_pct']
    value=f'{pct:.3f}%' if pct>=.0005 else '<0.001%'
    ticks=detail['ticks']
    tick_text=str(round(ticks)) if abs(ticks-round(ticks))<1e-8 else f'{ticks:.6f}'.rstrip('0').rstrip('.')
    if tick_text=='0' and ticks>0:
        tick_text='<0.000001'
    return f"📏 До входу: {value} • {detail['distance']:.8g} USDT • {tick_text} тиків"


def wait_text(entry, side, bid, ask, tick):
    gate=passive_gate(entry,side,bid,ask,tick)
    return (f"🚦 Ліміт {'LONG' if side==1 else 'SHORT'} {entry:.8g}: чекаємо "
            +f"{gate['quote_name']} {gate['operator']} {gate['required_quote']:.8g} "
            +f"(зараз {gate['current_quote']:.8g})")

ENTRY_CHECKS = (
    ('PASSIVE_LIMIT','Ліміт стоїть на правильному боці ціни'),
    ('FRESH_LEVEL','Рівень актуальний і ще не відвіданий'),
    ('VALID_STOP','Структурний SL відповідає межам ризику'),
    ('HTF_ALIGNMENT','Старші графіки допускають цей напрямок'),
    ('TARGET_RR','Потенціал TP достатній відносно SL'),
    ('CONTEXT_SCORE','Якість контексту проходить поріг'),
    ('STRUCTURAL_CONFIRMATION','Структурні умови входу підтверджені'),
    ('DECISION_EVALUATED','Сценарій оцінено без порогу win rate'),
)


def entry_checks():
    return [{'key':key,'condition':label,'met':None} for key,label in ENTRY_CHECKS]


def update_readiness(row):
    """Count setup prerequisites AND actual admission checks, not distance.

    None means not evaluated yet and does not count as passed. Portfolio
    capacity is separate: an otherwise ready setup stays ready while busy.
    """
    pattern=row.get('setup_conditions',row.get('conditions',[]))
    checks=row.setdefault('entry_checks',entry_checks())
    passed=sum(c.get('met') is True for c in pattern+checks)
    total=len(pattern)+len(checks)
    row['setup_readiness_pct']=round(100*sum(c.get('met') is True for c in pattern)/len(pattern),1) if pattern else 0.
    row['readiness_pct']=round(100*passed/total,1) if total else 0.
    row['met_conditions']=passed
    row['total_conditions']=total
    row['missing_conditions']=[c['condition'] for c in pattern+checks if c.get('met') is not True]
    return row


def selection_key(row, price=None):
    """Same deterministic ranking for the book, Telegram and dashboard.

    Completion is first. With equal readiness use independent historical
    confidence when available, then context quality and price distance.
    An unvalidated estimate is never used as an invented win probability.
    """
    ready=row.get('readiness_pct',100. if row.get('quality',{}).get('accepted') else 0.) or 0.
    stat=row.get('statistics',row.get('quality',{}).get('statistics',{}))
    adjustment=stat.get('ranking_adjustment',0.)
    level=row.get('level',row.get('entry'))
    distance=proximity(price,level)['distance_pct'] if price else row.get('distance_pct')
    tf=row.get('timeframe',row.get('setup_timeframe','15m'))
    return (float(ready),float(row.get('decision_score',float(row.get('score',0) or 0)+adjustment)),float(row.get('score',0) or 0),
            -float(distance) if distance is not None else float('-inf'),
            MINUTES.get(tf,15),str(row.get('setup','')),str(row.get('id',row.get('event_key','')) or ''))


def best_scenario(rows, price=None, sides=None):
    eligible=[r for r in rows if r.get('status') not in ('ERROR','FOLLOWED')
              and r.get('side') in (-1,1) and (sides is None or r.get('side') in sides)]
    return max(eligible,key=lambda r:selection_key(r,price),default=None)


def timeframe(plan):
    tf = plan.get('setup_timeframe', plan.get('timeframe', '15m'))
    return tf if tf in PRIMARY else '15m'


def build_contexts(snapshot, base, state, cfg):
    """Reuse detector slot names, with real bars and volatility of each TF.

    Session references ALWAYS come from real 15m data: four 4H candles are
    never passed off as an opening range. SMT peers exist only for real 15m.
    """
    core = cfg.core
    out = {'15m': dict(base, analysis_timeframe='15m', micro_timeframe='3m')}
    for tf in PRIMARY[1:]:
        micro = MICRO[tf]
        bars = {**snapshot.candles, '15m': snapshot.candles[tf],
                '3m': snapshot.candles[micro]}
        data = {**snapshot.metadata, 'price': snapshot.price,
                'spread': snapshot.ask-snapshot.bid, 'candles': bars,
                'execution_price_trusted': snapshot.trusted,
                'ticker_ts': snapshot.ticker_ts, 'instrument': cfg.instrument,
                'htf_source': {t: 'CONFIRMED_SNAPSHOT' for t in ('1H','4H')}}
        ctx = core.build_context(data, state, {'trades': state.get('trades', [])})
        for key in ('session', 'session_name', 'vwap', 'day_open', 'week_open',
                    'prev_day_high', 'prev_day_low'):
            if key in base:
                ctx[key] = base[key]
        ctx.update(analysis_timeframe=tf, micro_timeframe=micro)
        out[tf] = ctx
    return out


def conditions(setup, ctx, core, anchor=None):
    """Named boolean prerequisites matching each original detector's logic.

    Alternative levels/sides are evaluated as separate branches; report the
    most developed branch. Actual detector output remains authoritative.
    """
    c = [x for x in ctx.get('candles', {}).get('15m', []) if x.confirmed]
    m = [x for x in ctx.get('candles', {}).get('3m', []) if x.confirmed]
    a = max(ctx.get('atr15', 0), 1e-9)
    am = max(ctx.get('atr3', 0), 1e-9)
    price = ctx.get('price', 0)
    sess = ctx.get('session', {})
    struct = ctx.get('structure15', {})
    shift = ctx.get('structure_shift_15m', {})
    zones = ctx.get('zones', {})
    base = zones.get('consolidation', {})
    side = core._trend_side(ctx)
    sign = core.side_sign(side)
    trend = side in ('LONG','SHORT')
    last = c[-1] if c else None
    check = lambda name, ok: {'condition': name, 'met': bool(ok)}
    branches = []

    def branch(items, side=side, level=None):
        branches.append({'conditions': [check(k,v) for k,v in items],
                         'side': core.side_sign(side), 'level': level if level and level > 0 else None})

    if setup == 'SWEEP_RECLAIM':
        low = min((x.low for x in c[-21:-1]), default=0)
        branch([('Є 24 закриті свічки',len(c)>=24),
                ('Знято попередній мінімум',last and last.low<low),
                ('Закриття повернулося над рівень',last and last.close>low>0),
                ('Закриття у верхній частині свічки',last and core._close_location(last)>=core.REACTION_REJECTION_RATIO)], 'LONG',low)
    elif setup == 'CAPITULATION_RECOVERY':
        rows=m[-18:];vol=[x.volume for x in rows if x.volume>0];avg=mean(vol) if vol else 0
        cluster=[x for x in rows if avg and x.volume>=1.9*avg and core._range(x)>=1.6*am]
        low=min((x.low for x in cluster),default=0)
        branch([('Є 20 закритих молодших свічок',len(m)>=20),('Є обсяги у 8 свічках',len(vol)>=8),
                ('Є дві свічки капітуляції',len(cluster)>=2),('Останнє закриття вище низу',m and m[-1].close>low>0)],'LONG',low)
    elif setup == 'RANGE_EDGE_REVERSAL':
        low=base.get('low') or sess.get('day_low',0);high=base.get('high') or sess.get('day_high',0)
        for sd,level in [('LONG',low),('SHORT',high)]:
            branch([('Режим діапазону або переходу',ctx.get('regime') in ('RANGE','TRANSITION')),
                    ('Є 30 закритих свічок',len(c)>=30),('Діапазон ширший за 2 ATR',low>0 and high-low>=2*a),
                    ('Ціна біля межі: до 0.60 ATR',level>0 and abs(price-level)<=.60*a)],sd,level)
    elif setup == 'FAILED_AUCTION_REJECTION':
        vwap=ctx.get('vwap',0)
        refs=[(sess.get('prev_day_high',0),'SHORT'),(sess.get('prev_day_low',0),'LONG'),
              (sess.get('week_high',0),'SHORT'),(sess.get('week_low',0),'LONG'),
              (vwap,'LONG' if last and last.close>vwap else 'SHORT')]
        for level,sd in refs:
            sn=core.side_sign(sd)
            pierced=last and (last.low<level if sn==1 else last.high>level)
            wick=core._lower_wick_ratio(last) if last and sn==1 else core._upper_wick_ratio(last) if last else 0
            branch([('Є 12 закритих свічок',len(c)>=12),('Є опорний рівень',level>0),
                    ('Ціна вийшла за рівень',level>0 and pierced),('Закриття повернулося за рівень',last and sn*(last.close-level)>0),
                    ('Відмова хвостом підтверджена',wick>=core.REACTION_REJECTION_RATIO)],sd,level)
    elif setup == 'LIQUIDITY_SWEEP_REVERSAL_SHORT':
        sw=ctx.get('sweep',{});level=sw.get('level',0)
        branch([('Є закриті свічки',bool(c)),('Зняття ліквідності виявлено',sw.get('sweep')),
                ('Зняття саме над максимумом',sw.get('kind')=='SWEEP_HIGH')],'SHORT',level)
    elif setup == 'BUYER_EXHAUSTION_SHORT':
        bodies=[core._body(x)/a for x in c[-12:]]
        contraction=len(bodies)>=8 and mean(bodies[-4:])<.62*mean(bodies[:4])
        cvd=ctx.get('cvd',{});delta=cvd.get('available') and not cvd.get('rising',True)
        rejection=bool(c) and mean(core._upper_wick_ratio(x) for x in c[-5:])>=.34
        level=struct.get('last_swing_low') or min((x.low for x in c[-4:]),default=0)
        branch([('Є 30 закритих свічок',len(c)>=30),('Перед цим структура LONG',struct.get('direction')=='LONG'),
                ('Тіла свічок стиснулися до 62%',contraction),('Дельта слабшає або верхні хвости',delta or rejection)],'SHORT',level)
    elif setup == 'PULLBACK_CONTINUATION':
        candidates=[]
        if len(c)>=60:
            ladder=core._ema_ladder(c)
            candidates=[(k,v,58) for k,v in list(ladder.items())[:2]]+ [('VWAP',ctx.get('vwap',0),58)]
            for key in ('order_blocks','fvg'):
                candidates += [(z.kind,(z.low+z.high)/2,56+8*z.strength if key=='order_blocks' else 54+6*z.strength)
                               for z in zones.get(key,[]) if z.side==side]
            candidates += [('SWING',v,55) for v in struct.get('swing_lows' if sign==1 else 'swing_highs',[])[-2:]]
        valid=[x for x in candidates if x[1]>0 and sign*(x[1]-price)<=.10*a]
        level=max(valid,key=lambda x:x[2])[1] if valid else None
        branch([('Є напрямок тренду',trend),('Режим допускає відкат',ctx.get('regime') in ('TREND','NORMAL','TRANSITION')),
                ('Є 60 закритих свічок',len(c)>=60),('Є рівень відкату за ціною',bool(valid))],side,level)
    elif setup == 'FRESH_BASE_CONTINUATION':
        impulse=any(core._body(x)>=.9*a and sign*(x.close-x.open)>0 for x in c[-4:])
        level=base.get('low' if sign==1 else 'high',0)
        branch([('Є напрямок тренду',trend),('Є 20 закритих свічок',len(c)>=20),
                ('Сформована база',base.get('found') and base.get('low',0)>0 and base.get('high',0)>0),
                ('Імпульс від бази: 0.9 ATR',impulse)],side,level)
    elif setup in ('ACCEPTANCE_RETEST_CONTINUATION','DIRECTION_FLIP_15M'):
        sd=shift.get('side','NEUTRAL');level=shift.get('level',0)
        age=core.ACCEPTANCE_MAX_AGE_BARS if setup=='ACCEPTANCE_RETEST_CONTINUATION' else 3
        branch([('Пробій структури підтверджений',shift.get('shift')),
                ('Є напрямок і зламаний рівень',sd in ('LONG','SHORT') and level>0),
                ('Зміна структури ще свіжа',shift.get('age_bars',99)<=age)],sd,level)
    elif setup == 'MOMENTUM_NO_PULLBACK_CONTINUATION':
        rows=c[-6:];impulse=[x for x in rows if core._body(x)>=1.25*a and sign*(x.close-x.open)>0]
        gaps=[z for z in zones.get('fvg',[]) if z.side==side and not z.mitigated]
        level=(gaps[-1].high if sign==1 else gaps[-1].low) if gaps else impulse[-1].open if impulse else None
        branch([('Є напрямок тренду',trend),('Є 20 закритих свічок',len(c)>=20),
                ('Є імпульс 1.25 ATR',bool(impulse)),('Рух за 6 свічок не менше 2 ATR',len(rows)>=2 and sign*(rows[-1].close-rows[0].close)>=2*a)],side,level)
    elif setup == 'ACCELERATION_PULLBACK_REENTRY':
        rows=m[-24:];legs=[x for x in rows if core._body(x)>=1.10*am and sign*(x.close-x.open)>0]
        tail=rows[rows.index(legs[len(legs)//2])+1:] if legs else []
        level=(min(x.low for x in tail) if sign==1 else max(x.high for x in tail)) if tail else None
        depth=sign*(legs[len(legs)//2].close-level) if legs and level else 0
        branch([('Є напрямок тренду',trend),('Є 30 молодших і 20 основних свічок',len(m)>=30 and len(c)>=20),
                ('Є дві імпульсні ноги',len(legs)>=2),('Є відкат після першої ноги',bool(tail)),
                ('Глибина відкату 0.30–1.60 ATR',.30*a<=depth<=1.60*a)],side,level)
    elif setup == 'TREND_IGNITION':
        prior=core.detect_consolidation(c[:-1],a) if len(c)>=20 else {}
        for sd,key in [('LONG','high'),('SHORT','low')]:
            level=prior.get(key,0);sn=core.side_sign(sd)
            branch([('Є 20 закритих свічок',len(c)>=20),('База існувала до пробою',prior.get('found')),
                    ('Закриття за межею бази',last and level>0 and sn*(last.close-level)>0),
                    ('Тіло пробою не менше 1.10 ATR',last and core._body(last)>=1.10*a)],sd,level)
    elif setup == 'BREAKOUT_RETEST':
        for key,sd in [('prev_day_high','LONG'),('week_high','LONG'),('prev_day_low','SHORT'),('week_low','SHORT')]:
            level=sess.get(key,0);sn=core.side_sign(sd)
            branch([('Є 20 закритих свічок',len(c)>=20),('Є рівень дня або тижня',level>0),
                    ('Пробій підтверджено закриттям',level>0 and any(sn*(x.close-level)>.10*a for x in c[-6:]))],sd,level)
    elif setup == 'RANGE_COMPRESSION_BREAKOUT':
        sd='LONG' if price>=base.get('mid',0) else 'SHORT';level=base.get('low' if sd=='LONG' else 'high',0)
        branch([('База сформована',base.get('found')),('Стиснення до 1.40 ATR',base.get('compression',99)<=1.4),
                ('Є поточна ціна',price>0)],sd,level)
    elif setup == 'OPENING_RANGE_BREAKOUT':
        low=sess.get('opening_range_low',0);high=sess.get('opening_range_high',0)
        sd='LONG' if price>=(high+low)/2 else 'SHORT';level=high if sd=='LONG' else low
        branch([('Opening range завершено',sess.get('opening_range_complete')),('Є межі opening range',low>0 and high>0),
                ('Ширина не менше 0.50 ATR',high-low>=.5*a)],sd,level)
    elif setup == 'LIQUIDITY_LADDER':
        for key,sd in [('equal_lows','LONG'),('equal_highs','SHORT')]:
            pools=[p for p in ctx.get('liquidity',{}).get(key,[]) if p.get('touches',0)>=2]
            level=min(pools,key=lambda x:abs(x.get('level',0)-price)).get('level',0) if pools else 0
            branch([('Є поточна ціна',price>0),('Є рівень із двома дотиками',bool(pools)),
                    ('Відстань до рівня до 2.50 ATR',level>0 and abs(price-level)<=2.5*a)],sd,level)
    elif setup in ('FAILED_OPENING_RANGE_BREAKOUT','OR_FAILURE_2_SHORT'):
        refs=[(sess.get('opening_range_high',0),'SHORT')]
        if setup=='FAILED_OPENING_RANGE_BREAKOUT': refs.append((sess.get('opening_range_low',0),'LONG'))
        rows=c[-10:] if setup=='OR_FAILURE_2_SHORT' else c[-6:]
        for level,sd in refs:
            pierced=[x for x in rows if x.high>level] if sd=='SHORT' else [x for x in rows if x.low<level]
            refused=[x for x in pierced if core.side_sign(sd)*(x.close-level)>0]
            branch([('Opening range завершено',sess.get('opening_range_complete')),('Є рівень і свічки',level>0 and len(c)>=(12 if setup=='OR_FAILURE_2_SHORT' else 8)),
                    ('Є потрібна кількість пробоїв',len(pierced)>=(2 if setup=='OR_FAILURE_2_SHORT' else 1)),
                    ('Усі спроби закрилися всередині',bool(pierced) and len(refused)==len(pierced))],sd,level)
    elif setup == 'FAILED_BREAKOUT_SHORT':
        level=max((x.high for x in c[-21:-1]),default=0)
        branch([('Є 24 закриті свічки',len(c)>=24),('Знято попередній максимум',last and last.high>level>0),
                ('Закриття під рівнем',last and last.close<level),
                ('Підтверджена відмова від максимуму',last and (core._upper_wick_ratio(last)>=core.REACTION_REJECTION_RATIO or core._close_location(last)<=.45))],'SHORT',level)
    elif setup == 'MSS_REVERSAL_SHORT':
        level=shift.get('level') or max((x.high for x in c[-6:]),default=0)
        branch([('Є 30 закритих свічок',len(c)>=30),('MSS спрямований вниз',shift.get('side')=='SHORT'),
                ('Ціна нижче початку попередньої ноги',len(c)>=20 and c[-1].close<min(x.close for x in c[-20:-14])),
                ('Є рівень ретесту',level>0)],'SHORT',level)
    elif setup in ('SESSION_MEAN_RECLAIM','DAILY_WEEKLY_OPEN_RECLAIM'):
        refs=[ctx.get('vwap',0)] if setup=='SESSION_MEAN_RECLAIM' else [sess.get('day_open',0),sess.get('week_open',0)]
        for level in refs:
            sn=1 if price>=level else -1;sd='LONG' if sn==1 else 'SHORT'
            displaced=any(sn*(x.close-level)>1.10*a for x in c[-10:-1]) if setup=='SESSION_MEAN_RECLAIM' else any(sn*(x.close-level)<-.35*a for x in c[-10:-1])
            branch([('Є 12 закритих свічок',len(c)>=12),('Є опорний рівень вартості',level>0),
                    ('Є попередній відхід від рівня',displaced)],sd,level)
    elif setup == 'TIME_OF_DAY_ADAPTIVE':
        low=sess.get('asia_low',0);high=sess.get('asia_high',0)
        for sd,level in [('LONG',low),('SHORT',high)]:
            branch([('Сесія Лондона або Нью-Йорка',ctx.get('session_name') in ('LONDON_OPEN','LONDON','NY_OVERLAP','NY_PM')),
                    ('Азійський діапазон завершено',sess.get('asia_complete')),('Діапазон ширший за 0.80 ATR',low>0 and high-low>=.8*a),
                    ('Ціна біля межі: до 0.75 ATR',level>0 and abs(price-level)<=.75*a)],sd,level)
    else:
        raise ValueError('Missing setup conditions: '+setup)

    if anchor:
        # A detector has confirmed its OWN branch, which may not be the closest.
        selected=min(branches,key=lambda b:(b['side']!=core.side_sign(anchor.side),
                     abs((b['level'] or anchor.level)-anchor.level)))
        selected.update(side=core.side_sign(anchor.side),level=anchor.level)
        # Detector output remains authoritative if a display prerequisite differs.
        ready=100.;status='DETECTED'
    else:
        selected=max(branches,key=lambda b:(sum(x['met'] for x in b['conditions'])/len(b['conditions']),
                           -abs(price-(b['level'] or price))))
        ready=round(100*sum(x['met'] for x in selected['conditions'])/len(selected['conditions']),1)
        status='FORMING' if ready>0 else 'NO_SETUP'
        # Never report fully ready when the authoritative detector returned None.
        if ready==100:
            selected['conditions'].append(check('Оригінальний детектор підтвердив сетап',False))
            ready=round(100*(len(selected['conditions'])-1)/len(selected['conditions']),1)
    selected.update(setup=setup,readiness_pct=ready,status=status,
                    met_conditions=sum(x['met'] for x in selected['conditions']),
                    total_conditions=len(selected['conditions']),
                    missing_conditions=[x['condition'] for x in selected['conditions'] if not x['met']])
    return selected


def proximity(price, entry, initial=None):
    if not entry or entry<=0 or price<=0:
        return {'distance_pct':None,'approach_pct':None}
    distance=abs(price-entry)
    initial_distance=abs(initial-entry) if initial is not None else None
    approach=max(0.,min(100.,100*(1-distance/initial_distance))) if initial_distance and initial_distance>0 else None
    return {'distance_pct':100*distance/price,'approach_pct':approach}

# Exact original registry order, including the historic 15M enum identifier.
SETUP_NAMES = (
    'SWEEP_RECLAIM','CAPITULATION_RECOVERY','RANGE_EDGE_REVERSAL',
    'FAILED_AUCTION_REJECTION','LIQUIDITY_SWEEP_REVERSAL_SHORT','BUYER_EXHAUSTION_SHORT',
    'PULLBACK_CONTINUATION','FRESH_BASE_CONTINUATION','ACCEPTANCE_RETEST_CONTINUATION',
    'MOMENTUM_NO_PULLBACK_CONTINUATION','ACCELERATION_PULLBACK_REENTRY','DIRECTION_FLIP_15M',
    'TREND_IGNITION','BREAKOUT_RETEST','RANGE_COMPRESSION_BREAKOUT','OPENING_RANGE_BREAKOUT',
    'LIQUIDITY_LADDER','FAILED_OPENING_RANGE_BREAKOUT','FAILED_BREAKOUT_SHORT','MSS_REVERSAL_SHORT',
    'OR_FAILURE_2_SHORT','SESSION_MEAN_RECLAIM','DAILY_WEEKLY_OPEN_RECLAIM','TIME_OF_DAY_ADAPTIVE',
)
