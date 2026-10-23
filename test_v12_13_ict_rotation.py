"""v12.13 regression checks: instant causal rotation and per-setup ICT contracts.

Every fake market path is deterministic; none is a measured profit result.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import full_limit_engine as e
import ict_methodology as ict
from test_full_limit_engine import NOW, analysis, candle, order, snapshot


class ICTContracts(unittest.TestCase):
    def setUp(self):
        self.empty={'as_of_ts':NOW,'sweep_reclaim':False,'structure_shift':False,
                    'displacement':False,'near_unmitigated_fvg':False,
                    'level_rejection':False,'break_acceptance':False,
                    'reference_reclaim':False,'sweep_then_shift':False}

    def test_each_of_24_names_has_explicit_contract_and_no_generic_auto_pass(self):
        self.assertEqual(len(ict.CONTRACTS),24)
        for name in ict.CONTRACTS:
            with self.subTest(setup=name):
                plan={'setup':name,'detector_confirmed':True,
                      'anchor_known_ts':NOW,'anchor_proof':{}}
                admitted,reason,diag=ict.validate(plan,self.empty)
                self.assertFalse(admitted,(name,diag))
                self.assertNotEqual(reason,'CONFIRMED')
                self.assertEqual(diag['family'],ict.CONTRACTS[name].family)

    def test_context_only_names_need_an_actual_price_trigger(self):
        for name in ('SESSION_MEAN_RECLAIM','DAILY_WEEKLY_OPEN_RECLAIM',
                     'LIQUIDITY_LADDER','TIME_OF_DAY_ADAPTIVE'):
            with self.subTest(name=name):
                ok,_,_=ict.validate({'setup':name,'detector_confirmed':True},self.empty)
                self.assertFalse(ok)

    def test_liquidity_mss_must_follow_sweep_not_precede_it(self):
        base={'setup':'MSS_REVERSAL_SHORT','detector_confirmed':True}
        proof={**self.empty,'sweep_reclaim':True,'structure_shift':True}
        ok,_,_=ict.validate(base,proof)
        self.assertFalse(ok)
        ok,_,_=ict.validate(base,{**proof,'sweep_then_shift':True})
        self.assertTrue(ok)

    def test_arbitrary_anchor_metadata_cannot_claim_completed_sweep(self):
        for name in ('SWEEP_RECLAIM','LIQUIDITY_SWEEP_REVERSAL_SHORT'):
            with self.subTest(name=name):
                plan={'setup':name,'detector_confirmed':True,
                      'anchor_proof':{'session':'LONDON'},'anchor_known_ts':NOW}
                self.assertFalse(ict.validate(plan,self.empty)[0])

    def test_mss_requires_shift_even_with_displacement(self):
        p={'setup':'MSS_REVERSAL_SHORT','detector_confirmed':True}
        proof={**self.empty,'sweep_reclaim':True,'displacement':True,
               'latest_sweep':{'known_ts':NOW-900000},
               'latest_displacement_known_ts':NOW}
        self.assertFalse(ict.validate(p,proof)[0])
        self.assertTrue(ict.validate(p,{**proof,'structure_shift':True,
                                        'sweep_then_shift':True})[0])

    def test_direction_flip_cannot_be_called_mss_from_plain_acceptance(self):
        p={'setup':'DIRECTION_FLIP_15M','detector_confirmed':True}
        self.assertFalse(ict.validate(p,{**self.empty,'break_acceptance':True})[0])
        self.assertTrue(ict.validate(p,{**self.empty,'structure_shift':True})[0])

    def test_future_native_anchor_does_not_authorize_break(self):
        base={'setup':'RANGE_COMPRESSION_BREAKOUT','detector_confirmed':True,
              'anchor_proof':{'activation_phase':'LIMIT_RETEST','break_known_ts':NOW+180000},
              'anchor_known_ts':NOW}
        self.assertFalse(ict.validate(base,self.empty)[0])
        self.assertFalse(ict.validate({**base,'anchor_known_ts':NOW+180000},self.empty)[0])
        right={'setup':'RANGE_COMPRESSION_BREAKOUT','detector_confirmed':True,
               'anchor_proof':{'activation_phase':'LIMIT_RETEST','break_known_ts':NOW-180000},
               'anchor_known_ts':NOW-180000}
        self.assertTrue(ict.validate(right,self.empty)[0])


class FastRotation(unittest.TestCase):
    def setUp(self):
        self.cfg=e.Config()
        self.book=e.new_state(self.cfg)
        self.incumbents=[]
        for i,(entry,score) in enumerate(((93.,75.),(98.,52.))):
            p=order(1,placed=NOW-3*e.TF['3m'],expiry=NOW+3_600_000)
            p.update(id='old'+str(i),event_key='old'+str(i),entry=entry,
                     stop=entry-1,initial_stop=entry-1,risk=1.,tp=105.,
                     invalidation=entry-1,score=score,direction_score=score,
                     last_bar=NOW-e.TF['3m'],effective_ts=NOW-3*e.TF['3m'],
                     queue_execution_policy='PASSIVE_ARM_V2')
            self.incumbents.append(p)
        e.park_pending(self.book,self.incumbents)
        self.new=order(1,placed=NOW,expiry=NOW+3_600_000)
        self.new.update(id='candidate',event_key='candidate',entry=97.,stop=96.,
                        initial_stop=96.,tp=101.,invalidation=96.,risk=1.,
                        score=99.,direction_score=99.,setup='SWEEP_RECLAIM',
                        setup_timeframe='1H',readiness_pct=100.,
                        quality={'accepted':True},ict_admission_reason='CONFIRMED')

    def tick(self,now=NOW,price=100.,current_bar=None):
        s=snapshot(now=now,price=price)
        if current_bar:
            s.candles['3m']=[current_bar]
        return s

    def test_safe_boundary_rotates_in_same_cycle_without_third_order(self):
        # No prior bars touch the old limit levels. Decision, cancel and
        # replacement all occur after the final closed 3m bar at NOW.
        with patch.object(e,'analyze',return_value=analysis([copy.deepcopy(self.new)])):
            output=e.run_cycle(self.book,self.tick(),self.cfg)
        self.assertEqual(output['analysis']['portfolio_arbitration']['decision'],
                         'FAST_ROTATE_SAME_CYCLE')
        self.assertEqual({p['id'] for p in e.pending_plans(self.book)},
                         {'old0','candidate'})
        self.assertLessEqual(len(e.pending_plans(self.book)),2)
        self.assertFalse(e.active_plans(self.book))
        self.assertTrue(any(o['id']=='old1' and o['status']=='CANCELLED'
                            for o in self.book['orders']))
        self.assertTrue(any(x['kind']=='PORTFOLIO_ROTATION_COMMITTED'
                            for x in self.book['events']))
        self.assertNotIn('portfolio_rotation_intent',self.book)

    def test_between_boundaries_awaits_reconciliation_without_third_order(self):
        current=NOW+60_000
        self.new['expires_ts']=current+3_600_000
        with patch.object(e,'analyze',return_value=analysis([copy.deepcopy(self.new)])):
            result=e.run_cycle(self.book,self.tick(now=current),self.cfg)
        self.assertEqual(result['analysis']['portfolio_arbitration']['decision'],
                         'WAIT_CANCEL_RECONCILIATION')
        self.assertEqual(len(e.pending_plans(self.book)),2)
        self.assertEqual(len(result['selected_plans']),0)
        self.assertEqual(self.book['portfolio_rotation_intent']['status'],
                         'PREPARED_WAIT_RECONCILIATION')

    def test_cancellation_finishes_on_first_confirmed_boundary(self):
        current=NOW+60_000
        self.new['expires_ts']=current+3_600_000
        with patch.object(e,'analyze',return_value=analysis([copy.deepcopy(self.new)])):
            e.run_cycle(self.book,self.tick(now=current),self.cfg)
        # Scan at the FIRST full 3m boundary. No fill, cancellation safe.
        boundary=NOW+e.TF['3m']
        closed=candle(NOW,100.,100.5,99.7,100.)
        next_plan=copy.deepcopy(self.new)
        next_plan['placed_ts']=boundary
        with patch.object(e,'analyze',return_value=analysis([next_plan])):
            result=e.run_cycle(self.book,self.tick(now=boundary,current_bar=closed),self.cfg)
        self.assertEqual({p['id'] for p in e.pending_plans(self.book)},
                         {'old0','candidate'})
        self.assertLessEqual(len(e.pending_plans(self.book)),2)
        self.assertTrue(any(o.get('id')=='old1' and o.get('resolved_ts')==boundary
                            for o in self.book['orders']))
        self.assertTrue(result['selected_plans'])

    def test_fill_before_cancel_never_allows_replacement(self):
        current=NOW+60_000
        self.new['expires_ts']=current+3_600_000
        with patch.object(e,'analyze',return_value=analysis([copy.deepcopy(self.new)])):
            e.run_cycle(self.book,self.tick(now=current),self.cfg)
        boundary=NOW+e.TF['3m']
        # The old limit was reached during the unsettled interval.
        closed=candle(NOW,100.,101.,97.5,99.)
        with patch.object(e,'analyze',return_value=analysis([copy.deepcopy(self.new)])):
            result=e.run_cycle(self.book,self.tick(now=boundary,current_bar=closed),self.cfg)
        self.assertFalse(result['selected_plans'])
        self.assertTrue(e.active_plans(self.book) or any(v.get('reconciliation')
                          for _,v in e.slot_views(self.book)))
        self.assertFalse(any(p['id']=='candidate' for p in e.pending_plans(self.book)))


if __name__=='__main__':
    unittest.main()
