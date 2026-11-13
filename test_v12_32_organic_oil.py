"""Brent-specific release contracts on the production paths."""
import copy
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import full_limit_engine as engine
import live_terminal
import market_wave
import oil_risk
import setup_scanner as scanner
from test_v12_24_momentum import NOW, STEP, fixture
from test_v12_31_market_wave import snapshot as wave_snapshot


def swing_fixture():
    snap, cfg, _ = fixture(pivot=96.)
    for tf in ('1H', '4H'):
        snap.candles[tf] = [engine.Candle(c.ts, 95.8, 96.15, 95.40, 95.82, 100.)
                            for c in snap.candles[tf]]
    snap.candles['1H'][-5] = replace(snap.candles['1H'][-5], low=94.70)
    levels = [{'level': 98.26, 'kind': '1H_LIQUIDITY', 'timeframe': '1H', 'known_ts': snap.now - oil_risk.H1},
              {'level': 98.90, 'kind': '4H_LIQUIDITY', 'timeframe': '4H', 'known_ts': snap.now - oil_risk.H4}]
    return snap, cfg, levels


class OrganicOil(unittest.TestCase):
    def test_complete_days_and_hourly_true_range_have_causal_boundaries(self):
        day = NOW // oil_risk.DAY * oil_risk.DAY
        h4 = [engine.Candle(day - (90 - i) * oil_risk.H4, 96, 98, 95, 96) for i in range(90)]
        h1 = [engine.Candle(day - (32 - i) * oil_risk.H1, 96., 96.4, 95.6, 96.) for i in range(32)]
        h1[-1] = replace(h1[-1], high=98.4, low=97.6, open=98., close=98.)
        h4.append(engine.Candle(day, 96, 110, 90, 96, confirmed=False))
        profile = oil_risk.compute_organic_oil_risk_profile(96, [], h1, h4, as_of=day, active_session=True)
        self.assertEqual(profile['atr_source'], '1H_TRUE_RANGE')
        self.assertAlmostEqual(profile['atr_1h'], (13 * .8 + 2.4) / 14)
        self.assertEqual(profile['adr_days'], 14)
        self.assertEqual(profile['session_multiplier'], 1.2)
        self.assertLess(profile['adr_d1'], 4.)
        m15 = [engine.Candle(day - (20 - i) * oil_risk.M15, 96., 96.2, 95.8, 96.) for i in range(20)]
        fallback = oil_risk.compute_organic_oil_risk_profile(96, m15, [], [], as_of=day)
        self.assertEqual(fallback['atr_source'], '15M_X_2_15')
        self.assertAlmostEqual(fallback['atr_1h'], .86)
        self.assertIsNone(fallback['adr_d1'])

    def test_structural_stop_is_symmetric_and_cannot_clip_through_invalidation(self):
        snap, cfg, _ = swing_fixture()
        profile = oil_risk.snapshot_profile(snap)
        for side in (1, -1):
            with self.subTest(side=side):
                macro = {'level': 96 - side * .85, 'timeframe': '1H', 'known_ts': NOW - STEP}
                stop, risk, details = oil_risk.stop_geometry(96, 96 - side * .2, side, profile, [macro], cfg.tick, engine)
                self.assertIsNone(details['gate'])
                self.assertGreaterEqual(risk, .85)
                self.assertLessEqual(risk, 1.25)
                self.assertGreaterEqual(side * (macro['level'] - stop), details['buffer'] - 1e-8)
                _, _, details = oil_risk.stop_geometry(96, 96 - side * 2.03, side, profile, [macro], cfg.tick, engine)
                self.assertEqual(details['gate'], 'STRUCTURAL_STOP_TOO_WIDE')
                fallback = oil_risk.stop_geometry(96, 96 - side * .2, side, profile, [], cfg.tick, engine)[2]
                self.assertIsNone(fallback['gate'])
                self.assertTrue(fallback['anchor_valid'])

    def test_macro_target_windows_and_fallback_are_symmetric(self):
        snap, cfg, _ = swing_fixture()
        for side in (1, -1):
            def level(distance, timeframe, known=NOW - 1):
                return {'level': 96 + side * distance, 'kind': timeframe + '_LIQUIDITY',
                        'timeframe': timeframe, 'known_ts': known}
            noisy = [level(.70, '15m'), level(1.51, '15m'), level(1.51, '1H', NOW + 1)]
            runner = level(2.71, '4H')
            plan = scanner.organic_targets(96, 1.5, side, [*noisy, runner], snap, cfg.tick, engine)
            self.assertEqual(plan['tp1_source'], 'R_MULTIPLE_1_15')
            self.assertAlmostEqual(side * (plan['tp1'] - 96), 1.73)
            self.assertAlmostEqual(side * (plan['tp2'] - 96) / 1.5, 1.8)
            first = level(1.51, '1H')
            plan = scanner.organic_targets(96, 1.5, side, [first, runner], snap, cfg.tick, engine)
            self.assertAlmostEqual(side * (plan['tp1'] - 96), 1.50)
            self.assertTrue(plan['structural_tp2'])
            blocked = scanner.organic_targets(96, 1.5, side, [{**first, 'hard_barrier': True}, runner],
                                               snap, cfg.tick, engine)
            self.assertFalse(blocked['path_clear'])

    def test_real_router_and_terminal_share_one_organic_plan(self):
        snap, cfg, targets = swing_fixture()
        book = engine.new_state(cfg)
        with patch.object(engine, 'full_targets', return_value=targets), patch.dict(os.environ, {'BZU_ENTRY_MODE': 'HYBRID'}):
            result = engine.run_cycle(book, snap, cfg)
            self.assertEqual(result['action'], 'PLACE_LIMIT')
            order, = engine.pending_plans(book)
            self.assertEqual((order['entry'], order['stop'], order['tp1'], order['tp2']), (96., 94.41, 98.25, 98.89))
            self.assertTrue(oil_risk.is_organic(order))
            self.assertEqual(order['partials'], {'TP1': .5, 'TP2': .5})
            public = live_terminal.build(book, snap, result, cfg, engine)
            self.assertEqual(public['orders'][0]['organic_risk'], order['organic_risk'])
            self.assertEqual(public['max_new_pending'], 1)
            self.assertGreaterEqual(public['min_structural_spread'], 1.)
        # Explicitly check reservation even for two unrelated eligible records.
        other = {**order, 'id': 'legacy-duplicate', 'entry': 95.80}
        engine.park_pending(book, [order, other])
        eligible = engine.entry_priority(book, order['effective_ts'], 96.28, cfg, 60_000)
        self.assertEqual(len(eligible), 1)

    def test_live_reaction_can_publish_organic_plan_and_cannot_place(self):
        snap, cfg, targets = swing_fixture()
        book = engine.new_state(cfg)
        with patch.object(engine, 'full_targets', return_value=targets), patch.dict(os.environ, {'BZU_ENTRY_MODE': 'LIVE_REACTION'}):
            result = engine.run_cycle(book, snap, cfg)
            public = live_terminal.build(book, snap, result, cfg, engine)
        self.assertEqual(result['action'], 'WATCH_REACTION')
        self.assertFalse(engine.open_plans(book))
        self.assertEqual(public['radar']['LONG']['entry'], 96.)
        self.assertEqual(public['radar']['LONG']['organic_risk']['model'], oil_risk.MODEL)

    def test_no_4h_or_daily_runner_prevents_live_order(self):
        snap, cfg, targets = swing_fixture()
        targets[1]['timeframe'] = '1H'
        book = engine.new_state(cfg)
        with patch.object(engine, 'full_targets', return_value=targets):
            out = engine.run_cycle(book, snap, cfg)
        self.assertFalse(engine.pending_plans(book))
        self.assertGreater(out['analysis']['rejections'].get('STRUCTURAL_TP2_REQUIRED', 0), 0)

    def test_only_a_confirmed_macro_reclaim_can_override_the_old_trend(self):
        m15 = [(97.51, 97.70, 97.30, 97.52)] * 79 + [(97.63, 97.82, 97.53, 97.53)]
        m5 = [(97.53, 97.64, 97.48, 97.55)] * 7 + [(97.63, 97.82, 97.53, 97.53)]
        snap, cfg = wave_snapshot(97.53, m15, m5)
        regime = direction.market_regime(snap, cfg.tick, engine)
        zones = market_wave.zones(snap, cfg, engine, regime)
        micro = next(z for z in zones if z['kind'] == 'SWEEP_REVERSAL')
        self.assertFalse(market_wave.reversal_override(-1, micro, snap, cfg.tick))
        self.assertEqual(direction.regime_admission(-1, 97.7, regime, snap, cfg.tick, engine, micro),
                         (False, 'MACRO_REVERSAL_REQUIRED'))
        h1 = [engine.Candle(c.ts, 97.44, 97.56, 97.30, 97.48) for c in snap.candles['1H']]
        h1[-6] = replace(h1[-6], high=97.70)
        valid = replace(snap, candles={**snap.candles, '1H': h1})
        macro = next(z for z in market_wave.zones(valid, cfg, engine, regime)
                     if z['kind'] == 'SWEEP_REVERSAL' and z['sweep']['macro_timeframe'] == '1H')
        self.assertTrue(market_wave.reversal_override(-1, macro, valid, cfg.tick))
        self.assertLessEqual(macro['sweep']['reference_known_ts'], macro['sweep']['raid_ts'])
        self.assertTrue(direction.regime_admission(-1, 97.7, regime, valid, cfg.tick, engine, macro)[0])
        early = replace(valid, now=NOW - 1)
        self.assertFalse(market_wave.reversal_override(-1, macro, early, cfg.tick))

    def test_migration_preserves_open_trade_and_safely_cancels_waiting_records(self):
        snap, cfg, _ = swing_fixture()
        now = snap.now + 30_000
        snap = replace(snap, now=now)
        book = engine.new_state(cfg)
        active = {'id': 'old-filled', 'entry': 96., 'stop': 95.78, 'risk': .22, 'status': 'OPEN'}
        book['active'] = copy.deepcopy(active)
        old_live = {'id': '182', 'event_key': 'key-182', 'status': 'PENDING',
                    'side': 1, 'entry': 95.60, 'effective_ts': NOW - 60_000, 'expires_ts': NOW + 300_000}
        old_future = {**old_live, 'id': '183', 'event_key': 'key-183', 'effective_ts': NOW + 60_000}
        book['execution_step_ms'] = 60_000
        engine.park_pending(book, [old_live, old_future])
        engine.migrate_organic_pending(book, snap, cfg)
        engine.migrate_organic_pending(book, snap, cfg)
        self.assertEqual(book['active'], active)
        self.assertEqual([(p['id'], p['status']) for p in book['orders']], [('183', 'CANCELLED')])
        live, = engine.pending_plans(book)
        self.assertEqual(live['cancel_effective_ts'], NOW + 60_000)
        self.assertEqual([event['kind'] for event in book['events']], ['CANCEL_REQUESTED', 'CANCELLED'])
        boundary = NOW + 60_000
        safe_bar = engine.Candle(NOW, 96.2, 96.3, 96.1, 96.15)
        observed = replace(snap, now=boundary, candles={'1m': [safe_bar]})
        engine.reconcile_cancellations_at_closed_boundary(book, observed)
        self.assertEqual(len(engine.pending_plans(book)), 1)  # The last bar still needs replay.
        live['last_bar'] = NOW
        engine.reconcile_cancellations_at_closed_boundary(book, observed)
        self.assertFalse(engine.pending_plans(book))
        self.assertEqual(book['orders'][-1]['status'], 'CANCELLED')

    def test_breakeven_waits_for_partial_and_closed_m5_under_organic_rr(self):
        position = {'id': 'be', 'side': 1, 'entry': 96., 'stop': 94.5, 'tp1': 97.5, 'risk': 1.5,
                    'filled_fraction': 1., 'remaining': .5, 'tp1_hit': True,
                    'organic_risk': {'model': oil_risk.MODEL},
                    'realized_legs': [{'target': 'TP1', 'ts': NOW, 'fraction': .5}]}
        self.assertEqual(bzu_policy.partial_tp1_time(position), NOW)
        self.assertIsNone(bzu_policy.partial_tp1_time({**position, 'remaining': 1.}))
        self.assertIsNone(bzu_policy.partial_tp1_time({**position, 'organic_risk': None}))
        self.assertIsNone(bzu_policy.protective_amendment(position))
