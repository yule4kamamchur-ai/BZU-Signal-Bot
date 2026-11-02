"""Regression coverage for the 24 native detectors and the stale-anchor tombstone."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import entry_playbooks as play


class StrictContracts(unittest.TestCase):
    def test_complete_24_by_3_timeframes_and_both_sides(self):
        self.assertTrue(play.validate_contracts(core.DETECTED_SETUP_TYPES))
        self.assertEqual(len(play.ICT_PLAYBOOK_CONTRACTS),24)
        for setup in core.DETECTED_SETUP_TYPES:
            for timeframe in ('15m','1H','4H'):
                for side in (1,-1):
                    with self.subTest(setup=setup,timeframe=timeframe,side=side):
                        for proof in play.ICT_PLAYBOOK_CONTRACTS[setup]:
                            expected=not setup.endswith('_SHORT') or side==-1
                            self.assertEqual(play.contract_matches(setup,proof,side=side),expected)
                        self.assertFalse(play.contract_matches(setup,'IMAGINARY_PLAYBOOK',side=side))
        self.assertFalse(play.contract_matches('IMAGINARY_SETUP','MICRO_BOS_FVG',side=-1))

    def test_contract_rejects_mismatched_but_price_aligned_signal(self):
        event=play.Setup('MICRO_BOS_FVG',-1,100.0,102.0,1000,820,1.0,'closed signal')
        snap=SimpleNamespace(now=1000,price=99.0,candles={})
        plan={'side':-1,'entry':100.0,'stop':103.0,'setup_type':'FAILED_BREAKOUT_SHORT'}
        # Price, stop and time all agree, but a failed expansion needs a
        # reversal/rejection event, not a trend BOS/FVG.
        with patch.object(play.organic,'classify',return_value=SimpleNamespace()),\
             patch.object(play.organic,'qualify',return_value={'accepted':True,'strategy':'TEST'}):
            self.assertFalse(play.assess(plan,snap,.01,setup_candidates=[event])['accepted'])
            plan['setup_type']='PULLBACK_CONTINUATION'
            self.assertTrue(play.assess(plan,snap,.01,setup_candidates=[event])['accepted'])
            plan['setup_type']='UNRECOGNIZED_SETUP'
            self.assertEqual(play.assess(plan,snap,.01,setup_candidates=[event])['reason'],
                             'PLAYBOOK_SETUP_CONTRACT_UNKNOWN')

    def test_tombstone_will_not_be_revived_by_unchanged_detector(self):
        cfg=engine.Config(tick=.01,core=core)
        snapshot=SimpleNamespace(now=1_900_000_000_000)
        a=SimpleNamespace(setup_type='PULLBACK_CONTINUATION',side='SHORT',level=110.0)
        state={'anchor_memory':{'old':{'state':'ARMED','created_ts':snapshot.now-60_000}}}
        engine.retire_distant_anchor(state,a,'old',snapshot,cfg)
        self.assertEqual(state['anchor_memory']['old']['state'],'REJECTED')
        self.assertEqual(state['anchor_memory']['old']['reject_reason'],
                         'LIMIT_UNREACHABLE_REQUIRES_NEW_STRUCTURE')
        self.assertEqual(state['stale_refresh'][-1]['next_action'],
                         'FRESH_CONFIRMED_BOS_FVG_OR_RENEWAL')
        engine.retire_distant_anchor(state,a,'old',snapshot,cfg)
        self.assertEqual(len(state['stale_refresh']),1)

    def test_stop_cap_wider_but_bounded(self):
        self.assertGreaterEqual(core.LIMIT_MAX_STOP_ATR,4)
        self.assertLessEqual(core.LIMIT_MAX_STOP_ATR,6)
        self.assertAlmostEqual(core.min_stop_distance(100),1.0)
        self.assertAlmostEqual(engine.Config().min_target_pct,.020)

    def test_dedicated_worker_three_minute_buckets(self):
        import continuous_worker as worker
        base=9_000_000  # 15m boundary
        calls=[]
        checkpoint={}
        for minute in (0,3,6,9,12,15):
            now=base+minute*60_000+worker.SETTLE_MS
            checkpoint,ran=worker.step(now,checkpoint,lambda notify:calls.append(notify) or 0)
            self.assertTrue(ran)
            checkpoint,ran=worker.step(now+1000,checkpoint,lambda notify:99)
            self.assertFalse(ran)
        self.assertEqual(calls,[True,False,False,False,False,True])


if __name__=='__main__':
    unittest.main()
