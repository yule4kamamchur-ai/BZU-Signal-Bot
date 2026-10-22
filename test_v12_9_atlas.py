"""Synthetic causal regressions; never market evidence or training labels."""
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as e
import execution_atlas as atlas
import setup_paths
import setup_scanner
import limit_structure
import continuous_worker
import lifecycle_forecast
import notification_report
import signal_quality
from test_full_limit_engine import NOW,candle,snapshot,order,analysis,lifecycle_snapshot
from test_v12_8_paths import witness


class EarlyPaths(unittest.TestCase):
    def test_real_closed_micro_sweep_precedes_primary_pattern_in_both_directions(self):
        cfg=replace(e.Config(include_commission=True),core=core,tick=.01)
        for side in (1,-1):
            s=snapshot(price=100.3)
            s.candles={tf:[candle(NOW-(80-i)*step,100.1,100.4,99.6,100.1) for i in range(80)] for tf,step in e.TF.items()}
            for tf in ('15m','1H','4H'):
                s.candles[tf][-32]=candle(s.candles[tf][-32].ts,100.1,106.,99.6,100.1)
                s.candles[tf][-22]=candle(s.candles[tf][-22].ts,100.1,100.4,94.,100.1)
            s.candles['3m'][-1]=candle(NOW-180000,100.1,100.5,99.2,100.3)
            if side==-1:
                s.price=200-s.price
                s.candles={tf:[candle(c.ts,200-c.open,200-c.low,200-c.high,200-c.close) for c in rows] for tf,rows in s.candles.items()}
            s.bid=s.price-.01;s.ask=s.price+.01
            state=e.new_state(cfg)
            result=e.run_cycle(state,s,cfg)
            name='SWEEP_RECLAIM' if side==1 else 'LIQUIDITY_SWEEP_REVERSAL_SHORT'
            # Structural detection survives direction arbitration even if
            # a different confirmed scenario wins this portfolio cycle.
            plans=[p for p in result['analysis']['plans'] if p['setup']==name and p['side']==side]
            self.assertTrue(plans,(side,result['analysis']['admission_by_side']))
            self.assertLessEqual(len({p['side'] for p in result['selected_plans']}),1)
            p=plans[0]
            self.assertEqual(p['formation_timeframe'],'3m')
            self.assertEqual(p['setup_timeframe'],'15m')
            self.assertEqual(p['entry_route'],'MICRO_PRECURSOR')
            native=result['analysis']['timeframe_contexts']['15m']
            detector=core.DETECTORS[setup_scanner.SETUP_NAMES.index(name)]
            self.assertIsNone(detector(native))
            self.assertGreaterEqual(p['net_rr'],cfg.min_net_rr)
            self.assertGreaterEqual(p['tp1_distance_pct'],1.5)
            self.assertGreaterEqual(p['stop_distance_pct'],1.)

    def test_original_pullback_does_not_hide_closer_actual_references(self):
        ctx=witness('PULLBACK_CONTINUATION');ctx['price']=102.
        ctx['zones']['order_blocks']=[core.Zone('DEMAND_OB','LONG',95.,96.,NOW-7200000,'15m',4.)]
        with e.market_clock(core,NOW):
            raw=core.detect_pullback_continuation(ctx)
            paths=setup_paths.candidates(core.detect_pullback_continuation,'PULLBACK_CONTINUATION',ctx,core)
        self.assertLess(raw.level,97.)
        self.assertTrue(any(99.<a.level<101. for a in paths))

    def test_displacement_is_excluded_from_its_own_fresh_base(self):
        ctx=witness('FRESH_BASE_CONTINUATION');rows=ctx['candles']['15m']
        rows[-1]=candle(rows[-1].ts,100.,103.2,99.9,103.)
        ctx['zones']['consolidation']=core.detect_consolidation(rows,ctx['atr15'])
        self.assertFalse(ctx['zones']['consolidation']['found'])
        with e.market_clock(core,NOW):
            self.assertIsNone(core.detect_fresh_base_continuation(ctx))
            paths=setup_paths.candidates(core.detect_fresh_base_continuation,'FRESH_BASE_CONTINUATION',ctx,core)
        self.assertTrue(any(a.evidence.get('base_known_before_displacement') for a in paths))

    def test_all_24_have_explicit_native_and_micro_evaluations(self):
        cfg=replace(e.Config(),core=core);s=snapshot()
        result=e.analyze(s,cfg,e.new_state(cfg))
        self.assertEqual(len(result['setup_matrix']),72)
        self.assertEqual(len(result['detectors']),24)
        self.assertFalse([r for r in result['setup_matrix'] if r['status']=='ERROR'])
        for row in result['detectors']:
            native=next(v for v in row['timeframes'] if v['timeframe']=='15m')
            self.assertIn('micro_precursors',native)


