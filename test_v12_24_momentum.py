"""Release regressions for the closed M15 momentum and execution paths."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import setup_scanner as scanner
import signal_quality as quality
import strategy_organics as organic
from strategy_direct_entries import momentum_quote


def utc(day, hour, minute=0):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


NOW = utc(5, 12)
STEP = engine.TF['15m']


def fixture(side=1, pivot=74.50, quote_offset=None, staircase=False, now=NOW):
    """An actual closed body break of a pivot known three bars earlier."""
    shift = pivot - 74.50
    quote = (74.78 + shift if quote_offset is None else pivot + quote_offset)

    def bar(ts, o, h, l, c, volume=100.):
        if side == 1:
            return engine.Candle(ts, o + shift, h + shift, l + shift, c + shift, volume)
        return engine.Candle(ts, 150 - o - shift, 150 - l - shift,
                             150 - h - shift, 150 - c - shift, volume)

    rows = [bar(now - (80 - i) * STEP, 74.33, 74.40, 74.28, 74.35) for i in range(80)]
    rows[-5] = bar(now - 5 * STEP, 74.36, 74.50, 74.33, 74.43)
    rows[-1] = bar(now - STEP, 74.38, 74.82, 74.34, 74.78)
    if staircase:
        rows[-2] = bar(now - 2 * STEP, 74.35, 74.59, 74.32, 74.57)
        rows[-1] = bar(now - STEP, 74.56, 74.86, 74.53, 74.78)
    candles = {'15m': rows}
    for tf in ('1H', '4H'):
        step = engine.TF[tf]
        end = now // step * step
        candles[tf] = [bar(end - (64 - i) * step, 78 - i * .06, 78.08 - i * .06,
                           77.87 - i * .06, 77.94 - i * .06) for i in range(64)]
    candles['3m'] = [bar(now - (64 - i) * 180_000, 74.75, 74.81, 74.70, 74.78)
                     for i in range(64)]
    candles['1m'] = [bar(now - (40 - i) * 60_000, 74.75, 74.81, 74.70, 74.78)
                     for i in range(40)]
    price = quote if side == 1 else 150 - quote
    snapshot = engine.Snapshot(now, price, candles, round(price - .01, 4), round(price + .01, 4),
                               True, now, 'BZ-USDT-SWAP')
    cfg = engine.Config(core=core, tick=.01, include_commission=True, slippage_bps=0.)
    with engine.market_clock(core, now):
        ctx = engine.build_full_context(snapshot, engine.new_state(cfg), cfg)
    return snapshot, cfg, ctx


def anchor_for(snapshot, cfg, ctx, family='BREAKER_FLIP_15M'):
    return next(a for a in scanner.organic_anchors(snapshot, cfg, ctx, engine)
                if a.evidence['source_strategy'] == family)


def target_for(side):
    return [{'level': 77. if side == 1 else 73., 'kind': 'TEST_STRUCTURAL_TARGET', 'distance': 2.5}]


class MomentumPaths(unittest.TestCase):
    def test_breaker_limits_and_override_reach_full_portfolio(self):
        for side in (1, -1):
            with self.subTest(side=side):
                snap, cfg, ctx = fixture(side)
                anchor = anchor_for(snap, cfg, ctx)
                self.assertTrue(anchor.evidence['origin_broke_structure'])
                self.assertTrue(anchor.evidence['organic_confluence'])
                self.assertEqual(anchor.evidence['zone_freshness'], 'UNMITIGATED')
                regime = direction.market_regime(snap, cfg.tick, engine)
                self.assertEqual(regime['side_4h'], -side)
                self.assertEqual(regime['state'], 'BULLISH_ACTIVE' if side == 1 else 'BEARISH_ACTIVE')
                state = engine.new_state(cfg)
                with patch.object(engine, 'full_targets', return_value=target_for(side)):
                    result = engine.run_cycle(state, snap, cfg)
                self.assertEqual(result['action'], 'PLACE_LIMIT', result['analysis'].get('rejections'))
                plans = [p for p in engine.pending_plans(state)
                         if p.get('source_strategy') == 'BREAKER_FLIP_15M']
                self.assertEqual(len(plans), 1)
                plan = plans[0]
                self.assertAlmostEqual(plan['entry'], 74.52 if side == 1 else 75.48)
                self.assertGreaterEqual(plan['net_rr'], 1.8)
                self.assertEqual(plan['confluence']['route'], 'ORGANIC_TREND')
                self.assertEqual(plan['confluence']['threshold'], 60)

    def test_staircase_without_sweep_and_proximal_depth(self):
        for side in (1, -1):
            snap, cfg, ctx = fixture(side, staircase=True, quote_offset=.46)
            a = anchor_for(snap, cfg, ctx, 'MOMENTUM_STAIRCASE_15M')
            zone = a.evidence['entry_zone']
            proximal = zone['high'] if side == 1 else zone['low']
            with patch.object(engine, 'full_targets', return_value=target_for(side)):
                plan, reason = engine.full_plan(a, ctx, snap, cfg, {})
            self.assertIsNotNone(plan, reason)
            self.assertAlmostEqual(plan['entry'], engine.round_tick(
                round(proximal + side * .03, 10), cfg.tick, up=side == 1))
            with patch.object(quality, 'volume_footprint', return_value={'bonus': 0}), \
                 patch('ict_methodology.session_sweeps', return_value=[]):
                result = quality.confluence(plan, snap, {}, cfg, engine)
            self.assertEqual(result['score'], 75)
            self.assertTrue(result['accepted'])

    def test_open_break_and_wick_only_break_do_not_enable_override(self):
        snap, cfg, ctx = fixture()
        event = snap.candles['15m'][-1]
        for changed in (replace(event, confirmed=False), replace(event, close=74.49)):
            snap.candles['15m'][-1] = changed
            self.assertFalse(direction.active_structure(snap, cfg.tick, engine))
            self.assertFalse(organic.formations(snap, cfg.tick, engine))
        snap.candles['15m'][-1] = event
        zone = anchor_for(snap, cfg, ctx).evidence['entry_zone']
        zone['high'] += .05
        self.assertFalse(scanner.zone_origin(zone, snap, 1, cfg.tick, engine)['confirmed'])

    def test_break_invalidated_after_close_back_through_level(self):
        snap, cfg, _ = fixture()
        snap.now += STEP
        snap.candles['15m'].append(engine.Candle(NOW, 74.76, 74.80, 74.39, 74.46, 100.))
        self.assertFalse(direction.active_structure(snap, cfg.tick, engine))

    def test_current_quote_direct_uses_taker_costs_and_035r_cap(self):
        for side in (1, -1):
            snap, cfg, ctx = fixture(side, pivot=74.19, quote_offset=.08)
            a = anchor_for(snap, cfg, ctx)
            with patch.object(engine, 'full_targets', return_value=target_for(side)):
                plan, reason = engine.full_plan(a, ctx, snap, cfg, {})
                self.assertIsNotNone(plan, reason)
                self.assertEqual(plan['action_type'], 'ACTIVE_ENTRY')
                self.assertEqual(plan['entry'], snap.ask if side == 1 else snap.bid)
                self.assertGreater(plan['direct_entry']['move_r'], .25)
                self.assertLessEqual(plan['direct_entry']['move_r'], .35)
                self.assertEqual(plan['entry_fee_rate'], cfg.taker_fee)
                self.assertGreaterEqual(plan['net_rr'], 1.8)
                state = engine.new_state(cfg)
                cycle = engine.run_cycle(state, snap, cfg)
                self.assertEqual(cycle['action'], 'ACTIVE_ENTRY', cycle['analysis'].get('rejections'))
                self.assertEqual(engine.active_plans(state)[0]['filled_ts'], snap.now)
            # A one-tick later quote must not be chased.
            snap.price += side * .04
            snap.bid += side * .04
            snap.ask += side * .04
            with patch.object(engine, 'full_targets', return_value=target_for(side)):
                plan, reason = engine.full_plan(a, ctx, snap, cfg, {})
            self.assertIsNotNone(plan, reason)
            self.assertEqual(plan['action_type'], 'NEW_PENDING_LIMIT')

    def test_quote_rr_failure_keeps_valid_passive_breaker(self):
        snap, cfg, ctx = fixture(pivot=74.50, quote_offset=.07)
        a = anchor_for(snap, cfg, ctx)
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            plan, reason = engine.full_plan(a, ctx, snap, cfg, {})
        self.assertIsNotNone(plan, reason)
        self.assertEqual(plan['action_type'], 'NEW_PENDING_LIMIT')
        self.assertAlmostEqual(plan['entry'], 74.52)

    def test_reversal_sweep_reaches_admission_without_bos_or_fvg(self):
        for side in (1, -1):
            snap, cfg, _ = fixture(side)

            def candle(ts, o, h, l, c):
                if side == 1:
                    return engine.Candle(ts, o, h, l, c, 100.)
                return engine.Candle(ts, 150-o, 150-l, 150-h, 150-c, 100.)

            snap.candles['15m'] = [candle(c.ts, 74.56, 74.63, 74.37, 74.58)
                                   for c in snap.candles['15m']]
            snap.candles['15m'][-30] = candle(NOW-30*STEP, 74.56, 74.90, 74.37, 74.58)
            snap.candles['15m'][-1] = candle(NOW-STEP, 74.41, 74.61, 74.27, 74.53)
            for tf in ('1H', '4H'):
                snap.candles[tf] = [candle(c.ts, 74.52, 74.90, 74.37, 74.52)
                                    for c in snap.candles[tf]]
            snap.price = 74.54 if side == 1 else 75.46
            snap.bid, snap.ask = snap.price-.01, snap.price+.01
            state = engine.new_state(cfg)
            level, extreme = (74.37, 74.27) if side == 1 else (75.63, 75.73)
            setup = 'SWEEP_RECLAIM' if side == 1 else 'LIQUIDITY_SWEEP_REVERSAL_SHORT'
            with engine.market_clock(core, NOW):
                ctx = engine.build_full_context(snap, state, cfg)
                anchor = core.make_anchor(setup, 'LONG' if side == 1 else 'SHORT',
                    level, extreme, 'TEST_SWEEP', 'closed sweep', ctx, evidence={
                        'setup_timeframe': '15m', 'swept_level': level,
                        'sweep_extreme': extreme, 'wick_ratio': .5})
            with patch.object(engine, 'merge_full_anchors', return_value=[anchor]), \
                 patch.object(engine, 'full_targets', return_value=target_for(side)):
                result = engine.analyze(snap, cfg, state)
            self.assertEqual(len(result['plans']), 1, result['rejections'])
            plan = result['plans'][0]
            self.assertFalse(plan['origin_broke_structure'])
            self.assertEqual(plan['entry_zone']['kind'], 'SWEEP_RECLAIM_ZONE')
            self.assertEqual(plan['confluence']['route'], 'REVERSAL_SWEEP')
            self.assertEqual(plan['score'], 70)

    def test_score_paths_and_volume_boundaries(self):
        snap, cfg, ctx = fixture()
        a = anchor_for(snap, cfg, ctx)
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            plan, reason = engine.full_plan(a, ctx, snap, cfg, {})
        self.assertIsNotNone(plan, reason)
        with patch('ict_methodology.session_sweeps', return_value=[]):
            for volume, score in ((0., 75), (69., 75), (129., 75), (130., 85)):
                snap.candles['15m'][-1] = replace(snap.candles['15m'][-1], volume=volume)
                value = quality.confluence(plan, snap, {}, cfg, engine)
                self.assertEqual(value['score'], score)
                self.assertTrue(value['accepted'])
                self.assertEqual(value['volume_footprint']['penalty'], 0)
            snap.candles['15m'][-1] = replace(snap.candles['15m'][-1], volume=0.)
            reverse = {**plan, 'organic_confluence': False, 'm15_momentum_aligned': False}
            value = quality.confluence(reverse, snap, {'sweep_reclaim': True}, cfg, engine)
            self.assertEqual(value['score'], 70)
            self.assertEqual(value['route'], 'REVERSAL_SWEEP')
            self.assertTrue(value['accepted'])

    def test_favorable_displacement_clears_old_brake_both_sides(self):
        for side in (1, -1):
            snap, cfg, _ = fixture(side)
            bars = [engine.Candle(NOW - 2 * STEP, 75, 75.01, 74.18, 74.2),
                    engine.Candle(NOW - STEP, 74.24, 75.02, 74.21, 75.)]
            if side == -1:
                bars = [engine.Candle(c.ts, 150-c.open, 150-c.low, 150-c.high, 150-c.close)
                        for c in bars]
            zone = {'low': 74.40, 'high': 74.50} if side == 1 else {'low': 75.50, 'high': 75.60}
            snap.candles['15m'] = bars[:1]
            snap.now -= STEP
            self.assertTrue(adverse.approach(snap, .3, side, zone, cfg.tick)['blocked'])
            snap.candles['15m'] = bars
            snap.now += STEP
            verdict = adverse.approach(snap, .3, side, zone, cfg.tick)
            self.assertFalse(verdict['blocked'])
            self.assertFalse(verdict['fast'])

    def test_utc_boundaries_eia_and_previous_kyiv_cutoff(self):
        for hour, minute, allowed in ((0, 59, False), (1, 0, True),
                                      (21, 45, True), (22, 0, False)):
            self.assertEqual(bzu_policy.session_gate(utc(5, hour, minute))['allowed'], allowed)
        self.assertEqual(bzu_policy.session_gate(utc(4, 15, 30))['reason'], 'EIA_INVENTORY_WINDOW')
        self.assertTrue(bzu_policy.session_gate(utc(4, 15, 45))['allowed'])
        now = utc(5, 21, 0)
        snap, cfg, _ = fixture(now=now)
        self.assertLess(engine.session_cutoff(now, cfg), now)
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            state = engine.new_state(cfg)
            cycle = engine.run_cycle(state, snap, cfg)
        self.assertEqual(cycle['action'], 'PLACE_LIMIT')
        self.assertLessEqual(engine.pending_plans(state)[0]['expires_ts'], utc(5, 22, 0))

    def test_protected_exit_and_existing_policy_recognition(self):
        self.assertTrue(bzu_policy.is_precision({'policy': bzu_policy.MASTER_POLICY}))
        self.assertEqual(bzu_policy.round_target(74.20, 75.03, 1, .01), 74.97)
        self.assertEqual(bzu_policy.round_stop(73.99, 1, .01), 73.94)
        old = engine.new_state(engine.Config(core=core))
        old['quality'] = {'schema': quality.SCHEMA, 'strategy': 'bzu-master-rvol-vwap-round-5m-v12',
                          'observations': [{'id': 'old'}]}
        quality.ensure(old, engine.Config(core=core))
        self.assertEqual(old['quality_archives'][0]['observations'], [{'id': 'old'}])

    def test_zone_bounce_direct_requires_closed_fresh_reaction_and_quote(self):
        snap, cfg, _ = fixture(quote_offset=.08)
        zone = {'low': 74.48, 'high': 74.50, 'known_ts': NOW - STEP}
        reaction = engine.Candle(NOW - STEP, 74.49, 74.61, 74.48, 74.59)
        snap.candles['15m'][-1] = reaction
        direct = momentum_quote(snap, 1, 74.53, 74.33, zone, {}, cfg.tick)
        self.assertEqual(direct['pattern'], 'ZONE_REACTION')
        self.assertAlmostEqual(direct['move_r'], .30)
        snap.candles['15m'][-1] = replace(reaction, confirmed=False)
        self.assertIsNone(momentum_quote(snap, 1, 74.53, 74.33, zone, {}, cfg.tick))
        snap.candles['15m'][-1] = reaction
        snap.ticker_ts -= 121_000
        self.assertIsNone(momentum_quote(snap, 1, 74.53, 74.33, zone, {}, cfg.tick))

    def test_v23_pending_prices_survive_score_migration(self):
        snap, cfg, ctx = fixture()
        a = anchor_for(snap, cfg, ctx)
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            plan, why = engine.full_plan(a, ctx, snap, cfg, {})
        self.assertIsNotNone(plan, why)
        old_version = 'full-ict-v12.23.0-master-plan'
        state = engine.new_state(replace(cfg, min_score=65))
        state.update(version=old_version, last_run_ts=NOW - STEP)
        plan.update(policy=bzu_policy.MASTER_POLICY, version=old_version,
                    placed_ts=NOW - STEP, effective_ts=NOW - STEP)
        engine.park_pending(state, [plan])
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            engine.run_cycle(state, snap, cfg)
        old = next(p for p in engine.pending_plans(state) if p['id'] == plan['id'])
        for field in ('entry', 'stop', 'tp1', 'tp2', 'expires_ts', 'exit_policy'):
            self.assertEqual(old[field], plan[field])
        self.assertEqual(state['runtime_config']['min_score'], 60)

    def test_workflow_keeps_synchronized_15m_reports(self):
        workflow = Path('.github/workflows/main.yml').read_text()
        self.assertEqual(workflow, Path('WORKFLOW_main.yml').read_text())
        self.assertIn("cron: '*/15 * * * *'", workflow)
        self.assertIn("MIN_SETUP_SCORE: '60'", workflow)
        self.assertIn(engine.VERSION, workflow)
        self.assertEqual(core.BOT_VERSION, engine.VERSION)


if __name__ == '__main__':
    unittest.main()
