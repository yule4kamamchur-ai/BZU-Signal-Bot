"""Structural risk and the closed-M15 trend entry contract."""
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import entry_adverse_selection as adverse
import full_limit_engine as engine
import live_terminal
import market_wave as wave
import oil_risk
import scenario_planner as planner
from test_v12_31_market_wave import snapshot

STEP = oil_risk.M15


def base_fixture(hold=False):
    rows = [(92.95,93.04,92.85,92.95)]*110 + [
        (92.95,93.12,92.92,93.09),(93.09,93.33,93.06,93.29),(93.29,93.70,93.26,93.63),
        (93.63,93.68,92.82,93.57),(93.57,93.68,93.22,93.58),
        (93.58,93.68,93.24,93.55),(93.55,93.68,93.28,93.59),
        (93.25,93.30,93.25,93.27) if hold else (93.65,93.73,93.62,93.70),
    ]
    snap,cfg=snapshot(rows[-1][-1],rows,[],htf_side=1)
    return replace(snap,bid=snap.price-.01,ask=snap.price+.01),cfg


class CleanExecution(unittest.TestCase):
    def test_m15_stop_precedes_hourly_and_preserves_cap(self):
        snap,cfg=base_fixture()
        profile=oil_risk.snapshot_profile(snap)
        self.assertTrue(profile['atr_15m'])
        for side in (1,-1):
            anchors=[{'level':93.68-side*.86,'timeframe':'15m'},
                     {'level':93.68-side*.62,'timeframe':'1H'}]
            stop,risk,proof=oil_risk.stop_geometry(93.68,93.68-side*.40,side,profile,anchors,cfg.tick,engine)
            self.assertEqual(proof['anchor_source'],'15m')
            self.assertTrue(proof['anchor_valid'])
            self.assertGreater(side*(anchors[0]['level']-stop),0)
            self.assertGreaterEqual(risk,.86)
            self.assertLessEqual(risk,1.25)
            _,_,proof=oil_risk.stop_geometry(93.68,93.68-side*1.24,side,profile,anchors,cfg.tick,engine)
            self.assertEqual(proof['gate'],'STRUCTURAL_STOP_TOO_WIDE')

    def test_range_limit_only_for_weak_sideways_market(self):
        snap,cfg=snapshot(93.68,[(93.0,93.9,90.0,93.0)]*192,[])
        base={'range':{'low':90.0,'high':93.9,'discount':91.17,'premium':92.73},
              'LONG':{'poi_zones':[{'low':93.65,'high':93.75}]}}
        with patch.object(planner,'build',return_value=base):
            for state,strength,expected in (
                ('SIDEWAYS',.34,'BLOCKED_DEALING_RANGE_30_70'),
                ('SIDEWAYS',.35,None),('BULLISH_ACTIVE',.20,None),('SIDEWAYS',None,None),
            ):
                self.assertEqual(planner.entry_error(snap,cfg,engine,1,93.68,{},trend=True,
                    regime={'state':state,'trend_strength':strength}),expected)

    def test_closed_base_breakout_and_midpoint_hold(self):
        for hold in (False,True):
            snap,cfg=base_fixture(hold)
            rows=engine.policy.closed(snap,'15m')
            zone=engine.scanner.consolidation_base(rows,1,cfg.tick,engine)
            self.assertIsNotNone(zone)
            self.assertEqual(zone['kind'],wave.BASE)
            self.assertEqual(zone['wave_proof']['trigger'],'MIDPOINT_HOLD' if hold else 'BREAKOUT')
            trigger=adverse.trend_zone_market_trigger(snap,1,zone,.92,snap.ask,cfg.tick,engine)
            self.assertIsNotNone(trigger)
            self.assertEqual(trigger['pattern'],'M15_TREND_ZONE')
            self.assertIsNone(adverse.trend_zone_market_trigger(snap,1,zone,.92,
                zone['reference']+trigger['max_distance']+.01,cfg.tick,engine))
            early=replace(snap,now=snap.now-1,ticker_ts=snap.now-1)
            self.assertIsNone(adverse.trend_zone_market_trigger(early,1,zone,.92,snap.ask,cfg.tick,engine))

    def test_base_reaches_the_normal_paper_execution_route(self):
        snap,cfg=base_fixture()
        regime={'state':'BULLISH_ACTIVE','side_1h':1,'side_4h':1,'trend_anchor_side':1,
                'active_structure':{'side':1},'trend_phase':'TREND_CONTINUATION'}
        book=engine.new_state(cfg)
        with patch.object(engine.policy,'execution_regime',return_value=regime), \
             patch.object(planner,'structure_levels',return_value=[]), \
             patch.object(engine,'full_targets',return_value=[]), \
             patch.dict(os.environ,{'BZU_ENTRY_MODE':'HYBRID'}):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['action'],'ACTIVE_ENTRY',result['analysis'].get('rejections'))
        trade,=engine.active_plans(book)
        self.assertEqual((trade['entry'],trade['source_strategy'],trade['status']),
                         (snap.ask,wave.BASE,'OPEN'))
        self.assertLessEqual(trade['risk'],oil_risk.STOP_CAP)
        self.assertGreaterEqual(trade['rr1']+1e-8,oil_risk.ENTRY_TP1_MIN)
        public=live_terminal.build(book,snap,result,cfg,engine)
        self.assertEqual(public['orders'][0]['entry'],snap.ask)

    def test_sr_flip_allows_fresh_market_and_rejects_price_chasing(self):
        snap,cfg=base_fixture()
        zone={'kind':'SR_FLIP_RECLAIM','reference':93.68,'low':93.61,'high':93.68,
              'wave_proof':{'break_ts':snap.now-STEP}}
        proof=adverse.trend_zone_market_trigger(snap,1,zone,.9,snap.ask,cfg.tick,engine)
        self.assertIsNotNone(proof)
        self.assertIsNone(adverse.trend_zone_market_trigger(snap,1,zone,.9,93.86,cfg.tick,engine))
