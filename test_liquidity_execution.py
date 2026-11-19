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

    def test_exact_tick_minute_sweeps_consumed_for_both_sides(self):
        for side in (1, -1):
            with self.subTest(side=side):
                pool = {'level':100., 'side':side, 'known_ts':NOW}
                minute = self.bar(NOW,100.02,100.10,99.99,100.02,side)
                before = engine.Snapshot(NOW+30_000, minute.close, {'1m':[minute]})
                self.assertEqual(limit_structure.unswept_minute_pools(before,[pool],.01),[pool])
                live = replace(before, metadata={'now':before.now, 'current_candle_1m':{
                    'ts':NOW,'low':minute.low,'high':minute.high}})
                self.assertEqual(limit_structure.unswept_minute_pools(live,[pool],.01),[])
                historical = replace(live, now=NOW+20_000)
                self.assertEqual(limit_structure.unswept_minute_pools(historical,[pool],.01),[pool])
                closed = replace(before, now=NOW+60_000)
                self.assertEqual(limit_structure.unswept_minute_pools(closed,[pool],.01),[])

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

    def test_decay_penalty_restarts_contiguous_count_after_gap(self):
        zone = {'low':100., 'high':100.1, 'known_ts':NOW}
        bars = [self.bar(NOW+i*300_000,100.05,100.15,99.98,100.04) for i in range(16) if i != 11]
        snap = replace(snapshot(NOW+16*300_000), candles={'5m':bars})
        decay = signal_quality.consolidation_decay(zone, snap, 1, .01, engine)
        self.assertEqual((decay['bars'], decay['penalty'], decay['requires_confirmation']),(11,0,False))
        bars[:] = [self.bar(NOW+i*300_000,100.05,100.15,99.98,100.04) for i in range(12)]
        decay = signal_quality.consolidation_decay(zone, snap, 1, .01, engine)
        self.assertEqual((decay['bars'], decay['penalty'], decay['requires_confirmation']),(12,10,True))

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

    def test_early_pullback_survives_and_later_mfe_lock_preserves_targets(self):
        for side in (1,-1):
            with self.subTest(side=side):
                plan, _, _ = self.protect_fixture(side)
                plan.update(filled_ts=NOW-60_000, last_bar=NOW-60_000,
                            time_exit_enabled=False, initial_stop=100.-side)
                book=engine.new_state(self.cfg)
                book['active']=plan
                geometry=tuple(plan[k] for k in ('entry','initial_stop','tp1','tp2','risk'))
                progress=self.bar(NOW,100.,100.88,99.85,100.70,side)
                engine.manage_bar(book,progress,self.cfg,step=60_000)
                self.assertIsNone(bzu_policy.protective_amendment(plan))
                pullback=self.bar(NOW+60_000,100.70,100.71,100.05,100.17,side)
                engine.manage_bar(book,pullback,self.cfg,step=60_000)
                self.assertIs(book['active'],plan)
                self.assertEqual(plan['stop'],100.-side)
                book['active']=plan=deepcopy(plan)
                partial=self.bar(NOW+120_000,100.17,101.60,100.12,101.30,side)
                engine.manage_bar(book,partial,self.cfg,step=60_000)
                self.assertAlmostEqual(plan['remaining'],.5)
                self.assertAlmostEqual(plan['max_favorable_r'],1.60)
                self.assertAlmostEqual(bzu_policy.protective_amendment(plan)['price'],100.+side*.35)
                self.assertEqual(tuple(plan[k] for k in ('entry','initial_stop','tp1','tp2','risk')),geometry)
                exit_bar=self.bar(NOW+180_000,100.70,100.72,99.50,99.80,side)
                engine.manage_bar(book,exit_bar,self.cfg,step=60_000)
                closed,=book['trades']
                self.assertEqual(closed['close_reason'],'MFE_PROTECTIVE_EXIT')
                self.assertAlmostEqual(closed['net_r'],.675)

    def test_mfe_close_below_lock_uses_observed_close_before_time_exit(self):
        plan, _, _ = self.protect_fixture()
        plan.pop('scale_policy')
        plan.update(filled_ts=NOW-60_000, last_bar=NOW-60_000,
                    time_exit_enabled=True, exit_deadline=NOW+60_000)
        book=engine.new_state(self.cfg)
        book['active']=plan
        engine.manage_bar(book,self.bar(NOW,100.,101.30,99.85,100.03),self.cfg,step=60_000)
        closed,=book['trades']
        self.assertEqual((closed['close_reason'],closed['exit']),('MFE_PROTECTIVE_EXIT',100.03))

    def test_quote_armed_stop_does_not_use_earlier_wick_of_same_minute(self):
        plan, _, _ = self.protect_fixture()
        plan['filled_ts'] = NOW-60_000
        plan.pop('scale_policy')
        book=engine.new_state(self.cfg)
        book['active']=plan
        before=engine.Snapshot(NOW+20_000,101.30,{},bid=101.30,ask=101.32,ticker_ts=NOW+20_000)
        engine.check_quote_exits(book,before,self.cfg)
        after=replace(before,now=NOW+30_000,ticker_ts=NOW+30_000,price=100.40,bid=100.40,ask=100.42)
        engine.check_quote_exits(book,after,self.cfg)
        self.assertEqual(plan['stop'],99.)
        self.assertEqual(plan['pending_stop']['price'],100.35)
        engine.manage_bar(book,self.bar(NOW,100.,101.32,99.95,100.4),self.cfg,step=60_000)
        self.assertIsNotNone(book['active'])
        engine.manage_bar(book,self.bar(NOW+60_000,100.4,100.5,100.02,100.2),self.cfg,step=60_000)
        closed,=book['trades']
        self.assertEqual((closed['close_reason'],closed['exit']),('MFE_PROTECTIVE_EXIT',100.35))

    def test_mfe_protection_does_not_count_the_fill_candle(self):
        plan, _, _ = self.protect_fixture()
        plan.pop('scale_policy')
        plan.update(fill_time_status='WITHIN_1M_BAR', filled_ts=NOW, last_bar=NOW-60_000,
                    time_exit_enabled=False)
        book=engine.new_state(self.cfg)
        book['active']=plan
        engine.manage_bar(book,self.bar(NOW,100.,101.30,99.90,100.14),self.cfg,fill_bar=True,step=60_000)
        position_monitor.update_trade_excursions(plan,[self.bar(NOW,100.,101.40,99.90,100.14)],
                                                 NOW+900_000)
        self.assertEqual(plan['mfe_r'],0.)
        self.assertNotIn('pending_stop',plan)

    def test_repeated_stops_do_not_extend_the_existing_four_hour_cooldown(self):
        plan = {'score':80, 'quality':{'accepted':True}}
        prior = [{'side':side,'entry':100.,'net_r':-1.,'closed_ts':NOW-i*60_000,'close_reason':'STOP'}
                 for i in (1,2) for side in (-1,1)]
        deadline=NOW-60_000+bzu_policy.ADVERSE_LOCK_BARS*engine.TF['15m']
        for side in (1,-1):
            with self.subTest(side=side), patch.object(engine.policy,'local_structure',return_value={'side':-side}), \
                 patch.object(engine.policy,'structural_flip',return_value={'confirmed':False}), \
                 patch.object(bzu_policy,'fresh_reentry_confirmation',return_value=False) as proof:
                gate=lambda now: bzu_policy.adverse_reentry_reason(
                    side,prior,snapshot(now),self.cfg,engine,plan=plan)
                self.assertEqual(gate(deadline-1),'ADVERSE_STRUCTURE_LOCKOUT')
                proof.reset_mock()
                self.assertIsNone(gate(deadline))
                proof.assert_not_called()

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
