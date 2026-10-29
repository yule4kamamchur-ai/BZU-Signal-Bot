"""Regression tests for single-thesis monitoring and 20-episode cold-start.

These are controlled synthetic scenarios, NOT evidence of trading profitability.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import directional_policy as policy
import full_limit_engine as engine
import trade_meta_model as meta
from test_full_limit_engine import NOW, analysis, order, snapshot
from test_v12_12_meta import STRATEGY, make_rows


class SecondIndependentEntry(unittest.TestCase):
    def setUp(self):
        self.cfg = engine.Config()
        self.state = engine.new_state(self.cfg)
        first = order(1)
        first.update(id='active-A', event_key='sweep-A', zone_key='zone-A',
                     parent_keys=['supply-demand-A'], setup='SWEEP_RECLAIM',
                     setup_timeframe='15m', entry=100., risk=1., stop=99.,
                     initial_stop=99., tp=110., status='OPEN', remaining=1.,
                     filled_ts=NOW-180000, last_bar=NOW, expires_ts=NOW+3600000,
                     exit_deadline=NOW+3600000)
        self.state['active'] = first
        second = order(1)
        second.update(id='new-B', event_key='sweep-B', zone_key='zone-B',
                      parent_keys=['demand-B'], setup='SWEEP_RECLAIM',
                      setup_timeframe='15m', entry=97., risk=1., stop=96.,
                      initial_stop=96.,tp=110., direction_score=90., score=90.,
                      readiness_pct=100., expires_ts=NOW+3600000)
        self.second = second

    def cycle(self, proposals):
        with patch.object(engine, 'analyze', return_value=analysis(proposals)):
            return engine.run_cycle(self.state, snapshot(now=NOW,price=103.), self.cfg)

    def test_open_position_monitored_without_second_same_direction_entry(self):
        result=self.cycle([self.second])
        self.assertNotEqual(result['action'], 'PLACE_LIMIT')
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'ACTIVE_POSITION_MONITOR_ONLY')
        self.assertEqual([p['id'] for p in engine.active_plans(self.state)],['active-A'])
        self.assertEqual(engine.pending_plans(self.state),[])
        self.assertEqual(result['analysis']['position_reviews'][0]['order_id'],'active-A')

    def test_no_extra_limit_if_one_open_and_one_waiting(self):
        waiting=copy.deepcopy(self.second)
        waiting.update(id='waiting-B',event_key='event-B',zone_key='zone-B')
        engine.park_pending(self.state,[waiting])
        challenger=copy.deepcopy(self.second)
        challenger.update(id='third-C',event_key='event-C',zone_key='zone-C',entry=94.)
        result=self.cycle([challenger])
        self.assertEqual(result['selected_plans'],[])
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'ACTIVE_POSITION_MONITOR_ONLY')
        self.assertEqual(len(engine.pending_plans(self.state)),0)

    def test_legacy_one_open_and_two_pending_cancels_only_surplus(self):
        first=copy.deepcopy(self.second)
        first.update(id='waiting-B',event_key='waiting-B',zone_key='waiting-B',
                     score=91.,direction_score=91.,placed_ts=NOW-180000,
                     effective_ts=NOW-180000)
        surplus=copy.deepcopy(self.second)
        surplus.update(id='waiting-C',event_key='waiting-C',zone_key='waiting-C',
                       entry=94.,score=20.,direction_score=20.,
                       placed_ts=NOW-180000,effective_ts=NOW-180000)
        engine.park_pending(self.state,[first,surplus])
        result=self.cycle([])
        self.assertEqual(result['selected_plans'],[])
        self.assertEqual(len(engine.active_plans(self.state)),1)
        non_cancelled=[p for p in engine.pending_plans(self.state)
                       if not p.get('cancel_requested_ts')]
        self.assertLessEqual(len(non_cancelled),1)
        self.assertTrue(any(p.get('cancel_requested_ts') for p in engine.pending_plans(self.state))
                        or any(o['id']=='waiting-C' and o['status']=='CANCELLED'
                               for o in self.state['orders']))

    def test_no_third_limit_with_two_active_positions(self):
        second=copy.deepcopy(self.state['active'])
        second.update(id='active-B',event_key='event-B',zone_key='zone-B',entry=94.)
        self.state['second_slot']['active']=second
        result=self.cycle([self.second])
        self.assertEqual(result['selected_plans'],[])
        self.assertEqual(len(engine.active_plans(self.state)),2)
        self.assertEqual(engine.pending_plans(self.state),[])

    def test_opposing_candidate_never_hedges_active_long(self):
        opposing=copy.deepcopy(self.second)
        opposing.update(id='short',event_key='short',zone_key='short',side=-1,entry=108.,
                        stop=109.,initial_stop=109.,tp=103.,direction_score=130.)
        result=self.cycle([opposing])
        self.assertEqual(result['selected_plans'],[])
        self.assertEqual(engine.pending_plans(self.state),[])

    def test_existing_opposing_waiting_limit_is_cancelled_not_hedged(self):
        opposing=order(-1)
        opposing.update(id='legacy-short',event_key='legacy-short',zone_key='short-zone',
                        entry=108.,stop=109.,initial_stop=109.,risk=1.,tp=104.,
                        placed_ts=NOW-180000,effective_ts=NOW-180000,expires_ts=NOW+3600000)
        engine.park_pending(self.state,[opposing])
        # Keep the legacy opposite limit until the dedicated book interlock
        # sees it: don't let an unrelated HTF review clear the fixture first.
        with patch.object(engine, 'review_waiting_conditions', return_value=None):
            result=self.cycle([self.second])
        self.assertFalse(result['selected_plans'])
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'ACTIVE_POSITION_MONITOR_ONLY')
        self.assertTrue(any(p.get('cancel_requested_ts') for p in engine.pending_plans(self.state))
                        or any(o['id']=='legacy-short' and o['status']=='CANCELLED'
                               for o in self.state['orders']))

    def test_different_detector_same_zone_is_not_independent(self):
        duplicate=copy.deepcopy(self.second)
        duplicate.update(zone_key='zone-A',setup='RANGE_REJECTION')
        self.assertFalse(policy.compatible(duplicate,self.state['active'],self.cfg.tick))
        self.assertEqual(self.cycle([duplicate])['selected_plans'],[])

    def test_shared_parent_liquidity_event_is_not_independent(self):
        duplicate=copy.deepcopy(self.second)
        duplicate['parent_keys']=['supply-demand-A']
        self.assertFalse(policy.compatible(duplicate,self.state['active'],self.cfg.tick))
        self.assertEqual(self.cycle([duplicate])['selected_plans'],[])

    def test_entry_too_near_open_position_is_not_independent(self):
        duplicate=copy.deepcopy(self.second)
        duplicate['entry']=99.8
        self.assertFalse(policy.compatible(duplicate,self.state['active'],self.cfg.tick))
        self.assertEqual(self.cycle([duplicate])['selected_plans'],[])

    def test_distinct_same_setup_same_timeframe_still_not_opened(self):
        self.assertTrue(policy.compatible(self.second,self.state['active'],self.cfg.tick))
        self.assertEqual(self.cycle([self.second])['selected_plans'],[])

    def test_unknown_event_id_does_not_prove_independence(self):
        duplicate=copy.deepcopy(self.second)
        duplicate.pop('event_key')
        self.assertFalse(policy.compatible(duplicate,self.state['active'],self.cfg.tick))


class TwentyEpisodeMetaLearning(unittest.TestCase):
    def test_nineteen_not_enough_but_twenty_starts_training(self):
        self.assertEqual(meta.MIN_EPISODES,20)
        insufficient=meta.train(make_rows(19),NOW,STRATEGY)
        self.assertEqual(insufficient['required_independent_episodes'],20)
        self.assertFalse(insufficient['validated'])
        available=meta.train(make_rows(20),NOW,STRATEGY)
        self.assertEqual(available['independent_episodes'],20)
        self.assertGreaterEqual(available['training_episodes'],10)
        self.assertGreaterEqual(available['calibration_episodes'],3)
        self.assertGreaterEqual(available['validation_episodes'],5)
        self.assertFalse(available['entry_gate_enabled'])
        self.assertIsNone(meta.evaluate(available,make_rows(20)[0]['features'],net_rr=1.5)['probability'])

    def test_future_labels_do_not_fast_track_twenty_episode_learning(self):
        rows=make_rows(19)
        injected=copy.deepcopy(rows[0])
        injected['id']='future-win';injected['decision_ts']=NOW+10000
        injected['filled_ts']=NOW+11000;injected['closed_ts']=NOW+12000
        injected['features']['as_of_ts']=NOW+10000
        result=meta.train(rows+[injected],NOW,STRATEGY)
        self.assertEqual(result['independent_episodes'],19)
        self.assertFalse(result['entry_gate_enabled'])


if __name__=='__main__':
    unittest.main()
