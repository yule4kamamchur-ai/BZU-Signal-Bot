"""Closed-candle reversal and continuation zones for the current market wave."""
from dataclasses import replace

import ict_methodology as ict
import oil_risk

M15 = 900_000
M5 = 300_000
EARLY_M15_RECLAIM = 'M15_BOS_EMA20_ATR'
STRUCTURE_LOOKBACK = 96
BREAKDOWN = "BREAKDOWN_RECLAIM"
BREAKDOWN_MAX_AGE = 2 * M15
REVERSAL_ORIGIN_BARS = 192
TREND_ZONE_AGE = 6 * 60 * 60_000
TREND_ZONES = frozenset({'BREAKER_BLOCK', 'FVG_PULLBACK', 'FIB_OTE_ZONE', 'SWEEP_CONTINUATION', 'BOS_CONTINUATION'})
REVERSALS = frozenset({'SWEEP_REVERSAL', 'M15_CHOCH', 'BREAKER_CHOCH', 'MULTIBAR_SWEEP', 'RECLAIM_BREAKER', 'SR_FLIP_RECLAIM', BREAKDOWN})
CONTINUATIONS = frozenset({'BULLISH_BREAKER', 'BEARISH_BREAKER', 'CONTINUATION_PULLBACK',
                           'M5_IMPULSE_OB', 'FLAG_RETEST'})


def structure_breaks(rows, step, tick, engine, lookback, hourly_points=()):
    """A displacement must cross a pivot known before the candle opened."""
    points = engine.pivots(rows[-max(80, lookback + 12):], strength=1)
    for i in range(max(7, len(rows) - lookback), len(rows)):
        bar = rows[i]
        atr = engine.atr(rows[:i])
        for side in (1, -1):
            if atr <= 0 or side * (bar.close - bar.open) <= .6 * atr:
                continue
            pivot = ict.body_break(rows, i, points, side, tick, step)
            if pivot is None and hourly_points:
                pivot = ict.body_break(rows, i, hourly_points, side, tick, step)
            if pivot:
                yield i, side, pivot, atr


def fast_structure(rows, tick, engine):
    recent = list(structure_breaks(rows, M15, tick, engine, lookback=4))
    if not recent:
        return {}
    i, side, pivot, atr = recent[-1]
    level, bar = pivot['level'], rows[i]
    if any(side * (c.close - level) <= 0 for c in rows[i + 1:]):
        return {}
    return {'side': side, 'known_ts': bar.ts + M15, 'level': level, 'close': bar.close,
            'proof': 'FAST_M15_STRUCTURE_SHIFT', 'pivot_ts': pivot['ts'],
            'reference_known_ts': pivot['known_ts'] + M15,
            'body_atr': abs(bar.close - bar.open) / atr}


def early_structure_reclaim(snapshot, tick, engine):
    """A closed M15 swing break and EMA20 reclaim can lead an hourly reversal."""
    from strategy_organics import ema20

    rows = oil_risk.closed(snapshot.candles.get('15m', []), M15, snapshot.now)[-192:]
    if len(rows) < 32 or snapshot.now - rows[-1].ts - M15 >= 2*M15:
        return {}
    if any(b.ts-a.ts != M15 for a,b in zip(rows[-32:-1],rows[-31:])):
        return {}
    points = engine.pivots(rows[-80:],strength=1)
    recent = list(structure_breaks(rows,M15,tick,engine,lookback=8))
    for i,side,pivot,atr in reversed(recent):
        bar = rows[i]
        level = pivot['level']
        swing_kind = 'LOW' if side==1 else 'HIGH'
        prior = rows[i-16:i]
        origin_bar = min(prior,key=lambda c:c.low) if side==1 else max(prior,key=lambda c:c.high)
        origin = origin_bar.low if side==1 else origin_bar.high
        protected = next((p['level'] for p in reversed(points) if p['kind']==swing_kind
                          and origin_bar.ts<p['ts'] and p['known_ts']+M15<=bar.ts),None)
        if protected is None or side*(protected-origin)<tick-1e-8:
            continue
        if any(side*(c.close-protected) < -tick/2 for c in rows[i:]):
            continue
        if not any(side*(rows[k].close-ema20(rows[:k+1])) <= tick/2 for k in range(i-3,i)):
            continue
        for j in range(i,min(i+3,len(rows))):
            candle = rows[j]
            if side*(candle.close-level) < tick/2:
                break
            average = ema20(rows[:j+1])
            distance = side*(candle.close-origin)
            if side*(candle.close-average) >= tick/2 and distance >= 1.10*atr-1e-8:
                return {'side':side,'known_ts':candle.ts+M15,'break_ts':bar.ts+M15,
                        'reference_known_ts':pivot['known_ts']+M15,'level':level,
                        'protected_level':protected,'origin':origin,'close':candle.close,
                        'ema20_15m':average,'wave_atr':distance/atr,'proof':EARLY_M15_RECLAIM}
    return {}


