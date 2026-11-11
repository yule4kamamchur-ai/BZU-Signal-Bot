"""Closed-candle reversal and continuation zones for the current market wave."""
from dataclasses import replace

import ict_methodology as ict
import oil_risk

M15 = 900_000
M5 = 300_000
TREND_ZONE_AGE = 6 * 60 * 60_000
TREND_ZONES = frozenset({'BREAKER_BLOCK', 'FVG_PULLBACK', 'FIB_OTE_ZONE', 'SWEEP_CONTINUATION', 'BOS_CONTINUATION'})
REVERSALS = frozenset({'SWEEP_REVERSAL', 'M15_CHOCH', 'BREAKER_CHOCH', 'MULTIBAR_SWEEP', 'RECLAIM_BREAKER'})
CONTINUATIONS = frozenset({'BULLISH_BREAKER', 'BEARISH_BREAKER', 'CONTINUATION_PULLBACK',
                           'M5_IMPULSE_OB', 'FLAG_RETEST'})


def structure_breaks(rows, step, tick, engine, lookback):
    """A displacement must cross a pivot known before the candle opened."""
    points = engine.pivots(rows[-80:], strength=1)
    for i in range(max(7, len(rows) - lookback), len(rows)):
        bar = rows[i]
        atr = engine.atr(rows[:i])
        for side in (1, -1):
            if atr <= 0 or side * (bar.close - bar.open) <= .6 * atr:
                continue
            pivot = ict.body_break(rows, i, points, side, tick, step)
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
            yield _trend_zone('BREAKER_BLOCK', side, (level, level - side * max(tick, .12 * atr)),
                              bar, M15, level, structure='PIVOT',
                              pivot_ts=point['ts'], reference_known_ts=point['known_ts'] + M15)
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
            yield _trend_zone('BREAKER_BLOCK', side, near, bar, M15, entry, structure='SHELF',
                              reference_known_ts=prior[-1].ts + M15, shelf_start_ts=prior[0].ts)
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

    points = engine.pivots(rows[-50:],strength=1)
    for side in (1,-1):
        for sweep in direction.multi_bar_reversals(rows,side,tick,lookback=36):
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
            refs = [p for p in points if p['kind']==kind and p['known_ts']+M15 <= raid.ts
                    and side*(p['level']-extreme) >= .35]
            if not refs:
                continue
            reference = refs[-1]
            level = reference['level']
            start = confirm_index
            for index in range(start,min(len(rows),start+13)):
                atr = engine.atr(rows[max(0,index-20):index])
                if not ict.reclaim_break(rows,index,side,level,tick,atr):
                    continue
                width = min(.25,max(2*tick,.30*atr))
                low, high = sorted((level,level-side*width))
                yield {**_zone('RECLAIM_BREAKER',side,low,high,rows[index],M15,level,
                               pivot_ts=reference['ts'],reference_known_ts=reference['known_ts']+M15,
                               exhausted_adr=exhausted),'sweep':sweep}
                break


def zones(snapshot, cfg, engine, regime):
    """Use M5 only inside an established M15/HTF direction or a witnessed raid."""
    import directional_policy as direction
    import setup_scanner as scanner

    cache = vars(snapshot).setdefault('_wave_zones', {})
    key = (snapshot.now, cfg.tick, direction.regime_side(regime), regime.get('trend_anchor_side'), oil_risk.enabled(cfg))
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
        results.extend(_trend_breakers(m15, trend, tick, engine))
        results.extend(_trend_fvgs(m15, trend, M15, tick, engine))
        results.extend(_trend_fvgs(direction.closed(snapshot, '1H'), trend, oil_risk.H1, tick, engine))
        results.extend(_trend_ote(m15, trend, tick, engine))
        results.extend(_trend_sweep(m15, trend, M15, tick, engine))
        results.extend(_trend_sweep(m5, trend, M5, tick, engine))
    if oil_risk.enabled(cfg):
        results.extend(_multibar_zones(m15,tick,engine,oil_risk.snapshot_profile(snapshot)['adr_d1']))
    cache[key] = [z for z in results if z['high'] - z['low'] >= tick - 1e-8
                  and 0 <= snapshot.now - z['known_ts'] < (TREND_ZONE_AGE if z['kind'] in TREND_ZONES or z['kind']=='RECLAIM_BREAKER' else 30 * 60_000)]
    return cache[key]


def witnessed(zone, snapshot, cfg, engine, regime):
    keys = ('kind', 'side', 'low', 'high', 'known_ts', 'reference', 'wave_proof', 'sweep')
    return next((z for z in zones(snapshot, cfg, engine, regime)
                 if all(z.get(k) == zone.get(k) for k in keys)), None)


def reversal_override(side, zone, snapshot, tick, *, require_macro=True):
    """Consume the sweep that the planner already reconstructed from candles."""
    zone = zone or {}
    sweep = zone.get('sweep') or {}
    if zone.get('kind') in ('MULTIBAR_SWEEP','RECLAIM_BREAKER'):
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
