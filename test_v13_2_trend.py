"""Causality and execution regressions for the symmetric v13.2 trend paths."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import bzu_policy
import entry_adverse_selection as adverse
import full_limit_engine as engine
import market_wave as wave
import scenario_planner
import strategy_direct_entries
from test_v12_24_momentum import NOW, STEP
from test_v12_40_execution import base_fixture


class TrendBreakout(TestCase):
    @staticmethod
    def mirrored(rows, side):
        if side==1:
            return rows
        return [replace(b,open=200-b.open,high=200-b.low,low=200-b.high,close=200-b.close)
                for b in rows]

    def breakout_fixture(self, side, timeframe, event_volume=132):
        snap,cfg = base_fixture()
        step = STEP if timeframe=='15m' else wave.M5
        count = 1 if timeframe=='15m' else 2
        known = NOW if count==1 else NOW+2*step
        bars = [engine.Candle(known-(35-i)*step,100.,100.24,99.76,100.,100.)
                for i in range(35)]
        bars[-5] = replace(bars[-5],high=100.31)
        event = [(100.09,100.61,100.05,100.55)] if count==1 else [
                  (100.09,100.41,100.05,100.38),(100.38,100.61,100.34,100.55)]
        for i,ohlc in enumerate(event):
            bars[i-count] = engine.Candle(known-(count-i)*step,*ohlc,event_volume)
        history = self.mirrored(bars,side)
        m15 = history if count==1 else snap.candles['15m']
        c = history[-1].close
        candles = {'15m':m15,'1m':[],'5m':history if count==2 else []}
        snap = replace(snap,now=known,ticker_ts=known,price=c,bid=c-.01,ask=c+.01,candles=candles)
        return snap,cfg

    def test_newest_swing_supersedes_old_opposite_impulse(self):
        snapshot,cfg = base_fixture()
        rows = [engine.Candle(NOW-(36-i)*STEP,93.2,93.45,93.0,93.22,100.) for i in range(36)]
        rows[23] = replace(rows[23],low=91.6)
        closing = [94.12,94.85,95.0,94.61,93.83,93.36,93.02,92.85,92.3]
        for i, close in enumerate(closing):
            rows[i-9] = replace(rows[i-9],open=close+.05,high=close+.20,low=close-.12,close=close)
        for side in (1,-1):
            candles = self.mirrored(rows,-side)
            snap = replace(snapshot,price=candles[-1].close,candles={'15m':candles})
            original = {'state':'BULLISH_ACTIVE' if side==-1 else 'BEARISH_ACTIVE',
                        'side_1h':-side,'side_4h':-side,'active_structure':{'side':-side}}
            directions = (93.36,93.64) if side==-1 else (106.64,106.36)
            with self.subTest(side=side), patch.object(engine.policy,'market_regime',return_value=original), \
                 patch.object(engine.policy,'trend_references',return_value=directions), \
                 patch.object(wave,'displacement_override',return_value={}), \
                 patch.object(wave,'early_structure_reclaim',return_value={}), \
                 patch.object(wave,'breakdown_zones',return_value=[]), \
                 patch.object(wave,'bos_impulse_zones',return_value=[]):
                regime = engine.policy.execution_regime(snap,cfg,engine)
                self.assertEqual(regime['trend_anchor_side'],side)
                self.assertEqual(regime['wave_phase'],'BEARISH_IMPULSE' if side==-1 else 'BULLISH_IMPULSE')
                self.assertEqual(regime['structure_sync']['proof'],wave.RECENT_SWING)

    def test_bos_volume_and_completed_bars_in_both_directions(self):
        for timeframe in ('15m','5m'):
            for side in (1,-1):
                price_references = (100.,100.)
                with self.subTest(timeframe=timeframe,side=side), \
                     patch.object(engine.policy,'trend_references',return_value=price_references):
                    snap,cfg = self.breakout_fixture(side,timeframe)
                    zone, = wave.bos_impulse_zones(snap,cfg.tick,engine)
                    proof = zone['wave_proof']
                    self.assertEqual((zone['kind'],zone['side'],proof['timeframe']),
                                     (wave.BOS_IMPULSE,side,timeframe))
                    self.assertEqual(zone['known_ts'],snap.now)
                    self.assertAlmostEqual(proof['relative_volume'],1.32)
                    self.assertLess(proof['reference_known_ts'],snap.now)
                    self.assertFalse(wave.bos_impulse_zones(replace(snap,now=snap.now-1),cfg.tick,engine))
                    threshold,_ = self.breakout_fixture(side,timeframe,110)
                    self.assertTrue(wave.bos_impulse_zones(threshold,cfg.tick,engine))
                    low_volume,_ = self.breakout_fixture(side,timeframe,109)
                    self.assertFalse(wave.bos_impulse_zones(low_volume,cfg.tick,engine))

    def test_wide_quote_cap_and_overheated_signal(self):
        for side in (1,-1):
            with self.subTest(side=side), patch.object(engine.policy,'trend_references',return_value=(100.,100.)):
                snap,cfg = self.breakout_fixture(side,'15m')
                zone, = wave.bos_impulse_zones(snap,cfg.tick,engine)
                boundary = zone['reference']
                atr15 = engine.atr(snap.candles['15m'][:-1])
                trigger = adverse.trend_execution_trigger(snap,side,zone,boundary,.95,
                    boundary+side*.60*atr15,cfg.tick,engine)
                self.assertIsNotNone(trigger)
                self.assertAlmostEqual(trigger['max_distance'],.65*atr15)
                self.assertIsNone(adverse.trend_execution_trigger(snap,side,zone,boundary,.95,
                    boundary+side*.66*atr15,cfg.tick,engine))
                hot = {**zone,'wave_proof':{**zone['wave_proof'],'impulse_range':2.31*atr15}}
                with patch.object(wave,'bos_impulse_zones',return_value=[hot]):
                    self.assertIsNone(adverse.trend_execution_trigger(
                        snap,side,hot,boundary,.95,snap.price,cfg.tick,engine))

    def test_market_rr_uses_unchanged_targets(self):
        snap,cfg = base_fixture()
        anchor = type('Anchor', (), {'side':'LONG'})()
        geometry = {'tp1':94.34,'tp2':94.90,'path_clear':True,'structural_tp1':True,'structural_tp2':True}
        with patch.object(engine.scenario_planner,'structure_levels',return_value=[]), \
             patch.object(engine.scanner,'target_crossed',return_value=False):
            result, reason = bzu_policy.protected_targets({},snap,anchor,cfg,engine,93.,92.,1,.5,True,
                market_trigger={'pattern':'TREND_CONTINUATION_MARKET','known_ts':snap.now},
                reference_geometry=geometry)
        self.assertIsNone(result)
        self.assertEqual(reason,'BLOCKED_LOW_RR_135')

    def test_aligned_short_at_range_floor_uses_structure_checks(self):
        snap,cfg = base_fixture()
        snap = replace(snap,price=92.3)
        regimen = {'state':'SIDEWAYS','trend_strength':.20}
        levels = {'range':{'discount':92.6,'premium':94.5},
                  'SHORT':{'poi_zones':[{'low':92.3,'high':92.6}]}}
        with patch.object(scenario_planner,'build',return_value=levels), \
             patch.object(engine.policy,'trend_references',return_value=(93.36,93.64)):
            self.assertIsNone(scenario_planner.entry_error(snap,cfg,engine,-1,92.5,{},trend=True,regime=regimen))

    def test_fvg_reserves_proximal_edge_as_soon_as_third_bar_closes(self):
        snap,cfg = base_fixture()
        rows = [engine.Candle(NOW-3*STEP,99.7,99.9,99.6,99.8),
                engine.Candle(NOW-2*STEP,99.8,99.9,99.3,99.4),
                engine.Candle(NOW-STEP,99.4,99.48,99.2,99.25)]
        zone, = wave._trend_fvgs(rows,-1,STEP,cfg.tick,engine)
        self.assertEqual(zone['known_ts'],NOW)
        entry,basis = strategy_direct_entries.adaptive_limit(snap,zone,{},-1,cfg.tick,engine)
        self.assertEqual((entry,basis),(99.48,'STRUCTURAL_REFERENCE'))

    def test_fast_ema9_retest_confirms_on_m5(self):
        snap,cfg = base_fixture()
        m15 = [engine.Candle(NOW-(30-i)*STEP,100.,100.12,99.88,100.,100.)
               for i in range(30)]
        known = NOW+wave.M5
        m5 = [engine.Candle(known-(10-i)*wave.M5,99.80,99.86,99.70,99.80,100.)
              for i in range(10)]
        m5[-1] = replace(m5[-1],open=99.96,high=100.01,low=99.73,close=99.75)
        snap = replace(snap,now=known,price=99.75)
        with patch.object(engine.policy,'trend_references',return_value=(100.1,100.2)):
            zone = wave.fast_trend_retest(snap,m15,m5,-1,.50,cfg.tick,engine)
            self.assertIsNone(wave.fast_trend_retest(replace(snap,now=known-1),m15,m5,-1,.50,cfg.tick,engine))
        self.assertEqual((zone['kind'],zone['reference'],zone['wave_proof']['retest_source']),
                         (wave.VWAP_CONTINUATION,100.,'EMA9'))
