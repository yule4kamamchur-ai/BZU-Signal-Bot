"""Shared, causal Brent swing risk and execution thresholds."""
from math import isfinite
from statistics import mean

INSTRUMENT = 'BZ-USDT-SWAP'
MODEL = 'ORGANIC_OIL_V32'
M15, H1, H4, DAY = 900_000, 3_600_000, 14_400_000, 86_400_000
TP1_MIN, TP1_MAX, TP2_MIN, TP2_MAX = 1.00, 1.45, 1.80, 2.25
TARGET_1_FALLBACK = 1.15


def closed(rows, step, as_of):
    return sorted({c.ts: c for c in rows if c.confirmed and c.ts % step == 0
                   and (as_of is None or c.ts + step <= as_of)
                   and all(isfinite(p) and p > 0 for p in (c.open, c.high, c.low, c.close))
                   and c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high}.values(),
                  key=lambda c: c.ts)


def complete_ranges(rows, step, period):
    """Aggregate only complete UTC buckets, with all constituents present."""
    groups = {}
    for bar in rows:
        groups.setdefault(bar.ts // period * period, []).append(bar)
    count = period // step
    return {start: (max(c.high for c in group), min(c.low for c in group), group[-1].close)
            for start, group in groups.items()
            if len(group) == count and group[0].ts == start and group[-1].ts == start + period - step}


def daily_ranges(m15, h1, h4):
    return dict(sorted({
        **complete_ranges(h4, H4, DAY),
        **complete_ranges(m15, M15, DAY),
        **complete_ranges(h1, H1, DAY),
    }.items()))


def true_ranges(bars, step):
    return [max(h - low, abs(h - previous[2]), abs(low - previous[2]))
            for (first, previous), (second, (h, low, _)) in zip(bars, bars[1:]) if second - first == step]


def contiguous_ranges(bars, step):
    start = len(bars) - 1
    while start > 0 and bars[start][0] - bars[start - 1][0] == step:
        start -= 1
    return true_ranges(bars[start:], step)


def price_bounds(price):
    return max(1.15, round(price * .0125, 2)), max(2.10, round(price * .022, 2))


def compute_organic_oil_risk_profile(price, candles_15m, candles_1h, candles_4h,
                                     *, as_of=None, active_session=False):
    """ATR uses 14 true ranges; ADR averages up to 14 complete observed days."""
    m15 = closed(candles_15m, M15, as_of)
    h1 = closed(candles_1h, H1, as_of)
    h4 = closed(candles_4h, H4, as_of)
    hours = sorted({**complete_ranges(m15, M15, H1),
                    **{c.ts: (c.high, c.low, c.close) for c in h1}}.items())
    ranges = contiguous_ranges(hours, H1) if hours and (as_of is None or as_of - hours[-1][0] < 3 * H1) else []
    quarters = contiguous_ranges([(c.ts, (c.high, c.low, c.close)) for c in m15], M15)
    atr15 = mean(quarters[-14:]) if len(quarters) >= 14 else None
    hourly = mean(ranges[-14:]) if len(ranges) >= 14 else None
    atr1h = hourly if hourly else 2.15 * atr15 if atr15 else None
    days = list(daily_ranges(m15, h1, h4).values())[-14:]
    adr = mean(high - low for high, low, _ in days) if days else None
    turbulent = bool(hourly and len(ranges) >= 28 and mean(ranges[-28:-14]) > 0
                     and hourly >= 1.35 * mean(ranges[-28:-14]))
    multiplier = 1.35 if turbulent else 1.20 if active_session else 1.
    floor, cap = price_bounds(price)
    volatility = 1.65 * (atr1h or 0) * multiplier
    adr_stop = .45 * (adr or 0)
    target = max(1.45, round(price * .0155, 2), volatility, adr_stop)
    return {'model': MODEL, 'as_of_ts': as_of, 'reference_price': price,
            'atr_1h': atr1h, 'atr_source': '1H_TRUE_RANGE' if hourly else '15M_X_2_15' if atr1h else 'UNAVAILABLE',
            'adr_d1': adr, 'adr_days': len(days), 'session_multiplier': multiplier,
            'floor': floor, 'target': target, 'cap': cap, 'volatility_distance': volatility,
            'adr_distance': adr_stop, 'min_rr1': TP1_MIN, 'min_rr2': TP2_MIN}


def enabled(cfg):
    return cfg.instrument == INSTRUMENT


def is_organic(plan):
    return (plan.get('organic_risk') or {}).get('model') == MODEL


def snapshot_profile(snapshot):
    from ict_methodology import session_liquidity

    cache = vars(snapshot).setdefault('_oil_risk_cache', {})
    key = (snapshot.now, snapshot.price)
    if key not in cache:
        cache[key] = compute_organic_oil_risk_profile(
            snapshot.price, *(snapshot.candles.get(tf, []) for tf in ('15m', '1H', '4H')),
            as_of=snapshot.now, active_session=session_liquidity(snapshot)['active'])
    return cache[key]


def macro_anchors(snapshot, cfg, engine, side):
    """Confirmed opposing-side falsifiers from 1H and 4H structure."""
    cache = vars(snapshot).setdefault('_oil_anchors', {})
    key = (snapshot.now, side)
    if key not in cache:
        anchors = []
        for timeframe, step in (('1H', H1), ('4H', H4)):
            rows = closed(snapshot.candles.get(timeframe, []), step, snapshot.now)[-96:]
            kind = 'LOW' if side == 1 else 'HIGH'
            for pivot in engine.pivots(rows, strength=2):
                if (pivot['kind'] == kind and not any(c.ts > pivot['known_ts']
                        and side * (c.close - pivot['level']) < -cfg.tick for c in rows)):
                    anchors.append({'level': pivot['level'], 'timeframe': timeframe, 'kind': 'SWING_' + kind,
                                    'known_ts': pivot['known_ts'] + step})
            if cfg.core and rows:
                for ob in cfg.core.detect_order_blocks(rows, max(cfg.tick, engine.atr(rows)), limit=48):
                    if not ob.mitigated and cfg.core.side_sign(ob.side) == side and ob.created_ts + 2 * step <= snapshot.now:
                        anchors.append({'level': ob.low if side == 1 else ob.high, 'timeframe': timeframe,
                                        'kind': ob.kind, 'known_ts': ob.created_ts + 2 * step})
        cache[key] = anchors
    return cache[key]


def stop_geometry(entry, local_boundary, side, profile, anchors, tick, engine, *, allow_local=False):
    """A cap can clip volatility. A cap cannot move SL through a structural buffer."""
    floor, cap = price_bounds(entry)
    macro = min((a for a in anchors if 1.20 - 1e-8 <= side * (entry - a['level']) <= 1.80 + 1e-8),
                key=lambda a: (side * (entry - a['level']), a['timeframe'] != '4H'), default=None)
    anchor = macro['level'] if macro else local_boundary
    boundary = min(local_boundary, anchor) if side == 1 else max(local_boundary, anchor)
    buffer = max(tick, .25 * (profile['atr_1h'] or 0))
    structural_distance = max(0., side * (entry - boundary)) + buffer
    volatility_target = max(1.45, round(entry * .0155, 2),
                            profile['volatility_distance'], profile['adr_distance'])
    raw = max(floor, structural_distance, min(cap, volatility_target))
    stop = engine.round_tick(round(entry - side * raw, 10), tick, up=side == -1)
    risk = side * (entry - stop)
    if raw <= cap + 1e-8 and risk > cap + 1e-8:
        stop = engine.round_tick(round(entry - side * cap, 10), tick, up=side == 1)
        risk = side * (entry - stop)
    reason = ('OIL_VOLATILITY_UNAVAILABLE' if not profile['atr_1h'] else
              'MACRO_STOP_ANCHOR_REQUIRED' if macro is None and not allow_local else
              'STRUCTURAL_STOP_TOO_WIDE' if stop <= 0 or risk > cap + 1e-8 or
              risk < max(floor, structural_distance) - 1e-8 else None)
    return stop, risk, {**profile, 'entry': entry, 'floor': floor, 'cap': cap, 'target': volatility_target,
                        'macro_anchor': macro, 'anchor_source': 'HTF' if macro else 'CONFIRMED_ZONE' if allow_local else None,
                        'boundary': boundary, 'buffer': buffer,
                        'structural_distance': structural_distance, 'gate': reason}


def macro_extrema(snapshot, as_of, engine, side):
    """Previous completed UTC day and recent confirmed 1H/4H swing pools."""
    cache = vars(snapshot).setdefault('_oil_sweep_pools', {})
    key = (as_of, side)
    if key in cache:
        return cache[key]
    kind = 'LOW' if side == 1 else 'HIGH'
    h1 = closed(snapshot.candles.get('1H', []), H1, as_of)
    h4 = closed(snapshot.candles.get('4H', []), H4, as_of)
    m15 = closed(snapshot.candles.get('15m', []), M15, as_of)
    previous = as_of // DAY * DAY - DAY
    day = daily_ranges(m15, h1, h4).get(previous)
    pools = ([{'name': 'PD_' + kind, 'timeframe': 'D1', 'level': day[1 if side == 1 else 0],
               'known_ts': previous + DAY}] if day else [])
    for timeframe, step, bars in (('1H', H1, h1), ('4H', H4, h4)):
        for pivot in engine.pivots(bars[-48:], strength=2):
            if pivot['kind'] == kind:
                pools.append({'name': timeframe + '_SWING_' + kind, 'timeframe': timeframe,
                              'level': pivot['level'], 'known_ts': pivot['known_ts'] + step})
    cache[key] = [p for p in pools if not any(
        c.ts >= p['known_ts'] and side * ((c.low if side == 1 else c.high) - p['level']) < -1e-8
        for c in m15)]
    return cache[key]
