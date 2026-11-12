"""Verify the separation between a structural watch and an admitted paper order."""
from dataclasses import replace
import unittest

import full_limit_engine as engine
import live_terminal
import scenario_planner as scenarios
from test_v12_31_market_wave import snapshot


def bearish_discount():
    rows = [(92.8, 93.20, 92.4, 92.60)] * 125
    for i in range(62):
        close = 92.55-i*.055
        rows.append((close+.03, close+.05, close-.08, close))
    rows += [(89.21,89.36,89.03,89.10),(89.10,89.28,88.81,89.05),
             (89.05,89.30,88.99,89.15),(89.15,89.32,89.04,89.22)]
    return snapshot(89.22, rows, [], htf_side=-1)


class SingleFocus(unittest.TestCase):
    def test_discount_sweep_is_an_explicit_watch_even_with_a_stale_bullish_regime(self):
        snap, cfg = bearish_discount()
        plan = scenarios.focus(snap, cfg, engine, {'state':'BULLISH_ACTIVE'})
        self.assertEqual(plan['market']['phase'], 'BEARISH_DISCOUNT')
        self.assertEqual(plan['market']['trend_side'], 0)
        self.assertEqual(plan['headline'], 'ПОШУК РОЗВОРОТУ ВІД ДНА (LONG)')
        watch = plan['developing_setup']
        self.assertIs(watch, plan['primary'])
        self.assertEqual((watch['setup'],watch['low'],watch['high']),
                         ('BULLISH_LIQUIDITY_SWEEP',88.81,88.81))
        self.assertAlmostEqual(watch['distance'], .41)
        history = watch['touch_history']
        self.assertGreaterEqual(history['sample_hours'], 12)
        self.assertLessEqual(history['matched_hours'], history['sample_hours'])
        self.assertIn('закриття M15 вище 88.81', watch['trigger'])
        self.assertIn('злам M15 89.36', watch['trigger'])
        self.assertTrue(watch['preview']['preview_only'])
        self.assertLess(watch['preview']['stop'], watch['preview']['entry'])
        self.assertEqual(plan['waiting_reason'], 'NO_SETUP_CANDIDATE')
        self.assertEqual(plan['alternative']['side'], -1)
        self.assertIn('EMA20', plan['alternative']['trigger'])
        before = replace(snap, now=snap.now-3*engine.TF['15m'])
        older = scenarios.focus(before, cfg, engine, {'state':'BULLISH_ACTIVE'})
        self.assertNotEqual(older['primary']['level'], 88.81)

    def test_public_watch_and_report_share_focus_without_creating_an_order(self):
        snap, cfg = bearish_discount()
        state = engine.new_state(cfg)
        focus = scenarios.focus(snap, cfg, engine, {'state':'RANGE'})
        result = {'analysis':{'scenario_plan':focus,'admitted_plans':[]}}
        public = live_terminal.build(state, snap, result, cfg, engine)
        self.assertEqual(public['developing_setup'], focus['primary'])
        self.assertEqual(public['orders'], [])
        self.assertEqual(engine.open_plans(state), [])
        text = engine.build_message(state, snap, result, cfg)
        self.assertIn('Готових сетапів немає. LONG', text)
        self.assertIn('до рівня 0.41 $', text)
        self.assertIn('Попередньо', text)
        self.assertIn('Резерв SHORT', text)
        self.assertNotIn('Пошук за трендом', text)

    def test_admitted_and_ledger_plans_take_precedence(self):
        snap, cfg = bearish_discount()
        base = {'side':-1,'entry':90.10,'stop':91.60,'tp1':87.95,'rr1':1.43}
        for status in ('ADMITTED','PENDING','CANCELLING','POSITION'):
            order = {**base, 'status':'OPEN' if status=='POSITION' else 'PENDING',
                     'expires_ts':snap.now+engine.TF['15m']}
            if status=='CANCELLING':
                order['cancel_requested_ts'] = snap.now
            orders = () if status=='ADMITTED' else (order,)
            plan = scenarios.focus(snap, cfg, engine, {'state':'RANGE'}, orders=orders, admitted=(base,))
            self.assertEqual(plan['primary']['status'], status)
            self.assertEqual(plan['primary']['level'], base['entry'])
            self.assertIsNone(plan['developing_setup'])
            self.assertIsNone(plan['waiting_reason'])
            self.assertEqual(plan['primary']['expires_ts'], order['expires_ts']
                             if status in ('PENDING','CANCELLING') else None)


if __name__ == '__main__':
    unittest.main()
