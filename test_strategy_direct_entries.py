"""Direct strategy entries cannot be inferred from a nearby old detector level."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import entry_playbooks as playbooks
import strategy_organics as organic
import strategy_direct_entries as direct
from test_entry_playbooks import sweep_snapshot, aligned_snapshot


class DirectStrategyEntries(unittest.TestCase):
    def anchor(self, snap, signal):
        cfg=engine.Config(tick=.01,core=core)
        regime=organic.classify(snap)
        with engine.market_clock(core,snap.now):
            anchor=direct.from_signal(signal,snap,cfg,
                {'price':snap.price,'atr15':.35,'regime':regime.state},
                core,regime,playbooks)
        return anchor,cfg

    def test_direct_long_and_short_do_not_require_native_detector(self):
        for side in (1,-1):
            with self.subTest(side=side):
                snap=sweep_snapshot(side)
                signal=next(s for s in playbooks.scenarios(snap,.01)
                            if s.family=='MICRO_SWEEP_MSS' and s.side==side)
                anchor,cfg=self.anchor(snap,signal)
                self.assertIsNotNone(anchor)
                self.assertEqual(anchor.evidence['entry_route'],'STRATEGY_EVENT')
                self.assertEqual(anchor.evidence['source_strategy'],signal.family)
                self.assertEqual(anchor.created_ts,signal.signal_ts)
                self.assertEqual(anchor.evidence['strategy_event_ts'],signal.event_ts)
                self.assertNotEqual(engine.anchor_zone_key(anchor,cfg),
                                    engine.ident(anchor.setup_type,anchor.side,
                                      round(anchor.level/cfg.tick),
                                      round(anchor.invalidation/cfg.tick),anchor.kind))
                plan={'side':side,'entry':anchor.level,'stop':anchor.invalidation-side*.5,
                      'entry_route':'STRATEGY_EVENT','anchor_proof':anchor.evidence}
                self.assertTrue(playbooks.assess(plan,snap,.01)['accepted'])

    def test_source_strategy_cannot_be_cross_certified(self):
        snap=sweep_snapshot()
        signal=next(s for s in playbooks.scenarios(snap,.01)
                    if s.family=='MICRO_SWEEP_MSS')
        anchor,_=self.anchor(snap,signal)
        plan={'side':1,'entry':anchor.level,'stop':anchor.invalidation-.5,
              'entry_route':'STRATEGY_EVENT','anchor_proof':{**anchor.evidence,
                                                            'source_strategy':'MICRO_HTF_FVG'}}
        self.assertFalse(playbooks.assess(plan,snap,.01)['accepted'])
        plan['anchor_proof']={**anchor.evidence,'strategy_signal_ts':signal.signal_ts-180000}
        self.assertFalse(playbooks.assess(plan,snap,.01)['accepted'])

    def test_unconfirmed_future_and_retested_event_do_not_make_anchor(self):
        snap=sweep_snapshot()
        signal=next(s for s in playbooks.scenarios(snap,.01)
                    if s.family=='MICRO_SWEEP_MSS')
        snap.now=signal.signal_ts-1
        anchor,_=self.anchor(snap,signal)
        self.assertIsNone(anchor)
        # A previously visited level is no longer an admissible source event.
        snap=sweep_snapshot()
        signal=next(s for s in playbooks.scenarios(snap,.01)
                    if s.family=='MICRO_SWEEP_MSS')
        final=snap.candles['3m'][-1]
        final.low=signal.zone-.05
        self.assertFalse(any(x.family=='MICRO_SWEEP_MSS'
                             for x in playbooks.scenarios(snap,.01)))

    def test_stale_signal_is_not_resurrected(self):
        snap=sweep_snapshot()
        signal=next(s for s in playbooks.scenarios(snap,.01)
                    if s.family=='MICRO_SWEEP_MSS')
        snap.now+=2*playbooks.STEP
        anchor,_=self.anchor(snap,signal)
        self.assertIsNone(anchor)

    def test_trend_requires_confirmed_regime(self):
        snap=aligned_snapshot(1,'fvg')
        signal=next(s for s in playbooks.scenarios(snap,.01)
                    if s.family=='MICRO_HTF_FVG')
        anchor,_=self.anchor(snap,signal)
        self.assertIsNone(anchor)  # no 4H history: UNKNOWN

    def test_mirrored_trend_strategy_has_same_proxy(self):
        self.assertEqual(direct.proxy_setup('MICRO_HTF_FVG',1),
                         direct.proxy_setup('MICRO_HTF_FVG',-1))
        self.assertEqual(direct.proxy_setup('MICRO_SWEEP_MSS',-1), 'MSS_REVERSAL_SHORT')
        self.assertIsNone(direct.proxy_setup('UNVERIFIED',-1))


if __name__ == '__main__': unittest.main()
