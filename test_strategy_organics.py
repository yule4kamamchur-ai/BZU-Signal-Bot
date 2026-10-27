"""Regression for three executable hypotheses and the 15-minute deployment contract."""
from __future__ import annotations

import math
import unittest
from pathlib import Path
from types import SimpleNamespace

import entry_playbooks as pb
import strategy_organics as strat
from test_entry_playbooks import BASE, aligned_snapshot, candle, replace_like, sweep_snapshot


def sideways(side=1):
    snap = sweep_snapshot(1)
    m = []
    for i in range(148):
        v = 100 + .09 * math.sin(i * .4)
        m.append(SimpleNamespace(ts=BASE+i*pb.MICRO, open=v, high=v+.17,
                                 low=v-.17, close=v+.025, confirmed=True))
    # Already-published closed 15m range: roughly [99.55, 100.45].
    m[145] = SimpleNamespace(ts=BASE+145*pb.MICRO, open=99.9, high=100.02,
                             low=99.20, close=99.78, confirmed=True)
    m[146] = SimpleNamespace(ts=BASE+146*pb.MICRO, open=99.8, high=100.35,
                             low=99.72, close=100.25, confirmed=True)
    m[147] = SimpleNamespace(ts=BASE+147*pb.MICRO, open=100.20, high=100.47,
                             low=100.02, close=100.3, confirmed=True)
    h1 = []
    for i in range(35):
        v = 100+.07*math.sin(i*1.1)
        h1.append(SimpleNamespace(ts=BASE+(i-28)*pb.HOUR, open=v,high=v+.25,
                                  low=v-.25,close=v+.01,confirmed=True))
    h4 = []
    for i in range(26):
        v = 100+.05*math.sin(i*1.1)
        h4.append(SimpleNamespace(ts=BASE+(i-24)*pb.FOUR_HOURS, open=v, high=v+.3,
                                  low=v-.3,close=v+.01,confirmed=True))
    snap.candles.update({'3m':m, '1H':h1, '4H':h4})
    if side == -1:
        for tf in ('3m','15m','1H','4H'):
            snap.candles[tf] = [replace_like(x,open=200-x.open,close=200-x.close,
                                          high=200-x.low,low=200-x.high)
                                for x in snap.candles[tf]]
    snap.price = snap.candles['3m'][-1].close
    return snap


class RegimeAndEntryTests(unittest.TestCase):
    def test_sideways_rejection_has_distinct_raid_and_reclaim(self):
        for side in (1, -1):
            with self.subTest(side=side):
                snap=sideways(side)
                self.assertEqual(strat.classify(snap).state, 'RANGE')
                matched=[s for s in pb.scenarios(snap,.01)
                         if s.family=='MICRO_RANGE_REJECTION' and s.side==side]
                self.assertTrue(matched)
                signal=matched[0]
                self.assertGreater(signal.signal_ts,signal.event_ts)
                plan={'entry':signal.zone, 'side':side,
                      'stop':signal.invalidation-side*.3}
                verdict=pb.assess(plan,snap,.01)
                self.assertTrue(verdict['accepted'],verdict)
                self.assertEqual(verdict['strategy'],'RANGE_REJECTION')
                self.assertIsNone(verdict['estimated_win_rate'])

    def test_rejection_wick_without_separate_bar_is_not_an_entry(self):
        snap=sideways()
        c=snap.candles['3m'][146]
        snap.candles['3m'][146]=replace_like(c,open=c.close,close=c.open)
        self.assertFalse(any(z.family=='MICRO_RANGE_REJECTION'
                             for z in pb.scenarios(snap,.01)))

    def test_once_range_retested_no_chasing_a_past_price(self):
        snap=sideways()
        candidate=next(z for z in pb.scenarios(snap,.01) if z.family=='MICRO_RANGE_REJECTION')
        last=snap.candles['3m'][-1]
        snap.candles['3m'][-1]=replace_like(last,low=candidate.zone-.02)
        self.assertFalse(any(z.family=='MICRO_RANGE_REJECTION'
                             for z in pb.scenarios(snap,.01)))

    def test_FVG_without_prior_structural_BOS_is_not_trend_entry(self):
        s=aligned_snapshot(1,'fvg')
        self.assertTrue(any(z.family=='MICRO_HTF_FVG' for z in pb.scenarios(s,.01)))
        # Existing high is visible prior to displacement. A three-bar gap
        # still exists but price has NOT broken the previously known structure.
        old=s.candles['3m'][132]
        s.candles['3m'][132]=replace_like(old,high=105.)
        self.assertFalse(any(z.family=='MICRO_HTF_FVG' for z in pb.scenarios(s,.01)))

    def test_FVG_requires_fresh_htf_context_to_admit_order(self):
        s=aligned_snapshot(1,'fvg')
        signal=next(z for z in pb.scenarios(s,.01) if z.family=='MICRO_HTF_FVG')
        plan={'entry':signal.zone,'side':1,'stop':signal.invalidation-.3}
        # 4H context absent: scanner can observe a potential FVG but the
        # regime classifier cannot certify continuation.
        decision=pb.assess(plan,s,.01)
        self.assertFalse(decision['accepted'])
        self.assertEqual(strat.classify(s).state,'UNKNOWN')
        s.candles['4H'] = sideways().candles['4H']
        decision=pb.assess(plan,s,.01)
        self.assertTrue(decision['accepted'],decision)
        self.assertEqual(decision['strategy'],'TREND_CONTINUATION')

    def test_missing_and_future_4h_never_supply_trend(self):
        s=aligned_snapshot(1,'fvg')
        s.candles['4H']=sideways().candles['4H']
        good=strat.classify(s)
        self.assertEqual(good.state,'TREND_LONG')
        s.candles['4H']=[replace_like(x,confirmed=False) for x in s.candles['4H']]
        self.assertEqual(strat.classify(s).state,'UNKNOWN')
        # Even a closed future bar cannot become an as-of trend.
        s.candles['4H']=[replace_like(x,ts=s.now+pb.FOUR_HOURS,confirmed=True)
                         for x in sideways().candles['4H']]
        self.assertEqual(strat.classify(s).state,'UNKNOWN')

    def test_countertrend_reversal_does_not_require_trend_FVG(self):
        s=sweep_snapshot()
        x=next(z for z in pb.scenarios(s,.01) if z.family=='MICRO_SWEEP_MSS')
        out=pb.assess({'entry':x.zone,'side':1,'stop':x.invalidation-.5},s,.01)
        self.assertTrue(out['accepted'])
        self.assertEqual(out['strategy'],'LIQUIDITY_REVERSAL')

    def test_schedule_is_15_minutes_and_shadow_not_research_only(self):
        workflow=(Path(__file__).parent/'.github/workflows/main.yml').read_text()
        self.assertIn("cron: '*/15 * * * *'",workflow)
        self.assertIn("BZU_ENTRY_PLAYBOOK_MODE: 'STRICT'",workflow)
        self.assertNotIn("BZU_EDGE_MODE: 'RESEARCH_ONLY'",workflow)
        self.assertNotIn('continuous_worker.py',workflow)


if __name__ == '__main__':
    unittest.main()
