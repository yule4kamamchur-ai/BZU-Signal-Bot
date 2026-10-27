"""BZU scheduled-15m, causally confirmed *early* entry playbooks.

Different from the 24 legacy location detectors: a plan must match a fresh,
ordered closed-candle event and its entry zone. No win-rate claims or ML
projections. Called only with a point-in-time Snapshot, not historical labels.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Optional

import strategy_organics as organic

STEP = 900_000
MICRO = 180_000
HOUR = 3_600_000
FOUR_HOURS = 14_400_000


@dataclass(frozen=True)
class Setup:
    family: str
    side: int
    zone: float
    invalidation: float
    signal_ts: int
    event_ts: int
    atr: float
    note: str


def bars(snapshot, tf, duration):
    return sorted((x for x in snapshot.candles.get(tf, ())
                   if x.confirmed and x.ts + duration <= snapshot.now), key=lambda x: x.ts)


def vol(rows, n=14):
    if len(rows) < n+1:
        return 0.
    return mean(max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
                for p, c in zip(rows[-n-1:-1], rows[-n:]))


def bias(rows):
    if len(rows) < 14:
        return 0
    window=rows[-13:]
    move=window[-1].close-window[0].close
    path=sum(abs(b.close-a.close) for a,b in zip(window,window[1:]))
    atr=vol(rows,12)
    if not atr or abs(move)<1.25*atr or abs(move)/max(path,1e-12)<.31:
        return 0
    return 1 if move>0 else -1


def pivot_level(rows, before, side):
    """Most recent pivot confirmed *before* the sweep candle; no right-hand leakage."""
    for k in range(before-3, 1, -1):
        center=rows[k]
        neighbors=rows[k-2:k]+rows[k+1:k+3]
        if side==1 and all(center.high>x.high for x in neighbors):
            return center.high
        if side==-1 and all(center.low<x.low for x in neighbors):
            return center.low
    return None


def _slow_scenarios(snapshot, tick):
    """Fallback setups using closed 15m events when price has not retested.

    The candidate is *not* an order. The existing planner must separately
    prove stop/target geometry, costs, and a resting passive limit.
    """
    w=bars(snapshot,'15m',STEP)
    if len(w)<38 or w[-1].ts+STEP < snapshot.now-2*STEP:
        return []
    a=vol(w)
    if a<=0 or tick<=0:
        return []
    h1=bias(bars(snapshot,'1H',HOUR))
    h4=bias(bars(snapshot,'4H',FOUR_HOURS))
    latest=w[-1]
    found=[]
    # Liquidity raid -> reclaim -> a DIFFERENT, later confirmed MSS candle.
    # Prior pivot is known prior to the sweep; break cannot be backdated.
    for raid_idx in range(max(22,len(w)-6),len(w)-1):
        raid=w[raid_idx]
        history=w[raid_idx-20:raid_idx]
        if len(history)<20:
            continue
        for side in (1,-1):
            liquid=min(x.low for x in history) if side==1 else max(x.high for x in history)
            swept=(raid.low<liquid-tick and raid.close>liquid) if side==1 else (raid.high>liquid+tick and raid.close<liquid)
            if not swept:
                continue
            pivot=pivot_level(w,raid_idx,side)
            if pivot is None:
                continue
            for k in range(raid_idx+1,len(w)):
                shifted=(w[k].close>pivot+tick and w[k].close>w[k].open) if side==1 else (w[k].close<pivot-tick and w[k].close<w[k].open)
                impulse=abs(w[k].close-w[k].open)>=.40*a
                if not shifted or not impulse:
                    continue
                if any((x.close>pivot+tick if side==1 else x.close<pivot-tick)
                       for x in w[raid_idx+1:k]):
                    continue
                if latest.ts-w[k].ts>2*STEP:
                    continue
                if h1==h4==-side and abs(w[k].close-w[k].open)<.8*a:
                    continue
                zone=(liquid+pivot)/2  # center of swept liquidity-to-MSS area
                # Require a *passive* return; never chase a completed move.
                if side*(latest.close-zone)<2*tick:
                    continue
                extreme=raid.low if side==1 else raid.high
                found.append(Setup('LIQUIDITY_MSS',side,zone,extreme-tick if side==1 else extreme+tick,
                                   w[k].ts+STEP,raid.ts+STEP,a,'closed sweep/reclaim before confirmed MSS'))
                break
    # HTF aligned FVG retest: price has departed an unmitigated 3-candle gap.
    if h1:
        side=h1
        if h4!=-side:
            for k in range(max(2,len(w)-4),len(w)):
                x,y,z=w[k-2:k+1]
                gap_low=x.high if side==1 else z.high
                gap_high=z.low if side==1 else x.low
                if gap_high-gap_low<max(2*tick,.12*a):
                    continue
                if side*(y.close-y.open)<.55*a:
                    continue
                previous = w[max(0, k - 15):k - 2]
                if len(previous) < 10:
                    continue
                broken = max(c.high for c in previous) if side == 1 else min(c.low for c in previous)
                if side*(y.close-broken)<=tick:
                    continue
                if any((c.low<=gap_high if side==1 else c.high>=gap_low) for c in w[k+1:]):
                    continue
                zone=(gap_low+gap_high)/2
                if side*(latest.close-zone)<=2*tick:
                    continue
                found.append(Setup('HTF_FVG_RETEST',side,zone,
                                   gap_low-a*.25 if side==1 else gap_high+a*.25,
                                   z.ts+STEP,z.ts+STEP,a,'HTF-aligned unmitigated 3-candle FVG'))
    # Confirmed breakout acceptance is not the initial breakout wick.
    # Two consecutive closes beyond frozen pre-break range, then passive retest.
    for idx in range(max(21,len(w)-3),len(w)-1):
        prior=w[idx-20:idx]
        if len(prior)!=20:
            continue
        for side in (1,-1):
            if side!=h1 or h4==-side:
                continue
            level=max(c.high for c in prior) if side==1 else min(c.low for c in prior)
            first,second=w[idx],w[idx+1]
            if side*(first.close-level)<.12*a or side*(second.close-level)<2*tick:
                continue
            if side*(first.close-first.open)<.40*a:
                continue
            if any((c.low<=level if side==1 else c.high>=level) for c in w[idx+2:]):
                continue
            if side*(latest.close-level)<=2*tick:
                continue
            found.append(Setup('ACCEPTED_BREAK_RETEST',side,level,level-side*.5*a,
                               second.ts+STEP,first.ts+STEP,a,'two closed acceptance candles before retest'))
    return sorted(found,key=lambda x:(x.signal_ts,x.family),reverse=True)


def _fresh_micro(rows, now):
    """Do not infer events from missing candles or 3m bars still forming."""
    if len(rows) < 40 or now - (rows[-1].ts + MICRO) > STEP:
        return False
    # A missing observed interval could conceal a prior fill/retest.
    return all(b.ts - a.ts == MICRO for a, b in zip(rows[-16:-1], rows[-15:]))


def _untouched_since(rows, signal_index, zone, side, tick):
    """A level already revisited before this scan is not a new passive limit."""
    return all((row.low > zone + tick if side == 1 else row.high < zone - tick)
               for row in rows[signal_index + 1:])


def _micro_scenarios(snapshot, tick):
    """3m *closed* confirmations against already-known 15m/1H/4H context.

    This does not make the scheduler run every 3m. It makes recent 3m events
    usable at the next 15m run, provided no retracement has already occurred.
    """
    m = bars(snapshot, '3m', MICRO)
    w = bars(snapshot, '15m', STEP)
    if not _fresh_micro(m, snapshot.now) or len(w) < 25 or tick <= 0:
        return []
    atr = vol(m, 14)
    if not atr:
        return []
    h1 = bias(bars(snapshot, '1H', HOUR))
    h4 = bias(bars(snapshot, '4H', FOUR_HOURS))
    output = []
    # 3m raid -> confirmed 3m structural shift: do not require another 15m close.
    for raid_i in range(max(22, len(m) - 10), len(m) - 1):
        raid = m[raid_i]
        previous = [x for x in w if x.ts + STEP <= raid.ts]
        if len(previous) < 20:
            continue
        levels = previous[-20:]
        for side in (1, -1):
            # The reference liquidity was already published before the raid.
            liquidity = min(x.low for x in levels) if side == 1 else max(x.high for x in levels)
            swept = ((raid.low < liquidity - tick and raid.close > liquidity + tick)
                     if side == 1 else (raid.high > liquidity + tick and raid.close < liquidity - tick))
            if not swept:
                continue
            pivot = pivot_level(m, raid_i, side)
            if pivot is None:
                continue
            for k in range(raid_i + 1, min(len(m), raid_i + 7)):
                shift = ((m[k].close > pivot + tick and m[k].close > m[k].open)
                         if side == 1 else (m[k].close < pivot - tick and m[k].close < m[k].open))
                if not shift or abs(m[k].close - m[k].open) < .55 * atr:
                    continue
                if any((c.close > pivot + tick if side == 1 else c.close < pivot - tick)
                       for c in m[raid_i + 1:k]):
                    continue
                if snapshot.now - (m[k].ts + MICRO) > STEP:
                    continue
                if h1 == h4 == -side and abs(m[k].close - m[k].open) < 1.0 * atr:
                    continue
                # The impulse body is the predicted passive return region,
                # not the current candle close and not the already-swept low.
                zone = (m[k].open + m[k].close) / 2
                if side * (m[-1].close - zone) <= 2 * tick:
                    continue
                if not _untouched_since(m, k, zone, side, tick):
                    continue
                extreme = raid.low if side == 1 else raid.high
                if side * (zone - extreme) <= 2 * tick:
                    continue
                output.append(Setup('MICRO_SWEEP_MSS', side, zone,
                                    extreme - tick if side == 1 else extreme + tick,
                                    m[k].ts + MICRO, raid.ts + MICRO, atr,
                                    'prior 15m liquidity -> closed 3m sweep -> 3m MSS'))
                break
    # 3m three-candle imbalance may be prepared before a 15m bar completes.
    # HTF trend is read only from *completed* 1H/4H bars.
    if h1 and h4 != -h1:
        side = h1
        for k in range(max(2, len(m) - 6), len(m)):
            x, y, z = m[k - 2:k + 1]
            low = x.high if side == 1 else z.high
            high = z.low if side == 1 else x.low
            if high - low < max(2 * tick, .22 * atr):
                continue
            if side * (y.close - y.open) < .70 * atr:
                continue
            # FVG by itself is not a BOS. Require the displacement candle to
            # close beyond a swing level already known BEFORE that candle.
            prior = m[max(0, k - 18):k - 1]
            if len(prior) < 10:
                continue
            structural_level = (max(c.high for c in prior) if side == 1
                                else min(c.low for c in prior))
            if side * (y.close - structural_level) <= tick:
                continue
            if snapshot.now - (z.ts + MICRO) > STEP:
                continue
            zone = (low + high) / 2
            if side * (m[-1].close - zone) <= 2 * tick:
                continue
            if not _untouched_since(m, k, zone, side, tick):
                continue
            stop = low - .35 * atr if side == 1 else high + .35 * atr
            output.append(Setup('MICRO_HTF_FVG', side, zone, stop,
                                z.ts + MICRO, y.ts + MICRO, atr,
                                'closed 3m displacement FVG aligned with completed HTF'))
    # Breakout: frozen 15m range known before both 3m acceptance candles.
    for k in range(max(2, len(m) - 6), len(m) - 1):
        first, second = m[k], m[k + 1]
        prior = [c for c in w if c.ts + STEP <= first.ts]
        if len(prior) < 20:
            continue
        for side in (1, -1):
            if side != h1 or h4 == -side:
                continue
            level = max(c.high for c in prior[-20:]) if side == 1 else min(c.low for c in prior[-20:])
            if side * (first.close - level) < .25 * atr or side * (second.close - level) < 2 * tick:
                continue
            if side * (first.close - first.open) < .55 * atr:
                continue
            if snapshot.now - (second.ts + MICRO) > STEP:
                continue
            if side * (m[-1].close - level) <= 2 * tick or not _untouched_since(m, k + 1, level, side, tick):
                continue
            output.append(Setup('MICRO_ACCEPTED_BREAK', side, level, level - side * .65 * atr,
                                second.ts + MICRO, first.ts + MICRO, atr,
                                'frozen 15m range -> two closed 3m acceptance bars'))
    # Genuine range-edge mean reversion. This is NOT a naked Bollinger/VWAP
    # touch: published 15m boundaries -> 3m raid -> closed reclaim -> separate
    # closed rejection. Only use in independently identified sideways regimes.
    regime = organic.classify(snapshot)
    if regime.state == 'RANGE':
        for k in range(max(5, len(m) - 8), len(m) - 1):
            raid, rejection = m[k], m[k + 1]
            previous = [c for c in w if c.ts + STEP <= raid.ts]
            if len(previous) < 20:
                continue
            reference = previous[-20:]
            upper = max(c.high for c in reference)
            lower = min(c.low for c in reference)
            width = upper - lower
            # Regime width is defined BEFORE the raid; its enlarged true
            # range must not falsely disqualify a valid narrow range.
            baseline_atr = vol(m[:k], 14)
            if baseline_atr <= 0 or width < 2.0 * baseline_atr:
                continue
            for side in (1, -1):
                boundary = lower if side == 1 else upper
                swept = (raid.low < boundary - tick and raid.close > boundary + tick
                         if side == 1 else
                         raid.high > boundary + tick and raid.close < boundary - tick)
                if not swept:
                    continue
                # Closed continuation of the rejection; not the raid wick alone.
                if side * (rejection.close - rejection.open) < .25 * atr:
                    continue
                if side * (rejection.close - boundary) < 2 * tick:
                    continue
                if snapshot.now - (rejection.ts + MICRO) > STEP:
                    continue
                zone = boundary + side * max(2 * tick, .12 * atr)
                if side * (m[-1].close - zone) <= 2 * tick:
                    continue
                if not _untouched_since(m, k + 1, zone, side, tick):
                    continue
                extreme = raid.low if side == 1 else raid.high
                stop = extreme - side * max(tick, .15 * atr)
                if side * (zone - stop) <= 2 * tick:
                    continue
                output.append(Setup('MICRO_RANGE_REJECTION', side, zone, stop,
                                    rejection.ts + MICRO, raid.ts + MICRO, atr,
                                    'published 15m range -> 3m raid/reclaim -> separate rejection'))
    return sorted(output, key=lambda x: x.signal_ts, reverse=True)


def scenarios(snapshot, tick):
    """Micro confirmation first, slower 15m plays only before first retest."""
    micro = _micro_scenarios(snapshot, tick)
    slow = _slow_scenarios(snapshot, tick)
    # Reject slow events that already retested between the signal and now;
    # otherwise a completed 15m pattern can be too late for a LIMIT.
    m = bars(snapshot, '3m', MICRO)
    usable = []
    for signal in slow:
        after = [bar for bar in m if bar.ts >= signal.signal_ts]
        if not after or not _fresh_micro(m, snapshot.now):
            continue
        if any((bar.low <= signal.zone + tick if signal.side == 1 else bar.high >= signal.zone - tick)
               for bar in after):
            continue
        usable.append(signal)
    return micro + usable


def assess(plan, snapshot, tick, *, setup_candidates=None, market_regime=None):
    """Only admit legacy plan when a new independent strategy event matches it.

    All the native detector's existing quality checks must ALSO pass. The
    matcher never upgrades a failed native plan to an executable order.
    """
    setups=scenarios(snapshot,tick) if setup_candidates is None else setup_candidates
    side=plan.get('side')
    entry=plan.get('entry')
    if side not in (-1,1) or not isinstance(entry,(int,float)) or entry<=0:
        return {'accepted':False,'reason':'PLAYBOOK_INVALID_PLAN','playbook':None}
    regime = market_regime or organic.classify(snapshot)
    direct = plan.get('entry_route') == 'STRATEGY_EVENT'
    proof = plan.get('anchor_proof',{}) if direct else {}
    for signal in setups:
        if direct and (signal.family != proof.get('source_strategy') or
                       signal.signal_ts != proof.get('strategy_signal_ts') or
                       signal.event_ts != proof.get('strategy_event_ts')):
            continue
        if signal.side!=side:
            continue
        if snapshot.now-signal.signal_ts>STEP or snapshot.now<signal.signal_ts:
            continue
        if abs(entry-signal.zone)>max(4*tick,.65*signal.atr):
            continue
        if side*(snapshot.price-entry)<=tick:
            continue
        if side*(entry-signal.invalidation)<=0:
            continue
        # The entire invalidating swing must stay beyond the new limit stop.
        stop=plan.get('stop',0)
        if not isinstance(stop,(int,float)) or side*(signal.invalidation-stop)<-2*tick:
            continue
        regime_verdict = organic.qualify(signal, snapshot, tick, regime)
        if not regime_verdict['accepted']:
            continue
        return {'accepted':True,'reason':'PLAYBOOK_'+signal.family,
                'playbook':signal.family,'zone':round(signal.zone,8),
                'event_ts':signal.event_ts,'signal_ts':signal.signal_ts,
                'evidence':signal.note, **regime_verdict}
    return {'accepted':False,'reason':'NO_FRESH_CAUSAL_3M_OR_15M_PLAYBOOK','playbook':None}
