"""Causal, conservative second-stage trade selector (Python stdlib only).

Forecasts conditional win probability *after an observed paper-limit fill*.
Never claims exchange calibration. Splits are sequential by decision time, with
purging of training observations that closed after the next split began.
No model/invalid model => monitor only; no invented win rate or probability.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

VERSION = 'bzu-meta-label-v1'
MIN_EPISODES = 20  # Begin causal model assessment after 20 independent closes.
MIN_TRAIN = 10
MIN_CALIBRATE = 3
MIN_VALIDATE = 5
MIN_SELECTED_HOLDOUT = 10  # A 20-episode sample cannot certify a trading edge.
ENTRY_PROBABILITY_FLOOR = 0.65  # Experimental threshold, not a promised WR.
SCORE_ADJUSTMENT_CAP = 5.0

# Frozen, pre-decision features. Never include PnL, future MFE/MAE, label,
# closed_ts or realized future control actions as predictors.
FEATURES = (
    'htf_opposition', 'alignment_aligned', 'alignment_against',
    'shift3', 'shift15', 'impulse', 'acceptance', 'early_reversal_control',
    'distance_atr', 'range_position', 'cost_fraction', 'shock',
    'side', 'kind_reversal', 'kind_breakout', 'regime_trend',
)


def _bounded(value, lo, hi, default=0.0):
    try:
        v = float(value)
        return max(lo, min(hi, v)) if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default


def vector(f):
    """No native OHLC data or custom mutable runtime keys can leak into model."""
    regime = str(f.get('regime', '')).upper()
    return (
        _bounded(f.get('htf_opposition'), 0, 2)/2,
        float(f.get('alignment') == 'aligned'),
        float(f.get('alignment') == 'against'),
        float(bool(f.get('shift3'))),
        float(bool(f.get('shift15'))),
        float(bool(f.get('impulse'))),
        float(bool(f.get('acceptance'))),
        float(bool(f.get('early_reversal_control'))),
        _bounded(f.get('distance_atr'), -5, 5)/5,
        _bounded(f.get('range_position'), 0, 1),
        _bounded(f.get('cost_fraction'), 0, 1),
        float(bool(f.get('shock'))),
        _bounded(f.get('side'), -1, 1),
        float(f.get('kind') == 'reversal'),
        float(f.get('kind') == 'breakout'),
        float('TREND' in regime),
    )


def _sigmoid(x):
    return 1/(1+math.exp(-max(-40., min(40., x))))


def _logit(p):
    p=max(1e-6,min(1-1e-6,p))
    return math.log(p/(1-p))


def _probability(weights, f):
    x=vector(f)
    return _sigmoid(weights[0]+sum(a*b for a,b in zip(weights[1:],x)))


def _fit(rows):
    """Regularized weighted logistic model; fixed hyperparameters, deterministic."""
    wins=sum(r['net_r']>0 for r in rows)
    prior=(wins+2)/(len(rows)+4)
    weights=[_logit(prior)]+[0.]*len(FEATURES)
    # L2 on predictors, no regularization of intercept; no data-dependent
    # threshold optimization on the validation fold.
    xs=[vector(r['features']) for r in rows]
    ys=[float(r['net_r']>0) for r in rows]
    n=len(rows)
    for iteration in range(250):
        residuals=[_sigmoid(weights[0]+sum(w*x for w,x in zip(weights[1:],v)))-y
                   for v,y in zip(xs,ys)]
        gradients=[sum(residuals)/n]
        for j in range(len(FEATURES)):
            gradients.append(sum(err*v[j] for err,v in zip(residuals,xs))/n+0.10*weights[j+1])
        rate=0.65/(1+iteration/100)
        weights=[w-rate*g for w,g in zip(weights,gradients)]
    return weights


def _good_row(row, now, strategy):
    """Exclude labels not yet known and anything without a verifiable fill."""
    if not isinstance(row,dict) or row.get('strategy')!=strategy or row.get('qualified') is not True:
        return False
    if row.get('conservative_ambiguity') or not isinstance(row.get('features'),dict):
        return False
    try:
        d=int(row['decision_ts']);c=int(row['closed_ts']);filled=int(row['filled_ts'])
        net=float(row['net_r']);stamp=int(row['features']['as_of_ts'])
        return 0<stamp<=d<=filled<=c<now and math.isfinite(net)
    except (KeyError,ValueError,TypeError,OverflowError):
        return False


def episodes(observations, now, strategy):
    """Independently resolved, non-overlapping shadow episodes, as of now."""
    eligible=[r for r in observations if _good_row(r,now,strategy)]
    eligible.sort(key=lambda r:(r['decision_ts'],r['closed_ts'],str(r.get('id',''))))
    selected=[];end=-1
    for row in eligible:
        if row['decision_ts']>end:
            selected.append(row)
            end=row['closed_ts']
    return selected


def _partition(rows):
    n=len(rows)
    cut1=int(n*.60);cut2=int(n*.75)
    train=rows[:cut1]
    cal=rows[cut1:cut2]
    holdout=rows[cut2:]
    # Purge any training event overlapping the next phase. Prevent leakage
    # even when a trade opened many hours before another one closed.
    train=[r for r in train if r['closed_ts'] < cal[0]['decision_ts']] if cal else []
    cal=[r for r in cal if r['closed_ts'] < holdout[0]['decision_ts']] if holdout else []
    return train,cal,holdout


def _brier(ps, rows):
    return sum((p-float(r['net_r']>0))**2 for p,r in zip(ps,rows))/len(rows)


def train(observations, now, strategy):
    """One-pass chronological holdout evaluation; fail closed to MONITOR_ONLY."""
    rows=episodes(observations,now,strategy)
    result={'version':VERSION,'status':'MONITOR_ONLY_INSUFFICIENT_HISTORY',
            'validated':False,'independent_episodes':len(rows),
            'probability':None,'entry_gate_enabled':False,
            'training_source':'CAUSAL_SHADOW_FILLED_EPISODES',
            'warning':'PAPER_LABELS_NOT_EXCHANGE_EXECUTIONS'}
    if len(rows)<MIN_EPISODES:
        result['required_independent_episodes']=MIN_EPISODES
        return result
    train_rows,cal,holdout=_partition(rows)
    result.update(training_episodes=len(train_rows),calibration_episodes=len(cal),
                  validation_episodes=len(holdout))
    if len(train_rows)<MIN_TRAIN or len(cal)<MIN_CALIBRATE or len(holdout)<MIN_VALIDATE:
        result['status']='MONITOR_ONLY_SPLIT_TOO_SMALL_OR_OVERLAPPING'
        return result
    # A short calibration/holdout slice can still be examined, but never
    # treat a slice containing only one outcome class as calibrated evidence.
    if any(min(sum(r['net_r']>0 for r in group),sum(r['net_r']<=0 for r in group))<minimum
           for group,minimum in ((train_rows,2),(cal,1),(holdout,1))):
        result['status']='MONITOR_ONLY_CLASS_SUPPORT_INADEQUATE'
        return result
    w=_fit(train_rows)
    # 1-D intercept calibration on a later time slice. All weights fixed.
    offset=0.
    for _ in range(100):
        diffs=[_sigmoid(_logit(_probability(w,r['features']))+offset)-float(r['net_r']>0) for r in cal]
        offset=max(-2.,min(2.,offset-0.3*sum(diffs)/len(diffs)))
    probabilities=[_sigmoid(_logit(_probability(w,r['features']))+offset) for r in holdout]
    baseline=(sum(r['net_r']>0 for r in train_rows)+2)/(len(train_rows)+4)
    score=_brier(probabilities,holdout)
    base_score=_brier([baseline]*len(holdout),holdout)
    result.update(holdout_brier=round(score,6),baseline_brier=round(base_score,6),
                  holdout_win_rate=round(sum(r['net_r']>0 for r in holdout)/len(holdout),6),
                  trained_through_ts=max(r['closed_ts'] for r in train_rows),
                  validated_through_ts=max(r['closed_ts'] for r in holdout))
    # Need genuine out-of-sample improvement, rather than an in-sample score.
    if not score < base_score-0.005:
        result['status']='MONITOR_ONLY_NO_HOLDOUT_IMPROVEMENT'
        return result
    # The proposed 0.65 threshold must ALSO beat doing nothing on the
    # untouched holdout. Predictive Brier improvement alone is not a trading
    # edge; validate completed net R, not just classification accuracy.
    selected=[r for prob,r in zip(probabilities,holdout)
              if prob>=ENTRY_PROBABILITY_FLOOR]
    selected_wins=sum(r['net_r']>0 for r in selected)
    selected_mean=sum(r['net_r'] for r in selected)/len(selected) if selected else None
    result.update(holdout_selected=len(selected),
                  selected_win_rate=round(selected_wins/len(selected),6) if selected else None,
                  selected_expectancy_r=round(selected_mean,6) if selected_mean is not None else None)
    if len(selected)<MIN_SELECTED_HOLDOUT or selected_mean is None or selected_mean<=0 \
            or selected_wins/len(selected)<.65:
        result['status']='MONITOR_ONLY_FILTER_HAS_NO_VALIDATED_NET_EDGE'
        return result
    # PAPER out-of-sample validation is deliberately NOT called proof of a
    # live-trading win rate, fill probability, or real order execution.
    result.update(status='SHADOW_HOLDOUT_FILTER_PASSED',validated=True,
                  entry_gate_enabled=True,weights=[round(x,9) for x in w],
                  calibration_offset=round(offset,9))
    return result


def evaluate(model, features, *, net_rr, costs_r=0.0):
    """Evaluate an executable plan; never backfill an unsupported probability."""
    if not model.get('validated') or len(model.get('weights',[]))!=len(FEATURES)+1:
        return {'status':model.get('status','MONITOR_ONLY'), 'validated':False,
                'probability':None, 'entry_gate_enabled':False, 'accepted':True}
    p=_sigmoid(_logit(_probability(model['weights'],features))+model['calibration_offset'])
    win_reward=_bounded(net_rr,0,20)
    # net_rr already includes estimated WIN costs. On STOP assume the full
    # risk plus remaining exit costs, conservatively and only on the losing side.
    loss_r=1.+_bounded(costs_r,0,1)
    net_ev=p*win_reward-(1-p)*loss_r
    acceptable=p>=ENTRY_PROBABILITY_FLOOR and net_ev>0
    return {'status':'EXPERIMENTAL_SHADOW_P_WIN', 'validated':True,
            'probability':round(p,5), 'expected_net_r':round(net_ev,5),
            'entry_gate_enabled':True,'accepted':acceptable,
            'reason':'META_LOW_PROBABILITY_OR_NONPOSITIVE_EXPECTANCY' if not acceptable else 'META_ACCEPTED',
            'score_adjustment':max(-SCORE_ADJUSTMENT_CAP,min(SCORE_ADJUSTMENT_CAP,(p-.65)*12))}
