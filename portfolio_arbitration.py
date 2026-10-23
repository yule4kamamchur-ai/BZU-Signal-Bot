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


def decision(pending, candidates, snapshot, cfg, policy, last_swap_ts=0):
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
    # Review the weakest mature incumbent first, but keep the OTHER plan
    # in the comparison: replacement cannot create duplicate same-thesis risk.
    weakest,weak_info=min(eligible,key=lambda item:item[1]['score'])
    retained=[p for p in current if p is not weakest]
    for challenger in sorted(candidates,key=lambda p:policy.ranking(p,snapshot.price,cfg.tick),reverse=True):
        if challenger.get('side') not in (-1,1):continue
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
