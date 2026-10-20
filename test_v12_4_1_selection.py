"""Regression of the reported 103.62 message, one winner and zero commissions."""
import copy
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import setup_scanner as scanner
import signal_quality as quality
from test_full_limit_engine import NOW, full_fixture, order, snapshot, analysis
from test_signal_quality import quality_snapshot


class PercentageAndWinnerTests(unittest.TestCase):
    def reported_rows(self):
        rows=[]
        for setup,tf,side,entry,n in [('RANGE_EDGE_REVERSAL','15m',-1,103.76,4),
                                      ('OPENING_RANGE_BREAKOUT','1H',1,103.47,3),
                                      ('OPENING_RANGE_BREAKOUT','4H',1,103.47,3)]:
            row={'setup':setup,'timeframe':tf,'side':side,'level':entry,'score':60.,
                 'status':'BLOCKED','reason':'TARGET_RR_TOO_LOW',
                 'setup_conditions':[{'condition':'pattern '+str(i),'met':True} for i in range(n)],
                 'entry_checks':scanner.entry_checks(),'gross_rr':.80,'required_rr':1.20,
                 'statistics':{'observations':0}}
            for check in row['entry_checks']:
                if check['key'] in ('PASSIVE_LIMIT','FRESH_LEVEL','VALID_STOP','HTF_ALIGNMENT'): check['met']=True
                if check['key']=='TARGET_RR': check['met']=False
            scanner.update_readiness(row);rows.append(row)
        return rows

    def test_reported_prices_have_distinct_distances_at_three_decimal_precision(self):
        short=scanner.proximity(103.62,103.76)['distance_pct']
        long=scanner.proximity(103.62,103.47)['distance_pct']
        self.assertAlmostEqual(short,.1351090523)
        self.assertAlmostEqual(long,.1447596989)
        self.assertEqual(f'{short:.2f}',f'{long:.2f}')
        self.assertEqual(f'{short:.3f}','0.135')
        self.assertEqual(f'{long:.3f}','0.145')

    def test_detected_pattern_with_failed_rr_is_not_one_hundred_percent_ready(self):
        for row in self.reported_rows():
            self.assertEqual(row['setup_readiness_pct'],100.)
            self.assertLess(row['readiness_pct'],100.)
            self.assertAlmostEqual(row['readiness_pct'],round(100*row['met_conditions']/row['total_conditions'],1))
            self.assertIn('Потенціал TP достатній відносно SL',row['missing_conditions'])

    def test_user_report_shows_one_highest_readiness_and_no_opposing_alternatives(self):
        cfg=replace(engine.Config(),core=core);s=snapshot(price=103.62)
        rows=self.reported_rows();state=engine.new_state(cfg)
        best=scanner.best_scenario(rows,s.price)
        self.assertEqual(best['setup'],'RANGE_EDGE_REVERSAL')
        text=engine.build_message(state,s,{'action':'WATCH','analysis':{'watch':rows}},cfg)
        self.assertEqual(text.count('НА СПОСТЕРЕЖЕННІ — ордер ще не активований'),1)
        self.assertIn('SHORT • 15m • Розворот від межі діапазону',text)
        self.assertIn('готовність умов 67%',text)
        self.assertIn('Рівень: 103.76',text)
        self.assertNotIn('OPENING_RANGE_BREAKOUT',text)
        self.assertNotIn('LONG',text)
        self.assertNotIn('комісі',text)
        self.assertIn('TP дає 0.80R; мінімум 1.20R',text)

    def test_closest_price_is_not_mistaken_for_highest_readiness(self):
        rows=self.reported_rows();rows[0].update(readiness_pct=80.,level=100.,score=50.)
        rows[1].update(readiness_pct=70.,level=103.621,score=99.)
        rows[2]['readiness_pct']=50.
        self.assertIs(scanner.best_scenario(rows,103.62),rows[0])

    def test_equal_readiness_uses_context_quality_before_distance(self):
        rows=self.reported_rows();rows[0].update(readiness_pct=90.,score=70.)
        rows[1].update(readiness_pct=90.,score=80.,level=101.)
        rows[2].update(readiness_pct=80.,score=95.)
        self.assertIs(scanner.best_scenario(rows,103.62),rows[1])

    def test_execution_ranks_all_ready_alternatives_and_excludes_incomplete(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        first=order();first.update(id='first',event_key='first',readiness_pct=100.,score=70.)
        other=order(-1);other.update(id='other',event_key='other',readiness_pct=90.,score=99.)
        third=copy.deepcopy(first);third.update(id='third',event_key='third',entry=99.,score=65.)
        with patch.object(engine,'analyze',return_value=analysis([other,third,first])):
            result=engine.run_cycle(state,snapshot(),cfg)
        self.assertEqual([p['id'] for p in result['selected_plans']],['first','third'])
        self.assertEqual(state['pending']['side'],1)
        self.assertEqual(state['second_slot']['pending']['id'],'third')

    def test_existing_direction_is_preserved_for_the_next_chosen_scenario(self):
        rows=self.reported_rows();rows[0]['readiness_pct']=95.;rows[1]['readiness_pct']=90.
        self.assertEqual(scanner.best_scenario(rows,103.62,sides={1})['side'],1)

    def test_real_geometry_failure_has_measured_rr_and_partial_readiness(self):
        cfg,ctx,a,s=full_fixture();diag={}
        with patch.object(engine,'full_targets',return_value=[{'kind':'test','level':a.level+.2,'distance':.2}]):
            plan,why=engine.full_plan(a,ctx,s,cfg,{},diag)
        self.assertIsNone(plan);self.assertEqual(why,'TARGET_DISTANCE_TOO_SMALL')
        self.assertLess(diag['gross_rr'],diag['required_rr'])
        row={'setup_conditions':[{'condition':'pattern','met':True}],**diag}
        scanner.update_readiness(row)
        self.assertLess(row['readiness_pct'],100.)
        self.assertFalse(next(c for c in row['entry_checks'] if c['key']=='TARGET_RR')['met'])

    def test_real_ready_rows_pass_all_counted_conditions(self):
        cfg,s,ctx,p=quality_snapshot()
        result=engine.analyze(s,cfg,engine.new_state(cfg))
        ready=[r for r in result['setup_matrix'] if r['status']=='LIMIT_READY']
        self.assertTrue(ready)
        for row in ready:
            self.assertEqual(row['readiness_pct'],100.)
            self.assertEqual(row['met_conditions'],row['total_conditions'])
            self.assertFalse(row['missing_conditions'])


class NoCommissionTests(unittest.TestCase):
    def test_old_workflow_fee_variables_do_not_reenable_commissions(self):
        with patch.dict(os.environ,{'INCLUDE_COMMISSION':'false','MAKER_FEE_RATE':'.01','TAKER_FEE_RATE':'.05'}):
            cfg=engine.Config.from_env()
        self.assertFalse(cfg.include_commission)
        self.assertEqual(cfg.maker_fee,0.)
        self.assertEqual(cfg.taker_fee,0.)
        self.assertEqual(engine.fees_r(100,102,1,cfg),0.)

    def test_invalid_commission_flag_fails_clearly(self):
        with patch.dict(os.environ,{'INCLUDE_COMMISSION':'maybe'}):
            with self.assertRaises(ValueError): engine.Config.from_env()

    def test_feasible_gross_rr_is_not_rejected_by_commission_filter(self):
        cfg,ctx,a,s=full_fixture();base,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNotNone(base,why)
        buffer=max(.15*ctx['atr3'],2*cfg.tick)
        distance=max(1.6*base['risk'],cfg.min_target_pct*base['entry'])+buffer
        targets=[{'kind':'test','level':a.level+distance,'distance':distance}]
        cfg=replace(cfg,maker_fee=.01,taker_fee=.05)
        with patch.object(engine,'full_targets',return_value=targets):
            free,why=engine.full_plan(a,ctx,s,cfg,{})
            paid,paid_reason=engine.full_plan(a,ctx,s,replace(cfg,include_commission=True),{})
        self.assertIsNotNone(free,why)
        self.assertEqual(free['costs_r'],0.)
        self.assertEqual(free['gross_rr'],free['net_rr'])
        self.assertIsNone(paid)
        self.assertEqual(paid_reason,'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS')

    def test_genuinely_insufficient_target_is_still_rejected_without_fees(self):
        cfg,ctx,a,s=full_fixture()
        with patch.object(engine,'full_targets',return_value=[{'kind':'test','level':a.level+.1,'distance':.1}]):
            p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNone(p)
        self.assertEqual(why,'TARGET_DISTANCE_TOO_SMALL')

    def test_result_has_zero_commissions_even_with_legacy_entry_fee_metadata(self):
        cfg=engine.Config();state=engine.new_state(cfg);p=order()
        p.update(entry_fee_rate=.01,remaining=.35,realized_gross_r=.8,realized_exit_fees_r=.2)
        state['active']=p
        engine.close_trade(state,p,101,NOW+900000,'TEST',cfg)
        trade=state['trades'][0]
        self.assertEqual(trade['fees_r'],0.)
        self.assertEqual(trade['net_r'],trade['gross_r'])

    def test_disabled_fee_changes_do_not_reset_model_or_state_configuration(self):
        cfg=engine.Config();changed=replace(cfg,maker_fee=.01,taker_fee=.05)
        self.assertEqual(engine.config_dict(cfg),engine.config_dict(changed))
        self.assertEqual(quality.signature(cfg),quality.signature(changed))

    def test_upgrade_archives_the_old_fee_model_but_preserves_live_levels(self):
        cfg=engine.Config();state=engine.new_state(cfg);state['pending']=order()
        saved=copy.deepcopy(state['pending'])
        state['quality']={'schema':quality.SCHEMA,'strategy':'native-mtf-pretouch-limit-v2',
                          'signature':'old','observations':[{'historic':True}],
                          'pending':[],'seen':{},'last_run_ts':NOW}
        model=quality.ensure(state,cfg)
        self.assertEqual(model['strategy'],quality.STRATEGY)
        self.assertFalse(model['observations'])
        self.assertEqual(state['quality_archives'][0]['observations'],[{'historic':True}])
        self.assertEqual(state['pending'],saved)


if __name__=='__main__': unittest.main()
