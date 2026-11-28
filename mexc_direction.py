"""v15.3 causal local-wave / transition research features. No fitted probabilities.
All timestamps are bar OPEN times in ms. Use only fully closed prefixes.
"""
from __future__ import annotations
from statistics import mean

M1_AGE_MS = 180_000
M5_AGE_MS = 600_000


def trigger_age_limit(tf):
    return M1_AGE_MS if tf == 'M1' else M5_AGE_MS


def closed_prefix(rows, step, cutoff):
    return [b for b in rows if b.ts + step <= cutoff]


def contiguous(rows, step, count):
    tail = rows[-count:]
    return len(tail) == count and all(b.ts-a.ts == step for a,b in zip(tail,tail[1:]))


def scale(rows, n=14):
    r=rows[-(n+1):]
    return mean(max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(r,r[1:])) if len(r)>1 else 0.


def wave(rows, n, step, *, floor=1., efficiency_floor=.48):
    if not contiguous(rows, step, n+1):
        return dict(side=0, move_atr=0., efficiency=0., directional_bars=0)
    tail=rows[-(n+1):]; vol=scale(rows[:-1]) or scale(rows)
    changes=[b.close-a.close for a,b in zip(tail,tail[1:])]
    net=sum(changes); path=sum(map(abs,changes)); eff=abs(net)/path if path else 0.
    direction=1 if net>0 else -1 if net<0 else 0
    count=sum(direction*x>0 for x in changes)
    normalized=abs(net)/vol if vol else 0.
    side=direction if direction and normalized>=floor and eff>=efficiency_floor and count>=max(2,(n+1)//2) else 0
    return dict(side=side,move_atr=round(normalized,3),efficiency=round(eff,3),directional_bars=count)


def context(m5,m15):
    w5=wave(m5,4,300000,floor=1.05); w15=wave(m15,3,900000,floor=.75)
    wslow=wave(m5,8,300000,floor=1.35)
    a=scale(m5[:-1]); breaks=[]
    # Recent breaks of a pre-existing range, not a level constructed after the event.
    for i in range(max(6,len(m5)-3),len(m5)):
        if not contiguous(m5[:i+1],300000,7) or not a: continue
        c=m5[i]; pre=m5[i-6:i]
        for s in (1,-1):
            level=max(x.high for x in pre) if s==1 else min(x.low for x in pre)
            if s*(c.close-level)>=.08*a and s*(c.close-c.open)>=.40*a:
                # A subsequent close back through the level cancels this local shift.
                if all(s*(x.close-level)>=-.20*a for x in m5[i+1:]):
                    breaks.append(dict(side=s,level=level,ts=c.ts))
    def supported(s):
        return w5['side']==s and (w15['side']==s or wslow['side']==s or any(x['side']==s for x in breaks))
    side=next((s for s in (1,-1) if supported(s)),0)
    return dict(side=side,m5=w5,m15=w15,m5_slow=wslow,breaks=breaks)


def relative_strength(asset15, btc15):
    """Matched one-hour close-to-close return spread, scaled by asset M15 ATR/price.
    This is relative strength, NOT an estimated beta or calibrated probability.
    """
    if not asset15 or not btc15: return {'available':False,'side':0,'relative_atr':None,'asset_return_pct':None,'btc_return_pct':None}
    a={b.ts:b for b in asset15}; b={x.ts:x for x in btc15}
    latest=max(set(a)&set(b),default=-1)
    ts=[latest-i*900000 for i in range(4,-1,-1)]
    if latest!=asset15[-1].ts or latest!=btc15[-1].ts or not all(t in a and t in b for t in ts):
        return {'available':False,'side':0,'relative_atr':None,'asset_return_pct':None,'btc_return_pct':None}
    start_a=a[ts[0]].close; start_b=b[ts[0]].close; vol=scale(asset15)
    if min(start_a,start_b,vol)<=0:
        return {'available':False,'side':0,'relative_atr':None,'asset_return_pct':None,'btc_return_pct':None}
    ar=a[latest].close/start_a-1; br=b[latest].close/start_b-1
    rel=(ar-br)/(vol/start_a)
    return dict(available=True,side=1 if rel>=.65 else -1 if rel<=-.65 else 0,
                relative_atr=round(rel,3),asset_return_pct=round(ar*100,3),btc_return_pct=round(br*100,3))


def detect_transitions(rows, *, side, atr, tick, min_rvol, context_now, higher_opposes, timeframe):
    """Three additional mirrored M5 families. Local structure/volume evidence required.
    No directional trigger from HTF disagreement alone. M1 used separately for timing.
    """
    if timeframe!='M5' or atr<=0 or not contiguous(rows,300000,22): return []
    c=rows[-1]; p=rows[-2]; pre=rows[-7:-1]
    level=max(x.high for x in pre) if side==1 else min(x.low for x in pre)
    body=side*(c.close-c.open); width=max(tick,c.high-c.low)
    strength=side*(c.close-(c.high+c.low)/2)/width
    avg=mean(x.volume for x in rows[-21:-1]); rv=c.volume/avg if avg>0 else 0.
    if body<.35*atr or strength<.12: return []
    strong_price=body>=1.15*atr and strength>=.25 and side*(c.close-level)>=.18*atr
    if rv<max(.85,.75*min_rvol) and not (strong_price and rv>=.65): return []
    prior=wave(rows[:-1],4,300000,floor=.75)
    fresh_break=side*(c.close-level)>=.08*atr and body>=.5*atr
    supported=context_now['side']==side
    w5=context_now['m5']['side']==side
    w15=context_now['m15']['side']==side
    anchor=min(b.low for b in rows[-3:])-tick if side==1 else max(b.high for b in rows[-3:])+tick
    result=[]
    def add(name,family,label,base,retest=None):
        result.append(dict(setup=name,family=family,setup_ua='M5 '+label,base_score=base,
            structural_stop=anchor,reference=c.close,trigger_ts=c.ts,trigger_tf='M5',rvol=rv,
            retest_level=retest,transition_evidence=True))
    # First structural break against the old direction, or reversal of the recent local leg.
    if fresh_break and (prior['side']==-side or higher_opposes) and (w5 or w15 or body>=.85*atr) and (rv>=min_rvol or strong_price and rv>=.65):
        add('EARLY_STRUCTURE_SHIFT','EARLY_STRUCTURE_SHIFT','рання зміна структури',78,level)
    # Multi-bar countertrend wave: support from two horizons or a still-valid structural break.
    if higher_opposes and supported and (fresh_break or (side*(c.close-p.close)>=.22*atr and side*(p.close-rows[-4].close)>=.30*atr)) and rv>=.95:
        add('COUNTERTREND_WAVE','COUNTERTREND_WAVE','локальна контртрендова хвиля',77,level if fresh_break else None)
    # A separate closed break followed by fresh expansion / retest. Never invent a break from the current bar.
    for i in range(len(rows)-4,len(rows)-1):
        br=rows[i]; earlier=rows[i-7:i]
        lev=max(x.high for x in earlier) if side==1 else min(x.low for x in earlier)
        if side*(br.close-lev)<.10*atr or side*(br.close-br.open)<.45*atr: continue
        after=rows[i+1:]
        touch=c.low<=lev+.28*atr if side==1 else c.high>=lev-.28*atr
        follow=side*(c.close-p.close)>=.15*atr
        if side*(c.close-lev)>=.10*atr and (touch or follow) and all(side*(x.close-lev)>=-.20*atr for x in after) and (supported or w5) and rv>=.95:
            add('BREAKUP_CONTINUATION' if side==1 else 'BREAKDOWN_CONTINUATION','STRUCTURE_CONTINUATION','продовження після пробою',78,lev)
            break
    return result


def signal_event_key(symbol, side, known):
    # A M1 and M5 confirmation in the same five-minute bucket are one opportunity.
    return f'{symbol}:{side}:EVENT:{(known-1)//300000}'


def delayed_guard(thesis, side, known, m1, m5, now_ms, a5, tick):
    """Validate intervening CLOSED bars; don't reuse a broken or already spent thesis.
    For the older M5 bar require the entire closed M1 interval after confirmation.
    """
    later=[b for b in m1 if known<=b.ts and b.ts+60000<=now_ms]
    older=thesis['trigger_tf']=='M5' and now_ms-known>=300000
    if older:
        end=(now_ms//60000)*60000
        expected=list(range(known,end,60000))
        if [b.ts for b in later]!=expected: return 'DELAYED_DATA_GAP'
    anchor=thesis['structural_stop']; ref=thesis['reference']
    for b in later:
        if (b.low<=anchor if side==1 else b.high>=anchor): return 'INVALIDATED_AFTER_TRIGGER'
    # Also use closed M5 extremes to catch any invalidation in the intervening bar.
    for b in m5:
        if known<=b.ts and b.ts+300000<=now_ms and (b.low<=anchor if side==1 else b.high>=anchor):
            return 'INVALIDATED_AFTER_TRIGGER'
    if older and any(side*((b.high if side==1 else b.low)-ref)>max(2*tick,1.25*a5) for b in later):
        return 'DELAYED_WAVE_SPENT'
    return None
