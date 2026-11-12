"""Verify the separation between a structural watch and an admitted paper order."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import full_limit_engine as engine
import live_terminal
import market_wave
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


def early_reclaim_fixture(side):
    rows = []
    for i in range(140):
        close = 94-i*.031
        rows.append((close+.02,close+.10,close-.08,close))
    rows += [(89.70,89.74,89.06,89.13),(89.13,89.31,88.81,89.18),
             (89.26,89.85,89.24,89.68),(89.68,89.72,89.22,89.34),
             (89.34,89.48,89.31,89.42),(89.42,89.94,89.41,89.90),
             (89.90,90.34,89.88,90.30)]
    if side == -1:
        rows = [(180-o,180-l,180-h,180-c) for o,h,l,c in rows]
    return snapshot(rows[-1][-1],rows,[],htf_side=-side)


class SingleFocus(unittest.TestCase):
    def test_closed_m15_reclaim_leads_ema_cross_and_keeps_a_fixed_retest(self):
        for side in (1,-1):
            snap,cfg = early_reclaim_fixture(side)
            level, protected = (89.85,89.22) if side==1 else (90.15,90.78)
            old_state = 'BEARISH_ACTIVE' if side==1 else 'BULLISH_ACTIVE'
            slow = {'state':old_state,'side_1h':-side,'side_4h':-side,
                    'active_structure':{},'local_structure':{}}
            before_close = replace(snap, now=snap.now-1-engine.TF['15m'])
            with patch.object(engine.policy,'market_regime',return_value=slow), \
                 patch.object(market_wave,'displacement_override',return_value={}), \
                 patch.object(market_wave,'breakdown_zones',return_value=[]), \
                 patch.object(market_wave,'structure_sync',return_value={}):
                before = engine.policy.execution_regime(before_close,cfg,engine)
                self.assertEqual(before['state'],old_state)
                ema_test = replace(before_close,price=90.10 if side==1 else 89.90)
                testing = scenarios.focus(ema_test,cfg,engine,before)
                self.assertEqual(testing['headline'],'ТЕСТ ЗОНИ EMA20/50')
                self.assertIsNone(testing['primary'])
                for price in (snap.price,snap.price+side*.15):
                    current = replace(snap,price=price)
                    regime = engine.policy.execution_regime(current,cfg,engine)
                    proof = regime['structure_sync']
                    self.assertEqual(proof['proof'],market_wave.EARLY_M15_RECLAIM)
                    self.assertEqual(proof['side'],side)
                    self.assertAlmostEqual(proof['protected_level'],protected)
                    self.assertGreaterEqual(proof['wave_atr'],1.10)
                    focus = scenarios.focus(current,cfg,engine,regime)
                    self.assertLess(side*(focus['market']['ema20']-focus['market']['ema50']),0)
                    self.assertEqual(focus['market']['trend_side'],side)
                    self.assertAlmostEqual(focus['primary']['level'],level)
                    self.assertEqual(focus['primary']['setup'],'M15_STRUCTURE_RETEST')
                    self.assertTrue(focus['primary']['preview']['preview_only'])
                close = protected-side*.08
                bar = engine.Candle(snap.now,protected,protected+.10,protected-.10,close)
                lost = replace(snap,now=snap.now+engine.TF['15m'],price=close,
                               candles={**snap.candles,'15m':[*snap.candles['15m'],bar]})
                self.assertNotEqual(market_wave.early_structure_reclaim(lost,cfg.tick,engine).get('side'),side)

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

    def test_new_highs_do_not_promote_short_over_an_established_bullish_trend(self):
        rows = []
        for i in range(192):
            close = 89+i*.022
            rows.append((close-.02,close+.1,close-.10,close))
        snap,cfg = snapshot(93.26,rows,[])
        regime = {'state':'BULLISH_ACTIVE',
                  'structure_sync':{'side':1,'known_ts':snap.now-2*engine.TF['15m'],
                                    'protected_level':92.40,'level':92.70}}
        base = {'known_ts':snap.now,'source':'TEST','status':'DEVELOPING','trigger':'M15',
                'invalidation':'M15','label':'Тест'}
        long = {**base,'side':1,'setup':'FVG_PULLBACK','level':92.88,'low':92.80,'high':92.88,
                'distance':.38,'entry_hint':92.88}
        for price,high in ((93.26,93.28),(93.44,93.47)):
            current = replace(snap,price=price)
            short = {**base,'side':-1,'setup':'BEARISH_LIQUIDITY_SWEEP','level':high,
                     'distance':high-price,'source':'NEW_YORK_HIGH'}
            with patch.object(engine.scanner,'developing_watches',return_value=[short,long]):
                focus = scenarios.focus(current,cfg,engine,regime)
                self.assertEqual(focus['market']['trend_side'],1)
                self.assertTrue(focus['market']['premium'])
                self.assertEqual(focus['primary']['level'],92.88)
                self.assertEqual(focus['alternative']['level'],92.40)
                self.assertIn('нижче 92.40',focus['alternative']['trigger'])
            with patch.object(engine.scanner,'developing_watches',return_value=[short]):
                focus = scenarios.focus(current,cfg,engine,regime)
                self.assertIsNone(focus['primary'])
                self.assertEqual(focus['alternative']['level'],92.40)
                self.assertEqual(focus['headline'],'ЧЕКАЄМО ВІДКАТ ЗА ТРЕНДОМ')
                admitted = {'side':-1,'entry':93.20,'stop':94.70,'tp1':91.00,'rr1':1.47}
                actual = scenarios.focus(current,cfg,engine,regime,admitted=[admitted])
                self.assertEqual((actual['primary']['side'],actual['primary']['status']),(-1,'ADMITTED'))


if __name__ == '__main__':
    unittest.main()