def structure_sync(snapshot, tick, engine):
    """A closed M15 BOS and a 0.75 ADR leg can supersede the hourly bias."""
    import directional_policy as direction

    cache = vars(snapshot).setdefault('_m15_structure_sync', {})
    key = (snapshot.now, tick)
    if key not in cache:
        rows = direction.closed(snapshot, '15m')[-(REVERSAL_ORIGIN_BARS + STRUCTURE_LOOKBACK):]
        cache[key] = _confirmed_structure_sync(rows, snapshot, tick, engine)
    return cache[key]


def _confirmed_structure_sync(rows, snapshot, tick, engine):
    if not rows or snapshot.now - rows[-1].ts - M15 >= 2 * M15:
        return {}
    from strategy_organics import ema20

    hourly = oil_risk.closed(snapshot.candles.get('1H', []), oil_risk.H1, snapshot.now)
    current_ema = ema20(hourly)
    if current_ema is None:
        return {}
    ranges = oil_risk.daily_ranges(rows, hourly,
        oil_risk.closed(snapshot.candles.get('4H', []), oil_risk.H4, snapshot.now))
    hourly_points = [{**p, 'known_ts': p['known_ts'] + oil_risk.H1 - M15}
                     for p in engine.pivots(hourly[-96:], strength=1)]
    points = engine.pivots(rows, strength=1)
    proofs = []
    for i, side, pivot, _ in structure_breaks(rows, M15, tick, engine, STRUCTURE_LOOKBACK, hourly_points):
        if side * (rows[-1].close - current_ema) <= 0:
            continue
        bar = rows[i]
        prior = [c for c in rows[:i] if bar.ts - REVERSAL_ORIGIN_BARS * M15 <= c.ts]
        daily = [high - low for start, (high, low, _) in ranges.items()
                 if start + oil_risk.DAY <= bar.ts + M15][-14:]
        if not daily or not prior:
            continue
        adr = sum(daily) / len(daily)
        origin_bar = min(prior, key=lambda c: c.low) if side == 1 else max(prior, key=lambda c: c.high)
        origin = origin_bar.low if side == 1 else origin_bar.high
        protected = next((p['level'] for p in reversed(points) if p['kind'] == ('LOW' if side == 1 else 'HIGH')
                          and origin_bar.ts <= p['ts'] and p['known_ts'] + M15 <= bar.ts), origin)
        leg = rows[i:]
        if (any(b.ts - a.ts != M15 for a, b in zip(leg, leg[1:]))
                or any(side * (c.close - protected) < -tick / 2 for c in leg)):
            continue
        for c in leg:
            edge = c.high if side == 1 else c.low
            distance = side * (edge - origin)
            if side * (c.close - pivot['level']) <= 0 or adr <= 0 or distance < .75 * adr - 1e-8:
                continue
            ema = ema20([h for h in hourly if h.ts + oil_risk.H1 <= c.ts + M15])
            if ema is not None and side * (c.close - ema) > 0:
                proofs.append({'side': side, 'known_ts': c.ts + M15, 'break_ts': bar.ts + M15,
                               'level': pivot['level'], 'protected_level': protected,
                               'origin': origin, 'origin_ts': origin_bar.ts, 'close': c.close,
                               'adr': adr, 'wave_adr': distance / adr, 'ema20_1h': ema, 'proof': 'M15_BOS_75_ADR'})
                break
    return max(proofs, key=lambda p: (p['known_ts'], -p['break_ts']), default={})


def displacement_override(snapshot, tick, engine):
    import directional_policy as direction

    rows = direction.closed(snapshot, '15m')[-192:]
    points = engine.pivots(rows, strength=1)
    adr = oil_risk.snapshot_profile(snapshot)['adr_d1']
    for i in range(len(rows)-2, max(13, len(rows)-26), -1):
        bar = rows[i]
        atr = engine.atr(rows[max(0,i-15):i])
        if atr <= 0:
            continue
        for side in (1,-1):
            if side*(bar.close-bar.open) < 1.25*atr:
                continue
            pivot = ict.body_break(rows,i,points,side,tick,M15)
            gap = ict.displacement_gap(rows,i,side,tick)
            if not pivot or not gap:
                continue
            sweep = ict.prior_local_sweep(rows,i,points,side,tick)
            if not sweep and adr:
                day = bar.ts//oil_risk.DAY*oil_risk.DAY
                today = [c for c in rows[:i] if c.ts >= day]
                if today and today[0].ts == day:
                    boundary = today[0].open - side*adr
                    touched = next((c for c in reversed(today[-4:])
                        if side*((c.low if side==1 else c.high)-boundary)<=tick
                        and side*(c.close-boundary)>0),None)
                    if touched:
                        sweep = {'level':boundary, 'sweep_ts':touched.ts+M15,
                                 'extreme':touched.low if side==1 else touched.high}
            if not sweep:
                continue
            if any(side*(c.close-sweep['extreme']) <= 0 for c in rows[i:]):
                continue
            return {'side':side, 'known_ts':rows[i+1].ts+M15, 'break_ts':bar.ts+M15,
                    'level':pivot['level'], 'protected_level':sweep['extreme'],
                    'sweep_ts':sweep['sweep_ts'], 'fvg':gap, 'body_atr':abs(bar.close-bar.open)/atr,
                    'proof':'DISPLACEMENT_MSS_FVG'}
    return {}


