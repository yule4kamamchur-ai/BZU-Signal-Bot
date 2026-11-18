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
from test_pending_limit_lifecycle import waiting, MINUTE


class OrganicExecution(TestCase):
    def test_touch_requires_a_recent_closed_wick_and_reclaim_both_sides(self):
        zone = {'known_ts':NOW-5*MINUTE,'low':89.48,'high':89.52}
        def candle(side, start=NOW-MINUTE, confirmed=True, adverse=False):
            o,h,l,c = (89.55,89.56,89.48,89.51) if adverse else (89.51,89.56,89.46,89.54)
            if side == -1:
                o,h,l,c = 179.-o,179.-l,179.-h,179.-c
            return engine.Candle(start,o,h,l,c,confirmed=confirmed)

        for side in (1,-1):
            with self.subTest(side=side):
                price = 89.50+side*.06
                s = replace(snapshot(NOW,price=price), bid=price-.01, ask=price+.01)
                self.assertIsNone(scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01))
                for bar in (candle(side,confirmed=False),candle(side,adverse=True),
                            candle(side,start=NOW-2*MINUTE),candle(side,start=NOW)):
                    s.candles['1m']=[bar]
                    self.assertIsNone(scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01))
                s.candles['1m']=[candle(side)]
                trigger = scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01)
                self.assertEqual(trigger['pattern'],'INSTANT_TOUCH_MARKET_FILL')
                self.assertEqual(trigger['known_ts'],NOW)
                self.assertEqual(trigger['confirmation_timeframe'],'1m')
                self.assertIsNone(scanner.instant_touch_trigger(replace(s,trusted=False),side,zone,89.50,1.,.40,.01))
                far=89.50+side*.08
                self.assertIsNone(scanner.instant_touch_trigger(replace(s,bid=far-.01,ask=far+.01),
                                                               side,zone,89.50,1.,.40,.01))
                s.candles.pop('1m')
                s.candles['5m']=[candle(side,start=NOW-5*MINUTE)]
                self.assertEqual(scanner.instant_touch_trigger(s,side,zone,89.50,1.,.40,.01)
                                 ['confirmation_timeframe'],'5m')

    def test_limit_keeps_ema9_and_turtle_soup_reference_as_quote_moves(self):
        cfg = engine.Config(tick=.01)
        for kind in ('EMA9_MOMENTUM_SURF','TURTLE_SOUP','FAILED_BREAKOUT_TRAP','SR_FLIP_RECLAIM'):
            with self.subTest(kind=kind):
                zone = {'kind':kind,'low':89.46,'high':89.62,'reference':89.54}
                levels = [strategy_direct_entries.adaptive_limit(snapshot(NOW,price),zone,{},1,cfg.tick,engine)
                          for price in (89.57,89.72,90.)]
                self.assertEqual(len(set(levels)),1)

    def test_pending_organic_limit_ignores_entry_requalification(self):
        cfg = engine.Config(tick=.01)
        for side in (1,-1):
            with self.subTest(side=side):
                plan=waiting(side,NOW-2*MINUTE)
                plan.update(entry_zone={'kind':'FVG_BULL' if side==1 else 'FVG_BEAR','side':side},
                            invalidation=plan['entry']-side*.50)
                s=snapshot(NOW,price=plan['entry']+side*.13)
                state=engine.new_state(cfg)
                engine.park_pending(state,[plan])
                with patch.object(engine.policy,'trend_entry_error',return_value='AGAINST_VWAP_EMA20') as ema, \
                     patch.object(scenario_planner,'entry_error',return_value='BLOCKED_OUTSIDE_HTF_POI') as poi, \
                     patch.object(engine.oil_risk,'structural_room',return_value=(90.,.90)) as room, \
                     patch.object(engine.entry_adverse_selection,'approach_velocity',return_value={'blocked':True}) as velocity:
                    engine.review_waiting_conditions(state,plan,s,cfg)
                    self.assertEqual(engine.pending_plans(state),[plan])
                    self.assertIsNone(engine.pending_entry_gate(plan,[],s,cfg))
                    self.assertFalse(engine.hold_pending_approach(plan,s,cfg))
                    for check in (ema,poi,room,velocity):
                        check.assert_not_called()
                breached=replace(s,price=plan['invalidation']-side*.01)
                engine.review_waiting_conditions(state,plan,breached,cfg)
                self.assertEqual(engine.pending_plans(state),[])
                self.assertEqual(state['orders'][-1]['reason'],'STRUCTURE_INVALIDATED_NOW')

    def test_pending_limit_fills_when_price_approaches_then_touches_in_replay(self):
        cfg = engine.Config(tick=.01)
        for side in (1,-1):
            with self.subTest(side=side):
                plan=waiting(side,NOW)
                price=plan['entry']
                plan.update(invalidation=price-side*.6,queue_execution_policy='PASSIVE_ARM_V2',
                            entry_zone={'kind':'DEMAND_OB' if side==1 else 'SUPPLY_OB','side':side})
                opening, close=price-side*.03,price-side*.02
                bar=engine.Candle(NOW, opening, price+.06,price-.06,close)
                s=engine.Snapshot(NOW+MINUTE,close,{'1m':[bar]})
                state=engine.new_state(cfg)
                engine.park_pending(state,[plan])
                with patch.object(scenario_planner,'entry_error',return_value='BLOCKED_OUTSIDE_HTF_POI'), \
                     patch.object(engine.policy,'trend_entry_error',return_value='AGAINST_VWAP_EMA20'):
                    engine.advance_lifecycle(state,s,cfg)
                self.assertEqual(state['orders'][-1]['status'],'FILLED')
                self.assertEqual(engine.active_plans(state)[0]['entry'],price)

    def test_new_limit_and_touch_market_both_need_135(self):
        cfg = engine.Config(tick=.01)
        anchor = object()
        s = snapshot(NOW,price=90.)
        for active,obstacle,expected in (
            (False,1.34,'BLOCKED_NO_ROOM_TO_STRUCTURE'),(False,1.35,'ACCEPTED'),
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
        s.candles['1m']=[engine.Candle(NOW-MINUTE,92.02,92.09,91.99,92.06)]
        candidate['entry_zone']['known_ts']=NOW-2*MINUTE
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
