"""Causal v13 entry, scoring, position and display contracts."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import bzu_policy
import entry_adverse_selection as adverse
import full_limit_engine as engine
import ict_methodology
import notification_report
import scenario_planner
import signal_quality
from test_full_limit_engine import order
from test_v12_24_momentum import NOW, STEP
from test_v12_40_execution import base_fixture


class Intraday(unittest.TestCase):
    def test_five_minute_engulf_requires_a_closed_contiguous_pair_and_fresh_quote(self):
        snapshot, cfg = base_fixture()
        step = 300_000
        bars = [engine.Candle(NOW-2*step, 93.85, 93.86, 93.69, 93.71),
                engine.Candle(NOW-step, 93.70, 93.92, 93.68, 93.90)]
        snapshot = replace(snapshot, now=NOW, ticker_ts=NOW, price=93.90, trusted=True,
                           candles={**snapshot.candles, '1m': [], '5m': bars})
        zone = {'known_ts': NOW-3*step, 'low': 93.66, 'high': 93.81}
        with patch.object(engine, 'atr', return_value=.70):
            trigger = adverse.m5_reaction_market_trigger(snapshot, 1, zone, 93.80, .90, 93.91, cfg.tick, engine)
            self.assertEqual((trigger['pattern'], trigger['known_ts']), ('M5_ZONE_ENGULFING', NOW))
            self.assertIsNone(adverse.m5_reaction_market_trigger(
                snapshot, 1, zone, 93.80, .90, 94.20, cfg.tick, engine))
            before = replace(snapshot, now=NOW-1, ticker_ts=NOW-1)
            self.assertIsNone(adverse.m5_reaction_market_trigger(
                before, 1, zone, 93.80, .90, 93.91, cfg.tick, engine))

    def test_partial_and_protection_take_effect_without_reusing_the_same_bar(self):
        _, cfg = base_fixture()
        cfg = replace(cfg, include_commission=False, slippage_bps=0)
        plan = order(1, placed=NOW)
        plan.update(action_type='ACTIVE_ENTRY', policy=bzu_policy.POLICY, scale_policy=bzu_policy.SCALE_POLICY, tp1=101.45, tp2=101.90,
                    partials={'TP1': .50, 'TP2': .50},
                    entry_legs=[{'name':'L1', 'price':100, 'fraction':1., 'status':'PENDING'}])
        state = engine.new_state(cfg)
        minute = 60_000
        initial = engine.Candle(NOW, 100, 100.25, 99.90, 100.10)
        state['active'] = bzu_policy.first_fill(plan, initial, engine, cfg, minute)
        position = state['active']
        self.assertEqual(bzu_policy.partial_target(position), 101.)
        # This bar could have printed 100.05 before hitting +1R.
        target = engine.Candle(NOW+minute, 100.30, 101.05, 100.05, 100.70)
        bzu_policy.manage(state, target, cfg, engine, minute)
        self.assertTrue(position['tp1_hit'])
        self.assertAlmostEqual(position['realized_gross_r'], .5)
        self.assertEqual(position['remaining'], .5)
        self.assertEqual(position['stop'], 99.)
        self.assertEqual(position['pending_stop']['price'], 100.15)
        self.assertIn('50% на +1R', notification_report.targets(position))
        bzu_policy.apply_protective_stop(position, target.ts+minute)
        self.assertEqual(position['stop'], 100.15)

    def test_vwap_and_volume_bonus_at_125_percent_uses_one_score(self):
        snapshot, cfg = base_fixture()
        plan = {'institutional_family':'FVG_OB_PULLBACK', 'side':1, 'entry':93.69,
                'risk':.88, 'rr1':1.45, 'trend_rel':'WITH_TREND',
                'entry_zone':{'kind':'FVG_PULLBACK','low':93.50,'high':93.70},
                'zone_origin':{'confirmed':True, 'break_known_ts':NOW-STEP},
                'm15_momentum_aligned':True}
        with patch.object(engine.policy, 'session_vwap', return_value={'value':93.50}), \
             patch.object(engine.scanner, 'event_rvol', return_value={'rvol':1.25,'status':'AVAILABLE','volume':125,'sma_volume_20':100}), \
             patch.object(ict_methodology, 'session_sweeps', return_value=[]):
            points = signal_quality.confluence(plan, snapshot, {}, cfg, engine)
        self.assertEqual(points['session_vwap']['bonus'], 12)
        self.assertEqual(points['components']['zone_and_vwap'], 25)
        self.assertEqual(points['score'], sum(points['components'].values()))
        self.assertTrue(all(0 <= n <= 25 for n in points['components'].values()))

    def test_scenarios_do_not_invent_levels_and_keep_trade_geometry(self):
        snapshot,cfg = base_fixture()
        blank = replace(snapshot, candles={})
        self.assertEqual(scenario_planner.entry_scenarios(blank,cfg,engine,[],1),
                         {'pullback':None,'breakout':None})
        row = {'actionable':True, 'side':1, 'entry':93.69, 'stop':92.81,
               'tp1':94.97, 'tp2':95.36}
        plan = scenario_planner.entry_scenarios(snapshot,cfg,engine,[row],1)['pullback']
        self.assertEqual((plan['entry'],plan['stop'],plan['tp1'],plan['tp2']),
                         (93.69,92.81,94.57,95.36))

    def test_only_paper_reconciliation_before_activation_is_released(self):
        _, cfg = base_fixture()
        minute = 60_000
        plan = order(1, placed=NOW-12_000)
        plan.update(policy=bzu_policy.POLICY, execution='PAPER_SIGNAL', order_type='LIMIT',
                    effective_ts=NOW, resolved_ts=NOW, status='UNRESOLVED')
        state = engine.new_state(cfg)
        state['orders'] = [plan]
        state['reconciliation'] = {'order_id':plan['id'], 'ts':NOW,
                                   'reason':'PLACEMENT_INSIDE_TOUCHED_BAR'}
        plan['execution'] = 'MANUAL'
        engine.release_unconfirmed_paper(state, NOW+minute, cfg)
        self.assertIsNotNone(state['reconciliation'])
        plan['execution'] = 'PAPER_SIGNAL'
        engine.release_unconfirmed_paper(state, NOW+minute, cfg)
        self.assertIsNone(state['reconciliation'])
        self.assertEqual(state['reconciliation_history'][-1]['release_reason'], 'TOUCH_BEFORE_ACTIVATION')
        self.assertEqual(plan['status'], 'UNCONFIRMED')
        self.assertFalse(state['trades'])

    def test_future_paper_limits_never_fill_on_pre_activation_minute(self):
        _, cfg = base_fixture()
        minute = 60_000
        plan = order(1, placed=NOW-12_000)
        plan.update(policy=bzu_policy.POLICY, execution='PAPER_SIGNAL', order_type='LIMIT',
                    effective_ts=NOW)
        state = engine.new_state(cfg)
        state['pending'] = plan
        previous = engine.Candle(NOW-minute, 100.02, 100.07, 99.95, 100.01)
        snapshot = engine.Snapshot(NOW, 100.01, {'1m':[previous]})
        engine._advance_slot_lifecycle(state, snapshot, cfg, entry_gates_checked=True)
        self.assertIs(state['pending'], plan)
        self.assertIsNone(state['reconciliation'])
        self.assertIsNone(state['active'])

    def test_reports_are_independent_from_five_minute_scans(self):
        quarter = NOW // STEP * STEP
        offsets = (0, 5, 10, 15, 20, 25)
        last = quarter - STEP
        sent = []
        for offset in offsets:
            now = quarter + offset * 60_000
            if engine.report_due(now, last):
                sent.append(offset)
                last = now
        self.assertEqual(sent, [0, 15])
