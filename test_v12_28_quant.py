"""v12.28 transparent structural pre-plans, last-candle brake and hybrid routing."""
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import setup_scanner as scanner
import live_terminal
import entry_adverse_selection as adverse
from test_v12_24_momentum import fixture, target_for as legacy_targets, NOW


def target_for(side):
    entry = 74.50 if side == 1 else 75.50
    return [{'level': entry + side * .78, 'kind': 'TEST_TP1'}, *legacy_targets(side)]


class ZonePlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg=engine.Config(core=core,tick=.01)
        self.snap=engine.Snapshot(NOW,100.,{},99.99,100.01,True,NOW,'BZ-USDT-SWAP')
        for tf,step in engine.TF.items():
            self.snap.candles[tf]=[engine.Candle(NOW-(30-i)*step,100.,100.25,99.75,100.,100.) for i in range(30)]
        self.zone={'kind':'FVG_BULL','side':1,'timeframe':'15m','low':98.8,'high':99.,'known_ts':NOW}

    def plan(self,zone=None,side=1,**kwargs):
        with patch.object(engine,'atr',return_value=.5):
            return scanner.calculate_zone_trade_plan(zone or self.zone,self.snap,self.cfg,engine,side,targets=kwargs.pop('targets',[]),**kwargs)

    def test_exact_narrow_entry_distal_stop_and_fallback_targets_both_sides(self):
        for side in (1,-1):
            z=self.zone if side==1 else {**self.zone,'kind':'FVG_BEAR','side':-1,'low':101.,'high':101.2}
            p=self.plan(z,side)
            self.assertAlmostEqual(p['entry'],99.03 if side==1 else 100.97)
            self.assertAlmostEqual(p['stop'],98.67 if side==1 else 101.33)
            self.assertGreaterEqual(p['rr1'],1.50-1e-8)
            self.assertGreaterEqual(p['rr2'],2.20-1e-8)
            self.assertEqual(p['tp1_source'],'R_MULTIPLE_1_50')
            self.assertGreater(side*(p['entry']-p['stop']),0)

    def test_wide_zone_uses_equilibrium_without_an_invented_distal_third(self):
        p=self.plan({**self.zone,'low':98.,'high':99.})
        self.assertTrue(p['wide_zone'])
        self.assertAlmostEqual(p['entry'],98.5)
        self.assertEqual(p['entry_basis'],'EQUILIBRIUM')
        self.assertAlmostEqual(p['stop'],97.87)

    def test_five_score_components_add_up_and_are_not_a_probability(self):
        p=self.plan()
        self.assertEqual(p['score'],80)
        self.assertEqual([c['maximum'] for c in p['score_breakdown']],[25,20,20,20,15])
        self.assertEqual(p['score'],sum(c['earned'] for c in p['score_breakdown']))
        self.assertEqual(p['score_source'],'ZONE_HEURISTIC')
        self.assertNotIn('probability',p)
        htf=self.plan({**self.zone,'timeframe':'1H'})
        self.assertEqual(htf['score'],100)

    def test_grade_b_plus_has_complete_geometry_and_reaction_tactic(self):
        p=self.plan({**self.zone,'kind':'DEMAND_OB'})
        self.assertEqual(p['score'],60)
        self.assertEqual(p['grade'],'B+')
        self.assertEqual(p['tactic'],'REACTION_M5')
        self.assertTrue(all(isinstance(p[k],(int,float)) for k in ('entry','stop','tp1','tp2','rr1')))

    def test_tested_and_missing_history_do_not_earn_freshness_points(self):
        z={**self.zone,'known_ts':NOW-900_000}
        self.snap.candles['15m'][-1]=engine.Candle(NOW-900_000,99.1,99.3,98.95,99.2)
        self.assertEqual(self.plan(z)['freshness'],'TESTED')
        self.assertEqual(next(c for c in self.plan(z)['score_breakdown'] if c['criterion']=='UNTESTED')['earned'],0)
        unknown=self.plan({**self.zone,'low':97.8,'high':98.,'known_ts':NOW-100*900_000})
        self.assertEqual(unknown['freshness'],'UNKNOWN_HISTORY')

    def test_hard_barrier_blocks_target_projection(self):
        p=self.plan(targets=[{'level':99.15,'kind':'NEAR_LIQUIDITY','known_ts':NOW,'hard_barrier':True}])
        self.assertGreaterEqual(p['rr1'],1.50-1e-8)
        self.assertEqual(p['risk_gate'],'INSUFFICIENT_ROOM')
        self.assertEqual(p['tactic'],'OBSERVE')
        self.assertEqual(next(c for c in p['score_breakdown'] if c['criterion']=='ROOM_TO_RUN')['earned'],0)

    def test_existing_complete_geometry_is_frozen_and_partial_geometry_is_not_spliced(self):
        old={'entry':99.01,'stop':98.65,'tp1':99.7,'tp2':100.1}
        p=self.plan(geometry=old)
        self.assertEqual({k:p[k] for k in old},old)
        self.assertEqual(p['plan_basis'],'ENGINE_GEOMETRY')
        partial=self.plan(geometry={'entry':99.01,'stop':90.})
        self.assertAlmostEqual(partial['entry'],99.03)
        self.assertAlmostEqual(partial['stop'],98.67)

    def test_future_targets_and_open_candles_do_not_change_the_preplan(self):
        before=self.plan()
        self.snap.candles['15m'].append(engine.Candle(NOW,100.,120.,80.,90.,100.,False))
        after=self.plan(targets=[{'level':99.2,'known_ts':NOW+1}])
        for k in ('entry','stop','tp1','tp2','score'):self.assertEqual(before[k],after[k])

    def test_invalid_direction_atr_and_future_zone_are_not_filled_with_fake_numbers(self):
        self.assertIsNone(self.plan({**self.zone,'side':-1}))
        self.assertIsNone(self.plan({**self.zone,'known_ts':NOW+1}))
        with patch.object(engine,'atr',return_value=0):
            self.assertIsNone(scanner.calculate_zone_trade_plan(self.zone,self.snap,self.cfg,engine))

    def test_distant_zone_is_planned_but_cannot_win_an_actionable_radar(self):
        p=self.plan({**self.zone,'kind':'FVG_BEAR','side':-1,'low':110.,'high':110.2},-1)
        self.assertEqual(p['risk_gate'],'DISTANT_ZONE')
        self.assertIsInstance(p['tp1'],float)

    def test_all_exported_zones_have_full_plans_without_creating_orders_or_leaking_secrets(self):
        book=engine.new_state(self.cfg);before=copy.deepcopy(book)
        obj=SimpleNamespace(kind='FVG_BEAR',side='SHORT',timeframe='1H',low=100.8,high=101.,created_ts=NOW-2*engine.TF['1H'],mitigated=False)
        analysis={'full_context':{'zones':{'fvg':[obj]}},'entry_mode':'HYBRID','private':'SECRET'}
        with patch.object(engine.policy,'market_regime',return_value={'state':'BEARISH_ACTIVE'}),patch.object(engine,'atr',return_value=.5):
            out=live_terminal.build(book,self.snap,{'action':'WATCH','analysis':analysis},self.cfg,engine)
        self.assertTrue(out['zones'])
        for z in out['zones']:
            self.assertTrue(all(isinstance(z[k],(int,float)) for k in ('entry','stop','tp1','tp2','score','rr1')))
            self.assertFalse(z['execution_admitted'])
        self.assertIsNone(out['radar']['SHORT'])
        self.assertEqual(book,before)
        self.assertFalse(out['orders'])
        self.assertNotIn('SECRET',json.dumps(out))
        # A legacy order retains its own ledger score, while the zone score still sums correctly.
        old={'id':'legacy','side':-1,'status':'PENDING','entry':100.77,'stop':101.13,'risk':.36,
             'tp1':100.2,'tp2':99.9,'score':53,'setup':'LEGACY','entry_zone':{'kind':'FVG_BEAR',
             'side':-1,'timeframe':'1H','low':100.8,'high':101.,'known_ts':NOW}}
        with patch.object(engine.policy,'market_regime',return_value={'state':'BEARISH_ACTIVE'}),patch.object(engine,'atr',return_value=.5),patch.object(engine,'open_plans',return_value=[old]):
            managed=live_terminal.build(book,self.snap,{'action':'WATCH','analysis':analysis},self.cfg,engine)
        self.assertEqual(managed['orders'][0]['score'],53)
        for z in managed['zones']:self.assertEqual(z['score'],sum(c['earned'] for c in z['score_breakdown']))


