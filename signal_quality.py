"""Causal confirmation and empirical, past-only selection for all 24 setups.

No score is converted into a probability. Counterfactual PAPER observations
never occupy portfolio slots and never appear in the executed trade ledger.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

SCHEMA = 'ict_quality_v1'
STRATEGY = 'native-mtf-directional-limit-paths-v7'
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
    micro_tf=scanner.MICRO[source_tf]
    side=plan['side'];c3=closed_rows(snapshot,micro_tf);c15=closed_rows(snapshot,source_tf)
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
            'entry_policy':plan.get('entry_policy','LEGACY_CONFIRMED'),
            'detector_confirmed':plan.get('detector_confirmed',False),
            'htf_opposition':opposition,'maximum_return_atr':cfg.max_entry_distance_atr,
            'regime':str(context.get('regime','UNKNOWN')),'alignment':align,
            'side3':side3,'side15':side15,'shift3':shift3,'shift15':shift15,
            'impulse':impulse,'acceptance':accept,'range_position':round(location,4),
            'shock':shock,'distance_atr':round(side*(snapshot.price-plan['entry'])/a15,4),
            'cost_fraction':round(plan.get('costs_r',0)/max(plan.get('gross_rr',0),1e-9),4),
            'as_of_ts':snapshot.now,'strategy':STRATEGY}


COUNTERTREND_SETUPS=frozenset({'SWEEP_RECLAIM','CAPITULATION_RECOVERY','RANGE_EDGE_REVERSAL',
    'FAILED_AUCTION_REJECTION','LIQUIDITY_SWEEP_REVERSAL_SHORT','BUYER_EXHAUSTION_SHORT',
    'FAILED_BREAKOUT_SHORT','MSS_REVERSAL_SHORT','FAILED_OPENING_RANGE_BREAKOUT',
    'OR_FAILURE_2_SHORT','DIRECTION_FLIP_15M'})


def pretouch_countertrend_allowed(setup,side,primary_side,micro_side):
    return setup in COUNTERTREND_SETUPS and primary_side==side and micro_side==side


def structural_reason(f: dict, settings: Settings=Settings()) -> str:
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
        if f['htf_opposition']==2 and not pretouch_countertrend_allowed(f['setup'],f['side'],f['side15'],f['side3']):
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
    base=lambda r:r['features']['alignment']==f['alignment'] and r['features']['regime']==f['regime'] and r['features']['side']==f['side'] and r['features'].get('setup_timeframe','15m')==f.get('setup_timeframe','15m')
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


PLAN_FIELDS=('id','order_id','event_key','zone_key','side','instrument','setup','setup_type',
             'setup_family','canonical_setup_family','entry','limit_price','stop','initial_stop',
             'tp','tp0','tp1','tp2','tp3','risk','partials','placed_ts','effective_ts','expires_ts',
             'invalidation','htf_1h','htf_4h','score','net_rr','gross_rr','costs_r','timeframe',
             'zone_created_ts','last_bar','execution','status','target_cancel_armed','version',
             'setup_timeframe','micro_timeframe','entry_policy','detector_confirmed',
             'max_hold_minutes','intraday','include_commission','entry_fee_rate','exit_fee_rate','slippage_bps','exit_policy','full_exit_target','full_exit_candidates','exit_selection','target_management','exit_path_barriers','tp1_decision_gate')


def ensure(state: dict, cfg) -> dict:
    q=state.setdefault('quality',{'schema':SCHEMA,'strategy':STRATEGY,'signature':signature(cfg),
                                'pending':[],'observations':[],'seen':{},'last_run_ts':0})
    if q.get('schema')==SCHEMA and q.get('strategy') in ('confirmed-ict-quality-v1','native-mtf-pretouch-limit-v2','native-mtf-single-scenario-no-commission-v3','native-mtf-causal-episodes-fresh-targets-v4','native-mtf-full-lifecycle-continuous-learning-v5','native-mtf-adaptive-full-exit-open-cap-two-v6'):
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
        native=(contexts or {}).get(scanner.timeframe(plan),context)
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
        b1=direction(cfg.core.structure_snapshot(closed_rows(snapshot,'1H'),60,2)['direction'])
        b4=direction(cfg.core.structure_snapshot(closed_rows(snapshot,'4H'),60,2)['direction'])
    checkpoint=(q['last_run_ts']//TF['3m'])*TF['3m']
    from dataclasses import replace
    lifecycle=replace(snapshot,candles={**snapshot.candles,'3m':[c for c in closed_rows(snapshot,'3m') if c.ts>=checkpoint]})
    for item in q['pending']:
        book={'pending':item.get('pending'),'active':item.get('active'),'reconciliation':None,
              'orders':[],'trades':[],'events':[],'used_events':{},'last_run_ts':q['last_run_ts']}
        engine._advance_slot_lifecycle(book,lifecycle,cfg)
        engine._amend_slot_at_report(book,snapshot,cfg)
        # Keep the same report-time cancellation policy as selected plans.
        pending=book.get('pending')
        if pending:
            opposite=-pending['side']
            if pending['side']*(snapshot.price-pending['tp'])<0:
                pending['target_cancel_armed']=True
            if cfg.cancel_on_bias_flip and b1==b4==opposite and not (pending.get('htf_1h')==opposite and pending.get('htf_4h')==opposite):
                engine.request_cancel(book,pending,'CONFIRMED_HTF_FLIP',snapshot.now)
            elif pending['side']*(snapshot.price-pending['invalidation'])<=0:
                engine.request_cancel(book,pending,'STRUCTURE_INVALIDATED_NOW',snapshot.now)
            elif pending.get('target_cancel_armed',True) and pending['side']*(snapshot.price-pending['tp'])>=0:
                engine.request_cancel(book,pending,'TARGET_REACHED_WITHOUT_FILL',snapshot.now)
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
