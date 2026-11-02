"""New as-of tests: BOS/FVG first retest, stale-event safety, 15m report gate."""
from __future__ import annotations
import math
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import entry_playbooks as play
import strategy_organics
import strategy_direct_entries
import full_limit_engine as engine
import bot_oneshot as core

BASE=1800000000000

def b(ts,o,h,l,c,confirmed=True):
    return SimpleNamespace(ts=ts,open=o,high=h,low=l,close=c,confirmed=confirmed)

def fixture(side=-1):
    t=BASE+80*play.MICRO
    m=[]
    for i in range(80):
        o=100+.025*math.sin(i)
        m.append(b(BASE+i*play.MICRO,o,o+.19,o-.19,o+.005))
    m[75]=b(BASE+75*play.MICRO,99.9,100.05,99.7,99.78)
    m[76]=b(BASE+76*play.MICRO,99.7,99.78,98.05,98.2)
    m[77]=b(BASE+77*play.MICRO,98.1,98.3,97.7,97.9)
    m[78]=b(BASE+78*play.MICRO,97.95,98.,97.6,97.75)
    m[79]=b(BASE+79*play.MICRO,97.7,97.9,97.5,97.65)
    w=[b(BASE+(i-30)*play.STEP,100.1,100.3,99.85,100.0) for i in range(30)]
    # Window of HTF closes ends before the snapshot, no unknown future bar.
    h=[b(t-(32-i)*play.HOUR,110-.3*(i),110.3-.3*(i),109.6-.3*i,109.7-.3*i)
       for i in range(32)]
    four=[b(t-(32-i)*play.FOUR_HOURS,112-.2*(i),112.2-.2*i,111.7-.2*i,111.85-.2*i)
          for i in range(32)]
    if side==1:
        def mirror(c):return b(c.ts,200-c.open,200-c.low,200-c.high,200-c.close,c.confirmed)
        m=list(map(mirror,m));w=list(map(mirror,w));h=list(map(mirror,h));four=list(map(mirror,four))
    return SimpleNamespace(now=t,candles={'3m':m,'15m':w,'1H':h,'4H':four},price=m[-1].close)

class FastShortTests(unittest.TestCase):
    def test_short_and_long_new_bos_fvg_are_symmetric(self):
        for side in (1,-1):
            with self.subTest(side=side):
                s=fixture(side)
                events=[x for x in play.bos_fvg_continuations(s,.01) if x.side==side]
                self.assertTrue(events)
                x=events[0]
                self.assertEqual(x.family,'MICRO_BOS_FVG')
                self.assertLess(x.signal_ts,s.now)
                self.assertGreater(side*(s.price-x.zone),0)
                self.assertGreater(side*(x.zone-x.invalidation),0)
                verdict=play.assess({'setup_type':'PULLBACK_CONTINUATION','entry':x.zone,'side':side,'stop':x.invalidation},s,.01)
                self.assertTrue(verdict['accepted'],verdict)
                self.assertEqual(verdict['playbook'],x.family)
                strategy= strategy_direct_entries.from_signal(x,s,SimpleNamespace(tick=.01),
                    {'price':s.price,'atr15':1.,'atr3':.2},core,strategy_organics.classify(s),play)
                self.assertIsNotNone(strategy)
                self.assertEqual(strategy.evidence['source_strategy'],'MICRO_BOS_FVG')

    def test_no_future_signal_until_fvg_candle_closes(self):
        s=fixture()
        s.now=s.candles['3m'][77].ts+play.MICRO-1
        self.assertFalse(play.bos_fvg_continuations(s,.01))
        s=fixture();s.candles['3m'][77].confirmed=False
        self.assertFalse(play.bos_fvg_continuations(s,.01))

    def test_first_retest_is_never_rearmed(self):
        s=fixture()
        event=play.bos_fvg_continuations(s,.01)[0]
        last=s.candles['3m'][-1]
        s.candles['3m'][-1]=b(last.ts,last.open,event.zone+.02,last.low,last.close)
        self.assertFalse(any(e.family=='MICRO_BOS_FVG' for e in play.bos_fvg_continuations(s,.01)))

    def test_market_gap_cannot_confirm(self):
        s=fixture();s.candles['3m'].pop(-5)
        self.assertFalse(play.bos_fvg_continuations(s,.01))

    def test_both_htf_opposing_prohibit_bos(self):
        s=fixture(-1)
        def mirror(c):return b(c.ts,200-c.open,200-c.low,200-c.high,200-c.close)
        s.candles['1H']=list(map(mirror,s.candles['1H']))
        s.candles['4H']=list(map(mirror,s.candles['4H']))
        self.assertFalse(any(x.side==-1 for x in play.bos_fvg_continuations(s,.01)))

    def test_closed_micro_does_not_promote_distant_unrelated_legacy_level(self):
        s=fixture()
        x=play.bos_fvg_continuations(s,.01)[0]
        wrong={'entry':x.zone+8,'side':-1,'stop':x.invalidation}
        self.assertFalse(play.assess(wrong,s,.01)['accepted'])

    def test_report_once_per_quarter_not_every_scan(self):
        quarter=900_000
        now=BASE+6*quarter
        self.assertTrue(engine.report_due(now,now-quarter))
        self.assertFalse(engine.report_due(now+5*60_000,now))
        self.assertFalse(engine.report_due(now+10*60_000,now))
        self.assertTrue(engine.report_due(now+quarter,now))

    def test_config_min_stop_and_target_and_net_rr(self):
        cfg=engine.Config(min_stop_pct=.010,min_target_pct=.020)
        cfg.validate()
        with self.assertRaises(ValueError):replace(cfg,min_stop_pct=.009).validate()
        with self.assertRaises(ValueError):replace(cfg,min_target_pct=.019).validate()
        # TP still needs 1.2R net; the 2.0% floor is NOT a naked fill trigger.
        self.assertGreater(cfg.min_net_rr,1)

    def test_zone_asof_nonaccepted_barrier_remains_hard(self):
        import limit_structure
        zone=SimpleNamespace(kind='FVG_BULL',side='LONG',low=99.,high=100.,
                             strength=2.,created_ts=BASE)
        known=BASE+2*play.HOUR
        now=known+3*play.MICRO
        future=[b(known+i*play.MICRO,100.1,100.2,98.,98.2) for i in range(3)]
        # Build just-formed 1H zone with meaningful prior formation impulse
        native=[b(BASE,100.4,100.6,99.8,100.),b(BASE+play.HOUR,100.,100.2,99.,99.05)]
        past=limit_structure.zone_lifecycle(zone,native,'1H',now-1,.5,.01,future,'3m')
        # Closing 3m bars 0,1,2 only count when their close time <= now.
        after=limit_structure.zone_lifecycle(zone,native,'1H',now,.5,.01,future,'3m')
        self.assertGreaterEqual(after['accepted_depth_fraction'],0.)
        self.assertFalse(past['acceptance_known_ts'] and past['acceptance_known_ts']>now-1)
        self.assertTrue(after['acceptance_known_ts'] is None or after['acceptance_known_ts']<=now)

if __name__=='__main__':unittest.main()
