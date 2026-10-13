"""Finite-horizon scenario forecast from CLOSED native OHLC blocks.

This is an uncalibrated price model of TP1-before-SL conditional on entry.
It is NOT an empirical win rate, portfolio PnL model, or fill probability.
Whole contiguous blocks preserve observed candle ranges and local sequencing;
when a simulated bar crosses both barriers we conservatively count SL first.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

SCHEMA = 'closed_ohlc_block_forecast_v1'
TF = {'3m':180_000,'15m':900_000,'1H':3_600_000,'4H':14_400_000}
MICRO = {'15m':'3m','1H':'15m','4H':'1H'}
PATHS = 512
LOOKBACK = 80
MINIMUM_BARS = 30
MAXIMUM_STEPS = 480


def forecast(plan, snapshot, cfg):
    tf=plan.get('setup_timeframe',plan.get('timeframe','15m'))
    tf=tf if tf in MICRO else '15m'
    micro=MICRO[tf]
    closed=[c for c in snapshot.candles.get(micro,[]) if c.confirmed and c.ts+TF[micro]<=snapshot.now][-LOOKBACK:]
    entry=plan.get('entry',plan.get('level'))
    stop=plan.get('initial_stop',plan.get('stop'))
    target=plan.get('tp1',plan.get('tp'))
    side=plan.get('side')
    base={'schema':SCHEMA,'calibrated':False,'empirical_win_rate':None,
          'conditional_on':'ENTRY_AT_LIMIT','target':'TP1_BEFORE_INITIAL_SL',
          'source_timeframe':micro,'closed_bars':len(closed)}
    if side not in (-1,1) or any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in (entry,stop,target)):
        return {**base,'status':'NEEDS_SL_TP'}
    if side*(entry-stop)<=0 or side*(target-entry)<=0:
        return {**base,'status':'INVALID_BARRIERS'}
    if len(closed)<MINIMUM_BARS:
        return {**base,'status':'NEEDS_CLOSED_HISTORY','required_bars':MINIMUM_BARS}
    minutes=plan.get('max_hold_minutes',cfg.max_hold_minutes*(1 if tf=='15m' else 4 if tf=='1H' else 16))
    if plan.get('intraday',tf=='15m'):
        local=datetime.fromtimestamp(snapshot.now/1000,ZoneInfo(cfg.timezone))
        end=local.replace(hour=cfg.session_end_hour,minute=0,second=0,microsecond=0)
        minutes=min(minutes,max(0,(end.timestamp()*1000-snapshot.now)/60_000))
    steps=int(minutes*60_000//TF[micro])
    if steps<1 or steps>MAXIMUM_STEPS:
        return {**base,'status':'INVALID_HORIZON'}
    vectors=[]
    for previous,c in zip(closed,closed[1:]):
        vectors.append((c.open/previous.close,c.high/c.open,c.low/c.open,c.close/c.open))
    payload={'schema':SCHEMA,'rows':[(c.ts,c.open,c.high,c.low,c.close) for c in closed],
             'side':side,'entry':entry,'stop':stop,'tp1':target,'steps':steps,'paths':PATHS}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    prior=plan.get('price_forecast',{})
    if prior.get('fingerprint')==fingerprint:
        return prior
    rng=random.Random(int(fingerprint[:16],16))
    wins=losses=ambiguous=0
    for _ in range(PATHS):
        price=entry;index=rng.randrange(len(vectors));remaining=0
        for __ in range(steps):
            if remaining==0:
                index=rng.randrange(len(vectors));remaining=4
            gap,high_ratio,low_ratio,close_ratio=vectors[index]
            opening=price*gap;high=opening*high_ratio;low=opening*low_ratio
            price=opening*close_ratio
            stop_hit=low<=stop if side==1 else high>=stop
            tp_hit=high>=target if side==1 else low<=target
            if stop_hit:
                losses+=1;ambiguous+=int(tp_hit)
                break
            if tp_hit:
                wins+=1
                break
            index=(index+1)%len(vectors);remaining-=1
    timeouts=PATHS-wins-losses
    return {**base,'status':'MODEL_ONLY','fingerprint':fingerprint,
            'as_of_ts':snapshot.now,'last_closed_ts':closed[-1].ts+TF[micro],
            'entry':entry,'stop':stop,'tp1':target,'horizon_minutes':steps*TF[micro]//60_000,
            'paths':PATHS,'tp1_paths':wins,'sl_paths':losses,'timeout_paths':timeouts,
            'ambiguous_paths':ambiguous,'tp1_before_sl':wins/PATHS,
            'stop_before_tp1':losses/PATHS,'neither_within_horizon':timeouts/PATHS,
            'limitations':['Uncalibrated; recent market regime may change',
                           'Conditional on entry; no limit-fill probability',
                           'TP1 barrier only; not partial-exit win rate',
                           'Both barriers in one simulated candle counted as SL']}
