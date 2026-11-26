"""Chronological paper execution. Stop updates become effective AFTER the processed bar.
M1 OHLC cannot reveal intraminute order; partial bars use only post-entry close.
"""
from __future__ import annotations
import math


def advance(active, item, now, *, round_price, legacy_cursor=0):
    side=int(active['side']); entry=float(active['entry']); risk=float(active['risk']); tp=float(active['tp'])
    stop=float(active['stop']); tick=float(item.get('tick') or active.get('tick') or .001)
    pending=active.get('phase')=='PENDING_LIMIT'
    start=int(active.get('placed_ts' if pending else 'opened_ts',now))
    if 'processed_until_ts' not in active:
        # Never replay a legacy trade against a stop whose historical effective time is unknown.
        active['processed_until_ts']=max(start,int(legacy_cursor or start)); active['quality']='LEGACY_CURSOR_MIGRATION'
    cursor=int(active['processed_until_ts']); peak=float(active.get('peak_r',0.))
    quality=active.get('quality','OBSERVED_M1')
    if cursor>now:
        return None
    # Old synthetic callers/legacy snapshots may have only M5; production never falls back.
    m1='candles_1m' in item
    step=60000 if m1 else 300000
    if not m1: quality='LEGACY_M5'
    bars={int(c['t'])*1000:c for c in (item.get('candles_1m') if m1 else item.get('candles_5m') or []) or []}
    quote=float(item.get('bid' if side==1 else 'ask') or item.get('price') or entry)
    quote_ts=int(item.get('quote_ts',now)); quote_ok=item.get('quote_valid',not m1) and 0<=now-quote_ts<=60000 and quote_ts>=start and quote>0
    expires=int(active.get('expires_ts',start+8*60000))
    def finish(reason, price, ts):
        active.update(stop=stop,peak_r=peak,processed_until_ts=cursor,quality=quality)
        return {'reason':reason,'exit':price,'closed_ts':ts,'quality':quality}
    def fill(ts):
        nonlocal pending, start, quality
        pending=False; start=ts; quality='AMBIGUOUS_LIMIT_FILL' if quality=='OBSERVED_M1' else quality
        active.update(phase='OPEN',opened_ts=ts,opened_kyiv=None,peak_r=0.,be_armed=False)
    def cross(lo,hi,o,ts):
        # Stop-first if both levels touched; adverse gaps execute at open, not the old stop.
        stop_hit=lo<=stop if side==1 else hi>=stop
        tp_hit=hi>=tp if side==1 else lo<=tp
        if stop_hit:
            if tp_hit and quality=='OBSERVED_M1':
                active['ambiguous_ohlc']=True
            return ('BE_STOP' if active.get('be_armed') else 'STOP',min(o,stop) if side==1 else max(o,stop))
        if tp_hit: return ('TAKE_PROFIT',tp)
        return None
    for ts,c in sorted(bars.items()):
        end=ts+step
        if end<=cursor or end>now: continue
        if ts>cursor:
            quality='DATA_GAP'; active['data_gap']=True
        partial=ts<cursor or ts<start
        try: o,h,l,cl=map(float,(c['o'],c['h'],c['l'],c['c']))
        except (KeyError,ValueError,TypeError):
            quality='DATA_GAP'; continue
        if not all(math.isfinite(v) for v in (o,h,l,cl)) or not 0<l<=min(o,cl)<=max(o,cl)<=h:
            quality='DATA_GAP'; continue
        if partial:
            # Only close is known to occur after the entry/cursor. Intrabar extremes may precede entry.
            if quality=='OBSERVED_M1':
                # If the entire overlapping minute stayed away from all relevant barriers,
                # no unknowable pre-entry excursion can affect this lifecycle.
                could_touch=(l<=stop or h>=tp or h>=entry+.9*risk) if side==1 else (h>=stop or l<=tp or l<=entry-.9*risk)
                if pending: could_touch=True
                if could_touch: quality='PARTIAL_ENTRY_BAR'
            o=h=l=cl
            active['partial_entry_bar']=True
        if pending:
            if ts>=expires or end>expires:
                return finish('LIMIT_EXPIRED',entry,expires)
            touched=l<=entry if side==1 else h>=entry
            if touched and end<=expires:
                fill(end)
                # If adverse extreme crosses stop in the fill bar, take the conservative loss.
                if (l<=stop if side==1 else h>=stop):
                    cursor=end; return finish('STOP',min(o,stop) if side==1 else max(o,stop),end)
                cursor=end
                continue # No assumed TP in a bar with unknown fill ordering.
            if (h>=tp if side==1 else l<=tp):
                return finish('LIMIT_MISSED_TARGET',entry,end)
            cursor=end; continue
        hit=cross(l,h,o,end)
        cursor=end
        if hit: return finish(hit[0],hit[1],end)
        if end>=start+18*3600000: return finish("TIME_EXIT",cl,end)
        peak=max(peak,(h-entry)/risk if side==1 else (entry-l)/risk)
        # This update is at bar close and can only affect later bars/quotes.
        lock=.55 if peak>=1.40 else .05 if peak>=.90 else None
        if lock is not None:
            new=round_price(entry+side*lock*risk,tick)
            if side*(new-stop)>0:
                stop=new; active.update(be_armed=True,stop_effective_ts=end)
    if now-cursor>step*2:
        quality='DATA_GAP'; active['data_gap']=True
    if pending:
        if now>=expires: return finish('LIMIT_EXPIRED',entry,expires)
        # An observable executable quote may fill a resting paper limit.
        if quote_ok and quote_ts>=cursor and quote_ts>start:
            fill_quote=float(item.get('ask' if side==1 else 'bid') or quote)
            if side*(fill_quote-entry)<=0:
                fill(quote_ts); cursor=max(cursor,quote_ts)
                # Quote already beyond invalidation: record immediate adverse exit, not a free option.
                if side*(quote-stop)<=0:
                    return finish('STOP',quote,quote_ts)
    elif quote_ok and quote_ts>=cursor and quote_ts>start:
        # Quote is evaluated against the stop effective at quote time. Do not advance the bar cursor:
        # the still-open minute will be processed when it closes.
        if side*(quote-stop)<=0:
            if quote_ts>cursor and quality=='OBSERVED_M1': quality='QUOTE_EXIT_UNOBSERVED_INTRABAR'
            return finish('BE_STOP' if active.get('be_armed') else 'STOP',quote,quote_ts)
        if side*(quote-tp)>=0:
            if quote_ts>cursor and quality=='OBSERVED_M1': quality='QUOTE_EXIT_UNOBSERVED_INTRABAR'
            return finish('TAKE_PROFIT',tp,quote_ts)
        if now-start>=18*3600000: return finish('TIME_EXIT',quote,quote_ts)
    active.update(stop=stop,peak_r=peak,processed_until_ts=cursor,quality=quality,
                  current_price=quote if quote_ok else active.get('current_price',entry))
    return None
