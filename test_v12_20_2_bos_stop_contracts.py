"""v12.20.2: end-to-end BOS/FVG route and a single LIMIT stop-ATR contract.

Synthetic examples verify causal routing, not historical win-rate or exchange fills.
"""
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import bot_oneshot as core
import entry_playbooks as play
import full_limit_engine as engine
import strategy_direct_entries as direct
import strategy_organics as organic
from test_v12_19_fast_short import fixture


class BosFvgAdmissionTests(unittest.TestCase):
    def test_closed_bos_fvg_event_can_build_and_certify_direct_limit_for_both_sides(self):
        for side in (-1, 1):
            with self.subTest(side=side):
                snapshot = fixture(side)
                signal = next(x for x in play.scenarios(snapshot, .01)
                              if x.family == 'MICRO_BOS_FVG' and x.side == side)
                regime = organic.classify(snapshot)
                self.assertEqual(regime.state, 'TREND_SHORT' if side == -1 else 'TREND_LONG')
                qualification = organic.qualify(signal, snapshot, .01, regime)
                self.assertTrue(qualification['accepted'], qualification)
                self.assertEqual(qualification['strategy'], 'TREND_CONTINUATION')
                with engine.market_clock(core, snapshot.now):
                    anchor = direct.from_signal(
                        signal, snapshot, engine.Config(tick=.01, core=core),
                        {'price': snapshot.price, 'atr15': 1.0, 'atr3': .25},
                        core, regime, play)
                self.assertIsNotNone(anchor)
                self.assertEqual(anchor.setup_type, 'PULLBACK_CONTINUATION')
                self.assertEqual(anchor.evidence['source_strategy'], 'MICRO_BOS_FVG')
                self.assertEqual(anchor.evidence['entry_route'], 'STRATEGY_EVENT')
                plan = {'setup_type': anchor.setup_type, 'side': side, 'entry': anchor.level,
                        'stop': anchor.invalidation - side * .30,
                        'entry_route': 'STRATEGY_EVENT', 'anchor_proof': anchor.evidence}
                self.assertTrue(play.assess(plan, snapshot, .01)['accepted'])
                # A direct BOS/FVG never impersonates a native legacy detector.
                self.assertNotEqual(plan['entry_route'], 'NATIVE_PATTERN')

    def test_wrong_event_cannot_certify_a_direct_entry(self):
        snapshot = fixture(-1)
        signal = next(s for s in play.scenarios(snapshot, .01) if s.family == 'MICRO_BOS_FVG')
        regime = organic.classify(snapshot)
        with engine.market_clock(core, snapshot.now):
            anchor = direct.from_signal(signal, snapshot, engine.Config(tick=.01, core=core),
                                        {'price': snapshot.price, 'atr15': 1, 'atr3': .25},
                                        core, regime, play)
        plan = {'setup_type': anchor.setup_type, 'side': -1, 'entry': anchor.level,
                'stop': anchor.invalidation + .3,
                'entry_route': 'STRATEGY_EVENT', 'anchor_proof': anchor.evidence}
        self.assertTrue(play.assess(plan, snapshot, .01)['accepted'])
        for corrupted in ({'source_strategy': 'MICRO_HTF_FVG'},
                          {'strategy_signal_ts': signal.signal_ts - play.MICRO},
                          {'strategy_event_ts': signal.event_ts - play.MICRO}):
            with self.subTest(corrupted=corrupted):
                self.assertFalse(play.assess({**plan, 'anchor_proof': {**anchor.evidence, **corrupted}},
                                             snapshot, .01)['accepted'])

    def test_unknown_regime_does_not_authorize_direct_trend_entry(self):
        snapshot = fixture(-1)
        signal = next(s for s in play.scenarios(snapshot, .01) if s.family == 'MICRO_BOS_FVG')
        snapshot.candles['1H'] = []
        snapshot.candles['4H'] = []
        regime = organic.classify(snapshot)
        self.assertEqual(regime.state, 'UNKNOWN')
        self.assertFalse(organic.qualify(signal, snapshot, .01, regime)['accepted'])
        with engine.market_clock(core, snapshot.now):
            self.assertIsNone(direct.from_signal(
                signal, snapshot, engine.Config(tick=.01, core=core),
                {'price': snapshot.price, 'atr15': 1, 'atr3': .25}, core, regime, play))

    def test_future_or_expired_bos_event_cannot_create_direct_limit(self):
        snapshot = fixture(-1)
        signal = next(s for s in play.scenarios(snapshot, .01) if s.family == 'MICRO_BOS_FVG')
        for now in (signal.signal_ts - 1, signal.signal_ts + play.STEP + 1):
            with self.subTest(now=now):
                snapshot.now = now
                with engine.market_clock(core, now):
                    self.assertIsNone(direct.from_signal(
                        signal, snapshot, engine.Config(tick=.01, core=core),
                        {'price': snapshot.price, 'atr15': 1, 'atr3': .25},
                        core, organic.classify(snapshot), play))