class CausalReferences(unittest.TestCase):
    def test_early_reversal_proof_uses_one_shared_htf_contract(self):
        f={'setup':'LIQUIDITY_SWEEP_REVERSAL_SHORT','side':-1,'side3':-1,'side15':1,
           'shock':False,'cost_fraction':0.,'distance_atr':.2,'entry_policy':'PRETOUCH_LIMIT',
           'detector_confirmed':True,'htf_opposition':2,'early_reversal_control':True}
        self.assertTrue(signal_quality.pretouch_countertrend_allowed(f['setup'],-1,1,-1,True))
        self.assertEqual(signal_quality.structural_reason(f),'CONFIRMED')
        f['early_reversal_control']=False
        self.assertEqual(signal_quality.structural_reason(f),'HTF_REVERSAL_NOT_CONFIRMED')

    def test_a_range_edge_alone_never_bypasses_two_opposing_htfs(self):
        cfg=replace(e.Config(),core=core)
        self.assertFalse(signal_quality.early_reversal_control('RANGE_EDGE_REVERSAL',-1,snapshot(),{},cfg,e))

    def test_only_a_new_opposite_cross_cancels_a_waiting_continuation(self):
        cfg=replace(e.Config(),core=core);s=snapshot();p=order();p['setup']='PULLBACK_CONTINUATION'
        with patch.object(atlas,'causal_shift',return_value={'shift':True,'side':'SHORT','known_ts':NOW+1}),\
             patch.object(core,'structure_snapshot',return_value={'direction':'SHORT'}):
            self.assertTrue(atlas.control_reversed(p,s,cfg,e))
            p['placed_ts']=NOW+2
            self.assertFalse(atlas.control_reversed(p,s,cfg,e))
            p['setup']='SWEEP_RECLAIM';p['placed_ts']=NOW
            self.assertFalse(atlas.control_reversed(p,s,cfg,e))

    def test_a_small_chart_flip_does_not_invalidate_a_four_hour_pullback(self):
        cfg=replace(e.Config(),core=core);s=snapshot();p=order()
        p.update(setup='PULLBACK_CONTINUATION',setup_timeframe='4H')
        def crosses(rows,tf,engine,tick):
            return {'shift':tf=='3m','side':'SHORT','known_ts':NOW+1}
        with patch.object(atlas,'causal_shift',side_effect=crosses),patch.object(core,'structure_snapshot',return_value={'direction':'SHORT'}):
            self.assertFalse(atlas.control_reversed(p,s,cfg,e))
    def test_pivot_cannot_be_used_before_its_confirmation_close(self):
        rows=[candle(NOW-(8-i)*180000,100.,101.2,99.,100.) for i in range(8)]
        rows[-1]=candle(rows[-1].ts,100.,102.,99.,101.5)
        p={'kind':'HIGH','level':101.,'ts':rows[-3].ts,'known_ts':rows[-1].ts}
        with patch.object(e,'pivots',return_value=[p]):
            self.assertFalse(atlas.causal_shift(rows,'3m',e,.01)['shift'])
            p['known_ts']=rows[-2].ts
            shift=atlas.causal_shift(rows,'3m',e,.01)
        self.assertTrue(shift['shift'])
        self.assertEqual(shift['known_ts'],NOW)
        self.assertLessEqual(shift['pivot_known_ts'],rows[-1].ts)

    def test_remaining_beyond_pivot_is_not_a_new_crossing_every_scan(self):
        rows=[candle(NOW-(8-i)*180000,102.,103.2,101.8,103.) for i in range(8)]
        p={'kind':'HIGH','level':101.,'ts':NOW-5000000,'known_ts':NOW-4000000}
        with patch.object(e,'pivots',return_value=[p]):
            self.assertFalse(atlas.causal_shift(rows,'3m',e,.01)['shift'])

    def test_week_boundary_excludes_the_event_window(self):
        start=NOW-10*3600000
        ctx={'session':{'week_ts':start,'week_complete':True},'candles':{
            '1H':[candle(start+i*3600000,100.,102.,99.,101.) for i in range(10)],
            '15m':[candle(NOW-(12-i)*900000,100.,150.,90.,120.) for i in range(12)]}}
        frozen=atlas.frozen_week(ctx)
        self.assertEqual(frozen['week_high'],102.)
        self.assertLess(frozen['reference_as_of_ts'],ctx['candles']['15m'][-1].ts)
        ctx['candles']['1H']=ctx['candles']['1H'][1:]
        self.assertIsNone(atlas.frozen_week(ctx))


