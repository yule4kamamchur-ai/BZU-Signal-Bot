"""Regression checks for the institutional continuation and observation tiers."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import full_limit_engine as engine
import live_terminal
import setup_scanner as scanner
import strategy_organics as organic
from test_v12_24_momentum import NOW, anchor_for, fixture
from test_v12_28_quant import target_for


class InstitutionalPlans(unittest.TestCase):
    def test_inside_wide_zone_is_scored_and_prices_are_mirrored(self):
        for side in (1, -1):
            with self.subTest(side=side):
                snap, cfg, _ = fixture()
                mirror = lambda price: price if side == 1 else 200 - price
                snap.price = mirror(99.51)
                low, high = sorted((mirror(99.16), mirror(99.96)))
                zone = {'kind':'FVG_BULL' if side == 1 else 'FVG_BEAR','side':side,
                        'timeframe':'15m','known_ts':NOW,'low':low,'high':high}
                levels = [{'level':mirror(level),'kind':'SWING'} for level in (100.06,100.46)]
                with patch.object(engine, 'atr', return_value=.40):
                    result = scanner.calculate_zone_trade_plan(zone, snap, cfg, engine, side, targets=levels)
                self.assertTrue(result['in_zone'])
                self.assertEqual(result['entry_condition'], 'IN_ZONE_M5_RECLAIM')
                self.assertEqual(result['entry_basis'], 'DISTAL_35')
                self.assertAlmostEqual(result['entry'], mirror(99.44))
                self.assertAlmostEqual(result['stop'], mirror(99.06))
                self.assertGreaterEqual(result['score'], 60)
                self.assertEqual(result['tactic'], 'REACTION_M5')

    def test_near_distal_edge_uses_five_cent_passive_quote_offset(self):
        _, cfg, _ = fixture()
        entry, stop, risk, basis, wide, inside = scanner.zone_entry_geometry(
            99.16, 99.96, 99.30, 99.16, 1, .40, cfg, engine)
        self.assertEqual(basis, 'INSIDE_NEAR_QUOTE')
        self.assertAlmostEqual(entry, 99.25)
        self.assertGreaterEqual(risk, .22 - 1e-8)
        self.assertLessEqual(stop, 99.06)
        self.assertTrue(inside and wide)

    def test_wide_zone_slicing_and_structurally_too_wide_stop(self):
        snap, cfg, _ = fixture()
        entry, stop, risk, basis, *_ = scanner.zone_entry_geometry(
            95.67, 96.46, 99.51, 95.67, 1, .40, cfg, engine)
        self.assertEqual(basis, 'EQUILIBRIUM')
        self.assertAlmostEqual(entry, 96.07)
        self.assertLess(risk, .65)
        _, stop, risk, basis, *_ = scanner.zone_entry_geometry(
            95., 98., 99.51, 95., 1, .40, cfg, engine)
        self.assertEqual(basis, 'DISTAL_35')
        self.assertGreater(risk, .65)
        self.assertLessEqual(stop, 94.90)

    def test_near_old_pool_does_not_mask_real_farther_targets_or_pass_hard_wall(self):
        _, cfg, _ = fixture()
        levels = [{'level':level,'kind':'SWING'} for level in (97.46,98.05,98.35)]
        plan = scanner.zone_targets(96.92, .40, 1, levels, NOW, cfg.tick, engine, quote=99.51)
        self.assertEqual(plan['tp1'], 98.04)
        self.assertTrue(plan['structural_tp1'] and plan['structural_tp2'])
        reference = scanner.zone_targets(96.92, .40, 1, levels[:1], NOW, cfg.tick, engine, quote=99.51)
        self.assertEqual(reference['tp1_source'], 'QUOTE_REFERENCE')
        self.assertFalse(reference['structural_tp1'])
        levels[0]['hard_barrier'] = True
        blocked = scanner.zone_targets(96.92, .40, 1, levels, NOW, cfg.tick, engine, quote=99.51)
        self.assertFalse(blocked['path_clear'])

    def test_structural_pool_includes_recent_closed_m15_and_h1_extremes(self):
        snap, cfg, _ = fixture()
        levels = engine.full_targets({'analysis_timeframe':'4H'}, snap, 'LONG', 73., cfg)
        for tf in ('15m','1H'):
            step = engine.TF[tf]
            high = max(c.high for c in snap.candles[tf] if c.ts + step <= snap.now and c.confirmed
                       and c.ts >= snap.candles[tf][-48].ts)
            self.assertTrue(any(abs(target['level'] - high) < 1e-8 for target in levels))

    def test_ote_and_m15_breaker_use_same_completed_impulse(self):
        for side in (1, -1):
            snap, cfg, _ = fixture(side)
            formed = {z['family']:z for z in organic.formations(snap, cfg.tick, engine)}
            ote = formed['IMPULSE_OTE_15M']
            self.assertEqual(ote['signal_ts'], formed['BREAKER_FLIP_15M']['signal_ts'])
            self.assertEqual(ote['side'], side)
            latest = snap.candles['15m'][-1]
            tip = latest.high if side == 1 else latest.low
            wave = side * (tip - ote['invalidation'])
            levels = sorted((tip - side * .618 * wave, tip - side * .705 * wave))
            self.assertAlmostEqual(ote['entry_zone']['low'], levels[0])
            self.assertAlmostEqual(ote['entry_zone']['high'], levels[1])

    def test_m5_breaker_requires_closed_contiguous_bars_inside_native_zone(self):
        snap, cfg, _ = fixture()
        base = NOW - 4 * 300_000
        rows = [engine.Candle(base+i*300_000,99.3,99.45,99.2,99.4) for i in range(3)]
        rows.append(engine.Candle(base+3*300_000,99.4,99.7,99.39,99.65))
        zone = {'side':1,'low':99.16,'high':99.96,'known_ts':base-900_000}
        snap = replace(snap,candles={'5m':rows})
        self.assertEqual(scanner.m5_breaker(zone,snap,cfg.tick)['level'],99.45)
        rows[-1] = replace(rows[-1], confirmed=False)
        self.assertIsNone(scanner.m5_breaker(zone,snap,cfg.tick))

    def test_inside_zone_reaches_final_reaction_route_without_pending_order(self):
        snap, cfg, context = fixture()
        anchor = anchor_for(snap, cfg, context)
        snap.price, snap.bid, snap.ask = 74.60, 74.59, 74.61
        zone = {'kind':'FVG_BULL','timeframe':'15m','low':74.40,'high':74.70,'known_ts':NOW}
        origin = {'confirmed':True,'displacement_known_ts':NOW,'break_known_ts':NOW}
        with patch.object(engine.bzu_policy,'zone_for',return_value=zone), patch.object(
                scanner,'zone_origin',return_value=origin), patch.object(engine,'full_targets',return_value=target_for(1)):
            plan, reason = engine.full_plan(anchor, context, snap, cfg, {})
            self.assertIsNotNone(plan, reason)
            self.assertTrue(plan['reaction_only'])
            plan.update(score=80, quality={'accepted':True,'features':{},'reason':'ACCEPTED'})
            result = {'plans':[plan],'health':[],'full_context':context,'contexts':{}}
            book = engine.new_state(cfg)
            with patch.object(engine,'analyze',return_value=result):
                out = engine.run_cycle(book,snap,cfg)
        self.assertEqual(out['action'],'WATCH_REACTION')
        self.assertFalse(engine.pending_plans(book))
        self.assertEqual(out['analysis']['reaction_plans'][0]['id'],plan['id'])

    def test_reference_radar_has_numbers_reserve_and_opposite_without_arming(self):
        snap, cfg, _ = fixture()
        snap.price = 99.51
        zones = [SimpleNamespace(kind=kind, side=side, timeframe='15m', low=low, high=high,
                                 created_ts=NOW-1_800_000, mitigated=False)
                 for kind, side, low, high in [('FVG_BULL','LONG',99.16,99.96),
                                                ('DEMAND_OB','LONG',98.65,98.85),
                                                ('FVG_BEAR','SHORT',100.,100.35)]]
        analysis = {'full_context':{'zones':{'fvg':zones}}}
        with patch.object(engine.policy,'market_regime',return_value={'state':'BULLISH_ACTIVE'}),                 patch.object(engine,'atr',return_value=.4):
            out = live_terminal.build(engine.new_state(cfg),snap,{'analysis':analysis},cfg,engine)
        self.assertFalse(out['candidates'])
        self.assertFalse(out['orders'])
        self.assertEqual(out['watch_primary_id'], out['watch_levels']['LONG'])
        self.assertTrue(out['watch_levels']['SHORT'] and out['reserve_levels']['LONG'])
        self.assertTrue(all(isinstance(z['score'], int) and z['entry'] and z['stop'] for z in out['zones']))
        self.assertTrue(all(isinstance(z['score'],int) for z in out['context_bands']))


if __name__ == '__main__':
    unittest.main()
