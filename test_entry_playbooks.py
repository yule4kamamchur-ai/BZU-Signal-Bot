"""Focused tests for the production 15-minute entry strategy, not old releases."""
import math
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import entry_playbooks as p

STEP=p.STEP
BASE=1_800_000_000_000


def candle(idx, o, h, l, c, confirmed=True):
    return SimpleNamespace(ts=BASE+idx*STEP, open=o, high=h, low=l, close=c, confirmed=confirmed)


def sweep_snapshot(side=1):
    # 15m references exist *before* a 3m raid/MSS. No future HTF candles.
    htf = [candle(i,100,100.45,99.55,100.01) for i in range(29)]
    micro = []
    for i in range(148):
        v=100 + .12*math.sin(i*.6)
        micro.append(SimpleNamespace(ts=BASE+i*p.MICRO,open=v,high=v+.2,low=v-.2,close=v+.025,confirmed=True))
    micro[142] = SimpleNamespace(ts=BASE+142*p.MICRO,open=100.5,high=101.5,low=100.3,close=100.65,confirmed=True)
    micro[145] = SimpleNamespace(ts=BASE+145*p.MICRO,open=99.1,high=100.1,low=97.7,close=99.9,confirmed=True)
    micro[146] = SimpleNamespace(ts=BASE+146*p.MICRO,open=99.9,high=103.1,low=99.8,close=102.7,confirmed=True)
    micro[147] = SimpleNamespace(ts=BASE+147*p.MICRO,open=102.8,high=103.,low=102.1,close=102.85,confirmed=True)
    if side == -1:
        micro = [replace_like(r,open=200-r.open, high=200-r.low,
                              low=200-r.high,close=200-r.close) for r in micro]
        htf = [replace_like(r,open=200-r.open, high=200-r.low,
                            low=200-r.high,close=200-r.close) for r in htf]
    return SimpleNamespace(now=micro[-1].ts+p.MICRO,
                           candles={'3m':micro, '15m':htf, '1H':[], '4H':[]},
                           price=micro[-1].close)


def replace_like(o,**kw):
    return SimpleNamespace(**{**vars(o),**kw})


def aligned_snapshot(side=1, kind='fvg'):
    s = sweep_snapshot(side=1)
    h=[]
    for i in range(24):
        v=95+.4*i
        h.append(SimpleNamespace(ts=BASE+(i-17)*p.HOUR,open=v,high=v+.5,
                                 low=v-.35,close=v+.3,confirmed=True))
    s.candles['1H']=h
    m=s.candles['3m']
    # Clear previous reversal event; use an independent last-two/last-three-bar example.
    for j in (142,145,146,147):
        v=100+.02*math.sin(j)
        m[j]=SimpleNamespace(ts=BASE+j*p.MICRO,open=v,high=v+.18,
                             low=v-.18,close=v+.025,confirmed=True)
    if kind == 'fvg':
        m[145]=replace_like(m[145],open=100.1,high=100.35,low=99.95,close=100.2)
        m[146]=replace_like(m[146],open=100.4,high=102.7,low=100.4,close=102.5)
        m[147]=replace_like(m[147],open=102.6,high=103.,low=101.8,close=102.75)
    else:
        m[146]=replace_like(m[146],open=100.3,high=101.4,low=100.2,close=101.2)
        m[147]=replace_like(m[147],open=101.2,high=101.5,low=100.9,close=101.3)
    if side==-1:
        s.candles['1H']=[replace_like(r,open=200-r.open,high=200-r.low,low=200-r.high,close=200-r.close) for r in h]
        s.candles['3m']=[replace_like(r,open=200-r.open,high=200-r.low,low=200-r.high,close=200-r.close) for r in m]
        s.candles['15m']=[replace_like(r,open=200-r.open,high=200-r.low,low=200-r.high,close=200-r.close) for r in s.candles['15m']]
    s.price=s.candles['3m'][-1].close
    return s


