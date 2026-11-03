"""Regression checks for scan-gap and HTF-transition LONG/SHORT admission.

These are safety/causality contracts, not profitability or live-order claims.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import entry_playbooks as play
import strategy_organics as organic
import full_limit_engine as engine
from test_v12_19_fast_short import fixture, b
from test_v12_20_4_scan_gap_long import ScanGapRecoveryTests


class EntryAdmissionTests(unittest.TestCase):
    def mixed_case(self, side):
        s = fixture(side)
        t = s.now
        # 1H trend remains aligned, the lagging 4H trend is opposing.
        def invert(c):
            return b(c.ts, 200-c.open, 200-c.low, 200-c.high, 200-c.close)
        s.candles['4H'] = list(map(invert, s.candles['4H']))
        # Only CLOSED contiguous 15m bars support a measured trend.
        w = []
        for i in range(30):
            base = 99 + i * .05
            if side == -1:
                base = 200-base
            w.append(b(t-(30-i)*play.STEP, base, base+.13, base-.13, base))
        s.candles['15m'] = w
        regime = organic.classify(s)
        self.assertEqual(regime.state, 'MIXED')
        signal = next(x for x in play.bos_fvg_continuations(s,.01)
                      if x.family == 'MICRO_BOS_FVG' and x.side == side)
        return s, regime, signal

    def test_confirmed_mixed_bos_fvg_is_not_vetoed_when_1h_15m_align(self):
        for side in (-1,1):
            with self.subTest(side=side):
                s, regime, signal = self.mixed_case(side)
                result = organic.qualify(signal,s,.01,regime)
                self.assertTrue(result['accepted'],result)
                self.assertEqual(result['reason'],'STRATEGY_MIXED_1H_15M_BOS_CONFIRMED')
                self.assertTrue(play.assess({'setup_type':'PULLBACK_CONTINUATION',
                    'side':side,'entry':signal.zone,'stop':signal.invalidation},s,.01)['accepted'])

    def test_missing_or_stale_15m_confirmation_does_not_authorize_entry(self):
        s, regime, signal = self.mixed_case(1)
        s.candles['15m'].pop(-5)
        self.assertFalse(organic.qualify(signal,s,.01,regime)['accepted'])
        s, regime, signal = self.mixed_case(1)
        for row in s.candles['15m']:
            row.ts -= 4*play.STEP
        self.assertFalse(organic.qualify(signal,s,.01,regime)['accepted'])

    def test_unrelated_family_cannot_use_mixed_bos_exception(self):
        from dataclasses import replace
        s, regime, signal = self.mixed_case(1)
        other = replace(signal, family='MICRO_HTF_FVG')
        self.assertFalse(organic.qualify(other,s,.01,regime)['accepted'])

    def test_opposing_both_hourly_and_four_hourly_still_block(self):
        s, regime, signal = self.mixed_case(1)
        def invert(c):
            return b(c.ts, 200-c.open, 200-c.low, 200-c.high, 200-c.close)
        s.candles['1H'] = list(map(invert, s.candles['1H']))
        regime = organic.classify(s)
        self.assertFalse(organic.qualify(signal,s,.01,regime)['accepted'])

    def test_historical_view_uses_then_known_price_not_current_ticker(self):
        t = ScanGapRecoveryTests()
        s, signal, prev = t.delayed(1)
        s.price += 5.0  # Deliberately different from closes in the past.
        observed=[]
        original=play.scenarios
        def probe(view, tick):
            observed.append((view.now,view.price))
            return original(view,tick)
        with patch.object(play,'scenarios',side_effect=probe):
            play.scenarios_since(s,.01,prev)
        expected = [(now, next(c.close for c in reversed(s.candles['3m'])
                              if c.ts+play.MICRO<=now)) for now,_ in observed[1:]]
        self.assertEqual(observed[1:],expected)

    def test_missing_post_confirmation_bar_rejects_replay(self):
        t=ScanGapRecoveryTests()
        s,signal,prev=t.delayed(1)
        s.candles['3m'].pop(-4)
        recovered=play.scenarios_since(s,.01,prev)
        self.assertFalse(any(x.family=='MICRO_BOS_FVG' for x in recovered))

    def test_direct_setup_uses_true_15m_atr_not_disguised_3m_atr(self):
        """Exercise the complete scanner-to-planner call on both directions."""
        import bot_oneshot as core
        from test_v12_19_fast_short import fixture
        for side in (1,-1):
            with self.subTest(side=side):
                raw = fixture(side)
                t = raw.now
                raw.candles['15m'] = [b(t-(50-i)*play.STEP,
                    99+side*.05*i,99+side*.05*i+.13,
                    99+side*.05*i-.13,99+side*.05*i) for i in range(50)]
                candles = {tf:[engine.Candle(x.ts,x.open,x.high,x.low,x.close,0,True)
                    for x in bars] for tf,bars in raw.candles.items()}
                snap = engine.Snapshot(t,raw.price,candles,raw.price-.01,
                    raw.price+.01,True,t,'BZ-USDT-SWAP')
                cfg = engine.Config(tick=.01,include_commission=True,core=core)
                state = engine.new_state(cfg)
                seen=[]
                original=engine.full_plan
                def capture(a,context,*args,**kwargs):
                    if a.evidence.get('entry_route') == 'STRATEGY_EVENT':
                        seen.append((context['atr15'],engine.atr(snap.candles['15m'])))
                    return original(a,context,*args,**kwargs)
                with patch.object(engine,'full_plan',side_effect=capture):
                    engine.analyze(snap,cfg,state)
                self.assertTrue(seen)
                for used,actual in seen:
                    self.assertAlmostEqual(used,actual)

    def test_telegram_gate_and_sl_tp_minimums_unaffected(self):
        c=engine.Config()
        self.assertEqual(c.min_stop_pct,.01)
        self.assertEqual(c.min_target_pct,.02)
        self.assertFalse(engine.report_due(1793685900000,1793685600000))

if __name__=='__main__':
    unittest.main()
