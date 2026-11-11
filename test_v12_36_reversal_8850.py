"""Exercise the post-TP and 88.50 reversal paths through the paper book."""
from dataclasses import replace
from datetime import datetime
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import market_wave
import setup_scanner as scanner
from test_v12_24_momentum import NOW, STEP
from test_v12_31_market_wave import snapshot

REGIME = {'state':'BEARISH_ACTIVE','trend_state':'BEARISH_TREND',
          'side_1h':-1,'side_4h':-1,'active_structure':{'side':-1},'local_structure':{'side':-1}}
ENV = {'BZU_ENTRY_MODE':'HYBRID','BZU_EDGE_MODE':'SHADOW','BZU_ENTRY_PLAYBOOK_MODE':'STRICT'}


def reversal_snapshot(price=88.73):
    m15 = [(93.15,93.65,92.90,93.25)]*74 + [
        (90.40,90.55,89.30,89.55), (89.52,89.60,88.54,88.95),
        (88.95,89.63,88.88,89.40), (89.40,89.45,88.98,89.11),
        (89.11,89.15,88.50,88.56), (88.55,89.20,88.54,89.18),
    ]
    snap,cfg = snapshot(price,m15,[],htf_side=-1)
    snap = replace(snap,now=NOW+1_000,ticker_ts=NOW+1_000)
    profile=engine.oil_risk.snapshot_profile(snap)
    profile.update(adr_d1=4.41,adr_distance=.45*4.41)
    return snap,cfg


def known_target(level):
    return [{'level':level, 'kind':'1H_LIQUIDITY', 'timeframe':'1H', 'known_ts':NOW-STEP}]


def filled_short(cfg, ts):
    entry, stop, tp1, tp2 = 89.63,91.62,87.44,85.85
    plan={'id':'incumbent-185','order_id':'incumbent-185','event_key':'incumbent-185','side':-1,
          'entry':entry,'stop':stop,'initial_stop':stop,'risk':1.99,'tp1':tp1,'tp2':tp2,'tp':tp2,
          'version':engine.VERSION,'policy':bzu_policy.POLICY,'include_commission':False,'slippage_bps':0.,
          'action_type':'ACTIVE_ENTRY','setup':'PULLBACK_CONTINUATION','entry_fee_rate':0.,'exit_fee_rate':0.,
          'entry_legs':[{'name':'ACTIVE','price':entry,'fraction':1.,'status':'PENDING'}],
          'partials':{'TP1':.5,'TP2':.5},'placed_ts':ts,'effective_ts':ts,'expires_ts':ts+STEP,
          'execution':'PAPER_SIGNAL'}
    return bzu_policy.first_fill(plan,engine.Candle(ts,entry,entry,entry,entry),engine,cfg,60_000)


