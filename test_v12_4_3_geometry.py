"""User-defined entry-relative SL/TP floors and minimal report icons."""
import copy
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import price_forecast
import setup_scanner as scanner
import signal_quality as quality
from test_full_limit_engine import full_fixture, order, snapshot, analysis


class MinimumDistanceTests(unittest.TestCase):
    def fixture(self,side,entry=100.,tick=.01,tf='15m'):
        cfg,ctx,a,s=full_fixture(side)
        cfg=replace(cfg,tick=tick)
        a.level=entry
        sign=core.side_sign(side)
        a.invalidation=entry-sign*.1
        a.created_ts=s.now
        a.evidence['setup_timeframe']=tf
        s.price=entry+sign*.5
        s.bid=s.price-.01;s.ask=s.price+.01
        return cfg,ctx,a,s

    def test_user_short_example_and_mirrored_long_at_every_timeframe(self):
        for tf in ('15m','1H','4H'):
            for side,expected_stop,expected_tp in [('SHORT',101.,98.5),('LONG',99.,101.5)]:
                with self.subTest(tf=tf,side=side):
                    cfg,ctx,a,s=self.fixture(side,tf=tf)
                    sign=core.side_sign(side)
                    distance=1.5+max(.15*ctx['atr3'],2*cfg.tick)
                    targets=[{'kind':'test','level':100+sign*distance,'distance':distance}]
                    with patch.object(engine,'full_targets',return_value=targets):
                        p,why=engine.full_plan(a,ctx,s,cfg,{})
                        preview=engine.preview_geometry(a,ctx,s,cfg)
                    self.assertIsNotNone(p,why)
                    self.assertEqual(p['stop'],expected_stop)
                    self.assertEqual(p['tp1'],expected_tp)
                    for key in ('entry','stop','risk','tp1','gross_rr'):
                        self.assertAlmostEqual(p[key],preview[key])

    def test_all_24_setups_have_floors_for_both_sides_and_all_timeframes(self):
        for name in core.DETECTED_SETUP_TYPES:
            for tf in ('15m','1H','4H'):
                for side in ('LONG','SHORT'):
                    with self.subTest(setup=name,tf=tf,side=side):
                        cfg,ctx,a,s=self.fixture(side,tf=tf)
                        a.setup_type=name
                        sign=core.side_sign(side)
                        with patch.object(engine,'full_targets',return_value=[{'kind':'test','level':100+sign*3,'distance':3}]):
                            p,why=engine.full_plan(a,ctx,s,cfg,{})
                        self.assertIsNotNone(p,why)
                        self.assertGreaterEqual(p['stop_distance_pct']+1e-8,1.)
                        self.assertGreaterEqual(p['tp1_distance_pct']+1e-8,1.5)
                        self.assertGreaterEqual(p['gross_rr'],cfg.min_net_rr)

    def test_target_below_minimum_cannot_pass_even_with_good_rr(self):
        cfg,ctx,a,s=self.fixture('LONG')
        cfg=replace(cfg,min_net_rr=1.)
        buffer=max(.15*ctx['atr3'],2*cfg.tick);diag={}
        with patch.object(engine,'full_targets',return_value=[{'kind':'near','level':101.4+buffer,'distance':1.4+buffer}]):
            p,why=engine.full_plan(a,ctx,s,cfg,{},diag)
        self.assertIsNone(p);self.assertEqual(why,'TARGET_DISTANCE_TOO_SMALL')
        self.assertGreater(diag['gross_rr'],cfg.min_net_rr)
        self.assertNotIn('tp1',diag)
        self.assertEqual(diag['required_tp'],101.5)
        self.assertFalse(next(c for c in diag['entry_checks'] if c['key']=='TARGET_RR')['met'])

    def test_far_target_does_not_override_a_known_nearer_obstacle(self):
        cfg,ctx,a,s=self.fixture('SHORT');diag={}
        targets=[{'kind':'near','level':99.5,'distance':.5},{'kind':'far','level':97.,'distance':3.}]
        with patch.object(engine,'full_targets',return_value=targets):
            p,why=engine.full_plan(a,ctx,s,cfg,{},diag)
            preview=engine.preview_geometry(a,ctx,s,cfg)
        self.assertIsNone(p);self.assertEqual(why,'STRONG_OPPOSING_ZONE_BEFORE_TP')
        self.assertNotIn('tp1',preview)
        self.assertEqual(preview['required_tp'],98.5)
        self.assertEqual(price_forecast.forecast({'side':-1,**preview},s,cfg)['status'],'NEEDS_SL_TP')

    def test_coarse_ticks_round_stops_outward_and_required_targets_outward(self):
        for side in ('LONG','SHORT'):
            cfg,ctx,a,s=self.fixture(side,entry=103.25,tick=.25)
            sign=core.side_sign(side)
            with patch.object(engine,'full_targets',return_value=[{'kind':'test','level':103.25+sign*4,'distance':4}]):
                p,why=engine.full_plan(a,ctx,s,cfg,{})
                preview=engine.preview_geometry(a,ctx,s,cfg)
            self.assertIsNotNone(p,why)
            self.assertGreaterEqual(p['risk']/p['entry'],.01)
            self.assertGreaterEqual(sign*(p['tp1']-p['entry'])/p['entry'],.015)
            self.assertGreaterEqual(sign*(preview['required_tp']-p['entry'])/p['entry'],.015)
            for key in ('entry','stop','tp1'):
                self.assertAlmostEqual(p[key]/cfg.tick,round(p[key]/cfg.tick))

    def test_floors_are_based_on_entry_not_current_price(self):
        cfg,ctx,a,s=self.fixture('SHORT')
        before=engine.stop_geometry(a,ctx,cfg)
        s.price=90.
        self.assertEqual(before,engine.stop_geometry(a,ctx,cfg))
        self.assertEqual(before[1],101.)

    def test_structural_stop_stays_beyond_the_one_percent_floor(self):
        cfg,ctx,a,s=self.fixture('SHORT');a.invalidation=101.1
        entry,stop,risk,floor,buffer=engine.stop_geometry(a,ctx,cfg)
        self.assertGreater(stop,101.1)
        geometry=engine.target_geometry(entry,stop,risk,buffer,-1,[],cfg)
        self.assertGreaterEqual((entry-geometry['required_tp'])/risk,cfg.min_net_rr)

    def test_environment_cannot_restore_old_tight_levels(self):
        for settings in ({'ABS_MIN_STOP_PCT':'.004'},{'MIN_TP1_DISTANCE_PCT':'.01'}):
            with self.subTest(settings=settings),patch.dict(os.environ,settings,clear=True):
                with self.assertRaises(ValueError): engine.Config.from_env()
        with patch.dict(os.environ,{},clear=True):
            cfg=engine.Config.from_env()
        self.assertEqual(cfg.min_stop_pct,.01);self.assertEqual(cfg.min_target_pct,.015)