class BrakeTests(unittest.TestCase):
    def snapshot(self,side=1):
        rows=[engine.Candle(NOW-(21-i)*900_000,101.,101.5,100.5,101.) for i in range(20)]
        rows.append(engine.Candle(NOW-900_000,102.1,102.12,99.88,99.9))
        zone={'low':99.95,'high':100.1,'known_ts':NOW-2*900_000};stop=99.7
        if side==-1:
            rows=[engine.Candle(c.ts,200-c.open,200-c.low,200-c.high,200-c.close) for c in rows]
            zone={'low':99.9,'high':100.05,'known_ts':zone['known_ts']};stop=100.3
        return engine.Snapshot(NOW,rows[-1].close,{'15m':rows}),zone,stop

    def test_dominant_adverse_candle_near_stop_blocks_both_directions(self):
        for side in (1,-1):
            s,z,stop=self.snapshot(side)
            result=adverse.approach(s,1.,side,z,.01,stop=stop)
            self.assertTrue(result['blocked'])
            self.assertTrue(result['near_stop'])
            self.assertGreater(result['range_atr15'],1.8)

    def test_normal_pullback_far_stop_and_large_rejection_wick_are_not_knives(self):
        s,z,stop=self.snapshot()
        self.assertFalse(adverse.approach(s,1.,1,z,.01,stop=98.5)['blocked'])
        s.candles['15m'][-1]=engine.Candle(NOW-900_000,101.,101.1,99.8,99.9)
        self.assertFalse(adverse.approach(s,1.,1,z,.01,stop=stop)['blocked'])
        s.candles['15m'][-1]=engine.Candle(NOW-900_000,102.,102.1,99.,100.2)
        self.assertFalse(adverse.approach(s,1.,1,z,.01,stop=100.)['blocked'])

    def test_two_moderate_candles_are_not_combined_into_a_shock(self):
        s,z,stop=self.snapshot()
        s.candles['15m'][-2]=engine.Candle(NOW-1_800_000,102.3,102.4,101.,101.1)
        s.candles['15m'][-1]=engine.Candle(NOW-900_000,101.1,101.2,99.8,99.9)
        self.assertFalse(adverse.approach(s,1.,1,z,.01,stop=stop)['blocked'])

    def test_future_or_unclosed_candle_cannot_activate_a_brake(self):
        s,z,stop=self.snapshot();s.candles['15m'][-1]=engine.Candle(NOW,102.1,102.12,99.88,99.9,0.,False)
        self.assertFalse(adverse.approach(s,1.,1,z,.01,stop=stop)['blocked'])


