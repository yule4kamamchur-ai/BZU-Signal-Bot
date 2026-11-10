"""Position guard and setup arbitration regressions on the paper lifecycle."""
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import full_limit_engine as engine
import ict_methodology as ict
import live_terminal
import notification_report
import portfolio_arbitration as arbitration
import position_monitor as monitor
from strategy_direct_entries import shallow_bos_retest
from test_v12_24_momentum import NOW
from test_v12_32_organic_oil import swing_fixture

M15 = 900_000


class PositionGuard(unittest.TestCase):
    def make_position(self, *, side=1, commission=False):
        _, cfg, _ = swing_fixture()
        cfg = replace(cfg, include_commission=commission)
        entry = 98.26
        plan = {'id': 'guard-test', 'order_id': 'guard-test', 'event_key': 'guard-event',
                'side': side, 'policy': bzu_policy.POLICY, 'entry': entry, 'stop': entry-side*1.5,
                'initial_stop': entry-side*1.5, 'tp1': entry+side*1.65, 'tp2': entry+side*2.85,
                'tp': entry+side*2.85, 'risk': 1.5, 'version': engine.VERSION,
                'include_commission': commission, 'slippage_bps': 0., 'action_type': 'ACTIVE_ENTRY',
                'setup': 'PULLBACK_CONTINUATION',
                'entry_fee_rate': engine.fee_rate(cfg, 'taker'), 'exit_fee_rate': engine.fee_rate(cfg, 'taker'),
                'entry_legs': [{'name': 'ACTIVE', 'price': entry, 'fraction': 1., 'status': 'PENDING'}],
                'partials': {'TP1': .5, 'TP2': .5}, 'placed_ts': NOW,
                'effective_ts': NOW, 'expires_ts': NOW+M15, 'execution': 'PAPER_SIGNAL'}
        p = bzu_policy.first_fill(plan, engine.Candle(NOW, entry, entry, entry, entry), engine, cfg, 60_000)
        book = engine.new_state(cfg)
        book['active'] = p
        return p, book, cfg

    def bar(self, book, cfg, end, opened, high, low, close, snapshot=None):
        engine.manage_bar(book, engine.Candle(end-60_000, opened, high, low, close),
                          cfg, step=60_000, snapshot=snapshot)

    def test_round_trip_at_98_26_after_three_hours(self):
        for side in (1, -1):
            for commission in (False, True):
                with self.subTest(side=side, commission=commission):
                    p, book, cfg = self.make_position(side=side, commission=commission)
                    entry = p['entry']
                    low, high = sorted((entry+side*.90, entry-side*.12))
                    self.bar(book, cfg, NOW+M15, entry, high, low, entry+side*.70)
                    self.assertAlmostEqual(p['max_favorable_r'], .60)
                    self.assertAlmostEqual(p['max_adverse_r'], .08)
                    self.bar(book, cfg, NOW+5*M15, entry+side*.30, entry+.30, entry-.30, entry)
                    self.assertIs(book['active'], p)
                    self.bar(book, cfg, NOW+12*M15, entry+side*.10, entry+.10, entry-.10, entry)
                    self.assertIsNone(book['active'])
                    trade, = book['trades']
                    self.assertEqual(trade['close_reason'], 'CLOSED_ROUND_TRIP_BE')
                    self.assertEqual((trade['exit'], trade['bars_in_trade']), (98.26, 12))
                    self.assertEqual(trade['gross_r'], 0.)
                    self.assertAlmostEqual(trade['net_r'], -trade['fees_r'])
                    self.assertEqual(trade['fees_r'] > 0, commission)

    def test_hard_stop_priority_and_same_bar_peak_does_not_arm_retest(self):
        p, book, cfg = self.make_position()
        self.bar(book, cfg, NOW+7*M15, 98.26, 99.20, 98.20, 98.26)
        self.assertIs(book['active'], p)
        self.assertEqual(monitor.guard(p, NOW+7*M15)['mode'], 'ROUND_TRIP')
        self.bar(book, cfg, NOW+8*M15, 98.26, 98.30, 96.75, 98.26)
        self.assertEqual(book['trades'][0]['close_reason'], 'STOP')

    def test_stale_timeout_and_swing_stop(self):
        p, book, cfg = self.make_position()
        end = NOW+24*M15
        rows = [engine.Candle(end-(8-i)*M15, 98.30, 98.53, low, 98.44)
                for i, low in enumerate((98.16, 98.06, 98.17, 98.18, 98.15, 98.16, 98.17, 98.18))]
        snapshot = engine.Snapshot(end, 98.43, {'15m': rows})
        self.bar(book, cfg, end, 98.40, 98.52, 98.37, 98.43, snapshot)
        self.assertEqual(p['stop'], 98.14)
        self.assertLessEqual(p['entry']-p['stop'], .35*p['risk'])
        self.assertEqual(monitor.guard(p, end)['mode'], 'STALE_TIMEOUT')
        self.bar(book, cfg, end+60_000, 98.43, 98.45, 98.26, 98.26, snapshot)
        self.assertEqual(book['trades'][0]['close_reason'], 'CLOSED_STALE_TIMEOUT')

    def test_choch_only_exits_on_later_retest(self):
        p, book, cfg = self.make_position()
        end = NOW+10*M15
        rows = [engine.Candle(end-(8-i)*M15, 98.40, 98.60, low, close)
                for i, (low, close) in enumerate([(98.22,98.45)]*4+[(98.10,98.44),(98.23,98.47),
                                                                    (98.24,98.48),(97.97,98.05)])]
        snapshot = engine.Snapshot(end, 98.05, {'15m': rows})
        self.bar(book, cfg, end, 98.40, 98.42, 97.97, 98.05, snapshot)
        self.assertEqual(p['adverse_choch_ts'], end)
        self.assertIs(book['active'], p)
        self.bar(book, cfg, end+60_000, 98.05, 98.24, 98.03, 98.20, snapshot)
        self.assertEqual(book['trades'][0]['close_reason'], 'CLOSED_ADVERSE_CHOCH')

    def test_projection_and_telegram_have_same_guard(self):
        p, book, cfg = self.make_position()
        p.update(max_favorable_r=.60, max_adverse_r=.15)
        now = NOW+7*M15
        s = engine.Snapshot(now, 98.60, {}, 98.59, 98.61, ticker_ts=now)
        result = {'action':'DATA_HOLD', 'analysis':{'health':['STALE_3m']}}
        row, = live_terminal.build(book, s, result, cfg, engine)['orders']
        self.assertEqual(row['position_guard']['mode'], 'ROUND_TRIP')
        self.assertEqual((row['max_favorable_r'], row['max_adverse_r'], row['bars_in_trade']), (.60,.15,7))
        text = '\n'.join(notification_report.position_diagnostics(p, now, cfg, engine))
        for expected in ('1 год 45 хв', 'MFE +0.60R', '98.26'):
            self.assertIn(expected, text)

    def test_existing_fill_backfills_only_already_processed_bars(self):
        p, _, _ = self.make_position()
        del p['max_adverse_r'], p['max_favorable_r']
        p.update(last_bar=NOW+2*M15-60_000, last_execution_step_ms=60_000, mfe_r=.20)
        bars = [engine.Candle(NOW+M15, 98.25, 98.86, 97.96, 98.30),
                engine.Candle(NOW+2*M15, 98.30, 102., 95., 98.30)]
        snap = engine.Snapshot(NOW+3*M15, 98.30, {'15m': bars})
        monitor.backfill(p, snap, NOW+2*M15)
        self.assertAlmostEqual(p['max_favorable_r'], .40)
        self.assertAlmostEqual(p['max_adverse_r'], .20)

    def test_replacement_needs_new_market_A_plus_and_opposite_structure(self):
        p, _, cfg = self.make_position()
        now = NOW+16*M15
        snap = engine.Snapshot(now, 98.26, {}, 98.25, 98.27, ticker_ts=now)
        fresh = {'id':'fresh','event_key':'fresh','side':-1,'entry':98.25,'stop':99.75,
                 'rating':'A+','score':88,'order_type':'MARKET','trend_rel':'WITH_TREND',
                 'market_trigger':{'pattern':'POST_TOUCH_REJECTION','known_ts':now-60_000},
                 'entry_zone':{'known_ts':now-M15},'quality':{'accepted':True}}
        with patch.object(direction,'structural_flip',return_value={'confirmed':False}):
            self.assertIsNone(arbitration.stale_replacement(p,[fresh],snap,cfg,engine))
        with patch.object(direction,'structural_flip',return_value={'confirmed':True}):
            decision = arbitration.stale_replacement(p,[fresh],snap,cfg,engine)
            self.assertEqual((decision['candidate_id'],decision['exit_quote']), ('fresh',98.25))
            self.assertIsNone(arbitration.stale_replacement(p,[{**fresh,'rating':'A'}],snap,cfg,engine))

    def test_replacement_closes_old_slot_before_recording_new_market(self):
        snap, cfg, targets = swing_fixture()
        snap = replace(snap, price=96.02, bid=96.01, ask=96.03)
        regime = {**direction.market_regime(snap,cfg.tick,engine),'side_1h':1,'side_4h':0}
        with patch.object(direction,'market_regime',return_value=regime), \
             patch.object(engine,'full_targets',return_value=targets), \
             patch.object(direction,'vwap_priority',return_value={'aligned':True}), \
             patch.dict(os.environ, {'BZU_ENTRY_MODE':'HYBRID','BZU_EDGE_MODE':'SHADOW'}):
            first = engine.new_state(cfg)
            self.assertEqual(engine.run_cycle(first,snap,cfg)['action'], 'ACTIVE_ENTRY')
            incumbent, = engine.active_plans(first)
            incumbent.update(id='stale-old',order_id='stale-old',event_key='stale-old',filled_ts=snap.now-16*M15)
            book = engine.new_state(cfg)
            book['active'] = incumbent
            # The fixture's incumbent was already reconciled to the quote.
            with patch.object(engine,'advance_lifecycle'):
                result=engine.run_cycle(book,snap,cfg)
            self.assertEqual(result['action'],'ACTIVE_ENTRY')
            self.assertEqual(result['analysis']['portfolio_arbitration']['decision'],'STALE_POSITION_REPLACEMENT')
            trade, = book['trades']
            self.assertEqual((trade['id'],trade['close_reason']), ('stale-old','CLOSED_STALE_REPLACEMENT'))
            current, = engine.active_plans(book)
            self.assertEqual((current['order_type'],current['entry']), ('MARKET',snap.ask))
            self.assertEqual([e['kind'] for e in result['events'] if e['kind'] in ('CLOSED','FILLED')],
                             ['CLOSED','FILLED'])


