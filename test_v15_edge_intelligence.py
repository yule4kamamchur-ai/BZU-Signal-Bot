"""Causal contract tests for the unvalidated v15 research admission layer."""
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace as S
from pathlib import Path

import edge_intelligence as e
from edge_research import assess_history
import full_limit_engine as engine

NOW=1_800_000_000_000

def rows(side, step, n=18, close=100., size=.25):
    start=NOW-(n+1)*step
    return [S(ts=start+i*step,open=close+side*i*size-.06,
              high=close+side*i*size+.13,low=close+side*i*size-.13,
              close=close+side*i*size,confirmed=True) for i in range(n)]


def snapshot(h1=1, h4=1):
    return S(now=NOW,candles={'1H':rows(h1,e.HOUR_MS),
                              '4H':rows(h4,e.FOUR_HOUR_MS)})


def plan(name='PULLBACK_CONTINUATION',side=1):
    return {'side':side,'setup':name,'net_rr':1.6,'risk':1.,
            'ict_admission_reason':'CONFIRMED'}


def proof(**kw):
    return dict({'displacement':True, 'near_unmitigated_fvg':True,
                 'structure_shift':True,'break_acceptance':True},**kw)

class ResearchGate(unittest.TestCase):
    def test_nonfuture_preexisting_proof_allows_aligned_continuation(self):
        r=e.assess(plan(),snapshot(),proof())
        self.assertEqual(r['status'],'ALLOW')
        self.assertIsNone(r['estimated_win_rate'])

    def test_continuation_rejected_against_hour_trend(self):
        self.assertEqual(e.assess(plan(),snapshot(-1,-1),proof())['reason'],
                         'EDGE_HTF_AGAINST_CONTINUATION')

    def test_reversal_requires_liquidity_sequence_when_opposing_htf(self):
        p=plan('SWEEP_RECLAIM',-1)
        self.assertEqual(e.assess(p,snapshot(),proof())['status'],'REJECT')
        ok=e.assess(p,snapshot(),proof(sweep_reclaim=True,sweep_then_shift=True))
        self.assertEqual(ok['status'],'ALLOW')

    def test_session_context_alone_is_not_a_trade(self):
        p=plan('TIME_OF_DAY_ADAPTIVE')
        self.assertEqual(e.assess(p,snapshot(),proof(structure_shift=False))['reason'],
                         'EDGE_CONTEXT_WITHOUT_INDEPENDENT_ICT_SETUP')

    def test_unknown_contract_fail_closed(self):
        self.assertEqual(e.assess(plan('NOT_A_SETUP'),snapshot(),proof())['status'],'REJECT')

    def test_missing_higher_timeframe_fails_closed(self):
        s=snapshot();s.candles['4H']=[]
        self.assertEqual(e.assess(plan(),s,proof())['reason'],
                         'EDGE_INSUFFICIENT_OR_STALE_HTF')

    def test_unconfirmed_and_future_candles_cannot_flip_edge(self):
        s=snapshot();before=e.assess(plan(),s,proof())
        s.candles['1H'] += [S(ts=NOW,open=1.,close=.1,high=1.,low=.1,confirmed=False),
                            S(ts=NOW+e.HOUR_MS,open=1.,close=.1,high=1.,low=.1,confirmed=True)]
        self.assertEqual(before,e.assess(plan(),s,proof()))

    def test_insufficient_reward_and_unconfirmed_ict_veto(self):
        p=plan();p['net_rr']=.9
        self.assertEqual(e.assess(p,snapshot(),proof())['reason'],'EDGE_INSUFFICIENT_NET_REWARD')
        p['net_rr']=1.6;p['ict_admission_reason']='BLOCKED'
        self.assertEqual(e.assess(p,snapshot(),proof())['reason'],'EDGE_ICT_SEQUENCE_INCOMPLETE')

    def test_forensic_archive_requires_first_observation(self):
        t={'id':'a','setup':'PULLBACK_CONTINUATION','placed_ts':NOW,'status':'CLOSED','entry':100,'placement_price':100}
        archive={'candles':{},'first_seen':{}}
        result=assess_history([t],archive)
        self.assertFalse(result['trades'][0]['eligible_for_counterfactual'])
        self.assertEqual(result['eligible_counterfactual_trades'],0)

    def test_workflow_blocks_new_paper_entries_pending_validation(self):
        root=Path(__file__).resolve().parent
        live=(root/'.github/workflows/main.yml').read_text()
        self.assertIn("BZU_EDGE_MODE: 'RESEARCH_ONLY'",live)
        self.assertIn('edge_intelligence.py',live)
        self.assertTrue((root/'.github/workflows/bzu-quality.yml').exists())

    def test_research_only_interlock_prevents_placement_even_with_ready_mock(self):
        from unittest.mock import patch
        from test_full_limit_engine import NOW as TEST_NOW, snapshot as snap, analysis
        cfg=engine.Config()
        state=engine.new_state(cfg)
        state['last_run_ts']=TEST_NOW-180_000
        candidate={'id':'candidate-new','side':1,'setup':'PULLBACK_CONTINUATION',
                   'event_key':'new-event','entry':101.,'stop':100.,'tp':103.,
                   'risk':1.,'score':95.,'direction_score':95.,'readiness_pct':100.,
                   'quality':{'accepted':True,'features':{}},'net_rr':1.9,
                   'ict_admission_reason':'CONFIRMED','placed_ts':TEST_NOW}
        with patch.dict('os.environ',{'BZU_EDGE_MODE':'RESEARCH_ONLY'}), \
             patch.object(engine,'health',return_value=[]), \
             patch.object(engine,'execution_health',return_value=[]), \
             patch.object(engine,'advance_lifecycle'), \
             patch.object(engine,'amend_at_report'), \
             patch.object(engine.quality,'advance'), \
             patch.object(engine,'analyze',return_value=analysis([candidate])):
            result=engine.run_cycle(state,snap(now=TEST_NOW,price=103.),cfg)
        self.assertNotEqual(result['action'],'PLACE_LIMIT')
        self.assertEqual(len(engine.pending_plans(state)),0)

    def test_all_twenty_four_detector_contracts_have_strategy_family(self):
        import ict_methodology
        self.assertEqual(len(ict_methodology.CONTRACTS),24)
        for n,c in ict_methodology.CONTRACTS.items():
            self.assertTrue(c.family and c.status,n)


if __name__ == '__main__':
    unittest.main()