def breakdown_zones(snapshot, tick, engine):
    """A bearish flip from a strong close, independent of the slow bias."""
    import directional_policy as direction

    cache = vars(snapshot).setdefault('_breakdown_zones', {})
    key = (snapshot.now,tick)
    if key in cache:
        return cache[key]
    rows = direction.closed(snapshot,'15m')[-(STRUCTURE_LOOKBACK+3):]
    points = engine.pivots(rows,strength=1)
    zones = []
    for index in range(max(19,len(rows)-2),len(rows)):
        bar = rows[index]
        known = bar.ts+M15
        if not 0 <= snapshot.now-known < BREAKDOWN_MAX_AGE:
            continue
        atr = engine.atr(rows[max(0,index-15):index])
        evidence = ict.bearish_breakout(rows,index,points,tick,atr)
        if evidence is None:
            continue
        level = evidence.pop('level')
        high = level+max(tick,min(.10,.25*atr))
        if any(c.high >= level+tick-1e-8 for c in rows[index+1:]):
            # The original first retest is no longer available for a new plan.
            continue
        zones.append(_zone(BREAKDOWN,-1,level,high,bar,M15,level,
            structural_reclaim=True,break_ts=known,impulse_ts=bar.ts,
            breakdown_atr=atr,**evidence))
    cache[key] = zones
    return zones


def breakdown_intact(zone, snapshot, tick):
    proof = zone.get('wave_proof') or {}
    known = proof.get('break_ts',0)
    if (zone.get('kind')!=BREAKDOWN or zone.get('side')!=-1 or
            not 0 < proof.get('reference_known_ts',0) <= known-M15 or
            not 0 <= snapshot.now-known < BREAKDOWN_MAX_AGE):
        return False
    from directional_policy import closed

    return (snapshot.price <= zone['high']+tick/2 and
            not any(c.close>zone['high'] for c in closed(snapshot,'15m') if c.ts>=known))


def _reclaim_breakers(rows, tick, engine, *, proactive=False):
    """Revisit a causal pivot or shelf from the preceding 24 hours."""
    points = engine.pivots(rows[-2 * STRUCTURE_LOOKBACK:], strength=1)
    for index in range(max(8, len(rows) - STRUCTURE_LOOKBACK), len(rows)):
        bar = rows[index]
        prior = rows[max(0, index - STRUCTURE_LOOKBACK):index]
        atr = engine.atr(prior)
        if atr <= 0:
            continue
        for side in (1, -1):
            for level, reference_ts in ict.reclaim_references(prior, points, bar.ts, side, tick, atr):
                if not ict.reclaim_break(rows, index, side, level, tick, atr):
                    continue
                zone = _confirmed_retest_zone(rows, index, side, level, reference_ts, tick, atr)
                if proactive:
                    if zone:
                        zone['kind'] = 'SR_FLIP_RECLAIM'
                    else:
                        # An untouched breakout may reserve its FIRST retest. Once
                        # the level traded, wait for closed proof; never rearm blind.
                        touched = any(side*((c.low if side==1 else c.high)-level) <= tick/2
                                      for c in rows[index+1:])
                        if not touched:
                            low, high = sorted((level, level-side*max(2*tick,.30*atr)))
                            zone = _zone('SR_FLIP_RECLAIM',side,low,high,bar,M15,level,
                                         structural_reclaim=True, reference_known_ts=reference_ts,
                                         break_ts=bar.ts+M15, impulse_ts=bar.ts)
                if zone:
                    zone['wave_proof']['impulse_ts'] = bar.ts
                    yield zone
                    break


def _confirmed_retest_zone(rows, index, side, level, reference_ts, tick, atr, **proof):
    width = min(.25, max(2 * tick, .30 * atr))
    low, high = sorted((level, level - side * width))
    distal = low if side == 1 else high
    breakout = rows[index]
    retest = None
    for previous, bar in zip(rows[index:], rows[index + 1:]):
        if bar.ts - previous.ts != M15 or side * (bar.close - distal) < -tick / 2:
            return None
        if retest is not None or bar.ts - breakout.ts > STRUCTURE_LOOKBACK * M15:
            continue
        extreme = bar.low if side == 1 else bar.high
        if side * (extreme - level) <= tick / 2:
            if side*(previous.close-level) < tick/2 or side*(bar.close-level) < tick/2:
                return None
            retest = bar
    if retest is None:
        return None
    return _zone('RECLAIM_BREAKER', side, low, high, retest, M15, level,
                 structural_reclaim=True, reference_known_ts=reference_ts,
                 break_ts=breakout.ts + M15, retest_ts=retest.ts + M15, **proof)


def reclaim_is_intact(side, zone, snapshot, tick):
    import directional_policy as direction

    proof = zone.get('wave_proof') or {}
    known = zone.get('known_ts', 0)
    reference_ts = proof.get('reference_known_ts', 0)
    broken = proof.get('break_ts', 0)
    before_retest = zone.get('kind')=='SR_FLIP_RECLAIM' and broken == known and not proof.get('retest_ts')
    if (zone.get('side') != side or not proof.get('structural_reclaim')
            or not (before_retest or (proof.get('retest_ts') == known and 0 < known-broken <= STRUCTURE_LOOKBACK*M15))
            or not 0 < broken - reference_ts <= STRUCTURE_LOOKBACK * M15
            or not 0 <= snapshot.now - known < TREND_ZONE_AGE):
        return False
    distal = zone['low'] if side == 1 else zone['high']
    return (side * (snapshot.price - distal) >= -tick / 2 and
            not any(side * (c.close - distal) < -tick / 2
                    for c in direction.closed(snapshot, '15m') if c.ts >= known))


