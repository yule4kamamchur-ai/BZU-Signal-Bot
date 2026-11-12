"""Causal two-limit challenger/defender arbitration. Pure decisions, no fills.

All candidates and incumbent limits are compared using the SAME uncalibrated
score (not a claim of win probability). Cancel/replace is a two-phase workflow:
request cancellation NOW, submit a new limit only in a LATER scan, once the
old interval is reconciled. In-flight cancellations continue reserving capacity.
"""
from __future__ import annotations
import math

MIN_ADVANTAGE=14.0   # score points; conservative, NOT estimated expected R
MIN_AGE_MS=9*60_000 # do not churn a freshly queued order
COOLDOWN_MS=15*60_000


def loss_cooldown(plan, trades, now):
    """Legacy cooldown; current verified trend execution uses the zone and slot guards."""
    if plan.get('trend_rel') == 'WITH_TREND' and plan.get('order_type') in ('MARKET', 'LIMIT'):
        return {'allowed': True, 'reason': 'TREND_EXECUTION_POLICY'}
    losses=[t for t in trades if t.get('side')==plan.get('side')
            and t.get('close_reason') in ('STOP','STOP_GAP','SL','STOP_LOSS')
            and t.get('net_r',0)<0 and 0<t.get('closed_ts',0)<=now]
    last=max(losses,key=lambda t:t['closed_ts'],default=None)
    if last is None or now>=last['closed_ts']+3_600_000:
        return {'allowed':True,'reason':'COOLDOWN_CLEAR'}
    proof=plan.get('ict_evidence',{})
    raid=(proof.get('latest_sweep') or {}).get('known_ts',0)
    raids=plan.get('confluence',{}).get('session_sweeps',[])
    raid=max([raid]+[r.get('sweep_ts',0) for r in raids])
    exception=plan.get('score',0)>=82 and last['closed_ts']<raid<=now
    return {'allowed':exception,'reason':'NEW_A_PLUS_SWEEP' if exception else 'SAME_SIDE_LOSS_COOLDOWN',
            'last_stop_ts':last['closed_ts'],'until_ts':last['closed_ts']+3_600_000,
            'new_sweep_known_ts':raid}


def directional_lock(pending, active, side, snapshot, cfg, engine):
    """An OPEN position cannot be cancelled as though it were an unfilled order."""
    import directional_policy
    if any(p['side']!=side for p in active):
        return {'allowed':False,'reason':'OPEN_POSITION_DIRECTION_LOCK'}
    opposing=[p for p in pending if p['side']!=side]
    if not opposing: return {'allowed':True,'reason':'SAME_DIRECTION'}
    flip=directional_policy.structural_flip(snapshot,side,min(p['placed_ts'] for p in opposing),cfg.tick,engine)
    return {'allowed':False,'cancel_old_first':flip['confirmed'],
            'reason':'STRUCTURE_FLIPPED' if flip['confirmed'] else 'PENDING_DIRECTION_LOCK','proof':flip}


def _finite(value, fallback=0.0):
    try:
        number=float(value)
        return number if math.isfinite(number) else fallback
    except (ValueError,TypeError):
        return fallback


def current_score(incumbent, proof):
    """Update the ORIGINAL comparative score with a CLOSED-candle proof delta.

    Do not compare a previously fitted expected-return model against a new
    unvalidated model. Never turn a stale recorded probability into a fact.
    """
    import directional_policy as policy
    old=incumbent.get('ict_evidence',{})
    base=_finite(incumbent.get('direction_score',incumbent.get('decision_score',incumbent.get('score'))))
    if isinstance(old,dict) and old.get('proof_source')=='CLOSED_CANDLES_ONLY':
        previous=policy.adjusted_score({**incumbent,'decision_score':0.,'score':0.},old)
        latest=policy.adjusted_score({**incumbent,'decision_score':0.,'score':0.},proof)
        return round(base+max(-15.,min(15.,latest-previous)),4)
    # Old v12 plans lack proof baselines; preserve their original score and
    # never invent an unjustified quality decrease.
    return base


def cluster_rank(plan, snapshot, tick):
    from trade_meta_model import execution_composite

    evidence = execution_composite(plan, snapshot.price, tick, snapshot.now)
    return (evidence['score'], _finite(plan.get('score')), -abs(snapshot.price - plan['entry']),
            str(plan.get('id', '')))


