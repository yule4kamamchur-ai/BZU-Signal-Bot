"""Full-volume execution and truthful status regression scenarios."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import full_limit_engine as engine
import lifecycle_forecast
import notification_report
import signal_quality
from test_full_limit_engine import NOW, order, candle, snapshot, analysis, lifecycle_snapshot
import test_v12_4_3_geometry as geometry_fixtures


class FullVolumeExecution(unittest.TestCase):
    def plan(self, side=1):
        p=order(side)
        engine.use_full_exit(p,NOW)
        return p

    def test_new_native_plans_have_one_target_for_every_timeframe_and_side(self):
        for tf in ('15m','1H','4H'):
            for side in ('LONG','SHORT'):
                cfg,ctx,a,s=geometry_fixtures.MinimumDistanceTests().fixture(side,tf=tf)
                sign=1 if side=='LONG' else -1
                targets=[{'kind':'real','level':100+sign*3,'distance':3},
                         {'kind':'far','level':100+sign*5,'distance':5}]
                with patch.object(engine,'full_targets',return_value=targets):
                    p,why=engine.full_plan(a,ctx,s,cfg,{})
                self.assertIsNotNone(p,why)
                self.assertEqual(p['exit_policy'],'SINGLE_FULL_EXIT')
                self.assertEqual(p['partials'],{'TP1':1.})
                self.assertEqual(p['tp'],p['tp1'])
                self.assertGreaterEqual(sign*(p['tp2']-p['tp1']),0)
                self.assertGreaterEqual(sign*(p['tp3']-p['tp2']),0)

    def test_old_split_env_cannot_reenable_partial_closes(self):
        with patch.dict(os.environ,{'TP1_SIZE_PCT':'.65','TP2_SIZE_PCT':'.20'}):
            cfg=engine.Config.from_env()
        self.assertNotIn('partial_tp1',engine.config_dict(cfg))
        self.assertNotIn('partial_tp2',engine.config_dict(cfg))

    def test_tp_closes_the_full_long_and_short_without_partial_events_or_double_fees(self):
        for side in (1,-1):
            cfg=engine.Config(include_commission=True)
            state=engine.new_state(cfg);state['pending']=self.plan(side)
            first=candle(NOW,100.2,100.3,99.98,100.1) if side==1 else candle(NOW,99.8,100.02,99.7,99.9)
            target=candle(NOW+180000,100.1,102.2,100.1,102.) if side==1 else candle(NOW+180000,99.9,99.9,97.8,98.)
            bars=[first,target]
            snap=lifecycle_snapshot(bars)
            engine.advance_lifecycle(state,snap,cfg)
            self.assertTrue(state['active']['awaiting_tp1_review'])
            snap.price=target.close
            engine.amend_at_report(state,snap,cfg)
            self.assertIsNone(state['active'])
            self.assertEqual(len(state['trades']),1)
            t=state['trades'][0];raw=102 if side==1 else 98
            exit_price=raw*(1-side*.0002)
            fees=100*.0002+exit_price*.0005
            self.assertEqual(t['final_exit_fraction'],1.)
            self.assertEqual(t['realized_legs'],[])
            self.assertEqual(t['close_reason'],'ADAPTIVE_FULL_EXIT')
            self.assertAlmostEqual(t['net_r'],side*(exit_price-100)-fees)
            self.assertFalse([e for e in state['events'] if e['kind'] in ('TP1','TP2','TP3')])
            engine.advance_lifecycle(state,snap,cfg)
            self.assertEqual(len(state['trades']),1)
            self.assertEqual(len([e for e in state['events'] if e['kind']=='CLOSED']),1)

    def test_ambiguous_stop_closes_full_size_conservatively(self):
        cfg=engine.Config();state=engine.new_state(cfg);state['pending']=self.plan()
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,100.2,103,98,102)]),cfg)
        t=state['trades'][0]
        self.assertEqual(t['final_exit_fraction'],1.)
        self.assertEqual(t['close_reason'],'STOP')
        self.assertTrue(t['ambiguous_ohlc'])
        self.assertLess(t['net_r'],0)

    def test_timer_does_not_close_the_position_before_tp1(self):
        cfg=engine.Config();state=engine.new_state(cfg);p=self.plan()
        p.update(max_hold_minutes=3,intraday=False);state['pending']=p
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,100.2,100.3,99.98,100.1)]),cfg)
        self.assertFalse(state['trades'])
        self.assertEqual(state['active']['remaining'],1.)
        engine.manage_bar(state,candle(NOW+180000,100.1,100.2,98.9,99),cfg)
        self.assertEqual(state['trades'][0]['close_reason'],'STOP')
        self.assertEqual(state['trades'][0]['final_exit_fraction'],1.)

    def test_pending_migration_keeps_identity_entry_stop_expiry_and_history(self):
        state=engine.new_state(engine.Config());p=order()
        p.update(tp1=102,tp2=103,tp3=104,partials={'TP1':.65,'TP2':.2,'TP3':.15})
        state['pending']=p;state['trades']=[{'id':'old','net_r':.4}]
        before=copy.deepcopy(state)
        engine.upgrade_open_exits(state,NOW)
        for key in ('id','entry','stop','initial_stop','expires_ts','placed_ts'):
            self.assertEqual(p[key],before['pending'][key])
        self.assertEqual(p['tp'],102)
        self.assertEqual(p['partials'],{'TP1':1.})
        self.assertEqual(state['trades'],before['trades'])
        self.assertEqual(state['events'],before['events'])
        engine.upgrade_open_exits(state,NOW+180000)
        self.assertEqual(len(state['exit_policy_migrations']),1)

    def test_old_partial_position_keeps_realized_volume_and_closes_remainder_once(self):
        cfg=engine.Config(include_commission=True);state=engine.new_state(cfg)
        p=order();p.update(status='OPEN',filled_ts=NOW,remaining=.35,tp1_hit=True,
            tp1=102,tp2=103,tp3=104,partials={'TP1':.65,'TP2':.2,'TP3':.15},
            realized_gross_r=1.3,realized_exit_fees_r=.03315,realized_legs=[
                {'target':'TP1','price':102.,'fraction':.65,'ts':NOW}],exit_deadline=NOW+3600000)
        state['active']=p;engine.upgrade_open_exits(state,NOW)
        self.assertEqual(p['remaining'],.35)
        self.assertEqual(p['tp'],103.)
        self.assertEqual(p['realized_gross_r'],1.3)
        engine.manage_bar(state,candle(NOW+180000,102,103.2,101.5,103),cfg)
        t=state['trades'][0];exit_price=103*.9998
        self.assertEqual(t['final_exit_fraction'],.35)
        self.assertEqual(len(t['realized_legs']),1)
        self.assertAlmostEqual(t['net_r'],1.3+.35*(exit_price-100)-(.02+.03315+.35*exit_price*.0005))

    def test_previous_v126_quality_labels_are_archived_without_mixing(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        q=signal_quality.ensure(state,cfg)
        q['strategy']='native-mtf-full-lifecycle-continuous-learning-v5'
        q['observations']=[{'old':True}]
        new=signal_quality.ensure(state,cfg)
        self.assertEqual(new['observations'],[])
        self.assertEqual(state['quality_archives'][0]['observations'],[{'old':True}])
        self.assertIn('exit_policy',signal_quality.PLAN_FIELDS)

    def test_v126_upgrade_happens_after_past_bars_and_before_current_decisions(self):
        cfg=engine.Config();state=engine.new_state(cfg);state['pending']=order()
        state.update(version='full-ict-v12.6.0-lifecycle-adaptive-policy',config_hash='old')
        calls=[]
        def catch_up(book,*_):
            self.assertNotIn('exit_policy',book['pending']);calls.append('past')
        def current_decision(book,*_):
            self.assertEqual(book['pending']['exit_policy'],'SINGLE_FULL_EXIT');calls.append('current')
        with patch.object(engine,'advance_lifecycle',side_effect=catch_up), \
             patch.object(engine,'amend_at_report',side_effect=current_decision), \
             patch.object(engine,'analyze',return_value=analysis()):
            engine.run_cycle(state,snapshot(price=101),cfg)
        self.assertEqual(calls,['past','current'])
        self.assertEqual(state['version'],engine.VERSION)

    def test_two_pending_limits_do_not_prevent_a_third_waiting_candidate(self):
        cfg=engine.Config();state=engine.new_state(cfg);state['pending']=self.plan()
        other=self.plan();other.update(id='second',event_key='second',entry=98,stop=97,invalidation=97,tp=104,tp1=104)
        state['second_slot']['pending']=other
        candidate=self.plan();candidate.update(id='third',entry=96,event_key='third')
        with patch.object(engine,'analyze',return_value=analysis([candidate])):
            out=engine.run_cycle(state,snapshot(price=101),cfg)
        self.assertEqual(engine.occupied_slots(state),0)
        self.assertFalse(out['selected_plans'])
        self.assertEqual(len(engine.pending_plans(state)),2)
        self.assertIsNone(state['active'])
        self.assertIsNone(state['second_slot']['active'])


class ClearNotifications(unittest.TestCase):
    def test_zero_open_two_pending_has_no_contradictions_or_exit_percentages(self):
        cfg=engine.Config();state=engine.new_state(cfg);p=order()
        p.update(tp=108.14,tp1=108.14,tp2=109.25,tp3=110.3,partials={'TP1':.65,'TP2':.2,'TP3':.15})
        state['pending']=p
        second=copy.deepcopy(p);second['id']='second';state['second_slot']['pending']=second
        engine.upgrade_open_exits(state,NOW);engine.bind_order_numbers(state)
        text=engine.build_message(state,snapshot(),{'action':'WAIT_LIMIT','analysis':{'health':['SPREAD_TOO_WIDE']}},cfg)
        self.assertIn('Відкриті угоди: 0 • Ліміти в очікуванні: 2',text)
        self.assertEqual(text.count('LONG • Вхід:'),2)
        for plan in engine.pending_plans(state):
            self.assertEqual((plan['tp1'],plan['tp2'],plan['tp3']),(108.14,109.25,110.3))
        for banned in ('Увага: широкий спред','супровід наявних ордерів триває',
                       'У СУПРОВОДІ — 0','Відкритих угод немає','Зайнято місць',
                       'закрити 65%','закрити 20%','закрити 15%'):
            self.assertNotIn(banned,text)

    def test_empty_pending_and_active_sections_are_omitted(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        text=engine.build_message(state,snapshot(),{'action':'WATCH'},cfg)
        self.assertNotIn('У СУПРОВОДІ',text)
        self.assertNotIn('ОЧІКУЄМО ВХОДУ',text)

    def test_full_close_reason_is_one_clear_tp_result(self):
        t={'exit_policy':'SINGLE_FULL_EXIT','close_reason':'TAKE_PROFIT','tp1_hit':True}
        self.assertEqual(notification_report.close_reason(t),'тейк-профіт')

    def test_unsent_old_report_is_cleaned_but_its_close_result_is_delivered(self):
        message='''Ціна зараз: 100 • Зайнято місць: 2/2
Увага: широкий спред.
Нові входи на паузі; супровід наявних ордерів триває.
У СУПРОВОДІ — 0
Відкритих угод немає.
ОЧІКУЄМО ВХОДУ — 2
SL: 104.81 • TP1 108.14 (закрити 65%) • TP2 109.25 (закрити 20%) • TP3 110.3 (закрити 15%)
#027 ЗАКРИТО: -0.10R — збиток
за часом'''
        state=engine.new_state(engine.Config());state['notification_queue']=[{'text':message,'version':'old'}]
        sent=[]
        def send(url,body):
            sent.append(body['text']);return {'ok':True}
        with tempfile.TemporaryDirectory() as directory, patch.object(engine,'http_json',side_effect=send):
            self.assertTrue(engine.notify_queue(state,'test','test',Path(directory)/'state.json'))
        self.assertNotIn('SL:',sent[0])
        self.assertNotIn('TP2:',sent[0])
        self.assertIn('#027 ЗАКРИТО: -0.10R — збиток',sent[0])
        for banned in ('65%','20%','15%','Зайнято місць','СУПРОВОДІ — 0','супровід наявних'):
            self.assertNotIn(banned,sent[0])


class AdaptiveSupervision(unittest.TestCase):
    def state(self,price=101.8,side=1):
        cfg=engine.Config(include_commission=True)
        state=engine.new_state(cfg);p=order(side)
        p.update(status='OPEN',filled_ts=NOW-900000,remaining=1.,
            exit_deadline=NOW+3600000,last_bar=NOW-180000,
            tp=100+side*2,tp1=100+side*2,tp2=100+side*3,tp3=100+side*4,
            exit_policy='SINGLE_FULL_EXIT',full_exit_target='TP1',
            target_management='ADAPTIVE_FULL_VOLUME',tp1_decision_gate=True,partials={'TP1':1.},
            full_exit_candidates=[{'name':n,'price':100+side*d} for n,d in [('TP1',2),('TP2',3),('TP3',4)]])
        state['active']=p;s=snapshot(price=price)
        s.candles={'3m':[candle(NOW-360000,101,101.5,100.9,101.4),
                              candle(NOW-180000,101.4,101.9,101.3,price)]}
        return cfg,state,p,s

    def test_supporting_momentum_extends_to_tp2_without_closing_any_volume(self):
        cfg,state,p,s=self.state(102.1)
        p.update(tp1_hit=True,awaiting_tp1_review=True)
        engine.review_full_exit(state,s,cfg)
        self.assertEqual(p['pending_target']['name'],'TP2')
        self.assertEqual(p['remaining'],1.)
        self.assertFalse(state['trades'])
        self.assertEqual(p['full_exit_target'],'TP1')
        self.assertEqual(p['pending_target']['effective_ts'],NOW)
        engine.manage_bar(state,candle(NOW,102.1,102.2,102,102.15),cfg)
        self.assertEqual(state['active']['full_exit_target'],'TP2')
        self.assertEqual(state['active']['remaining'],1.)
        self.assertTrue(state['active']['tp1_hit'])

    def test_near_tp2_continuation_can_extend_to_tp3(self):
        cfg,state,p,s=self.state(102.8)
        p.update(tp=103.,full_exit_target='TP2',tp1_hit=True)
        engine.review_full_exit(state,s,cfg)
        self.assertEqual(p['pending_target']['name'],'TP3')
        self.assertEqual(p['remaining'],1.)

    def test_fading_momentum_after_tp1_exits_full_volume_at_current_quote(self):
        cfg,state,p,s=self.state(101.7)
        p.update(tp=103.,full_exit_target='TP2',tp1_hit=True)
        s.candles['3m']=[candle(NOW-360000,102.2,102.3,101.8,101.9),
                         candle(NOW-180000,101.9,102,101.6,101.7)]
        engine.review_full_exit(state,s,cfg)
        self.assertIsNone(state['active'])
        t=state['trades'][0]
        self.assertEqual(t['close_reason'],'ADAPTIVE_FULL_EXIT')
        self.assertEqual(t['final_exit_fraction'],1.)
        self.assertAlmostEqual(t['exit'],101.7*.9998)
        self.assertNotEqual(t['exit'],102.)
        self.assertEqual(t['closed_ts'],NOW)

    def test_short_continuation_is_mirrored(self):
        cfg,state,p,s=self.state(97.9,side=-1)
        p.update(tp1_hit=True,awaiting_tp1_review=True)
        s.candles['3m']=[candle(NOW-360000,99,99.1,98.5,98.6),
                         candle(NOW-180000,98.6,98.7,97.8,97.9)]
        engine.review_full_exit(state,s,cfg)
        self.assertEqual(p['pending_target']['name'],'TP2')
        self.assertEqual(p['pending_target']['price'],97.)

    def test_strong_obstacle_prevents_farther_target_even_with_good_momentum(self):
        cfg,state,p,s=self.state()
        p['exit_path_barriers']=[{'level':102.5,'hard_barrier':True}]
        engine.review_full_exit(state,s,cfg)
        self.assertNotIn('pending_target',p)
        self.assertEqual(p['full_exit_target'],'TP1')
        self.assertEqual(p['remaining'],1.)

    def test_new_strong_zone_closes_risk_at_current_quote(self):
        from dataclasses import replace
        import bot_oneshot as core
        cfg,state,p,s=self.state(102.2)
        cfg=replace(cfg,core=core)
        p.update(tp=103.,full_exit_target='TP2',tp1_hit=True)
        full=snapshot(price=102.2)
        full.candles['3m']=s.candles['3m']
        with patch.object(engine,'full_targets',return_value=[{'level':102.5,'hard_barrier':True}]):
            engine.review_full_exit(state,full,cfg)
        self.assertEqual(state['trades'][0]['close_reason'],'ADAPTIVE_FULL_EXIT')
        self.assertAlmostEqual(state['trades'][0]['exit'],102.2*.9998)

    def test_future_or_unclosed_candles_do_not_authorize_extension(self):
        cfg,state,p,s=self.state()
        s.candles['3m']=[candle(NOW,101,104,100,104),
                         candle(NOW-180000,101,104,100,104,False)]
        engine.review_full_exit(state,s,cfg)
        self.assertNotIn('pending_target',p)
        self.assertFalse(state['trades'])

    def test_old_candle_cannot_apply_future_target_amendment(self):
        cfg,state,p,s=self.state()
        p['pending_target']={'name':'TP2','price':103,'effective_ts':NOW+180000}
        engine.manage_bar(state,candle(NOW,101.8,102.2,101.7,102.1),cfg)
        self.assertEqual(state['active']['full_exit_target'],'TP1')
        self.assertTrue(state['active']['awaiting_tp1_review'])
        self.assertFalse(state['trades'])

    def test_repeat_review_does_not_repeat_an_exit_or_amendment(self):
        cfg,state,p,s=self.state()
        engine.review_full_exit(state,s,cfg);before=copy.deepcopy(state)
        engine.review_full_exit(state,s,cfg)
        self.assertEqual(state,before)


class UnlimitedWaitingQueue(unittest.TestCase):
    def pending(self,number,entry=100,score=80):
        p=order();p.update(id=f'p{number}',event_key=f'e{number}',entry=entry,
            stop=entry-1,initial_stop=entry-1,invalidation=entry-1,tp=entry+2,
            score=score,decision_score=score,effective_ts=NOW,intraday=False)
        engine.use_full_exit(p,NOW)
        return p

    def test_ten_waiting_plans_survive_restart_without_taking_open_slots(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        engine.park_pending(state,[self.pending(i,100-i*2) for i in range(10)])
        engine.bind_order_numbers(state)
        self.assertEqual(len(engine.pending_plans(state)),10)
        self.assertEqual(engine.occupied_slots(state),0)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';engine.atomic_write(path,state)
            restored=engine.load_state(path,cfg)
            self.assertEqual(engine.pending_plans(restored),engine.pending_plans(state))
            journal=Path(directory)/'journal.json';engine.export_journal(journal,restored)
            self.assertEqual(len(engine.read_json(journal)['full_v12']['pending_orders']),8)

    def test_arbitration_accounts_for_distance_before_seeing_candle_extremes(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        # The highest-scoring distant limit retains priority; the third lower
        # priority limit cannot be selected after observing a favorable touch.
        p1=self.pending(1,90,99);p2=self.pending(2,92,90);p3=self.pending(3,100,80)
        engine.park_pending(state,[p3,p2,p1])
        self.assertEqual([p['id'] for p in engine.entry_priority(state,NOW,101,cfg)],['p3','p1'])
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.2,99.9,100.5)]),cfg)
        self.assertEqual([p['id'] for p in engine.active_plans(state)],['p3'])
        self.assertEqual(len(engine.pending_plans(state)),2)
        self.assertTrue(p2['capacity_waited'])

    def test_three_simultaneous_triggers_cannot_create_a_third_fill(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        engine.park_pending(state,[self.pending(1,100,99),self.pending(2,98,90),self.pending(3,96,80)])
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.1,95.9,100)]),cfg)
        fills=[o for o in state['orders'] if o['status']=='FILLED']
        self.assertEqual({p['id'] for p in fills},{'p1','p2'})
        self.assertEqual([p['id'] for p in engine.pending_plans(state)],['p3'])
        self.assertLessEqual(engine.occupied_slots(state),2)

    def test_two_active_positions_block_new_fills_but_keep_all_waiting_plans(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        for number,view in engine.slot_views(state):
            p=self.pending(number,100+number*10)
            p.update(status='OPEN',remaining=1.,stop=90,exit_deadline=NOW+3600000,last_bar=NOW-180000,tp=140,tp1=140)
            view['active']=p;engine.store_slot(state,number,view)
        engine.park_pending(state,[self.pending(i,100) for i in range(3,8)])
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.2,99.9,100.5)]),cfg)
        self.assertEqual(len(engine.active_plans(state)),2)
        self.assertEqual(len(engine.pending_plans(state)),5)
        self.assertFalse(state['orders'])

    def test_a_close_does_not_free_a_slot_earlier_in_the_same_bar(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        for number,view in engine.slot_views(state):
            p=self.pending(number,100+number*10)
            p.update(status='OPEN',remaining=1.,stop=90,exit_deadline=NOW+3600000,last_bar=NOW-180000,tp=140,tp1=140)
            if number==1:p['stop']=100
            view['active']=p;engine.store_slot(state,number,view)
        engine.park_pending(state,[self.pending(3,100)])
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,101,101.2,99.9,100.5)]),cfg)
        self.assertEqual(len(state['trades']),1)
        self.assertEqual(len(engine.active_plans(state)),1)
        self.assertEqual(len(engine.pending_plans(state)),1)
        self.assertFalse([o for o in state['orders'] if o['status']=='FILLED'])

    def test_overflow_cancel_expiry_and_numbering_do_not_erase_other_waiting_plans(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        plans=[self.pending(i,100-i*2) for i in range(5)]
        engine.park_pending(state,plans);engine.bind_order_numbers(state)
        labels={p['id']:p['display_id'] for p in plans}
        engine.request_cancel(state,plans[3],'STRUCTURE_INVALIDATED_NOW',NOW)
        self.assertEqual(len(engine.pending_plans(state)),4)
        for p in engine.pending_plans(state):self.assertEqual(p['display_id'],labels[p['id']])
        plans[4]['expires_ts']=NOW+180000
        engine.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,120,120.2,119.9,120)]),cfg)
        self.assertEqual({p['id'] for p in engine.pending_plans(state)},{'p0','p1','p2'})
        self.assertEqual({o['status'] for o in state['orders']},{'EXPIRED','CANCELLED'})

    def test_all_waiting_orders_appear_in_report_without_occupied_slots_banner(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        engine.park_pending(state,[self.pending(i,100-i*2) for i in range(6)])
        engine.bind_order_numbers(state)
        text=engine.build_message(state,snapshot(),{'action':'WAIT_LIMIT'},cfg)
        self.assertIn('Відкриті угоди: 0 • Ліміти в очікуванні: 6',text)
        self.assertNotIn('Зайнято місць',text)
        for n in range(1,7):self.assertIn(f'#{n:03d}',text)


class TargetModelAndEvidence(unittest.TestCase):
    def test_initial_plan_preserves_three_targets_but_waits_for_actual_tp1(self):
        p=order();p.update(tp=104,tp1=102,tp2=103,tp3=104,exit_policy='SINGLE_FULL_EXIT',
            full_exit_target='TP3',partials={'TP3':1.},
            full_exit_candidates=[{'name':n,'price':v} for n,v in [('TP1',102),('TP2',103),('TP3',104)]])
        out=lifecycle_forecast.select_full_exit(p,snapshot(price=101),engine.Config(),engine,paths=4)
        self.assertEqual(out['status'],'WAIT_FOR_OBSERVED_TP1')
        self.assertEqual(p['tp'],102)
        self.assertTrue(p['tp1_decision_gate'])
        self.assertEqual(p['tp2'],103)
        self.assertEqual(p['tp3'],104)
        self.assertTrue(out['review_during_supervision'])

    def test_model_estimate_cannot_override_the_tp1_decision_gate(self):
        p=order();p.update(tp1=102,tp2=103,tp3=104,exit_policy='SINGLE_FULL_EXIT',
            full_exit_target='TP1',partials={'TP1':1.})
        prediction={'status':'MODEL_ONLY','expectancy_r_per_placed':-.2,
                    'net_win_probability_given_fill':.1}
        with patch.object(lifecycle_forecast,'forecast',return_value=prediction):
            out=lifecycle_forecast.select_full_exit(p,snapshot(),engine.Config(),engine)
        self.assertEqual(out['selected'],'TP1')
        self.assertEqual(p['tp'],102)
        self.assertEqual(p['partials'],{'TP1':1.})

    def test_queue_blocked_labels_are_not_misreported_as_unconditional_fill_calibration(self):
        import research_v12_6
        state=engine.new_state(engine.Config());p=order()
        p.update(status='EXPIRED',capacity_waited=True,decision_forecast={
            'status':'MODEL_ONLY','fill_probability':.99,
            'eligibility_assumption':'FREE_POSITION_CAPACITY_AND_ENTRY_PRIORITY'})
        state['orders']=[p]
        report=research_v12_6.summarize({'trades':[]},state)['forecast_evaluation']
        self.assertEqual(report['fill_labels'],0)
        self.assertEqual(report['queue_conditioned_excluded'],1)


class FirstTargetGate(unittest.TestCase):
    def test_no_early_exit_or_tp_extension_even_when_quote_is_just_below_tp1(self):
        cfg,state,p,s=AdaptiveSupervision().state(101.99)
        p.update(strong_closes=5)
        before=copy.deepcopy(p)
        engine.amend_at_report(state,s,cfg)
        self.assertFalse(state['trades'])
        self.assertNotIn('pending_target',p)
        self.assertNotIn('pending_stop',p)
        self.assertEqual(p['stop'],before['stop'])
        self.assertEqual(p['tp'],before['tp'])

    def test_full_position_waits_for_a_real_tp1_observation_before_review(self):
        cfg,state,p,s=AdaptiveSupervision().state(102.1)
        p['tp1_hit']=False
        engine.review_full_exit(state,s,cfg)
        self.assertFalse(state['trades'])
        self.assertNotIn('pending_target',p)
        p.update(tp1_hit=True,awaiting_tp1_review=True)
        engine.review_full_exit(state,s,cfg)
        self.assertEqual(p['pending_target']['name'],'TP2')
        self.assertEqual(p['remaining'],1.)

    def test_pre_tp1_displacement_zone_does_not_trigger_discretionary_closure(self):
        from dataclasses import replace
        import bot_oneshot as core
        cfg,state,p,s=AdaptiveSupervision().state(101.8)
        cfg=replace(cfg,core=core)
        with patch.object(engine,'full_targets',return_value=[{'level':101.9,'hard_barrier':True}]):
            engine.review_full_exit(state,s,cfg)
        self.assertFalse(state['trades'])
        self.assertEqual(p['remaining'],1.)

    def test_new_uncertain_position_is_not_erased_on_the_old_time_exit_timer(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        p=order();engine.use_full_exit(p,NOW)
        p.update(status='UNRESOLVED',reason='PLACEMENT_INSIDE_TOUCHED_BAR',resolved_ts=NOW)
        state['orders']=[p]
        state['reconciliation']={'order_id':p['id'],'reason':p['reason'],'ts':NOW}
        engine.release_unconfirmed_paper(state,NOW+2*86400000,cfg)
        self.assertIsNotNone(state['reconciliation'])
        self.assertFalse(state['trades'])
        self.assertEqual(p['status'],'UNRESOLVED')

    def test_audit_counts_overflow_waiting_orders(self):
        from analyze_bot import audit
        cfg=engine.Config();state=engine.new_state(cfg);state['last_run_ts']=NOW
        fixture=UnlimitedWaitingQueue()
        engine.park_pending(state,[fixture.pending(i,100-i*2) for i in range(7)])
        result=audit(state)
        self.assertEqual(result['orders_current_strategy']['pending'],7)
        self.assertEqual(result['orders_current_strategy']['active'],0)
        self.assertEqual(result['integrity']['occupied_slots'],0)


class CensoredFullPositionForecast(unittest.TestCase):
    def test_open_paths_keep_fill_probability_without_inventing_net_results(self):
        p=order();engine.use_full_exit(p,NOW)
        with patch.object(lifecycle_forecast,'simulate',return_value={'status':'CENSORED','filled':True}):
            forecast=lifecycle_forecast.forecast(p,snapshot(price=101),engine.Config(),engine,paths=4)
        self.assertEqual(forecast['status'],'MODEL_INCOMPLETE')
        self.assertEqual(forecast['fill_probability'],1.)
        self.assertEqual(forecast['filled_unresolved_paths'],4)
        self.assertIsNone(forecast['net_win_probability_given_fill'])
        self.assertIsNone(forecast['expectancy_r_per_placed'])

    def test_migration_removes_an_unapplied_early_stop_amendment_only(self):
        p=order();p.update(pending_stop={'price':100.2,'effective_ts':NOW+180000})
        engine.use_full_exit(p,NOW)
        self.assertNotIn('pending_stop',p)
        self.assertEqual(p['stop'],99)
        self.assertEqual(p['legacy_pending_stop']['price'],100.2)


class SpreadOnlyReport(unittest.TestCase):
    def test_updated_wide_spread_snapshot_does_not_claim_supervision_or_data_freeze(self):
        cfg=engine.Config();state=engine.new_state(cfg);state['last_run_ts']=NOW
        text=engine.build_message(state,snapshot(),{'action':'DATA_HOLD','analysis':{'health':['SPREAD_TOO_WIDE']}},cfg)
        self.assertNotIn('Дані не оновлені',text)
        self.assertNotIn('Увага:',text)
        self.assertNotIn('супровід наявних',text)
        self.assertNotIn('У СУПРОВОДІ',text)


if __name__=='__main__':
    unittest.main()