def _zone(kind, side, low, high, bar, step, reference, **evidence):
    return {'kind': kind, 'side': side, 'low': low, 'high': high, 'timeframe': '15m',
            'known_ts': bar.ts + step, 'reference': reference,
            'wave_proof': {'timeframe': '5m' if step == M5 else '15m', **evidence}}


def _reversals(snapshot, rows, step, tick, engine, atr15, require_macro):
    if len(rows) < 2:
        return
    pivots = engine.pivots(rows[-80:], strength=1)
    delay = 2 if step == M5 else ict.SWEEP_TO_BOS_M15_BARS
    for i in range(max(1, len(rows) - (4 if step == M5 else delay + 2)), len(rows)):
        bar = rows[i]
        if rows[i - 1].ts + step != bar.ts:
            continue
        past = replace(snapshot, now=bar.ts)
        pools = ict.session_liquidity(past)['pools']
        # During Asia the completed session pool is not yet published.
        # Use the observed high/low of the current Kyiv trading day too.
        import directional_policy as direction
        pools = [*pools, *direction.day_extrema(past)]
        # A just-confirmed swing close to a session extreme can be the first
        # liquidity level tested when the high/low is already established.
        for pool in tuple(pools):
            points = [p for p in pivots if pool['name'].endswith(p['kind'])
                      and p['known_ts'] + step <= bar.ts
                      and abs(p['level'] - pool['level']) <= max(.08, .3 * atr15)]
            if points:
                point = points[-1]
                pools.append({'name': 'SESSION_SWING_' + point['kind'], 'level': point['level'],
                              'known_ts': point['known_ts'] + step})
        for side in (1, -1):
            suffix = 'LOW' if side == 1 else 'HIGH'
            extreme = bar.low if side == 1 else bar.high
            wick_edge = min(bar.open, bar.close) if side == 1 else max(bar.open, bar.close)
            macro = oil_risk.macro_extrema(snapshot, bar.ts, engine, side) if require_macro else []
            eligible = [p for p in (*macro, *pools) if p['name'].endswith(suffix)
                        and p['known_ts'] <= bar.ts
                        and side * (p['level'] - extreme) >= tick - 1e-8
                        and abs(p['level'] - extreme) <= max(.20, .8 * atr15)]
            for pool in sorted(eligible, key=lambda p: (not p.get('timeframe'), abs(p['level'] - extreme))):
                level = pool['level']
                swept = (side * (wick_edge - level) >= -tick / 2
                         and side * (bar.close - level) >= tick - 1e-8)
                confirmation = bar
                shift = None
                if swept:
                    kind = 'SWEEP_REVERSAL'
                else:
                    for j in range(i, min(i + delay + 1, len(rows))):
                        test = rows[j]
                        test_extreme = test.low if side == 1 else test.high
                        if test.ts != bar.ts + (j - i) * step or side * (test_extreme - extreme) < -tick:
                            break
                        pivot = ict.body_break(rows, j, pivots, side, tick, step)
                        if pivot and side * (test.close - level) >= tick - 1e-8:
                            shift, confirmation = pivot, test
                            break
                    if not shift:
                        continue
                    kind = 'M15_CHOCH' if step == M15 else 'BREAKER_CHOCH'
                low, high = sorted((level, extreme))
                sweep = {'pool': pool['name'], 'level': level, 'extreme': extreme, 'side': side,
                         'known_ts': confirmation.ts + step, 'reference_known_ts': pool['known_ts'],
                         'pattern': 'WICK_RECLAIM' if swept else 'EXTREME_CHOCH',
                         'macro_timeframe': pool.get('timeframe'), 'raid_ts': bar.ts,
                         'reclaim_close': confirmation.close,
                         'wick_ratio': ict.wick_reclaim(bar, side, level, tick)}
                zone = _zone(kind, side, low, high, confirmation, step, level, pool=pool,
                             raid_ts=bar.ts, shift_level=shift['level'] if shift else None)
                yield {**zone, 'sweep': sweep}
                break


def _breakers(rows, cfg, engine):
    tick = cfg.tick
    swings = {(i, side): pivot for i, side, pivot, _
              in structure_breaks(rows, M15, tick, engine, lookback=4)}
    for i in range(max(7, len(rows) - 4), len(rows)):
        previous, bar = rows[i - 1:i + 1]
        if bar.ts != previous.ts + M15:
            continue
        atr = engine.atr(rows[:i])
        side = 1 if bar.close > bar.open else -1
        if atr <= 0 or abs(bar.close - bar.open) <= .6 * atr:
            continue
        kind = 'BULLISH_BREAKER' if side == 1 else 'BEARISH_BREAKER'
        pivot = swings.get((i, side))
        if pivot:
            level = pivot['level']
            low, high = sorted((level, level - side * max(2 * tick, .15 * atr)))
            yield _zone(kind, side, low, high, bar, M15, level,
                        pivot_ts=pivot['ts'], pivot_known_ts=pivot['known_ts'] + M15)
        # Read the opposing OB before the breakout consumes it. The OB's own
        # distal boundary is a separate reference from the closest swing.
        for ob in cfg.core.detect_order_blocks(rows[:i], atr, limit=32):
            known = ob.created_ts + 2 * M15
            level = ob.low if side == -1 else ob.high
            if (cfg.core.side_sign(ob.side) == -side and not ob.mitigated and known <= bar.ts
                    and side * (previous.close - level) <= tick / 2
                    and side * (bar.open - level) <= tick / 2
                    and side * (bar.close - level) > tick):
                yield _zone(kind, side, ob.low, ob.high, bar, M15, level,
                            ob_ts=ob.created_ts, ob_known_ts=known)


