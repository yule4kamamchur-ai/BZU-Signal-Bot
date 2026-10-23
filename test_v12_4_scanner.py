"""Native multi-timeframe, early limit and factual reporting regressions.

Synthetic fixtures verify engineering behavior; no market win rate is claimed.
"""
import copy
import json
import unittest
from dataclasses import replace
from functools import wraps
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import setup_scanner as scanner
import signal_quality as quality
from test_full_limit_engine import NOW, full_fixture, order, snapshot, candle, lifecycle_snapshot
from test_signal_quality import confirmed_features, observations, quality_snapshot


class NativeScannerTests(unittest.TestCase):
    def test_each_original_detector_receives_real_primary_and_micro_bars(self):
        cfg=replace(engine.Config(),core=core)
        s=snapshot();seen=[];wrapped=[]
        for detector in core.DETECTORS:
            @wraps(detector)
            def invoke(ctx, original=detector):
                tf=ctx.get('formation_timeframe',ctx['analysis_timeframe']);micro=ctx.get('formation_micro_timeframe',scanner.MICRO[ctx['analysis_timeframe']])
                self.assertEqual(ctx['candles']['15m'],s.candles[tf])
                self.assertEqual(ctx['candles']['3m'],s.candles[micro])
                self.assertAlmostEqual(ctx['atr15'],core._effective_atr15(core.atr(s.candles[tf],14),s.price))
                seen.append((original.__name__,tf))
                return original(ctx)
            wrapped.append(invoke)
        with patch.object(core,'DETECTORS',wrapped):
            result=engine.analyze(s,cfg,engine.new_state(cfg))
        self.assertEqual(len(set(seen)),96)
        self.assertEqual(result['timeframe_evaluations'],72)
        self.assertFalse([r for r in result['setup_matrix'] if r['status']=='ERROR'])
        self.assertEqual(set((r['setup'],r['timeframe']) for r in result['setup_matrix']),
                         {(name,tf) for name in scanner.SETUP_NAMES for tf in scanner.PRIMARY})

    def test_all_setup_condition_lists_exist_before_any_detector_fires(self):
        cfg,s,ctx,p=quality_snapshot()
        with engine.market_clock(core,s.now):
            base=engine.build_full_context(s,engine.new_state(cfg),cfg)
            contexts=scanner.build_contexts(s,base,{},cfg)
        for setup in scanner.SETUP_NAMES:
            for tf,native in contexts.items():
                with self.subTest(setup=setup,tf=tf):
                    row=scanner.conditions(setup,native,core)
                    self.assertGreaterEqual(row['total_conditions'],3)
                    self.assertLess(row['readiness_pct'],100)
                    self.assertAlmostEqual(row['readiness_pct'],
                        round(100*row['met_conditions']/row['total_conditions'],1))
                    self.assertTrue(row['missing_conditions'])
        self.assertEqual(set(scanner.SETUP_NAMES),set(quality.CONTRACTS))

    def test_session_references_are_not_rebuilt_from_four_hour_bars(self):
        cfg,s,ctx,p=quality_snapshot()
        with engine.market_clock(core,s.now):
            base=engine.build_full_context(s,engine.new_state(cfg),cfg)
            contexts=scanner.build_contexts(s,base,{},cfg)
        for tf in ('1H','4H'):
            self.assertEqual(contexts[tf]['session'],base['session'])
            self.assertEqual(contexts[tf]['vwap'],base['vwap'])
            self.assertFalse(contexts[tf]['smt']['available'])

    def test_sweep_forming_conditions_report_the_actual_missing_reclaim(self):
        cfg,ctx,a,s=full_fixture()
        ctx=copy.deepcopy(ctx)
        rows=[core.Candle(NOW-(25-i)*900000,100,101,99,100,10,True) for i in range(25)]
        rows[-1]=core.Candle(rows[-1].ts,99,100,98,98.5,10,True)
        ctx['candles']['15m']=rows
        row=scanner.conditions('SWEEP_RECLAIM',ctx,core)
        self.assertEqual(row['level'],99)
        self.assertIn('Закриття повернулося над рівень',row['missing_conditions'])
        self.assertIn('Знято попередній мінімум',[r['condition'] for r in row['conditions'] if r['met']])
        self.assertLess(row['readiness_pct'],100)

    def test_source_timeframe_is_part_of_anchor_identity_and_memory(self):
        cfg,ctx,a,s=full_fixture()
        clones=[]
        for tf in scanner.PRIMARY:
            clone=copy.deepcopy(a);clone.id=tf;clone.evidence['setup_timeframe']=tf;clones.append(clone)
        self.assertEqual(len({engine.anchor_key(a,cfg) for a in clones}),3)
        state=engine.new_state(cfg)
        self.assertEqual(len(engine.merge_full_anchors(clones,state,s,cfg)),3)
        repeat=copy.deepcopy(clones[-1]);repeat.created_ts+=900000;repeat.expires_ts+=900000
        repeat.id='new';s.now+=900000
        engine.merge_full_anchors([repeat],state,s,cfg)
        self.assertEqual(state['anchor_memory'][engine.anchor_key(repeat,cfg)]['created_ts'],a.created_ts)

    def test_unclosed_and_future_candles_cannot_change_the_scanner(self):
        cfg=replace(engine.Config(),core=core);s=snapshot()
        payload={'now':s.now,'price':s.price,'bid':s.bid,'ask':s.ask,'trusted':True,
                 'ticker_ts':s.now,'instrument':cfg.instrument,
                 'candles':{tf:[vars(c) for c in rows] for tf,rows in s.candles.items()}}
        before=engine.analyze(engine.Snapshot.parse(payload),cfg,engine.new_state(cfg))
        for tf,step in engine.TF.items():
            payload['candles'][tf]+=[vars(candle(s.now,50,200,10,180)),
                                    vars(candle(s.now-step,50,200,10,180,False))]
        after=engine.analyze(engine.Snapshot.parse(payload),cfg,engine.new_state(cfg))
        self.assertEqual([(r['setup'],r['timeframe'],r['conditions'],r['readiness_pct'],r['level']) for r in before['setup_matrix']],
                         [(r['setup'],r['timeframe'],r['conditions'],r['readiness_pct'],r['level']) for r in after['setup_matrix']])


