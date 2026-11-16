"""Regression coverage for the Brent paper-limit lifecycle."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy
import full_limit_engine as engine
import live_terminal
import oil_risk
import portfolio_arbitration as arbitration
from test_full_limit_engine import NOW, analysis, order, snapshot


MINUTE = 60_000


def waiting(side=-1, placed=NOW):
    plan = order(side, placed, placed + 180 * MINUTE)
    entry, risk = 91.49, .98
    plan.update(order_number=208, policy=bzu_policy.POLICY, execution='PAPER_SIGNAL',
                organic_risk={'model': oil_risk.MODEL}, scale_policy=bzu_policy.SCALE_POLICY,
                action_type='NEW_PENDING_LIMIT', order_type='LIMIT', trend_rel='WITH_TREND',
                entry=entry, stop=entry-side*risk, initial_stop=entry-side*risk, risk=risk,
                entry_legs=[{'name':'L1','price':entry,'fraction':1.,'status':'PENDING'}],
                partials={'TP1':.5,'TP2':.5}, exit_policy=bzu_policy.EXIT,
                tp1=entry+side*1.53, tp=entry+side*1.87, tp2=entry+side*1.87,
                placed_ts=placed, effective_ts=placed, placement_price=entry+side*.44,
                target_cancel_armed=True, last_execution_step_ms=MINUTE)
    return plan


class PendingLimitLifecycle(TestCase):
    cfg = engine.Config(core=core, tick=.01)

    def book(self, plan):
        book = engine.new_state(self.cfg)
        engine.park_pending(book, [plan])
        return book

    def test_short_208_cancels_on_wick_before_the_later_retest(self):
        plan = waiting()
        book = self.book(plan)
        threshold = bzu_policy.missed_target_level(plan)
        self.assertEqual(threshold, 90.755)
        self.assertEqual(bzu_policy.partial_target(plan), 90.51)
        wick = engine.Candle(NOW, 90.78, 90.86, 90.70, 90.82)
        retest = engine.Candle(NOW+MINUTE, 90.82, 91.53, 90.80, 91.42)
        s = engine.Snapshot(NOW+2*MINUTE, retest.close, {'1m': [wick, retest]})
        engine._advance_slot_lifecycle(book, s, self.cfg, entry_gates_checked=True)
        final, = book['orders']
        self.assertEqual((final['status'], final['reason'], final['resolved_ts']),
                         ('CANCELLED', engine.MISSED_TARGET_REASON, NOW+MINUTE))
        self.assertIsNone(book['active'])

    def test_long_cancels_at_exact_threshold(self):
        plan = waiting(1)
        book = self.book(plan)
        threshold = bzu_policy.missed_target_level(plan)
        c = engine.Candle(NOW, 92., threshold, 91.9, 92.)
        s = engine.Snapshot(NOW+MINUTE, c.close, {'1m': [c]})
        engine._advance_slot_lifecycle(book, s, self.cfg, entry_gates_checked=True)
        self.assertEqual(book['orders'][0]['reason'], engine.MISSED_TARGET_REASON)

    def test_waiting_rule_uses_actual_tp1_if_it_is_closer_than_point_eight_r(self):
        plan = waiting(1)
        plan.pop('scale_policy')
        plan['tp1'] = 92.00
        self.assertEqual(bzu_policy.missed_target_level(plan), 92.00)

    def test_both_levels_in_one_minute_preserves_the_fill(self):
        plan = waiting()
        book = self.book(plan)
        c = engine.Candle(NOW, 90.86, 91.53, 90.37, 90.82)
        s = engine.Snapshot(NOW+MINUTE, c.close, {'1m': [c]})
        engine._advance_slot_lifecycle(book, s, self.cfg, entry_gates_checked=True)
        self.assertEqual(book['orders'][0]['status'], 'FILLED')
        self.assertIsNotNone(book['active'])

    def test_quote_requests_cancel_then_reconciles_confirmed_minute(self):
        plan = waiting()
        book = self.book(plan)
        s = engine.Snapshot(NOW+30_000, 90.65, {}, ticker_ts=NOW+29_000)
        book['execution_step_ms'] = MINUTE
        engine.review_waiting_conditions(book, plan, s, self.cfg)
        self.assertEqual(plan['cancel_reason'], engine.MISSED_TARGET_REASON)
        c = engine.Candle(NOW, 90.82, 90.90, 90.37, 90.55)
        s = replace(s, now=NOW+MINUTE, candles={'1m':[c]})
        with patch.object(engine, 'pending_entry_gate', return_value=None):
            engine.advance_lifecycle(book, s, self.cfg)
        self.assertFalse(engine.pending_plans(book))
        self.assertEqual(book['orders'][0]['reason'], engine.MISSED_TARGET_REASON)

    def test_quote_does_not_erase_a_fill_in_the_unconfirmed_minute(self):
        plan = waiting()
        book = self.book(plan)
        book['execution_step_ms'] = MINUTE
        s = engine.Snapshot(NOW+30_000, 90.65, {}, ticker_ts=NOW+29_000)
        engine.review_waiting_conditions(book, plan, s, self.cfg)
        c = engine.Candle(NOW, 90.82, 91.54, 90.60, 90.65)
        s = replace(s, now=NOW+MINUTE, candles={'1m':[c]})
        with patch.object(engine, 'pending_entry_gate', return_value=None):
            engine.advance_lifecycle(book, s, self.cfg)
        self.assertEqual(book['orders'][0]['status'], 'FILLED')
        self.assertIsNotNone(book['active'])

    def test_upgrade_detects_an_excursion_in_an_already_replayed_minute(self):
        plan = waiting()
        plan['last_bar'] = NOW
        book = self.book(plan)
        c = engine.Candle(NOW, 90.82, 90.91, 90.63, 90.78)
        s = engine.Snapshot(NOW+MINUTE, 90.78, {'1m':[c]})
        self.assertEqual(engine.prepare_pending_limits(book, s, self.cfg), {plan['id']})
        self.assertFalse(engine.prepare_pending_limits(book, s, self.cfg))

    def test_six_m15_bars_expire_without_fill(self):
        plan = waiting()
        book = self.book(plan)
        deadline = NOW+90*MINUTE
        last = engine.Candle(deadline-MINUTE, 91.04, 91.14, 90.99, 91.04)
        late_touch = engine.Candle(deadline, 91.04, 91.56, 91.02, 91.52)
        s = engine.Snapshot(deadline+MINUTE, late_touch.close, {'1m': [last, late_touch]})
        engine.prepare_pending_limits(book, s, self.cfg)
        engine._advance_slot_lifecycle(book, s, self.cfg, entry_gates_checked=True)
        final, = book['orders']
        self.assertEqual((final['status'], final['reason'], final['resolved_ts']),
                         ('EXPIRED', 'LIMIT_TTL', deadline))

    def test_short_chart_drops_cancelled_order_and_scanner_follows_the_ledger(self):
        plan = waiting()
        book = self.book(plan)
        s = engine.Snapshot(NOW+MINUTE, 90.65, {}, ticker_ts=NOW+MINUTE)
        initial = live_terminal.build(book, s, {'action':'DATA_HOLD'}, self.cfg, engine)
        self.assertEqual(initial['orders'][0]['missed_target_level'], 90.755)
        book['execution_step_ms'] = MINUTE
        engine.review_waiting_conditions(book, plan, s, self.cfg)
        final = live_terminal.build(book, s, {'action':'DATA_HOLD'}, self.cfg, engine)
        self.assertEqual(final['orders'], [])
        self.assertEqual(final['last_event']['reason'], engine.MISSED_TARGET_REASON)

    def test_report_explains_cancellation_and_identifies_a_market_replacement(self):
        plan = waiting()
        book = self.book(plan)
        s = snapshot(NOW+MINUTE, price=90.65)
        book['execution_step_ms'] = MINUTE
        engine.review_waiting_conditions(book, plan, s, self.cfg)
        engine.emit(book, 'PORTFOLIO_ROTATION_COMMITTED', s.now, replaced_id=plan['id'],
                    replacement_id='next', order_type='MARKET')
        report = engine.build_message(book, s, {'action':'ACTIVE_ENTRY','events':book['events']}, self.cfg)
        self.assertIn('рух від входу досяг 0.75R', report)
        self.assertIn('Вхід MARKET після зняття ліміту', report)


class TrendReplacement(TestCase):
    cfg = engine.Config(core=core, tick=.01)

    def candidates(self, incumbent, side, now=NOW):
        plan = deepcopy(incumbent)
        entry = (90.94 if side == -1 else 92.04)
        plan.update(id='near', event_key='fresh-event', entry=entry, stop=entry-side*.98,
                    tp1=entry+side*1.53, tp2=entry+side*1.87, tp=entry+side*1.87,
                    quality={'accepted':True}, score=90, placed_ts=now, effective_ts=now,
                    entry_zone={'kind':'FVG_BEAR' if side == -1 else 'FVG_BULL',
                                'timeframe':'15m', 'low':entry-.03, 'high':entry+.03,
                                'known_ts':now, 'side':side})
        return plan

    def test_backend_replacement_threshold_works_symmetrically(self):
        for side in (-1, 1):
            old = waiting(side, NOW-15*MINUTE)
            new = self.candidates(old, side)
            new['entry'] = old['entry']+side*.16
            for gap, a15, expected in ((.50,.40,True),(.63,.90,True),(.60,.90,False),(.27,.40,False)):
                with self.subTest(side=side, gap=gap, atr=a15), patch.object(engine, 'atr', return_value=a15):
                    s = engine.Snapshot(NOW, old['entry']+side*gap, {})
                    result = arbitration.decision([old], [new], s, self.cfg, engine.policy,
                                                  engine=engine, regime={'trend_anchor_side':side})
                    self.assertEqual(result.get('reason') == 'CANCELLED_REPLACED_BY_LIVE_SETUP', expected)

    def test_stale_future_or_unapproved_challenger_cannot_cancel_the_limit(self):
        old = waiting(-1, NOW-15*MINUTE)
        new = self.candidates(old, -1)
        s = engine.Snapshot(NOW, 90.89, {})
        invalid = [
            {**new, 'entry_zone':{**new['entry_zone'], 'known_ts':ts}}
            for ts in (old['placed_ts'], NOW+MINUTE)
        ] + [{**new, 'side':1}, {**new, 'quality':{'accepted':False}}, {**new, 'entry':91.70}]
        for plan in invalid:
            result = arbitration.decision([old], [plan], s, self.cfg, engine.policy,
                                          engine=engine, regime={'trend_anchor_side':-1})
            self.assertEqual(result['decision'], 'KEEP_TREND_PRELIMIT')

    def test_confirmed_market_setup_can_challenge(self):
        old = waiting(-1, NOW-15*MINUTE)
        new = self.candidates(old, -1)
        s = engine.Snapshot(NOW, 90.88, {})
        new.update(action_type='ACTIVE_ENTRY', order_type='MARKET', entry=90.87,
                   market_trigger={'pattern':'TREND_CONTINUATION_MARKET','known_ts':NOW,'limit_entry':90.94})
        with patch.object(engine, 'atr', return_value=.5):
            result = arbitration.decision([old], [new], s, self.cfg, engine.policy,
                                          engine=engine, regime={'trend_anchor_side':-1})
        self.assertEqual(result['candidate_id'], new['id'])

    def test_rotation_through_the_router_releases_the_slot_before_placing(self):
        old = waiting(-1, NOW-15*MINUTE)
        new = self.candidates(old, -1)
        book = engine.new_state(self.cfg)
        engine.park_pending(book, [old])
        s = snapshot(NOW, price=90.89)
        s.candles['15m'] = [replace(c, open=90.89, high=91.14, low=90.64, close=90.89)
                           for c in s.candles['15m']]
        s.candles['1m'] = [engine.Candle(NOW-MINUTE, 90.9, 90.93, 90.86, 90.89)]
        old['last_bar'] = NOW-MINUTE
        market = {'state':'BEARISH_ACTIVE','trend_anchor_side':-1}
        with patch.object(engine, 'analyze', return_value=analysis([new])), \
             patch.object(engine, 'review_waiting_conditions'), \
             patch.object(engine.policy, 'execution_regime', return_value=market), \
             patch.object(engine.policy, 'trend_entry_error', return_value=None), \
             patch.object(engine.scenario_planner, 'entry_error', return_value=None), \
             patch.object(engine.oil_risk, 'structural_room', return_value=(None,None)), \
             patch.object(engine.scanner, 'candidate_plan_error', return_value=None), \
             patch.object(engine.scanner, 'calculate_zone_trade_plan', return_value={'tactic':'LIMIT','entry_condition':'RETEST'}), \
             patch.object(engine.scenario_planner, 'focus', return_value={'developing_setup':None}):
            result = engine.run_cycle(book, s, self.cfg)
        self.assertEqual(result['analysis']['portfolio_arbitration']['decision'], 'FAST_ROTATE_SAME_CYCLE')
        self.assertEqual([p['id'] for p in engine.pending_plans(book)], ['near'])
        self.assertEqual(book['orders'][-1]['reason'], 'CANCELLED_REPLACED_BY_LIVE_SETUP')
        self.assertEqual([e['kind'] for e in book['events'] if e['kind'] in ('CANCELLED', 'PLACED')],
                         ['CANCELLED', 'PLACED'])
