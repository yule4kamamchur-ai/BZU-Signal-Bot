"""v12.12: leakage, calibration holdout, abstention and cold-start tests.

Fixtures are SYNTHETIC; success here is not evidence of trading profitability.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import directional_policy as policy
import trade_meta_model as meta
import full_limit_engine as engine
from test_full_limit_engine import NOW, analysis, order, snapshot

STRATEGY='native-mtf-causal-direction-control-v9'


def make_rows(n=220):
    rows=[]
    start=NOW-n*720_000
    for i in range(n):
        # Outcome is correlated with strict pre-entry evidence, and always
        # stored only AFTER an actual synthetic fill and resolved close.
        win=i%4 in (0,1)
        d=start+i*720_000
        f={'as_of_ts':d,'side':1 if i%2==0 else -1,
           'alignment':'aligned' if win else 'against',
           'htf_opposition':0 if win else 2,
           'shift3':win,'shift15':win,'impulse':win,
           'acceptance':win,'early_reversal_control':False,
           'distance_atr':0.1 if win else 3.,'range_position':.2 if win else .8,
           'cost_fraction':.08 if win else .4,'shock':False,
           'regime':'TREND' if win else 'RANGE','kind':'continuation'}
        rows.append({'id':str(i),'strategy':STRATEGY,'features':f,
                     'qualified':True,'decision_ts':d,
                     'filled_ts':d+180_000,'closed_ts':d+360_000,
                     'net_r':1.6 if win else -1.,
                     'conservative_ambiguity':False})
    return rows


class MetaForecast(unittest.TestCase):
    def test_no_history_does_not_invent_probability_or_veto(self):
        model=meta.train([],NOW,STRATEGY)
        self.assertFalse(model['validated'])
        self.assertIsNone(model['probability'])
        decision=meta.evaluate(model,{'side':1},net_rr=1.5)
        self.assertTrue(decision['accepted'])
        self.assertIsNone(decision['probability'])
        self.assertFalse(decision['entry_gate_enabled'])

    def test_temporal_holdout_improves_over_baseline_on_controlled_synthetic_cases(self):
        model=meta.train(make_rows(),NOW,STRATEGY)
        self.assertEqual(model['status'],'SHADOW_HOLDOUT_FILTER_PASSED')
        self.assertTrue(model['validated'])
        self.assertLess(model['holdout_brier'],model['baseline_brier'])
        self.assertLess(model['trained_through_ts'],model['validated_through_ts'])
        positive=meta.evaluate(model,make_rows()[0]['features'],net_rr=1.5)
        negative=meta.evaluate(model,make_rows()[2]['features'],net_rr=1.5)
        self.assertGreater(positive['probability'],negative['probability'])
        self.assertTrue(positive['accepted'])
        self.assertFalse(negative['accepted'])
        self.assertGreater(positive['expected_net_r'],negative['expected_net_r'])

    def test_future_labels_and_unverified_nonfills_can_never_train(self):
        rows=make_rows()
        base=meta.train(rows,NOW,STRATEGY)
        injected=[]
        for r in rows[:20]:
            future=copy.deepcopy(r)
            future.update(id='future-'+r['id'],decision_ts=NOW+10,filled_ts=NOW+100,
                          closed_ts=NOW+500,net_r=999)
            future['features']['as_of_ts']=NOW+10
            injected.append(future)
            nofill=copy.deepcopy(r);nofill.update(id='no-fill-'+r['id'],filled_ts=None)
            injected.append(nofill)
            late=copy.deepcopy(r);late['features']['as_of_ts']=r['decision_ts']+100
            late['id']='late-feature-'+r['id'];injected.append(late)
            ambiguous=copy.deepcopy(r);ambiguous['id']='ambiguous-'+r['id'];ambiguous['conservative_ambiguity']=True
            injected.append(ambiguous)
        mutated=meta.train(rows+injected,NOW,STRATEGY)
        self.assertEqual(mutated,base)

    def test_overlapping_shadow_trades_are_one_episode(self):
        rows=make_rows(5)
        bad=copy.deepcopy(rows[1]);bad.update(id='overlap',decision_ts=rows[0]['decision_ts']+10,
                                              filled_ts=rows[0]['filled_ts'],closed_ts=rows[0]['closed_ts'])
        bad['features']['as_of_ts']=bad['decision_ts']
        self.assertEqual(len(meta.episodes(rows+[bad],NOW,STRATEGY)),5)

    def test_bad_holdout_keeps_model_as_observer_not_as_gate(self):
        rows=make_rows()
        for r in rows[-55:]:
            r['net_r']=-r['net_r']
        result=meta.train(rows,NOW,STRATEGY)
        self.assertFalse(result['entry_gate_enabled'])
        self.assertFalse(result['validated'])
        self.assertIn('MONITOR_ONLY_',result['status'])

    def test_past_only_no_gain_from_mutating_open_candles(self):
        f=make_rows()[0]['features']
        original=meta.vector(f)
        mutated={**f,'future_high':1e10,'future_low':-1e10,'closed_ts':NOW+1000,
                 'actual_net_r':999}
        self.assertEqual(meta.vector(mutated),original)


class PortfolioAbstention(unittest.TestCase):
    @staticmethod
    def candidate(side,score,name):
        p=order(side)
        p.update(id=name,event_key=name,direction_score=score,score=score,
                 entry=100 if side==1 else 102,readiness_pct=100)
        return p

    def test_near_tie_means_no_trade(self):
        long=self.candidate(1,80,'long')
        short=self.candidate(-1,79,'short')
        side,paths,reason=policy.choose([long,short],101,.01,margin=8)
        self.assertEqual((side,paths,reason),(0,[],'DIRECTION_AMBIGUOUS_NO_TRADE'))

    def test_strong_direction_still_places_and_allows_two_limit_slots(self):
        longs=[self.candidate(1,95,'long'),self.candidate(1,83,'long2')]
        short=self.candidate(-1,30,'short')
        side,plans,reason=policy.choose(longs+[short],101,.01,margin=8)
        self.assertEqual(side,1)
        self.assertEqual(len(plans),2)
        self.assertEqual(reason,'DIRECTION_SELECTED')

    def test_validated_predictions_prioritize_expected_net_R_over_old_score(self):
        long=self.candidate(1,98,'expensive-long')
        short=self.candidate(-1,70,'efficient-short')
        long['meta_decision']={'validated':True,'expected_net_r':.25}
        short['meta_decision']={'validated':True,'expected_net_r':1.1}
        side,plans,why=policy.choose([long,short],101,.01,margin=8)
        self.assertEqual(side,-1)
        self.assertEqual(plans[0]['id'],'efficient-short')

    def test_validated_expected_return_near_tie_abstains(self):
        long=self.candidate(1,98,'L')
        short=self.candidate(-1,45,'S')
        long['meta_decision']={'validated':True,'expected_net_r':.80}
        short['meta_decision']={'validated':True,'expected_net_r':.75}
        self.assertEqual(policy.choose([long,short],101,.01,margin=8)[0],0)

    def test_current_pending_gets_cancel_request_on_new_conflict(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        p=self.candidate(1,60,'waiting')
        p.update(entry=99.,stop=98.,initial_stop=98.,risk=1.,tp=999.,
                 placed_ts=NOW-360_000,effective_ts=NOW-360_000,
                 expires_ts=NOW+3_600_000,last_bar=NOW-180_000)
        engine.park_pending(state,[p])
        others=[self.candidate(1,81,'candidate-long'),self.candidate(-1,80,'candidate-short')]
        with patch.object(engine,'analyze',return_value=analysis(others)), \
             patch.object(engine.policy,'choose',return_value=(0,[],'DIRECTION_AMBIGUOUS_NO_TRADE')):
            result=engine.run_cycle(state,snapshot(),cfg)
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'DIRECTION_AMBIGUOUS_CANCEL_PENDING')
        self.assertFalse(result['selected_plans'])
        self.assertTrue(any(row.get('cancel_requested_ts') for row in engine.pending_plans(state)) or
                        any(row.get('reason')=='DIRECTION_AMBIGUOUS_CANCEL_PENDING'
                            for row in state['orders']))


if __name__=='__main__':
    unittest.main()