def should_reverse_on_exhaustion_peak(position, candidates, snapshot, cfg, engine):
    """Release a profitable incumbent only for an executable opposite signal."""
    from position_monitor import current_r

    if position.get('tp1_hit') or not snapshot.trusted or not 0 < snapshot.bid <= snapshot.ask:
        return None
    side = position['side']
    quote = snapshot.bid if side == 1 else snapshot.ask
    if current_r(position,quote) < .35-1e-8:
        return None
    actual = engine.execution_config(position,cfg)
    exit_price = quote*(1-side*actual.slippage_bps/10_000)
    fees = 0.
    if actual.include_commission:
        fees = position['entry']*position.get('entry_fee_rate',actual.maker_fee)+exit_price*actual.taker_fee
    if side*(exit_price-position['entry']) <= fees:
        return None
    ready = [p for p in candidates if p['side']==-side and p.get('rating') in ('A','A+') and p.get('score',0)>=78
             and p.get('quality',{}).get('accepted') and p.get('reversal_exhaustion_bonus')
             and p.get('action_type')=='ACTIVE_ENTRY' and engine.scanner.market_plan(p)
             and (p.get('market_trigger') or {}).get('pattern')=='CONFIRMED_REVERSAL'
             and 0 <= snapshot.now-(p.get('entry_zone') or {}).get('known_ts',0)<2*900_000]
    if not ready:
        return None
    best = max(ready,key=lambda plan:cluster_rank(plan,snapshot,cfg.tick))
    return {'decision':'PEAK_REVERSAL_FLIP','replace_id':position['id'],'candidate_id':best['id'],
            'exit_quote':quote,'close_reason':'CLOSED_REVERSAL_FLIP_PROFIT'}


def stale_replacement(position, candidates, snapshot, cfg, engine):
    from position_monitor import REPLACEMENT_BARS, current_r, duration

    filled = position.get('filled_ts')
    if filled is None or position.get('tp1_hit') or duration(position, snapshot.now)[1] < REPLACEMENT_BARS:
        return None
    exit_quote = snapshot.bid if position['side'] == 1 else snapshot.ask
    if (not 0 < snapshot.bid <= snapshot.ask or abs(current_r(position, exit_quote)) > .15 + 1e-8
            or position['side'] * (exit_quote - position['stop']) <= 0):
        return None
    ready = []
    for plan in candidates:
        zone = plan.get('entry_zone') or {}
        grade = (plan.get('zone_quality') or {}).get('grade')
        if (not (plan.get('rating') in ('A', 'A+') or grade == 'A')
                or not plan.get('quality', {}).get('accepted')
                or not 0 <= snapshot.now - zone.get('known_ts', 0) <= 4 * 900_000
                or plan.get('event_key') == position.get('event_key')):
            continue
        if engine.scanner.market_plan(plan):
            trigger = plan.get('market_trigger') or {}
            if not 0 <= snapshot.now - trigger.get('known_ts', 0) < 900_000:
                continue
        elif (plan.get('action_type') != 'NEW_PENDING_LIMIT' or
              not engine.scanner.passive_gate(plan['entry'], plan['side'], snapshot.bid, snapshot.ask, cfg.tick)['passive']):
            continue
        if plan['side'] != position['side'] and not engine.policy.structural_flip(
                snapshot, plan['side'], filled, cfg.tick, engine)['confirmed']:
            continue
        ready.append(plan)
    if not ready:
        return None
    best = max(ready, key=lambda plan: cluster_rank(plan, snapshot, cfg.tick))
    return {'decision': 'STALE_POSITION_REPLACEMENT', 'replace_id': position['id'],
            'candidate_id': best['id'], 'exit_quote': exit_quote}


