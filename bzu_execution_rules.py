"""Closed-candle entry routing and full-volume target selection for Brent."""
from dataclasses import replace

import directional_policy as direction
import ict_methodology as ict
import oil_risk
import setup_scanner as scanner

EPS = 1e-8
FULL_TARGET_MIN_R = oil_risk.ENTRY_TP1_MIN
FULL_TARGET_MAX_R = 1.88
RETEST_TTL_MS = 60 * 60_000
TREND_RETEST_TTL_MS = 90 * 60_000
ALIGNED_RETEST_TTL_MS = 120 * 60_000
EARLY_NY = 'NY_POST_SWEEP'


def early_ny(ts):
    return 13 * oil_risk.H1 <= ts % oil_risk.DAY < (13 * 60 + 105) * 60_000


def asia_session(ts):
    return ts is not None and 0 <= ts % oil_risk.DAY < 7 * oil_risk.H1


def pre_london(ts):
    return ts is not None and 5 * oil_risk.H1 <= ts % oil_risk.DAY < 7 * oil_risk.H1


def range_location(snapshot, price):
    cache = vars(snapshot).setdefault('_entry_ranges', {})
    if snapshot.now not in cache:
        bars = [b for b in direction.closed(snapshot,'15m')[-96:] if b.ts >= snapshot.now-oil_risk.DAY]
        cache[snapshot.now] = (min((b.low for b in bars), default=price),
                               max((b.high for b in bars), default=price))
    low, high = cache[snapshot.now]
    return (price-low)/(high-low) if high > low else None


def fresh_aligned_impulse(snapshot, side, regime, zone, origin):
    if regime.get('side_4h') != side:
        return False
    location = range_location(snapshot,snapshot.price)
    wave = zone.get('wave_proof') or {}
    known = (wave.get('break_ts') or origin.get('break_known_ts') or
             (zone.get('known_ts') if wave.get('first_retest') else None))
    return (location is not None and side*(location-.5) <= 0 and known is not None
            and 0 <= snapshot.now-known < 2*oil_risk.M15)


def refined_entry(snapshot, side, zone, fallback, tick, engine, *, early, late):
    low, high = zone['low'], zone['high']
    if late and 'FVG' in zone['kind']:
        price, basis = (low+high)/2, 'LATE_FVG_MIDPOINT'
    elif late:
        ema, _ = direction.trend_references(snapshot)
        if ema is None or not low <= ema <= high or side*(ema-fallback) > -tick:
            return fallback, None
        price, basis = ema, 'LATE_EMA20_RETEST'
    elif early and 'FVG' in zone['kind']:
        proximal = high if side == 1 else low
        price, basis = proximal-side*.20*(high-low), 'EARLY_FVG_20_PCT'
    else:
        return fallback, None
    # Round toward the interior of the zone.
    return engine.round_tick(round(price,10),tick,up=side == -1), basis


def _sweep(snapshot, rows, side, tick, engine, *, zone=None, lookback=3):
    """A closed wick raid and reclaim of a level known before the raid."""
    for raid in reversed(rows[-lookback:]):
        before = replace(snapshot, now=raid.ts)
        pools = ict.session_liquidity(before)['pools']
        pools = [p for p in pools if p['name'].endswith('LOW' if side == 1 else 'HIGH')]
        pools += oil_risk.macro_extrema(snapshot, raid.ts, engine, side)
        pools += [{'name': 'M15_SWING', 'level': p['level'], 'known_ts': p['known_ts']+oil_risk.M15}
                  for p in engine.pivots([bar for bar in rows if bar.ts < raid.ts][-30:], strength=2)
                  if p['kind'] == ('LOW' if side == 1 else 'HIGH')]
        if zone and not (raid.low <= zone['high']+tick and raid.high >= zone['low']-tick):
            continue
        extreme = raid.low if side == 1 else raid.high
        level = min((p for p in pools if p['known_ts'] <= raid.ts
                     and side*(p['level']-extreme) >= tick-EPS
                     and side*(raid.close-p['level']) >= tick-EPS),
                    key=lambda p: abs(p['level']-extreme), default=None)
        if level:
            return {**level, 'extreme': extreme, 'sweep_ts': raid.ts+oil_risk.M15}
    return None


