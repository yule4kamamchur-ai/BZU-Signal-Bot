"""Real reported timers/levels plus causal synthetic OHLC regressions.

These tests do not turn the uploaded tick log into a historical backtest.
"""
import copy
import json
import re
import tempfile
from types import SimpleNamespace
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import limit_structure as structure
import market_archive
import setup_scanner as scanner
import signal_quality as quality
import research_validate as research
from test_full_limit_engine import NOW, candle, full_fixture, order, snapshot, analysis
from test_signal_quality import quality_snapshot
import test_v12_4_3_geometry as geometry_fixtures

ROOT=Path(__file__).parent
REPORTED=json.loads((ROOT/'testdata/reported_20261014.json').read_text())


class CausalTargetTests(unittest.TestCase):
    def test_reported_close_liquidity_does_not_force_a_tight_tp_when_a_real_far_target_exists(self):
        cfg=replace(engine.Config(),core=core,tick=.01)
        w=REPORTED['best_scenario'];entry=w['entry'];stop=w['stop'];risk=w['risk']
        near={'kind':'1H_LIQUIDITY','level':103.31,'distance':.22,'hard_barrier':False}
        far={'kind':'4H_LIQUIDITY','level':101.5,'distance':2.03,'hard_barrier':False}
        g=engine.target_geometry(entry,stop,risk,.05,-1,[near,far],cfg)
        self.assertTrue(g['path_clear']);self.assertTrue(g['target_distance_met'])
        self.assertLessEqual(g['tp1'],w['required_tp'])
        self.assertEqual(g['intermediate_levels'],[near])
        self.assertEqual(g['target_source'],'4H_LIQUIDITY')

    def test_missing_distant_structure_is_still_blocked_not_replaced_by_a_fake_tp(self):
        cfg=engine.Config()
        targets=[{'kind':'1H_LIQUIDITY','level':99.8,'distance':.2,'hard_barrier':False}]
        g=engine.target_geometry(100,101,1,.03,-1,targets,cfg)
        self.assertNotIn('tp1',g)
        self.assertFalse(g['target_distance_met'])

    def test_strong_fresh_opposing_zone_still_blocks_far_tp(self):
        cfg,ctx,a,s=geometry_fixtures.MinimumDistanceTests().fixture('LONG')
        targets=[{'kind':'SUPPLY_OB','level':101,'distance':1,'hard_barrier':True},
                 {'kind':'4H_LIQUIDITY','level':104,'distance':4,'hard_barrier':False}]
        with patch.object(engine,'full_targets',return_value=targets):
            p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNone(p);self.assertEqual(why,'STRONG_OPPOSING_ZONE_BEFORE_TP')

    def test_untyped_legacy_levels_are_conservatively_treated_as_barriers(self):
        cfg=engine.Config()
        g=engine.target_geometry(100,99,1,.03,1,[{'kind':'legacy','level':101,'distance':1},
            {'kind':'far','level':104,'distance':4}],cfg)
        self.assertFalse(g['path_clear']);self.assertNotIn('tp1',g)

    def pivot_snapshot(self,consumed=False):
        s=snapshot();rows=[]
        highs=[101,101.1,101.2,102,101.2,101.1,101,102.03 if consumed else 101.1,101,101.1]
        for i,high in enumerate(highs):
            rows.append(candle(NOW-(len(highs)-i)*engine.TF['15m'],100.5,high,100.2,100.6))
        s.candles={'15m':rows,'1H':[],'4H':[],'3m':[]}
        return s

    def test_consumed_pivot_is_removed_but_unswept_pivot_is_retained(self):
        cfg=replace(engine.Config(),core=core,tick=.01);ctx={'analysis_timeframe':'15m','session':{}}
        with patch.object(core,'detect_order_blocks',return_value=[]),patch.object(core,'detect_fvg',return_value=[]):
            fresh=engine.full_targets(ctx,self.pivot_snapshot(), 'LONG',100,cfg)
            spent=engine.full_targets(ctx,self.pivot_snapshot(True),'LONG',100,cfg)
        self.assertIn(102,[t['level'] for t in fresh])
        self.assertNotIn(102,[t['level'] for t in spent])

    def test_future_or_unclosed_wick_cannot_consume_known_liquidity(self):
        cfg=replace(engine.Config(),core=core,tick=.01);s=self.pivot_snapshot()
        with patch.object(core,'detect_order_blocks',return_value=[]),patch.object(core,'detect_fvg',return_value=[]):
            before=engine.full_targets({'session':{}},s,'LONG',100,cfg)
            s.candles['15m'] += [candle(NOW,100,105,99,104),candle(NOW-900000,100,105,99,104,False)]
            self.assertEqual(engine.full_targets({'session':{}},s,'LONG',100,cfg),before)

    def test_full_traversal_removes_old_ob_from_targets(self):
        cfg=replace(engine.Config(),core=core,tick=.01);s=self.pivot_snapshot()
        zone=core.Zone('SUPPLY_OB','SHORT',102,102.5,s.candles['15m'][0].ts,'15m',2.)
        with patch.object(core,'detect_order_blocks',return_value=[zone]),patch.object(core,'detect_fvg',return_value=[]):
            fresh=engine.full_targets({'session':{}},s,'LONG',100,cfg)
            last=s.candles['15m'][-1];s.candles['15m'][-1]=candle(last.ts,101,103,100.2,102.8)
            spent=engine.full_targets({'session':{}},s,'LONG',100,cfg)
        self.assertTrue(any(t['kind']=='SUPPLY_OB' and t['hard_barrier'] for t in fresh))
        self.assertFalse(any(t['kind']=='SUPPLY_OB' for t in spent))

    def test_pretouch_planner_arms_before_return_and_preserves_user_distance_floors(self):
        for tf in scanner.PRIMARY:
            for side in ('LONG','SHORT'):
                with self.subTest(tf=tf,side=side):
                    cfg,ctx,a,s=geometry_fixtures.MinimumDistanceTests().fixture(side,tf=tf)
                    sign=core.side_sign(side)
                    targets=[{'kind':'minor_liquidity','level':100+sign*.3,'distance':.3,'hard_barrier':False},
                             {'kind':'major_liquidity','level':100+sign*3,'distance':3,'hard_barrier':False}]
                    with patch.object(engine,'full_targets',return_value=targets):
                        p,why=engine.full_plan(a,ctx,s,cfg,{})
                    self.assertIsNotNone(p,why);self.assertEqual(p['entry_policy'],'PRETOUCH_LIMIT')
                    self.assertNotEqual(s.price,p['entry'])
                    self.assertGreaterEqual(p['stop_distance_pct']+1e-8,1.)
                    self.assertGreaterEqual(p['tp1_distance_pct']+1e-8,1.5)
                    self.assertGreaterEqual(p['effective_ts'],s.now)


