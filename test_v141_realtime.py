"""Offline v14.1 release checks."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import bzu_policy
import bzu_execution_rules as rules
import continuous_worker as worker
import entry_adverse_selection as adverse
import full_limit_engine as engine
import market_wave
import setup_scanner as scanner
from test_pending_limit_lifecycle import MINUTE, NOW, waiting
from test_v12_40_execution import base_fixture


class RealTimeRelease(TestCase):
    def test_worker_scans_every_three_seconds_and_reports_once_per_bucket(self):
        calls = []
        checkpoint = {'report_bucket':NOW//worker.REPORT_MS-1}
        for tick in range(0, 15*MINUTE+9000, worker.SCAN_MS):
            for offset in (0, 100):
                checkpoint, _ = worker.step(NOW+tick+offset, checkpoint, lambda notify: calls.append(notify) or 0)
        self.assertEqual(len(calls), 303)
        self.assertEqual(sum(calls), 2)
        self.assertEqual(worker.SCAN_MS, 3000)

    def test_worker_serves_only_dashboard_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'index.html').write_text('live dashboard')
            (root/'live_state.json').write_text('{"schema":1}')
            (root/'signal_journal_v6_4.json').write_text('private')
            server = worker.serve_terminal(root, root/'live_state.json', 0)
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(base+'/live_state.json') as response:
                    self.assertEqual(response.read(), b'{"schema":1}')
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(base+'/signal_journal_v6_4.json')
                self.assertEqual(ctx.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_market_uses_executable_quote_in_065_atr_band(self):
        proof_pattern = 'TREND_CONTINUATION_MARKET'
        base, cfg = base_fixture()
        step = engine.TF['15m']
        rows = [engine.Candle(NOW-(20-i)*step,95.4,95.8,95.0,95.4) for i in range(20)]
        rows[-1] = engine.Candle(NOW-step,95.38,95.90,95.31,95.82)
        snap = replace(base, now=NOW, price=95.82, bid=95.81, ask=95.83,
                       ticker_ts=NOW, candles={'15m':rows})
        zone = {'kind':market_wave.BASE,'reference':95.40,'known_ts':NOW,
                'wave_proof':{'break_ts':NOW,'trigger':'BREAKOUT'}}
        with patch.object(engine.policy, 'trend_references', return_value=(95.40,95.40)):
            proof = adverse.trend_execution_trigger(snap,1,zone,95.48,1.,95.83,cfg.tick,engine)
            self.assertEqual(proof['pattern'],proof_pattern)
            self.assertEqual(proof['market_entry'],95.83)
            self.assertEqual(proof['limit_entry'],95.40)
            self.assertIsNone(adverse.trend_execution_trigger(snap,1,zone,95.48,1.,95.94,cfg.tick,engine))
        self.assertIsNone(scanner.impulse_market_entry(-1,95.50,95.4,.8,cfg.tick))
        for kind in (market_wave.BOS_IMPULSE,market_wave.EMA9_SURF,market_wave.V_REVERSAL,'TURTLE_SOUP',
                     market_wave.BASE,'SR_FLIP_RECLAIM','RECLAIM_BREAKER'):
            self.assertTrue(adverse.trend_supports_wide({'kind':kind}))

    def test_market_requires_rr_135_from_final_quote(self):
        snap,cfg = base_fixture()
        anchor = type('Anchor',(),{'side':'LONG'})()
        for room, expected in ((1.34, 'BLOCKED_NO_ROOM_TO_STRUCTURE'), (1.35, 'ACCEPTED')):
            with self.subTest(room=room), patch.object(engine.oil_risk,'structural_room',return_value=(100.+room,room)) as final_room, \
                 patch.object(scanner,'target_crossed',return_value=False):
                geometry, reason = bzu_policy.protected_targets({},snap,anchor,cfg,engine,100.,99.,1,.6,
                    active=True,market_trigger={'known_ts':NOW,'pattern':'TREND_CONTINUATION_MARKET'})
                self.assertEqual(reason,expected)
                self.assertEqual(final_room.call_args.args[1:3], (100., 1.))
                if geometry:
                    self.assertGreaterEqual(geometry['rr2'], 1.35 - 1e-8)

    def test_m5_replay_checks_lows_when_no_finer_history_exists(self):
        cfg=engine.Config(tick=.01)
        plan=waiting(placed=NOW-5*MINUTE)
        book=engine.new_state(cfg)
        engine.park_pending(book,[plan])
        bars=[engine.Candle(NOW-5*MINUTE,90.93,91.02,90.75,90.9),
              engine.Candle(NOW,90.95,91.55,90.88,91.51)]
        s=engine.Snapshot(NOW+5*MINUTE,91.51,{'5m':bars})
        self.assertEqual(engine.execution_series(s)[0],'5m')
        engine._advance_slot_lifecycle(book,s,cfg,entry_gates_checked=True)
        self.assertEqual([o['reason'] for o in book['orders']],[engine.MISSED_TARGET_REASON])

    def test_upgrade_rechecks_previously_replayed_move_between_075_and_080(self):
        plan=waiting()
        plan.update(last_bar=NOW,missed_target_r=.80)
        book=engine.new_state(engine.Config())
        engine.park_pending(book,[plan])
        s=engine.Snapshot(NOW+MINUTE,90.90,{'1m':[engine.Candle(NOW,90.86,90.94,90.74,90.90)]})
        self.assertEqual(engine.prepare_pending_limits(book,s,engine.Config()),{plan['id']})

    def test_router_cancels_incumbent_before_committing_market_fill(self):
        for same_event in (False, True):
            with self.subTest(same_event=same_event):
                result, book, snap, old = self.run_market_replacement(same_event=same_event)
                self.assertEqual(result['action'], 'ACTIVE_ENTRY')
                self.assertEqual([row['status'] for row in book['orders']], ['CANCELLED', 'FILLED'])
                self.assertEqual(book['orders'][0]['reason'], 'CANCELLED_REPLACED_BY_LIVE_SETUP')
                filled, = engine.active_plans(book)
                self.assertEqual(filled['entry'], snap.bid)
                self.assertNotEqual(filled['event_key'], old['event_key'])
                self.assertEqual(engine.pending_plans(book), [])

    def test_router_waits_with_an_unobserved_partial_minute_or_exchange_order(self):
        result, book, _, _ = self.run_market_replacement(offset=3000)
        self.assertEqual(result['action'], 'CANCEL_LIMIT')
        self.assertEqual([e['kind'] for e in result['events']], ['CANCEL_REQUESTED'])
        self.assertEqual(len(engine.pending_plans(book)), 1)
        self.assertEqual(engine.active_plans(book), [])
        result, book, _, _ = self.run_market_replacement(tracked=True)
        self.assertEqual(result['action'], 'CANCEL_LIMIT')
        self.assertEqual([row['status'] for row in book['orders']], ['CANCELLED'])
        self.assertEqual(engine.active_plans(book), [])

    def run_market_replacement(self, *, same_event=False, offset=0, tracked=False):
        from test_full_limit_engine import analysis, snapshot
        from test_pending_limit_lifecycle import TrendReplacement

        cfg = TrendReplacement.cfg
        old = waiting(-1, NOW-15*MINUTE)
        old['last_bar'] = NOW-MINUTE
        new = TrendReplacement().candidates(old,-1)
        proof = {'pattern':'TREND_CONTINUATION_MARKET','known_ts':NOW,
                 'limit_entry':90.94,'initial_risk':.98,'max_distance':.325}
        bid = 90.88
        new.update(action_type='ACTIVE_ENTRY', order_type='MARKET', entry=bid,
                   market_trigger=proof, stop=91.86, tp1=89.35, tp=89., tp2=89.,
                   entry_legs=[{'name':'ACTIVE','price':bid,'fraction':1.,'status':'PENDING'}])
        if same_event:
            new['event_key'] = old['event_key']
        book = engine.new_state(cfg)
        engine.park_pending(book,[old])
        if tracked:
            book['okx_execution'] = {'order': {'plan': old}}
        s = snapshot(NOW + offset, price=90.89)
        s.candles['15m'] = [replace(c,open=90.89,high=91.14,low=90.64,close=90.89)
                            for c in s.candles['15m']]
        s.candles['1m'] = [engine.Candle(NOW-MINUTE,90.90,90.93,90.86,90.89)]
        market = {'state':'BEARISH_ACTIVE','trend_anchor_side':-1}
        with patch.object(engine,'analyze',return_value=analysis([new])), \
             patch.object(engine,'review_waiting_conditions'), \
             patch.object(engine.policy,'execution_regime',return_value=market), \
             patch.object(engine.policy,'trend_entry_error',return_value=None), \
             patch.object(adverse,'trend_execution_trigger',return_value=proof), \
             patch.object(engine.scenario_planner,'entry_error',return_value=None), \
             patch.object(engine.oil_risk,'structural_room',return_value=(None,None)), \
             patch.object(scanner,'candidate_plan_error',return_value=None), \
             patch.object(scanner,'calculate_zone_trade_plan',return_value={'tactic':'MARKET','entry_condition':'RETEST'}), \
             patch.object(engine.scenario_planner,'focus',return_value={'developing_setup':None}):
            result=engine.run_cycle(book,s,cfg)
        return result, book, s, old

    def test_fresh_fvg_accepts_calm_approach_and_waits_for_closed_rejection_after_shock(self):
        step = engine.TF['15m']
        for side in (1, -1):
            for ratio in (.79, .95, 1., 1.01):
                with self.subTest(side=side, ratio=ratio):
                    zone = {'kind': 'FVG_PULLBACK', 'known_ts': NOW-step, 'reference': 100.,
                            'low': 99.95, 'high': 100.05}
                    rows = [engine.Candle(NOW-i*step, 100.2, 100.7, 99.7, 100.2, 10.)
                            for i in range(20, 1, -1)]
                    close = 100. + side*.1
                    open_price = close + side*ratio
                    rows.append(engine.Candle(NOW-step, open_price, max(open_price,close)+.02,
                                              min(open_price,close)-.02, close, 1000.))
                    rows.append(engine.Candle(NOW, 100., 100.55, 99.45, 100.+side*.5))
                    snap = engine.Snapshot(NOW, close, {'15m': rows})
                    label = 'WAIT_PULLBACK_REJECTION' if ratio > 1. else None
                    self.assertEqual(rules.setup_confirmation('PULLBACK_CONTINUATION', zone, snap,
                                                               side, .01, engine, False), label)
                    self.assertIsNone(rules.setup_confirmation('FVG_PULLBACK', zone,
                        replace(snap, now=NOW+step), side, .01, engine, False))

    def test_trend_limit_uses_full_target_and_keeps_longer_ttl(self):
        cfg = engine.Config(tick=.01)
        step = MINUTE
        placed = NOW + 180*step
        for trend, frame4h, minutes in ((False, -1, 60), (True, 0, 90), (True, -1, 120)):
            with self.subTest(minutes=minutes):
                ttl = bzu_policy.retest_ttl(-1, {'side_1h':-1, 'side_4h':frame4h}, trend=trend)
                self.assertEqual(ttl, minutes*step)
                plan = waiting(-1, placed)
                plan.update(exit_policy=bzu_policy.FULL_POSITION_EXIT, limit_ttl_ms=ttl, expires_ts=placed+ttl)
                price = plan['entry'] - .8*plan['risk']
                book = engine.new_state(cfg)
                engine.park_pending(book, [plan])
                probe = engine.Snapshot(placed+75*step, price, {}, ticker_ts=placed+75*step)
                engine.prepare_pending_limits(book, probe, cfg)
                self.assertEqual(plan['expires_ts'], placed+ttl)
                engine.review_waiting_conditions(book, plan, probe, cfg)
                if trend:
                    self.assertEqual(engine.pending_plans(book), [plan])
                    engine.review_waiting_conditions(book, plan, replace(probe, price=plan['tp']), cfg)
                self.assertEqual(book['orders'][-1]['reason'], engine.MISSED_TARGET_REASON)

    def test_bos_proof_survives_for_limit_but_expires_for_market_and_after_invalidation(self):
        from test_v13_2_trend import TrendBreakout

        step = engine.TF['15m']
        with patch.object(engine.policy, 'trend_references', return_value=(100., 100.)):
            snap, cfg = TrendBreakout().breakout_fixture(1, '15m')
            zone, = market_wave.bos_impulse_zones(snap, cfg.tick, engine)
            bars = snap.candles['15m'] + [engine.Candle(snap.now+i*step, 100.58, 100.62, 100.47, 100.54)
                                        for i in range(5)]
            later = replace(snap, now=snap.now+5*step, ticker_ts=snap.now+5*step, candles={'15m':bars})
            regime = {'state':'BULLISH_ACTIVE', 'trend_anchor_side':1, 'side_1h':1, 'side_4h':1}
            self.assertFalse(market_wave.bos_impulse_zones(later, cfg.tick, engine))
            self.assertTrue(scanner.zone_origin(zone, later, 1, cfg.tick, engine, cfg, regime)['confirmed'])
            self.assertIsNone(adverse.trend_execution_trigger(later, 1, zone, zone['reference'], .5,
                                                              later.ask, cfg.tick, engine))
            broken = replace(later, candles={'15m':bars[:-1] + [replace(bars[-1], low=100.10, close=100.20)]})
            self.assertFalse(scanner.zone_origin(zone, broken, 1, cfg.tick, engine, cfg, regime)['confirmed'])
