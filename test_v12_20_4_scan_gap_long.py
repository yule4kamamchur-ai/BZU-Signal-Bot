"""Causal recovery of missed closed-3m LONG/SHORT signals on delayed GitHub scans."""
import unittest
from copy import deepcopy
from types import SimpleNamespace

import entry_playbooks as play
import strategy_direct_entries
import strategy_organics
import bot_oneshot as core
from test_v12_19_fast_short import fixture, b


class ScanGapRecoveryTests(unittest.TestCase):
    def delayed(self, side):
        s = fixture(side)
        signal = next(x for x in play.bos_fvg_continuations(s, .01)
                      if x.family == 'MICRO_BOS_FVG' and x.side == side)
        last_scan = signal.signal_ts - play.MICRO
        last_close = s.candles['3m'][-1].close
        # Six closed 3m bars after the original scan: no return to the limit.
        for i in range(6):
            ts = s.now + i * play.MICRO
            price = last_close + side * .015 * (i + 1)
            s.candles['3m'].append(b(ts, price, price + .005, price - .005, price))
        s.now += 6 * play.MICRO
        s.price = s.candles['3m'][-1].close
        return s, signal, last_scan

    def test_both_sides_recover_confirmed_not_touched_missed_bos_fvg(self):
        for side in (-1, 1):
            with self.subTest(side=side):
                s, original, last_scan = self.delayed(side)
                self.assertFalse(any(x.family == 'MICRO_BOS_FVG' for x in play.scenarios(s, .01)))
                recovered = play.scenarios_since(s, .01, last_scan)
                events = [x for x in recovered if x.family == 'MICRO_BOS_FVG' and x.side == side]
                self.assertEqual(len(events), 1)
                x = events[0]
                self.assertTrue(x.recovered_from_scan_gap)
                self.assertEqual(x.signal_ts, original.signal_ts)
                self.assertEqual(x.event_ts, original.event_ts)
                plan = {'setup_type': 'PULLBACK_CONTINUATION', 'side': side,
                        'entry': x.zone, 'stop': x.invalidation}
                self.assertTrue(play.assess(plan, s, .01, setup_candidates=recovered)['accepted'])
                candidate = strategy_direct_entries.from_signal(
                    x, s, SimpleNamespace(tick=.01),
                    {'price': s.price, 'atr15': 1., 'atr3': .2}, core,
                    strategy_organics.classify(s), play)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.created_ts, original.signal_ts)

    def test_any_post_confirmation_touch_blocks_recovery(self):
        for side in (-1, 1):
            with self.subTest(side=side):
                s, event, last_scan = self.delayed(side)
                c = s.candles['3m'][-4]
                if side == 1:
                    s.candles['3m'][-4] = b(c.ts, c.open, c.high, event.zone - .02, c.close)
                else:
                    s.candles['3m'][-4] = b(c.ts, c.open, event.zone + .02, c.low, c.close)
                self.assertFalse(any(x.family == 'MICRO_BOS_FVG'
                                     for x in play.scenarios_since(s, .01, last_scan)))

    def test_recovery_never_replays_events_before_last_scan(self):
        s, event, _ = self.delayed(1)
        self.assertFalse(any(x.family == 'MICRO_BOS_FVG' for x in
                             play.scenarios_since(s, .01, event.signal_ts + play.MICRO)))

    def test_recovery_window_is_bounded_and_no_retroactive_first_run(self):
        s, event, last_scan = self.delayed(1)
        self.assertEqual(play.scenarios_since(s, .01, 0), play.scenarios(s, .01))
        s.now += 15 * play.MICRO
        self.assertFalse(any(x.family == 'MICRO_BOS_FVG'
                             for x in play.scenarios_since(s, .01, last_scan)))

    def test_no_unclosed_or_missing_3m_data_as_confirmation(self):
        s, event, last_scan = self.delayed(1)
        s.candles['3m'][77].confirmed = False
        self.assertFalse(any(x.family == 'MICRO_BOS_FVG'
                             for x in play.scenarios_since(s, .01, last_scan)))
        s, event, last_scan = self.delayed(1)
        s.candles['3m'].pop(76)
        self.assertFalse(any(x.family == 'MICRO_BOS_FVG'
                             for x in play.scenarios_since(s, .01, last_scan)))


if __name__ == '__main__':
    unittest.main()