class DistinctEpisodeTests(unittest.TestCase):
    def fixture(self,index=0,rejection=True):
        cfg=replace(engine.Config(),core=core,tick=REPORTED['tick'])
        raw=copy.deepcopy(list(REPORTED['anchor_memory'].values())[index])
        a=core.anchor_from_dict(copy.deepcopy(raw));a.created_ts=REPORTED['now'];a.expires_ts=a.created_ts+720*60000
        s=engine.Snapshot(REPORTED['now'],REPORTED['price'],{tf:[] for tf in engine.TF},trusted=True)
        sign=core.side_sign(a.side);micro=scanner.MICRO[a.evidence.get('setup_timeframe','15m')]
        closing=s.now//engine.TF[micro]*engine.TF[micro];ts=closing-engine.TF[micro]
        level=a.level
        c=candle(ts,level-sign*.03,level+.10,level-.10,level+sign*.08)
        if rejection:s.candles[micro]=[c]
        state=engine.new_state(cfg);key=engine.anchor_zone_key(a,cfg);state['anchor_memory']={key:raw}
        contexts={tf:{'atr3':.2,'session':{'day_ts':raw['created_ts']-3600000}} for tf in scanner.PRIMARY}
        return cfg,a,s,state,contexts

    def test_all_four_reported_expired_rows_can_form_a_distinct_new_episode(self):
        for index in range(4):
            with self.subTest(index=index):
                cfg,a,s,state,contexts=self.fixture(index)
                previous=copy.deepcopy(next(iter(state['anchor_memory'].values())))
                merged=engine.merge_full_anchors([a],state,s,cfg,contexts)
                current=next(x for x in merged if x.id==a.id)
                self.assertEqual(current.created_ts,s.now)
                self.assertGreater(current.expires_ts,s.now)
                self.assertNotEqual(current.id,previous['id'])
                self.assertIn('episode_ts',current.evidence)
                self.assertEqual(state['setup_renewals'][-1]['kind'],'NEW_CLOSED_REJECTION')

    def test_expiry_without_new_event_cannot_extend_a_timer(self):
        cfg,a,s,state,contexts=self.fixture(rejection=False)
        old=copy.deepcopy(next(iter(state['anchor_memory'].values())))
        self.assertEqual(engine.merge_full_anchors([a],state,s,cfg,contexts),[])
        self.assertEqual(a.created_ts,old['created_ts']);self.assertEqual(a.expires_ts,old['expires_ts'])

    def test_same_rejection_is_not_renewed_again_at_next_scan(self):
        cfg,a,s,state,contexts=self.fixture()
        engine.merge_full_anchors([a],state,s,cfg,contexts);before=(a.id,a.created_ts,a.expires_ts)
        s.now+=300000;fresh=copy.deepcopy(a);fresh.created_ts=s.now;fresh.expires_ts=s.now+3600000
        engine.merge_full_anchors([fresh],state,s,cfg,contexts)
        self.assertEqual((fresh.id,fresh.created_ts,fresh.expires_ts),before)
        self.assertEqual(len(state['setup_renewals']),1)

    def test_future_or_unclosed_rejection_cannot_renew_an_expired_level(self):
        cfg,a,s,state,contexts=self.fixture();micro=scanner.MICRO[a.evidence.get('setup_timeframe','15m')]
        c=s.candles[micro][0];s.candles[micro]=[replace(c,ts=s.now),replace(c,confirmed=False)]
        self.assertEqual(engine.merge_full_anchors([a],state,s,cfg,contexts),[])

    def test_invalidated_episode_is_not_resurrected_without_new_evidence(self):
        cfg,a,s,state,contexts=self.fixture(rejection=False)
        raw=next(iter(state['anchor_memory'].values()));raw['expires_ts']=s.now+3600000;raw['state']='INVALIDATED'
        self.assertEqual(engine.merge_full_anchors([a],state,s,cfg,contexts),[])
        self.assertEqual(a.state,'INVALIDATED')

    def test_live_limit_prevents_episode_renewal_at_its_level(self):
        cfg,a,s,state,contexts=self.fixture();p=order()
        p.update(event_key=engine.anchor_key(a,cfg),zone_key=engine.anchor_zone_key(a,cfg),entry=a.level)
        state['pending']=p;before=copy.deepcopy(p)
        engine.merge_full_anchors([a],state,s,cfg,contexts)
        self.assertNotIn('setup_renewals',state)
        self.assertEqual(state['pending'],before)

    def test_new_session_reference_can_renew_a_session_setup(self):
        cfg,a,s,state,contexts=self.fixture(rejection=False)
        raw=next(iter(state['anchor_memory'].values()));raw['created_ts']-=86400000;raw['expires_ts']-=86400000
        tf=a.evidence.get('setup_timeframe','15m');contexts[tf]['session']['day_ts']=s.now//86400000*86400000
        engine.merge_full_anchors([a],state,s,cfg,contexts)
        self.assertEqual(state['setup_renewals'][-1]['kind'],'NEW_SESSION_REFERENCE')

    def test_new_session_can_renew_an_unused_old_session_level_before_its_expiry(self):
        cfg,a,s,state,contexts=self.fixture(rejection=False)
        raw=next(iter(state['anchor_memory'].values()));raw['created_ts']-=86400000;raw['expires_ts']=s.now+3600000
        tf=a.evidence.get('setup_timeframe','15m');contexts[tf]['session']['day_ts']=s.now//86400000*86400000
        engine.merge_full_anchors([a],state,s,cfg,contexts)
        self.assertEqual(state['setup_renewals'][-1]['kind'],'NEW_SESSION_REFERENCE')

    def test_weekly_reference_does_not_renew_just_because_day_changed(self):
        cfg,a,s,state,contexts=self.fixture(index=1,rejection=False)
        a.evidence['reference']='week_open'
        raw=next(iter(state['anchor_memory'].values()));raw['created_ts']-=86400000;raw['expires_ts']-=86400000
        tf=a.evidence.get('setup_timeframe','15m');contexts[tf]['session']['day_ts']=s.now//86400000*86400000
        self.assertEqual(engine.merge_full_anchors([a],state,s,cfg,contexts),[])

    def test_repeat_after_visit_can_rearm_before_old_ttl_only_with_new_rejection(self):
        cfg,a,s,state,contexts=self.fixture()
        old=next(iter(state['anchor_memory'].values()));old['created_ts']=s.now-3600000;old['expires_ts']=s.now+3600000
        self.assertTrue(s.candles['3m'])
        engine.merge_full_anchors([a],state,s,cfg,contexts)
        self.assertEqual(a.created_ts,s.now)
        self.assertIn('episode_ts',a.evidence)

    def test_original_registry_does_not_label_an_expired_anchor_accepted(self):
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg)
        with engine.market_clock(core,s.now):
            context=engine.build_full_context(s,state,cfg)
            a=core.detect_range_compression_breakout(context)
        self.assertIsNotNone(a)
        a.evidence['setup_timeframe']='15m';a.created_ts=s.now-7200000;a.expires_ts=s.now-1800000
        state['anchor_memory'][engine.anchor_key(a,cfg)]=core.anchor_to_dict(a)
        result=engine.analyze(s,cfg,state)
        row=next(r for r in result['setup_matrix'] if r['setup']==a.setup_type and r['timeframe']=='15m')
        self.assertEqual(row['status'],'BLOCKED')
        self.assertIn(row['reason'],('ANCHOR_EXPIRED_AWAITING_NEW_EVENT','WAIT_CONFIRMED_BREAK_FOR_LIMIT'))
        self.assertLess(row['readiness_pct'],100.)