def _m5_continuations(rows, side, a15, tick, engine):
    for i in range(max(6, len(rows) - 4), len(rows)):
        prior, bar = rows[i - 6:i], rows[i]
        if any(a.ts + M5 != b.ts for a, b in zip(prior, [*prior[1:], bar])):
            continue
        a5 = engine.atr(rows[:i])
        if a5 <= 0 or side * (bar.close - bar.open) <= .6 * a5:
            continue
        extreme = max(c.high for c in prior) if side == 1 else min(c.low for c in prior)
        end = bar.high if side == 1 else bar.low
        # A flag requires a compressed range at the end of an earlier impulse.
        flag = prior[-3:]
        flag_low, flag_high = min(c.low for c in flag), max(c.high for c in flag)
        pole = prior[-4]
        edge = flag_high if side == 1 else flag_low
        if (side * (bar.close - edge) > tick and side * (bar.open - edge) <= tick / 2
                and flag_high - flag_low <= min(.85 * a15, 1.8 * a5)
                and side * (pole.close - pole.open) >= a5
                and abs(pole.close - edge) <= max(.10, .4 * a15)):
            low, high = sorted((edge, edge - side * max(tick, .15 * a5)))
            yield _zone('FLAG_RETEST', side, low, high, bar, M5, edge,
                        flag_start_ts=flag[0].ts, pole_ts=pole.ts)
        pivot = ict.body_break(rows, i, engine.pivots(rows[-80:], strength=1), side, tick, M5)
        if side * (bar.close - extreme) <= tick:
            if not pivot:
                continue
            extreme = pivot['level']
        opposite = [c for c in prior if side * (c.close - c.open) < 0]
        anchor = opposite[-1] if opposite else prior[0]
        leg_start = anchor.low if side == 1 else anchor.high
        leg = side * (end - leg_start)
        if leg < max(.20, .65 * a15):
            continue
        low, high = sorted(end - side * leg * fraction for fraction in (.382, .50))
        yield _zone('CONTINUATION_PULLBACK', side, low, high, bar, M5, extreme,
                    leg_start=leg_start, leg_end=end, leg_start_ts=anchor.ts)
        if opposite:
            low, high = sorted((anchor.open, leg_start))
            if high - low >= tick - 1e-8:
                yield _zone('M5_IMPULSE_OB', side, low, high, bar, M5, extreme,
                            ob_ts=anchor.ts, leg_end=end)


def trend_zone(zone, regime, origin=None):
    kind = zone.get('kind')
    witnessed = (kind in TREND_ZONES | {'FLAG_RETEST', 'BEARISH_BREAKER', 'BULLISH_BREAKER'}
                 and bool(zone.get('wave_proof')))
    native = (kind in ('DEMAND_OB', 'SUPPLY_OB', 'FVG_BULL', 'FVG_BEAR', 'IMPULSE_OTE_15M',
                       'DISPLACEMENT_CATCHER_15M', 'BREAKER_FLIP_15M', 'CONTINUATION_PULLBACK')
              and bool((origin or {}).get('break_known_ts')))
    return (witnessed or native) and regime.get('trend_anchor_side') == zone.get('side')


def _trend_zone(kind, side, bounds, bar, step, reference, **proof):
    timeframe = {M5: '5m', M15: '15m', oil_risk.H1: '1H'}[step]
    return {'kind': kind, 'side': side, 'timeframe': '15m' if step == M5 else timeframe,
            'low': min(bounds), 'high': max(bounds), 'reference': reference,
            'known_ts': bar.ts + step, 'wave_proof': {'timeframe': timeframe, **proof}}


