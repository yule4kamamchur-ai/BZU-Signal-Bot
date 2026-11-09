"""Release regressions for causality, distance and live trade-plan admission."""
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import directional_policy as direction
import full_limit_engine as engine
import live_terminal
import market_wave as wave
import setup_scanner as scanner
from test_v12_24_momentum import NOW, STEP, fixture


def snapshot(price, m15, m5, htf_side=1):
    snap, cfg, _ = fixture()
    for tf in ('1H', '4H'):
        snap.candles[tf] = [engine.Candle(
            c.ts, (value := 95. + htf_side * i * .025), value + .08, value - .08,
            value + htf_side * .02, 100.) for i, c in enumerate(snap.candles[tf])]
    snap.candles['3m'] = [engine.Candle(c.ts, price, price + .04, price - .04, price, 100.)
                          for c in snap.candles['3m']]
    snap.candles['15m'] = [engine.Candle(NOW - (len(m15) - i) * STEP, *row, 100.)
                          for i, row in enumerate(m15)]
    snap.candles.pop('1m')
    snap.candles['5m'] = [engine.Candle(NOW - (len(m5) - i) * wave.M5, *row, 100.)
                         for i, row in enumerate(m5)]
    snap.price, snap.bid, snap.ask = price, price - .01, price + .01
    return snap, cfg


def continuation():
    m15 = [(96.29, 96.50, 96.14, 96.30)] * 80
    m15[-6] = (96.26, 96.44, 96.04, 96.18)
    m15[-1] = (96.35, 96.35, 95.55, 95.69)
    m5 = [(96.30, 96.38, 96.10, 96.29)] * 3 + [
        (96.24, 96.35, 96.20, 96.32), (96.32, 96.34, 96.05, 96.10),
        (96.10, 96.13, 95.83, 95.88), (95.88, 95.90, 95.55, 95.69)]
    return snapshot(95.69, m15, m5)


