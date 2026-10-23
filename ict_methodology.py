"""Auditable ICT method contracts for each of the 24 ORIGINAL detectors.

This is an execution gate, not a new detector and not a claim of profit.
ICT does not specify one machine-testable universal recipe. Each entry below
states what the native detector establishes, what closed-price evidence is
required, and whether the name is a direct ICT model or a contextual derivative.
Unconfirmed locations cannot masquerade as completed market-structure events.
"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Contract:
    family: str
    status: str
    native: str
    proof: str

# Explicit coverage; never fall back silently for an unknown setup.
CONTRACTS = {
 'SWEEP_RECLAIM':Contract('LIQUIDITY_REVERSAL','ICT_CORE','Sell-side sweep + closed reclaim','LIQUIDITY'),
 'CAPITULATION_RECOVERY':Contract('LIQUIDITY_REVERSAL','ICT_DERIVED','Capitulation followed by observed recovery','RECOVERY'),
 'RANGE_EDGE_REVERSAL':Contract('RANGE_REVERSAL','ICT_DERIVED','Range edge is a location, not a reversal','RANGE_REJECTION'),
 'FAILED_AUCTION_REJECTION':Contract('LIQUIDITY_REVERSAL','ICT_DERIVED','Failed move beyond daily/weekly reference','FAILED_BREAK'),
 'LIQUIDITY_SWEEP_REVERSAL_SHORT':Contract('LIQUIDITY_REVERSAL','ICT_CORE','Buy-side raid and return inside range','LIQUIDITY'),
 'BUYER_EXHAUSTION_SHORT':Contract('LIQUIDITY_REVERSAL','ICT_DERIVED','Exhaustion plus confirmed bearish transfer','FAILED_BREAK'),
 'PULLBACK_CONTINUATION':Contract('TREND_CONTINUATION','ICT_DERIVED','Trend plus defended retracement zone','CONTINUATION'),
 'FRESH_BASE_CONTINUATION':Contract('TREND_CONTINUATION','ICT_DERIVED','Pre-existing base, then displacement','CONTINUATION'),
 'ACCEPTANCE_RETEST_CONTINUATION':Contract('TREND_CONTINUATION','ICT_DERIVED','Break and acceptance, then limit retest','ACCEPTANCE'),
 'MOMENTUM_NO_PULLBACK_CONTINUATION':Contract('TREND_CONTINUATION','ICT_DERIVED','Displacement and unconsumed imbalance','MOMENTUM'),
 'ACCELERATION_PULLBACK_REENTRY':Contract('TREND_CONTINUATION','ICT_DERIVED','Multiple impulse legs and retracement','CONTINUATION'),
 'DIRECTION_FLIP_15M':Contract('STRUCTURE_BREAK','ICT_CORE','Confirmed market-structure shift','STRUCTURE'),
 'TREND_IGNITION':Contract('STRUCTURE_BREAK','ICT_DERIVED','Pre-break compression and confirmed expansion','BREAK'),
 'BREAKOUT_RETEST':Contract('STRUCTURE_BREAK','ICT_DERIVED','Closed range break followed by retest','BREAK'),
 'RANGE_COMPRESSION_BREAKOUT':Contract('STRUCTURE_BREAK','ICT_DERIVED','Pre-mapped range boundary; wait for break','BREAK'),
 'OPENING_RANGE_BREAKOUT':Contract('SESSION_EXPANSION','ICT_DERIVED','Complete opening range then break/retest','BREAK'),
 'LIQUIDITY_LADDER':Contract('LIQUIDITY_REVERSAL','ICT_CONTEXT_ONLY','Equal highs/lows only mark stop clusters','LIQUIDITY_SHIFT'),
 'FAILED_OPENING_RANGE_BREAKOUT':Contract('FAILED_EXPANSION','ICT_DERIVED','Opening range raid and return','FAILED_BREAK'),
 'FAILED_BREAKOUT_SHORT':Contract('FAILED_EXPANSION','ICT_DERIVED','High sweep, rejection and close inside','FAILED_BREAK'),
 'MSS_REVERSAL_SHORT':Contract('LIQUIDITY_REVERSAL','ICT_CORE','Bearish MSS after liquidity raid','LIQUIDITY_SHIFT'),
 'OR_FAILURE_2_SHORT':Contract('FAILED_EXPANSION','ICT_DERIVED','Repeated failed opening-range escape','FAILED_BREAK'),
 'SESSION_MEAN_RECLAIM':Contract('VALUE_RECLAIM','ICT_CONTEXT_ONLY','VWAP is a reference, not a standalone ICT entry','VALUE_RECLAIM'),
 'DAILY_WEEKLY_OPEN_RECLAIM':Contract('VALUE_RECLAIM','ICT_CONTEXT_ONLY','Day/week open is a reference, not an ICT trigger','VALUE_RECLAIM'),
 'TIME_OF_DAY_ADAPTIVE':Contract('SESSION_CONTEXT','ICT_CONTEXT_ONLY','Time of day is a filter, not an entry trigger','SESSION_TRIGGER'),
}


def validate(plan, proof):
    """Return (allowed, code, diagnostics) using evidence known as-of decision.

    Strong proof requirements are tied to the claimed setup type, NOT imposed
    indiscriminately on trend following. Existing live orders are never altered
    by this candidate-admission function.
    """
    name=plan.get('setup')
    contract=CONTRACTS.get(name)
    if contract is None:
        return False,'ICT_UNKNOWN_SETUP',{'status':'UNKNOWN','required':'REGISTERED_CONTRACT'}
    route=contract.proof
    sweep=bool(proof.get('sweep_reclaim'))
    shift=bool(proof.get('structure_shift'))
    impulse=bool(proof.get('displacement'))
    fvg=bool(proof.get('near_unmitigated_fvg'))
    rejection=bool(proof.get('level_rejection'))
    accepted=bool(proof.get('break_acceptance'))
    reclaim=bool(proof.get('reference_reclaim'))
    sequence=bool(proof.get('sweep_then_shift'))
    native=bool(plan.get('detector_confirmed',True))
    recovery=bool(plan.get('pattern_proof',{}).get('confirmed'))
    # Original detector is authoritative ONLY if its witnessed evidence is
    # persisted from the actual formation and known before this scan. A test
    # fixture with a bare 'detector_confirmed' boolean is NOT such evidence.
    anchor=plan.get('anchor_proof')
    anchored=(isinstance(anchor,dict) and bool(anchor) and native
              and 0 < plan.get('anchor_known_ts',0) <= proof.get('as_of_ts',0))
    # Do not let arbitrary native-detector metadata impersonate a proven
    # sweep/break. The original witness must contain the actual method event.
    if anchored and name in ('SWEEP_RECLAIM','LIQUIDITY_SWEEP_REVERSAL_SHORT'):
        try:
            level=float(anchor['swept_level']); extreme=float(anchor['sweep_extreme'])
            anchored=(level>0 and (extreme<level if name=='SWEEP_RECLAIM' else extreme>level)
                      and float(anchor.get('wick_ratio',0))>0)
        except (KeyError,ValueError,TypeError):
            anchored=False
    if anchored and name=='DIRECTION_FLIP_15M':
        anchored=bool(anchor.get('broken_pivot') and anchor.get('shift_kind'))
    if anchored and name=='TREND_IGNITION':
        try:
            anchored=bool(anchor.get('compression') is not None and
                          float(anchor.get('break_body_atr',0))>=1.1)
        except (ValueError,TypeError):
            anchored=False
    if anchored and name=='BREAKOUT_RETEST':
        anchored=bool(anchor.get('broken_level') and anchor.get('broken_level_key'))
    if anchored and name=='FRESH_BASE_CONTINUATION':
        anchored=bool(anchor.get('base_known_before_displacement'))
    activated=(anchored and anchor.get('activation_phase')=='LIMIT_RETEST'
               and 0 < anchor.get('break_known_ts',0)<=proof.get('as_of_ts',0))
    # A waiting mapped boundary MUST NOT be treated as the completed break.
    phase=plan.get('activation_phase') or plan.get('planning_policy','')
    if route=='LIQUIDITY':
        ok=sweep or (anchored and name in ('SWEEP_RECLAIM','LIQUIDITY_SWEEP_REVERSAL_SHORT'))
        why='ICT_LIQUIDITY_SWEEP_NOT_CONFIRMED'
    elif route=='LIQUIDITY_SHIFT':
        if name=='MSS_REVERSAL_SHORT':
            # MSS is a STRUCTURE break AFTER a raid, not displacement alone.
            ok=sweep and shift and sequence
        else:
            impulse_ts=proof.get('latest_displacement_known_ts') or 0
            sweep_ts=(proof.get('latest_sweep') or {}).get('known_ts',0)
            after_impulse=bool(impulse and impulse_ts and sweep_ts and impulse_ts>sweep_ts)
            ok=sweep and ((shift and sequence) or after_impulse)
        why='ICT_SWEEP_STRUCTURE_SEQUENCE_MISSING'
    elif route=='RANGE_REJECTION':
        ok=sweep or rejection
        why='ICT_RANGE_EDGE_WITHOUT_REJECTION'
    elif route=='RECOVERY':
        ok=native and recovery
        why='ICT_RECOVERY_NOT_CONFIRMED'
    elif route=='FAILED_BREAK':
        ok=native and (rejection or sweep or reclaim or anchored)
        why='ICT_FAILED_BREAK_NOT_PROVEN'
    elif route=='STRUCTURE':
        ok=shift or (anchored and name=='DIRECTION_FLIP_15M')
        why='ICT_STRUCTURE_SHIFT_NOT_CONFIRMED'
    elif route=='BREAK':
        ok=native and (accepted or shift or activated or (anchored and name in ('TREND_IGNITION','BREAKOUT_RETEST')))
        why='ICT_BREAK_CLOSE_MISSING'
    elif route=='ACCEPTANCE':
        ok=native and (accepted or shift or anchored)
        why='ICT_ACCEPTANCE_NOT_CONFIRMED'
    elif route=='MOMENTUM':
        ok=native and impulse and fvg
        why='ICT_MOMENTUM_IMBALANCE_MISSING'
    elif route=='CONTINUATION':
        ok=native and (impulse or fvg or shift or accepted or (anchored and name=='FRESH_BASE_CONTINUATION'))
        why='ICT_CONTINUATION_DEPARTURE_MISSING'
    elif route=='VALUE_RECLAIM':
        ok=native and reclaim and (impulse or shift or fvg)
        why='ICT_VALUE_RECLAIM_UNCONFIRMED'
    elif route=='SESSION_TRIGGER':
        ok=native and (sweep or (shift and (impulse or fvg)) or (rejection and impulse))
        why='ICT_SESSION_HAS_NO_PRICE_TRIGGER'
    else:
        raise AssertionError('Unknown ICT requirement '+route)
    details={'status':contract.status,'family':contract.family,'native_claim':contract.native,
             'required':route,'sweep':sweep,'structure_shift':shift,'displacement':impulse,
             'fvg':fvg,'rejection':rejection,'acceptance':accepted,'reclaim':reclaim,
             'evidence_as_of_ts':proof.get('as_of_ts'),'original_detector_proven':anchored,
             'break_retest_activated':activated}
    return bool(ok),'CONFIRMED' if ok else why,details