def _trend_breakers(rows, side, tick, engine):
    from strategy_direct_entries import shallow_bos_retest

    pivots = engine.pivots(rows[-80:], strength=1)
    for i in range(max(8, len(rows) - 26), len(rows)):
        bar = rows[i]
        atr = engine.atr(rows[:i])
        if atr <= 0 or rows[i - 1].ts + M15 != bar.ts:
            continue
        # Several moderate candles can deliver the displacement together.
        leg = rows[i - 3:i + 1]
        if (any(a.ts + M15 != b.ts for a, b in zip(leg, leg[1:]))
                or side * (bar.close - bar.open) <= tick
                or (side * (bar.close - bar.open) < .55 * atr
                    and (side * (bar.close - leg[0].open) < .8 * atr
                         or sum(side * (c.close - c.open) > tick for c in leg) < 2))):
            continue
        point = ict.body_break(rows, i, pivots, side, tick, M15)
        if point:
            level = point['level']
            evidence = ict.breaker_evidence(rows, i, pivots, side, tick)
            if evidence:
                yield _trend_zone('BREAKER_BLOCK', side, (level, level-side*max(tick,.12*atr)),
                                  rows[i+1], M15, level, structure='PIVOT',
                                  pivot_ts=point['ts'], reference_known_ts=point['known_ts']+M15,
                                  **evidence)
            retest = shallow_bos_retest(bar, side, atr, tick)
            if retest:
                low, high, entry = retest
                yield _trend_zone('BOS_CONTINUATION', side, (low, high), bar, M15, entry,
                                  structure='PIVOT', break_level=level, pivot_ts=point['ts'],
                                  reference_known_ts=point['known_ts'] + M15, retracement=.30)

        # Two near-equal tests can form a shelf even if neither is a strict pivot.
        for j in range(i - 2, max(i - 7, 0), -1):
            prior = rows[j:i]
            if prior[0].ts + len(prior) * M15 != bar.ts:
                continue
            tests = [c.high if side == 1 else c.low for c in prior]
            edge = tests[-1]
            near = [level for level in tests if abs(level - edge) <= max(2 * tick, .18 * atr)]
            if len(near) < 2 or max(near) - min(near) < tick - 1e-8:
                continue
            threshold = max(near) if side == 1 else min(near)
            if (side * (bar.close - threshold) <= tick or side * (bar.open - threshold) > tick / 2
                    or any(side * (c.close - threshold) >= 0 for c in prior)):
                continue
            entry = min(near) if side == 1 else max(near)
            evidence = ict.breaker_evidence(rows, i, pivots, side, tick)
            if evidence:
                yield _trend_zone('BREAKER_BLOCK', side, near, rows[i+1], M15, entry, structure='SHELF',
                                  reference_known_ts=prior[-1].ts+M15, shelf_start_ts=prior[0].ts,
                                  **evidence)
            break


