"""Auditable ICT method contracts for each of the 24 ORIGINAL detectors.

This is an execution gate, not a new detector and not a claim of profit.
ICT does not specify one machine-testable universal recipe. Each entry below
states what the native detector establishes, what closed-price evidence is
required, and whether the name is a direct ICT model or a contextual derivative.
Unconfirmed locations cannot masquerade as completed market-structure events.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def session_liquidity(snapshot):
    """As-of pools. Session clocks follow London/NY DST, day follows Kyiv."""
    cache=vars(snapshot).setdefault('_session_cache',{})
    key=('pools',snapshot.now)
    if key in cache: return cache[key]
    now=datetime.fromtimestamp(snapshot.now/1000,ZoneInfo('Europe/Kyiv'))
    day=now.date()
    rows=sorted((c for c in snapshot.candles.get('15m', [])
                 if c.confirmed and c.ts+900_000<=snapshot.now),key=lambda c:c.ts)
    pools=[]
    def add(label,bars):
        if bars:
            for suffix,level in (('HIGH',max(c.high for c in bars)),('LOW',min(c.low for c in bars))):
                pools.append({'name':label+'_'+suffix,'level':level,
                              'known_ts':max(c.ts+900_000 for c in bars)})
    previous=[c for c in rows if datetime.fromtimestamp(c.ts/1000,ZoneInfo('Europe/Kyiv')).date()==day-timedelta(days=1)]
    add('PD',previous)
    today=[c for c in rows if datetime.fromtimestamp(c.ts/1000,ZoneInfo('Europe/Kyiv')).date()==day]
    asia=[c for c in today if datetime.fromtimestamp(c.ts/1000,ZoneInfo('Europe/Kyiv')).hour<9]
    # Do not publish an incomplete Asian reference as a finished session pool.
    if now.hour>=9: add('ASIA',asia)
    london=[]
    for c in today:
        d=datetime.fromtimestamp(c.ts/1000,ZoneInfo('Europe/London'))
        if 8*60<=d.hour*60+d.minute<16*60+30: london.append(c)
    add('LONDON',london)
    new_york = ZoneInfo('America/New_York')
    add('NEW_YORK', [c for c in today if 570 <=
        (d := datetime.fromtimestamp(c.ts/1000,new_york)).hour * 60 + d.minute < 1020])
    lon=now.astimezone(ZoneInfo('Europe/London'));ny=now.astimezone(ZoneInfo('America/New_York'))
    active_ny=9*60+30<=ny.hour*60+ny.minute<17*60
    active_lon=8*60<=lon.hour*60+lon.minute<16*60+30
    result={'session':'Нью-Йорк' if active_ny else 'Лондон' if active_lon else 'Азія / поза сесією',
            'active':active_ny or active_lon,'pools':pools,'as_of_ts':snapshot.now}
    cache[key]=result
    return result


SWEEP_TO_BOS_M15_BARS = 10


def wick_reclaim(bar, side, level, tick):
    """Wick-only raid with the candle body back on the protected side."""
    extreme = bar.low if side == 1 else bar.high
    body_edge = min(bar.open, bar.close) if side == 1 else max(bar.open, bar.close)
    if (side * (level - extreme) < tick - 1e-8 or side * (body_edge - level) < -tick/2
            or side * (bar.close - level) < tick - 1e-8):
        return 0.
    return side * (body_edge - extreme) / max(tick, bar.high - bar.low)


def reversal_engulfing(raid, confirmation, side, tick, step=900_000):
    """Require an opposite closed body, or a close beyond the entire raid."""
    if not (raid.confirmed and confirmation.confirmed and confirmation.ts-raid.ts == step
            and side * (confirmation.close-confirmation.open) >= tick-1e-8):
        return False
    extreme = raid.high if side == 1 else raid.low
    engulf = (side*(raid.close-raid.open) <= -tick+1e-8
              and side*(confirmation.open-raid.close) <= tick/2
              and side*(confirmation.close-raid.open) >= tick-1e-8)
    return engulf or side*(confirmation.close-extreme) >= tick-1e-8


def reclaim_break(rows, index, side, level, tick, atr):
    bar, previous = rows[index], rows[index-1]
    return (bar.confirmed and previous.confirmed and bar.ts-previous.ts == 900_000
            and side*(previous.close-level) <= 0
            and side*(bar.close-level) > max(tick, .15 * atr)
            and side*(bar.close-bar.open) >= max(tick,.60*atr))


def reclaim_references(prior, points, opened, side, tick, atr):
    """Known pivot and repeated-test levels on the approach to a break."""
    kind = 'HIGH' if side == 1 else 'LOW'
    start = prior[0].ts
    previous = prior[-1].close
    refs = [(p['level'], p['known_ts'] + 900_000) for p in points
            if p['kind'] == kind and start <= p['ts']
            and p['known_ts'] + 900_000 <= opened and side * (p['level'] - previous) >= 0]
    tolerance = max(2 * tick, .25 * atr)
    for i, bar in enumerate(prior):
        level = bar.high if side == 1 else bar.low
        if side * (level - previous) < 0 or any(side * (c.close - level) > tick/2 for c in prior[i:]):
            continue
        if any(abs((c.high if side == 1 else c.low) - level) <= tolerance for c in prior[max(0, i-8):i]):
            refs.append((level, bar.ts + 900_000))
    return sorted(refs, key=lambda ref: (side * (ref[0] - previous), -ref[1]))


def body_break(rows, index, points, side, tick, step):
    """Return a pivot already known when the crossing candle opened."""
    if index < 1 or rows[index].ts - rows[index - 1].ts != step:
        return None
    candle, previous = rows[index], rows[index - 1]
    kind = 'HIGH' if side == 1 else 'LOW'
    pivot = next((p for p in reversed(points) if p['kind'] == kind
                  and p['known_ts'] + step <= candle.ts), None)
    if pivot is None:
        return None
    level = pivot['level']
    if (side * (previous.close - level) <= 0
            and side * (candle.open - level) <= tick * .5
            and side * (candle.close - level) > tick
            and side * (candle.close - candle.open) > tick):
        return pivot
    return None


def immediate_session_sweep(snapshot, rows, side, tick):
    """Engulf the prior M15 body on the same candle that raids a known pool."""
    from dataclasses import replace

    if len(rows) < 2:
        return None
    previous, raid = rows[-2:]
    step = 900_000
    if raid.ts - previous.ts != step:
        return None
    if not (side * (previous.close - previous.open) < 0
            and side * (raid.close - raid.open) > tick
            and side * (raid.open - previous.close) <= tick * .5
            and side * (raid.close - previous.open) >= -tick * .5):
        return None
    prior = replace(snapshot, now=raid.ts)
    kyiv = ZoneInfo('Europe/Kyiv')
    local = datetime.fromtimestamp(raid.ts / 1000, kyiv)
    london = local.astimezone(ZoneInfo('Europe/London'))
    new_york = local.astimezone(ZoneInfo('America/New_York'))
    prefixes = []
    if 480 <= london.hour*60 + london.minute < 990:
        prefixes.append('LONDON_')
    if 570 <= new_york.hour*60 + new_york.minute < 1020:
        prefixes.append('NEW_YORK_')
    pools = [p for p in session_liquidity(prior)['pools'] if p['name'].startswith(tuple(prefixes))]
    asia = [c for c in rows[:-1] if (d := datetime.fromtimestamp(c.ts / 1000, kyiv)).date()
            == local.date() and d.hour < local.hour + 1] if local.hour < 9 else []
    previous_hour = raid.ts // 3_600_000 * 3_600_000 - 3_600_000
    hour = [c for c in rows[:-1] if previous_hour <= c.ts < previous_hour + 3_600_000]
    if [c.ts for c in hour] != list(range(previous_hour, previous_hour + 3_600_000, step)):
        hour = []
    suffix = 'LOW' if side == 1 else 'HIGH'
    extreme = raid.low if side == 1 else raid.high
    for label, bars in (('ASIA', asia), ('PREV_HOUR', hour)):
        if bars:
            level = min(c.low for c in bars) if side == 1 else max(c.high for c in bars)
            pools.append({'name': label + '_' + suffix, 'level': level,
                          'known_ts': bars[-1].ts + step})
    eligible = [p for p in pools if p['name'].endswith(suffix)
                and p['known_ts'] <= raid.ts and side * (p['level'] - extreme) >= tick - 1e-9
                and side * (raid.close - p['level']) > tick]
    if eligible:
        pool = min(eligible, key=lambda p: abs(p['level'] - extreme))
        return {'pool': pool['name'], 'level': pool['level'], 'extreme': extreme,
                'known_ts': raid.ts + step, 'sweep_ts': raid.ts + step,
                'reference_known_ts': pool['known_ts'], 'pattern': 'ENGULFING', 'side': side}
    return None


def session_sweeps(snapshot, side, tick, engine):
    """A pool must be known before its raid; later CLOSED 15m MSS is mandatory."""
    from dataclasses import replace
    cache=vars(snapshot).setdefault('_session_cache',{})
    key=('sweeps',snapshot.now,side,tick)
    if key in cache: return cache[key]
    rows=[c for c in snapshot.candles.get('15m', []) if c.confirmed and c.ts+900_000<=snapshot.now]
    results=[]
    step = 900_000
    for i in range(max(12, len(rows)-SWEEP_TO_BOS_M15_BARS-3), len(rows)-1):
        raid=rows[i]
        prior=replace(snapshot,now=raid.ts,candles={**snapshot.candles,'15m':rows[:i]})
        pools=session_liquidity(prior)['pools']
        for pool in pools:
            if not pool['name'].endswith('LOW' if side==1 else 'HIGH'): continue
            level=pool['level']
            swept=(raid.low<level-tick and raid.close>level if side==1
                   else raid.high>level+tick and raid.close<level)
            if not swept: continue
            pivots=[p for p in engine.pivots(rows[:i]) if p['kind']==('HIGH' if side==1 else 'LOW')
                    and p['known_ts']+900_000<=raid.ts]
            if not pivots: continue
            broken=pivots[-1]['level']
            for j in range(i+1,min(len(rows),i+SWEEP_TO_BOS_M15_BARS+1)):
                if rows[j].ts != raid.ts + (j-i)*step:
                    break
                if (side*(rows[j-1].close-broken)<=0 and side*(rows[j].open-broken)<=tick/2
                        and side*(rows[j].close-broken)>tick and side*(rows[j].close-rows[j].open)>tick):
                    results.append({'pool':pool['name'],'level':level,
                        'sweep_ts':raid.ts+900_000,'shift_ts':rows[j].ts+900_000,
                        'broken_level':broken,'extreme':raid.low if side==1 else raid.high,
                        'side':side,'raid_index':i,'shift_index':j})
                    break
    cache[key]=results
    return results

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


def entry_trigger(plan,proof):
    """A session/HTF score cannot substitute for a witnessed raid or BOS/FVG."""
    if proof.get('sweep_reclaim') or proof.get('displacement_bos_fvg'):
        return True
    if plan.get('policy') == 'BZU_LIVE_V12_25':
        origin = plan.get('zone_origin', {})
        return bool(origin.get('confirmed') and
                    (origin.get('sweep') or (origin.get('break_known_ts') and
                     plan.get('m15_momentum_aligned'))))
    if plan.get('policy') == 'BZU_MOMENTUM_V12_24':
        origin = plan.get('zone_origin', {})
        zone = plan.get('entry_zone', {})
        return bool(origin.get('confirmed') and origin.get('break_known_ts')
                    and (plan.get('organic_confluence') or 'FVG' in zone.get('kind', ''))
                    and not plan.get('zone_audit', {}).get('mitigated'))
    return False


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
    if plan.get('policy') in ('BZU_PRECISION_V12_22','BZU_MASTER_V12_23','BZU_MOMENTUM_V12_24','BZU_LIVE_V12_25') and not entry_trigger(plan,proof):
        ok=False;why='ICT_SWEEP_OR_BOS_FVG_REQUIRED'
    return bool(ok),'CONFIRMED' if ok else why,details


def displacement_gap(rows, index, side, tick, step=900_000):
    if not 1 <= index < len(rows)-1:
        return None
    first, middle, last = rows[index-1:index+2]
    if (not all(c.confirmed for c in (first,middle,last)) or
            middle.ts-first.ts != step or last.ts-middle.ts != step):
        return None
    low, high = (first.high, last.low) if side==1 else (last.high, first.low)
    return (low, high) if high-low >= tick-1e-8 and side*(middle.close-middle.open)>0 else None


def prior_local_sweep(rows, index, points, side, tick):
    kind = 'LOW' if side == 1 else 'HIGH'
    for raid in reversed(rows[max(0,index-SWEEP_TO_BOS_M15_BARS):index]):
        if rows[index].ts-raid.ts > SWEEP_TO_BOS_M15_BARS*900_000:
            break
        p = next((p for p in reversed(points) if p['kind']==kind and p['known_ts']+900_000 <= raid.ts), None)
        if p and wick_reclaim(raid, side, p['level'], tick) > 0:
            return {'sweep_ts': raid.ts+900_000, 'level': p['level'],
                    'extreme': raid.low if side==1 else raid.high}
    return None


def breaker_evidence(rows, index, points, side, tick):
    gap = displacement_gap(rows, index, side, tick)
    sweep = prior_local_sweep(rows, index, points, side, tick) if gap else None
    return {**sweep, 'fvg': gap, 'impulse_ts': rows[index].ts} if sweep else None


def bearish_breakout(rows, index, points, tick, atr):
    """Return the pre-existing support broken by a closed displacement."""
    if index < 19 or atr <= 0:
        return None
    bar = rows[index]
    if not bar.confirmed or bar.open-bar.close < 1.15*atr - 1e-8:
        return None
    closes = [c.close for c in rows[max(0,index-95):index+1]]
    ema20 = ema50 = closes[0]
    for close in closes[1:]:
        ema20 += (close-ema20)*2/21
        ema50 += (close-ema50)*2/51
    pivot = body_break(rows,index,points,-1,tick,900_000)
    if not (bar.close < ema20 and (bar.close < ema50 or pivot)):
        return None
    prior = rows[max(0,index-96):index]
    references = reclaim_references(prior,points,bar.ts,-1,tick,atr)
    if pivot:
        references.sort(key=lambda item: abs(item[0]-pivot['level']) >= tick/2)
    for level, known in references:
        if not reclaim_break(rows,index,-1,level,tick,atr) or bar.open < level-tick/2:
            continue
        if any(c.ts >= known and c.close < level-tick/2 for c in prior):
            continue
        return {'level':level,'reference_known_ts':known,'ema20':ema20,'ema50':ema50,
                'mss_level':pivot['level'] if pivot else None,'body_atr':(bar.open-bar.close)/atr}
    return None


def stop_sweep_recovery(rows, side, closed_ts, stop, tick, engine):
    """A witnessed stop raid and a fresh same-side MSS within four M15."""
    step = 900_000
    points = engine.pivots(rows,strength=1)
    for i, raid in enumerate(rows):
        if not raid.confirmed or not raid.ts <= closed_ts <= raid.ts+step:
            continue
        extreme = raid.high if side==-1 else raid.low
        if side*(extreme-stop) > tick/2:
            continue
        pre_sweep = [p for p in points if p['known_ts']+step <= raid.ts]
        for j in range(i+1,min(len(rows),i+5)):
            bar = rows[j]
            if not bar.confirmed or bar.ts-rows[j-1].ts != step:
                break
            if side*(bar.close-stop)<tick-1e-8:
                continue
            pivot = body_break(rows,j,pre_sweep,side,tick,step)
            if pivot:
                return {'known_ts':bar.ts+step,'level':pivot['level'],'sweep_ts':raid.ts+step}
    return None