class QueueArming(unittest.TestCase):
    def plan(self,side=1):
        p=order(side);p.update(queue_execution_policy='PASSIVE_ARM_V2',effective_ts=NOW)
        return p

    def test_model_budget_does_not_limit_the_waiting_queue(self):
        cfg=e.Config();state=e.new_state(cfg);plans=[]
        for i in range(9):
            p=self.plan();p.update(id=str(i),event_key=str(i),entry=100.-i,score=80.-i,readiness_pct=100.)
            plans.append(p)
        modeled=[]
        def model(p,*args,**kwargs):
            modeled.append(p['id'])
            return {'status':'MODEL_INCOMPLETE','as_of_ts':NOW,'calibrated':False}
        with patch.object(e,'analyze',return_value=analysis(plans)),patch.object(lifecycle_forecast,'forecast',side_effect=model):
            result=e.run_cycle(state,snapshot(price=101.),cfg)
        self.assertLessEqual(len(result['selected_plans']),2)
        self.assertLessEqual(len(e.pending_plans(state)),2)
        self.assertEqual(e.occupied_slots(state),0)
        self.assertLessEqual(len(set(modeled)),6)

    def test_no_catchup_fill_for_newly_available_marketable_limit(self):
        cfg=e.Config();state=e.new_state(cfg);state['pending']=self.plan()
        s=lifecycle_snapshot([candle(NOW,99.5,99.8,99.2,99.4)])
        e.advance_lifecycle(state,s,cfg)
        self.assertFalse(e.active_plans(state))
        self.assertEqual(state['orders'][-1]['reason'],'LEVEL_PASSED_WHILE_UNARMED')
        self.assertFalse(state['trades'])

    def test_queue_migration_does_not_change_the_already_started_bar(self):
        cfg=e.Config();state=e.new_state(cfg);p=self.plan()
        p['queue_policy_effective_ts']=NOW+180000;state['pending']=p
        self.assertEqual([x['id'] for x in e.entry_priority(state,NOW,99.8,cfg)],[p['id']])
        self.assertEqual(e.entry_priority(state,NOW+180000,99.8,cfg),[])

    def test_continuously_armed_limit_keeps_a_causal_gap_fill(self):
        cfg=e.Config();state=e.new_state(cfg);state['pending']=self.plan()
        bars=[candle(NOW,101.,101.2,100.2,100.5),candle(NOW+180000,99.8,100.,99.4,99.6)]
        e.advance_lifecycle(state,lifecycle_snapshot(bars),cfg)
        self.assertEqual(len(e.active_plans(state)),1)
        self.assertEqual(state['active']['filled_ts'],NOW+180000)
        self.assertEqual(state['active']['entry'],100.)

    def test_unarmed_crossing_in_a_full_book_is_retired_without_third_fill(self):
        cfg=e.Config();state=e.new_state(cfg)
        for number,view in e.slot_views(state):
            active=self.plan();active.update(id=str(number),event_key=str(number),entry=105.,risk=10.,
                stop=95.,tp=120.,status='OPEN',remaining=1.,last_bar=NOW-180000,filled_ts=NOW-180000,
                exit_deadline=NOW+3600000,tp1_decision_gate=True)
            view['active']=active;e.store_slot(state,number,view)
        e.park_pending(state,[self.plan()])
        e.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101.,101.2,99.8,100.5)]),cfg)
        self.assertEqual(len(e.active_plans(state)),2)
        self.assertFalse(e.pending_plans(state))
        self.assertEqual(state['orders'][-1]['reason'],'LEVEL_PASSED_WHILE_UNARMED')

    def test_daily_loss_never_cancels_waiting_alternatives(self):
        cfg=e.Config();state=e.new_state(cfg);state['pending']=self.plan()
        state['trades']=[{'id':'loss','net_r':-3.1,'closed_ts':NOW-180000,'side':1,'entry_version':'OLD'}]
        with patch.object(e,'analyze',return_value=analysis()):
            result=e.run_cycle(state,snapshot(price=101.),cfg)
        self.assertEqual(len(e.pending_plans(state)),1)
        self.assertEqual(result['action'],'WAIT_LIMIT')
        self.assertFalse(state['orders'])
        message=e.build_message(state,snapshot(),result,cfg)
        self.assertIn('ОЧІКУЄМО ВХОДУ',message)
        self.assertNotIn('денний ліміт ризику',message)

    def test_future_closed_label_cannot_pause_an_earlier_entry(self):
        cfg=e.Config();state=e.new_state(cfg)
        state['trades']=[{'net_r':-10.,'closed_ts':NOW+180000}]
        self.assertEqual(e.day_stats(state,NOW,cfg)['losses_r'],0.)

    def test_shadow_and_selected_continuations_share_the_same_cancellation(self):
        cfg=replace(e.Config(),core=core);state=e.new_state(cfg)
        p=self.plan();p.update(setup='PULLBACK_CONTINUATION',tp=104.,qualified=True,
                              decision_ts=NOW,features={'setup':'PULLBACK_CONTINUATION'})
        state['pending']=copy.deepcopy(p);state['last_run_ts']=NOW
        shadow=e.new_state(cfg);q=signal_quality.ensure(shadow,cfg)
        q['last_run_ts']=NOW;q['pending']=[{'pending':copy.deepcopy(p),'active':None}]
        s=snapshot(NOW+900000,price=101.)
        with patch.object(atlas,'control_reversed',return_value=True),patch.object(e,'analyze',return_value=analysis()):
            e.run_cycle(state,s,cfg)
            signal_quality.advance(shadow,s,cfg,e)
        self.assertFalse(e.pending_plans(state))
        self.assertFalse(q['pending'])
        self.assertEqual(state['orders'][-1]['reason'],'EXECUTION_CONTROL_REVERSED')