class EvidenceAndRuntimeTests(unittest.TestCase):
    def test_frozen_legacy_harness_does_not_load_or_execute_production_config(self):
        old=SimpleNamespace(_run_self_test=lambda:True)
        with patch('sys.argv',['bot_oneshot.py','--legacy-self-test']), \
             patch.object(engine.Config,'from_env',side_effect=AssertionError('production config must not load')), \
             patch.object(engine,'run_live',side_effect=AssertionError('offline only')):
            self.assertEqual(engine.main(old),0)

    def test_archive_replay_uses_actual_quote_instead_of_last_closed_candle_price(self):
        s=snapshot();rows={tf:[vars(c) for c in candles] for tf,candles in s.candles.items()}
        data={'instrument':s.instrument,'tick':.01,'start_ts':s.now,'end_ts':s.now+300000,
              'candles':rows,'quotes':[{'now':s.now,'price':103.27,'bid':103.26,'ask':103.28,
                                     'trusted':True,'ticker_ts':s.now}]}
        replay=list(research.snapshots(data))
        self.assertEqual(len(replay),1);self.assertEqual(replay[0].price,103.27)
        self.assertNotEqual(replay[0].price,replay[0].candles['3m'][-1].close)
        self.assertTrue(all(c.ts+engine.TF[tf]<=replay[0].now for tf,cs in replay[0].candles.items() for c in cs))

    def test_download_research_can_use_five_minute_scan_intervals(self):
        s=snapshot();s.candles['3m'] += [candle(NOW+i*180000) for i in range(10)]
        data={'instrument':s.instrument,'tick':.01,'start_ts':NOW,'end_ts':NOW+900000,
              'report_delay_ms':0,'scan_interval_ms':300000,
              'candles':{tf:[vars(c) for c in cs] for tf,cs in s.candles.items()}}
        replay=list(research.snapshots(data))
        self.assertEqual([s.now for s in replay],[NOW,NOW+300000,NOW+600000,NOW+900000])

    def test_archive_research_rejects_missing_warmup_history(self):
        archive={'schema':market_archive.SCHEMA,'instrument':'BZ-USDT-SWAP','candles':{},
                 'quotes':[{'now':NOW,'tick':.01},{'now':NOW+300000,'tick':.01}]}
        with self.assertRaisesRegex(ValueError,'more closed'):research.archive_dataset(archive)

    def test_supported_empirical_confidence_outranks_an_unvalidated_context_score(self):
        proven={'setup':'A','side':1,'readiness_pct':100,'score':65,'statistics':{'observations':50,'lower_95':.72}}
        heuristic={'setup':'B','side':1,'readiness_pct':100,'score':99,'price_forecast':{'tp1_before_sl':.99}}
        # Neither an old sample threshold nor an uncalibrated price-model WR
        # overrides the current continuous decision score.
        self.assertEqual(scanner.best_scenario([heuristic,proven],100)['setup'],'B')
        proven['decision_score']=101.
        self.assertEqual(scanner.best_scenario([heuristic,proven],100)['setup'],'A')

    def test_archive_excludes_future_bars_and_preserves_first_observed_closed_data(self):
        cfg=engine.Config();s=snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'market.json'
            s.candles['3m'] += [candle(NOW,100,105,99,104),candle(NOW-180000,100,105,99,104,False)]
            market_archive.record(path,s,cfg,engine);first=engine.read_json(path)
            original=s.candles['3m'][0];s.candles['3m'][0]=replace(original,high=200)
            market_archive.record(path,s,cfg,engine);after=engine.read_json(path)
            self.assertEqual(first,after)
            self.assertTrue(all(row[0]+engine.TF['3m']<=s.now for row in after['candles']['3m']))

    def test_unknown_market_archive_is_preserved_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'market.json';path.write_text('{"schema":"unknown"}')
            with self.assertRaises(ValueError):market_archive.record(path,snapshot(),engine.Config(),engine)
            self.assertEqual(path.read_text(),'{"schema":"unknown"}')

    def test_out_of_order_scan_cannot_erase_newer_market_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'market.json';cfg=engine.Config()
            market_archive.record(path,snapshot(NOW+300000),cfg,engine);before=path.read_bytes()
            result=market_archive.record(path,snapshot(NOW),cfg,engine)
            self.assertEqual(result['status'],'UNCHANGED');self.assertEqual(path.read_bytes(),before)

    def test_workflow_has_single_15_minute_trade_and_report_cycle(self):
        workflow=(ROOT/'.github/workflows/main.yml').read_text()
        self.assertIn("cron: '*/15 * * * *'",workflow)
        self.assertNotIn("cron: '5,10,20,25,35,40,50,55 * * * *'",workflow)
        self.assertIn('run: python bot_oneshot.py --notify',workflow)
        self.assertNotIn('steps.silent_scan.outcome',workflow)
        self.assertIn('market_history_v12.json',workflow)
        self.assertIn(engine.VERSION,workflow)
        quality=(ROOT/'.github/workflows/bzu-quality.yml').read_text()
        self.assertNotIn('python bot_oneshot.py --legacy-self-test', workflow)
        self.assertIn('ABS_MIN_STOP_PCT=0.004 python bot_oneshot.py --legacy-self-test',quality)

    def test_version_upgrade_preserves_live_orders_and_archives_old_quality_evidence(self):
        cfg=replace(engine.Config(),core=core);state=engine.new_state(cfg);state['pending']=order()
        state['version']='full-ict-v12.4.3-minimum-sl-tp';state['config_hash']='old'
        quality.ensure(state,cfg)
        state['quality']['strategy']='native-mtf-single-scenario-no-commission-v3';before=copy.deepcopy(state['pending'])
        with patch.object(engine,'health',return_value=[]),patch.object(engine,'execution_health',return_value=[]), \
             patch.object(engine,'advance_lifecycle'),patch.object(engine,'amend_at_report'), \
             patch.object(quality,'advance'),patch.object(engine,'analyze',return_value=analysis()):
            engine.run_cycle(state,snapshot(price=101),cfg)
        for key in ('entry','stop','tp','expires_ts'):self.assertEqual(state['pending'][key],before[key])
        self.assertTrue(state['config_migrations'][-1]['existing_plans_preserved'])
        quality.ensure(state,cfg)
        self.assertEqual(state['quality_archives'][-1]['strategy'],'native-mtf-single-scenario-no-commission-v3')


if __name__=='__main__':unittest.main()
