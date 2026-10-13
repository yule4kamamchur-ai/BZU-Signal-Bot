"""Targeted causal and risk regressions; synthetic data is not win-rate evidence."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import signal_quality as quality
from test_full_limit_engine import NOW, candle, full_fixture
from test_signal_quality import confirmed_features, quality_snapshot


class GeometryAndTimeTests(unittest.TestCase):
    def test_floor_cannot_be_disabled_by_environment_override(self):
        with self.assertRaises(ValueError):
            replace(engine.Config(),min_stop_pct=.002).validate()

    def test_stop_floor_applies_to_all_twenty_four_setup_names(self):
        for setup in core.DETECTED_SETUP_TYPES:
            with self.subTest(setup=setup):
                side='SHORT' if setup.endswith('SHORT') else 'LONG'
                cfg,ctx,a,s=full_fixture(side)
                a.setup_type=setup
                a.invalidation=a.level-core.side_sign(side)*.01
                p,why=engine.full_plan(a,ctx,s,cfg,{})
                self.assertIsNotNone(p,why)
                self.assertGreaterEqual(p['risk']+1e-8,cfg.min_stop_pct*p['entry'])
                self.assertGreaterEqual(p['stop_distance_pct']+1e-8,1.)
                self.assertGreaterEqual(p['net_rr'],cfg.min_net_rr)
                self.assertGreater(p['tp1']*p['side']-p['entry']*p['side'],0)

    def test_tight_target_is_rejected_after_widening_stop_and_charging_fees(self):
        cfg,ctx,a,s=full_fixture()
        a.invalidation=a.level-.01
        cfg=replace(cfg,include_commission=True)
        with patch.object(engine,'full_targets',return_value=[{'kind':'TEST','level':a.level+.10,'distance':.10}]):
            p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNone(p)
        self.assertEqual(why,'TARGET_DISTANCE_TOO_SMALL')

    def test_old_anchor_does_not_shorten_new_limit_ttl(self):
        cfg,ctx,a,s=full_fixture()
        a.created_ts=s.now-135*60_000
        # Hold the entry unvisited in the fixture; isolate TTL from old touches.
        s.candles['3m']=s.candles['3m'][-3:]
        a.expires_ts=s.now+40*60_000  # only 40m left on the setup
        p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNotNone(p,why)
        self.assertGreaterEqual((p['expires_ts']-p['effective_ts'])/60_000,110)
        self.assertLessEqual((p['expires_ts']-p['effective_ts'])/60_000,120)


class DetectorAndReferenceTests(unittest.TestCase):
    def test_ignition_uses_prior_compression_not_breakout_candle(self):
        step=900_000
        bars=[core.Candle(ts=NOW-(20-i)*step,open=100,high=100.2,low=99.8,close=100,
                          volume=20,confirmed=True) for i in range(20)]
        bars.append(core.Candle(ts=NOW,open=100,high=101.5,low=99.9,close=101.4,
                                volume=30,confirmed=True))
        ctx={'candles':{'15m':bars},'atr15':.5,'price':101.4,
             'regime_profile':{'efficiency_15m':.9}}
        with engine.market_clock(core,NOW):
            a=core.detect_trend_ignition(ctx)
        self.assertIsNotNone(a)
        self.assertEqual(a.setup_type,'TREND_IGNITION')
        self.assertAlmostEqual(a.level,100.2,3)

    def test_failed_opening_range_short_invalidation_is_above_sweep(self):
        rows=[core.Candle(ts=NOW-i*900_000,open=99.8,high=100.7,low=99.4,close=99.6,
                          volume=1,confirmed=True) for i in range(8,0,-1)]
        ctx={'candles':{'15m':rows},'atr15':.40,'price':99.8,
             'session':{'opening_range_complete':True,'opening_range_high':100.,'opening_range_low':98.}}
        with engine.market_clock(core,NOW):
            a=core.detect_failed_opening_range_breakout(ctx)
        self.assertIsNotNone(a)
        self.assertEqual(a.side,'SHORT')
        self.assertGreater(a.invalidation,max(c.high for c in rows))

    def test_asia_range_is_frozen_not_day_extrema_and_week_uses_1h(self):
        now=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
        day=datetime(2026,10,13,tzinfo=timezone.utc)
        day_ms=int(day.timestamp()*1000)
        h0=day-timedelta(days=2)
        hour=[]
        for i in range(61):
            ts=int((h0+timedelta(hours=i)).timestamp()*1000)
            hour.append(core.Candle(ts,100,102,98,100,1,True))
        quarter=[]
        for i in range(48):
            ts=day_ms+i*900_000
            price=140 if i>=29 else 100
            quarter.append(core.Candle(ts,price,price+.25,price-.25,price,1,True))
        session=core.session_profile(quarter,now,hour)
        self.assertTrue(session['asia_complete'])
        self.assertAlmostEqual(session['asia_high'],100.25)
        self.assertGreater(session['day_high'],session['asia_high'])
        self.assertTrue(session['week_complete'])
        self.assertEqual(session['week_high'],102)
        self.assertEqual(session['week_low'],98)
        self.assertTrue(session['opening_range_complete'])
        self.assertEqual(core.session_profile(quarter,now,None)['week_high'],0.)


class QualityAndReportingTests(unittest.TestCase):
    def test_later_confirmation_only_upgrades_unfilled_future_episode(self):
        cfg,s,ctx,p=quality_snapshot()
        q=quality.ensure({},cfg)
        plan=copy.deepcopy(p)
        plan.update(qualified=False,features=confirmed_features(side3=0),
                    decision_ts=s.now-900_000,placed_ts=s.now-900_000,
                    expires_ts=s.now+3_600_000,effective_ts=s.now-900_000)
        state={'quality':q}
        q['pending']=[{'pending':plan,'active':None}]
        with patch.object(quality,'features',return_value=confirmed_features()):
            quality.refresh_confirmations(state,s,ctx,cfg,engine)
        self.assertTrue(plan['qualified'])
        self.assertEqual(plan['decision_ts'],s.now)
        self.assertGreaterEqual(plan['effective_ts'],s.now)
        assert len(q['pending'])==1
        active=copy.deepcopy(plan);active['qualified']=False
        q['pending']=[{'pending':None,'active':active}]
        with patch.object(quality,'features',side_effect=AssertionError('past-filled must not be upgraded')):
            quality.refresh_confirmations(state,s,ctx,cfg,engine)
        self.assertFalse(active['qualified'])

    def test_message_is_plain_ukrainian_and_does_not_relabel_paper_as_real(self):
        cfg,s,ctx,p=quality_snapshot()
        state=engine.new_state(cfg)
        p.update(status='PENDING',partials={'TP1':1.0},expires_ts=s.now+3_600_000)
        state['pending']=p
        message=engine.build_message(state,s,{'action':'WAIT_LIMIT','events':[]},cfg)
        for label in ('💰','🧩','SL:','TP1','ЛІМІТНИЙ СИГНАЛ','Зайнято місць','Ціна зараз','До входу'):
            self.assertIn(label,message)


if __name__ == '__main__':
    unittest.main()

class DeploymentContinuityTests(unittest.TestCase):
    def test_upgrade_preserves_open_legacy_plan_geometry(self):
        from test_full_limit_engine import order, snapshot, analysis
        cfg=replace(engine.Config(),core=core)
        state=engine.new_state(cfg)
        state['pending']=order()
        state['config_hash']='old-configuration-contract'
        state['version']='full-ict-v12.2.0-confirmed-ict-quality'
        original=copy.deepcopy(state['pending'])
        with patch.object(engine,'health',return_value=[]), \
             patch.object(engine,'execution_health',return_value=[]), \
             patch.object(engine,'advance_lifecycle'), \
             patch.object(engine,'amend_at_report'), \
             patch.object(quality,'advance'), \
             patch.object(engine,'analyze',return_value=analysis()):
            engine.run_cycle(state,snapshot(price=101),cfg)
        self.assertEqual(state['pending']['stop'],original['stop'])
        self.assertEqual(state['pending']['entry'],original['entry'])
        self.assertEqual(state['pending']['expires_ts'],original['expires_ts'])
        self.assertTrue(state['config_migrations'][-1]['existing_plans_preserved'])

    def test_quiet_five_minute_scan_keeps_events_for_next_telegram(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from test_full_limit_engine import snapshot
        cfg=replace(engine.Config(),core=core)
        state=engine.new_state(cfg)
        old={'kind':'FILLED','ts':NOW-180_000,'order_id':'a'}
        state['events']=[old]
        state['last_notified_event_count']=0
        snap=snapshot()
        def fake_cycle(book, ss, cfg):
            book['events'].append({'kind':'FILLED','ts':ss.now,'order_id':'b'})
            book['last_run_ts']=ss.now
            return {'action':'WATCH','events':[book['events'][-1]],'selected_plans':[]}
        with tempfile.TemporaryDirectory() as temp:
            state_path=Path(temp)/'memory.json';journal_path=Path(temp)/'journal.json'
            engine.atomic_write(state_path,state)
            with patch.object(core,'STATE_FILE',str(state_path)), \
                 patch.object(core,'JOURNAL_FILE',str(journal_path)), \
                 patch.object(core,'TELEGRAM_TOKEN','test'), \
                 patch.object(core,'TELEGRAM_CHAT_ID','test'), \
                 patch.object(engine,'resolve_live_config',return_value=cfg), \
                 patch.object(engine,'collect_snapshot',return_value=snap), \
                 patch.object(engine,'run_cycle',side_effect=fake_cycle), \
                 patch.object(engine,'notify_queue',return_value=True), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(engine.run_live(core,notify=True),0)
            saved=engine.read_json(state_path)
            self.assertEqual(saved['last_notified_event_count'],2)
            message=saved['notification_queue'][-1]['text']
            self.assertIn('a',message)
            self.assertIn('b',message)
            self.assertIn('ЦІНА ПРОЙШЛА ЛІМІТ',message)
            self.assertNotIn('🟢',message)