class LivePlaybookTests(unittest.TestCase):
    def test_sweep_mss_requires_distinct_closed_break_and_matches_both_sides(self):
        for side in (1,-1):
            with self.subTest(side=side):
                snap=sweep_snapshot(side)
                found=[x for x in p.scenarios(snap,.01) if x.family=='MICRO_SWEEP_MSS' and x.side==side]
                self.assertTrue(found)
                x=found[0]
                plan={'side':side,'entry':x.zone,'stop':x.invalidation-side*.5}
                decision=p.assess(plan,snap,.01)
                self.assertTrue(decision['accepted'],decision)
                self.assertEqual(decision['playbook'],'MICRO_SWEEP_MSS')
                self.assertGreater(x.signal_ts,x.event_ts)

    def test_unclosed_mss_cannot_arm_limit(self):
        s=sweep_snapshot()
        last=s.candles['3m'][-2]
        s.candles['3m'][-2]=replace_like(last,confirmed=False)
        self.assertFalse(any(x.family=='MICRO_SWEEP_MSS' for x in p.scenarios(s,.01)))

    def test_future_bars_cannot_arm_limit(self):
        s=sweep_snapshot()
        s.now=s.candles['3m'][-3].ts+p.MICRO
        self.assertFalse(any(x.family=='MICRO_SWEEP_MSS' for x in p.scenarios(s,.01)))

    def test_nonmatching_entry_is_not_promoted(self):
        s=sweep_snapshot()
        x=next(z for z in p.scenarios(s,.01) if z.family=='MICRO_SWEEP_MSS' and z.side==1)
        wrong={'side':1,'entry':x.zone+50.,'stop':x.invalidation-1.}
        self.assertFalse(p.assess(wrong,s,.01)['accepted'])

    def test_wrong_or_unsafe_stop_is_not_promoted(self):
        s=sweep_snapshot()
        x=next(z for z in p.scenarios(s,.01) if z.family=='MICRO_SWEEP_MSS' and z.side==1)
        self.assertFalse(p.assess({'side':1,'entry':x.zone,'stop':x.invalidation+2},s,.01)['accepted'])

    def test_gaps_and_stale_market_history_are_not_entries(self):
        s=sweep_snapshot()
        s.now+=5*STEP
        self.assertEqual(p.scenarios(s,.01),[])


    def test_closed_3m_confirmation_arrives_before_next_15m_close(self):
        s=sweep_snapshot()
        # 3m MSS at minute 438-441, next 15m close is minute 450.
        self.assertLess(s.now,BASE+30*STEP)
        self.assertTrue(any(x.family=='MICRO_SWEEP_MSS' for x in p.scenarios(s,.01)))

    def test_price_already_retested_zone_is_not_late_limit(self):
        s=sweep_snapshot()
        signal=next(x for x in p.scenarios(s,.01) if x.family=='MICRO_SWEEP_MSS')
        last=s.candles['3m'][-1]
        s.candles['3m'][-1]=replace_like(last,low=signal.zone-.02)
        self.assertFalse(any(x.family=='MICRO_SWEEP_MSS' for x in p.scenarios(s,.01)))

    def test_missing_micro_bar_fails_safe(self):
        s=sweep_snapshot()
        s.candles['3m'].pop(-7)
        self.assertFalse(any(x.family=='MICRO_SWEEP_MSS' for x in p.scenarios(s,.01)))

    def test_15m_pattern_alone_does_not_resurrect_already_retested_limit(self):
        s=sweep_snapshot()
        s.candles['3m']=[]
        self.assertEqual(p.scenarios(s,.01),[])


    def test_early_3m_fvg_both_sides_in_htf_trend(self):
        for side in (1,-1):
            with self.subTest(side=side):
                s=aligned_snapshot(side,'fvg')
                self.assertTrue(any(x.side==side and x.family=='MICRO_HTF_FVG'
                                    for x in p.scenarios(s,.01)))

    def test_early_3m_break_retest_both_sides(self):
        for side in (1,-1):
            with self.subTest(side=side):
                s=aligned_snapshot(side,'break')
                self.assertTrue(any(x.side==side and x.family=='MICRO_ACCEPTED_BREAK'
                                    for x in p.scenarios(s,.01)))

    def test_micro_scenario_does_not_use_unclosed_hourly_trend(self):
        s=aligned_snapshot(1,'fvg')
        s.candles['1H']=[replace_like(c,confirmed=False) for c in s.candles['1H']]
        self.assertFalse(any(x.family=='MICRO_HTF_FVG' for x in p.scenarios(s,.01)))

    def test_exact_schedule_and_strategy_flag(self):
        w=(Path(__file__).parent/'.github/workflows/main.yml').read_text()
        self.assertIn("cron: '*/15 * * * *'",w)
        self.assertNotIn("5,10,20,25,35,40,50,55",w)
        self.assertNotIn("RESEARCH_ONLY'",w)
        self.assertIn("BZU_ENTRY_PLAYBOOK_MODE: 'STRICT'",w)
        self.assertIn("ANALYSIS_SCAN_MINUTES: '15'",w)
        self.assertNotIn('continuous_worker.py',w)


if __name__=='__main__':
    unittest.main()
