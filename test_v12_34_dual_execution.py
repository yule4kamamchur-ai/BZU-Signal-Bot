"""Causal trigger, price, and paper ledger contracts for dual execution."""
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import directional_policy as direction
import full_limit_engine as engine
import live_terminal
import market_wave as wave
import oil_risk
import setup_scanner as scanner
from test_v12_24_momentum import NOW, STEP
from test_v12_32_organic_oil import swing_fixture


class DualExecution(unittest.TestCase):
    def test_post_touch_requires_closed_evidence_and_bounded_chase(self):
        zone = {'kind': 'FVG_BEAR', 'low': 93.86, 'high': 94.14, 'known_ts': NOW - STEP}
        touch = engine.Candle(NOW - wave.M5, 93.82, 93.92, 93.75, 93.76)
        snap = engine.Snapshot(NOW, 93.76, {'5m': [touch]}, 93.76, 93.78, ticker_ts=NOW)

        def trigger(s=snap, z=zone, quote=93.76):
            return scanner.trend_market_trigger(s, z, -1, quote, 1.35, .01, engine)

        self.assertEqual(trigger()['pattern'], 'POST_TOUCH_REJECTION')
        self.assertEqual(trigger(quote=93.84)['pattern'], 'IN_ZONE_TOUCH')
        self.assertIsNone(trigger(quote=93.24))
        self.assertIsNone(trigger(z={**zone, 'known_ts': touch.ts + 1}))
        self.assertIsNone(trigger(s=replace(snap, ticker_ts=NOW - 1)))
        self.assertIsNone(trigger(s=replace(snap, candles={'5m': [replace(touch, confirmed=False)]})))
        self.assertIsNone(trigger(s=replace(snap, now=NOW + 6 * wave.M5, ticker_ts=NOW + 6 * wave.M5)))
        self.assertIsNone(trigger(s=replace(snap, trusted=False)))
        self.assertIsNone(trigger(s=replace(snap, candles={'5m': [replace(touch, low=94.25, high=94.35)]})))

    def test_breakout_requires_displacement_and_a_higher_score(self):
        bars = [engine.Candle(NOW - (5 - i) * wave.M5, 93.98, 94.01, 93.95, 93.99, 100)
                for i in range(4)]
        breakout = engine.Candle(NOW - wave.M5, 94.01, 94.01, 93.68, 93.72, 150)
        zone = {'kind': 'FLAG_RETEST', 'low': 93.86, 'high': 94.02, 'known_ts': NOW,
                'wave_proof': {'timeframe': '5m'}}
        snap = engine.Snapshot(NOW, 93.75, {'5m': [*bars, breakout]}, 93.75, 93.77, ticker_ts=NOW)
        trigger = scanner.trend_market_trigger(snap, zone, -1, snap.bid, 1.50, .01, engine)
        self.assertEqual(trigger['pattern'], 'BREAKOUT_CONTINUATION')
        self.assertEqual(scanner.min_plan_score('WITH_TREND', trigger), 60)
        self.assertEqual(scanner.min_plan_score('WITH_TREND'), 52)
        snap = replace(snap, candles={'5m': [*bars, replace(breakout, close=93.95)]})
        self.assertIsNone(scanner.trend_market_trigger(snap, zone, -1, snap.bid, 1.50, .01, engine))

    def test_organic_projection_skips_close_liquidity_and_keeps_fresh_barriers(self):
        snap, cfg, _ = swing_fixture()
        near = {'level': 92.81, 'timeframe': '1H', 'kind': '1H_LIQUIDITY', 'known_ts': NOW - STEP}
        plan = scanner.organic_targets(93.86, 1.35, -1, [near], snap, cfg.tick, engine, trend=True)
        self.assertEqual(plan['checkpoints'], [92.82])
        self.assertLessEqual(plan['tp1'], 93.86 - 1.10 * 1.35)
        self.assertLessEqual(plan['tp2'], 93.86 - 1.90 * 1.35)
        self.assertTrue(plan['path_clear'])
        barrier = {**near, 'hard_barrier': True}
        fresh = scanner.organic_targets(93.86, 1.35, -1, [barrier], snap, cfg.tick, engine, trend=True)
        self.assertFalse(fresh['path_clear'])
        profile = oil_risk.snapshot_profile(snap)
        stop, risk, details = oil_risk.stop_geometry(93.76, 94.14, -1, profile, [], .01, engine, allow_local=True)
        self.assertEqual(details['anchor_source'], 'CONFIRMED_ZONE')
        self.assertIsNone(details['gate'])
        self.assertGreaterEqual(stop - 94.14, details['buffer'])
        self.assertGreaterEqual(risk, details['floor'])

    def test_engine_persists_market_at_ask_and_recovers_projection_once(self):
        snap, cfg, targets = swing_fixture()
        snap = replace(snap, price=96.02, bid=96.01, ask=96.03)
        regime = {**direction.market_regime(snap, cfg.tick, engine), 'side_1h': 1, 'side_4h': 0}
        book = engine.new_state(cfg)
        with patch.object(direction, 'market_regime', return_value=regime),              patch.object(engine, 'full_targets', return_value=targets),              patch.dict(os.environ, {'BZU_ENTRY_MODE': 'HYBRID', 'BZU_ENTRY_PLAYBOOK_MODE': 'STRICT'}):
            result = engine.run_cycle(book, snap, cfg)
            self.assertEqual(result['action'], 'ACTIVE_ENTRY')
            order, = engine.active_plans(book)
            self.assertEqual((order['status'], order['order_type'], order['admission_status']),
                             ('OPEN', 'MARKET', 'ADMITTED_LIVE'))
            self.assertEqual((order['entry'], order['filled_ts']), (snap.ask, snap.now))
            self.assertEqual(order['entry_fee_rate'], engine.fee_rate(cfg, 'taker'))
            self.assertAlmostEqual(order['rr1'], (order['tp1'] - snap.ask) / order['risk'])
            self.assertFalse(engine.pending_plans(book))
            with TemporaryDirectory() as tmp:
                files = {name: Path(tmp) / name for name in ('state_path', 'journal_path', 'live_path')}
                live_terminal.sync_active_signal_to_journal(book, snap, result, cfg, engine, **files)
                public, = engine.read_json(files['live_path'])['orders']
                journal, = engine.read_json(files['journal_path'])['full_v12']['open_plans']
                for key in ('id', 'status', 'entry', 'stop', 'tp1', 'tp2', 'order_type'):
                    self.assertEqual(public[key], journal[key])
                files['live_path'].unlink()
                duplicate = engine.run_cycle(book, snap, cfg)
                self.assertEqual(duplicate['action'], 'DUPLICATE_OR_OUT_OF_ORDER')
                live_terminal.sync_active_signal_to_journal(book, snap, duplicate, cfg, engine, **files)
                self.assertEqual(engine.read_json(files['live_path'])['orders'], [public])
                events = engine.read_json(files['journal_path'])['full_v12']['events']
                self.assertEqual(len([event for event in events if event['kind'] == 'FILLED']), 1)
            next_snap = replace(snap, now=snap.now + 1000, ticker_ts=snap.now + 1000)
            engine.run_cycle(book, next_snap, cfg)
            self.assertEqual([p['id'] for p in engine.open_plans(book)], [order['id']])
            with patch.dict(os.environ, {'BZU_ENTRY_MODE': 'LIVE_REACTION'}):
                watch = engine.new_state(cfg)
                engine.run_cycle(watch, snap, cfg)
                self.assertFalse(engine.open_plans(watch))
