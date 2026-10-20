"""Resting-limit and complete-position scenarios, using the real lifecycle.

Block resampling is an uncalibrated scenario model, never a promised win rate.
Fees and a stated carry budget are included even when a legacy ledger is gross.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import replace
from statistics import mean

SCHEMA = 'limit_tp1_gated_full_volume_scenarios_v3'
PATHS = 96
LOOKBACK = 240
MINIMUM_BARS = 30


def economic_config(cfg):
    # from_env historically zeroed fee fields when the old gross ledger was
    # requested. Economic scenarios always disclose their fallback assumption.
    return replace(cfg, include_commission=True,
                   maker_fee=cfg.maker_fee if cfg.include_commission else (cfg.maker_fee or .0002),
                   taker_fee=cfg.taker_fee if cfg.include_commission else (cfg.taker_fee or .0005))


def simulate(plan, rows, cfg, engine, review_ms=None):
    """Exact deterministic OHLC path evaluator, shared with behavioral tests."""
    cfg = economic_config(cfg)
    review_ms=review_ms or cfg.analysis_scan_minutes*60000
    keys = ('id','order_id','event_key','zone_key','instrument','side','setup','setup_type',
            'timeframe','setup_timeframe','entry','initial_stop','stop','risk','tp','tp0','tp1','tp2','tp3',
            'partials','placed_ts','effective_ts','expires_ts','last_bar','invalidation',
            'max_hold_minutes','intraday','version','target_cancel_armed','exit_policy','full_exit_target','full_exit_candidates','target_management','exit_path_barriers','tp1_decision_gate')
    pending = {k: copy.deepcopy(plan[k]) for k in keys if k in plan}
    engine.use_full_exit(pending,plan['placed_ts'])
    pending.update(include_commission=True, entry_fee_rate=cfg.maker_fee,
                   exit_fee_rate=cfg.taker_fee, slippage_bps=cfg.slippage_bps)
    book = {'pending':pending,'active':None,'reconciliation':None,'orders':[],
            'trades':[],'events':[],'used_events':{},'last_run_ts':plan['placed_ts']}
    next_review = plan['placed_ts']+review_ms
    tail = []
    last_ts=plan['placed_ts']
    for c in rows:
        snap = engine.Snapshot(c.ts+engine.TF['3m'],c.close,{'3m':[c]})
        last_ts=snap.now
        engine._advance_slot_lifecycle(book,snap,cfg)
        tail = (tail+[c])[-25:]
        if snap.now >= next_review:
            snap.candles['3m'] = tail
            engine._amend_slot_at_report(book,snap,cfg)
            pending = book.get('pending')
            if pending:
                # Future HTF facts are unavailable in these 3m-only scenarios.
                engine.review_waiting_conditions(book,pending,snap,cfg)
            next_review = (snap.now//review_ms+1)*review_ms
        if book['trades'] or not (book.get('pending') or book.get('active')):
            break
    if book.get('active') or book.get('reconciliation'):
        # Caller must provide the predeclared full horizon, including the last
        # close. No unresolved active path is silently counted as a winner.
        return {'status':'CENSORED','filled':bool(book.get('active'))}
    if book['trades']:
        trade = book['trades'][0]
        exposure_ms=sum(leg['fraction']*(leg['ts']-trade['filled_ts'])
                        for leg in trade.get('realized_legs',[]))
        exposure_ms+=trade['final_exit_fraction']*(trade['closed_ts']-trade['filled_ts'])
        carry_r = trade['entry']*.0002*(exposure_ms/86400000)/trade['risk']
        exits = sum(leg['fraction']*leg['price'] for leg in trade.get('realized_legs',[]))
        exits += trade['final_exit_fraction']*trade['exit']
        slip=cfg.slippage_bps/10000
        raw_exits=exits/(1-trade['side']*slip)
        extra_slip_r = raw_exits*slip/trade['risk']
        stress_exits=exits-trade['side']*raw_exits*slip
        stress_fees=(trade['entry']*2*cfg.maker_fee+stress_exits*2*cfg.taker_fee)/trade['risk']
        net = trade['net_r']-carry_r
        return {'status':'CLOSED','filled':True,'net_r':net,'ledger_net_r':trade['net_r'],
                'fees_r':trade['fees_r'],'carry_budget_r':carry_r,
                'stress_net_r':net-(stress_fees-trade['fees_r'])-extra_slip_r-carry_r,
                'occupied_minutes':(trade['closed_ts']-plan['placed_ts'])/60000,
                'tp1_hit':bool(trade.get('tp1_hit')),'close_reason':trade['close_reason'],
                'remaining':trade['remaining'],'realized_legs':trade.get('realized_legs',[]),
                'ambiguous_ohlc':trade.get('ambiguous_ohlc',False)}
    return {'status':'UNFILLED' if not book.get('pending') else 'CENSORED',
            'filled':False,'net_r':0. if not book.get('pending') else None,
            'occupied_minutes':(last_ts-plan['placed_ts'])/60000}


def forecast(plan, snapshot, cfg, engine, paths=PATHS):
    rows = [c for c in snapshot.candles.get('3m',[]) if c.confirmed and c.ts+engine.TF['3m']<=snapshot.now][-LOOKBACK:]
    base = {'schema':SCHEMA,'calibrated':False,'target':'FULL_TRADE_NET_AFTER_COSTS',
            'exit_policy':'SINGLE_FULL_EXIT','eligibility_assumption':'FREE_POSITION_CAPACITY_AND_ENTRY_PRIORITY','empirical_win_rate':None,'as_of_ts':snapshot.now,'closed_bars':len(rows),
            'source_timeframe':'3m','fee_assumption':{'maker':economic_config(cfg).maker_fee,
              'taker':economic_config(cfg).taker_fee,'slippage_bps':cfg.slippage_bps,
              'carry_budget_bps_per_day':2.,'actual_funding_known':False}}
    if len(rows)<MINIMUM_BARS:
        return {**base,'status':'NEEDS_CLOSED_HISTORY'}
    if plan.get('side') not in (-1,1) or any(not isinstance(plan.get(k),(int,float))
            or not math.isfinite(plan[k]) or plan[k]<=0 for k in ('entry','stop','tp','risk','expires_ts')):
        return {**base,'status':'INVALID_PLAN'}
    start = ((snapshot.now+engine.TF['3m']-1)//engine.TF['3m'])*engine.TF['3m']
    if plan['expires_ts']<=start:
        return {**base,'status':'NO_REMAINING_LIMIT_LIFETIME'}
    hold = plan.get('max_hold_minutes',cfg.max_hold_minutes)*60000
    steps = math.ceil((plan['expires_ts']+hold-start)/engine.TF['3m'])+1
    if steps>3000 or paths<1:
        return {**base,'status':'UNSUPPORTED_HORIZON'}
    lean = {k:v for k,v in plan.items() if k in ('side','entry','stop','initial_stop','risk','tp','tp0',
              'tp1','tp2','tp3','partials','full_exit_target','target_management','tp1_decision_gate','exit_path_barriers','full_exit_candidates','expires_ts','max_hold_minutes','intraday','invalidation')}
    payload = {'plan':lean,'price':snapshot.price,'start':start,'paths':paths,
               'bars':[(c.ts,c.open,c.high,c.low,c.close) for c in rows],
               'costs':base['fee_assumption'],'review_ms':cfg.analysis_scan_minutes*60000,
               'version':engine.VERSION,'schema':SCHEMA}
    fingerprint = hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    previous = plan.get('outcome_forecast') or {}
    if previous.get('fingerprint') == fingerprint:
        return previous
    vectors = [(c.open/p.close,c.high/c.open,c.low/c.open,c.close/c.open)
               for p,c in zip(rows,rows[1:]) if c.ts-p.ts==engine.TF['3m']]
    if len(vectors)<MINIMUM_BARS-1:
        return {**base,'status':'NEEDS_CONTIGUOUS_HISTORY'}
    # Common random numbers: alternative full-exit targets see EXACTLY the
    # same prices. The selected target changes execution, never the sampled path.
    common=copy.deepcopy(payload)
    for key in ('tp','partials','full_exit_target'):
        common['plan'].pop(key,None)
    scenario_seed=hashlib.sha256(json.dumps(common,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    rng = random.Random(int(scenario_seed[:16],16))
    outcomes = []
    def path():
        price=snapshot.price;remaining=0;index=0
        for step in range(steps):
            if not remaining:
                index=rng.randrange(len(vectors));remaining=8
            gap,hr,lr,cr=vectors[index]
            opening=price*gap;price=opening*cr
            yield engine.Candle(start+step*engine.TF['3m'],opening,opening*hr,opening*lr,price,0.,True)
            index=(index+1)%len(vectors);remaining-=1
    for _ in range(paths):
        current={**plan,'placed_ts':snapshot.now,'effective_ts':start,'last_bar':0}
        outcomes.append(simulate(current,path(),cfg,engine))
    completed=[o for o in outcomes if o['status']!='CENSORED']
    filled=[o for o in completed if o['filled']]
    values=[o['net_r'] for o in filled]
    unresolved=len(completed)!=paths
    observed_fills=sum(bool(o.get('filled')) for o in outcomes)
    return {**base,'status':'MODEL_ONLY' if len(completed)==paths else 'MODEL_INCOMPLETE',
            'review_interval_minutes':cfg.analysis_scan_minutes,
            'fingerprint':fingerprint,'scenario_seed':scenario_seed,'paths':paths,
            'completed_paths':len(completed),'censored_paths':paths-len(completed),
            'fill_probability':observed_fills/paths,
            'filled_unresolved_paths':sum(bool(o.get('filled')) for o in outcomes if o['status']=='CENSORED'),
            'net_win_probability_given_fill':sum(v>0 for v in values)/len(values) if values and not unresolved else None,
            'net_win_probability_per_placed':sum(v>0 for v in values)/len(completed) if completed and not unresolved else None,
            'expectancy_r_per_placed':sum(values)/len(completed) if completed and not unresolved else None,
            'expectancy_r_given_fill':mean(values) if values and not unresolved else None,
            'expected_occupied_minutes':mean(o['occupied_minutes'] for o in completed) if completed and not unresolved else None,
            'stress_expectancy_r_per_placed':sum(o['stress_net_r'] for o in filled)/len(completed) if completed and not unresolved else None,
            'downside_r_given_fill':-mean(min(0.,v) for v in values) if values and not unresolved else None,
            'tp1_probability_given_fill':sum(o['tp1_hit'] for o in filled)/len(filled) if filled and not unresolved else None,
            'limitations':['Uncalibrated 3m block scenarios with fixed current context',
                           'Queue priority is conditional on eligibility; future HTF flips and funding are unknown',
                           'TP1 starts a full-volume review; unresolved paths at the horizon are censored',
                           'Carry is a disclosed cost budget; stress doubles fees, slippage and carry',
                           'SL first when same-candle ordering is ambiguous; exact touches do not fill']}


def decision_adjustment(model):
    if model.get('status')!='MODEL_ONLY' or model.get('expectancy_r_per_placed') is None:
        return 0.
    edge=model['expectancy_r_per_placed']
    stress=model.get('stress_expectancy_r_per_placed') or 0.
    downside=model.get('downside_r_given_fill') or 0.
    # Modest fixed opportunity cost for time until a placed plan resolves;
    # no WR/fill-probability threshold is applied.
    capacity_cost=.002*(model.get('expected_occupied_minutes') or 0.)/60
    return max(-12.,min(12.,10*(edge-capacity_cost)+2*min(0.,stress)-2*downside))


def select_full_exit(plan,snapshot,cfg,engine,paths=PATHS):
    """Prepare the TP1 gate; continuation is decided only AFTER TP1 is observed."""
    engine.use_full_exit(plan,snapshot.now)
    if plan.get('exit_selection'):
        return plan['exit_selection']
    first=plan.get('tp1',plan['tp'])
    plan.update(tp=first,full_exit_target='TP1',partials={'TP1':1.},
                target_management='ADAPTIVE_FULL_VOLUME',tp1_decision_gate=True)
    plan['outcome_forecast']=forecast(plan,snapshot,cfg,engine,paths=paths)
    plan['exit_selection']={'schema':'observed_tp1_full_volume_gate_v2','as_of_ts':snapshot.now,
        'status':'WAIT_FOR_OBSERVED_TP1','selected':'TP1',
        'initial_decision_preserved':True,'review_during_supervision':True,
        'rule':'NO_DISCRETIONARY_EXIT_OR_EXTENSION_BEFORE_OBSERVED_TP1',
        'calibrated':False}
    return plan['exit_selection']
