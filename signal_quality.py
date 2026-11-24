"""Causal confirmation and empirical, past-only selection for all 24 setups.

No score is converted into a probability. Counterfactual PAPER observations
never occupy portfolio slots and never appear in the executed trade ledger.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any

SCHEMA = 'ict_quality_v1'
STRATEGY = 'bzu-full-position-v14.4.1'
TF = {'3m':180_000,'15m':900_000,'1H':3_600_000,'4H':14_400_000}


@dataclass(frozen=True)
class Settings:
    target_win_rate: float = .70
    learning_prior_weight: float = 12.
    maximum_cost_fraction: float = .25
    maximum_shadow_plans: int = 48
    maximum_observations: int = 2500


# Every original detector has an explicit contract, rather than being replaced
# with a generic two-pattern strategy. Thresholds are fixed, not curve-fitted.
CONTRACTS = {
    'SWEEP_RECLAIM':('LIQUIDITY_REVERSAL','reversal'),
    'CAPITULATION_RECOVERY':('LIQUIDITY_REVERSAL','reversal'),
    'RANGE_EDGE_REVERSAL':('LIQUIDITY_REVERSAL','range'),
    'FAILED_AUCTION_REJECTION':('LIQUIDITY_REVERSAL','reversal'),
    'LIQUIDITY_SWEEP_REVERSAL_SHORT':('LIQUIDITY_REVERSAL','reversal'),
    'BUYER_EXHAUSTION_SHORT':('LIQUIDITY_REVERSAL','reversal'),
    'PULLBACK_CONTINUATION':('TREND_CONTINUATION','continuation'),
    'FRESH_BASE_CONTINUATION':('TREND_CONTINUATION','continuation'),
    'ACCEPTANCE_RETEST_CONTINUATION':('TREND_CONTINUATION','continuation'),
    'MOMENTUM_NO_PULLBACK_CONTINUATION':('TREND_CONTINUATION','continuation'),
    'ACCELERATION_PULLBACK_REENTRY':('TREND_CONTINUATION','continuation'),
    'DIRECTION_FLIP_15M':('STRUCTURAL_EXPANSION','breakout'),
    'TREND_IGNITION':('STRUCTURAL_EXPANSION','breakout'),
    'BREAKOUT_RETEST':('STRUCTURAL_EXPANSION','breakout'),
    'RANGE_COMPRESSION_BREAKOUT':('STRUCTURAL_EXPANSION','breakout'),
    'OPENING_RANGE_BREAKOUT':('SESSION_EXPANSION','breakout'),
    'LIQUIDITY_LADDER':('SESSION_EXPANSION','breakout'),
    'FAILED_OPENING_RANGE_BREAKOUT':('FAILED_EXPANSION','reversal'),
    'FAILED_BREAKOUT_SHORT':('FAILED_EXPANSION','reversal'),
    'MSS_REVERSAL_SHORT':('FAILED_EXPANSION','reversal'),
    'OR_FAILURE_2_SHORT':('FAILED_EXPANSION','reversal'),
    'SESSION_MEAN_RECLAIM':('VALUE_RECLAIM','value'),
    'DAILY_WEEKLY_OPEN_RECLAIM':('VALUE_RECLAIM','value'),
    'TIME_OF_DAY_ADAPTIVE':('VALUE_RECLAIM','range'),
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def signature(cfg) -> str:
    # A model trained under different fees/geometry cannot be used silently.
    values={key:value for key,value in vars(cfg).items() if key!='core'}
    if not getattr(cfg,'include_commission',True):
        values.update(maker_fee=0.,taker_fee=0.)
    return digest(values)


def closed_rows(snapshot, timeframe):
    return [c for c in snapshot.candles.get(timeframe,[])
            if c.confirmed and c.ts+TF[timeframe]<=snapshot.now]


def direction(value) -> int:
    return 1 if str(value).upper()=='LONG' else -1 if str(value).upper()=='SHORT' else 0


def shifted(rows, side: int, engine) -> bool:
    """Break of a pivot known BEFORE the crossing close, with no future pivot."""
    if len(rows)<12:
        return False
    pivots = engine.pivots(rows,2)
    for index in range(max(1,len(rows)-6),len(rows)):
        c,previous = rows[index],rows[index-1]
        points = [q for q in pivots if q['known_ts']<c.ts
                  and q['kind']==('HIGH' if side==1 else 'LOW')]
        if points:
            level=points[-1]['level']
            if side*(previous.close-level)<=0<side*(c.close-level) and side*(rows[-1].close-level)>0:
                return True
    return False


def features(plan: dict, snapshot, context: dict, cfg, engine) -> dict:
    import setup_scanner as scanner
    source_tf=scanner.timeframe(plan)
    micro_tf=plan.get('formation_micro_timeframe',scanner.MICRO[source_tf])
    formation_tf=plan.get('formation_timeframe',source_tf)
    side=plan['side'];c3=closed_rows(snapshot,micro_tf);c15=closed_rows(snapshot,formation_tf)
    a3=max(engine.atr(c3),cfg.tick);a15=max(engine.atr(c15),cfg.tick)
    side3=direction(context.get('structure3m',{}).get('direction'))
    side15=direction(context.get('structure15',{}).get('direction'))
    shift3=shifted(c3,side,engine);shift15=shifted(c15,side,engine)
    impulse=False
    for c in c3[-6:]:
        span=max(c.high-c.low,cfg.tick)
        close_location=(c.close-c.low)/span if side==1 else (c.high-c.close)/span
        if side*(c.close-c.open)>=.65*a3 and close_location>=.70:
            impulse=True
    # Acceptance means multiple closes outside a prior, fixed reference range.
    accept=False
    if len(c15)>=10:
        reference=c15[-10:-2]
        boundary=max(c.high for c in reference) if side==1 else min(c.low for c in reference)
        accept=all(side*(c.close-boundary)>0 for c in c15[-2:])
    swing=c15[-48:]
    low=min((c.low for c in swing),default=plan['entry'])
    high=max((c.high for c in swing),default=plan['entry'])
    location=(plan['entry']-low)/max(high-low,cfg.tick)
    location=max(0.,min(1.,location))
    side1,side4=plan.get('htf_1h',0),plan.get('htf_4h',0)
    agreement=int(side1==side)+int(side4==side)
    opposition=int(side1==-side)+int(side4==-side)
    execution_rows=closed_rows(snapshot,'3m')
    execution_atr=max(engine.atr(execution_rows),cfg.tick)
    shock=max(((c.high-c.low)/execution_atr for c in execution_rows[-2:]),default=0)>3.5
    family,kind=CONTRACTS[plan['setup']]
    align='aligned' if agreement and not opposition else 'mixed' if agreement and opposition else 'against' if opposition else 'neutral'
    return {'setup':plan['setup'],'family':family,'kind':kind,'side':side,
            'setup_timeframe':source_tf,'micro_timeframe':micro_tf,
            'formation_timeframe':formation_tf,'entry_route':plan.get('entry_route','NATIVE_PATTERN'),
            'entry_policy':plan.get('entry_policy','LEGACY_CONFIRMED'),
            'detector_confirmed':plan.get('detector_confirmed',False),
            'htf_opposition':opposition,'maximum_return_atr':cfg.max_entry_distance_atr,
            'regime':str(context.get('regime','UNKNOWN')),'alignment':align,
            'side3':side3,'side15':side15,'shift3':shift3,'shift15':shift15,
            'early_reversal_control':early_reversal_control(plan['setup'],side,snapshot,context,cfg,engine),
            'recovery_confirmed':plan.get('pattern_proof',{}).get('confirmed',True),
            'impulse':impulse,'acceptance':accept,'range_position':round(location,4),
            'shock':shock,'distance_atr':round(side*(snapshot.price-plan['entry'])/a15,4),
            'cost_fraction':round(plan.get('costs_r',0)/max(plan.get('gross_rr',0),1e-9),4),
            'as_of_ts':snapshot.now,'strategy':STRATEGY}


COUNTERTREND_SETUPS=frozenset({'SWEEP_RECLAIM','CAPITULATION_RECOVERY','RANGE_EDGE_REVERSAL',
    'FAILED_AUCTION_REJECTION','LIQUIDITY_SWEEP_REVERSAL_SHORT','BUYER_EXHAUSTION_SHORT',
    'FAILED_BREAKOUT_SHORT','MSS_REVERSAL_SHORT','FAILED_OPENING_RANGE_BREAKOUT',
    'OR_FAILURE_2_SHORT','DIRECTION_FLIP_15M'})


def pretouch_countertrend_allowed(setup,side,primary_side,micro_side,early_control=False):
    return setup in COUNTERTREND_SETUPS and ((primary_side==side and micro_side==side) or early_control)


def early_reversal_control(setup,side,snapshot,context,cfg,engine):
    # A mapped range edge alone is not a rejection. All other eligible names
    # here already require a native rejection/reclaim/transfer pattern.
    if setup not in COUNTERTREND_SETUPS or setup=='RANGE_EDGE_REVERSAL':
        return False
    import execution_atlas
    micro=context.get('formation_micro_timeframe',context.get('micro_timeframe','3m'))
    rows=closed_rows(snapshot,micro)
    cross=execution_atlas.causal_shift(rows,micro,engine,cfg.tick)
    return bool(cross.get('shift') and direction(cross.get('side'))==side
                and direction(context.get('structure3m',{}).get('direction'))==side)


def structural_reason(f: dict, settings: Settings=Settings()) -> str:
    if f.get('entry_policy')=='CHECKLIST_CONFIRMED':
        return 'CONFIRMED' if f.get('detector_confirmed') else 'SETUP_CONDITIONS_INCOMPLETE'
    if f.get('setup')=='CAPITULATION_RECOVERY' and not f.get('recovery_confirmed',True):
        return 'CAPITULATION_RECOVERY_NOT_CONFIRMED'
    if f['shock']:
        return 'SHOCK_STILL_ACTIVE'
    if f['cost_fraction']>settings.maximum_cost_fraction:
        return 'COSTS_CONSUME_EDGE'
    if f['distance_atr']>f.get('maximum_return_atr',2.5):
        return 'RETURN_PATH_TOO_LONG'
    if f.get('entry_policy')=='PRETOUCH_LIMIT':
        # The native detector already checks its own rejection, displacement,
        # range, reclaim or breakout conditions. A resting pullback limit must
        # not wait for a second reaction AFTER its level has been visited.
        if not f.get('detector_confirmed'):
            return 'SETUP_CONDITIONS_INCOMPLETE'
        if f['htf_opposition']==2 and not pretouch_countertrend_allowed(f['setup'],f['side'],f['side15'],f['side3'],f.get('early_reversal_control',False)):
            return 'HTF_REVERSAL_NOT_CONFIRMED'
        return 'CONFIRMED'
    if not (f['side3']==f['side'] or f['shift3']):
        return 'NO_CONFIRMED_3M_CONTROL'
    if not (f['impulse'] or f['acceptance']):
        return 'NO_CONFIRMED_DEPARTURE'
    if f['kind'] in ('continuation','breakout'):
        if not (f['side15']==f['side'] or f['shift15'] or f['acceptance']):
            return 'NO_15M_ACCEPTANCE'
        if f['alignment']=='against':
            return 'CONTINUATION_AGAINST_HTF'
    else:
        if f['side15']==-f['side'] and not (f['shift15'] and f['shift3']):
            return 'REVERSAL_NOT_CONFIRMED'
        if f['alignment']=='against' and not (f['shift15'] and f['shift3']):
            return 'HTF_REVERSAL_NOT_CONFIRMED'
    if f['kind']=='range':
        bad=f['range_position']>.40 if f['side']==1 else f['range_position']<.60
        if bad:
            return 'RANGE_ENTRY_NOT_AT_EDGE'
    return 'CONFIRMED'


def independent(rows: list[dict]) -> list[dict]:
    """Non-overlapping decision-to-close episodes, not correlated duplicates."""
    selected=[];end=-1
    for row in sorted(rows,key=lambda r:(r['decision_ts'],r['closed_ts'],r['id'])):
        if row['decision_ts']>end:
            selected.append(row);end=row['closed_ts']
    return selected


def wilson(wins: int, total: int) -> float:
    if not total:
        return 0.
    p=wins/total;z=1.96
    return (p+z*z/(2*total)-z*math.sqrt(p*(1-p)/total+z*z/(4*total*total)))/(1+z*z/total)


def empirical(f: dict, rows: list[dict], now: int, settings: Settings=Settings()) -> dict:
    # Both label availability and the original structural contract are checked.
    past=[r for r in rows if r.get('strategy')==STRATEGY and now-90*86_400_000<=r['closed_ts']<now
          and r['decision_ts']<r['closed_ts'] and r.get('qualified') and math.isfinite(r['net_r'])]
    base=lambda r:r['features']['alignment']==f['alignment'] and r['features']['regime']==f['regime'] and r['features']['side']==f['side'] and r['features'].get('setup_timeframe','15m')==f.get('setup_timeframe','15m') and r['features'].get('entry_route','NATIVE_PATTERN')==f.get('entry_route','NATIVE_PATTERN')
    import trade_dependencies
    exact=independent(trade_dependencies.evidence_rows([r for r in past if base(r) and r['features']['setup']==f['setup']]))
    group=independent(trade_dependencies.evidence_rows([r for r in past if base(r) and r['features']['family']==f['family']]))
    selected=exact if exact else group
    n=len(selected);wins=sum(r['net_r']>0 for r in selected)
    # Continuous shrinkage starts with the first resolved episode. It never
    # turns a target WR, sample count, or uncertainty bound into an entry veto.
    reliability=n/(n+settings.learning_prior_weight)
    return {'observations':n,'wins':wins,'win_rate':wins/n if n else None,
            'estimate':(.5*settings.learning_prior_weight+wins)/(settings.learning_prior_weight+n) if n else None,
            'lower_95':wilson(wins,n) if n else None,
            'expectancy_r':mean(r['net_r'] for r in selected) if n else None,
            'learning_weight':reliability,
            'ranking_adjustment':max(-5.,min(5.,5*reliability*mean(r['net_r'] for r in selected))) if n else 0.,
            'scope':'setup' if selected is exact else 'family',
            'latest_label_ts':max((r['closed_ts'] for r in selected),default=0)}


def assess(plan, snapshot, context, cfg, engine, rows, settings: Settings=Settings()):
    f=features(plan,snapshot,context,cfg,engine);reason=structural_reason(f,settings)
    stat=empirical(f,rows,snapshot.now,settings)
    status='CONTINUOUS_FEEDBACK_NO_WR_VETO'
    return {'accepted':reason=='CONFIRMED','reason':reason,'features':f,'statistics':stat,
            'status':status,'target':settings.target_win_rate,'strategy':STRATEGY,
            'probability_validated':False,'win_rate_gate_enabled':False,
            'ranking_adjustment':stat['ranking_adjustment']}


def calibration_signature(cfg):
    fields = ('instrument','tick','include_commission','maker_fee','taker_fee','slippage_bps',
              'max_hold_minutes','ttl_minutes')
    return digest({name:getattr(cfg,name) for name in fields})


@lru_cache(maxsize=4)
def _read_priors(path, modified, size):
    return json.loads(path.read_text())


def prepare_sizing(state, snapshot, cfg, engine):
    """Freeze training and finalized observations once for this whole scan."""
    import trade_meta_model as model
    from shadow_ledger import LABEL_SCHEMA

    snapshot._execution_calibration = None
    if state.get('calibration_replay'):
        return 'REPLAY_HEURISTIC'
    path = Path(os.environ.get('META_PRIORS_FILE', Path(__file__).with_name('meta_priors.json')))
    try:
        stat = path.stat()
        artifact = _read_priors(path,stat.st_mtime_ns,stat.st_size)
        if (artifact.get('schema') != model.PRIOR_SCHEMA or artifact.get('label_schema') != LABEL_SCHEMA
                or artifact.get('engine_version') != engine.VERSION or artifact.get('instrument') != cfg.instrument
                or artifact.get('signature') != calibration_signature(cfg)):
            return 'INCOMPATIBLE_PRIORS'
        if artifact['validation']['validated'] is not True or not 0 < artifact['data_end_ts'] < snapshot.now:
            return 'UNVALIDATED_PRIORS'
        counts = artifact['counts']
        if '[]' not in counts or not all(math.isfinite(w) and math.isfinite(n) and 0 <= w <= n
                                        for w,n in counts.values()):
            return 'INVALID_PRIORS'
        recent = model.sizing_labels([*state.get('shadow_trades',{}).get('outcomes',()), *state['trades']],
                                     before=snapshot.now, after=artifact['data_end_ts'])
        snapshot._execution_calibration = (counts,model.sizing_counts(recent,as_of=snapshot.now))
        return 'EMPIRICAL_BAYES'
    except FileNotFoundError:
        return 'NO_PRIORS'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return 'INVALID_PRIORS'


def execution_sizing(snapshot, side, entry, regime, *, wave_ts, rvol, room_r, late=False, setup=None):
    """Use the checked posterior when available and preserve the cold-start score."""
    from bzu_execution_rules import range_location
    import trade_meta_model as model

    location = range_location(snapshot, entry)
    aligned = regime.get('side_4h') == side
    counter = regime.get('side_4h') == -side
    premium_discount = location is not None and side * (location - .5) <= 0
    factors = {'htf_4h': aligned, 'premium_discount': premium_discount,
               'fresh_wave': bool(wave_ts and 0 <= snapshot.now-wave_ts <= 4*TF['15m']),
               'confirmation_volume': rvol is not None and rvol >= 1.2-1e-8,
               'liquidity_room': room_r is None or room_r >= 1.35-1e-8}
    weights = (30, 15, 20, 15, 20)
    score = sum(weight for passed, weight in zip(factors.values(), weights) if passed) / 100
    context = model.sizing_context(setup,regime.get('side_4h'),location,snapshot.now)
    calibration = getattr(snapshot,'_execution_calibration',None)
    probability, recent = model.sizing_posterior(*calibration,context) if calibration else (score,0.)
    tier = 1. if probability >= .70 else .65 if probability >= .50 else .35
    return {'meta_prob':probability, 'meta_prob_calibrated':calibration is not None, 'meta_factors':factors,
            'meta_context':context,'meta_heuristic':score,'meta_recent_weight':recent,
            'risk_multiplier':min(tier, .40 if counter else 1., .50 if late else 1.),
            'range_pos':location, 'late_entry':late, 'counter_4h':counter,
            'rejection_required':counter or late or probability < .50}


def confluence(plan, snapshot, proof, cfg, engine):
    """Score reversal and trend evidence independently."""
    import directional_policy as direction
    import ict_methodology
    import setup_scanner as scanner

    zone = plan.get('entry_zone', {})
    origin = plan.get('zone_origin', {})
    fresh = (zone.get('high', 0) > zone.get('low', 0)
             and (scanner.market_plan(plan) or not plan.get('zone_audit', {}).get('mitigated'))
             and (origin.get('confirmed') or 'FVG' in zone.get('kind', '')
                  or 'OB' in zone.get('kind', '')))
    sweeps = ict_methodology.session_sweeps(snapshot, plan['side'], cfg.tick, engine)
    if origin.get('sweep', {}).get('pattern') == 'ENGULFING':
        sweeps = [*sweeps, origin['sweep']]
    sweep = bool(origin.get('sweep') or proof.get('sweep_then_shift') or sweeps
                 or (proof.get('sweep_reclaim') and proof.get('level_rejection')))
    bos = bool((origin.get('confirmed') and origin.get('break_known_ts'))
               or proof.get('displacement_bos_fvg'))
    aligned = plan.get('m15_momentum_aligned')
    if aligned is None:
        aligned = direction.momentum_aligned(snapshot, plan['side'])
    relation = plan.get('trend_rel')
    regime_side = direction.regime_side(plan.get('market_regime', {}))
    aligned = aligned or regime_side == plan['side']
    reversal = {'base': 25, 'liquidity_reversal': 30 * int(sweep),
                'fresh_zone': 15 * int(fresh)}
    trend = {'base': 25, 'structure_break': 25 * int(bos),
             'm15_momentum': 15 * int(aligned),
             'fresh_zone': 10 * int(fresh and bos)}
    routes = [('REVERSAL_SWEEP', reversal, sweep and fresh),
              ('ORGANIC_TREND', trend, bos and aligned and fresh)]
    route, components, trigger = max(routes, key=lambda item: (item[2], sum(item[1].values())))
    if plan.get('structure_reclaim') and fresh and bos:
        route = 'STRUCTURAL_RECLAIM'
        components = {'base': 25, 'structure_break': 25, 'retest_confirmed': 24, 'fresh_zone': 10}
        trigger = True
    if fresh and plan.get('entry_confirmation'):
        route = 'M15_REJECTION_CONFIRMED'
        components = {'base':25, 'confirmed_rejection':30, 'fresh_zone':15}
        trigger = True
    volume = volume_footprint(plan, snapshot, proof, sweeps)
    parts = {**components, 'relative_volume_bonus': volume['bonus'],
             'proximity_bonus': scanner.proximity_bonus(
                 plan['side'], snapshot.price, plan.get('entry', snapshot.price), regime_side == plan['side'])}
    if relation == 'WITH_TREND':
        parts['trend_pullback'] = 12 * int((plan.get('trend_phase') or '').endswith('PULLBACK_OPPORTUNITY'))
    if relation == 'COUNTER_TREND':
        parts['confirmed_choch_m5'] = 12 * int(bool(plan.get('counter_confirmation')))
    if trigger and plan.get('reversal_exhaustion_bonus'):
        parts['adr_exhaustion_reversal'] = plan['reversal_exhaustion_bonus']
    if not trigger:
        parts = {'base': 25}
    vwap = None
    if plan.get('institutional_family'):
        vwap = direction.session_vwap(snapshot)
        aligned_vwap = (vwap['value'] is not None and
                        plan['side'] * (snapshot.price - vwap['value']) > 0)
        evidence = [e['rvol'] for e in volume.get('events', []) if e.get('rvol') is not None]
        market_trigger = plan.get('market_trigger') or {}
        is_m5 = (market_trigger.get('pattern') in ('M5_ZONE_ENGULFING','M5_SNIPER_REVERSAL')
                 or zone.get('kind') == scanner.wave.SNIPER_REVERSAL)
        recent = scanner.closed_five_minutes(snapshot) if is_m5 else direction.closed(snapshot, '15m')
        if recent:
            timeframe, step = ('5m', 300_000) if is_m5 else ('15m', 900_000)
            recent_volume = scanner.event_rvol(snapshot, timeframe, recent[-1].ts + step)
            if recent_volume['rvol'] is not None:
                evidence.append(recent_volume['rvol'])
        relative_volume = max(evidence, default=0.)
        vwap_volume = aligned_vwap and relative_volume >= 1.25
        vwap.update(aligned=aligned_vwap, relative_volume=relative_volume, bonus=12 if vwap_volume else 0)
        valid_risk = (plan.get('risk', 0) > 0 and
                      plan.get('rr1', 0) >= scanner.oil_risk.ENTRY_TP1_MIN - 1e-8)
        volatility_evidence = bool(recent and (bos or sweep))
        hour = snapshot.now // 3_600_000 % 24
        parts = {'structure': 25 * int(trigger),
                 'zone_and_vwap': 13 * int(fresh) + vwap['bonus'],
                 'trade_geometry': 25 * int(valid_risk),
                 'impulse_volume_session': (10 * int(volatility_evidence)
                                            + 10 * int(relative_volume >= 1.25)
                                            + 5 * int(7 <= hour < 21))}
        trigger = trigger and valid_risk
    decay = consolidation_decay(zone, snapshot, plan['side'], cfg.tick, engine)
    parts['consolidation_decay'] = -decay['penalty']
    if decay['requires_confirmation'] and (not scanner.market_plan(plan) or
            (plan.get('market_trigger') or {}).get('pattern') == 'INSTANT_TOUCH_MARKET_FILL'):
        trigger = False
    score = max(0, min(100, sum(parts.values())))
    threshold = (scanner.min_plan_score(relation, plan.get('market_trigger'),structural=True)
                 if scanner.oil_risk.is_organic(plan) else
                 scanner.min_plan_score(relation, plan.get('market_trigger')) if relation else max(60, cfg.min_score))
    active = plan.get('market_regime', {}).get('active_structure', {}).get('side')
    countertrend = active != plan['side'] and any(
        plan.get(key) == -plan['side'] for key in ('htf_1h', 'htf_4h'))
    return {'score': score, 'zone_decay': decay, 'components': parts, 'route': route,
            'route_scores': {name: sum(points.values()) for name, points, _ in routes},
            'volume_footprint': volume, 'session_vwap': vwap, 'accepted': trigger and score >= threshold,
            'rating': 'A+' if score >= 82 else 'A' if relation == scanner.REVERSAL_RELATION and score >= 75 else 'B',
            'trigger_confirmed': trigger,
            'reason': ('ICT_SWEEP_OR_BOS_FVG_REQUIRED' if not trigger else
                       'CONFIRMED' if score >= threshold else 'CONFLUENCE_BELOW_THRESHOLD'),
            'countertrend': countertrend, 'session_sweep_priority': bool(sweeps and fresh),
            'session_sweeps': sweeps, 'threshold': threshold, 'probability': None,
            'model_veto_enabled': False}


def volume_footprint(plan, snapshot, proof, session_sweeps):
    """A forming impulse above 1.35 SMA adds six points."""
    import setup_scanner as scanner
    events=[]
    origin=plan.get('zone_origin',{})
    tf=origin.get('timeframe',plan.get('formation_timeframe','15m'))
    if origin.get('confirmed'):
        for key,kind in (('displacement_known_ts','FORMATION'),('break_known_ts','BOS')):
            if origin.get(key) is not None:
                events.append((tf,origin[key],kind))
    proof_tf=proof.get('timeframe',plan.get('formation_timeframe','15m'))
    if proof.get('sweep_reclaim') and proof.get('latest_sweep'):
        events.append((proof_tf,proof['latest_sweep']['known_ts'],'SWEEP'))
    for event in session_sweeps:
        events.append(('15m',event['sweep_ts'],'SWEEP'))
    if not origin.get('confirmed') and proof.get('structure_shift') and proof.get('latest_structure_shift'):
        events.append((proof_tf,proof['latest_structure_shift']['known_ts'],'BOS'))
    evidence=[{**scanner.event_rvol(snapshot,timeframe,known),'kind':kind}
              for timeframe,known,kind in dict.fromkeys(events)]
    zone = plan.get('entry_zone') or {}
    formation = scanner.formation_volume(zone, snapshot)
    bonus = 6 if formation['confirmed'] else 0
    return {'bonus': bonus, 'penalty': 0, 'events': evidence,
            'status': 'AVAILABLE' if any(e['rvol'] is not None for e in evidence) else 'NO_EVENT_VOLUME'}




PLAN_FIELDS=('institutional_family','scale_policy','organic_confluence','origin_broke_structure','zone_freshness','m15_momentum_aligned','session_vwap','round_level_protection','policy','entry_zone','zone_origin','breakeven_trigger_r','breakeven_price','structural_trail_trigger_r','tp1_alignment','tp2_alignment','organic_risk','formation_volume','tp1_source','tp2_source','target_projection','reversal_override','reversal_sweep_override','local_reversal_override','reversal_exhaustion_bonus','structure_reclaim','continuation_entry','linked_reversal_zone','entry_basis','entry_condition','reaction_only','rr1','rr2','rr3',
             'approach','market_regime','ict_evidence','confluence','trend_rel','trend_phase','counter_confirmation','entry_max_distance','order_type','market_trigger','checkpoints',
             'entry_legs','action_type','execution_timeframe','id','order_id','event_key','zone_key','side','instrument','setup','setup_type',
             'setup_family','canonical_setup_family','entry','limit_price','stop','initial_stop',
             'tp','tp0','tp1','tp2','tp3','risk','partials','placed_ts','effective_ts','expires_ts',
             'invalidation','htf_1h','htf_4h','score','net_rr','gross_rr','costs_r','timeframe',
             'zone_created_ts','last_bar','execution','status','target_cancel_armed','version',
             'missed_target_fraction','limit_ttl_ms','entry_confirmation','risk_multiplier','meta_prob','late_entry',
             'setup_timeframe','micro_timeframe','formation_timeframe','formation_micro_timeframe','entry_route','queue_execution_policy','pattern_proof','entry_policy','detector_confirmed',
             'max_hold_minutes','intraday','time_exit_enabled','post_only','entry_retest_reason','include_commission','entry_fee_rate','exit_fee_rate','slippage_bps','exit_policy','full_exit_target','full_exit_candidates','exit_selection','target_management','exit_path_barriers','tp1_decision_gate')


def ensure(state: dict, cfg) -> dict:
    q=state.setdefault('quality',{'schema':SCHEMA,'strategy':STRATEGY,'signature':signature(cfg),
                                'pending':[],'observations':[],'seen':{},'last_run_ts':0})
    if q.get('schema')==SCHEMA and q.get('strategy') in ('bzu-full-position-v14.4','bzu-m15-displacement-structure-v14','bzu-m15-organic-momentum-v13','bzu-master-rvol-vwap-round-5m-v12','bzu-precision-confirmed-single-v11','bzu-checklist-confluence-split-v10','native-mtf-causal-direction-control-v9','confirmed-ict-quality-v1','native-mtf-pretouch-limit-v2','native-mtf-single-scenario-no-commission-v3','native-mtf-causal-episodes-fresh-targets-v4','native-mtf-full-lifecycle-continuous-learning-v5','native-mtf-adaptive-full-exit-open-cap-two-v6','native-mtf-directional-limit-paths-v7','native-mtf-anticipatory-execution-atlas-v8'):
        # Keep all v12.3 evidence, but do not infer win rates for a new entry
        # policy from the old late-confirmation policy.
        state.setdefault('quality_archives',[]).append(copy.deepcopy(q))
        q={'schema':SCHEMA,'strategy':STRATEGY,'signature':signature(cfg),
           'pending':[],'observations':[],'seen':{},'last_run_ts':0}
        state['quality']=q
    if q.get('schema')!=SCHEMA or q.get('strategy')!=STRATEGY:
        raise ValueError('Unknown quality ledger; preserve it instead of resetting')
    if q.get('signature')!=signature(cfg):
        # Keep evidence from the old cost contract, but never train on it.
        state.setdefault('quality_archives',[]).append(copy.deepcopy(q))
        q={'schema':SCHEMA,'strategy':STRATEGY,'signature':signature(cfg),
           'pending':[],'observations':[],'seen':{},'last_run_ts':0}
        state['quality']=q
    return q


def seed(state: dict, cfg, path: Path) -> None:
    if not path.is_file():
        return
    artifact=json.loads(path.read_text())
    if not isinstance(artifact,dict) or artifact.get('schema')!=SCHEMA or artifact.get('strategy')!=STRATEGY or artifact.get('signature')!=signature(cfg):
        raise ValueError('Quality model does not match strategy/instrument/cost settings')
    if 'settings' in artifact and artifact['settings']!=asdict(Settings()):
        raise ValueError('Quality model confirmation settings differ')
    if artifact.get('artifact_id') and artifact['artifact_id']!=digest({k:v for k,v in artifact.items() if k!='artifact_id'}):
        raise ValueError('Quality model checksum differs')
    q=ensure(state,cfg)
    key=artifact.get('artifact_id') or digest(artifact)
    if q.get('seed_id')==key:
        return
    rows=artifact.get('observations',[])
    try:
        valid=isinstance(rows,list) and isinstance(artifact['trained_until_ts'],int)
        for r in rows:
            f=r['features'];family,_=CONTRACTS[f['setup']]
            valid=valid and (r['strategy']==STRATEGY and isinstance(r['id'],str)
                 and isinstance(r['qualified'],bool) and 0<r['decision_ts']<r['closed_ts']<=artifact['trained_until_ts']
                 and math.isfinite(r['net_r']) and f['family']==family and f['side'] in (-1,1)
                 and f['alignment'] in ('neutral','mixed','aligned','against') and isinstance(f['regime'],str))
    except (KeyError,TypeError,ValueError):
        valid=False
    if not valid:
        raise ValueError('Invalid or future-labelled model observation')
    known={r['id']:r for r in q['observations']}
    known.update({r['id']:r for r in rows})
    q['observations']=sorted(known.values(),key=lambda r:r['closed_ts'])[-Settings().maximum_observations:]
    q['seed_id']=key


def watch_candidates(state: dict, candidates: list[dict], assessments: dict, cfg) -> None:
    q=ensure(state,cfg);settings=Settings()
    for plan in candidates:
        decision=assessments[plan['id']]
        # All proposed plans are observed, including structural/statistical rejects.
        # Parallel votes at one price are one episode, not 24 independent wins.
        key=engine_key(plan,cfg)
        if key in q['seen']:
            continue
        if len(q['pending'])>=settings.maximum_shadow_plans:
            q['unobserved_due_capacity']=q.get('unobserved_due_capacity',0)+1
            continue
        lean={k:copy.deepcopy(plan[k]) for k in PLAN_FIELDS if k in plan}
        lean.update(features=decision['features'],qualified=structural_reason(decision['features'])=='CONFIRMED',
                    strategy=STRATEGY,decision_ts=plan['placed_ts'])
        q['pending'].append({'pending':lean,'active':None})
        q['seen'][key]=plan['placed_ts']
    cutoff=max((p['placed_ts'] for p in candidates),default=q['last_run_ts'])-2*86_400_000
    q['seen']={key:ts for key,ts in q['seen'].items() if ts>=cutoff}


def refresh_confirmations(state: dict, snapshot, context: dict, cfg, engine, contexts=None) -> None:
    """Record a later *pre-fill* confirmation, effective only for future bars.

    The original decision stays in the audit trail. A confirmation after the
    entry cannot upgrade a losing observation retroactively.
    """
    q=ensure(state,cfg)
    for item in q['pending']:
        plan=item.get('pending')
        if not plan or plan.get('qualified') or plan.get('confirmed_ts'):
            continue
        if snapshot.now >= plan['expires_ts']:
            continue
        import setup_scanner as scanner
        native=(contexts or {}).get('MICRO_PRECURSOR' if plan.get('entry_route')=='MICRO_PRECURSOR' else scanner.timeframe(plan),context)
        f=features(plan,snapshot,native,cfg,engine)
        if structural_reason(f)!='CONFIRMED':
            continue
        # Start an independent future-only PAPER episode; no retrospective fill.
        plan['features']=f
        plan['qualified']=True
        plan['confirmed_ts']=snapshot.now
        plan['decision_ts']=snapshot.now
        plan['effective_ts']=((snapshot.now+TF['3m']-1)//TF['3m'])*TF['3m']
        item.setdefault('confirmation_history',[]).append({'ts':snapshot.now,'source':'CLOSED_BARS_ONLY'})


def engine_key(plan,cfg):
    return digest([plan['side'],round(plan['entry']/cfg.tick),plan.get('zone_created_ts',plan['placed_ts'])//900_000])


def advance(state: dict, snapshot, cfg, engine) -> None:
    q=ensure(state,cfg)
    if snapshot.now<=q['last_run_ts']:
        return
    if q['pending'] and q['last_run_ts']:
        # Never silently skip bars for a counterfactual, or fabricate a label.
        fake={'last_run_ts':q['last_run_ts']}
        if engine.execution_health(snapshot,fake):
            q.setdefault('incomplete_intervals',[]).append({'from':q['last_run_ts'],'to':snapshot.now})
            q['incomplete_intervals']=q['incomplete_intervals'][-50:]
            q['pending']=[]
    remaining=[]
    # All counterfactuals see the same current HTF facts and historical interval.
    b1=b4=0
    if q['pending']:
        import execution_atlas
        b1=direction(execution_atlas.snapshot_control(snapshot,'1H',cfg,engine)['side'])
        b4=direction(execution_atlas.snapshot_control(snapshot,'4H',cfg,engine)['side'])
    tf,step=engine.execution_series(snapshot)
    checkpoint=(q['last_run_ts']//step)*step
    from dataclasses import replace
    lifecycle=replace(snapshot,candles={**snapshot.candles,tf:[c for c in snapshot.candles.get(tf,[]) if c.confirmed and c.ts>=checkpoint and c.ts+step<=snapshot.now]})
    for item in q['pending']:
        book={'pending':item.get('pending'),'active':item.get('active'),'reconciliation':None,
              'orders':[],'trades':[],'events':[],'used_events':{},'last_run_ts':q['last_run_ts']}
        engine._advance_slot_lifecycle(book,lifecycle,cfg)
        engine._amend_slot_at_report(book,snapshot,cfg)
        # Keep the same report-time cancellation policy as selected plans.
        pending=book.get('pending')
        if pending:
            engine.review_waiting_conditions(book,pending,snapshot,cfg,b1,b4)
        for trade in book['trades'] if not q.get('freeze_observations') else []:
            q['observations'].append({'id':trade['id'],'strategy':STRATEGY,'features':trade['features'],
                                      'qualified':trade['qualified'],'decision_ts':trade['decision_ts'],
                                      'closed_ts':trade['closed_ts'],'net_r':trade['net_r'],
                                      'conservative_ambiguity':trade.get('ambiguous_ohlc',False),
                                      'entry':trade['entry'],'risk':trade['risk'],'side':trade['side'],
                                      'filled_ts':trade.get('filled_ts'),'instrument':trade.get('instrument','')})
        if book.get('pending') or book.get('active'):
            remaining.append({'pending':book.get('pending'),'active':book.get('active')})
    q['pending']=remaining
    q['observations']=q['observations'][-Settings().maximum_observations:]
    q['last_run_ts']=snapshot.now


def validation_status(trades: list[dict], independent_trades: list[dict]|None=None) -> dict:
    values=[t['net_r'] for t in trades if t.get('net_r') is not None]
    n=len(values);wins=sum(v>0 for v in values)
    independent_values=[t['net_r'] for t in independent_trades] if independent_trades is not None else values
    lower=wilson(sum(v>0 for v in independent_values),len(independent_values))
    expectation=mean(values) if values else None
    observed_goal_met=(wins/n>=.70) if n else False
    confidence_support=bool(independent_values) and lower>=.70 and expectation is not None and expectation>0
    return {'target':.70,'closed_trades':n,'observed_win_rate':wins/n if n else None,
            'independent_trades':len(independent_values),'wilson_lower_95':lower,
            'expectancy_r':expectation,'observed_goal_met':observed_goal_met,
            'target_confirmed_on_this_period':confidence_support,'entry_gate_enabled':False,
            'status':'OBSERVED_GOAL_MET_NOT_A_GUARANTEE' if observed_goal_met else 'GOAL_NOT_YET_MET',
            'future_performance_guaranteed':False}


def consolidation_decay(zone, snapshot, side, tick, engine):
    """Count contiguous closed M5 tests and require a fresh departure/retest."""
    from setup_scanner import closed_five_minutes

    low, high = zone.get('source_low', zone.get('low')), zone.get('source_high', zone.get('high'))
    if low is None or high is None:
        return {'penalty': 0, 'requires_confirmation': False, 'bars': 0}
    bars = [bar for bar in closed_five_minutes(snapshot)
            if bar.ts >= (zone.get('known_ts') or snapshot.now)]
    band = max(2 * tick, .10 * engine.atr(closed_rows(snapshot, '15m')))
    run = longest = 0
    previous = None
    for bar in bars:
        near = bar.low <= high + band and bar.high >= low - band
        run = (run + 1 if previous is not None and bar.ts == previous + 300_000 else 1) if near else 0
        longest = max(longest, run)
        previous = bar.ts
    confirmed = False
    if longest >= 12 and len(bars) >= 2:
        first, last = bars[-2:]
        edge = high if side == 1 else low
        confirmed = (last.ts == first.ts + 300_000 and snapshot.now - last.ts - 300_000 < 300_000
                     and side * (first.close - edge) > band
                     and last.low <= high + band and last.high >= low - band
                     and side * (last.close - edge) > tick
                     and side * (last.close - last.open) > tick)
    return {'penalty': 15 if longest >= 15 else 10 if longest >= 12 else 0,
            'requires_confirmation': longest >= 12 and not confirmed,
            'confirmed': confirmed, 'bars': longest}