class MessageAndUpgradeTests(unittest.TestCase):
    def test_normalization_keeps_only_three_icon_labels_and_math(self):
        text='📊 BZ-USDT\n💰 Ціна зараз: 100\n🏆 Найкращий сценарій:\n🧩 Готовність до входу: 75%\n🛑 SL: 101 • TP1 ≤ 98.5 ✅\n🔮 ≈60%\n🟢 #001 ЗАКРИТО +1R'
        actual=engine.decorate_message(text)
        self.assertEqual([c for c in actual if ord(c)>=0x1F000],['💰','🏆','🧩'])
        self.assertIn('TP1 ≤ 98.5',actual);self.assertIn('≈60%',actual)
        self.assertIn('#001 ЗАКРИТО +1R',actual)
        self.assertEqual(engine.decorate_message(actual),actual)

    def test_blocked_message_shows_required_tp_and_minimum_stop(self):
        cfg,ctx,a,s=MinimumDistanceTests().fixture('SHORT',entry=103.76)
        s.price=103.48;s.bid=103.47;s.ask=103.49;diag={}
        with patch.object(engine,'full_targets',return_value=[{'kind':'near','level':103.4,'distance':.36}]):
            p,why=engine.full_plan(a,ctx,s,cfg,{},diag)
        row={'setup':'RANGE_EDGE_REVERSAL','timeframe':'1H','side':-1,'level':a.level,
             'readiness_pct':75.,'reason':why,**diag}
        text=engine.build_message(engine.new_state(cfg),s,{'action':'WATCH','analysis':{'watch':[row]}},cfg)
        self.assertIn('SHORT • 1h • Розворот від межі діапазону',text)
        self.assertIn('ордер ще не активований',text)
        self.assertIn('мінімальний TP1 1.5%',text)
        self.assertNotIn('TP1: 103.',text)
        self.assertEqual([c for c in text if ord(c)>=0x1F000],['💰'])

    def test_reached_target_stays_understandable_without_a_checkmark(self):
        p=order();p.update(tp1=102.,tp1_hit=True,partials={'TP1':1.})
        self.assertEqual(engine.target_line(p),'TP1 102 (досягнуто)')

    def test_upgrade_from_1242_preserves_existing_plan_levels_and_ttl(self):
        cfg=replace(engine.Config(),core=core);state=engine.new_state(cfg)
        state['pending']=order();state['pending']['stop']=99.6
        state['version']='full-ict-v12.4.2-quote-gates-price-forecast'
        state['config_hash']='old-geometry';before=copy.deepcopy(state['pending'])
        with patch.object(engine,'health',return_value=[]),patch.object(engine,'execution_health',return_value=[]), \
             patch.object(engine,'advance_lifecycle'),patch.object(engine,'amend_at_report'), \
             patch.object(quality,'advance'),patch.object(engine,'analyze',return_value=analysis()):
            engine.run_cycle(state,snapshot(price=101.),cfg)
        for key in ('entry','stop','tp','expires_ts'):
            self.assertEqual(state['pending'][key],before[key])
        self.assertTrue(state['config_migrations'][-1]['existing_plans_preserved'])


if __name__=='__main__': unittest.main()