class ZoneAcceptance(unittest.TestCase):
    def test_three_closes_fill_only_the_traded_part_of_a_gap(self):
        native=[candle(NOW-4*3600000,100.,102.1,99.8,102.),candle(NOW-3600000,101.,101.5,100.5,101.)]
        rows=[candle(NOW-(3-i)*900000,100.8-i*.1,101.,100.1,100.6-i*.15) for i in range(3)]
        zone=core.Zone('FVG_BULL','LONG',99.,101.,NOW-5*3600000,'1H',2.)
        life=limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,rows,'15m')
        self.assertTrue(life['hard_barrier'])
        self.assertEqual(life['remaining_low'],99.)
        self.assertAlmostEqual(life['remaining_high'],100.6)
        self.assertGreater(life['accepted_depth_fraction'],0.)
        self.assertEqual(life['prefix_acceptance_known_ts'],NOW)

    def test_partial_gap_acceptance_does_not_weaken_an_order_block(self):
        zone=core.Zone('DEMAND_OB','LONG',99.,101.,NOW-5*3600000,'1H',2.)
        native=[candle(NOW-3600000,101.,101.5,100.5,101.)]
        rows=[candle(NOW-(3-i)*900000,100.8,101.,100.1,100.5) for i in range(3)]
        life=limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,rows,'15m')
        self.assertEqual(life['remaining_high'],101.)
        self.assertTrue(life['hard_barrier'])

    def test_a_wick_alone_does_not_fill_gap_acceptance_geometry(self):
        zone=core.Zone('FVG_BULL','LONG',99.,101.,NOW-5*3600000,'1H',2.)
        native=[candle(NOW-3600000,101.,101.5,100.5,101.)]
        rows=[candle(NOW-(3-i)*900000,101.1,101.2,99.1,101.1) for i in range(3)]
        life=limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,rows,'15m')
        self.assertEqual(life['remaining_high'],101.)
        self.assertEqual(life['accepted_depth_fraction'],0.)
    def test_execution_acceptance_retires_zone_before_parent_bar_closes(self):
        zone=core.Zone('DEMAND_OB','LONG',99.,100.,NOW-5*3600000,'1H',2.)
        native=[candle(NOW-3600000,100.5,101.,99.5,100.)]
        rows=[candle(NOW-900000,100.,100.1,98.4,98.5),
              candle(NOW-600000,98.5,98.6,98.,98.1),candle(NOW-300000,98.1,98.2,97.7,97.8)]
        # Use 3m spacing and a supplied 3m clock, never relabeled partial bars.
        rows=[replace(c,ts=NOW-(3-i)*180000) for i,c in enumerate(rows)]
        life=limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,rows,'3m')
        self.assertEqual(life['state'],'BROKEN_ACCEPTED')
        self.assertEqual(life['acceptance_known_ts'],NOW)
        self.assertFalse(life['hard_barrier'])

    def test_wicks_or_gapped_execution_bars_cannot_erase_strong_zone(self):
        zone=core.Zone('DEMAND_OB','LONG',99.,100.,NOW-5*3600000,'1H',2.)
        native=[candle(NOW-3600000,100.5,101.,99.5,100.)]
        wicks=[candle(NOW-(3-i)*180000,100.,100.2,98.,100.1) for i in range(3)]
        self.assertTrue(limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,wicks,'3m')['hard_barrier'])
        gaps=[candle(NOW-(6-i*2)*180000,98.8-i*.5,99.-i*.5,97.5-i*.5,98.-i*.5) for i in range(3)]
        self.assertTrue(limit_structure.zone_lifecycle(zone,native,'1H',NOW,1.,.01,gaps,'3m')['hard_barrier'])


