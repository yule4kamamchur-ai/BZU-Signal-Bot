"""Structural admission map and a single published trading focus."""
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
            pois.append({'low':level,'high':level,'kind':ref['kind'],'known_ts':known,
                         'entry_tolerance':width/2})
        pois = [p for p in pois if side*(snapshot.price-(p['low'] if side==1 else p['high']))
                >= -p.get('entry_tolerance', 0)]
        pois.sort(key=lambda p: max(0., side*(snapshot.price-(p['high'] if side==1 else p['low']))
                                     - p.get('entry_tolerance', 0)))
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
    if not any(p['low']-p.get('entry_tolerance',0) <= entry <= p['high']+p.get('entry_tolerance',0)
               for p in plan[label]['poi_zones']):
        return 'BLOCKED_OUTSIDE_HTF_POI'
    return None


FOCUS_DISTANCE = .80


def _hourly_excursions(snapshot):
    """Closed, contiguous UTC hours for an empirical level-touch estimate."""
    groups = {}
    for bar in closed(snapshot.candles.get('15m', []), M15, snapshot.now)[-192:]:
        groups.setdefault(bar.ts // H1 * H1, []).append(bar)
    return [(max(c.high for c in bars)-bars[0].open,
             bars[0].open-min(c.low for c in bars))
            for start,bars in groups.items()
            if [c.ts for c in bars] == [start+i*M15 for i in range(4)]]


def _preview(watch, snapshot, cfg, engine, levels):
    import oil_risk

    side, entry = watch['side'], watch['entry_hint']
    tick = cfg.tick
    entry = engine.round_tick(round(entry+tick/2,10),tick)
    boundary = watch.get('stop_anchor',watch['low'] if side==1 else watch['high'])
    stop, risk, profile = oil_risk.stop_geometry(entry,boundary,side,oil_risk.snapshot_profile(snapshot),
        oil_risk.macro_anchors(snapshot,cfg,engine,side),tick,engine,allow_local=True)
    obstacle, room = oil_risk.structural_room(side,entry,risk,levels)
    tp1 = engine.scanner.structural_target_price(obstacle,snapshot.now,side,tick,engine) if obstacle else None
    if tp1 is not None and side*(tp1-entry) <= 0:
        tp1 = None
    rr = side*(tp1-entry)/risk if tp1 is not None and risk>0 else None
    gate = (profile['gate'] or ('NO_CONFIRMED_TARGET' if rr is None else
            'BLOCKED_NO_ROOM_TO_STRUCTURE' if room is not None and room<1.35-1e-8 else
            oil_risk.entry_tp1_error(side,entry,risk,tp1)))
    return {'entry':entry,'stop':stop if risk>0 and stop>0 else None,'tp1':tp1,
            'rr1':rr,'gate':gate,'preview_only':True}


def _reversal_condition(snapshot, regime, trend):
    # Use a level from confirmed structure. Session highs/lows trail price.
    level = next((proof.get('protected_level') or proof.get('level')
                  for proof in (regime.get('structure_sync') or {}, regime.get('active_structure') or {})
                  if proof.get('side') == trend and 0 < proof.get('known_ts', 0) <= snapshot.now
                  and (proof.get('protected_level') or proof.get('level') or 0) > 0), None)
    trigger = 'Переоцінюємо сценарій після підтвердженого зламу структури M15.'
    if level is not None:
        cross = 'нижче' if trend == 1 else 'вище'
        trigger = f'Переоцінюємо сценарій після закриття M15 {cross} {level:.2f} і підтвердження зламу.'
    return {'side':-trend,'label':'Злам структури M15','level':level,
            'distance':abs(snapshot.price-level) if level is not None else None,'trigger':trigger}


def focus(snapshot, cfg, engine, regime, *, orders=(), admitted=(), action=None):
    """Select one focus. Developing records describe a test, never an order."""
    from market_wave import focus_context

    plan = build(snapshot,cfg,engine)
    market = focus_context(snapshot,regime,plan['range'],cfg.tick)
    levels = structure_levels(snapshot,engine)
    watches = engine.scanner.developing_watches(snapshot,cfg,engine,regime,market,levels,plan)
    hours = _hourly_excursions(snapshot)
    if len(hours) >= 12:
        for watch in watches:
            index = 0 if watch['level'] >= snapshot.price else 1
            touched = sum(hour[index] >= watch['distance'] - 1e-8 for hour in hours)
            watch['touch_history'] = {'matched_hours':touched,'sample_hours':len(hours)}
    strength = {'EMA20–EMA50 M15':2}
    watches.sort(key=lambda x:(round(x['distance']/cfg.tick),
                               -x.get('touch_history',{}).get('matched_hours',0),
                               strength.get(x['source'],0),x['distance'],
                               x['side'] != market['trend_side'],-x['known_ts']))
    trend = market['trend_side']
    extreme_side = (1 if not trend and market['discount'] and market['ema_side'] <= 0 else
                    -1 if not trend and market['premium'] and market['ema_side'] >= 0 else 0)
    reversal_at_extreme = next((w for w in watches if w['side']==extreme_side
                                and w['setup'].endswith('LIQUIDITY_SWEEP')
                                and w['distance'] <= FOCUS_DISTANCE), None)
    pullback = next((w for w in watches if w['side']==trend
                     and trend*(snapshot.price-w['level']) >= -cfg.tick/2),None)
    reclaim = next((w for w in watches if w['setup']=='M15_STRUCTURE_RETEST'
                    and w['distance']<=FOCUS_DISTANCE),None)
    pullback = reclaim or pullback
    watch = pullback if trend else reversal_at_extreme or next(iter(watches),None)
    order = min(orders,key=lambda p:p.get('status')!='OPEN',default=None)
    accepted = min(admitted,key=lambda p:abs(snapshot.price-p['entry']),default=None)
    current = order or accepted
    developing = None
    if current:
        status = ('POSITION' if order and current.get('status')=='OPEN' else
                  'CANCELLING' if order and current.get('cancel_requested_ts') else
                  'PENDING' if order else 'ADMITTED')
        label = current.get('source_strategy') or current.get('setup') or 'Ордер'
        entry = current['entry']
        primary = {'status':status,'side':current['side'],'setup':label,'label':label,'level':entry,
                   'distance':abs(snapshot.price-entry),'known_ts':current.get('placed_ts',snapshot.now),
                   'expires_ts':current.get('expires_ts') if status!='POSITION' else None,
                   'trigger':{'POSITION':'Позиція відкрита за журналом.',
                              'PENDING':'Чекаємо виконання ліміту за журналом.',
                              'CANCELLING':'Чекаємо підтвердження скасування.',
                              'ADMITTED':'План пройшов допуск. Чекаємо виконання умов входу.'}[status],
                   'invalidation':f"Захисний SL {current['stop']:.2f}.",
                   'preview':{'entry':entry,'stop':current['stop'],'tp1':current.get('tp1',current.get('tp')),
                              'rr1':current.get('rr1'),'gate':None,'preview_only':False}}
        headline = {'POSITION':'СУПРОВІД ПОЗИЦІЇ','PENDING':'ЧЕКАЄМО ВИКОНАННЯ ЛІМІТУ',
                    'CANCELLING':'СКАСУВАННЯ ЛІМІТУ','ADMITTED':'ДОПУЩЕНИЙ ПЛАН'}[status]
    elif market.get('ema_test'):
        primary = None
        headline = 'ТЕСТ ЗОНИ EMA20/50'
    elif watch:
        developing = dict(watch)
        developing['preview'] = _preview(developing,snapshot,cfg,engine,levels)
        primary = developing
        if primary['distance'] > FOCUS_DISTANCE:
            headline = 'ДАЛЕКИЙ ОРІЄНТИР'
        elif primary['setup'].endswith('LIQUIDITY_SWEEP'):
            headline = 'ПОШУК РОЗВОРОТУ ВІД ДНА (LONG)' if primary['side']==1 else 'ПОШУК РОЗВОРОТУ ВІД ВЕРШИНИ (SHORT)'
        elif market['trend_side'] == primary['side']:
            headline = 'ЧЕКАЄМО ВІДКАТ ЗА ТРЕНДОМ'
        else:
            headline = 'ЧЕКАЄМО ПІДТВЕРДЖЕННЯ СТРУКТУРИ'
    else:
        primary = None
        headline = 'ЧЕКАЄМО ВІДКАТ ЗА ТРЕНДОМ' if trend else 'ГОТОВИХ СЕТАПІВ НЕМАЄ'
    alternates = [x for x in watches if primary and x['side']==-primary['side']]
    if primary and primary['setup'].endswith('LIQUIDITY_SWEEP'):
        # At an extreme the alternative is a pullback to structure when one exists.
        alternates.sort(key=lambda x:x['setup'].endswith('LIQUIDITY_SWEEP'))
    alternate = next(iter(alternates),None)
    alternative = ({k:alternate[k] for k in ('side','label','level','distance','trigger','touch_history')
                    if k in alternate} if alternate else None)
    if trend and not current:
        alternative = _reversal_condition(snapshot,regime,trend)
    phase = market['phase_text']
    if market['range_position_pct'] is not None:
        phase += f" · {market['range_position_pct']:.0f}% діапазону"
    if market.get('ema_test'):
        low, high = sorted((market['ema20'],market['ema50']))
        phase += f" · Тест EMA20/50 {low:.2f}–{high:.2f}, чекаємо реакцію M15"
    if market['discount']:
        phase += ' · трендовий SHORT від цієї ціни обмежений правилом 30/70'
    elif market['premium']:
        phase += ' · трендовий LONG від цієї ціни обмежений правилом 30/70'
    reason = action if action in engine.REASON_UA and action != 'WATCH' else 'NO_SETUP_CANDIDATE'
    return {'as_of_ts':snapshot.now,'headline':headline,'market':market,'phase_text':phase,
            'primary':primary,'alternative':alternative,'developing_setup':developing,
            'waiting_reason':None if current else reason,
            'waiting_detail':None if current or reason=='NO_SETUP_CANDIDATE' else engine.REASON_UA[reason]}
