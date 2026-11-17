"""v14.2 regression tests for touch execution and structural hysteresis."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import bzu_policy
import full_limit_engine as engine
import scenario_planner
import setup_scanner as scanner
import strategy_direct_entries
from test_full_limit_engine import NOW, snapshot
from test_pending_limit_lifecycle import waiting


class OrganicExecution(TestCase):
    def test_touch_uses_executable_quote_and_atr_band_both_sides(self):
        zone = {'known_ts':NOW-60_000}
        for side in (1,-1):
            with self.subTest(side=side):
                price = 89.50+side*.08
                s = replace(snapshot(NOW,price=price), bid=price-.01, ask=price+.01)
                self.assertIsNone(scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01))
                price = 89.50+side*.06
                s = replace(s,price=price,bid=price-.01,ask=price+.01)
                trigger = scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01)
                self.assertEqual(trigger['pattern'],'INSTANT_TOUCH_MARKET_FILL')
                self.assertEqual(trigger['limit_entry'],89.50)
                self.assertIsNone(scanner.instant_touch_trigger(replace(s,trusted=False),side,zone,89.50,1.,.40,.01))

    def test_limit_keeps_ema9_and_turtle_soup_reference_as_quote_moves(self):
        cfg = engine.Config(tick=.01)
        for kind in ('EMA9_MOMENTUM_SURF','TURTLE_SOUP','FAILED_BREAKOUT_TRAP','SR_FLIP_RECLAIM'):
            with self.subTest(kind=kind):
                zone = {'kind':kind,'low':89.46,'high':89.62,'reference':89.54}
                levels = [strategy_direct_entries.adaptive_limit(snapshot(NOW,price),zone,{},1,cfg.tick,engine)
                          for price in (89.57,89.72,90.)]
                self.assertEqual(len(set(levels)),1)

    def test_pending_hysteresis_is_measured_from_fixed_entry(self):
        cfg = engine.Config(tick=.01)
        order = waiting(side=1)
        order['entry_zone'] = {'kind':'FVG_BULL','side':1}
        s = snapshot(NOW, price=order['entry']+.13)
        for room,expected in ((1.39,None),(1.16,None),(1.15,None),(1.149,'PENDING_ROOM_BELOW_1_15')):
            with self.subTest(room=room), patch.object(engine.policy,'trend_entry_error',return_value=None), \
                 patch.object(scenario_planner,'entry_error',return_value=None), \
                 patch.object(scenario_planner,'structure_levels',return_value=[]), \
                 patch.object(engine.oil_risk,'structural_room',return_value=(None,room)) as measure:
                self.assertEqual(engine.pending_scenario_error(order,s,cfg),expected)
                self.assertEqual(measure.call_args.args[:3],(1,order['entry'],order['risk']))

    def test_new_limit_needs_140_while_touch_market_needs_135(self):
        cfg = engine.Config(tick=.01)
        anchor = object()
        s = snapshot(NOW,price=90.)
        for active,obstacle,expected in (
            (False,1.39,'BLOCKED_NO_ROOM_TO_STRUCTURE'),(False,1.40,'ACCEPTED'),
            (True,1.34,'BLOCKED_NO_ROOM_TO_STRUCTURE'),(True,1.35,'ACCEPTED'),
        ):
            with self.subTest(active=active,obstacle=obstacle), \
                 patch.object(engine.oil_risk,'structural_room',return_value=(None,obstacle)), \
                 patch.object(scenario_planner,'structure_levels',return_value=[]), \
                 patch.object(scanner,'target_crossed',return_value=False):
                geom={'tp1':91.45,'tp2':91.95,'path_clear':True,'structural_tp1':True,'structural_tp2':True}
                _,reason=bzu_policy.protected_targets({},s,anchor,cfg,engine,90.,89.,1,.5,active,
                                                       reference_geometry=geom)
                self.assertEqual(reason,expected)

    def test_range_middle_block_and_confirmed_breakout_exception(self):
        cfg = engine.Config(tick=.01)
        s = snapshot(NOW,price=90.)
        zone={'kind':'FVG_BULL','known_ts':NOW}
        with patch.object(scenario_planner,'build',return_value={'range':{'low':89.,'high':91.}}):
            self.assertEqual(scenario_planner.market_range_error(s,cfg,engine,1,90.,zone,{'state':'SIDEWAYS'}),
                             'MARKET_BLOCKED_RANGE_MIDDLE')
            self.assertIsNone(scenario_planner.market_range_error(s,cfg,engine,1,89.25,zone,{'state':'SIDEWAYS'}))
            self.assertIsNone(scenario_planner.market_range_error(s,cfg,engine,1,90.,zone,{'state':'BULLISH_ACTIVE'}))
            step=engine.TF['15m']
            s.candles['15m']=[engine.Candle(NOW-(100-i)*step,89.4,89.8,89.0,89.5) for i in range(99)]
            s.candles['15m'].append(engine.Candle(NOW-step,89.69,90.04,89.6,90.0))
            proven={'kind':scanner.wave.BOS_IMPULSE,'known_ts':NOW}
            self.assertIsNone(scenario_planner.market_range_error(s,cfg,engine,1,90.,proven,{'state':'SIDEWAYS'}))

    def test_router_executes_touch_as_market_instead_of_parking_limit(self):
        from test_full_limit_engine import analysis
        from test_pending_limit_lifecycle import TrendReplacement

        fixture=TrendReplacement()
        cfg=fixture.cfg
        candidate=fixture.candidates(waiting(1),1)
        s=snapshot(NOW,price=92.04)
        s.candles['15m']=[replace(c,open=92.04,high=92.29,low=91.79,close=92.04)
                          for c in s.candles['15m']]
        ask=s.ask
        reference=92.04
        proof=scanner.instant_touch_trigger(s,1,candidate['entry_zone'],reference,1.,
                                             engine.atr(s.candles['15m']),cfg.tick)
        self.assertIsNotNone(proof)
        candidate.update(action_type='ACTIVE_ENTRY',order_type='MARKET',entry=ask,
                         stop=ask-1,tp1=ask+1.36,tp2=ask+2,tp=ask+2,risk=1.,
                         market_trigger=proof,
                         entry_legs=[{'name':'ACTIVE','price':ask,'fraction':1.,'status':'PENDING'}])
        book=engine.new_state(cfg)
        with patch.object(engine,'analyze',return_value=analysis([candidate])), \
             patch.object(engine.policy,'execution_regime',return_value={'state':'BULLISH_ACTIVE','trend_anchor_side':1}), \
             patch.object(engine.policy,'trend_entry_error',return_value=None), \
             patch.object(scenario_planner,'entry_error',return_value=None), \
             patch.object(engine.oil_risk,'structural_room',return_value=(None,1.36)), \
             patch.object(scanner,'calculate_zone_trade_plan',return_value={'tactic':'MARKET','entry_condition':'RETEST'}), \
             patch.object(scenario_planner,'focus',return_value={'developing_setup':None}):
            result=engine.run_cycle(book,s,cfg)
        self.assertEqual(result['action'],'ACTIVE_ENTRY')
        self.assertEqual(engine.pending_plans(book),[])
        self.assertEqual(book['orders'][-1]['reason'],'INSTANT_TOUCH_MARKET_FILL')
        self.assertEqual(engine.active_plans(book)[0]['entry'],ask)
