"""v12.11: causal ICT evidence and one-direction portfolio invariant.

All candles and order fills are synthetic tests, not historical backtest PnL.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import bot_oneshot as core
import directional_policy as dp
import full_limit_engine as e
import setup_paths
from test_full_limit_engine import NOW, analysis, candle, order, snapshot
from test_v12_8_paths import witness


class DirectionElection(unittest.TestCase):
    def proposal(self, side, number, score):
        p=order(side)
        p.update(id=str(number),event_key=str(number),score=score,
                 readiness_pct=100.,direction_score=score,
                 entry=100. if side==1 else 102.)
        return p

    def test_narrow_opposite_contest_abstains_without_an_edge(self):
        p=self.proposal(1,'long',81)
        q=self.proposal(-1,'short',80)
        side,ranked,why=dp.choose([p,q],101.,.01,margin=8.)
        self.assertEqual(side,0)
        self.assertEqual(ranked,[])
        self.assertEqual(why,'DIRECTION_AMBIGUOUS_NO_TRADE')

    def test_active_position_locks_out_opposite_candidate(self):
        cfg=e.Config();state=e.new_state(cfg)
        first=self.proposal(1,'long',60)
        first.update(status='OPEN',last_bar=NOW,filled_ts=NOW-180000,
                     exit_deadline=NOW+3600000,remaining=1.)
        state['active']=first
        rival=self.proposal(-1,'short',99)
        with patch.object(e,'analyze',return_value=analysis([rival])):
            result=e.run_cycle(state,snapshot(),cfg)
        self.assertFalse(result['selected_plans'])
        self.assertEqual(state['active']['id'],'long')
        self.assertFalse(e.pending_plans(state))
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'ACTIVE_POSITION_MONITOR_ONLY')

    def test_two_pending_opposite_limits_cannot_both_fill_same_bar(self):
        cfg=e.Config();state=e.new_state(cfg)
        p,q=self.proposal(1,'long',80),self.proposal(-1,'short',79)
        p.update(entry=100.,stop=98.5,initial_stop=98.5,tp=105.,risk=1.5)
        q.update(entry=101.,stop=103.,initial_stop=103.,tp=96.,risk=2.)
        for x in (p,q):
            x.update(placed_ts=NOW,effective_ts=NOW,expires_ts=NOW+3600000,
                     queue_execution_policy='PASSIVE_ARM_V2',last_bar=NOW-180000)
        e.park_pending(state,[p,q])
        snap=snapshot(now=NOW+180000,price=100.5)
        snap.candles['3m']=[candle(NOW,100.5,101.5,99.5,100.5)]
        e.advance_lifecycle(state,snap,cfg)
        self.assertLessEqual(len({p['side'] for p in e.active_plans(state)}),1)
        self.assertEqual(len([o for o in state['orders'] if o['status']=='FILLED']),1)
        self.assertEqual(len(e.active_plans(state)),1)

    def test_new_book_never_places_more_than_two_pending(self):
        cfg=e.Config();state=e.new_state(cfg)
        proposals=[]
        for i in range(8):
            p=self.proposal(1,i,96-i)
            p.update(entry=100.-i,stop=99.-i,initial_stop=99.-i,
                     tp=103.-i,invalidation=99.-i,risk=1.)
            proposals.append(p)
        with patch.object(e,'analyze',return_value=analysis(proposals)):
            result=e.run_cycle(state,snapshot(),cfg)
        self.assertLessEqual(len(result['selected_plans']),2)
        self.assertLessEqual(len(e.pending_plans(state)),2)
        self.assertTrue(all(p['side']==1 for p in e.pending_plans(state)))

    def test_legacy_unlimited_queue_is_reduced_to_two_pending(self):
        cfg=e.Config();state=e.new_state(cfg)
        backlog=[]
        for i in range(6):
            p=self.proposal(1,'legacy'+str(i),90-i)
            p.update(entry=99.-i,stop=98.-i,initial_stop=98.-i,
                     tp=200.,invalidation=80.,risk=1.,
                     placed_ts=NOW-180000,effective_ts=NOW-180000,
                     expires_ts=NOW+3600000,last_bar=NOW-180000)
            backlog.append(p)
        e.park_pending(state,backlog)
        with patch.object(e,'analyze',return_value=analysis([])):
            result=e.run_cycle(state,snapshot(),cfg)
        remaining=[p for p in e.pending_plans(state) if not p.get('cancel_requested_ts')]
        self.assertLessEqual(len(remaining),2)
        self.assertFalse(result['selected_plans'])
        self.assertTrue(any(o.get('reason')=='LEGACY_PENDING_EXCESS_CAP_2' for o in state['orders'])
                        or any(p.get('cancel_reason')=='LEGACY_PENDING_EXCESS_CAP_2'
                               for p in e.pending_plans(state)))

    def test_legacy_hedged_active_book_does_not_add_another_position(self):
        cfg=e.Config();state=e.new_state(cfg)
        for side,slot in ((1,1),(-1,2)):
            p=self.proposal(side,slot,80)
            p.update(status='OPEN',remaining=1.,last_bar=NOW,
                     exit_deadline=NOW+3600000)
            if slot==1:state['active']=p
            else:state['second_slot']['active']=p
        with patch.object(e,'analyze',return_value=analysis([self.proposal(1,3,99)])):
            result=e.run_cycle(state,snapshot(),cfg)
        self.assertFalse(result['selected_plans'])
        self.assertEqual(result['analysis']['direction_policy']['reason'],
                         'LEGACY_OPPOSING_OPEN_POSITIONS_MANAGE_ONLY')
        self.assertEqual(len(e.active_plans(state)),2)  # Never auto-close existing fills.


class CausalIctConfluence(unittest.TestCase):
    def setUp(self):
        self.step=e.TF['15m'];self.tick=.01
        self.base=[candle(NOW-(40-i)*self.step,100.,100.5,99.,100.)
                   for i in range(40)]
        self.p={'side':1,'setup':'SWEEP_RECLAIM','entry':99.6,
                'formation_timeframe':'15m','setup_timeframe':'15m'}

    def read(self, rows):
        s=snapshot(now=NOW,price=100.1)
        s.candles['15m']=copy.deepcopy(rows)
        return dp.evidence(self.p,s,self.tick)

    def test_liquidity_wick_and_close_reclaim_are_required(self):
        rows=copy.deepcopy(self.base)
        rows[-1]=candle(rows[-1].ts,99.7,100.2,98.4,99.8)
        good=self.read(rows)
        self.assertTrue(good['sweep_reclaim'])
        self.assertEqual(good['latest_sweep']['known_ts'],NOW)
        rows[-1]=candle(rows[-1].ts,99.7,100.2,98.4,98.8)
        self.assertFalse(self.read(rows)['sweep_reclaim'])

    def test_unclosed_and_future_candles_cannot_supply_confirmation(self):
        prior=self.read(self.base)
        rows=self.base+[candle(NOW,99.,101.,80.,100.5),
                        candle(NOW-self.step,99.,101.,80.,100.5,False)]
        self.assertEqual(self.read(rows),prior)

    def test_three_candle_wick_gap_near_entry_is_detected(self):
        rows=copy.deepcopy(self.base)
        rows[-2]=candle(rows[-2].ts,100.1,103.5,99.9,103.3)
        rows[-1]=candle(rows[-1].ts,102.5,103.3,101.5,102.9)
        self.p['entry']=100.8
        self.assertTrue(self.read(rows)['near_unmitigated_fvg'])
        self.assertFalse(self.read(self.base)['near_unmitigated_fvg'])

    def test_plain_equal_lows_are_not_a_liquidity_sweep_entry(self):
        p={**self.p,'setup':'LIQUIDITY_LADDER'}
        proof=self.read(self.base)
        allowed,reason=dp.admit(p,proof)
        self.assertFalse(allowed)
        self.assertEqual(reason,'ICT_LIQUIDITY_SWEEP_NOT_CONFIRMED')

    def test_compression_maps_true_break_boundary_and_requires_break(self):
        ctx=witness('RANGE_COMPRESSION_BREAKOUT')
        base=ctx['zones']['consolidation']
        with e.market_clock(core,NOW):
            paths=setup_paths.candidates(core.detect_range_compression_breakout,
                                         'RANGE_COMPRESSION_BREAKOUT',ctx,core)
        found={(a.side,round(a.level,8)):a for a in paths}
        self.assertIn(('LONG',round(base['high'],8)),found)
        self.assertIn(('SHORT',round(base['low'],8)),found)
        self.assertTrue(all(a.evidence['activation_phase']=='WAIT_BREAK' for a in paths))
        self.assertEqual(len(paths),2)


if __name__=='__main__':
    unittest.main()
