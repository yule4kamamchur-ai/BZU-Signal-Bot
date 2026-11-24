"""Release regression tests for the symmetric Brent scanner and position policy."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import live_terminal
import market_wave as wave
import notification_report
import position_monitor as monitor
from test_v12_24_momentum import NOW, STEP
import test_v12_35_stale_and_setups as position_fixtures
from test_v12_40_execution import base_fixture


def candles(ohlc, step=STEP):
    return [engine.Candle(NOW-(len(ohlc)-i)*step,*row,100.) for i,row in enumerate(ohlc)]


def oriented(rows, side):
    if side==1:
        return rows
    return [replace(bar,open=200-bar.open,close=200-bar.close,
                    low=200-bar.high,high=200-bar.low) for bar in rows]


class TraderBrain(TestCase):
    def test_matrix_owns_execution_direction_before_the_ema20_ema50_cross(self):
        template,cfg = base_fixture()
        closes = [100.]*54+[99.9,99.8,99.7,99.55,99.40,99.25]
        rows = candles([(c,c+.12,c-.12,c) for c in closes])
        for side in (1,-1):
            history = oriented(rows,-side)
            snap = replace(template,now=NOW,price=history[-1].close,candles={'15m':history})
            old = {'state':'RANGE','side_1h':-side,'side_4h':0}
            ema20,vwap = (100.,100.10) if side==-1 else (100.,99.90)
            with self.subTest(side=side), patch.object(direction,'trend_references',return_value=(ema20,vwap)), \
                 patch.object(direction,'market_regime',return_value=old), \
                 patch.object(wave,'chronological_wave',return_value={}):
                regime = direction.execution_regime(snap,cfg,engine)
            self.assertEqual(regime['trend_anchor_side'],side)
            self.assertEqual(regime['structure_sync']['proof'],wave.EMA_MATRIX)

    def test_v_reversal_needs_a_closed_neckline_break_at_the_range_edge(self):
        template,cfg = base_fixture()
        rows = candles([(102.,102.3,101.8,102.)]*22+[
            (100.8,101.,100.3,100.4),(100.4,100.6,99.8,99.95),
            (99.95,100.38,99.91,100.30),(100.30,100.82,100.24,100.74),
            (100.74,100.78,100.39,100.50),(100.50,100.55,100.03,100.18),
            (100.18,100.55,100.17,100.40),(100.40,100.71,100.39,100.67),
            (100.67,101.06,100.64,100.95),
        ])
        for side in (1,-1):
            history = oriented(rows,side)
            snap = replace(template,now=NOW,price=history[-1].close,candles={'15m':history})
            with self.subTest(side=side):
                before = replace(snap,now=NOW-1)
                self.assertFalse(list(wave.v_reversals(history,before,cfg.tick,engine)))
                zone, = wave.v_reversals(history,snap,cfg.tick,engine)
                self.assertEqual((zone['kind'],zone['side'],zone['known_ts']), (wave.V_REVERSAL,side,NOW))
                self.assertAlmostEqual(zone['reference'],100+side*.82)
                self.assertAlmostEqual(zone['wave_proof']['invalidation'],100+side*.03)
                self.assertTrue(direction.edge_reversal(side,zone,snap,cfg.tick))
                self.assertFalse(direction.edge_reversal(side,zone,replace(snap,price=100.),cfg.tick))

    def test_trap_requires_a_closed_failure_back_inside_the_range(self):
        template,cfg = base_fixture()
        rows = candles([(101.,101.75,100.,101.)]*24+
                       [(100.04,100.08,99.71,99.78),(99.77,100.30,99.75,100.22)])
        for side in (1,-1):
            history = oriented(rows,side)
            snap = replace(template,now=NOW,price=history[-1].close,candles={'15m':history})
            pool = {'name':'SESSION_LOW' if side==1 else 'SESSION_HIGH','level':100.,'known_ts':NOW-3*STEP}
            with self.subTest(side=side), patch.object(wave.ict,'session_liquidity',return_value={'pools':[pool]}), \
                 patch.object(direction,'day_extrema',return_value=[]):
                found = list(wave._reversals(snap,history,STEP,cfg.tick,engine,.60,False))
                trap = next(z for z in found if z['kind']==wave.FAILED_TRAP)
                self.assertEqual((trap['side'],trap['known_ts']), (side,NOW))
                self.assertTrue(direction.edge_reversal(side,trap,snap,cfg.tick))
                earlier = replace(snap,now=NOW-1,candles={'15m':history[:-1]})
                self.assertFalse(direction.edge_reversal(side,trap,earlier,cfg.tick))

    def test_ema9_surf_uses_pause_edge_and_cannot_see_an_unclosed_signal(self):
        template,cfg = base_fixture()
        rows = candles([(99.85,99.98,99.76,99.92)]*13+[
            (99.94,100.38,99.93,100.31),
            (100.31,100.34,100.12,100.19),
            (100.19,100.23,100.02,100.08),
            (100.08,100.31,99.99,100.26),
        ],step=wave.M5)
        for side in (1,-1):
            history = oriented(rows,side)
            snap = replace(template,now=NOW,price=history[-1].close)
            refs = {'side':side,'close':100+side*.25,'ema9':100.,'ema20':100-side*.20,'ema50':100-side*.42}
            with self.subTest(side=side), patch.object(direction,'market_matrix',return_value=refs):
                zone = wave.ema9_momentum_surf(history,snap,side,.50,cfg.tick,engine)
                self.assertEqual((zone['kind'],zone['reference']), (wave.EMA9_SURF,100.))
                self.assertEqual(zone['wave_proof']['pause_bars'],2)
                self.assertAlmostEqual(zone['wave_proof']['invalidation'],100-side*.01)
                self.assertIsNone(wave.ema9_momentum_surf(history,replace(snap,now=NOW-1),side,.50,cfg.tick,engine))

    def test_lockout_only_lifts_after_a_closed_bos_or_a_volume_engulfing(self):
        template,cfg = base_fixture()
        rows = candles([(100.,100.25,99.75,100.)]*22 +
                       [(100.25,100.30,99.88,99.95),(99.94,100.35,99.89,100.32)])
        loss = {'side':1,'entry':100.,'closed_ts':NOW-2*STEP,'net_r':-.5,'close_reason':'STOP'}
        candidate = {'score':68,'quality':{'accepted':True}}
        def result(history,plan=candidate,closed=loss):
            snap = replace(template,now=NOW,price=history[-1].close,candles={'15m':history})
            return bzu_policy.adverse_reentry_reason(1,[closed],snap,cfg,engine,plan=plan)

        with patch.object(direction,'structural_flip',return_value={'confirmed':False}), \
             patch.object(direction,'bos_recovers_level',return_value=False):
            strong = [*rows[:-1],replace(rows[-1],volume=125.)]
            self.assertEqual(result(rows),'ADVERSE_STRUCTURE_LOCKOUT')
            self.assertIsNone(result(strong))
            later = [*strong, replace(strong[-1],ts=NOW,volume=100.)]
            aged = replace(template,now=NOW+STEP,price=later[-1].close,candles={'15m':later})
            self.assertIsNone(bzu_policy.adverse_reentry_reason(1,[loss],aged,cfg,engine,plan=candidate))
            self.assertEqual(result(strong,{**candidate,'score':67}), 'ADVERSE_STRUCTURE_LOCKOUT')
            self.assertEqual(result(strong,closed={**loss,'closed_ts':NOW-STEP+1}), 'ADVERSE_STRUCTURE_LOCKOUT')
        with patch.object(direction,'structural_flip',return_value={'confirmed':True}):
            self.assertIsNone(result(rows))
            self.assertEqual(result(rows,{**candidate,'score':67}),'ADVERSE_STRUCTURE_LOCKOUT')

    def test_climactic_breakout_needs_both_large_span_and_an_extended_leg(self):
        template,_ = base_fixture()
        trending = candles([(100.+i*.20,100.29+i*.20,99.97+i*.20,100.24+i*.20) for i in range(24)])
        flat = candles([(100.,100.29,99.97,100.24)]*24)
        def blocked(rows, ratio):
            snap = replace(template,now=NOW,candles={'15m':rows})
            zone = {'kind':wave.BOS_IMPULSE,'wave_proof': {
                'break_ts':NOW, 'impulse_ts':NOW-STEP, 'impulse_atr':.4,'impulse_range':ratio*.4}}
            return adverse.climactic_breakout(snap,1,zone,engine)

        self.assertTrue(blocked(trending,2.31))
        self.assertFalse(blocked(trending,2.29))
        self.assertFalse(blocked(flat,2.31))

    def test_first_six_bars_keep_initial_stop_and_post_partial_can_only_hit_stop_or_tp2(self):
        fixture = position_fixtures.PositionGuard()
        position,_,cfg = fixture.make_position()
        for bars,partial,expected in ((5,False,False),(6,False,True),(6,True,False),(30,True,False)):
            ts = NOW+bars*STEP
            test = {**position,'max_favorable_r':.55,'adverse_impulse_ts':ts,'partial_tp1_hit':partial,
                    'adverse_close_pair':{'known_ts':ts,'level':98.15,'ema20':98.18,'session_vwap':98.20}}
            with self.subTest(bars=bars,partial=partial):
                decision = monitor.evaluate_active_trade_lifecycle(test,ts,97.95,cfg,engine)
                self.assertIs(decision['should_exit'],expected)
                self.assertIsNone(decision['stop'])
        timed = NOW+25*STEP
        plain = {**position,'max_favorable_r':.60,'position_regime_side':-position['side']}
        self.assertFalse(monitor.evaluate_active_trade_lifecycle(plain,timed,97.95,cfg,engine)['should_exit'])
        _,book,cfg = fixture.make_position()
        snapshot,cfg_live = base_fixture()
        snapshot = replace(snapshot,now=NOW+4*STEP)
        report = notification_report.position_diagnostics(book['active'],snapshot.now,cfg,engine)
        public = live_terminal.build(book,snapshot,{'analysis':{}},cfg_live,engine)
        self.assertIn('Початковий структурний SL', ' '.join(report))
        self.assertEqual(public['orders'][0]['position_guard']['mode'],'STRUCTURAL_HOLD')