class SetupSelection(unittest.TestCase):
    def test_liquidity_reclaim_can_override_local_slope_but_not_macro(self):
        candle = engine.Candle(NOW-M15, 98.24, 98.41, 97.89, 98.30)
        zone = {'kind':'TURTLE_SOUP','known_ts':NOW,
                'sweep':{'side':1,'pattern':'WICK_RECLAIM','level':98.20,'extreme':97.89,
                         'raid_ts':NOW-M15,'reference_known_ts':NOW-2*M15}}
        snap = engine.Snapshot(NOW,98.30,{'15m':[candle]})
        regime={'state':'RANGE','active_structure':{'side':-1},'side_1h':0,'side_4h':0}
        self.assertGreaterEqual(ict.wick_reclaim(candle,1,98.20,.01),.45)
        self.assertEqual(direction.regime_admission(1,98.20,regime,snap,.01,engine),
                         (False,'LOCAL_STRUCTURE_CONFLICT'))
        self.assertEqual(direction.regime_admission(1,98.20,regime,snap,.01,engine,zone),
                         (True,'REVERSAL_SWEEP_LOCAL_OVERRIDE'))
        self.assertFalse(direction.regime_admission(1,98.20,
                         {**regime,'trend_anchor_side':-1,'state':'BEARISH_ACTIVE'},snap,.01,engine,zone)[0])

    def test_session_sweep_bos_window_includes_tenth_closed_bar(self):
        start = NOW-30*M15
        raid_index = 16
        for delay in (10, 11):
            rows = [engine.Candle(start+i*M15, 98.4, 98.6, 98.3, 98.45)
                    for i in range(raid_index+delay+1)]
            raid = rows[raid_index]
            rows[raid_index] = replace(raid, low=97.90)
            rows[-1] = replace(rows[-1], high=98.95, close=98.90)
            snap=engine.Snapshot(rows[-1].ts+M15,98.90,{'15m':rows})
            pool={'name':'PD_LOW','level':98.20}
            pivot={'kind':'HIGH','level':98.70,'known_ts':raid.ts-2*M15}
            with patch.object(ict,'session_liquidity',return_value={'pools':[pool]}), \
                 patch.object(engine,'pivots',return_value=[pivot]):
                result=ict.session_sweeps(snap,1,.01,engine)
            self.assertEqual(bool(result),delay==10)

    def test_shallow_body_retracement_and_composite_ignore_detector_name(self):
        candle=engine.Candle(NOW-M15,98.,99.04,97.97,99.)
        self.assertEqual(shallow_bos_retest(candle,1,.50,.01),(98.65,98.75,98.70))
        snapshot=engine.Snapshot(NOW,98.80,{})
        trend={'id':'trend','setup':'TREND_PULLBACK','side':1,'entry':98.72,'stop':97.22,'score':95}
        ob={**trend,'id':'ob','setup':'ORDER_BLOCK','entry':98.60,'score':78,
            'organic_risk':{'macro_anchor':{'level':97.60,'known_ts':NOW-1,'kind':'SWING_LOW'}}}
        self.assertGreater(arbitration.cluster_rank(ob,snapshot,.01),arbitration.cluster_rank(trend,snapshot,.01))
        self.assertGreater(arbitration.cluster_rank(trend,snapshot,.01),
                           arbitration.cluster_rank({**ob,'organic_risk':{}},snapshot,.01))