def opening_trigger(snapshot, side, zone, entry, risk, cfg, engine, *, allow_late_retest=False):
    if not early_ny(snapshot.now):
        return None
    rows = direction.closed(snapshot, '15m')
    if len(rows) < 15 or not snapshot.trusted:
        return None
    last = rows[-1]
    known = last.ts + oil_risk.M15
    atr = engine.atr(rows[:-1])
    quote = snapshot.ask if side == 1 else snapshot.bid
    if (not 0 <= snapshot.ticker_ts-known < oil_risk.M15
            or side*(last.close-last.open) < max(cfg.tick, 1.2*atr)-EPS
            or side*(quote-(last.open+last.close)/2) < 0):
        return None
    maximum = max(2*cfg.tick, .65*atr)
    if abs(quote-last.close) > maximum:
        late = (allow_late_retest and snapshot.metadata.get('scan_gap_ms',0)>600_000
                and snapshot.now-known>600_000 and side*(quote-last.close)>maximum)
        if not late:
            return None
    proof = _sweep(snapshot, rows, side, cfg.tick, engine, zone=zone)
    if proof is None or side*(last.close-proof['level']) <= 0:
        return None
    return {'pattern': EARLY_NY, 'known_ts': known, 'limit_entry': entry, 'initial_risk': risk,
            'max_distance': maximum, 'reference_close': last.close,
            'sweep': proof}


def retest_reason(snapshot, side, quote, trigger, regime, atr, tick):
    ema, _ = direction.trend_references(snapshot)
    if ema is not None and regime.get('side_1h') == regime.get('side_4h') == side:
        if side*(quote-ema) > 1.2*atr:
            return 'HTF_EXTENDED_RETEST'
    known = trigger.get('known_ts') or snapshot.now
    if snapshot.metadata.get('scan_gap_ms', 0) > 600_000 and snapshot.now-known > 600_000:
        reference = trigger.get('reference_close', trigger.get('limit_entry', quote))
        if side*(quote-reference) > max(tick, .20*atr):
            return 'GAP_RETEST'
    return None


def retest_entry(snapshot, zone, side, fallback, reason, tick, engine):
    ema, _ = direction.trend_references(snapshot)
    kind = zone.get('kind', '')
    if 'FVG' in kind:
        level = (zone['low']+zone['high'])/2
    elif reason == 'HTF_EXTENDED_RETEST' and ema is not None:
        level = ema
    else:
        level = fallback
    quote = snapshot.bid if side == 1 else snapshot.ask
    return engine.round_tick(round(min(level, quote) if side == 1 else max(level, quote),10),
                             tick, up=side == -1)


def available_pools(snapshot, levels, tick):
    from limit_structure import unswept_minute_pools

    cache = vars(snapshot).setdefault('_v144_liquidity',{})
    key = (snapshot.now,tick)
    if key not in cache:
        cache[key] = unswept_minute_pools(snapshot,levels,tick)
    return cache[key]



def full_target_ratios_valid(rr1, rr2):
    return FULL_TARGET_MIN_R-EPS <= rr1 <= FULL_TARGET_MAX_R+EPS and abs(rr1-rr2) <= EPS


def full_target(levels, entry, risk, side, atr, tick, engine, *, min_r=None, max_r=None):
    """Choose the nearest reachable buffered pool in the full-exit band."""
    lo_r = FULL_TARGET_MIN_R if min_r is None else min_r
    hi_r = FULL_TARGET_MAX_R if max_r is None else max_r
    low = engine.round_tick(round(entry+side*lo_r*risk,10), tick, up=side == 1)
    high = engine.round_tick(round(entry+side*hi_r*risk,10), tick, up=side == -1)
    targets = sorted((p['level'] for p in levels if p['side'] == -side
                      and side*(p['level']-entry) > 0), key=lambda price: side*(price-entry))
    for pool in targets:
        if side*(pool-low) < tick-EPS:
            continue
        target = engine.round_tick(round(pool-side*max(tick,.15*atr),10), tick, up=side == -1)
        target = max((target,low),key=lambda level:side*level)
        if side*(high-target) < -EPS:
            return high, 'PROJECTION_BEFORE_LIQUIDITY', pool
        return target, 'BUFFERED_LIQUIDITY_POOL', pool
    return low, 'VOLATILITY_PROJECTION', None


def _adx(rows, period=14):
    """Wilder directional movement with a closed-candle warmup."""
    up, down = [], []
    for a,b in zip(rows, rows[1:]):
        high, low = b.high-a.high, a.low-b.low
        up.append(high if high > max(low, 0.) else 0.)
        down.append(low if low > max(high, 0.) else 0.)
    if len(up) < 2*period-1:
        return None
    totals = [sum(values[:period]) for values in (up,down)]
    dx = []
    for i in range(period-1,len(up)):
        if i >= period:
            totals = [old-old/period+items[i] for old,items in zip(totals,(up,down))]
        positive, negative = totals
        dx.append(100*abs(positive-negative)/(positive+negative) if positive+negative else 0.)
    adx = sum(dx[:period])/period
    for value in dx[period:]:
        adx += (value-adx)/period
    return adx


