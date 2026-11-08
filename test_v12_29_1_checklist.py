"""v12.29.1 checklist: causal volume, real targets and post-TP1 M5 protection."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import bzu_policy
import full_limit_engine as engine
import position_monitor
import setup_scanner as scanner
from test_v12_24_momentum import anchor_for, fixture, NOW
from test_v12_28_quant import target_for


class FormationVolumeTests(unittest.TestCase):
    def test_volume_requires_strict_ratio_on_native_closed_impulse(self):
        for tf in scanner.PRIMARY:
            with self.subTest(timeframe=tf):
                snap, _, _ = fixture()
                step = engine.TF[tf]
                known = NOW // step * step
                bars = [engine.Candle(known - i * step, 100., 100.3, 99.9, 100.2, 100.)
                        for i in range(21, 0, -1)]
                bars[-1] = replace(bars[-1], volume=135.)
                bars.append(replace(bars[-1], ts=known, volume=1000.))
                snap = replace(snap, now=known + step, candles={tf: bars})
                zone = {'timeframe': tf, 'known_ts': known + step, 'impulse_known_ts': known}
                result = scanner.formation_volume(zone, snap)
                self.assertEqual(result['sma_volume_20'], 100.)
                self.assertFalse(result['confirmed'])
                bars[-2] = replace(bars[-2], volume=135.01)
                self.assertTrue(scanner.formation_volume(zone, snap)['confirmed'])
                bars[-2] = replace(bars[-2], confirmed=False)
                self.assertFalse(scanner.formation_volume(zone, snap)['confirmed'])
                bars[-2] = replace(bars[-2], confirmed=True)
                bars.pop(5)
                self.assertFalse(scanner.formation_volume(zone, snap)['confirmed'])

    def test_weak_impulse_blocks_real_plan_and_caps_public_score(self):
        snap, cfg, context = fixture()
        anchor = anchor_for(snap, cfg, context)
        snap.candles['15m'][-1] = replace(snap.candles['15m'][-1], volume=135.)
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            plan, reason = engine.full_plan(anchor, context, snap, cfg, {})
            self.assertIsNone(plan)
            self.assertEqual(reason, 'FORMATION_VOLUME_REQUIRED')
            preview = scanner.calculate_zone_trade_plan(
                {**anchor.evidence['entry_zone'], 'side': 1}, snap, cfg, engine, 1)
        self.assertLess(preview['score'], 60)
        self.assertEqual(preview['tactic'], 'OBSERVE')

    def test_no_structural_target_means_no_executable_tp1_or_runner(self):
        for side in (1, -1):
            snap, cfg, context = fixture(side)
            anchor = anchor_for(snap, cfg, context)
            for levels, expected in (([], 'STRUCTURAL_TP1_REQUIRED'),
                                     (target_for(side)[:1], 'STRUCTURAL_TP2_REQUIRED')):
                with self.subTest(side=side, expected=expected), patch.object(
                        engine, 'full_targets', return_value=levels):
                    plan, reason = engine.full_plan(anchor, context, snap, cfg, {})
                    self.assertIsNone(plan)
                    self.assertEqual(reason, expected)

    def test_atr_floor_and_full_structural_buffer_survive_rounding(self):
        _, cfg, _ = fixture()
        for side in (1, -1):
            _, risk, safe = scanner.normalized_zone_stop(100., 100. - side * .04, side, 1., cfg, engine)
            self.assertAlmostEqual(risk, .40)
            self.assertTrue(safe)
            _, risk, safe = scanner.normalized_zone_stop(100., 100. - side * .60, side, .40, cfg, engine)
            self.assertAlmostEqual(risk, .65)
            self.assertFalse(safe)


    def test_obsolete_waiting_limits_are_cancelled_at_safe_boundary(self):
        snap, cfg, _ = fixture()
        snap = replace(snap, now=NOW+1)
        for zone, setup, reason in (
            ({'kind':'CONFIRMED_FVG','timeframe':'3m'}, 'BREAKOUT_RETEST', 'MICRO_ZONE_EXCLUDED'),
            ({'kind':'DEMAND_OB','timeframe':'15m'}, 'SESSION_MEAN_RECLAIM', 'SESSION_MEAN_LIMIT_DISABLED'),
        ):
            with self.subTest(reason=reason):
                book = engine.new_state(cfg)
                pending = {'id':'old-limit', 'setup':setup, 'entry_zone':zone, 'effective_ts':NOW}
                engine.review_waiting_conditions(book, pending, snap, cfg)
                self.assertEqual(pending['cancel_reason'], reason)
                self.assertGreater(pending['cancel_effective_ts'], snap.now)


class BreakevenProofTests(unittest.TestCase):
    def filled(self, side=1):
        snap, cfg, context = fixture(side)
        anchor = anchor_for(snap, cfg, context)
        with patch.object(engine, 'full_targets', return_value=target_for(side)):
            plan, reason = engine.full_plan(anchor, context, snap, cfg, {})
        self.assertIsNotNone(plan, reason)
        entry = plan['entry']
        book = engine.new_state(cfg)
        book['active'] = bzu_policy.first_fill(
            plan, engine.Candle(NOW, entry, entry, entry, entry), engine, cfg, 60_000)
        return book, book['active'], cfg

    def test_initial_stop_survives_old_amendment_and_second_touch(self):
        book, position, cfg = self.filled()
        entry, original = position['entry'], position['initial_stop']
        position['pending_stop'] = {'price': entry, 'effective_ts': NOW + 60_000}
        engine.manage_bar(book, engine.Candle(NOW+60_000, entry+.1, entry+.2, entry-.01, entry+.1),
                          cfg, step=60_000)
        self.assertEqual(position['stop'], original)
        self.assertIs(book['active'], position)
        self.assertNotIn('pending_stop', position)
        self.assertIn('retired_stop_amendment', position)

    def test_partial_then_closed_m5_moves_stop_forward_and_covers_fees(self):
        for side in (1, -1):
            with self.subTest(side=side):
                book, position, cfg = self.filled(side)
                entry, original = position['entry'], position['initial_stop']

                def candle(ts, o, hi, lo, close, confirmed=True):
                    a, b = sorted((entry + side * hi, entry + side * lo))
                    return engine.Candle(ts, entry + side * o, b, a, entry + side * close, 100., confirmed)

                target = side * (position['tp1'] - entry)
                engine.manage_bar(book, candle(NOW+60_000, .3, target, .25, .4), cfg, step=60_000)
                self.assertEqual(position['remaining'], .5)
                self.assertEqual(position['stop'], original)
                self.assertNotIn('pending_stop', position)
                base = NOW + 300_000
                m5 = [candle(base, .40, .52, .30, .43),
                      candle(base+300_000, .42, .50, .24, .35),
                      candle(base+600_000, .35, .60, .31, .57, False)]
                close = base + 900_000
                snap = engine.Snapshot(close, entry+side*.57, {'5m': m5})
                self.assertIsNone(position_monitor.confirmed_m5_structure(
                    snap, NOW+120_000, close, side, entry, cfg.tick))
                m5[-1] = replace(m5[-1], confirmed=True)
                # A later, already present snapshot bar cannot arm the earlier execution bar.
                engine.manage_bar(book, candle(close-120_000, .4, .50, .31, .45),
                                  cfg, step=60_000, snapshot=snap)
                self.assertNotIn('pending_stop', position)
                engine.manage_bar(book, candle(close-60_000, .45, .60, .40, .57),
                                  cfg, step=60_000, snapshot=snap)
                expected = bzu_policy.breakeven_price(position, cfg, engine)
                self.assertAlmostEqual(position['pending_stop']['price'], expected)
                self.assertEqual(position['pending_stop']['effective_ts'], close)
                self.assertEqual(position['stop'], original)
                actual_exit = expected * (1 - side * cfg.slippage_bps / 10_000)
                self.assertGreaterEqual(side * (actual_exit-entry) -
                    entry * position['entry_fee_rate'] - actual_exit * cfg.taker_fee, -1e-8)
                engine.manage_bar(book, candle(close, .57, .60, .45, .55), cfg, step=60_000, snapshot=snap)
                self.assertAlmostEqual(position['stop'], expected)
                self.assertNotIn('pending_stop', position)

    def test_marker_without_partial_and_subminimum_tp1_cannot_arm(self):
        _, position, _ = self.filled()
        position.update(tp1_hit=True, tp1_reached_ts=NOW+60_000)
        self.assertIsNone(bzu_policy.partial_tp1_time(position))
        position.update(remaining=.5, tp1=position['entry']+1.39*position['risk'],
                        realized_legs=[{'target':'TP1','fraction':.5,'ts':NOW+60_000}])
        self.assertIsNone(bzu_policy.partial_tp1_time(position))


if __name__ == '__main__':
    unittest.main()
