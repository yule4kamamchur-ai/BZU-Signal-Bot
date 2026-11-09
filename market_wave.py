"""Closed-candle reversal and continuation zones for the current market wave."""
from dataclasses import replace

import ict_methodology as ict

M15 = 900_000
M5 = 300_000
REVERSALS = frozenset({'SWEEP_REVERSAL', 'M15_CHOCH', 'BREAKER_CHOCH'})
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


def _reversals(snapshot, rows, step, tick, engine, atr15):
    if len(rows) < 2:
        return
    pivots = engine.pivots(rows[-80:], strength=1)
    for i in range(max(1, len(rows) - (4 if step == M5 else 2)), len(rows)):
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
            eligible = [p for p in pools if p['name'].endswith(suffix)
                        and p['known_ts'] <= bar.ts
                        and side * (p['level'] - extreme) >= tick - 1e-8
                        and abs(p['level'] - extreme) <= max(.20, .8 * atr15)]
            for pool in sorted(eligible, key=lambda p: abs(p['level'] - extreme)):
                level = pool['level']
                swept = (side * (wick_edge - level) >= -tick / 2
                         and side * (bar.close - level) >= tick - 1e-8)
                confirmation = bar
                shift = None
                if swept:
                    kind = 'SWEEP_REVERSAL'
                else:
                    for j in range(i, min(i + 3, len(rows))):
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
                         'pattern': 'WICK_RECLAIM' if swept else 'EXTREME_CHOCH'}
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


def zones(snapshot, cfg, engine, regime):
    """Use M5 only inside an established M15/HTF direction or a witnessed raid."""
    import directional_policy as direction
    import setup_scanner as scanner

    cache = vars(snapshot).setdefault('_wave_zones', {})
    key = (snapshot.now, cfg.tick, direction.regime_side(regime))
    if key in cache:
        return cache[key]
    m15 = direction.closed(snapshot, '15m')
    m5 = scanner.closed_five_minutes(snapshot)
    atr = engine.atr(m15)
    if atr <= 0:
        return []
    tick = cfg.tick
    results = [*_reversals(snapshot, m15, M15, tick, engine, atr),
               *_reversals(snapshot, m5, M5, tick, engine, atr), *_breakers(m15, cfg, engine)]
    side = direction.regime_side(regime)
    if side and (regime.get('active_structure') or direction.momentum_aligned(snapshot, side)):
        results.extend(_m5_continuations(m5, side, atr, tick, engine))
    cache[key] = [z for z in results if z['high'] - z['low'] >= tick - 1e-8
                  and 0 <= snapshot.now - z['known_ts'] < 30 * 60_000]
    return cache[key]


def witnessed(zone, snapshot, cfg, engine, regime):
    keys = ('kind', 'side', 'low', 'high', 'known_ts', 'reference', 'wave_proof', 'sweep')
    return next((z for z in zones(snapshot, cfg, engine, regime)
                 if all(z.get(k) == zone.get(k) for k in keys)), None)


def reversal_override(side, zone, snapshot, tick):
    """Consume the sweep that the planner already reconstructed from candles."""
    zone = zone or {}
    sweep = zone.get('sweep') or {}
    return (zone.get('kind') in REVERSALS and sweep.get('side') == side
            and sweep.get('pattern') in ('WICK_RECLAIM', 'EXTREME_CHOCH')
            and 0 < sweep.get('reference_known_ts', 0) <= zone.get('known_ts', 0)
            and 0 <= snapshot.now - zone.get('known_ts', 0) < 30 * 60_000
            and side * (snapshot.price - sweep['extreme']) > tick / 2)
