"""v15.5: unvalidated PAPER control + isolated challenger policies.
No candidate profit claim; do not promote experiments automatically.
"""
from mexc_opportunity import MODEL_SCHEMA, estimate, policy

SHADOW_FAMILIES = frozenset(('STRUCTURE_CONTINUATION','COUNTERTREND_WAVE'))
ACTIVE_EXIT = dict(tp1_enabled=False,time_stop_enabled=False,idea_review_enabled=False)

def active_estimate(history, candidate, as_of):
    # Only the same active execution policy, no counterfactuals or unvalidated historical priors.
    rows=[r for r in history if r.get('model_schema')==MODEL_SCHEMA and r.get('source','PAPER')=='PAPER' and r.get('quality')=='OBSERVED_M1']
    return estimate(rows,candidate,as_of)

def exit_policies(family,phase,rr,funding_regime):
    trial=policy(family,phase,rr,funding_regime)
    return {**trial,**ACTIVE_EXIT},trial

def tag_experiment(thesis):
    if thesis.get('research_variant'): return thesis
    if thesis.get('family') in SHADOW_FAMILIES:
        return {**thesis,'research_variant':thesis['family']}
    return thesis