class HybridTests(unittest.TestCase):
    def test_admitted_a_plan_can_place_a_paper_limit_in_advance(self):
        s,cfg,_=fixture();book=engine.new_state(cfg)
        with patch.dict(os.environ,{'BZU_ENTRY_MODE':'HYBRID'}),patch.object(engine,'full_targets',return_value=target_for(1)):
            out=engine.run_cycle(book,s,cfg)
        self.assertEqual(out['action'],'PLACE_LIMIT')
        self.assertTrue(engine.pending_plans(book))
        self.assertTrue(all(p['zone_quality']['score']>=65 for p in engine.pending_plans(book)))

    def test_b_plus_route_does_not_place_a_blind_paper_limit(self):
        s,cfg,_=fixture();book=engine.new_state(cfg);original=scanner.calculate_zone_trade_plan
        def b_plus(*args,**kwargs):
            p=original(*args,**kwargs)
            return {**p,'score':60,'grade':'B+','tactic':'REACTION_M5'} if p else None
        with patch.dict(os.environ,{'BZU_ENTRY_MODE':'HYBRID'}),patch.object(engine,'full_targets',return_value=target_for(1)),patch.object(scanner,'calculate_zone_trade_plan',side_effect=b_plus):
            out=engine.run_cycle(book,s,cfg)
        self.assertEqual(out['action'],'WATCH_REACTION')
        self.assertTrue(out['analysis']['reaction_plans'])
        self.assertFalse(engine.open_plans(book))


if __name__=='__main__':unittest.main()
