"""Position guard and setup arbitration regressions on the paper lifecycle."""
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
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
    def make_position(self, *, side=1, commission=False, entry=98.26, risk=1.50, entered=NOW):
        _, cfg, _ = swing_fixture()
        cfg = replace(cfg, include_commission=commission)
        plan = {'id': 'guard-test', 'order_id': 'guard-test', 'event_key': 'guard-event',
                'side': side, 'policy': bzu_policy.POLICY, 'entry': entry, 'stop': round(entry-side*risk, 2),
                'initial_stop': round(entry-side*risk, 2), 'tp1': entry+side*1.65, 'tp2': entry+side*2.85,
                'tp': entry+side*2.85, 'risk': risk, 'version': engine.VERSION,
                'include_commission': commission, 'slippage_bps': 0., 'action_type': 'ACTIVE_ENTRY',
                'setup': 'PULLBACK_CONTINUATION',
                'entry_fee_rate': engine.fee_rate(cfg, 'taker'), 'exit_fee_rate': engine.fee_rate(cfg, 'taker'),
                'entry_legs': [{'name': 'ACTIVE', 'price': entry, 'fraction': 1., 'status': 'PENDING'}],
                'partials': {'TP1': .5, 'TP2': .5}, 'placed_ts': entered,
                'effective_ts': entered, 'expires_ts': entered+M15, 'execution': 'PAPER_SIGNAL'}
        p = bzu_policy.first_fill(plan, engine.Candle(entered, entry, entry, entry, entry), engine, cfg, 60_000)
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

    def test_round_trip_uses_closed_peak_and_exits_below_old_band(self):
        p, book, cfg = self.make_position()
        end = NOW + 7 * M15
        self.bar(book, cfg, end, 98.26, 98.89, 97.70, 97.72)
        trade, = book['trades']
        self.assertIsNone(book['active'])
        self.assertAlmostEqual(trade['max_favorable_r'], .42)
        self.assertEqual((trade['close_reason'], trade['exit']), ('CLOSED_ROUND_TRIP_BE', 97.72))
        self.assertLess(trade['gross_r'], -.10)

    def test_hard_stop_has_priority(self):
        p, book, cfg = self.make_position()
        self.bar(book, cfg, NOW+M15, 98.26, 99.20, 98.20, 98.56)
        self.bar(book, cfg, NOW+8*M15, 98.26, 98.30, 96.75, 98.26)
        self.assertEqual(book['trades'][0]['close_reason'], 'STOP')

    def test_stale_time_stop_is_committed_without_swing_and_can_close_at_quote(self):
        for side, stop in ((1, 97.74), (-1, 98.79)):
            with self.subTest(side=side):
                p, book, cfg = self.make_position(side=side)
                end = NOW+24*M15
                self.bar(book, cfg, end-60_000, 98.26, 98.33, 98.20, 98.26)
                self.assertEqual(p['stop'], p['initial_stop'])
                self.bar(book, cfg, end, 98.26, 98.33, 98.20, 98.26)
                self.assertEqual(p['stop'], stop)
                self.assertEqual(monitor.guard(p, end)['mode'], 'STALE_TIGHTENED')
                self.assertEqual(book['events'][-1]['kind'], 'PAPER_STOP_AMENDED')
                ts = end + 1
                price = stop - side*.06
                s = engine.Snapshot(ts, price, {}, price if side==1 else price-.01,
                                    price+.01 if side==1 else price, ticker_ts=ts)
                engine.check_quote_exits(book, s, cfg)
                trade, = book['trades']
                self.assertEqual(trade['close_reason'], 'CLOSED_STALE_TIME_STOP')
                self.assertAlmostEqual(trade['exit'], price)

    def test_confirmed_choch_exits_in_loss_after_three_hours(self):
        p, book, cfg = self.make_position()
        end = NOW+12*M15
        rows = [engine.Candle(end-(8-i)*M15, 98.40, 98.60, low, close)
                for i, (low, close) in enumerate([(98.22,98.45)]*4+[(98.10,98.44),(98.23,98.47),
                                                                    (98.24,98.48),(97.97,98.02)])]
        snapshot = engine.Snapshot(end, 98.02, {'15m': rows})
        self.bar(book, cfg, end, 98.40, 98.42, 97.97, 98.02, snapshot)
        trade, = book['trades']
        self.assertEqual((trade['protection_structure_ts'], trade['close_reason']),
                         (end, 'CLOSED_ADVERSE_STRUCTURE_SHIFT'))

    def test_quote_regime_flip_exits_a_former_winner_in_loss(self):
        p, book, cfg = self.make_position(side=-1)
        end = NOW + 3 * M15
        rows = [engine.Candle(end-(9-i)*M15,98.2,98.4,98.1,98.3) for i in range(9)]
        p.update(max_favorable_r=.32)
        s = engine.Snapshot(end,98.36,{'15m':rows},98.35,98.36,ticker_ts=end)
        with patch.object(direction,'execution_regime',return_value={'state':'BULLISH_ACTIVE'}):
            engine.check_quote_exits(book,s,cfg)
        self.assertEqual(book['trades'][0]['close_reason'],'CLOSED_ADVERSE_STRUCTURE_SHIFT')

    def test_short_185_overdue_guard_commits_close_to_both_outputs(self):
        tz = ZoneInfo('Europe/Kyiv')
        entered = int(datetime(2026,11,10,10,45,53,tzinfo=tz).timestamp()*1000)
        now = int(datetime(2026,11,10,19,26,tzinfo=tz).timestamp()*1000)
        p, book, cfg = self.make_position(side=-1, entry=89.63, risk=1.99, entered=entered)
        p.update(order_number=185, last_bar=now-60_000, last_execution_step_ms=60_000)
        book.update(last_run_ts=now-1, order_sequence=185)
        candles = {}
        for tf, count in {'1m':20, **engine.MIN_BARS}.items():
            step = 60_000 if tf == '1m' else engine.TF[tf]
            end = now//step*step
            candles[tf] = [engine.Candle(end-(count+1-i)*step,90.49,90.65,90.40,90.60)
                           for i in range(count+1)]
        post_fill = next(i for i,c in enumerate(candles['15m']) if c.ts >= entered)
        candles['15m'][post_fill] = replace(candles['15m'][post_fill], low=88.75)
        s = engine.Snapshot(now, 90.64, candles,90.63,90.64, ticker_ts=now)
        overdue = '\n'.join(notification_report.position_diagnostics(p, now, cfg, engine, frozen=True))
        self.assertIn('Тайм-стоп 16:45 вичерпано', overdue)
        self.assertIn('прострочено на 2 год 40 хв', overdue)
        self.assertNotIn('Супровід до 16:45', overdue)
        bullish = {'state':'BULLISH_ACTIVE','side_1h':1,'side_4h':0,'trend_anchor_side':1}
        with patch.object(direction,'execution_regime',return_value=bullish), \
             patch.dict(os.environ, {'BZU_ENTRY_MODE':'LIVE_REACTION','BZU_EDGE_MODE':'RESEARCH_ONLY'}):
            result=engine.run_cycle(book,s,cfg)
            trade, = book['trades']
            self.assertEqual((trade['order_number'], trade['status'], trade['close_reason'], trade['exit']),
                             (185,'CLOSED','CLOSED_STALE_TIME_STOP',90.64))
            self.assertAlmostEqual(trade['max_favorable_r'], .88/1.99)
            self.assertEqual(trade['guard_exit_trigger_level'], 90.33)
            with TemporaryDirectory() as tmp:
                paths = {name: Path(tmp)/name for name in ('state_path','journal_path','live_path')}
                live_terminal.sync_active_signal_to_journal(book,s,result,cfg,engine,**paths)
                journal, = engine.read_json(paths['journal_path'])['full_v12']['trades']
                public = engine.read_json(paths['live_path'])
                self.assertEqual((journal['exit'],journal['close_reason']), (90.64,'CLOSED_STALE_TIME_STOP'))
                self.assertFalse(public['orders'])
                self.assertEqual((public['last_closed']['reason'],public['last_closed']['price']),
                                 ('CLOSED_STALE_TIME_STOP',90.64))
            text = notification_report.build(book,s,result,cfg,engine)
            self.assertNotIn('Супровід до 16:45',text)
            self.assertIn('шестигодинний тайм-стоп',text)

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

    def test_replacement_accepts_a_or_a_plus_and_needs_opposite_structure(self):
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
            self.assertIsNotNone(arbitration.stale_replacement(p,[{**fresh,'rating':'A'}],snap,cfg,engine))
            limit = {**fresh,'rating':'A','entry':98.40,'order_type':'LIMIT','action_type':'NEW_PENDING_LIMIT'}
            self.assertIsNotNone(arbitration.stale_replacement(p,[limit],snap,cfg,engine))
            self.assertIsNone(arbitration.stale_replacement(p,[{**fresh,'rating':'B+'}],snap,cfg,engine))

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
            self.assertEqual((trade['id'],trade['close_reason']), ('stale-old','CLOSED_REPLACED_BY_FRESH_SETUP'))
            current, = engine.active_plans(book)
            self.assertEqual((current['order_type'],current['entry']), ('MARKET',snap.ask))
            self.assertEqual([e['kind'] for e in result['events'] if e['kind'] in ('CLOSED','FILLED')],
                             ['CLOSED','FILLED'])

    def test_replacement_can_place_a_passive_a_limit(self):
        snap, cfg, targets = swing_fixture()
        snap = replace(snap, price=97.12, bid=97.11, ask=97.13)
        incumbent, _, _ = self.make_position(entry=97.12, entered=snap.now-16*M15, commission=True)
        regime = {**direction.market_regime(snap,cfg.tick,engine),'side_1h':1,'side_4h':0}
        book = engine.new_state(cfg)
        book['active'] = incumbent
        with patch.object(direction,'market_regime',return_value=regime), \
             patch.object(engine,'full_targets',return_value=targets), \
             patch.object(direction,'vwap_priority',return_value={'aligned':True}), \
             patch.object(engine,'advance_lifecycle'), \
             patch.dict(os.environ, {'BZU_ENTRY_MODE':'HYBRID','BZU_EDGE_MODE':'SHADOW'}):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['action'],'PLACE_LIMIT')
        self.assertFalse(engine.active_plans(book))
        self.assertEqual(len(engine.pending_plans(book)),1)
        self.assertEqual(book['trades'][0]['close_reason'],'CLOSED_REPLACED_BY_FRESH_SETUP')
        self.assertEqual([e['kind'] for e in result['events'] if e['kind'] in ('CLOSED','PLACED')],
                         ['CLOSED','PLACED'])


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

    def test_confirmed_sweep_rank_ignores_opposing_htf_and_meta_expectancy(self):
        plan = {'id':'sweep','side':1,'score':70,'htf_1h':-1,'htf_4h':-1,'entry':99.,
                'reversal_sweep_override':True}
        proof = {'sweep_reclaim':True,'near_unmitigated_fvg':False,'displacement':False}
        self.assertEqual(direction.adjusted_score(plan,proof),74.)
        unpenalized = direction.ranking(plan,99.2,.01)
        self.assertEqual(direction.ranking({**plan,'meta_decision':{'validated':True,'expected_net_r':-2}},99.2,.01),
                         unpenalized)

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