class MarketWave(unittest.TestCase):
    def test_extreme_sweep_short_passes_old_bullish_regime(self):
        m15 = [(97.51, 97.70, 97.30, 97.52)] * 79 + [(97.63, 97.82, 97.53, 97.53)]
        m5 = [(97.53, 97.64, 97.48, 97.55)] * 7 + [(97.63, 97.82, 97.53, 97.53)]
        snap, cfg = snapshot(97.53, m15, m5)
        self.assertEqual(direction.market_regime(snap, cfg.tick, engine)['state'], 'BULLISH_TREND')
        before = replace(snap, now=snap.now - 1)
        self.assertFalse(any(z['kind'] == 'SWEEP_REVERSAL' for z in wave.zones(
            before, cfg, engine, direction.market_regime(before, cfg.tick, engine))))
        levels = [{'kind': 'SWING', 'level': level} for level in (97.25, 97.09)]
        state = engine.new_state(cfg)
        with patch.object(engine, 'full_targets', return_value=levels), patch.dict(
                os.environ, {'BZU_ENTRY_MODE': 'HYBRID'}):
            result = engine.run_cycle(state, snap, cfg)
            self.assertEqual(result['action'], 'PLACE_LIMIT')
            order, = engine.pending_plans(state)
            self.assertTrue(order['reversal_override'])
            self.assertEqual((order['source_strategy'], order['entry'], order['stop']),
                             ('SWEEP_REVERSAL', 97.70, 97.92))
            self.assertFalse(order['formation_volume']['confirmed'])
            self.assertLessEqual(order['entry_zone']['sweep']['reference_known_ts'],
                                 order['entry_zone']['wave_proof']['raid_ts'])
            public = live_terminal.build(state, snap, result, cfg, engine)
            self.assertEqual(public['orders'][0]['entry'], 97.70)
            self.assertEqual(public['primary_bias'], 1)

    def test_followup_choch_requires_the_closed_break_after_the_raid(self):
        m15 = [(97.51, 97.70, 97.30, 97.52)] * 79 + [(97.76, 97.83, 97.50, 97.53)]
        m5 = [(97.66, 97.70, 97.64, 97.65), (97.65, 97.69, 97.60, 97.64),
              (97.64, 97.72, 97.63, 97.69), (97.69, 97.71, 97.65, 97.67),
              (97.67, 97.83, 97.66, 97.76), (97.75, 97.79, 97.65, 97.68),
              (97.68, 97.70, 97.51, 97.53)]
        snap, cfg = snapshot(97.53, m15, m5)
        for now in (NOW - 1, NOW):
            with self.subTest(now=now):
                at = replace(snap, now=now)
                regime = direction.market_regime(at, cfg.tick, engine)
                plans = [z for z in wave.zones(at, cfg, engine, regime)
                         if z['kind'] == 'BREAKER_CHOCH' and z['side'] == -1]
                self.assertEqual(bool(plans), now == NOW)
                if plans:
                    self.assertEqual(plans[0]['wave_proof']['shift_level'], 97.60)
                    self.assertEqual(plans[0]['sweep']['extreme'], 97.83)

    def test_fast_shift_uses_only_confirmed_preexisting_swing(self):
        snap, cfg = continuation()
        regime = direction.market_regime(snap, cfg.tick, engine)
        self.assertEqual(regime['trend_state'], 'BULLISH_TREND')
        self.assertEqual(regime['state'], 'BEARISH_ACTIVE')
        proof = regime['active_structure']
        self.assertEqual(proof['proof'], 'FAST_M15_STRUCTURE_SHIFT')
        self.assertEqual(proof['level'], 96.04)
        self.assertLessEqual(proof['reference_known_ts'], NOW - STEP)
        self.assertGreater(proof['body_atr'], .6)
        before = replace(snap, now=NOW - 1)
        self.assertNotEqual(direction.market_regime(before, cfg.tick, engine)['state'], 'BEARISH_ACTIVE')

    def test_shallow_pullback_has_projected_targets_and_survives_full_router(self):
        snap, cfg = continuation()
        state = engine.new_state(cfg)
        with patch.object(engine, 'full_targets', return_value=[]), patch.dict(
                os.environ, {'BZU_ENTRY_MODE': 'HYBRID'}):
            result = engine.run_cycle(state, snap, cfg)
            self.assertEqual(result['action'], 'PLACE_LIMIT')
            order, = engine.pending_plans(state)
            self.assertEqual(order['source_strategy'], 'CONTINUATION_PULLBACK')
            self.assertEqual((order['entry'], order['stop'], order['tp1'], order['tp2']),
                             (95.90, 96.12, 95.57, 95.39))
            self.assertTrue(order['target_projection']['tp1'])
            self.assertGreaterEqual(order['rr1'], 1.40)
            self.assertGreater(order['net_rr'], cfg.min_net_rr)
            public = live_terminal.build(state, snap, result, cfg, engine)
            self.assertEqual(public['orders'][0]['tp1'], 95.57)
            self.assertEqual(public['primary_bias'], -1)
            self.assertTrue(any(z['kind'] == 'BEARISH_BREAKER' for z in public['zones']))
            self.assertTrue(any(z['kind'] == 'M5_IMPULSE_OB' for z in public['zones']))
        with patch.object(engine, 'full_targets', return_value=[]), patch.dict(
                os.environ, {'BZU_ENTRY_MODE': 'LIVE_REACTION'}):
            state = engine.new_state(cfg)
            result = engine.run_cycle(state, snap, cfg)
            public = live_terminal.build(state, snap, result, cfg, engine)
            self.assertEqual(public['radar']['SHORT']['entry'], 95.90)
            self.assertEqual(public['primary_candidate_id'], public['radar']['SHORT']['id'])
            self.assertFalse(public['orders'])

    def test_closed_flag_break_creates_a_passive_retest(self):
        snap, cfg = continuation()
        flag = [(96.06, 96.12, 95.71, 95.74), (95.74, 95.78, 95.72, 95.75),
                (95.75, 95.78, 95.73, 95.74), (95.74, 95.78, 95.72, 95.75),
                (95.75, 95.76, 95.54, 95.60)]
        m5 = [(96.17, 96.22, 96.02, 96.14)] * 3 + flag
        snap.candles['5m'] = [engine.Candle(NOW - (len(m5) - i) * wave.M5, *row, 100.)
                             for i, row in enumerate(m5)]
        snap.price, snap.bid, snap.ask = 95.60, 95.59, 95.61
        regime = direction.market_regime(snap, cfg.tick, engine)
        zones = wave.zones(snap, cfg, engine, regime)
        retest = next(z for z in zones if z['kind'] == 'FLAG_RETEST')
        self.assertEqual(retest['low'], 95.72)
        self.assertEqual(retest['known_ts'], NOW)
        before = replace(snap, now=NOW - 1)
        self.assertFalse(any(z['kind'] == 'FLAG_RETEST' for z in wave.zones(before, cfg, engine, regime)))
        context = engine.build_full_context(snap, engine.new_state(cfg), cfg)
        anchor = next(a for a in scanner.organic_anchors(snap, cfg, context, engine)
                      if a.evidence['source_strategy'] == 'FLAG_RETEST')
        with patch.object(engine, 'full_targets', return_value=[]):
            plan, reason = engine.full_plan(anchor, context, snap, cfg, {})
        self.assertIsNotNone(plan, reason)
        self.assertEqual((plan['entry'], plan['action_type']), (95.72, 'NEW_PENDING_LIMIT'))

    def test_hard_barrier_vetoes_a_fresh_projection(self):
        snap, cfg = continuation()
        zone = next(z for z in wave.zones(snap, cfg, engine, direction.market_regime(snap, cfg.tick, engine))
                    if z['kind'] == 'CONTINUATION_PULLBACK')
        target = {'kind': 'OB', 'level': 95.67, 'hard_barrier': True, 'known_ts': NOW}
        geometry = scanner.planned_targets(95.90, .22, -1, [target], snap, cfg, engine, zone, .36)
        self.assertFalse(geometry['path_clear'])
        self.assertNotIn('target_projection', geometry)

    def test_old_target_and_out_of_range_limit_cannot_be_primary(self):
        snap, cfg = continuation()
        for side in (1, -1):
            self.assertEqual(scanner.entry_window(side, 100., 100. - side * .09, .5, cfg), 'ENTRY_TOO_CLOSE')
            self.assertIsNone(scanner.entry_window(side, 100., 100. - side * .60, .5, cfg))
            self.assertEqual(scanner.entry_window(side, 100., 100. - side * .61, .5, cfg), 'DISTANT_ZONE')
            self.assertEqual(scanner.entry_window(side, 100., 100. - side * .66, .9, cfg), 'DISTANT_ZONE')
        self.assertTrue(scanner.exhausted_move(-1, 95.69, 96.14))
        waiting = {'id': 'old', 'side': -1, 'entry': 96.98, 'tp1': 96.14}
        with patch.object(engine, 'request_cancel') as cancel:
            engine.review_waiting_conditions({}, waiting, snap, cfg)
        self.assertEqual(cancel.call_args.args[2], 'EXHAUSTED_MOVE')
        waiting['tp1'] = 95.2
        with patch.object(engine, 'request_cancel') as cancel:
            engine.review_waiting_conditions({}, waiting, snap, cfg)
        self.assertEqual(cancel.call_args.args[2], 'LIMIT_UNREACHABLE')