class StopAtrContractTests(unittest.TestCase):
    def test_config_default_matches_launcher_and_env(self):
        self.assertAlmostEqual(engine.Config().max_stop_atr, core.LIMIT_MAX_STOP_ATR)
        with patch.dict(os.environ, {'LIMIT_MAX_STOP_ATR': '4.75',
                                     'ABS_MIN_STOP_PCT': '0.010',
                                     'MIN_TP1_DISTANCE_PCT': '0.020'}, clear=True):
            cfg = engine.Config.from_env()
        self.assertAlmostEqual(cfg.max_stop_atr, 4.75)
        self.assertEqual(cfg.min_stop_pct, .010)
        self.assertEqual(cfg.min_target_pct, .020)
        with self.assertRaises(ValueError):
            replace(cfg, max_stop_atr=7.0).validate()

    def test_full_planner_applies_cap_symmetrically_and_no_floor_override(self):
        from test_full_limit_engine import full_fixture
        for side in ('LONG', 'SHORT'):
            with self.subTest(side=side):
                cfg, context, anchor, snapshot = full_fixture(side)
                self.assertEqual(cfg.max_stop_atr, 4.0)
                # This real planner fixture fits the cap and must not be
                # stopped by the old, conflicting 2.0 ATR default.
                plan, reason = engine.full_plan(anchor, context, snapshot, cfg, {})
                self.assertIsNotNone(plan, reason)
                self.assertLessEqual(plan['risk'], cfg.max_stop_atr * context['atr15'] + 1e-9)
                # At lower as-of ATR, 1% minimum itself cannot fit 4 ATR.
                # Unlike the old 1.25*floor exception, this must fail closed.
                quiet = {**context, 'atr15': .15}
                diagnostics = {}
                plan, reason = engine.full_plan(anchor, quiet, snapshot, cfg, {}, diagnostics)
                self.assertIsNone(plan)
                self.assertEqual(reason, 'STRUCTURAL_STOP_TOO_WIDE')
                self.assertTrue(diagnostics['minimum_stop_exceeds_cap'])
                self.assertAlmostEqual(diagnostics['atr_stop_cap'], cfg.max_stop_atr * .15)

    def test_stop_uses_one_true_cap_even_when_minimum_is_unachievable(self):
        cfg = engine.Config(tick=.01, core=core)
        anchor = SimpleNamespace(side='SHORT', level=100., invalidation=101.1)
        context = {'atr3': .5}
        entry, stop, risk, floor, _ = engine.stop_geometry(anchor, context, cfg)
        self.assertGreaterEqual(risk, 1.)
        self.assertGreaterEqual(floor, 1.)
        # Normal ATR: stop can fit within the cap.
        self.assertLessEqual(risk, cfg.max_stop_atr * .6)
        # Quiet market: the 1% floor would exceed 4 ATR and MUST NOT be
        # silently accepted by a multiplier of floor.
        self.assertGreater(floor, cfg.max_stop_atr * .2)


if __name__ == '__main__':
    unittest.main()