class Reversal8850(unittest.TestCase):
    def test_short_185_is_blocked_after_184_tp(self):
        tz=ZoneInfo('Europe/Kyiv')
        stamp=lambda h,m:int(datetime(2026,11,10,h,m,tzinfo=tz).timestamp()*1000)
        close=stamp(10,33)
        prior={'id':'short-184','entry':93.59,'exit':89.68,'closed_ts':close,
               'close_reason':'TAKE_PROFIT','side':-1}
        snap=engine.Snapshot(stamp(10,45),89.63,{})
        self.assertTrue(scanner.continuation_setup('CONTINUATION_PULLBACK',None,None))
        self.assertTrue(scanner.post_tp_exhaustion(-1,snap,[prior],4.41))
        self.assertFalse(scanner.post_tp_exhaustion(1,snap,[prior],4.41))
        self.assertFalse(scanner.post_tp_exhaustion(-1,replace(snap,price=91.18),[prior],4.41))
        self.assertFalse(scanner.post_tp_exhaustion(-1,replace(snap,now=close+8*STEP),[prior],4.41))
        zone={'wave_proof':{'leg_start':93.59,'leg_end':89.68}}
        self.assertFalse(adverse.continuation_market_retracement(zone,-1,89.63))
        self.assertTrue(adverse.continuation_market_retracement(zone,-1,91.25))

    def test_adr_exhaustion_can_span_sessions_but_excludes_future_bars(self):
        sweep={'side':1,'raid_ts':NOW-STEP,'extreme':88.50}
        older=engine.Candle(NOW-80*STEP,93.1,93.60,93.,93.2)
        recent=engine.Candle(NOW-2*STEP,90.5,91.1,90.4,90.6)
        future=engine.Candle(NOW,95.,96.,88.,95.)
        self.assertTrue(market_wave.exhausted_adr([older,recent],sweep,4.41))
        self.assertFalse(market_wave.exhausted_adr([recent,future],sweep,4.41))

    def test_double_bottom_can_open_long_during_bearish_regime(self):
        snap,cfg = reversal_snapshot()
        zones=market_wave.zones(snap,cfg,engine,REGIME)
        zone,=[z for z in zones if z['kind']=='MULTIBAR_SWEEP' and z['known_ts']==NOW]
        self.assertEqual((zone['low'],zone['sweep']['pattern']),(88.50,'DOUBLE_TEST_ENGULFING'))
        self.assertTrue(direction.reversal_sweep_override(1,zone,snap,cfg.tick))
        self.assertFalse(direction.reversal_sweep_override(1,zone,replace(snap,now=NOW-1),cfg.tick))
        book=engine.new_state(cfg)
        with patch.object(direction,'execution_regime',return_value=REGIME), \
             patch.object(engine,'full_targets',return_value=known_target(91.39)), patch.dict(os.environ,ENV):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['action'],'ACTIVE_ENTRY')
        plan,=engine.active_plans(book)
        self.assertEqual((plan['side'],plan['source_strategy'],plan['entry'],plan['status']),
                         (1,'MULTIBAR_SWEEP',snap.ask,'OPEN'))
        self.assertGreaterEqual(plan['score'],78)

    def test_profitable_short_reverses_before_new_long_is_recorded(self):
        snap,cfg=reversal_snapshot()
        cfg=replace(cfg,include_commission=False,slippage_bps=0.)
        book=engine.new_state(cfg)
        incumbent=filled_short(cfg,NOW-6*STEP)
        incumbent['mfe_r']=incumbent['max_favorable_r']=.55
        book['active']=incumbent
        with patch.object(direction,'execution_regime',return_value=REGIME), \
             patch.object(engine,'full_targets',return_value=known_target(91.39)), \
             patch.object(engine,'advance_lifecycle'),patch.dict(os.environ,ENV):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['analysis']['portfolio_arbitration']['decision'],'PEAK_REVERSAL_FLIP')
        trade,=book['trades']
        self.assertEqual(trade['close_reason'],'CLOSED_REVERSAL_FLIP_PROFIT')
        self.assertGreater(trade['net_r'],.35)
        plan,=engine.active_plans(book)
        self.assertEqual((plan['side'],plan['source_strategy']),(1,'MULTIBAR_SWEEP'))
        self.assertEqual([ev['kind'] for ev in result['events'] if ev['kind'] in ('CLOSED','FILLED')],
                         ['CLOSED','FILLED'])


    def test_reclaim_breaker_plants_limit_at_recovered_level(self):
        snap,cfg=reversal_snapshot()
        candles={**snap.candles,'15m':[*snap.candles['15m'],
                 engine.Candle(NOW,89.18,90.05,89.16,89.97,200.),
                 engine.Candle(NOW+STEP,89.97,90.02,89.63,89.95,200.)]}
        candles['3m']=[replace(bar,ts=bar.ts+2*STEP,open=89.98,high=90.02,low=89.92,close=89.98)
                       for bar in candles['3m']]
        snap=replace(snap,now=snap.now+2*STEP,ticker_ts=snap.ticker_ts+2*STEP,price=89.98,bid=89.97,ask=89.99,candles=candles)
        engine.oil_risk.snapshot_profile(snap).update(adr_d1=4.41,adr_distance=.45*4.41)
        book=engine.new_state(cfg)
        with patch.object(direction,'execution_regime',return_value=REGIME), \
             patch.object(engine,'full_targets',return_value=known_target(92.43)), patch.dict(os.environ,ENV):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['action'],'PLACE_LIMIT')
        plan,=engine.pending_plans(book)
        self.assertEqual((plan['side'],plan['source_strategy'],plan['entry']), (1,'RECLAIM_BREAKER',89.63))

    def test_bounce_sets_entry_stop_before_90_minutes(self):
        snap,cfg=reversal_snapshot()
        cfg=replace(cfg,include_commission=False,slippage_bps=0.)
        book=engine.new_state(cfg)
        p=filled_short(cfg,NOW-3*STEP)
        p['mfe_r']=p['max_favorable_r']=.45
        book['active']=p
        e=engine.Candle(NOW-60_000,89.05,89.20,89.00,89.18)
        engine.manage_bar(book,e,cfg,step=60_000,snapshot=snap)
        self.assertEqual((p['stop'],p['bounce_stop_ts']),(89.63,NOW))
        stop=engine.Candle(NOW,89.20,89.65,89.12,89.62)
        engine.manage_bar(book,stop,cfg,step=60_000,snapshot=snap)
        trade,=book['trades']
        self.assertEqual((trade['exit'],trade['net_r'],trade['close_reason']),(89.63,0.,'CLOSED_BOUNCE_BREAKEVEN'))
