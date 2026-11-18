"""Causal Brent liquidity entries and protection, without external requests."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy
import full_limit_engine as engine
import limit_structure
import position_monitor
import setup_scanner
import signal_quality
import strategy_direct_entries as direct
from test_full_limit_engine import NOW, snapshot, analysis
from test_pending_limit_lifecycle import waiting


class LiquidityExecution(TestCase):
    cfg = engine.Config(core=core, tick=.01, slippage_bps=0)

    def bar(self, ts, o, h, l, c, side=1):
        if side == -1:
            o, h, l, c = 200-o, 200-l, 200-h, 200-c
        return engine.Candle(ts, o, h, l, c, 100, True)

    def front_snapshot(self, side=1):
        first = self.bar(NOW, 100.25, 100.30, 100.10, 100.15, side)
        last = self.bar(NOW+300_000, 100.15, 100.65, 100.14, 100.62, side)
        minutes = [self.bar(NOW+i*60_000,100.25,100.30,100.10,100.15,side) for i in range(5)]
        minutes += [self.bar(NOW+i*60_000,100.15,100.65,100.14,100.62,side) for i in range(5,10)]
        price = 100.63 if side == 1 else 99.37
        snap = replace(snapshot(NOW+600_000, price), candles={'5m':[first,last],'1m':minutes},
                       bid=price-.01, ask=price+.01, ticker_ts=NOW+600_000)
        plan = waiting(side, NOW)
        plan.update(entry=100., stop=100.-side, risk=1., tp1=100.+side*3.,
                    tp2=100.+side*4., tp=100.+side*4.,
                    organic_risk={'model':'ORGANIC_OIL_V32','floor':.65,'cap':2.})
        return plan, snap

    def test_liquidity_anchor_is_symmetric_and_requires_near_unswept_pool(self):
        for side in (1,-1):
            with self.subTest(side=side):
                pool={'level':100.-side*.10,'side':side,'known_ts':NOW-600_000}
                with patch.object(limit_structure,'local_liquidity',return_value=[pool]):
                    anchor=direct.liquidity_anchor(snapshot(),{},side,100.,.8,.5,.01,engine)
                self.assertAlmostEqual(anchor['entry'],100.-side*.13)
                with patch.object(limit_structure,'local_liquidity',return_value=[{**pool,'level':100.-side*.30}]):
                    self.assertIsNone(direct.liquidity_anchor(snapshot(),{},side,100.,.8,.5,.01,engine))

    def test_wick_consumes_liquidity_even_if_candle_closes_back_above(self):
        bars=[self.bar(NOW+i*300_000,100.5,101.,low,100.5)
              for i,low in enumerate([100.1,100.1,99.9,100.1,100.1,99.8])]
        snap=replace(snapshot(NOW+1500_000),candles={'5m':bars})
        self.assertTrue(any(p['level']==99.9 for p in limit_structure.local_liquidity(snap,engine,.01)))
        snap.now+=300_000
        self.assertFalse(any(p['level']==99.9 for p in limit_structure.local_liquidity(snap,engine,.01)))

    def test_decay_requires_real_departure_and_retest_after_hour_in_zone(self):
        bars=[self.bar(NOW+i*300_000,100.05,100.15,99.98,100.04) for i in range(15)]
        zone={'low':100.,'high':100.1,'known_ts':NOW}
        snap=replace(snapshot(NOW+15*300_000),candles={'5m':bars})
        decay=signal_quality.consolidation_decay(zone,snap,1,.01,engine)
        self.assertEqual((decay['penalty'],decay['requires_confirmation']),(15,True))
        bars.extend([self.bar(snap.now,100.15,100.5,100.14,100.4),
                     self.bar(snap.now+300_000,100.12,100.4,100.08,100.35)])
        snap.now+=600_000
        self.assertFalse(signal_quality.consolidation_decay(zone,snap,1,.01,engine)['requires_confirmation'])

    def test_front_run_requires_impulse_no_touch_full_minute_history_and_room(self):
        for side in (1,-1):
            with self.subTest(side=side):
                plan,snap=self.front_snapshot(side)
                trigger=direct.front_run_trigger(plan,snap,self.cfg,engine)
                self.assertEqual(trigger['pattern'],'MARKET_FRONT_RUN_CATCH')
                self.assertEqual(trigger['known_ts'],snap.now)
                candidate=direct.front_run_candidate(plan,snap,self.cfg,engine)
                self.assertEqual(candidate['replaces_order_id'],plan['id'])
                self.assertTrue(setup_scanner.market_plan(candidate))
                self.assertGreaterEqual(candidate['rr1'],1.35)
                bad=deepcopy(snap);bad.candles['1m'][0]=self.bar(NOW,100.25,100.30,99.99,100.25,side)
                self.assertIsNone(direct.front_run_trigger(plan,bad,self.cfg,engine))
                bad=deepcopy(snap);bad.candles['1m'].pop(2)
                self.assertIsNone(direct.front_run_trigger(plan,bad,self.cfg,engine))
                self.assertIsNone(direct.front_run_trigger({**plan,'tp1':100.+side*.8},snap,self.cfg,engine))
                self.assertIsNone(direct.front_run_trigger(plan,replace(snap,trusted=False),self.cfg,engine))

    def test_live_minute_must_be_observed_before_immediate_replacement(self):
        plan,snap=self.front_snapshot()
        snap=replace(snap,now=snap.now+1000,ticker_ts=snap.now+1000)
        self.assertIsNone(direct.front_run_trigger(plan,snap,self.cfg,engine))
        snap.metadata={'current_candle_1m':{'ts':NOW+600_000,'low':100.60,'high':100.65}}
        self.assertIsNotNone(direct.front_run_trigger(plan,snap,self.cfg,engine))

    def protect_fixture(self, side=1):
        plan=waiting(side,NOW)
        plan.update(entry=100.,stop=100.-side,initial_stop=100.-side,risk=1.,status='OPEN',
                    filled_ts=NOW,filled_fraction=1.,remaining=1.,entry_cost=100.,risk_budget=1.,
                    realized_gross_r=0.,realized_exit_fees_r=0.,realized_legs=[],mfe_r=0.,mae_r=0.,
                    entry_fee_rate=0.,entry_legs=[{'status':'FILLED','fraction':1.,'price':100.}],
                    tp1=100.+side*1.5,tp2=100.+side*2.,last_bar=NOW-60_000)
        bars=[self.bar(NOW,100.,100.80,99.95,100.70,side),
              self.bar(NOW+300_000,100.70,100.72,99.98,100.20,side)]
        snap=replace(snapshot(NOW+600_000,100.+side*.20),candles={'5m':bars})
        pool={'side':-side,'level':100.+side*.4,'known_ts':NOW-300_000}
        return plan,snap,pool

    def test_protection_requires_sequential_sweep_profit_and_closed_rejection(self):
        for side in (1,-1):
            with self.subTest(side=side):
                plan,snap,pool=self.protect_fixture(side)
                with patch.object(limit_structure,'local_liquidity',return_value=[pool]):
                    protection=position_monitor.liquidity_protection(plan,snap,snap.now,snap.price,self.cfg,engine)
                    self.assertAlmostEqual(protection['price'],100.+side*.05)
                    self.assertFalse(protection['exit_now'])
                    self.assertIsNone(position_monitor.liquidity_protection(plan,snap,NOW+599_999,snap.price,self.cfg,engine))
                    self.assertIsNone(position_monitor.liquidity_protection(plan,replace(snap,candles={'5m':snap.candles['5m'][:1]}),
                                                                           snap.now,snap.price,self.cfg,engine))
                with patch.object(limit_structure,'local_liquidity',return_value=[]):
                    self.assertIsNone(position_monitor.liquidity_protection(plan,snap,snap.now,snap.price,self.cfg,engine))

    def test_pending_protection_is_causal_monotone_and_survives_reload(self):
        for side in (1,-1):
            with self.subTest(side=side):
                plan,snap,pool=self.protect_fixture(side)
                book=engine.new_state(self.cfg);book['active']=plan
                last=self.bar(snap.now-60_000,100.5,100.7,99.98,100.2,side)
                with patch.object(limit_structure,'local_liquidity',return_value=[pool]), \
                     patch.object(position_monitor,'observe_structure'):
                    engine.manage_bar(book,last,self.cfg,step=60_000,snapshot=snap)
                self.assertIsNotNone(book['active'])
                self.assertEqual(plan['stop'],100.-side)  # Confirmation low cannot hit the future stop.
                restored=deepcopy(plan)
                self.assertIsNotNone(bzu_policy.protective_amendment(restored))
                bzu_policy.apply_protective_stop(restored,snap.now-1)
                self.assertEqual(restored['stop'],100.-side)
                bzu_policy.apply_protective_stop(restored,snap.now)
                self.assertAlmostEqual(restored['stop'],100.+side*.05)
                book['active']=restored
                engine.manage_bar(book,self.bar(snap.now,100.2,100.25,100.,100.01,side),
                                  self.cfg,step=60_000,snapshot=replace(snap,now=snap.now+60_000))
                self.assertIsNone(book['active'])
                self.assertGreater(book['trades'][-1]['net_r'],0)

    def test_router_front_run_cancels_before_filling_at_current_quote(self):
        old,snap=self.front_snapshot()
        old.update(last_bar=snap.now-60_000,
                   entry_zone={'low':99.8,'high':100.1,'known_ts':NOW-300_000,'side':1,
                               'kind':'FVG_BULL','timeframe':'15m'},
                   quality={'accepted':True},readiness_pct=100.)
        book=engine.new_state(self.cfg)
        engine.park_pending(book,[old])
        snap.candles['15m']=snapshot(snap.now).candles['15m']
        market={'state':'BULLISH_ACTIVE','trend_anchor_side':1}
        with patch.object(engine,'analyze',return_value=analysis([])), \
             patch.object(engine,'review_waiting_conditions'), \
             patch.object(engine.policy,'execution_regime',return_value=market), \
             patch.object(engine.policy,'trend_entry_error',return_value=None), \
             patch.object(engine.scenario_planner,'entry_error',return_value=None), \
             patch.object(engine.scenario_planner,'market_range_error',return_value=None), \
             patch.object(engine.oil_risk,'structural_room',return_value=(None,None)), \
             patch.object(setup_scanner,'candidate_plan_error',return_value=None), \
             patch.object(setup_scanner,'calculate_zone_trade_plan',return_value={'tactic':'MARKET','entry_condition':'RETEST'}), \
             patch.object(engine.scenario_planner,'focus',return_value={'developing_setup':None}):
            result=engine.run_cycle(book,snap,self.cfg)
        self.assertEqual(result['action'],'ACTIVE_ENTRY')
        self.assertEqual([row['status'] for row in book['orders']],['CANCELLED','FILLED'])
        self.assertEqual(book['orders'][0]['reason'],'MARKET_FRONT_RUN_CATCH')
        self.assertEqual(engine.active_plans(book)[0]['entry'],snap.ask)
        self.assertEqual(engine.pending_plans(book),[])