def _trend_fvgs(rows, side, step, tick, engine):
    for i in range(max(2, len(rows) - TREND_ZONE_AGE // step), len(rows)):
        first, middle, last = rows[i - 2:i + 1]
        if first.ts + step != middle.ts or middle.ts + step != last.ts:
            continue
        low, high = (first.high, last.low) if side == 1 else (last.high, first.low)
        atr = engine.atr(rows[:i - 1])
        if high - low < max(tick, .12 * atr) - 1e-8 or side * (middle.close - middle.open) < max(tick, .45 * atr):
            continue
        if any((c.low <= low if side == 1 else c.high >= high) for c in rows[i + 1:]):
            continue
        yield _trend_zone('FVG_PULLBACK', side, (low, high), last, step,
                          (low + high) / 2, impulse_ts=middle.ts)


def _trend_ote(rows, side, tick, engine):
    pivots = engine.pivots(rows[-48:], strength=1)
    for i in range(max(10, len(rows) - 9), len(rows) - 1):
        end, confirm = rows[i:i + 2]
        if end.ts + M15 != confirm.ts:
            continue
        tip = end.high if side == 1 else end.low
        if side * (tip - (confirm.high if side == 1 else confirm.low)) <= tick:
            continue
        origins = [p for p in pivots if p['kind'] == ('LOW' if side == 1 else 'HIGH')
                   and end.ts - 12 * M15 <= p['ts'] and p['known_ts'] + M15 <= end.ts]
        atr = engine.atr(rows[:i])
        if not origins or atr <= 0:
            continue
        origin = origins[-1]
        size = side * (tip - origin['level'])
        leg = [c for c in rows if origin['ts'] <= c.ts <= end.ts]
        if (size < max(.65, 1.5 * atr) or len(leg) < 3 or
                side * (end.close - leg[0].close) < .8 * atr or
                any(a.ts + M15 != b.ts for a, b in zip(leg, leg[1:])) or
                any(side * ((c.high if side == 1 else c.low) - tip) > tick for c in rows[i + 1:])):
            continue
        bounds = tuple(tip - side * size * ratio for ratio in (.50, .618))
        yield _trend_zone('FIB_OTE_ZONE', side, bounds, confirm, M15, bounds[0],
                          leg_start_ts=origin['ts'], leg_end_ts=end.ts)
        return


def _trend_sweep(rows, side, step, tick, engine):
    points = engine.pivots(rows[-32:], strength=1)
    for bar in rows[-2:]:
        pivot = next((p for p in reversed(points) if p['kind'] == ('LOW' if side == 1 else 'HIGH')
                      and p['known_ts'] + step <= bar.ts), None)
        if not pivot:
            continue
        level = pivot['level']
        extreme = bar.low if side == 1 else bar.high
        if (side * (level - extreme) >= tick - 1e-8 and side * (bar.close - level) >= tick - 1e-8
                and side * (bar.close - bar.open) >= 0):
            yield _trend_zone('SWEEP_CONTINUATION', side, (level, extreme), bar, step, level,
                              pivot_ts=pivot['ts'], reference_known_ts=pivot['known_ts'] + step)


def exhausted_adr(rows, sweep, adr):
    """Measure the adverse leg in the preceding 24 hours of closed M15."""
    if not adr or not sweep.get('raid_ts'):
        return False
    side, raid_ts = sweep['side'], sweep['raid_ts']
    prior = [bar for bar in rows if raid_ts-96*M15 <= bar.ts < raid_ts]
    if not prior:
        return False
    origin = max(bar.high for bar in prior) if side == 1 else min(bar.low for bar in prior)
    return side*(origin-sweep['extreme']) >= .80*adr-1e-8


def _multibar_zones(rows, tick, engine, adr):
    import directional_policy as direction

    points = engine.pivots(rows[-2 * STRUCTURE_LOOKBACK:], strength=1)
    for side in (1,-1):
        for sweep in direction.multi_bar_reversals(rows,side,tick,lookback=STRUCTURE_LOOKBACK):
            extreme = sweep['extreme']
            raid_index = next(i for i,bar in enumerate(rows) if bar.ts == sweep['raid_ts'])
            exhausted = exhausted_adr(rows,sweep,adr)
            raid = rows[raid_index]
            confirm_index = raid_index + (sweep['known_ts']-raid.ts)//M15-1
            confirm = rows[confirm_index]
            body_edge = min(raid.open,raid.close) if side == 1 else max(raid.open,raid.close)
            width = min(.35,max(2*tick,side*(body_edge-extreme),side*(sweep['level']-extreme),
                                .5*side*(confirm.close-extreme)))
            bounds = (extreme,round(extreme+side*width,8))
            yield {**_zone('MULTIBAR_SWEEP',side,*sorted(bounds),confirm,M15,sweep['level'],
                           exhausted_adr=exhausted),'sweep':sweep}
            kind = 'HIGH' if side == 1 else 'LOW'
            refs = [p for p in points if p['kind']==kind
                    and raid.ts-STRUCTURE_LOOKBACK*M15 <= p['known_ts']+M15 <= raid.ts
                    and side*(p['level']-extreme) >= .35]
            if not refs:
                continue
            reference = refs[-1]
            level = reference['level']
            start = confirm_index
            for index in range(start,min(len(rows),start+STRUCTURE_LOOKBACK)):
                atr = engine.atr(rows[max(0,index-20):index])
                if not ict.reclaim_break(rows,index,side,level,tick,atr):
                    continue
                zone = _confirmed_retest_zone(rows, index, side, level, reference['known_ts'] + M15,
                    tick, atr, pivot_ts=reference['ts'], exhausted_adr=exhausted)
                if zone:
                    yield {**zone, 'sweep': sweep}
                    break


def zones(snapshot, cfg, engine, regime):
    """Use M5 only inside an established M15/HTF direction or a witnessed raid."""
    import directional_policy as direction
    import setup_scanner as scanner

    cache = vars(snapshot).setdefault('_wave_zones', {})
    key = (snapshot.now, cfg.tick, direction.regime_side(regime), regime.get('trend_anchor_side'),
           regime.get('side_1h'), regime.get('side_4h'), regime.get('structure_sync', {}).get('side'),
           oil_risk.enabled(cfg))
    if key in cache:
        return cache[key]
    m15 = direction.closed(snapshot, '15m')
    m5 = scanner.closed_five_minutes(snapshot)
    atr = engine.atr(m15)
    if atr <= 0:
        return []
    tick = cfg.tick
    results = [*_reversals(snapshot, m15, M15, tick, engine, atr, oil_risk.enabled(cfg)),
               *_reversals(snapshot, m5, M5, tick, engine, atr, oil_risk.enabled(cfg)), *_breakers(m15, cfg, engine)]
    side = direction.regime_side(regime)
    if side and (regime.get('active_structure') or direction.momentum_aligned(snapshot, side)):
        results.extend(_m5_continuations(m5, side, atr, tick, engine))
    trend = regime.get('trend_anchor_side')
    if trend:
        from bzu_policy import breaker_structure_error

        results.extend(z for z in _trend_breakers(m15, trend, tick, engine)
                       if not breaker_structure_error({'side': trend, 'entry_zone': z}, snapshot, cfg, engine))
        results.extend(_trend_fvgs(m15, trend, M15, tick, engine))
        results.extend(_trend_fvgs(direction.closed(snapshot, '1H'), trend, oil_risk.H1, tick, engine))
        results.extend(_trend_ote(m15, trend, tick, engine))
        results.extend(_trend_sweep(m15, trend, M15, tick, engine))
        results.extend(_trend_sweep(m5, trend, M5, tick, engine))
    if oil_risk.enabled(cfg):
        results.extend(_multibar_zones(m15,tick,engine,oil_risk.snapshot_profile(snapshot)['adr_d1']))
        results.extend(_reclaim_breakers(m15, tick, engine, proactive=True))
        results.extend(breakdown_zones(snapshot,tick,engine))
        reclaims = {}
        other = []
        for zone in results:
            if zone['kind'] not in ('RECLAIM_BREAKER','SR_FLIP_RECLAIM'):
                other.append(zone)
                continue
            if regime.get('structure_sync', {}).get('side') == -zone['side']:
                continue
            reclaim_key = (zone['side'], round(zone['reference'] / tick), zone['known_ts'])
            current = reclaims.get(reclaim_key)
            if current is None or (zone.get('sweep') or {}).get('known_ts', 0) > (
                    current.get('sweep') or {}).get('known_ts', 0):
                reclaims[reclaim_key] = zone
        results = [*other, *reclaims.values()]
    cache[key] = [z for z in results if z['high'] - z['low'] >= tick - 1e-8
                  and 0 <= snapshot.now - z['known_ts'] < (TREND_ZONE_AGE if z['kind'] in TREND_ZONES or z['kind'] in ('RECLAIM_BREAKER','SR_FLIP_RECLAIM') else 30 * 60_000)]
    return cache[key]


def witnessed(zone, snapshot, cfg, engine, regime):
    keys = ('kind', 'side', 'low', 'high', 'known_ts', 'reference', 'wave_proof', 'sweep')
    return next((z for z in zones(snapshot, cfg, engine, regime)
                 if all(z.get(k) == zone.get(k) for k in keys)), None)


def reversal_override(side, zone, snapshot, tick, *, require_macro=True):
    """Consume the sweep that the planner already reconstructed from candles."""
    zone = zone or {}
    sweep = zone.get('sweep') or {}
    if zone.get('kind') == BREAKDOWN:
        return side == -1 and breakdown_intact(zone,snapshot,tick)
    if zone.get('kind') in ('MULTIBAR_SWEEP','RECLAIM_BREAKER','SR_FLIP_RECLAIM'):
        import directional_policy as direction

        return direction.reversal_sweep_override(side, zone, snapshot, tick)
    return (zone.get('kind') in REVERSALS and sweep.get('side') == side
            and (not require_macro or (sweep.get('macro_timeframe') in ('1H', '4H', 'D1')
                 and sweep.get('reference_known_ts', 0) <= sweep.get('raid_ts', -1)
                 and side * (sweep.get('reclaim_close', sweep['level']) - sweep['level']) >= tick - 1e-8))
            and sweep.get('pattern') in ('WICK_RECLAIM', 'EXTREME_CHOCH')
            and 0 < sweep.get('reference_known_ts', 0) <= zone.get('known_ts', 0)
            and 0 <= snapshot.now - zone.get('known_ts', 0) < 30 * 60_000
            and side * (snapshot.price - sweep['extreme']) > tick / 2)


def focus_context(snapshot, regime, dealing_range, tick):
    """A watch bias is separate from the execution regime and the range position."""
    from directional_policy import regime_side

    rows = oil_risk.closed(snapshot.candles.get('15m', []), M15, snapshot.now)[-192:]
    ema20 = ema50 = None
    ema_side = 0
    if len(rows) >= 50:
        ema20 = ema50 = rows[0].close
        for i, candle in enumerate(rows[1:], 1):
            if i == len(rows)-3:
                previous20, previous50 = ema20, ema50
            ema20 += (candle.close-ema20)*2/21
            ema50 += (candle.close-ema50)*2/51
        noise = tick / 2
        if ema20-ema50 > noise and ema20-previous20 > noise and ema50-previous50 > noise:
            ema_side = 1
        elif ema50-ema20 > noise and previous20-ema20 > noise and previous50-ema50 > noise:
            ema_side = -1
    wave_side = regime_side(regime)
    proof = regime.get('structure_sync') or {}
    early = proof.get('proof')==EARLY_M15_RECLAIM and proof.get('side')==wave_side and bool(wave_side)
    trend_side = wave_side if early or ema_side == wave_side else 0
    location = None
    if dealing_range:
        low, high = dealing_range['low'], dealing_range['high']
        location = 100*(snapshot.price-low)/(high-low)
    discount = location is not None and location < 30
    premium = location is not None and location > 70
    phase = ('BULLISH_RECLAIM' if early and wave_side==1 else
             'BEARISH_RECLAIM' if early else
             'BEARISH_DISCOUNT' if ema_side == -1 and discount else
             'BULLISH_PREMIUM' if ema_side == 1 and premium else
             'BEARISH' if trend_side == -1 else 'BULLISH' if trend_side == 1 else
             'RANGE' if regime.get('state') in ('RANGE','RANGE_LIQUIDITY_ROTATION') else 'TRANSITION')
    labels = {'BULLISH_RECLAIM':'Підтверджений висхідний злам M15',
              'BEARISH_RECLAIM':'Підтверджений низхідний злам M15',
              'BEARISH_DISCOUNT':'Низхідна структура біля дна діапазону',
              'BULLISH_PREMIUM':'Висхідна структура біля вершини діапазону',
              'BEARISH':'Тренд низхідний','BULLISH':'Тренд висхідний',
              'RANGE':'Діапазон','TRANSITION':'Напрямок хвилі й EMA не підтвердили спільного тренду'}
    return {'phase':phase, 'phase_text':labels[phase], 'trend_side':trend_side, 'ema_side':ema_side,
            'ema20':ema20,'ema50':ema50,'range_position_pct':location,
            'discount':discount,'premium':premium,'early_reclaim':proof if early else None,
            'ema_test':bool(not early and ema20 is not None and
                            min(ema20,ema50)<=snapshot.price<=max(ema20,ema50))}
