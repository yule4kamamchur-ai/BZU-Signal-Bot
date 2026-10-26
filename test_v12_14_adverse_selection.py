"""Causal risk-veto regression tests. No synthetic win rate is asserted."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import entry_adverse_selection as guard

BASE=1_800_000_000_000
H=guard.HOUR_MS

def bar(i, op, hi, lo, close, confirmed=True):
    return SimpleNamespace(ts=BASE+i*H,open=op,high=hi,low=lo,
                           close=close,confirmed=confirmed)

class RejectionTrapTests(unittest.TestCase):
    def test_long_repeated_hourly_wick_rejection_blocks_near_liquidity(self):
        bars=[bar(0,111.9,111.95,111.8,111.82),
              bar(1,111.82,112.15,111.79,111.96),
              bar(2,111.96,112.17,111.79,111.86),
              bar(3,111.86,111.98,111.82,111.90),
              bar(4,111.90,111.92,111.86,111.87),
              bar(5,111.87,111.92,111.83,111.86)]
        targets=[{'kind':'1H_LIQUIDITY','level':112.17,
                  'freshness':'UNSWEPT','known_ts':BASE+H}]
        now=BASE+6*H
        outcome=guard.rejected_nearby_liquidity(1,111.97,1.14,targets,bars,now,.01)
        self.assertEqual(outcome['risk'],'HTF_REJECTED_NEAR_LIQUIDITY')
        self.assertLess(outcome['headroom_r'],.35)
        # No future or still-open hourly candle is allowed to change it.
        future=bar(6,111.86,114.0,111.8,113.0)
        self.assertEqual(outcome,guard.rejected_nearby_liquidity(1,111.97,1.14,targets,
                                                                  bars+[future],now,.01))
        fake_open=bar(5,111.87,114.0,111.83,113.0,confirmed=False)
        self.assertEqual(outcome,guard.rejected_nearby_liquidity(1,111.97,1.14,targets,
                                                                  bars+[fake_open],now,.01))

    def test_short_is_exact_price_mirror(self):
        original=[bar(0,111.9,111.95,111.8,111.82),
                  bar(1,111.82,112.15,111.79,111.96),
                  bar(2,111.96,112.17,111.79,111.86),
                  bar(3,111.86,111.98,111.82,111.90),
                  bar(4,111.90,111.92,111.86,111.87),
                  bar(5,111.87,111.92,111.83,111.86)]
        # Point-reflection around the same center turns wick rejection into
        # its exact sell-side mirror without changing the relative distances.
        center=110.
        mirrored=[bar(i,2*center-b.close,2*center-b.low,2*center-b.high,
                      2*center-b.open) for i,b in enumerate(original)]
        zone=[{'kind':'1H_LIQUIDITY','level':2*center-112.17,
               'freshness':'UNSWEPT','known_ts':BASE+H}]
        self.assertIsNotNone(guard.rejected_nearby_liquidity(-1,2*center-111.97,
                                      1.14,zone,mirrored,BASE+6*H,.01))

    def test_single_wick_or_accepted_break_is_not_rejected(self):
        bars=[bar(i,110.,110.12,109.9,110.08) for i in range(6)]
        bars[-1]=bar(5,110.08,110.44,110.02,110.10)
        zone=[{'kind':'1H_LIQUIDITY','level':110.44,'freshness':'UNSWEPT'}]
        self.assertIsNone(guard.rejected_nearby_liquidity(1,110.30,1.,zone,bars,BASE+6*H,.01))
        bars[-2:]=[bar(4,110.2,110.6,110.1,110.55),bar(5,110.55,110.7,110.5,110.65)]
        self.assertIsNone(guard.rejected_nearby_liquidity(1,110.30,1.,zone,bars,BASE+6*H,.01))

    def test_only_new_continuation_anchors_are_gated(self):
        bars=[bar(i,112.,112.2,111.8,111.85) for i in range(6)]
        target=[{'kind':'1H_LIQUIDITY','level':112.2,'freshness':'UNSWEPT'}]
        snap=SimpleNamespace(candles={'1H':bars},now=BASE+6*H)
        continuation=SimpleNamespace(setup_type='PULLBACK_CONTINUATION',
                                     side='LONG',evidence={})
        reversal=SimpleNamespace(setup_type='SWEEP_RECLAIM',side='LONG',evidence={})
        # Reversals use different contract and are not indiscriminately vetoed.
        self.assertIsNone(guard.admission(reversal,snap,target,111.95,1.0,.01))

    def test_missing_observations_and_invalid_numbers_fail_open_not_invent_data(self):
        rows=[bar(i,112.,112.2,111.8,111.85) for i in range(3)]
        targets=[{'kind':'1H_LIQUIDITY','level':112.2,'freshness':'UNSWEPT'}]
        self.assertIsNone(guard.rejected_nearby_liquidity(1,111.95,1.,targets,rows,BASE+3*H,.01))
        self.assertIsNone(guard.rejected_nearby_liquidity(1,111.95,float('nan'),targets,rows,BASE+3*H,.01))
        self.assertIsNone(guard.rejected_nearby_liquidity(1,111.95,1.,targets,rows,BASE+3*H,0))


class ThesisReviewTests(unittest.TestCase):
    def _snapshot(self, now=BASE+7*H):
        # Explicit bearish transitions known only through completed candles.
        hour=[bar(i,112.,112.1,111.7,111.8) for i in range(5)]
        hour.extend([bar(5,111.8,111.82,111.7,111.76),
                     bar(6,111.76,111.78,111.55,111.59)])
        quarter=[SimpleNamespace(ts=BASE+i*900_000,open=112.,high=112.1,
                                 low=111.6,close=111.8-.008*i,confirmed=True)
                 for i in range(28)]
        for i,close in enumerate((111.75,111.70,111.66,111.61,111.58)):
            quarter[-5+i].close=close
        return SimpleNamespace(now=now,price=111.58,
                               candles={'1H':hour,'15m':quarter})

    def test_post_deadline_loss_requires_two_closed_timeframes(self):
        s=self._snapshot();p={'side':1,'entry':111.97,'risk':1.14,
                              'exit_deadline':BASE+6*H,'tp1_hit':False}
        found=guard.failed_thesis_after_deadline(p,s,.01)
        self.assertIsNotNone(found)
        self.assertEqual(found['risk'],'POST_DEADLINE_THESIS_INVALIDATED')
        self.assertIsNone(guard.failed_thesis_after_deadline(p,
               self._snapshot(now=BASE+5*H),.01))
        self.assertIsNone(guard.failed_thesis_after_deadline({**p,'tp1_hit':True},s,.01))
        self.assertIsNone(guard.failed_thesis_after_deadline({**p,'mfe_r':1.15},s,.01))
        self.assertIsNone(guard.failed_thesis_after_deadline({**p,'entry':111.4},s,.01))
        # Session cutoff before the 6-hour configured holding period cannot
        # force an early exit even when the market looks adverse.
        premature={**p,'exit_deadline':BASE+2*H,
                   'filled_ts':BASE+H,'max_hold_minutes':360}
        self.assertIsNone(guard.failed_thesis_after_deadline(premature,
            self._snapshot(now=BASE+5*H),.01))

    def test_future_and_unconfirmed_htf_bar_cannot_justify_exit(self):
        s=self._snapshot()
        p={'side':1,'entry':111.97,'risk':1.14,'exit_deadline':BASE+6*H}
        result=guard.failed_thesis_after_deadline(p,s,.01)
        s.candles['1H'].append(bar(7,111.59,120,100,100,confirmed=False))
        s.candles['15m'].append(SimpleNamespace(ts=BASE+30*900_000,
            open=111.59,high=120,low=100,close=100,confirmed=True))
        self.assertEqual(result,guard.failed_thesis_after_deadline(p,s,.01))

    def test_live_review_version_locks_legacy_open_position(self):
        import full_limit_engine as engine
        snapshot=self._snapshot()
        cfg=engine.Config(tick=.01)
        base={'side':1,'entry':111.97,'risk':1.14,'exit_deadline':BASE+6*H,
              'target_management':'ADAPTIVE_FULL_VOLUME','status':'OPEN',
              'tp1_hit':False,'filled_ts':BASE+H, 'id':'example'}
        with patch.object(engine,'close_trade') as close:
            engine.review_full_exit({'active':{**base,'entry_version':'old-release'}},snapshot,cfg)
            close.assert_not_called()
            engine.review_full_exit({'active':{**base,'entry_version':engine.VERSION}},snapshot,cfg)
            close.assert_called_once()
            self.assertEqual(close.call_args.args[4], 'POST_DEADLINE_THESIS_INVALIDATED')

if __name__=='__main__': unittest.main()