class PretouchAndLifetimeTests(unittest.TestCase):
    def test_original_detectors_can_place_native_hourly_and_four_hour_limits(self):
        cfg=replace(engine.Config(),core=core)
        for target in ('1H','4H'):
            with self.subTest(timeframe=target):
                s=snapshot(price=101.25);s.bid=s.price-.001;s.ask=s.price+.001
                for tf in engine.TF:
                    high,low=(102,100) if tf==target else (102,101.9)
                    # Primary closes remain above the prior-day low: this is
                    # a unidirectional range-limit fixture, not a simultaneous
                    # downside breakout that SHOULD trigger conflict handling.
                    close=high-.05
                    s.candles[tf]=[candle(x.ts,(high+low)/2,high,low,close)
                                   for x in s.candles[tf]]
                if target=='4H':
                    s.candles['15m']=[candle(x.ts,102,104,100,102) for x in s.candles['15m']]
                state=engine.new_state(cfg)
                result=engine.run_cycle(state,s,cfg)
                self.assertIn(result['action'],('PLACE_LIMIT','WATCH','DIRECTION_CONFLICT'))
                self.assertTrue(any(r['timeframe']==target for r in result['analysis']['setup_matrix']))
                self.assertLessEqual(len({p['side'] for p in result['selected_plans']}),1)
                for p in result['selected_plans']:
                    self.assertTrue(scanner.passive_gate(p['entry'],p['side'],s.bid,s.ask,cfg.tick)['passive'])
                    self.assertGreaterEqual(p['net_rr'],cfg.min_net_rr)
                    self.assertTrue(p['quality']['accepted'])
                    self.assertIsNone(p['probability'])
                self.assertLessEqual(engine.occupied_slots(state),2)

    def test_original_range_detector_places_before_touch_without_micro_impulse(self):
        cfg,s,ctx,p=quality_snapshot()
        # Remove the short-term impulse and control that v12.3 required.
        s.candles['3m']=[candle(c.ts,101.2,101.3,101.1,101.2) for c in s.candles['3m']]
        state=engine.new_state(cfg)
        # Legacy fixtures verify messaging/pretouch logic, not ICT admission.
        with patch.object(engine.policy,'admit',return_value=(True,'CONFIRMED')), \
             patch.object(engine.policy,'choose',side_effect=lambda ps,*a,**kw:
                          (1,[p for p in ps if p['side']==1],'ISOLATED_TEST_DIRECTION')):
            result=engine.run_cycle(state,s,cfg)
        self.assertEqual(result['action'],'PLACE_LIMIT')
        p=state['pending']
        self.assertTrue(scanner.passive_gate(p['entry'],p['side'],s.bid,s.ask,cfg.tick)['passive'])
        self.assertIsNone(state['active'])
        self.assertFalse(p['quality']['features']['impulse'])
        self.assertEqual(p['quality']['features']['side3'],0)
        self.assertTrue(p['quality']['accepted'])
        self.assertEqual(p['entry_policy'],'PRETOUCH_LIMIT')
        self.assertIsNone(p['probability'])
        self.assertLessEqual(engine.occupied_slots(state),2)

    def test_incomplete_detector_conditions_and_volatility_shock_still_block(self):
        f=confirmed_features(entry_policy='PRETOUCH_LIMIT',detector_confirmed=False,htf_opposition=0)
        self.assertEqual(quality.structural_reason(f),'SETUP_CONDITIONS_INCOMPLETE')
        f.update(detector_confirmed=True,side3=0,impulse=False)
        self.assertEqual(quality.structural_reason(f),'CONFIRMED')
        f['shock']=True
        self.assertEqual(quality.structural_reason(f),'SHOCK_STILL_ACTIVE')
        f.update(shock=False,htf_opposition=2)
        self.assertEqual(quality.structural_reason(f),'HTF_REVERSAL_NOT_CONFIRMED')

    def test_limit_ttl_scales_with_its_native_timeframe(self):
        for tf,scale in [('15m',1),('1H',4),('4H',16)]:
            cfg,ctx,a,s=full_fixture()
            a.evidence['setup_timeframe']=tf
            a.expires_ts=s.now+cfg.setup_age_minutes*scale*60000
            p,reason=engine.full_plan(a,ctx,s,cfg,{})
            self.assertIsNotNone(p,reason)
            self.assertEqual(p['setup_timeframe'],tf)
            self.assertEqual(p['micro_timeframe'],scanner.MICRO[tf])
            self.assertEqual(p['max_hold_minutes'],cfg.max_hold_minutes*scale)
            self.assertEqual((p['expires_ts']-p['effective_ts'])/60000,cfg.ttl_minutes*scale)

    def test_four_hour_fill_keeps_its_hold_time_across_midnight(self):
        cfg=engine.Config();state=engine.new_state(cfg);p=order()
        p.update(setup_timeframe='4H',intraday=False,max_hold_minutes=5760,
                 effective_ts=NOW,version=engine.VERSION,execution='PAPER_SIGNAL')
        state['pending']=p
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.5,99.9,100.8)]),cfg)
        self.assertIsNotNone(state['active'])
        self.assertEqual(state['active']['exit_deadline'],NOW+5760*60000)
        self.assertGreater(state['active']['exit_deadline'],engine.session_cutoff(NOW,cfg))

    def test_uncertain_htf_fill_keeps_capacity_until_its_full_possible_hold_time(self):
        cfg=engine.Config();state=engine.new_state(cfg);p=order()
        p.update(status='UNRESOLVED',intraday=False,max_hold_minutes=1440,execution='PAPER_SIGNAL')
        state['orders']=[p];state['reconciliation']={'order_id':p['id'],'ts':NOW,'reason':'EXPIRY_INSIDE_BAR'}
        engine.release_unconfirmed_paper(state,NOW+6*3600000,cfg)
        self.assertIsNotNone(state['reconciliation'])
        self.assertEqual(state['reconciliation']['release_ts'],p['expires_ts']+1440*60000)