def setup_confirmation(name, zone, snapshot, side, tick, engine, settle, *, approach=None):
    """Recheck the weak setup families without disabling any detector."""
    kind = zone.get('kind','')
    rows = direction.closed(snapshot, '15m')[-70:]
    if not rows:
        return 'WAIT_CLOSED_M15'
    if kind == 'FVG_PULLBACK':
        from entry_adverse_selection import approach_velocity

        level = zone.get('reference', (zone['low'] + zone['high']) / 2)
        brake = approach if approach is not None else approach_velocity(snapshot, side, zone, level, tick, engine)
        return 'WAIT_PULLBACK_REJECTION' if brake['blocked'] else None
    if name == 'BREAKOUT_RETEST' and pre_london(snapshot.now):
        known = zone.get('impulse_known_ts') or zone['known_ts']
        volume = scanner.event_rvol(snapshot, '15m', known).get('rvol') or 0.
        if volume < 1.6 - EPS:
            return 'WAIT_PRE_LONDON_VOLUME'
        return None
    if name == 'LIQUIDITY_SWEEP_REVERSAL_SHORT' or kind == 'MULTIBAR_SWEEP':
        sweep = _sweep(snapshot, rows, side, tick, engine, zone=zone, lookback=6)
        sweep_ts = sweep['sweep_ts'] if sweep else (zone.get('sweep') or {}).get('sweep_ts') or zone.get('known_ts')
        if not sweep_ts:
            return 'WAIT_ARMED_SWEEP_CHOCH'
        atr_local = engine.atr(rows[:-1])
        choch = direction.structural_flip(snapshot, side, sweep_ts, tick, engine, timeframes=('15m', '5m'))['confirmed']
        ema20, _ = direction.trend_references(snapshot)
        last_bar = rows[-1]
        disp = (atr_local > 0 and side * (last_bar.close - last_bar.open) >= .80 * atr_local - EPS
                and (ema20 is None or side * (last_bar.close - ema20) >= -EPS))
        if choch or disp:
            return None
        return 'WAIT_ARMED_SWEEP_CHOCH'
    if name not in {'OPENING_RANGE_BREAKOUT', 'FAILED_OPENING_RANGE_BREAKOUT', 'OR_FAILURE_2_SHORT',
                    'SESSION_MEAN_RECLAIM', 'DIRECTION_FLIP_15M', 'SWEEP_RECLAIM', 'PULLBACK_CONTINUATION',
                    'MOMENTUM_NO_PULLBACK_CONTINUATION', 'ACCELERATION_PULLBACK_REENTRY'}:
        return None
    last = rows[-1]
    atr = engine.atr(rows[:-1])
    level = zone.get('reference', (zone['low']+zone['high'])/2)
    body = side*(last.close-last.open)
    touch = last.low <= level+tick and last.high >= level-tick
    fresh = 0 <= snapshot.now-last.ts-oil_risk.M15 < oil_risk.M15
    retest = fresh and touch and side*(last.close-level) >= tick-EPS and body >= tick-EPS
    if name in ('OPENING_RANGE_BREAKOUT','FAILED_OPENING_RANGE_BREAKOUT','OR_FAILURE_2_SHORT'):
        known = zone.get('impulse_known_ts') or zone['known_ts']
        volume = scanner.event_rvol(snapshot,'15m',known).get('rvol') or 0.
        return None if volume >= 1.5 and retest and last.ts >= known else 'WAIT_OR_VOLUME_RETEST'
    if name == 'SESSION_MEAN_RECLAIM':
        vwap = direction.session_vwap(snapshot)
        slope = vwap.get('slope')
        strength = _adx(rows)
        if slope is None or strength is None:
            return 'WAIT_VWAP_REGIME'
        flat = strength < 22 and abs(slope) <= .05*atr
        if flat:
            return None if _sweep(snapshot,rows,side,tick,engine,zone=zone) else 'WAIT_VWAP_SWEEP'
        return None if side*slope > 0 and retest else 'WAIT_TREND_VWAP_RETEST'
    if name == 'DIRECTION_FLIP_15M':
        sweep = _sweep(snapshot, rows[:-3], side, tick, engine, lookback=8)
        if len(rows) >= 7 and sweep and retest:
            for left, impulse, right in zip(rows[-8:-2], rows[-7:-1], rows[-6:]):
                gap = right.low-left.high if side == 1 else left.low-right.high
                if (sweep['sweep_ts'] <= impulse.ts < right.ts < last.ts
                        and impulse.ts-left.ts == right.ts-impulse.ts == oil_risk.M15
                        and side*(impulse.close-impulse.open) >= 1.2*atr and gap >= tick
                        and (last.low <= right.low+tick and last.high >= left.high-tick if side == 1
                             else last.low <= left.low+tick and last.high >= right.high-tick)):
                    return None
        return 'WAIT_SWEEP_DISPLACEMENT_FVG_RETEST'
    if name == 'SWEEP_RECLAIM':
        sweep = _sweep(snapshot,rows,side,tick,engine,zone=zone)
        if sweep:
            post = [bar for bar in rows if bar.ts+oil_risk.M15 >= sweep['sweep_ts']]
            raid = post[0]
            wick = min(raid.open,raid.close)-raid.low if side == 1 else raid.high-max(raid.open,raid.close)
            if wick >= .45*(raid.high-raid.low) and (len(post) >= 2 and all(
                side*(bar.close-sweep['level']) >= tick-EPS for bar in post[-2:]) or
                direction.structural_flip(snapshot,side,sweep['sweep_ts'],tick,engine,
                                          timeframes=('15m',))['confirmed']):
                return None
        return 'WAIT_SWEEP_HOLD'
    if name in ('MOMENTUM_NO_PULLBACK_CONTINUATION','ACCELERATION_PULLBACK_REENTRY'):
        ema, _ = direction.trend_references(snapshot)
        age = snapshot.now-(zone.get('impulse_known_ts') or zone['known_ts'])
        return None if (ema is not None and 0 <= age < 2*oil_risk.M15
                        and side*(snapshot.price-ema) <= 1.5*atr) else 'WAIT_FRESH_IGNITION'
    approach = rows[-3:-1]
    quiet = len(approach)==2 and (abs(last.close-last.open) < .8*atr
                                and last.volume <= approach[-1].volume <= approach[0].volume)
    rejection = retest and body >= .50*(last.high-last.low)
    return None if quiet or rejection and settle else 'WAIT_PULLBACK_REJECTION'


