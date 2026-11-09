"""Trend pre-limits use the same causal plan at scan, execution and display."""
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import live_terminal
import market_wave as wave
import portfolio_arbitration as arbitration
import setup_scanner as scanner
from test_v12_24_momentum import NOW, STEP
from test_v12_31_market_wave import snapshot
from test_v12_32_organic_oil import swing_fixture


class TrendPullback(unittest.TestCase):
    def test_weak_single_body_can_break_a_two_touch_shelf(self):
        ohlc = [(97.04, 97.3, 96.88, 97.05)] * 17 + [
            (97.10, 97.12, 96.70, 96.92), (96.92, 96.98, 96.66, 96.80),
            (96.80, 96.82, 96.54, 96.59)]
        bars = [engine.Candle(NOW - (len(ohlc) - i) * STEP, *row) for i, row in enumerate(ohlc)]
        self.assertFalse(list(wave._trend_breakers(bars[:-1], -1, .01, engine)))
        shelf = next(z for z in wave._trend_breakers(bars, -1, .01, engine)
                     if z['wave_proof']['structure'] == 'SHELF')
        self.assertEqual((shelf['low'], shelf['high'], shelf['reference']), (96.66, 96.70, 96.70))
        self.assertEqual(shelf['known_ts'], NOW)
        self.assertEqual(shelf['wave_proof']['reference_known_ts'], NOW - STEP)

    def test_higher_timeframe_direction_survives_local_upswing(self):
        snap, cfg, _ = swing_fixture()
        original = {'state': 'BULLISH_ACTIVE', 'side_1h': -1, 'side_4h': -1,
                    'active_structure': {'side': 1}, 'local_structure': {'side': 1}}
        with patch.object(direction, 'market_regime', return_value=original):
            regime = direction.execution_regime(snap, cfg, engine)
        self.assertEqual(regime['state'], 'BEARISH_ACTIVE')
        self.assertEqual(regime['trend_phase'], 'BEARISH_PULLBACK_OPPORTUNITY')
        self.assertEqual(regime['trend_anchor_side'], -1)

    def test_only_an_adverse_m5_spike_blocks_trend_approach(self):
        snap, _, _ = swing_fixture()
        row = engine.Candle(NOW - wave.M5, 96, 97.20, 96, 97.1)
        snap = replace(snap, candles={'5m': [row]})
        self.assertFalse(adverse.approach(snap, .2, -1, trend=True)['blocked'])
        snap = replace(snap, candles={'5m': [replace(row, high=97.21)]})
        self.assertTrue(adverse.approach(snap, .2, -1, trend=True)['blocked'])
        self.assertFalse(adverse.approach(snap, .2, 1, trend=True)['blocked'])
        self.assertFalse(adverse.approach(replace(snap, now=NOW-1), .2, -1, trend=True)['blocked'])

    def test_unfilled_gap_is_registered_when_third_candle_closes(self):
        bars = [engine.Candle(NOW - (3-i)*STEP, *x) for i, x in enumerate([
            (96.40, 96.60, 96.30, 96.50), (96.50, 96.53, 95.97, 96.02),
            (96.00, 96.18, 95.91, 96.08)])]
        self.assertFalse(list(wave._trend_fvgs(bars[:2], -1, STEP, .01, engine)))
        gap, = wave._trend_fvgs(bars, -1, STEP, .01, engine)
        self.assertEqual((gap['low'], gap['high'], gap['known_ts']), (96.18, 96.30, NOW))
        filled = engine.Candle(NOW, 96.08, 96.31, 96.00, 96.20)
        self.assertFalse(list(wave._trend_fvgs([*bars, filled], -1, STEP, .01, engine)))

    def test_ote_requires_the_right_candle_and_uses_50_to_61_8_percent(self):
        ohlc = [(97.58, 97.72, 97.39, 97.59)] * 12 + [
            (97.61, 98.00, 97.55, 97.80), (97.80, 97.91, 97.51, 97.61),
            (97.62, 97.64, 97.04, 97.18), (97.16, 97.17, 96.57, 96.70),
            (96.69, 96.70, 95.75, 95.84), (95.85, 96.05, 95.82, 96.02)]
        bars = [engine.Candle(NOW - (len(ohlc)-i)*STEP, *row) for i, row in enumerate(ohlc)]
        self.assertFalse(list(wave._trend_ote(bars[:-1], -1, .01, engine)))
        ote, = wave._trend_ote(bars, -1, .01, engine)
        self.assertAlmostEqual(ote['low'], 95.75 + .50 * (98.0 - 95.75))
        self.assertAlmostEqual(ote['high'], 95.75 + .618 * (98.0 - 95.75))
        self.assertEqual(ote['known_ts'], NOW)

    def test_live_cycle_preplaces_and_projects_a_trend_order_beyond_old_window(self):
        snap, cfg, targets = swing_fixture()
        snap = replace(snap, price=97.12, bid=97.11, ask=97.13)
        original = direction.market_regime(snap, cfg.tick, engine)
        original = {**original, 'side_1h': 1, 'side_4h': 0}
        book = engine.new_state(cfg)
        with patch.object(direction, 'market_regime', return_value=original),                 patch.object(engine, 'full_targets', return_value=targets),                 patch.dict(os.environ, {'BZU_ENTRY_MODE':'HYBRID'}):
            result = engine.run_cycle(book, snap, cfg)
            public = live_terminal.build(book, snap, result, cfg, engine)
        self.assertEqual(result['action'], 'PLACE_LIMIT')
        pending, = engine.pending_plans(book)
        self.assertEqual(pending['trend_rel'], 'WITH_TREND')
        self.assertEqual(pending['entry_condition'], 'RETEST')
        self.assertEqual(pending['approach']['brake_model'], 'TREND_M5_SPIKE_1_20')
        self.assertGreater(snap.price - pending['entry'], .65)
        self.assertAlmostEqual(public['orders'][0]['entry_max_distance'], scanner.TREND_LIMIT_MAX)
        self.assertEqual(public['orders'][0]['min_score'], 52)
        self.assertEqual(arbitration.decision([pending], [], snap, cfg, direction)['decision'],
                         'KEEP_TREND_PRELIMIT')

    def test_countertrend_macro_sweep_still_needs_closed_choch(self):
        m15 = [(97.51, 97.70, 97.30, 97.52)] * 79 + [(97.63, 97.82, 97.53, 97.53)]
        m5 = [(97.53, 97.64, 97.48, 97.55)] * 7 + [(97.63, 97.82, 97.53, 97.53)]
        snap, cfg = snapshot(97.53, m15, m5)
        h1 = [engine.Candle(c.ts, 97.44, 97.56, 97.30, 97.48) for c in snap.candles['1H']]
        h1[-6] = replace(h1[-6], high=97.70)
        snap = replace(snap, candles={**snap.candles, '1H': h1})
        bullish = {'state': 'BULLISH_ACTIVE', 'side_1h': 1, 'side_4h': 1, 'active_structure': {'side': 1}}
        with patch.object(direction, 'market_regime', return_value=bullish):
            context = engine.build_full_context(snap, engine.new_state(cfg), cfg)
            macro = next(a for a in scanner.organic_anchors(snap, cfg, context, engine)
                         if (a.evidence['entry_zone'].get('sweep') or {}).get('macro_timeframe') == '1H')
            with patch.object(direction, 'structural_flip', return_value={'confirmed':False}):
                self.assertEqual(engine.full_plan(macro, context, snap, cfg, {})[1], 'COUNTER_CHOCH_REQUIRED')
            with patch.object(direction, 'structural_flip', return_value={'confirmed':True,'known_ts':NOW}):
                self.assertEqual(engine.full_plan(macro, context, snap, cfg, {})[1], 'COUNTER_M5_REQUIRED')
        self.assertEqual(scanner.min_plan_score('COUNTER_TREND'), 78)

    def test_trend_runner_projection_keeps_untested_first_exit_barriers(self):
        snap, cfg, _ = swing_fixture()
        barrier = {'level':95.9,'kind':'DEMAND_OB','timeframe':'4H','known_ts':NOW-1,'hard_barrier':True}
        hard = scanner.organic_targets(96.7, 1.83, -1, [barrier], snap, cfg.tick, engine, trend=True)
        tested = scanner.organic_targets(96.7, 1.83, -1, [{**barrier,'freshness':'TESTED'}],
                                         snap, cfg.tick, engine, trend=True)
        self.assertFalse(hard['path_clear'])
        self.assertTrue(tested['path_clear'])
        self.assertEqual(tested['tp2_source'],'R_MULTIPLE_1_90')
