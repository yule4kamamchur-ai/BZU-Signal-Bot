"""Causal map of both sides, shared by admission and the two reports."""
from oil_risk import M15, H1, H4, closed
from ict_methodology import session_liquidity


def dealing_range(rows, as_of):
    window = [c for c in rows if as_of - 192*M15 <= c.ts and c.ts + M15 <= as_of]
    if len(window) < 96:
        return None
    low, high = min(c.low for c in window), max(c.high for c in window)
    return {'low': low, 'high': high, 'discount': low + .30*(high-low),
            'premium': high - .30*(high-low), 'bars': len(window)} if high > low else None


def structure_levels(snapshot, engine):
    cache = vars(snapshot).setdefault('_scenario_levels', {})
    if snapshot.now not in cache:
        m15 = closed(snapshot.candles.get('15m', []), M15, snapshot.now)[-192:]
        levels = []
        for p in session_liquidity(snapshot)['pools']:
            side = 1 if p['name'].endswith('LOW') else -1
            if not any(c.ts >= p['known_ts'] and side*(c.close-p['level']) < 0 for c in m15):
                levels.append({**p, 'kind': p['name'], 'side': side})
        for tf, step, count in (('15m', M15, 96), ('1H', H1, 48), ('4H', H4, 24)):
            rows = closed(snapshot.candles.get(tf, []), step, snapshot.now)[-count:]
            for p in engine.pivots(rows, strength=2):
                side = 1 if p['kind'] == 'LOW' else -1
                known = p['known_ts'] + step
                if any(c.ts >= known and side*(c.close-p['level']) < 0 for c in rows):
                    continue
                levels.append({'kind': tf + '_SWING_' + p['kind'], 'side': side,
                               'level': p['level'], 'known_ts': known})
        cache[snapshot.now] = levels
    return cache[snapshot.now]


def build(snapshot, cfg, engine):
    cache = vars(snapshot).setdefault('_scenario_map', {})
    key = (snapshot.now, snapshot.price, cfg.tick)
    if key in cache:
        return cache[key]
    from oil_risk import macro_anchors

    rows = closed(snapshot.candles.get('15m', []), M15, snapshot.now)
    rng = dealing_range(rows, snapshot.now)
    levels = structure_levels(snapshot, engine)
    plans = {}
    for side, label in ((1, 'LONG'), (-1, 'SHORT')):
        pois = []
        references = [p for p in levels if p['side'] == side and not p['kind'].startswith('15m_')]
        references.extend(macro_anchors(snapshot, cfg, engine, side))
        # Keep the real span of unmitigated HTF order blocks.
        if cfg.core:
            for tf, step in (('1H', H1), ('4H', H4)):
                bars = closed(snapshot.candles.get(tf, []), step, snapshot.now)[-96:]
                if bars:
                    for ob in cfg.core.detect_order_blocks(bars, max(cfg.tick, engine.atr(bars)), limit=48):
                        known = ob.created_ts + 2*step
                        if not ob.mitigated and cfg.core.side_sign(ob.side) == side and known <= snapshot.now:
                            pois.append({'low': ob.low, 'high': ob.high, 'kind': tf+'_'+ob.kind,
                                         'known_ts': known})
        width = max(2*cfg.tick, .35*engine.atr(rows))
        for ref in references:
            level, known = ref['level'], ref['known_ts']
            if any(c.ts >= known and side*(c.close-level) < -cfg.tick for c in rows[-192:]):
                continue
            pois.append({'low': level-width/2, 'high': level+width/2, 'kind': ref['kind'], 'known_ts': known})
        pois = [p for p in pois if side*(snapshot.price-(p['low'] if side==1 else p['high'])) >= 0]
        pois.sort(key=lambda p: max(0., side*(snapshot.price-(p['high'] if side==1 else p['low']))))
        poi = pois[0] if pois else None
        edge = (poi['high'] if side==1 else poi['low']) if poi else snapshot.price
        target = min((p for p in levels if p['side'] == -side and side*(p['level']-edge)>cfg.tick),
                     key=lambda p: side*(p['level']-edge), default=None)
        plans[label] = {'side': side, 'poi': poi, 'poi_zones': pois, 'target': target,
                        'breakout_level': (rng['high'] if side == 1 else rng['low']) if rng else None}
    result = {'as_of_ts': snapshot.now, 'range': rng, **plans}
    cache[key] = result
    return result


def range_retest(snapshot, zone, side, tick, entry):
    proof = zone.get('wave_proof') or {}
    broken, retest = proof.get('break_ts', 0), proof.get('retest_ts', 0)
    if zone.get('kind') not in ('RECLAIM_BREAKER','SR_FLIP_RECLAIM') or not 0 < broken < retest <= snapshot.now:
        return False
    rows = closed(snapshot.candles.get('15m', []), M15, snapshot.now)
    rng = dealing_range(rows, broken-M15)
    if rng is None:
        return False
    bound = rng['high'] if side==1 else rng['low']
    breakout = next((c for c in rows if c.ts+M15 == broken), None)
    test = next((c for c in rows if c.ts+M15==retest), None)
    return bool(breakout and test and side*(breakout.close-bound)>tick
                and side*((test.low if side==1 else test.high)-bound)<=tick
                and side*(test.close-bound)>0
                and abs(zone.get('reference',entry)-bound)<=2*tick and side*(entry-bound)>=-tick)


def entry_error(snapshot, cfg, engine, side, entry, zone, *, trend):
    from oil_risk import enabled

    if not enabled(cfg):
        return None
    plan = build(snapshot, cfg, engine)
    if range_retest(snapshot, zone, side, cfg.tick, entry):
        return None
    rng = plan['range']
    if trend and rng is None:
        return 'DEALING_RANGE_UNAVAILABLE'
    if trend and (entry <= rng['discount'] if side == -1 else entry >= rng['premium']):
        return 'BLOCKED_DEALING_RANGE_30_70'
    from market_wave import BREAKDOWN, breakdown_zones, breakdown_intact

    if zone.get('kind') == BREAKDOWN:
        witnessed = side == -1 and breakdown_intact(zone,snapshot,cfg.tick) and any(
            z['known_ts']==zone['known_ts'] and abs(z['reference']-zone['reference'])<cfg.tick/2
            for z in breakdown_zones(snapshot,cfg.tick,engine))
        # The witnessed former support is the new local POI.
        return None if witnessed else 'BREAKDOWN_EXPIRED'
    label = 'LONG' if side==1 else 'SHORT'
    if not any(p['low'] <= entry <= p['high'] for p in plan[label]['poi_zones']):
        return 'BLOCKED_OUTSIDE_HTF_POI'
    return None
