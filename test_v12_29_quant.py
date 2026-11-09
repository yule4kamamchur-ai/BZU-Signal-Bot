"""Regressions for v12.29 native-zone admission and target geometry."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import full_limit_engine as engine
import live_terminal
import setup_scanner as scanner
from test_v12_24_momentum import anchor_for, fixture
from test_v12_28_quant import target_for, structural_witnesses


class QuantAdmissionTests(unittest.TestCase):
    def test_four_cent_zone_gets_normalized_stop_on_both_sides(self):
        for side in (1, -1):
            with self.subTest(side=side):
                snap, cfg, _ = fixture(side)
                entry = snap.price - side * .50
                zone = {'kind': 'FVG_BULL' if side == 1 else 'FVG_BEAR',
                        'side': side, 'timeframe': '15m', 'known_ts': snap.now,
                        'low': entry - .02, 'high': entry + .02}
                with patch.object(engine, 'atr', return_value=.60):
                    plan = scanner.calculate_zone_trade_plan(
                        zone, snap, cfg, engine, side, targets=[
                            {'level': entry + side * .56, 'kind':'SWING'},
                            {'level': entry + side * .70, 'kind':'SWING'}])
                self.assertAlmostEqual(plan['risk'], .24)
                self.assertGreater(side * (entry - plan['stop']), .02)
                self.assertIsNone(plan['risk_gate'])

    def test_clip_never_authorizes_stop_inside_structural_swing(self):
        for side in (1, -1):
            _, cfg, _ = fixture(side)
            stop, risk, safe = scanner.normalized_zone_stop(
                98., 98. - side * .80, side, .3, cfg, engine)
            self.assertAlmostEqual(risk, .65)
            self.assertAlmostEqual(stop, 98. - side * .65)
            self.assertFalse(safe)

    def test_targets_skip_ineligible_pivots_and_use_distinct_tp2(self):
        for side in (1, -1):
            snap, cfg, _ = fixture(side)
            targets = [{'kind': f'LEVEL_{i}', 'level': 98. + side * distance}
                       for i, distance in enumerate((.05, .44, .60, .68))]
            plan = scanner.zone_targets(98., .30, side, targets, snap.now, cfg.tick, engine)
            self.assertAlmostEqual(plan['tp1'], 98. + side * .43)
            self.assertAlmostEqual(plan['tp2'], 98. + side * .67)
            self.assertTrue(plan['structural_tp2'])

    def test_micro_zones_are_rejected_before_setup_and_public_export(self):
        snap, cfg, ctx = fixture()
        anchor = anchor_for(snap, cfg, ctx)
        rows = []
        for kind in ('CONFIRMED_FVG', 'MICRO_BOS_FVG', 'DEMAND_OB'):
            zone = {**anchor.evidence['entry_zone'], 'timeframe': '3m', 'kind': kind}
            micro = replace(anchor, evidence={**anchor.evidence, 'entry_zone': zone})
            plan, reason = engine.full_plan(micro, ctx, snap, cfg, {})
            self.assertIsNone(plan)
            self.assertEqual(reason, 'MICRO_ZONE_EXCLUDED')
            rows.append({'id': kind, 'side': 1, 'score': 100, 'entry': anchor.level, 'entry_zone': zone})
        result = live_terminal.build(engine.new_state(cfg), snap,
            {'analysis': {'candidate_plans': rows, 'admitted_plans': rows}}, cfg, engine)
        self.assertFalse(result['zones'])
        self.assertFalse(result['candidates'])

    def test_session_mean_reclaim_cannot_create_limit(self):
        snap, cfg, ctx = fixture()
        anchor = replace(anchor_for(snap, cfg, ctx), setup_type='SESSION_MEAN_RECLAIM')
        with patch.object(engine, 'full_targets', return_value=target_for(1)):
            plan, reason = engine.full_plan(anchor, ctx, snap, cfg, {})
        self.assertIsNone(plan)
        self.assertEqual(reason, 'SESSION_MEAN_LIMIT_DISABLED')

    def test_range_proximity_is_measured_to_both_zone_edges(self):
        snap, cfg, _ = fixture()
        for side in (1, -1):
            edges = sorted((snap.price - side * .04, snap.price - side * .09))
            zone = {'kind': 'FVG_BULL' if side == 1 else 'FVG_BEAR',
                    'side': side, 'timeframe': '15m', 'known_ts': snap.now,
                    'low': edges[0], 'high': edges[1]}
            self.assertEqual(scanner.standalone_zone_error(zone, snap, cfg, engine, .30, 'RANGE'),
                             'RANGE_ZONE_TOO_CLOSE')
            self.assertIsNone(scanner.standalone_zone_error(zone, snap, cfg, engine, .30, 'TREND'))

    def test_closer_blocked_zone_cannot_outrank_admitted_plan(self):
        snap, cfg, _ = fixture()
        snap.candles['1H'][-1] = replace(snap.candles['1H'][-1], volume=200.)
        def plan(identity, distance):
            entry = snap.price - distance
            return structural_witnesses({'id': identity, 'side': 1, 'score': 85, 'entry': entry, 'stop': entry - .25,
                    'tp1': entry + .38, 'tp2': entry + .60,
                    'entry_zone': {'kind': 'FVG_BULL', 'timeframe': '1H', 'low': entry - .04,
                                   'high': entry + .04, 'known_ts': snap.now}}, cfg.tick)
        blocked = plan('same-id', .10)
        admitted = plan('same-id', .26)
        with patch.object(engine, 'atr', return_value=.25),              patch.object(engine, 'full_targets', return_value=[]),              patch.object(engine.policy, 'market_regime', return_value={'state': 'BULLISH_ACTIVE'}):
            out = live_terminal.build(engine.new_state(cfg), snap,
                {'analysis': {'candidate_plans': [blocked], 'admitted_plans': [admitted]}}, cfg, engine)
        self.assertEqual(len(out['candidates']), 1)
        self.assertAlmostEqual(out['radar']['LONG']['entry'], admitted['entry'])
        self.assertTrue(out['radar']['LONG']['actionable'])
        self.assertEqual(out['primary_candidate_id'], out['radar']['LONG']['id'])

    def test_context_bands_are_not_trade_plans(self):
        snap, cfg, _ = fixture()
        zones = [SimpleNamespace(kind='FVG_BULL', side='LONG', timeframe='1H',
                                 low=snap.price-.29, high=snap.price-.22,
                                 created_ts=snap.now-2*engine.TF['1H'], mitigated=False)]
        with patch.object(engine.policy, 'market_regime', return_value={'state': 'BULLISH_ACTIVE'}):
            out = live_terminal.build(engine.new_state(cfg), snap,
                {'analysis': {'full_context': {'zones': {'fvg': zones}}}}, cfg, engine)
        self.assertFalse(out['candidates'])
        self.assertEqual(len(out['context_bands']), 1)
        self.assertNotIn('entry', out['context_bands'][0])
        self.assertNotIn('stop', out['context_bands'][0])


if __name__ == '__main__':
    unittest.main()
