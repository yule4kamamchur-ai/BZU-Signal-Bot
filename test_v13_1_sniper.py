"""Entry and settlement regressions for Brent v13.1."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import bzu_policy
import entry_adverse_selection as adverse
import full_limit_engine as engine
import market_wave
import strategy_direct_entries
from test_full_limit_engine import order
from test_v12_24_momentum import NOW, STEP
from test_v12_40_execution import base_fixture


class Sniper(TestCase):
    def test_market_radius_and_better_quote_are_symmetric(self):
        for side in (1,-1):
            distal = 93.70-side*.15
            for move, allowed in ((-.16,False),(-.076,False),(-.075,True),(.075,True),(.076,False),(.17,False)):
                quote = 93.70+side*move
                self.assertIs(engine.scanner.pullback_quote_allowed(side,93.70,distal,quote,.50),allowed)

    def test_market_keeps_original_stop_and_targets_for_rr_validation(self):
        snapshot, cfg = base_fixture()
        anchor = type('Anchor', (), {'side':'LONG'})()
        entry, stop = 93.69, 92.81
        planned = {'tp1':94.97,'tp2':95.36,'path_clear':True,'structural_tp1':True,'structural_tp2':True}
        with patch.object(engine, 'full_targets', side_effect=AssertionError('Reprojected targets')), \
             patch.object(engine.scenario_planner,'structure_levels',return_value=[]), \
             patch.object(engine.scanner, 'target_crossed',return_value=False):
            quote_plan,_ = bzu_policy.protected_targets({},snapshot,anchor,cfg,engine,
                entry+.02,stop,1,.50,True,reference_geometry=planned)
            denied, reason = bzu_policy.protected_targets({},snapshot,anchor,cfg,engine,
                entry+.23,stop,1,.50,True,reference_geometry=planned)
        self.assertEqual((quote_plan['tp1'],quote_plan['tp2']),(94.97,95.36))
        self.assertIsNone(denied)
        self.assertEqual(reason,'BLOCKED_LOW_RR_135')

    def test_sr_and_vwap_use_fixed_level_buffer(self):
        snapshot,cfg = base_fixture()
        for kind in ('SR_FLIP_RECLAIM','RECLAIM_BREAKER',market_wave.VWAP_CONTINUATION):
            for side in (1,-1):
                zone = {'kind':kind,'reference':93.70,'low':93.55,'high':93.85}
                level, basis = strategy_direct_entries.adaptive_limit(snapshot,zone,{},side,cfg.tick,engine)
                self.assertAlmostEqual(level,93.70+side*.03)
                self.assertEqual(basis,'STRUCTURAL_REFERENCE_BUFFER')

    def test_base_and_trend_filter_require_closed_references(self):
        snapshot,cfg = base_fixture()
        with patch.object(engine.policy,'trend_references',return_value=(93.65,93.92)):
            self.assertEqual(engine.policy.trend_entry_error(snapshot,1,92.85,{'kind':market_wave.BASE}),
                             'BASE_AGAINST_VWAP_EMA20')
            self.assertEqual(engine.policy.trend_entry_error(snapshot,1,93.75,{'kind':market_wave.BASE}),
                             'BASE_AGAINST_VWAP_EMA20')
            self.assertEqual(engine.policy.trend_entry_error(snapshot,1,92.85,{'kind':'FVG_BULL'}),
                             'AGAINST_VWAP_EMA20')
            self.assertIsNone(engine.policy.trend_entry_error(snapshot,-1,92.85,{'kind':market_wave.BASE}))
            zone={'kind':market_wave.SNIPER_REVERSAL,'sweep':{'side':1,'pattern':'M5_SWEEP_ENGULFING'}}
            self.assertIsNone(engine.policy.trend_entry_error(snapshot,1,92.85,zone))
        with patch.object(engine.policy,'trend_references',return_value=(93.60,None)):
            self.assertEqual(engine.policy.trend_entry_error(snapshot,1,94.,{'kind':market_wave.BASE}),
                             'BASE_TREND_REFERENCES_MISSING')

    def test_pullback_planner_keeps_limit_when_quote_chases(self):
        snapshot, cfg = base_fixture()
        level = 93.69
        snapshot = replace(snapshot,price=93.84,bid=93.83,ask=93.85)
        zone = {'kind':'FVG_BULL','side':1,'low':93.50,'high':93.65,
                'known_ts':snapshot.now-STEP,'timeframe':'15m'}
        with engine.market_clock(cfg.core,snapshot.now):
            anchor = cfg.core.make_anchor('PULLBACK_CONTINUATION','LONG',level,93.50,'FVG_BULL','test',
                                          {'price':snapshot.price,'atr15':.50},
                                          evidence={'entry_zone':zone})
        origin = {'confirmed':True,'wave_confirmed':True,'break_known_ts':zone['known_ts']}
        regime = {'state':'BULLISH_ACTIVE','side_1h':1,'side_4h':1,'trend_anchor_side':1}
        with patch.object(engine,'atr',return_value=.50), \
             patch.object(engine.scanner,'zone_origin',return_value=origin), \
             patch.object(engine.policy,'execution_regime',return_value=regime), \
             patch.object(engine.policy,'trend_references',return_value=(92.,92.)), \
             patch.object(bzu_policy.scenario_planner,'entry_error',return_value=None), \
             patch.object(bzu_policy.scenario_planner,'structure_levels',return_value=[]), \
             patch.object(bzu_policy.oil_risk,'stop_geometry',return_value=(92.81,.88,
                 {'floor':.65,'cap':1.15,'gate':None,'boundary':93.50,'buffer':.12})), \
             patch.object(bzu_policy.adverse,'approach_velocity',return_value={'blocked':False}), \
             patch.object(bzu_policy.adverse,'m5_reaction_market_trigger',
                          return_value={'pattern':'M5_ZONE_ENGULFING','known_ts':snapshot.now}), \
             patch.object(bzu_policy.zones,'first_test',return_value={'mitigated':False,'touched':False}), \
             patch.object(engine,'full_targets',return_value=[]):
            plan, reason = bzu_policy.plan(anchor,{},snapshot,cfg,{},engine)
        self.assertIsNotNone(plan, reason)
        self.assertEqual((plan['order_type'],plan['entry']),('LIMIT',level))
        self.assertIsNone(plan['market_trigger'])

    def test_closed_m5_sweep_engulfing_before_m15_and_35_percent_retest(self):
        snapshot,cfg = base_fixture()
        known = NOW+market_wave.M5
        raid = engine.Candle(known-2*market_wave.M5,93.40,93.42,93.09,93.19)
        confirm = engine.Candle(known-market_wave.M5,93.18,93.51,93.15,93.47)
        snapshot = replace(snapshot,now=known,ticker_ts=known,price=93.47,trusted=True)
        with patch.object(market_wave.ict,'session_liquidity',
                          return_value={'pools':[{'name':'DAY_LOW','known_ts':raid.ts-STEP,'level':93.20}]}), \
             patch.object(engine.policy,'day_extrema',return_value=[]), \
             patch.object(market_wave.oil_risk,'macro_extrema',return_value=[]):
            zone, = list(market_wave.sniper_reversals(snapshot,[raid,confirm],cfg.tick,engine,.50))
            self.assertEqual(zone['reference'],93.37)
            self.assertEqual(zone['known_ts'],known)
            early = replace(snapshot,now=known-1,ticker_ts=known-1)
            self.assertFalse(list(market_wave.sniper_reversals(early,[raid,confirm],cfg.tick,engine,.50)))
            self.assertEqual(adverse.sniper_reversal_trigger(snapshot,1,zone,93.37,.8,93.48,.5,cfg.tick)
                             ['pattern'],'M5_SNIPER_REVERSAL')
            self.assertIsNone(adverse.sniper_reversal_trigger(snapshot,1,zone,93.37,.8,93.56,.5,cfg.tick))

    def test_scale_out_uses_weighted_realized_r_in_exported_journal(self):
        _,cfg = base_fixture()
        cfg = replace(cfg,include_commission=False,slippage_bps=0)
        for side in (1,-1):
            plan = order(side, placed=NOW)
            plan.update(action_type='ACTIVE_ENTRY', policy=bzu_policy.POLICY,
                        scale_policy=bzu_policy.SCALE_POLICY, tp1=100+side*1.45, tp2=100+side*1.90,
                        partials={'TP1':.5,'TP2':.5},
                        entry_legs=[{'name':'L1','price':100,'fraction':1.,'status':'PENDING'}])
            state = engine.new_state(cfg)
            minute = 60_000
            state['active'] = bzu_policy.first_fill(
                plan,engine.Candle(NOW,100,100.1,99.9,100),engine,cfg,minute)
            prices = sorted((100+side*.4,100+side*1.05))
            bzu_policy.manage(state,engine.Candle(NOW+minute,100+side*.4,prices[1],prices[0],100+side*.9),
                              cfg,engine,minute)
            prices = sorted((100+side*.15,100+side*.50))
            bzu_policy.manage(state,engine.Candle(NOW+2*minute,100+side*.40,prices[1],prices[0],100+side*.2),
                              cfg,engine,minute)
            with TemporaryDirectory() as folder:
                path = Path(folder)/'journal.json'
                engine.export_journal(path,state)
                closed, = engine.read_json(path)['full_v12']['trades']
            self.assertTrue(closed['partial_tp1_hit'])
            self.assertEqual(closed['close_reason'],'STOP')
            self.assertAlmostEqual(closed['realized_r'],.575)
            self.assertAlmostEqual(closed['net_r'],.575)