class EmpiricalAndReportingTests(unittest.TestCase):
    def test_old_pending_fill_is_not_counted_as_a_new_policy_trade(self):
        cfg=replace(engine.Config(),core=core);state=engine.new_state(cfg)
        p=order();p.update(version='full-ict-v12.3.0-causal-geometry-reports',effective_ts=NOW)
        state['pending']=p
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.5,99.9,100.8)]),cfg)
        self.assertEqual(state['active']['entry_version'],p['version'])
        engine.close_trade(state,state['active'],101,NOW+360000, 'TEST',cfg)
        self.assertEqual(state['trades'][0]['bot_version_at_entry'],p['version'])

    def test_win_rates_never_mix_primary_timeframes(self):
        rows=observations(40,35)
        for row in rows: row['features']['setup_timeframe']='1H'
        self.assertEqual(quality.empirical(confirmed_features(setup_timeframe='15m'),rows,NOW)['observations'],0)
        hourly=quality.empirical(confirmed_features(setup_timeframe='1H'),rows,NOW)
        self.assertEqual(hourly['observations'],40)
        self.assertEqual(hourly['wins'],35)
        self.assertEqual(hourly['scope'],'setup')

    def test_previous_entry_policy_is_archived_without_erasing_actual_orders(self):
        cfg=replace(engine.Config(),core=core);state=engine.new_state(cfg)
        state['pending']=order();original=copy.deepcopy(state['pending'])
        state['quality']={'schema':quality.SCHEMA,'strategy':'confirmed-ict-quality-v1',
                          'signature':quality.signature(cfg),'observations':[{'historic':True}],
                          'pending':[],'seen':{},'last_run_ts':NOW}
        q=quality.ensure(state,cfg)
        self.assertEqual(q['strategy'],quality.STRATEGY)
        self.assertFalse(q['observations'])
        self.assertEqual(state['quality_archives'][0]['observations'],[{'historic':True}])
        self.assertEqual(state['pending'],original)

    def test_missing_price_can_still_report_data_hold_with_an_existing_plan(self):
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg);state['pending']=p
        s.price=0;s.trusted=False
        text=engine.build_message(state,s,{'action':'DATA_HOLD','analysis':{'health':['UNTRUSTED_PRICE']}},cfg)
        self.assertIn('Ціна зараз: недоступна',text)
        self.assertIn('Вхід:',text)
        self.assertNotIn('До входу:',text)
        self.assertIn('Ціна зараз: недоступна',text)
        self.assertIn('Дані не оновлені. Збережений стан',text)
        self.assertNotIn('Ціна зараз: 0',text)

    def test_current_price_distance_progress_and_no_repeated_banners(self):
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg)
        p.update(placement_price=102.70,setup_timeframe='1H',expires_ts=s.now+3600000)
        state['pending']=p;engine.bind_order_numbers(state)
        message=engine.build_message(state,s,{'action':'WAIT_LIMIT','events':[]},cfg)
        for text in ('Ціна зараз: 101.25',
                     'LONG • Вхід:','ОЧІКУЄМО ВХОДУ — 1'):
            self.assertIn(text,message)
        for noise in ('PAPER','не біржові ордери','Нових входів немає','PAPER ='):
            self.assertNotIn(noise,message)
        self.assertNotIn('У СУПРОВОДІ — 0',message)
        self.assertNotIn('Готовність до входу на момент',message)
        self.assertNotIn('модель',message)

    def test_signal_with_unknown_history_does_not_present_seventy_percent_probability(self):
        self.assertIn('ще не перевірена',engine.expected_line({'observations':39,'estimate':.95}))
        self.assertIn('95.0%',engine.expected_line({'observations':39,'estimate':.95}))
        text=engine.expected_line({'observations':40,'estimate':.80,'scope':'family','lower_95':.65})
        self.assertIn('сім’я сетапів',text)
        self.assertIn('80.0%',text)
        self.assertIn('65.0%',text)

    def test_watch_report_explains_one_best_scenario_without_fallback_phrase(self):
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg)
        watch=[{'setup':'SWEEP_RECLAIM','timeframe':'15m','side':1,'level':100.,
                'readiness_pct':75.,'met_conditions':3,'total_conditions':4,
                'reason':'SETUP_CONDITIONS_INCOMPLETE','missing_conditions':['Закриття повернулося над рівень']},
               {'setup':'BREAKOUT_RETEST','timeframe':'1H','side':-1,'level':103.,
                'readiness_pct':60.,'reason':'TARGET_RR_TOO_LOW'}]
        text=engine.build_message(state,s,{'action':'WATCH','analysis':{'watch':watch}},cfg)
        self.assertIn('готовність умов 75%',text)
        self.assertIn('Закриття повернулося над рівень',text)
        self.assertNotIn('BREAKOUT_RETEST',text)
        self.assertNotIn('Нових входів немає',text)

    def test_active_level_is_not_duplicated_as_an_unexplained_ready_setup(self):
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg)
        # Legacy fixtures verify messaging/pretouch logic, not ICT admission.
        with patch.object(engine.policy,'admit',return_value=(True,'CONFIRMED')), \
             patch.object(engine.policy,'choose',side_effect=lambda ps,*a,**kw:
                          (1,[p for p in ps if p['side']==1],'ISOLATED_TEST_DIRECTION')):
            result=engine.run_cycle(state,s,cfg)
        live=engine.open_plans(state)
        self.assertTrue(live)
        for row in result['analysis']['watch']:
            self.assertNotEqual(row['status'],'FOLLOWED')
        followed=[r for r in state['setup_matrix'] if r['status']=='FOLLOWED']
        self.assertTrue(followed)
        self.assertIn(live[0]['id'],[r['followed_id'] for r in followed])
        text=engine.build_message(state,s,result,cfg)
        self.assertLess(len(text),4001)

    def test_setup_priors_are_separate_by_timeframe(self):
        cfg=replace(engine.Config(),core=core);state=engine.new_state(cfg)
        for tf,net in [('15m',-1.),('4H',1.)]:
            state['trades'] += [{'setup_type':'SWEEP_RECLAIM','setup_timeframe':tf,
                                'bot_version_at_entry':engine.VERSION,'net_r':net} for _ in range(30)]
        stats=engine.setup_statistics(state,cfg)['SWEEP_RECLAIM']['timeframes']
        self.assertEqual(stats['15m']['status'],'NEGATIVE_EXPECTANCY')
        self.assertEqual(stats['4H']['status'],'POSITIVE_EXPECTANCY')
        self.assertEqual(stats['1H']['status'],'INSUFFICIENT_SAMPLE')


if __name__=='__main__':
    unittest.main()