class WorkerClock(unittest.TestCase):
    def test_model_uses_the_same_declared_review_cadence(self):
        cfg=replace(e.Config(),analysis_scan_minutes=3)
        p=order();p.update(tp1=102.,tp2=103.,tp3=104.)
        s=snapshot(price=101.)
        first=lifecycle_forecast.forecast(p,s,cfg,e,paths=2)
        second=lifecycle_forecast.forecast(p,s,replace(cfg,analysis_scan_minutes=5),e,paths=2)
        self.assertEqual(first['review_interval_minutes'],3)
        self.assertEqual(second['review_interval_minutes'],5)
        self.assertNotEqual(first['fingerprint'],second['fingerprint'])

    def test_cadence_can_change_with_open_plan_without_changing_levels(self):
        cfg=e.Config();state=e.new_state(cfg);p=order();p['tp']=104.;state['pending']=p
        before=(p['entry'],p['stop'],p['tp'])
        with patch.object(e,'analyze',return_value=analysis()):
            e.run_cycle(state,snapshot(price=101.),replace(cfg,analysis_scan_minutes=3))
        after=e.pending_plans(state)[0]
        self.assertEqual((after['entry'],after['stop'],after['tp']),before)
        self.assertEqual(state['runtime_config']['analysis_scan_minutes'],3)

    def test_queued_waiting_compaction_preserves_cancellation_explanations(self):
        text='ОЧІКУЄМО ВХОДУ — 1\n#001 LONG • Вхід: 100\nСКАСУВАННЯ ЛІМІТІВ\n#002 • денний ліміт ризику\nЗАКРИТО З МИНУЛОГО ЗВІТУ\nНових закриттів немає.'
        cleaned=notification_report.clean_queued_message(text)
        self.assertIn('#001 LONG • Вхід: 100',cleaned)
        self.assertIn('#002 • денний ліміт ризику',cleaned)
    def test_scan_every_three_minutes_report_every_fifteen(self):
        checkpoint={};calls=[]
        def run(notify):calls.append(notify);return 0
        for minute in range(61):
            checkpoint,_=continuous_worker.step(NOW+minute*60000+5000,checkpoint,run)
        self.assertEqual(len(calls),21)
        self.assertEqual(sum(calls),5)
        self.assertEqual([i for i,v in enumerate(calls) if v],[0,5,10,15,20])

    def test_delivery_failure_does_not_rebuild_a_report_every_second(self):
        now=NOW+5000
        checkpoint,ran=continuous_worker.step(now,{},lambda notify:2)
        self.assertTrue(ran)
        self.assertFalse(continuous_worker.due(now+1000,checkpoint)[0])

    def test_failed_cycle_does_not_consume_the_notification_slot(self):
        original={}
        with self.assertRaises(RuntimeError):continuous_worker.step(NOW+5000,original,lambda notify:1)
        self.assertEqual(original,{})

    def test_settlement_delay_waits_for_closed_candle_publication(self):
        p={'scan_bucket':NOW//180000-1,'report_bucket':NOW//900000-1}
        self.assertFalse(continuous_worker.due(NOW+4999,p)[0])
        self.assertTrue(continuous_worker.due(NOW+5000,p)[1])


if __name__=='__main__':unittest.main()