def level_rejection(snapshot, side, zone, entry, risk, cfg, engine, *, early=False, strict=False):
    rows = direction.closed(snapshot, '15m')
    if len(rows) < 15 or not snapshot.trusted or risk <= 0:
        return None
    bar = rows[-1]
    known = bar.ts+oil_risk.M15
    quote = snapshot.ask if side == 1 else snapshot.bid
    radius = (.25 if early or strict else .20)*engine.atr(rows[:-1])
    extreme = bar.low if side == 1 else bar.high
    if (not 0 <= snapshot.now-snapshot.ticker_ts <= 120_000
            or not 0 <= snapshot.ticker_ts-known < oil_risk.M15 or bar.ts < zone['known_ts']
            or quote <= 0 or abs(quote-entry) > radius+EPS
            or (not strict and abs(extreme-entry) > radius+cfg.tick)
            or side*(bar.close-entry) < cfg.tick-EPS
            or side*(quote-entry) < -cfg.tick/2):
        return None
    wick = min(bar.open,bar.close)-bar.low if side == 1 else bar.high-max(bar.open,bar.close)
    previous = rows[-2]
    engulfing = side*(previous.close-previous.open) < 0 and ict.reversal_engulfing(previous,bar,side,cfg.tick,oil_risk.M15)
    if strict:
        if not (bar.low <= entry+cfg.tick and bar.high >= entry-cfg.tick
                and (wick > 0 and wick >= .35*(bar.high-bar.low)-EPS or engulfing)):
            return None
    elif side*(bar.close-bar.open) < cfg.tick-EPS:
        return None
    return {'pattern':'M15_CONFIRMED_LEVEL_REJECTION', 'known_ts':known, 'limit_entry':entry,
            'initial_risk':risk, 'max_distance':radius, 'reference_close':bar.close,
            'strict_rejection':strict, 'confirmation_timeframe':'15m', 'wick_ratio':wick/max(cfg.tick,bar.high-bar.low),
            'engulfing':engulfing}


def late_retest_evidence(snapshot, side, zone, entry, risk, cfg, engine):
    """Recover a closed trigger for a future limit. Never simulate a late MARKET fill."""
    rows = direction.closed(snapshot, '15m')
    if len(rows) < 15 or not snapshot.trusted or risk <= 0:
        return None
    last = rows[-1]
    known = last.ts+oil_risk.M15
    atr = engine.atr(rows[:-1])
    if not 600_000 < snapshot.now-known < oil_risk.M15 or known < zone['known_ts']:
        return None
    if side*(last.close-last.open) < .60*atr or side*(last.close-entry) <= 0:
        return None
    planned = {'pattern':'LATE_M15_RETEST', 'known_ts':known, 'reference_close':last.close,
               'limit_entry':entry, 'initial_risk':risk}
    live_quote = snapshot.ask if side == 1 else snapshot.bid
    if side*(live_quote-last.close) <= max(cfg.tick,.20*atr):
        return None
    return planned