def decision(pending, candidates, snapshot, cfg, policy, last_swap_ts=0, engine=None):
    """Propose at most ONE replacement per cycle; return auditable diagnostics.

    Called only for the unfilled book and with quality+ICT accepted candidates.
    Pending canceled/reconciling/filled orders cannot be replaced here.
    """
    current=[p for p in pending if not p.get('cancel_requested_ts')]
    audit={'policy':'CAUSAL_TWO_LIMIT_REPLACEMENT_V1','as_of_ts':snapshot.now,
           'capacity':2,'incumbents':[],'challengers':[],'decision':'KEEP',
           'candidate_id':None,'replace_id':None,'score_scale':'UNVALIDATED_COMPARATIVE_NOT_WR'}
    for p in current:
        from directional_policy import evidence
        fresh=evidence(p,snapshot,cfg.tick)
        score=current_score(p,fresh)
        audit['incumbents'].append({'id':p.get('id'),'side':p.get('side'),
            'score':score,'placed_ts':p.get('placed_ts'),'expires_ts':p.get('expires_ts'),
            'evidence_as_of_ts':fresh.get('as_of_ts')})
    if not current:
        audit['decision']='EMPTY_BOOK'
        return audit
    if len(current) == 1 and current[0].get('trend_rel') == 'WITH_TREND':
        audit.update(decision='KEEP_TREND_PRELIMIT', capacity=1)
        return audit
    if len(current)>2:
        audit['decision']='LEGACY_EXCESS_QUEUED'
        return audit
    if snapshot.now-last_swap_ts<COOLDOWN_MS and last_swap_ts:
        audit['decision']='REPLACEMENT_COOLDOWN'
        return audit
    # No mixed-direction pending book is ever automatically traded.
    if len({p['side'] for p in current})!=1:
        audit['decision']='HEDGED_LEGACY_BOOK'
        return audit
    # Never replace before another observation has arrived.
    eligible=[(p,v) for p,v in zip(current,audit['incumbents'])
              if snapshot.now-_finite(p.get('placed_ts',snapshot.now))>=MIN_AGE_MS
              and _finite(p.get('expires_ts',0))>snapshot.now+180_000]
    if not eligible:
        audit['decision']='INCUMBENT_TOO_NEW_OR_EXPIRING'
        return audit
    # A new structural step gets priority over score-only rotation.
    # Keep the cancellation handshake and make the new entry provably closer.
    import strategy_organics
    a15 = engine.atr(strategy_organics.closed(snapshot, '15m')) if engine else 0.
    for incumbent, info in sorted(eligible, key=lambda item: -abs(snapshot.price-item[0]['entry'])):
        side = incumbent['side']
        gap = side * (snapshot.price - incumbent['entry'])
        if a15 <= 0 or gap <= 1.3 * a15:
            continue
        for candidate in sorted(candidates,key=lambda p:policy.ranking(p,snapshot.price,cfg.tick),reverse=True):
            zone = candidate.get('entry_zone', {})
            origin = candidate.get('zone_origin', {})
            improvement = side * (candidate['entry'] - incumbent['entry'])
            if (candidate.get('side') != side
                    or candidate.get('action_type') != 'NEW_PENDING_LIMIT'
                    or candidate.get('event_key') == incumbent.get('event_key')
                    or candidate.get('id') == incumbent.get('id')
                    or not candidate.get('quality',{}).get('accepted')
                    or not origin.get('confirmed') or not origin.get('break_known_ts')
                    or not ('FVG' in zone.get('kind', '') or 'BREAKER' in zone.get('kind', ''))
                    or zone.get('known_ts',0) <= incumbent['placed_ts']
                    or improvement < cfg.tick * .99 or improvement >= gap
                    or any(other is not incumbent and not policy.compatible(candidate, other, cfg.tick)
                           for other in current)):
                continue
            score = _finite(candidate.get('direction_score',candidate.get('score')))
            audit.update(decision='REQUEST_REPLACEMENT',candidate_id=candidate['id'],
                         replace_id=incumbent['id'], advantage=round(score-info['score'],4),
                         reason='DYNAMIC_ORDER_STEP', distance_atr=gap/a15)
            return audit
    # Review the weakest mature incumbent first, but keep the OTHER plan
    # in the comparison: replacement cannot create duplicate same-thesis risk.
    weakest,weak_info=min(eligible,key=lambda item:item[1]['score'])
    retained=[p for p in current if p is not weakest]
    for challenger in sorted(candidates,key=lambda p:policy.ranking(p,snapshot.price,cfg.tick),reverse=True):
        if challenger.get('side') not in (-1,1):continue
        if challenger.get('action_type') == 'ACTIVE_ENTRY':continue
        if challenger.get('side')!=weakest.get('side'):continue  # opposite switching is a separate portfolio lock
        if challenger.get('id')==weakest.get('id') or challenger.get('event_key')==weakest.get('event_key'):
            continue
        if not challenger.get('quality',{}).get('accepted',True):continue
        if challenger.get('ict_admission_reason','CONFIRMED')!='CONFIRMED':continue
        if any(not policy.compatible(challenger,other,cfg.tick) for other in retained):
            continue
        if any(challenger.get('event_key')==p.get('event_key') for p in current):
            continue
        score=_finite(challenger.get('direction_score',challenger.get('decision_score',challenger.get('score'))))
        diff=round(score-weak_info['score'],4)
        audit['challengers'].append({'id':challenger.get('id'),'side':challenger['side'],
                                    'score':score,'vs_incumbent':weak_info['id'],
                                    'advantage':diff,'ict_admitted':True})
        if diff>=max(MIN_ADVANTAGE,_finite(cfg.conflict_margin)):
            audit.update(decision='REQUEST_REPLACEMENT',candidate_id=challenger.get('id'),
                         replace_id=weakest['id'],advantage=diff,
                         reason='BETTER_SAME_DIRECTION_ICT_CANDIDATE')
            break
        # sorted list: first high-scoring viable challenger is best
        audit['decision']='NO_VERIFIED_ADVANTAGE'
        break
    if not audit['challengers'] and audit['decision']=='KEEP':
        audit['decision']='NO_INDEPENDENT_CHALLENGER'
    return audit


def stop_and_reverse(events, trades):
    """Give a just-closed structural failure exactly one opposite-side scan."""
    exits = {e['order_id'] for e in events
             if e['kind']=='CLOSED' and e.get('reason')=='CLOSED_ADVERSE_STRUCTURE_SHIFT'}
    closed = max((p for p in trades if p['id'] in exits),
                 key=lambda p:p['closed_ts'],default=None)
    if closed is None:
        return None
    return {'decision':'STOP_AND_REVERSE_SCAN','side':-closed['side'],
            'closed_order_id':closed['id'],'closed_ts':closed['closed_ts']}
