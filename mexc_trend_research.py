"""v16.0 causal H1 challenger features. Research only; no orders, no veto by direction.
All synthetic HA prices are features, never execution prices, ATR or stop anchors.
"""
from __future__ import annotations
import math
from statistics import mean

PROFILES=('H1_QUALITY','H1_HA','H1_ADAPTIVE_CHANNEL','H1_FLEX_CAPITAL','H1_COMBINED','H1_FLEX_PURE','H1_OIL_CONTEXT')
QUALITY_PROFILES=('H1_QUALITY','H1_COMBINED')
HA_PROFILES=('H1_HA','H1_COMBINED')
CHANNEL_PROFILES=('H1_ADAPTIVE_CHANNEL','H1_COMBINED')
FLEX_PROFILES=('H1_FLEX_CAPITAL','H1_COMBINED','H1_FLEX_PURE')
CORRELATION_PROFILES=('H1_FLEX_CAPITAL','H1_COMBINED')
STRENGTH_RANK_PROFILES=('H1_ADAPTIVE_CHANNEL','H1_FLEX_PURE','H1_OIL_CONTEXT')
LEARN_PROFILES=('H1_COMBINED',)
LIVE_PROFILES={'trend_quality_v160_book':'H1_QUALITY','trend_ha_v160_book':'H1_HA','trend_channel_v160_book':'H1_ADAPTIVE_CHANNEL','trend_flex_v160_book':'H1_FLEX_CAPITAL','trend_combined_v160_book':'H1_COMBINED','trend_flex_pure_v160_book':'H1_FLEX_PURE','trend_oil_v160_book':'H1_OIL_CONTEXT'}


def clamp(x,lo=0.,hi=1.): return min(hi,max(lo,x))


def efficiency(q):
    path=sum(abs(b['c']-a['c']) for a,b in zip(q,q[1:]))
    return (q[-1]['c']-q[0]['c'])/path if path and len(q)>1 else 0.


def range_scale(q):
    return mean(max(b['h']-b['l'],abs(b['h']-a['c']),abs(b['l']-a['c'])) for a,b in zip(q,q[1:])) if len(q)>1 else 0.


def closed(rows,now,step=3600000):
    # Callers also validate OHLC; future/incomplete candles excluded here independently.
    return sorted({int(b['t']):b for b in rows if int(b['t'])*1000+step<=now}.values(),key=lambda b:b['t'])


def channel_period(rows,now):
    """Length chosen from PRE-trigger context only. Fixed predeclared 20/30/40."""
    q=closed(rows,now)[:-1]
    if len(q)<40: return 30
    slow=range_scale(q[-40:]); fast=range_scale(q[-9:]); ratio=fast/slow if slow else 1.
    er=abs(efficiency(q[-21:]))
    if ratio<.8 and er<.4: return 20
    if ratio>1.2 or er>.55: return 40
    return 30


def ha_features(rows,side,now):
    q=closed(rows,now)[-60:]
    if not q or side not in (-1,1): return {'ha_support':0.,'ha_streak':0,'ha_available':False}
    ho=(q[0]['o']+q[0]['c'])/2.; hc=sum(q[0][k] for k in ('o','h','l','c'))/4.
    directions=[]
    for b in q[1:]:
        ho=(ho+hc)/2.;hc=sum(b[k] for k in ('o','h','l','c'))/4.
        directions.append(1 if hc>ho else -1 if hc<ho else 0)
    streak=0
    for d in reversed(directions):
        if d!=side: break
        streak+=1
    last=directions[-3:]
    return {'ha_support':sum(side*d for d in last)/len(last) if last else 0.,'ha_streak':streak,'ha_available':len(q)>=15}


def breakout_features(rows,sig,now,m1=()):
    q=closed(rows,now);s=sig['side'];a=max(sig['atr'],1e-12)
    if len(q)<22 or s not in (-1,1): return {'quality_v160':.5,'breakout_risk':.5,'trend_phase':'UNKNOWN',**ha_features(q,s,now)}
    pre=q[:-1]; last=q[-1]; level=sig['upper'] if s==1 else sig['lower']; prior_atr=range_scale(pre[-15:]) or a
    prev_eff=efficiency(pre[-21:]); short_eff=efficiency(pre[-7:]); slow=range_scale(pre[-41:]) or a
    compression=range_scale(pre[-9:])/slow
    clearance=s*(last['c']-level)/prior_atr
    width=max(last['h']-last['l'],1e-12)
    close_location=(last['c']-last['l'])/width if s==1 else (last['h']-last['c'])/width
    body=s*(last['c']-last['o'])/prior_atr
    # Count ONLY already closed M1 after trigger. Lack of follow-through data is neutral.
    after=[b for b in closed(m1,now,60000) if b['t']*1000>=sig['signal_ts']]
    holds=sum(s*(b['c']-level)>0 for b in after)/len(after) if after else .5
    # A compression/transition has potential, but neither a mature trend nor a reversal is prohibited.
    phase='COMPRESSION_BREAK' if compression<.8 else 'STRUCTURE_TRANSITION' if abs(prev_eff)<.3 or s*prev_eff<0 else 'MATURE_TREND' if s*prev_eff>.55 else 'TREND_EXPANSION'
    # Wide candles, excessive distance and rejection wicks reduce quality smoothly.
    exhaustion=clamp((clearance-1.25)/1.5)*.5+clamp((width/prior_atr-2.5)/2.)*.5
    failure=clamp(.50*(1-close_location)+.30*(1-holds)+.20*clamp(-body))
    quality=clamp(.25*close_location+.20*clamp(clearance/.65)+.15*clamp(body)+.15*holds+.15*clamp(.5+.5*s*(short_eff-prev_eff))+.10*(1-clamp(compression-1))-.20*exhaustion)
    return {'quality_v160':quality,'breakout_risk':failure,'trend_phase':phase,'pre_efficiency':prev_eff,'pre_compression':compression,'clearance_atr':clearance,'breakout_close_location':close_location,'hold_fraction':holds,'post_trigger_closed_m1':len(after),'exhaustion':exhaustion,**ha_features(q,s,now)}


def evidence_weight(profile,features):
    weight=1.
    if profile in QUALITY_PROFILES:
        weight*=.65+.35*clamp(features['quality_v160']*(1-.35*features['breakout_risk']))
    if profile in HA_PROFILES and features.get('ha_available'):
        weight*=.8+.2*clamp((1+features['ha_support'])/2)
    return weight


def flex_budget(total,used,group_used,group,waiting_groups):
    """Lend idle risk; reserve 1/3 to other current eligible groups.
    Per group <= 1/2 and individual <= 1/3 of original total. No risk creation.
    """
    base=total/3.; idle=max(0.,total-used)
    protect=sum(max(0.,base-group_used.get(g,0.)) for g in waiting_groups if g!=group)
    available=max(0.,idle-protect)
    group_max=total*.5
    return max(0.,min(available,group_max-group_used.get(group,0.),base))


def overlap_exposure(symbol,side,positions,rows_by_symbol,correlator,total_risk):
    """Signed correlation risk-weighted by currently reserved dollars; unknown is reported, not zero proof."""
    positive=0.; known=0; unknown=0
    for sym,p in positions.items():
        if sym==symbol: continue
        corr=correlator(rows_by_symbol.get(symbol,[]),rows_by_symbol.get(sym,[]))
        if corr is None: unknown+=1;continue
        known+=1;positive+=max(0.,corr*side*p['side'])*p['reserve_usd']
    return {'overlap':clamp(positive/max(total_risk/3.,1e-12)),'known_pairs':known,'unknown_pairs':unknown}
